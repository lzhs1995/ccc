"""Authenticate one original build, then run the existing private Herdr fixture.
No Herdr build, installation, production configuration or real agent is involved.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from actions_context import require_actions, require_output, require_capacity

HERE = Path(__file__).resolve().parent
CONTRACT_SHA = "7f52049cdfb026c201a5c2d3f5840c70b1a8e683cd5df0c034d998c9f10c8457"
ARTIFACTS = {
    "evidence.zip": {"id": 13409575988, "bytes": 1654304,
                     "sha256": "f9ed088b21d42ac93f0de20201c8e2a76140e5c96b0891408022329bea6b5e5a",
                     "name": "herdr-namespace-evidence-42014273454-1"},
    "package.zip": {"id": 13409685690, "bytes": 15698726,
                    "sha256": "27475ab1601f5badae1bd7d3d3212036a04d5ebef925d4715ff23dd6d5c7b3ab",
                    "name": "herdr-namespace-arm64-42014273454-1"},
}
SERVER_SHA = "6037394411775f1734830be1727d4863a72a95e5ff8ff4c75393f906d70287bd"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2); stream.write("\n")


def verify_bundle(root, manifest_sha):
    manifest = root / "SOURCE-MANIFEST.json"
    if sha(manifest) != manifest_sha:
        raise ValueError("fixture source manifest pin differs")
    data = json.loads(manifest.read_bytes())
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual != set(data["files"]) | {"SOURCE-MANIFEST.json"}:
        raise ValueError("fixture bundle has missing or unreviewed members")
    for name, pin in data["files"].items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or sha(path) != pin:
            raise ValueError("fixture source member changed: " + name)
    return data


def verify_artifact_metadata(row, pin):
    origin = row.get("workflow_run", {})
    if not (row.get("id") == pin["id"] and row.get("name") == pin["name"]
            and row.get("size_in_bytes") == pin["bytes"] and row.get("expired") is False
            and row.get("digest") == "sha256:" + pin["sha256"]
            and origin.get("id") == 42014273454
            and origin.get("head_sha") == "be088d0a754edd7fc0603e4dac64e6ee1903edd0"
            and origin.get("head_branch") == "codex/herdr-v3-production-namespace-20261105"):
        raise ValueError("exact original artifact metadata differs")


def run_logged(command, path, *, env=None):
    with path.open("xb") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, env=env)
    if result.returncode:
        raise RuntimeError("command failed (log retained): " + path.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runner_temp = require_actions()
    source = verify_bundle(HERE, args.source_manifest_sha256)
    available = require_capacity(runner_temp)
    output = require_output(args.output, runner_temp)
    output.mkdir()
    try:
        save(output / "FIXTURE-SOURCE.json", source)
        save(output / "FIXTURE-JOB.json", {"repository": os.environ["GITHUB_REPOSITORY"],
            "head_sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
            "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "source_manifest_sha256": args.source_manifest_sha256,
            "available_before_download": available, "herdr_build_invoked": False})
        verifier_path = HERE / "fixture/remote_proof.py"
        spec = importlib.util.spec_from_file_location("remote_proof", verifier_path)
        proof = importlib.util.module_from_spec(spec); spec.loader.exec_module(proof)
        if sha(HERE / "CONTRACT.json") != CONTRACT_SHA:
            raise ValueError("original reviewed build contract changed")
        contract = json.loads((HERE / "CONTRACT.json").read_bytes())
        run = proof.gh_json("repos/lzhs1995/ccc/actions/runs/42014273454")
        jobs = proof.gh_json("repos/lzhs1995/ccc/actions/runs/42014273454/attempts/1/jobs?per_page=100")
        proof.validate_run(run, jobs, contract)
        for name, pin in ARTIFACTS.items():
            row = proof.gh_json("repos/lzhs1995/ccc/actions/artifacts/" + str(pin["id"]))
            verify_artifact_metadata(row, pin)
            save(output / (name + ".metadata.json"), row)
            with (output / name).open("xb") as stream:
                subprocess.run(["gh", "api", "repos/lzhs1995/ccc/actions/artifacts/" + str(pin["id"]) + "/zip"],
                               stdout=stream, check=True, timeout=120)
            if (output / name).stat().st_size != pin["bytes"] or sha(output / name) != pin["sha256"]:
                raise ValueError("downloaded exact artifact digest/bytes differ")
        run_logged([sys.executable, "-BE", str(verifier_path), "--contract", str(HERE / "CONTRACT.json"),
            "--contract-sha256", CONTRACT_SHA, "--evidence-zip", str(output / "evidence.zip"),
            "--package-zip", str(output / "package.zip"), "--output", str(output / "proof")], output / "proof.log")
        verified = output / "proof/REMOTE-PROOF.json"
        binary = output / "proof/herdr"
        proof.verify_saved_proof(verified, sha(verified), binary, SERVER_SHA)
        require_capacity(output)
        assets = output / "fixture-assets"; assets.mkdir()
        (assets / "candidate-v3").mkdir()
        shutil.copyfile(HERE / "github-build-preparation/AGY-RECOVERY-V3.patch", assets / "candidate-v3/AGY-RECOVERY-V3.patch")
        shutil.copyfile(HERE / "simulated_agy.rs", assets / "simulated_agy.rs")
        private_env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        rustc = shutil.which("rustc")
        if not rustc: raise RuntimeError("original pinned Actions Rust toolchain missing")
        version = subprocess.check_output([rustc, "+1.99.0", "--version"], text=True, env=private_env).strip()
        if not version.startswith("rustc 1.99.0 "): raise RuntimeError("tiny simulation compiler differs")
        command = [rustc, "+1.99.0", "--edition=2021", "--target", "aarch64-apple-darwin",
                   str(assets / "simulated_agy.rs"), "-o", str(assets / "simulated-agy")]
        run_logged(command, output / "simulation-compile.log", env=private_env)
        save(output / "SIMULATION-COMPILE.json", {"actual_command": command, "actual_rustc": version,
             "source_sha256": sha(assets / "simulated_agy.rs"), "binary_sha256": sha(assets / "simulated-agy"),
             "simulation_only": True, "herdr_build_invoked": False})
        require_capacity(output)
        name = "candidate-private-server-actions-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
        run_logged([sys.executable, "-BE", str(HERE / "fixture/run_private_remote_namespace.py"),
            "--assets", str(assets), "--binary", str(binary), "--sha256", SERVER_SHA,
            "--remote-proof", str(verified), "--remote-proof-sha256", sha(verified),
            "--run-name", name, "--namespace", "herdr"], output / "private-fixture.log", env=private_env)
        results = assets / name
        acceptance = json.loads((results / "CANDIDATE-RESULT.json").read_bytes())
        cleanup = json.loads((results / "CLEANUP.json").read_bytes())
        if acceptance.get("passed") is not True or cleanup.get("passed") is not True:
            raise RuntimeError("original private fixture/cleanup did not pass")
        save(output / "RESULT.json", {"passed": True, "fixture": acceptance, "cleanup": cleanup,
            "source_manifest_sha256": args.source_manifest_sha256,
            "original_build_run": 42014273454, "original_build_attempt": 1,
            "server_sha256": SERVER_SHA, "remote_proof_sha256": sha(verified),
            "herdr_rebuilt": False, "production_installed": False,
            "native_agy": False, "real_herdr_server_and_tui": True})
    except BaseException as exc:
        save(output / "RUNNER-FAILURE.json", {"error": repr(exc), "production_installed": False})
        raise


if __name__ == "__main__":
    main()
