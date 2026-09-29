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
import sqlite3
import time
import uuid

import ccc_codex_queue as native
import ccc_guard_scope as scope


def linked(path, identity):
    info = Path(path).stat()
    return info.st_dev == identity["device"] and info.st_ino == identity["inode"]


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
        files = native.process_writable_files(pid, identities=True)
        locks = [p for p in files if p.parent.name == 'thread-writer-locks' and p.suffix == '.lock']
        if len(locks) != 1 or str(uuid.UUID(locks[0].stem)) != sid:
            return None
        databases = [p for p in files if p.name == 'state_5.sqlite' and p.parent == locks[0].parent.parent]
        if len(databases) != 1:
            return None
        paths = [locks[0], databases[0]]
        if not all(linked(p, files[p]) for p in paths):
            return None
        def read():
            with closing(sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
                return db.execute('SELECT substr(model_provider,1,1025) FROM threads WHERE id=? LIMIT 2', (sid,)).fetchall()
        rows = read()
        if (len(rows) != 1 or not isinstance(rows[0][0], str)
                or not rows[0][0].strip() or len(rows[0][0]) > 1024 or read() != rows):
            return None
        current = native.process_writable_files(pid, identities=True)
        if scope.process(pid) != process or any(current.get(p) != files[p] or not linked(p, files[p]) for p in paths):
            return None
        return rows[0][0]
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


def blocked_goal(target, pid):
    """Require a live writer, its own databases and matching terminal error."""
    try:
        process = scope.process(pid)
        if (not process or process["surface_id"] != target["surface_id"]
                or process["environment_workspace_id"] != target["workspace_id"]):
            return None
        files = native.process_writable_files(pid, identities=True)
        locks = [p for p in files if p.parent.name == "thread-writer-locks" and p.suffix == ".lock"]
        goals = [p for p in files if p.name == "goals_1.sqlite"]
        logs = [p for p in files if p.name == "logs_2.sqlite"]
        if len(locks) != 1 or len(goals) != 1 or len(logs) != 1:
            return None
        sid = str(uuid.UUID(locks[0].stem))
        paths = [locks[0], goals[0], logs[0]]
        if not all(linked(p, files[p]) for p in paths):
            return None
        with closing(sqlite3.connect(goals[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            goal = db.execute("SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=?", (sid,)).fetchone()
        if not goal or goal[1] != "blocked" or not 0 <= time.time() - goal[2] / 1000 < 86400:
            return None
        query = ("SELECT id,ts,ts_nanos,substr(feedback_log_body,1,65537),process_uuid,target FROM logs "
                "WHERE thread_id=? AND ts>=? AND ((target='codex_core::session::turn' "
                "AND instr(feedback_log_body,': Turn error: ')>0) OR "
                "(target='codex_core::session::handlers' AND instr(feedback_log_body,'Submission sub=')>0)) "
                "ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT 65")
        params = (sid, max(process["birth"][0], int(goal[2] / 1000) - 30))
        with closing(sqlite3.connect(logs[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            rows = db.execute(query, params).fetchall()
        row = next((r for r in rows if r[5] == 'codex_core::session::turn'), None)
        if not row or not str(row[4]).startswith(f"pid:{pid}:"):
            return None
        error = re.search(r": Turn error: ([^\n]+)$", row[3] or "")
        turn = re.search(r"(?:^|[\s{])turn\.id=([0-9a-f-]{36})(?:\s|})", row[3] or "")
        at = row[1] + row[2] / 1e9
        born = process["birth"][0] + process["birth"][1] / 1e6
        if (not error or not turn or at < born or not -.001 <= goal[2] / 1000 - at <= 30
                or scope.process(pid) != process or not all(linked(p, files[p]) for p in paths)):
            return None
        following = rows[:rows.index(row)]
        if any(r[4] != row[4] or not _same_turn_steer(r[3] or "", sid, turn[1]) for r in following):
            return None
        # Re-read after log lookup. A manual resume or replacement invalidates
        # this evidence before terminal input can be considered.
        with closing(sqlite3.connect(goals[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            if db.execute("SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=?", (sid,)).fetchone() != goal:
                return None
        with closing(sqlite3.connect(logs[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            if db.execute(query, params).fetchall() != rows:
                return None
        current_files = native.process_writable_files(pid, identities=True)
        if scope.process(pid) != process or any(current_files.get(p) != files[p] or not linked(p, files[p]) for p in paths):
            return None
        return {"kind": "goal_blocked", "session_id": sid, "turn_id": turn[1], "pid": pid,
                "process_start": process["birth"][0], "birth": process["birth"], "at": goal[2] / 1000,
                "goal_id": goal[0], "error": {"message": error[1]}, "log_id": row[0],
                "submission_digest": hashlib.sha256(json.dumps(following).encode()).hexdigest()}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None
