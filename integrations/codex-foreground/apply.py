#!/usr/bin/env python3
"""Apply only the pinned TUI patch and native module to a clean upstream tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("non-relative integration path")
    return path


def run(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, text=True, capture_output=True,
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    repo = bundle.parents[1]
    root = args.codex.resolve(strict=True)
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    if manifest["schema"] != "ccc-codex-foreground-integration-v187":
        raise ValueError("unsupported integration manifest")
    if (run(root, "rev-parse", "HEAD") != manifest["upstream_commit"]
            or run(root, "rev-parse", "HEAD^{tree}") != manifest["upstream_tree"]
            or run(root, "status", "--porcelain")):
        raise ValueError("clean exact upstream checkout required")
    patch = bundle / relative_path(manifest["patch"]["path"])
    if digest(patch) != manifest["patch"]["sha256"]:
        raise ValueError("integration patch hash changed")
    for name, pin in manifest["patched_files"].items():
        path = root / relative_path(name)
        expected = pin["before_sha256"]
        if expected is None:
            if path.exists():
                raise ValueError("new integration path already exists")
        elif not path.is_file() or digest(path) != expected:
            raise ValueError("upstream integration preimage changed")
    run(root, "apply", "--check", str(patch))
    for name, expected in manifest["native_files"].items():
        relative = relative_path(name)
        source, target = repo / relative, root / relative
        if not source.is_file() or digest(source) != expected or target.exists():
            raise ValueError("native module source or destination changed")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    run(root, "apply", str(patch))
    for name, pin in manifest["patched_files"].items():
        path = root / relative_path(name)
        if not path.is_file() or digest(path) != pin["after_sha256"]:
            raise ValueError("applied integration bytes differ")
    print(json.dumps({
        "upstream_commit": manifest["upstream_commit"],
        "patch_sha256": manifest["patch"]["sha256"],
        "patched_files": len(manifest["patched_files"]),
        "native_files": len(manifest["native_files"]),
        "runtime_installed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
