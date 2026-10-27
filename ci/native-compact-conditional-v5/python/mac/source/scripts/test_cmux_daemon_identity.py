import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_daemon_identity as d
import cmux_workspace_guard as g

S = '11111111-1111-4111-8111-111111111111'
C = '22222222-2222-4222-8222-222222222222'
D = '33333333-3333-4333-8333-333333333333'
W = '44444444-4444-4444-8444-444444444444'
X = '55555555-5555-4555-8555-555555555555'
P = '66666666-6666-4666-8666-666666666666'
Q = '77777777-7777-4777-8777-777777777777'
T = '88888888-8888-4888-8888-888888888888'


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(CODEX_THREAD_ID=S, CMUX_SURFACE_ID=D, CMUX_WORKSPACE_ID=X)
        self.client_env = dict(CMUX_SURFACE_ID=C, CMUX_WORKSPACE_ID=W)
        def proc(pid, ppid, argv, env):
            return dict(pid=pid, ppid=ppid, argv=argv, env=env, birth=[100, pid],
                        executable=argv[0], file_identity=[1, pid, 123, 1])
        self.processes = {
            20: proc(20, 10, ['/bin/rtk'], dict(self.env)),
            10: proc(10, 1, ['/bin/codex', 'app-server', '--managed-daemon'], dict(self.env)),
            30: proc(30, 5, ['/bin/codex', 'resume', S], self.client_env),
        }
        self.proof = dict(session=S, daemon=self.processes[10],
                          client=dict(self.processes[30], tty='ttys1'),
                          chain=[self.processes[20], self.processes[10]])
        self.identity = {'caller': dict(surface_ref='surface:46', workspace_ref='workspace:15', pane_ref='pane:33')}
        def surface(ref, ident, tty):
            return dict(ref=ref, id=ident, type='terminal', tty=tty)
        self.tree = {'windows': [{'ref': 'window:1', 'workspaces': [
            dict(ref='workspace:1', id=W, panes=[
                dict(ref='pane:1', id=P, surfaces=[surface('surface:1', C, 'ttys1')]),
                dict(ref='pane:2', id=Q, surfaces=[surface('surface:2', T, 'ttys2')])]),
            dict(ref='workspace:15', id=X, panes=[
                dict(ref='pane:33', id=D, surfaces=[surface('surface:46', D, 'ttys46')])])]}]}

    def resolve(self):
        return d.resolve(self.identity, self.tree, self.env, self.proof)

    def test_actual_thread_wins_over_daemon_origin(self):
        identity, env = self.resolve()
        self.assertEqual(g.resolve_snapshot(identity, self.tree, 'surface:2', env=env)['caller_surface_uuid'], C)
        self.assertEqual(self.env['CMUX_SURFACE_ID'], D)
        self.assertEqual(env['CODEX_THREAD_ID'], S)

    def test_ordinary_path_unchanged(self):
        self.assertEqual(d.resolve(self.identity, self.tree, self.env, None), (self.identity, self.env))

    def test_foreign_target_still_denied(self):
        identity, env = self.resolve()
        with self.assertRaises(g.WorkspaceScopeError):
            g.resolve_snapshot(identity, self.tree, 'surface:46', env=env)

    def test_same_pane_still_denied(self):
        self.tree['windows'][0]['workspaces'][0]['panes'][1]['id'] = P
        identity, env = self.resolve()
        with self.assertRaises(g.WorkspaceScopeError):
            g.resolve_snapshot(identity, self.tree, 'surface:2', env=env)

    def test_tty_reuse(self):
        self.tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys9'
        with self.assertRaises(d.IdentityError): self.resolve()

    def test_unrelated_stale_tty_does_not_override_live_uuid(self):
        self.tree['windows'][0]['workspaces'][0]['panes'][1]['surfaces'][0]['tty'] = 'ttys1'
        identity, env = self.resolve()
        self.assertEqual(identity['caller']['surface_id'], C)
        self.assertEqual(env['CMUX_SURFACE_ID'], C)

    def test_foreign_stale_tty_cannot_permit_cross_workspace(self):
        self.tree['windows'][0]['workspaces'][1]['panes'][0]['surfaces'][0]['tty'] = 'ttys1'
        identity, env = self.resolve()
        with self.assertRaises(g.WorkspaceScopeError):
            g.resolve_snapshot(identity, self.tree, 'surface:46', env=env)

    def test_duplicate_caller_uuid_still_denied_with_same_tty(self):
        duplicate = self.tree['windows'][0]['workspaces'][0]['panes'][1]['surfaces'][0]
        duplicate.update(id=C, tty='ttys1')
        with self.assertRaisesRegex(d.IdentityError, 'UUIDs'): self.resolve()

    def test_other_tty_match_cannot_replace_wrong_caller_tty(self):
        self.tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys9'
        self.tree['windows'][0]['workspaces'][0]['panes'][1]['surfaces'][0]['tty'] = 'ttys1'
        with self.assertRaisesRegex(d.IdentityError, 'TTY'): self.resolve()

    def test_nonterminal_caller_still_denied(self):
        self.tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['type'] = 'browser'
        with self.assertRaises(d.IdentityError): self.resolve()

    def test_stale_workspace(self):
        self.client_env['CMUX_WORKSPACE_ID'] = X
        with self.assertRaises(d.IdentityError): self.resolve()

    def test_wrong_identify(self):
        self.identity['caller']['surface_ref'] = 'surface:1'
        with self.assertRaises(d.IdentityError): self.resolve()

    def collect(self, pids='10\n30\n', process_fn=None):
        def read(pid, **_kwargs):
            return process_fn(pid) if process_fn else copy.deepcopy(self.processes[pid])
        with patch.object(d.sys, 'platform', 'darwin'), patch.object(d.os, 'getppid', return_value=20), \
             patch.object(d, 'process', side_effect=read), \
             patch.object(d.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout=''.join(f'{pid} /bin/codex\n' for pid in pids.split())), SimpleNamespace(stdout='ttys1')]):
            return d.collect(self.env)

    def test_live_collection(self): self.assertEqual(self.collect(), self.proof)

    def test_forged_thread_selector(self):
        self.env['CODEX_THREAD_ID'] = C
        with self.assertRaisesRegex(d.IdentityError, 'ancestry'): self.collect()

    def test_forged_cmux_environment(self):
        self.env['CMUX_SURFACE_ID'] = C
        with self.assertRaisesRegex(d.IdentityError, 'environment'): self.collect()

    def test_missing_session(self):
        self.processes[30]['argv'][-1] = C
        with self.assertRaisesRegex(d.IdentityError, 'unique'): self.collect()

    def test_no_terminal(self):
        with patch.object(d.sys, 'platform', 'darwin'), patch.object(d.os, 'getppid', return_value=20), \
             patch.object(d, 'process', side_effect=lambda pid, **kw: copy.deepcopy(self.processes[pid])), \
             patch.object(d.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout='30 /bin/codex'), SimpleNamespace(stdout='??')]):
            with self.assertRaisesRegex(d.IdentityError, 'terminal'): d.collect(self.env)

    def test_duplicate_session(self):
        self.processes[31] = dict(self.processes[30], pid=31)
        with patch.object(d.sys, 'platform', 'darwin'), patch.object(d.os, 'getppid', return_value=20), \
             patch.object(d, 'process', side_effect=lambda pid, **kw: copy.deepcopy(self.processes[pid])), \
             patch.object(d.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout='30 /bin/codex\n31 /bin/codex'), SimpleNamespace(stdout='ttys1'), SimpleNamespace(stdout='ttys2')]):
            with self.assertRaisesRegex(d.IdentityError, 'unique'): d.collect(self.env)

    def test_process_replaced_during_inventory(self):
        seen = {}
        def read(pid):
            seen[pid] = seen.get(pid, 0) + 1
            p = copy.deepcopy(self.processes[pid])
            if pid == 30 and seen[pid] == 2: p['birth'][0] += 1
            return p
        with self.assertRaisesRegex(d.IdentityError, 'drift'): self.collect(process_fn=read)

    def test_final_boundaries_reject_identity_drift(self):
        for key in ('birth', 'argv', 'env'):
            changed = copy.deepcopy(self.proof)
            changed['client'][key] = []
            with self.subTest(key=key), patch.object(g.daemon_identity, 'collect', side_effect=[self.proof, self.proof, changed]), \
                 patch.object(g, '_read_json_command', side_effect=[self.identity, self.tree, self.identity, self.tree]), \
                 patch.dict(os.environ, self.env, clear=True):
                with self.assertRaisesRegex(g.WorkspaceScopeError, 'final check'): g.caller_snapshot()

    def test_tree_move_at_second_read_rejects(self):
        final = copy.deepcopy(self.tree)
        final['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys99'
        with patch.object(g.daemon_identity, 'collect', return_value=self.proof), \
             patch.object(g, '_read_json_command', side_effect=[self.identity, self.tree, self.identity, final]), \
             patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(g.WorkspaceScopeError): g.caller_snapshot()


if __name__ == '__main__': unittest.main()


class InventoryTests(unittest.TestCase):
    def test_discovery_uses_executable_paths_not_pgrep_name(self):
        with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='47564 /a directory/package/bin/codex\n42 /bin/zsh\n')) as run:
            self.assertEqual(d.client_candidates(), ['47564'])
            self.assertEqual(run.call_args.args[0][0], '/bin/ps')

    def test_inventory_failure_denies(self):
        for code, output in [(1, ''), (0, 'invalid'),
                             (0, '1 /bin/codex\n1 /bin/codex')]:
            with self.subTest(output=output), patch.object(d.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=code, stdout=output)):
                with self.assertRaises(d.IdentityError): d.client_candidates()

    def test_no_basename_substring_match(self):
        with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='1 /bin/codex-other\n2 /bin/notcodex\n')):
            self.assertEqual(d.client_candidates(), [])
