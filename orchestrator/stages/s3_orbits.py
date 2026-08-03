"""Stage 3: precise orbit file download. Uses the `eof` CLI (from the
sentineleof package -- note the import name is `eof`, not `sentineleof`,
a documented gotcha worth remembering if this ever gets reimplemented as a
direct Python call instead of shelling out)."""
from __future__ import annotations

import subprocess

from orchestrator.checkpoint import run_checkpointed
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


def run(config: RunConfig, paths: RunPaths) -> None:
    def _do():
        paths.orbits.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "eof",
                "-p", str(paths.slc),
                "--save-dir", str(paths.orbits),
                "--orbit-type", "precise",
                "--force-asf",
            ],
            check=True,
        )

    run_checkpointed(config.run_dir, "orbit_download", _do)
