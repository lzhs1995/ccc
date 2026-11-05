#!/usr/bin/env python3
"""Reviewed preparation only. Full compilation is restricted to GitHub macOS runners.

Requires a separately frozen overlay, exact release/test graphs, named new tests
and expected exported schemas. Never downloads or reuses historical test passes.
"""
import argparse
from collections import Counter
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET

UPSTREAM = "6aaac694803004a63c6f393d153c96d22521a6b9"
TREE = "d0caeb3c076fbeade281c18295fada090c52cf8f"
BASE_MANIFEST = "7d8017754da19290801c5f956a7700158987e2e9865db5eff1d802386ba02254"
BASE_APPLY = "7fc0b229ae1d7c318fae5513ca5459817f88bd415abf0623fdaf88862fde98d5"
BASE_PATCH = "9970ff7db887e69700aaa4c6bffd414a74c443a80dde31d373269a6bb18d42a8"
PACKAGES = ("codex-core", "codex-api", "codex-protocol", "codex-app-server-protocol",
            "codex-app-server", "codex-tui", "codex-goal-extension")
NATIVE_PACKAGE = "ccc-native-client-foreground"
ROLES = {"core": "codex-core", "api": "codex-app-server", "tui": "codex-tui",
         "protocol": "codex-app-server-protocol", "goal": "codex-goal-extension",
         "native_authority": "ccc-native-client-foreground"}


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def checked_path(root, name):
    relative = Path(name)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or ".git" in relative.parts or "target" in relative.parts):
        raise ValueError("unsafe source path: " + str(name))
    candidate = root / relative
    for parent in (candidate, *candidate.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("symlink in mutable source path: " + str(name))
    return candidate


def require_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("unfrozen SHA256")


def load_freeze(path, expected):
    require_sha(expected)
    if sha(path) != expected:
        raise ValueError("freeze manifest bytes changed")
    value = json.loads(path.read_bytes())
    if (value.get("schema") != "ccc-compact-recovery-build-v1"
            or value.get("status") != "FROZEN_FOR_ISOLATED_BUILD"
            or value.get("upstream_commit") != UPSTREAM
            or value.get("upstream_tree") != TREE):
        raise ValueError("candidate is not frozen against exact upstream")
    if not value.get("overlay") or not value.get("generated_schema_files"):
        raise ValueError("missing source or schema contract")
    for name, row in value["overlay"].items():
        checked_path(path.parent, name)
        if not name.startswith(("codex-rs/", "native/client-foreground/")):
            raise ValueError("overlay must be a Codex or exact native foreground source path")
        require_sha(row["after_sha256"])
        if row["before_sha256"] is not None:
            require_sha(row["before_sha256"])
        checked_path(path.parent, row["source"])
    for field in ("owner_review_sha256", "overlay_patch_sha256",
                  "full_release_source_manifest_sha256", "full_test_source_manifest_sha256",
                  "release_cargo_lock_sha256", "release_cargo_manifest_sha256",
                  "test_cargo_lock_sha256", "test_cargo_manifest_sha256",
                  "native_cargo_lock_sha256", "native_cargo_manifest_sha256"):
        require_sha(value[field])
    for name, pin in value["generated_schema_files"].items():
        checked_path(path.parent, name)
        require_sha(pin)
    if value.get("schema_export_argv") not in (
            ["app-server", "generate-json-schema", "--experimental"],
            ["app-server", "generate-json-schema"]):
        raise ValueError("unreviewed schema export command")
    cases = value.get("required_cases", [])
    seen = set()
    for row in cases:
        key = (row["package"], row["binary"], row["name"])
        if (row["package"] not in (*PACKAGES, NATIVE_PACKAGE) or key in seen or not row["name"]
                or any(x in row["name"] for x in "()'\"\n\r")):
            raise ValueError("invalid or duplicate required test")
        seen.add(key)
    for role, package in ROLES.items():
        if not any(x.get("role") == role and x["package"] == package and x.get("new") is True
                   for x in cases):
            raise ValueError("missing newly compiled " + role + " tests")
    if not any(x.get("role") == "foreground_regression" and x["package"] == "codex-tui"
               for x in cases):
        raise ValueError("missing actual foreground regression cases")
    return value


def run(argv, cwd, log=None):
    if log is None:
        return subprocess.run(argv, cwd=cwd, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
    with log.open("xb") as stream:
        result = subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); exact log: {log}")


def full_source(root):
    raw = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         cwd=root, check=True, stdout=subprocess.PIPE).stdout
    rows = []
    for name in sorted(set(os.fsdecode(x) for x in raw.split(b"\0") if x)):
        path = root / name
        if path.is_symlink():
            rows.append({"path": name, "kind": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            rows.append({"path": name, "kind": "file", "sha256": sha(path),
                         "executable": bool(path.stat().st_mode & 0o111)})
        else:
            raise ValueError("tracked source disappeared: " + name)
    return {"files": rows, "sha256": hashlib.sha256(canonical(rows)).hexdigest()}


def external_graph(raw):
    return sorted(canonical(p) for p in tomllib.loads(raw.decode())["package"] if "source" in p)


def require_native_dependency_subset(native_raw, workspace_raw):
    def versions(raw):
        return {(p["name"], p["version"], p["source"], p.get("checksum"))
                for p in tomllib.loads(raw.decode())["package"] if "source" in p}
    if not versions(native_raw) <= versions(workspace_raw):
        raise ValueError("native standalone graph introduces unfrozen external versions")


def run_native_tests(root, output, manifest):
    native = root / "native/client-foreground"
    for name, key in (("Cargo.toml", "native_cargo_manifest_sha256"),
                      ("Cargo.lock", "native_cargo_lock_sha256")):
        if sha(native / name) != manifest[key]:
            raise ValueError("standalone native graph changed")
    require_native_dependency_subset((native / "Cargo.lock").read_bytes(),
                                     (root / "codex-rs/Cargo.lock").read_bytes())
    config = output / "native-nextest.toml"
    # nextest 0.9.103 stores reports relative to its workspace, independently
    # of Cargo --target-dir. Bind the report to this absolute evidence path.
    junit = output / "native-junit-current.xml"
    config.write_text('[profile.default]\nretries = 0\n[profile.default.junit]\npath = '
                      + json.dumps(str(junit)) + "\n")
    target = output / "native-target"
    # This is a path dependency outside codex-rs workspace membership. Select
    # its actual manifest and preserve a separate JUnit result.
    prefix = ["cargo", "nextest", "run", "--manifest-path", str(native / "Cargo.toml"),
              "--target-dir", str(target), "--locked", "--no-fail-fast", "--retries", "0", "--config-file", str(config)]
    run([*prefix, "--lib"], native, output / "native-libraries.log")
    shutil.copyfile(junit, output / "native-libraries.xml")
    all_tests = check_junit(output / "native-libraries.xml")
    cases = [r for r in manifest["required_cases"] if r["package"] == NATIVE_PACKAGE]
    run([*prefix, "-E", exact_filter(cases)], native, output / "native-required.log")
    shutil.copyfile(junit, output / "native-required.xml")
    required = check_junit(output / "native-required.xml", [r["name"] for r in cases])
    run(["cargo", "test", "--locked", "--doc", "--target-dir", str(target), "--manifest-path", str(native / "Cargo.toml")],
        native, output / "native-doctests.log")
    return {"all_libraries": all_tests, "required_cases": required,
            "cargo_lock_sha256": sha(native / "Cargo.lock")}


def check_junit(path, required_names=()):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    if (not cases or any(c.find(tag) is not None for c in cases for tag in ("failure", "error", "skipped"))
            or any(int(n.get(k, "0")) for n in [root, *root.findall(".//testsuite")]
                   for k in ("failures", "errors", "skipped", "disabled"))):
        raise ValueError("test cases missing, skipped, or failed")
    names = [x.get("name") for x in cases]
    if required_names and Counter(required_names) != Counter(names):
        raise ValueError("new required test not actually executed: " + repr(set(required_names) - set(names)))
    return {"cases": len(cases), "names_sha256": hashlib.sha256(canonical(sorted(names))).hexdigest(),
            "junit_sha256": sha(path)}


def exact_filter(cases):
    return " | ".join(f'(package(={x["package"]}) & binary(={x["binary"]}) & test(={x["name"]}))'
                      for x in cases)


def require_github_runner():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "macOS"
            or os.environ.get("GITHUB_REPOSITORY") != "lzhs1995/ccc"
            or platform.machine() != "arm64"):
        raise ValueError("full builds are permitted only on the explicit GitHub macOS arm64 runner")


V8_SOURCE_PINS = {
    "scripts/codex_package/__init__.py": "e2140b37ed4d66a3758079a0961ee4d7dc01328f892d2b53a98582427bce341b",
    "scripts/codex_package/targets.py": "aa0b4f6652b27b512ffe759c9e2609909e90ea076716625b5a941e52f38ff2ed",
    "scripts/codex_package/v8.py": "a6ec2a955af2e99e8823aeaf8ea079aa8af170f7758fd639dfec7cc7bf91f238",
    "third_party/v8/rusty_v8_152_2_0_release_manifests.sha256": "1dd7c7f7d3d91af7f06a2846bddc6a1da7725acc67ecd7ab2c7dfc980dd8c6d4",
}


def prepare_v8_artifacts(root, output, expected_source):
    """Use the exact official verifier, with cache writes outside frozen source."""
    overrides = sorted(k for k in os.environ if k.startswith("RUSTY_V8_")
                       or k in ("V8_FROM_SOURCE", "V8_FORCE_DEBUG"))
    if overrides:
        raise ValueError("unreviewed V8 overrides: " + ", ".join(overrides))
    if full_source(root) != expected_source:
        raise ValueError("assembled source changed before V8 artifact preparation")
    source_pins = {name: sha(checked_path(root, name)) for name in V8_SOURCE_PINS}
    if source_pins != V8_SOURCE_PINS:
        raise ValueError("official V8 fetcher or trust root changed")
    if any(n == "codex_package" or n.startswith("codex_package.") for n in sys.modules):
        raise ValueError("unreviewed preloaded package helper")
    cache = output / "v8-artifacts"
    if cache.resolve().is_relative_to(root.resolve()) or cache.exists() or cache.is_symlink():
        raise ValueError("V8 artifacts require a fresh cache outside assembled source")
    cache.mkdir()
    old_path, old_bytecode = sys.path[:], sys.dont_write_bytecode
    old_repo = os.environ.get("CODEX_REPO_ROOT")
    try:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root / "scripts"))
        os.environ["CODEX_REPO_ROOT"] = str(root)
        targets = importlib.import_module("codex_package.targets")
        helper = importlib.import_module("codex_package.v8")
        for module, name in ((targets, "targets.py"), (helper, "v8.py")):
            if Path(module.__file__).resolve() != root / "scripts/codex_package" / name:
                raise ValueError("V8 helper import escaped verified source")
        version = helper.resolved_v8_crate_version()
        if version != "152.2.0":
            raise ValueError("V8 version differs from the frozen original lock")
        spec = targets.TARGET_SPECS["aarch64-apple-darwin"]
        # Do not call resolve_codex_v8_cargo_env: its override fast path is not proof.
        pair = helper.fetch_codex_v8_artifacts(spec, cache_root=cache)
        profile = "ptrcomp_sandbox_release_aarch64-apple-darwin"
        directory = cache / ("rusty-v8-" + version + "-" + spec.target)
        checksums = directory / ("rusty_v8_" + profile + ".sha256")
        expected_paths = [directory / ("librusty_v8_" + profile + ".a.gz"),
                          directory / ("src_binding_" + profile + ".rs")]
        if [pair.archive, pair.binding] != expected_paths:
            raise ValueError("official V8 pair paths differ")
        for path in [checksums, *expected_paths]:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(cache.resolve()):
                raise ValueError("V8 artifact path escaped private cache")
        helper.verify_release_checksum_manifest(checksums, version=version, remove_invalid=False)
        expected = helper.load_checksums(checksums, {p.name for p in expected_paths})
        if any(sha(path) != expected[path.name] for path in expected_paths):
            raise ValueError("downloaded V8 pair differs from official manifest")
        if full_source(root) != expected_source:
            raise ValueError("V8 preparation modified frozen source")
        base_url = "https://github.com/openai/codex/releases/download/rusty-v8-v" + version
        receipt = {"schema": "ccc-official-v8-artifact-preparation-v1", "passed": True,
                   "version": version, "target": spec.target, "profile": "ptrcomp_sandbox_release",
                   "assembled_source_sha256": expected_source["sha256"],
                   "official_source_pins": source_pins,
                   "function": "codex_package.v8.fetch_codex_v8_artifacts",
                   "function_source_sha256": hashlib.sha256(inspect.getsource(helper.fetch_codex_v8_artifacts).encode()).hexdigest(),
                   "trusted_manifest_file": str(root / "third_party/v8/rusty_v8_152_2_0_release_manifests.sha256"),
                   "release_manifest": {"path": str(checksums), "sha256": sha(checksums),
                                        "bytes": checksums.stat().st_size, "url": base_url + "/" + checksums.name},
                   "artifacts": [{"path": str(p), "sha256": sha(p), "bytes": p.stat().st_size,
                                  "url": base_url + "/" + p.name} for p in expected_paths],
                   "cargo_lock_sha256": sha(root / "codex-rs/Cargo.lock"),
                   "source_build_requested": False, "external_graph_changed": False}
        shutil.copyfile(checksums, output / "V8-RELEASE-CHECKSUMS.log")
        write_json(output / "V8-ARTIFACTS.json", receipt)
    finally:
        sys.path[:] = old_path
        sys.dont_write_bytecode = old_bytecode
        if old_repo is None:
            os.environ.pop("CODEX_REPO_ROOT", None)
        else:
            os.environ["CODEX_REPO_ROOT"] = old_repo
        for name in list(sys.modules):
            if name == "codex_package" or name.startswith("codex_package."):
                del sys.modules[name]
    os.environ["RUSTY_V8_ARCHIVE"] = str(pair.archive)
    os.environ["RUSTY_V8_SRC_BINDING_PATH"] = str(pair.binding)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ccc", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    manifest = load_freeze(args.freeze, args.freeze_sha256)
    if args.validate_only:
        print(json.dumps({"manifest_valid": True, "compiled": False, "production_installed": False}))
        return
    require_github_runner()
    ccc, root, bundle = args.ccc.resolve(), args.codex.resolve(), args.freeze.resolve().parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        short_tmp = Path("/tmp") / ("ccr-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"])
        short_tmp.mkdir(exist_ok=False)
        os.environ["TMPDIR"] = str(short_tmp)
        base = ccc / "integrations/codex-foreground"
        if (sha(base / "manifest.json") != BASE_MANIFEST or sha(base / "apply.py") != BASE_APPLY
                or sha(base / "integration.patch") != BASE_PATCH):
            raise ValueError("foreground base changed")
        if (run(["git", "rev-parse", "HEAD"], root) != UPSTREAM
                or run(["git", "rev-parse", "HEAD^{tree}"], root) != TREE
                or run(["git", "status", "--porcelain"], root)):
            raise ValueError("clean exact upstream checkout required")
        for field in ("owner_review", "overlay_patch"):
            if sha(checked_path(bundle, manifest[field])) != manifest[field + "_sha256"]:
                raise ValueError(field + " changed")
        shutil.copyfile(args.freeze, output / "freeze.json")
        write_json(output / "UPSTREAM-SOURCE.json", full_source(root))
        run([sys.executable, str(base / "apply.py"), "--codex", str(root)], ccc,
            output / "base-application.log")
        original_lock = (root / "codex-rs/Cargo.lock").read_bytes()
        for name, row in manifest["overlay"].items():
            source, target = checked_path(bundle, row["source"]), checked_path(root, name)
            if sha(source) != row["after_sha256"]:
                raise ValueError("overlay source changed: " + name)
            before = row["before_sha256"]
            if (before is None and target.exists()) or (before is not None and sha(target) != before):
                raise ValueError("overlay preimage changed: " + name)
        run(["git", "apply", "--check", str(checked_path(bundle, manifest["overlay_patch"]))], root)
        run(["git", "apply", str(checked_path(bundle, manifest["overlay_patch"]))], root)
        for name, row in manifest["overlay"].items():
            if sha(checked_path(root, name)) != row["after_sha256"]:
                raise ValueError("patch result differs from frozen overlay: " + name)
        release_source = full_source(root)
        if release_source["sha256"] != manifest["full_release_source_manifest_sha256"]:
            raise ValueError("complete release source differs")
        write_json(output / "RELEASE-SOURCE.json", release_source)
        workspace = root / "codex-rs"
        cargo_manifest, cargo_lock = workspace / "Cargo.toml", workspace / "Cargo.lock"
        release_manifest, release_lock = cargo_manifest.read_bytes(), cargo_lock.read_bytes()
        for path, key in ((cargo_manifest, "release_cargo_manifest_sha256"),
                          (cargo_lock, "release_cargo_lock_sha256")):
            if sha(path) != manifest[key]:
                raise ValueError("release graph changed")
        if external_graph(original_lock) != external_graph(release_lock):
            raise ValueError("external dependencies changed")
        if "CARGO_TARGET_DIR" in os.environ:
            raise ValueError("unreviewed global target overrides JUnit and isolated cleanup paths")
        run(["cargo", "fmt", "--", "--config", "imports_granularity=Item", "--check"], workspace,
            output / "format.log")
        run(["cargo", "fmt", "--manifest-path", str(root / "native/client-foreground/Cargo.toml"),
             "--", "--check"], root, output / "native-format.log")
        native_results = run_native_tests(root, output, manifest)
        if full_source(root)["sha256"] != release_source["sha256"]:
            raise ValueError("native tests modified frozen source or lock")
        marker = b'[workspace.package]\nversion = "0.164.0"'
        if release_manifest.count(marker) != 1:
            raise ValueError("release package version marker changed")
        cargo_manifest.write_bytes(release_manifest.replace(marker, b'[workspace.package]\nversion = "0.0.0"', 1))
        run(["cargo", "update", "--workspace"], workspace, output / "test-lock-update.log")
        if (sha(cargo_lock) != manifest["test_cargo_lock_sha256"]
                or sha(cargo_manifest) != manifest["test_cargo_manifest_sha256"]
                or external_graph(cargo_lock.read_bytes()) != external_graph(release_lock)):
            raise ValueError("test version changed dependency graph beyond frozen local versions")
        test_source = full_source(root)
        if test_source["sha256"] != manifest["full_test_source_manifest_sha256"]:
            raise ValueError("complete test source differs")
        write_json(output / "TEST-SOURCE.json", test_source)
        shutil.copyfile(cargo_lock, output / "test-Cargo.lock")
        package_args = [part for p in PACKAGES for part in ("-p", p)]
        workspace_cases = [r for r in manifest["required_cases"] if r["package"] != NATIVE_PACKAGE]
        prepare_v8_artifacts(root, output, test_source)
        # These unit suites launch real auxiliary executables. --lib does not build them.
        run(["cargo", "build", "--locked", "-p", "codex-rmcp-client", "--bin", "test_stdio_server",
             "-p", "codex-code-mode-host", "--bin", "codex-code-mode-host"], workspace,
            output / "build-test-helpers.log")
        helper_pins = {}
        for name in ("test_stdio_server", "codex-code-mode-host"):
            binary = workspace / "target/debug" / name
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise ValueError("required actual test helper missing: " + name)
            os.environ["CARGO_BIN_EXE_" + name] = str(binary)
            helper_pins[name] = {"path": str(binary), "sha256": sha(binary)}
        write_json(output / "TEST-HELPERS.json", helper_pins)
        run(["cargo", "nextest", "run", "--locked", "--no-run", *package_args], workspace,
            output / "compile-affected-test-targets.log")
        junit = workspace / "target/nextest/default/junit.xml"
        try:
            run(["cargo", "nextest", "run", "--locked", "--no-fail-fast", "--retries", "0", "--lib", *package_args], workspace,
                output / "affected-libraries.log")
        finally:
            # Preserve actual failed JUnit as well; command failure still propagates.
            if junit.is_file():
                shutil.copyfile(junit, output / "affected-libraries.xml")
        all_libraries = check_junit(output / "affected-libraries.xml")
        run(["cargo", "nextest", "run", "--locked", "--no-fail-fast", "--retries", "0", *package_args,
             "-E", exact_filter(workspace_cases)], workspace, output / "new-and-foreground-tests.log")
        shutil.copyfile(junit, output / "new-and-foreground-tests.xml")
        specific = check_junit(output / "new-and-foreground-tests.xml",
                               [r["name"] for r in workspace_cases])
        if full_source(root)["sha256"] != test_source["sha256"]:
            raise ValueError("tests modified source or snapshots")
        cargo_manifest.write_bytes(release_manifest)
        cargo_lock.write_bytes(release_lock)
        run(["cargo", "nextest", "run", "--locked", "--no-fail-fast", "--retries", "0", *package_args,
             "-E", exact_filter(workspace_cases)], workspace, output / "release-key-tests.log")
        shutil.copyfile(junit, output / "release-key-tests.xml")
        release_cases = check_junit(output / "release-key-tests.xml",
                                    [r["name"] for r in workspace_cases])
        if full_source(root)["sha256"] != release_source["sha256"]:
            raise ValueError("release tests changed frozen sources")
        write_json(output / "FRESH-TEST-RESULT.json", {"passed": True, "freeze_sha256": args.freeze_sha256,
                   "all_affected_libraries": all_libraries, "required_cases": specific,
                   "release_required_cases": release_cases, "standalone_native": native_results,
                   "historical_passes_reused": False})
        # This is the isolated CI checkout only; never clean a shared local target.
        run(["cargo", "clean"], workspace, output / "ci-test-target-clean.log")
        run(["cargo", "build", "--locked", "--release", "-p", "codex-cli", "--bin", "codex",
             "--target", "aarch64-apple-darwin"], workspace, output / "release-build.log")
        if full_source(root)["sha256"] != release_source["sha256"]:
            raise ValueError("release build modified pinned sources")
        package = output / "package"
        package.mkdir()
        binary = package / "codex-aarch64-apple-darwin"
        shutil.copy2(workspace / "target/aarch64-apple-darwin/release/codex", binary)
        version = run([str(binary), "--version"], output)
        if version != "codex-cli 0.164.0":
            raise ValueError("binary version differs")
        schema = output / "generated-schema"
        run([str(binary), *manifest["schema_export_argv"], "--out", str(schema)], output,
            output / "schema-export.log")
        schema_files = {str(p.relative_to(schema)): sha(p) for p in schema.rglob("*") if p.is_file()}
        if schema_files != manifest["generated_schema_files"]:
            raise ValueError("actual binary schema differs from frozen contract")
        write_json(output / "ACTUAL-SCHEMA.json", schema_files)
        for name in ("freeze.json", "RELEASE-SOURCE.json", "FRESH-TEST-RESULT.json", "ACTUAL-SCHEMA.json"):
            shutil.copyfile(output / name, package / name)
        build = {"schema": "ccc-compact-recovery-artifact-v1", "freeze_sha256": args.freeze_sha256,
                 "upstream_commit": UPSTREAM, "upstream_tree": TREE,
                 "base_patch_sha256": BASE_PATCH, "overlay_patch_sha256": manifest["overlay_patch_sha256"],
                 "complete_release_source_sha256": release_source["sha256"],
                 "cargo_lock_sha256": sha(cargo_lock), "binary_sha256": sha(binary),
                 "binary_bytes": binary.stat().st_size, "version": version,
                 "rustc": run(["rustc", "--version"], workspace),
                 "target": "aarch64-apple-darwin", "actions_run_id": os.environ["GITHUB_RUN_ID"],
                 "actions_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "ccc_commit": os.environ["GITHUB_SHA"],
                 "fresh_test_result_sha256": sha(output / "FRESH-TEST-RESULT.json"),
                 "actual_schema_manifest_sha256": sha(output / "ACTUAL-SCHEMA.json"),
                 "v8_artifacts_receipt_sha256": sha(output / "V8-ARTIFACTS.json"),
                 "compiled_and_tested": True, "real_180_300_fixture_passed": False,
                 "production_installed": False}
        write_json(package / "BUILD-MANIFEST.json", build)
        write_json(output / "RESULT.json", {"passed": True, "build_manifest_sha256": sha(package / "BUILD-MANIFEST.json")})
    except BaseException as exc:
        write_json(output / "FAILURE.json", {"type": type(exc).__name__, "error": str(exc),
                   "at_epoch": time.time(), "production_installed": False})
        raise


if __name__ == "__main__":
    main()
