"""Pinned original source evidence and small shared package primitives."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PRE = ROOT / "prerequisites"
SOURCE_PIN_SHA256 = "cc1f7d5223d96b135955c39ee2955c27966d52f458918c7c2096546912f7a4b0"
ADMISSION_SHA256 = "07846c5d4d1271397d9b1c3470412fc3856e37615539e2aec1a9959f5e50e4e8"
EXPECTATIONS_SHA256 = "95549620100de2f8baa5732160801ef2cf2efd57841a0f919208635cbb140465"
UPSTREAM = "2e109a1739d320fe36220afb3e7775b59c1acd47"
GREEN_TREE = "40e8ea03ce1435f11e90b35f98d554a13b9b8685"
GREEN_COMMIT = "febf361281fdc0f463e84d9d5e4dd65d107dd818"
PATCHES = {"tests.patch": "bdebf64e054476f3ea0476df3ec7acb91ddbfe86fc2927bb85686720c6a7bf8e",
           "fix.patch": "db4f2c96ff9e06fd47ddb55a102f0ccb45d00c691e9e3681ca722f3f97144422"}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def save(path, value):
    with Path(path).open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs)


def run_identity():
    return {"repository": os.environ["GITHUB_REPOSITORY"], "head_sha": os.environ["GITHUB_SHA"],
            "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}


class Commands:
    """Write intent and exact original streams before interpreting an outcome.

    A timeout is UNKNOWN, retained without retry. Large build output goes to
    files directly, rather than through the Python process's memory.
    """
    def __init__(self, root, *, cwd=None, environment=None):
        self.root, self.cwd, self.environment = Path(root), cwd, environment
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.sequence = 0

    def run(self, argv, *, timeout=60, allow_failure=False):
        self.sequence += 1
        stem = self.root / ("%03d" % self.sequence)
        argv = list(map(str, argv))
        save(stem.with_suffix(".intent.json"), {"argv": argv, "cwd": str(self.cwd) if self.cwd else None,
                                                "timeout_seconds": timeout, "retries": 0})
        started = time.monotonic()
        outcome = {"argv": argv, "returncode": None, "status": "UNKNOWN"}
        error = None
        with stem.with_suffix(".stdout").open("xb") as stdout, stem.with_suffix(".stderr").open("xb") as stderr:
            try:
                result = subprocess.run(argv, cwd=self.cwd, env=self.environment, stdout=stdout, stderr=stderr,
                                        timeout=timeout)
                outcome.update(returncode=result.returncode, status="COMPLETED")
            except BaseException as exc:
                error = exc
                outcome["error"] = repr(exc)
        outcome["elapsed_seconds"] = time.monotonic() - started
        for suffix in ("stdout", "stderr"):
            path = stem.with_suffix("." + suffix)
            outcome[suffix] = {"file": path.name, "sha256": digest(path), "bytes": path.stat().st_size}
        save(stem.with_suffix(".result.json"), outcome)
        if error is not None:
            raise error
        need(allow_failure or outcome["returncode"] == 0, "command failed; retained at " + str(stem))
        return outcome

    def text(self, argv, *, timeout=60, allow_failure=False):
        outcome = self.run(argv, timeout=timeout, allow_failure=allow_failure)
        path = self.root / outcome["stdout"]["file"]
        need(path.stat().st_size <= 16 * 1024**2, "command text exceeds inspection bound")
        return path.read_text()


def original_admission():
    path = PRE / "original_fixed_app_admission.py"
    need(digest(path) == ADMISSION_SHA256, "original admission source changed")
    spec = importlib.util.spec_from_file_location("original_fixed_app_admission", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def original_pin():
    need(digest(PRE / "APP-PIN.json") == SOURCE_PIN_SHA256, "original source-build pin changed")
    need(digest(PRE / "FIXED-TEST-EXPECTATIONS.json") == EXPECTATIONS_SHA256, "original test contract changed")
    admission = original_admission()
    pin = admission.require_pin(json.loads((PRE / "APP-PIN.json").read_text()))
    objects = {}
    for name, field in (("BUILD-RUN.json", "build_run_json_sha256"),
                        ("ARTIFACT.json", "artifact_json_sha256"),
                        ("PROVENANCE.json", "provenance_sha256")):
        need(digest(PRE / name) == pin[field], "original source receipt changed: " + name)
        objects[name] = json.loads((PRE / name).read_text())
    admission.validate_metadata(pin, objects["BUILD-RUN.json"], objects["ARTIFACT.json"], objects["PROVENANCE.json"])
    need(pin["upstream_commit"] == UPSTREAM and pin["green_source_tree"] == GREEN_TREE
         and pin["green_source_commit"] == GREEN_COMMIT, "fixed source chain differs")
    for name, expected in PATCHES.items():
        need(digest(ROOT / "inputs" / name) == expected, "fixed source patch changed: " + name)
    return pin


def command(argv, *, timeout=300, environment=None):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=environment)
    record = {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    need(result.returncode == 0, "command failed: " + json.dumps(record))
    return record


def fresh_runner_output(path):
    original_admission().runner_only()
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    path = Path(path).absolute()
    need(not path.exists() and not path.is_symlink(), "fresh output required; preserve the original attempt")
    parent = path.parent.resolve(strict=True)
    need(parent.is_relative_to(temporary) and parent.stat().st_uid == os.geteuid(), "output outside owned runner temp")
    path.mkdir(mode=0o700)
    return path.resolve(strict=True)


def current_source_contract():
    import natural_source_admission
    return natural_source_admission.current_source_contract()


def current_source_tree():
    return current_source_contract()["green_tree"]


def validate_native_summary(summary, *, expected_run):
    import natural_source_admission
    return natural_source_admission.validate_native_summary(summary, expected_run=expected_run)


def check_contract():
    pin = original_pin()
    current = current_source_contract()
    return {"passed": True, "source_app_pin_sha256": SOURCE_PIN_SHA256,
            "source_tree": current["green_tree"], "source_base_tree": GREEN_TREE,
            "native_contract_sha256": current["native_contract_sha256"],
            "old_test_receipts_sha256": pin["test_receipts_sha256"],
            "original_build_run": [pin["build_run_id"], pin["build_run_attempt"]],
            "current_native_evidence_required": True, "release_build_executed": False}
