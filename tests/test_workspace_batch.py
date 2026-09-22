"""Exercise 50 durable slots without creating real terminals or model calls."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests.test_watch import grid_payload


class BatchFixture:
    def __init__(self, test):
        self.test = test
        self.calls, self.sent, self.bindings, self.states = [], [], {}, {}
        self.open_file_sources = {}
        self.viewport_socket = None
        self.lose_create = self.lose_send = False
        self.frame_options = {}
        self.on_send = None

    def tree(self):
        return {'windows': [{'id': self.test.window, 'workspaces': [
            {'id': self.test.wid, 'ref': 'workspace:5', 'title': 'chosen', 'panes': [
                {'id': self.test.pane, 'focused': True, 'surfaces': [
                    {'id': sid, 'ref': f'surface:{index}', 'type': 'terminal'}
                    for index, sid in enumerate(self.calls, 1)]},
                {'id': 'dock', 'dock_scope': 'global', 'focused': True, 'surfaces': []}]},
            {'id': 'another', 'ref': 'workspace:6', 'panes': []}]}]}

    def top_all(self):
        return {'windows': []}

    def top(self, wid):
        return self.top_all()

    def new_codex_surface(self, window, wid, pane, command):
        self.test.assertEqual((window, wid, pane), (self.test.window, self.test.wid, self.test.pane))
        self.test.assertNotIn(batch.PROMPT, command)  # First prompt waits for readiness.
        tokens = shlex.split(command)
        jid = tokens[tokens.index('--job') + 1]
        index = int(tokens[tokens.index('--index') + 1])
        slot = core.load_json(batch.job_path(self.test.config, jid), {})['slots'][index]
        self.test.assertEqual(slot['phase'], 'creating')
        sid, session = str(uuid.uuid4()), str(uuid.uuid4())
        self.calls.append(sid)
        with patch.dict(os.environ, {'CMUX_SURFACE_ID': sid, 'CMUX_WORKSPACE_ID': wid}):
            batch.register(self.test.config, jid, index)
        path = self.test.root / f'{session}.jsonl'
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session}}) + '\n')
        self.bindings[session] = {'surfaceId': sid, 'workspaceId': wid, 'transcriptPath': str(path)}
        self.states[sid] = {'kind': 'unknown', 'session_id': session, 'pid': 1000 + index}
        if self.lose_create:
            raise core.CmuxError('create reply lost')
        return sid

    def current_turn(self, target):
        return dict(self.states[target['surface_id']])

    def records(self):
        return self.bindings

    def replay(self, wid, sid):
        self.test.assertEqual(wid, self.test.wid)
        frame = grid_payload([], **self.frame_options)
        frame['render_grid']['surface_id'] = sid
        return frame

    def send(self, wid, sid, message):
        self.test.assertEqual((wid, message), (self.test.wid, batch.PROMPT))
        job = core.load_json(self.test.worker.path, {})
        self.test.assertEqual(next(s for s in job['slots'] if s.get('surface_id') == sid)['phase'], 'submitting')
        self.test.assertIn(sid, self.test.store.load()['workspace_rules'][0]['excluded_surface_ids'])
        self.sent.append(sid)
        binding = self.bindings[self.states[sid]['session_id']]
        stamp = datetime.now(timezone.utc).isoformat()
        with Path(binding['transcriptPath']).open('a') as handle:
            # Codex can write task_started before the user_message event.
            for payload in ({'type': 'task_started', 'turn_id': 'first'},
                            {'type': 'user_message', 'message': message}):
                handle.write(json.dumps({'type': 'event_msg', 'timestamp': stamp, 'payload': payload}) + '\n')
        self.states[sid]['kind'] = 'task_started'
        if self.on_send:
            self.on_send()
        if self.lose_send:
            raise core.UncertainDeliveryError('send acknowledgement lost')


class WorkspaceBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.wid, self.window, self.pane = (str(uuid.uuid4()) for _ in range(3))
        self.store = core.ConfigStore(self.config)
        self.store.mutate(lambda c: c.update(mode='armed', global_paused=False))
        self.client = BatchFixture(self)
        self.job = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
        self.worker.job['status'] = 'running'

    def finish(self):
        for _ in range(110):
            if not self.worker.step():
                break
        return batch.counts(self.worker.job)

    def test_creates_exactly_50_pinned_tabs_and_confirms_every_original_start(self):
        duplicate = batch.start(self.config, 'workspace:5', client=self.client, launch=False)
        self.assertEqual(duplicate['job_id'], self.job['job_id'])
        result = self.finish()
        self.assertEqual(result, {'created': 50, 'ready': 50, 'submitted': 50, 'started': 50, 'failed': 0, 'total': 50})
        self.assertEqual(len(self.client.calls), len(set(self.client.calls)))
        self.assertEqual(self.client.sent, self.client.calls)
        self.assertEqual(self.worker.job['status'], 'complete')
        self.assertEqual(self.store.load()['workspace_rules'][0]['excluded_surface_ids'], [])

    def test_lost_create_and_send_replies_are_reconciled_without_duplicates(self):
        self.client.lose_create = self.client.lose_send = True
        result = self.finish()
        self.assertEqual(result['started'], 50)
        self.assertEqual(len(self.client.calls), 50)
        self.assertEqual(len(self.client.sent), 50)

    def test_restart_after_submit_uses_transcript_instead_of_repeating_prompt(self):
        self.worker.step()
        self.worker.step()
        self.assertEqual(len(self.client.sent), 1)
        self.worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
        self.assertEqual(self.finish()['started'], 50)
        self.assertEqual(len(self.client.sent), 50)

    def test_pool_pause_cancels_creation_and_prompt_submission_even_after_resume(self):
        self.worker.step()
        with patch.object(core, 'discover_codex_surfaces', return_value=[]):
            core.pause_workspace(self.store, self.wid, self.client)
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=False))
        self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.job['status'], 'cancelled')
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.sent, [])

    def test_drafts_menus_and_existing_tasks_are_not_overwritten(self):
        self.worker.step()
        first = self.client.calls[0]
        for options in ({'composer': 'busy'}, {'menu': True}, {'working': True}):
            self.client.frame_options = options
            self.worker._advance(self.worker.job['slots'][0])
            self.assertEqual(self.client.sent, [])
        self.client.frame_options = {}
        self.client.states[first]['kind'] = 'task_started'
        self.worker._advance(self.worker.job['slots'][0])
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.worker.job['slots'][0]['phase'], 'blocked')

    def test_ambiguous_creation_is_never_replayed_on_restart(self):
        slot = self.worker.job['slots'][0]
        slot.update(phase='creating', created_at=time.time() - 30)
        self.worker.save()
        self.worker.step()
        self.assertEqual(slot['phase'], 'create_unknown')
        self.assertNotIn('surface_id', slot)
        self.assertEqual(len(self.client.calls), 1)  # Only the next untouched slot.

    def test_authorization_and_user_exclusions_are_preserved(self):
        self.store.mutate(lambda c: c['workspace_rules'][0].update(
            excluded_surface_ids=['user-excluded'], excluded_surface_reasons={'user-excluded': 'user'}, paused=True))
        original = copy.deepcopy(self.store.load())
        with self.assertRaisesRegex(RuntimeError, '暂停'):
            batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertEqual(self.store.load(), original)
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=False))
        self.finish()
        self.assertEqual(self.store.load()['workspace_rules'][0]['excluded_surface_ids'], ['user-excluded'])

    def test_draft_appearing_during_final_preflight_prevents_first_prompt(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        original = self.client.replay
        calls = []
        def read(wid, sid):
            calls.append(sid)
            if len(calls) == 2:
                self.client.frame_options = {'composer': 'busy'}
            return original(wid, sid)
        with patch.object(self.client, 'replay', side_effect=read):
            self.worker._advance(slot)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.client.sent, [])
        self.assertNotIn('submit_at', slot)

    def test_acknowledgement_without_original_task_start_is_not_success(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.object(self.client, 'send') as send:
            self.worker._advance(slot)
            self.worker._advance(slot)
            self.worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
            self.worker._advance(self.worker.job['slots'][0])
        send.assert_called_once()
        self.assertEqual(self.worker.job['slots'][0]['phase'], 'submitted')
        self.assertEqual(batch.counts(self.worker.job)['started'], 0)


if __name__ == '__main__':
    unittest.main()
