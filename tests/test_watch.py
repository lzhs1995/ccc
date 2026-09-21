import json
import copy
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
import unittest
from unittest import mock
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmux_codex_watch as core  # noqa: E402
import claude_ccc_protocol as protocol  # noqa: E402

from cmux_codex_watch import (  # noqa: E402
    CLAUDE_MESSAGE,
    CLAUDE_WORKING_CLEAR_POLLS,
    CmuxClient,
    CmuxError,
    ClaudeHookSettingsManager,
    ConfigStore,
    FileLock,
    GlobalIncompatibleError,
    Grid,
    IncompatibleError,
    ScreenState,
    TargetRuntime,
    WatchDaemon,
    classify_claude_grid,
    classify_grid,
    classify_text_prefilter,
    default_config,
    discover_codex_surfaces,
    discover_pane_follow_targets,
    discover_rule_targets,
    effective_targets,
    find_surface,
    find_workspace,
    inspect_claude_process,
    main_surface_records,
    parse_claude_context_telemetry,
)


def span(row, column, text, style_id=0, cell_width=None):
    return {
        "row": row,
        "column": column,
        "cell_width": cell_width or max(1, len(text)),
        "style_id": style_id,
        "text": text,
    }


def grid_payload(lines, *, composer="placeholder", cursor_visible=True, working=False, menu=False, error=None, columns=120):
    styles = [
        {"id": 0, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": False},
        {"id": 1, "foreground": "#FFFFFF", "background": "#393939", "faint": False},
        {"id": 2, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": True},
        {"id": 3, "foreground": "#CC372E", "background": "#1E1E1E", "faint": False, "bold": True},
        {"id": 4, "foreground": "#989898", "foreground_source": "rgb", "background": "#393939", "faint": False},
    ]
    rows = max(12, len(lines) + 3)
    row_spans = []
    for row, text in enumerate(lines):
        row_spans.append(span(row, 0, text, 0))
    composer_row = rows - 3
    row_spans.extend([span(composer_row, 0, "›", 1), span(composer_row, 1, " ", 0)])
    if composer == "placeholder":
        row_spans.append(span(composer_row, 2, "Improve documentation in @filename", 2))
    elif composer == "busy":
        row_spans.append(span(composer_row, 2, "real user input", 0))
    if error:
        error_row = composer_row - 4
        row_spans.append(span(error_row, 0, "■ " + error, 3, len("■ " + error)))
    if working:
        row_spans.append(span(composer_row - 1, 0, "Working (0s • esc to interrupt)", 3))
    if menu:
        row_spans.append(span(composer_row - 5, 0, "Implement this plan?", 0))
        row_spans.append(span(composer_row - 4, 0, "1. Yes, implement this plan", 0))
    row_spans.append(span(rows - 1, 0, "gpt-5.6-sol xhigh", 0))
    row_spans.append(span(rows - 1, 20, "Plan mode", 0))
    return {
        "render_grid": {
            "format": "cmux.render-grid.v1",
            "surface_id": "surface-uuid",
            "rows": rows,
            "columns": columns,
            "cursor": {"row": composer_row, "column": 2 if composer == "placeholder" else 14, "visible": cursor_visible},
            "styles": styles,
            "row_spans": row_spans,
            "scrollback_spans": [span(0, 0, "■ old 429", 3)],
            "history_rows": 999,
        }
    }


HIGH_DEMAND_TEXT = "We're currently experiencing high demand, which may cause temporary errors."


def reconnect_payload(attempt="2/5", elapsed="1m 24s", spinner=False, stale_banner=False):
    payload = grid_payload([])
    composer_row = payload["render_grid"]["cursor"]["row"]
    if attempt:
        header = f"• Reconnecting... {attempt} ({elapsed} • esc to interrupt)"
    else:
        header = f"• Reconnecting... ({elapsed} • esc to interrupt)"
    nested = "└ " + HIGH_DEMAND_TEXT
    payload["render_grid"]["row_spans"].extend([
        span(composer_row - 5, 0, header, 0, len(header)),
        span(composer_row - 4, 2, nested, 3, len(nested)),
    ])
    if stale_banner:
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 8, 0, "■ " + HIGH_DEMAND_TEXT, 3, len("■ " + HIGH_DEMAND_TEXT))
        )
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 7, 0, "› 任务请继续", 0, 8)
        )
    if spinner:
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 1, 0, "⠁   ⠈         ⠄             ⠈     ⢀", 4)
        )
    return payload


def visible_lines(payload):
    return list(Grid.from_rpc(payload, "surface-uuid").lines)


def claude_grid_payload(
    lines=None,
    *,
    composer="empty",
    spinner=None,
    error=None,
    question=False,
    tool=False,
    progress=False,
    completed=None,
    ask_footer=False,
    cursor_visible=False,
    columns=120,
):
    """Honest Claude Code grid: ❯ composer, hidden cursor, Claude footer."""

    lines = list(lines or [])
    styles = [
        {"id": 0, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": False},
        {"id": 1, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": True},
    ]
    rows = max(16, len(lines) + 8)
    composer_row = rows - 4
    row_spans = [span(row, 0, text, 0) for row, text in enumerate(lines)]
    row_spans.append(span(composer_row, 0, "❯", 0))
    if composer == "busy":
        row_spans.append(span(composer_row, 2, "typed input", 0))
    elif composer == "placeholder":
        row_spans.append(span(composer_row, 2, "Type a message", 1))
    chrome_row = composer_row - 2
    if spinner:
        row_spans.append(span(chrome_row, 0, spinner, 0))
    if error:
        row_spans.append(span(chrome_row, 0, error, 0))
    if tool:
        row_spans.append(span(chrome_row - 1, 0, "◐ Bash… (timeout)", 0))
    if progress:
        row_spans.append(span(chrome_row - 1, 0, "▸ (3/5)", 0))
    if completed:
        text = completed if isinstance(completed, str) else "✻ Sautéed for 2m 50s"
        row_spans.append(span(chrome_row, 0, text, 0))
    if question:
        row_spans.append(span(chrome_row, 0, "Would you like to run this command?", 0))
    if ask_footer:
        row_spans.append(span(composer_row + 1, 0, "✓ AskUserQuestion ×2", 0))
    row_spans.append(span(rows - 2, 0, "  [Opus 5 (1M context)] │ ~/repo", 0))
    row_spans.append(span(rows - 1, 0, "  ⏵⏵ bypass permissions on", 0))
    return {
        "render_grid": {
            "format": "cmux.render-grid.v1",
            "surface_id": "surface-uuid",
            "rows": rows,
            "columns": columns,
            "cursor": {"row": composer_row, "column": 2, "visible": cursor_visible},
            "styles": styles,
            "row_spans": row_spans,
            "scrollback_spans": [],
            "history_rows": 0,
        }
    }


def display_width(text):
    """Terminal cells occupied by ``text``.

    CJK codepoints are double-width, so ``len()`` understates a Chinese prompt
    by nearly half.  That gap is not incidental to this fixture -- it is *why*
    the real prompt wrapped: ``CLAUDE_MESSAGE`` is 83 characters but 145 cells,
    so it needs a second row even in a 120-column terminal.
    """

    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def wrap_to_cells(text, budget):
    """Split ``text`` into chunks that each fit ``budget`` display cells."""

    chunks = []
    current = ""
    width = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if width + char_width > budget and current:
            chunks.append(current)
            current = ""
            width = 0
        current += char
        width += char_width
    if current:
        chunks.append(current)
    return chunks


def claude_wrapped_composer_payload(
    text=CLAUDE_MESSAGE,
    *,
    columns=60,
    cursor_visible=True,
    rule_before_cursor=False,
    lines=None,
):
    """A Claude composer holding ``text`` wrapped across several rows.

    The surface:43 shape (2026-08-31): a prompt too long for the terminal width
    puts the cursor at the END of the wrapped text, rows below its own ``❯``.
    ``claude_grid_payload`` can only build a single-row composer, so it cannot
    express this at all -- which is why the misclassification had no test.

    Wrapping is measured in display cells, not characters, because ``span``
    reports ``cell_width`` and the grid validator rejects a span that runs past
    the right edge.  Getting this wrong is not a cosmetic fixture detail: at
    ``columns=30`` a character-based split produced spans 60 cells wide.

    ``rule_before_cursor`` draws Claude's ``────`` box rule between the prompt
    and the cursor, i.e. a cursor that genuinely IS somewhere else.  That is the
    negative half of the pair: it must stay ``unverified``.
    """

    body = list(lines or ["previous output"])
    # Column 2 is where Claude starts composer text, so the usable width per row
    # is two cells short of the terminal.
    chunks = wrap_to_cells(text, columns - 2)
    styles = [
        {"id": 0, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": False},
        {"id": 1, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": True},
    ]
    prompt_row = len(body) + 1
    row_spans = [span(row, 0, item, 0, display_width(item)) for row, item in enumerate(body)]
    row_spans.append(span(prompt_row, 0, "❯", 0, 1))
    for offset, chunk in enumerate(chunks):
        row_spans.append(span(prompt_row + offset, 2, chunk, 0, display_width(chunk)))
    last_row = prompt_row + len(chunks) - 1
    cursor_row = last_row
    if rule_before_cursor:
        row_spans.append(span(last_row + 1, 0, "─" * columns, 0, columns))
        cursor_row = last_row + 2
    rows = cursor_row + 4
    footer = "  [Opus 5 (1M context)] │ ~/repo"
    hint = "  ⏵⏵ bypass permissions on"
    row_spans.append(span(rows - 2, 0, footer, 0, display_width(footer)))
    row_spans.append(span(rows - 1, 0, hint, 0, display_width(hint)))
    return {
        "render_grid": {
            "format": "cmux.render-grid.v1",
            "surface_id": "surface-uuid",
            "rows": rows,
            "columns": columns,
            "cursor": {
                "row": cursor_row,
                "column": min(columns, display_width(chunks[-1]) + 2),
                "visible": cursor_visible,
            },
            "styles": styles,
            "row_spans": row_spans,
            "scrollback_spans": [],
            "history_rows": 0,
        }
    }


def claude_idle_screen():
    return "\n".join([
        "✻ Sautéed for 2m 50s",
        "❯ ",
        "  [Opus 5 (1M context)] │ ~/repo",
        "  ⏵⏵ bypass permissions on",
    ])


def claude_hook_event(
    event_id,
    event_name="Stop",
    *,
    completed=False,
    prompt_kind=None,
    surface_id="surface-uuid",
    session_id="session-uuid",
    error_kind=None,
    stop_hook_active=False,
    message_hash=None,
    created_at=None,
):
    event = {
        "version": 1,
        "event_id": event_id,
        "created_at": time.time(),
        "event_name": event_name,
        "surface_id": surface_id,
        "workspace_id": "workspace-uuid",
        "session_id": session_id,
        "transcript_id": "transcript-hash",
        # Production always carries digest(prompt) here.  Verified against the
        # live journal on 2026-08-25: all 1382 watchdog-classified events had
        # exactly digest(configured_message), so a fixture with a synthetic hash
        # would exercise a state that cannot occur and would misreport echo
        # correlation as broken.
        "message_hash": message_hash or (
            protocol._digest(protocol.configured_claude_message())
            if prompt_kind == "watchdog" else "message-hash"
        ),
    }
    if created_at is not None:
        event["created_at"] = created_at
    if event_name == "UserPromptSubmit":
        event["prompt_kind"] = prompt_kind or "human"
    else:
        event["completed"] = completed
        if event_name == "Stop":
            event["stop_hook_active"] = bool(stop_hook_active)
    if error_kind:
        event["error_kind"] = error_kind
    return event


class FakeClient:
    def __init__(self, payload, text="", ping_ok=True, tree=None, top=None):
        self.payload = payload
        self.text = text
        self.ping_ok = ping_ok
        self.tree_data = tree if tree is not None else {"windows": []}
        self.top_data = top
        self.top_calls = []
        self.sent = []
        self.sent_text = []
        self.sent_keys = []
        self.replays = []
        self.reads = []
        self._base_payload = copy.deepcopy(payload)
        self._submit_echo = False

    def ping(self):
        return self.ping_ok

    def tree(self):
        if self.tree_data is None:
            raise CmuxError("surface not found")
        return self.tree_data

    def top(self, workspace_id):
        self.top_calls.append(workspace_id)
        if self.top_data is None:
            raise CmuxError("top unavailable")
        return self.top_data

    def read_screen(self, workspace_id, surface_id):
        self.reads.append((workspace_id, surface_id))
        return self.text

    def replay(self, workspace_id, surface_id):
        if not workspace_id or not surface_id:
            raise IncompatibleError("terminal.replay requires workspace_id and surface_id")
        self.replays.append((workspace_id, surface_id))
        if self._submit_echo and isinstance(self.payload, dict):
            payload = copy.deepcopy(self._base_payload)
            grid = payload.get("render_grid", {})
            spans = grid.get("row_spans", []) if isinstance(grid, dict) else []
            prompt_rows = [item.get("row") for item in spans if item.get("text") == "❯"]
            if prompt_rows:
                spans.append({"row": max(prompt_rows), "column": 2, "cell_width": 1, "style_id": 0, "text": CLAUDE_MESSAGE})
            return payload
        return self.payload

    def send(self, workspace_id, surface_id, message):
        self.sent.append((workspace_id, surface_id, message))

    def send_text(self, workspace_id, surface_id, message):
        self._submit_echo = True
        self.sent_text.append((workspace_id, surface_id, message))
        self.sent.append((workspace_id, surface_id, message))
        if "❯" in self.text:
            self.text = self.text.replace("❯ ", "❯ " + message, 1)

    def send_key(self, workspace_id, surface_id, key):
        self.sent_keys.append((workspace_id, surface_id, key))
        self._submit_echo = False


class PerSurfaceClient(FakeClient):
    def __init__(self, payloads, texts, *, tree=None, top=None):
        super().__init__({}, tree=tree, top=top)
        self.payloads = payloads
        self.texts = texts

    def read_screen(self, workspace_id, surface_id):
        self.reads.append((workspace_id, surface_id))
        return self.texts[surface_id]

    def replay(self, workspace_id, surface_id):
        self.replays.append((workspace_id, surface_id))
        return self.payloads[surface_id]


def armed_daemon(directory, client, extra_targets=None):
    root = Path(directory)
    config_path = root / "config.json"
    state_path = root / "state.json"
    targets = extra_targets or [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}]
    config = {
        "schema_version": 1,
        "mode": "armed",
        "global_paused": False,
        "message": "任务请继续",
        "send_interval_sec": 0,
        "same_frame_guard_polls": 1,
        "targets": targets,
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return WatchDaemon(config_path, state_path, client=client)


def discovery_fixture():
    workspace_id = "workspace-uuid"
    tree = {
        "windows": [{"workspaces": [{
            "id": workspace_id,
            "ref": "workspace:9",
            "title": "Codex pool",
            "panes": [{
                "id": "pane-uuid",
                "ref": "pane:20",
                "surfaces": [
                    {"id": "codex-a", "ref": "surface:44", "type": "terminal", "title": "cnm"},
                    {"id": "helper", "ref": "surface:45", "type": "terminal", "title": "cnm"},
                    {"id": "claude", "ref": "surface:46", "type": "terminal", "title": "claude"},
                    {"id": "shell", "ref": "surface:47", "type": "terminal", "title": "zsh"},
                ],
            }],
        }]}],
    }
    top = {
        "windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "ref": "surface:44", "processes": [
                {"kind": "process", "name": "codex", "path": "/opt/homebrew/bin/codex"},
                {"kind": "process", "name": "codex-code-mode", "path": "/opt/homebrew/bin/codex-code-mode-host"},
            ]},
            {"kind": "surface", "ref": "surface:45", "processes": [
                {"kind": "process", "name": "codex-code-mode", "path": "/opt/homebrew/bin/codex-code-mode-host"},
            ]},
            {"kind": "surface", "ref": "surface:46", "processes": [
                {"kind": "process", "name": "claude", "path": "/opt/homebrew/bin/claude"},
            ]},
            {"kind": "surface", "ref": "surface:47", "processes": [
                {"kind": "process", "name": "zsh", "path": "/bin/zsh"},
            ]},
        ]}]}],
    }
    return tree, top


def claude_armed_daemon(directory, client, **extra):
    """Module-level twin of WatchTests._claude_armed_daemon.

    Several later test classes need the same armed Claude daemon; binding it to
    one class made those tests depend on inheriting from that class.
    """

    root = Path(directory)
    config_path = root / "config.json"
    config = {
        "schema_version": 2,
        "mode": "armed",
        "global_paused": False,
        "message": "任务请继续",
        "send_interval_sec": 1,
        "repeat_send_delay_sec": 1,
        "claude_enabled": True,
        "claude_working_clear_polls": 3,
        "claude_background_input_grace_sec": 0,
        "claude_focused_input_grace_sec": 0,
        "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid",
                     "enabled": True, "paused": False}],
    }
    config.update(extra)
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return WatchDaemon(config_path, root / "state.json", client=client)


def process_fixture(*surfaces):
    """A minimal cmux top payload keyed by stable surface UUID."""

    return {
        "windows": [{"workspaces": [{"surfaces": [
            {
                "kind": "surface",
                "id": surface_id,
                "processes": [{
                    "kind": "process",
                    "name": agent_kind,
                    "path": f"/opt/homebrew/bin/{agent_kind}",
                }],
            }
            for surface_id, agent_kind in surfaces
        ]}]}],
    }


def process_fixture_with_pid(surface_id, agent_kind, pid):
    fixture = process_fixture((surface_id, agent_kind))
    process = fixture["windows"][0]["workspaces"][0]["surfaces"][0]["processes"][0]
    process.update({"pid": pid, "ppid": 1})
    return fixture


class WatchTests(unittest.TestCase):
    def test_plan_text_with_error_words_is_not_prefilter_candidate(self):
        state = classify_text_prefilter("Plan notes: high demand and stream disconnected before completion")
        self.assertEqual(state.kind, "idle")

    def test_menu_has_priority_over_old_error(self):
        payload = grid_payload([], menu=True, error="exceeded retry limit, last status: 429 Too Many Requests")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "menu")

    def test_working_has_priority_over_old_error(self):
        payload = grid_payload([], working=True, error="exceeded retry limit, last status: 429 Too Many Requests")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "working")

    def test_placeholder_grid_is_recoverable_when_error_is_current(self):
        payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "rate_limit")

    def test_find_surface_rejects_dock_surface(self):
        tree = {"windows": [{"workspaces": [{"panes": [{"surfaces": [{
            "id": "dock-uuid", "ref": "surface:84", "dock_scope": "global",
            "workspace_id": "workspace-uuid",
        }]}]}]}]}
        with self.assertRaises(CmuxError):
            find_surface(tree, "surface:84")

    def test_armed_send_blocks_dock_surface_even_without_manager_id(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            tree = {"windows": [{"workspaces": [{"panes": [{"surfaces": [{
                "id": "surface-uuid", "ref": "surface:84", "dock_scope": "global",
                "workspace_id": "workspace-uuid",
            }]}]}]}]}
            client = FakeClient(grid_payload([], error=error), "■ " + error, tree=tree)
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "blocked_dock")

    def test_armed_send_blocks_persisted_manager_surface_without_tree_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = armed_daemon(directory, client)
            daemon.config["manager_surface_id"] = "surface-uuid"
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "blocked_manager")

    def test_unexpected_405_phrase_is_a_direct_trigger(self):
        payload = grid_payload([], error="unexpected status 405 Method Not Allowed")
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.error_type, "http_405")

    def test_non_faint_composer_is_busy(self):
        payload = grid_payload([], composer="busy", error="exceeded retry limit, last status: 429 Too Many Requests")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "composer_busy")

    def test_invisible_cursor_is_incompatible(self):
        payload = grid_payload([], cursor_visible=False, error="exceeded retry limit, last status: 429 Too Many Requests")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "incompatible")

    def test_unverified_composer_does_not_disarm_or_pause_target(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            client = FakeClient(grid_payload([], cursor_visible=False, error=error), "■ " + error)
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(daemon.config["mode"], "armed")
            self.assertFalse(daemon.config["targets"][0]["paused"])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "incompatible")
            self.assertEqual(client.sent, [])

    def test_scrollback_error_is_ignored(self):
        payload = grid_payload([])
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_503_requires_status_context(self):
        plain = grid_payload([], error="port 503 appears in documentation")
        self.assertEqual(classify_grid(Grid.from_rpc(plain, "surface-uuid")).kind, "idle")
        valid = grid_payload([], error="Service Unavailable, HTTP 503")
        self.assertEqual(classify_grid(Grid.from_rpc(valid, "surface-uuid")).error_type, "http_503")

    def test_daemon_dry_run_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config = {
                "schema_version": 1,
                "mode": "dry-run",
                "global_paused": False,
                "message": "任务请继续",
                "poll_interval_sec": 1,
                "send_interval_sec": 1,
                "same_frame_guard_polls": 1,
                "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}],
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            client = FakeClient(payload, "■ exceeded retry limit, last status: 429 Too Many Requests")
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])

    def test_armed_send_has_explicit_target_and_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config = {
                "schema_version": 1,
                "mode": "armed",
                "global_paused": False,
                "message": "任务请继续",
                "send_interval_sec": 0,
                "same_frame_guard_polls": 1,
                "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}],
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            client = FakeClient(payload, "■ exceeded retry limit, last status: 429 Too Many Requests")
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [("workspace-uuid", "surface-uuid", "任务请继续")])

    def test_cmux_client_send_argv_has_enter_and_both_uuids(self):
        calls = []

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def runner(command, **kwargs):
            calls.append(command)
            return Result()

        CmuxClient("/opt/homebrew/bin/cmux", runner=runner).send("workspace-uuid", "surface-uuid", "任务请继续")
        self.assertEqual(
            calls,
            [["/opt/homebrew/bin/cmux", "send", "--workspace", "workspace-uuid", "--surface", "surface-uuid", "任务请继续\n"]],
        )

    def test_plan_body_keywords_without_error_marker_are_not_live_error(self):
        payload = grid_payload([
            "Plan: handle high demand, exceeded retry limit, HTTP 503,",
            "and stream disconnected before completion.",
        ])
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_same_frame_guard_suppresses_one_duplicate_then_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config = {
                "schema_version": 1,
                "mode": "armed",
                "global_paused": False,
                "message": "任务请继续",
                "send_interval_sec": 0,
                "same_frame_guard_polls": 1,
                # This test isolates the frame guard, so the repeat delay is off.
                # test_repeat_send_delay_bounds_one_episode covers that gate.
                "repeat_send_delay_sec": 0,
                "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}],
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            client = FakeClient(payload, "■ exceeded retry limit, last status: 429 Too Many Requests")
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon.process_once(client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 2)

    def test_default_retry_intervals_are_one_second(self):
        config = default_config()
        self.assertEqual(config["poll_interval_sec"], 1.0)
        self.assertEqual(config["send_interval_sec"], 1.0)
        self.assertEqual(config["repeat_send_delay_sec"], 1.0)

    def test_remaining_poll_delay_only_sleeps_unused_period(self):
        from cmux_codex_watch import remaining_poll_delay

        self.assertEqual(remaining_poll_delay(1.0, 0.25), 0.75)
        self.assertEqual(remaining_poll_delay(1.0, 1.25), 0.0)

    def test_current_error_retries_each_second_without_extra_frame_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config = {
                "schema_version": 2,
                "mode": "armed",
                "global_paused": False,
                "message": "任务请继续",
                "send_interval_sec": 1,
                "repeat_send_delay_sec": 1,
                "same_frame_guard_polls": 1,
                "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}],
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = WatchDaemon(config_path, state_path, client=client)

            daemon.process_once(client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(len(client.sent), 1)

            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            runtime.last_send_at -= 1.1
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 2)

    def _one_second_daemon(self, directory, client):
        """A daemon configured the way production actually runs: 1s / 1s."""

        root = Path(directory)
        config_path = root / "config.json"
        config = {
            "schema_version": 2,
            "mode": "armed",
            "global_paused": False,
            "message": "任务请继续",
            "send_interval_sec": 1,
            "repeat_send_delay_sec": 1,
            "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}],
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return WatchDaemon(config_path, root / "state.json", client=client)

    def test_first_send_of_an_episode_is_immediate(self):
        # The product requirement is that a freshly stuck surface is rescued at
        # once; the delay only ever bounds *repeats*.  Making the first nudge
        # wait one send interval quietly slowed every rescue by a poll.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = self._one_second_daemon(directory, client)

            daemon.process_once(client)

            self.assertEqual(len(client.sent), 1)
            self.assertEqual(daemon.runtime["surface-uuid"].send_count, 1)

    def test_working_keeps_the_episode_so_the_repeat_delay_still_applies(self):
        # Working is the *expected* consequence of a successful nudge, not a new
        # event.  Dropping it from the continuity set restarted the episode on
        # every recovery attempt, which reset send_count to 0 and made both the
        # repeat delay and circuit_pause_after unenforceable.  Production ran
        # 108 minutes that way: 12 surfaces, ~91 sends each, every log line
        # reporting count=1.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = self._one_second_daemon(directory, client)

            daemon.process_once(client)
            episode = daemon.runtime["surface-uuid"].episode_id
            self.assertEqual(len(client.sent), 1)

            client.text = "Working (1s • esc to interrupt)"
            daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].state, "working")

            client.text = "■ " + error
            daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].episode_id, episode)
            self.assertEqual(daemon.runtime["surface-uuid"].send_count, 1)
            self.assertEqual(len(client.sent), 1)

    def test_working_flapping_cannot_outrun_the_repeat_delay(self):
        # The failure mode the continuity fix has to close: alternating
        # error/Working frames inside one delay window used to mint a new
        # episode每 flap and send again, so the only real limit was however long
        # Codex happened to stay busy.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = self._one_second_daemon(directory, client)

            for _ in range(8):
                client.text = "■ " + error
                daemon.process_once(client)
                client.text = "Working (1s • esc to interrupt)"
                daemon.process_once(client)

            self.assertEqual(len(client.sent), 1)

    def test_queued_and_superseded_also_keep_the_episode(self):
        # Same argument as Working: both are mid-stall observations produced by
        # our own send, so neither may reset the episode's send accounting.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = self._one_second_daemon(directory, client)
            daemon.process_once(client)
            episode = daemon.runtime["surface-uuid"].episode_id

            for kind in ("queued_followup", "error_superseded"):
                daemon.runtime["surface-uuid"].state = kind
                client.text = "■ " + error
                daemon.process_once(client)
                self.assertEqual(daemon.runtime["surface-uuid"].episode_id, episode, kind)
                self.assertEqual(daemon.runtime["surface-uuid"].send_count, 1, kind)

            self.assertEqual(len(client.sent), 1)

    def test_claude_is_observed_but_never_nudged_while_disabled(self):
        # 14 Claude surfaces are already explicit targets.  Today nothing
        # reaches them only because the Codex composer check happens to reject
        # a ❯ prompt -- an accident, not a decision.  The switch makes the
        # decision explicit and defaults to off.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            screen = "\n".join([
                "■ " + error,
                "",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
            ])
            client = FakeClient(grid_payload([], error=error), screen)
            daemon = self._one_second_daemon(directory, client)

            for _ in range(5):
                daemon.process_once(client)

            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_observed")
            self.assertEqual(client.replays, [])
            # Observation must not quarantine the target: the Codex grid parser
            # cannot read a Claude screen, and letting it fail would persist
            # paused=true on a surface the user deliberately registered.
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_claude_process_without_footer_error_is_observed_not_paused(self):
        # surface:72 is a real Claude process whose frozen review viewport has
        # only a bare ❯, so the structural footer test correctly refuses to
        # call it Claude.  If an error appears in that state, process evidence
        # must keep it out of the Codex grid parser and out of persist-pause.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            screen = "\n".join([
                "■ " + error,
                "",
                "❯ [CMUX-AGENT] read-only review, do not send",
            ])
            client = FakeClient(
                grid_payload([], cursor_visible=False, error=error),
                screen,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._one_second_daemon(directory, client)

            daemon.process_once(client)

            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_observed")
            self.assertEqual(client.sent, [])
            self.assertEqual(client.replays, [])
            self.assertEqual(client.top_calls, ["workspace-uuid"])
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_codex_process_overrides_a_claude_quote_during_an_error(self):
        # A Codex error screen can quote an entire Claude transcript while its
        # own composer is temporarily missing.  Process evidence must win over
        # the text fingerprint, or the Claude observation gate would suppress
        # this real Codex rescue.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            screen = "\n".join([
                "❯ quoted Claude transcript",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
                "",
                "■ " + error,
            ])
            client = FakeClient(
                grid_payload([], error=error),
                screen,
                top=process_fixture(("surface-uuid", "codex")),
            )
            daemon = self._one_second_daemon(directory, client)

            daemon.process_once(client)

            self.assertEqual(client.top_calls, ["workspace-uuid"])
            self.assertEqual(len(client.sent), 1)
            self.assertNotEqual(daemon.runtime["surface-uuid"].state, "claude_observed")

    def test_codex_working_precedes_a_quoted_claude_transcript(self):
        # Working and menus are stronger evidence than a historical quotation.
        # This must short-circuit before a process query or replay.
        with tempfile.TemporaryDirectory() as directory:
            screen = "\n".join([
                "❯ quoted Claude transcript",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
                "Working (1s • esc to interrupt)",
            ])
            client = FakeClient(grid_payload([], working=True), screen)
            daemon = self._one_second_daemon(directory, client)

            daemon.process_once(client)

            self.assertEqual(daemon.runtime["surface-uuid"].state, "working")
            self.assertEqual(client.top_calls, [])
            self.assertEqual(client.replays, [])

    def test_claude_process_lookup_is_cached_per_workspace_for_error_candidates(self):
        # No full top sweep belongs in the one-second loop.  Two error
        # candidates in one workspace share the same five-second snapshot.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            ids = ("claude-a", "claude-b")
            texts = {
                surface_id: "\n".join([
                    "■ " + error,
                    "",
                    "❯ [CMUX-AGENT] frozen review",
                ])
                for surface_id in ids
            }
            payloads = {
                surface_id: grid_payload([], cursor_visible=False, error=error)
                for surface_id in ids
            }
            client = PerSurfaceClient(
                payloads,
                texts,
                top=process_fixture(*( (surface_id, "claude") for surface_id in ids )),
            )
            targets = [
                {"surface_id": surface_id, "workspace_id": "workspace-uuid", "enabled": True, "paused": False}
                for surface_id in ids
            ]
            daemon = armed_daemon(directory, client, extra_targets=targets)

            daemon.process_once(client)
            daemon.process_once(client)

            self.assertEqual(client.top_calls, ["workspace-uuid"])
            self.assertEqual(client.replays, [])
            self.assertEqual(client.sent, [])
            self.assertTrue(all(daemon.runtime[surface_id].state == "claude_observed" for surface_id in ids))

    def test_claude_gate_defaults_to_off_and_is_validated(self):
        self.assertIs(default_config()["claude_enabled"], False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"schema_version": 2, "claude_enabled": "yes"}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ConfigStore(path).load()

    def test_claude_enabled_true_is_accepted_now_that_the_adapter_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"schema_version": 2, "claude_enabled": True}), encoding="utf-8")
            self.assertIs(ConfigStore(path).load()["claude_enabled"], True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"schema_version": 2, "claude_enabled": False}), encoding="utf-8")
            self.assertIs(ConfigStore(path).load()["claude_enabled"], False)

    def test_claude_enabled_true_at_startup_is_not_coerced_off(self):
        # The adapter exists: true must stay true.  Coercing it back to false
        # made every production enable a no-op and hid behind KeepAlive.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "schema_version": 2, "mode": "armed", "global_paused": False,
                "message": "任务请继续", "send_interval_sec": 0,
                "same_frame_guard_polls": 1, "claude_enabled": True,
                "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid",
                             "enabled": True, "paused": False}],
            }, ensure_ascii=False), encoding="utf-8")

            client = FakeClient(grid_payload([]), "> ")
            daemon = WatchDaemon(config_path, root / "state.json", client=client)
            self.assertIs(daemon.config["claude_enabled"], True)
            daemon.process_once(client)
            on_disk = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIs(on_disk["claude_enabled"], True)

    def test_an_unidentifiable_process_falls_back_to_ui_structure(self):
        # The process judge is the strongest evidence, but it can be
        # unavailable: cmux top may fail for a workspace at any moment.  That
        # degraded path already works -- several existing tests reach it by
        # accident because FakeClient.top raises when no data was supplied --
        # but nothing pinned it by name.  If someone later gives FakeClient a
        # default top payload, this branch would silently stop being exercised
        # while every test still passed.  Pin the reason string and the fact
        # that a Claude screen is never handed to the Codex replay parser.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            screen = "\n".join([
                "\u25a0 " + error,
                "",
                "\u276f ",
                "  [Opus 5 (1M context)] \u2502 ~/repo",
                "  \u23f5\u23f5 bypass permissions on",
            ])
            # top=None makes FakeClient.top raise CmuxError -> agent_kind unknown.
            client = FakeClient(grid_payload([], error=error), screen, top=None)
            daemon = self._one_second_daemon(directory, client)
            daemon.process_once(client)

            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(runtime.state, "claude_observed")
            state = daemon._classify_target_screen(
                daemon.config["targets"][0], screen, client,
            )
            self.assertEqual(state.kind, "claude_observed")
            self.assertIn("process unavailable", state.reason)
            self.assertEqual(client.sent, [])
            # Never replay a Claude viewport with the Codex grid parser: that
            # is what used to persist paused=true on a registered surface.
            self.assertEqual(client.replays, [])
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_startup_still_fails_closed_where_no_safe_value_exists(self):
        # Only flags with a known-safe fallback may be coerced.  A bad mode has
        # no safe default -- guessing armed could send, guessing dry-run could
        # silently stop rescuing -- so that must still refuse to start.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"schema_version": 2, "mode": "nonsense"}),
                                   encoding="utf-8")
            with self.assertRaises(RuntimeError):
                WatchDaemon(config_path, root / "state.json", client=FakeClient({}))

    def test_a_hostile_config_edit_cannot_kill_the_watch_loop(self):
        # The refusal added this round is validated inside the *hot reload* that
        # runs at the top of every process_once.  A raise there propagates out
        # of the run loop, and because the LaunchAgent sets KeepAlive=true
        # launchd restarts straight back into the same invalid file: one edited
        # character would stop all 36 Codex surfaces from being rescued, for
        # good.  Refusing the value must never cost the Codex rescue.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "\n".join(["> ", "\u25a0 " + error]))
            daemon = self._one_second_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

            config_path = Path(directory) / "config.json"
            hostile = json.loads(config_path.read_text(encoding="utf-8"))
            hostile["claude_enabled"] = "yes"
            config_path.write_text(json.dumps(hostile, ensure_ascii=False), encoding="utf-8")

            daemon.process_once(client)          # must not raise
            # A non-boolean is still illegal.  Keep the last good in-memory
            # config; do not rewrite the user's file.
            self.assertIs(daemon.config["claude_enabled"], False)
            still_on_disk = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(still_on_disk["claude_enabled"], "yes")

    def test_an_unparseable_value_keeps_the_previous_config(self):
        # Any other invalid edit must also leave the loop running on the last
        # known good config rather than dying mid-flight.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "> ")
            daemon = self._one_second_daemon(directory, client)
            daemon.process_once(client)
            before = daemon.config["mode"]

            config_path = Path(directory) / "config.json"
            broken = json.loads(config_path.read_text(encoding="utf-8"))
            broken["mode"] = "definitely-not-a-mode"
            config_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

            daemon.process_once(client)          # must not raise
            self.assertEqual(daemon.config["mode"], before)

    def test_a_codex_screen_quoting_claude_still_gets_rescued(self):
        # The dangerous direction is the *reverse* of the one the switch guards.
        # With the gate on (production's setting) a Codex surface whose
        # transcript happens to quote a Claude screen -- a plan review, a pasted
        # screenshot, this very project's own notes -- was classified as Claude
        # and had its rescue silently withheld.  Detection must be anchored to
        # the live prompt at the bottom, not to any ❯ anywhere on screen.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            screen = "\n".join([
                "> quoting a Claude session for review:",
                "    ❯ ",
                "    [Opus 5 (1M context)] │ ~/repo",
                "    ⏵⏵ bypass permissions on (shift+tab to cycle)",
                "",
                "■ " + error,
                "",
                "› ",
                "gpt-5.6-sol xhigh · ~/Documents/cnm  100% context left",
            ])
            client = FakeClient(grid_payload([], error=error), screen)
            daemon = self._one_second_daemon(directory, client)
            daemon.process_once(client)

            # The rescue actually went out; the surface has already moved on to
            # awaiting_transition, which is what a *successful* nudge looks like.
            # What matters is that it was never diverted to claude_observed.
            self.assertNotEqual(daemon.runtime["surface-uuid"].state, "claude_observed")
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0][2], "任务请继续")

    def test_claude_detection_is_anchored_to_the_last_prompt_not_a_row_budget(self):
        from cmux_codex_watch import _looks_like_claude_ui

        # A row-distance threshold looked safe at 14 samples (deepest live
        # prompt sat 11 rows off the bottom) but two live surfaces break it the
        # moment Claude starts working: the spinner replaces the empty composer,
        # so the last ❯ becomes a historical user line 22 and 33 rows up.
        working_claude = [
            "❯ 请先阅读 claude.md",
            *([""] * 25),
            "· Frolicking… (45m 48s · ↓ 25.8k tokens)",
            "  [Opus 5 (1M context)] │ ~/repo",
            "  ⏵⏵ bypass permissions on",
        ]
        self.assertTrue(_looks_like_claude_ui(working_claude))

        # Same shape, but a Codex composer is present: Codex, whatever else the
        # transcript quotes.
        self.assertFalse(_looks_like_claude_ui([*working_claude, "› ", "gpt-5.6-sol xhigh"]))

    def test_a_codex_composer_anywhere_on_screen_rules_out_claude(self):
        from cmux_codex_watch import _looks_like_claude_ui

        footer = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"
        # Below the quoted block...
        self.assertFalse(_looks_like_claude_ui(["  ❯ ", footer, "› ", "gpt-5.6-sol xhigh"]))
        # ...and above it.  A Codex composer is drawn at the bottom, so a ›
        # above the ❯ means the ❯ is quoted text; live Claude screens carry no
        # › row at all (0/14 in production).
        self.assertFalse(_looks_like_claude_ui(["› ", "  ❯ ", footer]))

    def test_footer_markers_shared_with_codex_do_not_identify_claude(self):
        from cmux_codex_watch import _looks_like_claude_ui

        # Two live Codex surfaces matched the old footer regex: s133 draws
        # "Plan mode (shift+tab to cycle)" in its own status bar, and s138 was
        # editing prose that mentions CLAUDE.md.  Neither marker is Claude-only,
        # so neither may carry the judgement.
        self.assertFalse(_looks_like_claude_ui(["❯ ", "Plan mode (shift+tab to cycle)"]))
        self.assertFalse(_looks_like_claude_ui(["❯ ", "we edited CLAUDE.md today"]))
        # The strong markers still identify Claude.
        self.assertTrue(_looks_like_claude_ui(["❯ ", "  ⏵⏵ bypass permissions on"]))
        self.assertTrue(_looks_like_claude_ui(["❯ ", "  [Opus 5 (1M context)] │ ~/repo"]))

    def test_a_frozen_review_surface_is_not_claude(self):
        from cmux_codex_watch import _looks_like_claude_ui

        # s72 sits at a frozen "❯ [CMUX-AGENT] …" read-only review prompt with
        # no Claude status bar underneath.  A bare ❯ is a shell, not Claude.
        self.assertFalse(_looks_like_claude_ui([
            "❯ [CMUX-AGENT] read-only review, do not send",
            "",
        ]))

    def test_codex_screens_are_never_taken_for_claude(self):
        # The gate's one unacceptable failure is stopping a Codex rescue, so it
        # demands a ❯ prompt *and* a Claude-only footer marker.  A Codex screen
        # that merely mentions context or a model name must not match.
        from cmux_codex_watch import _looks_like_claude_ui

        self.assertFalse(_looks_like_claude_ui([
            "■ exceeded retry limit, last status: 429 Too Many Requests",
            "› Improve documentation in @filename",
            "gpt-5.6-sol xhigh · ~/Documents/cnm  100% context left",
        ]))
        self.assertFalse(_looks_like_claude_ui(["❯ ", "lzhs@mac repo %"]))
        self.assertTrue(_looks_like_claude_ui([
            "❯ ",
            "  [Opus 5 (1M context)] │ ~/repo",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]))

    def test_queued_followup_banner_is_not_a_send_state(self):
        # The real ws9/p20/s49 incident: high demand banner still on screen, the
        # composer empty and ready, and 30 copies of our message sitting in
        # Codex's own queue.  Codex has the message; sending more only queues more.
        payload = grid_payload(
            ["• Queued follow-up inputs", "  ↳ 任务请继续", "  ↳ 任务请继续"],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "queued_followup")

    def test_queued_followup_survives_the_header_scrolling_off(self):
        # Once the queue is long the banner leaves the viewport and only the
        # identical ↳ rows remain.
        payload = grid_payload(
            ["  ↳ 任务请继续", "  ↳ 任务请继续"],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "queued_followup")

    def test_distinct_arrow_rows_are_not_a_queue(self):
        # ↳ on its own is ordinary Codex output; only repeats of the *same* line
        # mean a pending queue, so a stuck surface here still gets rescued.
        payload = grid_payload(
            ["  ↳ read src/main.py", "  ↳ run pytest"],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "high_demand")

    def test_historical_error_with_newer_transcript_is_not_current(self):
        # The high-demand banner can remain in scrollback after Codex has
        # rendered our follow-up below it.  Even if cmux reuses the banner's
        # style for that newer row, it is not an error continuation.
        payload = grid_payload(
            [],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 2, 0, "› ordinary newer work", 3)
        )
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        # Not plain "idle": the banner is a real error we are deliberately not
        # acting on, and saying so keeps a wrong suppression auditable.
        self.assertEqual(state.kind, "error_superseded")
        self.assertEqual(state.error_type, "high_demand")

    def test_continue_echo_does_not_supersede_live_high_demand(self):
        # Split › / 任务请继续 spans are still CCC's own echo, not newer work.
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 2, 0, "› ", 1, 2),
            span(composer_row - 2, 2, "任务请继续", 0, 10),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_indented_error_tail_still_counts_as_current_block(self):
        # A real wrapped error detail remains eligible: it is indented and
        # uses the marker style, so the terminality gate must not reject it.
        payload = grid_payload([])
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, "■ We're currently experiencing high demand, which may cause", 3),
            span(composer_row - 3, 2, "temporary errors.", 3),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "high_demand")

    def test_indented_new_transcript_does_not_extend_old_error(self):
        # Indentation and reused error styling alone are not enough.  Otherwise
        # normal output below a sticky banner would still revive the old error.
        payload = grid_payload(
            [],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 2, 2, "ordinary newer transcript", 3)
        )
        self.assertEqual(
            classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "error_superseded"
        )

    def test_new_normal_output_with_error_words_blocks_daemon_send(self):
        # Error-looking words alone are not structural proof.  A normal
        # transcript row can legitimately contain both `status:` and a URL;
        # it still makes the old banner historical and must block the full
        # daemon send path.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            newer = "status: continuing at https://example.com/checkpoint"
            payload = grid_payload([], error=error)
            composer_row = payload["render_grid"]["cursor"]["row"]
            payload["render_grid"]["row_spans"].append(
                span(composer_row - 2, 0, newer, 0)
            )
            client = FakeClient(payload, "\n".join(("■ " + error, newer)))
            daemon = armed_daemon(directory, client)

            for _ in range(3):
                daemon.process_once(client)

            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "error_superseded")

    def test_wrapped_tail_without_error_syntax_still_rescues(self):
        # THE GAP.  The 503 message is 102 characters, so a 92-column terminal
        # wraps it and the tail is literally "re headers" -- no status, no URL,
        # no known phrase.  Judging membership by error *syntax* dropped the
        # whole block; judging it by *style* only works for as long as cmux
        # happens to colour the tail like its marker.  Geometry is the proof
        # that survives both: the marker row is full, so the row under it can
        # only be where that text continued.
        head = "■ unexpected status 503 Service Unavailable: upstream connect error or disconnect/reset befo"
        tail = "re headers"
        payload = grid_payload([], columns=len(head))
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, head, 3),
            # Deliberately the ordinary style, not the marker's.  A theme change
            # must not decide whether a stalled session gets rescued.
            span(composer_row - 3, 0, tail, 0),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "http_503")

    def test_multi_row_wrap_keeps_every_adjacent_tail(self):
        # Wrapping fills every row but the last, so membership has to chain:
        # the marker is full -> row 1 is its wrap; row 1 is full -> so is row 2;
        # row 2 is short -> the block ends there.  Asking only "is *this* row
        # full" would always drop the final, short tail.
        columns = 92
        message = ("■ unexpected status 503 Service Unavailable: upstream connect error or "
                   "disconnect/reset before headers while proxying to the upstream responses "
                   "endpoint, giving up after exhausting every retry budget configured for "
                   "this route")
        wrapped = [message[index:index + columns] for index in range(0, len(message), columns)]
        self.assertGreaterEqual(len(wrapped), 3)
        self.assertEqual(len(wrapped[0]), columns)
        self.assertEqual(len(wrapped[1]), columns)
        self.assertLess(len(wrapped[-1]), columns)
        payload = grid_payload([], columns=columns)
        composer_row = payload["render_grid"]["cursor"]["row"]
        first = composer_row - 2 - len(wrapped)
        payload["render_grid"]["row_spans"].extend(
            # Only the marker row carries the error style; the tails are plain.
            span(first + offset, 0, text, 3 if offset == 0 else 0)
            for offset, text in enumerate(wrapped)
        )
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "http_503")

    def test_adjacent_prose_under_a_short_banner_is_not_a_continuation(self):
        # The banner ends nowhere near the right edge, so nothing forced a wrap,
        # and the row below carries neither indentation nor the error style: it
        # is new output, so the banner is history.  This is the one shape that
        # actually occurs in production (a 54%-full MCP warning followed by
        # "Token usage: total=0"), which is why it is asserted here.
        error = "We're currently experiencing high demand, which may cause temporary errors."
        payload = grid_payload([], columns=120)
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, "■ " + error, 3),
            span(composer_row - 3, 0, "Token usage: total=0 input=0 output=0", 0),
        ])
        self.assertEqual(
            classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "error_superseded"
        )

    def test_known_residual_same_style_prose_under_a_short_banner_is_absorbed(self):
        # The honest counterpart of the test above, and the price of keeping the
        # structured nginx 405 working.  A 405 block is a short marker row
        # ending in "<html>" followed by col0 rows in the error style; a line of
        # prose that happens to reuse that style is byte-for-byte the same
        # shape, so it gets absorbed and the banner stays "current".
        #
        # Not reachable on the fleet today: across 46 marker/next-row pairs on
        # 70 live surfaces, the only not-full marker with an adjacent row had a
        # different style.  Asserted rather than hidden so the limit stays
        # visible; if a later change fixes it, this test turns red on purpose.
        error = "We're currently experiencing high demand, which may cause temporary errors."
        payload = grid_payload([], columns=120)
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, "■ " + error, 3),
            span(composer_row - 3, 0, "Here is what I found in the config.", 3),
        ])
        self.assertEqual(
            classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "recoverable_error"
        )

    def test_known_residual_almost_full_banner_absorbs_adjacent_output(self):
        # An honest record of what geometry cannot decide.  A 429 banner can
        # occupy 108 of 109 columns without wrapping at all; a row directly
        # beneath it that is not a transcript marker is then indistinguishable
        # from its tail, gets absorbed, and the banner still counts as current.
        #
        # Not reachable on the fleet today: every 429 observed had blank rows
        # under it, and a blank row ends the block before geometry is consulted.
        # Asserted rather than hidden so the limit stays visible; if a later
        # change fixes it, this test turns red and points at the reason.
        columns = 109
        base = "■ exceeded retry limit, last status: 429 Too Many Requests, url: https://api.example.com/v1/r"
        head = base.ljust(columns - 1, "-")[:columns - 1]
        payload = grid_payload([], columns=columns)
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, head, 3),
            span(composer_row - 3, 0, "Token usage: total=0 input=0 output=0", 0),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "rate_limit")

    def test_new_stall_below_a_storm_of_old_banners_is_rescued(self):
        # The real 04CF3C13 shape: eight historical "banner + our echo" pairs,
        # then a genuinely current stall at the bottom.  Only the last marker
        # matters, so this must still be rescued -- otherwise the terminality
        # gate would strand exactly the sessions it was written to protect.
        error = "We're currently experiencing high demand, which may cause temporary errors."
        # Eight pairs at a gap of 4 need ~32 rows above the composer, so the
        # viewport has to be sized for them or the grid rejects the spans.
        payload = grid_payload([""] * 40)
        composer_row = payload["render_grid"]["cursor"]["row"]
        spans = payload["render_grid"]["row_spans"]
        row = 0
        for _ in range(8):
            spans.append(span(row, 0, "■ " + error, 3))
            spans.append(span(row + 2, 0, "› 任务请继续", 1))
            row += 4
        spans.append(span(composer_row - 3, 0, "■ " + error, 3))
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "high_demand")

    def test_a_new_error_type_below_an_old_echo_reports_the_new_type(self):
        payload = grid_payload([])
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(0, 0, "■ We're currently experiencing high demand, which may cause temporary errors.", 3),
            span(2, 0, "› 任务请继续", 1),
            span(composer_row - 3, 0, "■ exceeded retry limit, last status: 429 Too Many Requests", 3),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "rate_limit")

    def test_tool_call_below_the_banner_makes_it_history(self):
        payload = grid_payload(
            [],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 2, 0, "• Ran rtk git status", 0)
        )
        self.assertEqual(
            classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "error_superseded"
        )

    def test_blank_gap_before_newer_prose_makes_the_banner_history(self):
        # Production never separates a wrap from its marker, and always leaves
        # blank rows before a new transcript entry (gap of 3 in 155/155 cases).
        # A blank row therefore ends the block, and the prose below it is proof
        # that Codex kept working after the error.
        payload = grid_payload(
            [],
            error="We're currently experiencing high demand, which may cause temporary errors.",
        )
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 2, 0, "Here is what I found in the config.", 3)
        )
        self.assertEqual(
            classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "error_superseded"
        )

    def test_queued_followup_blocks_the_daemon_from_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            payload = grid_payload(["• Queued follow-up inputs", "  ↳ 任务请继续"], error=error)
            # The text prefilter has to see the error too, or the daemon skips the
            # grid entirely and the test would pass for the wrong reason.
            client = FakeClient(payload, "■ " + error)
            daemon = armed_daemon(directory, client)
            for _ in range(5):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "queued_followup")

    def test_renamed_queue_banner_is_recognized_as_queued_input(self):
        """Codex renamed the banner; gate 1 only knew the old wording.

        Live wording (2026-09-15):

            • Messages to be submitted after next tool call (press esc to
              interrupt and send immediately)
              ↳ 任务请继续

        Nothing was suppressing this on purpose.  The banner happens to contain
        "esc to interrupt", so ``_working_present`` absorbed it and the surface
        read as Working -- suppression by accident, from a rule about a
        different thing.  Production still climbed 55 -> 66 sends.
        """
        banner = "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)"
        self.assertTrue(core._queued_followup_present([banner, "  ↳ 任务请继续"], 2))
        self.assertFalse(core._working_present([banner, "  ↳ 任务请继续", "■ " + HIGH_DEMAND_TEXT]))

    def test_renamed_queue_banner_blocks_sending_without_relying_on_working(self):
        """The residual the accident does not cover.

        Once the banner scrolls off, a single ``↳`` row remains: the repeated-row
        signature needs two, and there is no longer any "esc to interrupt" on
        screen, so both the accidental guard and gate 1 fall through and the
        daemon sends into a queue that already holds our message.
        """
        with tempfile.TemporaryDirectory() as directory:
            payload = grid_payload(
                ["• Messages to be submitted after next tool call", "  ↳ 任务请继续"],
                error=HIGH_DEMAND_TEXT,
            )
            client = FakeClient(payload, "■ " + HIGH_DEMAND_TEXT)
            daemon = armed_daemon(directory, client)
            for _ in range(5):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "queued_followup")

    def test_queue_banner_with_interrupt_hint_is_queued_not_working(self):
        # Live 8CD43148 / surface:145: reconnect + high demand + this banner.
        # classify_grid used to return working because of "esc to interrupt".
        banner = "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)"
        payload = grid_payload(
            [
                "■ " + HIGH_DEMAND_TEXT,
                "› 任务请继续",
                "• Reconnecting... 4/5 (1m 31s • esc to interrupt)",
                "  └ " + HIGH_DEMAND_TEXT,
                banner,
                "  ↳ 任务请继续",
            ],
        )
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "queued_followup")
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            for _ in range(5):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "queued_followup")

    def test_exhausted_token_401_is_reported_and_never_sent(self):
        """The provider token is out of quota; continuing cannot fix it.

        Representative quota block, with synthetic account and request identifiers:

            ■ unexpected status 401 Unauthorized: [test-token] 该令牌额度已用尽
            !token.UnlimitedQuota && token.RemainQuota = -1 (request id:
            fixture-request-id), url: https://provider.example/v1/responses

        ``_is_error_marker`` accepted the ■ row but ``_match_error_block`` had no
        401 rule, so the scan returned None and the Supervisor showed 空闲 --
        identical to a healthy session, which is why this looked like the
        watchdog had died.  RemainQuota is negative: another 任务请继续 buys the
        same 401.  So this is a reporting state, never a send state.
        """
        block = (
            "■ unexpected status 401 Unauthorized: [test-token] 该令牌额度已用尽\n"
            "!token.UnlimitedQuota && token.RemainQuota = -1 (request id:\n"
            "fixture-request-id), url: https://provider.example/v1/responses"
        )
        self.assertEqual(core._match_error_block(block), "token_exhausted")
        self.assertNotIn("token_exhausted", core.SEND_ELIGIBLE_STATES)

    def test_exhausted_token_surface_shows_its_own_state_not_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            lines = [
                "■ unexpected status 401 Unauthorized: [test-token] 该令牌额度已用尽",
                "!token.UnlimitedQuota && token.RemainQuota = -1 (request id:",
                "fixture-request-id), url: https://provider.example/v1/responses",
            ]
            payload = grid_payload(lines)
            client = FakeClient(payload, "\n".join(lines))
            daemon = armed_daemon(directory, client)
            for _ in range(5):
                daemon.process_once(client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.state, "token_exhausted")
            # ``error_type`` is the *send episode* trigger and stays None here:
            # this state never sends, so no episode opens.  The 错误 column reads
            # ``observed_error_type`` for exactly this reason, so that is the
            # field which has to carry the diagnostic to the Supervisor.
            self.assertEqual(runtime.observed_error_type, "token_exhausted")
            self.assertIsNone(runtime.error_type)

    def test_healthy_401_prose_is_not_an_exhausted_token(self):
        """Only the provider's quota banner counts, not any mention of 401."""
        self.assertIsNone(core._match_error_block(
            "■ unexpected status 401 Unauthorized: check your API key"))
        self.assertIsNone(core._match_error_block(
            "■ the docs explain 该令牌额度已用尽 as a billing state"))

    def test_repeat_send_delay_bounds_one_episode(self):
        # A changing error frame must still respect the one-second lower bound.
        # Once it expires, the same live error episode is retried without a
        # 20-second hold.
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload(["turn 0"], error=error), "■ " + error)
            daemon = armed_daemon(directory, client)
            daemon.config["repeat_send_delay_sec"] = 1
            for turn in range(1, 8):
                daemon.process_once(client)
                # Each poll shows a different screen, exactly like a growing queue.
                client.payload = grid_payload([f"turn {turn}"], error=error)
            self.assertEqual(len(client.sent), 1)

            # Once one second has passed a repeat is allowed again.
            daemon.runtime["surface-uuid"].last_send_at -= 1.1
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 2)

    def test_running_daemon_reloads_arm_from_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config = {
                "schema_version": 1,
                "mode": "dry-run",
                "global_paused": False,
                "message": "任务请继续",
                "send_interval_sec": 0,
                "same_frame_guard_polls": 1,
                "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "enabled": True, "paused": False}],
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            client = FakeClient(payload, "■ exceeded retry limit, last status: 429 Too Many Requests")
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            config["mode"] = "armed"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            daemon._config_mtime_ns = -1
            daemon.process_once(client)
            self.assertEqual(client.sent, [("workspace-uuid", "surface-uuid", "任务请继续")])

    def test_find_surface_preserves_uuid_and_nearest_workspace(self):
        tree = {
            "windows": [{"workspaces": [{"id": "workspace-uuid", "type": "workspace", "surfaces": [
                {"id": "surface-uuid", "ref": "surface:77", "type": "surface", "title": "Codex"}
            ]}]}]
        }
        self.assertEqual(find_surface(tree, "surface:77"), {
            "surface_id": "surface-uuid",
            "workspace_id": "workspace-uuid",
            "ref": "surface:77",
            "title": "Codex",
        })

    def test_find_surface_reads_live_cmux_tree_shape(self):
        tree = {
            "windows": [{
                "id": "window-uuid",
                "ref": "window:1",
                "workspaces": [{
                    "id": "20791EB2-4A63-483A-9E98-72E25D4231A2",
                    "ref": "workspace:3",
                    "title": "Sub2api",
                    "panes": [{
                        "id": "69846802-8A19-4BCA-B0B3-AF8840D3EC0D",
                        "ref": "pane:6",
                        "surfaces": [{
                            "id": "7631C221-7422-441C-B340-5ADB5A81308D",
                            "ref": "surface:25",
                            "type": "terminal",
                            "title": "cnm",
                            "pane_id": "69846802-8A19-4BCA-B0B3-AF8840D3EC0D",
                        }],
                    }],
                }],
            }]
        }
        self.assertEqual(find_surface(tree, "7631C221-7422-441C-B340-5ADB5A81308D"), {
            "surface_id": "7631C221-7422-441C-B340-5ADB5A81308D",
            "workspace_id": "20791EB2-4A63-483A-9E98-72E25D4231A2",
            "ref": "surface:25",
            "title": "cnm",
        })

    def test_three_real_placeholder_prompts_are_empty_composer(self):
        for prompt in (
            "Use /skills to list available skills",
            "Summarize recent commits",
            "Implement {feature}",
        ):
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            composer_spans = [item for item in payload["render_grid"]["row_spans"] if item["style_id"] == 2]
            self.assertTrue(composer_spans)
            composer_spans[0]["text"] = prompt
            state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
            self.assertEqual(state.kind, "recoverable_error", prompt)

    def test_cursor_off_composer_row_is_incompatible(self):
        payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
        payload["render_grid"]["cursor"]["row"] = 0
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "incompatible")

    def test_prompt_not_at_column_zero_is_incompatible(self):
        payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
        for item in payload["render_grid"]["row_spans"]:
            if item["text"] == "›":
                item["column"] = 3
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "incompatible")

    def test_six_error_types_trigger_independently(self):
        cases = {
            "rate_limit": "exceeded retry limit, last status: 429 Too Many Requests",
            "high_demand": "We're currently experiencing high demand, which may cause temporary errors.",
            "stream": "stream disconnected before completion: error sending request for url (https://api.zzzcoding.org/v1/responses)",
            "http_503": "unexpected status 503 Service Unavailable: Service temporarily unavailable",
            "http_405": "unexpected status 405 Method Not Allowed: <html>",
            "prompt_cache": "bad response status code 400 (request id: abc) param=prompt_cache_retention",
        }
        for error_type, text in cases.items():
            payload = grid_payload([], error=text)
            payload["render_grid"]["columns"] = max(payload["render_grid"]["columns"], len(text) + 8)
            state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
            self.assertEqual(state.kind, "recoverable_error", text)
            self.assertEqual(state.error_type, error_type, text)

    def test_zzzcoding_stream_url_triggers_without_prefix(self):
        payload = grid_payload([], error="error sending request for url (https://api.zzzcoding.org/v1/responses)")
        payload["render_grid"]["columns"] = 160
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "stream")

    def test_generic_error_sending_request_is_not_stream(self):
        payload = grid_payload([], error="error sending request for url (https://example.com/other)")
        payload["render_grid"]["columns"] = 160
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_wrapped_stream_url_with_changed_style_triggers(self):
        payload = grid_payload([])
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, "■ error sending request for url", 3),
            span(composer_row - 3, 2, "(https://api.zzzcoding.org/v1/responses)", 0),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "stream")

    def test_rate_limit_requires_both_retry_limit_and_429(self):
        payload = grid_payload([], error="exceeded retry limit, last status: 500")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_generic_400_is_not_recoverable(self):
        payload = grid_payload([], error="HTTP 400 Bad Request: invalid model")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_generic_405_without_method_phrase_is_not_recoverable(self):
        payload = grid_payload([], error="see port 405 in the lab notes")
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_bare_or_documentation_405_is_not_recoverable(self):
        for text in ("405 Not Allowed", "documentation: 405 Not Allowed"):
            payload = grid_payload([], error=text)
            self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle", text)

    def test_405_with_structured_nginx_evidence_triggers(self):
        payload = grid_payload([], error="405 Not Allowed from nginx")
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "http_405")

    def test_nginx_405_html_block_triggers(self):
        payload = grid_payload([])
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 5, 0, "■ unexpected status 405 Method Not Allowed: <html>", 3, 64),
            span(composer_row - 4, 0, "<head><title>405 Not Allowed</title></head>", 3, 48),
            span(composer_row - 3, 0, "url: https://api.zzzcoding.org/v1/responses", 3, 52),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "http_405")

    def test_prompt_cache_trigger_requires_parameter_and_error_context(self):
        for text in (
            "bad response status code 400 (request id: abc)",
            "prompt_cache_retention is described in this diagnostic",
        ):
            payload = grid_payload([], error=text)
            self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "idle", text)
        valid = grid_payload([], error=(
            '{"error":{"message":"bad response status code 400",'
            '"type":"invalid_request_error","param":"prompt_cache_retention",'
            '"code":"invalid_parameter"}}'
        ))
        valid["render_grid"]["columns"] = 300
        self.assertEqual(classify_grid(Grid.from_rpc(valid, "surface-uuid")).error_type, "prompt_cache")

    def test_wrapped_prompt_cache_with_changed_style_triggers(self):
        payload = grid_payload([])
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 4, 0, '■ {"error":{"message":"bad response status code 400",', 3),
            span(composer_row - 3, 2, '"param":"prompt_cache_retention","code":"invalid_parameter"}}', 0),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "prompt_cache")

    def test_wrapped_high_demand_error_triggers(self):
        payload = grid_payload([])
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append({
            "row": composer_row - 4,
            "column": 0,
            "cell_width": 65,
            "style_id": 3,
            "text": "■ We're currently experiencing high demand, which may cause",
        })
        payload["render_grid"]["row_spans"].append({
            "row": composer_row - 3,
            "column": 2,
            "cell_width": 17,
            "style_id": 3,
            "text": "temporary errors.",
        })
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "high_demand")

    def test_reconnect_high_demand_is_not_working_and_sends_once(self):
        payload = reconnect_payload("2/5")
        text = "\n".join(visible_lines(payload))
        self.assertEqual(classify_text_prefilter(text).kind, "candidate")
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, text)
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [("workspace-uuid", "surface-uuid", "任务请继续")])
            later = reconnect_payload("5/5", elapsed="2m 01s")
            client.payload = later
            client.text = "\n".join(visible_lines(later))
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            daemon.runtime["surface-uuid"].last_send_at -= 1.1
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 2)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 2)

    def test_high_demand_spinner_overlay_is_still_current(self):
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(composer_row - 1, 0, "⠁   ⠈         ⠄             ⠈     ⢀              ⠐ ⠐", 4)
        )
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_genuine_working_still_blocks_high_demand(self):
        payload = grid_payload([], error=HIGH_DEMAND_TEXT, working=True)
        self.assertEqual(classify_grid(Grid.from_rpc(payload, "surface-uuid")).kind, "working")
        self.assertEqual(
            classify_text_prefilter("\n".join(visible_lines(payload))).kind,
            "working",
        )

    def test_reconnect_echo_below_stale_banner_uses_reconnect_block(self):
        payload = reconnect_payload("5/5", stale_banner=True)
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))

    def test_live_split_reconnect_spans_are_current_high_demand(self):
        # 2026-09-14 capture: cmux splits the reconnect bullet from
        # the word "Reconnecting", and puts the nested high-demand line in a
        # column-0 span starting with two spaces.  The old validator required
        # the column-0 span itself to be the marker phrase, so classify_grid
        # returned idle while the exact ■ / └ high-demand sentence was visible.
        payload = grid_payload([" "] * 20, columns=82)
        composer_row = payload["render_grid"]["cursor"]["row"]
        banner = "■ " + HIGH_DEMAND_TEXT
        nested = "  └ " + HIGH_DEMAND_TEXT
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 10, 0, banner, 3, len(banner)),
            span(composer_row - 8, 0, "› ", 1, 2),
            span(composer_row - 8, 2, "任务请继续", 0, 10),
            span(composer_row - 5, 0, "•", 3, 1),
            span(composer_row - 5, 1, " ", 0, 1),
            span(composer_row - 5, 2, "Reconnecting... 1/5", 3, 19),
            span(composer_row - 5, 21, " ", 0, 1),
            span(composer_row - 5, 22, "(1m 23s • esc to interrupt)", 1, 27),
            span(composer_row - 4, 0, nested, 1, len(nested)),
            span(composer_row - 1, 0, "⠁   ⠈         ⠄             ⠈     ⢀", 4),
        ])
        state = classify_grid(Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
        self.assertEqual(classify_text_prefilter("\n".join(visible_lines(payload))).kind, "candidate")
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [("workspace-uuid", "surface-uuid", "任务请继续")])

    def test_replay_requires_both_uuids(self):
        with self.assertRaises(IncompatibleError):
            CmuxClient("/opt/homebrew/bin/cmux", runner=lambda *a, **k: None).replay("", "surface-uuid")
        with self.assertRaises(IncompatibleError):
            CmuxClient("/opt/homebrew/bin/cmux", runner=lambda *a, **k: None).replay("workspace-uuid", "")

    def test_replay_argv_includes_workspace_and_surface(self):
        calls = []

        class Result:
            returncode = 0
            stdout = json.dumps({"render_grid": {"format": "cmux.render-grid.v1"}})
            stderr = ""

        def runner(command, **kwargs):
            calls.append(command)
            return Result()

        CmuxClient("/opt/homebrew/bin/cmux", runner=runner).replay("workspace-uuid", "surface-uuid")
        params = json.loads(calls[0][-1])
        self.assertEqual(params["workspace_id"], "workspace-uuid")
        self.assertEqual(params["surface_id"], "surface-uuid")
        self.assertEqual(params["anchor"], "viewport")

    def test_alternating_errors_keep_one_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            client = FakeClient(payload, "■ exceeded retry limit, last status: 429 Too Many Requests")
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            first_episode = daemon.runtime["surface-uuid"].episode_id
            for text in (
                "unexpected status 503 Service Unavailable",
                "stream disconnected before completion",
                "We're currently experiencing high demand, which may cause temporary errors.",
            ):
                client.payload = grid_payload([], error=text)
                client.text = "■ " + text
                daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].episode_id, first_episode)
            self.assertGreaterEqual(daemon.runtime["surface-uuid"].send_count, 1)
            self.assertEqual(daemon.runtime["surface-uuid"].error_type, "high_demand")

    def test_multiple_uuids_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = grid_payload([], error="exceeded retry limit, last status: 429 Too Many Requests")
            client = FakeClient(payload, "■ exceeded retry limit, last status: 429 Too Many Requests")
            daemon = armed_daemon(directory, client, extra_targets=[
                {"surface_id": "surface-a", "workspace_id": "workspace-a", "enabled": True, "paused": False},
                {"surface_id": "surface-b", "workspace_id": "workspace-b", "enabled": True, "paused": False},
            ])
            daemon.process_once(client)
            self.assertEqual(client.sent, [
                ("workspace-a", "surface-a", "任务请继续"),
                ("workspace-b", "surface-b", "任务请继续"),
            ])
            self.assertNotEqual(daemon.runtime["surface-a"].episode_id, daemon.runtime["surface-b"].episode_id)

    def test_explicit_grid_failure_pauses_only_that_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            invalid = grid_payload([], error=error)
            invalid["render_grid"].pop("cursor")
            client = PerSurfaceClient(
                {"surface-a": invalid, "surface-b": grid_payload([], error=error)},
                {"surface-a": "■ " + error, "surface-b": "■ " + error},
            )
            daemon = armed_daemon(directory, client, extra_targets=[
                {"surface_id": "surface-a", "workspace_id": "workspace-a", "enabled": True, "paused": False},
                {"surface_id": "surface-b", "workspace_id": "workspace-b", "enabled": True, "paused": False},
            ])
            daemon.process_once(client)
            self.assertEqual(daemon.config["mode"], "armed")
            self.assertTrue(daemon.config["targets"][0]["paused"])
            self.assertIn("incompatible", daemon.config["targets"][0]["paused_reason"])
            self.assertFalse(daemon.config["targets"][1]["paused"])
            self.assertEqual(client.sent, [("workspace-b", "surface-b", "任务请继续")])

    def test_unsupported_grid_format_still_disarms_globally(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            unsupported = grid_payload([], error=error)
            unsupported["render_grid"]["format"] = "cmux.render-grid.v2"
            with self.assertRaises(GlobalIncompatibleError):
                Grid.from_rpc(unsupported, "surface-a")
            client = PerSurfaceClient(
                {"surface-a": unsupported, "surface-b": grid_payload([], error=error)},
                {"surface-a": "■ " + error, "surface-b": "■ " + error},
            )
            daemon = armed_daemon(directory, client, extra_targets=[
                {"surface_id": "surface-a", "workspace_id": "workspace-a", "enabled": True, "paused": False},
                {"surface_id": "surface-b", "workspace_id": "workspace-b", "enabled": True, "paused": False},
            ])
            daemon.process_once(client)
            self.assertEqual(daemon.config["mode"], "dry-run")
            self.assertFalse(daemon.config["targets"][0]["paused"])
            self.assertEqual(client.sent, [])

    def test_dynamic_grid_failure_persists_workspace_exclusion_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config_path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "armed",
                "global_paused": False,
                "message": "任务请继续",
                "send_interval_sec": 0,
                "workspace_discovery_interval_sec": 0,
                "targets": [],
                "workspace_rules": [{
                    "workspace_id": "workspace-uuid",
                    "ref": "workspace:9",
                    "name": "pool",
                    "enabled": True,
                    "excluded_surface_ids": [],
                    "excluded_surface_reasons": {},
                }],
            }), encoding="utf-8")
            tree, top = discovery_fixture()
            tree["windows"][0]["workspaces"][0]["panes"][0]["surfaces"].append(
                {"id": "codex-b", "ref": "surface:48", "type": "terminal", "title": "cnm"}
            )
            top["windows"][0]["workspaces"][0]["surfaces"].append({
                "kind": "surface",
                "ref": "surface:48",
                "processes": [{"kind": "process", "name": "codex", "path": "/opt/homebrew/bin/codex"}],
            })
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            invalid = grid_payload([], error=error)
            invalid["render_grid"].pop("cursor")
            client = PerSurfaceClient(
                {"codex-a": invalid, "codex-b": grid_payload([], error=error)},
                {"codex-a": "■ " + error, "codex-b": "■ " + error},
                tree=tree,
                top=top,
            )
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon.process_once(client)
            rule = daemon.config["workspace_rules"][0]
            self.assertEqual(daemon.config["mode"], "armed")
            self.assertEqual(rule["excluded_surface_ids"], ["codex-a"])
            self.assertIn("codex-a", rule["excluded_surface_reasons"])
            self.assertEqual(client.sent, [("workspace-uuid", "codex-b", "任务请继续")])

    def test_missing_surface_pauses_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient({}, "■ exceeded retry limit, last status: 429 Too Many Requests")

            def boom(*args, **kwargs):
                raise CmuxError("surface gone")

            client.read_screen = boom
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertTrue(daemon.config["targets"][0]["paused"])

    def test_cmux_down_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient({}, "■ exceeded retry limit, last status: 429 Too Many Requests", ping_ok=False)

            def boom(*args, **kwargs):
                raise CmuxError("socket missing")

            client.read_screen = boom
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertFalse(daemon.config["targets"][0].get("paused"))

    def test_track_surface_waiver_skips_codex_check_but_keeps_dock_excluded(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        import cmux_codex_watch as core

        tree = {"windows": [{"workspaces": [{
            "id": "workspace-uuid", "ref": "workspace:9", "title": "pool",
            "panes": [
                {"id": "pane-main", "ref": "pane:20", "surfaces": [
                    {"id": "shell-uuid", "ref": "surface:47", "type": "terminal", "title": "zsh"},
                ]},
                # A Dock pane whose child surface does not repeat dock_scope.
                {"id": "pane-dock", "ref": "pane:34", "dock_scope": "global", "surfaces": [
                    {"id": "dock-uuid", "ref": "surface:91", "type": "terminal", "title": "Supervisor"},
                ]},
            ],
        }]}]}
        top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": "shell-uuid", "ref": "surface:47",
             "processes": [{"kind": "process", "name": "zsh", "path": "/bin/zsh"}]},
        ]}]}]}

        class Client:
            def tree(self):
                return tree

            def top(self, workspace_id):
                return top

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            base = ["--config", str(config_path), "track-surface"]
            with mock.patch.object(core, "CmuxClient", return_value=Client()):
                with self.assertRaisesRegex(RuntimeError, "live codex process"):
                    core.cli([*base, "shell-uuid"])
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(core.cli([*base, "shell-uuid", "--allow-non-codex", "--name", "ws9-p20-s47"]), 0)
                # The waiver only lifts the process check; the Dock is resolved
                # through find_main_surface, which filters pane-level dock_scope.
                with self.assertRaisesRegex(core.CmuxError, "main-area surface not found"):
                    core.cli([*base, "dock-uuid", "--allow-non-codex"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual([item["surface_id"] for item in config["targets"]], ["shell-uuid"])
        self.assertEqual(config["targets"][0]["name"], "ws9-p20-s47")

    def test_agent_rules_cover_every_cli_seen_in_production(self):
        """One case per distinct process name inventoried from the live machine."""
        from cmux_codex_watch import classify_processes

        def kind(name, path=""):
            return classify_processes([{"kind": "process", "name": name, "path": path}])["agent_kind"]

        # Codex: exact, and its sibling helper must stay excluded.
        self.assertEqual(kind("codex", "/opt/homebrew/Caskroom/codex/0.147.0/bin/codex"), "codex")
        self.assertEqual(kind("codex-code-mode",
                              "/opt/homebrew/Caskroom/codex/0.147.0/bin/codex-code-mode-host"), "other")
        # Claude Code ships two spellings on this machine.
        self.assertEqual(kind("claude.exe",
                              "/Users/x/.nvm/.../@anthropic-ai/claude-code/bin/claude.exe"), "claude")
        self.assertEqual(kind("claude", "/opt/homebrew/Caskroom/claude-code/2.1.170/claude"), "claude")
        # grok: the reported name is truncated to 15 chars AND versioned, so only
        # the path can match it.  A version bump must not break detection.
        self.assertEqual(kind("grok-1.0.4-maco",
                              "/Users/x/.grok/downloads/grok-1.0.4-macos-aarch64"), "grok")
        self.assertEqual(kind("grok-1.0.5-maco",
                              "/Users/x/.grok/downloads/grok-1.0.5-macos-aarch64"), "grok")
        self.assertEqual(kind("grok-9.9.9-maco",
                              "/Users/x/.grok/downloads/grok-9.9.9-macos-aarch64"), "grok")
        # Copilot, plus its bundled helper which must not be mistaken for a CLI.
        self.assertEqual(kind("copilot", "/opt/homebrew/Caskroom/copilot-cli/1.0.65/copilot"), "copilot")
        self.assertEqual(kind("tgrep",
                              "/Users/x/Library/Caches/copilot/pkg/darwin-arm64/1.0.80/tgrep/bin/tgrep"), "other")
        self.assertEqual(kind("gh", "/opt/homebrew/bin/gh"), "gh")
        # Not CLIs at all.
        self.assertEqual(kind("Code Helper", "/Applications/Visual Studio Code.app/.../Code Helper"), "other")
        self.assertEqual(kind("OrbStack", "/Applications/OrbStack.app/Contents/MacOS/OrbStack"), "other")
        # Shell only vs no information at all: different facts, different labels.
        self.assertEqual(kind("zsh", "/bin/zsh"), "shell")
        self.assertEqual(classify_processes([
            {"kind": "process", "name": "bash", "path": "/bin/bash"},
            {"kind": "process", "name": "sleep", "path": "/bin/sleep"},
        ])["agent_kind"], "shell")
        self.assertEqual(classify_processes([])["agent_kind"], "unknown")

    def test_foreground_process_group_selects_the_interactive_agent_not_the_smallest_pid(self):
        from cmux_codex_watch import classify_processes

        processes = [
            {
                "kind": "process",
                "name": "claude.exe",
                "path": "/x/claude.exe",
                "pid": 80288,
                "ppid": 91502,
                "pgid": 80288,
            },
            {
                "kind": "process",
                "name": "claude.exe",
                "path": "/x/claude.exe",
                "pid": 64488,
                "ppid": 1,
                "pgid": 64488,
            },
        ]
        # PID values wrap and are not ages. The temporary nested Claude has
        # the smaller PID; cmux's foreground PGID is the TUI identity.
        classified = classify_processes(processes, foreground_pgids=[80288])
        self.assertEqual(classified["agent_kind"], "claude")
        self.assertEqual(classified["agent_pid"], 80288)

    def test_agent_rules_do_not_widen_the_codex_verdict(self):
        """The pool discovery gate: adding CLIs must never add a codex target."""
        from cmux_codex_watch import classify_processes

        for name, path in (
            ("claude.exe", "/x/@anthropic-ai/claude-code/bin/claude.exe"),
            ("grok-1.0.4-maco", "/Users/x/.grok/downloads/grok-1.0.4-macos-aarch64"),
            ("copilot", "/opt/homebrew/Caskroom/copilot-cli/1.0.65/copilot"),
            ("codex-code-mode", "/opt/homebrew/Caskroom/codex/0.147.0/bin/codex-code-mode-host"),
            ("zsh", "/bin/zsh"),
            ("Code Helper", "/Applications/Visual Studio Code.app/x/Code Helper"),
        ):
            self.assertNotEqual(
                classify_processes([{"kind": "process", "name": name, "path": path}])["agent_kind"],
                "codex", f"{name} must not read as codex")

    def test_program_stem_strips_exe_without_widening_codex_match(self):
        from cmux_codex_watch import _program_stem, classify_surface_processes

        self.assertEqual(_program_stem("claude.exe"), "claude")
        self.assertEqual(_program_stem("Claude.EXE"), "claude")
        self.assertEqual(_program_stem("codex"), "codex")
        top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": "a", "ref": "surface:1", "processes": [
                {"kind": "process", "name": "claude.exe", "path": ""},
                {"kind": "process", "name": "node", "path": "/usr/bin/node"},
            ]},
            {"kind": "surface", "id": "b", "ref": "surface:2", "processes": [
                # Still not codex: only the .exe suffix is ignored, the rest of
                # the name must match exactly.
                {"kind": "process", "name": "codex-code-mode", "path": "/bin/codex-code-mode-host"},
            ]},
            {"kind": "surface", "id": "c", "ref": "surface:3", "processes": [
                {"kind": "process", "name": "codex.exe", "path": "/bin/codex.exe"},
            ]},
            {"kind": "surface", "id": "d", "ref": "surface:4", "processes": [
                {"kind": "process", "name": "zsh", "path": "/bin/zsh"},
            ]},
        ]}]}]}
        kinds = {key: value["agent_kind"] for key, value in classify_surface_processes(top).items()}
        self.assertEqual(kinds["a"], "claude")
        self.assertEqual(kinds["b"], "other")
        self.assertEqual(kinds["c"], "codex")
        # Only a shell left: "shell", not the vaguer "other".
        self.assertEqual(kinds["d"], "shell")

    def test_ref_backfill_is_additive_and_idempotent(self):
        from cmux_codex_watch import TARGET_POSITION_FIELDS, plan_ref_backfill

        config = {"targets": [
            {"surface_id": "alive", "workspace_id": "ws", "ref": "surface:4", "name": "keep", "paused": True},
            {"surface_id": "already", "workspace_id": "ws", "ref": "surface:5",
             "workspace_ref": "workspace:9", "pane_ref": "pane:20", "pane_id": "p-uuid"},
            {"surface_id": "gone", "workspace_id": "ws", "ref": "surface:77"},
        ]}
        live = {
            "alive": {"surface_id": "alive", "workspace_ref": "workspace:1",
                      "pane_ref": "pane:1", "pane_id": "pane-uuid"},
            "already": {"surface_id": "already", "workspace_ref": "workspace:9",
                        "pane_ref": "pane:20", "pane_id": "p-uuid"},
        }
        plan = plan_ref_backfill(config, live)
        # Only the one that is alive and missing fields; never the vanished one.
        self.assertEqual([item["surface_id"] for item in plan], ["alive"])
        self.assertEqual(set(plan[0]["fields"]), set(TARGET_POSITION_FIELDS))

        for item in plan:
            target = next(t for t in config["targets"] if t["surface_id"] == item["surface_id"])
            target.update(item["fields"])
        # Idempotent: a second pass has nothing left to do.
        self.assertEqual(plan_ref_backfill(config, live), [])
        # Identity and user state untouched.
        alive = next(t for t in config["targets"] if t["surface_id"] == "alive")
        self.assertEqual(alive["name"], "keep")
        self.assertIs(alive["paused"], True)
        self.assertEqual(alive["workspace_id"], "ws")

    def test_registration_persists_position_and_old_config_still_loads(self):
        from cmux_codex_watch import _explicit_target_from_record, validate_config

        target = _explicit_target_from_record({
            "surface_id": "uuid", "workspace_id": "ws-uuid", "ref": "surface:9",
            "workspace_ref": "workspace:2", "pane_ref": "pane:5", "pane_id": "pane-uuid",
            "title": "cnm",
        }, "ws2-p5-s9")
        self.assertEqual(target["workspace_ref"], "workspace:2")
        self.assertEqual(target["pane_ref"], "pane:5")
        # A config written before these fields existed must still validate.
        legacy = validate_config({"schema_version": 2, "targets": [
            {"surface_id": "uuid", "workspace_id": "ws-uuid", "ref": "surface:9"},
        ], "workspace_rules": []})
        self.assertEqual(len(legacy["targets"]), 1)

    def test_cmux_client_top_uses_workspace_uuid_and_processes(self):
        calls = []

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def runner(command, **kwargs):
            calls.append(command)
            return Result()

        CmuxClient("/opt/homebrew/bin/cmux", runner=runner).top("workspace-uuid")
        self.assertEqual(calls, [[
            "/opt/homebrew/bin/cmux", "--json", "--id-format", "both",
            "top", "--workspace", "workspace-uuid", "--processes",
        ]])

    def test_workspace_discovery_requires_real_codex_process(self):
        tree, top = discovery_fixture()
        workspace = find_workspace(tree, "workspace:9")
        self.assertEqual(workspace["workspace_id"], "workspace-uuid")
        discovered = discover_codex_surfaces(tree, top, "workspace-uuid")
        self.assertEqual([item["surface_id"] for item in discovered], ["codex-a"])

    def test_workspace_discovery_joins_on_uuid_not_ref(self):
        tree, _ = discovery_fixture()
        # cmux renumbered every ref between the tree and top calls.  A ref join
        # would hand codex-a's processes to the wrong surface; a UUID join must
        # not care.
        top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": "codex-a", "ref": "surface:43", "processes": [
                {"kind": "process", "name": "codex", "path": "/opt/homebrew/bin/codex"},
            ]},
            {"kind": "surface", "id": "claude", "ref": "surface:44", "processes": [
                {"kind": "process", "name": "claude", "path": "/opt/homebrew/bin/claude"},
            ]},
        ]}]}],
        }
        discovered = discover_codex_surfaces(tree, top, "workspace-uuid")
        self.assertEqual([item["surface_id"] for item in discovered], ["codex-a"])

    def test_process_classification_falls_back_to_ref_without_uuid(self):
        # A payload with no surface UUID must still classify rather than
        # silently discovering nothing and stopping every rescue.
        tree, top = discovery_fixture()
        self.assertEqual(
            [item["surface_id"] for item in discover_codex_surfaces(tree, top, "workspace-uuid")],
            ["codex-a"],
        )

    def test_workspace_rule_exclusion_is_honoured(self):
        tree, top = discovery_fixture()
        client = FakeClient({}, tree=tree, top=top)
        config = {
            "targets": [],
            "workspace_rules": [{
                "workspace_id": "workspace-uuid",
                "ref": "workspace:9",
                "name": "pool",
                "enabled": True,
                "excluded_surface_ids": ["codex-a"],
            }],
        }
        self.assertEqual(discover_rule_targets(client, config), [])

    def test_pane_follow_picks_up_respawned_codex_on_the_same_pane(self):
        tree = {
            "windows": [{"workspaces": [{
                "id": "workspace-uuid",
                "ref": "workspace:12",
                "panes": [{
                    "id": "pane-uuid",
                    "ref": "pane:27",
                    "surfaces": [
                        {"id": "codex-old", "ref": "surface:145", "type": "terminal", "title": "old"},
                        {"id": "codex-new", "ref": "surface:213", "type": "terminal", "title": "new"},
                    ],
                }, {
                    "id": "other-pane",
                    "ref": "pane:1",
                    "surfaces": [
                        {"id": "other-pane-codex", "ref": "surface:99", "type": "terminal", "title": "other"},
                    ],
                }],
            }]}],
        }
        top = {
            "windows": [{"workspaces": [{"surfaces": [
                {"kind": "surface", "id": "codex-old", "ref": "surface:145", "processes": [
                    {"kind": "process", "name": "codex", "path": "/opt/homebrew/bin/codex"},
                ]},
                {"kind": "surface", "id": "codex-new", "ref": "surface:213", "processes": [
                    {"kind": "process", "name": "codex", "path": "/opt/homebrew/bin/codex"},
                ]},
                {"kind": "surface", "id": "other-pane-codex", "ref": "surface:99", "processes": [
                    {"kind": "process", "name": "codex", "path": "/opt/homebrew/bin/codex"},
                ]},
            ]}]}],
        }
        client = FakeClient({}, tree=tree, top=top)
        config = {
            "targets": [{
                "surface_id": "codex-old",
                "workspace_id": "workspace-uuid",
                "pane_id": "pane-uuid",
                "ref": "surface:145",
                "enabled": True,
            }],
            "workspace_rules": [],
        }
        found = discover_pane_follow_targets(client, config)
        self.assertEqual([item["surface_id"] for item in found], ["codex-new"])
        self.assertEqual(found[0]["source"], "pane_follow")
        config["workspace_rules"] = [{"workspace_id": "workspace-uuid",
                                       "excluded_surface_ids": ["codex-new"]}]
        self.assertEqual(discover_pane_follow_targets(client, config), [])
        config["workspace_rules"] = []
        config["targets"][0]["paused"] = True
        self.assertEqual(discover_pane_follow_targets(client, config), [])

    def test_explicit_target_wins_over_discovered_duplicate(self):
        explicit = {
            "surface_id": "codex-a", "workspace_id": "workspace-uuid", "ref": "surface:44",
            "name": "manual", "enabled": True, "paused": True,
        }
        dynamic = {
            "surface_id": "codex-a", "workspace_id": "workspace-uuid", "ref": "surface:44",
            "name": "pool:surface:44", "enabled": True, "paused": False, "source": "workspace_rule",
        }
        result = effective_targets({"targets": [explicit]}, [dynamic])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], explicit)
        self.assertTrue(result[0]["paused"])

    def test_daemon_adds_and_removes_dynamic_codex_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config = {
                "schema_version": 2,
                "mode": "armed",
                "global_paused": False,
                "message": "任务请继续",
                "send_interval_sec": 0,
                "same_frame_guard_polls": 1,
                "workspace_discovery_interval_sec": 0,
                "targets": [],
                "workspace_rules": [{
                    "workspace_id": "workspace-uuid", "ref": "workspace:9", "name": "pool",
                    "enabled": True, "excluded_surface_ids": [],
                }],
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            tree, top = discovery_fixture()
            error = "exceeded retry limit, last status: 429 Too Many Requests"
            client = FakeClient(grid_payload([], error=error), "■ " + error, tree=tree, top=top)
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon.process_once(client)
            self.assertEqual(set(daemon.dynamic_targets), {"codex-a"})
            self.assertEqual(client.sent, [("workspace-uuid", "codex-a", "任务请继续")])

            client.top_data = {"windows": []}
            daemon.process_once(client)
            self.assertEqual(daemon.dynamic_targets, {})
            self.assertNotIn("codex-a", daemon.runtime)

    def test_failed_workspace_discovery_keeps_last_known_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config_path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "dry-run",
                "workspace_discovery_interval_sec": 0,
                "targets": [],
                "workspace_rules": [{
                    "workspace_id": "workspace-uuid", "ref": "workspace:9", "enabled": True,
                    "excluded_surface_ids": [],
                }],
            }), encoding="utf-8")
            tree, top = discovery_fixture()
            client = FakeClient({}, tree=tree, top=top)
            daemon = WatchDaemon(config_path, state_path, client=client)
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertEqual(set(daemon.dynamic_targets), {"codex-a"})
            client.top_data = None
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertEqual(set(daemon.dynamic_targets), {"codex-a"})

    def test_config_store_upgrades_v1_on_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "mode": "dry-run",
                "targets": [],
                "workspace_rules": [],
            }), encoding="utf-8")
            store = ConfigStore(path)
            store.mutate(lambda config: config.update({"global_paused": True}))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 2)
            self.assertTrue(persisted["global_paused"])

    def test_deprecated_claude_timing_keys_are_migrated_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "armed",
                "targets": [],
                "workspace_rules": [],
                "claude_idle_first_delay_sec": 300,
                "claude_idle_repeat_sec": 300,
                "claude_error_repeat_delay_sec": 60,
                "claude_futile_idle_limit": 3,
                "claude_futile_error_limit": 4,
            }), encoding="utf-8")
            store = ConfigStore(path)
            self.assertTrue(store.migrate_deprecated())
            persisted = json.loads(path.read_text(encoding="utf-8"))
            for key in (
                "claude_idle_first_delay_sec",
                "claude_idle_repeat_sec",
                "claude_error_repeat_delay_sec",
                "claude_futile_idle_limit",
                "claude_futile_error_limit",
            ):
                self.assertNotIn(key, persisted)
            self.assertEqual(persisted["claude_working_clear_polls"], 3)
            self.assertFalse(store.migrate_deprecated())

    def test_migration_releases_only_legacy_automatic_claude_pauses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "armed",
                "targets": [
                    {
                        "surface_id": "legacy",
                        "workspace_id": "ws",
                        "enabled": True,
                        "paused": True,
                        "paused_reason": "Claude futile loop; need human (idle×3)",
                    },
                    {
                        "surface_id": "manual",
                        "workspace_id": "ws",
                        "enabled": True,
                        "paused": True,
                        "paused_reason": "paused by user",
                    },
                    {
                        "surface_id": "broken",
                        "workspace_id": "ws",
                        "enabled": True,
                        "paused": True,
                        "paused_reason": "incompatible: missing cursor",
                    },
                ],
                "workspace_rules": [],
            }), encoding="utf-8")

            store = ConfigStore(path)
            self.assertTrue(store.migrate_deprecated())
            targets = {item["surface_id"]: item for item in store.load()["targets"]}
            self.assertFalse(targets["legacy"]["paused"])
            self.assertNotIn("paused_reason", targets["legacy"])
            self.assertTrue(targets["manual"]["paused"])
            self.assertTrue(targets["broken"]["paused"])

    def test_config_store_rejects_unknown_future_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "schema_version": 3,
                "mode": "dry-run",
                "targets": [],
                "workspace_rules": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unsupported config schema_version"):
                ConfigStore(path).load()

    def test_config_lock_times_out_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "dry-run",
                "targets": [],
                "workspace_rules": [],
            }), encoding="utf-8")
            lock_path = root / "config.lock"
            store = ConfigStore(path, lock_path=lock_path, timeout_sec=0.05)
            with FileLock(lock_path, purpose="test holder"):
                with self.assertRaisesRegex(RuntimeError, "timed out waiting for config lock"):
                    store.mutate(lambda config: config.update({"mode": "armed"}))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mode"], "dry-run")

    def test_concurrent_config_mutations_preserve_both_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "armed",
                "targets": [{
                    "surface_id": "surface-a", "workspace_id": "workspace-a",
                    "enabled": True, "paused": False,
                }],
                "workspace_rules": [],
            }), encoding="utf-8")
            store = ConfigStore(path)
            first_has_lock = threading.Event()

            def add_surface(config):
                first_has_lock.set()
                time.sleep(0.1)
                config["targets"].append({
                    "surface_id": "surface-b", "workspace_id": "workspace-b",
                    "enabled": True, "paused": False,
                })

            worker = threading.Thread(target=lambda: store.mutate(add_surface))
            worker.start()
            self.assertTrue(first_has_lock.wait(timeout=1))

            def pause_surface(config):
                next(item for item in config["targets"] if item["surface_id"] == "surface-a")["paused"] = True

            store.mutate(pause_surface)
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            persisted = store.load()
            self.assertEqual({item["surface_id"] for item in persisted["targets"]}, {"surface-a", "surface-b"})
            self.assertTrue(next(item for item in persisted["targets"] if item["surface_id"] == "surface-a")["paused"])

    def test_daemon_narrow_pause_preserves_external_add(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            config_path.write_text(json.dumps({
                "schema_version": 2,
                "mode": "armed",
                "targets": [{
                    "surface_id": "surface-a", "workspace_id": "workspace-a",
                    "enabled": True, "paused": False,
                }],
                "workspace_rules": [],
            }), encoding="utf-8")
            daemon = WatchDaemon(config_path, state_path, client=FakeClient({}))
            ConfigStore(config_path).mutate(lambda config: config["targets"].append({
                "surface_id": "surface-b", "workspace_id": "workspace-b",
                "enabled": True, "paused": False,
            }))
            runtime = daemon.runtime.setdefault("surface-a", TargetRuntime())
            daemon._mark_target_incompatible(daemon.config["targets"][0], runtime, "bad grid")
            persisted = ConfigStore(config_path).load()
            self.assertEqual({item["surface_id"] for item in persisted["targets"]}, {"surface-a", "surface-b"})
            self.assertTrue(next(item for item in persisted["targets"] if item["surface_id"] == "surface-a")["paused"])

    def test_main_surface_records_excludes_dock_scope(self):
        tree = {
            "windows": [{
                "id": "window-a", "ref": "window:1", "workspaces": [{
                    "id": "workspace-a", "ref": "workspace:1", "panes": [{
                        "id": "pane-main", "ref": "pane:1", "surfaces": [
                            {"id": "main", "ref": "surface:1", "type": "terminal", "title": "Codex"},
                            {"id": "dock", "ref": "surface:2", "type": "terminal", "title": "Supervisor", "dock_scope": "global"},
                        ],
                    }],
                }],
            }],
        }
        self.assertEqual([item["surface_id"] for item in main_surface_records(tree)], ["main"])

    def test_global_active_surface_overrides_per_workspace_focused_flags(self):
        from cmux_codex_watch import is_surface_focused

        tree = {
            "active": {"surface_id": "global-active"},
            "windows": [{"workspaces": [{"panes": [{"surfaces": [
                {"id": "global-active", "ref": "surface:1", "focused": True},
                {"id": "other-workspace-tab", "ref": "surface:2", "focused": True},
            ]}]}]}],
        }
        self.assertTrue(is_surface_focused(tree, "global-active"))
        self.assertFalse(is_surface_focused(tree, "other-workspace-tab"))

    def _claude_armed_daemon(self, directory, client, **extra):
        root = Path(directory)
        config_path = root / "config.json"
        config = {
            "schema_version": 2,
            "mode": "armed",
            "global_paused": False,
            "message": "任务请继续",
            "send_interval_sec": 1,
            "repeat_send_delay_sec": 1,
            "claude_enabled": True,
            "claude_working_clear_polls": 3,
            "claude_background_input_grace_sec": 0,
            "claude_focused_input_grace_sec": 0,
            "targets": [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid",
                         "enabled": True, "paused": False}],
        }
        config.update(extra)
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return WatchDaemon(config_path, root / "state.json", client=client)

    def test_classify_claude_grid_sends_on_live_503_not_on_working_or_question(self):
        error = classify_claude_grid(Grid.from_rpc(claude_grid_payload(
            error="API Error: 503 No available accounts.",
            tool=True,
        ), "s"))
        self.assertEqual(error.kind, "recoverable_error")
        self.assertEqual(error.error_type, "claude_503")

        working = classify_claude_grid(Grid.from_rpc(claude_grid_payload(
            spinner="✶ Percolating… (2m 0s · ↓ 2.1k tokens)",
        ), "s"))
        self.assertEqual(working.kind, "working")

        question = classify_claude_grid(Grid.from_rpc(claude_grid_payload(question=True), "s"))
        self.assertEqual(question.kind, "menu")

        busy = classify_claude_grid(Grid.from_rpc(claude_grid_payload(composer="busy"), "s"))
        self.assertEqual(busy.kind, "composer_busy")

        idle = classify_claude_grid(Grid.from_rpc(claude_grid_payload(
            completed=True, progress=True, ask_footer=True,
        ), "s"))
        self.assertEqual(idle.kind, "claude_stopped")
        self.assertEqual(idle.error_type, "claude_stopped")

    def test_claude_screen_candidates_never_send_without_a_hook_event(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "API Error: 503 No available accounts."
            client = FakeClient(
                claude_grid_payload(error=error, tool=True),
                "\n".join([error, "◐ Bash…", "❯ ", "  [Opus 5 (1M context)] │ ~/repo"]),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            for _ in range(4):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_hook_unverified")

    def test_stop_failure_hook_sends_once_and_duplicate_event_is_deduped(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished"], completed=True),
                "unfinished\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            event = claude_hook_event("failure-1", "StopFailure", error_kind="claude_503")
            daemon._handle_claude_event(event, client)
            daemon._handle_claude_event(event, client)
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(runtime.claude_last_event_status, "sent")
            self.assertEqual(runtime.claude_last_resume_event_id, "failure-1")

    def test_http_client_retry_blocks_hook_and_fallback_without_pausing(self):
        for code in (429, 502, 503):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory, mock.patch(
                "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
            ):
                banner = f"✻ {code} Upstream access forbidden · Retrying in 32s · attempt 10/10"
                client = FakeClient(
                    claude_grid_payload(error=banner, tool=True, columns=160),
                    banner + "\n" + claude_idle_screen(),
                    top=process_fixture(("surface-uuid", "claude")),
                )
                daemon = self._claude_armed_daemon(directory, client)
                daemon._handle_claude_event(claude_hook_event("retry-failure", "StopFailure"), client)
                for _ in range(4):
                    daemon.process_once(client)
                self.assertEqual(client.sent, [])
                self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_hook_gap_fallback_requires_two_stable_unfocused_frames_and_sends_once(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "watchdog_prompt"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            state = ScreenState(
                "claude_hook_waiting",
                message_kind="claude",
                content_fingerprint="content-a",
                screen_signature="screen-a",
            )
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client
            ))
            self.assertEqual(client.sent, [])
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client
            ))
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client
            ))
            self.assertEqual(len(client.sent), 1)

    def test_hook_gap_fallback_accepts_ordinary_claude_stopped_frame(self):
        """A missed Hook must not exclude the normal stopped classifier state."""
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "sent"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            stopped = ScreenState(
                "claude_stopped",
                message_kind="claude",
                content_fingerprint="content-stopped",
                screen_signature="screen-stopped",
            )

            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client,
            ))
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])
            self.assertEqual(runtime.claude_last_event_status, "sent")
            self.assertEqual(len([
                row for row in daemon.claude_event_ledger.events.values()
                if row.get("status") == "sent"
            ]), 1)

    def test_hook_gap_fallback_cancels_for_focus_input_completion_and_generation_change(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = {"active": {"surface_id": "surface-uuid"}, "windows": []}
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree=tree,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "human_prompt"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            stopped = ScreenState(
                "claude_hook_waiting", message_kind="claude", content_fingerprint="content-a"
            )
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client
            ))
            self.assertIsNone(runtime.claude_fallback_candidate_fingerprint)
            client.tree_data = {"windows": []}
            daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client
            )
            busy = ScreenState("composer_busy", message_kind="claude")
            daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, busy, observation, client
            )
            self.assertIsNone(runtime.claude_fallback_candidate_fingerprint)
            runtime.claude_completed_latched = True
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client
            ))
            runtime.claude_completed_latched = False
            changed = {**observation, "generation": "generation-b"}
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, changed, client
            ))
            self.assertEqual(client.sent, [])

    def test_hook_gap_fallback_retries_same_fingerprint_after_unconfirmed_send(self):
        """A timeout must not permanently suppress the unchanged stopped frame."""
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "sent"
            runtime.claude_submit_last_reason = "confirmation_timeout"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            runtime.claude_fallback_last_fingerprint = "content-a"
            runtime.claude_fallback_sent_at = time.time() - 121.0
            runtime.claude_last_hook_at = runtime.claude_fallback_sent_at - 1.0
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            state = ScreenState(
                "claude_hook_waiting", message_kind="claude",
                content_fingerprint="content-a", screen_signature="screen-a",
            )
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client,
            ))
            self.assertEqual(len(client.sent), 1)

    def test_hook_gap_fallback_uses_unique_bounded_retries_and_then_stops(self):
        """An unconfirmed synthetic rescue cannot become an infinite prompt loop."""
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "sent"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            state = ScreenState(
                "claude_hook_waiting", message_kind="claude",
                content_fingerprint="content-a", screen_signature="screen-a",
            )

            # Initial rescue plus exactly three confirmation-timeout retries.
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client,
            ))
            for expected_retry in range(1, 4):
                daemon._clear_claude_submit(runtime, reason="confirmation_timeout")
                runtime.claude_fallback_sent_at = time.time() - 121.0
                self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                    daemon.config["targets"][0], runtime, state, observation, client,
                ))
                self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                    daemon.config["targets"][0], runtime, state, observation, client,
                ))
                self.assertEqual(runtime.claude_fallback_retry_count, expected_retry)

            daemon._clear_claude_submit(runtime, reason="confirmation_timeout")
            runtime.claude_fallback_sent_at = time.time() - 121.0
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, state, observation, client,
            ))
            self.assertTrue(runtime.claude_fallback_retry_exhausted)
            self.assertEqual(runtime.state, "claude_hook_gap_exhausted")
            self.assertEqual(len(client.sent), 4)
            sent_rows = [
                row for row in daemon.claude_event_ledger.events.values()
                if row.get("surface_id") == "surface-uuid" and row.get("status") == "sent"
            ]
            self.assertEqual(len(sent_rows), 4)

    def test_hook_gap_retry_budget_survives_upstream_fingerprint_change(self):
        """Retry chrome changes are not a new lifecycle episode."""
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "sent"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            first_state = ScreenState(
                "claude_hook_waiting", message_kind="claude",
                content_fingerprint="content-a", screen_signature="screen-a",
            )
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, first_state, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, first_state, observation, client,
            ))
            daemon._clear_claude_submit(runtime, reason="confirmation_timeout")
            runtime.claude_fallback_sent_at = time.time() - 121.0
            changed_state = ScreenState(
                "claude_hook_waiting", message_kind="claude",
                content_fingerprint="content-b", screen_signature="screen-b",
            )
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, changed_state, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, changed_state, observation, client,
            ))
            self.assertEqual(runtime.claude_fallback_retry_count, 1)
            self.assertEqual(len(client.sent), 2)
            self.assertNotEqual(
                daemon.claude_event_ledger.status_of(runtime.claude_fallback_last_event_id),
                "",
            )


    def test_real_stop_older_than_fallback_is_marked_late_without_duplicate_send(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture_with_pid("surface-uuid", "claude", 123),
            )
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_process_pid = 123
            runtime.claude_fallback_sent_at = time.time()
            event = claude_hook_event("late-real-stop")
            event["agent_pid"] = 123
            event["created_at"] = runtime.claude_fallback_sent_at - 0.5
            daemon._handle_claude_event(event, client)
            self.assertEqual(client.sent, [])
            self.assertEqual(
                daemon.claude_event_ledger.events["late-real-stop"]["status"],
                "late_after_fallback",
            )

    def test_stop_from_a_different_session_is_rejected_as_identity_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event(
                "human-1", "UserPromptSubmit", prompt_kind="human", session_id="session-a"
            ), client)
            daemon._handle_claude_event(claude_hook_event(
                "stop-foreign", session_id="session-b"
            ), client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.state, "claude_identity_conflict")
            self.assertEqual(runtime.claude_last_event_status, "identity_conflict")

    def test_session_owned_by_another_surface_is_rejected_before_send(self):
        """A stale CMUX_SURFACE_ID must not route another pane's Hook here."""
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            owner = daemon.runtime.setdefault("other-surface", TargetRuntime())
            owner.claude_session_id = "session-owned-elsewhere"
            owner.claude_hook_health = "healthy"
            daemon._handle_claude_event(
                claude_hook_event(
                    "cross-surface-stop", session_id="session-owned-elsewhere",
                ),
                client,
            )
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.state, "claude_identity_conflict")
            row = daemon.claude_event_ledger.events["cross-surface-stop"]
            self.assertEqual(row["status"], "identity_conflict")
            self.assertIn("other-su", row["detail"])

    def test_dead_cross_surface_session_owner_does_not_block_new_process(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.os.kill", side_effect=ProcessLookupError,
        ):
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            owner = daemon.runtime.setdefault("other-surface", TargetRuntime())
            owner.claude_session_id = "session-reused"
            owner.claude_hook_health = "healthy"
            owner.claude_process_pid = 11884
            owner.claude_process_generation = "old-generation"
            event = claude_hook_event("restarted-session", session_id="session-reused")
            daemon._handle_claude_event(event, client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertNotEqual(runtime.state, "claude_identity_conflict")
            self.assertEqual(runtime.claude_last_event_status, "sent")

    def test_non_claude_process_is_rejected_as_identity_conflict(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
                top=process_fixture(("surface-uuid", "codex")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("wrong-process"), client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.state, "claude_identity_conflict")
            self.assertEqual(runtime.claude_last_event_status, "cancelled")

    def test_completion_hook_latch_survives_working_scrollback_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(
                claude_grid_payload(lines=["完成，建议检查 usage: /context"], completed=True),
                "完成，建议检查 usage: /context\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("done-1", completed=True), client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertTrue(runtime.claude_completed_latched)
            for _ in range(CLAUDE_WORKING_CLEAR_POLLS + 2):
                client.payload = claude_grid_payload(spinner="✶ Percolating… (2m 0s · ↓ 2.1k tokens)")
                client.text = "✶ Percolating… (2m 0s · ↓ 2.1k tokens)\n❯ \n  [Opus 5 (1M context)] │ ~/repo"
                daemon.process_once(client)
            self.assertTrue(runtime.claude_completed_latched)
            self.assertEqual(runtime.state, "claude_completed")
            daemon.save()
            second = WatchDaemon(root / "config.json", root / "state.json", client=client)
            self.assertTrue(second.runtime["surface-uuid"].claude_completed_latched)

    def test_only_real_user_prompt_clears_completion_latch(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("done-1", completed=True), client)
            # A watchdog-shaped prompt that does NOT correlate with any send of
            # ours is a human paste of the same words.  It used to keep the latch
            # set, which stranded surface:36 on 2026-08-25: the user then had to
            # continue that session by hand indefinitely.
            daemon._handle_claude_event(claude_hook_event(
                "watchdog-1", "UserPromptSubmit", prompt_kind="watchdog"
            ), client)
            self.assertFalse(daemon.runtime["surface-uuid"].claude_completed_latched)
            self.assertEqual(
                daemon.runtime["surface-uuid"].claude_last_prompt_attribution,
                "human_exact_prompt",
            )
            # A correlated echo, by contrast, must leave the latch untouched.
            daemon.runtime["surface-uuid"].claude_completed_latched = True
            message = str(daemon.config.get("claude_message") or CLAUDE_MESSAGE)
            runtime = daemon.runtime["surface-uuid"]
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_at = time.time() - 2.0
            daemon._handle_claude_event(claude_hook_event(
                "echo-1", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
            ), client)
            self.assertTrue(runtime.claude_completed_latched)
            daemon._handle_claude_event(claude_hook_event(
                "human-1", "UserPromptSubmit", prompt_kind="human"
            ), client)
            self.assertFalse(daemon.runtime["surface-uuid"].claude_completed_latched)
            self.assertEqual(daemon.runtime["surface-uuid"].claude_generation_id, "human-1")

    def test_short_assistant_turn_creates_a_new_stop_event_without_working_polls(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["Reading the C4 verdict now."], completed=True),
                "Reading the C4 verdict now.\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("stop-1"), client)
            daemon._handle_claude_event(claude_hook_event(
                "watchdog-1", "UserPromptSubmit", prompt_kind="watchdog"
            ), client)
            daemon._handle_claude_event(claude_hook_event("stop-2"), client)
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE, CLAUDE_MESSAGE])
            self.assertEqual(daemon.runtime["surface-uuid"].claude_last_resume_event_id, "stop-2")

    def test_hook_event_for_unregistered_surface_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            event = claude_hook_event("wrong-surface", surface_id="not-authorized")
            daemon._handle_claude_event(event, client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.claude_event_ledger.events["wrong-surface"]["status"], "unmapped")

    def test_claude_permission_and_composer_busy_are_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            question_screen = "\n".join([
                "Would you like to run this command?",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(claude_grid_payload(question=True), question_screen)
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "menu")

            client.payload = claude_grid_payload(composer="busy")
            client.text = "\n".join([
                "❯ typed input",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "composer_busy")

    def test_claude_working_spinner_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = "\n".join([
                "✶ Percolating… (2m 0s · ↓ 2.1k tokens)",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(
                claude_grid_payload(spinner="✶ Percolating… (2m 0s · ↓ 2.1k tokens)"),
                screen,
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "working")

    def test_surface58_completed_agent_footer_is_not_a_live_spinner(self):
        payload = claude_grid_payload(lines=["let me confirm nothing actually changed."], completed=True)
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(span(
            composer_row + 1,
            0,
            "✓ general-purpose [opus-5]: Trace RUC design artifacts and protot... (21m 45s)",
            0,
        ))
        state = classify_claude_grid(Grid.from_rpc(payload, "surface58"))
        self.assertEqual(state.kind, "claude_stopped")

    def test_codex_rescue_still_works_when_claude_is_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            client = FakeClient(grid_payload([], error=error), "■ " + error)
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0][2], "任务请继续")
            self.assertNotEqual(daemon.runtime["surface-uuid"].state, "claude_observed")
            self.assertNotEqual(daemon.runtime["surface-uuid"].error_type, "claude_idle")

    def test_enabled_claude_is_never_handed_to_the_codex_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_hook_unverified")
            self.assertEqual(client.sent, [])
            self.assertGreaterEqual(len(client.replays), 1)
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_enabled_frozen_review_without_footer_does_not_persist_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = "❯ [CMUX-AGENT] read-only review, do not send"
            client = FakeClient(
                claude_grid_payload(composer="busy"),
                screen,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertFalse(daemon.config["targets"][0].get("paused", False))
            self.assertNotEqual(daemon.runtime["surface-uuid"].state, "claude_observed")
            self.assertNotEqual(daemon.runtime["surface-uuid"].state, "recoverable_error")

    def test_enabled_codex_process_still_rescues_a_quoted_claude_error(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "We're currently experiencing high demand, which may cause temporary errors."
            screen = "\n".join([
                "❯ quoted Claude transcript",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
                "",
                "■ " + error,
            ])
            client = FakeClient(
                grid_payload([], error=error),
                screen,
                top=process_fixture(("surface-uuid", "codex")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            before = [dict(item) for item in daemon.config["targets"]]
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertNotEqual(daemon.runtime["surface-uuid"].state, "claude_idle")
            self.assertEqual(daemon.config["targets"], before)

    def test_enabled_codex_working_still_precedes_quoted_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = "\n".join([
                "❯ quoted Claude transcript",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
                "Working (1s • esc to interrupt)",
            ])
            client = FakeClient(grid_payload([], working=True), screen)
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].state, "working")
            self.assertEqual(client.sent, [])
            self.assertEqual(client.replays, [])

    def test_recap_mentioning_503_is_one_stopped_event_not_a_storm(self):
        with tempfile.TemporaryDirectory() as directory:
            recap = "Earlier the 503 happened and the api error was logged."
            screen = "\n".join([
                recap,
                "✻ Sautéed for 2m 50s",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(claude_grid_payload(lines=[recap], completed=True), screen)
            daemon = self._claude_armed_daemon(directory, client)
            before = [item["surface_id"] for item in daemon.config["targets"]]
            daemon.process_once(client)
            self.assertEqual(classify_claude_grid(Grid.from_rpc(client.payload, "s")).kind, "claude_stopped")
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_hook_unverified")
            self.assertEqual(client.sent, [])
            for _ in range(3):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual([item["surface_id"] for item in daemon.config["targets"]], before)

    def test_tool_only_claude_is_working_and_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = "\n".join([
                "◐ Bash… (timeout)",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(claude_grid_payload(tool=True), screen)
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "working")

    def test_working_polls_never_create_a_new_stop_event(self):
        with tempfile.TemporaryDirectory() as directory:
            idle_client = FakeClient(
                claude_grid_payload(completed=True),
                claude_idle_screen(),
            )
            daemon = self._claude_armed_daemon(directory, idle_client)
            daemon.process_once(idle_client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(idle_client.sent, [])
            idle_client.payload = claude_grid_payload(
                spinner="✶ Percolating… (2m 0s · ↓ 2.1k tokens)",
            )
            idle_client.text = "\n".join([
                "✶ Percolating… (2m 0s · ↓ 2.1k tokens)",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            for _ in range(CLAUDE_WORKING_CLEAR_POLLS):
                daemon.process_once(idle_client)
                self.assertEqual(runtime.state, "working")
            self.assertFalse(runtime.claude_prompt_pending)
            self.assertEqual(idle_client.sent, [])

            idle_client.payload = claude_grid_payload(completed=True)
            idle_client.text = claude_idle_screen()
            daemon.process_once(idle_client)
            self.assertEqual(runtime.state, "claude_hook_unverified")
            self.assertEqual(idle_client.sent, [])
            daemon.process_once(idle_client)
            self.assertEqual(idle_client.sent, [])

    def test_broken_claude_replay_does_not_persist_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = claude_grid_payload(completed=True)
            broken["render_grid"].pop("cursor")
            client = FakeClient(broken, claude_idle_screen())
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertFalse(daemon.config["targets"][0].get("paused", False))
            self.assertEqual(daemon.runtime["surface-uuid"].state, "incompatible")

    def test_new_task_hint_is_idle_chrome(self):
        frame = classify_claude_grid(Grid.from_rpc(
            claude_grid_payload(lines=["new task?"], completed=True), "s",
        ))
        self.assertEqual(frame.kind, "claude_stopped")

    def test_composer_box_does_not_hide_a_completion_report(self):
        # 776BCF10 / workspace:7 / surface:36: last real sentence was
        # 「完成，建议检查 usage: /context」, then ✻ … for, then the ──── box
        # around empty ❯.  The rule was not chrome, so the detector treated the
        # box as last content and 5-minute-nudged a normal finish.
        from cmux_codex_watch import _claude_finished_normally

        lines = [
            "待你选一条才能继续：",
            "1. 先立项调查（推荐）",
            "2. 恢复桥后重跑整个 FULL45。",
            "3. 恢复桥后只重跑 S12 确认可复现性。",
            "完成，建议检查 usage: /context",
            "✻ Cogitated for 2m 37s",
            "─" * 40,
            "❯ ",
            "─" * 40,
            "  [Opus 5 (1M context)] │ ~/repo",
            "  ⏵⏵ bypass permissions on",
        ]
        prompt_row = 7
        self.assertTrue(_claude_finished_normally(lines, prompt_row))
        payload = claude_grid_payload(
            lines=[
                "待你选一条才能继续：",
                "1. 先立项调查（推荐）",
                "完成，建议检查 usage: /context",
            ],
            completed="✻ Cogitated for 2m 37s",
        )
        composer_row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 1, 0, "─" * 40, 0),
            span(composer_row + 1, 0, "─" * 40, 0),
        ])
        state = classify_claude_grid(Grid.from_rpc(payload, "s"))
        self.assertEqual(state.kind, "claude_completed")
        with tempfile.TemporaryDirectory() as directory:
            screen = "\n".join(lines)
            client = FakeClient(payload, screen)
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            daemon.runtime["surface-uuid"].episode_started_at -= 301
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_completed")

    def test_completion_report_is_a_normal_finish_and_is_never_sent(self):
        payload = claude_grid_payload(
            lines=["完成，建议检查 usage: /context"],
            completed=True,
        )
        state = classify_claude_grid(Grid.from_rpc(payload, "s"))
        self.assertEqual(state.kind, "claude_completed")
        with tempfile.TemporaryDirectory() as directory:
            screen = "\n".join([
                "完成，建议检查 usage: /context",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(payload, screen)
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            daemon.runtime["surface-uuid"].episode_started_at -= 301
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_completed")

    def test_newer_screen_content_cannot_override_hook_completion_latch(self):
        completion = claude_grid_payload(
            lines=["完成，建议检查 usage: /context"],
            completed=True,
        )
        client = FakeClient(
            completion,
            "完成，建议检查 usage: /context\n" + claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        with tempfile.TemporaryDirectory() as directory:
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("done-screen", completed=True), client)
            self.assertEqual(client.sent, [])
            self.assertTrue(daemon.runtime["surface-uuid"].claude_completed_latched)

            client.payload = claude_grid_payload(
                lines=["新的普通正文，但没有完成报告。"],
                completed=True,
            )
            client.text = "新的普通正文，但没有完成报告。\n" + claude_idle_screen()
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertTrue(daemon.runtime["surface-uuid"].claude_completed_latched)
            self.assertEqual(daemon.runtime["surface-uuid"].state, "claude_completed")

    def test_task_finished_alias_is_not_the_required_completion_suffix(self):
        payload = claude_grid_payload(lines=["好的，任务完成了"], completed=True)
        self.assertEqual(classify_claude_grid(Grid.from_rpc(payload, "s")).kind, "claude_stopped")

    def test_completion_suffix_must_be_the_last_effective_content(self):
        payload = claude_grid_payload(
            lines=[
                "完成，建议检查 usage: /context",
                "但随后又出现了新的正文。",
            ],
            completed=True,
        )
        self.assertEqual(classify_claude_grid(Grid.from_rpc(payload, "s")).kind, "claude_stopped")

    def test_default_claude_message_is_not_the_codex_nudge(self):
        cfg = default_config()
        self.assertEqual(cfg["message"], "任务请继续")
        self.assertEqual(cfg["claude_message"], CLAUDE_MESSAGE)
        self.assertEqual(cfg["claude_background_input_grace_sec"], 1.0)
        self.assertEqual(cfg["claude_focused_input_grace_sec"], 3.0)
        for key in (
            "claude_idle_first_delay_sec",
            "claude_idle_repeat_sec",
            "claude_error_repeat_delay_sec",
            "claude_futile_idle_limit",
            "claude_futile_error_limit",
        ):
            self.assertNotIn(key, cfg)
        self.assertEqual(cfg["claude_working_clear_polls"], CLAUDE_WORKING_CLEAR_POLLS)

    def test_connection_lost_is_a_live_claude_error(self):
        for banner, expected in (
            ("API Error: Connection lost mid-response", "claude_stream"),
            ("API Error (500 internal server error)", "claude_api"),
            ("stream disconnected before completion", "claude_stream"),
        ):
            state = classify_claude_grid(Grid.from_rpc(
                claude_grid_payload(lines=[banner], completed=False),
                "s",
            ))
            self.assertEqual(state.kind, "recoverable_error", banner)
            self.assertEqual(state.error_type, expected, banner)

    def test_recap_api_error_does_not_override_a_completion_report(self):
        payload = claude_grid_payload(
            lines=[
                "API Error: Connection lost mid-response",
                "I recovered and finished the write-up.",
                "完成，建议检查 usage: /context",
            ],
            completed=True,
        )
        state = classify_claude_grid(Grid.from_rpc(payload, "s"))
        self.assertEqual(state.kind, "claude_completed")

    def test_prose_mention_of_connection_lost_is_not_a_live_error(self):
        state = classify_claude_grid(Grid.from_rpc(
            claude_grid_payload(
                lines=["I discussed the Connection lost mid-response case in recap."],
                completed=True,
            ),
            "s",
        ))
        self.assertEqual(state.kind, "claude_stopped")

    def test_recap_retry_prose_is_not_a_live_banner(self):
        for line in (
            "During the outage it kept Retrying in 5s before recovering.",
            "We hit attempt 10/10 and gave up.",
            "No available accounts earlier today.",
        ):
            state = classify_claude_grid(Grid.from_rpc(
                claude_grid_payload(lines=[line], completed=True),
                "s",
            ))
            self.assertEqual(state.kind, "claude_stopped", line)

    def test_recap_retry_prose_does_not_override_completion(self):
        payload = claude_grid_payload(
            lines=[
                "We hit attempt 10/10 and gave up.",
                "No available accounts earlier today.",
                "完成，建议检查 usage: /context",
            ],
            completed=True,
        )
        self.assertEqual(classify_claude_grid(Grid.from_rpc(payload, "s")).kind, "claude_completed")

    def test_codex_stream_banner_is_not_classified_by_the_claude_regex(self):
        payload = grid_payload(
            ["■ stream disconnected before completion"],
            error="stream disconnected before completion",
        )
        state = classify_grid(Grid.from_rpc(payload, "s"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertEqual(state.error_type, "stream")
        self.assertEqual(state.message_kind, "codex")

    def test_connection_lost_requires_stop_failure_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            banner = "API Error: Connection lost mid-response"
            payload = claude_grid_payload(lines=[banner])
            screen = "\n".join([
                banner,
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(
                payload,
                screen,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            with mock.patch("cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0):
                daemon._handle_claude_event(
                    claude_hook_event("stream-1", "StopFailure", error_kind="claude_stream"),
                    client,
                )
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0][2], CLAUDE_MESSAGE)
            self.assertEqual(daemon.runtime["surface-uuid"].error_type, "claude_stream")

    def test_session_start_only_marks_hook_healthy_and_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture_with_pid("surface-uuid", "claude", 4321),
            )
            daemon = self._claude_armed_daemon(directory, client)
            event = claude_hook_event("session-start", "SessionStart")
            event["agent_pid"] = 4321
            daemon._handle_claude_event(event, client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.claude_hook_health, "healthy")
            self.assertEqual(runtime.claude_process_pid, 4321)
            self.assertEqual(runtime.claude_last_event_status, "session_started")

    def test_nested_claude_session_start_cannot_replace_the_surface_root_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture_with_pid("surface-uuid", "claude", 4321),
            )
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_process_pid = 4321
            runtime.claude_session_id = "root-session"
            runtime.claude_hook_health = "healthy"
            event = claude_hook_event(
                "nested-start",
                "SessionStart",
                session_id="nested-session",
            )
            event["agent_pid"] = 9876

            daemon._handle_claude_event(event, client)

            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.claude_session_id, "root-session")
            self.assertEqual(runtime.claude_process_pid, 4321)
            self.assertEqual(runtime.claude_hook_health, "healthy")
            ledger = json.loads((Path(directory) / "claude-event-ledger.json").read_text())
            self.assertEqual(ledger["events"]["nested-start"]["status"], "nested_process_ignored")

    def test_claude_child_hook_from_same_surface_process_tree_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top={"windows": [{"workspaces": [{"surfaces": [{
                    "kind": "surface",
                    "id": "surface-uuid",
                    "processes": [
                        {
                            "kind": "process",
                            "name": "claude.exe",
                            "path": "/x/claude.exe",
                            "pid": 4321,
                            "ppid": 1,
                        },
                        {
                            "kind": "process",
                            "name": "claude.exe",
                            "path": "/x/claude.exe",
                            "pid": 9876,
                            "ppid": 4321,
                        },
                    ],
                }]}]}]},
            )
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_process_pid = 4321
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            event = claude_hook_event("child-stop", session_id="session-uuid")
            event["agent_pid"] = 9876

            daemon._handle_claude_event(event, client)

            self.assertEqual(len(client.sent), 1)
            self.assertEqual(runtime.claude_last_event_status, "sent")
            self.assertNotIn(
                "child-stop",
                {
                    key
                    for key, value in daemon.claude_event_ledger.events.items()
                    if value.get("status") == "nested_process_ignored"
                },
            )

    def test_nested_claude_stop_cannot_complete_or_resume_the_surface_root_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture_with_pid("surface-uuid", "claude", 4321),
            )
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_process_pid = 4321
            runtime.claude_session_id = "root-session"
            event = claude_hook_event(
                "nested-stop",
                "Stop",
                completed=True,
                session_id="nested-session",
            )
            event["agent_pid"] = 9876

            daemon._handle_claude_event(event, client)

            self.assertEqual(client.sent, [])
            self.assertFalse(runtime.claude_completed_latched)
            self.assertEqual(runtime.claude_session_id, "root-session")

    def test_pid_bound_root_stop_repairs_a_stale_nested_session_from_old_state(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(lines=["completed"], completed=True),
                "completed\n" + claude_idle_screen(),
                top=process_fixture_with_pid("surface-uuid", "claude", 4321),
            )
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_process_pid = 4321
            runtime.claude_session_id = "stale-nested-session"
            event = claude_hook_event(
                "root-stop",
                "Stop",
                completed=True,
                session_id="root-session",
            )
            event["agent_pid"] = 4321

            daemon._handle_claude_event(event, client)

            self.assertEqual(client.sent, [])
            self.assertTrue(runtime.claude_completed_latched)
            self.assertEqual(runtime.claude_session_id, "root-session")
            self.assertEqual(runtime.claude_last_event_status, "completed")

    def test_consecutive_hook_rescues_warn_once_but_keep_sending(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(
                directory,
                client,
                claude_repeat_warning_after=2,
            )
            with mock.patch.object(daemon, "_notify_async") as notify:
                daemon._handle_claude_event(claude_hook_event("stop-1"), client)
                daemon._handle_claude_event(
                    claude_hook_event("watchdog-1", "UserPromptSubmit", prompt_kind="watchdog"), client
                )
                daemon._handle_claude_event(claude_hook_event("stop-2"), client)
                daemon._handle_claude_event(
                    claude_hook_event("watchdog-2", "UserPromptSubmit", prompt_kind="watchdog"), client
                )
                daemon._handle_claude_event(claude_hook_event("stop-3"), client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(len(client.sent), 3)
            self.assertEqual(runtime.claude_consecutive_resumes, 3)
            self.assertTrue(runtime.claude_repeat_warning)
            notify.assert_called_once()
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

            daemon._handle_claude_event(
                claude_hook_event("done", completed=True),
                client,
            )
            self.assertEqual(runtime.claude_consecutive_resumes, 0)
            self.assertFalse(runtime.claude_repeat_warning)

    def test_process_inspection_flags_legacy_inline_settings_without_leaking_command(self):
        command = (
            "Mon Aug 17 06:34:31 2026 "
            "claude --settings {\"hooks\":{\"Stop\":[{\"command\":\"cmux hooks claude stop\"}]}}"
        )
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            ["ps"], 0, stdout=command + "\n", stderr="",
        ))
        result = inspect_claude_process(23988, runner=runner)
        self.assertTrue(result["legacy_override"])
        self.assertTrue(result["has_inline_settings"])
        self.assertNotIn("command", result)
        self.assertNotIn("hooks claude", json.dumps(result))

    def test_process_inspection_does_not_flag_settings_file_as_legacy_override(self):
        command = (
            "Sun Aug 30 22:32:17 2026 "
            "claude --settings /Users/tester/.claude-profiles/linxi-paid5.json "
            "--resume 132509c7-e356-455d-af8d-645a9d832e5a"
        )
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            ["ps"], 0, stdout=command + "\n", stderr="",
        ))
        result = inspect_claude_process(91424, runner=runner)
        self.assertFalse(result["legacy_override"])
        self.assertFalse(result["has_inline_settings"])
        self.assertFalse(result["inline_has_ccc_hook"])
        self.assertNotIn("linxi-paid5", json.dumps(result))

    def test_process_inspection_accepts_equals_inline_settings(self):
        command = (
            "Sun Aug 30 22:32:17 2026 "
            "claude --settings={\"hooks\":{\"Stop\":[{\"command\":\"x\"}]}}"
        )
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            ["ps"], 0, stdout=command + "\n", stderr="",
        ))
        result = inspect_claude_process(91424, runner=runner)
        self.assertTrue(result["legacy_override"])
        self.assertTrue(result["has_inline_settings"])

    def test_legacy_hook_health_overrides_an_unreadable_claude_grid_without_pausing(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient({}, top=process_fixture(("surface-uuid", "claude")))
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            daemon._apply_claude_process_observation(runtime, {
                "pid": 23988,
                "started_at": "2026-08-17T06:34:31",
                "started_epoch": time.time() - 100,
                "generation": "legacy-generation",
                "legacy_override": True,
            })
            state = daemon._apply_claude_runtime_guards(
                "surface-uuid",
                runtime,
                type("State", (), {
                    "message_kind": None,
                    "error_type": None,
                    "kind": "incompatible",
                    "screen_signature": None,
                    "content_fingerprint": None,
                })(),
            )
            self.assertEqual(state.kind, "claude_hook_legacy")
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_repeated_stopped_frames_do_not_pause_or_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = claude_grid_payload(
                lines=["tool result: still the same recap"],
                completed=True,
            )
            screen = "\n".join([
                "tool result: still the same recap",
                "❯ ",
                "  [Opus 5 (1M context)] │ ~/repo",
                "  ⏵⏵ bypass permissions on",
            ])
            client = FakeClient(
                payload,
                screen,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            for _ in range(8):
                daemon.process_once(client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertFalse(daemon.config["targets"][0].get("paused", False))
            self.assertEqual(runtime.state, "claude_hook_unverified")

    def test_repeated_error_frames_do_not_pause_or_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            error = "API Error: 503 No available accounts."
            payload = claude_grid_payload(error=error, tool=True)
            screen = "\n".join([error, "◐ Bash…", "❯ ", "  [Opus 5 (1M context)] │ ~/repo"])
            client = FakeClient(
                payload,
                screen,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            for _ in range(8):
                daemon.process_once(client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertFalse(daemon.config["targets"][0].get("paused", False))
            self.assertEqual(runtime.state, "claude_hook_unverified")

    def test_background_hook_stop_uses_short_event_preflight(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("background-stop"), client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])
            self.assertEqual(runtime.state, "claude_event_sent")

    def test_focused_hook_stop_does_not_wait_the_old_three_second_grace(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            tree = {"active": {"surface_id": "surface-uuid"}, "windows": []}
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree=tree,
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("focused-stop"), client)
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])

    def test_hook_stop_is_blocked_by_proven_context_limit_without_pausing(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            payload = claude_grid_payload(lines=[
                "unfinished output",
                "⎿ Context limit reached · /compact or /clear to continue",
                "上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
                "0% until auto-compact",
            ])
            client = FakeClient(
                payload,
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon.config["claude_context_enforcement"] = True
            daemon._handle_claude_event(claude_hook_event("context-stop"), client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.state, "claude_context_waiting")
            self.assertEqual(runtime.claude_last_event_status, "cancelled")
            self.assertFalse(daemon.config["targets"][0].get("paused", False))

    def test_user_input_appearing_during_final_preflight_defers_send(self):
        class PreflightRaceClient(FakeClient):
            def __init__(self, stopped, busy, **kwargs):
                super().__init__(stopped, **kwargs)
                self.stopped = stopped
                self.busy = busy
                self.replay_count = 0

            def replay(self, workspace_id, surface_id):
                self.replays.append((workspace_id, surface_id))
                self.replay_count += 1
                return self.busy if self.replay_count >= 2 else self.stopped

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            stopped = claude_grid_payload(lines=["unfinished output"], completed=True)
            busy = claude_grid_payload(lines=["unfinished output"], composer="busy")
            client = PreflightRaceClient(
                stopped,
                busy,
                text="unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon._handle_claude_event(claude_hook_event("typing-race"), client)
            runtime = daemon.runtime["surface-uuid"]

            # The send must still be blocked -- that guarantee is unchanged.
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.state, "claude_input_guard")
            # But the event is now parked rather than discarded.  A terminal
            # "cancelled" left the episode with nothing to retry, which is how
            # surface:36 went sixteen minutes without a continuation on
            # 2026-08-25 and had to be continued by hand.
            self.assertEqual(
                runtime.claude_last_event_status, "deferred_Claude composer is busy",
            )
            self.assertIsNotNone(runtime.claude_deferred_event)
            self.assertEqual(runtime.claude_deferred_event["event_id"], "typing-race")

    def test_send_refuses_every_slash_command(self):
        # The watchdog may send only the two configured prose prompts. It must
        # never take over Claude slash commands, including /compact.
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        client = CmuxClient(runner=runner)
        client.send("ws", "sf", "任务请继续")
        client.send("ws", "sf", CLAUDE_MESSAGE)
        self.assertEqual(len(calls), 2)

        for forbidden in ("/compact", "/clear", "/exit", "/quit", "  /clear  ", "/CLEAR",
                          "/compact\n/clear", "/model opus"):
            with self.assertRaises(RuntimeError, msg=forbidden):
                client.send("ws", "sf", forbidden)
        self.assertEqual(len(calls), 2)

    def test_send_still_allows_prose_that_merely_mentions_a_command(self):
        # Our own nudge quotes "usage: /context", and an assistant sentence can
        # discuss /clear.  Only a message whose first character is the slash is
        # a command; refusing on substring would block the real prompt.
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        client = CmuxClient(runner=runner)
        client.send("ws", "sf", "别用 /clear，历史会没了")
        client.send("ws", "sf", "完成，建议检查 usage: /context")
        self.assertEqual(len(calls), 2)

    def test_context_parser_matches_live_footer_not_recap_prose(self):
        payload = claude_grid_payload(lines=[
            "recap: 上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
            "⎿ Context limit reached · /compact or /clear to continue",
            "✶ Compacting conversation…",
            "████░ 16%",
            "上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
            "                                                                       0% until auto-compact",
        ])
        grid = Grid.from_rpc(payload, "surface-uuid")
        telemetry = parse_claude_context_telemetry(grid, composer_kind="empty")
        self.assertEqual(telemetry.percent, 100)
        self.assertEqual(telemetry.cache_tokens, 1_100_000)
        self.assertEqual(telemetry.auto_compact_remaining_percent, 0)
        self.assertTrue(telemetry.limit_reached)
        self.assertTrue(telemetry.compacting)
        self.assertEqual(telemetry.compaction_percent, 16)

        recap_only = Grid.from_rpc(claude_grid_payload(lines=[
            "⏺ 上下文 ████████░░ 86% 读得到，但这是说明文字",
            "※ recap: 0% until auto-compact",
        ]), "surface-uuid")
        unknown = parse_claude_context_telemetry(recap_only)
        self.assertIsNone(unknown.percent)
        self.assertIsNone(unknown.auto_compact_remaining_percent)

    def test_context_warning_does_not_block_but_limit_and_stall_do(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_context_enforcement"] = True
            runtime = TargetRuntime()

            warning = classify_claude_grid(Grid.from_rpc(claude_grid_payload(lines=[
                "上下文 ████████░░ 85% (输入: 847k, 缓存: 0)",
                "15% until auto-compact",
            ]), "surface-uuid"))
            same = daemon._apply_claude_context_guard("surface-uuid", runtime, warning)
            self.assertEqual(runtime.claude_context_status, "warning")
            self.assertEqual(same.kind, warning.kind)

            waiting = classify_claude_grid(Grid.from_rpc(claude_grid_payload(lines=[
                "⎿ Context limit reached · /compact or /clear to continue",
                "上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
                "0% until auto-compact",
            ]), "surface-uuid"))
            blocked = daemon._apply_claude_context_guard("surface-uuid", runtime, waiting)
            self.assertEqual(blocked.kind, "claude_context_waiting")
            runtime.claude_context_limit_first_seen_at = time.time() - 61
            stalled = daemon._apply_claude_context_guard("surface-uuid", runtime, waiting)
            self.assertEqual(stalled.kind, "claude_context_stalled")
            self.assertFalse(daemon.config["targets"][0].get("paused", False))
            recovered = classify_claude_grid(Grid.from_rpc(claude_grid_payload(lines=[
                "上下文 ░░░░░░░░░░ 3%",
            ]), "surface-uuid"))
            daemon._apply_claude_context_guard("surface-uuid", runtime, recovered)
            self.assertEqual(runtime.claude_context_status, "normal")
            self.assertNotEqual(runtime.error_type, "claude_context_stalled")
            self.assertFalse(runtime.claude_context_notification_sent)

            runtime.error_type = "claude_context_stalled"
            parser_unknown = ScreenState(
                "recoverable_error",
                error_type="claude_api",
                message_kind="claude",
            )
            fail_open = daemon._apply_claude_context_guard(
                "surface-uuid", runtime, parser_unknown,
            )
            self.assertIs(fail_open, parser_unknown)
            self.assertEqual(runtime.claude_context_status, "unknown")
            self.assertEqual(runtime.error_type, "claude_api")

    def test_compaction_restart_does_not_reset_progress_or_absolute_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_context_enforcement"] = True
            runtime = TargetRuntime()

            def state_at(value):
                return classify_claude_grid(Grid.from_rpc(claude_grid_payload(lines=[
                    "✶ Compacting conversation…",
                    f"████░ {value}%",
                    "上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
                    "0% until auto-compact",
                ]), "surface-uuid"))

            daemon._apply_claude_context_guard("surface-uuid", runtime, state_at(16))
            progress_at = runtime.claude_compaction_last_progress_at
            daemon._apply_claude_context_guard("surface-uuid", runtime, state_at(11))
            self.assertEqual(runtime.claude_compaction_restart_count, 1)
            self.assertEqual(runtime.claude_compaction_last_progress_at, progress_at)
            runtime.claude_context_episode_started_at = time.time() - 901
            blocked = daemon._apply_claude_context_guard("surface-uuid", runtime, state_at(17))
            self.assertEqual(blocked.kind, "claude_context_stalled")

    def test_hook_auto_repair_caps_sixth_drift_and_manual_bypasses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            manager = ClaudeHookSettingsManager(
                settings,
                lock_path=root / "settings.lock",
                backup_dir=root / "backups",
                repair_state_path=root / "repair-state.json",
            )
            for index in range(5):
                settings.write_text(json.dumps({"hooks": {}, "marker": index}), encoding="utf-8")
                report = manager.ensure(repair=True, automatic=True)
                self.assertTrue(report["changed"])
            settings.write_text(json.dumps({"hooks": {}, "marker": 6}), encoding="utf-8")
            capped = manager.ensure(repair=True, automatic=True)
            self.assertEqual(capped["status"], "drift_storm")
            self.assertFalse(capped["changed"])
            self.assertLessEqual(capped["backup_count"], 20)
            manual = manager.ensure(repair=True, automatic=False)
            self.assertTrue(manual["changed"])
            self.assertEqual(manual["repair_budget_used"], 5)
            for index in range(7, 29):
                settings.write_text(json.dumps({"hooks": {}, "marker": index}), encoding="utf-8")
                manual = manager.ensure(repair=True, automatic=False)
            self.assertEqual(manual["backup_count"], 20)
            self.assertTrue(manual["persistent_drift"])

    def test_hook_drift_signature_never_records_env_or_arguments(self):
        signature = ClaudeHookSettingsManager._drift_signature({
            "hooks": {"Stop": [{"hooks": [{
                "type": "command",
                "command": "API_TOKEN=very-secret /usr/bin/python3 /tmp/hook.py --token very-secret",
            }]}]},
        })
        rendered = json.dumps(signature, sort_keys=True)
        self.assertNotIn("very-secret", rendered)
        self.assertNotIn("hook.py", rendered)
        self.assertEqual(signature["command_basenames"]["Stop"], ["python3"])

    def test_context_enforcement_cli_only_changes_the_context_gate(self):
        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = default_config()
            config.update({
                "mode": "armed",
                "global_paused": False,
                "claude_enabled": True,
                "targets": [{"surface_id": "s", "workspace_id": "w"}],
            })
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(core.cli(["--config", str(path), "context-enforce"]), 0)
            enabled = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(enabled["claude_context_enforcement"])
            self.assertEqual(enabled["targets"], config["targets"])
            self.assertEqual(core.cli(["--config", str(path), "context-observe"]), 0)
            self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["claude_context_enforcement"])

    def test_logs_subcommand_fails_loudly_when_log_dir_is_missing(self):
        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "logs-gone"
            with mock.patch.object(core, "DEFAULT_LOG_DIR", missing):
                code = core.cli(["logs"])
        # A missing log directory used to return 0 with no output, so
        # `ccc logs && echo OK` reported success while reading nothing.
        self.assertEqual(code, 4)

    def test_logs_subcommand_fails_loudly_when_log_file_is_missing(self):
        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            with mock.patch.object(core, "DEFAULT_LOG_DIR", log_dir):
                code = core.cli(["logs"])
        self.assertEqual(code, 4)

    def test_logs_subcommand_returns_zero_when_log_is_readable(self):
        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            (log_dir / "watch.log").write_text("hello\n", encoding="utf-8")
            with mock.patch.object(core, "DEFAULT_LOG_DIR", log_dir):
                self.assertEqual(core.cli(["logs"]), 0)

    def test_log_channel_records_incident_when_log_file_is_replaced(self):
        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            log_dir.mkdir()
            log_path = log_dir / "watch.log"
            log_path.write_text("first\n", encoding="utf-8")
            incidents = root / "log-channel-incidents.jsonl"
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            with mock.patch.object(core, "DEFAULT_LOG_DIR", log_dir), \
                    mock.patch.object(core, "DEFAULT_LOG_CHANNEL_INCIDENT_PATH", incidents):
                first = daemon._check_log_channel(force=True)
                self.assertTrue(first["healthy"])
                baseline_inode = first["inode"]
                # Simulate the observed production failure: the file we hold open
                # is unlinked and a different inode takes its place.
                log_path.unlink()
                log_path.write_text("second\n", encoding="utf-8")
                second = daemon._check_log_channel(force=True)
            self.assertNotEqual(second["inode"], baseline_inode)
            self.assertTrue(incidents.exists())
            record = json.loads(incidents.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["kind"], "log_file_replaced")
            self.assertEqual(record["previous_inode"], baseline_inode)

    def test_log_channel_check_is_throttled_between_intervals(self):
        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            (log_dir / "watch.log").write_text("x\n", encoding="utf-8")
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            daemon.config["log_channel_check_interval_sec"] = 3600.0
            with mock.patch.object(core, "DEFAULT_LOG_DIR", log_dir):
                daemon._check_log_channel(force=True)
                first_checked_at = daemon._log_channel_checked_at
                # Without force, a second call inside the interval must not
                # re-stat the file; the recorded timestamp stays put.
                daemon._check_log_channel()
            self.assertEqual(daemon._log_channel_checked_at, first_checked_at)

    def test_unprotected_severity_ladder_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            severity = daemon._claude_unprotected_severity
            self.assertEqual(severity(0.0), "")
            self.assertEqual(severity(3599.0), "")
            self.assertEqual(severity(3600.0), "warn")
            self.assertEqual(severity(14399.0), "warn")
            self.assertEqual(severity(14400.0), "severe")
            self.assertEqual(severity(43199.0), "severe")
            self.assertEqual(severity(43200.0), "critical")

    def test_unprotected_clock_starts_at_process_start_not_last_send(self):
        """surface:74 read 19.8h by last_send but only 14.5h of real exposure.

        last_send_at can predate the live Claude process by hours and describes a
        dead generation, so the clock must key off max(process, daemon) start.
        """

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            now = 1_000_000.0
            daemon._daemon_started_at = now - 20.0 * 3600.0
            runtime = TargetRuntime()
            runtime.claude_hook_health = "missing"
            runtime.last_send_at = now - 27.8 * 3600.0
            started_epoch = now - 14.5 * 3600.0
            daemon._refresh_claude_unprotected_clock(runtime, started_epoch, now)
            self.assertEqual(runtime.claude_hook_unprotected_since, started_epoch)
            elapsed_hours = (now - runtime.claude_hook_unprotected_since) / 3600.0
            self.assertAlmostEqual(elapsed_hours, 14.5, places=3)
            self.assertEqual(runtime.claude_hook_unprotected_severity, "critical")

    def test_unprotected_clock_floors_at_daemon_start_for_older_process(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            now = 1_000_000.0
            daemon._daemon_started_at = now - 2.0 * 3600.0
            runtime = TargetRuntime()
            runtime.claude_hook_health = "missing"
            # Process far older than the daemon: we can only claim exposure for
            # as long as we have actually been watching.
            daemon._refresh_claude_unprotected_clock(runtime, now - 50.0 * 3600.0, now)
            self.assertEqual(runtime.claude_hook_unprotected_since, daemon._daemon_started_at)
            self.assertEqual(runtime.claude_hook_unprotected_severity, "warn")

    def test_unprotected_clock_clears_when_hook_becomes_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            now = 1_000_000.0
            daemon._daemon_started_at = now - 20.0 * 3600.0
            runtime = TargetRuntime()
            runtime.claude_hook_health = "missing"
            daemon._refresh_claude_unprotected_clock(runtime, now - 14.5 * 3600.0, now)
            self.assertGreater(runtime.claude_hook_unprotected_since, 0.0)
            runtime.claude_hook_health = "healthy"
            daemon._refresh_claude_unprotected_clock(runtime, now - 14.5 * 3600.0, now)
            self.assertEqual(runtime.claude_hook_unprotected_since, 0.0)
            self.assertEqual(runtime.claude_hook_unprotected_severity, "")

    def test_unprotected_escalation_warns_once_per_severity_step(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            logger = logging.getLogger("test-unprotected")
            daemon.logger = logger
            now = 1_000_000.0
            daemon._daemon_started_at = now - 100.0 * 3600.0
            runtime = TargetRuntime()
            runtime.claude_hook_health = "missing"
            started = now - 5.0 * 3600.0
            with mock.patch.object(logger, "warning") as warn:
                daemon._refresh_claude_unprotected_clock(runtime, started, now)
                daemon._refresh_claude_unprotected_clock(runtime, started, now + 1.0)
                daemon._refresh_claude_unprotected_clock(runtime, started, now + 2.0)
                self.assertEqual(warn.call_count, 1)
                self.assertEqual(runtime.claude_hook_unprotected_severity, "severe")
                # Crossing into the next tier warns again, exactly once.
                daemon._refresh_claude_unprotected_clock(
                    runtime, started, runtime.claude_hook_unprotected_since + 43200.0,
                )
                self.assertEqual(warn.call_count, 2)
                self.assertEqual(runtime.claude_hook_unprotected_severity, "critical")

    def test_claude_submit_is_text_then_explicit_enter_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            event = claude_hook_event("submit-1", error_kind="claude_api")
            daemon._handle_claude_event(event, client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual([item[2] for item in client.sent_text], [CLAUDE_MESSAGE])
            self.assertEqual(client.sent_keys[-1][2], "enter")
            self.assertEqual(runtime.claude_submit_phase, "enter_sent")

            # A second Stop while the composer transaction is pending must not
            # write another prompt or press Enter again.
            daemon._handle_claude_event(claude_hook_event("submit-duplicate"), client)
            self.assertEqual(len(client.sent_text), 1)
            self.assertEqual(len(client.sent_keys), 1)
            self.assertEqual(runtime.claude_last_event_status, "submit_duplicate_suppressed")

            daemon._handle_claude_event(
                claude_hook_event("submit-confirm", "UserPromptSubmit", prompt_kind="watchdog"),
                client,
            )
            self.assertEqual(runtime.claude_submit_phase, "none")

    def test_claude_pending_retries_only_enter_when_exact_echo_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = self._claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", TargetRuntime())
            runtime.claude_submit_event_id = "event-1"
            runtime.claude_submit_since = time.time()
            runtime.claude_submit_last_attempt_at = time.time() - 2
            runtime.claude_submit_phase = "enter_sent"
            state = ScreenState(
                "composer_busy", message_kind="claude", watchdog_echo=True,
            )
            self.assertTrue(daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime, state, client,
            ))
            self.assertEqual(client.sent_text, [])
            self.assertEqual(client.sent_keys[-1][2], "enter")
            self.assertEqual(runtime.claude_submit_attempts, 1)

    def test_context_guard_stamps_sampled_at(self):
        """Every stored context verdict must carry the instant it was taken.

        On 2026-08-24 an audit showed a stored 81% beside a live 95% for the
        same pane and there was no way to tell the readings apart; both were
        correct for their own instant.
        """

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            state = core.ScreenState(
                "claude_stopped",
                claude_context=core.ClaudeContextTelemetry(percent=42),
            )
            before = time.time()
            daemon._apply_claude_context_guard("surface-uuid", runtime, state)
            self.assertGreaterEqual(runtime.claude_context_sampled_at, before)
            self.assertEqual(runtime.claude_context_percent, 42)

    def test_context_guard_leaves_sampled_at_untouched_when_unreadable(self):
        """Fail-open parsing must not forge a fresh timestamp.

        If telemetry is None the daemon deliberately does not gate; stamping a
        new sampled_at there would make a stale verdict look current.
        """

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            runtime.claude_context_sampled_at = 123.0
            state = core.ScreenState("claude_stopped", claude_context=None)
            daemon._apply_claude_context_guard("surface-uuid", runtime, state)
            self.assertEqual(runtime.claude_context_sampled_at, 123.0)
            self.assertEqual(runtime.claude_context_status, "unknown")

    def test_context_audit_dates_stored_and_live_readings(self):
        """The audit must say *when* each context reading was taken.

        The 5.65h observation logged 13 stored-vs-live disagreements of 6-22
        points in both directions (s104 read 8% stored beside 22% live).  Both
        numbers were right for their own instant, so the audit now labels each
        with a source and an age instead of inviting the reader to treat one as
        a parser bug.
        """

        import cmux_codex_watch as core

        surface_id = "claude-uuid"
        tree = {"windows": [{"id": "win", "ref": "window:1", "workspaces": [{
            "id": "ws", "ref": "workspace:9", "panes": [{
                "id": "pane", "ref": "pane:20", "surfaces": [
                    {"id": surface_id, "ref": "surface:104", "type": "terminal"},
                ],
            }],
        }]}]}
        top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": surface_id, "processes": [
                {"kind": "process", "name": "claude", "path": "/opt/homebrew/bin/claude", "pid": 4242, "ppid": 1},
            ]},
        ]}]}]}

        class AuditClient(FakeClient):
            def top_all(self):
                return self.top_data

        payload = claude_grid_payload(["上下文 ██░░ 22% (输入: 40k, 缓存: 10k)"])
        client = AuditClient(payload, claude_idle_screen(), tree=tree, top=top)
        stored_at = time.time() - 21600.0
        state = {surface_id: {
            "claude_context_status": "warning",
            "claude_context_percent": 8,
            "claude_context_sampled_at": stored_at,
            "claude_hook_health": "healthy",
        }}
        report = core.audit_claude_surfaces(
            {"claude_enabled": True, "targets": [], "claude_message": core.CLAUDE_MESSAGE},
            state,
            client,
            hook_config_report={"status": "healthy", "healthy": True},
        )
        row = report["live"][0]
        self.assertEqual(row["context"]["source"], "state")
        self.assertEqual(row["context"]["percent"], 8)
        # ~6h old: the gap is attributable to age, not to a broken parser.
        self.assertGreater(row["context"]["age_sec"], 21000.0)
        self.assertTrue(row["context"]["sampled_at"].endswith("Z"))
        self.assertEqual(row["live_context"]["source"], "fresh_replay")
        self.assertEqual(row["live_context"]["age_sec"], 0.0)
        self.assertEqual(row["live_context"]["percent"], 22)

    def test_context_audit_omits_age_when_never_sampled(self):
        """A surface with no stored reading must not report a fake age."""

        import cmux_codex_watch as core

        surface_id = "claude-uuid"
        tree = {"windows": [{"id": "win", "ref": "window:1", "workspaces": [{
            "id": "ws", "ref": "workspace:9", "panes": [{
                "id": "pane", "ref": "pane:20", "surfaces": [
                    {"id": surface_id, "ref": "surface:104", "type": "terminal"},
                ],
            }],
        }]}]}
        top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": surface_id, "processes": [
                {"kind": "process", "name": "claude", "path": "/opt/homebrew/bin/claude", "pid": 4242, "ppid": 1},
            ]},
        ]}]}]}

        class AuditClient(FakeClient):
            def top_all(self):
                return self.top_data

        client = AuditClient(claude_grid_payload([]), claude_idle_screen(), tree=tree, top=top)
        report = core.audit_claude_surfaces(
            {"claude_enabled": True, "targets": [], "claude_message": core.CLAUDE_MESSAGE},
            {},
            client,
            hook_config_report={"status": "healthy", "healthy": True},
        )
        row = report["live"][0]
        self.assertIsNone(row["context"]["age_sec"])
        self.assertEqual(row["context"]["sampled_at"], "")

    def test_slow_compaction_still_hits_absolute_timeout(self):
        """Crawling progress must not buy unlimited time.

        Advancing 1% every 170s keeps refreshing ``last_progress_at`` and so
        never trips the 180s no-progress rule.  Only the episode-wide 900s
        ceiling stops it.  The 5.65h production observation never reached this
        path (one compaction, cleared in ~3 minutes), so this test is the only
        proof the ceiling works.
        """

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_context_enforcement"] = True
            runtime = TargetRuntime()

            def state_at(value):
                return classify_claude_grid(Grid.from_rpc(claude_grid_payload(lines=[
                    "✶ Compacting conversation…",
                    f"████░ {value}%",
                    "上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
                    "0% until auto-compact",
                ]), "surface-uuid"))

            start = time.time()
            # t=0 at 10%: episode opens.
            with mock.patch.object(time, "time", return_value=start):
                first = daemon._apply_claude_context_guard("surface-uuid", runtime, state_at(10))
            self.assertEqual(first.kind, "claude_context_compacting")

            # 1% every 170s: always inside the 180s no-progress window.
            for offset, percent in ((170.0, 11), (340.0, 12), (510.0, 13), (680.0, 14)):
                with mock.patch.object(time, "time", return_value=start + offset):
                    state = daemon._apply_claude_context_guard(
                        "surface-uuid", runtime, state_at(percent),
                    )
                self.assertEqual(
                    state.kind, "claude_context_compacting",
                    f"offset={offset} must still be merely compacting",
                )

            # t=901s, still compacting and still making that crawling progress:
            # the absolute ceiling has to win.
            with mock.patch.object(time, "time", return_value=start + 901.0):
                stalled = daemon._apply_claude_context_guard(
                    "surface-uuid", runtime, state_at(15),
                )
            self.assertEqual(stalled.kind, "claude_context_stalled")
            self.assertEqual(runtime.claude_context_status, "stalled")
            # Crawling progress means the no-progress rule was never the cause.
            self.assertEqual(runtime.claude_compaction_restart_count, 0)
            self.assertEqual(runtime.claude_compaction_highest_percent, 15)

    def test_slow_compaction_survives_a_raised_absolute_timeout(self):
        """Reverse control: the ceiling is what stalls the crawl, nothing else.

        With the timeout raised past the elapsed episode, the same crawl must
        stay ``compacting``.  Without this, the test above could pass for the
        wrong reason and still look green.
        """

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_context_enforcement"] = True
            daemon.config["claude_context_absolute_timeout_sec"] = 100_000.0
            runtime = TargetRuntime()

            def state_at(value):
                return classify_claude_grid(Grid.from_rpc(claude_grid_payload(lines=[
                    "✶ Compacting conversation…",
                    f"████░ {value}%",
                    "上下文 ██████ 100% (输入: 0, 缓存: 1.1M)",
                    "0% until auto-compact",
                ]), "surface-uuid"))

            start = time.time()
            with mock.patch.object(time, "time", return_value=start):
                daemon._apply_claude_context_guard("surface-uuid", runtime, state_at(10))
            with mock.patch.object(time, "time", return_value=start + 901.0):
                state = daemon._apply_claude_context_guard("surface-uuid", runtime, state_at(11))
            self.assertEqual(state.kind, "claude_context_compacting")

    def test_send_log_breaks_preflight_into_named_stages(self):
        """A slow send must name its own bottleneck.

        On 2026-08-24 five SLA misses were 93-99% preflight with queue~=0, but
        the log only said "preflight was slow".  That is not actionable: the fix
        for a slow tree() is different from the fix for a slow replay(), and the
        preflight inherently runs ~6 cmux RPCs, so "slow" alone is not even
        evidence of a regression.
        """

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            with mock.patch.object(daemon.logger, "info") as info:
                daemon._handle_claude_event(
                    claude_hook_event("stage-1", error_kind="claude_api"), client
                )
            sent = [call for call in info.call_args_list
                    if "sent=claude_hook" in str(call.args[0])]
            self.assertTrue(sent, "no send line was logged")
            rendered = sent[-1].args[0] % sent[-1].args[1:]
            for stage in ("process_ms=", "frame1_ms=", "settle_ms=",
                          "frame2_ms=", "guard_ms=", "verify_ms="):
                self.assertIn(stage, rendered)
            self.assertIn("slow_preflight=", rendered)

    def test_send_log_names_slowest_stage_when_preflight_eats_the_budget(self):
        """When preflight burns half the SLA the slowest stage is named."""

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            # A tiny SLA makes any real preflight exceed half the budget, so the
            # attribution branch runs without an artificial sleep.
            daemon.config["claude_hook_sla_sec"] = 0.0001
            with mock.patch.object(daemon.logger, "warning") as warn, \
                    mock.patch.object(daemon.logger, "info") as info:
                daemon._handle_claude_event(
                    claude_hook_event("stage-2", error_kind="claude_api"), client
                )
            calls = [call for call in list(warn.call_args_list) + list(info.call_args_list)
                     if "sent=claude_hook" in str(call.args[0])]
            self.assertTrue(calls, "no send line was logged")
            rendered = calls[-1].args[0] % calls[-1].args[1:]
            self.assertIn("slow_preflight=true", rendered)
            # The named stage must be one of the real stages, not empty.
            named = rendered.split("slow_stage=")[1].split(" ")[0]
            self.assertIn(named, {"process_ms", "frame1_ms", "settle_ms",
                                  "frame2_ms", "guard_ms", "verify_ms"})

    def test_viewport_unreadable_clock_warns_once_per_threshold(self):
        """A parser blind spot must not stay silent.

        During the 2026-08-24 observation surface:72 was ``incompatible`` for
        the full 5.65 hours and produced no daemon-side warning at all, because
        the verdict was only ever instantaneous.
        """

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = TargetRuntime()
            with mock.patch.object(daemon.logger, "warning") as warn:
                daemon._refresh_claude_unreadable_clock("s", runtime, "incompatible")
                # First blind tick only starts the clock.
                self.assertGreater(runtime.claude_unreadable_since, 0.0)
                self.assertEqual(warn.call_count, 0)

                # Still inside the window: no warning yet.
                daemon._refresh_claude_unreadable_clock("s", runtime, "incompatible")
                self.assertEqual(warn.call_count, 0)

                # Past the threshold the exposure is stated once...
                runtime.claude_unreadable_since = time.time() - 1801.0
                daemon._refresh_claude_unreadable_clock("s", runtime, "incompatible")
                self.assertEqual(warn.call_count, 1)
                # ...and then throttled, so an hours-long blind pane cannot
                # emit one line per poll and bury every other signal.
                daemon._refresh_claude_unreadable_clock("s", runtime, "incompatible")
                self.assertEqual(warn.call_count, 1)

    def test_viewport_unreadable_clock_clears_when_pane_becomes_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = TargetRuntime()
            runtime.claude_unreadable_since = time.time() - 3600.0
            runtime.claude_unreadable_warned_at = time.time() - 10.0
            daemon._refresh_claude_unreadable_clock("s", runtime, "claude_stopped")
            self.assertEqual(runtime.claude_unreadable_since, 0.0)
            self.assertEqual(runtime.claude_unreadable_warned_at, 0.0)

    def test_viewport_unreadable_clock_counts_replay_failures_too(self):
        """``unreadable:CmuxError`` is the same blind spot as ``incompatible``."""

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = TargetRuntime()
            daemon._refresh_claude_unreadable_clock("s", runtime, "unreadable:CmuxError")
            self.assertGreater(runtime.claude_unreadable_since, 0.0)

    def test_new_claude_generation_resets_the_unprotected_clock(self):
        """A new Claude process must not inherit the old one's exposure.

        The clock only seeds when the stored value is 0, so without an explicit
        reset on generation change a process launched seconds ago reported the
        previous generation's hours and kept its escalated severity -- the exact
        cross-generation mixing this clock exists to prevent.
        """

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            now = time.time()
            daemon._daemon_started_at = now - 20.0 * 3600.0

            # Generation A: an old process that has been missing for 18h.
            daemon._apply_claude_process_observation(runtime, {
                "pid": 111,
                "generation": "gen-A",
                "started_at": "2026-08-24T06:00:00",
                "started_epoch": now - 18.0 * 3600.0,
                "verified": True,
            })
            self.assertEqual(runtime.claude_hook_health, "unverified")
            first_since = runtime.claude_hook_unprotected_since
            self.assertGreater(first_since, 0.0)
            # Force the escalated state the old generation would have reached.
            runtime.claude_hook_unprotected_severity = "critical"

            # Generation B: a brand-new process, still without a Hook.
            daemon._apply_claude_process_observation(runtime, {
                "pid": 222,
                "generation": "gen-B",
                "started_at": "2026-08-25T08:00:00",
                "started_epoch": now - 10.0,
                "verified": True,
            })
            self.assertEqual(runtime.claude_process_generation, "gen-B")
            self.assertNotEqual(runtime.claude_hook_unprotected_since, first_since)
            # The new clock starts at the new process, so exposure is seconds.
            self.assertLess(now - runtime.claude_hook_unprotected_since, 60.0)
            self.assertEqual(runtime.claude_hook_unprotected_severity, "")

    def test_same_generation_keeps_accumulating_unprotected_time(self):
        """Reset is per generation, not per poll: a stable process keeps its clock."""

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), "")
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            now = time.time()
            daemon._daemon_started_at = now - 10.0 * 3600.0
            observation = {
                "pid": 111,
                "generation": "gen-A",
                "started_at": "2026-08-25T00:00:00",
                "started_epoch": now - 5.0 * 3600.0,
                "verified": True,
            }
            daemon._apply_claude_process_observation(runtime, observation)
            first_since = runtime.claude_hook_unprotected_since
            daemon._apply_claude_process_observation(runtime, dict(observation))
            self.assertEqual(runtime.claude_hook_unprotected_since, first_since)

    def test_launchctl_start_creates_the_log_dir_before_bootstrap(self):
        """launchd opens the plist log paths before it execs us.

        Python's mkdir inside run() cannot repair StandardOutPath /
        StandardErrorPath: launchd already opened (or failed to open) them.  So
        the directory has to exist before bootstrap, on the start path too --
        not just on install.
        """

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            plist = Path(directory) / "agent.plist"
            plist.write_text("<plist/>", encoding="utf-8")
            calls = []

            def fake_launchctl(args, check=True):
                # Record whether the directory existed at each launchctl step.
                calls.append((tuple(args), log_dir.exists()))

            with mock.patch.object(core, "DEFAULT_LOG_DIR", log_dir), \
                    mock.patch.object(core, "DEFAULT_PLIST_PATH", plist), \
                    mock.patch.object(core, "_run_launchctl", fake_launchctl):
                self.assertFalse(log_dir.exists())
                core.launchctl("start")

            self.assertTrue(log_dir.exists())
            self.assertTrue(calls, "launchctl was never invoked")
            # Every launchctl step must have seen the directory already present.
            for args, existed in calls:
                self.assertTrue(existed, f"log dir missing during {args}")

    def test_launchctl_stop_does_not_create_the_log_dir(self):
        """stop/uninstall must not resurrect a directory the user removed."""

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            with mock.patch.object(core, "DEFAULT_LOG_DIR", log_dir), \
                    mock.patch.object(core, "_run_launchctl", lambda *a, **k: None):
                core.launchctl("stop")
            self.assertFalse(log_dir.exists())

    def test_preflight_timing_excludes_the_submit_transaction(self):
        """``preflight=`` must not swallow the submit half.

        The first version of this instrumentation measured "preflight" from
        entry all the way to the log call, so it silently included send_text,
        the readback, the explicit Enter and three fsync'ing save() calls --
        four cmux RPCs of unrelated work.  That made the number useless as
        evidence about preflight cost, which was its only purpose, and it also
        invalidated any argument about whether the 1.0s SLA is generous enough.
        """

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            # Make only the submit half expensive.  If the split is correct the
            # cost lands in submit= and leaves preflight= untouched.
            inner = client.send_text

            def slow_send_text(workspace_id, surface_id, message):
                time.sleep(0.30)
                return inner(workspace_id, surface_id, message)

            client.send_text = slow_send_text
            with mock.patch.object(daemon.logger, "info") as info, \
                    mock.patch.object(daemon.logger, "warning") as warn:
                daemon._handle_claude_event(
                    claude_hook_event("split-1", error_kind="claude_api"), client
                )
            calls = [call for call in list(warn.call_args_list) + list(info.call_args_list)
                     if "sent=claude_hook" in str(call.args[0])]
            self.assertTrue(calls, "no send line was logged")
            rendered = calls[-1].args[0] % calls[-1].args[1:]
            preflight = float(rendered.split("preflight=")[1].split("s")[0])
            submit = float(rendered.split("submit=")[1].split("s")[0])
            self.assertLess(preflight, 0.25, f"submit cost leaked into preflight: {rendered}")
            self.assertGreaterEqual(submit, 0.28, f"submit cost not measured: {rendered}")
            self.assertIn("text_ms=", rendered)

    def test_send_log_reports_submit_stages_separately(self):
        """The submit transaction gets its own named stages."""

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            with mock.patch.object(daemon.logger, "info") as info:
                daemon._handle_claude_event(
                    claude_hook_event("split-2", error_kind="claude_api"), client
                )
            sent = [call for call in info.call_args_list
                    if "sent=claude_hook" in str(call.args[0])]
            self.assertTrue(sent, "no send line was logged")
            rendered = sent[-1].args[0] % sent[-1].args[1:]
            for stage in ("text_ms=", "readback_ms=", "persist_ms=", "enter_ms="):
                self.assertIn(stage, rendered)

    def test_slow_stage_is_never_a_submit_stage(self):
        """Attribution points at preflight only.

        The submit stages run after the send decision is already made, so
        naming one of them as the bottleneck would misdirect the fix.
        """

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = self._claude_armed_daemon(directory, client)
            daemon.config["claude_hook_sla_sec"] = 0.0001
            inner = client.send_text

            def slow_send_text(workspace_id, surface_id, message):
                time.sleep(0.30)
                return inner(workspace_id, surface_id, message)

            client.send_text = slow_send_text
            with mock.patch.object(daemon.logger, "warning") as warn, \
                    mock.patch.object(daemon.logger, "info") as info:
                daemon._handle_claude_event(
                    claude_hook_event("split-3", error_kind="claude_api"), client
                )
            calls = [call for call in list(warn.call_args_list) + list(info.call_args_list)
                     if "sent=claude_hook" in str(call.args[0])]
            rendered = calls[-1].args[0] % calls[-1].args[1:]
            named = rendered.split("slow_stage=")[1].split(" ")[0]
            self.assertNotIn(named, {"text_ms", "readback_ms", "persist_ms", "enter_ms"})
            self.assertIn(named, {"process_ms", "frame1_ms", "settle_ms",
                                  "frame2_ms", "guard_ms", "verify_ms"})

    def test_incompatible_error_path_feeds_the_blind_clock(self):
        """A raised IncompatibleError is the same blind spot as a bad grid.

        The clock used to be wired only into the success path, so a pane whose
        replay *threw* went straight to pause without ever being timed -- the
        exact case that made surface:72 invisible for 5.65 hours.
        """

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            # Both handler-supplied kinds must start the clock.
            for kind in ("unreadable:initial_read", "unreadable:retry_incompatible"):
                runtime.claude_unreadable_since = 0.0
                daemon._refresh_claude_unreadable_clock("s", runtime, kind)
                self.assertGreater(
                    runtime.claude_unreadable_since, 0.0,
                    f"{kind} did not start the blind clock",
                )

    def test_socket_unavailable_is_not_a_parser_blind_spot(self):
        """cmux being down is infrastructure, not a rendering blind spot.

        Counting it would turn every cmux restart into a fake "viewport
        unreadable" escalation and bury the real parser cases.
        """

        import cmux_codex_watch as core

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), claude_idle_screen())
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            daemon._refresh_claude_unreadable_clock("s", runtime, "cmux_unavailable")
            self.assertEqual(runtime.claude_unreadable_since, 0.0)

    def test_blind_clock_is_never_called_from_a_socket_down_branch(self):
        """Structural guard: assert the wiring, not just the helper.

        A future edit could move the clock call into the ping()-failed branch
        and every behavioural test above would still pass, so pin the shape of
        the observation worker itself.
        """

        import ast
        import inspect
        import cmux_codex_watch as core

        source = inspect.getsource(core.WatchDaemon._process_one_target)
        tree = ast.parse("\n".join(
            line[4:] if line.startswith("    ") else line
            for line in source.splitlines()
        ))

        def calls_clock(node):
            return any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "_refresh_claude_unreadable_clock"
                for inner in ast.walk(node)
            )

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            # `if not client.ping():` -- the socket-down branch.
            if "ping" not in ast.dump(node.test):
                continue
            for stmt in node.body:
                if calls_clock(stmt):
                    offenders.append(getattr(stmt, "lineno", -1))
        self.assertEqual(
            offenders, [],
            "the blind clock must not run inside a socket-unavailable branch",
        )



class DaemonRuntimeIdentityTests(unittest.TestCase):
    """Stage 0: a running daemon must be able to prove which code it loaded."""

    def test_identity_file_records_source_sha_and_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon._write_daemon_runtime()
            record = json.loads(daemon.daemon_runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], os.getpid())
            self.assertEqual(len(record["source_sha256"]), 64)
            self.assertEqual(record["feature_revision"], core.FEATURE_REVISION)
            self.assertEqual(record["source_sha256"], core.file_sha256(Path(core.__file__)))

    def test_identity_is_not_written_into_state_json(self):
        # state.json loads every top-level key as a surface runtime, so daemon
        # metadata there would materialise a phantom target.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon._write_daemon_runtime()
            daemon.save()
            state = json.loads(daemon.state_path.read_text(encoding="utf-8"))
            self.assertNotIn("daemon", state)
            self.assertNotEqual(daemon.daemon_runtime_path, daemon.state_path)

    def test_describe_reports_mismatch_when_disk_is_newer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daemon-runtime.json"
            path.write_text(json.dumps({
                "pid": os.getpid(),
                "source_path": str(Path(core.__file__)),
                "source_sha256": "0" * 64,
            }), encoding="utf-8")
            described = core.describe_daemon_runtime(path)
            self.assertFalse(described["source_matches_disk"])
            self.assertTrue(described["pid_alive"])
            self.assertEqual(described["disk_source_sha256"], core.file_sha256(Path(core.__file__)))

    def test_run_publishes_identity_before_entering_the_poll_loop(self):
        """The whole point is production observability, so run() must publish.

        Calling _write_daemon_runtime() directly in a test proves the writer
        works but not that anything ever invokes it.  Deleting the call from
        run() would leave every assertion in this class passing while the
        running daemon stayed anonymous -- the exact condition stage 0 exists
        to end.
        """

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen(), ping_ok=False)
            daemon = armed_daemon(directory, client)
            daemon.stop_requested = True
            with mock.patch("cmux_codex_watch.DEFAULT_LOG_DIR", Path(directory) / "logs"):
                daemon.run()
            self.assertTrue(
                daemon.daemon_runtime_path.exists(),
                "run() must publish daemon identity",
            )
            record = json.loads(daemon.daemon_runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(record["source_sha256"], core.file_sha256(Path(core.__file__)))

    def test_describe_marks_absent_metadata_rather_than_claiming_match(self):
        with tempfile.TemporaryDirectory() as directory:
            described = core.describe_daemon_runtime(Path(directory) / "missing.json")
            self.assertFalse(described["runtime_metadata_present"])
            self.assertFalse(described["source_matches_disk"])


class WatchdogEchoAttributionTests(unittest.TestCase):
    """Stage 1: text equality cannot decide who typed the prompt."""

    def _daemon(self, directory):
        client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
        daemon = armed_daemon(directory, client)
        daemon.config["claude_enabled"] = True
        return daemon, client

    def test_truncated_digests_compare_by_common_prefix(self):
        # protocol._digest keeps 24 hex chars, _short_hash keeps 16.  A plain
        # equality between them is always False, which would classify every one
        # of our own echoes as a human prompt and re-arm sending.
        message = protocol.DEFAULT_CLAUDE_MESSAGE
        long_digest = protocol._digest(message)
        short_digest = core._short_hash(message)
        self.assertNotEqual(long_digest, short_digest)
        self.assertTrue(core.WatchDaemon._hashes_agree(short_digest, long_digest))
        self.assertTrue(core.WatchDaemon._hashes_agree(long_digest, short_digest))
        self.assertFalse(core.WatchDaemon._hashes_agree(short_digest, "f" * 24))
        self.assertFalse(core.WatchDaemon._hashes_agree("", long_digest))

    def test_exact_text_without_submit_record_is_human(self):
        # The surface:36 lockout: a completion latch plus a verbatim paste kept
        # the latch set forever, so continuation never resumed.
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_completed_latched = True
            runtime.claude_session_id = "session-uuid"
            runtime.send_count = 30
            event = claude_hook_event("e-human", "UserPromptSubmit", prompt_kind="watchdog")
            self.assertEqual(daemon._attribute_exact_prompt(runtime, event), "human_exact_prompt")
            daemon._handle_claude_event(event, daemon.client)
            self.assertFalse(runtime.claude_completed_latched)
            self.assertEqual(runtime.send_count, 0)

    def test_correlated_echo_keeps_latch_and_does_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            message = str(daemon.config.get("claude_message") or core.CLAUDE_MESSAGE)
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_at = time.time() - 5.0
            runtime.send_count = 7
            event = claude_hook_event(
                "e-echo", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
            )
            self.assertEqual(
                daemon._attribute_exact_prompt(runtime, event), "watchdog_echo_correlated",
            )
            daemon._handle_claude_event(event, daemon.client)
            self.assertEqual(runtime.send_count, 7)

    def test_echo_outside_window_is_treated_as_human(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            message = str(daemon.config.get("claude_message") or core.CLAUDE_MESSAGE)
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_at = time.time() - 120.0
            event = claude_hook_event(
                "e-late", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
            )
            self.assertEqual(daemon._attribute_exact_prompt(runtime, event), "human_exact_prompt")

    def test_echo_window_default_is_thirty_seconds(self):
        self.assertEqual(core.CLAUDE_WATCHDOG_ECHO_WINDOW_SEC, 30.0)
        # Must exceed the submit confirmation timeout, or an echo arriving after
        # the transaction expires would be read as a human prompt.
        self.assertGreater(
            core.CLAUDE_WATCHDOG_ECHO_WINDOW_SEC, core.CLAUDE_SUBMIT_CONFIRM_TIMEOUT_SEC,
        )

    def test_human_paste_during_a_pending_transaction_is_still_human(self):
        """The *other* attribution call site: a submit transaction is pending.

        _attribute_exact_prompt is consulted from two places -- with a pending
        submit transaction and without one.  The no-transaction branch is the one
        that stranded surface:36, but the pending branch decides the same
        question and must not fall back to text equality either: a human who
        pastes the watchdog sentence while our own prompt is still unconfirmed
        owns the turn, and the latch must clear.
        """

        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_completed_latched = True
            runtime.send_count = 30
            # A transaction is pending, but no correlation record exists, so the
            # byte-identical prompt cannot be our echo.
            runtime.claude_submit_phase = "text_written"
            runtime.claude_submit_event_id = "e-pending"
            event = claude_hook_event(
                "e-human-pending", "UserPromptSubmit", prompt_kind="watchdog",
            )
            daemon._handle_claude_event(event, daemon.client)
            self.assertEqual(runtime.claude_last_prompt_attribution, "human_exact_prompt")
            self.assertFalse(runtime.claude_completed_latched)
            self.assertEqual(runtime.send_count, 0)
            self.assertEqual(runtime.claude_submit_phase, "none")

    def test_correlated_echo_during_a_pending_transaction_confirms_it(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            message = str(daemon.config.get("claude_message") or core.CLAUDE_MESSAGE)
            runtime.claude_session_id = "session-uuid"
            runtime.claude_submit_phase = "enter_sent"
            runtime.claude_submit_event_id = "e-pending"
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_at = time.time() - 4.0
            runtime.send_count = 11
            event = claude_hook_event(
                "e-echo-pending", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
            )
            daemon._handle_claude_event(event, daemon.client)
            self.assertEqual(
                runtime.claude_last_prompt_attribution, "watchdog_echo_correlated",
            )
            # Our own echo confirms the transaction; it must not reset the turn.
            self.assertEqual(runtime.send_count, 11)
            self.assertEqual(runtime.claude_submit_phase, "none")
            self.assertEqual(runtime.claude_last_event_status, "watchdog_confirmed")

    def test_echo_arriving_just_before_our_anchor_is_not_human(self):
        """The 2026-08-25 22:01:16 production regression, reproduced.

        Claude Code stamps ``created_at`` on UserPromptSubmit the moment Enter
        lands; the daemon used to stamp its correlation anchor *after* the key,
        past ``enter_ms`` (40-87ms live) plus a ledger write and an fsync.  Our
        own echo therefore arrived with a *negative* age, which the first cut
        read as "human": it cleared the completion latch and reset send_count on
        our own echo (live: count 3 -> 1).  A small negative age is clock noise,
        never evidence of a human.
        """

        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            message = str(daemon.config.get("claude_message") or core.CLAUDE_MESSAGE)
            now = time.time()
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_at = now
            runtime.claude_completed_latched = True
            runtime.send_count = 3
            # Exactly the observed live delta.
            event = claude_hook_event(
                "e-neg", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
                created_at=now - 0.013,
            )
            self.assertNotEqual(
                daemon._attribute_exact_prompt(runtime, event), "human_exact_prompt",
            )
            daemon._handle_claude_event(event, daemon.client)
            # The latch and the counter belong to the turn we are continuing.
            self.assertTrue(runtime.claude_completed_latched)
            self.assertEqual(runtime.send_count, 3)

    def test_echo_far_before_our_anchor_is_still_human(self):
        # The tolerance absorbs clock noise, not causality: a prompt from well
        # before our send cannot be our echo.
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            message = str(daemon.config.get("claude_message") or core.CLAUDE_MESSAGE)
            now = time.time()
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_at = now
            event = claude_hook_event(
                "e-far-neg", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
                created_at=now - (core.CLAUDE_ECHO_CLOCK_TOLERANCE_SEC + 1.0),
            )
            self.assertEqual(
                daemon._attribute_exact_prompt(runtime, event), "human_exact_prompt",
            )

    def test_clock_tolerance_covers_the_measured_post_enter_bookkeeping(self):
        # Live segments on 2026-08-25: enter_ms up to 86.9, then ledger_finalize
        # 43.9 and persist_finalize 11.3.  The tolerance has to dominate that
        # whole tail with room to spare, or the regression returns under load.
        self.assertGreaterEqual(core.CLAUDE_ECHO_CLOCK_TOLERANCE_SEC, 1.0)
        self.assertLess(
            core.CLAUDE_ECHO_CLOCK_TOLERANCE_SEC, core.CLAUDE_WATCHDOG_ECHO_WINDOW_SEC,
        )

    def test_anchor_is_stamped_before_enter_and_rolled_back_on_failure(self):
        """Ordering is the real fix; the tolerance is only a backstop.

        Assert the anchor exists *while* send_key runs -- that is what makes a
        genuine echo strictly later than the record of the send that caused it.
        """

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            observed = {}
            real_send_key = client.send_key

            def watch_enter(workspace_id, surface_id, key):
                observed["anchor_at"] = runtime.claude_last_submit_at
                observed["anchor_event"] = runtime.claude_last_submit_event_id
                return real_send_key(workspace_id, surface_id, key)

            client.send_key = watch_enter
            event = claude_hook_event("e-order", "Stop")
            sent, _ = daemon._send_claude_event(event, target, runtime, client)
            self.assertTrue(sent)
            self.assertGreater(observed["anchor_at"], 0.0)
            self.assertEqual(observed["anchor_event"], "e-order")
            # And the surviving anchor is the pre-Enter one, not a later re-stamp.
            self.assertEqual(runtime.claude_last_submit_at, observed["anchor_at"])

    def test_failed_enter_restores_the_previous_anchor(self):
        # Nothing was submitted, so nothing can echo: a stale anchor would make
        # the *next* human paste look like our echo and suppress a real rescue.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            runtime.claude_last_submit_event_id = "e-older"
            runtime.claude_last_submit_at = 1234.5
            runtime.claude_last_submit_message_hash = "olderhash"

            def refuse_enter(workspace_id, surface_id, key):
                raise CmuxError("send-key refused")

            client.send_key = refuse_enter
            event = claude_hook_event("e-order-fail", "Stop")
            sent, _ = daemon._send_claude_event(event, target, runtime, client)
            self.assertFalse(sent)
            self.assertEqual(runtime.claude_last_submit_event_id, "e-older")
            self.assertEqual(runtime.claude_last_submit_at, 1234.5)
            self.assertEqual(runtime.claude_last_submit_message_hash, "olderhash")

    def test_echo_from_a_different_generation_is_human(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self._daemon(directory)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            message = str(daemon.config.get("claude_message") or core.CLAUDE_MESSAGE)
            runtime.claude_last_submit_message_hash = core._short_hash(message)
            runtime.claude_last_submit_session_id = "session-uuid"
            runtime.claude_last_submit_generation = "gen-old"
            runtime.claude_process_generation = "gen-new"
            runtime.claude_last_submit_at = time.time() - 3.0
            event = claude_hook_event(
                "e-gen", "UserPromptSubmit", prompt_kind="watchdog",
                message_hash=protocol._digest(message),
            )
            self.assertEqual(daemon._attribute_exact_prompt(runtime, event), "human_exact_prompt")


class PromptAttributionForensicsTests(unittest.TestCase):
    """Attribution belongs to one event and must not leak to the next.

    ``_apply_human_prompt_reset`` used to read
    ``runtime.claude_last_prompt_attribution``, which only the exact-prompt
    paths ever write.  An ordinary human prompt therefore inherited the previous
    event's verdict: 24 of surface:36's 25 post-deploy ``human_prompt`` ledger
    rows carried ``ambiguous_exact_prompt``, and one carried
    ``human_exact_prompt`` -- which also fired the "matched watchdog text" log
    line for a prompt whose text never matched it.
    """

    def _daemon(self, directory):
        client = FakeClient(
            claude_grid_payload(), text=claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        daemon = claude_armed_daemon(directory, client)
        runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
        runtime.claude_session_id = "session-uuid"
        runtime.claude_hook_health = "healthy"
        return daemon, client, runtime

    def test_plain_human_prompt_does_not_inherit_an_exact_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            # 1. An exact prompt records its own verdict.
            runtime.claude_completed_latched = True
            runtime.send_count = 9
            daemon._handle_claude_event(
                claude_hook_event("e-exact", "UserPromptSubmit", prompt_kind="watchdog"),
                client,
            )
            self.assertEqual(
                daemon.claude_event_ledger.events["e-exact"]["detail"],
                "human_exact_prompt",
            )
            # 2. A differently worded human prompt must not carry it forward.
            runtime.claude_completed_latched = True
            runtime.send_count = 5
            daemon._handle_claude_event(
                claude_hook_event("e-human", "UserPromptSubmit", prompt_kind="human"),
                client,
            )
            row = daemon.claude_event_ledger.events["e-human"]
            self.assertEqual(row["status"], "human_prompt")
            self.assertEqual(row["detail"], "human_prompt")
            self.assertEqual(runtime.claude_last_prompt_attribution, "human_prompt")
            # The recovery itself must be unchanged by the relabelling.
            self.assertFalse(runtime.claude_completed_latched)
            self.assertEqual(runtime.send_count, 0)

    def test_plain_human_prompt_does_not_claim_it_matched_watchdog_text(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            runtime.claude_completed_latched = True
            daemon._handle_claude_event(
                claude_hook_event("e-exact2", "UserPromptSubmit", prompt_kind="watchdog"),
                client,
            )
            runtime.claude_last_submit_at = time.time() - 3.0
            runtime.claude_completed_latched = True
            # ``assertLogs`` *requires* at least one record, but the assertion
            # here is that a specific line is absent -- and the fixed code may
            # legitimately log nothing at all.  Capture with a handler instead,
            # or a passing fix would fail the test for the wrong reason.
            records: list[str] = []

            class _Capture(logging.Handler):
                def emit(self, record):
                    # getMessage() already applies ``msg % args``; formatting a
                    # second time would raise or mangle the text.
                    records.append(record.getMessage())

            handler = _Capture()
            daemon.logger.setLevel(logging.INFO)
            daemon.logger.addHandler(handler)
            try:
                daemon._handle_claude_event(
                    claude_hook_event("e-human2", "UserPromptSubmit", prompt_kind="human"),
                    client,
                )
            finally:
                daemon.logger.removeHandler(handler)
            self.assertNotIn("matched watchdog text", "\n".join(records))

    def test_exact_prompt_path_still_reports_its_measured_verdict(self):
        # The fix must not silence the real case: an uncorrelated byte-identical
        # prompt is the surface:36 lockout and has to stay labelled as such.
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            runtime.claude_completed_latched = True
            runtime.send_count = 30
            daemon.logger.setLevel(logging.INFO)
            with self.assertLogs(daemon.logger, level="INFO") as captured:
                runtime.claude_last_submit_at = time.time() - 600.0
                daemon._handle_claude_event(
                    claude_hook_event("e-lock", "UserPromptSubmit", prompt_kind="watchdog"),
                    client,
                )
            row = daemon.claude_event_ledger.events["e-lock"]
            self.assertEqual(row["status"], "human_prompt")
            self.assertEqual(row["detail"], "human_exact_prompt")
            self.assertIn("matched watchdog text", "\n".join(captured.output))


class DeferredTerminalStatusTests(unittest.TestCase):
    """A released slot must say where the parked Stop went.

    Releasing was silent and left the ledger at ``deferred_<reason>`` forever, so
    "parked, then nothing" -- indistinguishable from a *lost* episode, the very
    bug being fixed -- looked identical to "parked, then correctly cancelled".
    Production had 5 such rows and zero release log lines.
    """

    def _daemon(self, directory, client=None):
        client = client or FakeClient(
            claude_grid_payload(), text=claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        daemon = claude_armed_daemon(directory, client)
        runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
        runtime.claude_session_id = "session-uuid"
        runtime.claude_hook_health = "healthy"
        return daemon, client, runtime

    def test_status_of_is_empty_for_an_unknown_id(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _, _ = self._daemon(directory)
            self.assertEqual(daemon.claude_event_ledger.status_of("no-such-id"), "")

    def test_human_prompt_release_reaches_a_terminal_status_and_logs(self):
        # The real 2026-08-25 23:09:18 chain: a Stop was parked for a working
        # viewport, then the user took the turn back 186ms later.
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                claude_hook_event("e-parked", "Stop"), "Claude viewport is working",
            )
            self.assertEqual(
                daemon.claude_event_ledger.events["e-parked"]["status"],
                "deferred_Claude viewport is working",
            )
            daemon.logger.setLevel(logging.INFO)
            with self.assertLogs(daemon.logger, level="INFO") as captured:
                daemon._handle_claude_event(
                    claude_hook_event("e-took-over", "UserPromptSubmit", prompt_kind="human"),
                    client,
                )
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(
                daemon.claude_event_ledger.events["e-parked"]["status"], "deferred_cleared",
            )
            joined = "\n".join(captured.output)
            self.assertIn("deferred stop released", joined)
            self.assertIn("reason=human_prompt", joined)
            self.assertIn("final_status=deferred_cleared", joined)

    def test_expired_ledger_row_is_rehydrated_for_retry(self):
        """A daemon restart must not strand an already-expired Stop forever."""
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            event = claude_hook_event("e-expired", "Stop")
            daemon.claude_event_ledger.mark(event, "deferred_expired", detail="old window")
            runtime.claude_last_event_id = "e-expired"
            runtime.claude_deferred_reason = "expired"
            runtime.claude_hook_health = "healthy"
            runtime.claude_session_id = "session-uuid"
            self.assertTrue(daemon._restore_expired_claude_deferred(runtime))
            self.assertEqual(runtime.claude_deferred_event["event_id"], "e-expired")
            self.assertEqual(runtime.claude_deferred_reason, "expired_retry")
            self.assertLess(time.time() - runtime.claude_deferred_since, 1.0)

    def test_legacy_provisional_deferred_row_is_also_rehydrated(self):
        """Old daemons may leave ``deferred_active_stop_hook`` at expiry."""
        with tempfile.TemporaryDirectory() as directory:
            daemon, _, runtime = self._daemon(directory)
            event = claude_hook_event("e-legacy-expired", "Stop")
            daemon.claude_event_ledger.mark(
                event, "deferred_active_stop_hook", detail="active_stop_hook",
            )
            runtime.claude_last_event_id = "e-legacy-expired"
            runtime.claude_deferred_reason = "expired"
            self.assertTrue(daemon._restore_expired_claude_deferred(runtime))
            self.assertEqual(
                runtime.claude_deferred_event["event_id"], "e-legacy-expired",
            )
            self.assertEqual(runtime.claude_deferred_reason, "expired_retry")

    def test_later_send_release_reaches_a_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                claude_hook_event("e-covered", "Stop"), "Claude composer is busy",
            )
            runtime.last_send_at = runtime.claude_deferred_since + 1.0
            handled = daemon._maybe_send_deferred_claude_stop(
                daemon.config["targets"][0], runtime,
                core.ScreenState("claude_stopped", message_kind="claude"), client,
            )
            self.assertFalse(handled)
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(
                daemon.claude_event_ledger.events["e-covered"]["status"], "deferred_cleared",
            )

    def test_later_send_still_covers_stop_after_retry_window_expires(self):
        for elapsed in (899.0, 900.0, 1800.0):
            with self.subTest(elapsed=elapsed), tempfile.TemporaryDirectory() as directory:
                daemon, client, runtime = self._daemon(directory)
                daemon._defer_claude_event(
                    "surface-uuid", runtime,
                    claude_hook_event("e-covered-expired", "Stop"), "Claude composer is busy",
                )
                runtime.claude_deferred_since = 100.0
                runtime.last_send_at = 200.0
                with mock.patch.object(core.time, "time", return_value=100.0 + elapsed), \
                        mock.patch.object(daemon, "_send_claude_event") as send:
                    handled = daemon._maybe_send_deferred_claude_stop(
                        daemon.config["targets"][0], runtime,
                        core.ScreenState("claude_hook_waiting", message_kind="claude"), client,
                    )
                send.assert_not_called()
                self.assertFalse(handled)
                self.assertIsNone(runtime.claude_deferred_event)
                self.assertEqual(runtime.claude_deferred_reason, "covered_by_later_send")
                self.assertEqual(runtime.last_send_at, 200.0)
                self.assertEqual(client.sent, [])

    def test_a_superseded_slot_is_filed_as_dropped(self):
        # The single slot means a newer Stop displaces the older one.  This is
        # the only release path where an episode is genuinely lost, so it must
        # never be filed as a clean cancellation.
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                claude_hook_event("e-old", "Stop"), "Claude composer is busy",
            )
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                claude_hook_event("e-new", "Stop"), "Claude viewport is working",
            )
            self.assertEqual(
                daemon.claude_event_ledger.events["e-old"]["status"], "deferred_dropped",
            )
            self.assertEqual(
                str(runtime.claude_deferred_event.get("event_id")), "e-new",
            )

    def test_expiry_retry_can_complete_and_stays_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                claude_hook_event("e-old-park", "Stop"), "Claude composer is busy",
            )
            runtime.claude_deferred_since = time.time() - (core.CLAUDE_DEFERRED_MAX_AGE_SEC + 1.0)
            daemon._maybe_send_deferred_claude_stop(
                daemon.config["targets"][0], runtime,
                core.ScreenState("claude_stopped", message_kind="claude"), client,
            )
            self.assertEqual(
                daemon.claude_event_ledger.events["e-old-park"]["status"], "sent",
            )

    def test_a_delivered_event_is_never_relabelled_by_its_own_cleanup(self):
        # The success path marks ``sent`` and *then* releases the slot.  Writing
        # a terminal status unconditionally would overwrite the record of the
        # delivery that actually happened.
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                claude_hook_event("e-recover", "Stop"), "Claude composer is busy",
            )
            before = len(client.sent_text)
            handled = daemon._maybe_send_deferred_claude_stop(
                daemon.config["targets"][0], runtime,
                core.ScreenState("claude_stopped", message_kind="claude"), client,
            )
            self.assertTrue(handled)
            self.assertEqual(
                daemon.claude_event_ledger.events["e-recover"]["status"], "sent",
            )
            self.assertIsNone(runtime.claude_deferred_event)
            # Exactly one delivery, not one per release path.
            self.assertEqual(len(client.sent_text) - before, 1)

    def test_releasing_an_empty_slot_stays_silent(self):
        # Every poll of every surface runs through the completion path, so a log
        # line or ledger write on an empty slot would be pure noise.
        with tempfile.TemporaryDirectory() as directory:
            daemon, _, runtime = self._daemon(directory)
            before = len(daemon.claude_event_ledger.events)
            daemon.logger.setLevel(logging.INFO)
            handler = logging.Handler()
            records = []
            handler.emit = records.append
            daemon.logger.addHandler(handler)
            try:
                held = daemon._clear_claude_deferred(runtime, reason="completed")
            finally:
                daemon.logger.removeHandler(handler)
            self.assertEqual(held, "")
            self.assertEqual(len(daemon.claude_event_ledger.events), before)
            self.assertEqual(
                [r for r in records if "deferred stop released" in str(r.msg)], [],
            )


class ClaudeEpisodeLedgerInvariantTests(unittest.TestCase):
    """Durable identities must survive retries, restarts, and ledger pressure."""

    @staticmethod
    def _fallback_event(event_id="event-id", *, episode="episode-a", attempt=1):
        return {
            "version": 1,
            "event_id": event_id,
            "created_at": time.time(),
            "event_name": "StopFailure",
            "surface_id": "surface-uuid",
            "session_id": "session-uuid",
            "synthetic_fallback": True,
            "episode_id": episode,
            "process_generation": "generation-a",
            "attempt_number": attempt,
            "evidence_fingerprint": "content-a",
        }

    def test_ledger_distinguishes_same_episode_collision_and_terminal_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = core.ClaudeEventLedger(Path(directory) / "ledger.json")
            event = self._fallback_event()
            self.assertEqual(ledger.claim_detailed(event), core.CLAUDE_CLAIMED)
            self.assertEqual(
                ledger.claim_detailed(dict(event)),
                core.CLAUDE_DUPLICATE_SAME_EPISODE,
            )
            collision = {**event, "episode_id": "episode-b"}
            self.assertEqual(
                ledger.claim_detailed(collision),
                core.CLAUDE_HISTORICAL_ID_COLLISION,
            )
            ledger.mark(event, "sent", detail="delivered")
            self.assertEqual(
                ledger.claim_detailed(dict(event)),
                core.CLAUDE_TERMINAL_CONFLICT,
            )
            self.assertEqual(ledger.events[event["event_id"]]["episode_id"], "episode-a")

    def test_ledger_pressure_never_evicts_active_rows(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            core, "CLAUDE_EVENT_LEDGER_LIMIT", 4,
        ):
            ledger = core.ClaudeEventLedger(Path(directory) / "ledger.json")
            ledger.events["old-generation-terminal"] = {
                "status": "deferred_generation_changed",
                "handled_at": 1.0,
                "event_name": "Stop",
                "surface_id": "surface-uuid",
                "session_id": "session-old",
            }
            active = {
                "handling": "handling",
                "reserved": "reserved",
                "deferred": "deferred_working",
            }
            for event_id, status in active.items():
                event = self._fallback_event(event_id, episode=f"episode-{event_id}")
                ledger.mark(event, status, detail=status)
            for index in range(8):
                event = self._fallback_event(
                    f"terminal-{index}", episode=f"terminal-episode-{index}",
                )
                ledger.mark(event, "sent", detail="terminal")
            self.assertLessEqual(len(ledger.events), 4)
            self.assertTrue(set(active).issubset(ledger.events))
            self.assertNotIn("old-generation-terminal", ledger.events)

    def test_generation_change_revokes_all_old_claude_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = claude_armed_daemon(directory, FakeClient(claude_grid_payload()))
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_process_pid = 111
            runtime.claude_process_generation = "generation-old"
            runtime.claude_hook_process_generation = "generation-old"
            runtime.claude_session_id = "session-old"
            runtime.claude_generation_id = "session-start-old"
            runtime.claude_last_hook_at = time.time()
            runtime.claude_last_event_id = "event-old"
            runtime.claude_last_event_status = "sent"
            runtime.claude_completed_latched = True
            runtime.claude_last_submit_event_id = "submit-old"
            runtime.claude_last_submit_session_id = "session-old"
            runtime.claude_last_submit_generation = "generation-old"
            runtime.claude_fallback_episode_id = "episode-old"
            runtime.claude_fallback_episode_generation = "generation-old"
            runtime.claude_fallback_episode_session_id = "session-old"
            runtime.episode_id = "codex-episode-old"
            runtime.send_count = 9
            daemon._defer_claude_event(
                "surface-uuid", runtime,
                {**claude_hook_event("deferred-old"), "process_generation": "generation-old"},
                "working",
            )

            daemon._apply_claude_process_observation(runtime, {
                "agent_kind": "claude",
                "pid": 222,
                "generation": "generation-new",
                "started_at": "2026-08-27T00:00:00",
                "started_epoch": time.time(),
            })

            self.assertEqual(runtime.claude_process_generation, "generation-new")
            self.assertEqual(runtime.claude_hook_health, "unverified")
            self.assertIsNone(runtime.claude_hook_process_generation)
            self.assertIsNone(runtime.claude_session_id)
            self.assertEqual(runtime.claude_last_hook_at, 0.0)
            self.assertFalse(runtime.claude_completed_latched)
            self.assertIsNone(runtime.claude_last_submit_event_id)
            self.assertIsNone(runtime.claude_fallback_episode_id)
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertIsNone(runtime.episode_id)
            self.assertEqual(runtime.send_count, 0)
            self.assertEqual(
                daemon.claude_event_ledger.status_of("deferred-old"),
                "deferred_generation_changed",
            )

    def test_same_process_backfills_hook_generation_from_legacy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = claude_armed_daemon(directory, FakeClient(claude_grid_payload()))
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            now = time.time()
            runtime.claude_process_pid = 111
            runtime.claude_process_generation = "generation-stable"
            runtime.claude_hook_process_generation = None
            # The first daemon running the new schema may already have
            # persisted this derived downgrade before the migration fix loads.
            runtime.claude_hook_health = "legacy_override"
            runtime.claude_session_id = "session-stable"
            runtime.claude_last_hook_at = now - 30.0

            daemon._apply_claude_process_observation(runtime, {
                "agent_kind": "claude",
                "pid": 111,
                "generation": "generation-stable",
                "started_at": "2026-08-27T00:00:00",
                "started_epoch": now - 3600.0,
                # A real same-process Hook outranks static inline-settings
                # inspection: the process demonstrably emitted the Hook.
                "legacy_override": True,
            })

            self.assertEqual(
                runtime.claude_hook_process_generation, "generation-stable",
            )
            self.assertEqual(runtime.claude_hook_health, "healthy")
            self.assertEqual(runtime.claude_session_id, "session-stable")

    def test_legacy_hook_generation_is_not_backfilled_without_causal_time_order(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = claude_armed_daemon(directory, FakeClient(claude_grid_payload()))
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            now = time.time()
            runtime.claude_process_pid = 111
            runtime.claude_process_generation = "generation-stable"
            runtime.claude_hook_process_generation = None
            runtime.claude_hook_health = "healthy"
            runtime.claude_session_id = "session-stale"
            runtime.claude_last_hook_at = now - 7200.0

            daemon._apply_claude_process_observation(runtime, {
                "agent_kind": "claude",
                "pid": 111,
                "generation": "generation-stable",
                "started_at": "2026-08-27T00:00:00",
                "started_epoch": now - 3600.0,
                "legacy_override": True,
            })

            self.assertIsNone(runtime.claude_hook_process_generation)
            self.assertEqual(runtime.claude_hook_health, "legacy_override")

    def test_deferred_generation_or_session_mismatch_cannot_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(lines=["unfinished"], completed=True),
                text="unfinished\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_process_generation = "generation-new"
            runtime.claude_session_id = "session-new"
            runtime.claude_hook_health = "healthy"
            old = {
                **claude_hook_event("deferred-mismatch", session_id="session-old"),
                "process_generation": "generation-old",
            }
            daemon._defer_claude_event("surface-uuid", runtime, old, "working")
            handled = daemon._maybe_send_deferred_claude_stop(
                daemon.config["targets"][0], runtime,
                core.ScreenState("claude_stopped", message_kind="claude"), client,
            )
            self.assertFalse(handled)
            self.assertEqual(client.sent, [])
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(
                daemon.claude_event_ledger.status_of("deferred-mismatch"),
                "deferred_generation_changed",
            )

    def test_new_episode_may_reuse_the_same_visible_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0,
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "sent"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            stopped = core.ScreenState(
                "claude_hook_waiting", message_kind="claude",
                content_fingerprint="same-content", screen_signature="same-screen",
            )
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client,
            ))
            first_event = runtime.claude_fallback_last_event_id
            first_episode = runtime.claude_fallback_episode_id
            daemon._clear_claude_submit(runtime, reason="hook_confirmed")
            daemon._clear_claude_fallback_episode(runtime)
            runtime.claude_last_event_status = "human_prompt"
            runtime.claude_last_hook_at = time.time()
            self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client,
            ))
            self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                daemon.config["targets"][0], runtime, stopped, observation, client,
            ))
            self.assertEqual(len(client.sent_text), 2)
            self.assertNotEqual(runtime.claude_fallback_episode_id, first_episode)
            self.assertNotEqual(runtime.claude_fallback_last_event_id, first_event)

    def test_fallback_recovers_historical_id_collision_without_overwriting_old_row(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0,
        ):
            client = FakeClient(
                claude_grid_payload(lines=["unfinished output"], completed=True),
                "unfinished output\n" + claude_idle_screen(),
                tree={"windows": []},
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            daemon._check_claude_hook_settings(force=True)
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_last_event_status = "human_prompt"
            runtime.claude_hook_health = "healthy"
            runtime.claude_process_pid = 123
            runtime.claude_process_generation = "generation-a"
            episode = "episode-new"
            token = "collision-token"
            collision_id = core.hashlib.sha256(
                f"ccc-hook-gap-v2\0surface-uuid\0generation-a\0{episode}\0"
                f"1\0{token}".encode()
            ).hexdigest()
            old_event = self._fallback_event(collision_id, episode="episode-old")
            daemon.claude_event_ledger.mark(old_event, "sent", detail="historical delivery")
            uuid_values = iter((episode, token, "replacement-token"))

            def deterministic_uuid():
                value = next(uuid_values, "unused-token")
                return mock.Mock(hex=value)

            observation = {"agent_kind": "claude", "pid": 123, "generation": "generation-a"}
            stopped = core.ScreenState(
                "claude_hook_waiting", message_kind="claude",
                content_fingerprint="content-a", screen_signature="screen-a",
            )
            with mock.patch.object(core.uuid, "uuid4", side_effect=deterministic_uuid):
                self.assertFalse(daemon._maybe_send_claude_hook_gap_fallback(
                    daemon.config["targets"][0], runtime, stopped, observation, client,
                ))
                self.assertTrue(daemon._maybe_send_claude_hook_gap_fallback(
                    daemon.config["targets"][0], runtime, stopped, observation, client,
                ))

            self.assertEqual(daemon.claude_event_ledger.events[collision_id]["episode_id"], "episode-old")
            self.assertEqual(daemon.claude_event_ledger.events[collision_id]["status"], "sent")
            self.assertNotEqual(runtime.claude_fallback_last_event_id, collision_id)
            replacement = daemon.claude_event_ledger.events[runtime.claude_fallback_last_event_id]
            self.assertEqual(replacement["episode_id"], episode)
            self.assertEqual(replacement["status"], "sent")


class DeferredStopTests(unittest.TestCase):
    """Stage 2: a transiently unsafe frame must not discard the episode."""

    def test_transient_classifier_accepts_only_instantaneous_reasons(self):
        transient = [
            "Claude composer is busy",
            "Claude viewport is working",
            "assistant content changed during preflight",
            "focus changed during preflight",
            "Claude submit text not visible; waiting for confirmation",
        ]
        for detail in transient:
            self.assertTrue(core.WatchDaemon._cancel_is_transient(detail), detail)
        for detail in ["context:claude_context_stalled", "process is codex", "cmux send text failed"]:
            self.assertFalse(core.WatchDaemon._cancel_is_transient(detail), detail)

    def test_active_stop_hook_is_deferred_not_dropped(self):
        # 2026-08-25 surface:36: two stop_hook_active Stops were dropped after a
        # Working-cancelled Stop, leaving nothing to retry for sixteen minutes.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            event = claude_hook_event("e-active", "Stop", stop_hook_active=True)
            daemon._handle_claude_event(event, client)
            self.assertIsNotNone(runtime.claude_deferred_event)
            self.assertEqual(runtime.claude_deferred_reason, "active_stop_hook")
            self.assertEqual(
                daemon.claude_event_ledger.events["e-active"]["status"],
                "deferred_active_stop_hook",
            )

    def test_transient_cancel_is_deferred_rather_than_dropped(self):
        """The 355-cancel measurement: 99.7% were transient and 3 never recovered.

        This is the path that produced the tail -- a Working viewport at the
        instant of the Stop -- so it needs its own test.  The active-stop-hook
        test above exercises a different branch and cannot cover this one.
        """

        class WorkingOnSecondFrame(FakeClient):
            def __init__(self, stopped, working, **kwargs):
                super().__init__(stopped, **kwargs)
                self.stopped = stopped
                self.working = working
                self.frames = 0

            def replay(self, workspace_id, surface_id):
                self.replays.append((workspace_id, surface_id))
                self.frames += 1
                return self.working if self.frames >= 2 else self.stopped

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            stopped = claude_grid_payload(lines=["half a sentence"], completed=True)
            working = claude_grid_payload(lines=["half a sentence"], spinner="Thinking")
            client = WorkingOnSecondFrame(
                stopped, working,
                text="half a sentence\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            daemon._handle_claude_event(claude_hook_event("e-working", "Stop"), client)

            self.assertEqual(client.sent_text, [])
            self.assertIsNotNone(runtime.claude_deferred_event)
            self.assertEqual(runtime.claude_deferred_event["event_id"], "e-working")
            status = daemon.claude_event_ledger.events["e-working"]["status"]
            self.assertTrue(status.startswith("deferred_"), status)
            self.assertNotEqual(status, "cancelled")

    def test_deferred_stop_is_sent_once_the_frame_is_safe_again(self):
        """End-to-end recovery: the tail case that used to need a human."""

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["half a sentence"], completed=True),
                text="half a sentence\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            daemon._defer_claude_event(
                "surface-uuid", runtime, claude_hook_event("e-parked", "Stop"), "working",
            )
            state = core.ScreenState("claude_stopped", message_kind="claude")
            handled = daemon._maybe_send_deferred_claude_stop(target, runtime, state, client)
            self.assertTrue(handled)
            self.assertEqual(len(client.sent_text), 1)
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(runtime.send_count, 1)
            # Exactly once: a second poll must not resend the same parked event.
            handled_again = daemon._maybe_send_deferred_claude_stop(target, runtime, state, client)
            self.assertFalse(handled_again)
            self.assertEqual(len(client.sent_text), 1)

    def test_expiry_log_reports_the_real_wait_not_an_epoch(self):
        """Expiry rebases a retry window instead of discarding the event."""

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon._defer_claude_event(
                "surface-uuid", runtime, claude_hook_event("e-old", "Stop"), "working",
            )
            runtime.claude_deferred_since = time.time() - 1000.0

            messages = []

            class Capture(logging.Handler):
                def emit(self, record):
                    messages.append(record.getMessage())

            handler = Capture()
            # The daemon logger is NOTSET and inherits root (WARNING by
            # default), so INFO records are dropped unless the level is set
            # here.  Without this the assertions below only pass when an
            # earlier test in the same process happened to call basicConfig --
            # a false green that disappears when the class runs alone.
            daemon.logger.setLevel(logging.INFO)
            daemon.logger.addHandler(handler)
            try:
                daemon._maybe_send_deferred_claude_stop(
                    target, runtime, core.ScreenState("working", message_kind="claude"),
                    client,
                )
            finally:
                daemon.logger.removeHandler(handler)
            line = next(m for m in messages if "deferred stop expired" in m)
            waited = float(re.search(r"waited_sec=(\d+)", line).group(1))
            self.assertLess(waited, 2000.0, line)
            self.assertGreater(waited, 900.0, line)
            self.assertIsNotNone(runtime.claude_deferred_event)
            self.assertEqual(runtime.claude_deferred_reason, "expired_retry")
            self.assertLess(time.time() - runtime.claude_deferred_since, 1.0)

    def test_only_one_deferred_slot_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            first = claude_hook_event("e-1", "Stop")
            second = claude_hook_event("e-2", "Stop")
            daemon._defer_claude_event("surface-uuid", runtime, first, "working")
            daemon._defer_claude_event("surface-uuid", runtime, second, "composer")
            self.assertEqual(runtime.claude_deferred_event["event_id"], "e-2")

    def test_later_send_drops_the_deferred_slot_instead_of_replaying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon._defer_claude_event("surface-uuid", runtime, claude_hook_event("e-old", "Stop"), "working")
            runtime.last_send_at = runtime.claude_deferred_since + 1.0
            state = core.ScreenState("claude_stopped", message_kind="claude")
            handled = daemon._maybe_send_deferred_claude_stop(target, runtime, state, client)
            self.assertFalse(handled)
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(client.sent_text, [])

    def test_completion_latch_clears_the_deferred_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon._defer_claude_event("surface-uuid", runtime, claude_hook_event("e-old", "Stop"), "working")
            runtime.claude_completed_latched = True
            state = core.ScreenState("claude_stopped", message_kind="claude")
            daemon._maybe_send_deferred_claude_stop(target, runtime, state, client)
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(client.sent_text, [])

    def test_deferred_slot_expires_without_pausing_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon._defer_claude_event("surface-uuid", runtime, claude_hook_event("e-old", "Stop"), "working")
            runtime.claude_deferred_since = time.time() - (core.CLAUDE_DEFERRED_MAX_AGE_SEC + 1)
            # A working frame must keep the rebased slot for a later safe poll;
            # expiry is no longer a terminal drop.
            state = core.ScreenState("working", message_kind="claude")
            daemon._maybe_send_deferred_claude_stop(target, runtime, state, client)
            self.assertIsNotNone(runtime.claude_deferred_event)
            self.assertEqual(runtime.state, "claude_deferred_retrying")
            self.assertEqual(runtime.claude_deferred_reason, "expired_retry")
            self.assertFalse(target.get("paused", False))

    def test_expired_deferred_stop_can_send_from_hook_waiting_state(self):
        """Hook-owned waiting is eligible once the current frame is stopped."""
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["half a sentence"], completed=True),
                text="half a sentence\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            daemon._defer_claude_event(
                "surface-uuid", runtime, claude_hook_event("e-expiry-send", "Stop"), "working",
            )
            runtime.claude_deferred_since = time.time() - (core.CLAUDE_DEFERRED_MAX_AGE_SEC + 1)
            handled = daemon._maybe_send_deferred_claude_stop(
                target, runtime, core.ScreenState("claude_hook_waiting", message_kind="claude"), client,
            )
            self.assertTrue(handled)
            self.assertEqual(len(client.sent_text), 1)
            self.assertIsNone(runtime.claude_deferred_event)
            self.assertEqual(
                daemon.claude_event_ledger.events["e-expiry-send"]["status"], "sent",
            )

    def test_deferred_stop_is_not_sent_while_viewport_is_unsafe(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon._defer_claude_event("surface-uuid", runtime, claude_hook_event("e-old", "Stop"), "working")
            state = core.ScreenState("working", message_kind="claude")
            handled = daemon._maybe_send_deferred_claude_stop(target, runtime, state, client)
            self.assertFalse(handled)
            self.assertIsNotNone(runtime.claude_deferred_event)
            self.assertEqual(client.sent_text, [])

    def test_human_prompt_supersedes_a_deferred_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            daemon._defer_claude_event("surface-uuid", runtime, claude_hook_event("e-old", "Stop"), "working")
            daemon._handle_claude_event(
                claude_hook_event("e-human", "UserPromptSubmit", prompt_kind="human"), client,
            )
            self.assertIsNone(runtime.claude_deferred_event)


class EnterFailureAccountingTests(unittest.TestCase):
    """Stage 3: a failed Enter must not be booked as a delivery."""

    def test_enter_failure_does_not_record_a_send(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"

            def refuse_enter(workspace_id, surface_id, key):
                raise CmuxError("send-key refused")

            client.send_key = refuse_enter
            event = claude_hook_event("e-enter", "Stop")
            sent, detail = daemon._send_claude_event(event, target, runtime, client)
            self.assertFalse(sent)
            self.assertIn("Enter failed", detail)
            self.assertEqual(runtime.send_count, 0)
            self.assertEqual(runtime.last_send_at, 0.0)
            self.assertEqual(runtime.claude_hook_sla_miss_count, 0)
            self.assertEqual(runtime.claude_hook_live_send_count, 0)
            self.assertNotEqual(runtime.claude_last_event_status, "sent")
            self.assertEqual(runtime.claude_submit_phase, "text_written")
            self.assertEqual(len(client.sent_text), 1)


class SubmitTimingSegmentTests(unittest.TestCase):
    """Stage 4: every segment must name exactly one operation."""

    def _send_and_capture(self, directory, records):
        client = FakeClient(
            claude_grid_payload(), text=claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        daemon = armed_daemon(directory, client)
        daemon.config["claude_enabled"] = True
        target = daemon.config["targets"][0]
        runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
        runtime.claude_session_id = "session-uuid"
        runtime.claude_hook_health = "healthy"

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        # See the note above: NOTSET logger + WARNING root means INFO is
        # discarded, and every timing assertion here reads an INFO line.
        daemon.logger.setLevel(logging.INFO)
        daemon.logger.addHandler(handler)
        try:
            sent, detail = daemon._send_claude_event(
                claude_hook_event("e-timing", "Stop"), target, runtime, client,
            )
        finally:
            daemon.logger.removeHandler(handler)
        return sent, detail

    def test_log_line_carries_the_new_segments(self):
        records = []
        with tempfile.TemporaryDirectory() as directory:
            sent, detail = self._send_and_capture(directory, records)
        self.assertTrue(sent, detail)
        line = next(m for m in records if "sent=claude_hook" in m)
        for field in ["claim_ms=", "preflight=", "submit=", "persist_ms=", "post_send=",
                      "slow_submit_stage="]:
            self.assertIn(field, line)
        for segment in ["reserve_bookkeeping_ms=", "ledger_reserve_ms=", "persist_reserve_ms=",
                        "transaction_init_ms=", "text_ms=", "readback_ms=",
                        "persist_pre_enter_ms=", "enter_ms="]:
            self.assertIn(segment, line)
        for segment in ["finalize_bookkeeping_ms=", "ledger_finalize_ms=", "persist_finalize_ms="]:
            self.assertIn(segment, line)

    def test_preflight_excludes_the_submit_transaction(self):
        # The pre-fix field ended after the log call and swallowed send_text, the
        # readback, the Enter and three fsyncing save() calls.
        records = []
        with tempfile.TemporaryDirectory() as directory:
            sent, detail = self._send_and_capture(directory, records)
        self.assertTrue(sent, detail)
        line = next(m for m in records if "sent=claude_hook" in m)
        preflight = float(re.search(r"preflight=([\d.]+)s", line).group(1))
        # Structural, not a wall-clock threshold.  A threshold assertion cannot
        # detect this bug on a fake client: measuring preflight all the way to
        # the log call only moved it 0.122s -> 0.125s, which passed every
        # plausible bound.  preflight must equal the six preflight stages and
        # nothing else, so folding in even one submit segment breaks it.
        six = ["process_ms", "frame1_ms", "settle_ms", "frame2_ms",
               "guard_ms", "verify_ms"]
        expected = sum(
            float(re.search(rf"\b{name}=([\d.]+)", line).group(1)) for name in six
        )
        self.assertAlmostEqual(preflight * 1000.0, expected, delta=2.0)
        submit_total = float(re.search(r"submit=([\d.]+)s", line).group(1)) * 1000.0
        self.assertGreater(submit_total, 0.0, line)
        self.assertLess(preflight * 1000.0, expected + submit_total, line)

    def test_submit_total_equals_the_sum_of_its_segments(self):
        records = []
        with tempfile.TemporaryDirectory() as directory:
            sent, detail = self._send_and_capture(directory, records)
        self.assertTrue(sent, detail)
        line = next(m for m in records if "sent=claude_hook" in m)
        submit = float(re.search(r"submit=([\d.]+)s", line).group(1))
        names = ["reserve_bookkeeping_ms", "ledger_reserve_ms", "persist_reserve_ms",
                 "transaction_init_ms", "text_ms", "readback_ms",
                 "persist_pre_enter_ms", "enter_ms"]
        total = sum(
            float(re.search(rf"\b{name}=([\d.]+)", line).group(1)) for name in names
        )
        self.assertAlmostEqual(submit * 1000.0, total, delta=2.0)

    def test_persist_ms_counts_only_the_two_in_latency_persists(self):
        records = []
        with tempfile.TemporaryDirectory() as directory:
            sent, detail = self._send_and_capture(directory, records)
        self.assertTrue(sent, detail)
        line = next(m for m in records if "sent=claude_hook" in m)
        persist_total = float(re.search(r"persist_ms=([\d.]+)", line).group(1))
        reserve = float(re.search(r"persist_reserve_ms=([\d.]+)", line).group(1))
        pre_enter = float(re.search(r"persist_pre_enter_ms=([\d.]+)", line).group(1))
        self.assertAlmostEqual(persist_total, reserve + pre_enter, delta=0.2)

    def test_slow_stage_is_never_a_submit_segment(self):
        records = []
        with tempfile.TemporaryDirectory() as directory:
            sent, detail = self._send_and_capture(directory, records)
        self.assertTrue(sent, detail)
        line = next(m for m in records if "sent=claude_hook" in m)
        slow = re.search(r"slow_stage=(\S*)", line).group(1)
        submit_names = {"reserve_bookkeeping_ms", "ledger_reserve_ms", "persist_reserve_ms",
                        "transaction_init_ms", "text_ms", "readback_ms",
                        "persist_pre_enter_ms", "enter_ms"}
        self.assertNotIn(slow, submit_names)

    def test_claim_cost_reaches_the_log_with_a_real_value(self):
        """claim= was stored under one key and read under another, so it was 0.000s.

        The claim fsync happens in _handle_claude_event_locked, *before*
        _send_claude_event is entered, yet it sits inside the end-to-end latency
        the SLA judges.  It must therefore be routed through the full event path,
        not the send helper alone -- which is also why this test must call
        _handle_claude_event rather than _send_claude_event directly.
        """

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            client = FakeClient(
                claude_grid_payload(lines=["half a sentence"], completed=True),
                "half a sentence\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            with self.assertLogs(daemon.logger, level="INFO") as logs:
                daemon._handle_claude_event(claude_hook_event("claim-cost"), client)
            line = next(m for m in logs.output if "sent=claude_hook" in m)
            claim = float(re.search(r"claim_ms=([0-9.]+)", line).group(1))
            # A real fsync of the ledger is never free.
            # One ledger fsync is never free, and never seconds either.  The
            # field was previously printed as seconds with 3 decimals, so every
            # real sub-millisecond measurement rendered as "0.000".
            self.assertGreater(claim, 0.0, line)
            self.assertLess(claim, 1000.0, line)
            # And the map must not leak: the entry is popped on use.
            self.assertNotIn("claim-cost", daemon._claude_claim_ms)

    def test_each_timing_segment_appears_exactly_once(self):
        """A duplicated segment name silently overwrites the earlier measurement."""

        records = []
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"

            class Capture(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())

            handler = Capture()
            daemon.logger.setLevel(logging.INFO)
            daemon.logger.addHandler(handler)
            try:
                sent, detail = daemon._send_claude_event(
                    claude_hook_event("e-once", "Stop"), target, runtime, client,
                )
            finally:
                daemon.logger.removeHandler(handler)
            self.assertTrue(sent, detail)
            line = next(m for m in records if "sent=claude_hook" in m)
            names = re.findall(r"\b(\w+_ms)=", line)
            duplicates = {n for n in names if names.count(n) > 1}
            self.assertEqual(duplicates, set(), f"duplicated segments: {duplicates}")

    def test_claim_cost_map_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            # Only the send path consumes an entry; most events never reach it.
            for index in range(core.CLAUDE_CLAIM_COST_LIMIT + 5):
                daemon._claude_claim_ms[f"event-{index}"] = 1.0
                if len(daemon._claude_claim_ms) >= core.CLAUDE_CLAIM_COST_LIMIT:
                    daemon._claude_claim_ms.clear()
            self.assertLessEqual(len(daemon._claude_claim_ms), core.CLAUDE_CLAIM_COST_LIMIT)


class BlindSpotReachabilityTests(unittest.TestCase):
    """Stage 5: the blind clock must be able to reach its own threshold."""

    def test_clock_accumulates_across_polls(self):
        # The clock only seeds on the first call and measures on later ones, so a
        # single-poll test cannot prove the warning is reachable at all.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            daemon._refresh_claude_unreadable_clock("surface-uuid", runtime, "incompatible",
                                                    entry="returned_main")
            first = runtime.claude_unreadable_since
            self.assertGreater(first, 0.0)
            runtime.claude_unreadable_since = time.time() - 3600.0
            daemon._refresh_claude_unreadable_clock("surface-uuid", runtime, "incompatible",
                                                    entry="raised_initial")
            self.assertGreater(runtime.claude_unreadable_warned_at, 0.0)
            # A changed entry point must not restart a continuing blind spot.
            self.assertEqual(runtime.claude_unreadable_entry, "raised_initial")

    def test_readable_frame_clears_the_clock_and_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen())
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            daemon._refresh_claude_unreadable_clock("s", runtime, "incompatible", entry="returned_main")
            daemon._refresh_claude_unreadable_clock("s", runtime, "claude_stopped")
            self.assertEqual(runtime.claude_unreadable_since, 0.0)
            self.assertIsNone(runtime.claude_unreadable_entry)

    def test_live_claude_process_keeps_a_blind_pane_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            self.assertTrue(
                daemon._claude_blind_spot_keeps_monitoring(target, runtime, client),
            )

    def test_live_shell_isolates_the_pane_even_with_claude_history(self):
        # Stale runtime fields must not authorise continued polling: a pane that
        # now holds a shell can classify as recoverable_error, which is sendable.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "zsh")),
            )
            daemon = armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            self.assertFalse(
                daemon._claude_blind_spot_keeps_monitoring(target, runtime, client),
            )

    def test_inconclusive_process_check_isolates(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), text=claude_idle_screen(), top=None)
            daemon = armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            runtime.claude_session_id = "session-uuid"
            self.assertFalse(
                daemon._claude_blind_spot_keeps_monitoring(target, runtime, client),
            )


class Surface36ReplayTests(unittest.TestCase):
    """Replay of the real 2026-08-25 surface:36 chain, end to end.

    14:07:32  Stop, viewport working      -> was terminal ``cancelled``
    14:07:55  Stop, stop_hook_active      -> was terminal ``ignored_active_stop_hook``
    14:08:29  Stop, stop_hook_active      -> same
    14:08:29+ 16 minutes with no send; the user continued the session by hand.

    Each link had its own unit test, but nothing replayed the whole chain, so the
    interaction between them was never covered.  The requirement is exact: after
    the viewport becomes safe again the episode recovers with *exactly one* send.
    """

    class _Viewport(FakeClient):
        """Working during preflight until ``safe`` is set, then stopped."""

        def __init__(self, stopped, working, **kwargs):
            super().__init__(stopped, **kwargs)
            self.stopped = stopped
            self.working = working
            self.safe = False
            self.frames = 0

        def replay(self, workspace_id, surface_id):
            if self.safe:
                # Delegate so the base class still simulates the watchdog echo
                # after send_text; without it the submit readback fails and the
                # send looks transient rather than successful.
                return super().replay(workspace_id, surface_id)
            self.replays.append((workspace_id, surface_id))
            self.frames += 1
            # Frame 1 stopped, frame 2 working: the double-frame preflight sees
            # the content change and refuses to send, exactly as in production.
            return self.working if self.frames >= 2 else self.stopped

    def test_the_full_chain_recovers_and_sends_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "cmux_codex_watch.CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC", 0
        ):
            stopped = claude_grid_payload(lines=["half a sentence"], completed=True)
            working = claude_grid_payload(lines=["half a sentence"], spinner="Thinking")
            client = self._Viewport(
                stopped, working,
                text="half a sentence\n" + claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"

            # 14:07:32 -- Stop lands on a working viewport.
            daemon._handle_claude_event(claude_hook_event("s1-working", "Stop"), client)
            self.assertEqual(client.sent, [], "must not send onto a working viewport")
            self.assertIsNotNone(
                runtime.claude_deferred_event,
                "a transient working frame must park the Stop, not discard it",
            )
            self.assertEqual(runtime.claude_deferred_event["event_id"], "s1-working")
            self.assertEqual(runtime.claude_submit_phase, "none")

            # 14:07:55 and 14:08:29 -- two stop_hook_active Stops.
            for event_id in ("s2-active", "s3-active"):
                daemon._handle_claude_event(
                    claude_hook_event(event_id, "Stop", stop_hook_active=True), client
                )
                self.assertNotEqual(
                    daemon.claude_event_ledger.events[event_id]["status"],
                    "ignored_active_stop_hook",
                    "an active Stop must never be dropped terminally",
                )
            self.assertEqual(client.sent, [], "still unsafe: no send yet")
            self.assertIsNotNone(runtime.claude_deferred_event, "slot must stay held")

            # Exactly one slot, never a queue.
            self.assertIsInstance(runtime.claude_deferred_event, dict)

            # The viewport becomes safe.  This is the poll that used to do nothing.
            client.safe = True
            safe = core.ScreenState(
                "claude_stopped", message_kind="claude", error_type="claude_stopped",
            )
            handled = daemon._maybe_send_deferred_claude_stop(
                target, runtime, safe, client,
            )
            self.assertTrue(handled, "the safe poll must own this cycle")
            self.assertEqual(len(client.sent), 1, "recovery must send exactly once")
            self.assertIsNone(
                runtime.claude_deferred_event, "slot must be released after sending",
            )

            # And a further safe poll must not send again.
            daemon._maybe_send_deferred_claude_stop(target, runtime, safe, client)
            self.assertEqual(len(client.sent), 1, "must not re-send a recovered event")

    def test_a_completion_report_cancels_the_parked_stop(self):
        """The 14:31:50 completion must not be followed by a stale deferred send."""

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = armed_daemon(directory, client)
            daemon.config["claude_enabled"] = True
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            daemon._defer_claude_event(
                "surface-uuid", runtime, claude_hook_event("parked", "Stop"), "working",
            )
            daemon._handle_claude_event(
                claude_hook_event("done", "Stop", completed=True), client,
            )
            self.assertTrue(runtime.claude_completed_latched)
            handled = daemon._maybe_send_deferred_claude_stop(
                target, runtime,
                core.ScreenState("claude_stopped", message_kind="claude"), client,
            )
            self.assertFalse(handled)
            self.assertEqual(client.sent, [], "a completed turn must not be continued")
            self.assertIsNone(runtime.claude_deferred_event)


class WrappedComposerClassificationTests(unittest.TestCase):
    """A composer whose text wraps is still a composer.

    Measured on surface:43 (2026-08-31): ``❯`` on row 64, cursor on row 66.  The
    distance gate read any ``distance > 1`` as "the cursor is somewhere else
    entirely" and returned ``unverified``, so a three-row prompt was
    indistinguishable from a cursor parked outside the box.  ``unverified``
    yields ``incompatible``, which computes no ``watchdog_echo`` -- so recovery
    keyed on that flag could never fire while the composer was misread.

    These four cells are the whole truth table.  Proving only that the fix
    accepts the fault shape cannot rule out a fix that also accepts a cursor
    which really is elsewhere, so the negative cell carries equal weight.
    """

    def _state(self, payload):
        grid = Grid.from_rpc(payload, "surface-uuid")
        kind, distance = core._claude_composer_state(grid)
        return grid, kind, distance

    def test_the_wrapped_fixture_actually_arms_the_distance_gate(self):
        """Non-vacuity: a gate that never arms proves nothing either way.

        With a wide terminal the message fits in two rows, distance is 1, and
        the gate is not even consulted -- an earlier version of this fixture
        passed for exactly that reason while testing nothing.
        """

        grid, _, distance = self._state(claude_wrapped_composer_payload())
        self.assertGreater(
            distance, 1,
            "fixture must wrap to 3+ rows or the distance gate is never reached",
        )
        self.assertTrue(grid.cursor.visible, "the gate also requires a visible cursor")

    def test_wrapped_watchdog_echo_is_busy_not_unverified(self):
        # Cell A: the exact surface:43 fault shape.
        payload = claude_wrapped_composer_payload()
        grid, kind, _ = self._state(payload)
        self.assertEqual(kind, "busy")
        state = classify_claude_grid(grid)
        self.assertEqual(state.kind, "composer_busy")
        # The flag recovery depends on is only computed on this branch.
        self.assertTrue(state.watchdog_echo)

    def test_cursor_below_a_rule_stays_unverified(self):
        # Cell B: a cursor that genuinely is outside the composer box.  Without
        # this, "accept wrapping" would be indistinguishable from "accept
        # anything", and the classifier would claim a composer it cannot see.
        grid, kind, distance = self._state(
            claude_wrapped_composer_payload(rule_before_cursor=True),
        )
        self.assertGreater(distance, 1)
        self.assertEqual(kind, "unverified")
        self.assertEqual(classify_claude_grid(grid).kind, "incompatible")

    def test_wrapped_user_text_is_busy_but_never_a_watchdog_echo(self):
        # Cell C: same geometry, different author.  Wrapping must not make a
        # human's prompt look like ours; watchdog_echo is the only credential
        # for pressing Enter, so this is the assertion that keeps recovery from
        # submitting something a user typed.
        grid, kind, _ = self._state(claude_wrapped_composer_payload(
            "我自己写的一段很长的输入，绝对不是守卫器的续跑提示，请不要替我发送出去",
        ))
        self.assertEqual(kind, "busy")
        state = classify_claude_grid(grid)
        self.assertEqual(state.kind, "composer_busy")
        self.assertFalse(
            state.watchdog_echo, "user text must never be credited as our echo",
        )

    def test_hidden_cursor_is_unchanged_by_the_fix(self):
        # Cell D: Claude reports cursor.visible=false on most frames, so the
        # gate never armed there.  This pins that the fix did not disturb the
        # overwhelmingly common case.
        grid, kind, _ = self._state(
            claude_wrapped_composer_payload(cursor_visible=False),
        )
        self.assertFalse(grid.cursor.visible)
        self.assertEqual(kind, "busy")
        self.assertTrue(classify_claude_grid(grid).watchdog_echo)

    def test_an_empty_composer_is_still_empty(self):
        """The send-eligible path must be untouched: empty stays empty."""

        grid = Grid.from_rpc(claude_grid_payload(), "surface-uuid")
        kind, _ = core._claude_composer_state(grid)
        self.assertEqual(kind, "empty")

    def test_blank_rows_continue_the_block_but_a_footer_ends_it(self):
        """The two boundary rules, asserted directly on the predicate.

        Claude pads the composer to its full height, so a blank row is *not* a
        boundary -- treating it as one would reintroduce the same false
        ``unverified`` for any prompt with a blank line in it.
        """

        payload = claude_wrapped_composer_payload()
        grid = Grid.from_rpc(payload, "surface-uuid")
        prompt_row = next(
            row for row, text in enumerate(grid.lines) if text.strip().startswith("❯")
        )
        self.assertTrue(core._claude_cursor_inside_composer(grid, prompt_row))
        # A footer row between prompt and cursor ends the block.
        spans = list(payload["render_grid"]["row_spans"])
        spans.append(span(prompt_row + 1, 0, "  [Opus 5 (1M context)] │ ~/repo", 0))
        payload["render_grid"]["row_spans"] = spans
        footer_grid = Grid.from_rpc(payload, "surface-uuid")
        self.assertFalse(core._claude_cursor_inside_composer(footer_grid, prompt_row))

    def test_the_predicate_rejects_rows_outside_the_grid(self):
        grid = Grid.from_rpc(claude_grid_payload(), "surface-uuid")
        self.assertFalse(core._claude_cursor_inside_composer(grid, -1))
        self.assertFalse(core._claude_cursor_inside_composer(grid, grid.rows))

    def test_the_measured_surface43_geometry_at_120_columns(self):
        """The production shape, which is NOT the multi-row text wrap above.

        Reconstructed from the live reading (prompt row 64, cursor row 66, 120
        columns): ``CLAUDE_MESSAGE`` is 145 display cells, so at 120 columns it
        occupies exactly TWO rows -- 64 and 65.  Row 66 is the composer's blank
        padding row, and that is where the cursor sat.  So the distance of 2 came
        from padding, not from a third row of text.

        This matters because it is a different branch of the predicate: the
        wrapped-text tests above exercise the "row has content" path, while this
        one exercises the ``if not stripped: continue`` path.  Testing only the
        former would leave the actual production geometry uncovered.
        """

        columns = 120
        chunks = wrap_to_cells(CLAUDE_MESSAGE, columns - 2)
        self.assertEqual(
            len(chunks), 2,
            "at 120 columns the watchdog prompt occupies two rows, not three",
        )
        prompt_row = 64
        rows = 71
        styles = [
            {"id": 0, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": False},
        ]
        row_spans = [span(prompt_row, 0, "❯", 0, 1)]
        for offset, chunk in enumerate(chunks):
            row_spans.append(
                span(prompt_row + offset, 2, chunk, 0, display_width(chunk)),
            )
        # Row 66 carries no span at all: blank padding inside the composer box.
        row_spans.append(span(rows - 2, 0, "  [Opus 5 (1M context)] │ ~/repo", 0))
        row_spans.append(span(rows - 1, 0, "  ⏵⏵ bypass permissions on", 0))
        payload = {
            "render_grid": {
                "format": "cmux.render-grid.v1",
                "surface_id": "surface-uuid",
                "rows": rows,
                "columns": columns,
                "cursor": {"row": 66, "column": 2, "visible": True},
                "styles": styles,
                "row_spans": row_spans,
                "scrollback_spans": [],
                "history_rows": 0,
            }
        }
        grid = Grid.from_rpc(payload, "surface-uuid")
        self.assertEqual(grid.lines[66].strip(), "", "row 66 must be the blank pad row")
        kind, distance = core._claude_composer_state(grid)
        self.assertEqual(distance, 2, "the measured prompt/cursor delta")
        self.assertEqual(kind, "busy")
        state = classify_claude_grid(grid)
        self.assertEqual(state.kind, "composer_busy")
        self.assertTrue(
            state.watchdog_echo,
            "without this flag the orphan recovery can never fire, which is why "
            "surface:43 stayed stuck",
        )

    def test_a_second_prompt_row_ends_the_block(self):
        """Defensive branch, tested directly because production cannot reach it.

        Mutation testing found this branch survives every end-to-end test: a
        brute-force sweep of prompt/cursor row configurations produced zero
        cases where ``_claude_composer_state`` picks an anchor with another ``❯``
        between it and the cursor, because it always anchors on the ``❯``
        *nearest* the cursor -- the intervening one would have won.  So the
        branch is unreachable through the only production caller, and the honest
        way to cover it is to call the predicate directly rather than to claim
        an end-to-end test that does not exist.  It stays in the source because
        a future caller choosing a different anchor would need it.
        """

        payload = claude_wrapped_composer_payload()
        prompt_rows = [
            item["row"] for item in payload["render_grid"]["row_spans"]
            if item["text"] == "❯"
        ]
        prompt_row = min(prompt_rows)
        cursor_row = payload["render_grid"]["cursor"]["row"]
        self.assertGreater(cursor_row, prompt_row + 1)
        spans = list(payload["render_grid"]["row_spans"])
        spans.append(span(prompt_row + 1, 0, "❯", 0, 1))
        payload["render_grid"]["row_spans"] = spans
        grid = Grid.from_rpc(payload, "surface-uuid")
        self.assertFalse(core._claude_cursor_inside_composer(grid, prompt_row))

    def test_a_wrapped_composer_never_becomes_send_eligible(self):
        """Reclassifying as ``busy`` must not open a send path.

        ``busy`` is not in SEND_ELIGIBLE_STATES and ``_claude_event_snapshot``
        rejects any composer that is not ``empty``.  The fix makes the daemon
        *see* the text; it must not make it type over it.
        """

        self.assertNotIn("composer_busy", core.SEND_ELIGIBLE_STATES)
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_wrapped_composer_payload(),
                text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            with self.assertRaises(IncompatibleError) as caught:
                daemon._claude_event_snapshot(daemon.config["targets"][0], client)
            self.assertIn("composer is busy", str(caught.exception))
            self.assertEqual(client.sent_text, [])
            self.assertEqual(client.sent_keys, [])


class BlankFirstRowComposerTests(unittest.TestCase):
    """F1（2026-09-01）：``❯`` 行为空、文字只在续行上的 composer。

    粘贴多行文本或行首换行会让 composer 的第一行完全空白，用户文字从
    prompt 行的下一行开始渲染。修复前 ``_claude_composer_state`` 只扫
    prompt 行的 span，把这种 composer 读成 ``empty``——唯一能打开发送
    路径的状态——守卫器会在用户输入之上直接打字。这里的每个负例与正例
    权重相同：只证明"接受故障形"无法排除"接受一切"的错误修法。
    """

    def _payload(
        self,
        text_rows,
        *,
        style_id=0,
        rule_after_prompt=False,
        cursor_on_prompt=False,
        columns=60,
    ):
        body = ["previous output"]
        prompt_row = len(body) + 1
        row_spans = [
            span(row, 0, item, 0, display_width(item))
            for row, item in enumerate(body)
        ]
        row_spans.append(span(prompt_row, 0, "❯", 0, 1))
        next_row = prompt_row + 1
        if rule_after_prompt:
            row_spans.append(span(next_row, 0, "─" * columns, 0, columns))
            next_row += 1
        for offset, chunk in enumerate(text_rows):
            row_spans.append(
                span(next_row + offset, 2, chunk, style_id, display_width(chunk)),
            )
        last_text_row = next_row + len(text_rows) - 1 if text_rows else prompt_row
        cursor_row = prompt_row if cursor_on_prompt else last_text_row
        rows = max(cursor_row, last_text_row) + 4
        styles = [
            {"id": 0, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": False},
            {"id": 1, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": True},
        ]
        footer = "  [Opus 5 (1M context)] │ ~/repo"
        hint = "  ⏵⏵ bypass permissions on"
        row_spans.append(span(rows - 2, 0, footer, 0, display_width(footer)))
        row_spans.append(span(rows - 1, 0, hint, 0, display_width(hint)))
        return {
            "render_grid": {
                "format": "cmux.render-grid.v1",
                "surface_id": "surface-uuid",
                "rows": rows,
                "columns": columns,
                "cursor": {"row": cursor_row, "column": 2, "visible": True},
                "styles": styles,
                "row_spans": row_spans,
                "scrollback_spans": [],
                "history_rows": 0,
            }
        }

    def _state(self, payload):
        grid = Grid.from_rpc(payload, "surface-uuid")
        kind, distance = core._claude_composer_state(grid)
        return grid, kind, distance

    def test_blank_prompt_row_with_continuation_text_is_busy(self):
        # 故障形本体：首行空、用户文字在续行 → 必须 busy，绝不能 empty。
        grid, kind, _ = self._state(self._payload(
            ["用户粘贴的多行内容第二行，首行是空行"],
        ))
        self.assertEqual(kind, "busy")
        state = classify_claude_grid(grid)
        self.assertEqual(state.kind, "composer_busy")
        # 用户文字绝不能被记为我们的回显——echo 是按 Enter 的唯一凭据。
        self.assertFalse(state.watchdog_echo)

    def test_two_continuation_rows_are_also_busy(self):
        grid, kind, _ = self._state(self._payload(
            ["第一段续行文字", "第二段续行文字"],
        ))
        self.assertEqual(kind, "busy")

    def test_faint_continuation_hint_stays_empty(self):
        # 淡色 span 是占位提示（chrome），不是输入；把它当输入会让空
        # composer 永远无法发送。
        grid, kind, _ = self._state(self._payload(
            ["Try \"fix lint errors\""], style_id=1,
        ))
        self.assertEqual(kind, "empty")

    def test_text_below_a_rule_stays_out_of_the_composer(self):
        # 负例：rule 是 composer 盒的结构边界。光标越过 rule 时走既有的
        # unverified 分支（incompatible，不发送）；无论如何绝不能是 busy
        # 之外的可发送状态，更不能把盒外文字算进 composer。
        grid, kind, _ = self._state(self._payload(
            ["盒子外面的历史输出"], rule_after_prompt=True,
        ))
        self.assertEqual(kind, "unverified")

    def test_cursor_on_prompt_row_scans_no_continuation(self):
        # 光标停在 ❯ 行时续行区间为空：空 composer 的历史行为逐字节不变。
        grid, kind, _ = self._state(self._payload(
            ["光标行以下的文字不参与判定"], cursor_on_prompt=True,
        ))
        self.assertEqual(kind, "empty")

    def test_the_new_busy_shape_never_becomes_send_eligible(self):
        self.assertNotIn("composer_busy", core.SEND_ELIGIBLE_STATES)
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                self._payload(["用户还没发出去的多行输入"]),
                text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            with self.assertRaises(IncompatibleError) as caught:
                daemon._claude_event_snapshot(daemon.config["targets"][0], client)
            self.assertIn("composer is busy", str(caught.exception))
            self.assertEqual(client.sent_text, [])
            self.assertEqual(client.sent_keys, [])


class OrphanWatchdogSubmitRecoveryTests(unittest.TestCase):
    """Text of ours left in the composer with no transaction to submit it.

    Measured on surface:43 (2026-08-31 14:51 IST): a submit hit
    ``confirmation_timeout``, which clears ``claude_submit_phase`` to ``none``
    *while leaving the typed text on screen*.  After that nobody could submit
    it -- ``_reconcile_claude_submit`` returned at its entry gate (no live
    transaction) and ``_claude_event_snapshot`` refused to open a new one (the
    composer is not empty).  The prompt sat there 283s with attempts=0.  A
    deadlock between two individually-correct guards, not a stuck keypress.
    """

    def _daemon(self, directory, *, payload=None, **extra):
        client = FakeClient(
            payload if payload is not None else claude_wrapped_composer_payload(),
            text=claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        daemon = claude_armed_daemon(directory, client, **extra)
        runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
        runtime.claude_session_id = "session-uuid"
        runtime.claude_hook_health = "healthy"
        return daemon, client, runtime

    def _orphan_state(self, *, watchdog_echo=True, kind="composer_busy"):
        return ScreenState(kind, message_kind="claude", watchdog_echo=watchdog_echo)

    def test_orphaned_watchdog_text_gets_its_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            # Exactly the stranded state: phase cleared, text still on screen.
            self.assertEqual(runtime.claude_submit_phase, "none")
            handled = daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime, self._orphan_state(), client,
            )
            self.assertTrue(handled, "recovery must claim the poll")
            self.assertEqual([item[2] for item in client.sent_keys], ["enter"])
            self.assertEqual(client.sent_text, [], "never retype, only press Enter")
            self.assertEqual(runtime.claude_orphan_enter_count, 1)
            self.assertEqual(runtime.state, "claude_orphan_enter")

    def test_user_text_in_the_composer_is_never_submitted(self):
        """The single most important negative: no echo, no key.

        ``watchdog_echo`` is an exact normalised match of ``claude_message``
        reconstructed across wrapped rows, so a human's text cannot satisfy it.
        """

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            handled = daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime,
                self._orphan_state(watchdog_echo=False), client,
            )
            self.assertFalse(handled)
            self.assertEqual(client.sent_keys, [])
            self.assertEqual(client.sent_text, [])
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_recovery_is_end_to_end_from_a_wrapped_grid(self):
        """The classifier fix and the recovery are one causal chain.

        Cell A of the wrapped table produces ``watchdog_echo=True``; that flag
        is recovery's only credential.  This drives recovery from a real grid
        rather than a hand-built ScreenState, so a regression in *either* half
        fails here -- which is what actually stranded surface:43.
        """

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            grid = Grid.from_rpc(client.payload, "surface-uuid")
            state = classify_claude_grid(grid)
            self.assertEqual(state.kind, "composer_busy")
            self.assertTrue(state.watchdog_echo)
            self.assertTrue(daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime, state, client,
            ))
            self.assertEqual([item[2] for item in client.sent_keys], ["enter"])

    def test_a_wrapped_grid_that_is_unverified_recovers_nothing(self):
        """The pre-fix classification, fed through the real path.

        This is why the two fixes shipped together: before the classifier fix
        this grid produced ``incompatible`` with ``watchdog_echo`` false, so
        recovery was unreachable no matter how correct it was.
        """

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(
                directory,
                payload=claude_wrapped_composer_payload(rule_before_cursor=True),
            )
            state = classify_claude_grid(Grid.from_rpc(client.payload, "surface-uuid"))
            self.assertEqual(state.kind, "incompatible")
            self.assertFalse(state.watchdog_echo)
            self.assertFalse(daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime, state, client,
            ))
            self.assertEqual(client.sent_keys, [])

    def test_a_live_transaction_is_advanced_not_recovered(self):
        """Recovery is the no-transaction branch only; it must not double-press."""

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            runtime.claude_submit_event_id = "event-live"
            runtime.claude_submit_phase = "text_written"
            runtime.claude_submit_since = time.time()
            self.assertTrue(daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime, self._orphan_state(), client,
            ))
            # The normal transaction path pressed Enter and accounted for it
            # there; the orphan counter must stay untouched.
            self.assertEqual(runtime.claude_orphan_enter_count, 0)
            self.assertEqual(runtime.claude_submit_attempts, 1)

    def test_only_a_composer_still_holding_the_text_is_recoverable(self):
        """``working``/``menu`` mean Claude already consumed it: nothing to send."""

        for kind in ("working", "menu", "claude_stopped", "claude_completed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                daemon, client, runtime = self._daemon(directory)
                handled = daemon._reconcile_claude_submit(
                    daemon.config["targets"][0], runtime,
                    self._orphan_state(kind=kind), client,
                )
                self.assertFalse(handled)
                self.assertEqual(client.sent_keys, [])
                self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_retry_is_rate_limited_and_counted(self):
        """Bounded per unit is not bounded in total, so the count is its own
        observable rather than something inferred from a timestamp."""

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            target = daemon.config["targets"][0]
            state = self._orphan_state()
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertEqual(len(client.sent_keys), 1)
            # Immediately again: still claims the poll, but presses nothing.
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertEqual(len(client.sent_keys), 1)
            self.assertEqual(runtime.claude_orphan_enter_count, 1)
            # Past the window it may try once more.
            runtime.claude_orphan_enter_at = time.time() - (
                core.CLAUDE_ORPHAN_ENTER_RETRY_SEC + 1.0
            )
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertEqual(len(client.sent_keys), 2)
            self.assertEqual(runtime.claude_orphan_enter_count, 2)

    def test_the_retry_window_is_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(
                directory, claude_orphan_enter_retry_sec=600,
            )
            target = daemon.config["targets"][0]
            state = self._orphan_state()
            daemon._reconcile_claude_submit(target, runtime, state, client)
            runtime.claude_orphan_enter_at = time.time() - 60.0
            daemon._reconcile_claude_submit(target, runtime, state, client)
            self.assertEqual(len(client.sent_keys), 1, "60s < 600s window")

    def test_a_paused_or_disabled_target_is_never_recovered(self):
        """Recovery sits downstream of the poll's paused/disabled gate.

        Placement is the whole safety argument, so it is asserted through
        ``process_once`` rather than by reading the source: a paused target is
        skipped before any Claude code runs.
        """

        for flag in ({"paused": True}, {"enabled": False}):
            with self.subTest(**flag), tempfile.TemporaryDirectory() as directory:
                daemon, client, runtime = self._daemon(directory)
                daemon.config["targets"][0].update(flag)
                daemon.process_once(client)
                self.assertEqual(client.sent_keys, [])
                self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_recovery_reaches_enter_through_a_full_poll(self):
        """The positive twin of the paused test: an armed target does recover."""

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon.process_once(client)
            self.assertEqual([item[2] for item in client.sent_keys], ["enter"])
            self.assertEqual(client.sent_text, [])
            self.assertEqual(runtime.claude_orphan_enter_count, 1)

    def test_a_globally_paused_daemon_recovers_nothing(self):
        """The gap this test found, kept as the reason it exists.

        Recovery differs from every other Claude send in one way that matters:
        it fires with *no* owning transaction, so it cannot inherit a gate that
        an earlier send already passed.  ``_reconcile_claude_submit`` never
        checked ``global_paused`` -- it did not need to, because reaching it
        required a transaction that ``_handle_claude_event_locked`` had already
        gated.  Recovery removed that guarantee and pressed Enter under a global
        pause until the same check was added here.
        """

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory, global_paused=True)
            daemon.process_once(client)
            self.assertEqual(client.sent_keys, [])
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_dry_run_mode_recovers_nothing(self):
        """The other half of the arming gate: dry-run presses no keys."""

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon.config["mode"] = "dry-run"
            daemon.process_once(client)
            self.assertEqual(client.sent_keys, [])
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_claude_disabled_recovers_nothing(self):
        """``claude_enabled=false`` must keep the whole Claude path dark."""

        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory, claude_enabled=False)
            daemon.process_once(client)
            self.assertEqual(client.sent_keys, [])
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_enter_is_the_only_key_recovery_can_press(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime, self._orphan_state(), client,
            )
            self.assertEqual({item[2] for item in client.sent_keys}, {"enter"})


class OrphanEnterBudgetTests(unittest.TestCase):
    """F2（2026-09-01）：孤儿 Enter 的总量上限与预算重置。

    间隔限制只保证"每 5 秒最多一次"，不保证总次数有限：一个永远提交不
    成功的 composer（渲染异常、Claude 卡死、Enter 键失效）会让恢复路径
    无限期按键。预算耗尽即 degraded；只有 composer 内容变化、新事务建立
    或人工重新 arm 才重置——每个重置点和"不该重置"的负例都在这里钉死。
    """

    def _daemon(self, directory, *, payload=None, **extra):
        client = FakeClient(
            payload if payload is not None else claude_wrapped_composer_payload(),
            text=claude_idle_screen(),
            top=process_fixture(("surface-uuid", "claude")),
        )
        daemon = claude_armed_daemon(directory, client, **extra)
        runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
        runtime.claude_session_id = "session-uuid"
        runtime.claude_hook_health = "healthy"
        return daemon, client, runtime

    def _orphan_state(self, *, watchdog_echo=True, kind="composer_busy"):
        return ScreenState(kind, message_kind="claude", watchdog_echo=watchdog_echo)

    def _drain_window(self, runtime):
        runtime.claude_orphan_enter_at = time.time() - (
            core.CLAUDE_ORPHAN_ENTER_RETRY_SEC + 1.0
        )

    def test_budget_exhaustion_stops_the_keypress_and_degrades(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            target = daemon.config["targets"][0]
            state = self._orphan_state()
            for expected in (1, 2, 3):
                self.assertTrue(daemon._reconcile_claude_submit(
                    target, runtime, state, client,
                ))
                self.assertEqual(runtime.claude_orphan_enter_count, expected)
                self._drain_window(runtime)
            # 第 4 次：仍然认领本轮 poll（防止其他路径接手），但绝不再按键。
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertEqual(len(client.sent_keys), 3, "budget is 3, not 4")
            self.assertEqual(runtime.claude_orphan_enter_count, 3)
            self.assertEqual(runtime.state, "claude_orphan_degraded")
            # 之后的每一轮都保持 degraded，不重新计数。
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertEqual(len(client.sent_keys), 3)

    def test_the_budget_is_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory, claude_orphan_enter_max=1)
            target = daemon.config["targets"][0]
            state = self._orphan_state()
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self._drain_window(runtime)
            self.assertTrue(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertEqual(len(client.sent_keys), 1)
            self.assertEqual(runtime.state, "claude_orphan_degraded")

    def test_an_emptied_composer_resets_the_budget(self):
        # 文字被消费或清空 = 孤儿事件结束，预算归零。
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            target = daemon.config["targets"][0]
            runtime.claude_orphan_enter_count = 3
            handled = daemon._reconcile_claude_submit(
                target, runtime,
                ScreenState("empty", message_kind="claude"), client,
            )
            self.assertFalse(handled, "empty composer has nothing to recover")
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_a_user_edit_resets_the_budget(self):
        # composer 仍 busy 但回显不再匹配 = 用户在编辑，同样终结孤儿事件。
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            runtime.claude_orphan_enter_count = 3
            handled = daemon._reconcile_claude_submit(
                daemon.config["targets"][0], runtime,
                self._orphan_state(watchdog_echo=False), client,
            )
            self.assertFalse(handled)
            self.assertEqual(runtime.claude_orphan_enter_count, 0)
            self.assertEqual(client.sent_keys, [])

    def test_transient_frames_never_reset_the_budget(self):
        # working/menu 帧上回显判定本来就不成立；靠它们重置会让预算被渲染
        # 抖动清零，上限形同虚设。
        for kind in ("working", "menu", "claude_stopped", "incompatible"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                daemon, client, runtime = self._daemon(directory)
                runtime.claude_orphan_enter_count = 2
                daemon._reconcile_claude_submit(
                    daemon.config["targets"][0], runtime,
                    self._orphan_state(watchdog_echo=False, kind=kind), client,
                )
                self.assertEqual(runtime.claude_orphan_enter_count, 2)

    def test_a_new_submit_transaction_resets_the_budget(self):
        # 新事务重新拥有 composer：走真实 deferred-stop 发送路径穿过事务
        # 初始化，而不是直接改字段。
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                claude_grid_payload(), text=claude_idle_screen(),
                top=process_fixture(("surface-uuid", "claude")),
            )
            daemon = claude_armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            runtime = daemon.runtime.setdefault("surface-uuid", core.TargetRuntime())
            runtime.claude_session_id = "session-uuid"
            runtime.claude_hook_health = "healthy"
            runtime.claude_orphan_enter_count = 3
            daemon._defer_claude_event(
                "surface-uuid", runtime, claude_hook_event("parked", "Stop"), "working",
            )
            client.safe = True
            safe = core.ScreenState(
                "claude_stopped", message_kind="claude", error_type="claude_stopped",
            )
            self.assertTrue(daemon._maybe_send_deferred_claude_stop(
                target, runtime, safe, client,
            ))
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_rearm_resets_the_budget_only_on_the_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            # armed→armed 的普通配置刷新不得重置。
            runtime.claude_orphan_enter_count = 2
            daemon._config_mtime_ns = -1
            daemon._reload_config_if_changed()
            self.assertEqual(runtime.claude_orphan_enter_count, 2)
            # 非armed→armed 的边沿是明确的"再试一次"授权。
            daemon.config["mode"] = "dry-run"
            daemon._config_mtime_ns = -1
            daemon._reload_config_if_changed()
            self.assertEqual(str(daemon.config.get("mode")), "armed")
            self.assertEqual(runtime.claude_orphan_enter_count, 0)

    def test_degraded_state_logs_once_not_every_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client, runtime = self._daemon(directory)
            target = daemon.config["targets"][0]
            state = self._orphan_state()
            runtime.claude_orphan_enter_count = 99
            with self.assertLogs(daemon.logger, level="WARNING") as captured:
                daemon._reconcile_claude_submit(target, runtime, state, client)
            self.assertEqual(
                len([r for r in captured.records if "budget exhausted" in r.getMessage()]),
                1,
            )
            self.assertEqual(runtime.state, "claude_orphan_degraded")
            # 已 degraded 后不再重复告警。
            with self.assertNoLogs(daemon.logger, level="WARNING"):
                daemon._reconcile_claude_submit(target, runtime, state, client)


if __name__ == "__main__":
    unittest.main()
