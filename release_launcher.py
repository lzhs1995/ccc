#!/usr/bin/env python3
"""Verify and optionally execute one content-addressed local CCC release.

This helper never installs or restarts launchd.  Verification is the default;
execution requires an explicit ``--run``.  The manifest is an operator approval
record, not a cryptographic signature or a defence against the local file owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseVerificationError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseVerificationError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseVerificationError(f"{name} must be a JSON object")
    return value


def _absolute_file(value: Any, name: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise ReleaseVerificationError(f"{name} must be an absolute path")
    if not path.is_file():
        raise ReleaseVerificationError(f"{name} is not a regular file: {path}")
    return path.resolve()


def verify_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseVerificationError(f"cannot load manifest {path}: {exc}") from exc
    manifest = _object(manifest, "manifest")
    if manifest.get("schema_version") != 1:
        raise ReleaseVerificationError("manifest schema_version must be 1")
    release_id = str(manifest.get("release_id") or "").strip()
    if not release_id:
        raise ReleaseVerificationError("manifest release_id is required")
    approved_by = str(manifest.get("approved_by") or "").strip()
    approved_at = str(manifest.get("approved_at") or "").strip()
    if not approved_by or not approved_at:
        raise ReleaseVerificationError("approved_by and approved_at are required")

    root_raw = Path(str(manifest.get("release_root") or ""))
    if not root_raw.is_absolute():
        raise ReleaseVerificationError("release_root must be an absolute path")
    if root_raw.is_symlink():
        raise ReleaseVerificationError("release_root must not be a symlink")
    root = root_raw.resolve()
    if not root.is_dir():
        raise ReleaseVerificationError(f"release_root must be a real directory: {root}")
    if stat.S_IMODE(root.stat().st_mode) & 0o222:
        raise ReleaseVerificationError("release_root must have no write permission bits")

    files = _object(manifest.get("files"), "files")
    if not files:
        raise ReleaseVerificationError("files must not be empty")
    verified_files: dict[str, str] = {}
    for raw_name, raw_digest in sorted(files.items()):
        name = str(raw_name)
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            raise ReleaseVerificationError(f"invalid release-relative file: {name!r}")
        expected = str(raw_digest or "")
        if not SHA256_RE.fullmatch(expected):
            raise ReleaseVerificationError(f"invalid SHA-256 for {name}")
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise ReleaseVerificationError(f"release file must be regular and not a symlink: {name}")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ReleaseVerificationError(f"release file escapes root: {name}") from exc
        if stat.S_IMODE(candidate.stat().st_mode) & 0o222:
            raise ReleaseVerificationError(f"release file must have no write permission bits: {name}")
        actual = sha256_of(candidate)
        if actual != expected:
            raise ReleaseVerificationError(
                f"release file digest mismatch: {name} expected={expected} actual={actual}"
            )
        verified_files[name] = actual

    entrypoint = str(manifest.get("entrypoint") or "")
    if entrypoint not in verified_files:
        raise ReleaseVerificationError("entrypoint must be present in files")
    python = _absolute_file(manifest.get("python"), "python")
    config = _object(manifest.get("config"), "config")
    config_path = _absolute_file(config.get("path"), "config.path")
    config_sha = str(config.get("sha256") or "")
    if not SHA256_RE.fullmatch(config_sha):
        raise ReleaseVerificationError("config.sha256 must be a lowercase SHA-256")
    actual_config_sha = sha256_of(config_path)
    if actual_config_sha != config_sha:
        raise ReleaseVerificationError(
            f"config digest mismatch: expected={config_sha} actual={actual_config_sha}"
        )

    return {
        "schema_version": 1,
        "release_id": release_id,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "manifest_path": str(path.resolve()),
        "release_root": str(root),
        "entrypoint": str(root / entrypoint),
        "python": str(python),
        "config_path": str(config_path),
        "config_sha256": actual_config_sha,
        "files": verified_files,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--run",
        action="store_true",
        help="exec the verified entrypoint; otherwise verification only",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="arguments for the entrypoint; defaults to watch",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verified = verify_manifest(args.manifest)
    except ReleaseVerificationError as exc:
        print(f"RELEASE REJECTED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(verified, ensure_ascii=False, sort_keys=True))
    if not args.run:
        return 0
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        command = ["watch"]
    os.execv(
        verified["python"],
        [
            verified["python"], verified["entrypoint"],
            "--config", verified["config_path"], *command,
        ],
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
