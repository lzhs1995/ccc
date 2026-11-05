"""Explicit Actions-only admission; never launch this fixture on the user's Mac."""
import os
import platform
from pathlib import Path
import sys


def require_actions(environ=None, system=None, machine=None):
    env = os.environ if environ is None else environ
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    expected = {"GITHUB_ACTIONS": "true", "CI": "true", "RUNNER_OS": "macOS",
                "RUNNER_ARCH": "ARM64", "RUNNER_ENVIRONMENT": "github-hosted",
                "GITHUB_REPOSITORY": "lzhs1995/ccc"}
    if system != "darwin" or machine != "arm64" or any(env.get(k) != v for k, v in expected.items()):
        raise RuntimeError("isolated GitHub-hosted macOS ARM64 fixture only; no local launch")
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not str(env.get(key, "")).isdigit() or int(env[key]) < 1:
            raise RuntimeError("missing exact fixture job identity")
    temp = Path(env.get("RUNNER_TEMP", ""))
    if not temp.is_absolute() or not temp.is_dir():
        raise RuntimeError("missing original runner temporary directory")
    return temp.resolve()


def require_output(path, runner_temp):
    path = Path(path)
    resolved = path.resolve()
    if path.is_symlink() or resolved == runner_temp or not resolved.is_relative_to(runner_temp) or resolved.exists():
        raise RuntimeError("fixture output must be a new directory inside original RUNNER_TEMP")
    return resolved


def require_capacity(path):
    info = os.statvfs(path)
    available = info.f_bavail * info.f_frsize
    if available < 40 * 1024**3:
        raise RuntimeError("private fixture requires at least 40 GiB available; no launch performed")
    return available
