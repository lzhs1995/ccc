"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Synthetic measured HUD layout; no private task text or live terminal input."""
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture
from test_claude_footer_parser_20261008 import FIXTURES, footer_variant, with_draft


CONTEXT = "上下文 █████████░ 93%"
BREAKDOWN = " (输入: 78k, 缓存: 854k)"


def captured():
    return (FIXTURES / "claude-context-breakdown-20261013.txt").read_text()


def idle():
    rows = captured().splitlines()
    start = next(i for i, row in enumerate(rows) if row.startswith("─"))
    return "✻ Baked for 14m 1s\n" + "\n".join(rows[start:]) + "\n"


def breakdown(value):
    return footer_variant(idle(), lambda footer: footer.replace(BREAKDOWN, value))


class ClaudeContextBreakdownTests(unittest.TestCase):
    def assert_zero_input(self, screen):
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", return_value=screen):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", "STATUS: CONTEXT0433", marker="CONTEXT0433")
            native.send.assert_not_called()
            native.key.assert_not_called()

    def assert_unknown(self, screen):
        self.assertIsNone(b._claude_bordered_compose(screen))
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assert_zero_input(screen)

    def test_measured_compacting_footer_has_an_empty_composer(self):
        screen = captured()
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertEqual(b.compose_block_text(screen), "")
        self.assertTrue(b.receiver_cannot_submit_now(screen))
        self.assert_zero_input(screen)

    def test_same_footer_at_idle_is_recognized(self):
        screen = idle()
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertFalse(b.receiver_cannot_submit_now(screen))
        self.assertFalse(b._queued_or_active_input(screen))

    def test_idle_complete_draft_and_native_user_receipt_confirm_one_send(self):
        before = idle()
        payload = "STATUS: CONTEXT0433"
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", side_effect=native.ready_screens(
                    before, with_draft(before, payload))):
            native.key.side_effect = native.receipt_on_key(payload)
            result = b.submit_text("offline-peer", payload, marker="CONTEXT0433")
            self.assertTrue(result["confirmed"])
            native.send.assert_called_once()
            native.key.assert_called_once_with("offline-peer", "enter")

    def test_optional_breakdown_and_renderer_number_formats(self):
        self.assertTrue(b.compose_block_is_empty(breakdown("")))
        for count in ("0", "1", "999", "1k", "78k", "854k", "999k", "1000k",
                      "1.0M", "12.3M"):
            for field in ("78k", "854k"):
                with self.subTest(count=count, field=field):
                    screen = breakdown(BREAKDOWN.replace(field, count))
                    self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                    self.assertTrue(b.compose_block_is_empty(screen))

    def test_breakdown_shaped_drafts_are_preserved_exactly(self):
        for draft in (CONTEXT + BREAKDOWN, BREAKDOWN, "  " + BREAKDOWN + "  ",
                      "\n  " + CONTEXT + BREAKDOWN,
                      "first\n  " + CONTEXT + BREAKDOWN + "\n  last",
                      "\n\n输入: 78k,\t缓存: 854k  ", "[Pasted text #1 +3 lines]"):
            with self.subTest(draft=draft):
                screen = with_draft(idle(), draft)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertEqual(b.compose_block_text(screen), draft)
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_unknown_numbers_and_units_authorize_no_input(self):
        for count in ("", "-1", "+78k", "01", "0k", "01k", "1000", "1001k",
                      "78.0k", "78K", "78m", "78M", "0.8M", "1.00M", ".8M",
                      "1e5", "78,000", "NaN", "７８k", "78 k"):
            for field in ("78k", "854k"):
                with self.subTest(count=count, field=field):
                    self.assert_unknown(breakdown(BREAKDOWN.replace(field, count)))

    def test_unknown_breakdown_syntax_or_suffix_authorizes_no_input(self):
        for value in (BREAKDOWN + " extra", BREAKDOWN + BREAKDOWN,
                      BREAKDOWN.replace("输入", "输出"),
                      BREAKDOWN.replace("缓存", "cache"),
                      BREAKDOWN.replace(": ", ":\t"), BREAKDOWN.replace(", ", ","),
                      BREAKDOWN.replace(" (", "\t("),
                      BREAKDOWN.replace(" (", " （"), BREAKDOWN[:-1],
                      " (缓存: 854k, 输入: 78k)", " (输入: 78k)"):
            with self.subTest(value=value):
                self.assert_unknown(breakdown(value))
        self.assert_unknown(idle() + "  unknown current state\n")

    def test_breakdown_requires_the_context_row_and_complete_borders(self):
        plain = breakdown("")
        for anchor in ("2 CLAUDE.md | 1 规则 | 10 MCPs | 7 钩子",
                       "✓ Bash ×12 | ✓ Edit ×6 | ✓ Write ×2",
                       "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"):
            with self.subTest(anchor=anchor):
                self.assert_unknown(footer_variant(
                    plain, lambda footer: footer.replace(anchor, anchor + BREAKDOWN)))
        rows = idle().splitlines()
        start = next(i for i, row in enumerate(rows) if row.startswith("❯"))
        for removed in ((start - 1,), (start + 1,), (start - 1, start + 1)):
            with self.subTest(removed=removed):
                self.assert_unknown("\n".join(
                    row for i, row in enumerate(rows) if i not in removed))
        rows[start + 1] = rows[start + 1][:-1]
        self.assert_unknown("\n".join(rows))

    def test_compacting_reconnecting_or_active_tool_still_authorizes_no_clear(self):
        for status in ("✽ Compacting conversation… (23s)", "✢ Reconnecting... 1/10",
                       "✢ Crafting… (23s · ↑ 4.9k tokens)"):
            with self.subTest(status=status):
                screen = idle().replace("✻ Baked for 14m 1s", status)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                if "Reconnecting" not in status:
                    with self.assertRaises(b.DispatchUnconfirmed):
                        b.require_clearable_agent_input(screen, "offline-peer")
                if "Crafting" not in status:
                    self.assertTrue(b.receiver_cannot_submit_now(screen))
                    self.assert_zero_input(screen)


if __name__ == "__main__":
    unittest.main()
