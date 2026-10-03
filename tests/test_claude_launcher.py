import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import ccc_claude_launcher as launcher


class ClaudeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ccc-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.observer = self.root / 'observer with space.cjs'
        self.observer.write_text('')
        self.directory = self.root / 'observations'

    def test_only_instrumentation_environment_changes(self):
        env = {'ANTHROPIC_API_KEY': 'fake-api', 'ANTHROPIC_AUTH_TOKEN': 'fake-auth',
               'ANTHROPIC_BASE_URL': 'http://127.0.0.1:1', 'CLAUDE_CONFIG_DIR': '/profiles/one',
               'BUN_OPTIONS': '--smol', 'CMUX_SURFACE_ID': 'surface-original'}
        actual = launcher.observation_environment(env, self.observer, self.directory)
        self.assertEqual({k:actual[k] for k in env if k != 'BUN_OPTIONS'},
                         {k:v for k,v in env.items() if k != 'BUN_OPTIONS'})
        self.assertTrue(actual['BUN_OPTIONS'].startswith('--smol --preload '))
        self.assertEqual(launcher.observation_environment(actual, self.observer, self.directory), actual)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(shutil.which('bun'), 'Bun runtime integration')
    def test_bun_actually_preloads_path_with_spaces(self):
        self.observer.write_text("globalThis.CCC_TEST_PRELOADED = 'loaded';")
        env = launcher.observation_environment({}, self.observer, self.directory)
        completed = subprocess.run([shutil.which('bun'), '-e', 'console.log(globalThis.CCC_TEST_PRELOADED)'],
                                   env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), 'loaded')

    def test_insecure_directory_rejected_without_chmod(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            launcher.observation_environment({}, self.observer, self.directory)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o755)

    def test_symlink_observer_or_directory_rejected(self):
        link = self.root / 'link'
        link.symlink_to(self.observer)
        with self.assertRaises(ValueError):
            launcher.observation_environment({}, link, self.directory)
        self.directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            launcher.observation_environment({}, self.observer, self.directory)

    def test_selected_binary_arguments_and_credentials_preserved(self):
        args = ['/fixed/claude', '--settings', '/my profile.json', '--resume', 'original-session']
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fake-original'}, clear=True), \
                patch.object(launcher, 'observation_environment', return_value={'unchanged': 'env'}) as env, \
                patch.object(launcher.os, 'execve') as execute:
            launcher.main(['--', *args])
            self.assertEqual(env.call_args.args[0]['ANTHROPIC_API_KEY'], 'fake-original')
        execute.assert_called_once_with(args[0], args, {'unchanged': 'env'})

    def test_observer_failure_still_executes_exact_selected_command(self):
        with patch.dict(os.environ, {'ANTHROPIC_AUTH_TOKEN': 'fake-auth'}, clear=True), \
                patch.object(launcher, 'observation_environment', side_effect=OSError('disk')), \
                patch.object(launcher.os, 'execve') as execute:
            launcher.main(['/pinned/claude', '-c'])
        execute.assert_called_once_with('/pinned/claude', ['/pinned/claude', '-c'],
                                       {'ANTHROPIC_AUTH_TOKEN': 'fake-auth'})

    @unittest.skipUnless(Path('/bin/zsh').exists(), 'zsh integration')
    def test_plain_and_pinned_shell_dispatch(self):
        directory = self.root / 'bin with space'
        directory.mkdir()
        fake = directory / 'claude'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@"\n')
        fake.chmod(0o700)
        pinned = directory / 'pinned'
        pinned.write_text(fake.read_text()); pinned.chmod(0o700)
        shell = self.root / 'observed.zsh'
        shell.write_text(launcher.render_shell(sys.executable, Path(launcher.__file__).resolve()))
        env = dict(os.environ, PATH=str(directory) + ':/usr/bin:/bin',
                   CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR=str(self.directory))
        command = 'source "$1"; claude --resume "original sid"; _ccc_claude_observed "$2" --settings "$3"'
        profile = '/profiles/one $(false); .json'
        completed = subprocess.run(['/bin/zsh', '-f', '-c', command, 'fixture', str(shell), str(pinned), profile],
                                   env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(),
                         [str(fake), '--resume', 'original sid', str(pinned), '--settings', profile])


if __name__ == '__main__':
    unittest.main()
