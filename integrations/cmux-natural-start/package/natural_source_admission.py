"""Bind the new source chain to freshly verified native evidence.

Only the published sibling native directory supplies executable validators.
The downloaded native artifact supplies data. No build or test is run here.
"""
from __future__ import annotations
import importlib.util
import re
import sys
from pathlib import Path

import source_contract as shared

NATIVE_ROOT = Path(__file__).resolve().parent.parent / "native"
NATIVE_CONTRACT_SHA256 = "46c9b3f15b087be92262ff2e3f3591fba8580ec532339d1c6130ee66ec38d01d"
# The native owner supplies these only after freezing the reviewed verifier.
# An unfinished pin set rejects every current-source and build admission.
NATIVE_CODE_PINS = {
    "evidence_native.py": {
        "bytes": 13457,
        "sha256": "160cd837b2fd53d106586cc33bae1b5f4ad8fa43109e287fc44fb64e0bb8331d",
    },
    "contract.py": {
        "bytes": 14177,
        "sha256": "ed9d9cfc5c8bd57432f45c505e7c606688878a988600c66e7c71fa85956a2012",
    },
    "upstream/app_host_result_accounting.py": {
        "bytes": 24117,
        "sha256": "826c5789e8112eb0dac52ce6ce1cce23194deda3881654bbd1dd78182e9abfdb",
    },
    "upstream/require_swift_test_execution.py": {
        "bytes": 3688,
        "sha256": "0b251902dee3de974d56300c61f675acf7b277f4f7a264f9305501707086db7e",
    },
}
REQUIRED_VERIFIER_FILES = {
    "evidence_native.py", "contract.py", "upstream/app_host_result_accounting.py",
    "upstream/require_swift_test_execution.py",
}
EXPECTED_SOURCE = {
    "upstream_commit": "2e109a1739d320fe36220afb3e7775b59c1acd47",
    "base_green_tree": "40e8ea03ce1435f11e90b35f98d554a13b9b8685",
    "red_tree": "2ac402069e1c937b23be75126c0fdb663f5905cd",
    "green_tree": "f0e2d0f26d1d5dde021ef2729aeb3f987993fe0a",
    "patches": {
        "original_tests": "bdebf64e054476f3ea0476df3ec7acb91ddbfe86fc2927bb85686720c6a7bf8e",
        "original_fix": "db4f2c96ff9e06fd47ddb55a102f0ccb45d00c691e9e3681ca722f3f97144422",
        "restore_tests": "a36c6c5b14c515b4e10b46dc642639aa7416968b79448f7579d5f49f4d9d06b1",
        "full_fix": "40092c6b45f0c773e241035eba032b20a9d16b9e1574bc53f40d4c9afc24250b",
    },
}
SUMMARY_SCHEMA = "cmux-natural-start-native-admission-v1"
SUMMARY_KEYS = {"schema", "passed", "run", "source", "contract_sha256",
                "native_success_sha256", "manifest_sha256", "verifier_sha256"}
need = shared.need


def same_json(actual, expected):
    """JSON identity keeps Boolean values distinct from integer values."""
    return shared.canonical(actual) == shared.canonical(expected)


def native_file(relative, *, sha256=None, size=None):
    relative = Path(relative)
    need(not relative.is_absolute() and relative.parts and ".." not in relative.parts,
         "unsafe published native path")
    root = NATIVE_ROOT.absolute()
    need(root.is_dir() and not root.is_symlink() and root.resolve(strict=True) == root,
         "published native source directory missing or noncanonical")
    path = root / relative
    need(path.is_file() and not path.is_symlink() and path.resolve(strict=True) == path,
         "published native file missing or linked: " + str(relative))
    if sha256 is not None:
        need(isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256)
             and shared.digest(path) == sha256, "published native file changed: " + str(relative))
    if size is not None:
        need(type(size) is int and size >= 0 and path.stat().st_size == size,
             "published native file size changed: " + str(relative))
    return path


def published_contract():
    need(REQUIRED_VERIFIER_FILES <= set(NATIVE_CODE_PINS),
         "NATIVE_SOURCE_PINS_PENDING: reviewed native verifier pins are incomplete")
    for relative, pin in NATIVE_CODE_PINS.items():
        native_file(relative, sha256=pin["sha256"], size=pin["bytes"])
    path = native_file("contract.json", sha256=NATIVE_CONTRACT_SHA256)
    value = shared.read_json(path)
    need(value.get("schema") == "cmux-natural-start-native-contract-v1"
         and value.get("status") == "FROZEN_READY_FOR_NATIVE_EXECUTION",
         "NATIVE_SOURCE_PINS_PENDING: current native source contract is not ready")
    need(same_json(value.get("source"), EXPECTED_SOURCE), "current native source chain differs")
    need(EXPECTED_SOURCE["upstream_commit"] == shared.UPSTREAM
         and EXPECTED_SOURCE["base_green_tree"] == shared.GREEN_TREE
         and EXPECTED_SOURCE["patches"]["original_tests"] == shared.PATCHES["tests.patch"]
         and EXPECTED_SOURCE["patches"]["original_fix"] == shared.PATCHES["fix.patch"],
         "original accepted source base changed")
    pins = value.get("input_pins")
    need(isinstance(pins, dict) and pins, "current native input pins missing")
    for relative, pin in pins.items():
        native_file(relative, sha256=pin["sha256"], size=pin["bytes"])
    for relative, name in (("inputs/restore-tests.patch", "restore_tests"),
                           ("inputs/full-fix.patch", "full_fix")):
        need(pins.get(relative, {}).get("sha256") == EXPECTED_SOURCE["patches"][name],
             "current source patch pin missing or changed: " + name)
    return value


def current_source_contract():
    shared.original_pin()
    source = published_contract()["source"]
    return {**source, "base_green_commit": shared.GREEN_COMMIT,
            "native_contract_sha256": NATIVE_CONTRACT_SHA256,
            "native_verifier_sha256": NATIVE_CODE_PINS["evidence_native.py"]["sha256"]}


def new_patch_steps():
    source = current_source_contract()
    return [("red", native_file("inputs/restore-tests.patch",
                                sha256=source["patches"]["restore_tests"]), source["red_tree"]),
            ("green", native_file("inputs/full-fix.patch",
                                  sha256=source["patches"]["full_fix"]), source["green_tree"])]


def source_file_pins():
    published_contract()
    frozen = shared.read_json(native_file("inputs/source-freeze.json"))
    return {relative: (row["candidate_bytes"], row["candidate_sha256"])
            for relative, row in frozen["files"].items()}


def validate_run(value):
    need(isinstance(value, dict) and set(value) == {"repository", "head_sha", "run_id", "run_attempt"},
         "native run context has missing or unexpected fields")
    need(value["repository"] == "lzhs1995/ccc" and isinstance(value["head_sha"], str)
         and re.fullmatch(r"[0-9a-f]{40}", value["head_sha"]), "native repository/head differs")
    need(type(value["run_id"]) is int and value["run_id"] > 0
         and type(value["run_attempt"]) is int and value["run_attempt"] > 0,
         "native run/attempt must be positive integers")


def validate_native_summary(summary, *, expected_run):
    """Check an already verified summary's binding; do not reclassify raw tests."""
    source = current_source_contract()
    validate_run(expected_run)
    need(isinstance(summary, dict) and set(summary) == SUMMARY_KEYS
         and summary.get("schema") == SUMMARY_SCHEMA and summary.get("passed") is True,
         "verified native admission summary missing or malformed")
    validate_run(summary["run"])
    need(same_json(summary["run"], expected_run), "native summary belongs to another run/attempt/head")
    actual = summary["source"]
    commit_keys = {stage + suffix for stage in ("base", "red", "green")
                   for suffix in ("_commit", "_parent_commit")}
    need(isinstance(actual, dict) and set(actual) == set(EXPECTED_SOURCE) | commit_keys,
         "native source summary has missing or unexpected fields")
    need(same_json({key: actual[key] for key in EXPECTED_SOURCE}, EXPECTED_SOURCE),
         "native summary has a foreign source tree or patch")
    need(all(isinstance(actual[key], str) and re.fullmatch(r"[0-9a-f]{40}", actual[key])
             for key in commit_keys), "native source commit identity is missing")
    commit_ids = {actual[stage + "_commit"] for stage in ("base", "red", "green")}
    need(actual["base_parent_commit"] == source["upstream_commit"]
         and actual["red_parent_commit"] == actual["base_commit"]
         and actual["green_parent_commit"] == actual["red_commit"]
         and len(commit_ids | {source["upstream_commit"]}) == 4,
         "native actual source commit chain differs")
    for key in ("native_success_sha256", "manifest_sha256", "contract_sha256", "verifier_sha256"):
        need(isinstance(summary[key], str) and re.fullmatch(r"[0-9a-f]{64}", summary[key]),
             "native evidence digest missing: " + key)
    need(summary["contract_sha256"] == source["native_contract_sha256"]
         and summary["verifier_sha256"] == source["native_verifier_sha256"],
         "native summary used another source contract or verifier")
    return summary


def load_evidence_verifier():
    published_contract()
    path = native_file("evidence_native.py")
    spec = importlib.util.spec_from_file_location("cmux_natural_start_release_evidence", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def admit_native_evidence(root, *, expected_run):
    need(root is not None, "this build requires explicit current native evidence")
    root = Path(root).absolute()
    need(root.is_dir() and not root.is_symlink() and root.resolve(strict=True) == root,
         "native evidence must be an existing canonical directory")
    source = current_source_contract()
    validate_run(expected_run)
    verifier = load_evidence_verifier()
    verified = verifier.verify(root, expected_run=expected_run,
        expected_source={key: source[key] for key in EXPECTED_SOURCE},
        expected_contract_sha256=source["native_contract_sha256"])
    need(isinstance(verified, dict) and verified.get("passed") is True,
         "complete current native evidence did not pass")
    receipt = shared.read_json(root / "NATIVE-SUCCESS.json")
    need(receipt.get("schema") == "cmux-natural-start-native-success-v1"
         and receipt.get("status") == "NATIVE_RED_GREEN_AND_ADJACENT_PASSED"
         and same_json(receipt.get("run"), expected_run)
         and same_json(receipt.get("source"), verified.get("source")),
         "original native success receipt differs from verified evidence")
    summary = {"schema": SUMMARY_SCHEMA, "passed": True, "run": receipt["run"],
        "source": receipt["source"], "contract_sha256": receipt["contract_sha256"],
        "native_success_sha256": shared.digest(root / "NATIVE-SUCCESS.json"),
        "manifest_sha256": shared.digest(root / "MANIFEST.json"),
        "verifier_sha256": source["native_verifier_sha256"]}
    need(summary["native_success_sha256"] == verified.get("success_sha256")
         and summary["manifest_sha256"] == verified.get("manifest_sha256")
         and same_json(verified.get("run"), expected_run)
         and verified.get("contract_sha256") == summary["contract_sha256"],
         "native evidence changed after verification")
    return validate_native_summary(summary, expected_run=expected_run)
