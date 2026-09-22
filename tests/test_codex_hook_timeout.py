"""Hook timeout cards must not hide a live provider error or bypass guards."""
import tempfile
import unittest

import cmux_codex_watch as core
from tests.test_codex_status_chrome import ERRORS, status_payload, visible_text
from tests.test_watch import FakeClient, armed_daemon, span


def timeout_payload(error="high_demand", detail="  └ hook timed out after 5s"):
    payload = status_payload(error)
    grid = payload["render_grid"]
    grid["row_spans"] += [span(47, 0, "•", 3), span(47, 1, " Hook failed"),
                          span(48, 0, detail)]
    return payload


class HookTimeoutTests(unittest.TestCase):
    def test_timeout_after_each_provider_error_remains_recoverable(self):
        for error in ERRORS:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                payload = timeout_payload(error)
                state = core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid"))
                self.assertEqual((state.kind, state.error_type), ("recoverable_error", error))
                self.assertTrue({47, 48}.issubset(state.ignored_chrome_rows))
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                self.addCleanup(daemon._process_snapshots.close)
                daemon.process_once(client)
                daemon.process_once(client)
                self.assertEqual(len(client.sent), 1)

    def test_exit_code_and_unknown_failure_cards_still_block(self):
        for detail in ("  └ hook exited with code 2", "  └ hook exited with code 3",
                       "  └ permission denied", "  └ hook timed out after 5s; denied",
                       "hook timed out after 5s", "", "  └ hook failed"):
            with self.subTest(detail=detail):
                payload = timeout_payload(detail=detail)
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind,
                                 "error_superseded")

    def test_timeout_does_not_hide_new_transcript_or_extra_guard_output(self):
        for text in ("• Finished the task.", "› user request", "  └ approval required"):
            with self.subTest(text=text):
                payload = timeout_payload()
                payload["render_grid"]["row_spans"].append(span(49, 0, text))
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind,
                                 "error_superseded")

    def test_timeout_without_provider_error_does_not_send(self):
        payload = timeout_payload()
        payload["render_grid"]["row_spans"] = [s for s in payload["render_grid"]["row_spans"]
                                               if s["row"] >= 47]
        self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind, "idle")

    def test_timeout_preserves_working_menu_draft_and_queue(self):
        for kind in ("working", "menu", "composer_busy", "queued_followup"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                payload = timeout_payload()
                grid = payload["render_grid"]
                if kind == "composer_busy":
                    draft = next(s for s in grid["row_spans"] if s["row"] == 54 and s["column"] == 2)
                    draft.update(text="asd", style_id=0, cell_width=3)
                    grid["cursor"]["column"] = 5
                else:
                    text = {"working": "Working (7s • esc to interrupt)",
                            "menu": "Would you like to run the following command?",
                            "queued_followup": "• Queued follow-up inputs"}[kind]
                    grid["row_spans"].append(span(49, 0, text))
                client = FakeClient(payload, visible_text(payload))
                daemon = armed_daemon(directory, client)
                self.addCleanup(daemon._process_snapshots.close)
                daemon.process_once(client)
                self.assertEqual(core.classify_grid(core.Grid.from_rpc(payload, "surface-uuid")).kind, kind)
                self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()
