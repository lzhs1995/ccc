"""Finite idle lifecycle and original-request safety; no live terminal is used."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import cmux_bridge
import executor_ready as ready


class FakeBridge:
    def __init__(self, *, native=True, queued=False, pin_error=None):
        self.native, self.queued, self.pin_error = native, queued, pin_error
        self.submitted, self.receipts = [], {}
        self.caller = ''
        self.dispatch_error = None

    def pin_workspace(self, surface, **kwargs):
        if self.pin_error:
            raise self.pin_error
        self.caller = kwargs.get('caller_uuid') or self.caller
        return dict(caller_surface_uuid=self.caller)

    def _looks_like_task_dispatch(self, text):
        return cmux_bridge._looks_like_task_dispatch(text)

    def submit_text(self, surface, text, marker=None, reconcile_only=False, recover_stranded=False):
        if recover_stranded:
            raise AssertionError('idle observation must never recover input')
        self.submitted.append(dict(marker=marker, text=text, reconcile_only=reconcile_only,
                                   recover_stranded=recover_stranded))
        journal = ready.state_root() / 'message-dispatch-v1' / ready.request_key(self.caller, marker)
        if not reconcile_only:
            ready.write_json(journal / 'attempt-0001.json', dict(phase='PASTED'))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        if self.queued:
            raise cmux_bridge.DispatchUnconfirmed('queued', state='DELIVERY_QUEUED_AT_RECEIVER')
        if self.native:
            self.receipts[marker] = dict(source='fixture_native_receipt')
        return dict(state='CONFIRMED')  # 此返回本身不能构成原生送达证明。


class Clock:
    def __init__(self):
        self.now = time.time()
        self.start = self.now
        self.on_sleep = None
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)


class FiniteReadyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='finite-ready-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.caller = str(uuid.uuid4())
        self.bridge = FakeBridge()
        self.bridge.caller = self.caller
        self.clock = Clock()
        for patcher in (patch.object(ready, 'state_root', return_value=self.root / 'state'),
                        patch.object(ready, '_receipt',
                                     side_effect=lambda b, s, t, m: b.receipts.get(m))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_loop(self, bridge=None, **kwargs):
        return ready.run_loop(bridge or self.bridge, 'surface:1', self.caller,
                              'surface:2', 'task-x', clock=self.clock.time,
                              sleep=self.clock.sleep, **kwargs)

    def ask(self, **kwargs):
        return self.run_loop(send_once=True, one_shot=True, **kwargs)

    def mailbox_reply(self, record, **overrides):
        marker = (record['asks'] or [{}])[-1].get('marker')
        body = dict(marker=marker, caller_surface_uuid=self.caller,
                    supervisor_uuid='surface:1', task_id='task-x',
                    episode_id=record['episode_id'], status='SOLO', trigger='scope complete')
        body.update(overrides)
        path = Path(record['mailbox']) / (marker + '.json' if marker else 'reply.json')
        ready.write_json(path, body)
        return path

    def test_default_persist_is_finite_and_never_sends_input(self):
        record = self.run_loop()
        self.assertEqual(record['state'], ready.TIMED_OUT)
        self.assertEqual(record['lifecycle_state'], ready.WAITING_DEPENDENCY)
        self.assertFalse(record['monitoring'])
        self.assertEqual(self.bridge.submitted, [])
        self.assertEqual(record['asks'], [])
        self.assertAlmostEqual(self.clock.now - self.clock.start, ready.DEFAULT_WAIT_SECONDS)
        self.assertEqual(ready.read_json(ready.loop_path(self.caller)), record)

    def test_requested_unlimited_duration_is_clamped_to_300_seconds(self):
        record = self.run_loop(max_seconds=10**12, max_ticks=10**9)
        self.assertEqual(record['budget_seconds'], ready.MAX_WAIT_SECONDS)
        self.assertLessEqual(self.clock.now - self.clock.start, 300)
        self.assertEqual(record['state'], ready.TIMED_OUT)
        self.assertEqual(self.bridge.submitted, [])

    def test_tick_budget_is_finite_when_clock_does_not_advance(self):
        record = ready.run_loop(self.bridge, 'surface:1', self.caller, 'surface:2', 'task-x',
                                clock=self.clock.time, sleep=lambda seconds: None)
        self.assertEqual(record['state'], ready.TIMED_OUT)
        self.assertEqual(record['exit_reason'], 'TICK_BUDGET_EXHAUSTED')
        self.assertEqual(record['ticks_total'], ready.MAX_TICKS)

    def test_invalid_time_arguments_never_disable_the_budget(self):
        for value in (float('nan'), float('inf'), -1, 0):
            with self.subTest(value=value):
                self.setUp()
                record = self.run_loop(max_seconds=value, poll=value, interval=value)
                self.assertGreater(record['budget_seconds'], 0)
                self.assertLessEqual(record['budget_seconds'], 300)
                self.assertGreaterEqual(record['poll_seconds'], 1)
                self.assertEqual(record['state'], ready.TIMED_OUT)

    def test_explicit_request_sends_only_once_even_without_a_reply(self):
        record = self.ask(max_seconds=300)
        self.assertEqual(record['state'], ready.WAITING_DEPENDENCY)
        self.assertEqual(record['asks'][0]['outcome'], 'CONFIRMED')
        self.assertIsNone(record['reply'])
        marker = record['asks'][0]['marker']
        record = self.run_loop(max_seconds=300)
        self.assertEqual(record['state'], ready.TIMED_OUT)
        self.assertEqual(record['ask_count'], 1)
        self.assertEqual(len(self.bridge.submitted), 1)
        self.assertEqual(record['asks'][0]['marker'], marker)
        self.assertIsNone(record['reply'])

    def test_second_explicit_ask_cannot_repost_an_unconfirmed_request(self):
        self.bridge.queued = True
        first = self.ask()
        second = self.ask()
        self.assertEqual(first['asks'][0]['marker'], second['asks'][0]['marker'])
        self.assertEqual(second['ask_count'], 1)
        writes = [s for s in self.bridge.submitted if not s['reconcile_only']]
        self.assertEqual(len(writes), 1)
        self.assertFalse(any(s['recover_stranded'] for s in self.bridge.submitted))

    def test_explicit_ask_can_follow_a_passive_observation_without_resetting_episode(self):
        passive = self.run_loop(one_shot=True)
        asked = self.ask()
        repeated = self.ask()
        self.assertEqual(passive['asks'], [])
        self.assertEqual(asked['episode_id'], passive['episode_id'])
        self.assertEqual(asked['deadline_epoch'], passive['deadline_epoch'])
        self.assertEqual(asked['asks'][0]['marker'], repeated['asks'][0]['marker'])
        self.assertEqual(sum(not s['reconcile_only'] for s in self.bridge.submitted), 1)

    def test_restart_only_reconciles_pending_original_and_never_recovers(self):
        self.bridge.queued = True
        record = self.ask(max_seconds=300)
        marker = record['asks'][0]['marker']
        record = self.run_loop(max_seconds=300)
        self.assertEqual(record['state'], ready.TIMED_OUT)
        self.assertEqual(record['ask_count'], 1)
        self.assertEqual({s['marker'] for s in self.bridge.submitted}, {marker})
        self.assertEqual(sum(not s['reconcile_only'] for s in self.bridge.submitted), 1)
        self.assertLessEqual(len(self.bridge.submitted), 6)
        self.assertEqual(record['asks'][0]['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')

    def test_timeout_does_not_reset_on_repeated_invocations(self):
        self.bridge.queued = True
        self.ask()
        first = self.run_loop(max_ticks=2)
        calls = len(self.bridge.submitted)
        for _ in range(3):
            record = self.run_loop(max_seconds=300, send_once=True)
            self.assertEqual(record['state'], ready.TIMED_OUT)
            self.assertEqual(record['deadline_epoch'], first['deadline_epoch'])
            self.assertEqual(record['started_epoch'], first['started_epoch'])
        self.assertEqual(len(self.bridge.submitted), calls)
        self.assertEqual(len(list(ready.requests_dir().glob('*.json'))), 1)

    def test_uncertain_rpc_details_survive_ask_and_original_reconciliation(self):
        details = dict(rpc_outcome='ACCEPTED', controller_provenance={'version': '0.4.35'},
                       reconcile_guidance={'mode': '--reconcile-only', 'input_operations': 0,
                                           'attempt': '/original/attempt-0001.json'})
        error = cmux_bridge.DispatchUnconfirmed('native receipt pending', state='NATIVE_PENDING',
                                               details=details)
        self.bridge.dispatch_error = error
        first = self.ask()
        second = self.run_loop(one_shot=True)
        for record in (first, second):
            ask = record['asks'][0]
            self.assertEqual(ask['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')
            self.assertEqual(ask['dispatch_state'], 'NATIVE_PENDING')
            self.assertEqual(ask['details'], details)
            self.assertEqual(ask['marker'], first['asks'][0]['marker'])
        self.assertEqual(sum(not s['reconcile_only'] for s in self.bridge.submitted), 1)
        self.assertTrue(self.bridge.submitted[-1]['reconcile_only'])
        self.assertEqual(error.details, details)

    def test_preflight_failure_preserves_diagnostics_without_submitting(self):
        details = dict(reason='receiver turn changed', expected_turn_id='original-turn')
        self.bridge.pin_error = cmux_bridge.DispatchUnconfirmed(
            'native preflight unavailable', state='NO_INPUT', details=details)
        ask = ready.prepare_ask(self.bridge, 'surface:1', self.caller, 'surface:2', 'task-x')
        result = ready.advance_ask(self.bridge, 'surface:1', self.caller, ask, allow_send=True)
        self.assertEqual(result['dispatch_state'], 'NO_INPUT')
        self.assertEqual(result['details']['reason'], details['reason'])
        self.assertEqual(result['details']['expected_turn_id'], 'original-turn')
        guidance = result['details']['reconcile_guidance']
        self.assertEqual(guidance['input_operations'], 0)
        self.assertTrue(guidance['reuse_original_parameters'])
        self.assertEqual(self.bridge.submitted, [])
        self.assertEqual(self.bridge.pin_error.details, details)

    def test_observation_error_cannot_reopen_an_episode_ended_by_tick_budget(self):
        first = self.run_loop(max_ticks=1)
        self.assertEqual(first['state'], ready.TIMED_OUT)
        stop = ready.stop_path(self.caller)
        stop.write_text('{')
        failed = self.run_loop()
        self.assertEqual(failed['state'], ready.TIMED_OUT)
        self.assertIn('STOP_RECORD_UNREADABLE', failed['last_error'])
        stop.unlink()
        restarted = self.ask()
        self.assertEqual(restarted['state'], ready.TIMED_OUT)
        self.assertEqual(restarted['deadline_epoch'], first['deadline_epoch'])
        self.assertEqual(restarted['asks'], [])
        self.assertEqual(self.bridge.submitted, [])

    def test_received_mailbox_reply_ends_the_wait(self):
        record = self.ask()
        self.clock.on_sleep = lambda c: self.mailbox_reply(record)
        result = self.run_loop()
        self.assertEqual(result['state'], ready.ANSWERED)
        self.assertEqual(result['reply']['source'], 'mailbox')
        self.assertEqual(result['reply']['status'], 'SOLO')
        self.assertEqual(len(self.bridge.submitted), 1)

    def test_mailbox_cannot_answer_a_passive_episode_with_no_issued_marker(self):
        record = self.run_loop(one_shot=True)
        self.mailbox_reply(record, status=ready.WAITING_DEPENDENCY)
        result = self.run_loop()
        self.assertEqual(result['state'], ready.TIMED_OUT)
        self.assertIsNone(result['reply'])
        self.assertEqual(self.bridge.submitted, [])

    def test_mailbox_wrong_binding_and_transport_status_cannot_claim_reply(self):
        record = self.ask()
        for override in (dict(marker='other'), dict(caller_surface_uuid='foreign'),
                         dict(task_id='foreign'), dict(status='DELIVERY_QUEUED_AT_RECEIVER'),
                         dict(episode_id='foreign'), dict(episode_id=None), dict(episode_id=''),
                         dict(queued=True), dict(received=False)):
            with self.subTest(override=override):
                path = self.mailbox_reply(record, **override)
                reply = ready.find_reply(record['mailbox'], None,
                                         [record['asks'][0]['marker']], record['started_epoch'],
                                         self.caller, 'task-x', record['episode_id'])
                self.assertIsNone(reply)
                path.unlink()

    def test_old_or_unbound_mailbox_file_is_not_a_new_reply(self):
        record = self.ask()
        path = self.mailbox_reply(record)
        os.utime(path, (self.clock.start - 10, self.clock.start - 10))
        arbitrary = Path(record['mailbox']) / 'unrelated.json'
        ready.write_json(arbitrary, dict(status='SOLO'))
        reply = ready.find_reply(record['mailbox'], None, [record['asks'][0]['marker']],
                                 record['started_epoch'], self.caller, 'task-x', record['episode_id'])
        self.assertIsNone(reply)

    def test_mailbox_requires_explicit_task_binding_even_when_task_id_is_empty(self):
        mailbox = self.root / 'mailbox'
        body = dict(marker='expected', caller_surface_uuid=self.caller, status='SOLO',
                    episode_id='episode', trigger='scope complete')
        for fields in (body, dict(body, task_id='foreign')):
            ready.write_json(mailbox / 'expected.json', fields)
            self.assertIsNone(ready.find_reply(mailbox, None, ['expected'], 0,
                                               self.caller, '', 'episode'))
        ready.write_json(mailbox / 'expected.json', dict(body, task_id=''))
        self.assertEqual(ready.find_reply(mailbox, None, ['expected'], 0,
                                          self.caller, '', 'episode')['status'], 'SOLO')

    def test_request_template_carries_original_episode_and_reply_is_accepted(self):
        record = self.ask()
        request = json.loads(Path(record['asks'][0]['record']).read_text())
        self.assertEqual(request['episode_id'], record['episode_id'])
        import cmux_prompt_reference as reference
        body, pin = reference.read_body(request['body_reference'])
        self.assertEqual(reference.validate_wire_body(request['text']), pin)
        self.assertLessEqual(len(request['text'].encode('utf-8')), reference.MAX_INLINE_BYTES)
        self.assertIn(request['marker'], body)
        template = body.split(' containing ', 1)[1].split('}.', 1)[0] + '}'
        body = json.loads(template)
        self.assertEqual(body['episode_id'], record['episode_id'])
        self.assertEqual(body['supervisor_uuid'], 'surface:1')
        self.assertFalse(ready.reply_contract.valid(body, [body['marker']], caller=self.caller,
                                                   task_id='task-x', episode_id=record['episode_id']))
        body['trigger'] = 'install successor after its offline suite passes'
        ready.write_json(Path(record['mailbox']) / (body['marker'] + '.json'), body)
        self.assertIsNotNone(ready.find_reply(record['mailbox'], None, [body['marker']],
                                             record['started_epoch'], self.caller, 'task-x',
                                             record['episode_id']))

    def test_mailbox_missing_episode_cannot_close_current_wait(self):
        record = self.ask()
        path = self.mailbox_reply(record)
        body = json.loads(path.read_text())
        del body['episode_id']
        ready.write_json(path, body)
        self.assertIsNone(ready.find_reply(record['mailbox'], None,
                                          [record['asks'][0]['marker']], record['started_epoch'],
                                          self.caller, 'task-x', record['episode_id']))

    def test_late_reply_after_timeout_is_observed_without_reopening_or_reposting(self):
        record = self.ask()
        self.run_loop(max_ticks=1)
        self.mailbox_reply(record)
        result = self.run_loop()
        self.assertEqual(result['state'], ready.ANSWERED)
        self.assertEqual(len(self.bridge.submitted), 1)

    def test_queued_transcript_event_is_not_received(self):
        events = [dict(type='attachment', attachment=dict(type='queued_command', prompt='STATUS: SOLO')),
                  dict(type='user', attachment=dict(type='queued_command', prompt='ACK'),
                       message=dict(content='STATUS: queued'))]
        for event in events:
            self.assertEqual(ready.user_texts(event), [])

    def test_only_real_user_turn_ends_transcript_wait(self):
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('')
        noise = [dict(type='attachment', attachment=dict(type='queued_command', prompt='STATUS: SOLO')),
                 dict(type='user', message=dict(content=[dict(type='tool_result', content='STATUS: ok')])),
                 dict(type='user', isMeta=True, message=dict(content='STATUS: meta')),
                 dict(type='assistant', message=dict(content='STATUS: mine')),
                 dict(type='user', message=dict(content='Stop hook feedback: SOLO'))]
        asked = self.ask(transcript=str(transcript))
        body = dict(marker=asked['asks'][0]['marker'], caller_surface_uuid=self.caller,
                    supervisor_uuid='surface:1', task_id='task-x', episode_id=asked['episode_id'],
                    status='WAITING_DEPENDENCY', trigger='release installed')
        real = dict(type='user', message=dict(role='user', content=json.dumps(body)))

        def feed(clock):
            with transcript.open('a') as handle:
                handle.write(json.dumps(noise.pop(0) if noise else real) + '\n')

        self.clock.on_sleep = feed
        record = self.run_loop(transcript=str(transcript))
        self.assertEqual(record['state'], ready.ANSWERED)
        self.assertEqual(record['reply']['source'], 'transcript')
        self.assertEqual(noise, [])
        self.assertEqual(len(self.bridge.submitted), 1)

    def test_downtime_reply_uses_original_transcript_inode_and_offset(self):
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('')
        asked = self.ask(transcript=str(transcript))
        reply = dict(marker=asked['asks'][0]['marker'], caller_surface_uuid=self.caller,
                     supervisor_uuid='surface:1', task_id='task-x', episode_id=asked['episode_id'], status='ACK')
        with transcript.open('a') as handle:
            handle.write(json.dumps(dict(type='user', message=dict(content=json.dumps(reply)))) + '\n')
        record = self.run_loop(transcript=str(transcript))
        self.assertEqual(record['state'], ready.ANSWERED)
        self.assertEqual(len(self.bridge.submitted), 1)

    def test_changed_transcript_preserves_original_request_and_stops_observation(self):
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('')
        self.ask(transcript=str(transcript))
        replacement = self.root / 'replacement.jsonl'
        replacement.write_text('{"type":"user","message":{"content":"STATUS: forged"}}\n')
        replacement.replace(transcript)
        record = self.run_loop(transcript=str(transcript))
        self.assertEqual(record['state'], ready.WAITING_DEPENDENCY)
        self.assertIn('REPLY_TRANSCRIPT_CHANGED', record['last_error'])
        self.assertIsNone(record['reply'])
        self.assertEqual(len(self.bridge.submitted), 1)

    def test_transcript_read_is_bounded_and_partial_records_are_not_consumed(self):
        transcript = self.root / 'large.jsonl'
        transcript.write_text('')
        tail = ready.Tail(transcript)
        line = json.dumps(dict(type='user', message=dict(content='ordinary note'))) + '\n'
        with transcript.open('a') as handle:
            handle.write(line * (ready.MAX_TRANSCRIPT_BYTES // len(line) + 10))
        tail.lines()
        self.assertLessEqual(tail.offset, ready.MAX_TRANSCRIPT_BYTES)
        self.assertLess(tail.offset, transcript.stat().st_size)
        incomplete = self.root / 'partial.jsonl'
        incomplete.write_text('')
        tail = ready.Tail(incomplete)
        incomplete.write_text('{"type":"user"')
        self.assertEqual(tail.lines(), [])
        self.assertEqual(tail.offset, 0)

    def test_stop_and_new_dispatch_use_existing_stop_file_without_input(self):
        for reason in ('OPERATOR', 'DISPATCHED'):
            with self.subTest(reason=reason):
                self.setUp()
                self.clock.on_sleep = lambda c: ready.stop_loop(self.caller, reason)
                record = self.run_loop()
                self.assertEqual(record['state'], ready.STOPPED)
                self.assertEqual(record['stop']['reason'], reason)
                before = ready.stop_path(self.caller).read_bytes()
                self.run_loop(send_once=True)
                self.assertEqual(ready.stop_path(self.caller).read_bytes(), before)
                self.assertEqual(self.bridge.submitted, [])

    def test_stop_signal_is_honored_before_opening_an_unreadable_transcript(self):
        ready.stop_loop(self.caller, 'DISPATCHED')
        result = self.ask(transcript=str(self.root / 'missing-transcript.jsonl'))
        self.assertEqual(result['state'], ready.STOPPED)
        self.assertEqual(result['stop']['reason'], 'DISPATCHED')
        self.assertEqual(result['asks'], [])
        self.assertEqual(self.bridge.submitted, [])

    def test_damaged_stop_record_never_authorizes_an_explicit_request(self):
        for kind in ('malformed', 'missing-reason', 'oversized', 'dangling-link'):
            with self.subTest(kind=kind):
                self.setUp()
                path = ready.stop_path(self.caller)
                path.parent.mkdir(parents=True)
                if kind == 'dangling-link':
                    path.symlink_to(self.root / 'missing-stop.json')
                    before = os.readlink(path)
                else:
                    path.write_text({'malformed': '{', 'missing-reason': '{}',
                                     'oversized': 'x' * (ready.MAX_JSON_BYTES + 1)}[kind])
                    before = path.read_bytes()
                result = self.ask()
                self.assertEqual(result['state'], ready.WAITING_DEPENDENCY)
                self.assertIn('STOP_RECORD_UNREADABLE', result['last_error'])
                self.assertEqual(result['asks'], [])
                self.assertEqual(self.bridge.submitted, [])
                self.assertEqual(os.readlink(path) if path.is_symlink() else path.read_bytes(), before)

    def test_task_files_and_unconfirmed_request_remain_after_timeout(self):
        marker = self.root / 'armed-task.json'
        marker.write_text('{"task_id":"task-x","armed":true}')
        before = marker.read_bytes()
        self.bridge.queued = True
        first = self.ask()
        request_path = Path(first['asks'][0]['record'])
        request_before = request_path.read_bytes()
        result = self.run_loop(max_ticks=1)
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(request_path.read_bytes(), request_before)
        self.assertEqual(result['state'], ready.TIMED_OUT)
        self.assertEqual(result['asks'][0]['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')

    def test_native_receipt_is_required_even_when_bridge_returns_confirmed(self):
        self.bridge.native = False
        record = self.ask()
        self.assertEqual(record['asks'][0]['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')
        self.assertIsNone(record['reply'])
        self.run_loop(max_seconds=300)
        self.assertEqual(sum(not s['reconcile_only'] for s in self.bridge.submitted), 1)

    def test_legacy_confirmed_claim_cannot_authorize_a_new_marker(self):
        record = self.ask()
        marker = record['asks'][0]['marker']
        self.bridge.receipts.clear()
        self.bridge.native = False
        result = self.run_loop(max_ticks=1)
        self.assertEqual(result['asks'][0]['marker'], marker)
        self.assertEqual(result['asks'][0]['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')
        self.assertEqual(sum(not s['reconcile_only'] for s in self.bridge.submitted), 1)

    def test_missing_original_journal_after_restart_never_recreates_input(self):
        record = self.ask()
        ask = record['asks'][0]
        original = ready.read_json(ask['record'])
        original.pop('version')
        ready.write_json(ask['record'], original)
        journal = ready.state_root() / 'message-dispatch-v1' / ready.request_key(self.caller, ask['marker'])
        (journal / 'attempt-0001.json').unlink()
        self.bridge.receipts.clear()
        self.bridge.submitted.clear()
        result = self.run_loop(max_ticks=1)
        self.assertIn('LEGACY_REQUEST_UNVERIFIABLE', result['asks'][0]['detail'])
        self.assertEqual(self.bridge.submitted, [])

    def test_corrupted_original_request_never_causes_new_marker_or_input(self):
        record = self.ask()
        path = Path(record['asks'][0]['record'])
        original = ready.read_json(path)
        original['text'] += ' tampered'
        ready.write_json(path, original)
        self.bridge.submitted.clear()
        result = self.run_loop(max_ticks=1)
        self.assertIn('ORIGINAL_REQUEST_REQUIRED', result['asks'][0]['detail'])
        self.assertEqual(result['ask_count'], 1)
        self.assertEqual(self.bridge.submitted, [])

    def test_prepared_orphan_is_adopted_read_only_after_a_crash(self):
        orphan = ready.prepare_ask(self.bridge, 'surface:1', self.caller, 'surface:2', 'task-x')
        result = self.ask()
        self.assertEqual(result['asks'][0]['marker'], orphan['marker'])
        self.assertEqual(result['asks'][0]['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')
        self.assertEqual(self.bridge.submitted, [])

    def test_damaged_orphan_preserves_evidence_and_prevents_a_fresh_request(self):
        path = ready.requests_dir() / 'orphan.json'
        path.parent.mkdir(parents=True)
        path.write_text('{')
        result = self.ask()
        self.assertEqual(result['state'], ready.WAITING_DEPENDENCY)
        self.assertIn('REQUEST_RECORD_UNREADABLE', result['last_error'])
        self.assertEqual(path.read_text(), '{')
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual(self.bridge.submitted, [])

    def test_original_attempt_scan_is_bounded_without_resending(self):
        record = self.ask()
        marker = record['asks'][0]['marker']
        journal = ready.state_root() / 'message-dispatch-v1' / ready.request_key(self.caller, marker)
        for index in range(3):
            (journal / ('diagnostic-' + str(index))).write_text('original evidence')
        self.bridge.receipts.clear()
        self.bridge.submitted.clear()
        with patch.object(ready, 'MAX_REQUEST_SCAN', 2):
            result = self.run_loop(max_ticks=1)
        self.assertIn('ORIGINAL_ATTEMPT_SCAN_BUDGET_EXHAUSTED', result['asks'][0]['detail'])
        self.assertEqual(result['asks'][0]['marker'], marker)
        self.assertEqual(self.bridge.submitted, [])

    def test_unreadable_original_attempt_is_not_reconciled_or_reposted(self):
        record = self.ask()
        marker = record['asks'][0]['marker']
        journal = ready.state_root() / 'message-dispatch-v1' / ready.request_key(self.caller, marker)
        path = journal / 'attempt-0001.json'
        path.write_text('{')
        self.bridge.receipts.clear()
        self.bridge.submitted.clear()
        result = self.run_loop(max_ticks=1)
        self.assertIn('ORIGINAL_ATTEMPT_UNREADABLE', result['asks'][0]['detail'])
        self.assertEqual(path.read_text(), '{')
        self.assertEqual(self.bridge.submitted, [])

    def test_crash_after_paste_retains_original_marker_and_never_reposts(self):
        submit = self.bridge.submit_text

        def crash(surface, text, **kwargs):
            stored = ready.read_json(ready.loop_path(self.caller))
            self.assertEqual(stored['asks'][-1]['marker'], kwargs['marker'])
            submit(surface, text, **kwargs)
            raise KeyboardInterrupt('fixture crash after input')

        with patch.object(self.bridge, 'submit_text', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.ask()
        result = self.run_loop(max_ticks=1)
        self.assertEqual(result['ask_count'], 1)
        self.assertEqual(len(self.bridge.submitted), 1)

    def test_foreign_episode_or_unreadable_loop_is_not_overwritten(self):
        self.ask()
        path = ready.loop_path(self.caller)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_IDLE_EPISODE'):
            ready.run_loop(self.bridge, 'surface:999', self.caller, task_id='new', max_ticks=1)
        self.assertEqual(path.read_bytes(), before)
        path.write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'UNREADABLE_IDLE_EPISODE'):
            self.run_loop(send_once=True)
        self.assertEqual(path.read_text(), 'corrupted')

    def test_dangling_loop_record_is_preserved_instead_of_starting_a_new_episode(self):
        path = ready.loop_path(self.caller)
        path.parent.mkdir(parents=True)
        target = self.root / 'unavailable-episode.json'
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'UNREADABLE_IDLE_EPISODE'):
            self.ask()
        self.assertEqual(os.readlink(path), str(target))
        self.assertEqual(self.bridge.submitted, [])

    def test_broken_bridge_ends_with_saved_unconfirmed_state_without_retrying_keys(self):
        self.bridge.pin_error = RuntimeError('WORKSPACE_SCOPE_DENIED')
        record = self.ask()
        self.assertIn('WORKSPACE_SCOPE_DENIED', record['asks'][0]['detail'])
        result = self.run_loop(max_ticks=2)
        self.assertEqual(result['state'], ready.TIMED_OUT)
        self.assertEqual(result['ask_count'], 1)
        self.assertEqual(self.bridge.submitted, [])

    def test_wall_clock_budget_interrupts_a_blocked_bridge_call(self):
        blocked_elapsed = []

        def blocked(*args, **kwargs):
            delay, repeat = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 5)
            self.assertEqual(repeat, 0)
            # Check the real installed timer/handler, but shorten the timer only
            # after reaching the bridge. CI setup I/O is not the blocked call.
            signal.setitimer(signal.ITIMER_REAL, 0.03)
            start = time.monotonic()
            try:
                time.sleep(2)
            finally:
                blocked_elapsed.append(time.monotonic() - start)

        with patch.object(self.bridge, 'pin_workspace', side_effect=blocked) as pin:
            result = self.ask(max_seconds=5)
        pin.assert_called_once()
        self.assertEqual(len(blocked_elapsed), 1)
        self.assertLess(blocked_elapsed[0], 0.5)
        self.assertEqual(result['state'], ready.TIMED_OUT)
        self.assertEqual(result['exit_reason'], 'WALL_CLOCK_BUDGET_EXHAUSTED')
        self.assertEqual(result['ask_count'], 1)
        self.assertEqual(self.bridge.submitted, [])

    def test_setup_exhaustion_times_out_before_entering_the_bridge(self):
        monotonic_now = [100.0]
        write = ready.write_json
        writes = []

        def slow_initial_record(path, record):
            write(path, record)
            writes.append(path)
            if len(writes) == 1:
                monotonic_now[0] += 0.06

        # Isolate elapsed-budget accounting; the preceding test checks the
        # actual signal interruption independently of fixture setup latency.
        with patch.object(ready.time, 'monotonic', side_effect=lambda: monotonic_now[0]), \
                patch.object(ready, 'write_json', side_effect=slow_initial_record), \
                patch.object(ready, 'runtime_limit', side_effect=contextlib.nullcontext), \
                patch.object(self.bridge, 'pin_workspace') as pin:
            result = self.ask(max_seconds=0.03)
        pin.assert_not_called()
        self.assertEqual(result['state'], ready.TIMED_OUT)
        self.assertEqual(result['exit_reason'], 'WAIT_BUDGET_EXHAUSTED')
        self.assertEqual(result['ask_count'], 0)
        self.assertEqual(self.bridge.submitted, [])

    def test_elapsed_budget_accumulates_across_restarts_and_wall_clock_rollback(self):
        monotonic_now = [100.0]

        def observe(*args, **kwargs):
            monotonic_now[0] += 0.25
            return None

        with patch.object(ready.time, 'monotonic', side_effect=lambda: monotonic_now[0]), \
             patch.object(ready, 'find_reply', side_effect=observe):
            for index in range(4):
                self.clock.now -= 30
                result = self.run_loop(one_shot=True, max_seconds=1)
                if index == 0:
                    deadline = result['deadline_epoch']
                self.assertEqual(result['deadline_epoch'], deadline)
                self.assertAlmostEqual(result['elapsed_wait_seconds'], (index + 1) * 0.25)
                expected = ready.TIMED_OUT if index == 3 else ready.WAITING_DEPENDENCY
                self.assertEqual(result['state'], expected)
        self.assertEqual(result['exit_reason'], 'WAIT_BUDGET_EXHAUSTED')
        self.assertEqual(self.bridge.submitted, [])

    def test_blocked_bridge_receives_only_the_remaining_episode_budget(self):
        passive = self.run_loop(one_shot=True, max_seconds=4)
        passive['elapsed_wait_seconds'] = 3.96
        ready.write_json(ready.loop_path(self.caller), passive)
        budgets = []

        @contextlib.contextmanager
        def bounded(seconds):
            budgets.append(seconds)
            yield

        def blocked(*args, **kwargs):
            # The real signal interruption has its own test above. Here the
            # contract is the remaining budget, independent of CI filesystem
            # latency before the bridge is reached.
            raise ready.WaitBudgetExpired()

        with patch.object(ready, 'runtime_limit', side_effect=bounded), \
                patch.object(ready.time, 'monotonic', return_value=100.0), \
                patch.object(self.bridge, 'pin_workspace', side_effect=blocked) as pin:
            result = self.ask(max_seconds=4)
        self.assertEqual(len(budgets), 1)
        self.assertAlmostEqual(budgets[0], 0.04)
        pin.assert_called_once()
        self.assertEqual(result['state'], ready.TIMED_OUT)
        self.assertEqual(result['exit_reason'], 'WALL_CLOCK_BUDGET_EXHAUSTED')
        self.assertEqual(result['deadline_epoch'], passive['deadline_epoch'])
        self.assertEqual(self.bridge.submitted, [])

    def test_bridge_initialization_is_bounded_and_has_a_durable_timeout_record(self):
        with patch.object(ready, '_bridge', side_effect=lambda: time.sleep(2)):
            start = time.monotonic()
            result = ready.persist('surface:1', self.caller, task_id='task-x', max_seconds=0.03)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result['outcome'], ready.TIMED_OUT)
        self.assertEqual(result['reason'], 'IDLE_OPERATION_BUDGET_EXHAUSTED')
        saved = ready.read_json(result['record'])
        self.assertEqual(saved['caller_surface_uuid'], self.caller)
        self.assertEqual(saved['outcome'], ready.TIMED_OUT)
        self.assertEqual(self.bridge.submitted, [])

    def test_loop_liveness_requires_real_process_freshness_and_finite_deadline(self):
        now = time.time()
        base = dict(state=ready.WAITING_DEPENDENCY, monitoring=True, heartbeat_epoch=now,
                    started_epoch=now - 1, deadline_epoch=now + 59, poll_seconds=5,
                    pid=1, argv='fixture loop argv')
        with patch.object(ready, 'pid_runs_loop', return_value=True):
            self.assertTrue(ready.loop_live(base, now))
            for override in (dict(state=ready.ASKING), dict(monitoring=False),
                             dict(heartbeat_epoch=now - 600), dict(deadline_epoch=now - 1),
                             dict(deadline_epoch=now + 10000), dict(poll_seconds=0)):
                self.assertFalse(ready.loop_live(dict(base, **override), now))
        with patch.object(ready, 'process_command', return_value='python -B executor_ready.py persist'):
            self.assertTrue(ready.pid_runs_loop(1, 'python -B executor_ready.py persist'))
            self.assertFalse(ready.pid_runs_loop(1, 'python'))
            self.assertFalse(ready.pid_runs_loop(1, ''))

    def test_existing_live_legacy_loop_is_not_duplicated(self):
        ready.write_json(ready.loop_path(self.caller), dict(state=ready.ASKING, pid=999,
                                                          argv='legacy-loop'))
        with patch.object(ready, '_bridge', return_value=self.bridge), \
             patch.object(ready, 'pid_runs_loop', return_value=True):
            result = ready.persist('surface:1', self.caller, task_id='task-x', max_ticks=1)
        self.assertEqual(result['outcome'], 'ALREADY_RUNNING')
        self.assertEqual(self.bridge.submitted, [])
        self.assertEqual(result['stop_file'], str(ready.stop_path(self.caller)))

    def test_finished_finite_process_is_not_treated_as_a_live_legacy_loop(self):
        record = self.run_loop(one_shot=True)
        record.update(pid=999, argv='reused process command')
        ready.write_json(ready.loop_path(self.caller), record)
        with patch.object(ready, '_bridge', return_value=self.bridge), \
             patch.object(ready, 'pid_runs_loop', return_value=True):
            result = ready.persist('surface:1', self.caller, task_id='task-x', max_ticks=1)
        self.assertEqual(result['outcome'], ready.TIMED_OUT)
        self.assertEqual(self.bridge.submitted, [])

    def test_still_monitoring_finite_process_is_not_duplicated(self):
        record = self.run_loop(one_shot=True)
        record.update(pid=999, argv='fixture process command', monitoring=True)
        ready.write_json(ready.loop_path(self.caller), record)
        with patch.object(ready, '_bridge', return_value=self.bridge), \
             patch.object(ready, 'pid_runs_loop', return_value=True):
            result = ready.persist('surface:1', self.caller, task_id='task-x', max_ticks=1)
        self.assertEqual(result['outcome'], 'ALREADY_RUNNING')
        self.assertEqual(self.bridge.submitted, [])

    def test_cli_ask_request_reconcile_and_persist_are_compatible_and_bounded(self):
        for mode in ('ask', 'request'):
            with self.subTest(mode=mode), patch.object(ready, 'request', return_value={}) as request, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ready.main([mode, '--supervisor', 'surface:1']), 0)
                self.assertEqual(request.call_args.kwargs['max_seconds'], ready.DEFAULT_WAIT_SECONDS)
        with patch.object(ready, 'persist', return_value={}) as persist, \
             contextlib.redirect_stdout(io.StringIO()):
            ready.main(['persist', '--supervisor', 'surface:1'])
            self.assertEqual(persist.call_args.kwargs['max_seconds'], ready.DEFAULT_WAIT_SECONDS)
            self.assertNotIn('send_once', persist.call_args.kwargs)
        with patch.object(ready, '_bridge', return_value=self.bridge):
            record = self.ask()
            self.bridge.receipts.clear()
            self.bridge.native = False
            self.bridge.submitted.clear()
            result = ready.reconcile('surface:1', record['asks'][0]['marker'])
        self.assertEqual(result['outcome'], 'UNCONFIRMED_DO_NOT_RESEND')
        self.assertTrue(all(s['reconcile_only'] for s in self.bridge.submitted))

    def test_request_text_is_single_line_and_describes_no_automatic_repeat(self):
        text = ready.build_text('surface:2', 'EXECUTOR_READY_abcd1234', 'task',
                                'note\nTASK: x', 1, '/tmp/mailbox', self.caller)
        self.assertFalse(cmux_bridge._looks_like_task_dispatch(text))
        self.assertNotIn('\n', text)
        self.assertIn('will not be repeated automatically', text)
        self.assertIn('EXECUTOR_READY_abcd1234.json', text)
        self.assertIn(self.caller, text)


if __name__ == '__main__':
    unittest.main()
