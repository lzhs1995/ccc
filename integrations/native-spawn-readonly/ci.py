#!/usr/bin/env python3
"""Validate only the new native spawn proof on the original frozen R9 source.

Builds run on the authorized GitHub macOS runner. Historical R9 tests remain
historical evidence; this program neither reruns that workflow nor installs a
client. The original source assembler, V8 verifier and JUnit verifier are reused.
"""
import argparse
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


R9_HEAD = "996025e9a2527ed9a1f03049e60c40c2ab94c145"
BASE_HEAD = "a0f7fa7216673009375405ca99ca5c0a4b8a997f"
CI_SHA = "240bd37340e38204d0eba52d00353e322f9b63ced1eb730e98270ea13d205729"
FREEZE_SHA = "ac04b248df2eee7ae8f73a6f873b7053610fa2bc639c0779729cac344f2db194"
R9_SOURCE_SHA = "9fc26034e72e4b99aa6576a706491d298e0c7eda8750368f0de391c75587084b"
NATIVE = "ccc-native-client-foreground"
ALLOWED_PACKAGES = {"codex-utils-pty", "codex-exec-server", "codex-core", "codex-tui", NATIVE}


def need(value, reason):
    if not value:
        raise ValueError(reason)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def original_ci(bundle):
    path = bundle / "ci.py"
    need(sha(path) == CI_SHA, "original R9 assembler changed")
    spec = importlib.util.spec_from_file_location("original_r9_ci", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_request(path, expected, old):
    old.require_sha(expected)
    need(sha(path) == expected, "delta request changed")
    request = json.loads(path.read_bytes())
    need(request.get("schema") == "ccc-native-spawn-delta-build-v1"
         and request.get("status") == "FROZEN_FOR_DELTA_BUILD", "delta is not frozen")
    need(request.get("r9_head") == R9_HEAD and request.get("base_head") == BASE_HEAD
         and request.get("r9_freeze_sha256") == FREEZE_SHA
         and request.get("r9_release_source_sha256") == R9_SOURCE_SHA,
         "original source chain changed")
    need(request.get("old_atomic_tests_rerun") is False
         and request.get("production_installation") is False, "incorrect validation scope")
    for key in ("patch_sha256", "expected_release_source_sha256", "independent_review_sha256"):
        old.require_sha(request[key])
    files = request.get("files", [])
    need(files and len(files) <= 32 and len({r["path"] for r in files}) == len(files),
         "invalid delta members")
    for row in files:
        name = row["path"]
        old.checked_path(path.parent, name)
        need(name.startswith(("codex-rs/", "native/client-foreground/")) and name.endswith(".rs"),
             "delta outside reviewed Rust source")
        old.require_sha(row["after_sha256"])
        if row["before_sha256"] is not None:
            old.require_sha(row["before_sha256"])
    cases = request.get("required_cases", [])
    need(cases and len(cases) <= 64, "missing or unbounded delta tests")
    seen = set()
    for row in cases:
        key = (row["package"], row["binary"], row["name"])
        need(row["package"] in ALLOWED_PACKAGES and key not in seen, "invalid test target")
        need(all(isinstance(s, str) and s and all(c.isalnum() or c in "_:-" for c in s)
                 for s in key), "unsafe exact test name")
        seen.add(key)
    need(any(r["package"] == NATIVE for r in cases)
         and any(r["package"] == "codex-core" for r in cases), "incomplete proof test scope")
    return request


def assemble_original(old, base_repo, root, bundle, output):
    """The original R9 assembly steps, without its historical test/build stages."""
    manifest = old.load_freeze(bundle / "freeze.json", FREEZE_SHA)
    old.require_goal_merge_contract(manifest)
    base = base_repo / "integrations/codex-foreground"
    need(old.run(["git", "rev-parse", "HEAD"], base_repo) == BASE_HEAD, "base checkout changed")
    need(sha(base / "manifest.json") == old.BASE_MANIFEST
         and sha(base / "apply.py") == old.BASE_APPLY
         and sha(base / "integration.patch") == old.BASE_PATCH, "foreground base changed")
    need(old.run(["git", "rev-parse", "HEAD"], root) == old.UPSTREAM
         and old.run(["git", "rev-parse", "HEAD^{tree}"], root) == old.TREE
         and not old.run(["git", "status", "--porcelain"], root), "upstream is not exact and clean")
    for field in ("owner_review", "overlay_patch"):
        need(sha(old.checked_path(bundle, manifest[field])) == manifest[field + "_sha256"],
             "original R9 " + field + " changed")
    old.run([sys.executable, str(base / "apply.py"), "--codex", str(root)], base_repo,
            output / "base-application.log")
    for name, row in manifest["overlay"].items():
        source = old.checked_path(bundle, row["source"])
        target = old.checked_path(root, name)
        need(sha(source) == row["after_sha256"], "R9 source changed: " + name)
        before = row["before_sha256"]
        need((not target.exists()) if before is None else sha(target) == before,
             "R9 preimage changed: " + name)
    patch = old.checked_path(bundle, manifest["overlay_patch"])
    old.run(["git", "apply", "--check", str(patch)], root)
    old.run(["git", "apply", str(patch)], root)
    for name, row in manifest["overlay"].items():
        need(sha(old.checked_path(root, name)) == row["after_sha256"], "R9 patch result differs")
    source = old.full_source(root)
    need(source["sha256"] == R9_SOURCE_SHA == manifest["full_release_source_manifest_sha256"],
         "complete R9 source differs")
    old.write_json(output / "ORIGINAL-R9-SOURCE.json", source)
    return manifest


def apply_delta(old, root, directory, request, output):
    patch = directory / "native-spawn.patch"
    need(sha(patch) == request["patch_sha256"], "delta patch bytes changed")
    for row in request["files"]:
        target = old.checked_path(root, row["path"])
        before = row["before_sha256"]
        need((not target.exists()) if before is None else sha(target) == before,
             "delta preimage differs: " + row["path"])
    old.run(["git", "apply", "--check", str(patch)], root)
    old.run(["git", "apply", str(patch)], root)
    for row in request["files"]:
        need(sha(old.checked_path(root, row["path"])) == row["after_sha256"],
             "delta result differs: " + row["path"])
    source = old.full_source(root)
    need(source["sha256"] == request["expected_release_source_sha256"],
         "complete delta source differs")
    old.write_json(output / "DELTA-SOURCE.json", source)
    return source


def format_delta(old, root, output, request, source):
    """Pin the fixed formatter transformation before any compilation.

    Local preparation has no Rust toolchain. The runner may format only the
    reviewed changed members; its exact diff remains subject to final review.
    """
    paths = {row["path"] for row in request["files"]}
    before = {name: old.checked_path(root, name).read_bytes() for name in paths}
    version = old.run(["rustfmt", "--version"], root)
    work = output / "format-stdin-work"
    work.mkdir()
    native_config = work / "native-rustfmt.toml"
    native_config.write_text('edition = "2021"\n')
    records = []

    def format_one(label, raw, native):
        config = native_config if native else root / "codex-rs/rustfmt.toml"
        edition = "2021" if native else "2024"
        # No filename is given to rustfmt. Its Input::Text path does not
        # recurse through module files. An empty cwd and real preflight below
        # verify this property on the exact installed 1.99.1 formatter.
        command = ["rustfmt", "--emit", "stdout", "--edition", edition,
                   "--config-path", str(config)]
        result = subprocess.run(command, cwd=work, input=raw, capture_output=True, timeout=30)
        records.append({"member": label, "argv": command, "cwd": str(work),
                        "input_sha256": hashlib.sha256(raw).hexdigest(),
                        "output_sha256": hashlib.sha256(result.stdout).hexdigest(),
                        "returncode": result.returncode, "stderr": result.stderr.decode(errors="replace")})
        old.write_json(output / "FORMAT-COMMANDS.json", records)
        need(result.returncode == 0 and 0 < len(result.stdout) <= 8 * 1024 * 1024,
             "isolated stdin formatter failed: " + label)
        result.stdout.decode("utf-8", errors="strict")
        return result.stdout

    probe = b'mod ccc_nonexistent_formatter_child; fn main() {}\n'
    for native in (False, True):
        format_one("nonrecursive-stdin-preflight-" + str(native), probe, native)
    need(set(p.name for p in work.iterdir()) == {native_config.name}, "formatter wrote unexpected files")
    for name in sorted(paths):
        native = name.startswith("native/")
        formatted = format_one(name, before[name], native)
        need(format_one(name + "-idempotence", formatted, native) == formatted,
             "formatter result not stable: " + name)
        old.checked_path(root, name).write_bytes(formatted)
    effective = old.full_source(root)
    original = {row["path"]: row for row in source["files"]}
    actual = {row["path"]: row for row in effective["files"]}
    need(set(original) == set(actual), "formatter added or removed source members")
    changed = sorted(name for name in original if original[name] != actual[name])
    need(set(changed) <= paths, "formatter changed source outside the reviewed delta")
    diffs = []
    for name in changed:
        need(original[name]["kind"] == actual[name]["kind"] == "file"
             and original[name]["executable"] == actual[name]["executable"],
             "formatter changed file type or permissions")
        after = old.checked_path(root, name).read_bytes()
        diffs.extend(difflib.unified_diff(before[name].decode().splitlines(keepends=True),
                                         after.decode().splitlines(keepends=True),
                                         fromfile="a/" + name, tofile="b/" + name))
    patch = output / "RUSTFMT.patch"
    patch.write_text("".join(diffs))
    old.write_json(output / "FORMAT-RESULT.json", {
        "formatter": version, "reviewed_input_source_sha256": source["sha256"],
        "effective_source_sha256": effective["sha256"], "changed_members": changed,
        "format_patch_sha256": sha(patch), "outside_delta_changes": 0,
        "format_delta_independent_review_complete": False})
    old.write_json(output / "EFFECTIVE-SOURCE.json", effective)
    return effective


def execute_tests(old, root, output, request):
    results = {}
    for label, native in (("native", True), ("workspace", False)):
        cases = [r for r in request["required_cases"] if (r["package"] == NATIVE) == native]
        need(cases, "empty test partition")
        cwd = root / ("native/client-foreground" if native else "codex-rs")
        junit = output / (label + "-tests.xml")
        config = output / (label + "-nextest.toml")
        config.write_text('[profile.default]\nretries = 0\n[profile.default.junit]\npath = '
                          + json.dumps(str(junit)) + "\n")
        command = ["cargo", "nextest", "run", "--locked", "--lib", "--no-fail-fast",
                   "--retries", "0", "--config-file", str(config)]
        if native:
            command += ["--manifest-path", str(cwd / "Cargo.toml"),
                        "--target-dir", str(output / "native-target")]
        else:
            for package in sorted({r["package"] for r in cases}):
                command += ["-p", package]
        command += ["-E", old.exact_filter(cases)]
        old.write_json(output / (label + "-command.json"), {"argv": command, "cwd": str(cwd)})
        old.run(command, cwd, output / (label + "-tests.log"))
        results[label] = old.check_junit(junit, [r["name"] for r in cases])
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ccc", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--codex", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    ccc, base_repo, root = args.ccc.resolve(), args.base.resolve(), args.codex.resolve()
    bundle = ccc / "integrations/compact-recovery-build"
    old = original_ci(bundle)
    request = load_request(args.request, args.request_sha256, old)
    old.require_github_runner()
    need(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "hosted runner required")
    need(os.environ.get("GITHUB_REF_NAME") == request["branch"], "unexpected branch")
    need(os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "rerunning original attempts is forbidden")
    need(old.run(["git", "rev-parse", "HEAD"], ccc) == os.environ["GITHUB_SHA"], "CI checkout changed")
    need(old.run(["git", "rev-parse", "HEAD^"], ccc) == R9_HEAD, "publication parent changed")
    need("CARGO_TARGET_DIR" not in os.environ, "unreviewed target directory override")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        short = Path("/tmp") / ("ns-" + os.environ["GITHUB_RUN_ID"] + "-1")
        short.mkdir(exist_ok=False)
        os.environ["TMPDIR"] = str(short)
        shutil.copyfile(args.request, output / "REQUEST.json")
        manifest = assemble_original(old, base_repo, root, bundle, output)
        source = apply_delta(old, root, args.request.resolve().parent, request, output)
        workspace = root / "codex-rs"
        for path, key in ((workspace / "Cargo.toml", "release_cargo_manifest_sha256"),
                          (workspace / "Cargo.lock", "release_cargo_lock_sha256"),
                          (root / "native/client-foreground/Cargo.toml", "native_cargo_manifest_sha256"),
                          (root / "native/client-foreground/Cargo.lock", "native_cargo_lock_sha256")):
            need(sha(path) == manifest[key], "original dependency graph changed")
        source = format_delta(old, root, output, request, source)
        old.prepare_v8_artifacts(root, output, source)
        check_command = ["cargo", "check", "--locked", "--all-targets", "-p", "codex-utils-pty",
                         "-p", "codex-exec-server", "-p", "codex-core", "-p", "codex-tui"]
        old.write_json(output / "CHECK-COMMAND.json", {"argv": check_command, "cwd": str(workspace)})
        old.run(check_command, workspace, output / "check-changed-targets.log")
        results = execute_tests(old, root, output, request)
        need(old.full_source(root) == source, "tests modified frozen source")
        old.write_json(output / "DELTA-TEST-RESULT.json", {
            "passed": True, "results": results, "request_sha256": args.request_sha256,
            "historical_r9_tests_rerun": False, "runtime_client_adopted": False})
        build_command = ["cargo", "build", "--locked", "--release", "-p", "codex-cli",
                         "--bin", "codex", "--target", "aarch64-apple-darwin"]
        old.write_json(output / "BUILD-COMMAND.json", {"argv": build_command, "cwd": str(workspace)})
        old.run(build_command, workspace, output / "release-build.log")
        need(old.full_source(root) == source, "build modified frozen source")
        package = output / "package"
        package.mkdir()
        binary = package / "codex-aarch64-apple-darwin"
        shutil.copy2(workspace / "target/aarch64-apple-darwin/release/codex", binary)
        need(old.run([str(binary), "--version"], output) == "codex-cli 0.164.0", "version changed")
        schema = output / "generated-schema"
        old.run([str(binary), *manifest["schema_export_argv"], "--out", str(schema)], output,
                output / "schema-export.log")
        schemas = {str(p.relative_to(schema)): sha(p) for p in schema.rglob("*") if p.is_file()}
        need(schemas == manifest["generated_schema_files"], "unreviewed protocol/schema change")
        old.write_json(output / "ACTUAL-SCHEMA.json", schemas)
        result = {"schema": "ccc-native-spawn-delta-artifact-v1", "passed": True,
                  "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": 1,
                  "head": os.environ["GITHUB_SHA"], "request_sha256": args.request_sha256,
                  "source_sha256": source["sha256"], "binary_sha256": sha(binary),
                  "binary_bytes": binary.stat().st_size, "new_tests": results,
                  "old_atomic_tests_rerun": False, "actual_schema_files": len(schemas),
                  "production_installed": False, "official_combined_release": False,
                  "live_root_hook_verified": False, "format_delta_review_complete": False}
        old.write_json(output / "RESULT.json", result)
        for name in ("REQUEST.json", "DELTA-SOURCE.json", "EFFECTIVE-SOURCE.json",
                     "FORMAT-RESULT.json", "RUSTFMT.patch", "DELTA-TEST-RESULT.json",
                     "ACTUAL-SCHEMA.json", "RESULT.json"):
            shutil.copyfile(output / name, package / name)
    except BaseException as error:
        old.write_json(output / "FAILURE.json", {"type": type(error).__name__, "error": str(error),
                       "at_epoch": time.time(), "production_installed": False})
        raise


if __name__ == "__main__":
    main()
