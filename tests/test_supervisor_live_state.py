"""Fresh display evidence must not change continuation ownership."""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

import cmux_codex_watch as core
import cmux_supervisor_tui as tui


class LiveStateTests(unittest.TestCase):
    def candidate(self, **changes):
        values = dict(record={"surface_id": "s", "workspace_id": "w"}, source="explicit",
                      state="claude_completed", error_type="", send_count=4, paused=False,
                      agent_kind="claude")
        return tui.Candidate(**{**values, **changes})

    def test_working_raw_viewport_is_published_without_clearing_latch(self):
        runtime = core.TargetRuntime(state="claude_completed", claude_completed_latched=True,
                                     claude_completion_event_id="old-completion", send_count=4)
        daemon = object.__new__(core.WatchDaemon)
        state = core.ScreenState("working", reason="Claude is working", message_kind="claude")
        daemon._classify_target_screen = mock.Mock(return_value=state)
        client = SimpleNamespace(read_viewport=lambda *a, **kw: ("working", None))
        with mock.patch.object(core.time, "time", return_value=100):
            self.assertIs(daemon._observe_target_viewport({"surface_id": "s", "workspace_id": "w"},
                                                         runtime, client), state)
        self.assertEqual((runtime.viewport_state, runtime.viewport_observed_at), ("working", 100))
        self.assertEqual((runtime.state, runtime.claude_completed_latched, runtime.send_count),
                         ("claude_completed", True, 4))
        with mock.patch.object(tui.time, "time", return_value=101):
            display = tui.runtime_viewport_state(vars(runtime))
            row = self.candidate(display_state=display)
            self.assertEqual(tui.screen_label(row), "运行中")
        self.assertEqual(row.state, "claude_completed")
        self.assertEqual(runtime.claude_completion_event_id, "old-completion")

    def test_stale_unreadable_or_invalid_raw_evidence_does_not_override_lifecycle(self):
        fresh = dict(viewport_state="working", viewport_observed_at=100, viewport_readable=True)
        with mock.patch.object(tui.time, "time", return_value=101):
            for invalid in (dict(viewport_observed_at=90), dict(viewport_observed_at=True),
                            dict(viewport_observed_at=float("nan")), dict(viewport_observed_at=102),
                            dict(viewport_readable=False)):
                with self.subTest(invalid=invalid):
                    self.assertEqual(tui.runtime_viewport_state({**fresh, **invalid}), "")
        self.assertEqual(tui.screen_label(self.candidate()), "已完成")

    def test_current_empty_error_does_not_display_historical_502_or_change_trigger(self):
        runtime = dict(state="working", error_type="http_502", observed_state="working",
                       observed_error_type=None, observed_reason="Codex is working", observed_at=100)
        before = copy.deepcopy(runtime)
        error, reason = tui.runtime_observation(runtime)
        self.assertEqual((error, reason), ("-", "Codex is working"))
        self.assertEqual(tui.error_label(self.candidate(state="working", agent_kind="codex",
                                                      error_type=error)), "—")
        self.assertEqual(runtime, before)

    def test_current_failure_still_displays_its_error(self):
        runtime = dict(state="recoverable_error", observed_state="recoverable_error",
                       observed_error_type="http_502", observed_at=100)
        error, _ = tui.runtime_observation(runtime)
        self.assertEqual(error, "http_502")
        self.assertNotEqual(tui.error_label(self.candidate(state="recoverable_error", error_type=error)), "—")

    def test_live_claude_outside_codex_pool_is_not_idling_or_monitored(self):
        row = self.candidate(source="workspace_non_codex", state="untracked", paused=True)
        self.assertFalse(tui.is_idling(row))
        self.assertEqual(tui.watch_label(row), "未纳入")
        self.assertNotIn(tui.watch_label(row), ("监控中", "整池", "整池空转"))
        shell = self.candidate(source="workspace_non_codex", state="idle", agent_kind="shell", paused=True)
        self.assertTrue(tui.is_idling(shell))

    def test_delay_is_visible_without_changing_pause_or_delivery_priority(self):
        row = self.candidate(continuation_status="delayed", observation_age_sec=230)
        self.assertEqual(tui.watch_label(row), "检测延迟")
        row.paused = True
        self.assertEqual(tui.watch_label(row), "已暂停")
        row.paused = False
        row.continuation_status = "delivery_unknown"
        self.assertEqual(tui.watch_label(row), "投递待验")


if __name__ == "__main__":
    unittest.main()
