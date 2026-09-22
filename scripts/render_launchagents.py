#!/usr/bin/env python3
"""Render service plists without activating services or replacing a runtime."""
from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cmux_codex_watch as watcher


def render(output: Path, runtime_root: Path, janitor_dir: Path) -> list[Path]:
    # Rendering never changes the installed code pointer. A fresh machine uses
    # `ccc install`, which owns the tested runtime/plist activation transaction.
    current = (runtime_root / "current").resolve(strict=True)
    watcher.validate_runtime_release(current)
    output.mkdir(parents=True, exist_ok=True)
    paths = [output / f"{watcher.DEFAULT_LABEL}.plist"]
    watcher.write_plist(paths[0], runtime_dir=current)
    for component, script, interval, at_load in (
        ("cmux-janitor", "cmux-janitor.sh", 1800, False),
        ("cmux-janitor-guard", "guard.sh", 60, True),
        ("cmux-janitor-quarantine-purge", "expire.sh", 300, False),
    ):
        label = f"{watcher.LABEL_PREFIX}.{component}"
        path = output / f"{label}.plist"
        payload = {"Label": label, "ProgramArguments": ["/bin/bash", str(janitor_dir / script)],
                   "StartInterval": interval, "RunAtLoad": at_load, "KeepAlive": False,
                   "EnvironmentVariables": {"USER": os.environ.get("USER", ""),
                                            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}
        path.write_bytes(plistlib.dumps(payload, sort_keys=False))
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", type=Path, default=Path.home() / "Library/LaunchAgents")
    parser.add_argument("--runtime-root", type=Path, default=watcher.DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--janitor-dir", type=Path,
                        default=Path(os.environ.get("CCC_CONFIG") or Path.home() / ".config/cmux-janitor"))
    args = parser.parse_args()
    try:
        paths = render(args.output, args.runtime_root, args.janitor_dir)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Cannot render installed runtime: {exc}. Run ccc install first.\n")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
