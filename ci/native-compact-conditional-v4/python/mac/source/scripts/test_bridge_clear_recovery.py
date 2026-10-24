"""Offline regression for a captured empty-editor/running-footer shape.

The public fixture is a synthetic reduction; original screen evidence stays private.

No test uses a live surface or modifies an original attempt. Recovery tests drive
the command/handshake entrypoints with mocked transport, preserving the failed
bridge bytes and requiring a fresh authenticated empty/idle observation.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_r3_hardening as r3

H = r3.HARNESS
B = H.cmux
FIXTURE = (Path(__file__).resolve().parents[1] / "verification" / "fixtures" /
           "claude-running-tool-footer-20261008.txt")
ACTIVE = FIXTURE.read_text(encoding="utf-8")
FIXTURE_SHA = "6c5ac4ef7b359b5f4b1cddd29ffcd2f1746a8f9a017ae2e5328d5ec0453bcb9b"
_lines = ACTIVE.splitlines()
_prompt = max(i for i, line in enumerate(_lines) if B._PROMPT_GLYPH_RE.match(line))
EDITOR = "\n".join(_lines[_prompt - 1:]) + "\n"
# 夹具独立声明活动行，避免用被测 parser 自己决定何为 idle 正例。
_RUNNING_ROW = "  ◐ Bash: .../example... | ✓ Bash ×19"
IDLE = "\n".join(line for line in EDITOR.splitlines()
                 if line != _RUNNING_ROW) + "\n"


def setUpModule():
    # 复用相同的临时 registry / 外部进程夹具，不触达 live harness 状态。
    r3.setUpModule()


def draft(text, screen=IDLE):
    lines = screen.splitlines()
    at = max(i for i, line in enumerate(lines) if B._PROMPT_GLYPH_RE.match(line))
    lines[at:at + 1] = ["❯ " + text]
    return "\n".join(lines) + "\n"


class ComposeBoundaryTests(unittest.TestCase):
    def test_minimal_captured_shape_is_empty_but_active(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA)
        self.assertEqual(B.receiver_input_kind(ACTIVE), "AGENT_TUI")
        self.assertEqual(B.compose_rendered_text(ACTIVE), "")
        self.assertTrue(B.compose_block_is_empty(ACTIVE))
        self.assertTrue(B._queued_or_active_input(ACTIVE))
        self.assertFalse(H._empty_idle_agent_screen(ACTIVE))

    def test_same_editor_after_tool_finishes_is_empty_idle(self):
        self.assertTrue(H._empty_idle_agent_screen(IDLE))
        self.assertFalse(B._queued_or_active_input(IDLE))

    def test_real_first_line_and_multiline_drafts_are_preserved(self):
        for text in ("user draft", "first line\n  second line", "B1_89abcdef",
                     "/context", "continue"):
            with self.subTest(text=text):
                screen = draft(text)
                self.assertEqual(B.compose_rendered_text(screen), text)
                self.assertFalse(B.compose_block_is_empty(screen))
                self.assertFalse(H._empty_idle_agent_screen(screen))

    def test_footer_like_draft_inside_borders_is_not_discarded(self):
        for text in ("\n  ✓ Bash ×19", "\n  上下文 15%",
                     "\n  [claude-opus-5-5[1M]]", "\n  ◐ Bash: my actual draft",
                     "\n  2 CLAUDE.md | 9 MCPs | 7 钩子"):
            with self.subTest(text=text):
                screen = draft(text)
                self.assertEqual(B.compose_rendered_text(screen), text.strip())
                self.assertFalse(B.compose_block_is_empty(screen))
                self.assertFalse(H._empty_idle_agent_screen(screen))

    def test_unknown_footer_and_missing_borders_do_not_whitelist_running_line(self):
        lines = EDITOR.splitlines()
        for screen in (EDITOR + "  unknown footer row\n",
                       "\n".join(lines[1:]),
                       "\n".join(lines[:2] + lines[3:])):
            with self.subTest(screen=screen):
                self.assertFalse(B.compose_block_is_empty(screen))
                self.assertFalse(H._empty_idle_agent_screen(screen))

    def test_queued_input_and_shell_remain_refused(self):
        for screen in ("Messages to be submitted after current tool\n" + IDLE,
                       "Press up to edit queued messages\n" + IDLE,
                       IDLE + "zsh$ "):
            with self.subTest(screen=screen):
                self.assertFalse(H._empty_idle_agent_screen(screen))


class BridgeActivityTests(r3.OfflineWorkspaceFixture):
    def run_probe(self, screens, *, sent, force=False, success=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r3.gate(root)
            with (
                mock.patch.object(B, "read_screen", side_effect=screens),
                mock.patch.object(B, "send_text") as paste,
                mock.patch.object(B, "send_key") as keys,
                mock.patch.object(B, "focus_surface"),
                mock.patch.object(B, "clear_known_compose_by_delete") as delete,
                mock.patch.object(H.time, "sleep"),
            ):
                if success:
                    H.cmd_bridge_test(r3.args(root, force_compose=force))
                else:
                    with self.assertRaises(SystemExit) as exc:
                        H.cmd_bridge_test(r3.args(root, force_compose=force))
                    self.assertEqual(exc.exception.code, 1)
            delete.assert_not_called()
            if sent:
                paste.assert_called_once_with("surface:2", "B1_01234567")
            else:
                paste.assert_not_called()
            evidence = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertIs(evidence["token_sent"], sent)
            self.assertIs(evidence["clear_confirmed"], success)
            return evidence, [call.args[1] for call in keys.call_args_list]

    def test_active_empty_or_occupied_pre_read_never_sends_even_with_override(self):
        for force in (False, True):
            for screen in (ACTIVE, draft("old user draft", EDITOR),
                           "Messages to be submitted after tool\n" + IDLE):
                with self.subTest(force=force, screen=screen):
                    ev, keys = self.run_probe([screen], sent=False, force=force)
                    self.assertEqual(keys, [])
                    self.assertEqual(ev["status"], "COMPOSE_OCCUPIED")
                    self.assertTrue(ev["active_or_queued_before_send"])

    def test_active_footer_prevents_owned_probe_deletion_or_clear_success(self):
        for screen in (EDITOR, draft("B1_01234567", EDITOR)):
            with self.subTest(screen=screen):
                ev, keys = self.run_probe([IDLE] + [screen] * 4, sent=True)
                self.assertEqual(keys, [])
                self.assertEqual(ev["clear_key_count"], 0)
                self.assertFalse(any(row["owned_token_prefix"]
                                     for row in ev["clear_observations"]))

    def test_active_to_idle_cleanup_recovers_only_after_observed_idle(self):
        ev, keys = self.run_probe([IDLE, EDITOR, IDLE], sent=True, success=True)
        self.assertEqual(keys, [])
        self.assertEqual(ev["clear_confirmed_by"], "EMPTY_COMPOSE")

    def test_active_after_override_clear_blocks_next_key_or_token(self):
        occupied = draft("authorized old draft")
        for screens, expected in (([occupied, EDITOR], ["escape"]),
                                  ([occupied, occupied, EDITOR], ["escape", "ctrl+u"])):
            with self.subTest(screens=screens):
                ev, keys = self.run_probe(screens, sent=False, force=True)
                self.assertEqual(keys, expected)
                self.assertTrue(ev["active_or_queued_before_send"])


class RecoveryTests(r3.OfflineWorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        r3.gate(self.root)
        self.original = self.root / "bridge-test-evidence.json"
        ev = {
            "task_id": "r3-test", "executor": "surface:2", "ordinal": 1,
            "token": "B1_01234567", "observed_in_screen": True,
            "clear_confirmed": False, "clear_attempts": 3, "clear_key_count": 0,
            "pre_read_performed": True, "compose_was_empty_before_send": True,
            "token_sent": True,
        }
        ev["executors"] = [copy.deepcopy(ev)]
        self.original.write_text(json.dumps(ev), encoding="utf-8")
        self.failed_bytes = self.original.read_bytes()
        self.receipt = self.root / "independent-clear-observation.json"
        self.args = r3.args(self.root, bridge_clear_recovery=str(self.receipt))
        for name in ("send_text", "send_key", "focus_surface",
                     "clear_known_compose_by_delete"):
            patcher = mock.patch.object(B, name)
            spy = patcher.start()
            self.addCleanup(patcher.stop)
            self.addCleanup(spy.assert_not_called)
        marker = mock.patch.object(H, "_touch_active_marker")
        marker.start()
        self.addCleanup(marker.stop)

    def observe(self, screen=IDLE, success=True):
        with mock.patch.object(B, "read_screen", return_value=screen) as read:
            if success:
                H.cmd_bridge_clear_observe(self.args)
            else:
                with self.assertRaises((SystemExit, RuntimeError)):
                    H.cmd_bridge_clear_observe(self.args)
        self.assertEqual(self.original.read_bytes(), self.failed_bytes)
        return read

    def handshake(self, screen=IDLE, success=False):
        with (
            mock.patch.object(B, "read_screen", return_value=screen),
            mock.patch.object(B, "submit_text", return_value={}) as submit,
            mock.patch.object(B, "wait_for_ack",
                              return_value="PREFLIGHT_ACK|r3-test|claude:identity|READY|INLINE|abcd"),
            mock.patch.object(H.secrets, "token_hex", return_value="abcd"),
        ):
            if success:
                H.cmd_handshake(self.args)
                submit.assert_called_once()
            else:
                with self.assertRaises((SystemExit, RuntimeError)):
                    H.cmd_handshake(self.args)
                submit.assert_not_called()
        self.assertEqual(self.original.read_bytes(), self.failed_bytes)
        return submit

    def test_observation_is_zero_input_and_preserves_original_failure(self):
        read = self.observe()
        read.assert_called_once_with("surface:2", lines=40)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["status"], "PASS")
        self.assertIs(receipt["terminal_input_sent"], False)
        self.assertEqual(receipt["original_bridge_sha256"],
                         hashlib.sha256(self.failed_bytes).hexdigest())
        self.assertFalse((self.root / "handshake-receipt.json").exists())
        self.assertFalse((self.root / "task-pack.json").exists())

    def test_active_or_occupied_observation_writes_refusal_only(self):
        for screen in (ACTIVE, draft("foreign draft"),
                       "Messages to be submitted after tool\n" + IDLE):
            with self.subTest(screen=screen):
                self.receipt = self.root / ("refused-" + str(len(list(self.root.iterdir()))) + ".json")
                self.args.bridge_clear_recovery = str(self.receipt)
                self.observe(screen, success=False)
                self.assertEqual(json.loads(self.receipt.read_text())["status"], "REFUSED")

    def test_original_native_identity_failure_stops_before_observation_write(self):
        with mock.patch.object(B, "pin_workspace", side_effect=RuntimeError("WORKSPACE_SCOPE_DENIED")):
            read = self.observe(success=False)
        read.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_identity_change_after_read_stops_before_observation_write(self):
        with mock.patch.object(B, "pin_workspace", side_effect=[{}, RuntimeError("WORKSPACE_SCOPE_DENIED")]):
            self.observe(success=False)
        self.assertFalse(self.receipt.exists())

    def test_original_binding_changed_during_read_refuses(self):
        def changed(*a, **kw):
            self.original.write_bytes(self.failed_bytes + b"\n")
            return IDLE
        with mock.patch.object(B, "read_screen", side_effect=changed):
            with self.assertRaises(SystemExit):
                H.cmd_bridge_clear_observe(self.args)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.original.read_bytes(), self.failed_bytes + b"\n")

    def test_existing_handshake_or_pack_prevents_recovery(self):
        for name in ("handshake-receipt.json", "task-pack.json"):
            with self.subTest(name=name):
                existing = self.root / name
                existing.write_text("{}")
                read = self.observe(success=False)
                read.assert_not_called()
                self.assertFalse(self.receipt.exists())
                self.handshake()
                existing.unlink()

    def test_existing_observation_is_never_overwritten(self):
        self.observe()
        before = self.receipt.read_bytes()
        read = self.observe(success=False)
        read.assert_not_called()
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_symlink_relative_or_original_receipt_paths_refuse(self):
        linked = self.root / "link.json"
        linked.symlink_to(self.root / "absent.json")
        for path in ("relative.json", str(self.original), str(linked),
                     str(self.root.parent / "outside.json")):
            with self.subTest(path=path):
                self.args.bridge_clear_recovery = path
                read = self.observe(success=False)
                read.assert_not_called()

    def test_missing_or_wrong_ownership_fields_refuse(self):
        baseline = json.loads(self.failed_bytes)
        for key, value in (("task_id", "another-task"), ("executor", "surface:3"),
                           ("clear_confirmed", True), ("pre_read_performed", False),
                           ("compose_was_empty_before_send", False), ("token_sent", False)):
            with self.subTest(key=key):
                ev = copy.deepcopy(baseline)
                ev[key] = value
                self.original.write_text(json.dumps(ev))
                with mock.patch.object(B, "read_screen") as read, self.assertRaises(SystemExit):
                    H.cmd_bridge_clear_observe(self.args)
                read.assert_not_called()
                self.assertFalse(self.receipt.exists())
        self.original.write_bytes(self.failed_bytes)

    def test_inconsistent_executor_leaf_and_force_override_refuse(self):
        ev = json.loads(self.failed_bytes)
        ev["executors"][0]["token_sent"] = False
        self.original.write_text(json.dumps(ev))
        with mock.patch.object(B, "read_screen") as read, self.assertRaises(SystemExit):
            H.cmd_bridge_clear_observe(self.args)
        read.assert_not_called()
        self.original.write_bytes(self.failed_bytes)
        self.args.force_compose = True
        self.observe(success=False)
        self.assertFalse(self.receipt.exists())

    def test_valid_recovery_allows_exactly_one_original_handshake(self):
        self.observe()
        frozen = self.receipt.read_bytes()
        self.handshake(success=True)
        result_path = self.root / "handshake-receipt.json"
        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["bridge_clear_recovery"], {
            "path": str(self.receipt), "sha256": hashlib.sha256(frozen).hexdigest()})
        result_bytes = result_path.read_bytes()
        self.handshake()
        self.assertEqual(result_path.read_bytes(), result_bytes)
        self.assertEqual(self.receipt.read_bytes(), frozen)

    def test_live_draft_activity_queue_or_unknown_blocks_handshake(self):
        self.observe()
        for screen in (ACTIVE, draft("new user text"),
                       "Press up to edit queued messages\n" + IDLE, "unknown receiver"):
            with self.subTest(screen=screen):
                self.handshake(screen)
                self.assertFalse((self.root / "handshake-receipt.json").exists())

    def test_identity_failure_before_handshake_preserves_saved_observation(self):
        self.observe()
        before = self.receipt.read_bytes()
        with mock.patch.object(B, "pin_workspace", side_effect=RuntimeError("WORKSPACE_SCOPE_DENIED")):
            self.handshake()
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_missing_malformed_non_object_or_refused_receipt_blocks_handshake(self):
        self.handshake()
        for raw in ('{"truncated":', "null", "[1]", '{"status":"REFUSED"}'):
            with self.subTest(raw=raw):
                self.receipt.write_text(raw)
                self.handshake()
                self.assertFalse((self.root / "handshake-receipt.json").exists())

    def test_saved_binding_screen_hash_and_status_tamper_refuse(self):
        self.observe()
        baseline = json.loads(self.receipt.read_text())
        for key, value in (("task_id", "other"), ("executor", "surface:3"),
                           ("original_bridge_sha256", "0" * 64),
                           ("identity_gate_sha256", "0" * 64),
                           ("screen_sha256", "0" * 64), ("status", "REFUSED"),
                           ("terminal_input_sent", True)):
            with self.subTest(key=key):
                mutated = dict(baseline)
                mutated[key] = value
                self.receipt.write_text(json.dumps(mutated))
                self.handshake()

    def test_saved_nonempty_or_active_screen_with_valid_hash_refuses(self):
        self.observe()
        baseline = json.loads(self.receipt.read_text())
        for screen in (ACTIVE, draft("foreign draft")):
            with self.subTest(screen=screen):
                mutated = dict(baseline, screen=screen,
                               screen_sha256=hashlib.sha256(screen.encode()).hexdigest())
                self.receipt.write_text(json.dumps(mutated))
                self.handshake()

    def test_original_or_gate_hash_change_invalidates_observation(self):
        self.observe()
        for path in (self.original, self.root / "identity-gate.json"):
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(before + b"\n")
                with mock.patch.object(B, "submit_text") as submit, self.assertRaises(SystemExit):
                    H.cmd_handshake(self.args)
                submit.assert_not_called()
                path.write_bytes(before)

    def test_receipt_changed_during_live_recheck_refuses(self):
        self.observe()
        def changed(*a, **kw):
            self.receipt.write_bytes(self.receipt.read_bytes() + b"\n")
            return IDLE
        with (
            mock.patch.object(B, "read_screen", side_effect=changed),
            mock.patch.object(B, "submit_text") as submit,
            self.assertRaises(SystemExit),
        ):
            H.cmd_handshake(self.args)
        submit.assert_not_called()
        self.assertFalse((self.root / "handshake-receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
