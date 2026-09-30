import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import ccc_standby_environment as env


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ccc-env-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.workspace, self.surface = str(uuid.uuid4()), str(uuid.uuid4())
        self.base = {'HOME': str(self.root), 'CODEX_HOME': str(self.root / 'codex'),
                     'PATH': '/target/bin', 'API_KEY': 'test-only-value',
                     'CMUX_SOCKET_PATH': '/target/socket', 'CMUX_SURFACE_ID': str(uuid.uuid4()),
                     'CMUX_WORKSPACE_ID': str(uuid.uuid4()), 'CODEX_THREAD_ID': 'parent-thread',
                     'CMUX_AGENT_LAUNCH_ARGV_B64': 'parent-argv', 'CMUX_CODEX_PID': '100'}
        self.child = {'HOME': '/wrong', 'API_KEY': 'wrong', 'PATH': '/wrong',
                      'CMUX_SOCKET_PATH': '/wrong', 'NEW_CREDENTIAL': 'wrong',
                      'CMUX_SURFACE_ID': self.surface, 'CMUX_WORKSPACE_ID': self.workspace,
                      'CMUX_TERMINAL_LIFECYCLE_ID': 'new-terminal', 'CMUX_PORT': '12000'}
        self.binding = {'job_id': str(uuid.uuid4()), 'generation': 'a' * 64}

    def compose(self, **kwargs):
        return env.compose(self.base, self.child, workspace_id=self.workspace,
            surface_id=self.surface, cwd=self.root, tui_log=self.root / 'events', **kwargs)

    def make_file(self):
        return env.EnvironmentFile.create(self.root / 'environment.json',
            binding=self.binding, environment=self.base)

    def test_fixed_credentials_and_configuration_survive_new_shell(self):
        result = self.compose()
        for key in ('HOME', 'CODEX_HOME', 'PATH', 'API_KEY', 'CMUX_SOCKET_PATH'):
            self.assertEqual(result[key], self.base[key])
        self.assertNotIn('NEW_CREDENTIAL', result)
        self.assertEqual(result['CMUX_SURFACE_ID'], self.surface)
        self.assertEqual(result['CMUX_WORKSPACE_ID'], self.workspace)
        self.assertEqual(result['CMUX_TERMINAL_LIFECYCLE_ID'], 'new-terminal')
        self.assertEqual(result['TOKIO_WORKER_THREADS'], '2')
        self.assertEqual(result['PWD'], str(self.root))

    def test_parent_agent_identity_is_not_reused(self):
        result = self.compose()
        for key in ('CODEX_THREAD_ID', 'CMUX_CODEX_PID', 'CMUX_AGENT_LAUNCH_ARGV_B64'):
            self.assertNotIn(key, result)

    def test_wrong_or_missing_terminal_identity_is_rejected(self):
        for key in ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID'):
            original = self.child.pop(key)
            with self.assertRaises(ValueError):
                self.compose()
            self.child[key] = str(uuid.uuid4())
            with self.assertRaises(ValueError):
                self.compose()
            self.child[key] = original

    def test_envelope_private_bound_and_detached(self):
        selected = self.make_file()
        self.assertEqual(selected.path.stat().st_mode & 0o777, 0o600)
        result = selected.current()
        result['API_KEY'] = 'mutated'
        self.assertEqual(selected.current()['API_KEY'], 'test-only-value')
        self.base['API_KEY'] = 'changed-caller'
        self.assertEqual(selected.current()['API_KEY'], 'test-only-value')
        with self.assertRaises(ValueError):
            env.EnvironmentFile(selected.path, selected.sha256, {**self.binding, 'job_id': str(uuid.uuid4())})

    def test_observed_content_change_is_permanent(self):
        selected = self.make_file()
        raw = selected.path.read_bytes()
        selected.path.write_bytes(raw.replace(b'test-only-value', b'test-only-other'))
        with self.assertRaises(ValueError):
            selected.current()
        selected.path.write_bytes(raw)
        with self.assertRaises(ValueError):
            selected.current()

    def test_replaced_inode_is_rejected(self):
        selected = self.make_file()
        raw = selected.path.read_bytes()
        selected.path.unlink()
        selected.path.write_bytes(raw)
        selected.path.chmod(0o600)
        with self.assertRaises(ValueError):
            selected.current()

    def test_symlink_hardlink_or_public_permissions_rejected(self):
        for mode in ('symlink', 'hardlink', 'permissions'):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir(mode=0o700)
                selected = env.EnvironmentFile.create(directory / 'environment.json',
                    binding=self.binding, environment=self.base)
                if mode == 'permissions':
                    selected.path.chmod(0o644)
                elif mode == 'hardlink':
                    os.link(selected.path, directory / 'second')
                else:
                    selected.path.rename(directory / 'original')
                    selected.path.symlink_to(directory / 'original')
                with self.assertRaises(ValueError):
                    selected.current()

    def test_original_parent_replacement_is_rejected(self):
        directory = self.root / 'private'
        directory.mkdir(mode=0o700)
        selected = env.EnvironmentFile.create(directory / 'environment.json',
            binding=self.binding, environment=self.base)
        directory.rename(self.root / 'moved')
        directory.symlink_to(self.root / 'moved', target_is_directory=True)
        with self.assertRaises(ValueError):
            selected.current()

    def test_invalid_environment_and_bounds(self):
        for changes in ({'HOME': 'relative'}, {'CODEX_HOME': 'relative'}, {'BAD=NAME': 'x'},
                        {'KEY': 'a\0b'}, {'KEY': 1}, {'KEY': 'a' * env.LIMIT}):
            with self.subTest(keys=list(changes)), self.assertRaises(ValueError):
                env.template({**self.base, **changes})

    def test_envelope_cannot_be_recreated(self):
        self.make_file()
        with self.assertRaises(FileExistsError):
            self.make_file()


if __name__ == '__main__':
    unittest.main()
