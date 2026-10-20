#!/usr/bin/env python3
"""Verify the complete pinned producer dependency closure before executing it."""
import hashlib
import json
from pathlib import Path

EXPECTED_MANIFEST = "4c30c0929edf9611c685a0365fe521bd11d7c41755edcbd024efa6a64694850f"

def main():
    root = Path(__file__).resolve().parent
    raw = (root / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_MANIFEST:
        raise ValueError("Python producer manifest changed")
    manifest = json.loads(raw)
    actual = {p.name for p in root.glob("*.py")} - {"verify_source.py"}
    if actual != set(manifest["files"]):
        raise ValueError("Python dependency closure paths changed")
    for name, info in manifest["files"].items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Python source is not a regular file: " + name)
        data = path.read_bytes()
        if len(data) != info["bytes"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise ValueError("Python producer source changed: " + name)
        compile(data, str(path), "exec")
    print(json.dumps({"status": "PINNED_PYTHON_SOURCE_VERIFIED",
                      "manifest_sha256": EXPECTED_MANIFEST,
                      "files": len(actual),
                      "native_admission": "NOT_RUN"}))

if __name__ == "__main__":
    main()

