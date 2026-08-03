"""
Idempotent ISCE2 / NumPy compatibility patches.

Newer NumPy raises when an array (not a plain scalar) is formatted with %f.
Two files inside ISCE2's installed site-packages do this in their Chi-squared
debug print, causing a crash deep inside topsStack processing (unrelated to
any specific AOI -- this is an environment-level bug, not a per-region one).

These patches are environment-level and must be re-verified on every run
(not just applied once at env-creation time), because they live inside
installed site-packages and can silently revert if the conda env is ever
rebuilt/updated.
"""
import os
import sys

PATCHES = [
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
]


def site_packages_dir():
    """Picks the site-packages dir that actually has ISCE2 installed, not
    just the first "site-packages"-named entry on sys.path -- an unrelated
    ~/.local/lib/pythonX/site-packages (e.g. from an unrelated --user pip
    install) can otherwise outrank the active conda env's site-packages,
    silently pointing every patch check at files that don't exist."""
    candidates = [p for p in sys.path if p.endswith("site-packages") and os.path.isdir(p)]
    for p in candidates:
        if os.path.isdir(os.path.join(p, "isce")):
            return p
    if candidates:
        return candidates[0]
    raise RuntimeError("Could not locate site-packages directory")


def check_and_apply(apply: bool):
    """Returns a list of {name, status, path} where status is one of:
    'already_patched', 'patched_now', 'needs_patch', 'not_found', 'unexpected_content'."""
    sp = site_packages_dir()
    results = []
    for p in PATCHES:
        path = os.path.join(sp, p["relpath"])
        if not os.path.exists(path):
            results.append({"name": p["name"], "status": "not_found", "path": path})
            continue
        with open(path) as f:
            content = f.read()
        if p["new"] in content:
            results.append({"name": p["name"], "status": "already_patched", "path": path})
        elif p["old"] in content:
            if apply:
                content = content.replace(p["old"], p["new"])
                with open(path, "w") as f:
                    f.write(content)
                results.append({"name": p["name"], "status": "patched_now", "path": path})
            else:
                results.append({"name": p["name"], "status": "needs_patch", "path": path})
        else:
            results.append({"name": p["name"], "status": "unexpected_content", "path": path})
    return results


if __name__ == "__main__":
    apply = "--check-only" not in sys.argv
    results = check_and_apply(apply=apply)
    ok = True
    for r in results:
        print(f"{r['name']}: {r['status']} ({r['path']})")
        if r["status"] in ("not_found", "unexpected_content", "needs_patch"):
            ok = False
    sys.exit(0 if ok else 1)
