#!/usr/bin/env python3
"""Package only a clean committed tree, with reproducible SHA-256 manifests."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


def package(root: Path, output: Path) -> Path:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])

    if git("status", "--porcelain").strip():
        raise RuntimeError("release packaging requires a clean committed tree")
    commit = git("rev-parse", "HEAD").decode().strip()
    version = git("show", "HEAD:VERSION").decode().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise RuntimeError("invalid VERSION")
    epoch = int(git("show", "-s", "--format=%ct", "HEAD"))
    source = git("archive", "--format=tar", "HEAD")
    prefix = f"ccc-{version}"
    records = {}
    entries = []
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or member.name == "RELEASE-MANIFEST.json":
                raise RuntimeError(f"unsupported release entry: {member.name}")
            content = archive.extractfile(member).read()
            records[member.name] = hashlib.sha256(content).hexdigest()
            # git archive's tar.umask can differ by machine; Git tracks only the
            # executable bit, so publish canonical permissions for reproducibility.
            entries.append((member.name, 0o755 if member.mode & 0o111 else 0o644, content))
    manifest = {"version": version, "commit": commit, "source_date_epoch": epoch,
                "files": dict(sorted(records.items()))}
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    entries.append(("RELEASE-MANIFEST.json", 0o644, manifest_bytes))
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"{prefix}.tar.gz"
    with artifact.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as zipped:
        with tarfile.open(fileobj=zipped, mode="w|") as archive:
            for name, mode, content in sorted(entries):
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size, info.mode, info.mtime = len(content), mode, epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest_checksum = hashlib.sha256(manifest_bytes).hexdigest()
    (output / "SHA256SUMS").write_text(
        f"{checksum}  {artifact.name}\n{manifest_checksum}  RELEASE-MANIFEST.json\n")
    (output / "RELEASE-MANIFEST.json").write_bytes(manifest_bytes)
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    print(package(args.root, args.output))


if __name__ == "__main__":
    main()
