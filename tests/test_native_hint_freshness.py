"""An old process index must not suppress a newer authenticated native source."""
import unittest
from unittest.mock import Mock, patch

from ccc_native_processes import NativeProcessIndex
from tests import test_native_dispatch_ownership as ownership
from tests.test_native_discovery_backoff import ControlledPool


class NativeHintFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ownership.OwnershipTests('test_parent_is_excluded_before_child_can_receive_assignment')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.source.update(session_id='new-session', birth=[1, 100])
        self.reader = Mock(side_effect=lambda _: {**f.source, 'kind': 'task_started'})
        f.daemon.codex_queue_recovery.current_turn = self.reader
        self.pool = ControlledPool()
        f.dispatch._discovery_pool = self.pool
        self.now = 0.0
        self.rows = [{'surface_id': f.sid, 'environment_workspace_id': f.targets[0]['workspace_id'],
                      'pid': 1, 'birth': [1, 100],
                      'foreground_generation': ('ok', 'old-session', 'same-client')}]
        self.index = NativeProcessIndex(loader=lambda: self.rows, clock=lambda: self.now)
        f.daemon._native_process_index = self.index
        self.births = {1: [1, 100]}
        self.placements = {1: 1}
        self.foreground = self.mock('ccc_client_thread_observation.read_foreground',
                                    return_value=('ok', 'new-session', 'same-client'))
        self.birth = self.mock('ccc_guard_scope.birth', side_effect=lambda pid, **kw: self.births.get(pid))
        self.placement = self.mock('ccc_codex_queue.process_placement_start',
                                   side_effect=lambda pid, target: self.placements.get(pid))
        self.mock('ccc_guard_scope.scan', side_effect=AssertionError('no full process scan'))
        self.mock('ccc_codex_queue.process_writable_files', side_effect=AssertionError('no writer scan'))

    def mock(self, name, **kwargs):
        fixture = patch(name, **kwargs)
        result = fixture.start()
        self.addCleanup(fixture.stop)
        return result

    def admit(self):
        f = self.fixture
        f.refresh()
        self.assertTrue(f.dispatch.owns(f.sid))
        return f.dispatch.owned[f.sid]['assignment']

    def test_old_index_cannot_delay_new_source_until_expiry(self):
        f = self.fixture
        self.index.refresh()
        assignment = self.admit()
        for at in (.1, .4, 1.0, 3.0, 4.9):
            self.now = at
            f.refresh()
            self.assertEqual(f.dispatch.cells.shared()[assignment['cell']], 1)
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(self.pool.calls, [])

    def test_delayed_old_snapshot_cannot_revoke_already_admitted_source(self):
        original = {}
        def delayed_publication():
            captured = list(self.rows)
            self.now = .1
            original.update(self.admit())
            return captured
        self.index.loader = delayed_publication
        self.index.refresh()
        self.now = .2
        f = self.fixture
        f.refresh()
        self.assertEqual(f.dispatch.cells.shared()[original['cell']], 1)
        self.assertEqual(len(f.dispatch.lanes[0]['lane'].assignments), 1)

    def test_agreeing_hint_needs_no_additional_process_or_foreground_read(self):
        self.rows[0]['foreground_generation'] = ('ok', 'new-session', 'same-client')
        self.index.refresh()
        self.admit()
        self.assertEqual(self.birth.call_count, 2)
        self.assertEqual(self.placement.call_count, 2)
        self.foreground.assert_not_called()
        self.birth.reset_mock()
        self.placement.reset_mock()
        self.fixture.refresh()
        self.birth.assert_not_called()
        self.placement.assert_not_called()
        self.foreground.assert_not_called()

    def test_old_pid_dead_or_moved_cannot_evict_current_source(self):
        for old_birth in (None, [2, 100]):
            with self.subTest(old_birth=old_birth):
                self.rows[0].update(pid=2, birth=[2, 100])
                self.births[2] = old_birth
                self.placements[2] = None
                self.index.refresh()
                assignment = self.admit()
                self.fixture.refresh()
                self.assertEqual(self.fixture.dispatch.cells.shared()[assignment['cell']], 1)

    def test_two_current_processes_on_same_target_are_ambiguous(self):
        assignment = self.admit()
        self.rows[0].update(pid=2, birth=[2, 100])
        self.births[2], self.placements[2] = [2, 100], 2
        self.index.refresh()
        f = self.fixture
        f.refresh()
        self.assertEqual(f.dispatch.cells.shared()[assignment['cell']], 0)
        self.assertTrue(f.dispatch.owns(f.sid))
        self.assertEqual(f.dispatch.lanes[0]['lane'].assignments, [])

    def test_stale_birth_cannot_evict_verified_same_second_successor(self):
        self.fixture.source['birth'] = [1, 101]
        self.births[1] = [1, 101]
        self.index.refresh()
        assignment = self.admit()
        self.fixture.refresh()
        self.assertEqual(self.fixture.dispatch.cells.shared()[assignment['cell']], 1)
        self.assertEqual(assignment['binding']['birth'], [1, 101])

    def test_fresh_same_second_pid_reuse_revokes_old_binding(self):
        assignment = self.admit()
        self.births[1] = [1, 101]
        self.rows[0]['birth'] = [1, 101]
        self.index.refresh()
        self.fixture.refresh()
        self.assertEqual(self.fixture.dispatch.cells.shared()[assignment['cell']], 0)

    def test_source_moved_off_target_is_not_preserved_by_stale_source(self):
        assignment = self.admit()
        self.placements[1] = None
        self.index.refresh()
        self.fixture.refresh()
        self.assertEqual(self.fixture.dispatch.cells.shared()[assignment['cell']], 0)

    def test_current_foreground_switch_still_revokes_promptly(self):
        assignment = self.admit()
        self.foreground.return_value = ('ok', 'actual-replacement', 'same-client')
        self.index.refresh()
        self.fixture.refresh()
        self.assertEqual(self.fixture.dispatch.cells.shared()[assignment['cell']], 0)

    def test_absent_or_invalid_foreground_is_not_proof_of_session_switch(self):
        assignment = self.admit()
        self.index.refresh()
        for status in ('absent', 'invalid'):
            with self.subTest(status=status):
                self.foreground.return_value = (status, None, None)
                self.fixture.refresh()
                self.assertEqual(self.fixture.dispatch.cells.shared()[assignment['cell']], 1)

    def test_ignoring_stale_hint_never_bypasses_unknown_full_reader(self):
        self.index.refresh()
        self.reader.side_effect = lambda _: {**self.fixture.source, 'kind': 'unknown'}
        for status in ('ok', 'absent', 'invalid'):
            with self.subTest(status=status):
                self.foreground.return_value = (status, 'new-session', 'same-client')
                self.fixture.refresh()
                self.assertFalse(self.fixture.dispatch.owns(self.fixture.sid))

    def test_successful_discovery_is_not_counted_as_failure_due_to_old_hint(self):
        f = self.fixture
        f.daemon.codex_queue_recovery.wakeup_sources = lambda _: []
        self.index.refresh()
        f.refresh()
        self.assertEqual(len(self.pool.calls), 1)
        self.pool.finish(0)
        f.refresh()
        self.assertEqual(f.dispatch._discovery_state[f.sid]['failures'], 0)


if __name__ == '__main__':
    unittest.main()
