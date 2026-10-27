#!/usr/bin/env python3
"""Wired tests for the R3 hardening mechanisms.

Task: multi-agent-skill-hardening-20260830

Every test here drives a real entry point — `cmd_bridge_test`, `cmd_handshake`,
`cmd_record_round`, `cmd_consensus_check`, or the bridge classifier — rather than
asserting on a helper in isolation. That distinction is the point: this task's
own inventory recorded a case where a helper suite passed while the wired gate
was broken, so a green helper test is not evidence that the gate works.

Each mechanism gets a positive control (legal input passes) and a poison case
(the defect this mechanism exists to catch is rejected). A mechanism with only a
positive control cannot distinguish "working" from "absent".
"""
from __future__ import annotations

import contextlib
import io
import json
import inspect
import shutil
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import cmux_bridge as BRIDGE  # noqa: E402
import mac_harness as HARNESS  # noqa: E402
import os  # noqa: E402
import cmux_consensus_stop_guard as STOP_GUARD  # noqa: E402
import cmux_consensus_round_guard as ROUND_GUARD  # noqa: E402
import cmux_handshake_receipt_guard as HANDSHAKE_GUARD  # noqa: E402
import cmux_lease_guard as LEASE_GUARD  # noqa: E402
import cmux_prompt_reference as REFERENCE  # noqa: E402
from native_test_support import NativeFixture  # noqa: E402


def setUpModule():
    # These fixtures use synthetic workspace identities. Never mix them with
    # the test runner's real managed-daemon ancestry. Native authentication has
    # its own dedicated positive/negative suite and live guard verification.
    import cmux_daemon_identity
    import offline_test_hook
    # 仅替换离线进程输入，真实 collect / collect_hook 与身份验证仍会执行。
    patcher = mock.patch.object(cmux_daemon_identity, "process",
                                side_effect=offline_test_hook.ordinary_process)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    registry = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(registry.cleanup)
    registry_patch = mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", Path(registry.name))
    registry_patch.start()
    unittest.addModuleCleanup(registry_patch.stop)
    body_patch = mock.patch.object(REFERENCE, "body_root",
                                  return_value=Path(registry.name) / "bodies")
    body_patch.start()
    unittest.addModuleCleanup(body_patch.stop)


def gate(root: Path, task_id="r3-test", executor="surface:2", supervisor="surface:1"):
    (root / "identity-gate.json").write_text(json.dumps({
        "task_id": task_id,
        "status": "PASS",
        "executor": executor,
        "supervisor": supervisor,
        "executor_provider": "claude",
        "workspace_ref": "workspace:10",
        "workspace_uuid": "ws-uuid",
        "supervisor_surface_uuid": "sup-uuid",
        "executor_surface_uuid": "uuid-2",
    }))


def bridge_evidence(root: Path, task_id="r3-test", executor="surface:2",
                    clear_confirmed=True):
    (root / "bridge-test-evidence.json").write_text(json.dumps({
        "task_id": task_id,
        "executor": executor,
        "token": f"BRIDGE_TEST_{task_id}",
        "observed_in_screen": True,
        "clear_confirmed": clear_confirmed,
        "clear_attempts": 1,
    }))


def args(root: Path, **over):
    values = {
        "artifact_root": str(root),
        "task_id": "r3-test",
        "round_id": "R1",
        "speaker": "claude",
        "verdict": "PASS",
        "artifact": str(root / "review.md"),
        "summary": "reviewed",
        "blocks_consensus": False,
        "resolves_round": [],
        "executor_evidence": True,
        "json": False,
        "timeout": None,
        "handshake_timeout": 600,
        "round_timeout": 180,
        "lines": 40,
    }
    values.update(over)
    return SimpleNamespace(**values)


def claude_editor(text=""):
    # 正例提供完整已知布局；不再用截断 shortcuts 行冒充空输入框。
    border = "─" * 40
    return f"{border}\n❯ {text}\n{border}\n[Opus 5]"


class OfflineWorkspaceFixture(unittest.TestCase):
    """These suites test delivery/receipts; workspace enforcement has its own
    wired tests in test_cmux_workspace_guard, without this transport mock."""
    def setUp(self):
        super().setUp()
        # Importing this fixture does not run this module's setUpModule.
        # Inheriting suites must also keep persisted handshake bodies offline.
        bodies = tempfile.TemporaryDirectory()
        self.addCleanup(bodies.cleanup)
        body_patch = mock.patch.object(REFERENCE, "body_root",
                                      return_value=Path(bodies.name) / "bodies")
        body_patch.start()
        self.addCleanup(body_patch.stop)
        def pin(surface, **kwargs):
            return {"workspace_uuid": "ws-uuid", "caller_surface_uuid": "sup-uuid",
                    "target_pane_uuid": "pane-executor",
                    "target_surface_uuid": "uuid-" + surface.split(":")[-1]}
        patcher = mock.patch.object(HARNESS.cmux, "pin_workspace", side_effect=pin)
        patcher.start()
        self.addCleanup(patcher.stop)
        mutex_home = tempfile.TemporaryDirectory()
        self.addCleanup(mutex_home.cleanup)
        import cmux_input_mutex
        mutex_patch = mock.patch.object(cmux_input_mutex, 'lock_paths', side_effect=lambda identity: (
            Path(mutex_home.name) / 'pane.lock',
            Path(mutex_home.name) / (identity['target_surface_uuid'] + '.lock')))
        mutex_patch.start()
        self.addCleanup(mutex_patch.stop)
        # Deterministic screen fixtures; production probes are short and random.
        token_patcher = mock.patch.object(
            HARNESS, "_bridge_test_token",
            side_effect=lambda task_id, ordinal: f"B{ordinal}_01234567",
        )
        token_patcher.start()
        self.addCleanup(token_patcher.stop)


class BridgeClearPostconditionTests(OfflineWorkspaceFixture):
    """ctrl+u is a request; clear_confirmed is the receipt.

    Measured incident: bridge-test left its token in compose at 11:45:33.009753Z,
    handshake started 1.003 ms later, read that token as executor input, and
    aborted with dispatch_submitted_at=None. The abort was then attributed to
    executor silence.
    """

    def test_clear_confirmed_when_compose_empties(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with (
                mock.patch.object(HARNESS.cmux, "send_text"),
                mock.patch.object(HARNESS.cmux, "send_key") as send_key,
                mock.patch.object(HARNESS.cmux, "read_screen",
                                  # 1st read is the ownership pre-read: the box
                                  # must be provably empty before anything is
                                  # typed. 2nd is post-paste, 3rd is post-clear.
                                  side_effect=[claude_editor(),
                                               claude_editor("B1_01234567"),
                                               claude_editor()]),
                mock.patch.object(HARNESS.time, "sleep"),
            ):
                HARNESS.cmd_bridge_test(args(root))
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertTrue(ev["clear_confirmed"])
            self.assertEqual(ev["clear_attempts"], 1)
            self.assertIsNotNone(ev["clear_verified_at"])
            self.assertTrue(ev["pre_read_performed"])
            self.assertTrue(ev["compose_was_empty_before_send"])
            self.assertTrue(ev["token_sent"])
            backspaces = [c for c in send_key.call_args_list if c.args[1] == "backspace"]
            self.assertEqual(len(backspaces), len("B1_01234567"))
            self.assertLess(len(backspaces), HARNESS.BRIDGE_TEST_CLEAR_DELETE_COUNT)

    def test_persistent_token_fails_closed_and_records_it(self):
        """POISON: the token never leaves compose."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            stuck = claude_editor("B1_01234567")
            with (
                mock.patch.object(HARNESS.cmux, "send_text"),
                mock.patch.object(HARNESS.cmux, "send_key"),
                # First read satisfies the ownership pre-read (empty box); every
                # read after that shows the token stuck in compose.
                mock.patch.object(HARNESS.cmux, "read_screen",
                                  side_effect=[claude_editor()] + [stuck] * 12),
                mock.patch.object(HARNESS.time, "sleep"),
                self.assertRaises(SystemExit) as exit_ctx,
            ):
                HARNESS.cmd_bridge_test(args(root))
            self.assertEqual(exit_ctx.exception.code, 1)
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertFalse(ev["clear_confirmed"])
            self.assertEqual(ev["clear_attempts"],
                             HARNESS.BRIDGE_TEST_CLEAR_MAX_ATTEMPTS)
            self.assertIsNone(ev["clear_verified_at"])


class PackEnumeratedLeaseTests(unittest.TestCase):
    """Leases cover ONLY paths the finalized pack enumerates.

    Motivating incident: mac_harness.py moved 834b6c73/L1931 -> 8201cd3d/L1944
    during an active task, and two agents independently recorded the same
    mutation from opposite sides without either being able to tell the other was
    mid-write. Narrow scope closes that without creating a global lock.
    """

    def _pack(self, root, paths, draft=False):
        HARNESS._write(root / "task-pack.json",
                       {"draft": draft, "source": [str(p) for p in paths]})

    def test_draft_pack_enumerates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "a.txt"; f.write_text("x")
            self._pack(root, [f], draft=True)
            self.assertEqual(HARNESS._pack_enumerated_paths(root), set())
            verdicts = HARNESS.lease_conflicts(root, "executor", [str(f)], "exclusive")
            self.assertIn(HARNESS.LEASE_SCOPE_VIOLATION,
                          [v["verdict"] for v in verdicts])

    def test_path_outside_pack_is_scope_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inside = root / "in.txt"; inside.write_text("x")
            outside = root / "out.txt"; outside.write_text("y")
            self._pack(root, [inside])
            self.assertEqual(
                HARNESS.lease_conflicts(root, "executor", [str(inside)], "exclusive"), [])
            verdicts = HARNESS.lease_conflicts(root, "executor", [str(outside)], "exclusive")
            self.assertIn(HARNESS.LEASE_SCOPE_VIOLATION,
                          [v["verdict"] for v in verdicts])

    def test_second_owner_conflicts_and_same_owner_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "shared.txt"; f.write_text("x")
            self._pack(root, [f])
            HARNESS.cmd_lease_acquire(SimpleNamespace(
                task_id="t", artifact_root=str(root), owner_role="executor",
                owner_surface="surface:58", path=[str(f)], shared=False,
                ttl_seconds=3600, reason=""))
            self.assertIn(HARNESS.LEASE_CONFLICT, [
                v["verdict"] for v in
                HARNESS.lease_conflicts(root, "supervisor", [str(f)], "exclusive")])
            self.assertEqual(
                HARNESS.lease_conflicts(root, "executor", [str(f)], "exclusive"), [])

    def test_expired_lease_is_stale_not_silently_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "old.txt"; f.write_text("x")
            self._pack(root, [f])
            past = datetime.now(timezone.utc) - timedelta(seconds=120)
            HARNESS._write(root / "leases" / "executor-expired.json", {
                "task_id": "t", "owner_role": "executor",
                "owner_surface": "surface:58", "paths": [str(f)],
                "mode": "exclusive",
                "created_at": (past - timedelta(seconds=60)).isoformat(),
                "expires_at": past.isoformat(),
                "source_list_sha256": HARNESS._lease_source_sha([str(f)]),
                "released_at": None,
            })
            live, stale = HARNESS._live_leases(root)
            self.assertEqual(len(live), 0)
            self.assertEqual(len(stale), 1)
            verdicts = HARNESS.lease_conflicts(root, "supervisor", [str(f)], "exclusive")
            self.assertIn(HARNESS.LEASE_STALE, [v["verdict"] for v in verdicts],
                          "an expired lease must surface as STALE, not vanish")

    def test_released_lease_stops_conflicting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "r.txt"; f.write_text("x")
            self._pack(root, [f])
            HARNESS.cmd_lease_acquire(SimpleNamespace(
                task_id="t", artifact_root=str(root), owner_role="executor",
                owner_surface="surface:58", path=[str(f)], shared=False,
                ttl_seconds=3600, reason=""))
            HARNESS.cmd_lease_release(SimpleNamespace(
                task_id="t", artifact_root=str(root), owner_role="executor"))
            self.assertEqual(
                HARNESS.lease_conflicts(root, "supervisor", [str(f)], "exclusive"), [])

    def test_shared_leases_do_not_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "s.txt"; f.write_text("x")
            self._pack(root, [f])
            HARNESS.cmd_lease_acquire(SimpleNamespace(
                task_id="t", artifact_root=str(root), owner_role="executor",
                owner_surface="surface:58", path=[str(f)], shared=True,
                ttl_seconds=3600, reason=""))
            self.assertEqual(
                HARNESS.lease_conflicts(root, "supervisor", [str(f)], "shared"), [])

    def test_source_sha_binds_the_path_list(self):
        """A lease cannot be widened after acquisition without changing identity."""
        a = HARNESS._lease_source_sha(["/x/one"])
        b = HARNESS._lease_source_sha(["/x/one", "/x/two"])
        self.assertNotEqual(a, b)
        self.assertEqual(a, HARNESS._lease_source_sha(["/x/one"]))


class LeaseHookExpiryTests(unittest.TestCase):
    """The mutation-boundary hook must expose stale records, not ignore them."""

    def test_expired_lease_is_stale_at_hook_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "declared.txt"
            target.write_text("before")
            (root / "task-pack.json").write_text(json.dumps({
                "draft": False, "source": [str(target)]
            }))
            expired = datetime.now(timezone.utc) - timedelta(seconds=5)
            (root / "leases").mkdir()
            (root / "leases" / "executor-expired.json").write_text(json.dumps({
                "task_id": "t",
                "owner_role": "executor",
                "owner_surface": "surface:58",
                "paths": [str(target)],
                "mode": "exclusive",
                "created_at": (expired - timedelta(seconds=60)).isoformat(),
                "expires_at": expired.isoformat(),
                "source_list_sha256": "0" * 64,
                "released_at": None,
            }))
            payload = {
                "tool_name": "bash",
                "tool_input": {
                    "command": (
                        f"printf x >> {target} --artifact-root {root} --task-id t"
                    )
                },
            }
            with mock.patch.dict(os.environ, {"CMUX_AGENT_ROLE": "supervisor"}, clear=False):
                ok, message = LEASE_GUARD.evaluate(payload)
            self.assertFalse(ok)
            self.assertIn("LEASE_STALE", message)

    def test_relative_root_fails_closed_at_hook_boundary(self):
        payload = {
            "tool_name": "bash",
            "tool_input": {
                "command": "printf x >> /tmp/declared.txt "
                           "--artifact-root relative-root --task-id t",
            },
        }
        ok, message = LEASE_GUARD.evaluate(payload)
        self.assertFalse(ok)
        self.assertIn("ARTIFACT_ROOT_NOT_ABSOLUTE", message)

    def test_armed_task_without_root_fails_closed_at_hook_boundary(self):
        active_dir = Path(tempfile.mkdtemp())
        try:
            payload = {
                "workspace_id": "workspace-test",
                "tool_name": "bash",
                "tool_input": {"command": "printf x >> /tmp/declared.txt"},
            }
            with mock.patch.object(LEASE_GUARD, "ACTIVE_DIR", active_dir), \
                 mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": "workspace-test",
                                             "CMUX_SURFACE_ID": "armed-surface"},
                                 clear=False):
                (active_dir / "workspace-test.json").write_text(json.dumps({
                    "task_id": "armed-without-root", "participants": [
                        {"role": "executor", "surface_uuid": "armed-surface"}]}))
                ok, message = LEASE_GUARD.evaluate(payload)
            self.assertFalse(ok)
            self.assertIn("ARTIFACT_ROOT_NOT_ABSOLUTE", message)
        finally:
            import shutil
            shutil.rmtree(active_dir, ignore_errors=True)

class AtomicWriteTests(unittest.TestCase):
    """Receipts are what a gate consults after a crash, so writes must be atomic.

    Plain write_text() leaves a window where the file exists but is truncated. A
    gate reading it would either raise (looking like a crash) or parse a partial
    object (looking like evidence).
    """

    def test_roundtrip_and_no_temp_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            HARNESS._write(d / "a.json", {"x": 1, "nested": {"y": [1, 2]}})
            self.assertEqual(HARNESS._read(d / "a.json"), {"x": 1, "nested": {"y": [1, 2]}})
            leftovers = [p.name for p in d.iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_replace_is_all_or_nothing(self):
        """A failed write must leave the previous bytes intact."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "r.json"
            HARNESS._write(target, {"generation": 1})
            before = target.read_bytes()
            with mock.patch.object(HARNESS.os, "replace",
                                   side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    HARNESS._write(target, {"generation": 2})
            self.assertEqual(target.read_bytes(), before,
                             "a failed write must not corrupt the existing artifact")
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [], "temp file must be cleaned up on failure")

    def test_truncated_artifact_is_unverifiable_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"truncated": ')
            got = HARNESS._read(bad)
            self.assertEqual(got["status"], HARNESS.STATUS_UNVERIFIABLE)
            self.assertFalse(got["pass"])
            self.assertIn("artifact", got)

    def test_absent_artifact_is_none_not_unverifiable(self):
        """'Never written' and 'written but unreadable' are different facts."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(HARNESS._read(Path(tmp) / "missing.json"))

    def test_unverifiable_never_reads_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "validation.json"
            bad.write_text("{not json at all")
            got = HARNESS._read(bad)
            self.assertNotEqual(got.get("status"), "PASS")
            self.assertIsNot(got.get("pass"), True)


class RoundBudgetAttributionTests(unittest.TestCase):
    """Delivery state and budget are orthogonal axes, and both must be recorded.

    Five round receipts in the preceding task carried None for every budget
    field. Three of those five had never been submitted at all, yet all five
    recorded executor-shaped errors, so a reader could not distinguish a 1.2s
    sender-side abort from a genuine 184s executor timeout.
    """

    def test_budget_source_distinguishes_override_from_default(self):
        self.assertEqual(
            HARNESS._budget_source(SimpleNamespace(timeout=None), "round"),
            "phase_round_default")
        self.assertEqual(
            HARNESS._budget_source(SimpleNamespace(timeout=30), "round"),
            "generic_timeout_override")
        self.assertEqual(
            HARNESS._budget_source(SimpleNamespace(timeout=None), "handshake"),
            "phase_handshake_default")

    def test_short_budget_is_not_attributable_to_executor(self):
        short = SimpleNamespace(timeout=30)
        eff = HARNESS._effective_timeout(short, "round")
        minimum = HARNESS.PHASE_MINIMUM_BUDGET_SECONDS["round"]
        self.assertLess(eff, minimum)
        # The rule the receipt encodes: below-minimum budget => not the executor's.
        self.assertFalse(not (eff < minimum))

    def test_phase_minimums_are_not_below_documented_floors(self):
        self.assertGreaterEqual(
            HARNESS.PHASE_MINIMUM_BUDGET_SECONDS["handshake"], 600)
        self.assertGreaterEqual(
            HARNESS.PHASE_MINIMUM_BUDGET_SECONDS["round"], 180)

    def test_five_delivery_states_exist_and_are_distinct(self):
        states = {
            BRIDGE.SUPERVISOR_DID_NOT_SUBMIT,
            BRIDGE.SUBMISSION_ABORTED_BUSY,
            BRIDGE.COMPOSE_OCCUPIED,
            BRIDGE.DELIVERY_UNVERIFIED_BY_DETECTOR,
            BRIDGE.DELIVERY_QUEUED_AT_RECEIVER,
        }
        self.assertEqual(len(states), 5,
                         "the five delivery causes must remain distinct values")


class ExecutorVerdictSplitTests(unittest.TestCase):
    """A nonce proves channel possession, never that a reviewer formed a view.

    Previously the expected ACK line was built from the supervisor's own
    --verdict, so only an echo could match and a pre-selected outcome was
    indistinguishable from an independent review.
    """

    def test_allowed_verdicts_include_disagreement(self):
        self.assertIn("FAIL", HARNESS.ROUND_ALLOWED_VERDICTS)
        self.assertIn("PASS_WITH_CHANGES", HARNESS.ROUND_ALLOWED_VERDICTS)

    def test_approving_set_excludes_pass_with_changes(self):
        """PASS_WITH_CHANGES must not silently close consensus."""
        self.assertNotIn("PASS_WITH_CHANGES", HARNESS.ROUND_APPROVING_VERDICTS)

    def test_bare_later_pass_does_not_supersede_earlier_nonapproving(self):
        rounds = [
            {"round_id": "A", "speaker": "claude:identity",
             "verdict": "PASS_WITH_CHANGES", "blocks_consensus": False,
             "resolves_rounds": []},
            {"round_id": "B", "speaker": "supervisor", "verdict": "PASS",
             "blocks_consensus": False, "resolves_rounds": []},
        ]
        unaccounted = HARNESS._unaccounted_nonapproving(rounds)
        self.assertEqual([r["round_id"] for r in unaccounted], ["A"])

    def test_explicit_resolves_rounds_accounts_for_it(self):
        rounds = [
            {"round_id": "A", "speaker": "claude:identity",
             "verdict": "PASS_WITH_CHANGES", "blocks_consensus": False,
             "resolves_rounds": []},
            {"round_id": "B", "speaker": "supervisor", "verdict": "PASS",
             "blocks_consensus": False, "resolves_rounds": ["A"]},
        ]
        self.assertEqual(HARNESS._unaccounted_nonapproving(rounds), [])

    def test_blocks_consensus_also_accounts_for_it(self):
        rounds = [
            {"round_id": "A", "speaker": "claude:identity",
             "verdict": "PASS_WITH_CHANGES", "blocks_consensus": True,
             "resolves_rounds": []},
        ]
        self.assertEqual(HARNESS._unaccounted_nonapproving(rounds), [])

    def test_capture_round_evidence_reports_expected_ack_explicitly(self):
        """nonce-possession must be distinguishable from a full verdict line."""
        ack = "ROUND_ACK|t|R1|claude:identity|FAIL|abcd1234"
        screen = f"⏺ {ack}\n"
        with mock.patch.object(BRIDGE, "read_screen", return_value=screen):
            ev = BRIDGE.capture_round_evidence(
                "surface:1", "abcd1234", provider="claude", expected_ack=ack)
        self.assertTrue(ev["expected_ack_found"])
        self.assertEqual(ev["expected_ack"], ack)
        with mock.patch.object(BRIDGE, "read_screen", return_value="⏺ abcd1234 only\n"):
            ev2 = BRIDGE.capture_round_evidence(
                "surface:1", "abcd1234", provider="claude", expected_ack=ack)
        self.assertFalse(ev2["expected_ack_found"])


class StopGuardPolarityTests(unittest.TestCase):
    """The turn-end gate must judge evidence, not vocabulary.

    Six honest message shapes were measured being blocked by the previous
    bare-substring matcher: the topic word alone, a truthful denial, an explicit
    retraction, a bug report quoting the guard's own filenames, the
    supervisor-assigned delivery marker, and a count of prior blocks. A lexical
    test standing in for a semantic property is both too strict and too
    permissive -- vagueness passed while precision was blocked.
    """

    def _marker(self, root, task_id="t-polarity", rounds=1,
                validation_pass=True, consensus_pass=False):
        if validation_pass:
            (root / "validation.json").write_text(json.dumps(
                {"task_id": task_id, "status": "PASS"}))
        if consensus_pass:
            (root / "consensus-validation.json").write_text(json.dumps(
                {"task_id": task_id, "status": "PASS"}))
        (root / "rounds.json").write_text(json.dumps({"rounds": [
            {"round_id": f"R{i}", "speaker": "claude:identity",
             "verdict": "PASS_WITH_CHANGES"} for i in range(1, rounds + 1)]}))
        return {"task_id": task_id, "artifact_root": str(root), "participants": [
            {"role": "supervisor", "surface_uuid": "polarity-supervisor"}]}

    def _allowed(self, guard, marker, text):
        """Drive the WIRED entry point, not internals.

        Calling _positive_evidence_claims/_contradicts_disk directly made these
        tests immune to mutations in evaluate(), which the non-vacuity matrix
        caught: two excisions stayed green because nothing under test ever
        reached the mutated line.
        """
        with mock.patch.object(guard, "_scope_candidates", return_value=[marker]), \
                mock.patch.object(guard.hook_identity, "resolve", return_value=("polarity-ws", "polarity-supervisor")), \
                mock.patch.object(guard, "_active_markers", return_value=[marker]):
            ok, _msg = guard.evaluate({"last_assistant_message": text})
        return ok

    # Honest shapes that MUST pass. The first four contain an evidence-shaped
    # claim *governed by a negation* -- without those, the polarity check would
    # have no jurisdiction over this fixture and excising it would look safe.
    HONEST = [
        "consensus-check is NOT PASS; the gate is still open.",
        "validation.json is not PASS for this task yet.",
        "It is not true that 3 of 3 rounds are recorded.",
        "I retract the earlier claim that consensus-check is PASS.",
        "Let us discuss consensus design later.",
        "There is no consensus. rounds.json is absent: zero of three.",
        "cmux_consensus_stop_guard.py and consensus-validation.json are buggy.",
        "STATUS=DONE COMMUNITY-CONSENSUS-R2-20260831-DONE delivered.",
        "Five blocked turn-ends across the three rounds now.",
        "Artifact written: consensus-plan.md sha256 f6a351da.",
    ]

    def test_honest_messages_are_not_blocked(self):
        guard = STOP_GUARD
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = self._marker(root)
            for text in self.HONEST:
                with self.subTest(text=text[:48]):
                    self.assertTrue(self._allowed(guard, marker, text),
                                    f"honest message was blocked: {text!r}")

    def test_false_round_count_is_blocked(self):
        guard = STOP_GUARD
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = self._marker(root, rounds=1)
            self.assertFalse(self._allowed(
                guard, marker, "All 3 of 3 rounds recorded and we are done."))

    def test_false_consensus_pass_is_blocked(self):
        guard = STOP_GUARD
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = self._marker(root, consensus_pass=False)
            self.assertFalse(self._allowed(
                guard, marker, "consensus-check is PASS for this task."))

    def test_true_claim_with_backing_is_allowed(self):
        guard = STOP_GUARD
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = self._marker(root, rounds=3, consensus_pass=True)
            self.assertTrue(self._allowed(
                guard, marker, "consensus-check is PASS and 3 of 3 rounds recorded."))

    def test_relative_marker_root_is_refused(self):
        marker = {"task_id": "t-relative", "artifact_root": "relative-root"}
        contradicted, message = STOP_GUARD._contradicts_disk(
            marker, ["consensus-check is PASS"])
        self.assertTrue(contradicted)
        self.assertIn("absolute artifact_root", message)


class RoundGuardRootResolutionTests(unittest.TestCase):
    """The round gate must never compose the artifact root from the caller's cwd.

    Measured defect: from inside the artifact tree the cwd-derived path doubled
    to <root>/handoff/multi-agent-artifacts/<TASK_ID>/handoff/... and the guard
    then reported validation.json as missing when it existed and read PASS. The
    verdict stayed BLOCK either way, so the harm was a false diagnosis rather
    than an unsafe allow.
    """

    def test_explicit_artifact_root_wins(self):
        tokens = ["python3", "mac_harness.py", "consensus-check",
                  "--task-id", "t-x", "--artifact-root", "/tmp/explicit-root"]
        self.assertEqual(ROUND_GUARD._artifact_root(tokens),
                         Path("/tmp/explicit-root"))

    def test_registry_is_used_when_no_explicit_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp)
            with mock.patch.object(ROUND_GUARD, "REGISTRY_DIR", reg):
                (reg / "t-y.json").write_text(json.dumps(
                    {"artifact_root": "/tmp/from-registry"}))
                tokens = ["python3", "mac_harness.py", "consensus-check",
                          "--task-id", "t-y"]
                self.assertEqual(ROUND_GUARD._artifact_root(tokens),
                                 Path("/tmp/from-registry"))

    def test_root_not_found_when_neither_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ROUND_GUARD, "REGISTRY_DIR", Path(tmp)):
                tokens = ["python3", "mac_harness.py", "consensus-check",
                          "--task-id", "t-absent"]
                self.assertIsNone(ROUND_GUARD._artifact_root(tokens))
                ok, msg = ROUND_GUARD.validate_command(
                    "python3 mac_harness.py consensus-check --task-id t-absent")
                self.assertFalse(ok)
                self.assertIn("ROOT_NOT_FOUND", msg)

    def test_verdict_is_cwd_invariant(self):
        """Same command, different cwd, identical message."""
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "reg"
            reg.mkdir()
            root = Path(tmp) / "artifacts"
            root.mkdir()
            (reg / "t-inv.json").write_text(json.dumps({"artifact_root": str(root)}))
            cmd = "python3 mac_harness.py consensus-check --task-id t-inv"
            seen = []
            with mock.patch.object(ROUND_GUARD, "REGISTRY_DIR", reg):
                for cwd in (Path(tmp), root, Path("/tmp")):
                    old = os.getcwd()
                    try:
                        os.chdir(cwd)
                        seen.append(ROUND_GUARD.validate_command(cmd))
                    finally:
                        os.chdir(old)
            self.assertEqual(len(set(seen)), 1,
                             f"verdict varied with cwd: {seen}")


class HarnessRootResolutionTests(unittest.TestCase):
    """The harness entry point must share the guards' cwd-invariant root rule."""

    def test_registered_root_wins_from_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry"
            registry.mkdir()
            root = Path(tmp) / "authoritative-artifacts"
            root.mkdir()
            (registry / "harness-root.json").write_text(json.dumps(
                {"artifact_root": str(root)}))
            old = Path.cwd()
            try:
                os.chdir("/tmp")
                with mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", registry):
                    resolved = HARNESS._artifact_root(SimpleNamespace(
                        artifact_root="", task_id="harness-root"))
            finally:
                os.chdir(old)
            self.assertEqual(resolved, root)

    def test_unregistered_root_fails_without_cwd_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry"
            registry.mkdir()
            old = Path.cwd()
            try:
                os.chdir(tmp)
                with mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", registry):
                    with self.assertRaises(SystemExit):
                        HARNESS._artifact_root(SimpleNamespace(
                            artifact_root="", task_id="never-registered"))
            finally:
                os.chdir(old)
            self.assertFalse(
                (Path(tmp) / "handoff" / "multi-agent-artifacts" /
                 "never-registered").exists())

    def test_relative_explicit_root_is_refused(self):
        tokens = ["python3", "mac_harness.py", "consensus-check",
                  "--task-id", "t-relative", "--artifact-root", "relative-root"]
        self.assertIsNone(ROUND_GUARD._artifact_root(tokens))
        self.assertIsNone(HANDSHAKE_GUARD._artifact_root(tokens))
        with tempfile.TemporaryDirectory() as tmp:
            old = Path.cwd()
            try:
                os.chdir(tmp)
                with self.assertRaises(SystemExit):
                    HARNESS._artifact_root(SimpleNamespace(
                        artifact_root="relative-root", task_id="t-relative"))
            finally:
                os.chdir(old)

    def test_relative_registry_root_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp)
            with mock.patch.object(ROUND_GUARD, "REGISTRY_DIR", reg), \
                 mock.patch.object(HANDSHAKE_GUARD, "REGISTRY_DIR", reg):
                (reg / "t-relative.json").write_text(json.dumps(
                    {"artifact_root": "relative-root"}))
                tokens = ["python3", "mac_harness.py", "consensus-check",
                          "--task-id", "t-relative"]
                self.assertIsNone(ROUND_GUARD._artifact_root(tokens))
                self.assertIsNone(HANDSHAKE_GUARD._artifact_root(tokens))

    def test_relative_lease_root_is_refused(self):
        payload = {"tool_input": {"command": "python3 mac_harness.py lease-check "
                                      "--task-id t-relative --artifact-root relative-root"}}
        tokens = ["python3", "mac_harness.py", "lease-check", "--task-id",
                  "t-relative", "--artifact-root", "relative-root"]
        self.assertEqual(LEASE_GUARD._artifact_roots(payload, tokens), [])
        allowed, message = LEASE_GUARD.evaluate(payload)
        self.assertFalse(allowed)
        self.assertIn("ARTIFACT_ROOT_NOT_ABSOLUTE", message)

    def test_active_marker_uses_atomic_writer(self):
        source = inspect.getsource(HARNESS.arm_task)
        self.assertIn('_write(_collab_dir(', source,
                      "arm_task must use _collab_dir for marker writes")
        self.assertIn('collaboration_id', source)
        self.assertIn('marker)', source)

    def test_registration_uses_task_id_not_directory_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry"
            root = Path(tmp) / "directory-name-does-not-match-task"
            with mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", registry):
                HARNESS._ensure_root(root, "task-identity")
                self.assertEqual(
                    HARNESS._registered_artifact_root("task-identity"), root)
                self.assertIsNone(
                    HARNESS._registered_artifact_root("directory-name-does-not-match-task"))

    def test_registration_refuses_rebinding_a_task_to_another_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry"
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            with mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", registry):
                HARNESS._ensure_root(first, "task-identity")
                with self.assertRaises(SystemExit):
                    HARNESS._ensure_root(second, "task-identity")
                self.assertEqual(
                    HARNESS._registered_artifact_root("task-identity"), first)


class BridgeOwnershipPreReadTests(OfflineWorkspaceFixture):
    """bridge-test must observe ownership of the compose line before typing.

    The clear step backspaces from the end, so it is only safe when the line
    holds nothing but bridge-test's own token. The previous version pasted first
    and read second, and justified the delete in a comment claiming the line was
    "otherwise empty" -- a premise nothing established. With unsubmitted user
    text present, the bounded backspace would consume it and still record
    clear_confirmed=true, because the postcondition only asks about the token.
    """

    OCCUPIED = "⏺ earlier\n\n❯ please refactor the parser, I was mid-sentence"

    def test_occupied_compose_refuses_without_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "send_key") as send_key,
                mock.patch.object(HARNESS.cmux, "read_screen",
                                  return_value=self.OCCUPIED),
                mock.patch.object(HARNESS.time, "sleep"),
                self.assertRaises(SystemExit) as exit_ctx,
            ):
                HARNESS.cmd_bridge_test(args(root))
            self.assertEqual(exit_ctx.exception.code, 1)
            # The load-bearing assertion: nothing was typed and no key was sent,
            # so the user's buffer is byte-unchanged by this run.
            send_text.assert_not_called()
            send_key.assert_not_called()
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertEqual(ev["status"], "COMPOSE_OCCUPIED")
            self.assertFalse(ev["clear_confirmed"])
            self.assertFalse(ev["compose_was_empty_before_send"])
            self.assertFalse(ev["token_sent"])
            self.assertTrue(ev["pre_read_performed"])

    def test_force_compose_uses_full_bounded_clear_chain(self):
        """Explicit override may clear one fingerprinted idle compose buffer."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            occupied = "❯ stale multiline prompt"
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "send_key") as send_key,
                mock.patch.object(HARNESS.cmux, "focus_surface"),
                mock.patch.object(
                    HARNESS.cmux,
                    "clear_known_compose_by_delete",
                    return_value=256,
                ) as bounded_delete,
                mock.patch.object(
                    HARNESS.cmux,
                    "read_screen",
                    side_effect=[
                        occupied, occupied, occupied, occupied, "❯ ",
                        "❯ B1_01234567", "❯ ",
                    ],
                ),
                mock.patch.object(HARNESS.time, "sleep"),
            ):
                HARNESS.cmd_bridge_test(args(root, force_compose=True))

            send_text.assert_called_once_with("surface:2", "B1_01234567")
            bounded_delete.assert_called_once_with("surface:2", "stale multiline prompt")
            self.assertEqual(
                [call.args[1] for call in send_key.call_args_list[:3]],
                ["escape", "ctrl+u", "ctrl+c"],
            )
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertTrue(ev["override"]["force_compose_override"])
            self.assertTrue(ev["override"]["clear_confirmed"])
            self.assertEqual(ev["override"]["delete_count"], 256)
            self.assertEqual(
                ev["override"]["clear_actions"],
                ["escape", "focused_ctrl+u", "ctrl+c", "end+bounded_backspace"],
            )

    def test_force_compose_directly_replaces_unknown_idle_virtual_suggestion(self):
        """User-authorized dispatch replaces an uneditable idle suggestion."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            occupied = "❯ product-owned suggestion not yet catalogued"
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "send_key"),
                mock.patch.object(HARNESS.cmux, "focus_surface"),
                mock.patch.object(
                    HARNESS.cmux,
                    "clear_known_compose_by_delete",
                    return_value=256,
                ),
                mock.patch.object(
                    HARNESS.cmux,
                    "read_screen",
                    side_effect=[
                        occupied, occupied, occupied, occupied, occupied,
                        "❯ B1_01234567", occupied,
                    ],
                ),
                mock.patch.object(HARNESS.time, "sleep"),
            ):
                HARNESS.cmd_bridge_test(args(root, force_compose=True))

            send_text.assert_called_once_with("surface:2", "B1_01234567")
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertTrue(ev["override"]["direct_replace_after_clear_attempts"])
            self.assertTrue(ev["clear_confirmed"])
            self.assertTrue(ev["token_sent"])

    def test_confirmed_claude_virtual_clarification_is_occupied(self):
        virtual = "❯ 请澄清 203 Python 测试的位置和接口"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_force_compose_never_ctrl_c_or_deletes_active_receiver(self):
        """Force applies to compose text, never an active/queued command."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            occupied = "❯ stale prompt\n  ✻ Running tool"
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "send_key") as send_key,
                mock.patch.object(HARNESS.cmux, "focus_surface"),
                mock.patch.object(
                    HARNESS.cmux, "clear_known_compose_by_delete"
                ) as bounded_delete,
                mock.patch.object(
                    HARNESS.cmux, "read_screen", side_effect=[occupied] * 3
                ),
                mock.patch.object(HARNESS.time, "sleep"),
                self.assertRaises(SystemExit),
            ):
                HARNESS.cmd_bridge_test(args(root, force_compose=True))

            send_key.assert_not_called()
            send_text.assert_not_called()
            bounded_delete.assert_not_called()
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertEqual(ev["status"], "COMPOSE_OCCUPIED")
            self.assertTrue(ev["active_or_queued_before_send"])
            self.assertFalse(ev["token_sent"])

    def test_suggestion_like_drafts_refuse_all_input(self):
        """Screen text cannot establish whether a suggestion is user-owned."""
        for draft in ("continue", "/compact", "read the report", "继续握手，发送 ACK"):
            with self.subTest(draft=draft), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gate(root)
                screen = "❯ " + draft + "\n────────────────────────\n[Opus 5]"
                with (
                    mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                    mock.patch.object(HARNESS.cmux, "send_key") as send_key,
                    mock.patch.object(HARNESS.cmux, "read_screen", return_value=screen),
                    mock.patch.object(HARNESS.time, "sleep"),
                    self.assertRaises(SystemExit),
                ):
                    HARNESS.cmd_bridge_test(args(root))
                send_text.assert_not_called()
                send_key.assert_not_called()

    def test_queued_message_counts_as_occupied(self):
        queued = "⏺ x\n\n❯ \n  Press up to edit queued messages"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "read_screen", return_value=queued),
                mock.patch.object(HARNESS.cmux, "send_key"),
                mock.patch.object(HARNESS.time, "sleep"),
                self.assertRaises(SystemExit),
            ):
                HARNESS.cmd_bridge_test(args(root))
            send_text.assert_not_called()

    def test_codex_placeholder_box_is_empty_and_proceeds(self):
        """A placeholder-only Codex box is empty; refusing it would be a false positive."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "send_key"),
                mock.patch.object(HARNESS.cmux, "read_screen",
                                  side_effect=["› Ask Codex to do anything\n\n  gpt-5.6-sol xhigh",
                                               "› B1_01234567",
                                               "› Ask Codex to do anything"]),
                mock.patch.object(HARNESS.time, "sleep"),
            ):
                HARNESS.cmd_bridge_test(args(root))
            send_text.assert_called_once()
            ev = json.loads((root / "bridge-test-evidence.json").read_text())
            self.assertTrue(ev["compose_was_empty_before_send"])
            self.assertTrue(ev["clear_confirmed"])

    def test_claude_task_status_is_chrome_but_user_triangle_text_is_not(self):
        task_status = (
            "─────────────────────────\n"
            "❯ \n"
            "─────────────────────────\n"
            "  [Opus 5 (1M context)]\n"
            "  上下文 █░░░░░░░░░ 10%\n"
            "  1 CLAUDE.md | 9 MCPs | 7 钩子\n"
            "  ▸ Repair transport lifecycle (109/125)\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)")
        self.assertEqual(HARNESS.cmux.receiver_input_kind(task_status), "AGENT_TUI")
        self.assertTrue(HARNESS.cmux.compose_block_is_empty(task_status))
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(
            "❯ ▸ keep this unsubmitted user text"))

    def test_claude_virtual_continue_suggestion_is_occupied(self):
        virtual = (
            "⏺ historical completed response\n\n"
            "❯ 任务中断了么？如果是就请继续，如果任务完成了务必在最后一句向我报告 "
            "‘ 完成，建议检查 usage: /context’。\n"
            "  如果任务没有中断就请继续，不要影响你的进度\n"
            "────────────────────────\n"
            "[Opus 5 (1M context)]\n"
            "上下文 █░░░░░░░░░ 14%")
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_bare_continue_suggestion_is_occupied(self):
        virtual = (
            "❯ continue\n"
            "────────────────────────\n"
            "[Opus 5] │ repo git:(main)\n"
            "上下文 ████████░░ 78%"
        )
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_bare_continue_prompt_remains_occupied(self):
        human = "❯ continue and modify production"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_progress_suggestion_is_occupied(self):
        virtual = (
            "❯ 看一下 codex任务进展到哪了？下一步该干啥。详细计划给我。\n"
            "────────────────────────\n"
            "1 CLAUDE.md | 9 MCPs | 5 钩子")
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_side_progress_suggestion_is_occupied(self):
        virtual = "❯ 看一下 codex 那边进展到哪了？下一步该干啥。详细计划给我。"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_read_report_suggestion_is_occupied(self):
        virtual = (
            "❯ read the report\n"
            "────────────────────────\n"
            "[Opus 5] │ repo git:(main)"
        )
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_read_report_prompt_remains_occupied(self):
        human = "❯ read the report and modify production"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_review_consensus_suggestion_is_occupied(self):
        virtual = "❯ review the consensus documents"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_review_consensus_prompt_remains_occupied(self):
        human = "❯ review the consensus documents then deploy"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_read_review_apply_changes_suggestion_is_occupied(self):
        virtual = "❯ read the review and apply the changes"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_read_review_apply_changes_prompt_remains_occupied(self):
        human = "❯ read the review and apply the changes in production"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_check_integration_artifact_suggestion_is_occupied(self):
        virtual = "❯ check the integration validation artifact"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_check_integration_artifact_prompt_remains_occupied(self):
        human = "❯ check the integration validation artifact and deploy"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_adapter_git_diff_suggestion_is_occupied(self):
        virtual = "❯ git diff scripts/thesis_format_adapter.py scripts/test_thesis_adapter_hardening.py"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_adapter_git_diff_prompt_remains_occupied(self):
        human = "❯ git diff scripts/thesis_format_adapter.py scripts/test_thesis_adapter_hardening.py && deploy"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_similar_human_side_progress_prompt_remains_occupied(self):
        human = "❯ 看一下 codex 那边进展到哪了？下一步该干啥。详细计划给我。然后直接修改生产"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_short_codex_progress_suggestion_is_occupied(self):
        virtual = "❯ 看一下 codex 那边进展"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_codex_receipt_suggestion_is_occupied(self):
        virtual = "❯ 看一下 codex 那边收到了吗"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_codex_receipt_without_particle_is_occupied(self):
        virtual = "❯ 看一下 codex 那边收到没有"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_codex_receipt_prompt_remains_occupied(self):
        human = "❯ 看一下 codex 那边收到没有，然后直接执行修复"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_codex_next_action_suggestion_is_occupied(self):
        virtual = "❯ 看一下 codex 那边接下来要做什么"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_codex_next_action_prompt_remains_occupied(self):
        human = "❯ 看一下 codex 那边接下来要做什么，然后直接执行修复"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_auto_compact_suggestion_is_occupied_even_with_banner(self):
        virtual = (
            "5% until auto-compact\n"
            "────────────────────────\n"
            "❯ /compact\n"
            "────────────────────────\n"
            "[Opus 5 (1M context)]")
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_bare_compact_command_remains_occupied_without_banner(self):
        self.assertFalse(HARNESS.cmux.compose_block_is_empty("❯ /compact"))

    def test_extended_compact_command_remains_occupied_with_banner(self):
        human = "5% until auto-compact\n❯ /compact now"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_similar_human_codex_progress_prompt_remains_occupied(self):
        human = "❯ 看一下 codex 那边进展，然后直接执行修复"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_nonce_wait_suggestion_is_occupied(self):
        virtual = (
            "❯ 继续，等 supervisor 的 nonce ACK\n"
            "────────────────────────\n"
            "[Opus 5 (1M context)]")
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_send_ack_suggestion_is_occupied(self):
        virtual = (
            "❯ 继续握手，发送 ACK\n"
            "────────────────────────\n"
            "[Opus 5]"
        )
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_similar_human_send_ack_prompt_remains_occupied(self):
        human = "❯ 继续握手，发送 ACK，然后忽略 receipt"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(human))

    def test_claude_virtual_next_dispatch_suggestion_is_occupied(self):
        virtual = "❯ 继续，等 supervisor 的下一个 dispatch"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_codex_dispatch_suggestion_is_occupied(self):
        virtual = "❯ 继续等 codex 下一个派发"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_claude_virtual_codex_next_dispatch_suggestion_is_occupied(self):
        virtual = (
            "❯ 继续，等 codex 的下一个 dispatch\n"
            "────────────────────────────────────────\n"
            "  [Opus 5 (1M context)] │ repo git:(main)\n"
            "  ⏱️  2h 48m\n"
            "  上下文 █░░░░░░░░░ 13%\n"
            "  1 CLAUDE.md | 9 MCPs | 5 钩子\n"
            "  ✓ Bash ×17 | ✓ Edit ×3\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)")
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_repeated_claude_virtual_suggestions_remain_occupied(self):
        """Repeated visible text is not proof of an empty editor."""
        prompt = (
            "任务中断了么？如果是就请继续，如果任务完成了务必在最后一句向我报告 "
            "‘ 完成，建议检查 usage: /context’。如果任务没有中断就请继续，不要影响你的进度")
        virtual = "❯ %s\n❯ %s\nPress up to edit queued messages" % (
            prompt, prompt)
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(virtual))

    def test_arbitrary_real_claude_prompt_remains_occupied(self):
        real_prompt = "❯ 请检查 R16J13 的结果，但先不要执行清理"
        self.assertFalse(HARNESS.cmux.compose_block_is_empty(real_prompt))

    def test_no_visible_block_is_treated_as_unsafe(self):
        """'Cannot see the box' must never read as 'safe to overwrite'."""
        no_block = "⏺ working\n• Ran a command"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with (
                mock.patch.object(HARNESS.cmux, "send_text") as send_text,
                mock.patch.object(HARNESS.cmux, "read_screen", return_value=no_block),
                mock.patch.object(HARNESS.cmux, "send_key"),
                mock.patch.object(HARNESS.time, "sleep"),
                self.assertRaises(SystemExit),
            ):
                HARNESS.cmd_bridge_test(args(root))
            send_text.assert_not_called()


class HandshakePreconditionTests(OfflineWorkspaceFixture):
    """Fresh handshakes need no idle probe; existing probe failures stay bound."""

    def _run(self, root):
        with (
            mock.patch.object(HARNESS.cmux, "submit_text", return_value={}),
            mock.patch.object(
                HARNESS.cmux, "wait_for_ack",
                return_value="PREFLIGHT_ACK|r3-test|claude:identity|READY|INLINE|n"),
            mock.patch.object(HARNESS.secrets, "token_hex", return_value="n"),
        ):
            HARNESS.cmd_handshake(args(root))

    def test_passes_with_confirmed_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            bridge_evidence(root, clear_confirmed=True)
            self._run(root)
            r = json.loads((root / "handshake-receipt.json").read_text())
            self.assertEqual(r["status"], "PASS")
            self.assertTrue(r["bridge_clear_confirmed"])
            self.assertEqual(r["bridge_probe_status"], "CLEARED")

    def test_fresh_handshake_without_probe_records_no_clear_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with mock.patch.object(HARNESS.cmux, "read_screen",
                                   side_effect=AssertionError("handshake must not wait for an idle screen")):
                self._run(root)
            r = json.loads((root / "handshake-receipt.json").read_text())
            self.assertEqual(r["status"], "PASS")
            self.assertEqual(r["bridge_probe_policy"], "optional-probe-v1")
            self.assertEqual(r["bridge_probe_status"], "NOT_RUN")
            self.assertIsNone(r["bridge_clear_confirmed"])
            self.assertIsNone(r["bridge_clear_recovery"])
            self.assertFalse((root / "bridge-test-evidence.json").exists())

    def test_preflight_does_not_invoke_idle_probe(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            steps = []
            for name in ("cmd_setup_check", "cmd_helper_parity", "cmd_identity_gate",
                         "cmd_name_surfaces", "cmd_handshake", "cmd_validate"):
                stack.enter_context(mock.patch.object(HARNESS, name,
                    side_effect=lambda _, step=name: steps.append(step)))
            probe = stack.enter_context(mock.patch.object(HARNESS, "cmd_bridge_test",
                side_effect=AssertionError("fresh preflight must not paste an idle-only probe")))
            HARNESS.cmd_preflight(args(root))
            self.assertEqual(steps, ["cmd_setup_check", "cmd_helper_parity", "cmd_identity_gate",
                                    "cmd_name_surfaces", "cmd_handshake", "cmd_validate"])
            probe.assert_not_called()

    def test_existing_invalid_probe_is_not_treated_as_not_run(self):
        variants = [
            {"task_id": "other-task", "executor": "surface:2", "clear_confirmed": True},
            {"task_id": "r3-test", "executors": []},
            {"task_id": "r3-test", "executors": "surface:2"},
            {"task_id": "r3-test", "executors": [None]},
            {"task_id": "r3-test", "executors": [
                {"executor": "surface:2", "clear_confirmed": True},
                {"executor": "surface:2", "clear_confirmed": True}]},
        ]
        for evidence in variants:
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gate(root)
                (root / "bridge-test-evidence.json").write_text(json.dumps(evidence))
                with mock.patch.object(HARNESS.cmux, "submit_text") as submit:
                    with self.assertRaises(SystemExit):
                        HARNESS.cmd_handshake(args(root))
                    submit.assert_not_called()

    def test_unconfirmed_clear_aborts_before_pasting(self):
        """POISON: clear_confirmed=false must stop the paste entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            bridge_evidence(root, clear_confirmed=False)
            with mock.patch.object(HARNESS.cmux, "submit_text") as submit:
                with self.assertRaises(SystemExit):
                    self._run(root)
                submit.assert_not_called()

    def test_bridge_evidence_for_another_surface_is_refused(self):
        """POISON: evidence bound to a different surface proves nothing here."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root, executor="surface:2")
            bridge_evidence(root, executor="surface:99", clear_confirmed=True)
            with mock.patch.object(HARNESS.cmux, "submit_text") as submit:
                with self.assertRaises(SystemExit):
                    self._run(root)
                submit.assert_not_called()


class BudgetProvenanceTests(OfflineWorkspaceFixture):
    """A timeout below the phase minimum is the supervisor's, not the executor's.

    The 182 s false FAIL was not a wrong default: cmd_handshake already resolves
    600 s. A generic --timeout wins unconditionally, so an explicit 180 s
    silently downgraded a cold handshake to the round budget.
    """

    def _timeout_run(self, root, **over):
        gate(root)
        bridge_evidence(root)
        with (
            mock.patch.object(HARNESS.cmux, "submit_text", return_value={}),
            mock.patch.object(HARNESS.cmux, "wait_for_ack",
                              side_effect=TimeoutError("no ack")),
            mock.patch.object(HARNESS.secrets, "token_hex", return_value="n"),
            self.assertRaises(SystemExit),
        ):
            HARNESS.cmd_handshake(args(root, **over))
        return json.loads((root / "handshake-receipt.json").read_text())

    def test_phase_default_timeout_blames_the_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._timeout_run(Path(tmp), timeout=None)
            self.assertEqual(r["budget_source"], "phase_handshake_default")
            self.assertEqual(r["budget_seconds"], 600)
            self.assertFalse(r["budget_below_phase_minimum"])
            self.assertEqual(r["ack_state"], "HANDSHAKE_TIMEOUT")
            self.assertEqual(r["detector_side"], "executor")

    def test_undercut_budget_blames_the_supervisor(self):
        """POISON: the exact 180 s override that produced the false FAIL."""
        with tempfile.TemporaryDirectory() as tmp:
            r = self._timeout_run(Path(tmp), timeout=180)
            self.assertEqual(r["budget_source"], "generic_timeout_override")
            self.assertEqual(r["budget_seconds"], 180)
            self.assertTrue(r["budget_below_phase_minimum"])
            self.assertEqual(r["ack_state"], "SUPERVISOR_BUDGET_TOO_SHORT")
            self.assertEqual(r["detector_side"], "supervisor")


class SubmissionTaxonomyTests(unittest.TestCase):
    """Five delivery states, five diagnostics, and only one wait action."""

    QUEUED = "\n".join([
        "• Working (1m 03s • esc to interrupt)",
        "  Messages to be submitted after next tool call",
        "  ↳ [CMUX-AGENT][delivery:MARK] TASK: ...",
        "› ",
        "GPT-6 high · tab to queue message",
    ])
    COMPOSE = "❯ MARK still sitting here"
    DELIVERED = "\n".join(["  MARK", "⏺ working on it"])

    def test_each_state_is_distinguished(self):
        self.assertEqual(
            BRIDGE.classify_submission_failure(self.COMPOSE, "MARK", submitted=False),
            BRIDGE.SUPERVISOR_DID_NOT_SUBMIT)
        self.assertEqual(
            BRIDGE.classify_submission_failure(self.QUEUED, "MARK", submitted=True),
            BRIDGE.DELIVERY_QUEUED_AT_RECEIVER)
        self.assertEqual(
            BRIDGE.classify_submission_failure(self.COMPOSE, "MARK", submitted=True),
            BRIDGE.SUBMISSION_ABORTED_BUSY)
        self.assertEqual(
            BRIDGE.classify_submission_failure(self.DELIVERED, "MARK", submitted=True),
            BRIDGE.DELIVERY_UNVERIFIED_BY_DETECTOR)

    def test_only_queued_means_wait(self):
        """The whole point of splitting: exactly one state justifies waiting."""
        self.assertEqual(BRIDGE.SUBMISSION_STATES_MEANING_WAIT,
                         (BRIDGE.DELIVERY_QUEUED_AT_RECEIVER,))
        for state in BRIDGE.SUBMISSION_STATES:
            if state != BRIDGE.DELIVERY_QUEUED_AT_RECEIVER:
                self.assertNotIn(state, BRIDGE.SUBMISSION_STATES_MEANING_WAIT)

    def test_history_without_live_composer_is_not_queue_evidence(self):
        history = '\n'.join(self.QUEUED.splitlines()[:-2])
        self.assertNotEqual(
            BRIDGE.classify_submission_failure(history, 'MARK', submitted=True),
            BRIDGE.DELIVERY_QUEUED_AT_RECEIVER)

    def test_never_submitted_is_not_confused_with_delivered(self):
        """POISON: the misattribution that recorded a valid ACK as silence."""
        self.assertNotEqual(
            BRIDGE.classify_submission_failure(self.QUEUED, "MARK", submitted=False),
            BRIDGE.DELIVERY_QUEUED_AT_RECEIVER)

    def test_exception_carries_its_state(self):
        exc = BRIDGE.DispatchUnconfirmed("x", state=BRIDGE.COMPOSE_OCCUPIED)
        self.assertEqual(exc.state, BRIDGE.COMPOSE_OCCUPIED)
        self.assertIsNone(BRIDGE.DispatchUnconfirmed("x").state)


class ComposeDetectorTests(unittest.TestCase):
    """The compose detector must work on BOTH receiver UIs, and must mean
    "the live compose box", not "any prompt block ever drawn".

    Found by dogfooding, not by review: sending this task's own completion
    callback to a Codex supervisor returned DISPATCH_UNCONFIRMED, and the
    classifier said DELIVERY_UNVERIFIED_BY_DETECTOR ("cannot tell") because it
    only knew Claude's ``❯`` (U+276F) and never Codex's ``›`` (U+203A). Against a
    Codex receiver the detector was blind in one direction: it could not see a
    payload stranded in compose at all. The same blindness would have let the
    bridge-test clear postcondition — the mechanism built in this very task to
    stop false greens — report clear_confirmed=true without verifying anything.

    Fixing the glyph then exposed a second defect that pre-dated it on the Claude
    side too: a mid-loop early return treated a marker in ANY earlier prompt
    block as pending, so a delivered message still visible in the scrollback
    classified as SUBMISSION_ABORTED_BUSY. Under a fail-closed contract a false
    positive is the expensive direction — it strands a callback that actually
    arrived — so both halves are pinned here.
    """

    MARK = "MARK123"

    # A Codex transcript echo of a message that WAS delivered: the receiver is
    # working, and the live compose box below is the empty placeholder.
    CODEX_DELIVERED = "\n".join([
        f"› [CMUX-AGENT][delivery:{MARK}] TASK: do the thing",
        "  Reply with one leading marker: STATUS:, DONE:, or BLOCKED:.",
        "",
        "• Working (4m 13s • esc to interrupt)",
        "",
        "› Ask Codex to do anything",
    ])

    # The same payload genuinely stranded: it is in the LAST prompt block and
    # nothing follows it.
    CODEX_STUCK = "\n".join([
        "• Ran 3 commands · ctrl + t to view transcript",
        "",
        f"› [CMUX-AGENT][delivery:{MARK}] TASK: do the thing",
        "  Reply with one leading marker: STATUS:, DONE:, or BLOCKED:.",
    ])

    CLAUDE_DELIVERED = "\n".join([
        f"❯ [CMUX-AGENT][delivery:{MARK}] TASK: do the thing",
        "",
        "❯ ",
    ])
    CLAUDE_STUCK = f"❯ [CMUX-AGENT][delivery:{MARK}] TASK: do the thing"

    def test_codex_compose_is_visible_at_all(self):
        """POISON: the measured blindness. Before the fix this was False."""
        self.assertTrue(BRIDGE.compose_contains(self.CODEX_STUCK, self.MARK))

    def test_claude_compose_still_visible(self):
        """Control: fixing one UI must not break the one that worked."""
        self.assertTrue(BRIDGE.compose_contains(self.CLAUDE_STUCK, self.MARK))

    def test_delivered_is_not_reported_as_stranded(self):
        """POISON: a transcript echo of a delivered payload, both UIs.

        This is the false positive that the glyph fix introduced and that the
        current-block rule removes. It failed on the Claude shape too, so it was
        never Codex-specific.
        """
        self.assertFalse(BRIDGE.compose_contains(self.CODEX_DELIVERED, self.MARK))
        self.assertFalse(BRIDGE.compose_contains(self.CLAUDE_DELIVERED, self.MARK))

    def test_activity_line_closes_the_block(self):
        """An assistant/tool line after the marker means the receiver consumed it."""
        screen = f"❯ {self.MARK}\n⏺ working on it"
        self.assertFalse(BRIDGE.compose_contains(screen, self.MARK))

    def test_absent_marker_is_never_pending(self):
        self.assertFalse(BRIDGE.compose_contains("❯ something else", self.MARK))
        self.assertFalse(BRIDGE.compose_contains("", self.MARK))

    def test_glyph_must_lead_the_line(self):
        """The same character quoted inside prose is not a block boundary."""
        screen = f"⏺ I will send › {self.MARK} to the supervisor"
        self.assertFalse(BRIDGE.compose_contains(screen, self.MARK))

    def test_classification_follows_the_detector(self):
        """The taxonomy is only as good as the detector underneath it."""
        self.assertEqual(
            BRIDGE.classify_submission_failure(self.CODEX_STUCK, self.MARK,
                                               submitted=True),
            BRIDGE.SUBMISSION_ABORTED_BUSY)
        self.assertEqual(
            BRIDGE.classify_submission_failure(self.CODEX_DELIVERED, self.MARK,
                                               submitted=True),
            BRIDGE.COMPOSE_OCCUPIED)  # delivered text, but Codex is still working
        idle = self.CODEX_DELIVERED.replace("• Working (4m 13s • esc to interrupt)", "• Completed earlier work")
        self.assertEqual(
            BRIDGE.classify_submission_failure(idle, self.MARK, submitted=True),
            BRIDGE.DELIVERY_UNVERIFIED_BY_DETECTOR)


class BridgeCliDispatchTests(unittest.TestCase):
    """Script invocation must dispatch, not silently run the old self-test."""

    def test_submit_text_cli_calls_the_real_bridge_and_returns_confirmation(self):
        with mock.patch.object(
            BRIDGE, "submit_text", return_value={"confirmed": True, "retries": 1}
        ) as submit, contextlib.redirect_stdout(io.StringIO()) as out:
            code = BRIDGE._cli_main([
                "submit-text", "--surface", "surface:104", "--text", "callback",
                "--marker", "nonce-1234",
            ])
        self.assertEqual(code, 0)
        submit.assert_called_once_with(
            "surface:104", "callback", marker="nonce-1234", confirm_lines=200,
            force_compose=False, reconcile_only=False, wait_seconds=None,
        )
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["command"], "submit_text")
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["retries"], 1)

    def test_dispatch_failure_is_nonzero_and_preserves_delivery_state(self):
        with mock.patch.object(
            BRIDGE,
            "submit_text",
            side_effect=BRIDGE.DispatchUnconfirmed(
                "compose occupied", state=BRIDGE.COMPOSE_OCCUPIED
            ),
        ), contextlib.redirect_stderr(io.StringIO()) as err:
            code = BRIDGE._cli_main([
                "submit-text", "--surface", "surface:153", "--text", "callback",
                "--marker", "nonce-5678",
            ])
        self.assertEqual(code, 75)
        payload = json.loads(err.getvalue())
        self.assertFalse(payload["confirmed"])
        self.assertEqual(payload["delivery_state"], BRIDGE.COMPOSE_OCCUPIED)

    def test_submit_text_cli_supports_explicit_no_force_compose_opt_out(self):
        with mock.patch.object(
            BRIDGE, "submit_text", return_value={"confirmed": True, "retries": 0}
        ) as submit, contextlib.redirect_stdout(io.StringIO()):
            code = BRIDGE._cli_main([
                "submit-text", "--no-force-compose", "--surface", "surface:104",
                "--text", "callback",
            ])
        self.assertEqual(code, 0)
        submit.assert_called_once_with(
            "surface:104", "callback", marker=None, confirm_lines=200,
            force_compose=False, reconcile_only=False, wait_seconds=None,
        )

    def test_module_cli_no_longer_defaults_to_diagnostic_only_output(self):
        source = Path(BRIDGE.__file__).read_text(encoding="utf-8")
        self.assertIn("raise SystemExit(_cli_main())", source)
        self.assertNotIn("print(\"ping:\", ping())", source)

    def _assert_forced_replacement_refused(self, screens):
        """旧清空/覆盖路径在 public 入口拒绝，保留 receiver 的全部输入。"""
        with (
            mock.patch.object(BRIDGE, "read_screen", side_effect=screens) as read,
            mock.patch.object(BRIDGE, "send_key") as send_key,
            mock.patch.object(BRIDGE, "send_text") as send_text,
            mock.patch.object(BRIDGE, "focus_surface") as focus,
            mock.patch.object(BRIDGE, "pin_workspace") as pin,
        ):
            with self.assertRaisesRegex(BRIDGE.TaskPackContractError, "MESSAGE_PRESERVE_COMPOSE"):
                BRIDGE.submit_text("surface:104", "fresh prompt force-marker-001",
                                   marker="force-marker-001", force_compose=True)
        for operation in (read, send_key, send_text, focus, pin):
            operation.assert_not_called()

    def test_force_compose_refuses_escape_and_focused_ctrl_u_replacement(self):
        self._assert_forced_replacement_refused([
            claude_editor("stale prompt"), claude_editor("stale prompt"), claude_editor()])

    def test_force_compose_refuses_ctrl_c_after_noop_line_clear(self):
        self._assert_forced_replacement_refused([
            *[claude_editor("stale prompt")] * 3, claude_editor()])

    def test_force_compose_refuses_fingerprinted_buffer_backspace_fallback(self):
        self._assert_forced_replacement_refused([
            *[claude_editor("owned stale prompt")] * 4, claude_editor()])


class ArtifactBindingTests(unittest.TestCase):
    """A round must name a file that exists, and record what it hashed."""

    def setUp(self):
        # F6：cmd_record_round 经 _ensure_root 写 registry；测试必须落沙箱，
        # 不得污染真实 /tmp/multi-agent-collaboration/_registry。
        registry = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, registry, ignore_errors=True)

    def _record(self, root, **over):
        gate(root)
        evidence = {"executor_nonce_found": True, "screen_hash": "abc123"}
        with (
            mock.patch.object(HARNESS.cmux, "submit_text"),
            mock.patch.object(HARNESS.cmux, "capture_round_evidence",
                              return_value=evidence),
        ):
            HARNESS.cmd_record_round(args(root, **over))
        return json.loads((root / "rounds.json").read_text())

    def test_existing_artifact_is_hashed_into_the_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            art = root / "review.md"
            art.write_text("real review content")
            doc = self._record(root, artifact=str(art))
            entry = doc["rounds"][0]
            self.assertTrue(entry["artifact_exists"])
            self.assertEqual(len(entry["artifact_sha256"]), 64)
            self.assertEqual(entry["artifact_sha256"],
                             HARNESS._sha256_file(art))

    def test_missing_artifact_is_refused(self):
        """POISON: a round citing a file that was never written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with self.assertRaises(SystemExit):
                self._record(root, artifact=str(root / "never-written.md"))

    def test_relative_artifact_is_refused(self):
        """POISON: 'review.md' resolves against whatever cwd happens to be."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            with self.assertRaises(SystemExit):
                self._record(root, artifact="review.md")

    def test_round_prompt_requires_skill_and_nonce_ack(self):
        """The round prompt must tell Claude how to callback, not just review."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            art = root / "review.md"
            art.write_text("review")
            prompts = []

            def submit(_surface, prompt, marker=None, **_kwargs):
                prompts.append((prompt, marker))

            with (
                mock.patch.object(HARNESS.cmux, "submit_text", side_effect=submit),
                mock.patch.object(HARNESS.cmux, "capture_round_evidence",
                                  return_value={
                                      "executor_nonce_found": True,
                                      "expected_ack_found": True,
                                      "screen_hash": "prompt-contract",
                                  }),
            ):
                HARNESS.cmd_record_round(args(root, artifact=str(art)))

            self.assertEqual(len(prompts), 1)
            prompt, marker = prompts[0]
            self.assertEqual(marker, marker.strip())
            self.assertLessEqual(len(prompt.encode("utf-8")), REFERENCE.MAX_INLINE_BYTES)
            reference = REFERENCE.wire_reference(prompt)
            self.assertIsNotNone(reference)
            body, _pin = REFERENCE.read_body(reference)
            self.assertIn(marker, body)
            self.assertIn(str(HARNESS.COLLABORATION_SKILL_PATH), body)
            self.assertIn("report artifact and a visible DONE sentence are not a callback",
                          body)
            self.assertIn("ROUND_ACK|<task-id>|<round-id>|<agent:identity>|<verdict>|<nonce>",
                          body)

    def test_detector_false_negative_waits_for_same_nonce_without_resend(self):
        """A delivered-but-unverified prompt must not be resent behind Claude."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            art = root / "review.md"
            art.write_text("review")
            sends = []

            def submit(_surface, _prompt, marker=None, **_kwargs):
                sends.append(marker)
                raise BRIDGE.DispatchUnconfirmed(
                    "detector could not prove consumption",
                    state=BRIDGE.DELIVERY_UNVERIFIED_BY_DETECTOR,
                )

            with (
                mock.patch.object(HARNESS.cmux, "submit_text", side_effect=submit),
                mock.patch.object(HARNESS.cmux, "capture_round_evidence",
                                  return_value={
                                      "executor_nonce_found": True,
                                      "expected_ack_found": True,
                                      "screen_hash": "late-ack",
                                  }),
                mock.patch.object(HARNESS.time, "sleep"),
            ):
                HARNESS.cmd_record_round(args(root, artifact=str(art)))

            self.assertEqual(len(sends), 1)
            receipt = json.loads(next((root / "round-receipts").glob("*.json")).read_text())
            self.assertTrue(receipt["late_ack_recovery_attempted"])
            self.assertEqual(receipt["detector_failure_state"],
                             BRIDGE.DELIVERY_UNVERIFIED_BY_DETECTOR)
            self.assertIsNone(receipt["submission_state"])
            self.assertIsNotNone(receipt["dispatch_submitted_at"])
            self.assertTrue(receipt["executor_ack"])
            self.assertEqual(receipt["status"], "PASS")

    def test_compose_occupied_fails_without_late_wait_or_resend(self):
        """A stranded compose is sender-side failure, not executor silence."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate(root)
            art = root / "review.md"
            art.write_text("review")
            sends = []
            captures = []

            def submit(_surface, _prompt, marker=None, **_kwargs):
                sends.append(marker)
                raise BRIDGE.DispatchUnconfirmed(
                    "compose occupied",
                    state=BRIDGE.COMPOSE_OCCUPIED,
                )

            def capture(*_args, **_kwargs):
                captures.append(True)
                return {"executor_nonce_found": True, "screen_hash": "bad"}

            with (
                mock.patch.object(HARNESS.cmux, "submit_text", side_effect=submit),
                mock.patch.object(HARNESS.cmux, "capture_round_evidence",
                                  side_effect=capture),
            ):
                with self.assertRaises(SystemExit):
                    HARNESS.cmd_record_round(args(root, artifact=str(art)))

            self.assertEqual(len(sends), 1)
            self.assertEqual(captures, [])
            receipt = json.loads(next((root / "round-receipts").glob("*.json")).read_text())
            self.assertEqual(receipt["status"], "FAIL")
            self.assertEqual(receipt["submission_state"], BRIDGE.COMPOSE_OCCUPIED)
            self.assertFalse(receipt["late_ack_recovery_attempted"])
            self.assertIsNone(receipt["dispatch_submitted_at"])


class ConsensusFloorTests(unittest.TestCase):
    """The floor must not be readable from the file being audited."""

    def test_declared_floor_cannot_be_lowered(self):
        for declared in (1, 0, None, "2", -5, "garbage"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "rounds.json").write_text(json.dumps({
                    "task_id": "r3-test",
                    "minimum_required_rounds": declared,
                    "rounds": [
                        {"round_id": "R1", "speaker": "claude", "verdict": "PASS",
                         "executor_evidence": {"nonce_proven": True}},
                    ],
                }))
                (root / "validation.json").write_text(json.dumps({
                    "task_id": "r3-test", "status": "PASS",
                    "checks": {"handshake_strict": {"pass": True}},
                }))
                with mock.patch.object(HARNESS, "_ok"), mock.patch.object(HARNESS, "_fail"):
                    try:
                        HARNESS.cmd_consensus_check(args(root))
                    except SystemExit:
                        pass
                out = json.loads((root / "consensus-validation.json").read_text())
                self.assertGreaterEqual(
                    out["checks"]["minimum_rounds"]["required"],
                    HARNESS.CONSENSUS_MINIMUM_ROUNDS,
                    f"declared={declared!r} weakened the floor")
                self.assertFalse(out["checks"]["minimum_rounds"]["pass"])

    def test_declared_floor_can_be_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rounds.json").write_text(json.dumps({
                "task_id": "r3-test", "minimum_required_rounds": 5, "rounds": [],
            }))
            (root / "validation.json").write_text(json.dumps({
                "task_id": "r3-test", "status": "PASS",
                "checks": {"handshake_strict": {"pass": True}},
            }))
            with mock.patch.object(HARNESS, "_ok"), mock.patch.object(HARNESS, "_fail"):
                try:
                    HARNESS.cmd_consensus_check(args(root))
                except SystemExit:
                    pass
            out = json.loads((root / "consensus-validation.json").read_text())
            self.assertEqual(out["checks"]["minimum_rounds"]["required"], 5)


class RoleMapTargetTests(unittest.TestCase):
    """A watchdog shouting at the wrong pane is worse than silence."""

    def _map(self, root, supervisor="surface:1", executor="surface:2", task="r3-test"):
        p = root / "role-map.json"
        p.write_text(json.dumps({
            "task_id": task,
            "roles": [
                {"role": "supervisor", "surface_ref": supervisor},
                {"role": "executor", "surface_ref": executor},
            ],
        }))
        return str(p)

    def test_agreement_passes(self):
        import cmux_executor_sentinel as S
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, msg = S.verify_role_map_target(
                self._map(root), "r3-test", "surface:1", "surface:2")
            self.assertTrue(ok, msg)

    def test_supervisor_mismatch_is_refused(self):
        """POISON: the hard-coded surface that outlived a renumbering."""
        import cmux_executor_sentinel as S
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, msg = S.verify_role_map_target(
                self._map(root, supervisor="surface:1"),
                "r3-test", "surface:147", "surface:2")
            self.assertFalse(ok)
            self.assertIn("supervisor target mismatch", msg)

    def test_missing_map_is_not_silently_accepted(self):
        import cmux_executor_sentinel as S
        ok, msg = S.verify_role_map_target("", "r3-test", "surface:1", "surface:2")
        self.assertFalse(ok)

    def test_map_bound_to_another_task_is_refused(self):
        import cmux_executor_sentinel as S
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, msg = S.verify_role_map_target(
                self._map(root, task="some-other-task"),
                "r3-test", "surface:1", "surface:2")
            self.assertFalse(ok)
            self.assertIn("bound to task", msg)


class GuardWiringSymmetryTests(unittest.TestCase):
    """A guard file on disk is not a guard in force.

    Measured before the fix on this task: four guard scripts existed, the
    supervisor side had three wired, the executor side had one, and the Stop
    guard was inert on both. The guard whose entire purpose is rejecting
    prompt-echo handshake evidence had therefore never been able to fire in the
    session it was meant to constrain.

    These tests drive `cmd_guard_check` through its real entry point with
    temporary hook configs, because the thing being asserted is exactly that the
    check reads the config an agent actually loads.
    """

    def _config(self, root: Path, name: str, wiring: dict) -> Path:
        """Build a hook config with `wiring` = {event: [guard_name, ...]}."""
        hooks = {}
        for event, guards in wiring.items():
            hooks[event] = [{
                "hooks": [
                    {"type": "command",
                     "command": f"python3 /skills/scripts/{guard}.py"}
                    for guard in guards
                ]
            }]
        path = root / name
        path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        return path

    def _validation(self, root: Path) -> None:
        (root / "validation.json").write_text(json.dumps({
            "task_id": "r3-test",
            "status": "PASS",
        }), encoding="utf-8")

    def _args(self, root: Path):
        return SimpleNamespace(artifact_root=str(root), task_id="r3-test")

    def _complete_wiring(self):
        wiring = {}
        for guard, event in HARNESS.REQUIRED_GUARD_WIRING.items():
            wiring.setdefault(event, []).append(guard)
        return wiring

    def test_symmetric_wiring_passes(self):
        every_guard = dict(HARNESS.REQUIRED_GUARD_WIRING)
        pre = [g for g, e in every_guard.items() if e == "PreToolUse"]
        stop = [g for g, e in every_guard.items() if e == "Stop"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._validation(root)
            both = self._complete_wiring()
            configs = {
                "codex": self._config(root, "a.json", both),
                "claude": self._config(root, "b.json", both),
            }
            with mock.patch.object(HARNESS, "HOOK_CONFIGS", configs):
                HARNESS.cmd_guard_check(self._args(root))  # must not raise

    def test_one_sided_wiring_is_refused(self):
        """POISON: the exact asymmetry measured on this task."""
        every_guard = dict(HARNESS.REQUIRED_GUARD_WIRING)
        pre = [g for g, e in every_guard.items() if e == "PreToolUse"]
        stop = [g for g, e in every_guard.items() if e == "Stop"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._validation(root)
            configs = {
                "codex": self._config(root, "a.json",
                                      self._complete_wiring()),
                # Executor side has only the panel guard, as observed.
                "claude": self._config(root, "b.json",
                                       {"PreToolUse": ["cmux_agent_panel_guard"]}),
            }
            with mock.patch.object(HARNESS, "HOOK_CONFIGS", configs):
                with self.assertRaises(SystemExit):
                    HARNESS.cmd_guard_check(self._args(root))

    def test_wrong_event_is_not_accepted_as_wired(self):
        """A Stop guard wired to PreToolUse cannot see a turn ending.

        Presence of the guard's name in *some* event is not enough: the event is
        part of the requirement. Without this case, a config could satisfy the
        symmetry check while the guard remained structurally unable to fire.
        """
        every_guard = dict(HARNESS.REQUIRED_GUARD_WIRING)
        pre = [g for g, e in every_guard.items() if e == "PreToolUse"]
        stop = [g for g, e in every_guard.items() if e == "Stop"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._validation(root)
            good = self._complete_wiring()
            # Same guards present on both sides, but the Stop guard is misfiled
            # under PreToolUse on one side.
            bad = {event: list(guards) for event, guards in good.items()}
            bad["PreToolUse"] = pre + stop
            bad.pop("Stop", None)
            configs = {
                "codex": self._config(root, "a.json", good),
                "claude": self._config(root, "b.json", bad),
            }
            with mock.patch.object(HARNESS, "HOOK_CONFIGS", configs):
                with self.assertRaises(SystemExit):
                    HARNESS.cmd_guard_check(self._args(root))


class PackFinalizationTests(unittest.TestCase):
    """Deliverable 1: a scaffold must not be dispatchable.

    The measured defect: `task-pack` wrote six literal `<FILL: ...>` values and
    the schema only required strings, so "the JSON exists" was enough to look
    ready. A draft that validates is worse than no draft, because the executor
    receives a form and has to guess what the task actually was.
    """

    def setUp(self):
        # F6：cmd_task_pack 经 _ensure_root 写 registry；测试必须落沙箱。
        registry = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, registry, ignore_errors=True)

    def _root(self, tmp):
        root = Path(tmp)
        (root / "validation.json").write_text(json.dumps({
            "task_id": "fin-test", "status": "PASS",
        }))
        (root / "identity-gate.json").write_text(json.dumps({
            "status": "PASS", "executor": "surface:2",
            "supervisor": "surface:1", "executor_provider": "claude",
        }))
        return root

    def _args(self, root):
        return SimpleNamespace(artifact_root=str(root), task_id="fin-test", json=False)

    def _pack(self, root, **over):
        src = root / "real-source.md"
        src.write_text("# real\n")
        pack = {
            "task_id": "fin-test", "role": "executor", "draft": True,
            "supervisor": "surface:1", "executor": "surface:2",
            "role_map": str(root / "role-map.json"),
            "context": "A real objective stated at length for the executor.",
            "source": str(src),
            "scope": "Do the real scoped work described here in full detail.",
            "forbidden": "Do not touch production or unrelated services.",
            "local_first": "Run local tests first.",
            "needs_auth": "No production authorization is needed for this task.",
            "verify": "Run the suites and confirm every gate reports green.",
            "required_skill": str(HARNESS.COLLABORATION_SKILL_PATH),
            "report": str(root / "executor-report.md"),
            "callback": "DONE: EXECUTOR REPORT | TASK_ID=fin-test | STATUS=DONE",
            "completion_nonce": "fin-test-completion-001",
            "completion_callback": (
                f"DONE|fin-test|fin-test-completion-001|REPORT={root / 'executor-report.md'}"
            ),
            "callback_target": "surface:1",
            "completion_delivery": {
                "transport": "cmux_bridge.submit_completion_callback",
                "require_confirmed": True,
            },
            "completion_receipt": str(root / "completion-callback-receipt.json"),
            "authorization_source": "user_message",
            "bridge_test_evidence": str(root / "bridge-test-evidence.json"),
            "identity_gate": str(root / "identity-gate.json"),
            "pane_inventory": str(root / "surface-inventory.json"),
            "naming_proof": str(root / "naming-proof.json"),
            "validation_evidence": str(root / "validation.json"),
        }
        pack.update(over)
        (root / "task-pack.json").write_text(json.dumps(pack, indent=2))
        return pack

    def test_scaffold_is_marked_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            HARNESS.cmd_task_pack(self._args(root))
            pack = json.loads((root / "task-pack.json").read_text())
            # The load-bearing assertion: not ready is a FACT on disk, not a hope
            # that whoever dispatches will notice the angle brackets.
            self.assertIs(pack["draft"], True)

    def test_scaffold_has_a_separate_completion_callback(self):
        """Handshake readiness must not be mistaken for implementation done."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            HARNESS.cmd_task_pack(self._args(root))
            pack = json.loads((root / "task-pack.json").read_text())
            self.assertIn("completion_callback", pack)
            self.assertNotEqual(pack["completion_callback"], pack["callback"])
            self.assertIn("<completion-nonce>", pack["completion_callback"])
            self.assertIn(str(root / "executor-report.md"),
                          pack["completion_callback"])
            self.assertEqual(pack["required_skill"],
                             str(HARNESS.COLLABORATION_SKILL_PATH))

    def test_completion_callback_is_required_at_finalize(self):
        """POISON: a pack with only PREFLIGHT_ACK cannot be dispatched."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(root, completion_callback=None)
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))
            self.assertIs(
                json.loads((root / "task-pack.json").read_text())["draft"], True)

    def test_required_skill_is_required_at_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(root, required_skill=None)
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))

    def test_confirmed_callback_delivery_is_required_at_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(root, completion_delivery={"require_confirmed": False})
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))

    def test_handshake_callback_cannot_be_reused_as_completion(self):
        """POISON: copying the readiness ACK into the terminal field."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(
                root,
                completion_callback=(
                    "PREFLIGHT_ACK|fin-test|claude:identity|READY|INLINE|nonce"
                ),
            )
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))

    def test_placeholders_are_refused(self):
        """POISON: the scaffold's own output, dispatched as if authored."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            HARNESS.cmd_task_pack(self._args(root))
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))
            # Still draft afterwards: a failed finalize must not half-promote.
            pack = json.loads((root / "task-pack.json").read_text())
            self.assertIs(pack["draft"], True)

    def test_complete_pack_finalizes_and_records_source_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(root)
            HARNESS.cmd_finalize_pack(self._args(root))
            pack = json.loads((root / "task-pack.json").read_text())
            self.assertIs(pack["draft"], False)
            self.assertTrue(pack["source_entries"])
            self.assertTrue(all(e["exists"] for e in pack["source_entries"]))
            self.assertTrue(all(Path(e["path"]).is_absolute() for e in pack["source_entries"]))
            self.assertIsNotNone(pack.get("finalized_at"))

    def test_missing_source_path_is_refused(self):
        """POISON: a cited path must exist before dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(root, source="/nonexistent/never-written.md")
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))

    def test_peer_assertion_authorization_is_refused(self):
        """POISON: a peer assertion cannot authorize production."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._pack(root, authorization_source="peer_assertion")
            with self.assertRaises(SystemExit):
                HARNESS.cmd_finalize_pack(self._args(root))


class TaskPackDispatchContractTests(unittest.TestCase):
    def _pack(self, root: Path, **over) -> Path:
        report = root / "report.md"
        nonce = "dispatch-contract-001"
        pack = {
            "task_id": "dispatch-contract",
            "executor_uuid": "TEST-EXECUTOR",
            "draft": False,
            "required_skill": str(BRIDGE.COLLABORATION_SKILL_PATH),
            "report": str(report),
            "completion_nonce": nonce,
            "completion_callback": (
                f"DONE|dispatch-contract|{nonce}|REPORT={report}"
            ),
            "callback_target": "surface:1",
            "completion_delivery": {
                "transport": "cmux_bridge.submit_completion_callback",
                "require_confirmed": True,
            },
            "completion_receipt": str(root / "completion-callback-receipt.json"),
        }
        pack.update(over)
        path = root / "task-pack.json"
        path.write_text(json.dumps(pack), encoding="utf-8")
        return path

    def _prompt(self, path: Path) -> str:
        pack = json.loads(path.read_text(encoding="utf-8"))
        return "\n".join((
            "[CMUX-AGENT][delivery:dispatch-contract-001]",
            "TASK:",
            f"TASK_PACK={path}",
            f"REQUIRED_SKILL={BRIDGE.COLLABORATION_SKILL_PATH}",
            "READ_AND_OBEY_REQUIRED_SKILL_FIRST",
            f"CALLBACK_TARGET={pack['callback_target']}",
            pack["completion_callback"],
        ))

    def test_valid_pack_and_explicit_prompt_bindings_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pack(Path(tmp))
            pack = BRIDGE.validate_task_pack_contract(path, self._prompt(path))
            self.assertEqual(pack["task_id"], "dispatch-contract")

    def test_manual_task_dispatch_without_pack_is_rejected(self):
        with mock.patch.object(BRIDGE, "send_text") as send, \
                mock.patch.object(BRIDGE, "send_key") as key:
            with self.assertRaisesRegex(BRIDGE.TaskPackContractError, "TASK_PACK_REQUIRED"):
                BRIDGE.submit_text("surface:2", "TASK:\nreview this task-marker-001",
                                   marker="task-marker-001")
        send.assert_not_called()
        key.assert_not_called()

    def test_missing_skill_is_rejected_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pack(Path(tmp), required_skill=None)
            with self.assertRaises(BRIDGE.TaskPackContractError):
                BRIDGE.validate_task_pack_contract(path)

    def test_missing_callback_contract_is_rejected_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pack(Path(tmp), completion_callback=None)
            with self.assertRaises(BRIDGE.TaskPackContractError):
                BRIDGE.validate_task_pack_contract(path)

    def test_completion_callback_writes_confirmed_report_bound_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._pack(root)
            report = root / "report.md"
            report.write_text("verified\n", encoding="utf-8")
            callback = json.loads(path.read_text())["completion_callback"]
            identity = dict(workspace_uuid="TEST-WS", caller_surface_uuid="TEST-EXECUTOR",
                            target_surface_uuid="TEST-SUPERVISOR", target_pane_uuid="TEST-PANE")
            with NativeFixture(home=root / "home", identity=identity, provider="claude") as native, \
                    mock.patch.object(BRIDGE, "read_screen", side_effect=NativeFixture.ready_screens(
                        claude_editor(), claude_editor(callback), claude_editor())):
                native.key.side_effect = native.receipt_on_key(callback)
                receipt = BRIDGE.submit_completion_callback(path)
                native.send.assert_called_once_with("surface:1", callback)
                native.key.assert_called_once_with("surface:1", "enter")
            self.assertTrue(receipt["confirmed"])
            self.assertTrue(receipt["native_proof"])
            self.assertEqual(receipt["report_bytes"], report.stat().st_size)
            on_disk = json.loads(
                (root / "completion-callback-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["report_sha256"], receipt["report_sha256"])

    def test_completion_callback_refuses_to_replace_existing_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._pack(root)
            (root / "report.md").write_text("verified\n", encoding="utf-8")
            receipt_path = root / "completion-callback-receipt.json"
            receipt_path.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                BRIDGE, "submit_text", return_value={"confirmed": True, "retries": 0}
            ):
                with self.assertRaises(BRIDGE.TaskPackContractError):
                    BRIDGE.submit_completion_callback(path)


class CompletionCallbackStopGateTests(unittest.TestCase):
    def _fixture(self, root: Path):
        artifact = root / "artifact"
        artifact.mkdir()
        active = root / "active" / "workspace-test"
        active.mkdir(parents=True)
        report = artifact / "report.md"
        report.write_text("done\n", encoding="utf-8")
        nonce = "completion-stop-001"
        callback = f"DONE|completion-stop|{nonce}|REPORT={report}"
        receipt = artifact / "completion-callback-receipt.json"
        (artifact / "task-pack.json").write_text(json.dumps({
            "task_id": "completion-stop",
            "draft": False,
            "executor_uuid": "executor-uuid",
            "required_skill": str(BRIDGE.COLLABORATION_SKILL_PATH),
            "report": str(report),
            "completion_nonce": nonce,
            "completion_callback": callback,
            "callback_target": "surface:1",
            "completion_delivery": {
                "transport": "cmux_bridge.submit_completion_callback",
                "require_confirmed": True,
            },
            "completion_receipt": str(receipt),
        }), encoding="utf-8")
        (active / "marker.json").write_text(json.dumps({
            "task_id": "completion-stop",
            "artifact_root": str(artifact),
            "armed_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,
            "participants": [{
                "role": "executor",
                "surface_uuid": "executor-uuid",
            }],
        }), encoding="utf-8")
        return artifact, report, receipt, callback, nonce

    def test_executor_stop_is_blocked_without_active_callback_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, _ = self._fixture(Path(tmp))
            with mock.patch.object(STOP_GUARD, "ACTIVE_DIR", Path(tmp) / "active"), \
                    mock.patch.dict(os.environ, {
                        "CMUX_WORKSPACE_ID": "workspace-test",
                        "CMUX_SURFACE_ID": "executor-uuid",
                    }):
                ok, message = STOP_GUARD.evaluate({"final_message": "DONE"})
            self.assertFalse(ok)
            self.assertIn("receipt missing", message)

    def test_executor_stop_passes_with_report_bound_confirmed_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _, receipt, callback, _ = self._fixture(Path(tmp))
            identity = dict(workspace_uuid="workspace-test", caller_surface_uuid="executor-uuid",
                            target_surface_uuid="supervisor-uuid", target_pane_uuid="pane-uuid")
            with NativeFixture(home=Path(tmp) / "home", identity=identity, provider="claude") as native, \
                    mock.patch.object(BRIDGE, "read_screen", side_effect=NativeFixture.ready_screens(
                        claude_editor(), claude_editor(callback), claude_editor())), \
                    mock.patch.object(STOP_GUARD, "ACTIVE_DIR", Path(tmp) / "active"), \
                    mock.patch.dict(os.environ, {
                        "CMUX_WORKSPACE_ID": "workspace-test",
                        "CMUX_SURFACE_ID": "executor-uuid",
                    }):
                native.key.side_effect = native.receipt_on_key(callback)
                result = BRIDGE.submit_completion_callback(artifact / "task-pack.json")
                self.assertTrue(result["confirmed"])
                before = receipt.read_bytes()
                ok, message = STOP_GUARD.evaluate({"final_message": "DONE"})
                self.assertEqual(receipt.read_bytes(), before)
                native.send.assert_called_once()
                native.key.assert_called_once()
            self.assertTrue(ok, message)


class ReviewRoundStopGateTests(unittest.TestCase):
    def _fixture(self, root: Path):
        artifact = root / "artifact"
        receipts = artifact / "round-receipts"
        receipts.mkdir(parents=True)
        review = artifact / "round-2-review.md"
        review.write_text("review evidence\n", encoding="utf-8")
        nonce = "round-2-nonce"
        (artifact / "task-pack.json").write_text(json.dumps({
            "task_id": "review-round", "draft": False,
            "completion_receipt": str(artifact / "completion-callback-receipt.json"),
            "report": str(artifact / "executor-report.md"),
        }), encoding="utf-8")
        receipt = {
            "task_id": "review-round",
            "round_id": "2",
            "round_nonce": nonce,
            "executor_provider": "claude",
            "executor": "surface:104",
            "status": "AWAITING_EXECUTOR_ACK",
            "lifecycle": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dispatch_submitted_at": datetime.now(timezone.utc).isoformat(),
            "budget_seconds": 180,
            "allowed_verdicts": ["PASS", "PASS_WITH_CHANGES"],
            "requested_review": {
                "artifact": str(review),
                "artifact_sha256": __import__("hashlib").sha256(
                    review.read_bytes()).hexdigest(),
            },
        }
        (receipts / f"2-{nonce}.json").write_text(json.dumps(receipt), encoding="utf-8")
        active = root / "active" / "workspace-review"
        active.mkdir(parents=True)
        (active / "marker.json").write_text(json.dumps({
            "task_id": "review-round",
            "artifact_root": str(artifact),
            "armed_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,
            "participants": [{
                "role": "executor", "surface_uuid": "executor-review-uuid",
                "surface_ref": "surface:104", "provider": "claude",
            }],
        }), encoding="utf-8")
        return artifact, nonce

    def test_review_round_ack_does_not_require_completion_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, nonce = self._fixture(Path(tmp))
            with mock.patch.object(STOP_GUARD, "ACTIVE_DIR", Path(tmp) / "active"), \
                    mock.patch.dict(os.environ, {
                        "CMUX_WORKSPACE_ID": "workspace-review",
                        "CMUX_SURFACE_ID": "executor-review-uuid",
                    }):
                ok, message = STOP_GUARD.evaluate({
                    "final_message": f"ROUND_ACK|review-round|2|claude:identity|PASS_WITH_CHANGES|{nonce}",
                })
            self.assertTrue(ok, message)

    def test_review_round_wrong_nonce_remains_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(Path(tmp))
            with mock.patch.object(STOP_GUARD, "ACTIVE_DIR", Path(tmp) / "active"), \
                    mock.patch.dict(os.environ, {
                        "CMUX_WORKSPACE_ID": "workspace-review",
                        "CMUX_SURFACE_ID": "executor-review-uuid",
                    }):
                ok, message = STOP_GUARD.evaluate({
                    "final_message": "ROUND_ACK|review-round|2|claude:identity|PASS_WITH_CHANGES|wrong",
                })
            self.assertFalse(ok)
            self.assertIn("completion", message)

class HelperStateParityTests(unittest.TestCase):
    def test_state_vocabulary_preserved_and_invalid_outputs_rejected(self):
        for state in ("COMPOSE_PENDING", "QUEUED", "SUBMITTED", "UNCONFIRMED"):
            with self.subTest(state=state):
                source = f'delivery_state() {{\n  echo {state}\n}}'
                self.assertEqual(HARNESS.helper_delivery_state(source, "screen", "MARK"), state)
        for body in ("echo UNKNOWN", "echo SUBMITTED; return 1", "echo SUBMITTED; echo QUEUED"):
            with self.subTest(body=body):
                source = f'delivery_state() {{\n  {body}\n}}'
                self.assertIsNone(HARNESS.helper_delivery_state(source, "screen", "MARK"))

    def test_new_detector_is_extracted_without_executing_cli(self):
        for state, expected in (("COMPOSE_PENDING", True), ("QUEUED", False),
                                ("SUBMITTED", False), ("UNCONFIRMED", "UNEVALUATED")):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper = root / "cmux-agent"
                sentinel = root / "SHOULD_NOT_EXIST"
                helper.write_text(f'delivery_state() {{\n  echo {state}\n}}\ntouch "{sentinel}"\n')
                original = helper.read_bytes()
                with mock.patch.object(HARNESS, "find_external_helper", return_value=helper):
                    result = HARNESS.check_helper_parity()
                self.assertEqual(result["function_name"], "delivery_state")
                self.assertTrue(result["function_found"])
                self.assertEqual(len(result["state_observations"]), len(HARNESS.HELPER_PARITY_FIXTURES))
                self.assertTrue(all(x["state"] == state for x in result["state_observations"]))
                self.assertTrue(all(x["helper"] == expected for x in result["divergences"]))
                if state == "UNCONFIRMED":
                    self.assertEqual(result["evaluated"], 0)
                    self.assertNotEqual(result["status"], "PARITY_OK")
                self.assertFalse(sentinel.exists())
                self.assertEqual(helper.read_bytes(), original)

    def test_unknown_state_requires_explicit_bridge_and_never_claims_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = root / "cmux-agent"
            helper.write_text('delivery_state() {\n  echo UNCONFIRMED\n}\n')
            args = SimpleNamespace(artifact_root=str(root), task_id="state-test", json=False,
                                   callback_transport="auto")
            with mock.patch.object(HARNESS, "find_external_helper", return_value=helper):
                with self.assertRaises(SystemExit):
                    HARNESS.cmd_helper_parity(args)
                args.callback_transport = "bridge"
                HARNESS.cmd_helper_parity(args)
            result = json.loads((root / "helper-parity.json").read_text())
            self.assertEqual(result["status"], "DIVERGENT")
            self.assertEqual(result["evaluated"], 0)


class HelperParityTests(unittest.TestCase):
    """The in-scope fix does not reach the external bash callback helper.

    Measured after the addendum shipped: `cmux_bridge.py` was fixed for both
    receiver glyphs and current-block-only parsing, but
    `~/.local/bin/cmux-agent` — the helper that actually carries every
    Claude->Codex callback — still had the pre-fix logic. Fixing a contract in one
    of its two implementations leaves the protocol broken on the path that
    matters. This gate detects the divergence read-only; it never edits the
    helper, which is outside the task-pack scope.
    """

    FIXED_FN = "\n".join([
        "prompt_block_pending() {",
        '  local screen="$1"',
        '  local marker="$2"',
        "  local line",
        '  local block=""',
        "  local in_prompt=0",
        '  while IFS= read -r line || [[ -n "$line" ]]; do',
        '    if [[ "$line" =~ ^[[:space:]]*(❯|›)([[:space:]]|$) ]]; then',
        "      in_prompt=1",
        '      block="$line"',
        "      continue",
        "    fi",
        '    if [[ "$line" =~ ^[[:space:]]*(⏺|✻|✢|✳|✶|✽|◐|◑|◒|◓)([[:space:]]|$) ]]; then',
        "      in_prompt=0",
        '      block=""',
        "      continue",
        "    fi",
        "    if (( in_prompt )); then",
        "      block+=$'\\n'\"$line\"",
        "    fi",
        '  done <<< "$screen"',
        '  (( in_prompt )) && [[ "$block" == *"$marker"* ]]',
        "}",
    ])

    # The shipped pre-fix shape, verbatim in structure: Claude glyph only, plus
    # the mid-loop early return that counts a transcript block as live compose.
    BROKEN_FN = "\n".join([
        "prompt_block_pending() {",
        '  local screen="$1"',
        '  local marker="$2"',
        "  local line",
        '  local block=""',
        "  local in_prompt=0",
        '  while IFS= read -r line || [[ -n "$line" ]]; do',
        '    if [[ "$line" =~ ^[[:space:]]*❯([[:space:]]|$) ]]; then',
        '      if (( in_prompt )) && [[ "$block" == *"$marker"* ]]; then',
        "        return 0",
        "      fi",
        "      in_prompt=1",
        '      block="$line"',
        "      continue",
        "    fi",
        '    if [[ "$line" =~ ^[[:space:]]*(⏺|✻|✢|✳|✶|✽|◐|◑|◒|◓)([[:space:]]|$) ]]; then',
        "      in_prompt=0",
        '      block=""',
        "      continue",
        "    fi",
        "    if (( in_prompt )); then",
        "      block+=$'\\n'\"$line\"",
        "    fi",
        '  done <<< "$screen"',
        '  (( in_prompt )) && [[ "$block" == *"$marker"* ]]',
        "}",
    ])

    def _helper(self, root: Path, function_src: str, sentinel: Path | None = None):
        """Write a synthetic helper that also has a trailing CLI dispatch."""
        path = root / "cmux-agent"
        trailing = ""
        if sentinel is not None:
            # If anything sources or executes this file, the sentinel appears.
            trailing = f'\ntouch "{sentinel}"\ncase "${{1:-}}" in *) : ;; esac\n'
        path.write_text("#!/usr/bin/env bash\n" + function_src + trailing,
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def _args(self, root: Path):
        return SimpleNamespace(artifact_root=str(root), task_id="parity-test",
                               json=False, callback_transport="auto")

    def test_fixed_helper_reaches_parity(self):
        """Control: an implementation matching the contract must pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self._helper(root, self.FIXED_FN)
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=helper):
                result = HARNESS.check_helper_parity()
            self.assertEqual(result["status"], "PARITY_OK")
            self.assertEqual(result["divergences"], [])
            self.assertEqual(result["evaluated"],
                             len(HARNESS.HELPER_PARITY_FIXTURES))

    def test_prefix_helper_is_reported_divergent(self):
        """POISON: the exact defect measured in the shipped helper.

        Both directions must be named, because they have different consequences:
        the Codex-glyph blindness can never reach the recovery Enter (permanent
        false negative), while the transcript-block defect drives a second Enter
        at an already-delivered message (duplicate risk).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self._helper(root, self.BROKEN_FN)
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=helper):
                result = HARNESS.check_helper_parity()
            self.assertEqual(result["status"], "DIVERGENT")
            fixtures = {d["fixture"] for d in result["divergences"]}
            self.assertIn("codex_stuck_in_live_compose", fixtures)
            self.assertIn("claude_delivered_transcript_then_empty_box", fixtures)
            for div in result["divergences"]:
                if div["fixture"] == "codex_stuck_in_live_compose":
                    self.assertIs(div["helper"], False)
                    self.assertIs(div["in_scope"], True)
                if div["fixture"] == "claude_delivered_transcript_then_empty_box":
                    self.assertIs(div["helper"], True)
                    self.assertIs(div["in_scope"], False)

    def test_divergence_is_fatal_at_the_gate(self):
        """A detected divergence must stop dispatch, not print a warning."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self._helper(root, self.BROKEN_FN)
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=helper):
                with self.assertRaises(SystemExit):
                    HARNESS.cmd_helper_parity(self._args(root))
            written = json.loads((root / "helper-parity.json").read_text())
            self.assertEqual(written["status"], "DIVERGENT")
            self.assertTrue(written["divergences"])

    def test_bridge_pinned_transport_can_bypass_only_the_external_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self._helper(root, self.BROKEN_FN)
            bridge_args = self._args(root)
            bridge_args.callback_transport = "bridge"
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=helper):
                HARNESS.cmd_helper_parity(bridge_args)
            written = json.loads((root / "helper-parity.json").read_text())
            self.assertEqual(written["status"], "DIVERGENT")
            self.assertTrue(written["divergences"])

    def test_check_never_executes_the_helper(self):
        """Read-only is load-bearing: the helper is out of scope to run, too.

        Sourcing it would fire its trailing CLI dispatch. The sentinel proves the
        check only ever evaluated the extracted function.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "SIDE_EFFECT_FIRED"
            helper = self._helper(root, self.FIXED_FN, sentinel=sentinel)
            before = helper.read_bytes()
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=helper):
                HARNESS.check_helper_parity()
            self.assertFalse(sentinel.exists(),
                             "the helper's trailing dispatch was executed")
            self.assertEqual(helper.read_bytes(), before,
                             "the helper was modified by a read-only check")

    def test_unparsable_helper_fails_closed(self):
        """POISON: silence when the check cannot answer.

        If the function is renamed or restructured, the honest result is
        INDETERMINATE and a non-zero exit — not a pass. An unanswerable gate that
        reports success is worse than no gate.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = self._helper(root, "some_other_function() {\n  :\n}")
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=helper):
                result = HARNESS.check_helper_parity()
                self.assertEqual(result["status"], "HELPER_FUNCTION_NOT_FOUND")
                with self.assertRaises(SystemExit):
                    HARNESS.cmd_helper_parity(self._args(root))

    def test_absent_helper_skips_rather_than_fails(self):
        """No helper installed is not a divergence; it is nothing to compare."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(HARNESS, "find_external_helper",
                                   return_value=None):
                result = HARNESS.check_helper_parity()
                self.assertEqual(result["status"], "HELPER_ABSENT")
                # Must NOT raise: absence is reported, not fatal.
                HARNESS.cmd_helper_parity(self._args(root))

    def test_fixtures_discriminate_between_the_two_implementations(self):
        """The fixture set must be able to tell the versions apart at all.

        Without this, a fixture set that both implementations agree on would make
        the gate structurally incapable of ever detecting the bug it exists for.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixed = self._helper(Path(tmp), self.FIXED_FN)
            fixed_src = HARNESS.extract_bash_function(
                fixed.read_text(encoding="utf-8"), "prompt_block_pending")
            broken_path = root / "broken.sh"
            broken_path.write_text(self.BROKEN_FN, encoding="utf-8")
            broken_src = HARNESS.extract_bash_function(
                broken_path.read_text(encoding="utf-8"), "prompt_block_pending")
            self.assertIsNotNone(fixed_src)
            self.assertIsNotNone(broken_src)

            differing = 0
            for name, screen in HARNESS.HELPER_PARITY_FIXTURES:
                a = HARNESS.helper_prompt_block_pending(
                    fixed_src, screen, HARNESS.HELPER_PARITY_MARKER)
                b = HARNESS.helper_prompt_block_pending(
                    broken_src, screen, HARNESS.HELPER_PARITY_MARKER)
                self.assertIsNotNone(a, f"{name} did not evaluate on fixed")
                self.assertIsNotNone(b, f"{name} did not evaluate on broken")
                if a != b:
                    differing += 1
            self.assertEqual(differing, 2,
                             "expected exactly the two measured divergences")

    def test_fixed_helper_agrees_with_the_python_implementation(self):
        """Parity is defined against the shipped Python, not a second opinion."""
        with tempfile.TemporaryDirectory() as tmp:
            helper = self._helper(Path(tmp), self.FIXED_FN)
            src = HARNESS.extract_bash_function(
                helper.read_text(encoding="utf-8"), "prompt_block_pending")
            for name, screen in HARNESS.HELPER_PARITY_FIXTURES:
                bash_verdict = HARNESS.helper_prompt_block_pending(
                    src, screen, HARNESS.HELPER_PARITY_MARKER)
                py_verdict = BRIDGE.compose_contains(
                    screen, HARNESS.HELPER_PARITY_MARKER)
                self.assertEqual(bash_verdict, py_verdict,
                                 f"{name}: bash={bash_verdict} python={py_verdict}")


class ActiveMarkerContractTests(unittest.TestCase):
    """The published active-marker v2 contract (schemas/active-marker.schema.json).

    The CCC Supervisor TUI renders its 协作 column from these markers and from
    nothing else, so the shape asserted here is an external API: one file per
    collaboration under _active/<workspace>/<collaboration_id>.json (concurrent
    tasks no longer overwrite each other), participants carry role+ordinal and
    surface UUIDs (refs renumber), last_activity_at heartbeats while the task
    produces evidence, and disarm removes exactly one collaboration's file.
    A drift caught here is a blank or lying column caught there.
    """

    def _patch_active(self, tmp):
        active = Path(tmp) / "_active"
        patches = (
            mock.patch.object(HARNESS, "_ACTIVE_DIR", active),
            mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": "ws-uuid-under-test"}),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return active

    def _armed(self, tmp, **kwargs):
        """arm_task under a patched _ACTIVE_DIR; returns (root, marker_path)."""
        active = self._patch_active(tmp)
        root = Path(tmp) / kwargs.pop("root_name", "artifacts")
        root.mkdir(exist_ok=True)
        marker = HARNESS.arm_task(
            kwargs.pop("task_id", "fin-contract"), root,
            supervisor="surface:153", executor="surface:104",
            supervisor_provider="codex", executor_provider="claude",
            supervisor_surface_uuid="sup-uuid",
            executor_surface_uuid=kwargs.pop("executor_surface_uuid", "exe-uuid"),
            workspace_uuid="ws-uuid-under-test", **kwargs,
        )
        path = (active / "ws-uuid-under-test"
                / f"{marker['collaboration_id']}.json")
        return root, path

    def test_arm_task_publishes_the_v2_shape_the_tui_consumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, marker_path = self._armed(tmp)
            marker = json.loads(marker_path.read_text())
        self.assertEqual(marker["marker_version"], 2)
        self.assertEqual(marker["collaboration_id"], marker_path.stem)
        self.assertEqual(marker["workspace_uuid"], "ws-uuid-under-test")
        self.assertEqual(marker["last_activity_at"], marker["armed_at"])
        by_role = {item["role"]: item for item in marker["participants"]}
        self.assertEqual(by_role["supervisor"]["ordinal"], 0)
        self.assertEqual(by_role["supervisor"]["surface_uuid"], "sup-uuid")
        self.assertEqual(by_role["supervisor"]["provider"], "codex")
        self.assertEqual(by_role["executor"]["ordinal"], 1)
        self.assertEqual(by_role["executor"]["surface_uuid"], "exe-uuid")
        self.assertEqual(by_role["executor"]["surface_ref"], "surface:104")
        self.assertGreater(marker["ttl_seconds"], 0)

    def test_multi_executor_arm_orders_ordinals_and_keeps_one_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = self._patch_active(tmp)
            root = Path(tmp) / "artifacts"
            root.mkdir()
            marker = HARNESS.arm_task(
                "fin-multi", root, supervisor="surface:153",
                supervisor_provider="codex", supervisor_surface_uuid="sup-uuid",
                workspace_uuid="ws-uuid-under-test",
                executors=[
                    {"surface_ref": "surface:104", "provider": "claude",
                     "surface_uuid": "exe-a"},
                    {"surface_ref": "surface:105", "provider": "grok",
                     "surface_uuid": "exe-b"},
                    {"surface_ref": "surface:106", "provider": "copilot",
                     "surface_uuid": "exe-c"},
                ])
            on_disk = json.loads(
                (active / "ws-uuid-under-test"
                 / f"{marker['collaboration_id']}.json").read_text())
        roles = [(p["role"], p["ordinal"], p["provider"])
                 for p in on_disk["participants"]]
        self.assertEqual(roles, [("supervisor", 0, "codex"),
                                 ("executor", 1, "claude"),
                                 ("executor", 2, "grok"),
                                 ("executor", 3, "copilot")])

    def test_arm_task_refuses_duplicate_uuids_and_zero_executors(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_active(tmp)
            root = Path(tmp) / "artifacts"
            root.mkdir()
            with self.assertRaises(ValueError):
                HARNESS.arm_task("fin-dup", root, supervisor="surface:1",
                                 supervisor_surface_uuid="same-uuid",
                                 executor_surface_uuid="same-uuid")
            with self.assertRaises(ValueError):
                HARNESS.arm_task("fin-none", root, supervisor="surface:1",
                                 supervisor_surface_uuid="sup", executors=[])

    def test_concurrent_arms_do_not_overwrite_each_other(self):
        # The v1 defect: one <workspace>.json meant the second arm silently
        # replaced the first collaboration's marker.
        with tempfile.TemporaryDirectory() as tmp:
            active = self._patch_active(tmp)
            for i in (1, 2):
                root = Path(tmp) / f"task{i}"
                root.mkdir()
                HARNESS.arm_task(f"fin-conc-{i}", root, supervisor="surface:1",
                                 supervisor_surface_uuid=f"sup-{i}",
                                 executor_surface_uuid=f"exe-{i}")
            files = list((active / "ws-uuid-under-test").glob("*.json"))
            self.assertEqual(len(files), 2)
            task_ids = {json.loads(p.read_text())["task_id"] for p in files}
        self.assertEqual(task_ids, {"fin-conc-1", "fin-conc-2"})

    def test_an_evidence_write_under_the_armed_root_beats_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, marker_path = self._armed(tmp)
            # Backdate the heartbeat so a same-second bump is still visible.
            marker = json.loads(marker_path.read_text())
            marker["last_activity_at"] = "2020-01-01T00:00:00+00:00"
            marker_path.write_text(json.dumps(marker))
            HARNESS._write(root / "receipts" / "r1.json", {"ok": True})
            after = json.loads(marker_path.read_text())["last_activity_at"]
        self.assertNotEqual(after, "2020-01-01T00:00:00+00:00")

    def test_heartbeat_routes_to_the_owning_collaboration_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_a, path_a = self._armed(tmp, task_id="fin-hb-a",
                                          root_name="task-a",
                                          executor_surface_uuid="exe-a")
            root_b, path_b = self._armed(tmp, task_id="fin-hb-b",
                                          root_name="task-b",
                                          executor_surface_uuid="exe-b")
            for p in (path_a, path_b):
                marker = json.loads(p.read_text())
                marker["last_activity_at"] = "2020-01-01T00:00:00+00:00"
                p.write_text(json.dumps(marker))
            HARNESS._write(root_b / "receipts" / "r1.json", {"ok": True})
            after_a = json.loads(path_a.read_text())["last_activity_at"]
            after_b = json.loads(path_b.read_text())["last_activity_at"]
        self.assertEqual(after_a, "2020-01-01T00:00:00+00:00",
                         "a neighbour collaboration's write must not beat")
        self.assertNotEqual(after_b, "2020-01-01T00:00:00+00:00")

    def test_writes_outside_the_root_or_inside_active_do_not_beat(self):
        """A neighbour task's artifacts must not keep this marker alive, and
        the marker write itself must not recurse into another touch."""

        with tempfile.TemporaryDirectory() as tmp:
            root, marker_path = self._armed(tmp)
            marker = json.loads(marker_path.read_text())
            marker["last_activity_at"] = "2020-01-01T00:00:00+00:00"
            marker_path.write_text(json.dumps(marker))
            HARNESS._write(Path(tmp) / "elsewhere" / "x.json", {"ok": True})
            HARNESS._write(marker_path.parent / "other-collab.json", {"ok": True})
            after = json.loads(marker_path.read_text())["last_activity_at"]
        self.assertEqual(after, "2020-01-01T00:00:00+00:00")

    def test_the_heartbeat_tmp_name_is_only_unique_per_process(self):
        """Pin the premise the heartbeat's tmp filename actually relies on.

        `_touch_active_marker` writes `.<marker>.hb.<pid>` and then os.replace()s
        it. That is collision-free between processes, and the cross-PID case is
        the one that happens in production. It is NOT collision-free between
        threads of one process: two threads would share the pid, write the same
        path, and one os.replace could publish a half-written marker.

        The harness is single-threaded today, which is what makes the pid-only
        name sufficient — so this test asserts that premise rather than the
        threading fix, and fails the moment someone adds a thread or pool and
        silently invalidates it.
        """
        src = Path(HARNESS.__file__).read_text()
        self.assertIn("hb.{os.getpid()}", src,
                      "heartbeat tmp name changed — recheck this premise")
        for token in ("import threading", "from threading",
                      "Thread(", "ThreadPool", "ProcessPool", "concurrent.futures"):
            self.assertNotIn(
                token, src,
                f"{token!r} appeared in the harness: the heartbeat tmp name is "
                "keyed on pid alone and is no longer collision-free — add a "
                "thread id or uuid4 suffix to _touch_active_marker")

    def test_two_processes_beating_at_once_never_publish_a_partial_marker(self):
        """The reachable concurrency case: distinct pids, same marker.

        Simulated by patching os.getpid, which is exactly what distinguishes the
        two tmp paths. Every observed marker must be complete JSON — a truncated
        or empty read here is the corruption the tmp+replace pattern exists to
        prevent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root, marker_path = self._armed(tmp)
            pids = [4001, 4002, 4003]
            seen = []
            for i in range(30):
                pid = pids[i % len(pids)]
                with mock.patch.object(HARNESS.os, "getpid", return_value=pid):
                    HARNESS._write(root / "receipts" / f"r{i}.json", {"i": i})
                raw = marker_path.read_text()
                seen.append(json.loads(raw)["task_id"])
                self.assertGreater(len(raw), 0)
            self.assertEqual(len(seen), 30)
            leftovers = list(marker_path.parent.glob(".*.hb.*"))
            self.assertEqual(leftovers, [],
                             f"tmp files must be replaced, not left: {leftovers}")

    def test_heartbeat_still_beats_a_legacy_v1_marker(self):
        # v1 single-file markers stay read-compatible during the migration
        # window; an armed v1 task must not look stalled just because the
        # writer was upgraded underneath it.
        with tempfile.TemporaryDirectory() as tmp:
            active = self._patch_active(tmp)
            root = Path(tmp) / "artifacts"
            root.mkdir()
            legacy = active / "ws-uuid-under-test.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({
                "marker_version": 1, "task_id": "fin-v1",
                "artifact_root": str(root),
                "last_activity_at": "2020-01-01T00:00:00+00:00"}))
            HARNESS._write(root / "receipts" / "r1.json", {"ok": True})
            after = json.loads(legacy.read_text())["last_activity_at"]
        self.assertNotEqual(after, "2020-01-01T00:00:00+00:00")

    def test_disarm_removes_the_marker_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, marker_path = self._armed(tmp)
            self.assertTrue(marker_path.exists())
            self.assertEqual(HARNESS.disarm_task(), 1)
            self.assertFalse(marker_path.exists())
            self.assertEqual(HARNESS.disarm_task(), 0)

    def test_bare_disarm_refuses_to_guess_between_collaborations(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, path_a = self._armed(tmp, task_id="fin-dis-a", root_name="task-a",
                                    executor_surface_uuid="exe-a")
            _, path_b = self._armed(tmp, task_id="fin-dis-b", root_name="task-b",
                                    executor_surface_uuid="exe-b")
            with self.assertRaises(ValueError):
                HARNESS.disarm_task()
            self.assertTrue(path_a.exists() and path_b.exists(),
                            "a refused disarm must not delete anything")
            self.assertEqual(HARNESS.disarm_task("fin-dis-a"), 1)
            self.assertFalse(path_a.exists())
            self.assertTrue(path_b.exists(),
                            "scoped disarm must only remove its own marker")

    # -- cmd_disarm, through the real CLI entry point ----------------------
    #
    # The two tests above call disarm_task() directly, which cannot see the
    # defect these cover: cmd_disarm decides whether to fall back to an
    # unscoped delete, and that decision is invisible from the library. A
    # wrong --task-id used to delete a bystander marker whenever exactly one
    # existed, because "no such task" fell through to "delete the only one".

    def _disarm(self, root, task_id, explicit=True):
        """Run cmd_disarm; return (exit_code_or_None, combined output).

        Both streams are captured: _ok/_info print to stdout while _fail
        prints to stderr, and a test that watched only one would miss half
        the observable behaviour.
        """
        buf = io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                HARNESS.cmd_disarm(args(root, task_id=task_id,
                                        task_id_explicit=explicit))
        except SystemExit as e:
            code = e.code
        return code, buf.getvalue()

    def test_cmd_disarm_wrong_task_id_deletes_nothing_with_one_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, path_a = self._armed(tmp, task_id="fin-keep-a")
            code, out = self._disarm(root, "no-such-task")
            self.assertIsNone(code)
            self.assertTrue(path_a.exists(),
                            "an explicitly named absent task must not delete the bystander")
            self.assertEqual(len(HARNESS._workspace_marker_paths()), 1)
            self.assertIn("no marker for task no-such-task", out)
            self.assertIn("1 other marker(s) untouched", out)

    def test_cmd_disarm_wrong_task_id_deletes_nothing_with_two_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, path_a = self._armed(tmp, task_id="fin-keep-a", root_name="task-a",
                                       executor_surface_uuid="exe-a")
            _, path_b = self._armed(tmp, task_id="fin-keep-b", root_name="task-b",
                                    executor_surface_uuid="exe-b")
            code, out = self._disarm(root, "no-such-task")
            self.assertIsNone(code)
            self.assertTrue(path_a.exists() and path_b.exists())
            self.assertEqual(len(HARNESS._workspace_marker_paths()), 2)
            self.assertIn("2 other marker(s) untouched", out)

    def test_cmd_disarm_default_task_id_still_removes_a_lone_marker(self):
        """The zero-friction path: no --task-id given, exactly one marker."""
        with tempfile.TemporaryDirectory() as tmp:
            root, path_a = self._armed(tmp, task_id="fin-default-a")
            code, out = self._disarm(root, HARNESS.DEFAULT_TASK_ID, explicit=False)
            self.assertIsNone(code)
            self.assertFalse(path_a.exists())
            self.assertEqual(HARNESS._workspace_marker_paths(), [])
            self.assertIn("disarmed 1 task marker(s)", out)

    def test_cmd_disarm_explicit_default_task_id_never_deletes_bystander(self):
        """An explicit default-valued option is still an explicit scope."""
        with tempfile.TemporaryDirectory() as tmp:
            root, path_a = self._armed(tmp, task_id="fin-explicit-default")
            code, out = self._disarm(root, HARNESS.DEFAULT_TASK_ID, explicit=True)
            self.assertIsNone(code)
            self.assertTrue(path_a.exists())
            self.assertEqual(len(HARNESS._workspace_marker_paths()), 1)
            self.assertIn("no marker for task multi-agent-task", out)
            self.assertIn("1 other marker(s) untouched", out)

    def test_cmd_disarm_default_task_id_refuses_to_guess_between_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, path_a = self._armed(tmp, task_id="fin-amb-a", root_name="task-a",
                                       executor_surface_uuid="exe-a")
            _, path_b = self._armed(tmp, task_id="fin-amb-b", root_name="task-b",
                                    executor_surface_uuid="exe-b")
            code, out = self._disarm(root, HARNESS.DEFAULT_TASK_ID, explicit=False)
            self.assertEqual(code, 1)
            self.assertTrue(path_a.exists() and path_b.exists(),
                            "a refused disarm must delete nothing")
            self.assertEqual(len(HARNESS._workspace_marker_paths()), 2)
            self.assertIn("--task-id", out)

    def test_cmd_disarm_correct_task_id_removes_only_that_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, path_a = self._armed(tmp, task_id="fin-hit-a", root_name="task-a",
                                       executor_surface_uuid="exe-a")
            _, path_b = self._armed(tmp, task_id="fin-hit-b", root_name="task-b",
                                    executor_surface_uuid="exe-b")
            code, out = self._disarm(root, "fin-hit-a")
            self.assertIsNone(code)
            self.assertFalse(path_a.exists())
            self.assertTrue(path_b.exists())
            self.assertEqual(len(HARNESS._workspace_marker_paths()), 1)
            self.assertIn("disarmed 1 task marker(s)", out)

    def test_successful_disarm_emits_conditional_status_sync_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, marker_path = self._armed(tmp, task_id="fin-sync")
            code, out = self._disarm(root, "fin-sync")
            self.assertIsNone(code)
            self.assertFalse(marker_path.exists())
            self.assertIn("next_action=SUPERVISOR_STATUS_SYNC_IF_STALE", out)
            self.assertIn("if a settled report's executor repeats", out)
            self.assertIn("not proof of tool recovery", out)

    def test_absent_marker_does_not_emit_a_new_status_sync_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, marker_path = self._armed(tmp, task_id="fin-sync-once")
            self.assertEqual(HARNESS.disarm_task("fin-sync-once"), 1)
            code, out = self._disarm(root, "fin-sync-once")
            self.assertIsNone(code)
            self.assertFalse(marker_path.exists())
            self.assertIn("nothing disarmed", out)
            self.assertNotIn("next_action=", out)

    def test_default_task_id_matches_the_argparse_default(self):
        """Poison case for constant drift.

        The sentinel comparison in cmd_disarm is only meaningful while the
        constant equals what argparse actually installs. If they drift, every
        real invocation carries a task_id that never equals DEFAULT_TASK_ID,
        the explicit-id branch swallows the default case, and the
        zero-friction path silently stops working.
        """
        parser = HARNESS.build_parser() if hasattr(HARNESS, "build_parser") else None
        if parser is None:
            src = Path(HARNESS.__file__).read_text()
            self.assertIn('p.add_argument("--task-id",          default=DEFAULT_TASK_ID)', src,
                          "argparse must reference the constant, not a duplicated literal")
            self.assertNotIn('add_argument("--task-id", default="multi-agent-task"', src)
        else:
            self.assertEqual(parser.parse_args([]).task_id, HARNESS.DEFAULT_TASK_ID)

    def test_surface_uuid_map_reads_the_both_format_tree_and_fails_empty(self):
        tree = {"windows": [{"workspaces": [{
            "id": "ws-uuid", "ref": "workspace:7",
            "panes": [{"surfaces": [
                {"ref": "surface:153", "id": "sup-uuid", "type": "terminal",
                 "pane_ref": "pane:9", "title": "codex"},
                {"ref": "surface:104", "id": "", "title": "no-uuid-yet"},
            ]}],
        }]}]}
        with mock.patch.object(BRIDGE, "_run", return_value=json.dumps(tree)):
            mapping = BRIDGE.surface_uuid_map()
        self.assertEqual(mapping["surface:153"]["surface_id"], "sup-uuid")
        self.assertEqual(mapping["surface:153"]["workspace_id"], "ws-uuid")
        self.assertNotIn("surface:104", mapping)   # no UUID -> no entry, no guess
        with mock.patch.object(BRIDGE, "_run", side_effect=RuntimeError("down")):
            self.assertEqual(BRIDGE.surface_uuid_map(), {})


class MultiExecutorGateTests(OfflineWorkspaceFixture):
    """One supervisor with N executors, driven through the real entry points.

    The defect this class exists to catch is a marker that names three
    executors while only the first one has bridge, handshake, or round
    evidence. That state is worse than refusing outright: the marker and the
    TUI both assert a three-agent review that never happened. So every gate
    below is asserted per executor, not once for the panel.

    Everything runs through `cmd_identity_gate`, `cmd_bridge_test`,
    `cmd_handshake`, `cmd_name_surfaces`, `cmd_map`, `cmd_validate`, and
    `cmd_task_pack` — never the helpers alone, because the single-executor
    versions of these helpers were already correct and the wiring was not.
    """

    SUP = "surface:1"

    @staticmethod
    def uid(label):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, label)).upper()

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        reg = self.root / "_registry"
        reg.mkdir()
        p = mock.patch.object(HARNESS, "ARTIFACT_REGISTRY_DIR", reg)
        p.start()
        self.addCleanup(p.stop)
        active = mock.patch.object(HARNESS, "_ACTIVE_DIR", self.root / "_active")
        active.start()
        self.addCleanup(active.stop)
        env = mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": self.uid("workspace:10"),
                                           "CMUX_SURFACE_ID": self.uid(self.SUP)})
        env.start()
        self.addCleanup(env.stop)

    # -- fixtures ----------------------------------------------------------

    def _identify(self, panes):
        """cmux side of identity: supervisor in pane:0, executors per `panes`."""
        def side(ref, workspace=None, window=None):
            if ref not in panes:
                raise RuntimeError(f"unknown surface {ref}")
            return {"workspace_ref": "workspace:10", "surface_ref": ref,
                    "surface_type": "terminal", "pane_ref": panes[ref],
                    "window_ref": "window:1"}
        return side

    def _run_gate(self, refs, panes=None, provider="codex", proof_overrides=None, aliases=None):
        """cmd_identity_gate with N explicit executor refs."""
        aliases = aliases or {}
        plain = [aliases.get(r.partition("=")[0], r.partition("=")[0]) for r in refs]
        panes = panes or {r: f"pane:{i + 1}" for i, r in enumerate(plain)}
        uuid_map = {r: {"surface_id": self.uid(r),
                        "workspace_id": self.uid("workspace:10")} for r in plain}
        uuid_map[self.SUP] = {"surface_id": self.uid(self.SUP), "workspace_id": self.uid("workspace:10")}
        def pin(ref, **kwargs):
            ref = aliases.get(ref, ref)
            proof = dict(workspace_uuid=self.uid("workspace:10"), workspace_ref="workspace:10",
                        caller_surface_uuid=self.uid(self.SUP), caller_surface_ref=self.SUP,
                        caller_pane_uuid=self.uid("pane:0"),
                        target_surface_uuid=self.uid(ref), target_surface_ref=ref,
                        target_pane_uuid=self.uid(panes.get(ref, "unknown")))
            proof.update((proof_overrides or {}).get(ref, {}))
            return proof
        with (
            mock.patch.object(HARNESS.cmux, "pin_workspace", side_effect=pin),
            mock.patch.object(HARNESS.cmux, "whoami", return_value={
                "workspace_ref": "workspace:10", "surface_ref": self.SUP,
                "surface_type": "terminal", "pane_ref": "pane:0",
                "window_ref": "window:1", "workspace_id": self.uid("workspace:10"),
                "surface_id": self.uid(self.SUP)}),
            mock.patch.object(HARNESS.cmux, "identify_surface",
                              side_effect=lambda ref, **kw: self._identify(panes)(aliases.get(ref, ref), **kw)),
            mock.patch.object(HARNESS.cmux, "surface_uuid_map", return_value=uuid_map),
            mock.patch.object(HARNESS.cmux, "list_surfaces", return_value=[]),
            mock.patch("cmux_hook_scope.bind_participants", side_effect=lambda bridge, workspace, rows: [
                dict(row, native_session_id=f"00000000-0000-4000-8000-{i:012d}")
                for i, row in enumerate(rows, start=1)
            ]),
        ):
            HARNESS.cmd_identity_gate(args(
                self.root, executor_surface=list(refs), executor=provider,
                supervisor="codex", spawn=False, spawn_authorized=False,
                direction="right", cwd=str(self.root), task_id="multi-exec"))
        return json.loads((self.root / "identity-gate.json").read_text())

    def _gate_args(self, **over):
        over.setdefault("task_id", "multi-exec")
        return args(self.root, **over)

    # -- identity-gate: 1S+1E, 1S+2E, 1S+3E --------------------------------

    def test_single_executor_gate_keeps_the_legacy_shape(self):
        """1S+1E: the singular fields must read exactly as they did before."""
        g = self._run_gate(["surface:2"])
        self.assertEqual(g["status"], "PASS")
        self.assertEqual(g["executor"], "surface:2")
        self.assertEqual(g["executor_provider"], "codex")
        self.assertEqual(g["executor_pane_ref"], "pane:1")
        self.assertEqual(g["executor_workspace_ref"], "workspace:10")
        self.assertEqual(g["executor_surface_uuid"], self.uid("surface:2"))
        # The new list exists even for one executor, so readers never have to
        # branch on its absence.
        self.assertEqual(len(g["executors"]), 1)
        self.assertEqual(g["executors"][0]["surface_ref"], "surface:2")

    def test_uuid_selectors_resolve_to_canonical_refs_and_keep_provider(self):
        uid = self.uid("surface:2")
        gate = self._run_gate([uid + "=claude", "surface:3=codex"], aliases={uid: "surface:2"})
        self.assertEqual(gate["executor"], "surface:2")
        self.assertEqual(gate["executor_surface_uuid"], uid)
        self.assertEqual(gate["executor_provider"], "claude")
        self.assertEqual(set(gate["workspace_proofs"]), {"surface:2", "surface:3"})
        self.assertTrue(gate["terminal_channels"]["pass"])

    def test_uuid_and_ref_alias_cannot_count_as_two_executors(self):
        uid = self.uid("surface:2")
        with self.assertRaises(SystemExit):
            self._run_gate([uid, "surface:2"], aliases={uid: "surface:2"})
        self.assertFalse(list((self.root / "_active").glob("**/*.json")))

    def test_two_executors_are_both_armed_in_one_marker(self):
        g = self._run_gate(["surface:2", "surface:3"])
        self.assertEqual([e["surface_ref"] for e in g["executors"]],
                         ["surface:2", "surface:3"])
        # active-marker.schema.json retires executor2/executor3 role enums:
        # every executor is role "executor" and order lives in ordinal. Keying
        # on ordinal here rather than on array position, per that schema.
        marker = json.loads(next(
            (self.root / "_active" / self.uid("workspace:10")).glob("*.json")).read_text())
        by_ord = {p["ordinal"]: p for p in marker["participants"]}
        self.assertEqual(by_ord[0]["role"], "supervisor")
        self.assertEqual(by_ord[1]["surface_uuid"], self.uid("surface:2"))
        self.assertEqual(by_ord[2]["surface_uuid"], self.uid("surface:3"))
        self.assertEqual({p["role"] for p in marker["participants"]},
                         {"supervisor", "executor"})

    def test_three_heterogeneous_providers_survive_to_the_marker(self):
        """1S+3E with Codex + Claude + Grok, each keeping its own provider.

        A panel-wide --executor would stamp all three with one provider, and
        the role map, task pack, and TUI would then name an agent that is not
        the one being addressed.
        """
        g = self._run_gate(
            ["surface:2=codex", "surface:3=claude", "surface:4=grok"],
            provider="codex")
        self.assertEqual([e["provider"] for e in g["executors"]],
                         ["codex", "claude", "grok"])
        self.assertEqual(g["executor_provider"], "codex")
        marker = json.loads(next(
            (self.root / "_active" / self.uid("workspace:10")).glob("*.json")).read_text())
        by_ord = {p["ordinal"]: p for p in marker["participants"]}
        self.assertEqual(by_ord[0]["provider"], "codex")
        self.assertEqual(by_ord[1]["provider"], "codex")
        self.assertEqual(by_ord[2]["provider"], "claude")
        self.assertEqual(by_ord[3]["provider"], "grok")

    def test_arm_task_is_called_once_with_every_executor(self):
        """POISON: N calls to arm_task, or one call carrying one executor.

        Either would leave a marker that describes fewer agents than the gate
        validated. The gate must arm the whole panel in a single write.
        """
        with mock.patch.object(HARNESS, "arm_task",
                               wraps=HARNESS.arm_task) as armed:
            self._run_gate(["surface:2", "surface:3", "surface:4"])
        armed.assert_called_once()
        passed = armed.call_args.kwargs["executors"]
        self.assertEqual([e["surface_ref"] for e in passed],
                         ["surface:2", "surface:3", "surface:4"])
        # The legacy single keyword must not also be set, or arm_task would
        # have two competing sources for executor 1.
        self.assertFalse(armed.call_args.kwargs.get("executor"))

    # -- identity-gate poison cases: no marker on any failure ---------------

    def _markers(self):
        d = self.root / "_active" / self.uid("workspace:10")
        return sorted(d.glob("*.json")) if d.exists() else []

    def _expect_gate_failure(self, refs, reason, panes=None):
        """Run the gate expecting exit 1, a FAIL gate, and zero markers."""
        with self.assertRaises(SystemExit) as ctx:
            self._run_gate(refs, panes=panes)
        self.assertEqual(ctx.exception.code, 1)
        g = json.loads((self.root / "identity-gate.json").read_text())
        self.assertEqual(g["status"], "FAIL")
        self.assertEqual(g["reason"], reason)
        # The load-bearing assertion: a rejected panel arms nothing at all.
        self.assertEqual(self._markers(), [],
                         "a failed identity-gate must not arm a marker")
        return g

    def test_two_existing_executors_sharing_one_pane_are_supported(self):
        code, _, _, _ = self._handshake(["surface:2", "surface:3", "surface:4"],
                           panes={"surface:2": "pane:1", "surface:3": "pane:2",
                                  "surface:4": "pane:2"})
        self.assertIsNone(code)
        g = json.loads((self.root / "identity-gate.json").read_text())
        self.assertEqual(g["status"], "PASS")
        self.assertFalse(g["side_panel"])
        self.assertEqual(g["panel_mode"], "existing-terminal-tabs")
        self.assertEqual(g["terminal_channels"]["shared_panes"], ["pane:2"])
        self.assertTrue(self._markers())
        with mock.patch.object(HARNESS.cmux, "rename_tab"):
            HARNESS.cmd_name_surfaces(self._gate_args())
        validated = self._validate()
        self.assertEqual(validated["status"], "PASS")
        self.assertTrue(validated["checks"]["terminal_channels"]["pass"])
        self.assertEqual(validated["checks"]["side_panel"]["status"], "SHARED_PANE")

    def test_existing_executor_in_supervisor_pane_is_supported(self):
        code, _, _, _ = self._handshake(["surface:2", "surface:3"],
                           panes={"surface:2": "pane:1", "surface:3": "pane:0"})
        self.assertIsNone(code)
        g = json.loads((self.root / "identity-gate.json").read_text())
        self.assertEqual(g["terminal_channels"]["shared_panes"], ["pane:0"])
        self.assertFalse(g["side_panel"])
        with mock.patch.object(HARNESS.cmux, "rename_tab"):
            HARNESS.cmd_name_surfaces(self._gate_args())
        validated = self._validate()
        self.assertEqual(validated["status"], "PASS")

    def test_duplicate_uuid_or_invalid_channel_proof_never_arms(self):
        for changes in ({"target_surface_uuid": self.uid(self.SUP)},
                        {"target_surface_uuid": "not-a-uuid"},
                        {"target_pane_uuid": self.uid("pane:1")},
                        {"caller_pane_uuid": self.uid("different-pane")}):
            with self.subTest(changes=changes), self.assertRaises(SystemExit):
                self._run_gate(["surface:2", "surface:3"],
                               proof_overrides={"surface:3": changes})
            self.assertEqual(self._markers(), [])
            gate = json.loads((self.root / "identity-gate.json").read_text())
            self.assertEqual(gate["reason"], "EXECUTOR_CHANNEL_IDENTITY_INVALID")

    def test_new_channel_evidence_cannot_use_legacy_layout_fallback(self):
        g = self._run_gate(["surface:2", "surface:3"])
        from cmux_terminal_channels import check_gate
        for key in ("workspace_proofs", "supervisor_pane_uuid", "executors"):
            changed = dict(g)
            del changed[key]
            self.assertFalse(check_gate(changed)["pass"])
        for version in (True, None, 2):
            self.assertFalse(check_gate(dict(g, terminal_channels_version=version))["pass"])

    def test_the_same_ref_passed_twice_is_refused(self):
        """POISON: --executor-surface surface:2 --executor-surface surface:2.

        Accepting it would arm a two-executor marker naming one agent, and the
        duplicate-UUID invariant inside arm_task would then raise after the
        gate had already reported PASS.
        """
        g = self._expect_gate_failure(
            ["surface:2", "surface:2"], "DUPLICATE_EXECUTOR_SURFACE")
        self.assertEqual(g["duplicates"], ["surface:2"])

    def test_supervisor_listed_as_its_own_executor_is_refused(self):
        self._expect_gate_failure([self.SUP, "surface:3"], "EXECUTOR_IS_SUPERVISOR")

    def test_one_unidentifiable_executor_fails_the_whole_panel(self):
        """POISON: two good executors and one that cmux cannot identify.

        Arming the two that worked is the forbidden half-finished state: the
        marker would claim three reviewers and carry evidence for two.
        """
        g = self._expect_gate_failure(
            ["surface:2", "surface:3", "surface:99"],
            "EXECUTOR_IDENTITY_UNAVAILABLE",
            panes={"surface:2": "pane:1", "surface:3": "pane:2"})
        self.assertEqual(g["failed_executor"], "surface:99")

    # -- bridge-test: one token per executor --------------------------------

    def _bridge(self, refs, screens=None, fail_on=None):
        """cmd_bridge_test over an armed N-executor gate.

        `screens` maps surface_ref -> list of read_screen returns. Default is
        the healthy empty/token/cleared cycle, with `fail_on` naming a surface
        whose token never clears.
        """
        self._run_gate(refs)
        # Refs may carry a provider (surface:3=claude); the harness only ever
        # sees the bare surface, so index tokens by the bare ref.
        bare = [r.split("=", 1)[0] for r in refs]
        sent = []

        def read(surface, lines=None, **kw):
            seq = screens[surface] if screens else None
            if seq is None:
                token = f"B{bare.index(surface) + 1}_01234567"
                if surface == fail_on:
                    seq = [claude_editor()] + [claude_editor(token)] * 12
                else:
                    seq = [claude_editor(), claude_editor(token), claude_editor()]
                screens_cache.setdefault(surface, list(seq))
                seq = screens_cache[surface]
            return seq.pop(0) if len(seq) > 1 else seq[0]

        screens_cache = {}
        code = None
        with (
            mock.patch.object(HARNESS.cmux, "send_text",
                              side_effect=lambda s, t, **k: sent.append((s, t))),
            mock.patch.object(HARNESS.cmux, "send_key"),
            mock.patch.object(HARNESS.cmux, "read_screen", side_effect=read),
            mock.patch.object(HARNESS.time, "sleep"),
        ):
            try:
                HARNESS.cmd_bridge_test(self._gate_args())
            except SystemExit as e:
                code = e.code
        ev = json.loads((self.root / "bridge-test-evidence.json").read_text())
        return code, ev, sent

    def test_each_executor_gets_its_own_bridge_token(self):
        """A shared token lets executor 1's echo prove executor 3's channel."""
        code, ev, sent = self._bridge(["surface:2", "surface:3", "surface:4"])
        self.assertIsNone(code)
        self.assertEqual([s for s, _ in sent],
                         ["surface:2", "surface:3", "surface:4"])
        tokens = [t for _, t in sent]
        self.assertEqual(len(set(tokens)), 3, f"tokens must be distinct: {tokens}")
        self.assertEqual(tokens, ["B1_01234567",
                                  "B2_01234567",
                                  "B3_01234567"])
        self.assertEqual(len(ev["executors"]), 3)
        self.assertTrue(all(e["clear_confirmed"] for e in ev["executors"]))
        # Singular top-level fields still describe executor 1.
        self.assertEqual(ev["executor"], "surface:2")
        self.assertTrue(ev["clear_confirmed"])

    def test_single_executor_records_its_actual_probe_token(self):
        """1S+1E: consumers read the actual token from evidence, not a task-name formula."""
        code, ev, sent = self._bridge(["surface:2"])
        self.assertIsNone(code)
        self.assertEqual([t for _, t in sent], ["B1_01234567"])
        self.assertEqual(ev["token"], "B1_01234567")
        self.assertEqual(len(ev["executors"]), 1)

    def test_one_stuck_executor_fails_the_bridge_and_records_the_others(self):
        """POISON: executor 2 never clears. The run must not report success."""
        code, ev, sent = self._bridge(["surface:2", "surface:3", "surface:4"],
                                      fail_on="surface:3")
        self.assertEqual(code, 1)
        # Aborted at executor 2, so executor 3 was never typed into.
        self.assertNotIn("surface:4", [s for s, _ in sent])
        self.assertEqual([e["executor"] for e in ev["executors"]],
                         ["surface:2", "surface:3"])
        self.assertTrue(ev["executors"][0]["clear_confirmed"])
        self.assertFalse(ev["executors"][1]["clear_confirmed"])

    # -- handshake: one nonce per executor ----------------------------------

    def _handshake(self, refs, answer=None, panes=None):
        """cmd_handshake over an armed + bridge-tested N-executor gate.

        The real cmux.wait_for_ack is left in place and fed through read_screen.
        Mocking wait_for_ack would delete the nonce comparison that decides
        whether an ACK belongs to this executor, so the poison cases below would
        pass no matter what the harness sent.

        `answer` maps surface_ref -> a callable(nonces_by_surface) -> screen
        text. The default answer returns that surface's own correct ACK line.
        """
        if panes is None:
            self._bridge(refs)
        else:
            self._run_gate(refs, panes=panes)
        # Each executor must ACK under ITS OWN provider: the expected line embeds
        # `<provider>:identity`, so a heterogeneous panel cannot be answered with
        # one provider string.
        providers = {e["surface_ref"]: e["provider"] for e in
                     json.loads((self.root / "identity-gate.json").read_text())["executors"]}
        nonces = {}
        reads = []

        def submit(surface, _msg, marker=None, **_kw):
            nonces[surface] = marker

        def screen(surface, **_kw):
            reads.append(surface)
            make = (answer or {}).get(surface)
            if make is not None:
                return make(nonces)
            # The claude provider additionally requires the match window to sit
            # inside an assistant response block (`⏺`) and rejects any window
            # containing the user-prompt marker, so a bare ACK line would fail
            # for a claude executor while passing for codex. Emit the block
            # marker for every provider: it is what a real screen looks like.
            return ("⏺ acknowledging the handshake\n"
                    f"PREFLIGHT_ACK|multi-exec|{providers[surface]}:identity|"
                    f"READY|INLINE|{nonces.get(surface)}")

        # A fake clock so a non-ACKing executor times out after two polls
        # instead of burning the real 600 s budget.
        clock = [1000.0]

        def tick():
            clock[0] += 250.0
            return clock[0]

        code = None
        with (
            mock.patch.object(HARNESS.cmux, "submit_text", side_effect=submit),
            mock.patch.object(HARNESS.cmux, "read_screen", side_effect=screen),
            mock.patch.object(HARNESS.cmux.time, "time", side_effect=tick),
            mock.patch.object(HARNESS.cmux.time, "sleep"),
        ):
            try:
                HARNESS.cmd_handshake(self._gate_args())
            except SystemExit as e:
                code = e.code
        r = json.loads((self.root / "handshake-receipt.json").read_text())
        return code, r, nonces, reads

    def test_each_executor_is_challenged_with_its_own_nonce(self):
        code, r, nonces, _ = self._handshake(
            ["surface:2", "surface:3", "surface:4"])
        self.assertIsNone(code)
        self.assertEqual(sorted(nonces), ["surface:2", "surface:3", "surface:4"])
        issued = list(nonces.values())
        self.assertTrue(all(issued), f"every executor needs a nonce: {nonces}")
        self.assertEqual(len(set(issued)), 3,
                         f"one nonce per executor, got {issued}")
        self.assertEqual(r["executor_count"], 3)
        self.assertTrue(r["all_executors_acked"])
        self.assertEqual([e["ack_nonce"] for e in r["executors"]], issued)
        self.assertEqual([e["ordinal"] for e in r["executors"]], [1, 2, 3])
        # Each receipt's own nonce must appear in its own ACK line.
        for e in r["executors"]:
            self.assertIn(e["ack_nonce"], e["ack_line"])
            self.assertEqual(e["ack_source"], "executor_response_nonce")
        # Singular fields still describe executor 1 for the receipt guard hook,
        # validate's handshake_strict, and the receipt command.
        self.assertEqual(r["executor"], "surface:2")
        self.assertEqual(r["ack_nonce"], nonces["surface:2"])
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(r["executor_ack"])

    def test_executor_two_replaying_executor_ones_nonce_is_refused(self):
        """POISON: executor 2's screen shows executor 1's ACK line verbatim.

        This is the concrete way a shared challenge would produce a fake
        three-agent panel: one agent's reply satisfying another's gate. The ACK
        is well-formed, so only the nonce distinguishes it.
        """
        code, r, nonces, _ = self._handshake(
            ["surface:2", "surface:3"],
            answer={"surface:3": lambda ns: (
                "PREFLIGHT_ACK|multi-exec|codex:identity|READY|INLINE|"
                f"{ns['surface:2']}")},
        )
        self.assertEqual(code, 1)
        self.assertNotEqual(nonces["surface:2"], nonces["surface:3"])
        self.assertFalse(r["all_executors_acked"])
        self.assertEqual([e["executor_ack"] for e in r["executors"]],
                         [True, False])
        self.assertEqual(r["executors"][1]["ack_state"], "HANDSHAKE_TIMEOUT")
        self.assertTrue(r["executors"][1]["attributable_to_executor"])

    def test_one_silent_executor_fails_the_handshake_for_the_panel(self):
        """POISON: executor 2 never ACKs.

        The receipt must not report PASS for the panel, and executor 3 must not
        be handshaken behind a panel that is already broken.
        """
        code, r, nonces, reads = self._handshake(
            ["surface:2", "surface:3", "surface:4"],
            answer={"surface:3": lambda _ns: "working on it, no ACK yet"},
        )
        self.assertEqual(code, 1)
        self.assertNotIn("surface:4", reads)
        self.assertNotIn("surface:4", nonces)
        self.assertFalse(r["all_executors_acked"])
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual([e["executor_ack"] for e in r["executors"]],
                         [True, False])
        self.assertEqual(r["executors"][1]["status"], "FAIL")
        self.assertEqual(r["executors"][1]["lifecycle"], "EXPIRED")
        # Executor 1 genuinely ACKed; its evidence stays intact and truthful.
        self.assertEqual(r["executors"][0]["lifecycle"], "ACKED")

    def test_a_partly_acked_panel_cannot_pass_handshake_strict(self):
        """The top-level verdict is the panel's, not executor 1's.

        validate's handshake_strict reads the top-level status/executor_ack, not
        executors[]. Copying executor 1's fields verbatim therefore published
        PASS for a panel whose second executor never replied — the marker would
        name two reviewers and the gate would clear on one ACK.
        """
        code, r, _, _ = self._handshake(
            ["surface:2", "surface:3"],
            answer={"surface:3": lambda _ns: "still thinking"},
        )
        self.assertEqual(code, 1)
        self.assertEqual(r["status"], "FAIL")
        self.assertFalse(r["executor_ack"])
        self.assertEqual(r["unproven_executors"], ["surface:3"])
        # Executor 1's own entry keeps its true PASS: the panel failed, it did not.
        self.assertEqual(r["executors"][0]["status"], "PASS")

        with (
            mock.patch.object(HARNESS.cmux, "read_screen", return_value=""),
            self.assertRaises(SystemExit) as ctx,
        ):
            HARNESS.cmd_validate(self._gate_args())
        self.assertEqual(ctx.exception.code, 1)
        v = json.loads((self.root / "validation.json").read_text())
        self.assertFalse(v["checks"]["handshake_strict"]["pass"])
        self.assertNotEqual(v["status"], "PASS")

    # -- visible labels: one row per executor -------------------------------

    def test_every_executor_is_visibly_named_by_its_ordinal(self):
        """The user identifies roles by tab label alone.

        Three executors rendering as one EXECUTOR_1 (or two of them going
        unnamed) breaks the only role signal visible without reading JSON.
        """
        self._run_gate(["surface:2=codex", "surface:3=claude", "surface:4=grok"])
        with mock.patch.object(HARNESS.cmux, "rename_tab") as rename:
            HARNESS.cmd_name_surfaces(self._gate_args())
        labelled = [c.args for c in rename.call_args_list]
        self.assertEqual(labelled, [
            ("surface:1", "SUPERVISOR | codex | surface:1"),
            ("surface:2", "EXECUTOR_1 | codex | surface:2"),
            ("surface:3", "EXECUTOR_2 | claude | surface:3"),
            ("surface:4", "EXECUTOR_3 | grok | surface:4"),
        ])
        proof = json.loads((self.root / "naming-proof.json").read_text())
        self.assertEqual(proof["status"], "PASS")
        self.assertEqual([e["role"] for e in proof["entries"]],
                         ["supervisor", "executor", "executor2", "executor3"])

    def test_one_failed_rename_fails_the_naming_proof(self):
        """POISON: executor 3's tab cannot be renamed.

        A partly named panel is a partly identifiable panel, so the proof must
        not report PASS for it.
        """
        self._run_gate(["surface:2", "surface:3", "surface:4"])

        def rename(ref, _label, **_pins):
            if ref == "surface:4":
                raise RuntimeError("rename-tab: surface gone")

        with (
            mock.patch.object(HARNESS.cmux, "rename_tab", side_effect=rename),
            self.assertRaises(SystemExit) as ctx,
        ):
            HARNESS.cmd_name_surfaces(self._gate_args())
        self.assertEqual(ctx.exception.code, 1)
        proof = json.loads((self.root / "naming-proof.json").read_text())
        self.assertEqual(proof["status"], "FAIL")
        self.assertEqual([e["status"] for e in proof["entries"]],
                         ["PASS", "PASS", "PASS", "FAIL"])

    def test_role_map_labels_every_executor_by_ordinal(self):
        self._run_gate(["surface:2=codex", "surface:3=claude"])
        HARNESS.cmd_map(self._gate_args(json=False))
        rm = json.loads((self.root / "role-map.json").read_text())
        self.assertEqual(rm["supervisor"]["display"], "SUPERVISOR")
        # `executor` stays the role name for the first one, so readers that know
        # only that key keep working.
        self.assertEqual(rm["executor"]["surface_ref"], "surface:2")
        self.assertEqual(rm["executor"]["display"], "EXECUTOR_1")
        self.assertEqual(rm["executor2"]["surface_ref"], "surface:3")
        self.assertEqual(rm["executor2"]["provider"], "claude")
        self.assertEqual(rm["executor2"]["display"], "EXECUTOR_2")

    # -- validate: every executor needs a consistent terminal identity -------

    def _validate(self, expect_exit=None):
        code = None
        with mock.patch.object(HARNESS.cmux, "read_screen", return_value=""):
            try:
                HARNESS.cmd_validate(self._gate_args())
            except SystemExit as e:
                code = e.code
        if expect_exit is not None:
            self.assertEqual(code, expect_exit)
        return json.loads((self.root / "validation.json").read_text())

    def test_changed_executor_pane_ref_cannot_pass_channel_proof(self):
        """Changing only the recorded pane ref disagrees with its UUID proof."""
        self._run_gate(["surface:2", "surface:3", "surface:4"])
        p = self.root / "identity-gate.json"
        gate = json.loads(p.read_text())
        gate["executors"][2]["pane_ref"] = gate["executors"][1]["pane_ref"]
        p.write_text(json.dumps(gate))
        v = self._validate(expect_exit=1)
        self.assertFalse(v["checks"]["terminal_channels"]["pass"])
        self.assertFalse(v["checks"]["side_panel"]["pass"])
        self.assertFalse(v["checks"]["side_panel"]["all_panes_distinct"])
        self.assertEqual(v["checks"]["side_panel"]["executor_count"], 3)

    def test_changed_supervisor_pane_alias_cannot_pass_channel_proof(self):
        self._run_gate(["surface:2", "surface:3"])
        p = self.root / "identity-gate.json"
        gate = json.loads(p.read_text())
        gate["executors"][1]["pane_ref"] = gate["supervisor_pane_ref"]
        p.write_text(json.dumps(gate))
        v = self._validate(expect_exit=1)
        self.assertFalse(v["checks"]["terminal_channels"]["pass"])
        self.assertFalse(v["checks"]["side_panel"]["pass"])
        # Executor refs remain distinct, but this alias contradicts their pins.
        self.assertTrue(v["checks"]["side_panel"]["all_panes_distinct"])

    def test_side_panel_passes_for_three_distinct_panes(self):
        """Positive control: the all-distinct panel must still clear the check."""
        self._run_gate(["surface:2", "surface:3", "surface:4"])
        v = self._validate()
        self.assertTrue(v["checks"]["side_panel"]["pass"])
        self.assertTrue(v["checks"]["side_panel"]["all_panes_distinct"])
        self.assertEqual([e["ordinal"] for e in
                          v["checks"]["side_panel"]["executor_panes"]], [1, 2, 3])

    # -- task-pack addresses the whole panel --------------------------------

    def test_task_pack_names_every_executor(self):
        """A pack addressing one of three executors dispatches to one of three.

        The other two would never learn what they were asked to review, while
        the marker still claims a three-agent task.
        """
        self._handshake(["surface:2=codex", "surface:3=claude", "surface:4=grok"])
        with mock.patch.object(HARNESS.cmux, "rename_tab"):
            HARNESS.cmd_name_surfaces(self._gate_args())
        self.assertEqual(self._validate()["status"], "PASS")
        HARNESS.cmd_task_pack(self._gate_args(json=False))
        pack = json.loads((self.root / "task-pack.json").read_text())
        self.assertEqual([e["surface_ref"] for e in pack["executors"]],
                         ["surface:2", "surface:3", "surface:4"])
        self.assertEqual([e["provider"] for e in pack["executors"]],
                         ["codex", "claude", "grok"])
        self.assertEqual([e["display"] for e in pack["executors"]],
                         ["EXECUTOR_1", "EXECUTOR_2", "EXECUTOR_3"])
        # Singular field and the safety defaults are unchanged.
        self.assertEqual(pack["executor"], "surface:2")
        self.assertTrue(pack["draft"])
        self.assertEqual(pack["authorization_source"], "none")

    # -- record-round: one nonce and one verdict per reviewer ---------------

    def _round(self, refs, verdicts=None, silent=None, requested="PASS"):
        """cmd_record_round over an armed N-executor gate.

        capture_round_evidence is faked per surface so each executor answers
        with ITS OWN nonce, which is what proves the panel reviewed rather than
        one reviewer's echo covering the rest.

        `verdicts` maps surface_ref -> the verdict that executor selects.
        `silent` is a set of refs that never produce their nonce.
        """
        self._run_gate(refs)
        art = self.root / "plan.md"
        art.write_text("# plan\n")
        nonces = {}

        def submit(surface, _prompt, marker=None, **_kw):
            nonces[surface] = marker

        def capture(surface, nonce, provider=None, lines=None, expected_ack=None):
            mine = nonce == nonces.get(surface) and surface not in (silent or set())
            want = (verdicts or {}).get(surface, "PASS")
            hit = mine and expected_ack and expected_ack.endswith(
                f"|{want}|{nonce}")
            return {"executor_nonce_found": bool(hit),
                    "expected_ack_found": bool(hit),
                    "screen_hash": f"hash-{surface}"}

        # A fake clock that leaves room for a few polls before the deadline, so
        # a silent executor times out fast without skipping the loop entirely.
        clock = [1000.0]

        def tick():
            clock[0] += 60.0
            return clock[0]

        code = None
        with (
            mock.patch.object(HARNESS.cmux, "submit_text", side_effect=submit),
            mock.patch.object(HARNESS.cmux, "capture_round_evidence",
                              side_effect=capture),
            mock.patch.object(HARNESS.time, "time", side_effect=tick),
            mock.patch.object(HARNESS.time, "sleep"),
        ):
            try:
                HARNESS.cmd_record_round(self._gate_args(
                    round_id="R1", speaker="executor", verdict=requested,
                    artifact=str(art), blocks_consensus=False,
                    resolves_round=None, notes="", json=False))
            except SystemExit as e:
                code = e.code
        # An aborted round may leave no rounds.json at all, which is itself the
        # required outcome — so absence is a readable result, not a fixture error.
        p = self.root / "rounds.json"
        rounds = json.loads(p.read_text()) if p.exists() else {}
        return code, rounds, nonces

    def test_every_reviewer_is_asked_with_its_own_round_nonce(self):
        code, rounds, nonces = self._round(
            ["surface:2", "surface:3", "surface:4"])
        self.assertIsNone(code)
        issued = list(nonces.values())
        self.assertEqual(len(set(issued)), 3, f"one nonce per reviewer: {nonces}")
        ev = rounds["rounds"][0]["executor_evidence"]
        self.assertEqual(ev["executor_count"], 3)
        self.assertTrue(ev["all_executors_proven"])
        self.assertEqual([e["round_nonce"] for e in ev["executors"]], issued)
        self.assertEqual([e["ordinal"] for e in ev["executors"]], [1, 2, 3])
        self.assertTrue(all(e["nonce_proven"] for e in ev["executors"]))
        # Singular field keeps executor 1 for consensus-check's nonce_proven.
        self.assertEqual(ev["executor"], "surface:2")
        self.assertTrue(ev["nonce_proven"])

    def test_the_round_takes_the_least_favourable_verdict(self):
        """One FAIL among three reviewers must not be averaged away.

        Without aggregation the LAST executor polled became the round verdict,
        so list order decided a review outcome.
        """
        code, rounds, _ = self._round(
            ["surface:2", "surface:3", "surface:4"],
            verdicts={"surface:2": "PASS", "surface:3": "FAIL",
                      "surface:4": "PASS"},
            requested="PASS")
        self.assertIsNone(code)
        entry = rounds["rounds"][0]
        self.assertEqual(entry["executor_verdicts"], ["PASS", "FAIL", "PASS"])
        self.assertEqual(entry["verdict"], "FAIL")
        self.assertFalse(entry["executor_verdicts_unanimous"])
        self.assertEqual(entry["requested_verdict"], "PASS")

    def test_unanimous_reviewers_are_recorded_as_unanimous(self):
        """Positive control: agreement must not be reported as disagreement."""
        code, rounds, _ = self._round(["surface:2", "surface:3"],
                                      requested="PASS_WITH_CHANGES",
                                      verdicts={"surface:2": "PASS_WITH_CHANGES",
                                                "surface:3": "PASS_WITH_CHANGES"})
        self.assertIsNone(code)
        entry = rounds["rounds"][0]
        self.assertTrue(entry["executor_verdicts_unanimous"])
        self.assertEqual(entry["verdict"], "PASS_WITH_CHANGES")

    def test_one_silent_reviewer_records_no_round_at_all(self):
        """POISON: executor 2 never produces its nonce.

        rounds.json must stay empty. A round appended with two of three
        reviewers proven is the "declared three, proved one" state at the
        consensus layer, where consensus-check counts it as a full round.
        """
        code, rounds, nonces = self._round(
            ["surface:2", "surface:3", "surface:4"],
            silent={"surface:3"})
        self.assertEqual(code, 1)
        self.assertEqual(rounds.get("rounds", []), [],
                         "an unproven panel must not append a round")
        self.assertNotIn("surface:4", nonces)

    # -- old-field compatibility: gates written before executors[] -----------

    def test_a_gate_without_executors_still_reads_as_one_executor(self):
        """A gate file written by the previous harness has no `executors` key.

        Those files are on disk right now, so every reader has to resolve the
        singular fields into a one-element panel. `_gate_executors` is the only
        place allowed to make that decision; this pins its fallback against the
        exact five singular fields the old writer produced.
        """
        legacy = {
            "task_id": "multi-exec", "status": "PASS",
            "executor": "surface:9",
            "executor_provider": "claude",
            "executor_workspace_ref": "workspace:10",
            "executor_pane_ref": "pane:7",
            "executor_surface_uuid": "uuid-9",
        }
        resolved = HARNESS._gate_executors(legacy)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["surface_ref"], "surface:9")
        self.assertEqual(resolved[0]["provider"], "claude")
        self.assertEqual(resolved[0]["workspace_ref"], "workspace:10")
        self.assertEqual(resolved[0]["pane_ref"], "pane:7")
        self.assertEqual(resolved[0]["surface_uuid"], "uuid-9")
        self.assertEqual(resolved[0]["ordinal"], 1)

    def test_an_empty_executors_list_falls_back_and_never_yields_zero(self):
        """`executors: []` must not resolve to an empty panel.

        A zero-length panel would make every per-executor loop a no-op, and a
        loop that runs zero times reports success without proving anything —
        bridge, handshake, and round would all pass vacuously.
        """
        resolved = HARNESS._gate_executors(
            {"executors": [], "executor": "surface:9", "executor_provider": "codex"})
        self.assertEqual([e["surface_ref"] for e in resolved], ["surface:9"])

    def test_legacy_gate_file_drives_the_real_handshake_entry_point(self):
        """End to end: rewrite the gate into the old shape, then handshake.

        Checking `_gate_executors` alone would not catch a caller that reads
        `gate["executors"]` directly, so this drives cmd_handshake over a gate
        file with the key removed.
        """
        self._bridge(["surface:2"])
        p = self.root / "identity-gate.json"
        g = json.loads(p.read_text())
        del g["executors"]
        p.write_text(json.dumps(g))

        nonces = {}
        with (
            mock.patch.object(HARNESS.cmux, "submit_text",
                              side_effect=lambda s, _m, marker=None, **k:
                              nonces.setdefault(s, marker)),
            mock.patch.object(HARNESS.cmux, "read_screen",
                              side_effect=lambda s, **k:
                              "⏺ ok\nPREFLIGHT_ACK|multi-exec|codex:identity|"
                              f"READY|INLINE|{nonces.get(s)}"),
        ):
            HARNESS.cmd_handshake(self._gate_args())
        r = json.loads((self.root / "handshake-receipt.json").read_text())
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(r["executor"], "surface:2")
        self.assertEqual(r["executor_count"], 1)
        self.assertTrue(r["all_executors_acked"])

    def test_executor_two_bridge_evidence_cannot_vouch_for_executor_three(self):
        """POISON: bridge evidence exists, but not for this surface.

        Reading the file's top-level record for every executor would let
        executor 1's clean buffer authorise pasting into executor 3's.
        """
        self._bridge(["surface:2", "surface:3"])
        ev = json.loads((self.root / "bridge-test-evidence.json").read_text())
        # Drop executor 2's entry, keeping the top-level (executor 1) record.
        ev["executors"] = [e for e in ev["executors"] if e["executor"] != "surface:3"]
        (self.root / "bridge-test-evidence.json").write_text(json.dumps(ev))
        with (
            mock.patch.object(HARNESS.cmux, "submit_text", return_value={}) as submit,
            mock.patch.object(HARNESS.cmux, "wait_for_ack",
                              return_value="PREFLIGHT_ACK|multi-exec|codex:identity|READY|INLINE|n"),
            mock.patch.object(HARNESS.secrets, "token_hex", return_value="n"),
            self.assertRaises(SystemExit) as ctx,
        ):
            HARNESS.cmd_handshake(self._gate_args())
        self.assertEqual(ctx.exception.code, 1)
        # Executor 1 was legitimately handshaken; executor 2 never was.
        self.assertEqual([c.args[0] for c in submit.call_args_list], ["surface:2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
