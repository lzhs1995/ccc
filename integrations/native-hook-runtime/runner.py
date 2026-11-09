"""One isolated automatic-hook fixture after exact successful-artifact admission.

No build, installation, supplier request, existing controller or business input.
The contract is pending until Root fills actual reviewed artifact/source pins.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixture"))
import artifact_io as io
import fixture
import fixture_common as common

REPOSITORY = "lzhs1995/ccc"
BUILD_RUN = 42561656291
BUILD_HEAD = "b977911d8596c311b70d43a0c8b9c920aa2f4fd1"
BUILD_BRANCH = "ccc/native-hook-caller-20261109-r1"
RUNTIME_BRANCH = "ccc/native-hook-runtime-20261109-r1"
FIXTURE_MANIFEST = "cde07456c55ad4fae54cbbd0aae2681dc88cace70c77b96c05c06212ddc1dee4"
FIXTURE_REVIEW = "0c82f56290ca813f1ba2919043074b3a6978d95f4630ffec6197c0bcbf9fc71d"
SHARED = {"REQUEST.json", "DELTA-SOURCE.json", "EFFECTIVE-SOURCE.json", "FORMAT-RESULT.json",
          "RUSTFMT.patch", "DELTA-TEST-RESULT.json", "ACTUAL-SCHEMA.json", "RESULT.json"}
REQUIRED_EVIDENCE = SHARED | {"workspace-tests.xml", "native-tests.xml", "CHECK-COMMAND.json",
                             "BUILD-COMMAND.json", "check-changed-targets.log", "release-build.log"}
SOURCE_PATHS = {"integrations/native-spawn-readonly/ci.py",
                "integrations/native-spawn-readonly/REQUEST.json",
                "integrations/native-spawn-readonly/native-spawn.patch",
                ".github/workflows/codex-native-spawn.yml"}
PUBLIC_SOURCE_PATHS = {"runner.py", "artifact_io.py", "EXTRACTION.json"} | {
    "fixture/" + name for name in ("fixture.py", "hook_probe.py", "native_query.py",
                                   "reader_acceptance.py", "fixture_common.py", "CONTRACT.json",
                                   "README.md", "EXTRACTION.json", "OFFICIAL-HOST-CONTRACT.json",
                                   "MANIFEST.json")}


def require_sha(value):
    io.need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "missing exact reviewed SHA256")


def validate_contract(c):
    """Pure checks only; unfilled contract never reaches any network call."""
    io.need(c.get("schema") == "native-auto-hook-single-runtime-contract-v1"
            and c.get("status") == "ROOT_REVIEWED_ACTUAL_BUILD", "pending actual build/artifact review")
    io.need(c.get("repository") == REPOSITORY and c.get("run_id") == BUILD_RUN
            and c.get("run_attempt") == 1 and c.get("head") == BUILD_HEAD
            and c.get("branch") == BUILD_BRANCH and c.get("build_conclusion") == "success",
            "not the designated successful automatic-hook build attempt")
    io.need(c.get("fixture_manifest_sha256") == FIXTURE_MANIFEST
            and c.get("fixture_source_review_sha256") == FIXTURE_REVIEW,
            "original fixture source/review changed")
    io.need(c.get("production_installation") is False and c.get("retries") == 0
            and c.get("runtime_attempt") == 1, "unreviewed runtime/install/retry scope")
    for key in ("root_artifact_review_sha256", "result_sha256", "effective_source_file_sha256",
                "effective_source_sha256", "binary_sha256"):
        require_sha(c.get(key))
    io.need(type(c.get("binary_bytes")) is int and 0 < c["binary_bytes"] <= io.MAX_ARCHIVE,
            "missing actual binary size")
    io.need(set(c.get("artifacts", {})) == {"evidence", "binary"}, "two exact artifacts required")
    for kind, row in c["artifacts"].items():
        expected_name = "native-caller-spawn-" + ("evidence" if kind == "evidence" else "arm64") + "-42561656291-1"
        io.need(type(row.get("id")) is int and row["id"] > 0
                and type(row.get("size_in_bytes")) is int and 0 < row["size_in_bytes"] <= io.MAX_DOWNLOAD
                and row.get("name") == expected_name, "missing exact artifact identity/size")
        io.need(isinstance(row.get("digest"), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"]), "missing actual artifact digest")
    for key, required in (("evidence_files", REQUIRED_EVIDENCE), ("source_files", SOURCE_PATHS),
                          ("runner_files", PUBLIC_SOURCE_PATHS)):
        rows = c.get(key, {})
        io.need(isinstance(rows, dict) and required <= set(rows), "missing reviewed members: " + key)
        if key != "evidence_files":
            io.need(set(rows) == required, "unexpected source members: " + key)
        io.need(len(rows) <= 64, "unbounded proof member list")
        for name, pin in rows.items():
            io.need(name and not Path(name).is_absolute() and ".." not in Path(name).parts
                    and "\\" not in name, "unsafe proof/source member")
            require_sha(pin)
    io.need(c["evidence_files"]["RESULT.json"] == c["result_sha256"]
            and c["evidence_files"]["EFFECTIVE-SOURCE.json"] == c["effective_source_file_sha256"]
            and c["evidence_files"]["REQUEST.json"] == fixture.BUILD_REQUEST
            and c["source_files"]["integrations/native-spawn-readonly/REQUEST.json"] == fixture.BUILD_REQUEST,
            "build proof pins are not linked")


def metadata(c):
    prefix = "repos/" + REPOSITORY
    run = io.gh_json(prefix + "/actions/runs/" + str(BUILD_RUN))
    jobs = io.gh_json(prefix + "/actions/runs/" + str(BUILD_RUN) + "/attempts/1/jobs?per_page=100")
    io.need(run["id"] == BUILD_RUN and run["head_sha"] == BUILD_HEAD and run["run_attempt"] == 1
            and run["repository"]["full_name"] == REPOSITORY
            and run["head_repository"]["full_name"] == REPOSITORY and run["head_branch"] == BUILD_BRANCH
            and run["event"] == "push" and run["path"].split("@")[0] == ".github/workflows/codex-native-spawn.yml"
            and run["status"] == "completed" and run["conclusion"] == "success", "actual build is not admissible")
    io.need(jobs["total_count"] == 1 and len(jobs["jobs"]) == 1, "unexpected build job graph")
    job = jobs["jobs"][0]
    io.need(job["name"] == "delta-macos" and job["head_sha"] == BUILD_HEAD and job["run_attempt"] == 1
            and job["status"] == "completed" and job["conclusion"] == "success", "build job did not pass")
    steps = {r["name"]: r for r in job["steps"]}
    for name in ("Verify and compile the reviewed new caller proof",
                 "Preserve original delta evidence including failures",
                 "Preserve changed candidate binary without installation"):
        io.need(steps.get(name, {}).get("conclusion") == "success", "required build/upload step missing: " + name)
    artifacts = {}
    for kind, expected in c["artifacts"].items():
        row = io.gh_json(prefix + "/actions/artifacts/" + str(expected["id"]))
        io.need(all(row.get(k) == expected[k] for k in ("id", "name", "size_in_bytes", "digest"))
                and row.get("expired") is False and row["workflow_run"]["id"] == BUILD_RUN
                and row["workflow_run"]["head_sha"] == BUILD_HEAD, "original artifact/API binding changed")
        artifacts[kind] = row
    sources = {}
    for name, expected in c["source_files"].items():
        row = io.gh_json(prefix + "/contents/" + name + "?ref=" + BUILD_HEAD)
        io.need(row.get("encoding") == "base64", "actual build source body unavailable")
        raw = base64.b64decode(row["content"])
        io.need(hashlib.sha256(raw).hexdigest() == expected, "actual source differs: " + name)
        sources[name] = {"blob_sha": row["sha"], "sha256": expected}
    return {"run": run, "jobs": jobs, "artifacts": artifacts, "sources": sources}


def admit(root, c):
    out = root / "a"
    out.mkdir(mode=0o700)
    live = metadata(c)
    io.save(out / "LIVE-METADATA.json", live)
    for kind in ("evidence", "binary"):
        archive = out / (kind + ".zip")
        io.download_artifact(root, live["artifacts"][kind], archive)
        expanded = io.validate_zip(archive, live["artifacts"][kind])
        io.capacity(root, expanded)
        io.extract_zip(archive, out / kind)
    evidence, package = out / "evidence", out / "binary"
    io.need(not (evidence / "FAILURE.json").exists(), "original build failure preserved; refuse runtime")
    for name, pin in c["evidence_files"].items():
        common.pinned(evidence / name, pin)
    io.need({str(p.relative_to(package)) for p in package.rglob("*") if p.is_file()}
            == SHARED | {"codex-aarch64-apple-darwin"}, "actual package file closure changed")
    for name in SHARED:
        io.need(common.strict_file(package / name) == common.strict_file(evidence / name),
                "package/evidence differ: " + name)
    build = json.loads(common.pinned(package / "RESULT.json", c["result_sha256"]))
    fixture.validate_build(build, str(BUILD_RUN), c["effective_source_sha256"])
    io.need(build.get("schema") == "ccc-native-spawn-delta-artifact-v1"
            and build.get("binary_sha256") == c["binary_sha256"] and build.get("binary_bytes") == c["binary_bytes"]
            and build.get("actual_schema_files") == 464 and build.get("official_combined_release") is False
            and build.get("live_root_hook_verified") is False, "new artifact summary differs")
    effective = json.loads(common.strict_file(evidence / "EFFECTIVE-SOURCE.json"))
    io.need(effective.get("sha256") == c["effective_source_sha256"], "reviewed effective source differs")
    request = json.loads(common.strict_file(evidence / "REQUEST.json"))
    cases = request["required_cases"]
    io.need(len(cases) == 24 and len({(r["package"], r["binary"], r["name"]) for r in cases}) == 24
            and request.get("selected_case_counts") == {"new": 8, "affected_prior": 16}
            and sum(r["package"] == "codex-hooks" for r in cases) == 5,
            "actual automatic-hook test case contract changed")
    native = [r for r in cases if r["package"] == "ccc-native-client-foreground"]
    workspace = [r for r in cases if r["package"] != "ccc-native-client-foreground"]
    junit = {"native": io.junit(evidence / "native-tests.xml", native),
             "workspace": io.junit(evidence / "workspace-tests.xml", workspace)}
    tests = json.loads(common.strict_file(evidence / "DELTA-TEST-RESULT.json"))
    io.need(tests.get("passed") is True and tests.get("request_sha256") == fixture.BUILD_REQUEST
            and tests.get("historical_r9_tests_rerun") is False and tests.get("runtime_client_adopted") is False,
            "original delta test proof differs")
    binary = package / "codex-aarch64-apple-darwin"
    raw = common.pinned(binary, c["binary_sha256"])
    io.need(len(raw) == c["binary_bytes"] and struct.unpack("<II", raw[:8]) == (0xFEEDFACF, 0x0100000C),
            "not the reviewed Mach-O arm64 binary")
    del raw
    binary.chmod(0o500)
    host = io.official_host(root, out)
    io.save(out / "ADMISSION.json", {"passed": True, "runner": io.current_runner(),
            "build_run": BUILD_RUN, "build_attempt": 1, "build_head": BUILD_HEAD,
            "result_sha256": c["result_sha256"], "effective_source_sha256": c["effective_source_sha256"],
            "binary_sha256": c["binary_sha256"], "original_junit_readback": junit,
            "tests_rerun": 0, "fixture_started": False, "production_installation": False})
    return binary, host, package / "RESULT.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    io.need(sys.version_info >= (3, 11) and sys.platform == "darwin" and platform.machine() == "arm64"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
            and os.environ.get("GITHUB_REF_NAME") == RUNTIME_BRANCH
            and os.environ.get("GITHUB_EVENT_NAME") == "push", "only original isolated macOS runtime attempt1")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    io.need(args.root == temporary / "ct", "use the fixed fresh short RUNNER_TEMP child")
    args.root.mkdir(mode=0o700, exist_ok=False)
    root = common.require_runner(args.root)
    started = False
    try:
        contract = json.loads(common.pinned(args.contract, args.contract_sha256))
        if contract.get("status") != "ROOT_REVIEWED_ACTUAL_BUILD":
            io.save(root / "PENDING.json", {"reason": "real success/artifact/source review not supplied",
                    "status": contract.get("status"), "fixture_started": False, "network_calls": 0})
            raise SystemExit(75)
        validate_contract(contract)
        for name, pin in contract["runner_files"].items():
            common.pinned(HERE / name, pin)
        io.need(len(os.fsencode(root / "f/n/credential-observations/compaction-recovery/c-99999999-12345678.sock")) < 104,
                "private root too long for original Unix socket")
        io.save(root / "ADMISSION-INTENT.json", {"runner": io.current_runner(),
                "contract_sha256": args.contract_sha256, "fixture_started": False})
        binary, host, result = admit(root, contract)
        target = root / "f"
        target.mkdir(mode=0o700)
        io.save(root / "RUN-INTENT.json", {"runner": io.current_runner(), "at_epoch": time.time(),
                "contract_sha256": args.contract_sha256, "fixture_manifest_sha256": FIXTURE_MANIFEST,
                "invocations": 1, "retries": 0, "fixture_only": True})
        started = True
        fixture.run(argparse.Namespace(root=target, binary=binary, host=host,
            build_result=result, build_result_sha256=contract["result_sha256"],
            build_run=str(BUILD_RUN), reviewed_source_sha256=contract["effective_source_sha256"]))
        actual = json.loads(common.strict_file(target / "RESULT.json"))
        io.need(actual.get("passed") is True and actual.get("actual_native_tool_roundtrips") == 2
                and actual.get("production_installed") is False
                and actual.get("live_root_hook_verified") is False
                and actual.get("isolated_automatic_root_and_child_hooks_verified") is True
                and actual.get("actual_automatic_hook_events") == 4
                and actual.get("cmux_host_verified") is False
                and actual.get("complete_inbox_or_bidirectional_receipt_verified") is False
                and actual.get("original_pure_reader_only") is True
                and actual.get("installed_hook_trust_or_loading_changed") is False,
                "isolated automatic-hook runtime result incomplete")
        io.save(root / "RESULT.json", {"passed": True, "runtime_result_sha256": io.sha(target / "RESULT.json"),
                "runtime_invocations": 1, "retries": 0, "tests_rerun": 0,
                "supplier_requests": 0, "production_operations": 0,
                "isolated_automatic_root_and_child_hooks_verified": True,
                "actual_automatic_hook_events": 4, "live_root_hook_verified": False,
                "cmux_host_verified": False,
                "complete_inbox_or_bidirectional_receipt_verified": False})
    except SystemExit:
        raise
    except BaseException as error:
        io.save(root / "FAILURE.json", {"type": type(error).__name__, "error": str(error),
                "fixture_invocation_started": started, "replay_allowed": False, "production_operations": 0})
        raise


if __name__ == "__main__":
    main()
