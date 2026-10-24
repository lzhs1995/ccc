"""Task enrollment is session-local; ordinary tools never need a supervisor."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import cmux_hook_scope as scope
import cmux_hook_identity as identity
import cmux_consensus_stop_guard as stop
import cmux_executor_closeout_guard as closeout
import cmux_lease_guard as lease
import mac_harness as harness
import cmux_native_delivery as native

SESSION = '67d0a734-5613-4653-aa3b-e4f156b14830'
OTHER = '6d7bac83-48d3-410c-9aa8-0aa6ed282071'


class HookScopeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.active = self.root / 'active'
        for mod in (stop, lease):
            self.stack.enter_context(patch.object(mod, 'ACTIVE_DIR', self.active))
        self.resolve = self.stack.enter_context(patch.object(identity, 'resolve',
            side_effect=identity.workspace.WorkspaceScopeError('no live resumed client')))
        self.marker = dict(task_id='actual-task', workspace_uuid='task-workspace',
            artifact_root=str(self.root / 'task'), participants=[dict(
                role='executor', surface_uuid='task-surface', provider='codex',
                native_session_id=SESSION)])
        self.path = self.active / 'task-workspace' / 'task.json'
        self.path.parent.mkdir(parents=True)
        self.write_marker()

    def write_marker(self):
        self.path.write_text(json.dumps(self.marker))

    def main(self, mod, session=OTHER, **extra):
        payload = dict(session_id=session, hook_event_name='Stop' if mod is stop else 'PreToolUse',
                       tool_name='Bash', tool_input={'command': 'python -c "print(1)"'}, **extra)
        with patch.object(sys, 'argv', [mod.__file__]), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', io.StringIO()), \
                patch.object(sys, 'stderr', io.StringIO()) as err:
            rc = mod.main()
        return rc, err.getvalue()

    def test_new_codex_skips_all_task_guards_before_identity(self):
        for mod in (stop, closeout, lease):
            self.assertEqual(self.main(mod), (0, ''))
        self.resolve.assert_not_called()

    def test_new_claude_session_does_not_inherit_old_surface(self):
        self.marker['participants'][0]['provider'] = 'claude'
        self.write_marker()
        for mod in (stop, closeout, lease):
            self.assertEqual(self.main(mod), (0, ''))
        self.resolve.assert_not_called()

    def test_legacy_marker_cannot_enroll_native_conversation(self):
        del self.marker['participants'][0]['native_session_id']
        self.write_marker()
        for mod in (stop, closeout, lease):
            self.assertEqual(self.main(mod, session=SESSION), (0, ''))
        self.resolve.assert_not_called()

    def test_enrollment_without_live_identity_cannot_lock_tools_or_stop(self):
        for mod in (stop, closeout, lease):
            rc, err = self.main(mod, session=SESSION)
            self.assertEqual((rc, err), (0, ''))

    def test_unresolved_context_is_empty_without_leaking_or_swallowing_consumer_errors(self):
        before = self.path.read_bytes()
        with scope.evaluation({'session_id': SESSION}, [self.marker]) as selected:
            self.assertEqual(selected, [])
            self.assertEqual(scope.current(), [])
        self.assertIsNone(scope.current())
        with self.assertRaisesRegex(ValueError, 'consumer failure'):
            with scope.evaluation({'session_id': SESSION}, [self.marker]):
                raise ValueError('consumer failure')
        self.assertIsNone(scope.current())
        self.assertEqual(before, self.path.read_bytes())

    def test_unresolved_supervisor_keeps_all_repair_tools_available(self):
        self.marker['participants'][0]['role'] = 'supervisor'
        self.write_marker()
        for tool, args in (
            ('Bash', {'command': 'rtk cmux-agent self'}),
            ('exec_command', {'cmd': 'rtk rg caller scripts'}),
            ('apply_patch', {'patch': 'repair source'}),
            ('update_goal', {'status': 'blocked'}),
            ('collaboration.send_message', {'target': 'reviewer', 'message': 'review'}),
        ):
            for mod in (closeout, lease):
                with self.subTest(tool=tool, guard=mod.__name__):
                    payload = dict(session_id=SESSION, hook_event_name='PreToolUse',
                                   tool_name=tool, tool_input=args)
                    self.assertTrue(mod.evaluate(payload)[0])

    def test_same_workspace_nonparticipant_is_unrestricted(self):
        self.resolve.side_effect = None
        self.resolve.return_value = ('task-workspace', 'other-surface')
        for mod in (stop, closeout, lease):
            self.assertEqual(self.main(mod, session=SESSION), (0, ''))

    def test_foreign_workspace_cannot_impose_task(self):
        self.resolve.side_effect = None
        self.resolve.return_value = ('ordinary-workspace', 'task-surface')
        for mod in (stop, closeout, lease):
            self.assertEqual(self.main(mod, session=SESSION), (0, ''))

    def test_enrolled_executor_missing_callback_still_blocked(self):
        self.resolve.side_effect = None
        self.resolve.return_value = ('task-workspace', 'task-surface')
        task = self.root / 'task'
        task.mkdir()
        (task / 'task-pack.json').write_text(json.dumps(dict(draft=False,
            report=str(task / 'executor-report.md'), completion_receipt=str(task / 'receipt.json'))))
        rc, err = self.main(stop, session=SESSION)
        self.assertEqual(rc, 2)
        self.assertIn('completion callback receipt missing', err)

    def test_task_markers_and_reports_are_not_modified(self):
        before = self.path.read_bytes()
        for mod in (stop, closeout, lease):
            self.main(mod)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_payload_session_does_not_inherit_pinned_task(self):
        self.assertEqual(scope.participants(self.marker, {}), [])

    def test_evaluation_context_does_not_leak_between_calls(self):
        self.resolve.side_effect = None
        self.resolve.return_value = ('task-workspace', 'task-surface')
        with scope.evaluation({'session_id': SESSION}, [self.marker]) as selected:
            self.assertEqual(selected, [self.marker])
        self.assertIsNone(scope.current())
        with scope.evaluation({'session_id': OTHER}, [self.marker]) as selected:
            self.assertEqual(selected, [])
        self.assertIsNone(scope.current())

    def test_directory_is_scope_only_not_repaired_identity_evidence(self):
        del self.marker['workspace_uuid']
        self.write_marker()
        m = stop._scope_candidates()[0]
        self.assertNotIn('workspace_uuid', m)
        self.assertEqual(m['_scope_workspace'], 'task-workspace')

    def test_arm_task_persists_native_session_enrollment(self):
        peers = [dict(role='supervisor', provider='codex', surface_uuid='supervisor', native_session_id=SESSION),
                 dict(role='executor', provider='claude', surface_uuid='executor', native_session_id=OTHER)]
        with patch.object(harness, '_ACTIVE_DIR', self.active), \
                patch.object(harness, '_workspace_key', return_value='task-workspace'):
            marker = harness.arm_task('new-task', self.root / 'new', supervisor_provider='codex',
                supervisor_surface_uuid='supervisor', workspace_uuid='task-workspace',
                executors=[dict(provider='claude', surface_uuid='executor')], native_participants=peers)
        self.assertEqual([p['native_session_id'] for p in marker['participants']], [SESSION, OTHER])

    def bind(self, workspace='task-workspace', provider='codex', changed=False):
        process = dict(pid=431, birth=[1234, 0], executable='/opt/codex')
        with patch.object(native, '_tree_row', return_value=dict(workspace_uuid=workspace)), \
                patch.object(native, '_target_process', return_value=process), \
                patch.object(native, '_provider', return_value=provider), \
                patch.object(native, '_live_session', return_value=SESSION), \
                patch.object(native.identity, 'process', return_value={} if changed else process):
            return scope.bind_participants(None, 'task-workspace', [dict(
                role='supervisor', provider='codex', surface_uuid='task-surface')])

    def test_binding_reads_designated_current_session(self):
        self.assertEqual(self.bind()[0]['native_session_id'], SESSION)

    def test_binding_refuses_other_workspace(self):
        with self.assertRaisesRegex(native.NativeDeliveryError, 'WORKSPACE_MISMATCH'):
            self.bind(workspace='other-workspace')

    def test_binding_refuses_provider_replacement(self):
        with self.assertRaisesRegex(native.NativeDeliveryError, 'PROVIDER_MISMATCH'):
            self.bind(provider='claude')

    def test_binding_refuses_process_replacement(self):
        with self.assertRaisesRegex(native.NativeDeliveryError, 'PROCESS_CHANGED'):
            self.bind(changed=True)


if __name__ == '__main__':
    unittest.main()
