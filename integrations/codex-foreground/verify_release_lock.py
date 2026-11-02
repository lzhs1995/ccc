#!/usr/bin/env python3
"""Verify the release-version refresh without permitting dependency upgrades."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--kind", choices=("candidate", "baseline"), required=True)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    root = args.codex.resolve(strict=True)
    for expression, expected in (("HEAD", manifest["upstream_commit"]),
                                 ("HEAD^{tree}", manifest["upstream_tree"])):
        actual = subprocess.run(
            ["git", "-C", str(root), "rev-parse", expression],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        if actual != expected:
            raise ValueError("release lock is not based on the pinned upstream")
    data = (root / "codex-rs/Cargo.lock").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    expected = manifest["release_lock"][args.kind + "_sha256"]
    if digest != expected:
        raise ValueError(
            "cargo update --workspace changed more than the pinned release versions: "
            f"expected {expected}, got {digest}"
        )
    packages = tomllib.loads(data.decode("utf-8"))["package"]
    print(json.dumps({
        "kind": args.kind,
        "lock_sha256": digest,
        "release_version_packages": manifest["release_lock"]["package_count"],
        "external_packages": sum("source" in p for p in packages),
        "external_dependency_upgrades": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
