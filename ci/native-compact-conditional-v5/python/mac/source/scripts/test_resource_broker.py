import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from resource_broker import Broker, ResourceBusy, os_lock


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = Broker(self.tmp.name)
        self.addCleanup(self.b.db.close)
        self.b.configure("shared")

    def request(self, task):
        return self.b.request("shared", task, str(uuid.uuid4()), str(uuid.uuid4()))

    def test_cross_workspace_queue_survives_coordinator_exit(self):
        a, b = self.request("a"), self.request("b")
        with self.assertRaisesRegex(ResourceBusy, "WAIT_YOUR_TURN"):
            self.b.grant(b["id"])
        lease = self.b.grant(a["id"])
        other = Broker(self.tmp.name)
        self.addCleanup(other.db.close)
        with self.assertRaisesRegex(ResourceBusy, "BUSY"):
            other.grant(b["id"])
        released = other.release(a["id"], lease["token"], {"pending": False, "own_processes_empty": True, "documents": "UNKNOWN"})
        self.assertFalse(released["release_proof"]["documents_verified"])
        self.assertEqual(other.grant(b["id"])["status"], "ACTIVE")

    def test_no_silent_takeover_on_expiry(self):
        a = self.request("a")
        lease = self.b.grant(a["id"])
        self.b.db.execute("UPDATE tickets SET expires=? WHERE id=?", (time.time() - 1, a["id"]))
        b = self.request("b")
        with self.assertRaisesRegex(ResourceBusy, "RECONCILIATION"):
            self.b.grant(b["id"])
        with self.assertRaisesRegex(ResourceBusy, "RECONCILIATION"):
            self.b.release(a["id"], lease["token"], {"pending": False, "own_processes_empty": True})
        self.b.release(a["id"], lease["token"], {"pending": False, "own_processes_empty": True}, reconcile=True)
        self.assertEqual(self.b.grant(b["id"])["status"], "ACTIVE")

    def test_actual_os_lock_and_pending_block_release(self):
        a = self.request("a")
        lease = self.b.grant(a["id"])
        with os_lock(self.b.lock_path("shared")):
            with self.assertRaisesRegex(ResourceBusy, "OS_LOCK_BUSY"):
                self.b.release(a["id"], lease["token"], {"pending": False, "own_processes_empty": True})
        with self.assertRaisesRegex(ResourceBusy, "DRAIN_REQUIRED"):
            self.b.release(a["id"], lease["token"], {"pending": True, "own_processes_empty": True})

    def test_run_records_real_child_group_and_release(self):
        a = self.request("a")
        lease = self.b.grant(a["id"])
        result = self.b.run(a["id"], lease["token"], [sys.executable, "-c", "print('fixture complete')"], text=True, stdout=subprocess.PIPE)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["process_group_empty"])
        self.assertIn("fixture complete", result["stdout"])
        self.b.release(a["id"], lease["token"], {"pending": False, "own_processes_empty": True})

    def test_external_lock_busy_and_mapping_pinned(self):
        p = Path(self.tmp.name) / "external.lock"
        self.b.configure("word", [p])
        with self.assertRaisesRegex(ResourceBusy, "PINNED"):
            self.b.configure("word", [])
        ticket = self.b.request("word", "a", str(uuid.uuid4()), str(uuid.uuid4()))
        with os_lock(p):
            with self.assertRaisesRegex(ResourceBusy, "OS_LOCK_BUSY"):
                self.b.grant(ticket["id"])

    def test_word_unknown_nonzero_and_missing_observation_fail_closed(self):
        self.b.configure("word-zotero")
        ticket = self.b.request("word-zotero", "word-task", str(uuid.uuid4()), str(uuid.uuid4()))
        lease = self.b.grant(ticket["id"])
        proof = {"pending": False, "own_processes_empty": True}
        with self.assertRaisesRegex(ResourceBusy, "RECEIPT_REQUIRED"):
            self.b.release(ticket["id"], lease["token"], proof)
        p = Path(self.tmp.name) / "observed.json"
        raw = {"task_id": "word-task", "documents": 0, "windows": 0, "modal": False,
               "zotero_current_doc": False, "zotero_current_window": False, "pending": False}
        for count in (None, 1, 0):
            p.write_text(json.dumps({**raw, "documents": count}))
            proof["observed_receipt"] = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            if count != 0:
                with self.assertRaisesRegex(ResourceBusy, "CONDITION_FAILED"):
                    self.b.release(ticket["id"], lease["token"], proof)
            else:
                self.assertEqual(self.b.release(ticket["id"], lease["token"], proof)["status"], "RELEASED")

    def test_nlm_empty_list_is_not_remote_completion(self):
        self.b.configure("nlm-account:test")
        ticket = self.b.request("nlm-account:test", "n", str(uuid.uuid4()), str(uuid.uuid4()))
        lease = self.b.grant(ticket["id"])
        proof = {"pending": False, "own_processes_empty": True}
        for requests in (None, ["still-running"], []):
            with self.assertRaises(ResourceBusy):
                self.b.release(ticket["id"], lease["token"], {**proof, "in_flight_requests": requests})
        self.assertEqual(self.b.release(ticket["id"], lease["token"], {**proof, "in_flight_requests": [], "no_request_submitted": True})["status"], "RELEASED")

    def test_abandoned_waiting_owner_can_withdraw(self):
        first, second = self.request("a"), self.request("b")
        with self.assertRaisesRegex(ResourceBusy, "OWNER"):
            self.b.withdraw(first["id"], "intruder", first["workspace_uuid"], first["surface_uuid"])
        self.b.withdraw(first["id"], "a", first["workspace_uuid"], first["surface_uuid"])
        self.assertEqual(self.b.grant(second["id"])["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
