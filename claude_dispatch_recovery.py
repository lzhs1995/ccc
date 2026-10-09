"""Read-only evidence for repairing a legacy, incorrectly retained handshake.

This module never replays Hooks, changes their ledger rows, or writes input.
The daemon owns the reservation and its normal Text/Enter guards.
"""
from __future__ import annotations

import datetime as dt
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from claude_ccc_protocol import completion_reported, task_dispatch

PROJECTS_DIR = Path.home() / ".claude" / "projects"
JOURNAL_LIMIT = 64 * 1024 * 1024
TRANSCRIPT_LIMIT = 128 * 1024 * 1024
DOCUMENT_LIMIT = 1024 * 1024
STAT_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]


def fingerprint(path: Path) -> list[int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("source is not a regular, non-symlink file")
    return [getattr(value, name) for name in STAT_FIELDS]


def read_file(path: Path, limit: int) -> tuple[bytes, list[int]]:
    before = fingerprint(path)
    if before[2] > limit:
        raise ValueError("source exceeds the bounded read")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        opened = os.fstat(stream.fileno())
        if [getattr(opened, name) for name in STAT_FIELDS] != before:
            raise ValueError("source replaced before read")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(data) > limit or [getattr(after, name) for name in STAT_FIELDS] != before
                or fingerprint(path) != before):
            raise ValueError("source changed during read")
    return data, before


def journal_events(path: Path, surface: str, since: float,
                   cache: dict) -> list[dict[str, Any]]:
    """Cache a stable read, not a guess based on an increasing file length."""
    key = (str(path), tuple(fingerprint(path)), surface, since)
    if cache.get("key") == key:
        return cache["events"]
    data, signature = read_file(path, JOURNAL_LIMIT)
    events = []
    needle = surface.encode()
    for line in data.splitlines():
        if needle not in line:
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or event.get("surface_id") != surface:
            continue
        stamp = event.get("created_at")
        if type(stamp) not in (float, int) or not math.isfinite(stamp):
            raise ValueError("invalid native Hook timestamp")
        if stamp >= since:
            events.append(event)
    cache.update(key=(str(path), tuple(signature), surface, since), events=events)
    return events


def _text(record: Mapping[str, Any]) -> str | None:
    message = record.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("invalid native message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(isinstance(block, dict) for block in content):
        texts = [block for block in content if block.get("type") == "text"]
        # Assistant messages can contain thinking/tool blocks alongside their
        # actual reply. Completion must still be detected in that reply. A
        # dispatch, however, must be the exact plain user message we hashed.
        if (texts and all(isinstance(block.get("text"), str) for block in texts)
                and (len(texts) == len(content) or record.get("type") == "assistant")):
            return "\n".join(block["text"] for block in texts)
    return None


def native_path(event: Mapping[str, Any]) -> Path:
    session = event["session_id"]
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session):
        raise ValueError("invalid native session id")
    matches = []
    for number, path in enumerate(PROJECTS_DIR.glob(f"*/{session}.jsonl")):
        if number >= 4096:
            raise ValueError("too many native transcript candidates")
        if digest(str(path)) == event.get("transcript_id"):
            matches.append(path)
    if len(matches) != 1:
        raise ValueError("native transcript pathname does not match the Hook")
    return matches[0]


def _native_dispatch(event: dict, stop: dict | None, *, previous: dict | None = None) -> tuple[dict, dict]:
    session = event["session_id"]
    path = native_path(event)
    data, signature = read_file(path, TRANSCRIPT_LIMIT)
    if previous and (str(path) != previous["path"] or signature[:2] != previous["stat"][:2]
                     or len(data) < previous["stat"][2]
                     or hashlib.sha256(data[:previous["stat"][2]]).hexdigest() != previous["sha256"]):
        raise ValueError("native transcript is not an append of the proven original")
    prompts, replies = {}, {}
    dispatched = False
    for line in data.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("invalid native transcript record")
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain") is True:
            continue
        if row.get("sessionId") != session or row.get("isSidechain") is not False:
            raise ValueError("native transcript session changed")
        text = _text(row)
        message = row["message"]
        content = message.get("content")
        # Tool results and native compact summaries are not a new human turn.
        # Mixed text/tool records do contain new input, even if _text cannot
        # represent the whole message as one plain string.
        has_user_text = isinstance(content, str) or (isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "text" for block in content))
        compact = (row.get("isCompactSummary") is True
                   and row.get("isVisibleInTranscriptOnly") is True)
        if dispatched and row.get("type") == "user" and has_user_text and not compact:
            raise ValueError("a newer native prompt owns the session")
        if (dispatched and row.get("type") == "assistant" and text
                and completion_reported(text)):
            raise ValueError("a newer native completion owns the session")
        if text is None:
            continue
        if (row.get("type") == "user" and message.get("role") == "user"
                and digest(text) == event.get("message_hash")):
            stamp = dt.datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).timestamp()
            origin = row.get("origin")
            if origin is not None and not isinstance(origin, Mapping):
                raise ValueError("invalid native prompt origin")
            if (not row.get("uuid") or abs(stamp - event["created_at"]) > 2
                    or row.get("isMeta") or row.get("isCompactSummary")
                    or row.get("promptSource") not in (None, "typed")
                    or (origin or {}).get("kind") not in (None, "human")):
                raise ValueError("prompt is not the original native dispatch")
            prompts[row["uuid"]] = text
            dispatched = True
        if (dispatched and stop and stop.get("event_name") == "Stop" and row.get("type") == "assistant"
                and digest(text) == stop.get("message_hash")):
            replies[row.get("uuid")] = text
    if len(prompts) != 1:
        raise ValueError("missing or ambiguous native dispatch prompt")
    dispatch = task_dispatch(next(iter(prompts.values())))
    if not dispatch:
        raise ValueError("native prompt is not a supported finalized dispatch")
    if stop and stop.get("event_name") == "Stop":
        if not replies or any(completion_reported(text) for text in replies.values()):
            raise ValueError("unfinished Stop is not proven in the native transcript")
    return dispatch, {"path": str(path), "stat": signature,
                      "sha256": hashlib.sha256(data).hexdigest(),
                      "prompt_uuid": next(iter(prompts)), "message_hash": event["message_hash"]}


def _task_files(challenge: dict, dispatch: dict, target: Mapping[str, Any]) -> list[dict]:
    receipt_path = Path(challenge["receipt_path"])
    pack_path = receipt_path.parent / "task-pack.json"
    if dispatch.get("task_pack") != str(pack_path):
        raise ValueError("dispatch points to another task pack")
    pack_bytes, pack_stat = read_file(pack_path, DOCUMENT_LIMIT)
    receipt_bytes, receipt_stat = read_file(receipt_path, DOCUMENT_LIMIT)
    pack, receipt = json.loads(pack_bytes), json.loads(receipt_bytes)
    if not isinstance(pack, Mapping) or not isinstance(receipt, Mapping):
        raise ValueError("task pack or receipt is not an object")
    task, nonce = challenge["task_id"], challenge["ack_nonce"]
    ack = f"PREFLIGHT_ACK|{task}|claude:identity|READY|INLINE|{nonce}"
    template = f"PREFLIGHT_ACK|{task}|<provider>:identity|READY|INLINE|<nonce>"
    rows = receipt.get("executors")
    if not isinstance(rows, list):
        raise ValueError("receipt has no executor records")
    rows = [row for row in rows if isinstance(row, dict) and row.get("executor") == challenge["executor"]]
    if len(rows) != 1:
        raise ValueError("receipt executor is not unique")
    row = rows[0]
    report = pack.get("report")
    callback = pack.get("completion_callback")
    if (pack.get("draft") is not False or pack.get("role") != "executor"
            or pack.get("task_id") != task or dispatch.get("task_id") != task
            or pack.get("executor") != challenge["executor"]
            or pack.get("executor_uuid") != target["surface_id"]
            or pack.get("callback") not in (ack, template)
            or pack.get("completion_nonce") != dispatch.get("marker")
            or not isinstance(report, str) or not report.startswith("/")
            or callback not in (f"DONE|{task}|{dispatch['marker']}|REPORT={report}",
                                f"BLOCKED|{task}|{dispatch['marker']}|REPORT={report}")
            or dispatch.get("completion_tail") != callback
            or dispatch.get("required_skill") != pack.get("required_skill")
            or dispatch.get("callback_target") != pack.get("callback_target")
            or receipt.get("task_id") != task or row.get("task_id") != task
            or row.get("executor_provider") != "claude"
            or row.get("status") != "PASS" or row.get("lifecycle") != "ACKED"
            or row.get("executor_ack") is not True or row.get("ack_nonce") != nonce
            or row.get("ack_line") != ack or row.get("ack_line_expected") != ack):
        raise ValueError("finalized task/ACK/skill/callback binding mismatch")
    return [{"path": str(pack_path), "stat": pack_stat},
            {"path": str(receipt_path), "stat": receipt_stat}]


def ordered_events(events: list[dict]) -> list[dict]:
    if not isinstance(events, list) or not events:
        raise ValueError("missing native history")
    for event in events:
        if (not isinstance(event, Mapping) or not isinstance(event.get("event_id"), str)
                or not event["event_id"] or type(event.get("created_at")) not in (int, float)
                or not math.isfinite(event["created_at"])):
            raise ValueError("invalid native event")
    if len({e["event_id"] for e in events}) != len(events):
        raise ValueError("ambiguous native history")
    return sorted(events, key=lambda e: (e["created_at"], e["event_id"]))


def plan(*, events: list[dict], ledger: Mapping[str, dict], challenge: dict,
         ack_id: str, ack_at: float, target: Mapping[str, Any],
         binding: dict, now: float) -> dict:
    """Validate only the old parser's exact suppression, including compaction."""
    if not all(isinstance(v, Mapping) for v in (ledger, challenge, target, binding)):
        raise ValueError("invalid migration binding")
    events = ordered_events(events)
    ids = {event["event_id"]: event for event in events}
    original = ids[challenge["event_id"]]
    ack = ids[ack_id]
    ch_fields = ("task_id", "agent", "ack_nonce", "executor", "receipt_path")
    if (original.get("event_name") != "UserPromptSubmit"
            or original.get("handshake_challenge") != {k: challenge[k] for k in ch_fields}
            or original.get("message_hash") != challenge.get("message_hash")
            or original["created_at"] != challenge["created_at"]
            or ack.get("event_name") != "Stop" or ack.get("completed") is not False
            or ack.get("handshake_ack") != {k: challenge[k] for k in ("task_id", "agent", "ack_nonce")}
            or ack["created_at"] != ack_at
            or not original["created_at"] < ack_at <= now
            or binding["turn_started_at"] != original["created_at"]):
        raise ValueError("original challenge/ACK binding mismatch")
    after_ack = [e for e in events if e["created_at"] > ack_at]
    dispatches = [e for e in after_ack if e.get("event_name") == "UserPromptSubmit"]
    if len(dispatches) != 1:
        raise ValueError("no unique suppressed dispatch")
    dispatch_event = dispatches[0]
    if (not dispatch_event.get("collaboration_protocol") or dispatch_event.get("task_dispatch")
            or dispatch_event.get("prompt_kind") != "human"
            or dispatch_event.get("handshake_challenge")):
        raise ValueError("not a legacy parser rejection")
    latest = events[-1]
    if (latest.get("event_id") != binding["last_event_id"]
            or latest["created_at"] != binding["last_hook_at"]):
        raise ValueError("newer native event owns the runtime")
    generations = [original["event_id"]]
    source_rows = {}
    for event in events:
        event_id = event["event_id"]
        row = ledger[event_id]
        if not isinstance(row, Mapping):
            raise ValueError("missing original ledger row")
        if (event.get("synthetic_fallback") or event.get("handshake_provider_mismatch")
                or event.get("completed") is True or not 0 < event["created_at"] <= now
                or any(event.get(k) != target.get(k) for k in ("surface_id", "workspace_id"))
                or event.get("session_id") != binding["session_id"]
                or event.get("agent_pid") != binding["pid"]
                or any(item.get("process_generation") not in (None, "", binding["process_generation"])
                       for item in (event, row))
                or any(row.get(k) != event.get(k) for k in (
                    "surface_id", "workspace_id", "agent_pid", "session_id", "event_name", "created_at"))):
            raise ValueError("native history/ledger identity mismatch")
        expected = "suppressed_handshake_wait"
        if event_id == original["event_id"]:
            expected = "handshake_challenge"
        elif event_id == ack_id:
            expected = "handshake_wait"
        elif event.get("event_name") == "SessionStart":
            if event.get("source") != "compact" or event["created_at"] < dispatch_event["created_at"]:
                raise ValueError("session restarted outside the dispatched task")
            generations.append(event_id)
            expected = "session_started"
        elif event_id != dispatch_event["event_id"]:
            if (event.get("event_name") not in ("Stop", "StopFailure")
                    or event.get("completed") is not False or event.get("handshake_ack")
                    or event["created_at"] < dispatch_event["created_at"]):
                raise ValueError("intervening native task or protocol boundary")
        if row.get("status") != expected:
            raise ValueError("original event already has a different outcome")
        source_rows[event_id] = dict(row)
    if binding["generation_id"] != generations[-1]:
        raise ValueError("turn/compact generation mismatch")
    stop = latest if latest.get("event_name") in ("Stop", "StopFailure") else None
    dispatch, native_file = _native_dispatch(dispatch_event, stop)
    files = _task_files(challenge, dispatch, target)
    return copy.deepcopy({"version": 1, "binding": binding, "challenge": challenge,
            "dispatch_event_id": dispatch_event["event_id"], "ack_event_id": ack_id,
            "source_rows": source_rows, "events": events, "files": [native_file, *files],
            "stop": stop, "recovered_at": now})


def sources_current(proof: Mapping[str, Any], events: list[dict], ledger: Mapping[str, dict]) -> bool:
    return (ordered_events(events) == proof["events"]
            and all(ledger.get(event_id) == row for event_id, row in proof["source_rows"].items())
            and all(fingerprint(Path(item["path"])) == item["stat"] for item in proof["files"]))


def refresh_append(proof: Mapping[str, Any], events: list[dict], ledger: Mapping[str, dict]) -> dict:
    """Reprove an append at a stopped boundary, never inside a write guard.

    Journal events, original ledger rows, pack and ACK receipt stay immutable.
    The native JSONL may only append to the same inode and exact byte prefix;
    its full new content must still prove no newer prompt or completion.
    """
    if (ordered_events(events) != proof["events"]
            or any(ledger.get(key) != row for key, row in proof["source_rows"].items())
            or any(fingerprint(Path(item["path"])) != item["stat"] for item in proof["files"][1:])):
        raise ValueError("original migration evidence changed")
    dispatch = next(e for e in proof["events"] if e["event_id"] == proof["dispatch_event_id"])
    _, native = _native_dispatch(dispatch, proof["stop"], previous=proof["files"][0])
    updated = copy.deepcopy(dict(proof))
    updated["files"][0] = native
    if not sources_current(updated, events, ledger):
        raise ValueError("migration evidence changed during stopped revalidation")
    return updated
