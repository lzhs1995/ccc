import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ccc_codex_queue import QueueRecovery, completed_error, _retryable_completed_message


EXHAUSTED_429 = ('exceeded retry limit, last status: 429 Too Many Requests, '
                 'request id: req_fixture_429')
TERMINAL_502 = ('unexpected status 502 Bad Gateway: Unknown error, '
                'url: http://127.0.0.1:18765/v1/responses')


class QueueRecoveryTests(unittest.TestCase):
    def test_terminal_502_banner_is_retryable_including_hard_wraps(self):
        for message in (TERMINAL_502, '■ ' + TERMINAL_502, TERMINAL_502.upper(),
                        TERMINAL_502.replace('Gateway', 'Gate\nway'),
                        TERMINAL_502.replace('127.0.0.1', '127.0.\n0.1')):
            with self.subTest(message=message):
                self.assertTrue(_retryable_completed_message(message))

    def test_terminal_502_quotes_truncation_and_provider_blockers_do_not_retry(self):
        messages = [prefix + TERMINAL_502 for prefix in (
            'Example: ', 'Documentation: ', 'Quoted: ', 'Sample: ',
            'User: ', 'Assistant: ', 'Please explain ', '> ', '```\n', '"')]
        messages += [TERMINAL_502[:-4], TERMINAL_502.replace('502', '5020', 1),
                     TERMINAL_502 + ' is an example.', 'HTTP 502',
                     TERMINAL_502 + ' {"error":{"code":"insufficient_quota"}}',
                     TERMINAL_502 + ' unexpected status 401 Unauthorized',
                     TERMINAL_502 + ' unexpected status 403 Forbidden']
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(_retryable_completed_message(message))

    def test_exhausted_429_banner_is_retryable_with_native_request_id(self):
        for message in (EXHAUSTED_429, EXHAUSTED_429.upper(),
                        EXHAUSTED_429.split(', request id:')[0], '■ ' + EXHAUSTED_429,
                        EXHAUSTED_429.replace('Too Many Requests', 'Too Many\nRequests'),
                        EXHAUSTED_429.replace('Requests', 'Reque\nsts')):
            with self.subTest(message=message):
                self.assertTrue(_retryable_completed_message(message))

    def test_exhausted_429_does_not_override_specific_provider_blocker(self):
        for detail in (
                '{"error":{"code":"insufficient_quota"}}',
                '{"error":{"code":"invalid_encrypted_content"}}',
                '{"error":{"type":"invalid_request_error","message":"Invalid request"}}',
                '{"error":{"code":"resource_mismatch"}}',
                'unexpected status 401 Unauthorized',
                'unknown status code 403',
                'HTTP 400 Bad Request'):
            with self.subTest(detail=detail):
                self.assertFalse(_retryable_completed_message(EXHAUSTED_429 + ': ' + detail))

    def test_exhausted_429_quote_user_text_and_other_status_are_not_retry_evidence(self):
        messages = [prefix + EXHAUSTED_429 for prefix in (
            'Documentation: ', 'Example: ', 'Quoted: ', 'Sample: ',
            '■ Example: ', '⚠ Quoted: ', '└ Sample: ', 'User: ', 'Assistant: ',
            'Please explain ', '> ', '```\n', '"', "'")]
        messages += [EXHAUSTED_429.replace('429', status, 1)
                     for status in ('400', '401', '403', '4290')]
        messages += ['HTTP 429 Too Many Requests', 'last status: 429 Too Many Requests']
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(_retryable_completed_message(message))

    def test_terminal_provider_blockers_override_stream_wrapper(self):
        for detail in (
                'unexpected status 401 Unauthorized',
                'unknown status code 403',
                'HTTP 429 {"error":{"code":"insufficient_quota"}}',
                'unexpected status 400 {"error":{"code":"invalid_encrypted_content"}}'):
            with self.subTest(detail=detail):
                self.assertFalse(_retryable_completed_message(
                    'stream disconnected before completion: ' + detail))
        self.assertTrue(_retryable_completed_message(
            'stream disconnected before completion: unexpected status 429 Too Many Requests'))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "queue.json"
        self.recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        self.evidence = {"session_id": "original", "pid": 123, "process_start": 1,
                         "completed_at": 100, "signature": [1, 2, 3]}
        self.recovery.evidence = Mock(return_value=self.evidence)
        self.target = {"surface_id": "s", "workspace_id": "w", "enabled": True}
        self.runtime = SimpleNamespace(delivery_status="confirmed", send_count=1)
        self.empty = {"empty": True, "busy": False, "editable": True, "queued": ["任务请继续"]}
        self.draft = {"empty": False, "busy": False, "queued": [], "draft": "任务请继续"}
        self.edit, self.enter = Mock(), Mock()

    def run_recovery(self, views=None, authorized=None):
        with patch("ccc_codex_queue.time.sleep"):
            return self.recovery.recover(self.target, self.runtime,
                read_view=Mock(side_effect=views or [self.empty, self.draft]),
                edit_queued=self.edit, enter=self.enter,
                authorized=authorized or Mock(return_value=True))

    def test_reuses_one_message_and_persists_before_each_key(self):
        phases = []
        self.edit.side_effect = lambda: phases.append(next(iter(json.loads(self.ledger.read_text()).values()))["phase"])
        self.enter.side_effect = lambda: phases.append(next(iter(json.loads(self.ledger.read_text()).values()))["phase"])
        self.assertEqual(self.run_recovery(), "queue_recovery_submitted")
        self.assertEqual(phases, ["editing", "submitting"])
        restarted = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        restarted.evidence = Mock(return_value=self.evidence)
        self.recovery = restarted
        self.assertEqual(self.run_recovery(), "")
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.enter.call_count, 1)

    def test_busy_draft_foreign_and_multiple_queued_messages_are_protected(self):
        for view in [{**self.empty, "busy": True}, {**self.empty, "empty": False},
                     {**self.empty, "editable": False}, {**self.empty, "queued": ["user instruction"]},
                     {**self.empty, "queued": ["任务请继续", "任务请继续"]}]:
            with self.subTest(view=view):
                self.recovery.next_probe.clear()
                self.assertEqual(self.run_recovery([view]), "")
                self.edit.assert_not_called()

    def test_unknown_delivery_cannot_be_overridden(self):
        self.runtime.delivery_status = "unknown"
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_unproven_original_session_cannot_be_recovered(self):
        self.recovery.evidence.return_value = None
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_revoked_authorization_before_edit(self):
        self.assertEqual(self.run_recovery(authorized=Mock(return_value=False)), "")
        self.edit.assert_not_called()

    def test_revoked_authorization_after_edit_preserves_the_draft(self):
        self.assertEqual(self.run_recovery(authorized=Mock(side_effect=[True, False])), "queue_recovery_unconfirmed")
        self.edit.assert_called_once()
        self.enter.assert_not_called()

    def test_changed_process_or_turn_before_edit(self):
        self.recovery.evidence.side_effect = [self.evidence, {**self.evidence, "pid": 456}]
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_changed_draft_blocks_enter(self):
        self.assertEqual(self.run_recovery([self.empty, {**self.draft, "draft": "user draft"}]), "queue_recovery_unconfirmed")
        self.enter.assert_not_called()

    def test_lost_edit_acknowledgement_is_never_retried(self):
        self.edit.side_effect = TimeoutError()
        self.assertEqual(self.run_recovery(), "queue_recovery_unconfirmed")
        self.recovery.next_probe.clear()
        self.assertEqual(self.run_recovery(), "queue_recovery_unconfirmed")
        self.assertEqual(self.edit.call_count, 1)
        self.enter.assert_not_called()

    def test_lost_enter_acknowledgement_is_never_retried(self):
        self.enter.side_effect = TimeoutError()
        self.assertEqual(self.run_recovery(), "queue_recovery_unconfirmed")
        self.recovery.next_probe.clear()
        self.run_recovery()
        self.assertEqual(self.enter.call_count, 1)

    def test_corrupt_ledger_cannot_be_reset(self):
        self.ledger.write_text("{broken")
        self.recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_transcript_requires_latest_turn_completed_with_retryable_error(self):
        now = time.time()
        path = self.root / "session.jsonl"
        meta = {"type": "session_meta", "payload": {"id": "original"}}
        finished = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                    "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old",
                    "error": {"message": "We're currently experiencing high demand, which may cause temporary errors."}}}
        path.write_text("\n".join(json.dumps(x) for x in [meta, finished]) + "\n")
        self.assertIsNotNone(completed_error(path, "original", now))
        self.assertIsNone(completed_error(path, "replacement", now))
        with path.open("a") as h:
            h.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        self.assertIsNone(completed_error(path, "original", now))

    def test_transport_failure_eligibility_is_shared_by_queue_and_transcript_checks(self):
        now = time.time()
        path = self.root / "session.jsonl"
        recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        for message, allowed in (("Connection failed: error sending request", True),
                                 ("stream disconnected before completion", True),
                                 (EXHAUSTED_429, True),
                                 (TERMINAL_502, True),
                                 ('Example: ' + TERMINAL_502, False),
                                 ('Example: ' + EXHAUSTED_429, False),
                                 (EXHAUSTED_429 + ' {"error":{"code":"insufficient_quota"}}', False),
                                 ("Connection failed: permission denied", False),
                                 ("Example: Connection failed: error sending request", False)):
            with self.subTest(message=message):
                turn = {"kind": "task_complete", "at": now - 10, "turn_id": "original-turn",
                        "error": {"message": message}, "session_id": "original", "pid": 123,
                        "process_start": 1, "signature": [1, 2, 3]}
                recovery.current_turn = Mock(return_value=turn)
                finished = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                            "type": "event_msg", "payload": {"type": "task_complete", "error": turn["error"]}}
                path.write_text("\n".join(json.dumps(x) for x in [
                    {"type": "session_meta", "payload": {"id": "original"}}, finished]) + "\n")
                self.assertEqual(recovery.evidence(self.target) is not None, allowed)
                self.assertEqual(completed_error(path, "original", now) is not None, allowed)

    def test_user_message_supersedes_exhausted_429_completion(self):
        now = time.time()
        path = self.root / 'session.jsonl'
        def event(kind, message):
            return {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 10)),
                    'type': 'event_msg', 'payload': {'type': kind, 'turn_id': 'original-turn',
                                                   'error': {'message': message}}}
        path.write_text('\n'.join(json.dumps(x) for x in [
            {'type': 'session_meta', 'payload': {'id': 'original'}},
            event('task_complete', EXHAUSTED_429), event('user_message', EXHAUSTED_429)]) + '\n')
        self.assertIsNone(completed_error(path, 'original', now))
        recovery = QueueRecovery(self.ledger, self.root / 'bindings', self.root, '任务请继续')
        recovery.current_turn = Mock(return_value={
            'kind': 'user_message', 'at': now - 10, 'error': {'message': EXHAUSTED_429}})
        self.assertIsNone(recovery.evidence(self.target))

    def test_new_input_and_unfinished_turn_supersede_terminal_502(self):
        now = time.time()
        path = self.root / 'session.jsonl'
        for kind in ('user_message', 'task_started'):
            with self.subTest(kind=kind):
                records = [
                    {'type': 'session_meta', 'payload': {'id': 'original'}},
                    {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 10)),
                     'type': 'event_msg', 'payload': {'type': 'task_complete',
                        'turn_id': 'failed', 'error': {'message': TERMINAL_502}}},
                ]
                path.write_text('\n'.join(json.dumps(x) for x in records) + '\n')
                self.assertIsNotNone(completed_error(path, 'original', now))
                records.append({'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 5)),
                                'type': 'event_msg', 'payload': {'type': kind, 'turn_id': 'new'}})
                path.write_text('\n'.join(json.dumps(x) for x in records) + '\n')
                self.assertIsNone(completed_error(path, 'original', now))


if __name__ == "__main__":
    unittest.main()
