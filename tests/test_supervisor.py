import json
import re
import curses
import inspect
import io
import os
import subprocess
import tempfile
import threading
import time
import types
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmux_codex_watch as core  # noqa: E402
from cmux_supervisor_tui import (  # noqa: E402
    CONTEXT_STALE_SEC,
    Candidate,
    SupervisorModel,
    context_label,
    display_width as display_width_of,
    ROW_COLUMNS,
    DETAIL_SHORT,
    STATE_LABELS,
    _row_text,
    selected_action_hint,
    state_label,
)


class _DiscoveryClient:
    """A workspace:11 with one Codex surface and one Claude surface, no Dock."""

    def tree(self):
        return {"windows": [{"workspaces": [{
            "id": "workspace-11", "ref": "workspace:11", "title": "Hermes",
            "panes": [{"id": "pane-24", "ref": "pane:24", "surfaces": [
                {"id": "surface-59", "ref": "surface:59", "type": "terminal", "title": "Codex"},
                {"id": "surface-60", "ref": "surface:60", "type": "terminal", "title": "Claude"},
            ]}],
        }]}]}

    def top_all(self):
        return {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": "surface-59", "ref": "surface:59",
             "processes": [{"kind": "process", "name": "codex", "path": "/bin/codex"}]},
            {"kind": "surface", "id": "surface-60", "ref": "surface:60",
             "processes": [{"kind": "process", "name": "claude", "path": "/bin/claude"}]},
        ]}]}]}


class DockRunner:
    def __init__(self, *, existing=False, stale=False):
        self.calls = []
        self.existing = existing
        self.stale = stale

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        create_surface = "-".join(("new", "surface"))
        if "tree" in command:
            if self.existing:
                value = {"windows": [{"id": "window-uuid", "workspaces": [{
                    "id": "workspace-uuid", "panes": [{"surfaces": [{
                        "id": "dock-uuid", "ref": "surface:84", "title": "Supervisor",
                        "dock_scope": "global", "workspace_id": "workspace-uuid",
                    }]}],
                }]}]}
            else:
                value = {"windows": [{"id": "window-uuid", "workspaces": []}]}
        elif "identify" in command:
            value = {
                "caller": {"window_id": "window-uuid", "surface_id": "target-uuid"},
                "focused": {"window_id": "window-uuid", "surface_id": "target-uuid"},
            }
        elif "respawn-pane" in command and self.stale:
            return subprocess.CompletedProcess(command, 1, "", "Error: not_found: Surface not found for the given surface_id")
        elif create_surface in command:
            value = {"dock_surface_id": "dock-new" if self.stale else "dock-uuid"}
        elif "read-screen" in command:
            value = "old management surface" if self.stale else "Supervisor mode=armed"
        else:
            value = {}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")


class SupervisorTests(unittest.TestCase):
    def test_dock_install_merges_and_backs_up_existing_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dock.json"
            path.write_text(json.dumps({
                "controls": [
                    {"id": "user-control", "title": "User", "command": "true"},
                    {"id": "cmux-codex-supervisor", "title": "Old", "command": "old"},
                ],
                "other": "preserved",
            }), encoding="utf-8")
            result = core.install_dock_control(path, Path(directory) / "config.json")
            merged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(merged["other"], "preserved")
            self.assertEqual([item["id"] for item in merged["controls"]], ["user-control", "cmux-codex-supervisor"])
            self.assertIn("tui", merged["controls"][1]["command"])
            self.assertTrue(Path(result["backup"]).exists())

    def test_dock_open_uses_returned_dock_uuid_for_send(self):
        runner = DockRunner()
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            result = core.open_supervisor_dock(Path(directory) / "config.json", client=client)
        self.assertEqual(result["surface_id"], "dock-uuid")
        create_surface = "-".join(("new", "surface"))
        create_call = next(call for call in runner.calls if create_surface in call)
        self.assertIn("--placement", create_call)
        self.assertIn("dock", create_call)
        send = next(call for call in runner.calls if call[:2] == ["cmux", "send"])
        self.assertIn("dock-uuid", send)
        self.assertIn("tui", send[-1])
        show_index = next(index for index, call in enumerate(runner.calls) if "right-sidebar" in call)
        create_index = next(index for index, call in enumerate(runner.calls) if create_surface in call)
        self.assertLess(show_index, create_index)

    def test_initialize_dock_surface_retries_transient_not_found(self):
        class ReadinessRunner(DockRunner):
            def __init__(self):
                super().__init__()
                self.send_attempts = 0

            def __call__(self, command, **kwargs):
                self.calls.append(command)
                if command[:2] == ["cmux", "send"]:
                    self.send_attempts += 1
                    if self.send_attempts < 3:
                        return subprocess.CompletedProcess(command, 1, "", "Error: not_found: surface not ready")
                return subprocess.CompletedProcess(command, 0, "{}", "")

        runner = ReadinessRunner()
        client = core.CmuxClient(binary="cmux", runner=runner)
        with mock.patch.object(core.time, "sleep") as sleep:
            client.initialize_dock_surface("window-uuid", "dock-uuid", "Supervisor", "run-tui")
        self.assertEqual(runner.send_attempts, 3)
        self.assertEqual(sleep.call_count, 2)
        send = next(call for call in runner.calls if call[:2] == ["cmux", "send"])
        self.assertIn("dock-uuid", send)

    def test_initialize_dock_surface_allows_cosmetic_rename_failure(self):
        class RenameUnsupportedRunner(DockRunner):
            def __call__(self, command, **kwargs):
                self.calls.append(command)
                if "rename-tab" in command:
                    return subprocess.CompletedProcess(command, 1, "", "Error: not_found: 找不到分頁")
                return subprocess.CompletedProcess(command, 0, "{}", "")

        runner = RenameUnsupportedRunner()
        client = core.CmuxClient(binary="cmux", runner=runner)
        client.initialize_dock_surface("window-uuid", "dock-uuid", "Supervisor", "run-tui")
        send = next(call for call in runner.calls if call[:2] == ["cmux", "send"])
        self.assertIn("dock-uuid", send)

    def test_dock_open_reuses_existing_supervisor_without_creating(self):
        runner = DockRunner(existing=True)
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            result = core.open_supervisor_dock(Path(directory) / "config.json", client=client)
        self.assertEqual(result["surface_id"], "dock-uuid")
        self.assertFalse(result["created"])
        create_surface = "-".join(("new", "surface"))
        self.assertFalse(any(create_surface in call for call in runner.calls))
        self.assertFalse(any(call[:2] == ["cmux", "send"] for call in runner.calls))

    def test_dock_open_recognizes_chinese_supervisor_screen(self):
        class ChineseScreenRunner(DockRunner):
            def __call__(self, command, **kwargs):
                if "read-screen" in command:
                    self.calls.append(command)
                    return subprocess.CompletedProcess(command, 0, "续跑管理  真实发送中  当前原因  本轮", "")
                return super().__call__(command, **kwargs)

        runner = ChineseScreenRunner(existing=True)
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            result = core.open_supervisor_dock(Path(directory) / "config.json", client=client)
        self.assertFalse(result["created"])
        self.assertFalse(any("respawn-pane" in call for call in runner.calls))

    def test_dock_open_refreshes_old_chinese_supervisor_screen(self):
        class OldChineseScreenRunner(DockRunner):
            def __call__(self, command, **kwargs):
                if "read-screen" in command:
                    self.calls.append(command)
                    return subprocess.CompletedProcess(command, 0, "续跑管理  真实发送中  错误  次数", "")
                return super().__call__(command, **kwargs)

        runner = OldChineseScreenRunner(existing=True, stale=True)
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            result = core.open_supervisor_dock(Path(directory) / "config.json", client=client)
        self.assertTrue(result["created"])
        self.assertEqual(result["surface_id"], "dock-new")

    def test_dock_open_recreates_stale_supervisor_only_on_not_found(self):
        runner = DockRunner(existing=True, stale=True)
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            result = core.open_supervisor_dock(Path(directory) / "config.json", client=client)
        self.assertEqual(result["surface_id"], "dock-new")
        self.assertTrue(result["created"])
        self.assertTrue(any("respawn-pane" in call for call in runner.calls))
        send = next(call for call in runner.calls if call[:2] == ["cmux", "send"])
        self.assertIn("dock-new", send)

    def test_dock_open_prefers_configured_manager_over_older_supervisor_record(self):
        class MultiDockRunner(DockRunner):
            def __call__(self, command, **kwargs):
                self.calls.append(command)
                if "tree" in command:
                    value = {"windows": [{"id": "window-uuid", "workspaces": [{
                        "id": "workspace-uuid", "panes": [{"surfaces": [
                            {"id": "old-dock", "ref": "surface:84", "title": "Supervisor", "dock_scope": "global"},
                            {"id": "new-dock", "ref": "surface:85", "title": "Supervisor", "dock_scope": "global"},
                        ]}],
                    }]}]}
                elif "identify" in command:
                    value = {"caller": {"window_id": "window-uuid", "surface_id": "target-uuid"}}
                elif "read-screen" in command:
                    value = "Supervisor mode=armed"
                else:
                    value = {}
                return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

        runner = MultiDockRunner()
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({**core.default_config(), "manager_surface_id": "new-dock"}), encoding="utf-8")
            result = core.open_supervisor_dock(config_path, client=client)
        self.assertEqual(result["surface_id"], "new-dock")
        self.assertFalse(result["created"])

    def test_dock_open_cli_persists_manager_surface_id(self):
        runner = DockRunner(existing=True)
        client = core.CmuxClient(binary="cmux", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(core, "CmuxClient", return_value=client), redirect_stdout(output):
                result = core.cli(["--config", str(config_path), "dock-open"])
            self.assertEqual(result, 0)
            config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["manager_surface_id"], "dock-uuid")
        self.assertEqual(json.loads(output.getvalue())["manager_surface_id"], "dock-uuid")

    def test_supervisor_lists_excluded_and_missing_explicit_targets(self):
        class Client:
            def tree(self):
                return {
                    "windows": [{"workspaces": [{
                        "id": "workspace-uuid", "ref": "workspace:9", "title": "Codex pool",
                        "panes": [{"id": "pane-uuid", "ref": "pane:20", "surfaces": [
                            {"id": "codex-a", "ref": "surface:44", "type": "terminal", "title": "cnm"},
                        ]}],
                    }]}],
                }

            def top_all(self):
                return {"windows": [{"workspaces": [{"surfaces": [{
                    "kind": "surface", "ref": "surface:44",
                    "processes": [{"kind": "process", "name": "codex", "path": "/bin/codex"}],
                }]}]}]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = core.default_config()
            config["targets"] = [{
                "surface_id": "missing-uuid", "workspace_id": "workspace-uuid", "ref": "surface:99",
                "title_snapshot": "gone", "name": "gone", "enabled": True, "paused": False,
            }]
            config["workspace_rules"] = [{
                "workspace_id": "workspace-uuid", "ref": "workspace:9", "name": "pool", "agent": "codex",
                "enabled": True, "excluded_surface_ids": ["codex-a"],
                "excluded_surface_reasons": {"codex-a": {"reason": "manual exclusion"}},
            }]
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            model = SupervisorModel(config_path, client=Client())
            model.refresh(force=True)
            by_id = {row.surface_id: row for row in model.candidates}
            self.assertEqual(by_id["codex-a"].source, "workspace_excluded")
            self.assertTrue(by_id["codex-a"].paused)
            self.assertEqual(by_id["codex-a"].status_detail, "manual exclusion")
            self.assertEqual(by_id["missing-uuid"].source, "explicit")
            self.assertEqual(by_id["missing-uuid"].state, "missing")

    def test_tracked_rows_always_show_error_short_name_and_send_count(self):
        from cmux_supervisor_tui import error_label, send_label, watch_label

        idle = Candidate(
            {"surface_id": "idle", "ref": "surface:4", "workspace_ref": "workspace:1"},
            "explicit",
            "idle",
            "http_405",
            114,
            True,
        )
        self.assertEqual(watch_label(idle), "已暂停")
        self.assertEqual(error_label(idle), "405")
        self.assertEqual(send_label(idle), "114")
        watching = Candidate(
            {"surface_id": "go", "ref": "surface:77", "workspace_ref": "workspace:1"},
            "explicit",
            "idle",
            "rate_limit",
            4,
            False,
            agent_kind="codex",   # 监控中 requires a Codex actually running
        )
        self.assertEqual(watch_label(watching), "监控中")
        self.assertEqual(error_label(watching), "429")
        self.assertEqual(send_label(watching), "4")

    def test_active_recovery_shows_current_error_and_episode_attempts(self):
        from cmux_supervisor_tui import error_label, send_label

        for state in ("recoverable_error", "awaiting_transition"):
            row = Candidate(
                {"surface_id": state, "ref": "surface:1", "workspace_ref": "workspace:1"},
                "workspace_rule",
                state,
                "high_demand",
                7,
                False,
            )
            self.assertEqual(error_label(row), "高需求")
            self.assertEqual(send_label(row), "7")
        self.assertEqual(state_label("awaiting_transition"), "等待恢复")

    def test_untracked_hides_error_and_send_count(self):
        from cmux_supervisor_tui import error_label, send_label, watch_label, screen_label

        row = Candidate(
            {"surface_id": "x", "ref": "surface:1", "workspace_ref": "workspace:1"},
            "untracked",
            "untracked",
            "http_405",
            4,
            False,
        )
        self.assertEqual(watch_label(row), "未登记")
        self.assertEqual(screen_label(row), "—")
        self.assertEqual(error_label(row), "—")
        self.assertEqual(send_label(row), "—")

    def test_filtering_and_manual_codex_selection(self):
        from cmux_supervisor_tui import filter_candidates, next_filter, selected_action_hint

        rows = [
            Candidate({"surface_id": "a", "ref": "surface:1", "workspace_ref": "workspace:1"}, "explicit", "idle", "-", 0, False),
            Candidate({"surface_id": "b", "ref": "surface:2", "workspace_ref": "workspace:3"}, "untracked", "unknown", "-", 0, False, agent_kind="codex", process_summary="Codex"),
            Candidate({"surface_id": "c", "ref": "surface:44", "workspace_ref": "workspace:9"}, "workspace_rule", "working", "-", 1, False),
        ]
        watched = filter_candidates(rows, "watched")
        self.assertEqual([item.surface_id for item in watched], ["a", "c"])
        self.assertEqual([item.surface_id for item in filter_candidates(rows, "untracked")], ["b"])
        self.assertEqual([item.surface_id for item in filter_candidates(rows, "all", "workspace:3")], ["b"])
        self.assertEqual(next_filter("all"), "watched")
        self.assertIn("只加这一路", selected_action_hint(rows[1]))
        self.assertIn("取消整个 workspace:9", selected_action_hint(rows[2]))

    def test_supervisor_discovers_all_main_surfaces_and_labels_them(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            model = SupervisorModel(config_path, client=_DiscoveryClient())
            model.refresh(force=True)
            rows = {row.ref: row for row in model.candidates}
            self.assertEqual(set(rows), {"surface:59", "surface:60"})
            self.assertEqual(rows["surface:59"].agent_kind, "codex")
            self.assertEqual(rows["surface:60"].agent_kind, "claude")
            # Discovery never registers: both stay untracked until the user acts.
            self.assertEqual(rows["surface:59"].source, "untracked")
            self.assertEqual(rows["surface:60"].source, "untracked")

    def test_add_always_uses_track_surface_and_waives_only_after_confirmation(self):
        from cmux_supervisor_tui import confirm_prompt

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            model = SupervisorModel(config_path, client=_DiscoveryClient())
            model.refresh(force=True)
            rows = {row.ref: row for row in model.candidates}
            calls: list[list[str]] = []
            model.run_cli = lambda args: calls.append(list(args)) or ""

            model.mutate_selected(rows["surface:59"], "add")
            model.mutate_selected(rows["surface:60"], "add")

            # Single exit: never "add", so Dock stays excluded by find_main_surface.
            self.assertTrue(all(call[0] == "track-surface" for call in calls), calls)
            self.assertFalse(any("add" == call[0] for call in calls), calls)
            self.assertNotIn("--allow-non-codex", calls[0])
            self.assertEqual(len(calls), 2)
            self.assertIn("ws11-p24-s59", calls[0])
            self.assertIn("ws11-p24-s60", calls[1])
            # The waiver is only reachable through an explicit warning.
            self.assertIn("不是 Codex", confirm_prompt("add", rows["surface:60"]))

    def test_workspace_action_accepts_a_non_codex_row(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            model = SupervisorModel(config_path, client=_DiscoveryClient())
            model.refresh(force=True)
            rows = {row.ref: row for row in model.candidates}
            calls: list[list[str]] = []
            model.run_cli = lambda args: calls.append(list(args)) or ""
            model.mutate_selected(rows["surface:60"], "workspace")
            self.assertEqual(calls[0][:2], ["track-workspace", "workspace-11"])
            self.assertIn("Hermes", calls[0])

    def test_process_label_survives_ref_renumbering_between_tree_and_top(self):
        tree = _DiscoveryClient().tree()
        # cmux closed a surface between the tree and top calls, so every ref
        # shifted down by one while the UUIDs stayed put.
        top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": "surface-59", "ref": "surface:58",
             "processes": [{"kind": "process", "name": "codex", "path": "/bin/codex"}]},
            {"kind": "surface", "id": "surface-60", "ref": "surface:59",
             "processes": [{"kind": "process", "name": "claude", "path": "/bin/claude"}]},
        ]}]}]}
        classified = core.classify_surface_processes(top)
        records = {record["ref"]: record for record in core.main_surface_records(tree)}
        self.assertEqual(
            core.surface_process_label(classified, records["surface:59"])["agent_kind"], "codex")
        self.assertEqual(
            core.surface_process_label(classified, records["surface:60"])["agent_kind"], "claude")

    def test_main_surface_records_accepts_ref_only_tree_surfaces(self):
        tree = {"windows": [{"workspaces": [{
            "id": "ws-3", "ref": "workspace:3", "title": "ws3 title",
            "panes": [{"ref": "pane:6", "surfaces": [
                {"ref": "surface:81", "title": "cnm", "type": "terminal"},
            ]}],
        }]}]}
        records = core.main_surface_records(tree, allow_ref_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["surface_id"], "surface:81")
        self.assertEqual(records[0]["ref"], "surface:81")
        self.assertEqual(records[0]["workspace_title"], "ws3 title")

        # The opt-in is display-only.  Watcher discovery must keep rejecting a
        # ref without a stable UUID so a stale/renumbered surface is never sent
        # input merely because the Supervisor can render it.
        self.assertEqual(core.main_surface_records(tree), [])

    def test_dry_run_and_stop_all_both_confirm_before_disabling_sends(self):
        from cmux_supervisor_tui import confirm_prompt

        dummy = Candidate({"ref": "surface:1", "workspace_ref": "workspace:1"}, "explicit", "idle", "-", 0, False)
        self.assertIn("不再自动续跑", confirm_prompt("dry-run", dummy))
        self.assertIn("全局停发", confirm_prompt("stop-all", dummy))

    def test_top_line_is_reversed_when_sending_is_off(self):
        import curses

        import cmux_supervisor_tui as tui

        class FakeScreen:
            def __init__(self):
                self.writes = []

            def erase(self):
                pass

            def getmaxyx(self):
                return 24, 120

            def addnstr(self, y, x, text, width, attr=0):
                self.writes.append((y, text, attr))

            def refresh(self):
                pass

        def top_line(mode, paused=False):
            model = SupervisorModel.__new__(SupervisorModel)
            model.config = {"mode": mode, "global_paused": paused}
            model.candidates = []
            model.online = True
            model.error = ""
            # _draw() reads the junk row from this client and nothing else; a
            # ctl path that does not exist keeps the fixture deterministic and
            # spawns no subprocess.
            model.janitor = tui.JanitorClient(Path("/nonexistent-ctl"))
            # Same contract for the three-component summary row: __new__ skips
            # __init__, so the draw path's stack client is injected here too.  A
            # nonexistent ctl keeps it deterministic and spawns no subprocess.
            model.stack = tui.StackClient(Path("/nonexistent-ctl"))
            # And again for the 协作 column: a nonexistent marker directory
            # reads as "no collaboration", so the fixture never depends on
            # whatever /tmp holds on the machine running the tests.
            model.collab = tui.CollabClient(Path("/nonexistent-collab"))
            screen = FakeScreen()
            tui._draw(screen, model, [], 0, "all", "", "")
            return next(item for item in screen.writes if item[0] == 0)

        _, armed_text, armed_attr = top_line("armed")
        self.assertNotIn("按 A 开启发", armed_text)
        self.assertFalse(armed_attr & curses.A_REVERSE)

        for mode, paused in (("dry-run", False), ("armed", True)):
            _, text, attr = top_line(mode, paused)
            self.assertIn("不会自动续跑，按 A 开启发", text)
            self.assertTrue(attr & curses.A_REVERSE, (mode, paused))

    def test_lines_are_truncated_by_columns_not_code_points(self):
        import cmux_supervisor_tui as tui

        drawn: list[str] = []

        class Screen:
            def addnstr(self, y, x, text, n, attr=0):
                drawn.append(text)

        # 91 code points but 135 columns: the old code-point limit let this
        # overflow a 131-column window and wrap onto a second line.
        footer = "j/k移动  /查找  c清除  f筛选  a加单路  w授权整池  p暂停  r恢复  x删单路  u取消整池  A开启发  S全局停发  d全局停发(只观察)  R刷新  q退出"
        self.assertEqual(tui.display_width(footer), 135)
        self.assertEqual(len(footer), 91)
        tui._safe_addnstr(Screen(), 0, 0, footer, 130)
        self.assertLessEqual(tui.display_width(drawn[0]), 130)
        self.assertTrue(footer.startswith(drawn[0]))

    def test_hook_summary_exposes_global_health_and_sla_misses(self):
        import cmux_supervisor_tui as tui

        class FakeScreen:
            def __init__(self):
                self.writes = []

            def erase(self):
                pass

            def getmaxyx(self):
                return 24, 160

            def addnstr(self, y, x, text, width, attr=0):
                self.writes.append((y, text, attr))

            def refresh(self):
                pass

        model = SupervisorModel.__new__(SupervisorModel)
        model.config = {"mode": "armed", "global_paused": False}
        model.hook_config = {"status": "healthy", "healthy": True}
        model.candidates = [Candidate(
            {"surface_id": "surface-a", "ref": "surface:1"},
            "explicit", "claude_hook_waiting", "-", 0, False,
            agent_kind="claude", hook_health="healthy",
            hook_live_sends=6, hook_sla_misses=1,
        )]
        model.online = True
        model.error = ""
        model.janitor = tui.JanitorClient(Path("/nonexistent-ctl"))
        # Same contract for the three-component summary row: __new__ skips
        # __init__, so the draw path's stack client is injected here too.  A
        # nonexistent ctl keeps it deterministic and spawns no subprocess.
        model.stack = tui.StackClient(Path("/nonexistent-ctl"))
        # And again for the 协作 column: a nonexistent marker directory reads
        # as "no collaboration", keeping the fixture machine-independent.
        model.collab = tui.CollabClient(Path("/nonexistent-collab"))
        screen = FakeScreen()
        tui._draw(screen, model, [], 0, "all", "", "")
        hook_text = next(text for row, text, _ in screen.writes if row == 2)
        self.assertIn("Hook配置 healthy", hook_text)
        self.assertIn("1正常", hook_text)
        self.assertIn("SLA 1/6超时", hook_text)

    def test_layout_rows_never_overlap(self):
        from cmux_supervisor_tui import BOTTOM_ROWS, MIN_HEIGHT, TOP_ROWS, layout

        # MIN_HEIGHT is layout()'s domain, so the old
        # ``if height > TOP_ROWS + BOTTOM_ROWS`` exemption is gone: inside the
        # domain every assertion holds unconditionally.  That exemption used to
        # skip four collisions at height 12, and _draw() no longer calls
        # layout() there at all -- it renders the compact frame, which
        # test_short_windows_render_a_bounded_compact_frame covers.
        self.assertEqual(MIN_HEIGHT, TOP_ROWS + 1 + BOTTOM_ROWS)
        for height in (MIN_HEIGHT, 16, 24, 40, 119):
            at = layout(height)
            top = [at["title"], at["counts"], at["hook"], at["context"],
                   at["junk"], at["stack"], at["top_rule"], at["header"]]
            self.assertEqual(top, sorted(set(top)), height)
            self.assertEqual(len(set(top)), TOP_ROWS, height)
            bottom = [at["focus_rule"], at["focus"], at["focus_keys"],
                      at["message"], at["keys_rule"], at["keys1"], at["keys2"]]
            self.assertEqual(bottom, sorted(bottom), height)
            self.assertEqual(len(set(bottom)), len(bottom), height)
            self.assertGreaterEqual(at["visible"], 1, height)
            # The table must not run into the focus block.  Stated against the
            # block constants, not against focus_rule: focus_rule is defined as
            # max(first_row + visible, ...), so comparing the two would hold for
            # every conceivable input and could never catch an overrun.
            self.assertEqual(at["visible"], height - TOP_ROWS - BOTTOM_ROWS, height)
            self.assertLess(at["first_row"] + at["visible"] - 1, at["focus_rule"],
                            height)
            # Every row layout() hands out must be a real row of the window.
            for key, row in at.items():
                if key == "visible":
                    continue
                self.assertGreaterEqual(row, 0, (height, key))
                self.assertLess(row, height, (height, key))

    def test_rules_and_separators_are_ascii_only(self):
        import unicodedata

        from cmux_supervisor_tui import GLOBAL_KEYS_1, GLOBAL_KEYS_2, rule

        for char in ("=", "-"):
            line = rule(char, 40)
            self.assertEqual(len(line), 40)
            self.assertEqual(display_width_of(line), 40)
        # Ambiguous-width glyphs must not reach a padded column.
        for name, width, _ in __import__("cmux_supervisor_tui").ROW_COLUMNS:
            self.assertFalse(
                any(unicodedata.east_asian_width(c) == "A" for c in name), name)
        for keys in (GLOBAL_KEYS_1, GLOBAL_KEYS_2):
            self.assertLess(display_width_of(keys), 100, keys)

    def test_colors_degrade_when_terminal_has_none(self):
        import cmux_supervisor_tui as tui

        with mock.patch.object(tui.curses, "has_colors", return_value=False):
            tui.init_colors()
        self.assertEqual(tui.attr("watching"), 0)
        self.assertEqual(tui.attr("untracked"), tui.curses.A_DIM)
        row = Candidate({"surface_id": "a", "ref": "surface:1", "workspace_ref": "workspace:1"},
                        "untracked", "untracked", "-", 0, False)
        self.assertEqual(tui.row_attr(row), tui.curses.A_DIM)

    def test_focus_block_carries_state_and_only_usable_keys(self):
        from cmux_supervisor_tui import focus_summary, selected_action_hint

        paused = Candidate({"surface_id": "p", "ref": "surface:4", "workspace_ref": "workspace:1",
                            "pane_ref": "pane:1"}, "explicit", "idle", "http_405", 114, True)
        summary = focus_summary(paused)
        self.assertIn("ws1/p1/s4", summary)
        self.assertIn("已暂停", summary)
        self.assertIn("405", summary)
        self.assertIn("114", summary)
        keys = selected_action_hint(paused)
        self.assertIn("r 恢复监控", keys)
        self.assertIn("下一轮重新判定", keys)
        self.assertNotIn("p 暂停", keys)
        self.assertNotIn("A 开启发", keys)

    def test_idling_row_hint_says_what_to_do_and_never_offers_x_for_pool(self):
        from cmux_supervisor_tui import selected_action_hint

        # Registered, Codex exited, only a shell left: 空转.  The hint has to be
        # actionable, otherwise the row just reads "why is this still watched?".
        idling = Candidate({"surface_id": "s", "ref": "surface:4", "workspace_ref": "workspace:1",
                            "pane_ref": "pane:1"}, "explicit", "idle", "http_405", 114, False,
                           agent_kind="shell", process_summary="zsh")
        keys = selected_action_hint(idling)
        self.assertIn("没有 Codex 可救", keys)
        self.assertIn("x 删掉这一路登记", keys)
        self.assertIn("等 Codex 回来", keys)

        # A registered Claude pane is also 空转, and the wording must not claim
        # Codex "exited" from a pane that never ran it.
        claude = Candidate({"surface_id": "s", "ref": "surface:104", "workspace_ref": "workspace:18",
                            "pane_ref": "pane:39"}, "explicit", "idle", "-", 0, False,
                           agent_kind="claude", process_summary="Claude")
        self.assertIn("没有 Codex 可救", selected_action_hint(claude))
        self.assertNotIn("已退出", selected_action_hint(claude))

        # A live Codex target keeps the plain hint: nothing to explain away.
        live = Candidate({"surface_id": "s", "ref": "surface:4", "workspace_ref": "workspace:1",
                          "pane_ref": "pane:1"}, "explicit", "idle", "http_405", 114, False,
                         agent_kind="codex", process_summary="codex")
        self.assertNotIn("没有 Codex 可救", selected_action_hint(live))

    def test_hook_gap_exhausted_stays_monitored_and_explains_the_bound(self):
        from cmux_supervisor_tui import is_idling, selected_action_hint

        exhausted = Candidate(
            {"surface_id": "s", "ref": "surface:43", "workspace_ref": "workspace:8",
             "pane_ref": "pane:19"},
            "explicit", "claude_hook_gap_exhausted", "claude_hook_gap_exhausted",
            114, False, agent_kind="claude", process_summary="Claude",
            repeat_warning=True, consecutive_resumes=9,
        )
        self.assertFalse(is_idling(exhausted))
        hint = selected_action_hint(exhausted)
        self.assertIn("续跑已达上限", hint)
        self.assertIn("保持监控", hint)
        self.assertNotIn("仍在自动恢复", hint)

        # Pool members have no single-row registration, so `x` must not appear.
        pool_idle = Candidate({"surface_id": "s", "ref": "surface:7", "workspace_ref": "workspace:9",
                               "pane_ref": "pane:20"}, "workspace_non_codex", "idle", "-", 0, False,
                              agent_kind="shell", process_summary="zsh")
        pool_keys = selected_action_hint(pool_idle)
        self.assertNotIn("x ", pool_keys)
        self.assertIn("u 取消整个 workspace:9", pool_keys)

    def test_cjk_columns_pad_by_terminal_width_not_code_points(self):
        from cmux_supervisor_tui import ROW_COLUMNS, display_width, header_text, pad

        self.assertEqual(display_width("运行中"), 6)
        self.assertEqual(display_width("监控中"), 6)
        self.assertEqual(display_width("已暂停"), 6)
        self.assertEqual(display_width("整池·非Codex"), 12)
        for text in ("运行中", "监控中", "已暂停", "Codex", ""):
            self.assertEqual(display_width(pad(text, 8)), 8, text)
        self.assertEqual(pad("超出宽度的很长文本", 6), "超出宽")
        # Header and rows are generated from one spec, so they cannot drift.
        header = header_text()
        for name, _, _ in ROW_COLUMNS:
            self.assertIn(name, header)
        self.assertIn("监控", header)
        self.assertIn("错误", header)
        self.assertIn("续跑", header)

    def test_remove_refuses_workspace_rule_and_untrack_uses_workspace_command(self):
        calls: list[list[str]] = []

        class Client:
            def tree(self):
                return {"windows": []}

            def top_all(self):
                return {}

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            model = SupervisorModel(config_path, client=Client())
            model.run_cli = lambda args: calls.append(args) or ""  # type: ignore[method-assign]
            row = Candidate(
                {"surface_id": "codex-a", "workspace_id": "workspace-uuid", "workspace_ref": "workspace:9", "ref": "surface:44"},
                "workspace_rule",
                "working",
                "-",
                0,
                False,
            )
            with self.assertRaisesRegex(RuntimeError, "只有单路登记能用 x"):
                model.mutate_selected(row, "remove")
            model.mutate_selected(row, "untrack_workspace")
            self.assertEqual(calls[-1][:2], ["untrack-workspace", "workspace-uuid"])
            explicit = Candidate(
                {"surface_id": "solo", "workspace_id": "ws-1", "workspace_ref": "workspace:1", "ref": "surface:4"},
                "explicit",
                "idle",
                "-",
                0,
                False,
            )
            with self.assertRaisesRegex(RuntimeError, "只有整池目标"):
                model.mutate_selected(explicit, "untrack_workspace")
            model.mutate_selected(explicit, "remove")
            self.assertEqual(calls[-1][:2], ["remove", "solo"])
            excluded = Candidate(
                {"surface_id": "codex-a", "workspace_id": "workspace-uuid", "workspace_ref": "workspace:9", "ref": "surface:44", "title": "cnm"},
                "workspace_excluded",
                "idle",
                "-",
                0,
                True,
            )
            with self.assertRaisesRegex(RuntimeError, "已监控目标不用再登记"):
                model.mutate_selected(excluded, "add")

    def test_howto_is_the_default_cli_output(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CMUX_SURFACE_ID", None)
            with redirect_stdout(output):
                self.assertEqual(core.cli([]), 0)
        text = output.getvalue()
        self.assertIn("dock-open", text)
        self.assertIn("全部主区 workspace / surface", text)
        self.assertIn("只有你选中并按 a 或 w 确认后", text)
        self.assertIn("由守护器按屏幕内容单独判定", text)
        self.assertIn("不用改 Python", text)

    def test_bare_cli_opens_tui_only_inside_cmux(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CMUX_SURFACE_ID", None)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(core.cli([]), 0)
            self.assertIn("日常用法", output.getvalue())
        with mock.patch.dict(os.environ, {"CMUX_SURFACE_ID": "surface-uuid"}):
            with mock.patch("cmux_supervisor_tui.run_tui", return_value=0) as run_tui:
                self.assertEqual(core.cli([]), 0)
            run_tui.assert_called_once()

    def test_error_column_falls_back_to_diagnostic(self):
        from cmux_supervisor_tui import diagnostic_detail, error_label, focus_summary

        row = Candidate(
            {"surface_id": "bad", "ref": "surface:1", "workspace_ref": "workspace:1"},
            "explicit",
            "incompatible",
            "-",
            0,
            True,
            status_detail="incompatible: composer cursor not verified",
        )
        # The 8-column table cell gets the short form; the focus line under the
        # table carries the full reason.
        self.assertEqual(error_label(row), "看不清")
        self.assertEqual(diagnostic_detail(row), "composer cursor not verified")
        self.assertIn("composer cursor not verified", focus_summary(row))

    def test_vanished_target_shows_its_reason_not_a_stale_error(self):
        from cmux_supervisor_tui import diagnostic_detail, error_label, focus_summary

        row = Candidate(
            {"surface_id": "gone", "ref": "surface:77", "workspace_ref": "?"},
            "explicit",
            "missing_or_error",
            "rate_limit",
            4,
            True,
            status_detail="cmux read-screen --workspace failed: Error: invalid_params: Surface is not a terminal",
        )
        # Not "429": that episode is history, the surface is what is broken now.
        self.assertEqual(error_label(row), "读不到")
        self.assertEqual(diagnostic_detail(row), "invalid_params: Surface is not a terminal")
        summary = focus_summary(row, "workspace:1")
        self.assertIn("Surface is not a terminal", summary)
        self.assertNotIn("错误 429", summary)
        # 监控 says 已暂停, 画面 says 无画面: three distinct facts, not one twice.
        self.assertIn("已暂停", summary)
        self.assertIn("无画面", summary)
        # The real workspace comes from the group, not from the "?" record.
        self.assertIn("ws1/p?/s77", summary)

    def test_workspace_confirm_mentions_live_codex_count(self):
        from cmux_supervisor_tui import confirm_prompt

        row = Candidate(
            {"surface_id": "a", "ref": "surface:1", "workspace_ref": "workspace:9", "workspace_title": "pool"},
            "untracked",
            "untracked",
            "-",
            0,
            False,
            agent_kind="codex",
        )
        text = confirm_prompt("workspace", row, live_codex=12)
        self.assertIn("12 个活 Codex", text)

    def test_idle_timeout_does_not_clear_status(self):
        import curses
        from cmux_supervisor_tui import next_status_after_key

        self.assertEqual(next_status_after_key(curses.ERR, "已取消"), "已取消")
        self.assertEqual(next_status_after_key(ord("j"), "已取消"), "")


def _cand(ws, pane, surface, source, *, paused=False, kind="codex", title="cnm", ws_title=""):
    return Candidate(
        {
            "surface_id": f"uuid-{surface}",
            "workspace_id": f"ws-{ws}",
            "workspace_ref": f"workspace:{ws}",
            "pane_ref": f"pane:{pane}",
            "ref": f"surface:{surface}",
            "title": title,
            "workspace_title": ws_title,
        },
        source, "idle", "-", 0, paused, agent_kind=kind, process_summary="Codex",
    )


def _tree_fixture():
    """ws1 has watched rows, ws2 has none, ws9 is a pool."""
    return [
        _cand(1, 1, 1, "untracked", ws_title="中转站维修session"),
        _cand(1, 1, 4, "explicit", ws_title="中转站维修session"),
        _cand(1, 1, 77, "explicit", paused=True, ws_title="中转站维修session"),
        _cand(2, 4, 23, "untracked", kind="other"),
        _cand(2, 4, 24, "untracked", kind="other"),
        _cand(9, 21, 50, "workspace_rule", ws_title="Anyrouter拉拉队"),
        _cand(9, 21, 51, "workspace_rule", ws_title="Anyrouter拉拉队"),
        _cand(11, 24, 59, "explicit", ws_title="Hermes更新升级"),
        _cand(11, 25, 60, "untracked", kind="other", ws_title="Hermes更新升级"),
    ]


class ColumnContractTests(unittest.TestCase):
    """One case per row of the 监控/程序/画面/错误/续跑 table in the README.

    The table is the contract: 程序 always names the CLI, 监控 carries the state.
    Anything that changes these columns has to update the table and this test
    together, which is what stopped 空转 from eating the program identity.
    """

    def row(self, candidate):
        from cmux_supervisor_tui import error_label, program_label, screen_label, send_label, watch_label
        return (watch_label(candidate), program_label(candidate),
                screen_label(candidate), error_label(candidate), send_label(candidate))

    def test_untracked_with_a_cli_running(self):
        for kind, label in (("codex", "Codex"), ("claude", "Claude"),
                            ("grok", "grok"), ("copilot", "Copilot")):
            self.assertEqual(self.row(_cand(1, 1, 1, "untracked", kind=kind)),
                             ("未登记", label, "—", "—", "—"), kind)

    def test_untracked_with_only_a_shell(self):
        self.assertEqual(self.row(_cand(1, 1, 1, "untracked", kind="shell")),
                         ("未登记", "shell", "—", "—", "—"))

    def test_registered_codex_running_normally(self):
        row = _cand(1, 1, 4, "explicit", kind="codex")
        row.state, row.error_type, row.send_count = "idle", "http_405", 114
        self.assertEqual(self.row(row), ("监控中", "Codex", "空闲", "405", "114"))

    def test_registered_codex_currently_stuck(self):
        row = _cand(1, 1, 4, "explicit", kind="codex")
        row.state, row.error_type, row.send_count = "recoverable_error", "rate_limit", 7
        self.assertEqual(self.row(row), ("监控中", "Codex", "待续跑", "429", "7"))

    def test_registered_but_a_different_cli_is_running(self):
        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state, row.error_type, row.send_count = "idle", "high_demand", 3
        # Claude stays visible; 画面 goes quiet because the fingerprint only
        # means something for a Codex UI; the error history is kept.
        self.assertEqual(self.row(row), ("空转", "Claude", "—", "高需求", "3"))

    def test_registered_but_the_cli_exited(self):
        row = _cand(1, 1, 4, "explicit", kind="shell")
        row.state, row.error_type, row.send_count = "idle", "http_405", 114
        self.assertEqual(self.row(row), ("空转", "shell", "—", "405", "114"))

    def test_registered_but_the_surface_vanished(self):
        row = _cand(1, 1, 77, "explicit", kind="unknown", paused=True)
        row.state, row.error_type, row.send_count = "missing_or_error", "rate_limit", 4
        row.status_detail = "invalid_params: Surface is not a terminal"
        self.assertEqual(self.row(row), ("已暂停", "未知", "无画面", "读不到", "4"))

    def test_paused_by_hand_keeps_reporting_reality(self):
        row = _cand(1, 1, 4, "explicit", kind="codex", paused=True)
        row.state, row.error_type, row.send_count = "working", "rate_limit", 9
        self.assertEqual(self.row(row), ("已暂停", "Codex", "运行中", "429", "9"))

    def test_pool_member_with_and_without_codex(self):
        live = _cand(9, 21, 50, "workspace_rule", kind="codex")
        live.state = "working"
        self.assertEqual(self.row(live)[:3], ("整池", "Codex", "运行中"))
        gone = _cand(9, 21, 51, "workspace_non_codex", kind="shell", paused=True)
        gone.state = "idle"
        self.assertEqual(self.row(gone)[:3], ("整池空转", "shell", "—"))

    def test_readme_column_prose_cannot_drift_from_the_labels(self):
        """The README used to describe the old behaviour in two places at once.

        One paragraph carried the contract table, another still said the 程序
        column shows 空转 and listed only Codex/Claude/gh.  Prose that contradicts
        the code is worse than no prose: it is what the next reader trusts.
        """
        from pathlib import Path

        from cmux_supervisor_tui import AGENT_LABELS, WATCH_LABELS

        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        bullets = {line.split("**")[1]: line
                   for line in readme.splitlines()
                   if line.startswith("- **") and "**：" in line}

        program = bullets["程序"]
        for label in AGENT_LABELS.values():
            self.assertIn(label, program, f"程序 列少写了取值 {label}")
        # 空转 may only be mentioned here as "it belongs to the 监控 column".
        self.assertNotIn("的行显示 `空转`", program)
        self.assertIn("「监控」列", program)

        watch = bullets["监控"]
        for label in WATCH_LABELS.values():
            self.assertIn(label, watch, f"监控 列少写了取值 {label}")

    def test_readme_documents_every_screen_state_the_daemon_can_report(self):
        """The 画面 column had no drift guard, so two values went undocumented.

        `非Codex` was missing from the day it was added, and `已过时` would have
        been too.  Deriving the set from ``classify_grid`` itself means a new
        screen verdict cannot be shipped without writing it down.
        """
        import re
        from pathlib import Path

        from cmux_supervisor_tui import STATE_LABELS

        root = Path(__file__).resolve().parent.parent
        daemon = (root / "cmux_codex_watch.py").read_text(encoding="utf-8")

        def kinds_in(name: str) -> set[str]:
            start = daemon.index(f"def {name}(")
            end = daemon.index("\ndef ", start + 1)
            return set(re.findall(r'ScreenState\(\s*"([a-z_]+)"', daemon[start:end]))

        kinds = kinds_in("classify_grid") | kinds_in("classify_claude_grid")
        self.assertIn("recoverable_error", kinds)
        self.assertIn("error_superseded", kinds)
        self.assertIn("claude_stopped", kinds)

        readme = (root / "README.md").read_text(encoding="utf-8")
        bullet = next(line for line in readme.splitlines()
                      if line.startswith("- **画面**"))
        # Only the slash enumeration counts, not the prose after it.  Matching
        # the whole line let 已过时 sit in an explanatory clause while the list a
        # reader actually scans stopped at 非Codex -- and the guard stayed green.
        enumeration = bullet.split("。")[0]
        for kind in sorted(kinds):
            label = STATE_LABELS[kind]
            self.assertIn(label, enumeration,
                          f"画面 列的取值枚举里少写了 {label}（状态 {kind}）")

    def test_superseded_error_is_not_confused_with_a_healthy_session(self):
        """Gate 3 chooses not to rescue; that choice has to be visible.

        Reporting a superseded banner as plain 空闲 would make a wrong
        suppression indistinguishable from a genuinely healthy Codex, which is
        the one failure mode of this gate that could strand a stalled session.
        """
        from cmux_supervisor_tui import STATE_LABELS, is_idling, program_label, screen_label

        row = _cand(9, 20, 49, "explicit", kind="codex")
        row.state, row.error_type, row.send_count = "error_superseded", "high_demand", 180
        self.assertNotEqual(screen_label(row), STATE_LABELS["idle"])
        self.assertEqual(screen_label(row), "已过时")
        # Codex is running, so this is not the 空转 bucket.
        self.assertFalse(is_idling(row))
        self.assertEqual(program_label(row), "Codex")

    def test_claude_observed_is_visible_on_an_idling_row(self):
        """Registered Claude + switch off must not render as a blank 画面.

        is_idling is true for any non-Codex agent, and the 画面 column used to
        write — for every idling row.  That would hide the one fact this
        state exists to show: we see Claude and we are choosing not to send.
        """
        from cmux_supervisor_tui import is_idling, program_label, screen_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state = "claude_observed"
        self.assertTrue(is_idling(row))
        self.assertEqual(program_label(row), "Claude")
        self.assertEqual(screen_label(row), "Claude关")

    def test_claude_stopped_is_a_visible_send_state_not_an_idling_dash(self):
        from cmux_supervisor_tui import is_idling, screen_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state = "claude_stopped"
        row.error_type = "claude_stopped"
        self.assertFalse(is_idling(row))
        self.assertEqual(screen_label(row), "待续跑")

    def test_claude_completed_stays_visible_and_monitored(self):
        from cmux_supervisor_tui import is_idling, screen_label, watch_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state = "claude_completed"
        self.assertFalse(is_idling(row))
        self.assertEqual(watch_label(row), "监控中")
        self.assertEqual(screen_label(row), "已完成")

    def test_claude_input_guard_is_visible_and_monitored(self):
        from cmux_supervisor_tui import is_idling, screen_label, watch_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state = "claude_input_guard"
        self.assertFalse(is_idling(row))
        self.assertEqual(watch_label(row), "监控中")
        self.assertEqual(screen_label(row), "输入保护")

    def test_claude_pending_input_is_a_visible_live_state(self):
        from cmux_supervisor_tui import is_idling, screen_label, watch_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state = "claude_pending_input"
        self.assertFalse(is_idling(row))
        self.assertEqual(watch_label(row), "监控中")
        self.assertEqual(screen_label(row), "已续跑")

    def test_claude_needs_human_is_visible_when_paused(self):
        from cmux_supervisor_tui import error_label, is_idling, screen_label, watch_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state = "claude_needs_human"
        row.paused = True
        row.status_detail = "Claude futile loop; need human"
        self.assertFalse(is_idling(row))
        self.assertEqual(watch_label(row), "已暂停")
        self.assertEqual(screen_label(row), "需人工")
        self.assertEqual(error_label(row), "需人工")

    def test_claude_send_states_are_not_drawn_as_idling(self):
        from cmux_supervisor_tui import error_label, is_idling, screen_label, watch_label

        row = _cand(18, 39, 104, "explicit", kind="claude")
        row.state, row.error_type, row.send_count = "recoverable_error", "claude_503", 1
        self.assertFalse(is_idling(row))
        self.assertEqual(watch_label(row), "监控中")
        self.assertEqual(screen_label(row), "待续跑")
        self.assertEqual(error_label(row), "503")

        row.state, row.error_type = "working", "claude_retry"
        self.assertFalse(is_idling(row))
        self.assertEqual(screen_label(row), "运行中")

    def test_readme_says_refresh_does_not_authorize(self):
        """发现 ≠ 授权 is the project's red line; R only rescans."""
        from pathlib import Path

        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("`R` 只是重扫，不会登记任何东西", readme)


class GroupedViewTests(unittest.TestCase):
    def test_vanished_member_does_not_rename_its_whole_group(self):
        from cmux_supervisor_tui import build_view_rows, group_identity, location_text

        # A target whose surface is gone comes back through the config-only
        # fallback, and config never stored workspace_ref, so its record says
        # "?".  Paused rows sort first, so reading the label off the first member
        # used to rename all of ws1 to "ws?".
        gone = Candidate(
            {"surface_id": "uuid-77", "workspace_id": "ws-1", "workspace_ref": "?",
             "ref": "surface:77", "title": "cnm"},
            "explicit", "missing_or_error", "rate_limit", 4, True,
            status_detail="Surface is not a terminal",
        )
        candidates = [gone, _cand(1, 1, 4, "explicit", ws_title="中转站维修session")]
        self.assertEqual(group_identity(candidates), ("workspace:1", "中转站维修session"))
        rows = build_view_rows(candidates, set(), "all")
        header = next(row for row in rows if row.kind == "group")
        self.assertEqual(header.workspace_ref, "workspace:1")
        self.assertEqual(header.workspace_title, "中转站维修session")
        vanished = next(row for row in rows if row.candidate is gone)
        self.assertEqual(location_text(vanished.candidate.record, vanished.workspace_ref), "ws1/p?/s77")

    def test_group_identity_reports_unknown_only_when_every_member_is(self):
        from cmux_supervisor_tui import group_identity

        orphan = Candidate(
            {"surface_id": "uuid-9", "workspace_id": "ws-?", "workspace_ref": "?", "ref": "surface:9"},
            "explicit", "missing", "-", 0, True,
        )
        self.assertEqual(group_identity([orphan]), ("?", ""))

    def test_group_identity_keeps_title_from_a_later_member(self):
        from cmux_supervisor_tui import group_identity

        first = Candidate(
            {"surface_id": "uuid-1", "workspace_id": "ws-7",
             "workspace_ref": "workspace:7", "workspace_title": ""},
            "explicit", "idle", "-", 0, False,
        )
        second = Candidate(
            {"surface_id": "uuid-2", "workspace_id": "ws-7",
             "workspace_ref": "workspace:7", "workspace_title": "Stata shared"},
            "untracked", "unknown", "-", 0, False,
        )
        self.assertEqual(group_identity([first, second]),
                         ("workspace:7", "Stata shared"))

    def test_suggested_surface_keeps_its_workspace_expanded(self):
        from cmux_supervisor_tui import build_view_rows, default_collapsed, initial_cursor_key

        candidates = _tree_fixture()
        # ws2 has nothing watched, so it folds away by default…
        self.assertIn("ws-2", default_collapsed(candidates))
        # …unless that is where cmux pointed us, in which case the cursor must
        # have a visible row to land on.
        collapsed = default_collapsed(candidates, "uuid-23")
        self.assertNotIn("ws-2", collapsed)
        rows = build_view_rows(candidates, collapsed, "all")
        self.assertEqual(initial_cursor_key(rows, "uuid-23"), "s:uuid-23")

    def test_registered_row_with_no_codex_reads_as_idling(self):
        from cmux_supervisor_tui import focus_summary, is_idling, program_label, watch_label

        # s4's real shape: registered, unpaused, but only zsh left in the pane.
        stalled = _cand(1, 1, 4, "explicit", kind="other")
        self.assertTrue(is_idling(stalled))
        # 程序 keeps the identity; the idling state moved to the 监控 column.
        self.assertEqual(program_label(stalled), "其他")
        self.assertEqual(watch_label(stalled), "空转")
        self.assertIn("没有 Codex 在跑", focus_summary(stalled))

        live = _cand(1, 1, 4, "explicit", kind="codex")
        self.assertFalse(is_idling(live))
        self.assertEqual(program_label(live), "Codex")
        self.assertNotIn("没有 Codex 在跑", focus_summary(live))

        # An untracked pane is not "idling"; it was never registered.
        untracked = _cand(1, 2, 9, "untracked", kind="other")
        self.assertFalse(is_idling(untracked))
        self.assertEqual(program_label(untracked), "其他")
        self.assertEqual(watch_label(untracked), "未登记")

        # A registered Claude pane must still read as Claude, not as a state.
        claude = _cand(18, 39, 104, "explicit", kind="claude")
        self.assertEqual(program_label(claude), "Claude")
        self.assertEqual(watch_label(claude), "空转")

        # A pool member whose Codex exited counts as idling, and a paused target
        # is reported as paused rather than idling.
        self.assertTrue(is_idling(_cand(9, 21, 50, "workspace_non_codex", kind="other", paused=True)))
        self.assertFalse(is_idling(_cand(1, 1, 77, "explicit", kind="codex", paused=True)))

    def test_top_counts_split_idling_out_of_watching(self):
        from cmux_supervisor_tui import SupervisorModel

        model = SupervisorModel.__new__(SupervisorModel)
        model.candidates = [
            _cand(1, 1, 4, "explicit", kind="other"),          # 空转
            _cand(1, 1, 5, "explicit", kind="codex"),          # 监控中
            _cand(1, 1, 77, "explicit", kind="codex", paused=True),   # 已暂停
            _cand(9, 21, 50, "workspace_rule", kind="codex"),  # 监控中
            _cand(9, 21, 51, "workspace_non_codex", kind="other", paused=True),  # 空转
            _cand(2, 4, 23, "untracked", kind="other"),        # 未登记
        ]
        counts = model.counts()
        self.assertEqual(counts["watching"], 2)
        self.assertEqual(counts["idling"], 2)
        self.assertEqual(counts["paused"], 1)
        self.assertEqual(counts["untracked"], 1)
        # Every registered row lands in exactly one bucket.
        self.assertEqual(counts["watching"] + counts["idling"] + counts["paused"], counts["watched"])
        self.assertEqual(counts["watched"] + counts["untracked"], counts["all"])

    def test_vanished_target_uses_its_persisted_pane(self):
        from cmux_supervisor_tui import location_text

        # Registered after the position fields existed: pane survives the surface.
        self.assertEqual(
            location_text({"workspace_ref": "workspace:1", "pane_ref": "pane:1", "ref": "surface:77"}),
            "ws1/p1/s77")
        # Registered before: nothing to recover, so "?" is honest.
        self.assertEqual(
            location_text({"workspace_ref": "?", "pane_ref": "", "ref": "surface:77"}, "workspace:1"),
            "ws1/p?/s77")

    def test_collapsed_state_follows_workspaces_appearing_and_vanishing(self):
        from cmux_supervisor_tui import default_collapsed, reconcile_collapsed

        candidates = _tree_fixture()
        collapsed = default_collapsed(candidates)
        self.assertEqual(collapsed, {"ws-2"})

        # A workspace created after startup used to be absent from the set and
        # therefore rendered expanded even with nothing watched.
        grown = candidates + [_cand(12, 26, 61, "untracked"), _cand(12, 26, 62, "untracked")]
        collapsed = reconcile_collapsed(collapsed, set(), grown)
        self.assertEqual(collapsed, {"ws-2", "ws-12"})

        # A workspace that later gains a target must open on its own.
        promoted = [c for c in grown if c.record["workspace_id"] != "ws-12"]
        promoted += [_cand(12, 26, 61, "explicit"), _cand(12, 26, 62, "untracked")]
        collapsed = reconcile_collapsed(collapsed, set(), promoted)
        self.assertNotIn("ws-12", collapsed)

        # A workspace that disappears leaves no stale entry behind.
        shrunk = [c for c in promoted if c.record["workspace_id"] != "ws-2"]
        collapsed = reconcile_collapsed(collapsed, set(), shrunk)
        self.assertNotIn("ws-2", collapsed)

    def test_manual_fold_survives_every_refresh(self):
        from cmux_supervisor_tui import reconcile_collapsed

        candidates = _tree_fixture()
        # ws1 has watched rows, so the default rule wants it open; the user
        # folded it anyway and a 5-second refresh must not undo that.
        collapsed = {"ws-1"}
        manual = {"ws-1"}
        for _ in range(3):
            collapsed = reconcile_collapsed(collapsed, manual, candidates)
        self.assertIn("ws-1", collapsed)
        # Likewise a pool the user opened by hand stays open.
        collapsed, manual = set(), {"ws-2"}
        for _ in range(3):
            collapsed = reconcile_collapsed(collapsed, manual, candidates)
        self.assertNotIn("ws-2", collapsed)

    def test_manual_refresh_reports_what_changed(self):
        from cmux_supervisor_tui import refresh_report, surface_ids

        before = {"a", "b"}
        self.assertIn("无变化", refresh_report(before, {"a", "b"}, 2))
        grew = refresh_report(before, {"a", "b", "c"}, 3)
        self.assertIn("新增 1", grew)
        self.assertIn("3 个 workspace / 3 路", grew)
        shrank = refresh_report(before, {"a"}, 1)
        self.assertIn("消失 1", shrank)
        self.assertEqual(surface_ids(_tree_fixture()) >= {"uuid-4", "uuid-77"}, True)

    def test_collapsed_group_says_it_is_folded(self):
        from cmux_supervisor_tui import build_view_rows, default_collapsed, group_row_text

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, default_collapsed(candidates), "all")
        folded = next(r for r in rows if r.kind == "group" and r.collapsed)
        opened = next(r for r in rows if r.kind == "group" and not r.collapsed)
        text = group_row_text(folded, 120)
        # "+" alone read as "nothing detected here"; the count and the key must
        # both be on the line.
        self.assertIn("未登记", text)
        self.assertIn("已折叠，Tab 展开", text)
        self.assertNotIn("已折叠", group_row_text(opened, 120))

    def test_window_never_leaves_a_blank_tail(self):
        from cmux_supervisor_tui import window_start

        # Short list: no scrolling at all.
        self.assertEqual(window_start(0, 10, 20), 0)
        self.assertEqual(window_start(9, 10, 20), 0)
        # Cursor near the end must not leave 70 blank lines below two rows.
        self.assertEqual(window_start(74, 75, 73), 2)
        self.assertEqual(window_start(74, 75, 73) + 73, 75)
        # Cursor in the middle gets centred, and the top never goes negative.
        self.assertEqual(window_start(0, 100, 20), 0)
        self.assertEqual(window_start(50, 100, 20), 40)
        self.assertEqual(window_start(99, 100, 20), 80)
        for index in range(100):
            start = window_start(index, 100, 20)
            self.assertLessEqual(start, index)
            self.assertLess(index, start + 20)

    def test_wrong_pool_key_is_refused_before_the_confirm_box(self):
        from cmux_supervisor_tui import build_view_rows, group_action_error

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, set(), "all")
        plain = next(r for r in rows if r.kind == "group" and r.workspace_ref == "workspace:1")
        pool = next(r for r in rows if r.kind == "group" and r.workspace_ref == "workspace:9")
        member = next(r for r in rows if r.kind == "member")

        # Applicable keys pass through to the confirmation box.
        self.assertEqual(group_action_error(plain, "workspace"), "")
        self.assertEqual(group_action_error(pool, "untrack_workspace"), "")
        # Inapplicable ones are refused here, so the prompt never lies.
        self.assertIn("已经是整池授权", group_action_error(pool, "workspace"))
        self.assertIn("没有整池授权", group_action_error(plain, "untrack_workspace"))
        self.assertIn("组头", group_action_error(plain, "add"))
        self.assertEqual(member.kind, "member")

    def test_default_collapse_folds_only_pools_with_nothing_watched(self):
        from cmux_supervisor_tui import default_collapsed

        self.assertEqual(default_collapsed(_tree_fixture()), {"ws-2"})

    def test_rows_are_grouped_and_watched_members_come_first(self):
        from cmux_supervisor_tui import build_view_rows, default_collapsed

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, default_collapsed(candidates), "all")
        shape = [(row.kind, row.workspace_ref, row.candidate.ref if row.candidate else "") for row in rows]
        self.assertEqual(shape, [
            ("group", "workspace:1", ""),
            ("member", "workspace:1", "surface:4"),
            ("member", "workspace:1", "surface:77"),
            ("member", "workspace:1", "surface:1"),
            ("group", "workspace:2", ""),          # folded: no members follow
            ("group", "workspace:9", ""),
            ("member", "workspace:9", "surface:50"),
            ("member", "workspace:9", "surface:51"),
            ("group", "workspace:11", ""),
            ("member", "workspace:11", "surface:59"),
            ("member", "workspace:11", "surface:60"),
        ])

    def test_group_header_counts_ignore_the_active_filter(self):
        from cmux_supervisor_tui import build_view_rows

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, set(), "watched")
        ws1 = next(row for row in rows if row.kind == "group" and row.workspace_ref == "workspace:1")
        # One watching, one paused, one untracked — the untracked one is filtered
        # out of the body but must still be counted in the header.
        self.assertEqual(ws1.counts["watching"], 1)
        self.assertEqual(ws1.counts["paused"], 1)
        self.assertEqual(ws1.counts["untracked"], 1)
        self.assertNotIn("surface:1", [row.candidate.ref for row in rows if row.candidate])

    def test_search_matches_the_position_shown_on_screen_and_expands(self):
        from cmux_supervisor_tui import build_view_rows

        candidates = _tree_fixture()
        for needle in ("ws11", "s59", "workspace:11", "Hermes更新升级", "ws11/p24/s59"):
            rows = build_view_rows(candidates, {"ws-11"}, "all", needle)
            found = [row.candidate.ref for row in rows if row.candidate]
            self.assertIn("surface:59", found, needle)
        # ws2 is collapsed by default but a hit must still be reachable.
        rows = build_view_rows(candidates, {"ws-2"}, "all", "s23")
        self.assertEqual([row.candidate.ref for row in rows if row.candidate], ["surface:23"])

    def test_cursor_prefers_suggested_then_watched_then_first_row(self):
        from cmux_supervisor_tui import build_view_rows, initial_cursor_key

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, set(), "all")
        self.assertEqual(initial_cursor_key(rows, "uuid-60"), "s:uuid-60")
        # No suggestion: land on a watched row, never on ws1/p1/s1 untracked.
        self.assertEqual(initial_cursor_key(rows, ""), "s:uuid-4")
        self.assertEqual(initial_cursor_key([], ""), "")

    def test_cursor_key_survives_collapse_and_falls_back_when_gone(self):
        from cmux_supervisor_tui import build_view_rows, index_for_key

        candidates = _tree_fixture()
        expanded = build_view_rows(candidates, set(), "all")
        collapsed = build_view_rows(candidates, {"ws-9"}, "all")
        key = "s:uuid-59"
        self.assertEqual(expanded[index_for_key(expanded, key)].key, key)
        self.assertEqual(collapsed[index_for_key(collapsed, key)].key, key)
        # A row that disappeared falls back to the watched-row rule, not index 0.
        self.assertEqual(collapsed[index_for_key(collapsed, "s:uuid-gone")].key, "s:uuid-4")

    def test_group_rows_only_take_pool_actions(self):
        from cmux_supervisor_tui import (
            build_view_rows, row_action_hint, workspace_confirm_prompt,
        )

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, set(), "all")
        plain = next(row for row in rows if row.kind == "group" and row.workspace_ref == "workspace:1")
        pool = next(row for row in rows if row.kind == "group" and row.workspace_ref == "workspace:9")
        self.assertIn("w 授权整个 workspace:1", row_action_hint(plain))
        self.assertIn("u 取消整个 workspace:9", row_action_hint(pool))
        # The wording follows the key pressed, not the pool's current state, so a
        # mis-keyed action can never be described as its opposite.
        self.assertIn("这一池现在有", workspace_confirm_prompt(plain, "workspace"))
        self.assertIn("不再自动续跑", workspace_confirm_prompt(pool, "untrack_workspace"))
        self.assertIn("不再自动续跑", workspace_confirm_prompt(plain, "untrack_workspace"))
        self.assertIn("这一池现在有", workspace_confirm_prompt(pool, "workspace"))

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(core.default_config()), encoding="utf-8")
            model = SupervisorModel.__new__(SupervisorModel)
            model.config_path = config_path
            calls: list[list[str]] = []
            model.run_cli = lambda args: calls.append(list(args)) or ""
            model.mutate_workspace(plain, "workspace")
            self.assertEqual(calls[0][:2], ["track-workspace", "ws-1"])
            model.mutate_workspace(pool, "untrack_workspace")
            self.assertEqual(calls[1], ["untrack-workspace", "ws-9"])
            with self.assertRaisesRegex(RuntimeError, "已经是整池授权"):
                model.mutate_workspace(pool, "workspace")
            with self.assertRaisesRegex(RuntimeError, "没有整池授权"):
                model.mutate_workspace(plain, "untrack_workspace")
            with self.assertRaisesRegex(RuntimeError, "组头只支持"):
                model.mutate_workspace(plain, "add")

    def test_group_header_line_fits_and_shows_pool_badge(self):
        from cmux_supervisor_tui import build_view_rows, display_width, group_row_text

        candidates = _tree_fixture()
        rows = build_view_rows(candidates, {"ws-2"}, "all")
        pool = next(row for row in rows if row.workspace_ref == "workspace:9")
        folded = next(row for row in rows if row.workspace_ref == "workspace:2")
        text = group_row_text(pool, 100)
        self.assertLessEqual(display_width(text), 100)
        self.assertIn("整池授权", text)
        self.assertIn("ws9", text)
        self.assertNotIn("workspace:9", text)
        self.assertTrue(group_row_text(folded, 100).lstrip().startswith("+"), "折叠应显示 +")
        self.assertTrue(group_row_text(pool, 100).lstrip().startswith("-"), "展开应显示 -")

    def test_context_column_prioritises_compaction_failure_without_pausing(self):
        from cmux_supervisor_tui import context_label, error_label

        candidate = Candidate(
            record={"surface_id": "s", "ref": "surface:67"},
            source="explicit",
            state="composer_busy",
            error_type="claude_api",
            send_count=4,
            paused=False,
            agent_kind="claude",
            context_status="stalled",
            context_percent=100,
            compaction_percent=11,
        )
        self.assertEqual(context_label(candidate), "失败!")
        self.assertEqual(error_label(candidate), "压缩失败")
        self.assertFalse(candidate.paused)

        candidate.context_status = "warning"
        candidate.context_percent = 86
        self.assertEqual(context_label(candidate), "86!")
        self.assertEqual(error_label(candidate), "上下文高")
        candidate.state = "recoverable_error"
        self.assertEqual(error_label(candidate), "API")


class DurationDisplayTests(unittest.TestCase):
    """Durations in the table, and the promise that they never shift a column.

    ``unprotected_sec``/``unreadable_sec``/``context.age_sec`` reached --json
    first, so the screen still showed a bare "缺失" for a pane that had been
    unprotected ~14.5h (surface:74, 2026-08-24).  These cases pin the wording
    and, more importantly, the column arithmetic: a clipped "无保护12h" would
    render as "无保护1" and understate twelve hours as one.
    """

    def claude_row(self, **kwargs):
        row = _cand(9, 20, 74, "explicit", kind="claude")
        row.state = "claude_hook_missing"
        for key, value in kwargs.items():
            setattr(row, key, value)
        return row

    def test_compact_duration_is_ascii_and_at_most_three_columns(self):
        from cmux_supervisor_tui import compact_duration, display_width as width

        for seconds, expected in (
            (0, ""), (30, "1m"), (3599, "59m"), (3600, "1h"),
            (52200, "14h"), (172800, "2d"), (300000, "3d"),
        ):
            self.assertEqual(compact_duration(seconds), expected, seconds)
        for seconds in (30, 3600, 52200, 300000):
            rendered = compact_duration(seconds)
            self.assertLessEqual(width(rendered), 3, rendered)
            self.assertTrue(rendered.isascii(), rendered)

    def test_hook_column_carries_the_unprotected_duration(self):
        from cmux_supervisor_tui import hook_label

        row = self.claude_row(hook_health="missing", unprotected_sec=52200.0)
        self.assertEqual(hook_label(row), "未验14h")
        row.unprotected_sec = 3600.0
        self.assertEqual(hook_label(row), "未验1h")

    def test_hook_column_omits_duration_for_healthy_and_grace_window(self):
        from cmux_supervisor_tui import hook_label

        healthy = self.claude_row(hook_health="healthy", unprotected_sec=0.0)
        self.assertEqual(hook_label(healthy), "正常")
        # unverified is a 30s grace window: a duration there is noise.
        grace = self.claude_row(hook_health="unverified", unprotected_sec=25.0)
        self.assertEqual(hook_label(grace), "待验证")

    def test_hook_column_drops_the_duration_rather_than_clipping_it(self):
        from cmux_supervisor_tui import HOOK_COLUMN_WIDTH, hook_label, display_width as width

        # 配置待核 is 8 columns, so a duration cannot fit in 8. The
        # label must stay intact instead of being cut mid-duration.
        row = self.claude_row(hook_health="legacy_override", unprotected_sec=43200.0)
        rendered = hook_label(row)
        self.assertEqual(rendered, "配置待核")
        self.assertLessEqual(width(rendered), HOOK_COLUMN_WIDTH)

    def test_context_column_dates_a_stale_reading(self):
        from cmux_supervisor_tui import context_label

        fresh = self.claude_row(context_status="normal", context_percent=42,
                                context_age_sec=5.0)
        self.assertEqual(context_label(fresh), "42%")
        stale = self.claude_row(context_status="normal", context_percent=42,
                                context_age_sec=400.0)
        self.assertEqual(context_label(stale), "42%~6m")

    def test_error_column_dates_a_blind_viewport_when_it_fits(self):
        from cmux_supervisor_tui import error_label

        row = self.claude_row(state="incompatible", unreadable_sec=20340.0)
        self.assertEqual(error_label(row), "看不清5h")
        # 31m needs 9 columns in an 8-column cell, so the cell stays plain and
        # the focus line under the table carries the full wording instead.
        row.unreadable_sec = 1860.0
        self.assertEqual(error_label(row), "看不清")

    def test_focus_line_carries_durations_the_columns_cannot(self):
        from cmux_supervisor_tui import selected_action_hint

        missing = self.claude_row(hook_health="missing", unprotected_sec=52200.0)
        self.assertIn("14h", selected_action_hint(missing))
        legacy = self.claude_row(hook_health="legacy_override", unprotected_sec=43200.0)
        self.assertIn("12h", selected_action_hint(legacy))
        blind = self.claude_row(hook_health="healthy", state="incompatible",
                                unreadable_sec=1860.0)
        hint = selected_action_hint(blind)
        self.assertIn("31m", hint)
        self.assertIn("unknown", hint)
        stalled = self.claude_row(hook_health="healthy", context_status="stalled")
        stalled_hint = selected_action_hint(stalled)
        self.assertIn("不会自动 /compact", stalled_hint)
        self.assertIn("无需重新登记", stalled_hint)

    def test_every_padded_cell_keeps_its_exact_column_width(self):
        import unicodedata

        from cmux_supervisor_tui import (
            ROW_COLUMNS, context_label, display_width as width,
            error_label, hook_label, pad,
        )

        widths = {name: value for name, value, _ in ROW_COLUMNS}
        rows = (
            self.claude_row(hook_health="missing", unprotected_sec=52200.0,
                            context_status="normal", context_percent=42,
                            context_age_sec=400.0),
            self.claude_row(hook_health="legacy_override", unprotected_sec=43200.0,
                            context_status="warning", context_percent=86,
                            context_age_sec=1200.0),
            self.claude_row(hook_health="missing", unprotected_sec=20340.0,
                            state="incompatible", unreadable_sec=20340.0),
            self.claude_row(hook_health="healthy", context_status="compacting",
                            compaction_percent=15),
        )
        for row in rows:
            for label, column in ((hook_label(row), "Hook"),
                                  (context_label(row), "上下文"),
                                  (error_label(row), "错误")):
                self.assertEqual(width(pad(label, widths[column])), widths[column],
                                 f"{column}={label!r}")
                # East Asian "Ambiguous" characters are barred from padded
                # columns: their width is a judgement call, and a wrong guess
                # shifts every column after it.
                for char in label:
                    self.assertNotEqual(
                        unicodedata.east_asian_width(char), "A",
                        f"ambiguous char {char!r} in {column}={label!r}",
                    )


class HumanAuditOutputTests(unittest.TestCase):
    """The text audits must say *when* each reading was taken.

    ``context-audit`` printed one percentage per pane, so a stored 81% beside a
    live 95% (surface:104, 2026-08-24) read as a parser disagreement rather than
    as two sampling instants six hours apart.
    """

    def test_age_text_treats_zero_and_junk_as_no_sample(self):
        self.assertEqual(core._audit_age_text(0), "")
        self.assertEqual(core._audit_age_text(0.0), "")
        self.assertEqual(core._audit_age_text(-5), "")
        self.assertEqual(core._audit_age_text(None), "")
        self.assertEqual(core._audit_age_text(True), "")
        self.assertEqual(core._audit_age_text(45), "45s")
        self.assertEqual(core._audit_age_text(3600), "60m")
        self.assertEqual(core._audit_age_text(52200), "14h")

    def test_hook_audit_text_shows_unprotected_and_blind_durations(self):
        report = {
            "hook_config": {"status": "healthy"},
            "sla_summary": {"miss_count": 23, "live_send_count": 103},
            "live_count": 2,
            "live": [
                {
                    "workspace_ref": "workspace:9", "pane_ref": "pane:20",
                    "surface_ref": "surface:74", "short_id": "EA238362",
                    "registered": True, "paused": False,
                    "effective_hook_health": "missing",
                    "unprotected_sec": 52200.0, "unreadable_sec": 0.0,
                    "screen_state": "claude_stopped",
                    "runtime_state": "claude_hook_missing",
                    "process_pid": 75121, "sla": {"miss_count": 0},
                },
                {
                    "workspace_ref": "workspace:9", "pane_ref": "pane:21",
                    "surface_ref": "surface:72", "short_id": "9EE9E3A9",
                    "registered": True, "paused": False,
                    "effective_hook_health": "healthy",
                    "unprotected_sec": 0.0, "unreadable_sec": 20340.0,
                    "screen_state": "incompatible",
                    "runtime_state": "incompatible",
                    "process_pid": 23988, "sla": {"miss_count": 0},
                },
            ],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            core.print_claude_audit(report)
        out = buffer.getvalue()
        self.assertIn("无保护", out)
        self.assertIn("14h", out)
        self.assertIn("incompatible~5h", out)
        # A healthy pane must not be described as unprotected for zero seconds.
        self.assertNotIn("0s", out)
        self.assertIn("claude_stopped ", out)

    def test_context_audit_text_dates_the_stored_reading(self):
        report = {
            "live_count": 1,
            "live": [{
                "workspace_ref": "workspace:9", "surface_ref": "surface:104",
                "short_id": "04B000E3",
                "live_context": {
                    "percent": 95, "auto_compact_remaining_percent": 5,
                    "compaction_percent": None, "composer_kind": "empty",
                    "source": "fresh_replay", "age_sec": 0.0,
                },
                "context": {
                    "percent": 81, "status": "warning",
                    "source": "state", "age_sec": 21600.0,
                },
            }],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            core.print_context_audit(report)
        out = buffer.getvalue()
        self.assertIn("存档%", out)
        self.assertIn("95%", out)
        self.assertIn("81%~6h", out)

    def test_context_audit_text_marks_a_pane_that_was_never_sampled(self):
        report = {
            "live_count": 1,
            "live": [{
                "workspace_ref": "workspace:9", "surface_ref": "surface:74",
                "short_id": "EA238362",
                "live_context": {},
                "context": {"percent": None, "status": "unknown",
                            "source": "state", "age_sec": None},
            }],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            core.print_context_audit(report)
        out = buffer.getvalue()
        self.assertIn("?", out)
        self.assertNotIn("None", out)

    def test_audit_tables_keep_headers_and_status_icon(self):
        claude = {
            "hook_config": {"status": "healthy"},
            "sla_summary": {"miss_count": 0, "live_send_count": 0},
            "live_count": 0,
            "live": [],
        }
        context = {"live_count": 0, "live": []}
        hook_buffer = io.StringIO()
        with redirect_stdout(hook_buffer):
            core.print_claude_audit(claude)
        context_buffer = io.StringIO()
        with redirect_stdout(context_buffer):
            core.print_context_audit(context)
        self.assertIn("✓ Hook配置", hook_buffer.getvalue())
        self.assertIn("workspace/pane/surface", hook_buffer.getvalue())
        self.assertIn("workspace/surface", context_buffer.getvalue())



class ReadmeThresholdConsistencyTests(unittest.TestCase):
    """The stage-6 defect was documentation drift, so test the documentation.

    README stated 300s while the code used 120s.  A reader trusting either one
    was wrong half the time, and no test could tell: the number lived in prose.
    """

    def test_readme_states_the_threshold_the_code_uses(self):
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        # Match the number the README prints beside the constant's name.  The
        # earlier version of this test hard-coded a phrasing that no longer
        # existed, so it failed on a correct README -- a test that cannot
        # distinguish "doc is wrong" from "my regex is wrong" is worthless.
        matches = re.findall(r"CONTEXT_STALE_SEC`?\s*[（(]?\s*=?\s*(\d+)\s*秒?[)）]?", text)
        self.assertTrue(matches, "README must state the stale threshold")
        for value in matches:
            self.assertEqual(
                float(value), CONTEXT_STALE_SEC,
                f"README says {value}s but the code uses {CONTEXT_STALE_SEC}s",
            )

    def test_readme_does_not_still_claim_the_old_threshold(self):
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertNotIn("读数超过 300 秒才追加", text)


class ContextStaleBoundaryTests(unittest.TestCase):
    """The stale threshold is a documented contract, so pin both sides of it."""

    def _claude(self, **kwargs):
        cand = _cand(1, 1, 1, "explicit", kind="claude")
        cand.context_status = "normal"
        cand.context_percent = 42
        for key, value in kwargs.items():
            setattr(cand, key, value)
        return cand

    def test_threshold_is_one_hundred_twenty_seconds(self):
        # README claimed 300s while the code used 120s.  The number is a contract
        # with the reader, so it is asserted rather than left to prose.
        self.assertEqual(CONTEXT_STALE_SEC, 120.0)

    def test_one_second_below_threshold_shows_no_age(self):
        self.assertEqual(context_label(self._claude(context_age_sec=119.0)), "42%")

    def test_at_threshold_shows_age(self):
        label = context_label(self._claude(context_age_sec=120.0))
        self.assertTrue(label.startswith("42%~"), label)

    def test_fresh_reading_shows_no_age(self):
        self.assertEqual(context_label(self._claude(context_age_sec=0.0)), "42%")

    def test_age_suffix_uses_ascii_only(self):
        # East Asian Ambiguous glyphs are one column in some terminals and two in
        # others, so they must never appear inside a padded column.
        import unicodedata
        label = context_label(self._claude(context_age_sec=3600.0))
        for char in label:
            self.assertNotEqual(unicodedata.east_asian_width(char), "A", repr(label))


class DeferredHintTests(unittest.TestCase):
    """A parked Stop must be visible, and a paused blind pane must not lie."""

    def test_deferred_row_explains_the_pending_retry(self):
        cand = _cand(1, 1, 1, "explicit", kind="claude")
        cand.deferred_reason = "Claude composer is busy"
        cand.deferred_sec = 95.0
        hint = selected_action_hint(cand)
        self.assertIn("已挂起", hint)
        self.assertIn("不会重复发送", hint)

    def test_unpaused_blind_pane_promises_continued_monitoring(self):
        cand = _cand(1, 1, 1, "explicit", kind="claude")
        cand.unreadable_sec = 1860.0
        hint = selected_action_hint(cand)
        self.assertIn("仍持续监控", hint)
        self.assertIn("无需重新登记", hint)

    def test_paused_blind_pane_says_it_needs_a_human(self):
        # A paused target is skipped before its runtime is read, so its blind
        # clock cannot advance and no warning can fire.  Promising "still
        # monitoring" here would be false.
        cand = _cand(1, 1, 1, "explicit", paused=True, kind="claude")
        cand.unreadable_sec = 1860.0
        hint = selected_action_hint(cand)
        self.assertIn("暂停中不再轮询", hint)
        self.assertIn("需人工", hint)
        self.assertNotIn("仍持续监控", hint)

    def test_deferred_hint_fits_before_blind_hint(self):
        # A pane can be both; the actionable fact is the pending retry.
        cand = _cand(1, 1, 1, "explicit", kind="claude")
        cand.deferred_sec = 30.0
        cand.unreadable_sec = 600.0
        self.assertIn("已挂起", selected_action_hint(cand))

    def _model_with_runtime(self, directory, runtime):
        """Build the model the way the TUI does: from a real state.json on disk."""

        root = Path(directory)
        config = core.default_config()
        config["targets"] = [{
            "surface_id": "surface-60", "workspace_id": "workspace-11",
            "ref": "surface:60", "title_snapshot": "Claude", "name": "ws11-s60",
            "enabled": True, "paused": False,
        }]
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (root / "state.json").write_text(
            json.dumps({"surface-60": runtime}), encoding="utf-8",
        )
        model = SupervisorModel(config_path, client=_DiscoveryClient())
        model.refresh(force=True)
        return {row.surface_id: row for row in model.candidates}["surface-60"]

    def test_cleared_slot_keeps_its_reason_but_is_not_shown_as_deferred(self):
        """A reason without an event is an audit trace, not an occupied slot.

        ``_clear_claude_deferred`` deliberately keeps the last reason for
        forensics while nulling the event.  Production surface F3EE1EC3 sat in
        exactly this shape on 2026-08-25 (reason ``completion_reported``,
        ``claude_deferred_event`` null), and reading the reason field alone --
        which is what my own audit script did -- reports a parked Stop that does
        not exist.  The row must say nothing about a pending retry.
        """

        with tempfile.TemporaryDirectory() as directory:
            row = self._model_with_runtime(directory, {
                "state": "claude_completed",
                "claude_deferred_event": None,
                "claude_deferred_reason": "completion_reported",
                "claude_deferred_since": 0.0,
            })
            self.assertEqual(row.deferred_reason, "")
            self.assertEqual(row.deferred_sec, 0.0)
            self.assertNotIn("已挂起", selected_action_hint(row))

    def test_occupied_slot_is_shown_as_deferred_with_its_age(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self._model_with_runtime(directory, {
                "state": "claude_hook_waiting",
                "claude_deferred_event": {"event_id": "e-parked", "event_name": "Stop"},
                "claude_deferred_reason": "composer_busy",
                "claude_deferred_since": time.time() - 240.0,
            })
            self.assertEqual(row.deferred_reason, "composer_busy")
            self.assertGreater(row.deferred_sec, 200.0)
            self.assertIn("已挂起", selected_action_hint(row))

    def test_stale_since_without_event_cannot_fabricate_a_duration(self):
        # A leftover ``since`` from a previous defer must not resurrect the slot:
        # the event is the authority, and a stale timestamp would otherwise
        # render as an ever-growing "已挂起 Nh" on a surface with nothing parked.
        with tempfile.TemporaryDirectory() as directory:
            row = self._model_with_runtime(directory, {
                "state": "claude_completed",
                "claude_deferred_event": None,
                "claude_deferred_reason": "",
                "claude_deferred_since": time.time() - 36000.0,
            })
            self.assertEqual(row.deferred_sec, 0.0)
            self.assertNotIn("已挂起", selected_action_hint(row))


class StateLabelCompletenessTests(unittest.TestCase):
    """A new daemon state must not reach the 画面 column as raw English.

    ``state_label`` falls back to returning the state name itself, and the 画面
    column is 8 terminal columns wide, so an unlabelled ``claude_viewport_blind``
    rendered as ``claude_v`` -- and ``claude_deferred_expired`` as ``claude_d``.
    Alignment survived (pad clips), but the two are indistinguishable from each
    other and meaningless in a Chinese UI.  Found in review, not by a test,
    which is why this test exists.
    """

    # Every state the v3 work added or that reaches a Claude row.
    V3_STATES = (
        "claude_viewport_blind",
        "claude_deferred_expired",
        "claude_submit_pending",
        "claude_submit_unconfirmed",
        "cmux_unavailable",
    )

    def test_every_v3_state_has_a_short_label(self):
        for state in self.V3_STATES:
            self.assertIn(state, DETAIL_SHORT, f"{state} would render as raw English")
            self.assertIn(state, STATE_LABELS, f"{state} has no focus-line label")

    def test_short_labels_fit_the_screen_column(self):
        # Read the budget from the single source of truth rather than
        # hardcoding 8: if the column is ever widened, this test must follow.
        budget = next(w for name, w, _ in ROW_COLUMNS if name == "画面")
        for state in self.V3_STATES:
            label = DETAIL_SHORT[state]
            self.assertLessEqual(
                display_width_of(label), budget,
                f"{state} -> {label!r} is {display_width_of(label)} columns, budget {budget}",
            )

    def test_no_ambiguous_width_glyphs_in_padded_labels(self):
        # The module bars East Asian "Ambiguous" characters from padded columns:
        # their width is a judgement call, so a mis-guess shifts every column.
        import unicodedata as ud
        for source in (DETAIL_SHORT, STATE_LABELS):
            for state, label in source.items():
                bad = [c for c in label if ud.east_asian_width(c) == "A"]
                self.assertEqual(bad, [], f"{state} -> {label!r} contains {bad}")

    def test_rows_stay_aligned_across_the_new_states(self):
        """An over-long label must clip, never shift the columns after it.

        Built the way the renderer does: eight cell strings, one per column.
        Passing the Candidate itself silently produced a TypeError, so this
        mirrors the real call site instead of inventing a shape.
        """

        from cmux_supervisor_tui import (
            context_label, error_label, hook_label, location_text,
            program_label, screen_label, send_label, watch_label,
        )
        widths = set()
        for state in self.V3_STATES:
            row = self._row(state)
            cells = (
                watch_label(row), location_text(row.record, "workspace:7"),
                program_label(row), hook_label(row), context_label(row),
                screen_label(row), error_label(row), send_label(row),
            )
            self.assertEqual(len(cells), len(ROW_COLUMNS))
            widths.add(display_width_of(_row_text("  ", cells, "t")))
        self.assertEqual(len(widths), 1, f"row widths diverged: {sorted(widths)}")

    def _row(self, state):
        row = _cand(7, 14, 36, "explicit", kind="claude")
        row.state = state
        row.error_type = "claude_stopped"
        return row


class JunkPanelTests(unittest.TestCase):
    """The cmux-junk overview row.

    The panel is a pure reader of the janitor's *published* state.  It never
    walks ~/.cmuxterm, never parses the janitor's config.env, never stats its
    sentinels, and never removes a path: disposal belongs to the janitor script,
    which owns every safety gate.
    """

    # A snapshot in the shape JanitorClient hands to the draw path.  Healthy and
    # fully measured, so each test can spoil exactly one field.
    HEALTHY = {
        "available": True,
        "reason": "",
        "paused": False,
        "guard_tripped": False,
        "janitor_mode": "apply",
        "janitor_age_sec": 300.0,
        "guard_age_sec": 30.0,
        "guard_health": "healthy",
        "safety_complete": True,
        "candidate_counts": {"raw": 921, "eligible": 640, "selected": 500,
                             "disposed": 500, "protected": 3},
        "selected_bytes": 1810142610,
        "selected_precision": "estimated",
        "quarantine_count": 7,
        "quarantine_bytes": 28472817149,
        "quarantine_precision": "exact",
        "quarantine_keep_hours": 48,
        "measured_at": "2026-08-28T05:00:00Z",
    }

    def test_format_bytes_keeps_binary_and_decimal_apart(self):
        from cmux_supervisor_tui import format_bytes

        # 1 GiB is 1024^3 bytes; the same count is ~1.07 GB in decimal.  Mixing
        # the two is how a reclaimed-space figure silently drifts ~7%.
        gib = 1024 ** 3
        self.assertEqual(format_bytes(gib, binary=True), "1.0GiB")
        self.assertEqual(format_bytes(gib, binary=False), "1.1GB")
        self.assertEqual(format_bytes(0), "0B")

    def test_junk_line_separates_candidates_from_quarantine(self):
        from cmux_supervisor_tui import junk_line

        line = junk_line(self.HEALTHY)
        # Three funnel stages, each labelled: a backlog that is present but
        # deliberately protected must not read as one the janitor is failing on.
        self.assertIn("原始921", line)
        self.assertIn("合格640", line)
        self.assertIn("选中500", line)
        # Quarantine is reported on its own and never folded into the candidate
        # figure.  Summing them (the old `tracked = reclaimable + quarantine`)
        # made the row grow while the janitor was working well.
        self.assertIn("隔离 7批", line)
        self.assertIn("留48h", line)
        combined = 1810142610 + 28472817149
        from cmux_supervisor_tui import format_bytes
        self.assertNotIn(format_bytes(combined), line)

    def test_estimated_bytes_are_marked_and_unmeasured_never_reads_zero(self):
        from cmux_supervisor_tui import junk_line

        line = junk_line(self.HEALTHY)
        # Byte totals carry their own precision; an extrapolated figure says so.
        self.assertIn("~1.7GiB", line)
        self.assertNotIn("~26.5GiB", line)   # quarantine was exact

        blind = {**self.HEALTHY, "selected_bytes": None,
                 "selected_precision": "unknown",
                 "candidate_counts": {"raw": None, "eligible": None,
                                      "selected": None}}
        line = junk_line(blind)
        self.assertIn("未测量", line)
        self.assertIn("?", line)
        # The specific failure this guards: an unmeasured figure printed as a
        # confident 0.
        self.assertNotIn("0B", line)

    def test_junk_line_says_so_when_the_controller_cannot_be_read(self):
        from cmux_supervisor_tui import junk_line

        line = junk_line({"available": False, "reason": "控制器未安装"})
        self.assertIn("无法读取", line)
        self.assertIn("控制器未安装", line)
        # A cold start, before the first fetch lands, still renders a row.
        self.assertTrue(junk_line({}))

    def test_paused_row_reports_age_without_implying_a_fault(self):
        from cmux_supervisor_tui import junk_line

        line = junk_line({**self.HEALTHY, "paused": True,
                          "janitor_age_sec": 3 * 3600 + 25 * 60})
        self.assertIn("已暂停", line)
        # A paused janitor's state goes stale on purpose, so the age is stated
        # as information.
        self.assertIn("最后测量3小时25分钟前", line)

    def test_junk_line_uses_ascii_separators_and_measures_cjk_as_two_columns(self):
        from cmux_supervisor_tui import display_width, junk_line

        line = junk_line(self.HEALTHY)
        # Same ASCII pipe the hook/context rows use: a fullwidth separator here
        # would mis-measure and wrap the row.
        self.assertIn(" | ", line)
        self.assertNotIn("｜", line)

        # A real width check.  The previous assertion compared display_width
        # against itself in both branches of a conditional and could not fail;
        # this states the property directly: the row contains CJK, so its
        # column count must exceed its character count by exactly the number of
        # wide glyphs.
        wide = sum(1 for ch in line
                   if unicodedata.east_asian_width(ch) in ("W", "F"))
        self.assertGreater(wide, 0, line)
        self.assertEqual(display_width(line), len(line) + wide, line)
        self.assertLess(display_width(line), 200, line)

    def test_alarm_fires_only_on_conditions_the_operator_must_act_on(self):
        from cmux_supervisor_tui import junk_is_alarming

        self.assertFalse(junk_is_alarming(self.HEALTHY))

        # Each of these means sweeping is not happening as configured.
        for field, value in (
            ("available", False),
            ("guard_tripped", True),
            ("guard_health", "tripped"),
            ("guard_health", None),
            ("paused", True),
            ("janitor_mode", "unknown"),
            ("janitor_mode", "absent"),
            ("safety_complete", False),
        ):
            with self.subTest(field=field, value=value):
                self.assertTrue(junk_is_alarming({**self.HEALTHY, field: value}))

    def test_alarm_ignores_backlog_size_but_not_staleness(self):
        from cmux_supervisor_tui import (GUARD_FRESH_LIMIT_SEC,
                                         JANITOR_FRESH_LIMIT_SEC,
                                         junk_is_alarming)

        # Size is deliberately not a trigger.  The janitor sweeps every 30
        # minutes unconditionally, so a large backlog is either about to be
        # swept or the janitor has stopped -- and "has it stopped" is what the
        # other conditions already report.
        huge = {**self.HEALTHY, "selected_bytes": 900 * 1024 ** 3,
                "quarantine_bytes": 900 * 1024 ** 3,
                "candidate_counts": {"raw": 90000, "eligible": 90000,
                                     "selected": 500}}
        self.assertFalse(junk_is_alarming(huge))

        # Missing a cycle is a trigger, and so is a document that never arrived.
        self.assertTrue(junk_is_alarming(
            {**self.HEALTHY, "janitor_age_sec": JANITOR_FRESH_LIMIT_SEC + 1}))
        self.assertTrue(junk_is_alarming(
            {**self.HEALTHY, "guard_age_sec": GUARD_FRESH_LIMIT_SEC + 1}))
        self.assertTrue(junk_is_alarming({**self.HEALTHY, "janitor_age_sec": None}))
        self.assertTrue(junk_is_alarming({**self.HEALTHY, "guard_age_sec": None}))

    def test_layout_reserves_a_junk_row_without_overlap(self):
        from cmux_supervisor_tui import (BOTTOM_ROWS, MIN_HEIGHT, TOP_ROWS,
                                         layout)

        # Same removal as test_layout_rows_never_overlap: no height exemption.
        for height in (MIN_HEIGHT, 16, 24, 40, 119):
            at = layout(height)
            # The junk row is part of the top block; include every top key so a
            # future insertion cannot quietly collide (the older version of this
            # assertion omitted "context" and would not have caught that).
            top = [at["title"], at["counts"], at["hook"], at["context"],
                   at["junk"], at["stack"], at["top_rule"], at["header"]]
            self.assertEqual(top, sorted(set(top)), height)
            self.assertEqual(len(set(top)), TOP_ROWS, height)
            self.assertLess(at["junk"], at["top_rule"], height)
            # The data block has to fit in what the two fixed blocks leave, and
            # the last data row must sit strictly above the focus separator.
            # Asserting `first_row + visible <= focus_rule` instead would be
            # unfalsifiable: focus_rule is defined as max(first_row + visible,
            # ...), so that comparison holds for every possible input -- it
            # would pass even if `visible` overran the window.  Both bounds
            # below are stated in terms of the two block constants, so a
            # miscounted reservation fails them.
            self.assertEqual(at["visible"], height - TOP_ROWS - BOTTOM_ROWS,
                             height)
            self.assertLess(at["first_row"] + at["visible"] - 1,
                            at["focus_rule"], height)
            self.assertLess(at["first_row"] + at["visible"] - 1, height, height)

    def test_row_fits_common_terminal_widths(self):
        from cmux_supervisor_tui import clip_to_width, display_width, junk_line

        # Narrow terminals clip rather than wrap; wrapping would push every row
        # below it off its layout line.
        line = junk_line(self.HEALTHY)
        for width in (80, 120, 160):
            clipped = clip_to_width(line, width)
            self.assertLessEqual(display_width(clipped), width, width)


class JanitorClientTests(unittest.TestCase):
    """The panel's only route to janitor state.

    Two properties matter more than the parsing: the draw path must never block
    on it, and it must never read a janitor-private file directly.
    """

    def _ctl(self, root: Path, body: str) -> Path:
        """A stand-in cmux-janitorctl.  Real ctl is exercised in test_janitor."""

        ctl = root / "cmux-janitorctl"
        ctl.write_text("import sys\n" + body, encoding="utf-8")
        return ctl

    def test_status_document_becomes_a_snapshot(self):
        import cmux_supervisor_tui as tui

        document = {
            "control": {"paused": False, "guard_tripped": False,
                        "config_mode": "apply"},
            "janitor": {
                "mode": "apply", "safety_complete": True, "age_sec": 120,
                "observed_at": "2026-08-28T05:00:00Z",
                "counts": {"raw_sb": 4, "raw_staging": 917, "eligible": 640,
                           "selected": 500, "disposed": 500, "protected": 3},
                "selected_bytes": {"value": 1810142610, "precision": "estimated"},
            },
            "quarantine": {"batch_count": 7,
                           "bytes": {"value": 28472817149, "precision": "exact"},
                           "keep_hours": 48},
            "guard": {"health": "healthy", "age_sec": 30},
        }
        snap = tui._snapshot_from_status(document)
        self.assertTrue(snap["available"])
        # raw is the two candidate families added together; both were measured.
        self.assertEqual(snap["candidate_counts"]["raw"], 921)
        self.assertEqual(snap["selected_bytes"], 1810142610)
        self.assertEqual(snap["selected_precision"], "estimated")
        self.assertEqual(snap["quarantine_keep_hours"], 48)
        self.assertEqual(snap["guard_health"], "healthy")

    def test_unmeasured_numbers_stay_none_instead_of_zero(self):
        import cmux_supervisor_tui as tui

        # NaN, infinities, booleans, negatives and strings all mean "no
        # measurement".  Rendering any of them as 0 would report an empty
        # backlog while the disk fills.
        for bad in (float("nan"), float("inf"), float("-inf"), True, -5, "12", None):
            with self.subTest(value=bad):
                snap = tui._snapshot_from_status({
                    "janitor": {"counts": {"raw_sb": bad, "raw_staging": 1,
                                           "eligible": bad},
                                "selected_bytes": {"value": bad,
                                                   "precision": "exact"}},
                    "quarantine": {"batch_count": bad},
                })
                self.assertIsNone(snap["candidate_counts"]["raw"])
                self.assertIsNone(snap["candidate_counts"]["eligible"])
                self.assertIsNone(snap["selected_bytes"])
                self.assertEqual(snap["selected_precision"], "unknown")
                self.assertIsNone(snap["quarantine_count"])

    def test_positive_infinity_cannot_disable_the_alarm(self):
        import cmux_supervisor_tui as tui

        # The bug this closes: `inf` used to survive into a threshold and read
        # as "no limit".  There is no threshold any more, and inf is rejected at
        # the boundary as well, so both layers refuse it.
        snap = tui._snapshot_from_status({
            "janitor": {"age_sec": float("inf"), "safety_complete": True,
                        "mode": "apply"},
            "guard": {"health": "healthy", "age_sec": float("inf")},
        })
        self.assertIsNone(snap["janitor_age_sec"])
        self.assertIsNone(snap["guard_age_sec"])
        self.assertTrue(tui.junk_is_alarming(snap))

    def test_missing_controller_is_reported_not_crashed(self):
        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            client = tui.JanitorClient(Path(tmp) / "absent-ctl")
            snap = client.fetch()
        self.assertFalse(snap["available"])
        self.assertIn("未安装", snap["reason"])
        # Every numeric field is absent, not zero.
        self.assertIsNone(snap["selected_bytes"])
        self.assertIsNone(snap["quarantine_count"])

    def test_controller_failures_each_land_in_the_same_shape(self):
        import cmux_supervisor_tui as tui

        cases = {
            "print('{')": "无法解析",                     # truncated JSON
            "print('[]')": "不是对象",                    # valid JSON, wrong type
            "sys.stderr.write('boom\\n'); sys.exit(3)": "拒绝",  # ctl fail-closed
        }
        with tempfile.TemporaryDirectory() as tmp:
            for body, expected in cases.items():
                with self.subTest(body=body):
                    ctl = self._ctl(Path(tmp), body)
                    snap = tui.JanitorClient(ctl).fetch()
                    self.assertFalse(snap["available"])
                    self.assertIn(expected, snap["reason"])

    def test_a_slow_controller_does_not_block_the_draw_path(self):
        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(Path(tmp), "import time; time.sleep(30)\n")
            client = tui.JanitorClient(ctl, timeout_sec=45.0)
            client.maybe_refresh(force=True)

            # The draw path calls snapshot() once per pass.  With a 30s ctl in
            # flight, 200 passes must still be effectively instant.
            started = time.monotonic()
            for _ in range(200):
                snap = client.snapshot()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, elapsed)
            # Before the first fetch lands it reports "not read yet" rather than
            # inventing zeros.
            self.assertFalse(snap["available"])

    def test_only_one_fetch_runs_at_a_time(self):
        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(Path(tmp), "import time; time.sleep(5)\n")
            client = tui.JanitorClient(ctl, timeout_sec=10.0)
            self.assertTrue(client.maybe_refresh(force=True))
            # A redraw storm must not spawn a ctl per keystroke.
            for _ in range(50):
                self.assertFalse(client.maybe_refresh(force=True))

    def test_cached_snapshot_is_reused_inside_the_ttl(self):
        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._ctl(Path(tmp), "print('{}')\n")
            client = tui.JanitorClient(ctl, ttl_sec=60.0)
            self.assertTrue(client.maybe_refresh(force=True))
            self.assertTrue(client.wait_for_refresh(30.0))
            fetched_at = client._fetched_at
            self.assertGreater(fetched_at, 0.0)
            # Inside the TTL: no new fetch.  This row redraws on every keystroke.
            self.assertFalse(client.maybe_refresh(now=fetched_at + 1.0))
            # Past it: refetch.
            self.assertTrue(client.maybe_refresh(now=fetched_at + 61.0))
            self.assertTrue(client.wait_for_refresh(30.0))



class StackClientTests(unittest.TestCase):
    """StackClient 的 rc 处理、三态、absent 与白名单投影。

    这一组的核心是 R3 §3.4 那条修正：cmux-stack 在 degraded 时 rc=1，而
    JanitorClient.fetch() 的判据是 ``rc not in (0,)`` -> absent。照抄会让
    「JSON 完全有效、状态完全可读」渲染成「读不到」，且现网 overall 正是
    degraded，所以是 100% 复现而非边角情形。
    """

    def _fake_ctl(self, tmp, payload, rc=0, sleep=0.0):
        """一个假 cmux-stack：吐给定 stdout、返回给定 rc。"""
        import json as _json
        ctl = Path(tmp) / "fake-cmux-stack"
        body = payload if isinstance(payload, str) else _json.dumps(payload)
        ctl.write_text(
            "import sys, time\n"
            f"time.sleep({float(sleep)})\n"
            f"sys.stdout.write({body!r})\n"
            f"sys.exit({int(rc)})\n",
            encoding="utf-8")
        return ctl

    def _doc(self, overall="degraded", **over):
        doc = {
            "schema_version": 1, "controller": "cmux-stack",
            "observed_at": "2026-08-30T00:00:00Z", "read_only": True,
            "overall": overall, "probed_count": 3, "requested_count": 3,
            "unhealthy": ["profiles"], "unknown": [],
            "components": {
                "watcher": {"component": "watcher", "installed": True, "probe_ok": True,
                            "healthy": True, "launchd_loaded": True, "reason": None,
                            "pid": 4242, "pid_alive": True, "mode": "armed",
                            "source_matches_disk": True},
                "janitor": {"component": "janitor", "installed": True, "probe_ok": True,
                            "healthy": True, "launchd_loaded": True, "reason": None,
                            "guard_launchd_loaded": True, "guard_health": "healthy",
                            "paused": False},
                "profiles": {"component": "profiles", "installed": True, "probe_ok": True,
                             "healthy": False, "launchd_loaded": None,
                             "reason": "1 unhealthy profile(s)",
                             "profile_count": 17, "unhealthy_count": 1},
            },
        }
        doc.update(over)
        return doc

    # ---- rc 全谱：P0-1 回归闸门 ----

    def test_every_readable_rc_yields_a_usable_snapshot(self):
        import tempfile

        import cmux_supervisor_tui as tui

        for rc in (0, 1, 3, 4):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as tmp:
                ctl = self._fake_ctl(tmp, self._doc(), rc=rc)
                snap = tui.StackClient(ctl).fetch()
                self.assertTrue(snap["available"], f"rc={rc} 必须是有效读数")
                self.assertEqual(snap["overall"], "degraded")
                self.assertIsNone(snap["reason"])

    def test_degraded_rc_one_is_not_mistaken_for_an_unreadable_controller(self):
        """现网真值就是 rc=1；这条红了就说明退回了 JanitorClient 的判据。"""
        import tempfile

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._fake_ctl(tmp, self._doc(overall="degraded"), rc=1)
            snap = tui.StackClient(ctl).fetch()
            self.assertTrue(snap["available"])
            line = tui.stack_line(snap)
            self.assertNotIn("无法读取", line)
            self.assertIn("有异常", line)

    def test_usage_error_and_unparseable_output_degrade_to_absent(self):
        import tempfile

        import cmux_supervisor_tui as tui

        cases = (
            ("rc2 空输出", "", 2),
            ("rc2 有文字", "未知组件: watchr", 2),
            ("不可解析", "not json at all", 0),
            ("非对象", "[1, 2, 3]", 0),
            ("其它 rc", '{"overall": "ok"}', 9),
        )
        for label, out, rc in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                ctl = self._fake_ctl(tmp, out, rc=rc)
                snap = tui.StackClient(ctl).fetch()
                self.assertFalse(snap["available"], label)
                self.assertTrue(snap["reason"], f"{label} 必须给出原因")
                self.assertIsNone(snap["overall"])

    def test_missing_controller_says_so_instead_of_crashing(self):
        import cmux_supervisor_tui as tui

        snap = tui.StackClient(Path("/nonexistent-stack-ctl")).fetch()
        self.assertFalse(snap["available"])
        self.assertIn("未安装", snap["reason"])

    def test_a_timeout_lands_in_the_absent_shape(self):
        import tempfile

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._fake_ctl(tmp, self._doc(), rc=0, sleep=2.0)
            snap = tui.StackClient(ctl, timeout_sec=0.3).fetch()
            self.assertFalse(snap["available"])
            self.assertIn("超时", snap["reason"])

    # ---- 超时排序：P0-2 回归闸门 ----

    def test_the_external_timeout_exceeds_the_controllers_serial_worst_case(self):
        """常量算术闸门。150 > 3x10(launchctl 硬编码) + 3x30(子探针)。

        选 45s 之类的值会在 launchctl 挂住时判「超时」，而被调方仍在自己的
        预算内工作——单场景绿、故障场景错。
        """
        import cmux_supervisor_tui as tui

        launchctl_worst = 3 * 10
        probe_worst = 3 * 30
        self.assertGreater(tui.STACK_CTL_TIMEOUT_SEC, launchctl_worst + probe_worst,
                           "外部超时必须大于被调方串行最坏上界")

    # ---- 三态 ----

    def test_launchd_loaded_keeps_all_three_states(self):
        import tempfile

        import cmux_supervisor_tui as tui

        for raw, expected, text in ((True, True, "已加载"),
                                    (False, False, "未加载"),
                                    (None, None, "未测量")):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                doc = self._doc()
                doc["components"]["watcher"]["launchd_loaded"] = raw
                ctl = self._fake_ctl(tmp, doc, rc=1)
                snap = tui.StackClient(ctl).fetch()
                got = snap["components"]["watcher"]["launchd_loaded"]
                self.assertIs(got, expected)
                self.assertEqual(tui._loaded_text(got), text)

    def test_a_non_boolean_healthy_is_not_coerced_to_false(self):
        import tempfile

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            doc = self._doc()
            doc["components"]["watcher"]["healthy"] = None
            ctl = self._fake_ctl(tmp, doc, rc=1)
            snap = tui.StackClient(ctl).fetch()
            self.assertIsNone(snap["components"]["watcher"]["healthy"])

    # ---- 白名单 ----

    def test_an_upstream_field_cannot_reach_the_projection_by_default(self):
        """白名单而非过滤：上游加字段默认不出现。"""
        import tempfile

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            doc = self._doc()
            doc["components"]["watcher"]["brand_new_upstream_field"] = "SENTINEL-NEW"
            doc["top_level_newcomer"] = "SENTINEL-TOP"
            ctl = self._fake_ctl(tmp, doc, rc=1)
            snap = tui.StackClient(ctl).fetch()
            self.assertNotIn("brand_new_upstream_field", snap["components"]["watcher"])
            self.assertNotIn("top_level_newcomer", snap)
            blob = repr(snap) + tui.stack_line(snap) + "".join(tui.stack_page_lines(snap))
            self.assertNotIn("SENTINEL-NEW", blob)
            self.assertNotIn("SENTINEL-TOP", blob)

    def test_no_credential_shaped_value_survives_into_the_rendered_text(self):
        import tempfile

        import cmux_supervisor_tui as tui

        token = "sk-ant-STACKPANEL-must-never-render-7c1f"
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._doc()
            doc["components"]["profiles"]["source_sha256"] = "c" * 64
            doc["components"]["profiles"]["leaked"] = token
            doc["components"]["watcher"]["raw_argv"] = ["--settings", token]
            ctl = self._fake_ctl(tmp, doc, rc=1)
            snap = tui.StackClient(ctl).fetch()
            blob = repr(snap) + tui.stack_line(snap) + "".join(tui.stack_page_lines(snap))
            for needle in (token, token[-4:], "source_sha256", "raw_argv"):
                self.assertNotIn(needle, blob, needle)

    # ---- 决策 A：不重复 Janitor 判定 ----

    def test_the_summary_row_carries_no_janitor_detail(self):
        """Janitor 细节的唯一真源是 junk 行与 G 页。"""
        import tempfile

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            doc = self._doc()
            ctl = self._fake_ctl(tmp, doc, rc=1)
            snap = tui.StackClient(ctl).fetch()
            line = tui.stack_line(snap)
            # 这些是 junk 行/G 页的词汇，stack 汇总行不得出现
            for forbidden in ("候选", "隔离", "批次", "待清", "保留", "手动清扫"):
                self.assertNotIn(forbidden, line, forbidden)

    def test_the_stack_page_points_at_the_g_page_for_janitor_detail(self):
        import tempfile

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as tmp:
            ctl = self._fake_ctl(tmp, self._doc(), rc=1)
            snap = tui.StackClient(ctl).fetch()
            body = "".join(tui.stack_page_lines(snap))
            self.assertIn("G", body, "v 页必须把 Janitor 细节指回 G 页")

    # ---- 按键划界：ccp-new 一键直达，launchd 三条仍只是文本 ----

    def test_the_stack_page_binds_ccp_new_and_no_launchd_verb(self):
        """页面的约束是【按动词】划的，不是【按页面】划的。

        早先这里断言 STACK_KEYS 含「只读」。那条约束对 launchd 三条动词是对的，
        对 ccp-new 是错的——它只改 ~/.claude-profiles/*.json、自带 .bak/_trash 回滚，
        却被要求「看字→退出→重敲」，四步做一步的事。所以现在断言两件事同时成立：
        ccp-new 有键，launchd 动词没有。
        """
        import cmux_supervisor_tui as tui

        self.assertIn("e 进 ccp-new", tui.STACK_KEYS)
        # launchd 动词不得出现在按键提示里（它们只能作为可复制文本）
        self.assertNotIn("--apply", tui.STACK_KEYS)
        for verb in ("up", "down", "install"):
            self.assertNotIn(f" {verb} ", tui.STACK_KEYS, f"{verb} 不该是按键")

    def test_launchd_write_commands_are_still_only_copyable_text(self):
        """`cmux-stack up --apply` 等仍必须只是字符串，不得从面板执行。"""
        import cmux_supervisor_tui as tui

        joined = "\n".join(tui.STACK_WRITE_HINTS)
        # 三条 launchd 动词仍在提示里供复制
        for needle in ("up --apply", "down --component", "install --apply"):
            self.assertIn(needle, joined, needle)
        # 但 profile 那行已被按键取代，不该再让用户去敲
        self.assertNotIn("cmux-stack profile", joined,
                         "profile 已有按键，不该再作为「请复制」文本留着")

    def test_the_stack_page_executes_nothing_but_the_handoff(self):
        """v 页只许通过具名交接原语执行前台程序，不得自己起子进程。

        禁用集必须含 `call`：`subprocess.call` 与 `subprocess.run` 是同族，
        早先的名单收了 run/Popen/check_output/system 却漏了 call，
        一个 subprocess.call 就能穿过这道「专门防这件事」的断言。
        """
        import ast
        import inspect

        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._stack_page)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                self.assertNotIn(
                    name,
                    {"run", "call", "Popen", "start_action", "check_output", "system"},
                    f"v 页不得直接执行: {name}（前台交接只许走 _run_profile/_handoff）")

    def test_the_client_has_no_action_method_at_all(self):
        """JanitorClient 有 start_action；StackClient 刻意没有。"""
        import cmux_supervisor_tui as tui

        for forbidden in ("start_action", "_run_action", "_invoke_control"):
            self.assertFalse(hasattr(tui.StackClient, forbidden), forbidden)

    # ---- absent 形状 ----

    def test_the_absent_snapshot_declares_every_key_the_page_reads(self):
        """缺键会让唯一的错误路径 KeyError——这正是 106.14.4 的第 4 条修复。"""
        import cmux_supervisor_tui as tui

        absent = tui._stack_absent("测试原因")
        for key in ("available", "reason", "overall", "probed_count",
                    "requested_count", "unhealthy", "unknown", "components"):
            self.assertIn(key, absent, key)
        self.assertFalse(absent["available"])
        # 未测量必须是 None,不能伪装成 0
        self.assertIsNone(absent["probed_count"])
        self.assertIsNone(absent["overall"])
        # 两条渲染路径都不能崩
        tui.stack_line(absent)
        tui.stack_page_lines(absent)


class StackKeyAndWidthTests(unittest.TestCase):
    """按键不冲突、窄窗口不截断出半截敏感串。"""

    def test_v_does_not_collide_with_any_existing_key(self):
        import re

        import cmux_supervisor_tui as tui

        source = Path(tui.__file__).read_text(encoding="utf-8")
        # 主屏与 G 页已占用的字母
        taken = set(re.findall(r'ord\("(\w)"\)', source))
        # v/V 是本轮新增的,应当只被我的分支占用
        self.assertIn("v", taken)
        body = source[source.index("def _storage_page("):]
        body = body[:body.index("\ndef ", 1)]
        self.assertNotIn('ord("v")', body, "G 页不得也用 v")

    def test_the_main_screen_keys_keep_their_meanings(self):
        """v 的加入不得改动既有键语义。"""
        import cmux_supervisor_tui as tui

        source = Path(tui.__file__).read_text(encoding="utf-8")
        # 这些既有键必须仍在主屏循环里
        for key in ('ord("q")', 'ord("G")', 'ord("f")', 'ord("/")', 'ord("A")', 'ord("S")'):
            self.assertIn(key, source, key)
        self.assertIn("v 三件套", tui.GLOBAL_KEYS_2)

    def test_narrow_terminals_never_render_a_partial_sensitive_string(self):
        """窄窗口靠 clip 截断;投影里本就没有敏感串,所以截断也截不出。"""
        import tempfile

        import cmux_supervisor_tui as tui

        token = "sk-ant-NARROW-must-not-appear-9a2b"
        with tempfile.TemporaryDirectory() as tmp:
            ctl = Path(tmp) / "fake"
            import json as _json
            doc = {
                "overall": "degraded", "probed_count": 3, "requested_count": 3,
                "unhealthy": [], "unknown": [],
                "components": {"profiles": {"component": "profiles", "installed": True,
                                            "probe_ok": True, "healthy": False,
                                            "launchd_loaded": None, "reason": token,
                                            "profile_count": 1, "unhealthy_count": 1}},
            }
            ctl.write_text("import sys\n"
                           f"sys.stdout.write({_json.dumps(doc)!r})\n"
                           "sys.exit(1)\n", encoding="utf-8")
            snap = tui.StackClient(ctl).fetch()
            # reason 是上游自己的文字,会被渲染;所以断言的是「渲染后按宽度截断
            # 不产生半截 token 之外的新泄漏面」——即 reason 原样出现或被整体截掉
            for width in (20, 40, 80, 200):
                for line in [tui.stack_line(snap)] + tui.stack_page_lines(snap):
                    clipped = line[:width]
                    if token[:8] in clipped:
                        # 若出现,必须是上游 reason 的一部分,而非我们拼接产生的
                        self.assertIn(token[:8], line)

class DrawPathIsolationTests(unittest.TestCase):
    """The draw path must not touch the filesystem.

    A cold scan of a four-figure backlog stalled the interface for 6.3-8.4s
    because _draw() called scan_junk() directly.  This asserts the property at
    the source level rather than by timing, which would be flaky under load.
    """

    def _draw_source(self) -> str:
        import cmux_supervisor_tui as tui
        import inspect

        return inspect.getsource(tui._draw)

    def test_draw_makes_no_filesystem_or_subprocess_call(self):
        source = self._draw_source()
        for needle in ("scandir", "statvfs", "rglob", "iterdir", "os.stat",
                       "subprocess", "read_text", "open(", ".exists()",
                       "scan_junk", "_dir_bytes", "_count_and_sample"):
            self.assertNotIn(needle, source, f"_draw must not call {needle}")

    def test_draw_reads_the_junk_row_from_the_client_snapshot(self):
        source = self._draw_source()
        # One route in, and it is the non-blocking one.
        self.assertIn("model.janitor.snapshot()", source)

    def test_janitor_private_files_are_never_opened_by_the_tui(self):
        import cmux_supervisor_tui as tui

        source = Path(tui.__file__).read_text(encoding="utf-8")
        # The panel used to parse the janitor's config.env for MODE and stat its
        # DISABLED/GUARD_TRIPPED sentinels.  That is how it came to promise 48h
        # retention while the script enforced 24h.
        for needle in ('"config.env"', "config.env\"", "'DISABLED'", '"DISABLED"',
                       '"GUARD_TRIPPED"', "'GUARD_TRIPPED'",
                       "janitor-state.json", "guard-state.json"):
            self.assertNotIn(needle, source, f"TUI must not read {needle}")

    def test_the_tui_never_removes_a_path(self):
        import cmux_supervisor_tui as tui

        source = Path(tui.__file__).read_text(encoding="utf-8")
        for needle in ("shutil.rmtree", "os.remove", "os.unlink", ".unlink(",
                       "shutil.move", "os.rename"):
            self.assertNotIn(needle, source,
                             "disposal belongs to the janitor script")

    # ---- the same property, checked transitively and with receivers ----
    #
    # The substring assertions above read only _draw's own body, so a blocking
    # call relocated into a helper that _draw calls would pass them unnoticed.
    # An ad-hoc probe that resolved bare callee names had the opposite fault:
    # it reported _draw as dirty because it saw `refresh` and bound it to
    # SupervisorModel.refresh, when the receiver is really the curses window.
    # The walk below keeps the receiver and follows the graph.

    FS_ATTRS = frozenset({
        "read_text", "write_text", "read_bytes", "write_bytes",
        "iterdir", "rglob", "glob", "scandir", "statvfs", "listdir", "walk",
        "unlink", "rmtree", "mkdir", "rename", "replace", "touch",
        "lstat", "stat", "exists", "is_dir", "is_file", "samefile",
        # ``run`` and ``call`` were missing here for as long as this gate has
        # existed, and their absence was invisible: ``subprocess.run`` only ever
        # appears inside WORKER_ONLY members, which this gate catches by function
        # name instead.  So the list never had to be right about run -- until a
        # foreground ``subprocess.call`` on the draw path walked straight through
        # a gate whose whole purpose is to stop exactly that.  Measured, not
        # assumed: with the two names absent, a mutation putting subprocess.call
        # into _stack_page's reachable graph left this test green.
        "check_output", "check_call", "Popen", "communicate", "getoutput",
        "run", "call",
        # ``core.load_json`` opens and parses a file, and ``Path.home()`` is the
        # first half of every "just read one more state file" regression.  A
        # mutation that moved the session fallbacks into _draw used exactly
        # these two names and passed the list above unnoticed.
        "load_json", "home", "expanduser",
    })
    FS_NAMES = frozenset({"open", "load_json"})
    # A curses window owns erase/refresh/getch.  The exemption is for
    # *traversal* only: `stdscr.read_text()` would still be reported, because a
    # window has no such method and the name could only mean real I/O.
    SCREEN_RECEIVERS = frozenset({"stdscr", "curses", "win", "window", "pad_win"})
    # Handing work to a worker thread is the design, so the walk stops here
    # rather than following the blocking calls that run over there.
    THREAD_BOUNDARY = frozenset({"maybe_refresh", "start_action"})
    # ...and this is the work that may only ever run on that worker.  The
    # session resolver's ps/lsof/state reading belongs here for the same reason
    # the janitor's ctl call does: one slow probe must delay a number, never a
    # redraw.
    WORKER_ONLY = frozenset({
        "_fetch_once", "_run_action", "_invoke_control", "fetch",
        "_resolve_once", "read_ps_table", "resolve_surface_session",
        "codex_session_from_lsof", "grok_session_for_pid",
        "claude_session_from_runtime",
    })
    # The one sanctioned way to run a foreground program from the draw thread.
    # Membership alone grants nothing: test_the_handoff_primitive_suspends_curses
    # asserts that each member really does suspend curses before the child and
    # restore it in `finally`.  A bare name allowlist would be a back door -- it
    # would let any future function launder a blocking call through this set by
    # being added to it, which is the failure this gate exists to prevent.
    FOREGROUND_HANDOFF = frozenset({"_handoff"})

    @staticmethod
    def _receiver(node) -> str | None:
        """Render a call's receiver as a dotted string, or None if computed."""

        import ast

        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        return ".".join(reversed(parts))

    def _walk(self, source: str, entry: str):
        """Follow every synchronous call reachable from ``entry``.

        Returns ``(reached, findings)`` where findings are
        ``(enclosing_def, receiver, attribute)`` triples.  Resolution is by
        final attribute name, which over-approximates: an unrelated object's
        ``.stat()`` would be reported.  That direction is deliberate -- the
        gate should fail loudly rather than pass quietly.
        """

        import ast

        defs: dict[str, list] = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append(node)

        reached: set[str] = set()
        findings: list[tuple[str, str | None, str]] = []
        queue = [entry]
        while queue:
            name = queue.pop()
            if name in reached:
                continue
            reached.add(name)
            for body in defs.get(name, []):
                for call in ast.walk(body):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    if isinstance(func, ast.Attribute):
                        receiver = self._receiver(func.value)
                        findings.append((name, receiver, func.attr))
                        tail = (receiver or "").split(".")[-1]
                        if tail in self.SCREEN_RECEIVERS:
                            continue        # curses, not one of ours
                        if func.attr not in self.THREAD_BOUNDARY:
                            queue.append(func.attr)
                    elif isinstance(func, ast.Name):
                        findings.append((name, None, func.id))
                        queue.append(func.id)
        return reached, findings

    def _walk_tui(self, entry: str):
        import cmux_supervisor_tui as tui

        return self._walk(Path(tui.__file__).read_text(encoding="utf-8"), entry)

    def test_draw_path_is_transitively_free_of_blocking_calls(self):
        for entry in ("_draw", "_storage_page", "_stack_page"):
            with self.subTest(entry=entry):
                _, findings = self._walk_tui(entry)
                bad = [(where, receiver, attribute)
                       for where, receiver, attribute in findings
                       if (attribute in self.FS_ATTRS or attribute in self.FS_NAMES)
                       and where not in self.FOREGROUND_HANDOFF]
                self.assertEqual(bad, [], f"{entry} reaches blocking calls: {bad}")

    def test_the_gate_would_reject_an_unexempted_foreground_subprocess(self):
        """对判据本身的断言：拿掉豁免，闸门必须报违规。

        没有这一条，「FS_ATTRS 增补 run/call」这个修复是不承重的——把两个名字删掉，
        整套测试仍然全绿，因为没有任何断言在检查这道闸门【会不会失败】。
        一道永远报绿的门与一道永远报红的门同样不提供信息。
        """
        source = '''
def _stack_page(stdscr, model):
    _sneaky(stdscr)


def _sneaky(stdscr):
    subprocess.call(["x"])
'''
        _, findings = self._walk(source, "_stack_page")
        bad = [(w, r, a) for w, r, a in findings
               if (a in self.FS_ATTRS or a in self.FS_NAMES)
               and w not in self.FOREGROUND_HANDOFF]
        self.assertEqual(bad, [("_sneaky", "subprocess", "call")],
                         "未豁免的前台子进程必须被判违规")

        # ...而同一个调用放进豁免原语里则不报，证明豁免是有效的而非空转。
        exempt = source.replace("_sneaky", "_handoff")
        _, findings2 = self._walk(exempt, "_stack_page")
        bad2 = [(w, r, a) for w, r, a in findings2
                if (a in self.FS_ATTRS or a in self.FS_NAMES)
                and w not in self.FOREGROUND_HANDOFF]
        self.assertEqual(bad2, [], "豁免原语内的同一调用不该被误判")

    def test_the_handoff_primitive_suspends_curses(self):
        """豁免的条件，不是名字。

        FOREGROUND_HANDOFF 的每个成员都必须：先 endwin 再起子进程、
        且在 finally 里 reset_prog_mode。顺序错了终端会被 curses 占着，
        恢复不在 finally 里则子进程一崩就把用户的终端留在瞎掉的状态。
        """
        import ast
        import inspect

        import cmux_supervisor_tui as tui

        for name in sorted(self.FOREGROUND_HANDOFF):
            with self.subTest(primitive=name):
                fn = getattr(tui, name)
                src = inspect.getsource(fn)
                tree = ast.parse(src)

                order = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        attr = getattr(node.func, "attr", None)
                        if attr in ("endwin", "def_prog_mode", "reset_prog_mode",
                                    "call", "run"):
                            order.append((node.lineno, attr))
                order.sort()
                seq = [a for _, a in order]

                self.assertIn("endwin", seq, f"{name} 必须挂起 curses")
                self.assertIn("reset_prog_mode", seq, f"{name} 必须恢复 curses")
                child = next(i for i, a in enumerate(seq) if a in ("call", "run"))
                self.assertLess(seq.index("endwin"), child,
                                f"{name}: endwin 必须早于子进程")
                self.assertGreater(seq.index("reset_prog_mode"), child,
                                   f"{name}: reset_prog_mode 必须晚于子进程")

                # 恢复必须在 finally 里，不是「写在后面」就算
                restored_in_finally = False
                for node in ast.walk(tree):
                    if isinstance(node, ast.Try):
                        for fin in node.finalbody:
                            for sub in ast.walk(fin):
                                if (isinstance(sub, ast.Call)
                                        and getattr(sub.func, "attr", None) == "reset_prog_mode"):
                                    restored_in_finally = True
                self.assertTrue(restored_in_finally,
                                f"{name}: reset_prog_mode 必须在 finally 里")

    def test_returning_from_ccp_new_forces_a_stack_reread(self):
        """回来必须强制重读，否则刚修好的 profile 还要红最多 STACK_CACHE_TTL_SEC。

        变异验证补上的：删掉 `force=True` 那一行时，整套测试原本仍然全绿。
        """
        import ast
        import inspect

        import cmux_supervisor_tui as tui

        tree = ast.parse(inspect.getsource(tui._run_profile))
        forced = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "maybe_refresh"
            and any(kw.arg == "force" and getattr(kw.value, "value", None) is True
                    for kw in node.keywords)
        ]
        self.assertTrue(forced,
                        "_run_profile 必须以 force=True 重读，不能等 TTL 过期")

    def test_the_page_ban_list_covers_the_whole_subprocess_family(self):
        """禁用集必须收齐同族名字，漏一个就等于没有这道门。

        变异验证补上的：从禁用集里删掉 `call` 时，整套测试原本仍然全绿——
        因为没有任何断言在检查【这个集合本身是否完整】。
        """
        import ast
        import inspect

        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._stack_page)
        tree = ast.parse(source)
        banned = None
        # 从被测断言自己的源码里取那个集合，避免两处各写一份而漂移
        # 名字写死为拥有该断言的类，而不是 type(self)——两者不在同一个类里，
        # 用 type(self) 会 AttributeError（已实测）。
        import textwrap

        # 名字写死为拥有该断言的类，而不是 type(self)——两者不在同一个类里，
        # 用 type(self) 会 AttributeError（已实测）。方法源码带缩进，
        # 不 dedent 直接 ast.parse 会 IndentationError（也已实测）。
        own = textwrap.dedent(inspect.getsource(
            StackClientTests.test_the_stack_page_executes_nothing_but_the_handoff))
        for node in ast.walk(ast.parse(own)):
            if isinstance(node, ast.Set):
                banned = {getattr(e, "value", None) for e in node.elts}
        self.assertIsNotNone(banned, "取不到禁用集，断言本身失效")
        for required in ("run", "call", "Popen", "check_output", "system", "start_action"):
            self.assertIn(required, banned,
                          f"禁用集缺 {required}：subprocess 同族漏一个就能穿过")

    def test_the_handoff_never_captures_the_childs_streams(self):
        """捕获会同时废掉 getpass 与 $EDITOR，所以一个捕获类 kwarg 都不许有。"""
        import ast
        import inspect

        import cmux_supervisor_tui as tui

        src = inspect.getsource(tui._handoff)
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("call", "run"):
                kwargs = {kw.arg for kw in node.keywords}
                for forbidden in ("capture_output", "stdout", "stderr", "input", "text"):
                    self.assertNotIn(forbidden, kwargs,
                                     f"前台交接不得传 {forbidden}（会废掉 getpass / $EDITOR）")

    def test_the_blocking_work_is_only_reachable_through_a_thread(self):
        for entry in ("_draw", "_storage_page", "_stack_page"):
            with self.subTest(entry=entry):
                reached, _ = self._walk_tui(entry)
                self.assertEqual(
                    sorted(reached & self.WORKER_ONLY), [],
                    f"{entry} reaches worker-only work without crossing a thread")

    def test_the_thread_boundary_starts_a_worker_and_never_joins_it(self):
        import inspect

        import cmux_supervisor_tui as tui

        for name in sorted(self.THREAD_BOUNDARY):
            with self.subTest(method=name):
                source = inspect.getsource(getattr(tui.JanitorClient, name))
                self.assertIn("threading.Thread", source)
                self.assertIn("worker.start()", source)
                # Joining here would put ctl back on the keystroke path, which
                # is the stall this whole arrangement exists to remove.
                self.assertNotIn(".join(", source)

    # ---- the walk has to be able to fail, or it proves nothing ----

    def test_the_gate_would_notice_a_blocking_call_in_a_helper(self):
        source = (
            "def helper(path):\n"
            "    return path.read_text()\n"
            "def _draw(stdscr, path):\n"
            "    stdscr.erase()\n"
            "    helper(path)\n"
            "    stdscr.refresh()\n"
        )
        _, findings = self._walk(source, "_draw")
        bad = [item for item in findings if item[2] in self.FS_ATTRS]
        self.assertEqual([item[2] for item in bad], ["read_text"],
                         "a call one level down must still be reported")

    def test_a_curses_refresh_does_not_drag_in_a_same_named_method(self):
        """The exact false positive a name-only probe produced."""

        source = (
            "def refresh(self):\n"
            "    return self.path.read_text()\n"
            "def _draw(stdscr):\n"
            "    stdscr.refresh()\n"
        )
        reached, findings = self._walk(source, "_draw")
        self.assertNotIn("refresh", reached)
        self.assertEqual([item for item in findings if item[2] in self.FS_ATTRS], [])

    def test_the_walk_stops_at_the_thread_boundary(self):
        source = (
            "def _fetch_once(self):\n"
            "    return subprocess.run(['ctl'])\n"
            "def maybe_refresh(self):\n"
            "    worker = threading.Thread(target=self._fetch_once)\n"
            "    worker.start()\n"
            "def _draw(stdscr, client):\n"
            "    client.maybe_refresh()\n"
        )
        reached, findings = self._walk(source, "_draw")
        self.assertNotIn("_fetch_once", reached)
        # The boundary crossing is still recorded, so renaming it cannot make
        # the traversal quietly skip a newly blocking method.
        self.assertIn(("_draw", "client", "maybe_refresh"), findings)


class StoragePageTests(unittest.TestCase):
    """The G page: what it states, what it refuses to imply, and its keys.

    The page is a reader plus three control actions.  Every action goes out
    through cmux-janitorctl and every safety gate stays in the janitor script,
    so the assertions here are about honesty of presentation and about never
    blocking the interface.
    """

    def _live(self, **overrides):
        import cmux_supervisor_tui as tui

        snapshot = tui._snapshot_from_status({
            "control": {"paused": False, "guard_tripped": False,
                        "config_mode": "apply"},
            "janitor": {
                "mode": "apply", "safety_complete": True, "age_sec": 120.0,
                "observed_at": "2026-08-28T06:00:00Z", "limit": 500,
                "counts": {"raw_sb": 4, "raw_staging": 900, "eligible": 880,
                           "selected": 500, "disposed": 500, "protected": 20},
                "selected_bytes": {"value": 3 * 1024 ** 3, "precision": "estimated"},
            },
            "quarantine": {"batch_count": 19, "keep_hours": 48,
                           "bytes": {"value": 21 * 1024 ** 3, "precision": "exact"}},
            "guard": {"health": "healthy", "age_sec": 30.0},
            # Production ctl always publishes this block (cmux-janitorctl:395),
            # so the "healthy" fixture has to carry it too.  Leaving it out made
            # every assertion here run against a shape production never emits.
            "launchd": {"janitor_loaded": True, "guard_loaded": True},
        })
        snapshot.update(overrides)
        return snapshot

    def test_sweep_prompt_states_everything_that_changes_the_outcome(self):
        from cmux_supervisor_tui import storage_sweep_prompt

        prompt = storage_sweep_prompt(self._live())
        # Candidate scale, the cap that bounds it, and the delay before anything
        # is actually deleted: all three decide whether this keypress is safe.
        self.assertIn("880", prompt)
        self.assertIn("500", prompt)
        self.assertIn("48h", prompt)
        self.assertIn("3.0GiB", prompt)
        # An extrapolated total must not be presented as a measured one.
        self.assertIn("~3.0GiB", prompt)
        self.assertIn("闸门", prompt)

    def test_sweep_prompt_never_prints_an_unmeasured_figure_as_zero(self):
        from cmux_supervisor_tui import storage_sweep_prompt

        blind = self._live(candidate_counts={}, selected_bytes=None,
                           selected_precision="unknown", per_run_limit=None,
                           quarantine_keep_hours=None)
        prompt = storage_sweep_prompt(blind)
        self.assertIn("?", prompt)
        self.assertIn("未测量", prompt)
        self.assertIn("未知", prompt)
        # "0 项" in a confirmation box reads as "nothing will happen".
        self.assertNotIn("0 项", prompt)

    def test_page_keeps_candidates_and_quarantine_apart(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(self._live()))
        self.assertIn("候选", body)
        self.assertIn("隔离", body)
        self.assertIn("19", body)      # quarantine batches
        self.assertIn("904", body)     # raw_sb + raw_staging, summed once
        self.assertIn("48h", body)
        # The two totals must never be added: 3GiB + 21GiB would be 24GiB.
        self.assertNotIn("24.0GiB", body)
        self.assertIn("~3.0GiB", body)
        self.assertIn("21.0GiB", body)

    def test_page_reports_an_unreadable_controller_as_such(self):
        from cmux_supervisor_tui import _absent_snapshot, storage_page_lines

        body = "\n".join(storage_page_lines(_absent_snapshot("控制器未安装")))
        self.assertIn("无法读取", body)
        self.assertIn("控制器未安装", body)
        self.assertIn("cmux-janitorctl", body)
        # Absence of a reading is not a reading of zero.
        self.assertNotIn("0 批", body)

    def test_page_states_when_the_last_run_could_not_prove_safety(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(self._live(safety_complete=False)))
        self.assertIn("不处置", body)

    def test_page_explains_a_tripped_guard_needs_a_human(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(self._live(guard_tripped=True)))
        self.assertIn("守卫跳闸", body)
        self.assertIn("rearm", body)

    def test_page_body_fits_common_terminal_widths(self):
        from cmux_supervisor_tui import (STORAGE_KEYS, display_width,
                                         storage_page_lines, storage_sweep_prompt)

        for snapshot in (self._live(), self._live(guard_tripped=True)):
            for line in storage_page_lines(snapshot, "正在请求手动清扫…"):
                self.assertLess(display_width(line), 80, line)
        self.assertLess(display_width(STORAGE_KEYS), 80)
        # The prompt renders on one row with " [y/N] " appended.
        self.assertLess(display_width(storage_sweep_prompt(self._live())) + 7, 160)

    def test_action_message_is_appended_when_present(self):
        from cmux_supervisor_tui import storage_page_lines

        without = storage_page_lines(self._live())
        with_msg = storage_page_lines(self._live(), "手动清扫被拒: guard is tripped")
        self.assertEqual(with_msg[:len(without)], without)
        self.assertIn("guard is tripped", with_msg[-1])

    # ---- launchd schedule (added after the 2026-08-29T06:39:50Z incident) ----
    #
    # A sandboxed test run booted both live labels out of launchd. The janitor
    # stopped being scheduled, yet the page still read "运行中" and the guard
    # still read "healthy": every field it rendered was a *file* reading, and
    # the files kept their last values because nothing was overwriting them.
    # ctl already published launchd.janitor_loaded / guard_loaded
    # (cmux-janitorctl:396-397); the panel simply never consumed them.

    def test_page_states_when_the_janitor_is_not_scheduled(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(self._live(janitor_loaded=False)))
        self.assertIn("未加载", body)
        # The headline must not claim the janitor is running when launchd has
        # no such service; that is the exact shape of the incident.
        self.assertNotIn("清扫器: 运行中", body)
        self.assertIn("bootstrap", body)

    def test_page_states_when_the_guard_is_not_scheduled(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(self._live(guard_loaded=False)))
        # A guard that is not scheduled cannot trip, so a stale healthy verdict
        # is the most dangerous thing on the page. It has to be visible even
        # while the janitor itself is fine.
        self.assertIn("未加载", body)

    def test_page_shows_the_schedule_even_when_everything_is_healthy(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(self._live()))
        # Always-on row: an operator should not have to know that the absence
        # of a warning is the only signal.
        self.assertIn("调度", body)
        self.assertIn("已加载", body)

    def test_unknown_launchd_state_is_not_reported_as_an_outage(self):
        from cmux_supervisor_tui import storage_page_lines

        body = "\n".join(storage_page_lines(
            self._live(janitor_loaded=None, guard_loaded=None)))
        self.assertIn("未测量", body)
        self.assertNotIn("未加载", body)
        self.assertNotIn("清扫器: 未加载到 launchd", body)

    def test_launchd_flags_survive_the_projection_as_three_states(self):
        import cmux_supervisor_tui as tui

        def project(launchd):
            document = {
                "control": {"paused": False, "guard_tripped": False,
                            "config_mode": "apply"},
                "janitor": {"mode": "apply", "safety_complete": True},
                "quarantine": {}, "guard": {"health": "healthy"},
            }
            if launchd is not None:
                document["launchd"] = launchd
            return tui._snapshot_from_status(document)

        loaded = project({"janitor_loaded": True, "guard_loaded": True})
        self.assertIs(loaded["janitor_loaded"], True)
        self.assertIs(loaded["guard_loaded"], True)

        gone = project({"janitor_loaded": False, "guard_loaded": True})
        self.assertIs(gone["janitor_loaded"], False)

        # ctl publishes None when launchctl itself cannot answer
        # (cmux-janitorctl:318). A bool() coercion here would turn "could not
        # ask" into a confident "not loaded" and raise a false alarm.
        unknown = project({"janitor_loaded": None, "guard_loaded": None})
        self.assertIsNone(unknown["janitor_loaded"])
        self.assertIsNone(unknown["guard_loaded"])

        # An older ctl with no launchd block at all must degrade, not crash.
        missing = project(None)
        self.assertIsNone(missing["janitor_loaded"])
        self.assertIsNone(missing["guard_loaded"])

    def test_absent_snapshot_carries_the_launchd_keys(self):
        from cmux_supervisor_tui import _absent_snapshot

        absent = _absent_snapshot("控制器未安装")
        # Every consumer indexes the snapshot by key; a missing key here would
        # be a KeyError on the one path that is already an error path.
        self.assertIn("janitor_loaded", absent)
        self.assertIn("guard_loaded", absent)
        self.assertIsNone(absent["janitor_loaded"])
        self.assertIsNone(absent["guard_loaded"])

    def test_panel_consumes_every_launchd_flag_ctl_publishes(self):
        """Structural: ctl adding a flag the panel ignores is how this bug began."""

        import re

        repo = Path(__file__).resolve().parent.parent
        ctl = (repo / "janitor" / "src" / "cmux-janitorctl").read_text(encoding="utf-8")
        block = re.search(r'"launchd":\s*\{(.*?)\}', ctl, re.S)
        self.assertIsNotNone(block, "ctl no longer publishes a launchd block")
        published = set(re.findall(r'"(\w+)":', block.group(1)))
        self.assertTrue(published, "no launchd flags found in ctl")

        tui_source = (repo / "cmux_supervisor_tui.py").read_text(encoding="utf-8")
        for flag in sorted(published):
            self.assertIn(flag, tui_source,
                          f"ctl publishes launchd.{flag} but the panel never reads it")


class StorageActionTests(unittest.TestCase):
    """Control actions: async, gate-preserving, and truthful about refusals."""

    def _ctl(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ctl-act-"))
        self.addCleanup(lambda: subprocess.run(["/bin/rm", "-rf", str(tmp)], check=False))
        script = tmp / "cmux-janitorctl"
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_pause_and_resume_map_to_the_controller_subcommands(self):
        import cmux_supervisor_tui as tui

        log = Path(tempfile.mkdtemp(prefix="ctl-log-"))
        self.addCleanup(lambda: subprocess.run(["/bin/rm", "-rf", str(log)], check=False))
        recorder = log / "argv.txt"
        ctl = self._ctl(
            "import sys, pathlib\n"
            f"pathlib.Path({str(recorder)!r}).write_text(' '.join(sys.argv[1:]))\n"
            "print('{\"ok\": true}')\n"
        )
        client = tui.JanitorClient(ctl)

        for command, expected in (("pause", "pause"), ("resume", "resume"),
                                  ("run", "run --manual")):
            self.assertTrue(client.start_action(command))
            self.assertTrue(client.wait_for_action(30))
            self.assertEqual(recorder.read_text(encoding="utf-8"), expected)
            phase, message = client.action_state()
            self.assertEqual(phase, "done", message)
            client.clear_action()

    def test_a_refusal_is_shown_with_the_gate_that_refused(self):
        import cmux_supervisor_tui as tui

        # ctl exits 1 and names the gate; the page must not flatten that into
        # "failed", because "guard is tripped" is actionable and "broken" is not.
        ctl = self._ctl(
            "import json, sys\n"
            "print(json.dumps({'ok': False, 'refused': ['guard is tripped; investigate']}))\n"
            "sys.exit(1)\n"
        )
        client = tui.JanitorClient(ctl)
        client.start_action("run")
        self.assertTrue(client.wait_for_action(30))
        phase, message = client.action_state()
        self.assertEqual(phase, "refused")
        self.assertIn("guard is tripped", message)

    def test_fail_closed_is_reported_as_an_error_not_a_success(self):
        import cmux_supervisor_tui as tui

        ctl = self._ctl(
            "import json, sys\n"
            "print(json.dumps({'ok': False, 'fail_closed': 'state unreadable'}))\n"
            "sys.exit(3)\n"
        )
        client = tui.JanitorClient(ctl)
        client.start_action("pause")
        self.assertTrue(client.wait_for_action(30))
        phase, message = client.action_state()
        self.assertEqual(phase, "error")
        self.assertIn("state unreadable", message)

    def test_a_missing_controller_cannot_look_like_a_completed_action(self):
        import cmux_supervisor_tui as tui

        client = tui.JanitorClient(Path("/nonexistent-ctl-binary"))
        client.start_action("run")
        self.assertTrue(client.wait_for_action(30))
        phase, message = client.action_state()
        self.assertEqual(phase, "error")
        self.assertIn("未安装", message)

    def test_a_slow_action_does_not_block_the_caller(self):
        import cmux_supervisor_tui as tui

        ctl = self._ctl("import time\ntime.sleep(30)\n")
        client = tui.JanitorClient(ctl)
        start = time.monotonic()
        self.assertTrue(client.start_action("run"))
        phase, message = client.action_state()
        elapsed = time.monotonic() - start
        # The keypress returns immediately with a progress message; a sweep that
        # spends minutes in lsof must not freeze the interface.
        self.assertLess(elapsed, 2.0, elapsed)
        self.assertEqual(phase, "running")
        self.assertIn("正在", message)
        # A second press while one is in flight is dropped, not queued.
        self.assertFalse(client.start_action("pause"))
        # An in-flight action cannot be cleared out from under itself.
        client.clear_action()
        self.assertEqual(client.action_state()[0], "running")

    def test_unsupported_actions_are_rejected_in_code_not_shelled_out(self):
        import cmux_supervisor_tui as tui

        client = tui.JanitorClient(Path("/nonexistent"))
        with self.assertRaises(ValueError):
            client.start_action("rearm")
        with self.assertRaises(ValueError):
            client.start_action("uninstall")

    def test_an_action_forces_the_next_status_read_to_refetch(self):
        import cmux_supervisor_tui as tui

        ctl = self._ctl("print('{\"ok\": true}')\n")
        client = tui.JanitorClient(ctl)
        client._fetched_at = time.time()  # pretend a fresh snapshot exists
        client.start_action("pause")
        self.assertTrue(client.wait_for_action(30))
        # Showing pre-action numbers after a successful pause would tell the
        # operator the pause did not take.
        self.assertTrue(client.maybe_refresh())

    def test_the_page_loop_never_bypasses_a_gate_or_rearms(self):
        import inspect

        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._storage_page)
        # Sentinels are the janitor's own gates; the page asks ctl and accepts
        # its answer instead of writing or deleting them itself.
        for needle in ("DISABLED", "GUARD_TRIPPED", "--rearm", "unlink",
                       "rmtree", "os.remove", "cmux-janitor.sh"):
            self.assertNotIn(needle, source, needle)
        # Both mutating keys go through a confirmation first.
        self.assertIn("_confirm", source)

    def test_the_page_loop_does_no_io_of_its_own(self):
        import inspect

        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._storage_page)
        for needle in ("open(", "scandir", "statvfs", "subprocess.",
                       "read_text", "iterdir", "glob("):
            self.assertNotIn(needle, source, needle)
        # It reads the worker's snapshot, exactly like the summary row.
        self.assertIn("client.snapshot()", source)

    def test_main_screen_key_meanings_are_untouched(self):
        import inspect

        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._run)
        # G is the only new main-screen key; p/c/q/Q keep their surface-level
        # meanings, which is why the janitor's pause lives on the G page.
        self.assertIn('ord("G")', source)
        self.assertIn('_storage_page', source)
        for existing in ('ord("q")', 'ord("f")', 'ord("R")', 'ord("p")'):
            self.assertIn(existing, source, existing)


# Synthetic ids.  These were originally real session ids copied off this machine
# (they matched live rollout filenames under ~/.codex/sessions), which R4 forbids
# in any artifact.  They are structurally valid UUIDv7-shaped values that exist
# in no state file, Grok session or rollout name, so the parsers still exercise
# the same code paths without carrying anyone's real session identity.
#
# They must also differ from each other at *every* slice the tests take -- the
# prefix-search test searches SID_A[9:13], so a shared block there makes one
# surface's needle legitimately match another's id and the failure looks like a
# search bug rather than a fixture collision.
SID_A = "a1a1a1a1-b1b1-7b11-8b11-c1c1c1c1c1c1"
SID_B = "d2d2d2d2-e2e2-7e22-8e22-f2f2f2f2f2f2"
SID_C = "a3a3a3a3-b3b3-7b33-8b33-c3c3c3c3c3c3"


def _ps_line(pid, command, started="Sat Aug 29 20:13:33 2026", ppid=1):
    return f"{pid:>6} {ppid:>5} {started} {command}"


def _fake_run(stdout="", *, raises=None):
    """A subprocess.run stand-in.  No test may touch the real ps/lsof."""

    def runner(argv, **kwargs):
        if raises is not None:
            raise raises
        return types.SimpleNamespace(stdout=stdout, returncode=0, stderr="")

    return runner


def _session_candidate(session=None, **kw):
    import cmux_supervisor_tui as tui

    record = {"surface_id": kw.pop("surface_id", "S-1"), "ref": "surface:9",
              "workspace_ref": "workspace:18", "pane_ref": "pane:2",
              "title": kw.pop("title", "t")}
    return tui.Candidate(
        record=record, source=kw.pop("source", "explicit"),
        state=kw.pop("state", "idle"), error_type="-", send_count=0,
        paused=kw.pop("paused", False), agent_kind=kw.pop("agent_kind", "codex"),
        session=session if session is not None else tui.SessionResult(),
    )


class _StubJanitor:
    """A janitor that reports nothing, so a session test cannot shell out.

    The panel's refresh always pokes the janitor; pointing it at the real
    client would run ``cmux-janitorctl`` from a unit test.
    """

    def maybe_refresh(self, *, now=None, force=False):
        return False

    def snapshot(self):
        import cmux_supervisor_tui as tui

        return tui._absent_snapshot("测试替身")

    def action_state(self):
        return "", ""


def _ps_runner(stdout: str):
    """A fake ``ps`` that returns fixed table text.

    Every session test injects one of these, so no test in this file ever runs
    a real ``ps``: the resolver's whole subprocess surface is the injected
    runner.
    """

    import types

    def run(argv, **kwargs):
        return types.SimpleNamespace(stdout=stdout, returncode=0)

    return run


class SessionParserTests(unittest.TestCase):
    """Strict validation and the two argv tiers."""

    def test_only_a_complete_uuid_validates(self):
        import cmux_supervisor_tui as tui

        self.assertTrue(tui.is_session_uuid(SID_A))
        self.assertTrue(tui.is_session_uuid(SID_A.upper()))
        for bad in ("", None, SID_A[:35], SID_A + "a", SID_A.replace("-", ""),
                    "zzzzzzzz-6ba1-7313-8bb8-eae8a5c39535", f"  {SID_A}  "):
            self.assertFalse(tui.is_session_uuid(bad), repr(bad))

    def test_every_documented_flag_form_parses(self):
        import cmux_supervisor_tui as tui

        for command in (
            f"codex --session-id {SID_A}",
            f"codex --session-id={SID_A}",
            f"codex resume {SID_A}",
            f"claude --resume {SID_A}",
            f"claude --resume={SID_A}",
            f"grok -r {SID_A}",
        ):
            status, sid, _ = tui.resolve_from_argv([command])
            self.assertEqual((status, sid), ("ok", SID_A), command)

    def test_explicit_session_id_outranks_a_resume_flag(self):
        import cmux_supervisor_tui as tui

        status, sid, tier = tui.resolve_from_argv(
            [f"codex --session-id {SID_A} resume {SID_B}"]
        )
        self.assertEqual((status, sid, tier), ("ok", SID_A, "session-id"))

    def test_a_loose_uuid_in_argv_is_never_evidence(self):
        import cmux_supervisor_tui as tui

        # Not flag-adjacent: a model name, a cwd, a title all can carry hex.
        status, sid, _ = tui.resolve_from_argv([f"codex --model {SID_A}"])
        self.assertEqual((status, sid), ("unknown", None))

    def test_same_id_across_pids_is_one_agent_not_a_conflict(self):
        import cmux_supervisor_tui as tui

        status, sid, _ = tui.resolve_from_argv(
            [f"codex resume {SID_A}", f"codex resume {SID_A}"]
        )
        self.assertEqual((status, sid), ("ok", SID_A))

    def test_distinct_ids_in_the_winning_tier_conflict(self):
        import cmux_supervisor_tui as tui

        status, sid, reason = tui.resolve_from_argv(
            [f"codex resume {SID_A}", f"codex resume {SID_B}"]
        )
        self.assertEqual((status, sid), ("conflict", None))
        self.assertIn("2", reason)

    def test_a_lower_tier_conflict_cannot_veto_the_winning_tier(self):
        """The whole point of tiering: an explicit flag settles it."""

        import cmux_supervisor_tui as tui

        status, sid, tier = tui.resolve_from_argv([
            f"codex --session-id {SID_A}",
            f"codex resume {SID_B}",
            f"codex resume {SID_C}",
        ])
        self.assertEqual((status, sid, tier), ("ok", SID_A, "session-id"))

    def test_ps_table_parses_lstart_and_command(self):
        import cmux_supervisor_tui as tui

        table = tui.parse_ps_table("\n".join([
            _ps_line(4242, f"/usr/bin/codex --session-id {SID_A}"),
            "garbage line",
            _ps_line(4243, "-zsh", started="Fri Aug 28 01:02:03 2026", ppid=4242),
        ]))
        self.assertEqual(sorted(table), [4242, 4243])
        self.assertEqual(table[4242]["started_at"], "2026-08-29T20:13:33")
        self.assertIn("--session-id", table[4242]["command"])
        self.assertEqual(table[4243]["ppid"], "4242")

    def test_an_unparseable_lstart_still_yields_a_stable_generation(self):
        import cmux_supervisor_tui as tui

        # ``ps -o lstart=`` always emits five tokens, so the realistic bad case
        # is five tokens that strptime rejects -- not a short field.
        table = tui.parse_ps_table(_ps_line(7, "codex", started="Xxx Yyy 99 99:99:99 9999"))
        self.assertIn(7, table)
        self.assertEqual(table[7]["started_at"], "Xxx Yyy 99 99:99:99 9999")
        self.assertTrue(tui.session_generation(7, table[7]["started_at"]))


class SessionGenerationTests(unittest.TestCase):
    """The gate that stops a replaced process inheriting an identity."""

    def test_formula_matches_the_watcher_byte_for_byte(self):
        """A drifted formula would fail closed and look like a missing feature."""

        import cmux_supervisor_tui as tui

        observed = core.inspect_claude_process(os.getpid())
        self.assertEqual(
            tui.session_generation(os.getpid(), observed.get("started_at") or ""),
            observed.get("generation"),
        )

    def test_empty_start_time_collapses_to_the_unknown_branch(self):
        import cmux_supervisor_tui as tui

        self.assertEqual(tui.session_generation(11, ""),
                         core._short_hash("11:unknown"))

    def test_pid_zero_has_no_generation(self):
        import cmux_supervisor_tui as tui

        self.assertEqual(tui.session_generation(0, "whatever"), "")

    def test_the_watchers_unhashed_sentinel_is_recognised(self):
        """``"<pid>:unknown"`` records "not measured", not an identity."""

        import cmux_supervisor_tui as tui

        self.assertTrue(tui._generation_is_unmeasured("11:unknown", 11))
        self.assertFalse(tui._generation_is_unmeasured(core._short_hash("11:unknown"), 11))


class SessionFallbackTests(unittest.TestCase):
    """Codex lsof, Grok exact-PID join, Claude PID+generation."""

    def test_codex_reads_the_rollout_filename_and_keeps_only_the_uuid(self):
        import cmux_supervisor_tui as tui

        stdout = "\n".join([
            "COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF     NODE NAME",
            "codex    3636  tester  cwd    DIR   1,14      704 12063724 /Users/tester/secret-project",
            "codex    3636  lzhs   12u   REG   1,14   1331483 12063724 "
            f"/Users/tester/.codex/sessions/2026/07/05/rollout-2026-07-05T07-55-57-{SID_A}.jsonl",
        ])
        found, reason = tui.codex_session_from_lsof(3636, runner=_fake_run(stdout))
        self.assertEqual(found, SID_A)
        self.assertEqual(reason, "")

    def test_codex_lsof_timeout_is_a_reason_not_a_crash(self):
        import cmux_supervisor_tui as tui

        found, reason = tui.codex_session_from_lsof(
            1, runner=_fake_run(raises=subprocess.TimeoutExpired("lsof", 1)))
        self.assertIsNone(found)
        self.assertTrue(reason)

    def test_two_rollout_files_are_a_conflict(self):
        import cmux_supervisor_tui as tui

        stdout = "\n".join([
            f"codex 1 lzhs 1u REG 1,14 1 1 /a/rollout-2026-07-05T07-55-57-{SID_A}.jsonl",
            f"codex 1 lzhs 2u REG 1,14 1 2 /a/rollout-2026-07-06T07-55-57-{SID_B}.jsonl",
        ])
        found, reason = tui.codex_session_from_lsof(1, runner=_fake_run(stdout))
        self.assertIsNone(found)
        self.assertIn("2", reason)

    def test_grok_joins_on_exact_pid(self):
        import cmux_supervisor_tui as tui

        document = [
            {"pid": "90235", "session_id": SID_A, "cwd": "/x"},
            {"pid": "9023", "session_id": SID_B, "cwd": "/y"},
        ]
        found, _ = tui.grok_session_for_pid(90235, path=Path("/nonexistent"),
                                           loader=lambda *_: document)
        self.assertEqual(found, SID_A)
        # A prefix must not match: 9023 is a different process.
        found_other, _ = tui.grok_session_for_pid(9023, path=Path("/nonexistent"),
                                                 loader=lambda *_: document)
        self.assertEqual(found_other, SID_B)

    def test_grok_duplicate_pid_entries_conflict(self):
        import cmux_supervisor_tui as tui

        document = [{"pid": "5", "session_id": SID_A},
                    {"pid": "5", "session_id": SID_B}]
        found, reason = tui.grok_session_for_pid(5, path=Path("/nonexistent"),
                                                loader=lambda *_: document)
        self.assertIsNone(found)
        self.assertIn("5", reason)

    def test_grok_missing_or_malformed_file_is_a_reason(self):
        import cmux_supervisor_tui as tui

        for document in ([], {}, None, [{"pid": "5"}], [{"session_id": "nope"}]):
            found, reason = tui.grok_session_for_pid(
                5, path=Path("/nonexistent"), loader=lambda *_a, d=document: d)
            self.assertIsNone(found, repr(document))
            self.assertTrue(reason, repr(document))

    def test_grok_loader_exceptions_are_reasons_not_crashes(self):
        """``core.load_json`` re-raises everything but FileNotFoundError.

        watch:1648-1655 returns the default only for a missing file; a truncated
        file, bad encoding, EACCES or a directory in place of a file all come
        back as RuntimeError.  Unguarded, that exception leaves the worker
        thread and aborts the entire pass, so one unreadable Grok file would
        blank the session column for every surface on the machine.
        """

        import cmux_supervisor_tui as tui

        failures = [
            OSError(13, "Permission denied"),
            ValueError("not json"),
            TypeError("path must be str"),
            RuntimeError("cannot read /x: boom"),
            json.JSONDecodeError("Expecting value", "", 0),
        ]
        for exc in failures:
            def boom(*_args, _exc=exc, **_kwargs):
                raise _exc

            found, reason = tui.grok_session_for_pid(
                90235, path=Path("/nonexistent"), loader=boom)
            self.assertIsNone(found, type(exc).__name__)
            self.assertIn(type(exc).__name__, reason,
                          f"reason must name the failure: {reason!r}")
            # The reason is shown to a user; it must not leak a filesystem path.
            self.assertNotIn("/", reason, reason)

    def test_grok_positive_case_still_resolves_after_the_guard(self):
        """The guard must not swallow the healthy path."""

        import cmux_supervisor_tui as tui

        document = [{"pid": "90235", "session_id": SID_B, "cwd": "/x"}]
        found, reason = tui.grok_session_for_pid(
            90235, path=Path("/nonexistent"), loader=lambda *_a: document)
        self.assertEqual((found, reason), (SID_B, ""))

    def test_a_failing_grok_loader_leaves_the_worker_alive(self):
        """End to end: the exception must not escape the resolution pass."""

        import cmux_supervisor_tui as tui

        errors = []
        previous = threading.excepthook
        threading.excepthook = lambda args: errors.append(args.exc_type.__name__)
        self.addCleanup(lambda: setattr(threading, "excepthook", previous))

        def boom(*_args, **_kwargs):
            raise RuntimeError("cannot read /Users/x/.grok/active_sessions.json")

        resolver = tui.SessionResolver(
            ttl_sec=0.0,
            ps_runner=_ps_runner(_ps_line(90235, "grok")),
            lsof_runner=_fake_run(""),
            grok_path=Path("/nonexistent"),
            grok_loader=boom)
        resolver.maybe_refresh(
            [{"surface_id": "s:grok", "agent_kind": "grok", "agent_pids": [90235]},
             {"surface_id": "s:codex", "agent_kind": "codex", "agent_pids": [90235]}],
            {}, force=True)
        self.assertTrue(resolver.wait_for_refresh(10))
        self.assertEqual(errors, [], "the worker thread must survive")
        snapshot = resolver.snapshot()
        self.assertEqual(sorted(snapshot), ["s:codex", "s:grok"])
        self.assertFalse(snapshot["s:grok"].ok)
        self.assertIn("RuntimeError", snapshot["s:grok"].reason)

    def test_claude_accepts_only_a_matching_pid_and_generation(self):
        import cmux_supervisor_tui as tui

        started = "2026-08-29T20:13:33"
        good = tui.session_generation(4242, started)
        ps_table = {4242: {"started_at": started, "command": "claude", "ppid": "1"}}
        runtime = {"claude_session_id": SID_A, "claude_process_pid": 4242,
                   "claude_process_generation": good}
        found, reason = tui.claude_session_from_runtime(runtime, [4242], ps_table)
        self.assertEqual((found, reason), (SID_A, ""))

    def test_claude_rejects_a_replaced_process_even_when_the_pid_is_live(self):
        """PIDs are reused; only the start-derived generation proves identity."""

        import cmux_supervisor_tui as tui

        ps_table = {4242: {"started_at": "2026-08-29T21:00:00",
                           "command": "claude", "ppid": "1"}}
        runtime = {"claude_session_id": SID_A, "claude_process_pid": 4242,
                   "claude_process_generation": tui.session_generation(
                       4242, "2026-08-29T20:13:33")}
        found, reason = tui.claude_session_from_runtime(runtime, [4242], ps_table)
        self.assertIsNone(found)
        self.assertIn("代际", reason)

    def test_claude_rejects_a_pid_absent_from_the_live_set(self):
        import cmux_supervisor_tui as tui

        runtime = {"claude_session_id": SID_A, "claude_process_pid": 999,
                   "claude_process_generation": "abc"}
        found, reason = tui.claude_session_from_runtime(runtime, [4242], {})
        self.assertIsNone(found)
        self.assertIn("999", reason)

    def test_claude_rejects_the_unmeasured_generation_sentinel(self):
        import cmux_supervisor_tui as tui

        runtime = {"claude_session_id": SID_A, "claude_process_pid": 7,
                   "claude_process_generation": "7:unknown"}
        found, reason = tui.claude_session_from_runtime(
            runtime, [7], {7: {"started_at": "2026-08-29T20:13:33"}})
        self.assertIsNone(found)
        self.assertIn("未测量", reason)

    def test_untracked_claude_with_no_runtime_stays_unmeasured(self):
        """No watcher state exists for it; inventing one would be a lie."""

        import cmux_supervisor_tui as tui

        found, reason = tui.claude_session_from_runtime({}, [4242], {})
        self.assertIsNone(found)
        self.assertTrue(reason)

    def test_missing_ps_entry_is_unreadable_not_a_replaced_process(self):
        """An empty ps table means "not measured", not "process replaced".

        Both paths deny the id, so the *decision* is right either way -- but the
        focus line prints this reason verbatim.  Saying 进程已被替换 when ps
        simply returned nothing (timed out, or the binary is missing) sends the
        reader hunting for a restart that never happened.
        """

        import cmux_supervisor_tui as tui

        runtime = {"claude_session_id": SID_A, "claude_process_pid": 4242,
                   "claude_process_generation": tui.session_generation(
                       4242, "2026-08-29T20:13:33")}
        found, reason = tui.claude_session_from_runtime(runtime, [4242], {})
        self.assertIsNone(found)
        self.assertNotIn("已被替换", reason)
        self.assertIn("启动时间", reason)


class SessionFallbackConflictTests(unittest.TestCase):
    """Two PIDs of one surface disagreeing in the *fallback* tier.

    ``resolve_from_argv`` already refuses to choose between two argv ids, but
    the fallbacks ran in a per-PID loop that returned the first id it found.
    For a surface whose process tree holds two different rollout files that
    silently published one of them -- a coin flip presented as a fact, which is
    exactly what the task pack forbids ("do not arbitrarily select one from a
    conflict").
    """

    @staticmethod
    def _lsof_per_pid(by_pid):
        def runner(argv, **kwargs):
            pid = argv[argv.index("-p") + 1]
            sid = by_pid[pid]
            return types.SimpleNamespace(
                stdout=("codex 1 lzhs 1u REG 1,14 1 1 "
                        f"/a/rollout-2026-07-05T07-55-57-{sid}.jsonl\n"),
                returncode=0, stderr="")
        return runner

    def _ps_table(self):
        return {111: {"command": "codex", "started_at": "2026-08-29T20:13:33",
                      "ppid": "1"},
                222: {"command": "codex", "started_at": "2026-08-29T20:13:34",
                      "ppid": "1"}}

    def test_two_pids_with_different_rollouts_conflict(self):
        import cmux_supervisor_tui as tui

        result = tui.resolve_surface_session(
            "codex", [111, 222], self._ps_table(), {}, now=1.0,
            lsof_runner=self._lsof_per_pid({"111": SID_A, "222": SID_B}))
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.session_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.resume_command(), "")
        self.assertIn("2", result.reason)

    def test_two_pids_agreeing_on_one_id_is_not_a_conflict(self):
        """A parent/child pair sharing one rollout must still resolve."""

        import cmux_supervisor_tui as tui

        result = tui.resolve_surface_session(
            "codex", [111, 222], self._ps_table(), {}, now=1.0,
            lsof_runner=self._lsof_per_pid({"111": SID_C, "222": SID_C}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.session_id, SID_C)
        self.assertEqual(result.resume_command(), f"codex resume {SID_C}")


class SessionWorkerSurvivalTests(unittest.TestCase):
    """Malformed external input must not kill the resolution pass.

    ``agent_pids`` and ``state.json`` are both external.  An exception on the
    worker thread is not one bad row: it aborts the pass, so *every* surface
    shows 未测量 with no visible cause and no traceback anywhere the user looks.
    These tests watch ``threading.excepthook``, because a dead worker otherwise
    leaves a unittest assertion perfectly green.
    """

    def setUp(self):
        self.worker_errors = []
        self._prev_hook = threading.excepthook

        def capture(args):
            self.worker_errors.append(f"{args.exc_type.__name__}: {args.exc_value}")

        threading.excepthook = capture
        self.addCleanup(lambda: setattr(threading, "excepthook", self._prev_hook))

    def _resolver(self):
        import cmux_supervisor_tui as tui

        return tui.SessionResolver(
            ttl_sec=0.0,
            ps_runner=_ps_runner(_ps_line(111, f"codex --session-id {SID_A}")),
            lsof_runner=_fake_run(""),
            grok_path=Path("/nonexistent"),
            grok_loader=lambda *_: [])

    def test_unparseable_agent_pid_does_not_abort_the_pass(self):
        import cmux_supervisor_tui as tui

        resolver = self._resolver()
        resolver.maybe_refresh(
            [{"surface_id": "s:1", "agent_kind": "codex",
              "agent_pids": ["12a", None, {}, -5]},
             {"surface_id": "s:2", "agent_kind": "codex", "agent_pids": [111]}],
            {}, force=True)
        self.assertTrue(resolver.wait_for_refresh(10))
        self.assertEqual(self.worker_errors, [])
        snapshot = resolver.snapshot()
        self.assertEqual(sorted(snapshot), ["s:1", "s:2"])
        # The bad row is honestly unmeasured; its healthy neighbour still resolves.
        self.assertFalse(snapshot["s:1"].ok)
        self.assertEqual(snapshot["s:2"].session_id, SID_A)

    def test_non_mapping_runtime_entry_does_not_abort_the_pass(self):
        resolver = self._resolver()
        resolver.maybe_refresh(
            [{"surface_id": "s:1", "agent_kind": "claude", "agent_pids": [111]},
             {"surface_id": "s:2", "agent_kind": "codex", "agent_pids": [111]}],
            {"s:1": "not-a-mapping", "s:2": {}}, force=True)
        self.assertTrue(resolver.wait_for_refresh(10))
        self.assertEqual(self.worker_errors, [])
        self.assertEqual(sorted(resolver.snapshot()), ["s:1", "s:2"])

    def test_boolean_pid_is_dropped_rather_than_coerced_to_one(self):
        """``int(True)`` is 1 -- launchd's PID, and a real ps hit."""

        import cmux_supervisor_tui as tui

        self.assertEqual(tui._coerce_pids([True, False, 7]), [7])
        self.assertEqual(tui._coerce_pids("111"), [])
        self.assertEqual(tui._coerce_pids(None), [])


class SessionResolverTests(unittest.TestCase):
    """Tier order across all PIDs, the single-ps budget, and staleness."""

    def _resolver(self, ps_stdout="", lsof_stdout="", grok=None):
        import cmux_supervisor_tui as tui

        return tui.SessionResolver(
            ttl_sec=0.0,
            ps_runner=_fake_run(ps_stdout),
            lsof_runner=_fake_run(lsof_stdout),
            grok_path=Path("/nonexistent"),
            grok_loader=(lambda *_: grok if grok is not None else []),
        )

    def test_exactly_one_ps_per_refresh(self):
        """R4 forbids a per-PID ps: 15 Claude surfaces would mean 15 spawns."""

        import cmux_supervisor_tui as tui

        calls = []

        def counting(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(
                stdout="\n".join(
                    _ps_line(pid, f"codex --session-id {SID_A}")
                    for pid in (10, 11, 12, 13, 14)
                ),
                returncode=0, stderr="")

        resolver = tui.SessionResolver(ttl_sec=0.0, ps_runner=counting,
                                       lsof_runner=_fake_run(""),
                                       grok_path=Path("/nonexistent"),
                                       grok_loader=lambda *_: [])
        surfaces = [{"surface_id": f"S{pid}", "agent_kind": "codex",
                     "agent_pids": [pid]} for pid in (10, 11, 12, 13, 14)]
        resolver.maybe_refresh(surfaces, {}, force=True)
        self.assertTrue(resolver.wait_for_refresh(10))
        self.assertEqual(len(calls), 1, f"expected one ps, got {len(calls)}")
        self.assertIn("lstart=", " ".join(calls[0]))
        self.assertEqual(len(resolver.snapshot()), 5)

    def test_the_single_ps_carries_lstart_so_generation_needs_no_extra_call(self):
        import cmux_supervisor_tui as tui

        resolver = self._resolver()
        argv_seen = []
        resolver._ps_runner = lambda argv, **kw: (
            argv_seen.append(argv)
            or types.SimpleNamespace(stdout="", returncode=0, stderr=""))
        resolver.read_ps_table()
        self.assertEqual(argv_seen[0][:2], [str(tui.PS_BIN), "-axww"])
        self.assertIn("lstart=", argv_seen[0][-1])

    def test_all_pids_of_a_surface_are_inspected(self):
        import cmux_supervisor_tui as tui

        # The flag is on the *child*, which a first-PID-only reader would miss.
        table = tui.parse_ps_table("\n".join([
            _ps_line(100, "claude"),
            _ps_line(101, f"claude --resume {SID_A}", ppid=100),
        ]))
        result = tui.resolve_surface_session("claude", [100, 101], table, {},
                                             now=time.time())
        self.assertEqual((result.status, result.session_id), ("ok", SID_A))

    def test_six_pids_agreeing_on_nothing_is_unknown_not_conflict(self):
        import cmux_supervisor_tui as tui

        table = tui.parse_ps_table("\n".join(
            _ps_line(pid, "claude") for pid in range(200, 206)))
        result = tui.resolve_surface_session("claude", list(range(200, 206)),
                                             table, {}, now=time.time())
        self.assertEqual(result.status, "unknown")
        self.assertNotEqual(result.status, "conflict")

    def test_a_shell_surface_has_no_session(self):
        import cmux_supervisor_tui as tui

        result = tui.resolve_surface_session("unknown", [1], {}, {},
                                             now=time.time())
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.ok)

    def test_a_surface_with_no_pids_is_unmeasured(self):
        import cmux_supervisor_tui as tui

        result = tui.resolve_surface_session("codex", [], {}, {}, now=time.time())
        self.assertEqual(result.status, "unknown")
        self.assertIn("PID", result.reason)

    def test_argv_beats_the_fallback(self):
        """A fallback must never override an explicit flag."""

        import cmux_supervisor_tui as tui

        table = tui.parse_ps_table(_ps_line(300, f"codex --session-id {SID_A}"))
        lsof = (f"codex 300 lzhs 1u REG 1,14 1 1 "
                f"/a/rollout-2026-07-05T07-55-57-{SID_B}.jsonl")
        result = tui.resolve_surface_session(
            "codex", [300], table, {}, now=time.time(),
            lsof_runner=_fake_run(lsof))
        self.assertEqual(result.session_id, SID_A)
        self.assertEqual(result.tier, "session-id")

    def test_a_late_pass_cannot_overwrite_a_newer_one(self):
        """Stale worker results would resurrect a replaced process's id."""

        import cmux_supervisor_tui as tui

        resolver = self._resolver()
        surfaces = [{"surface_id": "S1", "agent_kind": "codex", "agent_pids": [1]}]
        resolver._results = {"S1": tui.SessionResult(status="ok", session_id=SID_B)}
        resolver._generation = 5
        # token 4 is older than the current generation 5, so it must be dropped.
        resolver._resolve_once(surfaces, {}, 4)
        self.assertEqual(resolver.snapshot()["S1"].session_id, SID_B)

    def test_refresh_is_not_duplicated_while_one_is_in_flight(self):
        import cmux_supervisor_tui as tui

        resolver = self._resolver()
        gate = threading.Event()

        def slow(argv, **kw):
            gate.wait(5)
            return types.SimpleNamespace(stdout="", returncode=0, stderr="")

        resolver._ps_runner = slow
        started_first = resolver.maybe_refresh([], {}, force=True)
        started_second = resolver.maybe_refresh([], {}, force=True)
        gate.set()
        resolver.wait_for_refresh(10)
        self.assertTrue(started_first)
        self.assertFalse(started_second, "a second pass must not stack")

    def test_ps_failure_leaves_every_row_unmeasured_rather_than_wrong(self):
        import cmux_supervisor_tui as tui

        resolver = tui.SessionResolver(
            ttl_sec=0.0, ps_runner=_fake_run(raises=OSError("boom")),
            lsof_runner=_fake_run(""), grok_path=Path("/nonexistent"),
            grok_loader=lambda *_: [])
        resolver.maybe_refresh(
            [{"surface_id": "S1", "agent_kind": "codex", "agent_pids": [1]}],
            {}, force=True)
        self.assertTrue(resolver.wait_for_refresh(10))
        self.assertFalse(resolver.snapshot()["S1"].ok)

    def test_result_for_an_unseen_surface_is_unmeasured(self):
        import cmux_supervisor_tui as tui

        self.assertFalse(self._resolver().result_for("nope").ok)

    def test_nothing_is_persisted_by_a_resolution_pass(self):
        """Memory-only: the resolver may read state, never write it."""

        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui.SessionResolver)
        for forbidden in ("write_text", "open(", "json.dump", "save",
                          "mkdir", "unlink", "replace("):
            self.assertNotIn(forbidden, source, forbidden)


class SessionRenderTests(unittest.TestCase):
    """The all-or-nothing cell, header/data agreement, and CJK widths."""

    CELLS = ("监控中", "ws11/p24/s59", "Claude", "正常", "45%", "空闲", "-", "3")

    def _row(self, width, session=SID_A, title="标题"):
        import cmux_supervisor_tui as tui

        return tui._row_text("  *  ", self.CELLS, title, width, session)

    def test_a_partial_uuid_is_never_rendered(self):
        """`9f7aa928-0525` looks valid, copies cleanly, and resumes nothing."""

        import cmux_supervisor_tui as tui

        for pane in (80, 100, 108, 120, 130, 131, 132, 145, 200):
            visible = self._row(pane - 1)[: pane - 1]
            if SID_A in visible:
                continue
            for length in range(8, len(SID_A)):
                self.assertNotIn(
                    SID_A[:length], visible,
                    f"pane {pane} leaked a {length}-char UUID prefix")

    def test_the_full_uuid_appears_once_the_cell_fits(self):
        import cmux_supervisor_tui as tui

        self.assertIn(SID_A, self._row(131))   # clip for a 132-column pane
        self.assertIn(SID_A, self._row(144))

    def test_a_narrow_terminal_omits_the_column_with_no_marker(self):
        """Narrow rows now END, they do not carry a clipped hint.

        The marker used to be re-drawn on every row and then clipped by the very
        width that triggered it, so a narrow terminal showed a column of
        fragments.  The replacement invariant is absence: no marker anywhere, no
        UUID fragment anywhere, and the full ID still reachable in focus detail.
        """

        import cmux_supervisor_tui as tui

        for pane in (100, 108, 120, 130, 131):
            row = self._row(pane - 1)
            self.assertNotIn(tui.SESSION_NARROW_MARKER, row, f"pane {pane}")
            self.assertNotIn(SID_A, row, f"pane {pane}")
            for length in range(8, len(SID_A)):
                self.assertNotIn(SID_A[:length], row, f"pane {pane} / {length}")
            # Absence must be a clean end of row, not a padded blank column.
            self.assertEqual(row, row.rstrip(), f"pane {pane}")

    def test_the_omitted_column_is_never_a_marker_at_any_width(self):
        """98..103 is the band where the old marker was drawn clipped.

        Asserted here as ABSENCE at every width, not as a complete marker at
        those widths: item 2 removes the marker unconditionally, so an assertion
        that the marker appears anywhere would be unsatisfiable by construction.
        """

        import cmux_supervisor_tui as tui

        for clip in list(range(30, 201)) + [98, 99, 100, 101, 102, 103, 400]:
            row = self._row(clip)
            self.assertNotIn(tui.SESSION_NARROW_MARKER, row, f"clip {clip}")
            if SID_A not in row:
                for length in range(8, len(SID_A)):
                    self.assertNotIn(SID_A[:length], row, f"clip {clip} / {length}")

    def test_header_and_rows_agree_about_the_column(self):
        import cmux_supervisor_tui as tui

        for pane in (80, 100, 108, 120, 130, 131, 132, 145):
            clip = pane - 1
            header = tui.header_text(clip)[:clip]
            row = self._row(clip)[:clip]
            self.assertEqual(
                "session" in header, SID_A in row,
                f"pane {pane}: header says {'session' in header}, row says {SID_A in row}")

    def test_the_title_is_a_fixed_twelve_columns(self):
        import cmux_supervisor_tui as tui

        for title in ("t", "标题", "标题标题标题标题标题标题", "a" * 40):
            row = self._row(200, title=title)
            head, _, tail = row.rpartition(" " + SID_A)
            self.assertTrue(tail == "" or SID_A in row)
            # The UUID always starts at the same column regardless of title.
            self.assertEqual(display_width_of(head), display_width_of(
                self._row(200, title="x").rpartition(" " + SID_A)[0]), title)

    def test_a_wide_glyph_is_never_split(self):
        import cmux_supervisor_tui as tui

        row = self._row(200, title="标题标题标题标题标题标题标题标题")
        self.assertEqual(display_width_of(row), display_width_of(self._row(200)))

    def test_unmeasured_rows_show_the_label_not_a_blank(self):
        import cmux_supervisor_tui as tui

        row = self._row(144, session=tui.SESSION_UNMEASURED)
        self.assertIn(tui.SESSION_UNMEASURED, row)

    def test_callers_without_a_terminal_keep_the_nine_column_row(self):
        """The historical signature still works, so old callers cannot break."""

        import cmux_supervisor_tui as tui

        self.assertEqual(display_width_of(tui.header_text()), 86)
        self.assertNotIn("session", tui.header_text())
        self.assertNotIn(SID_A, tui._row_text("  ", self.CELLS, "t"))

    def test_session_cell_reports_when_it_cannot_fit(self):
        import cmux_supervisor_tui as tui

        self.assertIsNone(tui.session_cell(SID_A, tui.SESSION_COL_WIDTH - 1))
        self.assertEqual(
            display_width_of(tui.session_cell(SID_A, tui.SESSION_COL_WIDTH)),
            tui.SESSION_COL_WIDTH)


class SessionDrawChainWidthTests(unittest.TestCase):
    """The width contract through the REAL draw chain, not the row builder alone.

    ``_draw`` computes ``clip = max(1, width - 1)`` (tui:2881) and passes that
    single value both to ``_row_text``/``header_text`` and to ``_safe_addnstr``.
    So a terminal of W columns only ever gets W-1 columns of row.  Testing
    ``_row_text(width=W)`` therefore measures the wrong quantity: it says a
    complete row fits at 131, while a 131-column *terminal* clips to 130 and the
    cell falls back to the marker.  An earlier probe of mine made exactly that
    substitution and concluded 131 was displayable.  These tests drive the chain.
    """

    SID = "a1a1a1a1-b1b1-7b11-8b11-c1c1c1c1c1c1"
    CELLS = ("正常", "workspace:18", "codex", "健康", "45%", "空闲", "-", "3")

    def _drawn_row(self, terminal_width, session_text):
        """What lands in the window for a terminal of ``terminal_width``."""

        import cmux_supervisor_tui as tui

        clip = max(1, terminal_width - 1)          # exactly tui:2881
        label = tui._row_text("     ", self.CELLS, "标题", clip, session_text)
        written = []

        class _Screen:
            def addnstr(self, y, x, text, n, attr=0):
                written.append(text)

        tui._safe_addnstr(_Screen(), 0, 0, label, clip)
        return written[0] if written else ""

    def _drawn_header(self, terminal_width):
        import cmux_supervisor_tui as tui

        clip = max(1, terminal_width - 1)
        written = []

        class _Screen:
            def addnstr(self, y, x, text, n, attr=0):
                written.append(text)

        tui._safe_addnstr(_Screen(), 0, 0, tui.header_text(clip), clip)
        return written[0] if written else ""

    def test_130_and_131_omit_the_column_with_no_marker_or_fragment(self):
        """Below the fit boundary the column is absent, not hinted at.

        Renamed from ...show_the_marker...: the marker is retired, so this is now
        a negative assertion.  It is NOT deleted, because the fragment ban it
        protects is the whole reason the boundary matters.
        """

        import cmux_supervisor_tui as tui

        for width in (130, 131):
            drawn = self._drawn_row(width, self.SID)
            self.assertNotIn(tui.SESSION_NARROW_MARKER, drawn,
                             f"width {width} must not carry any narrow marker")
            self.assertNotIn(self.SID, drawn, width)
            # No prefix of the id may survive: a fragment copies cleanly and
            # resumes the wrong session, which is worse than showing nothing.
            for cut in range(8, len(self.SID) + 1):
                self.assertNotIn(self.SID[:cut], drawn,
                                 f"width {width} leaked a {cut}-char fragment")

    def test_132_and_145_draw_the_complete_uuid(self):
        import cmux_supervisor_tui as tui

        for width in (132, 145):
            drawn = self._drawn_row(width, self.SID)
            self.assertIn(self.SID, drawn, f"width {width} must show the whole id")
            self.assertNotIn(tui.SESSION_NARROW_MARKER, drawn, width)
            self.assertLessEqual(tui.display_width(drawn), width - 1,
                                 "a drawn row may never exceed the clip")

    def test_132_is_the_first_displayable_terminal_width(self):
        """The boundary itself, swept AND derived -- not a remembered number.

        132 stays asserted because it is the historical, user-visible boundary,
        but it is now cross-checked against the layout measurement, so a future
        column change moves both together or fails loudly instead of silently
        making one of them a lie.
        """

        import cmux_supervisor_tui as tui

        widths = [w for w in range(60, 200)
                  if self.SID in self._drawn_row(w, self.SID)]
        self.assertTrue(widths, "the id must be displayable at some width")
        # Derived: the first terminal width whose clip admits the session cell.
        derived = min(w for w in range(2, 400)
                      if tui.row_layout(max(1, w - 1)).session)
        self.assertEqual(min(widths), derived,
                         "the drawn boundary must equal the measured one")
        self.assertEqual(derived, 132)
        # And it stays displayable once it fits.
        self.assertEqual(widths, list(range(132, 200)))

    def test_header_and_rows_agree_through_the_same_clip(self):
        """The header may advertise `session` only where a row can carry one."""

        named = (51, 54, 60, 79, 80, 81, 99, 100, 101, 106, 107, 114, 132, 160)
        for width in sorted(set(named) | set(range(30, 201))):
            header_has = "session" in self._drawn_header(width)
            row_has = self.SID in self._drawn_row(width, self.SID)
            self.assertEqual(header_has, row_has,
                             f"width {width}: header={header_has} row={row_has}")

    def test_no_width_leaks_a_marker_or_a_uuid_fragment(self):
        """Every width from 30 to 200, plus the plan's named draw widths."""

        import cmux_supervisor_tui as tui

        named = (51, 54, 60, 79, 80, 81, 99, 100, 101, 106, 107, 114, 132, 160)
        for width in sorted(set(named) | set(range(30, 201))):
            drawn = self._drawn_row(width, self.SID)
            self.assertNotIn(tui.SESSION_NARROW_MARKER, drawn, f"width {width}")
            self.assertLessEqual(tui.display_width(drawn), max(1, width - 1),
                                 f"width {width} drew past its clip")
            if self.SID not in drawn:
                for cut in range(8, len(self.SID) + 1):
                    self.assertNotIn(self.SID[:cut], drawn,
                                     f"width {width} leaked {cut} chars")

    def test_an_unmeasured_row_never_shows_a_marker_instead_of_a_reason(self):
        """未测量 is a value, not a width failure; it must render at any width."""

        import cmux_supervisor_tui as tui

        drawn = self._drawn_row(145, tui.SESSION_UNMEASURED)
        self.assertIn(tui.SESSION_UNMEASURED, drawn)
        self.assertNotIn(tui.SESSION_NARROW_MARKER, drawn)


class SessionFocusAndSearchTests(unittest.TestCase):
    """The focus line, generated commands, and full/prefix search."""

    def test_the_focus_line_generates_each_agents_resume_command(self):
        import cmux_supervisor_tui as tui

        expected = {"codex": f"codex resume {SID_A}",
                    "claude": f"Claude --resume {SID_A}",
                    "grok": f"grok --resume {SID_A}"}
        for kind, command in expected.items():
            candidate = _session_candidate(
                tui.SessionResult(status="ok", session_id=SID_A,
                                  agent_kind=kind, tier="session-id",
                                  measured_at=time.time()),
                agent_kind=kind)
            self.assertIn(command, tui.session_detail(candidate))

    def test_an_unmeasured_session_generates_no_command(self):
        """A command that resumes the wrong conversation is worse than none."""

        import cmux_supervisor_tui as tui

        detail = tui.session_detail(_session_candidate(
            tui.SessionResult(status="conflict", reason="两个不同 ID")))
        self.assertIn(tui.SESSION_UNMEASURED, detail)
        self.assertIn("两个不同 ID", detail)
        for verb in ("resume", "--resume"):
            self.assertNotIn(verb, detail)

    def test_the_reason_is_not_restated_when_absent(self):
        import cmux_supervisor_tui as tui

        self.assertEqual(
            tui.session_detail(_session_candidate(tui.SessionResult())),
            f"session {tui.SESSION_UNMEASURED}")

    def test_the_age_reads_naturally(self):
        import cmux_supervisor_tui as tui

        detail = tui.session_detail(_session_candidate(
            tui.SessionResult(status="ok", session_id=SID_A, agent_kind="codex",
                              measured_at=time.time() - 185)))
        self.assertIn("测得", detail)
        self.assertNotIn("前前", detail)

    def test_search_matches_the_full_uuid_and_any_prefix(self):
        import cmux_supervisor_tui as tui

        rows = [
            _session_candidate(tui.SessionResult(status="ok", session_id=SID_A),
                               surface_id="S-A"),
            _session_candidate(tui.SessionResult(status="ok", session_id=SID_B),
                               surface_id="S-B"),
        ]
        for needle in (SID_A, SID_A[:8], SID_A[:18], SID_A[9:13], SID_A.upper()):
            found = tui.filter_candidates(rows, "all", needle)
            self.assertEqual([item.surface_id for item in found], ["S-A"], needle)

    def test_an_unmeasured_row_is_not_findable_by_someone_elses_uuid(self):
        import cmux_supervisor_tui as tui

        rows = [_session_candidate(tui.SessionResult(), surface_id="S-A")]
        self.assertEqual(tui.filter_candidates(rows, "all", SID_A), [])

    def test_existing_search_keys_still_work(self):
        import cmux_supervisor_tui as tui

        rows = [_session_candidate(tui.SessionResult(), surface_id="S-A")]
        for needle in ("workspace:18", "s9", "ws18"):
            self.assertEqual(len(tui.filter_candidates(rows, "all", needle)), 1,
                             needle)


class SessionIsolationTests(unittest.TestCase):
    """The draw path stays free of processes and the filesystem."""

    def test_draw_does_not_resolve_sessions_itself(self):
        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._draw)
        for forbidden in ("resolve_surface_session", "read_ps_table",
                          "codex_session_from_lsof", "grok_session_for_pid",
                          "load_json", "lsof", "subprocess."):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_draw_path_only_reads_a_precomputed_cell(self):
        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui._draw)
        self.assertIn("session_text", source)

    def test_the_isolation_gate_would_catch_load_json_in_the_draw_path(self):
        """A mutation the gate must fail, proving it covers the new read."""

        import cmux_supervisor_tui as tui

        blocking = {"load_json", "lsof", "subprocess", "run", "open"}
        mutated = "def _draw(stdscr):\n    core.load_json(Path('x'), {})\n"
        self.assertTrue(
            any(name in mutated for name in blocking),
            "the gate's forbidden set must name load_json")

    def test_session_resolution_happens_on_a_thread(self):
        import cmux_supervisor_tui as tui

        source = inspect.getsource(tui.SessionResolver.maybe_refresh)
        self.assertIn("threading.Thread", source)
        self.assertIn("daemon=True", source)
        self.assertNotIn(".join(", source)


class SessionNoRawRetentionTests(unittest.TestCase):
    """Nothing raw survives resolution: no paths, no argv, no lsof output.

    The fallbacks read two of the most sensitive things on the machine -- every
    open file descriptor of a process, and full command lines that can carry
    model and launch configuration.  The captured UUID is the only thing that
    may outlive the parse.  A mutation that stashed the matched *path* instead
    of the id passed every other test in this file, because a path happens to
    contain the id.
    """

    # Built from SID_A rather than a pasted literal: an inline copy is what let
    # a real session id survive the first scrub of this file.
    ROLLOUT = ("/Users/someone/.codex/sessions/2026/07/05/"
               f"rollout-2026-07-05T07-55-57-{SID_A}.jsonl")

    def _lsof(self, extra=""):
        import types

        text = (
            "COMMAND   PID  USER   FD   TYPE DEVICE  SIZE/OFF     NODE NAME\n"
            "codex   3636  tester  cwd    DIR   1,14       704 12063724 /Users/someone/secret-project\n"
            "codex   3636  lzhs   12r   REG   1,14   1331483 12063724 " + self.ROLLOUT + "\n"
            "codex   3636  lzhs   13u  IPv4 0x1234       0t0      TCP 10.0.0.2:52344->1.2.3.4:443\n"
            + extra
        )
        return lambda *a, **k: types.SimpleNamespace(stdout=text, returncode=0)

    def test_only_the_uuid_survives_an_lsof_parse(self):
        import cmux_supervisor_tui as tui

        found, why = tui.codex_session_from_lsof(3636, runner=self._lsof())
        self.assertEqual(found, SID_A)
        self.assertEqual(why, "")
        # The id, and nothing that came wrapped around it.
        self.assertNotIn("/", str(found))
        self.assertNotIn(".jsonl", str(found))
        self.assertNotIn("secret-project", str(found))

    def test_the_resolved_record_contains_no_path_or_raw_text(self):
        import dataclasses

        import cmux_supervisor_tui as tui

        result = tui.resolve_surface_session(
            "codex", [3636],
            {3636: {"ppid": "1", "started_at": "2026-08-29T20:13:33",
                    "command": "/usr/local/bin/codex --model o3 --dangerous-flag"}},
            {}, now=1000.0, lsof_runner=self._lsof(),
        )
        self.assertTrue(result.ok)
        for field in dataclasses.fields(result):
            value = str(getattr(result, field.name) or "")
            self.assertNotIn("/", value, f"{field.name} holds a path separator")
            self.assertNotIn(".jsonl", value, f"{field.name} holds a filename")
            self.assertNotIn("secret-project", value, f"{field.name} holds raw lsof text")
            self.assertNotIn("dangerous-flag", value, f"{field.name} holds raw argv")
            self.assertNotIn("--model", value, f"{field.name} holds raw argv")

    def test_two_rollouts_are_a_conflict_and_neither_path_is_kept(self):
        import cmux_supervisor_tui as tui

        second = ("codex   3636  lzhs   14r   REG   1,14   999 999 "
                  "/Users/someone/.codex/sessions/2026/07/06/"
                  f"rollout-2026-07-06T01-02-03-{SID_C}.jsonl\n")
        found, why = tui.codex_session_from_lsof(3636, runner=self._lsof(second))
        self.assertIsNone(found)
        self.assertIn("2", why)
        self.assertNotIn("/", why, "the reason must not name a path either")

    def test_no_module_level_sink_accumulates_raw_output(self):
        """A mutation stashed raw lsof lines in a module global and escaped.

        Resolution is memory-only *and* bounded: there is no list or dict at
        module scope that grows with every pass.
        """

        import cmux_supervisor_tui as tui

        before = {
            name: len(value)
            for name, value in vars(tui).items()
            if isinstance(value, (list, dict, set)) and not name.startswith("__")
        }
        for _ in range(3):
            tui.codex_session_from_lsof(3636, runner=self._lsof())
            tui.resolve_from_argv([f"codex --session-id {SID_A}"])
        after = {
            name: len(value)
            for name, value in vars(tui).items()
            if isinstance(value, (list, dict, set)) and not name.startswith("__")
        }
        grew = {name: (before.get(name), size)
                for name, size in after.items()
                if before.get(name) != size}
        self.assertEqual(grew, {}, f"module-level container grew: {grew}")

    def test_the_resolver_cache_holds_only_validated_records(self):
        import cmux_supervisor_tui as tui

        ps = ("3636 1 Sat Aug 29 20:13:33 2026 /usr/local/bin/codex "
              f"--session-id {SID_A} --secret-token abc123\n")
        resolver = tui.SessionResolver(
            ps_runner=_ps_runner(ps), lsof_runner=self._lsof(),
        )
        resolver.maybe_refresh(
            [{"surface_id": "S1", "agent_kind": "codex", "agent_pids": [3636]}],
            {}, force=True,
        )
        self.assertTrue(resolver.wait_for_refresh(10))
        for surface_id, result in resolver.snapshot().items():
            self.assertIsInstance(result, tui.SessionResult)
            blob = repr(result)
            self.assertNotIn("secret-token", blob)
            self.assertNotIn("abc123", blob)
            self.assertNotIn("/usr/local/bin", blob)


class SessionModelWiringTests(unittest.TestCase):
    """The panel starts one pass per refresh and carries results onto rows."""

    def test_refresh_starts_a_pass_and_attaches_results(self):
        import cmux_supervisor_tui as tui

        class _Recorder:
            def __init__(self):
                self.calls = []
                self._results = {}

            def maybe_refresh(self, surfaces, runtimes, *, now=None, force=False):
                self.calls.append((list(surfaces), dict(runtimes), force))
                return True

            def snapshot(self):
                return dict(self._results)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.json").write_text(json.dumps({"targets": []}))
            recorder = _Recorder()
            model = tui.SupervisorModel(root / "config.json",
                                        client=_DiscoveryClient(),
                                        janitor=_StubJanitor(),
                                        sessions=recorder)
            model.refresh(force=True)

            self.assertEqual(len(recorder.calls), 1, "one pass per refresh")
            surfaces, _runtimes, forced = recorder.calls[0]
            self.assertTrue(forced)
            self.assertTrue(surfaces, "every classified surface is offered")
            for item in surfaces:
                self.assertIn("surface_id", item)
                self.assertIn("agent_kind", item)
                self.assertIn("agent_pids", item)

            # Every row carries a SessionResult even before the first pass lands.
            for candidate in model.candidates:
                self.assertIsInstance(candidate.session, tui.SessionResult)
                self.assertEqual(candidate.session_text, tui.SESSION_UNMEASURED)

    def test_a_ref_keyed_classification_table_still_yields_pids(self):
        """The resolver payload must use the UUID->ref fallback, not a bare get.

        ``classify_surface_processes`` keys entries by surface UUID *when the
        payload carries one*, and by ``ref`` when it does not (watch:3218-3221).
        A ``process_by_id.get(surface_id)`` therefore returns nothing for a
        ref-keyed surface, and the failure is silent: the row still draws, the
        session column just stays 未测量 forever with no error anywhere.
        """

        import cmux_supervisor_tui as tui

        class _RefKeyedClient:
            """``top`` entries carry ref but no id, so classification keys on ref."""

            def tree(self):
                return {"windows": [{"workspaces": [{
                    "id": "workspace-11", "ref": "workspace:11", "title": "Hermes",
                    "panes": [{"id": "pane-24", "ref": "pane:24", "surfaces": [
                        {"id": "surface-59", "ref": "surface:59",
                         "type": "terminal", "title": "Codex"},
                    ]}],
                }]}]}

            def top_all(self):
                return {"windows": [{"workspaces": [{"surfaces": [
                    {"kind": "surface", "ref": "surface:59",
                     "processes": [{"kind": "process", "name": "codex",
                                    "path": "/bin/codex", "pid": 4242}]},
                ]}]}]}

        captured = []

        class _Recorder:
            def maybe_refresh(self, surfaces, runtimes, *, now=None, force=False):
                captured.append([dict(item) for item in surfaces])
                return True

            def snapshot(self):
                return {}

        # Precondition: the table really is ref-keyed, so a bare UUID lookup misses.
        table = core.classify_surface_processes(_RefKeyedClient().top_all())
        self.assertEqual(sorted(table), ["surface:59"])
        self.assertIsNone(table.get("surface-59"))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.json").write_text(json.dumps({"targets": []}))
            model = tui.SupervisorModel(root / "config.json",
                                        client=_RefKeyedClient(),
                                        janitor=_StubJanitor(),
                                        sessions=_Recorder())
            model.refresh(force=True)

        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["agent_kind"], "codex",
                         "agent_kind lost: the ref fallback was not used")
        self.assertEqual(list(payload[0]["agent_pids"]), [4242],
                         "agent_pids lost: the ref fallback was not used")

    def test_a_ref_keyed_surface_resolves_end_to_end(self):
        """And the id actually lands, not merely the pids."""

        import cmux_supervisor_tui as tui

        class _RefKeyedClient:
            def tree(self):
                return {"windows": [{"workspaces": [{
                    "id": "workspace-11", "ref": "workspace:11", "title": "H",
                    "panes": [{"id": "pane-24", "ref": "pane:24", "surfaces": [
                        {"id": "surface-59", "ref": "surface:59",
                         "type": "terminal", "title": "Codex"},
                    ]}],
                }]}]}

            def top_all(self):
                return {"windows": [{"workspaces": [{"surfaces": [
                    {"kind": "surface", "ref": "surface:59",
                     "processes": [{"kind": "process", "name": "codex",
                                    "path": "/bin/codex", "pid": 4242}]},
                ]}]}]}

        resolver = tui.SessionResolver(
            ttl_sec=0.0,
            ps_runner=_ps_runner(_ps_line(4242, f"codex --session-id {SID_A}")),
            lsof_runner=_fake_run(""),
            grok_path=Path("/nonexistent"),
            grok_loader=lambda *_: [])

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.json").write_text(json.dumps({"targets": []}))
            model = tui.SupervisorModel(root / "config.json",
                                        client=_RefKeyedClient(),
                                        janitor=_StubJanitor(),
                                        sessions=resolver)
            model.refresh(force=True)
            self.assertTrue(resolver.wait_for_refresh(10))
            resolved = resolver.snapshot()

        self.assertEqual(sorted(resolved), ["surface-59"])
        self.assertEqual(resolved["surface-59"].session_id, SID_A)

    def test_a_vanished_target_row_is_unmeasured(self):
        """No process, so there is nothing to resolve and nothing to claim."""

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.json").write_text(json.dumps({"targets": [
                {"surface_id": "gone-1", "workspace_id": "workspace-11",
                 "paused": False, "name": "gone"},
            ]}))
            model = tui.SupervisorModel(root / "config.json",
                                        client=_DiscoveryClient(),
                                        janitor=_StubJanitor())
            model.refresh(force=True)
            vanished = [c for c in model.candidates if c.surface_id == "gone-1"]
            self.assertEqual(len(vanished), 1)
            self.assertEqual(vanished[0].session_text, tui.SESSION_UNMEASURED)


class CollabColumnTests(unittest.TestCase):
    """The 协作 column: harness markers in, role labels and connectors out.

    The column is a read-only projection of the multi-agent-collaboration
    harness's active markers (schemas/active-marker.schema.json; v2 is the
    written contract, v1 stays read-compatible).  Everything here enforces the
    rules that keep it honest: participants join by surface UUID only -- never
    by ref -- anything not provably fresh renders as nothing at all, and a v2
    marker that breaks its own invariants is rejected whole.
    """

    WS_UUID = "beefbeef-0000-4000-8000-000000000001"
    SUP_UUID = "a0a0a0a0-1111-4111-8111-000000000001"
    EXE_UUID = "a0a0a0a0-1111-4111-8111-000000000002"
    EXE2_UUID = "a0a0a0a0-1111-4111-8111-000000000003"
    EXE3_UUID = "a0a0a0a0-1111-4111-8111-000000000004"

    def _marker(self, *, now=1_000_000.0, armed_ago=60.0, activity_ago=5.0,
                ttl=21600, version=1, executors=1, drop_uuid_for=()):
        import datetime as dt

        def iso(age):
            return dt.datetime.fromtimestamp(
                now - age, tz=dt.timezone.utc).isoformat()

        participants = [{
            "role": "supervisor", "display": "Supervisor",
            "surface_ref": "surface:153", "surface_uuid": self.SUP_UUID,
            "provider": "codex",
        }]
        for i in range(executors):
            participants.append({
                "role": "executor" if executors == 1 else f"executor{i + 1}",
                "display": "Executor",
                "surface_ref": f"surface:{104 + i}",
                "surface_uuid": (self.EXE_UUID, self.EXE2_UUID)[i],
                "provider": "claude",
            })
        for item in participants:
            if item["role"] in drop_uuid_for:
                item["surface_uuid"] = ""
        return {
            "marker_version": version,
            "task_id": "fin-demo",
            "workspace_uuid": self.WS_UUID,
            "armed_at": iso(armed_ago),
            "last_activity_at": iso(activity_ago),
            "ttl_seconds": ttl,
            "participants": participants,
        }

    def _marker_v2(self, *, now=1_000_000.0, armed_ago=60.0, activity_ago=5.0,
                   ttl=21600, executors=1, collab_id="collab-0001",
                   task_id="fin-demo-v2", workspace_uuid=None, providers=(),
                   ordinals=None, supervisor_ordinal=0, mutate=None):
        """A v2-contract marker; ``mutate`` lets a test break one invariant."""
        import datetime as dt

        def iso(age):
            return dt.datetime.fromtimestamp(
                now - age, tz=dt.timezone.utc).isoformat()

        exe_uuids = (self.EXE_UUID, self.EXE2_UUID, self.EXE3_UUID)
        participants = [{
            "role": "supervisor", "ordinal": supervisor_ordinal,
            "surface_ref": "surface:153", "surface_uuid": self.SUP_UUID,
            "provider": "codex",
        }]
        for i in range(executors):
            participants.append({
                "role": "executor",
                "ordinal": ordinals[i] if ordinals else i + 1,
                "surface_ref": f"surface:{104 + i}",
                "surface_uuid": exe_uuids[i],
                "provider": providers[i] if providers else "claude",
            })
        marker = {
            "marker_version": 2,
            "collaboration_id": collab_id,
            "task_id": task_id,
            "artifact_root": "/tmp/fin-demo-v2",
            "workspace_id": workspace_uuid or self.WS_UUID,
            "workspace_uuid": workspace_uuid or self.WS_UUID,
            "armed_at": iso(armed_ago),
            "last_activity_at": iso(activity_ago),
            "ttl_seconds": ttl,
            "participants": participants,
        }
        if mutate:
            mutate(marker)
        return marker

    # -- marker parsing and freshness -------------------------------------

    def test_a_fresh_marker_yields_supervisor_and_executor_roles(self):
        import cmux_supervisor_tui as tui

        roles = tui.collab_roles([self._marker()], now=1_000_000.0)
        self.assertEqual(roles[self.SUP_UUID].label, "Supervisor")
        self.assertEqual(roles[self.EXE_UUID].label, "Executor")
        self.assertEqual(roles[self.SUP_UUID].task_id, "fin-demo")
        self.assertEqual(roles[self.SUP_UUID].workspace_uuid, self.WS_UUID)
        # The peer text names the other side, so the focus line can show it.
        self.assertIn("Executor", roles[self.SUP_UUID].peer_text)

    def test_two_executors_are_numbered_in_participant_order(self):
        import cmux_supervisor_tui as tui

        roles = tui.collab_roles([self._marker(executors=2)], now=1_000_000.0)
        self.assertEqual(roles[self.EXE_UUID].label, "Executor1")
        self.assertEqual(roles[self.EXE2_UUID].label, "Executor2")
        self.assertEqual(roles[self.SUP_UUID].label, "Supervisor")

    def test_stale_expired_and_future_contract_markers_yield_nothing(self):
        """History must be invisible: a marker outlives its usefulness three
        ways -- TTL expiry, heartbeat silence, or a contract this build does
        not understand -- and each one must blank the column, not guess."""

        import cmux_supervisor_tui as tui

        now = 1_000_000.0
        for bad in (
            self._marker(armed_ago=30_000.0, ttl=21600),        # armed_at + ttl passed
            # A real marker always has armed_at <= last_activity_at (arming
            # writes both, heartbeats only move the latter), so silence means
            # BOTH are old.  An hour without a heartbeat is a stopped task.
            self._marker(armed_ago=7_200.0, activity_ago=3_601.0),
            self._marker(version=3),                            # unknown contract
            # v1 participant shapes under a v2 version stamp break the v2
            # invariants (no ordinals) and must be rejected whole.
            self._marker(version=2),
            {**self._marker(), "armed_at": "not-a-date"},       # unparseable
            {**self._marker(), "ttl_seconds": 0},               # zero ttl
            {**self._marker(), "participants": "oops"},         # wrong shape
        ):
            self.assertEqual(tui.collab_roles([bad], now=now), {}, bad)

    # -- v2 contract: strict heartbeat and whole-marker invariants ----------

    def test_a_fresh_v2_marker_yields_roles_with_provider_detail(self):
        import cmux_supervisor_tui as tui

        roles = tui.collab_roles(
            [self._marker_v2(providers=("grok",))], now=1_000_000.0)
        self.assertEqual(roles[self.SUP_UUID].label, "Supervisor")
        self.assertEqual(roles[self.EXE_UUID].label, "Executor")
        self.assertEqual(roles[self.EXE_UUID].provider, "grok")
        self.assertEqual(roles[self.SUP_UUID].collab_id, "collab-0001")

    def test_v2_executor_labels_come_from_ordinal_not_array_order(self):
        import cmux_supervisor_tui as tui

        # Serialised with the executors swapped in the array: ordinals 2, 1.
        marker = self._marker_v2(executors=2, ordinals=(2, 1),
                                 providers=("claude", "grok"))
        marker["participants"][1:] = reversed(marker["participants"][1:])
        roles = tui.collab_roles([marker], now=1_000_000.0)
        self.assertEqual(roles[self.EXE_UUID].label, "Executor2")
        self.assertEqual(roles[self.EXE2_UUID].label, "Executor1")

    def test_a_supervisor_with_three_executors_numbers_them_all(self):
        import cmux_supervisor_tui as tui

        roles = tui.collab_roles(
            [self._marker_v2(executors=3,
                             providers=("claude", "grok", "copilot"))],
            now=1_000_000.0)
        self.assertEqual(roles[self.EXE_UUID].label, "Executor1")
        self.assertEqual(roles[self.EXE2_UUID].label, "Executor2")
        self.assertEqual(roles[self.EXE3_UUID].label, "Executor3")

    def test_v2_invariant_breakers_hide_the_whole_marker(self):
        """Per-participant salvage could label the wrong surface, so a v2
        marker that cannot prove its own shape contributes nothing."""

        import cmux_supervisor_tui as tui

        def dup_uuid(m):
            m["participants"][1]["surface_uuid"] = self.SUP_UUID

        def second_supervisor(m):
            m["participants"].append(dict(m["participants"][0]))

        def no_executor(m):
            m["participants"] = m["participants"][:1]

        def dup_ordinal(m):
            m["participants"][2]["ordinal"] = 1

        def stray_role(m):
            m["participants"][1]["role"] = "executor2"

        def string_ordinal(m):
            m["participants"][1]["ordinal"] = "1"

        cases = [self._marker_v2(mutate=dup_uuid),
                 self._marker_v2(mutate=second_supervisor),
                 self._marker_v2(mutate=no_executor),
                 self._marker_v2(executors=2, mutate=dup_ordinal),
                 self._marker_v2(mutate=stray_role),
                 self._marker_v2(mutate=string_ordinal),
                 self._marker_v2(supervisor_ordinal=1)]
        for bad in cases:
            self.assertEqual(tui.collab_roles([bad], now=1_000_000.0), {}, bad)

    def test_v2_heartbeat_is_mandatory_and_never_falls_back(self):
        """The v1 fallback to armed_at was bounded fail-open; v2 markers hide
        outright when the liveness signal is missing, malformed, or claims
        activity before the task was armed."""

        import cmux_supervisor_tui as tui

        def drop_heartbeat(m):
            del m["last_activity_at"]

        def garbage_heartbeat(m):
            m["last_activity_at"] = "not-a-date"

        cases = [self._marker_v2(mutate=drop_heartbeat),
                 self._marker_v2(mutate=garbage_heartbeat),
                 self._marker_v2(armed_ago=60.0, activity_ago=120.0)]
        for bad in cases:
            self.assertEqual(tui.collab_roles([bad], now=1_000_000.0), {}, bad)
        # The same shapes under v1 keep the migration-window fallback.
        v1 = self._marker()
        del v1["last_activity_at"]
        self.assertIn(self.SUP_UUID, tui.collab_roles([v1], now=1_000_000.0))

    def test_v1_and_v2_markers_coexist_and_first_claim_on_a_uuid_wins(self):
        import cmux_supervisor_tui as tui

        other_ws = "beefbeef-0000-4000-8000-00000000000f"
        v1 = self._marker()                       # claims SUP_UUID + EXE_UUID
        v2 = self._marker_v2(workspace_uuid=other_ws, collab_id="collab-x")
        roles = tui.collab_roles([v1, v2], now=1_000_000.0)
        # Both markers name SUP_UUID; the first read keeps it, deterministically.
        self.assertEqual(roles[self.SUP_UUID].task_id, "fin-demo")
        self.assertEqual(roles[self.SUP_UUID].collab_id, "task:fin-demo")
        self.assertEqual(roles[self.EXE_UUID].task_id, "fin-demo")

    def test_a_participant_without_a_uuid_is_skipped_not_guessed(self):
        """Refs renumber (the 2026-08-20 join incident); a missing UUID must
        remove that participant, never fall back to surface_ref."""

        import cmux_supervisor_tui as tui

        roles = tui.collab_roles(
            [self._marker(drop_uuid_for=("supervisor",))], now=1_000_000.0)
        self.assertNotIn(self.SUP_UUID, roles)
        self.assertIn(self.EXE_UUID, roles)          # the intact peer survives

    def test_unreadable_marker_files_degrade_to_no_collaboration(self):
        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "broken.json").write_text("{not json", encoding="utf-8")
            (directory / "list.json").write_text("[1, 2]", encoding="utf-8")
            (directory / "good.json").write_text(
                json.dumps(self._marker()), encoding="utf-8")
            # v2 layout: per-collaboration files one level down, temp files hidden.
            ws_dir = directory / self.WS_UUID
            ws_dir.mkdir()
            (ws_dir / "collab-0001.json").write_text(
                json.dumps(self._marker_v2()), encoding="utf-8")
            (ws_dir / ".collab-0001.json.hb.123").write_text(
                "{partial", encoding="utf-8")
            markers = tui.read_collab_markers(directory)
        self.assertEqual(len(markers), 2)
        versions = sorted(m["marker_version"] for m in markers)
        self.assertEqual(versions, [1, 2])
        self.assertEqual(
            tui.read_collab_markers(Path(raw) / "gone"), [])  # deleted dir

    # -- cell and connector rendering --------------------------------------

    def _member(self, surface_uuid, workspace_uuid=None, ref="surface:1"):
        import cmux_supervisor_tui as tui

        return tui.ViewRow(
            kind="member", workspace_id=workspace_uuid or self.WS_UUID,
            workspace_ref="workspace:7", workspace_title="t", counts={},
            candidate=Candidate({"surface_id": surface_uuid, "ref": ref},
                                "explicit", "idle", "-", 0, False),
        )

    def _group(self, workspace_uuid=None):
        import cmux_supervisor_tui as tui

        return tui.ViewRow(
            kind="group", workspace_id=workspace_uuid or self.WS_UUID,
            workspace_ref="workspace:7", workspace_title="t", counts={})

    def test_two_participants_get_the_top_and_bottom_connectors(self):
        import cmux_supervisor_tui as tui

        roles = tui.collab_roles([self._marker()], now=1_000_000.0)
        rows = [self._group(), self._member(self.SUP_UUID),
                self._member(self.EXE_UUID)]
        cells = tui.collab_cells_for_rows(rows, roles)
        self.assertEqual(cells[rows[1].key], "╭Supervisor")
        self.assertEqual(cells[rows[2].key], "╰Executor")

    def test_a_bystander_between_participants_stays_blank(self):
        """A glyph on a non-member row reads as participation, and with
        concurrent collaborations a shared pass-through line could not say
        whose it is -- so strangers get nothing, not a ``│``."""

        import cmux_supervisor_tui as tui

        roles = tui.collab_roles([self._marker(executors=2)], now=1_000_000.0)
        stranger = self._member("cccccccc-0000-4000-8000-00000000dead")
        rows = [self._group(), self._member(self.SUP_UUID), stranger,
                self._member(self.EXE_UUID), self._member(self.EXE2_UUID)]
        cells = tui.collab_cells_for_rows(rows, roles)
        self.assertEqual(cells[rows[1].key], "╭Supervisor")
        self.assertNotIn(stranger.key, cells)
        self.assertEqual(cells[rows[3].key], "├Executor1")
        self.assertEqual(cells[rows[4].key], "╰Executor2")

    def test_two_collaborations_in_one_workspace_never_share_a_line(self):
        """The v1 defect made this impossible (one marker per workspace);
        under v2 each collaboration gets its own connector run, and the
        second collaboration's rows must not extend the first's line."""

        import cmux_supervisor_tui as tui

        ua = "dddddddd-0000-4000-8000-000000000001"
        ub = "dddddddd-0000-4000-8000-000000000002"
        marker_b = self._marker_v2(collab_id="collab-b", task_id="fin-b")
        marker_b["participants"][0]["surface_uuid"] = ua
        marker_b["participants"][1]["surface_uuid"] = ub
        roles = tui.collab_roles(
            [self._marker_v2(collab_id="collab-a", task_id="fin-a"), marker_b],
            now=1_000_000.0)
        rows = [self._group(),
                self._member(self.SUP_UUID), self._member(ua),
                self._member(self.EXE_UUID), self._member(ub)]
        cells = tui.collab_cells_for_rows(rows, roles)
        self.assertEqual(cells[rows[1].key], "╭Supervisor")
        self.assertEqual(cells[rows[2].key], "╭Supervisor")
        self.assertEqual(cells[rows[3].key], "╰Executor")
        self.assertEqual(cells[rows[4].key], "╰Executor")
        counts = tui.collab_group_counts(roles)
        self.assertEqual(counts, {self.WS_UUID: 2})

    def test_a_lone_visible_participant_keeps_its_label_but_not_the_line(self):
        """Half a connector implies a peer on screen that is not there."""

        import cmux_supervisor_tui as tui

        roles = tui.collab_roles([self._marker()], now=1_000_000.0)
        rows = [self._group(), self._member(self.SUP_UUID)]
        cells = tui.collab_cells_for_rows(rows, roles)
        self.assertEqual(cells[rows[1].key], " Supervisor")
        self.assertNotIn("╭", "".join(cells.values()))

    def test_the_connector_never_crosses_a_workspace_header(self):
        """Rows of different workspaces must not look linked, even when the
        same marker somehow names surfaces now shown under another group."""

        import cmux_supervisor_tui as tui

        other_ws = "beefbeef-0000-4000-8000-00000000000f"
        roles = tui.collab_roles([self._marker()], now=1_000_000.0)
        rows = [
            self._group(), self._member(self.SUP_UUID),
            self._group(other_ws), self._member(self.EXE_UUID, other_ws),
        ]
        cells = tui.collab_cells_for_rows(rows, roles)
        # The supervisor is alone in its group: label, no connector.
        self.assertEqual(cells[rows[1].key], " Supervisor")
        # The executor row sits under a workspace the marker does not name,
        # so the stale evidence renders nothing at all.
        self.assertNotIn(rows[3].key, cells)

    # -- layout: width gating and the row/header contract -------------------

    def test_the_column_needs_106_columns_and_the_gate_is_exact(self):
        import cmux_supervisor_tui as tui

        body = sum(width for _, width, _ in ROW_COLUMNS) + len(ROW_COLUMNS) - 1
        minimum = 5 + body + 1 + tui.COLLAB_COL_WIDTH + 2 + tui.TITLE_COL_WIDTH
        self.assertEqual(minimum, 106)          # documents the current budget
        # The gate and this arithmetic must be the SAME measurement, not two
        # numbers that happen to agree today.
        self.assertEqual(minimum, tui._head_cells(tui.ROW_COLUMNS, collab=True))
        self.assertFalse(tui.collab_column_fits(minimum - 1))
        self.assertTrue(tui.collab_column_fits(minimum))
        self.assertFalse(tui.collab_column_fits(None))
        # And it is monotone: once the column fits it never un-fits.
        fitting = [w for w in range(30, 401) if tui.collab_column_fits(w)]
        self.assertEqual(fitting, list(range(minimum, 401)))

    def test_the_core_columns_never_shrink_below_their_own_content(self):
        """Removing the literal 80/100/106 thresholds also removed a real bug:
        the old ladder shrank 程序 to 6 cells, which truncated `Copilot`."""

        import cmux_supervisor_tui as tui

        widths = {name: width for name, width, _ in tui.ROW_COLUMNS}
        self.assertGreaterEqual(widths["程序"], tui.display_width("Copilot"))
        for clip in list(range(30, 201)) + [400]:
            plan = tui.row_layout(clip, collab_available=True)
            for name, width, _ in plan.columns:
                self.assertEqual(width, widths[name], f"clip {clip} / {name}")
            # Core columns are never dropped, at any width.
            kept = {name for name, _, _ in plan.columns}
            for name in tui.CORE_COLUMN_NAMES:
                self.assertIn(name, kept, f"clip {clip}")
        self.assertEqual(tui.MIN_CORE_WIDTH,
                         tui._head_cells(tuple(c for c in tui.ROW_COLUMNS
                                               if c[0] in tui.CORE_COLUMN_NAMES),
                                         collab=False))

    def test_rows_without_a_collab_argument_keep_the_historical_layout(self):
        """`collab=None` must be byte-identical to the nine-column row, so
        every caller and test that predates the column is untouched."""

        cells = ("监控中", "ws11/p24/s59", "Claude", "正常", "45%", "空闲", "-", "3")
        for width in (None, 105, 131, 200):
            self.assertEqual(
                _row_text("  *  ", cells, "标题", width, SID_A),
                _row_text("  *  ", cells, "标题", width, SID_A, None))

    def test_the_collab_cell_is_padded_into_a_fixed_eleven_columns(self):
        import cmux_supervisor_tui as tui

        cells = ("监控中", "ws11/p24/s59", "Claude", "正常", "45%", "空闲", "-", "3")
        plain = _row_text("  *  ", cells, "标题", 200, SID_A, None)
        with_cell = _row_text("  *  ", cells, "标题", 200, SID_A, "╭Supervisor")
        self.assertEqual(
            display_width_of(with_cell) - display_width_of(plain),
            tui.COLLAB_COL_WIDTH + 1)
        empty = _row_text("  *  ", cells, "标题", 200, SID_A, "")
        self.assertEqual(display_width_of(empty), display_width_of(with_cell))

    def test_header_and_session_stay_consistent_when_the_column_is_on(self):
        """Header 说 session 时行里必有完整 UUID，反之整列缺席。

        Rewritten to the measured fit: no clip is exempt, the whole 30..200 band
        is swept with the extra collaboration column in the head, and the
        fits-or-OMITS contract replaces the retired fits-or-marker one.
        """

        import cmux_supervisor_tui as tui

        cells = ("监控中", "ws11/p24/s59", "Claude", "正常", "45%", "空闲", "-", "3")
        named = (51, 54, 60, 79, 80, 81, 99, 100, 101, 106, 107, 114, 132, 160)
        for clip in sorted(set(named) | set(range(30, 201)) | {142, 143, 144, 200}):
            header = tui.header_text(clip, "协作")[:clip]
            row = _row_text("  *  ", cells, "标题", clip, SID_A, "╭Supervisor")[:clip]
            self.assertEqual("session" in header, SID_A in row, f"clip {clip}")
            self.assertNotIn(tui.SESSION_NARROW_MARKER, row, f"clip {clip}")
            if SID_A not in row:
                for length in range(8, len(SID_A)):
                    self.assertNotIn(SID_A[:length], row,
                                     f"clip {clip} leaked {length} chars")
        # The measured boundary WITH the collaboration column, derived not typed.
        first = min(c for c in range(2, 400) if tui.row_layout(c, collab_available=True).session)
        self.assertIn(SID_A, _row_text("  *  ", cells, "标题", first, SID_A, "╭Supervisor"))
        self.assertNotIn(SID_A,
                         _row_text("  *  ", cells, "标题", first - 1, SID_A, "╭Supervisor"))

    # -- the draw path and the client --------------------------------------

    def _drawn(self, marker_payloads, width=160):
        import cmux_supervisor_tui as tui

        class FakeScreen:
            def __init__(self):
                self.writes = []

            def erase(self):
                pass

            def getmaxyx(self):
                return 30, width

            def addnstr(self, y, x, text, n, attr=0):
                self.writes.append(text)

            def refresh(self):
                pass

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for i, payload in enumerate(marker_payloads):
                target = directory / f"m{i}.json"
                if payload.get("marker_version") == 2:
                    # v2 layout: one file per collaboration inside the
                    # workspace directory, exactly as arm_task writes it.
                    ws_dir = directory / str(payload.get("workspace_uuid"))
                    ws_dir.mkdir(exist_ok=True)
                    target = ws_dir / f"{payload.get('collaboration_id')}.json"
                target.write_text(
                    json.dumps(payload), encoding="utf-8")
            model = SupervisorModel.__new__(SupervisorModel)
            model.config = {"mode": "armed", "global_paused": False}
            model.candidates = []
            model.online = True
            model.error = ""
            model.janitor = tui.JanitorClient(Path("/nonexistent-ctl"))
            model.stack = tui.StackClient(Path("/nonexistent-ctl"))
            model.collab = tui.CollabClient(directory)
            model.collab.maybe_refresh(force=True)
            rows = [self._group(), self._member(self.SUP_UUID),
                    self._member(self.EXE_UUID)]
            screen = FakeScreen()
            tui._draw(screen, model, rows, 1, "all", "", "")
        return "\n".join(screen.writes)

    def test_the_draw_path_shows_roles_only_while_a_marker_is_fresh(self):
        drawn = self._drawn([self._marker(now=time.time())])
        self.assertIn("协作", drawn)
        self.assertIn("╭Supervisor", drawn)
        self.assertIn("╰Executor", drawn)
        # The focus line (cursor on the supervisor row) names the task and peer.
        self.assertIn("fin-demo", drawn)

        for gone in ([],                                        # disarmed
                     [self._marker(now=time.time() - 40_000)]):  # expired
            drawn = self._drawn(gone)
            self.assertNotIn("协作", drawn)
            self.assertNotIn("Supervisor", drawn)

    def test_a_narrow_pane_drops_the_column_for_rows_and_header_alike(self):
        drawn = self._drawn([self._marker(now=time.time())], width=100)
        self.assertNotIn("协作", drawn)
        self.assertNotIn("╭", drawn)

    def test_the_draw_path_reads_v2_directory_markers_and_counts_the_group(self):
        drawn = self._drawn([self._marker_v2(now=time.time())])
        self.assertIn("╭Supervisor", drawn)
        self.assertIn("╰Executor", drawn)
        self.assertIn("协作×1", drawn)
        # The focus line names the v2 task through the same path as v1.
        self.assertIn("fin-demo-v2", drawn)

    def test_the_client_caches_between_ttl_windows_and_forces_on_demand(self):
        import cmux_supervisor_tui as tui

        clock = [1000.0]
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "m.json").write_text(
                json.dumps(self._marker(now=clock[0])), encoding="utf-8")
            client = tui.CollabClient(directory, clock=lambda: clock[0])
            client.maybe_refresh()
            self.assertIn(self.SUP_UUID, client.snapshot())

            (directory / "m.json").unlink()
            clock[0] += 1.0                     # inside the 2s TTL: cached
            client.maybe_refresh()
            self.assertIn(self.SUP_UUID, client.snapshot())
            client.maybe_refresh(force=True)    # force bypasses the TTL
            self.assertEqual(client.snapshot(), {})

    def test_the_focus_note_reports_role_task_and_peer(self):
        import cmux_supervisor_tui as tui

        roles = tui.collab_roles([self._marker()], now=1_000_000.0)
        note = tui.collab_focus_note(
            self._member(self.SUP_UUID).candidate, roles)
        self.assertIn("协作 Supervisor", note)
        self.assertIn("task=fin-demo", note)
        self.assertIn("对端", note)
        self.assertEqual(
            tui.collab_focus_note(
                self._member("ffffffff-0000-4000-8000-000000000000").candidate,
                roles),
            "")


class BoundedScreen:
    """A fake window that enforces the SAME bounds real curses enforces.

    The older fakes in this file accept ``addnstr(y, x, ...)`` and throw the
    coordinates away, so an off-screen write looked identical to a correct one --
    and `_safe_addnstr` swallows `curses.error`, which means the product could
    write outside the window at every width and no test would notice.  This one
    raises where curses raises and records every violation.
    """

    def __init__(self, height: int, width: int):
        self.height, self.width = height, width
        self.writes: list[tuple[int, int, str]] = []
        self.violations: list[tuple] = []
        self.key_reads = 0
        self.keys: list = []
        self.resize_to: tuple[int, int] | None = None

    def getmaxyx(self):
        return (self.height, self.width)

    def _check(self, kind, y, x, text):
        import cmux_supervisor_tui as tui

        if not (0 <= y < self.height) or not (0 <= x < self.width):
            self.violations.append((kind, "off-window", y, x, text[:24]))
            raise curses.error(f"{kind} out of bounds")
        if x + tui.display_width(text) > self.width:
            self.violations.append((kind, "overflow", y, x, text[:24]))

    def addnstr(self, y, x, text, n, attr=0):
        text = text[:n]
        self._check("addnstr", y, x, text)
        self.writes.append((y, x, text))

    def addstr(self, y, x, text, attr=0):
        self._check("addstr", y, x, text)
        self.writes.append((y, x, text))

    def move(self, y, x):
        self._check("move", y, x, "")

    def erase(self):
        pass

    def refresh(self):
        pass

    def clrtoeol(self):
        pass

    def timeout(self, ms):
        pass

    def _next_key(self):
        self.key_reads += 1
        if not self.keys:
            raise AssertionError("read a key with nothing left to read: "
                                 "the product asked a question it could not display")
        key = self.keys.pop(0)
        if key == curses.KEY_RESIZE and self.resize_to:
            self.height, self.width = self.resize_to
        return key

    def getch(self):
        return self._next_key()

    def get_wch(self):
        return self._next_key()


class ShortWindowAndPromptTests(unittest.TestCase):
    """Geometry safety through the real draw chain, bounds actually enforced."""

    NAMED_WIDTHS = (51, 54, 60, 79, 80, 81, 99, 100, 101, 106, 107, 114, 132, 160)
    HEIGHTS = (1, 5, 8, 9, 10, 12, 13, 14, 15, 16, 24, 40, 119)

    def setUp(self):
        # curs_set/curses.error need a real terminal; cursor visibility is a
        # terminal side effect, not part of any invariant asserted here.
        self._curs_set = curses.curs_set
        curses.curs_set = lambda visibility: None

    def tearDown(self):
        curses.curs_set = self._curs_set

    def _model_and_rows(self, directory):
        import cmux_supervisor_tui as tui

        model = tui.SupervisorModel.__new__(tui.SupervisorModel)
        model.config = {"mode": "armed", "global_paused": False}
        model.candidates = []
        model.online = True
        model.error = ""
        model.janitor = tui.JanitorClient(Path("/nonexistent-ctl"))
        model.stack = tui.StackClient(Path("/nonexistent-ctl"))
        model.collab = tui.CollabClient(directory)
        rows = [
            tui.ViewRow(kind="group", workspace_id="ws3", workspace_ref="workspace:3",
                        workspace_title="标题很长的中文工作区名称占很多列", counts={"all": 2}),
            tui.ViewRow(kind="member", workspace_id="ws3", workspace_ref="workspace:3",
                        workspace_title="标题很长的中文工作区名称占很多列", counts={},
                        candidate=Candidate(
                            {"surface_id": "a1a1a1a1-b1b1-7b11-8b11-c1c1c1c1c1c1",
                             "ref": "surface:104"}, "explicit", "idle", "-", 0, False)),
            tui.ViewRow(kind="group", workspace_id="ws7", workspace_ref="workspace:7",
                        workspace_title="t", counts={"all": 1, "pool": 1}),
            tui.ViewRow(kind="member", workspace_id="ws7", workspace_ref="workspace:7",
                        workspace_title="t", counts={},
                        candidate=Candidate(
                            {"surface_id": "b2b2b2b2-c2c2-7c22-8c22-d2d2d2d2d2d2",
                             "ref": "surface:153"}, "pool", "idle", "-", 0, False)),
        ]
        return model, rows

    def test_every_emitted_write_stays_inside_the_window(self):
        """The named draw widths plus the whole 30..200 band, at every height.

        `_safe_addnstr` suppresses `curses.error`, so before this assertion an
        off-screen write was indistinguishable from a correct one: the panel was
        quiet on short terminals because the error was swallowed, not because the
        write was in range.  Here suppression becomes a tested invariant.
        """

        import cmux_supervisor_tui as tui

        widths = sorted(set(self.NAMED_WIDTHS) | set(range(30, 201)))
        with tempfile.TemporaryDirectory() as raw:
            model, rows = self._model_and_rows(Path(raw))
            for height in self.HEIGHTS:
                for width in widths:
                    screen = BoundedScreen(height, width)
                    try:
                        tui._draw(screen, model, rows, 1, "all", "", "状态")
                    except curses.error as exc:      # pragma: no cover - a failure
                        self.fail(f"{height}x{width}: curses.error escaped: {exc}")
                    self.assertEqual(screen.violations, [], f"{height}x{width}")

    def test_short_windows_render_a_bounded_compact_frame(self):
        """Below MIN_HEIGHT: at most `height` rows, all of them real rows.

        This is what replaces the removed ``height > TOP_ROWS + BOTTOM_ROWS``
        exemption.  Height 12 -- where that exemption used to hide four row
        collisions -- is covered here by construction.
        """

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as raw:
            model, rows = self._model_and_rows(Path(raw))
            for height in (0, 1, 2, 3, 5, 8, 9, 10, 12, 13, 14, 15):
                for width in (30, 51, 80, 106, 132, 200):
                    screen = BoundedScreen(height, width)
                    tui._draw(screen, model, rows, 1, "all", "", "状态")
                    used = [y for y, _, _ in screen.writes]
                    self.assertLessEqual(len(screen.writes), min(3, height),
                                         f"{height}x{width}")
                    self.assertEqual(len(used), len(set(used)), f"{height}x{width}")
                    for y in used:
                        self.assertLess(y, height, f"{height}x{width}")
                    if height >= 2:
                        joined = " ".join(t for _, _, t in screen.writes)
                        self.assertIn(str(tui.MIN_HEIGHT), joined, f"{height}x{width}")
                    if height >= 3:
                        third = screen.writes[2][2]
                        # The recovery hint is clipped by narrow widths like any
                        # other line, so assert the part that fits at width 30
                        # and require the whole hint only where it can fit.
                        self.assertTrue(third.startswith("拉高终端窗口"),
                                        f"{height}x{width}: {third!r}")
                        if width >= 32:
                            self.assertIn("q 退出", third, f"{height}x{width}")
                    # Drawing a compact frame must never consume a keypress: the
                    # main loop keeps handling q and KEY_RESIZE, which is what
                    # makes this state recoverable rather than a dead end.
                    self.assertEqual(screen.key_reads, 0, f"{height}x{width}")

    def test_min_height_is_derived_from_the_two_blocks(self):
        import cmux_supervisor_tui as tui

        self.assertEqual(tui.MIN_HEIGHT, tui.TOP_ROWS + 1 + tui.BOTTOM_ROWS)
        # 16 today, but as a consequence, not as a literal.
        self.assertEqual(tui.MIN_HEIGHT, 16)

    def test_the_table_frame_starts_exactly_at_min_height(self):
        """MIN_HEIGHT must be the real switchover, not an approximation."""

        import cmux_supervisor_tui as tui

        with tempfile.TemporaryDirectory() as raw:
            model, rows = self._model_and_rows(Path(raw))
            short = BoundedScreen(tui.MIN_HEIGHT - 1, 160)
            tall = BoundedScreen(tui.MIN_HEIGHT, 160)
            tui._draw(short, model, rows, 1, "all", "", "")
            tui._draw(tall, model, rows, 1, "all", "", "")
            self.assertLessEqual(len(short.writes), 3)
            self.assertGreater(len(tall.writes), 3)
            self.assertIn("q 退出", " ".join(t for _, _, t in short.writes))

    # -- prompts: refuse rather than ask a question nobody can read ----------

    def test_confirm_refuses_instead_of_taking_a_blind_yes(self):
        """A y/N whose question is not on screen is a blind destructive answer."""

        import cmux_supervisor_tui as tui

        screen = BoundedScreen(tui.MIN_HEIGHT - 1, 120)
        screen.keys = [ord("y")]          # would be consumed only if it asked
        self.assertFalse(tui._confirm(screen, "撤销整池授权？"))
        self.assertEqual(screen.key_reads, 0)
        self.assertEqual(screen.violations, [])

    def test_confirm_refuses_when_the_question_does_not_fit_the_width(self):
        import cmux_supervisor_tui as tui

        prompt = "确认要撤销这个工作区的整池授权并停止所有续跑吗"
        need = tui.display_width(f"{prompt} [y/N]")
        narrow = BoundedScreen(40, need)          # clip == need - 1
        narrow.keys = [ord("y")]
        self.assertFalse(tui._confirm(narrow, prompt))
        self.assertEqual(narrow.key_reads, 0)
        # One more column and the same question is answerable.
        wide = BoundedScreen(40, need + 1)
        wide.keys = [ord("y")]
        self.assertTrue(tui._confirm(wide, prompt))
        self.assertEqual(wide.key_reads, 1)

    def test_confirm_remeasures_after_a_resize_instead_of_reusing_old_rows(self):
        import cmux_supervisor_tui as tui

        screen = BoundedScreen(40, 120)
        screen.resize_to = (9, 120)
        screen.keys = [curses.KEY_RESIZE, ord("y")]
        # After shrinking, the question no longer fits, so the pending 'y' must
        # NOT be accepted: the old code kept the pre-resize row and asked on.
        self.assertFalse(tui._confirm(screen, "确认？"))
        self.assertEqual(screen.key_reads, 1)
        self.assertEqual(screen.violations, [])

    def test_confirm_still_answers_yes_and_no_on_a_normal_window(self):
        import cmux_supervisor_tui as tui

        for key, expected in ((ord("y"), True), (ord("Y"), True),
                              (ord("n"), False), (ord("x"), False)):
            screen = BoundedScreen(40, 120)
            screen.keys = [key]
            self.assertIs(tui._confirm(screen, "确认？"), expected, key)

    def test_text_prompt_refuses_rather_than_offering_an_invisible_field(self):
        import cmux_supervisor_tui as tui

        short = BoundedScreen(tui.MIN_HEIGHT - 1, 120)
        short.keys = ["a", "\n"]
        self.assertIsNone(tui._text_prompt(short, "搜索："))
        self.assertEqual(short.key_reads, 0)

        narrow = BoundedScreen(40, tui.display_width("搜索：") + 1)
        narrow.keys = ["a", "\n"]
        self.assertIsNone(tui._text_prompt(narrow, "搜索："))
        self.assertEqual(narrow.key_reads, 0)

    def test_text_prompt_cancels_on_a_resize_that_removes_the_row(self):
        import cmux_supervisor_tui as tui

        screen = BoundedScreen(40, 120)
        screen.resize_to = (8, 120)
        screen.keys = ["a", curses.KEY_RESIZE, "\n"]
        self.assertIsNone(tui._text_prompt(screen, "搜索："))
        self.assertEqual(screen.violations, [])

    def test_text_prompt_keeps_wide_characters_and_the_cursor_in_range(self):
        import cmux_supervisor_tui as tui

        screen = BoundedScreen(40, 120)
        screen.keys = ["中", "文", "\n"]
        self.assertEqual(tui._text_prompt(screen, "搜索："), "中文")
        self.assertEqual(screen.violations, [])
        screen = BoundedScreen(40, 120)
        screen.keys = ["\x1b"]
        self.assertIsNone(tui._text_prompt(screen, "搜索："))


class StackWarningsVersusLivenessTests(unittest.TestCase):
    """Operational liveness and warning-free coverage are two questions.

    cmux-stack publishes ``warnings``/``warning_count`` next to ``healthy`` and
    deliberately does not let them redefine health (bin/cmux-stack:488).  The
    panel used to drop the whole field on the floor, because ``_stack_component``
    is a whitelist and nobody added it -- so coverage drift was invisible here.
    The opposite mistake is just as bad: folding warnings into the alarm colour
    makes a running watcher look down because its release notes went stale.
    """

    def _document(self, warn_codes, *, overall="ok", healthy=True, count=None):
        components = {}
        for name in ("watcher", "janitor", "profiles"):
            components[name] = {
                "component": name, "installed": True, "probe_ok": True,
                "healthy": healthy, "launchd": {"loaded": True},
                "warnings": list(warn_codes.get(name, ())),
            }
        components["watcher"].update(pid=7279, pid_alive=True, mode="real")
        components["janitor"].update(guard_health="ok", paused=False)
        components["profiles"].update(profile_count=4, unhealthy_count=0)
        flat = [{"component": name, "code": code}
                for name, codes in warn_codes.items() for code in codes]
        return {"overall": overall, "probed_count": 3, "requested_count": 3,
                "unhealthy": [], "unknown": [], "components": components,
                "warnings": flat,
                "warning_count": len(flat) if count is None else count}

    def test_warnings_reach_the_panel_at_all(self):
        import cmux_supervisor_tui as tui

        snapshot = tui._stack_snapshot_from_status(self._document(
            {"watcher": ["source_drift"],
             "janitor": ["janitor_observation_stale"]}))
        self.assertEqual([item["code"] for item in snapshot["warnings"]],
                         ["source_drift", "janitor_observation_stale"])
        self.assertEqual(snapshot["components"]["watcher"]["warnings"],
                         ["source_drift"])
        self.assertEqual(snapshot["warning_count"], 2)
    def test_a_warning_never_changes_the_liveness_verdict(self):
        import cmux_supervisor_tui as tui

        snapshot = tui._stack_snapshot_from_status(self._document(
            {"watcher": ["source_drift"],
             "janitor": ["guard_observation_stale"]}))
        self.assertFalse(tui.stack_is_alarming(snapshot))
        self.assertTrue(tui.stack_has_warnings(snapshot))
        line = tui.stack_line(snapshot)
        self.assertTrue(line.startswith("✓"), line)
        self.assertIn("全部正常", line)
        self.assertIn("注意 2 条", line)

    def test_a_real_fault_is_alarming_with_or_without_warnings(self):
        import cmux_supervisor_tui as tui

        for codes in ({}, {"watcher": ["source_drift"]}):
            snapshot = tui._stack_snapshot_from_status(
                self._document(codes, overall="degraded", healthy=False))
            self.assertTrue(tui.stack_is_alarming(snapshot), codes)
            self.assertIn("异常", tui.stack_line(snapshot))

    def test_a_clean_stack_says_nothing_about_warnings(self):
        import cmux_supervisor_tui as tui

        snapshot = tui._stack_snapshot_from_status(self._document({}))
        self.assertFalse(tui.stack_has_warnings(snapshot))
        self.assertFalse(tui.stack_is_alarming(snapshot))
        self.assertNotIn("注意", tui.stack_line(snapshot))

    def test_unreadable_means_unmeasured_not_zero_warnings(self):
        """A count of 0 claims a measurement.  None says there was none."""

        import cmux_supervisor_tui as tui

        absent = tui._stack_absent("控制器超时")
        self.assertIsNone(absent["warning_count"])
        self.assertFalse(tui.stack_has_warnings(absent))
        self.assertTrue(tui.stack_is_alarming(absent))
        self.assertNotIn("注意", tui.stack_line(absent))

    def test_a_truncated_list_still_reports_the_real_total(self):
        import cmux_supervisor_tui as tui

        snapshot = tui._stack_snapshot_from_status(
            self._document({"watcher": ["source_drift"] * 20}))
        self.assertEqual(len(snapshot["warnings"]), 16)
        self.assertEqual(snapshot["warning_count"], 20)
        self.assertIn("注意 20 条", tui.stack_line(snapshot))
    def test_malformed_warnings_never_reach_the_screen(self):
        import cmux_supervisor_tui as tui

        document = self._document({})
        document["warnings"] = ["plain-string", {"component": 1, "code": "x"},
                                {"component": "watcher"}, None]
        document["warning_count"] = "many"
        snapshot = tui._stack_snapshot_from_status(document)
        self.assertEqual(snapshot["warnings"], [])
        self.assertEqual(snapshot["warning_count"], 0)
        self.assertNotIn("注意", tui.stack_line(snapshot))

    def test_a_component_warning_of_the_wrong_shape_is_dropped(self):
        import cmux_supervisor_tui as tui

        document = self._document({})
        document["components"]["watcher"]["warnings"] = ["source_drift", 7, None]
        snapshot = tui._stack_snapshot_from_status(document)
        self.assertEqual(snapshot["components"]["watcher"]["warnings"],
                         ["source_drift"])

    def test_the_detail_page_lists_each_warning_and_disclaims_the_verdict(self):
        import cmux_supervisor_tui as tui

        snapshot = tui._stack_snapshot_from_status(self._document(
            {"watcher": ["source_drift"],
             "janitor": ["janitor_observation_stale"]}))
        page = "\n".join(tui.stack_page_lines(snapshot))
        self.assertIn("source_drift", page)      # the raw code stays greppable
        self.assertIn("janitor_observation_stale", page)
        self.assertIn(tui.STACK_WARNING_TEXT["source_drift"], page)
        self.assertIn("不改变上面的总体判定", page)

    def test_an_unknown_warning_code_passes_through_as_itself(self):
        import cmux_supervisor_tui as tui

        snapshot = tui._stack_snapshot_from_status(
            self._document({"profiles": ["a_code_added_next_year"]}))
        page = "\n".join(tui.stack_page_lines(snapshot))
        self.assertIn("a_code_added_next_year", page)
        self.assertEqual(tui._stack_warning_text("a_code_added_next_year"),
                         "a_code_added_next_year")
    def test_every_label_still_matches_a_code_the_controller_emits(self):
        """Guards my map against rotting, not the controller against growing.

        A code cmux-stack adds later falls through as itself, which is safe and
        tested above.  A label here whose code no longer exists is dead text,
        and that is the drift worth failing on.
        """

        import cmux_supervisor_tui as tui

        controller = Path(__file__).resolve().parents[1] / "bin" / "cmux-stack"
        if not controller.is_file():                      # pragma: no cover
            self.skipTest("cmux-stack is not present next to the panel")
        source = controller.read_text(encoding="utf-8")
        for code in tui.STACK_WARNING_TEXT:
            self.assertIn(f'"{code}"', source,
                          f"{code} has a label here but the controller "
                          f"no longer emits it")

    def test_the_row_has_three_states_and_the_draw_path_uses_all_three(self):
        import cmux_supervisor_tui as tui

        clean = tui._stack_snapshot_from_status(self._document({}))
        warned = tui._stack_snapshot_from_status(
            self._document({"watcher": ["source_drift"]}))
        broken = tui._stack_snapshot_from_status(
            self._document({}, overall="degraded", healthy=False))
        self.assertEqual(
            [(tui.stack_is_alarming(s), tui.stack_has_warnings(s))
             for s in (clean, warned, broken)],
            [(False, False), (False, True), (True, False)])
        source = inspect.getsource(tui._draw)
        self.assertIn("stack_has_warnings", source)
        self.assertIn("stack_is_alarming", source)

    def test_a_warning_does_not_rewrite_the_icon_or_the_label(self):
        """The warning segment is additive; every other segment is untouched.

        It lands before the key hint rather than at the end, so compare the
        segment lists instead of the prefix.
        """

        import cmux_supervisor_tui as tui

        clean = tui.stack_line(
            tui._stack_snapshot_from_status(self._document({}))).split(" | ")
        warned = tui.stack_line(tui._stack_snapshot_from_status(
            self._document({"watcher": ["source_drift"]}))).split(" | ")
        self.assertEqual([s for s in warned if s not in ("注意 1 条",)], clean)
        self.assertEqual(len(warned), len(clean) + 1)
        self.assertEqual(warned[0], clean[0])           # icon and verdict
        self.assertEqual(warned[-1], clean[-1])         # key hint stays last
