"""The narrow one-shot repair must not replay or leak input to another session."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import ccc_access_service as service
import cmux_codex_watch as core
from tools import recover_access_setup as repair


class RepairBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.jid, self.wid = str(uuid.uuid4()), str(uuid.uuid4())
        self.directory = service.job_root(self.config, self.jid)
        self.directory.mkdir(parents=True)
        self.desc = {'port': 54321, 'policy': {'job_id': self.jid, 'workspace_id': self.wid},
                     'tokens': ['local-fixture-token'] * 50}
        self.slots = [{'index': i, 'surface_id': str(uuid.uuid4()), 'session_id': str(uuid.uuid4()),
                       'pid': 100 + i, 'process_start': 1, 'transcript': str(self.root / f'rollout-{i}.jsonl')}
                      for i in range(50)]
        self.inputs = [{'index': i, 'surface_id': s['surface_id'], 'session_id': s['session_id'],
                        'identity': {'pid': s['pid']}, 'transcript': s['transcript'],
                        'turn': {'turn_id': str(uuid.uuid4())}} for i, s in enumerate(self.slots)]
        self.plan = {'config_path': str(self.config), 'restore_jobs': [self.jid], 'inputs': self.inputs}
        self.plan_path = self.root / 'plan.json'
        self.job = {'id': self.jid, 'workspace_id': self.wid, 'slots': self.slots}
        core.atomic_write_json(self.plan_path, self.plan)
        core.atomic_write_json(self.directory / 'job.json', self.job)
        core.atomic_write_json(self.directory / 'access.json', self.desc)
        self.client = Mock()
        self.client.tree.return_value = {}
        self.receipt = repair.Receipt(self.root / 'receipt.json', self.plan_path)
        for name in ('authorized',):
            p = patch.object(repair, name)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (('owner_alive', True), ('status', {'first_complete': None})):
            p = patch.object(service, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_remote_409_or_another_job_url_cannot_authorize_recovery(self):
        slot = self.slots[0]
        message = (f'unexpected status 409 Conflict: {repair.FAILURE}, url: '
                   f'http://127.0.0.1:54321/{self.jid}/0/local-fixture-token/v1/responses')
        turn = {'kind': 'task_complete', 'turn_id': 'original', 'error': {'message': message}}
        self.assertTrue(repair.terminal_failure(turn, self.desc, slot))
        for changed in (message.replace('127.0.0.1', 'upstream.example'),
                        message.replace(self.jid, str(uuid.uuid4())),
                        message.replace('/0/', '/1/'), message + ' appended text'):
            turn['error']['message'] = changed
            self.assertFalse(repair.terminal_failure(turn, self.desc, slot))

    def test_missing_last_slot_prevents_all_fifty_inputs(self):
        def check(_client, _desc, slot, **kwargs):
            if slot['index'] == 49:
                raise ValueError('last slot moved')
        with patch.object(repair, 'inspect_native', side_effect=check):
            with self.assertRaisesRegex(ValueError, 'last slot moved'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.client.send_text.assert_not_called()
        self.client.send_key.assert_not_called()
        self.assertFalse(self.receipt.value['inputs'])

    def test_uncertain_paste_has_durable_intent_and_is_never_repeated(self):
        self.client.send_text.side_effect = OSError('fixture acknowledgement lost')
        with patch.object(repair, 'inspect_native'):
            with self.assertRaises(OSError):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
            saved = service.read_private(self.receipt.path)
            self.assertIn('paste_intent_at', saved['inputs']['0'])
            self.assertNotIn('enter_intent_at', saved['inputs']['0'])
            with self.assertRaisesRegex(ValueError, 'already has an intent'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.assertEqual(self.client.send_text.call_count, 1)
        self.client.send_key.assert_not_called()

    def test_lifecycle_change_after_enter_intent_prevents_the_actual_enter(self):
        def check(_client, _desc, slot, **kwargs):
            if 'enter_intent_at' in self.receipt.value['inputs'].get('0', {}):
                raise ValueError('different native turn')
        with patch.object(repair, 'inspect_native', side_effect=check):
            with self.assertRaisesRegex(ValueError, 'different native turn'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.client.send_text.assert_called_once_with(self.wid, self.slots[0]['surface_id'], repair.batch.PROMPT)
        self.client.send_key.assert_not_called()
        self.assertIn('enter_intent_at', service.read_private(self.receipt.path)['inputs']['0'])

    def test_uncertain_enter_cannot_be_resubmitted(self):
        self.client.send_key.side_effect = OSError('fixture Enter acknowledgement lost')
        with patch.object(repair, 'inspect_native'):
            with self.assertRaises(OSError):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
            with self.assertRaisesRegex(ValueError, 'already has an intent'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.assertEqual(self.client.send_text.call_count, 1)
        self.assertEqual(self.client.send_key.call_count, 1)

    def test_turn_change_during_paste_intent_persistence_prevents_paste(self):
        changed = False
        save = self.receipt.save
        def persist(phase, **fields):
            nonlocal changed
            save(phase, **fields)
            if phase == 'input_paste_intent':
                changed = True
        def inspect(*args, **kwargs):
            if changed:
                raise ValueError('original turn changed during persistence')
        with patch.object(self.receipt, 'save', side_effect=persist), \
             patch.object(repair, 'inspect_native', side_effect=inspect):
            with self.assertRaisesRegex(ValueError, 'turn changed'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.client.send_text.assert_not_called()
        self.client.send_key.assert_not_called()

    def test_pause_during_paste_intent_persistence_prevents_paste(self):
        save = self.receipt.save
        paused = False
        def persist(phase, **fields):
            nonlocal paused
            save(phase, **fields)
            if phase == 'input_paste_intent':
                paused = True
        def authorized(*args, **kwargs):
            if paused:
                raise ValueError('operator paused the workspace')
        with patch.object(self.receipt, 'save', side_effect=persist), \
             patch.object(repair, 'authorized', side_effect=authorized), \
             patch.object(repair, 'inspect_native'):
            with self.assertRaisesRegex(ValueError, 'operator paused'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.client.send_text.assert_not_called()
        self.client.send_key.assert_not_called()

    def test_pause_during_enter_intent_persistence_prevents_enter(self):
        save = self.receipt.save
        paused = False
        def persist(phase, **fields):
            nonlocal paused
            save(phase, **fields)
            if phase == 'input_enter_intent':
                paused = True
        def authorized(*args, **kwargs):
            if paused:
                raise ValueError('operator paused the workspace')
        with patch.object(self.receipt, 'save', side_effect=persist), \
             patch.object(repair, 'authorized', side_effect=authorized), \
             patch.object(repair, 'inspect_native'):
            with self.assertRaisesRegex(ValueError, 'operator paused'):
                repair.submit_originals(self.plan, {}, self.receipt, self.client)
        self.client.send_text.assert_called_once()
        self.client.send_key.assert_not_called()

    def test_receipt_is_exclusive_and_owner_is_rechecked(self):
        with self.assertRaises(FileExistsError):
            repair.Receipt(self.receipt.path, self.plan_path)
        value = service.read_private(self.receipt.path)
        value['id'] = 'different-owner'
        core.atomic_write_json(self.receipt.path, value)
        with self.assertRaisesRegex(ValueError, 'ownership changed'):
            self.receipt.save('must-not-overwrite')
        self.assertEqual(service.read_private(self.receipt.path)['id'], 'different-owner')

    def test_listener_with_any_outgoing_or_local_peer_is_rejected(self):
        good = 'p123\nf7\ntIPv4\nn127.0.0.1:54321\nTST=LISTEN\n'
        owner = {'pid': 123, 'port': 54321}
        with patch.object(repair.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=good)):
            self.assertEqual(len(repair.idle_listener(owner)), 1)
        for raw in (good + 'f8\nn127.0.0.1:54321->127.0.0.1:45678\nTST=ESTABLISHED\n',
                    good.replace('LISTEN', 'CLOSE_WAIT'), good.replace('p123', 'p124'), ''):
            with patch.object(repair.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=raw)):
                with self.assertRaises(ValueError):
                    repair.idle_listener(owner)


if __name__ == '__main__':
    unittest.main()
