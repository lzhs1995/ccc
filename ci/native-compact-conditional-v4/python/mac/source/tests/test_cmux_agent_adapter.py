#!/usr/bin/env python3
"""Offline regressions through the rendered shell and actual adapter CLI.

Only transport/native verification dependencies are test doubles. No client
input, installation, live receipt or product acceptance is claimed by this suite.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from datetime import datetime, timezone

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from render_cmux_agent import render

BASELINE = SOURCE / "tests/fixtures/cmux-agent-legacy.sh"
REAL_SCRIPTS = SOURCE / "scripts"
BASELINE_SHA = "38d095d8065a72538cfa32c5e6efd3cc9308372f1d728b90aefd55580ab336b3"
WS = "11111111-1111-4111-8111-111111111111"
CALLER = "22222222-2222-4222-8222-222222222222"
A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def row(ref, surface, pane, workspace=WS, kind="terminal"):
    return dict(ref=ref, surface_id=surface, workspace_id=workspace, pane_ref="pane:" + pane,
                pane_id=pane.zfill(8) + "-1234-4234-8234-123456789012",
                surface_type=kind, selected=False)


class HelperEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cmux-helper-entrypoint-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        release = self.root / "fixed release/scripts"
        release.mkdir(parents=True)
        shutil.copy2(SOURCE / "scripts/cmux_agent_adapter.py", release)
        shutil.copy2(SOURCE / "scripts/cmux_evidence_io.py", release)
        shutil.copy2(SOURCE / "scripts/cmux_prompt_reference.py", release)
        for fixture in (SOURCE / "tests/fixtures").glob("*.py"):
            shutil.copy2(fixture, release)
        self.helper = self.root / "cmux-agent"
        self.helper.write_bytes(render(BASELINE.read_bytes(), expected_sha256=BASELINE_SHA,
            python=Path(sys.executable), adapter=release / "cmux_agent_adapter.py"))
        self.helper.chmod(0o755)
        self.data = dict(state="received", caller=dict(workspace_id=WS, surface_id=CALLER, pane_ref="pane:1"),
                         rows=[row("surface:1", CALLER, "1"), row("surface:2", A, "2"),
                               row("surface:3", B, "3")])
        self.save()

    def save(self):
        (self.root / "config.json").write_text(json.dumps(self.data))

    def call(self, *arguments):
        env = dict(os.environ, CMUX_HELPER_FIXTURE_ROOT=str(self.root), CMUX_BIN=shutil.which("true"))
        # Do not change HOME, CODEX_HOME or the user's installed state.
        result = subprocess.run([str(self.helper), *arguments], env=env, cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        return result

    def assert_exit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout if code == 0 else result.stderr)

    def events(self, op=None):
        path = self.root / "events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [item for item in events if op is None or item["op"] == op]

    def state_paths(self, pattern):
        return list((self.root / "state-home/.local/state/multi-agent-collaboration/cmux-agent-adapter-v1").glob(pattern))

    def test_unconfirmed_states_preserve_pending_and_refuse_success(self):
        for state in ("queued", "compose", "empty_compose", "fake_confirmed", "altered", "wrong_session"):
            with self.subTest(state=state):
                self.data["state"] = state
                self.save()
                result = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 75)
                self.assertFalse(result["confirmed"])
                self.assertEqual(len(self.state_paths("targets/*/pending.json")), 1)
        self.assertEqual(len(self.events("fixture_paste")), 1)
        self.assertEqual(len({e["marker"] for e in self.events("submit")}), 1)
        self.assertTrue(all(e["reconcile_only"] for e in self.events("submit")[1:]))

    def test_valid_receipt_success_repeated_request_has_zero_additional_input(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: exact bytes\n第二行"), 0)
        second = self.assert_exit(self.call("send", A.upper(), "STATUS: exact bytes\n第二行"), 0)
        self.assertEqual(first["marker"], second["marker"])
        self.assertTrue(second["already_received"])
        self.assertEqual(len(self.events("fixture_paste")), 1)
        self.assertEqual(len(self.events("submit")), 1)
        self.assertEqual(self.state_paths("targets/*/pending.json"), [])

    def test_original_queue_can_only_reconcile_read_only(self):
        self.data["state"] = "receipt_on_reconcile"
        self.save()
        self.assert_exit(self.call("ask", "surface:2", "STATUS: report state"), 75)
        intent = self.state_paths("targets/*/intents/*.json")[0]
        final = self.assert_exit(self.call("reconcile", "surface:2", "--intent", str(intent)), 0)
        self.assertTrue(final["reconciled_read_only"])
        submits = self.events("submit")
        self.assertEqual([s["reconcile_only"] for s in submits], [False, True])
        self.assertEqual(len(self.events("fixture_paste")), 1)
        self.assertEqual(submits[0]["payload"], submits[1]["payload"])

    def test_reconcile_without_intent_reports_path_and_keeps_pending(self):
        self.data["state"] = "receipt_on_reconcile"
        self.save()
        self.assert_exit(self.call("ask", "surface:2", "STATUS: exact original"), 75)
        intent = self.state_paths("targets/*/intents/*.json")[0]
        pending = self.state_paths("targets/*/pending.json")[0]
        before = (pending.stat().st_ino, pending.read_bytes(), self.events())
        result = self.assert_exit(self.call("reconcile", "surface:2"), 75)
        self.assertIn("HELPER_INTENT_REQUIRED", result["error"])
        self.assertEqual(Path(result["original_intent"]), intent)
        self.assertEqual((pending.stat().st_ino, pending.read_bytes(), self.events()), before)

    def test_explicit_intent_revalidates_after_pending_was_removed(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 0)
        self.assertEqual(self.state_paths("targets/*/pending.json"), [])
        final = self.assert_exit(self.call("reconcile", "surface:2", "--intent", first["request"]), 0)
        self.assertEqual(final["marker"], first["marker"])
        self.assertEqual(final["request"], first["request"])
        self.assertTrue(final["reconciled_read_only"])
        self.assertEqual(len(self.events("fixture_paste")), 1)
        self.assertEqual(len(self.events("submit")), 1)
        self.assertEqual(self.state_paths("targets/*/pending.json"), [])

    def test_foreign_target_intent_cannot_authorize_receipt(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 0)
        before = self.events()
        result = self.assert_exit(self.call("reconcile", "surface:3", "--intent", first["request"]), 75)
        self.assertIn("HELPER_INTENT_OUTSIDE_ORIGINAL_CHANNEL", result["error"])
        self.assertEqual(self.events(), before)

    def test_relative_copied_and_symlink_intents_are_not_original(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 0)
        intent = Path(first["request"])
        copied = self.root / intent.name
        shutil.copy2(intent, copied)
        alias = self.root / "alias.json"
        alias.symlink_to(intent)
        before = self.events()
        for path in (intent.name, str(copied), str(alias)):
            with self.subTest(path=path):
                result = self.assert_exit(self.call("reconcile", "surface:2", "--intent", path), 75)
                self.assertIn("HELPER_INTENT_OUTSIDE_ORIGINAL_CHANNEL", result["error"])
                self.assertEqual(self.events(), before)

    def test_pending_for_another_request_cannot_confirm_explicit_old_intent(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: prior"), 0)
        self.data["state"] = "queued"
        self.save()
        self.assert_exit(self.call("send", "surface:2", "STATUS: current"), 75)
        pending = self.state_paths("targets/*/pending.json")[0]
        before = (pending.read_bytes(), self.events())
        result = self.assert_exit(self.call("reconcile", "surface:2", "--intent", first["request"]), 75)
        self.assertIn("HELPER_PENDING_CHANGED", result["error"])
        self.assertEqual((pending.read_bytes(), self.events()), before)

    def test_hardlinked_intent_fails_the_same_guard_file_contract(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 0)
        intent = Path(first["request"])
        os.link(intent, self.root / "hardlink.json")
        before = self.events()
        result = self.assert_exit(self.call("reconcile", "surface:2", "--intent", str(intent)), 75)
        self.assertIn("HELPER_STATE_NOT_REGULAR", result["error"])
        self.assertEqual(self.events(), before)

    def test_changed_caller_cannot_reconcile_original_intent(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 0)
        self.data["caller"]["surface_id"] = "99999999-9999-4999-8999-999999999999"
        self.save()
        before = self.events()
        result = self.assert_exit(self.call("reconcile", "surface:2", "--intent", first["request"]), 75)
        self.assertIn("HELPER_REQUEST_CHANGED", result["error"])
        self.assertEqual(self.events(), before)

    def test_explicit_intent_must_already_exist(self):
        first = self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 0)
        intent = Path(first["request"])
        intent.unlink()
        before = self.events()
        self.assert_exit(self.call("reconcile", "surface:2", "--intent", str(intent)), 75)
        self.assertFalse(intent.exists())
        self.assertEqual(self.events(), before)

    def test_reconcile_without_original_pending_never_infers_delivery(self):
        result = self.assert_exit(self.call("reconcile", "surface:2"), 75)
        self.assertIn("HELPER_NO_PENDING", result["error"])
        self.assertEqual(self.events("submit"), [])

    def test_changed_message_cannot_replace_original_pending(self):
        self.data["state"] = "queued"
        self.save()
        self.assert_exit(self.call("ask", "surface:2", "STATUS: original"), 75)
        files = {str(p): p.read_bytes() for p in self.state_paths("targets/*/intents/*.json") + self.state_paths("targets/*/pending.json")}
        self.assert_exit(self.call("ask", "surface:2", "STATUS: different"), 75)
        self.assertEqual(len(self.events("submit")), 1)
        self.assertTrue(all(Path(p).read_bytes() == content for p, content in files.items()))

    def test_verified_other_caller_pending_is_diagnosed_without_adopting_or_bypassing(self):
        self.data['state'] = 'queued'
        self.save()
        self.assert_exit(self.call('ask', 'surface:2', 'STATUS: original supervisor'), 75)
        paths = self.state_paths('targets/*/intents/*.json') + self.state_paths('targets/*/pending.json')
        before = {p: (p.stat().st_ino, p.read_bytes()) for p in paths}
        self.data['caller']['surface_id'] = '99999999-9999-4999-8999-999999999999'
        self.save()
        operations = self.events()
        for arguments in [('ask', 'surface:2', 'STATUS: current supervisor'), ('reconcile', 'surface:2')]:
            result = self.assert_exit(self.call(*arguments), 75)
            self.assertIn('HELPER_PENDING_OTHER_CALLER', result['error'])
            self.assertEqual(result['details']['original_caller_surface_uuid'], CALLER)
            self.assertEqual(result['details']['input_operations'], 0)
            self.assertIn('successor-rebind-v2', result['recovery'])
            self.assertEqual(self.events(), operations)
            self.assertEqual({p: (p.stat().st_ino, p.read_bytes()) for p in paths}, before)

    def test_other_caller_diagnostic_requires_intact_original_intent(self):
        self.data['state'] = 'queued'
        self.save()
        self.assert_exit(self.call('ask', 'surface:2', 'STATUS: original supervisor'), 75)
        intent = self.state_paths('targets/*/intents/*.json')[0]
        value = json.loads(intent.read_text())
        value['payload'] += ' changed'
        intent.write_text(json.dumps(value))
        self.data['caller']['surface_id'] = '99999999-9999-4999-8999-999999999999'
        self.save()
        before = self.events()
        result = self.assert_exit(self.call('ask', 'surface:2', 'STATUS: new'), 75)
        self.assertNotIn('HELPER_PENDING_OTHER_CALLER', result['error'])
        self.assertIn('HELPER_REQUEST_CHANGED', result['error'])
        self.assertEqual(self.events(), before)

    def test_legacy_pending_is_preserved_and_never_replaced(self):
        directory = self.root / "state-home/.local/state/cmux-agent"
        directory.mkdir(parents=True)
        pending = directory / "surface_2.pending"
        pending.write_text("unknown original attempt")
        original = pending.read_bytes()
        result = self.assert_exit(self.call("send", "surface:2", "STATUS: message"), 75)
        self.assertIn("ORIGINAL_HELPER_CONTROLLER_REQUIRED", result["error"])
        self.assertEqual(pending.read_bytes(), original)
        self.assertEqual(self.events("submit"), [])

    def test_legacy_active_lock_cannot_be_taken_over(self):
        lock = self.root / "state-home/.local/state/cmux-agent/surface_2.lock"
        lock.mkdir(parents=True)
        self.assert_exit(self.call("ask", "surface:2", "STATUS: message"), 75)
        self.assertTrue(lock.is_dir())
        self.assertEqual(self.events("submit"), [])

    def test_intent_drift_after_original_attempt_refuses_reconciliation(self):
        self.data["state"] = "queued"
        self.save()
        self.assert_exit(self.call("send", "surface:2", "STATUS: original"), 75)
        path = self.state_paths("targets/*/intents/*.json")[0]
        value = json.loads(path.read_text())
        value["payload"] += " tampered"
        path.write_text(json.dumps(value))
        result = self.assert_exit(self.call("reconcile", "surface:2", "--intent", str(path)), 75)
        self.assertIn("HELPER_REQUEST_CHANGED", result["error"])
        self.assertEqual(len(self.events("submit")), 1)

    def test_lock_inode_replacement_refuses_success_even_with_receipt(self):
        self.data["mutate_on_submit"] = "lock"
        self.save()
        result = self.assert_exit(self.call("send", "surface:2", "STATUS: message"), 75)
        self.assertIn("HELPER_LOCK_CHANGED", result["error"])
        self.assertEqual(len(self.state_paths("targets/*/pending.json")), 1)

    def test_identity_change_during_initial_verifier_prevents_input(self):
        self.data["mutate_on_verify"] = "identity"
        self.save()
        self.assert_exit(self.call("send", "surface:2", "STATUS: message"), 75)
        self.assertEqual(self.events("submit"), [])

    def test_intent_change_during_initial_verifier_prevents_input(self):
        self.data["mutate_on_verify"] = "intent"
        self.save()
        result = self.assert_exit(self.call("send", "surface:2", "STATUS: message"), 75)
        self.assertIn("HELPER_STATE_CHANGED", result["error"])
        self.assertEqual(self.events("submit"), [])

    def test_pending_change_during_initial_verifier_prevents_input(self):
        self.data["mutate_on_verify"] = "pending"
        self.save()
        result = self.assert_exit(self.call("send", "surface:2", "STATUS: message"), 75)
        self.assertIn("HELPER_STATE_CHANGED", result["error"])
        self.assertEqual(self.events("submit"), [])

    def test_workspace_row_must_match_identity_proof(self):
        self.data["proof_override"] = dict(workspace_uuid="99999999-9999-4999-8999-999999999999")
        self.save()
        result = self.assert_exit(self.call("send", "surface:2", "STATUS: message"), 75)
        self.assertIn("HELPER_WORKSPACE_CHANGED", result["error"])
        self.assertEqual(self.events("submit"), [])

    def test_malformed_proof_never_becomes_success(self):
        self.data["malformed_proof"] = True
        self.save()
        result = self.assert_exit(self.call("ask", "surface:2", "STATUS: message"), 75)
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.events("submit"), [])

    def test_broadcast_original_workspace_terminals_including_same_pane_tab(self):
        self.data["rows"] += [row("surface:4", C, "4", workspace="99999999-9999-4999-8999-999999999999"),
                              row("surface:5", "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "1"),
                              row("surface:6", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "6", kind="browser")]
        self.save()
        first = self.assert_exit(self.call("broadcast", "STATUS: sync"), 0)
        self.assertEqual(len(first["recipients"]), 3)
        self.assertEqual({e["surface"] for e in self.events("fixture_paste")},
                         {A, B, "dddddddd-dddd-4ddd-8ddd-dddddddddddd"})
        self.data["rows"].append(row("surface:7", "ffffffff-ffff-4fff-8fff-ffffffffffff", "7"))
        self.data["rows"].reverse()
        for member in self.data["rows"]:
            for key in ("workspace_id", "surface_id", "pane_id"):
                member[key] = member[key].upper()
        self.save()
        second = self.assert_exit(self.call("broadcast", "STATUS: sync"), 0)
        self.assertEqual(len(second["recipients"]), 3)
        self.assertEqual(len(self.events("fixture_paste")), 3)
        self.assertEqual([x["marker"] for x in first["recipients"]], [x["marker"] for x in second["recipients"]])

    def test_broadcast_group_drift_prevents_first_input(self):
        self.data["mutate_on_verify"] = "group"
        self.save()
        result = self.assert_exit(self.call("broadcast", "STATUS: sync"), 75)
        self.assertIn("HELPER_BROADCAST_STATE_CHANGED", result["error"])
        self.assertEqual(self.events("submit"), [])

    def test_broadcast_group_lock_replacement_prevents_next_recipient(self):
        self.data["mutate_on_submit"] = "group_lock"
        self.save()
        result = self.assert_exit(self.call("broadcast", "STATUS: sync"), 75)
        self.assertIn("HELPER_LOCK_CHANGED", result["error"])
        self.assertEqual(len(self.events("fixture_paste")), 1)

    def test_broadcast_identity_change_cannot_redirect_next_recipient(self):
        self.data["mutate_on_submit"] = "identity"
        self.save()
        self.assert_exit(self.call("broadcast", "STATUS: sync"), 75)
        self.assertEqual(len(self.events("fixture_paste")), 1)

    def test_broadcast_rechecks_all_original_targets_before_new_input(self):
        self.assert_exit(self.call("broadcast", "STATUS: sync"), 0)
        self.data["rows"] = [r for r in self.data["rows"] if r["surface_id"] != B]
        self.save()
        self.assert_exit(self.call("broadcast", "STATUS: sync"), 75)
        self.assertEqual(len(self.events("submit")), 2)

    def test_real_task_pack_gate_distinguishes_ordinary_ask_from_task(self):
        self.assert_exit(self.call("ask", "surface:2", "STATUS: progress"), 0)
        ordinary = self.events("submit")[-1]
        refused = self.assert_exit(self.call("ask", "surface:3", "TASK: change source"), 75)
        self.assertIn("TASK_BOUND_TRANSPORT_REQUIRED", refused["error"])
        # The single-line reference planner rejects a formal task before bridge
        # submission. Do not accidentally reuse the previous ordinary event.
        self.assertEqual(len(self.events("submit")), 1)
        task = dict(surface=B, payload="TASK: TASK_GATE change source", marker="TASK_GATE")
        code = """import json,sys
sys.path.insert(0,sys.argv[1])
import cmux_bridge as bridge
import cmux_message_journal as journal
class IdentityBoundary(Exception): pass
def no_input(*args,**kwargs): raise IdentityBoundary()
bridge.pin_workspace=no_input
result=[]
for item in json.loads(sys.argv[2]):
 try:
  journal.deliver(bridge,item['surface'],item['payload'],item['marker'])
 except IdentityBoundary: result.append('ordinary_reaches_identity_boundary')
 except bridge.TaskPackContractError as exc: result.append(str(exc))
print(json.dumps(result))
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(REAL_SCRIPTS), json.dumps([ordinary, task])],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), ["ordinary_reaches_identity_boundary", "TASK_PACK_REQUIRED"])
        self.assertEqual(len(self.events("fixture_paste")), 1)

    def test_rendered_help_and_protocol_keep_non_sending_commands(self):
        result = self.call("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("self", "list", "read", "status", "clear-status", "log", "notify", "feed", "events", "start-codex", "protocol"):
            self.assertIn("cmux-agent " + name, result.stdout)
        result = self.call("protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Enter, an empty composer", result.stdout)
        self.assertEqual(self.events(), [])

    def test_renderer_requires_exact_baseline_and_retains_no_raw_sender(self):
        with self.assertRaisesRegex(ValueError, "HELPER_BASELINE_SHA_CHANGED"):
            render(BASELINE.read_bytes() + b"\n", expected_sha256=BASELINE_SHA,
                   python=Path(sys.executable), adapter=SOURCE / "scripts/cmux_agent_adapter.py")
        contents = self.helper.read_text()
        for value in ("RAW_UNVERIFIED", "SUBMITTED|QUEUED", "submit_text()", "canonical_surface()", "cmux send ", "cmux send-key "):
            self.assertNotIn(value, contents)
        self.assertIn(" -I -B ", contents)

    def test_rendered_entrypoint_preserves_virtual_environment(self):
        # 真实启动生成的 shell；只核解释器路由，不调用终端发送。
        runtime = self.root / "selected runtime"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
        python = runtime / "bin/python"
        probe = self.root / "runtime_probe.py"
        probe.write_text("import json,sys\nprint(json.dumps(dict(prefix=sys.prefix,executable=sys.executable)))\n")
        candidate = self.root / "venv-helper"
        candidate.write_bytes(render(BASELINE.read_bytes(), expected_sha256=BASELINE_SHA,
            python=python, adapter=probe))
        candidate.chmod(0o755)
        result = subprocess.run([str(candidate), "send", "surface:2", "STATUS: runtime probe"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(Path(observed["prefix"]).resolve(), runtime.resolve())
        # macOS 会将 /var 正规化为 /private/var；核解释器仍位于该 venv 的 bin。
        actual = Path(observed["executable"])
        self.assertEqual(actual.parent.resolve(), python.parent.resolve())
        self.assertEqual(actual.name, python.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--real-scripts", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    BASELINE, REAL_SCRIPTS = args.baseline, args.real_scripts
    files = [BASELINE, *sorted((SOURCE / "scripts").glob("*.py")),
             Path(__file__).resolve(), *sorted((SOURCE / "tests/fixtures").glob("*.py")),
             REAL_SCRIPTS / "cmux_bridge.py", REAL_SCRIPTS / "cmux_message_journal.py"]
    def pins():
        return {str(path): dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
                for path in files}
    before = pins()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HelperEntrypointTests)
    names = [test.id() for test in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = pins()
    report = dict(schema="cmux-helper-cli-offline-regression-v1", created_at=datetime.now(timezone.utc).isoformat(),
                  tests_run=result.testsRun, failures=[(str(test), trace) for test, trace in result.failures],
                  errors=[(str(test), trace) for test, trace in result.errors], test_methods=names,
                  passed=result.wasSuccessful() and before == after, inputs_unchanged=before == after,
                  inputs=before, native_receipts_test_doubles=True, live_input_operations=0,
                  live_client_execution_verified=False, installed=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    raise SystemExit(0 if report["passed"] else 1)
