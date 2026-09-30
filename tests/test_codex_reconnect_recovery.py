"""Recovery and no-duplicate guarantees for current Codex retry layouts."""

import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
from ccc_provider_retry import ProviderRetryStore
from tests.test_watch import (
    FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload,
    reconnect_payload, span, visible_lines,
)


def covered_prompt_payload(prefix="  "):
    payload = grid_payload([], error=HIGH_DEMAND_TEXT)
    grid = payload["render_grid"]
    row = grid["cursor"]["row"]
    grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] < row]
    grid["row_spans"].extend([
        span(row - 1, 0, "⠁       ⠈       ⢀", 4),
        span(row, 0, prefix, 4 if prefix.strip() else 1),
        span(row, 2, "Ask Codex to do anything", 2),
        span(row, 80, "⠈", 4),
        span(row + 2, 2, "gpt-6-astra xhigh · Context 0% used · Fast on", 0),
    ])
    return payload


class CodexReconnectRecoveryTests(unittest.TestCase):
    PEAK_LOAD = (
        "rate limit exceeded: The system is currently experiencing high demand "
        "and cannot process your request. Your request exceeds the maximum usage "
        "size allowed during peak load. For improved capacity reliability, "
        "consider switching to Provisioned Throughput."
    )

    def test_peak_load_banner_complete_and_wrapped(self):
        for prefix in ('■ ', '└ ', '■ rate limit exceeded: '):
            for width in (19, 53, 1000):
                banner = prefix + self.PEAK_LOAD
                wrapped = '\n'.join(banner[i:i+width] for i in range(0, len(banner), width))
                with self.subTest(prefix=prefix, width=width):
                    self.assertEqual(core._match_error_block(wrapped), 'rate_limit')
        for text in ('■ documentation: ' + self.PEAK_LOAD,
                     '■ example: ' + self.PEAK_LOAD,
                     '■ ' + self.PEAK_LOAD + ' This is a quoted example.',
                     '■ ' + self.PEAK_LOAD[:-20]):
            self.assertIsNone(core._match_error_block(text))

    def test_peak_load_waits_then_sends_once(self):
        payload = self.peak_load_payload()
        state = core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
        self.assertEqual((state.kind, state.error_type), ('recoverable_error', 'rate_limit'))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, '\n'.join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            now, _ = self.bind_provider(daemon, self.PEAK_LOAD, ready=False)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 0)
            now[0] += 15
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_terminal_rate_limit_dispatch_at_quarter_second_without_replay(self):
        errors = (self.PEAK_LOAD,
                  "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded token rate limit.")
        for error in errors:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                payload = grid_payload([], error=error, columns=500)
                client = FakeClient(payload, "\n".join(visible_lines(payload)))
                daemon = armed_daemon(directory, client)
                now, _ = self.bind_provider(daemon, error, ready=False)
                daemon.process_once(client)
                self.assertEqual(client.sent, [])
                now[0] = 200.249
                daemon.process_once(client)
                self.assertEqual(client.sent, [])
                now[0] = 200.25
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)
                for later in (200.5, 201.0, 260.0):
                    now[0] = later
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), 1)

    def test_peak_load_native_tips_preserve_recovery_and_dedup(self):
        for tip in ('└ Tip: Press ctrl+g to edit your current draft in an external editor.',
                    '└ Tip: Run /review to get a code review of your current changes.'):
            with self.subTest(tip=tip), tempfile.TemporaryDirectory() as directory:
                payload = self.peak_load_payload()
                row = payload['render_grid']['cursor']['row']
                payload['render_grid']['row_spans'].append(span(row - 3, 0, tip, 3))
                client = FakeClient(payload, '\n'.join(visible_lines(payload)))
                daemon = armed_daemon(directory, client)
                now, _ = self.bind_provider(daemon, self.PEAK_LOAD, ready=False)
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid')).error_type, 'rate_limit')
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 0)
                now[0] += 15
                daemon.process_once(client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)
        for suffix in ('└ Tip: arbitrary output.', '└ Tip: Run /review',
                       '└ Tip: Run /review to get a code review of your current changes. extra'):
            self.assertIsNone(core._match_error_block('■ ' + self.PEAK_LOAD + '\n' + suffix))

    def rotating_tip_payload(self, tip):
        payload = grid_payload([' '] * 20, columns=120)
        row = payload['render_grid']['cursor']['row']
        error = '└ ' + self.PEAK_LOAD
        card = ['• Reconnecting... 5/5(5m 20s • esc to interrupt)'] + [
            error[i:i+110] for i in range(0, len(error), 110)] + tip.split('\n')
        for index, text in enumerate(card):
            payload['render_grid']['row_spans'].append(
                span(row - len(card) - 2 + index, 0, text, 3))
        return payload

    def test_rotating_reconnect_tip_waits_and_recovers_once(self):
        for tip in ('└ Tip: Use /skills to list available skills or ask Codex to use one.',
                    '└ Tip: A future native suggestion.',
                    '└ Tip: Use /skills to list available skills\n  or ask Codex to use one.'):
            with self.subTest(tip=tip), tempfile.TemporaryDirectory() as directory:
                payload = self.rotating_tip_payload(tip)
                state = core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
                self.assertEqual((state.kind, state.error_type), ('recoverable_error', 'rate_limit'))
                client = FakeClient(payload, '\n'.join(visible_lines(payload)))
                daemon = armed_daemon(directory, client)
                now, _ = self.bind_provider(daemon, self.PEAK_LOAD, ready=False)
                daemon.process_once(client)
                self.assertEqual(client.sent, [])
                now[0] += 15
                daemon.process_once(client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)

    def test_rotating_tip_keeps_newer_output_and_input_guards(self):
        for expected, text in (('working', '• Working (4s • esc to interrupt)'),
                               ('composer_busy', 'unfinished draft'),
                               ('menu', 'Implement this plan?')):
            payload = self.rotating_tip_payload('└ Tip: Use /skills to list available skills.')
            row = payload['render_grid']['cursor']['row']
            payload['render_grid']['row_spans'].append(
                span(row, 30, text, 0) if expected == 'composer_busy'
                else span(row - 1, 0, text, 0))
            self.check_without_send(payload, expected)
        lines = ['• Reconnecting... 5/5(5s • esc to interrupt)',
                 '└ ' + self.PEAK_LOAD, '└ Tip: Use /skills.', '', '• New response']
        self.assertTrue(core._find_last_error(lines).superseded)
        self.assertIsNone(core._find_last_error([lines[0], '└ ' + self.PEAK_LOAD[:-20], lines[2]]))
        self.assertIsNone(core._find_last_error(['■ ' + self.PEAK_LOAD, lines[2]]))

    def test_peak_load_does_not_override_working_or_draft(self):
        for expected, text in (('working', '• Working (4s • esc to interrupt)'),
                               ('composer_busy', 'unfinished draft'),
                               ('menu', 'Implement this plan?')):
            payload = self.peak_load_payload()
            row = payload['render_grid']['cursor']['row']
            payload['render_grid']['row_spans'].append(
                span(row, 30, text, 0) if expected == 'composer_busy'
                else span(row - 2, 0, text, 0))
            self.check_without_send(payload, expected)

    def peak_load_payload(self):
        payload = grid_payload([' '] * 20, columns=82)
        row = payload['render_grid']['cursor']['row']
        text = '■ ' + self.PEAK_LOAD
        chunks = [text[i:i+80] for i in range(0, len(text), 80)]
        for index, chunk in enumerate(chunks):
            payload['render_grid']['row_spans'].append(
                span(row - len(chunks) - 3 + index, 0 if index == 0 else 2,
                     chunk, 3, len(chunk)))
        return payload

    def bind_provider(self, daemon, error='HTTP 429 Too Many Requests', *, ready=True):
        now = [1000.0 if ready else 200.0]
        turn = {'kind': 'task_complete', 'session_id': 'original', 'turn_id': 'failed',
                'at': 200.0, 'model_provider': 'synthetic-provider', 'error': {'message': error}}
        daemon.codex_queue_recovery.current_turn = lambda _: dict(turn)
        daemon._provider_retry = ProviderRetryStore(daemon._provider_retry.path,
            clock=lambda: now[0], jitter=lambda: 0)
        daemon._provider_retry.observe('original', 'synthetic-provider', 'failed',
            core._match_error_block(error), error, 200.0)
        if ready:
            now[0] += 15
        self.addCleanup(daemon._process_snapshots.close)
        return now, turn

    def test_provider_status_precedes_stream_wrapper(self):
        cases = {
            'HTTP 400 Bad Request': 'http_400',
            'HTTP 502 Bad Gateway': 'http_502',
            'HTTP 503 Unavailable': 'http_503',
            'HTTP 504 Gateway Timeout': 'http_504',
            'unexpected status 401 Unauthorized': 'http_401',
            'unexpected status 401: insufficient_quota': 'token_exhausted',
            'unexpected status 403 Forbidden': 'http_403',
            'HTTP 429 Too Many Requests': 'rate_limit',
            'HTTP 500 Internal Server Error': 'http_500',
            'unexpected status 524 A timeout occurred': 'http_524',
            'unknown status code: 524': 'http_524',
            'HTTP 429 {"error":{"code":"insufficient_quota"}}': 'token_exhausted',
            'HTTP 200 data: {"error":{"code":"rate_limit_exceeded"}}': 'rate_limit',
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertEqual(core._match_error_block(
                    'stream disconnected before completion: ' + error), expected)
        self.assertIsNone(core._match_error_block('documentation: rate_limit_exceeded'))

    def test_success_resolves_original_provider_before_refunding_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(covered_prompt_payload()))
            now, turn = self.bind_provider(daemon)
            store = daemon._provider_retry
            evidence = store.observe('original', 'synthetic-provider', 'failed',
                                     'rate_limit', 'HTTP 429 Too Many Requests', 200.0)
            for index in range(4):
                now[0] += 200
                evidence = store.observe('original', 'synthetic-provider', f'failed-{index}',
                                         'rate_limit', 'HTTP 429 Too Many Requests', 200.0 + index)
                self.assertTrue(store.reserve(evidence, str(index)))
            now[0] += 200
            self.assertFalse(store.ready(evidence))
            turn.update(turn_id='answered', at=300.0, error=None, last_agent_message='Actual answer')
            turn.pop('model_provider')
            target = {'surface_id': 'surface-uuid', 'workspace_id': 'workspace-uuid'}
            with mock.patch('ccc_codex_goal.provider_for_turn', return_value=None):
                daemon._provider_retry_success(target)
            self.assertFalse(store.ready(evidence))
            with mock.patch('ccc_codex_goal.provider_for_turn', return_value='synthetic-provider') as lookup:
                daemon._provider_retry_success(target)
                lookup.assert_called_once_with(target, turn)
            self.assertIsNone(store.observe('original', 'synthetic-provider', 'failed',
                                           'rate_limit', 'HTTP 429 Too Many Requests', 200.0))
            fresh = store.observe('original', 'synthetic-provider', 'new-failure',
                                  'rate_limit', 'HTTP 429 Too Many Requests', 301.0)
            now[0] += 15
            # A later completed answer permits the next original failure.
            self.assertTrue(store.reserve(fresh, 'after-success'))

    def test_provider_auth_and_permission_do_not_send(self):
        for status in (400, 401, 403):
            payload = grid_payload([], error=f'unexpected status {status} Unknown error')
            self.check_without_send(payload, 'provider_blocked')

    def test_provider_budget_waits_and_stops_after_four_remedies_across_turns(self):
        for error in ('HTTP 500 Internal Server Error',
                      'HTTP 524 A timeout occurred'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                payload = grid_payload([], error=error)
                client = FakeClient(payload, '\n'.join(visible_lines(payload)))
                daemon = armed_daemon(directory, client)
                now, turn = self.bind_provider(daemon, error, ready=False)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 0)
                runtime = daemon.runtime['surface-uuid']
                for i in range(4):
                    now[0] += 200
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), i + 1)
                    runtime.awaiting, runtime.last_send_at = False, 0
                    turn.update(turn_id=f'failed-{i}', at=201.0 + i)
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), i + 1)
                now[0] += 200
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 4)
                self.assertIn('shared retry slot' if '429' in error else 'four-remedy budget', runtime.paused_reason)

    def test_typographic_apostrophe_recovers_but_preserves_input_guards(self):
        error = HIGH_DEMAND_TEXT.replace("'", "’")
        for options, expected in (({}, 'recoverable_error'), ({'composer': 'busy'}, 'composer_busy'),
                                  ({'menu': True}, 'menu'), ({'working': True}, 'working')):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                payload = grid_payload([], error=error, **options)
                text = '\n'.join(visible_lines(payload))
                client = FakeClient(payload, text)
                daemon = armed_daemon(directory, client)
                self.addCleanup(daemon._process_snapshots.close)
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid')).kind, expected)
                daemon.process_once(client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1 if expected == 'recoverable_error' else 0)

    def test_native_rate_limit_prefix_added_to_provider_prefix_is_recoverable(self):
        banner = ('rate limit exceeded: rate limit exceeded: Your requests to gpt-6-astra '
                  'for gpt-6-astra in eastus2 have exceeded rate limit.')
        payload = grid_payload([], error=banner, columns=160)
        state = core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
        self.assertEqual((state.kind, state.error_type), ('recoverable_error', 'rate_limit'))
        self.assertEqual(core.classify_text_prefilter(visible_lines(payload)).kind, 'candidate')
        for text in ('documentation: ' + banner, banner + ' This is an example.',
                     'rate limit exceeded: ' + banner):
            self.assertIsNone(core._match_error_block('■ ' + text))
        for options, expected in (({'composer': 'busy'}, 'composer_busy'), ({'menu': True}, 'menu'),
                                  ({'working': True}, 'working')):
            self.check_without_send(grid_payload([], error=banner, columns=160, **options), expected)

    def check_without_send(self, payload, expected):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            for _ in range(3):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].state, expected)

    def test_timer_request_changes_and_restart_preserve_repeat_floor(self):
        payload = reconnect_payload()
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            with mock.patch.object(core.time, "time", return_value=1000) as clock:
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)
                first_fingerprint = daemon.runtime["surface-uuid"].sent_fingerprint
                for offset in (0.4, 0.8):
                    clock.return_value = 1000 + offset
                    payload = reconnect_payload("5/5", elapsed=f"{int(offset * 10)}s")
                    for row in payload["render_grid"]["row_spans"]:
                        if row["text"].startswith("└"):
                            row["text"] += f" Request id: fixture-{offset}"
                            row["cell_width"] = len(row["text"])
                    client.payload = payload
                    client.text = "\n".join(visible_lines(payload))
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), 1)
                latest = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertNotEqual(first_fingerprint, latest.fingerprint)
                daemon.save()
                restarted = core.WatchDaemon(daemon.config_path, daemon.state_path, client=client)
                restarted.process_once(client)
                self.assertEqual(len(client.sent), 1)
                clock.return_value = 1001.1
                restarted.process_once(client)
                self.assertEqual(len(client.sent), 2)

    def test_working_revisit_cannot_exceed_configured_poll_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(reconnect_payload(), "\n".join(visible_lines(reconnect_payload())))
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime()
            runtime.observed_state = "working"
            runtime.observed_at = 100.0
            daemon.config["healthy_revisit_sec"] = 2.0  # Legacy setting cannot add a second second.
            self.assertFalse(daemon._observation_due(runtime, 100.9))
            self.assertTrue(daemon._observation_due(runtime, 101.0))
            runtime.observed_state = "recoverable_error"
            self.assertTrue(daemon._observation_due(runtime, 101.0))

    def test_real_working_below_reconnect_header_still_blocks(self):
        payload = reconnect_payload()
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(row - 3, 0, "Working (4s • esc to interrupt)")
        )
        self.check_without_send(payload, "working")

    def test_real_working_below_reconnect_with_background_terminal_still_blocks(self):
        payload = reconnect_payload()
        row = payload["render_grid"]["cursor"]["row"]
        for item in payload["render_grid"]["row_spans"]:
            if item["text"].startswith("• Reconnecting"):
                item["text"] += " · 1 background terminal running"
                item["cell_width"] = len(item["text"])
        payload["render_grid"]["row_spans"].append(
            span(row - 3, 0, "Working (4s • esc to interrupt)")
        )
        self.check_without_send(payload, "working")
        payload["render_grid"]["row_spans"][-1] = span(
            row - 3, 0, "• Working (4s • esc to interrupt) · 1 background terminal running"
        )
        self.check_without_send(payload, "working")

    def test_wrapped_reconnect_header_and_error_are_recognized(self):
        payload = reconnect_payload()
        row = payload["render_grid"]["cursor"]["row"]
        spans = payload["render_grid"]["row_spans"]
        spans[:] = [s for s in spans if s["row"] not in (row - 5, row - 4)]
        spans.extend([
            span(row - 6, 0, "• Reconnecting... 5/5 (1m 24s •"),
            span(row - 5, 2, "esc to interrupt)"),
            span(row - 4, 0, "  └ We're currently experiencing high demand, which may cause", 3),
            span(row - 3, 2, "temporary errors.", 3),
        ])
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
        self.assertTrue(state.allow_repeat)
        self.assertEqual(core.classify_text_prefilter(visible_lines(payload)).kind, "candidate")

    def test_unicode_reconnect_ellipsis_uses_the_same_repeat_floor(self):
        payload = reconnect_payload()
        for row in payload["render_grid"]["row_spans"]:
            row["text"] = row["text"].replace("Reconnecting...", "Reconnecting…")
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertTrue(state.allow_repeat)

    def test_wrapped_queue_header_and_lone_pending_prompt_block(self):
        for queued in (
            ["• Messages to be", "  submitted after next tool call", "  ↳ another task"],
            ["  ↳ 任务请继续"],
        ):
            with self.subTest(queued=queued):
                payload = reconnect_payload()
                row = payload["render_grid"]["cursor"]["row"]
                payload["render_grid"]["row_spans"].extend(
                    span(row - len(queued) + i, 0, text)
                    for i, text in enumerate(queued)
                )
                self.check_without_send(payload, "queued_followup")

    def test_prompt_echo_does_not_supersede_live_high_demand(self):
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            with mock.patch.object(core.time, "time", return_value=1000) as clock:
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)
                row = payload["render_grid"]["cursor"]["row"]
                payload["render_grid"]["row_spans"].append(span(row - 2, 0, "› 任务请继续"))
                client.text = "\n".join(visible_lines(payload))
                clock.return_value = 1002
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 2)

    def test_live_d365940f_echo_and_sparse_spinner_stay_current(self):
        payload = grid_payload([" "] * 20, columns=100)
        composer_row = payload["render_grid"]["cursor"]["row"]
        banner = "■ " + HIGH_DEMAND_TEXT
        payload["render_grid"]["row_spans"].extend([
            span(composer_row - 10, 0, banner, 3, len(banner)),
            span(composer_row - 7, 0, "› 请继续任务", 0, 12),
            span(composer_row - 4, 0, banner, 3, len(banner)),
            span(composer_row - 2, 0, "› 任务请继续", 0, 12),
            span(composer_row - 1, 0, "    ⠈                    ⢀                       ⠐       ⠐", 0),
        ])
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_typed_placeholder_and_braille_at_cursor_home_are_protected(self):
        for text in ("Ask Codex to do anything", "⠁⠃⠉", "⠁ my task"):
            with self.subTest(text=text):
                payload = reconnect_payload()
                row = payload["render_grid"]["cursor"]["row"]
                spans = payload["render_grid"]["row_spans"]
                spans[:] = [s for s in spans if not (s["row"] == row and s["column"] >= 2)]
                spans.append(span(row, 2, text, 0))
                self.check_without_send(payload, "composer_busy")

    def test_rgb_overlay_on_dim_placeholder_keeps_composer_empty(self):
        payload = reconnect_payload()
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(span(row, 80, "⠈", 4))
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")

    def test_rgb_overlay_can_cover_prompt_with_blank_or_braille(self):
        for prefix in ("  ", "⡀ "):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                payload = covered_prompt_payload(prefix)
                client = FakeClient(payload, "\n".join(visible_lines(payload)))
                daemon = armed_daemon(directory, client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)

    def test_high_demand_hard_wrap_at_every_character_stays_current(self):
        for split, banner in ((split, '■ ' + text) for text in
                              (HIGH_DEMAND_TEXT, HIGH_DEMAND_TEXT.replace("'", "’"))
                              for split in range(3, len('■ ' + text))):
            with self.subTest(split=split, banner=banner):
                payload = covered_prompt_payload("⡀ ")
                grid = payload["render_grid"]
                row = grid["cursor"]["row"]
                grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] != row - 4]
                grid["row_spans"].extend([
                    span(row - 4, 0, banner[:split], 3),
                    span(row - 3, 0, banner[split:], 3),
                ])
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))
                self.assertEqual(core.classify_text_prefilter(visible_lines(payload)).kind, "candidate")

    def test_new_transient_http_error_supersedes_older_high_demand(self):
        for status, phrase in ((408, "Request Timeout"), (429, "Too Many Requests"),
                              (500, "Internal Server Error"), (502, "Bad Gateway"),
                              (503, "Service Unavailable"), (504, "Gateway Timeout")):
            with self.subTest(status=status):
                payload = covered_prompt_payload()
                row = payload["render_grid"]["cursor"]["row"]
                banner = f"■ unexpected status {status} {phrase}: Unknown error, url: https://example.test/v1/responses"
                payload["render_grid"]["row_spans"].extend([
                    span(row - 3, 0, banner[:38], 3),
                    span(row - 2, 0, banner[38:], 3),
                ])
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                expected = "rate_limit" if status == 429 else f"http_{status}"
                self.assertEqual((state.kind, state.error_type), ("recoverable_error", expected))

    def test_non_retryable_http_status_stays_blocked(self):
        for status in (400, 401, 403, 404, 409, 501):
            with self.subTest(status=status):
                payload = covered_prompt_payload()
                row = payload["render_grid"]["cursor"]["row"]
                payload["render_grid"]["row_spans"].append(
                    span(row - 2, 0, f"■ unexpected status {status} Unknown error", 3))
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual(state.kind, "provider_blocked" if status in (400, 401, 403) else "error_superseded")

    def test_old_working_above_new_error_waits_for_native_completion_then_recovers(self):
        payload = covered_prompt_payload()
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(span(row - 6, 0, "• Working (0s • esc to interrupt)"))
        self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind, "recoverable_error")
        self.assertEqual(core.classify_text_prefilter(visible_lines(payload)).kind, "candidate")
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            turn = {"kind": "task_started", "session_id": "original", "turn_id": "turn", "at": 900}
            daemon.codex_queue_recovery.current_turn = lambda _: dict(turn)
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            turn.update(kind="task_complete", error={"message": HIGH_DEMAND_TEXT})
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            runtime = daemon.runtime["surface-uuid"]
            runtime.last_send_at -= core.REPEAT_SEND_DELAY_SEC + 1
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            turn.update(turn_id='new-failed-turn', at=901)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 2)
            self.assertEqual(client.sent[-1][-1], core.MESSAGE)

    def test_covered_prompt_requires_native_placeholder_animation_and_footer(self):
        for missing in ("placeholder", "dim", "animation", "rgb", "footer", "model", "cursor", "home", "prefix"):
            with self.subTest(missing=missing):
                payload = covered_prompt_payload()
                grid = payload["render_grid"]
                row = grid["cursor"]["row"]
                for item in grid["row_spans"]:
                    if item["row"] == row and item["column"] == 2:
                        if missing == "placeholder":
                            item["text"] = "Ask Codex"
                        if missing == "dim":
                            item["style_id"] = 0
                    if item["row"] == row - 1:
                        if missing == "animation":
                            item["text"] = ""
                        if missing == "rgb":
                            item["style_id"] = 0
                    if item["row"] == row + 2:
                        if missing == "footer":
                            item["text"] = "gpt-6-astra xhigh"
                        if missing == "model":
                            item["text"] = "Context 0% used"
                    if item["row"] == row and item["column"] == 0 and missing == "prefix":
                        item["text"] = "x "
                if missing == "cursor":
                    grid["cursor"]["visible"] = False
                if missing == "home":
                    grid["cursor"]["column"] = 3
                self.check_without_send(payload, "incompatible")

    def test_covered_prompt_preserves_user_input_menu_working_and_queue_gates(self):
        for expected, text in (
            ("composer_busy", "my unfinished task"),
            ("menu", "Implement this plan?"),
            ("working", "• Working (4s • esc to interrupt)"),
            ("queued_followup", "• Queued follow-up inputs"),
        ):
            with self.subTest(expected=expected):
                payload = covered_prompt_payload()
                row = payload["render_grid"]["cursor"]["row"]
                payload["render_grid"]["row_spans"].append(
                    span(row, 30, text, 0) if expected == "composer_busy"
                    else span(row - 2, 0, text, 0)
                )
                self.check_without_send(payload, expected)

    def test_unverified_braille_transcript_is_not_ignored(self):
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(span(row - 1, 0, "⠁⠃⠉", 0))
        self.check_without_send(payload, "error_superseded")

    def test_unknown_new_error_never_replays_older_high_demand(self):
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(span(row - 2, 0, "■ invalid codex request", 3))
        self.check_without_send(payload, "error_superseded")

    def test_reconnect_background_terminal_separator_and_truncation_still_send(self):
        nested = (
            "└ Rate limit exceeded: Your requests to gpt-6-astra "
            "for gpt-6-astra in eastus2"
        )
        headers = (
            "• Reconnecting... 2/5 (16m 17s • esc to interrupt) • 1 background terminal running",
            "• Reconnecting... 2/5 (16m 17s • esc to interrupt) · 1 background terminal",
            "• Reconnecting... 2/5 (16m 17s • esc to interrupt) · 1 background terminal runnin…",
        )
        for header in headers:
            with self.subTest(header=header):
                payload = grid_payload([" "] * 20, columns=120)
                row = payload["render_grid"]["cursor"]["row"]
                payload["render_grid"]["row_spans"].extend([
                    span(row - 5, 0, header, 0, len(header)),
                    span(row - 4, 0, nested, 3, len(nested)),
                    span(row - 3, 2, "have exceeded rate limit.", 3),
                ])
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual((state.kind, state.error_type), ("recoverable_error", "rate_limit"))
                self.assertFalse(core._working_present(core.Grid.from_rpc(payload, "surface-uuid").lines))
                with tempfile.TemporaryDirectory() as directory:
                    client = FakeClient(payload, "\n".join(visible_lines(payload)))
                    daemon = armed_daemon(directory, client)
                    self.bind_provider(daemon)
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), 1)
                    self.assertEqual(client.sent[0][-1], core.MESSAGE)

    def test_reconnect_background_terminal_nested_provider_rate_limit_sends(self):
        # Live FD25C084: reconnect header failed fullmatch because of
        # ``· 1 background terminal running``, so Working won; nested
        # ``└ Rate limit exceeded: Your requests to … have exceeded rate limit.``
        # then never became a recoverable error.
        payload = grid_payload([" "] * 20, columns=82)
        row = payload["render_grid"]["cursor"]["row"]
        rest = (
            "Reconnecting... 2/5 (16m 17s • esc to interrupt) "
            "· 1 background terminal runnin…"
        )
        nested = (
            "└ Rate limit exceeded: Your requests to gpt-6-astra "
            "for gpt-6-astra in eastus2"
        )
        payload["render_grid"]["row_spans"].extend([
            span(row - 5, 0, "•", 0, 1),
            span(row - 5, 2, rest, 0, len(rest)),
            span(row - 4, 0, nested, 3, len(nested)),
            span(row - 3, 2, "have exceeded rate limit.", 3),
        ])
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "rate_limit"))
        self.assertTrue(state.allow_repeat)
        self.assertFalse(core._working_present(core.Grid.from_rpc(payload, "surface-uuid").lines))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            self.bind_provider(daemon)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0][-1], core.MESSAGE)

    def test_wrapped_reconnect_suffix_is_not_working(self):
        payload = grid_payload([" "] * 20, columns=82)
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].extend([
            span(row - 6, 0, "• Reconnecting... 2/5 (16m 17s •", 0),
            span(row - 5, 2, "esc to interrupt) · 1 background terminal running · /ps to v…", 0),
            span(row - 4, 0, "  └ Rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2", 3),
            span(row - 3, 2, "have exceeded rate limit.", 3),
        ])
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "rate_limit"))
        self.assertFalse(core._working_present(core.Grid.from_rpc(payload, "surface-uuid").lines))
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            self.bind_provider(daemon)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0][-1], core.MESSAGE)

    def test_working_with_background_terminal_suffix_still_blocks(self):
        payload = grid_payload([" "] * 20, columns=82)
        row = payload["render_grid"]["cursor"]["row"]
        working = "(33m 40s • esc to interrupt) · 1 background terminal running · /ps to v…"
        payload["render_grid"]["row_spans"].extend([
            span(row - 5, 0, "■ rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2", 3),
            span(row - 4, 2, "have exceeded rate limit.", 3),
            span(row - 2, 0, "•", 0, 1),
            span(row - 2, 2, "Working", 0, 7),
            span(row - 2, 10, working, 0, len(working)),
        ])
        self.check_without_send(payload, "working")

    def test_nested_reconnect_errors_keep_their_types(self):
        cases = (
            (
                "Rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit.",
                "rate_limit",
            ),
            (HIGH_DEMAND_TEXT, "high_demand"),
            ("stream disconnected before completion", "stream"),
            ("exceeded retry limit, last status: 429 Too Many Requests", "rate_limit"),
            ("unexpected status 503 Service Unavailable", "http_503"),
            ("unexpected status 405 Method Not Allowed url: https://zzzcoding.org/v1/responses", "http_405"),
            ("400 invalid_parameter: prompt_cache_retention", "prompt_cache"),
        )
        header = "• Reconnecting... 2/5 (16m 17s • esc to interrupt) · 1 background terminal running"
        for detail, error_type in cases:
            with self.subTest(error_type=error_type, detail=detail[:40]):
                payload = grid_payload([" "] * 20, columns=120)
                row = payload["render_grid"]["cursor"]["row"]
                payload["render_grid"]["row_spans"].extend([
                    span(row - 5, 0, header, 0, len(header)),
                    span(row - 4, 0, "  └ " + detail, 3, len("  └ " + detail)),
                ])
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual((state.kind, state.error_type), ("recoverable_error", error_type))

    def test_provider_rate_limit_block_matcher_rejects_quoted_prose(self):
        nested = (
            "• Reconnecting... 2/5 (16m 17s • esc to interrupt) · 1 background terminal running\n"
            "└ Rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2\n"
            "have exceeded rate limit."
        )
        self.assertEqual(core._match_error_block(nested), "rate_limit")
        banner = (
            "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 "
            "have exceeded rate limit."
        )
        self.assertIsNone(core._match_error_block("■ documentation: " + banner))
        self.assertIsNone(core._match_error_block("■ example: " + banner))
        self.assertIsNone(core._match_error_block("■ " + banner + " This is a quoted example."))
        self.assertIsNone(core._match_error_block("■ documentation: see\n└ " + banner))
        self.assertIsNone(core._match_error_block("■ documentation: see ■ " + banner))
        self.assertIsNone(core._match_error_block("example: see\n└ " + banner))


if __name__ == "__main__":
    unittest.main()
