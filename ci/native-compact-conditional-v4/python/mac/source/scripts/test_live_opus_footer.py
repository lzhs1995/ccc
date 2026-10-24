"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Measured footer geometry with synthetic history; all transport is mocked."""
import re
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture
from test_claude_footer_parser_20261008 import FIXTURES, with_draft, footer_variant


def captured(surface):
    return (FIXTURES / f"claude-opus-c{surface}-20261010.txt").read_text()


def clipped_git_footer(width=55, draft="", split_model=False):
    """Measured footer/border geometry; no captured conversation content."""
    border = "─" * width
    first = ("  [Opus 5]\n  " if split_model else "  [Opus 5] │ ")
    return (f"{border}\n❯ {draft}\n{border}\n"
            f"{first}tasks/07_Paper-workbench git:(ops/local-…\n"
            "  ⏱️  2092h 57m\n  上下文 █░░░░░ 17%\n"
            "  1 CLAUDE.md | 9 MCPs | 7 钩子\n  ✓ Bash ×20\n"
            "  ▸ Relaunch and complete standard stability suite (108/131)\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n")


class LiveOpusFooterTests(unittest.TestCase):
    def assert_zero_input(self, screen):
        with LegacyTerminalFixture(provider="claude"), \
                patch.object(b, "read_screen", return_value=screen), \
                patch.object(b, "send_text") as paste, patch.object(b, "send_key") as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", "STATUS: new-token", marker="new-token")
        paste.assert_not_called()
        key.assert_not_called()

    def test_ellipsis_clipped_git_footer_permits_one_verified_send(self):
        for width, split_model in ((55, False), (90, False), (55, True), (90, True)):
            with self.subTest(width=width, split_model=split_model), LegacyTerminalFixture(provider="claude") as native:
                screen = clipped_git_footer(width, split_model=split_model)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                payload = "STATUS: clipped-footer-token"
                native.key.side_effect = native.receipt_on_key(payload)
                with patch.object(b, "read_screen", side_effect=native.ready_screens(
                        screen, clipped_git_footer(width, payload, split_model=split_model))):
                    result = b.submit_text("offline-peer", payload, marker="clipped-footer-token")
                self.assertTrue(result["confirmed"])
                native.send.assert_called_once()
                native.key.assert_called_once()

    def test_clipped_footer_never_erases_drafts_or_unknown_layouts(self):
        screen = clipped_git_footer()
        drafts = ("actual draft", "tasks/07_Paper-workbench git:(ops/local-…",
                  "[Opus 5] │ tasks/07_Paper-workbench git:(ops/local-…")
        for draft in drafts:
            with self.subTest(draft=draft):
                changed = clipped_git_footer(draft=draft)
                self.assertEqual(b.compose_block_text(changed), draft)
                self.assertFalse(b.compose_block_is_empty(changed))
                self.assert_zero_input(changed)
        for changed in (
                screen.replace("ops/local-…", "ops/local-"),
                screen.replace("ops/local-…", "ops/local-… extra"),
                screen.replace("ops/local-…", "ops/local-… │ ⏱️  1h"),
                screen.replace("上下文 █░░░░░ 17%", "unknown footer"),
                screen.replace("─" * 55, "─" * 54, 1),
                screen + "user@mac ~ % "):
            with self.subTest(screen=changed):
                self.assertFalse(b.compose_block_is_empty(changed))
                self.assert_zero_input(changed)

    def test_both_real_layouts_are_idle_empty_agent_composers(self):
        self.assertIn("| +1 more", captured(2599))
        self.assertIn("[Opus 5 (1M context)]\n", captured(4546))
        for surface in (2599, 4546):
            with self.subTest(surface=surface):
                screen = captured(surface)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                self.assertEqual(b.compose_block_text(screen).strip(), "")
                self.assertFalse(b._queued_or_active_input(screen))
                self.assertFalse(b.receiver_cannot_submit_now(screen))

    def test_split_cwd_and_elapsed_rows_are_a_complete_measured_footer(self):
        screen = captured(4546).replace(
            "fixture/wrapped-footer git:(main) │ ⏱️  144h 52m",
            "fixture/wrapped-footer git:(main)\n  ⏱️  144h 52m")
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertFalse(b._queued_or_active_input(screen))
        self.assertFalse(b.receiver_cannot_submit_now(screen))

    def test_old_report_or_error_keywords_do_not_override_current_footer(self):
        for surface in (2599, 4546):
            screen = ("WAITING_SUPERVISOR REPORT_READY CALLBACK_UNCONFIRMED\n"
                      "• Reconnecting retrying\n✻ API error\n" + captured(surface))
            self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
            self.assertFalse(b.receiver_cannot_submit_now(screen))
            self.assertFalse(b._queued_or_active_input(screen))

    def test_foreign_and_footer_shaped_drafts_are_preserved_exactly(self):
        drafts = ("阿斯顿", "original user draft", "  trailing  ", "line 1\n\n  line 3",
                  "✓ Bash ×11 | +1 more", "+1 more",
                  "[Opus 5 (1M context)]\nwork git:(main) │ ⏱️  4h 12m")
        for surface in (2599, 4546):
            for draft in drafts:
                with self.subTest(surface=surface, draft=draft):
                    screen = with_draft(captured(surface), draft)
                    self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                    self.assertEqual(b.compose_block_text(screen), draft)
                    self.assertFalse(b.compose_block_is_empty(screen))
                    self.assert_zero_input(screen)

    def test_more_summary_requires_exact_positive_count_and_final_position(self):
        for suffix in ("+0 more", "+-1 more", "+1 other", "+1 more draft",
                       "+1 more | ✓ Read ×2", "+1 more | +2 more", "+1"):
            with self.subTest(suffix=suffix):
                screen = footer_variant(captured(2599),
                                        lambda footer: footer.replace("+1 more", suffix))
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_wrapped_cwd_time_requires_complete_known_fields(self):
        transforms = (
            lambda footer: footer.replace("144h 52m", "unknown command"),
            lambda footer: footer.replace("144h 52m", "144h 52m extra"),
            lambda footer: footer.replace("fixture/wrapped-footer", "unknown row / draft"),
            lambda footer: footer.replace(" │ ⏱️", "\n⏱️"),
            lambda footer: footer.replace("[Opus 5 (1M context)]", "[Opus 5 (1M context)] extra"),
            lambda footer: footer.replace("上下文 ██░░░░░░░░ 18%", "上下文 unknown"),
        )
        for transform in transforms:
            with self.subTest(transform=transform):
                screen = footer_variant(captured(4546), transform)
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assert_zero_input(screen)

    def test_current_active_tool_with_more_summary_still_blocks_clearing(self):
        for surface in (2599, 4546):
            screen = footer_variant(captured(surface), lambda footer: re.sub(
                r"(?m)^\s*✓ Bash[^\n]*$",
                "  ◐ Bash: running.py | ✓ Read ×3 | +1 more", footer))
            self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
            self.assertTrue(b.compose_block_is_empty(screen))
            self.assertTrue(b._queued_or_active_input(screen))
            with patch.object(b, "send_text") as paste, patch.object(b, "send_key") as key:
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.require_clearable_agent_input(screen, "offline-peer")
            paste.assert_not_called()
            key.assert_not_called()

    def test_unknown_trailing_rows_and_shells_override_old_agent_footer(self):
        for surface in (2599, 4546):
            for suffix, expected in (("unrecognized state", "UNKNOWN"),
                                     ("✓ unsubmitted draft", "UNKNOWN"),
                                     ("user@mac ~ %", "SHELL"), ("bash-3.2$ ", "SHELL")):
                with self.subTest(surface=surface, suffix=suffix):
                    screen = captured(surface) + "\n" + suffix
                    self.assertEqual(b.receiver_input_kind(screen), expected)
                    self.assertFalse(b.compose_block_is_empty(screen))
                    self.assert_zero_input(screen)

    def test_missing_or_mismatched_border_cannot_authorize_input(self):
        for surface in (2599, 4546):
            rows = captured(surface).splitlines()
            start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
            for boundary in (start - 1, start + 1):
                changed = rows.copy()
                changed[boundary] = changed[boundary][:-1]
                screen = "\n".join(changed)
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assert_zero_input(screen)


if __name__ == "__main__":
    unittest.main()
