"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

真实只读屏幕及其明确标注的派生变体；禁止测试向 live 终端发输入。"""
from pathlib import Path
import re
import textwrap
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture


FIXTURES = Path(__file__).resolve().parents[1] / "verification" / "fixtures"
TOKEN = "B1_01234567"


def captured(surface):
    return (FIXTURES / f"claude-c{surface}-screen-20261008.txt").read_text()


def with_draft(screen, draft):
    rows = screen.splitlines()
    start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
    if rows[start].strip() != "❯":
        raise AssertionError("fixture must start with an empty actual composer")
    rows[start] = "❯\u00a0" + draft
    return "\n".join(rows) + "\n"


def footer_variant(screen, transform):
    # 只改最后输入框之后的 footer；历史屏幕原文不参与变体替换。
    rows = screen.splitlines()
    start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
    return "\n".join(rows[:start + 2]) + "\n" + transform("\n".join(rows[start + 2:])) + "\n"


class ClaudeFooterParserTests(unittest.TestCase):
    def assert_zero_input(self, screen):
        with LegacyTerminalFixture(provider="claude"), patch.object(b, "read_screen", return_value=screen), \
                patch.object(b, "send_text") as paste, patch.object(b, "send_key") as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", "STATUS: new-token", marker="new-token")
        paste.assert_not_called()
        key.assert_not_called()

    def test_real_c41_is_idle_even_with_goal_active(self):
        screen = captured(41)
        self.assertIn("/goal active", screen)
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertEqual(b.compose_block_text(screen).strip(), "")
        self.assertFalse(b._queued_or_active_input(screen))

    def test_real_c2596_tool_is_active_and_composer_independently_empty(self):
        screen = captured(2596)
        self.assertIn("◐ Bash:", screen)
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertTrue(b._queued_or_active_input(screen))
        with self.assertRaises(b.DispatchUnconfirmed):
            b.require_clearable_agent_input(screen, "offline-peer")

    def test_no_git_model_row_preserves_the_other_required_fields(self):
        for surface in (41, 2596):
            with self.subTest(surface=surface):
                screen = footer_variant(captured(surface),
                                        lambda text: re.sub(r" git:\([^)]*\)", "", text))
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                self.assertEqual(b._queued_or_active_input(screen), surface == 2596)

    def test_tool_activity_without_spinner_is_still_active(self):
        screen = captured(2596).replace("✢ Crafting… (24m 23s · ↑ 44.9k tokens)", "")
        self.assertTrue(b._queued_or_active_input(screen))

    def test_active_spinner_without_tool_footer_is_still_active(self):
        screen = footer_variant(captured(2596), lambda text: re.sub(
            r"(?m)^\s*◐ Bash:[^\n]+$", "  ✓ Bash ×11 | ✓ Read ×5 | ✓ Write ×3", text))
        self.assertIn("✢ Crafting…", screen)
        self.assertTrue(b._queued_or_active_input(screen))
        self.assertTrue(b.compose_block_is_empty(screen))

    def test_other_tools_and_animation_frames_use_same_structural_row(self):
        for glyph in "◐◑◒◓":
            for name in ("Read", "Write", "Edit", "mcp__r_studio__execute_r"):
                with self.subTest(glyph=glyph, tool=name):
                    screen = footer_variant(captured(2596), lambda text: text.replace(
                        "◐ Bash:", f"{glyph} {name}:"))
                    self.assertTrue(b._queued_or_active_input(screen))
                    self.assertTrue(b.compose_block_is_empty(screen))

    def test_completed_summaries_and_historical_spinner_are_not_current_activity(self):
        screen = "✳ Running previous tool\n◐ Bash: stale\n" + captured(41)
        self.assertFalse(b._queued_or_active_input(screen))
        self.assertTrue(b.compose_block_is_empty(screen))

    def test_queue_before_composer_remains_active_but_a_draft_does_not(self):
        banner = "Messages to be submitted after completion:"
        rows = captured(41).splitlines()
        start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
        rows[start - 1:start - 1] = [banner, "  queued payload"]
        self.assertTrue(b._queued_or_active_input("\n".join(rows)))
        draft = with_draft(captured(41), banner)
        self.assertFalse(b._queued_or_active_input(draft))
        self.assertFalse(b.compose_block_is_empty(draft))

    def test_first_line_token_and_display_cursor_cell_are_preserved(self):
        for surface in (41, 2596):
            for cursor in ("", " "):
                with self.subTest(surface=surface, cursor=cursor):
                    screen = with_draft(captured(surface), TOKEN + cursor)
                    self.assertFalse(b.compose_block_is_empty(screen))
                    self.assertTrue(b.compose_contains(screen, TOKEN))
                    self.assertTrue(b._exact_pending_text(screen, TOKEN))
                    self.assert_zero_input(screen)

    def test_footer_shaped_drafts_are_never_filtered(self):
        drafts = [
            "✓ Bash ×20", "◐ Bash: " + TOKEN,
            "\n  ✓ Bash ×20", "\n  ◐ Bash: " + TOKEN,
            "\n  ✓ Plan [opus-5]: " + TOKEN + " (<1s)",
            "\n  [claude-opus-5-5[1M]]", "\n  上下文 █░░░ 8%",
            "\n  1 CLAUDE.md | 9 MCPs | 7 钩子",
            "\n  ⏵⏵ bypass permissions on (shift+tab to cycle)",
            "\n  ────────", "\n  >", "\n  │",
        ]
        for draft in drafts:
            with self.subTest(draft=draft):
                screen = with_draft(captured(41), draft)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assertFalse(b._queued_or_active_input(screen))
                self.assertEqual(b.compose_block_text(screen), draft)
                if TOKEN in draft:
                    self.assertTrue(b.compose_contains(screen, TOKEN))
                self.assert_zero_input(screen)

    def test_wrapped_token_remains_pending_and_requires_complete_payload(self):
        screen = captured(41)
        border = next(row for row in reversed(screen.splitlines()) if re.fullmatch("─{8,}", row))
        payload = TOKEN + "_" + "a" * 300
        parts = textwrap.wrap(payload, width=len(border) - 4)
        wrapped = parts[0] + "\n" + "\n".join("  " + row for row in parts[1:])
        screen = with_draft(screen, wrapped)
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assertTrue(b.compose_contains(screen, payload))
        self.assertTrue(b._exact_pending_text(screen, payload))
        self.assertFalse(b._exact_pending_text(screen, payload[:-1]))
        self.assert_zero_input(screen)

    def test_unknown_footer_rows_fail_closed_even_when_they_look_like_chrome(self):
        for unknown in ("unrecognized status", "✓ unsubmitted command", "上下文 draft",
                        "[Opus 5]", "gpt-6 token", TOKEN, "  wrapped footer fragment"):
            with self.subTest(unknown=unknown):
                screen = captured(41) + unknown + "\n"
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_unknown_elapsed_goal_and_incomplete_footer_fail_closed(self):
        transforms = (
            lambda text: text.replace("816h 55m", "unknown command"),
            lambda text: text.replace("active (7h)", "active (unknown)"),
            lambda text: text.replace("1 CLAUDE.md | 9 MCPs | 7 钩子", "1 CLAUDE.md | 9 MCPs"),
            lambda text: text.replace("✓ Bash ×20", "✓ Bash"),
            lambda text: text.replace("⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents", ""),
            lambda text: text.replace("git:(feat/", "git:(feat/\n"),
        )
        for index, transform in enumerate(transforms):
            with self.subTest(variant=index):
                screen = footer_variant(captured(41), transform)
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_missing_or_mismatched_border_cannot_authorize_input(self):
        rows = captured(41).splitlines()
        start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
        for indices in ((start - 1,), (start + 1,), (start - 1, start + 1)):
            edited = [row for i, row in enumerate(rows) if i not in indices]
            with self.subTest(removed=indices):
                screen = "\n".join(edited)
                self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)
        rows[start + 1] = rows[start + 1][:-1]
        screen = "\n".join(rows)
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assert_zero_input(screen)

    def test_same_width_border_in_draft_cannot_hide_the_remaining_body(self):
        screen = captured(41)
        border = next(row for row in reversed(screen.splitlines()) if re.fullmatch("─{8,}", row))
        screen = with_draft(screen, "\n" + border + "\n  " + TOKEN)
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assertTrue(b.compose_contains(screen, TOKEN))
        self.assert_zero_input(screen)

    def test_goal_row_alone_is_not_the_legacy_single_model_footer(self):
        border = "─" * 40
        screen = f"{border}\n❯ \n{border}\n[Opus 5] ◎ /goal active (7h)\n"
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assert_zero_input(screen)

    def test_shell_after_footer_overrides_history(self):
        for shell in ("user@mac ~ %", "bash-3.2$ ", "PS C:\\work> "):
            with self.subTest(shell=shell):
                screen = captured(41) + shell
                self.assertEqual(b.receiver_input_kind(screen), "SHELL")
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_truncated_old_fixture_is_not_a_complete_live_footer(self):
        screen = (FIXTURES / "claude-compose-truncated-footer-20260924.txt").read_text()
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assert_zero_input(screen)


if __name__ == "__main__":
    unittest.main()
