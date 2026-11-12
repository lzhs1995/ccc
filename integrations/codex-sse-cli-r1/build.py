#!/usr/bin/env python3
"""GHA-only, monitored dev-small build of the pinned R3 CLI source."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from smoke import FLOOR, digest, require, stop_owned


def verify_source(source, spec):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    require(head == spec["upstream_commit"], "wrong source commit")
    for name, expected in spec["source_files"].items():
        require(digest(source / name) == expected, "source changed: " + name)


def run(source, output):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "compilation is restricted to GitHub Actions")
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "wrong build platform")
    source = source.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    spec = json.loads((Path(__file__).parent / "BUILD-INPUTS.json").read_text())
    result = {"status": "NOT_STARTED", "returncode": None, "compiler_started": False,
              "runner_error": None, "child_reaped": False, "source_matches_after": False,
              "stopped_for_disk_floor": False, "samples": [], "argv": spec["build_command"],
              "build_inputs_sha256": digest(Path(__file__).parent / "BUILD-INPUTS.json")}
    child = None
    started = time.monotonic()

    def sample():
        stat = os.statvfs(output)
        free = stat.f_bavail * stat.f_frsize
        result["samples"].append({"elapsed_seconds": time.monotonic() - started, "free_bytes": free})
        return free

    try:
        verify_source(source, spec)
        version = subprocess.check_output(["rustc", "--version"], text=True).strip()
        require(version.split()[1] == spec["toolchain"], "wrong Rust toolchain")
        result["rustc"] = version
        result["sdk"] = subprocess.check_output(["xcrun", "--show-sdk-version"], text=True).strip()
        first = sample()
        time.sleep(1)
        second = sample()
        if min(first, second) < FLOOR:
            result["stopped_for_disk_floor"] = True
            raise RuntimeError("below 30 GiB before build")
        env = os.environ.copy()
        env.update({"CARGO_INCREMENTAL": "0", "CARGO_BUILD_JOBS": "3", "CARGO_NET_OFFLINE": "true"})
        with (output / "build.log").open("w") as log:
            child = subprocess.Popen(spec["build_command"], cwd=source / "codex-rs", env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            result["compiler_started"] = True
            while child.poll() is None:
                free = sample()
                if child.poll() is not None:
                    break
                if free < FLOOR or time.monotonic() - started > 3600:
                    result["stopped_for_disk_floor"] = free < FLOOR
                    raise RuntimeError("build disk floor" if free < FLOOR else "build deadline")
                time.sleep(1)
            result["returncode"] = child.wait()
        require(result["returncode"] == 0, "actual cargo build failed")
        binary = source / "codex-rs/target/dev-small/codex"
        require(binary.is_file() and not binary.is_symlink(), "compiled binary missing")
        description = subprocess.check_output(["file", str(binary)], text=True).strip()
        require("Mach-O" in description and "arm64" in description, "wrong compiled artifact format")
        result["artifact"] = {"path": "codex-rs/target/dev-small/codex", "sha256": digest(binary),
                              "bytes": binary.stat().st_size, "file": description}
        result["status"] = "PASS"
    except Exception as error:
        result["runner_error"] = type(error).__name__ + ": " + str(error)
        result["status"] = "FAIL"
    finally:
        try:
            if child is not None:
                result["returncode"] = stop_owned(child)
                result["child_reaped"] = child.poll() is not None
            verify_source(source, spec)
            result["source_matches_after"] = True
        except Exception as error:
            result["cleanup_error"] = type(error).__name__ + ": " + str(error)
            result["status"] = "FAIL"
        result["elapsed_seconds"] = time.monotonic() - started
        if (output / "build.log").is_file():
            result["build_log_sha256"] = digest(output / "build.log")
        (output / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.source, args.output.resolve()))
