"""Read-only evidence for resuming an existing goal after rollout corruption.

This never fabricates task_complete. The resulting action reactivates an
existing blocked goal through the native StartIfIdle continuation mechanism.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import re
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
import stat

import ccc_codex_queue as native
import ccc_guard_scope as scope
from ccc_shared_codex_turn import writer_files


def linked(path, identity):
    info = Path(path).stat()
    return info.st_dev == identity["device"] and info.st_ino == identity["inode"]


def _provider_database(files, lock):
    databases = [p for p in files if p.name == 'state_5.sqlite'
                 and p.parent == lock.parent.parent]
    if len(databases) != 1:
        raise ValueError('missing original writer provider database')
    return databases[0]


def _read_provider(database, sid):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
        rows = db.execute('SELECT substr(model_provider,1,1025) FROM threads WHERE id=? LIMIT 2', (sid,)).fetchall()
    if (len(rows) != 1 or not isinstance(rows[0][0], str)
            or not rows[0][0].strip() or len(rows[0][0]) > 1024):
        raise ValueError('missing original session provider')
    return rows[0][0]


def provider_for_turn(target, turn):
    """Read provider from the live writer's own state DB, never global config."""
    try:
        pid, sid = turn['pid'], turn['session_id']
        process = scope.process(pid)
        if (not process or process['surface_id'] != target['surface_id']
                or process['environment_workspace_id'] != target['workspace_id']
                or abs(process['birth'][0] + process['birth'][1] / 1e6
                       - float(turn['process_start'])) >= 1):
            return None
        shared = None
        writer_pid = pid
        if turn.get('shared_writer'):
            from ccc_shared_codex_turn import binding_for_turn
            shared = binding_for_turn(target, turn)
            if shared is None:
                return None
            writer_pid = shared['writer_pid']
        files = writer_files(writer_pid)
        # Child threads retain their own writer locks in the same process.
        # Select only the independently bound primary session; never choose
        # whichever lock happens to be returned first by the OS.
        sid = str(uuid.UUID(sid))
        locks = [p for p in files if p.parent.name == 'thread-writer-locks'
                 and p.suffix == '.lock' and p.stem == sid]
        if len(locks) != 1:
            return None
        database = _provider_database(files, locks[0])
        paths = [locks[0], database]
        if not all(not p.is_symlink() and linked(p, files[p]) for p in paths):
            return None
        provider = _read_provider(database, sid)
        if _read_provider(database, sid) != provider:
            return None
        current = writer_files(writer_pid)
        if ((shared is not None and binding_for_turn(target, turn) is None)
                or scope.process(pid) != process or any(current.get(p) != files[p] or p.is_symlink() or not linked(p, files[p]) for p in paths)):
            return None
        return provider
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def _debug_fields(value, name):
    """Read one Rust debug struct, never field-like text inside user content.

    Only the explicitly supported Steer envelope below is actionable. Unknown,
    duplicate, unbalanced or truncated fields remain a veto.
    """
    prefix = name + " {"
    if not value.startswith(prefix) or not value.endswith("}") or len(value) > 65536:
        raise ValueError("unsupported debug envelope")
    body = value[len(prefix):-1]
    stack, quoted, escaped, start, fields = [], False, False, 0, {}
    for index, char in enumerate(body + ","):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[(":
            stack.append(char)
            if len(stack) > 64:
                raise ValueError("debug nesting limit")
        elif char in "}])":
            if not stack or stack.pop() != {"}": "{", "]": "[", ")": "("}[char]:
                raise ValueError("unbalanced debug value")
        elif char == "," and not stack:
            part = body[start:index].strip()
            start = index + 1
            key, separator, item = part.partition(":")
            if not separator or not re.fullmatch(r"[a-z_]+", key) or key in fields or not item.strip():
                raise ValueError("invalid debug field")
            fields[key] = item.strip()
    if quoted or stack:
        raise ValueError("truncated debug value")
    return fields


def _same_turn_steer(body, session_id, turn_id):
    prefix = f"session_loop{{thread_id={session_id}}}: Submission sub="
    if not body.startswith(prefix):
        return False
    try:
        submission = _debug_fields(body[len(prefix):], "Submission")
        # New native builds include this optional ownership field. Only its
        # explicitly empty form preserves the previously supported envelope;
        # an unknown/nonempty guard must not authorize a terminal write.
        if submission.get("residency_guard") == "None":
            submission.pop("residency_guard")
        if set(submission) != {"id", "op", "trace", "parent_turn_id", "root_turn_id"}:
            return False
        uuid.UUID(json.loads(submission["id"]))
        op = _debug_fields(submission["op"], "TurnInput")
        if set(op) != {"request", "mode", "reply"}:
            return False
        mode = _debug_fields(op["mode"], "Steer")
        request = _debug_fields(op["request"], "TurnInputRequest")
        if set(request) != {'input', 'thread_settings', 'start', 'additional_context', 'responsesapi_client_metadata', 'trace'}:
            return False
        user = _debug_fields(request["input"], "UserInput")
        return (set(mode) == {"expected_turn_id"}
                and json.loads(mode["expected_turn_id"]) == turn_id
                and set(user) == {"content", "client_id"})
    except (ValueError, KeyError, TypeError):
        return False


def goal_state_for_turn(target, turn):
    """Read Goal state independently of the error log, never authorize resume.

    A missing resume proof is not proof that no Goal exists. In particular the
    logs database may lag the Goal database at a failed-turn boundary. Only an
    authenticated, stable absent/completed row permits the ordinary non-Goal route.
    """
    stage = 'native Goal identity unavailable'
    try:
        if (not isinstance(turn, dict) or turn.get('kind') != 'task_complete'
                or not isinstance(turn.get('session_id'), str)):
            return {'status': 'unknown', 'reason': stage}
        pid, sid = turn['pid'], str(uuid.UUID(turn['session_id']))
        process = scope.process(pid)
        if (not process or process['surface_id'] != target['surface_id']
                or process['environment_workspace_id'] != target['workspace_id']
                or turn.get('process_start') != process['birth'][0]):
            return {'status': 'unknown', 'reason': stage}
        shared = None
        writer = pid
        if turn.get('shared_writer'):
            from ccc_shared_codex_turn import binding_for_turn
            shared = binding_for_turn(target, turn)
            if shared is None:
                return {'status': 'unknown', 'reason': stage}
            writer = shared['writer_pid']
        files = writer_files(writer)
        locks = [p for p in files if p.parent.name == 'thread-writer-locks'
                 and p.name == sid + '.lock']
        if len(locks) != 1:
            return {'status': 'unknown', 'reason': stage}
        lock = locks[0]
        database = lock.parent.parent / 'goals_1.sqlite'
        stage = 'native Goal database unavailable or changed'
        info = database.lstat()
        identity = {'device': info.st_dev, 'inode': info.st_ino}
        if (not stat.S_ISREG(info.st_mode) or lock.is_symlink()
                or not linked(lock, files[lock])):
            return {'status': 'unknown', 'reason': stage}
        query = 'SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=? LIMIT 2'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            rows = db.execute(query, (sid,)).fetchall()
        if (len(rows) > 1 or (rows and (not isinstance(rows[0][0], str)
                or not rows[0][0] or rows[0][1] not in
                {'active', 'blocked', 'paused', 'complete', 'budget_limited', 'usage_limited'}
                or type(rows[0][2]) is not int or rows[0][2] <= 0))):
            return {'status': 'unknown', 'reason': 'native Goal row unsupported'}
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            unchanged = db.execute(query, (sid,)).fetchall() == rows
        after = writer_files(writer)
        if (not unchanged or scope.process(pid) != process
                or (shared is not None and binding_for_turn(target, turn) != shared)
                or after.get(lock) != files[lock] or not linked(lock, files[lock])
                or lock.is_symlink() or database.is_symlink() or not linked(database, identity)):
            return {'status': 'unknown', 'reason': stage}
        return {'status': rows[0][1] if rows else 'absent', 'session_id': sid,
                'goal_id': rows[0][0] if rows else None,
                'updated_at_ms': rows[0][2] if rows else None}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return {'status': 'unknown', 'reason': stage}


def ordinary_turn_after_pause(target, turn, goal):
    """Prove independent user work after a pause; never resume the paused Goal.

    The same native writer must retain a complete failed EOF, a task start
    after the unchanged pause, and that task's ordinary user input. Goal
    contexts, missing origin, later pauses and unsupported metadata fail closed.
    Read at most 8 MiB; a long turn without its origin remains unproved.
    """
    try:
        from ccc_shared_codex_turn import binding_for_turn, valid
        if (not isinstance(goal, dict) or goal.get('status') != 'paused'
                or type(goal.get('updated_at_ms')) is not int
                or goal['updated_at_ms'] <= 0
                or not isinstance(turn, dict) or turn.get('kind') != 'task_complete'
                or goal.get('session_id') != turn.get('session_id')
                or not isinstance(turn.get('terminal_tail'), dict)
                or not isinstance(turn.get('error'), dict)):
            return None
        sid, tid = str(uuid.UUID(turn['session_id'])), str(uuid.UUID(turn['turn_id']))
        proof = binding_for_turn(target, turn)
        if proof is None:
            return None
        path = Path(proof['path'])
        snapshot = native.task_snapshot(path, sid)
        if (not snapshot or snapshot.get('terminal_tail') != turn['terminal_tail']
                or any(snapshot.get(k) != turn.get(k)
                       for k in ('kind', 'turn_id', 'at', 'error', 'signature'))):
            return None
        pause_at = goal['updated_at_ms'] / 1000
        started, inputs = None, []
        with path.open('rb') as handle:
            stamp = native._task_stamp(os.fstat(handle.fileno()))
            if stamp != native._task_stamp(path.stat()) or path.is_symlink():
                return None
            size = stamp[2]
            limit = min(size, 16384)
            while started is None:
                offset = size - limit
                handle.seek(offset)
                raw = handle.read(limit)
                if len(raw) != limit or not raw.endswith(b'\n'):
                    return None
                lines = raw.splitlines(keepends=True)
                if offset:
                    lines = lines[1:]
                end, inputs = size, []
                for line in reversed(lines):
                    end -= len(line)
                    event = json.loads(line)
                    payload = event.get('payload')
                    if not isinstance(payload, dict):
                        return None
                    if (event.get('type') == 'event_msg'
                            and payload.get('type') == 'task_started'
                            and payload.get('turn_id') == tid):
                        at = native.epoch(event.get('timestamp'))
                        if not math.isfinite(at) or not pause_at < at <= turn['at']:
                            return None
                        started = {'at': at, 'offset': end,
                                   'sha256': hashlib.sha256(line).hexdigest()}
                        break
                    if (event.get('type') == 'response_item'
                            and payload.get('type') == 'message'
                            and payload.get('role') == 'user'):
                        metadata = payload.get('internal_chat_message_metadata_passthrough')
                        if not isinstance(metadata, dict):
                            return None
                        if metadata.get('turn_id') != tid:
                            return None
                        # Typed native provenance, not words quoted by a user.
                        if (metadata.get('content_item_kinds') != ['user.text']
                                or event.get('isMeta') or payload.get('isMeta')):
                            return None
                        at = native.epoch(event.get('timestamp'))
                        if not math.isfinite(at) or not pause_at < at <= turn['at']:
                            return None
                        inputs.append({'at': at, 'offset': end,
                                       'sha256': hashlib.sha256(line).hexdigest()})
                if started is not None or limit >= min(size, 8 * 1024 * 1024):
                    break
                limit = min(size, 8 * 1024 * 1024, limit * 4)
            if (not started or not inputs
                    or any(item['at'] < started['at'] for item in inputs)
                    or native._task_stamp(os.fstat(handle.fileno())) != stamp
                    or native._task_stamp(path.stat()) != stamp):
                return None
        if (not valid(target, proof)
                or native.task_snapshot(path, sid) != snapshot
                or goal_state_for_turn(target, turn) != goal
                or native._task_stamp(path.stat()) != stamp):
            return None
        return {'session_id': sid, 'turn_id': tid, 'goal': dict(goal),
                'task_start': started, 'ordinary_inputs': list(reversed(inputs)),
                'terminal_tail': dict(turn['terminal_tail'])}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def _native_blocked_goal(target, pid, turn, diagnostics):
    """Pin a failed native EOF and an existing blocked Goal independently.

    A provider failure need not have caused the original blocked transition.
    The current writer's complete failed turn is sufficient even if its log DB
    is missing or the same Goal has been blocked for hours. This proof cannot
    reactivate a paused/limited Goal or survive a new Goal revision or input.
    """
    stage = 'native_goal_identity'

    def refused():
        if diagnostics is not None:
            diagnostics['reason'] = stage
        return None

    try:
        from ccc_shared_codex_turn import binding_for_turn, valid
        if not isinstance(turn.get('session_id'), str):
            return refused()
        process = scope.process(pid)
        if (not process or process.get('remote') or turn.get('pid') != pid
                or process['surface_id'] != target['surface_id']
                or process['environment_workspace_id'] != target['workspace_id']
                or turn.get('process_start') != process['birth'][0]):
            return refused()
        shared = binding_for_turn(target, turn)
        if shared is None or shared['process'] != process:
            return refused()
        sid = str(uuid.UUID(turn['session_id']))
        stage = 'native_failed_eof'
        snapshot = native.task_snapshot(shared['path'], sid)
        # Re-read the bound file. A caller-supplied tail digest is not evidence.
        if (not snapshot or any(snapshot.get(key) != turn.get(key) for key in
                ('kind', 'turn_id', 'at', 'error', 'signature', 'terminal_tail'))
                or snapshot.get('kind') != 'task_complete'):
            return refused()
        if not isinstance(snapshot.get('turn_id'), str):
            return refused()
        uuid.UUID(snapshot['turn_id'])
        tail, signature = snapshot.get('terminal_tail'), snapshot.get('signature')
        error, at = snapshot.get('error'), snapshot.get('at')
        born = process['birth'][0] + process['birth'][1] / 1e6
        writer_born = shared['writer_birth'][0] + shared['writer_birth'][1] / 1e6
        if (not isinstance(tail, dict) or not isinstance(signature, list) or len(signature) != 3
                or any(type(value) is not int or value <= 0 for value in signature)
                or type(tail.get('offset')) is not int or tail['offset'] < 0
                or type(tail.get('end')) is not int
                or not tail['offset'] < tail['end'] == signature[1]
                or not isinstance(tail.get('sha256'), str)
                or re.fullmatch(r'[0-9a-f]{64}', tail['sha256']) is None
                or type(at) not in (int, float) or not math.isfinite(at)
                or at < max(born, writer_born) or at > time.time()
                or not isinstance(error, dict) or not isinstance(error.get('message'), str)
                or not error['message'].strip()):
            return refused()
        stage = 'native_goal_database'
        writer = shared['writer_pid']
        files = writer_files(writer)
        locks = [path for path in files if path.parent.name == 'thread-writer-locks'
                 and path.name == sid + '.lock']
        if len(locks) != 1:
            return refused()
        lock = locks[0]
        database = lock.parent.parent / 'goals_1.sqlite'
        # Idle clients may close their SQLite connections without releasing the
        # authenticated original writer lock. Resolve only that lock's state
        # database, pin its vnode and require the original SID on both reads.
        # An open descriptor, when present, must agree with the same vnode.
        provider_database = lock.parent.parent / 'state_5.sqlite'
        provider_info = provider_database.lstat()
        provider_identity = {'device': provider_info.st_dev, 'inode': provider_info.st_ino}
        info = database.lstat()
        goal_identity = {'device': info.st_dev, 'inode': info.st_ino}
        if (not stat.S_ISREG(info.st_mode)
                or not stat.S_ISREG(provider_info.st_mode)
                or lock.is_symlink() or not linked(lock, files[lock])
                or files.get(provider_database, provider_identity) != provider_identity):
            return refused()
        query = 'SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=? LIMIT 2'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            rows = db.execute(query, (sid,)).fetchall()
        if diagnostics is not None:
            diagnostics['goal_status'] = rows[0][1] if len(rows) == 1 else 'absent' if not rows else 'unknown'
        if (len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]
                or len(rows[0][0]) > 1024 or rows[0][1] != 'blocked'
                or type(rows[0][2]) is not int or rows[0][2] <= 0):
            return refused()
        goal_id, _, revision = rows[0]
        stage = 'native_goal_revision'
        # No claim that a newer unrelated Goal edit belongs to this failure.
        if revision / 1000 > at + .001:
            return refused()
        stage = 'native_goal_provider'
        provider = _read_provider(provider_database, sid)
        stage = 'native_goal_reread'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            if db.execute(query, (sid,)).fetchall() != rows:
                return refused()
        if _read_provider(provider_database, sid) != provider:
            return refused()
        current = writer_files(writer)
        if (scope.process(pid) != process or not valid(target, shared)
                or current.get(lock) != files[lock] or lock.is_symlink()
                or not linked(lock, files[lock])
                or current.get(provider_database, provider_identity) != provider_identity
                or provider_database.is_symlink() or not linked(provider_database, provider_identity)
                or database.is_symlink() or not linked(database, goal_identity)
                or native.task_snapshot(shared['path'], sid) != snapshot):
            return refused()
        # The full object is compared again by the reserved/paste/Enter gates.
        # Include database vnodes as well as values to detect same-row replacement.
        return {'kind': 'goal_blocked', 'session_id': sid, 'turn_id': snapshot['turn_id'],
                'pid': pid, 'process_start': process['birth'][0], 'birth': process['birth'],
                'at': revision / 1000, 'goal_id': goal_id, 'error': dict(error),
                'model_provider': provider, 'proof_source': 'native_failed_eof_v1',
                'goal_revision_ms': revision, 'goal_status': 'blocked',
                'goal_database': {'path': str(database), **goal_identity},
                'provider_database': {'path': str(provider_database), **provider_identity},
                'writer': {'pid': writer, 'birth': shared['writer_birth'],
                           'lock': {'path': str(lock), **files[lock]}},
                'native_evidence': {'path': str(shared['path']),
                                    'identity': dict(shared['rollout_identity']),
                                    'snapshot': snapshot}}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        if diagnostics is not None:
            diagnostics['exception'] = type(exc).__name__
        return refused()


def blocked_goal(target, pid, *, current_turn=None, diagnostics=None):
    """Require authenticated native EOF or the legacy matched error-log proof."""
    if (isinstance(current_turn, dict) and 'shared_writer' in current_turn
            and ('transcript_path' in current_turn or 'terminal_tail' in current_turn)):
        # Native evidence has priority, including its refusal. Never fall back
        # to an older log after an invalid, changed or incomplete native tail.
        return _native_blocked_goal(target, pid, current_turn, diagnostics)
    stage = 'client_identity'
    def refused():
        if diagnostics is not None:
            diagnostics['reason'] = stage
        return None
    try:
        process = scope.process(pid)
        if (not process or process["surface_id"] != target["surface_id"]
                or process["environment_workspace_id"] != target["workspace_id"]):
            return refused()
        shared = None
        writer_pid, writer_birth = pid, process['birth']
        if current_turn and current_turn.get('shared_writer'):
            stage = 'shared_turn_binding'
            from ccc_shared_codex_turn import binding_for_turn
            shared = binding_for_turn(target, current_turn)
            if shared is None:
                return refused()
            writer_pid, writer_birth = shared['writer_pid'], shared['writer_birth']
        stage = 'writer_inventory'
        files = writer_files(writer_pid)
        locks = [p for p in files if p.parent.name == "thread-writer-locks" and p.suffix == ".lock"]
        if current_turn is not None:
            if (not isinstance(current_turn.get('session_id'), str)
                    or current_turn.get('pid') != pid
                    or current_turn.get('process_start') != process['birth'][0]
                    or current_turn.get('kind') != 'task_complete'):
                return refused()
            sid = str(uuid.UUID(current_turn['session_id']))
            locks = [p for p in locks if p.stem == sid]
        goals = [p for p in files if p.name == "goals_1.sqlite"]
        logs = [p for p in files if p.name == "logs_2.sqlite"]
        if len(locks) != 1 or len(logs) != 1:
            return refused()
        sid = str(uuid.UUID(locks[0].stem))
        root = locks[0].parent.parent
        if logs[0].parent != root:
            return refused()
        # Resolve the provider while the real native turn still authenticates
        # the writer. A Goal proof has different kind/at fields and must not
        # impersonate that turn by copying its shared_writer hint later.
        provider_database = _provider_database(files, locks[0])
        held_paths = [locks[0], logs[0], provider_database]
        # Idle native clients may close their goal DB connection while retaining
        # the original writer lock and logs DB. Only an independently bound
        # completed primary turn may use that exact root's closed goal database.
        if not goals and current_turn is not None:
            candidate = root / 'goals_1.sqlite'
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                return refused()
            goals = [candidate]
            files = {**files, candidate: {'device': info.st_dev, 'inode': info.st_ino}}
        else:
            held_paths += goals
        if len(goals) != 1 or goals[0].parent != root or goals[0].is_symlink():
            return refused()
        paths = [locks[0], goals[0], logs[0], provider_database]
        if not all(not p.is_symlink() and linked(p, files[p]) for p in paths):
            return refused()
        provider = _read_provider(provider_database, sid)
        stage = 'goal_database'
        with closing(sqlite3.connect(goals[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            goal = db.execute("SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=?", (sid,)).fetchone()
        if diagnostics is not None:
            diagnostics['goal_status'] = goal[1] if goal else 'absent'
        if not goal or goal[1] != "blocked":
            return refused()
        age = time.time() - goal[2] / 1000
        # A completed primary turn is reauthenticated by the caller and matched
        # to the original writer/error below. Its blocked goal does not expire
        # merely because a provider outage lasts more than a day. Without that
        # independent turn, keep the bounded fallback for missing rollout data.
        if (not math.isfinite(age) or age < 0
                or (current_turn is None and age >= 86400)):
            return refused()
        stage = 'matching_error_log'
        query = ("SELECT id,ts,ts_nanos,substr(feedback_log_body,1,65537),process_uuid,target FROM logs "
                "WHERE thread_id=? AND ts>=? AND ((target='codex_core::session::turn' "
                "AND instr(feedback_log_body,': Turn error: ')>0) OR "
                "(target='codex_core::session::handlers' AND instr(feedback_log_body,'Submission sub=')>0)) "
                "ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT 65")
        params = (sid, max(process["birth"][0], writer_birth[0], int(goal[2] / 1000) - 30))
        with closing(sqlite3.connect(logs[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            rows = db.execute(query, params).fetchall()
        row = next((r for r in rows if r[5] == 'codex_core::session::turn'), None)
        if not row or not str(row[4]).startswith(f"pid:{writer_pid}:"):
            return refused()
        error = re.search(r": Turn error: ([^\n]+)$", row[3] or "")
        turn = re.search(r"(?:^|[\s{])turn\.id=([0-9a-f-]{36})(?:\s|})", row[3] or "")
        if current_turn is not None and (not turn or not error
                or turn[1] != current_turn.get('turn_id')
                or error[1] != (current_turn.get('error') or {}).get('message')):
            return refused()
        stage = 'goal_error_causality'
        at = row[1] + row[2] / 1e9
        born = process["birth"][0] + process["birth"][1] / 1e6
        if (not error or not turn or at < born or not -.001 <= goal[2] / 1000 - at <= 30
                or scope.process(pid) != process or not all(linked(p, files[p]) for p in paths)):
            return refused()
        stage = 'later_submission'
        following = rows[:rows.index(row)]
        if any(r[4] != row[4] or not _same_turn_steer(r[3] or "", sid, turn[1]) for r in following):
            return refused()
        # Re-read after log lookup. A manual resume or replacement invalidates
        # this evidence before terminal input can be considered.
        stage = 'goal_reread'
        with closing(sqlite3.connect(goals[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            if db.execute("SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=?", (sid,)).fetchone() != goal:
                return refused()
        stage = 'log_reread'
        with closing(sqlite3.connect(logs[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            if db.execute(query, params).fetchall() != rows:
                return refused()
        stage = 'provider_reread'
        if _read_provider(provider_database, sid) != provider:
            return refused()
        stage = 'final_identity_reread'
        current_files = writer_files(writer_pid)
        if (scope.process(pid) != process
                or (shared is not None and binding_for_turn(target, current_turn) is None)
                or any(current_files.get(p) != files[p] for p in held_paths)
                or any(not linked(p, files[p]) or p.is_symlink() for p in paths)):
            return refused()
        return {"kind": "goal_blocked", "session_id": sid, "turn_id": turn[1], "pid": pid,
                "process_start": process["birth"][0], "birth": process["birth"], "at": goal[2] / 1000,
                "goal_id": goal[0], "error": {"message": error[1]}, "log_id": row[0],
                "model_provider": provider,
                "submission_digest": hashlib.sha256(json.dumps(following).encode()).hexdigest()}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        if diagnostics is not None:
            diagnostics['exception'] = type(exc).__name__
        return refused()
