#!/usr/bin/env python3
"""Small, dependency-free protocol shared by the Claude hook and CCC daemon."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping


APP_NAME = "cmux-codex-continue"
APP_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
EVENT_JOURNAL_PATH = APP_DIR / "claude-events.jsonl"
EVENT_SOCKET_PATH = APP_DIR / "claude-events.sock"
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_CLAUDE_MESSAGE = (
    "任务中断了么？如果是就请继续，如果任务完成了务必在最后一句向我报告 "
    "‘ 完成，建议检查 usage: /context’ 。如果任务没有中断就请继续，不要影响你的进度"
)
COMPLETION_SUFFIX = "建议检查usage:/context"
TRAILING_PUNCTUATION_RE = re.compile(r"[。.!！?？'\"’”）)】」』]+$")


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = re.sub(r"[\s\u00a0]+", "", normalized)
    return TRAILING_PUNCTUATION_RE.sub("", normalized)


def completion_reported(value: str) -> bool:
    """Only the required suffix marks a Claude turn as normally complete."""

    return _compact(value).lower().endswith(COMPLETION_SUFFIX)


def report_ready_task(value: str) -> str | None:
    """Recognize the complete closeout declaration, never a quoted mention.

    This is a stop request, not proof of delivery or supervisor acceptance.
    Removing whitespace also accepts terminal wraps inside a task ID or path.
    """
    lines = (value or "").splitlines()
    candidate = None
    fence = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not stripped[len(token):].strip():
                fence = None
        # Only a declaration on its own line outside quoted/code content can
        # close the reply. Earlier explanation is allowed; later prose is not.
        if (fence is None and not line.startswith(('    ', '\t'))
                and stripped.startswith('STATUS:')):
            candidate = index
    if candidate is None:
        return None
    compact = re.sub(r"\s+", "", "\n".join(lines[candidate:]))
    match = re.fullmatch(
        r"STATUS:REPORT_READYTASK_ID=([A-Za-z0-9][A-Za-z0-9._:-]{0,159})"
        r"CALLBACK_UNCONFIRMEDREPORT=(/[^\x00-\x1f]+)"
        r"supervisor_reconciliation_required", compact,
    )
    return match.group(1) if match else None


def configured_claude_message(path: Path = CONFIG_PATH) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DEFAULT_CLAUDE_MESSAGE
    message = value.get("claude_message") if isinstance(value, Mapping) else None
    return message if isinstance(message, str) and message.strip() else DEFAULT_CLAUDE_MESSAGE


def prompt_kind(prompt: str, configured_message: str) -> str:
    return "watchdog" if _compact(prompt) == _compact(configured_message) else "human"


def _digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8", errors="replace")).hexdigest()[:24]


_TASK_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}"
_NONCE_TOKEN = r"[A-Za-z0-9_-]{8,160}"


def handshake_ack(value: str) -> dict[str, str] | None:
    # The harness requires the *entire* assistant reply to be the ACK. Quoted
    # examples, prose and StopFailure's previous assistant text are not ACKs.
    match = re.fullmatch(
        rf"PREFLIGHT_ACK\|({_TASK_TOKEN})\|(claude:identity)\|READY\|INLINE\|({_NONCE_TOKEN})",
        value.strip('\r\n'),
    )
    return (dict(zip(('task_id', 'agent', 'ack_nonce'), match.groups())) if match else None)


def handshake_challenge(value: str, *, _provider: str = 'claude') -> dict[str, str] | None:
    if not value.startswith('DELIVERY_NONCE='):
        return None
    fields = {}
    for key, pattern in [('ACK_TASK_ID', _TASK_TOKEN), ('ACK_AGENT', re.escape(_provider + ':identity')),
                         ('ACK_STATUS', 'READY'), ('ACK_REPORT', 'INLINE'),
                         ('ACK_NONCE', _NONCE_TOKEN)]:
        # Count declarations before validating their values. An invalid second
        # declaration is still ambiguous; it must not disappear from matching.
        if len(re.findall(rf'\b{key}=', value)) != 1:
            return None
        matches = re.findall(rf'\b{key}=({pattern})(?=[\s.]|$)', value)
        if len(matches) != 1:
            return None
        fields[key] = matches[0]
    task, nonce = fields['ACK_TASK_ID'], fields['ACK_NONCE']
    prefix = (rf'DELIVERY_NONCE={re.escape(nonce)}\. This is a legitimate cmux multi-agent '
              rf'harness handshake from supervisor surface:\d+ for task {re.escape(task)}\. ')
    if not re.match(prefix, value):
        return None
    for key, expected in [('task_id', task), ('executor_provider', _provider), ('ack_nonce', nonce)]:
        # Match the expected token exactly: the sentence's final dot is not
        # part of a nonce, while dots inside a task id are legitimate.
        if (len(re.findall(rf'\b{key} == ', value)) != 1
                or not re.search(rf'\b{key} == {re.escape(expected)}(?=\s|[,.](?:\s|$)|$)', value)):
            return None
    executors = re.findall(r'Select exactly one executors\[\] record with executor == (surface:\d+) and ordinal == \d+\.', value)
    receipts = re.findall(r'Verify the pending receipt at the absolute path (/[^\r\n;]+/handshake-receipt\.json);', value)
    if len(executors) != 1 or len(receipts) != 1:
        return None
    return {'task_id': task, 'ack_nonce': nonce, 'agent': fields['ACK_AGENT'],
            'executor': executors[0], 'receipt_path': receipts[0]}


def task_dispatch(value: str) -> dict[str, str] | None:
    # submit_task_pack accepts both the original multiline envelope and the
    # single-line DELIVERY_NONCE envelope used by the live harness. Parse only
    # explicit, unique fields; never open a prompt-selected file in a Hook.
    if value.startswith('DELIVERY_NONCE='):
        keys = ('DELIVERY_NONCE', 'REQUIRED_SKILL', 'TASK_PACK',
                'CALLBACK_TARGET', 'COMPLETION_CALLBACK')
        if any(len(re.findall(rf'\b{key}=', value)) != 1 for key in keys):
            return None
        nonce = r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}'
        match = re.fullmatch(
            rf'DELIVERY_NONCE=({nonce}) READ_AND_OBEY_REQUIRED_SKILL_FIRST '
            rf'REQUIRED_SKILL=(/[^\x00-\x1f]+?) TASK_PACK=(/[^\x00-\x1f]+?) '
            rf'CALLBACK_TARGET=(surface:\d+) COMPLETION_CALLBACK='
            rf'((?:DONE|BLOCKED)\|({_TASK_TOKEN})\|({nonce})\|REPORT=/[^\x00-\x1f]+)',
            value,
        )
        if not match or match[1] != match[7]:
            return None
        # The callback may end immediately before Chinese prose. Keep the tail
        # intact: only the finalized pack can establish the exact report path.
        return {'task_id': match[6], 'marker': match[1], 'task_pack': match[3],
                'required_skill': match[2], 'callback_target': match[4],
                'completion_tail': match[5]}
    lines = value.splitlines()
    if not lines or not (head := re.fullmatch(rf'TASK_DISPATCH ({_NONCE_TOKEN})', lines[0])):
        return None
    packs = [line[len('TASK_PACK='):] for line in lines if line.startswith('TASK_PACK=')]
    callbacks = [line[len('COMPLETION_CALLBACK='):] for line in lines if line.startswith('COMPLETION_CALLBACK=')]
    if len(packs) != 1 or len(callbacks) != 1 or not packs[0].startswith('/'):
        return None
    callback = re.fullmatch(rf'DONE\|({_TASK_TOKEN})\|({_NONCE_TOKEN})\|REPORT=/[^\x00-\x1f]+', callbacks[0])
    if not callback or callback[2] != head[1]:
        return None
    return {'task_id': callback[1], 'marker': head[1], 'task_pack': packs[0]}


def build_event(payload: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    env = environ if environ is not None else os.environ
    event_name = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event_name not in {"SessionStart", "UserPromptSubmit", "Stop", "StopFailure"}:
        return None
    now = time.time()
    session_id = str(payload.get("session_id") or "")
    transcript_path = str(payload.get("transcript_path") or "")
    assistant_message = str(payload.get("last_assistant_message") or "")
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
    error = str(payload.get("error") or payload.get("error_details") or "")
    try:
        agent_pid = int(env.get("CLAUDE_PID") or os.getppid())
    except (TypeError, ValueError):
        agent_pid = os.getppid()
    event: dict[str, Any] = {
        "version": 1,
        "event_id": uuid.uuid4().hex,
        "created_at": now,
        "event_name": event_name,
        "session_id": session_id,
        "transcript_id": _digest(transcript_path),
        "surface_id": str(env.get("CMUX_SURFACE_ID") or ""),
        "workspace_id": str(env.get("CMUX_WORKSPACE_ID") or ""),
        # The Hook process is spawned by Claude Code.  The daemon still
        # verifies the live surface process before any send; this PID is only a
        # health-generation hint and is never an authorization credential.
        "agent_pid": agent_pid,
        "cwd_hash": _digest(str(payload.get("cwd") or "")),
        "message_hash": _digest(assistant_message or prompt or error),
    }
    if event_name == "SessionStart":
        event["source"] = str(payload.get("source") or "startup")
    elif event_name == "UserPromptSubmit":
        event["prompt_kind"] = prompt_kind(prompt, configured_claude_message())
        event['collaboration_protocol'] = any(token in prompt for token in (
            'DELIVERY_NONCE=', 'PREFLIGHT_ACK|', 'TASK_DISPATCH', 'TASK_PACK=',
        ))
        event['handshake_challenge'] = handshake_challenge(prompt)
        # A harness naming Codex on a Claude process must hold, never authorize.
        event['handshake_provider_mismatch'] = handshake_challenge(prompt, _provider='codex')
        event['task_dispatch'] = task_dispatch(prompt)
    elif event_name == "Stop":
        event["completed"] = completion_reported(assistant_message)
        event["report_ready_task_id"] = report_ready_task(assistant_message)
        event["stop_hook_active"] = bool(payload.get("stop_hook_active"))
        event['handshake_ack'] = handshake_ack(assistant_message)
        foreign = re.fullmatch(
            rf'PREFLIGHT_ACK\|({_TASK_TOKEN})\|codex:identity\|READY\|INLINE\|({_NONCE_TOKEN})',
            assistant_message.strip('\r\n'),
        )
        event['handshake_provider_mismatch'] = (
            dict(zip(('task_id', 'ack_nonce'), foreign.groups())) if foreign else None)
    else:
        event["completed"] = completion_reported(assistant_message)
        lowered = error.lower()
        if "429" in lowered or "rate limit" in lowered:
            event["error_kind"] = "claude_429"
        elif "503" in lowered or "overloaded" in lowered:
            event["error_kind"] = "claude_503"
        elif "connection" in lowered or "stream" in lowered:
            event["error_kind"] = "claude_stream"
        else:
            event["error_kind"] = "claude_api"
    return event


def append_event(event: Mapping[str, Any], path: Path = EVENT_JOURNAL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def notify_daemon(event: Mapping[str, Any], path: Path = EVENT_SOCKET_PATH) -> None:
    data = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > 60_000:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.1)
        sock.sendto(data, str(path))
    except OSError:
        # The journal is durable; a restarted daemon replays it.
        pass
    finally:
        sock.close()
