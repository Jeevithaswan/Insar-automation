"""Seek-from-end log tailing -- reads only the last N bytes on each poll
rather than the whole file, since ISCE2/MintPy logs can grow large over an
hour-plus run."""
from __future__ import annotations

from pathlib import Path


def tail(path: Path, max_bytes: int = 20_000) -> str:
    if not path.exists():
        return "(no log yet)"
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = "... (truncated) ...\n" + text
    return text
