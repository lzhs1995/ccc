#!/usr/bin/env python3
"""Isolated source bootstrap only. Never installs or certifies native behavior."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "reference"))
from ci import (BASE_APPLY, BASE_MANIFEST, BASE_PATCH, TREE, UPSTREAM,
                canonical, checked_path, external_graph, full_source, sha, write_json)
from schema_bootstrap import GENERATOR, check_generator_log

PACKAGES = ("codex-protocol", "codex-state", "codex-core", "codex-goal-extension",
            "codex-app-server-protocol", "codex-app-server")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(path, expected):
    require(sha(path) == expected, "bootstrap manifest SHA changed")
    value = json.loads(path.read_bytes())
    require(value["schema"] == "ccc-goal-receipt-bootstrap-v1"
            and value["status"] == "FROZEN_SOURCE_FOR_FORMAT_SCHEMA_CHECK_ONLY"
            and value["upstream_commit"] == UPSTREAM
            and value["upstream_tree"] == TREE and value["generator"] == GENERATOR,
            "wrong bootstrap scope")
    for name, digest in value["input_files"].items():
        require(sha(checked_path(path.parent, name)) == digest, "changed input: " + name)
    members = json.loads((path.parent / "goal-members.json").read_bytes())["members"]
    require(len(members) == value["goal_members"] == 18, "incomplete Goal overlay")
    require(len({m["path"] for m in members}) == 18, "duplicate Goal source path")
    for member in members:
        require(member["path"].startswith("codex-rs/"), "out-of-scope Goal source")
        require(sha(checked_path(path.parent / "goal-source", member["path"]))
                == member["after_sha256"], "Goal source changed")
    original = (path.parent / "r7/release-Cargo.lock").read_bytes()
    for name in ("goal-source/codex-rs/Cargo.lock", "goal-test-Cargo.lock"):
        require(external_graph((path.parent / name).read_bytes()) == external_graph(original),
                "external dependency graph changed")
    return value, members


def source_delta(before, after):
    a = {r["path"]: r for r in before["files"]}
    b = {r["path"]: r for r in after["files"]}
    return {"added": sorted(b.keys() - a.keys()), "removed": sorted(a.keys() - b.keys()),
            "changed": sorted(k for k in a.keys() & b.keys() if a[k] != b[k])}


def check_format_scope(before, after, allowed):
    delta = source_delta(before, after)
    require(not delta["added"] and not delta["removed"]
            and set(delta["changed"]) <= set(allowed), "formatter changed unrelated source")
    return delta


def run(argv, cwd, log, *, env=None, allow_failure=False):
    with log.open("xb") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    write_json(log.with_suffix(".command.json"), {
        "argv": argv, "cwd": str(cwd), "exit_code": result.returncode,
        "log_sha256": sha(log)})
    if result.returncode and not allow_failure:
        raise RuntimeError(f"command failed ({result.returncode}): {log.name}")
    return result.returncode


def schema_inventory(root):
    return {p.relative_to(root).as_posix(): sha(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


def prepare_review_material(original, generated, output):
    old, new = schema_inventory(original), schema_inventory(generated)
    changed = sorted(k for k in old.keys() & new.keys() if old[k] != new[k])
    details = []
    diff_lines = []
    for name in changed:
        row = {"path": name, "before_sha256": old[name], "after_sha256": new[name]}
        if name.endswith((".json", ".ts")):
            a, b = (original / name).read_text(), (generated / name).read_text()
            if name.endswith(".json"):
                row["json_semantics_equal"] = json.loads(a) == json.loads(b)
            diff_lines.extend(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                                 fromfile="r7-schema/" + name,
                                                 tofile="goal-schema/" + name))
        else:
            row["binary_exports_require_independent_content_review"] = True
        details.append(row)
    write_json(output / "SCHEMA-REVIEW-MATERIAL.json", {
        "original": old, "generated": new, "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()), "changed": details,
        "compatibility_reviewed": False, "compatibility_passed": False,
        "note": "Actual generator bytes only. Review every changed original API; no name-based normalization or old-fixture substitution."})
    (output / "SCHEMA-CANDIDATE.diff").write_text("".join(diff_lines))
    return new


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ccc-base", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=HERE / "bootstrap.json")
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    value, members = validate(args.manifest, args.manifest_sha256)
    if args.validate_only:
        print(json.dumps({"input_pins_valid": True, "compiled": False, "published": False}))
        return
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_OS") == "macOS"
            and platform.system() == "Darwin", "compilation restricted to GitHub macOS runner")
    root, output, bundle = args.codex.resolve(), args.output.resolve(), args.manifest.resolve().parent
    require(root != output and root not in output.parents, "output must be outside source")
    output.mkdir(parents=True, exist_ok=False)
    phase = "initialize"
    env = os.environ.copy()
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
        require(not env.get(key), "unreviewed compiler override: " + key)
    env.update(CARGO_TARGET_DIR=str(output / "target"), CARGO_TERM_COLOR="never",
               CODEX_APP_SERVER_SCHEMA_ROOT=str(output / "generated-schema"))
    env.pop("CODEX_APP_SERVER_SCHEMA_PRETTIER", None)
    try:
        def git(*argv):
            return subprocess.check_output(["git", *argv], cwd=root, text=True).strip()
        require(git("rev-parse", "HEAD") == UPSTREAM and git("rev-parse", "HEAD^{tree}") == TREE
                and not git("status", "--porcelain"), "not fresh pinned upstream checkout")
        base = args.ccc_base.resolve() / "integrations/codex-foreground"
        require(sha(base / "manifest.json") == BASE_MANIFEST
                and sha(base / "apply.py") == BASE_APPLY
                and sha(base / "integration.patch") == BASE_PATCH, "original foreground base changed")
        phase = "apply-original-base"
        run([sys.executable, str(base / "apply.py"), "--codex", str(root)], args.ccc_base.resolve(),
            output / "base-application.log", env=env)
        phase = "apply-r7"
        freeze = json.loads((bundle / "r7/freeze.json").read_bytes())
        for name, pin in freeze["overlay"].items():
            target = checked_path(root, name)
            require((not target.exists() if pin["before_sha256"] is None else sha(target) == pin["before_sha256"]),
                    "r7 preimage changed: " + name)
        for action in (["--check"], []):
            run(["git", "apply", *action, str(bundle / "r7/native-compact-recovery.patch")], root,
                output / ("r7-apply-check.log" if action else "r7-apply.log"), env=env)
        r7 = full_source(root)
        require(r7["sha256"] == freeze["full_release_source_manifest_sha256"], "complete r7 source differs")
        write_json(output / "R7-SOURCE.json", r7)
        schema_root = root / "codex-rs/app-server-protocol/schema"
        shutil.copytree(schema_root, output / "original-r7-schema")
        original_schema_pins = schema_inventory(schema_root)
        phase = "apply-goal"
        for member in members:
            target = checked_path(root, member["path"])
            require((not target.exists() if member["before_sha256"] is None else sha(target) == member["before_sha256"]),
                    "Goal preimage changed: " + member["path"])
        for action in (["--check"], []):
            run(["git", "apply", *action, str(bundle / "goal-receipt.patch")], root,
                output / ("goal-apply-check.log" if action else "goal-apply.log"), env=env)
        before = full_source(root)
        require(before["sha256"] == value["source_sha256"], "complete unformatted Goal source differs")
        write_json(output / "UNFORMATTED-SOURCE.json", before)
        rust_files = [m["path"] for m in members if m["path"].endswith(".rs")]
        originals = {name: (root / name).read_text() for name in rust_files}
        workspace = root / "codex-rs"
        phase = "format"
        run(["rustc", "--version", "--verbose"], workspace, output / "rustc-version.log", env=env)
        run(["rustfmt", "--version"], workspace, output / "rustfmt-version.log", env=env)
        command = ["rustfmt", "--edition", "2024", "--config", "imports_granularity=Item,skip_children=true"]
        run([*command, *[str(root / name) for name in rust_files]], workspace, output / "format-write.log", env=env)
        after = full_source(root)
        delta = check_format_scope(before, after, rust_files)
        write_json(output / "FORMAT-CHANGES.json", delta)
        diff = []
        for name in rust_files:
            actual = (root / name).read_text()
            diff.extend(difflib.unified_diff(originals[name].splitlines(True), actual.splitlines(True),
                                             fromfile="before/" + name, tofile="after/" + name))
            destination = output / "formatted-goal-source" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(actual)
        (output / "FORMAT-CANDIDATE.patch").write_text("".join(diff))
        run([*command, "--check", *[str(root / name) for name in rust_files]], workspace,
            output / "format-check.log", env=env)
        write_json(output / "FORMATTED-RELEASE-SOURCE.json", after)
        phase = "release-locked-metadata"
        require(sha(workspace / "Cargo.lock") == value["release_lock_sha256"], "release lock changed")
        run(["cargo", "metadata", "--locked", "--format-version", "1"], workspace,
            output / "release-metadata.log", env=env)
        require(full_source(root)["sha256"] == after["sha256"], "release metadata changed source/lock")
        phase = "generate-schema"
        command = ["cargo", "test", "--locked", "-p", "codex-app-server-protocol", "--lib",
                   GENERATOR, "--", "--ignored", "--exact", "--nocapture"]
        for flag, name in (("0", "stable"), ("1", "experimental")):
            env["CODEX_APP_SERVER_SCHEMA_EXPERIMENTAL"] = flag
            log = output / (name + "-generator.log")
            run(command, workspace, log, env=env)
            check_generator_log(log)
            require(full_source(root)["sha256"] == after["sha256"], "generator changed source/lock")
        require(schema_inventory(schema_root) == original_schema_pins, "generator changed original fixtures")
        generated = prepare_review_material(output / "original-r7-schema", output / "generated-schema", output)
        require(all("precomputed/app-server-exports-" + name + ".json.zst" in generated
                    for name in ("stable", "experimental")), "missing real generated exports")
        json_content = "\n".join(p.read_text() for p in (output / "generated-schema/json").rglob("*.json"))
        for method in ("thread/goal/recover", "thread/goal/recovery/read"):
            require(method in json_content, "new method missing from actual generated schema: " + method)
        phase = "release-check"
        package_args = [x for name in PACKAGES for x in ("-p", name)]
        run(["cargo", "check", "--locked", *package_args, "--all-targets"], workspace,
            output / "release-check.log", env=env)
        require(full_source(root)["sha256"] == after["sha256"], "release check changed source/lock")
        phase = "test-lock"
        release_manifest = (workspace / "Cargo.toml").read_bytes()
        marker = b'[workspace.package]\nversion = "0.164.0"'
        require(release_manifest.count(marker) == 1, "release version marker changed")
        (workspace / "Cargo.toml").write_bytes(release_manifest.replace(marker, b'[workspace.package]\nversion = "0.0.0"', 1))
        run(["cargo", "update", "--workspace", "--offline"], workspace,
            output / "actual-test-lock-update.log", env=env)
        require(sha(workspace / "Cargo.toml") == sha(bundle / "r7/test-Cargo.toml")
                and sha(workspace / "Cargo.lock") == value["test_lock_sha256"], "actual Cargo test graph differs from pinned local-edge prediction")
        require(external_graph((workspace / "Cargo.lock").read_bytes())
                == external_graph((bundle / "r7/release-Cargo.lock").read_bytes()), "external graph changed")
        shutil.copyfile(workspace / "Cargo.lock", output / "ACTUAL-TEST-Cargo.lock")
        test_source = full_source(root)
        write_json(output / "TEST-SOURCE.json", test_source)
        phase = "test-check"
        run(["cargo", "metadata", "--locked", "--offline", "--format-version", "1"], workspace,
            output / "test-metadata.log", env=env)
        run(["cargo", "check", "--locked", "--offline", *package_args, "--all-targets"], workspace,
            output / "test-check.log", env=env)
        require(full_source(root)["sha256"] == test_source["sha256"], "test check changed source/lock")
        write_json(output / "RESULT.json", {
            "at_epoch": time.time(), "bootstrap_passed": True, "format_checked": True,
            "schema_generation_passed": True, "release_and_test_cargo_check_passed": True,
            "schema_compatibility_reviewed": False, "format_diff_reviewed": False,
            "native_tests_run": False, "full_ci_passed": False, "production_installed": False,
            "manifest_sha256": args.manifest_sha256, "source_sha256": after["sha256"],
            "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"]})
    except BaseException as error:
        write_json(output / "FAILURE.json", {"phase": phase, "error": repr(error),
                   "native_tests_run": False, "full_ci_passed": False, "production_installed": False})
        raise
    finally:
        try:
            write_json(output / "FINAL-SOURCE.json", full_source(root))
        except BaseException as error:
            write_json(output / "FINAL-SOURCE-ERROR.json", {"error": repr(error)})


if __name__ == "__main__":
    main()
