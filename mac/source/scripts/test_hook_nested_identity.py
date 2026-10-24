"""Hook -> bridge -> workspace integration with the actual identity resolver.

Only kernel collection and cmux reads are fixtures. In particular, neither
caller_snapshot nor pin_workspace is mocked: the managed-daemon regression
occurred after the outer hook had already resolved the correct caller.
"""
from contextlib import ExitStack
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_hook_identity as hook
import cmux_workspace_guard as guard
import test_cmux_daemon_identity as fixture


class NestedHookIdentityTests(unittest.TestCase):
    def setUp(self):
        fixture.DaemonTests.setUp(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(guard, 'SCOPE_DIR', root))
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(patch.dict(bridge._workspace_pins, {}, clear=True))
        self.default = self.stack.enter_context(patch.object(
            guard.daemon_identity, 'collect', return_value=None))
        self.collect = self.stack.enter_context(patch.object(
            guard.daemon_identity, 'collect_hook', side_effect=lambda env, payload: copy.deepcopy(self.proof)))
        self.read = self.stack.enter_context(patch.object(
            guard, '_read_json_command', side_effect=self.cmux_read))
        self.payload = dict(hook_event_name='PostToolUse', session_id=fixture.S)

    def cmux_read(self, *args):
        if args == ('identify', '--json'):
            return copy.deepcopy(self.identity)
        if args == ('tree', '--all', '--json', '--id-format', 'both'):
            return copy.deepcopy(self.tree)
        raise AssertionError(args)

    def test_nested_bridge_binds_native_peer_and_recollects_each_time(self):
        with hook.evaluation(self.payload):
            self.assertEqual(hook.identity(self.payload), (fixture.W, fixture.C))
            for _ in range(2):
                bound = bridge.pin_workspace(fixture.T)
                self.assertEqual(bound['workspace_uuid'], fixture.W)
                self.assertEqual(bound['caller_surface_uuid'], fixture.C)
        self.assertEqual(self.collect.call_count, 9)
        self.default.assert_not_called()
        self.assertEqual(dict(os.environ), self.env)

    def test_nested_evaluation_restores_original_payload_after_exception(self):
        inner = dict(self.payload, session_id=fixture.T)
        with hook.evaluation(self.payload):
            try:
                with hook.evaluation(inner):
                    bridge.pin_workspace(fixture.T)
                    self.assertIs(self.collect.call_args.args[1], inner)
                    raise RuntimeError('inner failure')
            except RuntimeError:
                pass
            bridge.pin_workspace(fixture.T)
            self.assertIs(self.collect.call_args.args[1], self.payload)
        # The context must not leak into later ordinary command evaluations.
        with self.assertRaises(guard.WorkspaceScopeError):
            bridge.pin_workspace(fixture.T)
        self.default.assert_called_once()

    def test_outer_resolution_failure_restores_collection_context(self):
        self.collect.side_effect = guard.daemon_identity.IdentityError('unavailable')
        with self.assertRaises(guard.daemon_identity.IdentityError):
            with hook.evaluation(self.payload):
                self.fail('resolution should fail before yielding')
        with self.assertRaises(guard.WorkspaceScopeError):
            bridge.pin_workspace(fixture.T)
        self.default.assert_called_once()

    def test_nested_cross_workspace_target_still_denied(self):
        with hook.evaluation(self.payload):
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'differ'):
                bridge.pin_workspace(fixture.D)
        self.assertNotIn(fixture.D, bridge._workspace_pins)

    def test_nested_process_drift_at_each_boundary_still_denied(self):
        for boundary in (1, 2):
            with self.subTest(boundary=boundary), hook.evaluation(self.payload):
                changed = copy.deepcopy(self.proof)
                changed['client']['birth'][0] += 1
                self.collect.side_effect = [copy.deepcopy(self.proof)] * boundary + [changed]
                with self.assertRaisesRegex(guard.WorkspaceScopeError, 'changed'):
                    bridge.pin_workspace(fixture.T)
                self.collect.side_effect = lambda env, payload: copy.deepcopy(self.proof)
        self.assertNotIn(fixture.T, bridge._workspace_pins)

    def test_nested_tree_drift_still_denied(self):
        changed = copy.deepcopy(self.tree)
        changed['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys99'
        with hook.evaluation(self.payload):
            self.read.side_effect = [self.identity, self.tree, self.identity, changed]
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'TTY'):
                bridge.pin_workspace(fixture.T)
        self.assertNotIn(fixture.T, bridge._workspace_pins)

    def test_explicit_collection_takes_precedence_and_does_not_leak(self):
        with hook.evaluation(self.payload):
            self.assertIsNone(guard.caller_snapshot(collector=lambda: None)[3])
            self.assertEqual(bridge.pin_workspace(fixture.T)['caller_surface_uuid'], fixture.C)


if __name__ == '__main__':
    unittest.main()
