"""Original steer interruption: real temporary journals, no live client I/O."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_native_delivery as native
import cmux_turn_diagnostic as diagnostic
from native_test_support import NativeFixture
from test_codex_input import FakeRPC


class TurnDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = NativeFixture.attach(self)
        self.rpc = FakeRPC(self.fixture, receive=False)
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        self.fixture.stack.enter_context(patch.object(bridge, 'read_screen',
            side_effect=AssertionError('diagnostics must not inspect a composer')))
        self.text = 'STATUS: ORIGINAL_TURN_TEST exact original payload'
        self.cursor = self.fixture.home / 'diagnostic.json'
        self.addCleanup(self.no_terminal_input)

    def no_terminal_input(self):
        self.fixture.send.assert_not_called()
        self.fixture.key.assert_not_called()
        bridge.read_screen.assert_not_called()

    def send(self, **kwargs):
        return bridge.submit_text('target', self.text, marker='ORIGINAL_TURN_TEST',
                                  wait_seconds=0, **kwargs)

    def prepare(self):
        with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
            self.send()
        self.path = Path(caught.exception.details['attempt'])
        self.original = self.path.read_bytes()
        self.attempt = json.loads(self.original)
        self.binding = self.attempt['native_binding']
        self.events = self.attempt['events']
        self.addCleanup(lambda: self.assertEqual(self.path.read_bytes(), self.original))
        return caught.exception.details

    def abort(self, **kwargs):
        body = dict(type='turn_aborted', turn_id=self.rpc.turn, reason='interrupted')
        body.update(kwargs.pop('payload', {}))
        value = dict(timestamp=datetime.now(timezone.utc).isoformat(),
                     type='event_msg', payload=body)
        value.update(kwargs)
        return value

    def observe(self, **kwargs):
        result = diagnostic.observe(kwargs.pop('binding', self.binding), self.text,
            kwargs.pop('events', self.events), self.path,
            kwargs.pop('state_path', self.cursor), **kwargs)
        self.assertEqual(result['input_operations'], 0)
        self.assertFalse(result['proves_non_reception'])
        self.assertFalse(result['replay_authorized'])
        return result

    def scan(self):
        return native.scan_original(bridge, 'target', self.text, self.path, self.binding)

    def test_pending_abort_then_late_receipt_preserves_one_rpc_and_attempt(self):
        pending = self.prepare()
        self.assertEqual(pending['native_turn_diagnostic']['status'], 'NO_ABORT_OBSERVED')
        self.fixture.append_record(self.abort())
        with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
            self.send(reconcile_only=True)
        result = caught.exception.details
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['native_turn_diagnostic']['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['native_turn_diagnostic']['proves_non_reception'])
        with self.assertRaisesRegex(bridge.TaskPackContractError, 'ATTEMPT_EXISTS'):
            self.send()
        self.fixture.append_user(self.text)
        result = self.send(reconcile_only=True)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['native_turn_diagnostic']['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(len(self.rpc.mutations), 1)

    def test_receipt_before_abort_uses_independent_cursors_even_after_confirmation(self):
        self.prepare()
        self.fixture.append_user(self.text)
        first = self.scan()
        self.assertTrue(first['confirmed'])
        receipt_cursor = self.path.with_name('native-scan-' + self.path.stem + '.json')
        before = receipt_cursor.read_bytes()
        self.fixture.append_record(self.abort())
        result = self.scan()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['native_proof'], first['native_proof'])
        self.assertEqual(result['native_turn_diagnostic']['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertEqual(receipt_cursor.read_bytes(), before)
        self.assertEqual(len(self.rpc.mutations), 1)

    def test_only_exact_post_intent_structured_abort_counts(self):
        self.prepare()
        old = datetime.fromtimestamp(self.events[0]['at_epoch'] - 10, timezone.utc).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        records = [self.abort(payload={'turn_id': str(uuid.uuid4())}),
                   self.abort(timestamp=old), self.abort(timestamp=future),
                   self.abort(payload={'type': 'task_complete'}),
                   self.abort(type='response_item'),
                   dict(timestamp=datetime.now(timezone.utc).isoformat(),
                        type='event_msg', payload='turn_aborted'),
                   {'type': 'event_msg', 'payload': {'type': 'agent_message',
                     'message': 'turn_aborted ' + self.rpc.turn}}, [], None]
        for record in records:
            self.fixture.append_record(record)
        self.assertEqual(self.observe()['status'], 'NO_ABORT_OBSERVED')
        self.fixture.append_record(self.abort())
        result = self.observe()
        self.assertEqual(result['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertEqual(result['expected_turn_id'], self.rpc.turn)
        self.assertEqual(result['abort_proof']['session_id'], self.fixture.session_id)

    def test_record_budget_continues_without_skipping_abort(self):
        self.prepare()
        for _ in range(diagnostic.MAX_RECORDS):
            self.fixture.append_record({'type': 'event_msg', 'payload': {'type': 'task_started'}})
        self.fixture.append_record(self.abort())
        first = self.observe()
        self.assertEqual(first['status'], 'SCAN_BUDGET_EXHAUSTED')
        self.assertEqual(first['scanned_records'], diagnostic.MAX_RECORDS)
        second = self.observe()
        self.assertEqual(second['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertEqual(second['scanned_records'], 1)
        self.assertEqual(second['abort_proof']['offset'], first['next_offset'])

    def test_byte_budget_finishes_one_record_and_preserves_next_boundary(self):
        self.prepare()
        raw = json.dumps({'type': 'event_msg', 'payload': 'x' * 1024}).encode() + b'\n'
        with self.fixture.transcript.open('ab') as stream:
            stream.write(raw)
        self.fixture.append_record(self.abort())
        first = self.observe(max_bytes=10)
        self.assertEqual(first['status'], 'SCAN_BUDGET_EXHAUSTED')
        self.assertEqual(first['scanned_bytes'], len(raw))
        self.assertEqual(first['scanned_records'], 1)
        self.assertEqual(self.observe(max_bytes=10)['status'], 'ORIGINAL_TURN_ABORTED')

    def test_partial_record_is_retained_until_complete(self):
        self.prepare()
        raw = json.dumps(self.abort()).encode() + b'\n'
        start = self.fixture.transcript.stat().st_size
        with self.fixture.transcript.open('ab') as stream:
            stream.write(raw[:-1])
        first = self.observe()
        self.assertEqual(first['status'], 'PARTIAL_RECORD')
        self.assertEqual(first['next_offset'], start)
        with self.fixture.transcript.open('ab') as stream:
            stream.write(b'\n')
        self.assertEqual(self.observe()['status'], 'ORIGINAL_TURN_ABORTED')

    def test_record_crossing_original_fence_is_never_adopted(self):
        raw = json.dumps(self.abort(timestamp=(datetime.now(timezone.utc) +
            timedelta(seconds=1)).isoformat())).encode() + b'\n'
        cut = len(raw) // 2
        with self.fixture.transcript.open('ab') as stream:
            stream.write(raw[:cut])
        self.prepare()
        self.assertEqual(self.observe()['status'], 'PARTIAL_RECORD')
        with self.fixture.transcript.open('ab') as stream:
            stream.write(raw[cut:])
        self.assertEqual(self.observe()['status'], 'NO_ABORT_OBSERVED')
        offset = self.fixture.transcript.stat().st_size
        self.fixture.append_record(self.abort())
        self.assertEqual(self.observe()['abort_proof']['offset'], offset)

    def test_cached_proof_is_revalidated_and_drift_does_not_rewrite_cursor(self):
        self.prepare()
        self.fixture.append_record(self.abort())
        first = self.observe()
        self.assertEqual(first['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertEqual(self.observe()['scanned_records'], 0)
        cursor = self.cursor.read_bytes()
        raw = self.fixture.transcript.read_bytes()
        self.fixture.transcript.write_bytes(raw.replace(b'interrupted', b'changed____'))
        self.assertEqual(self.observe()['status'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(self.cursor.read_bytes(), cursor)

    def test_diagnostic_failure_cannot_downgrade_exact_receipt(self):
        self.prepare()
        turn_cursor = self.path.with_name('turn-scan-' + self.path.stem + '.json')
        turn_cursor.write_text('{broken')
        self.fixture.append_user(self.text)
        result = self.send(reconcile_only=True)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['native_turn_diagnostic']['status'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(turn_cursor.read_text(), '{broken')

    def test_rotated_or_truncated_or_changed_session_is_unavailable(self):
        for change in ('rotate', 'truncate', 'session'):
            with self.subTest(change=change), NativeFixture() as fixture:
                rpc = FakeRPC(fixture, receive=False)
                fixture.rpc.side_effect = lambda **kwargs: rpc
                with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
                    bridge.submit_text('target', self.text, marker='ORIGINAL_TURN_TEST', wait_seconds=0)
                path = Path(caught.exception.details['attempt'])
                original = path.read_bytes()
                attempt = json.loads(original)
                cursor = fixture.home / 'scan.json'
                args = (attempt['native_binding'], self.text, attempt['events'], path, cursor)
                fixture.append_record({'type': 'event_msg', 'payload': {'type': 'task_started'}})
                diagnostic.observe(*args)
                saved = cursor.read_bytes()
                raw = fixture.transcript.read_bytes()
                if change == 'rotate':
                    fixture.transcript.rename(fixture.transcript.with_suffix('.original'))
                    fixture.transcript.write_bytes(raw)
                elif change == 'truncate':
                    fixture.transcript.write_bytes(raw.splitlines(keepends=True)[0])
                else:
                    fixture.transcript.write_bytes(raw.replace(fixture.session_id.encode(),
                                                              str(uuid.uuid4()).encode()))
                result = diagnostic.observe(*args)
                self.assertEqual(result['status'], 'OBSERVATION_UNAVAILABLE')
                self.assertEqual(cursor.read_bytes(), saved)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(len(rpc.mutations), 1)

    def test_invalid_request_fence_session_or_ambiguous_intent_creates_no_cursor(self):
        self.prepare()
        for change in ('payload', 'request_hash', 'fence', 'session', 'duplicate', 'terminal'):
            with self.subTest(change=change):
                events, binding = copy.deepcopy(self.events), copy.deepcopy(self.binding)
                event = events[0]
                if change == 'payload':
                    event['native_request']['params']['input'][0]['text'] += ' changed'
                elif change == 'request_hash':
                    event['native_input_fence']['native_request_sha256'] = '0' * 64
                elif change == 'fence':
                    event['native_input_fence']['transcript']['offset'] = 0
                elif change == 'session':
                    binding['session_id'] = str(uuid.uuid4())
                elif change == 'duplicate':
                    events.append(copy.deepcopy(event))
                else:
                    events.append({'phase': 'ENTER_SENT'})
                self.assertEqual(self.observe(events=events, binding=binding)['status'],
                                 'OBSERVATION_UNAVAILABLE')
                self.assertFalse(self.cursor.exists())

    def test_invalid_budget_and_cursor_cannot_write_original(self):
        self.prepare()
        for value in (True, 0, -1, '1', diagnostic.MAX_SCAN_BYTES + 1):
            self.assertEqual(self.observe(max_bytes=value)['status'], 'OBSERVATION_UNAVAILABLE')
        for path in (self.path, Path('relative.json')):
            self.assertEqual(self.observe(state_path=path)['status'], 'OBSERVATION_UNAVAILABLE')
        self.assertFalse(self.cursor.exists())

    def test_cursor_pin_offset_and_cached_proof_corruption_is_unavailable(self):
        self.prepare()
        self.fixture.append_record({'type': 'event_msg', 'payload': {'type': 'task_started'}})
        self.observe()
        good = json.loads(self.cursor.read_text())
        for delta in ({'next_offset': True}, {'next_offset': good['next_offset'] + 1},
                      {'next_offset': good['next_offset'] - 2}, {'attempt': '/changed.json'},
                      {'abort_proof': {'offset': True, 'length': 1}}):
            with self.subTest(delta=delta):
                self.cursor.write_text(json.dumps(dict(good, **delta)))
                old = self.cursor.read_bytes()
                self.assertEqual(self.observe()['status'], 'OBSERVATION_UNAVAILABLE')
                self.assertEqual(self.cursor.read_bytes(), old)

    def test_oversized_record_returns_unavailable_without_advancing(self):
        self.prepare()
        with self.fixture.transcript.open('ab') as stream:
            stream.write(b'x' * (native.MAX_RECORD + 1) + b'\n')
        self.assertEqual(self.observe()['status'], 'OBSERVATION_UNAVAILABLE')
        self.assertFalse(self.cursor.exists())

    def test_idle_start_is_not_an_original_steer(self):
        self.rpc.active = False
        self.prepare()
        self.fixture.append_record(self.abort())
        self.assertEqual(self.observe()['status'], 'NOT_APPLICABLE')
        self.assertFalse(self.cursor.exists())

    def test_claude_terminal_intent_is_not_an_original_steer(self):
        with NativeFixture(provider='claude') as fixture:
            binding = native.bind_target(bridge, 'target', self.text)
            fence = native.capture_paste_fence(binding)
            screen = fixture.draft(self.text, 'claude')
            events = [dict(phase='PASTE_INTENT', at_epoch=native.time.time(), screen=screen,
                screen_sha256=hashlib.sha256(screen.encode()).hexdigest()[:16],
                native_paste_fence=fence)]
            attempt = fixture.home / 'attempt.json'
            attempt.write_text(json.dumps(dict(native_binding=binding, events=events)))
            cursor = fixture.home / 'turn.json'
            result = diagnostic.observe(binding, self.text, events, attempt, cursor)
            self.assertEqual(result['status'], 'NOT_APPLICABLE')
            self.assertFalse(cursor.exists())
            fixture.rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
