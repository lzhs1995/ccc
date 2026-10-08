"""Original first-task proof, durable receipt, and scoped hold release."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch
import unittest
from tests.context_fixture import enter_context
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
        enter_context(self, patch.object(acceptance, 'process', side_effect=lambda *a, **k: copy.deepcopy(self.native)))
        self.born = enter_context(self, patch.object(acceptance, 'birth', return_value=self.native['birth']))
        enter_context(self, patch.object(acceptance, 'process_writable_files', side_effect=lambda *a, **k: copy.deepcopy(self.files)))
        enter_context(self, patch.object(acceptance, 'boot_id', return_value=self.boot))
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

    def test_injected_inventory_rechecks_final_writer_before_release(self):
        self.task()
        calls = []
        def read(*args, **kwargs):
            calls.append(1)
            return copy.deepcopy(self.files) if len(calls) == 1 else {}
        self.observer.files_reader = read
        with self.assertRaisesRegex(ValueError, 'writer changed'):
            self.observer.poll(0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.hold())
        self.assertFalse(self.result_path.exists())

    def test_shared_inventory_cancellation_preserves_hold(self):
        from ccc_standby_prepare import InventoryReader
        self.task()
        allowed = [True]
        def read(*args, **kwargs):
            allowed[0] = False
            return copy.deepcopy(self.files)
        reader = InventoryReader(read, lambda: allowed[0], limit=1)
        self.observer.files_reader = reader
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.observer.poll(0)
        self.assertEqual(reader.capacity, 1)
        self.assertFalse(reader.waiters)
        self.assertTrue(self.hold())
        self.assertFalse(self.result_path.exists())

    def test_shared_inventory_success_keeps_fresh_reads(self):
        from ccc_standby_prepare import InventoryReader
        self.task()
        calls = []
        def read(*args, **kwargs):
            calls.append(1)
            return copy.deepcopy(self.files)
        reader = InventoryReader(read, lambda: True, limit=1)
        self.observer.files_reader = reader
        self.assertTrue(self.observer.poll(0)['confirmation']['confirmed'])
        self.assertGreaterEqual(len(calls), 6)
        self.assertFalse(self.hold())
        self.assertEqual(reader.capacity, 1)
        self.assertFalse(reader.waiters)

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

    def test_evidence_wait_does_not_hold_config_lock(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.task()
        self.observer.poll(0, release=False)
        acquired = threading.Event()
        release = threading.Event()
        entered = threading.Event()
        original_lock = self.observer._evidence_lock
        class TracedLock:
            def __enter__(inner):
                if acquired.is_set():
                    entered.set()
                original_lock.acquire()
                return inner
            def __exit__(inner, *exc):
                original_lock.release()
        self.observer._evidence_lock = TracedLock()
        def hold():
            with original_lock:
                acquired.set()
                if not release.wait(5):
                    raise AssertionError('test evidence holder did not release')
        holders = []
        def before_config(*args):
            thread = threading.Thread(target=hold)
            holders.append(thread)
            thread.start()
            self.assertTrue(acquired.wait(2))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch('ccc_private_check.record_origin', side_effect=before_config):
                future = pool.submit(self.observer.poll, 0)
                try:
                    self.assertTrue(entered.wait(2))
                    # A live permission change must not wait behind evidence IO.
                    store = core.ConfigStore(self.config, timeout_sec=.1)
                    store.mutate(lambda config: config.update(global_paused=True))
                finally:
                    release.set()
                    for thread in holders:
                        thread.join(2)
                with self.assertRaisesRegex(ValueError, 'no longer authorized'):
                    future.result(timeout=3)
        self.assertTrue(self.hold())

    def test_incomplete_inventory_waits_without_releasing_then_rechecks(self):
        self.task()
        for error in (acceptance.IncompleteVnodeRead(9, 'closed fd'),
                      acceptance.VnodeInventoryChanged('changed')):
            with patch.object(acceptance, 'process_writable_files', side_effect=error):
                self.assertIsNone(self.observer.poll(0))
                self.assertIsNone(self.observer.poll(0))
            self.assertTrue(self.hold())
            self.assertFalse(self.result_path.exists())
            self.assertNotIn(0, self.observer._failed)
        self.assertTrue(self.observer.poll(0)['confirmation']['confirmed'])
        self.assertFalse(self.hold())
        self.assertFalse(self.client.sent)

    def test_incomplete_final_read_never_releases_cached_confirmation(self):
        self.task()
        original = self.observer._live
        calls = []
        def live(*args):
            calls.append(1)
            if len(calls) == 3:
                raise acceptance.IncompleteVnodeRead(9, 'final read')
            return original(*args)
        with patch.object(self.observer, '_live', side_effect=live):
            self.assertIsNone(self.observer.poll(0))
        self.assertTrue(self.hold())
        self.assertFalse(self.result_path.exists())
        self.born.return_value = [999, 1]
        with self.assertRaises(ValueError):
            self.observer.poll(0)
        self.assertTrue(self.hold())
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

    def test_live_discovery_uses_final_writable_inventory(self):
        row, claim, hook = self.observer._bind(0)
        initial = copy.deepcopy(self.files)
        initial[self.transcript] = dict(device=self.transcript.stat().st_dev,
                                       inode=self.transcript.stat().st_ino)
        final = {p: value for p, value in initial.items() if p != self.transcript}
        with patch.object(acceptance, 'process_writable_files', side_effect=[initial, final]):
            root, files = self.observer._live(row, claim, hook)
        self.assertNotIn(self.transcript, files)
        self.assertIsNone(self.observer._transcript_path(row, {**hook, 'transcript': None}, root, files))
        self.assertTrue(self.hold())
        self.assertFalse(self.result_path.exists())

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
