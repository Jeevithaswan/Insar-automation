"""Stage 6: MintPy prep. Renders smallbaselineApp.cfg (no reference point
yet -- that's Gate B's job), runs MintPy through load_data + modify_network,
then computes the coherence/connected-component products needed for the
reference-point candidate picker -- generalized from this project's own
reference-point selection logic (coherence threshold, elevation-matched,
valid connected-component region, distance-balanced), but now returning
top-N ranked candidates for human review instead of silently picking one,
and auto-widening the search radius if nothing is found nearby, instead of
requiring a manual redo like this project needed once."""
from __future__ import annotations

import json
import subprocess

import h5py
import numpy as np

from orchestrator.checkpoint import run_checkpointed
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths

CFG_TEMPLATE = """\
mintpy.load.processor      = isce
mintpy.load.metaFile       = ../stack_proc/reference/{reference_swath}.xml
mintpy.load.baselineDir    = ../stack_proc/baselines
mintpy.load.unwFile        = ../stack_proc/merged/interferograms/*/filt_fine.unw
mintpy.load.corFile        = ../stack_proc/merged/interferograms/*/filt_fine.cor
mintpy.load.connCompFile   = ../stack_proc/merged/interferograms/*/filt_fine.unw.conncomp
mintpy.load.demFile        = ../stack_proc/merged/geom_reference/hgt.rdr
mintpy.load.lookupYFile    = ../stack_proc/merged/geom_reference/lat.rdr
mintpy.load.lookupXFile    = ../stack_proc/merged/geom_reference/lon.rdr
mintpy.load.incAngleFile   = ../stack_proc/merged/geom_reference/los.rdr
mintpy.load.azAngleFile    = ../stack_proc/merged/geom_reference/los.rdr
mintpy.load.shadowMaskFile = ../stack_proc/merged/geom_reference/shadowMask.rdr
mintpy.compute.cluster     = local
mintpy.compute.numWorker   = {num_worker}

mintpy.unwrapError.method  = {unwrap_error_method}
mintpy.deramp              = {deramp}
mintpy.troposphericDelay.method = {tropo_method}
mintpy.troposphericDelay.weatherModel = {tropo_weather_model}
mintpy.topographicResidual = {topographic_residual}
"""


def _find_reference_swath(paths: RunPaths) -> str:
    """Not every AOI has burst data in every IW sub-swath -- stackSentinel
    only writes an IW*.xml file for swaths that actually got cropped burst
    data (e.g. an AOI near a swath edge may only ever populate IW2). Picking
    the first swath that actually exists avoids hardcoding IW1, which broke
    for a real test AOI that only had data in IW2."""
    candidates = sorted(paths.stack_proc.glob("reference/IW*.xml"))
    if not candidates:
        raise RuntimeError(f"No reference IW*.xml found under {paths.stack_proc / 'reference'}")
    return candidates[0].stem


def render_cfg(config: RunConfig, paths: RunPaths) -> None:
    paths.mintpy.mkdir(parents=True, exist_ok=True)
    content = CFG_TEMPLATE.format(
        reference_swath=_find_reference_swath(paths),
        num_worker=min(4, config.network.num_proc),
        unwrap_error_method=config.mintpy.unwrap_error_method,
        deramp=config.mintpy.deramp,
        tropo_method=config.mintpy.tropo_method if config.mintpy.tropo_enabled else "no",
        tropo_weather_model=config.mintpy.tropo_weather_model,
        topographic_residual="yes" if config.mintpy.topographic_residual else "no",
    )
    paths.mintpy_cfg.write_text(content)


def _load_data_and_modify_network(paths: RunPaths) -> None:
    subprocess.run(
        ["smallbaselineApp.py", str(paths.mintpy_cfg), "--dostep", "load_data"],
        check=True, cwd=str(paths.mintpy),
    )
    subprocess.run(
        ["smallbaselineApp.py", str(paths.mintpy_cfg), "--dostep", "modify_network"],
        check=True, cwd=str(paths.mintpy),
    )


def _compute_coherence_mask_products(paths: RunPaths) -> None:
    ifgram_stack = paths.mintpy / "inputs" / "ifgramStack.h5"
    subprocess.run(
        ["generate_mask.py", str(ifgram_stack), "--nonzero", "-o", str(paths.mintpy / "maskConnComp.h5"), "--update"],
        check=True, cwd=str(paths.mintpy),
    )
    subprocess.run(
        ["temporal_average.py", str(ifgram_stack), "--dataset", "coherence",
         "-o", str(paths.mintpy / "avgSpatialCoh.h5"), "--update"],
        check=True, cwd=str(paths.mintpy),
    )


def pick_reference_candidates(
    paths: RunPaths,
    aoi_center_lalo: tuple[float, float],
    reliability_threshold: float,
    search_radius_km: float,
    top_n: int = 10,
    max_widenings: int = 6,
) -> list[dict]:
    """Returns up to top_n ranked reference-point candidates. Auto-widens
    search_radius_km (doubling each time) if nothing valid is found within
    it -- this generalizes the manual redo this project needed once
    (original pick had no valid data within 17.75km of the AOI center) into
    a built-in behavior for any AOI, rather than requiring a human to
    notice and rerun by hand."""
    with h5py.File(paths.mintpy / "maskConnComp.h5", "r") as f:
        mask = f["mask"][:]
    with h5py.File(paths.mintpy / "avgSpatialCoh.h5", "r") as f:
        avg_coh = f["coherence"][:]
    with h5py.File(paths.mintpy / "inputs" / "geometryRadar.h5", "r") as f:
        lat = f["latitude"][:]
        lon = f["longitude"][:]
        hgt = f["height"][:]
        shadow = f["shadowMask"][:] if "shadowMask" in f else np.zeros_like(lat, dtype=bool)

    center_lat, center_lon = aoi_center_lalo
    center_row, center_col = np.unravel_index(
        np.argmin((lat - center_lat) ** 2 + (lon - center_lon) ** 2), lat.shape
    )
    center_elev = hgt[center_row, center_col]

    dist_km = np.sqrt(((lat - center_lat) * 111.0) ** 2 + ((lon - center_lon) * 109.0) ** 2)
    valid = mask.astype(bool) & (~shadow.astype(bool)) & (lat != 0) & (lon != 0)

    radius = search_radius_km
    good = np.zeros_like(valid)
    for _ in range(max_widenings):
        good = valid & (avg_coh >= reliability_threshold) & (dist_km <= radius)
        if good.sum() > 0:
            break
        radius *= 2
    if good.sum() == 0:
        # last resort: drop the coherence threshold to the best available
        # within any valid pixel, rather than returning nothing -- still
        # surfaced to the human at Gate B with its real (low) coherence
        # value, never silently hidden.
        good = valid
        if good.sum() == 0:
            return []

    elev_diff_abs = np.abs(hgt - center_elev)
    score = np.where(good, dist_km + elev_diff_abs / 50.0, np.inf)

    flat_idx = np.argsort(score, axis=None)
    candidates = []
    for idx in flat_idx:
        if len(candidates) >= top_n:
            break
        row, col = np.unravel_index(idx, score.shape)
        if not good[row, col]:
            continue
        candidates.append({
            "lat": float(lat[row, col]),
            "lon": float(lon[row, col]),
            "row": int(row),
            "col": int(col),
            "coherence": float(avg_coh[row, col]),
            "elevation_m": float(hgt[row, col]),
            "elevation_diff_m": float(hgt[row, col] - center_elev),
            "distance_km": float(dist_km[row, col]),
        })
    return candidates


def run(config: RunConfig, paths: RunPaths) -> list[dict]:
    def _do():
        render_cfg(config, paths)
        _load_data_and_modify_network(paths)
        _compute_coherence_mask_products(paths)

    run_checkpointed(config.run_dir, "mintpy_prep", _do)

    lon_min, lat_min, lon_max, lat_max = config.aoi.bbox
    aoi_center = ((lat_min + lat_max) / 2, (lon_min + lon_max) / 2)

    candidates = pick_reference_candidates(
        paths,
        aoi_center_lalo=aoi_center,
        reliability_threshold=config.mintpy.reliability_threshold,
        search_radius_km=config.mintpy.reference_search_radius_km,
    )
    paths.review.mkdir(parents=True, exist_ok=True)
    with open(paths.review / "candidate_reference_points.json", "w") as f:
        json.dump(candidates, f, indent=2)
    return candidates
