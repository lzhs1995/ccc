#!/usr/bin/env python3
"""Apply this package only to a clean, exact upstream checkout on GHA."""
import argparse
import os
from pathlib import Path
import subprocess
from verify import verify_package, verify_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--package-pin", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("source preparation is restricted to the isolated GHA checkout")
    root = Path(__file__).resolve().parent
    manifest = verify_package(root, args.package_pin)
    source = args.source.resolve(strict=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    clean = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True)
    if head != manifest["base_commit"] or clean:
        raise SystemExit("source must be the clean pinned upstream commit")
    subprocess.run(["git", "apply", "--check", str(root / "candidate.patch")], cwd=source, check=True)
    subprocess.run(["git", "apply", str(root / "candidate.patch")], cwd=source, check=True)
    print("Verified effective source files:", verify_source(source, manifest))


if __name__ == "__main__":
    main()
