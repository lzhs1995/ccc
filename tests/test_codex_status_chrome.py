import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import FakeClient, armed_daemon, span


FIXTURE = Path(__file__).parent / "fixtures" / "codex-429-background-terminal.json"
ERRORS = {
    "rate_limit": "exceeded retry limit, last status: 429 Too Many Requests",
    "high_demand": "We're currently experiencing high demand, which may cause temporary errors.",
    "stream": "stream disconnected before completion",
    "http_503": "unexpected status 503 Service Unavailable",
    "http_405": "unexpected status 405 Method Not Allowed",
    "prompt_cache": "400 invalid_parameter: prompt_cache_retention",
}
PROVIDER_RATE_LIMIT = (
    "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 "
    "have exceeded rate limit."
)


def captured_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def status_payload(error="rate_limit", columns=126, count=1, word_wrap=False):
    payload = captured_payload()
    grid = payload["render_grid"]
    grid["columns"] = columns
    noun = "terminal" if count == 1 else "terminals"
    status = f"  {count} background {noun} running · /ps to view · /stop to close"
    error_text = "■ " + ERRORS.get(error, error)
    if word_wrap:
        status_lines = textwrap.wrap(status, columns, drop_whitespace=True)
    else:
        status_lines = [status[i:i + columns] for i in range(0, len(status), columns)]
    error_lines = textwrap.wrap(error_text, columns)
    grid["row_spans"] = [
        span(43 + i, 0, line, 3) for i, line in enumerate(error_lines)
    ] + [
        span(52 - len(status_lines) + i, 0, line, 2)
        for i, line in enumerate(status_lines)
    ] + [
        span(54, 0, "›", 1), span(54, 1, " "),
        span(54, 2, "Ask Codex to do anything", 2),
        span(56, 0, "gpt-6-astra max", 2),
    ]
    return payload


def visible_text(payload):
    return "\n".join(core.Grid.from_rpc(payload, "surface-uuid").lines)


class CodexProviderRateLimitTests(unittest.TestCase):
    def test_provider_banner_survives_word_and_cell_wrapping(self):
        for columns in (40, 58, 80, 106, 126, 160):
            for hard_wrap in (False, True):
                with self.subTest(columns=columns, hard_wrap=hard_wrap):
                    payload = status_payload(PROVIDER_RATE_LIMIT, columns=columns)
                    if hard_wrap:
                        text = "■ " + PROVIDER_RATE_LIMIT
                        grid = payload["render_grid"]
                        grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] >= 49]
                        grid["row_spans"] += [
                            span(43 + i // columns, 0, text[i:i + columns], 3)
                            for i in range(0, len(text), columns)
                        ]
                    state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                    self.assertEqual((state.kind, state.error_type), ("recoverable_error", "rate_limit"))
                    self.assertEqual(core.classify_text_prefilter(visible_text(payload)).kind, "candidate")

    def test_reported_two_line_banner_allows_changed_tail_style(self):
        payload = status_payload(PROVIDER_RATE_LIMIT)
        grid = payload["render_grid"]
        grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] >= 49]
        grid["row_spans"] += [
            span(43, 0, "■ rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2", 3),
            span(44, 2, "have exceeded rate limit.", 0),
        ]
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "rate_limit"))

    def test_rate_limit_mentions_and_partial_banners_are_not_recoverable(self):
        for text in (
            "rate limit exceeded",
            "documentation: " + PROVIDER_RATE_LIMIT,
            "example: " + PROVIDER_RATE_LIMIT,
            PROVIDER_RATE_LIMIT.replace("have exceeded rate limit.", "may exceed rate limit."),
            PROVIDER_RATE_LIMIT + " This is a quoted example.",
        ):
            with self.subTest(text=text):
                payload = status_payload(text)
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind, "idle")
        for variant in ("no_marker", "indented_marker", "blank_gap", "different_unwrapped_output"):
            with self.subTest(variant=variant):
                payload = status_payload(PROVIDER_RATE_LIMIT, columns=160)
                grid = payload["render_grid"]
                banner = next(s for s in grid["row_spans"] if s["row"] == 43)
                if variant == "no_marker":
                    banner.update(text=PROVIDER_RATE_LIMIT, cell_width=len(PROVIDER_RATE_LIMIT))
                elif variant == "indented_marker":
                    banner["column"] = 2
                else:
                    first = "■ " + PROVIDER_RATE_LIMIT.split(" have ")[0]
                    banner.update(text=first, cell_width=len(first))
                    grid["row_spans"].append(span(45 if variant == "blank_gap" else 44,
                                                  0, "have exceeded rate limit.", 0))
                self.assertNotEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind,
                                    "recoverable_error")

    def test_provider_banner_keeps_working_input_queue_and_newer_output_guards(self):
        for kind in ("working", "menu", "composer_busy", "queued_followup", "error_superseded"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                payload = status_payload(PROVIDER_RATE_LIMIT)
                grid = payload["render_grid"]
                if kind == "composer_busy":
                    prompt = next(s for s in grid["row_spans"] if s["row"] == 54 and s["column"] == 2)
                    prompt.update(text="任务请继续", style_id=0, cell_width=10)
                    grid["cursor"]["column"] = 12
                else:
                    text = {"working": "Working (7m 52s • esc to interrupt)",
                            "menu": "Would you like to run the following command?",
                            "queued_followup": "• Queued follow-up inputs",
                            "error_superseded": "• The next operation completed."}[kind]
                    grid["row_spans"].append(span(49, 0, text))
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual(state.kind, kind)
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                daemon.process_once(client)
                self.assertEqual(client.sent, [])

    def test_provider_error_uses_existing_single_submission_and_echo_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = status_payload(PROVIDER_RATE_LIMIT)
            client = FakeClient(payload, visible_text(payload))
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(daemon.runtime["surface-uuid"].error_type, "rate_limit")
            payload["render_grid"]["row_spans"].append(span(49, 0, "› 任务请继续"))
            client.text = visible_text(payload)
            for _ in range(3):
                daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)


class CodexStatusChromeTests(unittest.TestCase):
    def test_status_above_verified_composer_particles_still_allows_recovery(self):
        for error in (PROVIDER_RATE_LIMIT, "high_demand"):
            for columns in (40, 58, 77, 126):
                for count in (1, 2):
                    with self.subTest(error=error, columns=columns, count=count):
                        payload = status_payload(error, columns, count, word_wrap=True)
                        grid = payload["render_grid"]
                        grid["styles"].append({"id": 4, "foreground_source": "rgb", "bold": False})
                        grid["row_spans"].append(span(53, 2, "⢀  ⠈   ⠁ ⢀", 4))
                        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                        self.assertEqual(state.kind, "recoverable_error")
                        self.assertIn(51, state.ignored_chrome_rows)
                        self.assertIn(53, state.ignored_chrome_rows)

    def test_unverified_particles_or_real_output_cannot_hide_status_blocker(self):
        for text, style in (("⢀  ⠈   ⠁ ⢀", 0), ("new output", 4), ("› user input", 4)):
            with self.subTest(text=text, style=style):
                payload = status_payload(PROVIDER_RATE_LIMIT)
                grid = payload["render_grid"]
                grid["styles"].append({"id": 4, "foreground_source": "rgb", "bold": False})
                grid["row_spans"].append(span(53, 2, text, style))
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual(state.kind, "error_superseded")

    def test_live_429_background_terminal_is_not_new_output(self):
        state = core.classify_grid(core.Grid.from_rpc(captured_payload(), "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "rate_limit"))

    def test_all_error_types_singular_plural_wrap_and_resize(self):
        for error in ERRORS:
            for columns in (40, 58, 59, 60, 80, 106, 126, 160):
                for count in (1, 2, 12):
                    for word_wrap in (False, True):
                        with self.subTest(error=error, columns=columns, count=count, word_wrap=word_wrap):
                            payload = status_payload(error, columns, count, word_wrap)
                            state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                            self.assertEqual((state.kind, state.error_type), ("recoverable_error", error))

    def test_status_does_not_change_error_fingerprint(self):
        fingerprints = set()
        for count in (1, 2, 12):
            payload = status_payload(count=count)
            state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
            fingerprints.add(state.fingerprint)
        self.assertEqual(len(fingerprints), 1)
        self.assertNotIn(None, fingerprints)

    def test_newer_transcript_even_faint_still_blocks(self):
        for text in ("• Ran pytest", "› continue", "ordinary newer output", "background terminal running"):
            for style in (0, 2, 3):
                with self.subTest(text=text, style=style):
                    payload = captured_payload()
                    payload["render_grid"]["row_spans"][0]["row"] = 47
                    payload["render_grid"]["row_spans"].append(span(50, 0, text, style))
                    state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                    self.assertEqual(state.kind, "error_superseded")

    def test_only_verified_chrome_shape_is_ignored(self):
        for variant in ("non_faint", "quoted", "wrong_position", "unknown_suffix", "plural_mismatch", "unindented"):
            with self.subTest(variant=variant):
                payload = captured_payload()
                status = next(item for item in payload["render_grid"]["row_spans"] if item["row"] == 51)
                if variant == "non_faint":
                    status["style_id"] = 0
                elif variant == "quoted":
                    status["text"] = "• " + status["text"]
                elif variant == "wrong_position":
                    status["row"] = 50
                elif variant == "unknown_suffix":
                    status["text"] += " extra output"
                elif variant == "plural_mismatch":
                    status["text"] = status["text"].replace("1 background", "2 background")
                elif variant == "unindented":
                    status["text"] = status["text"].lstrip()
                status["cell_width"] = len(status["text"])
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual(state.kind, "error_superseded")

    def test_working_menu_composer_and_queued_input_keep_priority(self):
        for kind in ("working", "menu", "composer_busy", "queued_followup"):
            with self.subTest(kind=kind):
                payload = captured_payload()
                grid = payload["render_grid"]
                if kind == "composer_busy":
                    prompt = next(item for item in grid["row_spans"] if item["row"] == 54 and item["column"] == 2)
                    prompt.update(text="real input", style_id=0, cell_width=10)
                    grid["cursor"]["column"] = 12
                else:
                    text = {"working": "Working (0s • esc to interrupt)",
                            "menu": "Would you like to run the following command?",
                            "queued_followup": "• Queued follow-up inputs"}[kind]
                    grid["row_spans"].append(span(50, 0, text))
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual(state.kind, kind)
                with tempfile.TemporaryDirectory() as directory:
                    client = FakeClient(payload, visible_text(payload))
                    daemon = armed_daemon(directory, client)
                    daemon.process_once(client)
                    self.assertEqual(client.sent, [])

    def test_daemon_recovers_live_shape_then_stops_at_echo_or_queue(self):
        for newer in ("› continue", "• Queued follow-up inputs"):
            with self.subTest(newer=newer), tempfile.TemporaryDirectory() as directory:
                payload = captured_payload()
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)
                payload["render_grid"]["row_spans"].append(span(50, 0, newer))
                client.text = visible_text(payload)
                for _ in range(3):
                    daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)

    def test_pause_resume_reclassifies_without_bypassing_send_gates(self):
        for busy in (False, True):
            with self.subTest(busy=busy), tempfile.TemporaryDirectory() as directory:
                payload = captured_payload()
                if busy:
                    payload["render_grid"]["row_spans"].append(span(50, 0, "• Queued follow-up inputs"))
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                config_path = Path(directory) / "config.json"
                config = json.loads(config_path.read_text())
                config["targets"][0]["paused"] = True
                config_path.write_text(json.dumps(config))
                daemon._config_mtime_ns = -1
                daemon.process_once(client)
                self.assertEqual(client.sent, [])
                config["targets"][0]["paused"] = False
                config_path.write_text(json.dumps(config))
                daemon._config_mtime_ns = -1
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 0 if busy else 1)

    def test_observation_is_persisted_without_overwriting_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = captured_payload()
            payload["render_grid"]["row_spans"].append(span(50, 0, "• Ran private-command"))
            client = FakeClient(payload, visible_text(payload))
            daemon = armed_daemon(directory, client)
            runtime = core.TargetRuntime(error_type="stream", send_count=8, episode_id="original")
            daemon.runtime["surface-uuid"] = runtime
            daemon.process_once(client)
            self.assertEqual((runtime.error_type, runtime.send_count, runtime.episode_id), ("stream", 8, "original"))
            self.assertEqual(runtime.observed_error_type, "rate_limit")
            self.assertEqual(runtime.observed_state, "error_superseded")
            self.assertEqual(runtime.observed_evidence_row, 50)
            self.assertIn("transcript", runtime.observed_reason)
            self.assertNotIn("private-command", runtime.observed_reason)
            self.assertEqual(runtime.observed_chrome_rows, [51])
            restored = core.TargetRuntime.from_dict(json.loads(daemon._serialize_state())["surface-uuid"])
            self.assertEqual(restored.to_dict(), runtime.to_dict())
            self.assertGreater(restored.observed_at, 0)
            self.assertEqual(client.sent, [])

    def test_observation_logs_a_changed_blocker_without_state_change(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(captured_payload()))
            runtime = core.TargetRuntime()
            states = [core.ScreenState("error_superseded", error_type="rate_limit", reason=f"blocker row {row}")
                      for row in (50, 52)]
            with mock.patch.object(daemon.logger, "info") as log:
                daemon._record_state("surface-uuid", runtime, states[0])
                daemon._record_state("surface-uuid", runtime, states[0])
                daemon._record_state("surface-uuid", runtime, states[1])
            self.assertEqual(log.call_count, 2)
            self.assertEqual(runtime.observed_reason, "blocker row 52")

    def test_old_runtime_loads_with_unknown_observation(self):
        runtime = core.TargetRuntime.from_dict({"state": "error_superseded", "error_type": "stream"})
        self.assertEqual(runtime.error_type, "stream")
        self.assertIsNone(runtime.observed_error_type)
        self.assertEqual(runtime.observed_at, 0)

    def test_pause_reload_logs_uuid_and_keeps_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(captured_payload()))
            daemon.runtime["surface-uuid"] = core.TargetRuntime(episode_id="original", send_count=8)
            config_path = Path(directory) / "config.json"
            config = json.loads(config_path.read_text())
            for paused in (True, False):
                config["targets"][0]["paused"] = paused
                config_path.write_text(json.dumps(config))
                daemon._config_mtime_ns = -1
                with mock.patch.object(daemon.logger, "info") as log:
                    daemon._reload_config_if_changed()
                    log.assert_any_call(
                        "surface=%s workspace=%s monitoring=%s next_poll_reclassifies=%s",
                        "surface-uuid", "workspace-uuid", "paused" if paused else "resumed", not paused,
                    )
            self.assertEqual(daemon.runtime["surface-uuid"].episode_id, "original")
            self.assertEqual(daemon.runtime["surface-uuid"].send_count, 8)

    def test_resuming_target_clears_stale_runtime_pause_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(captured_payload()))
            daemon.runtime["surface-uuid"] = core.TargetRuntime(
                state="cmux_unavailable", paused_reason="old read-screen failure",
            )
            config_path = Path(directory) / "config.json"
            config = json.loads(config_path.read_text())
            config["targets"][0]["paused"] = True
            config["targets"][0]["paused_reason"] = "manual pause"
            config_path.write_text(json.dumps(config))
            daemon._config_mtime_ns = -1
            daemon._reload_config_if_changed()
            config["targets"][0]["paused"] = False
            config["targets"][0].pop("paused_reason", None)
            config_path.write_text(json.dumps(config))
            daemon._config_mtime_ns = -1
            daemon._reload_config_if_changed()
            self.assertIsNone(daemon.runtime["surface-uuid"].paused_reason)

    def test_startup_clears_pause_reason_when_config_is_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            state_path = Path(directory) / "state.json"
            config = {
                "schema_version": 1, "mode": "armed", "global_paused": False,
                "message": "任务请继续", "targets": [{
                    "surface_id": "surface-uuid", "workspace_id": "workspace-uuid",
                    "enabled": True, "paused": False,
                }],
            }
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({
                "surface-uuid": {
                    "state": "cmux_unavailable",
                    "paused_reason": "old read-screen failure",
                },
            }))
            daemon = core.WatchDaemon(config_path, state_path, client=FakeClient(captured_payload()))
            self.assertIsNone(daemon.runtime["surface-uuid"].paused_reason)

    def test_tui_uses_current_429_and_shows_blocker_not_last_stream_trigger(self):
        from cmux_supervisor_tui import Candidate, focus_summary, runtime_observation

        runtime = {"state": "error_superseded", "error_type": "stream",
                   "observed_state": "error_superseded", "observed_error_type": "rate_limit",
                   "observed_at": 123, "observed_reason": "rate_limit blocked at row 50"}
        error, reason = runtime_observation(runtime)
        self.assertEqual(error, "rate_limit")
        candidate = Candidate({"surface_id": "surface-uuid"}, "explicit", "error_superseded",
                              error, 8, False, status_detail=reason, agent_kind="codex")
        summary = focus_summary(candidate)
        self.assertIn("429", summary)
        self.assertIn("row 50", summary)
        runtime["state"] = "missing"
        self.assertEqual(runtime_observation(runtime), ("stream", ""))

    def test_chrome_adjacent_to_error_is_not_part_of_fingerprint(self):
        payload = captured_payload()
        payload["render_grid"]["row_spans"][0]["row"] = 50
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        status = next(item for item in payload["render_grid"]["row_spans"] if item["row"] == 51)
        status["text"] = status["text"].replace("1 background terminal", "2 background terminals")
        status["cell_width"] = len(status["text"])
        updated = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.fingerprint, updated.fingerprint)


if __name__ == "__main__":
    unittest.main()
