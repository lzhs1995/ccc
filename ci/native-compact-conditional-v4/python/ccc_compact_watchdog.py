"""Scoped recovery by one occurrence-bound native conditional compact request.

The existing TUI can receive its dedicated bound command through one protected
submission. Native admission and write-once receipts decide the outcome. An
uncertain request is retained without replay, restart or Goal edits.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from ccc_compact_sensor import (LOG_DB, RolloutFacts, compaction_start, may_interrupt, native_compaction_start,
                                unsettled_compaction)
from ccc_compact_inputs import PendingInputs, clear_guard
from ccc_compact_intents import IntentIndex
from ccc_compact_goals import GoalNotifications, observing_rpc
from ccc_compact_support import Deadline, Journal, OwnedDirectory, owned_file, write_once, sync_directory
from ccc_compact_runtime import (CONFIG, ControlLockBusy, binding, goal_identity, libraries,
                                 metadata, control_request, control_locks, conditional_submit)
from ccc_compact_trace import intent_trace, new_request, request_bytes
from ccc_compact_native_client import (client_snapshot, write_authority, read_receipt, pinned_file)
from ccc_compact_join_accounting import goal_after_join
from ccc_compact_handoff import (handoff_ready, handoff_record, revoke_reasons, settle_record,
                                 settle_rejects)

CMUX = '/Applications/cmux.app/Contents/Resources/bin/cmux'
WARN = 180
LIMIT = 300
MAX_RETRIES = 2
AUTH_REFRESH_SECONDS = 30
LOCK_RETRY_SECONDS = 3
INITIALIZATION_RETRY_SECONDS = 15


def atomic_json(path, value):
    with OwnedDirectory(path.parent) as directory:
        directory.atomic_json(path.name, value)


def paused(config, target):
    from ccc_compact_guard import _current_config_clear, _uuid
    try:
        _current_config_clear(config, target, _uuid(target['surface_id']))
        return config.get('sessions', {}).get(target['session_id'], {}).get('paused', False) is not False
    except (ValueError, KeyError, TypeError, AttributeError):
        return True


def make_episode(snapshot, *, root=None, attempt=0, submitted_at=None):
    compact, facts = snapshot['compact'], snapshot['facts']
    started = submitted_at if submitted_at is not None else compact['started_at']
    key = hashlib.sha256(compact['key'].encode()).hexdigest()[:24]
    return dict(key=key, root=root or key, attempt=attempt, phase='compacting',
                turn_id=compact['turn_id'], started_at=started,
                compact_key=compact['key'], native_started_at=compact['started_at'],
                path=facts['path'], file_id=facts['file_id'],
                compacted_offset=facts['compacted_offset'], user_offset=facts['user_offset'],
                input_token=snapshot['input_guard']['token'], goal=snapshot['goal'],
                goal_full=snapshot.get('goal_full'),
                goal_revision=snapshot.get('goal_revision'),
                compaction_occurrence=compact.get('compaction_occurrence'),
                client_pid=snapshot.get('client_pid'), client_epoch=snapshot.get('client_epoch'),
                compact_source=compact.get('source'),
                compact_candidates=compact.get('candidates'),
                manual_present=compact.get('manual_present'),
                blocked_goal_revision=snapshot.get('blocked_goal_revision'))


def settled(episode, snapshot):
    facts = snapshot['facts']
    return (facts['path'] == episode['path'] and facts['file_id'] == episode['file_id']
            and facts['compacted_offset'] > episode['compacted_offset']
            and facts['compacted_at'] >= episode['started_at']
            and facts['replacement_items'] > 0)


class Worker:
    def __init__(self, target, directory, lib, stop, *, apply=False, intent_index=None,
                 config_path=CONFIG, db_path=LOG_DB, enrollment_path=None):
        # Enrollment and prior operation bindings are immutable evidence. Only
        # this worker's authenticated, in-memory process may be adopted.
        self.target, self.lib, self.stop, self.apply = copy.deepcopy(target), lib, stop, apply
        self.enrolled_process = copy.deepcopy(target['process'])
        self.config_path, self.db_path = Path(config_path), Path(db_path)
        self.enrollment_path = Path(enrollment_path) if enrollment_path is not None else None
        self.directory = directory / target['session_id']
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.journal_directory = OwnedDirectory(self.directory)
        self.journal = Journal(self.directory / 'events.jsonl', directory=self.journal_directory)
        self.state_path = self.directory / 'state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            'episode': None, 'closed': [], 'progress_after': None}
        self.facts = RolloutFacts(target['path'])
        self.inputs = PendingInputs(self.directory / 'pending-inputs.json',
                                    target['session_id'], self.facts, self.db_path,
                                    intent_index=intent_index, allow_authenticated_rebind=True)
        self.lock = threading.RLock()
        self.timer = None
        self.last_error = None
        self.authenticated_at = 0.0
        self.authenticated = False
        self.compact_cache = None
        self.goal_notices = GoalNotifications(self.directory / 'native-goal-notices.jsonl',
                                             target['session_id'])
        self.observer_rpc = None

    def observation_rpc(self, current_binding):
        # tick(), recover() and close() serialize all reads on this connection.
        # Keeping it open lets notifications accumulate between snapshots.
        if self.observer_rpc is None:
            self.observer_rpc = observing_rpc(self.lib[4], self.goal_notices)(
                binding=dict(current_binding, session_id=self.target['session_id']), timeout=2)
        return self.observer_rpc

    def close_observer(self):
        rpc, self.observer_rpc = self.observer_rpc, None
        if rpc is not None:
            rpc.close()

    def close(self):
        # Stop the timer before acquiring its control lock; joining it under
        # that lock could deadlock against a deadline already firing.
        if self.timer:
            self.timer.close()
        with self.lock:
            self.close_observer()
            self.journal_directory.close()

    def save(self):
        self.state.update({key: self.target.get(key) for key in
                           ('session_id', 'surface_id', 'workspace_id', 'pane_id')})
        self.journal_directory.atomic_json(self.state_path.name, self.state)

    def adopt_process(self, observed, facts):
        """Reauthenticate the same native session, without resetting any state."""
        if any(observed['identity'].get(k) != self.target['identity'].get(k)
               for k in ('workspace_uuid', 'target_surface_uuid', 'kind', 'dock')):
            raise RuntimeError('original terminal identity changed; no process adoption')
        if (observed['process'] == self.target['process']
                and observed['identity'] == self.target['identity']
                and self.state.get('authenticated_binding') == observed):
            return
        record = dict(session_id=self.target['session_id'], identity=observed['identity'],
                      path=facts['path'], file_id=facts['file_id'], at=time.time(),
                      previous_identity=copy.deepcopy(self.target['identity']),
                      previous=copy.deepcopy(self.target['process']),
                      current=copy.deepcopy(observed['process']))
        updated = copy.deepcopy(self.state)
        updated.setdefault('process_adoptions', []).append(record)
        updated['authenticated_binding'] = copy.deepcopy(observed)
        # If persistence fails, keep the old process and deny control. Neither
        # the enrollment nor any existing interrupt/compact intent is rewritten.
        self.journal.record('PROCESS_ADOPTION_VERIFIED', **record)
        self.journal_directory.atomic_json(self.state_path.name, updated)
        self.state = updated
        self.target['process'] = copy.deepcopy(observed['process'])
        self.target['identity'] = copy.deepcopy(observed['identity'])
        self.target['pane_id'] = observed['identity']['target_pane_uuid']

    def original_process_known(self, process):
        if process == self.target['process'] or process == getattr(self, 'enrolled_process', None):
            return True
        return any(record.get('session_id') == self.target['session_id']
                   and all((record.get('identity') or {}).get(k) == self.target['identity'].get(k)
                           for k in ('workspace_uuid', 'target_surface_uuid'))
                   and record.get('path') == self.target.get('path')
                   and process in (record.get('previous'), record.get('current'))
                   for record in self.state.get('process_adoptions', []))

    def snapshot(self, *, authenticate=True):
        bridge, native, _, _, RPC = self.lib
        observed = None
        facts = self.facts.refresh()
        # Read-only sampling reuses a short-lived identity observation. Every
        # mutation still requests a full fresh authentication, without caching.
        if (authenticate or not self.authenticated
                or time.monotonic() - self.authenticated_at >= AUTH_REFRESH_SECONDS):
            self.authenticated = False
            observed = binding(self.target, bridge, native, allow_relocated=True)
            if (observed['process'] != self.target['process']
                    or observed['identity'] != self.target['identity']):
                self.close_observer()
            self.authenticated_at = time.monotonic()
        valid = observed is not None or self.authenticated
        current_binding = observed or dict(identity=self.target['identity'],
                                           process=self.target['process'])
        native_client = client_snapshot(self.target, current_binding, facts)
        try:
            # Use the freshly authenticated process before adoption; the old
            # enrolled PID may have a different --remote endpoint.
            rpc = self.observation_rpc(current_binding)
            th, turn = metadata(rpc, self.target['session_id'])
            goal_result = rpc.request('thread/goal/get', {'threadId': self.target['session_id']})
        except (FileNotFoundError, ConnectionError, TimeoutError):
            self.close_observer()
            # An unavailable socket never grants authority. The native v4
            # foreground record and connected writer must independently pass.
            th, turn = native_client['thread'], native_client['turn']
            goal_result = {'goal': native_client['goal_full']}
        else:
            if (native_client['goal_full'] != goal_result.get('goal')
                    or (turn or {}).get('id') != native_client['turn']['id']
                    or (turn or {}).get('status') != native_client['turn']['status']):
                self.close_observer()
                raise RuntimeError('native Goal/turn and control endpoint disagree')
        goal = goal_identity(goal_result)
        full_goal = goal_result.get('goal')
        blocked_event = self.goal_notices.blocked_event(full_goal, (turn or {}).get('id'))
        blocked_revision = ({k: full_goal.get(k) for k in ('createdAt', 'updatedAt')}
                            if (full_goal or {}).get('status') == 'blocked' else None)
        valid = (valid and th['path'] == self.target['path']
                 and th.get('model') == self.target['model']
                 and th.get('reasoningEffort') == self.target['effort'])
        if valid and observed is not None:
            if (facts.get('session_id') != self.target['session_id']
                    or facts['path'] != self.target['path']):
                raise RuntimeError('original rollout session changed; no process adoption')
            adopting = (observed['process'] != self.target['process']
                        or observed['identity'] != self.target['identity']
                        or self.inputs.pending_binding is not None)
            if adopting:
                if (th.get('sessionId') != self.target['session_id']
                        or th.get('canAcceptDirectInput') is not True
                        or binding(self.target, bridge, native, allow_relocated=True) != observed):
                    raise RuntimeError('current loaded session not verified; no adoption')
                self.inputs.adopt_binding(dict(binding=observed, thread=th), facts)
            self.adopt_process(observed, facts)
            self.authenticated = True
        compact = native_compaction_start(self.target['session_id'], turn, facts)
        self.compact_cache = compact
        guard = self.inputs.refresh(final=authenticate)
        after_facts = self.facts.snapshot()
        if any(after_facts.get(key) != facts.get(key) for key in
               ('file_id', 'user_offset', 'compacted_offset', 'observed_turn',
                'compaction_occurrence', 'turn_context', 'lifecycle_error', 'caught_up')):
            raise RuntimeError('native occurrence or input changed during final observation')
        config = json.loads(self.config_path.read_text())
        result = dict(binding_ok=valid, thread=th, turn=turn, facts=facts, compact=compact,
                    input_guard=guard, goal=goal, goal_full=full_goal,
                    blocked_goal_event=blocked_event,
                    goal_observer=dict(healthy=self.goal_notices.healthy,
                                       connection_id=self.goal_notices.connection_id,
                                       cursor=self.goal_notices.cursor),
                    blocked_goal_revision=blocked_revision, authenticated_at=self.authenticated_at,
                    paused=paused(config, self.target)
                    or (self.directory.parent / 'STOP').exists())
        result.update({k: native_client[k] for k in ('goal_revision', 'client_pid', 'client_epoch',
                       'client_capability', 'foreground', 'writer')})
        return result

    def notify(self, title, body):
        try:
            subprocess.run(['rtk', 'proxy', CMUX, 'notify', '--workspace', self.target['workspace_id'],
                            '--surface', self.target['surface_id'], '--title', title, '--body', body],
                           timeout=2, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.journal.record('NOTIFICATION_FAILED', error=str(exc)[:200])

    def observe_without_control(self, control_error, *, notify=True):
        """Keep original-session journal observation alive if RPC is unavailable.

        This writes separate observation and notification evidence. It never adopts a process,
        refreshes pending-input coverage, arms a control deadline, or settles an
        existing episode. RPC-only state and its last successful watermark stay
        untouched so consumers cannot mistake observation for recoverability.
        """
        with self.lock:
            bridge, native = self.lib[:2]
            observed = binding(self.target, bridge, native, allow_relocated=True)
            facts = self.facts.refresh()
            if (facts.get('session_id') != self.target['session_id']
                    or facts['path'] != self.target['path']):
                raise RuntimeError('original rollout session changed; no observation')
            compact = unsettled_compaction(self.target['session_id'], facts, self.db_path)
            # Check lifecycle and the process binding again after the log query.
            after = self.facts.refresh()
            if (after['offset'] != facts['offset'] or not after['caught_up']):
                compact = None
            if binding(self.target, bridge, native, allow_relocated=True) != observed:
                raise RuntimeError('process binding changed during read-only observation')
            config = json.loads(self.config_path.read_text())
            is_paused = paused(config, self.target) or (self.directory.parent / 'STOP').exists()
            now = time.time()
            age = max(0, now - compact['started_at']) if compact else None
            level = (300 if age >= LIMIT else 180 if age >= WARN else 0) if age is not None else 0
            path = self.directory / 'read-only-observation.json'
            key = compact['key'] if compact else None
            # Keep the notification budget independent of the latest sample:
            # a partial append or temporary unknown must not permit a repeat.
            warning_id = hashlib.sha256(json.dumps([after['file_id'], key]).encode()).hexdigest()
            warning_paths = {n: self.directory / f'read-only-warning-{warning_id}-{n}.json'
                             for n in (180, 300)}
            prior_level = max((n for n, p in warning_paths.items() if p.exists()), default=0)
            should_notify = bool(notify and level > prior_level and not is_paused)
            if should_notify:
                try:
                    write_once(warning_paths[level], dict(at=now, compact_key=key,
                        session_id=self.target['session_id'], mode='read_only',
                        level=level, notification_status='INTENT', control_verified=False))
                except FileExistsError:
                    should_notify = False
            result = dict(at=now, state='read_only_observing', mode='read_only',
                          surface_id=self.target['surface_id'], session_id=self.target['session_id'],
                          process_binding=observed, process_binding_verified=True,
                          binding_ok=False, control_verified=False, control_error=control_error,
                          active_turn_verified=False, paused=is_paused, facts=after,
                          unsettled_compaction=compact, compact_key=key, elapsed=age,
                          warning_level=level,
                          notification_intent_level=level if should_notify else prior_level)
            # Persist warning deduplication before notification. An uncertain
            # notification is not retried and never grants a control budget.
            atomic_json(path, result)
            if should_notify:
                self.notify('压缩观察超时，控制入口不可用',
                            f'原会话已观察到压缩开始，{int(age)} 秒未见完成；仅观察，未发送中断。')
            return result

    def arm(self, episode):
        if self.timer:
            self.timer.stop.set()
        key = episode['key']
        age = max(0, time.time() - episode['started_at'])

        def fire(event, elapsed):
            if self.stop.is_set():
                return
            # Logging and warning notification must never hold the control lock.
            current = self.state.get('episode')
            if not current or current['key'] != key or current['phase'] != 'compacting':
                return
            self.journal.record(event, key=key, elapsed=elapsed, attempt=episode['attempt'])
            if event == 'WARNING_180S':
                self.notify('压缩超过3分钟', '五分钟上限不因 HTTP、reasoning 或重连重置。')
            else:
                self.recover(key)

        self.timer = Deadline(fire, warn=WARN, limit=LIMIT)
        self.timer.started -= age
        self.timer.start()
        self.journal.record('DEADLINE_ARMED', key=key, elapsed_on_discovery=age,
                            warning_at=episode['started_at'] + WARN,
                            recovery_at=episode['started_at'] + LIMIT)

    def close_episode(self, phase, **fields):
        ep = self.state['episode']
        if self.timer:
            self.timer.stop.set()
        ep.update(phase=phase, **fields)
        self.state['closed'] = (self.state['closed'] + [ep['key']])[-512:]
        self.save()
        self.journal.record(phase.upper(), key=ep['key'], **fields)

    def intent(self, episode, kind, params, snapshot, *, request=None):
        # Exclusive file creation is the durable consumed operation budget.
        path = self.directory / (episode['key'] + '-' + kind + '.json')
        value = dict(kind=kind, epoch=time.time(), params=params,
                     session_id=self.target['session_id'], binding=self.target['process'],
                     turn=snapshot['turn'], facts=snapshot['facts'],
                     input_token=snapshot['input_guard']['token'])
        if kind == 'interrupt':
            value.update(client_request=request, client_epoch=snapshot['client_epoch'],
                         goal_revision=snapshot.get('goal_revision'))
            write_authority(self, episode, snapshot, request, path)
        if kind == 'compact':
            raw = request_bytes(request, self.target['session_id'])
            value.update(native_request=request,
                         native_request_sha256=hashlib.sha256(raw).hexdigest())
        self.journal_directory.write_once(path.name, value)
        self.journal.record(kind.upper() + '_INTENT', params=params, key=episode['key'])
        return value

    def reconcile_compact(self, ep, snap):
        """Adopt native evidence for the original request; never send it again."""
        path = self.directory / (ep['key'] + '-compact.json')
        if not path.exists():
            return False
        intent = json.loads(path.read_text())
        if intent.get('client_request'):
            return self.reconcile_native_compact(ep, snap, intent)
        causal_trace = intent_trace(intent, self.target['session_id'])
        compact, turn = snap.get('compact'), snap.get('turn') or {}
        if (intent.get('session_id') != self.target['session_id']
                or not self.original_process_known(intent.get('binding'))
                or not ep.get('input_token') or intent.get('input_token') != ep['input_token']
                or not compact or compact['source'] != 'manual_submission'
                or not causal_trace or compact.get('trace_id') != causal_trace
                or compact.get('thread_id') != self.target['session_id']
                or compact.get('submission_id') != turn.get('id')
                or ep['attempt'] >= MAX_RETRIES
                or turn.get('status') != 'inProgress' or turn.get('id') == ep['turn_id']
                or compact['turn_id'] != turn.get('id')
                or compact['started_at'] < intent['epoch']
                or not self.unchanged(ep, snap, new_turn=True)):
            return False
        next_ep = make_episode(snap, root=ep['root'], attempt=ep['attempt'] + 1,
                               submitted_at=intent['epoch'])
        next_ep['input_token'] = ep['input_token']
        next_ep['parent_key'] = ep['key']
        self.state['closed'] = (self.state['closed'] + [ep['key']])[-512:]
        self.state['episode'] = next_ep
        self.save()
        self.journal.record('ORIGINAL_COMPACT_NATIVE_CONFIRMED', key=next_ep['key'],
                            parent_key=ep['key'], submitted_at=intent['epoch'],
                            native_started_at=compact['started_at'], attempt=next_ep['attempt'],
                            trace_id=causal_trace, submission_id=compact['submission_id'],
                            replayed=False, recovery_at=intent['epoch'] + LIMIT)
        self.arm(next_ep)
        return True

    def native_receipt_pending(self, episode):
        """Observe a submitted request before any snapshot can refresh its ledgers."""
        if episode.get('phase') != 'control_pending':
            return False
        intent_path = self.directory / (episode['key'] + '-interrupt.json')
        if not intent_path.exists():
            return True
        _pin, raw = pinned_file(intent_path)
        intent = json.loads(raw)
        request = intent.get('client_request')
        if not isinstance(request, dict) or request.get('attemptId') != episode['key']:
            return False  # Historical requests retain their original reconciliation.
        try:
            read_receipt(self.directory, episode['key'], 'compact-started', request)
        except FileNotFoundError:
            for phase in ('not-interrupted', 'compact-denied'):
                try:
                    fields = read_receipt(self.directory, episode['key'], phase, request)
                except FileNotFoundError:
                    continue
                if (fields.get('turnId') != episode['turn_id']
                        or fields.get('attemptId') != episode['key']):
                    raise ValueError('native denial receipt differs from the original request')
                self.close_episode('cancelled', reason='native conditional operation declined',
                                   native_denial=phase)
                return False
            return True
        return False

    def reconcile_native_compact(self, ep, snap, intent):
        """Only the original native client may attest the new compact turn."""
        request = intent.get('client_request') or {}
        if (request.get('attemptId') != ep['key']
                or request.get('expectedTurnId') != ep['turn_id']
                or request.get('compactionOccurrence') != ep.get('compaction_occurrence')
                or request.get('clientPid') != ep.get('client_pid')
                or intent.get('client_epoch') != ep.get('client_epoch')
                or intent.get('session_id') != self.target['session_id']
                or not self.original_process_known(intent.get('binding'))
                or intent.get('input_token') != ep.get('input_token')
                or snap.get('client_epoch') != ep.get('client_epoch')
                or snap.get('client_pid') != ep.get('client_pid')
                or ep['attempt'] >= MAX_RETRIES):
            return False
        try:
            claimed = read_receipt(self.directory, ep['key'], 'client-claimed', request)
            interrupted = read_receipt(self.directory, ep['key'], 'interrupted', request)
            receipt = read_receipt(self.directory, ep['key'], 'compact-started', request)
        except FileNotFoundError:
            return False
        if claimed != {'status': 'UNKNOWN'} or interrupted != {'turnId': ep['turn_id']}:
            raise ValueError('native claim/join receipts differ from the original request')
        started = receipt.get('turnId')
        if started is None:
            self.close_episode('cancelled', reason='native conditional compact declined after the joined interruption')
            return True
        adopted_goal = goal_after_join(ep, snap, receipt)
        compact, turn = snap.get('compact'), snap.get('turn') or {}
        if (not isinstance(started, str) or not started or started == ep['turn_id']
                or not compact or compact['turn_id'] != started
                or turn.get('id') != started or turn.get('status') != 'inProgress'
                or compact.get('compaction_occurrence') == ep.get('compaction_occurrence')
                or compact['started_at'] < intent['epoch']
                or not self.unchanged(adopted_goal, snap, new_turn=True)):
            return False
        next_ep = make_episode(snap, root=ep['root'], attempt=ep['attempt'] + 1)
        next_ep.update(input_token=ep['input_token'], parent_key=ep['key'],
                       native_receipt=dict(path=str(self.directory / (ep['key'] + '-compact-started.json')),
                                           turn_id=started))
        self.state['closed'] = (self.state['closed'] + [ep['key']])[-512:]
        self.state['episode'] = next_ep
        self.save()
        self.journal.record('NATIVE_COMPACT_RECEIPT_ADOPTED', key=ep['key'],
                            new_key=next_ep['key'], turn_id=started, attempt=next_ep['attempt'])
        self.arm(next_ep)
        return True

    def recovery_locks(self):
        return control_locks(self.lib, copy.deepcopy(self.target), self.config_path)

    def recover(self, key):
        with self.lock:
            ep = self.state.get('episode')
            if not ep or ep['key'] != key or ep['phase'] != 'compacting':
                return
            if not self.apply or not self.target.get('recovery_enabled', True):
                self.close_episode('observe_only', reason='control disabled for this enrollment')
                return
            acquired = False
            try:
                # Pin the complete authenticated binding, including process
                # birth and pane/TTY, for this entire control transaction.
                locked_target = copy.deepcopy(self.target)
                with self.recovery_locks() as check_locks:
                    acquired = True
                    snap = self.snapshot()
                    # Adoption durably replaces self.state; do not write the
                    # retry marker into the superseded episode object.
                    ep = self.state['episode']
                    if self.target != locked_target:
                        # Observation may authenticate a relocated pane, but
                        # this critical section still holds the original pane's
                        # MAC lock. No intent has been consumed yet: preserve
                        # this episode and take the new lock on the next tick.
                        now = time.time()
                        deferred = ep.get('recovery_deferred') or {}
                        ep['recovery_deferred'] = dict(reason='input_lock_binding_changed',
                            first_at=deferred.get('first_at', now), last_at=now,
                            retry_not_before=now + LOCK_RETRY_SECONDS,
                            count=deferred.get('count', 0) + 1,
                            last_warning=deferred.get('last_warning', 0))
                        self.save()
                        self.journal.record('RECOVERY_DEFERRED_BINDING_CHANGED',
                            key=ep['key'], deadline=ep['started_at'] + LIMIT,
                            retry_not_before=now + LOCK_RETRY_SECONDS, zero_input=True)
                        return
                    if not may_interrupt(ep, snap) or snap['goal'] != ep['goal']:
                        self.close_episode('cancelled', reason='fresh identity, input, pause, Goal or compact changed')
                        return
                    if ep['attempt'] >= MAX_RETRIES:
                        self.close_episode('exhausted', reason='two replacement attempts exhausted; session retained')
                        return
                    request = dict(threadId=locked_target['session_id'], expectedTurnId=ep['turn_id'],
                        attemptId=ep['key'],
                        intentPath=str(self.directory / (ep['key'] + '-interrupt.json')),
                        clientPid=snap['client_pid'], compactionOccurrence=ep['compaction_occurrence'])
                    def final_boundary():
                        fresh = self.snapshot()
                        check_locks()
                        if (self.target != locked_target or not may_interrupt(ep, fresh)
                                or not self.unchanged(ep, fresh)
                                or fresh.get('client_pid') != ep.get('client_pid')
                                or fresh.get('client_epoch') != ep.get('client_epoch')):
                            raise RuntimeError('original target/input/Goal/occurrence changed after intent; no control')
                        return fresh

                    def prepare(fresh):
                        ep['phase'] = 'control_pending'
                        self.save()
                        params = {'threadId': locked_target['session_id'], 'turnId': ep['turn_id']}
                        index = self.inputs.intent_index
                        with index.lock:
                            if index.control_is_pending():
                                raise RuntimeError('another original native control is pending')
                            self.intent(ep, 'interrupt', params, fresh, request=request)
                            index.freeze_for_control(request)
                        # After pinning, reads must not refresh the ledger or mutate this episode.
                        authority_path = self.directory / (ep['key'] + '-authority.json')
                        _authority_pin, raw = pinned_file(authority_path)
                        authority = json.loads(raw)
                        for pin in [authority['foreground'], authority['enrollment'],
                                    authority['config'], *authority['pinned_inputs']]:
                            if pinned_file(pin['path'])[0] != pin:
                                raise RuntimeError('original authority pin changed; no control')
                        if binding(self.target, self.lib[0], self.lib[1]) != dict(
                                identity=locked_target['identity'], process=locked_target['process']):
                            raise RuntimeError('native process or target changed after pinning')

                    outcome = conditional_submit(self, request, snap, check_locks, final_boundary, prepare)
                    self.journal.record('CONDITIONAL_SUBMIT_RETURNED', key=key, outcome=outcome)
                    # Neither an RPC reply nor a host paste acknowledgement authorizes another request.
                    result = outcome.get('result')
                    if result is not None:
                        if (not isinstance(result, dict) or result.get('attemptId') != key
                                or result.get('expectedTurnId') != ep['turn_id']
                                or type(result.get('interruptionCompleted')) is not bool):
                            raise RuntimeError('original conditional response UNKNOWN')
                        if result['interruptionCompleted'] is False:
                            self.close_episode('cancelled', reason='native conditional interruption denied')
                            return
                    if self.native_receipt_pending(ep):
                        self.journal.record('CONDITIONAL_NATIVE_RECEIPT_PENDING', key=key,
                                            replay_authorized=False)
                        return
                    after = self.snapshot()
                    if not self.reconcile_compact(ep, after):
                        # Keep the original phase while awaiting its receipt/item event.
                        self.save()
                        self.journal.record('CONDITIONAL_NATIVE_RECEIPT_PENDING', key=key,
                                            replay_authorized=False)
            except ControlLockBusy as exc:
                if ep.get('phase') == 'control_pending':
                    self.state['control_outcome'] = dict(key=ep['key'], status='UNKNOWN',
                        reason=str(exc)[:500], at=time.time(), replay_authorized=False)
                    self.save()
                    return
                if acquired or ep['phase'] != 'compacting':
                    self.close_episode('uncertain', reason=str(exc)[:500])
                    return
                now = time.time()
                deferred = ep.get('recovery_deferred') or {}
                ep['recovery_deferred'] = dict(reason='input_lock_busy',
                    first_at=deferred.get('first_at', now), last_at=now,
                    retry_not_before=now + LOCK_RETRY_SECONDS,
                    count=deferred.get('count', 0) + 1,
                    last_warning=deferred.get('last_warning', 0))
                if now - ep['recovery_deferred']['last_warning'] >= 60:
                    ep['recovery_deferred']['last_warning'] = now
                    self.notify('压缩恢复等待输入锁', '已超过五分钟；保留原次，三秒后重新核验，未发送中断。')
                self.save()
                self.journal.record('RECOVERY_DEFERRED_LOCK_BUSY', key=ep['key'],
                                    deadline=ep['started_at'] + LIMIT, elapsed=now - ep['started_at'],
                                    retry_not_before=now + LOCK_RETRY_SECONDS, zero_input=True)
            except Exception as exc:
                if ep.get('phase') == 'control_pending':
                    # The native operation can still finish after a transport timeout.
                    # Keep its raw episode immutable for its original write-once receipts.
                    self.state['control_outcome'] = dict(key=ep['key'], status='UNKNOWN',
                        reason=str(exc)[:500], at=time.time(), replay_authorized=False)
                    self.save()
                    self.journal.record('CONDITIONAL_OUTCOME_UNKNOWN', key=ep['key'],
                                        reason=str(exc)[:500], replay_authorized=False)
                else:
                    self.close_episode('uncertain', reason=str(exc)[:500])

    def handoff_path(self, kind, key):
        return self.directory / ('%s-%s.json' % (kind, key))

    def settle_handoff(self, ep, snap):
        # W1: persist the post-compaction baseline once, only for natural turns.
        reasons = settle_rejects(ep, snap)
        if reasons:
            self.state['handoff'] = dict(key=ep['key'], phase='ineligible', reasons=reasons)
            self.journal.record('HANDOFF_INELIGIBLE', key=ep['key'], reasons=reasons)
            return
        config_sha = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        record = settle_record(ep, snap, self.target, config_sha, time.time())
        sha = write_once(self.handoff_path('settle', ep['key']), record)
        self.state['handoff'] = dict(key=ep['key'], phase='settled', settle_sha256=sha)
        self.journal.record('HANDOFF_SETTLED', key=ep['key'], settle_sha256=sha)

    def handoff_step(self, snap):
        # W2/W3: watch the settled turn to its end; revocation is permanent.
        record = self.state.get('handoff')
        if not record or record.get('phase') not in {'settled', 'handoff'}:
            return
        try:
            self._handoff_step(record, snap)
        except Exception as exc:
            # A settle that cannot be read or judged never leaves an admissible
            # state behind: revoke permanently and persist before tick goes on.
            try:
                self.revoke_handoff(record, ['settle unreadable: %s' % type(exc).__name__])
            finally:
                self.save()

    def _handoff_step(self, record, snap):
        path = self.handoff_path('settle', record['key'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['settle_sha256']:
            return self.revoke_handoff(record, ['settle changed'])
        settle = json.loads(raw)
        deadline = record.get('expires_at', settle['expires_at'])
        reasons = revoke_reasons(settle, snap, deadline, time.time())
        if reasons:
            return self.revoke_handoff(record, reasons)
        if record['phase'] == 'settled' and handoff_ready(settle, snap):
            value = handoff_record(settle, record['settle_sha256'], snap, time.time())
            sha = write_once(self.handoff_path('handoff', record['key']), value)
            record.update(phase='handoff', handoff_sha256=sha, expires_at=value['expires_at'])
            self.journal.record('HANDOFF_PRODUCED', key=record['key'], handoff_sha256=sha)

    def revoke_handoff(self, record, reasons):
        # In-memory phase first: a failed marker write must not leave it admissible.
        record.update(phase='revoked', reasons=reasons)
        try:
            write_once(self.handoff_path('revoked', record['key']),
                       dict(key=record['key'], reasons=reasons, at=time.time()))
        except FileExistsError:
            pass
        self.journal.record('HANDOFF_REVOKED', key=record['key'], reasons=reasons)

    def unchanged(self, episode, snapshot, *, new_turn=False):
        facts = snapshot['facts']
        if (not snapshot['binding_ok'] or snapshot['paused'] or not facts['caught_up']
                or facts['path'] != episode['path'] or facts['file_id'] != episode['file_id']
                or facts['user_offset'] != episode['user_offset']
                or facts['compacted_offset'] != episode['compacted_offset']
                or snapshot['goal'] != episode['goal']
                or snapshot.get('goal_revision') != episode.get('goal_revision')
                or snapshot.get('client_pid') != episode.get('client_pid')
                or snapshot.get('client_epoch') != episode.get('client_epoch')):
            return False
        return (clear_guard(snapshot)
                and snapshot['input_guard']['token'] == episode.get('input_token'))

    def tick(self):
        with self.lock:
            episode = self.state.get('episode') or {}
            if self.native_receipt_pending(episode):
                self.observe_without_control('original native receipt pending; input evidence pinned',
                                             notify=False)
                return
            snap = self.snapshot(authenticate=False)
            self.state['observed_at'] = time.time()
            self.state['native'] = {k: snap[k] for k in
                                    ('binding_ok', 'thread', 'turn', 'compact', 'paused', 'facts',
                                     'input_guard', 'goal', 'goal_full', 'blocked_goal_event',
                                     'blocked_goal_revision', 'goal_observer', 'goal_revision', 'client_pid',
                                      'client_epoch', 'client_capability', 'foreground', 'writer') if k in snap}
            self.handoff_step(snap)
            ep = self.state.get('episode')
            if not snap['binding_ok'] or snap['paused'] or not snap['facts']['caught_up']:
                self.save()
                return
            if ep and ep['phase'] == 'compacting':
                if settled(ep, snap):
                    self.close_episode('compacted', applied_at=snap['facts']['compacted_at'],
                                       duration=snap['facts']['compacted_at'] - ep['started_at'])
                    self.state['progress_after'] = snap['facts']['compacted_offset']
                    self.settle_handoff(ep, snap)
                    self.save()
                    return
                if (not snap['turn'] or snap['turn']['id'] != ep['turn_id']
                        or snap['turn']['status'] != 'inProgress'
                        or not self.unchanged(ep, snap)):
                    self.close_episode('cancelled', reason='external input or native turn changed')
                    return
                deferred = ep.get('recovery_deferred')
                if (deferred and time.time() >= max(ep['started_at'] + LIMIT,
                                                    deferred['retry_not_before'])):
                    self.recover(ep['key'])
                    return
            elif ep and ep['phase'] in {'control_pending', 'uncertain'}:
                if self.reconcile_compact(ep, snap):
                    return
                # A timeout or restart never refreshes the operation budget.
                # Only a later, fully accounted native input can separate a
                # new incident from this uncertain original operation.
                if (not clear_guard(snap)
                        or snap['input_guard']['token'] == ep.get('input_token')
                        or snap['facts']['user_offset'] <= ep['user_offset']):
                    self.save()
                    return
            if (self.state.get('progress_after') is not None
                    and snap['facts']['tool_result_offset'] > self.state['progress_after']):
                self.journal.record('POST_COMPACT_TOOL_RESULT', at=snap['facts']['tool_result_at'],
                                    offset=snap['facts']['tool_result_offset'])
                self.state['progress_after'] = None
            if (snap['compact'] and clear_guard(snap)
                    and (not ep or ep['phase'] != 'compacting')):
                fresh = make_episode(snap)
                if fresh['key'] not in self.state['closed']:
                    self.state['episode'] = fresh
                    self.save()
                    self.arm(fresh)
            self.save()

    def run(self):
        try:
            # Catch up once before arming; the original native start is retained.
            while not self.facts.refresh()['caught_up'] and not self.stop.is_set():
                pass
            ep = self.state.get('episode')
            if ep and ep['phase'] == 'compacting':
                self.arm(ep)
            while not self.stop.is_set():
                try:
                    self.tick()
                    atomic_json(self.directory / 'worker-health.json', dict(
                        at=time.time(), state='observing', surface_id=self.target['surface_id'],
                        session_id=self.target['session_id'],
                        binding_verified=self.state.get('native', {}).get('binding_ok', False)))
                    self.last_error = None
                except Exception as exc:
                    error = f'{type(exc).__name__}: {exc}'[:500]
                    if error != self.last_error:
                        self.journal.record('OBSERVATION_ERROR_NO_CONTROL', error=error)
                    read_only = None
                    read_only_error = None
                    try:
                        read_only = self.observe_without_control(error)
                    except Exception as observation_error:
                        read_only_error = f'{type(observation_error).__name__}: {observation_error}'[:500]
                    try:
                        atomic_json(self.directory / 'worker-health.json', dict(
                            at=time.time(), state='read_only_observing' if read_only else 'observation_unavailable',
                            surface_id=self.target['surface_id'], session_id=self.target['session_id'],
                            error=error, control_verified=False,
                            read_only_observed_at=read_only['at'] if read_only else None,
                            read_only_error=read_only_error))
                    except OSError:
                        pass
                    self.last_error = error
                ep = self.state.get('episode') or {}
                self.stop.wait(1 if ep.get('phase') in {'compacting', 'control_pending', 'uncertain'} else 3)
        finally:
            self.close()


def run_target(target, directory, lib, stop, *, apply=False, intent_index=None,
               config_path=CONFIG, enrollment_path=None, once=False, worker_factory=Worker):
    """One target's broken evidence must not stop every other observer.

    Retry only construction/observation. Worker retains the durable operation
    ledger, so this loop never creates a new control or resets an unknown one.
    """
    health_path = directory / target['session_id'] / 'worker-health.json'
    previous_error = None
    while not stop.is_set():
        worker = None
        run_owns_close = False
        try:
            worker_options = dict(apply=apply, intent_index=intent_index, config_path=config_path)
            if enrollment_path is not None:
                worker_options['enrollment_path'] = enrollment_path
            worker = worker_factory(target, directory, lib, stop, **worker_options)
            atomic_json(health_path, dict(at=time.time(), state='initialized',
                        surface_id=target['surface_id'], session_id=target['session_id'],
                        control_verified=False))
            if once:
                while not worker.facts.refresh()['caught_up'] and not stop.is_set():
                    pass
                result = dict(surface=target['label'], **worker.snapshot())
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return result
            run_owns_close = True
            worker.run()
            return
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'[:500]
            result = dict(at=time.time(), surface_id=target['surface_id'],
                          session_id=target['session_id'], state='observation_unavailable',
                          error=error, control_verified=False)
            if once and worker is not None:
                try:
                    observed = worker.observe_without_control(error, notify=False)
                    result.update(state='read_only_observing', binding_ok=False,
                                  read_only_observation=observed)
                except Exception as observation_error:
                    result['read_only_error'] = f'{type(observation_error).__name__}: {observation_error}'[:500]
            try:
                health_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                atomic_json(health_path, result)
            except OSError as storage_error:
                result['health_write_error'] = str(storage_error)[:200]
            if error != previous_error or once:
                print(json.dumps(dict(event='TARGET_OBSERVATION_UNAVAILABLE', **result)),
                      flush=True)
            previous_error = error
            if once:
                return result
        finally:
            if worker is not None and not run_owns_close:
                try:
                    worker.close()
                except Exception as exc:
                    print(json.dumps(dict(event='TARGET_CLEANUP_FAILED',
                        surface_id=target['surface_id'], error=str(exc)[:200])), flush=True)
        stop.wait(INITIALIZATION_RETRY_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enrollment', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    lib = libraries()
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = lib[2].FileLock(args.state_dir / 'service.lock', timeout_sec=0,
                          purpose='single compaction watchdog')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with lock:
        enrollment = json.loads(args.enrollment.read_text())
        if enrollment.get('scope') not in {
            'four_existing_compact_sessions_180_300', 'existing_compact_sessions_180_300_v2'
        }:
            raise RuntimeError('explicit scoped enrollment required')
        targets = enrollment.get('targets')
        if (not isinstance(targets, list) or not targets
                or len({t['session_id'] for t in targets}) != len(targets)
                or len({t['surface_id'] for t in targets}) != len(targets)):
            raise RuntimeError('unique, explicitly bound existing targets required')
        intents = IntentIndex(args.state_dir / 'native-input-intents.json')
        if args.once:
            failed = False
            for target in targets:
                result = run_target(target, args.state_dir, lib, stop, apply=False,
                                    intent_index=intents, config_path=args.config, enrollment_path=args.enrollment, once=True)
                failed |= not (result or {}).get('binding_ok', False)
            return int(failed)
        threads = [threading.Thread(target=run_target, name=t['label'],
                   args=(t, args.state_dir, lib, stop),
                   kwargs=dict(apply=args.apply, intent_index=intents, config_path=args.config,
                                enrollment_path=args.enrollment))
                   for t in targets]
        print(json.dumps({'event': 'WATCHDOG_STARTED', 'epoch': time.time(), 'pid': os.getpid(),
                          'apply': args.apply, 'targets': [t['label'] for t in targets]}), flush=True)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


if __name__ == '__main__':
    raise SystemExit(main())
