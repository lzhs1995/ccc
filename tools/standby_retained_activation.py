"""Advance ten retained original cohorts through preparation and real UI.

The caller supplies a RetainedOwners pool and repeatedly calls step. This never
creates replacement handles or closes owners: recovery, performance, shared
liveness and scoped native cleanup remain the enclosing harness's responsibility.
"""
import copy
import math
import time
from pathlib import Path

from ccc_native_standby import COUNT, write_once
from tools.standby_run_manifest import directory_identity


class NativeSetup:
    """Bind each cohort's original proxy/provider before native and UI writes.

    Resources must already belong to the enclosing harness, which retains their
    lifetimes and cleanup handles. Each batch has its own fifty-session provider
    and forwarding gate, including successive batches in the same workspace.
    """
    def __init__(self, pool, resources, gate_factory):
        from tools.standby_original_transcript import OriginalTranscript
        from ccc_workspace_batch import PROMPT
        if set(resources) != set(pool.order) or not callable(gate_factory):
            raise ValueError('exact declared native resource set required')
        proxies, providers = set(), set()
        for key, item in resources.items():
            proxy, provider = item['proxy'], item['provider']
            invocation = pool.specs[key][0]
            invocation.current()
            if (id(proxy) in proxies or id(provider) in providers
                    or proxy.workspace != pool.expected[key]['workspace_id']
                    or proxy.gate is not None
                    or provider.url != invocation.value['upstream_url']
                    or provider.prompt != PROMPT):
                raise ValueError('unique original unbound proxy/provider required')
            proxies.add(id(proxy))
            providers.add(id(provider))
        self.pool = pool
        self.resources = {key: dict(value) for key, value in resources.items()}
        self.gate_factory = gate_factory
        self.transcript_factory = OriginalTranscript
        self.gates, self.resolvers, self.witnesses = {}, {}, {}
        self.attempts = set()
        self.original_sessions, self.original_processes, self.original_surfaces = set(), set(), set()

    def _consume(self, key, stage, handle):
        self.pool._health()
        if self.pool.handles.get(key) is not handle or (key, stage) in self.attempts:
            raise ValueError('native setup requires an unconsumed original handle')
        self.pool.specs[key][0].current()
        from ccc_workspace_batch import PROMPT
        provider = self.resources[key]['provider']
        if (provider.url != self.pool.specs[key][0].value['upstream_url']
                or provider.prompt != PROMPT):
            raise ValueError('native provider binding changed')
        self.attempts.add((key, stage))
        return handle['runner'].owner.service.preparation

    def before_prepare(self, key, handle):
        preparation = self._consume(key, 'prepare', handle)
        proxy = self.resources[key]['proxy']
        gate = self.gate_factory([preparation.bridge.command(i) for i in range(COUNT)])
        self.gates[key] = gate
        self.pool._health()
        with proxy.lock:
            if (proxy.gate is not None
                    or proxy.workspace != self.pool.expected[key]['workspace_id']):
                raise ValueError('native forwarding target changed before preparation')
            proxy.gate = gate

    def before_ui(self, key, handle):
        from ccc_standby_launch import claim_path
        from ccc_workspace_batch import PROMPT
        preparation = self._consume(key, 'ui', handle)
        proxy, provider = (self.resources[key][name] for name in ('proxy', 'provider'))
        gate = self.gates[key]
        invocation = self.pool.specs[key][0]
        config = invocation.value['config_path']
        job = preparation.job['id']
        witnesses, resolvers = {}, {}
        sessions, processes, surfaces = set(), set(), set()
        for index in range(COUNT):
            row = preparation.observe_for_activation(index)
            if (not isinstance(row, dict) or row.get('index') != index
                    or row.get('job_id') != job
                    or row.get('workspace_id') != self.pool.expected[key]['workspace_id']):
                raise ValueError('original activation witness missing or changed')
            identity = (row['pid'], tuple(row['birth']))
            if (row['session_id'] in sessions or identity in processes
                    or row['surface_id'] in surfaces
                    or row['session_id'] in self.original_sessions
                    or identity in self.original_processes
                    or row['surface_id'] in self.original_surfaces):
                raise ValueError('duplicate native original in cohort')
            sessions.add(row['session_id'])
            processes.add(identity)
            surfaces.add(row['surface_id'])
            witnesses[index] = copy.deepcopy(row)
            resolvers[index] = self.transcript_factory(row, claim_path(config, job, index))
        # Retain partial binding outcomes. A provider failure cannot authorize a
        # new resolver set or an attempt to replay earlier successful bindings.
        self.witnesses[key], self.resolvers[key] = witnesses, resolvers
        self.original_sessions.update(sessions)
        self.original_processes.update(processes)
        self.original_surfaces.update(surfaces)
        for index, resolver in resolvers.items():
            provider.bind(witnesses[index]['session_id'], resolver)
        self.pool._health()
        invocation.current()
        with proxy.lock:
            if (proxy.gate is not gate
                    or proxy.workspace != self.pool.expected[key]['workspace_id']):
                raise ValueError('native forwarding target changed before activation')
            gate.bind_activation(PROMPT, resolvers)


class ActivationRun:
    def __init__(self, pool, directory, *, seconds=600, idle_seconds=3,
                 clock=time.monotonic, before_prepare=None, before_ui=None):
        for callback in (before_prepare, before_ui):
            if callback is not None and not callable(callback):
                raise ValueError('setup callbacks must be callable')
        for value in (seconds, idle_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('positive finite observation bounds required')
        if seconds > 86400 or idle_seconds < 3 or idle_seconds >= seconds:
            raise ValueError('bounded run and at least three seconds idle required')
        pool._health()
        if pool.handles or getattr(pool, 'activation_run', None) is not None:
            raise ValueError('activation run requires the original unstarted pool')
        self.pool, self.directory, self.clock = pool, Path(directory), clock
        self.identity = directory_identity(self.directory)
        self.deadline = clock() + seconds
        self.idle_seconds = idle_seconds
        self.phases = {key: 'declared' for key in pool.order}
        self.idle = {}
        self.route_observations = {}
        self.failure = None
        self.sequence = 0
        self.callbacks = {'prepare': before_prepare, 'ui': before_ui}
        self.setup_attempts = set()
        # Retain this driver even if recording intent fails. A second directory
        # must not become a way to replace a partly constructed original run.
        pool.activation_run = self
        try:
            write_once(self.directory/'activation-run-intent.json', dict(
                kind='retained_activation_run', batches=pool.order,
                seconds=seconds, idle_seconds=idle_seconds, full_500_acceptance=False))
        except BaseException as exc:
            self.failure = type(exc).__name__
            raise

    def _check(self):
        if self.clock() >= self.deadline:
            raise TimeoutError('original activation observation deadline expired; retain handles')
        if directory_identity(self.directory) != self.identity:
            raise ValueError('original activation evidence directory changed')
        self.pool._health()

    def _record(self, key, phase, **fields):
        self._check()
        if self.sequence >= 10000:
            raise ValueError('activation evidence observation budget exhausted')
        self.sequence += 1
        write_once(self.directory/f'observation-{self.sequence:06d}.json', dict(
            batch_id=key, phase=phase, monotonic=self.clock(), **fields))

    def _predecessors_settled(self, key):
        workspace = self.pool.expected[key]['workspace_id']
        return all(self.phases[old] == 'settled'
                   for old in self.pool.order[:self.pool.order.index(key)]
                   if self.pool.expected[old]['workspace_id'] == workspace)

    def _setup(self, key, stage):
        """Consume before calling; an uncertain setup is never repeated."""
        token = (key, stage)
        if token in self.setup_attempts:
            raise ValueError('setup already attempted; retain original handles')
        self.setup_attempts.add(token)
        self._record(key, 'setup_attempt', stage=stage)
        callback = self.callbacks[stage]
        if callback is not None:
            callback(key, self.pool.handles[key])
        self._check()
        self._record(key, 'setup_complete', stage=stage)

    def _ready(self, handle):
        owner = handle['runner'].owner
        manager = owner.service.manager.refresh()
        routes = handle['runner'].caller.routes
        zeros = [routes.zero(i) for i in range(COUNT)]
        def unused(value):
            return (isinstance(value, dict)
                    and type(value.get('before_activation_model_requests')) is int
                    and value['before_activation_model_requests'] == 0
                    and type(value.get('requests')) is int and value['requests'] == 0
                    and 'action_id' in value and value['action_id'] is None
                    and type(value.get('observed_since_monotonic_ns')) is int
                    and value['observed_since_monotonic_ns'] > 0)
        if (owner.status()['state'] != 'ready' or manager['state'] != 'ready'
                or manager.get('ready_originals') != COUNT
                or manager.get('required_originals') != COUNT
                or not all(unused(value) for value in zeros)
                or len({value['observed_since_monotonic_ns'] for value in zeros}) != 1):
            raise ValueError('original cohort lost readiness or zero-request proof')
        stamp = zeros[0]['observed_since_monotonic_ns']
        previous = self.route_observations.get(id(handle))
        if (handle['runner'].caller.routes is not routes
                or (previous is not None and (
                    previous[0] is not handle or previous[1] is not routes
                    or previous[2] != stamp))):
            raise ValueError('original route observation changed during idle')
        # Retain the objects, not just their ids: a fresh observer must not
        # reset the evidence supporting the same cohort's idle interval.
        self.route_observations[id(handle)] = (handle, routes, stamp)
        return manager, zeros

    def _advance(self, key):
        phase = self.phases[key]
        if phase == 'declared':
            if self._predecessors_settled(key):
                self.pool.start(key)
                self.phases[key] = 'opening'
            return
        if phase == 'opening':
            if self.pool.await_open(key, timeout=0) is not None:
                self._setup(key, 'prepare')
                self.pool.prepare(key)
                self.phases[key] = 'preparing'
            return
        if phase == 'settled':
            return
        handle = self.pool.handles[key]
        owner = handle['runner'].owner
        state = owner.status()
        if state['state'] in {'failed', 'cancelled', 'closed'}:
            raise ValueError('original cohort failed: ' + key)
        if phase == 'preparing':
            if state['state'] != 'ready':
                if key in self.idle:
                    raise ValueError('original cohort lost readiness during idle')
                if state['state'] not in {'admitted', 'preparing'}:
                    raise ValueError('unexpected activation before bound UI')
                return
            # Actual owner refresh verifies original readiness; routes are
            # monotonic request counters owned by the same retained caller.
            manager, zeros = self._ready(handle)
            self._record(key, 'idle_ready', manager=manager, zero_requests=zeros)
            now = self.clock()
            since = self.idle.setdefault(key, now)
            if now - since >= self.idle_seconds:
                self._setup(key, 'ui')
                # Transcript binding can take time or expose changed state.
                # Recheck readiness/counters and deadline before UI creation.
                self._ready(handle)
                self._check()
                self.pool.begin_activation(key, self.directory/('ui-'+key))
                self.phases[key] = 'ui'
            return
        if phase == 'ui':
            value = self.pool.poll_activation(key, timeout=0)
            if value == 'confirmation_written':
                self.phases[key] = 'awaiting_settlement'
            return
        if phase == 'awaiting_settlement':
            if state['state'] == 'first_tasks_observed':
                binding = self.pool.settle(key)
                self._record(key, 'settled', binding=binding)
                self.phases[key] = 'settled'
            return
        raise ValueError('unknown original activation phase')

    def step(self):
        """One non-sleeping pass; timeout is not authority to restart or clean up.

        Synchronous original RPCs may take time. The deadline is rechecked after
        every batch, so a late return never constitutes successful observation.
        """
        if self.failure is not None:
            raise ValueError('activation failed or outcome unknown; retain handles: '+self.failure)
        try:
            self._check()
            for key in self.pool.order:
                self._advance(key)
                self._check()
            return dict(phases=copy.deepcopy(self.phases),
                        all_settled=all(p == 'settled' for p in self.phases.values()),
                        full_500_acceptance=False, run_terminal=False)
        except BaseException as exc:
            self.failure = type(exc).__name__
            raise
