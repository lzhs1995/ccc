import copy
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.standby_ui_activation import BoundActivation
from tools.standby_ui_pty import SupervisorPTY


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.batch = '00000000-0000-4000-8000-000000000001'
        self.workspace = '00000000-0000-4000-8000-000000000002'
        self.surface = '00000000-0000-4000-8000-000000000003'
        self.row = dict(workspace_id=self.workspace, surface_id=self.surface,
                        session_id='00000000-0000-4000-8000-000000000004',
                        pid=123, birth=[100, 456])
        self.tree = dict(windows=[dict(workspaces=[dict(id=self.workspace,
            ref='workspace:1', title='fixture', panes=[dict(ref='pane:2',
            surfaces=[dict(id=self.surface, ref='surface:3', type='terminal')])])])])
        self.owner = SimpleNamespace(status=lambda: dict(state='ready'),
            service=SimpleNamespace(preparation=SimpleNamespace(
                observe_for_activation=lambda index: copy.deepcopy(self.row))))
        self.handle = dict(opening={}, settlement=None,
                           runner=SimpleNamespace(owner=self.owner))
        self.invocation = SimpleNamespace(current=Mock(),
            value=dict(workspace_id=self.workspace, config_path='/fixture/config'))
        self.pool = SimpleNamespace(_health=Mock(), handles={self.batch: self.handle},
            specs={self.batch: (self.invocation, None)},
            expected={self.batch: dict(mode='b')})
        self.client = SimpleNamespace(tree=lambda: copy.deepcopy(self.tree))
        self.connector = patch('tools.standby_ui_activation.connect', return_value=self.client)
        self.connector.start()
        self.addCleanup(self.connector.stop)
        # Exercise actual SupervisorPTY phase/intent/partial-write semantics.
        # No child, terminal or model is started.
        self.ui = object.__new__(SupervisorPTY)
        self.ui.sent = set()
        self.ui.data = bytearray()
        self.ui.action_offset = None
        self.ui.master = 999
        self.ui.child = Mock(pid=12345)
        self.ui.child.poll.return_value = None
        self.ui.close = Mock()
        self.ui.poll = Mock(return_value=None)
        self.ui._record = Mock()
        self.writer = patch('tools.standby_ui_pty.os.write', return_value=1)
        self.write = self.writer.start()
        self.addCleanup(self.writer.stop)

    def driver(self):
        return BoundActivation(self.pool, self.batch, '/fixture/ui',
                               ui_factory=lambda *args: self.ui)

    def action(self, driver):
        self.ui.data.extend(driver.prefix)
        self.assertEqual(driver.poll(), 'action_written')
        self.ui.data.extend((driver.prompt+' [y/N]').encode())

    def test_success_once_and_no_settlement_claim(self):
        d = self.driver()
        self.assertEqual(d.poll(), 'waiting_focus')
        self.action(d)
        self.assertEqual(d.poll(), 'confirmation_written')
        self.assertEqual(d.poll(), 'confirmation_written')
        self.assertEqual(self.write.call_count, 2)
        self.assertIsNone(self.handle['settlement'])
        self.assertIs(self.handle['ui_activation'], d)

    def test_close_unknown_confirmation_signals_only_owned_ui_once(self):
        d = self.driver()
        self.action(d)
        self.write.side_effect = OSError('unknown')
        with self.assertRaises(OSError):
            d.poll()
        writes = self.write.call_count
        self.assertFalse(d.close())
        self.assertFalse(d.close())
        self.ui.child.terminate.assert_called_once()
        self.assertEqual(self.write.call_count, writes)
        self.ui.close.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'closing'):
            d.poll()
        self.ui.child.poll.return_value = 0
        self.assertTrue(d.close())
        self.assertTrue(d.close())
        self.ui.close.assert_called_once()

    def test_close_rejects_replaced_child(self):
        d = self.driver()
        original = self.ui.child
        self.ui.child = Mock()
        with self.assertRaisesRegex(ValueError, 'handle changed'):
            d.close()
        original.terminate.assert_not_called()
        self.ui.child.terminate.assert_not_called()

    def test_close_log_failure_never_retries_signal(self):
        d = self.driver()
        self.ui._record.side_effect = OSError('disk full')
        with self.assertRaises(OSError):
            d.close()
        self.assertFalse(d.close())
        self.ui.child.terminate.assert_not_called()
        self.ui.child.poll.return_value = 0
        with self.assertRaisesRegex(ValueError, 'evidence failed'):
            d.close()
        self.ui.close.assert_not_called()

    def test_real_retained_child_exit_leaves_unrelated_child_alive(self):
        children = []
        try:
            for _ in range(2):
                children.append(subprocess.Popen(['/bin/sleep', '30'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL))
            owned, unrelated = children
            self.ui.child = owned
            self.ui.poll.side_effect = lambda timeout: owned.poll()
            d = self.driver()
            d.close()
            owned.wait(timeout=5)
            self.assertTrue(d.close())
            self.assertIsNone(unrelated.poll())
            self.write.assert_not_called()
            self.ui.close.assert_called_once()
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)

    def test_access_mode_uses_original_prompt_and_key(self):
        self.pool.expected[self.batch]['mode'] = 'N'
        d = self.driver()
        self.action(d)
        self.assertEqual(self.write.call_args.args[1], b'N')
        self.assertEqual(d.poll(), 'confirmation_written')
        self.assertEqual(self.write.call_args.args[1], b'y')

    def test_unknown_action_never_confirmed_or_retried(self):
        d = self.driver()
        self.ui.data.extend(d.prefix)
        self.write.return_value = 0
        with self.assertRaises(OSError):
            d.poll()
        self.ui.data.extend((d.prompt+' [y/N]').encode())
        with self.assertRaisesRegex(ValueError, 'do not retry'):
            d.poll()
        self.assertEqual(self.write.call_count, 1)

    def test_unknown_confirmation_never_reported_written(self):
        d = self.driver()
        self.action(d)
        self.write.side_effect = OSError('write uncertain')
        with self.assertRaises(OSError):
            d.poll()
        with self.assertRaisesRegex(ValueError, 'do not retry'):
            d.poll()
        self.assertEqual(self.write.call_count, 2)
        self.assertNotIn('confirmation', d.written)

    def test_log_failure_after_confirmation_write_is_unknown(self):
        d = self.driver()
        self.action(d)
        def record(kind, **fields):
            if kind == 'input_written':
                raise OSError('fsync failed')
        self.ui._record.side_effect = record
        with self.assertRaises(OSError):
            d.poll()
        with self.assertRaises(ValueError):
            d.poll()
        self.assertEqual(self.write.call_count, 2)
        self.assertNotIn('confirmation', d.written)

    def test_identity_change_before_action(self):
        d = self.driver()
        self.ui.data.extend(d.prefix)
        self.row['birth'][1] += 1
        with self.assertRaisesRegex(ValueError, 'target changed'):
            d.poll()
        self.write.assert_not_called()

    def test_identity_change_before_confirmation(self):
        d = self.driver()
        self.action(d)
        self.row['pid'] += 1
        with self.assertRaisesRegex(ValueError, 'target changed'):
            d.poll()
        self.assertEqual(self.write.call_count, 1)

    def test_moved_target_rejected(self):
        d = self.driver()
        self.ui.data.extend(d.prefix)
        self.tree['windows'][0]['workspaces'][0]['panes'][0]['ref'] = 'pane:9'
        with self.assertRaisesRegex(ValueError, 'moved'):
            d.poll()
        self.write.assert_not_called()

    def test_not_ready_rejected(self):
        d = self.driver()
        self.action(d)
        self.owner.status = lambda: dict(state='invalidated')
        with self.assertRaisesRegex(ValueError, 'not ready'):
            d.poll()
        self.assertEqual(self.write.call_count, 1)

    def test_prompt_before_action_or_foreign_prompt_does_not_confirm(self):
        d = self.driver()
        self.ui.data.extend((d.prompt+' [y/N]').encode()+d.prefix)
        self.assertEqual(d.poll(), 'action_written')
        self.ui.data.extend(b'foreign [y/N]')
        self.assertEqual(d.poll(), 'waiting_confirmation')
        self.assertEqual(self.write.call_count, 1)

    def test_exit_before_confirmation(self):
        d = self.driver()
        self.action(d)
        self.ui.poll.return_value = 0
        with self.assertRaisesRegex(ValueError, 'exited'):
            d.poll()
        self.assertEqual(self.write.call_count, 1)

    def test_foreign_input_attempt_rejected(self):
        d = self.driver()
        self.ui.sent.add('confirmation')
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            d.poll()
        self.write.assert_not_called()

    def test_invalid_mode_before_spawn(self):
        self.pool.expected[self.batch]['mode'] = 'B'
        with self.assertRaisesRegex(ValueError, 'modes'):
            self.driver()
        self.assertNotIn('ui_activation_consumed', self.handle)

    def test_spawn_failure_retained_and_consumed(self):
        with self.assertRaises(OSError):
            BoundActivation(self.pool, self.batch, '/fixture/ui',
                            ui_factory=Mock(side_effect=OSError('spawn failed')))
        d = self.handle['ui_activation']
        self.assertEqual(d.failure, 'OSError')
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            self.driver()


if __name__ == '__main__':
    unittest.main()
