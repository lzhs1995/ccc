"""Owner lifecycle and real local RPC only; all native operations are doubles."""
import contextlib
import concurrent.futures
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import cmux_codex_watch as core
import ccc_standby_service as service


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

    def test_lost_response_repeated_action_never_resends(self):
        self.ready()
        self.owner.activate(self.origin)
        self.finish()
        self.owner.activate(copy.deepcopy(self.origin))
        self.assertEqual(self.manager.activations, 1)
        changed = {**self.origin, 'action_id': str(uuid.uuid4())}
        with self.assertRaises(ValueError):
            self.owner.activate(changed)

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
