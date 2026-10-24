"""Compaction hidden behind Codex's queued-inputs block (2026-10-09 01:15 +0800).

Measured with EXECUTOR_READY_73c025c0: the supervisor was compacting while an
earlier ask (ff5ade80) sat in "• Queued follow-up inputs". The status region
took that queue header as the latest status bullet, so every compaction gate
passed; the bridge pasted and pressed Enter into a compacting receiver and the
payload stayed in the composer. The same screens showed the header-blind queue
detector calling the genuinely queued ff5ade80 "not queued".
Fixtures are the journal's PASTE_INTENT / POST_ENTER screens, sanitized.
"""
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture

FIXTURES = Path(__file__).resolve().parents[1] / "verification" / "fixtures"
BEFORE = (FIXTURES / "codex-compacting-behind-queued-followups-20261009.txt").read_text()
AFTER = (FIXTURES / "codex-stranded-below-queued-followups-20261009.txt").read_text()
LIVE, QUEUED = "EXECUTOR_READY_73c025c0", "EXECUTOR_READY_ff5ade80"
TEXT = "STATUS_c0f0a001 | idle executor asks for the next independent task."
FOOTER = "  GPT-6-Astra xhigh · ~/project · Context 37% used"
COMPACTING = "• Compacting context (26s • esc to interrupt)"


def working(screen):
    # 同一布局只换当前状态：用作负对照。
    assert COMPACTING in screen
    return screen.replace(COMPACTING, "• Working (26s • esc to interrupt)")


class CompactionBehindQueueTests(unittest.TestCase):
    def test_fixture_puts_compaction_above_the_queue_block(self):
        rows = BEFORE.splitlines()
        status, header, prompt = (rows.index(COMPACTING), rows.index("• Queued follow-up inputs"),
                                  rows.index("› Ask Codex to do anything"))
        self.assertLess(status, header)
        self.assertLess(header, prompt)
        self.assertTrue(b.compose_block_is_empty(BEFORE))

    def test_compaction_behind_queue_block_cannot_submit(self):
        region = b._current_status_region(BEFORE)
        self.assertIn("Compacting context", region)
        self.assertNotIn("↳", region)
        self.assertTrue(b.receiver_cannot_submit_now(BEFORE))

    def test_no_paste_or_key_while_compaction_hides_behind_queue(self):
        with LegacyTerminalFixture(), patch.object(b, "read_screen", return_value=BEFORE), \
                patch.object(b, "send_text") as send, patch.object(b, "send_key") as key, \
                patch.object(b.time, "sleep"):
            with self.assertRaises(b.DispatchUnconfirmed) as caught:
                b.submit_text("peer", TEXT, marker="STATUS_c0f0a001")
        self.assertIn("RECEIVER_COMPACTING", str(caught.exception))
        send.assert_not_called()
        key.assert_not_called()

    def test_working_receiver_behind_same_queue_block_is_not_refused(self):
        # 负对照：同一布局但当前是 Working，必须照常粘贴（随后因无草稿而保留原次）。
        with LegacyTerminalFixture(), patch.object(b, "read_screen", return_value=working(BEFORE)), \
                patch.object(b, "send_text") as send, patch.object(b, "send_key") as key, \
                patch.object(b.time, "sleep"):
            with self.assertRaises(b.DispatchUnconfirmed) as caught:
                b.submit_text("peer", TEXT, marker="STATUS_c0f0a001")
        self.assertNotIn("RECEIVER_COMPACTING", str(caught.exception))
        self.assertEqual(send.call_count, 1)
        key.assert_not_called()

    def test_old_compaction_in_scrollback_still_does_not_block(self):
        old = ["• Compacting context (1s • esc to interrupt)", "  └ Making room to continue.", " "]
        screen = "\n".join(old) + "\n" + working(BEFORE)
        self.assertFalse(b.receiver_cannot_submit_now(screen))

    def test_queued_text_quoting_compaction_is_not_the_status(self):
        rows = working(BEFORE).splitlines()
        rows.insert(rows.index("• Queued follow-up inputs") + 2,
                    "    note: • Compacting context was hidden behind this block")
        self.assertFalse(b.receiver_cannot_submit_now("\n".join(rows)))

    def test_stale_queue_header_in_scrollback_does_not_hide_current_status(self):
        rows = ["• Queued follow-up inputs", "  ↳ an older ask", "    shift+← edit last queued message",
                " ", "• Ran rtk proxy cat notes.md", "  └ ok", " ", "• Compacting context (3s • esc to interrupt)",
                " ", "› Ask Codex to do anything", " ", FOOTER]
        self.assertTrue(b.receiver_cannot_submit_now("\n".join(rows)))
        rows[7] = "• Working (3s • esc to interrupt)"
        self.assertFalse(b.receiver_cannot_submit_now("\n".join(rows)))


class QueuedFollowupTests(unittest.TestCase):
    def test_queued_followup_is_queued_and_live_draft_is_not(self):
        self.assertTrue(b.pending_queue_holds(AFTER, QUEUED))
        self.assertTrue(b.pending_queue_holds(BEFORE, QUEUED))
        self.assertFalse(b.pending_queue_holds(AFTER, LIVE))

    def test_indented_quote_of_the_header_is_not_a_queue(self):
        rows = ["• Ran rtk proxy cat notes.md", "  └ ok", "    • Queued follow-up inputs",
                "    ↳ %s quoted from an old report" % QUEUED, " ", "› Ask Codex to do anything", " ", FOOTER]
        self.assertFalse(b.pending_queue_holds("\n".join(rows), QUEUED))


if __name__ == "__main__":
    unittest.main()
