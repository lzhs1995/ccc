"""An embedded TUI authenticates only its own live foreground tool or hook."""
import copy
from contextlib import ExitStack
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_daemon_identity as identity
import cmux_hook_identity as hook
import cmux_workspace_guard as guard
import executor_reask_identity as consumer

THREAD = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
SURFACE = '33333333-3333-4333-8333-333333333333'
WORKSPACE = '44444444-4444-4444-8444-444444444444'
PANE = '55555555-5555-4555-8555-555555555555'
PEER = '66666666-6666-4666-8666-666666666666'
SIDE = '77777777-7777-4777-8777-777777777777'


class EmbeddedNativeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.env = dict(CODEX_THREAD_ID=THREAD, CMUX_SURFACE_ID=SURFACE,
                        CMUX_WORKSPACE_ID=WORKSPACE)
        def proc(pid, parent, executable, env):
            return dict(pid=pid, ppid=parent, birth=[100, pid], executable=executable,
                        argv=[executable], env=env, file_identity=[1, pid, 100, 1])
        self.tool = proc(40, 30, '/bin/python', dict(self.env))
        self.wrapper = proc(30, 20, '/bin/rtk', dict(self.env))
        self.client = proc(20, 10, '/bin/codex',
                           {k: v for k, v in self.env.items() if k != 'CODEX_THREAD_ID'})
        self.chain = [self.wrapper, self.client]
        self.selected = dict(thread_id=THREAD, client_epoch='native-epoch',
                             path='/native/client-20-thread.json', signature=[1, 2, 3])
        self.payload = dict(session_id=THREAD, hook_event_name='PostToolUse')
        self.raw = dict(caller=dict(surface_ref='surface:1', workspace_ref='workspace:1',
                                   pane_ref='pane:1'))
        self.tree = dict(windows=[dict(ref='window:1', workspaces=[dict(
            ref='workspace:1', id=WORKSPACE, panes=[
                dict(ref='pane:1', id=PANE, surfaces=[dict(ref='surface:1', id=SURFACE,
                     type='terminal', tty='ttys1')]),
                dict(ref='pane:2', id=SIDE, surfaces=[dict(ref='surface:2', id=PEER,
                     type='terminal', tty='ttys2')])])])])
        self.mock(identity.sys, 'platform', new='darwin')
        self.mock(identity.os, 'getpid', return_value=40)
        self.process = self.mock(identity, 'process',
                                 side_effect=lambda pid: copy.deepcopy(self.tool))
        self.ancestry = self.mock(identity, '_ancestry',
                                 side_effect=lambda: (copy.deepcopy(self.chain), None))
        self.foreground = self.mock(identity.foreground, 'read',
                                   side_effect=lambda client: copy.deepcopy(self.selected))
        self.ps = self.mock(identity.subprocess, 'run',
                            return_value=SimpleNamespace(stdout='ttys1'))
        self.inventory = self.mock(identity, 'client_candidates',
                                   side_effect=AssertionError('no global discovery'))

    def mock(self, obj, name, **kwargs):
        return self.stack.enter_context(patch.object(obj, name, **kwargs))

    def collect(self):
        return identity.collect(self.env)

    def test_tool_authenticates_actual_ancestor_without_a_daemon(self):
        proof = self.collect()
        self.assertEqual(proof['source'], 'embedded-native-client-v1')
        self.assertIsNone(proof['daemon'])
        self.assertEqual(proof['client']['pid'], 20)
        self.assertEqual(proof['foreground'], self.selected)
        public = identity.public_proof(proof)
        self.assertIsNone(public['daemon_pid'])
        self.assertEqual(public['foreground'], {'thread_id': THREAD})
        self.assertFalse({'env', 'chain', 'daemon', 'client'} & public.keys())
        self.inventory.assert_not_called()

    def test_native_hook_without_inherited_thread_uses_matching_foreground(self):
        for env in (self.env, self.tool['env'], self.wrapper['env']):
            env.pop('CODEX_THREAD_ID')
        proof = identity.collect_hook(self.env, self.payload)
        self.assertEqual(proof['session'], THREAD)

    def test_ordinary_non_codex_ancestry_stays_unverified(self):
        self.chain = [self.wrapper]
        self.assertIsNone(self.collect())
        self.assertIsNone(identity.collect_hook(self.env, {}))
        self.process.assert_not_called()

    def test_unmanaged_app_server_cannot_be_an_embedded_tui(self):
        self.client['argv'] += ['app-server']
        with self.assertRaisesRegex(identity.IdentityError, 'unmanaged app-server'):
            self.collect()

    def test_missing_foreground_never_falls_back_to_resume_argv(self):
        self.client['argv'] += ['resume', THREAD]
        self.selected = None
        with self.assertRaisesRegex(identity.IdentityError, 'native foreground'):
            self.collect()

    def test_corrupt_foreground_cannot_be_ignored(self):
        self.foreground.side_effect = ValueError('untrusted foreground evidence')
        with self.assertRaisesRegex(identity.IdentityError, 'foreground unavailable'):
            self.collect()

    def test_child_tool_cannot_borrow_root_foreground(self):
        for env in (self.env, self.tool['env'], self.wrapper['env']):
            env['CODEX_THREAD_ID'] = CHILD
        with self.assertRaisesRegex(identity.IdentityError, 'native foreground'):
            self.collect()

    def test_child_hook_cannot_borrow_root_foreground(self):
        for env in (self.env, self.tool['env'], self.wrapper['env']):
            env.pop('CODEX_THREAD_ID')
        with self.assertRaisesRegex(identity.IdentityError, 'native foreground'):
            identity.collect_hook(self.env, dict(self.payload, session_id=CHILD))

    def test_forged_env_cannot_replace_actual_tool_thread(self):
        self.env['CODEX_THREAD_ID'] = CHILD
        with self.assertRaisesRegex(identity.IdentityError, 'tool ancestry'):
            self.collect()

    def test_cleared_env_cannot_hide_child_hook_selector(self):
        self.env.pop('CODEX_THREAD_ID')
        self.tool['env']['CODEX_THREAD_ID'] = CHILD
        with self.assertRaisesRegex(identity.IdentityError, 'tool ancestry'):
            identity.collect_hook(self.env, self.payload)

    def test_tool_requires_its_own_kernel_thread_selector(self):
        self.tool['env'].pop('CODEX_THREAD_ID')
        with self.assertRaisesRegex(identity.IdentityError, 'tool ancestry'):
            self.collect()

    def test_intermediate_wrapper_thread_mismatch_rejects(self):
        self.wrapper['env']['CODEX_THREAD_ID'] = CHILD
        with self.assertRaisesRegex(identity.IdentityError, 'tool ancestry'):
            self.collect()

    def test_all_uuid_sources_must_agree(self):
        for obj in (self.env, self.tool['env'], self.wrapper['env'], self.client['env']):
            for key in ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID'):
                with self.subTest(source=id(obj), key=key):
                    before = obj[key]
                    obj[key] = CHILD
                    with self.assertRaisesRegex(identity.IdentityError, 'UUID'):
                        self.collect()
                    obj[key] = before

    def test_missing_native_client_uuid_rejects(self):
        self.client['env'].pop('CMUX_SURFACE_ID')
        with self.assertRaisesRegex(identity.IdentityError, 'UUID'):
            self.collect()

    def test_unknown_or_invalid_hook_payload_rejects(self):
        for payload in ({}, dict(self.payload, session_id='invalid'),
                        dict(self.payload, hook_event_name='Unsupported')):
            with self.subTest(payload=payload), self.assertRaises(identity.IdentityError):
                identity.collect_hook(self.env, payload)

    def test_tool_reparenting_before_collection_rejects(self):
        self.tool['ppid'] = 99
        with self.assertRaisesRegex(identity.IdentityError, 'ancestry changed'):
            self.collect()

    def test_tool_pid_reuse_during_collection_rejects(self):
        changed = copy.deepcopy(self.tool)
        changed['birth'][0] += 1
        self.process.side_effect = [copy.deepcopy(self.tool), changed]
        with self.assertRaisesRegex(identity.IdentityError, 'changed during'):
            self.collect()

    def test_ancestor_change_during_collection_rejects(self):
        changed = copy.deepcopy(self.chain)
        changed[1]['birth'][0] += 1
        self.ancestry.side_effect = [(copy.deepcopy(self.chain), None), (changed, None)]
        with self.assertRaisesRegex(identity.IdentityError, 'changed during'):
            self.collect()

    def test_foreground_switch_during_collection_rejects(self):
        self.foreground.side_effect = [copy.deepcopy(self.selected),
                                       dict(self.selected, thread_id=CHILD)]
        with self.assertRaisesRegex(identity.IdentityError, 'changed during'):
            self.collect()

    def test_native_tui_requires_a_terminal(self):
        for tty in ('', '?', '??', '-'):
            self.ps.return_value = SimpleNamespace(stdout=tty)
            with self.subTest(tty=tty), self.assertRaisesRegex(identity.IdentityError, 'terminal'):
                self.collect()

    def test_real_hook_resolution_retains_embedded_proof(self):
        self.mock(guard, '_read_json_command', side_effect=lambda *args:
                  copy.deepcopy(self.raw if args[0] == 'identify' else self.tree))
        with patch.dict(os.environ, self.env, clear=True), hook.evaluation(self.payload):
            self.assertEqual(hook.identity(self.payload), (WORKSPACE, SURFACE))
            proof = hook.native_proof()
            self.assertEqual(proof['source'], 'embedded-native-client-v1')
            self.assertEqual(proof['foreground']['thread_id'], THREAD)
            self.assertIsNone(proof['daemon_pid'])

    def test_uuid_row_and_tty_still_gate_transport(self):
        proof = self.collect()
        resolved, env = identity.resolve(self.raw, self.tree, self.env, proof)
        self.assertEqual(guard.resolve_snapshot(resolved, self.tree, PEER,
                         env=env)['caller_surface_uuid'], SURFACE)
        self.tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys9'
        with self.assertRaisesRegex(identity.IdentityError, 'TTY'):
            identity.resolve(self.raw, self.tree, self.env, proof)

    def test_identify_cannot_select_an_unrelated_surface(self):
        proof = self.collect()
        self.raw['caller']['surface_ref'] = 'surface:2'
        with self.assertRaisesRegex(identity.IdentityError, 'native client origin'):
            identity.resolve(self.raw, self.tree, self.env, proof)

    def test_snapshot_rejects_a_late_tree_move(self):
        changed = copy.deepcopy(self.tree)
        changed['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys9'
        self.mock(guard, '_read_json_command', side_effect=[self.raw, self.tree, self.raw, changed])
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'TTY'):
                guard.caller_snapshot()

    def consumer_binding(self):
        proof = identity.public_proof(self.collect())
        binding = dict(session_id=THREAD, caller_surface_uuid=SURFACE,
                       workspace_uuid=WORKSPACE, pane_uuid=PANE, tty='ttys1',
                       process={k: self.client[k] for k in ('pid', 'birth', 'executable')})
        resolved = dict(caller=dict(surface_id=SURFACE, workspace_id=WORKSPACE,
                                    pane_id=PANE, surface_type='terminal'))
        self.consumer_caller = resolved['caller']
        self.mock(guard, 'caller_snapshot', return_value=(resolved, {}, self.env, proof))
        return binding, proof

    def test_new_embedded_consumer_can_pin_its_exact_caller_proof(self):
        binding, proof = self.consumer_binding()
        self.assertEqual(consumer._codex_proof(binding), proof)
        binding['caller_proof'] = copy.deepcopy(proof)
        self.assertEqual(consumer._codex_proof(binding), proof)

    def test_embedded_proof_does_not_migrate_existing_managed_episode(self):
        binding, proof = self.consumer_binding()
        binding['caller_proof'] = {k: v for k, v in proof.items() if k != 'source'}
        binding['caller_proof']['daemon_pid'] = 90
        with self.assertRaisesRegex(ValueError, 'proof changed'):
            consumer._codex_proof(binding)

    def test_embedded_consumer_rejects_foreign_thread_or_forged_mode(self):
        binding, proof = self.consumer_binding()
        for change in (dict(session=CHILD), dict(source='unverified'), dict(daemon_pid=90)):
            with self.subTest(change=change):
                original = copy.deepcopy(proof)
                proof.update(change)
                with self.assertRaisesRegex(ValueError, 'original authenticated'):
                    consumer._codex_proof(binding)
                proof.clear()
                proof.update(original)

    def test_conflicting_caller_uuid_alias_cannot_override_native_id(self):
        binding, _proof = self.consumer_binding()
        for name in ('surface', 'workspace', 'pane'):
            with self.subTest(name=name):
                self.consumer_caller[name + '_uuid'] = CHILD
                with self.assertRaisesRegex(ValueError, 'original authenticated'):
                    consumer._codex_proof(binding)
                self.consumer_caller.pop(name + '_uuid')


if __name__ == '__main__':
    unittest.main()
