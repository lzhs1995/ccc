"""Private short-check recovery through the daemon, SQLite and real socket guard."""
import copy
import json
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from tests import test_input_not_sent as ordinary


class PrivateInputNotSentTests(unittest.TestCase):
    def setUp(self):
        self.h = ordinary.InputNotSentTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.f = self.h.f
        # Retain only the original authorized short check. The ordinary fixture
        # appends an operator task, which must never inherit check semantics.
        records = self.f.path.read_bytes().splitlines(keepends=True)
        original = []
        for record in records:
            event = json.loads(record)
            if event.get('payload', {}).get('turn_id') == 'operator':
                break
            original.append(record)
        self.f.path.write_bytes(b''.join(original))
        self.proof = self.f.daemon.private_checks.select(
            self.f.target, lambda: self.f.daemon.codex_queue_recovery.current_turn(self.f.target))
        self.assertIsNotNone(self.proof)

    def exercise_recovery(self, *, restart=False):
        attempt = self.h.initial_refusal()
        self.assertTrue(self.f.runtime.codex_private_check)
        self.assertEqual(self.f.runtime.codex_input_phase, 'input_not_sent')
        if restart:
            self.h.restart_records()
        self.h.deny = False
        self.h.send()
        self.assertEqual(self.f.runtime.delivery_status, 'accepted')
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)
        self.assertEqual(self.h.episode()['count'], 1)
        self.assertEqual(self.h.pending, [('terminal.paste', 'sending', 'paste_submit_pending')])
        self.h.send()
        self.assertEqual(len(self.h.pending), 1)

    def test_private_guard_refusal_recovers_same_attempt(self):
        self.exercise_recovery()

    def test_private_guard_refusal_recovers_same_attempt_after_restart(self):
        self.exercise_recovery(restart=True)

    def test_first_pending_rollback_recovers_private_attempt_after_restart(self):
        self.h.assert_pending_write_recovers('rollback')

    def test_committed_pending_lost_result_recovers_private_attempt_after_restart(self):
        self.h.assert_pending_write_recovers('lost_result')

    def test_retry_pending_rollback_keeps_original_private_and_provider_attempt(self):
        self.h.test_proven_attempt_keeps_provider_reservation_after_pending_rollback()

    def test_delivery_proof_failure_cannot_recover_private_intent(self):
        self.h.test_pending_recovery_proof_failure_retains_unknown_input_barrier()

    def test_crash_after_delivery_pending_cannot_recreate_private_retry(self):
        self.h.test_crash_after_first_pending_cannot_recreate_live_recovery_token()

    def test_lost_ack_after_private_retry_never_replays(self):
        self.h.test_lost_ack_on_second_attempt_never_replays()

    def test_partial_ack_after_private_retry_never_replays(self):
        self.h.test_partial_submit_on_second_attempt_never_replays()

    def test_split_private_text_then_enter_refusal_never_replays(self):
        self.h.test_text_zero_write_can_retry_but_enter_refusal_cannot()

    def test_private_retry_preserves_pause_and_draft(self):
        self.h.test_pause_and_draft_still_block_recovery()

    def test_private_retry_rejects_changed_process_and_provider(self):
        self.h.test_changed_process_identity_cannot_borrow_unwritten_proof()

    def test_private_retry_rejects_active_or_unknown_native_turn(self):
        self.h.test_reconnect_and_new_user_input_do_not_reuse_proof()

    def assert_no_retry_after_restart(self, attempt):
        self.h.deny = False
        self.h.send()
        self.assertFalse(self.h.pending)
        self.h.restart_records()
        self.h.send()
        self.assertFalse(self.h.pending)
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)

    def test_crash_between_private_intent_and_delivery_pending_blocks_retry(self):
        attempt = self.h.initial_refusal()
        prior = copy.deepcopy(self.h.restored().codex_input_not_sent)
        reserve = self.f.daemon.private_checks.reserve

        def crash_after_private_write(*args, **kwargs):
            reserve(*args, **kwargs)
            raise SystemExit('crash after private intent commit')

        self.h.deny = False
        with patch.object(self.f.daemon.private_checks, 'reserve', side_effect=crash_after_private_write):
            with self.assertRaisesRegex(SystemExit, 'after private intent'):
                self.h.send()
        # Delivery still has the *old* zero-input proof, but the private ledger
        # has already revoked it. Restart must require both records, not either.
        self.assertEqual(self.h.restored().codex_input_not_sent, prior)
        self.assertEqual(self.f.daemon.private_checks._ledger(self.f.target)[attempt]['phase'], 'intent')
        self.assert_no_retry_after_restart(attempt)

    def test_private_finish_failure_keeps_committed_delivery_proof_unusable(self):
        with patch.object(self.f.daemon.private_checks, 'finish', side_effect=OSError('private fsync failed')):
            with self.assertRaisesRegex(OSError, 'private fsync'):
                self.h.send()
        attempt = self.f.runtime.send_attempt_id
        self.assertTrue(self.h.restored().codex_input_not_sent)
        self.assertEqual(self.f.daemon.private_checks._ledger(self.f.target)[attempt]['phase'], 'intent')
        self.assert_no_retry_after_restart(attempt)

    def test_changed_private_receipt_cannot_borrow_delivery_zero_input_proof(self):
        attempt = self.h.initial_refusal()
        checks = self.f.daemon.private_checks
        original = checks._ledger(self.f.target)
        damages = {
            'missing': lambda row: row.clear(),
            'other_attempt': lambda row: row.update({'other': row.pop(attempt)}),
            'duplicate_turn': lambda row: row.update({'other': copy.deepcopy(row[attempt])}),
            'io_started': lambda row: row[attempt].update(io_started_at=1),
            'unknown': lambda row: row[attempt].update(phase='unknown'),
            'changed_source': lambda row: row[attempt]['origin'].update(launch_id='different'),
            'changed_turn': lambda row: row[attempt].update(failed_turn_id='different'),
            'changed_message': lambda row: row[attempt].update(message='Unrelated task'),
        }
        self.h.deny = False
        for name, damage in damages.items():
            with self.subTest(name=name):
                ledger = copy.deepcopy(original)
                damage(ledger)
                core.atomic_write_json(checks._ledger_path(self.f.target), ledger)
                self.h.send()
                self.assertFalse(self.h.pending)
                self.assertEqual(self.f.runtime.send_attempt_id, attempt)
        core.atomic_write_json(checks._ledger_path(self.f.target), original)
        self.h.send()
        self.assertEqual(len(self.h.pending), 1)

    def test_missing_source_after_zero_input_never_falls_back_to_ordinary_prompt(self):
        attempt = self.h.initial_refusal()
        path = self.f.daemon.private_checks._ledger_path(self.f.target).parent / 'origin.json'
        path.unlink()
        self.assert_no_retry_after_restart(attempt)

    def test_source_changed_after_pending_write_withholds_input(self):
        attempt = self.h.initial_refusal()
        persist = self.f.daemon._save_delivery
        changes = []

        def remove_origin_after_commit(*args, **kwargs):
            result = persist(*args, **kwargs)
            if self.f.runtime.delivery_status == 'sending' and not changes:
                self.f.daemon.private_checks._ledger_path(self.f.target).with_name('origin.json').unlink()
                changes.append(True)
            return result

        self.h.deny = False
        with patch.object(self.f.daemon, '_save_delivery', side_effect=remove_origin_after_commit):
            self.h.send()
        self.assertTrue(changes)
        self.assert_no_retry_after_restart(attempt)


if __name__ == '__main__':
    unittest.main()
