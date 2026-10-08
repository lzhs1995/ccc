"""Owner lifecycle and real local RPC only; all native operations are doubles."""
import contextlib
import concurrent.futures
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import cmux_codex_watch as core
import ccc_standby_service as service
from ccc_standby_prepare import InventoryReader, LifetimeUnavailable


class Client:
    def __init__(self):
        self.local = threading.local()

    @contextlib.contextmanager
    def input_guard(self, check):
        self.local.guard = check
        try:
            yield
        finally:
            del self.local.guard


class Manager:
    def __init__(self, client):
        self.client = client
        self.state = 'preparing'
        self.refreshes = 0
        self.activations = 0
        self.writes = 0
        self.after_commit = lambda: None
        self.before_write = lambda: None
        self.proof = True

    def status(self):
        return {'state': self.state, 'ready_originals': 50 if self.state == 'ready' else 0}

    def refresh(self):
        self.refreshes += 1
        if self.proof:
            self.state = 'ready'
        return self.status()

    def activate(self, *, action_id, mode, prompt, committed):
        self.activations += 1
        guard = self.client.local.guard
        if not guard():
            raise ValueError('cancelled before commit')
        committed()
        self.after_commit()
        self.before_write()
        if not guard():
            raise ValueError('cancelled before write')
        self.writes += 50
        self.state = 'activated'
        return {'new_activation': True}

    def invalidate(self, reason):
        self.state = 'invalidated'


class Preparation:
    def __init__(self, root):
        self.client = Client()
        self.config_path = root / 'config.json'
        self.config_path.write_text('{}')
        self.jobfile = root / 'job.json'
        self.jobfile.write_text('{}')
        self.selected = {k: str(uuid.uuid4()) for k in ('job_id', 'cohort_id', 'workspace_id', 'boot_id')}
        self.selected.update(mode='b', generation='a' * 64, policy='native-standby-v1')
        self.job = {'initial_prompt': 'fixed original prompt'}
        self.created, self.polled = [], []
        self._files_reader = lambda *a, **kw: {}

    def bind_lifetime_guard(self, check):
        self.lifetime_guard = check

    def launch_one(self, index):
        self.created.append(index)

    def poll(self, index):
        self.polled.append(index)
        return {'witness': True}


class Activation:
    def __init__(self, root):
        self.preparation = Preparation(root)
        self.manager = Manager(self.preparation.client)
        self.closed = False

    def close(self):
        self.closed = True
        self.manager.invalidate('closed')


class Timing:
    def __init__(self):
        self.commits, self.finishes = [], []

    def committed(self):
        self.commits.append(True)

    def finish(self, *, outcome, reason):
        value = {'outcome': outcome, 'reason': reason}
        self.finishes.append(value)
        return value


class Observer:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.result = True

    def poll(self, index, *, release):
        self.calls.append((index, release))
        self.entered.set()
        return {'first_task': index} if self.result else None


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ccc-owner-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.activation = Activation(self.root)
        self.manager = self.activation.manager
        self.timing, self.observer = Timing(), Observer()
        self.owner = service.CohortService(self.activation,
            timing_factory=lambda *a: self.timing, observer_factory=lambda *a, **kw: self.observer,
            settlement_factory=lambda *a, **kw: {'synthetic_submission_settled': True},
            poll_interval=.001, observation_timeout=.1)
        self.addCleanup(self.owner.close)
        self.origin = {'action_id': str(uuid.uuid4()), 'origin': 'synthetic complete trace'}

    def ready(self):
        self.owner.prepare()
        self.owner._future.result(timeout=3)
        self.assertEqual(self.owner.status()['state'], 'ready')

    def finish(self):
        self.owner._future.result(timeout=3)
        return self.owner.status()

    def test_preparation_creates_exact_originals_once_and_witness_is_not_ready(self):
        self.ready()
        self.owner.prepare()
        self.assertEqual(sorted(self.activation.preparation.created), list(range(50)))
        self.assertEqual(sorted(self.activation.preparation.polled), list(range(50)))
        self.assertEqual(self.manager.refreshes, 1)
        self.assertEqual(self.manager.writes, 0)

    def test_pending_slot_repolled_while_another_slot_is_blocked(self):
        entered, release, repolled = threading.Event(), threading.Event(), threading.Event()
        attempts = []
        def poll(index):
            if index == 0:
                entered.set()
                if not release.wait(3): raise TimeoutError('test release absent')
            if index == 1:
                attempts.append(index)
                if len(attempts) == 1: return None
                repolled.set()
            return {'witness': True}
        self.activation.preparation.poll = poll
        self.owner.prepare()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(repolled.wait(1), 'pending slot waited for unrelated blocked slot')
        finally:
            release.set()
            self.owner._future.result(timeout=3)
        self.assertEqual(self.owner.status()['state'], 'ready')

    def test_eight_terminal_waits_do_not_block_other_original_observations(self):
        release, full, repolled = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        blocked, attempts = [], []
        def poll(index):
            if index < 8:
                with lock:
                    blocked.append(index)
                    if len(blocked) == 8:
                        full.set()
                if not release.wait(3):
                    raise TimeoutError('missing terminal release')
            if index == 49:
                attempts.append(index)
                if len(attempts) == 1:
                    return None
                repolled.set()
            return {'witness': True}
        self.activation.preparation.poll = poll
        self.owner.prepare()
        try:
            self.assertTrue(full.wait(2))
            self.assertTrue(repolled.wait(1), 'eight terminal waits consumed FD capacity')
        finally:
            release.set()
        self.owner._future.result(timeout=3)
        self.assertEqual(sorted(self.activation.preparation.created), list(range(50)))
        self.assertEqual(self.owner.status()['state'], 'ready')

    def test_cancelled_preparation_never_polls(self):
        self.owner._cancel.set()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.owner._poll_preparation(49)
        self.assertEqual(self.activation.preparation.polled, [])

    def test_route_commit_follows_durable_timing_before_any_send(self):
        sequence = []
        def commit(preparation, action, *, timing, authorized):
            self.assertEqual(timing.commits, [True])
            self.assertIs(preparation, self.activation.preparation)
            self.assertEqual(action, self.origin['action_id'])
            self.assertTrue(authorized())
            self.assertEqual(self.manager.writes, 0)
            sequence.append('route_release')
        def before_write():
            self.assertEqual(sequence, ['route_release'])
            sequence.append('write')
        self.owner.activation_committed = commit
        self.manager.before_write = before_write
        self.ready()
        self.owner.activate(self.origin)
        self.finish()
        self.assertEqual(sequence, ['route_release', 'write'])
        self.assertEqual(self.manager.writes, 50)

    def test_route_commit_failure_prevents_observation_and_send(self):
        def commit(*args, **kwargs):
            self.assertEqual(self.timing.commits, [True])
            raise ValueError('route generation changed')
        self.owner.activation_committed = commit
        self.ready()
        self.owner.activate(self.origin)
        self.assertEqual(self.finish()['state'], 'failed')
        self.assertEqual(self.manager.writes, 0)
        self.assertEqual(self.observer.calls, [])

    def test_missing_live_proof_stays_preparing(self):
        self.manager.proof = False
        seen = threading.Event()
        refresh = self.manager.refresh
        def pending():
            result = refresh()
            seen.set()
            return result
        self.manager.refresh = pending
        self.owner.prepare()
        self.assertTrue(seen.wait(2))
        with self.assertRaisesRegex(ValueError, 'not ready'):
            self.owner.activate(self.origin)
        self.owner.cancel()
        self.finish()
        self.assertEqual(self.manager.writes, 0)

    def test_prepare_failure_revokes_while_other_create_waits(self):
        entered, release = threading.Event(), threading.Event()
        def create(index):
            if index == 0:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('blocked creation not released')
            elif index == 1:
                if not entered.wait(3):
                    raise AssertionError('first creation not entered')
                raise OSError('creation failed')
        self.activation.preparation.launch_one = create
        self.owner.prepare()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.owner._cancel.wait(2))
            self.assertFalse(self.owner._allowed())
            self.assertFalse(self.owner._future.done())
        finally:
            release.set()
        self.finish()
        self.assertEqual(self.manager.refreshes, 0)
        self.assertEqual(self.owner.status()['state'], 'failed')

    def test_cancel_reaches_bound_preparation_lifetime_in_worker(self):
        writes = []
        def create(index):
            self.owner.cancel()
            if self.activation.preparation.lifetime_guard():
                writes.append(index)
        self.activation.preparation.launch_one = create
        self.owner.prepare()
        self.finish()
        self.assertEqual(writes, [])

    def test_partial_submission_failure_revokes_before_pool_join(self):
        entered, release, joining = threading.Event(), threading.Event(), threading.Event()
        def create(index):
            entered.set()
            self.assertTrue(release.wait(3))
        self.activation.preparation.launch_one = create
        class Pool:
            def __init__(inner, **kw):
                inner.pool = concurrent.futures.ThreadPoolExecutor(**kw)
                inner.count = 0
            def __enter__(inner):
                return inner
            def submit(inner, *a, **kw):
                inner.count += 1
                if inner.count == 2:
                    if not entered.wait(3):
                        raise AssertionError('first worker never started')
                    raise RuntimeError('partial submission')
                return inner.pool.submit(*a, **kw)
            def __exit__(inner, *_):
                joining.set()
                inner.pool.shutdown(wait=True)
        with patch.object(service, 'ThreadPoolExecutor', Pool):
            self.owner.prepare()
            try:
                self.assertTrue(joining.wait(3))
                self.assertFalse(self.activation.preparation.lifetime_guard())
                self.assertFalse(self.owner._future.done())
            finally:
                release.set()
            self.finish()
        self.assertEqual(self.owner.status()['state'], 'failed')

    def test_observer_runs_before_ack_and_preserves_continuation_lifetime(self):
        self.ready()
        self.manager.after_commit = lambda: self.assertTrue(self.observer.entered.wait(2))
        self.owner.activate(self.origin)
        result = self.finish()
        self.assertEqual(result['first_tasks_confirmed'], 50)
        self.assertEqual(result['state'], 'first_tasks_observed')
        self.assertEqual(result['activation_terminal']['outcome'], 'complete')
        self.assertFalse(result['job_terminal'])
        self.assertFalse(result['run_terminal'])
        self.assertFalse(self.activation.closed)
        self.assertEqual(self.manager.writes, 50)
        self.assertEqual(sorted(self.observer.calls), [(i, True) for i in range(50)])

    def test_first_task_pending_repolled_before_unrelated_slow_read_finishes(self):
        entered, release, repolled = (threading.Event() for _ in range(3))
        attempts = []
        self.owner.observation_timeout = 3
        def poll(index, *, release):
            if index == 0:
                entered.set()
                if not slow_release.wait(3):
                    raise TimeoutError('slow observer not released')
            if index == 1:
                attempts.append(index)
                if len(attempts) == 1:
                    return None
                repolled.set()
            return {'first_task': index}
        slow_release = release
        self.observer.poll = poll
        self.ready()
        self.owner.activate(self.origin)
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(repolled.wait(1), 'pending first task waited for unrelated slow read')
        finally:
            release.set()
            result = self.finish()
        self.assertEqual(result['first_tasks_confirmed'], 50)
        self.assertEqual(result['activation_terminal']['outcome'], 'complete')
        self.assertEqual(attempts, [1, 1])

    def test_first_task_pending_not_repolled_after_original_deadline(self):
        calls = []
        self.owner.observation_timeout = 1
        now = [100.0]
        def poll(index, *, release):
            calls.append(index)
            now[0] = 101.0
            return None
        self.observer.poll = poll
        # One slot makes the deadline boundary deterministic without sleeping.
        with patch.object(service, 'COUNT', 1), patch.object(service.time, 'monotonic', side_effect=lambda: now[0]):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(calls, [0])
        self.assertEqual(self.timing.finishes[-1]['outcome'], 'timeout')
        self.assertEqual(self.owner.status()['first_tasks_confirmed'], 0)

    def test_first_task_pending_cancelled_before_next_poll(self):
        calls = []
        def poll(index, *, release):
            calls.append(index)
            self.owner._cancel.set()
            return None
        self.observer.poll = poll
        with patch.object(service, 'COUNT', 1):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(calls, [0])
        self.assertEqual(self.timing.finishes[-1]['outcome'], 'cancelled')
        self.assertEqual(self.owner.status()['first_tasks_confirmed'], 0)

    def _cancel_inventory_observation(self, *, queued=False, read_error=False):
        entered, release = threading.Event(), threading.Event()
        reads = []
        self.owner.observation_timeout = 5
        def read(index):
            reads.append(index)
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test inventory not released')
            if read_error:
                raise OSError('original inventory failed during cancellation')
            return {'first_task': index}
        reader = InventoryReader(read, self.owner._allowed, limit=1)
        self.observer.poll = lambda index, **kw: reader(index)
        with patch.object(service, 'COUNT', 2 if queued else 1), patch.object(
                self.manager, 'invalidate', wraps=self.manager.invalidate) as invalidations:
            worker = threading.Thread(target=self.owner._observe, args=(self.observer, self.timing))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                if queued:
                    deadline = time.monotonic() + 2
                    while True:
                        with reader.condition:
                            if len(reader.waiters) == 1:
                                break
                        if time.monotonic() >= deadline:
                            self.fail('second observation never queued')
                        time.sleep(.001)
                self.owner.cancel()
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(reads), 1)
            self.assertEqual(reader.capacity, 1)
            self.assertEqual(len(reader.waiters), 0)
            state = self.owner.status()
            self.assertEqual(state['state'], 'cancelled')
            self.assertEqual(state['first_tasks_confirmed'], 0)
            self.assertEqual(self.manager.writes, 0)
            if read_error:
                self.assertIsNone(state['activation_terminal'])
                self.assertIn('OSError: original inventory failed', state['observation_error'])
                self.assertEqual(self.timing.finishes, [])
                self.assertEqual(invalidations.call_count, 2)
            else:
                self.assertIsNone(state['observation_error'])
                self.assertIsNone(state['error'])
                self.assertEqual(state['activation_terminal']['outcome'], 'cancelled')
                self.assertEqual(len(self.timing.finishes), 1)
                self.assertEqual(invalidations.call_count, 1)

    def test_cancel_during_inventory_read_finishes_cancelled(self):
        self._cancel_inventory_observation()

    def test_cancel_queued_inventory_read_finishes_cancelled(self):
        self._cancel_inventory_observation(queued=True)

    def test_real_read_error_survives_cancelled_inventory_waiter(self):
        self._cancel_inventory_observation(queued=True, read_error=True)

    def test_cancel_between_loop_and_live_check_finishes_cancelled(self):
        require_live = self.owner._require_live
        def cancel_then_check():
            self.owner.cancel()
            require_live()
        with patch.object(service, 'COUNT', 1), patch.object(
                self.owner, '_require_live', side_effect=cancel_then_check):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(self.observer.calls, [])
        self.assertEqual(self.timing.finishes[-1]['outcome'], 'cancelled')
        self.assertEqual(len(self.timing.finishes), 1)
        self.assertIsNone(self.owner.status()['observation_error'])

    def test_reader_refusal_with_live_service_remains_failure(self):
        reader = InventoryReader(lambda: self.fail('refused read ran'), lambda: False)
        self.observer.poll = lambda *a, **kw: reader()
        with patch.object(service, 'COUNT', 1):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(self.owner.status()['state'], 'failed')
        self.assertIn('LifetimeUnavailable', self.owner.status()['observation_error'])
        self.assertEqual(self.timing.finishes, [])

    def test_cancel_terminal_write_failure_remains_failure(self):
        def poll(*args, **kwargs):
            self.owner.cancel()
            raise LifetimeUnavailable('inventory reader owner cancelled or closed')
        self.observer.poll = poll
        with patch.object(service, 'COUNT', 1), patch.object(
                self.timing, 'finish', side_effect=OSError('cancel terminal disk full')) as finish:
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(finish.call_args.kwargs['outcome'], 'cancelled')
        self.assertIsNone(self.owner.status()['activation_terminal'])
        self.assertIn('OSError: cancel terminal disk full', self.owner.status()['observation_error'])

    def test_first_task_error_revokes_before_unrelated_read_finishes(self):
        entered, release = threading.Event(), threading.Event()
        self.owner.observation_timeout = 3
        def poll(index, *, release):
            if index == 0:
                entered.set()
                if not slow_release.wait(3):
                    raise TimeoutError('slow observer not released')
                return None
            if not entered.wait(3):
                raise TimeoutError('slow observer not entered')
            raise OSError('first-task evidence changed')
        slow_release = release
        self.observer.poll = poll
        with patch.object(service, 'COUNT', 2):
            worker = threading.Thread(target=self.owner._observe, args=(self.observer, self.timing))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.owner._cancel.wait(1))
                self.assertTrue(worker.is_alive())
                self.assertFalse(self.owner._allowed())
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.owner.status()['state'], 'failed')
        self.assertEqual(self.owner.status()['first_tasks_confirmed'], 0)
        self.assertEqual(self.timing.finishes, [])

    def test_lost_response_repeated_action_never_resends(self):
        self.ready()
        self.owner.activate(self.origin)
        self.finish()
        self.owner.activate(copy.deepcopy(self.origin))
        self.assertEqual(self.manager.activations, 1)
        changed = {**self.origin, 'action_id': str(uuid.uuid4())}
        with self.assertRaises(ValueError):
            self.owner.activate(changed)

    def test_settlement_follows_workers_and_observer_without_closing_owner(self):
        calls = []
        def settle(timing, *, authorized):
            self.assertEqual(self.manager.writes, 50)
            self.assertFalse(self.owner._observation_thread.is_alive())
            self.assertEqual(len(self.observer.calls), 50)
            self.assertTrue(authorized())
            calls.append(True)
            return {'settled': True}
        self.owner.settlement_factory = settle
        self.ready()
        self.owner.activate(self.origin)
        result = self.finish()
        self.assertEqual(calls, [True])
        self.assertEqual(result['submission_settlement'], {'settled': True})
        self.assertFalse(self.activation.closed)
        with self.assertRaises(RuntimeError):
            with core.FileLock(self.root / 'worker.lock', timeout_sec=0):
                pass

    def test_settlement_failure_never_publishes_admission_permission(self):
        self.owner.settlement_factory = lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full'))
        self.ready()
        self.owner.activate(self.origin)
        result = self.finish()
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['submission_settlement'])
        self.assertFalse(result['run_terminal'])

    def test_trace_mutation_same_action_cannot_replace_original(self):
        self.ready()
        self.owner.activate(self.origin)
        self.finish()
        with self.assertRaises(ValueError):
            self.owner.activate({**self.origin, 'origin': 'replacement'})

    def test_runtime_guard_revocation_prevents_write(self):
        self.ready()
        allowed = [True]
        self.manager.before_write = lambda: allowed.__setitem__(0, False)
        self.owner.activate(self.origin, action_guard=lambda: allowed[0])
        self.finish()
        self.assertEqual(self.manager.writes, 0)
        self.assertEqual(self.owner.status()['state'], 'failed')

    def test_cancel_during_ack_does_not_wait_for_operation_or_revive(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            self.assertTrue(release.wait(2))
        self.manager.after_commit = blocked
        self.owner.activate(self.origin)
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(self.owner.cancel()['state'], 'cancelled')
        finally:
            release.set()
        self.finish()
        self.assertEqual(self.manager.writes, 0)
        self.assertEqual(self.owner.status()['state'], 'cancelled')

    def test_commit_write_error_sends_nothing_and_never_constructs_observer(self):
        self.ready()
        self.timing.committed = lambda: (_ for _ in ()).throw(OSError('disk full'))
        self.owner.observer_factory = lambda *a, **k: self.fail('observer before persisted commit')
        self.owner.activate(self.origin)
        self.finish()
        self.assertEqual(self.manager.writes, 0)
        with self.assertRaises(ValueError):
            self.owner.activate(self.origin)

    def test_ack_without_tasks_times_out_not_complete(self):
        self.ready()
        self.observer.result = False
        self.owner.activate(self.origin)
        result = self.finish()
        self.assertEqual(self.manager.writes, 50)
        self.assertEqual(result['first_tasks_confirmed'], 0)
        self.assertEqual(result['activation_terminal']['outcome'], 'timeout')
        self.assertEqual(result['state'], 'observation_incomplete')

    def test_observer_persistence_error_never_reports_complete(self):
        self.ready()
        self.timing.finish = lambda **kw: (_ for _ in ()).throw(OSError('disk full'))
        self.owner.activate(self.origin)
        result = self.finish()
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['activation_terminal'])
        self.assertIn('OSError', result['observation_error'])

    def test_worker_lock_rejects_second_owner(self):
        with self.assertRaises(RuntimeError):
            service.CohortService(self.activation)

    def test_close_persistence_failure_still_releases_worker_lock(self):
        def fail():
            raise OSError('disk full')
        self.activation.close = fail
        with self.assertRaises(OSError):
            self.owner.close()
        with core.FileLock(self.root / 'worker.lock', timeout_sec=0):
            pass

    def test_observer_uses_same_preparation_inventory_admission(self):
        received = []
        def factory(*args, **kwargs):
            received.append(kwargs['files_reader'])
            return self.observer
        self.owner.observer_factory = factory
        self.ready()
        self.owner.activate(self.origin)
        self.finish()
        self.assertEqual(received, [self.activation.preparation._files_reader])
        self.assertEqual(self.manager.writes, 50)

    def test_first_task_return_at_or_after_deadline_not_confirmed(self):
        for returned_at in (101.0, 101.1):
            with self.subTest(returned_at=returned_at):
                now = [100.0]
                calls = []
                self.owner.observation_timeout = 1
                def poll(index, *, release):
                    calls.append(index)
                    now[0] = returned_at
                    return {'first_task': index}
                self.observer.poll = poll
                with patch.object(service, 'COUNT', 1), patch.object(
                        service.time, 'monotonic', side_effect=lambda: now[0]):
                    self.owner._observe(self.observer, self.timing)
                self.assertEqual(calls, [0])
                self.assertEqual(self.timing.finishes[-1]['outcome'], 'timeout')
                self.assertEqual(self.owner.status()['first_tasks_confirmed'], 0)
                self.assertEqual(self.manager.writes, 0)

    def test_first_task_return_before_deadline_confirmed(self):
        now = [100.0]
        self.owner.observation_timeout = 1
        def poll(index, *, release):
            now[0] = 100.5
            return {'first_task': index}
        self.observer.poll = poll
        with patch.object(service, 'COUNT', 1), patch.object(
                service.time, 'monotonic', side_effect=lambda: now[0]):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(self.timing.finishes[-1]['outcome'], 'complete')
        self.assertEqual(self.owner.status()['first_tasks_confirmed'], 1)

    def test_first_task_return_after_lifetime_revocation_not_confirmed(self):
        calls = []
        def poll(index, *, release):
            calls.append(index)
            self.owner._cancel.set()
            return {'first_task': index}
        self.observer.poll = poll
        with patch.object(service, 'COUNT', 1):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(calls, [0])
        self.assertEqual(self.timing.finishes[-1]['outcome'], 'cancelled')
        self.assertEqual(self.owner.status()['first_tasks_confirmed'], 0)
        self.assertEqual(self.manager.writes, 0)

    def test_first_task_collection_after_revocation_not_confirmed(self):
        def completed(futures):
            for future in concurrent.futures.as_completed(futures):
                self.assertIsNotNone(future.result())
                self.owner._cancel.set()
                yield future
        with patch.object(service, 'COUNT', 1), patch.object(
                service, 'as_completed', side_effect=completed):
            self.owner._observe(self.observer, self.timing)
        self.assertEqual(self.timing.finishes[-1]['outcome'], 'cancelled')
        self.assertEqual(self.owner.status()['first_tasks_confirmed'], 0)
        self.assertEqual(self.manager.writes, 0)

    def test_create_admission_bounded_but_all_fifty_originals_prepared(self):
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, active, peak = [], 0, 0
        def create(index):
            nonlocal active, peak
            with lock:
                calls.append(index)
                active += 1
                peak = max(peak, active)
                if active == 4:
                    entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError('creation not released')
            finally:
                with lock:
                    active -= 1
        self.activation.preparation.launch_one = create
        self.owner.prepare()
        try:
            self.assertTrue(entered.wait(2))
            with lock:
                self.assertEqual(len(calls), 4)
                self.assertEqual(peak, 4)
        finally:
            release.set()
        self.finish()
        self.assertEqual(sorted(calls), list(range(50)))
        self.assertEqual(sorted(self.activation.preparation.polled), list(range(50)))
        self.assertEqual(peak, 4)
        self.assertEqual(self.owner.status()['state'], 'ready')


    def test_cancel_does_not_launch_queued_creation(self):
        entered, release = threading.Event(), threading.Event()
        calls, lock = [], threading.Lock()
        def create(index):
            with lock:
                calls.append(index)
                if len(calls) == 4:
                    entered.set()
            if not release.wait(3):
                raise AssertionError('creation not released')
        self.activation.preparation.launch_one = create
        self.owner.prepare()
        try:
            self.assertTrue(entered.wait(2))
            self.owner.cancel()
        finally:
            release.set()
        self.finish()
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.activation.preparation.polled, [])
        self.assertEqual(self.manager.writes, 0)




class EndpointTests(unittest.TestCase):
    setUp = ServiceTests.setUp
    ready = ServiceTests.ready
    finish = ServiceTests.finish

    def endpoint(self):
        directory = self.root / 'endpoint'
        directory.mkdir(mode=0o700)
        endpoint = service.ServiceEndpoint(self.owner, directory)
        self.addCleanup(endpoint.close)
        return endpoint

    def test_endpoint_guard_queues_file_reads_behind_live_read(self):
        endpoint = self.endpoint()
        entered, release, overtook = (threading.Event() for _ in range(3))
        original = Path.read_bytes
        calls = []
        lock = threading.Lock()
        def read(path):
            if path == endpoint.spec_path:
                with lock:
                    calls.append(threading.get_ident())
                    first = len(calls) == 1
                if first:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('endpoint guard release missing')
                else:
                    overtook.set()
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                first = pool.submit(endpoint._authorized)
                self.assertTrue(entered.wait(3))
                later = [pool.submit(endpoint._authorized) for _ in range(3)]
                try:
                    self.assertFalse(overtook.wait(.1),
                        'concurrent guards duplicated an in-flight filesystem read')
                finally:
                    release.set()
                self.assertTrue(first.result(timeout=3))
                self.assertTrue(all(f.result(timeout=3) for f in later))
            self.assertEqual(len(calls), 2)
            self.assertTrue(endpoint._authorized())
            self.assertEqual(len(calls), 3)  # A later boundary requires a fresh read.

    def test_endpoint_close_after_shared_read_refuses_authorization(self):
        endpoint = self.endpoint()
        def read_then_close():
            endpoint._check_files()
            endpoint._closed.set()
        with patch.object(endpoint, '_file_checks', side_effect=read_then_close):
            with self.assertRaisesRegex(ValueError, 'endpoint changed'):
                endpoint._authorized()

    def test_endpoint_late_guard_rechecks_change_during_previous_read(self):
        endpoint = self.endpoint()
        entered, release = threading.Event(), threading.Event()
        read = endpoint._check_files
        calls = []
        def checked():
            read()
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('endpoint mutation release missing')
        with patch.object(endpoint, '_check_files', side_effect=checked):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(endpoint._authorized)
                self.assertTrue(entered.wait(3))
                later = pool.submit(endpoint._authorized)
                try:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with endpoint._file_checks.condition:
                            waiting = len(endpoint._file_checks.pending)
                        if waiting:
                            break
                        time.sleep(.001)
                    self.assertEqual(waiting, 1)
                    endpoint.spec_path.write_bytes(endpoint.spec_path.read_bytes() + b' ')
                finally:
                    release.set()
                with contextlib.suppress(ValueError):
                    first.result(timeout=3)
                with self.assertRaisesRegex(ValueError, 'endpoint changed'):
                    later.result(timeout=3)
        self.assertTrue(endpoint._closed.is_set())

    def test_resource_report_empty_endpoint_close(self):
        endpoint = self.endpoint()
        self.assertFalse(endpoint.resource_report()['resources_released'])
        endpoint.close()
        report = endpoint.resource_report()
        self.assertTrue(report['resources_released'])
        self.assertFalse(report['socket_path_present'])
        self.assertEqual(report['worker_threads_alive'], 0)

    def test_listener_close_does_not_claim_inflight_handler_completed(self):
        endpoint = self.endpoint()
        entered, release = threading.Event(), threading.Event()
        def answer(_):
            entered.set()
            release.wait(3)
            return {'ok': False}
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        self.addCleanup(release.set)
        client.settimeout(2)
        with patch.object(endpoint, 'answer', side_effect=answer):
            client.connect(str(endpoint.socket_path))
            client.sendall(b'{}\n')
            self.assertTrue(entered.wait(2))
            endpoint.close()
            report = endpoint.resource_report()
            self.assertTrue(report['listener_closed'])
            self.assertFalse(report['resources_released'])
            self.assertEqual(report['worker_threads_alive'], 1)
            self.assertEqual(report['connections'], 1)
            release.set()
            deadline = time.monotonic() + 2
            while not endpoint.resource_report()['resources_released'] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(endpoint.resource_report()['resources_released'])

    def test_real_socket_status_does_not_start_preparation(self):
        endpoint = self.endpoint()
        result = service.request(endpoint.spec_path, endpoint.sha256, 'status')
        self.assertEqual(result['state'], 'admitted')
        self.assertEqual(self.activation.preparation.created, [])
        self.assertEqual(endpoint.socket_path.stat().st_mode & 0o777, 0o600)

    def test_real_socket_activation_and_status_bind_original_action(self):
        self.ready()
        endpoint = self.endpoint()
        first = service.request(endpoint.spec_path, endpoint.sha256, 'activate', origin=self.origin)
        self.assertEqual(first['action_id'], self.origin['action_id'])
        self.finish()
        result = service.request(endpoint.spec_path, endpoint.sha256, 'status')
        self.assertEqual(result['first_tasks_confirmed'], 50)
        service.request(endpoint.spec_path, endpoint.sha256, 'activate', origin=self.origin)
        self.assertEqual(self.manager.activations, 1)

    def test_endpoint_close_reaches_live_action_guard(self):
        self.ready()
        endpoint = self.endpoint()
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            self.assertTrue(release.wait(2))
        self.manager.before_write = blocked
        service.request(endpoint.spec_path, endpoint.sha256, 'activate', origin=self.origin)
        self.assertTrue(entered.wait(2))
        endpoint.close()
        release.set()
        self.finish()
        self.assertEqual(self.manager.writes, 0)

    def test_wrong_descriptor_and_unknown_operation_never_activate(self):
        endpoint = self.endpoint()
        with self.assertRaises(ValueError):
            service.request(endpoint.spec_path, 'f' * 64, 'activate', origin=self.origin)
        with self.assertRaises(ValueError):
            service.request(endpoint.spec_path, endpoint.sha256, 'prepare')
        self.assertEqual(self.manager.activations, 0)
        self.assertEqual(self.activation.preparation.created, [])

    def test_oversized_request_is_rejected_before_dispatch(self):
        endpoint = self.endpoint()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(str(endpoint.socket_path))
            conn.sendall(b'x' * (service.LIMIT + 1))
            with conn.makefile('rb') as stream:
                self.assertFalse(json.loads(stream.readline())['ok'])
        self.assertEqual(self.manager.activations, 0)

    def test_descriptor_change_permanently_rejects_old_endpoint(self):
        endpoint = self.endpoint()
        original = endpoint.spec_path.read_bytes()
        endpoint.spec_path.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            endpoint._check()
        endpoint.spec_path.write_bytes(original)
        with self.assertRaises(ValueError):
            endpoint._check()

    def test_missing_endpoint_or_bad_permissions_permanently_rejects_after_restore(self):
        for kind in ('spec', 'socket', 'directory_mode', 'spec_mode'):
            with self.subTest(kind=kind):
                directory = self.root / kind
                directory.mkdir(mode=0o700)
                ep = service.ServiceEndpoint(self.owner, directory)
                self.addCleanup(ep.close)
                if kind in {'spec', 'socket'}:
                    path = ep.spec_path if kind == 'spec' else ep.socket_path
                    other = path.with_suffix('.saved')
                    path.rename(other)
                    restore = lambda: other.rename(path)
                else:
                    path = ep.directory if kind == 'directory_mode' else ep.spec_path
                    old_mode = path.stat().st_mode & 0o777
                    path.chmod(0o777)
                    restore = lambda: path.chmod(old_mode)
                try:
                    with self.assertRaises((OSError, ValueError)):
                        ep._check()
                finally:
                    restore()
                with self.assertRaises(ValueError):
                    ep._check()


if __name__ == '__main__':
    unittest.main()
