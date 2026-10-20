"""Authenticate a foreground TUI and its existing writer without a control socket.

The capability comes from one validated native foreground record. Rollout facts
are observation, and the native conditional operation remains the admission gate.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

import ccc_guard_scope as scope
import ccc_shared_codex_turn as shared
from ccc_client_thread_observation import read_foreground, COMPACT_INTERRUPT_CAPABILITY
from ccc_request_observation_policy import request_observation_directory_matches
from ccc_compact_support import owned_file, validate_native_receipt_fields
from ccc_compact_guard import _current_config_clear
from ccc_compact_sensor import native_compaction_start


def canonical_uuid(value):
    return str(uuid.UUID(value)).upper()


def pinned_file(path, *, limit=2 * 1024 * 1024):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('pinned evidence path must be canonical')
    with owned_file(path) as (stream, before):
        if before.st_size > limit:
            raise ValueError('pinned evidence exceeds the native reader limit')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) != before.st_size or len(raw) > limit
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('pinned evidence changed during read')
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest()), raw


def read_writer_goal(proof):
    sid = proof['session_id']
    files = shared.writer_files(proof['writer_pid'])
    locks = [p for p in files if p.parent.name == 'thread-writer-locks'
             and p.name == sid + '.lock']
    if len(locks) != 1 or locks[0] not in proof['paths']:
        raise ValueError('original writer Goal directory unavailable')
    path = locks[0].parent.parent / 'goals_1.sqlite'
    if path.resolve(strict=True) != path:
        raise ValueError('Goal database path changed')
    with owned_file(path) as (_stream, before):
        identity = (before.st_dev, before.st_ino)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.15)) as db:
            until = time.monotonic() + .5
            db.set_progress_handler(lambda: int(time.monotonic() > until), 1000)
            db.execute('PRAGMA query_only=ON')
            rows = db.execute(
                'SELECT thread_id,goal_id,objective,status,token_budget,tokens_used,'
                'time_used_seconds,created_at_ms,updated_at_ms '
                'FROM thread_goals WHERE thread_id=? LIMIT 2', (sid,)).fetchall()
        if identity != (path.lstat().st_dev, path.lstat().st_ino):
            raise ValueError('Goal database replaced during observation')
    if len(rows) > 1:
        raise ValueError('ambiguous original Goal')
    if not rows:
        return None, None
    row = rows[0]
    if (row[0] != sid or not isinstance(row[1], str) or not row[1]
            or not isinstance(row[2], str) or not row[2]
            or row[3] not in {'active', 'paused', 'blocked', 'usage_limited', 'budget_limited', 'complete'}
            or row[4] is not None and (type(row[4]) is not int or row[4] <= 0)
            or any(type(v) is not int or v < 0 for v in row[5:])
            or row[8] < row[7]):
        raise ValueError('invalid full native Goal revision')
    full = dict(threadId=sid, objective=row[2], status=row[3], tokenBudget=row[4],
                tokensUsed=row[5], timeUsedSeconds=row[6],
                createdAt=row[7] // 1000, updatedAt=row[8] // 1000)
    revision = dict(goal_id=row[1], created_at_ms=row[7], updated_at_ms=row[8])
    return full, revision


def client_snapshot(target, current_binding, facts):
    pid = current_binding['process']['pid']
    record = {}
    selection = read_foreground(pid, request_observation_directory_matches, _record=record)
    if (selection[0] != 'ok' or selection[1] != target['session_id']
            or record.get('compact_interrupt_capability') != COMPACT_INTERRUPT_CAPABILITY):
        raise ValueError('conditional compact requires the native v4 client capability')
    sessions_root = Path(selection[2][-1]).parent / 'sessions'
    proof, reason = shared.binding(target, pid, sessions_root)
    if not proof or proof['session_id'] != target['session_id'] or str(proof['path']) != target['path']:
        raise ValueError('native foreground writer not verified: ' + reason)
    if (proof['process']['birth'] != current_binding['process']['birth']
            or facts.get('session_id') != target['session_id'] or not facts.get('caught_up')
            or facts.get('lifecycle_error') or facts.get('receipt_overflow')
            or list(facts.get('file_id', [])) != [proof['rollout_identity']['device'], proof['rollout_identity']['inode']]):
        raise ValueError('current writer and original rollout disagree')
    observed = facts.get('observed_turn') or {}
    context = facts.get('turn_context') or {}
    if 'model' in record and 'effort' in record:
        settings = dict(model=record['model'], effort=record['effort'])
        settings_source = 'native-foreground-settings'
    elif context.get('turn_id') == observed.get('id'):
        settings = dict(model=context.get('model'), effort=context.get('effort'))
        settings_source = 'native-current-turn-context'
    else:
        raise ValueError('native settings observation unavailable for the original turn')
    if (not observed.get('id') or settings['model'] != target['model']
            or settings['effort'] != target['effort']):
        raise ValueError('native current-turn model, effort or identity changed')
    full_goal, goal_revision = read_writer_goal(proof)
    fresh = {}
    if (not shared.valid(target, proof)
            or read_foreground(pid, request_observation_directory_matches, _record=fresh) != selection
            or fresh != record):
        raise ValueError('native client changed during Goal observation')
    status = {'started_without_end_record': 'inProgress', 'task_complete': 'completed',
              'turn_aborted': 'interrupted'}.get(observed.get('status'))
    if status is None:
        raise ValueError('native turn lifecycle unavailable')
    turn = dict(id=observed['id'], status=status, startedAt=observed['started_at'])
    thread = dict(id=target['session_id'], sessionId=target['session_id'], path=target['path'],
                  model=settings['model'], reasoningEffort=settings['effort'],
                  canAcceptDirectInput=True, authority='native-conditional-capability-v4')
    return dict(thread=thread, turn=turn, compact=native_compaction_start(target['session_id'], turn, facts),
                goal_full=full_goal, goal_revision=goal_revision,
                client_pid=pid, client_epoch=record['client_epoch'],
                client_capability=COMPACT_INTERRUPT_CAPABILITY,
                foreground=dict(path=record['_source_path'], sha256=record['_source_sha256']),
                writer=dict(pid=proof['writer_pid'], birth=proof['writer_birth']),
                settings_source=settings_source)


def _same_target(row, target):
    return (row.get('session_id') == target['session_id']
            and all(canonical_uuid(row[k]) == canonical_uuid(target[k])
                    for k in ('surface_id', 'workspace_id', 'pane_id')))


def write_authority(worker, episode, snapshot, request, intent_path):
    target = worker.target
    guard = snapshot['input_guard']
    if (guard.get('safe') is not True or guard.get('caught_up') is not True
            or guard.get('token') != episode['input_token'] or guard.get('pending') != []
            or guard.get('intent_pending') != [] or guard.get('unknown') is not False
            or snapshot.get('paused') is not False
            or snapshot.get('client_capability') != COMPACT_INTERRUPT_CAPABILITY):
        raise ValueError('original current input or pause boundary unsafe')
    config_pin, config_raw = pinned_file(worker.config_path)
    config = json.loads(config_raw)
    _current_config_clear(config, target, canonical_uuid(target['surface_id']))
    if config.get('sessions', {}).get(target['session_id'], {}).get('paused', False) is not False:
        raise ValueError('current CCC session paused')
    enrollment_pin, enrollment_raw = pinned_file(worker.enrollment_path)
    enrollment = json.loads(enrollment_raw)
    if enrollment.get('scope') not in {'four_existing_compact_sessions_180_300', 'existing_compact_sessions_180_300_v2'}:
        raise ValueError('original explicit compact enrollment required')
    selected = [row for row in enrollment.get('targets', []) if row.get('session_id') == target['session_id']]
    if len(selected) != 1 or not _same_target(selected[0], target):
        raise ValueError('original enrollment target changed')
    _current_config_clear(config, selected[0], canonical_uuid(target['surface_id']))
    if (selected[0].get('model') != target['model'] or selected[0].get('effort') != target['effort']
            or selected[0].get('recovery_enabled', True) is not True):
        raise ValueError('original enrollment settings or recovery permission changed')
    index = worker.inputs.intent_index
    if index is None or index.path.name != 'native-input-intents.json':
        raise ValueError('complete actual external input intent index required')
    pins = [pinned_file(worker.inputs.path)[0], pinned_file(index.path)[0]]
    process = worker.state.get('authenticated_binding', {}).get('process')
    if (not process or process != target['process'] or process.get('pid') != request['clientPid']
            or not isinstance(process.get('birth'), list) or len(process['birth']) != 2):
        raise ValueError('current native process adoption boundary missing')
    authority = dict(schema='ccc-compact-authority-v4', episode_key=episode['key'],
        intent_path=str(intent_path), session_id=target['session_id'],
        compaction_occurrence=request['compactionOccurrence'],
        target=dict({k: target[k] for k in ('session_id', 'surface_id', 'workspace_id', 'pane_id',
                                           'model', 'effort')},
                    process=dict(process, tty=target['identity']['tty'])),
        client=dict(pid=request['clientPid'], epoch=snapshot['client_epoch'],
                    thread_id=target['session_id'], capability=COMPACT_INTERRUPT_CAPABILITY),
        foreground=snapshot['foreground'], enrollment=enrollment_pin, config=config_pin,
        input_guard=dict(safe=True, caught_up=True, token=guard['token'], pending=[], unknown=[]),
        pinned_inputs=pins)
    path = Path(intent_path).with_name(episode['key'] + '-authority.json')
    worker.journal_directory.write_once(path.name, authority)
    for pin in [config_pin, enrollment_pin, snapshot['foreground'], *pins]:
        if pinned_file(pin['path'])[0] != pin:
            raise ValueError('original pinned evidence changed after authority creation')
    return authority


def read_receipt(directory, key, phase, request):
    path = Path(directory) / f'{key}-{phase}.json'
    _pin, raw = pinned_file(path)
    value = json.loads(raw)
    if (value.get('episode_key') != key
            or value.get('original_intent') != request['intentPath']
            or not isinstance(value.get('fields'), dict)):
        raise ValueError('native receipt belongs to a different original request')
    return validate_native_receipt_fields(value['fields'], phase, request)
