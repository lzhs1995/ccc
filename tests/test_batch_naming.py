"""Name only a fresh owned session; metadata is not evidence of API success."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class BatchNamingTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def fresh(self):
        self.initial_name = ''
        self.worker.job['name_policy'] = 'before-first-turn-v1'
        self.worker.job['slots'] = self.worker.job['slots'][:1]
        self.worker.save()
        self.worker.step()
        return self.worker.job['slots'][0]

    def advance(self, slot):
        self.now += 1
        self.worker._advance(slot)

    def test_name_is_confirmed_before_exactly_one_model_prompt(self):
        slot = self.fresh()
        self.advance(slot)
        self.assertEqual(len(self.client.rename_sent), 1)
        self.assertEqual(self.client.sent, [])
        self.advance(slot)
        self.assertEqual(len(self.client.rename_enter), 1)
        self.assertEqual(self.client.sent, [])
        self.advance(slot)
        self.assertEqual(self.client.sent, [slot['surface_id']])
        self.assertIn('confirmed_name', slot['naming'])
        self.advance(slot)
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertEqual(len(self.client.rename_sent), 1)
        self.assertEqual(len(self.client.rename_enter), 1)

    def test_existing_operator_name_is_preserved_without_any_rename(self):
        slot = self.fresh()
        self.client.names[slot['surface_id']] = 'Keep my name'
        self.advance(slot)
        self.assertEqual(self.client.names[slot['surface_id']], 'Keep my name')
        self.assertEqual(self.client.rename_sent, [])
        self.assertEqual(self.client.rename_enter, [])
        self.assertEqual(self.client.sent, [slot['surface_id']])

    def test_lost_text_acknowledgement_does_not_repeat_the_draft(self):
        slot = self.fresh()
        send = self.client.send_text
        def uncertain(*args):
            send(*args)
            raise core.CmuxError('lost draft acknowledgement')
        with patch.object(self.client, 'send_text', side_effect=uncertain):
            self.advance(slot)
        for _ in range(3):
            self.advance(slot)
        self.assertEqual(len(self.client.rename_sent), 1)
        self.assertEqual(len(self.client.rename_enter), 1)
        self.assertEqual(len(self.client.sent), 1)

    def test_uncertain_transport_without_a_draft_is_never_replayed(self):
        slot = self.fresh()
        with patch.object(self.client, 'draft_batch_session_name', side_effect=core.CmuxError('unknown receipt')) as send:
            for _ in range(4):
                self.advance(slot)
        send.assert_called_once()
        self.assertIn('submitted_at', slot['naming'])
        self.assertEqual(self.client.rename_enter, [])
        self.assertEqual(self.client.sent, [])

    def test_lost_enter_acknowledgement_uses_native_metadata_without_replay(self):
        slot = self.fresh()
        self.advance(slot)
        enter = self.client.send_key
        def uncertain(*args):
            enter(*args)
            raise core.CmuxError('lost Enter acknowledgement')
        with patch.object(self.client, 'send_key', side_effect=uncertain):
            self.advance(slot)
        for _ in range(3):
            self.advance(slot)
        self.assertEqual(len(self.client.rename_enter), 1)
        self.assertEqual(len(self.client.sent), 1)

    def test_operator_draft_is_not_submitted_as_our_rename(self):
        slot = self.fresh()
        self.advance(slot)
        self.client.rename_drafts[slot['surface_id']] = 'operator draft'
        for _ in range(3):
            self.advance(slot)
        self.assertEqual(self.client.rename_enter, [])
        self.assertEqual(self.client.sent, [])

    def test_original_session_change_never_receives_enter_or_prompt(self):
        slot = self.fresh()
        self.advance(slot)
        self.client.states[slot['surface_id']]['session_id'] = 'another-original-session'
        self.advance(slot)
        self.assertEqual(self.client.rename_enter, [])
        self.assertEqual(self.client.sent, [])

    def test_pause_after_draft_is_preserved_without_enter(self):
        slot = self.fresh()
        self.advance(slot)
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        self.advance(slot)
        self.assertEqual(self.client.rename_enter, [])
        self.assertEqual(self.client.sent, [])
        self.assertTrue(self.store.load()['workspace_rules'][0]['paused'])

    def test_metadata_read_failure_does_not_guess_or_send(self):
        slot = self.fresh()
        self.worker.name_lookup = lambda *_: None
        self.advance(slot)
        self.assertEqual(self.client.rename_sent, [])
        self.assertEqual(self.client.sent, [])

    def test_started_operator_task_is_never_renamed(self):
        slot = self.fresh()
        self.client.states[slot['surface_id']]['kind'] = 'task_started'
        self.advance(slot)
        self.assertEqual(slot['phase'], 'blocked')
        self.assertEqual(self.client.rename_sent, [])
        self.assertEqual(self.client.sent, [])

    def test_pre_send_identity_uncertainty_is_deferred_without_losing_the_slot(self):
        slot = self.fresh()
        original = self.worker._native
        # Identity can become temporarily unavailable after the intent was
        # saved. No terminal call has happened, so this is not uncertain I/O.
        count = 0
        def once_uncertain(*args):
            nonlocal count
            count += 1
            return None if count == 4 else original(*args)
        with patch.object(self.worker, '_native', side_effect=once_uncertain):
            self.advance(slot)
        self.assertEqual(self.client.rename_sent, [])
        self.assertNotIn('submitted_at', slot['naming'])
        for _ in range(4):
            self.advance(slot)
        self.assertEqual(len(self.client.rename_sent), 1)
        self.assertEqual(len(self.client.rename_enter), 1)
        self.assertEqual(len(self.client.sent), 1)

    def test_pre_enter_identity_uncertainty_keeps_exact_draft_for_later_check(self):
        slot = self.fresh()
        self.advance(slot)
        original = self.worker._native
        count = 0
        def once_uncertain(*args):
            nonlocal count
            count += 1
            return None if count == 4 else original(*args)
        with patch.object(self.worker, '_native', side_effect=once_uncertain):
            self.advance(slot)
        self.assertEqual(self.client.rename_enter, [])
        self.assertNotIn('enter_at', slot['naming'])
        for _ in range(3):
            self.advance(slot)
        self.assertEqual(len(self.client.rename_sent), 1)
        self.assertEqual(len(self.client.rename_enter), 1)
        self.assertEqual(len(self.client.sent), 1)

    def test_known_unsent_draft_survives_worker_restart(self):
        slot = self.fresh()
        original = self.worker.save
        def paused_after_intent():
            original()
            if slot.get('naming', {}).get('submitted_at'):
                self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        with patch.object(self.worker, 'save', side_effect=paused_after_intent):
            self.advance(slot)
        self.assertEqual(self.client.rename_sent, [])
        self.assertNotIn('submitted_at', core.load_json(self.worker.path, {})['slots'][0]['naming'])
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=False))
        self.worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client,
                                        clock=lambda: self.now, pty_probe=lambda: True)
        self.addCleanup(self.worker.cache.close)
        for _ in range(4):
            self.advance(self.worker.job['slots'][0])
        self.assertEqual(len(self.client.rename_sent), 1)
        self.assertEqual(len(self.client.rename_enter), 1)
        self.assertEqual(len(self.client.sent), 1)

    def test_pre_send_exclusion_is_rechecked_and_never_overridden(self):
        slot = self.fresh()
        save = self.worker.save
        def exclude_after_intent():
            save()
            if slot.get('naming', {}).get('submitted_at'):
                self.store.mutate(lambda c: c['workspace_rules'][0].update(
                    excluded_surface_ids=[slot['surface_id']],
                    excluded_surface_reasons={slot['surface_id']: 'operator'}))
        with patch.object(self.worker, 'save', side_effect=exclude_after_intent):
            self.advance(slot)
        self.advance(slot)
        self.assertEqual(self.client.rename_sent, [])
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.store.load()['workspace_rules'][0]['excluded_surface_reasons'],
                         {slot['surface_id']: 'operator'})


class NativeNameOwnershipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = {'surface_id': 'same-surface', 'workspace_id': 'same-workspace'}
        self.native = {'pid': 123, 'process_start': 456, 'session_id': 'original-session'}
        self.process = {'pid': 123, 'process_start': 456, 'birth': [456, 789], 'remote': False,
                        'surface_id': 'SAME-SURFACE', 'environment_workspace_id': 'SAME-WORKSPACE',
                        'environment': {'CODEX_HOME': str(self.root)}}
        process = patch('ccc_guard_scope.process', side_effect=lambda *_args, **_kw: self.process)
        birth = patch('ccc_guard_scope.birth', return_value=[456, 789])
        process.start(); self.addCleanup(process.stop)
        birth.start(); self.addCleanup(birth.stop)
        self.path = self.root / 'session_index.jsonl'

    def test_uses_the_verified_process_home_and_latest_exact_session_name(self):
        rows = [{'id': 'original-session', 'thread_name': 'earlier'},
                {'id': 'unrelated-session', 'thread_name': 'not ours'},
                {'id': 'original-session', 'thread_name': 'current'}]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.assertEqual(batch.native_thread_name(self.target, self.native),
                         {'name': 'current', 'birth': [456, 789]})

    def test_foreign_or_remote_process_cannot_authorize_naming(self):
        for change in ({'remote': True}, {'environment_workspace_id': 'FOREIGN'},
                       {'surface_id': 'FOREIGN'}, {'process_start': 457}):
            with self.subTest(change=change), patch.dict(self.process, change):
                self.assertIsNone(batch.native_thread_name(self.target, self.native))

    def test_recycled_pid_or_partial_index_is_not_an_empty_name(self):
        with patch('ccc_guard_scope.birth', return_value=[456, 790]):
            self.assertIsNone(batch.native_thread_name(self.target, self.native))
        self.path.write_text('{"id":"original-session"')
        self.assertIsNone(batch.native_thread_name(self.target, self.native))

    def test_absent_index_is_empty_only_with_original_live_identity(self):
        self.assertEqual(batch.native_thread_name(self.target, self.native)['name'], '')


class BatchNamingTransportTests(unittest.TestCase):
    def test_typed_method_only_drafts_the_exact_local_name(self):
        client = core.CmuxClient('/unused/cmux')
        wid, sid, jid = (str(uuid.uuid4()) for _ in range(3))
        with patch.object(client, '_control_rpc', return_value={}) as rpc, patch.object(client, '_run') as run:
            client.draft_batch_session_name(wid, sid, jid, 0)
        rpc.assert_called_once_with('surface.send_text', {
            'workspace_id': wid, 'surface_id': sid, 'text': f'/rename B-check-{jid[:8]}-01'})
        run.assert_not_called()
        with self.assertRaises(RuntimeError):
            client.send_text(wid, sid, '/rename arbitrary text')

    def test_missing_identity_and_out_of_range_slot_never_send(self):
        client = core.CmuxClient('/unused/cmux')
        wid, sid, jid = (str(uuid.uuid4()) for _ in range(3))
        with patch.object(client, '_control_rpc') as rpc, patch.object(client, '_run') as run:
            for values in [('', sid, jid, 0), (wid, '', jid, 0), (wid, sid, '../job', 0),
                           (wid, sid, jid, -1), (wid, sid, jid, 50), (wid, sid, jid, True)]:
                with self.subTest(values=values), self.assertRaises(core.CmuxError):
                    client.draft_batch_session_name(*values)
        rpc.assert_not_called(); run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
