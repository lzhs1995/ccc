#!/usr/bin/env python3
"""Run one frozen Go case on GHA; record failures before returning nonzero."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import time
from verify import sha256, validate_log, verify_package, verify_source

FLOOR = 30 * 2**30


def capture(argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()


def stop_owned(child):
    """Signal only this still-unreaped direct child/session; never a saved PID."""
    if child.poll() is not None:
        return child.wait()
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        if child.poll() is None:
            raise
    try:
        return child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                if child.poll() is None:
                    raise
        return child.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=("focused", "compat", "race", "red"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-pin", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Go execution is restricted to GHA; local use is not authorized")
    root = Path(__file__).resolve().parent
    manifest = verify_package(root, args.package_pin)
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    spec = manifest["cases"][args.case]
    result = {"case": args.case, "status": "NOT_STARTED", "returncode": None,
              "compiler_started": False, "stopped_for_disk_floor": False,
              "runner_error": None, "source_matches_after": False,
              "package_pin": args.package_pin, "samples": []}
    child = None
    red_applied = False
    env = os.environ.copy()
    env.update({"GOTOOLCHAIN": "local", "GOWORK": "off", "GOENV": "off",
                "GOPROXY": "off", "GOSUMDB": "off", "GOMAXPROCS": "2",
                "GOOS": "darwin", "GOARCH": "arm64", "CGO_ENABLED": "1",
                "GOFLAGS": "", "GOEXPERIMENT": ""})
    for key, directory in (("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "xdg-config"),
                           ("XDG_CACHE_HOME", "xdg-cache"), ("XDG_DATA_HOME", "xdg-data")):
        destination = output / directory
        destination.mkdir()
        env[key] = str(destination)
    argv = ["go", "test", "-p=1", "-mod=readonly", "-tags=production", "-json",
            "-timeout=120s", "-count=" + str(spec["count"]), "-run", spec["pattern"]]
    if spec.get("race"):
        argv.append("-race")
    argv.append("./internal/gateway")
    result["argv"] = argv
    started = time.monotonic()

    def sample():
        free = shutil.disk_usage(output).free
        result["samples"].append({"elapsed_seconds": time.monotonic() - started, "free_bytes": free})
        return free

    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("this run requires a Darwin arm64 runner")
        version = subprocess.check_output(["go", "env", "GOVERSION"], env=env, text=True).strip()
        if version != manifest["toolchain"]:
            raise RuntimeError("Go toolchain does not match the pinned version")
        result["environment"] = {"go_version": version, "os": platform.system(),
                                 "arch": platform.machine(), "os_release": platform.release(),
                                 "sdk_version": capture(["xcrun", "--show-sdk-version"]),
                                 "clang": capture(["xcrun", "clang", "--version"]),
                                 "original_local_sdk": manifest["original_local_sdk"],
                                 "same_production_binary_claimed": False}
        env["SDKROOT"] = capture(["xcrun", "--show-sdk-path"])
        env["CC"] = capture(["xcrun", "--find", "clang"])
        env["CXX"] = capture(["xcrun", "--find", "clang++"])
        for key in ("CGO_CFLAGS", "CGO_CXXFLAGS", "CGO_LDFLAGS"):
            env[key] = "-mmacosx-version-min=12.0"
        env["CGO_CPPFLAGS"] = ""
        verify_source(source, manifest)
        first = sample()
        time.sleep(1)
        second = sample()
        if min(first, second) < FLOOR:
            result["stopped_for_disk_floor"] = True
            result["status"] = "PREFLIGHT_BELOW_FLOOR"
        else:
            if args.case == "red":
                subprocess.run(["git", "apply", "--check", str(root / "red.patch")], cwd=source, check=True)
                subprocess.run(["git", "apply", str(root / "red.patch")], cwd=source, check=True)
                red_applied = True
                verify_source(source, manifest, red=True)
            with (output / "test.jsonl").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
                child = subprocess.Popen(argv, cwd=source, env=env, stdout=stdout, stderr=stderr,
                                         start_new_session=True)
                result["compiler_started"] = True
                while child.poll() is None:
                    free = sample()
                    # Re-poll after a slow sample. Never signal an already reaped process.
                    if child.poll() is not None:
                        break
                    if free < FLOOR or time.monotonic() - started > 1200:
                        result["stopped_for_disk_floor"] = free < FLOOR
                        result["status"] = "DISK_FLOOR_STOP" if free < FLOOR else "RUNNER_DEADLINE"
                        stop_owned(child)
                        break
                    time.sleep(1)
                result["returncode"] = child.wait()
            if result["status"] == "NOT_STARTED":
                validation = validate_log(output / "test.jsonl", result["returncode"], spec,
                                          red=args.case == "red")
                result["validation"] = validation
                result["status"] = ("EXPECTED_RED" if args.case == "red" else "PASS") if validation["accepted"] else "FAIL"
    except Exception as error:
        result["runner_error"] = type(error).__name__ + ": " + str(error)
        result["status"] = "RUNNER_ERROR"
    finally:
        try:
            if child is not None:
                result["returncode"] = stop_owned(child)
        except Exception as error:
            result["cleanup_error"] = type(error).__name__ + ": " + str(error)
            result["status"] = "RUNNER_ERROR"
            if result["runner_error"] is None:
                result["runner_error"] = result["cleanup_error"]
        try:
            if red_applied:
                if child is not None and child.poll() is None:
                    raise RuntimeError("cannot restore RED while owned child is still live")
                subprocess.run(["git", "apply", "--reverse", str(root / "red.patch")], cwd=source, check=True)
            verify_source(source, manifest)
            verify_package(root, args.package_pin)
            result["source_matches_after"] = True
        except Exception as error:
            result["after_error"] = type(error).__name__ + ": " + str(error)
            result["status"] = "INPUT_CHANGED"
        result["elapsed_seconds"] = time.monotonic() - started
        result["live_child_after"] = child is not None and child.poll() is None
        result["log_hashes"] = {p.name: sha256(p) for p in output.glob("*.log")}
        if (output / "test.jsonl").exists():
            result["log_hashes"]["test.jsonl"] = sha256(output / "test.jsonl")
        (output / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
    raise SystemExit(0 if result["status"] in ("PASS", "EXPECTED_RED") else 1)


if __name__ == "__main__":
    main()
