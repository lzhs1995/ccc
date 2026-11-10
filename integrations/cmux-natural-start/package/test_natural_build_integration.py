"""Admission ordering and explicit evidence propagation without native work."""
from __future__ import annotations
import copy
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

import ci_release
import natural_source_admission as admission
import release_build
import source_contract as shared


class NaturalBuildIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.identity = {"repository": "lzhs1995/ccc", "head_sha": "a" * 40,
                    "run_id": 123456789, "run_attempt": 1}
        self.current = copy.deepcopy(admission.EXPECTED_SOURCE) | {
            "base_green_commit": shared.GREEN_COMMIT, "native_contract_sha256": "4" * 64,
            "native_verifier_sha256": "5" * 64}

    def test_plan_reports_new_tree_and_retains_original_build_commands_without_execution(self):
        with patch.object(shared, "current_source_contract", return_value=self.current), \
             patch.object(shared.subprocess, "run", side_effect=AssertionError("plan executed a command")):
            result = release_build.plan(self.root / "source", self.root / "build")
        self.assertEqual(result["source_tree"], self.current["green_tree"])
        self.assertEqual(result["after_current_tests_tree"], self.current["red_tree"])
        self.assertEqual(result["after_original_patches_tree"], shared.GREEN_TREE)
        self.assertTrue(result["current_native_evidence_required"])
        self.assertEqual(result["commands"][-1],
                         release_build.xcode_arguments(self.root / "source", self.root / "build") + ["build"])
        self.assertFalse(result["compiled"])

    def test_current_patch_chain_stops_before_green_when_red_tree_differs(self):
        steps = [("red", self.root / "restore-tests.patch", self.current["red_tree"]),
                 ("green", self.root / "full-fix.patch", self.current["green_tree"])]
        commands = []

        def run(argv):
            commands.append(argv)
            return "f" * 40 if argv == ["git", "write-tree"] else ""

        with patch.object(admission, "new_patch_steps", return_value=steps):
            with self.assertRaisesRegex(ValueError, "red tree"):
                release_build.apply_current_source_patches(run)
        self.assertEqual(len(commands), 3)
        self.assertFalse(any(str(steps[1][1]) in argv for argv in commands))

    def test_current_patch_chain_verifies_both_exact_trees_in_order(self):
        steps = [("red", self.root / "restore-tests.patch", self.current["red_tree"]),
                 ("green", self.root / "full-fix.patch", self.current["green_tree"])]
        trees = iter([row[2] for row in steps])
        commands = []

        def run(argv):
            commands.append(argv)
            return next(trees) if argv == ["git", "write-tree"] else ""

        with patch.object(admission, "new_patch_steps", return_value=steps):
            release_build.apply_current_source_patches(run)
        self.assertEqual([argv[-1] for argv in commands if argv[:3] == ["git", "apply", "--index"]],
                         [str(row[1]) for row in steps])
        self.assertEqual(sum(argv == ["git", "write-tree"] for argv in commands), 2)

    def test_compile_rejects_unadmitted_native_before_output_or_command_creation(self):
        workspace = self.root / "workspace"
        source = workspace / "source"
        source.mkdir(parents=True)
        temporary = self.root / "temporary"
        temporary.mkdir()
        output = temporary / "build"
        environment = {"GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temporary),
                       "GITHUB_REPOSITORY": self.identity["repository"], "GITHUB_SHA": self.identity["head_sha"],
                       "GITHUB_RUN_ID": str(self.identity["run_id"]),
                       "GITHUB_RUN_ATTEMPT": str(self.identity["run_attempt"])}
        runner = SimpleNamespace(runner_only=lambda: None)
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(shared, "original_admission", return_value=runner), \
             patch.object(release_build, "require_inputs", return_value={}), \
             patch.object(shared, "current_source_contract", return_value=self.current), \
             patch.object(admission, "admit_native_evidence", side_effect=ValueError("native did not pass")) as gate, \
             patch.object(shared, "Commands") as commands:
            with self.assertRaisesRegex(ValueError, "native did not pass"):
                release_build.compile_on_github(source, output, native_evidence=temporary / "native")
        gate.assert_called_once_with(temporary / "native", expected_run=self.identity)
        commands.assert_not_called()
        self.assertFalse(output.exists())

    def test_ci_rejects_native_before_gui_preflight_prior_cli_download_or_build(self):
        root = self.root / "attempt"
        root.mkdir()
        with patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted"}), \
             patch.object(shared, "run_identity", return_value=self.identity), \
             patch.object(shared, "check_contract", return_value={}), \
             patch.object(admission, "admit_native_evidence", side_effect=ValueError("native unavailable")), \
             patch.object(ci_release, "in_aqua") as aqua, \
             patch.object(ci_release.release_package, "download_prior_cli") as download, \
             patch.object(release_build, "compile_on_github") as compile_app:
            with self.assertRaisesRegex(ValueError, "native unavailable"):
                ci_release.run(root, self.root / "source", native_evidence=self.root / "native")
        aqua.assert_not_called()
        download.assert_not_called()
        compile_app.assert_not_called()
        failure = shared.read_json(root / "FAILURE.json")
        self.assertEqual(failure["stage"], "native-evidence-admission")
        self.assertEqual(failure["run"], self.identity)

    def test_ci_forwards_explicit_evidence_and_refuses_a_different_build_admission(self):
        root = self.root / "attempt"
        root.mkdir()
        evidence = self.root / "original-native-evidence"
        events = []
        summary = {"unit_test_only": "synthetic already-admitted object"}

        def admit(*args, **kwargs):
            events.append("native")
            return summary

        def build(*args, **kwargs):
            events.append("build")
            return {"native_admission_sha256": "0" * 64, "native_evidence": summary}

        with patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "github-hosted"}), \
             patch.object(shared, "run_identity", return_value=self.identity), \
             patch.object(shared, "check_contract", return_value={}), \
             patch.object(admission, "admit_native_evidence", side_effect=admit), \
             patch.object(ci_release, "in_aqua", side_effect=lambda *args: events.append("preflight")), \
             patch.object(ci_release.release_package, "download_prior_cli", side_effect=lambda *args: events.append("cli")), \
             patch.object(release_build, "compile_on_github", side_effect=build) as compile_app, \
             patch.object(ci_release.release_package, "package") as package_app:
            with self.assertRaisesRegex(ValueError, "differs between"):
                ci_release.run(root, self.root / "source", native_evidence=evidence)
        self.assertEqual(events, ["native", "preflight", "cli", "build"])
        compile_app.assert_called_once_with(self.root / "source", root / "build", native_evidence=evidence)
        package_app.assert_not_called()

    def test_compile_cli_cannot_omit_native_evidence(self):
        argv = ["release_build.py", "--source", str(self.root / "source"),
                "--output", str(self.root / "build"), "--compile-on-github"]
        with patch.object(release_build, "compile_on_github") as compile_app, patch("sys.argv", argv):
            with self.assertRaisesRegex(ValueError, "requires --native-evidence"):
                release_build.main()
        compile_app.assert_not_called()


    def test_published_verifier_and_package_admission_accept_same_complete_synthetic_evidence(self):
        # Reuse the native owner's fixture generator; this executes no Swift.
        fixture_path = admission.NATIVE_ROOT / "test_native.py"
        spec = importlib.util.spec_from_file_location("natural_build_native_fixture", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        prior_path = sys.path[:]
        try:
            sys.path.insert(0, str(admission.NATIVE_ROOT))
            sys.modules[spec.name] = fixture
            spec.loader.exec_module(fixture)
        finally:
            sys.path[:] = prior_path
        with fixture.synthetic_artifact() as (root, contract, contract_sha):
            summary = admission.admit_native_evidence(root.resolve(strict=True), expected_run=fixture.IDENTITY)
            self.assertEqual(summary["contract_sha256"], contract_sha)
            self.assertEqual(summary["source"]["green_tree"], contract["source"]["green_tree"])
            self.assertEqual(summary["native_success_sha256"], shared.digest(root / "NATIVE-SUCCESS.json"))
            self.assertEqual(shared.validate_native_summary(summary, expected_run=fixture.IDENTITY), summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
