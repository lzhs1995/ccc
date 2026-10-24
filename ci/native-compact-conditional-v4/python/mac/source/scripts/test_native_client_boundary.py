"""Model clients cannot borrow a Codex caller through inherited tool variables."""
import copy
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_daemon_identity as identity

THREAD = '11111111-1111-4111-8111-111111111111'
SURFACE = '33333333-3333-4333-8333-333333333333'
WORKSPACE = '44444444-4444-4444-8444-444444444444'


class NativeClientBoundaryTests(unittest.TestCase):
    def fixture(self, stack, mode, intermediate='/bin/zsh', argv=None):
        env = dict(CODEX_THREAD_ID=THREAD, CMUX_SURFACE_ID=SURFACE,
                   CMUX_WORKSPACE_ID=WORKSPACE)

        def proc(pid, parent, executable, args=None):
            return dict(pid=pid, ppid=parent, birth=[100, pid],
                        executable=executable, argv=args or [executable],
                        env=dict(env), file_identity=[1, pid, 100, 1])

        tool = proc(50, 40, '/opt/bin/python3.14')
        proxy = proc(40, 30, '/bin/rtk')
        middle = proc(30, 20, intermediate, argv)
        origin = proc(20, 1, '/bin/codex')
        chain = [proxy, middle, origin]
        daemon = None
        client = origin
        if mode == 'managed':
            origin['argv'] += ['app-server', '--managed-daemon']
            daemon = origin
            client = proc(60, 1, '/bin/codex')
        records = {p['pid']: p for p in (tool, proxy, middle, origin, client)}
        stack.enter_context(patch.object(identity.sys, 'platform', 'darwin'))
        stack.enter_context(patch.object(identity.os, 'getpid', return_value=50))
        stack.enter_context(patch.object(identity.os, 'getppid', return_value=40))
        stack.enter_context(patch.object(identity, '_ancestry',
            side_effect=lambda: (copy.deepcopy(chain), copy.deepcopy(daemon))))
        stack.enter_context(patch.object(identity, 'process',
            side_effect=lambda pid, **kw: copy.deepcopy(records[pid])))
        stack.enter_context(patch.object(identity, 'client_candidates',
            return_value=[str(client['pid'])]))
        stack.enter_context(patch.object(identity.foreground, 'read',
            return_value=dict(thread_id=THREAD, client_epoch='native-epoch')))
        stack.enter_context(patch.object(identity.subprocess, 'run',
            return_value=SimpleNamespace(returncode=0, stdout='ttys1')))
        return env, chain, records

    def collect(self, env, entry):
        if entry == 'tool':
            return identity.collect(env)
        return identity.collect_hook(env, dict(session_id=THREAD,
                                               hook_event_name='PostToolUse'))

    def test_nested_claude_cannot_borrow_root_for_tools_or_hooks(self):
        for mode in ('embedded', 'managed'):
            for entry in ('tool', 'hook'):
                with self.subTest(mode=mode, entry=entry), ExitStack() as stack:
                    env, _, _ = self.fixture(stack, mode, '/bin/claude')
                    with self.assertRaisesRegex(identity.IdentityError, 'proxy'):
                        self.collect(env, entry)

    def test_version_named_native_client_cannot_hide_behind_argv(self):
        for mode in ('embedded', 'managed'):
            with self.subTest(mode=mode), ExitStack() as stack:
                env, _, _ = self.fixture(stack, mode,
                    '/Users/test/.local/share/claude/versions/2.1.0', ['zsh'])
                with self.assertRaisesRegex(identity.IdentityError, 'proxy'):
                    self.collect(env, 'tool')

    def test_interpreted_model_and_unknown_wrappers_fail_closed(self):
        for executable, argv in (
                ('/bin/node', ['/bin/node', '/opt/claude/cli.js']),
                ('/bin/python3.14', ['/bin/python3.14', '-m', 'aider']),
                ('/bin/new-model', ['/bin/new-model'])):
            for mode in ('embedded', 'managed'):
                for entry in ('tool', 'hook'):
                    with self.subTest(executable=executable, mode=mode, entry=entry), ExitStack() as stack:
                        env, _, _ = self.fixture(stack, mode, executable, argv)
                        with self.assertRaisesRegex(identity.IdentityError, 'proxy'):
                            self.collect(env, entry)

    def test_normal_shell_proxy_chain_and_leaf_interpreter_still_authenticate(self):
        for executable in ('/bin/sh', '/bin/bash', '/bin/zsh', '/bin/rtk'):
            for mode in ('embedded', 'managed'):
                for entry in ('tool', 'hook'):
                    with self.subTest(executable=executable, mode=mode, entry=entry), ExitStack() as stack:
                        env, _, _ = self.fixture(stack, mode, executable)
                        proof = self.collect(env, entry)
                        self.assertEqual(proof['session'], THREAD)
                        self.assertEqual(proof['client']['pid'], 20 if mode == 'embedded' else 60)

    def test_model_above_nearest_proxy_is_still_rejected(self):
        for mode in ('embedded', 'managed'):
            with self.subTest(mode=mode), ExitStack() as stack:
                env, chain, records = self.fixture(stack, mode)
                model = copy.deepcopy(records[30])
                model.update(pid=25, ppid=20, executable='/bin/claude',
                             argv=['/bin/claude'])
                records[30]['ppid'] = 25
                records[25] = model
                chain.insert(2, model)
                with self.assertRaisesRegex(identity.IdentityError, 'proxy'):
                    self.collect(env, 'tool')

    def test_cleared_model_selector_cannot_make_it_a_proxy(self):
        for mode in ('embedded', 'managed'):
            for entry in ('tool', 'hook'):
                with self.subTest(mode=mode, entry=entry), ExitStack() as stack:
                    env, _, records = self.fixture(stack, mode, '/bin/claude')
                    records[30]['env'].pop('CODEX_THREAD_ID')
                    with self.assertRaisesRegex(identity.IdentityError, 'proxy'):
                        self.collect(env, entry)


if __name__ == '__main__':
    unittest.main()
