"""Recovery and no-duplicate guarantees for current Codex retry layouts."""

import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
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
                self.assertEqual(state.kind, "error_superseded")

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
