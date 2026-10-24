"""Real marker/lease/journal boundaries under a managed caller's foreign env."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cmux_hook_identity as identity
import cmux_consensus_stop_guard as stop
import cmux_executor_closeout_guard as closeout
import cmux_lease_guard as lease
import test_executor_closeout as closeout_tests


class HookIdentityTests(unittest.TestCase):
    session = '4ec8ad00-956d-4d00-b302-64927c25ce3f'

    def enroll_native(self):
        for row in self.marker['participants']:
            row['native_session_id'] = self.session
        self.write(self.active / 'native.json', self.marker)
        self.payload['session_id'] = self.session

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.active = self.root / 'active'
        self.active.mkdir()
        for mod in (stop, lease):
            self.stack.enter_context(patch.object(mod, 'ACTIVE_DIR', self.active))
        self.stack.enter_context(patch.dict(os.environ, {
            'CMUX_WORKSPACE_ID': 'daemon', 'CMUX_SURFACE_ID': 'daemon-surface',
            'CMUX_AGENT_ROLE': 'supervisor'}))
        self.collect = self.stack.enter_context(patch.object(
            identity.workspace.daemon_identity, 'collect_hook', return_value={'client': {}}))
        self.snapshot = self.stack.enter_context(patch.object(
            identity.workspace, 'caller_snapshot', return_value=({}, {}, {
                'CMUX_WORKSPACE_ID': 'native', 'CMUX_SURFACE_ID': 'native-surface'}, {})))
        self.task = self.root / 'task'
        self.task.mkdir()
        self.marker = dict(task_id='task', artifact_root=str(self.task), participants=[
            dict(role='executor', surface_uuid='native-surface')])
        self.write(self.active / 'native.json', self.marker)
        self.write(self.task / 'task-pack.json', dict(draft=False,
                   report=str(self.task / 'executor-report.md'),
                   completion_receipt=str(self.task / 'receipt.json')))
        self.payload = dict(hook_event_name='Stop', final_message='Report pending')

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def test_stop_reads_native_workspace_and_executor_together(self):
        ok, msg = stop.evaluate(self.payload)
        self.assertFalse(ok)
        self.assertIn('completion callback receipt missing', msg)
        self.assertEqual(self.snapshot.call_count, 1)
        self.assertEqual(os.environ['CMUX_WORKSPACE_ID'], 'daemon')

    def test_foreign_workspace_marker_cannot_block_native_supervisor(self):
        (self.active / 'native.json').unlink()
        self.write(self.active / 'daemon.json', self.marker)
        self.assertTrue(stop.evaluate(dict(final_message='consensus-validation PASS'))[0])

    def test_native_supervisor_false_claim_is_checked(self):
        self.marker['participants'] = [dict(role='supervisor', surface_uuid='native-surface')]
        self.write(self.active / 'native.json', self.marker)
        self.assertFalse(stop.evaluate(dict(final_message='consensus-validation PASS'))[0])

    def test_failed_resolution_skips_task_scope_without_falling_back(self):
        self.enroll_native()
        self.snapshot.side_effect = identity.workspace.WorkspaceScopeError('drift')
        before = {p: p.read_bytes() for p in self.root.rglob('*.json')}
        for mod, payload in ((stop, self.payload), (closeout, dict(hook_event_name='PreToolUse')),
                             (lease, dict(tool_name='Edit', tool_input={'file_path': '/tmp/test'}))):
            ok, msg = mod.evaluate(dict(payload, session_id=self.session))
            self.assertTrue(ok, msg)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*.json')})

    def test_incomplete_resolved_identity_cannot_fall_back(self):
        self.enroll_native()
        self.snapshot.return_value = ({}, {}, {'CMUX_WORKSPACE_ID': 'native'}, {})
        self.assertTrue(stop.evaluate(self.payload)[0])

    def test_managed_caller_cannot_downgrade_to_inherited_environment(self):
        self.enroll_native()
        self.snapshot.return_value = ({}, {}, dict(os.environ), None)
        ok, message = stop.evaluate(self.payload)
        self.assertTrue(ok, message)

    def test_discovery_timeout_does_not_trap_stop_and_recovery_restores_scope(self):
        self.enroll_native()
        self.collect.side_effect = subprocess.TimeoutExpired('ps', 5)
        self.assertTrue(stop.evaluate(self.payload)[0])
        self.collect.side_effect = None
        self.assertFalse(stop.evaluate(self.payload)[0])

    def test_stop_reentry_does_not_resolve_or_mutate(self):
        self.collect.side_effect = AssertionError('must not discover on reentry')
        for event in ('Stop', 'SubagentStop'):
            self.assertTrue(stop.evaluate(dict(hook_event_name=event, stop_hook_active=True))[0])

    def test_resolution_is_scoped_not_cached_between_calls(self):
        self.assertFalse(stop.evaluate(self.payload)[0])
        self.snapshot.return_value = ({}, {}, {'CMUX_WORKSPACE_ID': 'next',
                                              'CMUX_SURFACE_ID': 'next-surface'}, {})
        self.assertTrue(stop.evaluate(self.payload)[0])
        self.assertEqual(self.snapshot.call_count, 2)

    def test_ordinary_client_keeps_existing_binding(self):
        self.collect.return_value = None
        self.write(self.active / 'daemon.json', dict(self.marker, participants=[
            dict(role='executor', surface_uuid='daemon-surface')]))
        self.assertFalse(stop.evaluate(self.payload)[0])
        self.snapshot.assert_not_called()

    def test_native_lease_is_enforced_and_foreign_lease_ignored(self):
        target = self.task / 'source.py'
        target.write_text('original')
        (self.task / 'leases').mkdir()
        self.write(self.task / 'leases' / 'executor.json', dict(
            owner_role='executor', paths=[str(target)], mode='exclusive',
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
        payload = dict(tool_name='Edit', tool_input={'file_path': str(target)})
        ok, msg = lease.evaluate(payload)
        self.assertFalse(ok)
        self.assertIn('LEASE_CONFLICT', msg)
        (self.active / 'native.json').rename(self.active / 'daemon.json')
        self.assertTrue(lease.evaluate(payload)[0])
        self.assertEqual(target.read_text(), 'original')

    def test_read_only_lease_check_does_not_need_discovery(self):
        self.collect.side_effect = AssertionError('read-only')
        self.assertTrue(lease.evaluate(dict(tool_name='Read', tool_input={'file_path': '/tmp/test'}))[0])

    def test_real_stop_main_refuses_missing_callback(self):
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(self.payload))), \
                patch.object(sys, 'argv', ['cmux_consensus_stop_guard.py']), \
                patch.object(sys, 'stderr', io.StringIO()) as err:
            self.assertEqual(stop.main(), 2)
            self.assertIn('completion callback receipt missing', err.getvalue())

    def test_real_closeout_journal_uses_native_identity(self):
        fixture = closeout_tests.CloseoutTests('test_terminal_unknown_seals_tools_allows_honest_stop_without_writing')
        fixture.setUp()
        try:
            self.snapshot.return_value = ({}, {}, {'CMUX_WORKSPACE_ID': fixture.workspace,
                                                  'CMUX_SURFACE_ID': fixture.surface}, {})
            self.write(self.active / (fixture.workspace + '.json'), fixture.marker)
            before = {p: p.read_bytes() for p in fixture.root.rglob('*') if p.is_file()}
            self.assertFalse(closeout.evaluate(dict(hook_event_name='PreToolUse'))[0])
            self.assertTrue(stop.evaluate(dict(hook_event_name='Stop', final_message=fixture.line))[0])
            self.assertFalse(fixture.receipt.exists())
            self.assertEqual(before, {p: p.read_bytes() for p in fixture.root.rglob('*') if p.is_file()})
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
