"""Recovery and no-duplicate guarantees for current Codex retry layouts."""

import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import (
    FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload,
    reconnect_payload, span, visible_lines,
)


class CodexReconnectRecoveryTests(unittest.TestCase):
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
                for offset in (1, 2, 30, 59):
                    clock.return_value = 1000 + offset
                    payload = reconnect_payload("5/5", elapsed=f"{offset}s")
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
                clock.return_value = 1060
                restarted.process_once(client)
                self.assertEqual(len(client.sent), 2)

    def test_real_working_below_reconnect_header_still_blocks(self):
        payload = reconnect_payload()
        row = payload["render_grid"]["cursor"]["row"]
        payload["render_grid"]["row_spans"].append(
            span(row - 3, 0, "Working (4s • esc to interrupt)")
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
        self.assertFalse(state.allow_repeat)
        self.assertEqual(core.classify_text_prefilter(visible_lines(payload)).kind, "candidate")

    def test_unicode_reconnect_ellipsis_uses_the_same_repeat_floor(self):
        payload = reconnect_payload()
        for row in payload["render_grid"]["row_spans"]:
            row["text"] = row["text"].replace("Reconnecting...", "Reconnecting…")
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual(state.kind, "recoverable_error")
        self.assertFalse(state.allow_repeat)

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

    def test_prompt_echo_only_rearms_after_a_new_error(self):
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
                for now in (1002, 1065, 1200):
                    clock.return_value = now
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), 1)
                client.payload = reconnect_payload(stale_banner=True)
                client.text = "\n".join(visible_lines(client.payload))
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 2)

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


if __name__ == "__main__":
    unittest.main()
