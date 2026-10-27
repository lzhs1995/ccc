"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Synthetic observed-layout fixtures keep rules-count chrome outside the borders."""
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture
from test_claude_footer_parser_20261008 import FIXTURES, with_draft, footer_variant


COUNTS = "2 CLAUDE.md | 1 规则 | 10 MCPs | 7 钩子"


def captured(surface):
    return (FIXTURES / f"claude-rules-c{surface}-20261009.txt").read_text()


class ClaudeRulesFooterTests(unittest.TestCase):
    def assert_zero_input(self, screen):
        with LegacyTerminalFixture(provider="claude"), patch.object(b, "read_screen", return_value=screen), \
                patch.object(b, "send_text") as paste, patch.object(b, "send_key") as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", "STATUS: new-token", marker="new-token")
        paste.assert_not_called()
        key.assert_not_called()

    def test_both_fixture_empty_composers_are_recognized(self):
        for surface in (29, 2598):
            with self.subTest(surface=surface):
                screen = captured(surface)
                self.assertIn(COUNTS, screen)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                self.assertEqual(b.compose_block_text(screen).strip(), "")

    def test_active_turn_remains_blocked_with_empty_composer(self):
        screen = captured(2598)
        self.assertTrue(b._queued_or_active_input(screen))
        with patch.object(b, "send_text") as paste, patch.object(b, "send_key") as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                b.require_clearable_agent_input(screen, "offline-peer")
        paste.assert_not_called()
        key.assert_not_called()
        self.assertFalse(b._queued_or_active_input(captured(29)))

    def test_real_drafts_and_footer_lookalikes_are_preserved(self):
        for draft in ("STATUS: original body", COUNTS, "\n  " + COUNTS,
                      "first\n  " + COUNTS + "\n  last", "  spaced\t正文  ",
                      "\n\n  1 规则\n  ✓ Bash ×20"):
            with self.subTest(draft=draft):
                screen = with_draft(captured(29), draft)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assertEqual(b.compose_block_text(screen), draft)
                self.assert_zero_input(screen)

    def test_incomplete_payload_still_fails_exact_match(self):
        payload = "STATUS: only-the-whole-original-payload"
        for draft in (payload[:16], payload + " edited", payload.replace("original", "other")):
            with self.subTest(draft=draft):
                screen = with_draft(captured(29), draft)
                self.assertFalse(b._exact_pending_text(screen, payload))
                self.assert_zero_input(screen)
        self.assertTrue(b._exact_pending_text(with_draft(captured(29), payload), payload))

    def test_malformed_or_unknown_count_fields_refuse_input(self):
        for counts in ("2 CLAUDE.md | 规则 | 10 MCPs | 7 钩子",
                       "2 CLAUDE.md | -1 规则 | 10 MCPs | 7 钩子",
                       "2 CLAUDE.md | 1 其他 | 10 MCPs | 7 钩子",
                       "2 CLAUDE.md | 1 规则 | 10 MCPs",
                       "2 CLAUDE.md | 1 规则 | 1 规则 | 10 MCPs | 7 钩子",
                       "2 CLAUDE.md | 10 MCPs | 1 规则 | 7 钩子"):
            with self.subTest(counts=counts):
                screen = footer_variant(captured(29), lambda text: text.replace(COUNTS, counts))
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_unknown_footer_rows_remain_occupied(self):
        screen = captured(29) + "  unrecognized state\n"
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assert_zero_input(screen)


if __name__ == "__main__":
    unittest.main()
