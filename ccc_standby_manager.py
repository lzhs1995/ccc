"""Offline-tested cohort orchestration; native/UI readiness adapter still required.

This manager never invents readiness from identity-only observations, launches
replacements or retries consumed inputs. Callbacks own actual native inspection
and the connected input guard. No CLI or automatic daemon entrypoint is enabled.
"""
from __future__ import annotations

import copy
import contextlib
import os
from concurrent.futures import ThreadPoolExecutor, wait
import threading
import time

from ccc_native_standby import COUNT, ObservationExpired, identifier, original, write_once


class StandbyManager:
    def __init__(self, ledger, *, generation_current, boot_current, authorized, observe, send,
                 operation_context=contextlib.nullcontext):
        callbacks = (generation_current, boot_current, authorized, observe, send, operation_context)
        if not all(callable(callback) for callback in callbacks):
            raise ValueError('complete standby adapter callbacks required')
        self.ledger = ledger
        self.generation_current, self.boot_current = generation_current, boot_current
        self.authorized, self.observer, self.sender = authorized, observe, send
        self.operation_context = operation_context
        self._operation = threading.RLock()
        self._state_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=COUNT, thread_name_prefix='ccc-standby')
        self._closed = False
        self._refresh_wake = threading.Event()
        self._observation_costs = {}
        self._invalid = False
        self.state = 'observation_only' if ledger._consumed() else 'preparing'
        self.ready_count = 0
        self._result = None

    def _current(self):
        manifest = self.ledger.manifest
        self._alive()
        if (self.generation_current() != manifest['generation']
                or identifier(self.boot_current()) != manifest['boot_id']):
            raise ValueError('standby manager configuration/boot/lifetime changed')
        self._alive()  # callbacks may invalidate while blocked

    def _alive(self):
        with self._state_lock:
            if (self._closed or self._invalid or self.ledger._invalid
                    or os.path.lexists(self.ledger.directory / 'invalidated.json')):
                self._invalid = True
                if self.state != 'closed':
                    self.state, self.ready_count = 'invalidated', 0
                raise ValueError('standby manager permanently invalidated')

    def _publish(self, state, ready_count=0):
        with self._state_lock:
            self._alive()
            self.state, self.ready_count = state, ready_count

    def _authorized(self, index):
        self._current()
        # The real adapter must include CmuxClient's original connected
        # input_guard here, not just workspace.enabled from a cached config.
        value = self.authorized(index) is True
        # Authorization can block on connected reads. Check generation/boot
        # again on its return, including the ledger's final write admission.
        self._current()
        return value

    def _observe(self, index):
        started = time.monotonic()
        value = self.observer(index)
        elapsed = max(0.0, time.monotonic() - started)
        if value is None:
            return None
        if value.get('index') != index:
            raise ValueError('standby adapter returned a different slot')
        # inspect_original explicitly returns False. Only a separate native
        # readiness adapter may upgrade it after proving its missing premises.
        if value.get('readiness_proven') is not True:
            return None
        if not value.get('writer_lock') or not value.get('writer_identity'):
            raise ValueError('standby readiness lacks original writer identity')
        with self._state_lock:
            self._observation_costs[index] = elapsed
        return copy.deepcopy(value)

    def invalidate(self, reason):
        # Never hold the short status lock while waiting for ledger I/O or
        # its write guard. Activation holds _operation while waiting on jobs.
        with self._state_lock:
            self._invalid = True
            self.state, self.ready_count = 'invalidated', 0
        self._refresh_wake.set()
        self.ledger._invalidate(reason)

    def _gather(self, callback, indexes=range(COUNT)):
        # Keep this operation's live caller authorization installed until all
        # submitted callbacks have finished, including an early result error
        # or a partial submission failure. No old worker may inherit a later
        # button action's authorization.
        futures = []
        try:
            for index in indexes:
                futures.append(self._executor.submit(callback, index))
            return [future.result() for future in futures]
        finally:
            wait(futures)

    def _gather_aligned_observations(self, deadline):
        # Advisory scheduling only: read every original again, launching slower
        # reads first. No timestamp is changed and no old proof is reused.
        # Unlike rolling refresh, all workers finish within this caller context
        # before the full authorization pass and ledger freshness validation.
        with self._state_lock:
            costs = dict(self._observation_costs)
        target = time.monotonic() + max(costs.values(), default=0.0)

        def inspect(index):
            self._current()
            start = target - costs.get(index, 0.0)
            remaining = max(0.0, min(start, deadline) - time.monotonic())
            self._refresh_wake.wait(remaining)
            self._current()
            if time.monotonic() >= deadline:
                raise TimeoutError('standby observations unavailable for 30 seconds')
            return self._observe(index)

        return self._gather(inspect)

    def refresh(self):
        with self._operation, self.operation_context():
            return self._refresh_locked()

    def _refresh_locked(self, *, deadline=None):
        """Refresh within the existing operation and caller authorization context."""
        if self.state in {'observation_only', 'activated', 'partial', 'invalidated', 'closed'}:
            return self.status()
        if deadline is None:
            deadline = time.monotonic() + 30.0
        observation_seconds = authorization_seconds = 0.0
        observation_rounds = expired_slots = 0
        try:
            self._current()
            started = time.monotonic()
            rows = self._gather(self._observe)
            observation_seconds += time.monotonic() - started
            observation_rounds += 1
            while True:
                self._current()
                started = time.monotonic()
                permissions = self._gather(self._authorized)
                authorization_seconds += time.monotonic() - started
                if not all(permissions):
                    raise ValueError('standby workspace or original authorization changed')
                ready_count = sum(row is not None for row in rows)
                if ready_count != COUNT:
                    if self.state == 'ready' or self.ledger._originals_raw is not None:
                        raise ValueError('standby readiness was lost')
                    self._publish('preparing', ready_count)
                    return self.status()
                if time.monotonic() >= deadline:
                    raise TimeoutError('standby observations unavailable for 30 seconds; '
                        f'observation_seconds={observation_seconds:.3f}; '
                        f'authorization_seconds={authorization_seconds:.3f}; '
                        f'observation_rounds={observation_rounds}; expired_slots={expired_slots}')
                try:
                    self.ledger.observe_ready(rows, config_generation=self.generation_current(),
                        boot_id=self.boot_current(), authorized=True)
                    break
                except ObservationExpired as expired:
                    self._publish('preparing')
                    # An expired subset can alternate forever with the other
                    # slots. Reinspect the cohort with measured completion
                    # alignment; retain each original and actual timestamp.
                    # Recheck every permission after all workers have finished.
                    started = time.monotonic()
                    replacements = self._gather_aligned_observations(deadline)
                    observation_seconds += time.monotonic() - started
                    observation_rounds += 1
                    expired_slots += len(expired.indexes)
                    for index, row in enumerate(replacements):
                        before = rows[index]
                        if row is None or original(row, self.ledger.manifest['workspace_id']) != original(
                                before, self.ledger.manifest['workspace_id']):
                            raise ValueError('standby original readiness or identity changed')
                        if row['observed_monotonic'] <= before['observed_monotonic']:
                            raise ValueError('standby observation time did not advance')
                        rows[index] = row
            self._publish('ready', COUNT)
        except Exception as exc:
            self.invalidate(exc)
            raise
        return self.status()

    def activate(self, *, action_id, mode, prompt, committed=None):
        """Consume one confirmed UI action, attempt original inputs once.

        This returns delivery outcomes only. Native task_started/Hook and the
        two original UI clocks remain required for startup acceptance.
        """
        with self._operation, self.operation_context():
            if committed is not None and not callable(committed):
                raise ValueError('activation commit observer must be callable')
            if self.state in {'activated', 'partial', 'observation_only'}:
                return {'new_activation': False, **self.status()}
            if self.state != 'ready':
                raise ValueError('complete fresh standby readiness required')
            try:
                # User confirmation may arrive long after preparation. Inspect
                # the same native originals anew; never renew cached timestamps.
                # Preparation and pre-consumption reinspection share one
                # deadline. Moving to another phase cannot renew the budget.
                deadline = time.monotonic() + 30.0
                while True:
                    self._refresh_locked(deadline=deadline)
                    self._current()
                    permitted = all(self._gather(self._authorized))
                    if time.monotonic() >= deadline:
                        raise TimeoutError('standby activation observations unavailable for 30 seconds')
                    try:
                        consumed = self.ledger.consume_activation(action_id=action_id, mode=mode, prompt=prompt,
                            config_generation=self.generation_current(), boot_id=self.boot_current(), authorized=permitted)
                        break
                    except ObservationExpired:
                        self._publish('preparing')
                if not consumed:
                    self._publish('observation_only')
                    return {'new_activation': False, **self.status()}
                self._publish('activating')
                # Bind the accepted UI action to the durable activation before
                # any worker can send. Failure consumes the action permanently.
                if committed is not None:
                    committed()
                    self._current()

                def attempt(index):
                    try:
                        delivered = self.ledger.deliver(index, action_id=action_id,
                            observe=self._observe, authorized=self._authorized, send=self.sender)
                        return {'index': index, 'acknowledged': delivered, 'error': None}
                    except Exception as exc:
                        # Keep bounded causal diagnostics. An error after a
                        # write remains unknown delivery, never a retry permit.
                        chain, seen, error = [], set(), exc
                        while error is not None and id(error) not in seen and len(chain) < 4:
                            seen.add(id(error))
                            chain.append({'type': type(error).__name__, 'message': str(error)[:1024]})
                            error = error.__cause__ or error.__context__
                        return {'index': index, 'acknowledged': False, 'error': type(exc).__name__,
                                'error_chain': chain, 'error_monotonic': time.monotonic()}

                outcomes = self._gather(attempt)
                self._result = {'action_id': identifier(action_id), 'cohort_id': self.ledger.manifest['cohort_id'],
                    'workspace_id': self.ledger.manifest['workspace_id'], 'boot_id': self.ledger.manifest['boot_id'],
                    'outcomes': outcomes, 'acknowledged_inputs': sum(row['acknowledged'] for row in outcomes),
                    'native_task_acceptance_evaluated': False}
                write_once(self.ledger.directory / 'delivery-results.json', self._result)
                # Late ACKs are factual delivery outcomes, not permission to
                # revive a cancelled/closed cohort. Keep its permanent state.
                with self._state_lock:
                    if not self._invalid and not self._closed and not self.ledger._invalid:
                        self._publish('activated' if self._result['acknowledged_inputs'] == COUNT else 'partial')
                    elif self.state != 'closed':
                        self._invalid = True
                        self.state, self.ready_count = 'invalidated', 0
                return {'new_activation': True, **self.status()}
            except Exception as exc:
                self.invalidate(exc)
                raise

    def status(self):
        with self._state_lock:
            return {'state': self.state, 'ready_originals': self.ready_count,
                    'required_originals': COUNT, 'delivery': copy.deepcopy(self._result)}

    def close(self):
        # Invalidate before waiting for callbacks, so no late callback may
        # obtain a write permit while shutdown waits for a blocked observer.
        with self._state_lock:
            self._closed = True
        try:
            self.invalidate('standby manager closed')
        finally:
            with self._operation:
                try:
                    self._executor.shutdown(wait=True, cancel_futures=True)
                finally:
                    with self._state_lock:
                        self.state, self.ready_count = 'closed', 0
