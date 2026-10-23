"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Synthetic Claude layouts retaining measured footer parser conditions.

Private task paths, UUIDs and transcript prose are not part of these public
fixtures. They exercise parser behavior and are not live delivery evidence.
"""
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture, ScreenSequence
from test_claude_footer_parser_20261008 import FIXTURES, footer_variant, with_draft


HINT = "new task? /clear to save 170.9k tokens"
TEXT = "STATUS: FOOTER044 exact  body"


def captured(label):
    return (FIXTURES / f"claude-clear-hint-{label}-20261009.txt").read_text()


def with_hint(hint):
    return footer_variant(captured("b"), lambda footer: footer.replace(HINT, hint))


class ClaudeClearHintFooterTests(unittest.TestCase):
    def assert_zero_input(self, screen):
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", return_value=screen):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", TEXT, marker="FOOTER044")
            native.send.assert_not_called()
            native.key.assert_not_called()

    def assert_unknown_and_zero_input(self, screen):
        self.assertIsNone(b._claude_bordered_compose(screen))
        self.assertEqual(b.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assert_zero_input(screen)

    def test_fixture_b_empty_composer_with_decimal_hint_is_idle(self):
        screen = captured("b")
        self.assertIn(HINT, screen)
        self.assertIn("• Reconnecting...", screen)
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertEqual(b.compose_block_text(screen), "")
        self.assertFalse(b._queued_or_active_input(screen))
        self.assertFalse(b.receiver_cannot_submit_now(screen))

    def test_fixture_a_folded_draft_remains_occupied(self):
        screen = captured("a")
        self.assertIn("new task? /clear to save 245.7k tokens", screen)
        self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
        self.assertEqual(b.compose_block_text(screen), "[Pasted text #1 +3 lines]")
        self.assertFalse(b.compose_block_is_empty(screen))
        self.assertFalse(b._exact_pending_text(screen, TEXT))
        self.assert_zero_input(screen)

    def test_hint_is_optional_and_numeric_forms_are_explicit(self):
        for number in ("170.9k", "245.7k", "1.0k", "171k", "0", "999", "170900"):
            with self.subTest(number=number):
                screen = with_hint(f"new task? /clear to save {number} tokens")
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
        self.assertEqual(b.receiver_input_kind(with_hint("")), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(with_hint("")))

    def test_hint_shaped_body_is_preserved_character_for_character(self):
        for draft in (HINT, "  " + HINT, "\n  " + HINT,
                      "first\n  " + HINT + "\n  last",
                      "⏱️  99h 44m       " + HINT,
                      "\n\n  " + HINT + "  "):
            with self.subTest(draft=draft):
                screen = with_draft(captured("b"), draft)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertEqual(b.compose_block_text(screen), draft)
                self.assertFalse(b.compose_block_is_empty(screen))
                self.assert_zero_input(screen)

    def test_unknown_hint_spelling_units_and_numbers_fail_closed(self):
        hints = [
            HINT.replace("new task?", "new task"),
            HINT.replace("new", "New"), HINT.replace("/clear", "/clean"),
            HINT.replace("to save", "to  save"), HINT.replace("to save", "to\tsave"),
            HINT.replace("tokens", "token"), HINT + " extra",
            HINT + " │ extra", HINT + " " + HINT,
        ]
        hints += [HINT.replace("170.9k", number) for number in (
            "-170.9k", "+170.9k", "170.9", "170.90k", ".9k", "170.k",
            "170.9K", "170.9m", "1e5", "170,900", "NaN", "１７０.９k", "")]
        for hint in hints:
            with self.subTest(hint=hint):
                self.assert_unknown_and_zero_input(with_hint(hint))

    def test_hint_requires_the_elapsed_field_and_ascii_padding(self):
        for elapsed in ("", "unknown", "⏱️  unknown", "⏱️  99h 44m",
                        "⏱️  99h 44m\t", "⏱️  99h 44m\u00a0"):
            with self.subTest(elapsed=elapsed):
                screen = footer_variant(captured("b"), lambda footer: footer.replace(
                    "⏱️  99h 44m       ", elapsed))
                self.assert_unknown_and_zero_input(screen)

    def test_hint_is_only_inline_or_a_final_independent_footer_row(self):
        screen = with_hint("")
        for anchor in ("上下文 ██░░░░░░░░ 17%", "1 CLAUDE.md | 9 MCPs | 7 钩子",
                       "✓ Bash ×13 | ✓ Edit ×5 | ✓ Write ×2"):
            with self.subTest(anchor=anchor):
                changed = footer_variant(screen, lambda footer: footer.replace(
                    anchor, anchor + " " + HINT))
                self.assert_unknown_and_zero_input(changed)
        separate = screen + "  " + HINT + "\n"
        self.assertEqual(b.receiver_input_kind(separate), "AGENT_TUI")
        self.assertTrue(b.compose_block_is_empty(separate))
        self.assert_unknown_and_zero_input(separate + "  unknown current state\n")
        self.assert_unknown_and_zero_input(screen + "  " + HINT + " extra\n")
        self.assert_unknown_and_zero_input(captured("b") + "  unknown current state\n")

    def test_missing_or_mismatched_borders_still_fail_closed(self):
        rows = captured("b").splitlines()
        start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
        for removed in ((start - 1,), (start + 1,), (start - 1, start + 1)):
            with self.subTest(removed=removed):
                self.assert_unknown_and_zero_input("\n".join(
                    row for i, row in enumerate(rows) if i not in removed))
        rows[start + 1] = rows[start + 1][:-1]
        self.assert_unknown_and_zero_input("\n".join(rows))

    def test_standalone_clear_hint_accepts_only_exact_goal_suffix(self):
        base = with_hint("")
        for duration in ("4h", "4h 2m", "3m 2s", "8s"):
            hint = HINT + f" · ◎ /goal active ({duration})"
            screen = base + "  " + hint + "\n"
            self.assertTrue(b.compose_block_is_empty(screen))
            self.assertFalse(b._queued_or_active_input(screen))
            draft = with_draft(screen, hint)
            self.assertEqual(b.compose_block_text(draft), hint)
            self.assert_zero_input(draft)
        for suffix in (" · ◎ /goal active (4h) extra", " · ◎ /goal done (4h)",
                       " · ◎ /goal active (soon)", " | ◎ /goal active (4h)"):
            self.assert_unknown_and_zero_input(base + "  " + HINT + suffix + "\n")

    def test_compaction_and_reconnection_with_hint_refuse_all_input(self):
        for status in ("✻ Compacting context (23s)",
                       "✽ Compacting conversation… (23s)", "✢ Reconnecting... 1/10"):
            with self.subTest(status=status):
                screen = captured("b").replace("✻ Baked for 14m 1s", status)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.receiver_cannot_submit_now(screen))
                self.assert_zero_input(screen)

    def test_active_tool_and_spinner_with_hint_remain_unclearable(self):
        tool = footer_variant(captured("b"), lambda footer: footer.replace(
            "✓ Bash ×13", "◐ Bash: .../task | ✓ Bash ×13"))
        rows = captured("b").splitlines()
        start = max(i for i, row in enumerate(rows) if row.startswith("❯"))
        rows.insert(start - 1, "✢ Crafting… (23s · ↑ 4.9k tokens)")
        for screen in (tool, "\n".join(rows)):
            with self.subTest(screen=screen[-800:]), LegacyTerminalFixture(provider="claude") as native:
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                self.assertTrue(b._queued_or_active_input(screen))
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.require_clearable_agent_input(screen, "offline-peer")
                native.send.assert_not_called()
                native.key.assert_not_called()

    def test_altered_body_spaces_tabs_and_blank_rows_never_match(self):
        for draft in (TEXT[:-1], TEXT.replace("exact", "edited"), " " + TEXT,
                      TEXT.replace("exact  body", "exact body"),
                      TEXT.replace("exact  body", "exact\tbody"),
                      TEXT + "  ", TEXT + "\n  ", "\n  " + TEXT):
            with self.subTest(draft=draft):
                screen = with_draft(captured("b"), draft)
                self.assertEqual(b.compose_block_text(screen), draft)
                self.assertFalse(b._exact_pending_text(screen, TEXT))
                self.assert_zero_input(screen)

    def test_post_paste_edit_or_folded_summary_authorizes_no_key(self):
        for draft in (TEXT.replace("exact", "edited"), TEXT + "  ",
                      TEXT.replace("exact  body", "exact body"),
                      "[Pasted text #1 +3 lines]"):
            before = captured("b")
            after = with_draft(before, draft)
            with self.subTest(draft=draft), LegacyTerminalFixture(provider="claude") as native, \
                    patch.object(b, "read_screen", side_effect=ScreenSequence([before, after])):
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.submit_text("offline-peer", TEXT, marker="FOOTER044")
                native.send.assert_called_once()
                native.key.assert_not_called()

    def test_full_draft_requires_two_stable_observations(self):
        before = captured("b")
        draft = with_draft(before, TEXT)
        edited = with_draft(before, TEXT.replace("exact", "edited"))
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", side_effect=ScreenSequence([before, draft, edited])):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", TEXT, marker="FOOTER044")
            native.send.assert_called_once()
            native.key.assert_not_called()

    def test_recognized_footer_and_enter_do_not_prove_native_reception(self):
        before = captured("b")
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", side_effect=native.ready_screens(before, with_draft(before, TEXT))):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", TEXT, marker="FOOTER044")
            native.send.assert_called_once()
            native.key.assert_called_once_with("offline-peer", "enter")

    def test_exact_stable_draft_and_new_native_user_confirm_delivery_offline(self):
        before = captured("b")
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", side_effect=native.ready_screens(before, with_draft(before, TEXT))):
            native.key.side_effect = native.receipt_on_key(TEXT)
            result = b.submit_text("offline-peer", TEXT, marker="FOOTER044")
            native.send.assert_called_once()
            native.key.assert_called_once_with("offline-peer", "enter")
            self.assertTrue(result["confirmed"])


if __name__ == "__main__":
    unittest.main()
