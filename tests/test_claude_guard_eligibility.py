"""Ineligible legacy recovery does not repeat the observation's full guards."""
import tempfile
import unittest
from unittest.mock import Mock

import cmux_codex_watch as core
from tests.test_watch import FakeClient, armed_daemon, claude_grid_payload


class ClaudeGuardEligibilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = FakeClient(claude_grid_payload([]))
        self.daemon = armed_daemon(directory.name, self.client)
        self.addCleanup(self.daemon._delivery_store.close)
        self.runtime = core.TargetRuntime()
        self.provider = self.daemon._claude_provider_guard_reason = Mock(return_value='')
        self.wait = self.daemon._claude_supervisor_wait_guard = Mock(return_value=None)

    def test_working_without_legacy_latch_does_not_repeat_guards(self):
        self.assertFalse(self.daemon._recover_claude_report_stop('surface-uuid', self.runtime))
        self.provider.assert_not_called()
        self.wait.assert_not_called()

    def test_dispatch_without_handshake_does_not_repeat_provider_guard(self):
        state = core.ScreenState('claude_stopped', message_kind='claude')
        self.assertFalse(self.daemon._recover_claude_dispatch_wait('surface-uuid', self.runtime, state))
        self.provider.assert_not_called()

    def test_eligible_dispatch_still_checks_provider_before_recovery(self):
        self.runtime.claude_handshake_wait = True
        self.provider.return_value = 'claude_provider_blocked'
        state = core.ScreenState('claude_stopped', message_kind='claude')
        self.assertFalse(self.daemon._recover_claude_dispatch_wait('surface-uuid', self.runtime, state))
        self.provider.assert_called_once()


if __name__ == '__main__':
    unittest.main()
