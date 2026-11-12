"""New local-only build-contract checks; no Xcode, App, network or old tests."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import release_build as build
import source_contract as shared


class ReleaseBuildTests(unittest.TestCase):
    def test_fork_owner_survives_without_inherited_compiler_or_transport_overrides(self):
        original = {"GITHUB_REPOSITORY": "lzhs1995/ccc", "GITHUB_REPOSITORY_OWNER": "lzhs1995",
                    "GITHUB_SHA": "a" * 40, "HOME": "/runner/home", "PATH": "/usr/bin:/bin",
                    "CMUX_CI_XCODE_ALLOW_BELOW_FLOOR": "1", "CMUX_CI_REQUIRED_MACOS_SDK_MAJOR": "15",
                    "DEVELOPER_DIR": "/old/Xcode", "GHOSTTYKIT_URL": "https://wrong.invalid",
                    "GH_TOKEN": "synthetic-test-token", "OTHER_SWIFT_FLAGS": "-D DEBUG",
                    "CARGO_HOME": "/other/cargo", "GITHUB_ENV": "/job/environment"}
        before = dict(original)
        actual = build.build_environment(Path("/private/build"), original)
        self.assertEqual(actual["GITHUB_REPOSITORY_OWNER"], "lzhs1995")
        self.assertEqual(actual["GITHUB_SHA"], "a" * 40)
        self.assertEqual(actual["CMUX_CI_REQUIRED_MACOS_SDK_MAJOR"], "26")
        self.assertEqual(actual["GITHUB_ENV"], "/private/build/toolchain.env")
        for key in ("DEVELOPER_DIR", "GH_TOKEN", "GHOSTTYKIT_URL", "OTHER_SWIFT_FLAGS", "CARGO_HOME",
                    "CMUX_CI_XCODE_ALLOW_BELOW_FLOOR"):
            self.assertNotIn(key, actual)
        self.assertEqual(original, before)

    def test_owner_identity_must_be_present_and_consistent(self):
        base = {"GITHUB_REPOSITORY": "lzhs1995/ccc", "GITHUB_REPOSITORY_OWNER": "lzhs1995",
                "HOME": "/runner/home", "PATH": "/usr/bin"}
        for changes in ({"GITHUB_REPOSITORY_OWNER": ""}, {"GITHUB_REPOSITORY_OWNER": "manaflow-ai"},
                        {"GITHUB_REPOSITORY": "other/ccc"}, {"HOME": ""}, {"PATH": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build.build_environment(Path("/build"), base | changes)

    def settings(self):
        return [{"target": "cmux", "buildSettings": {
            "CONFIGURATION": "Release", "PRODUCT_BUNDLE_IDENTIFIER": "com.cmuxterm.app",
            "PRODUCT_NAME": "cmux", "FULL_PRODUCT_NAME": "cmux.app", "EXECUTABLE_NAME": "cmux",
            "ARCHS": "arm64", "SWIFT_OPTIMIZATION_LEVEL": "-O", "CODE_SIGNING_ALLOWED": "NO",
            "CODE_SIGN_ENTITLEMENTS": "Resources/cmux.entitlements"}}]

    def test_release_sdk_alias_has_same_identity_but_other_sdk_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            sdk = root / "MacOSX26.0.sdk"
            sdk.mkdir()
            alias = root / "MacOSX.sdk"
            alias.symlink_to(sdk.name)
            settings = self.settings()
            settings[0]["buildSettings"]["SDKROOT"] = str(alias)
            self.assertEqual(build.validate_settings(settings, sdk_path=str(sdk))["CONFIGURATION"], "Release")
            other = root / "MacOSX15.0.sdk"
            other.mkdir()
            with self.assertRaises(ValueError):
                build.validate_settings(settings, sdk_path=str(other))

    def test_debug_or_wrong_target_architecture_and_signature_config_are_rejected(self):
        bad = {"CONFIGURATION": "Debug", "PRODUCT_BUNDLE_IDENTIFIER": "com.cmuxterm.app.debug",
               "PRODUCT_NAME": "cmux DEV", "FULL_PRODUCT_NAME": "cmux DEV.app", "EXECUTABLE_NAME": "other",
               "ARCHS": "arm64 x86_64", "SWIFT_OPTIMIZATION_LEVEL": "-Onone", "CODE_SIGNING_ALLOWED": "YES",
               "CODE_SIGN_ENTITLEMENTS": "cmux.release.entitlements",
               "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG", "OTHER_SWIFT_FLAGS": "-D DEBUG",
               "GCC_PREPROCESSOR_DEFINITIONS": "DEBUG=0"}
        for key, value in bad.items():
            rows = self.settings()
            rows[0]["buildSettings"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                build.validate_settings(rows)
        for rows in ([], {}, [None], self.settings() * 2, [{"target": "cmux"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                build.validate_settings(rows)

    def test_default_plan_reuses_real_original_receipts_without_a_subprocess(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("plan must not execute")):
            result = build.plan(Path("/disposable/source"), Path("/disposable/output"))
        self.assertFalse(result["compiled"])
        self.assertEqual(result["after_original_patches_tree"], shared.GREEN_TREE)
        self.assertEqual(result["old_test_actions"], [])
        command = result["commands"][-1]
        self.assertEqual(command[command.index("-configuration") + 1], "Release")
        self.assertEqual(command[-1], "build")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", command)
        self.assertNotIn("test", command)
        self.assertNotIn("archive", command)
        self.assertFalse(result["uses_reload_script"])

    def test_checkout_pins_refuse_changed_bytes_and_symlink_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source"
            source.mkdir()
            folder = source / "scripts"
            folder.mkdir()
            payload = b"original source\n"
            path = folder / "select.sh"
            path.write_bytes(payload)
            pins = {"scripts/select.sh": (len(payload), hashlib.sha256(payload).hexdigest())}
            with patch.object(build, "BUILD_SOURCE_PINS", pins):
                self.assertEqual(len(build.verify_build_sources(source)), 1)
                path.write_bytes(b"changed! source\n")
                with self.assertRaises(ValueError):
                    build.verify_build_sources(source)
                path.write_bytes(payload)
                folder.rename(root / "elsewhere")
                folder.symlink_to(root / "elsewhere", target_is_directory=True)
                with self.assertRaises(ValueError):
                    build.verify_build_sources(source)

    def test_command_timeout_preserves_both_original_streams_and_has_no_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            commands = shared.Commands(Path(temp) / "commands")
            argv = [sys.executable, "-B", "-c",
                    "import os,time;os.write(1,b'partial-out\\x00');os.write(2,b'partial-err\\xff');time.sleep(30)"]
            with self.assertRaises(subprocess.TimeoutExpired):
                commands.run(argv, timeout=0.3)
            root = commands.root
            result = shared.read_json(root / "001.result.json")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIsNone(result["returncode"])
            self.assertEqual(len(list(root.glob("*.intent.json"))), 1)
            self.assertEqual(shared.read_json(root / "001.intent.json")["retries"], 0)
            for stream, expected in (("stdout", b"partial-out\0"), ("stderr", b"partial-err\xff")):
                self.assertEqual((root / result[stream]["file"]).read_bytes(), expected)
                self.assertEqual(result[stream]["sha256"], hashlib.sha256(expected).hexdigest())
                self.assertEqual(result[stream]["bytes"], len(expected))


if __name__ == "__main__":
    unittest.main(verbosity=2)
