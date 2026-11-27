"""只为不提交的 bridge probe 核实 Claude 中断后残留的 Bash HUD。

正常投递、排队和全局 busy 判定不变。屏幕或一次工具退出都不能独立授权：
必须绑定原生会话、核尾部三条中断链及进程、随后每个清理观察都重新核验。
这只允许测试自己的短 token，不恢复用户拒绝的 Bash，不提交任务或按 Enter。
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import cmux_native_delivery as native


MAX_TAIL = 128 * 1024
PROBE_ERRORS = (RuntimeError, OSError, ValueError, TypeError, KeyError,
                AttributeError, IndexError, subprocess.SubprocessError)
INTERRUPTION = "[Request interrupted by user for tool use]"
REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP "
    "what you are doing and wait for the user to tell you how to proceed."
)


def _require(condition, reason):
    if not condition:
        raise native.NativeDeliveryError("INTERRUPTED_PROBE_" + reason)


def _screen(bridge, screen, token, *, initial=False):
    parsed = bridge._claude_bordered_compose(screen)
    _require(parsed is not None, "UNKNOWN_COMPOSER")
    body = parsed["body"]
    _require(body == "" or (not initial and body and token.startswith(body)), "FOREIGN_DRAFT")
    last = next((row.strip() for row in reversed(parsed["before"]) if row.strip()), "")
    _require(re.fullmatch(r"⎿\s+Interrupted · What should Claude do instead\?", last),
             "NOT_CURRENT_INTERRUPTION")
    tools = [row for row in parsed["footer"] if bridge._CLAUDE_ACTIVE_TOOL_RE.fullmatch(row)]
    _require((len(tools) == 1 if initial else len(tools) <= 1)
             and all(re.match(r"[◐◑◒◓]\s+Bash:", row) for row in tools), "NOT_SOLE_BASH_HUD")
    _require(not re.search(r"^[ \t]*(?:Messages? to be submitted after|Press up to edit queued messages)",
                           "\n".join(parsed["before"]), re.I | re.M), "QUEUED_INPUT")


def _tail(binding):
    """有限读取；最后三条必须仍是原客户端自己的连续中断链。"""
    path = Path(binding["transcript"]["path"])
    with native._open(path) as stream:
        native._check_file(stream, binding)
        before = os.fstat(stream.fileno())
        _require(before.st_size == binding["transcript"]["offset"], "TRANSCRIPT_APPENDED")
        start = max(0, before.st_size - MAX_TAIL)
        stream.seek(start)
        if start:
            stream.readline()
        raw = stream.read(MAX_TAIL + 1)
        after = os.fstat(stream.fileno())
        current = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    stamp = tuple(getattr(before, k) for k in fields)
    _require(stamp == tuple(getattr(after, k) for k in fields)
             == tuple(getattr(current, k) for k in fields), "TRANSCRIPT_CHANGED")
    _require(raw.endswith(b"\n") and len(raw) <= MAX_TAIL, "INCOMPLETE_TAIL")
    lines = raw.splitlines(keepends=True)
    _require(len(lines) >= 3, "SHORT_TAIL")
    rows = [json.loads(line) for line in lines[-3:]]
    _require(all(isinstance(row, dict) and row.get("sessionId") == binding["session_id"]
                 and row.get("isSidechain") is False for row in rows), "SESSION_OR_SIDECHAIN")
    call, result, end = rows
    _require([row.get("type") for row in rows] == ["assistant", "user", "user"], "RECORD_TYPES")
    cm, rm, em = [row.get("message", {}) for row in rows]
    _require(all(isinstance(msg, dict) for msg in (cm, rm, em)), "MESSAGE_SHAPE")
    _require(cm.get("role") == "assistant" and rm.get("role") == em.get("role") == "user", "ROLES")
    _require(em.get("content") == [{"type": "text", "text": INTERRUPTION}], "INTERRUPTION_TEXT")
    use = cm.get("content")
    _require(isinstance(use, list) and len(use) == 1 and isinstance(use[0], dict), "TOOL_CALL_SHAPE")
    use = use[0]
    _require(use.get("type") == "tool_use" and use.get("name") == "Bash"
             and isinstance(use.get("id"), str) and use["id"], "NOT_BASH_CALL")
    _require(rm.get("content") == [{"type": "tool_result", "content": REJECTED,
                "is_error": True, "tool_use_id": use["id"]}]
             and rm["content"][0]["is_error"] is True
             and result.get("toolUseResult") == "User rejected tool use", "NOT_REJECTED_RESULT")
    _require(all(isinstance(row.get("uuid"), str) and row["uuid"] for row in rows)
             and len({row["uuid"] for row in rows}) == 3
             and result.get("parentUuid") == result.get("sourceToolAssistantUUID") == call["uuid"]
             and end.get("parentUuid") == result["uuid"]
             and isinstance(cm.get("id"), str) and cm["id"]
             and end.get("interruptedMessageId") == cm["id"], "BROKEN_PARENT_CHAIN")
    times = [native._timestamp(row.get("timestamp")) for row in rows]
    _require(all(t is not None for t in times)
             and times[0] <= times[1] <= times[2] <= binding["bound_at_epoch"], "TIMESTAMPS")
    return {"file_stamp": list(stamp), "records_sha256": hashlib.sha256(b"".join(lines[-3:])).hexdigest(),
            "record_uuids": [row["uuid"] for row in rows], "tool_use_id": use["id"],
            "tool_started_epoch": times[0], "interrupted_at_epoch": times[2]}


def _children(binding, tool_started_epoch):
    """拒绝仍存续的本次 Bash 子进程或同组进程；旧常驻 MCP 可保留。

    全部后代及同 PGID 进程都必须早于本次 tool_use，且两次核实时身份不变。
    不用进程名称猜测终态，也不把客户端自身存在当作工具仍在执行。
    """
    listing = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,pgid="],
                             capture_output=True, text=True, check=True, timeout=3)
    rows = {}
    for line in listing.stdout.splitlines():
        fields = line.split()
        _require(len(fields) == 3 and all(f.isdecimal() for f in fields), "PROCESS_LIST_INVALID")
        pid, parent, group = map(int, fields)
        _require(pid not in rows, "DUPLICATE_PROCESS")
        rows[pid] = (parent, group)
    root = binding["process"]["pid"]
    _require(root in rows, "CLIENT_MISSING")
    selected = {root}
    for _ in range(len(rows)):
        expanded = selected | {p for p, (parent, _) in rows.items() if parent in selected}
        if expanded == selected:
            break
        selected = expanded
    selected |= {p for p, (_, group) in rows.items() if group == rows[root][1]}
    selected.discard(root)
    _require(len(selected) <= 64, "PROCESS_TREE_TOO_LARGE")
    result = []
    for pid in sorted(selected):
        p = native.identity.process(pid)
        _require(p["pid"] == pid and p["ppid"] == rows[pid][0]
                 and isinstance(p["birth"], list) and len(p["birth"]) == 2
                 and all(type(v) is int for v in p["birth"])
                 and p["birth"][0] > 0 and 0 <= p["birth"][1] < 1000000,
                 "PROCESS_PARENT_CHANGED")
        birth = p["birth"][0] + p["birth"][1] / 1000000
        _require(birth < tool_started_epoch, "TOOL_PROCESS_STILL_PRESENT")
        result.append(dict(native._public_process(p), ppid=p["ppid"], pgid=rows[pid][1]))
    return result


class InterruptedBashBoundary:
    """单次 probe 内的只读边界；任何输入、原生追加或进程变化即失效。"""

    def __init__(self, bridge, surface, screen, token):
        _require(isinstance(token, str) and len(token) <= 32
                 and re.fullmatch(r"B[1-9][0-9]*_[0-9a-f]{8}", token), "INVALID_PROBE_TOKEN")
        _screen(bridge, screen, token, initial=True)
        self.bridge, self.surface, self.token = bridge, surface, token
        self.binding = native.bind_target(bridge, surface, token)
        _require(self.binding["provider"] == "claude", "NOT_CLAUDE")
        self.tail = _tail(self.binding)
        self.children = _children(self.binding, self.tail["tool_started_epoch"])
        self.evidence = {"schema": "interrupted-bash-probe-v1", "binding": self.binding,
                         "terminal_chain": self.tail, "preexisting_processes": self.children,
                         "successful_rechecks": 0, "failure": None,
                         "scope": "NON_SUBMITTING_OWN_TOKEN_ONLY"}
        _require(self.check(screen), "INITIAL_RECHECK_FAILED")

    def check(self, screen):
        if self.evidence["failure"] is not None:
            return False
        try:
            _screen(self.bridge, screen, self.token)
            native.require_bound(self.bridge, self.surface, self.binding, self.token)
            _require(_tail(self.binding) == self.tail, "TRANSCRIPT_APPENDED_OR_CHANGED")
            _require(_children(self.binding, self.tail["tool_started_epoch"]) == self.children,
                     "PROCESS_TREE_CHANGED")
            _require(_tail(self.binding) == self.tail, "TRANSCRIPT_CHANGED_DURING_PROCESS_CHECK")
            self.evidence["successful_rechecks"] += 1
            return True
        except PROBE_ERRORS as exc:
            self.evidence["failure"] = (type(exc).__name__ + ": " + str(exc))[:240]
            return False
