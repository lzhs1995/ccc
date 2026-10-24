import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cmux_hook_runtime_audit as audit


class RuntimeAuditTests(unittest.TestCase):
    def test_receipts_are_bounded_and_do_not_copy_command_or_environment(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Path, 'home', return_value=Path(directory)), \
                patch.object(audit, '_entrypoint', return_value='cmux_executor_closeout_guard.py'), \
                patch.object(audit, '_parent_chain', return_value=[]):
            payload = dict(session_id='11111111-1111-4111-8111-111111111111',
                           hook_event_name='PreToolUse', tool_input={'command': 'private-data'})
            for status in ('UNRESOLVED_TASK_SCOPE_ALLOWED', 'RESOLVED_TASK_SCOPE'):
                audit.record(payload, status)
            receipts = list(Path(directory).rglob('*.json'))
            self.assertEqual(len(receipts), 1)
            self.assertNotIn('private-data', receipts[0].read_text())
            self.assertEqual(json.loads(receipts[0].read_text())['scope_status'], 'RESOLVED_TASK_SCOPE')

    def test_offline_tests_and_invalid_sessions_do_not_create_live_evidence(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Path, 'home', return_value=Path(directory)):
            self.assertIsNone(audit._entrypoint())
            audit.record({'session_id': '../../foreign', 'hook_event_name': 'Stop'}, 'NO_ENROLLMENT')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_audit_failure_does_not_block_recovery(self):
        with patch.object(audit, '_entrypoint', side_effect=OSError('unavailable')):
            audit.record({}, 'UNRESOLVED_TASK_SCOPE_ALLOWED')
