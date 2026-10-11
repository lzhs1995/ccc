"""Persistent pause policy must stay visible through every read-only projection."""
import copy
import json
import unittest
from unittest import mock

import ccc_observation as health
import cmux_codex_watch as core
import cmux_supervisor_tui as tui
from tests.test_cmux_stack import Sandbox, run_stack, watcher_status


def target(sid="paused", **fields):
    return {"surface_id": sid, "workspace_id": "w", "enabled": True,
            "paused": True, "pause_origin": "user", "paused_reason": "maintenance hold",
            "paused_at": 50, **fields}


def observe(item):
    return health.observation_row(item, item, {"agent_kind": "codex", "agent_pid": 7,
        "identity_verified_pids": [7]}, {"runtime_surface_ready": True},
        {"viewport_checked_at": 100, "viewport_readable": True},
        owner_alive=True, now=100, stale_after=30)


class PauseProjectionTests(unittest.TestCase):
    def test_original_fields_and_duration_are_read_only_and_age_at_the_reader(self):
        item = target(paused_reason="  original reason\nsecond line  ")
        before = copy.deepcopy(item)
        runtime = {"state": "working", "last_send_error": "old delivery issue"}
        observed = observe(item)
        snapshot = copy.deepcopy(observed)
        for row in (health.continuation_row(item, runtime, now=110),
                    health.summarize_observation([observed], now=110, stale_after=30)["targets"][0]):
            self.assertEqual(row["paused_reason"], item["paused_reason"])
            self.assertEqual((row["pause_origin"], row["paused_at"], row["pause_age_sec"],
                              row["pause_time_status"]), ("user", 50, 60, "valid"))
        self.assertEqual(item, before)
        self.assertEqual(observed, snapshot)

    def test_bad_or_future_pause_timestamps_never_become_zero_age(self):
        cases = ((None, "missing"), (0, "missing"), (False, "invalid"), (True, "invalid"),
                 (-1, "invalid"), ("50", "invalid"), (float("nan"), "invalid"),
                 (float("inf"), "invalid"), (101, "future"))
        for at, time_status in cases:
            with self.subTest(at=at):
                row = health.continuation_row(target(paused_at=at), {}, now=100)
                self.assertEqual(row["status"], "paused")
                self.assertIsNone(row["pause_age_sec"])
                self.assertEqual(row["pause_time_status"], time_status)
        row = health.continuation_row(target(paused=False), {}, now=100)
        self.assertIsNone(row["pause_age_sec"])
        self.assertEqual(row["pause_time_status"], "not_paused")

    def test_all_held_and_mixed_fleets_have_visible_aggregate_verdicts(self):
        paused, active, disabled = target(), target("active", paused=False), target("disabled", enabled=False)
        cases = (([paused], "paused", "all_enabled_targets_paused"),
                 ([disabled], "disabled", "all_targets_disabled"),
                 ([active, paused], "attention", "some_enabled_targets_paused"),
                 ([active, disabled], "attention", "some_targets_disabled"),
                 ([paused, disabled], "paused", "all_enabled_targets_paused"))
        for items, verdict, reason in cases:
            with self.subTest(verdict=verdict, targets=[i["surface_id"] for i in items]):
                observations = [observe(item) for item in items]
                runtime = {i["surface_id"]: {"state": "working", "viewport_checked_at": 100} for i in items}
                for report in (health.summarize_observation(observations, now=101, stale_after=30),
                               health.continuation_report(items, runtime, now=101, observations=observations)):
                    self.assertEqual(report["status"], verdict)
                    self.assertIn(reason, report["attention_reasons"])
                    self.assertEqual(report["counts"]["disabled"], int(disabled in items))
                    self.assertEqual(report["counts"]["paused"], int(paused in items))

    def test_actual_fault_and_uncertainty_keep_priority_over_explicit_holds(self):
        items = [target(), target("active", paused=False)]
        for state, observed_status, verdict in (("cmux_unavailable", "live_unreadable", "degraded"),
                                                ("unknown", "unknown", "unknown")):
            with self.subTest(state=state):
                runtime = {"active": {"state": state, "viewport_checked_at": 100}}
                observed = [observe(i) for i in items]
                observed[1]["status"] = observed_status
                for report in (health.continuation_report(items, runtime, now=101),
                               health.summarize_observation(observed, now=101, stale_after=30)):
                    self.assertEqual(report["status"], verdict)
                    self.assertEqual(report["counts"]["paused"], 1)

    def test_closed_and_idle_pause_history_does_not_count_as_a_current_hold(self):
        items = [target("gone", pause_origin="automatic_observation_error"), target("shell")]
        observed = [dict(observe(items[0]), status="missing"), dict(observe(items[1]), status="dormant")]
        for report in (health.continuation_report(items, {}, now=101, observations=observed),
                       health.summarize_observation(observed, now=101, stale_after=30)):
            self.assertEqual((report["status"], report["attention_reasons"]), ("ok", []))
        items.append(target())
        observed.append(observe(items[-1]))
        self.assertEqual(health.continuation_report(items, {}, now=101, observations=observed)["status"], "paused")

    def test_unobserved_configuration_preserves_holds_and_their_original_metadata(self):
        for enabled, expected in ((True, "paused"), (False, "disabled")):
            config = {"targets": [target(enabled=enabled)]}
            for snapshot in ({}, {"config_key": core.monitoring_config_key(config), "rows": []}):
                with self.subTest(enabled=enabled, snapshot=snapshot), mock.patch.object(core.time, "time", return_value=100):
                    report = core.observation_status(config, {}, snapshot)
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["targets"][0]["pause_age_sec"], 50)
                self.assertEqual(report["targets"][0]["paused_reason"], "maintenance hold")

    def test_current_config_metadata_overrides_snapshot_metadata_without_mutating_it(self):
        item = target()
        config = {"targets": [item]}
        snapshot = {"config_key": core.monitoring_config_key(config), "rows": [observe(item)]}
        original = copy.deepcopy(snapshot)
        item.update(paused_reason="new explicit reason", paused_at=80)
        with mock.patch.object(core.time, "time", return_value=100):
            report = core.observation_status(config, {}, snapshot)
        self.assertEqual(report["targets"][0]["paused_reason"], "new explicit reason")
        self.assertEqual(report["targets"][0]["pause_age_sec"], 20)
        self.assertEqual(snapshot, original)


class PauseDisplayTests(unittest.TestCase):
    def candidate(self, item):
        with mock.patch.object(tui.time, "time", return_value=100):
            fields = tui.continuation_fields(item, {"state": "working", "viewport_checked_at": 100})
        return tui.Candidate(item, "explicit", "working", "", 0, item["paused"],
                             agent_kind="codex", **fields)

    def test_pause_origin_user_does_not_claim_a_human_actor(self):
        candidate = self.candidate(target())
        summary = tui.focus_summary(candidate)
        for text in ("已暂停 50s", "maintenance hold", "来源 user（操作者未记录）", "paused_at=50"):
            self.assertIn(text, summary)
        self.assertNotIn("用户暂停", summary)
        candidate = self.candidate(target(paused_at=101))
        self.assertIn("暂停时长未知", tui.focus_summary(candidate))
        self.assertIn("晚于当前时间", tui.focus_summary(candidate))

    def test_mixed_rows_preserve_active_label_and_show_distinct_holds(self):
        candidates = [self.candidate(target("active", paused=False)), self.candidate(target()),
                      self.candidate(target("disabled", enabled=False, paused=False))]
        self.assertEqual([tui.watch_label(c) for c in candidates], ["监控中", "已暂停", "已停用"])
        counts = tui.group_counts(candidates)
        self.assertEqual((counts["watching"], counts["paused"], counts["disabled"]), (1, 1, 1))
        group = tui.build_view_rows(candidates, set(), tui.DEFAULT_FILTER)[0]
        rendered = tui.group_row_text(group, 160)
        self.assertIn("1 已暂停", rendered)
        self.assertIn("1 已停用", rendered)
        with mock.patch.dict(tui._ATTRS, {"watching": 1, "paused": 2}):
            self.assertEqual([tui.row_attr(c) for c in candidates], [1, 2, 2])

    def test_live_pool_pause_is_visible_while_idle_shell_keeps_its_meaning(self):
        candidate = self.candidate(target())
        candidate.source = "workspace_rule"
        self.assertEqual(tui.watch_label(candidate), "已暂停")
        self.assertEqual(tui.group_counts([candidate])["paused"], 1)
        self.assertEqual(tui.group_counts([candidate])["pool"], 0)
        candidate.source = "workspace_non_codex"
        candidate.agent_kind = "shell"
        candidate.state = "idle"
        self.assertEqual(tui.watch_label(candidate), "整池空转")
        self.assertEqual(tui.group_counts([candidate])["idling"], 1)
        self.assertEqual(tui.group_counts([candidate])["paused"], 0)

    def test_stack_public_projection_and_tui_do_not_render_held_fleets_green(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        for items, expected in (([target()], "paused"), ([target(enabled=False)], "disabled"),
                                ([target(), target("active", paused=False)], "attention")):
            with self.subTest(expected=expected):
                payload = watcher_status(targets=len(items))
                payload["observation_coverage"] = health.summarize_observation(
                    [observe(i) for i in items], now=101, stale_after=30)
                payload["continuation_health"] = health.continuation_report(items,
                    {i["surface_id"]: {"state": "working", "viewport_checked_at": 100} for i in items}, now=101)
                env = sandbox.env(watcher=payload)
                response = run_stack(sandbox, ["status", "--json", "--component", "watcher"], env)
                self.assertEqual(response.returncode, 1)
                document = json.loads(response.stdout)
                self.assertEqual(document["overall"], "degraded")
                watcher = document["components"]["watcher"]
                self.assertEqual(watcher["continuation_health"]["status"], expected)
                self.assertEqual(watcher["observation_coverage"]["counts"]["disabled"], int(expected == "disabled"))
                snapshot = tui._stack_snapshot_from_status(document)
                self.assertTrue(tui.stack_is_alarming(snapshot))
                line = tui.stack_line(snapshot)
                self.assertIn("有异常", line)
                self.assertNotIn("全部正常", line)
                self.assertIn("停用 1" if expected == "disabled" else "暂停 1", line)
                # A visible hold is not a reason for `up` to restart an already
                # scheduled daemon or discard its in-flight delivery state.
                up = run_stack(sandbox, ["up", "--apply", "--json", "--component", "watcher"], env)
                self.assertEqual(up.returncode, 0)
                self.assertEqual([p["action"] for p in json.loads(up.stdout)["plan"]], ["none"])
        self.assertFalse(sandbox.launchctl_calls_write())


if __name__ == "__main__":
    unittest.main()
