"""Stage 1: search and download Sentinel-1 SLC scenes for the configured
AOI/date range/track. Uses asf_search, authenticated via ~/.netrc -- no
interactive login prompt, which is what makes this runnable unattended for
any AOI (the old blocker in this project's manual scripts was exactly the
interactive Earthdata login prompt)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import asf_search as asf

from orchestrator.checkpoint import run_checkpointed
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


def _bbox_to_wkt(bbox: list[float]) -> str:
    lon_min, lat_min, lon_max, lat_max = bbox
    return (
        f"POLYGON(({lon_min} {lat_min}, {lon_max} {lat_min}, "
        f"{lon_max} {lat_max}, {lon_min} {lat_max}, {lon_min} {lat_min}))"
    )


def _geo_search_raw(
    bbox: list[float],
    start: str,
    end: str,
    relative_orbit: Optional[int] = None,
    flight_direction: Optional[str] = None,
):
    """Returns the real asf_search ASFSearchResults (list of ASFProduct),
    not a flattened dict -- callers that need to download must use this,
    not search_scenes(), so the actual product objects (with their real
    download URLs/session handling) are preserved rather than reconstructed
    from a plain dict later."""
    kwargs = dict(
        platform=asf.PLATFORM.SENTINEL1,
        intersectsWith=_bbox_to_wkt(bbox),
        start=start,
        end=end,
        processingLevel=asf.PRODUCT_TYPE.SLC,
        beamMode=asf.BEAMMODE.IW,
    )
    if relative_orbit is not None:
        kwargs["relativeOrbit"] = relative_orbit
    if flight_direction is not None:
        kwargs["flightDirection"] = flight_direction
    return asf.geo_search(**kwargs)


def _product_to_dict(r) -> dict:
    props = r.properties
    return {
        "scene_name": props.get("sceneName"),
        "start_time": props.get("startTime"),
        "relative_orbit": props.get("pathNumber"),
        "flight_direction": props.get("flightDirection"),
        "polarization": props.get("polarization"),
        "file_id": props.get("fileID"),
        "url": props.get("url"),
        "size_mb": props.get("bytes", 0) / (1024 * 1024) if props.get("bytes") else None,
    }


def search_scenes(
    bbox: list[float],
    start: str,
    end: str,
    relative_orbit: Optional[int] = None,
    flight_direction: Optional[str] = None,
    polarization: str = "VV",
) -> list[dict]:
    """Fast, download-free search -- used both by stage 1 internally and by
    the dashboard's 'Search tracks' button on the New Run page, so track
    availability is shown before committing to anything for a given AOI.
    Returns plain dicts (display-only, not for downloading -- see
    download_scenes, which re-queries and keeps the real product objects)."""
    results = _geo_search_raw(bbox, start, end, relative_orbit, flight_direction)
    return [_product_to_dict(r) for r in results]


def summarize_tracks(scenes: list[dict]) -> list[dict]:
    """Groups search results by (relative_orbit, flight_direction) so the
    dashboard can show 'this AOI has 2 descending tracks and 1 ascending
    track available, with N/M/K scenes each' rather than a flat scene list --
    this is the informed-choice step for track selection (manual-step #1)."""
    tracks: dict[tuple, list[dict]] = {}
    for s in scenes:
        key = (s["relative_orbit"], s["flight_direction"])
        tracks.setdefault(key, []).append(s)
    summary = []
    for (orbit, direction), group in sorted(tracks.items(), key=lambda kv: -len(kv[1])):
        dates = sorted(s["start_time"] for s in group if s["start_time"])
        summary.append({
            "relative_orbit": orbit,
            "flight_direction": direction,
            "scene_count": len(group),
            "date_range": (dates[0], dates[-1]) if dates else (None, None),
        })
    return summary


def download_scenes(config: RunConfig, paths: RunPaths, progress_fn=None) -> list[str]:
    if config.sentinel1.relative_orbit is None or config.sentinel1.flight_direction is None:
        raise ValueError(
            "sentinel1.relative_orbit and sentinel1.flight_direction must be set "
            "(via the 'Search tracks' step) before downloading -- track selection "
            "is never silently defaulted."
        )

    results = _geo_search_raw(
        bbox=config.aoi.bbox,
        start=config.date_range.start,
        end=config.date_range.end,
        relative_orbit=config.sentinel1.relative_orbit,
        flight_direction=config.sentinel1.flight_direction,
    )
    if not results:
        raise RuntimeError(
            f"No scenes found for orbit={config.sentinel1.relative_orbit} "
            f"direction={config.sentinel1.flight_direction} in the given AOI/date range."
        )
    # filter to the requested polarization -- asf_search's polarization param
    # matches on exact string (e.g. "VV") while ASF often reports "VV+VH" for
    # dual-pol products, so filter client-side on substring instead
    results = [r for r in results if config.sentinel1.polarization in (r.properties.get("polarization") or "")]
    if not results:
        raise RuntimeError(f"No scenes match polarization={config.sentinel1.polarization} after filtering.")

    paths.slc.mkdir(parents=True, exist_ok=True)
    session = asf.ASFSession()  # picks up ~/.netrc automatically for the Earthdata redirect

    total = len(results)
    downloaded = []
    for i, product in enumerate(results, start=1):
        if progress_fn:
            progress_fn(i - 1, total, f"downloading scene {i} of {total}")
        file_id = product.properties.get("fileID")
        # NOTE: fileID includes a "-SLC" product-type suffix (e.g.
        # "..._32FF-SLC") that is NOT part of the actual downloaded filename
        # on disk -- asf_search names the file after sceneName instead.
        # Confirmed empirically: a real download landed at "{sceneName}.zip",
        # not "{fileID}.zip". Marker name can still use file_id (just needs
        # to be unique/stable), but the expected path must use sceneName.
        scene_name = product.properties.get("sceneName")
        marker_name = f"s1_download_{file_id}"
        expected_path = paths.slc / f"{scene_name}.zip"

        def _do_download(p=product, expected=expected_path):
            # skip re-downloading if a previous attempt already succeeded on
            # disk but crashed before the checkpoint marker got written
            # (e.g. the sceneName/fileID filename mismatch bug this comment
            # is next to fixing) -- avoids wasting multi-GB bandwidth on retry
            if expected.exists() and expected.stat().st_size > 0:
                return
            p.download(path=str(paths.slc), session=session)
            if not expected.exists():
                raise RuntimeError(f"Download reported success but file not found: {expected}")

        run_checkpointed(config.run_dir, marker_name, _do_download)
        downloaded.append(str(expected_path))

    return downloaded
