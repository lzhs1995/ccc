"""Busy detection around Claude's measured Tip row; no live terminal input."""
import unittest

import cmux_bridge as bridge
from native_test_support import NativeFixture
from test_claude_footer_parser_20261008 import with_draft
from test_claude_provider_goal_footer import frame


TIP = "  ⎿ \u00a0Tip: Use /clear to start fresh when switching topics and free up context"


class ClaudeSpinnerTipTests(unittest.TestCase):
    def test_tip_below_live_spinner_keeps_input_gate_closed(self):
        for spinner in ("✢ Brewing… (1m 2s)", "✻ Compacting conversation…"):
            for suffix in ("", "\n\n◎ /goal active (59m)"):
                with self.subTest(spinner=spinner, suffix=suffix):
                    screen = frame(status=spinner + "\n" + TIP + suffix)
                    self.assertEqual(bridge.receiver_input_kind(screen), "AGENT_TUI")
                    self.assertTrue(bridge.compose_block_is_empty(screen))
                    self.assertTrue(bridge._queued_or_active_input(screen))
                    with NativeFixture(provider="claude") as native:
                        with self.assertRaises(bridge.DispatchUnconfirmed):
                            bridge.require_clearable_agent_input(screen, "offline-peer")
                        native.send.assert_not_called()
                        native.key.assert_not_called()

    def test_tip_below_completed_summary_does_not_strand_idle_input(self):
        for summary in ("✻ Brewed for 3m", "✻ Cooked for 42s"):
            with self.subTest(summary=summary):
                screen = frame(status=summary + "\n" + TIP)
                self.assertFalse(bridge._queued_or_active_input(screen))
                bridge.require_clearable_agent_input(screen, "offline-peer")

    def test_history_and_tip_shaped_drafts_are_preserved(self):
        screen = frame(status="✢ Brewing…\nFinished reviewing.\n" + TIP)
        self.assertFalse(bridge._queued_or_active_input(screen))
        draft = "✢ Brewing…\n" + TIP
        screen = with_draft(frame(status="✻ Brewed for 3m\n" + TIP), draft)
        self.assertEqual(bridge.compose_block_text(screen), draft)
        self.assertFalse(bridge.compose_block_is_empty(screen))
        self.assertFalse(bridge._queued_or_active_input(screen))


if __name__ == "__main__":
    unittest.main()
