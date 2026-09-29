"""Join a production target, shared source pin, route observer and live owner.

No synthetic ready toggle: proofs use the original /pwd return observation,
all fifty launch source graphs and continuous preactivation request counts.
Native still owns skill success/warnings; a refresh return is not renamed a
successful skills load. Caller construction does not start a native process.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from ccc_native_standby import COUNT, original
from ccc_standby_generation import PRODUCTION_BOUNDS, SCOPES, StandbyGeneration
from ccc_standby_sources import NativeFileSources
from ccc_standby_target import normalize, network_environment, select_provider


def _record(path, expected):
    """Read original private bytes; keep ancestor and inode identities too."""
    path = Path(path)
    parents = tuple(path.parents)
    def stamp(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def chain():
        result = []
        for parent in parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('activation evidence ancestor changed')
            result.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        return tuple(result)
    before_chain = chain()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077 or before.st_nlink != 1
                or before.st_size > 4 * 1024**2):
            raise ValueError('activation evidence is not bounded private original')
        raw = stream.read(4 * 1024**2 + 1)
        after = os.fstat(stream.fileno())
    if (raw != expected or stamp(before) != stamp(after)
            or stamp(after) != stamp(path.lstat()) or chain() != before_chain):
        raise ValueError('original activation evidence changed')
    return before_chain, stamp(after)


class CohortSources:
    """One watcher for the union; each slot retains its own argv and cwd."""
    def __init__(self, discoveries, live_target, **bounds):
        self.discoveries = tuple(discoveries)
        if len(self.discoveries) != COUNT or not callable(live_target):
            raise ValueError('all original slot source graphs required')
        before = tuple(d.discover() for d in self.discoveries)
        signatures = tuple(d.signature() for d in before)
        roots = {scope: sorted({p for graph in before for p in graph.roots[scope]})
                 for scope in SCOPES}
        self.pin = StandbyGeneration(roots, lambda: {
            'slot_source_signatures': signatures, 'target': live_target()}, **bounds)
        try:
            if tuple(d.discover().signature() for d in self.discoveries) != signatures:
                raise ValueError('native source graph changed during cohort capture')
            self.pin.current()
        except BaseException:
            self.pin.close()
            raise

    def current(self):
        return self.pin.current()

    def close(self):
        self.pin.close()


class ProductionCaller:
    """Own the original routes until explicitly closed, including continuation.

argv/provider/upstream_url are resolved from the intended native invocation
by the entry point, never taken from an unrelated bootstrap shell. The
readiness contract covers filesystem sources and the observed model route;
it does not claim an export of native in-memory settings or all skills.
"""
    def __init__(self, *, argv, provider, upstream_url, environment, runtime_files,
                 route_factory=None, source_bounds=None, system_dir=Path('/etc/codex'),
                 lifetime_guard=None):
        from ccc_standby_routes import RouteObserver
        self._lock = threading.RLock()
        if lifetime_guard is not None and not callable(lifetime_guard):
            raise ValueError('live caller lifetime callback required')
        self._lifetime_guard = lifetime_guard
        self._lifetime_failed = False
        self._closed = False
        self.owner = self.sources = None
        self._job = self._action = self._activation_raw = None
        self._captured = False
        self._runtime = tuple(Path(p) for p in runtime_files)
        self._bounds = dict(PRODUCTION_BOUNDS if source_bounds is None else source_bounds)
        self._system_dir = Path(system_dir)
        from ccc_standby_environment import template
        self.original_environment = template(environment)
        if select_provider(argv, self.original_environment, system_dir=self._system_dir) != (provider, upstream_url):
            raise ValueError('requested route differs from original profile/config/CLI')
        self.environment, proxy, ca = network_environment(self.original_environment, upstream_url)
        self.routes = (route_factory or RouteObserver)([upstream_url] * COUNT,
                                                       proxy_url=proxy, ca_file=ca)
        try:
            self.target = normalize({'argv': list(argv), 'provider': provider,
                'upstream_url': upstream_url, 'route_urls': list(self.routes.urls)})
        except BaseException:
            self.routes.close()
            raise

    def _live(self):
        def lifetime():
            if self._closed or self._lifetime_failed:
                raise ValueError('original production caller closed')
            try:
                if self._lifetime_guard is not None and self._lifetime_guard() is not True:
                    raise ValueError('original production caller lifetime revoked')
            except BaseException:
                self._lifetime_failed = True
                raise
        with self._lock:
            lifetime()
        route = self.routes.current()
        with self._lock:
            lifetime()
        return {'target': copy.deepcopy(self.target), 'environment': dict(self.environment),
                'route_observer': route}

    def capture(self, draft, *, environment):
        from ccc_standby_launch import launch_argv
        from ccc_workspace_batch import working_directory
        with self._lock:
            if self._captured or self._closed:
                raise ValueError('original source capture already consumed or closed')
            self._captured = True
        sources = None
        try:
            if draft.get('standby_target') != self.target or environment != self.environment:
                raise ValueError('admitted target differs from production caller')
            cwds = [working_directory(draft['config_path'], draft['id'], i) for i in range(COUNT)]
            def selection():
                for cwd in cwds:
                    if select_provider(self.target['argv'], self.original_environment,
                            cwd=cwd, system_dir=self._system_dir) != (
                                self.target['provider'], self.target['upstream_url']):
                        raise ValueError('original slot provider changed during capture')
            selection()
            self._live()
            graphs = [NativeFileSources(argv=launch_argv(draft['config_path'], draft, i),
                        environment=environment, cwd=cwds[i], runtime_files=self._runtime,
                        system_dir=self._system_dir) for i in range(COUNT)]
            sources = CohortSources(graphs, self._live, **self._bounds)
            selection()
            generation = sources.current()
            with self._lock:
                self._live()
                self.sources = sources
                self._job = {key: draft[key] for key in ('id', 'standby_boot_id', 'workspace_id')}
                self._job['generation'] = generation
            return sources, self.readiness
        except BaseException:
            if sources is not None:
                sources.close()
            self.close()
            raise

    def readiness(self, index, row):
        if type(index) is not int or not 0 <= index < COUNT or self.sources is None:
            raise ValueError('unknown production readiness slot')
        generation = self.sources.current()
        self._live()
        if (row.get('job_id') != self._job['id'] or row.get('index') != index
                or row.get('boot_id') != self._job['standby_boot_id']
                or row.get('workspace_id') != self._job['workspace_id']
                or row.get('generation') != generation or generation != self._job['generation']):
            raise ValueError('readiness belongs to another job or generation')
        if row.get('refresh_return_observed') is not True:
            return None
        receipt = row.get('return_receipt_sha256')
        if not isinstance(receipt, str) or not re.fullmatch(r'[a-f0-9]{64}', receipt):
            raise ValueError('original native refresh return receipt absent')
        zero = self.routes.zero(index)
        if zero['before_activation_model_requests'] != 0:
            raise ValueError('native route was used during standby')
        if self.sources.current() != generation:
            raise ValueError('readiness configuration changed')
        self._live()
        return {'readiness_proven': True, 'sources_complete': True,
            'source_scope': 'original launch filesystem union',
            'refresh_success_verified': False,
            'job_id': row['job_id'], 'generation': generation, 'boot_id': row['boot_id'],
            'original': original(row, row['workspace_id']),
            'return_receipt_sha256': receipt, 'model_request_count': 0}

    def committed(self, preparation, action_id, *, timing, authorized):
        """Called after ledger consumption, before any initial-task write."""
        self._live()
        if preparation.source_pin is not self.sources or preparation.job['id'] != self._job['id']:
            raise ValueError('route release differs from original admitted owner')
        root = preparation.jobfile.parent / 'standby'
        # Timing just persisted its UI receipt, bound to the ledger's consumed
        # activation. Compare against those in-memory original bytes; a fresh
        # self-consistent pair of files cannot become a new authorization.
        if (timing.jobfile != preparation.jobfile or timing.origin['action_id'] != action_id
                or timing._receipt_raw is None or not callable(authorized)):
            raise ValueError('original committed timing object required')
        expected = {root / (name + '.json'): raw for name, raw in timing._inputs.items()}
        expected[timing.receipt] = timing._receipt_raw
        stamps = {path: _record(path, raw) for path, raw in expected.items()}
        raw = expected[root / 'activation.json']
        record = json.loads(raw)
        attempt = json.loads(expected[root / 'activation-attempt.json'])
        from ccc_native_standby import digest
        if (record.get('action_id') != action_id or attempt.get('action_id') != action_id
                or attempt.get('activation_sha256') != digest(record)
                or record.get('generation') != self.sources.current()
                or record.get('workspace_id') != self._job['workspace_id']
                or record.get('cohort_id') != preparation.selected['cohort_id']
                or record.get('boot_id') != self._job['standby_boot_id']):
            raise ValueError('route release lacks original durable activation')
        with self._lock:
            if self._closed or self._action is not None:
                raise ValueError('route release consumed or closed')
            if authorized() is not True:
                raise ValueError('route release action no longer authorized')
            for path, content in expected.items():
                if _record(path, content) != stamps[path]:
                    raise ValueError('activation original replaced before route release')
            self.sources.current()
            self._live()
            self._action, self._activation_raw = action_id, raw
            self.routes.release(action_id)
            self._live()

    def admit(self, config_path, workspace_id, *, mode, client):
        from ccc_standby_factory import admit
        home = Path(self.environment.get('CODEX_HOME') or
                    str(Path(self.environment['HOME']) / '.codex'))
        try:
            owner = admit(config_path, workspace_id, mode=mode, client=client,
                capture_sources=self.capture, sessions_root=home / 'sessions',
                target_environment=self.environment, native_target=self.target,
                activation_committed=self.committed)
            with self._lock:
                closed = self._closed
                if not closed:
                    self.owner = owner
            if closed:
                owner.close()
                raise ValueError('production caller closed during admission')
            return owner
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self.owner is not None:
                self.owner.close()
            elif self.sources is not None:
                self.sources.close()
        finally:
            self.routes.close()
