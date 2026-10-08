"""Actual private PTY, harmless child; not a real Supervisor/UI acceptance."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from tools.standby_ui_pty import SupervisorPTY


class PTYTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = self.root/'config.json'; config.write_text('{}')
        popen = subprocess.Popen
        code = ('import os,tty; tty.setraw(0); print("READY",flush=True); '
                'key=os.read(0,1); print("bound workspace [y/N]",flush=True); '
                'key=os.read(0,1); print("ACCEPTED",flush=True); os.read(0,1)')
        def child(command, **kwargs):
            return popen([sys.executable, '-B', '-c', code], **kwargs)
        with patch('tools.standby_ui_pty.subprocess.Popen', side_effect=child):
            self.ui = SupervisorPTY(config, str(uuid.uuid4()), self.root/'ui')
        self.wait(b'READY')

    def wait(self, text):
        deadline = time.monotonic()+5
        while text not in self.ui.data and time.monotonic() < deadline:
            self.ui.poll(.05)
        self.assertIn(text, self.ui.data)

    def tearDown(self):
        # The harmless test child is disposable; product driver has no signals.
        if self.ui.child.poll() is None:
            self.ui.child.kill()
        self.ui.child.wait(timeout=5)
        self.ui.close()
        self.temp.cleanup()

    def test_actual_bytes_and_original_records(self):
        self.ui.press_action('b'); self.wait(b'[y/N]')
        self.ui.confirm('bound workspace'); self.wait(b'ACCEPTED')
        self.ui.quit(); self.ui.child.wait(timeout=5)
        rows = [json.loads(x) for x in (self.root/'ui/events.jsonl').read_text().splitlines()]
        self.assertEqual([r['key'] for r in rows if r['kind']=='input_intent'], ['b','y','q'])
        self.assertIn(b'bound workspace', (self.root/'ui/output.bin').read_bytes())

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin process generation')
    def test_resource_identity_is_original_real_child(self):
        from ccc_guard_scope import birth
        row = self.ui.resource_identity()
        self.assertEqual(row, dict(pid=self.ui.child.pid, birth=birth(self.ui.child.pid)))
        row['birth'][0] = 0
        self.assertGreater(self.ui.resource_identity()['birth'][0], 0)

    def test_resource_identity_never_rebases_missing_startup(self):
        self.ui._resource_birth = None
        with self.assertRaisesRegex(ValueError, 'original live'):
            self.ui.resource_identity()

    def test_resource_identity_rejects_generation_drift(self):
        self.ui._resource_birth = [123, 456]
        with patch('ccc_guard_scope.birth', return_value=[124, 456]):
            with self.assertRaisesRegex(ValueError, 'original live'):
                self.ui.resource_identity()

    def test_resource_identity_rejects_replaced_child_handle(self):
        original = self.ui.child
        try:
            self.ui.child = object()
            with self.assertRaisesRegex(ValueError, 'original live'):
                self.ui.resource_identity()
        finally:
            self.ui.child = original

    def test_resource_identity_rejects_exit_during_read(self):
        self.ui._resource_birth = [123, 456]
        def exited(pid):
            self.ui.child.kill()
            self.ui.child.wait(timeout=5)
            return [123, 456]
        with patch('ccc_guard_scope.birth', side_effect=exited):
            with self.assertRaisesRegex(ValueError, 'original live'):
                self.ui.resource_identity()

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin resource observation')
    def test_real_ui_auxiliary_rss_and_descriptors(self):
        from ccc_guard_scope import birth
        from tools.standby_resource_sample import capture
        # The caller stands in for one native row; no Codex session is started.
        # The UI itself is a real private PTY child, sampled through production
        # ps/libproc readers rather than a synthetic PID/RSS fixture.
        caller = dict(pid=os.getpid(), birth=birth(os.getpid()))
        ui = self.ui.resource_identity()
        result = capture([caller], self.root.resolve(), expected_count=1,
                         auxiliaries=[ui])
        row, = [r for r in result['processes'] if r['role'] == 'auxiliary']
        self.assertEqual(row['pid'], ui['pid'])
        self.assertEqual(row['birth'], ui['birth'])
        self.assertGreater(row['rss_bytes'], 0)
        self.assertGreaterEqual(row['fd_count'], 3)
        self.assertEqual(result['auxiliary_process_count'], 1)
        self.assertFalse(result['full_500_acceptance'])
        self.assertFalse(result['peak_usage_proven'])
        self.assertEqual(self.ui.resource_identity(), ui)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin resource observation')
    def test_ui_exits_after_binding_before_collector(self):
        from ccc_guard_scope import birth
        from tools.standby_resource_sample import capture
        caller = dict(pid=os.getpid(), birth=birth(os.getpid()))
        ui = self.ui.resource_identity()
        self.ui.child.kill()
        self.ui.child.wait(timeout=5)
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            capture([caller], self.root.resolve(), expected_count=1,
                    auxiliaries=[ui])

    def test_high_descriptor_pty_input_and_drain(self):
        high = fcntl.fcntl(self.ui.master, fcntl.F_DUPFD, 2048)
        os.close(self.ui.master)
        self.ui.master = high
        self.ui.press_action('b'); self.wait(b'[y/N]')
        self.ui.confirm('bound workspace'); self.wait(b'ACCEPTED')
        self.ui.quit(); self.ui.child.wait(timeout=5)
        self.ui.poll(.01)

    def test_wrong_workspace_never_confirmed(self):
        self.ui.press_action('N'); self.wait(b'[y/N]')
        with self.assertRaisesRegex(ValueError, 'not observed'):
            self.ui.confirm('other workspace')
        self.assertNotIn('confirmation', self.ui.sent)

    def test_old_prompt_cannot_confirm(self):
        self.ui.data.extend(b'old workspace [y/N]')
        self.ui.press_action('b')
        with self.assertRaisesRegex(ValueError, 'not observed'):
            self.ui.confirm('old workspace')

    def test_no_escape_or_interrupt_key(self):
        for key in ('\x1b', '\x03', 'P', 'B'):
            with self.assertRaises(ValueError): self.ui.press_action(key)
        self.assertEqual(self.ui.sent, set())

    def test_live_child_not_closed_on_timeout(self):
        self.ui.poll(.01)
        with self.assertRaisesRegex(ValueError, 'still alive'): self.ui.close()
        self.assertIsNone(self.ui.child.poll())

    def test_unknown_write_never_replayed(self):
        with patch('tools.standby_ui_pty.os.write', side_effect=OSError('unknown')):
            with self.assertRaises(OSError): self.ui.press_action('b')
        with self.assertRaisesRegex(ValueError, 'consumed'): self.ui.press_action('b')

    def test_confirmation_never_repeated(self):
        self.ui.press_action('b'); self.wait(b'[y/N]')
        self.ui.confirm('bound workspace'); self.wait(b'ACCEPTED')
        with self.assertRaisesRegex(ValueError, 'consumed'): self.ui.confirm('bound workspace')

    def test_unconfirmed_private_child_can_exit_without_input(self):
        self.ui.press_action('b'); self.wait(b'[y/N]')
        self.ui.stop_unconfirmed()
        self.ui.child.wait(timeout=5)
        self.assertEqual(self.ui.sent, {'action'})

    def test_confirmed_child_cannot_be_stopped_as_unconfirmed(self):
        self.ui.press_action('b'); self.wait(b'[y/N]')
        self.ui.confirm('bound workspace'); self.wait(b'ACCEPTED')
        with self.assertRaisesRegex(ValueError, 'confirmation consumed'):
            self.ui.stop_unconfirmed()
        self.assertIsNone(self.ui.child.poll())


if __name__ == '__main__':
    unittest.main()
