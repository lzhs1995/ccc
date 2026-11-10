"""Prepare or run an app-only Release build of the already accepted source tree.

Default is a local plan with no subprocesses. --compile-on-github is restricted
to a clean separate exact-upstream checkout on the original hosted GitHub Mac.
It does not sign, install, launch, run old tests, or invoke reload.sh.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import re

ROOT = Path(__file__).resolve().parent
import source_contract as package

UPSTREAM = "2e109a1739d320fe36220afb3e7775b59c1acd47"
GREEN_TREE = "40e8ea03ce1435f11e90b35f98d554a13b9b8685"
GHOSTTY = "ef4f1570d62117ebc51e3b0f07ad4f34032267b6"
GHOSTTY_SHA256 = "ae38ade4e1c7e4d6ce21b8d7c16e9a95c346005848b4e2daa2afb6f56084c024"
PATCHES = {"tests.patch": "bdebf64e054476f3ea0476df3ec7acb91ddbfe86fc2927bb85686720c6a7bf8e",
           "fix.patch": "db4f2c96ff9e06fd47ddb55a102f0ccb45d00c691e9e3681ca722f3f97144422"}
BUILD_SECONDS = 180 * 60
need = package.need
BUILD_SOURCE_PINS = {
    "cmux.xcodeproj/project.pbxproj": (2634579, "c2ce7b22d2bb1f7746a5fd4483bd78d12846335a836972bee8fd1878f8f7e2fe"),
    "cmux.xcodeproj/xcshareddata/xcschemes/cmux.xcscheme": (2741, "42134ff616da80f021f0b2773e4de887edfbafb701166d5cc6adc9ef863a9a0b"),
    "scripts/select-ci-xcode.sh": (15257, "035e398c4f4be4f80732f069bbba3cf1709c92dc1004a3d13e4d40333e9edd7a"),
    "scripts/install-rust-ci.sh": (1130, "6ef6ece19cd8c4372ec9bad79e1cd9b2f9c41bce6fdeb87f7c3f2b13030d87f3"),
    "scripts/download-prebuilt-ghosttykit.sh": (7729, "266879ce4ece3ef0e324e3057dbb2d5781db0f1594a547437d6834a6cb75f786"),
    "scripts/ghosttykit-checksums.txt": (16313, "056df074a16a5dc3d108bf0920b23528c3da5f30148ef92e0a78c00ab970fcf8"),
}


def build_environment(output, inherited):
    """Keep the authenticated hosted-repository owner for upstream fork selection.

    The caller's cmux, signing, downloader and compiler overrides do not enter
    this build. GITHUB_ENV/PATH are private files, not the job's global channel.
    """
    need(inherited.get("GITHUB_REPOSITORY") == "lzhs1995/ccc"
         and inherited.get("GITHUB_REPOSITORY_OWNER") == "lzhs1995",
         "original hosted repository owner missing or inconsistent")
    keys = ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG",
            "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER", "GITHUB_SHA",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKSPACE", "RUNNER_NAME",
            "RUNNER_TEMP", "RUNNER_ENVIRONMENT")
    env = {key: inherited[key] for key in keys if key in inherited}
    need(bool(env.get("HOME")) and bool(env.get("PATH")), "runner HOME/PATH missing")
    env.update(CMUX_SKIP_ZIG_BUILD="1", CMUX_GHOSTTYKIT_PREPROVISIONED="1", CMUX_CI_REQUIRED_MACOS_SDK_MAJOR="26",
               GHOSTTYKIT_DOWNLOAD_RETRIES="2", GHOSTTYKIT_DOWNLOAD_RETRY_DELAY="2",
               GHOSTTYKIT_DOWNLOAD_CONNECT_TIMEOUT="10", GHOSTTYKIT_DOWNLOAD_MAX_TIME="300")
    env["GITHUB_ENV"] = str(output / "toolchain.env")
    env["GITHUB_PATH"] = str(output / "toolchain.path")
    return env


def verify_build_sources(source):
    """Read the fixed checkout; diagnostic download caches are not dependencies."""
    rows = []
    for relative, (size, expected) in BUILD_SOURCE_PINS.items():
        path = source / relative
        need(path.is_file() and not path.is_symlink() and path.resolve(strict=True) == path,
             "fixed build source is not a regular contained file: " + relative)
        need(path.stat().st_size == size and package.digest(path) == expected,
             "fixed build source differs: " + relative)
        rows.append({"path": relative, "sha256": expected, "bytes": size})
    return rows


def xcode_arguments(source, output):
    return ["/usr/bin/xcodebuild", "-project", str(source / "cmux.xcodeproj"),
            "-scheme", "cmux", "-configuration", "Release",
            "-derivedDataPath", str(output / "derived"),
            "-destination", "generic/platform=macOS",
            "-sdk", "macosx",
            "-clonedSourcePackagesDirPath", str(output / "spm-cache"),
            "ARCHS=arm64", "ONLY_ACTIVE_ARCH=YES", "CODE_SIGNING_ALLOWED=NO"]


def require_inputs():
    pin = package.original_pin()
    need(pin["upstream_commit"] == UPSTREAM and pin["green_source_tree"] == GREEN_TREE, "original green tree differs")
    for name, expected in PATCHES.items():
        need(package.digest(ROOT / "inputs" / name) == expected, "original patch changed: " + name)
    return pin


def validate_settings(rows, *, sdk_path=None):
    need(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "malformed build settings")
    selected = [row.get("buildSettings") for row in rows if row.get("target") == "cmux"]
    need(len(selected) == 1, "cmux target settings not unique")
    settings = selected[0]
    need(isinstance(settings, dict), "malformed cmux build settings")
    conditions = " ".join(str(settings.get(key, "")) for key in
        ("SWIFT_ACTIVE_COMPILATION_CONDITIONS", "GCC_PREPROCESSOR_DEFINITIONS", "OTHER_SWIFT_FLAGS"))
    need(not re.search(r"(?<![A-Za-z0-9_])(?:-D)?DEBUG(?:=1)?(?![A-Za-z0-9_])", conditions),
         "Release contains DEBUG compile conditions")
    need(settings.get("CONFIGURATION") == "Release"
         and settings.get("PRODUCT_BUNDLE_IDENTIFIER") == "com.cmuxterm.app"
         and settings.get("PRODUCT_NAME") == "cmux"
         and settings.get("FULL_PRODUCT_NAME") == "cmux.app"
         and settings.get("EXECUTABLE_NAME") == "cmux"
         and settings.get("ARCHS") == "arm64"
         and settings.get("SWIFT_OPTIMIZATION_LEVEL") == "-O"
         and settings.get("CODE_SIGNING_ALLOWED") == "NO"
         and settings.get("CODE_SIGN_ENTITLEMENTS") == "Resources/cmux.entitlements",
         "not the fixed native Release configuration")
    if sdk_path is not None:
        resolved_sdk = Path(settings.get("SDKROOT", ""))
        need(resolved_sdk.is_absolute() and resolved_sdk.is_dir()
             and resolved_sdk.resolve(strict=True) == Path(sdk_path).resolve(strict=True),
             "resolved Release SDK differs from selected SDK 26")
    return settings


def plan(source, output):
    pin = require_inputs()
    return {"schema": "cmux-release-app-only-build-recipe-v1", "status": "PLAN_NOT_RUN",
            "source": str(source), "source_base_commit": UPSTREAM,
            "after_original_patches_tree": GREEN_TREE, "original_green_commit": pin["green_source_commit"],
            "source_test_receipts_sha256": pin["test_receipts_sha256"],
            "architecture": ["arm64"], "configuration": "Release",
            "build_source_pins": BUILD_SOURCE_PINS,
            "repository_owner_required": "lzhs1995",
            "commands": [["./scripts/select-ci-xcode.sh"], ["./scripts/ensure-metal-toolchain.sh"],
                         ["./scripts/install-rust-ci.sh"], ["./scripts/download-prebuilt-ghosttykit.sh"],
                         xcode_arguments(source, output) + ["build"]],
            "requires_preinstalled_tools": {"Go": "1.26.x", "Bun": "1.3.14"},
            "old_test_actions": [], "uses_reload_script": False,
            "compile_timeout_seconds": BUILD_SECONDS, "compiled": False,
            "signature_and_new_package_pin_complete": False, "runtime_smoke_complete": False,
            "remaining": ["Review and apply local Release metadata including exact original stable socket and automation mode.",
                          "Sign local Release package ad-hoc using its actual nested-code closure; no upstream Team claim.",
                          "Create new complete manifest/archive/readback/CDHash pin. Old Debug APP-PIN cannot admit Release.",
                          "Adapt/independently accept new package runtime gate for Release and test exact old cached socket.",
                          "Verify original CCC guarded control path and original four-surface first-adoption ownership."]}


def compile_on_github(source, output):
    package.original_admission().runner_only()
    require_inputs()
    source = source.resolve(strict=True)
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    need(source != workspace and source.is_relative_to(workspace) and source.is_dir(),
         "separate source checkout inside the disposable workspace required")
    need(source.stat().st_uid == os.geteuid(), "source checkout is not owned by the runner")
    need(output.parent.resolve(strict=True).is_relative_to(temporary)
         and not output.exists() and not output.is_symlink(), "fresh private build output required")
    need(output.parent.resolve(strict=True).stat().st_uid == os.geteuid(), "build parent is not runner-owned")
    env = build_environment(output, os.environ)
    output.mkdir(mode=0o700)
    commands = package.Commands(output / "commands", cwd=source, environment=env)
    def run(argv, timeout=60):
        return commands.text(argv, timeout=timeout)
    need(run(["git", "rev-parse", "HEAD"]).strip() == UPSTREAM, "source base is not fixed upstream")
    need(not run(["git", "status", "--porcelain", "--untracked-files=normal"]).strip(), "source checkout is not clean")
    need(run(["git", "rev-parse", "HEAD:ghostty"]).strip() == GHOSTTY
         and run(["git", "-C", "ghostty", "rev-parse", "HEAD"]).strip() == GHOSTTY, "Ghostty submodule differs")
    for name in PATCHES:
        path = ROOT / "inputs" / name
        run(["git", "apply", "--check", str(path)])
        run(["git", "apply", "--index", str(path)])
    need(run(["git", "write-tree"]).strip() == GREEN_TREE, "fixed patches did not reproduce accepted green content")
    source_pins = verify_build_sources(source)
    package.save(output / "SOURCE-CLOSURE.json", {"source_upstream": UPSTREAM, "green_tree": GREEN_TREE,
        "original_green_commit": package.GREEN_COMMIT, "patches": PATCHES,
        "ghostty_submodule": GHOSTTY, "ghosttykit_archive_sha256": GHOSTTY_SHA256,
        "source_app_pin_sha256": package.SOURCE_PIN_SHA256,
        "reused_original_test_receipts_sha256": require_inputs()["test_receipts_sha256"],
        "run": package.run_identity(), "original_tests_rerun": 0,
        "build_source_pins": source_pins,
        "repository_owner_for_original_toolchain_selector": env["GITHUB_REPOSITORY_OWNER"],
        "configuration": "Release", "swift_logic_changes_beyond_original_fix": 0})
    run(["./scripts/select-ci-xcode.sh"])
    selected = (output / "toolchain.env").read_text().splitlines()
    developer = [line.split("=", 1)[1] for line in selected if line.startswith("DEVELOPER_DIR=")]
    need(len(developer) == 1 and Path(developer[0]).is_dir(), "official toolchain selection missing")
    env["DEVELOPER_DIR"] = developer[0]
    version = run(["/usr/bin/xcodebuild", "-version"])
    need(re.match(r"Xcode 26(?:\.|\n|$)", version), "different Xcode major from original compiled source")
    sdk_version = run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"]).strip()
    need(re.fullmatch(r"26(?:\.[0-9]+)*", sdk_version), "selected SDK is not macOS 26")
    sdk_path = run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"]).strip()
    need(Path(sdk_path).is_absolute() and Path(sdk_path).is_dir(), "selected SDK path missing")
    need(run(["go", "version"]).startswith("go version go1.26."), "Release app requires official Go 1.26.x setup")
    need(run(["bun", "--version"]).strip() == "1.3.14", "Release app requires pinned Bun 1.3.14")
    run(["./scripts/ensure-metal-toolchain.sh"], 900)
    run(["./scripts/install-rust-ci.sh"], 900)
    cargo_bin = Path(env["HOME"]) / ".cargo/bin"
    need(cargo_bin.is_dir(), "official Rust installer did not provision its toolchain")
    env["PATH"] = str(cargo_bin) + os.pathsep + env["PATH"]
    lines = (source / "scripts/ghosttykit-checksums.txt").read_text().splitlines()
    found = [line.split()[1] for line in lines if len(line.split()) >= 2 and line.split()[0] == GHOSTTY]
    need(found == [GHOSTTY_SHA256], "prebuilt GhosttyKit checksum differs")
    run(["./scripts/download-prebuilt-ghosttykit.sh"], 1020)
    need((source / "GhosttyKit.xcframework").is_dir(), "verified prebuilt GhosttyKit missing")
    argv = xcode_arguments(source, output)
    settings = validate_settings(json.loads(run(argv + ["-showBuildSettings", "-json"], 300)), sdk_path=sdk_path)
    package.save(output / "RESOLVED-RELEASE-SETTINGS.json", {key: settings.get(key) for key in
        ("CONFIGURATION", "PRODUCT_BUNDLE_IDENTIFIER", "SWIFT_ACTIVE_COMPILATION_CONDITIONS",
         "PRODUCT_NAME", "EXECUTABLE_NAME", "FULL_PRODUCT_NAME", "SDKROOT", "ARCHS", "CODE_SIGNING_ALLOWED",
         "CODE_SIGN_ENTITLEMENTS", "ENABLE_HARDENED_RUNTIME", "SWIFT_OPTIMIZATION_LEVEL",
         "SWIFT_COMPILATION_MODE", "GCC_PREPROCESSOR_DEFINITIONS", "OTHER_SWIFT_FLAGS", "DEVELOPER_DIR")})
    build = commands.run(argv + ["build"], timeout=BUILD_SECONDS)
    package.save(output / "BUILD-COMMAND.json", build)
    need(run(["git", "write-tree"]).strip() == GREEN_TREE, "build changed the accepted index")
    run(["git", "diff", "--exit-code", "--quiet", "--ignore-submodules=none"])
    need(verify_build_sources(source) == source_pins, "build changed pinned build sources")
    package.save(output / "BUILD-SOURCE-PINS.json", {"files": source_pins + [
        {"path": str(path.relative_to(source)), "sha256": package.digest(path), "bytes": path.stat().st_size}
        for path in (source / "Resources/cmux.entitlements",
                     source / "cmux.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved")],
        "submodules": run(["git", "submodule", "status", "--recursive"]),
        "tracked_source_unchanged_after_build": True})
    app = output / "derived/Build/Products/Release/cmux.app"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    need(info["CFBundleIdentifier"] == "com.cmuxterm.app", "Release bundle id differs")
    need(run(["/usr/bin/lipo", "-archs", str(app / "Contents/MacOS" / info["CFBundleExecutable"])]).strip() == "arm64",
         "wrong architecture")
    package.save(output / "UNSIGNED-BUNDLE-MANIFEST.json", package.original_admission().inventory(app))
    result = {"status": "RELEASE_BUILT_UNSIGNED_NOT_PACKAGED_OR_RUNTIME_ACCEPTED",
        "build_passed": True, "source_tree": GREEN_TREE, "app": str(app), "configuration": "Release",
        "run": package.run_identity(), "build_elapsed_seconds": build["elapsed_seconds"],
        "resolved_settings_sha256": package.digest(output / "RESOLVED-RELEASE-SETTINGS.json"),
        "unsigned_manifest_sha256": package.digest(output / "UNSIGNED-BUNDLE-MANIFEST.json"),
        "source_test_receipts_reused": True, "old_tests_rerun": 0, "swift_logic_changes_beyond_original_fix": 0,
        "signature_verified": False, "new_package_pin": None, "runtime_verified": False, "installed": False}
    package.save(output / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compile-on-github", action="store_true")
    args = parser.parse_args()
    result = compile_on_github(args.source, args.output) if args.compile_on_github else plan(args.source, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
