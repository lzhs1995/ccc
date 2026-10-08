"""Run one explicit production caller until cancellation or its lifetime bound.

The private invocation is consumed once. Admission alone creates no native;
--prepare explicitly starts the original cohort. Activation remains the real
b/N UI action. First-task completion does not stop routes or end the run.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time

import cmux_codex_watch as core
from ccc_batch_timing import boot_id, source_hashes
from ccc_native_standby import identifier, write_once
from ccc_standby_caller import ProductionCaller, _record
from ccc_standby_environment import template

LIMIT = 512 * 1024


class Invocation:
    """An original, bounded private file; no ambient native environment."""
    def __init__(self, path, sha256):
        self.path = Path(path)
        self._failed = False
        if (not self.path.is_absolute() or self.path.parent.resolve(strict=True) != self.path.parent
                or not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256)):
            raise ValueError('canonical invocation path and original SHA256 required')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                    or info.st_size > LIMIT):
                raise ValueError('invocation must be a bounded private original file')
            self._raw = stream.read(LIMIT + 1)
        if len(self._raw) > LIMIT or hashlib.sha256(self._raw).hexdigest() != sha256:
            raise ValueError('original invocation hash mismatch')
        self._stamp = _record(self.path, self._raw)
        self.sha256 = sha256
        value = json.loads(self._raw)
        fields = {'version', 'kind', 'invocation_id', 'config_path', 'workspace_id',
                  'mode', 'argv', 'provider', 'upstream_url', 'environment',
                  'cmux_binary', 'cmux_socket', 'lifetime_seconds'}
        if (not isinstance(value, dict) or set(value) != fields
                or type(value['version']) is not int or value['version'] != 1
                or value['kind'] != 'standby_production_invocation'
                or value['mode'] not in {'b', 'N'}):
            raise ValueError('explicit standby production invocation required')
        identifier(value['invocation_id'])
        identifier(value['workspace_id'])
        for key in ('config_path', 'cmux_binary', 'cmux_socket'):
            if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
                raise ValueError('absolute invocation paths required')
        bound = value['lifetime_seconds']
        if type(bound) not in (int, float) or not math.isfinite(bound) or not 1 <= bound <= 86400:
            raise ValueError('explicit lifetime of 1 through 86400 seconds required')
        # Validation only: ProductionCaller applies its documented template.
        template(value['environment'])
        self.value = value
        self.current()

    def current(self):
        if self._failed:
            raise ValueError('original invocation permanently invalidated')
        try:
            if _record(self.path, self._raw) != self._stamp:
                raise ValueError('original invocation replaced')
            return self.sha256
        except BaseException:
            self._failed = True
            raise


def connect(value):
    """Discover the selected controller and require its actual atomic transport."""
    transport = core.CmuxViewportSocket(max_connections=64)
    client = core.CmuxClient(value['cmux_binary'], viewport_socket=transport)
    transport.configure(client.capabilities())
    if (transport.path != value['cmux_socket'] or not
            {'system.tree', 'system.top', 'surface.create', 'terminal.paste'}.issubset(
                transport.control_methods)):
        raise ValueError('selected cmux controller lacks the original atomic transport')
    return client


class Runner:
    """One process lifetime, retained after the first-task observation terminal."""
    def __init__(self, invocation, directory, *, stop=None, caller_factory=ProductionCaller,
                 client_factory=connect, clock=time.monotonic):
        self.invocation, self.directory = invocation, Path(directory)
        self.stop = stop if stop is not None else threading.Event()
        self.caller_factory, self.client_factory, self.clock = caller_factory, client_factory, clock
        self.caller = self.owner = None
        self._consumed = False
        self._deadline = None
        if self.directory.resolve(strict=True) != self.directory:
            raise ValueError('canonical runner evidence directory required')
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('runner evidence directory must be private')
        self._directory = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)

    def _check(self):
        if not self._allowed():
            raise ValueError('runner lifetime cancelled')
        self.invocation.current()
        info = self.directory.lstat()
        if (self.directory.resolve(strict=True) != self.directory
                or (info.st_dev, info.st_ino, info.st_mode, info.st_uid) != self._directory):
            raise ValueError('original runner evidence directory changed')

    def _allowed(self):
        # This reaches workers through the caller's live generation callback.
        # Never reread the full invocation or rescan source files in this guard.
        return not self.stop.is_set() and (self._deadline is None or self.clock() < self._deadline)

    def run(self, *, prepare=False, emit=None):
        if type(prepare) is not bool or self._consumed:
            raise ValueError('runner invocation already consumed or invalid prepare flag')
        self._consumed = True
        value = copy.deepcopy(self.invocation.value)
        started = self.clock()
        self._deadline = started + value['lifetime_seconds']
        reason, error = 'lifetime_expired', None
        self._check()
        intent = {'version': 1, 'kind': 'standby_runner_intent',
                  'invocation_id': value['invocation_id'], 'invocation_sha256': self.invocation.sha256,
                  'workspace_id': value['workspace_id'], 'mode': value['mode'],
                  'boot_id': boot_id(), 'pid': os.getpid(), 'prepare_requested': prepare,
                  'started_at': time.time(), 'started_monotonic': started,
                  'lifetime_seconds': value['lifetime_seconds']}
        if intent['boot_id'] is None:
            raise ValueError('runner requires the original boot identity')
        # A restart must retain and inspect this attempt, never create another cohort.
        write_once(self.directory / 'runner-intent.json', intent)
        try:
            hashes = source_hashes()
            root = Path(core.__file__).resolve().parent
            runtime = [root / name for name in hashes]
            runtime += [Path(__file__).resolve(), self.invocation.path]
            client = self.client_factory(value)
            self._check()
            self.caller = self.caller_factory(argv=value['argv'], provider=value['provider'],
                upstream_url=value['upstream_url'], environment=value['environment'],
                runtime_files=tuple(dict.fromkeys(runtime)), lifetime_guard=self._allowed)
            self._check()
            self.owner = self.caller.admit(value['config_path'], value['workspace_id'],
                                          mode=value['mode'], client=client)
            self._check()
            preparation = self.owner.service.preparation
            opened = {'version': 1, 'kind': 'standby_runner_open', **preparation.selected,
                'invocation_id': value['invocation_id'], 'invocation_sha256': self.invocation.sha256,
                'job_path': str(preparation.jobfile), 'config_path': str(preparation.config_path),
                'owner_spec_path': str(self.owner.endpoint.spec_path),
                'owner_spec_sha256': self.owner.endpoint.sha256,
                'source_hashes': hashes, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'job_terminal': False, 'run_terminal': False, 'native_creation_started': False}
            write_once(self.directory / 'runner-open.json', opened)
            if emit is not None:
                emit(opened)
            self._check()
            if prepare:
                self.owner.prepare()
            previous = None
            while self.clock() - started < value['lifetime_seconds']:
                if self.stop.is_set():
                    reason = 'cancelled'
                    break
                self._check()
                state = self.owner.status()
                if state['state'] == 'failed':
                    # Preserve the original private failure before callbacks or
                    # teardown can hide it behind a closed-endpoint exception.
                    # Do not include these diagnostics in public UI progress.
                    write_once(self.directory / 'runner-service-failure.json', {
                        'version': 1, 'kind': 'standby_runner_service_failure',
                        'invocation_id': value['invocation_id'],
                        'observed_at': time.time(), 'observed_monotonic': self.clock(),
                        'status': state})
                # Keep raw service diagnostics in the original private service;
                # public progress needs no credentials or native argv/env.
                progress = {key: state.get(key) for key in (
                    'job_id', 'cohort_id', 'workspace_id', 'mode', 'boot_id', 'generation',
                    'state', 'action_id', 'first_tasks_confirmed', 'job_terminal', 'run_terminal')}
                if progress != previous:
                    core.atomic_write_json(self.directory / 'runner-status.json', progress)
                    previous = progress
                    if emit is not None:
                        emit(progress)
                if state['state'] in {'failed', 'cancelled', 'closed'}:
                    reason = 'service_' + state['state']
                    break
                # first_tasks_observed is deliberately not a runner/run terminal.
                self.stop.wait(.1)
        except BaseException as exc:
            reason, error = 'failed', type(exc).__name__
            raise
        finally:
            close_error = None
            try:
                if self.caller is not None:
                    self.caller.close()
            except BaseException as exc:
                close_error = type(exc).__name__
                raise
            finally:
                info = self.directory.lstat()
                if (self.directory.resolve(strict=True) == self.directory
                        and (info.st_dev, info.st_ino, info.st_mode, info.st_uid) == self._directory):
                    closed = {'version': 1, 'kind': 'standby_runner_closed',
                        'invocation_id': value['invocation_id'], 'reason': reason, 'error_type': error,
                        'handles_closed': close_error is None, 'close_error_type': close_error,
                        'closed_at': time.time(), 'closed_monotonic': self.clock(),
                        'job_terminal': False, 'run_terminal': False,
                        'native_processes_terminated': False}
                    # Preserve actual communication-resource observations.
                    # close() returning alone does not prove handler exit or
                    # native completion; missing/error reports stay unknown.
                    closed['communication_resources'] = {}
                    for name, resource, method in (
                        ('routes', getattr(self.caller, 'routes', None), 'report'),
                        ('owner_endpoint', getattr(self.owner, 'endpoint', None), 'resource_report'),
                    ):
                        try:
                            report = getattr(resource, method)()
                            if not isinstance(report, dict):
                                raise ValueError('resource report unavailable')
                            closed['communication_resources'][name] = report
                        except Exception as exc:
                            closed['communication_resources'][name] = {
                                'resources_released': False, 'observation_error': type(exc).__name__}
                    closed['communication_resources_released'] = all(
                        report.get('resources_released') is True
                        for report in closed['communication_resources'].values())
                    try:
                        sources = self.caller.sources.resource_report()
                        if not isinstance(sources, dict):
                            raise ValueError('source resource report unavailable')
                    except Exception as exc:
                        sources = {'resources_released': False,
                                   'observation_error': type(exc).__name__}
                    closed['source_resources'] = sources
                    closed['all_resources_released'] = (
                        closed['communication_resources_released'] and
                        sources.get('resources_released') is True and close_error is None)
                    write_once(self.directory / 'runner-closed.json', closed)
        return 1 if reason.startswith('service_') and reason != 'service_cancelled' else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Own one explicit original native standby cohort')
    parser.add_argument('--spec', required=True)
    parser.add_argument('--spec-sha256', required=True)
    parser.add_argument('--directory', required=True, help='new private evidence directory')
    parser.add_argument('--prepare', action='store_true', help='explicitly create the original 50 natives')
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: stop.set())
        invocation = Invocation(args.spec, args.spec_sha256)
        runner = Runner(invocation, args.directory, stop=stop)
        return runner.run(prepare=args.prepare, emit=lambda row: print(json.dumps(row), flush=True))
    except Exception as exc:
        print(json.dumps({'kind': 'standby_runner_failed', 'error_type': type(exc).__name__,
                          'job_terminal': False, 'run_terminal': False}), flush=True)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
