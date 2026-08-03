"""Stage 7: MintPy inversion, resumed from the Gate-B-approved reference
point. Writes the approved lat/lon into the cfg, then lets MintPy use its
own native step-skipping (--start reference_point) to resume rather than
custom markers -- load_data/modify_network are already done from stage 6
and MintPy already knows that."""
from __future__ import annotations

import subprocess

from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


def write_approved_reference_point(config: RunConfig, paths: RunPaths, lat: float, lon: float) -> None:
    config.mintpy.reference_lalo = [lat, lon]
    with open(paths.mintpy_cfg, "a") as f:
        f.write(f"\nmintpy.reference.lalo      = {lat}, {lon}\n")
    # keep config_used.yaml in sync for reproducibility
    config.freeze_into_run_dir()


def run(config: RunConfig, paths: RunPaths, log_fn=print) -> None:
    if config.mintpy.reference_lalo is None:
        raise ValueError(
            "mintpy.reference_lalo is not set -- Gate B must be approved "
            "(write_approved_reference_point called) before running this stage."
        )

    process = subprocess.Popen(
        ["smallbaselineApp.py", str(paths.mintpy_cfg), "--start", "reference_point"],
        cwd=str(paths.mintpy),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in process.stdout:
        log_fn(line.rstrip())
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(f"MintPy run failed with exit code {returncode}")
