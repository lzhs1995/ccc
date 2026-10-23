"""Read-only reconciliation of durable callback attempts; shared cases live in test_callback_journal."""
import unittest
from unittest.mock import patch
import cmux_bridge as b
import test_callback_journal as journal_tests

class CallbackObservationTests(unittest.TestCase):
    setUp = journal_tests.JournalTests.setUp
    call = journal_tests.JournalTests.call
    queued = journal_tests.JournalTests.queued
    confirmed_screen = journal_tests.JournalTests.confirmed_screen

    def test_pack_change_denies_without_input(self):
        self.queued()
        self.packpath.write_text('{"changed":true}')
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.TaskPackContractError, 'BINDING_CHANGED'):
                self.call(reconcile_only=True)
            send.assert_not_called()
            key.assert_not_called()

    def test_moved_peer_denies_without_input(self):
        self.queued()
        self.proof['target_pane_uuid'] = 'OTHER_PANE'
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.TaskPackContractError, 'BINDING_CHANGED'):
                self.call(reconcile_only=True)
            send.assert_not_called()
            key.assert_not_called()

    def test_hint_truncation_and_unknown_suffix(self):
        head = '› Ask Codex to do anything\nGPT-6 high\n'
        for suffix in ['', ' ⚠', ' ⚠ 5 warnings', ' ⚠ 5 warnings · f2 to view']:
            self.assertEqual(b.receiver_input_kind(head + '? for shortcuts' + suffix), 'AGENT_TUI')
        for suffix in [' arbitrary command', ' ⚠ 5 warnings · f3 to view']:
            self.assertEqual(b.receiver_input_kind(head + '? for shortcuts' + suffix), 'UNKNOWN')
