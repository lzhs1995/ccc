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
from unittest.mock import Mock, patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests.test_watch import grid_payload, span


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
        self.test.assertEqual(core.batch_start_hold(self.test.store.load()['workspace_rules'][0], sid)['job_id'], job['id'])
        self.sent.append(sid)
        binding = self.bindings[self.states[sid]['session_id']]
        stamp = datetime.fromtimestamp(self.test.worker.clock(), timezone.utc).isoformat()
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

    def send_text(self, wid, sid, message):
        self.send(wid, sid, message)


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
        self.now = time.time()
        self.worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client, clock=lambda: self.now)
        self.addCleanup(self.worker.cache.close)
        self.worker.job['status'] = 'running'

    def finish(self):
        self.worker.clock = lambda: self.now
        for _ in range(110):
            self.now += 1
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

    def test_native_millisecond_timestamp_can_confirm_the_new_task(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.object(self.client, 'send'):
            self.worker._advance(slot)
        stamp = datetime.fromtimestamp(slot['submit_at'], timezone.utc).isoformat(timespec='milliseconds')
        with Path(slot['transcript']).open('a') as handle:
            for payload in ({'type': 'task_started'}, {'type': 'user_message', 'message': batch.PROMPT}):
                handle.write(json.dumps({'type': 'event_msg', 'timestamp': stamp, 'payload': payload}) + '\n')
        self.worker._advance(slot)
        self.assertEqual(slot['phase'], 'confirmed')

    def test_lazy_native_session_is_confirmed_only_by_its_later_original_transcript(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        sid = slot['surface_id']
        native = {**self.client.states[sid], 'kind': 'uninitialized', 'process_start': slot['created_at']}
        binding = self.client.bindings[native['session_id']]
        path = Path(binding['transcriptPath'])
        path.unlink()
        with patch.object(self.client, 'current_turn', return_value={'kind': 'unknown'}), \
                patch.object(self.client, 'initial_session', return_value=native, create=True), \
                patch.object(self.client, 'records', return_value={}), patch.object(self.client, 'send_text') as send:
            self.worker._advance(slot)
            self.assertEqual(slot['phase'], 'submitted')
            self.assertEqual(slot['transcript'], '')
            send.assert_called_once()
        self.client.states[sid] = {**native, 'kind': 'task_started'}
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [
            {'type': 'session_meta', 'payload': {'id': native['session_id']}},
            {'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'task_started'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': '<environment_context>test</environment_context>'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': batch.PROMPT}]}},
        ]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        self.worker._advance(slot)
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertNotIn(sid, self.store.load()['workspace_rules'][0]['excluded_surface_ids'])

    def test_separate_enter_requires_exact_recorded_draft_and_is_never_repeated(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.object(self.client, 'send_text'):
            self.worker._advance(slot)
        self.worker.clock = lambda: slot['submit_at'] + 1
        frame = grid_payload([])
        grid = frame['render_grid']
        row = grid['cursor']['row']
        grid['row_spans'] = [s for s in grid['row_spans'] if not (s['row'] == row and s['column'] >= 2)]
        draft = span(row, 2, batch.PROMPT, 0)
        grid['row_spans'].append(draft)
        grid['cursor']['column'] = 2 + len(batch.PROMPT)
        grid['surface_id'] = slot['surface_id']
        with patch.object(self.client, 'replay', return_value=frame), \
                patch.object(self.client, 'send_key', create=True, side_effect=core.UncertainDeliveryError('reply lost')) as enter:
            draft['text'] = 'x' + batch.PROMPT[1:]
            self.worker._advance(slot)
            enter.assert_not_called()
            draft['text'] = batch.PROMPT
            self.worker._advance(slot)
            self.assertIn('enter_attempt_at', slot)
            self.worker._advance(slot)
            enter.assert_called_once_with(self.wid, slot['surface_id'], 'enter')
        self.assertEqual(slot['phase'], 'uncertain')

    def test_exact_draft_accepts_text_merged_with_prompt_padding(self):
        for column, prefix in ((0, '› '), (1, ' '), (2, '')):
            with self.subTest(column=column):
                frame = grid_payload([])
                grid = frame['render_grid']
                row = grid['cursor']['row']
                grid['row_spans'] = [s for s in grid['row_spans'] if s['row'] != row]
                if column:
                    grid['row_spans'].append(span(row, 0, '›', 0))
                grid['row_spans'].append(span(row, column, prefix + batch.PROMPT + '   ', 0))
                grid['cursor']['column'] = 2 + len(batch.PROMPT)
                self.assertTrue(batch.BatchWorker._own_prompt_draft(core.Grid.from_rpc(frame, 'test')))

    def test_merged_prompt_padding_does_not_hide_extra_draft_text(self):
        for column, text in ((0, '›x' + batch.PROMPT), (1, 'x' + batch.PROMPT),
                             (1, ' ' + batch.PROMPT + 'x')):
            with self.subTest(column=column, text=text):
                frame = grid_payload([])
                grid = frame['render_grid']
                row = grid['cursor']['row']
                grid['row_spans'] = [s for s in grid['row_spans'] if s['row'] != row]
                if column:
                    grid['row_spans'].append(span(row, 0, '›', 0))
                grid['row_spans'].append(span(row, column, text, 0))
                grid['cursor']['column'] = 2 + len(batch.PROMPT)
                self.assertFalse(batch.BatchWorker._own_prompt_draft(core.Grid.from_rpc(frame, 'test')))

    def test_response_item_from_a_different_human_prompt_is_not_batch_confirmation(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.object(self.client, 'send_text'):
            self.worker._advance(slot)
        stamp = datetime.now(timezone.utc).isoformat()
        with Path(slot['transcript']).open('a') as out:
            for row in [
                {'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'task_started'}},
                *({'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}
                  for text in ('another user task', batch.PROMPT)),
            ]:
                out.write(json.dumps(row) + '\n')
        self.assertFalse(self.worker._confirm(slot))


if __name__ == '__main__':
    unittest.main()
