#!/usr/bin/env python3
"""Build the frozen tree's native dependencies outside that tree for full tests."""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.absolute()
    if output.exists() or output.is_relative_to(source):
        raise ValueError("native dependency output must be fresh and outside source")
    output.mkdir(parents=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    manifest_path = source / "third_party/voice/sources.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest["schema_version"] != 1:
        raise ValueError("unknown native source manifest")
    archives = output / "archives"
    archives.mkdir()

    def download(entry):
        name = entry["archive"]
        if Path(name).name != name or not entry["url"].startswith("https://"):
            raise ValueError("invalid archive input")
        request = urllib.request.Request(entry["url"], headers={"User-Agent": "ccc-full-native-deps"})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) > 64 * 1024 * 1024 or digest != entry["sha256"]:
            raise ValueError(f"native archive mismatch: {name}")
        (archives / name).write_bytes(raw)
        print(f"Verified {name}: {digest}", flush=True)
        return {"name": name, "bytes": len(raw), "sha256": digest, "url": entry["url"]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        archive_records = list(pool.map(download, manifest["sources"]))
    receipt = {"source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
               "archives": archive_records, "build": "NOT_RUN"}
    receipt_path = args.evidence / "native-dependencies.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    build = output / "build"
    command = ["python3", "-I", "-B", str(source / "third_party/voice/build_native.py"),
               "--archives", str(archives), "--output", str(build),
               "--target", "x86_64-unknown-linux-gnu", "--jobs", "2"]
    for role, name in (("cc", "gcc"), ("cxx", "g++"), ("cmake", "cmake"),
                       ("make", "make"), ("pkg-config", "pkg-config"), ("shell", "bash")):
        tool = shutil.which(name)
        if not tool:
            raise ValueError(f"native dependency tool missing: {name}")
        command.extend([f"--{role}", tool])
    receipt["command"] = command
    try:
        subprocess.run(command, check=True)
    finally:
        logs = args.evidence / "native-dependency-build"
        logs.mkdir(exist_ok=True)
        if build.is_dir():
            for pattern in ("*.json", "*.log"):
                for path in build.glob(pattern):
                    shutil.copy2(path, logs / path.name)
    built = json.loads((build / "built.json").read_text())
    if built["manifest_sha256"] != receipt["source_manifest_sha256"]:
        raise ValueError("native dependency build manifest changed")
    prefix = build / "prefix"
    system_pc_paths = subprocess.check_output(
        ["pkg-config", "--variable=pc_path", "pkg-config"], text=True).strip()
    # The voice helper also needs the runner's declared ALSA inputs. Prefer all
    # frozen native metadata, then retain distro metadata for those system libs.
    pc_paths = str(prefix / "lib/pkgconfig") + os.pathsep + system_pc_paths
    env = {**os.environ, "PKG_CONFIG_PATH": "",
           "PKG_CONFIG_LIBDIR": pc_paths}
    versions = {}
    for module in ("glib-2.0", "gobject-2.0", "gio-2.0", "gstreamer-1.0",
                   "gstreamer-base-1.0", "gstreamer-app-1.0", "gstreamer-audio-1.0"):
        version = subprocess.check_output(["pkg-config", "--modversion", module], env=env, text=True).strip()
        if module.startswith("gstreamer") and tuple(map(int, version.split(".")[:2])) < (1, 28):
            raise ValueError(f"GStreamer 1.28 required: {module} {version}")
        versions[module] = version
    receipt.update({"build": "PASSED", "versions": versions, "prefix": str(prefix),
                    "pkg_config_libdir": pc_paths,
                    "built_receipt_sha256": hashlib.sha256((build / "built.json").read_bytes()).hexdigest()})
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    with open(os.environ["GITHUB_ENV"], "a") as handle:
        handle.write(f"PKG_CONFIG_PATH=\nPKG_CONFIG_LIBDIR={pc_paths}\n")
        handle.write(f"LD_LIBRARY_PATH={prefix}/lib\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
