"""Stage 5: execute ISCE2's generated run_01..run_NN scripts. Generalizes
the line-level marker/resume pattern already proven in this project for
run_16 (SNAPHU unwrapping, which OOM'd at 4-way parallelism) to every
run-file uniformly, discovered dynamically rather than hardcoded to exactly
16 files (the exact count/naming can vary by ISCE2 version/options)."""
from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from orchestrator.checkpoint import is_done, mark_done
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


def _natural_key(path: Path):
    # run_01_x, run_02_y, ..., run_16_unwrap -- sort numerically on the
    # leading run number rather than lexicographically (which would put
    # run_10 before run_2).
    m = re.match(r"run_(\d+)", path.name)
    return int(m.group(1)) if m else 0


def _discover_run_files(paths: RunPaths) -> list[Path]:
    run_files_dir = paths.stack_proc / "run_files"
    files = [p for p in run_files_dir.glob("run_*") if p.is_file() and not p.name.endswith(".job")]
    return sorted(files, key=_natural_key)


def _run_one_line(run_dir, marker_prefix: str, line_num: int, cmd: str, run_file_name: str, log_fn) -> tuple[int, bool, str]:
    marker_name = f"{marker_prefix}_line{line_num}"
    if is_done(run_dir, marker_name):
        log_fn(f"[{run_file_name}] line {line_num}: skipped (already done)")
        return line_num, True, "skipped"

    # Stream stdout/stderr live, line by line, as the subprocess produces it
    # -- rather than capturing everything silently and only reporting a
    # one-line summary once the whole command finishes. This is what makes
    # the dashboard's log view show real-time activity (matching what you'd
    # see running the same command directly in a WSL terminal) instead of
    # looking frozen on a single "running" status for tens of minutes.
    log_fn(f"[{run_file_name}] line {line_num}: $ {cmd}")
    # ISCE2's compiled steps (e.g. topozero) write to stdout via C stdio,
    # which glibc auto-switches from line-buffered to fully block-buffered
    # (several KB) whenever stdout isn't a real terminal -- exactly the case
    # here, since we're capturing through a pipe. Without stdbuf, minutes of
    # real progress can sit unflushed in the child's own buffer, making the
    # log look frozen even though the process is actively working. stdbuf
    # forces line buffering via LD_PRELOAD, restoring true live output for
    # both Python and compiled steps alike.
    process = subprocess.Popen(
        f"stdbuf -oL -eL {cmd}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    tail_lines = []
    for out_line in process.stdout:
        out_line = out_line.rstrip()
        if out_line:
            log_fn(f"[{run_file_name}] line {line_num} | {out_line}")
            tail_lines.append(out_line)
            tail_lines = tail_lines[-50:]  # keep only the last 50 for the error message, if needed
    returncode = process.wait()

    if returncode != 0:
        return line_num, False, "\n".join(tail_lines)
    mark_done(run_dir, marker_name)
    log_fn(f"[{run_file_name}] line {line_num}: done")
    return line_num, True, "done"


def _execute_run_file(run_dir: Path, run_file: Path, parallelism: int, log_fn) -> None:
    lines = [l.strip() for l in run_file.read_text().splitlines() if l.strip()]
    marker_prefix = f"stack_{run_file.name}"

    if parallelism <= 1:
        for i, cmd in enumerate(lines, start=1):
            line_num, ok, detail = _run_one_line(run_dir, marker_prefix, i, cmd, run_file.name, log_fn)
            if not ok:
                raise RuntimeError(f"{run_file.name} line {line_num} failed:\n{detail}")
        return

    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(_run_one_line, run_dir, marker_prefix, i, cmd, run_file.name, log_fn): i
            for i, cmd in enumerate(lines, start=1)
        }
        failures = []
        for future in as_completed(futures):
            line_num, ok, detail = future.result()
            if not ok:
                failures.append((line_num, detail))
        if failures:
            msgs = "\n".join(f"line {n}: {d}" for n, d in failures)
            raise RuntimeError(f"{run_file.name} had {len(failures)} failed line(s):\n{msgs}")


def run(config: RunConfig, paths: RunPaths, log_fn=print, progress_fn=None) -> None:
    run_files = _discover_run_files(paths)
    if not run_files:
        raise RuntimeError(f"No run_* files found in {paths.stack_proc / 'run_files'} -- did stack_setup run?")

    total_files = len(run_files)
    for idx, run_file in enumerate(run_files, start=1):
        if progress_fn:
            progress_fn(idx - 1, total_files, f"ISCE2 step {idx} of {total_files}: {run_file.name}")

        stage_marker = f"stack_execute_{run_file.name}_complete"
        if is_done(config.run_dir, stage_marker):
            log_fn(f"[{run_file.name}] already complete, skipping")
            if progress_fn:
                progress_fn(idx, total_files, f"ISCE2 step {idx} of {total_files}: {run_file.name} (skipped)")
            continue

        # SNAPHU unwrapping is memory-heavy (~2GB/instance, observed to OOM
        # at 4-way parallelism in this project) -- any run-file whose name
        # indicates the unwrap step uses the configured, conservative
        # run16_parallelism; every other stage uses num_proc, matching
        # stackSentinel's own assumption that those steps parallelize safely.
        is_unwrap_step = "unwrap" in run_file.name.lower()
        parallelism = config.network.run16_parallelism if is_unwrap_step else config.network.num_proc

        log_fn(f"Starting {run_file.name} (parallelism={parallelism}, unwrap_step={is_unwrap_step})")
        _execute_run_file(config.run_dir, run_file, parallelism, log_fn)
        mark_done(config.run_dir, stage_marker)
        log_fn(f"Finished {run_file.name}")
        if progress_fn:
            progress_fn(idx, total_files, f"ISCE2 step {idx} of {total_files}: {run_file.name} (done)")
