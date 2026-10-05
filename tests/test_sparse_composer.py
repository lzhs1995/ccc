"""Full sparse render grids from Codex 0.160 must preserve input guards."""
import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import (FakeClient, HIGH_DEMAND_TEXT, armed_daemon,
                              grid_payload, reconnect_payload, span, visible_lines)


def sparse_payload(**kwargs):
    payload = grid_payload([], **kwargs)
    grid = payload["render_grid"]
    grid["full"] = True
    row = grid["cursor"]["row"]
    grid["row_spans"] = [s for s in grid["row_spans"]
                         if not (s["row"] == row and s["column"] == 1)]
    for s in grid["row_spans"]:
        if s["row"] == row and s["column"] == 2 and s["style_id"] == 2:
            s.update(text="Ask Codex to do anything", cell_width=24)
    return payload


def classify(payload):
    return core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind


class SparseComposerTests(unittest.TestCase):
    def test_full_sparse_placeholder_is_empty_and_idle(self):
        grid = core.Grid.from_rpc(sparse_payload(), "surface-uuid")
        self.assertEqual(grid.lines[grid.cursor.row], "› Ask Codex to do anything")
        self.assertEqual(core._composer_status(grid), ("empty", grid.cursor.row))
        self.assertEqual(core.classify_grid(grid).kind, "idle")

    def test_full_sparse_final_error_is_recoverable(self):
        self.assertEqual(classify(sparse_payload(error="exceeded retry limit, last status: 429 Too Many Requests")),
                         "recoverable_error")

    def test_unknown_or_delta_frame_cannot_prove_missing_separator(self):
        for full in (None, False):
            with self.subTest(full=full):
                payload = sparse_payload()
                payload["render_grid"]["full"] = full
                self.assertEqual(classify(payload), "incompatible")

    def test_typed_placeholder_at_home_is_busy(self):
        payload = sparse_payload()
        for s in payload["render_grid"]["row_spans"]:
            if s["column"] == 2:
                s["style_id"] = 0
        self.assertEqual(classify(payload), "composer_busy")

    def test_typed_draft_is_busy(self):
        self.assertEqual(classify(sparse_payload(composer="busy")), "composer_busy")

    def test_wrong_separator_is_incompatible(self):
        for text, style in (("x", 0), (" ", 2)):
            with self.subTest(text=text, style=style):
                payload = sparse_payload()
                grid = payload["render_grid"]
                grid["row_spans"].append(span(grid["cursor"]["row"], 1, text, style))
                self.assertEqual(classify(payload), "incompatible")

    def test_merged_prompt_and_separator_remain_supported(self):
        payload = sparse_payload()
        for s in payload["render_grid"]["row_spans"]:
            if s["text"] == "›":
                s.update(text="› ", cell_width=2)
        self.assertEqual(classify(payload), "idle")

    def test_merged_nonblank_separator_is_incompatible(self):
        payload = sparse_payload()
        for s in payload["render_grid"]["row_spans"]:
            if s["text"] == "›":
                s.update(text="›x", cell_width=2)
        self.assertEqual(classify(payload), "incompatible")

    def test_hidden_cursor_is_incompatible(self):
        self.assertEqual(classify(sparse_payload(cursor_visible=False)), "incompatible")

    def test_dim_or_invisible_prompt_is_incompatible(self):
        for attr in ("faint", "invisible"):
            with self.subTest(attr=attr):
                payload = sparse_payload()
                payload["render_grid"]["styles"][1][attr] = True
                self.assertEqual(classify(payload), "incompatible")

    def test_prompt_out_of_position_is_incompatible(self):
        payload = sparse_payload()
        for s in payload["render_grid"]["row_spans"]:
            if s["text"] == "›":
                s["column"] = 3
        self.assertEqual(classify(payload), "incompatible")

    def test_working_and_menu_still_win(self):
        for kwargs, expected in (({"working": True}, "working"), ({"menu": True}, "menu")):
            with self.subTest(expected=expected):
                self.assertEqual(classify(sparse_payload(error="rate limit exceeded", **kwargs)), expected)

    def test_reconnect_waits_for_native_final_failure(self):
        payload = reconnect_payload(stale_banner=True)
        grid = payload["render_grid"]
        grid["full"] = True
        grid["row_spans"] = [s for s in grid["row_spans"]
                             if not (s["row"] == grid["cursor"]["row"] and s["column"] == 1)]
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "\n".join(visible_lines(payload)))
            daemon = armed_daemon(directory, client)
            self.addCleanup(daemon._process_snapshots.close)
            turn = mock.Mock()
            daemon.codex_queue_recovery.current_turn = turn
            for kind in ("task_started", "unknown"):
                turn.return_value = {"kind": kind}
                daemon.process_once(client)
                self.assertEqual(client.sent, [], kind)
            client.payload = sparse_payload(error=HIGH_DEMAND_TEXT)
            client.text = "\n".join(visible_lines(client.payload))
            turn.return_value = {"kind": "task_complete", "session_id": "original",
                                 "turn_id": "failed-one", "at": 100,
                                 "error": {"message": HIGH_DEMAND_TEXT}}
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_queued_input_is_not_idle_or_sendable(self):
        payload = sparse_payload()
        grid = payload["render_grid"]
        grid["row_spans"].append(span(grid["cursor"]["row"] - 2, 0, "• Queued follow-up inputs"))
        self.assertEqual(classify(payload), "queued_followup")


if __name__ == "__main__":
    unittest.main()
