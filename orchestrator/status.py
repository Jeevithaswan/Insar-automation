"""
Atomic status.json read/write. This is the single source of truth the
dashboard polls -- job state lives entirely in this file on disk, never
only in the orchestrator process's memory or in Streamlit's session state.
That's what makes "resume monitoring after closing the browser tab" and
"survive a Streamlit restart" both just work, for free.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

STAGES = [
    "env_precheck",
    "search_download",
    "dem_download",
    "orbit_download",
    "stack_setup",
    "stack_execute",
    "mintpy_prep",
    "mintpy_run",
    "deliverables",
]

STAGE_LABELS = {
    "env_precheck": "Environment check",
    "search_download": "Search & download SLCs",
    "dem_download": "DEM download",
    "orbit_download": "Orbit files",
    "stack_setup": "ISCE2 stack setup (Gate A follows)",
    "stack_execute": "ISCE2 stack processing (run_01..run_16)",
    "mintpy_prep": "MintPy prep + reference point candidates (Gate B follows)",
    "mintpy_run": "MintPy inversion",
    "deliverables": "Final deliverables",
}


def status_path(run_dir: Path) -> Path:
    return run_dir / "status.json"


def read_status(run_dir: Path) -> dict[str, Any]:
    p = status_path(run_dir)
    if not p.exists():
        return {
            "run_id": run_dir.name,
            "current_stage": None,
            "stage_status": {s: "pending" for s in STAGES},
            "pid": None,
            "pgid": None,
            "started_at": None,
            "updated_at": None,
            "needs_review": None,   # None | "gate_a" | "gate_b"
            "error": None,
            "progress": None,        # {"current": int, "total": int, "label": str}
        }
    with open(p) as f:
        return json.load(f)


def write_status(run_dir: Path, status: dict[str, Any]) -> None:
    """Atomic write: write to a temp file in the same directory, then
    os.replace -- a reader (the dashboard) never sees a partially-written
    file, even if it polls mid-write."""
    status["updated_at"] = time.time()
    run_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=run_dir, prefix=".status_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp_path, status_path(run_dir))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def update_status(run_dir: Path, **kwargs) -> dict[str, Any]:
    status = read_status(run_dir)
    status.update(kwargs)
    write_status(run_dir, status)
    return status


def set_stage_status(run_dir: Path, stage: str, value: str) -> dict[str, Any]:
    status = read_status(run_dir)
    status["current_stage"] = stage
    status["stage_status"][stage] = value
    write_status(run_dir, status)
    return status


def set_needs_review(run_dir: Path, gate: Optional[str]) -> dict[str, Any]:
    return update_status(run_dir, needs_review=gate)


def set_pid(run_dir: Path, pid: Optional[int], pgid: Optional[int]) -> dict[str, Any]:
    return update_status(run_dir, pid=pid, pgid=pgid)


def set_progress(run_dir: Path, current: int, total: int, label: str) -> dict[str, Any]:
    """Fine-grained progress within a stage (e.g. '3 of 16 ISCE2 steps',
    '1 of 2 scenes downloaded') -- separate from stage_status, which only
    tracks pending/running/done at the whole-stage level. Cleared (set to
    None) whenever a new stage starts, so a stale bar from a finished
    stage never lingers on screen."""
    return update_status(run_dir, progress={"current": current, "total": total, "label": label})


def clear_progress(run_dir: Path) -> dict[str, Any]:
    return update_status(run_dir, progress=None)


def is_process_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
