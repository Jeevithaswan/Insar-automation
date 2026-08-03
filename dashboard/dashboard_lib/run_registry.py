"""Discovers existing runs under the runs/ base directory, for populating
the Monitor/Results page run-selector."""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator import status as status_mod

DEFAULT_BASE_DIR = os.path.expanduser("~/insar-automation/runs")


def list_runs(base_dir: str = DEFAULT_BASE_DIR) -> list[dict]:
    base = Path(base_dir)
    if not base.exists():
        return []
    runs = []
    for run_dir in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not run_dir.is_dir() or run_dir.name.startswith("_test"):
            continue
        if not (run_dir / "config_used.yaml").exists():
            continue
        s = status_mod.read_status(run_dir)
        runs.append({
            "run_id": run_dir.name,
            "current_stage": s.get("current_stage"),
            "needs_review": s.get("needs_review"),
            "error": s.get("error"),
            "pid": s.get("pid"),
        })
    return runs
