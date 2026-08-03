"""Standard sub-directory layout inside a run directory, shared by every stage."""
from __future__ import annotations
from pathlib import Path


class RunPaths:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.slc = self.run_dir / "slc"
        self.orbits = self.run_dir / "orbits"
        self.aux = self.run_dir / "aux"
        self.dem = self.run_dir / "dem"
        self.stack_proc = self.run_dir / "stack_proc"
        self.mintpy = self.run_dir / "mintpy"
        self.deliverables = self.run_dir / "deliverables"
        self.review = self.run_dir / "review"
        self.logs = self.run_dir / "logs"

    def ensure_all(self) -> None:
        for p in [self.slc, self.orbits, self.aux, self.dem, self.stack_proc,
                  self.mintpy, self.deliverables, self.review, self.logs]:
            p.mkdir(parents=True, exist_ok=True)

    @property
    def dem_tif(self) -> Path:
        return self.dem / "dem.tif"

    @property
    def mintpy_cfg(self) -> Path:
        return self.mintpy / "smallbaselineApp.cfg"
