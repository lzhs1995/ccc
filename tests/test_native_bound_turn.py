"""A late legacy Hook or guard record must not strand an exact native lane."""
import json
import unittest
import uuid
from unittest.mock import Mock, patch

import ccc_batch_guard as guard
from ccc_codex_queue import QueueRecovery, _BindingFile
from ccc_native_lanes import NativeDispatcher, _bound_native_turn
from tests import test_codex_idle_session as idle
from tests import test_native_dispatch_ownership as ownership


class BoundTurnTests(unittest.TestCase):
    def setUp(self):
        self.owner = ownership.OwnershipTests('test_parent_is_excluded_before_child_can_receive_assignment')
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        self.f = idle.IdleSessionTests('test_closed_rollout_with_current_writer_lock_supplies_failed_turn')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        self.owner.targets[0].update(f.target)
        # Registration policy indexes the list on publication. Replacing that
        # list keeps the UUID fixture inside the same real admission guard.
        self.owner.daemon.config['targets'] = list(self.owner.targets)
        self.owner.sid = f.target['surface_id']
        f.target = self.owner.targets[0]
        f.process.update(surface_id=f.target['surface_id'],
                         environment_workspace_id=f.target['workspace_id'])
        self.owner.daemon.codex_queue_recovery = f.queue
        self.binding = NativeDispatcher.binding(f.queue.current_turn(f.target))
        self.child = QueueRecovery(f.root / 'child-ledger', f.queue.bindings, f.sessions, 'continue')
        self.child._binding_file = _BindingFile(self.child.bindings)
        self.child.process_lookup = f.queue.process_lookup

    def read(self):
        return _bound_native_turn(self.f.target, self.binding, self.child.current_turn)

    def publish_hook(self, session_id=None):
        f = self.f
        self.child.bindings.write_text(json.dumps({'sessions': {
            session_id or f.sid: {'surfaceId': f.target['surface_id'],
                'workspaceId': f.target['workspace_id'], 'pid': 123, 'pidStartSeconds': 100,
                'transcriptPath': str(f.path)}}}) + '\n')

    def publish_guard(self, *, precise=True, session_id=None):
        f = self.f
        self.child.guard_config_path = f.root / 'guard-config.json'
        path = guard.pool_dir(self.child.guard_config_path, f.target['workspace_id']) / (
            guard.uid(f.target['surface_id']) + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**f.target, 'session_id': session_id or f.sid,
            'pid': 123, 'process_start': 100, 'kind': 'task_complete', 'at': 200,
            'turn_id': 'failed-original-turn', 'start_id': 'start',
            'turn_error': {'message': 'currently experiencing high demand'},
            **({'birth': [100, 100]} if precise else {})}) + '\n')

    def test_late_hook_keeps_separate_parent_and_lane_caches_on_original_binding(self):
        self.owner.refresh()
        assigned = self.owner.dispatch.owned[self.owner.sid]['assignment']
        self.assertIsNot(self.child._binding_file, self.f.queue._binding_file)
        self.assertIsNot(self.child.open_file_sources, self.f.queue.open_file_sources)
        self.assertEqual(self.read()['birth'], self.binding['birth'])
        self.publish_hook()
        with patch.object(self.child, '_open_process_turn', wraps=self.child._open_process_turn) as opened:
            for _ in range(3):
                value = self.read()
                self.assertEqual(value['kind'], 'task_complete')
                self.assertEqual(value['birth'], self.binding['birth'])
                self.owner.refresh()
                self.assertEqual(self.owner.dispatch.cells.shared()[assigned['cell']], 1)
            opened.assert_not_called()

    def test_hook_arriving_before_parent_admission_keeps_precise_source(self):
        self.publish_hook()
        self.owner.refresh()
        self.assertTrue(self.owner.dispatch.owns(self.owner.sid))
        assigned = self.owner.dispatch.owned[self.owner.sid]['assignment']
        self.assertEqual(assigned['binding'], self.binding)

    def admit_legacy_hook_with_precise_guard(self):
        self.publish_hook()
        self.publish_guard()
        self.f.queue.guard_config_path = self.child.guard_config_path
        self.f.queue.open_file_sources.clear()
        sources = self.f.queue.wakeup_sources([self.f.target])
        self.assertEqual(len(sources), 1)
        self.assertNotIn('birth', sources[0])
        self.assertEqual(self.f.queue.current_turn(self.f.target)['birth'], self.binding['birth'])
        self.owner.refresh()
        owned = self.owner.dispatch.owned[self.owner.sid]
        self.assertEqual(owned['assignment']['binding'], self.binding)
        return owned['assignment'], owned['state']['lane']

    def test_legacy_hook_keeps_precise_guard_assignment_across_parent_refreshes(self):
        assignment, lane = self.admit_legacy_hook_with_precise_guard()
        for _ in range(3):
            self.assertNotIn('birth', self.f.queue.wakeup_sources([self.f.target])[0])
            self.owner.refresh()
            self.assertIs(self.owner.dispatch.owned[self.owner.sid]['assignment'], assignment)
            self.assertEqual(assignment['binding'], self.binding)
            self.assertEqual(self.owner.dispatch.cells.shared()[assignment['cell']], 1)
            self.assertEqual(lane.assignments, [assignment])
            self.assertEqual(self.read()['birth'], self.binding['birth'])
        self.assertEqual(len(self.owner.dispatch.lanes), 1)

    def test_legacy_hook_cannot_retain_guard_assignment_after_same_second_pid_reuse(self):
        assignment, lane = self.admit_legacy_hook_with_precise_guard()
        self.f.process['birth'] = [100, 101]
        self.assertNotIn('birth', self.f.queue.wakeup_sources([self.f.target])[0])
        self.owner.refresh()
        self.assertEqual(self.owner.dispatch.cells.shared()[assignment['cell']], 0)
        self.assertEqual(lane.assignments, [])
        self.assertTrue(self.owner.dispatch.owns(self.owner.sid))
        self.assertIsNone(self.owner.daemon._active_send_target(self.f.target))
        self.assertEqual(self.read(), {'kind': 'unknown'})

    def test_legacy_hook_cannot_retain_guard_assignment_after_placement_disappears(self):
        assignment, lane = self.admit_legacy_hook_with_precise_guard()
        with patch('ccc_codex_queue.process_placement_start', return_value=None):
            self.owner.refresh()
            self.assertEqual(self.read(), {'kind': 'unknown'})
        self.assertEqual(self.owner.dispatch.cells.shared()[assignment['cell']], 0)
        self.assertEqual(lane.assignments, [])
        self.assertTrue(self.owner.dispatch.owns(self.owner.sid))
        self.assertIsNone(self.owner.daemon._active_send_target(self.f.target))

    def test_guard_record_with_or_without_birth_keeps_original_lane(self):
        for precise in (False, True):
            with self.subTest(precise=precise):
                self.publish_guard(precise=precise)
                with patch.object(self.child, '_open_process_turn', wraps=self.child._open_process_turn) as opened:
                    value = self.read()
                self.assertEqual(value['kind'], 'task_complete')
                self.assertEqual(value['birth'], self.binding['birth'])
                opened.assert_not_called()

    def test_guard_turn_retains_its_own_explicit_birth(self):
        self.publish_guard()
        self.assertEqual(self.child.current_turn(self.f.target).get('birth'), [100, 100])

    def test_missing_or_reused_birth_cannot_downgrade_an_exact_lane(self):
        for publish in (self.publish_hook, self.publish_guard):
            publish()
            for actual in (None, [100, 101], [101, 100]):
                with self.subTest(source=publish.__name__, birth=actual):
                    with patch('ccc_guard_scope.birth', return_value=actual), \
                            patch.object(self.child, 'current_turn', wraps=self.child.current_turn) as reader:
                        self.assertEqual(self.read(), {'kind': 'unknown'})
                        reader.assert_not_called()

    def test_missing_or_changed_placement_cannot_supply_an_exact_lane(self):
        self.publish_hook()
        for actual in (None, 101):
            with self.subTest(start=actual), patch('ccc_codex_queue.process_placement_start', return_value=actual):
                self.assertEqual(self.read(), {'kind': 'unknown'})

    def test_changed_session_from_hook_or_guard_remains_unknown(self):
        for publish in (self.publish_hook, self.publish_guard):
            with self.subTest(source=publish.__name__):
                publish(session_id=str(uuid.uuid4()))
                self.assertEqual(self.read(), {'kind': 'unknown'})

    def test_identity_change_during_each_original_reader_is_rejected(self):
        for publish in (self.publish_hook, self.publish_guard):
            for kind in ('birth', 'placement'):
                with self.subTest(source=publish.__name__, kind=kind):
                    self.f.process['birth'] = [100, 100]
                    publish()
                    placement = [100]
                    def read_and_change(target):
                        value = self.child.current_turn(target)
                        if kind == 'birth':
                            self.f.process['birth'] = [100, 101]
                        else:
                            placement[0] = None
                        return value
                    with patch('ccc_codex_queue.process_placement_start', side_effect=lambda *a: placement[0]):
                        self.assertEqual(_bound_native_turn(self.f.target, self.binding, read_and_change),
                                         {'kind': 'unknown'})

    def test_explicit_empty_malformed_or_conflicting_birth_is_never_overwritten(self):
        for value in (None, [], [100], [100, 101], [True, 100]):
            with self.subTest(birth=value):
                reader = Mock(return_value={**self.binding, 'birth': value, 'kind': 'task_complete'})
                self.assertEqual(_bound_native_turn(self.f.target, self.binding, reader), {'kind': 'unknown'})
                reader.assert_called_once_with(self.f.target)

    def test_legacy_binding_without_birth_keeps_portable_reader_contract(self):
        legacy = {key: value for key, value in self.binding.items() if key != 'birth'}
        reader = Mock(return_value={**legacy, 'kind': 'task_complete'})
        with patch('ccc_guard_scope.birth', side_effect=AssertionError('legacy must not require native birth')):
            self.assertEqual(_bound_native_turn(self.f.target, legacy, reader), reader.return_value)
        reader.assert_called_once_with(self.f.target)

    def test_legacy_binding_rejects_explicit_malformed_birth_without_admission_crash(self):
        legacy = {key: value for key, value in self.binding.items() if key != 'birth'}
        for born in (None, [], [100], [100, True], [0, 1], [100, 1000000]):
            with self.subTest(birth=born):
                value = {**legacy, 'birth': born, 'kind': 'task_complete'}
                reader = Mock(return_value=value)
                self.assertEqual(_bound_native_turn(self.f.target, legacy, reader), {'kind': 'unknown'})
                self.assertIsNone(NativeDispatcher.binding(value))
                reader.assert_called_once_with(self.f.target)

    def test_legacy_reader_birth_is_promoted_only_with_fresh_matching_process(self):
        legacy = {key: value for key, value in self.binding.items() if key != 'birth'}
        value = {**self.binding, 'kind': 'task_complete'}
        for actual in (None, [100, 101], [100, 100]):
            with self.subTest(actual=actual), patch('ccc_guard_scope.birth', return_value=actual):
                reader = Mock(return_value=value)
                self.assertEqual(_bound_native_turn(self.f.target, legacy, reader),
                                 value if actual == value['birth'] else {'kind': 'unknown'})
                reader.assert_called_once_with(self.f.target)
        with patch('ccc_codex_queue.process_placement_start', return_value=None):
            self.assertEqual(_bound_native_turn(self.f.target, legacy, Mock(return_value=value)),
                             {'kind': 'unknown'})

    def test_failed_fresh_probe_keeps_unknown_without_calling_reader(self):
        reader = Mock()
        with patch('ccc_guard_scope.birth', side_effect=OSError('process unavailable')):
            self.assertEqual(_bound_native_turn(self.f.target, self.binding, reader), {'kind': 'unknown'})
        reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
