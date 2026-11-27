"""Pure proposals for current-screen legacy provider retry migration.

The caller authenticates journal membership, process birth and native_proof,
and enforces pause/pending/unknown/composer/transport gates. This module does
no I/O, changes no input object, and never grants permission to send by itself.
latest_events is an ordered collection of original, normalized lifecycle events.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

VERSION = "provider-legacy-retry-v1"
PROVIDER_KINDS = frozenset({"claude_auth", "claude_billing", "claude_model_unavailable",
    "claude_request_invalid", "claude_group_deleted", "claude_429", "claude_503",
    "claude_api", "claude_stream"})
IDENTITY_KEYS = ("surface_id", "workspace_id", "session_id", "agent_pid",
                 "process_generation")


def _copy(value):
    try:
        return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("proof inputs must be finite JSON values") from exc


def _object(value, name):
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a nonempty object")
    return _copy(dict(value))


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _identity(value):
    result = {}
    for key in IDENTITY_KEYS:
        item = value.get(key)
        if key == "agent_pid":
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValueError("agent_pid must be a positive integer")
        elif not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} is missing")
        result[key] = item
    return result


def _same_identity(value, identity, name):
    if _identity(value) != identity:
        raise ValueError(f"{name} identity changed")


def _synthetic(event):
    return (bool(event.get("synthetic_fallback")) or bool(event.get("synthetic"))
            or event.get("event_name") == "ProviderRetry"
            or any(event.get(key) for key in (
                "report_migration_source_event_id",
                "dispatch_migration_source_event_id",
                "provider_migration_source_event_id")))


def _latest(source, events, identity):
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("latest_events must be an ordered sequence")
    candidates = []
    for index, item in enumerate(events):
        if not isinstance(item, Mapping):
            raise ValueError("malformed lifecycle event")
        if item.get("surface_id") != identity["surface_id"] or _synthetic(item):
            continue
        stamp = _number(item.get("created_at"), "lifecycle created_at")
        candidates.append((stamp, index, item))
    matching = [item for _, _, item in candidates
                if item.get("event_id") == source["event_id"]]
    if len(matching) != 1 or _copy(dict(matching[0])) != source:
        raise ValueError("source must occur exactly once, unchanged, in original journal events")
    if (not candidates or candidates[-1][2].get("event_id") != source["event_id"]
            or any(stamp > source["created_at"] for stamp, _, _ in candidates)):
        raise ValueError("source is superseded by a later lifecycle")


def _failure(value):
    failure = _object(value, "failure")
    if (failure.get("error_kind") not in PROVIDER_KINDS
            or not isinstance(failure.get("error_reason"), str) or not failure["error_reason"]):
        raise ValueError("current evidence is not a provider diagnostic")
    failure["provider_blocked"] = False  # Classification does not grant a permanent hold.
    for key in ("screen_fingerprint", "error_evidence_hash"):
        if not isinstance(failure.get(key), str) or not failure[key]:
            raise ValueError(f"current evidence lacks {key}")
    return failure


def _binding(value):
    binding = _object(value, "binding")
    identity = _identity(binding)
    turn = _number(binding.get("turn_started_at"), "turn_started_at")
    # Optional caller guard facts can only narrow admission, never clear state.
    for key in ("paused", "global_paused", "write_unknown", "submit_write_unknown",
                "pending", "completed", "handshake_wait", "supervisor_wait"):
        if binding.get(key):
            raise ValueError(f"binding is protected by {key}")
    if binding.get("submit_phase", "none") != "none":
        raise ValueError("binding has an existing submit transaction")
    return identity, turn


def propose(*, guard, source_event, latest_events, target, binding, failure,
            native_proof, observed_at) -> dict:
    """Return a detached proof and ProviderRetry event, or fail closed.

    target requires surface_id/workspace_id; binding, source_event and guard
    require all IDENTITY_KEYS, except an original source may omit generation
    when binding.started_epoch proves it occurred after the current PID birth.
    process_generation must identify PID plus birth.
    native_proof is a nonempty JSON boundary authenticated by the caller.
    """
    old_guard = _object(guard, "guard")
    source = _object(source_event, "source_event")
    selected = _object(target, "target")
    identity, turn = _binding(binding)
    _same_identity(old_guard, identity, "guard")
    birth = None
    if source.get("process_generation") in (None, ""):
        birth = _number(binding.get("started_epoch"), "started_epoch")
        _same_identity({**source, "process_generation": identity["process_generation"]},
                       identity, "source")
    else:
        _same_identity(source, identity, "source")
    if (selected.get("surface_id") != identity["surface_id"]
            or selected.get("workspace_id") != identity["workspace_id"]):
        raise ValueError("target identity changed")
    for key in IDENTITY_KEYS[2:]:
        if key in selected and selected[key] != identity[key]:
            raise ValueError("target identity changed")
    if selected.get("paused") or selected.get("enabled", True) is not True:
        raise ValueError("target is paused or disabled")
    if (old_guard.get("status") != "blocked" or old_guard.get("invalid")
            or old_guard.get("error_kind") not in PROVIDER_KINDS):
        raise ValueError("only a valid legacy provider hold can be migrated")
    if (source.get("event_name") != "StopFailure" or _synthetic(source)
            or source.get("completed") not in (None, False)):
        raise ValueError("source must be an original incomplete StopFailure")
    if not isinstance(source.get("event_id"), str) or not source["event_id"]:
        raise ValueError("source event_id is missing")
    if not isinstance(source.get("stop_failure_error"), str) or not source["stop_failure_error"]:
        raise ValueError("source StopFailure enum is missing")
    if source.get("error_kind") not in PROVIDER_KINDS:
        raise ValueError("source is not a provider failure")
    old_stamp = _number(old_guard.get("source_created_at"), "guard source_created_at")
    old_id = old_guard.get("source_event_id")
    if not isinstance(old_id, str) or not old_id:
        raise ValueError("guard source_event_id is missing")
    stamp = _number(source.get("created_at"), "source created_at")
    if birth is not None and stamp < birth:
        raise ValueError("source predates authenticated current PID birth")
    if ((source["event_id"] == old_id and stamp != old_stamp)
            or (source["event_id"] != old_id and stamp <= old_stamp)
            or stamp < turn):
        raise ValueError("source predates guard or current turn")
    _latest(source, latest_events, identity)
    current_failure = _failure(failure)
    boundary = _object(native_proof, "native_proof")
    observed = _number(observed_at, "observed_at")
    if observed < stamp:
        raise ValueError("observation predates source")
    stable = {**identity, "source_event_id": source["event_id"], "turn_started_at": turn}
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    resume_id = VERSION + "-" + hashlib.sha256(encoded).hexdigest()
    event = {**identity, "event_id": resume_id, "episode_id": resume_id,
             "event_name": "ProviderRetry", "created_at": stamp, "completed": False,
             "provider_migration_source_event_id": source["event_id"],
             "provider_migration_turn_started_at": turn,
             "error_kind": current_failure["error_kind"], "error_reason": current_failure["error_reason"],
             "stop_failure_error": source["stop_failure_error"],
             "provider_blocked": False,
             "error_evidence_hash": current_failure["error_evidence_hash"]}
    return {"version": VERSION, "guard": old_guard, "source_event": source,
            "identity": identity, "turn_started_at": turn, "started_epoch": birth,
            "native_proof": boundary,
            "failure": current_failure, "observed_at": observed,
            "resume_event_id": resume_id, "resume_event": event}


def current(proof, latest_events, binding, native_proof) -> bool:
    """Recheck immutable proof, same live identity/turn and unchanged boundary.

    The caller must also freshly recheck the exact current screen diagnostic.
    """
    try:
        saved = _object(proof, "proof")
        identity, turn = _binding(binding)
        if (saved.get("version") != VERSION or saved.get("identity") != identity
                or saved.get("turn_started_at") != turn
                or saved.get("native_proof") != _object(native_proof, "native_proof")):
            return False
        rebuilt = propose(guard=saved["guard"], source_event=saved["source_event"],
                          latest_events=latest_events, target=identity, binding=binding,
                          failure=saved["failure"], native_proof=native_proof,
                          observed_at=saved["observed_at"])
        return rebuilt == saved
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
