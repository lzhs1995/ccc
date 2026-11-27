"""One durable ledger transaction for a proved legacy provider retry.

No terminal input. revalidate must recheck original Hook/native boundaries,
current identity, target/manual pause, pending/unknown, and diagnostic under
this call. The returned guard only schedules normal guarded sender handling.
"""
from __future__ import annotations

import claude_provider_recovery as recovery


def commit(proof, *, ledger, revalidate, atomic_write_json, retry_after):
    """Atomically store retry guard and event; fail without in-memory mutation.

    ledger is the existing ClaudeEventLedger, not a new database. The provided
    atomic_write_json is its existing fsync/replace writer. Never split this
    operation into put_provider_guard then mark. A retry id already present is
    always consumed for migration purposes, including uncertain outcomes.
    """
    saved = recovery._object(proof, "proof")
    if saved.get("version") != recovery.VERSION:
        raise ValueError("wrong migration proof version")
    deadline = recovery._number(retry_after, "retry_after")
    if deadline < saved["observed_at"]:
        raise ValueError("retry deadline precedes observation")
    source, event = saved["source_event"], saved["resume_event"]
    surface, event_id = saved["identity"]["surface_id"], saved["resume_event_id"]
    if event.get("event_id") != event_id:
        raise ValueError("retry id changed")
    with ledger._lock:
        if revalidate(saved) is not True:
            raise ValueError("current source or protected state changed")
        guards = recovery._copy(ledger.provider_guards)
        events = recovery._copy(ledger.events)
        old = guards.get(surface)
        row = events.get(source["event_id"])
        if old != saved["guard"] or event_id in events:
            raise ValueError("guard changed or retry id already consumed")
        if not isinstance(row, dict) or row.get("status") not in {"provider_blocked", "handling"}:
            raise ValueError("source is missing, consumed or outcome unknown")
        for key in ("event_name", "surface_id", "workspace_id", "session_id", "agent_pid", "created_at"):
            if row.get(key) != source.get(key):
                raise ValueError("ledger source identity changed")
        if row.get("process_generation") not in (None, "", saved["identity"]["process_generation"]):
            raise ValueError("ledger source process changed")
        revision = old.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid old guard revision")
        guard = {**old, "revision": revision + 1, "status": "retry_wait",
                 "source_event_id": source["event_id"], "source_created_at": source["created_at"],
                 "error_kind": event["error_kind"], "error_reason": event["error_reason"],
                 "stop_failure_error": event["stop_failure_error"], "provider_blocked": False,
                 "retry_count": 1, "retry_after": deadline, "retry_event": event,
                 "evidence_source": "hook", "notification_planned": False,
                 "provider_migration": saved}
        guards[surface] = guard
        events[event_id] = {**event, "status": "provider_retry_wait"}
        if revalidate(saved) is not True:
            raise ValueError("source changed before durable transaction")
        atomic_write_json(ledger.path, {"version": 2, "events": events, "provider_guards": guards})
        ledger.events, ledger.provider_guards = events, guards
        return recovery._copy(guard)
