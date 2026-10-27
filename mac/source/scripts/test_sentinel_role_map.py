"""Offline producer/consumer checks for real harness role-map shapes."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mac_harness
import cmux_executor_sentinel as sentinel
from cmux_executor_sentinel import verify_role_map_target


class RoleMapIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'role-map.json'

    def check(self, value, task='synthetic', supervisor='surface:2', executor='surface:1'):
        self.path.write_text(json.dumps(value))
        return verify_role_map_target(str(self.path), task, supervisor, executor)

    def test_actual_harness_map_output_is_accepted(self):
        gate = {'task_id': 'synthetic', 'status': 'PASS', 'workspace_ref': 'workspace:synthetic',
                'supervisor': 'surface:2', 'supervisor_provider': 'codex',
                'executor': 'surface:1', 'executor_provider': 'claude'}
        (self.root / 'identity-gate.json').write_text(json.dumps(gate))
        mac_harness.cmd_map(SimpleNamespace(task_id='synthetic', artifact_root=str(self.root), json=False))
        value = json.loads(self.path.read_text())
        self.assertIsInstance(value['supervisor'], dict)
        self.assertTrue(verify_role_map_target(str(self.path), 'synthetic', 'surface:2', 'surface:1')[0])

    def test_detailed_maps_still_reject_wrong_task_and_either_surface(self):
        value = {'task_id': 'synthetic', 'supervisor': {'surface_ref': 'surface:2'},
                 'executor': {'surface_ref': 'surface:1'}}
        for changes in ({'task': 'other'}, {'supervisor': 'surface:3'}, {'executor': 'surface:3'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.check(value, **changes)[0])

    def test_legacy_flat_and_roles_list_remain_supported(self):
        for value in ({'supervisor': 'surface:2', 'executor': 'surface:1'},
                      {'roles': [{'role': 'supervisor', 'surface_ref': 'surface:2'},
                                 {'role': 'executor', 'surface_ref': 'surface:1'}]}):
            with self.subTest(value=value):
                self.assertTrue(self.check(value)[0])

    def test_missing_and_conflicting_role_identity_fail_closed(self):
        for value in ({'supervisor': {'provider': 'codex'}},
                      {'roles': {'supervisor': 'surface:9'}, 'supervisor': {'surface_ref': 'surface:2'}}):
            with self.subTest(value=value):
                self.assertFalse(self.check(value)[0])

    def test_explicit_malformed_role_cannot_accept_an_arbitrary_target(self):
        malformed = [{}, {'provider': 'claude'}, {'surface_ref': True},
                     {'surface_ref': None}, {'surface_ref': ''}, {'surface_ref': ' '},
                     True, None, '', ' ']
        for role in ('supervisor', 'executor'):
            for declaration in malformed:
                value = {'supervisor': {'surface_ref': 'surface:2'},
                         'executor': {'surface_ref': 'surface:1'}, role: declaration}
                with self.subTest(role=role, declaration=declaration):
                    self.assertFalse(self.check(value, **{role: 'surface:99999'})[0])

    def test_surface_aliases_must_be_valid_and_consistent(self):
        for declaration in ({'surface': 'surface:1'},
                            {'surface_ref': 'surface:1', 'surface': 'surface:1'}):
            self.assertTrue(self.check({'supervisor': 'surface:2', 'executor': declaration})[0])
        for declaration in ({'surface_ref': 'surface:1', 'surface': 'surface:9'},
                            {'surface_ref': None, 'surface': 'surface:1'},
                            {'surface_ref': True, 'surface': 'surface:1'}):
            with self.subTest(declaration=declaration):
                self.assertFalse(self.check({'supervisor': 'surface:2', 'executor': declaration})[0])

    def protocol_map(self):
        return {'task_id': 'synthetic',
                'supervisor': {'identity': 'surface:2', 'agent': 'codex'},
                'executors': [{'identity': 'surface:1', 'role': 'executor', 'agent': 'claude'},
                              {'identity': 'surface:3', 'role': 'executor2', 'agent': 'claude'}]}

    def test_protocol_identity_and_executor_list_accept_each_bound_peer(self):
        for target in ('surface:1', 'surface:3'):
            with self.subTest(target=target):
                self.assertTrue(self.check(self.protocol_map(), executor=target)[0])
        self.assertFalse(self.check(self.protocol_map(), executor='surface:9')[0])

    def test_actual_two_executor_harness_map_accepts_second_peer(self):
        gate = {'task_id': 'synthetic', 'status': 'PASS', 'supervisor': 'surface:2',
                'executors': [{'surface_ref': 'surface:1', 'ordinal': 1},
                              {'surface_ref': 'surface:3', 'ordinal': 2}]}
        (self.root / 'identity-gate.json').write_text(json.dumps(gate))
        mac_harness.cmd_map(SimpleNamespace(task_id='synthetic', artifact_root=str(self.root), json=False))
        self.assertTrue(sentinel.verify_role_map_target(str(self.path), 'synthetic', 'surface:2', 'surface:3')[0])

    def test_protocol_map_rejects_task_supervisor_and_alias_conflicts(self):
        for changes in ({'task': 'other'}, {'supervisor': 'surface:9'}):
            self.assertFalse(self.check(self.protocol_map(), **changes)[0])
        for role in ('supervisor', 'executor'):
            value = self.protocol_map()
            obj = value['supervisor'] if role == 'supervisor' else value['executors'][0]
            obj['surface_ref'] = 'surface:9'
            self.assertFalse(self.check(value)[0])

    def test_role_list_conflicts_cannot_be_overwritten_by_later_entries(self):
        value = {'roles': [{'role': 'supervisor', 'identity': 'surface:9'},
                           {'role': 'supervisor', 'identity': 'surface:2'},
                           {'role': 'executor', 'identity': 'surface:1'}]}
        self.assertFalse(self.check(value)[0])
        value = self.protocol_map()
        value['executor'] = {'surface_ref': 'surface:9'}
        self.assertFalse(self.check(value)[0])

    def test_malformed_protocol_members_and_roots_refuse_without_exception(self):
        for value in (None, True, [], 'bad', {'supervisor': 'surface:2'}):
            with self.subTest(value=value):
                self.assertFalse(self.check(value)[0])
        for members in (None, {}, [], [None], [{'identity': True}],
                        [{'identity': 'surface:1', 'role': 'supervisor'}],
                        [{'identity': 'surface:1', 'role': 'executor'},
                         {'identity': 'surface:9', 'role': 'executor'}]):
            value = self.protocol_map()
            value['executors'] = members
            with self.subTest(members=members):
                self.assertFalse(self.check(value)[0])

    def test_duplicate_surface_roles_and_missing_executor_refuse(self):
        for second in ({'identity': 'surface:1', 'role': 'executor2'},
                       {'identity': 'surface:2', 'role': 'executor2'}):
            value = self.protocol_map()
            value['executors'][1] = second
            self.assertFalse(self.check(value)[0])

    def test_role_collection_shapes_merge_only_consistent_declarations(self):
        for key in ('roles', 'entries'):
            value = self.protocol_map()
            value[key] = {'supervisor': {'surface_ref': 'surface:2'},
                          'executor1': {'surface': 'surface:1'}}
            self.assertTrue(self.check(value)[0])
            value[key]['executor1']['surface'] = 'surface:9'
            self.assertFalse(self.check(value)[0])

    def test_run_entrypoint_refuses_conflict_before_daemon(self):
        value = self.protocol_map()
        value['executors'][0]['surface_ref'] = 'surface:9'
        self.path.write_text(json.dumps(value))
        argv = ['sentinel', 'run', '--task-id', 'synthetic', '--role-map', str(self.path),
                '--supervisor-surface', 'surface:2', '--executor-surface', 'surface:1']
        with patch('sys.argv', argv), patch.object(sentinel, 'run_daemon') as daemon, \
                patch.object(sentinel, 'state_paths', return_value=(self.root/'state', self.root/'lock', self.root/'stop')):
            self.assertEqual(sentinel.main(), 3)
            daemon.assert_not_called()

    def test_run_entrypoint_passes_matching_protocol_map_to_daemon(self):
        self.path.write_text(json.dumps(self.protocol_map()))
        argv = ['sentinel', 'run', '--task-id', 'synthetic', '--role-map', str(self.path),
                '--supervisor-surface', 'surface:2', '--executor-surface', 'surface:3']
        with patch('sys.argv', argv), patch.object(sentinel, 'run_daemon', return_value=0) as daemon, \
                patch.object(sentinel, 'state_paths', return_value=(self.root/'state', self.root/'lock', self.root/'stop')):
            self.assertEqual(sentinel.main(), 0)
            daemon.assert_called_once()


if __name__ == '__main__':
    unittest.main()
