"""Embedded late-ACK authentication: native roots pass; selectors alone do not."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import reconcile_handshake_ack as reconcile

if __name__ == '__main__' and '--original-controller' in sys.argv:
    index = sys.argv.index('--original-controller')
    reconcile.original_modules(sys.argv[index + 1])
    del sys.argv[index:index + 2]

import cmux_daemon_identity as identity
import cmux_native_delivery as native
import cmux_workspace_guard as guard
import mac_harness as harness

THREAD = '11111111-1111-4111-8111-111111111111'
CALLER = '22222222-2222-4222-8222-222222222222'
WORKSPACE = '33333333-3333-4333-8333-333333333333'
EXECUTOR = '44444444-4444-4444-8444-444444444444'
PANE = '55555555-5555-4555-8555-555555555555'
SIDE = '66666666-6666-4666-8666-666666666666'


class EmbeddedSupervisorTests(unittest.TestCase):
    def patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        self.env = dict(CODEX_THREAD_ID=THREAD, CMUX_SURFACE_ID=CALLER,
                        CMUX_WORKSPACE_ID=WORKSPACE)
        def process(pid, ppid, exe, env):
            return dict(pid=pid, ppid=ppid, birth=[100, pid], executable=exe,
                        argv=[exe], env=env, file_identity=[1, pid, 100, 1])
        self.tool = process(40, 30, '/bin/python', dict(self.env))
        self.wrapper = process(30, 20, '/bin/rtk', dict(self.env))
        self.client = process(20, 10, '/bin/codex',
                              {k: v for k, v in self.env.items() if k != 'CODEX_THREAD_ID'})
        self.chain = [self.wrapper, self.client]
        self.selected = dict(thread_id=THREAD, client_epoch='native-epoch',
                             path='/native/client-20-thread.json', signature=[1, 2, 3])
        self.tree = dict(windows=[dict(workspaces=[dict(ref='workspace:1', id=WORKSPACE,
            panes=[dict(ref='pane:1', id=PANE, surfaces=[
                dict(ref='surface:1', id=CALLER, type='terminal', tty='ttys1')]),
                   dict(ref='pane:2', id=SIDE, surfaces=[
                dict(ref='surface:2', id=EXECUTOR, type='terminal', tty='ttys2')])])])])
        self.snapshot = (dict(caller=dict(surface_ref='surface:1',
            workspace_ref='workspace:1', pane_ref='pane:1')), self.tree, self.env, None)
        self.row = dict(workspace_uuid=WORKSPACE, target_surface_uuid=CALLER,
                        target_pane_uuid=PANE, tty='ttys1', kind='terminal', dock=None)
        self.gate = dict(supervisor_surface_uuid=CALLER, workspace_uuid=WORKSPACE,
            executors=[dict(surface_ref='surface:2', surface_uuid=EXECUTOR)],
            native_participants=[dict(role='supervisor', provider='codex',
                surface_uuid=CALLER, native_session_id=THREAD)])
        self.patch(identity.os, 'getpid', return_value=40)
        self.process = self.patch(identity, 'process', side_effect=lambda pid: copy.deepcopy(self.tool))
        self.ancestry = self.patch(identity, '_ancestry',
                                  side_effect=lambda: (copy.deepcopy(self.chain), None))
        self.foreground = self.patch(identity.foreground, 'read',
                                    side_effect=lambda client: copy.deepcopy(self.selected))
        self.patch(identity.subprocess, 'run', return_value=SimpleNamespace(stdout='ttys1'))
        self.caller = self.patch(guard, 'caller_snapshot',
                                side_effect=lambda: copy.deepcopy(self.snapshot))
        self.tree_row = self.patch(native, '_tree_row', side_effect=lambda *args: copy.deepcopy(self.row))
        self.workspace = self.patch(harness, '_recheck_workspace_gate')
        self.send = self.patch(harness.cmux, 'send_text')
        self.key = self.patch(harness.cmux, 'send_key')
        self.proof = {}

    def check(self):
        return reconcile.require_supervisor(harness, self.gate, proof_out=self.proof)

    def reject(self, message):
        with self.assertRaisesRegex((ValueError, guard.WorkspaceScopeError), message):
            self.check()
        self.workspace.assert_not_called()
        self.send.assert_not_called()
        self.key.assert_not_called()

    def test_original_embedded_root_authenticates_and_retains_native_proof(self):
        self.assertEqual(self.check(), THREAD)
        self.assertEqual(self.proof['source'], 'embedded-original-supervisor-v1')
        self.assertEqual(self.proof['client']['pid'], 20)
        self.assertEqual(self.proof['foreground'], self.selected)
        self.workspace.assert_called_once_with(self.gate)
        self.send.assert_not_called()
        self.key.assert_not_called()

    def test_environment_alone_cannot_select_a_supervisor(self):
        self.chain = [self.wrapper]
        self.reject('ANCESTRY_REQUIRED')

    def test_unmanaged_app_server_is_not_an_embedded_tui(self):
        self.client['argv'].append('app-server')
        self.reject('ANCESTRY_REQUIRED')

    def test_child_thread_cannot_inherit_root_authority(self):
        self.tool['env']['CODEX_THREAD_ID'] = EXECUTOR
        self.reject('TOOL_THREAD_CHANGED')

    def test_missing_current_kernel_selector_fails(self):
        self.tool['env'].pop('CODEX_THREAD_ID')
        self.reject('TOOL_THREAD_CHANGED')

    def test_intermediate_thread_mismatch_fails(self):
        self.wrapper['env']['CODEX_THREAD_ID'] = EXECUTOR
        self.reject('TOOL_THREAD_CHANGED')

    def test_forged_cmux_environment_fails(self):
        self.env['CMUX_SURFACE_ID'] = EXECUTOR
        self.reject('UUID_CHANGED')

    def test_native_foreground_required_without_resume_fallback(self):
        self.client['argv'] += ['resume', THREAD]
        self.selected = None
        self.reject('NATIVE_FOREGROUND_REQUIRED')

    def test_wrong_native_foreground_fails(self):
        self.selected['thread_id'] = EXECUTOR
        self.reject('NATIVE_FOREGROUND_REQUIRED')

    def test_tty_mismatch_fails(self):
        self.row['tty'] = 'ttys9'
        self.reject('TTY_CHANGED')

    def test_process_reuse_during_check_fails(self):
        changed = copy.deepcopy(self.tool)
        changed['birth'][0] += 1
        self.process.side_effect = [copy.deepcopy(self.tool), changed]
        self.reject('CHANGED_DURING_CHECK')

    def test_ancestry_drift_fails(self):
        changed = copy.deepcopy(self.chain)
        changed[0]['ppid'] = 99
        self.ancestry.side_effect = [(copy.deepcopy(self.chain), None), (changed, None)]
        self.reject('CHANGED_DURING_CHECK')

    def test_native_foreground_change_at_last_read_fails(self):
        changed = dict(self.selected, thread_id=EXECUTOR)
        self.foreground.side_effect = [copy.deepcopy(self.selected), changed]
        self.reject('CHANGED_DURING_CHECK')

    def test_cmux_change_during_check_fails(self):
        changed = copy.deepcopy(self.row)
        changed['target_pane_uuid'] = SIDE
        self.tree_row.side_effect = [copy.deepcopy(self.row), changed]
        self.reject('CHANGED_DURING_CHECK')


if __name__ == '__main__':
    unittest.main()
