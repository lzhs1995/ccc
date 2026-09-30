"""Native replay padding must not hide a current error from the text router."""
import tempfile
import unittest

import cmux_codex_watch as watch
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload


class ViewportPaddingTests(unittest.TestCase):
    def payload(self, **kwargs):
        payload = grid_payload([], error=HIGH_DEMAND_TEXT, **kwargs)
        payload["render_grid"]["rows"] = 80
        return payload

    def test_padded_frame_enters_the_same_structural_guard_as_unpadded_text(self):
        payload = self.payload()
        grid = watch.Grid.from_rpc(payload, "surface-uuid")
        self.assertEqual(watch.classify_text_prefilter(grid.lines).kind, "candidate")
        self.assertEqual(watch.classify_grid(grid).kind, "recoverable_error")
        client = FakeClient(payload, "\n".join(grid.lines))
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, client)
            self.addCleanup(daemon._process_snapshots.close)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_padding_does_not_bypass_draft_or_working_or_menu_protection(self):
        for options in ({"composer": "busy"}, {"working": True}, {"menu": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                payload = self.payload(**options)
                grid = watch.Grid.from_rpc(payload, "surface-uuid")
                client = FakeClient(payload, "\n".join(grid.lines))
                daemon = armed_daemon(directory, client)
                self.addCleanup(daemon._process_snapshots.close)
                daemon.process_once(client)
                self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()
