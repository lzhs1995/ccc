"""Authenticate the pinned official Nightly on an isolated GitHub Mac; never launch it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request

REPOSITORY = "manaflow-ai/cmux"
ASSET_ID = 713343428
ASSET_NAME = "cmux-nightly-macos-arm64-4200579208601.dmg"
ASSET_BYTES = 81411044
ASSET_SHA = "1c14b5b4d85de19966efbc4b261dbde7e9c4a34d857722cee846ef5d41d48252"
BUILD_RUN = 42005792086
BUILD_ATTEMPT = 1
SOURCE_HEAD = "2e109a1739d320fe36220afb3e7775b59c1acd47"
BUILD_VERSION = "4200579208601"
TEAM = "7WLXT3NR37"
BUNDLE = "com.cmuxterm.app.nightly"
ASSET_URL = f"https://github.com/{REPOSITORY}/releases/download/nightly/{ASSET_NAME}"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    # Must match private_factory.canonical used by the original GUI loader.
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return digest(raw)


def api(endpoint, destination):
    result = subprocess.run(["gh", "api", endpoint], check=True, capture_output=True, timeout=45)
    with destination.open("xb") as stream:
        stream.write(result.stdout)
    return json.loads(result.stdout)


def validate_metadata(asset, run):
    require(asset.get("id") == ASSET_ID and asset.get("name") == ASSET_NAME,
            "official immutable asset identity changed")
    require(asset.get("state") == "uploaded" and asset.get("size") == ASSET_BYTES
            and asset.get("digest") == "sha256:" + ASSET_SHA
            and asset.get("browser_download_url") == ASSET_URL, "official asset content contract changed")
    require(run.get("id") == BUILD_RUN and run.get("run_attempt") == BUILD_ATTEMPT
            and run.get("head_sha") == SOURCE_HEAD and run.get("head_branch") == "main"
            and run.get("path") == ".github/workflows/nightly.yml"
            and run.get("event") == "push" and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("repository", {}).get("full_name") == REPOSITORY,
            "original official build is not the pinned successful run")


def validate_plist(info):
    require(info.get("CFBundleIdentifier") == BUNDLE, "wrong application bundle identity")
    require(info.get("CFBundleVersion") == BUILD_VERSION, "wrong actual application build")
    require(info.get("CMUXCommit") == SOURCE_HEAD[:7], "signed application source marker differs")
    require(info.get("CFBundleExecutable") == "cmux", "unexpected application executable")


def inventory(app):
    result = {}
    for item in app.rglob("*"):
        name = item.relative_to(app).as_posix()
        if item.is_symlink():
            require(item.resolve(strict=True).is_relative_to(app), "app symlink escapes its bundle")
            result[name] = {"symlink": os.readlink(item)}
        elif item.is_file():
            require(item.stat().st_nlink == 1, "application file is hardlinked")
            result[name] = {"sha256": file_sha(item), "mode": stat.S_IMODE(item.stat().st_mode)}
        else:
            require(item.is_dir(), "special application member")
    return result


def download(destination):
    # The immutable URL is public. No authorization header is forwarded to a CDN.
    request = urllib.request.Request(ASSET_URL, headers={"User-Agent": "ccc-nightly-admission"})
    sha = hashlib.sha256()
    count = 0
    deadline = time.monotonic() + 180
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as stream:
        require(urllib.parse.urlsplit(response.url).scheme == "https", "download left HTTPS")
        while block := response.read(1024 * 1024):
            require(time.monotonic() <= deadline, "bounded download deadline exceeded")
            count += len(block)
            require(count <= ASSET_BYTES, "asset exceeded its pinned size")
            sha.update(block)
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())
    require(count == ASSET_BYTES and sha.hexdigest() == ASSET_SHA, "download content failed exact SHA/size")


def admit(root):
    require(sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true",
            "only the isolated GitHub macOS runner is authorized")
    require(os.environ.get("GITHUB_REPOSITORY") == "lzhs1995/ccc"
            and re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", ""))
            and re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", "")),
            "require the original repository and actual GitHub run/attempt")
    runner = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    requested = Path(root)
    require(not requested.is_symlink(), "output root is a symlink")
    root = requested.resolve()
    require(root != runner and root.is_relative_to(runner) and not root.exists(),
            "output must be a fresh directory under actual RUNNER_TEMP")
    root.mkdir(mode=0o700)
    available = os.statvfs(root)
    require(available.f_bavail * available.f_frsize >= 4 * 1024**3, "insufficient isolated runner reserve")
    asset = api(f"repos/{REPOSITORY}/releases/assets/{ASSET_ID}", root / "ASSET.json")
    run = api(f"repos/{REPOSITORY}/actions/runs/{BUILD_RUN}/attempts/{BUILD_ATTEMPT}", root / "BUILD-RUN.json")
    validate_metadata(asset, run)
    save(root / "DOWNLOAD-INTENT.json", {"asset_id": ASSET_ID, "sha256": ASSET_SHA,
        "size": ASSET_BYTES, "source_head": SOURCE_HEAD, "launch_authorized_by_this_script": False})
    dmg = root / ASSET_NAME
    download(dmg)
    mount = root / "mount"
    mount.mkdir(mode=0o700)
    app = root / "cmux.app"
    attached = False
    try:
        attached_result = subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
            "-noautoopen", "-mountpoint", str(mount), "-plist", str(dmg)],
            check=True, capture_output=True, timeout=90)
        attached = True
        (root / "MOUNT.plist").write_bytes(attached_result.stdout)
        require(any(x.get("mount-point") == str(mount)
                    for x in plistlib.loads(attached_result.stdout).get("system-entities", [])),
                "unexpected actual DMG mount")
        apps = list(mount.glob("*.app"))
        require(len(apps) == 1 and not apps[0].is_symlink(), "expected one actual app in official DMG")
        subprocess.run(["/usr/bin/ditto", str(apps[0]), str(app)], check=True, capture_output=True, timeout=90)
    finally:
        # A failed attach may still have mounted; inspect the exact requested path.
        if attached or os.path.ismount(mount):
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True, capture_output=True, timeout=30)
    require(not os.path.ismount(mount), "private DMG remained mounted")
    app = app.resolve(strict=True)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    validate_plist(info)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                   check=True, capture_output=True, timeout=60)
    signed = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
                            check=True, capture_output=True, timeout=15)
    signed_raw = signed.stdout + signed.stderr
    (root / "CODESIGN.txt").write_bytes(signed_raw)
    text = signed_raw.decode()
    require(re.findall(r"^TeamIdentifier=(.+)$", text, re.M) == [TEAM], "unexpected signing team")
    require("Authority=Developer ID Application:" in text, "missing Developer ID authority")
    entries = inventory(app)
    save(root / "APP-INVENTORY.json", entries)
    exe = app / "Contents/MacOS/cmux"
    cli = app / "Contents/Resources/bin/cmux"
    for item in [exe, cli]:
        require(item.is_file() and not item.is_symlink() and os.access(item, os.X_OK),
                "missing actual executable or CLI")
    proof = {"schema": "official-nightly-artifact-admission-v1", "passed": True,
        "artifact_id": ASSET_ID, "artifact_sha256": ASSET_SHA, "artifact_bytes": ASSET_BYTES,
        "source_repository": REPOSITORY, "exact_source_head": SOURCE_HEAD,
        "admission_repository": os.environ["GITHUB_REPOSITORY"],
        "admission_run": int(os.environ["GITHUB_RUN_ID"]),
        "admission_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "original_build_run": BUILD_RUN, "original_build_attempt": BUILD_ATTEMPT,
        "app": str(app), "app_inventory_sha256": digest(canonical(entries)),
        "application_sha256": file_sha(exe), "cli_sha256": file_sha(cli),
        "team_identifier": TEAM, "bundle_identifier": BUNDLE, "build_version": BUILD_VERSION,
        "signed_commit_marker": info["CMUXCommit"], "actual_mount_detached": True,
        "source_provenance_scope": "Official immutable release SHA and successful build plus signed seven-character source marker; no reproducible-build claim.",
        "app_launched": False, "production_installation": False,
        "evidence": {name: file_sha(root / name) for name in ["ASSET.json", "BUILD-RUN.json", "CODESIGN.txt", "APP-INVENTORY.json"]}}
    proof_sha = save(root / "ARTIFACT-PROOF.json", proof)
    contract = {"schema": "original-private-nightly-artifact-contract-v1", "status": "ARTIFACT_AUTHENTICATED_GUI_NOT_STARTED",
        "artifact_sha256": ASSET_SHA, "artifact_proof_sha256": proof_sha,
        **{key: proof[key] for key in ["app_inventory_sha256", "application_sha256", "cli_sha256", "team_identifier", "source_repository", "exact_source_head"]}}
    save(root / "GUI-ARTIFACT-CONTRACT.json", contract)
    print(json.dumps({"app": str(app), "proof": str(root / "ARTIFACT-PROOF.json"),
                      "contract": str(root / "GUI-ARTIFACT-CONTRACT.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    admit(parser.parse_args().output)
