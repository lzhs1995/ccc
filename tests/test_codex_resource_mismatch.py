"""Native provider envelopes must agree across viewport, queue, and health.

Regression fixtures include the resource-ownership failure seen on surface5986.
No test sends terminal input or reads an operator's real session/ledger.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ccc_observation as health
import cmux_codex_watch as core
from ccc_codex_queue import QueueRecovery, completed_error, _retryable_completed_message
from ccc_provider_retry import ProviderRetryStore, structured_error_type
from tests.test_watch import grid_payload, span


RESOURCE = ('{"error":{"type":"invalid_request_error","code":null,"message":'
            '"The requested item was created under a different OpenAI resource. '
            'Please use the same resource to access this item."}}')
MASKED_RESOURCE = json.dumps({'error': {
    'code': None, 'param': None, 'type': 'invalid_request_error',
    'message': ('agentrouter-B: OpenAI Responses bad request: The requested item was created under a '
                'different *** OpenAI resource. Use the same resource that created the item to access it. '
                '[trace_id=6f2f8ce70b9b3b1a248cb599693747dc]'),
}})
STREAM = "stream disconnected before completion: "
HIGH_DEMAND = "We're currently experiencing high demand, which may cause temporary errors: "
RATE = '{"error":{"message":"Request limit reached"}}'
SERVER = '{"error":{"type":"server_error","message":"Internal server error"}}'
PROMPT_CACHE = ('{"error":{"type":"invalid_request_error","param":"prompt_cache_retention",'
                '"code":null,"message":"bad response status code 400"}}')
DEPLOYMENT = ('Could not find an existing deployment to match the model in the request. '
              'Please verify the model matches an existing deployment in the account.')
BALANCE = 'unexpected status 403 Forbidden: 用户额度不足, 剩余额度: ¥-0.067956 (request id: fixture)'


def screen(message, *, wrapped=False, composer="placeholder"):
    if not wrapped:
        payload = grid_payload([], error=message, composer=composer, columns=600)
    else:
        line = "■ " + message
        chunks = [line[i:i + 74] for i in range(0, len(line), 74)]
        payload = grid_payload([""] * (len(chunks) + 4), composer=composer, columns=74)
        payload["render_grid"]["row_spans"].extend(
            span(i, 0, chunk, 3) for i, chunk in enumerate(chunks))
    return core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))


class StructuredProviderEnvelopeTests(unittest.TestCase):
    def assert_envelope(self, message, expected, kind):
        with self.subTest(layer="native"):
            self.assertEqual(structured_error_type(message), expected)
        with self.subTest(layer="error_block"):
            self.assertEqual(core._match_error_block("■ " + message), expected)
        for wrapped in (False, True):
            with self.subTest(layer="grid", hard_wrap=wrapped):
                state = screen(message, wrapped=wrapped)
                self.assertEqual((state.kind, state.error_type), (kind, expected))

    def test_resource_without_http_status_overrides_generic_retry_wrappers(self):
        for prefix in ("", STREAM, HIGH_DEMAND):
            with self.subTest(prefix=prefix):
                self.assert_envelope(prefix + RESOURCE, "resource_mismatch", "recoverable_error")
                self.assertTrue(_retryable_completed_message(prefix + RESOURCE))

    def test_masked_resource_under_http_400_agrees_across_native_queue_and_viewport(self):
        for body in (RESOURCE, MASKED_RESOURCE):
            for prefix in ('', STREAM, HIGH_DEMAND, 'unexpected status 400 Bad Request: ',
                           'HTTP/1.1 400 Bad Request: ', 'unknown status code 400: '):
                with self.subTest(body=body, prefix=prefix):
                    message = prefix + body
                    self.assert_envelope(message, 'resource_mismatch', 'recoverable_error')
                    self.assertTrue(_retryable_completed_message(message))
                    self.assertTrue(_retryable_completed_message('\n'.join(
                        message[i:i + 74] for i in range(0, len(message), 74))))

    def test_resource_http_400_does_not_override_authentication_or_quoted_errors(self):
        message = 'unexpected status 400 Bad Request: ' + MASKED_RESOURCE
        for extra in (' unexpected status 401 Unauthorized', ' HTTP 403 Forbidden'):
            self.assertFalse(_retryable_completed_message(message + extra))
        for prefix in ('Documentation: ', 'Example: ', 'Quoted: ', 'Sample: '):
            self.assertIsNone(structured_error_type(prefix + message))
            self.assertFalse(_retryable_completed_message(prefix + message))
        self.assertEqual(screen(message, composer='busy').kind, 'composer_busy')

    def test_plaintext_deployment_and_balance_block_every_recovery_layer(self):
        for body, expected in ((DEPLOYMENT, 'deployment_not_found'), (BALANCE, 'billing_exhausted')):
            for prefix in ('', STREAM, HIGH_DEMAND):
                with self.subTest(cause=expected, prefix=prefix):
                    message = prefix + body
                    self.assert_envelope(message, expected, 'provider_blocked')
                    self.assertFalse(_retryable_completed_message(message))
                    self.assertFalse(_retryable_completed_message('\n'.join(
                        message[i:i + 31] for i in range(0, len(message), 31))))

    def test_structured_balance_and_deployment_codes_keep_the_concrete_blocker(self):
        for code, expected in (('DeploymentNotFound', 'deployment_not_found'),
                               ('model_not_found', 'deployment_not_found'),
                               ('insufficient_balance', 'billing_exhausted')):
            body = json.dumps({'error': {'code': code, 'type': 'invalid_request_error', 'message': 'Rejected'}})
            self.assert_envelope(STREAM + body, expected, 'provider_blocked')

    def test_specific_plaintext_failures_in_docs_history_or_drafts_do_not_grant_input(self):
        for body in (DEPLOYMENT, BALANCE):
            self.assertIsNone(structured_error_type('Documentation: ' + body))
            self.assertIsNone(structured_error_type('■ Example: ' + STREAM + body))
            self.assertFalse(_retryable_completed_message('Example: ' + STREAM + body))
            self.assertEqual(screen(body, composer='busy').kind, 'composer_busy')
            history = grid_payload([])
            history['render_grid']['scrollback_spans'] = [span(0, 0, '■ ' + body, 3)]
            self.assertNotEqual(core.classify_grid(core.Grid.from_rpc(history, 'surface-uuid')).kind,
                                'provider_blocked')

    def test_http_status_with_reason_phrase_survives_wrapping(self):
        for status, body, expected in (("429 Too Many Requests", RATE, "rate_limit"),
                                       ("500 Internal Server Error", SERVER, "http_500")):
            for banner in ("unexpected status " + status + ": ", "HTTP " + status[:3] + ": "):
                with self.subTest(banner=banner):
                    self.assert_envelope(banner + body, expected, "recoverable_error")
                    # Queue recovery already requires a transport wrapper for
                    # message-only HTTP envelopes; do not expand that policy.
                    self.assertTrue(_retryable_completed_message(STREAM + banner + body))

    def test_specific_retry_code_beats_generic_invalid_request_type(self):
        body = ('{"error":{"type":"invalid_request_error","code":"rate_limit_exceeded",'
                '"message":"Rate limit reached"}}')
        for prefix in ("", "unexpected status 429 Too Many Requests: "):
            with self.subTest(prefix=prefix):
                self.assert_envelope(prefix + body, "rate_limit", "recoverable_error")
                self.assertTrue(_retryable_completed_message(STREAM + prefix + body))

    def test_prompt_cache_exception_with_null_and_canonical_code(self):
        for body in (PROMPT_CACHE, PROMPT_CACHE.replace('"code":null', '"code":"invalid_parameter"')):
            with self.subTest(body=body):
                self.assert_envelope(body, "prompt_cache", "recoverable_error")
                self.assertTrue(_retryable_completed_message(STREAM + body))

    def test_specific_blockers_beat_high_demand_and_http_status(self):
        cases = (
            (HIGH_DEMAND + '{"error":{"type":"invalid_request_error","message":"Invalid request"}}',
             "invalid_request", "provider_blocked"),
            ('HTTP 429: {"error":{"type":"insufficient_quota","message":"Quota exceeded"}}',
             "token_exhausted", "token_exhausted"),
            ('HTTP 500: {"error":{"code":"invalid_encrypted_content"}}',
             "invalid_encrypted_content", "provider_blocked"),
            ('{"error":{"type":"mystery","message":"Unrecognized failure"}}',
             "provider_error_unknown", "provider_blocked"),
        )
        for body, expected, kind in cases:
            with self.subTest(expected=expected):
                self.assert_envelope(body, expected, kind)
                self.assertFalse(_retryable_completed_message(STREAM + body))

    def test_resource_keeps_the_stalled_goal_visible_for_ordinary_retry(self):
        payload = grid_payload([], error=STREAM + RESOURCE, columns=600)
        payload["render_grid"]["row_spans"].append(
            span(payload["render_grid"]["cursor"]["row"] + 1, 0, "Goal stalled (/goal resume)", 0))
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "resource_mismatch"))
        # resource 不再是阻塞卡，stalled goal 因此不被掩盖；
        # 负对照见 test_plaintext_deployment_and_balance_block_every_recovery_layer。
        self.assertTrue(state.native_goal_stalled)

    def test_documentation_scrollback_and_superseded_errors_are_not_current_failures(self):
        documentation = grid_payload(["Documentation: " + RESOURCE], columns=600)
        history = grid_payload([])
        history["render_grid"]["scrollback_spans"] = [span(0, 0, "■ " + RESOURCE, 3)]
        superseded = grid_payload([], error=STREAM + RESOURCE, columns=600)
        row = superseded["render_grid"]["cursor"]["row"]
        superseded["render_grid"]["row_spans"].extend([
            span(row - 3, 0, "› 任务请继续", 0), span(row - 2, 0, "• 已继续检查", 0)])
        for name, payload in (("documentation", documentation), ("history", history), ("superseded", superseded)):
            with self.subTest(name=name):
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertNotIn(state.kind, {"provider_blocked", "recoverable_error", "token_exhausted"})
        self.assertIsNone(structured_error_type("Documentation: " + RESOURCE))
        self.assertFalse(_retryable_completed_message("Example: " + STREAM + RESOURCE))

    def test_user_draft_blocks_even_retryable_provider_error(self):
        for body in (RESOURCE, STREAM + RATE, STREAM + PROMPT_CACHE):
            with self.subTest(body=body):
                state = screen(body, composer="busy")
                self.assertEqual(state.kind, "composer_busy")
                self.assertNotIn(state.kind, core.SEND_ELIGIBLE_STATES)


class NativeQueueEnvelopeTests(unittest.TestCase):
    def test_queue_view_blocks_only_current_provider_error_cards(self):
        high_demand = "We're currently experiencing high demand, which may cause temporary errors."
        invalid_request = ('{"error":{"type":"invalid_request_error","code":null,"message":'
                           '"An unsupported request parameter was supplied."}}')
        target = {"surface_id": "surface-uuid", "workspace_id": "workspace", "enabled": True}
        # A retryable native snapshot must not override a currently visible
        # provider blocker; this exercises the independent viewport gate.
        turn = {"session_id": "s", "model_provider": "p", "turn_id": "latest", "kind": "task_complete",
                "at": 1250, "error": {"message": high_demand}}
        evidence = {"session_id": "s", "pid": 123, "process_start": 1,
                    "turn_id": "latest", "completed_at": 1250}
        # resource_mismatch 现按授权可续跑，不再是阻塞卡；其余三种仍阻塞。
        for message, blocks in ((RESOURCE, False), (invalid_request, True),
                                (STREAM + DEPLOYMENT, True), (BALANCE, True)):
            for columns in (600, 74):
                for replacement in ("none", "user", "high_demand", "continue_echo"):
                    with self.subTest(message=message, columns=columns, replacement=replacement):
                        marked = "■ " + message
                        content = [(marked[i:i + columns], 3) for i in range(0, len(marked), columns)]
                        if replacement == "user":
                            content.append(("› 请检查另一项任务", 0))
                        elif replacement == "continue_echo":
                            content.append(("› 任务请继续", 0))
                        elif replacement == "high_demand":
                            marked = "■ " + high_demand
                            content.extend((marked[i:i + columns], 3)
                                           for i in range(0, len(marked), columns))
                        payload = grid_payload([""] * (len(content) + 4), columns=columns)
                        rows = payload["render_grid"]["row_spans"]
                        rows.extend(span(i, 0, text, style) for i, (text, style) in enumerate(content))
                        row = payload["render_grid"]["cursor"]["row"]
                        rows.extend([span(row - 3, 0, "• Queued follow-up inputs", 0),
                                     span(row - 2, 0, "↳ 任务请继续", 0),
                                     span(row - 1, 0, "alt+up to edit last queued message", 0)])
                        views = []

                        def capture_view(*args, read_view, **kwargs):
                            views.append(read_view())
                            return ""

                        daemon = core.WatchDaemon.__new__(core.WatchDaemon)
                        daemon.logger = Mock()
                        daemon.codex_queue_recovery = SimpleNamespace(
                            message="任务请继续", current_turn=Mock(return_value=turn),
                            evidence=Mock(return_value=evidence), recover=Mock(side_effect=capture_view))
                        daemon._current_blocked_goal = Mock(return_value=None)
                        runtime = core.TargetRuntime()
                        runtime.delivery_status, runtime.send_count = "confirmed", 4
                        client = Mock()
                        client.replay.return_value = payload
                        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                        self.assertEqual(state.kind, "queued_followup")
                        daemon._recover_stranded_codex_queue(
                            target, runtime, state, client, lambda **kwargs: True)
                        self.assertEqual(len(views), 1)
                        self.assertTrue(views[0]["empty"])
                        self.assertTrue(views[0]["editable"])
                        self.assertEqual(views[0]["queued"], ["任务请继续"])
                        self.assertEqual([str(item) for item in client.mock_calls],
                                         ["call.replay('workspace', 'surface-uuid')"])
                        self.assertEqual(
                            views[0]["busy"],
                            blocks and replacement in {"none", "continue_echo"},
                            "current provider errors block; CCC echoes are not newer work")

    def test_queue_view_ignores_documented_or_superseded_resource_errors(self):
        high_demand = "We're currently experiencing high demand, which may cause temporary errors."
        target = {"surface_id": "surface-uuid", "workspace_id": "workspace", "enabled": True}
        turn = {"session_id": "s", "model_provider": "p", "turn_id": "latest", "kind": "task_complete",
                "at": 1250, "error": {"message": high_demand}}
        evidence = {"session_id": "s", "pid": 123, "process_start": 1,
                    "turn_id": "latest", "completed_at": 1250}
        for previous in ("documentation", "superseded_error", "neutral_control"):
            with self.subTest(previous=previous):
                payload = grid_payload(["Earlier task output"], error=high_demand, columns=600)
                rows = payload["render_grid"]["row_spans"]
                row = payload["render_grid"]["cursor"]["row"]
                if previous == "documentation":
                    rows.append(span(1, 0, "Documentation: " + RESOURCE, 0))
                elif previous == "superseded_error":
                    rows.extend([span(1, 0, "■ " + RESOURCE, 3), span(2, 0, "› 任务请继续", 0)])
                else:
                    rows.append(span(1, 0, "Documentation: ordinary API example", 0))
                rows.extend([span(row - 3, 0, "• Queued follow-up inputs", 0),
                             span(row - 2, 0, "↳ 任务请继续", 0),
                             span(row - 1, 0, "alt+up to edit last queued message", 0)])
                views = []

                def capture_view(*args, read_view, **kwargs):
                    views.append(read_view())
                    return ""

                daemon = core.WatchDaemon.__new__(core.WatchDaemon)
                daemon.logger = Mock()
                daemon.codex_queue_recovery = SimpleNamespace(
                    message="任务请继续", current_turn=Mock(return_value=turn),
                    evidence=Mock(return_value=evidence), recover=Mock(side_effect=capture_view))
                daemon._current_blocked_goal = Mock(return_value=None)
                runtime = core.TargetRuntime()
                runtime.delivery_status, runtime.send_count = "confirmed", 4
                client = Mock()
                client.replay.return_value = payload
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual(state.kind, "queued_followup")
                daemon._recover_stranded_codex_queue(target, runtime, state, client, lambda **kwargs: True)
                self.assertEqual(len(views), 1)
                self.assertTrue(views[0]["empty"])
                self.assertTrue(views[0]["editable"])
                self.assertEqual(views[0]["queued"], ["任务请继续"])
                self.assertFalse(views[0]["busy"], "only the current error card may block queue recovery")
                self.assertEqual([str(item) for item in client.mock_calls],
                                 ["call.replay('workspace', 'surface-uuid')"])

    def test_real_transcript_and_queue_evidence_share_terminal_error_eligibility(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            recovery = QueueRecovery(root / "queue.json", root / "bindings", root, "任务请继续")
            cases = ((STREAM + RESOURCE, True), (HIGH_DEMAND + RESOURCE, True),
                     ('unexpected status 400 Bad Request: ' + MASKED_RESOURCE, True),
                     ('unexpected status 400 Bad Request: ' + MASKED_RESOURCE + ' HTTP 401 Unauthorized', False),
                     (STREAM + DEPLOYMENT, False), (STREAM + BALANCE, False),
                     (STREAM + "unexpected status 429 Too Many Requests: " + RATE, True),
                     (STREAM + "unexpected status 500 Internal Server Error: " + SERVER, True),
                     (STREAM + PROMPT_CACHE, True))
            meta = {"type": "session_meta", "payload": {"id": "original"}}
            for body, allowed in cases:
                with self.subTest(body=body):
                    turn = {"session_id": "original", "pid": 123, "process_start": 1,
                            "signature": [1, 2, 3], "turn_id": "turn", "kind": "task_complete",
                            "at": now - 10, "error": {"message": body}}
                    recovery.current_turn = Mock(return_value=turn)
                    finished = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                                "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn",
                                "error": turn["error"]}}
                    path.write_text("\n".join(json.dumps(x) for x in (meta, finished)) + "\n")
                    self.assertEqual(completed_error(path, "original", now) is not None, allowed)
                    self.assertEqual(recovery.evidence({"surface_id": "surface", "workspace_id": "workspace"})
                                     is not None, allowed)
                    # A new native turn invalidates old failures, even if the
                    # screen still shows their red text.
                    with path.open("a") as handle:
                        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
                    self.assertIsNone(completed_error(path, "original", now))


class NativeProviderBlockGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ProviderRetryStore(Path(temporary.name) / "provider.sqlite3", clock=lambda: 1100, jitter=lambda: 0)
        evidence = self.store.observe("s", "p", "t", "http_500", "HTTP 500", 1000)
        self.store.clock = lambda: 1200
        self.assertTrue(self.store.reserve(evidence, "original-unknown-attempt"))
        self.reservation_before = self.reservation()
        self.daemon = core.WatchDaemon.__new__(core.WatchDaemon)
        self.daemon.logger = Mock()
        self.daemon._provider_retry = self.store
        self.turn = {"session_id": "s", "model_provider": "p", "turn_id": "resource-failure",
                     "kind": "task_complete", "at": 1250, "error": {"message": STREAM + RESOURCE}}
        self.daemon.codex_queue_recovery = SimpleNamespace(
            message="任务请继续", current_turn=Mock(return_value=self.turn),
            evidence=Mock(return_value=None), recover=Mock())
        self.daemon._current_blocked_goal = Mock(return_value=None)
        self.client = Mock()
        self.target = {"surface_id": "surface", "workspace_id": "workspace", "enabled": True}
        self.runtime = core.TargetRuntime()
        self.runtime.delivery_status = "unknown"
        self.runtime.send_count = 4
        self.runtime.send_attempt_id = "original-unknown-attempt"
        self.runtime.viewport_checked_at = 1300
        self.queued = core.ScreenState("queued_followup", reason="Codex already holds queued follow-up input")
        for name in ("_notify", "_notify_async"):
            mocked = patch.object(core.WatchDaemon, name)
            mocked.start()
            self.addCleanup(mocked.stop)

    def assert_preserved(self):
        self.assertEqual(self.runtime.delivery_status, "unknown")
        self.assertEqual(self.runtime.send_count, 4)
        self.assertEqual(self.runtime.send_attempt_id, "original-unknown-attempt")
        self.assertEqual(self.reservation(), self.reservation_before)
        self.daemon.codex_queue_recovery.recover.assert_not_called()
        self.assertEqual(self.client.mock_calls, [])

    def reservation(self):
        """只取预留身份字段。retryable 原因会合法新增观测与 waiter 行，
        整文件字节相等因此不再是「未消费原 attempt」的判据。"""
        with sqlite3.connect(self.store.path) as connection:
            rows = list(connection.execute("select identity, record from episodes"))
        keys = ("attempt", "reserved_stamp", "last_reserved_at")
        return sorted((identity, tuple(json.loads(record).get(k) for k in keys))
                      for identity, record in rows)

    def assert_retryable_health(self):
        snapshot = self.runtime.to_dict()
        unknown = health.continuation_row(self.target, snapshot, now=1301)
        self.assertEqual(unknown["status"], "delivery_unknown")
        # 原因可续跑时健康面不得报 blocked，也不得报 ok。
        snapshot["delivery_status"] = "confirmed"
        row = health.continuation_row(self.target, snapshot, now=1301)
        self.assertEqual((row["status"], row["reason_code"]),
                         ("unknown", "task_progress_unconfirmed"))
        report = health.continuation_report([self.target], {"surface": snapshot}, now=1301)
        self.assertEqual(report["counts"]["blocked"], 0)

    def assert_waiting_health(self):
        snapshot = self.runtime.to_dict()
        snapshot["delivery_status"] = "confirmed"
        row = health.continuation_row(self.target, snapshot, now=1301)
        self.assertEqual((row["status"], row["reason_code"]), ("waiting", "followup_queued"))
        report = health.continuation_report([self.target], {"surface": snapshot}, now=1301)
        self.assertEqual(report["counts"]["blocked"], 0)

    def assert_blocked_health(self, expected='resource_mismatch'):
        snapshot = self.runtime.to_dict()
        unknown = health.continuation_row(self.target, snapshot, now=1301)
        self.assertEqual(unknown["status"], "delivery_unknown")
        # Delivery uncertainty keeps precedence. With an independently known
        # receipt the same provider cause must become visible, not green/waiting.
        snapshot["delivery_status"] = "confirmed"
        row = health.continuation_row(self.target, snapshot, now=1301)
        self.assertEqual((row["status"], row["reason_code"]), ("blocked", expected))
        report = health.continuation_report([self.target], {"surface": snapshot}, now=1301)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["counts"]["blocked"], 1)

    def test_ordinary_native_gate_blocks_without_consuming_unknown_attempt(self):
        self.assertFalse(self.daemon._provider_retry_gate(
            self.target, self.runtime, self.queued, attempt="must-not-consume"))
        self.assertEqual((self.runtime.observed_state, self.runtime.observed_error_type),
                         ("", None))
        self.assert_preserved()
        self.assert_retryable_health()

    def test_plaintext_native_blockers_survive_queued_state_and_keep_unknown_delivery(self):
        for message, expected in ((STREAM + DEPLOYMENT, 'deployment_not_found'),
                                   (BALANCE, 'billing_exhausted')):
            with self.subTest(expected=expected):
                self.turn['error'] = {'message': message}
                result = self.daemon._recover_stranded_codex_queue(
                    self.target, self.runtime, self.queued, self.client, lambda **kwargs: True)
                self.assert_preserved()
                self.assertEqual((result.kind, result.error_type), ('provider_blocked', expected))
                self.daemon._record_state('surface', self.runtime, result)
                self.assert_blocked_health(expected)

    def test_unknown_native_turn_uses_blocked_goal_without_consuming_unknown_attempt(self):
        self.daemon.codex_queue_recovery.current_turn.return_value = {"kind": "unknown"}
        self.daemon._current_blocked_goal.return_value = {
            **self.turn, "kind": "goal_blocked", "goal_id": "goal", "error": {"message": RESOURCE}}
        self.assertFalse(self.daemon._provider_retry_gate(
            self.target, self.runtime, self.queued, attempt="must-not-consume"))
        self.assertEqual((self.runtime.observed_state, self.runtime.observed_error_type),
                         ("", None))
        self.assert_preserved()
        self.assert_retryable_health()

    def test_queued_native_blocker_survives_callers_state_recording(self):
        result = self.daemon._recover_stranded_codex_queue(
            self.target, self.runtime, self.queued, self.client, lambda **kwargs: True)
        self.assert_preserved()
        self.assertEqual((result.kind, result.error_type), ("queued_followup", None))
        self.daemon._record_state("surface", self.runtime, result)
        self.assertEqual((self.runtime.observed_state, self.runtime.observed_error_type),
                         ("queued_followup", None))
        self.assert_waiting_health()

    def test_queued_goal_blocker_survives_callers_state_recording(self):
        self.daemon.codex_queue_recovery.current_turn.return_value = {"kind": "unknown"}
        self.daemon._current_blocked_goal.return_value = {
            **self.turn, "kind": "goal_blocked", "goal_id": "goal", "error": {"message": RESOURCE}}
        result = self.daemon._recover_stranded_codex_queue(
            self.target, self.runtime, self.queued, self.client, lambda **kwargs: True)
        self.assert_preserved()
        self.assertEqual((result.kind, result.error_type), ("queued_followup", None))
        self.daemon._record_state("surface", self.runtime, result)
        self.assert_waiting_health()


if __name__ == "__main__":
    unittest.main()
