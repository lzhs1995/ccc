"""Zero-input migration must preserve history, identity and the one retry budget."""
import argparse
import hashlib
import json
import unittest
from unittest.mock import patch

import cmux_bridge as b
import cmux_task_journal as j
import cmux_native_delivery as native
import cmux_evidence_io as evidence
import cmux_callback_journal as journal_io
import availability_contract as availability
import repair_task_notice as repair
import test_task_dispatch_journal as fixture
from test_task_dispatch_journal import IDLE, TEXT
from native_test_support import ScreenSequence


class TaskNoticeRepairTests(unittest.TestCase):
    setUp = fixture.TaskDispatchJournalTests.setUp

    def original(self):
        self.pack['completion_nonce'] = 'original-nonce-1234'
        self.path.write_text(json.dumps(self.pack))
        original_text = TEXT + '\n' + 'x' * 710
        prompt = self.home / 'original-prompt.txt'
        prompt.write_text(original_text)
        # Simulate the pre-fix controller: it reached native binding before the
        # transport rejected the wire. No paste observer was called.
        with patch('cmux_prompt_reference.require_inline'), patch.object(b, 'submit_text',
                side_effect=ValueError('LONG_MESSAGE_REFERENCE_REQUIRED: zero paste')):
            with self.assertRaises(ValueError):
                j.deliver(b, 'peer', original_text, self.path)
        original = next(self.home.rglob('attempt-0001.json')).resolve()
        self.raw = original.read_bytes()
        self.args = argparse.Namespace(controller='/original/complete/controller', surface='peer',
            task_pack=str(self.path), original_attempt=str(original),
            original_attempt_sha256=hashlib.sha256(self.raw).hexdigest(),
            original_text_file=str(prompt), apply=False)
        return original

    def run_repair(self, **kw):
        for key, value in kw.items():
            setattr(self.args, key, value)
        return repair.repair(self.args, (b, native, evidence, journal_io, availability))

    def test_new_invalid_wire_does_not_reserve_a_task_slot(self):
        for text in (TEXT + '\n', TEXT + 'x' * 710):
            with self.subTest(text=text[:40]), patch.object(b, 'pin_workspace') as pin:
                with self.assertRaisesRegex(ValueError, 'REFERENCE_REQUIRED'):
                    b.submit_task_pack('peer', text, str(self.path))
                pin.assert_not_called()
                self.assertEqual(list(self.home.rglob('attempt-*.json')), [])

    def test_readonly_plan_preserves_original_and_sends_nothing(self):
        original = self.original()
        with patch.object(b, 'task_pack_notice', return_value=TEXT), \
                patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            result = self.run_repair()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT')
        self.assertEqual(original.read_bytes(), self.raw)
        self.assertEqual(evidence.attempt_paths(original.parent), [original])
        send.assert_not_called(); key.assert_not_called()

    def test_one_actual_delivery_keeps_old_attempt_and_original_pack(self):
        original = self.original()
        pack_raw = self.path.read_bytes()
        draft = self.native.draft(TEXT, 'claude')
        screens = ScreenSequence([IDLE, IDLE, draft, draft, '⏺ done\n' + IDLE])
        with patch.object(b, 'task_pack_notice', return_value=TEXT), \
                patch.object(b, 'read_screen', side_effect=screens), \
                patch.object(b, 'send_text') as send, \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(TEXT)) as key:
            result = self.run_repair(apply=True)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['original_attempt_preserved'])
        self.assertEqual(original.read_bytes(), self.raw)
        self.assertEqual(self.path.read_bytes(), pack_raw)
        self.assertEqual(len(evidence.attempt_paths(original.parent)), 2)
        send.assert_called_once(); key.assert_called_once()
        with patch.object(b, 'task_pack_notice', return_value=TEXT), patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'RETRY_BUDGET_USED'):
                self.run_repair(apply=True)
            send.assert_not_called()

    def test_paste_crash_uses_budget_and_cannot_be_repaired_again(self):
        original = self.original()
        with patch.object(b, 'task_pack_notice', return_value=TEXT), \
                patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text', side_effect=RuntimeError('paste outcome unknown')), \
                patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(RuntimeError, 'paste outcome unknown'):
                self.run_repair(apply=True)
            key.assert_not_called()
        second = json.loads((original.parent / 'attempt-0002.json').read_text())
        self.assertEqual(second['phase'], 'PASTE_INTENT')
        self.assertFalse((original.parent / 'receipt.json').exists())
        self.assertEqual(original.read_bytes(), self.raw)
        with patch.object(b, 'task_pack_notice', return_value=TEXT), patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'RETRY_BUDGET_USED'):
                self.run_repair(apply=True)
            send.assert_not_called()

    def test_any_prior_event_or_uncertain_phase_refuses_repair(self):
        original = self.original()
        for phase, events in [('NO_INPUT', [{'phase': 'PASTE_INTENT'}]), ('PREPARED', []), ('PASTED', []), ('KEY_SENT', [])]:
            changed = dict(json.loads(self.raw), phase=phase, events=events)
            raw = json.dumps(changed).encode()
            original.write_bytes(raw)
            self.args.original_attempt_sha256 = hashlib.sha256(raw).hexdigest()
            with patch.object(b, 'task_pack_notice', return_value=TEXT), patch.object(b, 'send_text') as send:
                with self.assertRaisesRegex(ValueError, 'ZERO_EVENTS'):
                    self.run_repair(apply=True)
                send.assert_not_called()

    def test_changed_pack_or_text_cannot_migrate(self):
        self.original()
        self.path.write_text(self.path.read_text() + ' ')
        with patch.object(b, 'task_pack_notice', return_value=TEXT), patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINDING_CHANGED'):
                self.run_repair(apply=True)
            send.assert_not_called()

    def test_wrong_caller_cannot_use_another_supervisors_slot(self):
        self.original()
        with patch.object(b, 'task_pack_notice', return_value=TEXT), \
                patch.object(b, 'pin_workspace', return_value=dict(self.proof, caller_surface_uuid='OTHER')):
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_FIRST_ATTEMPT'):
                self.run_repair(apply=True)

    def test_existing_draft_is_never_cleared_or_overwritten(self):
        original = self.original()
        with patch.object(b, 'task_pack_notice', return_value=TEXT), \
                patch.object(b, 'read_screen', return_value=self.native.draft('user draft', 'claude')), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(ValueError, 'RECEIVER_NOT_READY'):
                self.run_repair(apply=True)
            send.assert_not_called(); key.assert_not_called()
        self.assertEqual(evidence.attempt_paths(original.parent), [original])


if __name__ == '__main__':
    unittest.main()
