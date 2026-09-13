import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import FakeClient, claude_armed_daemon, claude_hook_event, claude_grid_payload


class HookIdentityReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = FakeClient({})
        self.daemon = claude_armed_daemon(temporary.name, self.client)
        self.target = self.daemon.config['targets'][0]
        self.sid = self.target['surface_id']
        self.now = time.time()
        self.observation = dict(pid=1234, generation='same-generation',
                                started_epoch=self.now - 100000,
                                started_at='2026-08-27T23:21:27',
                                agent_kind='claude', legacy_override=False)
        self.runtime = self.daemon.runtime.setdefault(self.sid, core.TargetRuntime())
        self.daemon._apply_claude_process_observation(self.runtime, self.observation)

    def journal(self, **changes):
        events = []
        for name, age in [('SessionStart', 90000), ('UserPromptSubmit', 89990),
                          ('StopFailure', 89980)]:
            event = claude_hook_event('history-' + name, name)
            event.update(surface_id=self.sid, workspace_id=self.target['workspace_id'],
                         agent_pid=1234, created_at=self.now - age,
                         session_id='session-history')
            event.update(changes)
            events.append(event)
        self.daemon.claude_event_inbox.journal_path.write_text(
            ''.join(json.dumps(event) + '\n' for event in events))

    def reconcile(self):
        self.daemon._reconcile_claude_hook_identity(
            self.target, self.runtime, self.observation)

    def test_old_same_process_events_recover_identity_but_not_send_authority(self):
        self.journal()
        self.reconcile()
        self.assertEqual(self.runtime.claude_session_id, 'session-history')
        self.assertEqual(self.runtime.claude_hook_health, 'historical')
        self.assertEqual(self.runtime.claude_hook_provenance, 'journal_identity')
        self.assertEqual(self.runtime.claude_last_event_status, 'identity_recovered')
        self.assertEqual(self.runtime.claude_submit_phase, 'none')
        self.assertIsNone(self.runtime.claude_deferred_event)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.claude_event_ledger.known_ids(), set())
        self.daemon._apply_claude_process_observation(self.runtime, self.observation)
        self.assertEqual(self.runtime.claude_hook_health, 'historical')

    def test_rejected_unregistered_event_is_not_a_healthy_hook(self):
        event = claude_hook_event('unregistered', 'UserPromptSubmit')
        event.update(surface_id='unregistered-surface', agent_pid=1234)
        self.daemon._handle_claude_event(event, self.client)
        runtime = self.daemon.runtime['unregistered-surface']
        self.assertEqual(runtime.claude_last_hook_at, 0)
        self.assertEqual(runtime.claude_last_rejected_hook_status, 'unmapped')
        self.assertEqual(runtime.claude_last_rejected_hook_reason, 'surface is not authorized')
        self.assertEqual(self.client.sent, [])

    def test_history_cannot_authorize_fallback_even_with_a_prompt_status(self):
        self.journal()
        self.reconcile()
        self.daemon._claude_hook_config_health = {'healthy': True}
        self.runtime.claude_last_event_status = 'human_prompt'
        state = core.ScreenState('claude_stopped', content_fingerprint='stable-stop')
        self.assertFalse(self.daemon._maybe_send_claude_hook_gap_fallback(
            self.target, self.runtime, state, self.observation, self.client))
        self.assertFalse(self.runtime.claude_fallback_episode_id)
        self.assertEqual(self.client.sent, [])

    def test_wrong_pid_workspace_time_and_synthetic_history_cannot_bind(self):
        for changes in ({'agent_pid':9999}, {'workspace_id':'foreign-ws'},
                        {'created_at':self.now-200000}, {'created_at':self.now+100},
                        {'created_at':float('nan')}, {'synthetic_fallback':True},
                        {'session_id':''}):
            with self.subTest(changes=changes):
                self.journal(**changes)
                self.daemon.claude_hook_history = core.ClaudeHookHistory(
                    self.daemon.claude_event_inbox.journal_path)
                self.reconcile()
                self.assertIsNone(self.runtime.claude_session_id)
                self.assertEqual(self.client.sent, [])

    def test_completed_paused_and_inflight_runtime_is_not_rebound(self):
        self.journal()
        for field, value in [('claude_completed_latched', True),
                             ('claude_submit_phase', 'enter_sent'),
                             ('claude_session_id', 'already-bound')]:
            with self.subTest(field=field):
                setattr(self.runtime, field, value)
                before = copy.deepcopy(self.runtime)
                self.reconcile()
                self.assertEqual(self.runtime, before)
                setattr(self.runtime, field, getattr(core.TargetRuntime(), field))
        self.target['paused'] = True
        self.reconcile()
        self.assertIsNone(self.runtime.claude_session_id)

    def test_foreign_live_owner_and_unknown_process_start_cannot_bind(self):
        self.journal()
        self.daemon.runtime['foreign'] = core.TargetRuntime(claude_session_id='session-history')
        self.reconcile()
        self.assertIsNone(self.runtime.claude_session_id)
        del self.daemon.runtime['foreign']
        self.observation['started_epoch'] = 0
        self.reconcile()
        self.assertIsNone(self.runtime.claude_session_id)

    def test_new_generation_clears_recovered_identity(self):
        self.journal()
        self.reconcile()
        self.daemon._apply_claude_process_observation(self.runtime, {
            **self.observation, 'pid':1235, 'generation':'new-generation',
            'started_epoch':self.now})
        self.assertIsNone(self.runtime.claude_session_id)
        self.assertEqual(self.runtime.claude_hook_provenance, '')

    def test_missing_start_or_conflicting_session_cannot_bind(self):
        self.journal()
        path = self.daemon.claude_event_inbox.journal_path
        events = [json.loads(line) for line in path.read_text().splitlines()]
        for variant in (events[1:], [events[0], {**events[-1], 'session_id':'other'}]):
            with self.subTest(variant=variant):
                path.write_text(''.join(json.dumps(event)+'\n' for event in variant))
                self.daemon.claude_hook_history = core.ClaudeHookHistory(path)
                self.reconcile()
                self.assertIsNone(self.runtime.claude_session_id)

    def test_live_prompt_promotes_recovered_identity_without_resending(self):
        self.journal()
        self.reconcile()
        event = claude_hook_event('real-new-prompt', 'UserPromptSubmit', prompt_kind='human')
        event.update(agent_pid=1234, surface_id=self.sid, session_id='session-history')
        self.daemon._handle_claude_event(event, self.client)
        self.assertEqual(self.runtime.claude_hook_health, 'healthy')
        self.assertEqual(self.runtime.claude_hook_provenance, 'live_event')
        self.assertEqual(self.client.sent, [])
        self.daemon._handle_claude_event(event, self.client)
        self.assertEqual(self.client.sent, [])

    def test_coverage_keeps_completed_gap_visible_without_counting_it_as_active(self):
        record = dict(self.target, ref='surface:1')
        self.client.top_all = mock.Mock(return_value={})
        with mock.patch.object(core, 'main_surface_records', return_value=[record]), \
                mock.patch.object(core, 'classify_surface_processes', return_value={}), \
                mock.patch.object(core, 'surface_process_label', return_value={'agent_kind':'claude', 'agent_pid':1234}), \
                mock.patch.object(core, 'inspect_claude_process', return_value=self.observation), \
                mock.patch.object(core, 'discover_rule_targets', return_value=[]):
            for disposition in ('active', 'completed', 'paused'):
                self.target['paused'] = disposition == 'paused'
                self.runtime.claude_completed_latched = disposition == 'completed'
                state = {self.sid: core.dataclasses.asdict(self.runtime)}
                result = core.claude_hook_coverage(self.daemon.config, state, self.client)
                self.assertEqual(result['verified'], 0)
                self.assertEqual(result['status'], 'degraded' if disposition == 'active' else 'ok')
                if disposition != 'active':
                    self.assertEqual(result[disposition + '_unverified'], ['surface:1'])


class ClaudeModelBlockTests(unittest.TestCase):
    def test_http_retry_banners_keep_client_ownership_through_final_attempt(self):
        banners = (
            '✻ 502 Upstream access forbidden, please … · Retrying in 32s · attempt 10/10',
            '✻ 503 No available accounts. Retrying in 3s · attempt 10/10',
            '✽ 429 Rate limit exceeded · Retrying in 0.5s · attempt 2/10',
        )
        for banner in banners:
            for tool in (False, True):
                with self.subTest(banner=banner, tool=tool):
                    payload = claude_grid_payload(error=banner, tool=tool, columns=160)
                    state = core.classify_claude_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
                    self.assertEqual(state.kind, 'working')
                    self.assertEqual(state.error_type, 'claude_retry')

    def test_old_http_retry_banner_does_not_hide_newer_output(self):
        lines = ['✻ 502 Upstream access forbidden · Retrying in 32s · attempt 10/10',
                 'That was the previous retry.', 'Newer unfinished answer.']
        state = core.classify_claude_grid(core.Grid.from_rpc(
            claude_grid_payload(lines=lines, completed=True, columns=160), 'surface-uuid'))
        self.assertEqual(state.kind, 'claude_stopped')

    def test_live_client_retry_countdown_is_working_not_a_new_stop(self):
        for attempt in (1, 2, 10):
            for tool in (False, True):
                with self.subTest(attempt=attempt, tool=tool):
                    payload = claude_grid_payload(
                        error=f'\u273b API error \u00b7 Retrying in 18s \u00b7 attempt {attempt}/10',
                        tool=tool)
                    verdict = core.classify_claude_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
                    self.assertEqual(verdict.kind, 'working')
                    self.assertEqual(verdict.error_type, 'claude_retry')

    def test_retry_countdown_quoted_in_older_output_does_not_hold_a_stopped_turn(self):
        lines = ['\u273b API error \u00b7 Retrying in 18s \u00b7 attempt 2/10',
                 'This was the old failure banner.', 'Newer unfinished answer.']
        verdict = core.classify_claude_grid(core.Grid.from_rpc(
            claude_grid_payload(lines=lines, completed=True, columns=120), 'surface-uuid'))
        self.assertEqual(verdict.kind, 'claude_stopped')

    def test_terminal_api_failure_without_countdown_remains_recoverable(self):
        verdict = core.classify_claude_grid(core.Grid.from_rpc(
            claude_grid_payload(error='API Error: 524 request failed'), 'surface-uuid'))
        self.assertEqual(verdict.kind, 'recoverable_error')

    def test_current_invalid_model_blocks_without_hiding_hook_health(self):
        lines = ["There's an issue with the selected model (claude-opus-4-8[1m]). It may not",
                 "exist or you may not have access to it. Run /model to pick a different",
                 "model."]
        grid = core.Grid.from_rpc(claude_grid_payload(lines=lines, completed=True), 'surface-uuid')
        state = core.classify_claude_grid(grid)
        self.assertEqual(state.kind, 'claude_model_unavailable')
        self.assertNotIn(state.kind, core.SEND_ELIGIBLE_STATES)

    def test_old_invalid_model_does_not_block_newer_output(self):
        lines = ["There's an issue with the selected model (old). It may not exist or you may not have access to it. Run /model to pick a different model.",
                 '', 'New answer after recovery']
        grid = core.Grid.from_rpc(claude_grid_payload(lines=lines, completed=True, columns=240), 'surface-uuid')
        self.assertNotEqual(core.classify_claude_grid(grid).kind, 'claude_model_unavailable')


class StackHookCoverageTests(unittest.TestCase):
    def test_service_health_degrades_for_an_authorized_hook_coverage_gap(self):
        path = Path(core.__file__).parent / 'bin' / 'cmux-stack'
        loader = importlib.machinery.SourceFileLoader('hook_coverage_stack', str(path))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        payload = {'mode':'armed', 'global_paused':False, 'explicit_targets':[], 'workspace_rules':[],
                   'daemon':{'pid':1234,'pid_alive':True,'source_matches_disk':True,
                             'runtime_metadata_present':True},
                   'claude_hook_coverage':{'status':'degraded','monitored_live':4,'verified':0,
                                           'needs_verification':['surface:74','surface:157'],
                                           'completed_unverified':['surface:71']}}
        with mock.patch.object(module, '_run', return_value=(0,json.dumps(payload),'')), \
                mock.patch.object(module, '_launchd_loaded', return_value=True):
            row = module.probe_watcher()
        self.assertIs(row['healthy'], False)
        self.assertEqual(row['claude_hook_coverage']['needs_verification'], ['surface:74','surface:157'])
        self.assertIn('claude_hook_coverage_gap', module._component_warnings('watcher',row))
        row['claude_hook_coverage']['status'] = 'unknown'
        self.assertIn('claude_hook_coverage_unknown', module._component_warnings('watcher',row))


if __name__ == '__main__':
    unittest.main()
