"""
Background job launching/killing. The core rule: job state lives entirely
in files on disk (status.json, written by the orchestrator process itself),
never only in Streamlit's session memory -- this is what makes "resume
monitoring after closing the browser tab" and "survive a Streamlit restart"
both work for free, since a fresh Streamlit process just reads the same
files.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil

ORCHESTRATOR_HOME = os.path.expanduser("~/insar-automation")
CLI_MODULE = "orchestrator.cli"


def _python_exe() -> str:
    return sys.executable


def launch_run(config_path: str) -> int:
    """Starts a new run (or resumes an existing one, since the orchestrator
    checks its own markers first). Returns the launched PID."""
    proc = subprocess.Popen(
        [_python_exe(), "-m", CLI_MODULE, "run", "--config", config_path],
        cwd=ORCHESTRATOR_HOME,
        start_new_session=True,  # detach -- survives Streamlit restart, not in its signal path
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.pid


def approve_gate_a(run_id: str, network_overrides: dict | None = None) -> int:
    args = [_python_exe(), "-m", CLI_MODULE, "approve-gate-a", "--run-id", run_id]
    if network_overrides:
        import json
        args += ["--network-overrides", json.dumps(network_overrides)]
    proc = subprocess.Popen(
        args, cwd=ORCHESTRATOR_HOME, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.pid


def approve_gate_b(run_id: str, lat: float, lon: float) -> int:
    proc = subprocess.Popen(
        [_python_exe(), "-m", CLI_MODULE, "approve-gate-b", "--run-id", run_id,
         "--lat", str(lat), "--lon", str(lon)],
        cwd=ORCHESTRATOR_HOME, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.pid


def is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    return psutil.pid_exists(pid)


def stop_run(pid: int | None, pgid: int | None) -> bool:
    """Kills the whole process group, not just the orchestrator PID --
    otherwise an in-flight SNAPHU/ISCE2 child process spawned by the
    orchestrator would keep running after the orchestrator itself dies."""
    if pid is None:
        return False
    target_pgid = pgid or pid
    try:
        os.killpg(target_pgid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # fall back to killing just the tracked PID if group kill isn't permitted
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False
