"""Candidate exclusion must not weaken native caller or workspace authentication."""
import copy
import ctypes
import errno
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import cmux_bridge as bridge
import cmux_daemon_identity as daemon
import cmux_workspace_guard as guard
import test_cmux_daemon_identity as fixtures

S, C, T = fixtures.S, fixtures.C, fixtures.T
# test_all's outer fixture blocks live ancestry. These tests exercise the real
# reader with entirely synthetic libproc/sysctl boundaries, never the host PID.
read_kernel_process = daemon.process


class CandidateExclusionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DaemonTests()
        self.fixture.setUp()
        self.processes = self.fixture.processes
        self.processes[99] = dict(copy.deepcopy(self.processes[30]), pid=99,
                                  argv=['codex', 'resume', T], birth=[100, 99])
        self.reads = []

    def collect(self, mutate=None):
        def read(pid, *, validate_argv=True, allow_system_login=False):
            self.reads.append((pid, validate_argv))
            item = copy.deepcopy(self.processes[pid])
            if mutate:
                item = mutate(pid, item)
            if validate_argv and item['argv'][0] != item['executable']:
                raise daemon.IdentityError('argv executable differs from kernel executable')
            return item
        def ps(argv, **kwargs):
            self.assertEqual(argv[:2], ['/bin/ps', '-p'])
            return SimpleNamespace(returncode=0, stdout='ttys1')
        with patch.object(daemon.sys, 'platform', 'darwin'), \
             patch.object(daemon.os, 'getppid', return_value=20), \
             patch.object(daemon, 'client_candidates', return_value=['10', '30', '99']), \
             patch.object(daemon, 'process', side_effect=read), \
             patch.object(daemon.subprocess, 'run', side_effect=ps):
            return daemon.collect(self.fixture.env)

    def test_stable_nonmatching_relative_argv_is_excluded(self):
        self.assertEqual(self.collect()['client']['pid'], 30)
        self.assertEqual([strict for pid, strict in self.reads if pid == 99], [False, False])
        self.assertEqual([strict for pid, strict in self.reads if pid == 30], [False, True, True])

    def test_initial_confirmed_exit_is_excluded(self):
        def exited(pid, item):
            if pid == 99:
                raise daemon.ProcessExited('initial ESRCH')
            return item
        self.assertEqual(self.collect(exited)['client']['pid'], 30)
        self.assertEqual([pid for pid, _ in self.reads].count(99), 1)

    def test_ambiguous_old_exit_message_is_not_excluded(self):
        def unknown(pid, item):
            if pid == 99:
                raise daemon.IdentityError('process exited, foreign, or inaccessible')
            return item
        with self.assertRaisesRegex(daemon.IdentityError, 'inaccessible'):
            self.collect(unknown)

    def test_unknown_argv_failure_is_not_excluded(self):
        def unknown(pid, item):
            if pid == 99:
                raise daemon.IdentityError('process arguments inaccessible')
            return item
        with self.assertRaisesRegex(daemon.IdentityError, 'arguments inaccessible'):
            self.collect(unknown)

    def test_nonmatching_second_read_exit_is_not_excluded(self):
        def exited(pid, item):
            if pid == 99 and [p for p, _ in self.reads].count(99) == 2:
                raise daemon.ProcessExited('initial ESRCH on second observation')
            return item
        with self.assertRaises(daemon.IdentityError):
            self.collect(exited)

    def test_nonmatching_birth_change_is_not_excluded(self):
        def drift(pid, item):
            if pid == 99 and [p for p, _ in self.reads].count(99) == 2:
                item['birth'][0] += 1
            return item
        with self.assertRaisesRegex(daemon.IdentityError, 'candidate identity drift'):
            self.collect(drift)

    def test_nonmatching_argv_becoming_current_session_is_not_excluded(self):
        def drift(pid, item):
            if pid == 99 and [p for p, _ in self.reads].count(99) == 2:
                item['argv'][-1] = S
            return item
        with self.assertRaisesRegex(daemon.IdentityError, 'candidate identity drift'):
            self.collect(drift)

    def test_selected_relative_argv_still_denies(self):
        self.processes[30]['argv'][0] = 'codex'
        with self.assertRaisesRegex(daemon.IdentityError, 'argv executable'):
            self.collect()

    def test_second_matching_client_cannot_hide_behind_relative_argv(self):
        self.processes[99]['argv'][-1] = S
        with self.assertRaisesRegex(daemon.IdentityError, 'argv executable'):
            self.collect()

    def test_duplicate_matching_clients_still_deny(self):
        self.processes[99]['argv'] = list(self.processes[30]['argv'])
        with self.assertRaisesRegex(daemon.IdentityError, 'unique'):
            self.collect()

    def test_selected_exit_at_strict_recheck_still_denies(self):
        def exited(pid, item):
            if pid == 30 and [p for p, _ in self.reads].count(30) == 2:
                raise daemon.ProcessExited('initial ESRCH at selected recheck')
            return item
        with self.assertRaises(daemon.IdentityError):
            self.collect(exited)

    def test_selected_final_birth_change_still_denies(self):
        def drift(pid, item):
            if pid == 30 and [p for p, _ in self.reads].count(30) == 3:
                item['birth'][0] += 1
            return item
        with self.assertRaisesRegex(daemon.IdentityError, 'process identity drift'):
            self.collect(drift)


class ProcessExitEvidenceTests(unittest.TestCase):
    def process(self, observations, *, arguments=False, validate_argv=True, path=None):
        reads = iter(observations)
        def info(pid, flavor, arg, ptr, size):
            entry = next(reads)
            if 'errno' in entry:
                ctypes.set_errno(entry['errno'])
                return 0
            b = ptr._obj
            b.pid, b.ppid, b.uid, b.start_sec, b.start_usec = pid, 5, 501, 100, 1
            b.status = 2
            for key, value in entry.items():
                setattr(b, key, value)
            return size
        def pidpath(pid, buffer, size):
            buffer.value = str(path).encode()
            return len(buffer.value)
        lib = SimpleNamespace(proc_pidinfo=Mock(side_effect=info),
                              proc_pidpath=Mock(side_effect=pidpath))
        libc = SimpleNamespace(sysctl=Mock(return_value=0))
        with patch.object(daemon.sys, 'platform', 'darwin'), \
             patch.object(daemon.os, 'getuid', return_value=501), \
             patch.object(daemon.os, 'sysconf', return_value=262144), \
             patch.object(daemon.ctypes, 'CDLL', side_effect=lambda name, **kw: lib if name else libc), \
             patch.object(daemon, '_args', return_value=(['codex', 'resume', T], {})):
            return read_kernel_process(99, arguments=arguments, validate_argv=validate_argv)

    def test_only_initial_esrch_or_owned_zombie_is_skippable(self):
        for observation in ({'errno': errno.ESRCH}, {'status': 5}):
            with self.subTest(observation=observation), self.assertRaises(daemon.ProcessExited):
                self.process([observation])

    def test_initial_unknown_permission_foreign_or_invalid_are_not_skippable(self):
        for observation in ({'errno': 0}, {'errno': errno.EPERM}, {'errno': errno.EACCES},
                            {'uid': 0}, {'pid': 98}, {'start_sec': 0}):
            with self.subTest(observation=observation), self.assertRaises(daemon.IdentityError) as caught:
                self.process([observation])
            self.assertIs(type(caught.exception), daemon.IdentityError)

    def test_second_kernel_read_exit_or_reuse_is_not_skippable(self):
        for observation in ({'errno': errno.ESRCH}, {'status': 5}, {'start_sec': 101}):
            with self.subTest(observation=observation), self.assertRaises(daemon.IdentityError) as caught:
                self.process([{}, observation])
            self.assertIs(type(caught.exception), daemon.IdentityError)

    def test_raw_candidate_read_preserves_strict_default(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / 'codex'
            executable.write_bytes(b'fake executable identity')
            with self.assertRaisesRegex(daemon.IdentityError, 'argv executable'):
                self.process([{}], arguments=True, path=executable)
            result = self.process([{}, {}], arguments=True, validate_argv=False, path=executable)
            self.assertEqual(result['argv'][0], 'codex')
            self.assertEqual(result['executable'], str(executable.resolve()))


class GlobalDockTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DaemonTests()
        self.fixture.setUp()
        self.workspace = self.fixture.tree['windows'][0]['workspaces'][0]
        self.identity, self.env = self.fixture.resolve()
        self.dock = dict(ref='surface:65', id='DDDDDDDD-0000-4000-8000-000000000066',
                         type='terminal', title='Claude', tty='ttys65', dock_scope='global')
        self.workspace['panes'].append(dict(ref='pane:65',
            id='DDDDDDDD-0000-4000-8000-000000000065', surfaces=[self.dock]))

    def test_inventory_excludes_nested_global_dock(self):
        with patch.object(guard, 'caller_snapshot', return_value=(
                self.identity, self.fixture.tree, self.env, self.fixture.proof)):
            self.assertEqual([r['ref'] for r in bridge.list_surfaces()], ['surface:1', 'surface:2'])

    def test_global_target_denies_by_ref_or_uuid(self):
        for target in ('surface:65', self.dock['id']):
            with self.subTest(target=target), self.assertRaisesRegex(guard.WorkspaceScopeError, 'global dock'):
                guard.resolve_snapshot(self.identity, self.fixture.tree, target, env=self.env)

    def test_global_caller_denies(self):
        self.workspace['panes'][0]['surfaces'][0]['dock_scope'] = 'global'
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'global dock'):
            guard.resolve_snapshot(self.identity, self.fixture.tree, 'surface:2', env=self.env)

    def test_managed_caller_and_origin_cannot_bind_global_dock(self):
        for ws in self.fixture.tree['windows'][0]['workspaces']:
            surface = ws['panes'][0]['surfaces'][0]
            surface['dock_scope'] = 'global'
            with self.assertRaisesRegex(daemon.IdentityError, 'global dock'):
                self.fixture.resolve()
            del surface['dock_scope']

    def test_global_row_does_not_erase_duplicate_uuid_ambiguity(self):
        self.dock['id'] = C
        with self.assertRaisesRegex(daemon.IdentityError, 'UUIDs'):
            self.fixture.resolve()
        self.dock['id'] = T
        with self.assertRaisesRegex(guard.WorkspaceScopeError, 'ambiguous'):
            guard.resolve_snapshot(self.identity, self.fixture.tree, T, env=self.env)

    def test_workspace_scoped_dock_remains_local(self):
        self.dock['dock_scope'] = 'workspace'
        self.assertEqual(guard.resolve_snapshot(self.identity, self.fixture.tree,
            'surface:65', env=self.env)['target_surface_uuid'], self.dock['id'])


if __name__ == '__main__':
    unittest.main()
