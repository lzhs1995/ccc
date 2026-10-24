"""Exec-optimized tools retain the native thread without an incidental wrapper."""
from contextlib import ExitStack
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_daemon_identity as identity
import test_cmux_daemon_identity as fixtures


class DirectToolCallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DaemonTests()
        self.fixture.setUp()
        self.processes = self.fixture.processes
        self.processes[40] = dict(pid=40, ppid=10, birth=[100, 40],
                                  executable='/opt/python3.14', argv=['python3.14', '-B', 'cli.py'],
                                  env=dict(self.fixture.env), file_identity=[1, 40, 123, 1])
        # A native foreground selector works for a new session, not just resume.
        self.processes[30]['argv'] = ['/bin/codex']
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(identity.sys, 'platform', 'darwin'))
        self.parent = self.stack.enter_context(patch.object(identity.os, 'getppid', return_value=10))
        self.stack.enter_context(patch.object(identity.os, 'getpid', return_value=40))
        self.reader = self.stack.enter_context(patch.object(identity, 'process', side_effect=self.read))
        self.stack.enter_context(patch.object(identity, 'client_candidates', return_value=['10', '30']))
        self.stack.enter_context(patch.object(identity.subprocess, 'run', return_value=SimpleNamespace(stdout='ttys1')))
        self.selector = self.stack.enter_context(patch.object(identity.foreground, 'read', side_effect=lambda p:
            {'thread_id': fixtures.S, 'client_epoch': 'native-client'} if p['pid'] == 30 else None))

    def read(self, pid, **kwargs):
        return copy.deepcopy(self.processes[pid])

    def collect(self):
        return identity.collect(dict(self.fixture.env))

    def test_exec_optimized_direct_child_resolves_fresh_native_client(self):
        proof = self.collect()
        self.assertEqual(proof['client']['pid'], 30)
        self.assertEqual(proof['session'], fixtures.S)
        self.assertEqual([p['pid'] for p in proof['chain']], [40, 10])
        resolved, env = identity.resolve(self.fixture.identity, self.fixture.tree, self.fixture.env, proof)
        self.assertEqual(resolved['caller']['surface_id'], fixtures.C)
        self.assertEqual(env['CMUX_SURFACE_ID'], fixtures.C)

    def test_environment_selector_cannot_replace_kernel_selector(self):
        for value in (None, fixtures.C):
            with self.subTest(value=value):
                self.processes[40]['env']['CODEX_THREAD_ID'] = value
                with self.assertRaisesRegex(identity.IdentityError, 'current tool ancestry'):
                    self.collect()

    def test_actual_parent_must_be_the_observed_managed_daemon(self):
        self.processes[40]['ppid'] = 99
        with self.assertRaisesRegex(identity.IdentityError, 'current tool ancestry'):
            self.collect()

    def test_kernel_tool_scope_must_match_unmodified_tool_environment(self):
        self.processes[40]['env']['CMUX_SURFACE_ID'] = fixtures.C
        with self.assertRaisesRegex(identity.IdentityError, 'current tool environment'):
            self.collect()

    def test_no_fallback_for_missing_selector_in_non_direct_chain(self):
        self.parent.return_value = 20
        self.processes[20]['env'].pop('CODEX_THREAD_ID')
        with self.assertRaisesRegex(identity.IdentityError, 'missing from tool ancestry'):
            self.collect()

    def test_conflicting_ancestor_cannot_be_overridden_by_current_process(self):
        self.parent.return_value = 20
        self.processes[20]['env']['CODEX_THREAD_ID'] = fixtures.C
        with self.assertRaisesRegex(identity.IdentityError, 'differs from tool ancestry'):
            self.collect()

    def test_current_process_birth_or_parent_drift_rejected_after_inventory(self):
        for field in ('birth', 'ppid'):
            seen = 0
            def read(pid, **kwargs):
                nonlocal seen
                result = self.read(pid, **kwargs)
                if pid == 40:
                    seen += 1
                    if seen > 1:
                        result[field] = [999, 1] if field == 'birth' else 99
                return result
            with self.subTest(field=field):
                self.reader.side_effect = read
                with self.assertRaisesRegex(identity.IdentityError, 'identity drift'):
                    self.collect()
        self.reader.side_effect = self.read

    def test_missing_or_switched_native_foreground_still_denies(self):
        self.selector.side_effect = lambda p: {'thread_id': fixtures.C} if p['pid'] == 30 else None
        with self.assertRaisesRegex(identity.IdentityError, 'unique active client'):
            self.collect()


if __name__ == '__main__':
    unittest.main()
