"""Native hook spawn shapes and deadline boundaries, with OS edges simulated."""
from contextlib import ExitStack
import copy
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cmux_daemon_identity as daemon
import cmux_hook_identity as hook
import cmux_identity_budget as budget
import cmux_workspace_guard as workspace
import test_cmux_daemon_identity as fixtures

# Keep the real kernel reader: test_all isolates the host ancestry around each
# test by patching daemon.process. These tests deliberately exercise that reader
# with libproc inputs controlled below, rather than the outer suite's fake.
read_kernel_process = daemon.process


class NativeHookCallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DaemonTests()
        self.fixture.setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.env = {k: v for k, v in self.fixture.env.items() if k != 'CODEX_THREAD_ID'}
        self.payload = dict(session_id=fixtures.S, hook_event_name='PreToolUse')
        self.stack.enter_context(patch.object(daemon.sys, 'platform', 'darwin'))
        self.parent = self.stack.enter_context(patch.object(daemon.os, 'getppid', return_value=10))
        self.reader = self.stack.enter_context(patch.object(daemon, 'process', side_effect=self.read))
        self.stack.enter_context(patch.object(daemon, 'client_candidates', return_value=['10', '30']))
        self.stack.enter_context(patch.object(daemon.subprocess, 'run',
            return_value=SimpleNamespace(stdout='ttys1')))

    def read(self, pid, **kw):
        return copy.deepcopy(self.fixture.processes[pid])

    def collect(self):
        return daemon.collect_hook(self.env, self.payload)

    def test_direct_daemon_child_without_thread_env(self):
        proof = self.collect()
        self.assertEqual(proof['client']['pid'], 30)
        self.assertEqual(proof['chain'], [self.fixture.processes[10]])

    def test_direct_daemon_child_with_matching_thread_env(self):
        self.env['CODEX_THREAD_ID'] = fixtures.S
        self.assertEqual(self.collect()['session'], fixtures.S)

    def test_non_exec_shell_without_thread_env(self):
        self.parent.return_value = 20
        self.fixture.processes[20]['env'].pop('CODEX_THREAD_ID')
        self.assertEqual(self.collect()['client']['pid'], 30)

    def test_tool_ancestry_with_matching_thread_env(self):
        self.parent.return_value = 20
        self.assertEqual(self.collect()['client']['pid'], 30)

    def test_missing_or_invalid_payload_session_never_uses_daemon_env(self):
        for value in (None, '', 'bad', True):
            self.payload['session_id'] = value
            with self.subTest(value=value), self.assertRaisesRegex(daemon.IdentityError, 'session_id'):
                self.collect()

    def test_unknown_event_denies(self):
        self.payload['hook_event_name'] = 'Invented'
        with self.assertRaisesRegex(daemon.IdentityError, 'event'):
            self.collect()

    def test_conflicting_thread_in_hook_or_shell_denies(self):
        self.env['CODEX_THREAD_ID'] = fixtures.C
        with self.assertRaisesRegex(daemon.IdentityError, 'ancestry'):
            self.collect()
        self.env.pop('CODEX_THREAD_ID')
        self.parent.return_value = 20
        self.fixture.processes[20]['env']['CODEX_THREAD_ID'] = fixtures.C
        with self.assertRaisesRegex(daemon.IdentityError, 'ancestry'):
            self.collect()

    def test_changed_hook_environment_denies(self):
        self.env['CMUX_SURFACE_ID'] = fixtures.C
        with self.assertRaisesRegex(daemon.IdentityError, 'environment'):
            self.collect()

    def test_payload_workspace_cannot_choose_a_different_caller(self):
        self.payload.update(workspace_id='forged', surface_id='forged')
        with patch.dict(os.environ, self.env, clear=True), patch.object(
                workspace, '_read_json_command', side_effect=[self.fixture.identity,
                self.fixture.tree, self.fixture.identity, self.fixture.tree]):
            self.assertEqual(hook.resolve(self.payload), (fixtures.W, fixtures.C))
        self.assertNotIn('CODEX_THREAD_ID', self.env)

    def test_client_change_after_final_tree_denies(self):
        reads = 0
        def read(pid, **kw):
            nonlocal reads
            item = self.read(pid, **kw)
            if pid == 30:
                reads += 1
                if reads == 9:
                    item['birth'][0] += 1
            return item
        self.reader.side_effect = read
        with patch.dict(os.environ, self.env, clear=True), patch.object(
                workspace, '_read_json_command', side_effect=[self.fixture.identity,
                self.fixture.tree, self.fixture.identity, self.fixture.tree]):
            with self.assertRaisesRegex(workspace.WorkspaceScopeError, 'drift'):
                hook.resolve(self.payload)
        self.assertEqual(reads, 9)

    def test_direct_daemon_is_not_valid_tool_shell_proof(self):
        # The managed parent alone is insufficient. An exec-replaced tool must
        # also carry the matching selector in its own kernel-read environment.
        self.fixture.processes[40] = dict(self.fixture.processes[20], pid=40, ppid=10,
                                          env=dict(self.env))
        with patch.object(daemon.os, 'getpid', return_value=40):
            with self.assertRaisesRegex(daemon.IdentityError, 'ancestry'):
                daemon.collect(self.fixture.env)

    def test_ordinary_terminal_stops_at_verified_login(self):
        self.parent.return_value = 20
        self.fixture.processes[20]['ppid'] = 9
        self.fixture.processes[9] = dict(pid=9, ppid=8, birth=[100, 9], system_login=True)
        self.assertIsNone(self.collect())
        self.assertIsNone(daemon.collect(self.fixture.env))
        self.assertNotIn(8, [call.args[0] for call in self.reader.call_args_list])

    def test_unknown_foreign_ancestor_remains_denied(self):
        self.reader.side_effect = daemon.IdentityError('foreign ancestor')
        with self.assertRaisesRegex(daemon.IdentityError, 'foreign'):
            self.collect()

    def test_ordinary_ancestor_drift_is_not_a_fallback(self):
        self.fixture.processes[20]['ppid'] = 1
        self.parent.return_value = 20
        item = self.read(20)
        changed = copy.deepcopy(item)
        changed['birth'][0] += 1
        self.reader.side_effect = [item, changed]
        with self.assertRaisesRegex(daemon.IdentityError, 'drift'):
            self.collect()


class SystemLoginBoundaryTests(unittest.TestCase):
    def read_login(self, *, path='/usr/bin/login', uid=0, final=None,
                   path_final=None, allowed=True, stat_uid=0, mode=0o100755):
        reads = 0
        def info(pid, flavor, arg, ptr, size):
            nonlocal reads
            reads += 1
            b = ptr._obj
            b.pid, b.ppid, b.uid, b.start_sec, b.start_usec = pid, 8, uid, 100, 2
            b.status = 2
            if reads == 2 and final:
                for k, v in final.items():
                    setattr(b, k, v)
            return size
        paths = iter([path, path if path_final is None else path_final])
        def pidpath(pid, buffer, size):
            buffer.value = next(paths).encode()
            return len(buffer.value)
        lib = SimpleNamespace(proc_pidinfo=Mock(side_effect=info),
                              proc_pidpath=Mock(side_effect=pidpath))
        with patch.object(daemon.sys, 'platform', 'darwin'), \
             patch.object(daemon.os, 'getuid', return_value=501), \
             patch.object(daemon.ctypes, 'CDLL', return_value=lib), \
             patch.object(daemon.Path, 'stat', return_value=SimpleNamespace(st_uid=stat_uid, st_mode=mode)):
            return read_kernel_process(9, allow_system_login=allowed)

    def test_exact_protected_root_login_is_only_a_boundary(self):
        result = self.read_login()
        self.assertEqual(result, dict(pid=9, ppid=8, birth=[100, 2], system_login=True))
        self.assertNotIn('env', result)

    def test_normal_process_api_still_rejects_root_login(self):
        with self.assertRaisesRegex(daemon.IdentityError, 'foreign'):
            self.read_login(allowed=False)

    def test_basename_other_path_or_foreign_uid_rejected(self):
        for kwargs in ({'path': '/tmp/login'}, {'path': '/usr/bin/other'}, {'uid': 502}):
            with self.subTest(kwargs=kwargs), self.assertRaises(daemon.IdentityError):
                self.read_login(**kwargs)

    def test_root_login_path_birth_owner_and_mode_drift_rejected(self):
        for kwargs in ({'path_final': '/tmp/login'}, {'final': {'start_sec': 101}},
                       {'final': {'uid': 502}}, {'stat_uid': 501}, {'mode': 0o100777}):
            with self.subTest(kwargs=kwargs), self.assertRaises(daemon.IdentityError):
                self.read_login(**kwargs)


class IdentityDeadlineTests(unittest.TestCase):
    def test_each_command_receives_only_remaining_budget(self):
        now = [10.0]
        observed = []
        def run(*args, **kw):
            observed.append(kw['timeout'])
            now[0] += .8
            return SimpleNamespace(stdout='{}')
        with patch.object(budget.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(workspace.subprocess, 'run', side_effect=run), budget.limit(2.5):
            workspace._read_json_command('identify', '--json')
            workspace._read_json_command('tree', '--json')
        self.assertAlmostEqual(observed[0], 2.5)
        self.assertAlmostEqual(observed[1], 1.7)

    def test_success_returned_after_deadline_is_rejected(self):
        now = [0.0]
        def run(*args, **kw):
            now[0] = 3.0
            return SimpleNamespace(stdout='{}')
        with patch.object(budget.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(workspace.subprocess, 'run', side_effect=run):
            with self.assertRaises(workspace.WorkspaceScopeError), budget.limit(2.5):
                workspace._read_json_command('identify')

    def test_inventory_obeys_same_budget(self):
        with patch.object(daemon.subprocess, 'run', return_value=SimpleNamespace(
                stdout='', returncode=0)) as run, budget.limit(.2):
            daemon.client_candidates()
            self.assertLessEqual(run.call_args.kwargs['timeout'], .2)

    def test_nested_scope_cannot_extend_outer_deadline_and_resets(self):
        with patch.object(budget.time, 'monotonic', return_value=100):
            with budget.limit(1):
                with budget.limit(10):
                    self.assertEqual(budget.timeout(), 1)
            self.assertEqual(budget.timeout(), 5)

    def test_real_slow_cmux_process_is_terminated_within_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            slow = Path(tmp) / 'cmux'
            slow.write_text('#!/bin/sh\nexec /bin/sleep 10\n')
            slow.chmod(0o700)
            started = time.monotonic()
            with patch.object(workspace, 'CMUX', str(slow)):
                with self.assertRaises(workspace.WorkspaceScopeError), budget.limit(.15):
                    workspace._read_json_command('identify')
            self.assertLess(time.monotonic() - started, 1.5)


if __name__ == '__main__':
    unittest.main()
