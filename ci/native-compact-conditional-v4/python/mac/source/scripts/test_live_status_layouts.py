"""Synthetic fixtures for observed Claude recap and Codex warning/steer layouts."""
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
from native_test_support import LegacyTerminalFixture
from test_compaction_queue_block import BEFORE, COMPACTING, working


FIXTURES = Path(__file__).resolve().parents[1] / 'verification' / 'fixtures'
CLAUDE_IDLE = (FIXTURES / 'claude2599-idle-report-recap-20261009.txt').read_text()
TEXT = 'STATUS_LAYOUT1003 native delivery regression'
WARN = '  ⚠ 4 warnings · f2 to view'


class CurrentStatusTests(unittest.TestCase):
    def claude_status(self, row):
        self.assertIn('✻ Baked for 14m 1s', CLAUDE_IDLE)
        return CLAUDE_IDLE.replace('✻ Baked for 14m 1s', row)

    def test_idle_claude_report_and_recap_are_not_codex_activity(self):
        self.assertIn('• Compacting context', CLAUDE_IDLE)
        self.assertIn('• Reconnecting...', CLAUDE_IDLE)
        self.assertIn('※ recap:', CLAUDE_IDLE)
        self.assertEqual(bridge.receiver_input_kind(CLAUDE_IDLE), 'AGENT_TUI')
        self.assertTrue(bridge.compose_block_is_empty(CLAUDE_IDLE))
        self.assertFalse(bridge.receiver_cannot_submit_now(CLAUDE_IDLE))
        # Public send proceeds to exactly one paste; without a visible exact
        # draft or a native user event it must still refuse any submit key.
        with LegacyTerminalFixture(provider='claude') as native, patch.object(
                bridge, 'read_screen', return_value=CLAUDE_IDLE):
            with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
                bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
        self.assertNotIn('RECEIVER_COMPACTING', str(caught.exception))
        native.send.assert_called_once()
        native.key.assert_not_called()

    def test_actual_claude_compaction_or_reconnect_blocks_all_input(self):
        for status in ('✻ Compacting context (23s)', '✽ Compacting conversation… (23s)',
                       '✢ Reconnecting... 1/10'):
            screen = self.claude_status(status)
            with self.subTest(status=status), LegacyTerminalFixture(provider='claude') as native, \
                    patch.object(bridge, 'read_screen', return_value=screen):
                self.assertTrue(bridge.receiver_cannot_submit_now(screen))
                with self.assertRaises(bridge.DispatchUnconfirmed):
                    bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
                native.send.assert_not_called()
                native.key.assert_not_called()

    def test_unknown_claude_footer_cannot_authorize_input(self):
        screen = CLAUDE_IDLE + '\nunknown current editor state'
        with LegacyTerminalFixture(provider='claude') as native, patch.object(
                bridge, 'read_screen', return_value=screen):
            self.assertEqual(bridge.receiver_input_kind(screen), 'UNKNOWN')
            with self.assertRaises(bridge.DispatchUnconfirmed):
                bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
        native.send.assert_not_called()
        native.key.assert_not_called()

    def test_steer_queue_does_not_hide_compaction_or_reconnect(self):
        steer = BEFORE.replace('• Queued follow-up inputs',
                               '• Press up to edit queued messages')
        for status in (COMPACTING, '• Reconnecting... 1/10'):
            screen = steer.replace(COMPACTING, status)
            with self.subTest(status=status), LegacyTerminalFixture() as native, patch.object(
                    bridge, 'read_screen', return_value=screen):
                self.assertTrue(bridge.receiver_cannot_submit_now(screen))
                with self.assertRaises(bridge.DispatchUnconfirmed):
                    bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
                native.send.assert_not_called()
                native.key.assert_not_called()
        self.assertFalse(bridge.receiver_cannot_submit_now(working(steer)))


class WarningFooterTests(unittest.TestCase):
    def test_standalone_warning_footer_allows_exact_draft_native_delivery(self):
        before = '› Ask Codex to do anything\nGPT-6 high\n' + WARN
        draft = '› ' + TEXT + '\nGPT-6 high\n' + WARN
        with LegacyTerminalFixture() as native, patch.object(bridge, 'read_screen', side_effect=
                native.ready_screens(before, draft)):
            native.key.side_effect = native.receipt_on_key(TEXT)
            result = bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
            native.send.assert_called_once()
            native.key.assert_called_once_with('peer', 'enter')
            self.assertTrue(result['confirmed'])

    def test_unknown_warning_hint_never_authorizes_input(self):
        for footer in ('⚠ 4 warnings · f3 to view', '⚠ 4 warnings · f2 to edit',
                       '⚠ 4 warnings · f2 to view\nunknown input'):
            with self.subTest(footer=footer), LegacyTerminalFixture() as native, patch.object(
                    bridge, 'read_screen', return_value='› \nGPT-6 high\n' + footer):
                with self.assertRaises(bridge.DispatchUnconfirmed):
                    bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
                native.send.assert_not_called()
                native.key.assert_not_called()

    def test_warning_like_user_draft_stays_occupied(self):
        for text in ('⚠ 4 warnings · f2 to view', TEXT + '\n  ⚠ 4 warnings · f2 to view'):
            screen = '› ' + text + '\nGPT-6 high\n' + WARN
            with self.subTest(text=text), LegacyTerminalFixture() as native, patch.object(
                    bridge, 'read_screen', return_value=screen):
                self.assertFalse(bridge.compose_block_is_empty(screen))
                with self.assertRaises(bridge.DispatchUnconfirmed):
                    bridge.submit_text('peer', TEXT, marker='LAYOUT1003')
                native.send.assert_not_called()
                native.key.assert_not_called()


if __name__ == '__main__':
    unittest.main()
