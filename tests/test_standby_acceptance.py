"""Original first-task proof, durable receipt, and scoped hold release."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import uuid

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_acceptance as acceptance
from tests import test_standby_launch as fixtures


class FirstTaskTests(unittest.TestCase):
    start_native = fixtures.StandbyLaunchTests.start_native
    hook_fixture = fixtures.StandbyLaunchTests.hook_fixture
    bind = fixtures.StandbyLaunchTests.bind

    def setUp(self):
        fixtures.StandbyLaunchTests.setUp(self)
        payload = self.hook_fixture()
        # Native UserTurn has ts; the older Hook fixture did not need it.
        path = Path(self.claim['tui_log'])
        events = [json.loads(line) for line in path.read_bytes().splitlines()]
        self.submitted = self.claim['at'] + .1
        events[-1]['ts'] = datetime.fromtimestamp(self.submitted, timezone.utc).isoformat()
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        self.transcript = self.native_home / 'sessions' / ('rollout-original-' + self.session + '.jsonl')
        self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.session}}) + '\n')
        self.bind({**payload, 'transcript_path': str(self.transcript)})
        self.enterContext(patch.object(acceptance, 'process', side_effect=lambda *a, **k: copy.deepcopy(self.native)))
        self.born = self.enterContext(patch.object(acceptance, 'birth', return_value=self.native['birth']))
        self.enterContext(patch.object(acceptance, 'process_writable_files', side_effect=lambda *a, **k: copy.deepcopy(self.files)))
        self.enterContext(patch.object(acceptance, 'boot_id', return_value=self.boot))
        self.observer = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action,
            client=self.client, clock=lambda: self.submitted + .1)
        self.job_before = self.worker.path.read_bytes()
        self.result_path = self.worker.path.parent / 'standby-first-task-0.json'

    def task(self, message=batch.PROMPT, *, turn='first', session=None, at=None):
        if session:
            self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session}}) + '\n')
        stamp = datetime.fromtimestamp(at or self.submitted + .01, timezone.utc).isoformat()
        with self.transcript.open('a') as handle:
            for payload in ({'type': 'task_started', 'turn_id': turn},
                            {'type': 'user_message', 'message': message}):
                handle.write(json.dumps({'type': 'event_msg', 'timestamp': stamp, 'payload': payload}) + '\n')

    def hold(self):
        return core.batch_start_hold(self.store.load()['workspace_rules'][0], self.slot['surface_id'])

    def test_original_first_task_releases_only_after_receipt_without_mutating_job(self):
        self.assertIsNone(self.observer.poll(0))
        self.assertTrue(self.hold())
        self.task()
        original = self.observer.store.mutate
        def mutate(callback):
            self.assertTrue(self.result_path.exists())
            return original(callback)
        with patch.object(self.observer.store, 'mutate', side_effect=mutate):
            result = self.observer.poll(0)
        self.assertEqual(result['action_id'], self.action)
        self.assertEqual(result['confirmation']['task_id'], 'first')
        self.assertFalse(self.hold())
        self.assertEqual(self.worker.path.read_bytes(), self.job_before)
        self.assertEqual(self.observer.poll(0), result)
        self.assertFalse(self.client.sent)

    def test_missing_hook_never_substitutes_ack(self):
        self.task()
        (self.worker.path.parent / 'standby-session-0.json').unlink()
        self.assertIsNone(self.observer.poll(0))
        self.assertTrue(self.hold())
        self.assertFalse(self.result_path.exists())

    def test_wrong_task_then_matching_later_task_never_releases(self):
        self.task('operator task')
        with self.assertRaises(ValueError):
            self.observer.poll(0)
        self.task()
        with self.assertRaisesRegex(ValueError, 'permanently'):
            self.observer.poll(0)
        self.assertTrue(self.hold())

    def test_foreign_session_and_old_timestamp_rejected(self):
        for change in ({'session': str(uuid.uuid4())}, {'at': self.submitted - 2}):
            with self.subTest(change=change):
                self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.session}}) + '\n')
                self.task(**change)
                observer = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action, client=self.client)
                with self.assertRaises(ValueError):
                    observer.poll(0)
                self.assertTrue(self.hold())

    def test_pid_reuse_and_wrong_writer_reject(self):
        self.task()
        self.born.return_value = [1234, 9999]
        with self.assertRaises(ValueError):
            self.observer.poll(0)
        self.assertTrue(self.hold())
        self.assertFalse(self.result_path.exists())

    def test_receipt_write_failure_cannot_release(self):
        self.task()
        with patch.object(acceptance, 'write_once', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.observer.poll(0)
        self.assertTrue(self.hold())

    def test_pause_during_receipt_persistence_prevents_release(self):
        self.task()
        original = acceptance.write_once
        def save(path, value):
            raw = original(path, value)
            self.store.mutate(lambda c: c.update(global_paused=True))
            return raw
        with patch.object(acceptance, 'write_once', side_effect=save):
            with self.assertRaisesRegex(ValueError, 'no longer authorized'):
                self.observer.poll(0)
        self.assertTrue(self.result_path.exists())
        self.assertTrue(self.hold())

    def test_manual_exclusion_and_foreign_hold_are_preserved(self):
        self.task()
        sid = self.slot['surface_id']
        def exclude(config):
            rule = config['workspace_rules'][0]
            rule.setdefault('excluded_surface_ids', []).append(sid)
            rule.setdefault('excluded_surface_reasons', {})[sid] = 'manual'
        self.store.mutate(exclude)
        with self.assertRaises(ValueError):
            self.observer.poll(0)
        self.assertEqual(self.store.load()['workspace_rules'][0]['excluded_surface_reasons'][sid], 'manual')
        self.assertTrue(self.hold())

    def test_persisted_receipt_restart_is_observation_only_and_no_input(self):
        self.task()
        first = self.observer.poll(0, release=False)
        restarted = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action, client=self.client)
        self.assertEqual(restarted.poll(0), first)
        self.assertFalse(self.hold())
        self.assertFalse(self.client.sent)

    def test_no_hook_path_uses_original_writable_rollout_without_history_scan(self):
        path = self.worker.path.parent / 'standby-session-0.json'
        hook = json.loads(path.read_bytes())
        hook['transcript'] = None
        core.atomic_write_json(path, hook)
        self.task()
        self.files[self.transcript] = dict(device=self.transcript.stat().st_dev, inode=self.transcript.stat().st_ino)
        with patch.object(Path, 'rglob', side_effect=AssertionError('no history scan')):
            self.assertTrue(self.observer.poll(0)['confirmation']['confirmed'])

    def test_wrong_action_does_not_observe_old_activation(self):
        with self.assertRaises(ValueError):
            acceptance.FirstTaskObserver(self.config, self.worker.job['id'], str(uuid.uuid4()), client=self.client)
        self.assertTrue(self.hold())

    def test_hook_precomputed_path_may_wait_for_first_persistence(self):
        self.transcript.unlink()
        self.assertIsNone(self.observer.poll(0))
        self.assertTrue(self.hold())
        self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.session}}) + '\n')
        self.task()
        self.assertTrue(self.observer.poll(0)['confirmation']['confirmed'])

    def test_original_writer_replaced_during_process_read_rejects(self):
        self.task()
        calls = []
        def changed(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                self.lock.rename(self.lock.with_suffix('.old'))
                self.lock.write_bytes(b'')
            return copy.deepcopy(self.native)
        with patch.object(acceptance, 'process', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'writer changed'):
                self.observer.poll(0)
        self.assertTrue(self.hold())

    def test_confirmed_transcript_rewrite_cannot_release_cached_proof(self):
        self.task()
        self.observer.poll(0, release=False)
        original = self.transcript.read_bytes()
        self.transcript.write_bytes(original.replace(b'first', b'other'))
        with self.assertRaisesRegex(ValueError, 'prefix changed'):
            self.observer.poll(0)
        self.assertTrue(self.hold())

    def test_restarted_receipt_rejects_rewritten_context_even_if_same_first_task(self):
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'original'}}) + '\n')
        self.task()
        self.observer.poll(0, release=False)
        self.transcript.write_bytes(self.transcript.read_bytes().replace(b'original', b'replaced'))
        restarted = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action, client=self.client)
        with self.assertRaisesRegex(ValueError, 'prefix changed'):
            restarted.poll(0)
        self.assertTrue(self.hold())

    def test_later_append_does_not_reject_original_confirmation(self):
        self.task()
        first = self.observer.poll(0, release=False)
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
        self.assertEqual(self.observer.poll(0), first)
        self.assertFalse(self.hold())


if __name__ == '__main__':
    unittest.main()
