"""Claude-A V13 gates: real Worker.recover/run, control_health, guard._observer and
conditional_submit on real files. Only the cmux host binding and the native RPC are
stubbed (no UI, no cmux). Every wait and join is bounded."""
import contextlib
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

RUNTIME = Path(os.environ["CCC_COMPACT_TEST_RUNTIME"])
sys.path.insert(0, str(RUNTIME))
if os.environ.get("CCC_COMPACT_TEST_OVERLAY"):
    sys.path.insert(0, os.environ["CCC_COMPACT_TEST_OVERLAY"])
import ccc_compact_watchdog as wd
import ccc_compact_guard as guard
from ccc_compact_control_health import ControlEvidenceBusy, control_health
from ccc_compact_intents import IntentIndex
from ccc_compact_runtime import conditional_submit
from ccc_compact_support import Journal, OwnedDirectory

BOUND = 10


class Stop:
    """One Worker.run loop iteration: stop.wait() ends the loop."""
    def __init__(self):
        self.flag = False

    def is_set(self):
        return self.flag

    def wait(self, _timeout=None):
        self.flag = True


class Gates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("CCC_V13_TMP", str(Path(__file__).resolve().parent)))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.sid = str(uuid.uuid4())
        self.key = "a" * 24
        self.journal = self.base / self.sid
        self.journal.mkdir(mode=0o700)
        self.index = IntentIndex(self.base / "native-input-intents.json",
                                 state_root=self.base / "mac", active_root=self.base / "active")
        self.index.save()
        self.index_bytes = self.index.path.read_bytes()
        self.owned = OwnedDirectory(self.journal)
        self.addCleanup(self.owned.close)
        for name in ("fg.json", "en.json", "cf.json"):
            self.owned.write_once(name, dict(name=name))
        self.transports = []
        self.intents = []
        self.epoch = str(uuid.uuid4())
        self.occurrence = str(uuid.uuid4())

    # --- real Worker object with only host/native edges stubbed -----------------
    def worker(self, *, binding_gate=None, intent_mode="ok", rpc_mode="none"):
        test = self
        w = wd.Worker.__new__(wd.Worker)
        w.lock = __import__("threading").RLock()
        w.apply = True
        w.target = dict(session_id=self.sid, surface_id=str(uuid.uuid4()),
                        workspace_id=str(uuid.uuid4()), pane_id=str(uuid.uuid4()),
                        identity=dict(id="row"), process=dict(pid=1), path="rollout")
        w.directory = self.journal
        w.journal_directory = self.owned
        w.journal = Journal(self.journal / "events.jsonl", directory=self.owned)
        w.state_path = self.journal / "state.json"
        w.inputs = SimpleNamespace(intent_index=self.index)
        w.notify = lambda *a, **k: None
        w.timer = None
        w.state = dict(episode=dict(key=self.key, phase="compacting", turn_id="turn-1",
                                    attempt=0, goal=None, started_at=time.time() - 30,
                                    compaction_occurrence=self.occurrence,
                                    client_pid=os.getpid(), client_epoch=self.epoch),
                       closed=[], progress_after=None)
        snap = dict(goal=None, client_pid=os.getpid(), client_epoch=self.epoch,
                    foreground=dict(path="same", sha256="same"))
        w.snapshot = lambda **kw: dict(snap)
        w.unchanged = lambda *a, **kw: True
        w.recovery_locks = lambda: contextlib.nullcontext(lambda: None)
        w.native_receipt_pending = lambda ep: True

        def pin(name):
            return wd.pinned_file(self.journal / name)[0]

        def intent(episode, kind, params, snapshot, *, request=None):
            test.intents.append(kind)
            authority = dict(foreground=pin("fg.json"), enrollment=pin("en.json"),
                             config=pin("cf.json"), pinned_inputs=[])
            test.owned.write_once(episode["key"] + "-authority.json", authority)
            if intent_mode == "enospc_before_interrupt":
                raise OSError(errno.ENOSPC, "No space left on device (simulated)")
            if intent_mode == "change_pin_before_freeze":
                test.owned.atomic_bytes("fg.json", b'{"changed": true}\n')
            test.owned.write_once(episode["key"] + "-interrupt.json",
                                  dict(kind=kind, client_request=request))
        w.intent = intent

        class RPC:
            def __init__(self, **kw): pass
            def close(self): pass
            def request(self, method, params):
                test.transports.append(method)
                if rpc_mode == "ceb":
                    raise ControlEvidenceBusy("shared control evidence busy after intent (patched)")
                raise AssertionError("unexpected transport")
        bridge = SimpleNamespace(require_prompt_free=lambda *a, **k: None,
                                 submit_text_protected=lambda *a: test.fail("unexpected TUI input"))
        w.lib = (bridge, None, None, None, RPC)

        def host_binding(target, _bridge, _native, **kw):
            if binding_gate is not None:
                binding_gate["entered"].set()
                if not binding_gate["release"].wait(BOUND):
                    raise TimeoutError("test host binding never released")
            return dict(identity=target["identity"], process=target["process"])
        return w, host_binding

    def recover(self, w, host_binding):
        with patch.object(wd, "binding", host_binding), \
                patch.object(wd, "may_interrupt", lambda *a, **k: True):
            w.recover(self.key)

    def events(self):
        path = self.journal / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text().splitlines() if line.strip()]

    def peer_request(self):
        peer = "b" * 24
        # A real peer owns its journal directory before it can pin an intent there.
        (self.base / "peer-sid").mkdir(mode=0o700, exist_ok=True)
        return dict(threadId=str(uuid.uuid4()), expectedTurnId="peer-turn", attemptId=peer,
                    intentPath=str(self.base / "peer-sid" / (peer + "-interrupt.json")),
                    clientPid=1, compactionOccurrence=str(uuid.uuid4()))
    # G1: host binding blocked, RLock free; peer pins meanwhile -> zero intent/transport.
    def test_g1_blocked_host_binding_releases_rlock_and_peer_pin_defers_zero_intent(self):
        gate = dict(entered=threading.Event(), release=threading.Event())
        w, host_binding = self.worker(binding_gate=gate)
        before_ep = dict(w.state["episode"])
        errors = []

        def run():
            try:
                self.recover(w, host_binding)
            except BaseException as exc:  # recorded and asserted below
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(gate["entered"].wait(BOUND), "host binding call not reached")
            acquired = self.index.lock.acquire(timeout=2)
            self.assertTrue(acquired, "fleet IntentIndex RLock held during host binding")
            try:
                self.index.freeze_for_control(self.peer_request())
            finally:
                self.index.lock.release()
        finally:
            gate["release"].set()
            thread.join(BOUND)
        self.assertFalse(thread.is_alive(), "worker did not finish")
        self.assertEqual(errors, [])
        ep = w.state["episode"]
        self.assertEqual((ep["phase"], ep["attempt"], ep["started_at"], ep["key"]),
                         (before_ep["phase"], before_ep["attempt"], before_ep["started_at"],
                          before_ep["key"]))
        self.assertEqual(ep["recovery_deferred"]["reason"], "shared_control_evidence_pinned")
        self.assertEqual((self.intents, self.transports), ([], []))
        for suffix in ("-interrupt.json", "-client-submit.json", "-client-not-sent.json",
                       "-authority.json"):
            self.assertFalse((self.journal / (self.key + suffix)).exists(), suffix)
        self.assertFalse((self.base / "native-control-pins-v4" / (self.key + ".json")).exists())
        self.assertEqual(self.index.path.read_bytes(), self.index_bytes)
        self.assertIn("RECOVERY_DEFERRED_SHARED_CONTROL", self.events())
        self.assertNotIn("control_outcome", w.state)

    # G2: consumed local intent without a shared pin is visible to real health and guard.
    def test_g2_consumed_local_intent_without_pin_is_retained_health(self):
        w, host_binding = self.worker(intent_mode="change_pin_before_freeze")
        self.recover(w, host_binding)
        self.assertEqual(w.state["episode"]["phase"], "control_pending")
        self.assertEqual(w.state["control_outcome"]["status"], "UNKNOWN")
        self.assertEqual((self.intents, self.transports), (["interrupt"], []))
        self.assertFalse((self.base / "native-control-pins-v4" / (self.key + ".json")).exists())
        self.assertFalse(self.index.control_is_pending())
        self.assertTrue((self.journal / (self.key + "-client-not-sent.json")).exists())
        health = control_health(self.index, worker=w)
        self.assertEqual(health, dict(state="pinned_not_transmitted", key=self.key,
                                      session_id=self.sid, client_submit_present=False,
                                      input_evidence_frozen=False, original_intent_retained=True,
                                      replay_authorized=False, evidence_released=False))
        record = self.run_once(w)
        self.assertEqual(record["state"], "pinned_not_transmitted")
        result = self.observe(w, record)
        self.assertNotEqual(result.get("state"), "healthy")
        self.assertEqual(result.get("state"), "pinned_not_transmitted")

    # G2b: missing original interrupt file must never present as observing/healthy.
    def test_g2b_missing_interrupt_file_never_observing(self):
        w, host_binding = self.worker(intent_mode="enospc_before_interrupt")
        self.recover(w, host_binding)
        self.assertEqual(w.state["episode"]["phase"], "control_pending")
        self.assertFalse((self.journal / (self.key + "-interrupt.json")).exists())
        self.assertEqual(self.transports, [])
        # The previous successful tick (before recovery) wrote a current observing heartbeat.
        self.owned.atomic_json("worker-health.json", dict(
            at=time.time(), state="observing", control_evidence=None,
            surface_id=w.target["surface_id"], session_id=self.sid, binding_verified=True))
        record = self.run_once(w)
        self.assertNotEqual(record.get("state"), "observing",
                            "missing original interrupt file presented as observing")
        self.assertNotEqual(self.observe(w, record).get("state"), "healthy")
    # G3: marker ENOSPC keeps the original failure and adds no transport.
    def marker_failure(self, journal_fails):
        class RPC:
            def __init__(s, **kw): pass
            def close(s): pass
            def request(s, *a):
                self.transports.append(a)
                raise AssertionError("must not transmit")
        bridge = SimpleNamespace(require_prompt_free=lambda *a, **k: None,
                                 submit_text_protected=lambda *a: self.fail("unexpected TUI input"))
        worker = SimpleNamespace(
            target=dict(identity={}, process={}, session_id=self.sid, surface_id=str(uuid.uuid4())),
            lib=(bridge, None, None, None, RPC), journal_directory=self.owned,
            state=dict(episode=dict(key=self.key, phase="compacting", turn_id="turn-1")),
            journal=Journal(self.journal / "events.jsonl", directory=self.owned))
        request = dict(threadId=self.sid, expectedTurnId="turn-1", attemptId=self.key,
                       intentPath=str(self.journal / (self.key + "-interrupt.json")),
                       clientPid=os.getpid(), compactionOccurrence=self.occurrence)
        snap = dict(client_epoch=self.epoch, foreground=dict(path="same", sha256="same"))

        def prepare(_):
            worker.state["episode"]["phase"] = "control_pending"
            self.owned.write_once(self.key + "-interrupt.json", dict(client_request=request))
            raise RuntimeError("original pre-submit failure")
        real = OwnedDirectory.write_once

        def write_once(directory, name, *a, **k):
            if str(name).endswith("-client-not-sent.json"):
                raise OSError(errno.ENOSPC, "No space left on device (simulated)")
            return real(directory, name, *a, **k)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(OwnedDirectory, "write_once", write_once))
            if journal_fails:
                stack.enter_context(patch.object(Journal, "record", side_effect=OSError(
                    errno.ENOSPC, "journal full (simulated)")))
            with self.assertRaises(RuntimeError) as caught:
                conditional_submit(worker, request, snap, lambda: None, lambda: snap, prepare)
        self.assertIn("original pre-submit failure", str(caught.exception))
        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(any("marker failed" in n for n in notes), notes)
        self.assertEqual(self.transports, [])
        for suffix in ("-client-not-sent.json", "-client-submit.json"):
            self.assertFalse((self.journal / (self.key + suffix)).exists(), suffix)
        return notes

    def test_g3_marker_enospc_preserves_original_failure_and_zero_transport(self):
        self.marker_failure(journal_fails=False)
        self.assertIn("CONDITIONAL_NOT_TRANSMITTED_MARKER_FAILED", self.events())

    def test_g3b_marker_and_journal_failure_keep_original_with_both_notes(self):
        notes = self.marker_failure(journal_fails=True)
        self.assertTrue(any("journal failed" in n for n in notes), notes)

    # G4: guard reports the exact retained state, never healthy.
    def test_g4_guard_reports_exact_retained_state_never_healthy(self):
        surface = str(uuid.uuid4())
        value = dict(native=dict(binding_ok=True, facts=dict(caught_up=True), compact=None),
                     observed_at=time.time())
        for state in ("pinned_before_submission", "pinned_not_transmitted", "native_receipt_pending"):
            health = dict(at=time.time(), state=state, control_evidence=dict(state=state, key=self.key),
                          surface_id=surface, session_id=self.sid, binding_verified=True)
            result = guard._observer(value, health, self.sid, guard._uuid(surface), time.time())
            self.assertEqual(result["state"], state)
            self.assertNotEqual(result["state"], "healthy")
            self.assertIn(state, result["reason"])
            self.assertEqual(result["control_evidence"], health["control_evidence"])
        ok = dict(at=time.time(), state="observing", surface_id=surface, session_id=self.sid,
                  binding_verified=True)
        self.assertEqual(guard._observer(value, ok, self.sid, guard._uuid(surface), time.time())["state"],
                         "healthy")
        with self.assertRaises(ValueError):
            guard._observer(value, dict(ok, state="bogus"), self.sid, guard._uuid(surface), time.time())
    # G5: terminal receipt landing between the pending scan and the health loop.
    def test_g5_terminal_between_scans_is_not_a_false_error(self):
        request = dict(threadId=self.sid, expectedTurnId="turn-1", attemptId=self.key,
                       intentPath=str(self.journal / (self.key + "-interrupt.json")),
                       clientPid=os.getpid(), compactionOccurrence=self.occurrence)
        self.index.freeze_for_control(request)
        self.assertTrue(self.index.control_is_pending())
        real = IntentIndex.control_is_pending
        landed = []

        def racing(index):
            pending = real(index)
            if pending and not landed:
                landed.append(True)
                self.owned.write_once(self.key + "-compact-started.json", dict(
                    episode_key=self.key, original_intent=request["intentPath"],
                    fields=dict(turnId="turn-2", joinAccounting=dict(status="not_accounted"))))
            return pending
        with patch.object(IntentIndex, "control_is_pending", racing):
            self.assertIsNone(control_health(self.index))
        self.assertEqual(landed, [True])
        self.assertFalse(self.index.control_is_pending())

    # G6: ControlEvidenceBusy after the intent (patched transport) is recorded UNKNOWN.
    def test_g6_post_intent_control_evidence_busy_is_unknown_once(self):
        w, host_binding = self.worker(rpc_mode="ceb")
        before = dict(w.state["episode"])
        self.recover(w, host_binding)
        ep = w.state["episode"]
        self.assertEqual((ep["phase"], ep["attempt"], ep["started_at"]),
                         ("control_pending", before["attempt"], before["started_at"]))
        self.assertEqual(w.state["control_outcome"]["status"], "UNKNOWN")
        self.assertFalse(w.state["control_outcome"]["replay_authorized"])
        self.assertEqual(self.transports, ["thread/compact/interrupt"])
        self.assertTrue((self.journal / (self.key + "-client-submit.json")).exists())
        self.assertFalse((self.journal / (self.key + "-client-not-sent.json")).exists())
        self.assertTrue(self.index.control_is_pending())
        self.assertIn("CONDITIONAL_OUTCOME_UNKNOWN", self.events())
        self.assertEqual(control_health(self.index)["state"], "native_receipt_pending")

    # --- helpers -----------------------------------------------------------------
    def run_once(self, w):
        w.stop = Stop()
        w.facts = SimpleNamespace(refresh=lambda: dict(caught_up=True))
        w.tick = lambda: None
        w.observe_without_control = lambda error: dict(at=time.time())
        w.arm = lambda ep: None
        w.close = lambda: None
        w.last_error = None
        wd.Worker.run(w)
        return json.loads((self.journal / "worker-health.json").read_bytes())

    def observe(self, w, record):
        value = dict(native=dict(binding_ok=True, facts=dict(caught_up=True), compact=None),
                     observed_at=time.time())
        try:
            return guard._observer(value, record, self.sid, guard._uuid(w.target["surface_id"]),
                                   time.time())
        except ValueError as exc:
            return dict(state="unavailable", reason=str(exc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
