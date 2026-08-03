"""
Environment sanity checks, run standalone (Settings page "run diagnostics")
and as pipeline stage 0 on every single run. Patch re-verification happens
every run (not just once) because the patches live inside installed
site-packages and can silently revert if the conda env is ever rebuilt.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

PATCHES_MODULE = os.path.expanduser("~/insar-automation/patches/apply_patches.py")

REQUIRED_IMPORTS = ["isce", "mintpy", "asf_search", "dem_stitcher", "h5py", "rasterio", "yaml", "pydantic"]
REQUIRED_CLI_TOOLS = ["stackSentinel.py", "smallbaselineApp.py", "eof", "gdal2isce_xml.py"]


def _load_patches_module():
    spec = importlib.util.spec_from_file_location("apply_patches", PATCHES_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_conda_env() -> dict:
    return {
        "check": "conda_env",
        "ok": os.environ.get("CONDA_DEFAULT_ENV") == "insar",
        "detail": f"CONDA_DEFAULT_ENV={os.environ.get('CONDA_DEFAULT_ENV')}",
    }


def check_python_imports() -> list[dict]:
    results = []
    for mod in REQUIRED_IMPORTS:
        ok = importlib.util.find_spec(mod) is not None
        results.append({"check": f"import:{mod}", "ok": ok, "detail": "" if ok else "not importable"})
    return results


def check_cli_tools() -> list[dict]:
    results = []
    for tool in REQUIRED_CLI_TOOLS:
        path = shutil.which(tool)
        results.append({"check": f"cli:{tool}", "ok": path is not None, "detail": path or "not on PATH"})
    return results


def check_numpy_patches() -> list[dict]:
    mod = _load_patches_module()
    patch_results = mod.check_and_apply(apply=True)  # auto-apply if needed, exactly like a one-time env fix
    return [
        {"check": f"patch:{p['name']}", "ok": p["status"] in ("already_patched", "patched_now"), "detail": p["status"]}
        for p in patch_results
    ]


def check_disk_space(min_gb: float = 20.0) -> dict:
    total, used, free = shutil.disk_usage(os.path.expanduser("~"))
    free_gb = free / (1024 ** 3)
    return {
        "check": "disk_space",
        "ok": free_gb >= min_gb,
        "detail": f"{free_gb:.1f} GB free (need >= {min_gb} GB)",
    }


def check_credentials() -> list[dict]:
    netrc_ok = os.path.exists(os.path.expanduser("~/.netrc"))
    cdsapirc_ok = os.path.exists(os.path.expanduser("~/.cdsapirc"))
    return [
        {"check": "earthdata_netrc", "ok": netrc_ok,
         "detail": "~/.netrc present (needed for ASF download)" if netrc_ok else "~/.netrc MISSING -- set via Settings page before running search_download"},
        {"check": "cds_api_credentials", "ok": cdsapirc_ok,
         "detail": "~/.cdsapirc present" if cdsapirc_ok else "~/.cdsapirc missing -- only needed if tropo_enabled=true"},
    ]


def run_all_checks(require_credentials: bool = True) -> dict:
    """Returns {'ok': bool, 'checks': [...]}. require_credentials=False lets
    a dry env check pass even before .netrc is set up (e.g. right after a
    fresh install, before the user has visited Settings)."""
    checks = []
    checks.append(check_conda_env())
    checks.extend(check_python_imports())
    checks.extend(check_cli_tools())
    checks.extend(check_numpy_patches())
    checks.append(check_disk_space())
    cred_checks = check_credentials()
    checks.extend(cred_checks)

    hard_fail_checks = [c for c in checks if c["check"] != "cds_api_credentials"]
    if not require_credentials:
        hard_fail_checks = [c for c in hard_fail_checks if c["check"] != "earthdata_netrc"]

    ok = all(c["ok"] for c in hard_fail_checks)
    return {"ok": ok, "checks": checks}


if __name__ == "__main__":
    result = run_all_checks()
    for c in result["checks"]:
        status = "OK" if c["ok"] else "FAIL"
        print(f"[{status}] {c['check']}: {c['detail']}")
    sys.exit(0 if result["ok"] else 1)
