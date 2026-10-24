#!/usr/bin/env python3
"""Validate actual offline harness receipts with JSON Schema 2020-12.

Test-only dependency: jsonschema==4.25.1. No live terminal or native RPC I/O.
"""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cmux_prompt_reference as reference
import mac_harness as harness


class HandshakeSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "schemas/handshake-receipt.schema.json").read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def generate(self, outcomes=("ACK",), *, timeout=None):
        snapshots = []
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            args = SimpleNamespace(task_id="schema-receipt", timeout=timeout,
                                   handshake_timeout=600, lines=40)
            gate = {"supervisor": "surface:1"}
            per_executor = []
            original_write = harness._write

            def write(path, value):
                original_write(path, value)
                if Path(path).name == "handshake-receipt.json":
                    snapshots.append(copy.deepcopy(value))

            for ordinal, outcome in enumerate(outcomes, 1):
                item = {"surface_ref": f"surface:{ordinal + 1}",
                        "provider": "claude", "ordinal": ordinal}
                nonce = f"nonce-{ordinal}"
                ack = f"PREFLIGHT_ACK|schema-receipt|claude:identity|READY|INLINE|{nonce}"
                reject = (harness.cmux.DispatchUnconfirmed("COMPOSE_OCCUPIED",
                           state=harness.cmux.COMPOSE_OCCUPIED)
                          if outcome == "NO_INPUT" else None)
                with patch.object(harness, "_write", side_effect=write), \
                        patch.object(harness.secrets, "token_hex", return_value=nonce), \
                        patch.object(reference, "body_root", return_value=root / "bodies"), \
                        patch.object(harness.cmux, "submit_text", side_effect=reject, return_value=None), \
                        patch.object(harness.cmux, "wait_for_ack", return_value=ack,
                                     side_effect=TimeoutError("offline timeout")
                                     if outcome == "TIMEOUT" else None):
                    success = harness._handshake_one(args, root, gate, item, per_executor)
                    harness._write_handshake_receipt(root, per_executor)
                if not success:
                    break
        return snapshots

    def test_emitted_pending_pass_rejected_and_expired_receipts(self):
        observed = set()
        for outcomes, timeout in ((("ACK",), None), (("NO_INPUT",), None),
                                  (("TIMEOUT",), None), (("TIMEOUT",), 1)):
            with self.subTest(outcomes=outcomes, timeout=timeout):
                for receipt in self.generate(outcomes, timeout=timeout):
                    self.validator.validate(receipt)
                    observed.add(receipt["lifecycle"])
        self.assertEqual(observed, {"PENDING", "ACKED", "REJECTED", "EXPIRED"})

    def test_emitted_multiple_executor_success_and_partial_failure(self):
        for outcomes in (("ACK", "ACK"), ("ACK", "TIMEOUT"), ("ACK", "NO_INPUT")):
            with self.subTest(outcomes=outcomes):
                receipts = self.generate(outcomes)
                for receipt in receipts:
                    self.validator.validate(receipt)
                final = receipts[-1]
                self.assertEqual(len(final["executors"]), 2)
                self.assertEqual(final["status"], "PASS" if outcomes[-1] == "ACK" else "FAIL")

    def test_legacy_receipt_without_new_probe_fields_remains_readable(self):
        receipt = self.generate()[-1]
        for row in [receipt] + receipt["executors"]:
            for field in ("bridge_probe_policy", "bridge_probe_status",
                          "bridge_clear_confirmed", "bridge_clear_recovery"):
                row.pop(field)
            row["clear_confirmed"] = True
            row["budget_source"] = "phase_default"
        self.validator.validate(receipt)

    def test_each_probe_state_has_consistent_clear_evidence(self):
        base = self.generate()[-1]
        for state, cleared, recovery in (("NOT_RUN", None, None), ("CLEARED", True, None),
                                        ("CLEAR_RECOVERED", True, {"path": "/original/receipt"})):
            with self.subTest(state=state):
                receipt = copy.deepcopy(base)
                for row in [receipt] + receipt["executors"]:
                    row.update(bridge_probe_status=state, bridge_clear_confirmed=cleared,
                               bridge_clear_recovery=recovery)
                self.validator.validate(receipt)

    def test_unrun_probe_cannot_claim_clearing_or_omit_evidence_fields(self):
        base = self.generate()[-1]
        for target in ("top", "executor"):
            for field, value in (("bridge_clear_confirmed", True),
                                 ("bridge_clear_recovery", {}),
                                 ("bridge_probe_status", "UNKNOWN"),
                                 ("bridge_probe_policy", "future-controller")):
                with self.subTest(target=target, field=field):
                    receipt = copy.deepcopy(base)
                    row = receipt if target == "top" else receipt["executors"][0]
                    row[field] = value
                    with self.assertRaises(ValidationError):
                        self.validator.validate(receipt)
            for field in ("bridge_probe_status", "bridge_clear_confirmed", "bridge_clear_recovery"):
                with self.subTest(target=target, missing=field):
                    receipt = copy.deepcopy(base)
                    row = receipt if target == "top" else receipt["executors"][0]
                    del row[field]
                    with self.assertRaises(ValidationError):
                        self.validator.validate(receipt)

    def test_bad_executor_or_budget_is_rejected(self):
        base = self.generate()[-1]
        for change in ({"executors": []}, {"executors": [{}]},
                       {"budget_seconds": 0}, {"budget_source": "invented"}):
            with self.subTest(change=change):
                receipt = copy.deepcopy(base)
                receipt.update(change)
                with self.assertRaises(ValidationError):
                    self.validator.validate(receipt)


if __name__ == "__main__":
    unittest.main()
