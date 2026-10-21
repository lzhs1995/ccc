"""Exercise real guard entrypoints with private markers and lease files."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

import cmux_hook_identity as identity
import cmux_consensus_stop_guard as stop
import cmux_executor_closeout_guard as closeout
import cmux_lease_guard as lease


class HookMarkerJurisdictionTests(unittest.TestCase):
    session = '4ec8ad00-956d-4d00-b302-64927c25ce3f'

    def peers(self):
        return [dict(role='supervisor', surface_uuid='actual-surface', native_session_id=self.session)]

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.active = self.root / 'active'
        self.active.mkdir()
        self.registry = self.root / 'registry'
        self.registry.mkdir()
        for mod in (stop, lease):
            self.stack.enter_context(patch.object(mod, 'ACTIVE_DIR', self.active))
        self.stack.enter_context(patch.object(lease, 'REGISTRY_DIR', self.registry))
        self.stack.enter_context(patch.dict(os.environ, {
            'CMUX_WORKSPACE_ID': 'inherited', 'CMUX_SURFACE_ID': 'inherited-surface',
            'CMUX_AGENT_ROLE': 'supervisor'}))
        self.resolve = self.stack.enter_context(patch.object(identity, 'resolve',
            side_effect=identity.workspace.WorkspaceScopeError('test caller unavailable')))
        self.target = self.root / 'source.py'
        self.target.write_text('original\n')
        self.payloads = (
            (stop, dict(hook_event_name='Stop', final_message='Report pending')),
            (closeout, dict(hook_event_name='PreToolUse', tool_name='Bash')),
            (lease, dict(tool_name='Edit', tool_input={'file_path': str(self.target)})),
        )

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def marker(self, workspace='actual', *, v2=False, **extra):
        path = (self.active / workspace / 'task.json' if v2
                else self.active / (workspace + '.json'))
        extra.setdefault('participants', self.peers())
        self.write(path, dict(task_id='task', artifact_root=str(self.root / 'task'), **extra))
        return path

    def expired(self):
        return dict(armed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                    ttl_seconds=60)

    def lease_file(self, root, *, holder='executor', stale=False):
        self.write(root / 'leases' / 'lease.json', dict(
            owner_role=holder, mode='exclusive', paths=[str(self.target)],
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=-1 if stale else 1)).isoformat()))

    def explicit(self, root):
        return dict(tool_name='Bash', tool_input={'command':
            'touch ' + shlex.quote(str(self.target)) + ' --artifact-root ' + shlex.quote(str(root))})

    def resolved(self):
        self.resolve.side_effect = None
        self.resolve.return_value = ('actual', 'actual-surface')

    def main(self, mod, payload):
        payload = dict(payload, session_id=self.session)
        with patch.object(sys, 'argv', [mod.__file__]), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(sys, 'stdout', io.StringIO()), \
                patch.object(sys, 'stderr', io.StringIO()) as err:
            rc = mod.main()
        return rc, err.getvalue()

    def test_empty_or_absent_active_directory_skips_discovery_in_all_mains(self):
        for absent in (False, True):
            if absent:
                self.active.rmdir()
            for mod, payload in self.payloads:
                with self.subTest(absent=absent, guard=mod.__name__):
                    self.assertEqual(self.main(mod, payload), (0, ''))
        self.resolve.assert_not_called()

    def test_nonmarkers_and_hidden_staging_files_skip_discovery(self):
        self.write(self.active / 'list.json', [])
        (self.active / 'broken.json').write_text('{')
        self.write(self.active / 'actual' / '.staged.json', {'task_id': 'staged'})
        self.write(self.active / 'actual' / 'too-deep' / 'task.json', {'task_id': 'deep'})
        for mod, payload in self.payloads:
            self.assertEqual(self.main(mod, payload), (0, ''))
        self.resolve.assert_not_called()

    def test_unrelated_v1_marker_does_not_require_discovery(self):
        self.marker(workspace='not-inherited', participants=[dict(
            role='executor', surface_uuid='elsewhere', native_session_id='other-session')])
        for mod, payload in self.payloads:
            with self.subTest(guard=mod.__name__):
                rc, err = self.main(mod, payload)
                self.assertEqual((rc, err), (0, ''))
        self.resolve.assert_not_called()

    def test_unrelated_v2_marker_does_not_require_discovery(self):
        self.marker(workspace='not-inherited', v2=True, participants=[dict(
            role='executor', surface_uuid='elsewhere', native_session_id='other-session')])
        for mod, payload in self.payloads:
            with self.subTest(guard=mod.__name__):
                rc, err = self.main(mod, payload)
                self.assertEqual((rc, err), (0, ''))
        self.resolve.assert_not_called()

    def test_expired_markers_do_not_enroll_any_native_session(self):
        for v2 in (False, True):
            path = self.marker(v2=v2, **self.expired())
            for mod, payload in self.payloads:
                self.assertEqual(self.main(mod, payload), (0, ''))
            self.resolve.assert_not_called()
            path.unlink()

    def test_stop_identity_error_does_not_accuse_consensus_or_suggest_disarm(self):
        self.marker()
        rc, err = self.main(*self.payloads[0])
        self.assertEqual((rc, err), (0, ''))
        self.assertNotIn('claims multi-agent', err)
        self.assertNotIn('consensus', err)
        self.assertNotIn('disarm', err)
        self.assertNotIn('record-round', err)

    def test_stop_reentry_only_discovers_identity_for_waiting_output(self):
        self.marker()
        # 重入 verdict 保留放行；原生等待出口只在 Stop 上核身份，错误不伪造等待。
        self.assertEqual(self.main(stop, dict(
            hook_event_name='Stop', stop_hook_active=True)), (0, ''))
        self.resolve.assert_called_once()
        self.resolve.reset_mock()
        self.assertEqual(self.main(stop, dict(
            hook_event_name='SubagentStop', stop_hook_active=True)), (0, ''))
        self.resolve.assert_not_called()

    def test_explicit_root_alone_enforces_conflict_without_discovery(self):
        root = self.root / 'explicit'
        self.lease_file(root)
        rc, err = self.main(lease, self.explicit(root))
        self.assertEqual(rc, 2)
        self.assertIn('LEASE_CONFLICT', err)
        self.resolve.assert_not_called()
        self.assertEqual(self.target.read_text(), 'original\n')

    def test_explicit_root_alone_allows_own_live_lease_without_discovery(self):
        root = self.root / 'explicit'
        self.lease_file(root, holder='supervisor')
        self.assertEqual(self.main(lease, self.explicit(root)), (0, ''))
        self.resolve.assert_not_called()

    def test_explicit_root_alone_rejects_stale_lease_without_discovery(self):
        root = self.root / 'explicit'
        self.lease_file(root, stale=True)
        rc, err = self.main(lease, self.explicit(root))
        self.assertEqual(rc, 2)
        self.assertIn('LEASE_STALE', err)
        self.resolve.assert_not_called()

    def test_invalid_explicit_roots_without_markers_still_refused(self):
        for command in ('touch /tmp/file --artifact-root relative',
                        'touch /tmp/file --artifact-root'):
            rc, err = self.main(lease, dict(tool_name='Bash', tool_input={'command': command}))
            self.assertEqual(rc, 2)
            self.assertIn('ARTIFACT_ROOT_NOT_ABSOLUTE', err)
        self.resolve.assert_not_called()

    def test_explicit_root_ignores_unrelated_marker_but_checks_lease(self):
        self.marker(workspace='not-inherited', v2=True, participants=[])
        root = self.root / 'explicit'
        self.lease_file(root)
        rc, err = self.main(lease, self.explicit(root))
        self.assertEqual(rc, 2)
        self.assertIn('LEASE_CONFLICT', err)
        self.resolve.assert_not_called()

    def test_explicit_root_cannot_hide_second_resolved_workspace_lease(self):
        self.resolved()
        self.marker(v2=True)
        second = self.root / 'second'
        self.write(self.active / 'actual' / 'second.json', dict(artifact_root=str(second), participants=self.peers()))
        self.lease_file(second)
        self.lease_file(self.root / 'foreign')
        self.write(self.active / 'inherited.json', dict(artifact_root=str(self.root / 'foreign')))
        rc, err = self.main(lease, self.explicit(self.root / 'explicit'))
        self.assertEqual(rc, 2)
        self.assertIn('LEASE_CONFLICT', err)
        self.resolve.assert_called_once()

    def test_resolved_workspace_ignores_foreign_lease(self):
        self.resolved()
        self.marker(workspace='inherited')
        self.lease_file(self.root / 'task')
        self.assertEqual(self.main(lease, self.explicit(self.root / 'explicit')), (0, ''))
        self.resolve.assert_called_once()

    def test_expired_marker_does_not_override_explicit_root(self):
        self.resolved()
        self.marker(v2=True, **self.expired())
        self.lease_file(self.root / 'task')
        self.assertEqual(self.main(lease, self.explicit(self.root / 'explicit')), (0, ''))
        self.resolve.assert_not_called()
        rc, err = self.main(lease, self.explicit(self.root / 'task'))
        self.assertEqual(rc, 2)
        self.assertIn('LEASE_CONFLICT', err)

    def test_valid_explicit_root_cannot_hide_invalid_marker_root(self):
        self.resolved()
        self.write(self.active / 'actual.json', dict(artifact_root='relative', participants=self.peers()))
        rc, err = self.main(lease, self.explicit(self.root / 'explicit'))
        self.assertEqual(rc, 2)
        self.assertIn('ARTIFACT_ROOT_NOT_ABSOLUTE', err)

    def test_invalid_explicit_root_cannot_hide_behind_valid_marker(self):
        self.resolved()
        self.marker()
        rc, err = self.main(lease, self.explicit('relative'))
        self.assertEqual(rc, 2)
        self.assertIn('ARTIFACT_ROOT_NOT_ABSOLUTE', err)

    def test_marker_registry_fallback_is_enforced_with_explicit_root(self):
        self.resolved()
        self.write(self.active / 'actual.json', dict(task_id='registered', participants=self.peers()))
        root = self.root / 'registered'
        self.write(self.registry / 'registered.json', dict(artifact_root=str(root)))
        self.lease_file(root)
        rc, err = self.main(lease, self.explicit(self.root / 'explicit'))
        self.assertEqual(rc, 2)
        self.assertIn('LEASE_CONFLICT', err)

    def test_missing_marker_registry_root_is_not_hidden_by_explicit_root(self):
        self.resolved()
        self.write(self.active / 'actual.json', dict(task_id='unregistered', participants=self.peers()))
        rc, err = self.main(lease, self.explicit(self.root / 'explicit'))
        self.assertEqual(rc, 2)
        self.assertIn('no registered artifact_root', err)


if __name__ == '__main__':
    unittest.main()
