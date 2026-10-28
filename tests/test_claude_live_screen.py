"""A scrolled viewport must not decide ownership of the live Claude input."""
import copy
import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as w
from tests.test_claude_composer_boundaries import bordered_payload, payload_from_lines
from tests import test_claude_provider_guard as provider_guard


class LiveScreenTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for name in ("_notify", "_notify_async"):
            patcher = mock.patch.object(w.WatchDaemon, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def case(self, live_payload, viewport_payload, *, pending=False):
        d, t, r, e, c = provider_guard.ProviderGuardTests().case(self.folder.name, pending=pending)
        c.payload = c._base_payload = copy.deepcopy(live_payload)
        raw_replay = c.replay
        calls = []

        def replay(workspace_id, surface_id, *, live=None, timeout=12.0):
            calls.append(live)
            if live:
                return raw_replay(workspace_id, surface_id, live=live, timeout=timeout)
            return copy.deepcopy(viewport_payload)

        c.replay = replay
        return d, t, r, e, c, calls

    def test_old_prompt_outside_cursor_window_does_not_hide_live_empty_nbsp(self):
        rows = ["historical tools"] * 24
        rows[16] = "❯ historical continuation"
        viewport = payload_from_lines(rows, cursor_row=0)
        live = bordered_payload(["\xa0"])[2]
        self.assertEqual(w._claude_composer_state(w.Grid.from_rpc(viewport, "surface-uuid"))[0],
                         "unverified")
        self.assertEqual(w._claude_composer_state(w.Grid.from_rpc(live, "surface-uuid"))[0],
                         "empty")
        d, t, r, e, c, calls = self.case(live, viewport)
        d._claude_event_snapshot(t, c)
        self.assertTrue(calls)
        self.assertTrue(all(value is True for value in calls))
        self.assertEqual((c.sent_text, c.sent_keys), ([], []))

    def test_empty_viewport_cannot_authorize_input_over_a_live_user_draft(self):
        d, t, r, e, c, calls = self.case(
            bordered_payload(["unfinished user draft"])[2], bordered_payload([""])[2])
        before = r.to_dict()
        with self.assertRaises(w.IncompatibleError):
            d._claude_event_snapshot(t, c)
        self.assertEqual(r.to_dict(), before)
        self.assertEqual((c.sent_text, c.sent_keys), ([], []))
        self.assertTrue(calls)
        self.assertTrue(all(value is True for value in calls))

    def test_classification_does_not_reuse_a_passed_historical_viewport(self):
        rows = ["historical tools"] * 24
        rows[16] = "❯ historical continuation"
        viewport = payload_from_lines(rows, cursor_row=0)
        d, t, r, e, c, calls = self.case(bordered_payload(["\xa0"])[2], viewport)
        state = d._classify_target_screen(t, c.text, c,
                                         grid=w.Grid.from_rpc(viewport, "surface-uuid"))
        self.assertTrue(calls)
        self.assertTrue(all(value is True for value in calls))
        self.assertIsNotNone(state.claude_context)
        self.assertEqual(state.claude_context.composer_kind, "empty")
        self.assertEqual((c.sent_text, c.sent_keys), ([], []))

    def test_pending_enter_uses_live_draft_and_preserves_original_budget(self):
        d, t, r, e, c, calls = self.case(
            bordered_payload(["unfinished user draft"])[2], bordered_payload([""])[2], pending=True)
        budget_keys = ("claude_submit_attempts", "claude_submit_phase", "claude_submit_event_id",
                       "claude_submit_message_hash", "claude_orphan_enter_count", "claude_orphan_enter_at")
        before = {key: r.to_dict()[key] for key in budget_keys}
        before_input = copy.deepcopy((c.sent_text, c.sent_keys))
        self.assertFalse(d._send_claude_enter(t, r, c, reason="live-screen-regression"))
        self.assertEqual({key: r.to_dict()[key] for key in budget_keys}, before)
        self.assertTrue(r.claude_submit_not_sent)
        self.assertFalse(r.claude_submit_write_unknown)
        self.assertEqual((c.sent_text, c.sent_keys), before_input)
        self.assertTrue(calls)
        self.assertTrue(all(value is True for value in calls))

    def provider_retry_case(self):
        payload = bordered_payload([""], transcript=("⏺ API Error: connection reset by peer",))[2]
        d, t, r, e, c, calls = self.case(payload, payload)
        with self.assertRaises(w.IncompatibleError):
            d._claude_event_snapshot(t, c)
        self.assertEqual(r.claude_provider_guard["status"], "retry_wait")
        due = r.claude_provider_guard["retry_after"] + 1
        e["created_at"] = due
        return d, t, r, e, c, calls, due

    def test_missing_composer_at_connected_text_write_never_writes(self):
        d, t, r, e, c, calls, due = self.provider_retry_case()
        payload = copy.deepcopy(c.payload)
        grid = w.Grid.from_rpc(payload, "surface-uuid")
        bottom = w._claude_composer_box(grid.lines)[1]
        payload["render_grid"]["row_spans"] = [
            span for span in payload["render_grid"]["row_spans"] if span["row"] != bottom]
        changed = w.Grid.from_rpc(payload, "surface-uuid")
        self.assertEqual(w._claude_composer_state(changed)[0], "unverified")
        self.assertEqual(w.classify_claude_grid(changed).kind, "recoverable_error")
        reached = []

        def before_write(kind):
            if kind == "text":
                reached.append(kind)
                c.payload = c._base_payload = copy.deepcopy(payload)

        provider_guard.ProviderGuardTests().guard_transport(c, before_write)
        r.claude_orphan_enter_count, r.claude_orphan_enter_at = 2, 123.0
        with mock.patch.object(w.time, "time", return_value=due):
            sent, _ = d._send_claude_event(e, t, r, c)
        self.assertFalse(sent)
        self.assertEqual(reached, ["text"], "earlier empty preflights must reach the final write guard")
        self.assertEqual((c.sent_text, c.sent_keys), ([], []))
        self.assertEqual((r.claude_orphan_enter_count, r.claude_orphan_enter_at), (2, 123.0))
        self.assertEqual(r.claude_submit_phase, "none")
        self.assertFalse(r.claude_submit_write_unknown)
        self.assertEqual(d.claude_event_ledger.events[e["event_id"]]["input_not_sent"]["phase"], "text")
        self.assertTrue(all(value is True for value in calls))

    def test_empty_composer_still_allows_retry_after_original_provider_backoff(self):
        d, t, r, e, c, calls, due = self.provider_retry_case()
        provider_guard.ProviderGuardTests().guard_transport(c)
        with mock.patch.object(w.time, "time", return_value=due):
            sent, detail = d._send_claude_event(e, t, r, c)
        self.assertTrue(sent, detail)
        self.assertEqual((len(c.sent_text), len(c.sent_keys)), (1, 1))
        self.assertTrue(all(value is True for value in calls))


if __name__ == "__main__":
    unittest.main()
