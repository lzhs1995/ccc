"""Real-file pre-send and backup failure regressions for the next CCC runtime."""
import contextlib
import copy
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

RUNTIME = Path(os.environ["CCC_COMPACT_TEST_RUNTIME"])
sys.path.insert(0, str(RUNTIME))
from ccc_compact_backup import publish_backup
from ccc_compact_control_health import control_health
from ccc_compact_inputs import PendingInputs
from ccc_compact_intents import IntentIndex
from ccc_compact_runtime import conditional_submit
from ccc_compact_support import Journal, OwnedDirectory
from ccc_compact_watchdog import Worker


class ControlDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.sid = str(uuid.uuid4())
        self.key = "e" * 24
        self.journal = self.base / self.sid
        self.journal.mkdir(mode=0o700)
        self.index = IntentIndex(self.base / "native-input-intents.json",
                                state_root=self.base / "mac", active_root=self.base / "active")
        self.index.save()
        self.request = dict(threadId=self.sid, expectedTurnId="turn-1", attemptId=self.key,
                            intentPath=str(self.journal / (self.key + "-interrupt.json")),
                            clientPid=os.getpid(), compactionOccurrence=str(uuid.uuid4()))
        self.owned = OwnedDirectory(self.journal)
        self.addCleanup(self.owned.close)

    def worker(self, rpc_type):
        bridge = SimpleNamespace(require_prompt_free=lambda *a, **kw: None,
                                 submit_text_protected=lambda *a: self.fail("unexpected TUI input"))
        return SimpleNamespace(target=dict(identity={}, process={}, session_id=self.sid,
                                           surface_id=str(uuid.uuid4())),
                               lib=(bridge, None, None, None, rpc_type),
                               journal_directory=self.owned,
                               state=dict(episode=dict(key=self.key, phase="compacting",
                                                       turn_id="turn-1")),
                               journal=Journal(self.journal / "events.jsonl", directory=self.owned))

    def snapshot(self):
        return dict(client_epoch=str(uuid.uuid4()), foreground=dict(path="same", sha256="same"))

    def prepare(self, worker):
        worker.state["episode"]["phase"] = "control_pending"
        self.owned.write_once(self.key + "-interrupt.json", dict(client_request=self.request))
        self.index.freeze_for_control(self.request)

    def test_failed_post_freeze_check_records_health_without_releasing_or_transmitting(self):
        calls = []
        class RPC:
            def __init__(self, **kw): pass
            def close(self): calls.append("closed")
            def request(self, *a): calls.append("transport"); raise AssertionError("must not transmit")
        worker = self.worker(RPC)
        snap = self.snapshot()
        def prepare(_):
            self.prepare(worker)
            raise RuntimeError("binding changed after freeze")
        with self.assertRaisesRegex(RuntimeError, "binding changed"):
            conditional_submit(worker, self.request, snap, lambda: None, lambda: snap, prepare)
        self.assertEqual(calls, ["closed"])
        self.assertFalse((self.journal / (self.key + "-client-submit.json")).exists())
        self.assertTrue(self.index.control_is_pending())
        expected = dict(state="pinned_not_transmitted", key=self.key, session_id=self.sid,
                        client_submit_present=False, input_evidence_frozen=True,
                        replay_authorized=False, evidence_released=False)
        self.assertEqual(control_health(self.index), expected)
        recreated = IntentIndex(self.index.path, state_root=self.base / "mac",
                                active_root=self.base / "active")
        self.assertTrue(recreated.control_is_pending())
        self.assertEqual(control_health(recreated), expected)
        marker = json.loads((self.journal / (self.key + "-client-not-sent.json")).read_bytes())
        self.assertEqual(marker["status"], "NOT_TRANSMITTED")
        self.assertEqual(marker["client_request"], self.request)
        self.assertFalse(marker["replay_authorized"])
        # Operator health is not a terminal receipt, even after a daemon restart.
        before = self.index.path.read_bytes()
        recreated.state["entries"]["new"] = {}
        with self.assertRaisesRegex(RuntimeError, "receipt pending"):
            recreated.save()
        self.assertEqual(self.index.path.read_bytes(), before)

    def test_failure_before_control_intent_has_no_not_sent_marker_or_active_pin(self):
        class RPC:
            def __init__(self, **kw): pass
            def close(self): pass
        worker = self.worker(RPC)
        snap = self.snapshot()
        def fail(_): raise RuntimeError("selection changed before intent")
        with self.assertRaisesRegex(RuntimeError, "selection changed"):
            conditional_submit(worker, self.request, snap, lambda: None, lambda: snap, fail)
        self.assertFalse((self.journal / (self.key + "-client-not-sent.json")).exists())
        self.assertFalse((self.base / "native-control-pins-v4").exists())

    def test_pre_submit_lock_failure_keeps_original_pin_and_zero_transport(self):
        calls = []
        class RPC:
            def __init__(self, **kw): pass
            def close(self): pass
            def request(self, *a): self.fail("must not transmit")
        worker = self.worker(RPC)
        snap = self.snapshot()
        def check():
            calls.append("check")
            if worker.state["episode"]["phase"] == "control_pending":
                raise RuntimeError("original lock replaced")
        with self.assertRaisesRegex(RuntimeError, "lock replaced"):
            conditional_submit(worker, self.request, snap, check, lambda: snap,
                               lambda _: self.prepare(worker))
        self.assertEqual(len(calls), 2)
        self.assertEqual(control_health(self.index)["state"], "pinned_not_transmitted")
        self.assertFalse((self.journal / (self.key + "-client-submit.json")).exists())

    def test_transport_timeout_and_duplicate_are_never_relabeled_not_sent(self):
        operations = []
        class RPC:
            def __init__(self, **kw): pass
            def close(self): pass
            def request(self, *a): operations.append(a); raise TimeoutError("unknown request")
        worker = self.worker(RPC)
        snap = self.snapshot()
        with self.assertRaises(TimeoutError):
            conditional_submit(worker, self.request, snap, lambda: None, lambda: snap,
                               lambda _: self.prepare(worker))
        original = (self.journal / (self.key + "-client-submit.json")).read_bytes()
        with self.assertRaises(FileExistsError):
            conditional_submit(worker, self.request, snap, lambda: None, lambda: snap, lambda _: None)
        self.assertEqual(len(operations), 1)
        self.assertEqual((self.journal / (self.key + "-client-submit.json")).read_bytes(), original)
        self.assertFalse((self.journal / (self.key + "-client-not-sent.json")).exists())
        self.assertEqual(control_health(self.index)["state"], "native_receipt_pending")

    def test_after_intent_crash_without_submit_reports_evidence_only(self):
        self.index.freeze_for_control(self.request)
        self.assertEqual(control_health(self.index), dict(
            state="pinned_before_submission", key=self.key, session_id=self.sid,
            client_submit_present=False, input_evidence_frozen=True,
            replay_authorized=False, evidence_released=False))

    def test_backup_crash_before_publication_does_not_poison_final_name(self):
        name = "legacy-backup.json"
        raw = b'{ "version": 2, "pending": ["original"] }\n'
        self.owned.write_once_bytes("original.json", raw)
        code = ("import sys,os;sys.path.insert(0," + repr(str(RUNTIME)) + ");"
                "import ccc_compact_backup as m;"
                "from ccc_compact_support import OwnedDirectory;"
                "m._rename_exclusive=lambda *a:os._exit(93);"
                "d=OwnedDirectory(" + repr(str(self.journal)) + ");"
                "m.publish_backup(d," + repr(name) + "," + repr(raw) + ")")
        child = subprocess.run([sys.executable, "-I", "-B", "-c", code])
        self.assertEqual(child.returncode, 93)
        self.assertFalse((self.journal / name).exists())
        temporary = list(self.journal.glob(".backup-*.tmp"))
        self.assertEqual(len(temporary), 1)
        self.assertEqual(temporary[0].read_bytes(), raw)
        self.assertEqual(temporary[0].stat().st_nlink, 1)
        publish_backup(self.owned, name, raw)
        self.assertEqual(self.owned.read_bytes(name, limit=len(raw)), raw)
        self.assertEqual((self.journal / name).stat().st_nlink, 1)
        self.assertEqual(self.owned.read_bytes("original.json", limit=len(raw)), raw)
        self.assertTrue(temporary[0].exists(), "crash evidence was removed")

    def test_backup_crash_after_atomic_publication_keeps_complete_single_link(self):
        name, raw = "published.json", b'{"original":true}\n'
        code = ("import sys,os;sys.path.insert(0," + repr(str(RUNTIME)) + ");"
                "import ccc_compact_backup as m;"
                "from ccc_compact_support import OwnedDirectory;"
                "real=m._rename_exclusive;"
                "exec('def stop(*a):\\n real(*a)\\n os._exit(94)');"
                "m._rename_exclusive=stop;"
                "d=OwnedDirectory(" + repr(str(self.journal)) + ");"
                "m.publish_backup(d," + repr(name) + "," + repr(raw) + ")")
        child = subprocess.run([sys.executable, "-I", "-B", "-c", code])
        self.assertEqual(child.returncode, 94)
        self.assertEqual(self.owned.read_bytes(name, limit=len(raw)), raw)
        self.assertEqual((self.journal / name).stat().st_nlink, 1)
        with self.assertRaises(FileExistsError):
            publish_backup(self.owned, name, b"changed")
        self.assertEqual(self.owned.read_bytes(name, limit=len(raw)), raw)

    def test_backup_disk_failure_leaves_original_and_final_name_unmodified(self):
        name, raw = "disk-failed.json", b"original"
        original = self.journal / "ledger.json"
        self.owned.write_once_bytes(original.name, raw)
        with patch("ccc_compact_backup._rename_exclusive",
                   side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                publish_backup(self.owned, name, raw)
        self.assertFalse((self.journal / name).exists())
        self.assertEqual(original.read_bytes(), raw)
        publish_backup(self.owned, name, raw)
        self.assertEqual(self.owned.read_bytes(name, limit=len(raw)), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
