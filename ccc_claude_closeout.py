"""CCC 的只读协作封口适配；不产生回执、输入、核收或 completion 声明。"""
from __future__ import annotations

import builtins
import copy
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import time
import types
from typing import Mapping
import uuid

import claude_ccc_protocol as protocol
import claude_dispatch_recovery as dispatch_recovery


WAITING = "WAITING_SUPERVISOR"
TIMED_OUT = "WAITING_SUPERVISOR_TIMEOUT"
WAIT_SECONDS = 300.0
NATIVE_TAIL_LIMIT = 4 * 1024 * 1024
RELEASES_ROOT = Path.home() / ".local/share/multi-agent-collaboration/releases"
ACTIVE_ROOT = Path("/tmp/multi-agent-collaboration/_active")
IDENTITY_KEYS = ("workspace_id", "surface_id", "session_id", "agent_pid", "process_generation")
PROTOCOL_FIELDS = ("collaboration_protocol", "handshake_challenge", "handshake_ack",
                   "handshake_provider_mismatch", "task_dispatch")
PROTOCOL_TOKENS = ("DELIVERY_NONCE=", "PREFLIGHT_ACK|", "TASK_DISPATCH", "TASK_PACK=",
                   "READ_AND_OBEY_REQUIRED_SKILL_FIRST", "STATUS: BUSINESS_DISPATCH",
                   "COMPLETION_CALLBACK=", "DONE|", "BLOCKED|", "STATUS: REPORT_READY")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _epoch(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.timestamp() if stamp.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _read(path, limit=1024 * 1024):
    """只读实际普通文件；禁止经符号链接载入验证器或 marker。"""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("noncanonical file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_mode & 0o022 or opened.st_size > limit):
            raise ValueError("untrusted file")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        if (len(raw) > limit or (opened.st_dev, opened.st_ino, opened.st_size,
                                opened.st_mtime_ns, opened.st_ctime_ns)
                != (current.st_dev, current.st_ino, current.st_size,
                    current.st_mtime_ns, current.st_ctime_ns)
                or (opened.st_dev, opened.st_ino, opened.st_size,
                    opened.st_mtime_ns, opened.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("file changed")
        return raw


def wait_state(wait, now=None):
    """期限从原次 callback 返回起计；超时保持封闭，不刷新、不重跑。"""
    if not wait:
        return None
    now = time.time() if now is None else now
    value = copy.deepcopy(wait)
    if (value.get("status") not in (WAITING, TIMED_OUT)
            or not _number(value.get("deadline_at"))
            or not _number(now) or now >= value["deadline_at"]):
        value["status"] = TIMED_OUT
    return value


def snapshot_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def native_tail(path):
    """A locked, bounded suffix of complete native records, with byte provenance.

    This is not a dispatch proof. Whole-file dispatch verification keeps its
    existing contract; closeout only needs the most recent native turn boundary.
    A partial final record is uncertain, so it never authorizes a state change.
    """
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("noncanonical native transcript source")
    signature = dispatch_recovery.fingerprint(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_mode & 0o022 or
                [getattr(opened, k) for k in dispatch_recovery.STAT_FIELDS] != signature):
            raise ValueError("untrusted or replaced native source")
        offset = max(0, opened.st_size - NATIVE_TAIL_LIMIT)
        stream.seek(max(0, offset - 1))
        partial_first = offset > 0 and stream.read(1) != b"\n"
        data = stream.read(NATIVE_TAIL_LIMIT)
        if partial_first:
            boundary = data.find(b"\n")
            if boundary < 0:
                raise ValueError("no complete native tail record")
            offset += boundary + 1
            data = data[boundary + 1:]
        if data and not data.endswith(b"\n"):
            raise ValueError("partial native tail record")
        if ([getattr(os.fstat(fd), k) for k in dispatch_recovery.STAT_FIELDS] != signature
                or dispatch_recovery.fingerprint(path) != signature):
            raise ValueError("native source changed during read")
    return data, {"path": str(path), "stat": signature, "offset_bytes": offset,
                  "bytes_read": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def native_event_from_journal(path, accepted):
    """Recover only the exact already-accepted Hook, never synthesize/replay it."""
    data, proof = native_tail(path)
    matches = []
    for line in data.splitlines():
        row = json.loads(line)
        if isinstance(row, dict) and row.get("event_id") == accepted.get("event_id"):
            matches.append(row)
    if len(matches) != 1:
        raise ValueError("original accepted Hook is outside the bounded journal tail")
    event = matches[0]
    for key in ("event_name", "session_id", "agent_pid", "surface_id", "workspace_id", "created_at"):
        if not accepted.get(key) or event.get(key) != accepted[key]:
            raise ValueError("original Hook does not match its accepted identity")
    for key in ("process_generation", "transcript_id", "message_hash"):
        if (event.get(key) and accepted.get(key)
                and event[key] != accepted[key]):
            raise ValueError("original Hook provenance conflicts with the accepted event")
    if event.get("synthetic_fallback") or not event.get("transcript_id"):
        raise ValueError("not an original native Hook")
    return event, proof


def native_human_prompt(event, wait, identity, *, watermark, watchdog_message, now=None):
    """证明 callback 之后的新原生输入；Hook 的 human 标签本身没有授权力。

    原生主线的 typed/user 记录（旧 schema 可省略 source/origin）提供来源证据。
    调用者还须排除自己的 pending/已提交 watchdog，再持久化旧等待快照。
    """
    now = time.time() if now is None else now
    original = wait.get("binding") if isinstance(wait, dict) else None
    evidence = wait.get("evidence") if isinstance(wait, dict) else None
    ended = evidence.get("ended_at_epoch") if isinstance(evidence, dict) else None
    stamp = event.get("created_at")
    if (event.get("event_name") != "UserPromptSubmit" or event.get("synthetic_fallback")
            or any(event.get(key) for key in PROTOCOL_FIELDS)):
        raise ValueError("not an ordinary native UserPromptSubmit")
    if (not isinstance(original, dict) or type(event.get("agent_pid")) is not int
            or any(not identity.get(key) or event.get(key) != identity[key]
                   or original.get(key) != identity[key] for key in IDENTITY_KEYS)):
        raise ValueError("native prompt does not own the original callback identity")
    if (not _number(now) or not _number(ended) or not 0 < ended <= now
            or not _number(watermark) or not _number(stamp)
            or not max(ended, watermark) < stamp <= now + 1):
        raise ValueError("native Hook does not follow the callback and accepted watermark")
    message_hash = event.get("message_hash")
    if not isinstance(message_hash, str) or not re.fullmatch(r"[0-9a-f]{24}", message_hash):
        raise ValueError("native Hook has no exact message hash")
    path = dispatch_recovery.native_path(event)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("noncanonical native transcript source")
    data, source = native_tail(path)
    signature = source["stat"]
    matches = []
    latest_input = None
    for index, line in enumerate(data.splitlines()):
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("invalid native transcript record")
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain") is True:
            continue
        if row.get("sessionId") != identity["session_id"] or row.get("isSidechain") is not False:
            raise ValueError("native transcript session or mainline identity changed")
        if row.get("type") != "user":
            continue
        message = row.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "user":
            raise ValueError("invalid native user role")
        text = dispatch_recovery._text(row)
        content = message.get("content")
        has_text = isinstance(content, str) or (isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "text" for block in content))
        compact = row.get("isCompactSummary") is True and row.get("isVisibleInTranscriptOnly") is True
        if has_text and not compact:
            latest_input = index
        if text is None or dispatch_recovery.digest(text) != message_hash:
            continue
        native_stamp = _epoch(row.get("timestamp"))
        # Repeated ordinary prompts are legal: an older identical sentence is
        # not the candidate for this Hook, even when its hash is the same.
        if (not _number(native_stamp) or not max(ended, watermark) < native_stamp <= now + 1
                or abs(native_stamp - stamp) > 2):
            continue
        origin = row.get("origin")
        if (not isinstance(row.get("uuid"), str) or not row["uuid"] or not text.strip()
                or row.get("isMeta") or row.get("isCompactSummary")
                or row.get("isVisibleInTranscriptOnly") or row.get("isSynthetic")
                or row.get("promptSource") not in (None, "typed")
                or (origin is not None and not isinstance(origin, Mapping))
                or (origin or {}).get("kind") not in (None, "human")
                or any(token in text for token in PROTOCOL_TOKENS)):
            raise ValueError("native record is not an ordinary human prompt")
        matches.append((index, row, text, native_stamp))
    if len(matches) != 1 or matches[0][0] != latest_input:
        raise ValueError("missing, ambiguous or superseded native human prompt")
    if dispatch_recovery.fingerprint(path) != signature:
        raise ValueError("native source changed during verification")
    _, row, text, native_stamp = matches[0]
    return {**source,
            "transcript_id": event["transcript_id"], "prompt_uuid": row["uuid"],
            "prompt_sha256": snapshot_sha256(row), "message_hash": message_hash,
            "message_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "timestamp": row["timestamp"], "created_at": native_stamp,
            "prompt_source": row.get("promptSource"), "origin": row.get("origin"),
            "watchdog_text": any(protocol.prompt_kind(text, configured) == "watchdog"
                                 for configured in (watchdog_message, protocol.DEFAULT_CLAUDE_MESSAGE))}


def native_prompt_current(proof, event):
    """审计落盘后再核对原始文件，变化不会授权解除等待。"""
    try:
        path = dispatch_recovery.native_path(event)
        if str(path) != proof["path"] or path.resolve(strict=True) != path:
            return False
        _, source = native_tail(path)
        return all(source.get(k) == proof.get(k) for k in source)
    except (OSError, ValueError, KeyError, TypeError):
        return False


class CloseoutEvidence:
    """由已安装 hook 定位真实共享验证器，终态验证只调用其公共函数。"""

    def __init__(self, settings_path, *, releases_root=RELEASES_ROOT, active_root=ACTIVE_ROOT):
        self.settings_path = Path(settings_path)
        self.releases_root = Path(releases_root).resolve()
        self.active_root = Path(active_root).resolve()
        self._validator_cache = None

    def _validator(self):
        try:
            settings_raw = _read(self.settings_path)
            settings = json.loads(settings_raw)
            candidates = set()
            for group in settings.get("hooks", {}).get("PreToolUse", []):
                for hook in group.get("hooks", []):
                    if hook.get("type") != "command":
                        continue
                    command = shlex.split(hook.get("command", ""))
                    # 仅识别已安装的精确 Python -B hook，不执行配置中的命令。
                    if (len(command) == 3 and command[1] == "-B"
                            and Path(command[0]).is_absolute()
                            and Path(command[0]).name.startswith("python")
                            and Path(command[2]).name == "cmux_executor_closeout_guard.py"):
                        candidates.add(Path(command[2]))
            if len(candidates) != 1:
                return None
            hook_path = candidates.pop()
            if not hook_path.is_absolute() or hook_path.resolve() != hook_path:
                return None
            relative = hook_path.relative_to(self.releases_root)
            if len(relative.parts) != 4 or relative.parts[1:3] != ("source", "scripts"):
                return None
            hook_raw = _read(hook_path)
            helper_path = hook_path.with_name("executor_closeout.py")
            source = _read(helper_path)
            io_path = hook_path.with_name("cmux_evidence_io.py")
            io_source = _read(io_path)
            io_digest = hashlib.sha256(io_source).hexdigest()
            digest = hashlib.sha256(source).hexdigest()
            key = (str(helper_path), digest, io_digest)
            cached = self._validator_cache
            if cached is None or cached[0] != key:
                # 依赖和验证器来自同一 release；不借用 sys.path/sys.modules
                # 中可能已载入的其他版本，也不污染 daemon 的全局导入状态。
                evidence_io = types.ModuleType("_ccc_installed_evidence_io_" + io_digest)
                evidence_io.__file__ = str(io_path)
                exec(compile(io_source, str(io_path), "exec"), evidence_io.__dict__)

                def installed_import(name, globals=None, locals=None, fromlist=(), level=0):
                    if name == "cmux_evidence_io" and level == 0:
                        return evidence_io
                    return builtins.__import__(name, globals, locals, fromlist, level)

                module = types.ModuleType("_ccc_installed_executor_closeout_" + digest)
                module.__file__ = str(helper_path)
                module.__dict__["__builtins__"] = dict(vars(builtins), __import__=installed_import)
                exec(compile(source, str(helper_path), "exec"), module.__dict__)
                if not callable(getattr(module, "terminal_report", None)) or not callable(
                        getattr(module, "superseded", None)):
                    return None
                cached = (key, module)
            if (_read(self.settings_path) != settings_raw or _read(hook_path) != hook_raw
                    or _read(helper_path) != source or _read(io_path) != io_source):
                return None
            self._validator_cache = cached
            return cached[1], {"path": str(helper_path), "sha256": digest,
                               "evidence_io_path": str(io_path),
                               "evidence_io_sha256": io_digest}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, SyntaxError):
            return None

    def _markers(self, workspace, now):
        # 只扫描已验证目标的 workspace；不能采用 daemon 自己的 cmux 身份。
        try:
            uuid.UUID(workspace)
        except (ValueError, TypeError, AttributeError):
            return []
        directory = self.active_root / workspace
        paths = [] if directory.is_symlink() else sorted(directory.glob("*.json"))
        paths.append(self.active_root / (workspace + ".json"))
        markers = []
        for path in paths:
            if path.name.startswith("."):
                continue
            try:
                raw = _read(path)
                marker = json.loads(raw)
                armed, ttl = _epoch(marker.get("armed_at")), marker.get("ttl_seconds")
                if (marker.get("workspace_uuid") == workspace and _number(armed)
                        and _number(ttl) and ttl > 0 and 0 < armed <= now
                        and now - armed <= ttl):
                    markers.append((path, raw, marker))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return markers

    @staticmethod
    def _matches_marker(binding, marker):
        try:
            return (marker.get("task_id") == binding["task_id"]
                    and Path(marker["artifact_root"]).is_absolute()
                    and Path(marker["artifact_root"]).resolve()
                    == Path(binding["artifact_root"]).resolve()
                    and _epoch(marker.get("armed_at")) <= binding["dispatch_created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def returned(self, binding, identity, *, event=None, now=None):
        """原生派包 + 同会话/进程 + 共享验证器返回，才能进入等待。"""
        now = time.time() if now is None else now
        if not isinstance(binding, dict) or not _number(now):
            return None
        if any(not binding.get(key) or binding.get(key) != identity.get(key) for key in IDENTITY_KEYS):
            return None
        stamp = binding.get("dispatch_created_at")
        if (type(binding.get("agent_pid")) is not int or not _number(stamp)
                or not 0 < stamp <= now or not binding.get("dispatch_event_id")
                or not binding.get("task_pack_sha256") or not binding.get("completion_nonce")):
            return None
        if event is not None and (event.get("synthetic_fallback") or any(
                event.get(key) != binding.get(key) for key in IDENTITY_KEYS)):
            return None
        loaded = self._validator()
        if loaded is None:
            return None
        validator, provenance = loaded
        for path, raw, marker in self._markers(binding["workspace_id"], now):
            if not self._matches_marker(binding, marker):
                continue
            evidence = validator.terminal_report(marker, binding["workspace_id"], binding["surface_id"])
            if not isinstance(evidence, dict):
                continue
            start, end = evidence.get("started_at_epoch"), evidence.get("ended_at_epoch")
            if (evidence.get("task_id") != binding["task_id"]
                    or evidence.get("completion_nonce") != binding["completion_nonce"]
                    or evidence.get("task_pack_sha256") != binding["task_pack_sha256"]
                    or Path(evidence.get("artifact_root", "")).resolve()
                    != Path(binding["artifact_root"]).resolve()
                    or not _number(start) or not _number(end) or not stamp <= start <= end <= now):
                continue
            try:
                if _read(path) != raw:
                    continue
            except (OSError, ValueError):
                continue
            return wait_state({
                "status": WAITING, "task_id": binding["task_id"],
                "observed_at": now, "deadline_at": end + WAIT_SECONDS,
                "binding": copy.deepcopy(binding), "evidence": evidence,
                "validator": provenance,
            }, now)
        return None

    def native_wait(self, identity, *, event, process_started_at, after=0.0,
                    watchdog_message=protocol.DEFAULT_CLAUDE_MESSAGE, now=None):
        """Observe a real native Stop without inventing a lost dispatch binding.

        The marker need only have been live at that native Stop. Expiration now
        cannot turn an honest historical wait into unfinished executor work.
        No receipt, dispatch, completion or terminal input is produced here.
        """
        now = time.time() if now is None else now
        try:
            if (not isinstance(event, dict) or event.get("synthetic_fallback")
                    or type(identity.get("agent_pid")) is not int
                    or any(not identity.get(k) or event.get(k) != identity[k] for k in IDENTITY_KEYS)
                    or not _number(process_started_at) or not _number(after)
                    or not _number(now) or not 0 < process_started_at <= now
                    or not _number(event.get("created_at"))
                    or not process_started_at <= event["created_at"] <= now + 1):
                return None
            path = dispatch_recovery.native_path(event)
            data, source = native_tail(path)
            rows = []
            selected = None
            offset = source["offset_bytes"]
            for line in data.splitlines(keepends=True):
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                if row.get("type") in ("system", "user", "assistant") and row.get("isSidechain") is not True:
                    if row.get("sessionId") != identity["session_id"] or row.get("isSidechain") is not False:
                        return None
                    rows.append(row)
                    if row.get("type") == "system" and row.get("subtype") == "stop_hook_summary":
                        selected = (len(rows) - 1, row, offset)
                offset += len(line)
            if selected is None:
                return None
            index, row, offset = selected
            stamp = _epoch(row.get("timestamp"))
            if (not _number(stamp) or not max(process_started_at, after) < stamp <= now + 1
                    or row.get("session_id", identity["session_id"]) != identity["session_id"]
                    or row.get("preventedContinuation") is not True or row.get("hookErrors") != []
                    or not str(row.get("stopReason", "")).startswith(WAITING + ":")
                    or not row.get("uuid")):
                return None
            # A newer real task owns the session. The triggering human event
            # may still discover this wait so the existing audited takeover
            # runs; earlier generic watchdog echoes cannot hide a real Stop.
            for later in rows[index + 1:]:
                if later.get("type") != "user" or later.get("isCompactSummary"):
                    continue
                text = dispatch_recovery._text(later)
                content = later.get("message", {}).get("content")
                has_text = isinstance(content, str) or (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "text" for b in content))
                if not has_text:
                    continue
                candidate = (text is not None and event.get("event_name") == "UserPromptSubmit"
                             and dispatch_recovery.digest(text) == event.get("message_hash")
                             and _number(_epoch(later.get("timestamp")))
                             and abs(_epoch(later["timestamp"]) - event["created_at"]) <= 2)
                if not candidate and (text is None or not any(
                        protocol.prompt_kind(text, configured) == "watchdog"
                        for configured in (watchdog_message, protocol.DEFAULT_CLAUDE_MESSAGE))):
                    return None
            hooks = []
            for info in row.get("hookInfos", []):
                command = shlex.split(info.get("command", ""))
                if (len(command) != 3 or command[1] != "-B"
                        or not Path(command[0]).is_absolute()
                        or not Path(command[0]).name.startswith("python")):
                    continue
                hook = Path(command[2])
                if hook.name != "cmux_consensus_stop_guard.py":
                    continue
                relative = hook.relative_to(self.releases_root)
                if (len(relative.parts) != 4 or relative.parts[1:3] != ("source", "scripts")
                        or hook.resolve(strict=True) != hook):
                    return None
                hooks.append({"path": str(hook), "sha256": hashlib.sha256(_read(hook)).hexdigest()})
            loaded = self._validator()
            if len(hooks) != 1 or loaded is None:
                return None
            validator, provenance = loaded
            matches = []
            for marker_path, raw, marker in self._markers(identity["workspace_id"], stamp):
                if marker.get("artifact_root") != row.get("cwd"):
                    continue
                evidence = validator.terminal_report(marker, identity["workspace_id"], identity["surface_id"])
                if (not isinstance(evidence, dict)
                        or not _epoch(marker["armed_at"]) <= evidence["started_at_epoch"]
                        <= evidence["ended_at_epoch"] <= stamp
                        or _read(marker_path) != raw):
                    continue
                matches.append((marker_path, raw, evidence))
            if len(matches) != 1 or not native_prompt_current(source, event):
                return None
            marker_path, raw, evidence = matches[0]
            return wait_state({
                "status": WAITING, "task_id": evidence["task_id"],
                "origin": "native_stop_summary", "observed_at": now,
                "deadline_at": evidence["ended_at_epoch"] + WAIT_SECONDS,
                "binding": dict(identity), "evidence": evidence, "validator": provenance,
                "native_summary": {**source, "record_offset_bytes": offset,
                                   "record_sha256": snapshot_sha256(row),
                                   "uuid": row["uuid"], "timestamp": row["timestamp"],
                                   "process_started_at": process_started_at, "hook": hooks[0]},
                "historical_marker": {"path": str(marker_path),
                                      "sha256": hashlib.sha256(raw).hexdigest(),
                                      "validated_at_native_stop": stamp},
            }, now)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def admits_successor(self, wait, binding, *, now=None):
        """只解除同主管的已验证后继派包；复用共享 superseded 规则。"""
        now = time.time() if now is None else now
        if not wait:
            return True
        evidence = wait.get("evidence")
        original = wait.get("binding", {})
        if (not isinstance(evidence, dict) or not isinstance(binding, dict)
                or not _number(now) or not _number(binding.get("dispatch_created_at"))
                or not 0 < binding["dispatch_created_at"] <= now
                or any(binding.get(k) != original.get(k) for k in ("surface_id", "workspace_id"))):
            return False
        loaded = self._validator()
        if loaded is None:
            return False
        validator, _ = loaded
        for path, raw, marker in self._markers(binding["workspace_id"], now):
            if not self._matches_marker(binding, marker):
                continue
            supervisors = [p for p in marker.get("participants", [])
                           if isinstance(p, dict) and p.get("role") == "supervisor"]
            if (len(supervisors) != 1 or binding.get("callback_target") not in (
                    supervisors[0].get("surface_uuid"), supervisors[0].get("surface_ref"))):
                continue
            if validator.superseded(evidence, [marker], binding["workspace_id"],
                                    binding["surface_id"], now=now):
                try:
                    if _read(path) == raw:
                        return True
                except (OSError, ValueError):
                    pass
        return False
