"""Original-controller exit recovery boundaries.

Historical context is a locator. It never proves an exit, an empty shell or
permission to type. This module does not create a controller or send input.
The owning loop supplies its existing owner check and one transport adapter.
"""
from __future__ import annotations

from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import ctypes
import hashlib
import json
import os
from pathlib import Path

from ccc_herdr_exit_context import (
    PinnedDirectory, PROTECTED, SCHEMA, canonical, history_header, need,
)


BINDING_FIELDS = ('session', 'pane_id', 'terminal_id', 'cmux_workspace_id',
                  'cmux_surface_id', 'server')
TERMINAL_PHASES = {'native_resumed', 'native_work_observed', 'refused_before_input'}


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def select_context(state, original_stores, terminal_binding, *, expected_generation=None):
    """Only a unique captured generation in the exact original terminal.

    Do not rank by mtime, directory order or an old pane label. Format 1 is
    retained as evidence but cannot replace the configuration-bearing record.
    If the terminal has hosted two captured generations, selection is UNKNOWN
    until the owner provides a separately authenticated last-selection record.
    """
    if expected_generation is not None:
        need(set(expected_generation)=={'pid','birth'} and type(expected_generation['pid']) is int
             and isinstance(expected_generation['birth'],list) and len(expected_generation['birth'])==2,
             'original_host_exit_generation_invalid')
    state = Path(state)
    need(str(state) == original_stores['state'], 'original_state_path_changed')
    need(terminal_binding['cmux_surface_id'].upper() != PROTECTED,
         'protected_business_surface')
    with PinnedDirectory(state, expected=original_stores['state_identity'],
                         forbidden_mode=0o022) as parent:
        with PinnedDirectory(state / 'original-exit-contexts') as directory:
            raw = directory.read('index.json', 512 * 1024)
            need(raw is not None, 'original_context_index_missing')
            index = json.loads(raw)
            need(index.get('schema') == 'ccc-exit-context-index-v2'
                 and index.get('state_identity') == list(parent.identity)
                 and index.get('directory_identity') == list(directory.identity)
                 and isinstance(index.get('entries'), dict)
                 and len(index['entries']) <= 128, 'original_context_index_invalid')
            found = []
            for entry in index['entries'].values():
                identity = entry['identity']
                if identity.get('capture_format') != 2:
                    continue
                if expected_generation is not None and any(identity.get(k)!=v for k,v in expected_generation.items()):
                    continue
                if (any(identity.get(k) != terminal_binding.get(k) for k in BINDING_FIELDS)
                        or identity.get('key') != terminal_binding.get('key')):
                    continue
                sha = entry['sha256']
                need(isinstance(sha, str) and len(sha) == 64
                     and all(c in '0123456789abcdef' for c in sha), 'invalid_context_digest')
                body = directory.read(sha + '.json')
                need(body is not None and hashlib.sha256(body).hexdigest() == sha,
                     'original_context_bytes_changed')
                document = json.loads(body)
                from ccc_herdr_exit_context import ExitContextMonitor
                ExitContextMonitor._match_document(document, identity)
                need(document.get('schema') == SCHEMA and document.get('capture_format') == 2
                     and document.get('capture_owner') == entry.get('capture_owner')
                     and document['capture_owner']['original_stores'] == original_stores
                     and document['binding']['pid'] != PUBLIC_PROTECTED_PID
                     and document['configuration_evidence']['state']
                         == 'HISTORICAL_CONFIGURATION_OBSERVED', 'original_context_owner_unbound')
                header = history_header(document['history'], document['session_id'])
                need(header['header_sha256'] == document['history']['header_sha256'],
                     'original_history_header_changed')
                found.append((sha, document))
            need(directory.read('index.json', 512 * 1024) == raw,
                 'original_context_index_changed')
            parent.check(); directory.check()
    need(len(found) == 1, 'original_context_missing' if not found
         else 'original_context_generation_ambiguous')
    return found[0]


def generation_state(scope, pid, expected_birth):
    """A missing birth read alone is UNKNOWN, not proof that a process exited."""
    need(type(pid) is int and pid > 1 and pid != PUBLIC_PROTECTED_PID, 'invalid_original_pid')
    native = scope.native
    if native._proc_listpids is None:
        return {'state': 'unknown', 'reason': 'native_process_inventory_unavailable'}

    def inventory():
        count = native._proc_listpids(1, 0, None, 0)
        need(0 < count < 4 * 1024 * 1024, 'native_process_inventory_unavailable')
        buf = (ctypes.c_int * ((count + 16384) // 4))()
        size = ctypes.sizeof(buf)
        used = native._proc_listpids(1, 0, buf, size)
        need(0 < used < size and used % 4 == 0, 'native_process_inventory_incomplete')
        return set(buf[:used // 4])

    before = inventory()
    observed = scope.birth(pid)
    after = inventory()
    if observed == expected_birth and pid in before and pid in after:
        return {'state': 'alive', 'reason': 'original_generation_still_running'}
    if observed is None and pid not in before and pid not in after:
        return {'state': 'exited', 'reason': 'absent_from_two_native_inventories'}
    if observed is not None and observed != expected_birth:
        return {'state': 'unknown', 'reason': 'original_pid_reused'}
    return {'state': 'unknown', 'reason': 'native_process_observations_disagree'}


def assess(document, terminal, generation, history, goal, *, policy, owned_host=None):
    """Separate preserving a running agent from recovering an exited one."""
    result = dict(schema='ccc-original-exit-boundary-v1', phase='held',
        session_id=document['session_id'], recovery_available=False,
        input_operations=0, original_launch_context_available=True,
        attention_required=False, preserve_running_agents=True)
    binding = document['binding']
    observed = terminal.get('binding', {})
    same = (all(observed.get(k) == binding.get(k) for k in BINDING_FIELDS)
            and observed.get('key') == binding['socket'] + '#' + binding['pane_id'])
    event = history.get('last_native_event') or {}
    terminal_ready=terminal.get('phase')=='shell_foreground' if owned_host is None else False
    if owned_host is not None:
        identity=owned_host.get('identity',{});host_terminal=identity.get('terminal',{})
        exited=owned_host.get('last_exit') or {};child=exited.get('child') or {}
        processes=terminal.get('processes') or []
        terminal_ready=bool(terminal.get('phase')=='program_foreground'
            and owned_host.get('phase')=='awaiting_original_controller' and owned_host.get('child') is None
            and len(processes)==1 and processes[0].get('pid')==identity.get('host_pid')
            and processes[0].get('birth')==identity.get('host_birth')
            and terminal.get('tty')==str(host_terminal.get('tty','')).removeprefix('/dev/')
            and terminal.get('foreground_pgid')==host_terminal.get('pgid')
            and child.get('pid')==binding['pid'] and child.get('birth')==binding['birth']
            and type(exited.get('exit_status')) is int and exited['exit_status']>0)
    reason = 'remaining_execution_checks_required'
    if binding['pid'] == PUBLIC_PROTECTED_PID or binding['cmux_surface_id'].upper() == PROTECTED:
        reason = 'protected_business_surface'
    elif generation.get('state') != 'exited':
        reason = generation.get('reason', 'original_exit_unverified')
    elif not same or not terminal_ready:
        reason = 'same_original_shell_unverified'
    elif history.get('session_id') != document['session_id'] or goal.get('session_id') != document['session_id']:
        reason = 'original_sid_mismatch'
    elif (history.get('file_id') != [document['history']['device'], document['history']['inode']]
          or history.get('header_sha256') != document['history']['header_sha256']):
        reason = 'original_history_identity_changed'
    elif event.get('kind') == 'turn_aborted':
        reason = 'user_abort_preserved'
    elif goal.get('status') != 'active':
        # Blocked is not implicitly reopened by launching another process.
        reason = 'original_goal_' + str(goal.get('status', 'unknown'))
    elif event.get('kind') != 'task_complete' or not event.get('error'):
        reason = 'unexpected_exit_cause_not_verified'
    elif policy.get('resume_original_after_error_exit') is not True:
        reason = 'original_exit_recovery_policy_disabled'
    else:
        result.update(phase='exit_verified', attention_required=True)
    return dict(result, reason=reason)


def diagnostic_adapter(transport, previous):
    """Connect saved contexts to the original observer; retain old locators.

    No reconstruction of missing client RPC and no startup from reader UNKNOWN.
    This is the input to the execution controller, not automatic enrollment.
    """
    def diagnose(observer, endpoint, pane, server, terminal):
        try:
            need(terminal.get('phase') == 'shell_foreground'
                 and terminal.get('schema') == 'ccc-herdr-terminal-program-r38'
                 and terminal.get('mode') == 'read_only', 'original_shell_unverified')
            binding = terminal['binding']
            need(binding['server'] == server
                 and binding['key'] == endpoint['socket']+'#'+pane['pane_id']
                 and binding['terminal_id'] == pane['terminal_id']
                 and binding['cmux_surface_id'] == endpoint['cmux_surface_id']
                 and binding['cmux_workspace_id'] == endpoint['cmux_workspace_id'],
                 'current_terminal_binding_mismatch')
            stores = observer.guard_config['original_stores']
            sha, document = select_context(stores['state'], stores, binding)
            generation = generation_state(observer.scope, document['binding']['pid'],
                                          document['binding']['birth'])
            base = dict(schema='ccc-herdr-original-exit-diagnostic-r41', mode='read_only',
                binding=binding, advisory_session_id=document['session_id'],
                original_launch_context_available=True, context_sha256=sha,
                original_generation=generation, recovery_available=False,
                current_thread_authority=False, control_verified=False,
                input_operations=0, goal_writes=0, new_attempt_created=False,
                attention_required=False, business_no_restart_constraint=True)
            if generation['state'] != 'exited':
                return dict(base, phase='original_generation_not_exited', reason=generation['reason'])
            history = transport.r40_original_history(document['history']['path'], document['session_id'])
            # State/Queue/Goals use the same SqliteConfig home, unlike logs.
            db = document['configuration_evidence']['database_locator']
            goal = transport.r40_original_goal(str(Path(db['path']).parent), document['session_id'])
            disposition = assess(document, terminal, generation, history, goal,
                policy=observer.guard_config.get('original_exit_recovery', {}))
            again = transport.terminal_program_observation(observer, endpoint, pane, server)
            fields = ('phase', 'binding', 'processes', 'shell_pid', 'foreground_pgid', 'tty')
            need(all(again.get(k) == terminal.get(k) for k in fields),
                 'original_shell_changed_during_diagnostic')
            need(generation_state(observer.scope, document['binding']['pid'],
                 document['binding']['birth']) == generation, 'original_generation_changed')
            return dict(base, phase=disposition['phase'], reason=disposition['reason'],
                attention_required=disposition['attention_required'],
                original_goal_status=goal['status'],
                original_goal_revision=goal.get('updated_at_ms'),
                history_file_id=history['file_id'], history_fence=history['fence'],
                last_native_event_kind=(history.get('last_native_event') or {}).get('kind'),
                last_native_event_has_error=bool((history.get('last_native_event') or {}).get('error')),
                remaining_checks=['same_empty_shell_input', 'native_no_other_writer',
                    'current_launch_inputs', 'original_single_use_intent', 'native_same_sid_receipt'])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            old = previous(observer, endpoint, pane, server, terminal)
            return dict(old, saved_context_reason=str(error)[:200])
    return diagnose


class OriginalExitAttempts:
    """One append-only attempt per exact error/terminal generation.

    Intent is durable before calling the original transport. An exception or a
    lost response remains UNKNOWN. Re-entering this method only reconciles;
    it never pastes again, presses Enter, refunds or creates a second attempt.
    The owner must keep the ORIGINAL guard/pane input locks for this call.
    """
    def __init__(self, directory, *, owner_recheck):
        self.directory = Path(directory)
        self.owner_recheck = owner_recheck

    @staticmethod
    def attempt_key(plan):
        return digest({k: plan[k] for k in ('session_id', 'binding', 'source_event')})

    def run(self, plan, *, validate, submit, reconcile):
        self.owner_recheck()
        key = self.attempt_key(plan)
        name = key + '.json'
        with PinnedDirectory(self.directory) as directory:
            existing = directory.read(name)
            if existing is not None:
                record = json.loads(existing)
                need(record.get('attempt_key') == key
                     and record.get('plan_sha256') == digest(plan), 'original_exit_attempt_conflict')
                if record['phase'] in TERMINAL_PHASES:
                    return record
                receipt = reconcile(record)
                if receipt is not None:
                    need(receipt.get('same_sid_native_receipt') is True
                         and receipt.get('session_id') == plan['session_id']
                         and receipt.get('original_history_identity') == plan['history_identity']
                         and receipt.get('new_pid_birth_verified') is True
                         and receipt.get('launch_attempt_key') == key,
                         'original_resume_receipt_unverified')
                    record.update(phase='native_work_observed' if receipt.get('natural_work_observed') is True
                                  else 'native_resumed', native_receipt=receipt)
                    self.owner_recheck()
                    directory.publish(name, canonical(record)+b'\n', replace=True, expected=existing)
                return record

            validation = validate(plan)
            need(validation.get('ready') is True, validation.get('reason', 'original_exit_not_ready'))
            self.owner_recheck()
            record = dict(schema='ccc-original-exit-attempt-v1', attempt_key=key,
                plan_sha256=digest(plan), session_id=plan['session_id'],
                plan=plan,
                phase='input_intent', input_operations=0, wire_attempts=0,
                validation=validation, native_receipt=None)
            hosted=plan.get('actuation_kind')=='owned_child_host'
            if hosted:record['process_launches']=0
            raw = canonical(record)+b'\n'
            need(directory.publish(name, raw) is True, 'original_exit_attempt_created_elsewhere')
            need(directory.read(name) == raw, 'original_exit_intent_not_durable')
            # submit performs a fresh final admission using the same owner and
            # original input locks. A host OK is only a host acknowledgement.
            try:
                self.owner_recheck()
                response = submit(plan, record)
                need(isinstance(response, dict) and response.get('payload_sent') is True
                     and response.get('input_operations') in (0, 1), 'host_submission_unverified')
                known_refusal=hosted and response.get('known_not_executed') is True
                need(not known_refusal or (response['input_operations']==0 and response.get('process_launches')==0),
                     'host_refusal_execution_accounting_conflict')
                record.update(phase='refused_before_input' if known_refusal else 'awaiting_native_receipt',
                              input_operations=response['input_operations'],
                              wire_attempts=1, host_response=response)
                if hosted:record['process_launches']=response.get('process_launches',0)
            except Exception as error:
                record.update(phase='write_unknown', wire_attempts=1, input_operations=0 if hosted else 1,
                              input_count_semantics='upper_bound_until_native_receipt',
                              wire_count_semantics='upper_bound_until_transport_evidence',
                              error_type=type(error).__name__)
                if hosted:
                    record.update(process_launches_upper_bound=1,
                                  input_count_semantics='host_channel_never_writes_terminal_input')
            self.owner_recheck()
            directory.publish(name, canonical(record)+b'\n', replace=True, expected=raw)
            return record
