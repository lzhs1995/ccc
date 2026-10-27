"""The parent and independent lane retain exact identity as rollouts open/close."""
import unittest

from ccc_codex_queue import QueueRecovery
from ccc_native_lanes import _binding_matches
from tests import test_codex_idle_session as idle
from tests import test_native_dispatch_ownership as ownership


class IdleNativeLaneTests(unittest.TestCase):
    def test_original_idle_open_idle_cycle_retains_cell_and_child_identity(self):
        parent = ownership.OwnershipTests('test_parent_is_excluded_before_child_can_receive_assignment')
        parent.setUp()
        self.addCleanup(parent.doCleanups)
        reader = idle.IdleSessionTests('test_closed_rollout_with_current_writer_lock_supplies_failed_turn')
        reader.setUp()
        self.addCleanup(reader.doCleanups)
        reader.target = parent.targets[0]
        reader.process.update(surface_id=reader.target['surface_id'],
                              environment_workspace_id=reader.target['workspace_id'])
        parent.daemon.codex_queue_recovery = reader.queue
        reader.queue.current_turn(reader.target)
        parent.refresh()
        assignment = parent.dispatch.owned[parent.sid]['assignment']
        self.assertEqual(assignment['binding']['birth'], [100, 100])
        child = QueueRecovery(reader.root / 'child-ledger', reader.root / 'missing-hooks',
                              reader.sessions, 'continue')
        child.process_lookup = reader.queue.process_lookup
        for opened in (False, True, True, True, False):
            with self.subTest(opened=opened):
                if opened:
                    reader.files[reader.path] = reader.identity(reader.path)
                else:
                    reader.files.pop(reader.path, None)
                child.open_file_cache.clear()
                turn = child.current_turn(reader.target)
                self.assertEqual(turn['kind'], 'task_complete')
                self.assertTrue(_binding_matches(turn, assignment['binding']))
                parent.refresh()
                self.assertEqual(parent.dispatch.cells.shared()[assignment['cell']], 1)
                self.assertEqual(len(parent.dispatch.lanes[0]['lane'].assignments), 1)
        reader.files[reader.path] = reader.identity(reader.path)
        reader.process['birth'] = [100, 101]
        child.open_file_cache.clear()
        self.assertFalse(_binding_matches(child.current_turn(reader.target), assignment['binding']))


if __name__ == '__main__':
    unittest.main()
