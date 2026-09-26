"""Read-only terminal identity, observation coverage and registration evidence.

No terminal input or configuration writes belong in this module. The watcher
owns the verdict; controllers only project its public fields.
"""
from __future__ import annotations

import errno
import math
import subprocess
from collections.abc import Mapping
from typing import Any


HEALTH_PUBLISH_INTERVAL_SEC = 1.0


def transient_observation_error(error: BaseException | str) -> bool:
    """Recognize a failed read transport, never an input delivery outcome.

    CLI errors carry text; wrapped OS errors also carry a cause. Explicit target
    identity failures take precedence over either transient representation.
    """
    # Bound traversal, including cycles; wrappers may use either exception link.
    chain, pending, seen = [], [error], set()
    while pending and len(chain) < 16:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        chain.append(cause)
        for link in ("__cause__", "__context__"):
            child = getattr(cause, link, None)
            if child is not None:
                pending.append(child)
    detail = "\n".join(str(cause).lower() for cause in chain)
    if any(marker in detail for marker in (
        "surface not found", "surface_not_found", "workspace not found",
        "workspace_not_found", "not a terminal", "invalid_params", "identity mismatch",
    )):
        return False
    for cause in chain:
        if isinstance(cause, (subprocess.TimeoutExpired, TimeoutError, ConnectionError)):
            return True
        if isinstance(cause, OSError) and cause.errno in {
            errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED,
            errno.ECONNABORTED, errno.ENOTCONN, errno.ETIMEDOUT,
        }:
            return True
    return any(marker in detail for marker in (
        "failed to read terminal text", "command timed out", " timed out",
        "broken pipe", "connection reset by peer", "connection refused",
        "connection aborted", "socket is not connected", "no live cmux socket found",
        "socket closed before reply", "connection closed before reply",
        "socket read error",
    ))


def pause_health(target):
    """A transport message alone cannot establish who requested a pause."""
    if not target.get("enabled", True):
        return "paused", "explicitly_paused_or_disabled"
    origin = str(target.get("pause_origin") or "")
    if origin.startswith("automatic_"):
        return "unavailable", "paused_after_observation_error"
    if origin != "user" and transient_observation_error(str(target.get("paused_reason") or "")):
        return "unknown", "pause_origin_unverified"
    return "paused", "explicitly_paused_or_disabled"


def objects(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def surface_entries(value: Any, workspace_id: str = ""):
    if isinstance(value, Mapping):
        if value.get("kind") == "workspace":
            workspace_id = str(value.get("id") or value.get("workspace_id") or "")
        if value.get("kind") == "surface":
            yield value, str(value.get("workspace_id") or workspace_id)
        for child in value.values():
            yield from surface_entries(child, workspace_id)
    elif isinstance(value, list):
        for child in value:
            yield from surface_entries(child, workspace_id)


def attributable_processes(surface: Mapping[str, Any], workspace_id: str = ""):
    """Reject foreign CMUX identity, including descendants of foreign roots.

    TTY reuse can put another terminal's entire process tree under an old slot.
    Missing identity remains advisory; it never becomes explicit ownership.
    """
    sid = str(surface.get("id") or surface.get("surface_id") or "")
    processes = [p for p in objects(surface.get("processes", [])) if p.get("kind") == "process"]
    rejected = set()
    for p in processes:
        psid, pwid = p.get("cmux_surface_id"), p.get("cmux_workspace_id")
        if (psid and sid and psid != sid) or (pwid and workspace_id and pwid != workspace_id):
            rejected.add(p.get("pid"))
    changed = True
    while changed:
        changed = False
        for p in processes:
            if p.get("ppid") in rejected and p.get("pid") not in rejected:
                rejected.add(p.get("pid"))
                changed = True
    return [p for p in processes if p.get("pid") not in rejected], len(rejected)


def native_runtime_ready(terminal: Mapping[str, Any]) -> bool | None:
    value = terminal.get("runtime_surface_ready")
    pointer = terminal.get("ghostty_surface_ptr")
    present = str(pointer or "").strip().lower() not in {"", "nil", "null", "none", "0", "0x0"}
    if type(value) is not bool:
        return None
    if "ghostty_surface_ptr" in terminal and value != present:
        return None  # Contradictory snapshots cannot prove a dormant terminal.
    return value


def observation_row(target, record, process, terminal, runtime, *, owner_alive, now, stale_after,
                    inventory_complete=True):
    sid, wid = str(target["surface_id"]), str(target["workspace_id"])
    native = native_runtime_ready(terminal)
    kind = str(process.get("agent_kind") or "unknown")
    pid = int(process.get("agent_pid") or 0)
    explicit = pid > 0 and pid in process.get("identity_verified_pids", [])
    live_agent = pid > 0 and kind not in {"shell", "other", "unknown"} and (explicit or owner_alive is True)
    checked = float(runtime.get("viewport_checked_at") or 0)
    fresh = checked > 0 and 0 <= now - checked <= stale_after
    row = {
        "surface_id": sid, "workspace_id": wid,
        "agent_kind": kind, "agent_pid": pid,
        "runtime_ready": native, "observed_at": now,
        "viewport_checked_at": checked,
        "viewport_source": str(runtime.get("viewport_source") or ""),
        "status": "unknown", "reason_code": "identity_or_observation_unknown",
    }
    # Historical automatic pauses say why observation stopped, not whether the
    # terminal still exists. Current UUID/owner evidence must outrank that flag.
    if record is None:
        if live_agent or owner_alive is True:
            row.update(status="live_unreadable", reason_code="live_owner_without_surface")
        elif inventory_complete and owner_alive is False and not pid:
            row.update(status="missing", reason_code="surface_closed_owner_exited")
    elif str(record.get("workspace_id") or "") != wid:
        row["reason_code"] = "workspace_identity_mismatch"
    elif (inventory_complete and process.get("process_snapshot_present")
          and not process.get("identity_conflicts") and owner_alive is False and not pid
          and kind in {"shell", "other"}):
        row.update(status="dormant", reason_code="no_supported_agent")
    elif target.get("paused") or not target.get("enabled", True):
        health, reason = pause_health(target)
        row.update(status="live_unreadable" if health == "unavailable" else health, reason_code=reason)
    elif native is False:
        if live_agent or owner_alive is True:
            row.update(status="live_unreadable", reason_code="live_owner_runtime_uninitialized")
        elif (inventory_complete and process.get("process_snapshot_present")
              and not process.get("identity_conflicts") and owner_alive is False and not pid
              and kind in {"shell", "other", "unknown"}):
            row.update(status="dormant", reason_code="runtime_uninitialized_no_agent")
    elif not fresh:
        row["reason_code"] = "viewport_observation_stale"
    elif runtime.get("viewport_readable") is True:
        if pid and kind not in {"shell", "other", "unknown"} and not live_agent:
            row["reason_code"] = "agent_ownership_unverified"
        elif owner_alive is None:
            row["reason_code"] = "persisted_owner_unverified"
        else:
            row.update(status="readable", reason_code="current_viewport_available")
    elif live_agent or owner_alive is True:
        row.update(status="live_unreadable", reason_code="current_viewport_unavailable")
    return row


def summarize_observation(rows, *, now, stale_after):
    counts = {key: 0 for key in ("readable", "live_unreadable", "dormant", "paused", "missing", "unknown")}
    projected = []
    for original in rows:
        row = dict(original)
        if row.get("status") != "paused" and not 0 <= now - float(row.get("observed_at") or 0) <= stale_after:
            row.update(status="unknown", reason_code="diagnostic_snapshot_stale")
        status = row.get("status", "unknown")
        counts[status if status in counts else "unknown"] += 1
        projected.append(row)
    status = "degraded" if counts["live_unreadable"] else "unknown" if counts["unknown"] else "ok"
    return {"status": status, "scope": "authorized_targets", "observed_at": now,
            "stale_after_sec": stale_after, "counts": counts, "targets": projected}


def continuation_row(target, runtime, *, now, poll_interval=1.0, observation=None, published_at=None):
    """Current scheduling/delivery health; enabling monitoring is not freshness."""
    checked = float(runtime.get("viewport_checked_at") or 0)
    age = now - checked if checked > 0 else None
    status, reason = "ok", "current_observation"
    phase = str(runtime.get("state") or "unknown")
    delivery = str(runtime.get("delivery_status") or "")
    cadence = runtime.get("observation_cadence_sec", poll_interval)
    if type(cadence) not in (int, float) or not math.isfinite(cadence) or cadence < poll_interval:
        cadence = poll_interval
    # Only the scheduler's bounded native-monitor lease permits the 10s scan.
    cadence = min(cadence, max(10.0, poll_interval))
    observed = observation if isinstance(observation, Mapping) else {}
    current = (observed.get("surface_id") == target["surface_id"]
               and observed.get("workspace_id") == target["workspace_id"]
               and 0 <= now - float(observed.get("observed_at") or 0) <= max(30.0, 3 * poll_interval))
    deadline_age, publication_age = age, None
    # The producer evaluates the actual observation deadline. Readers see a
    # periodically published snapshot: adding its transport age to that same
    # deadline invents misses between healthy publications. Credit only the
    # exact UUID-bound observation, for at most two publication intervals.
    # Keep send-ack deadlines on the reader's real clock; never cache a verdict
    # indefinitely or make an already-late producer observation look healthy.
    published_checked = observed.get("viewport_checked_at")
    if (current and observed.get("status") == "readable"
            and type(published_checked) in (int, float) and math.isfinite(published_checked)
            and published_checked >= checked and published_checked > 0
            and type(published_at) in (int, float) and math.isfinite(published_at)
            and published_checked <= published_at <= now
            and now - published_at <= 2 * HEALTH_PUBLISH_INTERVAL_SEC):
        # State and health use independent atomic writers. A newer verified
        # observation must not be replaced by an older durable state snapshot.
        checked = published_checked
        age = now - checked
        deadline_age = published_at - checked
        publication_age = now - published_at
        reason = "current_published_observation"
    if current and observed.get("status") == "missing":
        status, reason = "missing", "surface_closed_owner_exited"
    elif current and observed.get("status") == "dormant":
        status, reason = "inactive", str(observed.get("reason_code") or "no_supported_agent")
    elif not target.get("enabled", True):
        status, reason = "paused", "explicitly_paused_or_disabled"
    elif target.get("paused"):
        status, reason = pause_health(target)
    elif age is None or age < 0:
        status, reason = "unknown", "no_current_observation"
    elif deadline_age > 2 * cadence:
        status, reason = "delayed", "observation_deadline_missed"
    elif delivery == "unknown":
        status, reason = "delivery_unknown", "send_receipt_unconfirmed"
    elif delivery == "sending" and now - float(runtime.get("send_started_at") or 0) > 2 * poll_interval:
        status, reason = "delivery_unknown", "send_acknowledgement_overdue"
    elif delivery == "failed":
        status, reason = "send_failed", "cmux_rejected_send"
    elif phase in {"cmux_unavailable", "send_guard_unavailable", "incompatible", "claude_viewport_blind"}:
        status, reason = "unavailable", phase
    elif phase in {"provider_blocked", "token_exhausted", "network_wait"}:
        status, reason = "blocked", str(runtime.get("observed_error_type") or runtime.get("error_type") or phase)
    elif phase in {"claude_hook_missing", "claude_hook_unverified", "claude_hook_legacy"}:
        status, reason = "unknown", phase
    elif phase in {"claude_hook_config_degraded", "claude_hook_gap_exhausted", "claude_model_unavailable"}:
        status, reason = "blocked", phase
    elif phase in {"error_superseded", "queued_followup"}:
        # Waiting on the agent is not a failed watcher. This explicitly does
        # not claim successful model access, task completion or queue delivery.
        status, reason = "waiting", "previous_error_superseded" if phase == "error_superseded" else "followup_queued"
    elif phase in {"unknown", "queue_recovery_submitted"}:
        status, reason = "unknown", "task_progress_unconfirmed"
    elif phase == "missing_or_error":
        status, reason = "unavailable", phase
    elif phase == "queue_recovery_unconfirmed":
        status, reason = "delivery_unknown", phase
    return {
        "surface_id": str(target["surface_id"]), "workspace_id": str(target["workspace_id"]),
        "status": status, "reason_code": reason, "state": phase,
        "viewport_checked_at": checked, "observation_age_sec": round(age, 3) if age is not None else None,
        "observation_deadline_age_sec": round(deadline_age, 3) if deadline_age is not None else None,
        "publication_age_sec": round(publication_age, 3) if publication_age is not None else None,
        "poll_interval_sec": poll_interval,
        "observation_cadence_sec": cadence,
        "observation_interval_ms": runtime.get("observation_interval_ms", 0),
        "scheduler_lag_ms": runtime.get("scheduler_lag_ms", 0),
        "read_duration_ms": runtime.get("read_duration_ms", 0),
        "send_queue_ms": runtime.get("send_queue_ms", 0),
        "send_duration_ms": runtime.get("send_duration_ms", 0),
        "send_persist_duration_ms": runtime.get("send_persist_duration_ms", 0),
        "detection_to_send_ms": runtime.get("detection_to_send_ms", 0),
        "delivery_status": delivery, "send_started_at": runtime.get("send_started_at", 0),
        "send_io_started_at": runtime.get("send_io_started_at", 0),
        "send_completed_at": runtime.get("send_completed_at", 0),
        "last_send_error": runtime.get("last_send_error", ""),
    }


def continuation_report(targets, runtime, *, now, poll_interval=1.0, observations=(), published_at=None):
    by_id = {r.get("surface_id"): r for r in observations if isinstance(r, Mapping)}
    rows = [continuation_row(t, runtime.get(str(t["surface_id"]), {}), now=now, poll_interval=poll_interval,
                             observation=by_id.get(t["surface_id"]), published_at=published_at)
            for t in targets]
    counts = {key: 0 for key in ("ok", "paused", "unknown", "delayed", "delivery_unknown",
                               "send_failed", "unavailable", "blocked", "missing", "inactive", "waiting")}
    for row in rows:
        counts[row["status"]] += 1
    bad = sum(counts[key] for key in ("delayed", "delivery_unknown", "send_failed", "unavailable", "blocked"))
    return {"status": "degraded" if bad else "unknown" if counts["unknown"] else "ok",
            "observed_at": now, "scope": "authorized_targets", "counts": counts, "targets": rows}


def registration_candidate(events, target, observation, runtime, ledger, *, now, max_age):
    """Return one original, genuine Stop; no evidence is synthesized here."""
    sid, wid = target["surface_id"], target["workspace_id"]
    relevant = []
    for event in events:
        if event.get("surface_id") != sid or event.get("synthetic_fallback"):
            continue
        at = event.get("created_at")
        if type(at) not in (int, float) or not math.isfinite(at) or at <= 0 or at > now:
            return None, "event_time_unverified"
        relevant.append(event)
    if not relevant:
        return None, "no_registration_stop"
    latest = max(enumerate(relevant), key=lambda item: (item[1]["created_at"], item[0]))[1]
    if latest.get("event_name") not in {"Stop", "StopFailure"} or latest.get("completed"):
        return None, "latest_event_not_unfinished_stop"
    if latest.get("workspace_id") != wid or now - latest["created_at"] > max_age:
        return None, "stop_identity_or_age_mismatch"
    row = ledger.get(latest.get("event_id"), {})
    rejected = row.get("status") == "unmapped" and row.get("detail") == "surface is not authorized"
    interrupted = row.get("status") == "handling" and bool(row.get("registration_revalidated_at"))
    if not (rejected or interrupted):
        return None, "stop_not_rejected_for_authorization"
    pid = latest.get("agent_pid")
    members = observation.get("identity_verified_pids", [])
    if type(pid) is not int or pid <= 0 or pid not in members:
        return None, "stop_process_ownership_unverified"
    started = float(observation.get("event_pid_started_epoch") or 0)
    if not started or started > latest["created_at"] or not observation.get("generation"):
        return None, "stop_process_generation_unverified"
    session = latest.get("session_id")
    if not session or runtime.get("claude_session_id") not in (None, "", session):
        return None, "stop_session_mismatch"
    starts = [e for e in relevant if e.get("event_name") == "SessionStart"
              and e.get("session_id") == session and e.get("workspace_id") == wid
              and e.get("agent_pid") == pid and started <= e["created_at"] <= latest["created_at"]]
    if not starts:
        return None, "session_start_unverified"
    if runtime.get("claude_completed_latched") or runtime.get("claude_submit_phase", "none") != "none":
        return None, "completed_or_submission_in_flight"
    if float(runtime.get("last_send_at") or 0) >= latest["created_at"]:
        return None, "stop_covered_by_later_send"
    # A row for the same id but another identity must never be reclaimed.
    for key in ("surface_id", "session_id", "event_name", "workspace_id", "agent_pid", "created_at"):
        if (key in {"surface_id", "session_id", "event_name"} or key in row) and row.get(key) != latest.get(key):
            return None, "ledger_identity_mismatch"
    return dict(latest), "eligible_registration_stop"
