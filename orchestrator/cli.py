"""
Orchestrator CLI entrypoint. Designed to be launched by the dashboard as a
detached background process (subprocess.Popen(..., start_new_session=True))
but works identically run directly for headless testing -- all state lives
in status.json/markers on disk, not in this process's memory, so either
caller sees the same thing.

Commands:
  run --config PATH.yaml [--from-stage N]   start or resume a run
  approve-gate-a --run-id ID [--network-overrides JSON] [--base-dir DIR]
  approve-gate-b --run-id ID --lat LAT --lon LON [--base-dir DIR]
  status --run-id ID [--base-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from orchestrator import checkpoint, status as status_mod
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths
from orchestrator.stages import (
    s0_env_precheck, s1_search_download, s2_dem, s3_orbits,
    s4_stack_setup, s5_stack_execute, s6_mintpy_prep, s7_mintpy_run, s8_deliverables,
)

DEFAULT_BASE_DIR = os.path.expanduser("~/insar-automation/runs")


def _load_frozen_config(run_id: str, base_dir: str) -> RunConfig:
    cfg_path = Path(base_dir) / run_id / "config_used.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No frozen config found at {cfg_path} -- has this run been started?")
    return RunConfig.from_yaml(str(cfg_path))


def _log(run_dir: Path, msg: str) -> None:
    print(msg, flush=True)
    log_path = run_dir / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(msg + "\n")


def run_pipeline(config: RunConfig, from_stage: str | None = None) -> None:
    paths = RunPaths(config.run_dir)
    paths.ensure_all()
    config.freeze_into_run_dir()

    pid = os.getpid()
    try:
        pgid = os.getpgrp()
    except (AttributeError, OSError):
        pgid = pid
    status_mod.set_pid(config.run_dir, pid, pgid)
    status_mod.set_needs_review(config.run_dir, None)
    status_mod.update_status(config.run_dir, error=None)

    def log(msg):
        _log(config.run_dir, msg)

    def progress(current, total, label):
        status_mod.set_progress(config.run_dir, current, total, label)

    try:
        log("=== env_precheck ===")
        status_mod.set_stage_status(config.run_dir, "env_precheck", "running")
        s0_env_precheck.run(config, paths)
        status_mod.set_stage_status(config.run_dir, "env_precheck", "done")

        log("=== search_download ===")
        status_mod.set_stage_status(config.run_dir, "search_download", "running")
        downloaded = s1_search_download.download_scenes(config, paths, progress_fn=progress)
        status_mod.clear_progress(config.run_dir)
        log(f"Downloaded/verified {len(downloaded)} scene(s)")
        status_mod.set_stage_status(config.run_dir, "search_download", "done")

        log("=== dem_download ===")
        status_mod.set_stage_status(config.run_dir, "dem_download", "running")
        s2_dem.run(config, paths)
        status_mod.set_stage_status(config.run_dir, "dem_download", "done")

        log("=== orbit_download ===")
        status_mod.set_stage_status(config.run_dir, "orbit_download", "running")
        s3_orbits.run(config, paths)
        status_mod.set_stage_status(config.run_dir, "orbit_download", "done")

        log("=== stack_setup ===")
        status_mod.set_stage_status(config.run_dir, "stack_setup", "running")
        cost = s4_stack_setup.run(config, paths, num_scenes=len(downloaded))
        log(f"Network cost estimate: {json.dumps(cost)}")
        status_mod.set_stage_status(config.run_dir, "stack_setup", "done")

        if not checkpoint.is_done(config.run_dir, "gate_a_approved"):
            log("=== Gate A: network design review needed ===")
            status_mod.set_needs_review(config.run_dir, "gate_a")
            return

        log("=== stack_execute ===")
        status_mod.set_stage_status(config.run_dir, "stack_execute", "running")
        s5_stack_execute.run(config, paths, log_fn=log, progress_fn=progress)
        status_mod.clear_progress(config.run_dir)
        status_mod.set_stage_status(config.run_dir, "stack_execute", "done")

        log("=== mintpy_prep ===")
        status_mod.set_stage_status(config.run_dir, "mintpy_prep", "running")
        candidates = s6_mintpy_prep.run(config, paths)
        log(f"Found {len(candidates)} reference point candidate(s)")
        status_mod.set_stage_status(config.run_dir, "mintpy_prep", "done")

        if not checkpoint.is_done(config.run_dir, "gate_b_approved"):
            log("=== Gate B: reference point review needed ===")
            status_mod.set_needs_review(config.run_dir, "gate_b")
            return

        log("=== mintpy_run ===")
        status_mod.set_stage_status(config.run_dir, "mintpy_run", "running")
        s7_mintpy_run.run(config, paths, log_fn=log)
        status_mod.set_stage_status(config.run_dir, "mintpy_run", "done")

        log("=== deliverables ===")
        status_mod.set_stage_status(config.run_dir, "deliverables", "running")
        summary = s8_deliverables.run(config, paths)
        log(f"Deliverables summary: {json.dumps(summary)}")
        status_mod.set_stage_status(config.run_dir, "deliverables", "done")

        log("=== RUN COMPLETE ===")

    except Exception as e:
        log(f"ERROR: {e}")
        status_mod.update_status(config.run_dir, error=str(e))
        raise


def cmd_run(args):
    config = RunConfig.from_yaml(args.config)
    run_pipeline(config)


def cmd_approve_gate_a(args):
    config = _load_frozen_config(args.run_id, args.base_dir)
    if args.network_overrides:
        overrides = json.loads(args.network_overrides)
        for k, v in overrides.items():
            setattr(config.network, k, v)
        config.freeze_into_run_dir()
    checkpoint.mark_done(config.run_dir, "gate_a_approved")
    run_pipeline(config)


def cmd_approve_gate_b(args):
    config = _load_frozen_config(args.run_id, args.base_dir)
    paths = RunPaths(config.run_dir)
    s7_mintpy_run.write_approved_reference_point(config, paths, args.lat, args.lon)
    checkpoint.mark_done(config.run_dir, "gate_b_approved")
    run_pipeline(config)


def cmd_status(args):
    run_dir = Path(args.base_dir) / args.run_id
    print(json.dumps(status_mod.read_status(run_dir), indent=2))


def main():
    parser = argparse.ArgumentParser(description="InSAR pipeline orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Start or resume a run from its config")
    p_run.add_argument("--config", required=True)
    p_run.set_defaults(func=cmd_run)

    p_ga = sub.add_parser("approve-gate-a", help="Approve network design, resume from stack_execute")
    p_ga.add_argument("--run-id", required=True)
    p_ga.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    p_ga.add_argument("--network-overrides", help="JSON dict of network config fields to override")
    p_ga.set_defaults(func=cmd_approve_gate_a)

    p_gb = sub.add_parser("approve-gate-b", help="Approve reference point, resume from mintpy_run")
    p_gb.add_argument("--run-id", required=True)
    p_gb.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    p_gb.add_argument("--lat", type=float, required=True)
    p_gb.add_argument("--lon", type=float, required=True)
    p_gb.set_defaults(func=cmd_approve_gate_b)

    p_status = sub.add_parser("status", help="Print status.json for a run")
    p_status.add_argument("--run-id", required=True)
    p_status.add_argument("--base-dir", default=DEFAULT_BASE_DIR)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
