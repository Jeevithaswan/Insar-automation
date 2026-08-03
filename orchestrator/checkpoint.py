"""
Generic checkpoint/resume helper. Every stage (and, within stage 5, every
individual run_NN command line) uses the exact same marker-file pattern:
write a marker only after a step succeeds, and skip any step whose marker
already exists. This generalizes the line-level resume pattern already
proven for SNAPHU unwrapping to the whole pipeline uniformly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

SKIPPED = "skipped"
DONE = "done"
FAILED = "failed"


def marker_dir(run_dir: Path) -> Path:
    d = run_dir / ".done_markers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_done(run_dir: Path, marker_name: str) -> bool:
    return (marker_dir(run_dir) / f"{marker_name}.done").exists()


def mark_done(run_dir: Path, marker_name: str) -> None:
    (marker_dir(run_dir) / f"{marker_name}.done").touch()


def clear_marker(run_dir: Path, marker_name: str) -> None:
    p = marker_dir(run_dir) / f"{marker_name}.done"
    if p.exists():
        p.unlink()


def run_checkpointed(run_dir: Path, marker_name: str, fn: Callable[[], None]) -> str:
    """Run fn() unless marker_name is already marked done. Marker is only
    written after fn() completes without raising -- a failed attempt leaves
    no marker, so a retry/resume will run it again rather than silently
    treating a crash as success."""
    if is_done(run_dir, marker_name):
        return SKIPPED
    fn()
    mark_done(run_dir, marker_name)
    return DONE


def clear_stage_and_after(run_dir: Path, all_marker_names_in_order: list[str], from_marker: str) -> None:
    """Used by '--stage N --force': clears the marker for from_marker and
    every marker that logically comes after it, so a forced rerun of one
    stage also invalidates anything downstream that depended on it."""
    if from_marker not in all_marker_names_in_order:
        raise ValueError(f"Unknown marker: {from_marker}")
    idx = all_marker_names_in_order.index(from_marker)
    for name in all_marker_names_in_order[idx:]:
        clear_marker(run_dir, name)
