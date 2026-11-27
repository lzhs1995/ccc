"""Adapt original CCC native Codex admission to a private Herdr panel.

The original QueueRecovery/Goal/EncryptedRecovery readers remain authoritative.
Each panel has its own reader and compact store, never a synthetic cmux surface.
An absent native publication alone grants no input; the original official-UI
reader carries separate evidence. No client is restarted here.
"""
from __future__ import annotations

import copy

from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid

import ccc_herdr_lifecycle as life

MAX_RECEIPT = 8 * 1024 * 1024
FINAL_PHASES = frozenset({'native_received', 'native_goal_resumed', 'native_compact_observed',
                         'native_compact_failed', 'native_compact_cancelled',
                         'native_compact_superseded', 'host_refused', 'prewrite_refused'})
COMPACT_RESULTS = {'ready': 'native_compact_observed', 'failed': 'native_compact_failed',
                   'cancelled': 'native_compact_cancelled', 'superseded': 'native_compact_superseded'}


def target(endpoint):
    return {'workspace_id': endpoint['cmux_workspace_id'],
            'surface_id': endpoint['cmux_surface_id']}


def policy(config, endpoint):
    if endpoint.get('discovered_read_only') is True:
        return False, 'discovered_endpoint_monitor_only'
    if config.get('mode') != 'armed':
        return False, 'global_mode_not_armed'
    if config.get('global_paused') or not config.get('codex_enabled', True):
        return False, 'global_pause_or_codex_disabled'
    if endpoint['cmux_workspace_id'] in config.get('codex_excluded_workspace_ids', []):
        return False, 'workspace_excluded'
    for rule in config.get('workspace_rules', []):
        if rule.get('workspace_id') == endpoint['cmux_workspace_id'] and (
                rule.get('paused') or rule.get('enabled', True) is not True
                or rule.get('codex_enabled', True) is not True):
            return False, 'workspace_paused_or_disabled'
    # The excluded outer surface isolates the cmux transport. It must never
    # select an inner target; private membership/terminal checks do that.
    return True, None


def screen_admission(text):
    lines = text.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip()[:1] in {'›', '»'}]
    # A failed remote connection has no writable Codex composer. Recognize
    # that state independently of today's quit-only footer; a future footer
    # change must not turn a disconnected TUI into an input target.
    # Both callers capture at most160 visible lines. Scan that complete bounded
    # frame: intervening output must not hide a failed connection from admission.
    area = '\n'.join(lines)
    if re.search(r'Automatic reconnect could not restore|Server connection could not be restored|Reconnect failed\b[\s\S]{0,120}\brelaunch\b', area, re.I):
        return False, 'client_connection_failed_relaunch_required'
    menu = '\n'.join(lines[-18:])
    if (re.search(r'^\s*Implement this plan\?\s*$', menu, re.M | re.I)
            and re.search(r'enter select\s*·\s*esc back', menu, re.I)):
        return False, 'dialog_or_queued_input'
    if not prompts:
        return False, 'composer_not_verified_empty'
    i = prompts[-1]
    prompt = lines[i].strip()
    if prompt not in {'›', '› Ask Codex to do anything', '»', '» Ask Codex to do anything'}:
        return False, 'user_draft_preserved'
    # Official session-local statusline may prefix the model with its full ID.
    # Normalize this display item only below the verified empty composer. It
    # supplies no selection proof; original native admission remains separate.
    lines[i + 1:] = [re.sub(
        r'^(\s*)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s+·\s+(?=GPT-)',
        r'\1', line) for line in lines[i + 1:]]
    lines[i + 1:] = [re.sub(r'\s*(?:·\s*)?⚠\s+\d+ warnings?\s+·\s+f2 to view\s*$', '', line)
                      if re.search(r'[?] for shortcuts|← for agents|Plan mode', line, re.I) else line for line in lines[i + 1:]]
    footer = '\n'.join(lines[max(0, i - 18):])
    if re.search(r'\[<\d+;\d+;\d+[mM]', footer):
        return False, 'sgr_input_fragment_preserved'
    if re.search(r'Interrupted\s*·|Turn aborted|What should Codex do instead', footer, re.I):
        return False, 'user_interrupted'
    if re.search(r'esc to interrupt|Reconnecting\s+\d+/\d+|running (?:Pre|Post)ToolUse', footer, re.I):
        return False, 'client_working_or_reconnecting'
    if re.search(r'Esc to cancel|Enter to confirm|Do you trust|Permission required|Select.{0,25}(?:model|session)|Press up to edit|queued message|Queued follow-up inputs', footer, re.I):
        return False, 'dialog_or_queued_input'
    # Join all non-empty rows below the verified empty composer so arbitrary
    # terminal column widths (45..240 cols) wrapping inside paths, percentages,
    # "258K\nwindow", or shortened "⚠ 4 · f2" never break footer recognition.
    below = [line.strip() for line in lines[i + 1:] if line.strip()]
    if below:
        joined = re.sub(r'\s+', ' ', ' '.join(below))
        if re.search(r'(?:[$%#]\s*$|^>>>\s)', joined):
            return False, 'composer_footer_unrecognized'
        if not re.search(
                r'\bGPT-[\w.-]+\b|[?] for shortcuts|← for agents|Plan mode|'
                r'context (?:left|used)|\bGoal (?:stalled|paused|achieved)\b|'
                r'Pursuing goal|\bTip:|\bf2\b',
                joined, re.I):
            return False, 'composer_footer_unrecognized'
    return True, None


def terminal_proven(turn):
    proof, signature = turn.get('terminal_tail'), turn.get('signature')
    return bool(isinstance(proof, dict) and isinstance(signature, list)
                and len(signature) == 3 and all(type(n) is int and n > 0 for n in signature)
                and type(proof.get('offset')) is int and proof['offset'] >= 0
                and type(proof.get('end')) is int
                and proof['offset'] < proof['end'] == signature[1]
                and isinstance(proof.get('sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', proof['sha256']))


def selected_current_thread(turn):
    """Recognize explicit and kernel-verified standalone/appserver proofs."""
    if (turn.get('native_foreground_status') == 'ok'
            and type(turn.get('native_selection_schema')) is int
            and turn['native_selection_schema'] == 2
            and turn.get('native_selection_source') == 'schema2_committed_foreground'
            and turn.get('native_selection_evidence')):
        return True
    if turn.get('native_selection_source') in {
            'independent_optional_legacy_reader',
            'original_standalone_writer_with_stale_optional_metadata'}:
        try:
            sid = str(uuid.UUID(turn['session_id']))
            return bool(sid == turn['session_id']
                        and type(turn.get('pid')) is int and turn['pid'] > 0
                        and isinstance(turn.get('birth'), list) and len(turn['birth']) == 2
                        and isinstance(turn.get('transcript_path'), str) and turn['transcript_path'])
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
    if (turn.get('native_foreground_status') != 'official_statusline_native_writer'
            or turn.get('native_selection_schema') is not None
            or turn.get('native_selection_source') != 'official_statusline_native_writer'):
        return False
    try:
        evidence = turn['native_selection_evidence']
        sid = str(uuid.UUID(turn['session_id']))
        binding, lock, rollout = evidence['binding'], evidence['writer_lock'], evidence['rollout']
        return bool(evidence.get('schema') == 'ccc-official-statusline-native-writer-v1'
            and evidence.get('native_foreground_lease') is False
            and evidence['session_id'] == sid
            and binding['pid'] == turn['pid'] and binding['birth'] == turn['birth']
            and isinstance(binding.get('terminal_id'), str) and binding['terminal_id']
            and Path(lock['path']).name == sid + '.lock'
            and Path(lock['path']).parent.name == 'thread-writer-locks'
            and rollout['path'] == turn['transcript_path']
            and rollout['inode'] == turn['signature'][0]
            and all(type(item[k]) is int and item[k] > 0 for item in (lock, rollout) for k in ('device', 'inode'))
            and isinstance(evidence.get('diagnostic_id'), str) and evidence['diagnostic_id']
            and re.fullmatch(r'[0-9a-f]{64}', evidence['diagnostic_sha256'])
            and re.fullmatch(r'[0-9a-f]{64}', evidence['official_binary_sha256']))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def goal_receipt_selection(saved, current):
    """Keep old schema2 receipts unchanged; bind new UI receipts to their original proof."""
    if saved.get('native_selection_source') == 'official_statusline_native_writer':
        return bool(selected_current_thread(saved) and selected_current_thread(current)
            and current.get('native_selection_source') == 'official_statusline_native_writer'
            and current.get('native_selection_evidence') == saved.get('native_selection_evidence'))
    if saved.get('native_selection_source') in {
            'independent_optional_legacy_reader',
            'original_standalone_writer_with_stale_optional_metadata'}:
        return bool(selected_current_thread(saved) and selected_current_thread(current)
            and current.get('session_id') == saved.get('session_id')
            and current.get('pid') == saved.get('pid')
            and current.get('birth') == saved.get('birth'))
    return bool(current.get('native_foreground_status') == 'ok'
        and current.get('native_selection_schema') == 2
        and current.get('native_selection_source') == 'schema2_committed_foreground'
        and current.get('native_selection_evidence'))


def selected_goal_resume(turn, goal):
    """A stopped Goal requires the current client selection, not its startup SID."""
    return bool(goal.get('status') == 'blocked'
                and goal.get('session_id') == turn.get('session_id')
                and isinstance(goal.get('goal_id'), str) and goal['goal_id']
                and selected_current_thread(turn))


def goal_admission(endpoint, turn, goal, *, compact=False, completed_repair=None, request_attempts=None):
    import ccc_codex_goal as original
    status = goal.get('status', 'unknown')
    if status in {'absent', 'active'}:
        return True, None
    if status == 'complete':
        if completed_repair is not None:
            valid = (selected_current_thread(turn)
                and completed_request_matches(completed_repair, dict(turn, **target(endpoint)), goal, request_attempts=request_attempts))
        else:
            valid = completed_request_for(endpoint, turn, goal, attempts=request_attempts) is not None
        return (True, 'completed_goal_new_user_request') if valid else (
            False, 'completed_goal_requires_original_request_proof')
    if status == 'blocked':
        diagnostics = {}
        bound = original.blocked_goal(target(endpoint), turn['pid'],
                                      current_turn=turn, diagnostics=diagnostics)
        if (bound and bound.get('session_id') == turn.get('session_id')
                and bound.get('turn_id') == turn.get('turn_id')
                and bound.get('error') == turn.get('error')):
            return True, None
        return False, 'blocked_goal_evidence_unavailable'
    if status == 'paused' and not compact and original.ordinary_turn_after_pause(
            target(endpoint), turn, goal):
        return True, None
    return False, 'native_goal_' + status


def native_admission(endpoint, process, turn, goal, *, compact_continue=False, completed_repair=None, request_attempts=None):
    import cmux_codex_watch as original
    if (not isinstance(turn, dict) or turn.get('pid') != process['pid']
            or turn.get('birth') != process['birth'] or not turn.get('session_id')
            or not turn.get('turn_id') or turn.get('kind') != 'task_complete'
            or not terminal_proven(turn)):
        return False, 'native_failed_turn_unverified', None
    at = turn.get('at')
    born = process['birth'][0] + process['birth'][1] / 1e6
    if (type(at) not in (int, float) or not math.isfinite(at)
            or not born <= at <= time.time() + 1 or time.time() - at > 24 * 3600):
        return False, 'native_terminal_outside_process_lifecycle', None
    error = turn.get('error')
    message = error.get('message') if isinstance(error, dict) else None
    kind = original._match_error_block(message) if isinstance(message, str) else None
    if not compact_continue and error is None and goal.get('status') == 'active':
        # A successful single turn can finish while the user's native Goal is
        # still active. It is continuation authority only for the authentic
        # currently selected thread, not an old resume SID or optional reader.
        try:
            goal_id = goal.get('goal_id')
            valid_goal_id = isinstance(goal_id, str) and str(uuid.UUID(goal_id)) == goal_id
        except (ValueError, AttributeError):
            valid_goal_id = False
        if (goal.get('session_id') != turn['session_id'] or not valid_goal_id
                or not selected_current_thread(turn)
                or not isinstance(turn.get('last_agent_message'), str)
                or not turn['last_agent_message'].strip()):
            return False, 'native_active_goal_continuation_unverified', None
        return True, 'native_codex_active_goal_unfinished', None
    if compact_continue:
        if error:
            return False, 'original_compact_not_successful', kind
    elif (not isinstance(message, str)
          or kind not in original.PROVIDER_RETRYABLE | {'stream'}
          or not original._is_recovery_candidate(original.ScreenState(
              'recoverable_error', error_type=kind, message_kind='codex'))):
        return False, 'native_terminal_not_retryable', kind
    if (goal.get('status') == 'blocked' and not compact_continue
            and kind != 'invalid_encrypted_content'
            and not selected_goal_resume(turn, goal)):
        return False, 'native_blocked_goal_selection_unverified', kind
    if (compact_continue and completed_repair is not None
            and completed_repair.get('completed_goal_request') is not None
            and not completed_request_matches(completed_repair, dict(turn, **target(endpoint)), goal, request_attempts=request_attempts)):
        return False, 'original_completed_request_changed', kind
    allowed, reason = goal_admission(endpoint, turn, goal,
                                    compact=kind == 'invalid_encrypted_content' or compact_continue,
                                    completed_repair=completed_repair if compact_continue else None, request_attempts=request_attempts)
    return allowed, reason, kind


def read_current(guard, endpoint, pane, process, state, *, allow_fast=False):
    """Keep native selection first; label the original official-UI fallback."""
    import ccc_client_thread_observation as foreground
    from ccc_request_observation_policy import request_observation_directory_matches
    from ccc_herdr_selection187 import guarded_current
    original = guarded_current(process,
        lambda: _read_current(guard, endpoint, pane, process, state, allow_fast=allow_fast),
        foreground, request_observation_directory_matches,
        standalone_read=lambda: _read_stale_standalone(guard, endpoint, pane, process, state, allow_fast=allow_fast))
    turn = original[0]
    if turn.get('kind') != 'unknown':
        return original
    status, reason = turn.get('native_foreground_status'), turn.get('reason')
    # These are the original reader's two established compatibility failures.
    # A cleared/current/changed native publication or a different UNKNOWN is
    # never a reason to try another reader. The fallback rechecks its own
    # complete native veto, exact original diagnostic and writer/lifecycle.
    if (status == 'absent'
            and reason == 'Original native reader identity unavailable'):
        return _read_statusline_current(guard, endpoint, pane, process, state)
    if (status != 'invalid'
            or reason != 'Native foreground observation invalid or expired'):
        return original
    try:
        stale = foreground.stale_optional_foreground(
            process['pid'], request_observation_directory_matches)
        if stale is None:
            return original
        observed = _read_statusline_current(guard, endpoint, pane, process, state)
        if observed[0].get('kind') == 'unknown':
            return observed
        if (observed[0].get('native_selection_evidence', {}).get(
                'stale_optional_foreground') != stale):
            return ({'kind': 'unknown', 'native_foreground_status': 'invalid',
                     'reason': 'Original optional foreground proof changed during compatibility read'},
                    {'status': 'unknown'})
        return observed
    except (OSError, ValueError, TypeError, KeyError, RuntimeError,
            subprocess.SubprocessError) as error:
        return ({'kind': 'unknown', 'native_foreground_status': 'invalid',
                 'reason': 'Original compatibility reader unavailable: ' + str(error)[:160]},
                {'status': 'unknown'})


def statusline_rollout_locator(guard, root, sid, lock, files, process):
    """Resolve a selected, lock-held thread, including its closed idle rollout.

    Like the original QueueRecovery idle reader, the cache holds a path and
    inode only. Callers still re-read selection, lock, metadata, lifecycle and
    EOF; a cached terminal observation never authorizes input.
    """
    if lock.parent != root.parent / 'thread-writer-locks':
        raise ValueError('selected writer lock is outside the original Codex home')
    paths = [p for p in files if p.is_relative_to(root) and p.name.endswith('-' + sid + '.jsonl')]
    if len(paths) > 1:
        raise ValueError('selected thread has ambiguous open rollouts')
    key = (str(root), process['pid'], tuple(process['birth']), sid,
           files[lock]['device'], files[lock]['inode'])
    cache = getattr(guard, '_statusline_rollout_locators', None)
    if cache is None:
        cache = guard._statusline_rollout_locators = {}
    held = bool(paths)
    if held:
        path, identity = paths[0], files[paths[0]]
    elif key in cache:
        path, identity = cache[key]
    else:
        # The full selected UUID and held native lock are required before this
        # locator scan; never choose a newest file or a different thread.
        paths = list(root.rglob('*-' + sid + '.jsonl'))
        if len(paths) != 1:
            raise ValueError('selected idle rollout is missing or ambiguous')
        path = paths[0]
        info = path.lstat()
        identity = {'device': info.st_dev, 'inode': info.st_ino}
    info = path.lstat()
    if (path.resolve(strict=True) != path or not path.is_relative_to(root)
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or identity != {'device': info.st_dev, 'inode': info.st_ino}):
        raise ValueError('selected rollout locator identity changed')
    # This is a bounded location cache, not an identity or lifecycle cache.
    if key not in cache and len(cache) >= 96:
        cache.pop(next(iter(cache)))
    cache[key] = path, dict(identity)
    return path, identity, held


def _read_statusline_current(guard, endpoint, pane, process, state):
    import ccc_herdr_codex as codex
    import ccc_herdr_guard as transport
    import ccc_codex_queue as queue
    import ccc_client_thread_observation as foreground
    from ccc_request_observation_policy import request_observation_directory_matches
    def unknown(reason):
        return {'kind':'unknown','reason':reason,'selection_source':'official_statusline_native_writer'}, {'status':'unknown'}
    try:
        if endpoint.get('discovered_read_only') is True:
            return unknown('original endpoint is monitoring only')
        specs=[guard.guard_config.get('statusline_diagnostic'),*guard.guard_config.get('statusline_diagnostics',[])]
        specs=[s for s in specs if s and s['session']==endpoint['session'] and s['pane_id']==pane['pane_id']]
        if len(specs)!=1:
            return unknown('original observed statusline diagnostic is not registered')
        spec=specs[0]
        if transport.generation(endpoint,pane,process,spec['binding']['server'])!=spec['binding']:
            return unknown('original statusline process or terminal changed')
        source=state/('statusline-diagnostic-'+spec['id']+'.json')
        raw=source.read_bytes()
        diagnostic=json.loads(raw)
        if (diagnostic.get('spec')!=spec or diagnostic.get('phase')!='display_observed'
                or diagnostic.get('input_operations')!=1
                or diagnostic.get('native_selection_authority') is not False):
            return unknown('original UI-only diagnostic has no observed completion')
        # Never supersede an explicit live/cleared/invalid native selection.
        native_status=foreground.read_foreground(process['pid'],request_observation_directory_matches)[0]
        stale=(foreground.stale_optional_foreground(process['pid'],request_observation_directory_matches)
               if native_status=='invalid' else None)
        if native_status!='absent' and stale is None:
            return unknown('native foreground publication owns selection')
        if Path(process['executable']).resolve(strict=True)!=Path(spec['executable']):
            return unknown('original official executable changed')
        current,text=codex.statusline_binding_read(guard,endpoint,spec,require_empty=False)
        display=codex.statusline_bound_display(guard,current,text)
        if display.get('phase')!='displayed':
            return unknown('current full statusline session ID unavailable')
        sid=display['advisory_session_id']
        if diagnostic.get('display',{}).get('advisory_session_id')!=sid:
            # A user navigation requires a new explicit observation contract;
            # never transfer the diagnostic to a different selected thread.
            return unknown('original statusline thread selection changed')
        root=(Path(process['environment'].get('CODEX_HOME',str(Path.home() / '.codex')))/'sessions').resolve(strict=True)
        files=queue.process_writable_files(process['pid'],identities=True)
        locks=[p for p in files if p.parent.name=='thread-writer-locks' and p.name==sid+'.lock']
        if len(locks)!=1:
            return unknown('selected thread requires exactly one held writer lock')
        lock=locks[0]
        path,file_identity,rollout_open=statusline_rollout_locator(guard,root,sid,lock,files,process)
        def linked(p,identity):
            info=p.lstat()
            return (p.resolve(strict=True)==p and stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                    and identity=={'device':info.st_dev,'inode':info.st_ino})
        if not linked(lock,files[lock]) or not linked(path,file_identity):
            return unknown('selected writer path does not match the kernel vnode')
        start=queue.process_placement_start(process['pid'],codex.target(endpoint))
        if start!=process['birth'][0]:
            return unknown('native process placement changed')
        snapshot=queue.task_snapshot(path,sid)
        if not snapshot:
            return unknown('selected native lifecycle is incomplete or changing')
        if not rollout_open and snapshot.get('kind') not in {'task_complete','turn_aborted'}:
            return unknown('closed selected rollout has no terminal native boundary')
        turn=dict(snapshot,pid=process['pid'],birth=process['birth'],process_start=start,
                  session_id=sid,transcript_path=str(path))
        goal=(guard.goal.goal_state_for_turn(codex.target(endpoint),turn) if turn['kind']=='task_complete'
              else guard.goal.goal_state_for_open_turn(codex.target(endpoint),turn)
              if turn['kind'] in {'task_started','native_activity','user_message'} else {'status':'unknown'})
        again,text2=codex.statusline_binding_read(guard,endpoint,spec,require_empty=False)
        files2=queue.process_writable_files(process['pid'],identities=True)
        selected2={p for p in files2 if p.is_relative_to(root) and p.name.endswith('-'+sid+'.jsonl')}
        if (again!=current or codex.statusline_bound_display(guard,again,text2)!=display
                or files2.get(lock)!=files[lock] or selected2-{path}
                or (path in files2 and files2[path]!=file_identity)
                or (path not in files2 and snapshot.get('kind') not in {'task_complete','turn_aborted'})
                or not linked(lock,files[lock]) or not linked(path,file_identity)
                or queue.task_snapshot(path,sid)!=snapshot
                or source.read_bytes()!=raw
                or foreground.read_foreground(process['pid'],request_observation_directory_matches)[0]!=native_status
                or (stale is not None and foreground.stale_optional_foreground(process['pid'],request_observation_directory_matches)!=stale)):
            return unknown('selection, writer, lifecycle or original diagnostic changed during read')
        evidence={'schema':'ccc-official-statusline-native-writer-v1','diagnostic_id':spec['id'],
            'diagnostic_sha256':hashlib.sha256(raw).hexdigest(),'binding':spec['binding'],
            'session_id':sid,'writer_lock':{'path':str(lock),**files[lock]},
            'rollout':{'path':str(path),**file_identity},'native_foreground_lease':False,
            'stale_optional_foreground':stale,
            'official_binary_sha256':spec['executable_sha256']}
        turn.update(native_foreground_status='official_statusline_native_writer',
            native_selection_schema=None,native_selection_source='official_statusline_native_writer',
            native_selection_evidence=evidence,native_startup_resume_id=process.get('native_resume_id'))
        return turn,goal
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError) as error:
        return unknown(str(error)[:200])



def _current_reader(guard, endpoint, pane, process, state):
    key = (endpoint['socket'], endpoint['session'], pane['pane_id'], pane['terminal_id'])
    reader = guard.codex_readers.get(key)
    root = Path(process['environment'].get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
    if reader is None or reader.sessions_root != root:
        reader = guard.native.QueueRecovery(state / 'unused-codex-ledger.json',
                                           state / 'unused-codex-bindings.json', root, '')
        guard.codex_readers[key] = reader
    reader.process_lookup = lambda _: {'agent_kind': 'codex', 'agent_pids': [process['pid']]}
    return reader


_GOALS_DB_CACHE = {'path': None, 'conn': None, 'stamp': None, 'rows': {}}


def _stat_pin(path):
    try:
        st = Path(path).lstat()
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
    except OSError:
        return None


def _thread_goal_pin(goals_db, session_id):
    if not isinstance(session_id, str) or not session_id:
        return None
    db_pin = _stat_pin(goals_db)
    if db_pin is None:
        return ('absent',)
    wal_pin = _stat_pin(goals_db.with_name(goals_db.name + '-wal'))
    stamp = (str(goals_db), db_pin, wal_pin)
    if _GOALS_DB_CACHE['stamp'] != stamp:
        try:
            conn = _GOALS_DB_CACHE['conn']
            if conn is None or _GOALS_DB_CACHE['path'] != (str(goals_db), db_pin[0], db_pin[1]):
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                conn = sqlite3.connect(goals_db.as_uri() + '?mode=ro', uri=True, timeout=0.5)
                _GOALS_DB_CACHE['conn'] = conn
                _GOALS_DB_CACHE['path'] = (str(goals_db), db_pin[0], db_pin[1])
            rows = {}
            for tid, gid, st_val, upd in conn.execute(
                    'SELECT thread_id, goal_id, status, updated_at_ms FROM thread_goals'):
                rows[tid] = rows.get(tid, ()) + ((gid, st_val, upd),)
            _GOALS_DB_CACHE['rows'] = rows
            _GOALS_DB_CACHE['stamp'] = stamp
        except sqlite3.Error:
            return None
    return ('present', _GOALS_DB_CACHE['rows'].get(session_id, ()))


def _fast_observation_pin(guard, process, turn):
    if not isinstance(turn, dict) or turn.get('kind') == 'unknown' or not turn.get('transcript_path'):
        return None
    try:
        files = guard.native.process_writable_files(process['pid'], identities=True)
        tpath = Path(turn['transcript_path'])
        if tpath.resolve(strict=True) != tpath:
            return None
        home = Path(process.get('environment', {}).get('CODEX_HOME', str(Path.home() / '.codex')))
        goals_pin = _thread_goal_pin(home / 'goals_1.sqlite', turn.get('session_id'))
        if goals_pin is None:
            return None
        return {
            'pid': process['pid'],
            'birth': list(process['birth']),
            'files': sorted((str(k), v['device'], v['inode']) for k, v in files.items()),
            'rollout': _stat_pin(tpath),
            'thread_goal': goals_pin,
        }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _read_stale_standalone(guard, endpoint, pane, process, state, *, allow_fast=False):
    cache = getattr(guard, '_fast_stale_standalone_cache', None)
    if cache is None:
        cache = guard._fast_stale_standalone_cache = {}
    ckey = (endpoint['socket'], endpoint['session'], pane['pane_id'], pane['terminal_id'])
    if allow_fast and ckey in cache:
        saved_pin, saved_turn = cache[ckey]
        now_pin = _fast_observation_pin(guard, process, saved_turn)
        if now_pin is not None and now_pin == saved_pin:
            return copy.deepcopy(saved_turn)
    reader = _current_reader(guard, endpoint, pane, process, state)
    turn = reader._stale_foreground_standalone_turn(target(endpoint), process['pid'])
    if turn is not None and turn.get('kind') != 'unknown':
        pin = _fast_observation_pin(guard, process, turn)
        if pin is not None:
            cache[ckey] = (pin, copy.deepcopy(turn))
    else:
        cache.pop(ckey, None)
    return turn


def _read_current(guard, endpoint, pane, process, state, *, allow_fast=False):
    """Use the complete original native reader, scoped to one actual inner PID."""
    cache = getattr(guard, '_fast_current_turn_cache', None)
    if cache is None:
        cache = guard._fast_current_turn_cache = {}
    ckey = (endpoint['socket'], endpoint['session'], pane['pane_id'], pane['terminal_id'])
    if allow_fast and ckey in cache:
        saved_pin, saved_turn, saved_goal = cache[ckey]
        now_pin = _fast_observation_pin(guard, process, saved_turn)
        if now_pin is not None and now_pin == saved_pin:
            return copy.deepcopy(saved_turn), copy.deepcopy(saved_goal)
    reader = _current_reader(guard, endpoint, pane, process, state)
    turn = reader._open_process_turn(target(endpoint), fresh=True)
    if (not isinstance(turn, dict) or not turn.get('session_id')
            or turn.get('pid') != process['pid'] or turn.get('birth') != process['birth']):
        cache.pop(ckey, None)
        reason = turn.get('reason') if isinstance(turn, dict) else None
        return {'kind': 'unknown', 'reason': reason or 'Original native reader identity unavailable'}, {'status': 'unknown'}
    goal = (guard.goal.goal_state_for_turn(target(endpoint), turn)
            if turn.get('kind') == 'task_complete' else
            guard.goal.goal_state_for_open_turn(target(endpoint), turn)
            if turn.get('kind') in {'task_started', 'native_activity', 'user_message'} else {'status': 'unknown'})
    pin = _fast_observation_pin(guard, process, turn)
    if pin is not None and goal.get('status') != 'unknown':
        cache[ckey] = (pin, copy.deepcopy(turn), copy.deepcopy(goal))
    else:
        cache.pop(ckey, None)
    return turn, goal


def compact_monitor_signature(signature, info):
    """An append may advance a sample; replacement and in-place edits may not."""
    return (isinstance(signature, list) and len(signature) == 3
            and all(type(v) is int and v >= 0 for v in signature)
            and signature[0] == info.st_ino
            and info.st_size >= signature[1]
            and (info.st_size > signature[1] or info.st_mtime_ns == signature[2]))


def compact_monitor_same_turn(before, after):
    # Both samples come from read_current(), including its original process,
    # native selection and writer checks. A growing signature is not selection.
    required = ('pid', 'birth', 'session_id', 'transcript_path')
    if (not isinstance(after, dict) or before.get('kind') == 'unknown'
            or after.get('kind') == 'unknown'
            or any(not before.get(k) or after.get(k) != before[k] for k in required)):
        return False
    # Native activity rows intentionally omit turn_id. The complete incremental
    # lifecycle supplies an observation clock below, never an RPC active turn.
    if (before.get('turn_id') and after.get('turn_id')
            and before['turn_id'] != after['turn_id']):
        return False
    return all(before.get(k) == after.get(k) for k in (
        'native_foreground_status', 'native_selection_schema',
        'native_selection_source', 'native_selection_evidence', 'shared_writer'))


def compact_monitor_same_goal(before, after):
    # Usage and update timestamps may advance during observation. Goal identity
    # and stop/budget status still fence the sample. This is never control proof.
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    return all(before.get(k) == after.get(k) for k in (
        'session_id', 'goal_id', 'status', 'objective', 'token_budget', 'tokenBudget'))


def compact_control_availability(row, endpoint, config, *, native_capability):
    """Report the original v4 contract conflict without projecting its policy."""
    result = {'mode': 'read_only', 'implemented': False,
              'native_capability_verified': native_capability is True,
              'control_verified': False, 'recovery_available': False,
              'input_operations': 0, 'restart_operations': 0}
    if config.get('mode') != 'armed' or config.get('global_paused'):
        return dict(result, reason='current_policy_paused')
    if not row.get('native_reader_available'):
        return dict(result, reason='current_native_thread_unbound')
    if native_capability is not True:
        return dict(result, reason='native_conditional_capability_unavailable')
    if endpoint['cmux_surface_id'] in config.get('discovery_excluded_surface_ids', []):
        return dict(result, reason='native_v4_rejects_required_outer_input_isolation')
    # The required outer isolation must not be removed to make an old native
    # contract pass. Even a malformed configuration never enrolls a controller.
    return dict(result, reason='required_outer_input_isolation_missing')



def compact_observation_clock(owned, name, old, session_id, facts, compact):
    """Retain closed read-only history when a fully re-read file is replaced.

    A new file never inherits an active clock, watermark or control budget.
    Caller must still authenticate the current reader/lifecycle and complete
    EOF. Only an idle clock for this exact session and canonical path can be
    archived; write the exact old bytes before publishing any new clock.
    """
    from ccc_compact_watchdog import read_only_root_clock
    scope = dict(session_id=session_id, path=facts['path'], file_id=facts['file_id'])
    if old is None or old.get('scope') == scope:
        return read_only_root_clock(old, session_id, facts, compact)
    previous = old.get('scope') or {}
    file_id = previous.get('file_id')
    current_id = scope['file_id']
    if (old.get('schema') != 'read-only-root-clock-v1'
            or previous.get('session_id') != session_id
            or previous.get('path') != facts['path']
            or not isinstance(file_id, list) or len(file_id) != 2
            or not isinstance(current_id, list) or len(current_id) != 2
            or not all(type(n) is int and n > 0 for n in file_id + current_id)
            or old.get('phase') != 'idle' or old.get('episode', True) is not None
            or old.get('control_verified') is not False
            or old.get('recovery_enabled') is not False):
        # In particular, an unresolved clock is not reset after file rotation.
        return read_only_root_clock(old, session_id, facts, compact)
    clock = read_only_root_clock(None, session_id, facts, compact)
    raw = owned.read_bytes(name, limit=64 * 1024)
    if json.loads(raw) != old:
        raise ValueError('Original idle clock changed before archival')
    pin = hashlib.sha256(raw).hexdigest()
    archive = name[:-5] + '.retired-idle-' + pin + '.json'
    try:
        owned.write_once_bytes(archive, raw)
    except FileExistsError:
        if owned.read_bytes(archive, limit=64 * 1024) != raw:
            raise ValueError('Original idle clock archive differs')
    clock['prior_idle_clock'] = dict(archive=archive, sha256=pin, scope=previous)
    return clock


def compact_observation(guard, row, state, config):
    """Preserve the original read-only clock across ordinary rollout appends.

    Authenticate the original current reader twice, then consume only complete
    incremental facts from the same inode and original lifecycle. No input,
    intent, provider budget, pause projection or native capability is invented.
    """
    result = {'mode': 'read_only', 'phase': 'unavailable',
              'control_verified': False, 'recovery_available': False,
              'warning_seconds': 180, 'recovery_seconds': 300,
              'observed_at': time.time()}
    result['conditional_control'] = {
        'mode': 'read_only', 'implemented': False, 'control_verified': False,
        'recovery_available': False, 'native_capability_verified': False,
        'input_operations': 0, 'restart_operations': 0,
        'reason': 'current_native_thread_unbound'}
    options = config.get('codex_compact_watchdog', {})
    if not isinstance(options, dict) or options.get('enabled') is not True:
        return dict(result, phase='disabled')
    if not row.get('native_reader_available'):
        return dict(result, reason='current_native_thread_unbound')
    try:
        from ccc_compact_sensor import RolloutFacts, unsettled_compaction
        from ccc_compact_watchdog import read_only_root_clock
        from ccc_compact_support import OwnedDirectory
        import ccc_herdr_guard as transport
        endpoint = transport.endpoint_for(guard.config, row['session'])
        proof = guard.codex_sources.get((endpoint['socket'], row['pane_id']))
        selected = copy.deepcopy((proof or {}).get('turn') or {})
        if (selected.get('kind') == 'unknown' or not selected.get('session_id')
                or not selected.get('transcript_path')
                or selected.get('pid') != row['pid'] or selected.get('birth') != row['birth']):
            raise ValueError('Current native compaction reader changed')
        from ccc_client_thread_observation import read_compact_capability
        from ccc_request_observation_policy import request_observation_directory_matches
        status, sid, _evidence, capable = read_compact_capability(
            row['pid'], request_observation_directory_matches)
        result['conditional_control'] = compact_control_availability(
            row, endpoint, config, native_capability=(
                status == 'ok' and sid == selected['session_id'] and capable is True))
        path = Path(selected['transcript_path'])
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or path != path.resolve(strict=True)
                or not compact_monitor_signature(selected.get('signature'), info)):
            raise ValueError('Current compaction rollout replaced or edited')
        identity = (row['pid'], tuple(row['birth']), row['terminal_id'],
                    selected['session_id'], str(path), info.st_dev, info.st_ino)
        cache = getattr(guard, '_inner_compact_observers', None)
        if cache is None:
            guard._inner_compact_observers = cache = {}
        key = row['key']
        if key not in cache or cache[key]['identity'] != identity:
            cache[key] = {'identity': identity, 'facts': RolloutFacts(path)}
        facts_reader = cache[key]['facts']
        facts = facts_reader.refresh(max_bytes=16 * 1024 * 1024)
        if not facts['caught_up']:
            return dict(result, phase='scanning', scanned_bytes=facts['offset'],
                        total_bytes=path.lstat().st_size, original_clock_retained=True,
                        reason='waiting_for_complete_native_records')
        if facts['session_id'] != selected['session_id'] or facts['lifecycle_error']:
            raise ValueError('Current native compaction lifecycle is unbound')
        current_process = guard.process(row['pid'])
        if (current_process.get('pid') != row['pid']
                or current_process.get('birth') != row['birth']):
            raise ValueError('Current native compaction process changed')
        settled_turn = (facts.get('observed_turn') or {}).get('status') != 'started_without_end_record'
        if settled_turn and selected.get('signature') == [info.st_ino, info.st_size, info.st_mtime_ns]:
            latest, latest_goal = selected, copy.deepcopy(proof['goal'])
        else:
            latest, latest_goal = read_current(guard, endpoint,
                {'pane_id': row['pane_id'], 'terminal_id': row['terminal_id']},
                current_process, state, allow_fast=True)
        if (not compact_monitor_same_turn(selected, latest)
                or not compact_monitor_same_goal(proof['goal'], latest_goal)):
            raise ValueError('Current native selection, writer, lifecycle or Goal changed')
        # Include append-only activity that occurred while the second native
        # reader ran. Incomplete bytes never close or reset the original clock.
        facts = facts_reader.refresh(max_bytes=16 * 1024 * 1024)
        later = path.lstat()
        if ((later.st_dev, later.st_ino) != (info.st_dev, info.st_ino)
                or later.st_size < info.st_size
                or not compact_monitor_signature(latest.get('signature'), later)):
            raise ValueError('Current native rollout replaced, truncated or edited')
        if not facts['caught_up']:
            return dict(result, phase='scanning', scanned_bytes=facts['offset'],
                        total_bytes=later.st_size, original_clock_retained=True,
                        reason='waiting_for_complete_native_records')
        if (facts['session_id'] != selected['session_id'] or facts['lifecycle_error']
                or any(sample.get('turn_id') and
                       (facts.get('observed_turn') or {}).get('id') != sample['turn_id']
                       for sample in (selected, latest))):
            raise ValueError('Current native lifecycle advanced during observation')
        compact = unsettled_compaction(selected['session_id'], facts)
        # A log query may outlive another append. Retain the old clock instead
        # of committing a sample whose complete native EOF is no longer current.
        final_facts = facts_reader.snapshot()
        if (not final_facts['caught_up'] or final_facts['offset'] != facts['offset']):
            return dict(result, phase='scanning', scanned_bytes=final_facts['offset'],
                        total_bytes=path.lstat().st_size, original_clock_retained=True,
                        reason='native_rollout_advanced_after_sample')
        directory = state / 'codex-panels' / life.digest(key.encode()) / 'compact-observation'
        name = life.digest(selected['session_id'].encode()) + '.json'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with OwnedDirectory(directory) as owned:
            try:
                old = json.loads(owned.read_bytes(name, limit=64 * 1024))
            except FileNotFoundError:
                old = None
            clock = compact_observation_clock(owned, name, old, selected['session_id'], facts, compact)
            observed = facts.get('observed_turn') or {}
            episode = clock.get('episode')
            if (episode and observed.get('status') in {'task_complete', 'turn_aborted'}
                    and observed.get('ended_at', 0) >= episode['root_started_at']):
                clock['last_closed'] = dict(episode, reason=observed['status'],
                                            boundary_at=observed['ended_at'])
                clock['episode'] = None
                clock['phase'] = 'idle'
            if clock != old:
                owned.atomic_json(name, clock)
        episode = clock.get('episode')
        if episode:
            elapsed = result['observed_at'] - episode['root_started_at']
            if not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError('Native compact observation clock is in the future')
            phase = ('deadline_exceeded' if elapsed >= 300 else
                     'warning' if elapsed >= 180 else 'observing')
            return dict(result, phase=phase, elapsed_seconds=elapsed,
                        started_at=episode['root_started_at'], episode_key=episode['key'],
                        native_session_id=selected['session_id'],
                        current_start_observed=compact is not None,
                        reason='native_compact_observation_only')
        return dict(result, phase='idle', native_session_id=selected['session_id'],
                    last_closed=clock.get('last_closed'))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError,
            sqlite3.Error, subprocess.SubprocessError) as error:
        return dict(result, phase='unavailable', reason=str(error)[:160])


def reader_observation(guard, row, state):
    """Keep a diagnostic clock for a bound client with an unavailable reader.

    This clock carries no SID, input intent or recovery authority.  Each actual
    terminal/process/server generation retains its own original observation.
    """
    now = time.time()
    result = {'mode': 'read_only', 'phase': 'unavailable', 'observed_at': now,
              'warning_seconds': 180, 'escalation_seconds': 300,
              'control_verified': False, 'recovery_available': False,
              'attention_required': False}
    if row.get('agent') != 'codex' or row.get('process_bound') is not True:
        return dict(result, reason='current_process_unbound')
    try:
        from ccc_compact_support import OwnedDirectory
        fields = ('key', 'session', 'pane_id', 'terminal_id', 'pid', 'birth',
                  'server', 'cmux_workspace_id', 'cmux_surface_id')
        binding = {name: row[name] for name in fields}
        if (not all(isinstance(binding[name], str) and binding[name]
                    for name in ('key', 'session', 'pane_id', 'terminal_id',
                                 'cmux_workspace_id', 'cmux_surface_id'))
                or type(binding['pid']) is not int or binding['pid'] <= 0
                or not isinstance(binding['birth'], list) or len(binding['birth']) != 2
                or any(type(part) is not int for part in binding['birth'])
                or not isinstance(binding['server'], dict)
                or type(now) not in (int, float) or not math.isfinite(now)):
            raise ValueError('Current reader observation binding is incomplete')
        before = guard.process(row['pid'])
        if (not isinstance(before, dict) or before.get('pid') != row['pid']
                or before.get('birth') != row['birth']):
            raise ValueError('Current reader observation process changed')
        result['binding'] = binding
        directory = state / 'codex-panels' / life.digest(row['key'].encode()) / 'reader-observation'
        name = life.digest(life.canonical(binding)) + '.json'
        available = row.get('native_reader_available') is True
        if available and not directory.exists():
            return dict(result, phase='bound', reason='current_native_reader_bound')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with OwnedDirectory(directory) as owned:
            try:
                old = json.loads(owned.read_bytes(name, limit=64 * 1024))
            except FileNotFoundError:
                old = None
            if old is not None and (not isinstance(old, dict) or old.get('schema') != 1
                                    or old.get('binding') != binding):
                raise ValueError('Original reader observation clock is unbound')
            clock = dict(old) if old is not None else {'schema': 1, 'binding': binding,
                                                       'episode': None}
            episode = clock.get('episode')
            if episode is not None:
                start = episode.get('first_unbound_at') if isinstance(episode, dict) else None
                if (type(start) not in (int, float) or not math.isfinite(start)
                        or start <= 0 or start > now):
                    raise ValueError('Original reader observation clock is invalid')
            if available:
                if episode is not None:
                    clock.update(episode=None, last_closed=dict(episode,
                        ended_at=now, reason='current_native_reader_bound'))
            elif episode is None:
                clock['episode'] = {'first_unbound_at': now}
            after = guard.process(row['pid'])
            if after != before:
                raise ValueError('Current process changed during reader observation')
            if clock != old and not (available and old is None):
                owned.atomic_json(name, clock)
        if available:
            return dict(result, phase='bound', reason='current_native_reader_bound',
                        last_closed=clock.get('last_closed'))
        start = clock['episode']['first_unbound_at']
        elapsed = now - start
        phase = ('deadline_exceeded' if elapsed >= 300 else
                 'warning' if elapsed >= 180 else 'observing')
        return dict(result, phase=phase, started_at=start, elapsed_seconds=elapsed,
                    attention_required=elapsed >= 180,
                    reason='current_native_reader_unbound')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError,
            sqlite3.Error, subprocess.SubprocessError) as error:
        return dict(result, phase='unavailable', reason=str(error)[:160])


def selection_observation(guard, row, state):
    """Track full current-selection readiness separately from a legacy reader.

    A legacy reader may bind briefly while its selected-thread publication is
    still absent.  It must not reset this diagnostic clock.  The original
    reader, native input admission, counters and recovery authority stay intact.
    """
    selected = bool(row.get('native_reader_available') is True
                    and row.get('native_foreground_observation') == 'ok'
                    and type(row.get('native_selection_schema')) is int
                    and row['native_selection_schema'] == 2
                    and row.get('native_selection_source') == 'schema2_committed_foreground'
                    and row.get('native_selection_evidence'))
    selected = (selected or row.get('official_selection_verified') is True
                or row.get('current_selection_verified') is True)
    observed = dict(row, native_reader_available=selected)
    result = reader_observation(guard, observed, state / 'selection-observation')
    return dict(result, purpose='current_selection',
                current_selection_verified=selected,
                legacy_reader_available=row.get('native_reader_available') is True)


def panel_control_coverage(row, endpoint, config, *, service_armed):
    """Describe the observed original gates; never enroll or authorize input.

    A discovered panel may have a perfectly good current reader while its
    original controller still has no input enrollment. Conversely, an enabled
    rule is not proof that an unavailable reader can recover that panel.
    """
    result = {'schema': 'ccc-herdr-panel-coverage-r43',
              'observation_mode': 'read_only', 'input_enrolled': False,
              'input_eligible': False, 'current_reader_bound': False,
              'current_selection_verified': False,
              'native_conditional_control_available': False,
              'original_terminal_respawn_implemented': False,
              'receipt_observed': False, 'state': 'unavailable'}
    if row.get('agent') != 'codex' or row.get('process_bound') is not True:
        return dict(result, reason='current_process_unbound')
    result['current_reader_bound'] = row.get('native_reader_available') is True
    result['current_selection_verified'] = bool(
        result['current_reader_bound']
        and row.get('native_foreground_observation') == 'ok'
        and type(row.get('native_selection_schema')) is int
        and row['native_selection_schema'] == 2
        and row.get('native_selection_source') == 'schema2_committed_foreground'
        and row.get('native_selection_evidence'))
    result['current_selection_verified'] = (
        result['current_selection_verified']
        or row.get('official_selection_verified') is True
        or row.get('current_selection_verified') is True)
    result['selection_proof_source'] = row.get('native_selection_source')
    # This is the original row's receipt count, not a new delivery assertion.
    count = row.get('native_receipts')
    if type(count) is int and count >= 0:
        result.update(native_receipts=count, receipt_observed=count > 0)
    monitor = row.get('native_compaction_monitor')
    if isinstance(monitor, dict):
        result['native_conditional_control_available'] = bool(
            monitor.get('control_verified') is True
            and monitor.get('recovery_available') is True
            and isinstance(monitor.get('conditional_control'), dict)
            and monitor['conditional_control'].get('control_verified') is True
            and monitor['conditional_control'].get('recovery_available') is True)
    try:
        enabled, reason = policy(config, endpoint)
    except (TypeError, ValueError, KeyError) as error:
        return dict(result, reason='original_policy_unavailable: ' + str(error)[:120])
    if endpoint.get('discovered_read_only') is True:
        return dict(result, state='monitor_only', reason='discovered_endpoint_monitor_only')
    if service_armed is not True or not enabled:
        return dict(result, state='paused', reason=reason or 'original_service_not_armed')
    result['input_enrolled'] = True
    if not result['current_reader_bound']:
        return dict(result, state='reader_unavailable', reason=row.get('native_reader_reason')
                    or 'current_native_thread_unbound')
    if row.get('submit_phase') in {'intent', 'submitted', 'write_unknown', 'receipt_unknown'}:
        return dict(result, state='input_pending', reason='original_input_pending_or_unknown')
    if row.get('dialog_observed') is True or row.get('provider_reconnecting_observed') is True:
        return dict(result, state='input_held', reason='original_dialog_or_provider_retry')
    if row.get('continuation_eligible') is True and row.get('auto_input_enabled') is True:
        return dict(result, state='eligible', reason=row.get('recovery_reason')
                    or 'original_current_admission_passed', input_eligible=True)
    if (row.get('native_goal') or {}).get('status') == 'blocked' and not result['current_selection_verified']:
        return dict(result, state='selection_unavailable',
                    reason='native_blocked_goal_selection_unverified')
    return dict(result, state='waiting', reason=row.get('recovery_reason')
                or 'original_recoverable_episode_not_observed')


def fleet_control_coverage(rows):
    """Count actual per-panel observations, independently of global rule flags."""
    selected = [row for row in rows if row.get('agent') == 'codex']
    observations = [row.get('control_coverage') or {} for row in selected]
    states = {}
    for observed in observations:
        state = observed.get('state', 'unavailable')
        states[state] = states.get(state, 0) + 1
    return {'schema': 'ccc-herdr-fleet-coverage-r43',
            'codex_panels': len(selected), 'states': states,
            'input_enrolled_panels': sum(row.get('input_enrolled') is True for row in observations),
            'input_eligible_panels': sum(row.get('input_eligible') is True for row in observations),
            'reader_bound_panels': sum(row.get('current_reader_bound') is True for row in observations),
            'selected_thread_verified_panels': sum(row.get('current_selection_verified') is True for row in observations),
            'native_conditional_control_panels': sum(row.get('native_conditional_control_available') is True for row in observations),
            'observation_only': True, 'input_operations': 0,
            'all_panels_automatically_recoverable': False,
            'original_terminal_respawn_implemented': False}


def build_guard_class(base):
    class CodexGuard(base):
        def __init__(self, config):
            super().__init__(config)
            self.codex_readers, self.codex_sources = {}, {}
            from ccc_provider_retry import ProviderRetryStore
            self.codex_provider_retry = ProviderRetryStore(
                Path(config['ccc_config']).parent / 'codex-provider-retry.sqlite3')

        def codex_turn(self, endpoint, pane, process, state):
            turn, goal = read_current(self, endpoint, pane, process, state, allow_fast=True)
            key = (endpoint['socket'], pane['pane_id'])
            self.codex_sources[key] = {'turn': turn, 'goal': goal}
            if turn.get('kind') == 'unknown':
                return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                        'native_reader_reason': turn.get('reason') or 'Current native thread/reader is unbound; startup session is only a locator',
                        'native_foreground_observation': turn.get('native_foreground_status'),
                        'native_goal': goal, 'continuation_eligible': False,
                        'recovery_reason': 'native_thread_unbound'}
            _, _, error = native_admission(endpoint, process, turn, goal, request_attempts=request_attempts(state))
            verified_selection = selected_current_thread(turn)
            return {'native_turn_kind': turn['kind'], 'native_reader_available': True,
                    'native_session_id': turn['session_id'], 'native_turn_id': turn.get('turn_id'),
                    'native_at': turn.get('at'), 'native_signature': turn.get('signature'),
                    'native_error_type': error,
                    'native_error_message': str((turn.get('error') or {}).get('message', ''))[:700],
                    'native_goal': goal,
                    'native_foreground_observation': turn.get('native_foreground_status'),
                    'native_selection_source': turn.get('native_selection_source'),
                    'current_selection_verified': verified_selection,
                    'official_selection_verified': (turn.get('native_selection_source') == 'official_statusline_native_writer' and verified_selection),
                    'native_selection_schema': turn.get('native_selection_schema'),
                    'native_selection_evidence': turn.get('native_selection_evidence'),
                    'native_startup_resume_id': turn.get('native_startup_resume_id'),
                    'native_proof_sha256': life.digest(life.canonical({'turn': turn, 'goal': goal}))}

        def capture(self, state):
            result = super().capture(state)
            config = json.loads(Path(self.guard_config['ccc_config']).read_bytes())
            shared_attempts = None
            for row in result['agents']:
                if row.get('agent') != 'codex' or not row.get('process_bound'):
                    continue
                import ccc_herdr_guard as transport
                endpoint = transport.endpoint_for(self.config, row['session'])
                proof = self.codex_sources.get((endpoint['socket'], row['pane_id']))
                enabled, policy_reason = policy(config, endpoint)
                try:
                    if not proof or proof['turn'].get('kind') == 'unknown':
                        raise ValueError('native_thread_unbound')
                    if shared_attempts is None:
                        shared_attempts = request_attempts(state)
                    allowed, reason, _ = native_admission(endpoint, self.process(row['pid']),
                                                        proof['turn'], proof['goal'], request_attempts=shared_attempts)
                    text = self.screens.get((row['session'], row['pane_id']), '')
                    empty, screen_reason = screen_admission(text)
                    row.update(continuation_eligible=bool(enabled and allowed and empty),
                               recovery_reason=policy_reason if not enabled else reason if not allowed else screen_reason if not empty
                                               else reason or 'native_codex_failed_turn',
                               auto_input_enabled=bool(self.guard_config.get('armed') and enabled),
                               dialog_observed=not empty and screen_reason == 'dialog_or_queued_input',
                               codex_iec_request_respawn_available=False)
                except (OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
                    row.update(continuation_eligible=False, auto_input_enabled=False,
                               recovery_reason=str(error)[:160])
                row['native_compaction_monitor'] = compact_observation(self, row, state, config)
                # Observation is independent of enrollment, pause and input.
                # These diagnostics create no provider intent or authority.
                from ccc_herdr_native_query import observe_ingress
                row['native_ingress_health'] = observe_ingress(self, row)
                row['native_reader_health'] = reader_observation(self, row, state)
                row['native_selection_health'] = selection_observation(self, row, state)
                row['control_coverage'] = panel_control_coverage(
                    row, endpoint, config, service_armed=self.guard_config.get('armed'))
            armed = bool(self.guard_config.get('armed') and config.get('mode') == 'armed')
            result['active_rules'].update(codex_error_continuation=armed,
                                         codex_continuation=armed,
                                         codex_goal_continuation=armed,
                                         codex_native_compact_recovery=armed,
                                         codex_original_terminal_respawn=False)
            result['all_continuation_rules_active'] = False
            result['codex_reader_attention_required'] = sum(
                row.get('native_reader_health', {}).get('attention_required') is True
                for row in result['agents'])
            result['codex_selection_attention_required'] = sum(
                row.get('native_selection_health', {}).get('attention_required') is True
                for row in result['agents'])
            result['codex_control_coverage'] = fleet_control_coverage(result['agents'])
            from ccc_herdr_native_query import fleet_ingress_health
            result['codex_native_ingress_health'] = fleet_ingress_health(result['agents'])
            return result
    return CodexGuard


def verify_original(guard, endpoint, pane, process, server, proof, state, *, compact_continue=False, completed_repair=None):
    import ccc_herdr_guard as transport
    if process['pid'] == transport.PROTECTED_PID or endpoint['cmux_surface_id'] == transport.PROTECTED_SURFACE:
        raise ValueError('Protected session is outside all Herdr input scope')
    transport.verify_membership(guard, endpoint, server)
    snapshot = guard.cli(endpoint['session'], ['api', 'snapshot'])['snapshot']
    matches = [p for p in snapshot['panes'] if p['pane_id'] == pane['pane_id']]
    if (len(matches) != 1 or matches[0].get('agent') != 'codex'
            or any(matches[0].get(k) != pane.get(k) for k in ('terminal_id', 'workspace_id', 'tab_id'))):
        raise ValueError('Original Codex pane changed or requires user input')
    current = guard.foreground(endpoint, matches[0], server)
    fields = ('pid', 'birth', 'pgid', 'tty', 'argv', 'environment', 'executable')
    if any(current.get(k) != process.get(k) for k in fields):
        raise ValueError('Original Herdr foreground process changed')
    turn, goal = read_current(guard, endpoint, matches[0], current, state)
    if proof != {'turn': turn, 'goal': goal}:
        raise ValueError('Original Codex turn or Goal changed')
    allowed, reason, _ = native_admission(endpoint, current, turn, goal, compact_continue=compact_continue, completed_repair=completed_repair, request_attempts=request_attempts(state))
    text = guard.cli(endpoint['session'], ['pane', 'read', pane['pane_id'],
                                         '--source', 'visible', '--lines', '160'])['text']
    empty, reason_screen = screen_admission(text)
    config = json.loads(Path(guard.guard_config['ccc_config']).read_bytes())
    enabled, reason_policy = policy(config, endpoint)
    if not enabled or not allowed or not empty:
        raise ValueError(reason_policy or reason or reason_screen)
    transport.verify_membership(guard, endpoint, server)
    after = guard.foreground(endpoint, matches[0], server)
    if any(after.get(k) != process.get(k) for k in fields):
        raise ValueError('Original foreground changed during final input checks')
    later, later_goal = read_current(guard, endpoint, matches[0], after, state)
    if proof != {'turn': later, 'goal': later_goal}:
        raise ValueError('Original native source changed during final input checks')
    return config


def fence(turn):
    path = Path(turn['transcript_path'])
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_mode & 0o022 or path.resolve(strict=True) != path):
        raise ValueError('Original native transcript path refused')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        header = stream.readline()
        if json.loads(header).get('payload', {}).get('id') != turn['session_id']:
            raise ValueError('Original native transcript header changed')
        start = max(0, info.st_size - 4096)
        stream.seek(start)
        tail = stream.read()
    after = path.stat()
    if ([info.st_ino, info.st_size, info.st_mtime_ns] != turn['signature']
            or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
               (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or not tail.endswith(b'\n')):
        raise ValueError('Original native EOF changed')
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
            'size': info.st_size, 'header_bytes': len(header),
            'header_sha256': life.digest(header), 'tail_start': start,
            'tail_sha256': life.digest(tail)}


def receipt(attempt, *, current_proof=None):
    goal_resume = attempt.get('route') == 'goal_resume'
    if goal_resume:
        # Slash control has no ordinary user-message receipt. It needs the
        # same selected Goal plus a new native Goal-triggered start at the
        # original EOF. Missing selection leaves the original attempt pending.
        current_proof = current_proof or {}
        turn, goal = current_proof.get('turn') or {}, current_proof.get('goal') or {}
        saved_turn, saved_goal = attempt['source']['turn'], attempt['source']['goal']
        if (turn.get('pid') != saved_turn['pid'] or turn.get('birth') != saved_turn['birth']
                or turn.get('session_id') != saved_turn['session_id']
                or turn.get('transcript_path') != attempt['fence']['path']
                or not goal_receipt_selection(saved_turn, turn)
                or goal.get('session_id') != saved_turn['session_id']
                or goal.get('goal_id') != saved_goal.get('goal_id')
                or goal.get('status') not in {'active', 'blocked', 'complete'}
                or type(goal.get('updated_at_ms')) is not int
                or goal['updated_at_ms'] <= saved_goal.get('updated_at_ms', 0)):
            return None
    source = attempt['fence']
    path = Path(source['path'])
    first = path.lstat()
    if (not stat.S_ISREG(first.st_mode) or first.st_uid != os.getuid()
            or first.st_mode & 0o022):
        raise ValueError('Original transcript path changed')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (source['device'], source['inode']) or info.st_size < source['size']:
            raise ValueError('Original transcript replaced or truncated')
        if life.digest(stream.read(source['header_bytes'])) != source['header_sha256']:
            raise ValueError('Original transcript header changed')
        stream.seek(source['tail_start'])
        if life.digest(stream.read(source['size'] - source['tail_start'])) != source['tail_sha256']:
            raise ValueError('Original native input fence changed')
        if goal_resume and attempt.get('native_goal_update'):
            update = attempt['native_goal_update']
            if (not isinstance(update, dict) or update.get('session_id') != saved_turn['session_id']
                    or type(update.get('offset')) is not int or type(update.get('bytes')) is not int
                    or not source['size'] <= update['offset'] < info.st_size
                    or not 0 < update['bytes'] <= 256 * 1024
                    or update['offset'] + update['bytes'] > info.st_size):
                raise ValueError('Original native Goal update boundary changed')
            stream.seek(update['offset'])
            if life.digest(stream.read(update['bytes'])) != update.get('sha256'):
                raise ValueError('Original native Goal update changed')
        cursor = attempt.get('receipt_cursor', source['size'])
        anchor = attempt.get('receipt_cursor_anchor')
        if type(cursor) is not int or not source['size'] <= cursor <= info.st_size:
            raise ValueError('Original receipt cursor changed')
        if cursor != source['size']:
            if (not isinstance(anchor, dict) or type(anchor.get('offset')) is not int
                    or type(anchor.get('bytes')) is not int or anchor['bytes'] <= 0
                    or anchor['offset'] < source['size']
                    or anchor['offset'] + anchor['bytes'] != cursor):
                raise ValueError('Original receipt cursor has no record-boundary proof')
            stream.seek(anchor['offset'])
            if life.digest(stream.read(anchor['bytes'])) != anchor['sha256']:
                raise ValueError('Original receipt cursor boundary changed')
        stream.seek(cursor)
        consumed = 0
        while consumed < MAX_RECEIPT:
            offset = stream.tell()
            raw = stream.readline(MAX_RECEIPT - consumed + 1)
            if not raw:
                return None
            if len(raw) + consumed > MAX_RECEIPT:
                if consumed == 0:
                    raise ValueError('Original native record exceeds receipt observation bound')
                return None
            if not raw.endswith(b'\n'):
                # A writer may still be appending this record. Retain its start;
                # neither partial EOF nor a full scan budget is non-reception.
                return None
            consumed += len(raw)
            row = json.loads(raw)
            payload = row.get('payload')
            if not isinstance(payload, dict):
                raise ValueError('Original native input record changed shape')
            attempt['receipt_cursor'] = stream.tell()
            attempt['receipt_cursor_anchor'] = {'offset': offset, 'bytes': len(raw),
                                               'sha256': life.digest(raw)}
            if goal_resume:
                if row.get('type') != 'event_msg':
                    continue
                if payload.get('type') == 'thread_goal_updated':
                    if any(row.get(k) or payload.get(k) for k in
                           ('isMeta', 'isSynthetic', 'isSidechain', 'isCompactSummary')):
                        continue
                    updated_goal = payload.get('goal') or {}
                    from ccc_codex_queue import epoch
                    update_at = epoch(row.get('timestamp'))
                    if (payload.get('threadId') == saved_turn['session_id']
                            and isinstance(updated_goal, dict)
                            and updated_goal.get('threadId') == saved_turn['session_id']
                            and updated_goal.get('status') == 'active'
                            and attempt.get('write_started_at', attempt['attempted_at']) <= update_at <= time.time() + 1):
                        attempt['native_goal_update'] = {'offset': offset, 'bytes': len(raw),
                            'sha256': life.digest(raw), 'session_id': saved_turn['session_id']}
                    continue
                if payload.get('type') != 'task_started' or not attempt.get('native_goal_update'):
                    continue
                attribution = payload.get('turn_attribution') or {}
                from ccc_codex_queue import epoch
                event_at = epoch(row.get('timestamp'))
                if (row.get('type') == 'event_msg' and payload.get('type') == 'task_started'
                        and isinstance(attribution, dict)
                        and attribution.get('turn_trigger') == 'goal'
                        and isinstance(payload.get('turn_id'), str)
                        and attribution.get('turn_id') == payload['turn_id']
                        and payload['turn_id'] != saved_turn['turn_id']
                        and event_at is not None
                        and attempt.get('write_started_at', attempt['attempted_at']) <= event_at <= time.time() + 1
                        and not any(row.get(k) or payload.get(k) for k in
                            ('isMeta', 'isSynthetic', 'isSidechain', 'isCompactSummary'))):
                    return {'schema': 'herdr-codex-native-goal-start-receipt-r33',
                            'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
                            'offset': offset, 'bytes': len(raw), 'sha256': life.digest(raw),
                            'session_id': saved_turn['session_id'], 'goal_id': saved_goal['goal_id'],
                            'turn_id': payload['turn_id'], 'native_goal_update': attempt['native_goal_update'],
                            'ordinary_user_receipt': False}
                continue
            if row.get('type') != 'response_item' or payload.get('type') != 'message' or payload.get('role') != 'user':
                continue
            if any(row.get(k) or payload.get(k) for k in
                   ('isMeta', 'isSynthetic', 'isSidechain', 'isCompactSummary')):
                continue
            from ccc_encrypted_recovery import _native_goal_context
            if _native_goal_context(payload):
                continue
            content = payload.get('content')
            if (isinstance(content, list) and len(content) == 1
                    and isinstance(content[0], dict) and content[0].get('type') == 'input_text'
                    and content[0].get('text') == attempt['message']):
                return {'schema': 'herdr-codex-native-user-receipt-v176',
                        'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
                        'offset': offset, 'bytes': len(raw), 'sha256': life.digest(raw)}
    return None


def compact_store(state, row):
    from ccc_encrypted_recovery import EncryptedRecoveryStore

    class PanelRecoveryStore(EncryptedRecoveryStore):
        """Stage original compact admission under its original SQLite lock.

        A provider refusal rolls back only this prospective transaction. It
        never deletes a previously consumed repair or refunds a reservation.
        """
        _admission_db = None

        def get(self, surface_id):
            # Admission reads must see this same transaction's uncommitted writes.
            if self._admission_db is not None:
                return super().get(surface_id)
            if not self.path.exists():
                return None
            # Polling does not initialize schema or reserve SQLite's writer lock.
            try:
                db = sqlite3.connect(self.path.absolute().as_uri() + '?mode=ro',
                                     uri=True, timeout=2)
                try:
                    row = db.execute('SELECT record FROM repairs WHERE surface=? '
                                     'ORDER BY rowid DESC LIMIT 1', (surface_id,)).fetchone()
                finally:
                    db.close()
            except sqlite3.Error as error:
                raise ValueError('Original compact recovery observation unavailable: '
                                 + str(error)[:160]) from error
            if row is None:
                return None
            record = json.loads(row[0])
            if (not isinstance(record, dict) or not isinstance(record.get('phase'), str)
                    or not record['phase']):
                raise ValueError('Original compact recovery record has an invalid shape')
            return record

        def begin(self, identity, source, path, *, completed_request=None, request_attempts=None):
            if completed_request is None:
                return super().begin(identity, source, path)
            with self.admission_transaction() if self._admission_db is None else nullcontext():
                current = _completed_request_source(identity, source, path, completed_request.get('goal', {}),
                    expected_continuation=(request_attempts or {}).get(completed_request.get('continuation', {}).get('attempt_key'), {}).get('message'))
                plain = {k: v for k, v in completed_request.items() if k != 'continuation'}
                if current != plain or not reprove_completed_request(completed_request, request_attempts):
                    raise ValueError('Original completed-request source changed before compact intent')
                record, created = super().begin(identity, source, path)
                if created:
                    record = self.update(record, completed_goal_request=copy.deepcopy(completed_request))
                return record, created

        @contextmanager
        def transaction(self):
            if self._admission_db is not None:
                yield self._admission_db
            else:
                with super().transaction() as db:
                    yield db

        @contextmanager
        def admission_transaction(self):
            if self._admission_db is not None:
                raise ValueError('Nested original compact admission refused')
            with super().transaction() as db:
                self._admission_db = db
                try:
                    yield
                finally:
                    self._admission_db = None

    return PanelRecoveryStore(state / 'codex-panels' / life.digest(row['key'].encode()) / 'encrypted.sqlite3')


def provider_evidence(guard, endpoint, turn, error_type):
    """Share the original per-session/provider budget with main CCC."""
    from ccc_provider_retry import RETRYABLE
    if error_type not in RETRYABLE:
        return None
    provider = turn.get('model_provider') or guard.goal.provider_for_turn(target(endpoint), turn)
    message = str((turn.get('error') or {}).get('message') or '')
    evidence = guard.codex_provider_retry.observe(turn.get('session_id'), provider,
        turn.get('turn_id'), error_type, message, turn.get('at'))
    if not evidence:
        raise ValueError('provider_retry_evidence_missing_or_superseded')
    return evidence


def persist_ledger(path, ledger, row):
    """A persistence fault holds input on this row without discarding the fleet."""
    import ccc_herdr_guard as transport
    try:
        transport.atomic(path, ledger)
    except (OSError, ValueError, TypeError) as error:
        row.update(continuation_eligible=False, attention_required=True,
                   ledger_persistence_error=str(error)[:160],
                   recovery_reason='original_ledger_persistence_failed; no replay')
        return False
    return True


def mark_pending_attention(attempt):
    """Expose uncertainty without changing the original send or replay budget."""
    if attempt['phase'] in FINAL_PHASES:
        return False
    now = time.time()
    if (attempt['phase'] == 'write_unknown'
            and (attempt.get('payload_not_sent') or attempt.get('provider_reservation_uncertain'))):
        reason = 'original_input_not_sent_or_provider_budget_uncertain; no replay'
    elif now - attempt.get('write_started_at', attempt['attempted_at']) >= 120:
        reason = ('native_compact_evidence_missing; replay remains forbidden'
                  if attempt.get('route') == 'compact'
                  else 'native_input_receipt_missing; replay remains forbidden')
    else:
        return False
    if attempt.get('attention_required'):
        return False
    attempt.update(attention_required=True, attention_since=now, attention_reason=reason)
    return True


def step(guard, snapshot, state):
    import ccc_herdr_guard as transport
    from ccc_encrypted_recovery import ACTIVE, same_session
    path = state / 'herdr-codex-input-ledger.json'
    ledger = json.loads(path.read_bytes()) if path.exists() else {'schema': 'herdr-codex-input-ledger-v176', 'attempts': {}}
    transport.rollover_ledger(path, ledger, FINAL_PHASES)
    attempts = ledger['attempts']
    if len(attempts) > 8192:
        raise ValueError('Herdr Codex ledger exceeds bound; history preserved')
    statusline_maintenance(guard, snapshot, state, ledger)
    for row in snapshot['agents']:
        if row.get('agent') != 'codex' or not row.get('process_bound'):
            continue
        endpoint = transport.endpoint_for(guard.config, row['session'])
        if endpoint.get('discovered_read_only') is True:
            # Monitoring must not settle, reserve or reset another controller's
            # provider/compact/input transactions, even after native success.
            row.update(continuation_eligible=False, auto_input_enabled=False,
                       recovery_reason='discovered_endpoint_monitor_only')
            continue
        proof = guard.codex_sources.get((endpoint['socket'], row['pane_id']))
        try:
            store = compact_store(state, row)
            repair = store.get(endpoint['cmux_surface_id'])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            # One unreadable panel ledger must not suppress other observations.
            # Preserve every original attempt and budget; no input is admissible.
            row.update(continuation_eligible=False, attention_required=True,
                       recovery_reason='original_compact_ledger_observation_unavailable',
                       compact_ledger_observation_error=str(error)[:240])
            if isinstance(row.get('control_coverage'), dict):
                row['control_coverage'].update(input_eligible=False, state='waiting',
                    reason='original_compact_ledger_observation_unavailable')
            continue
        phase = None
        if repair and repair['phase'] in ACTIVE and proof and proof['turn'].get('session_id'):
            repair, phase = store.observe(repair, dict(proof['turn'], **target(endpoint)))
        related = [a for a in attempts.values() if a['key'] == row['key']]
        pending = False
        changed = False
        isolated = []
        for attempt in related:
            if attempt['phase'] in FINAL_PHASES:
                continue
            if (attempt.get('route') == 'compact' and repair
                    and repair['identity'].get('turn_id') == attempt['source']['turn'].get('turn_id')
                    and phase in COMPACT_RESULTS
                    and (phase in {'cancelled', 'superseded'}
                         or repair.get('compact_started_offset') is not None)):
                attempt.update(phase=COMPACT_RESULTS[phase],
                    compact_observation={k: repair.get(k) for k in ('key', 'file_id', 'compact_turn',
                        'compact_started_offset', 'compacted_offset', 'compacted_sha256',
                        'compact_completed_offset', 'phase', 'reason', 'cursor')})
                changed = True
                continue
            if not transport.same_generation(attempt, row):
                isolated.append(attempt.get('intent_id'))
                continue
            try:
                old_cursor = attempt.get('receipt_cursor')
                observed = (receipt(attempt, current_proof=proof)
                            if attempt.get('route') == 'goal_resume' else receipt(attempt))
                changed = changed or old_cursor != attempt.get('receipt_cursor')
            except (OSError, ValueError, TypeError, KeyError, ImportError) as error:
                observed = None
                attempt.update(phase='receipt_unknown', reason=str(error)[:160])
                changed = True
            if observed:
                attempt.pop('attention_required', None)
                attempt.pop('attention_reason', None)
                attempt.pop('attention_since', None)
                attempt.update(phase='native_goal_resumed' if attempt.get('route') == 'goal_resume'
                               else 'native_received', receipt=observed, confirmed_at=time.time())
                changed = True
            else:
                pending = True
                changed = mark_pending_attention(attempt) or changed
        row.update(send_count=len(related), native_receipts=sum(a['phase'] == 'native_received' for a in related),
                   native_goal_receipts=sum(a['phase'] == 'native_goal_resumed' for a in related),
                   submit_phase='pending' if pending else 'none', isolated_generation_intents=isolated)
        if changed and not persist_ledger(path, ledger, row):
            continue
        if pending:
            attention = [a for a in related if a['phase'] not in FINAL_PHASES and a.get('attention_required')]
            row.update(continuation_eligible=False,
                       recovery_reason=(attention[0].get('attention_reason') or 'original_input_evidence_missing; no replay')
                       if attention else 'original_input_pending',
                       attention_required=bool(attention))
            continue
        if not guard.guard_config.get('armed') or not row.get('native_reader_available'):
            continue
        if not proof or not proof['turn'].get('session_id'):
            continue
        turn, goal = proof['turn'], proof['goal']
        if (turn.get('kind') == 'task_complete' and not turn.get('error')
                and turn.get('last_agent_message') and guard.codex_provider_retry.path.exists()):
            provider = turn.get('model_provider') or guard.goal.provider_for_turn(target(endpoint), turn)
            guard.codex_provider_retry.success(turn.get('session_id'), provider, at=turn['at'],
                completed_turn=turn.get('turn_id'), last_agent_message=turn.get('last_agent_message'), error=None)
        ident = dict(turn, **target(endpoint))
        compact_continue = False
        route = 'continue'
        if repair and repair['phase'] in ACTIVE:
            repair, phase = store.observe(repair, ident)
            if phase != 'ready':
                unresolved = any(a.get('repair_finalization_unknown') for a in related)
                row.update(continuation_eligible=False, attention_required=unresolved,
                           recovery_reason='original_compact_refusal_finalization_unknown; no replay'
                           if unresolved else 'original_encrypted_recovery_' + phase)
                continue
            compact_continue = True
        elif repair and repair.get('compact_turn') == turn.get('turn_id'):
            row.update(continuation_eligible=False, recovery_reason='original_compact_' + repair['phase'] + '; no guarded inner-terminal respawn')
            continue
        elif not row.get('continuation_eligible'):
            continue
        elif row.get('native_error_type') == 'invalid_encrypted_content':
            route = 'compact'
        elif goal.get('status') == 'blocked':
            if not selected_goal_resume(turn, goal):
                row.update(continuation_eligible=False, recovery_reason='native_blocked_goal_selection_unverified')
                continue
            route = 'goal_resume'
            if any(a['source']['turn'].get('session_id') == turn['session_id']
                   and a['source']['turn'].get('turn_id') == turn['turn_id']
                   and not transport.retryable_zero_input(a) for a in related):
                # A new route cannot replay a consumed/UNKNOWN old attempt.
                row.update(continuation_eligible=False, recovery_reason='original_turn_already_attempted')
                continue
        key = life.digest(life.canonical({'key': row['key'], 'server': row['server'],
             'pid': row['pid'], 'birth': row['birth'], 'session_id': turn['session_id'],
             'turn_id': turn['turn_id'], 'route': route}))
        old = attempts.get(key)
        if old and not transport.retryable_zero_input(old):
            row.update(continuation_eligible=False, recovery_reason='original_turn_already_attempted')
            continue
        if any(not transport.same_generation(a, row)
               and a.get('source', {}).get('turn', {}).get('session_id') == turn['session_id']
               and a.get('source', {}).get('turn', {}).get('turn_id') == turn['turn_id']
               and not transport.retryable_zero_input(a) for a in related):
            row.update(continuation_eligible=False,
                       recovery_reason='original_turn_attempted_in_previous_generation; no replay')
            continue
        provider_reservation_started = False
        before_write_complete = False
        followup_base_operations = None
        try:
            panes = guard.cli(endpoint['session'], ['api', 'snapshot'])['snapshot']['panes']
            matches = [p for p in panes if p['pane_id'] == row['pane_id']]
            if len(matches) != 1 or matches[0]['terminal_id'] != row['terminal_id']:
                raise ValueError('Original inner terminal changed')
            pane = matches[0]
            native = guard.foreground(endpoint, pane, row['server'])
            if native['pid'] != row['pid'] or native['birth'] != row['birth']:
                raise ValueError('Original inner foreground generation changed')
            config = verify_original(guard, endpoint, pane, native, row['server'], proof, state,
                                     compact_continue=compact_continue, completed_repair=repair if compact_continue else None)
            interval = max(1.0, float(config.get('send_interval_sec', 1)))
            if time.time() - max((a.get('attempted_at', 0) for a in related), default=0) < interval:
                row.update(recovery_reason='original_retry_interval')
                continue
            message = ('/compact' if route == 'compact' else
                       '/goal resume' if route == 'goal_resume' else config.get('message'))
            if compact_continue:
                if not same_session(repair, ident) or repair['phase'] != 'compact_complete':
                    raise ValueError('Owned original compact completion changed')
                message = str(message or '') + ' CCC_IEC_' + repair['key'][:16]
            if (not isinstance(message, str) or not message.strip() or len(message.encode()) > 700
                    or any(c in message for c in '\r\n\t\x1b')):
                raise ValueError('Configured Codex continuation is outside the wire bound')
            completed_request = None
            if goal.get('status') == 'complete':
                completed_request = (repair.get('completed_goal_request') if compact_continue else
                                     completed_request_for(endpoint, turn, goal, attempts=attempts))
                if completed_request is None:
                    raise ValueError('Original completed-request proof unavailable')
                if not compact_continue and route == 'continue':
                    message += ' CCC_REQUEST_' + completed_request['source_span']['sha256'][:16]
                    if len(message.encode()) > 700:
                        raise ValueError('Bound request continuation exceeds wire limit')
            original_fence = fence(turn)
            attempt = {'schema': 'herdr-codex-original-input-intent-v176', 'key': row['key'],
                'intent_id': old['intent_id'] if old else uuid.uuid4().hex,
                'binding': transport.generation(endpoint, pane, native, row['server']),
                'source': proof, 'fence': original_fence, 'route': route,
                'compact_followup': compact_continue,
                'message': message, 'message_sha256': life.digest(message.encode()),
                'phase': 'intent', 'attempted_at': time.time(), 'input_operations': 0,
                'wire_requests': 0, 'input_cas_available': False,
                'input_count_semantics': 'prospective upper bound until host/native proof'}
            if completed_request is not None:
                attempt['completed_goal_request'] = completed_request
                if compact_continue:
                    attempt['completed_compact_boundary'] = request_chain_compact_boundary(repair, turn, goal, attempts)
            if old:
                transport.preserve_refused_attempt(attempts, key)
            attempts[key] = attempt
            transport.atomic(path, ledger)
            def revalidate():
                current_config = verify_original(guard, endpoint, pane, native, row['server'],
                    proof, state, compact_continue=compact_continue, completed_repair=repair if compact_continue else None)
                if route == 'continue' and current_config.get('message') != config.get('message'):
                    raise ValueError('Original continuation text changed')
                if (completed_request is not None and not compact_continue
                        and completed_request_for(endpoint, turn, goal, attempts=request_attempts(state)) != completed_request):
                    raise ValueError('Original completed-request source changed before input')
                if fence(turn) != original_fence:
                    raise ValueError('Original native EOF fence changed before input')
            def before_write():
                nonlocal repair, provider_reservation_started, before_write_complete, followup_base_operations
                revalidate()
                with store.admission_transaction() if route == 'compact' or compact_continue else nullcontext():
                    if route == 'compact':
                        repair, created = store.begin(ident, turn, turn['transcript_path'], completed_request=completed_request, request_attempts=attempts)
                        if not created:
                            raise ValueError('Original compact already has a consumed intent')
                    elif compact_continue:
                        observed, phase = store.observe(repair, ident)
                        if phase != 'ready':
                            raise ValueError('Original compact no longer ready')
                        followup_base_operations = observed.get('input_operations', 1)
                        repair = store.update(observed, phase='continue_intent', continue_message=message,
                                              input_operations=followup_base_operations + 1)
                    evidence_turn = repair['identity'] if compact_continue else turn
                    error_kind = 'invalid_encrypted_content' if compact_continue else row.get('native_error_type')
                    evidence = provider_evidence(guard, endpoint, evidence_turn, error_kind)
                    # Persist uncertainty BEFORE reserving the separate provider
                    # budget. If every later JSON write fails, this exact owned
                    # turn still has a durable no-replay marker with zero payloads.
                    attempt.update(phase='write_unknown', input_operations=0, wire_requests=0,
                                   provider_reservation_uncertain=evidence is not None)
                    transport.atomic(path, ledger)
                    if evidence is not None:
                        provider_attempt = (next((a['intent_id'] for a in related
                            if compact_continue and a.get('route') == 'compact'
                            and a['source']['turn'].get('turn_id') == repair.get('source_turn')), attempt['intent_id']))
                        provider_reservation_started = True
                        decision = guard.codex_provider_retry.evaluate(evidence, provider_attempt, reserve=True)
                        attempt['provider_retry'] = {'evidence': evidence, 'attempt_id': provider_attempt,
                                                    'reason': decision.reason_code, 'retry_at': decision.retry_at}
                        if not decision.allowed:
                            provider_reservation_started = decision.reason_code == 'provider_retry_ledger_unavailable'
                            attempt['provider_reservation_uncertain'] = provider_reservation_started
                            raise ValueError(decision.reason_code)
                        attempt['provider_reservation_uncertain'] = False
                        attempt['provider_reservation_consumed'] = True
                    attempt.update(phase='write_unknown', input_operations=1, wire_requests=1,
                                   write_started_at=time.time())
                    transport.atomic(path, ledger)
                before_write_complete = True
            response = transport.submit_with_fallback(endpoint, row['server'], pane['pane_id'],
                message, guard.scope, attempt, lambda: transport.atomic(path, ledger), before_write, revalidate)
            attempt['host_response'] = response
            if 'error' in response:
                error = response['error']
                code = error.get('code') if isinstance(error, dict) else None
                if transport.zero_input_refusal(attempt, response):
                    attempt.update(phase='host_refused', input_operations=0)
                    if route == 'compact' and repair and repair['phase'] == 'compact_intent':
                        attempt['repair_finalization_unknown'] = True
                        repair = store.update(repair, phase='compact_refused', input_operations=0,
                                              reason='authoritative Herdr pre-input refusal: ' + code)
                        attempt['repair_finalization_unknown'] = False
                    elif compact_continue and repair and repair['phase'] == 'continue_intent':
                        if followup_base_operations is None:
                            raise ValueError('Owned compact followup preimage missing')
                        attempt['repair_finalization_unknown'] = True
                        # Exact-record CAS removes only this new prospective
                        # followup count; consumed compact/provider history stays.
                        repair = store.update(repair, phase='continue_refused',
                                              input_operations=followup_base_operations,
                                              reason='authoritative Herdr pre-input followup refusal: ' + code)
                        attempt['repair_finalization_unknown'] = False
                else:
                    attempt['phase'] = 'write_unknown'
            else:
                attempt.update(phase='submitted', submitted_at=time.time())
            transport.atomic(path, ledger)
            row.update(continuation_eligible=False, submit_phase=attempt['phase'],
                       recovery_reason='original_codex_' + route + '_submission', send_count=len(related) + 1)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration, ImportError, sqlite3.Error, subprocess.SubprocessError) as error:
            if key in attempts:
                attempt = attempts[key]
                attempt['reason'] = str(error)[:160]
                if not before_write_complete:
                    # request() cannot send a payload until before_write returns.
                    # Correct only this prospective attempt, never old counters.
                    attempt.update(input_operations=0, wire_requests=0, payload_not_sent=True)
                if attempt['input_operations'] == 0 and attempt['wire_requests'] == 0 and not provider_reservation_started:
                    attempt['phase'] = 'prewrite_refused'
                    attempt['provider_reservation_uncertain'] = False
                if attempt.get('repair_finalization_unknown'):
                    attempt['attention_required'] = True
                mark_pending_attention(attempt)
                if not persist_ledger(path, ledger, row):
                    continue
                row['submit_phase'] = attempt['phase']
            row.update(continuation_eligible=False, attention_required=bool(
                       attempts.get(key, {}).get('repair_finalization_unknown')
                       or attempts.get(key, {}).get('attention_required')),
                       recovery_reason=str(error)[:160])
    snapshot['codex_input_operations'] = sum(a.get('input_operations', 0) for a in attempts.values())
    snapshot['codex_native_receipts'] = sum(a['phase'] == 'native_received' for a in attempts.values())
    snapshot['codex_native_goal_receipts'] = sum(a['phase'] == 'native_goal_resumed' for a in attempts.values())
    snapshot['codex_native_compact_results'] = sum(a['phase'] in {'native_compact_observed', 'native_compact_failed'}
                                                   for a in attempts.values())
    snapshot['codex_attention_required'] = sum((a['phase'] not in FINAL_PHASES and a.get('attention_required', False))
                                               or a.get('repair_finalization_unknown', False)
                                               for a in attempts.values())
    snapshot['codex_ledger_persistence_errors'] = sum(bool(row.get('ledger_persistence_error'))
                                                    for row in snapshot['agents'])
    snapshot['codex_compact_ledger_observation_errors'] = sum(
        'compact_ledger_observation_error' in row for row in snapshot['agents'])
    if snapshot['codex_compact_ledger_observation_errors']:
        snapshot['codex_control_coverage'] = fleet_control_coverage(snapshot['agents'])
    snapshot['input_operations'] = snapshot.get('input_operations', 0) + snapshot['codex_input_operations']
    snapshot['native_receipts'] = snapshot.get('native_receipts', 0) + snapshot['codex_native_receipts']


"""Append-only candidate helpers for the original owning Herdr guard.

The only permitted input is an official session-local statusline command.
Displayed IDs remain diagnostic clues, never native selection or input authority.
"""

STATUSLINE_COMMAND = '/statusline session-id model-with-reasoning current-dir fast-mode task-progress context-used context-window-size'


def statusline_display_observation(text):
    lines = text.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip()[:1] in {'›', '»'}]
    result = {'source': 'official_tui_statusline_diagnostic', 'input_authority': False,
              'current_selection_verified': False}
    if not prompts or lines[prompts[-1]].strip() not in {
            '›', '»', '› Ask Codex to do anything', '» Ask Codex to do anything'}:
        return dict(result, phase='unavailable', reason='empty_composer_anchor_missing')
    footer = lines[prompts[-1] + 1:]
    matches = []
    for line in footer:
        match = re.fullmatch(r'\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+·\s+GPT-[^\r\n]+', line)
        if match:
            matches.append(match.group(1))
    if len(matches) != 1:
        return dict(result, phase='unavailable', reason='full_statusline_sid_missing_or_ambiguous')
    return dict(result, phase='displayed', advisory_session_id=matches[0])


def statusline_narrow_prefix(line):
    """Strict measured footer shapes; never a selection or input proof."""
    goal = (r'(?:\s+Goal (?:stalled|paused) \(/goal resume\)'
            r'|\s+Goal achieved \([0-9dhms ]+\)'
            r'|\s+Pursuing goal \([0-9]+[dhms](?: [0-9]+[dhms])*\))?\s*')
    base = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-'
    for pattern in (r'\s*(' + base + r'[0-9a-f]{8,12})…' + goal,
                    r'\s*(' + base + r'[0-9a-f]{12}) +…' + goal,
                    r'\s*(' + base + r'[0-9a-f]{12})\s+·\s+(?:G|GP|GPT)…' + goal):
        match = re.fullmatch(pattern, line)
        if match:
            return match.group(1)
    return None


def statusline_bound_display(guard, process, text):
    """A UUID or model-truncated footer is resolved by kernel-held files.

    At least 112 displayed UUID bits, one matching lock and one matching rollout,
    unchanged native process birth and two identical vnode reads are mandatory.
    This grants no input authority or native conditional control.
    """
    full = statusline_display_observation(text)
    if full.get('reason') == 'empty_composer_anchor_missing':
        return full
    lines = text.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip()[:1] in {'›', '»'}]
    footer = lines[prompts[-1] + 1:]
    uuid_lines = [line for line in footer if re.match(r'^\s*[0-9a-fA-F]{8}-', line)]
    if len(uuid_lines) != 1:
        return {key: value for key, value in dict(full, phase='unavailable',
            reason='full_statusline_sid_missing_or_ambiguous').items() if key != 'advisory_session_id'}
    if full.get('phase') == 'displayed':
        return full
    import ccc_codex_queue as queue
    try:
        prefix = statusline_narrow_prefix(uuid_lines[0])
        if prefix is None:
            return full
        pid, birth = process['pid'], process['birth']
        if guard.scope.birth(pid) != birth:
            return full
        home = Path(process['environment'].get('CODEX_HOME', str(Path.home() / '.codex'))).resolve(strict=True)
        root = (home/'sessions').resolve(strict=True)
        files = queue.process_writable_files(pid, identities=True)
        lock_candidates = []
        for path in files:
            if path.parent != home/'thread-writer-locks' or path.suffix != '.lock':
                continue
            sid = path.stem
            if sid.startswith(prefix) and str(uuid.UUID(sid)) == sid:
                lock_candidates.append((sid,path))
        if len(lock_candidates) != 1:
            return full
        sid, lock = lock_candidates[0]
        path, file_identity, rollout_open = statusline_rollout_locator(guard, root, sid, lock, files, process)
        if not rollout_open:
            snapshot = queue.task_snapshot(path, sid)
            if not snapshot or snapshot.get('kind') not in {'task_complete', 'turn_aborted'}:
                return full
        for p in (lock,path):
            info = p.lstat()
            if (p.resolve(strict=True) != p or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or (files[lock] if p == lock else file_identity) != {'device':info.st_dev,'inode':info.st_ino}):
                return full
        again = queue.process_writable_files(pid, identities=True)
        relevant = lambda values: {p:v for p,v in values.items() if
            (p.parent == home/'thread-writer-locks' and p.stem.startswith(prefix))
            or (p.is_relative_to(root) and p.name.endswith('-'+sid+'.jsonl'))}
        if guard.scope.birth(pid) != birth or relevant(again) != relevant(files):
            return full
        info = path.lstat()
        if (path.resolve(strict=True) != path or file_identity != {'device':info.st_dev,'inode':info.st_ino}
                or (not rollout_open and queue.task_snapshot(path, sid) != snapshot)):
            return full
        writer_evidence = {'kernel_writer_pair': {str(lock): files[lock]}}
        if rollout_open:
            writer_evidence['kernel_writer_pair'][str(path)] = file_identity
        else:
            writer_evidence['idle_rollout_identity'] = {'path': str(path), **file_identity}
        return dict(full, phase='displayed', reason=None,
                    source='official_tui_statusline_kernel_unique_prefix',
                    advisory_session_id=sid, displayed_uuid_prefix=prefix,
                    **writer_evidence)
    except (OSError,ValueError,KeyError,TypeError,IndexError,RuntimeError,subprocess.SubprocessError):
        return full


def statusline_binding_read(guard, endpoint, spec, *, require_empty=True):
    import ccc_herdr_guard as transport
    if (endpoint.get('discovered_read_only') is True
            or endpoint['cmux_surface_id'] == transport.PROTECTED_SURFACE
            or spec['binding'].get('pid') == transport.PROTECTED_PID):
        raise ValueError('statusline diagnostic target is outside original input scope')
    membership = guard.cmux_membership(endpoint)
    server = guard.server(endpoint, membership)
    snapshot = guard.cli(endpoint['session'], ['api', 'snapshot'])['snapshot']
    matches = [p for p in snapshot['panes'] if p['pane_id'] == spec['pane_id']]
    if len(matches) != 1 or matches[0].get('agent') != 'codex':
        raise ValueError('statusline diagnostic target is not one original Codex pane')
    pane = matches[0]
    process = guard.foreground(endpoint, pane, server)
    binding = transport.generation(endpoint, pane, process, server)
    if binding != spec['binding']:
        raise ValueError('statusline diagnostic original binding changed')
    text = guard.cli(endpoint['session'], ['pane', 'read', pane['pane_id'], '--source', 'visible', '--lines', '160'])['text']
    if require_empty:
        allowed, reason = statusline_screen_admission(text, allow_running=spec.get("allow_running") is True)
        if not allowed:
            raise ValueError('statusline diagnostic: ' + str(reason))
    after = guard.foreground(endpoint, pane, server)
    transport.verify_membership(guard, endpoint, server)
    if after != process:
        raise ValueError('statusline diagnostic foreground changed during read')
    return process, text


def statusline_spec_valid(spec):
    """Pure shape check shared by group admission and the original member handler."""
    if not isinstance(spec, dict):
        return False
    return not (spec.get('schema') != 'ccc-original-statusline-diagnostic-v1'
        or spec.get('command') != STATUSLINE_COMMAND
        or spec.get('max_inputs') != 1
        or spec.get('input_authority') is not False
        or type(spec.get('expires_at')) not in (int, float)
        or not math.isfinite(spec['expires_at'])
        or not isinstance(spec.get('id'), str)
        or re.fullmatch(r'[a-z0-9-]{1,80}', spec['id']) is None)


def statusline_single_maintenance(guard, snapshot, state, input_ledger, spec):
    """One pinned UI-only operation under the production service's original lock.

    The caller is the existing step, already serialized by OriginalOwner. No
    independent sender, native reader binding, recovery budget or replay exists.
    """
    import ccc_herdr_guard as transport
    if spec is None:
        return
    if not statusline_spec_valid(spec):
        raise ValueError('statusline diagnostic specification changed')
    selected = [r for r in snapshot['agents'] if r.get('session') == spec.get('session')
                and r.get('pane_id') == spec.get('pane_id')]
    if len(selected) != 1:
        return
    row = selected[0]
    diagnostic = row.setdefault('statusline_diagnostic', {'input_authority': False})
    path = state / ('statusline-diagnostic-' + spec['id'] + '.json')
    attempt = json.loads(path.read_bytes()) if path.exists() else None
    if attempt is not None:
        # A lost host reply is not permission to repeat the command or Enter.
        if attempt.get('spec') != spec:
            diagnostic.update(phase='held', reason='original_statusline_attempt_spec_changed')
            return
        diagnostic.update(phase=attempt['phase'], input_operations=attempt['input_operations'])
        if attempt['phase'] not in {'submitted', 'write_unknown'}:
            return
        try:
            endpoint = transport.endpoint_for(guard.config, spec['session'])
            process, text = statusline_binding_read(guard, endpoint, spec, require_empty=False)
            displayed = statusline_bound_display(guard, process, text)
            if displayed['phase'] == 'displayed':
                attempt.update(phase='display_observed', display=displayed,
                               observed_at=time.time(), screen_sha256=life.digest(text.encode()))
                transport.atomic(path, attempt)
                diagnostic.update(attempt['display'], phase='display_observed')
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            diagnostic.update(reason=str(error)[:200])
        return
    if not guard.guard_config.get('armed') or time.time() > spec['expires_at']:
        diagnostic.update(phase='held', reason='diagnostic_disabled_or_expired')
        return
    before_write_complete = False
    try:
        endpoint = transport.endpoint_for(guard.config, spec['session'])
        policy_bytes = Path(guard.guard_config['ccc_config']).read_bytes()
        if life.digest(policy_bytes) != spec['policy_sha256']:
            raise ValueError('original control policy changed')
        enabled, reason = policy(json.loads(policy_bytes), endpoint)
        if not enabled:
            raise ValueError(reason)
        if any(a.get('key') == row['key'] and a.get('phase') not in FINAL_PHASES
               for a in input_ledger['attempts'].values()):
            raise ValueError('original input is unresolved; preserve its draft and attempt')
        if row.get('submit_phase') not in {None, 'none'}:
            raise ValueError('original panel submission is pending')
        process, text = statusline_binding_read(guard, endpoint, spec)
        executable = Path(process['executable']).resolve(strict=True)
        if str(executable) != spec['executable']:
            raise ValueError('official executable path changed')
        with executable.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != spec['executable_sha256']:
                raise ValueError('official executable content changed')
        attempt = {'schema': spec['schema'], 'spec': spec, 'phase': 'intent',
                   'input_operations': 0, 'wire_requests': 0, 'input_cas_available': False,
                   'before_screen_sha256': life.digest(text.encode()), 'created_at': time.time(),
                   'business_prompt': False, 'native_selection_authority': False}
        transport.atomic(path, attempt)
        def before_write():
            nonlocal before_write_complete
            if time.time() > spec['expires_at']:
                raise ValueError('original diagnostic expired before input')
            if Path(guard.guard_config['ccc_config']).read_bytes() != policy_bytes:
                raise ValueError('original policy changed before diagnostic input')
            fresh, current_text = statusline_binding_read(guard, endpoint, spec)
            if (fresh != process or statusline_composer_frame(current_text) != statusline_composer_frame(text)):
                raise ValueError('original process or full empty screen changed')
            attempt.update(phase='write_unknown', input_operations=1, wire_requests=1,
                           write_started_at=time.time())
            transport.atomic(path, attempt)
            before_write_complete = True
        response = transport.request(endpoint, spec['binding']['server'], 'agent.prompt',
            {'target': spec['pane_id'], 'text': STATUSLINE_COMMAND}, guard.scope,
            before_write=before_write)
        attempt['host_response'] = response
        if 'error' not in response:
            attempt['phase'] = 'submitted'
        elif isinstance(response['error'], dict) and response['error'].get('code') in {
                'agent_blocked', 'agent_not_found', 'agent_not_ready', 'empty_agent_prompt'}:
            attempt.update(phase='host_refused', input_operations=0)
        transport.atomic(path, attempt)
        diagnostic.update(phase=attempt['phase'], input_operations=attempt['input_operations'])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        diagnostic.update(phase='held', reason=str(error)[:200])
        if attempt is not None:
            if not before_write_complete:
                attempt.update(phase='prewrite_refused', input_operations=0, wire_requests=0)
            attempt['reason'] = str(error)[:200]
            try:
                transport.atomic(path, attempt)
            except (OSError, ValueError, TypeError):
                # Preserve a durable existing UNKNOWN even when reporting fails.
                diagnostic.update(phase='write_unknown', reason='diagnostic_ledger_write_failed')


def statusline_composer_frame(text):
    lines = text.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip()[:1] in {'›', '»'}]
    return '\n'.join(lines[prompts[-1]:]) if prompts else text


def statusline_screen_admission(text, *, allow_running=False):
    allowed, reason = screen_admission(text)
    if allowed or not allow_running or reason != 'client_working_or_reconnecting':
        return allowed, reason
    # Only the documented session-local statusline operation takes this path.
    # A reconnect, hook, dialog, queued draft or failed connection is retained.
    lines = text.splitlines()
    filtered = [line for line in lines if not re.fullmatch(
        r'\s*• Working \([^\r\n]* • esc to interrupt\)[^\r\n]*', line)]
    if len(filtered) == len(lines):
        return False, reason
    prompts = [i for i, line in enumerate(filtered) if line.lstrip()[:1] in {'›', '»'}]
    if prompts:
        for i in range(prompts[-1] + 1, len(filtered)):
            if re.match(r'^\s*GPT-[\w.-]+\s+ultra\s+·\s+(?:/|~/)', filtered[i]):
                filtered[i] = re.sub(r'\s+(?:Pursuing goal|Goal achieved) \([0-9dhms ]+\)\s*$', '', filtered[i])
    return screen_admission('\n'.join(filtered))


def statusline_maintenance(guard, snapshot, state, input_ledger):
    specs = list(guard.guard_config.get('statusline_diagnostics', []))
    if guard.guard_config.get('statusline_diagnostic') is not None:
        specs.insert(0, guard.guard_config['statusline_diagnostic'])
    # Validate the entire bounded set before observing or sending any member.
    # Different diagnostic IDs must not grant a second input to the same pane.
    if (len(specs) > 16
            or any(not isinstance(s, dict)
                   or any(not isinstance(s.get(k), str) or not s[k]
                          for k in ('id', 'session', 'pane_id')) for s in specs)):
        raise ValueError('Original statusline diagnostic list is ambiguous or exceeds bound')
    if (len({s['id'] for s in specs}) != len(specs)
            or len({(s['session'], s['pane_id']) for s in specs}) != len(specs)):
        raise ValueError('Original statusline diagnostic list contains a duplicate ID or pane')
    if any(not statusline_spec_valid(spec) for spec in specs):
        raise ValueError('Original diagnostic set contains an invalid specification')
    for spec in specs:
        statusline_single_maintenance(guard, snapshot, state, input_ledger, spec)


# Original V171 bounded request provenance; no new controller or reader.

def completed_request_environment(payload, tid):
    """Exact official harness environment context is advisory, never a task."""
    metadata = payload.get('internal_chat_message_metadata_passthrough')
    content = payload.get('content')
    if (not isinstance(metadata, dict) or metadata.get('turn_id') != tid
            or metadata.get('content_item_kinds') != ['environments.environment_context']
            or metadata.get('content_item_metadata') != [{'provenance': {'type': 'harness', 'hook': False}}]
            or not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get('type') != 'input_text'):
        return False
    text = content[0].get('text')
    if not isinstance(text, str) or not text.startswith('<environment_context>') or not text.endswith('</environment_context>'):
        return False
    import xml.etree.ElementTree as ET
    try:
        if '<!' in text or '<?' in text:
            return False
        root = ET.fromstring(text)
        return root.tag == 'environment_context' and not root.attrib
    except ET.ParseError:
        return False


def completed_request_error(message):
    import cmux_codex_watch as original
    kind = original._match_error_block(message) if isinstance(message, str) else None
    return bool(kind in original.PROVIDER_RETRYABLE | {'stream'}
        and original._is_recovery_candidate(original.ScreenState(
            'recoverable_error', error_type=kind, message_kind='codex')))


def completed_request_for(endpoint, turn, goal, *, attempts=None):
    if not selected_current_thread(turn):
        return None
    return (capture_completed_request(dict(turn, **target(endpoint)), turn,
                                      turn.get('transcript_path'), goal)
            or completed_request_chain(endpoint, turn, goal, attempts))

def _passive_completion(event, sid, tid):
    payload = event.get('payload', {})
    item = payload.get('item')
    return (event.get('type') == 'event_msg'
            and payload.get('type') == 'item_completed'
            and payload.get('thread_id') == sid and payload.get('turn_id') == tid
            and isinstance(item, dict) and item.get('type') == 'SubAgentActivity'
            and item.get('kind') == 'completed')

def _completed_request_source(identity, source, path, goal, *, expected_continuation=None):
    """Read one bounded original user span, including its exact failed fence.

    This is byte provenance, not native-process authorization. The caller must
    supply and recheck the authentic current reader and unchanged Goal row.
    An appended compact may retain the old source proof; a rewritten source,
    missing origin or later input inside that span cannot.
    """
    import ccc_codex_queue as native
    from ccc_encrypted_recovery import IDENTITY_FIELDS, READ_LIMIT, same_session, _native_goal_context
    from ccc_provider_retry import structured_error_type
    try:
        if not all(isinstance(value, dict) for value in (identity, source, goal)):
            return None
        sid, tid = str(uuid.UUID(identity['session_id'])), str(uuid.UUID(source['turn_id']))
        if (any(not identity.get(k) for k in IDENTITY_FIELDS)
                or any(identity.get(k) != source.get(k)
                       for k in ('session_id', 'pid', 'process_start', 'turn_id', 'at'))
                or source.get('kind') != 'task_complete'
                or not isinstance(source.get('error'), dict)
                or not completed_request_error(source['error'].get('message'))
                or goal.get('status') != 'complete' or goal.get('session_id') != sid
                or not isinstance(goal.get('goal_id'), str) or not goal['goal_id']
                or type(goal.get('updated_at_ms')) is not int or goal['updated_at_ms'] <= 0):
            return None
        signature, tail = source.get('signature'), source.get('terminal_tail')
        if (not isinstance(signature, list) or len(signature) != 3
                or any(type(v) is not int for v in signature)
                or not isinstance(tail, dict) or tail.get('end') != signature[1]
                or type(tail.get('offset')) is not int or not 0 <= tail['offset'] < signature[1]):
            return None
        fence = signature[1]
        path = Path(path)
        if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb', buffering=0) as stream:
            before = os.fstat(stream.fileno())
            stamp = native._task_stamp(before)
            if (not stat.S_ISREG(before.st_mode) or before.st_ino != signature[0]
                    or before.st_uid != os.getuid() or before.st_mode & 0o022
                    or before.st_size < fence or native._task_stamp(path.stat()) != stamp):
                return None
            prefix = os.pread(stream.fileno(), min(fence, 65536), 0)
            newline = prefix.find(b'\n')
            if newline < 0:
                return None
            header = prefix[:newline + 1]
            meta = json.loads(header)
            if (not isinstance(meta, dict) or meta.get('type') != 'session_meta'
                    or not isinstance(meta.get('payload'), dict) or meta['payload'].get('id') != sid):
                return None
            offset = max(0, fence - READ_LIMIT)
            raw = os.pread(stream.fileno(), fence - offset, offset)
            if len(raw) != fence - offset or not raw.endswith(b'\n'):
                return None
            if offset:
                skip = raw.find(b'\n') + 1
                if not skip:
                    return None
                raw, offset = raw[skip:], offset + skip
            end, records, started = fence, [], None
            for line in reversed(raw.splitlines(keepends=True)):
                end -= len(line)
                event = json.loads(line)
                if not isinstance(event, dict) or any(event.get(k) for k in ('isMeta', 'isSynthetic', 'synthetic', 'isSidechain', 'isCompactSummary')):
                    return None
                payload = event.get('payload')
                if not isinstance(payload, dict) or any(payload.get(k) for k in ('isMeta', 'isSynthetic', 'synthetic', 'isSidechain', 'isCompactSummary')):
                    return None
                records.append((end, line, event))
                if event.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                    if payload.get('turn_id') != tid:
                        return None
                    started = records[-1]
                    break
            if started is None:
                return None
            records.reverse()
            at = native.epoch(started[2].get('timestamp'))
            attribution = started[2]['payload'].get('turn_attribution')
            required = {'turn_id', 'turn_trigger', 'parent_turn_id', 'root_turn_id', 'initiating_agent_path'}
            if (not isinstance(attribution, dict) or not required.issubset(attribution)
                    or attribution['turn_id'] != tid or attribution['turn_trigger'] != 'user'
                    or attribution['root_turn_id'] != tid or attribution['parent_turn_id'] is not None
                    or attribution['initiating_agent_path'] is not None
                    or not math.isfinite(at) or not math.isfinite(source['at'])
                    or not goal['updated_at_ms'] / 1000 < at <= source['at']):
                return None
            ordinary, advisory, completed = [], [], None
            for position, line, event in records[1:]:
                payload, outer = event['payload'], event.get('type')
                kind = payload.get('type')
                if completed is not None:
                    if not _passive_completion(event, sid, tid):
                        return None
                    continue
                if outer == 'event_msg' and kind in {'turn_aborted', 'task_started'}:
                    return None
                if outer == 'event_msg' and kind == 'task_complete':
                    if (payload.get('turn_id') != tid or payload.get('error') != source['error']
                            or native.epoch(event.get('timestamp')) != source['at']
                            or position != tail['offset']):
                        return None
                    completed = position
                elif outer == 'event_msg' and kind == 'user_message':
                    advisory.append(payload.get('message'))
                elif outer == 'response_item' and kind == 'message' and payload.get('role') == 'user':
                    metadata = payload.get('internal_chat_message_metadata_passthrough')
                    if not isinstance(metadata, dict) or metadata.get('turn_id') != tid:
                        return None
                    if _native_goal_context(payload) or completed_request_environment(payload, tid):
                        continue
                    content = payload.get('content')
                    items = metadata.get('content_item_metadata')
                    if (metadata.get('content_item_kinds') != ['user.text']
                            or not isinstance(items, list) or len(items) != 1
                            or not isinstance(items[0], dict)
                            or not isinstance(items[0].get('provenance'), dict)
                            or items[0]['provenance'].get('type') != 'user'
                            or event.get('isMeta') or payload.get('isMeta')
                            or not isinstance(content, list) or len(content) != 1
                            or not isinstance(content[0], dict) or content[0].get('type') != 'input_text'
                            or not isinstance(content[0].get('text'), str)):
                        return None
                    text, input_at = content[0]['text'].strip(), native.epoch(event.get('timestamp'))
                    if (not text or '\x1b' in text or '\0' in text or text.startswith('/')
                            or text.startswith('<codex_internal_context')
                            or (expected_continuation is None and (text.lower() in {'continue', 'retry', 'resume'}
                                or 'CCC_IEC_' in text or 'CCC_IEC_REQUEST_' in text or 'CCC_REQUEST_' in text))
                            or (expected_continuation is not None and content[0]['text'] != expected_continuation)
                            or not math.isfinite(input_at) or not at <= input_at <= source['at']):
                        return None
                    # Keep original root proofs compatible, but authenticate retry bytes exactly.
                    if expected_continuation is not None:
                        text = content[0]['text']
                    ordinary.append({'offset': position, 'bytes': len(line), 'at': input_at,
                                     'sha256': hashlib.sha256(line).hexdigest(),
                                     'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text})
            span = b''.join(line for _, line, _ in records)
            if (completed is None or len(ordinary) != 1
                    or any(value != ordinary[0]['text'] for value in advisory)
                    or hashlib.sha256(span[completed - started[0]:]).hexdigest() != tail.get('sha256')
                    or os.pread(stream.fileno(), len(span), started[0]) != span
                    or os.pread(stream.fileno(), len(header), 0) != header
                    or native._task_stamp(os.fstat(stream.fileno())) != stamp
                    or native._task_stamp(path.stat()) != stamp):
                return None
        return {'schema': 'completed-goal-ordinary-request-v1', 'identity': copy.deepcopy(identity),
                'goal': copy.deepcopy(goal), 'path': str(path),
                'file_id': [before.st_dev, before.st_ino], 'fence': fence,
                'header_sha256': hashlib.sha256(header).hexdigest(), 'header_bytes': len(header),
                'source': {k: copy.deepcopy(source.get(k)) for k in
                           ('session_id', 'pid', 'process_start', 'kind', 'turn_id', 'at', 'error', 'signature', 'terminal_tail')},
                'source_span': {'offset': started[0], 'bytes': len(span), 'end': fence,
                                'sha256': hashlib.sha256(span).hexdigest()},
                'ordinary_input': {k: v for k, v in ordinary[0].items() if k != 'text'}}
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None

def capture_completed_request(identity, source, path, goal, *, expected_continuation=None):
    """Capture only a current ordinary user failure after an unchanged complete Goal."""
    import ccc_codex_queue as native
    from ccc_encrypted_recovery import IDENTITY_FIELDS, READ_LIMIT, same_session, _native_goal_context
    from ccc_provider_retry import structured_error_type
    try:
        if not all(isinstance(value, dict) for value in (identity, source, goal)):
            return None
        snapshot = native.task_snapshot(path, identity['session_id'])
        keys = ('kind', 'turn_id', 'at', 'error', 'signature', 'terminal_tail')
        if not snapshot or any(snapshot.get(k) != source.get(k) for k in keys):
            return None
        value = _completed_request_source(identity, source, path, goal, expected_continuation=expected_continuation)
        return value if value and native.task_snapshot(path, identity['session_id']) == snapshot else None
    except (OSError, ValueError, KeyError, TypeError):
        return None

def completed_request_matches(record, turn, goal, *, request_attempts=None):
    """Re-prove the original user request and only its owned current compact.

    Legacy repairs have no such proof. Later Goal edits, user inputs, aborts,
    missing/torn records and consumed/unknown continuation phases stay refused.
    The caller still applies the existing live process, draft and input gates.
    """
    import ccc_codex_queue as native
    from ccc_encrypted_recovery import IDENTITY_FIELDS, READ_LIMIT, same_session, _native_goal_context
    from ccc_provider_retry import structured_error_type
    try:
        if not all(isinstance(value, dict) for value in (record, turn, goal)):
            return False
        proof = record.get('completed_goal_request')
        phase = record.get('phase')
        if (not isinstance(proof, dict) or proof.get('goal') != goal
                or phase not in {'compact_complete', 'continue_intent', 'failed'}
                or record.get('input_operations') != (2 if phase == 'continue_intent' else 1)
                or not same_session(record, turn) or record.get('source_turn') != proof['source']['turn_id']
                or record.get('identity') != proof.get('identity')
                or any(record.get(k) != proof.get(k) for k in ('path', 'file_id', 'fence'))
                or turn.get('kind') != 'task_complete' or turn.get('turn_id') != record.get('compact_turn')
                or record['compact_turn'] == record['source_turn']):
            return False
        if not reprove_completed_request(proof, request_attempts):
            return False
        snapshot = native.task_snapshot(record['path'], turn['session_id'])
        keys = ('kind', 'turn_id', 'at', 'error', 'signature', 'terminal_tail')
        if not snapshot or any(snapshot.get(k) != turn.get(k) for k in keys):
            return False
        path = Path(record['path'])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb', buffering=0) as stream:
            before = os.fstat(stream.fileno())
            stamp = native._task_stamp(before)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022
                    or [before.st_dev, before.st_ino] != record['file_id']
                    or record.get('cursor') != before.st_size
                    or not 0 < before.st_size - record['fence'] <= READ_LIMIT):
                return False
            boundary = os.pread(stream.fileno(), record['fence'] - record['boundary_start'], record['boundary_start'])
            if hashlib.sha256(boundary).hexdigest() != record['boundary_sha256']:
                return False
            raw = os.pread(stream.fileno(), before.st_size - record['fence'], record['fence'])
            if not raw.endswith(b'\n'):
                return False
            offset, started, compacted, completed = record['fence'], None, None, None
            for line in raw.splitlines(keepends=True):
                position, offset = offset, offset + len(line)
                event = json.loads(line)
                if not isinstance(event, dict) or any(event.get(k) for k in ('isMeta', 'isSynthetic', 'synthetic', 'isSidechain', 'isCompactSummary')):
                    return False
                payload = event.get('payload')
                if not isinstance(payload, dict) or any(payload.get(k) for k in ('isMeta', 'isSynthetic', 'synthetic', 'isSidechain', 'isCompactSummary')):
                    return False
                outer, kind = event.get('type'), payload.get('type')
                if completed is not None:
                    if not _passive_completion(event, turn['session_id'], turn['turn_id']):
                        return False
                    continue
                if outer == 'event_msg' and kind == 'turn_aborted':
                    return False
                if outer == 'event_msg' and kind == 'user_message' and str(payload.get('message') or '').strip() != '/compact':
                    return False
                if outer == 'response_item' and kind == 'message' and payload.get('role') == 'user':
                    if not (_native_goal_context(payload) or completed_request_environment(payload, record['compact_turn'])):
                        content = payload.get('content')
                        if (not isinstance(content, list) or len(content) != 1
                                or not isinstance(content[0], dict) or content[0].get('type') != 'input_text'
                                or str(content[0].get('text') or '').strip() != '/compact'):
                            return False
                if outer == 'event_msg' and kind == 'task_started':
                    attribution = payload.get('turn_attribution')
                    if (started is not None or payload.get('turn_id') != record['compact_turn']
                            or position != record.get('compact_started_offset')
                            or attribution is not None and (not isinstance(attribution, dict)
                                or attribution.get('turn_trigger') != 'user')):
                        return False
                    started = position
                elif outer == 'compacted':
                    if (started is None or compacted is not None or position != record.get('compacted_offset')
                            or len(line) != record.get('compacted_bytes')
                            or hashlib.sha256(line).hexdigest() != record.get('compacted_sha256')):
                        return False
                    compacted = position
                elif outer == 'event_msg' and kind == 'task_complete':
                    if (started is None or payload.get('turn_id') != turn['turn_id']
                            or payload.get('error') != turn.get('error')
                            or native.epoch(event.get('timestamp')) != turn.get('at')
                            or position != turn.get('terminal_tail', {}).get('offset')):
                        return False
                    completed = position
                elif (outer == 'event_msg' and kind in {'exec_command_begin', 'mcp_tool_call_begin'}
                      or outer == 'response_item' and kind in {'function_call_output', 'custom_tool_call_output'}):
                    return False
            if phase == 'failed':
                if structured_error_type((turn.get('error') or {}).get('message')) != 'invalid_encrypted_content':
                    return False
            elif (turn.get('error') or compacted is None
                  or record.get('compact_completed_offset') != completed):
                return False
            if (completed is None or os.pread(stream.fileno(), len(raw), record['fence']) != raw
                    or native._task_stamp(os.fstat(stream.fileno())) != stamp
                    or native._task_stamp(path.stat()) != stamp):
                return False
        return native.task_snapshot(path, turn['session_id']) == snapshot
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return False


"""Original-attempt links for repeated failure of an already received retry.

The ledger stays the owner. A marker alone never supplies a new user request.
Each link rechecks its complete source bytes, original receipt and predecessor.
"""

_REQUEST_ATTEMPTS_CACHE = {}


def request_attempts(state):
    path = Path(state) / 'herdr-codex-input-ledger.json'
    if not path.exists():
        _REQUEST_ATTEMPTS_CACHE.pop(str(path), None)
        return {}
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or info.st_size > 64 * 1024 * 1024):
        raise ValueError('Original request ledger is not owner-controlled or bounded')
    stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    cached = _REQUEST_ATTEMPTS_CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    raw = path.read_bytes()
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != stamp:
        raise ValueError('Original request ledger changed while reading')
    value = json.loads(raw)
    attempts = value.get('attempts')
    if not isinstance(attempts, dict) or len(attempts) > 4096:
        raise ValueError('Original request ledger changed shape')
    if len(_REQUEST_ATTEMPTS_CACHE) >= 16 and str(path) not in _REQUEST_ATTEMPTS_CACHE:
        _REQUEST_ATTEMPTS_CACHE.pop(next(iter(_REQUEST_ATTEMPTS_CACHE)))
    _REQUEST_ATTEMPTS_CACHE[str(path)] = (stamp, attempts)
    return attempts


def request_chain_file(path, start, end, *, file_id, expected_sha=None):
    """Read a fixed owned span twice, refusing replacement or concurrent rewrite."""
    from ccc_codex_queue import _task_stamp
    path = Path(path)
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or type(start) is not int or type(end) is not int
            or not 0 <= start < end or end - start > MAX_RECEIPT):
        raise ValueError('Original request chain span unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                or [info.st_dev, info.st_ino] != file_id or info.st_size < end):
            raise ValueError('Original request chain file changed')
        raw = os.pread(fd, end - start, start)
        if (len(raw) != end - start or not raw.endswith(b'\n')
                or expected_sha is not None and life.digest(raw) != expected_sha
                or os.pread(fd, end - start, start) != raw
                or _task_stamp(info) != _task_stamp(os.fstat(fd))
                or _task_stamp(info) != _task_stamp(path.stat())):
            raise ValueError('Original request chain bytes changed')
    return raw


def request_chain_compact_boundary(repair, turn, goal, attempts):
    if not completed_request_matches(repair, dict(turn, **{
            k: repair['identity'][k] for k in ('surface_id', 'workspace_id')}), goal,
            request_attempts=attempts):
        raise ValueError('Original compact request proof changed')
    source = repair['completed_goal_request']
    start, end = source['fence'], turn['signature'][1]
    raw = request_chain_file(source['path'], start, end, file_id=source['file_id'])
    owned = [a for a in attempts.values() if a.get('route') == 'compact'
        and a.get('phase') == 'native_compact_observed'
        and a.get('source', {}).get('turn', {}).get('turn_id') == repair['source_turn']
        and a.get('compact_observation', {}).get('key') == repair['key']]
    if len(owned) != 1:
        raise ValueError('Original compact has no unique controller receipt')
    return {'start': start, 'end': end, 'sha256': life.digest(raw),
        'repair_key': repair['key'], 'compact_turn': repair['compact_turn'],
        'compact_intent_id': owned[0]['intent_id']}


def request_chain_parent(attempt, parent_proof, goal):
    """Bind a settled continuation to its captured request and original fence."""
    import ccc_herdr_guard as transport
    if (attempt.get('phase') != 'native_received' or attempt.get('route') != 'continue'
            or not transport.single_input_wire_proven(attempt)
            or attempt.get('completed_goal_request') != parent_proof
            or attempt.get('source', {}).get('goal') != goal or parent_proof.get('goal') != goal):
        raise ValueError('Original retry is not a settled request input')
    turn, boundary, receipt_value = attempt['source']['turn'], attempt['fence'], attempt['receipt']
    if (not selected_current_thread(turn) or not goal_receipt_selection(parent_proof['identity'], turn)
            or any(turn.get(k) != parent_proof['identity'].get(k) for k in ('pid', 'birth', 'session_id', 'process_start'))
            or boundary['path'] != parent_proof['path']
            or [boundary['device'], boundary['inode']] != parent_proof['file_id']
            or boundary['size'] != turn['signature'][1]
            or attempt['binding']['pid'] != turn['pid'] or attempt['binding']['birth'] != turn['birth']
            or attempt['binding']['cmux_surface_id'] != parent_proof['identity']['surface_id']
            or attempt['binding']['cmux_workspace_id'] != parent_proof['identity']['workspace_id']
            or attempt['message_sha256'] != life.digest(attempt['message'].encode())
            or receipt_value.get('schema') != 'herdr-codex-native-user-receipt-v176'
            or receipt_value['path'] != boundary['path']
            or [receipt_value['device'], receipt_value['inode']] != parent_proof['file_id']
            or receipt_value['offset'] < boundary['size']):
        raise ValueError('Original request input identity or receipt changed')
    request_chain_file(boundary['path'], 0, boundary['header_bytes'], file_id=parent_proof['file_id'],
        expected_sha=boundary['header_sha256'])
    request_chain_file(boundary['path'], boundary['tail_start'], boundary['size'], file_id=parent_proof['file_id'],
        expected_sha=boundary['tail_sha256'])
    if attempt.get('compact_followup'):
        compact = attempt['completed_compact_boundary']
        if (compact['start'] != parent_proof['fence'] or compact['end'] != boundary['size']
                or compact['compact_turn'] != turn['turn_id']):
            raise ValueError('Original compact continuation boundary changed')
        request_chain_file(boundary['path'], compact['start'], compact['end'],
            file_id=parent_proof['file_id'], expected_sha=compact['sha256'])
    elif any(parent_proof['source'].get(k) != turn.get(k) for k in
             ('kind', 'turn_id', 'at', 'error', 'signature', 'terminal_tail')):
        raise ValueError('Original retry is not for its proven request failure')
    return attempt


def request_chain_edge(proof, attempt):
    """A received retry must be the sole input of the next failed native turn."""
    from ccc_codex_queue import epoch
    boundary, received = attempt['fence'], attempt['receipt']
    if (proof['source_span']['offset'] < boundary['size']
            or proof['source']['turn_id'] == attempt['source']['turn']['turn_id']
            or any(proof['ordinary_input'][k] != received[k] for k in ('offset', 'bytes', 'sha256'))
            or proof['ordinary_input']['text_sha256'] != attempt['message_sha256']):
        raise ValueError('Received retry does not belong to the current failed turn')
    start = proof['source_span']['offset']
    if start > boundary['size']:
        raw = request_chain_file(proof['path'], boundary['size'], start, file_id=proof['file_id'])
        original_span = proof['source_span']
        source_bytes = request_chain_file(proof['path'], original_span['offset'], original_span['end'],
            file_id=proof['file_id'], expected_sha=original_span['sha256'])
        started_at = epoch(json.loads(source_bytes.split(b'\n', 1)[0]).get('timestamp'))
        settings_seen = False
        for line in raw.splitlines():
            event = json.loads(line)
            if _passive_completion(event, proof['identity']['session_id'], attempt['source']['turn']['turn_id']):
                continue
            if not settings_seen and request_chain_settings_notice(event, proof['identity']['session_id'],
                    attempt['write_started_at'], started_at):
                settings_seen = True
                continue
            raise ValueError('Another event intervened before the received retry')
    raw = request_chain_file(proof['path'], received['offset'], received['offset'] + received['bytes'],
        file_id=proof['file_id'], expected_sha=received['sha256'])
    event = json.loads(raw)
    at = epoch(event.get('timestamp'))
    if not isinstance(at, (int, float)) or not math.isfinite(at) or at < attempt['write_started_at']:
        raise ValueError('Retry receipt predates its original input')


def reprove_completed_request(proof, attempts=None):
    """Walk original ledger references iteratively; never treat a retry as a task."""
    attempts = attempts or {}
    try:
        current, links, visited = proof, [], set()
        while current.get('continuation') is not None:
            link = current['continuation']
            key = link['attempt_key']
            if key in visited or len(visited) >= 4096:
                return False
            visited.add(key)
            attempt = attempts[key]
            if attempt['intent_id'] != link['intent_id']:
                return False
            parent = attempt['completed_goal_request']
            request_chain_parent(attempt, parent, current['goal'])
            links.append((current, attempt))
            current = parent
        if _completed_request_source(current['identity'], current['source'], current['path'], current['goal']) != current:
            return False
        for value, attempt in reversed(links):
            plain = {k: v for k, v in value.items() if k != 'continuation'}
            fresh = _completed_request_source(value['identity'], value['source'], value['path'], value['goal'],
                                              expected_continuation=attempt['message'])
            if fresh != plain:
                return False
            request_chain_edge(value, attempt)
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return False


def completed_request_chain(endpoint, turn, goal, attempts):
    """Consider only the most recent settled original input on this panel."""
    if not attempts or not selected_current_thread(turn):
        return None
    candidates = [(key, a) for key, a in attempts.items()
        if a.get('completed_goal_request') and a.get('route') == 'continue'
        and a.get('binding', {}).get('pid') == turn.get('pid')
        and a['binding'].get('birth') == turn.get('birth')
        and a['binding'].get('cmux_surface_id') == endpoint['cmux_surface_id']
        and a.get('source', {}).get('turn', {}).get('session_id') == turn.get('session_id')
        and a.get('phase') != 'prewrite_refused'
        and a.get('fence', {}).get('size', float('inf')) < turn.get('signature', [0, 0])[1]]
    if not candidates:
        return None
    key, attempt = max(candidates, key=lambda kv: kv[1]['fence']['size'])
    try:
        parent = attempt['completed_goal_request']
        request_chain_parent(attempt, parent, goal)
        if not reprove_completed_request(parent, attempts):
            return None
        value = capture_completed_request(dict(turn, **target(endpoint)), turn, turn['transcript_path'], goal,
                                          expected_continuation=attempt['message'])
        if value is None:
            return None
        value['continuation'] = {'attempt_key': key, 'intent_id': attempt['intent_id']}
        request_chain_edge(value, attempt)
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None



def request_chain_settings_notice(event, session_id, written_at, started_at):
    """Official thread snapshot before this owned input's turn; no new authority.

    Goal state, exact original native receipt and user-source chain are still
    mandatory. Other inputs, lifecycle events and copied/synthetic snapshots
    cannot use this exception.
    """
    from ccc_codex_queue import epoch
    if not isinstance(event, dict) or event.get('type') != 'event_msg':
        return False
    payload = event.get('payload')
    flags = ('isMeta', 'isSynthetic', 'synthetic', 'isSidechain', 'isCompactSummary')
    if (not isinstance(payload, dict) or set(payload) != {'type', 'thread_id', 'thread_settings'}
            or any(event.get(k) or payload.get(k) for k in flags)
            or payload.get('type') != 'thread_settings_applied' or payload.get('thread_id') != session_id):
        return False
    at = epoch(event.get('timestamp'))
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (at, written_at, started_at)):
        return False
    settings = payload.get('thread_settings')
    required = {'model', 'model_provider_id', 'approval_policy', 'approvals_reviewer',
                'permission_profile', 'cwd', 'collaboration_mode', 'disabled_plugin_ids'}
    optional = {'service_tier', 'active_permission_profile', 'runtime_workspace_roots',
                'reasoning_effort', 'reasoning_summary', 'personality',
                'requested_approvals_reviewer'}
    return (written_at <= at <= started_at and isinstance(settings, dict)
        and required.issubset(settings) and set(settings).issubset(required | optional)
        and all(isinstance(settings[k], str) and settings[k] for k in ('model', 'model_provider_id', 'cwd'))
        and Path(settings['cwd']).is_absolute()
        and isinstance(settings['permission_profile'], dict)
        and isinstance(settings['collaboration_mode'], dict)
        and isinstance(settings['disabled_plugin_ids'], list))
