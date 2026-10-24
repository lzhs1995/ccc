"""Bounded, read-only native proof for one current provider StopFailure.

This does not authorize input. ROOT owns live identity, pause, pending/unknown,
current-screen and exact-composer gates. A draft that has not been submitted
changes neither journal, so it does not invalidate this proof.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path

import ccc_claude_closeout as closeout
import claude_ccc_protocol as protocol
import claude_dispatch_recovery as dispatch
from claude_provider_recovery import PROVIDER_KINDS

LIMIT = 4 * 1024 * 1024
IDENTITY = ("surface_id", "workspace_id", "session_id", "agent_pid", "process_generation")


def _number(value):
    if type(value) not in (int, float):
        raise ValueError("invalid epoch")
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid epoch") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid epoch")
    return value


def _epoch(value):
    if not isinstance(value, str):
        raise ValueError("missing native timestamp")
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("naive native timestamp")
        return _number(stamp.timestamp())
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid native timestamp") from exc


def _snapshot(value):
    try:
        return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid evidence object") from exc


def _records(raw, boundary):
    if boundary["bytes_read"] > LIMIT or len(raw) > LIMIT:
        raise ValueError("tail exceeds 4 MiB")
    rows, offset = [], boundary["offset_bytes"]
    for line in raw.splitlines(keepends=True):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid JSONL tail record") from exc
        if not isinstance(row, dict):
            raise ValueError("non-object JSONL tail record")
        rows.append((row, offset, hashlib.sha256(line).hexdigest()))
        offset += len(line)
    return rows


def _synthetic(event):
    return (bool(event.get("synthetic_fallback")) or bool(event.get("synthetic"))
            or event.get("event_name") == "ProviderRetry"
            or any(event.get(k) for k in (
                "provider_migration_source_event_id", "report_migration_source_event_id",
                "dispatch_migration_source_event_id")))


def _diagnostic(failure):
    if (not isinstance(failure, Mapping)
            or failure.get("error_kind") not in PROVIDER_KINDS
            or not isinstance(failure.get("error_reason"), str) or not failure["error_reason"]):
        raise ValueError("not a classified provider diagnostic")


def _unresolved_queue(rows, session):
    # An enqueue/dequeue banner alone is not a native receipt. Only a later
    # exact mainline user record can consume one known queued payload.
    pending = []
    for row, _, _ in rows:
        attachment = row.get("attachment")
        queued = (isinstance(attachment, Mapping)
                  and attachment.get("type") == "queued_command")
        operation = row.get("type") == "queue-operation"
        if queued or operation:
            if row.get("sessionId") != session:
                raise ValueError("queue identity unknown or changed")
            if queued:
                payload = attachment.get("prompt")
                pending.append(payload if isinstance(payload, str) and payload else None)
            else:
                action = row.get("operation")
                if action == "enqueue":
                    payload = row.get("content")
                    pending.append(payload if isinstance(payload, str) and payload else None)
                elif action != "dequeue":
                    raise ValueError("unknown queue operation")
                # A dequeue without the exact native user receipt proves no input.
                elif not pending:
                    raise ValueError("queue consumption predates bounded evidence")
        elif row.get("type") == "user" and row.get("isSidechain") is False:
            if row.get("sessionId") != session:
                raise ValueError("native input session changed")
            message = row.get("message")
            if (any(row.get(key) for key in ("isMeta", "isSynthetic", "isCompactSummary"))
                    or not isinstance(message, Mapping) or message.get("role") != "user"):
                continue
            content = message.get("content")
            if not (isinstance(content, str) or (
                    isinstance(content, list) and content and all(
                        isinstance(block, Mapping) and block.get("type") == "text"
                        and isinstance(block.get("text"), str) for block in content))):
                continue
            text = dispatch._text(row)
            if pending and text is not None and pending[0] == text:
                pending.pop(0)
    if pending:
        raise ValueError("unresolved queued input")


def read_evidence(*, source_event, hook_journal_path, binding, failure) -> dict:
    """Return a stable native_proof, or ValueError; no writes and no input.

    binding: full IDENTITY plus started_epoch and turn_started_at.
    source_event: unchanged original journal StopFailure.
    Transcript discovery uses session_id and the original transcript_id digest.
    """
    try:
        return _read_evidence(source_event, hook_journal_path, binding, failure)
    except (OSError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError("native evidence unavailable or malformed") from exc


def _read_evidence(source_event, hook_journal_path, binding, failure):
    if not isinstance(source_event, Mapping) or not isinstance(binding, Mapping):
        raise ValueError("source/binding must be objects")
    source = _snapshot(dict(source_event))
    identity = {k: binding.get(k) for k in IDENTITY}
    if (type(identity["agent_pid"]) is not int or identity["agent_pid"] <= 0
            or any(not isinstance(identity[k], str) or not identity[k]
                   for k in IDENTITY if k != "agent_pid")):
        raise ValueError("unverified live identity")
    for key in IDENTITY:
        if key == "process_generation" and source.get(key) in (None, ""):
            continue
        if source.get(key) != identity[key]:
            raise ValueError("source identity changed")
    birth, turn = _number(binding.get("started_epoch")), _number(binding.get("turn_started_at"))
    stamp = _number(source.get("created_at"))
    if (source.get("event_name") != "StopFailure" or _synthetic(source)
            or not isinstance(source.get("event_id"), str) or not source["event_id"]
            or source.get("stop_failure_error") not in protocol.STOP_FAILURE_ENUMS
            or source.get("completed") not in (False, None) or stamp < max(birth, turn)):
        raise ValueError("not an original current provider StopFailure")
    _diagnostic(failure)
    for key in ("screen_fingerprint", "error_evidence_hash"):
        if not isinstance(failure.get(key), str) or not failure[key]:
            raise ValueError("current diagnostic lacks fingerprint")
    hook_path = Path(hook_journal_path)
    hook_raw, hook_boundary = closeout.native_tail(hook_path)
    hooks = _records(hook_raw, hook_boundary)
    originals = [(index, row) for index, (row, _, _) in enumerate(hooks)
                 if row.get("surface_id") == identity["surface_id"] and not _synthetic(row)]
    matches = [(index, row) for index, row in originals
               if row.get("event_id") == source.get("event_id")]
    if len(matches) != 1 or matches[0][1] != source:
        raise ValueError("original source is missing, duplicated or changed")
    source_index = matches[0][0]
    if originals[-1][0] != source_index:
        raise ValueError("a later Hook lifecycle owns the surface")
    prior = None
    for index, row in originals:
        hook_stamp = _number(row.get("created_at"))
        if index != source_index:
            if hook_stamp > stamp:
                raise ValueError("Hook chronology conflicts with source")
            prior = hook_stamp if prior is None else max(prior, hook_stamp)
    path = dispatch.native_path(source)
    native_raw, native_boundary = closeout.native_tail(path)
    native = _records(native_raw, native_boundary)
    if not native:
        raise ValueError("empty native tail")
    # Claude 2.1.170 writes a parent-bound turn_duration directly after an API
    # error (verified in the frozen 7063 sample). It contains no input. No other
    # trailing record, including progress/file-history, is silently admitted.
    trailing = None
    error_index = len(native) - 1
    if native[-1][0].get("type") == "system" and native[-1][0].get("subtype") == "turn_duration":
        trailing = native[-1][0]
        error_index -= 1
    if error_index < 0:
        raise ValueError("missing native API error")
    error, offset, row_sha = native[error_index]
    message = error.get("message")
    if (error.get("type") != "assistant" or error.get("isApiErrorMessage") is not True
            or error.get("isSidechain") is not False
            or error.get("sessionId") != identity["session_id"]
            or not isinstance(message, Mapping) or message.get("role") != "assistant"
            or not isinstance(error.get("uuid"), str) or not error["uuid"]
            or error.get("error") not in (None, source["stop_failure_error"])
            or ("apiErrorStatus" in error and (type(error["apiErrorStatus"]) is not int
                or not 400 <= error["apiErrorStatus"] <= 599))):
        raise ValueError("latest native record is not the original mainline API error")
    native_stamp = _epoch(error.get("timestamp"))
    if (not max(birth, turn) <= native_stamp <= stamp
            or (prior is not None and native_stamp <= prior)):
        raise ValueError("native error is outside the source lifecycle interval")
    if trailing is not None:
        allowed = {"parentUuid", "isSidechain", "type", "subtype", "durationMs", "messageCount",
                   "timestamp", "uuid", "isMeta", "userType", "entrypoint", "cwd", "sessionId",
                   "version", "gitBranch", "slug"}
        if (set(trailing) - allowed or trailing.get("parentUuid") != error["uuid"]
                or trailing.get("sessionId") != identity["session_id"]
                or trailing.get("isSidechain") is not False or trailing.get("isMeta") is not False
                or not isinstance(trailing.get("uuid"), str) or not trailing["uuid"]
                or type(trailing.get("durationMs")) is not int or trailing["durationMs"] < 0
                or type(trailing.get("messageCount")) is not int or trailing["messageCount"] < 0
                or not native_stamp <= _epoch(trailing.get("timestamp")) <= stamp):
            raise ValueError("unverified trailing turn bookkeeping")
    content = message.get("content")
    if not (isinstance(content, str) or (
            isinstance(content, list) and content and all(
                isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str) for block in content))):
        raise ValueError("API error contains non-text payload")
    text = dispatch._text(error)
    native_failure = protocol.classify_stop_failure(source["stop_failure_error"], text)
    _diagnostic(native_failure)
    if any(native_failure.get(k) != failure.get(k) for k in ("error_kind", "error_reason")):
        raise ValueError("current screen and native diagnostic differ")
    enum_hash = dispatch.digest(source["stop_failure_error"] + "\n")
    if source.get("message_hash") not in (enum_hash, dispatch.digest(text)):
        raise ValueError("source hash is neither original enum-only nor exact native text")
    _unresolved_queue(native, identity["session_id"])
    # Reject same-session message records with ambiguous mainline identity.
    for row, _, _ in native:
        if row.get("type") in ("user", "assistant") and row.get("isSidechain") is not True:
            if row.get("sessionId") != identity["session_id"] or row.get("isSidechain") is not False:
                raise ValueError("native mainline identity is ambiguous")
    if (dispatch.fingerprint(hook_path) != hook_boundary["stat"]
            or dispatch.fingerprint(path) != native_boundary["stat"]):
        raise ValueError("source changed during cross-journal validation")
    return {"version": "provider-native-generic-v1", "identity": identity, "started_epoch": birth,
            "turn_started_at": turn, "source_event_id": source["event_id"],
            "source_created_at": stamp, "hook_boundary": hook_boundary,
            "native_boundary": native_boundary, "native_error_uuid": error["uuid"],
            "native_error_created_at": native_stamp, "native_error_offset_bytes": offset,
            "native_error_record_sha256": row_sha,
            "native_error_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "error_kind": native_failure["error_kind"], "error_reason": native_failure["error_reason"],
            "source_hash_binding": "enum_only" if source["message_hash"] == enum_hash else "exact_text"}


def current(proof, *, source_event, hook_journal_path, binding, failure) -> bool:
    """Repeat bounded verification; any append/replacement/boundary change fails."""
    try:
        return proof == read_evidence(source_event=source_event, hook_journal_path=hook_journal_path,
                                      binding=binding, failure=failure)
    except (ValueError, TypeError):
        return False
