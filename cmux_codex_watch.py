#!/usr/bin/env python3
"""Opt-in cmux/Codex continuation watchdog.

The daemon intentionally fails closed. It only sends to UUIDs explicitly
registered by ``add`` or discovered inside an ``add-workspace`` rule, and only
after a visible Codex error, an empty composer, and all UI guards are verified.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import errno
import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from claude_ccc_protocol import EVENT_JOURNAL_PATH, EVENT_SOCKET_PATH


APP_NAME = "cmux-codex-continue"
WATCHER_LABEL = os.environ.get("CMUX_WATCHER_LABEL") or "com.example.ccc-continue"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_APP_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / APP_NAME
DEFAULT_CONFIG_PATH = DEFAULT_APP_DIR / "config.json"
DEFAULT_STATE_PATH = DEFAULT_APP_DIR / "state.json"
DEFAULT_LOCK_PATH = DEFAULT_APP_DIR / "daemon.lock"
DEFAULT_CONFIG_LOCK_PATH = DEFAULT_APP_DIR / "config.lock"
DEFAULT_CLAUDE_EVENT_LEDGER_PATH = DEFAULT_APP_DIR / "claude-event-ledger.json"
DEFAULT_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
DEFAULT_CLAUDE_SETTINGS_LOCK_PATH = Path.home() / ".claude" / "settings.json.ccc.lock"
DEFAULT_CLAUDE_SETTINGS_BACKUP_DIR = DEFAULT_APP_DIR / "settings-backups"
DEFAULT_CLAUDE_HOOK_REPAIR_STATE_PATH = DEFAULT_APP_DIR / "claude-hook-repair-state.json"
# Log-channel incidents live in the app dir, never in the log dir: the whole
# point is to survive the log directory being deleted.
DEFAULT_LOG_CHANNEL_INCIDENT_PATH = DEFAULT_APP_DIR / "log-channel-incidents.jsonl"
# Runtime identity of the *running* daemon.  Deliberately a separate file:
# state.json treats every top-level key as a surface runtime, so daemon
# metadata stored there would materialise as a phantom target.
DEFAULT_DAEMON_RUNTIME_PATH = DEFAULT_APP_DIR / "daemon-runtime.json"
DEFAULT_DOCK_CONFIG_PATH = Path.home() / ".config" / "cmux" / "dock.json"
DEFAULT_PLIST_PATH = Path(os.environ.get("CMUX_WATCHER_PLIST") or
                          (Path.home() / "Library" / "LaunchAgents" /
                           f"{WATCHER_LABEL}.plist"))
DEFAULT_CLI_LINK = Path("/opt/homebrew/bin/cmux-codex-continue")
DEFAULT_SHORT_CLI_LINK = Path("/opt/homebrew/bin/ccc")
DEFAULT_PYTHON = "/opt/homebrew/bin/python3"
DEFAULT_CMUX = "/opt/homebrew/bin/cmux"
MESSAGE = "任务请继续"
# Claude-only.  Codex keeps MESSAGE.  The wording is the completion detector:
# a normal finish must end with the report phrase; anything else may be a stall.
CLAUDE_MESSAGE = (
    "任务中断了么？如果是就请继续，如果任务完成了务必在最后一句向我报告 "
    "‘ 完成，建议检查 usage: /context’ 。如果任务没有中断就请继续，不要影响你的进度"
)
REQUIRED_CAPABILITIES = {"terminal.render_grid.v1", "terminal.replay.v1"}
REQUIRED_METHODS = {"surface.read_text", "surface.send_text", "terminal.replay"}
GRID_FORMAT = "cmux.render-grid.v1"
SUPPORTED_CONFIG_SCHEMA_VERSIONS = {1, 2}

HIGH_DEMAND = "We're currently experiencing high demand, which may cause temporary errors."
FOOTER_RE = re.compile(
    r"^(?:gpt[-\w. ]+|context\b|fast\s+(?:on|off)\b|plan mode$|\S+% used\b|\d+[kKmM] window$|~/.+)$",
    re.IGNORECASE,
)
ERROR_TAIL_RE = re.compile(
    r"(?:request id|https?://|error sending request|status\s*[:=]|\burl\b|^\s*at\s+)",
    re.IGNORECASE,
)
MENU_PATTERNS = (
    "Implement this plan?",
    "1. Yes, implement this plan",
    "2. Yes, clear context and implement",
    "3. No, stay in Plan mode",
    "Keep waiting",
)
PERMISSION_RE = re.compile(
    r"(?:do you want to|would you like to|allow (?:command|once|always)|permission required|press enter to confirm)",
    re.IGNORECASE,
)
WORKING_RE = re.compile(r"(?:esc to interrupt|esc to cancel|\bworking\s*\()", re.IGNORECASE)
# Codex's own banner for input it accepted but has not consumed yet.
QUEUED_FOLLOWUP_RE = re.compile(r"queued\s+follow-?up\s+input", re.IGNORECASE)
# Minimum seconds between continuation attempts while the current viewport is
# still a recoverable error.  This is not a blind heartbeat: Working, menus,
# queued input, user input and newer output all stop the send path first.
REPEAT_SEND_DELAY_SEC = 1.0
# Working must last this many polls (~1s each) before a watchdog prompt is
# treated as consumed.  1–2 frame flickers keep the same stop event pending.
CLAUDE_WORKING_CLEAR_POLLS = 3
# A stop candidate is deliberately not sent from the frame that discovered it.
# The active surface gets a longer grace because that is where the user is most
# likely to begin typing.  The final replay immediately before send is still the
# authoritative gate.
CLAUDE_BACKGROUND_INPUT_GRACE_SEC = 1.0
CLAUDE_FOCUSED_INPUT_GRACE_SEC = 3.0
CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC = 0.12
CLAUDE_EVENT_MAX_AGE_SEC = 3600.0
CLAUDE_EVENT_LEDGER_LIMIT = 4096
CLAUDE_EVENT_WORKERS = 4
CLAUDE_REPEAT_WARNING_AFTER = 5
CLAUDE_HOOK_UNVERIFIED_GRACE_SEC = 30.0
CLAUDE_HOOK_SLA_SEC = 1.0
# A surface whose Hook generation is not trustworthy is fail-closed: ccc will
# never send to it.  That is correct, but it used to be reported as a flat label,
# so surface:74 sat unprotected for ~14.5h during the 2026-08-24 observation
# without a single line saying so.  These thresholds escalate the *duration*.
# How long a raw viewport may stay unreadable before we say so out loud.  During
# the 2026-08-24 observation surface:72 was `incompatible` for the entire 5.65h
# window and the count of blind panes rose 1 -> 3, with no log line ever
# mentioning it: an unreadable pane also reports context "unknown", which the
# gate deliberately fails open on, so blindness was invisible *and* permissive.
CLAUDE_UNREADABLE_WARN_SEC = 1800.0
CLAUDE_HOOK_UNPROTECTED_WARN_SEC = 3600.0
CLAUDE_HOOK_UNPROTECTED_SEVERE_SEC = 14400.0
CLAUDE_HOOK_UNPROTECTED_CRITICAL_SEC = 43200.0
# How often the daemon re-checks that its own log directory still exists.  A
# deleted log dir leaves the file handler writing to an orphan inode: sends keep
# working, but every path-based reader (``ccc logs``, tail, observers) silently
# sees nothing.  Failing loudly beats a silent EXIT=0.
LOG_CHANNEL_CHECK_INTERVAL_SEC = 60.0
CLAUDE_HOOK_LATENCY_WINDOW = 64
CLAUDE_HOOK_GAP_CONFIRM_POLLS = 2
# A synthetic gap rescue whose Enter confirmation timed out may leave the
# same stopped fingerprint on screen even though Claude never consumed the
# prompt.  Permit one fresh two-frame candidate after this quiet period; this
# is deliberately far above the 1s repeat gate so an unconfirmed send cannot
# become a storm.
CLAUDE_HOOK_GAP_RETRY_AFTER_SEC = 120.0
# A missing Hook must not turn the same stopped frame into an unbounded stream
# of synthetic submissions.  The initial rescue plus three bounded retries is
# enough to cover a transient confirmation race; after that we keep monitoring
# and expose the unresolved condition for human intervention.  A real Hook,
# human prompt, new process generation, or new content fingerprint resets it.
CLAUDE_HOOK_GAP_MAX_RETRIES = 3
# Claim results are deliberately plain strings rather than booleans.  A bool
# cannot tell a duplicate delivery from an event-id collision with an older
# episode; the latter must get a fresh id instead of being silently swallowed.
CLAUDE_CLAIMED = "claimed"
CLAUDE_DUPLICATE_SAME_EVENT = "duplicate_same_event"
CLAUDE_DUPLICATE_SAME_EPISODE = "duplicate_same_episode"
CLAUDE_HISTORICAL_ID_COLLISION = "historical_id_collision"
CLAUDE_TERMINAL_CONFLICT = "terminal_conflict"
CLAUDE_LEDGER_PROVISIONAL_STATUSES = frozenset({"handling", "reserved"})
CLAUDE_DEFERRED_TERMINAL_STATUSES = frozenset({
    "deferred_cleared",
    "deferred_recovered",
    "deferred_expired",
    "deferred_dropped",
    "deferred_generation_changed",
})
CLAUDE_LEDGER_TERMINAL_STATUSES = frozenset({
    "sent", "completed", "human_prompt", "watchdog_prompt", "watchdog_confirmed",
    "cancelled", "failed", "unmapped", "ignored_paused", "observed_disabled",
    "dry_run", "identity_conflict", "nested_process_ignored", "process_unverified",
    "process_conflict", "late_after_fallback", "suppressed_completed",
    "submit_duplicate_suppressed", "deferred_cleared", "deferred_recovered",
    "deferred_expired", "deferred_dropped",
})
CLAUDE_HOOK_CONFIG_POLICY = "auto_repair"
CLAUDE_HOOK_AUTO_REPAIR_MAX_PER_HOUR = 5
CLAUDE_HOOK_DRIFT_WARNING_PER_DAY = 20
CLAUDE_HOOK_SETTINGS_BACKUP_KEEP = 20
CLAUDE_HOOK_EVENTS = ("SessionStart", "Stop", "StopFailure", "UserPromptSubmit")
CLAUDE_HOOK_COMMAND = (
    f'{DEFAULT_PYTHON} "{PROJECT_DIR / "claude_ccc_event_hook.py"}"'
)
CLAUDE_PROCESS_INSPECTION_CACHE_SEC = 30.0
# Process inspection is used only when a current recoverable-error candidate
# cannot be safely classified from its screen.  It is deliberately cached by
# workspace so an unavailable Claude footer never turns the one-second watch
# loop into a full ``cmux top`` sweep.
CLAUDE_PROCESS_CACHE_SEC = 5.0
# States that mean "still the same stuck episode", so send_count and the
# repeat delay keep accumulating across them.
#
# ``working`` belongs here even though it looks like recovery: it is the
# *expected consequence* of a successful nudge.  Treating it as a new event
# restarted the episode on every retry, zeroing send_count -- which made both
# repeat_send_delay_sec and circuit_pause_after unenforceable.  Production ran
# 108 minutes that way (12 surfaces, ~91 sends each, every log line saying
# count=1).  ``queued_followup`` and ``error_superseded`` are the same story:
# both are mid-episode observations of a nudge we already sent.
EPISODE_CONTINUITY_STATES = frozenset({
    "recoverable_error",
    "awaiting_transition",
    "working",
    "queued_followup",
    "error_superseded",
})
# Codex recoverable errors retain their existing retry policy.  Claude uses a
# separate stop-event guard: an empty verified ❯ or a live Claude API error is
# eligible once, then remains pending until real new activity is confirmed.
SEND_ELIGIBLE_STATES = frozenset({"recoverable_error", "claude_stopped"})
DEPRECATED_CLAUDE_CONFIG_KEYS = frozenset({
    "claude_error_repeat_delay_sec",
    "claude_futile_idle_limit",
    "claude_futile_error_limit",
    "claude_idle_first_delay_sec",
    "claude_idle_repeat_sec",
})
LEGACY_CLAUDE_AUTO_PAUSE_PREFIX = "Claude futile loop; need human"
# How few cells may be left unused before a row counts as "full", meaning the
# row beneath it is that row's wrapped tail.  Measured, not chosen: across 43
# adjacent rows on 70 live surfaces, every genuine wrap left 0-7 cells unused
# and the two rows that were new output left 32 and 38.  The threshold sits at
# the top of the measured wrap range; widening it only grows the band where a
# nearly-full banner that did *not* wrap gets mistaken for one that did.
WRAP_SLACK_CELLS = 7
CLAUDE_CONTEXT_WARNING_PERCENT = 80
CLAUDE_CONTEXT_START_GRACE_SEC = 60.0
CLAUDE_CONTEXT_STALL_SEC = 180.0
CLAUDE_CONTEXT_ABSOLUTE_TIMEOUT_SEC = 900.0
# Claude submission is a transaction: write the prompt as text, then submit
# it with an explicit Enter key. The transaction survives daemon restarts
# through TargetRuntime and suppresses duplicate Hook/fallback deliveries.
# Human label only.  Acceptance always compares SHA-256 of the loaded source:
# a revision string is hand-maintained and therefore can lie about what runs.
FEATURE_REVISION = "2026-08-25.v3-surface36"
# How long after our own send a byte-identical UserPromptSubmit can still be
# our echo.  Must exceed claude_submit_confirm_timeout_sec so that a late
# echo arriving after the transaction timed out is not read as a human.
CLAUDE_WATCHDOG_ECHO_WINDOW_SEC = 30.0
# How far *before* our recorded submit a byte-identical prompt may appear and
# still be our own echo.  The anchor is stamped before the Enter key leaves this
# process, so a real echo is always later; this only absorbs a system clock step
# landing between the two stamps.  Judging that noise "human" would clear the
# completion latch on our own echo, which is the failure this whole path fixes.
CLAUDE_ECHO_CLOCK_TOLERANCE_SEC = 2.0
# Longest a deferred (transiently blocked) Stop may wait for a safe frame.
CLAUDE_DEFERRED_MAX_AGE_SEC = 900.0
# Cap on the in-memory ledger-claim cost map (event_id -> ms).
CLAUDE_CLAIM_COST_LIMIT = 512
CLAUDE_SUBMIT_CONFIRM_TIMEOUT_SEC = 15.0
CLAUDE_SUBMIT_RETRY_ENTER_SEC = 1.0
# How often an orphaned watchdog composer may be re-submitted.  An orphan is our
# own text left in the composer after its transaction was already cleared (a
# confirmation timeout clears ``claude_submit_phase`` to "none" while the text is
# still on screen), so no transaction remains to retry the key.  Observed live on
# surface:43 (2026-08-31): phase=none, attempts=0, last_reason=confirmation_timeout,
# our exact prompt sitting in the composer for 283s with nobody left to press Enter.
#
# This is a *recovery* interval, not a send interval: it only ever presses Enter on
# text already proven byte-exact to claude_message, and never types anything.
CLAUDE_ORPHAN_ENTER_RETRY_SEC = 5.0
# 孤儿 Enter 的总量上限（F2，2026-09-01）。每次重试仍受上面的间隔限制，但
# "每单位有界"不等于"总量有界"：一个永远提交不成功的 composer（渲染异常、
# Claude 卡死、Enter 键失效）会让恢复路径无限期地每 5 秒按一次 Enter。达到
# 上限后进入 degraded，不再自动按键；只有 composer 内容变化（被消费或被用户
# 编辑）、新的提交事务建立、或人工重新 arm 才重置预算。
CLAUDE_ORPHAN_ENTER_MAX = 3


class CmuxError(RuntimeError):
    """An expected cmux command or protocol failure."""


class IncompatibleError(CmuxError):
    """The connected cmux does not expose the required protocol shape."""


class GlobalIncompatibleError(IncompatibleError):
    """The cmux protocol version is incompatible for every surface."""


@dataclasses.dataclass(frozen=True)
class Span:
    row: int
    column: int
    cell_width: int
    style_id: int
    text: str


@dataclasses.dataclass(frozen=True)
class Cursor:
    row: int
    column: int
    visible: bool


@dataclasses.dataclass(frozen=True)
class Grid:
    rows: int
    columns: int
    spans: tuple[Span, ...]
    styles: Mapping[int, Mapping[str, Any]]
    cursor: Cursor
    lines: tuple[str, ...]
    raw: Mapping[str, Any]

    @classmethod
    def from_rpc(cls, payload: Mapping[str, Any], surface_id: str) -> "Grid":
        response: Mapping[str, Any] = payload
        if isinstance(response.get("result"), Mapping):
            response = response["result"]
        render_grid = response.get("render_grid", response)
        if isinstance(render_grid, Mapping) and isinstance(render_grid.get("result"), Mapping):
            render_grid = render_grid["result"]
        if not isinstance(render_grid, Mapping):
            raise IncompatibleError("terminal.replay missing render_grid")
        if render_grid.get("format") != GRID_FORMAT:
            raise GlobalIncompatibleError(f"unsupported render grid format: {render_grid.get('format')!r}")
        rows = render_grid.get("rows")
        columns = render_grid.get("columns")
        spans_data = render_grid.get("row_spans")
        styles_data = render_grid.get("styles")
        cursor_data = render_grid.get("cursor")
        if not isinstance(rows, int) or not isinstance(columns, int) or rows <= 0 or columns <= 0:
            raise IncompatibleError("invalid render grid dimensions")
        if not isinstance(spans_data, list) or not isinstance(styles_data, list) or not isinstance(cursor_data, Mapping):
            raise IncompatibleError("render grid missing row_spans/styles/cursor")
        styles: dict[int, Mapping[str, Any]] = {}
        for style in styles_data:
            if not isinstance(style, Mapping) or not isinstance(style.get("id"), int):
                raise IncompatibleError("invalid render grid style")
            styles[style["id"]] = style
        spans: list[Span] = []
        for item in spans_data:
            if not isinstance(item, Mapping):
                raise IncompatibleError("invalid render grid span")
            try:
                span = Span(
                    row=int(item["row"]),
                    column=int(item["column"]),
                    cell_width=int(item.get("cell_width", item.get("width", 1))),
                    style_id=int(item["style_id"]),
                    text=str(item.get("text", "")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IncompatibleError("invalid render grid span fields") from exc
            if not (0 <= span.row < rows and 0 <= span.column < columns):
                raise IncompatibleError("render grid span outside viewport")
            if span.cell_width <= 0 or span.column + span.cell_width > columns or span.style_id not in styles:
                raise IncompatibleError("render grid span dimensions/style invalid")
            spans.append(span)
        try:
            cursor = Cursor(
                row=int(cursor_data["row"]),
                column=int(cursor_data["column"]),
                visible=bool(cursor_data["visible"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IncompatibleError("invalid render grid cursor") from exc
        if not (0 <= cursor.row < rows and 0 <= cursor.column <= columns):
            raise IncompatibleError("render grid cursor outside viewport")
        lines = _render_lines(rows, spans)
        return cls(rows, columns, tuple(spans), styles, cursor, tuple(lines), render_grid)

    def style(self, style_id: int) -> Mapping[str, Any]:
        try:
            return self.styles[style_id]
        except KeyError as exc:
            raise IncompatibleError(f"missing style {style_id}") from exc

    def signature(self) -> str:
        relevant = "\n".join(self.lines)
        return _short_hash(f"{relevant}\0{self.cursor.row}:{self.cursor.column}:{self.cursor.visible}")


@dataclasses.dataclass(frozen=True)
class ScreenState:
    kind: str
    error_type: str | None = None
    fingerprint: str | None = None
    screen_signature: str | None = None
    reason: str = ""
    # Explicit adapter routing.  ``None`` keeps legacy non-send states cheap;
    # send-eligible states always carry either ``codex`` or ``claude``.
    message_kind: str | None = None
    # Last non-chrome content, digits collapsed. Used to identify the same
    # stopped transcript without treating a retry countdown tick as new work.
    content_fingerprint: str | None = None
    # True only when the active Claude composer contains our exact configured
    # watchdog prompt.  It prevents the prompt's brief echo from masquerading
    # as new user input that would re-arm the same stop event.
    watchdog_echo: bool = False
    claude_context: "ClaudeContextTelemetry | None" = None


@dataclasses.dataclass(frozen=True)
class ClaudeContextTelemetry:
    """A current-viewport Claude context reading; unknown values stay None."""

    percent: int | None = None
    input_tokens: int | None = None
    cache_tokens: int | None = None
    auto_compact_remaining_percent: int | None = None
    limit_reached: bool = False
    compacting: bool = False
    compaction_percent: int | None = None
    composer_kind: str = "unverified"


@dataclasses.dataclass
class TargetRuntime:
    state: str = "unknown"
    error_type: str | None = None
    episode_id: str | None = None
    episode_started_at: float = 0.0
    send_count: int = 0
    last_send_at: float = 0.0
    awaiting: bool = False
    awaiting_suppressed: int = 0
    sent_fingerprint: str | None = None
    sent_screen_signature: str | None = None
    paused_reason: str | None = None
    last_notice_at: float = 0.0
    # Claude-only runtime guards.  They live in state.json, never config.json,
    # so a daemon restart cannot forget a completed turn or requeue the same
    # long watchdog prompt merely because the viewport changed.
    claude_prompt_pending: bool = False
    claude_prompt_kind: str | None = None
    claude_prompt_fingerprint: str | None = None
    claude_prompt_sent_at: float = 0.0
    claude_completed_latched: bool = False
    claude_completion_fingerprint: str | None = None
    claude_completed_at: float = 0.0
    claude_working_polls: int = 0
    # Two-phase input arbitration.  The first empty-composer frame only arms a
    # candidate; a later replay must prove the same stop event is still current.
    claude_candidate_key: str | None = None
    claude_candidate_since: float = 0.0
    claude_candidate_focused: bool = False
    claude_last_cancel_reason: str | None = None
    claude_last_cancel_at: float = 0.0
    # Hook-driven lifecycle state.  Screen polling is only a send-safety gate;
    # these event IDs define task boundaries and survive daemon restarts.
    claude_session_id: str | None = None
    claude_generation_id: str | None = None
    claude_last_hook_at: float = 0.0
    claude_hook_status: str | None = None
    claude_last_event_id: str | None = None
    claude_last_event_status: str | None = None
    claude_completion_event_id: str | None = None
    claude_last_resume_event_id: str | None = None
    claude_event_message_hash: str | None = None
    # Hook health is independent from the viewport classifier.  In particular,
    # an unreadable grid must not hide a legacy Claude process that cannot emit
    # CCC lifecycle events.
    claude_hook_health: str = "unverified"
    claude_process_pid: int = 0
    claude_process_started_at: str | None = None
    claude_process_generation: str | None = None
    # Process generation to which the last accepted *real* Hook was bound.
    # ``claude_last_hook_at`` alone is insufficient after a restart: a timestamp
    # and session string from the previous process can otherwise authorize the
    # new process once its grace period expires.
    claude_hook_process_generation: str | None = None
    claude_hook_unverified_since: float = 0.0
    # How long this surface has had no trustworthy Hook generation.  The clock
    # starts at max(process_started, daemon_started) -- never at last_send_at,
    # which can predate the live Claude process and describe a dead generation.
    # How long the raw viewport has been unreadable (incompatible/unreadable).
    # Measured on the classifier's own verdict, before runtime guards relabel it,
    # so a blind pane cannot hide behind a friendlier derived state.
    claude_unreadable_since: float = 0.0
    claude_unreadable_warned_at: float = 0.0
    # Which of the four entry points last observed this blind spot.  A blind spot
    # that changes entry (returned <-> raised) is still the same exposure, so this
    # is recorded for the log without restarting the clock.
    claude_unreadable_entry: str | None = None
    claude_hook_unprotected_since: float = 0.0
    claude_hook_unprotected_severity: str = ""
    claude_consecutive_resumes: int = 0
    claude_repeat_warning: bool = False
    claude_repeat_warning_at: float = 0.0
    # Hook-gap fallback. A screen stop is only eligible after two identical
    # frames and only once for a process-generation/content fingerprint.
    claude_fallback_candidate_fingerprint: str | None = None
    claude_fallback_candidate_polls: int = 0
    claude_fallback_candidate_generation: str | None = None
    # Stable lifecycle identity for one Hook-gap recovery episode.  The visible
    # content fingerprint is evidence *inside* the episode, never its identity:
    # retry chrome can change every poll, while a later stopped turn can render
    # byte-identically to an older one.  Persisting a random token prevents both
    # retry-budget resets and historical ledger-id collisions across restarts.
    claude_fallback_episode_id: str | None = None
    claude_fallback_episode_started_at: float = 0.0
    claude_fallback_episode_generation: str | None = None
    claude_fallback_episode_session_id: str | None = None
    claude_fallback_attempt_token: str | None = None
    claude_fallback_last_fingerprint: str | None = None
    claude_fallback_last_event_id: str | None = None
    claude_fallback_sent_at: float = 0.0
    claude_fallback_retry_count: int = 0
    claude_fallback_retry_exhausted: bool = False
    # Persisted live Hook delivery SLA. Journal replays are deliberately kept
    # separate because an offline replay cannot satisfy a one-second live SLA.
    claude_hook_latency_ms: list[float] = dataclasses.field(default_factory=list)
    claude_hook_live_send_count: int = 0
    claude_hook_sla_miss_count: int = 0
    claude_hook_last_latency_ms: float = 0.0
    claude_hook_max_latency_ms: float = 0.0
    claude_hook_replay_count: int = 0
    claude_hook_replay_max_age_sec: float = 0.0
    # Context telemetry never pauses/removes a target and never sends slash
    # commands.  It only blocks a continuation while Claude is at a proven
    # hard limit or actively/stuck compacting.
    claude_context_status: str = "unknown"
    claude_context_percent: int | None = None
    claude_context_input_tokens: int | None = None
    claude_context_cache_tokens: int | None = None
    claude_auto_compact_remaining_percent: int | None = None
    claude_context_episode_started_at: float = 0.0
    claude_context_limit_first_seen_at: float = 0.0
    claude_compaction_started_at: float = 0.0
    claude_compaction_last_progress_at: float = 0.0
    claude_compaction_last_seen_at: float = 0.0
    claude_compaction_current_percent: int | None = None
    claude_compaction_highest_percent: int | None = None
    claude_compaction_restart_count: int = 0
    claude_context_composer_kind: str = "unverified"
    # When the persisted context reading above was taken.  Audits show both this
    # stored verdict and a freshly replayed one; during the 2026-08-24
    # observation they disagreed 13 times by 6-22 points in both directions.
    # The gap is real signal (context moves fast), so the fix is to date each
    # reading rather than force them to match.
    claude_context_sampled_at: float = 0.0
    claude_context_notification_sent: bool = False
    # Explicit Claude composer transaction. ``text_written`` means the exact
    # watchdog text was placed in the composer; ``enter_sent`` means cmux was
    # asked to submit it. A pending transaction blocks duplicate events.
    claude_submit_event_id: str | None = None
    claude_submit_message_hash: str | None = None
    claude_submit_fingerprint: str | None = None
    claude_submit_since: float = 0.0
    claude_submit_last_attempt_at: float = 0.0
    claude_submit_phase: str = "none"
    claude_submit_attempts: int = 0
    claude_submit_confirmed_at: float = 0.0
    claude_submit_last_reason: str | None = None
    # Last watchdog text we actually submitted on this surface.  Kept *after*
    # the transaction clears, because correlation must outlive it: a byte-
    # identical UserPromptSubmit arriving seconds later is our own echo, and
    # the transaction is already gone by then (confirmed/timed out).  Without
    # this, an echo looks exactly like a human paste of the same words.
    claude_last_submit_event_id: str | None = None
    claude_last_submit_message_hash: str | None = None
    claude_last_submit_session_id: str | None = None
    claude_last_submit_generation: str | None = None
    claude_last_submit_at: float = 0.0
    # How the most recent byte-identical prompt was attributed.  Purely
    # diagnostic; never an authorization input.
    claude_last_prompt_attribution: str | None = None
    # One -- and only one -- Stop that could not be sent yet because the frame
    # was transiently unsafe.  Measured on 2026-08-25: of 355 cancelled events
    # 99.7% were transient (composer busy / working / content changed), the
    # median one was rescued by a later Stop in 20.5s, but the tail was not --
    # 4794s, 5224s, 5366s, 6764s, and three never recovered at all.  A terminal
    # ``cancelled`` verdict is what created that tail, so the event is parked
    # here instead and retried from the poll loop.  Exactly one slot: a queue
    # would let a burst of Stops turn into a burst of prompts.
    claude_deferred_event: dict[str, Any] | None = None
    claude_deferred_reason: str | None = None
    claude_deferred_since: float = 0.0
    claude_deferred_attempts: int = 0
    claude_deferred_last_attempt_at: float = 0.0
    # Last time we pressed Enter on an *orphaned* watchdog composer -- our own
    # text whose transaction is already gone.  Rate-limits recovery so a surface
    # whose Enter is being swallowed cannot turn into a keypress loop.
    claude_orphan_enter_at: float = 0.0
    claude_orphan_enter_count: int = 0

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TargetRuntime":
        fields = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value[key] for key in fields if key in value})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def remaining_poll_delay(interval_sec: float, elapsed_sec: float) -> float:
    """Sleep only the unused part of a polling period."""

    return max(0.0, max(0.0, interval_sec) - max(0.0, elapsed_sec))


def _render_lines(rows: int, spans: Sequence[Span]) -> list[str]:
    by_row: dict[int, list[Span]] = {}
    for span in spans:
        by_row.setdefault(span.row, []).append(span)
    output: list[str] = []
    for row in range(rows):
        pieces: list[str] = []
        column = 0
        for span in sorted(by_row.get(row, []), key=lambda item: item.column):
            if span.column > column:
                pieces.append(" " * (span.column - column))
            pieces.append(span.text)
            column = max(column, span.column + span.cell_width)
        output.append("".join(pieces).rstrip())
    return output


def _normalise_lines(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    return [str(line).rstrip() for line in value]


def _menu_present(lines: Sequence[str]) -> bool:
    text = "\n".join(lines)
    return any(pattern.lower() in text.lower() for pattern in MENU_PATTERNS) or bool(PERMISSION_RE.search(text))


def _working_present(lines: Sequence[str]) -> bool:
    return bool(WORKING_RE.search("\n".join(lines)))


def _queued_followup_present(lines: Sequence[str], composer_row: int) -> bool:
    """True when Codex is already holding queued input of its own accounting.

    When input arrives while Codex is busy it does not consume it: it prints

        • Queued follow-up inputs
          ↳ 任务请继续
          ↳ 任务请继续

    and drains the queue one item per later turn.  The composer stays empty and
    ready the whole time, and on `high demand` the error banner stays in the
    transcript, so every poll still read "stuck, composer free, send again" and
    piled one more copy onto the queue every second.  Codex is telling us plainly
    that it already has the message; that is a hard reason not to send another.

    Two signatures, because the banner scrolls off once the queue is long:
    the header itself, or a run of identical `↳` rows (Codex only stacks
    identical rows like that when it is listing the same pending input).
    """
    head = [line.strip() for line in lines[:max(composer_row, 0)]]
    if any(QUEUED_FOLLOWUP_RE.search(line) for line in head):
        return True
    previous = ""
    for line in head:
        if line.startswith("↳"):
            payload = line[1:].strip()
            if payload and payload == previous:
                return True
            previous = payload
        elif line:
            previous = ""
    return False


def _match_error_block(block_text: str) -> str | None:
    lower = re.sub(r"\s+", " ", block_text).strip().lower()
    if "exceeded retry limit" in lower and ("429" in lower or "too many requests" in lower):
        return "rate_limit"
    if HIGH_DEMAND.lower() in lower:
        return "high_demand"
    if "stream disconnected before completion" in lower:
        return "stream"
    if "error sending request for url" in lower and (
        "zzzcoding.org" in lower or "/v1/responses" in lower
    ):
        return "stream"
    if any(token in lower for token in ("last status: 503", "http 503", "503 service unavailable")):
        return "http_503"
    has_405 = "405 not allowed" in lower or "405 method not allowed" in lower
    has_405_evidence = any(
        token in lower
        for token in ("<html", "nginx", "url:", "zzzcoding.org", "/v1/responses")
    )
    if "unexpected status 405 method not allowed" in lower or (has_405 and has_405_evidence):
        return "http_405"
    if "prompt_cache_retention" in lower and ("400" in lower or "invalid_parameter" in lower):
        return "prompt_cache"
    return None


def _row_is_full(grid: Grid, row: int) -> bool:
    """Whether ``row`` reaches the right edge, so the next row is its wrap.

    Measured in terminal cells via ``span.cell_width``, never ``len(text)`` --
    a CJK character occupies two columns, so counting code points would call a
    full row short and drop the tail that followed it.

    ``WRAP_SLACK_CELLS`` is the observed boundary, not a guess: across 43
    adjacent rows on 70 live surfaces every genuine wrap left 0-7 cells unused,
    while the two rows that were new output (not wraps) left 32 and 38.  The
    band between is empty, so the threshold sits at the top of the measured
    wrap range rather than a cell above it.

    Known residual: a banner that nearly fills its row without wrapping looks
    identical to one that did.  Production has three such rows (the 429 banner
    at 108/109 cells), and all three are followed by a blank row, which
    ``_find_last_error`` stops on before geometry is ever consulted.  Should
    Codex one day print unindented output directly beneath such a banner, it
    would be absorbed into the block; see README.
    """

    if row < 0 or row >= grid.rows:
        return False
    occupied = max(
        (span.column + span.cell_width for span in grid.spans if span.row == row and span.text.strip()),
        default=0,
    )
    return occupied > 0 and grid.columns - occupied <= WRAP_SLACK_CELLS


def _is_transcript_row(text: str) -> bool:
    """Codex's own transcript markers: a prompt echo or a tool/agent entry.

    These start a new transcript item, so they can never be the wrapped tail of
    an error block -- not even when cmux happens to render them in the error
    style, which it does after we send into a stalled session.
    """

    return text.lstrip().startswith(("›", "•", "↳"))


@dataclasses.dataclass(frozen=True)
class ErrorScan:
    """The last recoverable error block on screen, and whether it is still live.

    ``superseded`` means the block matched one of the six recoverable errors but
    Codex has printed something after it, so it is history.  Keeping the type
    instead of collapsing to "nothing found" is what makes a suppression
    auditable in the Supervisor rather than silently indistinguishable from a
    healthy idle session.
    """

    error_type: str
    block: str
    superseded: bool


def _find_last_error(
    lines: Sequence[str],
    composer_row: int | None = None,
    marker_validator: Any | None = None,
    continuation_validator: Any | None = None,
) -> ErrorScan | None:
    """Return the last error block on screen, live or superseded.

    Two structural facts decide this, both measured on 169 real markers across
    28 live surfaces rather than inferred from the wording of a row:

    * **Adjacency proves membership.**  Codex never separates a wrapped error
      line from its marker (gap of 1 row in 14/14 observed wraps) and always
      separates a new transcript entry with blank rows (gap of 3 in 155/155).
      So the block is the marker plus the rows directly beneath it; the first
      blank row ends it.
    * **Newer output proves the marker is history.**  Anything non-blank below
      the block means Codex printed something after the error -- our echoed
      follow-up, a tool call, an agent reply.  The error has already been dealt
      with, and sending again just piles onto Codex's input queue.  That loop
      is what produced 180 queued copies of one message on surface 04CF3C13.

    An earlier attempt judged membership by whether a row still *read* like an
    error (status code, URL, known phrase).  That silently dropped genuine
    wraps: the 503 message wraps at 92 columns to the tail "re headers", which
    carries no error syntax at all, so a real stall would never be rescued.
    Failing to rescue is the worse error of the two, hence structure over text.
    """

    limit = composer_row if composer_row is not None else len(lines)
    marker_rows = [index for index, line in enumerate(lines[:limit]) if line.lstrip().startswith(("■", "⚠"))]
    if not marker_rows:
        return None
    marker = marker_rows[-1]
    if marker_validator is not None and not marker_validator(marker):
        return None

    block_end = marker + 1
    for index in range(marker + 1, limit):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith(("■", "⚠")) or _is_transcript_row(stripped):
            break
        if continuation_validator is not None and not continuation_validator(index):
            break
        block_end = index + 1

    superseded = any(
        lines[index].strip() and not _is_footer(lines[index].strip())
        for index in range(block_end, limit)
    )

    block = [line.strip() for line in lines[marker:block_end] if line.strip()]
    if not block:
        return None
    block_text = "\n".join(block)
    error_type = _match_error_block(block_text)
    if error_type is None:
        return None
    return ErrorScan(error_type, block_text, superseded)


def _is_footer(line: str) -> bool:
    return bool(FOOTER_RE.match(line)) or line in {"·", "│", "|"}


def _is_codexish(lines: Sequence[str], composer_row: int) -> bool:
    text = "\n".join(lines)
    lower = text.lower()
    return "gpt-" in lower or "context" in lower or "plan mode" in lower or "esc to interrupt" in lower


def _composer_status(grid: Grid) -> tuple[str, int] | tuple[str, None]:
    cursor = grid.cursor
    if not cursor.visible:
        return "incompatible", None
    row_spans = sorted((span for span in grid.spans if span.row == cursor.row), key=lambda span: span.column)
    prompt = next((span for span in row_spans if span.column == 0 and "›" in span.text), None)
    if prompt is None:
        return "incompatible", None
    if grid.style(prompt.style_id).get("faint", False):
        return "incompatible", None
    space_span = next((span for span in row_spans if span.column <= 1 < span.column + span.cell_width), None)
    if space_span is None or grid.style(space_span.style_id).get("faint", False):
        return "incompatible", None
    if cursor.column != 2:
        return "composer_busy", cursor.row
    for span in row_spans:
        span_end = span.column + span.cell_width
        if span_end <= 2:
            continue
        text = span.text
        if span.column < 2:
            text = text[max(0, 2 - span.column):]
        if not text.strip():
            continue
        if not grid.style(span.style_id).get("faint", False):
            return "composer_busy", cursor.row
    return "empty", cursor.row


# Footer markers only Claude Code draws.  The gate deliberately requires one of
# these *plus* a ❯ prompt: the one unacceptable failure mode is mistaking a
# Codex screen for Claude and thereby stopping a rescue, so a bare ❯ (a plain
# shell) or a stray "context" mention is not enough.  ``_is_codexish`` cannot be
# reused here -- it matches 13/15 live Claude screens because of the "1M
# context" footer.
#
# Two markers were removed after live Codex surfaces matched them:
# "shift+tab to cycle" is drawn by Codex's own Plan mode status bar (s133), and
# a bare "CLAUDE.md" is just a filename any transcript can mention (s138 was
# editing prose about it).  What remains is drawn by Claude and nothing else.
CLAUDE_FOOTER_RE = re.compile(
    r"(?:bypass permissions on|\[Opus [\d.]+|\[Sonnet [\d.]+|"
    r"\[Haiku [\d.]+|/clear to save)",
    re.IGNORECASE,
)
CLAUDE_PROMPT_RE = re.compile(r"^\s*❯")
# Codex's composer prompt.  A row starting with › is Codex's own input line.
CODEX_PROMPT_RE = re.compile(r"^\s*›")
# Claude working / chrome.  Copied from the read-only sampler so this module
# does not import it (the sampler already imports us).
CLAUDE_ACTIVE_SPINNER_RE = re.compile(
    r"^\s*(?!✓)\S{1,3}\s+.+(?:…|\.\.\.)\s*\([^)]*(?:\d+\s*[hms]|↓|tokens?)[^)]*\)\s*$",
    re.IGNORECASE,
)
CLAUDE_ACTIVE_TOOL_RE = re.compile(r"^\s*◐\s+\S.*(?:…|\.\.\.)\s*(?:\([^)]*\))?\s*$", re.IGNORECASE)
CLAUDE_PROGRESS_RE = re.compile(r"^\s*▸\s*\(\s*\d+\s*/\s*\d+\s*\)", re.IGNORECASE)
CLAUDE_COMPLETED_RE = re.compile(r"^\s*✻\s+.+\s+for\s+\d+(?:\.\d+)?\s*[hms]\b", re.IGNORECASE)
CLAUDE_QUESTION_RE = re.compile(
    r"(?:do you want to|would you like to|permission required|press enter to confirm)",
    re.IGNORECASE,
)
CLAUDE_ASK_FOOTER_RE = re.compile(r"^\s*✓\s*AskUserQuestion(?:\s*[×x]\s*\d+)?\s*$", re.IGNORECASE)
CLAUDE_DONE_RE = re.compile(
    r"建议检查\s*usage\s*[:：]\s*/context\s*[。.!！?？'\"’”）)]*\s*$",
    re.IGNORECASE,
)
CLAUDE_DONE_COMPACT = "建议检查usage:/context"
CLAUDE_CONTEXT_LINE_RE = re.compile(
    r"^\s*上下文\s+[█▓▒░]+\s+(?P<percent>\d{1,3})%"
    r"(?:\s*\(\s*输入\s*:\s*(?P<input>[\d.]+[kKmM]?)\s*,\s*"
    r"缓存\s*:\s*(?P<cache>[\d.]+[kKmM]?)\s*\))?\s*$"
)
CLAUDE_AUTO_COMPACT_RE = re.compile(
    r"^\s*(?P<percent>\d{1,3})%\s+until\s+auto-compact\s*$",
    re.IGNORECASE,
)
CLAUDE_CONTEXT_LIMIT_RE = re.compile(
    r"^\s*(?:⎿\s*)?Context limit reached\s*[·.-].*$",
    re.IGNORECASE,
)
CLAUDE_COMPACTING_RE = re.compile(
    r"^\s*\S{0,3}\s*Compacting conversation(?:…|\.\.\.)",
    re.IGNORECASE,
)
CLAUDE_COMPACTION_PROGRESS_RE = re.compile(
    r"^\s*(?:[█▓▒░]+\s*)?(?P<percent>\d{1,3})%\s*$"
)

# Live Claude retry chrome.  Must look like a status row (spinner/bullet
# prefix).  Unanchored "Retrying in 5s" / "attempt 10/10" in recap prose is
# not a send trigger.
CLAUDE_LIVE_BANNER_RE = re.compile(
    r"^\s*(?:[✻※✶●◐◑✘✗xX✖]\s+).*(?:"
    r"retrying in\s+\d|"
    r"attempt\s+\d+\s*/\s*\d+|"
    r"no available accounts|"
    r"credit(?:s)?\s+(?:exhausted|limit)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# Live Claude API/stream chrome.  Anchored at the start of a line so a recap
# sentence that *mentions* an API Error is not a send trigger.  Optional
# spinner/bullet prefix covers the rows Claude actually draws.
CLAUDE_LIVE_API_LINE_RE = re.compile(
    r"^\s*(?:[✻※✶●◐◑✘✗xX✖]\s+)?(?:"
    r"api error\s*[:（(]|"
    r"connection lost|"
    r"stream disconnected before completion"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# Union of the two anchored patterns for any leftover callers.
CLAUDE_LIVE_ERROR_RE = re.compile(
    "(?:%s|%s)" % (CLAUDE_LIVE_BANNER_RE.pattern, CLAUDE_LIVE_API_LINE_RE.pattern),
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_claude_ui(lines: Sequence[str]) -> bool:
    """Whether this screen is Claude Code rather than Codex.

    Three structural conditions, all required:

    1. a ❯ prompt row exists -- take the *last visible* one as an anchor;
    2. a Claude-only footer marker appears strictly *below* it;
    3. no row anywhere on screen starts with Codex's › composer.

    Condition 2 is what stops a Codex surface from being mistaken for Claude
    when its transcript merely quotes a Claude screen -- a plan review, a
    pasted screenshot, this project's own notes.  Anchoring to the last ❯ with
    the footer below it means quoted blocks fail: their footer sits above
    Codex's own status bar, not below the quoted prompt.

    Condition 3 is free and absolute: 0 of 14 live Claude surfaces draw a ›
    row, while Codex always draws one at its composer.  It also covers the
    case where the quoted block sits *above* a live Codex composer.

    Deliberately *not* a row-distance threshold.  The live prompt sits 6-11
    rows off the bottom while Claude is idle, which made "within 15 rows" look
    safe, but the empty composer row disappears the moment Claude starts
    working: on two production surfaces the last visible ❯ then becomes a
    historical user line 22 and 33 rows up.  A magic number would have dropped
    both.
    """

    if any(CODEX_PROMPT_RE.match(line) for line in lines):
        return False
    last_prompt = None
    for index, line in enumerate(lines):
        if CLAUDE_PROMPT_RE.match(line):
            last_prompt = index
    if last_prompt is None:
        return False
    return any(CLAUDE_FOOTER_RE.search(line) for line in lines[last_prompt + 1:])


def _parse_token_count(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"([\d.]+)([kKmM]?)", value.strip())
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    try:
        return int(float(match.group(1)) * multiplier)
    except ValueError:
        return None


def parse_claude_context_telemetry(
    grid: Grid,
    *,
    composer_kind: str = "unverified",
) -> ClaudeContextTelemetry:
    """Parse only anchored Claude footer/live-chrome rows from the viewport.

    The progress bar width is intentionally ignored.  Narrative mentions in a
    transcript or recap do not match because every accepted line is anchored.
    """

    percent = input_tokens = cache_tokens = remaining = None
    for line in reversed(grid.lines):
        match = CLAUDE_CONTEXT_LINE_RE.fullmatch(line)
        if match:
            percent = min(100, int(match.group("percent")))
            input_tokens = _parse_token_count(match.group("input"))
            cache_tokens = _parse_token_count(match.group("cache"))
            break
    for line in reversed(grid.lines):
        match = CLAUDE_AUTO_COMPACT_RE.fullmatch(line)
        if match:
            remaining = min(100, int(match.group("percent")))
            break

    bottom_start = max(0, grid.rows - 24)
    bottom = list(grid.lines[bottom_start:])
    limit_reached = any(CLAUDE_CONTEXT_LIMIT_RE.fullmatch(line) for line in bottom)
    compacting_row = next(
        (index for index, line in enumerate(bottom) if CLAUDE_COMPACTING_RE.match(line)),
        None,
    )
    compaction_percent = None
    if compacting_row is not None:
        for line in bottom[compacting_row + 1:compacting_row + 5]:
            match = CLAUDE_COMPACTION_PROGRESS_RE.fullmatch(line)
            if match:
                compaction_percent = min(100, int(match.group("percent")))
                break
    return ClaudeContextTelemetry(
        percent=percent,
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        auto_compact_remaining_percent=remaining,
        limit_reached=limit_reached,
        compacting=compacting_row is not None,
        compaction_percent=compaction_percent,
        composer_kind=composer_kind,
    )


def _claude_spans_on_row(grid: Grid, row: int) -> list[Span]:
    return sorted((span for span in grid.spans if span.row == row), key=lambda span: span.column)


def _claude_faint(grid: Grid, span: Span) -> bool:
    return bool(grid.style(span.style_id).get("faint", False))


def _claude_cursor_inside_composer(grid: Grid, prompt_row: int) -> bool:
    """Whether the cursor sits on a continuation row of ``prompt_row``'s prompt.

    Only true when *every* row from the prompt down to the cursor is part of the
    same composer block.  A ``────`` rule or a footer row ends the block, so a
    cursor below one is genuinely outside the composer and stays unverified.
    This is deliberately a structural test: guessing from the row delta alone is
    what made a wrapped prompt look like a cursor parked elsewhere.
    """

    if prompt_row < 0 or prompt_row >= grid.rows:
        return False
    cursor_row = grid.cursor.row
    # Above the prompt is never a continuation of it.
    if cursor_row <= prompt_row:
        return cursor_row == prompt_row
    if cursor_row >= grid.rows:
        return False
    for row in range(prompt_row + 1, cursor_row + 1):
        stripped = grid.lines[row].strip()
        if not stripped:
            # A blank row inside the box is still the box; a blank row is not a
            # boundary because Claude pads the composer to its full height.
            continue
        if CLAUDE_RULE_RE.match(stripped) or CLAUDE_FOOTER_RE.search(stripped):
            return False
        # A second ``❯`` means we mis-anchored: this row starts a new prompt.
        if CLAUDE_PROMPT_RE.match(stripped):
            return False
    return True


def _claude_composer_state(grid: Grid) -> tuple[str, int | None]:
    """empty / busy / unverified using the nearest ❯ row.

    Claude's cmux render reports cursor.visible=false even while the input row
    is present, so Codex's › + visible-cursor gate cannot be reused here.
    """

    candidates: list[tuple[int, int]] = []
    anchor_row = grid.cursor.row
    for row in range(max(0, anchor_row - 6), min(grid.rows, anchor_row + 2)):
        text = grid.lines[row]
        prompt_column = text.find("❯")
        if prompt_column >= 0 and not text[:prompt_column].strip():
            candidates.append((abs(row - grid.cursor.row), row))
    if not candidates:
        return "unverified", None
    distance, prompt_row = min(candidates)
    # A multi-line prompt puts the cursor at the END of the wrapped text, several
    # rows below its own ``❯``.  The old gate read any distance > 1 as "the
    # cursor is somewhere else entirely" and returned unverified, which made a
    # three-row prompt indistinguishable from a cursor parked outside the
    # composer.  Measured on surface:43 (2026-08-31): ``❯`` on row 64, cursor on
    # row 66, distance 2 -> unverified, and the send was deferred for minutes
    # with our own text sitting in the composer.
    #
    # Wrapping is provable rather than assumed: Claude draws a ``────`` rule
    # under the composer box, so if no rule/footer row separates the prompt from
    # the cursor, every row between them belongs to this one prompt.  A cursor
    # that really is elsewhere always has a rule between it and the ``❯``.
    if distance > 1 and grid.cursor.visible and not _claude_cursor_inside_composer(
        grid, prompt_row,
    ):
        return "unverified", distance

    prompt_end = None
    for span in _claude_spans_on_row(grid, prompt_row):
        offset = span.text.find("❯")
        if offset >= 0:
            prompt_end = span.column + offset + 1
            break
    if prompt_end is None:
        return "unverified", distance
    for span in _claude_spans_on_row(grid, prompt_row):
        if span.column + span.cell_width <= prompt_end:
            continue
        if span.column <= prompt_end <= span.column + span.cell_width:
            prompt_offset = max(0, prompt_end - span.column)
            remainder = span.text[prompt_offset:]
            if not remainder.replace("\u00a0", "").strip():
                continue
        if not span.text.strip():
            continue
        if not _claude_faint(grid, span):
            return "busy", distance
    # F1（2026-09-01）：``❯`` 行为空不能证明 composer 为空。首行是空行（粘贴
    # 多行文本、行首换行）时，用户文字只渲染在续行上；只扫 prompt 行会把这种
    # composer 误判成 ``empty``——而 ``empty`` 是唯一能打开发送路径的状态，
    # 误判的后果是守卫器覆盖用户输入。沿用 ``_claude_cursor_inside_composer``
    # 的结构边界向下走到光标行：rule / footer / 第二个 ``❯`` 终止块，空行是
    # 盒内填充不是边界，淡色 span 是占位提示不是输入。光标行以下不扫，
    # composer 为空时光标就停在 ``❯`` 行、区间为空，历史布局零影响。
    cursor_row = min(grid.cursor.row, grid.rows - 1)
    for row in range(prompt_row + 1, cursor_row + 1):
        stripped = grid.lines[row].strip()
        if not stripped:
            continue
        if CLAUDE_RULE_RE.match(stripped) or CLAUDE_FOOTER_RE.search(stripped):
            break
        if CLAUDE_PROMPT_RE.match(stripped):
            break
        for span in _claude_spans_on_row(grid, row):
            if not span.text.strip():
                continue
            if not _claude_faint(grid, span):
                return "busy", distance
    return "empty", distance


def _claude_error_type(text: str) -> str | None:
    if not text:
        return None
    if not (CLAUDE_LIVE_BANNER_RE.search(text) or CLAUDE_LIVE_API_LINE_RE.search(text)):
        return None
    lower = re.sub(r"\s+", " ", text).lower()
    if "connection lost" in lower or "stream disconnected" in lower:
        return "claude_stream"
    if "503" in lower or "no available accounts" in lower:
        return "claude_503"
    if "429" in lower or "rate limit" in lower:
        return "claude_429"
    if "overloaded" in lower:
        return "claude_overloaded"
    if "credit" in lower or "usage limit" in lower:
        return "claude_quota"
    if "retrying" in lower or "attempt" in lower:
        return "claude_retry"
    return "claude_api"


CLAUDE_RULE_RE = re.compile(r"^[─━═\-│┌┐└┘├┤┬┴┼\s]{8,}$")
CLAUDE_CHOICE_RE = re.compile(r"待你选一条才能继续|待你选一条")
CLAUDE_NUDGE_WRAP_RE = re.compile(
    r"^(?:。)?如果任务没有中断就请继续",
)


def _claude_is_chrome_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if CLAUDE_RULE_RE.match(stripped):
        return True
    if CLAUDE_FOOTER_RE.search(stripped) or CLAUDE_COMPLETED_RE.search(stripped):
        return True
    if CLAUDE_ASK_FOOTER_RE.search(stripped) or CLAUDE_PROGRESS_RE.search(stripped):
        return True
    if CLAUDE_ACTIVE_SPINNER_RE.search(stripped) or CLAUDE_ACTIVE_TOOL_RE.search(stripped):
        return True
    if CLAUDE_PROMPT_RE.match(stripped) or "new task?" in stripped.lower():
        return True
    if stripped.startswith("任务中断了么") or CLAUDE_NUDGE_WRAP_RE.match(stripped):
        return True
    return False


def _normalise_claude_prompt(value: str) -> str:
    """Normalise a rendered Claude user prompt without weakening semantics."""

    return re.sub(r"[\s\u00a0]+", "", value).translate(
        str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
    )


def _claude_watchdog_prompt_rows(
    lines: Sequence[str],
    prompt_row: int,
    claude_message: str,
) -> set[int]:
    """Rows occupied by our most recent Claude prompt, including wraps.

    The completion sentence deliberately appears inside ``claude_message``.
    Treating only its first rendered row as chrome made a narrow terminal see
    the wrapped middle row as Claude's own completion report.  Reconstruct the
    exact known user message from the latest historical ``❯`` instead.
    """

    target = _normalise_claude_prompt(claude_message)
    if not target:
        return set()
    candidates = [
        row
        for row in range(min(prompt_row, len(lines)))
        if CLAUDE_PROMPT_RE.match(lines[row].strip())
    ]
    for start in reversed(candidates):
        rows: list[int] = []
        pieces: list[str] = []
        for row in range(start, min(prompt_row, start + 16)):
            raw = lines[row]
            stripped = raw.strip()
            if row == start:
                stripped = CLAUDE_PROMPT_RE.sub("", stripped, count=1).strip()
            elif not stripped or CLAUDE_RULE_RE.match(stripped):
                break
            pieces.append(stripped)
            rows.append(row)
            rendered = _normalise_claude_prompt("".join(pieces))
            if rendered == target:
                return set(rows)
            if not target.startswith(rendered):
                break
    return set()


def _claude_active_prompt_is_watchdog(
    lines: Sequence[str],
    prompt_row: int,
    claude_message: str,
) -> bool:
    """Whether the active busy composer is our exact watchdog prompt.

    ``cmux send`` may expose the submitted text for a frame before Claude
    consumes it.  That frame is not fresh user activity and must not clear the
    one-send latch.  Wrapped rows are reconstructed with the same strict
    normalisation used for historical watchdog prompts.
    """

    if prompt_row < 0 or prompt_row >= len(lines):
        return False
    target = _normalise_claude_prompt(claude_message)
    if not target:
        return False
    pieces: list[str] = []
    for row in range(prompt_row, min(len(lines), prompt_row + 16)):
        stripped = lines[row].strip()
        if row == prompt_row:
            if not CLAUDE_PROMPT_RE.match(stripped):
                return False
            stripped = CLAUDE_PROMPT_RE.sub("", stripped, count=1).strip()
        elif not stripped or CLAUDE_RULE_RE.match(stripped) or CLAUDE_FOOTER_RE.search(stripped):
            break
        pieces.append(stripped)
        rendered = _normalise_claude_prompt("".join(pieces))
        if rendered == target:
            return True
        if not target.startswith(rendered):
            return False
    return False


def _claude_last_content_lines(
    lines: Sequence[str],
    prompt_row: int,
    claude_message: str = CLAUDE_MESSAGE,
) -> list[str]:
    """Closest-to-composer non-chrome lines, newest first.

    Chrome between that content and the composer (``✻ … for``, blank rows,
    the ``────`` box around ``❯``, our own watchdog prompt) is skipped.
    """

    nudge_rows = _claude_watchdog_prompt_rows(lines, prompt_row, claude_message)
    collected: list[str] = []
    for row in range(min(prompt_row, len(lines)) - 1, -1, -1):
        text = lines[row].strip()
        if row in nudge_rows:
            if collected:
                break
            continue
        if _claude_is_chrome_line(text):
            if collected:
                break
            continue
        collected.append(text)
        if len(collected) >= 3:
            break
    return collected


def _claude_block_is_completion(collected: Sequence[str]) -> bool:
    if not collected:
        return False
    last_line = collected[0]
    # The newest effective content must end with the sentinel.  A completion
    # sentence farther up the transcript cannot win over newer content.
    if CLAUDE_DONE_RE.search(last_line):
        return True
    compact = re.sub(r"\s+", "", last_line)
    return compact.endswith(CLAUDE_DONE_COMPACT)


def _claude_content_fingerprint(
    lines: Sequence[str],
    prompt_row: int,
    claude_message: str = CLAUDE_MESSAGE,
) -> str | None:
    collected = _claude_last_content_lines(lines, prompt_row, claude_message)
    if not collected:
        return None
    blob = re.sub(r"\d+", "N", "\n".join(reversed(collected)))
    return _short_hash(blob)


def _claude_live_error_type(
    grid: Grid,
    prompt_row: int,
    claude_message: str = CLAUDE_MESSAGE,
) -> str | None:
    """Current Claude error only: last content line, or live bottom chrome.

    Recap text 16 rows above the composer is not searched.  A completion
    report as last content suppresses API-Error/Connection-lost matches so a
    finished turn that *mentions* those strings is not re-queued; live retry
    banners (``Retrying in``, ``attempt n/n``) in the bottom band still win.
    """

    collected = _claude_last_content_lines(grid.lines, prompt_row, claude_message)
    last_is_done = _claude_block_is_completion(collected)
    if collected and not last_is_done:
        error_type = _claude_error_type(collected[0])
        if error_type is not None:
            return error_type
    bottom = "\n".join(grid.lines[max(0, grid.rows - 8):])
    if last_is_done:
        if CLAUDE_LIVE_BANNER_RE.search(bottom):
            return _claude_error_type(bottom)
        return None
    return _claude_error_type(bottom)


def _claude_completion_fingerprint(
    lines: Sequence[str],
    prompt_row: int,
    claude_message: str = CLAUDE_MESSAGE,
) -> str | None:
    """True when the last real sentence is the instructed completion report.

    Chrome between that sentence and the composer (``✻ … for``, blank rows,
    the ``────`` box around ``❯``) does not count as a newer sentence.
    Production 776BCF10 was misclassified because the composer rule was
    treated as last content, so the visible ``完成，建议检查 usage: /context``
    never became the last sentence.
    """

    collected = _claude_last_content_lines(lines, prompt_row, claude_message)
    if not _claude_block_is_completion(collected):
        return None
    return _short_hash(" ".join(reversed(collected)))


def _claude_finished_normally(
    lines: Sequence[str],
    prompt_row: int,
    claude_message: str = CLAUDE_MESSAGE,
) -> bool:
    return _claude_completion_fingerprint(lines, prompt_row, claude_message) is not None


def classify_claude_grid(grid: Grid, *, claude_message: str = CLAUDE_MESSAGE) -> ScreenState:
    """Independent Claude send classifier. Never uses the Codex › parser.

    Measured live + stage-1 rules:

    * live API/503/retry chrome → recoverable_error, first send immediate;
    * live spinner / tool widget → working, do not send;
    * live permission question → menu, do not send;
    * non-faint text after ❯ → composer_busy, do not send;
    * last effective sentence ends with the completion report → claude_completed, never send;
    * empty ❯ without that report → claude_stopped, one send per stop event;
    * unverified composer → incompatible, do not persist-pause.
    """

    signature = grid.signature()
    composer_kind, prompt_distance = _claude_composer_state(grid)
    context = parse_claude_context_telemetry(grid, composer_kind=composer_kind)
    prompt_rows = [
        row
        for row, text in enumerate(grid.lines)
        if "❯" in text and not text[: text.find("❯")].strip()
    ]
    if prompt_rows:
        prompt_row = max(prompt_rows)
    elif prompt_distance is not None:
        prompt_row = grid.cursor.row
    else:
        prompt_row = grid.rows - 1
    start = max(0, prompt_row - 16)
    end = min(grid.rows, prompt_row + 8)
    # When Claude is working the empty ❯ disappears and the last visible ❯
    # becomes a historical user line 20-30 rows up.  The live spinner/error
    # sits at the bottom, so always include that band.
    scan_rows = set(range(start, end)) | set(range(max(0, grid.rows - 12), grid.rows))
    nearby = [grid.lines[row] for row in sorted(scan_rows)]
    content_fp = _claude_content_fingerprint(grid.lines, prompt_row, claude_message)

    if any(
        CLAUDE_QUESTION_RE.search(text) and not CLAUDE_ASK_FOOTER_RE.search(text)
        for text in nearby
    ):
        return ScreenState(
            "menu",
            screen_signature=signature,
            reason="Claude is waiting for a user answer",
            message_kind="claude",
            content_fingerprint=content_fp,
            claude_context=context,
        )
    if composer_kind == "busy":
        return ScreenState(
            "composer_busy",
            screen_signature=signature,
            reason="Claude composer contains user text",
            message_kind="claude",
            content_fingerprint=content_fp,
            watchdog_echo=_claude_active_prompt_is_watchdog(
                grid.lines,
                prompt_row,
                claude_message,
            ),
            claude_context=context,
        )

    error_type = _claude_live_error_type(grid, prompt_row, claude_message)
    if error_type is not None:
        # Live retry chrome wins over a leftover ◐ tool row: s67 showed
        # ``✻ 503 … Retrying in 3s · attempt 10/10`` plus ``◐ Bash``.  The
        # retry is the stall; waiting for the tool widget would never send.
        return ScreenState(
            "recoverable_error",
            error_type=error_type,
            fingerprint=content_fp or _short_hash(error_type),
            screen_signature=signature,
            reason=f"current {error_type} Claude error",
            message_kind="claude",
            content_fingerprint=content_fp,
            claude_context=context,
        )

    if any(CLAUDE_ACTIVE_SPINNER_RE.search(text) for text in nearby) or any(
        CLAUDE_ACTIVE_TOOL_RE.search(text) for text in nearby
    ):
        return ScreenState(
            "working",
            screen_signature=signature,
            reason="Claude is working",
            message_kind="claude",
            content_fingerprint=content_fp,
            claude_context=context,
        )

    # Sticky ▸ (n/m) and ✻ … for are chrome on an empty ❯, not live work.
    _ = (
        any(CLAUDE_PROGRESS_RE.search(text) for text in nearby),
        any(CLAUDE_COMPLETED_RE.search(text) for text in nearby),
    )
    if composer_kind == "empty":
        completion_fingerprint = _claude_completion_fingerprint(
            grid.lines,
            prompt_row,
            claude_message,
        )
        if completion_fingerprint is not None:
            return ScreenState(
                "claude_completed",
                fingerprint=completion_fingerprint,
                screen_signature=signature,
                reason="Claude last sentence is a completion report",
                message_kind="claude",
                content_fingerprint=content_fp,
                claude_context=context,
            )
        if any(CLAUDE_CHOICE_RE.search(text) for text in nearby):
            return ScreenState(
                "menu",
                screen_signature=signature,
                reason="Claude is waiting for a numbered choice",
                message_kind="claude",
                content_fingerprint=content_fp,
                claude_context=context,
            )
        return ScreenState(
            "claude_stopped",
            error_type="claude_stopped",
            screen_signature=signature,
            reason="Claude stopped without completion report; send once",
            message_kind="claude",
            content_fingerprint=content_fp,
            claude_context=context,
        )
    return ScreenState(
        "incompatible",
        screen_signature=signature,
        reason="Claude composer not verified",
        message_kind="claude",
        content_fingerprint=content_fp,
        claude_context=context,
    )


def classify_text_prefilter(value: str | Iterable[str]) -> ScreenState:
    lines = _normalise_lines(value)
    if _menu_present(lines):
        return ScreenState("menu", reason="interactive menu")
    if _working_present(lines):
        return ScreenState("working", reason="Codex is working")
    tail_count = max(12, len(lines) // 2)
    tail = lines[-tail_count:]
    marker_rows = [index for index, line in enumerate(tail) if line.lstrip().startswith(("■", "⚠"))]
    if marker_rows and _match_error_block("\n".join(tail[marker_rows[-1]:])) is not None:
        return ScreenState("candidate", reason="visible error candidate")
    return ScreenState("idle", reason="no visible error candidate")


def classify_grid(grid: Grid) -> ScreenState:
    lines = grid.lines
    if _menu_present(lines):
        return ScreenState("menu", screen_signature=grid.signature(), reason="interactive menu")
    if _working_present(lines):
        return ScreenState("working", screen_signature=grid.signature(), reason="Codex is working")
    composer_kind, composer_row = _composer_status(grid)
    if composer_kind == "incompatible" or composer_row is None:
        return ScreenState("incompatible", screen_signature=grid.signature(), reason="composer cursor/prompt not verified")
    if not _is_codexish(lines, composer_row):
        return ScreenState("non_codex_or_unknown", screen_signature=grid.signature(), reason="Codex UI fingerprint missing")
    if composer_kind == "composer_busy":
        return ScreenState("composer_busy", screen_signature=grid.signature(), reason="composer contains user text")
    if _queued_followup_present(lines, composer_row):
        return ScreenState(
            "queued_followup",
            screen_signature=grid.signature(),
            reason="Codex already holds queued follow-up input",
        )
    marker_rows = [row for row, line in enumerate(lines[:composer_row]) if line.lstrip().startswith(("■", "⚠"))]
    if not marker_rows:
        return ScreenState("idle", screen_signature=grid.signature(), reason="empty composer without current recoverable error")
    marker_row = marker_rows[-1]
    marker_style_ids = {
        span.style_id
        for span in grid.spans
        if span.row == marker_row and span.text.strip()
    }

    def marker_validator(row: int) -> bool:
        return any(
            span.column == 0 and span.text.lstrip().startswith(("■", "⚠"))
            for span in grid.spans
            if span.row == row
        )

    def continuation_validator(row: int) -> bool:
        """Whether this row is the wrapped tail of the row above it.

        A terminal only wraps because the previous row ran out of columns, so
        that is what gets asked: **was the row above full?**  Every row of a
        wrap except the last one must be full, which makes the test chain
        naturally down a multi-row wrap and stop at the short final tail.

        Three signals are accepted, in descending order of how much they prove:

        1. **Indentation.**  Codex indents some of its own hard-wrapped detail
           lines; those belong to the block regardless of the row above.
        2. **The row above is full.**  Terminal geometry, and the only signal
           that survives a theme change.
        3. **Same style as the marker.**  Convention, not structure -- kept
           deliberately, see the comment at the return below.

        One alternative was measured and rejected: *does this row still read
        like an error?*  That dropped genuine wraps, because the 503 message
        wraps at 92 columns to the tail "re headers", which carries no error
        syntax at all.  A real stall would then never be rescued.
        """
        content_spans = sorted(
            (span for span in grid.spans if span.row == row and span.text.strip()),
            key=lambda span: span.column,
        )
        if not content_spans:
            return False
        if _is_transcript_row("".join(span.text for span in content_spans).strip()):
            return False
        if content_spans[0].column > 0:
            return True
        if _row_is_full(grid, row - 1):
            return True
        # Last resort, and the one signal that is convention rather than
        # geometry.  It is kept because the structured nginx 405 block -- one of
        # the six error types this daemon exists for -- is exactly this shape: a
        # short marker row ending in ``<html>`` followed by col0 rows carrying
        # the HTML dump and the url, all in the error style.  Nothing structural
        # separates that from a line of prose printed in the same colour, so
        # dropping it would silently strand every 405.
        #
        # The residual is bounded by measurement: across 46 marker/next-row
        # pairs on 70 live surfaces, the only not-full marker with an adjacent
        # row had a *different* style (``Token usage`` under a 54%-full MCP
        # warning), which this still rejects.
        return bool(marker_style_ids) and all(
            span.style_id in marker_style_ids for span in content_spans
        )

    error = _find_last_error(
        lines,
        composer_row,
        marker_validator=marker_validator,
        continuation_validator=continuation_validator,
    )
    if error is None:
        return ScreenState("idle", screen_signature=grid.signature(), reason="empty composer without current recoverable error")
    if error.superseded:
        # Not plain "idle": the error is real, we are choosing not to act on it
        # because Codex printed something after it.  Reporting that choice keeps
        # a wrong suppression visible in the Supervisor instead of hiding it
        # among genuinely healthy sessions.
        return ScreenState(
            "error_superseded",
            error_type=error.error_type,
            screen_signature=grid.signature(),
            reason=f"{error.error_type} error already followed by newer output",
        )
    return ScreenState(
        "recoverable_error",
        error_type=error.error_type,
        fingerprint=_short_hash(error.block),
        screen_signature=grid.signature(),
        reason=f"current {error.error_type} error block",
        message_kind="codex",
    )


def default_config() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": "dry-run",
        "global_paused": False,
        "message": MESSAGE,
        "claude_message": CLAUDE_MESSAGE,
        "poll_interval_sec": 1.0,
        "send_interval_sec": 1.0,
        "circuit_pause_after": 0,
        "same_frame_guard_polls": 1,
        "repeat_send_delay_sec": REPEAT_SEND_DELAY_SEC,
        "claude_working_clear_polls": CLAUDE_WORKING_CLEAR_POLLS,
        "claude_background_input_grace_sec": CLAUDE_BACKGROUND_INPUT_GRACE_SEC,
        "claude_focused_input_grace_sec": CLAUDE_FOCUSED_INPUT_GRACE_SEC,
        "claude_event_workers": CLAUDE_EVENT_WORKERS,
        "claude_repeat_warning_after": CLAUDE_REPEAT_WARNING_AFTER,
        "claude_hook_unverified_grace_sec": CLAUDE_HOOK_UNVERIFIED_GRACE_SEC,
        "claude_hook_unprotected_warn_sec": CLAUDE_HOOK_UNPROTECTED_WARN_SEC,
        "claude_hook_unprotected_severe_sec": CLAUDE_HOOK_UNPROTECTED_SEVERE_SEC,
        "claude_hook_unprotected_critical_sec": CLAUDE_HOOK_UNPROTECTED_CRITICAL_SEC,
        "claude_unreadable_warn_sec": CLAUDE_UNREADABLE_WARN_SEC,
        "claude_hook_config_policy": CLAUDE_HOOK_CONFIG_POLICY,
        "claude_hook_sla_sec": CLAUDE_HOOK_SLA_SEC,
        "claude_hook_latency_window": CLAUDE_HOOK_LATENCY_WINDOW,
        "claude_hook_gap_fallback_enabled": True,
    "claude_hook_gap_confirm_polls": CLAUDE_HOOK_GAP_CONFIRM_POLLS,
    "claude_hook_gap_retry_after_sec": CLAUDE_HOOK_GAP_RETRY_AFTER_SEC,
        "claude_hook_gap_max_retries": CLAUDE_HOOK_GAP_MAX_RETRIES,
        "claude_hook_auto_repair_max_per_hour": CLAUDE_HOOK_AUTO_REPAIR_MAX_PER_HOUR,
        "claude_hook_drift_warning_per_day": CLAUDE_HOOK_DRIFT_WARNING_PER_DAY,
        "claude_hook_settings_backup_keep": CLAUDE_HOOK_SETTINGS_BACKUP_KEEP,
        "claude_context_warning_percent": CLAUDE_CONTEXT_WARNING_PERCENT,
        "claude_context_start_grace_sec": CLAUDE_CONTEXT_START_GRACE_SEC,
        "claude_context_stall_sec": CLAUDE_CONTEXT_STALL_SEC,
        "claude_context_absolute_timeout_sec": CLAUDE_CONTEXT_ABSOLUTE_TIMEOUT_SEC,
        "claude_submit_confirm_timeout_sec": CLAUDE_SUBMIT_CONFIRM_TIMEOUT_SEC,
        "claude_submit_retry_enter_sec": CLAUDE_SUBMIT_RETRY_ENTER_SEC,
        "log_channel_check_interval_sec": LOG_CHANNEL_CHECK_INTERVAL_SEC,
        # Deployment is staged: telemetry first, then this gate is enabled only
        # after live viewports prove the parser. Unknown telemetry always fails
        # open even when enforcement is true.
        "claude_context_enforcement": False,
        # The first nudge of a stall goes out at once; the delay above only ever
        # bounds repeats.  Set false to make even the first send wait one
        # interval.
        "first_send_immediate": True,
        # Independent of the target list: a registered Claude surface is still
        # observed-only until this switch is on.  true now enables
        # classify_claude_grid; it never hands a Claude screen to the Codex ›
        # parser.
        "claude_enabled": False,
        "cmux_unavailable_poll_sec": 2.0,
        "workspace_discovery_interval_sec": 5.0,
        "cmux_path": DEFAULT_CMUX if Path(DEFAULT_CMUX).exists() else shutil.which("cmux") or DEFAULT_CMUX,
        "manager_surface_id": "",
        "targets": [],
        "workspace_rules": [],
    }


def ensure_app_dir(path: Path = DEFAULT_APP_DIR) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_app_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


class FileLock:
    def __init__(self, path: Path, *, timeout_sec: float = 0.0, purpose: str = "lock"):
        self.path = path
        self.timeout_sec = timeout_sec
        self.purpose = purpose
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        ensure_app_dir(self.path.parent)
        self.handle = self.path.open("a+")
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        deadline = time.monotonic() + max(0.0, self.timeout_sec)
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    self.handle.close()
                    raise RuntimeError(f"cannot acquire {self.purpose}: {exc}") from exc
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise RuntimeError(f"timed out waiting for {self.purpose}: {self.path}") from exc
                time.sleep(0.05)
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _is_ccc_claude_hook(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("type") == "command"
        and "claude_ccc_event_hook.py" in str(value.get("command") or "")
    )


class ClaudeHookSettingsManager:
    """Inspect and non-destructively repair the four CCC Claude Hook entries.

    The manager never restores an old settings file. It locks, re-reads the
    current JSON, changes only CCC hook objects, backs up the exact current
    bytes, and atomically replaces the file with mode 0600.
    """

    def __init__(
        self,
        path: Path = DEFAULT_CLAUDE_SETTINGS_PATH,
        *,
        lock_path: Path | None = None,
        backup_dir: Path = DEFAULT_CLAUDE_SETTINGS_BACKUP_DIR,
        repair_state_path: Path | None = None,
        timeout_sec: float = 2.0,
    ):
        self.path = path
        self.lock_path = lock_path or DEFAULT_CLAUDE_SETTINGS_LOCK_PATH
        self.backup_dir = backup_dir
        self.repair_state_path = repair_state_path or (
            DEFAULT_CLAUDE_HOOK_REPAIR_STATE_PATH
            if path == DEFAULT_CLAUDE_SETTINGS_PATH
            else backup_dir.parent / "claude-hook-repair-state.json"
        )
        self.timeout_sec = timeout_sec

    @staticmethod
    def _drift_signature(settings: Mapping[str, Any]) -> dict[str, Any]:
        hooks = settings.get("hooks") if isinstance(settings.get("hooks"), Mapping) else {}
        events: list[str] = []
        non_ccc_counts: dict[str, int] = {}
        command_basenames: dict[str, list[str]] = {}
        for event_name, rules in sorted(hooks.items()):
            if not isinstance(rules, list):
                continue
            events.append(str(event_name))
            count = 0
            names: set[str] = set()
            for rule in rules:
                commands = rule.get("hooks", []) if isinstance(rule, Mapping) else []
                if not isinstance(commands, list):
                    continue
                for command in commands:
                    if _is_ccc_claude_hook(command):
                        continue
                    count += 1
                    raw = str(command.get("command") or "") if isinstance(command, Mapping) else ""
                    with contextlib.suppress(ValueError):
                        argv = shlex.split(raw)
                        for token in argv:
                            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                                continue
                            basename = Path(token).name
                            if basename == "env":
                                continue
                            names.add(basename)
                            break
            non_ccc_counts[str(event_name)] = count
            command_basenames[str(event_name)] = sorted(names)
        payload = {
            "surviving_events": events,
            "non_ccc_counts": non_ccc_counts,
            "command_basenames": command_basenames,
        }
        payload["signature"] = _short_hash(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        return payload

    def _load_repair_state(self) -> dict[str, Any]:
        value = load_json(self.repair_state_path, {"version": 1, "repairs": [], "drifts": []})
        if not isinstance(value, dict):
            value = {"version": 1, "repairs": [], "drifts": []}
        value.setdefault("version", 1)
        value.setdefault("repairs", [])
        value.setdefault("drifts", [])
        return value

    def _rotate_backups(self, keep: int) -> int:
        backups = sorted(self.backup_dir.glob("claude-settings.*.json"), key=lambda path: path.name)
        for old in backups[:-max(1, keep)]:
            with contextlib.suppress(OSError):
                old.unlink()
        return len(list(self.backup_dir.glob("claude-settings.*.json")))

    def mtime_ns(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    @staticmethod
    def _validate_root(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError("Claude settings root must be a JSON object")
        hooks = value.get("hooks")
        if hooks is not None and not isinstance(hooks, dict):
            raise RuntimeError("Claude settings hooks must be a JSON object")
        return value

    @staticmethod
    def _event_counts(settings: Mapping[str, Any]) -> dict[str, int]:
        hooks = settings.get("hooks") or {}
        counts: dict[str, int] = {}
        for event_name in CLAUDE_HOOK_EVENTS:
            rules = hooks.get(event_name, []) if isinstance(hooks, Mapping) else []
            if rules is None:
                rules = []
            if not isinstance(rules, list):
                raise RuntimeError(f"Claude Hook {event_name} must be an array")
            count = 0
            for rule in rules:
                if not isinstance(rule, Mapping):
                    raise RuntimeError(f"Claude Hook {event_name} rule must be an object")
                commands = rule.get("hooks", [])
                if not isinstance(commands, list):
                    raise RuntimeError(f"Claude Hook {event_name}.hooks must be an array")
                count += sum(1 for command in commands if _is_ccc_claude_hook(command))
            counts[event_name] = count
        return counts

    def inspect(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            settings = self._validate_root(json.loads(raw))
            counts = self._event_counts(settings)
        except FileNotFoundError:
            counts = dict.fromkeys(CLAUDE_HOOK_EVENTS, 0)
            return {
                "status": "missing_file",
                "healthy": False,
                "path": str(self.path),
                "event_counts": counts,
                "mtime_ns": None,
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "status": "invalid",
                "healthy": False,
                "path": str(self.path),
                "event_counts": {},
                "mtime_ns": self.mtime_ns(),
                "error": str(exc),
            }
        healthy = all(counts.get(name) == 1 for name in CLAUDE_HOOK_EVENTS)
        report = {
            "status": "healthy" if healthy else "repair_needed",
            "healthy": healthy,
            "path": str(self.path),
            "event_counts": counts,
            "mtime_ns": self.mtime_ns(),
        }
        budget = self._load_repair_state()
        now = time.time()
        repairs = [float(value) for value in budget.get("repairs", []) if isinstance(value, (int, float)) and now - float(value) < 3600]
        drifts = [value for value in budget.get("drifts", []) if isinstance(value, Mapping) and isinstance(value.get("at"), (int, float)) and now - float(value["at"]) < 86400]
        next_retry_at = float(budget.get("next_retry_at") or 0.0)
        if not healthy and budget.get("last_status") == "drift_storm" and now < next_retry_at:
            report["status"] = "drift_storm"
        report.update({
            "repair_budget_used": len(repairs),
            "repair_budget_max": int(budget.get("repair_budget_max") or CLAUDE_HOOK_AUTO_REPAIR_MAX_PER_HOUR),
            "next_retry_at": next_retry_at,
            "persistent_drift": bool(budget.get("persistent_drift")),
            "drift_count_24h": len(drifts),
            "drift_signature": budget.get("last_signature", {}),
            "backup_count": len(list(self.backup_dir.glob("claude-settings.*.json"))),
        })
        return report

    @staticmethod
    def _merge(settings: dict[str, Any]) -> bool:
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise RuntimeError("Claude settings hooks must be a JSON object")
        changed = False
        canonical = {
            "type": "command",
            "command": CLAUDE_HOOK_COMMAND,
            "timeout": 5,
        }
        for event_name in CLAUDE_HOOK_EVENTS:
            rules = hooks.setdefault(event_name, [])
            if not isinstance(rules, list):
                raise RuntimeError(f"Claude Hook {event_name} must be an array")
            found = False
            for rule in rules:
                if not isinstance(rule, dict):
                    raise RuntimeError(f"Claude Hook {event_name} rule must be an object")
                commands = rule.setdefault("hooks", [])
                if not isinstance(commands, list):
                    raise RuntimeError(f"Claude Hook {event_name}.hooks must be an array")
                kept: list[Any] = []
                for command in commands:
                    if not _is_ccc_claude_hook(command):
                        kept.append(command)
                        continue
                    if not found:
                        kept.append(dict(canonical))
                        if command != canonical:
                            changed = True
                        found = True
                    else:
                        changed = True
                if kept != commands:
                    rule["hooks"] = kept
            if found:
                continue
            destination = next(
                (rule for rule in rules if isinstance(rule, dict) and rule.get("matcher") == "*"),
                None,
            )
            if destination is None:
                rules.append({"matcher": "*", "hooks": [dict(canonical)]})
            else:
                destination.setdefault("hooks", []).append(dict(canonical))
            changed = True
        return changed

    def ensure(
        self,
        *,
        repair: bool,
        automatic: bool = True,
        max_repairs_per_hour: int = CLAUDE_HOOK_AUTO_REPAIR_MAX_PER_HOUR,
        drift_warning_per_day: int = CLAUDE_HOOK_DRIFT_WARNING_PER_DAY,
        backup_keep: int = CLAUDE_HOOK_SETTINGS_BACKUP_KEEP,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path, timeout_sec=self.timeout_sec, purpose="Claude settings lock"):
            try:
                raw_bytes = self.path.read_bytes()
            except FileNotFoundError:
                raw_bytes = b"{}\n"
            source_mtime_ns = self.mtime_ns()
            try:
                settings = self._validate_root(json.loads(raw_bytes.decode("utf-8")))
                self._event_counts(settings)
            except (UnicodeDecodeError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f"cannot safely repair Claude settings: {exc}") from exc
            signature = self._drift_signature(settings)
            changed = self._merge(settings)
            now = time.time()
            budget = self._load_repair_state()
            repairs = [
                float(value) for value in budget.get("repairs", [])
                if isinstance(value, (int, float)) and now - float(value) < 3600
            ]
            drifts = [
                dict(value) for value in budget.get("drifts", [])
                if isinstance(value, Mapping)
                and isinstance(value.get("at"), (int, float))
                and now - float(value["at"]) < 86400
            ]
            if changed and repair and not any(
                value.get("source_mtime_ns") == source_mtime_ns
                and value.get("signature") == signature.get("signature")
                for value in drifts[-1:]
            ):
                drifts.append({"at": now, "source_mtime_ns": source_mtime_ns, **signature})
            budget.update({"repairs": repairs, "drifts": drifts, "last_signature": signature})
            persistent_drift = len(drifts) > int(drift_warning_per_day)
            capped = bool(changed and repair and automatic and len(repairs) >= int(max_repairs_per_hour))
            if capped:
                next_retry_at = min(repairs) + 3600 if repairs else now + 3600
                budget.update({
                    "last_status": "drift_storm",
                    "next_retry_at": next_retry_at,
                    "repair_budget_max": int(max_repairs_per_hour),
                    "persistent_drift": persistent_drift,
                })
                atomic_write_json(self.repair_state_path, budget)
                return {
                    "status": "drift_storm",
                    "healthy": False,
                    "path": str(self.path),
                    "event_counts": self._event_counts(settings),
                    "mtime_ns": self.mtime_ns(),
                    "changed": False,
                    "would_change": True,
                    "repair_budget_used": len(repairs),
                    "repair_budget_max": int(max_repairs_per_hour),
                    "next_retry_at": next_retry_at,
                    "persistent_drift": persistent_drift,
                    "drift_count_24h": len(drifts),
                    "drift_signature": signature,
                    "backup_count": len(list(self.backup_dir.glob("claude-settings.*.json"))),
                }
            backup_path = ""
            if changed and repair:
                ensure_app_dir(self.backup_dir)
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup = self.backup_dir / f"claude-settings.{stamp}.json"
                with backup.open("xb") as handle:
                    handle.write(raw_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(backup, 0o600)
                atomic_write_json(self.path, settings)
                os.chmod(self.path, 0o600)
                backup_path = str(backup)
                if automatic:
                    repairs.append(now)
                budget.update({
                    "repairs": repairs,
                    "last_status": "healthy",
                    "next_retry_at": 0.0,
                    "repair_budget_max": int(max_repairs_per_hour),
                    "persistent_drift": persistent_drift,
                })
                atomic_write_json(self.repair_state_path, budget)
                backup_count = self._rotate_backups(int(backup_keep))
            else:
                backup_count = len(list(self.backup_dir.glob("claude-settings.*.json")))
            report = self.inspect() if repair else {
                "status": "repair_needed" if changed else "healthy",
                "healthy": not changed,
                "path": str(self.path),
                "event_counts": self._event_counts(settings),
                "mtime_ns": self.mtime_ns(),
            }
            report["changed"] = bool(changed and repair)
            report["would_change"] = changed
            if backup_path:
                report["backup_path"] = backup_path
            report.update({
                "repair_budget_used": len(repairs),
                "repair_budget_max": int(max_repairs_per_hour),
                "next_retry_at": (min(repairs) + 3600) if repairs else 0.0,
                "persistent_drift": persistent_drift,
                "drift_count_24h": len(drifts),
                "drift_signature": signature,
                "backup_count": backup_count,
            })
            return report


def validate_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("config must be a JSON object")
    schema_version = value.get("schema_version", 2)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RuntimeError("config schema_version must be an integer")
    if schema_version not in SUPPORTED_CONFIG_SCHEMA_VERSIONS:
        raise RuntimeError(f"unsupported config schema_version: {schema_version}")
    merged = default_config()
    merged.update(value)
    # Schema-v2 readers accept old files, but these knobs no longer influence
    # Claude.  ConfigStore.mutate writes the validated result back without
    # them, providing an atomic migration under config.lock.
    for key in DEPRECATED_CLAUDE_CONFIG_KEYS:
        merged.pop(key, None)
    for key in ("targets", "workspace_rules"):
        if not isinstance(merged.get(key), list):
            raise RuntimeError(f"config {key} must be an array")
        if any(not isinstance(item, dict) for item in merged[key]):
            raise RuntimeError(f"config {key} entries must be objects")
    if merged.get("mode") not in {"armed", "dry-run"}:
        raise RuntimeError("config mode must be armed or dry-run")
    if not isinstance(merged.get("global_paused"), bool):
        raise RuntimeError("config global_paused must be boolean")
    for flag in (
        "first_send_immediate",
        "claude_enabled",
        "claude_hook_gap_fallback_enabled",
        "claude_context_enforcement",
    ):
        if not isinstance(merged.get(flag), bool):
            raise RuntimeError(f"config {flag} must be boolean")
    if merged.get("claude_hook_config_policy") not in {"auto_repair", "observe"}:
        raise RuntimeError("config claude_hook_config_policy must be auto_repair or observe")
    if not isinstance(merged.get("message"), str) or not isinstance(merged.get("cmux_path"), str):
        raise RuntimeError("config message and cmux_path must be strings")
    if not isinstance(merged.get("claude_message"), str) or not merged.get("claude_message").strip():
        raise RuntimeError("config claude_message must be a non-empty string")
    if not isinstance(merged.get("manager_surface_id"), str):
        raise RuntimeError("config manager_surface_id must be a string")
    numeric_rules = {
        "poll_interval_sec": (0.0, False),
        "send_interval_sec": (0.0, True),
        "circuit_pause_after": (0.0, True),
        "same_frame_guard_polls": (0.0, True),
        "repeat_send_delay_sec": (0.0, True),
        "claude_working_clear_polls": (1.0, True),
        "claude_background_input_grace_sec": (0.0, True),
        "claude_focused_input_grace_sec": (0.0, True),
        "cmux_unavailable_poll_sec": (0.0, False),
        "workspace_discovery_interval_sec": (0.0, True),
        "claude_event_workers": (1.0, True),
        "claude_repeat_warning_after": (1.0, True),
        "claude_hook_unverified_grace_sec": (0.0, True),
        "claude_hook_unprotected_warn_sec": (1.0, True),
        "claude_hook_unprotected_severe_sec": (1.0, True),
        "claude_hook_unprotected_critical_sec": (1.0, True),
        "claude_unreadable_warn_sec": (1.0, True),
        "claude_hook_sla_sec": (0.0, False),
        "claude_hook_latency_window": (1.0, True),
        "claude_hook_gap_confirm_polls": (2.0, True),
        "claude_hook_gap_retry_after_sec": (0.0, True),
        "claude_hook_gap_max_retries": (0.0, True),
        "claude_hook_auto_repair_max_per_hour": (1.0, True),
        "claude_hook_drift_warning_per_day": (1.0, True),
        "claude_hook_settings_backup_keep": (1.0, True),
        "claude_context_warning_percent": (1.0, True),
        "claude_context_start_grace_sec": (1.0, True),
        "claude_context_stall_sec": (1.0, True),
        "claude_context_absolute_timeout_sec": (1.0, True),
        "claude_submit_confirm_timeout_sec": (1.0, True),
        "claude_submit_retry_enter_sec": (0.1, True),
        "log_channel_check_interval_sec": (1.0, True),
    }
    for key, (minimum, inclusive) in numeric_rules.items():
        number = merged.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise RuntimeError(f"config {key} must be numeric")
        if number < minimum or (not inclusive and number == minimum):
            operator = ">=" if inclusive else ">"
            raise RuntimeError(f"config {key} must be {operator} {minimum:g}")
    for key in (
        "claude_event_workers",
        "claude_repeat_warning_after",
        "claude_hook_latency_window",
        "claude_hook_gap_confirm_polls",
        "claude_hook_gap_max_retries",
        "claude_hook_auto_repair_max_per_hour",
        "claude_hook_drift_warning_per_day",
        "claude_hook_settings_backup_keep",
        "claude_context_warning_percent",
    ):
        if isinstance(merged.get(key), bool) or not isinstance(merged.get(key), int):
            raise RuntimeError(f"config {key} must be an integer")
    if int(merged["claude_context_warning_percent"]) > 100:
        raise RuntimeError("config claude_context_warning_percent must be <= 100")
    return merged


class ConfigStore:
    """Serializes short, validated config mutations across CLI/TUI/daemon writers."""

    def __init__(self, path: Path, *, lock_path: Path | None = None, timeout_sec: float = 2.0):
        self.path = path
        self.lock_path = lock_path or (path.parent / "config.lock")
        self.timeout_sec = timeout_sec

    def load(self) -> dict[str, Any]:
        return validate_config(load_json(self.path, default_config()))

    def migrate_deprecated(self) -> bool:
        """Remove obsolete knobs and only the old automatic Claude pause."""

        with FileLock(self.lock_path, timeout_sec=self.timeout_sec, purpose="config migration"):
            raw = load_json(self.path, default_config())
            if not isinstance(raw, dict):
                return False
            migrated = dict(raw)
            changed = False
            for key in DEPRECATED_CLAUDE_CONFIG_KEYS:
                if key in migrated:
                    migrated.pop(key, None)
                    changed = True
            targets = migrated.get("targets", [])
            if isinstance(targets, list):
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    reason = str(target.get("paused_reason") or "")
                    if bool(target.get("paused")) and reason.startswith(LEGACY_CLAUDE_AUTO_PAUSE_PREFIX):
                        target["paused"] = False
                        target.pop("paused_reason", None)
                        changed = True
            if not changed:
                return False
            migrated = validate_config(migrated)
            atomic_write_json(self.path, migrated)
            return True

    def mtime_ns(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def mutate(self, mutator: Any) -> tuple[dict[str, Any], Any, int | None]:
        with FileLock(self.lock_path, timeout_sec=self.timeout_sec, purpose="config lock"):
            config = self.load()
            result = mutator(config)
            config["schema_version"] = 2
            config = validate_config(config)
            atomic_write_json(self.path, config)
            return config, result, self.mtime_ns()


def _valid_claude_event(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("version") == 1
        and isinstance(value.get("event_id"), str)
        and value.get("event_id")
        and value.get("event_name") in {"SessionStart", "UserPromptSubmit", "Stop", "StopFailure"}
        and isinstance(value.get("created_at"), (int, float))
    )


class ClaudeEventLedger:
    """Durable at-most-once outcomes for Hook events.

    ``reserved`` is written before cmux send.  If the daemon dies in the tiny
    gap after that write, restart deliberately refuses to send the event again:
    avoiding a duplicate queued prompt is safer than guessing whether cmux
    accepted the first call.
    """

    def __init__(self, path: Path = DEFAULT_CLAUDE_EVENT_LEDGER_PATH):
        self.path = path
        self._lock = threading.RLock()
        value = load_json(path, {"version": 1, "events": {}})
        events = value.get("events", {}) if isinstance(value, Mapping) else {}
        self.events: dict[str, dict[str, Any]] = {
            str(key): dict(item)
            for key, item in events.items()
            if isinstance(key, str) and isinstance(item, Mapping)
        }

    def contains(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self.events

    def known_ids(self) -> set[str]:
        with self._lock:
            return set(self.events)

    @staticmethod
    def _identity(event: Mapping[str, Any]) -> tuple[str, ...]:
        """Return the lifecycle identity carried by an event.

        ``event_id`` is intentionally not part of this tuple.  A UUID is the
        transport de-duplication key, while the fields below explain *which*
        episode owns that key.  Keeping the two concepts separate lets the
        ledger detect an old deterministic fallback id being reused by a new
        episode instead of treating the new Stop as a duplicate.
        """

        return (
            str(event.get("event_name") or ""),
            str(event.get("surface_id") or ""),
            str(event.get("session_id") or ""),
            str(event.get("episode_id") or ""),
            str(event.get("process_generation") or ""),
            str(event.get("attempt_number") or ""),
            str(event.get("evidence_fingerprint") or ""),
            "1" if bool(event.get("synthetic_fallback")) else "",
        )

    @classmethod
    def _row_identity(cls, row: Mapping[str, Any]) -> tuple[str, ...]:
        return cls._identity(row)

    @staticmethod
    def _is_active_status(status: str) -> bool:
        """Statuses which must survive ledger-capacity pruning."""

        return status in CLAUDE_LEDGER_PROVISIONAL_STATUSES or (
            status.startswith("deferred_")
            and status not in CLAUDE_DEFERRED_TERMINAL_STATUSES
        )

    def claim_detailed(self, event: Mapping[str, Any], *, status: str = "handling") -> str:
        """Claim an event and return a semantic result code.

        The old boolean API collapsed two materially different cases: replaying
        the same event (safe to ignore) and reusing an id from a prior fallback
        episode (must be retried with a new id).  This method keeps the atomic
        check+reserve while exposing that distinction to the fallback caller.
        """

        with self._lock:
            event_id = str(event["event_id"])
            existing = self.events.get(event_id)
            if existing is not None:
                incoming_identity = self._identity(event)
                existing_identity = self._row_identity(existing)
                # Rows written by pre-episode versions have no provenance fields.
                # An exact event-id replay remains a duplicate for compatibility;
                # a provenance-bearing event that differs is a real collision.
                if all(not value for value in existing_identity[3:]) and all(
                    not value for value in incoming_identity[3:]
                ):
                    return CLAUDE_DUPLICATE_SAME_EVENT
                if incoming_identity == existing_identity:
                    existing_status = str(existing.get("status") or "")
                    if incoming_identity[3] and not self._is_active_status(existing_status):
                        return CLAUDE_TERMINAL_CONFLICT
                    return (
                        CLAUDE_DUPLICATE_SAME_EPISODE
                        if incoming_identity[3]
                        else CLAUDE_DUPLICATE_SAME_EVENT
                    )
                return CLAUDE_HISTORICAL_ID_COLLISION
            self.events[event_id] = self._row_from_event(event, status=status, detail="atomic claim")
            self._prune()
            atomic_write_json(self.path, {"version": 1, "events": self.events})
            return CLAUDE_CLAIMED

    def claim_if_absent(self, event: Mapping[str, Any], *, status: str = "handling") -> bool:
        """Backward-compatible boolean wrapper around :meth:`claim_detailed`."""

        return self.claim_detailed(event, status=status) == CLAUDE_CLAIMED

    @staticmethod
    def _row_from_event(
        event: Mapping[str, Any],
        *,
        status: str,
        detail: str,
        handled_at: float | None = None,
    ) -> dict[str, Any]:
        row = {
            "status": status,
            "handled_at": time.time() if handled_at is None else handled_at,
            "event_name": str(event.get("event_name") or ""),
            "surface_id": str(event.get("surface_id") or ""),
            "session_id": str(event.get("session_id") or ""),
            "detail": detail[:160],
        }
        # Preserve provenance only when supplied.  This keeps old ledger rows
        # readable while making all new fallback rows collision-detectable.
        for key in (
            "episode_id", "process_generation", "attempt_number",
            "evidence_fingerprint", "synthetic_fallback", "agent_pid",
        ):
            if key in event and event.get(key) not in (None, ""):
                row[key] = event[key]
        return row

    def _prune(self) -> None:
        """Drop old terminal rows without ever evicting active evidence."""

        if len(self.events) <= CLAUDE_EVENT_LEDGER_LIMIT:
            return
        protected: dict[str, dict[str, Any]] = {}
        removable: list[tuple[str, dict[str, Any]]] = []
        for event_id, row in self.events.items():
            status = str(row.get("status") or "")
            if self._is_active_status(status):
                protected[event_id] = row
            else:
                removable.append((event_id, row))
        removable.sort(key=lambda item: float(item[1].get("handled_at") or 0.0), reverse=True)
        room = max(0, CLAUDE_EVENT_LEDGER_LIMIT - len(protected))
        self.events = {**protected, **dict(removable[:room])}

    def status_of(self, event_id: str) -> str:
        """Current status, or "" when unknown.

        ``mark`` replaces the whole row, so a caller that wants to advance a
        provisional status without clobbering a terminal one has to read first.
        Needed by the deferred-slot cleanup: the success path marks ``sent`` and
        *then* releases the slot, so writing a terminal state unconditionally
        would overwrite the record of the delivery that actually happened.
        """

        with self._lock:
            row = self.events.get(str(event_id))
            return str(row.get("status") or "") if isinstance(row, Mapping) else ""

    def mark(
        self,
        event: Mapping[str, Any],
        status: str,
        *,
        detail: str = "",
        expected_status: str | None = None,
    ) -> bool:
        with self._lock:
            event_id = str(event["event_id"])
            current = self.events.get(event_id)
            current_status = str(current.get("status") or "") if isinstance(current, Mapping) else ""
            if expected_status is not None and current_status != expected_status:
                return False
            # Keep the identity claimed before preflight.  ``mark`` used to
            # replace the whole row, which erased episode provenance and made a
            # later id collision indistinguishable from a duplicate.
            row = dict(current) if isinstance(current, Mapping) else {}
            row.update(self._row_from_event(event, status=status, detail=detail))
            self.events[event_id] = row
            self._prune()
            atomic_write_json(self.path, {"version": 1, "events": self.events})
            return True


class ClaudeEventInbox:
    """Durable journal replay plus a Unix-datagram wakeup for low latency."""

    def __init__(
        self,
        journal_path: Path = EVENT_JOURNAL_PATH,
        socket_path: Path = EVENT_SOCKET_PATH,
    ):
        self.journal_path = journal_path
        self.socket_path = socket_path
        self.items: queue.Queue[dict[str, Any]] = queue.Queue()
        self.wakeup = threading.Event()
        self.stop = threading.Event()
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self._queued_ids: set[str] = set()
        self._ids_lock = threading.Lock()

    def _enqueue(
        self,
        event: Mapping[str, Any],
        known_ids: set[str] | None = None,
        *,
        source: str = "socket",
    ) -> None:
        if not _valid_claude_event(event):
            return
        event_id = str(event["event_id"])
        if known_ids is not None and event_id in known_ids:
            return
        with self._ids_lock:
            if event_id in self._queued_ids:
                return
            self._queued_ids.add(event_id)
        queued = dict(event)
        queued.setdefault("_inbox_at", time.time())
        queued.setdefault("_inbox_source", source)
        self.items.put(queued)
        self.wakeup.set()

    def replay_journal(self, known_ids: set[str]) -> None:
        try:
            with self.journal_path.open(encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    lines = handle.readlines()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return
        cutoff = time.time() - CLAUDE_EVENT_MAX_AGE_SEC
        for line in lines[-CLAUDE_EVENT_LEDGER_LIMIT:]:
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if _valid_claude_event(event) and float(event["created_at"]) >= cutoff:
                self._enqueue(event, known_ids, source="journal_replay")

    def start(self, known_ids: set[str]) -> None:
        ensure_app_dir(self.socket_path.parent)
        self.replay_journal(known_ids)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        sock.settimeout(0.25)
        self.sock = sock

        def listen() -> None:
            while not self.stop.is_set():
                try:
                    data = sock.recv(60_000)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    event = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                self._enqueue(event, source="socket")

        self.thread = threading.Thread(target=listen, name="claude-ccc-events", daemon=True)
        self.thread.start()

    def get_nowait(self) -> dict[str, Any] | None:
        try:
            event = self.items.get_nowait()
        except queue.Empty:
            self.wakeup.clear()
            return None
        with self._ids_lock:
            self._queued_ids.discard(str(event.get("event_id") or ""))
        if self.items.empty():
            self.wakeup.clear()
        return event

    def wait(self, timeout: float) -> None:
        self.wakeup.wait(max(0.0, timeout))

    def close(self) -> None:
        self.stop.set()
        if self.sock is not None:
            self.sock.close()
        if self.thread is not None:
            self.thread.join(timeout=1)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()


class ClaudeEventWorkerPool:
    """Dispatch Hook events immediately while preserving per-surface order.

    A slow full-screen polling RPC must not hold a lifecycle event for several
    seconds.  Stable sharding gives each surface one serial queue while allowing
    unrelated Claude sessions to run their preflights concurrently.
    """

    def __init__(
        self,
        inbox: ClaudeEventInbox,
        handler: Callable[[Mapping[str, Any], "CmuxClient"], None],
        client_factory: Callable[[], "CmuxClient"],
        *,
        workers: int = CLAUDE_EVENT_WORKERS,
    ):
        self.inbox = inbox
        self.handler = handler
        self.client_factory = client_factory
        self.worker_count = max(1, int(workers))
        self.stop = threading.Event()
        self.queues: list[queue.Queue[dict[str, Any] | None]] = [
            queue.Queue() for _ in range(self.worker_count)
        ]
        self.dispatcher: threading.Thread | None = None
        self.threads: list[threading.Thread] = []

    def _shard(self, event: Mapping[str, Any]) -> int:
        surface_id = str(event.get("surface_id") or event.get("event_id") or "")
        digest = hashlib.sha256(surface_id.encode("utf-8", errors="replace")).digest()
        return int.from_bytes(digest[:4], "big") % self.worker_count

    def start(self) -> None:
        for index, work_queue in enumerate(self.queues):
            client = self.client_factory()

            def work(
                assigned: queue.Queue[dict[str, Any] | None] = work_queue,
                assigned_client: "CmuxClient" = client,
            ) -> None:
                while not self.stop.is_set():
                    try:
                        event = assigned.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if event is None:
                        return
                    event.setdefault("_worker_started_at", time.time())
                    self.handler(event, assigned_client)

            thread = threading.Thread(
                target=work,
                name=f"claude-ccc-worker-{index}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        def dispatch() -> None:
            while not self.stop.is_set():
                event = self.inbox.get_nowait()
                if event is None:
                    self.inbox.wait(0.05)
                    continue
                event.setdefault("_dispatched_at", time.time())
                self.queues[self._shard(event)].put(event)

        self.dispatcher = threading.Thread(
            target=dispatch,
            name="claude-ccc-dispatcher",
            daemon=True,
        )
        self.dispatcher.start()

    def close(self) -> None:
        self.stop.set()
        self.inbox.wakeup.set()
        if self.dispatcher is not None:
            self.dispatcher.join(timeout=1)
        for work_queue in self.queues:
            work_queue.put(None)
        for thread in self.threads:
            thread.join(timeout=1)


class CmuxClient:
    def __init__(self, binary: str = DEFAULT_CMUX, runner: Any = subprocess.run):
        self.binary = binary
        self.runner = runner

    def _run(self, args: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        command = [self.binary, *args]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CmuxError(str(exc)) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise CmuxError(f"cmux {' '.join(args[:2])} failed: {detail}")
        return result

    def ping(self) -> bool:
        try:
            result = self._run(["ping"], timeout=3)
        except CmuxError as exc:
            logging.getLogger(APP_NAME).info("cmux ping failed: %s", exc)
            return False
        return result.returncode == 0

    def capabilities(self) -> Mapping[str, Any]:
        result = self._run(["--json", "capabilities"])
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise IncompatibleError("capabilities is not JSON") from exc
        capabilities = set(value.get("capabilities", [])) if isinstance(value, Mapping) else set()
        methods = set(value.get("methods", [])) if isinstance(value, Mapping) else set()
        missing = (REQUIRED_CAPABILITIES - capabilities) | (REQUIRED_METHODS - methods)
        if missing:
            raise IncompatibleError(f"cmux capabilities missing: {', '.join(sorted(missing))}")
        return value

    def tree(self) -> Mapping[str, Any]:
        result = self._run(["--json", "--id-format", "both", "tree", "--all"])
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("tree is not JSON") from exc
        if not isinstance(value, Mapping):
            raise CmuxError("tree JSON is not an object")
        return value

    def top(self, workspace_id: str) -> Mapping[str, Any]:
        if not workspace_id:
            raise CmuxError("cmux top requires workspace UUID")
        # --id-format both is what puts the stable surface UUID in the payload;
        # without it cmux returns refs only and process labels can only be
        # joined on a handle that renumbers.
        result = self._run(
            ["--json", "--id-format", "both", "top", "--workspace", workspace_id, "--processes"],
            timeout=12,
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("top is not JSON") from exc
        if not isinstance(value, Mapping):
            raise CmuxError("top JSON is not an object")
        return value

    def top_all(self) -> Mapping[str, Any]:
        result = self._run(["--json", "--id-format", "both", "top", "--all", "--processes"], timeout=20)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("top --all is not JSON") from exc
        if not isinstance(value, Mapping):
            raise CmuxError("top --all JSON is not an object")
        return value

    def identify(self) -> Mapping[str, Any]:
        result = self._run(["--json", "--id-format", "both", "identify"])
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("identify is not JSON") from exc
        if not isinstance(value, Mapping):
            raise CmuxError("identify JSON is not an object")
        return value

    def new_dock_surface(self, window_id: str) -> str:
        if not window_id:
            raise CmuxError("new Dock surface requires window UUID")
        result = self._run([
            "--json", "--id-format", "both", "new-surface",
            "--type", "terminal", "--placement", "dock",
            "--window", window_id, "--focus", "false",
        ])
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("new Dock surface response is not JSON") from exc
        surface_id = _find_string_key(value, "dock_surface_id")
        if not surface_id:
            raise CmuxError("new Dock surface response missing dock_surface_id")
        return surface_id

    def rename_surface(self, window_id: str, surface_id: str, title: str) -> None:
        self._run([
            "rename-tab", "--window", window_id,
            "--surface", surface_id, "--title", title,
        ])

    def initialize_dock_surface(
        self,
        window_id: str,
        surface_id: str,
        title: str,
        command: str,
        *,
        attempts: int = 20,
        delay_sec: float = 0.1,
    ) -> None:
        """Wait briefly for a newly-created Dock surface to become addressable."""

        last_error: CmuxError | None = None
        for attempt in range(attempts):
            try:
                # Dock terminal surfaces are addressable for send before the
                # generic tab-action resolver accepts rename-tab.  Renaming
                # is cosmetic, so never make a usable Supervisor depend on it.
                try:
                    self.rename_surface(window_id, surface_id, title)
                except CmuxError as rename_error:
                    rename_detail = str(rename_error).lower()
                    if "not_found" not in rename_detail and "not found" not in rename_detail:
                        raise
                self.send_surface(surface_id, command)
                return
            except CmuxError as exc:
                detail = str(exc).lower()
                if "not_found" not in detail and "not found" not in detail:
                    raise
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(delay_sec)
        raise last_error or CmuxError(f"Dock surface {surface_id} did not become ready")

    def show_dock(self, window_id: str) -> None:
        self._run(["right-sidebar", "set", "dock", "--window", window_id, "--no-focus"])

    def read_surface(self, surface_id: str) -> str:
        if not surface_id:
            raise CmuxError("read_surface requires explicit surface UUID")
        return self._run(["read-screen", "--surface", surface_id], timeout=5).stdout

    def send_surface(self, surface_id: str, text: str) -> None:
        if not surface_id:
            raise CmuxError("send_surface requires explicit surface UUID")
        self._run(["send", "--surface", surface_id, f"{text}\n"], timeout=8)

    def respawn_surface(self, window_id: str, surface_id: str, command: str) -> None:
        if not window_id or not surface_id:
            raise CmuxError("respawn_surface requires explicit window and surface UUIDs")
        self._run([
            "respawn-pane", "--window", window_id, "--surface", surface_id,
            "--command", command,
        ], timeout=8)

    def read_screen(self, workspace_id: str, surface_id: str) -> str:
        args = ["read-screen", "--workspace", workspace_id, "--surface", surface_id]
        if any(flag in args for flag in ("--lines", "--scrollback")):
            raise RuntimeError("read-screen must never request scrollback")
        result = self._run(args, timeout=8)
        return result.stdout

    def replay(self, workspace_id: str, surface_id: str) -> Mapping[str, Any]:
        if not workspace_id or not surface_id:
            raise IncompatibleError("terminal.replay requires workspace_id and surface_id")
        params = json.dumps(
            {"workspace_id": workspace_id, "surface_id": surface_id, "anchor": "viewport"},
            ensure_ascii=False,
        )
        result = self._run(["--json", "rpc", "terminal.replay", params], timeout=12)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise IncompatibleError("terminal.replay is not JSON") from exc
        if not isinstance(value, Mapping):
            raise IncompatibleError("terminal.replay response is not an object")
        return value

    def send(self, workspace_id: str, surface_id: str, message: str) -> None:
        # Keep the newline in the argv value. cmux maps it to Enter without
        # requiring focus or a separate send-key operation.
        self._reject_unapproved_command(message)
        self._run(["send", "--workspace", workspace_id, "--surface", surface_id, f"{message}\n"], timeout=8)

    def send_text(self, workspace_id: str, surface_id: str, message: str) -> None:
        """Write text only; do not rely on a newline being interpreted as Enter."""

        if not workspace_id or not surface_id:
            raise CmuxError("send_text requires explicit workspace and surface UUIDs")
        self._reject_unapproved_command(message)
        self._run(["send", "--workspace", workspace_id, "--surface", surface_id, message], timeout=8)

    def send_key(self, workspace_id: str, surface_id: str, key: str) -> None:
        """Send a named key to an explicitly addressed surface, never focused UI."""

        if not workspace_id or not surface_id:
            raise CmuxError("send_key requires explicit workspace and surface UUIDs")
        if key != "enter":
            raise RuntimeError(f"Claude submit only permits Enter, got {key!r}")
        self._run([
            "send-key", "--workspace", workspace_id, "--surface", surface_id, key,
        ], timeout=8)

    @staticmethod
    def _reject_unapproved_command(message: str) -> None:
        """The watchdog is a text nudge, never a Claude slash-command runner.

        Only the *first* non-space character decides whether this is a command,
        so ``完成，建议检查 usage: /context`` and a sentence warning against
        ``/clear`` both stay sendable.
        """

        stripped = message.strip()
        if not stripped.startswith("/"):
            return
        raise RuntimeError(
            f"refusing to send slash command: {stripped.splitlines()[0][:40]!r}"
        )


def _object_id(value: Mapping[str, Any], prefix: str) -> str | None:
    for key in (f"{prefix}_id", f"{prefix}Id", "id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and (key != "id" or prefix in str(value.get("type", value.get("kind", ""))).lower()):
            return candidate
    return None


def _find_string_key(value: Any, key: str) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        for child in value.values():
            found = _find_string_key(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string_key(child, key)
            if found:
                return found
    return None


def find_surface(tree: Mapping[str, Any], selector: str) -> dict[str, str]:
    found: list[dict[str, str]] = []

    def visit(value: Any, workspace: str | None = None) -> None:
        if isinstance(value, Mapping):
            kind = str(value.get("type", value.get("kind", ""))).lower()
            ref = value.get("ref") or value.get("surface_ref") or value.get("surfaceRef")
            ref_text = str(ref or "")
            current_workspace = workspace
            if (
                ref_text.startswith("workspace:")
                or "workspace" in kind
                or "workspace_id" in value
            ):
                current_workspace = (
                    value.get("workspace_id")
                    or value.get("workspaceId")
                    or (value.get("id") if ref_text.startswith("workspace:") or "workspace" in kind else None)
                    or workspace
                )
            sid = value.get("surface_id") or value.get("surfaceId")
            if ref_text.startswith("surface:"):
                sid = sid or value.get("id")
            if ("surface" in kind or kind == "terminal") and not sid:
                sid = value.get("id")
            if isinstance(sid, str):
                ref_text = str(ref or "")
                if sid == selector or ref_text == selector or ref_text == f"surface:{selector}":
                    if value.get("dock_scope") is not None:
                        raise CmuxError(f"Dock surface cannot be monitored: {selector}")
                    found.append({
                        "surface_id": sid,
                        "workspace_id": str(value.get("workspace_id") or value.get("workspaceId") or current_workspace or ""),
                        "ref": ref_text,
                        "title": str(value.get("title") or value.get("name") or ""),
                    })
            for child in value.values():
                visit(child, current_workspace)
        elif isinstance(value, list):
            for child in value:
                visit(child, workspace)

    visit(tree)
    if not found:
        raise CmuxError(f"surface not found: {selector}")
    result = found[0]
    if not result["workspace_id"]:
        raise CmuxError(f"workspace UUID not found for surface: {selector}")
    return result


def find_workspace(tree: Mapping[str, Any], selector: str) -> dict[str, str]:
    for window in tree.get("windows", []):
        if not isinstance(window, Mapping):
            continue
        for workspace in window.get("workspaces", []):
            if not isinstance(workspace, Mapping):
                continue
            workspace_id = str(workspace.get("id") or workspace.get("workspace_id") or "")
            ref = str(workspace.get("ref") or workspace.get("workspace_ref") or "")
            if selector in {workspace_id, ref} or ref == f"workspace:{selector}":
                if not workspace_id:
                    break
                return {
                    "workspace_id": workspace_id,
                    "ref": ref,
                    "title": str(workspace.get("title") or workspace.get("name") or ""),
                }
    raise CmuxError(f"workspace not found: {selector}")


def main_surface_records(
    tree: Mapping[str, Any], *, allow_ref_only: bool = False,
) -> list[dict[str, str]]:
    """Return main-area surfaces only; Dock surfaces are never candidates."""

    records: list[dict[str, str]] = []
    for window in tree.get("windows", []):
        if not isinstance(window, Mapping):
            continue
        window_id = str(window.get("id") or window.get("window_id") or "")
        window_ref = str(window.get("ref") or window.get("window_ref") or "")
        for workspace in window.get("workspaces", []):
            if not isinstance(workspace, Mapping):
                continue
            workspace_id = str(workspace.get("id") or workspace.get("workspace_id") or "")
            workspace_ref = str(workspace.get("ref") or workspace.get("workspace_ref") or "")
            workspace_title = str(workspace.get("title") or workspace.get("name") or "")
            panes = workspace.get("panes", [])
            if not isinstance(panes, list):
                continue
            for pane in panes:
                if not isinstance(pane, Mapping) or pane.get("dock_scope") is not None:
                    continue
                pane_id = str(pane.get("id") or pane.get("pane_id") or "")
                pane_ref = str(pane.get("ref") or pane.get("pane_ref") or "")
                for surface in pane.get("surfaces", []):
                    if not isinstance(surface, Mapping) or surface.get("dock_scope") is not None:
                        continue
                    surface_ref = str(surface.get("ref") or surface.get("surface_ref") or "")
                    # Some cmux tree payloads expose a ref without the stable
                    # UUID.  Keep the row visible for rendering and inspection;
                    # UUID remains preferred whenever it is present.
                    surface_id = str(surface.get("id") or surface.get("surface_id") or "")
                    # Ref-only records are suitable for a read-only display,
                    # but must never enter watcher discovery by default.
                    if not surface_id and allow_ref_only:
                        surface_id = surface_ref
                    if not surface_id or not surface_ref:
                        continue
                    records.append({
                        "surface_id": surface_id,
                        "workspace_id": workspace_id,
                        "window_id": window_id,
                        "pane_id": pane_id,
                        "ref": surface_ref,
                        "workspace_ref": workspace_ref,
                        "window_ref": window_ref,
                        "pane_ref": pane_ref,
                        "workspace_title": workspace_title,
                        "title": str(surface.get("title") or surface.get("name") or ""),
                        "type": str(surface.get("type") or surface.get("kind") or ""),
                    })
    return records


def find_main_surface(tree: Mapping[str, Any], selector: str) -> dict[str, str]:
    for record in main_surface_records(tree):
        if selector in {record["surface_id"], record["ref"]} or record["ref"] == f"surface:{selector}":
            return record
    raise CmuxError(f"main-area surface not found: {selector}")


def dock_surface_records(tree: Mapping[str, Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []

    def visit(value: Any, window_id: str = "", workspace_id: str = "") -> None:
        if isinstance(value, Mapping):
            next_window = window_id
            next_workspace = workspace_id
            ref = str(value.get("ref") or value.get("surface_ref") or "")
            if ref.startswith("window:"):
                next_window = str(value.get("id") or value.get("window_id") or window_id)
            if ref.startswith("workspace:"):
                next_workspace = str(value.get("id") or value.get("workspace_id") or workspace_id)
            if value.get("dock_scope") is not None and ref.startswith("surface:"):
                surface_id = str(value.get("id") or value.get("surface_id") or "")
                if surface_id:
                    records.append({
                        "surface_id": surface_id,
                        "workspace_id": str(value.get("workspace_id") or next_workspace),
                        "window_id": str(value.get("window_id") or next_window),
                        "ref": ref,
                        "title": str(value.get("title") or value.get("name") or ""),
                        "dock_scope": str(value.get("dock_scope") or ""),
                    })
            for child in value.values():
                visit(child, next_window, next_workspace)
        elif isinstance(value, list):
            for child in value:
                visit(child, window_id, workspace_id)

    visit(tree)
    unique: dict[str, dict[str, str]] = {}
    for record in records:
        unique[record["surface_id"]] = record
    return list(unique.values())


def is_dock_surface(tree: Mapping[str, Any], surface_id: str) -> bool:
    return any(record["surface_id"] == surface_id for record in dock_surface_records(tree))


def is_surface_focused(tree: Mapping[str, Any], surface_id: str) -> bool:
    """Whether this exact surface currently owns the user's main-area focus."""

    for key in ("active", "focused"):
        identity = tree.get(key)
        if isinstance(identity, Mapping) and identity.get("surface_id"):
            # cmux tree marks one selected tab inside *each* workspace with a
            # nested focused=true. Only the top-level active/focused identity
            # is the global keyboard target.
            return str(identity.get("surface_id")) == surface_id
    # Compatibility fallback for older tree payloads without a top-level
    # identity. It is intentionally used only when no authoritative identity
    # exists, because several nested surfaces may be focused simultaneously.
    for value in _walk_objects(tree):
        ref = str(value.get("ref") or value.get("surface_ref") or "")
        candidate = str(value.get("surface_id") or (value.get("id") if ref.startswith("surface:") else ""))
        if candidate == surface_id and bool(value.get("focused")):
            return True
    return False


def workspace_surface_records(tree: Mapping[str, Any], workspace_id: str) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for window in tree.get("windows", []):
        if not isinstance(window, Mapping):
            continue
        for workspace in window.get("workspaces", []):
            if not isinstance(workspace, Mapping) or str(workspace.get("id")) != workspace_id:
                continue
            for pane in workspace.get("panes", []):
                if not isinstance(pane, Mapping):
                    continue
                pane_id = str(pane.get("id") or "")
                for surface in pane.get("surfaces", []):
                    if not isinstance(surface, Mapping):
                        continue
                    if surface.get("dock_scope") is not None or pane.get("dock_scope") is not None:
                        continue
                    surface_id = str(surface.get("id") or surface.get("surface_id") or "")
                    ref = str(surface.get("ref") or surface.get("surface_ref") or "")
                    if not surface_id or not ref:
                        continue
                    records[ref] = {
                        "surface_id": surface_id,
                        "workspace_id": workspace_id,
                        "ref": ref,
                        "pane_id": pane_id,
                        "title": str(surface.get("title") or surface.get("name") or ""),
                    }
    return records


def _walk_objects(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def _program_stem(value: str) -> str:
    """Lowercased program name without a trailing .exe.

    cmux reports Claude Code as ``claude.exe`` on this machine, which an exact
    ``== "claude"`` test missed, so seven panes were labelled 其他.  Only the
    ``.exe`` suffix is stripped: the match stays exact otherwise, because
    ``codex-code-mode`` must not count as ``codex``.
    """
    stem = value.strip().lower()
    return stem[:-4] if stem.endswith(".exe") else stem


SHELL_PROGRAMS = {"zsh", "bash", "sh", "fish", "dash", "tcsh", "ksh", "login"}
# Plumbing that nearly every pane carries; it must not turn "just a shell" into
# "something else is running".  Kept deliberately tiny: anything the user
# actually started should stay visible in the label.
TRANSPARENT_PROGRAMS = {"sleep"}


def _is_codex_process(stem: str, basename: str, path: str) -> bool:
    """The one rule that must never loosen: it gates workspace-pool discovery.

    ``codex-code-mode`` ships in the same directory and is a different program,
    so the match stays exact on both the reported name and the path basename.
    """
    return stem == "codex" and (not path or basename == "codex")


# Ordered most-specific first.  ``path`` is the primary evidence: cmux reports
# process names through the macOS 15-character comm limit, so "grok-1.0.4-maco"
# is truncated *and* carries a version, and no exact name test can ever match
# it.  The full path does not have either problem.
AGENT_RULES: tuple[tuple[str, str, Any], ...] = (
    ("codex", "Codex", _is_codex_process),
    ("claude", "Claude", lambda stem, basename, path:
        stem == "claude" or basename == "claude" or "claude-code/" in path.lower()),
    ("grok", "grok", lambda stem, basename, path:
        stem == "grok" or basename.startswith("grok-") or "/.grok/" in path.lower()),
    ("copilot", "Copilot", lambda stem, basename, path:
        stem == "copilot" or basename == "copilot" or "copilot-cli/" in path.lower()),
    ("gh", "gh", lambda stem, basename, path: stem == "gh" or basename == "gh"),
)


def classify_processes(
    processes: Iterable[Mapping[str, Any]],
    *,
    foreground_pgids: Iterable[int] = (),
) -> dict[str, Any]:
    """Identify the agent CLI in one surface's process list.

    Returns the *identity* only — what is running — never a monitoring state.
    ``shell`` means "we saw the processes and there is no agent, just a shell";
    ``unknown`` means "no process information at all".  Keeping those apart
    matters: the first is a Codex that exited, the second is a missing reading.
    """
    names: list[str] = []
    matched: set[str] = set()
    matched_processes: dict[str, list[tuple[int, int, int]]] = {}
    foreground: set[int] = set()
    for value in foreground_pgids:
        try:
            pgid = int(value)
        except (TypeError, ValueError):
            continue
        if pgid > 0:
            foreground.add(pgid)
    for process in processes:
        if process.get("kind") != "process":
            continue
        name = str(process.get("name") or "").strip()
        path = str(process.get("path") or "")
        stem = _program_stem(name)
        basename = _program_stem(Path(path).name) if path else ""
        if name and name.lower() not in {value.lower() for value in names}:
            names.append(name)
        for kind, _, matches in AGENT_RULES:
            if matches(stem, basename, path):
                matched.add(kind)
                try:
                    pid = int(process.get("pid") or 0)
                    ppid = int(process.get("ppid") or 0)
                    pgid = int(process.get("pgid") or 0)
                except (TypeError, ValueError):
                    pid = ppid = pgid = 0
                if pid > 0:
                    matched_processes.setdefault(kind, []).append((pid, ppid, pgid))
                break
    for kind, label, _ in AGENT_RULES:
        if kind in matched:
            matches = matched_processes.get(kind, [])
            matched_pids = {pid for pid, _, _ in matches}
            foreground_matches = sorted(
                pid for pid, _, pgid in matches if pid in foreground or pgid in foreground
            )
            roots = sorted(pid for pid, ppid, _ in matches if ppid not in matched_pids)
            agent_pid = (
                foreground_matches[0]
                if foreground_matches
                else roots[0] if roots
                else min(matched_pids) if matched_pids
                else 0
            )
            return {
                "agent_kind": kind,
                "summary": label,
                "agent_pid": agent_pid,
                # Hooks can run in a Claude child process (for example the
                # bg-pty session) while cmux reports the launcher as the
                # foreground/root PID.  Keep every matched PID so event
                # binding can verify membership in this exact surface's
                # process tree without trusting an arbitrary PID.
                "agent_pids": sorted(matched_pids),
            }
    if not names:
        return {"agent_kind": "unknown", "summary": "无进程信息", "agent_pids": []}
    telling = [value for value in names if _program_stem(value) not in TRANSPARENT_PROGRAMS]
    if not telling or all(_program_stem(value) in SHELL_PROGRAMS for value in telling):
        return {"agent_kind": "shell", "summary": "/".join(telling[:2] or names[:1]), "agent_pids": []}
    return {"agent_kind": "other", "summary": "/".join(telling[:2]), "agent_pids": []}


def classify_surface_processes(top: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Label every surface by its running processes, keyed by stable surface UUID.

    The label is advisory.  It describes what a surface *looks* like so the
    management surface can warn before registering one; it never by itself
    decides whether a surface may be registered, and it is never the gate that
    permits a continuation send.  The send gate is the live-screen fingerprint
    in ``classify_grid``.

    ``ref`` is deliberately not the preferred join key.  cmux renumbers refs
    whenever surfaces open or close, and ``tree`` and ``top`` are two separate
    calls, so a ref-keyed join can bind one surface's processes onto another
    surface's UUID.  Every surface in ``cmux top`` currently carries the stable
    UUID, so that is what we key on.

    Entries that arrive without a UUID are keyed by ``ref`` instead, and callers
    look the UUID up first.  A UUID-only lookup would return nothing at all for
    such a payload, which would quietly stop every rescue; refs and UUIDs cannot
    collide as strings, so keeping both is unambiguous.
    """

    summaries: dict[str, dict[str, Any]] = {}
    for item in _walk_objects(top):
        if item.get("kind") != "surface":
            continue
        key = str(item.get("id") or item.get("surface_id") or item.get("ref") or "")
        if not key:
            continue
        summaries[key] = classify_processes(
            _walk_objects(item.get("processes", [])),
            foreground_pgids=item.get("foreground_pgids", []),
        )
    return summaries


def surface_process_label(
    classified: Mapping[str, Mapping[str, Any]],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Look a surface's process label up by UUID, falling back to ref."""
    for key in (str(record.get("surface_id") or ""), str(record.get("ref") or "")):
        if key and key in classified:
            return dict(classified[key])
    return {"agent_kind": "unknown", "summary": "无进程信息"}


def inspect_claude_process(
    pid: int,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Return only sanitized Hook-health facts for one Claude process.

    The command line may contain model or launch configuration, so callers must
    never persist it.  We retain only a process-generation fingerprint and
    whether a legacy inline settings object omits the CCC Hook.
    """

    if pid <= 0:
        return {"pid": 0, "started_at": "", "generation": "", "legacy_override": False}
    try:
        result = runner(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"pid": pid, "started_at": "", "generation": f"{pid}:unknown", "legacy_override": False}
    line = (result.stdout or "").strip() if result.returncode == 0 else ""
    parts = line.split(None, 5)
    started_at = ""
    started_epoch = 0.0
    command = ""
    if len(parts) >= 6:
        raw_started = " ".join(parts[:5])
        command = parts[5]
        try:
            parsed_started = dt.datetime.strptime(raw_started, "%a %b %d %H:%M:%S %Y")
            started_at = parsed_started.isoformat()
            started_epoch = time.mktime(parsed_started.timetuple())
        except ValueError:
            started_at = raw_started
    # ``--settings`` accepts either an inline JSON object or a path to a
    # settings file. Treating the flag itself as proof of an override made
    # every normal ``claude --settings ~/.claude-profiles/foo.json`` launch
    # look like a legacy process and disabled the fallback permanently.
    settings_match = re.search(r"(?:^|\s)--settings(?:=|\s+)(?P<value>.+)$", command)
    settings_value = str(settings_match.group("value") or "").lstrip() if settings_match else ""
    if settings_value.startswith("{"):
        inline_settings = settings_value
    elif settings_value[:1] in {"'", '"'} and settings_value[1:].lstrip().startswith("{"):
        inline_settings = settings_value[1:]
    else:
        inline_settings = ""
    has_inline_settings = bool(inline_settings)
    inline_has_ccc_hook = bool(inline_settings and "claude_ccc_event_hook.py" in inline_settings)
    return {
        "pid": pid,
        "started_at": started_at,
        "started_epoch": started_epoch,
        "generation": _short_hash(f"{pid}:{started_at or 'unknown'}"),
        "has_inline_settings": has_inline_settings,
        "inline_has_ccc_hook": inline_has_ccc_hook,
        "legacy_override": has_inline_settings and not inline_has_ccc_hook,
    }


def _claude_session_owner_is_live(runtime: Any) -> bool:
    """Return whether a persisted session owner is still the same process.

    A state entry can outlive a Claude process after a pane is closed. Such a
    dead owner must not block a newly resumed process, but any inability to
    prove that it is stale remains a live-owner result (fail closed).
    """

    pid = int(getattr(runtime, "claude_process_pid", 0) or 0)
    expected_generation = str(getattr(runtime, "claude_process_generation", "") or "")
    if pid <= 0 or not expected_generation:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    inspection = inspect_claude_process(pid)
    current_generation = str(inspection.get("generation") or "")
    started_at = str(inspection.get("started_at") or "")
    if not current_generation or not started_at or current_generation.endswith(":unknown"):
        return True
    return current_generation == expected_generation


def discover_codex_surfaces(
    tree: Mapping[str, Any],
    top: Mapping[str, Any],
    workspace_id: str,
) -> list[dict[str, str]]:
    """Find live Codex surfaces in one workspace, joined on stable surface UUID."""

    classified = classify_surface_processes(top)
    discovered = [
        dict(record)
        for record in workspace_surface_records(tree, workspace_id).values()
        if surface_process_label(classified, record)["agent_kind"] == "codex"
    ]
    return sorted(discovered, key=lambda record: _ref_number(record["ref"]))


def _ref_number(ref: str) -> tuple[str, int]:
    prefix, _, suffix = ref.partition(":")
    try:
        return prefix, int(suffix)
    except ValueError:
        return prefix, sys.maxsize


def workspace_rule_by_id(config: Mapping[str, Any], selector: str) -> dict[str, Any]:
    for rule in config.get("workspace_rules", []):
        if selector in {rule.get("workspace_id"), rule.get("ref"), rule.get("name")}:
            return rule
    raise RuntimeError(f"workspace rule not found: {selector}")


def discover_rule_targets(client: CmuxClient, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules = [rule for rule in config.get("workspace_rules", []) if rule.get("enabled", True)]
    if not rules:
        return []
    tree = client.tree()
    targets: list[dict[str, Any]] = []
    for rule in rules:
        workspace_id = str(rule.get("workspace_id") or "")
        excluded = {str(value) for value in rule.get("excluded_surface_ids", [])}
        for record in discover_codex_surfaces(tree, client.top(workspace_id), workspace_id):
            if record["surface_id"] in excluded:
                continue
            targets.append({
                **record,
                "name": f"{rule.get('name') or rule.get('ref') or workspace_id[:8]}:{record['ref']}",
                "enabled": True,
                "paused": False,
                "source": "workspace_rule",
                "source_workspace_id": workspace_id,
            })
    return targets


def effective_targets(
    config: Mapping[str, Any],
    dynamic_targets: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Combine explicit and discovered targets, with explicit registration winning."""

    combined: dict[str, dict[str, Any]] = {}
    for target in dynamic_targets:
        surface_id = str(target.get("surface_id") or "")
        if surface_id:
            combined[surface_id] = target if isinstance(target, dict) else dict(target)
    for target in config.get("targets", []):
        surface_id = str(target.get("surface_id") or "")
        if surface_id:
            combined[surface_id] = target
    return sorted(combined.values(), key=lambda target: _ref_number(str(target.get("ref") or "")))


def target_by_id(config: Mapping[str, Any], target_id: str) -> dict[str, Any]:
    for target in config.get("targets", []):
        if target.get("surface_id") == target_id or target.get("ref") == target_id or target.get("name") == target_id:
            return target
    raise RuntimeError(f"registered target not found: {target_id}")


def file_sha256(path: Path) -> str:
    """SHA-256 of one file, or "" when unreadable.  Never raises."""

    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def daemon_source_identity() -> dict[str, Any]:
    """Identity of the source file this process actually loaded.

    The 2026-08-25 review could not tell whether production ran the reviewed
    code: the daemon had started eleven hours before the last edit, and the only
    available evidence was indirect (absence of a new log field).  A running
    process must be able to state its own provenance.
    """

    source = Path(__file__).resolve()
    try:
        mtime_ns = source.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return {
        "source_path": str(source),
        "source_sha256": file_sha256(source),
        "source_mtime_ns": mtime_ns,
        "feature_revision": FEATURE_REVISION,
    }


def describe_daemon_runtime(
    path: Path = DEFAULT_DAEMON_RUNTIME_PATH,
) -> dict[str, Any]:
    """Compare the *running* daemon's source against what is on disk now.

    ``disk_source_sha256`` is what a restart would load; ``source_sha256`` is
    what the live process did load.  When they differ, on-disk fixes are not in
    production yet -- the exact ambiguity that made the 2026-08-25 review report
    disk state as if it were running state.
    """

    record = load_json(path, {})
    if not isinstance(record, Mapping):
        record = {}
    running_sha = str(record.get("source_sha256") or "")
    pid = int(record.get("pid") or 0)
    alive = False
    if pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    source_path = Path(str(record.get("source_path") or "")) if record.get("source_path") else Path(__file__).resolve()
    disk_sha = file_sha256(source_path)
    result = dict(record)
    result["disk_source_sha256"] = disk_sha
    result["pid_alive"] = alive
    # Unknown is not the same as mismatched: absent metadata means the running
    # daemon predates this feature, which is itself the answer to "is it live?".
    result["source_matches_disk"] = bool(running_sha and disk_sha and running_sha == disk_sha)
    result["runtime_metadata_present"] = bool(record)
    return result


class WatchDaemon:
    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        state_path: Path = DEFAULT_STATE_PATH,
        client: CmuxClient | None = None,
        hook_settings_manager: ClaudeHookSettingsManager | None = None,
    ):
        self.config_path = config_path
        self.state_path = state_path
        self.config_store = ConfigStore(config_path)
        if hook_settings_manager is not None:
            self.claude_hook_settings = hook_settings_manager
        elif config_path == DEFAULT_CONFIG_PATH:
            self.claude_hook_settings = ClaudeHookSettingsManager()
        else:
            self.claude_hook_settings = ClaudeHookSettingsManager(
                config_path.parent / "claude-settings.json",
                lock_path=config_path.parent / "claude-settings.json.ccc.lock",
                backup_dir=config_path.parent / "settings-backups",
            )
        self.claude_event_ledger = ClaudeEventLedger(config_path.parent / "claude-event-ledger.json")
        self.claude_event_inbox = ClaudeEventInbox(
            config_path.parent / "claude-events.jsonl",
            config_path.parent / "claude-events.sock",
        )
        self.client = client
        self.stop_requested = False
        self.logger = logging.getLogger(APP_NAME)
        self._runtime_lock = threading.RLock()
        self._surface_locks_guard = threading.Lock()
        self._surface_locks: dict[str, threading.RLock] = {}
        self._targets_lock = threading.RLock()
        self._process_cache_lock = threading.RLock()
        self.config = self._load_config_at_startup()
        self.runtime: dict[str, TargetRuntime] = self._load_runtime()
        self.dynamic_targets: dict[str, dict[str, Any]] = {}
        self._local_paused_surface_ids: set[str] = set()
        self._candidate_process_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._process_inspection_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._event_worker_pool: ClaudeEventWorkerPool | None = None
        self._last_workspace_discovery_at = 0.0
        self._config_mtime_ns = self._config_mtime()
        # Set while the on-disk config is invalid, so the rejection is logged
        # once per distinct reason instead of every second.
        self._config_rejected_reason: str | None = None
        self._claude_hook_config_health: dict[str, Any] = self.claude_hook_settings.inspect()
        self._claude_hook_settings_mtime_ns: int | None = None
        self._claude_hook_settings_error: str | None = None
        self._claude_hook_settings_next_retry_at: float = 0.0
        # Log-channel self-check state.  The log directory was observed being
        # deleted under a running daemon (2026-08-24): logging kept succeeding
        # into an unlinked inode, so every path-based reader saw silence while
        # the daemon believed it was writing fine.
        self._log_channel_checked_at: float = 0.0
        self._log_channel_inode: int | None = None
        self._log_channel_broken: bool = False
        # Floor for the "no trustworthy Hook" clock.  A surface that predates
        # this daemon process cannot be blamed for exposure we could not observe,
        # so unprotected time is measured from whichever started later.
        self._daemon_started_at: float = time.time()
        # event_id -> ledger-claim cost, handed from the claim site to the send
        # path.  Bounded: entries are popped on use and pruned by size.
        self._claude_claim_ms: dict[str, float] = {}
        # Provenance of the code this process is actually running.  Kept in its
        # own file: every top-level key of state.json is loaded as a surface
        # runtime (see _load_runtime), so a "daemon" key there would materialise
        # a phantom target.
        self.daemon_runtime_path = (
            DEFAULT_DAEMON_RUNTIME_PATH
            if config_path == DEFAULT_CONFIG_PATH
            else config_path.parent / "daemon-runtime.json"
        )
        self._last_state_serialized = self._serialize_state()

    def _write_daemon_runtime(self) -> None:
        """Publish who we are and what code we loaded.  Never raises.

        Without this, "is the fix live?" could only be answered by inference --
        during the 2026-08-25 review the sole evidence that production still ran
        old code was that a new log field was absent, which is unfalsifiable if
        the field simply never fires.  The source SHA is computed, not declared,
        so it cannot drift out of date the way a version string does.
        """

        identity = daemon_source_identity()
        try:
            config_stat_mtime = self.config_path.stat().st_mtime_ns
        except OSError:
            config_stat_mtime = 0
        record = {
            "version": 1,
            "pid": os.getpid(),
            "started_at": round(self._daemon_started_at, 3),
            "started_at_text": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(self._daemon_started_at),
            ),
            "config_path": str(self.config_path),
            "config_sha256": file_sha256(self.config_path),
            "config_mtime_ns": config_stat_mtime,
            "mode": str(self.config.get("mode") or ""),
            "global_paused": bool(self.config.get("global_paused", False)),
            "claude_enabled": bool(self.config.get("claude_enabled", False)),
            "target_count": len(self.config.get("targets", []) or []),
            **identity,
        }
        try:
            atomic_write_json(self.daemon_runtime_path, record)
        except OSError as exc:
            self.logger.warning("could not publish daemon runtime identity: %s", exc)
        self.logger.info(
            "daemon_identity pid=%d source_sha=%s feature=%s config_sha=%s "
            "mode=%s targets=%d",
            record["pid"],
            str(record["source_sha256"])[:12] or "unknown",
            record["feature_revision"],
            str(record["config_sha256"])[:12] or "unknown",
            record["mode"] or "unknown",
            record["target_count"],
        )

    def _log_channel_incident(self, kind: str, detail: dict[str, Any]) -> None:
        """Append one durable incident record.  Never raises into the poll loop.

        We cannot learn *who* deleted the directory (macOS gives us no fs audit
        trail here), so we record enough to prove a recurrence and to date it --
        the same approach used for the settings.json overwriter signature.
        """

        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "ts": round(time.time(), 3),
            "kind": kind,
            "pid": os.getpid(),
            "log_dir": str(DEFAULT_LOG_DIR),
            **detail,
        }
        try:
            DEFAULT_LOG_CHANNEL_INCIDENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(DEFAULT_LOG_CHANNEL_INCIDENT_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # The incident log is diagnostics only.  Losing it must never stop
            # Claude continuation.
            pass

    def _check_log_channel(self, *, force: bool = False) -> dict[str, Any]:
        """Detect a vanished/replaced log file and rebind the file handler.

        Returns a small report for telemetry.  Purely local: it never sends,
        never touches surfaces, and never restarts anything.
        """

        now = time.time()
        interval = float(self.config.get(
            "log_channel_check_interval_sec", LOG_CHANNEL_CHECK_INTERVAL_SEC,
        ))
        if not force and now - self._log_channel_checked_at < interval:
            return {
                "healthy": not self._log_channel_broken,
                "inode": self._log_channel_inode,
                "checked_at": self._log_channel_checked_at,
            }
        self._log_channel_checked_at = now
        log_path = DEFAULT_LOG_DIR / "watch.log"
        try:
            current_inode: int | None = log_path.stat().st_ino
        except OSError:
            current_inode = None

        if current_inode is not None and current_inode == self._log_channel_inode:
            self._log_channel_broken = False
            return {"healthy": True, "inode": current_inode, "checked_at": now}

        if self._log_channel_inode is None:
            # First observation: just record the baseline.
            self._log_channel_inode = current_inode
            self._log_channel_broken = current_inode is None
            return {
                "healthy": current_inode is not None,
                "inode": current_inode,
                "checked_at": now,
            }

        # The path we log to is no longer the file we opened.  Rebind so future
        # lines land somewhere a reader can actually reach.
        previous_inode = self._log_channel_inode
        detail: dict[str, Any] = {
            "previous_inode": previous_inode,
            "current_inode": current_inode,
            "dir_exists": DEFAULT_LOG_DIR.exists(),
        }
        rebound = False
        try:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            root = logging.getLogger()
            for handler in list(root.handlers):
                if isinstance(handler, logging.FileHandler) and Path(
                    getattr(handler, "baseFilename", "")
                ) == log_path:
                    root.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:
                        pass
            replacement = logging.FileHandler(log_path, encoding="utf-8")
            replacement.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            root.addHandler(replacement)
            rebound = True
            self._log_channel_inode = log_path.stat().st_ino
            self._log_channel_broken = False
        except OSError as exc:
            detail["rebind_error"] = str(exc)
            self._log_channel_broken = True
        detail["rebound"] = rebound
        self._log_channel_incident("log_file_replaced", detail)
        self.logger.warning(
            "log channel replaced previous_inode=%s current_inode=%s dir_exists=%s rebound=%s",
            previous_inode,
            current_inode,
            detail["dir_exists"],
            rebound,
        )
        return {
            "healthy": rebound,
            "inode": self._log_channel_inode,
            "checked_at": now,
            "rebound": rebound,
        }

    def _check_claude_hook_settings(self, *, force: bool = False) -> dict[str, Any]:
        current_mtime = self.claude_hook_settings.mtime_ns()
        if (
            not force
            and self._claude_hook_config_health.get("status") == "drift_storm"
            and time.time() < self._claude_hook_settings_next_retry_at
        ):
            return self._claude_hook_config_health
        if (
            not force
            and current_mtime == self._claude_hook_settings_mtime_ns
            and self._claude_hook_config_health.get("healthy")
        ):
            return self._claude_hook_config_health
        repair = self.config.get("claude_hook_config_policy") == "auto_repair"
        try:
            report = self.claude_hook_settings.ensure(
                repair=repair,
                automatic=True,
                max_repairs_per_hour=int(self.config.get(
                    "claude_hook_auto_repair_max_per_hour",
                    CLAUDE_HOOK_AUTO_REPAIR_MAX_PER_HOUR,
                )),
                drift_warning_per_day=int(self.config.get(
                    "claude_hook_drift_warning_per_day",
                    CLAUDE_HOOK_DRIFT_WARNING_PER_DAY,
                )),
                backup_keep=int(self.config.get(
                    "claude_hook_settings_backup_keep",
                    CLAUDE_HOOK_SETTINGS_BACKUP_KEEP,
                )),
            )
        except RuntimeError as exc:
            report = {
                "status": "invalid",
                "healthy": False,
                "path": str(self.claude_hook_settings.path),
                "event_counts": {},
                "mtime_ns": current_mtime,
                "error": str(exc),
            }
            if self._claude_hook_settings_error != str(exc):
                self.logger.error("Claude Hook configuration unsafe: %s", exc)
                self._notify_async(
                    "CCC Claude Hook 配置异常",
                    "settings.json 无法安全修复；Claude 续跑已降级并保持可见",
                    name="ccc-hook-settings-invalid",
                )
            self._claude_hook_settings_error = str(exc)
        else:
            previous_status = str(self._claude_hook_config_health.get("status") or "")
            self._claude_hook_settings_next_retry_at = float(report.get("next_retry_at") or 0.0)
            if report.get("status") == "drift_storm" and previous_status != "drift_storm":
                self.logger.error(
                    "Claude Hook drift storm; automatic repair paused budget=%s/%s signature=%s",
                    report.get("repair_budget_used"),
                    report.get("repair_budget_max"),
                    (report.get("drift_signature") or {}).get("signature", ""),
                )
                self._notify_async(
                    "CCC Hook 持续被覆盖",
                    "自动修复已达到每小时上限；请排查覆盖 ~/.claude/settings.json 的进程",
                    name="ccc-hook-drift-storm",
                )
            if report.get("changed"):
                self.logger.warning(
                    "Claude Hook configuration auto-repaired backup=%s",
                    report.get("backup_path", ""),
                )
                self._notify_async(
                    "CCC 已修复 Claude Hook",
                    "检测到 settings.json 漂移，已只合并四组 CCC Hook",
                    name="ccc-hook-settings-repaired",
                )
            if self._claude_hook_settings_error is not None:
                self.logger.info("Claude Hook configuration healthy again")
            self._claude_hook_settings_error = None
        self._claude_hook_config_health = report
        self._claude_hook_settings_mtime_ns = self.claude_hook_settings.mtime_ns()
        return report

    def _load_config(self) -> dict[str, Any]:
        return self.config_store.load()

    def _surface_lock(self, surface_id: str) -> threading.RLock:
        with self._surface_locks_guard:
            return self._surface_locks.setdefault(surface_id, threading.RLock())

    def _load_config_at_startup(self) -> dict[str, Any]:
        """Load the config. Invalid files still fail closed at startup.

        Hot-reload keeps the last good config if a hand edit is rejected
        (``KeepAlive=true`` must not crash-loop).  ``claude_enabled`` is a real
        switch now: true is valid and must not be coerced to false, or enabling
        it on disk would be a no-op.
        """

        migrated = self.config_store.migrate_deprecated()
        if migrated:
            self.logger.info("migrated deprecated Claude timing configuration")
        return self._load_config()

    def _load_runtime(self) -> dict[str, TargetRuntime]:
        value = load_json(self.state_path, {})
        if not isinstance(value, Mapping):
            return {}
        return {str(key): TargetRuntime.from_dict(item) for key, item in value.items() if isinstance(item, Mapping)}

    def _config_mtime(self) -> int | None:
        return self.config_store.mtime_ns()

    def _serialize_state(self) -> str:
        with self._runtime_lock:
            return json.dumps(
                {key: value.to_dict() for key, value in self.runtime.items()},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

    def _reload_config_if_changed(self) -> None:
        current_mtime = self._config_mtime()
        if current_mtime == self._config_mtime_ns:
            return
        try:
            self.config_store.migrate_deprecated()
            reloaded = self._load_config()
        except RuntimeError as exc:
            # An unreadable or rejected config must never take the watch loop
            # down.  ``KeepAlive=true`` means an exception here does not stop
            # the daemon once -- launchd restarts it, startup validation fails
            # the same way, and the surfaces stay unrescued in a crash loop.
            # A hand edit that trips validation (a typo in ``mode``, a
            # non-boolean ``claude_enabled``) is exactly the case this has to
            # survive, so keep serving the last good config and record the
            # rejection.
            # ``_config_mtime_ns`` is advanced regardless: without that the
            # same bad file is re-read and re-logged on every single poll.
            self._config_mtime_ns = current_mtime
            if self._config_rejected_reason != str(exc):
                self._config_rejected_reason = str(exc)
                self.logger.error("configuration rejected, keeping previous: %s", exc)
            return
        previous_mode = str(self.config.get("mode", ""))
        self.config = reloaded
        self._config_mtime_ns = current_mtime
        self._last_workspace_discovery_at = 0.0
        # F2 重置点之三：人工重新 arm 是明确的"再试一次"授权，孤儿 Enter
        # 预算随之归零。只在 非armed→armed 的边沿触发，普通配置刷新不重置。
        if previous_mode != "armed" and str(reloaded.get("mode", "")) == "armed":
            for runtime in self.runtime.values():
                runtime.claude_orphan_enter_count = 0
                runtime.claude_orphan_enter_at = 0.0
        if self._config_rejected_reason is not None:
            self._config_rejected_reason = None
            self.logger.info("configuration accepted again after rejection")
        self.logger.info(
            "configuration reloaded mode=%s explicit_targets=%d workspace_rules=%d",
            self.config.get("mode"),
            len(self.config.get("targets", [])),
            len(self.config.get("workspace_rules", [])),
        )

    def _mutate_config(self, mutator: Any) -> Any:
        config, result, mtime_ns = self.config_store.mutate(mutator)
        self.config = config
        self._config_mtime_ns = mtime_ns
        self._last_workspace_discovery_at = 0.0
        return result

    def _candidate_process_label(
        self,
        target: Mapping[str, Any],
        client: CmuxClient,
    ) -> dict[str, Any]:
        """Return the current process label only for an error candidate.

        A strong Claude screen fingerprint is enough when process data is not
        available.  When it *is* available, an exact Codex process label wins:
        a Codex transcript may quote Claude text, but that must never suppress
        a real Codex rescue.  This is observation evidence, never a send grant.
        """

        workspace_id = str(target.get("workspace_id") or "")
        if not workspace_id:
            return {"agent_kind": "unknown", "summary": "workspace unavailable"}
        now = time.monotonic()
        with self._process_cache_lock:
            cached = self._candidate_process_cache.get(workspace_id)
        if cached is not None and now - cached[0] < CLAUDE_PROCESS_CACHE_SEC:
            labels = cached[1]
        else:
            try:
                labels = classify_surface_processes(client.top(workspace_id))
            except CmuxError:
                return {"agent_kind": "unknown", "summary": "process lookup unavailable"}
            with self._process_cache_lock:
                self._candidate_process_cache[workspace_id] = (now, labels)
        return surface_process_label(labels, target)

    def _inspect_process_cached(self, pid: int) -> dict[str, Any]:
        now = time.monotonic()
        with self._process_cache_lock:
            cached = self._process_inspection_cache.get(pid)
            if cached is not None and now - cached[0] < CLAUDE_PROCESS_INSPECTION_CACHE_SEC:
                return dict(cached[1])
        inspected = inspect_claude_process(pid)
        with self._process_cache_lock:
            self._process_inspection_cache[pid] = (now, inspected)
        return dict(inspected)

    def _claude_process_observation(
        self,
        target: Mapping[str, Any],
        client: CmuxClient,
    ) -> dict[str, Any] | None:
        label = self._candidate_process_label(target, client)
        if label.get("agent_kind") != "claude":
            return None
        pid = int(label.get("agent_pid") or 0)
        inspected = self._inspect_process_cached(pid)
        inspected["agent_kind"] = "claude"
        return inspected

    def _apply_claude_process_observation(
        self,
        runtime: TargetRuntime,
        observation: Mapping[str, Any] | None,
    ) -> None:
        if not observation:
            return
        now = time.time()
        pid = int(observation.get("pid") or 0)
        generation = str(observation.get("generation") or "")
        started_epoch = float(observation.get("started_epoch") or 0.0)
        generation_changed = bool(generation and generation != runtime.claude_process_generation)
        # Older state files predate ``claude_hook_process_generation``.  A
        # daemon-only restart must not discard a real Hook from the exact same
        # still-running Claude process merely because that new provenance field
        # is absent.  PID + start-derived generation + session + causal time
        # order make this a narrow schema migration; a new process, unknown
        # start time, or pre-start Hook remains fail-closed below.
        if (
            not generation_changed
            and not runtime.claude_hook_process_generation
            and runtime.claude_last_hook_at > 0
            and runtime.claude_session_id
            and pid > 0
            and pid == runtime.claude_process_pid
            and generation
            and generation == runtime.claude_process_generation
            and started_epoch > 0
            and runtime.claude_last_hook_at >= started_epoch
        ):
            runtime.claude_hook_process_generation = generation
        hook_belongs_to_process = bool(
            runtime.claude_last_hook_at > 0
            and runtime.claude_hook_process_generation == generation
            and (started_epoch <= 0 or runtime.claude_last_hook_at >= started_epoch)
        )
        if generation_changed:
            runtime.claude_process_pid = pid
            runtime.claude_process_started_at = str(observation.get("started_at") or "") or None
            runtime.claude_process_generation = generation
            runtime.claude_hook_unverified_since = now
            # A process generation is a hard lifecycle boundary.  Clear every
            # field that can grant fallback or submit authority; retaining any
            # of these from the old process lets a new Claude inherit a dead
            # session's qualification during the Hook grace window.
            runtime.claude_hook_health = "unverified"
            runtime.claude_hook_process_generation = None
            runtime.claude_session_id = None
            runtime.claude_generation_id = None
            runtime.claude_last_hook_at = 0.0
            runtime.claude_last_event_id = None
            runtime.claude_last_event_status = None
            runtime.claude_last_resume_event_id = None
            runtime.claude_completion_event_id = None
            runtime.claude_event_message_hash = None
            runtime.claude_last_prompt_attribution = None
            runtime.claude_completed_latched = False
            runtime.claude_completion_fingerprint = None
            runtime.claude_completed_at = 0.0
            runtime.claude_prompt_pending = False
            runtime.claude_prompt_kind = None
            runtime.claude_prompt_fingerprint = None
            runtime.claude_prompt_sent_at = 0.0
            runtime.episode_id = None
            runtime.episode_started_at = 0.0
            runtime.send_count = 0
            runtime.last_send_at = 0.0
            runtime.awaiting = False
            runtime.awaiting_suppressed = 0
            runtime.sent_fingerprint = None
            runtime.sent_screen_signature = None
            runtime.claude_last_submit_event_id = None
            runtime.claude_last_submit_message_hash = None
            runtime.claude_last_submit_session_id = None
            runtime.claude_last_submit_generation = None
            runtime.claude_last_submit_at = 0.0
            runtime.claude_submit_last_reason = None
            runtime.claude_submit_confirmed_at = 0.0
            self._clear_claude_fallback_episode(runtime)
            self._clear_claude_submit(runtime, reason="generation_changed")
            self._clear_claude_deferred(runtime, reason="generation_changed")
            # A new Claude process is a new exposure.  Without this reset the
            # clock below keeps the previous generation's start (it only seeds
            # when the value is 0), so a process launched seconds ago inherits
            # hours of "unprotected" time and an already-escalated severity --
            # exactly the cross-generation mixing this clock exists to avoid.
            runtime.claude_hook_unprotected_since = 0.0
            runtime.claude_hook_unprotected_severity = ""
            self._reset_claude_context_runtime(runtime)
        if hook_belongs_to_process:
            runtime.claude_hook_health = "healthy"
            runtime.claude_hook_unverified_since = 0.0
            self._refresh_claude_unprotected_clock(runtime, started_epoch, now)
            return
        if bool(observation.get("legacy_override")):
            runtime.claude_hook_health = "legacy_override"
            if runtime.claude_hook_unverified_since <= 0:
                runtime.claude_hook_unverified_since = now
            self._refresh_claude_unprotected_clock(runtime, started_epoch, now)
            return
        if runtime.claude_hook_unverified_since <= 0:
            runtime.claude_hook_unverified_since = now
        grace = max(0.0, float(self.config.get(
            "claude_hook_unverified_grace_sec",
            CLAUDE_HOOK_UNVERIFIED_GRACE_SEC,
        )))
        runtime.claude_hook_health = (
            "missing"
            if now - runtime.claude_hook_unverified_since >= grace
            else "unverified"
        )
        self._refresh_claude_unprotected_clock(runtime, started_epoch, now)

    def _refresh_claude_unprotected_clock(
        self,
        runtime: TargetRuntime,
        started_epoch: float,
        now: float,
    ) -> None:
        """Track how long this surface has had no trustworthy Hook generation.

        Fail-closed is correct: ccc must not send where it cannot prove the Hook
        generation.  What was missing is the *duration*.  During the 2026-08-24
        observation surface:74 sat unprotected for hours and every audit line
        looked identical, so nothing ever escalated.

        The clock starts at ``max(process_started, daemon_started)`` and never at
        ``last_send_at``: a Claude process can be restarted while ccc keeps
        running, so ``last_send_at`` may predate the live process by many hours
        and describes a dead generation.  The same surface read 19.8h by
        last_send but 14.5h by process start; only the latter is this process's
        real exposure.
        """

        if runtime.claude_hook_health == "healthy":
            runtime.claude_hook_unprotected_since = 0.0
            runtime.claude_hook_unprotected_severity = ""
            return
        if runtime.claude_hook_unprotected_since <= 0:
            floor = max(started_epoch, self._daemon_started_at)
            runtime.claude_hook_unprotected_since = floor if floor > 0 else now
        elapsed = max(0.0, now - runtime.claude_hook_unprotected_since)
        severity = self._claude_unprotected_severity(elapsed)
        if severity and severity != runtime.claude_hook_unprotected_severity:
            runtime.claude_hook_unprotected_severity = severity
            self.logger.warning(
                "surface_hook_unprotected severity=%s elapsed_sec=%.0f health=%s",
                severity,
                elapsed,
                runtime.claude_hook_health,
            )

    def _claude_unprotected_severity(self, elapsed: float) -> str:
        """Map an unprotected duration onto a coarse escalation label."""

        critical = float(self.config.get(
            "claude_hook_unprotected_critical_sec",
            CLAUDE_HOOK_UNPROTECTED_CRITICAL_SEC,
        ))
        severe = float(self.config.get(
            "claude_hook_unprotected_severe_sec",
            CLAUDE_HOOK_UNPROTECTED_SEVERE_SEC,
        ))
        warn = float(self.config.get(
            "claude_hook_unprotected_warn_sec",
            CLAUDE_HOOK_UNPROTECTED_WARN_SEC,
        ))
        if elapsed >= critical:
            return "critical"
        if elapsed >= severe:
            return "severe"
        if elapsed >= warn:
            return "warn"
        return ""

    def _claude_blind_spot_keeps_monitoring(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        client: CmuxClient,
    ) -> bool:
        """Decide whether an unparseable pane stays registered or gets isolated.

        A parse failure used to always pause the target.  A paused target is
        never polled again, so the blind-spot clock below could seed but never
        accumulate: the 30-minute warning was unreachable from this entry.

        Keeping a pane registered is only safe with *live* evidence that it is
        still Claude.  Persisted runtime fields cannot carry that: the process
        may have exited and left a shell behind, and a pane holding error text
        can classify as ``recoverable_error``, which is send-eligible.  So this
        re-reads ``top`` -- a separate RPC from the read_screen that just failed,
        and normally still healthy -- and isolates on anything else, including
        an inconclusive check.
        """

        try:
            label = self._candidate_process_label(target, client)
        except CmuxError:
            return False
        live_is_claude = str(label.get("agent_kind") or "") == "claude"
        if not live_is_claude and runtime.claude_session_id:
            # Persisted Claude identity plus a live non-Claude process is exactly
            # the stale-runtime case: isolate, and say why, so the divergence is
            # not silently resolved in favour of the older evidence.
            self.logger.info(
                "surface=%s blind pane isolated: live process is %s though runtime "
                "still records a Claude session",
                str(target["surface_id"])[:8],
                str(label.get("agent_kind") or "unknown"),
            )
        return live_is_claude

    def _refresh_claude_unreadable_clock(
        self,
        surface_id: str,
        runtime: TargetRuntime,
        raw_kind: str,
        *,
        entry: str = "",
    ) -> None:
        """Time how long a viewport has been unparseable, and say so.

        ``incompatible``/``unreadable:*`` were only ever instantaneous verdicts,
        so a pane could sit in a parser blind spot indefinitely in silence.
        During the 2026-08-24 observation surface:72 was ``incompatible`` for the
        full 5.65 hours and never produced a single daemon-side warning, while
        two further panes entered the blind spot mid-window (1 -> 3).  A blind
        pane still fails open on context, so the exposure has to be visible.

        ``raw_kind`` must be the *pre-guard* classification: the runtime guards
        rewrite the state into ``claude_hook_missing`` and friends, which would
        otherwise mask the fact that the viewport itself could not be read.
        """

        blind = raw_kind == "incompatible" or raw_kind.startswith("unreadable")
        if not blind:
            runtime.claude_unreadable_since = 0.0
            runtime.claude_unreadable_warned_at = 0.0
            runtime.claude_unreadable_entry = None
            return
        now = time.time()
        # A continuing blind spot keeps its original start even when the entry
        # point changes (a returned ``incompatible`` can become a raised one and
        # back).  Only a readable frame clears the clock.
        if entry:
            runtime.claude_unreadable_entry = entry
        if runtime.claude_unreadable_since <= 0:
            runtime.claude_unreadable_since = now
            return
        elapsed = now - runtime.claude_unreadable_since
        threshold = float(self.config.get(
            "claude_unreadable_warn_sec", CLAUDE_UNREADABLE_WARN_SEC,
        ))
        if elapsed < threshold:
            return
        # Re-state periodically rather than every poll: a pane blind for hours
        # would otherwise emit one line per second and bury everything else.
        if runtime.claude_unreadable_warned_at > 0 and now - runtime.claude_unreadable_warned_at < threshold:
            return
        runtime.claude_unreadable_warned_at = now
        self.logger.warning(
            "surface=%s claude_viewport_unreadable kind=%s entry=%s elapsed_sec=%.0f "
            "monitoring=true",
            surface_id[:8],
            raw_kind,
            entry or runtime.claude_unreadable_entry or "-",
            elapsed,
        )

    def _claude_observed_state(self, reason: str) -> ScreenState:
        return ScreenState("claude_observed", reason=f"{reason}; claude_enabled is off")

    def _classify_target_screen(
        self,
        target: Mapping[str, Any],
        screen_text: str,
        client: CmuxClient,
    ) -> ScreenState:
        """Classify one visible viewport without letting Claude reach Codex replay.

        Codex menus/Working always win first: a Codex screen can quote a Claude
        transcript while its own composer is briefly gone.  When the Claude
        switch is on, a Claude UI goes to classify_claude_grid except when an
        error candidate's current process is exact Codex — that rescue must
        not be withheld.  When the switch is off, Claude is observed only
        (never persist-paused).
        """

        lines = _normalise_lines(screen_text)
        claude_on = bool(self.config.get("claude_enabled", False))
        looks_like_claude = _looks_like_claude_ui(lines)
        prefilter = classify_text_prefilter(lines)

        def replay_claude() -> ScreenState:
            try:
                payload = client.replay(str(target["workspace_id"]), str(target["surface_id"]))
                state = classify_claude_grid(
                    Grid.from_rpc(payload, str(target["surface_id"])),
                    claude_message=str(self.config.get("claude_message") or CLAUDE_MESSAGE),
                )
                if state.kind in {"claude_stopped", "recoverable_error"}:
                    return ScreenState(
                        "claude_hook_waiting",
                        error_type=state.error_type,
                        fingerprint=state.fingerprint,
                        screen_signature=state.screen_signature,
                        reason="Claude stop eligibility comes only from lifecycle hooks",
                        message_kind="claude",
                        content_fingerprint=state.content_fingerprint,
                        claude_context=state.claude_context,
                    )
                return state
            except IncompatibleError as exc:
                # A bad Claude grid must not persist-pause a UUID the user
                # registered.  Codex replay failures still pause via the
                # outer IncompatibleError handler.
                return ScreenState("incompatible", reason=f"Claude replay unreadable: {exc}")

        def replay_codex() -> ScreenState:
            payload = client.replay(str(target["workspace_id"]), str(target["surface_id"]))
            return classify_grid(Grid.from_rpc(payload, str(target["surface_id"])))

        if prefilter.kind in {"menu", "working"}:
            if claude_on:
                label = self._candidate_process_label(target, client)
                if label.get("agent_kind") == "claude":
                    return replay_claude()
            return prefilter

        if not claude_on:
            if prefilter.kind == "idle":
                if looks_like_claude:
                    return self._claude_observed_state("Claude UI")
                return prefilter
            label = self._candidate_process_label(target, client)
            agent_kind = label.get("agent_kind")
            if agent_kind == "claude":
                return self._claude_observed_state("Claude process")
            if agent_kind == "unknown" and looks_like_claude:
                return self._claude_observed_state("Claude UI; process unavailable")
            return replay_codex()

        if prefilter.kind == "idle":
            if looks_like_claude:
                return replay_claude()
            # A footer may be temporarily scrolled out.  Process identity is
            # only an adapter-routing hint here; the Claude grid still has to
            # prove its composer before any heartbeat can become eligible.
            label = self._candidate_process_label(target, client)
            if label.get("agent_kind") == "claude":
                return replay_claude()
            if label.get("agent_kind") == "codex":
                return dataclasses.replace(prefilter, message_kind="codex")
            return prefilter
        label = self._candidate_process_label(target, client)
        agent_kind = label.get("agent_kind")
        if agent_kind == "codex":
            return replay_codex()
        if agent_kind == "claude" or looks_like_claude:
            return replay_claude()
        return replay_codex()

    def save(self) -> None:
        with self._runtime_lock:
            state_serialized = self._serialize_state()
            if state_serialized != self._last_state_serialized or not self.state_path.exists():
                atomic_write_json(self.state_path, json.loads(state_serialized))
                self._last_state_serialized = state_serialized

    def request_stop(self, *_: Any) -> None:
        self.stop_requested = True

    def run(self) -> int:
        ensure_app_dir(self.config_path.parent)
        log_path = DEFAULT_LOG_DIR / "watch.log"
        log_dir_was_missing = not DEFAULT_LOG_DIR.exists()
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stderr)],
        )
        # Seed the log-channel baseline so the first periodic check compares
        # against the inode we actually opened rather than re-discovering it.
        try:
            self._log_channel_inode = log_path.stat().st_ino
        except OSError:
            self._log_channel_inode = None
        self._log_channel_checked_at = time.time()
        if log_dir_was_missing:
            # launchd opens StandardOutPath/StandardErrorPath before exec'ing us,
            # so a directory that vanished while we were down means those two
            # streams were redirected to nowhere and only a plist reinstall can
            # restore them.  watch.log itself is fine: we just recreated it.
            self._log_channel_incident("log_dir_recreated_at_startup", {
                "note": "launchd stdout/stderr may be unbound until plist reinstall",
                "watch_log_inode": self._log_channel_inode,
            })
            self.logger.warning(
                "log directory was missing at startup and has been recreated; "
                "launchd stdout/stderr may be unbound until `ccc install` reinstalls the plist",
            )
        self._write_daemon_runtime()
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        client = self.client or CmuxClient(str(self.config.get("cmux_path", DEFAULT_CMUX)))
        capabilities_ok = False
        last_unavailable_log = 0.0
        self.claude_event_inbox.start(self.claude_event_ledger.known_ids())
        self._event_worker_pool = ClaudeEventWorkerPool(
            self.claude_event_inbox,
            self._handle_claude_event_safely,
            lambda: CmuxClient(str(self.config.get("cmux_path", DEFAULT_CMUX))),
            workers=int(self.config.get("claude_event_workers", CLAUDE_EVENT_WORKERS)),
        )
        self._event_worker_pool.start()
        try:
            while not self.stop_requested:
                self._check_log_channel()
                self._check_claude_hook_settings()
                if not client.ping():
                    capabilities_ok = False
                    if time.time() - last_unavailable_log >= 30:
                        self.logger.info("cmux unavailable; waiting")
                        last_unavailable_log = time.time()
                    self.claude_event_inbox.wait(float(self.config.get("cmux_unavailable_poll_sec", 2)))
                    continue
                if not capabilities_ok:
                    try:
                        client.capabilities()
                        capabilities_ok = True
                    except IncompatibleError as exc:
                        self.logger.error("incompatible cmux: %s", exc)
                        self._mark_global_incompatible(TargetRuntime(), str(exc))
                        self.save()
                        self.claude_event_inbox.wait(float(self.config.get("cmux_unavailable_poll_sec", 2)))
                        continue
                cycle_started = time.monotonic()
                self.process_once(client)
                delay = remaining_poll_delay(
                    float(self.config.get("poll_interval_sec", 1)),
                    time.monotonic() - cycle_started,
                )
                if delay:
                    self.claude_event_inbox.wait(delay)
            return 0
        finally:
            if self._event_worker_pool is not None:
                self._event_worker_pool.close()
            self.claude_event_inbox.close()

    def process_once(self, client: CmuxClient) -> None:
        self._reload_config_if_changed()
        self._check_claude_hook_settings()
        self._refresh_dynamic_targets(client)
        with self._targets_lock:
            targets = effective_targets(self.config, self.dynamic_targets.values())
        try:
            send_guard_tree: Mapping[str, Any] | None = client.tree()
            send_guard_error = ""
        except CmuxError as exc:
            send_guard_tree = None
            send_guard_error = str(exc)
        for target in targets:
            if not target.get("enabled", True) or target.get("paused", False):
                continue
            surface_id = str(target["surface_id"])
            self._local_paused_surface_ids.discard(surface_id)
            with self._runtime_lock:
                runtime = self.runtime.setdefault(surface_id, TargetRuntime())
            try:
                screen_text = client.read_screen(str(target["workspace_id"]), surface_id)
                state = self._classify_target_screen(target, screen_text, client)
                observation = (
                    None
                    if state.kind in {"working", "menu"} and not self._runtime_is_claude(runtime, state)
                    else self._claude_process_observation(target, client)
                )
                with self._surface_lock(surface_id):
                    self._apply_claude_process_observation(runtime, observation)
                    # Must run on the pre-guard kind: the guards below rewrite an
                    # unreadable viewport into claude_hook_missing and friends,
                    # which would hide the blind spot entirely.
                    self._refresh_claude_unreadable_clock(
                        surface_id, runtime, state.kind, entry="returned_main",
                    )
                    state = self._apply_claude_runtime_guards(surface_id, runtime, state)
                    state = self._apply_claude_context_guard(surface_id, runtime, state)
                    if self._reconcile_claude_submit(target, runtime, state, client):
                        continue
                    self._restore_expired_claude_deferred(runtime)
                    if self._maybe_send_deferred_claude_stop(target, runtime, state, client):
                        continue
                    if self._runtime_is_claude(runtime, state) and self._maybe_send_claude_hook_gap_fallback(
                        target, runtime, state, observation, client
                    ):
                        continue
                    if state.kind not in SEND_ELIGIBLE_STATES:
                        self._record_state(surface_id, runtime, state)
                        continue
            except GlobalIncompatibleError as exc:
                with self._surface_lock(surface_id):
                    self._mark_global_incompatible(runtime, str(exc))
                continue
            except IncompatibleError as exc:
                with self._surface_lock(surface_id):
                    # A raised IncompatibleError is the same parser blind spot as
                    # a returned ``incompatible`` state, but it used to skip the
                    # clock entirely: this branch pauses the target, and a paused
                    # target is never polled again, so the exposure could never be
                    # timed or restated.  Distinct entries keep the four entry
                    # points apart in the log and in hook-audit.
                    self._refresh_claude_unreadable_clock(
                        surface_id, runtime, "unreadable:initial_read",
                        entry="raised_initial",
                    )
                    if self._claude_blind_spot_keeps_monitoring(target, runtime, client):
                        self._record_state(surface_id, runtime, ScreenState(
                            "claude_viewport_blind",
                            reason=f"viewport unreadable, still monitoring: {exc}",
                            message_kind="claude",
                        ))
                    else:
                        self._mark_target_incompatible(target, runtime, str(exc))
                continue
            except CmuxError as exc:
                if not client.ping():
                    with self._surface_lock(surface_id):
                        # Deliberately *not* a parser blind spot: cmux being gone
                        # is infrastructure. Counting it would turn every socket
                        # restart into a fake "viewport unreadable" report.
                        self._record_state(surface_id, runtime, ScreenState("cmux_unavailable", reason="socket disappeared during poll"))
                    continue
                if self._refresh_workspace(target, client):
                    try:
                        screen_text = client.read_screen(str(target["workspace_id"]), surface_id)
                        state = self._classify_target_screen(target, screen_text, client)
                        observation = (
                            None
                            if state.kind in {"working", "menu"} and not self._runtime_is_claude(runtime, state)
                            else self._claude_process_observation(target, client)
                        )
                        with self._surface_lock(surface_id):
                            self._apply_claude_process_observation(runtime, observation)
                            self._refresh_claude_unreadable_clock(
                                surface_id, runtime, state.kind, entry="returned_retry",
                            )
                            state = self._apply_claude_runtime_guards(surface_id, runtime, state)
                            state = self._apply_claude_context_guard(surface_id, runtime, state)
                            if self._reconcile_claude_submit(target, runtime, state, client):
                                continue
                            self._restore_expired_claude_deferred(runtime)
                            if self._maybe_send_deferred_claude_stop(target, runtime, state, client):
                                continue
                            if self._runtime_is_claude(runtime, state) and self._maybe_send_claude_hook_gap_fallback(
                                target, runtime, state, observation, client
                            ):
                                continue
                            if state.kind not in SEND_ELIGIBLE_STATES:
                                self._record_state(surface_id, runtime, state)
                                continue
                    except GlobalIncompatibleError as retry_exc:
                        with self._surface_lock(surface_id):
                            self._mark_global_incompatible(runtime, str(retry_exc))
                        continue
                    except IncompatibleError as retry_exc:
                        with self._surface_lock(surface_id):
                            # Same blind spot, reached after a workspace refresh.
                            # Tagged distinctly so an audit can tell a first-read
                            # failure from one that survived a retry.
                            self._refresh_claude_unreadable_clock(
                                surface_id, runtime, "unreadable:retry_incompatible",
                                entry="raised_retry",
                            )
                            if self._claude_blind_spot_keeps_monitoring(target, runtime, client):
                                self._record_state(surface_id, runtime, ScreenState(
                                    "claude_viewport_blind",
                                    reason=f"viewport unreadable after retry, still monitoring: {retry_exc}",
                                    message_kind="claude",
                                ))
                            else:
                                self._mark_target_incompatible(target, runtime, str(retry_exc))
                        continue
                    except CmuxError as retry_exc:
                        if not client.ping():
                            with self._surface_lock(surface_id):
                                self._record_state(surface_id, runtime, ScreenState("cmux_unavailable", reason="socket disappeared during retry"))
                            continue
                        with self._surface_lock(surface_id):
                            self._pause_missing_or_error(target, runtime, str(retry_exc))
                        continue
                else:
                    with self._surface_lock(surface_id):
                        self._pause_missing_or_error(target, runtime, str(exc))
                    continue
            with self._surface_lock(surface_id):
                self._handle_state(
                    target,
                    runtime,
                    state,
                    client,
                    send_guard_tree=send_guard_tree,
                    send_guard_error=send_guard_error,
                )
        self.save()

    def _event_target(self, surface_id: str) -> dict[str, Any] | None:
        with self._targets_lock:
            for target in effective_targets(self.config, self.dynamic_targets.values()):
                if str(target.get("surface_id") or "") == surface_id:
                    return dict(target)
        return None

    def _mark_claude_event(
        self,
        event: Mapping[str, Any],
        runtime: TargetRuntime | None,
        status: str,
        *,
        detail: str = "",
    ) -> None:
        synthetic_fallback = bool(event.get("synthetic_fallback"))
        if runtime is not None:
            runtime.claude_last_event_id = str(event.get("event_id") or "")
            runtime.claude_last_event_status = status
            if not synthetic_fallback:
                runtime.claude_last_hook_at = float(event.get("created_at") or time.time())
            runtime.claude_hook_status = status
            runtime.claude_event_message_hash = str(event.get("message_hash") or "") or None
        self.claude_event_ledger.mark(event, status, detail=detail)

    def _drain_claude_events(self, client: CmuxClient, *, limit: int = 64) -> None:
        for _ in range(limit):
            event = self.claude_event_inbox.get_nowait()
            if event is None:
                return
            self._handle_claude_event_safely(event, client)

    def _handle_claude_event_safely(
        self,
        event: Mapping[str, Any],
        client: CmuxClient,
    ) -> None:
        try:
            self._handle_claude_event(event, client)
        except Exception as exc:
            surface_id = str(event.get("surface_id") or "")
            with self._runtime_lock:
                runtime = self.runtime.get(surface_id)
                self.logger.exception(
                    "surface=%s Claude hook event failed event=%s",
                    surface_id[:8] or "missing",
                    str(event.get("event_id") or "")[:8],
                )
                self._mark_claude_event(event, runtime, "failed", detail=str(exc))
                self.save()
        else:
            self.save()

    def _claude_event_snapshot(
        self,
        target: Mapping[str, Any],
        client: CmuxClient,
    ) -> tuple[Mapping[str, Any], bool, ScreenState, str]:
        surface_id = str(target["surface_id"])
        workspace_id = str(target["workspace_id"])
        tree = client.tree()
        manager_surface_id = str(self.config.get("manager_surface_id") or "")
        if surface_id == manager_surface_id or is_dock_surface(tree, surface_id):
            raise IncompatibleError("management surface cannot receive Claude continuation")
        # Keep read-screen in the preflight: it is viewport-only and proves the
        # target still resolves before the larger render-grid RPC.
        client.read_screen(workspace_id, surface_id)
        grid = Grid.from_rpc(client.replay(workspace_id, surface_id), surface_id)
        composer_kind, _ = _claude_composer_state(grid)
        state = classify_claude_grid(
            grid,
            claude_message=str(self.config.get("claude_message") or CLAUDE_MESSAGE),
        )
        if composer_kind != "empty":
            raise IncompatibleError(f"Claude composer is {composer_kind}")
        if state.kind not in {"claude_stopped", "recoverable_error"}:
            raise IncompatibleError(f"Claude viewport is {state.kind}")
        return tree, is_surface_focused(tree, surface_id), state, grid.signature()

    # Where a released slot lands in the ledger.  Without this the row stayed at
    # ``deferred_<reason>`` forever and the release was silent, so "parked, then
    # nothing" -- which is exactly what a *lost* episode looks like -- could not
    # be told apart from "parked, then correctly cancelled".  Production had 5
    # such rows and zero release log lines.
    _DEFERRED_TERMINAL_STATUS = {
        # The parked Stop was superseded by something that already spoke for
        # this episode, so replaying it would have been the duplicate send.
        "human_prompt": "deferred_cleared",
        "completed": "deferred_cleared",
        "completion_reported": "deferred_cleared",
        "covered_by_later_send": "deferred_cleared",
        "expired": "deferred_expired",
        "generation_changed": "deferred_generation_changed",
        # A newer Stop displaced this one in the single slot.  This is the only
        # release path where an episode is genuinely dropped.
        "superseded": "deferred_dropped",
        # Only reachable if the send path failed to mark ``sent`` itself; the
        # guard below normally skips this case entirely.  Never let a delivered
        # event be filed as dropped.
        "sent": "deferred_recovered",
    }

    def _clear_claude_deferred(self, runtime: TargetRuntime, *, reason: str = "") -> str:
        """Release the deferred slot.  Returns the event id it held, if any.

        Also gives the released event a terminal ledger status and one log line.
        Both are skipped when the slot was empty, so ordinary polls stay quiet.
        """

        deferred = runtime.claude_deferred_event or {}
        held = str(deferred.get("event_id") or "")
        waited = max(0.0, time.time() - runtime.claude_deferred_since) if held else 0.0
        parked_reason = runtime.claude_deferred_reason or ""
        runtime.claude_deferred_event = None
        runtime.claude_deferred_reason = reason or None
        runtime.claude_deferred_since = 0.0
        runtime.claude_deferred_attempts = 0
        runtime.claude_deferred_last_attempt_at = 0.0
        if not held:
            return held
        # ``mark`` replaces the whole row, and the success path marks ``sent``
        # *before* releasing the slot.  Only advance a status that is still the
        # provisional ``deferred_*`` one, or the record of a real delivery would
        # be overwritten by its own cleanup.
        current = self.claude_event_ledger.status_of(held)
        final = current
        if current.startswith("deferred_"):
            # ``undeferrable: <detail>`` embeds the cancel detail, so it cannot
            # be a dict key; it and any future reason both mean "not retried".
            final = self._DEFERRED_TERMINAL_STATUS.get(reason, "deferred_dropped")
            self.claude_event_ledger.mark(
                dict(deferred), final,
                detail=f"parked={parked_reason} released={reason} waited_sec={waited:.0f}",
            )
        self.logger.info(
            "surface=%s Claude deferred stop released event=%s reason=%s "
            "waited_sec=%.0f final_status=%s",
            str(deferred.get("surface_id") or "")[:8],
            held[:8],
            reason or "-",
            waited,
            final or "-",
        )
        return held

    # Transient preflight verdicts: the frame was unsafe *at that instant*.
    # Anything else (identity conflict, context gate, unreadable viewport) is a
    # standing condition and must stay terminal.
    _DEFERRABLE_CANCEL_MARKERS = (
        "composer",
        "viewport is working",
        "content changed",
        "focus changed",
        "text not visible",
        "readback failed",
    )

    @classmethod
    def _cancel_is_transient(cls, detail: str) -> bool:
        lowered = (detail or "").lower()
        if lowered.startswith("context:") or lowered.startswith("process is "):
            return False
        return any(marker in lowered for marker in cls._DEFERRABLE_CANCEL_MARKERS)

    def _defer_claude_event(
        self,
        surface_id: str,
        runtime: TargetRuntime,
        event: Mapping[str, Any],
        reason: str,
    ) -> None:
        """Park one Stop for a later safe frame instead of discarding it."""

        superseded = self._clear_claude_deferred(runtime, reason="superseded")
        parked_episode_id = str(
            event.get("episode_id")
            or runtime.claude_fallback_episode_id
            or f"hook-stop:{event.get('event_id') or uuid.uuid4().hex}"
        )
        parked_generation = str(
            event.get("process_generation") or runtime.claude_process_generation or ""
        )
        parked_event = dict(event)
        parked_event["episode_id"] = parked_episode_id
        parked_event["process_generation"] = parked_generation
        runtime.claude_deferred_event = {
            "event_id": str(parked_event.get("event_id") or ""),
            "event_name": str(parked_event.get("event_name") or ""),
            "surface_id": str(parked_event.get("surface_id") or ""),
            "session_id": str(parked_event.get("session_id") or ""),
            "created_at": float(parked_event.get("created_at") or time.time()),
            "error_kind": str(parked_event.get("error_kind") or "claude_stopped"),
            "completed": False,
            "process_generation": parked_generation,
            "episode_id": parked_episode_id,
        }
        runtime.claude_deferred_reason = reason
        runtime.claude_deferred_since = time.time()
        runtime.claude_deferred_attempts = 0
        runtime.claude_deferred_last_attempt_at = 0.0
        self._mark_claude_event(parked_event, runtime, f"deferred_{reason}", detail=reason)
        self.logger.info(
            "surface=%s Claude stop deferred event=%s reason=%s superseded=%s",
            surface_id[:8],
            str(event.get("event_id") or "")[:8],
            reason,
            superseded[:8] or "-",
        )

    def _restore_expired_claude_deferred(self, runtime: TargetRuntime) -> bool:
        """Rehydrate a deferred Stop that expired before the retry policy existed.

        Older daemon instances cleared the slot at the 900-second boundary and
        left only ``deferred_expired`` in the ledger.  A Claude surface can then
        remain stopped forever because no new lifecycle event is required to
        describe the same unfinished turn.  Restore only that exact ledger row,
        with a fresh local retry timestamp; the send path still performs the
        current two-frame safety check before touching the terminal.
        """

        if runtime.claude_deferred_event:
            return False
        if runtime.claude_deferred_reason != "expired":
            return False
        if runtime.claude_hook_health != "healthy":
            return False
        event_id = str(runtime.claude_last_event_id or "")
        if not event_id:
            return False
        with self.claude_event_ledger._lock:
            row = self.claude_event_ledger.events.get(event_id)
            # Older daemons did not always advance the ledger row when the
            # 900-second slot expired.  In production this left the exact
            # event at ``deferred_active_stop_hook`` even though runtime.json
            # already said ``reason=expired``.  Accept any still-provisional
            # deferred status, but never resurrect a terminal cleared/sent row.
            row_status = str(row.get("status") or "") if isinstance(row, Mapping) else ""
            if not isinstance(row, Mapping) or not row_status.startswith("deferred_"):
                return False
            row_generation = str(row.get("process_generation") or "")
            row_episode = str(row.get("episode_id") or "")
            if row_generation and row_generation != str(runtime.claude_process_generation or ""):
                self.logger.warning(
                    "surface=%s expired deferred row belongs to old generation event=%s",
                    str(row.get("surface_id") or "")[:8], event_id[:8],
                )
                return False
            if row_episode and runtime.claude_fallback_episode_id and row_episode != runtime.claude_fallback_episode_id:
                self.logger.warning(
                    "surface=%s expired deferred row belongs to old episode event=%s",
                    str(row.get("surface_id") or "")[:8], event_id[:8],
                )
                return False
            event = {
                "event_id": event_id,
                "event_name": str(row.get("event_name") or "Stop"),
                "surface_id": str(row.get("surface_id") or ""),
                "session_id": str(row.get("session_id") or runtime.claude_session_id or ""),
                "created_at": time.time(),
                "error_kind": "claude_stopped",
                "completed": False,
                "process_generation": row_generation or str(runtime.claude_process_generation or ""),
                "episode_id": row_episode or str(runtime.claude_fallback_episode_id or ""),
            }
        now = time.time()
        runtime.claude_deferred_event = event
        runtime.claude_deferred_reason = "expired_retry"
        runtime.claude_deferred_since = now
        runtime.claude_deferred_attempts = 0
        runtime.claude_deferred_last_attempt_at = 0.0
        runtime.state = "claude_deferred_retrying"
        self.logger.warning(
            "surface=%s Claude expired deferred stop requeued event=%s retrying=true",
            event["surface_id"][:8], event_id[:8],
        )
        return True

    def _maybe_send_deferred_claude_stop(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        state: ScreenState,
        client: CmuxClient,
    ) -> bool:
        """Retry one parked Stop once the viewport is safe again.

        Returns True when this poll is fully handled.  The guards here exist to
        make the retry strictly a recovery of a *lost* send and never an extra
        one: any send that happened after the deferral already covered this
        episode, so the slot is dropped rather than replayed.
        """

        deferred = runtime.claude_deferred_event
        if not deferred:
            return False
        surface_id = str(target["surface_id"])
        now = time.time()
        deferred_generation = str(deferred.get("process_generation") or "")
        current_generation = str(runtime.claude_process_generation or "")
        deferred_session = str(deferred.get("session_id") or "")
        current_session = str(runtime.claude_session_id or "")
        if (
            (deferred_generation and deferred_generation != current_generation)
            or (deferred_session and current_session and deferred_session != current_session)
        ):
            self._clear_claude_deferred(runtime, reason="generation_changed")
            return False
        max_age = float(self.config.get(
            "claude_deferred_max_age_sec", CLAUDE_DEFERRED_MAX_AGE_SEC,
        ))
        waited = max(0.0, now - runtime.claude_deferred_since)
        if waited >= max_age:
            # Do not discard the only evidence that this Stop still needs a
            # continuation.  Rebase the bounded retry window and let the same
            # current-frame guards decide whether to send now or on the next
            # poll.  This prevents a protected transient from becoming a
            # permanent ``claude_hook_waiting`` blind spot.
            runtime.claude_deferred_since = now
            runtime.claude_deferred_attempts = 0
            runtime.claude_deferred_last_attempt_at = 0.0
            runtime.claude_deferred_reason = "expired_retry"
            runtime.state = "claude_deferred_retrying"
            self.logger.warning(
                "surface=%s Claude deferred stop expired event=%s waited_sec=%.0f "
                "retrying=true continuing_to_monitor=true",
                surface_id[:8], str(deferred.get("event_id") or "")[:8], waited,
            )
            waited = 0.0
        # Anything newer than the deferral already spoke for this episode:
        # our own send, a completion latch, or a live submit transaction.
        if runtime.last_send_at > runtime.claude_deferred_since:
            self._clear_claude_deferred(runtime, reason="covered_by_later_send")
            return False
        if runtime.claude_completed_latched:
            self._clear_claude_deferred(runtime, reason="completed")
            return False
        if runtime.claude_submit_phase != "none":
            return False
        if target.get("paused", False) or not target.get("enabled", True):
            return False
        if not bool(self.config.get("claude_enabled", False)):
            return False
        if self.config.get("mode") != "armed" or self.config.get("global_paused", False):
            return False
        # A lifecycle Hook already established that this Claude turn stopped.
        # Runtime guards intentionally rewrite a stopped Claude viewport to
        # ``claude_hook_waiting`` so ordinary screen polling cannot send; a
        # deferred Hook retry is the one exception, and _send_claude_event's
        # own two-frame preflight still rejects a working/composer-busy frame.
        if state.kind not in SEND_ELIGIBLE_STATES and not (
            state.kind == "claude_hook_waiting"
            and runtime.claude_hook_health == "healthy"
        ):
            return False
        # Source "deferred" keeps these out of the socket SLA sample: their
        # latency is measured from a Hook that fired minutes ago, and mixing
        # them in would corrupt the very distribution used to judge the SLA.
        event = dict(deferred)
        event["_inbox_source"] = "deferred"
        event["_inbox_at"] = now
        event["_worker_started_at"] = now
        runtime.claude_deferred_attempts += 1
        runtime.claude_deferred_last_attempt_at = now
        attempts = runtime.claude_deferred_attempts
        sent, detail = self._send_claude_event(event, target, runtime, client)
        if sent:
            self._clear_claude_deferred(runtime, reason="sent")
            self.logger.warning(
                "surface=%s Claude deferred stop recovered event=%s waited_sec=%.0f "
                "attempts=%d",
                surface_id[:8], str(event["event_id"])[:8], waited, attempts,
            )
            return True
        if not self._cancel_is_transient(detail):
            held = self._clear_claude_deferred(runtime, reason=f"undeferrable: {detail}")
            self.logger.info(
                "surface=%s Claude deferred stop dropped event=%s reason=%s",
                surface_id[:8], held[:8], detail,
            )
        return True

    def _apply_human_prompt_reset(
        self,
        surface_id: str,
        runtime: TargetRuntime,
        event: Mapping[str, Any],
        *,
        attribution: str = "human_prompt",
    ) -> None:
        """Start a fresh human-owned turn: this is what unlatches a surface.

        Shared by both prompt paths so a verbatim paste and a differently worded
        prompt produce exactly the same recovery.  The asymmetry between them was
        the whole defect.

        ``attribution`` must be supplied by the caller that actually decided it.
        This used to read ``runtime.claude_last_prompt_attribution``, which is
        only written on the exact-prompt paths, so an ordinary human prompt with
        completely different text inherited the *previous* event's verdict: 24
        of surface:36's 25 post-deploy ``human_prompt`` ledger rows carried
        ``ambiguous_exact_prompt``, and one carried ``human_exact_prompt``, which
        also fired the "matched watchdog text" log line for a prompt that never
        matched it.  Attribution belongs to one event and must not leak forward.
        """

        event_id = str(event.get("event_id") or "")
        # Record this event's own verdict.  Written here rather than read from
        # here: the field is forensics for the turn just started, never the
        # default for the next one.
        runtime.claude_last_prompt_attribution = attribution
        runtime.claude_fallback_last_fingerprint = None
        runtime.claude_fallback_last_event_id = None
        runtime.claude_fallback_sent_at = 0.0
        runtime.claude_fallback_retry_count = 0
        runtime.claude_fallback_retry_exhausted = False
        runtime.claude_generation_id = event_id
        runtime.claude_completed_latched = False
        runtime.claude_completion_event_id = None
        runtime.claude_completion_fingerprint = None
        runtime.claude_completed_at = 0.0
        runtime.send_count = 0
        self._reset_claude_repeat_warning(runtime)
        runtime.state = "claude_hook_waiting"
        # A human taking the turn back supersedes any deferred Stop: that Stop
        # described output the user has now replaced.
        self._clear_claude_deferred(runtime, reason="human_prompt")
        self._mark_claude_event(event, runtime, "human_prompt", detail=attribution)
        if attribution == "human_exact_prompt" and runtime.claude_last_submit_at > 0:
            self.logger.info(
                "surface=%s human prompt matched watchdog text but not our submit "
                "record; treating as human and clearing completion latch",
                surface_id[:8],
            )

    @staticmethod
    def _hashes_agree(left: str, right: str) -> bool:
        """Compare two digests of the same value truncated to different widths.

        ``claude_ccc_protocol._digest`` keeps 24 hex chars; this module's
        ``_short_hash`` keeps 16.  Both are SHA-256 of the same bytes, so one is
        a strict prefix of the other -- but a plain ``==`` between them is
        *always* False.  Getting this wrong would classify every one of our own
        echoes as a human prompt, clear the completion latch and re-arm sending:
        the exact opposite of the bug being fixed.
        """

        if not left or not right:
            return False
        width = min(len(left), len(right))
        return left[:width] == right[:width]

    def _attribute_exact_prompt(
        self,
        runtime: TargetRuntime,
        event: Mapping[str, Any],
    ) -> str:
        """Decide whether a byte-identical prompt is our echo or a human paste.

        Text equality alone cannot answer this: the user can paste the watchdog
        sentence verbatim, and on 2026-08-25 surface:36 that is exactly what
        happened.  ``prompt_kind`` said "watchdog", so the completion latch was
        never cleared, ``send_count`` never reset, and automatic continuation
        stayed off until a *differently worded* prompt arrived.  The user had to
        keep continuing the session by hand.

        Correlation, not text, is the evidence: our own echo must match the
        surface's last submitted message hash, the same Claude session and
        process generation, and arrive inside a bounded window.
        """

        window = float(self.config.get(
            "claude_watchdog_echo_window_sec", CLAUDE_WATCHDOG_ECHO_WINDOW_SEC,
        ))
        sent_at = runtime.claude_last_submit_at
        if sent_at <= 0:
            return "human_exact_prompt"
        tolerance = float(self.config.get(
            "claude_echo_clock_tolerance_sec", CLAUDE_ECHO_CLOCK_TOLERANCE_SEC,
        ))
        age = float(event.get("created_at") or time.time()) - sent_at
        if age > window:
            return "human_exact_prompt"
        if age < -tolerance:
            # Far enough before our anchor that no clock step explains it.  The
            # anchor is stamped before Enter leaves this process, so a genuine
            # echo cannot precede it by more than clock noise.
            return "human_exact_prompt"
        if age < 0:
            # Slightly before the anchor: a clock step, not a human.  Calling
            # this "human" is the 22:01:16 regression -- it cleared the
            # completion latch and reset send_count on our own echo.
            age = 0.0
        stored_hash = str(runtime.claude_last_submit_message_hash or "")
        event_hash = str(event.get("message_hash") or "")
        if stored_hash and event_hash:
            if not self._hashes_agree(stored_hash, event_hash):
                # Different text under the same words-look: a human typed this.
                return "human_exact_prompt"
        elif not event_hash:
            # No hash to compare.  Everything else about this event correlates
            # with a send we just made, so calling it a human prompt would clear
            # the completion latch and reset the resume counters on our own echo.
            # Absence of evidence is not evidence of a human.
            return "ambiguous_exact_prompt"
        session_id = str(event.get("session_id") or "")
        if session_id and runtime.claude_last_submit_session_id and (
            session_id != runtime.claude_last_submit_session_id
        ):
            return "human_exact_prompt"
        generation = runtime.claude_process_generation or ""
        if (
            generation
            and runtime.claude_last_submit_generation
            and generation != runtime.claude_last_submit_generation
        ):
            return "human_exact_prompt"
        # Inside the window with everything matching.  A human *could* have
        # pasted it in these few seconds; that is unprovable at the Hook layer.
        # Treat it as our echo (never sending twice is the safer error) but say
        # so plainly rather than claiming certainty.
        return "ambiguous_exact_prompt" if age <= 1.0 else "watchdog_echo_correlated"

    @staticmethod
    def _clear_claude_submit(runtime: TargetRuntime, *, reason: str = "") -> None:
        runtime.claude_submit_event_id = None
        runtime.claude_submit_message_hash = None
        runtime.claude_submit_fingerprint = None
        runtime.claude_submit_since = 0.0
        runtime.claude_submit_last_attempt_at = 0.0
        runtime.claude_submit_phase = "none"
        runtime.claude_submit_attempts = 0
        if reason in {"confirmed", "hook_confirmed", "completion_reported"}:
            runtime.claude_submit_confirmed_at = runtime.claude_submit_confirmed_at or time.time()
        else:
            runtime.claude_submit_confirmed_at = 0.0
        runtime.claude_submit_last_reason = reason or runtime.claude_submit_last_reason

    def _send_claude_enter(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        client: CmuxClient,
        *,
        reason: str,
    ) -> bool:
        """Submit only the already verified watchdog text with an explicit key."""

        surface_id = str(target["surface_id"])
        try:
            client.send_key(str(target["workspace_id"]), surface_id, "enter")
        except (CmuxError, RuntimeError) as exc:
            runtime.claude_submit_last_reason = f"enter failed: {exc}"
            self.logger.warning(
                "surface=%s Claude submit Enter failed phase=%s reason=%s",
                surface_id[:8], runtime.claude_submit_phase, reason,
            )
            return False
        now = time.time()
        runtime.claude_submit_phase = "enter_sent"
        runtime.claude_submit_last_attempt_at = now
        runtime.claude_submit_attempts += 1
        runtime.claude_last_event_status = "submitted"
        runtime.claude_hook_status = "submitted"
        self.logger.info(
            "surface=%s Claude submit Enter phase=enter_sent attempt=%d reason=%s",
            surface_id[:8], runtime.claude_submit_attempts, reason,
        )
        return True

    def _recover_orphan_watchdog_submit(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        state: ScreenState,
        client: CmuxClient,
    ) -> bool:
        """Press Enter for watchdog text stranded without an owning transaction.

        Measured on surface:43 (2026-08-31): a submit transaction hit
        ``confirmation_timeout``, which clears ``claude_submit_phase`` to ``none``
        *while its text is still in the composer*.  After that, nobody can submit
        it: ``_reconcile_claude_submit`` returns at its entry gate because the
        phase is ``none``, and ``_claude_event_snapshot`` refuses to open a new
        transaction because the composer is not empty.  The text sat there for
        minutes with ``claude_submit_attempts=0``.

        The only key pressed here is Enter, and only when the composer is proven
        to hold *our exact watchdog prompt* -- ``state.watchdog_echo`` is an exact
        normalised match against ``claude_message``, reconstructed across wrapped
        rows.  A user's own text can never satisfy it, so this cannot submit
        something a human typed.  Returning True claims the poll so the deferred
        and fallback paths do not also act on the same surface.
        """

        # Every other Enter/text site checks this pair; recovery is a *new* send
        # origin rather than the continuation of an armed transaction, so it has
        # to check them too.  Without this a globally paused daemon still pressed
        # Enter, because the pause was only ever enforced where a transaction was
        # opened -- and this path deliberately runs where there is none.
        if self.config.get("mode") != "armed" or self.config.get("global_paused", False):
            return False
        if not state.watchdog_echo:
            return False
        # Only a composer that still holds the text is recoverable.  ``working``
        # and ``menu`` mean Claude already consumed it.
        if state.kind != "composer_busy":
            return False
        surface_id = str(target["surface_id"])
        # F2（2026-09-01）：总量上限。间隔限制只保证"每 5 秒最多一次"，不保证
        # 总次数有限；预算耗尽即 degraded，把决定权交还给人而不是永远按键。
        limit = int(self.config.get("claude_orphan_enter_max", CLAUDE_ORPHAN_ENTER_MAX))
        if runtime.claude_orphan_enter_count >= limit:
            if runtime.state != "claude_orphan_degraded":
                self.logger.warning(
                    "surface=%s orphan watchdog composer: Enter budget exhausted "
                    "count=%d limit=%d reason=degraded -- no further automatic "
                    "Enter until the composer changes, a new submit transaction "
                    "opens, or the daemon is re-armed",
                    surface_id[:8], runtime.claude_orphan_enter_count, limit,
                )
                runtime.state = "claude_orphan_degraded"
            return True
        now = time.time()
        retry = float(self.config.get(
            "claude_orphan_enter_retry_sec", CLAUDE_ORPHAN_ENTER_RETRY_SEC,
        ))
        if runtime.claude_orphan_enter_at and now - runtime.claude_orphan_enter_at < retry:
            # Bounded per unit is not bounded in total, so the count below is
            # kept as its own observable rather than inferred from timestamps.
            return True
        runtime.claude_orphan_enter_at = now
        runtime.claude_orphan_enter_count += 1
        self.logger.warning(
            "surface=%s orphan watchdog composer: pressing Enter count=%d last_reason=%s",
            surface_id[:8], runtime.claude_orphan_enter_count,
            runtime.claude_submit_last_reason,
        )
        self._send_claude_enter(target, runtime, client, reason="orphan_watchdog_echo")
        runtime.state = "claude_orphan_enter"
        return True

    def _reconcile_claude_submit(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        state: ScreenState,
        client: CmuxClient,
    ) -> bool:
        """Advance one persisted Claude submit transaction.

        Returns True when the caller must skip normal event/fallback handling
        for this poll.  A user-owned composer cancels the transaction; an
        unchanged watchdog composer retries Enter only, never the full text.
        """

        if runtime.claude_submit_phase == "none" or not runtime.claude_submit_event_id:
            # F2 重置点之一：composer 内容变化即孤儿事件结束。``empty`` 表示
            # 文字已被消费或清空；``composer_busy`` 且回显不再匹配表示用户在
            # 编辑。两者都终结当前孤儿事件，预算归零。working/menu 等瞬态帧
            # 不重置——回显判定在这些帧上本来就不成立，靠它们重置会让预算被
            # 渲染抖动清零。
            if runtime.claude_orphan_enter_count and (
                state.kind == "empty"
                or (state.kind == "composer_busy" and not state.watchdog_echo)
            ):
                runtime.claude_orphan_enter_count = 0
                runtime.claude_orphan_enter_at = 0.0
            # No live transaction -- but our own text can still be sitting in the
            # composer, and then nobody is left to submit it.  Measured on
            # surface:43 (2026-08-31 14:51 IST): a prior transaction hit
            # ``confirmation_timeout``, which clears ``phase`` to ``none`` while
            # leaving the typed text on screen.  From then on this gate returned
            # False (no transaction to advance) and _claude_event_snapshot refused
            # to open a new one (composer not empty), so the prompt stayed in the
            # composer for 283s with attempts=0.  Deadlock, not a stuck keypress.
            #
            # Recovery presses Enter for text we can *prove* is ours:
            # ``watchdog_echo`` is an exact normalised match of claude_message
            # reconstructed across wrapped rows, so a user's text can never
            # satisfy it.  Everything else -- paused/disabled targets, the runtime
            # and context guards, busy/error states -- is decided upstream of this
            # call, so no safety gate is bypassed by acting here.
            return self._recover_orphan_watchdog_submit(target, runtime, state, client)
        surface_id = str(target["surface_id"])
        now = time.time()
        age = max(0.0, now - runtime.claude_submit_since)
        timeout = float(self.config.get(
            "claude_submit_confirm_timeout_sec", CLAUDE_SUBMIT_CONFIRM_TIMEOUT_SEC,
        ))
        if age >= timeout:
            self.logger.warning(
                "surface=%s Claude submit confirmation timeout event=%s attempts=%d",
                surface_id[:8], str(runtime.claude_submit_event_id)[:8],
                runtime.claude_submit_attempts,
            )
            self._clear_claude_submit(runtime, reason="confirmation_timeout")
            runtime.state = "claude_submit_unconfirmed"
            return True

        # Once Claude has left the composer, the explicit Enter was accepted.
        # Working/menu are both valid post-submit states; Hook events provide a
        # stronger confirmation asynchronously but are not required for the
        # transaction to stop retrying.
        if state.kind in {"working", "menu"} and not state.watchdog_echo:
            runtime.claude_submit_confirmed_at = now
            self.logger.info(
                "surface=%s Claude submit confirmed state=%s event=%s",
                surface_id[:8], state.kind, str(runtime.claude_submit_event_id)[:8],
            )
            self._clear_claude_submit(runtime, reason="confirmed")
            return True

        if state.kind == "composer_busy" and not state.watchdog_echo:
            self.logger.info(
                "surface=%s Claude submit cancelled: composer changed to user text",
                surface_id[:8],
            )
            self._clear_claude_submit(runtime, reason="user_input_conflict")
            runtime.state = "composer_busy"
            return True

        if state.watchdog_echo:
            retry = float(self.config.get(
                "claude_submit_retry_enter_sec", CLAUDE_SUBMIT_RETRY_ENTER_SEC,
            ))
            if runtime.claude_submit_phase == "text_written" or (
                now - runtime.claude_submit_last_attempt_at >= retry
            ):
                self._send_claude_enter(target, runtime, client, reason="pending_watchdog_echo")
            runtime.state = "claude_submit_pending"
            return True

        # An unreadable/incompatible frame cannot prove either ownership or
        # completion. Keep the transaction pending until the bounded timeout.
        runtime.state = "claude_submit_pending"
        return True

    def _send_claude_event(
        self,
        event: Mapping[str, Any],
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        client: CmuxClient,
    ) -> tuple[bool, str]:
        surface_id = str(target["surface_id"])
        preflight_started = time.time()
        # In-process durations use perf_counter (monotonic, unaffected by clock
        # steps); only the cross-process latency below stays on epoch time,
        # because the Hook stamped ``created_at`` in another process.
        perf_started = time.perf_counter()
        # Per-stage timing.  A send inherently costs ~10 cmux RPCs: six in the
        # double-frame preflight (two snapshots x tree/read_screen/replay) plus
        # four in the submit transaction (send_text, readback read_screen,
        # readback replay, send-key enter), around a 120ms settle and three
        # fsync'ing save() calls.  "It was slow" was never actionable, so each
        # phase is timed separately and the two phases are never mixed.
        stages: dict[str, float] = {}
        stage_mark = time.perf_counter()

        def _stage(name: str) -> None:
            nonlocal stage_mark
            current = time.perf_counter()
            stages[name] = round((current - stage_mark) * 1000.0, 1)
            stage_mark = current

        # Submit-phase timing is kept in a *separate* dict.  The first version of
        # this instrumentation measured "preflight" all the way to the log call,
        # which silently swallowed the whole submit transaction (send_text, the
        # readback, the explicit Enter and three fsync'ing save() calls).  That
        # made ``preflight=`` unusable as evidence about preflight cost, which was
        # its only purpose.
        # Submit phase is measured in its own dict with its own sub-segments.
        # Codex's 2026-08-25 review showed the earlier naming was wrong: the
        # reservation save() was folded into ``text_ms``, ``persist_ms`` named
        # only the middle save(), and the post-send work was in no segment at
        # all.  Each boundary below now matches exactly one operation.
        submit_stages: dict[str, float] = {}
        post_send_stages: dict[str, float] = {}
        submit_mark = 0.0

        def _submit_stage(name: str) -> None:
            nonlocal submit_mark
            current = time.perf_counter()
            submit_stages[name] = round((current - submit_mark) * 1000.0, 1)
            submit_mark = current

        def _post_stage(name: str) -> None:
            nonlocal submit_mark
            current = time.perf_counter()
            post_send_stages[name] = round((current - submit_mark) * 1000.0, 1)
            submit_mark = current

        process = self._candidate_process_label(target, client)
        _stage("process_ms")
        if process.get("agent_kind") != "claude":
            return False, f"process is {process.get('agent_kind') or 'unknown'}"
        try:
            _, focused_before, first, first_signature = self._claude_event_snapshot(target, client)
            _stage("frame1_ms")
            time.sleep(CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC)
            _stage("settle_ms")
            _, focused_after, final, final_signature = self._claude_event_snapshot(target, client)
            _stage("frame2_ms")
        except (CmuxError, IncompatibleError) as exc:
            return False, str(exc)
        if focused_before != focused_after:
            return False, "focus changed during preflight"
        final = self._apply_claude_context_guard(surface_id, runtime, final)
        _stage("guard_ms")
        if final.kind in {
            "claude_context_waiting",
            "claude_context_compacting",
            "claude_context_stalled",
        }:
            return False, f"context:{final.kind}"
        if first.content_fingerprint != final.content_fingerprint:
            return False, "assistant content changed during preflight"
        _stage("verify_ms")
        # Freeze preflight cost here, at the last preflight stage.  Everything
        # after this line is the submit transaction and is timed on its own.
        preflight_duration = max(0.0, time.perf_counter() - perf_started)
        submit_mark = time.perf_counter()

        runtime.claude_last_event_id = str(event["event_id"])
        runtime.claude_last_event_status = "reserved"
        runtime.claude_hook_status = "reserved"
        runtime.state = "claude_event_reserved"
        runtime.sent_screen_signature = final_signature or first_signature
        _submit_stage("reserve_bookkeeping_ms")
        # Two durable writes happen here, and both fsync.  They were previously
        # folded into ``text_ms``, which made send_text look several times more
        # expensive than it is and hid disk latency entirely.
        self.claude_event_ledger.mark(event, "reserved", detail="before cmux send")
        _submit_stage("ledger_reserve_ms")
        self.save()
        _submit_stage("persist_reserve_ms")
        message = str(self.config.get("claude_message") or CLAUDE_MESSAGE)
        runtime.claude_submit_event_id = str(event["event_id"])
        runtime.claude_submit_message_hash = _short_hash(message)
        runtime.claude_submit_fingerprint = final.content_fingerprint or final_signature or first_signature
        runtime.claude_submit_since = time.time()
        runtime.claude_submit_last_attempt_at = 0.0
        runtime.claude_submit_phase = "text_written"
        runtime.claude_submit_attempts = 0
        runtime.claude_submit_confirmed_at = 0.0
        # F2 重置点之二：新事务重新拥有 composer，上一个孤儿事件的预算作废。
        runtime.claude_orphan_enter_count = 0
        runtime.claude_orphan_enter_at = 0.0
        _submit_stage("transaction_init_ms")
        try:
            client.send_text(str(target["workspace_id"]), surface_id, message)
        except (CmuxError, RuntimeError) as exc:
            self._clear_claude_submit(runtime, reason=f"text failed: {exc}")
            return False, f"cmux send text failed after reservation: {exc}"
        _submit_stage("text_ms")
        # Close the race with a human typing between the double-frame
        # preflight and the write.  Only press Enter after the exact watchdog
        # text is visible in this surface's own viewport; otherwise leave the
        # transaction pending for the next poll and never press a user's key.
        try:
            rendered = client.read_screen(str(target["workspace_id"]), surface_id)
            if _normalise_claude_prompt(message) not in _normalise_claude_prompt(rendered):
                runtime.claude_submit_last_reason = "watchdog text not visible after send"
                return False, "Claude submit text not visible; waiting for confirmation"
            verification_grid = Grid.from_rpc(
                client.replay(str(target["workspace_id"]), surface_id), surface_id,
            )
            verification_state = classify_claude_grid(
                verification_grid, claude_message=message,
            )
            if not verification_state.watchdog_echo:
                runtime.claude_submit_last_reason = "composer changed before Enter"
                return False, "Claude submit composer changed before Enter"
        except (CmuxError, RuntimeError) as exc:
            runtime.claude_submit_last_reason = f"submit readback failed: {exc}"
            return False, f"Claude submit readback failed; waiting for confirmation: {exc}"
        _submit_stage("readback_ms")
        self.save()
        _submit_stage("persist_pre_enter_ms")
        # Stamp the echo-correlation anchor *before* the key leaves this process.
        # Claude Code timestamps UserPromptSubmit the moment Enter lands, while
        # this function only reached its own bookkeeping tens of milliseconds
        # later (enter_ms 40-87ms plus a ledger write and an fsync).  Anchoring
        # afterwards made our own echo arrive *earlier* than the record of the
        # send that caused it, and a negative age was read as "human": the
        # completion latch was cleared and send_count reset on our own echo.
        # Observed live on 2026-08-25 at 22:01:16 (age -0.013s, count 3 -> 1).
        prior_submit_anchor = (
            runtime.claude_last_submit_event_id,
            runtime.claude_last_submit_message_hash,
            runtime.claude_last_submit_session_id,
            runtime.claude_last_submit_generation,
            runtime.claude_last_submit_at,
        )
        runtime.claude_last_submit_event_id = str(event["event_id"])
        runtime.claude_last_submit_message_hash = runtime.claude_submit_message_hash
        runtime.claude_last_submit_session_id = runtime.claude_session_id
        runtime.claude_last_submit_generation = runtime.claude_process_generation
        runtime.claude_last_submit_at = time.time()
        # A newline passed to ``cmux send`` is not a reliable submission on
        # every cmux/Claude combination. Always issue an explicit Enter now;
        # later polls retry only this key while the exact watchdog echo stays.
        entered = self._send_claude_enter(target, runtime, client, reason="initial_submit")
        _submit_stage("enter_ms")
        if not entered:
            # Nothing was submitted, so nothing can echo: restore the previous
            # anchor rather than leaving a record of a send that never happened.
            (
                runtime.claude_last_submit_event_id,
                runtime.claude_last_submit_message_hash,
                runtime.claude_last_submit_session_id,
                runtime.claude_last_submit_generation,
                runtime.claude_last_submit_at,
            ) = prior_submit_anchor
            # The Enter key never reached cmux, so nothing was submitted: the
            # text is sitting unsent in the composer.  This used to fall through
            # and record ``sent`` anyway -- incrementing send_count, stamping
            # last_send_at and counting one SLA sample -- so the ledger, the
            # send counter and the SLA all reported a delivery that did not
            # happen.  The transaction stays in ``text_written`` and
            # _reconcile_claude_submit retries only the key on a later poll.
            return False, "Claude submit Enter failed; transaction stays pending"
        now = time.time()
        runtime.last_send_at = now
        runtime.send_count += 1
        runtime.claude_consecutive_resumes += 1
        runtime.error_type = str(event.get("error_kind") or "claude_stopped")
        runtime.claude_last_resume_event_id = str(event["event_id"])
        runtime.claude_last_event_status = "sent"
        runtime.claude_hook_status = "sent"
        runtime.state = "claude_event_sent"
        runtime.claude_prompt_pending = False
        runtime.claude_working_polls = 0
        # The echo-correlation anchor was already stamped above, before the Enter
        # key left this process.  Re-stamping it here with a *later* clock read is
        # what created the negative-age window; the anchor is deliberately not
        # cleared with the transaction, because the echo usually arrives after the
        # transaction is confirmed and gone.
        _post_stage("finalize_bookkeeping_ms")
        self.claude_event_ledger.mark(event, "sent")
        _post_stage("ledger_finalize_ms")
        self.save()
        _post_stage("persist_finalize_ms")
        warning_after = int(self.config.get("claude_repeat_warning_after", CLAUDE_REPEAT_WARNING_AFTER))
        if runtime.claude_consecutive_resumes >= warning_after and not runtime.claude_repeat_warning:
            runtime.claude_repeat_warning = True
            runtime.claude_repeat_warning_at = now
            self._notify_async(
                "CCC Claude 连续中断",
                f"surface {surface_id[:8]} 已连续自动续跑 {runtime.claude_consecutive_resumes} 次；仍在持续监控和恢复",
                name=f"ccc-notify-{surface_id[:8]}",
            )
            self.logger.warning(
                "surface=%s Claude repeated interruptions count=%d continuing=true",
                surface_id[:8],
                runtime.claude_consecutive_resumes,
            )
            self.save()
            _post_stage("notify_ms")
        latency = max(0.0, now - float(event.get("created_at") or now))
        inbox_source = str(event.get("_inbox_source") or "socket")
        inbox_at = float(event.get("_inbox_at") or preflight_started)
        worker_started_at = float(event.get("_worker_started_at") or preflight_started)
        queue_delay = max(0.0, worker_started_at - inbox_at)
        sla_sec = float(self.config.get("claude_hook_sla_sec", CLAUDE_HOOK_SLA_SEC))
        sla_miss = hook_sla_missed(latency, sla_sec)
        # The ledger claim fsync happens in _handle_claude_event_locked, before
        # this method is entered, yet it is inside the end-to-end latency the SLA
        # judges.  Carrying it here keeps the accounting complete.
        claim_duration = float(
            event.get("_claim_ms")
            or self._claude_claim_ms.pop(str(event.get("event_id") or ""), 0.0)
        ) / 1000.0
        if inbox_source == "socket":
            latency_ms = latency * 1000.0
            window = int(self.config.get("claude_hook_latency_window", CLAUDE_HOOK_LATENCY_WINDOW))
            runtime.claude_hook_latency_ms = (
                list(runtime.claude_hook_latency_ms) + [latency_ms]
            )[-window:]
            runtime.claude_hook_live_send_count += 1
            runtime.claude_hook_last_latency_ms = latency_ms
            runtime.claude_hook_max_latency_ms = max(runtime.claude_hook_max_latency_ms, latency_ms)
            if sla_miss:
                runtime.claude_hook_sla_miss_count += 1
        elif inbox_source == "journal_replay":
            runtime.claude_hook_replay_count += 1
            runtime.claude_hook_replay_max_age_sec = max(
                runtime.claude_hook_replay_max_age_sec,
                latency,
            )
        # Name the slowest *preflight* stage whenever preflight eats half the SLA
        # budget.  slow_stage is chosen only from the six preflight stages on
        # purpose: the submit stages run after the send decision is already made,
        # so blaming one of them would misdirect any fix.  ``latency`` remains the
        # end-to-end figure the SLA is judged on; ``preflight`` and ``submit`` now
        # split it into the two halves that have different causes and cures.
        _post_stage("sla_bookkeeping_ms")
        submit_duration = sum(submit_stages.values()) / 1000.0
        post_send_duration = sum(post_send_stages.values()) / 1000.0
        # ``persist_ms`` is the fsync cost *inside* the SLA window only.  There
        # are exactly two state persists before ``now`` above; the third save()
        # and the ledger "sent" write happen after it and belong to post-send.
        persist_ms = sum(
            value for name, value in submit_stages.items() if name.startswith("persist_")
        )
        slow_stage = max(stages, key=lambda name: stages[name]) if stages else ""
        slow_submit_stage = (
            max(submit_stages, key=lambda name: submit_stages[name]) if submit_stages else ""
        )
        slow_preflight = preflight_duration >= 0.5 * sla_sec
        stage_text = " ".join(f"{name}={value:.1f}" for name, value in stages.items())
        submit_text = " ".join(f"{name}={value:.1f}" for name, value in submit_stages.items())
        post_text = " ".join(f"{name}={value:.1f}" for name, value in post_send_stages.items())
        log = self.logger.warning if inbox_source == "socket" and sla_miss else self.logger.info
        log(
            "surface=%s sent=claude_hook event=%s source=%s latency=%.3fs queue=%.3fs "
            "claim_ms=%.1f preflight=%.3fs submit=%.3fs persist_ms=%.1f post_send=%.3fs "
            "sla_miss=%s count=%d slow_preflight=%s slow_stage=%s slow_submit_stage=%s %s %s %s",
            surface_id[:8],
            str(event["event_id"])[:8],
            inbox_source,
            latency,
            queue_delay,
            claim_duration * 1000.0,
            preflight_duration,
            submit_duration,
            persist_ms,
            post_send_duration,
            str(inbox_source == "socket" and sla_miss).lower(),
            runtime.send_count,
            str(slow_preflight).lower(),
            slow_stage if slow_preflight else "",
            slow_submit_stage,
            stage_text,
            submit_text,
            post_text,
        )
        return True, "sent"

    @staticmethod
    def _reset_claude_repeat_warning(runtime: TargetRuntime) -> None:
        runtime.claude_consecutive_resumes = 0
        runtime.claude_repeat_warning = False
        runtime.claude_repeat_warning_at = 0.0

    def _handle_claude_event(
        self,
        event: Mapping[str, Any],
        client: CmuxClient,
    ) -> str | None:
        surface_id = str(event.get("surface_id") or "")
        with self._surface_lock(surface_id or "__unmapped__"):
            return self._handle_claude_event_locked(event, client)

    def _handle_claude_event_locked(
        self,
        event: Mapping[str, Any],
        client: CmuxClient,
    ) -> str | None:
        event_id = str(event.get("event_id") or "")
        if not _valid_claude_event(event):
            return
        claim_mark = time.perf_counter()
        claim_result = self.claude_event_ledger.claim_detailed(event)
        if claim_result != CLAUDE_CLAIMED:
            if claim_result == CLAUDE_HISTORICAL_ID_COLLISION:
                self.logger.error(
                    "surface=%s Claude ledger id collision event=%s episode=%s "
                    "generation=%s attempt=%s",
                    str(event.get("surface_id") or "")[:8],
                    event_id[:8],
                    str(event.get("episode_id") or "")[:8] or "-",
                    str(event.get("process_generation") or "")[:8] or "-",
                    str(event.get("attempt_number") or "-"),
                )
            return claim_result
        # The atomic claim fsyncs the ledger before any preflight runs, and it
        # sits inside the end-to-end latency the SLA is judged on.  It was
        # previously invisible in every timing field.
        claim_ms = round((time.perf_counter() - claim_mark) * 1000.0, 1)
        # Only the send path consumes an entry, and most events never reach it
        # (echo confirmations, human prompts, deferrals, unmapped surfaces), so
        # this map has to be self-limiting or it grows for the process lifetime.
        if len(self._claude_claim_ms) >= CLAUDE_CLAIM_COST_LIMIT:
            self._claude_claim_ms.clear()
        self._claude_claim_ms[str(event.get("event_id") or "")] = claim_ms
        surface_id = str(event.get("surface_id") or "")
        target = self._event_target(surface_id)
        runtime = self.runtime.setdefault(surface_id, TargetRuntime()) if surface_id else None
        if not surface_id or target is None:
            self._mark_claude_event(event, runtime, "unmapped", detail="surface is not authorized")
            return
        session_id = str(event.get("session_id") or "")
        if not session_id:
            self._mark_claude_event(event, runtime, "unmapped", detail="missing Claude session_id")
            return
        assert runtime is not None
        # A Claude session is a process identity, not merely a string carried
        # by the Hook.  CMUX can leave a detached/forked Claude process alive
        # after its terminal moved, and that process may retain a stale
        # ``CMUX_SURFACE_ID``.  If the same session is already owned by a
        # different live target, fail closed before it can change this target's
        # latch or send a prompt into the wrong pane.  Surface-local checks
        # alone are insufficient: surface:104 and surface:43 demonstrated the
        # exact cross-surface collision in production.
        with self._runtime_lock:
            foreign_surface = next(
                (
                    other_surface
                    for other_surface, other_runtime in self.runtime.items()
                    if other_surface != surface_id
                    and str(other_runtime.claude_session_id or "") == session_id
                    and str(other_runtime.claude_hook_health or "") != "unverified"
                    and _claude_session_owner_is_live(other_runtime)
                ),
                "",
            )
        if foreign_surface:
            runtime.state = "claude_identity_conflict"
            self._mark_claude_event(
                event,
                runtime,
                "identity_conflict",
                detail=(
                    "Claude session is already bound to another surface: "
                    f"{foreign_surface[:8]}"
                ),
            )
            self.logger.error(
                "surface=%s Claude cross-surface session conflict event=%s owner=%s",
                surface_id[:8], event_id[:8], foreign_surface[:8],
            )
            return
        event_name = str(event["event_name"])
        # A submit transaction owns this episode. Do not let a duplicate Stop
        # event or the screen fallback enqueue another copy while the first
        # prompt is still sitting in the composer. The watchdog's own prompt
        # confirms the transaction; a real user prompt cancels it and wins.
        if runtime.claude_submit_phase != "none":
            prompt_kind = str(event.get("prompt_kind") or "")
            if event_name == "UserPromptSubmit" and prompt_kind == "human":
                self._clear_claude_submit(runtime, reason="human_prompt_override")
            elif event_name == "UserPromptSubmit":
                # Byte-identical to our prompt.  With a transaction pending this
                # is almost certainly our echo, but confirm by correlation so a
                # human paste during the window is not silently mislabelled.
                attribution = self._attribute_exact_prompt(runtime, event)
                runtime.claude_last_prompt_attribution = attribution
                if attribution == "human_exact_prompt":
                    self._clear_claude_submit(runtime, reason="human_prompt_override")
                    self._apply_human_prompt_reset(
                        surface_id, runtime, event, attribution=attribution,
                    )
                    return
                self._clear_claude_submit(runtime, reason="hook_confirmed")
                self._mark_claude_event(event, runtime, "watchdog_confirmed", detail=attribution)
                return
            elif event_name in {"Stop", "StopFailure"} and bool(event.get("completed")):
                # A real completion report is stronger than a pending submit;
                # latch it and stop all further continuation for this turn.
                self._clear_claude_submit(runtime, reason="completion_reported")
            elif event_name in {"Stop", "StopFailure"}:
                self._mark_claude_event(
                    event,
                    runtime,
                    "submit_duplicate_suppressed",
                    detail="Claude submit transaction already pending",
                )
                return
        event_pid = int(event.get("agent_pid") or 0)
        synthetic_fallback = bool(event.get("synthetic_fallback"))
        if (
            not synthetic_fallback
            and event_name in {"Stop", "StopFailure"}
            and runtime.claude_fallback_sent_at > 0
            and float(event.get("created_at") or 0.0) <= runtime.claude_fallback_sent_at
        ):
            self._mark_claude_event(
                event,
                runtime,
                "late_after_fallback",
                detail="screen fallback already rescued this stopped output",
            )
            return

        # CMUX_SURFACE_ID is inherited by subprocesses. A Hook may therefore
        # arrive from the Claude child that owns the bg-pty session while cmux
        # reports the long-lived launcher as the surface's root/foreground
        # PID. Bind by exact surface process-tree membership first, then keep
        # the session-id check below as the guard against a nested session.
        # Use the PID already observed for this process generation when
        # possible; refresh ``top`` only when it differs or is not known.
        expected_pid = int(runtime.claude_process_pid or 0)
        process_kind = "claude" if expected_pid > 0 and expected_pid == event_pid else "unknown"
        surface_agent_pids: set[int] = set()
        event_pid_is_surface_claude = False
        if event_pid > 0 and expected_pid != event_pid:
            try:
                labels = classify_surface_processes(client.top(str(target.get("workspace_id") or "")))
                process = surface_process_label(labels, target)
            except CmuxError:
                process = {"agent_kind": "unknown", "agent_pid": 0, "agent_pids": []}
            process_kind = str(process.get("agent_kind") or "unknown")
            surface_agent_pids = {
                int(pid)
                for pid in process.get("agent_pids", [])
                if str(pid).isdigit() and int(pid) > 0
            }
            root_pid = int(process.get("agent_pid") or 0)
            event_pid_is_surface_claude = (
                process_kind == "claude" and event_pid in surface_agent_pids
            )
            if event_pid_is_surface_claude:
                # Keep the root PID in TargetRuntime so process-generation and
                # legacy-settings inspection remain stable. The Hook PID is a
                # valid descendant, not a replacement root identity.
                process_kind = "claude" if event_pid == root_pid else "claude_descendant"
                if root_pid > 0:
                    expected_pid = root_pid
            else:
                expected_pid = int(process.get("agent_pid") or 0)
        if (
            event_pid > 0
            and expected_pid > 0
            and event_pid != expected_pid
            and not event_pid_is_surface_claude
        ):
            self.claude_event_ledger.mark(
                event,
                "nested_process_ignored",
                detail="event PID is not a Claude PID in this surface process tree",
            )
            self.logger.info(
                "surface=%s Claude nested hook ignored event=%s type=%s",
                surface_id[:8],
                event_id[:8],
                event_name,
            )
            return
        if event_name == "SessionStart" and not (
            event_pid > 0
            and (
                (expected_pid == event_pid and process_kind == "claude")
                or (process_kind == "claude_descendant" and not runtime.claude_session_id)
            )
        ):
            self.claude_event_ledger.mark(
                event,
                "process_unverified",
                detail="SessionStart could not be bound to the surface root Claude PID",
            )
            self.logger.info(
                "surface=%s Claude SessionStart unverified event=%s",
                surface_id[:8],
                event_id[:8],
            )
            return
        if event_pid > 0 and process_kind not in {"claude", "claude_descendant", "unknown"}:
            self.claude_event_ledger.mark(
                event,
                "process_conflict",
                detail="surface root process is not Claude",
            )
            return

        event_generation = str(event.get("process_generation") or "")
        if (
            not synthetic_fallback
            and event_generation
            and runtime.claude_process_generation
            and event_generation != runtime.claude_process_generation
        ):
            self.claude_event_ledger.mark(
                event,
                "stale_generation",
                detail="Hook event generation does not match the live Claude process",
            )
            self.logger.warning(
                "surface=%s stale Claude Hook generation event=%s event_generation=%s live_generation=%s",
                surface_id[:8], event_id[:8], event_generation[:8],
                str(runtime.claude_process_generation)[:8],
            )
            return

        if not synthetic_fallback:
            runtime.claude_hook_health = "healthy"
            runtime.claude_hook_unverified_since = 0.0
            runtime.claude_hook_process_generation = runtime.claude_process_generation or event_generation or None
        if event_pid > 0 and process_kind == "claude":
            runtime.claude_process_pid = event_pid
        if event_name == "SessionStart":
            runtime.claude_session_id = session_id
            runtime.claude_generation_id = event_id
            runtime.claude_hook_process_generation = runtime.claude_process_generation or event_generation or None
            runtime.state = "claude_hook_waiting"
            self._mark_claude_event(event, runtime, "session_started")
            self.logger.info(
                "surface=%s state=claude_hook_healthy source=session_start",
                surface_id[:8],
            )
            return
        previous_session_id = runtime.claude_session_id
        if (
            event_name != "UserPromptSubmit"
            and previous_session_id
            and previous_session_id != session_id
        ):
            if event_pid > 0 and expected_pid == event_pid and process_kind == "claude":
                # A PID-bound root event is stronger evidence than an old
                # persisted session string. This also repairs state written by
                # the pre-PID SessionStart implementation, which could accept
                # a nested Claude session inherited through CMUX_SURFACE_ID.
                self.logger.warning(
                    "surface=%s Claude root session rebound event=%s",
                    surface_id[:8],
                    event_id[:8],
                )
            else:
                runtime.state = "claude_identity_conflict"
                self._mark_claude_event(
                    event,
                    runtime,
                    "identity_conflict",
                    detail="Stop session_id does not match the current surface session",
                )
                self.logger.error(
                    "surface=%s Claude hook identity conflict event=%s",
                    surface_id[:8],
                    event_id[:8],
                )
                return
        if not synthetic_fallback and event_name in {"Stop", "StopFailure"}:
            # A real lifecycle event is fresh evidence for this process.  It
            # supersedes any synthetic gap retry budget from an older missing
            # Hook and makes the next stopped turn eligible again.
            self._clear_claude_fallback_episode(runtime)
        runtime.claude_session_id = session_id
        if not synthetic_fallback:
            runtime.claude_last_hook_at = float(event.get("created_at") or time.time())

        if event_name == "UserPromptSubmit":
            self._clear_claude_fallback_episode(runtime)
            if event.get("prompt_kind") == "human":
                # Different text from our watchdog sentence: no exact-prompt
                # correlation was run, so there is no verdict to report but
                # "a human typed this".
                self._apply_human_prompt_reset(
                    surface_id, runtime, event, attribution="human_prompt",
                )
                return
            # Text says "watchdog", but with no submit transaction pending this
            # is the branch that stranded surface:36 on 2026-08-25: the user
            # pasted the watchdog sentence verbatim after a completion latch, so
            # the latch was kept, send_count stayed high, and every later Stop
            # hit ``suppressed_completed``.  Only correlation can tell the two
            # apart, and an uncorrelated exact prompt is a human.
            attribution = self._attribute_exact_prompt(runtime, event)
            runtime.claude_last_prompt_attribution = attribution
            if attribution == "human_exact_prompt":
                self._apply_human_prompt_reset(
                    surface_id, runtime, event, attribution=attribution,
                )
                return
            self._mark_claude_event(event, runtime, "watchdog_prompt", detail=attribution)
            return

        if bool(event.get("completed")):
            self._clear_claude_fallback_episode(runtime)
            runtime.claude_completed_latched = True
            runtime.claude_completion_event_id = event_id
            runtime.claude_completed_at = time.time()
            # A real completion report outranks any parked Stop: that Stop
            # described output this report has now closed out.
            self._clear_claude_deferred(runtime, reason="completion_reported")
            runtime.error_type = None
            self._reset_claude_repeat_warning(runtime)
            runtime.state = "claude_completed"
            self._mark_claude_event(event, runtime, "completed")
            self.logger.info("surface=%s state=claude_completed source=hook", surface_id[:8])
            return

        if event_name == "Stop" and bool(event.get("stop_hook_active")):
            # ``stop_hook_active`` only says Claude's own Stop hook is running
            # recursively.  It proves neither completion nor that continuation is
            # unnecessary, yet it used to be a terminal drop -- the second half of
            # the surface:36 failure on 2026-08-25, where two such Stops (14:07:55
            # and 14:08:29) followed a Working-cancelled Stop and the episode was
            # left with nothing to retry for sixteen minutes.
            #
            # Park it instead.  Sending *now* would be unsafe (we would be
            # answering a hook that is still executing), so the deferred slot's
            # own guards decide later: a stopped viewport, no send since the
            # deferral, no completion latch, no pending transaction.
            self._defer_claude_event(surface_id, runtime, event, "active_stop_hook")
            return
        if runtime.claude_completed_latched:
            runtime.state = "claude_completed"
            self._mark_claude_event(event, runtime, "suppressed_completed")
            return
        runtime.error_type = str(event.get("error_kind") or "claude_stopped")
        runtime.state = "claude_event_pending"
        if target.get("paused", False) or not target.get("enabled", True):
            self._mark_claude_event(event, runtime, "ignored_paused")
            return
        if not bool(self.config.get("claude_enabled", False)):
            runtime.state = "claude_hook_waiting"
            self._mark_claude_event(event, runtime, "observed_disabled")
            return
        if self.config.get("mode") != "armed" or self.config.get("global_paused", False):
            runtime.state = "claude_hook_waiting"
            self._mark_claude_event(event, runtime, "dry_run")
            return
        sent, detail = self._send_claude_event(event, target, runtime, client)
        if not sent:
            if detail.startswith("process is "):
                runtime.state = "claude_identity_conflict"
            elif detail.startswith("context:"):
                # _apply_claude_context_guard already recorded the precise
                # context state. Keep it instead of mislabelling the event as
                # an incompatible viewport.
                pass
            elif "composer" in detail or "changed" in detail or "submit" in detail or "visible" in detail:
                runtime.state = "claude_input_guard"
            else:
                runtime.state = "incompatible"
            # A transient block is not an answer about whether this turn needs
            # continuing -- it only says "not at this instant".  Measured on
            # 2026-08-25: 354/355 cancels were transient, and while most were
            # rescued by a later Stop (p50 20.5s), the tail was not (p90 1734s;
            # worst 6764s, three never).  Park the event in one bounded slot so
            # the next safe frame can finish it.
            if self._cancel_is_transient(detail):
                self._defer_claude_event(surface_id, runtime, event, detail)
                return
            self._mark_claude_event(event, runtime, "cancelled", detail=detail)
            self.logger.info(
                "surface=%s Claude hook send cancelled event=%s reason=%s",
                surface_id[:8],
                event_id[:8],
                detail,
            )

    def _refresh_dynamic_targets(self, client: CmuxClient, *, force: bool = False) -> None:
        now = time.monotonic()
        interval = float(self.config.get("workspace_discovery_interval_sec", 5))
        if not force and now - self._last_workspace_discovery_at < interval:
            return
        self._last_workspace_discovery_at = now
        try:
            discovered = discover_rule_targets(client, self.config)
        except CmuxError as exc:
            # Keep the last known set, but do not add or rebind anything from a
            # partial/failed discovery cycle.
            self.logger.error("workspace discovery failed; keeping previous targets: %s", exc)
            return
        new_targets = {str(target["surface_id"]): target for target in discovered}
        with self._targets_lock:
            old_ids = set(self.dynamic_targets)
        new_ids = set(new_targets)
        for surface_id in sorted(new_ids - old_ids):
            target = new_targets[surface_id]
            self.logger.info(
                "surface=%s discovered workspace=%s ref=%s",
                surface_id[:8],
                str(target.get("workspace_id", ""))[:8],
                target.get("ref", ""),
            )
        explicit_ids = {str(target.get("surface_id")) for target in self.config.get("targets", [])}
        for surface_id in sorted(old_ids - new_ids):
            self.logger.info("surface=%s no longer an active Codex; removed from dynamic targets", surface_id[:8])
            if surface_id not in explicit_ids:
                with self._runtime_lock:
                    self.runtime.pop(surface_id, None)
        with self._targets_lock:
            self.dynamic_targets = new_targets

    def _refresh_workspace(self, target: dict[str, Any], client: CmuxClient) -> bool:
        """Refresh only the same UUID; never rebind a stale numeric ref."""
        try:
            record = find_surface(client.tree(), str(target["surface_id"]))
        except CmuxError:
            return False
        old_workspace = str(target.get("workspace_id", ""))
        if record["workspace_id"] != old_workspace:
            target["workspace_id"] = record["workspace_id"]
            if target.get("source") != "workspace_rule":
                surface_id = str(target["surface_id"])

                def update_workspace(config: dict[str, Any]) -> None:
                    persisted = target_by_id(config, surface_id)
                    persisted["workspace_id"] = record["workspace_id"]

                try:
                    self._mutate_config(update_workspace)
                except RuntimeError as exc:
                    self.logger.error("surface=%s workspace refresh persistence failed: %s", surface_id[:8], exc)
            self.logger.info("surface=%s workspace UUID refreshed", str(target["surface_id"])[:8])
        return True

    def _notify(self, title: str, body: str) -> None:
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(["/usr/bin/osascript", "-e", script], check=False, timeout=3, capture_output=True)

    def _notify_async(self, title: str, body: str, *, name: str = "ccc-notify") -> None:
        threading.Thread(
            target=self._notify,
            args=(title, body),
            name=name,
            daemon=True,
        ).start()

    def _mark_global_incompatible(self, runtime: TargetRuntime, reason: str) -> None:
        runtime.state = "incompatible"
        runtime.awaiting = False
        if self.config.get("mode") == "armed":
            try:
                self._mutate_config(lambda config: config.update({"mode": "dry-run"}))
            except RuntimeError as exc:
                # Keep this daemon fail-closed even if another config writer
                # holds the lock for too long.
                self.config["mode"] = "dry-run"
                self.logger.error("cannot persist global dry-run: %s", exc)
            self._notify("cmux-codex-continue", "cmux incompatible; switched to dry-run")
        self.logger.error("global incompatible: %s", reason)

    def _mark_target_incompatible(
        self,
        target: dict[str, Any],
        runtime: TargetRuntime,
        reason: str,
    ) -> None:
        surface_id = str(target["surface_id"])
        detail = f"incompatible: {reason}"
        runtime.state = "incompatible"
        runtime.awaiting = False
        runtime.paused_reason = detail
        target["paused"] = True
        source = str(target.get("source") or "explicit")
        recovery = f"cmux-codex-continue resume {surface_id}"
        try:
            if source == "workspace_rule":
                workspace_id = str(target.get("source_workspace_id") or target.get("workspace_id") or "")

                def exclude_dynamic(config: dict[str, Any]) -> None:
                    rule = workspace_rule_by_id(config, workspace_id)
                    excluded = rule.setdefault("excluded_surface_ids", [])
                    if surface_id not in excluded:
                        excluded.append(surface_id)
                    reasons = rule.setdefault("excluded_surface_reasons", {})
                    reasons[surface_id] = {
                        "reason": detail,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }

                self._mutate_config(exclude_dynamic)
                recovery = f"cmux-codex-continue include {surface_id}"
            else:
                def pause_explicit(config: dict[str, Any]) -> None:
                    persisted = target_by_id(config, surface_id)
                    persisted["paused"] = True
                    persisted["paused_reason"] = detail

                self._mutate_config(pause_explicit)
        except RuntimeError as exc:
            self._local_paused_surface_ids.add(surface_id)
            self.logger.error("surface=%s isolation persistence failed: %s", surface_id[:8], exc)
        self.logger.error(
            "surface=%s paused incompatible source=%s reason=%s recover=%s",
            surface_id[:8],
            source,
            reason,
            recovery,
        )
        self._notify(
            "cmux-codex-continue",
            f"surface {surface_id[:8]} paused: incompatible render data",
        )

    def _record_state(self, surface_id: str, runtime: TargetRuntime, state: ScreenState) -> None:
        if runtime.state != state.kind:
            self.logger.info("surface=%s state=%s reason=%s", surface_id[:8], state.kind, state.reason)
        runtime.state = state.kind
        if state.kind != "recoverable_error":
            runtime.awaiting = False

    @staticmethod
    def _runtime_is_claude(runtime: TargetRuntime, state: ScreenState) -> bool:
        return bool(
            state.message_kind == "claude"
            or str(state.error_type or "").startswith("claude")
            or str(runtime.error_type or "").startswith("claude")
            or runtime.state.startswith("claude")
            or runtime.claude_prompt_pending
            or runtime.claude_completed_latched
            or runtime.claude_candidate_key
            or runtime.claude_session_id
            or runtime.claude_hook_status
            or runtime.claude_process_pid
            or runtime.claude_hook_health != "unverified"
        )

    @staticmethod
    def _reset_claude_context_runtime(runtime: TargetRuntime) -> None:
        runtime.claude_context_status = "unknown"
        runtime.claude_context_percent = None
        runtime.claude_context_input_tokens = None
        runtime.claude_context_cache_tokens = None
        runtime.claude_auto_compact_remaining_percent = None
        runtime.claude_context_episode_started_at = 0.0
        runtime.claude_context_limit_first_seen_at = 0.0
        runtime.claude_compaction_started_at = 0.0
        runtime.claude_compaction_last_progress_at = 0.0
        runtime.claude_compaction_last_seen_at = 0.0
        runtime.claude_compaction_current_percent = None
        runtime.claude_compaction_highest_percent = None
        runtime.claude_compaction_restart_count = 0
        runtime.claude_context_composer_kind = "unverified"
        runtime.claude_context_notification_sent = False

    def _apply_claude_context_guard(
        self,
        surface_id: str,
        runtime: TargetRuntime,
        state: ScreenState,
    ) -> ScreenState:
        telemetry = state.claude_context
        if telemetry is None:
            # Parser/version uncertainty is deliberately fail-open.
            runtime.claude_context_status = "unknown"
            if runtime.error_type in {
                "claude_context_waiting",
                "claude_context_compacting",
                "claude_context_stalled",
            }:
                runtime.error_type = state.error_type
            return state
        now = time.time()
        runtime.claude_context_sampled_at = now
        runtime.claude_context_percent = telemetry.percent
        runtime.claude_context_input_tokens = telemetry.input_tokens
        runtime.claude_context_cache_tokens = telemetry.cache_tokens
        runtime.claude_auto_compact_remaining_percent = telemetry.auto_compact_remaining_percent
        runtime.claude_context_composer_kind = telemetry.composer_kind
        hard_limit = bool(
            telemetry.limit_reached
            or (
                telemetry.percent == 100
                and telemetry.auto_compact_remaining_percent == 0
            )
        )
        warning_percent = int(self.config.get(
            "claude_context_warning_percent",
            CLAUDE_CONTEXT_WARNING_PERCENT,
        ))
        status = "unknown" if telemetry.percent is None else "normal"
        if telemetry.percent is not None and telemetry.percent >= warning_percent:
            status = "warning"
        if hard_limit or telemetry.compacting:
            if runtime.claude_context_episode_started_at <= 0:
                runtime.claude_context_episode_started_at = now
            if hard_limit and runtime.claude_context_limit_first_seen_at <= 0:
                runtime.claude_context_limit_first_seen_at = now
        else:
            runtime.claude_context_episode_started_at = 0.0
            runtime.claude_context_limit_first_seen_at = 0.0
            runtime.claude_compaction_started_at = 0.0
            runtime.claude_compaction_last_progress_at = 0.0
            runtime.claude_compaction_last_seen_at = 0.0
            runtime.claude_compaction_current_percent = None
            runtime.claude_compaction_highest_percent = None
            runtime.claude_compaction_restart_count = 0
            runtime.claude_context_notification_sent = False

        stalled = False
        if telemetry.compacting:
            status = "compacting"
            runtime.claude_compaction_last_seen_at = now
            if runtime.claude_compaction_started_at <= 0:
                runtime.claude_compaction_started_at = now
                runtime.claude_compaction_last_progress_at = now
            current = telemetry.compaction_percent
            previous = runtime.claude_compaction_current_percent
            highest = runtime.claude_compaction_highest_percent
            if current is not None:
                if highest is None or current > highest:
                    runtime.claude_compaction_highest_percent = current
                    runtime.claude_compaction_last_progress_at = now
                elif highest is not None and current < highest and (
                    previous is None or current < previous
                ):
                    runtime.claude_compaction_restart_count += 1
                runtime.claude_compaction_current_percent = current
            no_progress = now - runtime.claude_compaction_last_progress_at >= float(self.config.get(
                "claude_context_stall_sec", CLAUDE_CONTEXT_STALL_SEC,
            ))
            absolute = now - runtime.claude_context_episode_started_at >= float(self.config.get(
                "claude_context_absolute_timeout_sec", CLAUDE_CONTEXT_ABSOLUTE_TIMEOUT_SEC,
            ))
            stalled = no_progress or absolute
        elif hard_limit:
            status = "limit_waiting"
            runtime.claude_compaction_current_percent = None
            waiting_since = max(
                runtime.claude_context_limit_first_seen_at,
                runtime.claude_compaction_last_seen_at,
            )
            stalled = now - waiting_since >= float(self.config.get(
                "claude_context_start_grace_sec", CLAUDE_CONTEXT_START_GRACE_SEC,
            ))
            stalled = stalled or (
                now - runtime.claude_context_episode_started_at >= float(self.config.get(
                    "claude_context_absolute_timeout_sec", CLAUDE_CONTEXT_ABSOLUTE_TIMEOUT_SEC,
                ))
            )
        if stalled:
            status = "stalled"
        runtime.claude_context_status = status

        context_errors = {
            "claude_context_waiting",
            "claude_context_compacting",
            "claude_context_stalled",
        }
        if status not in {"limit_waiting", "compacting", "stalled"} and runtime.error_type in context_errors:
            runtime.error_type = state.error_type if state.error_type not in context_errors else None

        enforcement = bool(self.config.get("claude_context_enforcement", False))
        if status == "stalled" and enforcement and not runtime.claude_context_notification_sent:
            runtime.claude_context_notification_sent = True
            self.logger.error(
                "surface=%s Claude context compaction stalled percent=%s compaction=%s restarts=%d",
                surface_id[:8],
                telemetry.percent,
                telemetry.compaction_percent,
                runtime.claude_compaction_restart_count,
            )
            self._notify_async(
                "CCC Claude 压缩失败",
                f"surface {surface_id[:8]} 上下文压缩未恢复；需人工处理，监控保持开启",
                name=f"ccc-context-{surface_id[:8]}",
            )
        if not enforcement or status not in {"limit_waiting", "compacting", "stalled"}:
            return state
        kind = {
            "limit_waiting": "claude_context_waiting",
            "compacting": "claude_context_compacting",
            "stalled": "claude_context_stalled",
        }[status]
        reason = {
            "limit_waiting": "Claude reached context limit; waiting for auto-compact",
            "compacting": "Claude is compacting conversation",
            "stalled": "Claude context compaction failed; human intervention required",
        }[status]
        runtime.state = kind
        runtime.error_type = kind
        return ScreenState(
            kind,
            error_type=kind,
            fingerprint=state.fingerprint,
            screen_signature=state.screen_signature,
            reason=reason,
            message_kind="claude",
            content_fingerprint=state.content_fingerprint,
            watchdog_echo=state.watchdog_echo,
            claude_context=telemetry,
        )

    def _apply_claude_runtime_guards(
        self,
        surface_id: str,
        runtime: TargetRuntime,
        state: ScreenState,
    ) -> ScreenState:
        """Preserve Hook-owned Claude state without deriving task boundaries.

        Working, menus and viewport fingerprints are observations, not lifecycle
        events.  In particular they must never clear a completion latch or
        unlock another send.  Only ``UserPromptSubmit`` and ``Stop`` events do
        that in ``_handle_claude_event``.
        """

        if state.message_kind == "codex" or not self._runtime_is_claude(runtime, state):
            return state
        # Migrate old state in memory.  The fields remain readable so existing
        # state.json files load, but no Hook decision depends on them.
        runtime.claude_prompt_pending = False
        runtime.claude_prompt_kind = None
        runtime.claude_prompt_fingerprint = None
        runtime.claude_working_polls = 0
        runtime.claude_candidate_key = None
        runtime.claude_candidate_since = 0.0
        runtime.claude_candidate_focused = False
        if runtime.claude_completed_latched:
            return ScreenState(
                "claude_completed",
                fingerprint=runtime.claude_completion_event_id,
                screen_signature=state.screen_signature,
                reason="Claude completion remains latched until a real user prompt",
                message_kind="claude",
                content_fingerprint=state.content_fingerprint,
                claude_context=getattr(state, "claude_context", None),
            )
        if runtime.claude_hook_health == "legacy_override":
            return ScreenState(
                "claude_hook_legacy",
                screen_signature=state.screen_signature,
                reason="legacy Claude process inline settings omit the CCC Hook; restart/resume once",
                message_kind="claude",
                content_fingerprint=state.content_fingerprint,
                claude_context=getattr(state, "claude_context", None),
            )
        if runtime.claude_hook_health == "missing":
            return ScreenState(
                "claude_hook_missing",
                screen_signature=state.screen_signature,
                reason="current Claude process has not emitted a CCC lifecycle Hook",
                message_kind="claude",
                content_fingerprint=state.content_fingerprint,
                claude_context=getattr(state, "claude_context", None),
            )
        if runtime.claude_hook_health == "unverified" and state.kind in {
            "claude_hook_waiting",
            "incompatible",
        }:
            return ScreenState(
                "claude_hook_unverified",
                screen_signature=state.screen_signature,
                reason="waiting for SessionStart or another Claude lifecycle Hook",
                message_kind="claude",
                content_fingerprint=state.content_fingerprint,
                claude_context=getattr(state, "claude_context", None),
            )
        if state.kind == "claude_hook_waiting" and runtime.claude_last_hook_at <= 0:
            return ScreenState(
                "claude_hook_missing",
                fingerprint=state.fingerprint,
                screen_signature=state.screen_signature,
                reason="Claude UI is idle but no lifecycle Hook has been observed",
                message_kind="claude",
                content_fingerprint=state.content_fingerprint,
                claude_context=getattr(state, "claude_context", None),
            )
        return state

    @staticmethod
    def _reset_claude_fallback_candidate(runtime: TargetRuntime) -> None:
        runtime.claude_fallback_candidate_fingerprint = None
        runtime.claude_fallback_candidate_polls = 0
        runtime.claude_fallback_candidate_generation = None
        runtime.claude_fallback_attempt_token = None

    @staticmethod
    def _clear_claude_fallback_episode(runtime: TargetRuntime) -> None:
        """End one synthetic lifecycle episode without touching Hook identity."""

        WatchDaemon._reset_claude_fallback_candidate(runtime)
        runtime.claude_fallback_episode_id = None
        runtime.claude_fallback_episode_started_at = 0.0
        runtime.claude_fallback_episode_generation = None
        runtime.claude_fallback_episode_session_id = None
        runtime.claude_fallback_last_fingerprint = None
        runtime.claude_fallback_last_event_id = None
        runtime.claude_fallback_sent_at = 0.0
        runtime.claude_fallback_retry_count = 0
        runtime.claude_fallback_retry_exhausted = False

    def _maybe_send_claude_hook_gap_fallback(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        state: ScreenState,
        observation: Mapping[str, Any] | None,
        client: CmuxClient,
    ) -> bool:
        """Rescue a proven stopped Claude frame when its Stop Hook went missing.

        This is intentionally narrower than the old idle heartbeat: exact root
        Claude generation, a prior prompt Hook baseline, global CCC hooks
        installed, an unfocused empty composer, and two identical frames are all
        mandatory. One stable lifecycle episode owns unique per-attempt events;
        content fingerprints are observations and cannot reset its retry budget.
        """

        if not bool(self.config.get("claude_hook_gap_fallback_enabled", True)):
            self._reset_claude_fallback_candidate(runtime)
            return False
        if runtime.claude_submit_phase != "none":
            # The explicit submit transaction owns this stop; fallback must
            # never manufacture a second event while its Enter is pending.
            self._reset_claude_fallback_candidate(runtime)
            return False
        if not bool(self._claude_hook_config_health.get("healthy")):
            self._reset_claude_fallback_candidate(runtime)
            return False
        if (
            self.config.get("mode") != "armed"
            or self.config.get("global_paused", False)
            or not self.config.get("claude_enabled", False)
            or runtime.claude_completed_latched
            or runtime.claude_hook_health == "legacy_override"
            # A healthy Claude session can be classified as the ordinary
            # stopped state even when its lifecycle Hook was missed.  Keep the
            # fallback guards below intact; this only admits that proven
            # terminal frame into the same two-poll rescue path.
            or state.kind not in {
                "claude_hook_waiting",
                "claude_hook_missing",
                "claude_stopped",
            }
            or not state.content_fingerprint
        ):
            self._reset_claude_fallback_candidate(runtime)
            return False
        if not observation or observation.get("agent_kind") != "claude":
            self._reset_claude_fallback_candidate(runtime)
            return False
        generation = str(observation.get("generation") or "")
        pid = int(observation.get("pid") or 0)
        if (
            not generation
            or generation != runtime.claude_process_generation
            or pid <= 0
            or pid != runtime.claude_process_pid
            or not runtime.claude_session_id
            or runtime.claude_last_event_status not in {
                "human_prompt",
                "watchdog_prompt",
                "sent",
                "cancelled",
            }
        ):
            self._reset_claude_fallback_candidate(runtime)
            return False
        try:
            if is_surface_focused(client.tree(), str(target["surface_id"])):
                self._reset_claude_fallback_candidate(runtime)
                return False
        except CmuxError:
            self._reset_claude_fallback_candidate(runtime)
            return False
        fingerprint = str(state.content_fingerprint)
        # A missing Hook leaves us without a lifecycle boundary.  Upstream retry
        # chrome can nevertheless change the visible content fingerprint every
        # poll; treating that as a fresh episode reopens attempt=1 and bypasses
        # both the 120s backoff and the bounded retry budget.  Until a real Hook,
        # human prompt, confirmation, completion, or process generation change
        # arrives, keep the confirmation-timeout episode intact and only use the
        # current fingerprint to make this attempt's ledger ID unique.
        episode_matches = bool(
            runtime.claude_fallback_episode_id
            and runtime.claude_fallback_episode_generation == generation
            and runtime.claude_fallback_episode_session_id == runtime.claude_session_id
        )
        legacy_unconfirmed_send = bool(
            not runtime.claude_fallback_episode_id
            and runtime.claude_fallback_last_fingerprint
            and runtime.claude_fallback_sent_at > 0
            and runtime.claude_submit_last_reason == "confirmation_timeout"
            and runtime.claude_last_hook_at <= runtime.claude_fallback_sent_at
        )
        if legacy_unconfirmed_send:
            # State written before fallback episodes existed can still contain
            # a real, unconfirmed rescue and its retry budget.  Give that state
            # an episode identity in place: clearing it here would reinterpret
            # the timeout as a brand-new first send and reopen the budget.
            runtime.claude_fallback_episode_id = uuid.uuid4().hex
            runtime.claude_fallback_episode_started_at = (
                runtime.claude_fallback_sent_at or time.time()
            )
            runtime.claude_fallback_episode_generation = generation
            runtime.claude_fallback_episode_session_id = runtime.claude_session_id
            episode_matches = True
        elif not episode_matches:
            # A new lifecycle episode gets a new durable identity even when its
            # viewport is byte-identical to an older Stop.  This is the boundary
            # the deterministic fingerprint-based id could not represent.
            self._clear_claude_fallback_episode(runtime)
            runtime.claude_fallback_episode_id = uuid.uuid4().hex
            runtime.claude_fallback_episode_started_at = time.time()
            runtime.claude_fallback_episode_generation = generation
            runtime.claude_fallback_episode_session_id = runtime.claude_session_id
            episode_matches = True
        retry_episode = bool(
            episode_matches
            and runtime.claude_fallback_last_fingerprint
            and runtime.claude_fallback_sent_at > 0
            and runtime.claude_submit_last_reason == "confirmation_timeout"
            and runtime.claude_last_hook_at <= runtime.claude_fallback_sent_at
        )
        if retry_episode:
            retry_after = float(self.config.get(
                "claude_hook_gap_retry_after_sec", CLAUDE_HOOK_GAP_RETRY_AFTER_SEC,
            ))
            max_retries = int(self.config.get(
                "claude_hook_gap_max_retries", CLAUDE_HOOK_GAP_MAX_RETRIES,
            ))
            retry_due = (
                runtime.claude_submit_last_reason == "confirmation_timeout"
                and runtime.claude_fallback_sent_at > 0
                and time.time() - runtime.claude_fallback_sent_at >= retry_after
                and runtime.claude_last_hook_at <= runtime.claude_fallback_sent_at
            )
            if not retry_due:
                self._reset_claude_fallback_candidate(runtime)
                return False
            if runtime.claude_fallback_retry_count >= max_retries:
                first_notice = not runtime.claude_fallback_retry_exhausted
                runtime.claude_fallback_retry_exhausted = True
                runtime.state = "claude_hook_gap_exhausted"
                runtime.error_type = "claude_hook_gap_exhausted"
                self._reset_claude_fallback_candidate(runtime)
                if first_notice:
                    self.logger.error(
                        "surface=%s Claude hook-gap retry exhausted retries=%d "
                        "continuing_to_monitor=true",
                        str(target["surface_id"])[:8],
                        runtime.claude_fallback_retry_count,
                    )
                return True
            # Treat the old synthetic rescue as covered only after a real Hook
            # arrives.  Until then require the normal two-poll confirmation
            # again, but keep the fingerprint so this remains a bounded retry
            # rather than being mistaken for a fresh first attempt.
            self.logger.warning(
                "surface=%s Claude hook-gap rescue unconfirmed; retrying after %.0fs "
                "retry=%d/%d",
                str(target["surface_id"])[:8], retry_after,
                runtime.claude_fallback_retry_count + 1,
                max_retries,
            )
        if (
            runtime.claude_fallback_candidate_fingerprint != fingerprint
            or runtime.claude_fallback_candidate_generation != generation
        ):
            runtime.claude_fallback_candidate_fingerprint = fingerprint
            runtime.claude_fallback_candidate_generation = generation
            runtime.claude_fallback_candidate_polls = 1
            runtime.claude_fallback_attempt_token = uuid.uuid4().hex
            runtime.state = "claude_hook_gap_candidate"
            return False
        runtime.claude_fallback_candidate_polls += 1
        required = int(self.config.get(
            "claude_hook_gap_confirm_polls",
            CLAUDE_HOOK_GAP_CONFIRM_POLLS,
        ))
        if runtime.claude_fallback_candidate_polls < required:
            runtime.state = "claude_hook_gap_candidate"
            return False
        attempt_number = runtime.claude_fallback_retry_count + 2 if retry_episode else 1
        episode_id = str(runtime.claude_fallback_episode_id or "")
        attempt_token = str(runtime.claude_fallback_attempt_token or uuid.uuid4().hex)
        event_id = hashlib.sha256(
            f"ccc-hook-gap-v2\0{target['surface_id']}\0{generation}\0{episode_id}\0"
            f"{attempt_number}\0{attempt_token}".encode()
        ).hexdigest()
        event = {
            "version": 1,
            "event_id": event_id,
            "created_at": time.time(),
            "event_name": "StopFailure",
            "session_id": runtime.claude_session_id,
            "surface_id": str(target["surface_id"]),
            "workspace_id": str(target.get("workspace_id") or ""),
            "agent_pid": pid,
            "error_kind": "claude_hook_gap",
            "synthetic_fallback": True,
            "episode_id": episode_id,
            "process_generation": generation,
            "attempt_number": attempt_number,
            "evidence_fingerprint": fingerprint,
            "_inbox_source": "fallback",
            "_inbox_at": time.time(),
        }
        previous_last_send_at = runtime.last_send_at
        claim_result = self._handle_claude_event_locked(event, client)
        if claim_result == CLAUDE_HISTORICAL_ID_COLLISION:
            # Preserve the current candidate and retry exactly once with a fresh
            # nonce.  The old row is immutable evidence from another episode.
            replacement_token = uuid.uuid4().hex
            replacement_id = hashlib.sha256(
                f"ccc-hook-gap-v2\0{target['surface_id']}\0{generation}\0{episode_id}\0"
                f"{attempt_number}\0{replacement_token}".encode()
            ).hexdigest()
            event = {**event, "event_id": replacement_id}
            event_id = replacement_id
            runtime.claude_fallback_attempt_token = replacement_token
            self.logger.warning(
                "surface=%s Claude fallback collision recovered new_event=%s episode=%s",
                str(target["surface_id"])[:8], event_id[:8], episode_id[:8],
            )
            claim_result = self._handle_claude_event_locked(event, client)
        sent = (
            claim_result not in {
                CLAUDE_DUPLICATE_SAME_EVENT,
                CLAUDE_DUPLICATE_SAME_EPISODE,
                CLAUDE_HISTORICAL_ID_COLLISION,
                CLAUDE_TERMINAL_CONFLICT,
            }
            and
            self.claude_event_ledger.status_of(event_id) == "sent"
            and runtime.claude_last_resume_event_id == event_id
            and runtime.last_send_at > previous_last_send_at
        )
        if sent:
            runtime.claude_fallback_last_fingerprint = fingerprint
            runtime.claude_fallback_last_event_id = event_id
            runtime.claude_fallback_sent_at = time.time()
            runtime.claude_fallback_retry_count = (
                runtime.claude_fallback_retry_count + 1 if retry_episode else 0
            )
            runtime.claude_fallback_retry_exhausted = False
            self.logger.warning(
                "surface=%s sent=claude_hook_gap event=%s polls=%d attempt=%d",
                str(target["surface_id"])[:8],
                event_id[:8],
                required,
                attempt_number,
            )
        self._reset_claude_fallback_candidate(runtime)
        return sent

    def _pause_missing_or_error(self, target: Mapping[str, Any], runtime: TargetRuntime, reason: str) -> None:
        surface_id = str(target["surface_id"])
        runtime.state = "missing_or_error"
        runtime.paused_reason = reason
        target["paused"] = True
        if target.get("source") != "workspace_rule":
            def pause_explicit(config: dict[str, Any]) -> None:
                persisted = target_by_id(config, surface_id)
                persisted["paused"] = True
                persisted["paused_reason"] = reason

            try:
                self._mutate_config(pause_explicit)
            except RuntimeError as exc:
                self._local_paused_surface_ids.add(surface_id)
                self.logger.error("surface=%s pause persistence failed: %s", surface_id[:8], exc)
        self.logger.error("surface=%s paused: %s", surface_id[:8], reason)

    def _outgoing_message(self, state: ScreenState) -> str:
        """Select a prompt from the classifier's explicit adapter route."""

        if state.message_kind == "claude":
            return str(self.config.get("claude_message") or CLAUDE_MESSAGE)
        if state.message_kind == "codex":
            return str(self.config.get("message") or MESSAGE)
        raise RuntimeError(f"send-eligible state lacks a message route: {state.kind}")

    @staticmethod
    def _claude_candidate_key(state: ScreenState) -> str:
        return _short_hash("\0".join((
            str(state.error_type or "claude_stopped"),
            str(state.fingerprint or ""),
            str(state.content_fingerprint or ""),
        )))

    def _cancel_claude_candidate(
        self,
        surface_id: str,
        runtime: TargetRuntime,
        reason: str,
    ) -> None:
        if runtime.claude_candidate_key:
            self.logger.info("surface=%s send_cancelled=%s", surface_id[:8], reason)
        runtime.claude_candidate_key = None
        runtime.claude_candidate_since = 0.0
        runtime.claude_candidate_focused = False
        runtime.claude_last_cancel_reason = reason
        runtime.claude_last_cancel_at = time.time()

    def _prepare_claude_send(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        state: ScreenState,
        client: CmuxClient,
        tree: Mapping[str, Any] | None,
        tree_error: str,
    ) -> tuple[ScreenState | None, Mapping[str, Any] | None]:
        """Arm, wait, then re-read the exact Claude surface before sending."""

        surface_id = str(target["surface_id"])
        if tree is None:
            self._cancel_claude_candidate(surface_id, runtime, "tree_unavailable")
            self._record_state(
                surface_id,
                runtime,
                ScreenState(
                    "send_guard_unavailable",
                    reason=tree_error or "tree snapshot unavailable",
                    message_kind="claude",
                ),
            )
            return None, tree

        now = time.time()
        focused = is_surface_focused(tree, surface_id)
        key = self._claude_candidate_key(state)
        if (
            runtime.claude_candidate_key != key
            or runtime.claude_candidate_focused != focused
            or runtime.claude_candidate_since <= 0
        ):
            if runtime.claude_candidate_key:
                reason = "focus_changed" if runtime.claude_candidate_focused != focused else "stop_changed"
                self._cancel_claude_candidate(surface_id, runtime, reason)
            runtime.claude_candidate_key = key
            runtime.claude_candidate_since = now
            runtime.claude_candidate_focused = focused
            runtime.error_type = state.error_type or "claude_stopped"
            runtime.state = "claude_input_guard"
            grace = float(self.config.get(
                "claude_focused_input_grace_sec" if focused else "claude_background_input_grace_sec",
                CLAUDE_FOCUSED_INPUT_GRACE_SEC if focused else CLAUDE_BACKGROUND_INPUT_GRACE_SEC,
            ))
            self.logger.info(
                "surface=%s candidate=claude_stop focused=%s grace=%.1fs",
                surface_id[:8],
                str(focused).lower(),
                grace,
            )
            # A zero grace is useful for deterministic unit tests and remains
            # an explicit configuration choice. Production defaults are 1s
            # for background panes and 3s for the focused pane.
            if grace > 0:
                return None, tree

        grace = max(0.0, float(self.config.get(
            "claude_focused_input_grace_sec" if focused else "claude_background_input_grace_sec",
            CLAUDE_FOCUSED_INPUT_GRACE_SEC if focused else CLAUDE_BACKGROUND_INPUT_GRACE_SEC,
        )))
        if now - runtime.claude_candidate_since < grace:
            runtime.state = "claude_input_guard"
            return None, tree

        try:
            fresh_tree = client.tree()
            fresh_focused = is_surface_focused(fresh_tree, surface_id)
            screen_text = client.read_screen(str(target["workspace_id"]), surface_id)
            final_state = self._classify_target_screen(target, screen_text, client)
            final_state = self._apply_claude_runtime_guards(surface_id, runtime, final_state)
            final_state = self._apply_claude_context_guard(surface_id, runtime, final_state)
        except GlobalIncompatibleError:
            raise
        except (CmuxError, IncompatibleError) as exc:
            self._cancel_claude_candidate(surface_id, runtime, "preflight_unavailable")
            self._record_state(
                surface_id,
                runtime,
                ScreenState("incompatible", reason=f"Claude final preflight failed: {exc}", message_kind="claude"),
            )
            return None, tree

        if fresh_focused != focused:
            self._cancel_claude_candidate(surface_id, runtime, "focus_changed")
            self._record_state(surface_id, runtime, final_state)
            return None, fresh_tree
        if final_state.kind != "claude_stopped" or final_state.message_kind != "claude":
            reason = {
                "composer_busy": "user_input",
                "working": "working",
                "menu": "menu",
                "claude_completed": "completed",
            }.get(final_state.kind, "state_changed")
            self._cancel_claude_candidate(surface_id, runtime, reason)
            self._record_state(surface_id, runtime, final_state)
            return None, fresh_tree
        if self._claude_candidate_key(final_state) != key:
            self._cancel_claude_candidate(surface_id, runtime, "stop_changed")
            self._record_state(surface_id, runtime, final_state)
            return None, fresh_tree

        runtime.claude_candidate_key = None
        runtime.claude_candidate_since = 0.0
        runtime.claude_candidate_focused = False
        return final_state, fresh_tree

    def _handle_state(
        self,
        target: Mapping[str, Any],
        runtime: TargetRuntime,
        state: ScreenState,
        client: CmuxClient,
        *,
        send_guard_tree: Mapping[str, Any] | None,
        send_guard_error: str = "",
    ) -> None:
        surface_id = str(target["surface_id"])
        previous_state = runtime.state
        if state.kind not in SEND_ELIGIBLE_STATES:
            self._record_state(surface_id, runtime, state)
            return
        if state.message_kind == "claude":
            state, send_guard_tree = self._prepare_claude_send(
                target,
                runtime,
                state,
                client,
                send_guard_tree,
                send_guard_error,
            )
            if state is None:
                return
        now = time.time()
        send_class = "claude_stop" if state.kind == "claude_stopped" else "error"
        previous_class = "claude_stop" if runtime.error_type == "claude_stopped" else "error"
        continuity = EPISODE_CONTINUITY_STATES
        if (
            state.message_kind == "claude"
            or
            runtime.episode_id is None
            or previous_state not in continuity
            or send_class != previous_class
        ):
            runtime.episode_id = _short_hash(f"{surface_id}:{time.time_ns()}")
            runtime.episode_started_at = now
            runtime.send_count = 0
            runtime.error_type = state.error_type or ("claude_stopped" if send_class == "claude_stop" else None)
            runtime.awaiting = False
            runtime.awaiting_suppressed = 0
            runtime.sent_fingerprint = None
            runtime.sent_screen_signature = None
        else:
            # Keep episode_id; always store the latest trigger so the TUI
            # error column is not stuck on the first type of this streak.
            runtime.error_type = state.error_type or runtime.error_type
            if runtime.episode_started_at <= 0:
                runtime.episode_started_at = now
        runtime.state = state.kind
        if previous_state != state.kind:
            self.logger.info("surface=%s state=%s reason=%s", surface_id[:8], state.kind, state.reason)

        if state.message_kind == "claude":
            # Claude stop events are single-shot.  The runtime guard converts
            # every unchanged subsequent frame to claude_pending_input, so no
            # delay-based repeat path is reachable for Claude.
            repeat_delay = 0.0
            first_send_immediate = True
        else:
            interval = max(0.0, float(self.config.get("send_interval_sec", 1)))
            configured_repeat = self.config.get("repeat_send_delay_sec", REPEAT_SEND_DELAY_SEC)
            repeat_delay = max(
                interval,
                max(0.0, float(configured_repeat)),
            )
            first_send_immediate = bool(self.config.get("first_send_immediate", True))
        if runtime.awaiting:
            if state.screen_signature != runtime.sent_screen_signature:
                runtime.awaiting = False
                runtime.awaiting_suppressed = 0
            elif now - runtime.last_send_at < repeat_delay:
                # The send interval itself is the unchanged-frame guard in
                # normal 1-second production operation.  Do not add a second
                # whole poll of latency once that interval has elapsed.
                return
            elif repeat_delay <= 0 and runtime.awaiting_suppressed < int(self.config.get("same_frame_guard_polls", 1)):
                runtime.awaiting_suppressed += 1
                self.logger.info("surface=%s suppressing unchanged post-send frame", surface_id[:8])
                return
            else:
                runtime.awaiting = False

        # First send of an episode is immediate by default.  Repeats wait
        # ``repeat_delay``.  Working / queued / superseded stay in this
        # episode, so the delay still applies after a successful nudge.
        if runtime.send_count == 0:
            # The product requirement is that a freshly stuck surface is rescued
            # immediately; the delay only ever bounds *repeats*.  Left
            # configurable so the old behaviour is still reachable, but the
            # default matches the requirement.
            if not first_send_immediate:
                started_at = runtime.episode_started_at
                if started_at <= 0 or now - started_at < interval:
                    return
        elif now - runtime.last_send_at < repeat_delay:
            return
        circuit_limit = int(self.config.get("circuit_pause_after", 0) or 0)
        if circuit_limit > 0 and runtime.send_count >= circuit_limit:
            target["paused"] = True
            if target.get("source") != "workspace_rule":
                reason = f"circuit limit reached: {circuit_limit}"

                def pause_explicit(config: dict[str, Any]) -> None:
                    persisted = target_by_id(config, surface_id)
                    persisted["paused"] = True
                    persisted["paused_reason"] = reason

                try:
                    self._mutate_config(pause_explicit)
                except RuntimeError as exc:
                    self._local_paused_surface_ids.add(surface_id)
                    self.logger.error("surface=%s circuit pause persistence failed: %s", surface_id[:8], exc)
            runtime.paused_reason = f"circuit limit reached: {circuit_limit}"
            self.logger.error("surface=%s paused: %s", surface_id[:8], runtime.paused_reason)
            return
        if self.config.get("mode") != "armed" or self.config.get("global_paused", False):
            if now - runtime.last_notice_at >= 30 or previous_state not in SEND_ELIGIBLE_STATES:
                self.logger.info("surface=%s dry-run candidate=%s", surface_id[:8], state.error_type)
                runtime.last_notice_at = now
            return
        manager_surface_id = str(self.config.get("manager_surface_id") or "")
        if manager_surface_id and surface_id == manager_surface_id:
            runtime.state = "blocked_manager"
            runtime.awaiting = False
            self.logger.error("surface=%s send blocked: registered Supervisor manager surface", surface_id[:8])
            return
        if send_guard_tree is None:
            runtime.state = "send_guard_unavailable"
            runtime.awaiting = False
            self.logger.error(
                "surface=%s send blocked: cannot verify Dock scope: %s",
                surface_id[:8],
                send_guard_error or "tree snapshot unavailable",
            )
            return
        if is_dock_surface(send_guard_tree, surface_id):
            runtime.state = "blocked_dock"
            runtime.awaiting = False
            self.logger.error("surface=%s send blocked: Dock surfaces are management-only", surface_id[:8])
            return
        try:
            client.send(str(target["workspace_id"]), surface_id, self._outgoing_message(state))
        except CmuxError as exc:
            self.logger.error("surface=%s send failed: %s", surface_id[:8], exc)
            return
        runtime.last_send_at = now
        runtime.send_count += 1
        runtime.awaiting = True
        runtime.awaiting_suppressed = 0
        runtime.sent_fingerprint = state.fingerprint
        runtime.sent_screen_signature = state.screen_signature
        if state.message_kind == "claude":
            runtime.claude_prompt_pending = True
            runtime.claude_prompt_kind = "claude_stopped"
            runtime.claude_prompt_fingerprint = state.fingerprint
            runtime.claude_prompt_sent_at = now
        runtime.state = "claude_pending_input" if state.message_kind == "claude" else "awaiting_transition"
        if state.message_kind == "claude":
            runtime.awaiting = False
        self.logger.info("surface=%s sent=%s count=%d", surface_id[:8], state.error_type, runtime.send_count)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def load_config_for_cli(path: Path) -> dict[str, Any]:
    return ConfigStore(path).load()


def write_plist(path: Path = DEFAULT_PLIST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    script = str(PROJECT_DIR / "cmux_codex_watch.py")
    plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{WATCHER_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{DEFAULT_PYTHON}</string>
    <string>{script}</string>
    <string>watch</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>WorkingDirectory</key><string>{PROJECT_DIR}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>{Path.home()}</string>
    <key>USER</key><string>{os.environ.get("USER", "")}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>StandardOutPath</key><string>{DEFAULT_LOG_DIR / "launchd.out.log"}</string>
  <key>StandardErrorPath</key><string>{DEFAULT_LOG_DIR / "launchd.err.log"}</string>
</dict></plist>
'''
    path.write_text(plist, encoding="utf-8")


def _run_launchctl(args: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "launchctl failed").strip()
        raise RuntimeError(detail)
    return result


def install_cli_link(path: Path = DEFAULT_CLI_LINK) -> None:
    target = PROJECT_DIR / "bin" / APP_NAME
    for link in (path, DEFAULT_SHORT_CLI_LINK):
        if link.is_symlink() and link.resolve() == target.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise RuntimeError(f"refusing to replace existing CLI path: {link}")
        link.symlink_to(target)


def uninstall_cli_link(path: Path = DEFAULT_CLI_LINK) -> None:
    target = PROJECT_DIR / "bin" / APP_NAME
    for link in (path, DEFAULT_SHORT_CLI_LINK):
        if link.is_symlink() and link.resolve() == target.resolve():
            link.unlink()


def supervisor_command(config_path: Path = DEFAULT_CONFIG_PATH, suggested_surface: str = "") -> str:
    command = [
        DEFAULT_PYTHON,
        str(PROJECT_DIR / "cmux_codex_watch.py"),
        "--config", str(config_path),
        "tui",
    ]
    if suggested_surface:
        command.extend(["--suggest-surface", suggested_surface])
    return shlex.join(command)


def install_dock_control(
    path: Path = DEFAULT_DOCK_CONFIG_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Merge the Supervisor control into a personal Dock seed config."""

    existing = load_json(path, {"controls": []}) if path.exists() else {"controls": []}
    if not isinstance(existing, dict):
        raise RuntimeError(f"Dock config must be a JSON object: {path}")
    controls = existing.get("controls", [])
    if not isinstance(controls, list) or any(not isinstance(item, dict) for item in controls):
        raise RuntimeError(f"Dock config controls must be an array of objects: {path}")
    backup_path: Path | None = None
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        backup_path = path.with_name(f"{path.name}.bak-{stamp}")
        suffix = 1
        while backup_path.exists():
            backup_path = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
            suffix += 1
        shutil.copy2(path, backup_path)
        with contextlib.suppress(OSError):
            os.chmod(backup_path, 0o600)
    control = {
        "id": "cmux-codex-supervisor",
        "title": "Supervisor",
        "type": "terminal",
        "command": supervisor_command(config_path),
        "cwd": str(PROJECT_DIR),
        "height": 420,
    }
    merged_controls = [item for item in controls if item.get("id") != control["id"]]
    merged_controls.append(control)
    updated = dict(existing)
    updated["controls"] = merged_controls
    atomic_write_json(path, updated)
    return {
        "path": str(path),
        "backup": str(backup_path) if backup_path else None,
        "control": control,
        "reload_note": "Dock config seeds new sessions; reload/reopen Dock to apply it to an existing session.",
    }


def enable_cmux_dock_beta() -> None:
    result = subprocess.run(
        ["/usr/bin/defaults", "write", "com.cmuxterm.app", "rightSidebar.beta.dock.enabled", "-bool", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "cannot enable cmux Dock beta").strip()
        raise RuntimeError(detail)


def open_supervisor_dock(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    window_id: str = "",
    suggested_surface: str = "",
    client: CmuxClient | None = None,
) -> dict[str, Any]:
    """Open exactly one Supervisor surface without changing main-area focus."""

    client = client or CmuxClient()
    tree = client.tree()
    identity: Mapping[str, Any] = {}
    with contextlib.suppress(CmuxError):
        identity = client.identify()
    caller = identity.get("caller", {}) if isinstance(identity, Mapping) else {}
    focused = identity.get("focused", {}) if isinstance(identity, Mapping) else {}
    if not window_id:
        window_id = str(caller.get("window_id") or focused.get("window_id") or "")
    if not window_id:
        raise CmuxError("cannot determine cmux window UUID; pass --window")
    # cmux 0.64 can return a Dock UUID before its terminal tab is addressable
    # when the Dock has not been materialized in this window yet.
    client.show_dock(window_id)
    main_ids = {record["surface_id"] for record in main_surface_records(tree)}
    if suggested_surface:
        suggested_surface = find_main_surface(tree, suggested_surface)["surface_id"]
    else:
        suggested_surface = next(
            (
                candidate for candidate in (
                    str(focused.get("surface_id") or ""),
                    str(caller.get("surface_id") or ""),
                )
                if candidate in main_ids
            ),
            "",
        )
    command = supervisor_command(config_path, suggested_surface)
    existing = [
        record for record in dock_surface_records(tree)
        if not record.get("window_id") or record.get("window_id") == window_id
    ]
    configured_manager_id = ""
    with contextlib.suppress(RuntimeError):
        configured_manager_id = str(ConfigStore(config_path).load().get("manager_surface_id") or "")
    supervisor = next(
        (record for record in existing if record.get("surface_id") == configured_manager_id),
        None,
    ) or next(
        (record for record in existing if record.get("title", "").lower().startswith("supervisor")),
        None,
    )
    created = False
    if supervisor:
        surface_id = supervisor["surface_id"]
        try:
            screen = client.read_surface(surface_id)
        except CmuxError:
            screen = ""
        # The Chinese TUI title is shared by old builds.  Require the current
        # column marker too, so a stale pre-polish Supervisor is refreshed.
        if "Supervisor" not in screen and "当前原因" not in screen:
            try:
                client.respawn_surface(window_id, surface_id, command)
            except CmuxError as exc:
                detail = str(exc).lower()
                if "not_found" not in detail and "surface not found" not in detail:
                    raise
                # cmux can retain a stale Dock record after the underlying
                # surface has been destroyed. Recreate only for that exact
                # not-found case; other respawn failures remain fatal.
                surface_id = client.new_dock_surface(window_id)
                created = True
                client.initialize_dock_surface(window_id, surface_id, "Supervisor", command)
    else:
        surface_id = client.new_dock_surface(window_id)
        created = True
        # new-surface starts a shell; address only the returned Dock UUID.
        client.initialize_dock_surface(window_id, surface_id, "Supervisor", command)
    return {
        "window_id": window_id,
        "surface_id": surface_id,
        "created": created,
        "suggested_surface": suggested_surface,
        "command": command,
    }


def launchctl(action: str) -> None:
    label = WATCHER_LABEL
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    if action in {"start", "install"}:
        # launchd opens StandardOutPath/StandardErrorPath itself, before it execs
        # us, so Python's own mkdir in run() is too late to save those two file
        # descriptors.  If the directory is missing at bootstrap time the daemon
        # still runs but its launchd-side stderr -- the only place a crash
        # traceback lands -- is gone for the lifetime of the process.  Creating
        # the directory here is idempotent and costs nothing.
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    if action == "start":
        if not DEFAULT_PLIST_PATH.exists():
            raise RuntimeError(f"LaunchAgent is not installed: {DEFAULT_PLIST_PATH}")
        _run_launchctl(["bootstrap", domain, str(DEFAULT_PLIST_PATH)], check=False)
        _run_launchctl(["kickstart", "-k", service], check=True)
    elif action == "stop":
        _run_launchctl(["bootout", service], check=False)
    elif action == "install":
        write_plist()
        install_cli_link()
        _run_launchctl(["bootout", service], check=False)
        _run_launchctl(["bootstrap", domain, str(DEFAULT_PLIST_PATH)], check=True)
    elif action == "uninstall":
        _run_launchctl(["bootout", service], check=False)
        with contextlib.suppress(FileNotFoundError):
            DEFAULT_PLIST_PATH.unlink()
        uninstall_cli_link()


HOWTO_TEXT = """日常用法（不用改 Python）

打开管理面（在右侧 Dock 格子里敲）：
  ccc
  cmux-codex-continue tui
  cmux-codex-continue dock-open

Dock 会自动列出 cmux 全部主区 workspace / surface，但不会自动登记：
  /  输入 workspace:11、surface:59 或中文标题关键词查找；c 清除查找
  f  切换 全部 / 只看已监控 / 只看未登记
  a  登记当前这一路
  w  授权整个 workspace（该池以后新开的 Codex 也会跟）
  p / r  暂停或恢复这一路
  x  删除单路登记
  u  取消整个 workspace 授权
  A  开始真实发送    S 全局停发    d 全局停发(只观察)
  R  刷新    q  退出（守护器继续跑）

命令行：
  cmux-codex-continue add surface:77 --name task-77
  cmux-codex-continue track-surface surface:77 --allow-non-codex --name ws1-p1-s77
  cmux-codex-continue remove surface:77
  cmux-codex-continue pause surface:77
  cmux-codex-continue resume surface:77
  cmux-codex-continue add-workspace workspace:9 --name anyrouter
  cmux-codex-continue remove-workspace workspace:9
  cmux-codex-continue effective
  cmux-codex-continue status
  cmux-codex-continue hook-audit --json     # 只读，不输出终端正文或进程参数
  cmux-codex-continue context-audit --json  # context/自动压缩状态，只读
  cmux-codex-continue context-observe       # 只采遥测，不阻断续跑
  cmux-codex-continue context-enforce       # 到顶/压缩/卡死时阻断续跑
  cmux-codex-continue stop-all
  cmux-codex-continue arm

每行会标出 Codex / Claude / gh / 其他，那只是提示。
只有你选中并按 a 或 w 确认后，才会写入监控配置；看起来不是 Codex 的行会先警告再放行。
是否真的发送「任务请继续」由守护器按屏幕内容单独判定，登记了也不代表会发。
Claude 显示「已完成」时仍在监控；下一个任务自动恢复判断，不需要手动 resume。
"""


def howto_text() -> str:
    return HOWTO_TEXT.strip() + "\n"


def claude_sla_report(runtime: Mapping[str, Any], sla_sec: float = CLAUDE_HOOK_SLA_SEC) -> dict[str, Any]:
    samples = [
        float(value)
        for value in runtime.get("claude_hook_latency_ms", [])
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    live_count = int(runtime.get("claude_hook_live_send_count") or 0)
    misses = int(runtime.get("claude_hook_sla_miss_count") or 0)
    return {
        "threshold_ms": round(float(sla_sec) * 1000.0, 3),
        "live_send_count": live_count,
        "miss_count": misses,
        "miss_rate": round(misses / live_count, 4) if live_count else 0.0,
        "p50_ms": round(statistics.median(samples), 3) if samples else 0.0,
        "last_ms": round(float(runtime.get("claude_hook_last_latency_ms") or 0.0), 3),
        "max_ms": round(float(runtime.get("claude_hook_max_latency_ms") or 0.0), 3),
        "replay_count": int(runtime.get("claude_hook_replay_count") or 0),
        "replay_max_age_sec": round(float(runtime.get("claude_hook_replay_max_age_sec") or 0.0), 3),
    }


def hook_sla_missed(latency_sec: float, sla_sec: float = CLAUDE_HOOK_SLA_SEC) -> bool:
    """The SLA boundary is inclusive: exactly one second is not a miss."""

    return float(latency_sec) > float(sla_sec)


def audit_claude_surfaces(
    config: Mapping[str, Any],
    state: Mapping[str, Any],
    client: CmuxClient,
    hook_config_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only Claude inventory.  Never logs or returns terminal content."""

    hook_config = dict(hook_config_report or ClaudeHookSettingsManager().inspect())
    hook_config_healthy = bool(hook_config.get("healthy"))
    tree = client.tree()
    records = {record["surface_id"]: record for record in main_surface_records(tree)}
    processes = classify_surface_processes(client.top_all())
    explicit = {
        str(target.get("surface_id") or ""): target
        for target in config.get("targets", [])
        if target.get("surface_id")
    }
    now = time.time()
    rows: list[dict[str, Any]] = []
    live_ids: set[str] = set()
    for surface_id, record in records.items():
        process = surface_process_label(processes, record)
        if process.get("agent_kind") != "claude":
            continue
        live_ids.add(surface_id)
        target = explicit.get(surface_id)
        runtime = state.get(surface_id, {}) if isinstance(state.get(surface_id), Mapping) else {}
        process_pid = int(process.get("agent_pid") or 0)
        inspection = inspect_claude_process(process_pid)
        last_hook_at = float(runtime.get("claude_last_hook_at") or 0.0)
        started_epoch = float(inspection.get("started_epoch") or 0.0)
        if last_hook_at > 0 and (started_epoch <= 0 or last_hook_at >= started_epoch):
            hook_health = "healthy"
        elif inspection.get("legacy_override"):
            hook_health = "legacy_override"
        else:
            hook_health = str(runtime.get("claude_hook_health") or "unverified")
        screen_state = "unreadable"
        live_context: dict[str, Any] = {}
        try:
            payload = client.replay(str(record["workspace_id"]), surface_id)
            classified = classify_claude_grid(
                Grid.from_rpc(payload, surface_id),
                claude_message=str(config.get("claude_message") or CLAUDE_MESSAGE),
            )
            screen_state = classified.kind
            if classified.claude_context is not None:
                live_context = dataclasses.asdict(classified.claude_context)
                # Date every reading.  ``context`` below is the daemon's stored
                # verdict and this one is a fresh replay; they legitimately
                # disagree while context moves, so consumers need to know which
                # is which instead of assuming one timestamp for both.
                live_context["source"] = "fresh_replay"
                live_context["sampled_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                )
                live_context["age_sec"] = 0.0
        except (CmuxError, IncompatibleError) as exc:
            screen_state = f"unreadable:{type(exc).__name__}"
        candidate_since = float(runtime.get("claude_candidate_since") or 0.0)
        effective_hook_health = hook_health if hook_config_healthy else "degraded_config"
        unprotected_since = float(runtime.get("claude_hook_unprotected_since") or 0.0)
        unprotected_sec = (
            round(max(0.0, now - unprotected_since), 1) if unprotected_since > 0 else 0.0
        )
        stored_sampled_at = float(runtime.get("claude_context_sampled_at") or 0.0)
        context_sampled_at = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stored_sampled_at))
            if stored_sampled_at > 0
            else ""
        )
        context_age_sec = (
            round(max(0.0, now - stored_sampled_at), 1) if stored_sampled_at > 0 else None
        )
        unreadable_since = float(runtime.get("claude_unreadable_since") or 0.0)
        unreadable_sec = (
            round(max(0.0, now - unreadable_since), 1) if unreadable_since > 0 else 0.0
        )
        rows.append({
            "workspace_ref": record.get("workspace_ref", ""),
            "pane_ref": record.get("pane_ref", ""),
            "surface_ref": record.get("ref", ""),
            "surface_id": surface_id,
            "short_id": surface_id[:8],
            "focused": is_surface_focused(tree, surface_id),
            "registered": target is not None,
            "paused": bool(target and target.get("paused")),
            "paused_reason": str((target or {}).get("paused_reason") or ""),
            "screen_state": screen_state,
            # How long this viewport has been unparseable.  A bare
            # "incompatible" label hid surface:72 for the full 5.65h of the
            # 2026-08-24 observation; a blind pane still fails open on
            # context, so the duration has to be reportable.
            "unreadable_sec": unreadable_sec,
            "live_context": live_context,
            "runtime_state": str(runtime.get("state") or "unknown"),
            "candidate_age_sec": round(max(0.0, now - candidate_since), 1) if candidate_since else 0.0,
            "last_send_at": float(runtime.get("last_send_at") or 0.0),
            "last_cancel_reason": runtime.get("claude_last_cancel_reason"),
            "process": str(process.get("summary") or "Claude"),
            "process_pid": process_pid,
            "process_started_at": str(inspection.get("started_at") or ""),
            "hook_health": hook_health,
            "effective_hook_health": effective_hook_health,
            # How long this surface has had no trustworthy Hook generation.
            # Measured from max(process_start, daemon_start), never from
            # last_send_at: during the 2026-08-24 observation surface:74 read
            # 19.8h by last_send but only 14.5h of real exposure for the live
            # process.  A bare "missing" label hid that for 5.65 hours.
            "unprotected_sec": unprotected_sec,
            "unprotected_severity": str(runtime.get("claude_hook_unprotected_severity") or ""),
            "last_hook_at": last_hook_at,
            "last_hook_event": str(runtime.get("claude_last_event_status") or ""),
            "legacy_inline_settings": bool(inspection.get("legacy_override")),
            "consecutive_resumes": int(runtime.get("claude_consecutive_resumes") or 0),
            "repeat_warning": bool(runtime.get("claude_repeat_warning")),
            "context": {
                # This is the daemon's stored verdict, not a live reading.  Its
                # age matters: on 2026-08-24 a stored 8% sat beside a live 22%
                # for the same pane.  Both were correct for their own instant.
                "source": "state",
                "sampled_at": context_sampled_at,
                "age_sec": context_age_sec,
                "status": str(runtime.get("claude_context_status") or "unknown"),
                "percent": runtime.get("claude_context_percent"),
                "input_tokens": runtime.get("claude_context_input_tokens"),
                "cache_tokens": runtime.get("claude_context_cache_tokens"),
                "auto_compact_remaining_percent": runtime.get("claude_auto_compact_remaining_percent"),
                "compaction_percent": runtime.get("claude_compaction_current_percent"),
                "compaction_highest_percent": runtime.get("claude_compaction_highest_percent"),
                "compaction_restart_count": int(runtime.get("claude_compaction_restart_count") or 0),
                "composer_kind": str(runtime.get("claude_context_composer_kind") or "unverified"),
            },
            "sla": claude_sla_report(
                runtime,
                float(config.get("claude_hook_sla_sec", CLAUDE_HOOK_SLA_SEC)),
            ),
        })
    stale = [
        {
            "surface_id": surface_id,
            "short_id": surface_id[:8],
            "surface_ref": str(target.get("ref") or ""),
            "paused": bool(target.get("paused")),
            "paused_reason": str(target.get("paused_reason") or ""),
        }
        for surface_id, target in explicit.items()
        if surface_id not in live_ids
        and (
            "claude" in str(target.get("title_snapshot") or "").lower()
            or str((state.get(surface_id) or {}).get("state") or "").startswith("claude")
        )
    ]
    rows.sort(key=lambda row: _ref_number(str(row["surface_ref"])))
    hook_counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("effective_hook_health") or "unverified")
        hook_counts[key] = hook_counts.get(key, 0) + 1
    total_live_sends = sum(int(row["sla"]["live_send_count"]) for row in rows)
    total_misses = sum(int(row["sla"]["miss_count"]) for row in rows)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "claude_enabled": bool(config.get("claude_enabled")),
        "hook_config": hook_config,
        "hook_health_counts": hook_counts,
        "sla_summary": {
            "live_send_count": total_live_sends,
            "miss_count": total_misses,
            "miss_rate": round(total_misses / total_live_sends, 4) if total_live_sends else 0.0,
        },
        "live_count": len(rows),
        "live": rows,
        "stale_registered": stale,
    }


def _audit_display_width(value: str) -> int:
    """Measure terminal cells so CJK audit fields do not shift columns."""

    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in value)


def _audit_clip(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    if width <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + size > width:
            break
        kept.append(char)
        used += size
    return "".join(kept)


def _audit_row(values: tuple[Any, ...], widths: tuple[int, ...]) -> str:
    cells: list[str] = []
    for value, width in zip(values, widths):
        text = _audit_clip(value, width)
        cells.append(text + " " * max(0, width - _audit_display_width(text)))
    return "  ".join(cells)


def print_claude_audit(report: Mapping[str, Any]) -> None:
    hook_config = report.get("hook_config", {})
    sla = report.get("sla_summary", {})
    hook_status = str(hook_config.get("status", "unknown"))
    hook_icon = "✓" if hook_status in {"ok", "healthy", "enabled"} else ("⚠" if hook_status in {"unknown", "unverified"} else "✗")
    print(
        f"{hook_icon} Hook配置: {hook_status} | "
        f"SLA: {sla.get('miss_count', 0)}/{sla.get('live_send_count', 0)} 超时"
    )
    print(
        "workspace/pane/surface       UUID      登记    监控      Hook            无保护   SLA       PID     画面              运行态"
    )
    for row in report.get("live", []):
        location = "/".join((
            str(row.get("workspace_ref") or "?"),
            str(row.get("pane_ref") or "?"),
            str(row.get("surface_ref") or "?"),
        ))
        registered = "是" if row.get("registered") else "否"
        watched = "已暂停" if row.get("paused") else ("监控中" if row.get("registered") else "未登记")
        focused = "是" if row.get("focused") else "否"
        # A bare "missing" hid a ~14.5h exposure for the whole 2026-08-24 window.
        # The duration is the part that makes it actionable, so it gets a column
        # of its own rather than living only in --json.
        unprotected_text = _audit_age_text(row.get("unprotected_sec")) or "-"
        blind_age = _audit_age_text(row.get("unreadable_sec"))
        screen_text = str(row.get("screen_state") or "")
        if blind_age:
            screen_text = f"{screen_text}~{blind_age}"
        print(_audit_row((location, str(row.get('short_id') or ''), registered,
                          watched, str(row.get('effective_hook_health') or ''),
                          unprotected_text,
                          str((row.get('sla') or {}).get('miss_count', 0)) + ' miss',
                          str(row.get('process_pid') or '-'), screen_text,
                          str(row.get('runtime_state') or '')),
                   (28, 8, 4, 8, 15, 8, 9, 7, 16, 12)))
    print(f"Claude 活屏: {report.get('live_count', 0)}")


def _audit_age_text(seconds: Any) -> str:
    """Compact age for human audit output; '' when never sampled."""

    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return ""
    # Zero means "no clock running", not "zero seconds old".  Rendering it as
    # "0s" made a healthy pane read as "unprotected 0s" and a readable viewport
    # as "claude_stopped~0s", both of which invent an exposure that is not there.
    if seconds <= 0:
        return ""
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def print_context_audit(report: Mapping[str, Any]) -> None:
    # Two percentages for one pane is not a bug, it is two sampling instants: on
    # 2026-08-24 a stored 81% sat beside a live 95% and the text output gave no
    # way to tell which was which.  The 存档 column now carries its own age so
    # the gap reads as "six hours old" instead of "the parser disagrees".
    print(
        "workspace/surface      UUID      实时%  存档%      自动压缩余量  压缩进度  runtime状态       composer"
    )
    for row in report.get("live", []):
        live = row.get("live_context", {}) if isinstance(row.get("live_context"), Mapping) else {}
        runtime = row.get("context", {}) if isinstance(row.get("context"), Mapping) else {}
        percent = live.get("percent")
        remaining = live.get("auto_compact_remaining_percent")
        progress = live.get("compaction_percent")
        stored_percent = runtime.get("percent")
        stored_age = _audit_age_text(runtime.get("age_sec"))
        if stored_percent is None:
            stored_text = "?"
        elif stored_age:
            stored_text = f"{stored_percent}%~{stored_age}"
        else:
            stored_text = f"{stored_percent}%"
        print(_audit_row((str(row.get('workspace_ref') or '') + '/' + str(row.get('surface_ref') or ''),
                          str(row.get('short_id') or ''),
                          (str(percent) + '%') if percent is not None else '?',
                          stored_text,
                          (str(remaining) + '%') if remaining is not None else '?',
                          (str(progress) + '%') if progress is not None else '-',
                          str(runtime.get('status') or 'unknown'),
                          str(live.get('composer_kind') or '?')),
                   (22, 9, 6, 10, 13, 9, 17, 16)))
    print(f"Claude 活屏: {report.get('live_count', 0)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="cmux Codex 自动续跑。不带子命令时打印日常用法。")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("watch")
    add = sub.add_parser("add")
    add.add_argument("selector")
    add.add_argument("--name", default="")
    add_here = sub.add_parser("add-here")
    add_here.add_argument("--name", default="")
    track_current = sub.add_parser("track-current")
    track_current.add_argument("--name", default="")
    track_surface = sub.add_parser("track-surface")
    track_surface.add_argument("selector")
    track_surface.add_argument("--name", default="")
    track_surface.add_argument(
        "--allow-non-codex",
        action="store_true",
        help="register a main-area surface that has no live codex process right now",
    )
    add_workspace = sub.add_parser("add-workspace")
    add_workspace.add_argument("selector")
    add_workspace.add_argument("--name", default="")
    track_workspace = sub.add_parser("track-workspace")
    track_workspace.add_argument("selector")
    track_workspace.add_argument("--name", default="")
    remove_workspace = sub.add_parser("remove-workspace")
    remove_workspace.add_argument("workspace")
    untrack_workspace = sub.add_parser("untrack-workspace")
    untrack_workspace.add_argument("workspace")
    discover = sub.add_parser("discover")
    discover.add_argument("workspace")
    exclude = sub.add_parser("exclude")
    exclude.add_argument("surface")
    include = sub.add_parser("include")
    include.add_argument("surface")
    for command in ("remove", "pause", "resume"):
        item = sub.add_parser(command)
        item.add_argument("target")
    untrack_surface = sub.add_parser("untrack-surface")
    untrack_surface.add_argument("surface")
    backfill = sub.add_parser("backfill-refs")
    backfill.add_argument("--apply", action="store_true", help="write the changes; default is a preview")
    audit_claude = sub.add_parser("audit-claude")
    audit_claude.add_argument("--json", action="store_true", help="print structured JSON")
    hook_audit = sub.add_parser("hook-audit")
    hook_audit.add_argument("--json", action="store_true", help="print structured JSON")
    context_audit = sub.add_parser("context-audit")
    context_audit.add_argument("--json", action="store_true", help="print structured JSON")
    hook_doctor = sub.add_parser("hook-doctor")
    hook_doctor.add_argument("--json", action="store_true", help="print structured JSON")
    sub.add_parser("hook-repair")
    tui = sub.add_parser("tui")
    tui.add_argument("--suggest-surface", default="")
    dock_install = sub.add_parser("dock-install")
    dock_install.add_argument("--path", type=Path, default=DEFAULT_DOCK_CONFIG_PATH)
    dock_open = sub.add_parser("dock-open")
    dock_open.add_argument("--window", default="")
    dock_open.add_argument("--surface", default="")
    for command in (
        "list", "effective", "status", "logs", "dry-run", "arm", "stop-all",
        "context-enforce", "context-observe",
        "start", "stop", "install", "uninstall", "howto",
    ):
        sub.add_parser(command)
    return parser


TARGET_POSITION_FIELDS = ("workspace_ref", "pane_ref", "pane_id")


def plan_ref_backfill(
    config: Mapping[str, Any],
    live: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Which explicit targets are missing a persisted position, and what to add.

    Pure and idempotent: a target that already carries a field is left alone, and
    one that is no longer in the tree cannot be recovered at all, so it is
    skipped rather than guessed.
    """
    plan: list[dict[str, Any]] = []
    for target in config.get("targets", []):
        surface_id = str(target.get("surface_id") or "")
        record = live.get(surface_id)
        if not surface_id or record is None:
            continue
        fields = {
            field: str(record.get(field) or "")
            for field in TARGET_POSITION_FIELDS
            if str(record.get(field) or "") and not str(target.get(field) or "")
        }
        if fields:
            plan.append({
                "surface_id": surface_id,
                "ref": str(target.get("ref") or ""),
                "name": str(target.get("name") or ""),
                "fields": fields,
            })
    return plan


def _explicit_target_from_record(record: Mapping[str, str], name: str = "") -> dict[str, Any]:
    target: dict[str, Any] = {
        "surface_id": record["surface_id"],
        "workspace_id": record["workspace_id"],
        "ref": record["ref"],
        "title_snapshot": record["title"],
        "name": name or record["title"] or record["ref"] or record["surface_id"][:8],
        "enabled": True,
        "paused": False,
    }
    # Position is only knowable while the surface exists.  Persisting it means a
    # target that later disappears can still be shown as ws1/p1/s77 instead of
    # ws?/p?/s77.  These fields are display-only; identity stays the UUID.
    for field in TARGET_POSITION_FIELDS:
        value = str(record.get(field) or "")
        if value:
            target[field] = value
    return target


def _workspace_rule_from_record(record: Mapping[str, str], name: str = "") -> dict[str, Any]:
    return {
        "workspace_id": record["workspace_id"],
        "ref": record["ref"],
        "title_snapshot": record["title"],
        "name": name or record["title"] or record["ref"] or record["workspace_id"][:8],
        "agent": "codex",
        "enabled": True,
        "excluded_surface_ids": [],
        "excluded_surface_reasons": {},
    }


def _discover_workspace(client: CmuxClient, tree: Mapping[str, Any], selector: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    workspace = find_workspace(tree, selector)
    surfaces = discover_codex_surfaces(tree, client.top(workspace["workspace_id"]), workspace["workspace_id"])
    return workspace, surfaces


def cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path: Path = args.config
    if not args.command or args.command == "howto":
        # Bare `ccc` / no subcommand inside a cmux surface opens the TUI.
        # Outside cmux, keep printing the short howto so a normal Terminal
        # does not start curses.
        if not args.command and os.environ.get("CMUX_SURFACE_ID"):
            from cmux_supervisor_tui import run_tui

            return run_tui(config_path, "")
        print(howto_text(), end="")
        return 0
    if args.command != "watch":
        configure_logging()
    if args.command == "watch":
        with FileLock(DEFAULT_LOCK_PATH, purpose="daemon lock"):
            return WatchDaemon(config_path=config_path).run()
    if args.command == "tui":
        from cmux_supervisor_tui import run_tui

        return run_tui(config_path, args.suggest_surface)
    if args.command == "dock-install":
        result = install_dock_control(args.path, config_path)
        enable_cmux_dock_beta()
        result["dock_beta_enabled"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "dock-open":
        store = ConfigStore(config_path)
        config = store.load()
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        result = open_supervisor_dock(
            config_path,
            window_id=args.window,
            suggested_surface=args.surface,
            client=client,
        )
        _, _, _ = store.mutate(
            lambda latest: latest.update({"manager_surface_id": str(result["surface_id"])})
        )
        result["manager_surface_id"] = str(result["surface_id"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    store = ConfigStore(config_path)
    config = store.load()
    if args.command in {"hook-doctor", "hook-repair"}:
        manager = ClaudeHookSettingsManager()
        report = manager.ensure(
            repair=args.command == "hook-repair",
            automatic=False,
            max_repairs_per_hour=int(config.get(
                "claude_hook_auto_repair_max_per_hour",
                CLAUDE_HOOK_AUTO_REPAIR_MAX_PER_HOUR,
            )),
            drift_warning_per_day=int(config.get(
                "claude_hook_drift_warning_per_day",
                CLAUDE_HOOK_DRIFT_WARNING_PER_DAY,
            )),
            backup_keep=int(config.get(
                "claude_hook_settings_backup_keep",
                CLAUDE_HOOK_SETTINGS_BACKUP_KEEP,
            )),
        )
        if args.command == "hook-doctor" and not args.json:
            print(
                f"Claude Hook配置: {report.get('status')} | "
                + " | ".join(
                    f"{name}={count}"
                    for name, count in report.get("event_counts", {}).items()
                )
            )
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"audit-claude", "hook-audit", "context-audit"}:
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        report = audit_claude_surfaces(
            config,
            load_json(DEFAULT_STATE_PATH, {}),
            client,
            ClaudeHookSettingsManager().inspect(),
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "context-audit":
            print_context_audit(report)
        else:
            print_claude_audit(report)
        return 0
    if args.command in {"add", "add-here", "track-current", "track-surface"}:
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        is_current = args.command in {"add-here", "track-current"}
        selector = os.environ.get("CMUX_SURFACE_ID", "") if is_current else args.selector
        if not selector:
            raise RuntimeError("CMUX_SURFACE_ID is unavailable; run this command inside the target cmux surface")
        tree = client.tree()
        # --allow-non-codex only waives the live-process check.  Surface
        # resolution still goes through find_main_surface, so the Dock stays
        # excluded at both the pane and the surface level.
        allow_non_codex = bool(getattr(args, "allow_non_codex", False))
        require_live_codex = (is_current or args.command == "track-surface") and not allow_non_codex
        if require_live_codex:
            record = find_main_surface(tree, selector)
            active_codex = {
                item["surface_id"]
                for item in discover_codex_surfaces(tree, client.top(record["workspace_id"]), record["workspace_id"])
            }
            if record["surface_id"] not in active_codex:
                raise RuntimeError(f"{args.command} requires a main-area surface with a live codex process")
        elif args.command in {"add-here", "track-current", "track-surface"}:
            record = find_main_surface(tree, selector)
        else:
            record = find_surface(tree, selector)
        new_target = _explicit_target_from_record(record, args.name)

        def add_target(latest: dict[str, Any]) -> dict[str, Any]:
            if any(item.get("surface_id") == record["surface_id"] for item in latest["targets"]):
                raise RuntimeError(f"surface already registered: {record['surface_id']}")
            latest["targets"].append(new_target)
            return new_target

        _, added, _ = store.mutate(add_target)
        print(json.dumps(added, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"add-workspace", "track-workspace"}:
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        tree = client.tree()
        workspace, surfaces = _discover_workspace(client, tree, args.selector)
        new_rule = _workspace_rule_from_record(workspace, args.name)

        def add_rule(latest: dict[str, Any]) -> dict[str, Any]:
            if any(rule.get("workspace_id") == workspace["workspace_id"] for rule in latest["workspace_rules"]):
                raise RuntimeError(f"workspace already registered: {workspace['workspace_id']}")
            latest["workspace_rules"].append(new_rule)
            return new_rule

        _, rule, _ = store.mutate(add_rule)
        print(json.dumps({"rule": rule, "active_codex_surfaces": surfaces}, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"remove-workspace", "untrack-workspace"}:
        def remove_rule(latest: dict[str, Any]) -> dict[str, Any]:
            rule = workspace_rule_by_id(latest, args.workspace)
            latest["workspace_rules"].remove(rule)
            return rule

        _, removed, _ = store.mutate(remove_rule)
        print(json.dumps({"removed_workspace_rule": removed}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "discover":
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        workspace, surfaces = _discover_workspace(client, client.tree(), args.workspace)
        print(json.dumps({"workspace": workspace, "active_codex_surfaces": surfaces}, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"exclude", "include"}:
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        surface_id = args.surface
        workspace_id = ""
        with contextlib.suppress(CmuxError):
            record = find_surface(client.tree(), args.surface)
            surface_id = record["surface_id"]
            workspace_id = record["workspace_id"]
        def change_exclusion(latest: dict[str, Any]) -> None:
            if args.command == "exclude":
                if not workspace_id:
                    raise RuntimeError("exclude requires a currently live surface so its workspace can be verified")
                rule = workspace_rule_by_id(latest, workspace_id)
                excluded = rule.setdefault("excluded_surface_ids", [])
                if surface_id not in excluded:
                    excluded.append(surface_id)
                reasons = rule.setdefault("excluded_surface_reasons", {})
                reasons[surface_id] = {
                    "reason": "manual exclusion",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                return
            matching_rules = [
                rule for rule in latest["workspace_rules"]
                if surface_id in rule.get("excluded_surface_ids", [])
                or (workspace_id and rule.get("workspace_id") == workspace_id)
            ]
            if not matching_rules:
                raise RuntimeError(f"surface is not excluded by a workspace rule: {args.surface}")
            for rule in matching_rules:
                excluded = rule.setdefault("excluded_surface_ids", [])
                with contextlib.suppress(ValueError):
                    excluded.remove(surface_id)
                reasons = rule.setdefault("excluded_surface_reasons", {})
                reasons.pop(surface_id, None)

        store.mutate(change_exclusion)
        print(json.dumps({"surface_id": surface_id, "action": args.command}, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"remove", "pause", "resume"}:
        def change_target(latest: dict[str, Any]) -> dict[str, Any]:
            target = target_by_id(latest, args.target)
            if args.command == "remove":
                latest["targets"].remove(target)
            elif args.command == "pause":
                target["paused"] = True
                target["paused_reason"] = "manual pause"
            else:
                target["paused"] = False
                target.pop("paused_reason", None)
            return target

        _, changed, _ = store.mutate(change_target)
        print(json.dumps({"action": args.command, "target": changed}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "backfill-refs":
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        live = {record["surface_id"]: record for record in main_surface_records(client.tree())}
        plan = plan_ref_backfill(config, live)
        if args.apply and plan:
            def backfill(latest: dict[str, Any]) -> list[dict[str, Any]]:
                applied: list[dict[str, Any]] = []
                for item in plan:
                    target = target_by_id(latest, item["surface_id"])
                    # Only ever add the display fields; identity and user state
                    # (surface_id / workspace_id / name / paused) stay untouched.
                    target.update(item["fields"])
                    applied.append(item)
                return applied

            _, plan, _ = store.mutate(backfill)
        print(json.dumps({
            "action": "backfill-refs",
            "applied": bool(args.apply),
            "missing_from_tree": sorted(
                str(t.get("ref") or t.get("surface_id"))
                for t in config.get("targets", [])
                if str(t.get("surface_id") or "") not in live
            ),
            "targets": plan,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "untrack-surface":
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        surface_id = args.surface
        workspace_id = ""
        with contextlib.suppress(CmuxError):
            record = find_surface(client.tree(), args.surface)
            surface_id = record["surface_id"]
            workspace_id = record["workspace_id"]
        with contextlib.suppress(RuntimeError):
            explicit = target_by_id(config, args.surface)
            surface_id = str(explicit["surface_id"])
            workspace_id = workspace_id or str(explicit.get("workspace_id") or "")

        def untrack(latest: dict[str, Any]) -> dict[str, Any]:
            removed = [
                target for target in latest["targets"]
                if surface_id in {str(target.get("surface_id") or ""), str(target.get("ref") or ""), str(target.get("name") or "")}
                or args.surface in {str(target.get("surface_id") or ""), str(target.get("ref") or ""), str(target.get("name") or "")}
            ]
            for target in removed:
                latest["targets"].remove(target)
            excluded_rules: list[str] = []
            for rule in latest["workspace_rules"]:
                if workspace_id and str(rule.get("workspace_id") or "") == workspace_id:
                    excluded = rule.setdefault("excluded_surface_ids", [])
                    if surface_id not in excluded:
                        excluded.append(surface_id)
                    reasons = rule.setdefault("excluded_surface_reasons", {})
                    reasons[surface_id] = {
                        "reason": "manual untrack",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    excluded_rules.append(str(rule.get("workspace_id") or ""))
            if not removed and not excluded_rules:
                raise RuntimeError(f"surface is not monitored: {args.surface}")
            return {
                "surface_id": surface_id,
                "removed_explicit": len(removed),
                "excluded_workspace_rules": excluded_rules,
            }

        _, result, _ = store.mutate(untrack)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "list":
        print(json.dumps({"explicit_targets": config.get("targets", []), "workspace_rules": config.get("workspace_rules", [])}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "effective":
        client = CmuxClient(str(config.get("cmux_path", DEFAULT_CMUX)))
        dynamic = discover_rule_targets(client, config)
        print(json.dumps(effective_targets(config, dynamic), ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        state = load_json(DEFAULT_STATE_PATH, {})
        print(json.dumps({
            "mode": config.get("mode"),
            "global_paused": config.get("global_paused"),
            "daemon": describe_daemon_runtime(),
            "explicit_targets": config.get("targets", []),
            "workspace_rules": config.get("workspace_rules", []),
            "runtime": state,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "logs":
        log_path = DEFAULT_LOG_DIR / "watch.log"
        # A missing log used to return 0 with no output, so `ccc logs && echo OK`
        # reported success while reading nothing.  The log directory really does
        # get deleted out from under a running daemon (observed 2026-08-24), and
        # the daemon then keeps writing to an unlinked inode nobody can read.
        # Silence is the worst possible answer here, so fail loudly instead.
        if not DEFAULT_LOG_DIR.exists():
            print(
                f"日志目录不存在: {DEFAULT_LOG_DIR}\n"
                "守卫器可能仍在向已删除的 inode 写日志（发送不受影响，但日志不可读）。\n"
                f"修复: mkdir -p '{DEFAULT_LOG_DIR}' 然后重启守卫器以重建日志句柄",
                file=sys.stderr,
            )
            return 4
        if not log_path.exists():
            print(
                f"日志文件不存在: {log_path}\n"
                "目录已就位，但守卫器还没写到这里；若它正在运行，"
                "其日志句柄可能仍指向旧 inode，需重启才会写入新文件",
                file=sys.stderr,
            )
            return 4
        print(log_path.read_text(encoding="utf-8")[-20000:])
        return 0
    if args.command == "dry-run":
        def set_mode(latest: dict[str, Any]) -> None:
            latest["mode"] = "dry-run"
    elif args.command == "arm":
        def set_mode(latest: dict[str, Any]) -> None:
            latest["mode"] = "armed"
            latest["global_paused"] = False
    elif args.command == "stop-all":
        def set_mode(latest: dict[str, Any]) -> None:
            latest["mode"] = "dry-run"
            latest["global_paused"] = True
    elif args.command in {"context-enforce", "context-observe"}:
        def set_mode(latest: dict[str, Any]) -> None:
            latest["claude_context_enforcement"] = args.command == "context-enforce"
    elif args.command in {"start", "stop", "install", "uninstall"}:
        if args.command == "install" and not config_path.exists():
            def initialize(latest: dict[str, Any]) -> None:
                latest["mode"] = "dry-run"
                latest["global_paused"] = False

            store.mutate(initialize)
        launchctl(args.command)
        return 0
    store.mutate(set_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
