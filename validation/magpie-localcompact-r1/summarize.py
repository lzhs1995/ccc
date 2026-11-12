#!/usr/bin/env python3
"""Revalidate all four actual case logs; a driver exit alone is insufficient."""
import argparse
import json
from pathlib import Path
from verify import sha256, validate_log, verify_package


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--package-pin", required=True)
    args = parser.parse_args()
    manifest = verify_package(Path(__file__).resolve().parent, args.package_pin)
    summary = {"accepted": False, "cases": {}, "errors": [], "package_pin": args.package_pin}
    for name, spec in manifest["cases"].items():
        try:
            folder = args.results / name
            result = json.loads((folder / "RESULT.json").read_text())
            expected_status = "EXPECTED_RED" if name == "red" else "PASS"
            required = {"case": name, "status": expected_status, "compiler_started": True,
                        "stopped_for_disk_floor": False, "runner_error": None,
                        "source_matches_after": True, "live_child_after": False,
                        "package_pin": args.package_pin}
            if any(result.get(k, "MISSING") != v for k, v in required.items()):
                raise ValueError("driver did not finish cleanly with the required state")
            if result.get("cleanup_error") or result.get("after_error"):
                raise ValueError("cleanup or input-integrity error")
            samples = result.get("samples", [])
            if len(samples) < 2 or any(type(s.get("free_bytes")) is not int or s["free_bytes"] < 30 * 2**30 for s in samples):
                raise ValueError("missing or insufficient disk monitoring")
            expected_environment = {"go_version": manifest["toolchain"], "os": "Darwin",
                                    "arch": "arm64", "same_production_binary_claimed": False}
            if any(result.get("environment", {}).get(k) != v for k, v in expected_environment.items()):
                raise ValueError("toolchain or target metadata mismatch")
            if any(not isinstance(result["environment"].get(k), str) or not result["environment"][k]
                   for k in ("sdk_version", "clang", "os_release")):
                raise ValueError("missing SDK, clang or OS metadata")
            if result["environment"].get("original_local_sdk") != manifest["original_local_sdk"]:
                raise ValueError("original SDK metadata mismatch")
            for log in ("test.jsonl", "stderr.log"):
                if result["log_hashes"][log] != sha256(folder / log):
                    raise ValueError("case log changed: " + log)
            validation = validate_log(folder / "test.jsonl", result["returncode"], spec, red=name == "red")
            if not validation["accepted"]:
                raise ValueError(str(validation["errors"]))
            summary["cases"][name] = {"result_sha256": sha256(folder / "RESULT.json"),
                                      "validation": validation, "environment": result["environment"]}
        except Exception as error:
            summary["errors"].append(name + ": " + type(error).__name__ + ": " + str(error))
    summary["accepted"] = not summary["errors"] and len(summary["cases"]) == 4
    args.results.mkdir(parents=True, exist_ok=True)
    (args.results / "SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["accepted"] else 1)


if __name__ == "__main__":
    main()
