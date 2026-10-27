"""A populated cmux row must not strand the same Claude when top goes empty."""
import contextlib
import copy
import tempfile
import time
import unittest
from unittest import mock

import cmux_codex_watch as w
import ccc_native_processes as native
from claude_ccc_protocol import classify_stop_failure
from tests import test_claude_delivery_recovery as delivery_recovery
from tests.test_watch import process_fixture, process_fixture_with_pid


class GuiNativeContinuityTests(unittest.TestCase):
    @contextlib.contextmanager
    def case(self):
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
            daemon, target, runtime, event, client = delivery_recovery.DeliveryRecoveryTests().setup_case(root)
            runtime.claude_process_pid = 1234
            client.top_data = process_fixture_with_pid('surface-uuid', 'claude', 1234)
            row = dict(pid=1234, birth=(10, 27), agent_kind='claude', surface_id='surface-uuid',
                       environment_workspace_id='workspace-uuid', pgid=1234, tpgid=1234, tdev=77)
            target_proof = dict(surface_id='surface-uuid', workspace_id='workspace-uuid',
                                pane_id='pane-uuid', window_id='window-uuid', tty='ttys077')
            raw_records = w.display_surface_records

            def records(tree, values):
                # The fixture's non-UUID selectors omit display metadata.
                # Model the already-validated UUID/TTY join from the real host;
                # native grouping, foreground, identity and proof checks below
                # use their actual implementation.
                return [dict(value, native_display_target=target_proof)
                        if value.get('surface_id') == target['surface_id'] else value
                        for value in raw_records(tree, values)]

            scan = stack.enter_context(mock.patch('ccc_guard_scope.scan', return_value=[row]))
            process = stack.enter_context(mock.patch.object(native, 'display_process', side_effect=lambda pid: row if pid == row['pid'] else None))
            device = stack.enter_context(mock.patch.object(native, '_display_tty_device', return_value=77))
            stack.enter_context(mock.patch.object(w, 'display_surface_records', side_effect=records))
            stack.enter_context(mock.patch.object(daemon, '_claude_send_process_identity',
                                                 return_value=dict(pid=1234, generation='generation-a', started_epoch=10.000027)))
            stack.enter_context(mock.patch.object(daemon, '_notify'))
            stack.enter_context(mock.patch.object(daemon, '_notify_async'))
            yield daemon, target, runtime, event, client, row, scan, process, device

    def goes_empty(self, client):
        populated = copy.deepcopy(client.top_data)
        count = 0

        def top(workspace):
            nonlocal count
            count += 1
            client.top_calls.append(workspace)
            return populated if count == 1 else process_fixture()

        client.top = top

    def test_populated_gui_pins_native_proof_before_first_empty_read(self):
        with self.case() as (d, t, r, e, c, row, *_):
            self.goes_empty(c)
            first = d._live_claude_process_label(t, c)
            self.assertEqual(first.get('agent_pid'), 1234)
            self.assertIn('native_identity', first)
            pinned = copy.deepcopy(first['native_identity'])
            second = d._live_claude_process_label(t, c, pinned=pinned, allow_unpinned=False)
            self.assertEqual(second['native_identity'], pinned)
            self.assertEqual(second['agent_pid'], first['agent_pid'])

    def test_native_proof_survives_json_round_trip_and_gui_recovery(self):
        import json
        with self.case() as (d, t, r, e, c, *_):
            proof = d._live_claude_process_label(t, c).get('native_identity')
            self.assertIsNotNone(proof)
            proof = json.loads(json.dumps(proof))
            self.assertEqual(d._live_claude_process_label(t, c, pinned=proof, allow_unpinned=False)['agent_pid'], 1234)

    def test_gui_failure_does_not_strand_verified_native_continuation(self):
        with self.case() as (d, t, r, e, c, *_):
            c.top = mock.Mock(side_effect=w.CmuxError('system.top response deadline exceeded'))
            self.assertTrue(d._send_claude_event(e, t, r, c)[0])
            self.assertEqual(len(c.sent_text), 1)
            self.assertEqual(len(c.sent_keys), 1)
            self.assertEqual(d.claude_event_ledger.status_of(e['event_id']), 'sent')
            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
            self.assertEqual(len(c.sent_text), 1)
            self.assertEqual(len(c.sent_keys), 1)

    def test_gui_failure_keeps_mixed_native_surface_ambiguous(self):
        with self.case() as (d, t, r, e, c, row, scan, *_):
            c.top = mock.Mock(side_effect=w.CmuxError('system.top response deadline exceeded'))
            scan.return_value = [row, dict(row, pid=777, agent_kind='codex')]
            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
            self.assertEqual(c.sent_text, [])
            self.assertEqual(c.sent_keys, [])

    def test_bounded_gui_read_never_borrows_input_socket(self):
        import types
        borrowed = mock.Mock(side_effect=AssertionError('must preserve input socket'))
        transport = types.SimpleNamespace(
            _connection_local=types.SimpleNamespace(read_rpc=borrowed, input_remaining=lambda: 4),
            connection_window=lambda budget: contextlib.nullcontext(budget))
        client = w.CmuxClient(viewport_socket=transport)
        with mock.patch.object(client, '_control_rpc_once', side_effect=w.CmuxError('timeout')) as read:
            self.assertEqual(client.top_for_input('workspace-uuid'), {})
        self.assertEqual(read.call_args.kwargs['timeout'], 0.5)
        borrowed.assert_not_called()

    def test_401_and_deleted_group_403_send_once_after_native_backoff(self):
        banners = ('⏺ Please run /login · API Error: 401 百倍0.08: API key is disabled',
                   '⏺ Please run /login · API Error: 403 百倍: API Key 所属分组已删除')
        for banner in banners:
            with self.subTest(banner=banner), self.case() as (d, t, r, e, c, *_):
                failure = dict(e, **classify_stop_failure('authentication_failed', banner),
                               agent_pid=1234, synthetic_fallback=False, created_at=time.time())
                self.assertEqual(failure['error_kind'], 'claude_auth')
                self.assertFalse(failure['provider_blocked'])
                guard = d._remember_claude_provider_failure(t, r, failure, event=failure)
                d.claude_event_ledger.mark(failure, 'provider_retry_wait')
                self.goes_empty(c)
                with mock.patch.object(w.time, 'time', return_value=guard['retry_after'] + 1):
                    stopped = w.ScreenState('claude_stopped', message_kind='claude')
                    self.assertTrue(d._maybe_send_deferred_claude_stop(t, r, stopped, c))
                    self.assertFalse(d._maybe_send_deferred_claude_stop(t, r, stopped, c))
                self.assertEqual(len(c.sent_text), 1)
                self.assertEqual(len(c.sent_keys), 1)
                self.assertEqual(r.send_count, 1)
                self.assertEqual(d.claude_event_ledger.status_of(failure['event_id']), 'sent')

    def test_populated_gui_and_native_pid_conflict_is_refused(self):
        with self.case() as (d, t, r, e, c, row, *_):
            row['pid'] = row['pgid'] = row['tpgid'] = 777
            self.assertEqual(d._live_claude_process_label(t, c)['agent_kind'], 'unknown')
            self.assertEqual(c.sent_text, [])
            self.assertEqual(c.sent_keys, [])

    def test_duplicate_claude_on_same_surface_remains_ambiguous(self):
        with self.case() as (d, t, r, e, c, row, scan, *_):
            scan.return_value = [row, dict(row, pid=777)]
            self.assertEqual(d._live_claude_process_label(t, c)['agent_kind'], 'unknown')

    def test_unrelated_surface_does_not_build_an_input_proof(self):
        with self.case() as (d, t, r, e, c, row, scan, process, *_):
            records = w.display_surface_records.side_effect
            foreign = dict(row, pid=777, surface_id='other-surface')
            scan.return_value = [row, foreign]
            process.side_effect = lambda pid: row if pid == row['pid'] else foreign
            other_record = dict(surface_id='other-surface', native_display_target=dict(
                surface_id='other-surface', workspace_id='other-workspace',
                pane_id='other-pane', window_id='window-uuid', tty='ttys077'))
            with mock.patch.object(w, 'display_surface_records', side_effect=lambda tree, values:
                                   records(tree, values) + [other_record]):
                live = d._live_claude_process_label(t, c)
            self.assertEqual(live['agent_pid'], row['pid'])
            self.assertTrue(process.call_args_list)
            self.assertTrue(all(call.args == (row['pid'],) for call in process.call_args_list))

    def test_pinned_birth_and_tty_changes_are_not_adopted(self):
        for change in ('birth', 'tty'):
            with self.subTest(change=change), self.case() as (d, t, r, e, c, row, scan, process, device):
                proof = copy.deepcopy(d._live_claude_process_label(t, c).get('native_identity'))
                self.assertIsNotNone(proof)
                c.top_data = process_fixture()
                if change == 'birth':
                    row['birth'] = (20, 99)
                else:
                    device.return_value = 78
                self.assertEqual(d._live_claude_process_label(t, c, pinned=proof, allow_unpinned=False)['agent_kind'], 'unknown')

    def test_populated_conflicting_agent_after_pin_is_refused(self):
        with self.case() as (d, t, r, e, c, *_):
            proof = d._live_claude_process_label(t, c).get('native_identity')
            self.assertIsNotNone(proof)
            c.top_data = process_fixture_with_pid('surface-uuid', 'codex', 1234)
            self.assertEqual(d._live_claude_process_label(t, c, pinned=proof, allow_unpinned=False)['agent_kind'], 'unknown')

    def test_old_pending_without_original_proof_cannot_acquire_one(self):
        with self.case() as (d, t, r, e, c, row, scan, *_):
            c.top_data = process_fixture()
            scan.reset_mock()
            self.assertEqual(d._live_claude_process_label(t, c, allow_unpinned=False)['agent_kind'], 'unknown')
            scan.assert_not_called()
            r.claude_submit_phase = 'text_written'
            r.claude_submit_write_unknown = True
            d.claude_event_ledger.mark(e, 'reserved')
            original = copy.deepcopy(r.to_dict())
            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
            self.assertEqual(r.to_dict(), original)
            self.assertEqual(d.claude_event_ledger.status_of(e['event_id']), 'reserved')
            self.assertEqual(c.sent_text, [])
            self.assertEqual(c.sent_keys, [])


if __name__ == '__main__':
    unittest.main()
