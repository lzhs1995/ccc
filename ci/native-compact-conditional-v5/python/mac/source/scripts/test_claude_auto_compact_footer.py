"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Synthetic auto-compact footer regression; all transport calls are mocked."""
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture
from test_claude_footer_parser_20261008 import with_draft
from test_claude_provider_goal_footer import frame


HINT = "3% until auto-compact · /model opus[1m]"
TEXT = "STATUS: auto-compact-footer exact  payload"


def screen(hint=HINT, **kwargs):
    value = frame(provider="example", **kwargs)
    return value.replace("⏱️ 847h 26m", "⏱️ 847h 26m                         " + hint)


class AutoCompactFooterTests(unittest.TestCase):
    def assert_zero_input(self, value):
        with LegacyTerminalFixture(provider="claude") as native, patch.object(
                b, "read_screen", return_value=value):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", TEXT, marker="auto-compact-footer",
                              wait_seconds=0)
            native.send.assert_not_called()
            native.key.assert_not_called()

    def assert_unparsed(self, value):
        self.assertIsNone(b._claude_bordered_compose(value))
        self.assertEqual(b.receiver_input_kind(value), "UNKNOWN")
        self.assertFalse(b.compose_block_is_empty(value))
        self.assert_zero_input(value)

    def test_complete_suffix_and_bounded_ascii_percentage(self):
        for percent in ("0", "3", "9", "10", "99", "100"):
            for goal in ("none", "inline", "final"):
                with self.subTest(percent=percent, goal=goal):
                    value = screen(HINT.replace("3%", percent + "%"),
                                   goal_position=goal)
                    self.assertEqual(b.receiver_input_kind(value), "AGENT_TUI")
                    self.assertTrue(b.compose_block_is_empty(value))
                    self.assertEqual(b.compose_block_text(value), "")
                    self.assertFalse(b.receiver_cannot_submit_now(value))

    def test_existing_wrapped_model_cwd_elapsed_layouts(self):
        value = screen(goal_position="none")
        for wrapped in (
                value.replace("] │ tasks/", "]\n  tasks/"),
                value.replace("] │ tasks/", "]\n  tasks/").replace(
                    "git:(main) │ ⏱️", "git:(main)\n  ⏱️")):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(b.receiver_input_kind(wrapped), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(wrapped))

    def test_unknown_suffixes_fail_closed_without_input(self):
        malformed = [HINT.replace("3%", p + "%") for p in (
            "-1", "+3", "101", "03", "3.0", "３", "3e0", "")]
        malformed += [
            HINT.replace("auto-compact", "auto compact"),
            HINT.replace("until", "Until"), HINT.replace(" · ", " | "),
            HINT.replace("/model", "/models"), HINT.replace("opus[1m]", "unknown"),
            HINT.replace("opus[1m]", "opus[2m]"),
            HINT.replace("opus[1m]", "opus[1m"),
            HINT.replace(" until ", "\tuntil "), HINT + " extra",
            HINT + " │ extra", HINT + " " + HINT,
            HINT + " new task? /clear to save 3k tokens",
            HINT.replace(" · ", "\n  · "),
        ]
        for hint in malformed:
            with self.subTest(hint=hint):
                self.assert_unparsed(screen(hint))

    def test_hint_only_in_elapsed_field_below_complete_borders(self):
        value = frame(provider="example", goal_position="none")
        for changed in (
                value + HINT + "\n",
                value.replace("tasks/example", "tasks/example " + HINT),
                value.replace("18%", "18% " + HINT),
                value.replace("✓ Bash ×8", "✓ Bash ×8 " + HINT),
                screen().replace("─" * 100, "─" * 99, 1),
                screen().replace("─" * 100, "", 1),
                screen().replace("847h 26m                         ", ""),
                screen().replace("847h 26m                         ", "847h 26m\t"),
                screen().replace("847h 26m                         ", "847h 26m\u00a0"),
                screen() + "unexpected footer row\n"):
            with self.subTest(changed=changed):
                self.assert_unparsed(changed)

    def test_footer_shaped_drafts_are_preserved_verbatim(self):
        for draft in (HINT, "  " + HINT + "  ", "\n" + HINT,
                      "first\n  " + HINT + "\nlast  ", "\t" + HINT,
                      TEXT, "[Pasted text #1 +3 lines]"):
            with self.subTest(draft=draft):
                value = with_draft(screen(), draft)
                self.assertEqual(b.receiver_input_kind(value), "AGENT_TUI")
                self.assertEqual(b.compose_block_text(value), draft)
                self.assertFalse(b.compose_block_is_empty(value))
                self.assert_zero_input(value)

    def test_exact_pending_payload_preserves_spaces_and_line_breaks(self):
        for draft in (TEXT, HINT, "first\n  " + HINT + "\nlast  "):
            value = with_draft(screen(), draft)
            self.assertTrue(b._exact_pending_text(value, draft))
            for changed in (draft + " extra", " " + draft, draft.replace(" ", "  ", 1)):
                with self.subTest(draft=draft, changed=changed):
                    self.assertFalse(b._exact_pending_text(value, changed))

    def test_active_compaction_and_reconnection_refuse_input(self):
        for status in ("✻ Compacting conversation…", "✢ Reconnecting... 1/10"):
            with self.subTest(status=status):
                value = screen(status=status)
                self.assertEqual(b.receiver_input_kind(value), "AGENT_TUI")
                self.assertTrue(b.receiver_cannot_submit_now(value))
                self.assert_zero_input(value)

    def test_busy_goal_accepts_ready_message_without_clear_or_pause(self):
        for goal in ("inline", "final"):
            with self.subTest(goal=goal), LegacyTerminalFixture(provider="claude") as native:
                before = screen(goal_position=goal, tool=True,
                                status="✢ Crafting… (23s · ↑ 4.9k tokens)")
                self.assertTrue(b._queued_or_active_input(before))
                self.assertFalse(b.receiver_cannot_submit_now(before))
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.require_clearable_agent_input(before, "offline-peer")
                current = {"screen": before}

                def paste(surface, payload):
                    self.assertEqual((surface, payload), ("offline-peer", TEXT))
                    current["screen"] = with_draft(before, payload)

                def receive(surface, key):
                    self.assertEqual((surface, key), ("offline-peer", "enter"))
                    native.append_user(TEXT)
                    current["screen"] = before

                native.send.side_effect = paste
                native.key.side_effect = receive
                with patch.object(b, "read_screen", side_effect=lambda *a, **k: current["screen"]):
                    result = b.submit_text("offline-peer", TEXT,
                                           marker="auto-compact-footer", wait_seconds=0)
                self.assertTrue(result["confirmed"])
                self.assertEqual(result["confirmation_source"], "native_user_message_v1")
                native.send.assert_called_once_with("offline-peer", TEXT)
                native.key.assert_called_once_with("offline-peer", "enter")
                self.assertEqual(current["screen"], before)

    def test_queue_only_is_unconfirmed_and_reconciliation_never_resends(self):
        with LegacyTerminalFixture(provider="claude") as native:
            before = screen(tool=True)
            current = {"screen": before}

            def paste(surface, payload):
                current["screen"] = with_draft(before, payload)

            def queue(surface, key):
                native.append_queued(TEXT)
                current["screen"] = before

            native.send.side_effect = paste
            native.key.side_effect = queue
            with patch.object(b, "read_screen", side_effect=lambda *a, **k: current["screen"]):
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.submit_text("offline-peer", TEXT, marker="auto-compact-footer",
                                  wait_seconds=0)
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.submit_text("offline-peer", TEXT, marker="auto-compact-footer",
                                  reconcile_only=True, wait_seconds=0)
            native.send.assert_called_once_with("offline-peer", TEXT)
            native.key.assert_called_once_with("offline-peer", "enter")


if __name__ == "__main__":
    unittest.main()
