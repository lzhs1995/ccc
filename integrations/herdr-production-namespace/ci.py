"""Isolated Herdr build; final crate uses production namespace, not release profile."""
import argparse
import fcntl
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET

PATCH_SHA = "3a18fdb4c86a77acfe2471bb6610db3f283281e9d4e75c890f8d3a105f661966"
UPSTREAM = "2ecf19a92fc3a7fff75b6ce3655ef04f4625ec01"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(argv, cwd, env, log=None):
    if log is None:
        return subprocess.check_output(argv, cwd=cwd, env=env, text=True).strip()
    with log.open("xb") as stream:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    require(result.returncode == 0, f"command exit {result.returncode}: {log}")


def validate_bundle(bundle, expected):
    require(sha(bundle / "freeze.json") == expected, "freeze changed")
    value = json.loads((bundle / "freeze.json").read_bytes())
    require(value["schema"] == "herdr-production-namespace-ci-v1"
            and value["status"] == "FROZEN_FOR_ISOLATED_CI_ONLY"
            and value["upstream_commit"] == UPSTREAM
            and value["patch_sha256"] == PATCH_SHA, "unreviewed candidate")
    for name, key in (("AGY-RECOVERY-V3.patch", "patch_sha256"),
                      ("CANDIDATE-SOURCE-PINS.json", "pins_sha256"),
                      ("UPSTREAM-SOURCE.json", "upstream_manifest_sha256"),
                      ("CANDIDATE-SOURCE.json", "candidate_manifest_sha256"),
                      ("EXPECTED-CASES.json", "expected_cases_sha256")):
        require(sha(bundle / name) == value[key], f"changed bundle member: {name}")
    require((value["rust_toolchain"], value["nextest"], value["zig"])
            == ("1.99.0", "0.9.146", "0.16.0"), "toolchain contract changed")
    return value


def source_manifest(root):
    rows = []
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            if (Path(directory) / name).is_symlink():
                directories.remove(name)
                files.append(name)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                rows.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
            else:
                rows.append({"path": relative, "kind": "file", "sha256": sha(path),
                             "executable": bool(path.stat().st_mode & 0o111)})
    rows.sort(key=lambda row: row["path"])
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"files": rows, "sha256": digest}


def source_checkpoint(source, expected, output, phase):
    actual = source_manifest(source)
    before = {row["path"]: row for row in expected["files"]}
    after = {row["path"]: row for row in actual["files"]}
    difference = {"phase": phase, "expected_sha256": expected["sha256"],
                  "actual_sha256": actual["sha256"],
                  "added": [after[key] for key in sorted(after.keys() - before.keys())],
                  "removed": [before[key] for key in sorted(before.keys() - after.keys())],
                  "changed": [{"before": before[key], "after": after[key]}
                              for key in sorted(before.keys() & after.keys())
                              if before[key] != after[key]]}
    save(output / ("SOURCE-" + phase + ".json"), actual)
    save(output / ("SOURCE-DIFF-" + phase + ".json"), difference)
    require(actual == expected, "frozen source changed at " + phase + "; see saved SOURCE-DIFF")


def zig_vendor_inputs(expected_source):
    prefix = "vendor/libghostty-vt/"
    rows = [{**row, "path": row["path"][len(prefix):]} for row in expected_source["files"]
            if row["path"].startswith(prefix)]
    require(rows and all(row["kind"] == "file" and not row["path"].startswith("zig-pkg/")
                         for row in rows), "unexpected frozen Zig vendor input shape")
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"files": rows, "sha256": digest}


def validate_zig_mirror(mirror, expected):
    actual = source_manifest(mirror)
    frozen = {row["path"]: row for row in expected["files"]}
    observed = {row["path"]: row for row in actual["files"]}
    require(all(observed.get(name) == row for name, row in frozen.items()),
            "Zig mirror frozen input changed or disappeared")
    packages = [row for name, row in observed.items() if name not in frozen]
    require(all(row["path"].startswith("zig-pkg/") for row in packages),
            "unexpected generated file outside external Zig package directory")
    return actual, packages


def zig_wrapper_main(spec_path, spec_sha256, argv):
    spec_path = Path(spec_path)
    require(sha(spec_path) == spec_sha256, "Zig wrapper spec changed")
    spec = json.loads(spec_path.read_bytes())
    require(sha(Path(__file__)) == spec["ci_sha256"] and sha(Path(spec["real_zig"])) == spec["real_zig_sha256"],
            "reviewed CI or real Zig executable changed")
    original_cwd = Path.cwd().resolve()
    if argv == ["version"]:
        execution_cwd = original_cwd
    else:
        require(len(argv) == 9 and argv[:2] == ["build", "--prefix"], "unexpected Zig command")
        prefix = Path(argv[2])
        require(prefix.is_absolute() and prefix.resolve().is_relative_to(Path(spec["target_dir"]))
                and prefix.name == "zig-out" and prefix.parent.name == "out",
                "Zig install prefix outside original Cargo OUT_DIR")
        require(argv[3:] == spec["build_tail"] and original_cwd == Path(spec["original_vendor"]),
                "Zig build arguments or original vendor cwd changed")
        inputs = Path(spec["inputs"])
        require(sha(inputs) == spec["inputs_sha256"], "Zig input manifest changed")
        expected = json.loads(inputs.read_bytes())
        require(source_manifest(original_cwd) == expected, "original frozen Zig source changed")
        execution_cwd = Path(spec["mirror"])
        validate_zig_mirror(execution_cwd, expected)
        os.chdir(execution_cwd)
    event = {"event": "exec_intent", "pid": os.getpid(), "argv": [spec["real_zig"], *argv],
             "original_cwd": str(original_cwd), "execution_cwd": str(execution_cwd),
             "real_zig_sha256": spec["real_zig_sha256"], "spec_sha256": spec_sha256,
             "wrapper_sha256": sha(Path(spec["wrapper"])), "at_epoch": time.time()}
    with Path(spec["invocations"]).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.execv(spec["real_zig"], [spec["real_zig"], *argv])


def prepare_zig_mirror(source, output, env, real_zig, expected_source):
    expected = zig_vendor_inputs(expected_source)
    vendor = (source / "vendor/libghostty-vt").resolve()
    require(source_manifest(vendor) == expected, "original vendor differs before mirroring")
    mirror = output / "zig-vendor-inputs"
    shutil.copytree(vendor, mirror, symlinks=True)
    require(source_manifest(mirror) == expected, "copied vendor differs from frozen source")
    inputs = output / "ZIG-MIRROR-INPUTS.json"
    save(inputs, expected)
    wrapper = output / "zig-output-wrapper"
    spec_path = output / "ZIG-MIRROR-SPEC.json"
    spec = {"schema": "herdr-zig-output-mirror-v1", "original_vendor": str(vendor),
            "mirror": str(mirror), "inputs": str(inputs), "inputs_sha256": sha(inputs),
            "target_dir": env["CARGO_TARGET_DIR"], "real_zig": str(real_zig), "real_zig_sha256": sha(real_zig),
            "ci_sha256": sha(Path(__file__)), "ci_path": str(Path(__file__).resolve()), "wrapper": str(wrapper),
            "invocations": str(output / "zig-invocations.log"),
            "build_tail": ["-Demit-lib-vt", "-Doptimize=ReleaseFast", "-Dsimd=true", "-Dtarget=aarch64-macos",
                           "-Dversion-string=" + (vendor / "VERSION").read_text().strip(), "-Demit-xcframework=false"]}
    save(spec_path, spec)
    wrapper.write_text("#!" + sys.executable + "\nimport hashlib, pathlib, runpy, sys\n"
        + "ci_path = pathlib.Path(" + repr(str(Path(__file__).resolve())) + ")\n"
        + "assert hashlib.sha256(ci_path.read_bytes()).hexdigest() == " + repr(spec["ci_sha256"]) + "\n"
        + "runpy.run_path(str(ci_path))['zig_wrapper_main'](" + repr(str(spec_path)) + ", "
        + repr(sha(spec_path)) + ", sys.argv[1:])\n")
    wrapper.chmod(0o555)
    save(output / "ZIG-MIRROR-PREPARATION.json", {"spec_sha256": sha(spec_path),
        "inputs_sha256": sha(inputs), "frozen_input_count": len(expected["files"]),
        "wrapper_sha256": sha(wrapper), "wrapper_source": wrapper.read_text(),
        "real_zig_sha256": spec["real_zig_sha256"], "system_package_mode": False})
    env["ZIG"] = str(wrapper)
    return spec


def zig_mirror_checkpoint(output, spec, phase):
    inputs = Path(spec["inputs"])
    require(sha(inputs) == spec["inputs_sha256"], "Zig input manifest changed")
    expected = json.loads(inputs.read_bytes())
    actual, packages = validate_zig_mirror(Path(spec["mirror"]), expected)
    save(output / ("ZIG-MIRROR-" + phase + ".json"), actual)
    save(output / ("ZIG-PACKAGES-" + phase + ".json"), {"files": packages,
        "sha256": hashlib.sha256(json.dumps(packages, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})


def download(pin, destination):
    with urllib.request.urlopen(pin["url"], timeout=120) as response, destination.open("xb") as stream:
        shutil.copyfileobj(response, stream)
    require(destination.stat().st_size == pin["bytes"] and sha(destination) == pin["sha256"],
            "download bytes differ from reviewed official archive")


def extract(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    require(len(roots) == 1 and roots[0].is_dir(), "archive top-level changed")
    return roots[0]


def compile_target(source, output, env, unit):
    label = "unit" if unit else "server"
    command = ["cargo", "+1.99.0", "rustc", "--locked", "--offline", "-p", "herdr",
               "--bin", "herdr", "--message-format=json", "--verbose",
               *(["--profile", "test"] if unit else []), "--", "-C", "debug-assertions=off"]
    log = output / (label + "-build.log")
    run(command, source, env, log)
    messages = []
    # Preserve raw log bytes; Cargo color escapes must not hide the actual Running line.
    lines = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text()).splitlines()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    artifacts = [row for row in messages if row.get("reason") == "compiler-artifact"
                 and row.get("target", {}).get("name") == "herdr" and row.get("executable")]
    require(len(artifacts) == 1, "ambiguous final compiler artifact")
    artifact = artifacts[0]
    require(artifact["profile"]["test"] is unit and artifact["target"]["kind"] == ["bin"],
            "wrong compiler target/profile")
    commands = [line for line in lines if "Running " in line and "--crate-name herdr " in line]
    require(len(commands) == 1 and "-C debug-assertions=off" in commands[0]
            and ((" --test " in commands[0]) is unit), "actual final rustc flags differ")
    frozen = output / ("herdr-unit" if unit else "herdr")
    shutil.copy2(artifact["executable"], frozen)
    frozen.chmod(0o555)
    record = {"command": command, "artifact": artifact, "path": str(frozen),
              "sha256": sha(frozen), "bytes": frozen.stat().st_size,
              "actual_rustc_command": commands[0], "nonoptimized": True,
              "root_crate_debug_assertions": False, "dependency_profile": "unchanged dev/test"}
    save(output / (label + "-compile.json"), record)
    return record, messages


def require_case_set(listing, expected, binary):
    suites = listing["rust-suites"]
    require(list(suites) == ["herdr::bin/herdr"], "unexpected test binary")
    suite = suites["herdr::bin/herdr"]
    require(suite["binary-path"] == binary, "nextest selected a different binary")
    actual = sorted(name for name, row in suite["testcases"].items() if not row["ignored"])
    ignored = sorted(name for name, row in suite["testcases"].items() if row["ignored"])
    require(actual == expected["nonignored"] and ignored == expected["ignored"],
            "new cfg changed test discovery; review saved LIST.json before changing expectations")


def require_junit(path, expected):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    require(not any(case.find(tag) is not None for case in cases for tag in ("error", "failure", "skipped")),
            "JUnit contains failed/skipped test")
    require(all(case.get("classname") == "herdr::bin/herdr" for case in cases), "wrong JUnit binary")
    require(Counter(case.get("name") for case in cases) == Counter(expected["nonignored"]),
            "JUnit is not complete exact nonignored suite")
    return {"successful_cases": len(cases), "ignored_not_run": len(expected["ignored"]),
            "junit_sha256": sha(path)}


def run_units(source, output, env, unit, messages, expected, expected_source):
    target = Path(env["CARGO_TARGET_DIR"])
    metadata = json.loads(run(["cargo", "+1.99.0", "metadata", "--locked", "--offline",
                              "--format-version", "1", "--filter-platform", "aarch64-apple-darwin"], source, env))
    save(output / "cargo-metadata.json", metadata)
    package = next(p for p in metadata["packages"] if p["name"] == "herdr"
                   and Path(p["manifest_path"]) == source / "Cargo.toml")
    require(unit["artifact"]["package_id"] == package["id"], "unit package differs from metadata")
    build_out, build_info, linked = {}, {}, set()
    for row in messages:
        if row.get("reason") != "build-script-executed":
            continue
        if row["package_id"] in metadata["workspace_members"]:
            build_out[row["package_id"]] = os.path.relpath(row["out_dir"], target)
            build_info[row["package_id"]] = {"envs": dict(row.get("env", []))}
        for path in row.get("linked_paths", []):
            linked.add(os.path.relpath(path.split("=", 1)[-1], target))
    binary_meta = {"rust-build-meta": {"target-directory": str(target), "build-directory": str(target),
        "base-output-directories": ["debug"], "non-test-binaries": {},
        "build-script-out-dirs": build_out, "build-script-info": build_info, "linked-paths": sorted(linked)},
        "rust-binaries": {"herdr::bin/herdr": {"binary-id": "herdr::bin/herdr", "binary-name": "herdr",
        "package-id": package["id"], "kind": "bin", "binary-path": unit["path"], "build-platform": "target"}}}
    save(output / "binaries-metadata.json", binary_meta)
    save(output / "METADATA-PROVENANCE.json", {
        "origin": "Explicit nextest reuse input from actual Cargo compiler-artifact/build-script metadata; not test results",
        "unit_compile_sha256": sha(output / "unit-compile.json"),
        "cargo_metadata_sha256": sha(output / "cargo-metadata.json"),
        "binary_metadata_sha256": sha(output / "binaries-metadata.json")})
    config = output / "nextest.toml"
    junit = output / "unit.xml"
    config.write_text('[store]\ndir = ' + json.dumps(str(output / "nextest-store"))
                      + '\n[profile.default]\nretries = 0\n[profile.default.junit]\npath = '
                      + json.dumps(str(junit)) + "\n")
    reuse = ["--cargo-metadata", str(output / "cargo-metadata.json"), "--binaries-metadata",
             str(output / "binaries-metadata.json"), "--config-file", str(config), "--user-config-file", "none"]
    private = Path(env["TMPDIR"]) / "home"
    private.mkdir()
    test_env = {key: value for key, value in env.items() if not key.startswith("HERDR_")}
    test_env.update(HOME=str(private), XDG_CONFIG_HOME=str(private / ".config"),
        XDG_DATA_HOME=str(private / ".local/share"), XDG_STATE_HOME=str(private / ".local/state"),
        XDG_CACHE_HOME=str(private / ".cache"), TMPDIR=env["TMPDIR"])
    listing = json.loads(run(["cargo-nextest", "nextest", "list", *reuse, "--message-format", "json"], source, test_env))
    save(output / "LIST.json", listing)
    source_checkpoint(source, expected_source, output, "after-list")
    require_case_set(listing, expected, unit["path"])
    require(sha(unit["path"]) == unit["sha256"], "unit bytes changed before tests")
    run(["cargo-nextest", "nextest", "run", *reuse, "--test-threads", "2", "--no-fail-fast",
         "--retries", "0", "--status-level", "fail", "--final-status-level", "fail",
         "--failure-output", "final", "--success-output", "never"], source, test_env, output / "unit-tests.log")
    result = require_junit(junit, expected)
    require(sha(unit["path"]) == unit["sha256"], "unit bytes changed during tests")
    save(output / "UNIT-RESULT.json", {"passed": True, **result, "unit_binary_sha256": unit["sha256"],
                                      "cargo_build_invoked_by_nextest": False})
    source_checkpoint(source, expected_source, output, "after-tests")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    frozen = validate_bundle(bundle, args.freeze_sha256)
    if args.validate_only:
        print(json.dumps({"contract_valid": True, "compiled": False, "production_installed": False}))
        return
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY") == "lzhs1995/ccc"
            and os.environ.get("RUNNER_OS") == "macOS" and platform.machine() == "arm64",
            "full builds only on approved isolated GitHub macOS arm64 runner")
    output = args.output.resolve()
    output.mkdir(exist_ok=False)
    try:
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("HERDR_") or key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "RUSTDOC"):
                del env[key]
        env.update(RUSTUP_TOOLCHAIN="1.99.0", CARGO_TARGET_DIR=str(output / "target"),
            CARGO_INCREMENTAL="0", CARGO_BUILD_JOBS="2", CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_TEST_DEBUG="0",
            ZIG_GLOBAL_CACHE_DIR=str(output / "zig-global-cache"), ZIG_LOCAL_CACHE_DIR=str(output / "zig-local-cache"))
        env.setdefault("CARGO_HOME", str(Path.home() / ".cargo"))
        env.setdefault("RUSTUP_HOME", str(Path.home() / ".rustup"))
        # Short absolute TMPDIR prevents AF_UNIX pathname length failures in unit tests.
        short_tmp = Path("/tmp") / ("hns-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"])
        short_tmp.mkdir(exist_ok=False)
        (output / "short-tmp").symlink_to(short_tmp, target_is_directory=True)
        env["TMPDIR"] = str(short_tmp)
        env["CARGO"] = run(["rustup", "which", "--toolchain", "1.99.0", "cargo"], output, env)
        rustc = run(["rustc", "+1.99.0", "--version"], output, env)
        nextest = run(["cargo-nextest", "nextest", "--version"], output, env)
        require(rustc.startswith("rustc 1.99.0 ") and nextest.startswith("cargo-nextest 0.9.146 "), "actual tools differ")
        download(frozen["source_archive"], output / "source.tar.gz")
        source = extract(output / "source.tar.gz", output / "source")
        require(source_manifest(source) == json.loads((bundle / "UPSTREAM-SOURCE.json").read_bytes()), "full upstream tree differs")
        run(["git", "apply", "--check", str(bundle / "AGY-RECOVERY-V3.patch")], source, env, output / "patch-check.log")
        run(["git", "apply", str(bundle / "AGY-RECOVERY-V3.patch")], source, env, output / "patch-apply.log")
        expected_source = json.loads((bundle / "CANDIDATE-SOURCE.json").read_bytes())
        require(source_manifest(source) == expected_source, "full patched source differs")
        save(output / "SOURCE.json", expected_source)
        require(sha(source / "Cargo.toml") == frozen["cargo_manifest_sha256"]
                and sha(source / "Cargo.lock") == frozen["cargo_lock_sha256"], "Cargo graph differs")
        download(frozen["zig_archive"], output / "zig.tar.xz")
        zig = extract(output / "zig.tar.xz", output / "zig")
        env["ZIG"] = str(zig / "zig")
        require(run([env["ZIG"], "version"], output, env) == "0.16.0", "Zig version differs")
        zig_mirror = prepare_zig_mirror(source, output, env, zig / "zig", expected_source)
        run(["cargo", "+1.99.0", "fetch", "--locked", "--target", "aarch64-apple-darwin"], source, env, output / "fetch.log")
        source_checkpoint(source, expected_source, output, "after-fetch")
        server, _ = compile_target(source, output, env, False)
        source_checkpoint(source, expected_source, output, "after-server")
        zig_mirror_checkpoint(output, zig_mirror, "after-server")
        unit, messages = compile_target(source, output, env, True)
        source_checkpoint(source, expected_source, output, "after-unit")
        zig_mirror_checkpoint(output, zig_mirror, "after-unit")
        expected = json.loads((bundle / "EXPECTED-CASES.json").read_bytes())
        tests = run_units(source, output, env, unit, messages, expected, expected_source)
        zig_mirror_checkpoint(output, zig_mirror, "after-tests")
        require(sha(server["path"]) == server["sha256"], "server artifact changed")
        package = output / "package"
        package.mkdir()
        manifest = {"schema": "herdr-production-namespace-artifact-v1", "freeze_sha256": args.freeze_sha256,
            "patch_sha256": PATCH_SHA, "full_source_sha256": expected_source["sha256"],
            "cargo_lock_sha256": frozen["cargo_lock_sha256"], "server_sha256": server["sha256"],
            "server_bytes": server["bytes"], "unit_sha256": unit["sha256"], "unit_results": tests,
            "rustc": rustc, "nextest": nextest, "zig": "0.16.0", "namespace": "herdr",
            "nonoptimized": True, "root_crate_debug_assertions": False, "official_release": False,
            "upstream_rust": "1.96.1", "actual_rust": "1.99.0", "production_installed": False,
            "private_real_recovery_fixture_passed": False, "physical_power_loss_tested": False,
            "actions_run_id": os.environ["GITHUB_RUN_ID"], "actions_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "commit": os.environ["GITHUB_SHA"], "ci_sha256": sha(__file__)}
        save(package / "BUILD-MANIFEST.json", manifest)
        for name in ("freeze.json", "EXPECTED-CASES.json"):
            shutil.copyfile(bundle / name, package / name)
        with tarfile.open(package / "herdr-candidate.tar.gz", "w:gz") as archive:
            archive.add(server["path"], arcname="herdr", recursive=False)
        save(output / "RESULT.json", {"passed": True, "build_manifest_sha256": sha(package / "BUILD-MANIFEST.json"),
            "archive_sha256": sha(package / "herdr-candidate.tar.gz"), "production_installed": False})
    except BaseException as error:
        save(output / "FAILURE.json", {"type": type(error).__name__, "message": str(error),
                                      "at_epoch": time.time(), "production_installed": False})
        raise


if __name__ == "__main__":
    main()
