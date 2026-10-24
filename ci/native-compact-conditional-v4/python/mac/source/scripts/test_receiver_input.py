"""No live terminal writes: regression probes use the public send entrypoint."""
import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import cmux_bridge as bridge
from native_test_support import LegacyTerminalFixture


class ReceiverInputTests(unittest.TestCase):
    @contextlib.contextmanager
    def isolated_message_identity(self):
        with LegacyTerminalFixture() as native:
            yield native

    def test_shell_or_unknown_does_not_receive_any_bytes_or_keys(self):
        screens = ["researcher@mac ~ %", "bash-3.2$ ", "PS C:\\work> ",
                   "› Ask Codex to do anything\nGPT-6 high\nresearcher@mac ~ %",
                   "❯ ", "", "ordinary text"]
        for screen in screens:
            for force in (False, True):
                with self.subTest(screen=screen, force=force), LegacyTerminalFixture(), \
                     patch.object(bridge, "read_screen", return_value=screen), \
                     patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key:
                    with self.assertRaises(bridge.TaskPackContractError if force else bridge.DispatchUnconfirmed) as error:
                        bridge.submit_text("peer", "STATUS: continuation", marker="continuation", force_compose=force)
                    if not force:
                        self.assertEqual(error.exception.state, bridge.SUPERVISOR_DID_NOT_SUBMIT)
                    send.assert_not_called()
                    key.assert_not_called()

    def test_no_marker_does_not_claim_consumption(self):
        with patch.object(bridge, "read_screen", return_value="› Ask Codex to do anything\nGPT-6 high"), \
             patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key, \
             patch.object(bridge.time, "sleep"):
            with self.assertRaisesRegex(bridge.TaskPackContractError, 'MESSAGE_MARKER_REQUIRED'):
                bridge.submit_text("peer", "STATUS: continuation")
        send.assert_not_called()
        key.assert_not_called()

    def test_user_draft_is_not_appended_or_cleared(self):
        with LegacyTerminalFixture(), patch.object(bridge, "read_screen", return_value="› my unsent question\nGPT-6 high"), \
             patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key:
            with self.assertRaises(bridge.DispatchUnconfirmed) as error:
                bridge.submit_text("peer", "STATUS: continuation", marker="continuation")
            self.assertEqual(error.exception.state, bridge.COMPOSE_OCCUPIED)
            send.assert_not_called()
            key.assert_not_called()

    def test_public_cli_default_preserves_unsent_draft(self):
        for command in ("submit-text", "submit_text"):
            with self.subTest(command=command), self.isolated_message_identity(), \
                 patch.object(bridge, "read_screen", return_value="› my unsent question\nGPT-6 high"), \
                 patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key, \
                 contextlib.redirect_stderr(io.StringIO()):
                code = bridge._cli_main([command, "--surface", "peer", "--text", "STATUS: continuation",
                                         "--marker", "continuation"])
                self.assertEqual(code, 75)
                send.assert_not_called()
                key.assert_not_called()

    def test_both_cli_dispatch_commands_default_to_no_force(self):
        for command, method, extra in (("submit-text", "submit_text", []),
                                       ("submit_text", "submit_text", []),
                                       ("submit-task-pack", "submit_task_pack", ["--task-pack", "/tmp/pack.json"]),
                                       ("submit_task_pack", "submit_task_pack", ["--task-pack", "/tmp/pack.json"])):
            with self.subTest(command=command), patch.object(bridge, method, return_value={"confirmed": True}) as submit, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bridge._cli_main([command, "--surface", "peer", "--text", "text", *extra]), 0)
                self.assertIs(submit.call_args.kwargs["force_compose"], False)

    def test_cli_explicit_force_choice_is_preserved(self):
        for command, method, extra in (("submit-text", "submit_text", []),
                                       ("submit-task-pack", "submit_task_pack", ["--task-pack", "/tmp/pack.json"])):
            for flag, expected in (("--force-compose", True), ("--no-force-compose", False)):
                with self.subTest(command=command, flag=flag), patch.object(bridge, method, return_value={"confirmed": True}) as submit, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(bridge._cli_main([command, "--surface", "peer", "--text", "text", *extra, flag]), 0)
                    self.assertIs(submit.call_args.kwargs["force_compose"], expected)

    def test_queued_message_is_one_send_not_a_failed_delivery_retry(self):
        screens = ["› Ask Codex to do anything\nGPT-6 high",
                   "Messages to be submitted after current tool\nmarker\n› Ask Codex to do anything\nGPT-6 high"]
        with LegacyTerminalFixture() as native, patch.object(bridge, "read_screen", side_effect=native.ready_screens(
                screens[0], native.draft('STATUS: marker'), screens[1])), patch.object(bridge, "send_text") as send, \
             patch.object(bridge, "send_key") as key, patch.object(bridge.time, "sleep"):
            with self.assertRaises(bridge.DispatchUnconfirmed) as error:
                bridge.submit_text("peer", "STATUS: marker", marker="marker")
        self.assertEqual(error.exception.state, bridge.DELIVERY_QUEUED_AT_RECEIVER)
        send.assert_called_once()
        key.assert_called_once_with("peer", "enter")

    def test_current_provider_chrome_not_a_historical_banner(self):
        self.assertEqual(bridge.receiver_input_kind("❯\n[Opus 5] context 30%"), "AGENT_TUI")
        self.assertEqual(bridge.receiver_input_kind("[Opus 5] context 30%\n❯"), "UNKNOWN")

    def test_unknown_bottom_input_overrides_historical_tui(self):
        historical = "› Ask Codex to do anything\nGPT-6 high"
        for tail in ("➜  project git:(main)", "custom-host [main] >>", "interpreter waiting"):
            for force in (False, True):
                with self.subTest(tail=tail, force=force), LegacyTerminalFixture(), \
                     patch.object(bridge, "read_screen", return_value=historical + "\n" + tail), \
                     patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key:
                    with self.assertRaises(bridge.TaskPackContractError if force else bridge.DispatchUnconfirmed):
                        bridge.submit_text("peer", "STATUS: continuation", marker="continuation", force_compose=force)
                    send.assert_not_called()
                    key.assert_not_called()

    def test_force_never_clears_shell_unknown_active_or_user_draft(self):
        occupied = "› supervisor-owned unsent draft\nGPT-6 high"
        unsafe = ("researcher@mac ~/project %", "render unavailable",
                  "✻ Running tool\n" + occupied,
                  "• Working (3s • esc to interrupt)\n" + occupied,
                  "Messages to be submitted after current tool\n" + occupied)
        for changed in unsafe:
            for safe_reads in (1, 2, 3, 4):
                with self.subTest(changed=changed, safe_reads=safe_reads), \
                     patch.object(bridge, "read_screen", side_effect=[occupied] * safe_reads + [changed]), \
                     patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key, \
                     patch.object(bridge, "focus_surface"), patch.object(bridge.time, "sleep"):
                    with self.assertRaisesRegex(bridge.TaskPackContractError, 'PRESERVE_COMPOSE'):
                        bridge.submit_text("peer", "STATUS: continuation", marker="continuation", force_compose=True)
                    send.assert_not_called()
                    key.assert_not_called()

    def test_marker_queue_has_priority_over_new_activity_from_earlier_work(self):
        import contextlib
        import io
        import json
        idle = "› Ask Codex to do anything\nGPT-6 high"
        queued = "• Ran preceding task tool\nMessages to be submitted after next tool call\nmarker\n" + idle
        for screens in ([idle, queued], [idle, "render unavailable", queued]):
            with self.subTest(screens=screens), self.isolated_message_identity() as native, \
                 patch.object(bridge, "read_screen", side_effect=native.ready_screens(
                     screens[0], native.draft('STATUS: marker'), *screens[1:])), \
                 patch.object(bridge, "send_text") as send, patch.object(bridge, "send_key") as key, \
                 patch.object(bridge.time, "sleep"), contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()) as error:
                code = bridge._cli_main(["submit-text", "--surface", "peer", "--text", "STATUS: marker", "--marker", "marker", "--no-force-compose"])
                self.assertEqual(code, 75)
                self.assertEqual(json.loads(error.getvalue())["delivery_state"], bridge.DELIVERY_QUEUED_AT_RECEIVER)
                send.assert_called_once()
                key.assert_called_once_with("peer", "enter")


class PendingQueueRegionTests(unittest.TestCase):
    MARKER = 'queue-boundary-marker-20261009'
    BANNER = 'Messages to be submitted after next tool call'

    @staticmethod
    def composers(text):
        body = text.replace('\n', '\n  ')
        border = '─' * 96
        return [
            ('codex', '› ' + body + '\nGPT-6 high'),
            ('claude_unbordered', '❯ ' + body + '\n[claude-opus-5]'),
            ('claude_bordered', border + '\n❯ ' + body + '\n' + border + '\n[claude-opus-5]'),
        ]

    def test_own_live_draft_under_unrelated_queue_is_not_queued(self):
        for layout, composer in self.composers('STATUS: ' + self.MARKER):
            screen = self.BANNER + '\nunrelated queued message\n' + composer
            with self.subTest(layout=layout):
                self.assertFalse(bridge.pending_queue_holds(screen, self.MARKER))

    def test_marker_in_actual_queue_region_is_queued(self):
        for layout, composer in self.composers(''):
            screen = self.BANNER + '\nSTATUS: ' + self.MARKER + '\n' + composer
            with self.subTest(layout=layout):
                self.assertTrue(bridge.pending_queue_holds(screen, self.MARKER))
                with self.assertRaises(bridge.DispatchUnconfirmed) as error:
                    bridge._unconfirmed_native(screen, self.MARKER, {})
                self.assertEqual(error.exception.state, bridge.DELIVERY_QUEUED_AT_RECEIVER)

    def test_banner_inside_live_composer_is_not_a_queue_region(self):
        for text in [self.BANNER + '\n' + self.MARKER,
                     'my draft\n' + self.BANNER + '\n' + self.MARKER]:
            for layout, composer in self.composers(text):
                with self.subTest(layout=layout, text=text):
                    self.assertFalse(bridge.pending_queue_holds(composer, self.MARKER))

    def test_marker_below_composer_or_in_footer_is_not_queued(self):
        for layout, composer in self.composers(''):
            for tail in [self.MARKER, self.BANNER + '\n' + self.MARKER]:
                screen = self.BANNER + '\nunrelated queued message\n' + composer + '\n' + tail
                with self.subTest(layout=layout, tail=tail):
                    self.assertFalse(bridge.pending_queue_holds(screen, self.MARKER))

    def test_marker_before_queue_banner_is_not_queued(self):
        for layout, composer in self.composers(''):
            screen = self.MARKER + '\n' + self.BANNER + '\nunrelated queued message\n' + composer
            with self.subTest(layout=layout):
                self.assertFalse(bridge.pending_queue_holds(screen, self.MARKER))

    def test_new_activity_ends_the_queue_region(self):
        for layout, composer in self.composers(''):
            for activity in ['• Read evidence', '⏺ Read evidence']:
                screen = '\n'.join([self.BANNER, 'unrelated queued message', activity,
                                    'tool output quotes ' + self.MARKER, composer])
                with self.subTest(layout=layout, activity=activity):
                    self.assertFalse(bridge.pending_queue_holds(screen, self.MARKER))

    def test_historical_prompt_after_banner_is_not_queued(self):
        for layout, composer in self.composers(''):
            for glyph in ['›', '❯']:
                screen = '\n'.join([self.BANNER, 'unrelated queued message',
                                    glyph + ' ' + self.MARKER, '• Read evidence', composer])
                with self.subTest(layout=layout, glyph=glyph):
                    self.assertFalse(bridge.pending_queue_holds(screen, self.MARKER))

    def test_missing_live_composer_cannot_bound_a_queue_region(self):
        for tail in ['', '\nresearcher@mac ~ %', '\nrender unavailable']:
            with self.subTest(tail=tail):
                self.assertFalse(bridge.pending_queue_holds(self.BANNER + '\n' + self.MARKER + tail,
                                                           self.MARKER))

    def test_public_send_reports_own_draft_as_stranded_under_unrelated_queue(self):
        text = 'STATUS: ' + self.MARKER
        drafts = dict(self.composers(text))
        for layout, idle in self.composers(''):
            provider = 'codex' if layout == 'codex' else 'claude'
            draft = drafts[layout]
            after = self.BANNER + '\nunrelated queued message\n' + draft
            with self.subTest(layout=layout), LegacyTerminalFixture(provider=provider) as native, \
                    patch.object(bridge, 'read_screen', side_effect=native.ready_screens(idle, draft, after)), \
                    patch.object(bridge, 'send_text') as send, patch.object(bridge, 'send_key') as key:
                with self.assertRaises(bridge.DispatchUnconfirmed) as error:
                    bridge.submit_text('peer', text, marker=self.MARKER)
                self.assertEqual(error.exception.state, bridge.STRANDED_IN_COMPOSE)
                send.assert_called_once_with('peer', text)
                key.assert_called_once_with('peer', 'enter')


# --- 2026-10-04 measured regression: pane-width truncation of chrome rows ---
# Both defects below were measured on real screens, not synthesized. A narrow
# pane cuts a trailing chrome row mid-token. Because the row is matched with
# `fullmatch` (receiver kind) or an anchored alternation (compose chrome), the
# partial row stops matching and is then treated as unknown current input /
# user content -- refusing executor<->supervisor delivery with zero keys sent.
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "verification" / "fixtures"


class TruncatedChromeRowTests(unittest.TestCase):
    """A chrome row cut off by pane width must stay chrome."""

    def test_measured_truncated_codex_footer_is_agent_tui(self):
        screen = (FIXTURES / "codex-footer-truncated-20261004.txt").read_text()
        self.assertEqual(bridge.receiver_input_kind(screen), "AGENT_TUI")

    def test_progressive_truncation_of_hint_row_stays_agent_tui(self):
        head = ("\u2022 Ran a tool\n \n\u203a Ask Codex to do anything\n \n"
                "  GPT-6-Astra xhigh \u00b7 ~/work \u00b7 Context 34% used \u00b7 Fa\u2026 Pursuing goal\n")
        tails = (
            "  ? for shortcuts                    \u26a0 5 warnings \u00b7 f2 to view",
            "  ? for shortcuts                    \u26a0 5 warnings \u00b7 f2 to",
            "  ? for shortcuts                    \u26a0 5 warnings \u00b7 f2",
            "  ? for shortcuts                    \u26a0 5 warnings \u00b7",
            "  ? for shortcuts                    \u26a0 5 warnings ",
            "  ? for shortcuts                    \u26a0 5 warning",
            "  ? for shortcuts                    \u26a0 5 ",
            "  ? for shortcuts                    \u26a0",
            "  ? for shortcuts",
            "  \u2190 for agents \u00b7 ? for shortcuts        \u26a0 12 warnings \u00b7 f2 to v",
        )
        for tail in tails:
            with self.subTest(tail=tail.strip()[-28:]):
                self.assertEqual(bridge.receiver_input_kind(head + tail), "AGENT_TUI")

    def test_unknown_trailing_row_still_overrides_footer(self):
        """Fail-closed direction preserved: only the known hint row is skipped."""
        head = ("\u203a Ask Codex to do anything\n \n"
                "  GPT-6-Astra xhigh \u00b7 ~/work \u00b7 Context 34% used\n")
        for tail in ("  \u279c  project git:(main)", "  custom-host [main] >>",
                     "  interpreter waiting", "  ? for shortcut",
                     "  something ? for shortcuts elsewhere extra"):
            with self.subTest(tail=tail.strip()):
                self.assertEqual(bridge.receiver_input_kind(head + tail), "UNKNOWN")

    def test_measured_truncated_claude_footer_fails_closed(self):
        screen = (FIXTURES / "claude-compose-truncated-footer-20260924.txt").read_text()
        body = bridge.compose_block_text(screen)
        self.assertIsNotNone(body)
        # 截断 footer 无法证明完整边框；不能据此授权输入或清空。
        self.assertEqual(bridge.receiver_input_kind(screen), "UNKNOWN")
        self.assertFalse(bridge.compose_block_is_empty(screen))

    def test_truncated_model_and_cwd_rows_are_chrome(self):
        for row in ("  [claude-opus\u2026", "  claude/u8-fo\u2026", "  [Opus 5\u2026",
                    "  claude/feature-branch", "  10 MCPs | 6 \u2026"):
            with self.subTest(row=row.strip()):
                self.assertTrue(bridge._COMPOSE_CHROME_RE.match(row.strip()),
                                f"{row!r} should be chrome")

    def test_measured_tab_to_queue_footer_is_agent_tui(self):
        """Codex swaps the shortcuts row for this one when the composer holds text.

        Measured 2026-10-04 on surface:27 while a peer's callback sat unsent.
        Same defect shape: the row renders BELOW the model row, so a
        tail[-1]-only footer check never reaches the model row and a live agent
        receiver classified as UNKNOWN, refusing every send with zero input.
        """
        screen = (FIXTURES / "codex-footer-tab-to-queue-20261004.txt").read_text()
        self.assertEqual(bridge.receiver_input_kind(screen), "AGENT_TUI")

    def test_tab_to_queue_row_truncates_without_losing_detection(self):
        head = ("\u203a Ask Codex to do anything\n \n"
                "  GPT-6-Astra xhigh \u00b7 ~/work \u00b7 Context 34% used\n")
        for tail in ("  tab to queue message", "  tab to queue mess", "  tab to q"):
            with self.subTest(tail=tail.strip()):
                self.assertEqual(bridge.receiver_input_kind(head + tail), "AGENT_TUI")

    def test_tab_row_near_misses_stay_unknown(self):
        """Fail-closed: only the measured row is chrome, not any 'tab to ...' line."""
        head = ("\u203a Ask Codex to do anything\n \n"
                "  GPT-6-Astra xhigh \u00b7 ~/work \u00b7 Context 34% used\n")
        for tail in ("  tab to something else", "  tab to", "  queue message"):
            with self.subTest(tail=tail.strip()):
                self.assertEqual(bridge.receiver_input_kind(head + tail), "UNKNOWN")

    def test_tab_to_queue_does_not_expose_peer_unsent_text(self):
        """Receiver-kind and compose-emptiness are separate gates; keep them so.

        The row becoming chrome must not let a caller clear a peer's pending
        callback: the compose check still sees the pasted text above it.
        """
        screen = (FIXTURES / "codex-footer-tab-to-queue-20261004.txt").read_text()
        self.assertFalse(bridge.compose_block_is_empty(screen))

    def test_genuine_typed_text_still_reads_occupied(self):
        """Negative control: the fix must not erase real user input."""
        screen = ("\u256d\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256e\n"
                  "\u2502 > please refactor the parser \u2502\n"
                  "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f\n"
                  "  [claude-opus\u2026\n  \u23f5\u23f5 bypass")
        self.assertFalse(bridge.compose_block_is_empty(screen))


if __name__ == "__main__":
    unittest.main()
