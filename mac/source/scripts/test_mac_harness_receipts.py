#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from native_test_support import LegacyTerminalFixture, ScreenSequence
import cmux_prompt_reference as reference


HARNESS_PATH = Path(__file__).with_name("mac_harness.py")
SPEC = importlib.util.spec_from_file_location("mac_harness", HARNESS_PATH)
HARNESS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(HARNESS)

BRIDGE = HARNESS.cmux

ROUND_GUARD_PATH = Path(__file__).with_name("cmux_consensus_round_guard.py")
ROUND_GUARD_SPEC = importlib.util.spec_from_file_location(
    "cmux_consensus_round_guard", ROUND_GUARD_PATH
)
ROUND_GUARD = importlib.util.module_from_spec(ROUND_GUARD_SPEC)
assert ROUND_GUARD_SPEC.loader is not None
ROUND_GUARD_SPEC.loader.exec_module(ROUND_GUARD)


def setUpModule():
    # harness 的 registry 也是持久写入；所有测试都必须限制在临时目录。
    registry = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(registry.cleanup)
    patcher = mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", Path(registry.name))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    bodies = mock.patch.object(reference, 'body_root', return_value=Path(registry.name) / 'bodies')
    bodies.start()
    unittest.addModuleCleanup(bodies.stop)


class ReceiptOrderingTests(unittest.TestCase):
    def args(self, root: Path, **overrides):
        # record-round hashes the artifact, so it must exist before the call.
        artifact = root / "review.md"
        if not artifact.exists():
            artifact.write_text("# round review\n", encoding="utf-8")
        values = {
            "artifact_root": str(root),
            "task_id": "receipt-test",
            "round_id": "R1",
            "speaker": "claude",
            "verdict": "PASS",
            # Absolute + really-written: record-round now refuses relative paths
            # and non-existent artifacts, so the fixture must satisfy the same
            # contract a real round does.
            "artifact": str(root / "review.md"),
            "summary": "reviewed",
            "blocks_consensus": False,
            "resolves_round": [],
            "executor_evidence": True,
            "timeout": 1,
            "lines": 40,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_round_receipt_exists_before_prompt_and_is_finalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "identity-gate.json").write_text(json.dumps({
                "status": "PASS",
                "executor": "surface:2",
                "executor_provider": "claude",
            }))

            def assert_receipt_before_send(_executor, prompt, marker=None):
                receipts = list((root / "round-receipts").glob("R1-*.json"))
                self.assertEqual(len(receipts), 1)
                pending = json.loads(receipts[0].read_text())
                self.assertEqual(pending["status"], "AWAITING_EXECUTOR_ACK")
                body, pin = reference.read_body(pending['dispatch_body_reference'])
                self.assertEqual(reference.validate_wire_body(prompt), pin)
                self.assertEqual(pending['dispatch_payload_sha256'], reference.digest(prompt))
                self.assertIn('ROUND_NONCE=' + marker, body)
                self.assertIn('ROUND_ACK|<task-id>|<round-id>|<agent:identity>|<verdict>|<nonce>', body)
                self.assertLessEqual(len(prompt.encode('utf-8')), reference.MAX_INLINE_BYTES)

            evidence = {"executor_nonce_found": True, "screen_hash": "abc123"}
            with (
                mock.patch.object(HARNESS.cmux, "pin_workspace"),
                mock.patch.object(HARNESS.cmux, "submit_text", side_effect=assert_receipt_before_send),
                mock.patch.object(HARNESS.cmux, "capture_round_evidence", return_value=evidence),
            ):
                HARNESS.cmd_record_round(self.args(root))

            receipts = list((root / "round-receipts").glob("R1-*.json"))
            final = json.loads(receipts[0].read_text())
            self.assertEqual(final["status"], "PASS")
            self.assertTrue(final["executor_ack"])
            self.assertIsNotNone(final["dispatch_started_at"])
            self.assertIsNotNone(final["dispatch_submitted_at"])
            self.assertIsNotNone(final["evidence_observed_at"])
            self.assertIsNotNone(final["parser_confirmed_at"])
            rounds = json.loads((root / "rounds.json").read_text())
            self.assertTrue(rounds["rounds"][0]["executor_evidence"]["nonce_proven"])

    def test_handshake_receipt_exists_before_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "identity-gate.json").write_text(json.dumps({
                "status": "PASS",
                "executor": "surface:2",
                "supervisor": "surface:1",
                "workspace_uuid": "ws-uuid",
                "supervisor_surface_uuid": "sup-uuid",
                "executor_surface_uuid": "uuid-2",
                "executor_provider": "claude",
            }))
            # Fresh handshakes no longer touch the composer with a probe.

            def assert_receipt_before_send(_executor, prompt, marker=None):
                receipt = json.loads((root / "handshake-receipt.json").read_text())
                # Persist a nonterminal wait before sending, never a false FAIL.
                self.assertEqual(receipt["status"], "AWAITING_EXECUTOR_ACK")
                self.assertEqual(receipt["lifecycle"], "PENDING")
                self.assertNotIn("error", receipt)
                self.assertEqual(receipt["executors"][0]["status"], "HELLO_SENT")
                self.assertFalse(receipt["prompt_contains_literal_ack"])
                item = receipt['executors'][0]
                self.assertEqual(item['bridge_probe_status'], 'NOT_RUN')
                self.assertIsNone(item['bridge_clear_confirmed'])
                self.assertIsNone(item['bridge_clear_recovery'])
                body, pin = reference.read_body(item['dispatch_body_reference'])
                self.assertEqual(reference.validate_wire_body(prompt), pin)
                self.assertEqual(item['dispatch_payload_sha256'], reference.digest(prompt))
                self.assertEqual(item['dispatch_confirmation_scope'], 'reference_notice')
                self.assertLessEqual(len(prompt.encode('utf-8')), reference.MAX_INLINE_BYTES)
                self.assertIn('ACK_NONCE=' + marker, body)
                self.assertIn("entire assistant response must be exactly one compact ACK line", body)
                self.assertNotIn(receipt["ack_line_expected"], body)

            with (
                mock.patch.object(HARNESS.cmux, "pin_workspace"),
                mock.patch.object(HARNESS.cmux, "submit_text", side_effect=assert_receipt_before_send),
                mock.patch.object(
                    HARNESS.cmux,
                    "wait_for_ack",
                    return_value="PREFLIGHT_ACK|receipt-test|claude:identity|READY|INLINE|nonce",
                ),
                mock.patch.object(HARNESS.secrets, "token_hex", return_value="nonce"),
            ):
                HARNESS.cmd_handshake(self.args(root))

            receipt = json.loads((root / "handshake-receipt.json").read_text())
            self.assertIsNotNone(receipt["dispatch_started_at"])
            self.assertIsNotNone(receipt["dispatch_submitted_at"])
            self.assertIsNotNone(receipt["ack_observed_at"])
            self.assertIsNotNone(receipt["parser_confirmed_at"])
            self.assertFalse((root / "bridge-test-evidence.json").exists())

    def test_handshake_uncertain_dispatch_polls_same_nonce_without_resend(self):
        for state in (BRIDGE.DELIVERY_UNVERIFIED_BY_DETECTOR,
                      BRIDGE.DELIVERY_QUEUED_AT_RECEIVER,
                      BRIDGE.SUPERVISOR_DID_NOT_SUBMIT):
            for acknowledge in (True, False):
                with self.subTest(state=state, acknowledge=acknowledge), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "identity-gate.json").write_text(json.dumps({
                        "status": "PASS", "executor": "surface:2",
                        "supervisor": "surface:1", "workspace_uuid": "ws-uuid",
                        "supervisor_surface_uuid": "sup-uuid",
                        "executor_surface_uuid": "uuid-2", "executor_provider": "claude",
                    }))
                    (root / "bridge-test-evidence.json").write_text(json.dumps({
                        "task_id": "receipt-test", "executor": "surface:2",
                        "clear_confirmed": True,
                    }))
                    expected = "PREFLIGHT_ACK|receipt-test|claude:identity|READY|INLINE|nonce"
                    with (
                        mock.patch.object(BRIDGE, "pin_workspace"),
                        mock.patch.object(HARNESS.secrets, "token_hex", return_value="nonce"),
                        mock.patch.object(BRIDGE, "submit_text", side_effect=BRIDGE.DispatchUnconfirmed("original detector failure", state=state)) as send,
                        mock.patch.object(BRIDGE, "wait_for_ack", return_value=expected,
                                          side_effect=None if acknowledge else TimeoutError("no matching ACK")) as wait,
                    ):
                        try:
                            HARNESS.cmd_handshake(self.args(root))
                        except SystemExit as exc:
                            self.assertEqual(exc.code, 1)
                    send.assert_called_once()
                    r = json.loads((root / "handshake-receipt.json").read_text())["executors"][0]
                    self.assertEqual(r["submission_state"], state)
                    self.assertEqual(r["dispatch_error"], "original detector failure")
                    self.assertIsNone(r["dispatch_submitted_at"])
                    if state == BRIDGE.SUPERVISOR_DID_NOT_SUBMIT:
                        wait.assert_not_called()
                        self.assertEqual(r["status"], "FAIL")
                    else:
                        wait.assert_called_once()
                        self.assertEqual(wait.call_args.kwargs["nonce"], "nonce")
                        self.assertEqual(wait.call_args.kwargs["task_id"], "receipt-test")
                        self.assertEqual(wait.call_args.kwargs["provider"], "claude")
                        self.assertEqual(wait.call_args.kwargs["timeout"], 1)
                        self.assertEqual(r["status"], "PASS" if acknowledge else "FAIL")
                        self.assertEqual(r["late_ack_recovered"], acknowledge)
                        self.assertFalse(r["attributable_to_executor"])

    def test_later_round_can_explicitly_resolve_prior_blocker(self):
        rounds = [
            {"round_id": "R1", "blocks_consensus": True, "resolves_rounds": []},
            {"round_id": "R2", "blocks_consensus": False, "resolves_rounds": ["R1"]},
        ]
        self.assertEqual(HARNESS._unresolved_blockers(rounds), [])

        unresolved = [
            {"round_id": "R1", "blocks_consensus": True, "resolves_rounds": []},
            {"round_id": "R2", "blocks_consensus": False, "resolves_rounds": []},
        ]
        self.assertEqual(
            [entry["round_id"] for entry in HARNESS._unresolved_blockers(unresolved)],
            ["R1"],
        )

    def test_phase_timeout_defaults_and_explicit_override(self):
        defaults = SimpleNamespace(timeout=None, handshake_timeout=600, round_timeout=180)
        self.assertEqual(HARNESS._effective_timeout(defaults, "handshake"), 600)
        self.assertEqual(HARNESS._effective_timeout(defaults, "round"), 180)
        override = SimpleNamespace(timeout=7, handshake_timeout=600, round_timeout=180)
        self.assertEqual(HARNESS._effective_timeout(override, "handshake"), 7)
        self.assertEqual(HARNESS._effective_timeout(override, "round"), 7)


class SubmissionConfirmationTests(unittest.TestCase):
    def test_completed_spinner_summary_is_not_active_input(self):
        self.assertFalse(BRIDGE._queued_or_active_input("✻ Churned for 7m 19s\n❯"))

    def test_live_spinner_or_queued_message_is_active_input(self):
        self.assertTrue(BRIDGE._queued_or_active_input("✳ Recording r6 evidence (1m 1s)"))
        self.assertTrue(BRIDGE._queued_or_active_input("❯ prompt\nPress up to edit queued messages"))

    def test_prompt_parser_distinguishes_history_from_pending_compose(self):
        self.assertFalse(
            BRIDGE._prompt_block_pending("❯ [delivery:x]\n⏺ response", "delivery:x")
        )
        self.assertTrue(
            BRIDGE._prompt_block_pending("❯ [delivery:x]\nTASK: work", "delivery:x")
        )
        self.assertFalse(
            BRIDGE._submission_confirmed("❯ [delivery:x]\nold prose", "delivery:x")
        )
        self.assertTrue(
            BRIDGE._submission_confirmed("❯ [delivery:x]\n✻ running", "delivery:x")
        )
        self.assertTrue(
            BRIDGE._submission_confirmed("❯ [delivery:x]\n• Edited file", "delivery:x")
        )
        self.assertFalse(
            BRIDGE._submission_confirmed("❯ [delivery:x]\n• ordinary prose", "delivery:x")
        )

    def test_submit_text_confirms_without_retry_when_assistant_started(self):
        text = "delivery:x"
        idle = "❯ Ask Claude to do anything\n[Opus 5]"
        done = "❯ delivery:x\n⏺ response\n" + idle
        with (
            LegacyTerminalFixture(provider="claude") as native,
            mock.patch.object(BRIDGE, "send_text"),
            mock.patch.object(BRIDGE, "send_key", side_effect=native.receipt_on_key(text)) as send_key,
            mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                idle, native.draft(text, "claude"), done)),
            mock.patch.object(BRIDGE.time, "sleep"),
        ):
            result = BRIDGE.submit_text("surface:2", text, marker="delivery:x")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["retries"], 0)
        send_key.assert_called_once_with("surface:2", "enter")

    def test_legacy_multiline_pending_recovery_budget_and_exact_native_receipt(self):
        text = "delivery:x\nstatus: work"
        idle = "❯ Ask Claude to do anything\n[Opus 5]"
        with LegacyTerminalFixture(provider="claude") as native:
            draft = native.draft(text, "claude")
            screens = native.ready_screens(idle, draft, draft)
            with (
                mock.patch.object(BRIDGE, "send_text") as paste,
                mock.patch.object(BRIDGE, "send_key") as send_key,
                mock.patch.object(BRIDGE, "read_screen", side_effect=screens),
            ):
                # Create a legacy attempt with its genuine fixture fence and
                # one original key, as before the new single-line paste gate.
                # All recovery and read-only checks below use current guards.
                with mock.patch("cmux_prompt_reference.require_inline"), self.assertRaises(BRIDGE.DispatchUnconfirmed):
                    BRIDGE.submit_text("surface:2", text, marker="delivery:x")
                paste.assert_called_once_with("surface:2", text)
                send_key.assert_called_once_with("surface:2", "enter")
                attempts = list((native.home / '.local/state/multi-agent-collaboration/'
                                 'message-dispatch-v1').glob('*/attempt-*.json'))
                self.assertEqual(len(attempts), 1)
                receipt = attempts[0].parent / 'receipt.json'
                self.assertFalse(receipt.exists())
                # 原次只有一次共享补键；未确认也不得重新取得预算。
                with self.assertRaises(BRIDGE.DispatchUnconfirmed):
                    BRIDGE.submit_text("surface:2", text, marker="delivery:x", recover_stranded=True)
                with self.assertRaisesRegex(BRIDGE.TaskPackContractError, 'RECOVERY_ALREADY_USED'):
                    BRIDGE.submit_text("surface:2", text, marker="delivery:x", recover_stranded=True)
                self.assertEqual(send_key.call_count, 2)
                native.append_user(text + ' ')
                with self.assertRaises(BRIDGE.DispatchUnconfirmed):
                    BRIDGE.submit_text("surface:2", text, marker="delivery:x", reconcile_only=True)
                self.assertFalse(receipt.exists())
                native.append_user(text)
                result = BRIDGE.submit_text("surface:2", text, marker="delivery:x", reconcile_only=True)
                self.assertEqual(result['attempt'], str(attempts[0]))
                self.assertTrue(result['reconciled_read_only'])
                self.assertEqual(len(list(attempts[0].parent.glob('attempt-*.json'))), 1)
                paste.assert_called_once_with("surface:2", text)
        self.assertTrue(result["confirmed"])
        self.assertEqual([call.args for call in send_key.call_args_list], [
            ("surface:2", "enter"),
            ("surface:2", "enter"),
        ])

    def test_submit_text_refuses_blind_retry_for_queued_input(self):
        text = "delivery:x"
        idle = "❯ Ask Claude to do anything\n[Opus 5]"
        queued = "❯ delivery:x\nPress up to edit queued messages"
        with (
            LegacyTerminalFixture(provider="claude") as native,
            mock.patch.object(BRIDGE, "send_text"),
            mock.patch.object(BRIDGE, "send_key", side_effect=lambda *_: native.append_queued(text)) as send_key,
            mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                idle, native.draft(text, "claude"), queued)),
            mock.patch.object(BRIDGE.time, "sleep"),
            self.assertRaises(BRIDGE.DispatchUnconfirmed),
        ):
            BRIDGE.submit_text("surface:2", text, marker="delivery:x")
        send_key.assert_called_once_with("surface:2", "enter")

    def test_submit_text_fails_closed_when_marker_disappears(self):
        screen = "⏺ unrelated response"
        with (
            LegacyTerminalFixture(provider="claude") as native,
            mock.patch.object(BRIDGE, "send_text"),
            mock.patch.object(BRIDGE, "send_key") as key,
            mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                screen + "\n❯ Ask Claude to do anything\n[Opus 5]",
                native.draft("delivery:x", "claude"), screen)),
            mock.patch.object(BRIDGE.time, "sleep"),
            self.assertRaises(BRIDGE.DispatchUnconfirmed),
        ):
            BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        key.assert_called_once_with("surface:2", "enter")

    def test_submit_text_rejects_new_activity_when_marker_scrolled_off(self):
        screens = [
            "⏺ previous response\n❯ Ask Claude to do anything\n[Opus 5]",
            "⏺ previous response\n⏺ new tool running",
        ]
        with (
            LegacyTerminalFixture(provider="claude") as native,
            mock.patch.object(BRIDGE, "send_text"),
            mock.patch.object(BRIDGE, "send_key") as send_key,
            mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                screens[0], native.draft("delivery:x", "claude"), screens[-1])),
            mock.patch.object(BRIDGE.time, "sleep"),
        ):
            with self.assertRaises(BRIDGE.DispatchUnconfirmed):
                BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        send_key.assert_called_once_with("surface:2", "enter")

    def test_submit_text_rejects_codex_tool_activity_when_marker_scrolled_off(self):
        screens = [
            "• previous tool\n› Ask Codex to do anything\nGPT-6 high",
            "• previous tool\n• Edited file",
        ]
        with (
            LegacyTerminalFixture() as native,
            mock.patch.object(BRIDGE, "send_text"),
            mock.patch.object(BRIDGE, "send_key") as send_key,
            mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                screens[0], native.draft("delivery:x"), screens[-1])),
            mock.patch.object(BRIDGE.time, "sleep"),
        ):
            with self.assertRaises(BRIDGE.DispatchUnconfirmed):
                BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        send_key.assert_called_once_with("surface:2", "enter")

    def test_submit_text_does_not_accept_non_activity_screen_change(self):
        screens = [
            "⏺ previous response\n❯ Ask Claude to do anything\n[Opus 5]",
            "⏺ previous response\nstatus changed",
            "⏺ previous response\nstatus changed",
        ]
        with (
            LegacyTerminalFixture(provider="claude") as native,
            mock.patch.object(BRIDGE, "send_text"),
            mock.patch.object(BRIDGE, "send_key") as key,
            mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                screens[0], native.draft("delivery:x", "claude"), screens[-1])),
            mock.patch.object(BRIDGE.time, "sleep"),
            self.assertRaises(BRIDGE.DispatchUnconfirmed),
        ):
            BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        key.assert_called_once_with("surface:2", "enter")

    def test_late_confirmation_observes_without_resubmitting(self):
        idle = "⏺ previous response\n❯ Ask Claude to do anything\n[Opus 5]"
        done = "❯ delivery:x\n⏺ new tool running\n❯ Ask Claude to do anything\n[Opus 5]"
        with LegacyTerminalFixture(provider="claude") as native:
            def delayed_user():
                native.append_user("delivery:x")
                return done

            with (mock.patch.object(BRIDGE, "send_text") as paste,
                  mock.patch.object(BRIDGE, "send_key") as key,
                  mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                      idle, native.draft("delivery:x", "claude"), idle, delayed_user)) as read):
                result = BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["retries"], 0)
        self.assertEqual(read.call_count, 5)
        paste.assert_called_once_with("surface:2", "delivery:x")
        key.assert_called_once_with("surface:2", "enter")

    def test_late_queued_message_is_not_reported_as_confirmed(self):
        idle = "⏺ previous response\n❯ Ask Claude to do anything\n[Opus 5]"
        queued = "Messages to be submitted after the tool completes:\ndelivery:x"
        with LegacyTerminalFixture(provider="claude") as native:
            def delayed_queue():
                native.append_queued("delivery:x")
                return queued

            with (mock.patch.object(BRIDGE, "send_text"),
                  mock.patch.object(BRIDGE, "send_key") as key,
                  mock.patch.object(BRIDGE, "read_screen", side_effect=native.ready_screens(
                      idle, native.draft("delivery:x", "claude"), idle, delayed_queue)),
                  self.assertRaises(BRIDGE.DispatchUnconfirmed) as error):
                BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        self.assertEqual(error.exception.state, BRIDGE.DELIVERY_QUEUED_AT_RECEIVER)
        key.assert_called_once_with("surface:2", "enter")

    def test_submit_text_fails_closed_on_historical_echo_without_new_activity(self):
        with (
            LegacyTerminalFixture(provider="claude"),
            mock.patch.object(BRIDGE, "send_text") as paste,
            mock.patch.object(BRIDGE, "send_key") as key,
            mock.patch.object(BRIDGE, "read_screen", return_value="❯ delivery:x\nold prose\n❯ Ask Claude to do anything\n[Opus 5]"),
            mock.patch.object(BRIDGE.time, "sleep"),
            self.assertRaisesRegex(BRIDGE.TaskPackContractError, "MESSAGE_MARKER_ALREADY_VISIBLE"),
        ):
            BRIDGE.submit_text("surface:2", "delivery:x", marker="delivery:x")
        paste.assert_not_called()
        key.assert_not_called()


class ExecutorReuseTests(unittest.TestCase):
    def test_spawn_requires_explicit_user_authorization_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(
                artifact_root=str(root),
                task_id="reuse-test",
                supervisor="codex",
                executor="claude",
                executor_surface="",
                spawn=True,
                spawn_authorized=False,
                direction="right",
                cwd="",
            )
            caller = {
                "surface_ref": "surface:1",
                "workspace_ref": "workspace:1",
                "pane_ref": "pane:1",
                "window_ref": "window:1",
            }
            with (
                mock.patch.object(HARNESS.cmux, "whoami", return_value=caller),
                mock.patch.object(HARNESS.cmux, "list_surfaces", return_value=[]),
                mock.patch.object(HARNESS.cmux, "spawn_executor") as spawn_executor,
                self.assertRaises(SystemExit),
            ):
                HARNESS.cmd_identity_gate(args)

            spawn_executor.assert_not_called()
            gate = json.loads((root / "identity-gate.json").read_text())
            self.assertEqual(gate["reason"], "SPAWN_REQUIRES_EXPLICIT_AUTHORIZATION")

    def test_spawn_authorized_flag_is_invalid_without_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(
                artifact_root=str(root),
                task_id="reuse-test",
                supervisor="codex",
                executor="claude",
                executor_surface="",
                spawn=False,
                spawn_authorized=True,
                direction="right",
                cwd="",
            )
            caller = {
                "surface_ref": "surface:1",
                "workspace_ref": "workspace:1",
                "pane_ref": "pane:1",
                "window_ref": "window:1",
            }
            with (
                mock.patch.object(HARNESS.cmux, "whoami", return_value=caller),
                mock.patch.object(HARNESS.cmux, "spawn_executor") as spawn_executor,
                self.assertRaises(SystemExit),
            ):
                HARNESS.cmd_identity_gate(args)

            spawn_executor.assert_not_called()
            gate = json.loads((root / "identity-gate.json").read_text())
            self.assertEqual(gate["reason"], "SPAWN_REQUIRES_BOTH_FLAGS")


class StrictScreenEvidenceTests(unittest.TestCase):
    def test_handshake_prompt_echo_is_not_an_ack(self):
        expected = "PREFLIGHT_ACK|task-x|claude:identity|READY|INLINE|abc123"
        screen = (
            "⏺ prior assistant text\n"
            "❯ construct this response: " + expected + "\n"
        )
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            with self.assertRaises(TimeoutError):
                BRIDGE.wait_for_ack(
                    "surface:2", timeout=0.01, poll=0.001, lines=20,
                    task_id="task-x", provider="claude", nonce="abc123",
                )

    def test_exact_handshake_in_assistant_block_passes(self):
        expected = "PREFLIGHT_ACK|task-x|claude:identity|READY|INLINE|abc123"
        screen = "⏺ " + expected + "\n"
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            actual = BRIDGE.wait_for_ack(
                "surface:2", timeout=0.01, poll=0.001, lines=20,
                task_id="task-x", provider="claude", nonce="abc123",
            )
        self.assertEqual(actual, expected)

    def test_long_handshake_response_keeps_structural_assistant_boundary(self):
        expected = "PREFLIGHT_ACK|task-x|claude:identity|READY|INLINE|abc123"
        prose = "\n".join("  verification detail %d" % i for i in range(24))
        screen = "⏺ verified receipt\n" + prose + "\n  " + expected + "\n"
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            actual = BRIDGE.wait_for_ack(
                "surface:2", timeout=0.01, poll=0.001, lines=80,
                task_id="task-x", provider="claude", nonce="abc123",
            )
        self.assertEqual(actual, expected)

    def test_quoted_prompt_symbol_inside_assistant_prose_is_not_boundary(self):
        expected = "PREFLIGHT_ACK|task-x|claude:identity|READY|INLINE|abc123"
        screen = (
            "⏺ verified receipt\n"
            "  the ❯ symbol quoted in prose is not a prompt marker\n"
            "  " + expected + "\n"
        )
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            actual = BRIDGE.wait_for_ack(
                "surface:2", timeout=0.01, poll=0.001, lines=40,
                task_id="task-x", provider="claude", nonce="abc123",
            )
        self.assertEqual(actual, expected)

    def test_round_refusal_quoting_nonce_fails(self):
        expected = "ROUND_ACK|task-x|R1|claude:identity|PASS|abc123"
        screen = "⏺ I refuse this request; nonce abc123 was quoted.\n"
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            evidence = BRIDGE.capture_round_evidence(
                "surface:2", "abc123", provider="claude", lines=20,
                expected_ack=expected,
            )
        self.assertFalse(evidence["executor_nonce_found"])

    def test_exact_round_ack_in_assistant_block_passes(self):
        expected = "ROUND_ACK|task-x|R1|claude:identity|PASS|abc123"
        screen = "⏺ " + expected + "\n"
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            evidence = BRIDGE.capture_round_evidence(
                "surface:2", "abc123", provider="claude", lines=20,
                expected_ack=expected,
            )
        self.assertTrue(evidence["executor_nonce_found"])

    def test_long_round_response_keeps_structural_assistant_boundary(self):
        expected = "ROUND_ACK|task-x|R1|claude:identity|PASS|abc123"
        prose = "\n".join("  review detail %d" % i for i in range(24))
        screen = "⏺ reviewed artifact\n" + prose + "\n  " + expected + "\n"
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            evidence = BRIDGE.capture_round_evidence(
                "surface:2", "abc123", provider="claude", lines=80,
                expected_ack=expected,
            )
        self.assertTrue(evidence["executor_nonce_found"])


class ConsensusRoundGuardTests(unittest.TestCase):
    @staticmethod
    def write_root(root: Path, rounds: list[dict]) -> None:
        (root / "validation.json").write_text(json.dumps({
            "task_id": "guard-test",
            "status": "PASS",
            "checks": {"handshake_strict": {"pass": True}},
        }))
        (root / "rounds.json").write_text(json.dumps({
            "task_id": "guard-test",
            "minimum_required_rounds": 3,
            "rounds": rounds,
        }))

    def test_explicit_resolution_clears_historical_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_root(root, [
                {"round_id": "R1", "speaker": "supervisor",
                 "verdict": "PROPOSED", "blocks_consensus": False,
                 "resolves_rounds": []},
                {"round_id": "R2", "speaker": "executor",
                 "verdict": "FAIL", "blocks_consensus": True,
                 "resolves_rounds": []},
                {"round_id": "R3", "speaker": "executor",
                 "verdict": "PASS", "blocks_consensus": False,
                 "resolves_rounds": ["R2"]},
            ])
            ok, message = ROUND_GUARD.consensus_is_valid(root, "guard-test")
        self.assertTrue(ok, message)

    def test_unresolved_historical_blocker_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_root(root, [
                {"round_id": "R1", "speaker": "supervisor",
                 "verdict": "PROPOSED", "blocks_consensus": False,
                 "resolves_rounds": []},
                {"round_id": "R2", "speaker": "executor",
                 "verdict": "FAIL", "blocks_consensus": True,
                 "resolves_rounds": []},
                {"round_id": "R3", "speaker": "executor",
                 "verdict": "PASS", "blocks_consensus": False,
                 "resolves_rounds": []},
            ])
            ok, message = ROUND_GUARD.consensus_is_valid(root, "guard-test")
        self.assertFalse(ok)
        self.assertIn("1 consensus blocker", message)


if __name__ == "__main__":
    unittest.main()
