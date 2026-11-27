"""Live generation bridge for original no-prompt native bootstraps.

The preparing owner retains the source watcher. A child cannot recreate a
generation from a persisted hash after that owner exits. This module launches
only an already registered, explicitly authorized standby job; it never
creates a replacement job or certifies readiness.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
import uuid

import ccc_standby_launch as launch
from ccc_guard_scope import birth
from ccc_native_standby import generation, identifier, write_once
from ccc_standby_environment import EnvironmentFile, template, signature


LIMIT = 16384


def _identity(path, kind):
    value = Path(path).lstat()
    if (not kind(value.st_mode) or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) & 0o077):
        raise ValueError('bootstrap endpoint must be private and owned')
    return value.st_dev, value.st_ino


def _encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _read(stream):
    raw = stream.readline(LIMIT + 1)
    if not raw.endswith(b'\n') or len(raw) > LIMIT:
        raise ValueError('incomplete or oversized bootstrap message')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('bootstrap object required')
    return value


class _Server(socketserver.ThreadingUnixStreamServer):
    # All 50 original bootstraps may reach an exec guard together. The
    # socketserver default of five drops valid local connections on Darwin.
    request_queue_size = 128
    daemon_threads = True
    block_on_close = False


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            request = _read(self.rfile)
            result = self.server.owner.answer(request)
            self.wfile.write(_encode(result))
        except Exception:
            # No positive generation on callback/protocol/lifetime failure.
            with contextlib.suppress(OSError):
                self.wfile.write(_encode({'ok': False}))


class GenerationBridge:
    """Owned live endpoint used at both existing launcher exec guards.

    current must be the owner's live source pin. authorized(index) includes
    preparation lifetime and original job admission, not a stored approval.
    The launcher additionally performs its full workspace/hold authorization.
    """
    def __init__(self, directory, *, config_path, job, current, authorized, target_environment):
        if not callable(current) or not callable(authorized):
            raise ValueError('live bootstrap callbacks required')
        self.directory = Path(directory)
        if self.directory.resolve(strict=True) != self.directory:
            raise ValueError('canonical bootstrap directory required')
        self._directory = _identity(self.directory, stat.S_ISDIR)
        self.config_path = Path(config_path).resolve(strict=True)
        self.selected = launch.policy(job, self.config_path)
        environment = template(target_environment)
        if job.get('standby_environment_sha256') != signature(environment):
            raise ValueError('bootstrap target environment differs from original job')
        self.launches = [identifier(slot['launch_id']) for slot in job['slots']]
        if len(set(self.launches)) != len(self.launches):
            raise ValueError('duplicate original bootstrap launch')
        self.current, self.authorized = current, authorized
        self._closed = threading.Event()
        self._failed = threading.Event()
        self.socket_path = self.directory / 'generation.sock'
        self.spec_path = self.directory / 'bootstrap.json'
        if os.path.lexists(self.socket_path) or os.path.lexists(self.spec_path):
            raise ValueError('bootstrap lifetime cannot be resumed')
        self.spec = {'schema': 1, 'kind': 'standby_live_bootstrap',
            'config_path': str(self.config_path), **self.selected,
            'launch_ids': self.launches, 'socket_path': str(self.socket_path),
            'nonce': str(uuid.uuid4())}
        self._server = None
        self._thread = None
        try:
            self.environment = EnvironmentFile.create(self.directory / 'environment.json',
                binding=self.selected, environment=environment)
            self.spec.update(environment_path=str(self.environment.path),
                             environment_sha256=self.environment.sha256)
            self._server = _Server(str(self.socket_path), _Handler)
            self.socket_path.chmod(0o600)
            self._socket = _identity(self.socket_path, stat.S_ISSOCK)
            self.spec['socket_identity'] = list(self._socket)
            self._raw = write_once(self.spec_path, self.spec)
            self._spec_identity = _identity(self.spec_path, stat.S_ISREG)
            self.sha256 = hashlib.sha256(self._raw).hexdigest()
            self._check()
            self._server.owner = self
            self._thread = threading.Thread(target=self._server.serve_forever,
                kwargs={'poll_interval': .05}, name='ccc-standby-bootstrap', daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def _check(self):
        if self._closed.is_set() or self._failed.is_set():
            raise ValueError('bootstrap generation lifetime ended')
        try:
            def endpoint_current():
                self.environment.current()
                return (self.directory.resolve(strict=True) == self.directory
                    and _identity(self.directory, stat.S_ISDIR) == self._directory
                    and _identity(self.socket_path, stat.S_ISSOCK) == self._socket
                    and _identity(self.spec_path, stat.S_ISREG) == self._spec_identity
                    and self.spec_path.read_bytes() == self._raw)
            if (not endpoint_current()
                    or generation(self.current()) != self.selected['generation']
                    or not endpoint_current()
                    or self._closed.is_set() or self._failed.is_set()):
                raise ValueError('bootstrap generation or original endpoint changed')
        except BaseException:
            self._failed.set()
            raise

    def answer(self, request):
        self._check()
        index = request.get('index')
        if (type(index) is not int or not 0 <= index < len(self.launches)
                or request != {'nonce': self.spec['nonce'], 'request_id': request.get('request_id'),
                    'index': index, 'job_id': self.selected['job_id'],
                    'launch_id': self.launches[index], 'spec_sha256': self.sha256}):
            raise ValueError('bootstrap request differs from original launch')
        request_id = identifier(request['request_id'])
        if self.authorized(index) is not True:
            self._failed.set()
            raise ValueError('bootstrap admission revoked')
        self._check()  # Authorization may block or invalidate the source pin.
        return {'ok': True, 'request_id': request_id, 'index': index,
            'job_id': self.selected['job_id'], 'launch_id': self.launches[index],
            'spec_sha256': self.sha256, 'generation': self.selected['generation']}

    def command(self, index):
        self._check()
        if type(index) is not int or not 0 <= index < len(self.launches):
            raise ValueError('invalid original bootstrap index')
        return shlex.join([sys.executable, '-B', str(Path(__file__).resolve()),
            '--config', str(self.config_path), '--spec', str(self.spec_path),
            '--spec-sha256', self.sha256, '--index', str(index)])

    def close(self):
        self._closed.set()
        if self._server is not None:
            if self._thread is not None:
                self._server.shutdown()
                self._thread.join(timeout=3)
            self._server.server_close()
        # Keep the spec as a consumed lifetime record; remove only our socket.
        with contextlib.suppress(OSError, ValueError):
            if (self.directory.resolve(strict=True) == self.directory
                    and _identity(self.directory, stat.S_ISDIR) == self._directory
                    and _identity(self.socket_path, stat.S_ISSOCK) == getattr(self, '_socket', None)):
                self.socket_path.unlink()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def live_reader(spec_path, sha256, index, config_path):
    path = Path(spec_path)
    directory = path.parent
    if directory.resolve(strict=True) != directory:
        raise ValueError('bootstrap parent identity changed')
    parent_identity = _identity(directory, stat.S_ISDIR)
    file_identity = _identity(path, stat.S_ISREG)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('bootstrap spec hash mismatch')
    spec = json.loads(raw)
    if (spec.get('schema') != 1 or spec.get('kind') != 'standby_live_bootstrap'
            or spec['config_path'] != str(Path(config_path).resolve(strict=True))
            or type(index) is not int or not 0 <= index < len(spec['launch_ids'])
            or spec['socket_path'] != str(directory / 'generation.sock')):
        raise ValueError('bootstrap spec binding mismatch')
    failed = False

    def check():
        if (directory.resolve(strict=True) != directory
                or _identity(directory, stat.S_ISDIR) != parent_identity
                or _identity(path, stat.S_ISREG) != file_identity or path.read_bytes() != raw
                or list(_identity(Path(spec['socket_path']), stat.S_ISSOCK)) != spec['socket_identity']):
            raise ValueError('bootstrap endpoint replaced')

    def current():
        nonlocal failed
        if failed:
            raise ValueError('bootstrap reader permanently invalidated')
        try:
            check()
            request = {'nonce': spec['nonce'], 'request_id': str(uuid.uuid4()),
                'index': index, 'job_id': spec['job_id'], 'launch_id': spec['launch_ids'][index],
                'spec_sha256': sha256}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(spec['socket_path'])
                connection.sendall(_encode(request))
                with connection.makefile('rb') as stream:
                    result = _read(stream)
            check()
            expected = {k: request[k] for k in ('request_id', 'index', 'job_id', 'launch_id', 'spec_sha256')}
            if result != {'ok': True, **expected, 'generation': spec['generation']}:
                raise ValueError('live bootstrap generation refused')
            return generation(result['generation'])
        except BaseException:
            failed = True
            raise
    return spec, current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--spec-sha256', required=True)
    parser.add_argument('--index', type=int, required=True)
    args = parser.parse_args()
    spec, current = live_reader(args.spec, args.spec_sha256, args.index, args.config)
    if spec.get('environment_path') != str(args.spec.parent / 'environment.json'):
        raise ValueError('bootstrap environment path differs from original endpoint')
    selected = {k: spec[k] for k in
                ('policy', 'job_id', 'cohort_id', 'workspace_id', 'mode', 'boot_id', 'generation')}
    environment = EnvironmentFile(spec['environment_path'], spec['environment_sha256'], selected)
    directory_identity = _identity(args.spec.parent, stat.S_ISDIR)
    bootstrap_pid = os.getpid()
    bootstrap_birth = birth(bootstrap_pid)
    bootstrap_surface = os.environ.get('CMUX_SURFACE_ID')
    bootstrap_workspace = os.environ.get('CMUX_WORKSPACE_ID')
    try:
        launch.launch_registered(args.config, spec['job_id'], args.index,
            spec['launch_ids'][args.index], generation_current=current,
            environment_current=environment.current)
    except BaseException as error:
        # Successful exec never returns. Preserve the bootstrap's own failure,
        # rather than inferring its cause later from an unavailable PID.
        try:
            if _identity(args.spec.parent, stat.S_ISDIR) != directory_identity:
                raise ValueError('bootstrap diagnostic directory changed')
            write_once(args.spec.parent / f'bootstrap-failure-{args.index}.json', {
                'kind': 'bootstrap_launch_failure', **selected,
                'index': args.index, 'launch_id': spec['launch_ids'][args.index],
                'spec_sha256': args.spec_sha256, 'pid': os.getpid(),
                # Diagnostic observations, not a registration/cleanup claim.
                # Preserve both samples so unknown/drift cannot certify identity.
                'bootstrap_identity': {
                    'pid': bootstrap_pid, 'birth_before': bootstrap_birth,
                    'birth_after': birth(bootstrap_pid),
                    'surface_id': bootstrap_surface,
                    'workspace_id': bootstrap_workspace,
                    'surface_id_after': os.environ.get('CMUX_SURFACE_ID'),
                    'workspace_id_after': os.environ.get('CMUX_WORKSPACE_ID'),
                },
                'at': time.time(), 'monotonic_ns': time.monotonic_ns(),
                'error': repr(error), 'traceback': traceback.format_exc(),
            })
        except BaseException as diagnostic_error:
            # Keep the original failure and never retry the consumed launch.
            error.bootstrap_diagnostic_error = repr(diagnostic_error)
        raise


if __name__ == '__main__':
    main()
