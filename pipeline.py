"""
InSAR SBAS automation pipeline -- single file, run one stage at a time from
a plain terminal.

Same tools used for the manual Pettimudi run (asf_search, dem_stitcher, eof,
ISCE2's stackSentinel.py/topsStack, MintPy's smallbaselineApp.py), just called
from code instead of by hand, so it works for any AOI/date range/track.

HOW TO RUN (VS Code terminal or any terminal):
    conda activate insar
    python pipeline.py tracks               find which Sentinel-1 tracks cover a NEW AOI (run this first for a new region)
    python pipeline.py 0                    env precheck
    python pipeline.py 1                    search + download SLCs
    python pipeline.py 2                    DEM
    python pipeline.py 3                    orbit files
    python pipeline.py 4                    ISCE2 stackSentinel.py setup (generates run_01..run_NN)
    python pipeline.py 5                    ISCE2 run_01..run_NN execution (the heavy step)
    python pipeline.py 6                    MintPy prep + reference point candidates
    python pipeline.py 7 <lat> <lon> [config]  MintPy inversion using a chosen reference point (pick from stage 6's output)
    python pipeline.py 8                    deliverables (plots/csv/kmz)

By default reads configs/runs/dummy_test_aoi.yaml. Pass a different config
as an extra argument, e.g.: python pipeline.py 1 configs/runs/pettimudi_repro.yaml
(for stage 7 the config comes AFTER lat/lon: python pipeline.py 7 <lat> <lon> configs/runs/pettimudi_repro.yaml
 -- if omitted, defaults to dummy_test_aoi.yaml same as every other stage; make sure this matches
 whatever config you used for stages 0-6 on the same run, or stage 7 will operate on the wrong run's data)

Every stage is checkpointed on disk (runs/<run_id>/.done_markers/) -- if a
stage is interrupted (Ctrl+C, crash, closed terminal) and you re-run the same
command, work already finished is skipped rather than redone.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent

# dem_stitcher/rasterio pull Copernicus DEM tiles from a *public* S3 bucket,
# but boto3's default credential chain still probes for AWS credentials
# first (including a 1s-timeout probe of the EC2 metadata service, which
# doesn't exist on this machine) -- harmless, but at DEBUG level it dumps a
# full retry traceback per attempt, which reads as a wall of errors even
# though the actual download succeeds right after. Quieting these chatty
# libraries to WARNING keeps stage output readable without hiding real errors.
import logging
for _noisy_logger in ("botocore", "boto3", "s3transfer", "urllib3", "rasterio", "asf_search"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

# ISCE2's own CLI scripts (stackSentinel.py, gdal2isce_xml.py) are not on
# PATH by default in the "insar" conda env (confirmed by direct testing --
# the env's activate hook only sets ISCE_HOME/ISCE_STACK, it doesn't add
# their directories to PATH). Extending PATH here, once, means every stage
# below can call these tools by bare name via subprocess, same as
# smallbaselineApp.py/eof (already on PATH via their own package's
# console-script entry points).
#
# stackSentinel.py additionally does `from topsStack.Stack import ...` --
# it needs to import topsStack as a Python PACKAGE, which means its PARENT
# directory (share/isce2, one level up from share/isce2/topsStack) must be
# on PYTHONPATH, not just topsStack itself on PATH. Confirmed by direct
# testing: without this, stackSentinel.py is found and starts, then crashes
# immediately with "ModuleNotFoundError: No module named 'topsStack'".
def _extend_path_for_isce2():
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if not conda_prefix:
        return
    isce2_share = Path(conda_prefix) / "share" / "isce2"
    tops_stack = isce2_share / "topsStack"
    try:
        import isce
        applications_dir = Path(isce.__file__).resolve().parent / "applications"
    except ImportError:
        applications_dir = None

    path_extra = [str(p) for p in (tops_stack, applications_dir) if p and p.is_dir()]
    current_path = os.environ.get("PATH", "")
    for p in path_extra:
        if p not in current_path.split(os.pathsep):
            current_path = p + os.pathsep + current_path
    os.environ["PATH"] = current_path

    if isce2_share.is_dir():
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        if str(isce2_share) not in current_pythonpath.split(os.pathsep):
            os.environ["PYTHONPATH"] = str(isce2_share) + os.pathsep + current_pythonpath


_extend_path_for_isce2()


# ============================================================
# Setup -- config + paths + checkpoint helpers, shared plumbing not a stage
# ============================================================
DEFAULT_CONFIG = REPO_ROOT / "configs" / "runs" / "dummy_test_aoi.yaml"


def load_config(path: str | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(os.path.expanduser(str(path))) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("output", {}).setdefault("base_dir", "~/insar-automation/runs")
    return cfg


class Paths:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.slc = run_dir / "slc"
        self.orbits = run_dir / "orbits"
        self.aux = run_dir / "aux"
        self.dem = run_dir / "dem"
        self.dem_tif = self.dem / "dem.tif"
        self.dem_isce = self.dem / "dem.dem"
        self.stack_proc = run_dir / "stack_proc"
        self.mintpy = run_dir / "mintpy"
        self.mintpy_cfg = self.mintpy / "smallbaselineApp.cfg"
        self.deliverables = run_dir / "deliverables"
        self.review = run_dir / "review"
        self.logs = run_dir / "logs"

    def ensure_all(self):
        for p in [self.slc, self.orbits, self.aux, self.dem, self.stack_proc,
                  self.mintpy, self.deliverables, self.review, self.logs]:
            p.mkdir(parents=True, exist_ok=True)


def _marker_dir(run_dir: Path) -> Path:
    d = run_dir / ".done_markers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_done(run_dir: Path, name: str) -> bool:
    return (_marker_dir(run_dir) / f"{name}.done").exists()


def mark_done(run_dir: Path, name: str) -> None:
    (_marker_dir(run_dir) / f"{name}.done").touch()


def run_checkpointed(run_dir: Path, name: str, fn) -> None:
    if is_done(run_dir, name):
        print(f"  [skip] {name} already done")
        return
    fn()
    mark_done(run_dir, name)


def setup(config_path: str | None = None) -> tuple[dict, Paths]:
    cfg = load_config(config_path)
    run_dir = Path(os.path.expanduser(cfg["output"]["base_dir"])) / cfg["run_id"]
    paths = Paths(run_dir)
    paths.ensure_all()
    with open(run_dir / "config_used.yaml", "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"[run_id={cfg['run_id']}] run_dir={run_dir}")
    return cfg, paths


# ============================================================
# STAGE 0 -- environment precheck
# ============================================================
REQUIRED_IMPORTS = ["isce", "mintpy", "asf_search", "dem_stitcher", "h5py", "rasterio", "yaml"]
REQUIRED_CLI_TOOLS = ["stackSentinel.py", "smallbaselineApp.py", "eof", "gdal2isce_xml.py"]

# Known installed-library bugs that need patching in this conda env, checked
# and re-applied every run since they live in installed site-packages and can
# silently revert if the env is ever rebuilt:
#
# 1-2. ISCE2 + newer NumPy: two files format an array (not a plain scalar)
#    with %f in a debug print, which newer NumPy raises on -- crashes deep
#    inside topsStack processing, unrelated to any specific AOI.
#
# 3. MintPy + single-pair networks: np.loadtxt returns a 1-D array (not 2-D)
#    when the text file it's reading has exactly one row -- true for any AOI
#    whose network has only 1 interferogram pair (e.g. a 2-scene test stack).
#    MintPy's own read_text_file always indexes it as 2-D, crashing
#    modify_network. Confirmed by direct testing on this project's own
#    2-scene/1-pair dummy test AOI.
#
# 4. MintPy + phase-closure unwrap-error correction needs at least 3
#    interferograms forming a closed triangle in time -- mathematically
#    impossible with only 1 pair. get_design_matrix4triplet() already
#    detects this and returns None with a "No triangles found" warning, and
#    the caller already has a graceful "skip phase closure correction" path
#    for an empty region list -- but get_common_region_int_ambiguity() calls
#    .astype(float) on that None before the caller ever gets a chance to
#    check it, crashing instead of taking the skip path that already exists.
#
# 5. MintPy + single-pair networks (again): ifgramStack.get_reference_phase()
#    reads a 1x1 pixel box across all interferograms and returns it as a 1-D
#    array of length num_ifgram -- except when num_ifgram is 1, where the
#    underlying read collapses to a 0-D scalar instead. Downstream code in
#    ifgram_inversion.py indexes it as ref_phase[i], which fails on a 0-D
#    array. Same root pattern as patch 3 (numpy silently dropping a
#    length-1 axis), different call site.
#
# 6. MintPy velocity estimation + exactly-determined time function: scipy's
#    lstsq only returns a residual (e2) when the system is OVERdetermined
#    (num_date > num_param) -- with exactly 2 dates fit to a 2-parameter
#    (intercept + velocity) line, it's exactly determined, so scipy
#    correctly returns an empty e2 by its own documented convention (exact
#    fit, zero residual, nothing to report). MintPy treats "empty e2" as a
#    proxy for "rank-deficient G" and raises unconditionally, which is a
#    false positive here -- G is actually full rank for a plain 2-date
#    linear fit. Patch checks the real matrix rank instead of using empty-e2
#    as the signal, so genuinely rank-deficient G (the case this check was
#    actually meant to catch, e.g. a broken exp/log time function) still
#    raises, while a benign exact fit no longer does.
_LIBRARY_PATCHES = [
    {
        "name": "Poly1D.py",
        "relpath": "isce/components/isceobj/Util/Poly1D.py",
        "old": "print('Chi squared: %f'%(np.sqrt(res/(1.0*Npts))))",
        "new": "print('Chi squared: %f'%(float(np.asarray(np.sqrt(res/(1.0*Npts))).flat[0])))",
    },
    {
        "name": "Poly2D.py",
        "relpath": "isce/components/isceobj/Util/Poly2D.py",
        "old": "print('Chi squared: %f'%(np.sqrt(res/(1.0*len(z)))))",
        "new": "print('Chi squared: %f'%(float(np.asarray(np.sqrt(res/(1.0*len(z)))).flat[0])))",
    },
    {
        "name": "mintpy/utils1.py:read_text_file",
        "relpath": "mintpy/utils/utils1.py",
        "old": "        txtContent = np.loadtxt(fname, dtype=bytes).astype(str)\n        meanList = [float(i) for i in txtContent[:, 1]]",
        "new": "        txtContent = np.loadtxt(fname, dtype=bytes).astype(str)\n        if txtContent.ndim == 1:\n            txtContent = txtContent.reshape(1, -1)\n        meanList = [float(i) for i in txtContent[:, 1]]",
    },
    {
        "name": "mintpy/unwrap_error_phase_closure.py:get_common_region_int_ambiguity",
        "relpath": "mintpy/unwrap_error_phase_closure.py",
        "old": "    C = matrix(ifgramStack.get_design_matrix4triplet(date12_list).astype(float))",
        "new": "    design_matrix = ifgramStack.get_design_matrix4triplet(date12_list)\n    if design_matrix is None:\n        print('Not enough interferograms to form any triplet -- skipping phase closure correction.')\n        return []\n    C = matrix(design_matrix.astype(float))",
    },
    {
        "name": "mintpy/objects/stack.py:get_reference_phase",
        "relpath": "mintpy/objects/stack.py",
        "old": "                                  print_msg=False)\n        return ref_phase",
        "new": "                                  print_msg=False)\n            ref_phase = np.atleast_1d(ref_phase)\n        return ref_phase",
    },
    {
        "name": "mintpy/utils/time_func.py:estimate_time_func",
        "relpath": "mintpy/utils/time_func.py",
        "old": (
            "    e2 = np.array(e2)\n"
            "    if e2.size == 0:\n"
            "        print('\\nWarning: empty e2 residues array due to a redundant or rank-deficient G matrix. This can cause sigularities.')\n"
            "        print('Please check: https://docs.scipy.org/doc/scipy/reference/generated/scipy.linalg.lstsq.html#scipy.linalg.lstsq')\n"
            "        print('The issue may be due to:')\n"
            "        print('\\t1) very small char time(s) or tau(s) of the exp/log function(s)')\n"
            "        print('\\t2) the onset time(s) of exp/log are far earlier than the minimum date of the time series.')\n"
            "        print('Try a different char time, onset time.')\n"
            "        print('Your G matrix of the temporal model: \\n', G)\n"
            "        raise ValueError('G matrix is redundant/rank-deficient!')"
        ),
        "new": (
            "    e2 = np.array(e2)\n"
            "    if e2.size == 0 and np.linalg.matrix_rank(G) < G.shape[1]:\n"
            "        print('\\nWarning: empty e2 residues array due to a redundant or rank-deficient G matrix. This can cause sigularities.')\n"
            "        print('Please check: https://docs.scipy.org/doc/scipy/reference/generated/scipy.linalg.lstsq.html#scipy.linalg.lstsq')\n"
            "        print('The issue may be due to:')\n"
            "        print('\\t1) very small char time(s) or tau(s) of the exp/log function(s)')\n"
            "        print('\\t2) the onset time(s) of exp/log are far earlier than the minimum date of the time series.')\n"
            "        print('Try a different char time, onset time.')\n"
            "        print('Your G matrix of the temporal model: \\n', G)\n"
            "        raise ValueError('G matrix is redundant/rank-deficient!')\n"
            "    elif e2.size == 0:\n"
            "        # exactly-determined system (num_date == num_param): a valid exact\n"
            "        # fit with zero residual, not an error -- see patch 6 above.\n"
            "        e2 = np.zeros(dis_ts.shape[1], dtype=G.dtype)"
        ),
    },
]


def _check_and_apply_library_patches() -> list[dict]:
    import isce
    sp = Path(isce.__file__).resolve().parent.parent
    results = []
    for p in _LIBRARY_PATCHES:
        path = sp / p["relpath"]
        if not path.exists():
            results.append({"check": f"patch:{p['name']}", "ok": False, "detail": f"not found: {path}"})
            continue
        content = path.read_text()
        if p["new"] in content:
            results.append({"check": f"patch:{p['name']}", "ok": True, "detail": "already patched"})
        elif p["old"] in content:
            path.write_text(content.replace(p["old"], p["new"]))
            results.append({"check": f"patch:{p['name']}", "ok": True, "detail": "patched now"})
        else:
            results.append({"check": f"patch:{p['name']}", "ok": False, "detail": "unexpected file content"})
    return results


def stage0_env_precheck(cfg: dict, paths: Paths) -> None:
    import importlib.util

    checks = []
    checks.append({
        "check": "conda_env", "ok": os.environ.get("CONDA_DEFAULT_ENV") == "insar",
        "detail": f"CONDA_DEFAULT_ENV={os.environ.get('CONDA_DEFAULT_ENV')}",
    })
    for mod in REQUIRED_IMPORTS:
        ok = importlib.util.find_spec(mod) is not None
        checks.append({"check": f"import:{mod}", "ok": ok, "detail": "" if ok else "not importable"})
    for tool in REQUIRED_CLI_TOOLS:
        path = shutil.which(tool)
        checks.append({"check": f"cli:{tool}", "ok": path is not None, "detail": path or "not on PATH"})
    checks.extend(_check_and_apply_library_patches())

    total, used, free = shutil.disk_usage(os.path.expanduser("~"))
    free_gb = free / (1024 ** 3)
    checks.append({"check": "disk_space", "ok": free_gb >= 20.0, "detail": f"{free_gb:.1f} GB free (need >= 20 GB)"})

    netrc_ok = os.path.exists(os.path.expanduser("~/.netrc"))
    checks.append({"check": "earthdata_netrc", "ok": netrc_ok,
                    "detail": "present" if netrc_ok else "~/.netrc MISSING -- needed for ASF download"})

    for c in checks:
        print(f"  [{'OK' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")

    hard_fail = [c for c in checks if not c["ok"]]
    if hard_fail:
        raise RuntimeError(f"Environment precheck failed: {len(hard_fail)} check(s) did not pass (see above)")
    print("Environment precheck PASSED.")


def _bbox_to_wkt(bbox: list[float]) -> str:
    lon_min, lat_min, lon_max, lat_max = bbox
    return (f"POLYGON(({lon_min} {lat_min}, {lon_max} {lat_min}, "
            f"{lon_max} {lat_max}, {lon_min} {lat_max}, {lon_min} {lat_min}))")


# ============================================================
# TRACK SEARCH -- find which Sentinel-1 tracks actually cover an AOI/date
# range, before committing to one in the config. Run standalone:
#   python pipeline.py tracks [config_yaml]
# Only needs aoi.bbox + date_range set (sentinel1.relative_orbit/
# flight_direction not required yet -- that's what this finds). Prints
# every available track with its scene count so you can pick one and fill
# it into the config yourself -- same "pipeline surfaces the options, you
# make the call" pattern as reference-point selection (stage 6/7), since
# ascending vs. descending changes which deformation direction you're
# sensitive to, not something to silently auto-pick.
# ============================================================
def search_tracks(cfg: dict) -> list[dict]:
    import asf_search as asf

    wkt = _bbox_to_wkt(cfg["aoi"]["bbox"])
    results = asf.geo_search(
        platform=asf.PLATFORM.SENTINEL1,
        intersectsWith=wkt,
        start=cfg["date_range"]["start"],
        end=cfg["date_range"]["end"],
        processingLevel=asf.PRODUCT_TYPE.SLC,
        beamMode=asf.BEAMMODE.IW,
    )
    if not results:
        print("No Sentinel-1 IW SLC scenes found at all for this AOI/date range.")
        return []

    tracks: dict[tuple, list] = {}
    for r in results:
        key = (r.properties.get("pathNumber"), r.properties.get("flightDirection"))
        tracks.setdefault(key, []).append(r)

    summary = []
    print(f"Found {len(results)} scene(s) across {len(tracks)} track(s):\n")
    for (orbit, direction), scenes in sorted(tracks.items(), key=lambda kv: -len(kv[1])):
        scenes = sorted(scenes, key=lambda s: s.properties.get("startTime") or "")
        dates = [s.properties.get("startTime") for s in scenes if s.properties.get("startTime")]
        summary.append({
            "relative_orbit": orbit,
            "flight_direction": direction,
            "scene_count": len(scenes),
            "first_date": dates[0][:10] if dates else None,
            "last_date": dates[-1][:10] if dates else None,
        })

        print(f"=== relative_orbit={orbit} flight_direction={direction} ({len(scenes)} scene(s)) ===")
        for s in scenes:
            p = s.properties
            size_mb = (p.get("bytes") or 0) / (1024 * 1024)
            print(f"    {p.get('startTime', '')[:10]}  {p.get('sceneName')}  "
                  f"pol={p.get('polarization')}  {size_mb:.0f} MB")
        print()

    print("Pick a track above, then set sentinel1.relative_orbit / flight_direction in your config YAML to match it.")
    print("Stage 1 downloads every scene listed under whichever track you pick -- for SBAS, more dates on the")
    print("same track generally means a better time series, so there's usually no reason to drop individual dates.")
    print("If you do need to exclude a specific date, note it now and we can add that -- not built in yet.")
    return summary


# ============================================================
# STAGE 1 -- search + download Sentinel-1 SLC scenes (ASF)
# ============================================================
def stage1_search_download(cfg: dict, paths: Paths) -> list[str]:
    import asf_search as asf

    s1 = cfg["sentinel1"]
    if s1.get("relative_orbit") is None or s1.get("flight_direction") is None:
        raise ValueError(
            "sentinel1.relative_orbit and sentinel1.flight_direction must be set in the config "
            "-- run `python pipeline.py tracks` first to find which tracks cover this AOI."
        )

    wkt = _bbox_to_wkt(cfg["aoi"]["bbox"])
    results = asf.geo_search(
        platform=asf.PLATFORM.SENTINEL1,
        intersectsWith=wkt,
        start=cfg["date_range"]["start"],
        end=cfg["date_range"]["end"],
        processingLevel=asf.PRODUCT_TYPE.SLC,
        beamMode=asf.BEAMMODE.IW,
        relativeOrbit=s1["relative_orbit"],
        flightDirection=s1["flight_direction"],
    )
    if not results:
        raise RuntimeError(f"No scenes found for orbit={s1['relative_orbit']} direction={s1['flight_direction']}")

    # Same names/idea as stackSentinel.py's own -i/-x flags: a date_range
    # covering real acquisitions can still return MORE scenes than you
    # actually want to reproduce (e.g. a real revisit that exists in the
    # archive but wasn't part of a specific prior analysis) -- confirmed by
    # direct testing: searching Pettimudi's real date range surfaced a real
    # 2020-07-17 scene that isn't part of the 6-date ground truth. Without
    # an explicit whitelist, every scene ASF returns in range gets
    # downloaded and processed, silently growing the network beyond what's
    # being reproduced.
    include_dates = s1.get("include_dates")
    exclude_dates = s1.get("exclude_dates")
    if include_dates:
        results = [r for r in results if (r.properties.get("startTime") or "")[:10].replace("-", "") in include_dates]
    if exclude_dates:
        results = [r for r in results if (r.properties.get("startTime") or "")[:10].replace("-", "") not in exclude_dates]
    if not results:
        raise RuntimeError("No scenes left after applying include_dates/exclude_dates filtering")

    polarization = s1.get("polarization", "VV")
    results = [r for r in results if polarization in (r.properties.get("polarization") or "")]
    if not results:
        raise RuntimeError(f"No scenes match polarization={polarization} after filtering")

    paths.slc.mkdir(parents=True, exist_ok=True)
    session = asf.ASFSession()  # picks up ~/.netrc automatically

    downloaded = []
    for i, product in enumerate(results, start=1):
        # NOTE: fileID includes a "-SLC" suffix that is NOT part of the
        # actual downloaded filename on disk -- asf_search names the file
        # after sceneName, not fileID. Confirmed empirically.
        file_id = product.properties.get("fileID")
        scene_name = product.properties.get("sceneName")
        expected_path = paths.slc / f"{scene_name}.zip"
        marker_name = f"s1_download_{file_id}"

        def _do(p=product, expected=expected_path):
            if expected.exists() and expected.stat().st_size > 0:
                return
            p.download(path=str(paths.slc), session=session)
            if not expected.exists():
                raise RuntimeError(f"Download reported success but file not found: {expected}")

        print(f"  [{i}/{len(results)}] {scene_name}")
        run_checkpointed(paths.run_dir, marker_name, _do)
        downloaded.append(str(expected_path))

    print(f"Downloaded/verified {len(downloaded)} scene(s).")
    return downloaded


# ============================================================
# STAGE 2 -- DEM (Copernicus GLO-30, ellipsoidal height)
# ============================================================
def stage2_dem(cfg: dict, paths: Paths) -> None:
    import rasterio
    from dem_stitcher import stitch_dem

    def _do():
        lon_min, lat_min, lon_max, lat_max = cfg["aoi"]["bbox"]
        pad = cfg["dem"]["padding_deg"]
        bbox = [lon_min - pad, lat_min - pad, lon_max + pad, lat_max + pad]

        # deliberately ellipsoidal (WGS84) height, not geoid -- a datum
        # mismatch here is a known way to get wildly wrong displacement values
        X, profile = stitch_dem(
            bbox, dem_name=cfg["dem"]["source"],
            dst_ellipsoidal_height=True, dst_area_or_point="Point",
        )
        paths.dem.mkdir(parents=True, exist_ok=True)
        with rasterio.open(paths.dem_tif, "w", **profile) as dst:
            dst.write(X, 1)

        # Convert to ISCE2's native uncompressed raw-binary format (GDAL's
        # built-in "ISCE" driver) instead of leaving the DEM as a DEFLATE-
        # compressed, tiled GeoTIFF. ISCE2's topo step samples scattered
        # neighborhoods across the whole raster during interpolation, not
        # sequential blocks -- against a compressed/tiled source that forces
        # GDAL to repeatedly seek/decompress whole tiles, which is why this
        # matters: confirmed by comparing to the original manual Pettimudi
        # run, which fed topo an uncompressed ENVI .dem (via the same GDAL
        # ISCE driver) and finished 18 bursts in ~18 min, vs. this pipeline
        # feeding topo the compressed .tif directly and stalling for 4+
        # hours on a single burst of a far smaller AOI.
        subprocess.run(
            ["gdal_translate", "-of", "ISCE", str(paths.dem_tif), str(paths.dem_isce)],
            check=True, cwd=str(paths.dem),
        )
        # gdal_translate -of ISCE writes dem.dem + a dem.dem.xml whose
        # file_name is only the bare filename (no directory) -- ISCE2's
        # DataAccessor always appends ".vrt" to that name and opens it
        # relative to the CALLING PROCESS's cwd, not the DEM's own
        # directory. Since topo runs from a different cwd, this reliably
        # fails with "Cannot open the file dem.dem.vrt in read mode."
        # Re-running gdal2isce_xml.py on the raw binary regenerates the xml
        # with an ABSOLUTE file_name and also writes the companion .dem.vrt
        # that ISCE2 actually opens -- confirmed by direct testing, this is
        # the same two-file layout (.dem + .dem.vrt) the manual Pettimudi
        # run used.
        subprocess.run(
            ["gdal2isce_xml.py", "-i", str(paths.dem_isce)],
            check=True, cwd=str(paths.dem),
        )

    run_checkpointed(paths.run_dir, "dem_download", _do)
    print(f"DEM written to {paths.dem_tif} (ISCE-native copy at {paths.dem_isce})")


# ============================================================
# STAGE 3 -- precise orbit files
# ============================================================
def stage3_orbits(cfg: dict, paths: Paths) -> None:
    def _do():
        paths.orbits.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["eof", "-p", str(paths.slc), "--save-dir", str(paths.orbits),
             "--orbit-type", "precise", "--force-asf"],
            check=True,
        )

    run_checkpointed(paths.run_dir, "orbit_download", _do)
    print(f"Orbit files in {paths.orbits}:")
    for f in sorted(paths.orbits.glob("*")):
        print(f"  {f.name}")


# ============================================================
# STAGE 4 -- ISCE2 stackSentinel.py setup (generates run_01..run_NN)
# ============================================================
_APPROX_RANGE_PX_M = 2.3
_APPROX_AZIMUTH_PX_M = 14.0


def _estimate_network_cost(cfg: dict, num_scenes: int) -> dict:
    net = cfg["network"]
    c = net["connections"]
    approx_pairs = max(0, num_scenes * c - c * (c + 1) // 2)

    lon_min, lat_min, lon_max, lat_max = cfg["aoi"]["bbox"]
    lat_center = (lat_min + lat_max) / 2
    width_m = (lon_max - lon_min) * 111320 * math.cos(math.radians(lat_center))
    height_m = (lat_max - lat_min) * 110540
    range_px = width_m / (_APPROX_RANGE_PX_M * net["range_looks"])
    azimuth_px = height_m / (_APPROX_AZIMUTH_PX_M * net["azimuth_looks"])
    est_pixels = max(1, range_px) * max(1, azimuth_px)
    est_snaphu_mb = max(200, (est_pixels / 1_000_000) * 2000)

    return {
        "num_scenes": num_scenes,
        "approx_interferogram_pairs": approx_pairs,
        "approx_pixels_after_multilook": int(est_pixels),
        "approx_snaphu_mb_per_instance": round(est_snaphu_mb),
        "approx_snaphu_peak_mb": round(est_snaphu_mb * net["run16_parallelism"]),
    }


def stage4_stack_setup(cfg: dict, paths: Paths) -> dict:
    num_scenes = len(list(paths.slc.glob("*.zip")))
    cost = _estimate_network_cost(cfg, num_scenes)
    print("Network cost estimate:")
    for k, v in cost.items():
        print(f"  {k}: {v}")

    def _do():
        paths.aux.mkdir(parents=True, exist_ok=True)
        paths.stack_proc.mkdir(parents=True, exist_ok=True)
        paths.review.mkdir(parents=True, exist_ok=True)
        with open(paths.review / "network_design_summary.json", "w") as f:
            json.dump(cost, f, indent=2)

        lon_min, lat_min, lon_max, lat_max = cfg["aoi"]["bbox"]
        bbox_str = f"{lat_min} {lat_max} {lon_min} {lon_max}"  # ISCE2 wants S N W E
        net = cfg["network"]
        cmd = ["stackSentinel.py",
               "-s", str(paths.slc), "-o", str(paths.orbits), "-a", str(paths.aux), "-d", str(paths.dem_isce),
               "-b", bbox_str, "-W", "interferogram", "-C", "NESD",
               "-c", str(net["connections"]), "-z", str(net["azimuth_looks"]), "-r", str(net["range_looks"]),
               "-u", "snaphu", "--num_proc", str(net["num_proc"]), "--num_proc4topo", str(net["num_proc4topo"])]

        # Ionospheric correction is conditionally worth it -- ISCE2's own
        # docs recommend it only for low-latitude/ascending data AND only
        # where coherence is high enough for phase unwrapping to succeed
        # (it explicitly warns it "only works well in areas with high
        # coherence"). There's no universal on/off answer, so it's a
        # config toggle, not a default -- see stage 6, which prints the
        # AOI's actual measured coherence so this can be an informed,
        # per-study decision rather than a blind guess made before seeing
        # any real data.
        #
        # KNOWN INCOMPLETE (2026-08-05): this only wires up ISCE2's OWN
        # ionosphere estimation (confirmed generating the right run_17..24
        # steps by direct testing). It does NOT yet wire the result into
        # MintPy -- stage 6's CFG_TEMPLATE never sets
        # mintpy.ionosphericDelay.method or the ion load-file paths, so even
        # with this enabled, MintPy currently never actually APPLIES the
        # correction it computed. Do not rely on ionosphere.enabled=true
        # doing anything useful end-to-end until that's finished and tested.
        ion = cfg.get("ionosphere", {})
        if ion.get("enabled"):
            # ISCE2's own shipped default parameter file (every value
            # commented out = use the tool's own built-in defaults)
            isce_share = Path(shutil.which("stackSentinel.py")).resolve().parent
            template = isce_share / "ion_param.txt"
            ion_param_path = paths.aux / "ion_param.txt"
            ion_param_path.write_text(template.read_text())
            cmd += ["--param_ion", str(ion_param_path),
                    "--num_connections_ion", str(ion.get("num_connections_ion", 3))]

        subprocess.run(cmd, check=True, cwd=str(paths.stack_proc))

    run_checkpointed(paths.run_dir, "stack_setup", _do)
    run_files = sorted((paths.stack_proc / "run_files").glob("run_*"))
    print(f"Generated {len(run_files)} run file(s):")
    for f in run_files:
        print(f"  {f.name}")
    return cost


# ============================================================
# STAGE 5 -- execute ISCE2's run_01..run_NN (the heavy preprocessing step)
# ============================================================
def _natural_key(path: Path):
    m = re.match(r"run_(\d+)", path.name)
    return int(m.group(1)) if m else 0


def _discover_run_files(paths: Paths) -> list[Path]:
    run_files_dir = paths.stack_proc / "run_files"
    files = [p for p in run_files_dir.glob("run_*") if p.is_file() and not p.name.endswith(".job")]
    return sorted(files, key=_natural_key)


def _run_one_line(run_dir, marker_prefix, line_num, cmd, run_file_name, omp_threads=None):
    marker_name = f"{marker_prefix}_line{line_num}"
    if is_done(run_dir, marker_name):
        print(f"[{run_file_name}] line {line_num}: skipped (already done)")
        return line_num, True, "skipped"

    print(f"[{run_file_name}] line {line_num}: $ {cmd}")
    env = os.environ.copy()
    if omp_threads:
        env["OMP_NUM_THREADS"] = str(omp_threads)
    # ISCE2's compiled steps write to stdout via C stdio, which glibc
    # switches to block-buffered when stdout isn't a real terminal (true here,
    # since we're capturing through a pipe) -- without stdbuf, minutes of real
    # progress sit unflushed and the log looks frozen even while it's working.
    process = subprocess.Popen(
        f"stdbuf -oL -eL {cmd}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    tail_lines = []
    for out_line in process.stdout:
        out_line = out_line.rstrip()
        if out_line:
            print(f"[{run_file_name}] line {line_num} | {out_line}")
            tail_lines.append(out_line)
            tail_lines = tail_lines[-50:]
    returncode = process.wait()
    if returncode != 0:
        return line_num, False, "\n".join(tail_lines)
    mark_done(run_dir, marker_name)
    print(f"[{run_file_name}] line {line_num}: done")
    return line_num, True, "done"


def _execute_run_file(run_dir: Path, run_file: Path, parallelism: int, omp_threads=None) -> None:
    lines = [l.strip() for l in run_file.read_text().splitlines() if l.strip()]
    marker_prefix = f"stack_{run_file.name}"

    if parallelism <= 1:
        for i, cmd in enumerate(lines, start=1):
            line_num, ok, detail = _run_one_line(run_dir, marker_prefix, i, cmd, run_file.name, omp_threads)
            if not ok:
                raise RuntimeError(f"{run_file.name} line {line_num} failed:\n{detail}")
        return

    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(_run_one_line, run_dir, marker_prefix, i, cmd, run_file.name, omp_threads): i
            for i, cmd in enumerate(lines, start=1)
        }
        failures = []
        for future in as_completed(futures):
            line_num, ok, detail = future.result()
            if not ok:
                failures.append((line_num, detail))
        if failures:
            msgs = "\n".join(f"line {n}: {d}" for n, d in failures)
            raise RuntimeError(f"{run_file.name} had {len(failures)} failed line(s):\n{msgs}")


def stage5_stack_execute(cfg: dict, paths: Paths) -> None:
    run_files = _discover_run_files(paths)
    if not run_files:
        raise RuntimeError(f"No run_* files found in {paths.stack_proc / 'run_files'} -- did stage 4 run?")

    net = cfg["network"]
    for idx, run_file in enumerate(run_files, start=1):
        stage_marker = f"stack_execute_{run_file.name}_complete"
        if is_done(paths.run_dir, stage_marker):
            print(f"[{run_file.name}] already complete, skipping ({idx}/{len(run_files)})")
            continue

        # SNAPHU unwrapping is memory-heavy (~2GB/instance, observed to OOM at
        # 4-way parallelism previously) -- every other step parallelizes safely.
        is_unwrap = "unwrap" in run_file.name.lower()
        parallelism = net["run16_parallelism"] if is_unwrap else net["num_proc"]
        print(f"\n=== [{idx}/{len(run_files)}] {run_file.name} "
              f"(parallelism={parallelism}, topo_omp_threads={net.get('topo_omp_threads')}) ===")
        _execute_run_file(paths.run_dir, run_file, parallelism, net.get("topo_omp_threads"))
        mark_done(paths.run_dir, stage_marker)
        print(f"Finished {run_file.name}")

    print("stack_execute complete.")


# ============================================================
# STAGE 6 -- MintPy prep + reference point candidates
# ============================================================
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


def _find_reference_swath(paths: Paths) -> str:
    # not every AOI has burst data in every IW sub-swath -- pick whichever
    # swath actually got an IW*.xml written, rather than assuming IW1
    candidates = sorted(paths.stack_proc.glob("reference/IW*.xml"))
    if not candidates:
        raise RuntimeError(f"No reference IW*.xml found under {paths.stack_proc / 'reference'}")
    return candidates[0].stem


def stage6_mintpy_prep(cfg: dict, paths: Paths) -> list[dict]:
    import h5py
    import numpy as np

    def _do():
        paths.mintpy.mkdir(parents=True, exist_ok=True)
        mp = cfg["mintpy"]
        net = cfg["network"]
        content = CFG_TEMPLATE.format(
            reference_swath=_find_reference_swath(paths),
            num_worker=min(4, net["num_proc"]),
            unwrap_error_method=mp["unwrap_error_method"],
            deramp=mp["deramp"],
            tropo_method=mp["tropo_method"] if mp["tropo_enabled"] else "no",
            tropo_weather_model=mp["tropo_weather_model"],
            topographic_residual="yes" if mp["topographic_residual"] else "no",
        )
        paths.mintpy_cfg.write_text(content)

        subprocess.run(["smallbaselineApp.py", str(paths.mintpy_cfg), "--dostep", "load_data"],
                        check=True, cwd=str(paths.mintpy))

        # Guard against a race: if this runs before stage 5 has finished
        # unwrapping every pair (e.g. stages 5 and 6 invoked in separate
        # terminals), load_data silently loads whatever pair directories
        # exist AT THAT MOMENT, and mintpy.load.updateMode=auto (yes) plus
        # this stage's own checkpoint means it never gets re-run once the
        # rest of the pairs finish -- MintPy then silently inverts a
        # partial, wrong network with no error. Caught exactly this on the
        # Pettimudi validation run (2026-08-06): checkpointed after loading
        # only 3 of 9 pairs, producing a 3-date instead of 6-date time
        # series with no warning at any stage. Fail loudly instead.
        expected_pairs = len([d for d in (paths.stack_proc / "merged" / "interferograms").iterdir()
                               if d.is_dir()])
        ifgram_stack = paths.mintpy / "inputs" / "ifgramStack.h5"
        with h5py.File(ifgram_stack, "r") as f:
            loaded_pairs = f["date"].shape[0]
        if loaded_pairs != expected_pairs:
            raise RuntimeError(
                f"mintpy load_data only loaded {loaded_pairs}/{expected_pairs} interferogram "
                f"pairs -- stage 5 was likely still running when this stage started. Delete "
                f"{paths.mintpy} and the mintpy_prep/deliverables done-markers, confirm stage 5 "
                f"is fully finished, then re-run stage 6."
            )

        subprocess.run(["smallbaselineApp.py", str(paths.mintpy_cfg), "--dostep", "modify_network"],
                        check=True, cwd=str(paths.mintpy))

        subprocess.run(["generate_mask.py", str(ifgram_stack), "--nonzero",
                         "-o", str(paths.mintpy / "maskConnComp.h5"), "--update"],
                        check=True, cwd=str(paths.mintpy))
        subprocess.run(["temporal_average.py", str(ifgram_stack), "--dataset", "coherence",
                         "-o", str(paths.mintpy / "avgSpatialCoh.h5"), "--update"],
                        check=True, cwd=str(paths.mintpy))

    run_checkpointed(paths.run_dir, "mintpy_prep", _do)

    # ---- reference point candidate picker ----
    mp = cfg["mintpy"]
    lon_min, lat_min, lon_max, lat_max = cfg["aoi"]["bbox"]
    center_lat, center_lon = (lat_min + lat_max) / 2, (lon_min + lon_max) / 2

    with h5py.File(paths.mintpy / "maskConnComp.h5", "r") as f:
        mask = f["mask"][:]
    with h5py.File(paths.mintpy / "avgSpatialCoh.h5", "r") as f:
        avg_coh = f["coherence"][:]
    with h5py.File(paths.mintpy / "inputs" / "geometryRadar.h5", "r") as f:
        lat, lon, hgt = f["latitude"][:], f["longitude"][:], f["height"][:]
        shadow = f["shadowMask"][:] if "shadowMask" in f else np.zeros_like(lat, dtype=bool)

    center_row, center_col = np.unravel_index(
        np.argmin((lat - center_lat) ** 2 + (lon - center_lon) ** 2), lat.shape)
    center_elev = hgt[center_row, center_col]
    dist_km = np.sqrt(((lat - center_lat) * 111.0) ** 2 + ((lon - center_lon) * 109.0) ** 2)
    valid = mask.astype(bool) & (~shadow.astype(bool)) & (lat != 0) & (lon != 0)

    # Ionospheric correction has no universal on/off answer -- ISCE2's own
    # docs recommend it only where coherence is high enough for phase
    # unwrapping to succeed. Report the AOI's actual measured coherence so
    # that's an informed, per-study decision (made via ionosphere.enabled
    # in the config, for the *next* run) instead of a guess made blind,
    # before any real data existed for this AOI.
    if valid.any():
        aoi_mean_coh = float(np.nanmean(avg_coh[valid]))
        print(f"AOI average coherence (valid pixels): {aoi_mean_coh:.3f}")
        if aoi_mean_coh >= mp["reliability_threshold"]:
            print(f"  -> at/above this run's reliability_threshold ({mp['reliability_threshold']}) -- "
                  f"ionospheric correction (ionosphere.enabled) may be worth trying if this AOI is "
                  f"also low-latitude/ascending, per ISCE2's own guidance.")
        else:
            print(f"  -> below this run's reliability_threshold ({mp['reliability_threshold']}) -- ISCE2's "
                  f"own docs warn ionospheric correction 'only works well in areas with high coherence', "
                  f"so it's probably not reliable here; recommend leaving ionosphere.enabled off.")

    radius = mp["reference_search_radius_km"]
    good = np.zeros_like(valid)
    for _ in range(6):  # auto-widen if nothing found -- avoids the manual redo this needed once on Pettimudi
        good = valid & (avg_coh >= mp["reliability_threshold"]) & (dist_km <= radius)
        if good.sum() > 0:
            break
        radius *= 2
    if good.sum() == 0:
        good = valid  # last resort: any valid pixel, still surfaced with its real (low) coherence

    elev_diff_abs = np.abs(hgt - center_elev)
    score = np.where(good, dist_km + elev_diff_abs / 50.0, np.inf)
    flat_idx = np.argsort(score, axis=None)

    candidates = []
    for idx in flat_idx:
        if len(candidates) >= 10 or not good.flat[idx]:
            if len(candidates) >= 10:
                break
            continue
        row, col = np.unravel_index(idx, score.shape)
        candidates.append({
            "lat": float(lat[row, col]), "lon": float(lon[row, col]),
            "coherence": float(avg_coh[row, col]), "elevation_m": float(hgt[row, col]),
            "elevation_diff_m": float(hgt[row, col] - center_elev), "distance_km": float(dist_km[row, col]),
        })

    paths.review.mkdir(parents=True, exist_ok=True)
    with open(paths.review / "candidate_reference_points.json", "w") as f:
        json.dump(candidates, f, indent=2)

    print(f"Found {len(candidates)} reference point candidate(s). Top 5:")
    for c in candidates[:5]:
        print(f"  lat={c['lat']:.5f} lon={c['lon']:.5f} coherence={c['coherence']:.3f} "
              f"dist_km={c['distance_km']:.2f} elev_diff_m={c['elevation_diff_m']:.1f}")
    print(f"Full list: {paths.review / 'candidate_reference_points.json'}")
    return candidates


# ============================================================
# STAGE 7 -- MintPy inversion using a chosen reference point
# (look at stage 6's printed candidates, or paths.review/candidate_reference_points.json,
# then run: python pipeline.py 7 <lat> <lon>)
# ============================================================
def stage7_mintpy_run(cfg: dict, paths: Paths, lat: float, lon: float) -> None:
    # Replace any previous reference.lalo line rather than appending a new
    # one -- appending unconditionally means re-running this stage with a
    # different candidate (a completely normal thing to do, e.g. after
    # deciding the first pick wasn't on stable ground) piles up duplicate,
    # conflicting lines in the same config file. Confirmed by direct
    # testing: MintPy's own config reader happens to take the LAST
    # occurrence, so it isn't currently producing wrong results, but
    # relying on that undocumented behavior instead of just keeping one
    # clean line is fragile, not something this should depend on silently.
    lines = [l for l in paths.mintpy_cfg.read_text().splitlines()
              if not l.strip().startswith("mintpy.reference.lalo")]
    lines.append(f"mintpy.reference.lalo      = {lat}, {lon}")
    paths.mintpy_cfg.write_text("\n".join(lines) + "\n")

    process = subprocess.Popen(
        ["smallbaselineApp.py", str(paths.mintpy_cfg), "--start", "reference_point"],
        cwd=str(paths.mintpy), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in process.stdout:
        print(line.rstrip())
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(f"MintPy run failed with exit code {returncode}")
    print("mintpy_run complete.")


# ============================================================
# STAGE 8 -- deliverables (velocity map, time series, CSV, KMZ)
# ============================================================
def stage8_deliverables(cfg: dict, paths: Paths) -> dict:
    import h5py
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import numpy as np
    from PIL import Image

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

    result = {}

    def _do():
        geo_dir = paths.mintpy / "geo"
        vel_data, atr = _load_geo(geo_dir / "geo_velocity.h5", ["velocity"])
        tcoh_data, _ = _load_geo(geo_dir / "geo_temporalCoherence.h5", ["temporalCoherence"])
        ts_files = list(geo_dir.glob("geo_timeseries*.h5"))
        if not ts_files:
            raise FileNotFoundError(f"No geo_timeseries*.h5 found in {geo_dir}")
        ts_data, _ = _load_geo(ts_files[0], ["timeseries", "date"])

        lats, lons = _latlon_grid(atr, vel_data["velocity"].shape)
        lon_min, lat_min, lon_max, lat_max = cfg["aoi"]["bbox"]
        r0, r1, c0, c1 = _crop_indices(lats, lons, lat_min, lat_max, lon_min, lon_max)

        vel_crop = vel_data["velocity"][r0:r1, c0:c1] * 1000  # mm/yr
        tcoh_crop = tcoh_data["temporalCoherence"][r0:r1, c0:c1]
        ts_crop = ts_data["timeseries"][:, r0:r1, c0:c1] * 1000
        lats_crop, lons_crop = lats[r0:r1], lons[c0:c1]
        dates = [d.decode() if isinstance(d, bytes) else d for d in ts_data["date"]]
        dates_fmt = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]

        reliable_mask = tcoh_crop > cfg["mintpy"]["reliability_threshold"]
        extent = [lons_crop.min(), lons_crop.max(), lats_crop.min(), lats_crop.max()]
        paths.deliverables.mkdir(parents=True, exist_ok=True)

        vel_masked = np.where(reliable_mask, vel_crop, np.nan)
        vmax_v = float(np.nanpercentile(np.abs(vel_masked[reliable_mask]), 98)) if reliable_mask.any() else 1.0
        fig, ax = plt.subplots(figsize=(9, 8))
        im = ax.imshow(vel_masked, extent=extent, origin="upper", cmap="jet_r", vmin=-vmax_v, vmax=vmax_v, aspect="auto")
        plt.colorbar(im, ax=ax, shrink=0.8, label="LOS velocity (mm/yr)")
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        ax.set_title(f"LOS Deformation Velocity -- {cfg['run_id']}")
        plt.tight_layout()
        plt.savefig(paths.deliverables / "velocity_map.png", dpi=200)
        plt.close()

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
        fig.suptitle(f"Cumulative LOS Displacement by Date -- {cfg['run_id']}")
        cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
        fig.colorbar(im, cax=cbar_ax, label="Cumulative displacement (mm)")
        plt.tight_layout(rect=[0, 0, 0.9, 0.95])
        plt.savefig(paths.deliverables / "timeseries_by_date.png", dpi=200)
        plt.close()

        lon_min_c, lat_min_c, lon_max_c, lat_max_c = cfg["aoi"]["bbox"]
        center_row = int(np.argmin(np.abs(lats_crop - (lat_min_c + lat_max_c) / 2)))
        center_col = int(np.argmin(np.abs(lons_crop - (lon_min_c + lon_max_c) / 2)))
        r_lo, r_hi = max(0, center_row - 3), center_row + 3
        c_lo, c_hi = max(0, center_col - 3), center_col + 3
        center_ts = np.nanmean(ts_crop[:, r_lo:r_hi, c_lo:c_hi], axis=(1, 2))
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.plot(dates_fmt, center_ts, "o-", color="darkred")
        ax.set_xlabel("Date"); ax.set_ylabel("Cumulative LOS displacement (mm)")
        ax.set_title(f"Displacement Time Series at AOI Center -- {cfg['run_id']}")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()
        plt.tight_layout()
        plt.savefig(paths.deliverables / "aoi_center_timeseries.png", dpi=200)
        plt.close()

        csv_path = paths.deliverables / "whole_aoi_statistics.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Mean_mm", "Median_mm", "StdDev_mm", "Min_mm", "Max_mm", "N_Reliable_Pixels"])
            for i, d in enumerate(dates_fmt):
                frame = np.where(reliable_mask, ts_crop[i], np.nan)
                writer.writerow([d, round(float(np.nanmean(frame)), 2), round(float(np.nanmedian(frame)), 2),
                                  round(float(np.nanstd(frame)), 2), round(float(np.nanmin(frame)), 2),
                                  round(float(np.nanmax(frame)), 2), int(np.sum(~np.isnan(frame)))])

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
        kml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n'
               f'<name>{cfg["run_id"]} -- LOS Velocity</name>\n<GroundOverlay>\n<name>LOS velocity (mm/yr)</name>\n'
               f'<Icon><href>velocity.png</href></Icon>\n'
               f'<LatLonBox><north>{north}</north><south>{south}</south><east>{east}</east><west>{west}</west></LatLonBox>\n'
               f'</GroundOverlay>\n</Document>\n</kml>')
        kmz_path = paths.deliverables / "velocity_overlay.kmz"
        with zipfile.ZipFile(kmz_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", kml)
            z.write(tmp_png, "velocity.png")
        tmp_png.unlink()

        result.update({
            "reliable_pixels": int(reliable_mask.sum()),
            "total_pixels": int(reliable_mask.size),
            "reliable_pct": round(100 * reliable_mask.sum() / reliable_mask.size, 2),
            "velocity_min_mmyr": float(np.nanmin(vel_masked)) if reliable_mask.any() else None,
            "velocity_max_mmyr": float(np.nanmax(vel_masked)) if reliable_mask.any() else None,
            "dates": dates_fmt,
        })
        with open(paths.deliverables / "summary.json", "w") as f:
            json.dump(result, f, indent=2)

    run_checkpointed(paths.run_dir, "deliverables", _do)
    if not result and (paths.deliverables / "summary.json").exists():
        with open(paths.deliverables / "summary.json") as f:
            result = json.load(f)

    print("Deliverables summary:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print(f"\nFiles written to {paths.deliverables}")
    return result


# ============================================================
# entrypoint
# ============================================================
STAGES = {
    "0": stage0_env_precheck,
    "1": stage1_search_download,
    "2": stage2_dem,
    "3": stage3_orbits,
    "4": stage4_stack_setup,
    "5": stage5_stack_execute,
    "6": stage6_mintpy_prep,
    "7": stage7_mintpy_run,
    "8": stage8_deliverables,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or (sys.argv[1] not in STAGES and sys.argv[1] != "tracks"):
        print(__doc__)
        sys.exit(1)

    stage_key = sys.argv[1]
    extra_args = sys.argv[2:]

    if stage_key == "tracks":
        config_path = extra_args[0] if extra_args else None
        cfg, paths = setup(config_path)
        search_tracks(cfg)
    elif stage_key == "7":
        # BUG FIXED 2026-08-05: this used to call setup() with no config
        # path at all, meaning it silently ALWAYS used the default config
        # (dummy_test_aoi.yaml) regardless of which config every other
        # stage was run with -- for any non-default run, stage 7 would
        # silently operate on the wrong run's data. Now takes an optional
        # 3rd argument, same as every other stage.
        if len(extra_args) not in (2, 3):
            print("Usage: python pipeline.py 7 <lat> <lon> [config_yaml]")
            sys.exit(1)
        lat, lon = float(extra_args[0]), float(extra_args[1])
        config_path = extra_args[2] if len(extra_args) == 3 else None
        cfg, paths = setup(config_path)
        stage7_mintpy_run(cfg, paths, lat, lon)
    else:
        config_path = extra_args[0] if extra_args else None
        cfg, paths = setup(config_path)
        STAGES[stage_key](cfg, paths)
