"""Coalesced bootstrap commits using real files, locks, and worker threads."""
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class BatchRegistrationTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def requests(self, count=50):
        result = []
        for slot in self.worker.job['slots'][:count]:
            slot.update(phase='creating', launch_id=str(uuid.uuid4()))
            request = dict(job_id=self.worker.job['id'], index=slot['index'],
                           request_id=str(uuid.uuid4()), surface_id=str(uuid.uuid4()),
                           workspace_id=self.wid, launch_id=slot['launch_id'],
                           requested_at=self.now, shell_pid=123, shell_start='original-shell')
            batch._publish_registration(self.config, request)
            result.append(request)
        self.worker.save()
        return result

    def commit(self):
        return batch._commit_registration_requests(self.config, self.worker.job['id'])

    def another_batch(self):
        wid, jid = str(uuid.uuid4()), str(uuid.uuid4())
        job = dict(id=jid, workspace_id=wid, config_path=str(self.config), created_at=self.now,
                   status='running', slots=[])
        requests = []
        for index in range(50):
            launch_id = str(uuid.uuid4())
            job['slots'].append(dict(index=index, phase='creating', launch_id=launch_id))
            request = dict(job_id=jid, index=index, request_id=str(uuid.uuid4()),
                           surface_id=str(uuid.uuid4()), workspace_id=wid, launch_id=launch_id,
                           requested_at=self.now, shell_pid=123, shell_start='other-original-shell')
            batch._publish_registration(self.config, request)
            requests.append(request)
        path = batch.job_path(self.config, jid)
        core.atomic_write_json(path, job)
        def authorize(config):
            rule = core._workspace_rule_from_record(dict(workspace_id=wid, ref='', title='other fixture'))
            rule.update(active_batch_id=jid, last_batch_id=jid)
            config['workspace_rules'].append(rule)
        self.store.mutate(authorize)
        return path, requests

    def test_fifty_ready_requests_share_one_real_config_commit(self):
        requests = self.requests()
        before = self.worker.path.read_bytes()
        original, writes = core.atomic_write_json, []
        def write(path, value):
            if path == self.config:
                writes.append(copy.deepcopy(value))
            return original(path, value)
        with patch.object(core, 'atomic_write_json', side_effect=write):
            self.assertTrue(self.commit())
            self.assertTrue(self.commit())
        self.assertEqual(len(writes), 1)
        holds = core.workspace_rule_by_id(self.store.load(), self.wid)['batch_start_holds']
        self.assertEqual(set(holds), {request['surface_id'] for request in requests})
        for request in requests:
            receipt = batch._registration_outcome(self.worker.path.parent, request)
            self.assertEqual(receipt['request_id'], request['request_id'])
            self.assertEqual(receipt['launch_id'], request['launch_id'])
        self.assertEqual(self.worker.path.read_bytes(), before)
        self.assertEqual(self.client.sent, [])

    def test_fifty_real_threads_cannot_duplicate_the_group_commit(self):
        requests = self.requests()
        barrier, lock, writes = threading.Barrier(50), threading.Lock(), []
        original = core.atomic_write_json
        def write(path, value):
            if path == self.config:
                with lock:
                    writes.append(1)
            return original(path, value)
        def run(_):
            barrier.wait(10)
            return self.commit()
        with patch.object(core, 'atomic_write_json', side_effect=write), ThreadPoolExecutor(50) as pool:
            list(pool.map(run, range(50)))
        self.assertEqual(len(writes), 1)
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) for request in requests))

    def test_no_native_receipt_is_visible_before_config_is_durable(self):
        requests = self.requests()
        entered, release = threading.Event(), threading.Event()
        original = core.atomic_write_json
        def write(path, value):
            if path == self.config:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('test did not release config commit')
            return original(path, value)
        with patch.object(core, 'atomic_write_json', side_effect=write), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.commit)
            try:
                self.assertTrue(entered.wait(10))
                self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) is None
                                    for request in requests))
            finally:
                release.set()
            self.assertTrue(future.result(10))

    def test_pause_after_requests_vetoes_every_native_launch(self):
        requests = self.requests()
        self.store.mutate(lambda config: config['workspace_rules'][0].update(paused=True))
        self.commit()
        for request in requests:
            with self.assertRaisesRegex(RuntimeError, 'paused or cancelled'):
                batch._registration_outcome(self.worker.path.parent, request)
        self.assertEqual(core.workspace_rule_by_id(self.store.load(), self.wid).get('batch_start_holds', {}), {})

    def test_one_excluded_surface_does_not_hold_the_other_forty_nine(self):
        requests = self.requests()
        self.store.mutate(lambda config: config['workspace_rules'][0].setdefault('excluded_surface_ids', []).append(requests[0]['surface_id']))
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'excluded'):
            batch._registration_outcome(self.worker.path.parent, requests[0])
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) for request in requests[1:]))

    def test_stale_launch_request_cannot_receive_a_native_receipt(self):
        requests = self.requests()
        self.worker.job['slots'][0]['launch_id'] = str(uuid.uuid4())
        self.worker.save()
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'stale batch launch'):
            batch._registration_outcome(self.worker.path.parent, requests[0])
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) for request in requests[1:]))

    def test_interrupted_receipt_write_reconciles_without_another_config_write(self):
        requests = self.requests()
        original, writes = core.atomic_write_json, []
        fail = True
        def write(path, value):
            nonlocal fail
            if path == self.config:
                writes.append(1)
            if path.name == 'surface-0.json' and fail:
                fail = False
                raise OSError('receipt disk write interrupted')
            return original(path, value)
        with patch.object(core, 'atomic_write_json', side_effect=write):
            with self.assertRaisesRegex(OSError, 'disk write interrupted'):
                self.commit()
            self.assertTrue(self.commit())
        self.assertEqual(len(writes), 1)
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) for request in requests))

    def test_busy_shared_config_retains_requests_without_acknowledging(self):
        requests = self.requests()
        original = core.ConfigStore
        with core.FileLock(self.config.parent / 'config.lock'), patch.object(core, 'ConfigStore',
                side_effect=lambda path: original(path, timeout_sec=0)):
            self.assertFalse(self.commit())
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) is None for request in requests))
        self.assertTrue(self.commit())

    def test_existing_surface_and_slot_hold_cannot_be_rebound(self):
        requests = self.requests(2)
        other = str(uuid.uuid4())
        self.worker.job['slots'][0]['surface_id'] = other
        self.worker.save()
        self.store.mutate(lambda config: config['workspace_rules'][0].setdefault('batch_start_holds', {}).update({
            other: {'job_id': self.worker.job['id'], 'index': 1, 'created_at': self.now}}))
        self.commit()
        for request in requests:
            with self.assertRaisesRegex(RuntimeError, 'original surface'):
                batch._registration_outcome(self.worker.path.parent, request)

    def test_two_active_batches_share_one_config_commit(self):
        first = self.requests()
        path, second = self.another_batch()
        original, writes = core.atomic_write_json, []
        def write(target, value):
            if target == self.config:
                writes.append(1)
            return original(target, value)
        with patch.object(core, 'atomic_write_json', side_effect=write):
            self.commit()
        self.assertEqual(writes, [1])
        for directory, requests in ((self.worker.path.parent, first), (path.parent, second)):
            self.assertTrue(all(batch._registration_outcome(directory, request) for request in requests))
            self.assertTrue(all((directory / f"registration-{request['index']}.json").exists() for request in requests))
        self.assertEqual(list((self.config.parent / 'batch-registration-ready').glob('*.json')), [])

    def test_paused_pool_does_not_prevent_other_pool_receipts(self):
        first = self.requests()
        path, second = self.another_batch()
        self.store.mutate(lambda config: config['workspace_rules'][0].update(paused=True))
        self.commit()
        for request in first:
            with self.assertRaisesRegex(RuntimeError, 'paused or cancelled'):
                batch._registration_outcome(self.worker.path.parent, request)
        self.assertTrue(all(batch._registration_outcome(path.parent, request) for request in second))

    def test_changed_request_snapshot_is_retained_without_a_native_receipt(self):
        requests = self.requests()
        changed = {**requests[0], 'surface_id': str(uuid.uuid4())}
        core.atomic_write_json(self.worker.path.parent / 'registration-0.json', changed)
        self.commit()
        self.assertIsNone(batch._registration_outcome(self.worker.path.parent, requests[0]))
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) for request in requests[1:]))
        self.assertTrue((self.config.parent / 'batch-registration-ready' / f"{requests[0]['request_id']}.stale").exists())

    def test_invalid_queue_record_cannot_block_valid_bootstraps(self):
        requests = self.requests()
        bad = self.config.parent / 'batch-registration-ready' / 'invalid.json'
        bad.write_text('{')
        self.commit()
        self.assertTrue(bad.with_suffix('.invalid').exists())
        self.assertTrue(all(batch._registration_outcome(self.worker.path.parent, request) for request in requests))


if __name__ == '__main__':
    unittest.main()
