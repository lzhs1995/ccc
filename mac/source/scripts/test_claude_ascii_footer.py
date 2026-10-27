"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Measured empty Stata Claude composers; all input is isolated/mocked."""
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture
from test_claude_footer_parser_20261008 import FIXTURES, with_draft


def captured(name):
    return (FIXTURES / f"claude-ascii-{name}-20261012.txt").read_text()


class ClaudeAsciiFooterTests(unittest.TestCase):
    def assert_zero_input(self, screen):
        with LegacyTerminalFixture(provider="claude") as native, \
                patch.object(b, "read_screen", return_value=screen):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("offline-peer", "STATUS: fresh-token", marker="fresh-token")
            native.send.assert_not_called()
            native.key.assert_not_called()

    def test_measured_empty_composers_deliver_once(self):
        for name in ("control", "state"):
            with self.subTest(name=name), LegacyTerminalFixture(provider="claude") as native:
                screen = captured(name)
                self.assertEqual(b.receiver_input_kind(screen), "AGENT_TUI")
                self.assertTrue(b.compose_block_is_empty(screen))
                self.assertFalse(b._queued_or_active_input(screen))
                payload = "STATUS: fresh-token"
                native.key.side_effect = native.receipt_on_key(payload)
                with patch.object(b, "read_screen", side_effect=native.ready_screens(
                        screen, with_draft(screen, payload))):
                    result = b.submit_text("offline-peer", payload, marker="fresh-token")
                self.assertTrue(result["confirmed"])
                native.send.assert_called_once()
                native.key.assert_called_once()

    def test_footer_text_inside_composer_is_preserved(self):
        for name in ("control", "state"):
            for draft in ("actual draft", "tasks/work git:(ops/loc...",
                          "▸ Relaunch and complete standard sta...", "line 1\n  line 2"):
                with self.subTest(name=name, draft=draft):
                    screen = with_draft(captured(name), draft)
                    self.assertEqual(b.compose_block_text(screen), draft)
                    self.assert_zero_input(screen)

    def test_clipping_only_at_row_end_with_complete_structure(self):
        for name in ("control", "state"):
            screen = captured(name)
            border = screen.splitlines()[0]
            changes = (
                screen.replace("...", "... extra", 1),
                screen.replace("...", "... │ ⏱️  1h", 1),
                screen.replace("...", "..", 1),
                screen.replace(border, border[:-1], 1),
                screen.replace(border + "\n", "", 1),
                screen.replace("  ✓ Bash", "  unknown footer\n  ✓ Bash"),
                screen + "user@mac ~ %\n",
            )
            for changed in changes:
                with self.subTest(name=name, screen=changed):
                    self.assertFalse(b.compose_block_is_empty(changed))
                    self.assert_zero_input(changed)
        screen = captured("control")
        for summary in ("▸ Relaunch and complete standard sta... extra",
                        "▸ Relaunch and complete standard sta... │ unknown",
                        "▸ Relaunch and complete standard sta.."):
            changed = screen.replace("▸ Relaunch and complete standard sta...", summary)
            self.assertFalse(b.compose_block_is_empty(changed))
            self.assert_zero_input(changed)

    def test_empty_composer_does_not_override_active_tool(self):
        screen = captured("state").replace("✓ Bash ×20", "◐ Bash: running.py")
        self.assertTrue(b.compose_block_is_empty(screen))
        self.assertTrue(b._queued_or_active_input(screen))
        with self.assertRaises(b.DispatchUnconfirmed):
            b.require_clearable_agent_input(screen, "offline-peer")


if __name__ == "__main__":
    unittest.main()
