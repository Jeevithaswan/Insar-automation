"""Stage 0: environment precheck. Runs on every single run, for any AOI --
this is deliberately not a one-time setup step, since the NumPy patches live
in installed site-packages and can silently revert if the env is ever
rebuilt."""
from __future__ import annotations

from orchestrator import env as env_checks
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths


class EnvPrecheckFailed(Exception):
    def __init__(self, checks):
        self.checks = checks
        failed = [c for c in checks if not c["ok"]]
        msg = "; ".join(f"{c['check']}: {c['detail']}" for c in failed)
        super().__init__(f"Environment precheck failed: {msg}")


def run(config: RunConfig, paths: RunPaths) -> dict:
    result = env_checks.run_all_checks(require_credentials=True)
    if not result["ok"]:
        raise EnvPrecheckFailed(result["checks"])
    return result
