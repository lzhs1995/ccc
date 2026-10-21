"""Contention before any input must be recoverable without replaying uncertainty."""
import argparse
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import availability_contract as availability
import cmux_bridge as b
import cmux_callback_journal as journal_io
import cmux_evidence_io as evidence
import cmux_native_delivery as native
import cmux_task_journal as journal
import resume_task_no_input as recovery
import test_task_dispatch_journal as fixture
from native_test_support import ScreenSequence
from test_task_dispatch_journal import IDLE, TEXT


class ZeroInputResumeTests(unittest.TestCase):
    setUp = fixture.TaskDispatchJournalTests.setUp

    def original(self):
        self.pack['completion_nonce'] = 'preserve-original-nonce'
        self.path.write_text(json.dumps(self.pack))
        prompt = self.home / 'prompt.txt'
        prompt.write_text(TEXT)
        occupied = self.native.draft('owned by a separate caller', 'claude')
        with patch.object(b, 'read_screen', return_value=occupied), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            for _ in range(2):
                with self.assertRaisesRegex(b.DispatchUnconfirmed, 'COMPOSE_OCCUPIED'):
                    journal.deliver(b, 'peer', TEXT, self.path)
            send.assert_not_called(); key.assert_not_called()
        self.directory = next(self.home.rglob('attempt-0001.json')).parent
        self.records = [(p, p.read_bytes()) for p in evidence.attempt_paths(self.directory)]
        self.args = argparse.Namespace(controller='/original/immutable/controller', surface='peer',
            task_pack=str(self.path), text_file=str(prompt), marker=None, apply=False,
            history_sha256=recovery.history_pin(self.records)['history_sha256'])

    def run_recovery(self, **values):
        for key, value in values.items():
            setattr(self.args, key, value)
        return recovery.resume(self.args, (b, native, evidence, journal_io, availability))

    def assert_preserved(self):
        for path, raw in self.records:
            self.assertEqual(path.read_bytes(), raw)

    def repin(self):
        rows = [(p, p.read_bytes()) for p in evidence.attempt_paths(self.directory)]
        self.args.history_sha256 = recovery.history_pin(rows)['history_sha256']

    def test_plan_after_two_zero_input_attempts_is_read_only(self):
        self.original()
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            result = self.run_recovery()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT')
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['plan']['completion_nonce'], self.pack['completion_nonce'])
        self.assertEqual(len(evidence.attempt_paths(self.directory)), 2)
        self.assert_preserved(); send.assert_not_called(); key.assert_not_called()

    def test_single_third_attempt_gets_native_receipt_and_preserves_every_old_byte(self):
        self.original()
        pack_raw = self.path.read_bytes()
        draft = self.native.draft(TEXT, 'claude')
        screens = ScreenSequence([IDLE, IDLE, draft, draft, '⏺ done\n' + IDLE])
        with patch.object(b, 'read_screen', side_effect=screens), \
                patch.object(b, 'send_text') as send, \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(TEXT)) as key:
            result = self.run_recovery(apply=True)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['original_attempts_preserved'])
        self.assertEqual(self.path.read_bytes(), pack_raw)
        self.assertEqual(Path(result['attempt']).resolve(),
                         (self.directory / 'attempt-0003.json').resolve())
        self.assert_preserved(); send.assert_called_once(); key.assert_called_once()
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(ValueError):
                self.run_recovery(apply=True)
            send.assert_not_called(); key.assert_not_called()

    def test_first_paste_crash_is_unknown_and_cannot_resume_again(self):
        self.original()
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text', side_effect=RuntimeError('outcome unknown')) as send, \
                patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                self.run_recovery(apply=True)
            send.assert_called_once(); key.assert_not_called()
        third = json.loads((self.directory / 'attempt-0003.json').read_text())
        self.assertEqual(third['phase'], 'PASTE_INTENT')
        self.assertFalse((self.directory / 'receipt.json').exists())
        self.repin()
        with patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'INPUT_OR_UNKNOWN'):
                self.run_recovery(apply=True)
            send.assert_not_called()
        self.assert_preserved()

    def test_checks_all_old_attempts_not_only_the_last(self):
        self.original()
        path, raw = self.records[0]
        cases = [('PASTE_INTENT', []), ('UNKNOWN', []), ('NATIVE_QUEUED', []),
                 ('CONFIRMED', []), ('PREPARED', []), ('NO_INPUT', [{'phase': 'PASTE_INTENT'}])]
        for phase, events in cases:
            with self.subTest(phase=phase, events=events):
                row = dict(json.loads(raw), phase=phase, events=events)
                path.write_text(json.dumps(row)); self.repin()
                with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                    with self.assertRaisesRegex(ValueError, 'INPUT_OR_UNKNOWN'):
                        self.run_recovery(apply=True)
                    send.assert_not_called(); key.assert_not_called()

    def test_apply_requires_reviewed_history(self):
        self.original()
        for value in (None, '0' * 64):
            with self.subTest(value=value), patch.object(b, 'send_text') as send:
                with self.assertRaisesRegex(ValueError, 'REVIEWED_HISTORY'):
                    self.run_recovery(apply=True, history_sha256=value)
                send.assert_not_called()

    def test_occupied_composer_after_plan_never_cleared_and_no_new_slot(self):
        self.original()
        with patch.object(b, 'read_screen', return_value=IDLE):
            self.run_recovery()
        occupied = self.native.draft('user draft', 'claude')
        with patch.object(b, 'read_screen', return_value=occupied), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(ValueError, 'RECEIVER_NOT_READY'):
                self.run_recovery(apply=True)
            send.assert_not_called(); key.assert_not_called()
        self.assertEqual(len(evidence.attempt_paths(self.directory)), 2)
        self.assert_preserved()

    def test_new_contention_is_still_zero_input_but_requires_new_review(self):
        self.original()
        occupied = self.native.draft('another writer arrived', 'claude')
        with patch.object(b, 'read_screen', side_effect=[IDLE, occupied]), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.DispatchUnconfirmed, 'COMPOSE_OCCUPIED'):
                self.run_recovery(apply=True)
            send.assert_not_called(); key.assert_not_called()
        third = json.loads((self.directory / 'attempt-0003.json').read_text())
        self.assertEqual((third['phase'], third['events']), ('NO_INPUT', []))
        with patch.object(b, 'read_screen', return_value=IDLE):
            with self.assertRaisesRegex(ValueError, 'REVIEWED_HISTORY_CHANGED'):
                self.run_recovery(apply=True)
            self.repin()
            self.assertEqual(self.run_recovery(apply=False)['status'], 'READY_ZERO_INPUT')
        self.assert_preserved()

    def test_edited_pack_or_text_cannot_create_new_slot(self):
        self.original()
        for path in (self.path, self.home / 'prompt.txt'):
            raw = path.read_bytes(); path.write_bytes(raw + b' ')
            with patch.object(b, 'send_text') as send:
                with self.assertRaisesRegex(ValueError, 'BINDING_CHANGED'):
                    self.run_recovery(apply=True)
                send.assert_not_called()
            path.write_bytes(raw)

    def test_changed_lock_refuses_even_with_all_zero_input(self):
        self.original()
        lock = self.directory / 'delivery.lock'
        lock.rename(self.directory / 'preserved-original-lock')
        lock.touch()
        with patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_LOCK_CHANGED'):
                self.run_recovery(apply=True)
            send.assert_not_called()

    def test_receipt_forbids_resend_even_if_old_phase_looks_zero_input(self):
        self.original()
        (self.directory / 'receipt.json').write_text('{}')
        with patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'RECEIPT_OR_LEGACY'):
                self.run_recovery(apply=True)
            send.assert_not_called()

    def test_different_live_runtime_cannot_be_rebound(self):
        self.original()
        with patch.object(native, 'require_bound', side_effect=RuntimeError('NATIVE_PROCESS_SESSION_CHANGED')), \
                patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(RuntimeError, 'PROCESS_SESSION_CHANGED'):
                self.run_recovery(apply=True)
            send.assert_not_called()

    def test_missing_history_slot_is_not_recreated(self):
        self.original()
        self.records[0][0].rename(self.directory / 'preserved-first.json')
        self.repin()
        with patch.object(b, 'send_text') as send:
            with self.assertRaisesRegex(ValueError, 'NOT_CONTIGUOUS'):
                self.run_recovery(apply=True)
            send.assert_not_called()

    def test_no_new_receipt_without_exact_native_evidence(self):
        self.original()
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'submit_text', return_value={'confirmed': True}), \
                patch.object(native, 'receipt_evidence', return_value=False):
            with self.assertRaisesRegex(b.DispatchUnconfirmed, 'NATIVE_PROOF_REQUIRED'):
                self.run_recovery(apply=True)
        self.assertFalse((self.directory / 'receipt.json').exists())
        # This unsupported failure is deliberately not eligible for another send.
        self.repin()
        with self.assertRaisesRegex(ValueError, 'INPUT_OR_UNKNOWN'):
            self.run_recovery(apply=True)


if __name__ == '__main__':
    unittest.main()
