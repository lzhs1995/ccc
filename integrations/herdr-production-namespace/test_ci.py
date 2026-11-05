"""Offline preparation gates only; no Herdr compilation or test execution."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("namespace_ci", HERE / "ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class Gates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.expected = {"nonignored": ["actual"], "ignored": ["kept_ignored"]}
        self.listing = {"rust-suites": {"herdr::bin/herdr": {
            "binary-path": "/immutable/unit", "testcases": {
                "actual": {"ignored": False}, "kept_ignored": {"ignored": True}}}}}

    def test_real_frozen_bundle_is_valid(self):
        self.assertEqual(ci.validate_bundle(HERE, ci.sha(HERE / "freeze.json"))["patch_sha256"], ci.PATCH_SHA)

    def test_freeze_bytes_pin_required(self):
        with self.assertRaises(ValueError):
            ci.validate_bundle(HERE, "0" * 64)

    def test_same_names_wrong_binary_rejected(self):
        self.listing["rust-suites"]["herdr::bin/herdr"]["binary-path"] = "/mutable/other"
        with self.assertRaises(ValueError):
            ci.require_case_set(self.listing, self.expected, "/immutable/unit")

    def test_cfg_case_change_cannot_silently_update_expected(self):
        for name, value in (("missing", None), ("new", {"ignored": False}),
                            ("kept_ignored", {"ignored": False})):
            listing = copy.deepcopy(self.listing)
            cases = listing["rust-suites"]["herdr::bin/herdr"]["testcases"]
            if value is None:
                cases.pop("actual")
            else:
                cases[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                ci.require_case_set(listing, self.expected, "/immutable/unit")

    def test_exact_current_binary_and_cases_accept(self):
        ci.require_case_set(self.listing, self.expected, "/immutable/unit")

    def test_junit_duplicate_skip_failure_wrong_binary_rejected(self):
        for body in ('<testcase name="actual" classname="herdr::bin/herdr"><failure/></testcase>',
                     '<testcase name="actual" classname="herdr::bin/herdr"><skipped/></testcase>',
                     '<testcase name="actual" classname="other"/>',
                     '<testcase name="actual" classname="herdr::bin/herdr"/>' * 2, ''):
            path = self.root / "junit.xml"
            path.write_text('<testsuites><testsuite>' + body + '</testsuite></testsuites>')
            with self.subTest(body=body), self.assertRaises(ValueError):
                ci.require_junit(path, self.expected)

    def test_ignored_is_not_inherited_as_passed(self):
        path = self.root / "junit.xml"
        path.write_text('<testsuites><testsuite><testcase name="actual" classname="herdr::bin/herdr"/></testsuite></testsuites>')
        self.assertEqual(ci.require_junit(path, self.expected)["successful_cases"], 1)
        expected = {"nonignored": ["actual", "kept_ignored"], "ignored": []}
        with self.assertRaises(ValueError):
            ci.require_junit(path, expected)

    def test_full_source_hash_detects_added_files_and_modes(self):
        source = self.root / "source"
        source.mkdir()
        file = source / "one.rs"
        file.write_text("fn main() {}")
        original = ci.source_manifest(source)
        file.chmod(0o755)
        self.assertNotEqual(original, ci.source_manifest(source))
        file.chmod(0o644)
        (source / "unexpected").write_text("new")
        self.assertNotEqual(original, ci.source_manifest(source))

    def test_source_checkpoint_keeps_exact_difference_before_rejecting(self):
        source = self.root / "source"
        source.mkdir()
        (source / "changed.rs").write_text("old")
        (source / "removed.rs").write_text("kept")
        original = ci.source_manifest(source)
        ci.source_checkpoint(source, original, self.root, "unchanged")
        (source / "changed.rs").write_text("new")
        (source / "removed.rs").unlink()
        (source / "target/nextest").mkdir(parents=True)
        (source / "target/nextest/unexpected").write_text("generated")
        with self.assertRaisesRegex(ValueError, "frozen source changed"):
            ci.source_checkpoint(source, original, self.root, "mutation")
        difference = json.loads((self.root / "SOURCE-DIFF-mutation.json").read_bytes())
        self.assertEqual([row["path"] for row in difference["added"]], ["target/nextest/unexpected"])
        self.assertEqual([row["path"] for row in difference["removed"]], ["removed.rs"])
        self.assertEqual([row["before"]["path"] for row in difference["changed"]], ["changed.rs"])
        self.assertEqual(json.loads((self.root / "SOURCE-mutation.json").read_bytes()), ci.source_manifest(source))


    def mirror_fixture(self):
        source = self.root / "source"
        vendor = source / "vendor/libghostty-vt"
        vendor.mkdir(parents=True)
        (vendor / "VERSION").write_text("fixture-version\n")
        (vendor / "build.zig").write_text("// frozen fixture, never compiled\n")
        output = self.root / "output";output.mkdir()
        real = self.root / "zig";real.write_text("not executable in these tests")
        spec = ci.prepare_zig_mirror(source, output, {"CARGO_TARGET_DIR": str(output / "target")},
                                     real, ci.source_manifest(source))
        spec_path = output / "ZIG-MIRROR-SPEC.json"
        argv = ["build", "--prefix", str(output / "target/debug/build/fixture/out/zig-out"), *spec["build_tail"]]
        return source, output, vendor, spec, spec_path, argv

    def call_wrapper(self, spec_path, argv, cwd):
        original = Path.cwd()
        try:
            os.chdir(cwd)
            with mock.patch.object(ci.os, "execv") as execute:
                ci.zig_wrapper_main(spec_path, ci.sha(spec_path), argv)
                return execute.call_args, Path.cwd()
        finally:
            os.chdir(original)

    def test_wrapper_preserves_exact_build_argv_and_switches_only_to_verified_mirror(self):
        source, output, vendor, spec, spec_path, argv = self.mirror_fixture()
        before = ci.source_manifest(source)
        call, cwd = self.call_wrapper(spec_path, argv, vendor)
        self.assertEqual(call.args, (spec["real_zig"], [spec["real_zig"], *argv]))
        self.assertEqual(cwd, Path(spec["mirror"]))
        self.assertEqual(ci.source_manifest(source), before)
        event = json.loads(Path(spec["invocations"]).read_text())
        self.assertEqual(event["event"], "exec_intent")
        self.assertEqual(event["original_cwd"], str(vendor))
        self.assertEqual(event["execution_cwd"], str(cwd))

    def test_wrapper_version_executes_real_zig_without_cwd_change(self):
        _, _, _, spec, spec_path, _ = self.mirror_fixture()
        call, cwd = self.call_wrapper(spec_path, ["version"], self.root)
        self.assertEqual(call.args, (spec["real_zig"], [spec["real_zig"], "version"]))
        self.assertEqual(cwd, self.root)

    def test_wrapper_rejects_foreign_cwd_changed_flags_fetch_and_outside_prefix(self):
        _, _, vendor, spec, spec_path, argv = self.mirror_fixture()
        variants = [(argv, self.root), (["fetch", "archive.tar.gz"], vendor),
                    ([*argv, "--system", "/tmp/packages"], vendor)]
        bad = argv.copy();bad[2] = "/tmp/out/zig-out";variants.append((bad, vendor))
        bad = argv.copy();bad[4] = "-Doptimize=Debug";variants.append((bad, vendor))
        for args, cwd in variants:
            with self.subTest(args=args, cwd=str(cwd)),self.assertRaises(ValueError):
                self.call_wrapper(spec_path, args, cwd)
        self.assertFalse(Path(spec["invocations"]).exists())

    def test_wrapper_rejects_changed_original_or_mirror_frozen_input(self):
        _, _, vendor, spec, spec_path, argv = self.mirror_fixture()
        for directory in [vendor, Path(spec["mirror"])]:
            target = directory / "VERSION";old = target.read_bytes();target.write_bytes(b"changed")
            try:
                with self.subTest(directory=str(directory)),self.assertRaises(ValueError):
                    self.call_wrapper(spec_path, argv, vendor)
            finally:target.write_bytes(old)
        self.assertFalse(Path(spec["invocations"]).exists())

    def test_wrapper_rejects_changed_real_binary_or_manifest_pin(self):
        _, _, vendor, spec, spec_path, argv = self.mirror_fixture()
        for target in [Path(spec["real_zig"]), Path(spec["inputs"])]:
            old = target.read_bytes();target.write_bytes(old + b"changed")
            try:
                with self.subTest(target=str(target)),self.assertRaises(ValueError):
                    self.call_wrapper(spec_path, argv, vendor)
            finally:target.write_bytes(old)
        with self.assertRaises(ValueError):ci.zig_wrapper_main(spec_path, "0" * 64, argv)

    def test_mirror_generated_packages_fully_recorded_without_source_exclusion(self):
        source, output, _, spec, _, _ = self.mirror_fixture()
        before = ci.source_manifest(source)
        mirror = Path(spec["mirror"]);package = mirror / "zig-pkg/exact-hash/one.zig"
        package.parent.mkdir(parents=True);package.write_text("package")
        ci.zig_mirror_checkpoint(output, spec, "test")
        packages = json.loads((output / "ZIG-PACKAGES-test.json").read_bytes())
        self.assertEqual([x["path"] for x in packages["files"]], ["zig-pkg/exact-hash/one.zig"])
        self.assertEqual(ci.source_manifest(source), before)
        (mirror / "unexpected").write_text("reject")
        with self.assertRaises(ValueError):ci.zig_mirror_checkpoint(output, spec, "bad")

    def test_original_vendor_cache_is_not_accepted_as_frozen_input(self):
        bad = {"files": [{"path":"vendor/libghostty-vt/zig-pkg/hidden", "kind":"file"}]}
        with self.assertRaises(ValueError):ci.zig_vendor_inputs(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
