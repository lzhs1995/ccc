#!/usr/bin/env python3
"""Generate unreviewed schema candidates from pinned types, never certify a build.

This deliberately uses the upstream test-only generator. The production schema
command reads precomputed exports and cannot bootstrap a changed protocol.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

from ci import (BASE_APPLY, BASE_MANIFEST, BASE_PATCH, TREE, UPSTREAM,
                checked_path, external_graph, full_source, require_github_runner,
                require_sha, run, sha, write_json)

GENERATOR = "schema_fixtures_tests::write_schema_fixtures_from_env"


def load_bootstrap(path, expected_sha):
    require_sha(expected_sha)
    if sha(path) != expected_sha:
        raise ValueError("bootstrap manifest changed")
    value = json.loads(path.read_bytes())
    if (value.get("schema") != "ccc-native-schema-bootstrap-v1"
            or value.get("status") != "FROZEN_FOR_SCHEMA_GENERATION_ONLY"
            or value.get("upstream_commit") != UPSTREAM
            or value.get("upstream_tree") != TREE
            or value.get("generator") != GENERATOR
            or not value.get("overlay")):
        raise ValueError("unreviewed bootstrap contract")
    for field in ("owner_review_sha256", "overlay_patch_sha256",
                  "full_source_sha256", "cargo_manifest_sha256", "cargo_lock_sha256"):
        require_sha(value[field])
    for field in ("owner_review", "overlay_patch"):
        if sha(checked_path(path.parent, value[field])) != value[field + "_sha256"]:
            raise ValueError(field + " changed")
    for name, pin in value["overlay"].items():
        if not name.startswith(("codex-rs/", "native/client-foreground/")):
            raise ValueError("unreviewed overlay path")
        checked_path(path.parent, name)
        if name.startswith("codex-rs/app-server-protocol/schema/"):
            raise ValueError("bootstrap must start from unmodified original schema fixtures")
        require_sha(pin["after_sha256"])
        if pin["before_sha256"] is not None:
            require_sha(pin["before_sha256"])
        if sha(checked_path(path.parent, pin["source"])) != pin["after_sha256"]:
            raise ValueError("overlay member changed: " + name)
    return value


def check_generator_log(path):
    content = path.read_text()
    if (f"test {GENERATOR} ... ok" not in content
            or "test result: ok. 1 passed; 0 failed; 0 ignored;" not in content):
        raise ValueError("exact upstream generator was not executed once")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ccc", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    value = load_bootstrap(args.manifest, args.manifest_sha256)
    if args.validate_only:
        print(json.dumps({"valid": True, "generated": False, "compiled_and_tested": False}))
        return
    require_github_runner()
    root, output = args.codex.resolve(), args.output.resolve()
    bundle = args.manifest.resolve().parent
    output.mkdir(parents=True, exist_ok=False)
    try:
        if (root == output or root in output.parents
                or run(["git", "rev-parse", "HEAD"], root) != UPSTREAM
                or run(["git", "rev-parse", "HEAD^{tree}"], root) != TREE
                or run(["git", "status", "--porcelain"], root)):
            raise ValueError("fresh exact upstream checkout and external output required")
        base = args.ccc.resolve() / "integrations/codex-foreground"
        if (sha(base / "manifest.json") != BASE_MANIFEST
                or sha(base / "apply.py") != BASE_APPLY
                or sha(base / "integration.patch") != BASE_PATCH):
            raise ValueError("original foreground base changed")
        run([sys.executable, str(base / "apply.py"), "--codex", str(root)],
            args.ccc.resolve(), output / "base-application.log")
        original_lock = (root / "codex-rs/Cargo.lock").read_bytes()
        original_schema = root / "codex-rs/app-server-protocol/schema"
        original_pins = {str(p.relative_to(original_schema)): sha(p)
                         for p in original_schema.rglob("*") if p.is_file()}
        write_json(output / "ORIGINAL-SCHEMA-PINS.json", original_pins)
        for name, pin in value["overlay"].items():
            p = checked_path(root, name)
            before = pin["before_sha256"]
            if (before is None and p.exists()) or (before is not None and sha(p) != before):
                raise ValueError("bootstrap preimage changed: " + name)
        patch = checked_path(bundle, value["overlay_patch"])
        run(["git", "apply", "--check", str(patch)], root)
        run(["git", "apply", str(patch)], root)
        for name, pin in value["overlay"].items():
            if sha(checked_path(root, name)) != pin["after_sha256"]:
                raise ValueError("bootstrap patch result differs: " + name)
        after_schema = {str(p.relative_to(original_schema)): sha(p)
                        for p in original_schema.rglob("*") if p.is_file()}
        if after_schema != original_pins:
            raise ValueError("bootstrap patch modified original schema fixtures")
        source = full_source(root)
        if source["sha256"] != value["full_source_sha256"]:
            raise ValueError("complete bootstrap source changed")
        write_json(output / "SOURCE.json", source)
        workspace = root / "codex-rs"
        if (sha(workspace / "Cargo.toml") != value["cargo_manifest_sha256"]
                or sha(workspace / "Cargo.lock") != value["cargo_lock_sha256"]
                or external_graph(original_lock) != external_graph((workspace / "Cargo.lock").read_bytes())):
            raise ValueError("bootstrap dependency graph changed")
        os.environ["CARGO_TARGET_DIR"] = str(output / "target")
        os.environ["CODEX_APP_SERVER_SCHEMA_ROOT"] = str(output / "generated-schema")
        os.environ.pop("CODEX_APP_SERVER_SCHEMA_PRETTIER", None)
        command = ["cargo", "test", "--locked", "-p", "codex-app-server-protocol",
                   "--lib", GENERATOR, "--", "--ignored", "--exact", "--nocapture"]
        for flag, name in (("0", "stable"), ("1", "experimental")):
            os.environ["CODEX_APP_SERVER_SCHEMA_EXPERIMENTAL"] = flag
            log = output / (name + "-generator.log")
            run(command, workspace, log)
            check_generator_log(log)
            if full_source(root)["sha256"] != source["sha256"]:
                raise ValueError("generator changed original source or fixtures")
        generated = output / "generated-schema"
        pins = {str(p.relative_to(generated)): sha(p)
                for p in generated.rglob("*") if p.is_file()}
        for name in ("stable", "experimental"):
            if f"precomputed/app-server-exports-{name}.json.zst" not in pins:
                raise ValueError("missing generated " + name + " exports")
        if not any(p.startswith("json/") for p in pins) or not any(p.startswith("typescript/") for p in pins):
            raise ValueError("missing schema fixtures")
        write_json(output / "GENERATED-SCHEMA-PINS.json", pins)
        write_json(output / "SCHEMA-CHANGE-LIST.json", {
            "added": sorted(set(pins) - set(original_pins)),
            "removed": sorted(set(original_pins) - set(pins)),
            "changed": sorted(n for n in pins.keys() & original_pins.keys() if pins[n] != original_pins[n]),
            "reviewed": False})
        shutil.copyfile(args.manifest, output / "bootstrap-manifest.json")
        write_json(output / "RESULT.json", {
            "at_epoch": time.time(), "schema_generation_passed": True,
            "schema_reviewed": False, "compiled_and_tested": False,
            "full_codex_build": False, "real_180_300_fixture_passed": False,
            "production_installed": False, "manifest_sha256": args.manifest_sha256,
            "source_sha256": source["sha256"], "generated_schema_count": len(pins),
            "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"]})
    except BaseException as exc:
        write_json(output / "FAILURE.json", {"error": repr(exc), "compiled_and_tested": False})
        raise


if __name__ == "__main__":
    main()
