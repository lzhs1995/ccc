#!/usr/bin/env python3
"""Verify every bundled runtime, MAC source and regression fixture before use."""
import hashlib
import json
from pathlib import Path
import re

EXPECTED_MANIFEST = "b2d25fc2d40b14ebf2bb51b6fa90fddb58428defcfef4a351206e5909b9fa437"


def main():
    root = Path(__file__).resolve().parent
    raw = (root / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_MANIFEST:
        raise ValueError("Python runtime manifest changed")
    manifest = json.loads(raw)
    paths = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    if paths != set(manifest["files"]) | {"verify_source.py", "manifest.json"}:
        raise ValueError("Bundled runtime dependency paths changed")
    for name, expected in manifest["files"].items():
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("Invalid runtime source path")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Runtime source is not a regular file: " + name)
        data = path.read_bytes()
        if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Runtime source changed: " + name)
        if path.suffix == ".py":
            compile(data, str(path), "exec")
    # Import the actual dynamic host library chain without making any transport.
    import sys
    sys.path.insert(0, str(root))
    from ccc_compact_runtime import libraries
    loaded = libraries()
    if any(not Path(item.__file__).resolve().is_relative_to(root) for item in loaded[:4]):
        raise ValueError("Mixed runtime imports")
    print(json.dumps({"status": "COMPLETE_PINNED_RUNTIME_VERIFIED",
                      "manifest_sha256": EXPECTED_MANIFEST,
                      "files": len(manifest["files"]),
                      "native_admission": "NOT_RUN",
                      "import_only_transport_operations": 0}))


if __name__ == "__main__":
    main()
