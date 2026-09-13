import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_readonly_sampler as sampler  # noqa: E402
from cmux_codex_watch import Grid  # noqa: E402


def span(row, column, text, style_id=0, cell_width=None):
    return {
        "row": row,
        "column": column,
        "cell_width": cell_width or max(1, len(text)),
        "style_id": style_id,
        "text": text,
    }


def claude_payload(lines, *, composer="empty", active=False, completed=False, question=False, cursor_visible=True):
    rows = max(16, len(lines) + 5)
    composer_row = rows - 3
    styles = [
        {"id": 0, "foreground": "#fff", "background": "#000", "faint": False},
        {"id": 1, "foreground": "#fff", "background": "#000", "faint": True},
    ]
    spans = [span(row, 0, text) for row, text in enumerate(lines)]
    spans.append(span(composer_row, 0, "❯"))
    if composer == "busy":
        spans.append(span(composer_row, 2, "typed input"))
    elif composer == "placeholder":
        spans.append(span(composer_row, 2, "Type a message", 1))
    if active:
        spans.append(span(composer_row - 2, 0, "✶ Percolating… (2m 0s · ↓ 2.1k tokens)"))
    if completed:
        spans.append(span(composer_row - 2, 0, "✻ Sautéed for 2m 50s"))
    if question:
        spans.append(span(composer_row - 2, 0, "Would you like to run this command?"))
    return {
        "render_grid": {
            "format": "cmux.render-grid.v1",
            "rows": rows,
            "columns": 120,
            "cursor": {"row": composer_row, "column": 2, "visible": cursor_visible},
            "styles": styles,
            "row_spans": spans,
            "scrollback_spans": [span(0, 0, "✶ Percolating… (old)")],
        }
    }


class ReadOnlyClient:
    def __init__(self, payload):
        self.payload = payload
        self.replays = []

    def replay(self, workspace_id, surface_id):
        self.replays.append((workspace_id, surface_id))
        return self.payload


class ClaudeSamplerTests(unittest.TestCase):
    def test_active_spinner_is_active(self):
        frame = sampler.classify_claude_grid(Grid.from_rpc(claude_payload([], active=True), "s"))
        self.assertEqual(frame.state, "active")
        self.assertTrue(frame.active_spinner)

    def test_completed_past_tense_is_not_active(self):
        frame = sampler.classify_claude_grid(Grid.from_rpc(claude_payload([], completed=True), "s"))
        self.assertEqual(frame.state, "idle_probeable")
        self.assertFalse(frame.active_spinner)
        self.assertTrue(frame.completed_marker)

    def test_footer_ask_user_question_count_is_not_a_dialog(self):
        frame = sampler.classify_claude_grid(
            Grid.from_rpc(claude_payload(["✓ AskUserQuestion ×2"], question=False), "s")
        )
        self.assertFalse(frame.waiting_user)

    def test_real_question_is_waiting_user(self):
        frame = sampler.classify_claude_grid(Grid.from_rpc(claude_payload([], question=True), "s"))
        self.assertEqual(frame.state, "waiting_user")

    def test_non_faint_composer_is_busy(self):
        frame = sampler.classify_claude_grid(Grid.from_rpc(claude_payload([], composer="busy"), "s"))
        self.assertEqual(frame.state, "composer_busy")

    def test_sampler_is_read_only_and_does_not_send(self):
        client = ReadOnlyClient(claude_payload([], completed=True))
        target = [{"surface_id": "surface-uuid", "workspace_id": "workspace-uuid", "workspace_ref": "workspace:11", "pane_ref": "pane:24", "ref": "surface:59"}]
        with tempfile.TemporaryDirectory() as directory:
            result = sampler.run_sampling(client, Path(directory), duration_sec=0, interval_sec=5, targets=target)
            self.assertEqual(result["target_count"], 1)
            self.assertEqual(client.replays, [("workspace-uuid", "surface-uuid")])
            frames = (Path(directory) / "frames.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("Sautéed", frames)
            metadata = json.loads((Path(directory) / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["read_only"])

    def test_existing_frames_can_be_summarized_without_cmux(self):
        rows = [
            {"captured_at": 100.0, "surface_key": "a", "state": "active", "signature": "x", "composer": "empty"},
            {"captured_at": 105.0, "surface_key": "a", "state": "active", "signature": "y", "composer": "empty"},
            {"captured_at": 110.0, "surface_key": "a", "state": "idle_probeable", "signature": "z", "composer": "empty"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = root / "frames.jsonl"
            frames.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            (root / "metadata.json").write_text(
                json.dumps({"started_at": 100.0, "targets": [{"surface_key": "a"}]}),
                encoding="utf-8",
            )
            result = sampler.summarize_frames(frames, root / "metadata.json")
            self.assertEqual(result["frame_count"], 3)
            self.assertEqual(result["states"]["active"], 2)
            self.assertEqual(result["signature_change_interval_sec"]["median"], 5.0)


if __name__ == "__main__":
    unittest.main()
