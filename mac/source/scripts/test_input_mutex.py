"""Real flock contention and probe dispatch entrypoint regression coverage."""
import fcntl
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_input_mutex as mutex
import mac_harness as harness


class InputMutexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        home = patch.object(Path, 'home', return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.identity = dict(workspace_uuid='WS', target_pane_uuid='PANE', target_surface_uuid='CLAUDE')

    def test_matches_existing_formal_delivery_locks(self):
        from cmux_task_journal import digest
        root = self.home / '.local/state/multi-agent-collaboration/deliveries-v1'
        self.assertEqual(mutex.lock_paths(self.identity),
                         (root / ('pane-' + digest('WS:PANE') + '.lock'),
                          root / ('target-' + digest('CLAUDE') + '.lock')))

    def test_same_pane_other_surface_conflicts_and_lock_releases(self):
        other = dict(self.identity, target_surface_uuid='SECOND')
        with mutex.hold(self.identity):
            with self.assertRaisesRegex(mutex.InputMutexError, 'INPUT_CHANNEL_BUSY'):
                with mutex.hold(other):
                    self.fail('second pane writer acquired')
        with mutex.hold(other) as check:
            check()

    def test_replaced_inode_is_rejected_and_not_unlinked(self):
        with mutex.hold(self.identity) as check:
            path = mutex.lock_paths(self.identity)[0]
            path.rename(path.with_suffix('.original'))
            path.touch()
            with self.assertRaisesRegex(mutex.InputMutexError, 'INPUT_MUTEX_REPLACED'):
                check()
        self.assertTrue(path.exists())
        self.assertTrue(path.with_suffix('.original').exists())

    def test_symlink_is_rejected(self):
        path = mutex.lock_paths(self.identity)[0]
        path.parent.mkdir(parents=True)
        source = self.home / 'original'
        source.touch()
        path.symlink_to(source)
        with self.assertRaises((OSError, ValueError)):
            with mutex.hold(self.identity):
                self.fail('symlink lock acquired')

    def test_probe_cannot_read_type_or_clear_while_another_writer_owns_pane(self):
        with mutex.hold(self.identity), \
                patch.object(harness.cmux, 'pin_workspace', return_value=self.identity), \
                patch.object(harness.cmux, 'read_screen') as read, \
                patch.object(harness.cmux, 'send_text') as paste, \
                patch.object(harness.cmux, 'send_key') as key:
            with self.assertRaisesRegex(mutex.InputMutexError, 'INPUT_CHANNEL_BUSY'):
                harness._bridge_test_one(SimpleNamespace(task_id='probe', lines=100), 'peer', 'B1_1234', 1)
            read.assert_not_called()
            paste.assert_not_called()
            key.assert_not_called()

    def test_probe_holds_both_shared_locks_for_entire_transaction(self):
        def inner(*args):
            for path in mutex.lock_paths(self.identity):
                with path.open('a+b') as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {'clear_confirmed': True}, True
        with patch.object(harness.cmux, 'pin_workspace', return_value=self.identity), \
                patch.object(harness, '_bridge_test_one_locked', side_effect=inner):
            self.assertTrue(harness._bridge_test_one(None, 'peer', 'B1_1234', 1)[1])


if __name__ == '__main__':
    unittest.main()
