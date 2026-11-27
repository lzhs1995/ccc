"""Real packaged service in a private empty fleet with inert host executables."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / 'integrations/herdr/manage.py'


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin native process reader')
class PublicHerdrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.discovery = self.base / 'empty'
        self.discovery.mkdir(mode=0o700)
        self.instance = self.base / 'instance'

    def cli(self, action, *args, ok=True):
        p = subprocess.run([sys.executable, '-B', str(MANAGER), action,
                            '--instance', str(self.instance), *map(str, args)],
                           text=True, capture_output=True, timeout=45)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout.splitlines()[-1])
        self.assertNotEqual(p.returncode, 0)
        return p.stderr

    def install(self, *args, ok=True):
        return self.cli('install', '--herdr', '/usr/bin/false', '--cmux', '/usr/bin/false',
                        '--discovery-root', self.discovery, *args, ok=ok)

    def test_first_install_verify_cycle_and_rollback(self):
        self.assertFalse(self.install()['armed'])
        self.assertTrue(self.cli('verify')['verified'])
        result = self.cli('once')
        self.assertEqual((result['mode'], result['input_operations']), ('read_only', 0))
        self.assertIsNone(result['observation_error'])
        status = self.instance / 'state/status.json'
        self.assertTrue(status.is_file())
        with (self.instance / 'manager.lock').open('rb') as fd:
            fcntl.flock(fd, fcntl.LOCK_SH)
            self.cli('rollback', ok=False)
        self.assertTrue(self.cli('rollback')['ledgers_retained'])
        self.assertTrue(status.exists())
        self.cli('once', ok=False)
        self.assertFalse(self.cli('rollback')['active'])

    def test_armed_shares_control_and_keeps_original_attempts(self):
        self.install('--arm', ok=False)
        control = self.base / 'config.json'
        config = json.loads((ROOT / 'config.example.json').read_bytes())
        config.update(mode='armed', discovery_excluded_surface_ids=[])
        control.write_text(json.dumps(config))
        self.assertTrue(self.install('--arm', '--control', control)['armed'])
        result = self.cli('once')
        self.assertEqual((result['mode'], result['input_operations']), ('armed', 0))
        self.assertIsNone(result['observation_error'])
        owner_state = control.parent / 'herdr-observer/state'
        ledger = owner_state / 'herdr-codex-input-ledger.json'
        ledger.write_text('{"attempts":{"original":{"phase":"write_unknown"}}}')
        before = ledger.read_bytes()
        self.cli('rollback')
        self.assertEqual(ledger.read_bytes(), before)
        self.instance = self.base / 'second'
        self.assertIn('existing observer', self.install('--arm', '--control', control, ok=False))
        self.assertEqual(ledger.read_bytes(), before)

    def test_tampered_dependency_fails_verification(self):
        self.install()
        script, = (self.instance / 'runtime').glob('*/ccc_compact_runtime.py')
        script.write_bytes(script.read_bytes() + b'\n# changed\n')
        self.cli('verify', ok=False)
