"""Public message dispatch: crash/retry, stale identity, and zero-input recovery."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture


class MessageJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        p = patch.object(Path, 'home', return_value=self.home)
        p.start(); self.addCleanup(p.stop)
        self.identity = dict(workspace_uuid='workspace', caller_surface_uuid='caller',
                             target_surface_uuid='target', target_pane_uuid='pane')
        self.native = LegacyTerminalFixture.attach(self, home=self.home, identity=self.identity)
        self.home = self.native.home
        self.pin = self.native.pin
        p = patch.object(b.time, 'sleep')
        p.start(); self.addCleanup(p.stop)
        self.text = 'STATUS: message-nonce-20261005'
        self.marker = 'message-nonce-20261005'
        self.idle = '› \nGPT-6 high'
        self.done = '› ' + self.text + '\n• Read evidence\n' + self.idle

    def call(self, **kwargs):
        return b.submit_text('peer', self.text, marker=self.marker, **kwargs)

    def test_success_blocks_duplicate_and_changed_payload(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                self.idle, self.native.draft(self.text), self.done)), \
                patch.object(b, 'send_text') as paste, \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(self.text)) as key:
            result = self.call()
            self.assertTrue(result['confirmed'])
            with self.assertRaisesRegex(b.TaskPackContractError, 'RECEIPT_EXISTS'):
                self.call()
            self.text += ' modified'
            with self.assertRaisesRegex(b.TaskPackContractError, 'BINDING_CHANGED'):
                self.call()
            self.assertEqual(paste.call_count, 1)
            self.assertEqual(key.call_count, 1)

    def test_lost_paste_ack_never_repeats_then_reconciles(self):
        with patch.object(b, 'read_screen', return_value=self.idle), \
                patch.object(b, 'send_text', side_effect=OSError('lost ACK')) as paste, \
                patch.object(b, 'send_key') as key:
            with self.assertRaises(OSError):
                self.call()
            with self.assertRaisesRegex(b.TaskPackContractError, 'ATTEMPT_EXISTS'):
                self.call()
            paste.assert_called_once(); key.assert_not_called()
        self.native.append_user(self.text)
        with patch.object(b, 'read_screen', return_value=self.done), \
                patch.object(b, 'send_text') as paste, patch.object(b, 'send_key') as key:
            result = self.call(reconcile_only=True)
            self.assertTrue(result['reconciled_read_only'])
            self.assertEqual(json.loads(Path(result['observation']).read_text())['input_operations'], 0)
            paste.assert_not_called(); key.assert_not_called()

    def test_occupied_or_stale_marker_never_mutates(self):
        for screen in ('› continue\nGPT-6 high', self.done):
            with self.subTest(screen=screen), patch.object(b, 'read_screen', return_value=screen), \
                    patch.object(b, 'send_text') as paste, patch.object(b, 'send_key') as key:
                with self.assertRaises((b.DispatchUnconfirmed, b.TaskPackContractError)):
                    self.call()
                paste.assert_not_called(); key.assert_not_called()

    def test_identity_changes_before_paste(self):
        def move_after_observation(*args, **kwargs):
            self.identity['target_pane_uuid'] = 'moved'
            return self.idle
        with patch.object(b, 'read_screen', side_effect=move_after_observation), \
                patch.object(b, 'send_text') as paste, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.TaskPackContractError, 'IDENTITY_CHANGED'):
                self.call()
            paste.assert_not_called(); key.assert_not_called()

    def test_marker_and_force_are_required_to_be_safe(self):
        with patch.object(b, 'send_text') as paste, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.TaskPackContractError, 'MARKER_REQUIRED'):
                b.submit_text('peer', 'STATUS: no marker')
            with self.assertRaisesRegex(b.TaskPackContractError, 'PRESERVE_COMPOSE'):
                self.call(force_compose=True)
            with self.assertRaisesRegex(b.TaskPackContractError, 'NO_SUBMITTED_MESSAGE'):
                self.call(reconcile_only=True)
            paste.assert_not_called(); key.assert_not_called()

    def test_posthook_revalidates_message_and_rejects_modified_evidence(self):
        from cmux_submit_confirmation_guard import _attempt_evidence
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                self.idle, self.native.draft(self.text), self.done)), \
                patch.object(b, 'send_text'), \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(self.text)):
            result = self.call()
        call = dict(kind='text', surface='peer', text=self.text, marker=self.marker)
        with patch.object(b, 'send_text') as paste, patch.object(b, 'send_key') as key:
            proof = _attempt_evidence(call, 'peer', b)
            self.assertEqual(proof['source'], 'revalidated_message_dispatch_v1')
            path = Path(result['attempt'])
            attempt = json.loads(path.read_text())
            attempt['events'][0]['screen_sha256'] = 'changed'
            path.write_text(json.dumps(attempt))
            self.assertIsNone(_attempt_evidence(call, 'peer', b))
            paste.assert_not_called(); key.assert_not_called()


    def _reference(self):
        import cmux_prompt_reference as reference
        full = "STATUS: " + self.marker + "\n" + "原文保留空格  \t\r\n" * 100
        planned = reference.plan(full, self.marker)
        self.body_pin = reference.persist(planned["reference"], full)
        self.text = planned["text"]
        self.body_reference = planned["reference"]
        self.body = Path(self.body_reference["path"])
        self.done = self.idle

    def test_new_oversized_raw_message_refuses_before_paste(self):
        self.text += "\n" + "中" * 1000
        with self.assertRaisesRegex(b.TaskPackContractError, "LONG_MESSAGE_REFERENCE_REQUIRED"):
            self.call()
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()

    def test_new_raw_terminal_newline_refuses_before_any_input(self):
        original = self.text
        for suffix in ("\n", "\r\n", "\r"):
            with self.subTest(suffix=repr(suffix)):
                self.text = original + suffix
                with self.assertRaisesRegex(b.TaskPackContractError, "TRAILING_NEWLINE_REFERENCE_REQUIRED"):
                    self.call()
                self.native.send.assert_not_called()
                self.native.key.assert_not_called()

    def test_legacy_terminal_newline_attempt_reconciles_exactly_without_input(self):
        import cmux_prompt_reference as reference
        self.text += "\r\n"
        with patch.object(reference, "require_inline"), \
                patch.object(b, "read_screen", return_value=self.idle), \
                patch.object(b, "send_text", side_effect=OSError("legacy lost paste ACK")):
            with self.assertRaises(OSError):
                self.call()
        attempts = list((self.home / ".local/state/multi-agent-collaboration/message-dispatch-v1")
                        .glob("*/attempt-*.json"))
        self.assertEqual(len(attempts), 1)
        original_attempt = attempts[0].read_bytes()
        self.native.append_user(self.text)
        self.native.send.reset_mock()
        self.native.key.reset_mock()
        result = self.call(reconcile_only=True)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["reconciled_read_only"])
        self.assertEqual(Path(result["attempt"]), attempts[0])
        self.assertEqual(attempts[0].read_bytes(), original_attempt)
        saved = json.loads(original_attempt)
        self.assertEqual(saved["binding"]["payload_sha256"], reference.digest(self.text))
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()

    def test_reference_body_change_after_paste_authorizes_no_key(self):
        self._reference()
        def mutate(*args, **kwargs):
            self.body.chmod(0o600)
        with patch.object(b, "read_screen", side_effect=self.native.ready_screens(
                self.idle, self.native.draft(self.text), self.done)), \
                patch.object(b, "send_text", side_effect=mutate) as paste, \
                patch.object(b, "send_key") as key:
            with self.assertRaisesRegex(b.TaskPackContractError, "MESSAGE_BODY_CHANGED"):
                self.call()
            paste.assert_called_once()
            key.assert_not_called()

    def test_reference_native_reconciliation_is_zero_input_and_notice_only(self):
        self._reference()
        with patch.object(b, "read_screen", return_value=self.idle), \
                patch.object(b, "send_text", side_effect=OSError("lost paste ACK")):
            with self.assertRaises(OSError):
                self.call()
        self.native.append_user(self.text)
        self.native.send.reset_mock()
        self.native.key.reset_mock()
        result = self.call(reconcile_only=True)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["reconciled_read_only"])
        self.assertEqual(result["confirmation_scope"], "reference_notice")
        self.assertIs(result["body_read_confirmed"], False)
        from cmux_message_journal import verified_receipt
        proof = verified_receipt(b, "peer", self.text, self.marker)
        self.assertEqual(proof["body_pin"], self.body_pin)
        self.assertIs(proof["body_read_confirmed"], False)
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()

    def test_changed_reference_cannot_reconcile_or_borrow_saved_receipt(self):
        self._reference()
        with patch.object(b, "read_screen", return_value=self.idle), \
                patch.object(b, "send_text", side_effect=OSError("lost paste ACK")):
            with self.assertRaises(OSError):
                self.call()
        self.native.append_user(self.text)
        replacement = self.body.with_name("same-byte-replacement.txt")
        replacement.write_bytes(self.body.read_bytes())
        replacement.chmod(0o400)
        replacement.replace(self.body)
        self.native.send.reset_mock()
        self.native.key.reset_mock()
        with self.assertRaisesRegex(b.TaskPackContractError, "MESSAGE_BODY_CHANGED"):
            self.call(reconcile_only=True)
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()


    def test_task_target_lock_prevents_message_input(self):
        import fcntl
        from cmux_message_journal import digest
        root = self.home / '.local/state/multi-agent-collaboration/task-dispatch-v1'
        root.mkdir(parents=True)
        with (root / ('target-' + digest('target') + '.lock')).open('a+b') as lock, \
                patch.object(b, 'send_text') as paste, patch.object(b, 'send_key') as key:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(b.TaskPackContractError, 'MESSAGE_IN_PROGRESS'):
                self.call()
            paste.assert_not_called(); key.assert_not_called()


if __name__ == '__main__':
    unittest.main()
