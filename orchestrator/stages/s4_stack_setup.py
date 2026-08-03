"""Stage 4: ISCE2 stackSentinel.py setup. This is the main per-AOI-tunable
step -- network design (connections/looks) can OOM SNAPHU or blow up
processing time depending on THIS AOI's size and terrain, so a cost
estimate is computed here for Gate A review rather than silently trusting
the config defaults for every region alike."""
from __future__ import annotations

import json
import math
import subprocess

from orchestrator.checkpoint import run_checkpointed
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths

# Rough, deliberately approximate Sentinel-1 IW ground pixel spacing before
# multilooking (meters) -- used only to give the reviewer a ballpark size
# estimate, not for any processing calculation.
_APPROX_RANGE_PX_M = 2.3
_APPROX_AZIMUTH_PX_M = 14.0


def estimate_network_cost(config: RunConfig, num_scenes: int) -> dict:
    c = config.network.connections
    # nearest-neighbor connection network: each date links to its c nearest
    # neighbors in time; edge dates (start/end) have fewer than c available.
    # This slightly overcounts in the same way stackSentinel's -c flag does
    # not perfectly hit N*c due to edge effects, hence "approx".
    approx_pairs = max(0, num_scenes * c - c * (c + 1) // 2)

    lon_min, lat_min, lon_max, lat_max = config.aoi.bbox
    lat_center = (lat_min + lat_max) / 2
    width_m = (lon_max - lon_min) * 111320 * math.cos(math.radians(lat_center))
    height_m = (lat_max - lat_min) * 110540

    range_px = width_m / (_APPROX_RANGE_PX_M * config.network.range_looks)
    azimuth_px = height_m / (_APPROX_AZIMUTH_PX_M * config.network.azimuth_looks)
    est_pixels_after_looks = max(1, range_px) * max(1, azimuth_px)

    # SNAPHU's own memory scales roughly linearly with pixel count; ~2GB per
    # instance was the observed ceiling for an AOI on the order of 10^6
    # post-look pixels in this project's own prior run -- used here only as
    # an order-of-magnitude anchor, not a precise model.
    est_snaphu_mb_per_instance = max(200, (est_pixels_after_looks / 1_000_000) * 2000)

    return {
        "num_scenes": num_scenes,
        "approx_interferogram_pairs": approx_pairs,
        "approx_pixels_after_multilook": int(est_pixels_after_looks),
        "approx_snaphu_mb_per_instance": round(est_snaphu_mb_per_instance),
        "configured_run16_parallelism": config.network.run16_parallelism,
        "approx_snaphu_peak_mb": round(est_snaphu_mb_per_instance * config.network.run16_parallelism),
        "note": "Rough order-of-magnitude estimate for Gate A review, not a precise prediction.",
    }


def run(config: RunConfig, paths: RunPaths, num_scenes: int) -> dict:
    cost = estimate_network_cost(config, num_scenes)
    review_path = paths.review / "network_design_summary.json"

    def _do():
        paths.aux.mkdir(parents=True, exist_ok=True)
        paths.stack_proc.mkdir(parents=True, exist_ok=True)
        paths.review.mkdir(parents=True, exist_ok=True)
        with open(review_path, "w") as f:
            json.dump(cost, f, indent=2)

        lon_min, lat_min, lon_max, lat_max = config.aoi.bbox
        bbox_str = f"{lat_min} {lat_max} {lon_min} {lon_max}"  # ISCE2 wants S N W E

        subprocess.run(
            [
                "stackSentinel.py",
                "-s", str(paths.slc),
                "-o", str(paths.orbits),
                "-a", str(paths.aux),
                "-d", str(paths.dem_tif),
                "-b", bbox_str,
                "-W", "interferogram",
                "-C", "NESD",
                "-c", str(config.network.connections),
                "-z", str(config.network.azimuth_looks),
                "-r", str(config.network.range_looks),
                "-u", "snaphu",
                "--num_proc", str(config.network.num_proc),
                "--num_proc4topo", str(config.network.num_proc4topo),
            ],
            check=True,
            cwd=str(paths.stack_proc),
        )

    run_checkpointed(config.run_dir, "stack_setup", _do)
    return cost
