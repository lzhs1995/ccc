"""Live preparation owner joining original bootstraps, writers and refresh.

This owner accepts an already durably admitted standby job and a live source
pin. It never upgrades a refresh witness into ready, creates a replacement
surface after an unknown result, or reconstructs an old owner from disk.
"""
from __future__ import annotations

from functools import partial
from collections import deque
import copy
import hashlib
import json
from pathlib import Path
import threading
import time

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_launch as launch
from ccc_batch_timing import boot_id
from ccc_native_standby import identifier, write_once
from ccc_standby_bootstrap import GenerationBridge
from ccc_standby_identity import inspect_original, ObservationPending
from ccc_standby_readiness import StandbyRefreshBarrier
from ccc_standby_rollouts import RolloutInventory
from ccc_standby_inventory import ProcessInventoryReader
from ccc_standby_environment import template, signature


class LifetimeUnavailable(ValueError):
    """A live guard refused work, distinct from a read or persistence error."""


class InventoryReader:
    """Bound complete FD reads without holding capacity during UI/RPC waits.

    Every caller performs its own fresh read. FIFO admission never shares a
    snapshot; cancellation is checked again after admission and after reading.
    Connected input checks may pass background reads, with at most four such
    admissions before a waiting background read. FIFO holds within each class.
    Capacity covers only the full FD read, never a terminal or RPC wait.
    """
    def __init__(self, read, allowed, limit=8, *, priority=None):
        self.read, self.allowed = read, allowed
        self.condition = threading.Condition()
        self.waiters = deque()
        self.capacity = limit
        self.priority = priority or (lambda: False)
        self.priority_burst = 0

    def _live(self):
        if self.allowed() is not True:
            raise LifetimeUnavailable('inventory reader owner cancelled or closed')

    def _head(self):
        if not self.waiters:
            return None
        foreground = next((w for w in self.waiters if w.inventory_priority), None)
        background = next((w for w in self.waiters if not w.inventory_priority), None)
        if foreground is not None and (background is None or self.priority_burst < 4):
            return foreground
        return background

    def _wake_head(self):
        # Called under condition. Wake only the selected admission ticket.
        head = self._head()
        if head is not None and self.capacity:
            head.set()

    def __call__(self, *args, **kwargs):
        self._live()
        ticket = threading.Event()
        ticket.inventory_priority = self.priority() is True
        acquired = False
        with self.condition:
            self.waiters.append(ticket)
            self._wake_head()
        try:
            while True:
                self._live()
                with self.condition:
                    if self._head() is ticket and self.capacity:
                        self.waiters.remove(ticket)
                        self.priority_burst = self.priority_burst + 1 if ticket.inventory_priority else 0
                        self.capacity -= 1
                        acquired = True
                        self._wake_head()
                        break
                    # Clear while holding admission lock: a subsequent release
                    # cannot be lost between the predicate and the wait.
                    ticket.clear()
                ticket.wait(.05)
            self._live()
            result = self.read(*args, **kwargs)
        finally:
            with self.condition:
                if acquired:
                    self.capacity += 1
                else:
                    self.waiters.remove(ticket)
                self._wake_head()
        # The FD read is complete: later readers can use its capacity while
        # this caller checks its result. Never return before this live check.
        self._live()
        return result


class FreshTopology:
    """Coalesce pending readers, never attach a reader to an ongoing RPC.

    Each cohort is detached before its read starts. A later caller waits for
    a new read, so a post-replay check cannot inherit a pre-replay snapshot.
    There is no cache, worker thread, or retry of failed reads.
    """
    def __init__(self, read, *, collect=False):
        self.collect = collect
        self.read = read
        self.condition = threading.Condition()
        self.pending = []
        self.active = False

    def __call__(self):
        ticket = {'wake': threading.Event()}
        with self.condition:
            self.pending.append(ticket)
            if not self.active:
                ticket['wake'].set()
        while True:
            ticket['wake'].wait()
            with self.condition:
                if ticket.get('done'):
                    result = ticket
                    cohort = None
                    break
                if not self.active:
                    # An awake caller can serve every pending ticket while
                    # the signalled head is waiting to be scheduled. Detach
                    # the entire cohort only when its new read begins; this
                    # neither skips older callers nor reuses an earlier read.
                    self.active = True
                    if self.collect:
                        # Collect only before the actual read starts. The fixed
                        # 2ms window is never renewed by incoming callers.
                        # Post-start callers still require a newer snapshot.
                        deadline = time.monotonic() + .002
                        while time.monotonic() < deadline:
                            self.condition.wait(max(0.0, deadline - time.monotonic()))
                    cohort, self.pending = self.pending, []
                    break
                ticket['wake'].clear()
        if cohort is not None:
            try:
                result = {'value': self.read()}
            except BaseException as exc:
                result = {'error': exc}
            with self.condition:
                for pending in cohort:
                    pending.update(result, done=True)
                    pending['wake'].set()
                self.active = False
                # Wake the head when no caller is already running. A new
                # arrival may lead, but must include all older pending tickets.
                if self.pending:
                    self.pending[0]['wake'].set()
        # Copies stay outside admission; completed results are immutable.
        if 'error' in result:
            raise result['error']
        return copy.deepcopy(result['value'])


class PreparationOwner:
    def __init__(self, config_path, job_id, *, directory, client, source_pin,
                 sessions_root, target_environment):
        self.config_path = Path(config_path).resolve(strict=True)
        self.jobfile = batch.job_path(self.config_path, identifier(job_id))
        self._job_raw = self.jobfile.read_bytes()
        self.job = json.loads(self._job_raw)
        if self.job.get('id') != identifier(job_id):
            raise ValueError('preparation job payload differs from its requested key')
        self.selected = launch.policy(self.job, self.config_path)
        self.target_environment = template(target_environment)
        self.environment_sha256 = signature(self.target_environment)
        if self.job.get('standby_environment_sha256') != self.environment_sha256:
            raise ValueError('preparation environment differs from original job')
        self.directory = Path(directory)
        self.client, self.source_pin = client, source_pin
        # Source checks share one pin/lock across all originals. Coalesce only
        # callers queued before a new check begins, just like topology reads;
        # callers arriving during that check require the next fresh check.
        # Per-caller lifetime checks below still bracket this shared read.
        self._source_current = FreshTopology(lambda: self.source_pin.current())
        # Each read serves only callers already queued when it begins.
        # Post-source checks must join a later fresh read, never a cache.
        # Slot, boot, owner and service lifetime checks remain per caller.
        self._job_current = FreshTopology(lambda:
            not self.jobfile.is_symlink() and self.jobfile.read_bytes() == self._job_raw)
        # Only readers already waiting before load starts share its result.
        # Later/final permission checks require a new disk read; no cache.
        # Each caller still evaluates its own slot and live lifetime below.
        self._permission_config = FreshTopology(
            lambda: core.ConfigStore(self.config_path).load())
        self._topology = FreshTopology(
            lambda: self.client.workspace_tree(self.job['workspace_id']), collect=True)
        # Keep admitted readers separate from readers acquiring a connection.
        # The wave leader reads on its own thread-local admitted socket; late
        # callers require a new wave, including every post-screen check.
        self._connected_topology = FreshTopology(
            lambda: self.client.workspace_tree(self.job['workspace_id']), collect=True)
        self.sessions_root = Path(sessions_root).resolve(strict=True)
        self._closed = threading.Event()
        self._failed = threading.Event()
        self._lifetime_guard = None
        self._inventory_process = None
        self._files_reader = InventoryReader(
            lambda *args, **kwargs: self._inventory_process(
                *args, writer_identity_only=True, **kwargs),
            lambda: not self._closed.is_set() and not self._failed.is_set()
                and self._lifetime_allowed(), limit=1, priority=self._inventory_priority)
        self._operations = [threading.Lock() for _ in self.job['slots']]
        self._consumed = set()
        self._surfaces, self._barriers = {}, {}
        self.bridge = self.rollouts = None
        try:
            self._current()
            self._inventory_process = ProcessInventoryReader()
            self.rollouts = RolloutInventory(self.sessions_root, refresh_coalescer=FreshTopology)
            self.bridge = GenerationBridge(self.directory, config_path=self.config_path,
                job=self.job, current=self._current, authorized=self._authorized,
                target_environment=self.target_environment)
        except BaseException:
            self.close()
            raise

    def _inventory_priority(self):
        # Only the current thread holding an admitted input connection qualifies.
        connection = getattr(getattr(self.client, 'viewport_socket', None),
                             '_connection_local', None)
        return (isinstance(connection, threading.local)
                and callable(getattr(connection, 'read_rpc', None)))

    def _current(self):
        def lifetime(*, final=False):
            if self._closed.is_set():
                return 'owner closed'
            if self._failed.is_set():
                return 'owner previously failed'
            if not self._lifetime_allowed():
                return 'service lifetime guard refused'
            if final and boot_id() != self.selected['boot_id']:
                return 'boot identity changed'
            if not self._job_current():
                return 'original job changed'
            if not final and boot_id() != self.selected['boot_id']:
                return 'boot identity changed'
            return None
        try:
            reason = lifetime()
            if reason is None and self._source_current() != self.selected['generation']:
                reason = 'source generation changed'
            if reason is None:
                reason = lifetime(final=True)
            if reason is not None:
                raise ValueError('original preparation lifetime changed: ' + reason)
            return self.selected['generation']
        except BaseException:
            self._failed.set()
            raise

    def bind_lifetime_guard(self, check):
        """Bind the owning service before any creation; retain the callback."""
        if not callable(check) or self._lifetime_guard is not None or self._consumed:
            raise ValueError('preparation lifetime already bound or started')
        self._lifetime_guard = check
        self._current()

    def _lifetime_allowed(self):
        return self._lifetime_guard is None or self._lifetime_guard() is True

    def _authorized(self, index, *, surface_id=None, connected=None):
        try:
            return self._authorization(index, surface_id=surface_id, connected=connected)
        except BaseException:
            self._failed.set()
            raise

    def _authorization(self, index, *, surface_id=None, connected=None):
        self._current()
        if type(index) is not int or not 0 <= index < len(self.job['slots']):
            return False
        if not self._permission(index, surface_id):
            return False
        if surface_id is not None and connected is not None:
            connection = getattr(getattr(connected, 'viewport_socket', None),
                                 '_connection_local', None)
            borrowed = (isinstance(connection, threading.local)
                        and callable(getattr(connection, 'read_rpc', None)))
            # An admitted input guard already owns a connection slot. A
            # coalesced reader may be waiting for that very slot, so borrow
            # the guard's connection for a fresh read instead of joining it.
            if connected is self.client:
                tree = (self._connected_topology() if borrowed else self._topology())
            else:
                tree = connected.workspace_tree(self.job['workspace_id'])
            target = core.find_main_surface(tree, surface_id)
            if target.get('workspace_id') != self.job['workspace_id']:
                self._failed.set()
                return False
        self._current()
        # Source and connected readers may block or revoke permissions. Load
        # live permissions after they return, at the actual input boundary.
        return self._permission(index, surface_id)

    def _permission(self, index, surface_id):
        config = self._permission_config()
        if not batch.allowed(config, self.job):
            self._failed.set()
            return False
        if surface_id is not None:
            rule = core.workspace_rule_by_id(config, self.job['workspace_id'])
            hold = core.batch_start_hold(rule, surface_id)
            if (not isinstance(hold, dict) or hold.get('job_id') != self.job['id'] or hold.get('index') != index
                    or (surface_id in rule.get('excluded_surface_ids', [])
                        and rule.get('excluded_surface_reasons', {}).get(surface_id)
                            != f"batch:{self.job['id']}:initial")
                    or any(t.get('surface_id') == surface_id
                        and (t.get('paused') or not t.get('enabled', True))
                        for t in config['targets'])):
                self._failed.set()
                return False
        return (self._lifetime_allowed()
                and not (self._closed.is_set() or self._failed.is_set()))

    def launch_one(self, index):
        """Consume one creation intent, then create exactly that original slot."""
        if type(index) is not int or not 0 <= index < len(self._operations):
            raise ValueError('invalid preparation index')
        with self._operations[index]:
            if index in self._consumed:
                raise ValueError('original surface creation already consumed')
            if not self._authorized(index):
                raise ValueError('preparation launch authorization refused')
            tree = self.client.workspace_tree(self.job['workspace_id'])
            panes = [(win, pane) for win in tree.get('windows', [])
                for workspace in win.get('workspaces', [])
                if workspace.get('id') == self.job['workspace_id']
                for pane in workspace.get('panes', [])
                if pane.get('dock_scope') is None and pane.get('id')]
            if not panes:
                raise ValueError('original preparation workspace has no main pane')
            win, pane = next((p for p in panes if p[1].get('focused')), panes[0])
            command = self.bridge.command(index)
            self._consumed.add(index)
            write_once(self.directory / f'create-intent-{index}.json', {
                **self.selected, 'index': index,
                'launch_id': self.job['slots'][index]['launch_id'],
                'bootstrap_sha256': self.bridge.sha256, 'at': time.time()})
            try:
                with core.workspace_input_lock(self.config_path, self.job['workspace_id'], shared=True):
                    with self.client.input_guard(lambda: self._authorized(index)):
                        surface = self.client.new_codex_surface(win['id'], self.job['workspace_id'],
                            pane['id'], command, clean_shell=True)
                identifier(surface)
                # The create already happened. Preserve its known identity even
                # if the source/lifetime changed while the ACK was in flight.
                # Recording evidence grants no further launch authorization.
                write_once(self.directory / f'create-ack-{index}.json', {
                    'index': index, 'launch_id': self.job['slots'][index]['launch_id'],
                    'surface_id': surface, 'workspace_id': self.job['workspace_id'],
                    'at': time.time()})
                self._surfaces[index] = surface
                self._current()
                return surface
            except BaseException:
                self._failed.set()
                raise  # Keep the consumed intent even if no ACK was received.

    def _barrier(self, index):
        if index in self._barriers:
            return self._barriers[index]
        if index not in self._surfaces:
            raise ValueError('preparation has no original creation acknowledgement')
        claim_path = launch.claim_path(self.config_path, self.job['id'], index)
        if not claim_path.exists():
            return None
        raw = claim_path.read_bytes()
        try:
            claim = json.loads(raw)
        except json.JSONDecodeError:
            # Exclusive creation publishes the path before write_once finishes.
            # Its newline marks a complete publication. Keep incomplete bytes
            # consumed and pending under the caller's existing deadline; never
            # rewrite the claim or replay creation. Complete corruption fails.
            if not raw.endswith(b'\n'):
                return None
            raise
        expected = {'job_id': self.job['id'], 'index': index,
            'launch_id': self.job['slots'][index]['launch_id'],
            'surface_id': self._surfaces[index], 'workspace_id': self.job['workspace_id']}
        argv = launch.launch_argv(self.config_path, self.job, index)
        if (claim.get('argv') != argv or any(claim.get(k) != v for k, v in expected.items())
                or claim.get('target_environment_sha256') != self.environment_sha256
                or not isinstance(claim.get('environment_sha256'), str)
                or any(claim.get(k) != self.selected[k] for k in
                       ('generation', 'cohort_id', 'boot_id', 'mode'))):
            raise ValueError('preparation claim differs from original launch')
        # A claim precedes exec. Waiting for its native writer does not certify
        # identity or send input. The full helper performs all checks next.
        from ccc_guard_scope import process, birth
        def check_birth(stage):
            syscall = {}
            observed = birth(claim['bootstrap_pid'], observation=syscall)
            if observed != claim['bootstrap_birth']:
                error = ValueError('original preparation bootstrap identity unavailable or changed')
                error.process_observation = {
                    'stage': stage, 'index': index,
                    'pid': claim['bootstrap_pid'],
                    'expected_birth': claim['bootstrap_birth'],
                    'observed_birth': observed,
                    'syscall': syscall,
                    'claim_sha256': hashlib.sha256(raw).hexdigest(),
                    'surface_id': self._surfaces[index],
                    'at': time.time(),
                    'monotonic_ns': time.monotonic_ns(),
                }
                raise error
        check_birth('before_native_writer')
        native = process(claim['bootstrap_pid'], launch=True)
        if not native:
            return None
        try:
            paths = self._files_reader(claim['bootstrap_pid'], identities=True)
        except OSError:
            # Startup can close/reuse descriptors between libproc reads. This
            # is unknown evidence, not a writer witness. Retry observation on
            # a later bounded poll; never construct a barrier or send input.
            check_birth('after_writer_inventory_error')
            if not self._authorized(index):
                raise ValueError('preparation observation no longer authorized')
            return None
        if not any(p.parent == self.sessions_root.parent / 'thread-writer-locks'
                   and p.suffix == '.lock' for p in paths):
            return None
        inspect = partial(inspect_original, claim_path,
            claim_sha256=hashlib.sha256(raw).hexdigest(), expected=expected,
            expected_argv=argv, sessions_root=self.sessions_root,
            files_reader=self._files_reader,
            rollout_absent=self.rollouts.absent,
            expected_environment_sha256=self.environment_sha256)
        barrier = StandbyRefreshBarrier(self.directory, claim_path,
            claim_sha256=hashlib.sha256(raw).hexdigest(), expected=expected,
            expected_argv=argv, sessions_root=self.sessions_root, client=self.client,
            generation_current=self._current, boot_current=boot_id,
            authorized=lambda client, row: self._authorized(index,
                surface_id=row['surface_id'], connected=client), inspect=inspect)
        self._barriers[index] = barrier
        return barrier

    def poll(self, index):
        """Progress the one control and return only its fresh refresh witness."""
        if type(index) is not int or not 0 <= index < len(self._operations):
            raise ValueError('invalid preparation index')
        with self._operations[index]:
            try:
                if not self._authorized(index):
                    raise ValueError('preparation observation no longer authorized')
                barrier = self._barrier(index)
                if barrier is None:
                    return None
                with core.workspace_input_lock(self.config_path, self.job['workspace_id'], shared=True):
                    with self.client.input_guard(lambda: self._authorized(index,
                            surface_id=self._surfaces[index], connected=self.client)):
                        barrier.prepare()
                        return barrier.observe()
            except BaseException:
                self._failed.set()
                raise

    def close(self):
        self._closed.set()
        try:
            if self.bridge is not None:
                self.bridge.close()
        finally:
            try:
                if self.rollouts is not None:
                    self.rollouts.close()
            finally:
                try:
                    if self._inventory_process is not None:
                        self._inventory_process.close()
                finally:
                    self.source_pin.close()

    def observe_for_activation(self, index, *, connected_check=None, final_check=None):
        """Observe an existing return witness without preparation or input.

        This uses the original barrier and shared rollout inventory. An absent
        slot/witness remains pending; it cannot create a substitute original.
        The returned row still needs complete source/zero-request policy before
        a manager may accept it as ready.
        """
        if type(index) is not int or not 0 <= index < len(self._operations):
            raise ValueError('invalid preparation index')
        with self._operations[index]:
            try:
                self._current()
                barrier = self._barriers.get(index)
                if barrier is None:
                    return None
                if connected_check is None and final_check is None:
                    return barrier.observe_for_activation()
                return barrier.observe_for_activation(connected_check=connected_check,
                                                      final_check=final_check)
            except ObservationPending:
                # The barrier validated this temporary read failure. Only
                # the activation owner may retry its complete observation.
                raise
            except BaseException:
                self._failed.set()
                raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
