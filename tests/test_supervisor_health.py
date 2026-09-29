"""Live topology, deliberate waiting and complete clipboard identity."""
import copy
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ccc_observation as health
import cmux_codex_watch as core
import cmux_supervisor_tui as tui
from tests.test_supervisor_responsiveness import QuietSource


SESSION_A = "01a0ab0e-c369-70e1-a43a-876767c94e64"
SESSION_B = "01a0d8ef-f046-7a12-863c-d8c9448fdb3c"


def row(sid="live", session=SESSION_A):
    return tui.Candidate(
        {"surface_id": sid, "workspace_id": "live-workspace", "workspace_ref": "workspace:28",
         "workspace_title": "Live thesis", "ref": "surface:28", "type": "terminal"},
        "explicit", "idle", "", 0, False, agent_kind="codex",
        session=tui.SessionResult(status="ok", session_id=session, agent_kind="codex"),
    )


class CurrentTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.tree = {"windows": [{"id": "win", "workspaces": [{
            "id": "live-workspace", "ref": "workspace:28", "title": "Live thesis",
            "panes": [{"id": "pane", "ref": "pane:8", "surfaces": [
                {"id": "live", "ref": "surface:28", "type": "terminal", "title": "Codex"}]}],
        }]}]}
        config = core.default_config()
        config["targets"] = [
            {"surface_id": "gone19", "workspace_id": "old19", "workspace_ref": "workspace:19", "paused": True},
            {"surface_id": "gone28", "workspace_id": "old28", "workspace_ref": "workspace:28", "paused": True},
            {"surface_id": "gone-in-live", "workspace_id": "live-workspace", "workspace_ref": "workspace:19", "paused": True},
            {"surface_id": "live", "workspace_id": "live-workspace", "workspace_ref": "workspace:22", "paused": False},
        ]
        self.path.write_text(json.dumps(config))
        self.client = SimpleNamespace(tree=lambda: copy.deepcopy(self.tree), top_all=lambda: {"windows": []})
        quiet = QuietSource()
        self.model = tui.SupervisorModel(self.path, client=self.client, janitor=quiet,
                                         sessions=quiet, stack=quiet, collab=quiet)
        self.addCleanup(self.model.close)
        patcher = mock.patch.object(core.ClaudeHookSettingsManager, "inspect", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reused_ref_keeps_live_uuid_and_hides_closed_rows_without_mutation(self):
        before = self.path.read_bytes()
        self.model.refresh(force=True)
        self.assertEqual([c.surface_id for c in self.model.candidates], ["live"])
        rows = tui.build_view_rows(self.model.candidates, set(), tui.DEFAULT_FILTER)
        self.assertEqual((rows[0].workspace_id, rows[0].workspace_ref), ("live-workspace", "workspace:28"))
        self.assertEqual(rows[0].workspace_title, "Live thesis")
        self.assertEqual(self.path.read_bytes(), before)

    def test_ref_renumber_and_closed_tab_reconcile_on_next_successful_refresh(self):
        self.model.refresh(force=True)
        self.tree["windows"][0]["workspaces"][0]["ref"] = "workspace:41"
        self.model.refresh(force=True)
        self.assertEqual(self.model.candidates[0].workspace_ref, "workspace:41")
        self.tree["windows"][0]["workspaces"][0]["panes"][0]["surfaces"] = []
        self.model.refresh(force=True)
        self.assertEqual(self.model.candidates, [])

    def test_unavailable_inventory_keeps_last_view_and_never_prunes_authorization(self):
        self.model.refresh(force=True)
        before = self.path.read_bytes()
        self.client.tree = mock.Mock(side_effect=core.CmuxError("socket unavailable"))
        self.model.refresh(force=True)
        self.assertFalse(self.model.online)
        self.assertEqual([c.surface_id for c in self.model.candidates], ["live"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_group_identity_does_not_take_a_reused_historical_ref(self):
        historical = row("gone")
        historical.record.pop("workspace_title")
        historical.record["workspace_ref"] = "workspace:19"
        self.assertEqual(tui.group_identity([historical, row()]), ("workspace:28", "Live thesis"))


class CurrentHealthTests(unittest.TestCase):
    def setUp(self):
        self.target = {"surface_id": "s", "workspace_id": "w", "enabled": True}

    def observation(self, *, record=None, process=None, owner=False, complete=True):
        return health.observation_row(self.target, record, process or {}, {}, {}, owner_alive=owner,
                                       now=100, stale_after=30, inventory_complete=complete)

    def test_closed_automatic_pause_is_history_but_a_live_lost_owner_remains_a_fault(self):
        self.target.update(paused=True, pause_origin="automatic_observation_error")
        observed = self.observation()
        self.assertEqual(observed["status"], "missing")
        report = health.continuation_report([self.target], {}, now=101, observations=[observed])
        self.assertEqual((report["status"], report["counts"]["missing"]), ("ok", 1))
        self.assertEqual(self.observation(owner=True)["status"], "live_unreadable")
        self.assertEqual(self.observation(owner=None)["status"], "unknown")
        self.assertEqual(self.observation(complete=False)["status"], "unknown")

    def test_non_agent_shell_has_no_claude_hook_obligation(self):
        shell = dict(agent_kind="shell", agent_pid=0, process_snapshot_present=True)
        observed = self.observation(record=self.target, process=shell)
        self.assertEqual(observed["status"], "dormant")
        report = health.continuation_report([self.target], {"s": {"state": "claude_hook_missing"}},
                                             now=101, observations=[observed])
        self.assertEqual((report["status"], report["counts"]["inactive"]), ("ok", 1))
        self.assertNotEqual(self.observation(record=self.target, process=shell, owner=None)["status"], "dormant")
        self.assertNotEqual(self.observation(record=self.target, process=shell, complete=False)["status"], "dormant")
        shell["identity_conflicts"] = 1
        self.assertNotEqual(self.observation(record=self.target, process=shell)["status"], "dormant")

    def test_stale_or_wrong_workspace_history_cannot_hide_an_active_read_failure(self):
        observed = self.observation()
        runtime = {"viewport_checked_at": 100, "state": "cmux_unavailable"}
        for changed in ({"workspace_id": "foreign"}, {"observed_at": 1}, {"surface_id": "foreign"}):
            with self.subTest(changed=changed):
                result = health.continuation_row(self.target, runtime, now=101, observation={**observed, **changed})
                self.assertEqual(result["status"], "unavailable")

    def test_native_monitor_cadence_does_not_fake_lateness_or_hide_a_stall(self):
        runtime = {"viewport_checked_at": 100, "state": "working", "observation_cadence_sec": 10}
        self.assertEqual(health.continuation_row(self.target, runtime, now=109)["status"], "ok")
        self.assertEqual(health.continuation_row(self.target, runtime, now=121)["status"], "delayed")
        for value in (False, float("nan"), -1, "10"):
            runtime["observation_cadence_sec"] = value
            self.assertEqual(health.continuation_row(self.target, runtime, now=103)["status"], "delayed")

    def test_waiting_is_explicit_and_does_not_claim_model_or_task_success(self):
        for phase in ("error_superseded", "queued_followup"):
            runtime = {"viewport_checked_at": 100, "state": phase}
            report = health.continuation_report([self.target], {"s": runtime}, now=101)
            self.assertEqual((report["status"], report["counts"].get("waiting", 0), report["counts"]["ok"]), ("ok", 1, 0))
            runtime["delivery_status"] = "unknown"
            self.assertEqual(health.continuation_row(self.target, runtime, now=101)["status"], "delivery_unknown")

    def test_status_rechecks_freshness_and_authorization_of_retired_targets(self):
        config = {"targets": [self.target], "workspace_rules": []}
        snapshot = {"config_key": core.monitoring_config_key(config), "rows": [self.observation()]}
        with mock.patch.object(core.time, "time", return_value=101):
            self.assertEqual(core.continuation_status(config, {}, snapshot)["counts"]["missing"], 1)
            changed = copy.deepcopy(config)
            changed["targets"][0]["workspace_id"] = "new-workspace"
            self.assertEqual(core.continuation_status(changed, {}, snapshot)["status"], "unknown")
        with mock.patch.object(core.time, "time", return_value=140):
            self.assertEqual(core.continuation_status(config, {}, snapshot)["status"], "unknown")


class SessionClipboardTests(unittest.TestCase):
    def test_full_native_id_goes_to_stdin_even_if_column_is_hidden(self):
        candidate = row()
        self.assertFalse(tui.row_layout(70).session)
        with mock.patch.object(tui.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(tui.copy_session_id(candidate), SESSION_A)
        self.assertEqual(run.call_args.kwargs["input"], SESSION_A)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pbcopy"])
        self.assertNotIn(candidate.surface_id, run.call_args.args[0])

    def test_invalid_conflicting_or_group_identity_never_changes_clipboard(self):
        for candidate in (None, row(session="partial"), row()):
            if candidate and candidate.session.ok:
                candidate.session.status = "conflict"
            with mock.patch.object(tui.subprocess, "run") as run, self.assertRaises(RuntimeError):
                tui.copy_session_id(candidate)
            run.assert_not_called()

    def test_clipboard_failure_is_reported_instead_of_claiming_success(self):
        with mock.patch.object(tui.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "剪贴板写入失败"):
                tui.copy_session_id(row())

    def run_keys(self, keys, *, mouse=None):
        candidates = [row("a"), row("b", SESSION_B)]
        model = SimpleNamespace(
            candidates=candidates, suggested_surface="a", maybe_refresh=lambda **_: None,
            poll_action=lambda: None, collab=QuietSource(), error="",
        )
        results = []
        def action(operation, success, **kwargs):
            results.append(operation())
            return success
        model.start_action = action
        key_iter = iter(keys)
        screen = SimpleNamespace(keypad=lambda _: None, timeout=lambda _: None,
                                 getch=lambda: next(key_iter), getmaxyx=lambda: (40, 180))
        with mock.patch.object(tui.curses, "curs_set"), mock.patch.object(tui, "init_colors"), \
             mock.patch.object(tui, "_draw"), mock.patch.object(tui.curses, "getmouse", return_value=mouse), \
             mock.patch.object(tui.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            tui._run(screen, model)
        return results, run

    def test_keyboard_copy_preserves_clear_search_shortcut(self):
        results, run = self.run_keys([ord("c"), ord("y"), ord("q")])
        self.assertEqual(results, [SESSION_A])
        self.assertEqual(run.call_count, 1)
        self.assertIn("y 复制ID", tui.GLOBAL_KEYS_1)

    def test_click_copies_the_clicked_rows_id_not_the_previous_selection(self):
        x = tui.row_layout(179).head_cells + 1
        y = tui.layout(40)["first_row"] + 2
        results, run = self.run_keys([tui.curses.KEY_MOUSE, ord("q")],
                                     mouse=(0, x, y, 0, tui.curses.BUTTON1_RELEASED))
        self.assertEqual(results, [SESSION_B])
        self.assertEqual(run.call_args.kwargs["input"], SESSION_B)


class JanitorHealthDisplayTests(unittest.TestCase):
    def test_original_trip_and_transient_measurement_have_distinct_visible_causes(self):
        document = {"control": {"guard_tripped": True, "paused": True},
                    "guard": {"health": "tripped", "reason": "already tripped",
                              "violations": ["R9 USE_QUARANTINE baseline1 -> missing"]}}
        snapshot = tui._snapshot_from_status(document)
        self.assertIn("R9 USE_QUARANTINE", tui.junk_line(snapshot))
        self.assertIn("R9 USE_QUARANTINE", "\n".join(tui.storage_page_lines(snapshot)))
        document["control"] = {"guard_unavailable": True, "paused": False}
        document["guard"] = {"health": "observation_error", "reason": "measurement unavailable"}
        snapshot = tui._snapshot_from_status(document)
        self.assertTrue(tui.junk_is_alarming(snapshot))
        self.assertNotIn("守卫跳闸", tui.junk_line(snapshot))
        self.assertIn("等待守卫重新完成测量", tui.junk_line(snapshot))
        self.assertIn("measurement unavailable", "\n".join(tui.storage_page_lines(snapshot)))


if __name__ == "__main__":
    unittest.main()
