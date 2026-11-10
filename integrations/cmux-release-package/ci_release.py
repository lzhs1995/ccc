"""Build and accept exactly one new Release package on a hosted GitHub Mac.

This is deliberately separate from publication, installation on a user machine,
and original business-session adoption. It cannot restart the local host.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import pwd
import sys

import source_contract as shared
import release_build
import release_package
import release_paired
import release_smoke

need, save = shared.need, shared.save


def contained_attempt(path, *, fresh=False):
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    path = Path(path).absolute()
    need(path.parent.resolve(strict=True).is_relative_to(temporary) and not path.is_symlink(),
         "attempt must be private to RUNNER_TEMP")
    if fresh:
        need(not path.exists(), "fresh attempt required; never replay a build or GUI launch")
        path.mkdir(mode=0o700)
    need(path.is_dir() and path.resolve(strict=True) == path and path.stat().st_uid == os.geteuid(),
         "attempt is not an owned canonical directory")
    return path


def aqua_environment():
    environment = release_paired.safe_launch_environment(os.environ)
    for key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                "GITHUB_SHA", "GITHUB_WORKSPACE", "RUNNER_ENVIRONMENT", "RUNNER_TEMP"):
        environment[key] = os.environ[key]
    need(not any("TOKEN" in key or "SECRET" in key for key in environment), "credential must not enter GUI runtime")
    return environment


def in_aqua(root, phase):
    need(phase in ("preflight", "runtime"), "unknown GUI phase")
    commands = shared.Commands(root / (phase + "-aqua-commands"))
    manager = commands.text(["/bin/launchctl", "managername"], timeout=5).strip()
    if manager == "Aqua":
        if phase == "preflight":
            save(root / "RUNTIME-PREFLIGHT.json", release_smoke.runtime_preflight())
        else:
            release_smoke.runtime(root / "runtime", root / "package", root / "old-cli/cmux")
        return
    uid = Path("/dev/console").stat().st_uid
    need(uid == os.geteuid() and uid > 0, "runner is not the original real console owner")
    user = pwd.getpwuid(uid).pw_name
    argv = ["/usr/bin/sudo", "-n", "/bin/launchctl", "asuser", str(uid),
            "/usr/bin/sudo", "-n", "-u", user, "/usr/bin/env", "-i",
            *[key + "=" + value for key, value in aqua_environment().items()], sys.executable, "-BE",
            str(Path(__file__).resolve()), "--output", str(root), "--" + phase + "-only"]
    commands.run(argv, timeout=60 if phase == "preflight" else release_smoke.RUNTIME_SECONDS + 120)


def run(root, source):
    save(root / "RUN-CONTEXT.json", {"run": shared.run_identity(), "source": str(source),
        "runner_environment": os.environ["RUNNER_ENVIRONMENT"], "compile_attempts": 1,
        "old_tests_rerun": 0, "business_adoption_ready": False})
    stage = "preflight"
    try:
        shared.check_contract()
        in_aqua(root, "preflight")
        stage = "old-cli-admission"
        release_package.download_prior_cli(root / "old-cli")
        stage = "release-build"
        release_build.compile_on_github(source, root / "build")
        stage = "release-package"
        release_package.package(root / "build", root / "package")
        stage = "new-package-runtime"
        in_aqua(root, "runtime")
        runtime = shared.read_json(root / "runtime/RESULT.json")
        cleanup = shared.read_json(root / "runtime/CLEANUP.json")
        pin = shared.read_json(root / "package/PACKAGE-PIN.json")
        need(runtime.get("passed") is True and cleanup.get("passed") is True
             and runtime.get("run") == shared.run_identity() == pin.get("run")
             and runtime.get("package_pin_sha256") == shared.digest(root / "package/PACKAGE-PIN.json"),
             "runtime, cleanup and new package do not agree on this exact attempt")
        result = {"schema": "cmux-local-release-ci-result-v1", "passed": True, "run": shared.run_identity(),
            "package_pin_sha256": shared.digest(root / "package/PACKAGE-PIN.json"),
            "runtime_result_sha256": shared.digest(root / "runtime/RESULT.json"),
            "cleanup_result_sha256": shared.digest(root / "runtime/CLEANUP.json"),
            "archive": pin["archive"], "configuration": "Release", "business_adoption_ready": False,
            "source_tests_rerun": 0, "release_builds": 1, "gui_generations": runtime["gui_generations"],
            "acceptance_elapsed_seconds": runtime["acceptance_elapsed_seconds"],
            "cleanup_elapsed_seconds": cleanup["cleanup_elapsed_seconds"],
            "local_install_performed": False, "old_business_controller_verified": False}
        save(root / "RESULT.json", result)
        return result
    except BaseException as error:
        save(root / "FAILURE.json", release_smoke.error_record(error) | {"stage": stage, "run": shared.run_identity(),
            "passed": False, "business_adoption_ready": False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--runtime-only", action="store_true")
    args = parser.parse_args()
    shared.original_admission().runner_only()
    resumed = args.preflight_only or args.runtime_only
    need((args.source is None) == resumed, "a new compile requires --source; Aqua stages reuse the original attempt")
    root = contained_attempt(args.output, fresh=not resumed)
    if resumed:
        context = shared.read_json(root / "RUN-CONTEXT.json")
        need(context.get("run") == shared.run_identity(), "Aqua stage belongs to another CI attempt")
        if args.preflight_only:
            save(root / "RUNTIME-PREFLIGHT.json", release_smoke.runtime_preflight())
        else:
            release_smoke.runtime(root / "runtime", root / "package", root / "old-cli/cmux")
        return
    result = run(root, args.source.resolve(strict=True))
    print(json.dumps({"passed": result["passed"], "output": str(root),
                      "package_pin_sha256": result["package_pin_sha256"], "business_adoption_ready": False}))


if __name__ == "__main__":
    main()
