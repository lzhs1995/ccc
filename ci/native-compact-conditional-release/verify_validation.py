#!/usr/bin/env python3
"""Authenticate completed validation before full tests or native packaging."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

WORKFLOW_ID = 390949450
WORKFLOW_PATH = ".github/workflows/native-compact-conditional-v4.yml"
EXPECTED_MANIFEST = "e5618a9b6b942f1432c771db94349b96f5d39154335bdc5298a55d16631e6d05"
EXPECTED_PYTHON = "b2d25fc2d40b14ebf2bb51b6fa90fddb58428defcfef4a351206e5909b9fa437"
EXPECTED_TREE = "847e4e67d24dff972f6f8caad498fea52564187d3348670619d63010253c67db"

def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["VALIDATION_RUN_ID"]
    commit = os.environ["SOURCE_SHA"]
    if repository != "lzhs1995/ccc" or not run_id.isdecimal() or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid validation identity")
    token = os.environ["GH_TOKEN"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ccc-native-validation"})
    with urllib.request.urlopen(request, timeout=30) as response:
        run = json.load(response)
    if (run["id"] != int(run_id) or run["head_sha"] != commit or run["workflow_id"] != WORKFLOW_ID
            or run["path"] != WORKFLOW_PATH or run["status"] != "completed"
            or run["conclusion"] != "success" or run["head_repository"]["full_name"] != repository
            or not isinstance(run["run_attempt"], int) or run["run_attempt"] < 1):
        raise ValueError("exact source validation has not passed")
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    bundle = workspace / "ci/native-compact-conditional-v4"
    manifest = json.loads((bundle / "source-manifest.json").read_bytes())
    pins = {
        "source_manifest": hashlib.sha256((bundle / "source-manifest.json").read_bytes()).hexdigest(),
        "python_manifest": hashlib.sha256((bundle / "python/manifest.json").read_bytes()).hexdigest(),
        "source_tree": manifest["source_tree_sha256"],
    }
    if pins != {"source_manifest": EXPECTED_MANIFEST, "python_manifest": EXPECTED_PYTHON,
                "source_tree": EXPECTED_TREE}:
        raise ValueError("source or Python pins do not match the validated candidate")
    evidence = workspace / "full-release-evidence"
    evidence.mkdir(exist_ok=True)
    proof = {"schema": "ccc-native-full-release-validation-v1", "repository": repository,
             "source_sha": commit, "validation_run_id": int(run_id),
             "validation_run_attempt": run["run_attempt"], "validation_url": run["html_url"],
             "status": "COMPLETED_VALIDATION_PASSED", "pins": pins,
             "full_test_execution": "NOT_RUN", "native_build": "NOT_RUN", "adoption": "NOT_PERFORMED"}
    (evidence / "validation.json").write_text(json.dumps(proof, indent=2) + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"source_sha={commit}\n")
        output.write(f"validation_run_id={int(run_id)}\nvalidation_run_attempt={run['run_attempt']}\n")
        for name, value in pins.items():
            output.write(f"{name}_sha256={value}\n")
    print(json.dumps(proof, indent=2))

if __name__ == "__main__":
    main()
