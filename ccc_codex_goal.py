"""Read-only evidence for resuming an existing goal after rollout corruption.

This never fabricates task_complete. The resulting action reactivates an
existing blocked goal through the native StartIfIdle continuation mechanism.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import re
import sqlite3
import time
import uuid

import ccc_codex_queue as native
import ccc_guard_scope as scope


def linked(path, identity):
    info = Path(path).stat()
    return info.st_dev == identity["device"] and info.st_ino == identity["inode"]


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
        with closing(sqlite3.connect(logs[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            row = db.execute("SELECT id,ts,ts_nanos,feedback_log_body,process_uuid FROM logs "
                "WHERE thread_id=? AND ts>=? AND ((target='codex_core::session::turn' "
                "AND instr(feedback_log_body,': Turn error: ')>0) OR "
                "(target='codex_core::session::handlers' AND instr(feedback_log_body,'Submission sub=')>0)) "
                "ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT 1",
                (sid, max(process["birth"][0], int(goal[2] / 1000) - 30))).fetchone()
        if not row or not str(row[4]).startswith(f"pid:{pid}:"):
            return None
        error = re.search(r": Turn error: ([^\n]+)$", row[3] or "")
        turn = re.search(r"(?:^|[\s{])turn\.id=([0-9a-f-]{36})(?:\s|})", row[3] or "")
        at = row[1] + row[2] / 1e9
        born = process["birth"][0] + process["birth"][1] / 1e6
        if (not error or not turn or at < born or not -.001 <= goal[2] / 1000 - at <= 30
                or scope.process(pid) != process or not all(linked(p, files[p]) for p in paths)):
            return None
        # Re-read after log lookup. A manual resume or replacement invalidates
        # this evidence before terminal input can be considered.
        with closing(sqlite3.connect(goals[0].as_uri() + "?mode=ro", uri=True, timeout=.05)) as db:
            if db.execute("SELECT goal_id,status,updated_at_ms FROM thread_goals WHERE thread_id=?", (sid,)).fetchone() != goal:
                return None
        return {"kind": "goal_blocked", "session_id": sid, "turn_id": turn[1], "pid": pid,
                "process_start": process["birth"][0], "birth": process["birth"], "at": goal[2] / 1000,
                "goal_id": goal[0], "error": {"message": error[1]}, "log_id": row[0]}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None
