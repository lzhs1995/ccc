"""Historical process IDs must not resurrect an old terminal or its failures."""
import json
import tempfile
import time
import unittest
from unittest import mock

import cmux_codex_watch as core
import ccc_observation as health
from ccc_scheduling import SnapshotClient
from tests.test_registration_observation import main_tree
from tests.test_watch import FakeClient, claude_armed_daemon, claude_grid_payload, process_fixture_with_pid


class HistoricalOwnerHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = time.time()
        top = process_fixture_with_pid('surface-uuid', 'zsh', 5678)
        top['sample'] = {'enumeration_complete': True}
        top['windows'][0]['workspaces'][0].update(id='workspace-uuid', kind='workspace')
        self.client = FakeClient(claude_grid_payload(), tree=main_tree(), top=top)
        self.client.top_all = lambda: self.client.top_data
        self.client.terminal_diagnostics = lambda: {'terminals': [{
            'surface_id': 'surface-uuid', 'runtime_surface_ready': True,
            'ghostty_surface_ptr': '0x1234'}]}
        self.daemon = claude_armed_daemon(temporary.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon._check_claude_hook_settings = mock.Mock()
        self.daemon._native_process_index.snapshot = lambda: {}
        self.daemon.runtime['surface-uuid'] = core.TargetRuntime(
            state='terminal_dormant', viewport_readable=True,
            viewport_checked_at=self.now - 20, claude_process_pid=1234,
            claude_process_generation='original-generation', claude_hook_health='historical')
        self.inspected = {'pid': 1234, 'started_at': '2026-09-26T12:00:00',
                          'generation': 'reused-generation', 'legacy_override': False}

    def refresh(self, *, kill_error=None):
        client = SnapshotClient(self.client, self.daemon._process_snapshots)
        with mock.patch.object(core.os, 'kill', side_effect=kill_error), \
             mock.patch.object(core, 'inspect_claude_process', return_value=self.inspected), \
             mock.patch.object(core.time, 'time', return_value=self.now):
            self.daemon._refresh_observation_health(client)
            snapshot = json.loads(self.daemon.observation_health_path.read_text())
            state = self.daemon._runtime_snapshot()
            observation = core.observation_status(self.daemon.config, state, snapshot)
            continuation = core.continuation_status(self.daemon.config, state, snapshot)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.sent_keys, [])
        return observation['targets'][0], continuation['targets'][0]

    def test_reused_unattributed_pid_keeps_shell_inactive(self):
        observed, continued = self.refresh()
        self.assertEqual(observed['status'], 'dormant')
        self.assertEqual(continued['status'], 'inactive')

    def test_same_surface_reused_pid_still_requires_original_generation(self):
        process = self.client.top_data['windows'][0]['workspaces'][0]['surfaces'][0]['processes'][0]
        process.update(pid=1234, cmux_surface_id='surface-uuid', cmux_workspace_id='workspace-uuid')
        observed, continued = self.refresh()
        self.assertEqual(observed['status'], 'dormant')
        self.assertEqual(continued['status'], 'inactive')

    def test_closed_surface_with_reused_pid_stays_missing(self):
        self.client.tree_data = {'windows': []}
        self.client.top_data = {'windows': [], 'sample': {'enumeration_complete': True}}
        observed, continued = self.refresh()
        self.assertEqual(observed['status'], 'missing')
        self.assertEqual(continued['status'], 'missing')

    def test_moved_surface_original_owner_survives_old_workspace_environment(self):
        process = self.client.top_data['windows'][0]['workspaces'][0]['surfaces'][0]['processes'][0]
        process.update(pid=1234, cmux_surface_id='surface-uuid', cmux_workspace_id='old-workspace')
        self.inspected['generation'] = 'original-generation'
        self.refresh()
        self.assertIs(self.daemon._observation_metadata['owners']['surface-uuid'], True)

    def test_moved_surface_reused_pid_does_not_restore_original_owner(self):
        process = self.client.top_data['windows'][0]['workspaces'][0]['surfaces'][0]['processes'][0]
        process.update(pid=1234, cmux_surface_id='surface-uuid', cmux_workspace_id='old-workspace')
        self.refresh()
        self.assertIs(self.daemon._observation_metadata['owners']['surface-uuid'], False)

    def test_foreign_surface_cannot_restore_owner_even_with_matching_generation(self):
        process = self.client.top_data['windows'][0]['workspaces'][0]['surfaces'][0]['processes'][0]
        process.update(pid=1234, cmux_surface_id='foreign-surface', cmux_workspace_id='workspace-uuid')
        self.inspected['generation'] = 'original-generation'
        self.refresh()
        self.assertIs(self.daemon._observation_metadata['owners']['surface-uuid'], False)

    def test_real_original_owner_cannot_be_hidden_as_dormant(self):
        self.inspected['generation'] = 'original-generation'
        self.client.tree_data = {'windows': []}
        observed, _ = self.refresh()
        self.assertEqual(observed['status'], 'live_unreadable')
        self.assertEqual(observed['reason_code'], 'live_owner_without_surface')

    def test_inspection_failure_and_missing_saved_generation_remain_unknown(self):
        self.inspected.update(started_at='', generation='1234:unknown')
        self.assertEqual(self.refresh()[0]['status'], 'unknown')
        self.inspected.update(started_at='2026-09-26T12:00:00', generation='reused-generation')
        self.daemon.runtime['surface-uuid'].claude_process_generation = None
        self.assertEqual(self.refresh()[0]['status'], 'unknown')

    def test_exited_owner_does_not_need_a_replacement_process(self):
        observed, continued = self.refresh(kill_error=ProcessLookupError())
        self.assertEqual(observed['status'], 'dormant')
        self.assertEqual(continued['status'], 'inactive')

    def test_incomplete_inventory_never_proves_no_agent(self):
        self.client.top_data['sample']['enumeration_complete'] = False
        self.daemon.runtime['surface-uuid'].viewport_readable = False
        self.assertEqual(self.refresh(kill_error=ProcessLookupError())[0]['status'], 'unknown')


class IncompleteInventoryHealthTests(unittest.TestCase):
    def setUp(self):
        self.target = {'surface_id': 's', 'workspace_id': 'w', 'enabled': True}
        self.shell = {'agent_kind': 'shell', 'agent_pid': 0, 'process_snapshot_present': True}

    def observe(self, *, present=True, process=None, owner=False, runtime=None):
        return health.observation_row(self.target, self.target if present else None,
            self.shell if process is None else process,
            {'runtime_surface_ready': True, 'ghostty_surface_ptr': '0x123'}, runtime or {},
            owner_alive=owner, now=100, stale_after=30, inventory_complete=False)

    def test_partial_process_scan_reports_uncertainty_instead_of_old_pauses(self):
        self.target.update(paused=True, pause_origin='automatic_observation_error')
        observed = self.observe(present=False)
        self.assertEqual(observed['reason_code'], 'process_inventory_incomplete')
        row = health.continuation_row(self.target, {'state': 'missing_or_error', 'viewport_checked_at': 1},
                                      now=101, observation=observed)
        self.assertEqual((row['status'], row['reason_code']), ('unknown', 'process_inventory_incomplete'))

    def test_old_shell_viewport_and_claude_hook_history_cannot_become_current_faults(self):
        for phase in ('terminal_dormant', 'claude_hook_missing', 'idle', 'claude_completed'):
            with self.subTest(phase=phase):
                runtime = {'state': phase, 'viewport_checked_at': 90, 'viewport_readable': True}
                observed = self.observe(runtime=runtime)
                self.assertEqual((observed['status'], observed['reason_code']),
                                 ('unknown', 'process_inventory_incomplete'))
                row = health.continuation_row(self.target, runtime, now=101, observation=observed)
                self.assertEqual(row['status'], 'unknown')
                self.assertEqual(row['reason_code'], 'process_inventory_incomplete')

    def test_real_native_identity_still_uses_current_observation_and_deadline(self):
        process = dict(agent_kind='codex', agent_pid=7, identity_verified_pids=[7], process_snapshot_present=True)
        runtime = {'state': 'working', 'viewport_checked_at': 100, 'viewport_readable': True}
        observed = self.observe(process=process, runtime=runtime)
        self.assertEqual(observed['status'], 'readable')
        self.assertEqual(health.continuation_row(self.target, runtime, now=101, observation=observed)['status'], 'ok')
        self.assertEqual(health.continuation_row(self.target, runtime, now=103, observation=observed)['status'], 'delayed')
        observed = self.observe(present=False, process=process)
        self.assertEqual(observed['status'], 'live_unreadable')

    def test_explicit_user_pause_is_preserved(self):
        self.target.update(paused=True, pause_origin='user')
        observed = self.observe()
        self.assertEqual(observed['status'], 'paused')
        self.assertEqual(health.continuation_row(self.target, {}, now=101, observation=observed)['status'], 'paused')

    def test_current_send_failures_and_read_errors_are_not_hidden_by_incomplete_scan(self):
        observed = self.observe()
        for runtime, status in (({'delivery_status': 'failed'}, 'send_failed'),
                                ({'delivery_status': 'unknown'}, 'delivery_unknown'),
                                ({'delivery_status': 'sending', 'send_started_at': 90}, 'delivery_unknown'),
                                ({'state': 'cmux_unavailable'}, 'unavailable'),
                                ({'state': 'provider_blocked'}, 'blocked')):
            with self.subTest(runtime=runtime):
                row = health.continuation_row(self.target, {'viewport_checked_at': 100, **runtime},
                                              now=101, observation=observed)
                self.assertEqual(row['status'], status)

    def test_stale_or_foreign_inventory_uncertainty_does_not_override_current_deadlines(self):
        observed = self.observe()
        for changes in ({'observed_at': 1}, {'surface_id': 'foreign'}, {'workspace_id': 'foreign'}):
            with self.subTest(changes=changes):
                row = health.continuation_row(self.target, {'viewport_checked_at': 1},
                                              now=101, observation={**observed, **changes})
                self.assertEqual(row['status'], 'delayed')


if __name__ == '__main__':
    unittest.main()
