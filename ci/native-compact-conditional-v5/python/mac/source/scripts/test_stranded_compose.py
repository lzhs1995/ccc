"""Enter is not delivery: a payload stranded in the Codex composer (2026-10-08).

Measured on surface:40: the bridge pasted during "• Compacting context", Enter
did nothing, and the payload stayed in the composer. Tab was then refused for
two reasons: Codex's word wrap dropped the space at "only|because", and a
finished compaction block in scrollback was read as the current state.
"""
import json
import unittest
from unittest.mock import patch

import cmux_bridge as b
from native_test_support import LegacyTerminalFixture, ScreenSequence

MARKER = "C2596_STATUS_20261008T131247Z"
WORDS = ("%s | status: r22 cmaverse-source-delivery-evidence-20261008-r22: bridge test "
         "B1_45ec8129 at 13:04:19Z was COMPOSE_OCCUPIED only because my turn "
         "was still running and my compose box was empty so please re-run the preflight or "
         "dispatch through the protected Tab queue; I am watching read-only and will reply "
         "with the exact acknowledgement as soon as the token arrives. This is a one-time "
         "status message, not a loop, and will not be resent." % MARKER)
# Measured geometry: the first row ends at "only" and "because" starts row two.
WIDTH = WORDS.index(" because") + 4
# Codex pads its footer to the terminal width (measured: content width + 2).
FOOTER = "  GPT-6-Astra xhigh · ~/project · Context 28% used".ljust(WIDTH + 2)


def codex_wrap(text, width=WIDTH):
    """Codex word wrap: break at a space and drop that space."""
    rows, row = [], ""
    for word in text.split(" "):
        if row and len(row) + 1 + len(word) > width:
            rows.append(row)
            row = word
        else:
            row = word if not row else row + " " + word
    rows.append(row)
    return ["› " + rows[0]] + ["  " + r for r in rows[1:]]


def screen(status, text=WORDS, scrollback=(), hint=True):
    rows = list(scrollback) + [status, " ", " "] + codex_wrap(text) + [" ", " ", FOOTER]
    if hint:
        rows.append("  tab to queue message")
    return "\n".join(rows)


WORKING = "• Working (21m 34s • esc to interrupt)"
COMPACTING = "• Compacting context (31s • esc to interrupt)"
OLD_COMPACTION = ["• Compacting context (1s • esc to interrupt)", "  └ Making room to continue.",
                  "• Ran rtk proxy cat file", "  └ ok"]


class WrapMatchTests(unittest.TestCase):
    def test_fixture_reproduces_space_dropping_wrap(self):
        rows = codex_wrap(WORDS)
        self.assertGreater(len(rows), 2)
        self.assertTrue(rows[0].endswith("only"))
        self.assertTrue(rows[1].startswith("  because"))

    def test_word_wrapped_payload_is_owned(self):
        self.assertTrue(b._exact_pending_text(screen(WORKING), WORDS))

    def test_changed_payloads_still_rejected(self):
        for changed in (WORDS + "x", WORDS.replace("only because", "only  because"),
                        WORDS.replace("was still", "was  still"), WORDS.replace("loop", "lop")):
            with self.subTest(changed=changed[-40:]):
                self.assertFalse(b._exact_pending_text(screen(WORKING), changed))

    def test_short_row_is_not_a_wrap(self):
        # A row far shorter than the composer cannot have word-wrapped.
        rendered = "\n".join([WORKING, " ", "› " + MARKER, "  short line", " ", " ",
                              "-" * WIDTH, FOOTER])
        self.assertFalse(b._exact_pending_text(rendered, MARKER + " short line"))
        # Same rows remain an exact match for the explicit newline payload.
        self.assertTrue(b._exact_pending_text(rendered.replace("\n" + "-" * WIDTH, ""),
                                              MARKER + "\nshort line"))


class StatusRegionTests(unittest.TestCase):
    def test_stale_compaction_in_scrollback_does_not_block_tab(self):
        s = screen(WORKING, scrollback=OLD_COMPACTION)
        self.assertFalse(b.receiver_cannot_submit_now(s))
        self.assertTrue(b._codex_tab_queue_allowed(s, WORDS))

    def test_current_compaction_blocks_tab(self):
        for status in (COMPACTING, "• Reconnecting... 2/5 (esc to interrupt)"):
            with self.subTest(status=status):
                s = screen(status, scrollback=OLD_COMPACTION)
                self.assertTrue(b.receiver_cannot_submit_now(s))
                self.assertFalse(b._codex_tab_queue_allowed(s, WORDS))


class PrePasteGateTests(unittest.TestCase):
    def test_no_paste_while_receiver_compacting(self):
        idle = "\n".join([COMPACTING, " ", "› ", FOOTER])
        with LegacyTerminalFixture(), patch.object(b, "read_screen", return_value=idle), \
                patch.object(b, "send_text") as send, patch.object(b, "send_key") as key, \
                patch.object(b.time, "sleep"):
            with self.assertRaises(b.DispatchUnconfirmed) as caught:
                b.submit_text("peer", WORDS, marker=MARKER)
        self.assertIn("RECEIVER_COMPACTING", str(caught.exception))
        send.assert_not_called()
        key.assert_not_called()

    def test_enter_left_in_composer_is_named_stranded(self):
        idle = "\n".join([WORKING, " ", "› ", FOOTER])
        # 完整草稿连续两次稳定后发 Enter；压缩在按键后开始。
        draft = "\n".join([" ", " "] + codex_wrap(WORDS) + [" ", " ", FOOTER])
        stuck = screen(COMPACTING)
        with LegacyTerminalFixture() as native, patch.object(b, "read_screen",
                side_effect=native.ready_screens(idle, draft, stuck)), \
                patch.object(b, "send_text"), patch.object(b, "send_key") as key, \
                patch.object(b.time, "sleep"):
            with self.assertRaises(b.DispatchUnconfirmed) as caught:
                b.submit_text("peer", WORDS, marker=MARKER)
        self.assertEqual(caught.exception.state, b.STRANDED_IN_COMPOSE)
        self.assertIn("NATIVE_DELIVERY_UNCONFIRMED", str(caught.exception))
        self.assertEqual([c.args[1] for c in key.call_args_list], ["enter"])


class MidRenderTests(unittest.TestCase):
    """r23 callback: the post-Enter read showed only a prefix of the payload."""

    def test_partial_render_is_reobserved_then_tab_queued(self):
        idle = "\n".join([WORKING, " ", "› ", FOOTER])
        partial = "\n".join([WORKING, " ", "› " + WORDS[:60], "  next_", " ", FOOTER,
                             "  tab to queue message"])
        consumed = "\n".join(["› " + WORDS, "• Read evidence", WORKING, " ", "› ", FOOTER])
        self.assertTrue(b._compose_is_partial_payload(partial.replace("  next_", ""), WORDS))
        with LegacyTerminalFixture() as native, patch.object(b, "read_screen",
                side_effect=ScreenSequence([idle, partial.replace("\n  next_", ""),
                                            screen(WORKING), screen(WORKING), consumed])), \
                patch.object(b, "send_text"), \
                patch.object(b, "send_key", side_effect=native.receipt_on_key(WORDS)) as key, \
                patch.object(b.time, "sleep"):
            result = b.submit_text("peer", WORDS, marker=MARKER)
        self.assertEqual([c.args[1] for c in key.call_args_list], ["tab"])
        self.assertTrue(result["confirmed"])

    def test_full_or_foreign_compose_is_not_partial(self):
        self.assertFalse(b._compose_is_partial_payload(screen(WORKING), WORDS))
        self.assertFalse(b._compose_is_partial_payload(screen(WORKING, text="other draft"), WORDS))


class RecoverStrandedTests(unittest.TestCase):
    BEFORE = "\n".join([WORKING, " ", "› ", FOOTER])

    def consumed(self):
        return "\n".join(["› " + WORDS, "• Read evidence", WORKING, " ", "› ", FOOTER])

    def queued_screen(self):
        # 排队文本位于 live composer 上方；composer 自身保持完整原结构。
        banner = "Messages to be submitted after next tool call"
        return screen(WORKING).replace(WORKING, "\n".join([WORKING, banner, WORDS]), 1)

    @staticmethod
    def add_rows(value, added):
        return value.replace("\n" + FOOTER, "\n" + added + "\n" + FOOTER, 1)

    def original_attempt(self, native, observations=None):
        # 经公开入口建立原始 journal，不手写或修改原 ENTER 的证据。
        draft = "\n".join([" ", " "] + codex_wrap(WORDS) + [" ", " ", FOOTER])
        after = [screen(WORKING)] if observations is None else observations
        with patch.object(b, "read_screen", side_effect=native.ready_screens(
                self.BEFORE, draft, *after)), \
                patch.object(b, "send_text") as original_send, \
                patch.object(b, "send_key") as original_key:
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_text("peer", WORDS, marker=MARKER)
            original_send.assert_called_once_with("peer", WORDS)
            original_key.assert_called_once_with("peer", "enter")
        root = native.home / ".local/state/multi-agent-collaboration/message-dispatch-v1"
        attempts = list(root.glob("*/attempt-*.json"))
        self.assertEqual(len(attempts), 1)
        return attempts[0]

    def run_recovery(self, screens, *, received=False, original_observations=None):
        with LegacyTerminalFixture() as native:
            attempt = self.original_attempt(native, original_observations)
            with patch.object(b, "read_screen", side_effect=ScreenSequence(screens)), \
                    patch.object(b, "send_text") as send, \
                    patch.object(b, "send_key", side_effect=(
                        native.receipt_on_key(WORDS) if received else None)) as key:
                try:
                    result = b.submit_text("peer", WORDS, marker=MARKER, recover_stranded=True)
                except (b.DispatchUnconfirmed, b.TaskPackContractError) as exc:
                    result = exc
            send.assert_not_called()  # 恢复不得再次粘贴。
            root = native.home / ".local/state/multi-agent-collaboration/message-dispatch-v1"
            attempts = list(root.glob("*/attempt-*.json"))
            self.assertEqual(attempts, [attempt])
            journal = json.loads(attempts[0].read_text())
            self.assertEqual(sum(e["phase"] == "PASTE_INTENT" for e in journal["events"]), 1)
            events = [e["phase"] for e in journal["events"]
                      if e.get("recovery") == "stranded_compose"]
        return result, [c.args[1] for c in key.call_args_list], events

    def test_current_compaction_refuses_even_if_later_draft_is_unchanged(self):
        result, keys, events = self.run_recovery(
            [screen(COMPACTING), screen(COMPACTING),
             screen(WORKING), screen(WORKING)])
        self.assertEqual(keys, [])
        self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_unchanged_busy_draft_gets_one_tab(self):
        result, keys, events = self.run_recovery(
            [*([screen(WORKING, scrollback=OLD_COMPACTION)] * 3), self.consumed()], received=True)
        self.assertEqual(keys, ["tab"])
        self.assertEqual(events.count("QUEUE_TAB_INTENT"), 1)
        self.assertEqual(events[-1], "POST_QUEUE_TAB_OBSERVATION")
        self.assertTrue(result["confirmed"])

    def test_idle_receiver_gets_one_enter(self):
        idle_pending = "\n".join([" ", " "] + codex_wrap(WORDS) + [" ", " ", FOOTER])
        result, keys, _ = self.run_recovery([idle_pending] * 3 + [self.consumed()], received=True)
        self.assertEqual(keys, ["enter"])
        self.assertTrue(result["confirmed"])

    def test_changed_draft_gets_no_key(self):
        result, keys, _ = self.run_recovery([screen(WORKING, text=WORDS + " extra")])
        self.assertEqual(keys, [])
        self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_unconsumed_after_key_is_not_success(self):
        s = screen(WORKING)
        result, keys, _ = self.run_recovery([s, s])
        self.assertEqual(keys, ["tab"])
        self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_current_extra_blank_rows_cannot_authorize_recovery(self):
        for added in ["", "\n", " ", "  ", "\n  "]:
            with self.subTest(added=repr(added)):
                result, keys, _ = self.run_recovery([self.add_rows(screen(WORKING), added)])
                self.assertEqual(keys, [])
                self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_original_post_enter_blank_change_still_blocks_after_restore(self):
        intact = screen(WORKING)
        for added in ["", "\n", " ", "  "]:
            with self.subTest(added=repr(added)):
                result, keys, _ = self.run_recovery([intact], original_observations=[
                    self.add_rows(intact, added), intact])
                self.assertEqual(keys, [])
                self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_original_unknown_or_partial_observation_still_blocks_after_restore(self):
        intact = screen(WORKING)
        for changed in ["render unavailable", screen(WORKING, text=WORDS[:80])]:
            with self.subTest(changed=changed):
                result, keys, _ = self.run_recovery([intact], original_observations=[changed, intact])
                self.assertEqual(keys, [])
                self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_original_post_enter_compaction_still_blocks_after_it_finishes(self):
        intact = screen(WORKING)
        result, keys, _ = self.run_recovery([intact], original_observations=[screen(COMPACTING), intact])
        self.assertEqual(keys, [])
        self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_original_queue_observation_still_blocks_after_draft_restores(self):
        intact = screen(WORKING)
        queued = self.queued_screen()
        self.assertTrue(b.pending_queue_holds(queued, MARKER))
        result, keys, _ = self.run_recovery([intact], original_observations=[queued, intact])
        self.assertEqual(keys, [])
        self.assertIsInstance(result, (b.DispatchUnconfirmed, b.TaskPackContractError))

    def test_queue_during_recovery_still_blocks_after_draft_restores(self):
        intact = screen(WORKING)
        result, keys, _ = self.run_recovery([intact, self.queued_screen(), intact])
        self.assertEqual(keys, [])
        self.assertIsInstance(result, (b.DispatchUnconfirmed, b.TaskPackContractError))

    def test_rejected_queue_observation_cannot_be_retried_after_restore(self):
        with LegacyTerminalFixture() as native:
            attempt = self.original_attempt(native)
            with patch.object(b, "send_text") as send, patch.object(b, "send_key") as key:
                for observed in [self.queued_screen(), screen(WORKING)]:
                    with patch.object(b, "read_screen", return_value=observed):
                        with self.assertRaises((b.DispatchUnconfirmed, b.TaskPackContractError)):
                            b.submit_text("peer", WORDS, marker=MARKER, recover_stranded=True)
                send.assert_not_called()
                key.assert_not_called()
            events = json.loads(attempt.read_text())["events"]
            self.assertEqual(sum(e["phase"] == "PASTE_INTENT" for e in events), 1)
            self.assertFalse(attempt.with_name("receipt.json").exists())

    def test_recovery_observation_change_cannot_be_forgiven_by_later_stability(self):
        intact = screen(WORKING)
        changed_screens = [self.add_rows(intact, ""), self.add_rows(intact, "  "),
                           "render unavailable", screen(WORKING, text=WORDS[:80]),
                           screen(COMPACTING),
                           self.add_rows(screen(COMPACTING), ""),
                           intact + "\nunknown interpreter"]
        for changed in changed_screens:
            with self.subTest(changed=changed):
                result, keys, _ = self.run_recovery([intact, changed, intact])
                self.assertEqual(keys, [])
                self.assertIsInstance(result, b.DispatchUnconfirmed)

    def test_rejected_changed_observation_cannot_be_retried_after_restore(self):
        intact = screen(WORKING)
        with LegacyTerminalFixture() as native:
            attempt = self.original_attempt(native)
            with patch.object(b, "send_text") as send, patch.object(b, "send_key") as key:
                for observed in [self.add_rows(intact, ""), intact]:
                    with patch.object(b, "read_screen", return_value=observed):
                        with self.assertRaises((b.DispatchUnconfirmed, b.TaskPackContractError)):
                            b.submit_text("peer", WORDS, marker=MARKER, recover_stranded=True)
                send.assert_not_called()
                key.assert_not_called()
            self.assertFalse(attempt.with_name("receipt.json").exists())

    def test_one_recovery_key_budget_includes_transport_failure(self):
        for fail_key in (False, True):
            with self.subTest(fail_key=fail_key), LegacyTerminalFixture() as native:
                attempt = self.original_attempt(native)
                effect = RuntimeError("transport lost") if fail_key else None
                with patch.object(b, "read_screen", return_value=screen(WORKING)), \
                        patch.object(b, "send_text") as send, \
                        patch.object(b, "send_key", side_effect=effect) as key:
                    with self.assertRaises(RuntimeError):
                        b.submit_text("peer", WORDS, marker=MARKER, recover_stranded=True)
                    key.assert_called_once_with("peer", "tab")
                    with self.assertRaisesRegex(b.TaskPackContractError, "STRANDED_RECOVERY_ALREADY_USED"):
                        b.submit_text("peer", WORDS, marker=MARKER, recover_stranded=True)
                    key.assert_called_once_with("peer", "tab")
                    send.assert_not_called()
                events = json.loads(attempt.read_text())["events"]
                self.assertEqual(sum(e["phase"] == "PASTE_INTENT" for e in events), 1)
                self.assertEqual(sum(e["phase"] == "QUEUE_TAB_INTENT" for e in events), 1)


if __name__ == "__main__":
    unittest.main()
