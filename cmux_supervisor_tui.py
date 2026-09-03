#!/usr/bin/env python3
"""Curses management surface for cmux-codex-continue.

This module never sends the continuation message. It only discovers candidates,
reads runtime state, and invokes the core CLI for confirmed configuration
mutations.
"""

from __future__ import annotations

import contextlib
import curses
import datetime as dt
import io
import json
import locale
import re
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import cmux_codex_watch as core


SOURCE_LABELS = {
    "untracked": "未登记",
    "explicit": "单路",
    "workspace_rule": "整池",
    "workspace_excluded": "已排除",
    "workspace_non_codex": "整池·非Codex",
}

# Management status: registered vs paused vs untracked.  Distinct from the
# Codex screen state in the 画面 column.
WATCH_LABELS = {
    "untracked": "未登记",
    "watching": "监控中",
    # 空转 = registered, not paused, but no Codex is running there, so the daemon
    # polls it and the screen fingerprint always refuses.  It is a *monitoring*
    # state, which is why it lives here and not in the 程序 column.
    "idling": "空转",
    "pool_idling": "整池空转",
    "paused": "已暂停",
    "pool": "整池",
    "excluded": "已排除",
}

# 程序 column vocabulary.  Module level so the README-consistency test can prove
# the documented list still covers every label the code can print.
AGENT_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
    "grok": "grok",
    "copilot": "Copilot",
    "gh": "gh",
    "shell": "shell",
    "other": "其他",
    "unknown": "未知",
}

ERROR_LABELS = {
    "rate_limit": "429",
    "high_demand": "高需求",
    "http_503": "503",
    "http_405": "405",
    "stream": "断流",
    "prompt_cache": "缓存400",
    "claude_503": "503",
    "claude_429": "429",
    "claude_overloaded": "过载",
    "claude_quota": "额度",
    "claude_retry": "重试",
    "claude_api": "API",
    "claude_stopped": "已停止",
    "claude_stream": "断流",
    "claude_context_waiting": "等压缩",
    "claude_context_compacting": "压缩中",
    "claude_context_stalled": "压缩失败",
    "claude_hook_gap_exhausted": "需人工",
}

STATE_LABELS = {
    "working": "运行中",
    "idle": "空闲",
    "menu": "菜单",
    "recoverable_error": "待续跑",
    "awaiting_transition": "等待恢复",
    "incompatible": "看不清",
    "missing": "已消失",
    "missing_or_error": "无画面",
    "unknown": "未知",
    "composer_busy": "正在输入",
    # Codex accepted our message but has not consumed it yet; sending more would
    # only lengthen its queue.
    "queued_followup": "已排队",
    # A real error is on screen, but Codex printed something after it, so it is
    # history and we deliberately do not send.  Shown as its own state rather
    # than plain 空闲: if that judgement is ever wrong, it has to be visible
    # here instead of hiding among genuinely healthy sessions.
    "error_superseded": "已过时",
    # Registered Claude surface, but the Claude send path is switched off.
    # Distinct from 空转: the pane is Claude on purpose, we are choosing not
    # to nudge it.
    "claude_observed": "Claude关",
    # Claude stop events are single-shot: stopped sends once, completed waits
    # for the user, pending never queues a duplicate prompt.
    "claude_stopped": "待续跑",
    "claude_input_guard": "输入保护",
    "claude_completed": "已完成",
    "claude_pending_input": "已续跑",
    "claude_hook_waiting": "Hook等待",
    "claude_event_pending": "待续跑",
    "claude_event_reserved": "发送中",
    "claude_event_sent": "已续跑",
    "claude_hook_missing": "Hook缺失",
    "claude_hook_unverified": "Hook待验",
    "claude_hook_legacy": "旧Hook",
    "claude_hook_config_degraded": "Hook配置错",
    "claude_hook_gap_candidate": "双帧确认",
    "claude_hook_gap_exhausted": "续跑限次",
    "claude_identity_conflict": "身份冲突",
    "claude_needs_human": "需人工",
    "claude_context_waiting": "等待压缩",
    "claude_context_compacting": "压缩中",
    "claude_context_stalled": "需人工",
    "non_codex_or_unknown": "非Codex",
    "blocked_manager": "已阻断",
    "blocked_dock": "已阻断",
    "send_guard_unavailable": "验证失败",
    "untracked": "未登记",
    # v3 states.  screen_label() feeds the 8-column 画面 cell from this dict, so
    # every value stays within that budget.
    "claude_viewport_blind": "读不出",
    "claude_deferred_expired": "挂起超时",
    "claude_deferred_retrying": "重试中",
    "claude_submit_pending": "提交中",
    "claude_submit_unconfirmed": "未确认",
    "cmux_unavailable": "无cmux",
}

DETAIL_FALLBACKS = {
    "incompatible": "界面无法验证",
    "missing": "目标已消失",
    "missing_or_error": "目标读取失败",
    "blocked_manager": "Supervisor 管理面",
    "blocked_dock": "Dock 管理面",
    "send_guard_unavailable": "无法验证 Dock 状态",
    "claude_observed": "Claude 功能关闭",
    "claude_stopped": "Claude 已停止，本次事件可续跑一次",
    "claude_input_guard": "等待输入保护期结束；用户输入优先",
    "claude_completed": "Claude 已完成；仍持续监控，下个任务自动恢复",
    "claude_pending_input": "本次停止已续跑，不会重复排队",
    "claude_hook_waiting": "等待 Claude Stop/StopFailure 事件；画面本身不会触发发送",
    "claude_event_pending": "已收到 Claude 停止事件，正在进行输入保护校验",
    "claude_event_reserved": "事件已预留，正在调用 cmux send",
    "claude_event_sent": "本次 Claude 停止事件已续跑",
    "claude_hook_missing": "未收到 Claude 生命周期 Hook，保持监控但不猜测发送",
    "claude_hook_unverified": "等待 SessionStart 或下一条 Claude 生命周期 Hook 验证",
    "claude_hook_legacy": "旧 Claude 会话未加载 CCC Hook；重启/恢复一次即可，登记不变",
    "claude_hook_config_degraded": "全局 Claude Hook 配置缺失或损坏；守护器会尝试无损修复",
    "claude_hook_gap_candidate": "Hook 未到，正在验证第二帧停止画面",
    "claude_identity_conflict": "Hook 会话与 surface 身份无法唯一对应",
    "claude_needs_human": "连续救援无效，需人工",
    "claude_context_waiting": "上下文已到顶，等待 Claude 自动压缩；不发送续跑",
    "claude_context_compacting": "Claude 正在压缩上下文；不发送续跑",
    "claude_context_stalled": "上下文压缩失败，需人工；目标仍持续监控",
    # v3 states.  Free-form focus text, so these may be full sentences.
    "claude_viewport_blind": "画面无法解析，仍持续监控；不据此发送，无需重新登记",
    "claude_deferred_expired": "已挂起的停止事件等待过久，需人工；目标仍持续监控",
    "claude_deferred_retrying": "挂起事件正在重新尝试；仍持续监控，直到当前停止画面安全续跑",
    "claude_submit_pending": "文本已写入，正在等待安全时机按 Enter",
    "claude_submit_unconfirmed": "提交确认超时，未能证实是否已送达",
    "cmux_unavailable": "cmux 不可用；这是基础设施问题，不计入画面盲区",
}

# Column-width versions of the same states: the 错误 column is 8 columns, so the
# long wording only fits on the focus line under the table.
DETAIL_SHORT = {
    "incompatible": "看不清",
    "missing": "已消失",
    "missing_or_error": "读不到",
    "blocked_manager": "管理面",
    "blocked_dock": "管理面",
    "send_guard_unavailable": "验不了",
    "claude_observed": "Claude关",
    "claude_stopped": "已停止",
    "claude_input_guard": "保护中",
    "claude_completed": "已完成",
    "claude_pending_input": "已续跑",
    "claude_hook_waiting": "等Hook",
    "claude_event_pending": "待续跑",
    "claude_event_reserved": "发送中",
    "claude_event_sent": "已续跑",
    "claude_hook_missing": "缺Hook",
    "claude_hook_unverified": "待验",
    "claude_hook_legacy": "需重启",
    "claude_hook_config_degraded": "配置错",
    "claude_hook_gap_candidate": "验二帧",
    "claude_identity_conflict": "身份错",
    "claude_needs_human": "需人工",
    "claude_context_waiting": "等压缩",
    "claude_context_compacting": "压缩中",
    "claude_context_stalled": "压缩失败",
    # v3 states.  Without an entry here state_label() returns the raw key and
    # pad() clips it to 8 columns, so 画面 showed 'claude_v' / 'claude_d' --
    # truncated English that cannot be told apart.
    "claude_viewport_blind": "读不出",
    "claude_deferred_expired": "挂起超时",
    "claude_deferred_retrying": "重试中",
    "claude_submit_pending": "提交中",
    "claude_submit_unconfirmed": "未确认",
    "cmux_unavailable": "无cmux",
}

FILTERS = ("all", "watched", "untracked")
DEFAULT_FILTER = "all"
FILTER_LABELS = {
    "watched": "只看已监控",
    "untracked": "只看未登记",
    "all": "全部workspace/surface",
}


@dataclass
class Candidate:
    record: dict[str, str]
    source: str
    state: str
    error_type: str
    send_count: int
    paused: bool
    selected_hint: bool = False
    status_detail: str = ""
    agent_kind: str = "unknown"
    process_summary: str = ""
    hook_health: str = ""
    consecutive_resumes: int = 0
    repeat_warning: bool = False
    hook_live_sends: int = 0
    hook_sla_misses: int = 0
    context_status: str = "unknown"
    context_percent: int | None = None
    compaction_percent: int | None = None
    # Duration fields.  A bare "缺失" said nothing about exposure: during the
    # 2026-08-24 observation one pane sat unprotected for 14.5h and another was
    # unreadable for the full 5.65h, and neither number was visible anywhere.
    unprotected_sec: float = 0.0
    unreadable_sec: float = 0.0
    # Live viewport reading plus the age of the stored verdict, so two different
    # percentages can no longer look like a parser bug.
    context_age_sec: float | None = None
    # One Stop parked because the frame was transiently unsafe.  Measured on
    # 2026-08-25: such events used to be dropped outright, and while most were
    # rescued by a later Stop within ~20s, the tail reached 113 minutes and three
    # never recovered.  A parked event is now visible instead of silent.
    deferred_reason: str = ""
    deferred_sec: float = 0.0
    # Resolved agent session identity.  Carried on the candidate rather than in
    # a side map so `filter_candidates` can search it: that function only ever
    # looks at `item.*`, so an id held anywhere else would be unsearchable.
    session: SessionResult = field(default_factory=lambda: SessionResult())

    @property
    def session_text(self) -> str:
        """What the session column shows for this row."""
        if self.session.ok:
            return str(self.session.session_id)
        return SESSION_UNMEASURED

    @property
    def surface_id(self) -> str:
        return self.record["surface_id"]

    @property
    def ref(self) -> str:
        return str(self.record.get("ref") or self.surface_id[:8])

    @property
    def workspace_ref(self) -> str:
        return str(self.record.get("workspace_ref") or self.record.get("workspace_id") or "?")


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def is_idling(candidate: Candidate) -> bool:
    """Registered and not paused, but nothing that looks like Codex is running.

    A target you registered stays registered after its Codex exits, so the
    daemon keeps polling it and the screen fingerprint keeps refusing to send.
    That is correct but invisible: the row used to read 监控中 / 其他 / 空闲 with
    no hint that it is doing nothing.

    A target whose surface has vanished is *not* idling — it is paused with a
    diagnostic, which is a different and more specific story.
    """
    if candidate.source == "untracked":
        return False
    if candidate.state == "claude_observed":
        # The adapter is disabled, so this registered target is observation-only.
        return True
    if candidate.state in {
        "working",
        "menu",
        "recoverable_error",
        "awaiting_transition",
        "composer_busy",
        "queued_followup",
        "error_superseded",
        "claude_stopped",
        "claude_input_guard",
        "claude_completed",
        "claude_pending_input",
        "claude_hook_waiting",
        "claude_event_pending",
        "claude_event_reserved",
        "claude_event_sent",
        "claude_hook_gap_exhausted",
        "claude_hook_missing",
        "claude_hook_unverified",
        "claude_hook_legacy",
        "claude_identity_conflict",
        "claude_needs_human",
    }:
        # Live send/wait states stay 监控中 even when the process is Claude.
        return False
    if candidate.state in DETAIL_FALLBACKS:
        return False
    if candidate.source == "workspace_non_codex":
        return True
    if candidate.paused or candidate.source == "workspace_excluded":
        return False
    return candidate.agent_kind != "codex"


def program_label(candidate: Candidate) -> str:
    """程序 column: which CLI is in that pane.  Identity only, never a state.

    An earlier version returned "空转" here, which overwrote the identity: a
    registered Claude pane read as 空转 and you could no longer see it was
    Claude.  The idling state now lives in the 监控 column instead.
    """
    return agent_label(candidate.agent_kind)


def watch_kind(candidate: Candidate) -> str:
    if candidate.source == "untracked":
        return "untracked"
    if candidate.source == "workspace_excluded":
        return "excluded"
    if candidate.source in {"workspace_rule", "workspace_non_codex"}:
        return "pool_idling" if is_idling(candidate) else "pool"
    if candidate.source == "explicit" and candidate.paused:
        return "paused"
    if candidate.source == "explicit":
        return "idling" if is_idling(candidate) else "watching"
    return "untracked"


def watch_label(candidate: Candidate) -> str:
    return WATCH_LABELS.get(watch_kind(candidate), "未登记")


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def screen_label(candidate: Candidate) -> str:
    """画面 column: only meaningful for a Codex UI.

    The fingerprint calls an empty shell "idle", which read as if the target
    were a healthy Codex waiting for work.  When there is no Codex to read,
    say nothing rather than something wrong.
    """
    if candidate.source == "untracked":
        return "—"
    if is_idling(candidate) and candidate.state not in DETAIL_FALLBACKS:
        return "—"
    return state_label(candidate.state)


def _elapsed_since(value: object) -> float:
    """Seconds since an epoch stamp in state.json; 0.0 when unset or unusable.

    state.json is written by a separate process and may lag or carry a
    partially-written row, so a bad value must degrade to "no duration known"
    rather than raise inside the draw loop.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value <= 0:
        return 0.0
    return max(0.0, time.time() - float(value))


def agent_label(agent_kind: str) -> str:
    """程序 column: identity only.  "shell" means the agent exited and left a
    shell behind; "未知" means we got no process information at all."""
    return AGENT_LABELS.get(agent_kind, agent_kind)


def compact_duration(seconds: float) -> str:
    """At most 3 columns of ASCII: ``45m`` / ``2h`` / ``12h`` / ``3d``.

    Deliberately ASCII: :func:`display_width` treats East Asian "Ambiguous"
    characters as one column, which is a judgement call, so those characters are
    barred from anything that goes through :func:`pad`.  A duration that shifted
    a column would be worse than no duration at all.
    """

    if seconds <= 0:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)}m"
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours}h"
    return f"{int(seconds // 86400)}d"


HOOK_LABELS = {
    "healthy": "正常",
    "unverified": "待验证",
    "legacy_override": "旧会话",
    "missing": "缺失",
    "offline": "离线",
}


def fit_suffix(label: str, suffix: str, width: int) -> str:
    """Append ``suffix`` only if the result still fits ``width`` columns.

    A silently clipped duration is worse than none: "无保护12h" cut to 8 columns
    renders as "无保护1", which reads as one hour instead of twelve.  When the
    duration cannot fit, the column keeps the plain label and the focus line
    under the table carries the full wording.
    """

    if not suffix:
        return label
    combined = f"{label}{suffix}"
    return combined if display_width(combined) <= width else label


HOOK_COLUMN_WIDTH = 8


def hook_label(candidate: Candidate) -> str:
    """Hook column: health plus how long this pane has been unprotected.

    A bare "缺失" hid a real exposure for 5.65 hours during the 2026-08-24
    observation: surface:74 had no trustworthy Hook generation for ~14.5h and
    nothing on screen said so.  The duration is what makes it actionable.
    """

    if candidate.agent_kind != "claude" and not candidate.state.startswith("claude"):
        return "—"
    label = HOOK_LABELS.get(candidate.hook_health or "unverified", candidate.hook_health or "待验证")
    if candidate.hook_sla_misses:
        return f"{label}!{candidate.hook_sla_misses}"
    # "unverified" is a 30s grace window, so a duration there is noise, not signal.
    if candidate.hook_health in {"missing", "legacy_override", "offline"}:
        return fit_suffix(label, compact_duration(candidate.unprotected_sec), HOOK_COLUMN_WIDTH)
    return label


def diagnostic_detail(candidate: Candidate) -> str:
    """Full reason the daemon recorded, with its command noise stripped."""
    detail = " ".join(str(candidate.status_detail or "").split())
    for prefix in ("incompatible: ", "cmux read-screen --workspace failed: ", "Error: "):
        while detail.lower().startswith(prefix.lower()):
            detail = detail[len(prefix):]
    return detail


def error_label(candidate: Candidate) -> str:
    """Why this row is where it is: current diagnostic first, else last trigger.

    A row the daemon can no longer read is described by its diagnostic, not by
    whatever error it hit hours ago.  Showing a stale "429" for a surface that
    has vanished reads as "still rate limited" and buries the real reason, which
    the focus line under the table spells out in full.
    """
    if candidate.source == "untracked":
        return "—"
    if candidate.context_status == "stalled":
        return "压缩失败"
    if candidate.context_status == "limit_waiting":
        return "等待压缩"
    if candidate.context_status == "compacting":
        return "压缩中"
    if candidate.context_status == "warning" and candidate.state not in {
        "recoverable_error",
        "claude_event_pending",
    }:
        return "上下文高"
    if candidate.state in DETAIL_SHORT:
        label = DETAIL_SHORT[candidate.state]
        # A pane the parser cannot read stays fail-open on context, so how long
        # it has been blind is the actionable part.  surface:72 was blind for the
        # entire 5.65h observation window with nothing on screen saying so.
        if candidate.state == "incompatible" and candidate.unreadable_sec > 0:
            return fit_suffix(
                label, compact_duration(candidate.unreadable_sec), ERROR_COLUMN_WIDTH,
            )
        return label
    key = str(candidate.error_type or "")
    if key not in {"", "-", "None", "null"}:
        return ERROR_LABELS.get(key, key)
    return "—"


CONTEXT_COLUMN_WIDTH = 10
ERROR_COLUMN_WIDTH = 8
# Below this, a reading is current enough that its age is noise on screen.
CONTEXT_STALE_SEC = 120.0


def context_label(candidate: Candidate) -> str:
    """上下文 column: the percentage plus how old that reading is.

    This TUI reads ``state.json``; it never replays a viewport itself, so every
    number here is the daemon's stored verdict.  On 2026-08-24 a stored 81% sat
    beside a live 95% for the same pane and nothing distinguished them, so the
    age is shown once a reading goes stale rather than implying it is live.
    """

    if candidate.agent_kind != "claude" or candidate.source == "untracked":
        return "—"
    if candidate.context_status == "stalled":
        return "失败!"
    if candidate.context_status == "limit_waiting":
        return "等待"
    if candidate.context_status == "compacting":
        value = candidate.compaction_percent
        return f"压缩{value}%" if value is not None else "压缩中"
    if candidate.context_percent is None:
        return "?"
    suffix = "!" if candidate.context_percent >= 80 else "%"
    label = f"{candidate.context_percent}{suffix}"
    age = candidate.context_age_sec
    if age is not None and age >= CONTEXT_STALE_SEC:
        return fit_suffix(label, f"~{compact_duration(age)}", CONTEXT_COLUMN_WIDTH)
    return label


def send_label(candidate: Candidate) -> str:
    """Episode send count for 任务请继续; kept after idle until the next error."""
    if candidate.source == "untracked":
        return "—"
    if candidate.send_count <= 0:
        return "—"
    return str(candidate.send_count)


def display_reason(candidate: Candidate) -> str:
    return error_label(candidate)


def display_attempts(candidate: Candidate) -> str:
    return send_label(candidate)


def next_filter(current: str) -> str:
    try:
        index = FILTERS.index(current)
    except ValueError:
        return FILTERS[0]
    return FILTERS[(index + 1) % len(FILTERS)]


def display_width(text: str) -> int:
    """Terminal columns a string occupies.

    Wide/Fullwidth (CJK) count as two.  East Asian "Ambiguous" characters —
    ``● ○ ▶ ═ ─ ·`` and the box-drawing set — count as **one**, matching how
    this Mac's terminal and cmux render them.  Because that is a judgement call
    rather than a fact, Ambiguous characters are barred from any column that
    goes through :func:`pad`; separators and rules use ASCII instead so a
    mis-guess can never shift a column or overflow a line.
    """
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def clip_to_width(text: str, width: int) -> str:
    """Cut a string to fit `width` terminal columns without splitting a glyph."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = display_width(char)
        if used + char_width > width:
            break
        kept.append(char)
        used += char_width
    return "".join(kept)


def pad(text: str, width: int, align: str = "<") -> str:
    """Pad or truncate to a terminal column count, never splitting a wide glyph.

    ``str.ljust`` and f-string padding count code points, so "运行中" (3 points,
    6 columns) misaligns every following column by a different amount.
    """
    if width <= 0:
        return text
    text = clip_to_width(text, width)
    filler = " " * (width - display_width(text))
    return filler + text if align == ">" else text + filler


def rule(char: str, width: int) -> str:
    """A full-width horizontal rule.  ASCII only, so it can never double up."""
    return char * max(0, width)


# ---------------------------------------------------------------------------
# Session identity
#
# The session column exists so an operator can copy a resume command straight
# off the row.  That single purpose sets every rule below: a *partial* UUID is
# not a cosmetic problem, it is wrong data that looks right, so the cell is
# all-or-nothing.  Everything here is derived from live processes and held in
# memory only -- no session id is ever written to state, logs or metrics.
# ---------------------------------------------------------------------------

TITLE_COL_WIDTH = 12
SESSION_COL_WIDTH = 36
# Shown instead of the UUID when the terminal cannot fit the whole cell.  It
# must never be confusable with an id, so it carries no hex at all.
SESSION_NARROW_MARKER = "窗口过窄"
SESSION_UNMEASURED = "未测量"
SESSION_GROUP_CELL = "—"

# Strict: exactly 36 characters, anchored.  A loose search would happily accept
# the leading 36 characters of a longer hex blob.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
# Codex rollout files are ``rollout-<ISO-with-dashes>-<uuid>.jsonl``.  The
# timestamp contains dashes too, so the id is anchored to the ``.jsonl`` tail
# rather than found by scanning.
_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl\Z"
)
_SESSION_ID_FLAGS = ("--session-id",)
_RESUME_FLAGS = ("--resume", "-r", "resume")

GROK_SESSIONS_PATH = Path.home() / ".grok" / "active_sessions.json"
LSOF_BIN = Path("/usr/sbin/lsof")
PS_BIN = Path("/bin/ps")
SESSION_REFRESH_TTL_SEC = 8.0
SESSION_PS_TIMEOUT_SEC = 6.0
SESSION_LSOF_TIMEOUT_SEC = 6.0

RESUME_COMMANDS = {
    "codex": "codex resume {sid}",
    "claude": "Claude --resume {sid}",
    "grok": "grok --resume {sid}",
}


def is_session_uuid(value: Any) -> bool:
    """True only for a complete, well-formed UUID."""
    return bool(_UUID_RE.match(str(value or "")))


def session_generation(pid: int, started_at: str) -> str:
    """The watcher's process-generation fingerprint, reproduced exactly.

    Mirrors ``cmux_codex_watch.inspect_claude_process``: the hash input is
    ``"<pid>:<started_at>"`` with an empty start time collapsing to
    ``unknown``.  Drifting from that formula by even a separator would make
    every persisted generation mismatch, which fails closed and would quietly
    turn the whole Claude tier into 未测量 while looking correct.
    """

    if pid <= 0:
        return ""
    return core._short_hash(f"{pid}:{started_at or 'unknown'}")


def _generation_is_unmeasured(generation: str, pid: int) -> bool:
    """True for the watcher's *unhashed* ``"<pid>:unknown"`` sentinel.

    That value is written when the watcher's own ``ps`` failed, so it records
    "not measured" rather than an identity.  Comparing it against a hash could
    never match; treating it as an identity would be worse.  Either way the
    honest answer is 未测量.
    """

    return generation == f"{pid}:unknown"


@dataclass
class SessionResult:
    """One surface's resolved session identity.

    Only validated, structured data lives here.  Raw argv, ``lsof`` output and
    filesystem paths are parsed and dropped before this record is built.
    """

    status: str = "unknown"          # ok | unknown | conflict | malformed | group
    session_id: str | None = None
    reason: str = ""
    tier: str = ""
    agent_kind: str = ""
    measured_at: float = 0.0
    pid: int = 0
    generation: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and is_session_uuid(self.session_id)

    def resume_command(self) -> str:
        """Copy-pasteable resume command, or "" when there is nothing valid."""
        if not self.ok:
            return ""
        template = RESUME_COMMANDS.get(self.agent_kind, "")
        return template.format(sid=self.session_id) if template else ""


def parse_ps_table(text: str) -> dict[int, dict[str, str]]:
    """Parse ``ps -axww -o pid=,ppid=,lstart=,command=`` into per-PID facts.

    ``lstart`` is five whitespace-separated tokens (``Sat Aug 29 20:13:33
    2026``), so the command begins at field eight.  The command text is used
    for parsing by the caller and must not be retained afterwards.
    """

    table: dict[int, dict[str, str]] = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        raw_started = " ".join(parts[2:7])
        started_at = ""
        try:
            started_at = dt.datetime.strptime(
                raw_started, "%a %b %d %H:%M:%S %Y"
            ).isoformat()
        except ValueError:
            # Same fallback the watcher uses: keep the raw field so the
            # generation stays stable even when the locale surprises us.
            started_at = raw_started
        table[pid] = {"ppid": str(ppid), "started_at": started_at,
                      "command": parts[7]}
    return table


def _coerce_pids(value: Any) -> list[int]:
    """PIDs from an untrusted ``agent_pids`` field, dropping anything unusable.

    ``cmux top`` output reaches here after several hops, and this runs on the
    worker thread where an exception is not merely a bad row -- it kills the
    whole pass, leaving *every* surface at 未测量 with no visible cause.  A
    non-list, a non-numeric string or a bool is therefore skipped rather than
    coerced: ``int(True)`` is 1, which is a real PID belonging to launchd.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return []
    pids: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            pid = int(item)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def _split_argv(command: str) -> list[str]:
    """Whitespace tokens of a command line.

    Deliberately not ``shlex``: an unbalanced quote in a pane title would raise
    and take out the whole refresh.  Session flags never contain spaces.
    """
    return [token for token in (command or "").split() if token]


def _ids_for_flags(tokens: list[str], flags: tuple[str, ...]) -> list[str]:
    """Every valid id introduced by one of ``flags``, in order.

    Only flag-adjacent values count.  A bare UUID sitting elsewhere in the
    command line is never evidence -- that was the whole point of tiering.
    """

    found: list[str] = []
    for index, token in enumerate(tokens):
        for flag in flags:
            if token == flag:
                if index + 1 < len(tokens) and is_session_uuid(tokens[index + 1]):
                    found.append(tokens[index + 1].lower())
                break
            prefix = f"{flag}="
            if token.startswith(prefix):
                value = token[len(prefix):]
                if is_session_uuid(value):
                    found.append(value.lower())
                break
    return found


def resolve_from_argv(commands: list[str]) -> tuple[str, str | None, str]:
    """Evaluate both argv tiers across every PID of one surface.

    Returns ``(status, session_id, reason)``.  Precedence is explicit
    ``--session-id`` first, then flag-adjacent resume forms.  A conflict is
    judged *within* the winning tier only: two PIDs agreeing on one id is a
    parent/child pair, not a disagreement.
    """

    tokens_per_pid = [_split_argv(command) for command in commands]
    for tier_name, flags in (("session-id", _SESSION_ID_FLAGS),
                             ("resume", _RESUME_FLAGS)):
        found: list[str] = []
        for tokens in tokens_per_pid:
            found.extend(_ids_for_flags(tokens, flags))
        distinct = sorted(set(found))
        if len(distinct) == 1:
            return "ok", distinct[0], tier_name
        if len(distinct) > 1:
            return "conflict", None, f"{tier_name} 层出现 {len(distinct)} 个不同 ID"
    return "unknown", None, ""


def codex_session_from_lsof(
    pid: int,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[str | None, str]:
    """Codex's open rollout file names its session.  Parse it and discard.

    ``lsof`` also lists every other descriptor the process holds.  Only the
    captured UUID leaves this function: the path, and every unrelated line, is
    dropped here rather than stored, logged or returned.
    """

    # No pre-flight existence check: a missing binary raises OSError from the
    # runner below and lands in the same "say why" branch.  Checking first would
    # also have meant a second, unfakeable filesystem call on the worker path.
    try:
        result = runner(
            [str(LSOF_BIN), "-p", str(pid)],
            capture_output=True, text=True,
            timeout=SESSION_LSOF_TIMEOUT_SEC, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "lsof 超时或无法运行"
    found: set[str] = set()
    for line in (getattr(result, "stdout", "") or "").splitlines():
        candidate = line.rsplit(None, 1)[-1] if line.split() else ""
        match = _ROLLOUT_RE.search(candidate)
        if match:
            found.add(match.group(1).lower())
    if len(found) == 1:
        return found.pop(), ""
    if len(found) > 1:
        return None, f"lsof 发现 {len(found)} 个不同 rollout"
    return None, "未找到 rollout 文件"


def grok_session_for_pid(
    pid: int,
    path: Path | None = None,
    loader: Callable[..., Any] | None = None,
) -> tuple[str | None, str]:
    """Grok records its live sessions keyed by PID.  Join exactly, never fuzzily.

    Two entries claiming the same PID is a genuine ambiguity and is reported as
    a conflict rather than resolved by picking one.
    """

    target = GROK_SESSIONS_PATH if path is None else path
    read = core.load_json if loader is None else loader
    # ``core.load_json`` returns the default only for FileNotFoundError; every
    # other failure -- a truncated file, bad encoding, EACCES, a directory where
    # a file was expected -- is re-raised as RuntimeError (watch:1648-1655).
    # Unguarded, that exception leaves this function on the *worker thread*,
    # aborts the whole pass, and leaves every surface at 未测量 with no visible
    # cause.  One unreadable file must cost one surface, not the table.  The
    # exception type is named in the reason because "读取失败" alone gives the
    # reader nothing to act on.
    try:
        document = read(target, [])
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        return None, f"active_sessions.json 读取失败（{type(exc).__name__}）"
    if not isinstance(document, list):
        return None, "active_sessions.json 结构异常"
    matches = {
        str(entry.get("session_id") or "").lower()
        for entry in document
        if isinstance(entry, Mapping)
        and str(entry.get("pid") or "").strip() == str(pid)
        and is_session_uuid(entry.get("session_id"))
    }
    if len(matches) == 1:
        return matches.pop(), ""
    if len(matches) > 1:
        return None, f"active_sessions.json 中 PID {pid} 有 {len(matches)} 个不同 ID"
    return None, "active_sessions.json 无此 PID"


def claude_session_from_runtime(
    runtime: Mapping[str, Any],
    agent_pids: list[int],
    ps_table: Mapping[int, Mapping[str, str]],
) -> tuple[str | None, str]:
    """Persisted Claude id, admitted only when PID *and* generation still match.

    The watcher clears this field when a process generation changes, but that
    clearing lives behind ``if not enabled or paused: continue``, so a paused or
    excluded surface can hold a frozen id indefinitely with no mechanism to
    correct it.  PIDs are also reused.  Presence alone, and even liveness
    alone, therefore proves nothing -- both the PID and its start-derived
    generation must still agree.
    """

    session_id = str(runtime.get("claude_session_id") or "")
    if not is_session_uuid(session_id):
        return None, "运行态无持久化 session"
    try:
        pid = int(runtime.get("claude_process_pid") or 0)
    except (TypeError, ValueError):
        return None, "运行态 PID 无法解析"
    if pid <= 0 or pid not in set(agent_pids):
        return None, f"持久化 PID {pid} 不在当前进程集合中"
    stored = str(runtime.get("claude_process_generation") or "")
    if not stored:
        return None, "运行态无进程代际"
    if _generation_is_unmeasured(stored, pid):
        return None, "运行态代际为未测量哨兵"
    entry = ps_table.get(pid)
    if not entry or not str(entry.get("started_at") or ""):
        # "We could not measure" and "the process was replaced" are different
        # facts and only one of them is evidence.  When our own ``ps`` returned
        # nothing for this PID -- it timed out, failed, or the row was
        # unparseable -- claiming replacement would invent an observation we
        # never made.  Both still fail closed; only the reason differs.
        return None, "无法读取该 PID 的启动时间"
    fresh = session_generation(pid, str(entry.get("started_at") or ""))
    if not fresh:
        return None, "无法计算进程代际"
    if fresh != stored:
        return None, "进程代际不匹配（进程已被替换）"
    return session_id.lower(), ""


def resolve_surface_session(
    agent_kind: str,
    agent_pids: list[int],
    ps_table: Mapping[int, Mapping[str, str]],
    runtime: Mapping[str, Any],
    *,
    now: float,
    lsof_runner: Callable[..., Any] = subprocess.run,
    grok_path: Path | None = None,
    grok_loader: Callable[..., Any] | None = None,
) -> SessionResult:
    """Resolve one surface, argv tiers first and bounded fallbacks last.

    Every PID of the surface is inspected: a single agent is often a small
    process tree, so looking at one PID would miss the flag and looking at
    several must not be mistaken for disagreement.
    """

    base = SessionResult(agent_kind=agent_kind, measured_at=now)
    if agent_kind not in RESUME_COMMANDS:
        base.status = "unknown"
        base.reason = "该 surface 没有可识别的 agent"
        return base
    live = [pid for pid in agent_pids if pid > 0]
    if not live:
        base.status = "unknown"
        base.reason = "没有可用的进程 PID"
        return base

    commands = [str((ps_table.get(pid) or {}).get("command") or "") for pid in live]
    status, session_id, note = resolve_from_argv(commands)
    if status == "ok":
        base.status, base.session_id, base.tier = "ok", session_id, note
        base.pid = live[0]
        return base
    if status == "conflict":
        base.status, base.reason, base.tier = "conflict", note, "argv"
        return base

    # Fallbacks are per-agent, read-only and bounded.  They run only because
    # argv carried nothing; they never override an explicit flag.
    #
    # Every PID is probed and the answers are compared *before* one is accepted.
    # Returning on the first hit would have silently picked whichever PID ``ps``
    # happened to list first when two disagreed -- exactly the arbitrary
    # selection the design forbids -- so the loop collects, then judges once.
    reasons: list[str] = []
    hits: dict[str, int] = {}
    for pid in live:
        if agent_kind == "codex":
            found, why = codex_session_from_lsof(pid, runner=lsof_runner)
        elif agent_kind == "grok":
            found, why = grok_session_for_pid(pid, path=grok_path,
                                              loader=grok_loader)
        else:
            found, why = claude_session_from_runtime(runtime, live, ps_table)
        if found:
            hits.setdefault(found, pid)
        elif why:
            reasons.append(why)
        if agent_kind == "claude":
            # The Claude tier is keyed on the surface's runtime record, not on
            # an individual PID, so retrying per PID would repeat one answer.
            break
    if len(hits) == 1:
        session_id, pid = next(iter(hits.items()))
        base.status, base.session_id = "ok", session_id
        base.tier, base.pid = f"{agent_kind}-fallback", pid
        entry = ps_table.get(pid) or {}
        base.generation = session_generation(pid, str(entry.get("started_at") or ""))
        return base
    if len(hits) > 1:
        base.status = "conflict"
        base.tier = f"{agent_kind}-fallback"
        base.reason = f"{agent_kind} 回退层出现 {len(hits)} 个不同 ID"
        return base
    base.status = "unknown"
    base.reason = reasons[0] if reasons else "未找到 session 证据"
    return base


class SessionResolver:
    """Resolves agent session ids on a worker thread.

    Same contract as :class:`JanitorClient`: ``snapshot()`` is what the draw
    path calls, it never blocks and never touches the filesystem or a
    subprocess.  All of the ``ps``/``lsof``/state reading happens here, off the
    draw path, and results are cached by ``(pid, generation)`` so a process
    replacement invalidates rather than inherits an identity.

    Nothing is persisted.  The cache is in-memory for the life of the panel.
    """

    def __init__(
        self,
        *,
        ttl_sec: float = SESSION_REFRESH_TTL_SEC,
        ps_runner: Callable[..., Any] = subprocess.run,
        lsof_runner: Callable[..., Any] = subprocess.run,
        grok_path: Path | None = None,
        grok_loader: Callable[..., Any] | None = None,
    ) -> None:
        self.ttl_sec = ttl_sec
        self._ps_runner = ps_runner
        self._lsof_runner = lsof_runner
        self._grok_path = grok_path
        self._grok_loader = grok_loader
        self._lock = threading.Lock()
        self._results: dict[str, SessionResult] = {}
        self._fetched_at = 0.0
        self._worker: threading.Thread | None = None
        self._generation = 0
        self.ps_calls = 0

    # ---- draw-path side: must never block ----

    def snapshot(self) -> dict[str, SessionResult]:
        with self._lock:
            return dict(self._results)

    def result_for(self, surface_id: str) -> SessionResult:
        with self._lock:
            return self._results.get(surface_id) or SessionResult(
                status="unknown", reason="尚未测量",
            )

    # ---- worker side ----

    def maybe_refresh(
        self,
        surfaces: list[Mapping[str, Any]],
        runtimes: Mapping[str, Mapping[str, Any]],
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        """Start one resolution pass if the cache is due.  Never joins."""

        moment = time.time() if now is None else now
        with self._lock:
            running = self._worker is not None and self._worker.is_alive()
            due = force or (moment - self._fetched_at) >= self.ttl_sec
            if running or not due:
                return False
            self._generation += 1
            token = self._generation
            payload = [dict(item) for item in surfaces]
            # ``state.json`` is external input.  A value that is not a mapping
            # would make ``dict(value)`` raise here, on the caller's thread,
            # taking down the whole refresh for one malformed record.  Skip the
            # bad entry instead: that surface resolves to 未测量 and the rest of
            # the table still gets ids.
            runtime_copy = {
                key: dict(value)
                for key, value in runtimes.items()
                if isinstance(value, Mapping)
            }
            worker = threading.Thread(
                target=self._resolve_once, args=(payload, runtime_copy, token),
                name="session-resolver", daemon=True,
            )
            self._worker = worker
        worker.start()
        return True

    def wait_for_refresh(self, timeout: float = 30.0) -> bool:
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def read_ps_table(self) -> dict[int, dict[str, str]]:
        """Exactly one full-table ``ps`` per pass.

        ``lstart`` is included so the process generation can be derived here
        rather than by calling the watcher's per-PID helper, which would spawn
        one ``ps`` for every Claude surface and break the single-call budget.
        """

        self.ps_calls += 1
        try:
            result = self._ps_runner(
                [str(PS_BIN), "-axww", "-o", "pid=,ppid=,lstart=,command="],
                capture_output=True, text=True,
                timeout=SESSION_PS_TIMEOUT_SEC, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        return parse_ps_table(getattr(result, "stdout", "") or "")

    def _resolve_once(
        self,
        surfaces: list[Mapping[str, Any]],
        runtimes: Mapping[str, Mapping[str, Any]],
        token: int,
    ) -> None:
        ps_table = self.read_ps_table()
        now = time.time()
        resolved: dict[str, SessionResult] = {}
        for item in surfaces:
            surface_id = str(item.get("surface_id") or "")
            if not surface_id:
                continue
            pids = _coerce_pids(item.get("agent_pids"))
            resolved[surface_id] = resolve_surface_session(
                str(item.get("agent_kind") or ""), pids, ps_table,
                runtimes.get(surface_id) or {}, now=now,
                lsof_runner=self._lsof_runner,
                grok_path=self._grok_path, grok_loader=self._grok_loader,
            )
        with self._lock:
            # A pass that started before a newer one is discarded rather than
            # written: late results would otherwise resurrect ids for processes
            # that have already been replaced.
            if token != self._generation:
                return
            self._results = resolved
            self._fetched_at = now


# ---------------------------------------------------------------------------
# 协作 column: read-only projection of multi-agent collaboration markers.
#
# Source of truth is the multi-agent-collaboration harness.  The v2 contract
# (2026-09-01) writes one marker per collaboration at
# /tmp/multi-agent-collaboration/_active/<WS_UUID>/<COLLABORATION_ID>.json so
# concurrent collaborations in one workspace no longer overwrite each other;
# the v1 single file _active/<WS_UUID>.json stays read-compatible (published
# contract: skill schemas/active-marker.schema.json).  This panel only *reads*
# those markers -- it never writes them, and collaboration state never feeds
# any send/continuation decision.  Same philosophy as the janitor row: consume
# another program's published verdict, never re-derive it.
#
# Join discipline: participants are matched by surface UUID only.  Markers also
# carry surface refs, but refs renumber whenever surfaces open or close (the
# 2026-08-20 tree/top ref-join incident), so a participant without a UUID shows
# nothing rather than guessing from the ref.
# ---------------------------------------------------------------------------

COLLAB_ACTIVE_DIR = Path("/tmp/multi-agent-collaboration/_active")
# v1: single file per workspace, numbered executor role enums, heartbeat may
#     fall back to armed_at (bounded by TTL + idle window below).
# v2: per-collaboration files, role+ordinal participants, strict mandatory
#     heartbeat -- a missing/malformed/pre-armed last_activity_at hides the
#     whole marker instead of falling back.
COLLAB_MARKER_VERSIONS = (1, 2)
# 1 connector glyph + "Supervisor", the widest role label at 10 ASCII columns.
COLLAB_COL_WIDTH = 11
# A marker whose task stopped producing artifacts is history, not liveness:
# armed_at + 6h TTL alone would keep a dead collaboration glowing for hours.
COLLAB_IDLE_SEC = 3600.0
COLLAB_CACHE_TTL_SEC = 2.0


@dataclass(frozen=True)
class CollabRole:
    """One surface's part in an active collaboration, keyed by surface UUID."""

    task_id: str
    label: str            # Supervisor / Executor / Executor1 / Executor2 ...
    role: str
    provider: str
    workspace_uuid: str
    collab_id: str        # groups participants; distinct collaborations never link
    peer_text: str        # the other participants, for the focus line
    armed_age_sec: float
    activity_age_sec: float


def _collab_parse_ts(value: Any) -> float | None:
    """ISO-8601 string -> epoch seconds, None when unparseable."""
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def read_collab_markers(directory: Path = COLLAB_ACTIVE_DIR) -> list[dict[str, Any]]:
    """All parseable marker dicts.  Unreadable files are skipped, not raised:
    a broken marker must degrade to "no collaboration shown", never to a dead
    panel.  v2 per-collaboration files live one level down in per-workspace
    directories; v1 single files sit directly in the root."""
    markers: list[dict[str, Any]] = []
    try:
        entries = sorted(directory.glob("*.json")) + sorted(directory.glob("*/*.json"))
    except OSError:
        return markers
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            value = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            markers.append(value)
    return markers


def _collab_participants_v1(
    participants: list[Any],
) -> list[tuple[str, str, Mapping[str, Any]]] | None:
    """(uuid, label, item) triples under the v1 rules: numbered role enums,
    executors labelled in array order, unknown roles skipped per-participant."""
    executor_total = sum(
        1 for item in participants
        if isinstance(item, Mapping) and str(item.get("role", "")).startswith("executor")
    )
    labelled: list[tuple[str, str, Mapping[str, Any]]] = []
    executor_seen = 0
    for item in participants:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        uuid = str(item.get("surface_uuid") or "")
        if role == "supervisor":
            label = "Supervisor"
        elif role.startswith("executor"):
            executor_seen += 1
            label = "Executor" if executor_total <= 1 else f"Executor{executor_seen}"
        else:
            continue
        labelled.append((uuid, label, item))
    return labelled


def _collab_participants_v2(
    participants: list[Any],
) -> list[tuple[str, str, Mapping[str, Any]]] | None:
    """(uuid, label, item) triples under the strict v2 rules, or None to
    reject the whole marker.

    v2 markers are validated as a unit: exactly one supervisor at ordinal 0,
    at least one executor at unique ordinals >= 1, roles only from the schema
    enum, and no duplicate surface UUID.  A marker that breaks any of these
    cannot prove who is collaborating with whom, so it contributes nothing --
    per-participant salvage could label the wrong surface.  Labels derive from
    ordinal, never from array position, so a re-serialised marker cannot
    silently renumber executors.
    """
    supervisors: list[tuple[int, Mapping[str, Any]]] = []
    executors: list[tuple[int, Mapping[str, Any]]] = []
    for item in participants:
        if not isinstance(item, Mapping):
            return None
        role = str(item.get("role") or "")
        ordinal = item.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            return None
        if role == "supervisor":
            if ordinal != 0:
                return None
            supervisors.append((ordinal, item))
        elif role == "executor":
            if ordinal < 1:
                return None
            executors.append((ordinal, item))
        else:
            return None
    if len(supervisors) != 1 or not executors:
        return None
    ordinals = [ordinal for ordinal, _ in executors]
    if len(ordinals) != len(set(ordinals)):
        return None
    uuids = [str(item.get("surface_uuid") or "")
             for _, item in supervisors + executors]
    non_empty = [u for u in uuids if u]
    if len(non_empty) != len(set(non_empty)):
        return None
    labelled = [(str(supervisors[0][1].get("surface_uuid") or ""),
                 "Supervisor", supervisors[0][1])]
    executors.sort(key=lambda pair: pair[0])
    for ordinal, item in executors:
        label = "Executor" if len(executors) == 1 else f"Executor{ordinal}"
        labelled.append((str(item.get("surface_uuid") or ""), label, item))
    return labelled


def collab_roles(
    markers: Iterable[Mapping[str, Any]], now: float | None = None,
) -> dict[str, CollabRole]:
    """surface UUID -> CollabRole for every *fresh* marker participant.

    Fresh means all of: understood marker_version, armed_at within its own
    ttl_seconds, and the last heartbeat younger than COLLAB_IDLE_SEC.  For v2
    markers the heartbeat is mandatory and strict: a missing, malformed or
    pre-armed last_activity_at hides the whole marker (v1 keeps its bounded
    fallback to armed_at for the migration window).  Anything else -- unknown
    contract, expired, stalled, missing timestamps, invariant-breaking
    participants -- contributes nothing.  Evidence of a past collaboration is
    deliberately invisible.
    """
    now = time.time() if now is None else now
    roles: dict[str, CollabRole] = {}
    for marker in markers:
        version = marker.get("marker_version")
        if version not in COLLAB_MARKER_VERSIONS:
            continue
        armed_at = _collab_parse_ts(marker.get("armed_at"))
        if armed_at is None:
            continue
        try:
            ttl = float(marker.get("ttl_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if ttl <= 0 or now >= armed_at + ttl:
            continue
        activity_at = _collab_parse_ts(marker.get("last_activity_at"))
        if version >= 2:
            # Strict: no fallback.  arm_task writes both stamps in the same
            # call, so a heartbeat earlier than armed_at is corruption, not a
            # clock quirk, and a corrupt liveness signal must hide the marker.
            if activity_at is None or activity_at < armed_at:
                continue
        elif activity_at is None:
            activity_at = armed_at
        activity_age = max(0.0, now - max(activity_at, armed_at))
        if activity_age > COLLAB_IDLE_SEC:
            continue
        participants = marker.get("participants")
        if not isinstance(participants, list):
            continue
        if version >= 2:
            labelled = _collab_participants_v2(participants)
        else:
            labelled = _collab_participants_v1(participants)
        if not labelled:
            continue
        task_id = str(marker.get("task_id") or "")
        workspace_uuid = str(marker.get("workspace_uuid") or marker.get("workspace_id") or "")
        collab_id = str(marker.get("collaboration_id") or "") or f"task:{task_id}"
        for uuid, label, item in labelled:
            if not uuid:
                # No UUID means no provable identity; a ref-based guess could
                # label a stranger's surface after a renumber.
                continue
            if uuid in roles:
                # Two fresh markers naming the same surface cannot both be
                # right; keep the first (deterministic read order) rather than
                # letting the last writer relabel the row every refresh.
                continue
            peers = ", ".join(
                f"{peer_label}={str(peer.get('surface_ref') or peer.get('surface_uuid') or '?')}"
                for peer_uuid, peer_label, peer in labelled
                if peer_uuid != uuid or peer_label != label
            )
            roles[uuid] = CollabRole(
                task_id=task_id,
                label=label,
                role=str(item.get("role") or ""),
                provider=str(item.get("provider") or ""),
                workspace_uuid=workspace_uuid,
                collab_id=collab_id,
                peer_text=peers,
                armed_age_sec=max(0.0, now - armed_at),
                activity_age_sec=activity_age,
            )
    return roles


def collab_cells_for_rows(
    rows: list["ViewRow"], roles: Mapping[str, CollabRole],
) -> dict[str, str]:
    """row.key -> rendered 协作 cell (connector glyph + role label).

    Participants of ONE collaboration are joined with box-drawing glyphs:
    first ``╭``, middles ``├``, last ``╰``.  Non-member rows stay blank -- a
    glyph on a bystander's row reads as participation, and with concurrent
    collaborations a shared pass-through line could not say whose it is; the
    role labels plus the focus line carry the linkage across any visual gap.
    Distinct collaborations in the same workspace are segmented independently
    (their connectors never merge), segments never cross a workspace header,
    and a lone visible participant keeps its label but gets no connector --
    half a line would imply a peer that is not on screen.
    """
    cells: dict[str, str] = {}

    def flush(segment: list["ViewRow"]) -> None:
        member_roles: dict[str, CollabRole] = {}
        by_collab: dict[str, list[int]] = {}
        for i, row in enumerate(segment):
            candidate = row.candidate
            if candidate is None:
                continue
            role = roles.get(candidate.surface_id)
            if role is None:
                continue
            if role.workspace_uuid and str(row.workspace_id) != role.workspace_uuid:
                # A marker naming a surface now shown under another workspace is
                # stale evidence, not a collaboration.
                continue
            member_roles[row.key] = role
            by_collab.setdefault(role.collab_id, []).append(i)
        for indexes in by_collab.values():
            connected = len(indexes) >= 2
            first, last = indexes[0], indexes[-1]
            for i in indexes:
                row = segment[i]
                if not connected:
                    glyph = " "
                elif i == first:
                    glyph = "╭"
                elif i == last:
                    glyph = "╰"
                else:
                    glyph = "├"
                cells[row.key] = f"{glyph}{member_roles[row.key].label}"
    segment: list["ViewRow"] = []
    for row in rows:
        if row.kind == "group":
            flush(segment)
            segment = []
        else:
            segment.append(row)
    flush(segment)
    return cells


def collab_group_counts(roles: Mapping[str, CollabRole]) -> dict[str, int]:
    """workspace UUID -> number of distinct fresh collaborations.

    Only markers that survived collab_roles' freshness and invariant checks
    are counted, so history and broken markers never inflate the header.
    """
    seen: dict[str, set[str]] = {}
    for role in roles.values():
        if role.workspace_uuid:
            seen.setdefault(role.workspace_uuid, set()).add(role.collab_id)
    return {ws: len(ids) for ws, ids in seen.items()}


def collab_column_fits(width: int | None) -> bool:
    """Frame-level decision, computed from the same spec that draws the row."""
    if width is None:
        return False
    body = sum(col_width for _, col_width, _ in ROW_COLUMNS) + (len(ROW_COLUMNS) - 1)
    minimum = 5 + body + 1 + COLLAB_COL_WIDTH + 2 + TITLE_COL_WIDTH
    return width >= minimum


def collab_focus_note(candidate: Candidate | None, roles: Mapping[str, CollabRole]) -> str:
    """Focus-line detail for a collaborating surface, empty otherwise."""
    if candidate is None:
        return ""
    role = roles.get(candidate.surface_id)
    if role is None:
        return ""
    parts = [f"协作 {role.label}"]
    if role.task_id:
        parts.append(f"task={role.task_id}")
    if role.peer_text:
        parts.append(f"对端 {role.peer_text}")
    parts.append(f"活跃 {compact_duration(role.activity_age_sec)}前")
    return "  ".join(parts)


class CollabClient:
    """TTL-cached reader of the active-marker directory.

    Unlike the janitor/stack clients this one has no worker thread: it reads a
    handful of sub-KB local JSON files, not a subprocess that can wedge.  The
    TTL only exists so key-repeat redraws do not re-parse the directory.
    """

    def __init__(
        self,
        directory: Path = COLLAB_ACTIVE_DIR,
        *,
        ttl_sec: float = COLLAB_CACHE_TTL_SEC,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._directory = directory
        self._ttl_sec = ttl_sec
        self._clock = clock
        self._fetched_at = 0.0
        self._roles: dict[str, CollabRole] = {}

    def maybe_refresh(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and self._roles is not None and now - self._fetched_at < self._ttl_sec:
            return
        self._fetched_at = now
        self._roles = collab_roles(read_collab_markers(self._directory), now=now)

    def snapshot(self) -> dict[str, CollabRole]:
        return dict(self._roles)


# One spec drives both the header and the rows so they cannot drift apart.
ROW_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("监控", 8, "<"),
    ("位置", 14, "<"),
    # 7 columns: the widest label is "Copilot"; 6 truncated it to "Copilo".
    ("程序", 7, "<"),
    ("Hook", 8, "<"),
    ("上下文", 10, "<"),
    ("画面", 8, "<"),
    ("错误", 8, "<"),
    ("续跑", 5, ">"),
)


def session_cell(text: str, available: int) -> str | None:
    """The session cell for a row, or ``None`` when it does not fit.

    All-or-nothing by construction.  A clipped UUID is not a cosmetic
    imperfection: ``9f7aa928-0525`` looks exactly like a valid id, copies
    cleanly, and resumes nothing.  So when the terminal cannot hold the whole
    fixed-width field the caller is told to render a marker instead -- never a
    prefix of the id.
    """

    if available < SESSION_COL_WIDTH:
        return None
    return pad(text, SESSION_COL_WIDTH)


def _row_text(
    prefix: str,
    cells: tuple[str, ...],
    title: str,
    width: int | None = None,
    session: str = "",
    collab: str | None = None,
) -> str:
    """One table row.

    ``width`` is the usable column count.  Passing ``None`` keeps the historical
    nine-column row, which is what callers that have no terminal to measure
    (and the older tests) rely on.

    ``collab`` is the 协作 cell.  ``None`` omits the column entirely -- the
    historical layout -- while any string (even "") reserves COLLAB_COL_WIDTH
    columns.  The caller decides once per frame via collab_column_fits(), so
    the header and every row always agree, and the session cell's own
    fits-or-marker arithmetic keeps working because it measures the head it is
    actually appended to.
    """

    body = " ".join(pad(value, width_, align) for value, (_, width_, align) in zip(cells, ROW_COLUMNS))
    if collab is not None:
        body = f"{body} {pad(collab, COLLAB_COL_WIDTH)}"
    head = f"{prefix}{body}  {pad(title, TITLE_COL_WIDTH)}"
    if width is None:
        return f"{prefix}{body}  {title}"
    cell = session_cell(session, width - display_width(head) - 1)
    return f"{head} {SESSION_NARROW_MARKER if cell is None else cell}".rstrip()


def header_text(width: int | None = None, collab: str | None = None) -> str:
    # Five leading spaces: cursor, suggested-marker, then the member indent that
    # nests surfaces under their workspace header.
    #
    # The header goes through the same code path as the data rows, so the
    # session label cannot appear on one and not the other.
    return _row_text(
        "     ", tuple(name for name, _, _ in ROW_COLUMNS), "标题",
        width, "session", collab,
    )


def _ref_digits(value: Any) -> str:
    _, _, suffix = str(value or "").partition(":")
    return suffix or "?"


def location_text(record: Mapping[str, Any], workspace_ref: str = "") -> str:
    """Compact ws/pane/surface position, e.g. ws11/p24/s59.

    The long form "workspace:11/pane:24/surface:59" is 31 columns and pushed the
    surface number — the part that identifies the row — off the end.

    ``workspace_ref`` overrides the record, which lets a row whose surface has
    vanished still show its real workspace instead of "ws?" — config-only
    fallback records never carried workspace_ref.
    """
    return "ws{}/p{}/s{}".format(
        _ref_digits(workspace_ref or record.get("workspace_ref")),
        _ref_digits(record.get("pane_ref")),
        _ref_digits(record.get("ref")),
    )


def target_name(record: Mapping[str, Any]) -> str:
    """Build the positional ws{N}-p{M}-s{K} name used for explicit targets.

    A surface's terminal title is whatever the agent is printing right now, so
    it makes a useless persistent name.  The position is stable enough to read
    at a glance and matches the convention the existing registrations use.
    """

    def number(value: Any) -> str:
        return _ref_digits(value)

    return "ws{}-p{}-s{}".format(
        number(record.get("workspace_ref")),
        number(record.get("pane_ref")),
        number(record.get("ref")),
    )


def workspace_rule_name(record: Mapping[str, Any]) -> str:
    """Workspace titles are stable and human-written, so prefer them here."""
    title = str(record.get("workspace_title") or "").strip()
    if title:
        return title
    _, _, suffix = str(record.get("workspace_ref") or "").partition(":")
    return f"ws{suffix}" if suffix else str(record.get("workspace_id") or "")[:8]


def filter_candidates(candidates: list[Candidate], view: str, query: str = "") -> list[Candidate]:
    # A pool member with no live Codex is still registered, so it belongs on the
    # watched side of this filter even though it is idling.
    if view == "watched":
        filtered = [item for item in candidates if item.source != "untracked"]
    elif view == "untracked":
        filtered = [item for item in candidates if item.source == "untracked"]
    else:
        filtered = list(candidates)
    needle = query.strip().lower()
    if not needle:
        return filtered
    return [item for item in filtered if needle in " ".join((
        item.workspace_ref, str(item.record.get("workspace_title") or ""),
        str(item.record.get("pane_ref") or ""), item.ref,
        str(item.record.get("title") or ""), item.agent_kind,
        item.process_summary,
        # What the screen actually prints: "ws11/p24/s59".  Without this you
        # cannot search for the thing you are looking at.
        location_text(item.record),
        *location_text(item.record).split("/"),
        # The session UUID, so pasting any prefix of an ID from a resume command
        # finds the row it belongs to.  ``in`` over the joined haystack already
        # matches every prefix and every interior fragment, so no separate
        # prefix pass is needed.  Only a validated ID is searchable: an
        # unmeasured row must not be findable by someone else's UUID.
        str(item.session.session_id or ""),
    )).lower()]


def focus_summary(candidate: Candidate | None, workspace_ref: str = "") -> str:
    """What the cursor is on: position, management state, screen, error, count."""
    if candidate is None:
        return "当前筛选没有行"
    title = str(candidate.record.get("workspace_title") or "").strip()
    facts = [location_text(candidate.record, workspace_ref)]
    if title:
        facts.append(clip_to_width(title, 30))
    facts.append(WATCH_LABELS.get(watch_kind(candidate), "未登记"))
    if candidate.agent_kind == "claude" or candidate.state.startswith("claude"):
        facts.append(f"Hook {hook_label(candidate)}")
    if candidate.source != "untracked":
        facts.append(state_label(candidate.state))
        if is_idling(candidate):
            # Registered but nothing to rescue: say so, or the row reads as if
            # it were being actively watched.
            program = candidate.process_summary or agent_label(candidate.agent_kind)
            facts.append(f"这一格当前没有 Codex 在跑（{program}），守护器不会发送")
        if candidate.state in DETAIL_FALLBACKS:
            # Spell the diagnostic out here; the column only had room for "读不到".
            facts.append(diagnostic_detail(candidate) or DETAIL_FALLBACKS[candidate.state])
        elif error_label(candidate) != "—":
            facts.append(f"错误 {error_label(candidate)}")
        if send_label(candidate) != "—":
            facts.append(f"本轮已续跑 {send_label(candidate)} 次")
        if candidate.repeat_warning:
            facts.append(f"连续中断 {candidate.consecutive_resumes} 次，仍在自动恢复")
    else:
        facts.append(candidate.process_summary or agent_label(candidate.agent_kind))
    # The session detail goes last because it is the longest, and because it is
    # the one fact the table itself may be too narrow to show.  On a narrow
    # terminal this line is the only place the complete UUID appears, so the
    # generated command is built here from the validated ID -- never from the
    # possibly-clipped cell.
    facts.append(session_detail(candidate))
    return "  |  ".join(facts)


def session_detail(candidate: Candidate) -> str:
    """The focus line's session fact: source, age, and the resume command."""

    result = candidate.session
    if not result.ok:
        # Say *why* it is unmeasured.  "未测量" with no reason was the thing
        # that made the old Hook column useless.  With no reason to add, the
        # bare label stands alone rather than restating itself in brackets.
        if not result.reason:
            return f"session {SESSION_UNMEASURED}"
        return f"session {SESSION_UNMEASURED}（{result.reason}）"
    parts = [f"session {result.session_id}"]
    if result.tier:
        parts.append(f"来源 {result.tier}")
    if result.measured_at > 0:
        # ``_age_text`` already ends in 前 ("3分钟前"), so appending 前测得 would
        # read "3分钟前前测得".
        parts.append(f"{_age_text(time.time() - result.measured_at)}测得")
    # Built from the record's own agent_kind, not the candidate's: those are two
    # separate reads of the process table, and a command generated from the
    # wrong one would resume the wrong conversation.
    command = result.resume_command()
    if command:
        parts.append(command)
    return "，".join(parts)


MONITORED_SOURCES = {"explicit", "workspace_rule", "workspace_excluded", "workspace_non_codex"}
POOL_SOURCES = {"workspace_rule", "workspace_excluded", "workspace_non_codex"}


@dataclass
class ViewRow:
    """One visible line: either a workspace header or one surface under it."""

    kind: str                        # "group" | "member"
    workspace_id: str
    workspace_ref: str
    workspace_title: str
    counts: dict[str, int]
    candidate: Candidate | None = None
    collapsed: bool = False

    @property
    def key(self) -> str:
        """Stable cursor anchor; an integer index points elsewhere after a refresh."""
        if self.candidate is not None:
            return f"s:{self.candidate.surface_id}"
        return f"w:{self.workspace_id}"


def group_counts(candidates: list[Candidate]) -> dict[str, int]:
    idling = sum(1 for item in candidates if is_idling(item))
    return {
        "watching": sum(1 for item in candidates
                        if item.source == "explicit" and not item.paused and not is_idling(item)),
        "idling": idling,
        "paused": sum(1 for item in candidates
                      if not is_idling(item)
                      and (item.paused or item.source == "workspace_excluded")),
        "pool": sum(1 for item in candidates if item.source in POOL_SOURCES),
        "untracked": sum(1 for item in candidates if item.source == "untracked"),
        "warnings": sum(1 for item in candidates if item.repeat_warning),
        "all": len(candidates),
    }


def default_collapsed(candidates: list[Candidate], suggested_surface: str = "") -> set[str]:
    """Fold away workspaces with nothing under watch; keep the rest open.

    A workspace holding the surface cmux suggested stays open even with nothing
    registered in it — otherwise ``--suggest-surface`` points at a row that has
    no visible line and the cursor silently lands somewhere else.
    """
    by_workspace: dict[str, list[Candidate]] = {}
    for item in candidates:
        by_workspace.setdefault(str(item.record.get("workspace_id") or ""), []).append(item)
    return {
        workspace_id
        for workspace_id, members in by_workspace.items()
        if not any(member.source in MONITORED_SOURCES for member in members)
        and not any(member.surface_id == suggested_surface for member in members)
    }


def group_identity(members: list[Candidate]) -> tuple[str, str]:
    """Pick a workspace ref/title from a member that still exists in the tree.

    A target whose surface is gone falls back to a config-only record, and
    config never persisted workspace_ref, so that record carries "?".  Reading
    the label off whichever member happens to sort first would rename the whole
    group to "ws?" because paused rows sort ahead of untracked ones.
    """
    ref = ""
    title = ""
    for item in members:
        candidate_ref = str(item.record.get("workspace_ref") or "")
        if not ref and candidate_ref and candidate_ref != "?":
            ref = candidate_ref
        for key in ("workspace_title", "title_snapshot", "workspace_name"):
            candidate_title = str(item.record.get(key) or "").strip()
            if candidate_title:
                title = candidate_title
                break
        if ref and title:
            break
    return ref or "?", title


def reconcile_collapsed(
    collapsed: set[str],
    manual: set[str],
    candidates: list[Candidate],
    suggested_surface: str = "",
) -> set[str]:
    """Realign fold state with workspaces that appeared or disappeared.

    ``default_collapsed`` used to run once at startup, so a workspace created
    later was absent from the set and rendered expanded even with nothing
    watched, while one that later gained a target stayed folded.  Workspaces you
    folded or unfolded by hand are listed in ``manual`` and are never
    overridden — otherwise the 5-second refresh would undo your own Tab.
    """
    present = {str(item.record.get("workspace_id") or "") for item in candidates}
    wanted = default_collapsed(candidates, suggested_surface)
    result = {workspace_id for workspace_id in collapsed if workspace_id in present}
    for workspace_id in present - manual:
        if workspace_id in wanted:
            result.add(workspace_id)
        else:
            result.discard(workspace_id)
    return result


def surface_ids(candidates: list[Candidate]) -> set[str]:
    return {item.surface_id for item in candidates}


def refresh_report(previous: set[str], current: set[str], workspaces: int) -> str:
    """What a manual refresh actually found, so R is not a silent no-op."""
    added = len(current - previous)
    gone = len(previous - current)
    parts = [f"已刷新：{workspaces} 个 workspace / {len(current)} 路"]
    if added:
        parts.append(f"新增 {added}")
    if gone:
        parts.append(f"消失 {gone}")
    if not added and not gone:
        parts.append("无变化")
    return "，".join(parts)


def build_view_rows(
    candidates: list[Candidate],
    collapsed: set[str],
    view: str,
    query: str = "",
) -> list[ViewRow]:
    """Turn a flat candidate list into workspace headers plus their members.

    Counts on the header come from every candidate in that workspace, not just
    the ones surviving the filter, so the header never understates a pool.  An
    active search overrides collapse: a hit you cannot see is worse than noise.
    """
    visible = filter_candidates(candidates, view, query)
    order: list[str] = []
    members: dict[str, list[Candidate]] = {}
    for item in visible:
        workspace_id = str(item.record.get("workspace_id") or "")
        if workspace_id not in members:
            members[workspace_id] = []
            order.append(workspace_id)
        members[workspace_id].append(item)
    everything: dict[str, list[Candidate]] = {}
    for item in candidates:
        everything.setdefault(str(item.record.get("workspace_id") or ""), []).append(item)

    searching = bool(query.strip())
    rows: list[ViewRow] = []
    for workspace_id in order:
        group = members[workspace_id]
        counts = group_counts(everything.get(workspace_id, group))
        folded = (not searching) and workspace_id in collapsed
        group_ref, group_title = group_identity(everything.get(workspace_id, group))
        rows.append(ViewRow(
            kind="group",
            workspace_id=workspace_id,
            workspace_ref=group_ref,
            workspace_title=group_title,
            counts=counts,
            collapsed=folded,
        ))
        if folded:
            continue
        # Watched rows first so they are not buried among the untracked ones.
        for item in sorted(group, key=lambda row: row.source == "untracked"):
            rows.append(ViewRow(
                kind="member",
                workspace_id=workspace_id,
                workspace_ref=group_ref,
                workspace_title=group_title,
                counts=counts,
                candidate=item,
            ))
    return rows


def initial_cursor_key(rows: list[ViewRow], suggested_surface: str = "") -> str:
    """Land on the surface cmux suggested, else the first watched row."""
    if suggested_surface:
        for row in rows:
            if row.candidate is not None and row.candidate.surface_id == suggested_surface:
                return row.key
    for row in rows:
        if row.candidate is not None and row.candidate.source in MONITORED_SOURCES:
            return row.key
    return rows[0].key if rows else ""


def index_for_key(rows: list[ViewRow], key: str, suggested_surface: str = "") -> int:
    for index, row in enumerate(rows):
        if row.key == key:
            return index
    fallback = initial_cursor_key(rows, suggested_surface)
    for index, row in enumerate(rows):
        if row.key == fallback:
            return index
    return 0


def group_row_text(row: ViewRow, width: int) -> str:
    marker = "+" if row.collapsed else "-"
    # Compact "ws9" matches the position column below it; the canonical
    # "workspace:9" stays in the confirm prompts where precision matters.
    left = f"{marker} ws{_ref_digits(row.workspace_ref)}"
    if row.workspace_title:
        left = f"{left}  {row.workspace_title}"
    bits = []
    if row.counts.get("pool"):
        bits.append("整池授权")
    watched = row.counts.get("watching", 0) + row.counts.get("pool", 0)
    if watched:
        bits.append(f"{watched} 监控")
    if row.counts.get("idling"):
        bits.append(f"{row.counts['idling']} 空转")
    if row.counts.get("paused"):
        bits.append(f"{row.counts['paused']} 已暂停")
    if row.counts.get("untracked"):
        bits.append(f"{row.counts['untracked']} 未登记")
    if row.counts.get("warnings"):
        bits.append(f"{row.counts['warnings']} 连续中断")
    if row.collapsed:
        # "+" alone reads as "nothing here" rather than "folded", which is why
        # a full pool looked undetected.  Say it in words.
        bits.append("已折叠，Tab 展开")
    right = "  |  ".join(bits)
    gap = max(2, width - display_width(left) - display_width(right))
    return f"{left}{' ' * gap}{right}"


def selected_action_hint(candidate: Candidate | None) -> str:
    """Only the keys that do something to the row under the cursor."""
    if candidate is None:
        return "/ 搜索  ·  f 切换筛选"
    if candidate.source == "untracked":
        return f"a 只加这一路   w 授权整个 {candidate.workspace_ref}（以后新开的 Codex 也会跟）"
    if candidate.source == "workspace_non_codex":
        return f"u 取消整个 {candidate.workspace_ref} 授权   （这一路发不发由守护器读屏决定）"
    if candidate.source == "explicit":
        if candidate.paused:
            if candidate.unreadable_sec > 0:
                span = compact_duration(candidate.unreadable_sec)
                # A paused target is skipped before its runtime is read, so the
                # blind clock cannot advance and no warning can fire: this row
                # genuinely needs a human, unlike an unpaused blind pane.
                return (
                    f"画面读不出且已暂停（已 {span}）：暂停中不再轮询，"
                    "需人工 r 恢复发送   x 删除这一路登记"
                )
            return "r 恢复发送   x 删除这一路登记"
        # The 错误 column is 8 columns wide, so a blind pane can only say
        # "看不清" there.  This line is free-form, so it carries the duration --
        # the number is the whole point: surface:72 sat unreadable for 5.65h
        # during the 2026-08-24 observation and nothing ever said how long.
        if candidate.deferred_sec > 0:
            span = compact_duration(candidate.deferred_sec)
            return (
                f"已收到停止事件但画面当时不安全，已挂起 {span}：画面一稳就自动续跑一次；"
                "不会重复发送，无需重新登记"
            )
        if candidate.unreadable_sec > 0:
            span = compact_duration(candidate.unreadable_sec)
            # Only an *unpaused* pane is still being polled.  Promising continued
            # monitoring for a paused one would be false: a paused target is
            # skipped before its runtime is even read.
            return (
                f"画面读不出（已 {span}）：不会据此发送，context 判定退回 unknown；"
                "仍持续监控，无需重新登记"
            )
        if candidate.context_status == "stalled":
            return (
                "上下文压缩失败，需人工：守护器不会自动 /compact 或 /clear；"
                "处理后自动恢复，无需重新登记"
            )
        if candidate.state == "claude_completed":
            return "已完成但仍持续监控；下个任务自动恢复判断，无需按 r   ·   p 暂停   x 删除"
        if candidate.hook_health == "legacy_override":
            span = compact_duration(candidate.unprotected_sec)
            suffix = f"（已 {span}）" if span else ""
            return f"旧会话未加载 CCC Hook{suffix}：在该格重启/恢复 Claude 一次；登记和监控不会丢"
        if candidate.hook_health == "missing":
            span = compact_duration(candidate.unprotected_sec)
            suffix = f"（已 {span}）" if span else ""
            return f"Hook 缺失{suffix}：保持监控但不会猜测发送；查看 ccc hook-audit 后重启/恢复该会话"
        if candidate.state == "claude_hook_gap_exhausted":
            return (
                "同一停止画面续跑已达上限：保持监控但不再重复发送；确认 Claude 状态后，"
                "新的 Hook、人工 prompt 或新内容会自动恢复"
            )
        if candidate.repeat_warning:
            return f"已连续中断 {candidate.consecutive_resumes} 次；仍在自动恢复   ·   p 暂停   x 删除"
        if is_idling(candidate):
            # 空转 rows read as "why is this still being monitored?".  The state
            # line above already names the program that *is* there; this line
            # says what you can do about it.  Wording avoids "Codex 已退出",
            # which is false for a pane that is running Claude and never ran
            # Codex.  Deliberately not offered for pool members: they have no
            # single-row registration, so `x` would be wrong.
            return "没有 Codex 可救：x 删掉这一路登记，或等 Codex 回来自动恢复   ·   p 暂停这一路"
        return "p 暂停这一路   x 删除这一路登记"
    if candidate.source == "workspace_rule":
        return f"p 只排除这一路   u 取消整个 {candidate.workspace_ref}"
    if candidate.source == "workspace_excluded":
        return f"r 重新纳入   u 取消整个 {candidate.workspace_ref}"
    return ""


def confirm_prompt(action: str, candidate: Candidate, *, live_codex: int | None = None) -> str:
    location = f"{candidate.workspace_ref}/{candidate.ref}"
    if action == "workspace":
        title = str(candidate.record.get("workspace_title") or "").strip()
        pool = f"{candidate.workspace_ref}{f'「{title}」' if title else ''}"
        count = f"这一池现在有 {live_codex} 个活 Codex。" if live_codex is not None else ""
        return f"确认授权整个 {pool}？{count}之后该池新开的 Codex 也会自动续跑"
    if action == "add":
        if candidate.agent_kind != "codex":
            kind = candidate.process_summary or agent_label(candidate.agent_kind)
            return f"{location} 看起来是 {kind}，不是 Codex。仍要登记这一路 UUID？"
        return f"确认只登记 {location}（{target_name(candidate.record)}）？只有这一路会自动续跑"
    if action == "untrack_workspace":
        return f"确认取消整个 {candidate.workspace_ref} 授权？该池将不再自动续跑"
    if action == "pause":
        if candidate.source in {"workspace_rule", "workspace_excluded"}:
            return f"确认只排除 {location}？不会取消整个 {candidate.workspace_ref}"
        return f"确认暂停 {location}？"
    if action == "remove":
        return f"确认删除 {location} 的单路登记？"
    if action == "arm":
        return "确认开始真实发送「任务请继续」？"
    if action == "stop-all":
        return "确认全局停发？已登记目标会保留"
    if action == "dry-run":
        return "确认改为只观察？全部已登记目标都不再自动续跑"
    return f"确认 {action} {location}？"


def workspace_confirm_prompt(row: ViewRow, action: str, *, live_codex: int | None = None) -> str:
    """Confirmation for a pool action taken on a workspace header.

    The wording follows the key that was pressed, never the current state: a
    prompt that describes the opposite of what you asked for is worse than no
    prompt at all.
    """
    pool = f"{row.workspace_ref}{f'「{clip_to_width(row.workspace_title, 20)}」' if row.workspace_title else ''}"
    if action == "untrack_workspace":
        return f"确认取消整个 {pool} 授权？该池将不再自动续跑"
    count = row.counts.get("all", 0) if live_codex is None else live_codex
    return f"确认授权整个 {pool}？这一池现在有 {count} 路，之后新开的 Codex 也会自动续跑"


def mode_label(config: Mapping[str, Any]) -> str:
    if config.get("global_paused"):
        return "全局已停发"
    if config.get("mode") == "armed":
        return "真实发送中"
    return "只观察不发送"


class SupervisorModel:
    def __init__(self, config_path: Path, suggested_surface: str = "", client: Any | None = None,
                 janitor: Any | None = None, sessions: Any | None = None,
                 stack: Any | None = None, collab: Any | None = None):
        self.config_path = config_path
        self.store = core.ConfigStore(config_path)
        self.client = client or core.CmuxClient()
        # The janitor panel's only data source.  Constructed here so the draw
        # path has nothing to build and nothing to wait for; see JanitorClient.
        self.janitor = janitor if janitor is not None else JanitorClient()
        # Session ids are resolved on the same off-draw-path contract as the
        # janitor status: the panel reads a snapshot, never the processes.
        self.sessions = sessions if sessions is not None else SessionResolver()
        # Same off-draw-path contract again, for the three-component control
        # plane.  Read-only: StackClient has no action method at all.
        self.stack = stack if stack is not None else StackClient()
        # Read-only projection of multi-agent collaboration markers.  Synchronous
        # by design: see CollabClient.
        self.collab = collab if collab is not None else CollabClient()
        self.suggested_surface = suggested_surface
        self.config: dict[str, Any] = {}
        self.runtime: dict[str, Any] = {}
        self.hook_config: dict[str, Any] = {}
        self.candidates: list[Candidate] = []
        self.error = ""
        self.last_top_refresh = 0.0
        self.online = False

    def refresh(self, *, force: bool = False) -> None:
        self.config = self.store.load()
        self.runtime = core.load_json(self.config_path.parent / "state.json", {})
        self.hook_config = core.ClaudeHookSettingsManager().inspect()
        # Kicks off a background ctl read when the cached snapshot is due; it
        # never joins the worker, so a slow or wedged ctl cannot stall a redraw.
        self.janitor.maybe_refresh(force=force)
        # Independently scheduled: a wedged cmux-stack must not delay the
        # janitor row, and vice versa.  Neither call ever joins its worker.
        self.stack.maybe_refresh(force=force)
        self.collab.maybe_refresh(force=force)
        now = time.monotonic()
        if not force and now - self.last_top_refresh < 5:
            return
        self.last_top_refresh = now
        try:
            tree = self.client.tree()
            top = self.client.top_all()
            candidates = core.main_surface_records(tree, allow_ref_only=True)
            process_by_id = core.classify_surface_processes(top)
            # One resolution pass per refresh, on a worker thread.  Started
            # before the rows are built so the ids that land are used by the
            # *next* redraw; the current one shows the previous snapshot rather
            # than waiting.  Every classified surface is offered, so untracked,
            # pool and paused rows are covered too.
            # ``core.surface_process_label`` is the only correct way to read this
            # table: it looks up by surface UUID *and then by ref*, because
            # ``classify_surface_processes`` keys entries by whichever identifier
            # the ``top`` payload carried.  A bare ``get(surface_id)`` silently
            # misses every ref-keyed surface -- the row still draws, so the loss
            # shows up only as a permanently 未测量 session column with no error.
            # The rest of this function already goes through the helper (below,
            # for the 程序 column); the resolver payload was the one place that
            # did not.
            session_payload: list[dict[str, Any]] = []
            for record in candidates:
                label = core.surface_process_label(process_by_id, record)
                session_payload.append({
                    "surface_id": str(record["surface_id"]),
                    "agent_kind": str(label.get("agent_kind") or ""),
                    "agent_pids": list(label.get("agent_pids") or []),
                })
            self.sessions.maybe_refresh(
                session_payload,
                self.runtime if isinstance(self.runtime, Mapping) else {},
                force=force,
            )
            session_by_id = self.sessions.snapshot()
            explicit_by_id = {
                str(item["surface_id"]): item
                for item in self.config.get("targets", [])
                if item.get("surface_id")
            }
            rules_by_workspace = {
                str(rule.get("workspace_id")): rule
                for rule in self.config.get("workspace_rules", [])
                if rule.get("enabled", True) and rule.get("workspace_id")
            }
            rows: list[Candidate] = []
            for record in candidates:
                surface_id = record["surface_id"]
                target = explicit_by_id.get(surface_id)
                rule = rules_by_workspace.get(str(record.get("workspace_id") or ""))
                process_info = core.surface_process_label(process_by_id, record)
                agent_kind = str(process_info.get("agent_kind") or "unknown")
                process_summary = str(process_info.get("summary") or "无进程信息")
                excluded = {
                    str(value)
                    for value in (rule or {}).get("excluded_surface_ids", [])
                }
                if target is not None:
                    source = "explicit"
                elif rule is not None and surface_id in excluded:
                    source = "workspace_excluded"
                    target = {"paused": True}
                elif rule is not None and agent_kind == "codex":
                    source = "workspace_rule"
                    target = {"paused": False}
                elif rule is not None:
                    source = "workspace_non_codex"
                    target = {"paused": True}
                else:
                    source = "untracked"
                runtime = self.runtime.get(surface_id, {}) if isinstance(self.runtime, Mapping) else {}
                exclusion = (rule or {}).get("excluded_surface_reasons", {}).get(surface_id, {})
                exclusion_reason = str(exclusion.get("reason") or "") if isinstance(exclusion, Mapping) else ""
                rows.append(Candidate(
                    record=record,
                    source=source,
                    state=("untracked" if source in {"untracked", "workspace_non_codex"} else str(runtime.get("state") or "unknown")),
                    error_type=str(runtime.get("error_type") or "-"),
                    send_count=int(runtime.get("send_count") or 0),
                    paused=bool(target and target.get("paused")),
                    selected_hint=surface_id in {self.suggested_surface, str(self.suggested_surface)},
                    status_detail=str(runtime.get("paused_reason") or (target or {}).get("paused_reason") or exclusion_reason),
                    agent_kind=agent_kind,
                    process_summary=process_summary,
                    hook_health=str(runtime.get("claude_hook_health") or ""),
                    consecutive_resumes=int(runtime.get("claude_consecutive_resumes") or 0),
                    repeat_warning=bool(runtime.get("claude_repeat_warning")),
                    hook_live_sends=int(runtime.get("claude_hook_live_send_count") or 0),
                    hook_sla_misses=int(runtime.get("claude_hook_sla_miss_count") or 0),
                    context_status=str(runtime.get("claude_context_status") or "unknown"),
                    context_percent=(
                        int(runtime["claude_context_percent"])
                        if isinstance(runtime.get("claude_context_percent"), (int, float))
                        and not isinstance(runtime.get("claude_context_percent"), bool)
                        else None
                    ),
                    compaction_percent=(
                        int(runtime["claude_compaction_current_percent"])
                        if isinstance(runtime.get("claude_compaction_current_percent"), (int, float))
                        and not isinstance(runtime.get("claude_compaction_current_percent"), bool)
                        else None
                    ),
                    # Exposure durations. A bare "缺失" label hid a pane that had
                    # been unprotected for 14.5h, and a bare "看不清" hid one blind
                    # for the whole 5.65h observation. The number is the point.
                    unprotected_sec=_elapsed_since(runtime.get("claude_hook_unprotected_since")),
                    unreadable_sec=_elapsed_since(runtime.get("claude_unreadable_since")),
                    context_age_sec=_elapsed_since(runtime.get("claude_context_sampled_at")),
                    deferred_reason=(
                        str(runtime.get("claude_deferred_reason") or "")
                        if runtime.get("claude_deferred_event") else ""
                    ),
                    deferred_sec=(
                        _elapsed_since(runtime.get("claude_deferred_since"))
                        if runtime.get("claude_deferred_event") else 0.0
                    ),
                    # Last completed resolution for this surface.  Absent until
                    # the first worker pass lands, which is why the default is
                    # an unmeasured result rather than an empty string.
                    session=session_by_id.get(surface_id) or SessionResult(),
                ))
            live_ids = {row.surface_id for row in rows}
            for target in self.config.get("targets", []):
                surface_id = str(target.get("surface_id") or "")
                if not surface_id or surface_id in live_ids:
                    continue
                runtime = self.runtime.get(surface_id, {}) if isinstance(self.runtime, Mapping) else {}
                rows.append(Candidate(
                    record={
                        "surface_id": surface_id,
                        "workspace_id": str(target.get("workspace_id") or ""),
                        # Persisted at registration time; a target registered
                        # before that existed still shows "?" and cannot be
                        # recovered, because its pane is already gone.
                        "workspace_ref": str(target.get("workspace_ref") or "?"),
                        "pane_ref": str(target.get("pane_ref") or ""),
                        "ref": str(target.get("ref") or surface_id[:8]),
                        "title": str(target.get("title_snapshot") or target.get("name") or "(not present)"),
                    },
                    source="explicit",
                    state=str(runtime.get("state") or "missing"),
                    error_type=str(runtime.get("error_type") or "-"),
                    send_count=int(runtime.get("send_count") or 0),
                    paused=bool(target.get("paused")),
                    selected_hint=surface_id == self.suggested_surface,
                    status_detail=str(runtime.get("paused_reason") or target.get("paused_reason") or ""),
                    # The surface is gone, so nothing is running in it.  Claiming
                    # "Codex" here was a lie the 程序 column then printed.
                    agent_kind="unknown",
                    process_summary="目标已消失",
                    hook_health="offline",
                    consecutive_resumes=int(runtime.get("claude_consecutive_resumes") or 0),
                    repeat_warning=bool(runtime.get("claude_repeat_warning")),
                    hook_live_sends=int(runtime.get("claude_hook_live_send_count") or 0),
                    hook_sla_misses=int(runtime.get("claude_hook_sla_miss_count") or 0),
                    context_status=str(runtime.get("claude_context_status") or "unknown"),
                    context_percent=(
                        int(runtime["claude_context_percent"])
                        if isinstance(runtime.get("claude_context_percent"), (int, float))
                        and not isinstance(runtime.get("claude_context_percent"), bool)
                        else None
                    ),
                    compaction_percent=(
                        int(runtime["claude_compaction_current_percent"])
                        if isinstance(runtime.get("claude_compaction_current_percent"), (int, float))
                        and not isinstance(runtime.get("claude_compaction_current_percent"), bool)
                        else None
                    ),
                    # Exposure durations. A bare "缺失" label hid a pane that had
                    # been unprotected for 14.5h, and a bare "看不清" hid one blind
                    # for the whole 5.65h observation. The number is the point.
                    unprotected_sec=_elapsed_since(runtime.get("claude_hook_unprotected_since")),
                    unreadable_sec=_elapsed_since(runtime.get("claude_unreadable_since")),
                    context_age_sec=_elapsed_since(runtime.get("claude_context_sampled_at")),
                    deferred_reason=(
                        str(runtime.get("claude_deferred_reason") or "")
                        if runtime.get("claude_deferred_event") else ""
                    ),
                    deferred_sec=(
                        _elapsed_since(runtime.get("claude_deferred_since"))
                        if runtime.get("claude_deferred_event") else 0.0
                    ),
                ))
            self.candidates = sorted(rows, key=lambda row: (
                core._ref_number(str(row.record.get("workspace_ref") or "")),
                core._ref_number(str(row.record.get("pane_ref") or "")),
                core._ref_number(str(row.record.get("ref") or "")),
            ))
            self.online = True
            self.error = ""
        except (core.CmuxError, RuntimeError, OSError) as exc:
            self.online = False
            self.error = str(exc)

    def run_cli(self, args: list[str]) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = core.cli(["--config", str(self.config_path), *args])
        if code:
            raise RuntimeError(output.getvalue().strip() or f"command failed: {' '.join(args)}")
        return output.getvalue().strip()

    def mutate_selected(self, candidate: Candidate, action: str) -> None:
        surface_id = candidate.surface_id
        if action == "add":
            if candidate.source != "untracked":
                raise RuntimeError("已监控目标不用再登记")
            # Single exit: always track-surface, so Dock stays excluded at both
            # the pane and the surface level via find_main_surface.  The process
            # label only decides whether the waiver flag is attached; it never
            # decides which command runs.
            args = ["track-surface", surface_id, "--name", target_name(candidate.record)]
            if candidate.agent_kind != "codex":
                # The non-Codex warning was acknowledged in the confirm prompt.
                args.append("--allow-non-codex")
            self.run_cli(args)
        elif action == "workspace":
            self.run_cli(["track-workspace", candidate.record["workspace_id"], "--name", workspace_rule_name(candidate.record)])
        elif action == "pause":
            if candidate.source in {"workspace_rule", "workspace_excluded"}:
                self.run_cli(["exclude", surface_id])
            else:
                self.run_cli(["pause", surface_id])
        elif action == "resume":
            if candidate.source in {"workspace_rule", "workspace_excluded"}:
                self.run_cli(["include", surface_id])
            else:
                self.run_cli(["resume", surface_id])
        elif action == "remove":
            if candidate.source != "explicit":
                raise RuntimeError("只有单路登记能用 x 删除")
            self.run_cli(["remove", surface_id])
        elif action == "untrack_workspace":
            if candidate.source not in {"workspace_rule", "workspace_excluded", "workspace_non_codex"}:
                raise RuntimeError("只有整池目标才能取消 workspace 授权")
            selector = candidate.record.get("workspace_id") or candidate.record.get("workspace_ref")
            if not selector:
                raise RuntimeError("当前行没有 workspace，无法取消整池授权")
            self.run_cli(["untrack-workspace", str(selector)])
        else:
            raise RuntimeError(f"unsupported supervisor action: {action}")

    def mutate_workspace(self, row: ViewRow, action: str) -> None:
        """Pool-level actions taken from a workspace header row."""
        if action == "workspace":
            if row.counts.get("pool"):
                raise RuntimeError(f"{row.workspace_ref} 已经是整池授权")
            self.run_cli(["track-workspace", row.workspace_id,
                          "--name", row.workspace_title or row.workspace_ref])
        elif action == "untrack_workspace":
            if not row.counts.get("pool"):
                raise RuntimeError(f"{row.workspace_ref} 没有整池授权，不用取消")
            self.run_cli(["untrack-workspace", row.workspace_id])
        else:
            raise RuntimeError("组头只支持 w 授权整池 · u 取消整池 · Tab 折叠")

    def counts(self) -> dict[str, int]:
        idling = sum(1 for item in self.candidates if is_idling(item))
        paused = sum(1 for item in self.candidates
                     if item.source != "untracked" and not is_idling(item)
                     and (item.paused or item.source == "workspace_excluded"))
        registered = sum(1 for item in self.candidates if item.source != "untracked")
        return {
            "watched": registered,
            # 监控中 now means "registered, not paused, Codex actually running";
            # 空转 is the rest of the registered set that cannot be sent to.
            "watching": registered - idling - paused,
            "idling": idling,
            "paused": paused,
            "untracked": sum(1 for item in self.candidates if item.source == "untracked"),
            "all": len(self.candidates),
        }

    def set_mode(self, mode: str) -> None:
        self.run_cli([mode])


# ---------------------------------------------------------------- cmux junk
# cmux writes a per-turn diff baseline under ~/.cmuxterm.  Captures that die
# before publishing leave the staging directory behind, and interrupted atomic
# writes of the live store leave full-size ``.sb-*`` copies.  Neither is ever
# reclaimed by cmux itself, so the daemon's own rescue activity — more turns —
# converts directly into disk growth.
#
# This panel is strictly a *reader*, and the boundary is deliberate:
#
#   * Every count and byte total comes from the janitor's own published state,
#     fetched through ``cmux-janitorctl status --json``.  The TUI does not walk
#     ~/.cmuxterm, does not parse the janitor's config.env, and does not stat
#     its DISABLED/GUARD_TRIPPED sentinels.  Reading another component's
#     private files is how the panel came to promise 48h retention while the
#     script was enforcing 24h.
#   * Nothing is fetched from the draw path.  An earlier version called
#     scan_junk() inside _draw(); a cold scan of a four-figure backlog stalled
#     the whole interface for 6.3-8.4s.  Refresh happens on a worker thread and
#     the draw path reads whatever the last one left behind.
#   * Disposal stays in the janitor script, which owns every safety gate.
#
# Byte units are IEC throughout — KiB/MiB/GiB, 1024-based — because that is
# what ``du`` reports and what the janitor's state carries.  This is *this
# project's* choice and is stated here so nobody has to infer it: mole is not
# the source of the convention (upstream cmd/analyze/format.go:89-91 calls
# units.BytesSI, which is 1000-based).
JANITOR_DIR = Path.home() / ".config" / "cmux-janitor"
JANITOR_CTL = JANITOR_DIR / "cmux-janitorctl"

# How long a fetched snapshot stays usable before the worker refetches.
JUNK_CACHE_TTL_SEC = 60.0
# A ctl call is a Python interpreter start; give it room but never wait forever.
JANITOR_CTL_TIMEOUT_SEC = 20.0
# Freshness limits for the two published documents.  The janitor runs every
# 30min, so 45min means it missed a cycle; the guard runs every 60s.
#
# There is deliberately no backlog *size* threshold here any more.  Sweeping is
# unconditional every 30 minutes, so size alone never told the operator whether
# anything was wrong; staleness and the sentinels do.
JANITOR_FRESH_LIMIT_SEC = 45 * 60.0
GUARD_FRESH_LIMIT_SEC = 3 * 60.0


def format_bytes(value: float, *, binary: bool = True) -> str:
    """Human-readable size.  ``binary`` picks GiB (1024) over GB (1000)."""
    step = 1024.0 if binary else 1000.0
    units = ("B", "KiB", "MiB", "GiB", "TiB") if binary else ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < step or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= step
    return f"{size:.1f}{units[-1]}"


def _measurement(raw: Any) -> tuple[int | None, str]:
    """Unpack one ``{"value": N, "precision": "..."}`` pair from ctl.

    ctl already rejects NaN, infinities, booleans and negatives, replacing the
    value with ``None`` and the precision with ``"unknown"``.  This re-checks
    rather than trusting it: the panel must never render an unmeasured figure as
    a confident 0, and a stale ctl on disk is exactly the case where the two
    versions disagree.
    """

    if not isinstance(raw, Mapping):
        return None, "unknown"
    value = _finite_int(raw.get("value"))
    precision = raw.get("precision")
    if precision not in {"exact", "estimated", "unknown"}:
        precision = "unknown"
    if value is None:
        return None, "unknown"
    return value, precision


def _finite_int(raw: Any) -> int | None:
    """A non-negative finite integer, or ``None`` for anything else.

    ``bool`` is rejected before ``int`` on purpose: ``True`` is an ``int`` in
    Python and would otherwise render as the count 1.
    """

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw != raw or raw in (float("inf"), float("-inf")):
        return None
    if raw < 0:
        return None
    return int(raw)


class JanitorClient:
    """The panel's only route to janitor state.

    Everything is fetched by running ``cmux-janitorctl status --json`` on a
    worker thread.  ``snapshot()`` is what the draw path calls and it never
    blocks, never walks a directory and never opens a janitor-private file: it
    returns the last completed fetch, or an "absent" snapshot before the first
    one lands.
    """

    def __init__(self, ctl: Path | None = None, *,
                 ttl_sec: float = JUNK_CACHE_TTL_SEC,
                 timeout_sec: float = JANITOR_CTL_TIMEOUT_SEC) -> None:
        self.ctl = JANITOR_CTL if ctl is None else ctl
        self.ttl_sec = ttl_sec
        self.timeout_sec = timeout_sec
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = _absent_snapshot("尚未读取")
        self._fetched_at = 0.0
        self._worker: threading.Thread | None = None
        # Control actions get their own worker and their own reported phase, so
        # a sweep request in flight cannot be confused with a stale status read.
        self._action_worker: threading.Thread | None = None
        self._action_phase = ""
        self._action_message = ""

    # ---- draw-path side: must never block ----

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def maybe_refresh(self, *, now: float | None = None, force: bool = False) -> bool:
        """Start a fetch if the cached snapshot is due.  Returns True if started.

        Called once per draw pass.  A fetch already in flight is never joined
        and never duplicated, so a slow ctl delays the numbers, not the redraw.
        """

        moment = time.time() if now is None else now
        with self._lock:
            running = self._worker is not None and self._worker.is_alive()
            due = force or (moment - self._fetched_at) >= self.ttl_sec
            if running or not due:
                return False
            worker = threading.Thread(target=self._fetch_once, name="janitor-status",
                                      daemon=True)
            self._worker = worker
        worker.start()
        return True

    def wait_for_refresh(self, timeout: float = 30.0) -> bool:
        """Block until the in-flight fetch finishes.  Tests and `r` use this."""

        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    # ---- control side: also off the draw path ----

    def action_state(self) -> tuple[str, str]:
        """``(phase, message)`` for the pending control action.

        ``phase`` is ``""`` when nothing is running.  The draw path reads this
        the same way it reads ``snapshot()``: a dict/tuple copy under a lock.
        """

        with self._lock:
            return self._action_phase, self._action_message

    def start_action(self, command: str) -> bool:
        """Run one ctl subcommand on a worker thread.

        ``pause``/``resume``/``run --manual`` all shell out, and ``run`` in
        particular re-reads state before it will start.  None of that may happen
        between a keypress and the next redraw, so the keypress only starts the
        work and the row reports progress.
        """

        argv = {
            "pause": ["pause"],
            "resume": ["resume"],
            "run": ["run", "--manual"],
        }.get(command)
        if argv is None:
            raise ValueError(f"unsupported janitor action: {command}")
        with self._lock:
            if self._action_worker is not None and self._action_worker.is_alive():
                return False
            self._action_phase = "running"
            self._action_message = {
                "pause": "正在暂停清扫器…",
                "resume": "正在恢复清扫器…",
                "run": "正在请求手动清扫…",
            }[command]
            worker = threading.Thread(target=self._run_action, args=(command, argv),
                                      name=f"janitor-{command}", daemon=True)
            self._action_worker = worker
        worker.start()
        return True

    def wait_for_action(self, timeout: float = 60.0) -> bool:
        with self._lock:
            worker = self._action_worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def clear_action(self) -> None:
        with self._lock:
            if self._action_worker is not None and self._action_worker.is_alive():
                return
            self._action_phase = ""
            self._action_message = ""

    def _run_action(self, command: str, argv: list[str]) -> None:
        phase, message = self._invoke_control(command, argv)
        with self._lock:
            self._action_phase = phase
            self._action_message = message
            # A control action changes exactly what the row displays, so the
            # next draw pass refetches rather than showing pre-action numbers.
            self._fetched_at = 0.0

    def _invoke_control(self, command: str, argv: list[str]) -> tuple[str, str]:
        """Run ctl and translate its exit code into a phase and a message.

        ctl's refusals (rc=1) carry a ``refused`` list explaining which gate said
        no; those are surfaced verbatim rather than flattened into "failed", so
        "guard is tripped" does not read the same as "ctl is broken".
        """

        label = {"pause": "暂停", "resume": "恢复", "run": "手动清扫"}[command]
        if not self.ctl.is_file():
            return "error", f"{label}失败: 控制器未安装"
        try:
            result = subprocess.run(
                [sys.executable, str(self.ctl), *argv],
                capture_output=True, text=True, timeout=self.timeout_sec,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "error", f"{label}失败: 控制器超时"
        except OSError as exc:
            return "error", f"{label}失败: {exc.strerror or exc}"

        detail = ""
        with contextlib.suppress(json.JSONDecodeError, ValueError, AttributeError):
            document = json.loads(result.stdout)
            if isinstance(document, Mapping):
                refused = document.get("refused")
                if isinstance(refused, list) and refused:
                    detail = "；".join(str(item) for item in refused[:3])
                elif isinstance(document.get("fail_closed"), str):
                    detail = document["fail_closed"]
        if not detail:
            trailing = (result.stderr or "").strip().splitlines()
            detail = trailing[-1][:120] if trailing else ""

        if result.returncode == 0:
            done = {"pause": "已暂停清扫器", "resume": "已恢复清扫器",
                    "run": "已请求手动清扫（仍走全套闸门）"}[command]
            return "done", done
        if result.returncode == 1:
            return "refused", f"{label}被拒: {detail or '控制器未说明原因'}"
        return "error", f"{label}失败(退出码 {result.returncode}): {detail or '无输出'}"

    # ---- worker side ----

    def _fetch_once(self) -> None:
        snapshot = self.fetch()
        with self._lock:
            self._snapshot = snapshot
            self._fetched_at = time.time()

    def fetch(self) -> dict[str, Any]:
        """Run ctl once and convert its document into a panel snapshot.

        Every failure mode lands in the same shape with a stated reason, because
        "the janitor is not reporting" is itself a condition the operator needs
        to see rather than a blank row.
        """

        if not self.ctl.is_file():
            return _absent_snapshot("控制器未安装")
        try:
            result = subprocess.run(
                [sys.executable, str(self.ctl), "status", "--json"],
                capture_output=True, text=True, timeout=self.timeout_sec,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return _absent_snapshot("控制器超时")
        except OSError as exc:
            return _absent_snapshot(f"控制器无法运行: {exc.strerror or exc}")
        # rc=3 is ctl's fail-closed exit; it still prints a reason on stderr.
        if result.returncode not in (0,):
            reason = (result.stderr or result.stdout or "").strip().splitlines()
            detail = reason[-1][:80] if reason else f"退出码 {result.returncode}"
            return _absent_snapshot(f"控制器拒绝: {detail}")
        try:
            document = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return _absent_snapshot("控制器输出无法解析")
        if not isinstance(document, Mapping):
            return _absent_snapshot("控制器输出不是对象")
        return _snapshot_from_status(document)


def _absent_snapshot(reason: str) -> dict[str, Any]:
    """A snapshot that says "no measurement", never one that implies zero."""

    return {
        "available": False,
        "reason": reason,
        "paused": None,
        "guard_tripped": None,
        "janitor_mode": "absent",
        "janitor_age_sec": None,
        "guard_age_sec": None,
        "guard_health": None,
        "safety_complete": None,
        "candidate_counts": {},
        "selected_bytes": None,
        "selected_precision": "unknown",
        "quarantine_count": None,
        "quarantine_bytes": None,
        "quarantine_precision": "unknown",
        "quarantine_keep_hours": None,
        "per_run_limit": None,
        "measured_at": None,
        "phase": None,
        "error": None,
        # None, not False: when ctl cannot be read at all we have no idea whether
        # launchd holds the labels, and rendering "未加载" from a missing
        # measurement would be the same "absent means zero" mistake this whole
        # snapshot shape exists to avoid.
        "janitor_loaded": None,
        "guard_loaded": None,
    }


def _launchd_flag(document: Mapping[str, Any], key: str) -> bool | None:
    """Read one ctl ``launchd`` flag, keeping "unknown" distinct from "no".

    ctl publishes ``None`` when ``launchctl print`` cannot answer
    (cmux-janitorctl:318), so the tri-state has to survive the projection: a
    coerced ``bool()`` here would turn "could not ask launchd" into a
    confident "not loaded" and put a false alarm on the panel.
    """

    launchd = document.get("launchd")
    if not isinstance(launchd, Mapping):
        return None
    value = launchd.get(key)
    return value if isinstance(value, bool) else None


def _snapshot_from_status(document: Mapping[str, Any]) -> dict[str, Any]:
    """Project ctl's document onto the fields the panel renders.

    Candidate figures and quarantine figures stay in separate keys and are never
    summed.  An earlier version added them (``tracked = reclaimable +
    quarantine_bytes``) which meant the alarm was driven almost entirely by
    material the janitor had already dealt with.
    """

    control = document.get("control")
    control = control if isinstance(control, Mapping) else {}
    janitor = document.get("janitor")
    janitor = janitor if isinstance(janitor, Mapping) else {}
    quarantine = document.get("quarantine")
    quarantine = quarantine if isinstance(quarantine, Mapping) else {}
    guard = document.get("guard")
    guard = guard if isinstance(guard, Mapping) else {}
    counts = janitor.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}

    selected_bytes, selected_precision = _measurement(janitor.get("selected_bytes"))
    quarantine_bytes, quarantine_precision = _measurement(quarantine.get("bytes"))

    mode = janitor.get("mode")
    if mode not in {"dry", "apply"}:
        mode = control.get("config_mode") if control.get("config_mode") in {"dry", "apply"} else "unknown"

    return {
        "available": True,
        "reason": "",
        "paused": control.get("paused") is True,
        "guard_tripped": control.get("guard_tripped") is True,
        "janitor_mode": mode,
        # Three separate figures with three separate meanings; the panel labels
        # each one rather than presenting a single "reclaimable" total.
        "candidate_counts": {
            "raw": _sum_optional(counts.get("raw_sb"), counts.get("raw_staging")),
            "eligible": _finite_int(counts.get("eligible")),
            "selected": _finite_int(counts.get("selected")),
            "disposed": _finite_int(counts.get("disposed")),
            "protected": _finite_int(counts.get("protected")),
        },
        "selected_bytes": selected_bytes,
        "selected_precision": selected_precision,
        "safety_complete": janitor.get("safety_complete") is True,
        "janitor_age_sec": _finite_number_or_none(janitor.get("age_sec")),
        "measured_at": janitor.get("observed_at") if isinstance(janitor.get("observed_at"), str) else None,
        "quarantine_count": _finite_int(quarantine.get("batch_count")),
        "quarantine_bytes": quarantine_bytes,
        "quarantine_precision": quarantine_precision,
        "quarantine_keep_hours": _finite_int(quarantine.get("keep_hours")),
        # MAX_ITEMS_PER_RUN, so the confirm box can state the cap a manual sweep
        # will honour rather than implying it clears the whole backlog.
        "per_run_limit": _finite_int(janitor.get("limit")),
        "guard_health": guard.get("health") if isinstance(guard.get("health"), str) else None,
        "guard_age_sec": _finite_number_or_none(guard.get("age_sec")),
        # ctl publishes launchd's own answer (cmux-janitorctl:395-397) and the
        # panel ignored it, so a janitor that had been booted out of launchd
        # still rendered "运行中 / healthy": every state file it had already
        # written stayed valid and fresh-looking while nothing was scheduled to
        # run again.  That is not hypothetical -- on 2026-08-29T06:39:50Z both
        # labels were booted out and this page reported a healthy sweeper.
        # None means launchctl could not answer, which is distinct from False.
        "janitor_loaded": _launchd_flag(document, "janitor_loaded"),
        "guard_loaded": _launchd_flag(document, "guard_loaded"),
        # The janitor's own verdict on its last run.  Dropping these was a real
        # defect: a run that aborted on a bad config publishes phase="error" with
        # the reason, and a panel that ignored both rendered "运行中" while the
        # scheduled job was failing every 30 minutes.
        "phase": janitor.get("phase") if isinstance(janitor.get("phase"), str) else None,
        "error": janitor.get("error") if isinstance(janitor.get("error"), str) else None,
    }


def _sum_optional(*values: Any) -> int | None:
    """Add measured parts; ``None`` if any part was not measured."""

    total = 0
    for value in values:
        number = _finite_int(value)
        if number is None:
            return None
        total += number
    return total


def _finite_number_or_none(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw != raw or raw in (float("inf"), float("-inf")):
        return None
    return float(raw)


def _age_text(age_sec: float | None) -> str:
    if age_sec is None:
        return "无测量"
    minutes = int(age_sec // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes}分钟前"
    return f"{minutes // 60}小时{minutes % 60}分钟前"


def _loaded_text(flag: bool | None) -> str:
    """Render one launchd load state, keeping "unknown" out of "not loaded".

    ``None`` means launchctl could not be asked, which is not the same claim as
    "the agent is gone"; printing 未加载 for it would invent an outage.
    """

    if flag is None:
        return "未测量"
    return "已加载" if flag else "未加载"


def _count_text(value: int | None) -> str:
    return "?" if value is None else str(value)


def _bytes_text(value: int | None, precision: str) -> str:
    if value is None:
        return "未测量"
    prefix = "~" if precision == "estimated" else ""
    return f"{prefix}{format_bytes(value)}"


# --------------------------------------------------------------- cmux-stack
#
# cmux-stack is the control-plane aggregator over the three components (watcher,
# janitor, ccp-new profiles).  This panel consumes it exactly the way it consumes
# the janitor: through the published `status --json` projection, on a worker
# thread, never from the draw path.
#
# Two contract details are NOT copied from JanitorClient, and both were measured
# rather than assumed:
#
#   * rc.  JanitorClient.fetch() treats `rc != 0` as unreadable.  That is right
#     for janitorctl, which returns 0 even when degraded.  cmux-stack returns
#     rc=1 for degraded -- which is the CURRENT state of this machine -- so
#     copying that test would render a fully valid, fully readable document as
#     "cannot read".  Valid exit codes here are 0/1/3/4; only rc=2 (usage, and
#     its stdout is empty) and unparseable output are errors.
#   * timeout.  cmux-stack probes serially (build_status has no concurrency
#     primitives) with 3 launchctl calls at a hardcoded 10s plus 3 subprobes at
#     30s, so its own worst case is 120s.  A 20s external timeout would kill it
#     while it is still working correctly.  Hence 150s here.
#
# Known defect, recorded rather than worked around: CMUX_STACK_TIMEOUT does not
# cover the launchctl leg (cmux-stack:116 passes timeout=10 literally), so
# shrinking that variable does NOT shrink the total budget.  Measured: a slow
# launchctl with CMUX_STACK_TIMEOUT=2 still took 10.2s.  Fixing that means
# changing cmux-stack itself, which is a later phase.
STACK_CTL = Path(__file__).resolve().parent / "bin" / "cmux-stack"

# Longer than the janitor's TTL: this aggregates three components and each one
# costs a Python interpreter start.
STACK_CACHE_TTL_SEC = 90.0
# Must exceed cmux-stack's own serial worst case (3*10 + 3*30 = 120s), or the
# panel reports a timeout for a controller that is still inside its own budget.
STACK_CTL_TIMEOUT_SEC = 150.0
# Exit codes that still carry a usable JSON document.
STACK_READABLE_RCS = (0, 1, 3, 4)


def _stack_absent(reason: str) -> dict[str, Any]:
    """A snapshot that says "no measurement", never one that implies healthy."""

    return {
        "available": False,
        "reason": reason,
        "overall": None,
        "probed_count": None,
        "requested_count": None,
        "unhealthy": [],
        "unknown": [],
        "components": {},
    }


def _flat_flag(row: Mapping[str, Any], key: str) -> bool | None:
    """Read a tri-state boolean that sits at the TOP level of a mapping.

    Deliberately separate from :func:`_launchd_flag`, which reads janitorctl's
    *nested* ``launchd`` block.  cmux-stack publishes ``launchd_loaded`` and
    ``guard_launchd_loaded`` directly on each component row instead, so calling
    the nested reader on a component row returns ``None`` for every input --
    which silently pins the tri-state to "未測量" no matter what the controller
    actually said.  That is the 106.14.4 shape exactly: a field the producer
    publishes and the consumer never manages to read.  Two shapes, two readers,
    and a test for each.
    """

    value = row.get(key)
    return value if isinstance(value, bool) else None


def _stack_component(raw: Any, name: str) -> dict[str, Any]:
    """Project ONE component by naming every field.  Never dict(raw).

    A whitelist is the point: the upstream document grows fields over time (the
    R1 audit added four), and a filter would let each new one through by
    default.  Anything not named here cannot reach the screen.
    """

    row = raw if isinstance(raw, Mapping) else {}
    projected: dict[str, Any] = {
        "component": name,
        "installed": row.get("installed") is True,
        "probe_ok": row.get("probe_ok") is True,
        # Tri-state, deliberately not coerced with bool(): None means the probe
        # answered "unknown", and flattening it would either invent an outage or
        # hide one.
        "healthy": row.get("healthy") if isinstance(row.get("healthy"), bool) else None,
        "launchd_loaded": _flat_flag(row, "launchd_loaded"),
        "reason": row.get("reason") if isinstance(row.get("reason"), str) else None,
    }
    if name == "watcher":
        projected["pid"] = _finite_int(row.get("pid"))
        projected["pid_alive"] = row.get("pid_alive") is True
        projected["mode"] = row.get("mode") if isinstance(row.get("mode"), str) else None
        projected["source_matches_disk"] = (
            row.get("source_matches_disk") if isinstance(row.get("source_matches_disk"), bool) else None
        )
    elif name == "janitor":
        # Summary only.  Janitor DETAIL has exactly one source in this panel --
        # the junk row and the G page, which read cmux-janitorctl directly.  If
        # this row also derived janitor verdicts there would be two independent
        # derivations of one fact, and the panel could contradict itself the
        # moment they drift.
        projected["guard_launchd_loaded"] = _flat_flag(row, "guard_launchd_loaded")
        projected["guard_health"] = (
            row.get("guard_health") if isinstance(row.get("guard_health"), str) else None
        )
        projected["paused"] = row.get("paused") if isinstance(row.get("paused"), bool) else None
    elif name == "profiles":
        projected["profile_count"] = _finite_int(row.get("profile_count"))
        projected["unhealthy_count"] = _finite_int(row.get("unhealthy_count"))
    return projected


def _stack_snapshot_from_status(document: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one `cmux-stack status --json` document into a panel snapshot."""

    components_raw = document.get("components")
    components_raw = components_raw if isinstance(components_raw, Mapping) else {}
    components = {
        name: _stack_component(components_raw.get(name), name)
        for name in ("watcher", "janitor", "profiles")
        if name in components_raw
    }
    overall = document.get("overall")
    return {
        "available": True,
        "reason": None,
        "overall": overall if isinstance(overall, str) else None,
        "probed_count": _finite_int(document.get("probed_count")),
        "requested_count": _finite_int(document.get("requested_count")),
        "unhealthy": [n for n in (document.get("unhealthy") or []) if isinstance(n, str)][:8],
        "unknown": [n for n in (document.get("unknown") or []) if isinstance(n, str)][:8],
        "components": components,
    }


class StackClient:
    """The panel's only route to cmux-stack state.  Read-only by construction.

    There is no action method here, and that is a design decision rather than an
    omission: JanitorClient has start_action() because the janitor's controller
    owns its own write gates, whereas cmux-stack spans three trust domains.
    Giving one keystroke the power to bootstrap or bootout across all three is a
    blast radius the panel should not have.  Write commands are shown as
    copyable CLI text instead.
    """

    def __init__(self, ctl: Path | None = None, *,
                 ttl_sec: float = STACK_CACHE_TTL_SEC,
                 timeout_sec: float = STACK_CTL_TIMEOUT_SEC) -> None:
        self.ctl = STACK_CTL if ctl is None else ctl
        self.ttl_sec = ttl_sec
        self.timeout_sec = timeout_sec
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = _stack_absent("尚未读取")
        self._fetched_at = 0.0
        self._worker: threading.Thread | None = None

    # ---- draw-path side: must never block ----

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def maybe_refresh(self, *, now: float | None = None, force: bool = False) -> bool:
        """Start a fetch if the cached snapshot is due.  Returns True if started."""

        moment = time.time() if now is None else now
        with self._lock:
            running = self._worker is not None and self._worker.is_alive()
            due = force or (moment - self._fetched_at) >= self.ttl_sec
            if running or not due:
                return False
            worker = threading.Thread(target=self._fetch_once, name="stack-status",
                                      daemon=True)
            self._worker = worker
        worker.start()
        return True

    def wait_for_refresh(self, timeout: float = 30.0) -> bool:
        """Block until the in-flight fetch finishes.  Tests and `r` use this."""

        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    # ---- worker side ----

    def _fetch_once(self) -> None:
        snapshot = self.fetch()
        with self._lock:
            self._snapshot = snapshot
            self._fetched_at = time.time()

    def fetch(self) -> dict[str, Any]:
        """Run the controller once and project its document.

        Every failure lands in the same absent shape with a stated reason,
        because "the controller is not reporting" is itself something the
        operator needs to see rather than a blank row.
        """

        if not self.ctl.is_file():
            return _stack_absent("控制器未安装")
        try:
            result = subprocess.run(
                [sys.executable, str(self.ctl), "status", "--json"],
                capture_output=True, text=True, timeout=self.timeout_sec,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return _stack_absent("控制器超时")
        except OSError as exc:
            return _stack_absent(f"控制器无法运行: {exc.strerror or exc}")
        # 0/1/3/4 all carry a document; rc=1 in particular is "degraded", which
        # is a reading, not a failure to read.  rc=2 is usage error and prints
        # nothing on stdout.
        if result.returncode not in STACK_READABLE_RCS:
            reason = (result.stderr or result.stdout or "").strip().splitlines()
            detail = reason[-1][:80] if reason else f"退出码 {result.returncode}"
            return _stack_absent(f"控制器拒绝: {detail}")
        try:
            document = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return _stack_absent("控制器输出无法解析")
        if not isinstance(document, Mapping):
            return _stack_absent("控制器输出不是对象")
        return _stack_snapshot_from_status(document)


def _stack_verdict_text(row: Mapping[str, Any]) -> str:
    """One component's one-word verdict, scheduling first.

    Same precedence as the controller's own ladder and for the same reason
    (106.14.4): every other field is a file read that keeps its last healthy
    value when nothing is scheduled to overwrite it.
    """

    if not row.get("probe_ok"):
        return "未探测"
    if row.get("launchd_loaded") is False:
        return "未加载"
    if row.get("healthy") is True:
        return "正常"
    if row.get("healthy") is False:
        return "异常"
    return "未知"


def _stack_overall_text(overall: Any) -> str:
    """Render the aggregate verdict.  One definition, used by both surfaces.

    The main-screen row and the v page must never disagree about what
    "degraded" is called.  An inline copy in each of them is how two renderings
    of one fact drift apart, so the mapping lives here only.  An unrecognised
    verdict falls through as itself rather than becoming a cheerful default:
    a new value upstream should look unfamiliar, not look healthy.
    """

    if not isinstance(overall, str) or not overall:
        return "状态未知"
    return {"ok": "全部正常", "degraded": "有异常",
            "partial": "组件缺失", "unknown": "状态未知"}.get(overall, overall)


def stack_line(snapshot: Mapping[str, Any]) -> str:
    """One summary row for the main screen.  No janitor detail lives here."""

    if not snapshot.get("available"):
        reason = snapshot.get("reason") or "无法读取"
        return f"✗ 三件套 无法读取({reason}) | 请查 cmux-stack status"

    label = _stack_overall_text(snapshot.get("overall"))
    components = snapshot.get("components")
    components = components if isinstance(components, Mapping) else {}
    names = {"watcher": "续跑", "janitor": "清扫", "profiles": "配置"}
    icon = "✓" if snapshot.get("overall") == "ok" else ("⚠" if snapshot.get("overall") else "?")
    parts = [f"{icon} 三件套 {label}"]
    for key in ("watcher", "janitor", "profiles"):
        if key in components:
            parts.append(f"{names[key]} {_stack_verdict_text(components[key])}")
    probed = snapshot.get("probed_count")
    requested = snapshot.get("requested_count")
    if isinstance(probed, int) and isinstance(requested, int) and probed < requested:
        parts.append(f"已探测 {probed}/{requested}")
    parts.append("v 详情")
    return " | ".join(parts)


def stack_is_alarming(snapshot: Mapping[str, Any]) -> bool:
    """Highlight only what the operator has to act on."""

    if not snapshot.get("available"):
        return True
    if snapshot.get("overall") != "ok":
        return True
    components = snapshot.get("components")
    components = components if isinstance(components, Mapping) else {}
    for row in components.values():
        if not isinstance(row, Mapping):
            return True
        if row.get("launchd_loaded") is False or row.get("healthy") is not True:
            return True
    return False


def junk_line(snapshot: Mapping[str, Any]) -> str:
    """One ASCII-separated row, same shape as the Hook/context summaries.

    Candidates and quarantine are reported side by side and never added
    together.  They mean opposite things: candidates are material still on the
    live tree, quarantine is material the janitor already moved out of the way
    and will delete on its own schedule.  Summing them (the previous
    ``tracked = reclaimable + quarantine_bytes``) made the row grow when the
    janitor was working *well*.

    Every figure that was not measured prints as ``?`` or ``未测量``.  None of
    them fall back to 0.
    """

    if not snapshot.get("available"):
        reason = snapshot.get("reason") or "无法读取"
        return f"✗ cmux垃圾 无法读取({reason}) | 请查 cmux-janitorctl status"

    counts = snapshot.get("candidate_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    keep_hours = snapshot.get("quarantine_keep_hours")
    keep_text = "?" if keep_hours is None else f"{keep_hours}h"

    parts = [
        # Three stages of the same funnel, so a backlog that is present but
        # deliberately protected cannot be mistaken for one the janitor is
        # failing to clear.
        f"cmux垃圾 候选 原始{_count_text(counts.get('raw'))}"
        f"/合格{_count_text(counts.get('eligible'))}"
        f"/选中{_count_text(counts.get('selected'))}",
        f"待清 {_bytes_text(snapshot.get('selected_bytes'), str(snapshot.get('selected_precision')))}",
        f"隔离 {_count_text(snapshot.get('quarantine_count'))}批 "
        f"{_bytes_text(snapshot.get('quarantine_bytes'), str(snapshot.get('quarantine_precision')))}"
        f"(留{keep_text})",
    ]

    if snapshot.get("guard_tripped"):
        parts.append(f"清扫器 守卫跳闸 最后测量{_age_text(snapshot.get('janitor_age_sec'))}")
    elif snapshot.get("paused"):
        # A paused janitor's state file goes stale by design, so the age is
        # reported as information rather than as a fault.
        parts.append(f"清扫器 已暂停 最后测量{_age_text(snapshot.get('janitor_age_sec'))}")
    elif snapshot.get("phase") == "error":
        # The janitor aborted its own run, which it reports by publishing
        # phase="error" plus the reason.  Without this branch the row printed
        # "清扫器 dry 刚刚" for a janitor that had refused to run at all: the
        # mode is what it *would* use, not evidence that anything ran.
        detail = snapshot.get("error") or "未说明原因"
        parts.append(f"清扫器 运行中止({detail})")
    else:
        parts.append(f"清扫器 {snapshot.get('janitor_mode', 'unknown')} "
                     f"{_age_text(snapshot.get('janitor_age_sec'))}")
        if not snapshot.get("safety_complete"):
            parts.append("安全性未完整(本轮不处置)")

    parts.append(f"守卫 {snapshot.get('guard_health') or '无状态'} "
                 f"{_age_text(snapshot.get('guard_age_sec'))}")
    return " | ".join(parts)


def junk_is_alarming(snapshot: Mapping[str, Any]) -> bool:
    """Highlight only what the operator has to act on.

    There is deliberately no size threshold here.  The janitor sweeps every 30
    minutes unconditionally, so a large backlog is not itself a fault — it is
    either about to be swept or the janitor has stopped, and "has it stopped" is
    what this reports.  Backlog size is on the row for the reader to see.

    Any of these means sweeping is not happening as configured:
      * the controller could not be read at all (no claim can be made)
      * the last run aborted instead of sweeping
      * the guard tripped, or is not reporting healthy
      * the janitor is paused
      * the mode is not one the janitor recognises
      * a published document is older than its cycle allows
      * the last run could not prove safety, so it disposed nothing
    """

    if not snapshot.get("available"):
        return True
    # An aborted run is caught by safety_complete below as well, but it is named
    # here because it is the condition, not a side effect of one: a config the
    # janitor refuses means every scheduled run ends at validate_config.
    if snapshot.get("phase") == "error" or snapshot.get("error"):
        return True
    if snapshot.get("guard_tripped"):
        return True
    if snapshot.get("guard_health") != "healthy":
        return True
    guard_age = snapshot.get("guard_age_sec")
    if guard_age is None or guard_age > GUARD_FRESH_LIMIT_SEC:
        return True
    if snapshot.get("paused"):
        return True
    if snapshot.get("janitor_mode") not in {"dry", "apply"}:
        return True
    # Staleness is only a fault while the janitor is enabled; the paused case
    # already returned above.
    janitor_age = snapshot.get("janitor_age_sec")
    if janitor_age is None or janitor_age > JANITOR_FRESH_LIMIT_SEC:
        return True
    if not snapshot.get("safety_complete"):
        return True
    return False


# One layout table so no row number is derived twice.  The search prompt and the
# y/N confirm deliberately reuse the transient-message row, so they can never be
# drawn on top of the key legend.
TOP_ROWS = 8      # title, counts, Hook, context, junk, stack, rule, column header
BOTTOM_ROWS = 7   # rule, focus summary, focus keys, message, rule, keys x2


def layout(height: int) -> dict[str, int]:
    floor = TOP_ROWS
    first_row = TOP_ROWS
    visible = max(1, height - TOP_ROWS - BOTTOM_ROWS)
    # Keep the first data row above the focus separator on short terminals.
    focus_rule = max(first_row + visible, height - 7)
    focus = max(focus_rule, height - 6)
    focus_keys = max(focus, height - 5)
    message = max(focus_keys, height - 4)
    keys_rule = max(message, height - 3)
    keys1 = max(keys_rule, height - 2)
    keys2 = max(keys1, height - 1)
    return {
        "title": 0,
        "counts": 1,
        "hook": 2,
        "context": 3,
        "junk": 4,
        # The cmux-stack summary sits directly under the janitor row and carries
        # only the aggregate verdict: janitor DETAIL has exactly one source in
        # this panel (the junk row above and the G page), so this row must not
        # re-derive it.  Two derivations of one fact can disagree the moment they
        # drift, which is the 106.4.3 failure mode.
        "stack": 5,
        "top_rule": 6,
        "header": 7,
        "first_row": first_row,
        "visible": visible,
        "focus_rule": focus_rule,
        "focus": focus,
        "focus_keys": focus_keys,
        "message": message,
        "keys_rule": keys_rule,
        "keys1": keys1,
        "keys2": keys2,
    }


_ATTRS: dict[str, int] = {}


def init_colors() -> None:
    """Colour carries the layering; fall back to bold/dim when unavailable."""
    _ATTRS.clear()
    _ATTRS.update(dict.fromkeys(
        ("watching", "pool", "paused", "untracked", "error", "group", "title", "dim", "rule"), 0))
    _ATTRS["untracked"] = curses.A_DIM
    _ATTRS["dim"] = curses.A_DIM
    _ATTRS["title"] = curses.A_BOLD
    _ATTRS["group"] = curses.A_BOLD
    if not curses.has_colors():
        return
    with contextlib.suppress(curses.error):
        curses.start_color()
    background = -1
    try:
        curses.use_default_colors()  # keep the terminal's own background
    except curses.error:
        background = curses.COLOR_BLACK
    spec = (
        ("watching", curses.COLOR_GREEN, 0),
        ("pool", curses.COLOR_CYAN, 0),
        ("paused", curses.COLOR_RED, 0),
        ("error", curses.COLOR_YELLOW, 0),
        ("group", curses.COLOR_MAGENTA, curses.A_BOLD),
        ("title", curses.COLOR_MAGENTA, curses.A_BOLD),
        ("rule", curses.COLOR_BLUE, 0),
    )
    for index, (key, foreground, extra) in enumerate(spec, start=1):
        with contextlib.suppress(curses.error):
            curses.init_pair(index, foreground, background)
            _ATTRS[key] = curses.color_pair(index) | extra


def attr(key: str) -> int:
    return _ATTRS.get(key, 0)


def row_attr(candidate: Candidate) -> int:
    return {
        "watching": attr("watching"),
        "pool": attr("pool"),
        "paused": attr("paused"),
        "excluded": attr("paused"),
        "untracked": attr("untracked"),
    }.get(watch_kind(candidate), 0)


def _safe_addnstr(stdscr: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """Draw one line, truncated by terminal columns.

    ``curses.addnstr`` limits by code points, so a 91-character CJK footer was
    happily written as 135 columns into a 131-column window and wrapped.  Cut by
    display width first, then hand curses a string that already fits.
    """
    if y < 0 or width <= 0:
        return
    text = clip_to_width(text, width)
    with contextlib.suppress(curses.error):
        stdscr.addnstr(y, x, text, len(text) + 1, attr)


def _confirm(stdscr: Any, prompt: str) -> bool:
    height, width = stdscr.getmaxyx()
    row = layout(height)["message"]
    _safe_addnstr(stdscr, row, 0, " " * max(1, width - 1), max(1, width - 1))
    _safe_addnstr(stdscr, row, 0, f"{prompt} [y/N] ", max(1, width - 1), curses.A_REVERSE)
    stdscr.refresh()
    stdscr.timeout(-1)
    try:
        while True:
            key = stdscr.getch()
            if key in (curses.ERR, curses.KEY_RESIZE):
                continue
            return key in (ord("y"), ord("Y"))
    finally:
        stdscr.timeout(500)


def _text_prompt(stdscr: Any, prompt: str, initial: str = "") -> str | None:
    """Read a line, accepting multi-byte input.

    ``getch`` yields one byte at a time, so a UTF-8 character arrives as
    several bytes and ``chr(key)`` turns it into mojibake.  Workspace titles
    here are mostly Chinese, so searching by name needs ``get_wch``.
    """
    height, width = stdscr.getmaxyx()
    # Reuse the transient-message row so the prompt never covers the key legend.
    y = layout(height)["message"]
    value = initial
    curses.curs_set(1)
    stdscr.timeout(-1)
    try:
        while True:
            _safe_addnstr(stdscr, y, 0, " " * max(1, width - 1), max(1, width - 1))
            _safe_addnstr(stdscr, y, 0, f"{prompt}{value}", max(1, width - 1), curses.A_BOLD)
            cursor_column = display_width(prompt) + display_width(value)
            stdscr.move(y, min(width - 2, cursor_column))
            stdscr.refresh()
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except AttributeError:  # pragma: no cover - very old curses
                key = stdscr.getch()
            if isinstance(key, str):
                if key in ("\n", "\r"):
                    return value.strip()
                if key == "\x1b":
                    return None
                if key in ("\x7f", "\b"):
                    value = value[:-1]
                elif key.isprintable():
                    value += key
                continue
            if key in (10, 13, curses.KEY_ENTER):
                return value.strip()
            if key == 27:
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
    finally:
        curses.curs_set(0)
        stdscr.timeout(500)


def row_focus_summary(row: ViewRow | None) -> str:
    if row is None:
        return "当前筛选没有行"
    if row.kind == "group":
        parts = [row.workspace_ref]
        if row.workspace_title:
            parts.append(clip_to_width(row.workspace_title, 30))
        parts.append("整池授权" if row.counts.get("pool") else "无整池授权")
        parts.append(f"共 {row.counts.get('all', 0)} 路")
        return "  |  ".join(parts)
    return focus_summary(row.candidate, row.workspace_ref)


def row_action_hint(row: ViewRow | None) -> str:
    if row is None:
        return "/ 搜索   f 切换筛选"
    if row.kind == "group":
        fold = "Tab 展开" if row.collapsed else "Tab 折叠"
        if row.counts.get("pool"):
            return f"u 取消整个 {row.workspace_ref} 授权   {fold}"
        return f"w 授权整个 {row.workspace_ref}（以后新开的 Codex 也会跟）   {fold}"
    return selected_action_hint(row.candidate)


def window_start(index: int, total: int, visible: int) -> int:
    """First row to draw: a sliding window that always fills the screen.

    Block paging (``index // visible * visible``) leaves the last page nearly
    empty — landing on the second-to-last row showed two rows above seventy
    blank lines.  Centre the cursor instead, then clamp so no blank tail shows.
    """
    if visible <= 0 or total <= visible:
        return 0
    start = index - visible // 2
    return max(0, min(start, total - visible))


def group_action_error(row: ViewRow, action: str) -> str:
    """Why this key does nothing on this workspace header, or "" if it applies.

    Checked before the confirmation box: a prompt that asks you to confirm an
    action which is about to be refused teaches you to distrust the prompt.
    """
    if action not in {"workspace", "untrack_workspace"}:
        return f"这是 {row.workspace_ref} 的组头。整池用 w / u，单路请先按 j 进到组里"
    pooled = bool(row.counts.get("pool"))
    if action == "workspace" and pooled:
        return f"{row.workspace_ref} 已经是整池授权。要取消请按 u"
    if action == "untrack_workspace" and not pooled:
        return f"{row.workspace_ref} 没有整池授权，不用取消。要授权请按 w"
    return ""


def view_row_attr(row: ViewRow) -> int:
    if row.kind == "group":
        return attr("group")
    return row_attr(row.candidate) if row.candidate else 0


GLOBAL_KEYS_1 = "↑↓ jk 移动   Tab 折/展   z 全折起   Z 全展开   [ ] 跳 workspace   / 查找   c 清除"
GLOBAL_KEYS_2 = "f 筛选   R 刷新   G 存储   v 三件套   e 配置   A 开启发   S 全局停发   d 全局停发(只观察)   q 退出"


def _draw(
    stdscr: Any,
    model: SupervisorModel,
    rows: list[ViewRow],
    index: int,
    view: str,
    query: str,
    status: str,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    at = layout(height)
    clip = max(1, width - 1)
    counts = model.counts()
    armed = model.config.get("mode") == "armed" and not model.config.get("global_paused")

    # Line 1: identity and whether anything is actually being rescued.
    head = f"→ 续跑管理   {mode_label(model.config)}"
    if not armed:
        head = f"{head}   ←  不会自动续跑，按 A 开启发"
    tail = "cmux 在线" if model.online else "cmux 离线"
    gap = max(1, clip - display_width(head) - display_width(tail))
    line1 = f"{head}{' ' * gap}{tail}"
    _safe_addnstr(stdscr, at["title"], 0, line1 if armed else pad(line1, clip),
                  clip, attr("title") if armed else curses.A_REVERSE | curses.A_BOLD)

    # Line 2: the counts, ASCII separated so a wide-glyph guess cannot wrap it.
    groups = len({str(row.workspace_id) for row in rows})
    facts = [
        f"{groups} workspace(不含 Dock)",
        f"{counts['watching']} 监控中",
        f"{counts['idling']} 空转",
        f"{counts['paused']} 已暂停",
        f"{counts['untracked']} 未登记",
        f"筛选 {FILTER_LABELS[view]}",
    ]
    if query:
        facts.append(f"查找 {query}")
    _safe_addnstr(stdscr, at["counts"], 0, " | ".join(facts), clip, attr("dim"))
    claude_rows = [item for item in model.candidates if item.agent_kind == "claude"]
    hook_config = getattr(model, "hook_config", {})
    hook_ok = bool(hook_config.get("healthy"))
    healthy = sum(1 for item in claude_rows if item.hook_health == "healthy") if hook_ok else 0
    needs_repair = len(claude_rows) - healthy
    live_sends = sum(item.hook_live_sends for item in claude_rows)
    sla_misses = sum(item.hook_sla_misses for item in claude_rows)
    hook_line = (
        f"Hook配置 {hook_config.get('status', 'unknown')} | "
        f"{healthy}正常 | {needs_repair}需修复 | SLA {sla_misses}/{live_sends}超时 | "
        f"修复 {hook_config.get('repair_budget_used', 0)}/{hook_config.get('repair_budget_max', 5)} | "
        f"漂移24h {hook_config.get('drift_count_24h', 0)} | 备份 {hook_config.get('backup_count', 0)}"
    )
    _safe_addnstr(
        stdscr,
        at["hook"],
        0,
        hook_line,
        clip,
        attr("dim") if hook_ok and not sla_misses else attr("error") | curses.A_BOLD,
    )
    context_high = sum(
        1 for item in claude_rows
        if item.context_status == "warning"
        or (item.context_percent is not None and item.context_percent >= 80)
    )
    context_compacting = sum(1 for item in claude_rows if item.context_status == "compacting")
    context_human = sum(1 for item in claude_rows if item.context_status == "stalled")
    context_waiting = sum(1 for item in claude_rows if item.context_status == "limit_waiting")
    context_line = (
        f"上下文 {context_high}高占用 | {context_compacting}压缩中 | "
        f"{context_waiting}等待压缩 | {context_human}需人工"
    )
    _safe_addnstr(
        stdscr,
        at["context"],
        0,
        context_line,
        clip,
        attr("error") | curses.A_BOLD if context_human else attr("dim"),
    )
    # Whatever the last background ctl read left behind.  No filesystem work
    # happens here: this is a dict copy under a lock.
    junk = model.janitor.snapshot()
    _safe_addnstr(
        stdscr,
        at["junk"],
        0,
        junk_line(junk),
        clip,
        attr("error") | curses.A_BOLD if junk_is_alarming(junk) else attr("dim"),
    )
    # Same contract as the junk row above: a dict copy under a lock, nothing
    # fetched here.  This row deliberately carries only cmux-stack's own
    # `overall` plus a one-word-per-component summary -- it does NOT re-derive
    # any Janitor detail.  The junk row and the G page remain the single source
    # for that, because two independent derivations of the same fact are how a
    # panel starts contradicting the executor it reports on (106.4.3).
    stack = model.stack.snapshot()
    _safe_addnstr(
        stdscr,
        at["stack"],
        0,
        stack_line(stack),
        clip,
        attr("error") | curses.A_BOLD if stack_is_alarming(stack) else attr("dim"),
    )
    _safe_addnstr(stdscr, at["top_rule"], 0, rule("=", clip), clip, attr("rule"))
    # One frame, one decision: the same show_collab drives the header and every
    # member row, so the 协作 column cannot appear on one and not the other.
    # The column only exists while a fresh collaboration marker is live -- when
    # the last task disarms, the layout returns to the historical one and the
    # session column regains its usual width budget.
    collab_by_uuid = model.collab.snapshot()
    show_collab = bool(collab_by_uuid) and collab_column_fits(clip)
    collab_cells = collab_cells_for_rows(rows, collab_by_uuid) if show_collab else {}
    collab_counts = collab_group_counts(collab_by_uuid) if show_collab else {}
    _safe_addnstr(stdscr, at["header"], 0,
                  header_text(clip, "协作" if show_collab else None), clip, attr("dim"))

    visible = at["visible"]
    page = window_start(index, len(rows), visible)
    for row_index, row in enumerate(rows[page:page + visible]):
        actual_index = page + row_index
        selected = actual_index == index
        cursor = ">" if selected else " "
        if row.kind == "group":
            fresh = collab_counts.get(str(row.workspace_id), 0)
            # group_row_text pads to its full width budget, so the annotation
            # must be carved out of the budget, not appended past the clip.
            suffix = f"  协作×{fresh}" if fresh else ""
            body = group_row_text(row, clip - 3 - display_width(suffix))
            label = f"{cursor}  {body}{suffix}"
        else:
            candidate = row.candidate
            assert candidate is not None
            label = _row_text(
                f"{cursor}{'*' if candidate.selected_hint else ' '}   ",
                (
                    watch_label(candidate),
                    location_text(candidate.record, row.workspace_ref),
                    program_label(candidate),
                    hook_label(candidate),
                    context_label(candidate),
                    screen_label(candidate),
                    error_label(candidate),
                    send_label(candidate),
                ),
                str(candidate.record.get("title", "")),
                clip,
                candidate.session_text,
                collab_cells.get(row.key, "") if show_collab else None,
            )
        style = curses.A_REVERSE if selected else view_row_attr(row)
        _safe_addnstr(stdscr, at["first_row"] + row_index, 0, label, clip, style)

    # Everything about the cursor lives below the table, never above it.
    focus = rows[index] if rows else None
    _safe_addnstr(stdscr, at["focus_rule"], 0, rule("-", clip), clip, attr("rule"))
    focus_line = row_focus_summary(focus)
    if focus is not None and focus.candidate is not None:
        note = collab_focus_note(focus.candidate, collab_by_uuid)
        if note:
            focus_line = f"{focus_line}  |  {note}"
    _safe_addnstr(stdscr, at["focus"], 0, focus_line, clip,
                  view_row_attr(focus) if focus else attr("dim"))
    _safe_addnstr(stdscr, at["focus_keys"], 0, f"  {row_action_hint(focus)}", clip)
    if model.error:
        _safe_addnstr(stdscr, at["message"], 0, f"错误: {model.error}", clip, attr("paused") | curses.A_BOLD)
    elif status:
        _safe_addnstr(stdscr, at["message"], 0, status, clip, attr("error"))
    _safe_addnstr(stdscr, at["keys_rule"], 0, rule("-", clip), clip, attr("rule"))
    _safe_addnstr(stdscr, at["keys1"], 0, GLOBAL_KEYS_1, clip, attr("dim"))
    _safe_addnstr(stdscr, at["keys2"], 0, GLOBAL_KEYS_2, clip, attr("dim"))
    stdscr.refresh()


def next_status_after_key(key: int, status: str) -> str:
    """Keep the last footer message across idle timeout redraws."""
    return status if key == curses.ERR else ""


def storage_sweep_prompt(snapshot: Mapping[str, Any]) -> str:
    """The confirmation text for a manual sweep.

    Everything that changes what the sweep will do is stated here, because this
    box is the last point at which the operator can decline: how many candidates
    the last run saw, how many bytes that was and whether that figure is exact,
    the per-run cap, and how long quarantine keeps a batch before deleting it.

    An unmeasured figure prints as ``?``/``未测量``.  Presenting a missing
    measurement as ``0`` in a confirmation box is how somebody approves a sweep
    believing there is nothing to sweep.
    """

    counts = snapshot.get("candidate_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    selected = _count_text(counts.get("selected"))
    eligible = _count_text(counts.get("eligible"))
    size = _bytes_text(snapshot.get("selected_bytes"),
                       str(snapshot.get("selected_precision")))
    limit = snapshot.get("per_run_limit")
    limit_text = "未知" if limit is None else str(limit)
    keep = snapshot.get("quarantine_keep_hours")
    keep_text = "未知" if keep is None else f"{keep}h"
    return (
        f"确认立即手动清扫？上轮合格 {eligible} 项、选中 {selected} 项（{size}）；"
        f"单轮上限 {limit_text} 项；移入隔离区保留 {keep_text} 后才真删。"
        "全套安全闸门照旧生效"
    )


def storage_page_lines(snapshot: Mapping[str, Any], action_message: str = "") -> list[str]:
    """The G page body.  Pure text so it can be asserted without a terminal.

    Reads only the snapshot the worker already fetched; like the summary row it
    performs no I/O of its own.
    """

    if not snapshot.get("available"):
        reason = snapshot.get("reason") or "无法读取"
        lines = [
            f"清扫器状态: 无法读取（{reason}）",
            "",
            "面板只经 cmux-janitorctl 读状态，不直接读清扫器的私有文件，所以控制器",
            "缺失或超时时这里显示「读不到」，而不是「没有垃圾」。",
            "",
            "排查: cmux-janitorctl status --json",
        ]
        if action_message:
            lines += ["", action_message]
        return lines

    counts = snapshot.get("candidate_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    keep = snapshot.get("quarantine_keep_hours")
    keep_text = "?" if keep is None else f"{keep}h"
    limit = snapshot.get("per_run_limit")
    limit_text = "?" if limit is None else str(limit)

    # Not loaded in launchd dominates every other state: the state file keeps
    # its last healthy contents forever, so age, health and mode all still read
    # fine while nothing is scheduled to run again.  On 2026-08-29T06:39:50Z both
    # labels were booted out of launchd and this page kept rendering
    # "运行中，模式 apply" plus "守卫 healthy", because the panel consumed only the
    # ``janitor``/``guard`` blocks and ignored ctl's ``launchd`` block entirely.
    # Same failure shape as the phase=="error" branch below: describing the
    # schedule while hiding that no run can complete.
    if snapshot.get("janitor_loaded") is False:
        state = "未加载到 launchd（不会再自动清扫；需 launchctl bootstrap 重新加载）"
    elif snapshot.get("guard_tripped"):
        state = "守卫跳闸（清扫已停；需人工 guard.sh --status 后 --rearm）"
    elif snapshot.get("paused"):
        state = "已暂停（DISABLED 存在）"
    elif snapshot.get("phase") == "error":
        # The janitor refused its own config and exited before touching
        # anything.  It is still scheduled, so it will keep aborting every
        # cycle; saying "运行中" here would describe the schedule while hiding
        # that no run has completed.
        detail = snapshot.get("error") or "未给出原因"
        state = f"配置被拒，每轮中止（{detail}）"
    else:
        state = f"运行中，模式 {snapshot.get('janitor_mode', 'unknown')}"

    measured = snapshot.get("measured_at")
    measured_text = f"，观测时刻 {measured}" if isinstance(measured, str) and measured else ""

    return_lines = [
        f"清扫器: {state}",
        f"最后测量: {_age_text(snapshot.get('janitor_age_sec'))}{measured_text}",
        f"守卫: {snapshot.get('guard_health') or '无状态'}，"
        f"最后测量 {_age_text(snapshot.get('guard_age_sec'))}",
        # Always shown, not only on failure: "healthy" above describes the last
        # run the guard managed to make, and says nothing about whether it is
        # still scheduled to make another one.
        f"调度: 清扫器 {_loaded_text(snapshot.get('janitor_loaded'))}，"
        f"守卫 {_loaded_text(snapshot.get('guard_loaded'))}",
        "",
        "候选（仍在实时目录里；三段口径来自同一次测量）:",
        f"  原始扫到       {_count_text(counts.get('raw'))}",
        f"  通过年龄/引用   {_count_text(counts.get('eligible'))}",
        f"  本轮选中       {_count_text(counts.get('selected'))}（单轮上限 {limit_text}）",
        f"  受保护跳过     {_count_text(counts.get('protected'))}",
        f"  待清体积       "
        f"{_bytes_text(snapshot.get('selected_bytes'), str(snapshot.get('selected_precision')))}",
        "",
        "隔离区（已移出实时目录；到期由清扫器自行删除）:",
        f"  批次 {_count_text(snapshot.get('quarantine_count'))}，"
        f"体积 {_bytes_text(snapshot.get('quarantine_bytes'), str(snapshot.get('quarantine_precision')))}，"
        f"保留 {keep_text}",
        "",
        f"上轮安全性完整: "
        f"{'是' if snapshot.get('safety_complete') else '否（该轮不处置任何文件）'}",
        "",
        "候选与隔离分开统计，从不相加: 前者还在实时目录里，后者已被处理掉。",
        "相加会让清扫器越勤快、数字越大。",
    ]
    if action_message:
        return_lines += ["", action_message]
    return return_lines


# The v page binds exactly one action key: ``e`` hands the terminal to ccp-new.
#
# The earlier revision of this panel had no action key at all and printed every
# command as copyable text.  That was the right call for the launchd verbs and
# the wrong call for ccp-new, and the difference is worth stating because the
# first version got it wrong by applying one rule to both:
#
#   * ``up --apply`` / ``down --apply`` / ``install --apply`` reach across three
#     trust domains -- one keystroke could bootstrap or bootout the watcher, the
#     janitor and the guard.  Those stay text.
#   * ccp-new edits ~/.claude-profiles/*.json and nothing else.  It keeps its own
#     ``.bak`` and ``_trash`` rollback, prompts before overwriting, and cannot
#     touch launchd.  Making the user read a command, quit the panel and retype
#     it bought no safety and cost four steps for a one-step job.
#
# So the containment that matters is per-verb, not per-page.
STACK_KEYS = "e 进 ccp-new   r 重读状态   Esc/v/q 返回"

# Shown verbatim on the page so the user can copy a line instead of being given
# a key that acts.  These are strings, never executed from the panel.
STACK_WRITE_HINTS = (
    "cmux-stack up              # 干跑：只列出会做什么",
    "cmux-stack up --apply      # 落地：bootstrap 缺失的 LaunchAgent",
    "cmux-stack down --component <名> --apply   # 停单个组件（无全量停机）",
    "cmux-stack install         # 干跑：PATH 安装计划",
    "cmux-stack install --apply # 落地：建 /opt/homebrew/bin/cmux-stack 链接",
)

# rc -> what to put on the footer.  ccp-new's own exit codes, kept as its
# contract rather than reinterpreted here: 0 ok / 1 upstream verify failed /
# 2 usage / 130 cancelled.  ``None`` means the child never started.
PROFILE_STATUS = {
    0: "ccp-new 已完成",
    1: "ccp-new: 上游验证未通过（GET /v1/models 失败）",
    2: "ccp-new: 输入错误",
    130: "ccp-new 已取消",
}


def profile_status_text(rc: int | None) -> str:
    """Describe a finished ccp-new run without inventing detail.

    An unknown rc is reported as itself.  Collapsing it into "失败" would hide
    which contract the child broke, and collapsing it into "已完成" would claim
    success we did not observe.
    """

    if rc is None:
        return "无法启动 ccp-new"
    known = PROFILE_STATUS.get(rc)
    return known if known is not None else f"ccp-new 退出码 {rc}"


def profile_argv() -> list[str]:
    """The one place that knows how ccp-new is launched.

    Routing through ``cmux-stack profile`` rather than calling ccp-new directly
    keeps a single implementation of "which interpreter, which path, how are
    passthrough args stripped".  The controller already reports its own absence
    with a real exit code, so a missing ccp-new surfaces as a footer message
    instead of a traceback.
    """

    return [sys.executable, str(STACK_CTL), "profile"]


def _handoff(stdscr: Any, argv: list[str]) -> int | None:
    """Give the whole terminal to a foreground program, then take it back.

    This is the one place in the panel that runs a subprocess on the draw
    thread, and that is the design rather than an oversight.  The worker-thread
    rule exists because *automatic polling* must never stall a redraw; here the
    user has asked to leave, so standing still is the correct behaviour.

    Three properties are load-bearing and each has a test:

    * ``endwin`` before the child, ``reset_prog_mode`` after it.  Without the
      suspend, curses still owns the terminal and the child's prompts land on a
      screen that is about to be overwritten.
    * no capture of any kind.  ``capture_output``/``stdout``/``stderr`` would
      break ``getpass`` (it needs the tty to disable echo) and ``$EDITOR``
      (it needs the tty to draw at all) in the same stroke.  ccp-new's own
      docstring calls piping it "forbidden by design".
    * restore inside ``finally``.  A child that crashes, an ``OSError`` from a
      missing controller, or a Ctrl-C must still hand back a usable terminal.
      This was verified by an accident rather than a fixture: a NameError in an
      early probe killed the call before the child existed, and the restore
      still ran.

    Ctrl-C reaches the whole foreground process group, so the parent has to
    absorb it; letting it propagate would take the panel down with the child.
    """

    curses.def_prog_mode()
    curses.endwin()
    rc: int | None = None
    try:
        rc = subprocess.call(argv)
    except KeyboardInterrupt:
        rc = 130
    except OSError:
        rc = None
    finally:
        curses.reset_prog_mode()
        stdscr.clearok(True)
        stdscr.refresh()
    return rc


def _run_profile(stdscr: Any, model: SupervisorModel) -> str:
    """Hand over to ccp-new and refresh the stack row on the way back.

    The forced refresh is what makes the row honest: a profile the user just
    repaired should not keep reading 异常 for up to STACK_CACHE_TTL_SEC because
    the cache has not expired yet.
    """

    rc = _handoff(stdscr, profile_argv())
    model.stack.maybe_refresh(force=True)
    return profile_status_text(rc)


def stack_page_lines(snapshot: Mapping[str, Any]) -> list[str]:
    """The v page body.  Pure text so it can be asserted without a terminal.

    Reads only the snapshot the worker already fetched; performs no I/O of its
    own, exactly like storage_page_lines().
    """

    if not snapshot.get("available"):
        reason = snapshot.get("reason") or "无法读取"
        lines = [
            f"cmux-stack: 无法读取（{reason}）",
            "",
            "本页只经 cmux-stack status --json 读状态，不自己探测三个组件，所以控制器",
            "缺失或超时时这里显示「读不到」，而不是「一切正常」。",
            "",
            "排查: cmux-stack status --json",
        ]
        return lines

    components = snapshot.get("components")
    components = components if isinstance(components, Mapping) else {}
    probed = snapshot.get("probed_count")
    requested = snapshot.get("requested_count")

    lines = [
        f"总体: {_stack_overall_text(snapshot.get('overall'))}"
        f"（已探测 {_count_text(probed)}/{_count_text(requested)}）",
    ]
    unhealthy = snapshot.get("unhealthy") or []
    unknown = snapshot.get("unknown") or []
    if unhealthy:
        lines.append(f"不健康: {'、'.join(unhealthy)}")
    if unknown:
        lines.append(f"未知（判不出，按 fail closed 处理）: {'、'.join(unknown)}")
    lines.append("")

    for name, label in (("watcher", "续跑守卫器"), ("janitor", "清扫器"),
                        ("profiles", "配置管理")):
        row = components.get(name)
        if not isinstance(row, Mapping):
            lines.append(f"{label}: 未投影")
            continue
        lines.append(f"{label} ({name}): {_stack_verdict_text(row)}")
        if not row.get("installed"):
            lines.append("    未安装")
        if name == "profiles":
            # ccp-new is interactive by design and has no LaunchAgent, so there
            # is no schedule line to show for it.
            lines.append(f"    profile 数 {_count_text(row.get('profile_count'))}，"
                         f"不健康 {_count_text(row.get('unhealthy_count'))}")
            lines.append("    ccp-new 永远前台交互，无 LaunchAgent（设计要求）")
        else:
            lines.append(f"    调度: {_loaded_text(row.get('launchd_loaded'))}")
        if name == "watcher":
            pid = row.get("pid")
            lines.append(f"    PID {pid if pid else '无'}，"
                         f"{'存活' if row.get('pid_alive') else '不存活'}，"
                         f"模式 {row.get('mode') or '未知'}")
            if row.get("source_matches_disk") is False:
                lines.append("    磁盘源与运行源不同：重启会改变行为（是否重启属人工决策）")
        if name == "janitor":
            lines.append(f"    守卫调度 {_loaded_text(row.get('guard_launchd_loaded'))}，"
                         f"守卫 {row.get('guard_health') or '无状态'}")
            # One source for janitor detail, and it is not this page.
            lines.append("    清扫器细节（候选/隔离/体积/阈值）见 G 页，本页只做汇总")
        reason = row.get("reason")
        if reason:
            lines.append(f"    原因: {reason}")
        lines.append("")

    lines.append("写操作不在本页执行。请复制到终端运行：")
    lines.extend(f"  {hint}" for hint in STACK_WRITE_HINTS)
    return lines


def _stack_page(stdscr: Any, model: SupervisorModel) -> str:
    """The v page's own loop.  Returns the status line for the main screen.

    Its keys are ``e`` (hand the terminal to ccp-new) and ``r`` (reread).  The
    launchd verbs stay as copyable text -- see STACK_KEYS for why the split is
    per-verb.  Like the G page it keeps redrawing while the worker fetches, so a
    slow controller delays the numbers rather than the interface.
    """

    client = model.stack
    client.maybe_refresh(force=True)
    while True:
        height, width = stdscr.getmaxyx()
        clip = max(1, width - 1)
        snapshot = client.snapshot()
        stdscr.erase()
        _safe_addnstr(stdscr, 0, 0, "cmux-stack / 三组件汇总（只读）", clip, attr("title"))
        _safe_addnstr(stdscr, 1, 0, rule("=", clip), clip, attr("rule"))
        body = stack_page_lines(snapshot)
        for offset, line in enumerate(body):
            row = 2 + offset
            if row >= height - 2:
                break
            _safe_addnstr(stdscr, row, 0, line, clip, attr("dim"))
        _safe_addnstr(stdscr, max(2, height - 2), 0, rule("-", clip), clip, attr("rule"))
        _safe_addnstr(stdscr, max(3, height - 1), 0, STACK_KEYS, clip, attr("dim"))
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.ERR, curses.KEY_RESIZE):
            client.maybe_refresh()
            continue
        if key in (27, ord("v"), ord("V"), ord("q"), ord("Q")):
            return ""
        if key in (ord("e"), ord("E")):
            # Returning ends the page: ccp-new may have changed what this very
            # page is describing, so the main screen redraws against the forced
            # refresh instead of leaving a stale body on screen.
            return _run_profile(stdscr, model)
        if key == ord("r"):
            client.maybe_refresh(force=True)
            continue


STORAGE_KEYS = "c 手动清扫   p 暂停/恢复   r 重读状态   Esc/G 返回"


def _storage_page(stdscr: Any, model: SupervisorModel) -> str:
    """The G page's own loop.  Returns the status line for the main screen.

    Its keys are local: ``p`` here pauses the *janitor*, while ``p`` on the main
    screen pauses a *surface*.  Keeping them local is why the main screen's
    ``q``/``Q``/``p``/``c`` meanings are untouched.

    Every action is started on a worker thread and this loop keeps redrawing, so
    a sweep that spends minutes in lsof does not freeze the interface.
    """

    client = model.janitor
    client.maybe_refresh(force=True)
    while True:
        height, width = stdscr.getmaxyx()
        clip = max(1, width - 1)
        snapshot = client.snapshot()
        phase, message = client.action_state()
        stdscr.erase()
        _safe_addnstr(stdscr, 0, 0, "cmux 存储 / 清扫器", clip, attr("title"))
        _safe_addnstr(stdscr, 1, 0, rule("=", clip), clip, attr("rule"))
        body = storage_page_lines(snapshot, message)
        # The action message is always appended last, so its row is known by
        # position rather than by comparing string contents.
        message_row = len(body) - 1 if message else -1
        for offset, line in enumerate(body):
            row = 2 + offset
            if row >= height - 2:
                break
            emphasis = attr("dim")
            if offset == message_row:
                if phase == "running":
                    emphasis = attr("title")
                elif phase in {"refused", "error"}:
                    emphasis = attr("paused") | curses.A_BOLD
            _safe_addnstr(stdscr, row, 0, line, clip, emphasis)
        _safe_addnstr(stdscr, max(2, height - 2), 0, rule("-", clip), clip, attr("rule"))
        _safe_addnstr(stdscr, max(3, height - 1), 0, STORAGE_KEYS, clip, attr("dim"))
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.ERR, curses.KEY_RESIZE):
            # The 500ms timeout expiring is what keeps a running action's
            # progress visible; it is not an event.
            client.maybe_refresh()
            continue
        if key in (27, ord("G"), ord("q"), ord("Q")):
            client.clear_action()
            return ""
        if key == ord("r"):
            client.clear_action()
            client.maybe_refresh(force=True)
            continue
        if key == ord("p"):
            paused = snapshot.get("paused")
            if paused is None:
                client.clear_action()
                continue
            command = "resume" if paused else "pause"
            prompt = ("确认恢复清扫器？恢复后仍需守卫健康、MODE 与基线一致才会真正开工"
                      if paused else
                      "确认暂停清扫器？暂停后不再清理，垃圾会继续累积")
            if not _confirm(stdscr, prompt):
                continue
            client.start_action(command)
            continue
        if key == ord("c"):
            if not _confirm(stdscr, storage_sweep_prompt(snapshot)):
                continue
            client.start_action("run")
            continue

def _run(stdscr: Any, model: SupervisorModel) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(500)
    init_colors()
    model.refresh(force=True)
    view = DEFAULT_FILTER
    query = ""
    status = ""
    # Fold pools with nothing under watch.  Recomputed every pass against the
    # live workspace list so newly created ones follow the same rule; anything
    # the user folded or unfolded by hand is remembered in `manual` and left
    # alone.
    manual: set[str] = set()
    collapsed = default_collapsed(model.candidates, model.suggested_surface)
    seen_surfaces = surface_ids(model.candidates)
    cursor_key = ""
    while True:
        try:
            model.refresh()
        except Exception as exc:  # keep the management surface alive
            model.error = str(exc)
        collapsed = reconcile_collapsed(collapsed, manual, model.candidates, model.suggested_surface)
        manual &= {str(item.record.get("workspace_id") or "") for item in model.candidates}
        rows = build_view_rows(model.candidates, collapsed, view, query)
        if not cursor_key:
            cursor_key = initial_cursor_key(rows, model.suggested_surface)
        # Track by identity, not by position: collapsing, filtering and the
        # 5-second refresh all reshuffle the list.
        index = index_for_key(rows, cursor_key, model.suggested_surface)
        if rows:
            cursor_key = rows[index].key
        _draw(stdscr, model, rows, index, view, query, status)
        key = stdscr.getch()
        status = next_status_after_key(key, status)
        if key in (ord("q"), ord("Q")):
            return
        if key == ord("G"):
            # The storage page owns its own keys while it is open, which is why
            # p/c/r there act on the janitor without changing what they mean
            # here.  It returns the footer message for this screen.
            status = _storage_page(stdscr, model)
            continue
        if key in (ord("e"), ord("E")):
            # The whole point of the task: one keystroke from the main screen to
            # ccp-new's full menu, in this terminal.  Deliberately not routed
            # through the v page -- making the user open a panel first would put
            # the step back that this key exists to remove.
            status = _run_profile(stdscr, model)
            continue
        if key in (ord("v"), ord("V")):
            # Same containment as G: the stack page owns its keys while open, so
            # r there rereads the stack without touching what R means here.  Both
            # cases were checked against every letter the main screen and the G
            # page already bind -- v and V were the only unbound pair left.
            status = _stack_page(stdscr, model)
            continue
        if key == ord("f"):
            view = next_filter(view)
            status = f"筛选改为{FILTER_LABELS[view]}"
            continue
        if key == ord("/"):
            value = _text_prompt(stdscr, "查找 workspace/surface/标题: ", query)
            if value is not None:
                query = value
                status = f"当前查找: {query}（命中的 workspace 已自动展开）" if query else "已显示全部候选"
            continue
        if key in (ord("c"), ord("C")):
            query = ""
            status = "已清除查找"
            continue
        if key in (curses.KEY_UP, ord("k")) and rows:
            cursor_key = rows[max(0, index - 1)].key
            continue
        if key in (curses.KEY_DOWN, ord("j")) and rows:
            cursor_key = rows[min(len(rows) - 1, index + 1)].key
            continue
        if key in (9, curses.KEY_BTAB) and rows:  # Tab folds the current pool
            workspace_id = rows[index].workspace_id
            manual.add(workspace_id)
            if workspace_id in collapsed:
                collapsed.discard(workspace_id)
                status = f"已展开 {rows[index].workspace_ref}"
            else:
                collapsed.add(workspace_id)
                cursor_key = f"w:{workspace_id}"
                status = f"已折叠 {rows[index].workspace_ref}"
            continue
        if key in (ord("z"), ord("Z")) and rows:
            every = {str(item.record.get("workspace_id") or "") for item in model.candidates}
            manual |= every
            if key == ord("z"):
                collapsed |= every
                cursor_key = f"w:{rows[index].workspace_id}"
                status = f"已折起全部 {len(every)} 个 workspace（Z 全部展开）"
            else:
                collapsed.clear()
                status = f"已展开全部 {len(every)} 个 workspace（z 全部折起）"
            continue
        if key in (ord("["), ord("]")) and rows:
            heads = [position for position, row in enumerate(rows) if row.kind == "group"]
            if heads:
                if key == ord("["):
                    target = max([p for p in heads if p < index], default=heads[-1])
                else:
                    target = min([p for p in heads if p > index], default=heads[0])
                cursor_key = rows[target].key
            continue
        if key == ord("R"):
            model.refresh(force=True)
            current = surface_ids(model.candidates)
            groups = len({str(item.record.get("workspace_id") or "") for item in model.candidates})
            status = refresh_report(seen_surfaces, current, groups)
            seen_surfaces = current
            continue
        if key in (ord("A"), ord("S"), ord("d")):
            action = {ord("A"): "arm", ord("S"): "stop-all", ord("d"): "dry-run"}[key]
            dummy = Candidate({"ref": "", "workspace_ref": ""}, "explicit", "", "", 0, False)
            # dry-run silently stops every target from being rescued, so it
            # needs the same confirmation as stop-all.
            if not _confirm(stdscr, confirm_prompt(action, dummy)):
                status = "已取消"
                continue
            try:
                model.set_mode(action)
                status = {"arm": "已开始真实发送", "stop-all": "已全局停发", "dry-run": "已改为只观察"}[action]
                model.refresh(force=True)
            except Exception as exc:
                status = f"失败: {exc}"
            continue
        if not rows or key not in (ord("a"), ord("w"), ord("p"), ord("r"), ord("x"), ord("u")):
            continue
        action = {
            ord("a"): "add",
            ord("w"): "workspace",
            ord("p"): "pause",
            ord("r"): "resume",
            ord("x"): "remove",
            ord("u"): "untrack_workspace",
        }[key]
        row = rows[index]
        if row.kind == "group":
            refusal = group_action_error(row, action)
            if refusal:
                status = refusal
                continue
            live = row.counts.get("all", 0) or None
            if not _confirm(stdscr, workspace_confirm_prompt(row, action, live_codex=live)):
                status = "已取消"
                continue
            try:
                model.mutate_workspace(row, action)
                status = (f"已授权整个 {row.workspace_ref}" if action == "workspace"
                          else f"已取消整个 {row.workspace_ref} 授权")
                model.refresh(force=True)
            except Exception as exc:
                status = f"失败: {exc}"
            continue
        candidate = row.candidate
        assert candidate is not None
        if action == "add" and candidate.source != "untracked":
            status = "已监控目标不用再按 a；按 / 搜索未登记行，或按 f 切到「只看未登记」"
            continue
        if action in {"pause", "resume"} and candidate.source in {"untracked", "workspace_non_codex"}:
            status = "未登记候选不能暂停/恢复；先按 a 加单路，或按 w 授权其 workspace"
            continue
        if action == "remove" and candidate.source != "explicit":
            if candidate.source == "untracked":
                status = "未登记目标不用删。要监控请按 a，或按 w 授权整个 workspace"
            else:
                status = "整池目标不能用 x 删除；按 p 排除这一路，或按 u 取消整个 workspace"
            continue
        if action == "untrack_workspace" and candidate.source not in {"workspace_rule", "workspace_excluded", "workspace_non_codex"}:
            status = "只有整池行才能按 u。单路请用 x，未登记不用取消"
            continue
        live_codex = None
        if action == "workspace":
            workspace_id = str(candidate.record.get("workspace_id") or "")
            live_codex = sum(
                1 for item in model.candidates
                if item.agent_kind == "codex"
                and str(item.record.get("workspace_id") or "") == workspace_id
            )
        if action in {"pause", "remove", "add", "workspace", "untrack_workspace"} and not _confirm(
            stdscr, confirm_prompt(action, candidate, live_codex=live_codex)
        ):
            status = "已取消"
            continue
        try:
            model.mutate_selected(candidate, action)
            where = f"{candidate.workspace_ref}/{candidate.ref}"
            status = {
                "add": f"已登记 {where}",
                "workspace": f"已授权整个 {candidate.workspace_ref}",
                "pause": f"已暂停 {where}" if candidate.source == "explicit" else f"已排除 {where}",
                "resume": f"已恢复 {where}",
                "remove": f"已删除 {where} 的单路登记",
                "untrack_workspace": f"已取消整个 {candidate.workspace_ref} 授权",
            }.get(action, f"已处理 {where}")
            model.refresh(force=True)
        except Exception as exc:
            status = f"失败: {exc}"


def run_tui(config_path: Path, suggested_surface: str = "") -> int:
    # curses needs the locale set before it can decode wide characters, which
    # the search box relies on for Chinese workspace titles.
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "")
    # Dock terminal tabs do not currently accept rename-tab in cmux 0.64.
    # OSC 0 gives the management surface a stable, user-visible title.
    sys.stdout.write("\x1b]0;Supervisor\x07")
    sys.stdout.flush()
    model = SupervisorModel(config_path, suggested_surface)
    curses.wrapper(_run, model)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tui(core.DEFAULT_CONFIG_PATH))
