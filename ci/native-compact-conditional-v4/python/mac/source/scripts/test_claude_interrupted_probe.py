"""原生中断链 + 未提交探针回归；临时原生日志真实读写，终端仅用内存替身。"""
import copy
import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_claude_interruption as interruption
import mac_harness as harness
from test_claude_clear_hint_footer_20261009 import captured
from test_claude_footer_parser_20261008 import with_draft, footer_variant
from test_native_independent import NativeCase, PID, TARGET, SESSION, EPOCH, stamp

TOKEN = "B1_01234567"


def screen(body=""):
    original = captured("b")
    parsed = bridge._claude_bordered_compose(original)
    rows = original.splitlines()[parsed["start"] - 1:]
    base = "  ⎿ Interrupted · What should Claude do instead?\n" + "\n".join(rows)
    base = footer_variant(base, lambda footer: footer.replace(
        "✓ Bash ×13", "◐ Bash: stale command | ✓ Bash ×13"))
    return with_draft(base, body)


class InterruptedCase(NativeCase):
    def __enter__(self):
        self.children = {}
        self.listing_extra = ""
        self.rows = [
            dict(type="assistant", uuid="call", message=dict(role="assistant", id="message",
                content=[dict(type="tool_use", name="Bash", id="tool", input={"command": "old"})])),
            dict(type="user", uuid="result", parentUuid="call", sourceToolAssistantUUID="call",
                toolUseResult="User rejected tool use", message=dict(role="user", content=[
                    dict(type="tool_result", tool_use_id="tool", is_error=True,
                         content=interruption.REJECTED)])),
            dict(type="user", uuid="end", parentUuid="result", interruptedMessageId="message",
                message=dict(role="user", content=[dict(type="text", text=interruption.INTERRUPTION)])),
        ]
        super().__enter__()
        self.bridge._claude_bordered_compose = bridge._claude_bordered_compose
        self.bridge._CLAUDE_ACTIVE_TOOL_RE = bridge._CLAUDE_ACTIVE_TOOL_RE
        self.write_chain()
        return self

    def write_chain(self):
        self.create_transcript(SESSION)
        for i, row in enumerate(self.rows):
            self.append(dict(row, sessionId=SESSION, isSidechain=False,
                             timestamp=stamp(EPOCH - 3 + i)))

    def _ps(self, args, **kwargs):
        if args == ["/bin/ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,pgid="]:
            rows = [f"{PID} 1 {PID}"]
            rows.extend(f"{pid} {p['ppid']} {p.get('pgid', PID)}"
                        for pid, p in self.children.items())
            return subprocess.CompletedProcess(args, 0,
                stdout="\n".join(rows) + "\n" + self.listing_extra, stderr="")
        return super()._ps(args, **kwargs)

    def _process(self, pid, **kwargs):
        if pid in self.children:
            return copy.deepcopy(self.children[pid])
        return super()._process(pid, **kwargs)

    def add_child(self, *, epoch=EPOCH - 60, parent=PID):
        child = copy.deepcopy(self.process)
        child.update(pid=PID + 1, ppid=parent, birth=[epoch, 0], executable="/test/bin/mcp")
        self.children[PID + 1] = child

    def boundary(self, observed=None, token=TOKEN):
        return interruption.InterruptedBashBoundary(
            self.bridge, TARGET, screen() if observed is None else observed, token)


class BoundaryTests(unittest.TestCase):
    def test_exact_chain_with_preexisting_mcp_allows_only_own_token(self):
        with InterruptedCase() as case:
            case.add_child()
            bound = case.boundary()
            for body in (TOKEN, TOKEN[:4], ""):
                self.assertTrue(bound.check(screen(body)))
            self.assertEqual(bound.evidence["scope"], "NON_SUBMITTING_OWN_TOKEN_ONLY")
            self.assertEqual(len(bound.evidence["preexisting_processes"]), 1)
            self.assertFalse(bound.check(screen("foreign prompt")))
            self.assertFalse(bound.check(screen()))
            self.assertIn("FOREIGN_DRAFT", bound.evidence["failure"])

    def test_interrupted_hud_disappearing_does_not_skip_native_checks(self):
        with InterruptedCase() as case:
            bound = case.boundary()
            idle = screen().replace("◐ Bash: stale command | ", "")
            self.assertTrue(bound.check(idle))
            case.append(case.user_record())
            self.assertFalse(bound.check(idle))

    def test_malformed_rejection_or_parent_chain_is_rejected(self):
        mutations = [
            lambda rows: rows[1]["message"]["content"][0].update(is_error=1),
            lambda rows: rows[1]["message"]["content"][0].update(is_error=False),
            lambda rows: rows[1]["message"]["content"][0].update(tool_use_id="other"),
            lambda rows: rows[1].update(toolUseResult="success"),
            lambda rows: rows[1].update(sourceToolAssistantUUID="other"),
            lambda rows: rows[2].update(parentUuid="other"),
            lambda rows: rows[2].update(interruptedMessageId="other"),
            lambda rows: rows[2].update(uuid="call"),
            lambda rows: rows[0]["message"]["content"][0].update(name="Read"),
            lambda rows: rows[2]["message"]["content"][0].update(text="generic interruption"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), InterruptedCase() as case:
                mutation(case.rows)
                case.write_chain()
                with self.assertRaises(interruption.native.NativeDeliveryError):
                    case.boundary()

    def test_sidechain_session_future_and_additional_record_are_rejected(self):
        for change in ({"isSidechain": True}, {"sessionId": "other"},
                       {"timestamp": stamp(EPOCH + 60)}):
            with self.subTest(change=change), InterruptedCase() as case:
                rows = [json.loads(r) for r in case.transcript.read_bytes().splitlines()]
                rows[-1].update(change)
                case.transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))
                with self.assertRaises(interruption.native.NativeDeliveryError):
                    case.boundary()
        with InterruptedCase() as case:
            case.append(case.user_record())
            with self.assertRaises(interruption.native.NativeDeliveryError):
                case.boundary()

    def test_record_changes_permanently_invalidate_existing_boundary(self):
        for operation in ("append", "truncate", "replace", "touch", "rewrite"):
            with self.subTest(operation=operation), InterruptedCase() as case:
                bound = case.boundary()
                old = case.transcript.read_bytes()
                if operation == "append":
                    case.append(case.user_record())
                elif operation == "truncate":
                    case.transcript.write_bytes(old[:-1])
                elif operation == "replace":
                    alternate = case.transcript.with_suffix(".new")
                    alternate.write_bytes(old)
                    alternate.replace(case.transcript)
                elif operation == "touch":
                    s = case.transcript.stat()
                    os.utime(case.transcript, ns=(s.st_atime_ns, s.st_mtime_ns + 1000000))
                else:
                    case.transcript.write_bytes(old.replace(b'"name":"Bash"', b'"name":"Read"'))
                self.assertFalse(bound.check(screen()))
                self.assertIsNotNone(bound.evidence["failure"])

    def test_identity_or_process_tree_drift_refuses(self):
        for change in ("client_birth", "new_child", "child_exit", "child_birth"):
            with self.subTest(change=change), InterruptedCase() as case:
                case.add_child()
                bound = case.boundary()
                if change == "client_birth":
                    case.process["birth"][1] += 1
                elif change == "new_child":
                    case.children[PID + 2] = dict(case.children[PID + 1], pid=PID + 2)
                elif change == "child_exit":
                    case.children.clear()
                else:
                    case.children[PID + 1]["birth"][1] += 1
                self.assertFalse(bound.check(screen()))

    def test_current_or_reparented_same_group_tool_blocks_probe(self):
        for parent in (PID, 1):
            with self.subTest(parent=parent), InterruptedCase() as case:
                case.add_child(epoch=EPOCH - 2, parent=parent)
                with self.assertRaisesRegex(interruption.native.NativeDeliveryError,
                                            "TOOL_PROCESS_STILL_PRESENT"):
                    case.boundary()

    def test_invalid_process_listing_fails_closed(self):
        with InterruptedCase() as case:
            case.listing_extra = "bad listing\n"
            with self.assertRaisesRegex(interruption.native.NativeDeliveryError,
                                        "PROCESS_LIST_INVALID"):
                case.boundary()

    def test_busy_or_queued_or_unknown_or_foreign_editor_never_binds(self):
        observed = [
            screen(TOKEN), screen(" "),
            screen().replace("Interrupted · What should Claude do instead?",
                             "Working… (3s · 20 tokens)"),
            "Messages to be submitted after current response\n" + screen(),
            "Press up to edit queued messages\n" + screen(),
            screen().replace("◐ Bash:", "◐ Read:"),
            "unknown layout",
        ]
        for s in observed:
            with self.subTest(screen=s[-100:]), InterruptedCase() as case:
                with self.assertRaises(interruption.native.NativeDeliveryError):
                    case.boundary(s)
                self.assertEqual(case.process_calls, [])

    def test_only_generated_short_probe_token_is_authorized(self):
        for token in ("DONE text", TOKEN + "\n", "B0_01234567", "B1_ABCDEFGH"):
            with self.subTest(token=token), InterruptedCase() as case:
                with self.assertRaisesRegex(interruption.native.NativeDeliveryError,
                                            "INVALID_PROBE_TOKEN"):
                    case.boundary(token=token)
                self.assertEqual(case.process_calls, [])

    def test_global_busy_and_cleanup_guards_remain_conservative(self):
        self.assertTrue(bridge._queued_or_active_input(screen()))
        with self.assertRaises(bridge.DispatchUnconfirmed):
            bridge.require_clearable_agent_input(screen(), TARGET)


class HarnessTests(unittest.TestCase):
    def run_probe(self, case, *, race=None, force=False):
        state = dict(body="", sent=False, raced=False)

        def read(*args, **kwargs):
            if state["sent"] and not state["raced"] and race in ("foreign", "append"):
                state["raced"] = True
                if race == "foreign":
                    state["body"] = "human draft"
                else:
                    case.append(case.user_record())
            return screen(state["body"])

        def paste(target, text):
            self.assertEqual(target, TARGET)
            self.assertEqual(text, TOKEN)
            state.update(body=text, sent=True)

        def key(target, name):
            self.assertEqual(target, TARGET)
            self.assertIn(name, ("end", "backspace"))
            if name == "backspace":
                state["body"] = state["body"][:-1]
                if race == "mid_clear" and not state["raced"]:
                    state["raced"] = True
                    case.append(case.user_record())

        with (
            patch.object(harness.cmux, "pin_workspace", side_effect=case.bridge.pin_workspace),
            patch.object(harness.cmux, "_run", side_effect=case.bridge._run),
            patch.object(harness.cmux, "read_screen", side_effect=read),
            patch.object(harness.cmux, "send_text", side_effect=paste) as send,
            patch.object(harness.cmux, "send_key", side_effect=key) as keys,
            patch.object(harness.time, "sleep"),
        ):
            evidence, ok = harness._bridge_test_one(
                SimpleNamespace(task_id="offline-interrupted", lines=200, force_compose=force),
                TARGET, TOKEN, 1)
        return evidence, ok, send, keys

    def test_native_interruption_permits_non_submitting_probe_and_verified_cleanup(self):
        with InterruptedCase() as case:
            ev, ok, send, keys = self.run_probe(case)
            self.assertTrue(ok)
            send.assert_called_once_with(TARGET, TOKEN)
            self.assertEqual([c.args[1] for c in keys.call_args_list],
                             ["end"] + ["backspace"] * len(TOKEN))
            self.assertTrue(ev["clear_confirmed"])
            self.assertEqual(ev["clear_key_count"], len(TOKEN))
            self.assertIsNone(ev["interrupted_bash_boundary"]["failure"])

    def test_changed_native_or_foreign_draft_causes_zero_cleanup_keys(self):
        for race in ("foreign", "append"):
            with self.subTest(race=race), InterruptedCase() as case:
                ev, ok, send, keys = self.run_probe(case, race=race)
                self.assertFalse(ok)
                send.assert_called_once()
                keys.assert_not_called()
                self.assertFalse(ev["clear_confirmed"])

    def test_each_cleanup_key_rechecks_native_interruption(self):
        with InterruptedCase() as case:
            ev, ok, send, keys = self.run_probe(case, race="mid_clear")
            self.assertFalse(ok)
            self.assertEqual([c.args[1] for c in keys.call_args_list], ["end", "backspace"])
            self.assertEqual(ev["clear_key_count"], 1)

    def test_force_compose_does_not_authorize_interrupted_exception(self):
        with InterruptedCase() as case:
            ev, ok, send, keys = self.run_probe(case, force=True)
            self.assertFalse(ok)
            send.assert_not_called()
            keys.assert_not_called()
            self.assertIsNone(ev["interrupted_bash_boundary"])

    def test_nonterminal_native_state_is_zero_input_refusal(self):
        with InterruptedCase() as case:
            case.append(case.user_record())
            ev, ok, send, keys = self.run_probe(case)
            self.assertFalse(ok)
            send.assert_not_called()
            keys.assert_not_called()
            self.assertIn("INTERRUPTED_PROBE_", ev["interrupted_bash_refusal"])


if __name__ == "__main__":
    unittest.main()
