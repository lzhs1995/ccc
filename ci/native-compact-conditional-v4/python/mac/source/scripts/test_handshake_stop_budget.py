"""Exercise the legacy Stop diagnostic's ACK budget with fixture-local identity."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class HandshakeStopBudgetTests(unittest.TestCase):
    def setUp(self):
        source = Path(os.environ.get("COLLAB_STOP_GUARD_UNDER_TEST", ""))
        if not source.is_file():
            source = Path(__file__).with_name("cmux_consensus_stop_guard.py")
        spec = importlib.util.spec_from_file_location("budget_stop_guard", source)
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.active = self.root / "active"
        self.active.mkdir()
        self.now = datetime.now(timezone.utc)
        self.ack = "PREFLIGHT_ACK|budget-task|claude:identity|READY|INLINE|nonce-a"
        self.marker = {
            "task_id": "budget-task", "artifact_root": str(self.root),
            "participants": [{"role": "executor", "provider": "claude",
                              "surface_uuid": "executor-fixture",
                              "surface_ref": "surface:2"}],
        }
        (self.active / "workspace-fixture.json").write_text(json.dumps(self.marker))
        (self.root / "task-pack.json").write_text(json.dumps({
            "draft": False, "completion_receipt": str(self.root / "missing.json")
        }))
        self.receipt = {
            "task_id": "budget-task", "executor_provider": "claude",
            "executor": "surface:2", "ack_nonce": "nonce-a",
            "lifecycle": "PENDING", "status": "HELLO_SENT",
            "ack_line_expected": self.ack,
            "dispatch_submitted_at": self.now.isoformat(),
            "created_at": self.now.isoformat(), "budget_seconds": 900,
        }

    def run_hook(self, *, age=0, surface="executor-fixture", ack=None):
        self.receipt["created_at"] = (self.now - timedelta(seconds=age)).isoformat()
        (self.root / "handshake-receipt.json").write_text(json.dumps(self.receipt))
        payload = {"hook_event_name": "Stop", "workspace_id": "workspace-fixture",
                   "surface_id": surface, "last_assistant_message": ack or self.ack}
        output = io.StringIO()
        # All identity and marker state is fixture-local; no live cmux is called.
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(self.guard, "ACTIVE_DIR", self.active), \
             patch.object(self.guard.hook_identity, "resolve", side_effect=lambda p: (
                 p["workspace_id"], p["surface_id"])), \
             patch.object(self.guard.hook_scope.runtime_audit, "record"), \
             patch.object(self.guard, "_now", return_value=self.now), \
             patch("sys.argv", ["stop_guard"]), \
             patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stderr(output):
            code = self.guard.main()
        return code, output.getvalue()

    def test_default_budget_ack_allows_stop_without_completing_task(self):
        self.receipt["budget_seconds"] = 600
        self.assertEqual(self.run_hook()[0], 0)
        self.assertFalse((self.root / "missing.json").exists())

    def test_extended_budget_allows_fresh_ack(self):
        self.assertEqual(self.run_hook()[0], 0)

    def test_extended_budget_allows_ack_after_default_deadline(self):
        self.assertEqual(self.run_hook(age=700)[0], 0)

    def test_expired_or_future_ack_is_rejected(self):
        for age in (901, -1):
            with self.subTest(age=age):
                self.assertEqual(self.run_hook(age=age)[0], 2)

    def test_nonfinite_or_invalid_budget_is_rejected(self):
        for budget in (True, None, "900", 0, -1, float("inf"), float("nan")):
            with self.subTest(budget=budget):
                self.receipt["budget_seconds"] = budget
                self.assertEqual(self.run_hook()[0], 2)

    def test_wrong_nonce_is_rejected(self):
        self.assertEqual(self.run_hook(ack=self.ack.replace("nonce-a", "nonce-b"))[0], 2)

    def test_wrong_executor_binding_is_rejected(self):
        self.receipt["executor"] = "surface:99"
        self.assertEqual(self.run_hook()[0], 2)

    def test_unsubmitted_ack_is_rejected(self):
        self.receipt["dispatch_submitted_at"] = None
        self.assertEqual(self.run_hook()[0], 2)


if __name__ == "__main__":
    unittest.main()
