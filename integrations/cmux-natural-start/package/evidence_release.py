"""Export bounded evidence separately from a retained full GitHub app artifact.

All original command streams are copied losslessly (large streams as gzip),
with original and export hashes. App/old-artifact ZIPs and build directories are
never placed in the small evidence artifact. This does not dispatch a workflow.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

import source_contract as shared

need, save, digest = shared.need, shared.save, shared.digest
MAX_EVIDENCE_BYTES = 64 * 1024**2
MAX_STREAM_BYTES = 1024**3
MAX_FILES = 4096
MAX_MEMBER_BYTES = 16 * 1024**2


def evidence_candidates(root):
    root = Path(root)
    selected = set()
    for directory in (root, root / "build", root / "package", root / "runtime", root / "old-cli"):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.suffix in (".json", ".plist", ".stderr"):
                    selected.add(path)
    for directory in (root / "build/commands", root / "package/commands", root / "old-cli/commands",
                      root / "preflight-aqua-commands", root / "runtime-aqua-commands", root / "artifact-binding-commands"):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.suffix in (".json", ".stdout", ".stderr"):
                    selected.add(path)
    need(len(selected) <= MAX_FILES, "too many evidence members")
    result = []
    for path in sorted(selected):
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and path.resolve(strict=True) == path
             and path.resolve(strict=True).is_relative_to(root), "unsafe evidence member: " + str(path))
        need(info.st_size <= (MAX_STREAM_BYTES if path.suffix in (".stdout", ".stderr") else MAX_MEMBER_BYTES),
             "original evidence member exceeds bound: " + str(path))
        result.append(path)
    return result


def export_member(source, root, output):
    relative = source.relative_to(root)
    compressed = source.suffix in (".stdout", ".stderr") and source.stat().st_size > 4096
    target = output / relative
    if compressed:
        target = target.with_name(target.name + ".gz")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = source.stat()
    original_sha = hashlib.sha256()
    original_bytes = 0
    with source.open("rb") as reader, target.open("xb") as raw_writer:
        writer = gzip.GzipFile(fileobj=raw_writer, filename="", mode="wb", mtime=0) if compressed else raw_writer
        try:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                original_sha.update(chunk)
                original_bytes += len(chunk)
                need(original_bytes <= MAX_STREAM_BYTES, "original stream exceeded bound while exporting")
                writer.write(chunk)
        finally:
            if compressed:
                writer.close()
        after = os.fstat(reader.fileno())
    need((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) ==
         (after.st_dev, after.st_ino, after.st_mtime_ns, original_bytes), "original changed during export")
    return {"original": relative.as_posix(), "original_sha256": original_sha.hexdigest(),
            "original_bytes": original_bytes, "exported": target.relative_to(output).as_posix(),
            "sha256": digest(target), "bytes": target.stat().st_size,
            "encoding": "gzip" if compressed else "identity", "lossless": True}


def bind_app_artifact(root, app_id, app_digest, app_url):
    """Bind upload-action outputs to this exact API artifact and run attempt."""
    context = shared.run_identity()
    receipt = {"schema": "cmux-local-release-app-artifact-binding-v1", "run": context,
               "final_run_conclusion_required": True, "business_adoption_ready": False}
    if not app_id:
        need(not app_digest and not app_url, "partial artifact upload outputs are UNKNOWN")
        receipt.update(status="NO_APP_ARTIFACT_UPLOAD_OBSERVED", app_artifact=None)
        save(root / "APP-ARTIFACT-BINDING.json", receipt)
        return receipt
    need(app_id.isdigit() and int(app_id) > 0, "invalid uploaded app artifact id")
    app_digest = app_digest.removeprefix("sha256:")
    need(re.fullmatch("[0-9a-f]{64}", app_digest), "missing uploaded artifact digest")
    expected_url = ("https://github.com/" + context["repository"] + "/actions/runs/" + str(context["run_id"])
                    + "/artifacts/" + app_id)
    need(app_url == expected_url, "uploaded app artifact URL belongs to another run")
    pin_path = root / "package/PACKAGE-PIN.json"
    pin = shared.read_json(pin_path)
    archive = root / "package" / pin["archive"]["name"]
    need(pin.get("run") == context and pin.get("archive", {}).get("name") == "cmux-release.zip"
         and archive.stat().st_size == pin["archive"]["bytes"] and digest(archive) == pin["archive"]["sha256"],
         "uploaded inner app archive no longer matches the package pin")
    commands = shared.Commands(root / "artifact-binding-commands")
    artifact_raw = commands.text(["gh", "api", "--method", "GET", "repos/" + context["repository"]
                                  + "/actions/artifacts/" + app_id], timeout=35)
    artifact = json.loads(artifact_raw)
    save(root / "APP-ARTIFACT-API.json", artifact)
    expected_name = "cmux-release-app-" + str(context["run_id"]) + "-" + str(context["run_attempt"])
    need(artifact.get("id") == int(app_id) and artifact.get("expired") is False
         and artifact.get("digest") == "sha256:" + app_digest and artifact.get("name") == expected_name
         and artifact.get("workflow_run", {}).get("id") == context["run_id"]
         and artifact.get("workflow_run", {}).get("head_sha") == context["head_sha"],
         "artifact API does not confirm this upload, digest and source head")
    run_raw = commands.text(["gh", "api", "--method", "GET", "repos/" + context["repository"]
        + "/actions/runs/" + str(context["run_id"]) + "/attempts/" + str(context["run_attempt"])], timeout=35)
    run = json.loads(run_raw)
    save(root / "RUN-ATTEMPT-API.json", run)
    need(run.get("id") == context["run_id"] and run.get("run_attempt") == context["run_attempt"]
         and run.get("head_sha") == context["head_sha"]
         and run.get("repository", {}).get("full_name") == context["repository"], "API run attempt differs")
    receipt.update(status="UPLOAD_BOUND_FINAL_RUN_CONCLUSION_NOT_YET_OBSERVED", app_artifact={
        "id": int(app_id), "sha256": app_digest, "url": app_url, "name": expected_name,
        "bytes": artifact.get("size_in_bytes"), "metadata_sha256": digest(root / "APP-ARTIFACT-API.json")},
        inner_archive=pin["archive"], package_pin_sha256=digest(pin_path),
        package_manifest_sha256=digest(root / "package/PACKAGE-MANIFEST.json"),
        run_attempt_metadata_sha256=digest(root / "RUN-ATTEMPT-API.json"))
    save(root / "APP-ARTIFACT-BINDING.json", receipt)
    return receipt


def check_app_upload(root):
    shared.original_admission().runner_only()
    root = Path(root).resolve(strict=True)
    need(root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)), "foreign attempt")
    pin_path = root / "package/PACKAGE-PIN.json"
    ready = False
    if pin_path.is_file():
        import release_package
        import release_paired
        expected_sha = shared.read_json(root / "package/PACKAGE-PIN-SHA256.json")["sha256"]
        pin, _ = release_paired.read_pin(pin_path, expected_sha, root / "package/PACKAGE-MANIFEST.json")
        need(pin.get("run") == shared.run_identity() and pin["archive"]["name"] == "cmux-release.zip",
             "upload pin belongs to another build")
        archive = root / "package/cmux-release.zip"
        need(archive.stat().st_size == pin["archive"]["bytes"] and digest(archive) == pin["archive"]["sha256"],
             "full App changed before upload")
        ready = True
    save(root / "UPLOAD-READY.json", {"ready": ready, "run": shared.run_identity(),
                                      "runtime_passed_is_separate": True})
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write("ready=" + ("true" if ready else "false") + "\n")
    return ready


def verify_exported(output, rows):
    total = 0
    for row in rows:
        path = output / row["exported"]
        need(path.is_file() and not path.is_symlink() and path.resolve(strict=True).is_relative_to(output),
             "missing or unsafe exported evidence")
        need(path.stat().st_size == row["bytes"] and digest(path) == row["sha256"], "exported evidence changed")
        total += path.stat().st_size
        need(total <= MAX_EVIDENCE_BYTES, "small evidence exceeds the 64 MiB ceiling")
        with (gzip.open(path, "rb") if row["encoding"] == "gzip" else path.open("rb")) as stream:
            original_sha = hashlib.sha256(); original_bytes = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                original_sha.update(chunk); original_bytes += len(chunk)
                need(original_bytes <= MAX_STREAM_BYTES, "evidence expansion exceeds bound")
        need(original_bytes == row["original_bytes"] and original_sha.hexdigest() == row["original_sha256"],
             "lossless original command evidence readback differs")
    return total


def export(root, output, app_id="", app_digest="", app_url=""):
    shared.original_admission().runner_only()
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    root = Path(root).resolve(strict=True)
    need(root.is_relative_to(temporary), "original attempt is not in runner temp")
    output = shared.fresh_runner_output(output)
    error = None
    try:
        binding = bind_app_artifact(root, app_id, app_digest, app_url)
    except BaseException as exc:
        error = exc
        binding = {"status": "UNKNOWN", "error": repr(exc), "app_id_observed": app_id or None,
                   "app_digest_observed": app_digest or None, "automatic_retry": False}
        save(root / "APP-ARTIFACT-BINDING-UNKNOWN.json", binding)
    rows = []
    try:
        candidates = evidence_candidates(root)
        for path in candidates:
            rows.append(export_member(path, root, output))
            need(sum(row["bytes"] for row in rows) <= MAX_EVIDENCE_BYTES - 2 * 1024**2,
                 "small evidence budget exceeded; original logs stay on this run, no completeness claim")
        source_dir = output / "source-pins"
        source_dir.mkdir(mode=0o700)
        sources = []
        for path in sorted(shared.ROOT.glob("*.py")):
            sources.append({"file": path.name, "sha256": digest(path), "bytes": path.stat().st_size})
        save(source_dir / "IMPLEMENTATION-PINS.json", sources)
        for name in ("APP-PIN.json", "BUILD-RUN.json", "ARTIFACT.json", "FIXED-TEST-EXPECTATIONS.json", "PROVENANCE.json"):
            shutil.copyfile(shared.PRE / name, source_dir / ("ORIGINAL-" + name))
        for name in ("SOURCE-MANIFEST.json", "FROZEN.json"):
            path = shared.ROOT / name
            if path.is_file():
                shutil.copyfile(path, source_dir / name)
        total = verify_exported(output, rows)
        ci_result = shared.read_json(root / "RESULT.json") if (root / "RESULT.json").is_file() else None
        if ci_result is not None:
            need(ci_result.get("passed") is True and binding.get("app_artifact") is not None,
                 "successful new package has no verified full app artifact")
        result = {"schema": "cmux-local-release-evidence-export-v1", "passed": error is None,
            "run": shared.run_identity(), "evidence_bytes_without_export_manifest": total,
            "max_evidence_bytes": MAX_EVIDENCE_BYTES, "source_members": rows,
            "app_artifact_binding": binding, "ci_acceptance_observed": ci_result is not None,
            "ci_result_sha256": digest(root / "RESULT.json") if ci_result is not None else None,
            "full_app_in_evidence": False, "original_command_streams_lossless": True,
            "final_external_run_check_required": True, "business_adoption_ready": False}
        save(output / "EXPORT.json", result)
        inventory = [{"path": p.relative_to(output).as_posix(), "sha256": digest(p), "bytes": p.stat().st_size}
                     for p in sorted(output.rglob("*")) if p.is_file()]
        save(output / "EVIDENCE-MANIFEST.json", {"files": inventory, "run": shared.run_identity(),
            "scope": "Every export member except this manifest and its SHA receipt"})
        save(output / "EVIDENCE-MANIFEST-SHA256.json", {"sha256": digest(output / "EVIDENCE-MANIFEST.json")})
        need(sum(p.stat().st_size for p in output.rglob("*") if p.is_file()) <= MAX_EVIDENCE_BYTES,
             "final small evidence including manifests exceeds 64 MiB")
    except BaseException as exc:
        error = error or exc
        save(output / "EXPORT-UNKNOWN.json", {"error": repr(exc), "passed": False, "complete": False,
                                             "original_evidence_retained": str(root), "retries": 0})
    if error is not None:
        raise error
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-app-upload", action="store_true")
    args = parser.parse_args()
    if args.check_app_upload:
        need(args.output is None, "upload admission does not export evidence")
        print(json.dumps({"ready": check_app_upload(args.attempt)}))
        return
    need(args.output is not None, "evidence output is required")
    result = export(args.attempt, args.output, os.environ.get("RELEASE_APP_ARTIFACT_ID", ""),
                    os.environ.get("RELEASE_APP_ARTIFACT_DIGEST", ""), os.environ.get("RELEASE_APP_ARTIFACT_URL", ""))
    print(json.dumps({"passed": result["passed"], "full_app_in_evidence": False,
                      "ci_acceptance_observed": result["ci_acceptance_observed"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
