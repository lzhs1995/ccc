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

from ccc_native_standby import COUNT, identifier, write_once


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
        value = self.observer(index)
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
        return copy.deepcopy(value)

    def invalidate(self, reason):
        # Never hold the short status lock while waiting for ledger I/O or
        # its write guard. Activation holds _operation while waiting on jobs.
        with self._state_lock:
            self._invalid = True
            self.state, self.ready_count = 'invalidated', 0
        self.ledger._invalidate(reason)

    def _gather(self, callback):
        # Keep this operation's live caller authorization installed until all
        # submitted callbacks have finished, including an early result error
        # or a partial submission failure. No old worker may inherit a later
        # button action's authorization.
        futures = []
        try:
            for index in range(COUNT):
                futures.append(self._executor.submit(callback, index))
            return [future.result() for future in futures]
        finally:
            wait(futures)

    def refresh(self):
        with self._operation, self.operation_context():
            return self._refresh_locked()

    def _refresh_locked(self):
        """Refresh within the existing operation and caller authorization context."""
        if self.state in {'observation_only', 'activated', 'partial', 'invalidated', 'closed'}:
            return self.status()
        try:
            self._current()
            rows = self._gather(self._observe)
            self._current()
            # These independent slot checks already run on workers during
            # observation/delivery. Serial checks age out otherwise fresh
            # observations as cohort size grows. Drain all futures before
            # leaving the original caller authorization context.
            if not all(self._gather(self._authorized)):
                raise ValueError('standby workspace or original authorization changed')
            ready_count = sum(row is not None for row in rows)
            if ready_count != COUNT:
                # Incomplete initial preparation is progress. Losing a
                # previously complete cohort is permanent invalidation.
                if self.state == 'ready':
                    raise ValueError('standby readiness was lost')
                self._publish('preparing', ready_count)
                return self.status()
            self.ledger.observe_ready(rows, config_generation=self.generation_current(),
                boot_id=self.boot_current(), authorized=True)
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
                self._refresh_locked()
                self._current()
                permitted = all(self._gather(self._authorized))
                consumed = self.ledger.consume_activation(action_id=action_id, mode=mode, prompt=prompt,
                    config_generation=self.generation_current(), boot_id=self.boot_current(), authorized=permitted)
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
                        return {'index': index, 'acknowledged': False, 'error': type(exc).__name__}

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
