import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import cmux_workspace_guard as guard
import cmux_bridge as bridge
import mac_harness as harness
from types import SimpleNamespace

W = '00000000-0000-4000-8000-000000000001'
X = '00000000-0000-4000-8000-000000000002'
C = '00000000-0000-4000-8000-000000000003'
T = '00000000-0000-4000-8000-000000000004'
P = '00000000-0000-4000-8000-000000000005'
Q = '00000000-0000-4000-8000-000000000006'


def snapshots():
    identity = {'caller': {'surface_ref': 'surface:1', 'workspace_ref': 'workspace:1',
                           'pane_ref': 'pane:1'}, 'focused': {'surface_ref': 'surface:999'}}
    tree = {'windows': [{'workspaces': [{'id': W, 'ref': 'workspace:1', 'panes': [
        {'id': P, 'ref': 'pane:1', 'surfaces': [{'id': C, 'ref': 'surface:1', 'type': 'terminal'}]},
        {'id': Q, 'ref': 'pane:2', 'surfaces': [{'id': T, 'ref': 'surface:2', 'type': 'terminal'}]},
    ]}]}]}
    return identity, tree


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.identity, self.tree = snapshots()
        self.workspace = self.tree['windows'][0]['workspaces'][0]
        bridge._workspace_pins.clear()

    def resolve(self, **kw):
        return guard.resolve_snapshot(self.identity, self.tree, 'surface:2', **kw)

    def test_same_workspace(self):
        self.assertEqual(self.resolve()['target_surface_uuid'], T)

    def test_uuid_selector(self):
        self.assertEqual(guard.resolve_snapshot(self.identity, self.tree, T)['workspace_uuid'], W)

    def test_focus_is_not_caller(self):
        self.identity.pop('caller')
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'caller missing'):
            self.resolve()

    def test_cross_workspace(self):
        pane = self.workspace['panes'].pop()
        self.tree['windows'][0]['workspaces'].append({'id': X, 'ref': 'workspace:2', 'panes': [pane]})
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'differ'):
            self.resolve()

    def test_missing_uuid(self):
        for node, key in [(self.workspace, 'id'), (self.workspace['panes'][1], 'id'),
                          (self.workspace['panes'][1]['surfaces'][0], 'id')]:
            old = node[key]
            node[key] = ''
            with self.assertRaises(guard.WorkspaceScopeError):
                self.resolve()
            node[key] = old

    def test_environment_is_not_identity(self):
        for env in [{'CMUX_WORKSPACE_ID': X}, {'CMUX_SURFACE_ID': T}]:
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'environment'):
                self.resolve(env=env)

    def test_stale_ref_reassignment(self):
        self.workspace['panes'][1]['surfaces'][0]['id'] = X
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'changed'):
            self.resolve(expected={'target_surface_uuid': T})

    def test_designated_workspace(self):
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'changed'):
            self.resolve(expected={'workspace_uuid': X})

    def test_same_pane_uuid_cannot_name_different_pane_refs(self):
        self.workspace['panes'][1]['id'] = P
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'pane reference/UUID'):
            self.resolve()

    def test_distinct_terminal_tabs_in_one_pane_are_supported(self):
        peer = self.workspace['panes'].pop()['surfaces'][0]
        self.workspace['panes'][0]['surfaces'].append(peer)
        proof = self.resolve()
        self.assertEqual(proof['caller_pane_uuid'], proof['target_pane_uuid'])
        self.assertNotEqual(proof['caller_surface_uuid'], proof['target_surface_uuid'])

    def test_same_pane_ref_cannot_name_different_uuids(self):
        self.workspace['panes'][1]['ref'] = 'pane:1'
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'pane reference/UUID'):
            self.resolve()

    def test_self(self):
        with self.assertRaises(guard.WorkspaceScopeError):
            guard.resolve_snapshot(self.identity, self.tree, C)

    def test_missing_target(self):
        with self.assertRaises(guard.WorkspaceScopeError):
            guard.resolve_snapshot(self.identity, self.tree, 'surface:999')

    def test_browser_target(self):
        self.workspace['panes'][1]['surfaces'][0]['type'] = 'browser'
        with self.assertRaises(guard.WorkspaceScopeError):
            self.resolve()

    def test_caller_tree_disagreement(self):
        self.identity['caller']['workspace_ref'] = 'workspace:99'
        with self.assertRaises(guard.WorkspaceScopeError):
            self.resolve()

    def test_duplicate_target(self):
        self.workspace['panes'][1]['surfaces'].append(copy.deepcopy(self.workspace['panes'][1]['surfaces'][0]))
        with self.assertRaises(guard.WorkspaceScopeError):
            self.resolve()

    def test_persistent_designation(self):
        for scope in [
            {'version': 1, 'caller_surface_uuid': C, 'workspace_uuid': X, 'target_surface_uuids': [T]},
            {'version': 1, 'caller_surface_uuid': C, 'workspace_uuid': W, 'target_surface_uuids': [X]},
            {'version': 1, 'caller_surface_uuid': C, 'workspace_uuid': W, 'target_surface_uuids': []},
        ]:
            with tempfile.TemporaryDirectory() as tmp, patch.object(guard, 'SCOPE_DIR', Path(tmp)), \
                    patch.object(guard, '_read_json_command', side_effect=[self.identity, self.tree]), \
                    patch.dict(os.environ, {}, clear=True):
                (Path(tmp) / (C + '.json')).write_text(json.dumps(scope))
                with self.assertRaises(guard.WorkspaceScopeError):
                    guard.require_same_workspace('surface:2')

    def test_runtime_rejects_before_send(self):
        with patch.object(bridge, 'require_same_workspace', side_effect=guard.WorkspaceScopeError('DENIED')), \
                patch.object(bridge.subprocess, 'run') as run:
            for fn, value in [(bridge.send_text, 'handshake'), (bridge.send_key, 'enter')]:
                with self.assertRaises(guard.WorkspaceScopeError):
                    fn('surface:2', value)
            run.assert_not_called()

    def test_runtime_binds_both_uuids(self):
        proof = self.resolve()
        with patch.object(bridge, 'require_same_workspace', return_value=proof), \
                patch.object(bridge.subprocess, 'run', return_value=Mock(returncode=0, stdout='ok')) as run:
            bridge.send_text('surface:2', 'handshake')
            args = run.call_args.args[0]
            self.assertEqual(args[1:3], ['rpc', 'terminal.paste'])
            self.assertEqual(json.loads(args[3]), dict(
                text='handshake', submit_key='none', workspace_id=W, surface_id=T))

    def test_runtime_rechecks_between_paste_and_enter(self):
        with patch.object(bridge, 'require_same_workspace', side_effect=[self.resolve(), self.resolve(), guard.WorkspaceScopeError('moved')]) as check, \
                patch.object(bridge.subprocess, 'run', return_value=Mock(returncode=0, stdout='ok')) as run:
            bridge.send_text('surface:2', 'handshake')
            with self.assertRaises(guard.WorkspaceScopeError):
                bridge.send_key('surface:2', 'enter')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(check.call_args.kwargs['expected']['target_surface_uuid'], T)

    def test_literal_paste_preserves_escapes_tabs_spaces_and_unicode(self):
        payload = '中文  ' + r'\n\r\t' + '\n\tblank follows\n\n tail '
        with patch.object(bridge, 'require_same_workspace', return_value=self.resolve()), \
                patch.object(bridge.subprocess, 'run', return_value=Mock(returncode=0, stdout='ok')) as run:
            bridge.send_text('surface:2', payload)
            self.assertEqual(run.call_count, 1)
            argv = run.call_args.args[0]
            self.assertEqual(argv[1:3], ['rpc', 'terminal.paste'])
            self.assertEqual(json.loads(argv[3])['text'], payload)
            self.assertEqual(json.loads(argv[3])['submit_key'], 'none')

    def test_paste_rpc_failure_never_falls_back_or_submits(self):
        with patch.object(bridge, 'require_same_workspace', return_value=self.resolve()), \
                patch.object(bridge.subprocess, 'run', return_value=Mock(returncode=1, stdout='', stderr='unsupported RPC')) as run:
            with self.assertRaisesRegex(RuntimeError, 'unsupported RPC'):
                bridge.send_text('surface:2', r'original\ntext')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1:3], ['rpc', 'terminal.paste'])

    def test_paste_parameters_cannot_submit_or_override_workspace(self):
        good = dict(text='original', submit_key='none', workspace_id=W, surface_id=T)
        bad = [dict(good, submit_key=k) for k in ('enter', 'tab', '', None)]
        bad += [dict(good, workspace_id=X), dict(good, unexpected=True),
                dict(good, text=''), dict(good, text=True), []]
        with patch.object(bridge, 'require_same_workspace', return_value=self.resolve()), \
                patch.object(bridge.subprocess, 'run') as run:
            for params in bad:
                with self.subTest(params=params), self.assertRaises(guard.WorkspaceScopeError):
                    bridge._run('rpc', 'terminal.paste', json.dumps(params))
            for args in [(), ('{',), (json.dumps(good), 'extra')]:
                with self.subTest(args=args), self.assertRaises(guard.WorkspaceScopeError):
                    bridge._run('rpc', 'terminal.paste', *args)
            run.assert_not_called()

    def test_paste_rechecks_live_target_before_any_input(self):
        with patch.object(bridge, 'require_same_workspace', side_effect=[self.resolve(), guard.WorkspaceScopeError('moved')]), \
                patch.object(bridge.subprocess, 'run') as run:
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'moved'):
                bridge.send_text('surface:2', 'handshake')
            run.assert_not_called()

    def test_pin_cannot_be_replaced_by_explicit_argument(self):
        with patch.object(bridge, 'require_same_workspace', return_value=self.resolve()) as check:
            bridge.pin_workspace('surface:2', target_uuid=T)
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'overwrite'):
                bridge.pin_workspace('surface:2', target_uuid=X)
            self.assertEqual(check.call_count, 1)

    def test_hook_payload_variants_and_non_shell_edits(self):
        command = 'cmux send-key --surface surface:2 enter'
        cases = [
            ({'toolName': 'Bash', 'toolInput': {'command': command}}, 0),
            ({'name': 'exec_command', 'input': {'cmd': command}}, 0),
            ({'tool_name': 'exec_command', 'cmd': command}, 0),
            ({'tool_name': 'Edit', 'tool_input': {'command': command}}, 0),
            ({'toolName': 'Write', 'input': {'cmd': command}}, 0),
        ]
        for script in ['cmux_workspace_guard.py', 'cmux_agent_panel_guard.py']:
            for payload, expected in cases:
                with self.subTest(script=script, payload=payload):
                    result = subprocess.run([sys.executable, '-B', str(Path(__file__).parent / script)],
                        input=json.dumps(payload), text=True, capture_output=True)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_real_hook_entrypoints(self):
        scripts = Path(__file__).parent
        direct_commands = ['cmux send --surface surface:2 -- hello',
               'rtk cmux-agent ask surface:2 "STATUS: hello"',
               'cmux --help; cmux send-key --surface surface:2 enter',
               'CMUX_WORKSPACE_ID=forged cmux send --surface surface:2 hello',
               'cmux-agent broadcast hello',
               '/Applications/cmux.app/Contents/Resources/bin/cmux send-key --surface surface:2 enter',
               'cmux ' + chr(92) + '\n send --surface surface:2 hello',
               'sh -c "cmux send --surface surface:2 hello"',
               'cmux send --surface surface:2 -- "claude --resume old"']
        for script in ['cmux_workspace_guard.py', 'cmux_agent_panel_guard.py']:
            for command in direct_commands:
                for client in ['Bash', 'exec_command']:
                    result = subprocess.run([sys.executable, '-B', str(scripts / script)],
                        input=json.dumps({'tool_name': client, 'tool_input': {'command': command}}),
                        text=True, capture_output=True)
                    expected = 2 if script == 'cmux_agent_panel_guard.py' and 'claude --resume old' in command else 0
                    self.assertEqual(result.returncode, expected, (script, command, result.stderr))

    def test_harness_rejects_legacy_gate_before_any_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "identity-gate.json").write_text(json.dumps({
                "status": "PASS", "executor": "surface:2", "supervisor": "surface:1"}))
            args = SimpleNamespace(artifact_root=str(root), task_id="test")
            with patch.object(harness.cmux, "send_text") as paste, patch.object(harness.cmux, "send_key") as key, patch.object(harness.cmux, "submit_text") as submit:
                for fn in [harness.cmd_name_surfaces, harness.cmd_bridge_test, harness.cmd_handshake]:
                    with self.assertRaisesRegex(RuntimeError, "lacks UUID"):
                        fn(args)
                paste.assert_not_called()
                key.assert_not_called()
                submit.assert_not_called()

    def test_harness_saved_gate_is_rechecked_against_live_tree(self):
        gate = {"workspace_uuid": W, "supervisor_surface_uuid": C,
                "executor": "surface:2", "executor_surface_uuid": T}
        with patch.object(bridge, "require_same_workspace", return_value=self.resolve()) as live:
            harness._recheck_workspace_gate(gate)
            self.assertEqual(live.call_args.kwargs["expected"]["target_surface_uuid"], T)
        with patch.object(bridge, "require_same_workspace", side_effect=guard.WorkspaceScopeError("target moved")):
            with self.assertRaises(guard.WorkspaceScopeError):
                harness._recheck_workspace_gate(gate)

    def test_readonly_hook_entrypoints(self):
        for script in ['cmux_workspace_guard.py', 'cmux_agent_panel_guard.py']:
            for command in ['cmux identify --json', 'cmux-agent read surface:2 100', 'git status', 'rg "cmux send" README.md']:
                result = subprocess.run([sys.executable, '-B', str(Path(__file__).parent / script)],
                    input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}}),
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
