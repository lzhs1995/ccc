#!/usr/bin/env python3
"""Reconstruct and verify the frozen native compact candidate; never build it."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import urllib.request


UPSTREAM_COMMIT = "a956835d020762cb2b570053af06f643a11c0ecc"
UPSTREAM_ARCHIVE_SHA256 = "a04042fa9ecce4b06445d769e68496c56c371d7472bb2cdfbe9d16aff5e17fd3"
PINNED_FILES = {
    "baseline-overlay.tar.gz": "654b5c8774f631b5c9dce0a9f924d9c91b0853c6a2b9f58a0405dca7cd71383a",
    "baseline-overlay-manifest.json": "72afea8f26d6438f80303c7e7a7ceddb909e2b33ff9e16ab5549de20d7229792",
    "compact-native-timeout.patch": "c028bac2df2b86c548f10ed9c8c0d6fbcd058d6310ad29f573b80bc2e7f7b6f1",
    "source-manifest.json": "3362bde0f953e1c899849ba56112e1bcd9637f804f3bdffc8cf6e25110ea2bd5",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_file(path, expected):
    actual = digest(path.read_bytes())
    if actual != expected:
        raise ValueError(f"SHA256 mismatch for {path}: {actual}, expected {expected}")


def snapshot(root):
    files = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        filenames += [name for name in directories if (Path(current) / name).is_symlink()]
        for name in filenames:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                files[relative] = {
                    "kind": "symlink",
                    "sha256": digest(os.fsencode(target)),
                    "target": target,
                    "git_mode": "120000",
                }
            else:
                data = path.read_bytes()
                files[relative] = {
                    "kind": "file",
                    "sha256": digest(data),
                    "bytes": len(data),
                    "git_mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                }
    return files


def tree_digest(files):
    return digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())


def extract_upstream(archive, destination):
    with tarfile.open(archive, "r:gz") as source:
        members = []
        for member in source.getmembers():
            parts = PurePosixPath(member.name).parts
            if parts == ("openai-codex-a956835",):
                continue
            if len(parts) < 2 or parts[0] != "openai-codex-a956835" or ".." in parts:
                raise ValueError(f"Unexpected upstream archive path: {member.name}")
            stripped = copy.copy(member)
            stripped.name = PurePosixPath(*parts[1:]).as_posix()
            members.append(stripped)
        source.extractall(destination, members=members, filter="data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--upstream-archive", type=Path)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    for name, expected in PINNED_FILES.items():
        verify_file(bundle / name, expected)
    baseline = json.loads((bundle / "baseline-overlay-manifest.json").read_text())
    candidate = json.loads((bundle / "source-manifest.json").read_text())
    if baseline["upstream_commit"] != UPSTREAM_COMMIT or candidate["upstream_commit"] != UPSTREAM_COMMIT:
        raise ValueError("Upstream commit mismatch")
    destination = args.output.resolve()
    if destination.exists():
        raise ValueError(f"Output must not exist: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = args.upstream_archive
    if archive is None:
        archive = destination.parent / f"upstream-{UPSTREAM_COMMIT}.tar.gz"
        with urllib.request.urlopen(baseline["upstream_archive_url"], timeout=60) as response:
            with archive.open("wb") as output:
                shutil.copyfileobj(response, output)
    verify_file(archive, UPSTREAM_ARCHIVE_SHA256)
    extract_upstream(archive, destination)
    files = snapshot(destination)
    if len(files) != baseline["upstream_file_count"]:
        raise ValueError("Unexpected upstream file count")
    for change in baseline["files"]:
        if files.get(change["path"]) != change["upstream"]:
            raise ValueError(f"Overlay precondition failed: {change['path']}")
    if baseline["removed"]:
        raise ValueError("This frozen baseline must not remove upstream files")
    with tarfile.open(bundle / "baseline-overlay.tar.gz", "r:gz") as overlay:
        expected_paths = {change["path"] for change in baseline["files"]}
        members = overlay.getmembers()
        if len(members) != len(expected_paths) or {member.name for member in members} != expected_paths:
            raise ValueError("Overlay entries do not match the manifest")
        overlay.extractall(destination, filter="data")
    files = snapshot(destination)
    if len(files) != baseline["baseline_file_count"] or tree_digest(files) != baseline["source_tree_sha256"]:
        raise ValueError("Full reconstructed baseline does not match the frozen source tree")
    for change in candidate["files"]:
        name = "codex-rs/" + change["path"]
        if (files[name]["sha256"] if name in files else None) != change["baseline_sha256"]:
            raise ValueError(f"Candidate baseline mismatch: {name}")
        files[name] = {
            "kind": "file",
            "sha256": change["candidate_sha256"],
            "bytes": change["candidate_bytes"],
            "git_mode": files.get(name, {}).get("git_mode", "100644"),
        }
    # Avoid inheriting a surrounding checkout's repository prefix for this patch.
    git_env = os.environ.copy()
    git_env["GIT_CEILING_DIRECTORIES"] = str(destination)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        git_env.pop(key, None)
    patch = str(bundle / "compact-native-timeout.patch")
    for command in (["git", "apply", "--check", patch], ["git", "apply", patch]):
        subprocess.run(command, cwd=destination / "codex-rs", env=git_env, check=True)
    actual = snapshot(destination)
    if actual != files:
        raise ValueError("Applied candidate differs from the complete expected source tree")
    for name, expected in candidate["supporting_source_sha256"].items():
        verify_file(destination / "codex-rs" / name, expected)
    report = {
        "status": "SOURCE_RECONSTRUCTION_VERIFIED",
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_archive_sha256": UPSTREAM_ARCHIVE_SHA256,
        "pinned_files": PINNED_FILES,
        "baseline_file_count": baseline["baseline_file_count"],
        "baseline_tree_sha256": baseline["source_tree_sha256"],
        "candidate_file_count": len(actual),
        "candidate_tree_sha256": tree_digest(actual),
        "candidate_changed_files": len(candidate["files"]),
        "compilation": "NOT_RUN_BY_PREPARATION",
        "tests": "NOT_RUN_BY_PREPARATION",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
