#!/usr/bin/env python3
"""Verify frozen public inputs and actual Go test JSON using only the stdlib."""
from collections import Counter
import hashlib
import json
import re
from pathlib import Path

PACKAGE = "github.com/yetone/magpie/internal/gateway"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("unsafe relative input path")
    return path


def verify_package(root, expected_pin):
    root = Path(root)
    pin_file = root / "PINNED-FILES.json"
    if sha256(pin_file) != expected_pin:
        raise ValueError("package manifest pin mismatch")
    for name, expected in json.loads(pin_file.read_text()).items():
        if sha256(root / relative_path(name)) != expected:
            raise ValueError("package input changed: " + name)
    return json.loads((root / "SOURCE-MANIFEST.json").read_text())


def verify_source(source, manifest, red=False):
    source = Path(source)
    expected = {row["path"]: row["sha256"] for row in manifest["source_files"]}
    if red:
        expected[manifest["red"]["path"]] = manifest["red"]["sha256"]
    for name, digest in expected.items():
        path = source / relative_path(name)
        if not path.is_file() or sha256(path) != digest:
            raise ValueError("effective source changed: " + name)
    actual = {
        str(path.relative_to(source))
        for path in source.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(source).parts
    }
    if actual != set(expected):
        raise ValueError("effective source membership changed")
    return len(expected)


def validate_events(events, returncode, spec, red=False):
    errors = []
    if any(not isinstance(event, dict) for event in events):
        return {"accepted": False, "errors": ["non-object test JSON"]}
    relevant = [e for e in events if e.get("Package") == PACKAGE]
    expected = set(spec["expected_top_level"])
    if any(e.get("Package") not in (None, PACKAGE) for e in events):
        errors.append("unexpected test package")
    active = set()
    for event in relevant:
        action, name = event.get("Action"), event.get("Test")
        if not name:
            continue
        if not isinstance(name, str) or name.split("/", 1)[0] not in expected:
            errors.append("unexpected test name")
            continue
        if red and name not in (spec["expected_leaf"], spec["expected_leaf"].split("/", 1)[0]):
            errors.append("unexpected red subtest")
        if action == "run":
            if name in active:
                errors.append("duplicate active test run")
            active.add(name)
        elif action in ("pass", "fail", "skip"):
            if name not in active:
                errors.append("test terminal without an active run")
            active.discard(name)
    if active:
        errors.append("unterminated test runs")
    runs = Counter(e["Test"] for e in relevant
                   if e.get("Action") == "run" and e.get("Test") and "/" not in e["Test"])
    target = Counter({name: spec["count"] for name in expected})
    if runs != target:
        errors.append("wrong top-level test run counts")
    if any(e.get("Action") == "skip" for e in relevant):
        errors.append("skipped tests are not accepted")
    if any(e.get("Action") in ("build-fail", "build-failed") for e in events):
        errors.append("build failure is not a behavior result")
    failures = Counter(e["Test"] for e in relevant
                       if e.get("Action") == "fail" and e.get("Test"))
    package_results = [e.get("Action") for e in relevant
                       if not e.get("Test") and e.get("Action") in ("pass", "fail", "skip")]
    if sum(e.get("Action") == "start" and not e.get("Test") for e in relevant) != 1:
        errors.append("missing or duplicated package start")
    if not relevant or relevant[-1].get("Test") or relevant[-1].get("Action") not in ("pass", "fail"):
        errors.append("package terminal must be the last record")
    if red:
        leaf = spec["expected_leaf"]
        root = leaf.split("/", 1)[0]
        if returncode != 1 or failures != Counter({root: 1, leaf: 1}):
            errors.append("red must fail only the requested parent and success leaf")
        leaf_runs = [e for e in relevant if e.get("Action") == "run" and e.get("Test") == leaf]
        if len(leaf_runs) != 1:
            errors.append("red leaf did not actually run once")
        output = "".join(e.get("Output", "") for e in relevant if e.get("Test") == leaf)
        if spec["expected_failure_message"] not in output:
            errors.append("red did not fail on the missing complete heartbeat assertion")
        diagnostics = re.findall(r"^\s+[^\n]+\.go:\d+: (.*)$", output, re.MULTILINE)
        if diagnostics != [spec["expected_failure_message"]]:
            errors.append("red has extra or missing assertion diagnostics")
        combined = "".join(e.get("Output", "") for e in events)
        if any(marker in combined for marker in ("panic:", "WARNING: DATA RACE", "build failed", "test timed out")):
            errors.append("red contains an unrelated runtime or build failure")
        if package_results != ["fail"]:
            errors.append("red package result is missing or ambiguous")
    else:
        passes = Counter(e["Test"] for e in relevant
                         if e.get("Action") == "pass" and e.get("Test") and "/" not in e["Test"])
        if returncode != 0 or failures or passes != target or package_results != ["pass"]:
            errors.append("candidate did not pass every requested test")
    return {"accepted": not errors, "errors": errors,
            "actual_top_level_runs": dict(runs), "actual_failures": dict(failures),
            "package_results": package_results, "returncode": returncode}


def validate_log(path, returncode, spec, red=False):
    events = []
    invalid = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            invalid.append(number)
    result = validate_events(events, returncode, spec, red)
    if invalid:
        result["errors"].append("non-JSON stdout records")
        result["invalid_lines"] = invalid
        result["accepted"] = False
    return result
