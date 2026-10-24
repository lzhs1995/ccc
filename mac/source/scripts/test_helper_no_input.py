"""Original helper NO_INPUT settlement with real journals and native receipts.

Only external process, socket and terminal observations are fixtures. The helper
request, sender, journal verifier, locks and receipt validation run unchanged.
"""
import copy
import fcntl
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_agent_adapter as helper
import cmux_bridge as bridge
import cmux_message_journal as journal
from test_codex_native_p2_regressions import (
    OfflineCase, WORKSPACE, CALLER, TARGET, CONNECT_ERROR, read_json,
)


class HelperNoInputTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.adapter = helper.Adapter(self.fixture.home)
        self.fixture.stack.enter_context(patch.object(bridge, 'list_surfaces', return_value=[
            dict(surface_id=TARGET, workspace_id=WORKSPACE,
                 surface_type='terminal', ref='surface:3')]))
        self.channel = self.adapter.channel(self.fixture.identity)
        self.pending = self.channel / 'pending.json'
        self.message = 'STATUS: original helper request, preserve its exact bytes'

    def send(self, request_id='original', message=None):
        return self.adapter.deliver(TARGET, 'send', message or self.message, request_id)

    def first_no_input(self, *, old_pending=False, message=None):
        self.fixture.rpc.side_effect = TimeoutError(CONNECT_ERROR)
        if old_pending:
            # Simulate an older adapter leaving its pointer, not its journal.
            with patch.object(helper, 'verified_no_input', return_value=None):
                with self.assertRaisesRegex(TimeoutError, CONNECT_ERROR):
                    self.send(message=message)
        else:
            with self.assertRaisesRegex(helper.Unconfirmed, 'HELPER_NO_INPUT_SETTLED') as caught:
                self.send(message=message)
            self.assertEqual(caught.exception.details['input_operations'], 0)
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        intents = list((self.channel / 'intents').glob('*.json'))
        self.assertEqual(len(intents), 1)
        self.intent = intents[0]
        self.request = read_json(self.intent)
        key = journal.digest(json.dumps([CALLER, self.request['marker']], sort_keys=True))
        self.journal = self.state_root / 'message-dispatch-v1' / key
        self.attempt = self.journal / 'attempt-0001.json'
        record = read_json(self.attempt)
        self.assertEqual(record['phase'], 'NO_INPUT')
        self.assertEqual(record['events'], [])
        self.assertNotIn('result', record)
        self.assertEqual(self.rpc.mutations, [])
        self.original = {p: p.read_bytes() for p in (self.intent, self.attempt)}
        self.assertEqual(self.pending.exists(), old_pending)

    def reconcile(self):
        return self.adapter.deliver(TARGET, 'reconcile', reconcile=True, intent=str(self.intent))

    def proof(self, **kwargs):
        return journal.verified_no_input(bridge, TARGET, self.request['payload'],
                                         self.request['marker'], **kwargs)

    def assert_original_unchanged(self):
        for path, raw in self.original.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(list(self.journal.glob('attempt-*.json')), [self.attempt])

    def test_original_no_input_settles_without_claiming_delivery(self):
        self.first_no_input()
        settlement = read_json(self.channel / 'settlements' / (self.request['marker'] + '.json'))
        self.assertIs(settlement['confirmed'], False)
        self.assertEqual(settlement['input_operations'], 0)
        self.assertEqual(settlement['request'], str(self.intent))
        self.assertEqual(settlement['no_input_validation'], self.proof())
        self.assertFalse((self.journal / 'receipt.json').exists())
        self.assert_original_unchanged()

    def test_original_repeat_and_reconcile_never_resend(self):
        self.first_no_input()
        self.fixture.rpc.reset_mock()
        for call in (self.send, self.reconcile, self.send):
            with self.assertRaisesRegex(helper.Unconfirmed, 'HELPER_NO_INPUT_SETTLED'):
                call()
            self.assertFalse(self.pending.exists())
            self.assert_original_unchanged()
        self.fixture.rpc.assert_not_called()
        self.assertEqual(self.rpc.mutations, [])

    def test_separate_request_can_send_and_receive_after_settlement(self):
        self.first_no_input(old_pending=True)
        with self.assertRaisesRegex(helper.Unconfirmed, 'HELPER_NO_INPUT_SETTLED'):
            self.reconcile()
        result = self.send(request_id='separately-authorized-new-request')
        self.assertTrue(result['confirmed'])
        self.assertNotEqual(result['marker'], self.request['marker'])
        new = read_json(Path(result['request']))
        self.assertEqual(self.rpc.mutations, [('turn/steer', dict(
            threadId=self.fixture.session_id, expectedTurnId=self.rpc.turn,
            input=[dict(type='text', text=new['payload'])]))])
        self.assertEqual(result['receipt_validation']['source'], 'revalidated_message_dispatch_v1')
        self.assertFalse(self.pending.exists())
        self.assert_original_unchanged()

    def test_other_caller_cannot_settle_or_take_over_pending(self):
        self.first_no_input(old_pending=True)
        pending = self.pending.read_bytes()
        self.fixture.identity['caller_surface_uuid'] = '00000000-0000-4000-8000-000000000009'
        with self.assertRaisesRegex(helper.Unconfirmed, 'HELPER_PENDING_OTHER_CALLER'):
            self.send(request_id='foreign-caller')
        self.assertEqual(self.pending.read_bytes(), pending)
        self.assertEqual(self.rpc.mutations, [])
        self.assert_original_unchanged()

    def test_commit_holds_original_journal_lock(self):
        self.first_no_input(old_pending=True)
        original_write = helper.write_json
        checked = []

        def write(path, value):
            if path.parent.name == 'settlements':
                fd = os.open(self.journal / 'delivery.lock', os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    checked.append(path)
                finally:
                    os.close(fd)
            return original_write(path, value)

        with patch.object(helper, 'write_json', side_effect=write):
            with self.assertRaisesRegex(helper.Unconfirmed, 'HELPER_NO_INPUT_SETTLED'):
                self.reconcile()
        self.assertEqual(len(checked), 1)
        self.assertFalse(self.pending.exists())
        self.assert_original_unchanged()

    def test_failed_commit_keeps_pending_and_propagates_without_input(self):
        self.first_no_input(old_pending=True)
        pending = self.pending.read_bytes()
        with patch.object(helper, 'write_json', side_effect=OSError('offline commit failed')):
            with self.assertRaisesRegex(OSError, 'offline commit failed'):
                self.reconcile()
        self.assertEqual(self.pending.read_bytes(), pending)
        self.assertEqual(self.rpc.mutations, [])
        self.assert_original_unchanged()

    def test_no_proof_from_missing_journal_or_lock(self):
        self.first_no_input(old_pending=True)
        self.attempt.unlink()  # Owned temporary test evidence only.
        self.assertIsNone(self.proof())
        self.attempt.write_bytes(self.original[self.attempt])
        (self.journal / 'delivery.lock').unlink()
        self.assertIsNone(self.proof())
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.rpc.mutations, [])

    def test_receipt_or_legacy_record_prevents_settlement(self):
        self.first_no_input(old_pending=True)
        legacy = self.state_root / 'deliveries-v1' / (self.journal.name + '.json')
        for path in (self.journal / 'receipt.json', legacy):
            with self.subTest(path=path.name):
                path.write_text('{}')
                self.assertIsNone(self.proof())
                self.assertTrue(self.pending.exists())
                path.unlink()
        self.assert_original_unchanged()

    def test_result_field_even_empty_or_false_is_not_no_input_proof(self):
        self.first_no_input(old_pending=True)
        for result in ({}, False, None, [], '', 0, {'confirmed': False}):
            with self.subTest(result=result):
                value = json.loads(self.original[self.attempt])
                value['result'] = result
                self.attempt.write_text(json.dumps(value))
                self.assertIsNone(self.proof())
                self.assertTrue(self.pending.exists())
        self.assertEqual(self.rpc.mutations, [])

    def test_event_phase_or_binding_change_never_clears_pointer(self):
        self.first_no_input(old_pending=True)
        cases = [dict(events=[dict(phase='NATIVE_INPUT_INTENT')]),
                 dict(phase='PREPARED'), dict(phase='NATIVE_INPUT_INTENT'),
                 dict(binding={}), dict(events=None)]
        pending = self.pending.read_bytes()
        for changes in cases:
            with self.subTest(changes=changes):
                value = json.loads(self.original[self.attempt])
                value.update(changes)
                self.attempt.write_text(json.dumps(value))
                self.assertIsNone(self.proof())
                with self.assertRaises(Exception):
                    self.reconcile()
                self.assertEqual(self.pending.read_bytes(), pending)
                self.assertEqual(len(list(self.journal.glob('attempt-*.json'))), 1)
        self.assertEqual(self.rpc.mutations, [])

    def test_changed_body_preserves_pointer_and_attempt(self):
        self.first_no_input(old_pending=True, message='STATUS: ' + '完整正文 ' * 200)
        body = Path(self.request['body_reference']['path'])
        body.chmod(0o600)  # This fixture owns the otherwise immutable body.
        body.write_text('different body')
        self.assertIsNone(self.proof())
        with self.assertRaises(Exception):
            self.reconcile()
        self.assertTrue(self.pending.exists())
        self.assert_original_unchanged()
        self.assertEqual(self.rpc.mutations, [])

    def test_complete_attempt_set_required_and_all_must_be_no_input(self):
        self.first_no_input(old_pending=True)
        second = self.journal / 'attempt-0002.json'
        second.write_bytes(self.original[self.attempt])
        self.assertEqual(len(self.proof()['attempts']), 2)
        third = self.journal / 'attempt-0003.json'
        third.write_bytes(self.original[self.attempt])
        self.assertIsNone(self.proof())
        second.unlink()
        self.assertIsNone(self.proof())  # A missing attempt cannot be skipped.
        third.unlink()
        second.write_text(json.dumps(dict(read_json(self.attempt), events=[dict(phase='PASTE_INTENT')])))
        self.assertIsNone(self.proof())
        self.assertTrue(self.pending.exists())

    def test_live_identity_change_during_verification_prevents_commit(self):
        self.first_no_input(old_pending=True)
        original = copy.deepcopy(self.fixture.identity)
        changed = dict(original, target_pane_uuid='00000000-0000-4000-8000-000000000008')
        with patch.object(bridge, 'pin_workspace', side_effect=[original, changed]):
            committed = []
            self.assertIsNone(self.proof(commit=committed.append))
        self.assertEqual(committed, [])
        self.assertTrue(self.pending.exists())
        self.assert_original_unchanged()


if __name__ == '__main__':
    unittest.main()
