"""Live cohort lifetime and UI transport for an already admitted original job.

The caller supplies the actual ActivationOwner (and its live readiness reader).
This service cannot reconstruct ready from disk, create replacement originals,
or turn an activation observation terminal into a continuation/run terminal.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
import time
import uuid

import cmux_codex_watch as core
from ccc_native_standby import COUNT, identifier, write_once
from ccc_standby_acceptance import FirstTaskObserver
from ccc_standby_timing import ActivationTiming
from ccc_standby_bootstrap import _identity, _encode

LIMIT = 256 * 1024


def _read(stream):
    raw = stream.readline(LIMIT + 1)
    if len(raw) > LIMIT or not raw.endswith(b'\n'):
        raise ValueError('incomplete or oversized standby service message')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('standby service message must be an object')
    return value


class CohortService:
    """Retain the same owner from preparation through first-task observation.

    `prepare` is explicit. `activate` accepts one complete UI origin and returns
    immediately after queueing it. Polling status never sends or resumes work.
    No method stops native processes or declares the continuation job complete.
    """
    def __init__(self, activation, *, observation_timeout=30.0, poll_interval=.02,
                 timing_factory=ActivationTiming, observer_factory=FirstTaskObserver,
                 activation_committed=None):
        if activation_committed is not None and not callable(activation_committed):
            raise ValueError('live activation commit callback required')
        self.activation_committed = activation_committed
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (observation_timeout, poll_interval)):
            raise ValueError('positive finite service observation bounds required')
        self.activation = activation
        self.preparation, self.manager = activation.preparation, activation.manager
        self.selected = copy.deepcopy(self.preparation.selected)
        self.timing_factory, self.observer_factory = timing_factory, observer_factory
        self.observation_timeout, self.poll_interval = observation_timeout, poll_interval
        self._lock = threading.RLock()
        self._stop, self._cancel = threading.Event(), threading.Event()
        self._started = False
        self._origin = self._action = self._action_guard = None
        self._future = None
        self._observation_thread = None
        self._error = self._observation_error = None
        self._confirmed = set()
        self._terminal = None
        self._phase = 'admitted'
        self._runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ccc-standby-owner')
        self._worker_lock = core.FileLock(self.preparation.jobfile.parent / 'worker.lock', timeout_sec=0)
        try:
            self._worker_lock.__enter__()
        except BaseException:
            self._runner.shutdown(wait=True)
            raise
        try:
            self.preparation.bind_lifetime_guard(self._allowed)
        except BaseException:
            self._stop.set()
            self._runner.shutdown(wait=True)
            self._worker_lock.__exit__(None, None, None)
            raise

    def _allowed(self):
        if self._stop.is_set() or self._cancel.is_set():
            return False
        guard = self._action_guard
        allowed = guard is None or guard() is True
        return allowed and not (self._stop.is_set() or self._cancel.is_set())

    def _require_live(self):
        if not self._allowed():
            raise ValueError('standby service action cancelled or closed')

    def status(self):
        with self._lock:
            return {**self.selected, 'state': self._phase,
                'manager': self.manager.status(), 'action_id': self._action,
                'first_tasks_confirmed': len(self._confirmed),
                'activation_terminal': copy.deepcopy(self._terminal),
                'error': self._error, 'observation_error': self._observation_error,
                'job_terminal': False, 'run_terminal': False}

    def prepare(self):
        with self._lock:
            self._require_live()
            if not self._started:
                if self._action is not None:
                    raise ValueError('activation already accepted')
                self._started = True
                self._phase = 'preparing'
                self._future = self._runner.submit(self._prepare)
            return self.status()

    def _prepare(self):
        try:
            with ThreadPoolExecutor(max_workers=COUNT, thread_name_prefix='ccc-standby-prepare') as pool:
                def create(index):
                    self._require_live()
                    return self.preparation.launch_one(index)
                futures = []
                try:
                    for i in range(COUNT):
                        futures.append(pool.submit(create, i))
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    self._cancel.set()
                    for future in futures:
                        future.cancel()
                    raise
                pending = set(range(COUNT))
                while pending:
                    self._require_live()
                    def poll(index):
                        self._require_live()
                        return self.preparation.poll(index)
                    futures = {}
                    try:
                        for i in pending:
                            futures[pool.submit(poll, i)] = i
                        for future in as_completed(futures):
                            if future.result() is not None:
                                pending.remove(futures[future])
                    except BaseException:
                        self._cancel.set()
                        for future in futures:
                            future.cancel()
                        raise
                    if pending:
                        self._stop.wait(self.poll_interval)
            # A refresh witness alone is insufficient; the actual owner's
            # proof reader decides whether each original can enter ready.
            while True:
                self._require_live()
                with self.preparation.client.input_guard(self._allowed):
                    state = self.manager.refresh()
                if state['state'] == 'ready':
                    with self._lock:
                        self._require_live()
                        self._phase = 'ready'
                    return
                if state['state'] != 'preparing':
                    raise ValueError('preparation lost its original manager')
                self._stop.wait(self.poll_interval)
        except Exception as exc:
            self._fail(exc)

    def activate(self, origin, *, action_guard=None):
        if action_guard is not None and not callable(action_guard):
            raise ValueError('live action callback required')
        origin = copy.deepcopy(origin)
        action = identifier(origin['action_id'])
        with self._lock:
            self._require_live()
            if self._action is not None:
                if self._origin != origin:
                    raise ValueError('original activation action cannot be replaced')
                return self.status()  # Lost RPC reply cannot replay the input.
            if self._phase != 'ready' or self.manager.status()['state'] != 'ready':
                raise ValueError('original cohort is not ready')
            timing = self.timing_factory(self.preparation.config_path, self.selected['job_id'], origin)
            self._action, self._origin, self._action_guard = action, origin, action_guard
            self._phase = 'activation_queued'
            try:
                self._require_live()
                self._future = self._runner.submit(self._activate, timing)
            except BaseException:
                self._cancel.set()
                self._phase = 'failed'
                raise
            return self.status()

    def _activate(self, timing):
        try:
            def committed():
                self._require_live()
                timing.committed()
                if self.activation_committed is not None:
                    self.activation_committed(self.preparation, self._action,
                                              timing=timing, authorized=self._allowed)
                    self._require_live()
                observer = self.observer_factory(self.preparation.config_path,
                    self.selected['job_id'], self._action, client=self.preparation.client)
                self._require_live()
                # Construct only after durable consumption. Start observing
                # before send workers and independently of their ACK waits.
                with self._lock:
                    self._phase = 'activating'
                    self._observation_thread = threading.Thread(target=self._observe,
                        args=(observer, timing), name='ccc-standby-first-tasks', daemon=True)
                    self._observation_thread.start()

            with self.preparation.client.input_guard(self._allowed):
                self.manager.activate(action_id=self._action, mode=self.selected['mode'],
                    prompt=self.preparation.job['initial_prompt'], committed=committed)
            if self._observation_thread is not None:
                self._observation_thread.join()
            with self._lock:
                if self._phase not in {'failed', 'cancelled', 'closed'}:
                    self._phase = ('first_tasks_observed' if self._terminal
                        and self._terminal['outcome'] == 'complete' else 'observation_incomplete')
        except Exception as exc:
            self._fail(exc)

    def _observe(self, observer, timing):
        deadline = time.monotonic() + self.observation_timeout
        try:
            pending = set(range(COUNT))
            with ThreadPoolExecutor(max_workers=COUNT, thread_name_prefix='ccc-first-task') as pool:
                while pending and self._allowed() and time.monotonic() < deadline:
                    def poll(index):
                        self._require_live()
                        return observer.poll(index, release=True)
                    futures = {}
                    try:
                        for i in pending:
                            futures[pool.submit(poll, i)] = i
                        for future in as_completed(futures):
                            if future.result() is not None:
                                index = futures[future]
                                pending.remove(index)
                                with self._lock:
                                    self._confirmed.add(index)
                    except BaseException:
                        self._cancel.set()
                        for future in futures:
                            future.cancel()
                        raise
                    finally:
                        wait(futures)
                    if pending:
                        self._stop.wait(self.poll_interval)
            if not self._allowed():
                outcome, reason = 'cancelled', 'original action cancelled or owner closed'
            elif pending:
                outcome, reason = 'timeout', 'first-task observation deadline elapsed'
            else:
                outcome, reason = 'complete', None
            terminal = timing.finish(outcome=outcome, reason=reason)
            with self._lock:
                self._terminal = terminal
        except Exception as exc:
            with self._lock:
                self._observation_error = type(exc).__name__ + ': ' + str(exc)
            self._fail(exc)

    def _fail(self, exc):
        self._cancel.set()
        with self._lock:
            self._error = type(exc).__name__ + ': ' + str(exc)
            if self._phase not in {'closed', 'cancelled'}:
                self._phase = 'failed'
        try:
            self.manager.invalidate(exc)
        except Exception as persistence_error:
            with self._lock:
                self._error += '; invalidation: ' + type(persistence_error).__name__

    def cancel(self):
        # The shared action event reaches worker authorization immediately;
        # do not wait for the operation thread or ACKs to revoke it.
        self._cancel.set()
        with self._lock:
            if self._phase != 'closed':
                self._phase = 'cancelled'
        self.manager.invalidate('original standby service action cancelled')
        return self.status()

    def close(self):
        self._stop.set()
        self._cancel.set()
        with self._lock:
            if self._phase == 'closed':
                return
            self._phase = 'closed'
        try:
            self.activation.close()
        finally:
            try:
                self._runner.shutdown(wait=True, cancel_futures=True)
                if self._observation_thread is not None:
                    self._observation_thread.join()
            finally:
                self._worker_lock.__exit__(None, None, None)


class _Server(socketserver.ThreadingUnixStreamServer):
    request_queue_size = 128
    daemon_threads = True
    block_on_close = False


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            response = self.server.endpoint.answer(_read(self.rfile))
        except Exception as exc:
            response = {'ok': False, 'error': type(exc).__name__}
        with contextlib.suppress(OSError):
            self.wfile.write(_encode(response))


class ServiceEndpoint:
    """Private local UI endpoint; descriptor is not a readiness certificate."""
    def __init__(self, service, directory, *, binding_path=None):
        self.service = service
        self.directory = Path(directory)
        if self.directory.resolve(strict=True) != self.directory:
            raise ValueError('canonical service directory required')
        self._directory = _identity(self.directory, stat.S_ISDIR)
        self.socket_path, self.spec_path = self.directory / 'owner.sock', self.directory / 'owner.json'
        if len(os.fsencode(self.socket_path)) >= 104:
            raise ValueError('standby owner requires a short private socket directory')
        self.binding_path = Path(binding_path) if binding_path is not None else None
        self._binding_raw = None
        if self.binding_path is not None:
            if self.binding_path.parent.resolve(strict=True) != self.binding_path.parent:
                raise ValueError('canonical original owner binding directory required')
            self._binding_directory = _identity(self.binding_path.parent, stat.S_ISDIR)
            if os.path.lexists(self.binding_path):
                raise ValueError('original owner binding already consumed')
        if os.path.lexists(self.socket_path) or os.path.lexists(self.spec_path):
            raise ValueError('standby service lifetime cannot be recreated')
        self._closed = threading.Event()
        self._server = self._thread = None
        try:
            self._server = _Server(str(self.socket_path), _Handler)
            self.socket_path.chmod(0o600)
            self._socket = _identity(self.socket_path, stat.S_ISSOCK)
            self.spec = {'version': 1, 'kind': 'standby_live_owner',
                **service.selected, 'socket_path': str(self.socket_path),
                'socket_identity': list(self._socket), 'nonce': str(uuid.uuid4())}
            self._raw = write_once(self.spec_path, self.spec)
            self._spec_identity = _identity(self.spec_path, stat.S_ISREG)
            self.sha256 = hashlib.sha256(self._raw).hexdigest()
            if self.binding_path is not None:
                self._binding_raw = write_once(self.binding_path, {
                    'version': 1, 'kind': 'standby_owner_binding', **service.selected,
                    'spec_path': str(self.spec_path), 'spec_sha256': self.sha256})
                self._binding_identity = _identity(self.binding_path, stat.S_ISREG)
            self._server.endpoint = self
            self._thread = threading.Thread(target=self._server.serve_forever,
                kwargs={'poll_interval': .05}, name='ccc-standby-ui', daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def _check(self):
        try:
            if (self._closed.is_set() or self.directory.resolve(strict=True) != self.directory
                    or _identity(self.directory, stat.S_ISDIR) != self._directory
                    or _identity(self.spec_path, stat.S_ISREG) != self._spec_identity
                    or self.spec_path.read_bytes() != self._raw
                    or _identity(self.socket_path, stat.S_ISSOCK) != self._socket):
                raise ValueError('original standby service endpoint changed')
            if self.binding_path is not None and (
                    self.binding_path.parent.resolve(strict=True) != self.binding_path.parent
                    or _identity(self.binding_path.parent, stat.S_ISDIR) != self._binding_directory
                    or _identity(self.binding_path, stat.S_ISREG) != self._binding_identity
                    or self.binding_path.read_bytes() != self._binding_raw):
                raise ValueError('original job owner binding changed')
        except BaseException:
            self._closed.set()
            raise

    def answer(self, request):
        self._check()
        operation = request.get('operation')
        request_id = identifier(request.get('request_id'))
        expected = {'operation', 'request_id', 'nonce', 'spec_sha256', 'job_id'}
        if operation == 'activate':
            expected.add('origin')
        if (set(request) != expected or request['nonce'] != self.spec['nonce']
                or request['spec_sha256'] != self.sha256
                or request['job_id'] != self.spec['job_id']):
            raise ValueError('UI request differs from original service')
        if operation == 'status':
            result = self.service.status()
        elif operation == 'activate':
            result = self.service.activate(request['origin'], action_guard=self._authorized)
        elif operation == 'cancel':
            result = self.service.cancel()
        else:
            raise ValueError('unknown standby UI operation')
        self._check()
        return {'ok': True, 'request_id': request_id, 'spec_sha256': self.sha256,
                'job_id': self.spec['job_id'], 'result': result}

    def _authorized(self):
        self._check()
        return True

    def close(self):
        self._closed.set()
        if self._server is not None:
            if self._thread is not None:
                self._server.shutdown()
                self._thread.join()
            self._server.server_close()
        with contextlib.suppress(OSError, ValueError):
            if (self.directory.resolve(strict=True) == self.directory
                    and _identity(self.directory, stat.S_ISDIR) == self._directory
                    and _identity(self.socket_path, stat.S_ISSOCK) == getattr(self, '_socket', None)):
                self.socket_path.unlink()


def request(spec_path, spec_sha256, operation, *, origin=None):
    """One RPC attempt. A lost activation reply is recovered by status only."""
    path = Path(spec_path)
    if path.parent.resolve(strict=True) != path.parent:
        raise ValueError('canonical owner descriptor required')
    parent = _identity(path.parent, stat.S_ISDIR)
    identity = _identity(path, stat.S_ISREG)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec_sha256:
        raise ValueError('original owner descriptor hash changed')
    spec = json.loads(raw)
    if (spec.get('version') != 1 or spec.get('kind') != 'standby_live_owner'
            or spec.get('socket_path') != str(path.parent / 'owner.sock')):
        raise ValueError('invalid original owner descriptor')
    def check():
        if (path.parent.resolve(strict=True) != path.parent
                or _identity(path.parent, stat.S_ISDIR) != parent
                or _identity(path, stat.S_ISREG) != identity or path.read_bytes() != raw
                or list(_identity(Path(spec['socket_path']), stat.S_ISSOCK)) != spec['socket_identity']):
            raise ValueError('original owner endpoint changed')
    check()
    payload = {'request_id': str(uuid.uuid4()), 'operation': operation,
        'nonce': spec['nonce'], 'spec_sha256': spec_sha256, 'job_id': spec['job_id']}
    if origin is not None:
        payload['origin'] = origin
    wire = _encode(payload)
    if len(wire) > LIMIT:
        raise ValueError('oversized standby UI origin')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(spec['socket_path'])
        check()
        connection.sendall(wire)
        with connection.makefile('rb') as stream:
            result = _read(stream)
    check()
    if (result.get('ok') is not True or set(result) != {'ok', 'request_id', 'spec_sha256', 'job_id', 'result'}
            or any(result.get(k) != payload[k] for k in ('request_id', 'spec_sha256', 'job_id'))):
        raise ValueError('standby UI response missing or mismatched; query status, do not recreate')
    return result['result']


def main(argv=None):
    parser = argparse.ArgumentParser(description='Contact the original live standby owner')
    parser.add_argument('operation', choices=('status', 'activate', 'cancel'))
    parser.add_argument('--spec', required=True)
    parser.add_argument('--spec-sha256', required=True)
    parser.add_argument('--origin', help='original accepted UI trace, required for activate')
    args = parser.parse_args(argv)
    if (args.operation == 'activate') != bool(args.origin):
        parser.error('--origin is required only for activate')
    origin = None
    if args.origin:
        from ccc_standby_timing import _read as read_evidence
        origin, _ = read_evidence(Path(args.origin))
    print(json.dumps(request(args.spec, args.spec_sha256, args.operation, origin=origin)))


if __name__ == '__main__':
    main()
