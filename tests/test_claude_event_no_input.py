"""A proven pre-text refusal must not consume a deferred Claude Stop."""

import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import (
    FakeClient, claude_armed_daemon, claude_grid_payload, claude_hook_event,
    claude_idle_screen, process_fixture,
)


class ClaudeEventNoInputTests(unittest.TestCase):
    def setUp(self):
        self.settle = mock.patch.object(core, "CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0)
        self.settle.start()
        self.addCleanup(self.settle.stop)

    def _fixture(self, directory):
        client = FakeClient(
            claude_grid_payload(), text=claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        daemon = claude_armed_daemon(directory, client)
        runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
        runtime.claude_session_id = "session-uuid"
        runtime.claude_hook_health = "healthy"
        daemon._defer_claude_event(
            "surface-uuid", runtime, claude_hook_event("ordinary-stop"), "composer_busy",
        )
        return daemon, client, runtime

    def _poll(self, daemon, client, runtime):
        return daemon._maybe_send_deferred_claude_stop(
            daemon.config["targets"][0], runtime,
            core.ScreenState("claude_stopped", message_kind="claude"), client,
        )

    def test_controller_zero_input_refusal_retries_original_stop_once(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._fixture(directory)
            original = dict(runtime.claude_deferred_event)
            with mock.patch.object(client, "send_text", side_effect=core.InputNotSentError(
                "controller admission changed before input",
            )):
                self.assertTrue(self._poll(daemon, client, runtime))
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.claude_deferred_event, original)
            self.assertEqual(runtime.claude_submit_phase, "none")
            self.assertTrue(self._poll(daemon, client, runtime))
            self.assertEqual(len(client.sent_text), 1)
            self.assertEqual(len(client.sent_keys), 1)
            self.assertEqual(daemon.claude_event_ledger.status_of("ordinary-stop"), "sent")
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertFalse(self._poll(daemon, client, runtime))
            self.assertEqual(len(client.sent_text), 1)

    def test_live_identity_refusal_is_durable_and_restores_orphan_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._fixture(directory)
            runtime.claude_orphan_enter_count = 1
            runtime.claude_orphan_enter_at = 123.0
            # This check runs before client.send_text: a process birth change
            # between preflight and text authorization must produce zero input.
            pinned = daemon._claude_send_process_identity(1234)
            with mock.patch.object(daemon, "_claude_send_process_identity", side_effect=[
                pinned, {**pinned, "generation": "changed-before-text"},
            ]):
                self.assertTrue(self._poll(daemon, client, runtime))
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.claude_orphan_enter_count, 1)
            self.assertEqual(runtime.claude_orphan_enter_at, 123.0)
            durable = core.ClaudeEventLedger(daemon.claude_event_ledger.path)
            row = durable.events["ordinary-stop"]
            self.assertEqual(row["status"], "deferred_input_not_sent")
            self.assertEqual(row["input_not_sent"]["phase"], "text")
            self.assertIn("before text", row["input_not_sent"]["reason"])
            self.assertTrue(self._poll(daemon, client, runtime))
            self.assertEqual(len(client.sent_text), 1)

    def test_failed_proof_persistence_keeps_original_reservation_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._fixture(directory)
            ledger = daemon.claude_event_ledger
            write = ledger._write

            def reject_proof(events=None):
                rows = ledger.events if events is None else events
                if "input_not_sent" in rows.get("ordinary-stop", {}):
                    raise OSError("proof commit failed")
                return write(events)

            with mock.patch.object(client, "send_text", side_effect=core.InputNotSentError(
                "input authorization changed",
            )), mock.patch.object(ledger, "_write", side_effect=reject_proof):
                with self.assertRaisesRegex(OSError, "proof commit failed"):
                    self._poll(daemon, client, runtime)
            self.assertEqual(client.sent, [])
            self.assertEqual(ledger.status_of("ordinary-stop"), "reserved")
            self.assertNotIn("input_not_sent", ledger.events["ordinary-stop"])
            self.assertEqual(core.ClaudeEventLedger(ledger.path).status_of("ordinary-stop"), "reserved")
            self.assertNotEqual(runtime.claude_submit_phase, "none")
            self.assertFalse(self._poll(daemon, client, runtime))
            self.assertEqual(client.sent, [])

    def test_lost_text_ack_remains_unknown_and_never_retypes(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._fixture(directory)
            send = client.send_text

            def write_then_lose_ack(*args):
                send(*args)
                raise core.CmuxError("text written; acknowledgement lost")

            with mock.patch.object(client, "send_text", side_effect=write_then_lose_ack):
                self.assertTrue(self._poll(daemon, client, runtime))
            self.assertTrue(runtime.claude_submit_write_unknown)
            self.assertEqual(daemon.claude_event_ledger.status_of("ordinary-stop"), "reserved")
            self.assertNotIn("input_not_sent", daemon.claude_event_ledger.events["ordinary-stop"])
            self.assertFalse(self._poll(daemon, client, runtime))
            self.assertEqual(len(client.sent_text), 1)
            self.assertEqual(client.sent_keys, [])

    def test_old_zero_input_proof_never_reopens_a_later_unknown_write(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._fixture(directory)
            with mock.patch.object(client, "send_text", side_effect=core.InputNotSentError(
                "first admission refused before text",
            )):
                self.assertTrue(self._poll(daemon, client, runtime))
            proof = dict(daemon.claude_event_ledger.events["ordinary-stop"]["input_not_sent"])
            send = client.send_text

            def write_then_lose_ack(*args):
                send(*args)
                raise core.CmuxError("second attempt text written; acknowledgement lost")

            with mock.patch.object(client, "send_text", side_effect=write_then_lose_ack):
                self.assertTrue(self._poll(daemon, client, runtime))
            durable = core.ClaudeEventLedger(daemon.claude_event_ledger.path)
            self.assertEqual(durable.status_of("ordinary-stop"), "reserved")
            self.assertEqual(durable.events["ordinary-stop"]["input_not_sent"], proof)
            self.assertTrue(runtime.claude_submit_write_unknown)
            self.assertFalse(self._poll(daemon, client, runtime))
            # A restart still observes the reservation, even though its old
            # zero-input evidence is retained for audit.
            daemon.claude_event_ledger = durable
            self.assertFalse(self._poll(daemon, client, runtime))
            self.assertEqual(len(client.sent_text), 1)
            self.assertEqual(client.sent_keys, [])


if __name__ == "__main__":
    unittest.main()
