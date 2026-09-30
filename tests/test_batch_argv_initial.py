"""Initial argv tasks are confirmed from the original durable native hook."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch, Mock
import unittest

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class ArgvInitialTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def prepare(self):
        slot = self.worker.job['slots'][0]
        self.worker._create(slot)
        self.worker.job['initial_prompt_policy'] = batch.ARGV_INITIAL_POLICY
        self.worker.save()
        self.client.sessions_root = self.root
        self.client.workspace_tree = lambda wid: self.client.tree()
        self.slot = slot
        self.session = self.client.states[slot['surface_id']]['session_id']
        self.hook = self.client.bindings[self.session]
        self.hook['transcriptPath'] = str(Path(self.hook['transcriptPath']).resolve())
        self.hook.update(pid=3456, pidStartSeconds=1234, pidStartMicroseconds=5678,
                         sessionId=self.session)
        self.receipt = self.worker.path.parent / 'surface-0.json'
        self.claim = {'policy': batch.ARGV_INITIAL_POLICY, 'job_id': self.worker.job['id'],
                      'workspace_id': self.wid, 'surface_id': slot['surface_id'], 'index': 0,
                      'launch_id': slot['launch_id'], 'bootstrap_pid': 3456,
                      'bootstrap_birth': [1234, 5678], 'at': self.now,
                      'argv': ['/native/codex', batch.PROMPT], 'state': 'exec_intent',
                      'receipt_sha256': hashlib.sha256(self.receipt.read_bytes()).hexdigest()}
        self.claim_path = self.worker.path.parent / 'initial-argv-0.json'
        self.first_path = self.worker.path.parent / 'initial-session-0.json'
        self.event_path = self.worker.path.parent / 'initial-native-events-0.jsonl'
        self.header = (json.dumps({'dir': 'meta', 'kind': 'session_start',
            'cwd': str(batch.working_directory(self.config, self.worker.job['id'], 0)),
            'ts': datetime.fromtimestamp(self.now, timezone.utc).isoformat()}) + '\n').encode()
        self.event_path.write_bytes(self.header)
        st = self.event_path.stat()
        self.claim.update(cwd=str(batch.working_directory(self.config, self.worker.job['id'], 0)),
                          tui_log=str(self.event_path), tui_log_identity=[st.st_dev, st.st_ino])

    def claim_and_task(self, prompt=batch.PROMPT):
        core.atomic_write_json(self.claim_path, self.claim)
        core.atomic_write_json(self.first_path, {
            'claim_sha256': hashlib.sha256(self.claim_path.read_bytes()).hexdigest(),
            'session_id': self.session, 'pid': 3456, 'birth': [1234, 5678],
            'surface_id': self.slot['surface_id'], 'workspace_id': self.wid,
            'source': 'startup', 'transcript': self.hook['transcriptPath'],
            'sessions_root': str(self.root.resolve()), 'tui_prefix_bytes': len(self.header),
            'tui_prefix_sha256': hashlib.sha256(self.header).hexdigest()})
        self.event_path.write_bytes(self.header + (
            json.dumps({'dir': 'to_tui', 'kind': 'app_event', 'variant': 'StartupThreadStarted'}) + '\n' +
            json.dumps({'dir': 'from_tui', 'kind': 'op', 'payload': {'UserTurn': {
                'items': [{'type': 'text', 'text': batch.PROMPT}]}}}) + '\n').encode())
        stamp = datetime.fromtimestamp(self.now + .01, timezone.utc).isoformat()
        path = Path(self.hook['transcriptPath'])
        with path.open('a') as handle:
            for payload in ({'type': 'task_started', 'turn_id': 'first'},
                            {'type': 'user_message', 'message': prompt},
                            {'type': 'task_complete', 'turn_id': 'first'}):
                handle.write(json.dumps({'type': 'event_msg', 'timestamp': stamp,
                                         'payload': payload}) + '\n')

    def test_new_explicit_check_selects_argv_but_historical_job_does_not(self):
        self.assertFalse(batch.argv_initial(self.worker.job, self.config))
        self.prepare()
        self.assertTrue(batch.argv_initial(self.worker.job, self.config))
        argv = batch.native_launch_argv(self.config, self.worker.job, 0)
        self.assertEqual(argv[-1], batch.PROMPT)
        self.assertEqual(argv.count(batch.PROMPT), 1)
        self.assertIn(['--disable', 'skill_search'], [argv[i:i+2] for i in range(len(argv)-1)])

    def test_native_worker_environment_is_explicit_and_legacy_unchanged(self):
        historical = {k:v for k,v in self.worker.job.items() if k != 'native_runtime_policy'}
        self.assertEqual(batch.native_launch_environment(historical), {})
        self.prepare()
        self.worker.job['native_runtime_policy'] = batch.NATIVE_RUNTIME_POLICY
        self.assertEqual(batch.native_launch_environment(self.worker.job), {'TOKIO_WORKER_THREADS':'2'})
        self.worker.job['native_runtime_policy'] = 'unknown'
        with self.assertRaises(RuntimeError):
            batch.native_launch_environment(self.worker.job)

    def test_incompatible_protocol_never_silently_falls_back(self):
        self.prepare()
        for key, value in [('initial_prompt', 'other'), ('name_policy', 'before-first-turn-v1'),
                           ('guard_version', 1), ('check_retry_policy', 'other')]:
            with self.subTest(key=key):
                job = {**self.worker.job, key: value}
                with self.assertRaises(RuntimeError):
                    batch.argv_initial(job, self.config)

    def test_argv_step_does_not_wait_for_unused_global_process_inventory(self):
        self.prepare()
        with patch.object(self.client, 'top_all', side_effect=RuntimeError('inventory unavailable')) as top, \
             patch.object(self.worker, '_dispatch_slots') as dispatch:
            self.assertTrue(self.worker.step())
        top.assert_not_called()
        self.assertTrue(dispatch.called)
        self.assertNotEqual(self.worker.job.get('preparation_wait', {}).get('kind'), 'process_inventory')

    def test_legacy_startup_retains_process_inventory(self):
        self.worker.job.pop('native_runtime_policy', None)
        with patch.object(self.client, 'top_all', return_value={}) as top, \
             patch.object(self.worker, '_dispatch_slots'):
            self.worker.step()
        top.assert_called_once()

    def test_fast_completed_exited_native_is_confirmed_without_any_input(self):
        self.prepare()
        self.claim_and_task()
        with patch.object(self.client, 'current_turn', side_effect=AssertionError('not live')), \
             patch.object(self.client, 'send') as send, patch.object(self.client, 'send_key') as enter:
            self.worker._advance(self.slot)
            self.assertEqual(self.slot['phase'], 'confirmed')
            self.assertTrue(self.slot['confirmation']['confirmed'])
            self.worker._advance(self.slot)
        send.assert_not_called()
        enter.assert_not_called()
        self.assertFalse(core.batch_start_hold(self.store.load()['workspace_rules'][0], self.slot['surface_id']))

    def test_wrong_original_prompt_then_matching_later_turn_is_not_confirmed(self):
        self.prepare()
        self.claim_and_task('user task')
        self.claim_and_task()
        self.worker._advance(self.slot)
        self.assertNotEqual(self.slot['phase'], 'confirmed')
        self.assertEqual(self.slot['confirmation']['blocked'], 'different user prompt')
        self.assertFalse(self.client.sent)

    def test_microsecond_pid_reuse_cannot_bind_session(self):
        self.prepare()
        self.claim_and_task()
        record = json.loads(self.first_path.read_bytes())
        record['birth'][1] += 1
        core.atomic_write_json(self.first_path, record)
        self.worker._advance(self.slot)
        self.assertNotIn('argv_claim_sha256', self.slot)
        self.assertFalse(self.client.sent)

    def test_global_hook_cannot_replace_missing_immutable_first_receipt(self):
        self.prepare()
        self.claim_and_task()
        self.client.bindings['second'] = dict(self.hook)
        self.first_path.unlink()
        self.worker._advance(self.slot)
        self.assertNotIn('argv_claim_sha256', self.slot)
        self.assertFalse(self.client.sent)

    def test_hook_pins_only_original_startup_once(self):
        self.prepare()
        self.claim['cwd'] = str(batch.working_directory(self.config, self.worker.job['id'], 0))
        core.atomic_write_json(self.claim_path, self.claim)
        native = {'pid': 3456, 'birth': [1234, 5678], 'surface_id': self.slot['surface_id'],
                  'environment_workspace_id': self.wid, 'argv': self.claim['argv'],
                  'environment': {'CODEX_HOME': str(self.root)}}
        payload = {'hook_event_name': 'SessionStart', 'source': 'startup',
                   'session_id': self.session, 'cwd': self.claim['cwd'], 'transcript_path': None}
        with patch('ccc_guard_scope.process', return_value=native), \
             patch('ccc_guard_scope.birth', return_value=[1234, 5678]), \
             patch('ccc_codex_queue.process_writable_files', return_value={self.event_path.resolve(): {
                 'device': self.event_path.stat().st_dev, 'inode': self.event_path.stat().st_ino}}):
            batch.bind_initial_session(self.config, self.worker.job['id'], 0, self.slot['launch_id'], payload)
            original = self.first_path.read_bytes()
            with self.assertRaises(FileExistsError):
                batch.bind_initial_session(self.config, self.worker.job['id'], 0, self.slot['launch_id'], payload)
            with self.assertRaisesRegex(RuntimeError, 'original native startup'):
                batch.bind_initial_session(self.config, self.worker.job['id'], 0, self.slot['launch_id'],
                                           {**payload, 'source': 'clear'})
        self.assertEqual(self.first_path.read_bytes(), original)

    def test_missed_hook_then_later_new_session_cannot_bind_as_first(self):
        self.prepare()
        self.claim_and_task()
        self.first_path.unlink()
        self.event_path.write_bytes(self.header + b'{"dir":"to_tui","kind":"new_session"}\n')
        native = {'pid': 3456, 'birth': [1234, 5678], 'surface_id': self.slot['surface_id'],
                  'environment_workspace_id': self.wid, 'argv': self.claim['argv'],
                  'environment': {'CODEX_HOME': str(self.root)}}
        payload = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': self.session,
                   'cwd': self.claim['cwd'], 'transcript_path': None}
        with patch('ccc_guard_scope.process', return_value=native), \
             patch('ccc_guard_scope.birth', return_value=[1234, 5678]), \
             patch('ccc_codex_queue.process_writable_files', return_value={self.event_path.resolve(): {
                 'device': self.event_path.stat().st_dev, 'inode': self.event_path.stat().st_ino}}):
            with self.assertRaisesRegex(RuntimeError, 'session changed'):
                batch.bind_initial_session(self.config, self.worker.job['id'], 0, self.slot['launch_id'], payload)
        self.assertFalse(self.first_path.exists())
        self.assertTrue((self.worker.path.parent / 'initial-hook-attempt-0.json').exists())

    def test_unknown_or_partial_claim_never_enters_ui_or_respawn(self):
        self.prepare()
        for contents in (None, b'', b'{'):
            with self.subTest(contents=contents):
                if contents is not None:
                    self.claim_path.write_bytes(contents)
                self.worker._advance(self.slot)
                self.worker._finish_submission(self.slot)
                self.worker._restart_failed(self.slot)
        self.assertFalse(self.client.sent)
        self.assertFalse(self.client.rename_enter)

    def test_restored_worker_only_confirms_original_first_task(self):
        self.prepare()
        self.claim_and_task()
        restored = batch.BatchWorker(self.config, self.worker.job['id'], client=self.client,
                                     queue=self.client, clock=lambda: self.now)
        self.addCleanup(restored.close)
        restored._advance(restored.job['slots'][0])
        self.assertEqual(restored.job['slots'][0]['phase'], 'confirmed')
        self.assertFalse(self.client.sent)

    def test_final_pause_after_claim_prevents_exec(self):
        self.prepare()
        self.event_path.unlink()  # Bootstrap creates the original event inode.
        executable = self.root / 'native'
        executable.write_text('fixture')
        claim = batch._claim_argv_initial
        def paused(*args):
            result = claim(*args)
            self.store.mutate(lambda c: c.update(global_paused=True))
            return result
        with patch.dict(os.environ, CMUX_SURFACE_ID=self.slot['surface_id'], CMUX_WORKSPACE_ID=self.wid), \
             patch('ccc_guard_scope.birth', return_value=[1234, 5678]), \
             patch.object(batch, '_client', return_value=self.client), \
             patch.object(batch, '_claim_argv_initial', side_effect=paused), \
             patch.object(os, 'execv') as execute:
            with self.assertRaisesRegex(RuntimeError, 'authorization changed'):
                batch._launch_initial_registered(self.config, self.worker.job, 0,
                    self.slot['launch_id'], [str(executable), batch.PROMPT])
        execute.assert_not_called()
        self.assertTrue(self.claim_path.exists())


if __name__ == '__main__':
    unittest.main()
