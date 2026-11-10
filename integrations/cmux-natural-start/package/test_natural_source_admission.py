"""Offline admission tests with synthetic receipts; no native tests or App."""
from __future__ import annotations
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import natural_source_admission as admission
import source_contract as shared


class NaturalSourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.native = self.root / "native"
        self.native.mkdir()
        self.code_pins = {}
        for relative in admission.REQUIRED_VERIFIER_FILES:
            path = self.native / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raise AssertionError('fixture code must not be executed')\n")
            self.code_pins[relative] = {"sha256": shared.digest(path), "bytes": path.stat().st_size}
        inputs = {}
        for relative in ("inputs/restore-tests.patch", "inputs/full-fix.patch", "inputs/source-freeze.json"):
            path = self.native / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((admission.NATIVE_ROOT / relative).read_bytes())
            inputs[relative] = {"sha256": shared.digest(path), "bytes": path.stat().st_size}
        self.contract = {"schema": "cmux-natural-start-native-contract-v1",
            "status": "FROZEN_READY_FOR_NATIVE_EXECUTION", "source": copy.deepcopy(admission.EXPECTED_SOURCE),
            "input_pins": inputs}
        self.write_json(self.native / "contract.json", self.contract)
        for name, value in (("NATIVE_ROOT", self.native), ("NATIVE_CODE_PINS", self.code_pins),
                            ("NATIVE_CONTRACT_SHA256", shared.digest(self.native / "contract.json"))):
            setter = patch.object(admission, name, value)
            setter.start()
            self.addCleanup(setter.stop)
        self.identity = {"repository": "lzhs1995/ccc", "head_sha": "a" * 40,
                    "run_id": 123456789, "run_attempt": 1}

    def write_json(self, path, value):
        path.write_text(json.dumps(value, sort_keys=True) + "\n")

    def summary(self):
        source = copy.deepcopy(admission.EXPECTED_SOURCE)
        source.update(base_commit="1" * 40, red_commit="2" * 40, green_commit="3" * 40,
                      base_parent_commit=shared.UPSTREAM, red_parent_commit="1" * 40,
                      green_parent_commit="2" * 40)
        return {"schema": admission.SUMMARY_SCHEMA, "passed": True, "run": dict(self.identity),
                "source": source, "contract_sha256": admission.NATIVE_CONTRACT_SHA256,
                "native_success_sha256": "4" * 64, "manifest_sha256": "5" * 64,
                "verifier_sha256": self.code_pins["evidence_native.py"]["sha256"]}

    def evidence(self):
        root = self.root / "native-data"
        root.mkdir()
        summary = self.summary()
        receipt = {"schema": "cmux-natural-start-native-success-v1",
                   "status": "NATIVE_RED_GREEN_AND_ADJACENT_PASSED", "run": summary["run"],
                   "source": summary["source"], "contract_sha256": summary["contract_sha256"]}
        self.write_json(root / "NATIVE-SUCCESS.json", receipt)
        self.write_json(root / "MANIFEST.json", {"synthetic_unit_test_fixture": True})
        verified = receipt | {"passed": True, "success_sha256": shared.digest(root / "NATIVE-SUCCESS.json"),
                              "manifest_sha256": shared.digest(root / "MANIFEST.json")}
        return root, verified

    def test_current_source_keeps_original_base_and_never_executes_published_or_artifact_code(self):
        source = shared.current_source_contract()
        self.assertEqual(source["base_green_tree"], shared.GREEN_TREE)
        self.assertEqual(source["base_green_commit"], shared.GREEN_COMMIT)
        self.assertEqual(shared.current_source_tree(), admission.EXPECTED_SOURCE["green_tree"])
        self.assertNotEqual(source["green_tree"], shared.GREEN_TREE)
        self.assertEqual([row[0] for row in admission.new_patch_steps()], ["red", "green"])
        self.assertEqual(len(admission.source_file_pins()), 6)

    def test_pending_code_pins_and_pending_contract_do_not_fall_back_to_old_tree(self):
        with patch.object(admission, "NATIVE_CODE_PINS", {}), self.assertRaisesRegex(ValueError, "PENDING"):
            shared.current_source_tree()
        self.contract["status"] = "SOURCE_PINS_PENDING"
        self.write_json(self.native / "contract.json", self.contract)
        with patch.object(admission, "NATIVE_CONTRACT_SHA256", shared.digest(self.native / "contract.json")):
            with self.assertRaisesRegex(ValueError, "PENDING"):
                shared.current_source_tree()

    def test_even_repinned_foreign_source_contract_cannot_replace_the_current_chain(self):
        self.contract["source"]["green_tree"] = shared.GREEN_TREE
        self.write_json(self.native / "contract.json", self.contract)
        with patch.object(admission, "NATIVE_CONTRACT_SHA256", shared.digest(self.native / "contract.json")):
            with self.assertRaisesRegex(ValueError, "source chain"):
                shared.current_source_contract()

    def test_changed_published_code_patch_or_symlink_parent_is_rejected(self):
        for relative in ("evidence_native.py", "inputs/full-fix.patch"):
            path = self.native / relative
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            try:
                with self.subTest(path=relative), self.assertRaises(ValueError):
                    shared.current_source_contract()
            finally:
                path.write_bytes(original)
        outside = self.root / "outside-upstream"
        (self.native / "upstream").rename(outside)
        (self.native / "upstream").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked"):
            shared.current_source_contract()

    def test_summary_requires_exact_run_types_source_patches_and_actual_commit_chain(self):
        good = self.summary()
        self.assertEqual(shared.validate_native_summary(good, expected_run=self.identity), good)
        mutations = [
            lambda x: x.update(passed=1),
            lambda x: x.update(extra="unreviewed"),
            lambda x: x["run"].update(run_attempt=True),
            lambda x: x["run"].update(run_attempt=2),
            lambda x: x["run"].update(run_id=123456790),
            lambda x: x["run"].update(head_sha="b" * 40),
            lambda x: x["source"].update(green_tree=shared.GREEN_TREE),
            lambda x: x["source"]["patches"].update(full_fix="0" * 64),
            lambda x: x["source"].update(base_parent_commit="0" * 40),
            lambda x: x["source"].update(green_parent_commit="1" * 40),
            lambda x: x["source"].update(green_commit="2" * 40),
            lambda x: x["source"].update(base_commit=shared.UPSTREAM, red_parent_commit=shared.UPSTREAM),
            lambda x: x["source"].update(red_commit=shared.UPSTREAM, green_parent_commit=shared.UPSTREAM),
            lambda x: x.update(contract_sha256="0" * 64),
            lambda x: x.update(verifier_sha256="0" * 64),
            lambda x: x.update(native_success_sha256=""),
            lambda x: x.update(manifest_sha256=True),
        ]
        for mutate in mutations:
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.subTest(summary=bad), self.assertRaises(ValueError):
                shared.validate_native_summary(bad, expected_run=self.identity)
        with self.assertRaises(ValueError):
            shared.validate_native_summary(good, expected_run=self.identity | {"run_attempt": True})

    def test_full_verifier_receives_the_original_artifact_and_exact_expected_context(self):
        root, verified = self.evidence()
        verify = Mock(return_value=verified)
        with patch.object(admission, "load_evidence_verifier", return_value=SimpleNamespace(verify=verify)):
            summary = admission.admit_native_evidence(root, expected_run=self.identity)
        verify.assert_called_once_with(root, expected_run=self.identity,
            expected_source=admission.EXPECTED_SOURCE, expected_contract_sha256=admission.NATIVE_CONTRACT_SHA256)
        self.assertEqual(summary["native_success_sha256"], shared.digest(root / "NATIVE-SUCCESS.json"))
        self.assertEqual(summary["manifest_sha256"], shared.digest(root / "MANIFEST.json"))

    def test_nonpassing_or_unknown_full_verdict_is_not_retried_or_replaced_with_a_summary(self):
        root, _ = self.evidence()
        for result in ({"passed": False}, {"passed": 1}, TimeoutError("original verification unknown")):
            verify = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            with patch.object(admission, "load_evidence_verifier", return_value=SimpleNamespace(verify=verify)):
                with self.subTest(result=str(result)), self.assertRaises((ValueError, TimeoutError)):
                    admission.admit_native_evidence(root, expected_run=self.identity)
            self.assertEqual(verify.call_count, 1)

    def test_success_receipt_mutation_after_full_verification_is_detected(self):
        root, verified = self.evidence()
        original = (root / "NATIVE-SUCCESS.json").read_bytes()

        def changed(*args, **kwargs):
            (root / "NATIVE-SUCCESS.json").write_bytes(original + b"\n")
            return verified

        with patch.object(admission, "load_evidence_verifier", return_value=SimpleNamespace(verify=changed)):
            with self.assertRaisesRegex(ValueError, "changed after"):
                admission.admit_native_evidence(root, expected_run=self.identity)

    def test_success_receipt_cannot_hide_boolean_attempt_behind_normalized_verifier_output(self):
        root, verified = self.evidence()
        receipt = shared.read_json(root / "NATIVE-SUCCESS.json")
        receipt["run"]["run_attempt"] = True
        self.write_json(root / "NATIVE-SUCCESS.json", receipt)
        verified["success_sha256"] = shared.digest(root / "NATIVE-SUCCESS.json")
        with patch.object(admission, "load_evidence_verifier", return_value=SimpleNamespace(verify=lambda *args, **kwargs: verified)):
            with self.assertRaises(ValueError):
                admission.admit_native_evidence(root, expected_run=self.identity)


if __name__ == "__main__":
    unittest.main(verbosity=2)
