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
    elif event_name == "Stop":
        event["completed"] = completion_reported(assistant_message)
        event["stop_hook_active"] = bool(payload.get("stop_hook_active"))
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
