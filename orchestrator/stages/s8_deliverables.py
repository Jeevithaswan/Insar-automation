"""Stage 8: final deliverables. Every figure/export here crops to the
config's AOI bbox and computes its own color scale from the actual data of
this run -- nothing assumes a landslide, a "crown" point, or any other
region-specific concept. The AOI-center time series stands in generically
for what the Pettimudi project called the "crown time series" -- there is
no equivalent point-of-interest for an arbitrary AOI, so the center of the
bbox is used instead, purely as a representative point, not implying any
particular significance."""
from __future__ import annotations

import json
import zipfile

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
from PIL import Image

from orchestrator.checkpoint import run_checkpointed
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


def _load_geo(path, dsets):
    with h5py.File(path, "r") as f:
        atr = dict(f.attrs)
        data = {d: f[d][:] for d in dsets}
    return data, atr


def _latlon_grid(atr, shape):
    y0, x0 = float(atr["Y_FIRST"]), float(atr["X_FIRST"])
    dy, dx = float(atr["Y_STEP"]), float(atr["X_STEP"])
    return y0 + dy * np.arange(shape[0]), x0 + dx * np.arange(shape[1])


def _crop_indices(lats, lons, s, n, w, e):
    ri = np.where((lats >= s) & (lats <= n))[0]
    ci = np.where((lons >= w) & (lons <= e))[0]
    if len(ri) == 0 or len(ci) == 0:
        raise ValueError("AOI bbox does not overlap the geocoded product extent")
    return ri.min(), ri.max() + 1, ci.min(), ci.max() + 1


def _generate(config: RunConfig, paths: RunPaths) -> dict:
    geo_dir = paths.mintpy / "geo"
    vel_data, atr = _load_geo(geo_dir / "geo_velocity.h5", ["velocity"])
    tcoh_data, _ = _load_geo(geo_dir / "geo_temporalCoherence.h5", ["temporalCoherence"])
    ts_files = list(geo_dir.glob("geo_timeseries*.h5"))
    if not ts_files:
        raise FileNotFoundError(f"No geo_timeseries*.h5 found in {geo_dir}")
    ts_data, _ = _load_geo(ts_files[0], ["timeseries", "date"])

    lats, lons = _latlon_grid(atr, vel_data["velocity"].shape)
    lon_min, lat_min, lon_max, lat_max = config.aoi.bbox
    r0, r1, c0, c1 = _crop_indices(lats, lons, lat_min, lat_max, lon_min, lon_max)

    vel_crop = vel_data["velocity"][r0:r1, c0:c1] * 1000  # mm/yr
    tcoh_crop = tcoh_data["temporalCoherence"][r0:r1, c0:c1]
    ts_crop = ts_data["timeseries"][:, r0:r1, c0:c1] * 1000
    lats_crop, lons_crop = lats[r0:r1], lons[c0:c1]
    dates = [d.decode() if isinstance(d, bytes) else d for d in ts_data["date"]]
    dates_fmt = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]

    reliable_mask = tcoh_crop > config.mintpy.reliability_threshold
    extent = [lons_crop.min(), lons_crop.max(), lats_crop.min(), lats_crop.max()]

    paths.deliverables.mkdir(parents=True, exist_ok=True)

    # ---- Velocity map ----
    vel_masked = np.where(reliable_mask, vel_crop, np.nan)
    vmax_v = float(np.nanpercentile(np.abs(vel_masked[reliable_mask]), 98)) if reliable_mask.any() else 1.0
    fig, ax = plt.subplots(figsize=(9, 8))
    im = ax.imshow(vel_masked, extent=extent, origin="upper", cmap="jet_r", vmin=-vmax_v, vmax=vmax_v, aspect="auto")
    plt.colorbar(im, ax=ax, shrink=0.8, label="LOS velocity (mm/yr)")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.set_title(f"LOS Deformation Velocity -- {config.run_id}")
    plt.tight_layout()
    plt.savefig(paths.deliverables / "velocity_map.png", dpi=200)
    plt.close()

    # ---- Cumulative displacement time series, multi-panel ----
    n_dates = len(dates)
    ncols = min(3, n_dates)
    nrows = int(np.ceil(n_dates / ncols))
    ts_vmax = float(np.nanpercentile(np.abs(np.where(reliable_mask[None], ts_crop, np.nan)), 98)) if reliable_mask.any() else 1.0
    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4.5 * nrows), squeeze=False)
    for i, d in enumerate(dates_fmt):
        r, c = divmod(i, ncols)
        ax = axes[r][c]
        frame = np.where(reliable_mask, ts_crop[i], np.nan)
        im = ax.imshow(frame, extent=extent, origin="upper", cmap="jet_r", vmin=-ts_vmax, vmax=ts_vmax, aspect="auto")
        ax.set_title(d); ax.set_xticks([]); ax.set_yticks([])
    for i in range(n_dates, nrows * ncols):
        r, c = divmod(i, ncols)
        axes[r][c].axis("off")
    fig.suptitle(f"Cumulative LOS Displacement by Date -- {config.run_id}")
    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    fig.colorbar(im, cax=cbar_ax, label="Cumulative displacement (mm)")
    plt.tight_layout(rect=[0, 0, 0.9, 0.95])
    plt.savefig(paths.deliverables / "timeseries_by_date.png", dpi=200)
    plt.close()

    # ---- AOI-center time series (generic stand-in for a "point of interest") ----
    center_row = int(np.argmin(np.abs(lats_crop - (lat_min + lat_max) / 2)))
    center_col = int(np.argmin(np.abs(lons_crop - (lon_min + lon_max) / 2)))
    r_lo, r_hi = max(0, center_row - 3), center_row + 3
    c_lo, c_hi = max(0, center_col - 3), center_col + 3
    center_ts = np.nanmean(ts_crop[:, r_lo:r_hi, c_lo:c_hi], axis=(1, 2))
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(dates_fmt, center_ts, "o-", color="darkred")
    ax.set_xlabel("Date"); ax.set_ylabel("Cumulative LOS displacement (mm)")
    ax.set_title(f"Displacement Time Series at AOI Center -- {config.run_id}")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(paths.deliverables / "aoi_center_timeseries.png", dpi=200)
    plt.close()

    # ---- CSV export ----
    import csv
    csv_path = paths.deliverables / "whole_aoi_statistics.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Mean_mm", "Median_mm", "StdDev_mm", "Min_mm", "Max_mm", "N_Reliable_Pixels"])
        for i, d in enumerate(dates_fmt):
            frame = np.where(reliable_mask, ts_crop[i], np.nan)
            writer.writerow([
                d, round(float(np.nanmean(frame)), 2), round(float(np.nanmedian(frame)), 2),
                round(float(np.nanstd(frame)), 2), round(float(np.nanmin(frame)), 2),
                round(float(np.nanmax(frame)), 2), int(np.sum(~np.isnan(frame))),
            ])

    # ---- KMZ velocity overlay ----
    def _array_to_rgba_png(data, path, vmin, vmax):
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
        cmap = matplotlib.colormaps["jet_r"]
        rgba = cmap(norm(data))
        rgba[..., 3] = np.where(np.isnan(data), 0.0, 1.0)
        Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA").save(path)

    tmp_png = paths.deliverables / "_velocity_overlay.png"
    _array_to_rgba_png(vel_masked, tmp_png, -vmax_v, vmax_v)
    north, south = float(lats_crop.max()), float(lats_crop.min())
    east, west = float(lons_crop.max()), float(lons_crop.min())
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
<name>{config.run_id} -- LOS Velocity</name>
<GroundOverlay>
<name>LOS velocity (mm/yr)</name>
<Icon><href>velocity.png</href></Icon>
<LatLonBox><north>{north}</north><south>{south}</south><east>{east}</east><west>{west}</west></LatLonBox>
</GroundOverlay>
</Document>
</kml>"""
    kmz_path = paths.deliverables / "velocity_overlay.kmz"
    with zipfile.ZipFile(kmz_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        z.write(tmp_png, "velocity.png")
    tmp_png.unlink()

    summary = {
        "reliable_pixels": int(reliable_mask.sum()),
        "total_pixels": int(reliable_mask.size),
        "reliable_pct": round(100 * reliable_mask.sum() / reliable_mask.size, 2),
        "velocity_min_mmyr": float(np.nanmin(vel_masked)) if reliable_mask.any() else None,
        "velocity_max_mmyr": float(np.nanmax(vel_masked)) if reliable_mask.any() else None,
        "dates": dates_fmt,
        "files": ["velocity_map.png", "timeseries_by_date.png", "aoi_center_timeseries.png",
                  "whole_aoi_statistics.csv", "velocity_overlay.kmz"],
    }
    with open(paths.deliverables / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def run(config: RunConfig, paths: RunPaths) -> dict:
    result = {}

    def _do():
        result.update(_generate(config, paths))

    run_checkpointed(config.run_dir, "deliverables", _do)
    if not result and (paths.deliverables / "summary.json").exists():
        with open(paths.deliverables / "summary.json") as f:
            result = json.load(f)
    return result
