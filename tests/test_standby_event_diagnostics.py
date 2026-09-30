"""Bounded diagnostics preserve the event latch; no real configuration scans."""
import select
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ccc_standby_generation import _VnodeWatch


@unittest.skipUnless(hasattr(select, 'kqueue'), 'Darwin vnode events')
class EventDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.watch = _VnodeWatch({str(self.root): 'identity'}, 1)
        self.addCleanup(self.watch.close)
        self.event = select.kevent(self.watch._fds[0], filter=select.KQ_FILTER_VNODE,
                                   fflags=select.KQ_NOTE_WRITE)

    def test_continuously_busy_ancestor_does_not_require_quiescence(self):
        queue = Mock()
        queue.control.return_value = [self.event]
        with patch.object(self.watch, '_queue', queue):
            for _ in range(5):
                self.watch.check()
        self.assertEqual(queue.control.call_count, 5)
        self.assertFalse(self.watch._invalid)

    def test_benign_event_then_empty_remains_valid(self):
        queue = Mock()
        queue.control.side_effect = [[self.event], []]
        with patch.object(self.watch, '_queue', queue):
            self.watch.check()
        self.assertFalse(self.watch._invalid)

    def test_permission_event_still_rejects_immediately(self):
        queue = Mock()
        queue.control.return_value = [select.kevent(self.watch._fds[0],
            filter=select.KQ_FILTER_VNODE, fflags=select.KQ_NOTE_ATTRIB)]
        with patch.object(self.watch, '_queue', queue):
            with self.assertRaisesRegex(ValueError, 'event observed') as raised:
                self.watch.check()
        self.assertEqual(raised.exception.dependency_event['path'], str(self.root))
        self.assertEqual(queue.control.call_count, 1)
        first = self.watch.failure_diagnostic.copy()
        with self.assertRaisesRegex(ValueError, 'invalidated'):
            self.watch.check()
        self.assertEqual(self.watch.failure_diagnostic, first)

    def test_mutation_beyond_old_batch_size_cannot_be_left_unread(self):
        pending = [self.event] * 4096 + [select.kevent(self.watch._fds[0],
            filter=select.KQ_FILTER_VNODE, fflags=select.KQ_NOTE_ATTRIB)]
        def read(changes, maximum, timeout):
            result = pending[:maximum]
            del pending[:maximum]
            return result
        queue = Mock()
        queue.control.side_effect = read
        # Simulated 4097 registrations; returned events use our real test fd.
        with patch.object(self.watch, '_fds', [self.watch._fds[0]] * 4097), \
                patch.object(self.watch, '_queue', queue):
            with self.assertRaisesRegex(ValueError, 'event observed'):
                self.watch.check()
        self.assertEqual(pending, [])


if __name__ == '__main__':
    unittest.main()
