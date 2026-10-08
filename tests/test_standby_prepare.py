"""Preparation composition with an isolated real generation socket."""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch
import uuid

import ccc_standby_prepare as prep
from tests.test_standby_bootstrap import descriptor


class InventoryReaderTests(unittest.TestCase):
    def test_eight_fresh_reads_and_all_fifty_complete(self):
        release, full = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, peak, seen = 0, 0, []
        def read(index, *, identities):
            nonlocal active, peak
            self.assertTrue(identities)
            with lock:
                active += 1
                peak = max(peak, active)
                seen.append(index)
                if active == 8:
                    full.set()
            try:
                if not release.wait(3):
                    raise TimeoutError('missing inventory release')
                return {'original': index}
            finally:
                with lock:
                    active -= 1
        reader = prep.InventoryReader(read, lambda: True)
        with ThreadPoolExecutor(max_workers=50) as pool:
            futures = [pool.submit(reader, i, identities=True) for i in range(50)]
            try:
                self.assertTrue(full.wait(2))
                self.assertEqual(peak, 8)
            finally:
                release.set()
            self.assertEqual([f.result(timeout=3) for f in futures],
                             [{'original': i} for i in range(50)])
        self.assertEqual(sorted(seen), list(range(50)))
        self.assertEqual(peak, 8)
        self.assertEqual(reader.capacity, 8)

    def test_fifo_cancelled_waiter_and_failed_read_release_capacity(self):
        live, release, entered = threading.Event(), threading.Event(), threading.Event()
        live.set()
        seen = []
        def read(index):
            seen.append(index)
            if index == 0:
                entered.set()
                if not release.wait(3): raise TimeoutError('missing release')
            if index == 7: raise OSError('inventory changed')
            return index
        reader = prep.InventoryReader(read, live.is_set, limit=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(reader, 0)
            self.assertTrue(entered.wait(2))
            later = []
            try:
                for index in (3, 2, 1):
                    later.append(pool.submit(reader, index))
                    deadline = time.monotonic() + 2
                    with reader.condition:
                        while len(reader.waiters) != len(later):
                            self.assertLess(time.monotonic(), deadline)
                            reader.condition.wait(.01)
            finally:
                release.set()
            self.assertEqual([f.result(timeout=2) for f in [first, *later]], [0, 3, 2, 1])
        self.assertEqual(seen, [0, 3, 2, 1])
        with self.assertRaises(OSError): reader(7)
        self.assertEqual(reader(8), 8)
        reader.capacity = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(reader, 9)
            deadline = time.monotonic() + 2
            with reader.condition:
                while not reader.waiters:
                    self.assertLess(time.monotonic(), deadline)
                    reader.condition.wait(.01)
            live.clear()
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                waiting.result(timeout=1)
        self.assertNotIn(9, seen)
        self.assertFalse(reader.waiters)

    def test_revocation_during_read_discards_result(self):
        allowed = [True]
        def read():
            allowed[0] = False
            return {'must_not_escape': True}
        reader = prep.InventoryReader(read, lambda: allowed[0])
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            reader()
        self.assertEqual(reader.capacity, 8)


class FreshTopologyTests(unittest.TestCase):
    def test_awake_reader_serves_pending_wave_when_head_is_not_scheduled(self):
        self.assisted_wave(False)

    def test_assisted_failed_wave_is_delivered_once_to_all_pending_readers(self):
        self.assisted_wave(True)

    def assisted_wave(self, fail):
        real_event = threading.Event
        entered, release = real_event(), real_event()
        delayed, resume = real_event(), real_event()
        completed = real_event()
        values, errors, reads = {}, {}, []
        class DelayedWake:
            def __init__(self):
                self.event = real_event()
            def set(self):
                self.event.set()
            def clear(self):
                self.event.clear()
            def wait(self):
                self.event.wait()
                delayed.set()
                if not resume.wait(3):
                    raise TimeoutError('test head release missing')
        def event_factory():
            return DelayedWake() if threading.current_thread().name == 'delayed-head' else real_event()
        def read():
            reads.append(threading.current_thread().name)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test first read release missing')
            elif fail:
                raise OSError('fresh assisted read failed')
            return {'generation': len(reads), 'mutable': []}
        fresh = prep.FreshTopology(read)
        def run(name):
            try:
                values[name] = fresh()
            except BaseException as exc:
                errors[name] = exc
            finally:
                if name == 'helper':
                    completed.set()
        threads = [threading.Thread(target=run, args=(name,), name=name)
                   for name in ('first', 'delayed-head', 'helper')]
        with patch.object(prep.threading, 'Event', event_factory):
            try:
                threads[0].start()
                self.assertTrue(entered.wait(3))
                threads[1].start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with fresh.condition:
                        queued = len(fresh.pending)
                    if queued == 1:
                        break
                    time.sleep(.001)
                self.assertEqual(queued, 1)
                release.set()
                self.assertTrue(delayed.wait(3))
                threads[2].start()
                self.assertTrue(completed.wait(1), 'idle reader waits for unscheduled head')
            finally:
                release.set()
                resume.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(reads, ['first', 'helper'])
        self.assertEqual(values['first']['generation'], 1)
        if fail:
            self.assertEqual(set(errors), {'delayed-head', 'helper'})
            self.assertTrue(all(isinstance(exc, OSError) for exc in errors.values()))
        else:
            self.assertEqual(errors, {})
            self.assertEqual(values['delayed-head']['generation'], 2)
            self.assertEqual(values['helper']['generation'], 2)
            self.assertIsNot(values['helper']['mutable'], values['delayed-head']['mutable'])

    def test_waiter_copies_do_not_hold_admission_lock(self):
        entered, release = threading.Event(), threading.Event()
        reads = []
        class Snapshot:
            def __deepcopy__(self, memo):
                if fresh.condition._is_owned():
                    raise AssertionError('snapshot copy holds shared admission lock')
                return {'independent': []}
        def read():
            reads.append(True)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('missing test release')
            return Snapshot()
        fresh = prep.FreshTopology(read)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(fresh)
            self.assertTrue(entered.wait(3))
            waiters = [pool.submit(fresh) for _ in range(2)]
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with fresh.condition:
                        count = len(fresh.pending)
                    if count == 2:
                        break
                    time.sleep(.001)
                self.assertEqual(count, 2)
            finally:
                release.set()
            values = [f.result(timeout=3) for f in [first, *waiters]]
        self.assertEqual(len(reads), 2)
        self.assertIsNot(values[1]['independent'], values[2]['independent'])

    def test_late_readers_require_new_snapshot_and_share_only_pending_wave(self):
        self.exercise(False)

    def test_failed_wave_releases_waiters_without_reusing_or_retrying_it(self):
        self.exercise(True)

    def exercise(self, fail):
        entered, release = threading.Event(), threading.Event()
        reads = []
        def read():
            reads.append(len(reads) + 1)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test release missing')
                if fail:
                    raise OSError('original RPC failed')
            return {'snapshot': len(reads), 'surfaces': [len(reads)]}
        fresh = prep.FreshTopology(read)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(fresh)
            self.assertTrue(entered.wait(3))
            later = [pool.submit(fresh) for _ in range(3)]
            try:
                deadline = time.monotonic() + 3
                while True:
                    with fresh.condition:
                        count = len(fresh.pending)
                    if count == 3 or time.monotonic() >= deadline:
                        break
                    time.sleep(.001)
                self.assertEqual(count, 3)
            finally:
                release.set()
            if fail:
                with self.assertRaisesRegex(OSError, 'original RPC failed'):
                    first.result(timeout=3)
            else:
                self.assertEqual(first.result(timeout=3)['snapshot'], 1)
            values = [f.result(timeout=3) for f in later]
        self.assertEqual([v['snapshot'] for v in values], [2, 2, 2])
        values[0]['surfaces'].clear()
        self.assertEqual(values[1]['surfaces'], [2])
        self.assertEqual(fresh()['snapshot'], 3)  # No cached sequential read.
        self.assertEqual(reads, [1, 2, 3])


class PreparationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ccc-prep-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / 'config.json'
        self.job = descriptor(self.config)
        self.sid = str(uuid.uuid4())
        self.sid = getattr(self, 'controller_id', str)(self.sid)
        self.job['workspace_id'] = getattr(self, 'controller_id', str)(self.job['workspace_id'])
        self.wid = self.job['workspace_id']
        config = prep.core.default_config()
        config.update(mode='armed', global_paused=False, targets=[], workspace_rules=[{
            'workspace_id': self.wid, 'enabled': True, 'active_batch_id': self.job['id'],
            'batch_start_holds': {self.sid: {'job_id': self.job['id'], 'index': 0}}}])
        prep.core.atomic_write_json(self.config, config)
        self.jobfile = prep.batch.job_path(self.config, self.job['id'])
        prep.core.atomic_write_json(self.jobfile, self.job)
        self.directory = self.root / 'owner'
        self.directory.mkdir(mode=0o700)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.pin = Mock()
        self.pin.current.return_value = self.job['standby_generation']
        self.client = Mock()
        self.client.workspace_tree.return_value = {'windows': [{'id': str(uuid.uuid4()),
            'workspaces': [{'id': self.wid, 'panes': [{'id': str(uuid.uuid4()),
                'surfaces': [{'id': self.sid, 'type': 'terminal'}]}]}]}]}
        self.writes = []
        self.guard = None
        @contextlib.contextmanager
        def guarded(check):
            self.guard = check
            yield
        self.client.input_guard.side_effect = guarded
        def create(*args, **kwargs):
            if self.before_write:
                self.before_write()
            if not self.guard():
                raise ValueError('actual create guard refused')
            self.writes.append((args, kwargs))
            if self.unknown:
                raise OSError('ACK lost')
            return self.sid
        self.before_write = None
        self.unknown = False
        self.client.new_codex_surface.side_effect = create
        self.rollouts = Mock()
        p = patch.object(prep, 'RolloutInventory', return_value=self.rollouts)
        self.inventory = p.start(); self.addCleanup(p.stop)
        p = patch.object(prep, 'boot_id', return_value=self.job['standby_boot_id'])
        p.start(); self.addCleanup(p.stop)
        self.reader = Mock()
        p = patch.object(prep, 'ProcessInventoryReader', return_value=self.reader)
        p.start(); self.addCleanup(p.stop)
        self.owner = prep.PreparationOwner(self.config, self.job['id'], directory=self.directory,
            client=self.client, source_pin=self.pin, sessions_root=self.sessions,
            target_environment={'HOME': str(self.root)})
        self.addCleanup(self.owner.close)

    def revoke(self):
        prep.core.ConfigStore(self.config).mutate(lambda cfg: cfg.update(global_paused=True))

    def test_connected_authorization_coalesces_only_pending_admitted_readers(self):
        local = threading.local()
        self.client.viewport_socket._connection_local = local
        tree = self.client.workspace_tree.return_value
        tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['ref'] = 'surface:1'
        entered, release = threading.Event(), threading.Event()
        readers = []
        def read(workspace):
            self.assertEqual(workspace, self.wid)
            self.assertTrue(callable(local.read_rpc))
            readers.append(threading.get_ident())
            if len(readers) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('connected test release missing')
            return tree
        self.client.workspace_tree.side_effect = read
        self.owner._topology = Mock(side_effect=AssertionError('unadmitted reader used'))
        def authorize():
            local.read_rpc = lambda *args: None
            try:
                return self.owner._authorized(0, surface_id=self.sid, connected=self.client)
            finally:
                local.read_rpc = None
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(authorize)
            self.assertTrue(entered.wait(3))
            later = [pool.submit(authorize) for _ in range(3)]
            try:
                deadline = time.monotonic() + 3
                count = 0
                while time.monotonic() < deadline:
                    with self.owner._connected_topology.condition:
                        count = len(self.owner._connected_topology.pending)
                    if count == 3:
                        break
                    time.sleep(.001)
                self.assertEqual(count, 3)
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3))
            self.assertTrue(all(f.result(timeout=3) for f in later))
        self.assertEqual(len(readers), 2)
        self.assertTrue(authorize())
        self.assertEqual(len(readers), 3)  # A later final check never reuses a wave.

    def test_permission_config_shares_pending_wave_but_final_read_sees_pause(self):
        entered, release = threading.Event(), threading.Event()
        reads = []
        load = prep.core.ConfigStore.load
        def read(store):
            value = load(store)
            reads.append(value)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('permission release missing')
            return value
        with patch.object(prep.core.ConfigStore, 'load', read):
            with ThreadPoolExecutor(max_workers=4) as pool:
                first = pool.submit(self.owner._permission, 0, self.sid)
                self.assertTrue(entered.wait(3))
                later = [pool.submit(self.owner._permission, 0, self.sid) for _ in range(3)]
                try:
                    deadline = time.monotonic() + 3
                    while True:
                        with self.owner._permission_config.condition:
                            count = len(self.owner._permission_config.pending)
                        if count == 3 or time.monotonic() >= deadline:
                            break
                        time.sleep(.001)
                    self.assertEqual(count, 3)
                finally:
                    release.set()
                self.assertTrue(first.result(3))
                self.assertEqual([f.result(3) for f in later], [True] * 3)
            self.assertEqual(len(reads), 2)
            value = load(prep.core.ConfigStore(self.config))
            value['global_paused'] = True
            prep.core.atomic_write_json(self.config, value)
            self.assertFalse(self.owner._permission(0, self.sid))
            self.assertEqual(len(reads), 3)
            self.assertTrue(self.owner._failed.is_set())

    def test_permission_config_checks_lifetime_after_blocking_load(self):
        load = prep.core.ConfigStore.load
        hits = []
        def read(store):
            value = load(store)
            hits.append(True)
            self.owner._closed.set()
            return value
        with patch.object(prep.core.ConfigStore, 'load', read):
            self.assertFalse(self.owner._permission(0, self.sid))
        self.assertEqual(hits, [True])
        self.assertFalse(self.writes)

    def test_source_waiters_require_fresh_wave_and_share_only_queued_checks(self):
        entered, release = threading.Event(), threading.Event()
        reads = []
        def read():
            reads.append(len(reads) + 1)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('source release missing')
            return self.job['standby_generation']
        self.pin.current.side_effect = read
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.owner._current)
            self.assertTrue(entered.wait(3))
            later = [pool.submit(self.owner._current) for _ in range(3)]
            try:
                deadline = time.monotonic() + 3
                while True:
                    with self.owner._source_current.condition:
                        count = len(self.owner._source_current.pending)
                    if count == 3 or time.monotonic() >= deadline:
                        break
                    time.sleep(.001)
                self.assertEqual(count, 3)
            finally:
                release.set()
            self.assertEqual(first.result(3), self.job['standby_generation'])
            self.assertEqual([f.result(3) for f in later],
                             [self.job['standby_generation']] * 3)
        self.assertEqual(reads, [1, 2])
        self.pin.current.side_effect = lambda: 'changed'
        with self.assertRaisesRegex(ValueError, 'source generation changed'):
            self.owner._current()

    def test_source_shared_read_does_not_hide_lifetime_revocation(self):
        def read():
            self.owner._closed.set()
            return self.job['standby_generation']
        self.pin.current.side_effect = read
        with self.assertRaisesRegex(ValueError, 'owner closed'):
            self.owner._current()
        # Let the normal cleanup close owned resources.
        self.owner._closed.clear()

    def test_source_failure_remains_permanent(self):
        self.pin.current.side_effect = OSError('source watcher failed')
        with self.assertRaisesRegex(OSError, 'source watcher failed'):
            self.owner._current()
        self.pin.current.side_effect = None
        with self.assertRaisesRegex(ValueError, 'owner previously failed'):
            self.owner._current()

    def test_job_final_read_rejects_change_inside_source_check(self):
        def source():
            self.jobfile.write_bytes(self.owner._job_raw + b' ')
            return self.job['standby_generation']
        self.pin.current.side_effect = source
        with self.assertRaisesRegex(ValueError, 'original job changed'):
            self.owner._current()
        self.assertFalse(self.writes)

    def test_job_final_read_rejects_same_bytes_symlink(self):
        target = self.root / 'same-job.json'
        target.write_bytes(self.owner._job_raw)
        def source():
            self.jobfile.unlink()
            self.jobfile.symlink_to(target)
            return self.job['standby_generation']
        self.pin.current.side_effect = source
        with self.assertRaisesRegex(ValueError, 'original job changed'):
            self.owner._current()
        self.assertFalse(self.writes)

    def test_job_read_waiters_do_not_inherit_inflight_result(self):
        entered, release = threading.Event(), threading.Event()
        read = self.owner._job_current.read
        count = []
        def blocked():
            value = read()
            count.append(True)
            if len(count) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('job read release missing')
            return value
        self.owner._job_current.read = blocked
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.owner._job_current)
            self.assertTrue(entered.wait(3))
            later = [pool.submit(self.owner._job_current) for _ in range(3)]
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with self.owner._job_current.condition:
                        pending = len(self.owner._job_current.pending)
                    if pending == 3:
                        break
                    time.sleep(.001)
                self.assertEqual(pending, 3)
                self.jobfile.write_bytes(self.owner._job_raw + b' ')
            finally:
                release.set()
            self.assertTrue(first.result(3))
            self.assertEqual([f.result(3) for f in later], [False] * 3)
        self.assertEqual(len(count), 2)
        with self.assertRaisesRegex(ValueError, 'original job changed'):
            self.owner._current()

    def test_job_read_does_not_hide_owner_close(self):
        read = self.owner._job_current.read
        def close():
            value = read()
            self.owner._closed.set()
            return value
        self.owner._job_current.read = close
        try:
            with self.assertRaisesRegex(ValueError, 'owner closed'):
                self.owner._current()
            self.assertFalse(self.writes)
        finally:
            self.owner._closed.clear()

    def test_original_creation_intent_precedes_write_and_is_never_replayed(self):
        def check():
            value = json.loads((self.directory / 'create-intent-0.json').read_bytes())
            self.assertEqual(value['launch_id'], self.job['slots'][0]['launch_id'])
            self.assertEqual(value['job_id'], self.job['id'])
        self.before_write = check
        self.assertEqual(self.owner.launch_one(0), self.sid)
        self.assertTrue(self.writes[0][1]['clean_shell'])
        self.assertIn(str(self.owner.bridge.spec_path), self.writes[0][0][3])
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(json.loads(self.jobfile.read_bytes()), self.job)

    def test_unknown_create_permanently_consumes_original(self):
        self.unknown = True
        with self.assertRaises(OSError): self.owner.launch_one(0)
        self.unknown = False
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertEqual(len(self.writes), 1)
        self.assertTrue((self.directory / 'create-intent-0.json').exists())
        self.assertFalse((self.directory / 'create-ack-0.json').exists())

    def test_known_ack_preserved_when_source_changes_after_create(self):
        create = self.client.new_codex_surface.side_effect
        def changed(*args, **kwargs):
            result = create(*args, **kwargs)
            self.pin.current.return_value = 'changed'
            return result
        self.client.new_codex_surface.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'lifetime changed'):
            self.owner.launch_one(0)
        ack = json.loads((self.directory/'create-ack-0.json').read_bytes())
        self.assertEqual(ack['surface_id'], self.sid)
        self.assertEqual(self.owner._surfaces[0], self.sid)
        self.assertTrue(self.owner._failed.is_set())
        from tools.standby_preparation_inventory import inventory
        evidence = inventory(self.config, self.job['id'], self.directory)
        self.assertEqual(len(evidence['rows']), 50)
        self.assertEqual(evidence['rows'][0]['surface_id'], self.sid)
        self.assertEqual(evidence['rows'][0]['state'], 'unknown_process')
        self.assertTrue(all(r['state'] == 'unrecorded' for r in evidence['rows'][1:]))
        self.assertFalse(evidence['cleanup_proven'])
        self.pin.current.return_value = self.job['standby_generation']
        with self.assertRaises(ValueError):
            self.owner.launch_one(0)
        self.assertEqual(len(self.writes), 1)

    def test_ack_persistence_failure_never_recreates(self):
        original = prep.write_once
        def write(path, value):
            if path.name == 'create-ack-0.json':
                raise OSError('disk failure')
            return original(path, value)
        with patch.object(prep, 'write_once', side_effect=write):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.owner.launch_one(0)
        with self.assertRaises(ValueError):
            self.owner.launch_one(0)
        self.assertEqual(len(self.writes), 1)
        self.assertTrue((self.directory/'create-intent-0.json').exists())
        self.assertFalse((self.directory/'create-ack-0.json').exists())

    def test_pause_during_connection_refuses_create_and_does_not_revive(self):
        self.before_write = self.revoke
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        prep.core.ConfigStore(self.config).mutate(lambda cfg: cfg.update(global_paused=False))
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertFalse(self.writes)

    def test_job_change_observed_then_restore_does_not_revive(self):
        raw = self.jobfile.read_bytes()
        self.jobfile.write_bytes(raw + b' ')
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.jobfile.write_bytes(raw)
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertFalse(self.writes)

    def test_missing_hold_is_denied(self):
        prep.core.ConfigStore(self.config).mutate(lambda cfg:
            cfg['workspace_rules'][0].update(batch_start_holds={}))
        self.assertFalse(self.owner._authorized(0, surface_id=self.sid))
        self.assertTrue(self.owner._failed.is_set())

    def test_connected_read_revocation_is_rechecked(self):
        def read(_):
            self.revoke()
            return self.client.workspace_tree.return_value
        self.client.workspace_tree.side_effect = read
        with patch.object(prep.core, 'find_main_surface', return_value={'workspace_id': self.wid}):
            self.assertFalse(self.owner._authorized(0, surface_id=self.sid, connected=self.client))

    def test_shared_inventory_is_passed_to_actual_identity_helper(self):
        self.owner.launch_one(0)
        argv = ['/private/native']
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        claim = {**self.owner.selected, 'index': 0, 'launch_id': self.job['slots'][0]['launch_id'],
            'target_environment_sha256': self.owner.environment_sha256, 'environment_sha256': 'f' * 64,
            'surface_id': self.sid, 'argv': argv, 'bootstrap_pid': 123, 'bootstrap_birth': [4, 5]}
        path.write_text(json.dumps(claim))
        with patch.object(prep.launch, 'launch_argv', return_value=argv), \
                patch('ccc_guard_scope.birth', return_value=[4, 5]), \
                patch('ccc_guard_scope.process', return_value={'pid': 123}), \
                patch.object(self.owner, '_inventory_process', return_value={
                    self.root / 'thread-writer-locks' / 'session.lock': {}}), \
                patch.object(prep, 'StandbyRefreshBarrier') as factory:
            factory.return_value.observe.return_value = {'readiness_proven': False}
            self.assertEqual(self.owner.poll(0), {'readiness_proven': False})
            kwargs = factory.call_args.kwargs
            self.assertIs(kwargs['inspect'].keywords['rollout_absent'], self.rollouts.absent)
            self.assertIs(kwargs['inspect'].keywords['files_reader'], self.owner._files_reader)
            self.assertEqual(kwargs['claim_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.owner.poll(0)
            factory.assert_called_once()
        self.inventory.assert_called_once_with(self.sessions, refresh_coalescer=prep.FreshTopology)

    def test_close_revokes_reader_and_releases_inventory_and_pin(self):
        from ccc_standby_bootstrap import live_reader
        _, read = live_reader(self.owner.bridge.spec_path, self.owner.bridge.sha256, 0, self.config)
        self.assertEqual(read(), self.job['standby_generation'])
        self.owner.close()
        with self.assertRaises((OSError, ValueError)): read()
        self.rollouts.close.assert_called_once()
        self.pin.close.assert_called_once()

    def writer_observation(self):
        self.owner.launch_one(0)
        argv = ['/private/native']
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        path.write_text(json.dumps({**self.owner.selected, 'index': 0,
            'launch_id': self.job['slots'][0]['launch_id'],
            'target_environment_sha256': self.owner.environment_sha256,
            'environment_sha256': 'f' * 64, 'surface_id': self.sid,
            'argv': argv, 'bootstrap_pid': 123, 'bootstrap_birth': [4, 5]}))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(prep.launch, 'launch_argv', return_value=argv))
        birth = stack.enter_context(patch('ccc_guard_scope.birth', return_value=[4, 5]))
        stack.enter_context(patch('ccc_guard_scope.process', return_value={'pid': 123}))
        files = stack.enter_context(patch.object(self.owner, '_inventory_process'))
        barrier = stack.enter_context(patch.object(prep, 'StandbyRefreshBarrier'))
        return files, barrier, birth

    def test_partial_claim_waits_then_full_identity_barrier_once(self):
        files, barrier, birth = self.writer_observation()
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        complete = path.read_bytes()
        for raw in (b'', complete[:20]):
            path.write_bytes(raw)
            self.assertIsNone(self.owner.poll(0))
            self.assertEqual(path.read_bytes(), raw)
            barrier.assert_not_called()
            birth.assert_not_called()
        path.write_bytes(complete + b'\n')
        files.return_value = {self.root / 'thread-writer-locks' / 'session.lock': {}}
        barrier.return_value.observe.return_value = {'fresh': True}
        self.assertEqual(self.owner.poll(0), {'fresh': True})
        barrier.assert_called_once()
        barrier.return_value.prepare.assert_called_once()
        self.assertEqual(len(self.writes), 1)

    def test_real_exclusive_writer_visible_before_flush_waits(self):
        import ccc_native_standby as native
        files, barrier, _ = self.writer_observation()
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        claim = json.loads(path.read_bytes())
        path.unlink()  # Fixture setup only; product never removes consumed claims.
        original = native.os.fdopen
        observations = []
        def observe_before_write(fd, mode):
            handle = original(fd, mode)
            try:
                observations.append((path.read_bytes(), self.owner.poll(0)))
                barrier.assert_not_called()
            except BaseException:
                handle.close()
                raise
            return handle
        with patch.object(native.os, 'fdopen', side_effect=observe_before_write):
            native.write_once(path, claim)
        self.assertEqual(observations, [(b'', None)])
        files.return_value = {self.root / 'thread-writer-locks' / 'session.lock': {}}
        self.owner.poll(0)
        barrier.return_value.prepare.assert_called_once()
        self.assertEqual(len(self.writes), 1)

    def test_complete_corrupt_claim_remains_terminal(self):
        _, barrier, _ = self.writer_observation()
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        path.write_bytes(b'{broken\n')
        with self.assertRaises(json.JSONDecodeError):
            self.owner.poll(0)
        self.assertTrue(self.owner._failed.is_set())
        self.assertEqual(path.read_bytes(), b'{broken\n')
        barrier.assert_not_called()

    def test_partial_claim_does_not_bypass_revocation_or_replay_creation(self):
        _, barrier, _ = self.writer_observation()
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        path.write_bytes(b'')
        for _ in range(3):
            self.assertIsNone(self.owner.poll(0))
        self.revoke()
        with self.assertRaises(ValueError):
            self.owner.poll(0)
        with self.assertRaises(ValueError):
            self.owner.launch_one(0)
        self.assertEqual(path.read_bytes(), b'')
        self.assertEqual(len(self.writes), 1)
        barrier.assert_not_called()

    def test_incomplete_writer_inventory_waits_then_uses_full_barrier_once(self):
        files, barrier, _ = self.writer_observation()
        files.side_effect = [OSError('incomplete vnode descriptor'), {
            self.root / 'thread-writer-locks' / 'session.lock': {}}]
        self.assertIsNone(self.owner.poll(0))
        barrier.assert_not_called()
        self.assertFalse(self.owner._failed.is_set())
        barrier.return_value.observe.return_value = {'fresh': True}
        self.assertEqual(self.owner.poll(0), {'fresh': True})
        barrier.assert_called_once()
        barrier.return_value.prepare.assert_called_once()
        self.assertEqual(len(self.writes), 1)

    def test_persistently_unknown_writer_inventory_never_sends(self):
        files, barrier, _ = self.writer_observation()
        files.side_effect = OSError('incomplete vnode descriptor')
        for _ in range(3):
            self.assertIsNone(self.owner.poll(0))
        barrier.assert_not_called()
        self.assertEqual(len(self.writes), 1)

    def test_writer_inventory_error_with_replaced_pid_is_terminal(self):
        files, barrier, birth = self.writer_observation()
        files.side_effect = OSError('incomplete vnode descriptor')
        birth.side_effect = [[4, 5], [4, 6]]
        with self.assertRaises(ValueError) as caught:
            self.owner.poll(0)
        self.assertEqual(caught.exception.process_observation['observed_birth'], [4, 6])
        self.assertEqual(caught.exception.process_observation['stage'], 'after_writer_inventory_error')
        self.assertTrue(self.owner._failed.is_set())
        barrier.assert_not_called()

    def test_missing_birth_preserves_first_observation_and_never_sends(self):
        files, barrier, birth = self.writer_observation()
        birth.return_value = None
        with self.assertRaises(ValueError) as caught:
            self.owner.poll(0)
        evidence = caught.exception.process_observation
        self.assertEqual(evidence['expected_birth'], [4, 5])
        self.assertIsNone(evidence['observed_birth'])
        self.assertEqual(evidence['pid'], 123)
        self.assertEqual(evidence['stage'], 'before_native_writer')
        self.assertEqual(len(evidence['claim_sha256']), 64)
        birth.assert_called_once_with(123, observation={})
        files.assert_not_called()
        barrier.assert_not_called()
        self.assertTrue(self.owner._failed.is_set())
        self.assertEqual(len(self.writes), 1)

    def test_writer_inventory_error_with_pause_is_terminal(self):
        files, barrier, _ = self.writer_observation()
        def fail():
            self.revoke()
            raise OSError('incomplete vnode descriptor')
        files.side_effect = lambda *a, **k: fail()
        with self.assertRaises(ValueError):
            self.owner.poll(0)
        self.assertTrue(self.owner._failed.is_set())
        barrier.assert_not_called()

    def test_full_barrier_error_still_fails_without_replay(self):
        files, barrier, _ = self.writer_observation()
        files.return_value = {self.root / 'thread-writer-locks' / 'session.lock': {}}
        barrier.return_value.prepare.side_effect = OSError('identity evidence unavailable')
        with self.assertRaises(OSError):
            self.owner.poll(0)
        with self.assertRaises(ValueError):
            self.owner.poll(0)
        barrier.return_value.prepare.assert_called_once()

    def test_activation_observation_uses_original_barrier_without_launch_or_prepare(self):
        self.assertIsNone(self.owner.observe_for_activation(0))
        barrier = Mock()
        witness = {'index': 0, 'readiness_proven': False}
        barrier.observe_for_activation.return_value = witness
        self.owner._barriers[0] = barrier
        self.assertEqual(self.owner.observe_for_activation(0), witness)
        barrier.prepare.assert_not_called()
        self.client.new_codex_surface.assert_not_called()
        self.inventory.assert_called_once()

    def test_activation_observer_failure_permanently_revokes_original_owner(self):
        barrier = Mock()
        barrier.observe_for_activation.side_effect = ValueError('lost original')
        self.owner._barriers[0] = barrier
        with self.assertRaises(ValueError): self.owner.observe_for_activation(0)
        barrier.observe_for_activation.side_effect = None
        with self.assertRaises(ValueError): self.owner.observe_for_activation(0)
        self.assertTrue(self.owner._failed.is_set())


if __name__ == '__main__':
    unittest.main()
