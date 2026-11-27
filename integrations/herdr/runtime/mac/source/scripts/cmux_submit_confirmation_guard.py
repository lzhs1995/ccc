#!/usr/bin/env python3
"""
PostToolUse guard — 铁律：粘贴 ≠ 提交。

向 peer 投递（握手 ACK、任务包、完成回调、裸 bridge send）之后，本 guard 重读目标
surface，报告「协议行仍停在接收端 compose 框里、未提交」这一真实缺陷状态。

只读：从不发送文本、从不发送按键、从不写回执。检测失败时报 INDETERMINATE 而不是
静默放行（未量到的不算零）。

退出码：
  0  通过 / 跳过 / 仪器不可用（已显式说明）
  2  检测到「已粘贴未提交」，把判据和下一步回灌给模型

环境开关：
  CMUX_SUBMIT_GUARD_ADVISORY=1   降级为建议（始终 exit 0，仍打印）
  CMUX_SUBMIT_GUARD_LINES=N      读屏窗口，默认 2000（繁忙 supervisor 上窗口不足会假阴性）
  CMUX_SUBMIT_GUARD_DISABLE=1    完全停用
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from cmux_submission_inputs import (delivery_calls, is_bridge_help_command,
                                    target as submission_target)

# 触发词：只有疑似 peer 投递才去读屏，避免给每条 Bash 加开销。
_DELIVERY_HINTS = (
    "submit_completion_callback",
    "submit_task_pack",
    "submit_text",
    "cmux_bridge",
    "cmux-bridge",
    "cmux-agent ask",
    "cmux-agent broadcast",
    "cmux send",
    "send-key",
)

# 协议行形状：compose 框里出现这些，等于「该发出去的东西还没发出去」。
#
# 两种真实形态都必须覆盖（2026-10-04 peer 复核指出漏项，已实测确认）：
#   管道形态 DONE|<task>|<nonce>|REPORT=...      ← submit_completion_callback 的 pack 串
#   冒号形态 STATUS: / DONE: / BLOCKED: ...      ← executor-protocol.md 的 cmux-agent ask 形态
# 早期只写了管道形态，对 `STATUS: TASK_ID=... MILESTONE=...` 一律漏判 → guard 在
# 里程碑/阻断类投递上静默放行。
_PROTOCOL_LINE_RE = re.compile(
    r"(?:^|\s|›|❯)(?:DONE|BLOCKED|PREFLIGHT_ACK|STATUS|MILESTONE)\s*\|"
)
# 冒号形态按 executor-protocol.md 自己的判据锚定行首（"only when `DONE:` or
# `BLOCKED:` begins the line"）——这同时挡住散文里提到 DONE 的误报。
#
# 另外必须接受「引号起头」的同一形态：receiver 是 SHELL 时，compose 框里躺着的
# 是整条命令 `cmux-agent ask surface:42 "STATUS: TASK_ID=..."`，协议行在行中而非
# 行首。只收 ASCII 引号 ' " 、刻意不收反引号：协议文档里讲解协议的散文写的是
# 反引号形态（`` 必须 actively send `STATUS:` ``），收了就会把文档正文判成待发。
_PROTOCOL_COLON_RE = re.compile(
    r"""(?:^[ \t]*(?:[›❯][ \t]*)?|["'])"""
    r"(?:DONE|BLOCKED|STATUS|MILESTONE|PREFLIGHT_ACK)[ \t]*:",
    re.MULTILINE,
)
# `EXECUTOR REPORT | TASK_ID=… | STATUS=…` 整行形态
_EXECUTOR_REPORT_RE = re.compile(r"\bEXECUTOR\s+REPORT\s*\|")
_REPORT_BINDING_RE = re.compile(r"\|REPORT=")


def has_protocol_shape(text: str) -> bool:
    """任一协议形态命中即为真。导出以便测试直接打形态用例。"""
    if not isinstance(text, str) or not text:
        return False
    return bool(
        _PROTOCOL_LINE_RE.search(text)
        or _PROTOCOL_COLON_RE.search(text)
        or _EXECUTOR_REPORT_RE.search(text)
        or _REPORT_BINDING_RE.search(text)
    )

_SURFACE_RE = re.compile(r"surface:\d+")
_NONCE_RE = re.compile(r"\b[0-9a-f]{16}\b")

# UUID 形态的投递目标（2026-10-04 peer 复核 V5 指出的真缺口）。
#
# `cmux_bridge` 的模块说明自己写着「Surface refs are strings like "surface:17"
# or UUIDs」，而 `_run()` 在跑 send/send-key 前会把 `--surface <ref>` 原地改写成
# `proof["target_surface_uuid"]`。所以 hook 真正看到的命令里，目标常常已经是
# UUID；只认 `surface:\d+` 的话，这类命令一律落到 "no target surface resolvable"
# 而静默 skip —— 又一次在最该报警的命令上放行。
#
# 刻意要求 UUID 前面有 --surface / --target / surface= 之类的上下文：同一条命令里
# 还会出现 workspace UUID、task_id 等别的 UUID，裸扫会把它们当成读屏目标。
_UUID_BODY = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SURFACE_UUID_RE = re.compile(
    r"(?:--surface|--target(?:-surface)?|\bsurface\s*=|\btarget\s*=)"
    r"[\s=]+[\"']?(" + _UUID_BODY + r")[\"']?"
)

_LEDGER = (
    Path(os.environ.get("HOME", "/tmp"))
    / ".local/state/multi-agent-collaboration/submission-audit/ledger.jsonl"
)


def _window_lines() -> int:
    raw = os.environ.get("CMUX_SUBMIT_GUARD_LINES", "2000")
    try:
        return max(200, min(5000, int(raw)))
    except (TypeError, ValueError):
        return 2000


def _extract_commands(payload: dict[str, Any]) -> list[str]:
    """Read nested tool inputs as data; never execute JS or inspect tool output.

    Codex exec receives a raw JS string; exec_command receives a JSON object.
    Wrappers can JSON-encode either shape. Joining command leaves also covers
    parallel calls without accepting a tool_response as a new send operation.
    """
    commands: list[str] = []
    wrappers = ("tool_input", "toolInput", "input", "params", "arguments",
                "parameters", "tool_uses", "calls")
    def visit(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, RecursionError):
                decoded = None
            if isinstance(decoded, (dict, list, str)) and decoded != value:
                visit(decoded, depth + 1)
            elif value.strip() and value not in commands:
                commands.append(value)
        elif isinstance(value, dict):
            for key in ("command", "cmd", "script", "code", "source"):
                child = value.get(key)
                if isinstance(child, str):
                    visit(child, depth + 1)
            for key in wrappers:
                if key in value:
                    visit(value[key], depth + 1)
        elif isinstance(value, list):
            for child in value[:128]:
                visit(child, depth + 1)
    visit(payload)
    return commands


def _extract_command(payload: dict[str, Any]) -> str:
    return "\n".join(_extract_commands(payload))


def looks_like_delivery(command: str) -> bool:
    # Skip only a single literal CLI help invocation. A quoted --help in a
    # payload, shell compound, or Python/JS wrapper is still inspected.
    if is_bridge_help_command(command):
        return False
    low = command.lower()
    return any(hint.lower() in low for hint in _DELIVERY_HINTS)


def _task_pack_paths(command: str) -> list[Path]:
    """
    从命令串里取出任务包路径。

    必须同时支持带空格的路径：本机真实工程目录就叫 `20260928 spatial_analysis`，
    早期版本用 `[^\\s]*` 做路径字符类，对这类路径永远匹配不到 —— 于是 guard 在它
    最该报警的那条命令上静默 skip。先取引号包裹的（允许空格），再取裸路径。
    """
    found: list[Path] = []

    def _add(raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        path = Path(raw)
        if path not in found:
            found.append(path)

    # 1) 双引号 / 单引号包裹：内部允许空格
    for pattern in (r'"(/[^"]*task-pack[^"]*\.json)"', r"'(/[^']*task-pack[^']*\.json)'"):
        for match in re.finditer(pattern, command):
            _add(match.group(1))

    # 2) 裸路径（无引号时不含空格）
    for match in re.finditer(r"(?<![\"'])(/[^\s\"']*task-pack[^\s\"']*\.json)", command):
        _add(match.group(1))

    return found


def _nonce_from_task_pack(command: str) -> str | None:
    """命令点名了任务包就直接读出 completion_nonce，拿到精确 marker。"""
    for candidate in _task_pack_paths(command):
        if not candidate.is_file():
            continue
        try:
            pack = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(pack, dict):
            nonce = pack.get("completion_nonce")
            if isinstance(nonce, str) and nonce and "<" not in nonce:
                return nonce
    return None


def _candidate_markers(command: str) -> list[str]:
    markers: list[str] = []
    pack_nonce = _nonce_from_task_pack(command)
    if pack_nonce:
        markers.append(pack_nonce)
    for hit in _NONCE_RE.findall(command):
        if hit not in markers:
            markers.append(hit)
    return markers


_GLYPH_RE = re.compile(r"^\s*[›❯]\s")


def _fallback_compose_has_protocol(screen: str) -> bool | None:
    """
    bridge 谓词缺失时的自备判据：取最后一个 glyph 块，看里面有没有协议行。

    返回 True/False 为已量到；None 表示屏上找不到 glyph 块，无法定位 compose，
    此时调用方必须报 INDETERMINATE，绝不能当成「没有协议行」放行。
    """
    if not isinstance(screen, str) or not screen.strip():
        return None
    lines = screen.splitlines()
    glyph_rows = [i for i, line in enumerate(lines) if _GLYPH_RE.match(line)]
    if not glyph_rows:
        return None
    block = lines[glyph_rows[-1]:]
    joined = "\n".join(block)
    return has_protocol_shape(joined)


def _screen_has_protocol_anywhere(screen: str) -> bool:
    return has_protocol_shape(screen)


def _bridge():
    """导入同目录 bridge。失败不等于通过——调用方须报 INDETERMINATE。"""
    try:
        import cmux_bridge  # type: ignore

        return cmux_bridge
    except Exception:  # noqa: BLE001 - 仪器不可用本身就是要报告的事实
        return None


def _predicate(bridge, *names: str) -> Callable | None:
    for name in names:
        fn = getattr(bridge, name, None)
        if callable(fn):
            return fn
    return None


def classify_surface(screen: str, markers: list[str], bridge=None) -> dict[str, Any]:
    """
    判定一块屏幕。纯函数，便于注入合成屏做正/负对照。

    verdict:
      PENDING_UNSUBMITTED  协议行停在 compose 框（真缺陷）
      QUEUED               已离开 compose、但停在接收端待提交队列（尚未被消费）
      CONSUMED             由 evaluate 另行核对原 attempt 后授予
      NO_PROTOCOL_LINE     屏上没有协议行——**不等于已消费**，见下
      INDETERMINATE        读不到屏或谓词缺失

    三条不可混用的语义（2026-10-04 peer 复核 V5 指出前两条曾被合并）：

    * QUEUED ≠ CONSUMED。排队态只说明「接收端收下了、等自己的 tool 边界」，
      既不能据此宣布送达，也不能据此重发。曾把 any_queued 直接判成 CONSUMED，
      等于用「还没被读」证明「已经被读」。
    * NO_PROTOCOL_LINE ≠ CONSUMED。屏上没有协议行有三种成因：窗口不足
      （confirm_lines 太小）、已滚出可视区、或从未粘上。三者都不是消费证明。
      它只用来表达「这一屏没看到要核的东西」，动作上不阻断而已。
    * marker-only submission_confirmed 不足以证明这一次投递。
    """
    result: dict[str, Any] = {
        "verdict": "INDETERMINATE",
        "markers_checked": list(markers),
        "evidence": {},
    }
    if not isinstance(screen, str) or not screen.strip():
        result["evidence"]["reason"] = "screen empty or unreadable"
        return result

    bridge = bridge if bridge is not None else _bridge()
    compose_text_fn = _predicate(bridge, "compose_block_text") if bridge else None
    compose_empty_fn = _predicate(bridge, "compose_block_is_empty") if bridge else None
    pending_fn = _predicate(bridge, "_prompt_block_pending", "prompt_block_pending") if bridge else None
    confirmed_fn = _predicate(bridge, "_submission_confirmed", "submission_confirmed") if bridge else None
    queued_fn = _predicate(bridge, "pending_queue_holds") if bridge else None
    kind_fn = _predicate(bridge, "receiver_input_kind") if bridge else None

    compose_text = None
    if compose_text_fn:
        try:
            compose_text = compose_text_fn(screen)
        except Exception as exc:  # noqa: BLE001
            result["evidence"]["compose_text_error"] = str(exc)

    compose_is_empty = None
    if compose_empty_fn:
        try:
            compose_is_empty = bool(compose_empty_fn(screen))
        except Exception as exc:  # noqa: BLE001
            result["evidence"]["compose_empty_error"] = str(exc)

    receiver_kind = None
    if kind_fn:
        try:
            receiver_kind = kind_fn(screen)
        except Exception:  # noqa: BLE001
            receiver_kind = None

    result["evidence"].update(
        {
            "compose_is_empty": compose_is_empty,
            "receiver_kind": receiver_kind,
            "compose_has_protocol_line": None,
        }
    )

    # 第一判据：compose 框里是否有协议行。这是不依赖 marker 的形状判据。
    compose_has_protocol = None
    if isinstance(compose_text, str):
        compose_has_protocol = has_protocol_shape(compose_text)
        result["evidence"]["compose_has_protocol_line"] = compose_has_protocol
        result["evidence"]["compose_excerpt"] = compose_text[:240]
    else:
        # bridge 的 compose_block_text 不可用时改用自备 glyph 块判据。
        # 若连 glyph 块都找不到（返回 None），保持未量到，由下方报 INDETERMINATE。
        compose_has_protocol = _fallback_compose_has_protocol(screen)
        result["evidence"]["compose_has_protocol_line"] = compose_has_protocol
        result["evidence"]["compose_source"] = "fallback_last_glyph_block"

    # 第二判据：bridge 自己的 marker 级谓词。
    marker_states: dict[str, dict[str, Any]] = {}
    any_pending = False
    any_confirmed = False
    any_queued = False
    for marker in markers:
        state: dict[str, Any] = {}
        if pending_fn:
            try:
                state["prompt_block_pending"] = bool(pending_fn(screen, marker))
            except Exception as exc:  # noqa: BLE001
                state["prompt_block_pending_error"] = str(exc)
        if confirmed_fn:
            try:
                state["submission_confirmed"] = bool(confirmed_fn(screen, marker))
            except Exception as exc:  # noqa: BLE001
                state["submission_confirmed_error"] = str(exc)
        if queued_fn:
            try:
                state["pending_queue_holds"] = bool(queued_fn(screen, marker))
            except Exception as exc:  # noqa: BLE001
                state["pending_queue_holds_error"] = str(exc)
        marker_states[marker] = state
        any_pending = any_pending or state.get("prompt_block_pending") is True
        any_confirmed = any_confirmed or state.get("submission_confirmed") is True
        any_queued = any_queued or state.get("pending_queue_holds") is True
    if marker_states:
        result["evidence"]["marker_states"] = marker_states

    # 「量到」必须是真有判据返回了布尔值，不能因为 marker_states 这个 dict 存在就算。
    # 全是空 state（谓词缺失/全抛异常）时属未量到。
    marker_measured = any(
        isinstance(state.get(key), bool)
        for state in marker_states.values()
        for key in ("prompt_block_pending", "submission_confirmed", "pending_queue_holds")
    )
    measured = compose_has_protocol is not None or marker_measured
    if not measured:
        result["evidence"]["reason"] = (
            "no usable predicate (bridge import or API missing, and no glyph block "
            "found for fallback) — submission state NOT measured"
        )
        # 屏上别处出现协议行，但定位不到 compose：仍然是未量到，附线索供人判。
        result["evidence"]["protocol_line_somewhere_on_screen"] = _screen_has_protocol_anywhere(screen)
        return result

    if any_pending or compose_has_protocol:
        result["verdict"] = "PENDING_UNSUBMITTED"
        return result
    # 排队态必须独列：它既不是「停在 compose」也不是「已消费」。
    # 动作上与 CONSUMED 一样不阻断（重发才是真风险），但判据上绝不授予消费。
    if any_queued:
        result["verdict"] = "QUEUED"
        result["evidence"]["note"] = (
            "queued at receiver; wait for its tool boundary, do not resend. "
            "QUEUED is NOT proof of consumption."
        )
        return result
    if any_confirmed:
        result["verdict"] = "INDETERMINATE"
        result["evidence"]["reason"] = (
            "marker-only or historical glyph confirmation cannot identify this delivery; "
            "original attempt or exact native payload proof is required"
        )
        return result
    if compose_has_protocol is False:
        result["verdict"] = "NO_PROTOCOL_LINE"
        result["evidence"]["note"] = (
            "no protocol line in this window — NOT proof of consumption "
            "(could be insufficient confirm_lines, scrolled out, or never pasted)"
        )
        return result
    # compose 判据未量到、marker 谓词也没给出肯定结论：不得按通过收尾。
    result["evidence"]["reason"] = "marker predicates inconclusive and compose not measured"
    result["verdict"] = "INDETERMINATE"
    return result


def _attempt_evidence(call: dict[str, Any], surface: str, bridge) -> dict[str, Any] | None:
    """Re-read this literal task/callback's durable attempt; never create a receipt.

    A confirmed flag, a nonce visible in scrollback, or a tool's returned JSON
    alone is insufficient. Recheck the pinned payload/files, target identity and
    full before/after observation through the sender's strict classifier.
    """
    if (bridge is not None and call.get("kind") == "text"
            and call.get("surface") == surface and call.get("text")):
        from cmux_message_journal import verified_receipt
        return verified_receipt(bridge, surface, call["text"], call.get("marker"))
    if call.get("kind") not in ("task", "callback") or not call.get("pack") or bridge is None:
        return None
    if call["kind"] == "task" and call.get("surface") == surface and call.get("text"):
        from cmux_task_journal import verified_receipt
        proof = verified_receipt(bridge, surface, call["text"], call.get("marker"), call["pack"])
        if proof is not None:
            return proof
    if call['kind'] == 'callback':
        from cmux_callback_journal import verified_receipt
        proof = verified_receipt(bridge, surface, call['pack'])
        if proof is not None:
            return proof
    from cmux_delivery_evidence import confirmed, contains, digest, own_draft
    from delivery_receipts import snapshot
    for candidate in [Path(call["pack"])]:
        try:
            if not candidate.is_absolute():
                continue
            pack_pin = snapshot(candidate)
            is_callback = call["kind"] == "callback"
            if is_callback:
                pack = bridge.validate_task_pack_contract(candidate)
                if submission_target(pack.get("callback_target")) != surface:
                    continue
                marker = pack["completion_nonce"]
                text = pack["completion_callback"]
            else:
                text, marker = call.get("text"), call.get("marker")
                # Never infer the sent task text/marker from the callback fields.
                if call.get("surface") != surface or not text or not marker:
                    continue
                pack = bridge.validate_task_pack_contract(candidate, prompt_text=text)
            if not isinstance(marker, str) or not marker or not isinstance(text, str) or not text:
                continue
            report_pin = snapshot(pack["report"]) if is_callback else None
            live = bridge.pin_workspace(surface)
            identity = {k: live[k] for k in ("workspace_uuid", "caller_surface_uuid",
                                             "target_surface_uuid", "target_pane_uuid")}
            if not all(isinstance(v, str) and v for v in identity.values()):
                continue
            key = digest(json.dumps([identity["caller_surface_uuid"], marker], sort_keys=True))
            path = Path.home() / ".local/state/multi-agent-collaboration/deliveries-v1" / (key + ".json")
            attempt_pin = snapshot(path)
            attempt = json.loads(path.read_text())
            files = {str(candidate.resolve()): pack_pin["sha256"]}
            if is_callback:
                files[str(Path(pack["report"]))] = report_pin["sha256"]
            if (type(attempt.get("version")) is not int or attempt["version"] != 1
                    or attempt.get("identity") != identity or attempt.get("marker") != marker
                    or attempt.get("payload_sha256") != digest(text)
                    or attempt.get("binding") != files or attempt.get("phase") != "confirmed"
                    or attempt.get("paste_intent") is not True
                    or type(attempt.get("enter_attempts")) is not int
                    or attempt["enter_attempts"] not in (0, 1, 2)):
                continue
            before, after = attempt.get("before_screen"), attempt.get("last_screen")
            if not isinstance(before, str) or not isinstance(after, str):
                continue
            adopted = attempt.get("adopted_pending", False)
            if type(adopted) is not bool:
                continue
            if adopted:
                glyphs = [i for i, line in enumerate(before.splitlines())
                          if bridge._PROMPT_GLYPH_RE.match(line)]
                prefix = "\n".join(before.splitlines()[:glyphs[-1]]) if glyphs else before
                if not own_draft(bridge, before, text) or contains(prefix, marker):
                    continue
            if not confirmed(bridge, before, after, marker, text, adopted=adopted):
                continue
            if (snapshot(candidate) != pack_pin or (is_callback and snapshot(pack["report"]) != report_pin)
                    or snapshot(path) != attempt_pin or bridge.pin_workspace(surface) != live):
                continue
            return {"source": "revalidated_original_" + call["kind"] + "_attempt", "attempt": attempt_pin,
                    "pack": pack_pin, "report": report_pin, "identity": identity}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            # An unavailable or incompatible historical proof stays unknown.
            continue
    return None


_STATE_ROOT = Path(os.environ.get("HOME", "/tmp")) / ".local/state/multi-agent-collaboration"


def stranded_attempts(caller: str | None, reader: Callable[[str, int], str], bridge,
                      window_seconds: float = 900.0, now: float | None = None) -> list[dict[str, Any]]:
    """Enter ≠ 送达：本 caller 的原 attempt 已按 Enter、无 receipt、marker 仍在接收端 compose。

    2026-10-08 实测：自定义 marker（非 16-hex、非协议形状）的普通消息卡在 Codex
    compose 框，形状判据全部漏判，本 guard 静默放行。这里改以原 journal 为准：
    不看命令长相，只看本 caller 自己的未确认 attempt 与接收端当前 compose。
    """
    if not caller or bridge is None:
        return []
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    for folder in ("message-dispatch-v1", "task-dispatch-v1"):
        for path in (_STATE_ROOT / folder).glob("*/attempt-*.json"):
            try:
                if now - path.stat().st_mtime > window_seconds:
                    continue
                if (path.parent / "receipt.json").exists():
                    continue
                attempt = json.loads(path.read_text(encoding="utf-8"))
                binding = attempt.get("binding") or {}
                identity = binding.get("identity") or {}
                if str(identity.get("caller_surface_uuid", "")).upper() != caller.upper():
                    continue
                phases = [e.get("phase") for e in attempt.get("events", [])]
                marker = binding.get("marker")
                target = identity.get("target_surface_uuid")
                if (attempt.get("phase") == "CONFIRMED" or "ENTER_SENT" not in phases
                        or not isinstance(marker, str) or not marker or not target):
                    continue
                screen = reader(target, _window_lines())
                if not bridge.compose_contains(screen, marker):
                    continue
                used = "QUEUE_TAB_INTENT" in phases or "EXTRA_ENTER_INTENT" in phases
                out.append({"journal": folder, "attempt": str(path), "marker": marker,
                            "target": target, "task_id": binding.get("task_id"),
                            "recovery_used": used})
            except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
                continue
    return out


def evaluate(payload: dict[str, Any], reader: Callable[[str, int], str] | None = None,
             bridge=None, caller: str | None = None) -> dict[str, Any]:
    """返回 {'action': 'skip'|'pass'|'warn', ...}。reader 可注入以便测试。"""
    command = _extract_command(payload)
    if not command or not looks_like_delivery(command):
        return {"action": "skip", "reason": "not a peer-delivery command", "surfaces": {}}

    calls = [call for source in _extract_commands(payload) for call in delivery_calls(source)]
    surfaces = []
    for call in calls:
        if call.get("kind") == "callback" and call.get("pack"):
            try:
                pack = json.loads(Path(call["pack"]).read_text(encoding="utf-8"))
                call["surface"] = submission_target(pack.get("callback_target"))
                call["marker"] = pack.get("completion_nonce")
            except (OSError, ValueError, AttributeError):
                pass
        surface = call.get("surface")
        if surface and surface not in surfaces:
            surfaces.append(surface)
    if caller is None:
        caller = os.environ.get("CMUX_SURFACE_ID")
    journal_bridge = bridge if bridge is not None else _bridge()
    journal_read = reader
    if journal_read is None and journal_bridge is not None and callable(getattr(journal_bridge, "read_screen", None)):
        def journal_read(surface: str, n: int, _fn=journal_bridge.read_screen) -> str:  # type: ignore[misc]
            return _fn(surface, lines=n)
    stranded = (stranded_attempts(caller, journal_read, journal_bridge)
                if journal_read is not None else [])
    if not surfaces:
        if stranded:
            return {"action": "warn", "surfaces": {}, "stranded": stranded, "markers": [],
                    "confirm_lines": _window_lines()}
        return {"action": "pass", "reason": "INDETERMINATE: delivery command has no resolvable target; "
                "submission was NOT verified", "indeterminate": True, "surfaces": {}}

    markers_by_surface = {surface: [] for surface in surfaces}
    for call in calls:
        own_markers = markers_by_surface.get(call.get("surface"))
        if own_markers is None:
            continue
        explicit = call.get("marker")
        if isinstance(explicit, str) and explicit:
            candidates = [explicit]
        elif call.get("kind") == "legacy":
            candidates = _candidate_markers(command)
        else:
            candidates = _NONCE_RE.findall(call.get("text") or "")
        own_markers.extend(m for m in candidates if m not in own_markers)
    markers = list(dict.fromkeys(m for own in markers_by_surface.values() for m in own))
    lines = _window_lines()

    if reader is None:
        active = bridge if bridge is not None else _bridge()
        read_fn = _predicate(active, "read_screen") if active else None
        if read_fn is None:
            return {
                "action": "pass",
                "reason": "INDETERMINATE: cmux_bridge.read_screen unavailable; "
                          "submission was NOT verified",
                "indeterminate": True,
                "surfaces": {},
            }

        def reader(surface: str, n: int, _fn=read_fn) -> str:  # type: ignore[misc]
            return _fn(surface, lines=n)

    findings: dict[str, Any] = {}
    pending_any = False
    for surface in surfaces:
        try:
            screen = reader(surface, lines)
        except Exception as exc:  # noqa: BLE001
            findings[surface] = {"verdict": "INDETERMINATE",
                                 "evidence": {"read_error": str(exc)}}
            continue
        verdict = classify_surface(screen, markers_by_surface[surface], bridge=bridge)
        if verdict["verdict"] not in ("PENDING_UNSUBMITTED", "QUEUED") and screen.strip():
            active = bridge if bridge is not None else _bridge()
            relevant = [call for call in calls if call.get("surface") == surface]
            proofs = [_attempt_evidence(call, surface, active) for call in relevant]
            # Multiple sends to one surface must each prove their own payload.
            if proofs and all(proofs):
                verdict["verdict"] = "CONSUMED"
                verdict["evidence"]["consumption_proofs"] = proofs
        verdict["confirm_lines"] = lines  # 读数只有带窗口大小才是事实
        findings[surface] = verdict
        pending_any = pending_any or verdict["verdict"] == "PENDING_UNSUBMITTED"

    return {
        "action": "warn" if pending_any or stranded else "pass",
        "surfaces": findings,
        "stranded": stranded,
        "markers": markers,
        "confirm_lines": lines,
    }


def _record(entry: dict[str, Any]) -> None:
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _render(result: dict[str, Any]) -> str:
    lines = [
        "",
        "[multi-agent 铁律] 粘贴 ≠ 提交：检测到协议行仍停在接收端 compose 框，未提交。",
        "",
    ]
    for surface, finding in result.get("surfaces", {}).items():
        if finding.get("verdict") != "PENDING_UNSUBMITTED":
            continue
        evidence = finding.get("evidence", {})
        lines.append(f"  目标 {surface}  confirm_lines={finding.get('confirm_lines')}")
        lines.append(f"    compose_is_empty       = {evidence.get('compose_is_empty')}")
        lines.append(f"    compose 内含协议行      = {evidence.get('compose_has_protocol_line')}")
        lines.append(f"    receiver_kind          = {evidence.get('receiver_kind')}")
        excerpt = evidence.get("compose_excerpt")
        if excerpt:
            lines.append(f"    compose 摘录           = {excerpt!r}")
        for marker, state in (evidence.get("marker_states") or {}).items():
            lines.append(f"    marker {marker}: {json.dumps(state, ensure_ascii=False)}")
        lines.append("    完成回调卡在 compose：原执行者用 submit_completion_callback(pack, "
                     "resume_queue_only=True) 只补一次 Tab；普通消息用 --recover-stranded。")
    for item in result.get("stranded") or []:
        lines.append(f"  STRANDED_IN_COMPOSE  marker={item['marker']}  target={item['target']}")
        lines.append(f"    原 attempt = {item['attempt']}")
        lines.append("    已按 Enter 但 payload 仍在接收端 compose：未送达，不是「已排队」。")
        if item.get("recovery_used"):
            lines.append("    恢复键预算已用尽：只读 --reconcile-only 核收，不再补键。")
        elif item["journal"] == "message-dispatch-v1":
            lines.append("    恢复（同一 attempt、只补一键、不重贴）：cmux_bridge.py submit-text "
                         "--surface <同一目标> --text <原文> --marker <原 marker> --recover-stranded")
        else:
            lines.append("    任务包投递：由原发送器只读核收；不得重贴。")
    lines += [
        "",
        "  沿原 attempt 只读核收，保留原 payload、nonce、身份与发送预算。",
        "  只有核对原 attempt 的完整 payload 及新响应证据，或核对原生 user 入站记录，",
        "  才可宣布消费确认。排队、旧 glyph、nonce 回显、空 compose 和通用活动均不足。",
        "  QUEUED = 等待消费，不能重发；UNKNOWN = 未核实，不能推断没有发送。",
        "  本 Hook 不补按 Enter/Tab。恢复只能由原受保护发送器核原草稿与剩余预算。",
        "  禁止：把返回值或退出码当送达证据；手写 completion-callback-receipt.json；",
        "        在繁忙 supervisor 上用默认窗口判定「未送达」。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    if os.environ.get("CMUX_SUBMIT_GUARD_DISABLE") == "1":
        return 0
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        result = evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - guard 自身故障不该打断用户工作
        sys.stderr.write(
            f"\n[multi-agent 铁律] guard 自身异常，未完成提交核验：{exc}\n"
            "本次投递的送达状态属未量到，请手动只读复判。\n\n"
        )
        return 0

    if result["action"] == "skip":
        return 0

    if result.get("indeterminate"):
        sys.stderr.write(
            "\n[multi-agent 铁律] 未能核验提交状态（仪器不可用）："
            f"{result.get('reason')}\n请手动只读复判接收端。\n\n"
        )
        return 0

    _record({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": result["action"],
        "markers": result.get("markers"),
        "confirm_lines": result.get("confirm_lines"),
        "surfaces": {k: v.get("verdict") for k, v in result.get("surfaces", {}).items()},
        "stranded": [item["marker"] for item in result.get("stranded") or []],
    })

    if result["action"] == "warn":
        sys.stderr.write(_render(result))
        if os.environ.get("CMUX_SUBMIT_GUARD_ADVISORY") == "1":
            return 0
        return 2
    notes = {
        "QUEUED": "已排队，尚未证明消费；等待原投递，禁止重发。",
        "CONSUMED": "已复核原任务或 callback attempt 的绑定及完整 payload 消费证据。",
        "NO_PROTOCOL_LINE": "此窗口未见协议行，消费状态未核实；禁止凭此重发。",
        "INDETERMINATE": "消费状态未核实；只读核原 attempt，禁止盲目重贴或补键。",
    }
    for surface, finding in result.get("surfaces", {}).items():
        verdict = finding.get("verdict", "INDETERMINATE")
        sys.stderr.write(f"[multi-agent 提交核验] {surface} {verdict}: "
                         f"{notes.get(verdict, notes['INDETERMINATE'])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
