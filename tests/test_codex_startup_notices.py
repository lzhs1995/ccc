"""Native startup notices must not hide a stopped, otherwise retryable turn."""
import tempfile
import textwrap
import unittest

import cmux_codex_watch as core
from tests.test_codex_status_chrome import ERRORS, status_payload, visible_text
from tests.test_watch import FakeClient, armed_daemon, span


NOTICE = "⚠ `--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation."
CONNECTION_ERROR = "Connection failed: error sending request"


def notice_payload(error="high_demand", *, columns=126, hard_wrap=False, notice=NOTICE):
    payload = status_payload(error, columns=columns)
    grid = payload["render_grid"]
    grid["styles"].append({"id": 15, "foreground": "#CDAC08", "foreground_source": "palette",
                           "foreground_palette_index": 3, "faint": False, "bold": False})
    grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] >= 54]
    error_lines = textwrap.wrap("■ " + ERRORS.get(error, error), columns) if error else []
    notice_lines = ([notice[i:i + columns] for i in range(0, len(notice), columns)] if hard_wrap
                    else textwrap.wrap(notice, columns))
    grid["row_spans"] += [span(20 + i, 0, text, 3) for i, text in enumerate(error_lines)]
    grid["row_spans"] += [span(43 + i, 0, text, 15) for i, text in enumerate(notice_lines)]
    return payload


class StartupNoticeTests(unittest.TestCase):
    def test_native_notice_keeps_each_error_recoverable_and_deduplicated(self):
        for error in [*ERRORS, CONNECTION_ERROR]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                payload = notice_payload(error)
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                expected = "stream" if error == CONNECTION_ERROR else error
                self.assertEqual((state.kind, state.error_type), ("recoverable_error", expected))
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                self.addCleanup(daemon._process_snapshots.close)
                daemon.process_once(client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)

    def test_complete_notice_survives_narrow_word_and_hard_wrapping(self):
        for columns in (40, 58, 80, 126):
            for hard_wrap in (False, True):
                with self.subTest(columns=columns, hard_wrap=hard_wrap):
                    payload = notice_payload(columns=columns, hard_wrap=hard_wrap)
                    state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                    self.assertEqual((state.kind, state.error_type), ("recoverable_error", "high_demand"))

    def test_partial_changed_quoted_or_unstyled_warning_still_blocks(self):
        for variant in ("partial", "extra", "other_warning", "quote", "indented", "unstyled", "gap"):
            with self.subTest(variant=variant):
                text = {"partial": NOTICE[:-12], "extra": NOTICE + " Approval required.",
                        "other_warning": "⚠ Hook requires approval before this task can continue.",
                        "quote": "• " + NOTICE}.get(variant, NOTICE)
                payload = notice_payload(notice=text, columns=58 if variant == "gap" else 126)
                notice_spans = [s for s in payload["render_grid"]["row_spans"] if 43 <= s["row"] < 54]
                if variant == "indented":
                    notice_spans[0]["column"] = 2
                elif variant == "unstyled":
                    notice_spans[0]["style_id"] = 0
                elif variant == "gap":
                    for s in notice_spans[1:]:
                        s["row"] += 1
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind,
                                 "error_superseded")

    def test_notice_alone_cannot_start_a_turn(self):
        payload = notice_payload(error="")
        self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_newer_transcript_and_input_guards_remain_effective(self):
        for kind in ("working", "menu", "composer_busy", "queued_followup", "error_superseded"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                payload = notice_payload(CONNECTION_ERROR)
                grid = payload["render_grid"]
                if kind == "composer_busy":
                    draft = next(s for s in grid["row_spans"] if s["row"] == 54 and s["column"] == 2)
                    draft.update(text="user draft", style_id=0, cell_width=10)
                    grid["cursor"]["column"] = 12
                else:
                    text = {"working": "• Working (7s • esc to interrupt)",
                            "menu": "Would you like to run the following command?",
                            "queued_followup": "• Queued follow-up inputs",
                            "error_superseded": "• Finished the task."}[kind]
                    grid["row_spans"].append(span(49, 0, text))
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                self.addCleanup(daemon._process_snapshots.close)
                daemon.process_once(client)
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind, kind)
                self.assertEqual(client.sent, [])

    def test_connection_error_requires_complete_native_banner(self):
        for text in (CONNECTION_ERROR, "Connection failed: error send\ning request"):
            self.assertEqual(core._match_error_block("■ " + text), "stream")
        for text in ("Connection failed", "Example: " + CONNECTION_ERROR,
                     CONNECTION_ERROR + " This is an example.", "Connection failed: permission denied"):
            self.assertIsNone(core._match_error_block("■ " + text))

    def test_newer_connection_failure_replaces_an_older_high_demand_error(self):
        payload = notice_payload(CONNECTION_ERROR)
        payload["render_grid"]["row_spans"].append(span(10, 0, "■ " + ERRORS["high_demand"], 3))
        state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
        self.assertEqual((state.kind, state.error_type), ("recoverable_error", "stream"))


if __name__ == "__main__":
    unittest.main()
