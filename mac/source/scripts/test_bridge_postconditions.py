"""Regression tests for observed cleanup and pending handshake receipts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_r3_hardening as r3

H = r3.HARNESS
TOKEN = "B1_01234567"


class CleanupTests(r3.OfflineWorkspaceFixture):
    def run_probe(self, screens, success, force=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r3.gate(root)
            with (
                mock.patch.object(H.cmux, "send_text") as paste,
                mock.patch.object(H.cmux, "send_key") as keys,
                mock.patch.object(H.cmux, "focus_surface"),
                mock.patch.object(H.cmux, "clear_known_compose_by_delete", return_value=256),
                mock.patch.object(H.cmux, "read_screen", side_effect=screens),
                mock.patch.object(H.time, "sleep"),
            ):
                if success:
                    H.cmd_bridge_test(r3.args(root, force_compose=force))
                else:
                    with self.assertRaises(SystemExit) as exc:
                        H.cmd_bridge_test(r3.args(root, force_compose=force))
                    self.assertEqual(exc.exception.code, 1)
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            paste.assert_called_once_with("surface:2", TOKEN)
            self.assertEqual(ev["clear_confirmed"], success)
            self.assertNotIn("enter", [c.args[1] for c in keys.call_args_list])
            return ev, [c.args[1] for c in keys.call_args_list]

    def test_partial_prefix_is_cleaned_and_observed_before_success(self):
        ev, keys = self.run_probe(["❯ ", "❯ " + TOKEN, "❯ B1_012", "❯ "], True)
        self.assertEqual(keys.count("backspace"), len(TOKEN) + len("B1_012"))
        self.assertEqual(ev["clear_attempts"], 2)
        self.assertEqual(ev["clear_confirmed_by"], "EMPTY_COMPOSE")

    def test_persistent_prefix_is_not_false_success(self):
        ev, _ = self.run_probe(["❯ ", "❯ " + TOKEN] + ["❯ B1_012"] * 3, False)
        self.assertIsNone(ev["clear_verified_at"])
        self.assertEqual(ev["clear_attempts"], 3)

    def test_missing_glyph_is_only_reread_never_cleared_or_accepted(self):
        ev, keys = self.run_probe(["❯ "] + ["transient screen without prompt"] * 4, False)
        self.assertEqual(keys, [])
        self.assertTrue(all(not o["compose_observed"] for o in ev["clear_observations"]))

    def test_missing_glyph_can_recover_to_observed_empty_without_keys(self):
        ev, keys = self.run_probe(["❯ ", "transient screen", "❯ "], True)
        self.assertEqual(keys, [])
        self.assertEqual(ev["clear_confirmed_by"], "EMPTY_COMPOSE")

    def test_foreign_and_queued_text_are_never_deleted(self):
        for body in ("❯ user draft", "❯ " + TOKEN + " user draft",
                     "❯ " + TOKEN + "\n  ✻ Running tool",
                     "❯ \n  Press up to edit queued messages"):
            with self.subTest(body=body):
                ev, keys = self.run_probe(["❯ "] + [body] * 4, False)
                self.assertEqual(keys, [])
                self.assertEqual(ev["clear_key_count"], 0)

    def test_force_restores_actual_pre_paste_suggestion(self):
        old, current = "❯ old suggestion", "❯ new suggestion"
        ev, _ = self.run_probe([old] * 4 + [current, "❯ " + TOKEN, current], True, True)
        self.assertEqual(ev["clear_confirmed_by"], "AUTHORIZED_PRE_PASTE_RESTORED")

    def test_force_cannot_accept_old_suggestion_when_pre_paste_changed(self):
        old, current = "❯ old suggestion", "❯ new suggestion"
        ev, keys = self.run_probe([old] * 4 + [current, "❯ " + TOKEN] + [old] * 3, False, True)
        self.assertEqual(keys.count("backspace"), len(TOKEN))
        self.assertFalse(ev["clear_confirmed"])


class PendingReceiptTests(r3.OfflineWorkspaceFixture):
    def test_live_wait_persists_pending_until_exact_ack(self):
        for late in (False, True):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                r3.gate(root)
                r3.bridge_evidence(root)
                expected = "PREFLIGHT_ACK|r3-test|claude:identity|READY|INLINE|abcd"

                def check_wait(*args, **kwargs):
                    receipt = json.loads((root / "handshake-receipt.json").read_text())
                    self.assertEqual(receipt["status"], "AWAITING_EXECUTOR_ACK")
                    self.assertEqual(receipt["lifecycle"], "PENDING")
                    self.assertFalse(receipt["executor_ack"])
                    self.assertNotIn("error", receipt)
                    self.assertIsNone(receipt["terminal_error_at"])
                    self.assertEqual(kwargs["timeout"], 600)
                    self.assertEqual(kwargs["nonce"], "abcd")
                    if late:
                        self.assertEqual(receipt["dispatch_error"], "detector unconfirmed")
                        self.assertIsNone(receipt["dispatch_submitted_at"])
                    return expected

                dispatch_error = H.cmux.DispatchUnconfirmed(
                    "detector unconfirmed", state=H.cmux.DELIVERY_UNVERIFIED_BY_DETECTOR)
                with (
                    mock.patch.object(H.secrets, "token_hex", return_value="abcd"),
                    mock.patch.object(H.cmux, "submit_text", return_value={},
                                      side_effect=dispatch_error if late else None) as send,
                    mock.patch.object(H.cmux, "wait_for_ack", side_effect=check_wait),
                ):
                    H.cmd_handshake(r3.args(root))
                send.assert_called_once()
                final = json.loads((root / "handshake-receipt.json").read_text())
                self.assertEqual(final["status"], "PASS")
                self.assertEqual(final["ack_line"], expected)

    def test_panel_wait_does_not_pass_or_hide_terminal_failure(self):
        acked = {"executor": "surface:2", "executor_ack": True,
                 "status": "PASS", "lifecycle": "ACKED"}
        for lifecycle, status in (("PENDING", "HELLO_SENT"), ("REJECTED", "FAIL"),
                                  ("EXPIRED", "FAIL"), ("UNKNOWN", "UNKNOWN")):
            with self.subTest(lifecycle=lifecycle), tempfile.TemporaryDirectory() as tmp:
                pending = {"executor": "surface:3", "executor_ack": False,
                           "status": status, "lifecycle": lifecycle,
                           "terminal_error_at": None if lifecycle == "PENDING" else "end",
                           "error": "specific failure"}
                H._write_handshake_receipt(Path(tmp), [acked, pending])
                receipt = json.loads((Path(tmp) / "handshake-receipt.json").read_text())
                self.assertFalse(receipt["executor_ack"])
                self.assertEqual(receipt["unproven_executors"], ["surface:3"])
                self.assertEqual(receipt["lifecycle"], lifecycle)
                if lifecycle == "PENDING":
                    self.assertEqual(receipt["status"], "AWAITING_EXECUTOR_ACK")
                    self.assertNotIn("error", receipt)
                else:
                    self.assertEqual(receipt["status"], "FAIL")
                    self.assertIn("specific failure", receipt["error"])
                    self.assertEqual(receipt["terminal_error_at"], "end")


class ProbeTokenTests(unittest.TestCase):
    def test_real_generator_stays_short_and_distinct_from_long_task_ids(self):
        tokens = [H._bridge_test_token("long-task-" * 100, i) for i in (1, 2)]
        self.assertNotEqual(*tokens)
        for i, token in enumerate(tokens, 1):
            self.assertRegex(token, rf"^B{i}_[0-9a-f]{{8}}$")
            self.assertEqual(len(token), 11)
        with mock.patch.object(H.secrets, "token_hex", side_effect=["aaaaaaaa", "bbbbbbbb"]):
            self.assertNotEqual(H._bridge_test_token("same", 1), H._bridge_test_token("same", 1))
