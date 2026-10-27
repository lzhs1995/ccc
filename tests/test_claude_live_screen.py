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


if __name__ == "__main__":
    unittest.main()
