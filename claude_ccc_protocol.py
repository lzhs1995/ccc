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
COMPLETION_SUFFIX = "完成,建议检查usage:/context"
TRAILING_PUNCTUATION_RE = re.compile(r"[。.!！?？'\"’”）)】」』]+$")
COMPLETION_SENTENCE_RE = re.compile(
    r"(?:\A|(?<=[\n。.!！?？;；]))[ \t]{0,3}"
    r"完成\s*,\s*建议检查\s*usage\s*:\s*/context"
    r"[\s。.!！'\"’”）)】」』]*\Z", re.IGNORECASE,
)


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = re.sub(r"[\s\u00a0]+", "", normalized)
    return TRAILING_PUNCTUATION_RE.sub("", normalized)


def completion_reported(value: str) -> bool:
    """Require the user's whole final sentence, outside quoted/code content.

    Keep line boundaries until after checking fences and indentation. Flattening
    first made a quoted example or ``未完成，建议检查 ...`` a completion signal.
    Whitespace, terminal wraps and full-width punctuation remain compatible.
    """
    text = unicodedata.normalize("NFKC", value or "")
    match = COMPLETION_SENTENCE_RE.search(text)
    if not match:
        return False
    fence = None
    for line in text[:match.start()].splitlines():
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None
    return fence is None


def report_ready_task(value: str) -> str | None:
    """Recognize the complete closeout declaration, never a quoted mention.

    This is report metadata, not task completion, delivery or acceptance.
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
    declaration = "\n".join(lines[candidate:])
    # Keep callback diagnostics even when the executor separately declares its
    # bounded task complete. Report readiness itself never implies completion.
    footer = COMPLETION_SENTENCE_RE.search(unicodedata.normalize("NFKC", declaration))
    if footer:
        declaration = unicodedata.normalize("NFKC", declaration)[:footer.start()]
    compact = re.sub(r"\s+", "", declaration)
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


CLAUDE_PROVIDER_RETRY_KINDS = frozenset({
    "claude_auth", "claude_billing", "claude_model_unavailable", "claude_request_invalid",
    "claude_429", "claude_503", "claude_api", "claude_stream",
})
# Error labels describe the last failure; they never override the user's
# noncompletion continuation rule. Keep the old import compatible for callers.
CLAUDE_PROVIDER_BLOCKED_KINDS = frozenset()
STOP_FAILURE_ENUMS = frozenset({
    "authentication_failed", "oauth_org_not_allowed", "cloud_credential_error",
    "billing_error", "account_on_hold", "model_not_found", "invalid_request",
    "rate_limit", "overloaded", "server_error", "unknown", "max_output_tokens",
})
STOP_FAILURE_METADATA = (
    "error_kind", "stop_failure_error", "error_reason", "error_evidence_hash", "provider_blocked",
)
_ERROR_STATUS_RE = re.compile(
    r"(?:\b(?:HTTP(?:/\d(?:\.\d)?)?(?:\s+Error)?|API\s+Error|status(?:_code)?|error)"
    r"\s*[\"':= -]*|^\s*)([45]\d\d)\b", re.IGNORECASE | re.MULTILINE,
)
_REJECTED_STATUS_RE = re.compile(r"\brequest\s+rejected\s*\(\s*([45]\d\d)\s*\)", re.IGNORECASE)
_ERROR_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def classify_stop_failure(error: Any, error_details: Any = "") -> dict[str, Any]:
    """Classify native failure evidence without retaining response text.

    Hook enums and response details are independent diagnostic evidence.
    All incomplete failures use bounded continuation; a label must not create
    an indefinite authentication/billing/model hold.
    """
    def text(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value or "")

    raw_error, details = text(error), text(error_details)
    enum = raw_error.strip().lower()
    if enum.endswith("_error") and enum[:-6] in STOP_FAILURE_ENUMS:
        enum = enum[:-6]
    normalized_enum = enum if enum in STOP_FAILURE_ENUMS else "unknown"
    evidence = _ERROR_ANSI_RE.sub("", unicodedata.normalize("NFKC", raw_error + "\n" + details))
    folded = re.sub(r"\s+", " ", evidence).lower()
    codes = {match[1] for match in _ERROR_STATUS_RE.finditer(evidence)}
    codes.update(match[1] for match in _REJECTED_STATUS_RE.finditer(folded))
    kind, reason = "claude_api", "provider_error"
    if re.search(
        r"insufficient.{0,32}(?:credit|balance|quota)|credit balance.{0,32}(?:low|exhaust|insufficient)|"
        r"(?:billing|payment)[_ ](?:error|required)|account[_ ]on[_ ]hold|"
        r"余额不足|余额已耗尽|预扣费.{0,16}(?:失败|拒绝|不足)|欠费|额度不足", folded,
    ) or "402" in codes or enum in {"billing_error", "account_on_hold"}:
        kind, reason = "claude_billing", "billing_rejected"
    elif enum in {"authentication_failed", "oauth_org_not_allowed", "cloud_credential_error"}:
        kind, reason = "claude_auth", "authentication_rejected"
    elif enum == "model_not_found":
        kind, reason = "claude_model_unavailable", "model_unavailable"
    elif re.search(
        r"authentication[_ ](?:failed|error)|oauth_org_not_allowed|cloud_credential_error|"
        r"invalid[_ ](?:api[_ ]?)?key|(?:api[_ ]?key|credentials?).{0,32}(?:expired|revoked|invalid)|"
        r"permission[_ ]error|unauthorized|forbidden|所属分组.{0,12}(?:已删除|不存在|禁用)|"
        r"无效.{0,8}(?:密钥|令牌)|(?:密钥|令牌).{0,8}(?:失效|过期|禁用)|无权访问|认证失败", folded,
    ) or codes.intersection({"401", "403"}):
        kind, reason = "claude_auth", "authentication_rejected"
    elif re.search(
        r"model[_ ]not[_ ]found|(?:model|selected model).{0,120}"
        r"(?:does not exist|not found|not available|unavailable|not have access|may not exist)|"
        r"(?:模型).{0,20}(?:不存在|不可用|无权|未授权)", folded,
    ):
        kind, reason = "claude_model_unavailable", "model_unavailable"
    elif ("405" in codes or re.search(
        r"method not allowed|unsupported (?:endpoint|method|route)|"
        r"(?:endpoint|route).{0,24}(?:not found|incompatible)|"
        r"prompt (?:is )?too long|context (?:length|window).{0,24}(?:exceed|limit)|"
        r"请求.{0,8}(?:不合法|无效)|不支持.{0,8}(?:端点|请求方法)", folded,
    )):
        kind, reason = "claude_request_invalid", "request_rejected"
    elif (enum == "rate_limit" or "429" in codes or re.search(
        r"rate[_ ]limit(?:ed|ing|_error)?\b|(?:group\s+)?requests\s*-\s*per\s*-\s*minute\s+limit\s+exceeded",
        folded,
    )):
        kind, reason = "claude_429", "rate_limited"
    elif enum == "overloaded" or "503" in codes or re.search(r"\boverloaded(?:_error)?\b", folded):
        kind, reason = "claude_503", "provider_overloaded"
    elif enum == "invalid_request":
        kind, reason = "claude_request_invalid", "request_rejected"
    elif re.search(r"connection|stream|network|econnreset|etimedout", folded):
        kind, reason = "claude_stream", "transport_interrupted"
    return {
        "error_kind": kind, "stop_failure_error": normalized_enum,
        "error_reason": reason, "provider_blocked": False,
        "error_evidence_hash": _digest(raw_error + "\n" + details),
    }


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
    if value.startswith(('TASK_PACK=', 'STATUS: BUSINESS_DISPATCH ',
                         'READ_AND_OBEY_REQUIRED_SKILL_FIRST')):
        # The bridge's older finalized envelope uses CR (or LF) separators
        # and a bare callback. It has no DELIVERY_NONCE/TASK_DISPATCH header.
        lines = value.splitlines()
        header_marker = None
        skill_first = lines[0] == 'READ_AND_OBEY_REQUIRED_SKILL_FIRST'
        if lines[0].startswith('STATUS: BUSINESS_DISPATCH '):
            header = re.fullmatch(rf'STATUS: BUSINESS_DISPATCH ({_NONCE_TOKEN})', lines.pop(0))
            if not header:
                return None
            header_marker = header[1]
        if skill_first:
            # The current bridge also puts the read requirement first, then
            # the three fields and a named callback. Normalize only this exact
            # layout; arbitrary prose/quoted examples remain inadmissible.
            if len(lines) < 5:
                return None
            lines = [lines[1], lines[2], lines[0], *lines[3:]]
        keys = ('TASK_PACK', 'REQUIRED_SKILL', 'CALLBACK_TARGET')
        if (len(lines) < 5 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value)
                or lines[2] != 'READ_AND_OBEY_REQUIRED_SKILL_FIRST'
                or sum(line == 'READ_AND_OBEY_REQUIRED_SKILL_FIRST' for line in lines) != 1
                or any(len(re.findall(rf'\b{key}=', value)) != 1 for key in keys)
                or any(token in value for token in ('DELIVERY_NONCE=', 'TASK_DISPATCH'))
                or value.count('STATUS: BUSINESS_DISPATCH ') != int(header_marker is not None)
                or len(re.findall(r'\bCOMPLETION_CALLBACK=', value)) != int(skill_first)
                or sum(line.startswith(('DONE|', 'BLOCKED|', 'COMPLETION_CALLBACK=')) for line in lines) != 1):
            return None
        pack = re.fullmatch(r'TASK_PACK=(/[^\x00-\x1f]+)', lines[0])
        skill = re.fullmatch(r'REQUIRED_SKILL=(/[^\x00-\x1f]+)', lines[1])
        target = re.fullmatch(r'CALLBACK_TARGET=(surface:\d+)', lines[3])
        tail = lines[4]
        if skill_first:
            if not tail.startswith('COMPLETION_CALLBACK='):
                return None
            tail = tail[len('COMPLETION_CALLBACK='):]
        callback = re.fullmatch(
            rf'(?:DONE|BLOCKED)\|({_TASK_TOKEN})\|({_NONCE_TOKEN})\|REPORT=/[^\x00-\x1f]+', tail)
        if not all((pack, skill, target, callback)):
            return None
        if header_marker is not None and header_marker != callback[2]:
            return None
        return {'task_id': callback[1], 'marker': callback[2], 'task_pack': pack[1],
                'required_skill': skill[1], 'callback_target': target[1],
                'completion_tail': tail}
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
    error = payload.get("error") or ""
    error_details = payload.get("error_details") or ""
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
        "message_hash": _digest(assistant_message or prompt or str(error) + "\n" + str(error_details)),
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
        event.update(classify_stop_failure(error, error_details))
        # 仅「本地未识别」的失败保留有界诊断原文，供 JEV 异步分类；
        # 原文须完整且其摘要等于 error_evidence_hash，截断则不保留（可验来源）。
        evidence = _stop_failure_evidence_text(error, error_details)
        if (event["error_kind"] == "claude_api" and event["stop_failure_error"] == "unknown"
                and len(evidence) <= JEV_EXCERPT_LIMIT
                and _digest(evidence) == event["error_evidence_hash"]):
            event["error_excerpt"] = evidence
    return event


JEV_EXCERPT_LIMIT = 600


def _stop_failure_evidence_text(error: Any, error_details: Any) -> str:
    """Same text that classify_stop_failure hashes into error_evidence_hash."""
    def text(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value or "")
    return text(error) + "\n" + text(error_details)


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
