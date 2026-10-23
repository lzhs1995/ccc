#!/usr/bin/env python3
"""Behavior tests for the callback-first sentinel classifiers."""

import unittest

import cmux_executor_sentinel as sentinel


class Args:
    task_id = "test"
    executor_surface = "surface:1"
    supervisor_surface = "surface:2"
    interval = 300
    unchanged_threshold = 1
    lines = 220
    callback_token = "TOKEN"
    compact_stall_threshold = 2
    compact_interval = 60
    active_stall_threshold = 3


class SentinelTests(unittest.TestCase):
    def test_api_error_precedes_activity(self):
        self.assertEqual(sentinel.classify("Thinking\nAPI Error: 502 Upstream API request failed"), "API_ERROR")

    def test_historical_api_error_followed_by_active_spinner_is_active(self):
        screen = (
            "API Error: 502 Upstream API request failed\n"
            "❯ continue\n"
            "✳ current task… (26s · thinking with max effort)"
        )
        self.assertEqual(sentinel.classify(screen), "ACTIVE")

    def test_new_api_error_after_activity_wins(self):
        screen = "✳ current task… (26s)\nAPI Error: 502 Upstream API request failed"
        self.assertEqual(sentinel.classify(screen), "API_ERROR")

    def test_api_error_words_in_supervisor_prompt_are_not_an_error(self):
        screen = (
            "❯ [CMUX-AGENT][from:surface:41]\n"
            "  TASK:\n"
            "  STATUS: If a real API error occurs, leave its request id visible.\n"
            "\n"
            "✳ current task… (26s · thinking with max effort)"
        )
        self.assertEqual(sentinel.classify(screen), "ACTIVE")

    def test_verbatim_api_error_quoted_in_prompt_is_not_an_error(self):
        screen = (
            "❯ supervisor asks about this line:\n"
            "  API Error: 502 Upstream API request failed. (request id: old)\n"
        )
        self.assertEqual(sentinel.classify(screen), "IDLE_OR_UNKNOWN")

    def test_real_api_error_after_prompt_is_detected(self):
        screen = (
            "❯ continue; if API error occurs preserve it\n"
            "\n"
            "✻ 502 Upstream API request failed. (request id: current) · Retrying in 7s"
        )
        self.assertEqual(sentinel.classify(screen), "API_ERROR")

    def test_done_and_blocked(self):
        self.assertEqual(sentinel.classify("\nDONE: report ready"), "DONE")
        self.assertEqual(sentinel.classify("\nBLOCKED: missing input"), "BLOCKED")

    def test_stale_done_for_another_phase_is_not_terminal(self):
        screen = "\nDONE: PLAN_REVIEW_R3\n\nThinking with max effort"
        self.assertEqual(sentinel.classify(screen, "PHASE1_IMPLEMENTATION_R1"), "ACTIVE")

    def test_current_callback_token_is_terminal(self):
        screen = "\nDONE: PHASE1_IMPLEMENTATION_R1 | EVIDENCE=/tmp/report.md"
        self.assertEqual(sentinel.classify(screen, "PHASE1_IMPLEMENTATION_R1"), "DONE")

    def test_historical_done_and_current_token_in_prompt_do_not_combine(self):
        screen = (
            "DONE: PHASE1_IMPLEMENTATION_R1 | EVIDENCE=/tmp/r1.md\n"
            "❯ supervisor says callback with DONE: PHASE1_IMPLEMENTATION_R2\n"
            "Thinking with max effort"
        )
        self.assertEqual(sentinel.classify(screen, "PHASE1_IMPLEMENTATION_R2"), "ACTIVE")

    def test_embedded_callback_template_is_not_done(self):
        screen = 'Run: cmux-agent ask surface:41 "DONE: PHASE1_IMPLEMENTATION_R2"'
        self.assertEqual(sentinel.classify(screen, "PHASE1_IMPLEMENTATION_R2"), "IDLE_OR_UNKNOWN")

    def test_multiline_cmux_supervisor_callback_template_is_not_done(self):
        screen = (
            "❯ [CMUX-AGENT][2026-08-11T02:43:39Z][from:surface:41]\n"
            "  TASK:\n"
            "  STATUS: FINAL API retry. Create the missing manifest, then callback\n"
            "  DONE: PHASE2B_RC715_ISOLATED_INSTALL_SMOKE_R1 | VERDICT=<PASS|FAIL> |\n"
            "  EVIDENCE=<absolute-report-path>.\n"
            "\n"
            "✻ 502 Upstream API request failed. (request id: current)\n"
        )
        self.assertEqual(
            sentinel.classify(screen, "PHASE2B_RC715_ISOLATED_INSTALL_SMOKE_R1"),
            "API_ERROR",
        )

    def test_executor_assistant_done_after_prompt_is_terminal(self):
        screen = (
            "❯ supervisor asks for the final callback\n"
            "  DONE: PHASE2B_RC715_ISOLATED_INSTALL_SMOKE_R1\n"
            "\n"
            "⏺ DONE: PHASE2B_RC715_ISOLATED_INSTALL_SMOKE_R1 | EVIDENCE=/tmp/report.md\n"
        )
        self.assertEqual(
            sentinel.classify(screen, "PHASE2B_RC715_ISOLATED_INSTALL_SMOKE_R1"),
            "DONE",
        )

    def test_compacting_is_not_idle(self):
        self.assertEqual(sentinel.classify("Compacting conversation… 57%"), "COMPACTING")

    def test_spinner_task_line_is_active(self):
        screen = "✳ 攻克Section16-20卡死根因… (12m 6s · ↓ 21.2k tokens)"
        self.assertEqual(sentinel.classify(screen), "ACTIVE")

    def test_compaction_failure_and_context_full_are_distinct(self):
        self.assertEqual(sentinel.classify("Compaction failed: unable to summarize"), "COMPACT_FAILED")
        self.assertEqual(
            sentinel.classify("Autocompact is thrashing: context refilled"),
            "COMPACT_FAILED",
        )
        self.assertEqual(sentinel.classify("Context full"), "CONTEXT_FULL")
        self.assertEqual(
            sentinel.classify("Context limit reached · /compact or /clear to continue"),
            "CONTEXT_FULL",
        )

    def test_new_compacted_receipt_supersedes_historical_thrash(self):
        screen = (
            "Autocompact is thrashing: context refilled\n"
            "❯ /compact keep active task only\n"
            "  ⎿  Compacted (ctrl+o to see full summary)\n"
            "❯ \n"
        )
        self.assertEqual(sentinel.classify(screen), "COMPACTED")

    def test_compact_progress_uses_progress_bar_not_context_chrome(self):
        screen = "Compacting conversation… (9m)\n  █████ 89%\n上下文 79%"
        self.assertEqual(sentinel.compact_progress(screen), 89)

    def test_compaction_uses_fast_poll_without_changing_normal_cadence(self):
        args = Args()
        args.interval = 7200
        args.compact_interval = 60
        self.assertEqual(
            sentinel.next_poll_interval(args, {"last_classification": "COMPACTING"}),
            60,
        )
        self.assertEqual(
            sentinel.next_poll_interval(args, {"last_classification": "ACTIVE"}),
            7200,
        )

    def test_dynamic_chrome_is_removed(self):
        a = sentinel.normalize_screen("work\n上下文 70%\nThinking for 12s ✻")
        b = sentinel.normalize_screen("work\n上下文 71%\nThinking for 18s ✢")
        self.assertEqual(a, b)

    def test_active_unchanged_screen_does_not_emit_no_progress(self):
        original_read = sentinel.read_screen
        original_notify = sentinel.notify_supervisor
        notifications = []
        try:
            sentinel.read_screen = lambda _surface, _lines: "still thinking with max effort"
            sentinel.notify_supervisor = lambda _surface, message: notifications.append(message)
            normalized = sentinel.normalize_screen("still thinking with max effort")
            digest = __import__("hashlib").sha256(normalized.encode("utf-8")).hexdigest()
            next_state, terminal = sentinel.inspect(
                Args(), {"screen_signature": digest, "unchanged_checks": 0}
            )
            self.assertFalse(terminal)
            self.assertEqual(next_state["last_classification"], "ACTIVE")
            self.assertEqual(next_state["unchanged_checks"], 1)
            self.assertEqual(notifications, [])
        finally:
            sentinel.read_screen = original_read
            sentinel.notify_supervisor = original_notify

    def test_active_stall_alerts_only_after_three_unchanged_checks(self):
        original_read = sentinel.read_screen
        original_notify = sentinel.notify_supervisor
        notifications = []
        screen = "✳ current task… (11m · ↓ 308 tokens)"
        try:
            sentinel.read_screen = lambda _surface, _lines: screen
            sentinel.notify_supervisor = lambda _surface, message: notifications.append(message)
            normalized = sentinel.normalize_screen(screen)
            digest = __import__("hashlib").sha256(normalized.encode("utf-8")).hexdigest()
            state = {"screen_signature": digest, "unchanged_checks": 1}
            state, _ = sentinel.inspect(Args(), state)
            self.assertEqual(state["unchanged_checks"], 2)
            self.assertEqual(notifications, [])
            state, _ = sentinel.inspect(Args(), state)
            self.assertEqual(state["unchanged_checks"], 3)
            self.assertEqual(len(notifications), 1)
            self.assertIn("EVENT=ACTIVE_STALLED", notifications[0])
        finally:
            sentinel.read_screen = original_read
            sentinel.notify_supervisor = original_notify

    def test_ordinary_no_progress_is_recorded_but_not_announced(self):
        """Ordinary NO_PROGRESS writes state and stays silent.

        This encodes the R3 decision that reversed the previous contract. The old
        behaviour notified the supervisor on every idle interval, which is the
        incident where a watchdog cost more tokens than the task it guarded: an
        executor that is safely idle between milestones is not news, and repeating
        it every interval trains the supervisor to ignore the channel.

        Silence here is not blindness. The classification still lands in state, so
        `status` shows it, and the dedup ledger is still updated so a genuinely
        new event is not suppressed behind a stale key. Terminal and error events
        (DONE, BLOCKED, API_ERROR, CONTEXT_FULL, COMPACT_FAILED) still announce
        immediately -- see the neighbouring tests, which must stay green.
        """
        original_read = sentinel.read_screen
        original_notify = sentinel.notify_supervisor
        notifications = []
        try:
            sentinel.read_screen = lambda _surface, _lines: "❯ "
            sentinel.notify_supervisor = lambda _surface, message: notifications.append(message)
            next_state, terminal = sentinel.inspect(
                Args(), {"screen_signature": "different", "unchanged_checks": 9,
                         "last_classification": "ACTIVE"}
            )
            self.assertFalse(terminal)
            self.assertEqual(next_state["last_classification"], "IDLE_OR_UNKNOWN")
            self.assertEqual(next_state["unchanged_checks"], 0)
            # The load-bearing assertion: no ask() for ordinary no-progress.
            self.assertEqual(notifications, [])
            # But the event IS on the record, so the supervisor can read it.
            self.assertTrue(next_state.get("last_alert_key", "").startswith("NO_PROGRESS:"))
            self.assertIsNotNone(next_state.get("last_alert_at"))
        finally:
            sentinel.read_screen = original_read
            sentinel.notify_supervisor = original_notify

    def test_terminal_events_still_announce_immediately(self):
        """Control for the test above: silence must be scoped to NO_PROGRESS only.

        Without this pairing, a future edit could silence every alert and the
        no-progress test would still pass. A watchdog that never speaks is not a
        cheaper watchdog, it is an absent one.
        """
        original_read = sentinel.read_screen
        original_notify = sentinel.notify_supervisor
        notifications = []
        try:
            # classify() requires the current callback token on the same line as the
            # marker, so a bare "BLOCKED:" is deliberately NOT terminal. Args.callback_token
            # is "TOKEN"; omitting it is how a historical or templated BLOCKED line is
            # correctly ignored.
            sentinel.read_screen = lambda _surface, _lines: "⏺ BLOCKED: TOKEN needs credentials"
            sentinel.notify_supervisor = lambda _surface, message: notifications.append(message)
            next_state, _terminal = sentinel.inspect(Args(), {})
            self.assertEqual(next_state["last_classification"], "BLOCKED")
            self.assertEqual(len(notifications), 1)
            self.assertIn("EVENT=BLOCKED", notifications[0])
        finally:
            sentinel.read_screen = original_read
            sentinel.notify_supervisor = original_notify

    def test_compact_stall_requires_two_unchanged_checks(self):
        original_read = sentinel.read_screen
        original_notify = sentinel.notify_supervisor
        notifications = []
        screen = "Compacting conversation… (10m)\n  █████ 72%"
        try:
            sentinel.read_screen = lambda _surface, _lines: screen
            sentinel.notify_supervisor = lambda _surface, message: notifications.append(message)
            state, _ = sentinel.inspect(Args(), {
                "last_classification": "COMPACTING",
                "compact_progress_percent": 72,
                "compact_unchanged_checks": 0,
            })
            self.assertEqual(state["compact_unchanged_checks"], 1)
            self.assertEqual(notifications, [])
            state, _ = sentinel.inspect(Args(), state)
            self.assertEqual(state["compact_unchanged_checks"], 2)
            self.assertEqual(len(notifications), 1)
            self.assertIn("EVENT=COMPACT_STALLED", notifications[0])
        finally:
            sentinel.read_screen = original_read
            sentinel.notify_supervisor = original_notify


if __name__ == "__main__":
    unittest.main()
