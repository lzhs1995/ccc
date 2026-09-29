"""N transport results must agree with the resume gate and visible table."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import ccc_access_service as service
import cmux_codex_watch as core
import cmux_supervisor_tui as tui
from tests.test_supervisor_responsiveness import QuietSource


class AccessObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.wid, self.jid = str(uuid.uuid4()), str(uuid.uuid4())
        self.bindings = {f's-{i}': {'job_id': self.jid, 'index': i} for i in range(50)}
        self.config = {'workspace_rules': [{'workspace_id': self.wid, 'access_check_slots': self.bindings}]}
        self.status_path = service.job_root(self.path, self.jid) / 'access-status.json'
        self.value = {'job_id': self.jid, 'workspace_id': self.wid, 'updated_at': time.time(),
                      'attempts': 50, 'max_attempts': 1000, 'forwarded': 50, 'in_flight': 0,
                      'complete': 0, 'blocked_slots': [], 'first_complete': None,
                      'closed': False, 'fault': '', 'authorized': True}
        self.target = {'workspace_id': self.wid, 'surface_id': 's-0'}

    def decision(self, **change):
        value = {**self.value, **change}
        core.atomic_write_json(self.status_path, value)
        decision = service.continuation_decision(self.bindings['s-0'], value, self.wid)
        self.assertEqual(service.continuation_allowed(self.path, self.config, self.target), decision['allowed'])
        return decision

    def model(self):
        quiet = QuietSource()
        model = tui.SupervisorModel(self.path, client=object(), janitor=quiet,
                                    sessions=quiet, stack=quiet, collab=quiet)
        model.config = copy.deepcopy(self.config)
        model.candidates = [tui.Candidate(
            {'workspace_id': self.wid, 'workspace_ref': 'workspace:51', 'surface_id': f's-{i}',
             'ref': f'surface:{1402 + i}', 'pane_ref': 'pane:77', 'type': 'terminal'},
            'workspace_rule', 'working' if i % 2 else 'awaiting_transition', '', 0, False,
            agent_kind='codex', continuation_status='ok') for i in range(50)]
        self.addCleanup(model.close)
        return model

    def test_legacy_fifty_blocked_slots_are_errors_even_without_new_diagnostics(self):
        self.assertEqual(self.decision(blocked_slots=list(range(50)))['phase'], 'uncertain')

    def test_known_rejection_is_retryable_but_undetermined_cost_is_not(self):
        results = {'0': {'outcome': 'rejected', 'attempt': 1, 'error': {
            'type': 'UpstreamRejected', 'stage': 'upstream_response', 'http_status': 503,
            'reason': 'API check rejected with HTTP 503'}}}
        state = self.decision(slot_results=results)
        self.assertTrue(state['allowed'])
        self.assertEqual(state['phase'], 'retryable')
        self.assertEqual(self.decision(blocked_slots=[0], slot_results=results)['phase'], 'uncertain')

    def test_auth_or_request_errors_do_not_claim_automatic_retry(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                state = self.decision(slot_results={'0': {'outcome': 'rejected', 'attempt': 1,
                    'error': {'type': 'UpstreamRejected', 'http_status': code}}})
                self.assertFalse(state['allowed'])
                self.assertEqual(state['phase'], 'rejected')

    def test_completed_batch_and_in_flight_peer_are_not_all_called_successful(self):
        first = {'response_id': 'resp-real', 'number': 1}
        state = self.decision(first_complete=first, slot_results={'0': {'outcome': 'in_flight', 'attempt': 2}})
        self.assertEqual(state['phase'], 'settling')
        self.assertFalse(state['allowed'])
        self.assertEqual(self.decision(first_complete=first,
            slot_results={'0': {'outcome': 'complete', 'attempt': 1}})['phase'], 'complete')
        self.assertEqual(self.decision(first_complete=first)['phase'], 'stopped')

    def test_missing_stale_future_and_wrong_identity_stop_and_explain(self):
        for change, phase in (({'updated_at': time.time() - 10}, 'stale'),
                              ({'updated_at': time.time() + 10}, 'stale'),
                              ({'workspace_id': 'different'}, 'invalid'),
                              ({'job_id': str(uuid.uuid4())}, 'invalid'),
                              ({'attempts': True}, 'invalid'),
                              ({'blocked_slots': ['0']}, 'invalid')):
            with self.subTest(change=change):
                self.assertEqual(self.decision(**change)['phase'], phase)
        self.assertEqual(service.continuation_decision(self.bindings['s-0'], {}, self.wid)['phase'], 'missing')

    def test_paused_exhausted_and_local_fault_have_distinct_states(self):
        for change, phase in (({'authorized': False}, 'paused'), ({'attempts': 1000}, 'exhausted'),
                              ({'fault': 'local accounting unavailable'}, 'fault')):
            with self.subTest(change=change):
                self.assertEqual(self.decision(**change)['phase'], phase)

    def test_legacy_B_and_unrelated_surface_bypass_N_only_gate(self):
        self.decision(blocked_slots=list(range(50)))
        self.assertTrue(service.continuation_allowed(self.path, self.config,
            {**self.target, 'surface_id': 'original-B'}))
        self.assertTrue(service.continuation_allowed(self.path, self.config,
            {**self.target, 'workspace_id': 'different-workspace'}))

    def test_table_focus_error_filter_and_group_counts_show_all_fifty_failures(self):
        self.decision(blocked_slots=list(range(50)))
        model = self.model()
        originals = list(model.candidates)
        with patch('ccc_workspace_batch.snapshots', return_value={}), \
                patch.object(service, 'status', wraps=service.status) as read:
            model._refresh_batch_snapshots()
        self.assertEqual(read.call_count, 1, 'one status read per job, not per native surface')
        self.assertEqual(len(tui.filter_candidates(model.candidates, 'errors')), 50)
        self.assertEqual(tui.group_counts(model.candidates)['errors'], 50)
        for row, original in zip(model.candidates, originals):
            self.assertEqual(tui.screen_label(row), '结果不明')
            self.assertEqual(tui.error_label(row), '结果不明')
            self.assertIn('不自动重发', tui.focus_summary(row))
            self.assertNotIn('运行中', tui.focus_summary(row))
            self.assertNotIn('等待恢复', tui.focus_summary(row))
            self.assertIsNot(row, original, 'async refresh must not mutate published candidates')
            self.assertFalse(original.access)

    def test_detailed_protocol_error_reaches_error_column_and_focus(self):
        detail = {'stage': 'upstream_response', 'type': 'ProtocolFault',
                  'reason': 'upstream did not return Responses SSE'}
        self.decision(blocked_slots=[0], slot_results={'0': {'outcome': 'uncertain', 'attempt': 1, 'error': detail}})
        model = self.model()
        with patch('ccc_workspace_batch.snapshots', return_value={}):
            model._refresh_batch_snapshots()
        self.assertEqual(tui.error_label(model.candidates[0]), '协议异常')
        self.assertIn('非 SSE', tui.focus_summary(model.candidates[0]))
        self.assertEqual(len(tui.filter_candidates(model.candidates, 'errors')), 1)

    def test_refresh_clears_old_error_when_another_slot_retries_and_scopes_new_workspace(self):
        self.decision(blocked_slots=[0])
        model = self.model()
        with patch('ccc_workspace_batch.snapshots', return_value={}):
            model._refresh_batch_snapshots()
            self.decision(attempts=51, slot_results={'0': {'outcome': 'in_flight', 'attempt': 51}})
            model._refresh_batch_snapshots()
        self.assertEqual(tui.screen_label(model.candidates[0]), '检查中')
        self.assertEqual(tui.error_label(model.candidates[0]), '—')
        self.assertEqual(tui.filter_candidates(model.candidates, 'errors'), [])
        model.candidates[0] = replace(model.candidates[0],
            record={**model.candidates[0].record, 'workspace_id': 'elsewhere'})
        with patch('ccc_workspace_batch.snapshots', return_value={}):
            model._refresh_batch_snapshots()
        self.assertFalse(model.candidates[0].access)

    def test_corrupt_status_detail_cannot_render_credentials_or_terminal_escapes(self):
        state = self.decision(blocked_slots=[0], slot_results={'0': {'outcome': 'uncertain', 'attempt': 1,
            'error': {'stage': 'upstream_response', 'type': 'ProtocolFault',
                      'reason': '\x1b[31m Bearer private-fixture'}}})
        self.assertNotIn('private-fixture', json.dumps(state))
        self.assertNotIn('\x1b', state.get('detail', ''))

    def test_bad_completion_and_counter_records_do_not_fake_success(self):
        for change in ({'first_complete': True}, {'first_complete': {}},
                       {'first_complete': {'response_id': 'fake', 'number': 51}},
                       {'forwarded': 51}, {'in_flight': -1},
                       {'slot_results': {'0': {'outcome': 'complete', 'attempt': 51}}}):
            with self.subTest(change=change):
                self.assertEqual(self.decision(**change)['phase'], 'invalid')

    def test_footer_reports_live_failure_instead_of_launch_completion(self):
        value = {**self.value, 'blocked_slots': list(range(50))}
        text = tui.access_batch_progress(value)
        self.assertIn('50路结果不明', text)
        self.assertIn('不自动重发', text)
        self.assertIn('HTTP 50/1000', text)
        self.assertNotIn('已接通', text)

    def test_unsafe_diagnostic_shapes_cannot_crash_the_projection(self):
        for detail in ({'type': []}, {'type': 'ProtocolFault', 'reason': {'private': 'data'}},
                       {'type': 'UpstreamRejected', 'http_status': 'Bearer fixture-private'},
                       {'stage': [], 'type': 'ProtocolFault'},
                       {'stage': {}, 'type': 'ProtocolFault'}):
            with self.subTest(detail=detail):
                state = self.decision(blocked_slots=[0], slot_results={
                    '0': {'outcome': 'uncertain', 'attempt': 1, 'error': detail}})
                self.assertEqual(state['phase'], 'uncertain')
                self.assertNotIn('fixture-private', json.dumps(state))

    def test_invalid_outcome_shape_stops_sending_without_breaking_the_table(self):
        for outcome in ([], {}, 1, True):
            with self.subTest(outcome=outcome):
                state = self.decision(slot_results={
                    '0': {'outcome': outcome, 'attempt': 1}})
                self.assertEqual(state['phase'], 'invalid')
                self.assertFalse(state['allowed'])
                model = self.model()
                with patch('ccc_workspace_batch.snapshots', return_value={}):
                    model._refresh_batch_snapshots()
                self.assertTrue(model.candidates[0].access['alarming'])
                self.assertEqual(len(tui.filter_candidates(model.candidates, 'errors')), 1)


if __name__ == '__main__':
    unittest.main()
