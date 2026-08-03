"""Stage 2: DEM acquisition via dem_stitcher (Copernicus GLO-30, ellipsoidal
WGS84 height -- deliberately not EGM96/geoid height, since a datum mismatch
here is a known way to get wildly wrong displacement values). bbox always
comes from config, padded by dem.padding_deg -- never hardcoded."""
from __future__ import annotations

import subprocess

import rasterio
from dem_stitcher import stitch_dem

from orchestrator.checkpoint import run_checkpointed
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


def run(config: RunConfig, paths: RunPaths) -> None:
    def _do():
        lon_min, lat_min, lon_max, lat_max = config.aoi.bbox
        pad = config.dem.padding_deg
        bbox = [lon_min - pad, lat_min - pad, lon_max + pad, lat_max + pad]

        X, profile = stitch_dem(
            bbox,
            dem_name=config.dem.source,
            dst_ellipsoidal_height=True,
            dst_area_or_point="Point",
        )
        paths.dem.mkdir(parents=True, exist_ok=True)
        with rasterio.open(paths.dem_tif, "w", **profile) as dst:
            dst.write(X, 1)

        subprocess.run(
            ["gdal2isce_xml.py", "-i", str(paths.dem_tif)],
            check=True, cwd=str(paths.dem),
        )

    run_checkpointed(config.run_dir, "dem_download", _do)
