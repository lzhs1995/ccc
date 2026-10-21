from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import availability_contract as a


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = a.initial("task", str(uuid.uuid4()), str(uuid.uuid4()), "surface:2",
                               authorization_source="user_message", authorization_record=self.ref("auth", {"user_message": "Automatically continue solo on executor failure."}),
                               fallback_policy="automatic_user_authorized", protected_paths=[str(self.root / "protected")])

    def ref(self, name, value):
        p = self.root / (name + ".json")
        p.write_text(json.dumps(value))
        return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}

    def outage(self):
        return a.transition(self.state, "UNAVAILABLE_AUTH", failure_receipt=self.ref("failure", {"http_status": 401}))

    def manifest(self, active_writers=None):
        return self.ref("takeover", {"task_id": "task", "executor_uuid": self.state["executor_uuid"],
            "checkpoint": self.ref("checkpoint", {"phase": "review"}),
            "freeze_evidence": self.ref("freeze", {"executor_frozen": True, "sentinel_stopped": True, "active_writers": active_writers or []}),
            "active_nonces": ["nonce"], "protected_paths": self.state["protected_paths"], "resume_phase": "review"})

    def test_authorized_dual_solo_dual(self):
        solo = a.transition(self.outage(), "SOLO_TAKEOVER", takeover_manifest=self.manifest())
        self.assertEqual(solo["review_provenance"], "solo_self_review")
        handoff = self.ref("handoff", {"task_id": "task", "phase_boundary": True, "active_writers": []})
        ready = a.transition(solo, "HANDOFF_READY", handoff=handoff)
        ack = self.ref("ack", {"task_id": "task", "status": "PASS", "executor_ack": True, "updated_at": datetime.now(timezone.utc).isoformat()})
        identity = self.ref("identity", {"status": "PASS", "workspace_uuid": ready["workspace_uuid"], "executor_surface_uuid": ready["executor_uuid"]})
        active = a.transition(ready, "ACTIVE", handshake=ack, identity=identity)
        self.assertTrue(active["executor_dispatch_allowed"])

    def test_free_text_and_unfrozen_writer_do_not_authorize_takeover(self):
        state = self.outage()
        state["authorization_source"] = "peer_agent"
        with self.assertRaisesRegex(a.AvailabilityError, "AUTHORIZATION"):
            a.transition(state, "SOLO_TAKEOVER", reason="the user said yes", takeover_manifest=self.manifest())
        with self.assertRaisesRegex(a.AvailabilityError, "NOT_DRAINED"):
            a.transition(self.outage(), "SOLO_TAKEOVER", takeover_manifest=self.manifest(["writer"]))

    def test_renumber_is_not_uuid_replacement(self):
        with self.assertRaisesRegex(a.AvailabilityError, "CONTINUITY"):
            a.transition(self.outage(), "SOLO_TAKEOVER", executor_surface="surface:99", takeover_manifest=self.manifest())

    def test_expired_state_and_late_callback_rejected(self):
        path = self.root / "state.json"
        state = self.outage()
        a.save(path, state)
        for action in ("dispatch", "callback", "round_record", "sentinel"):
            with self.subTest(action=action), self.assertRaises(a.AvailabilityError):
                a.require_action("task", action, {"availability_state": str(path)})
        self.state["expires_at"] = time.time() - 1
        a.save(path, self.state)
        with self.assertRaisesRegex(a.AvailabilityError, "EXPIRED"):
            a.require_action("task", "dispatch", {"availability_state": str(path)})

    def test_failure_classification_and_retry_budget(self):
        for code in (401, 403, 429):
            self.assertFalse(a.classify_failure({"http_status": code})["retry"])
        payload = {"http_status": 502, "retryable": True}
        self.assertTrue(a.classify_failure(payload, attempts=2, now=160, last_attempt_at=100)["retry"])
        self.assertFalse(a.classify_failure(payload, attempts=2, now=159, last_attempt_at=100)["retry"])
        self.assertTrue(a.classify_failure(payload, attempts=3)["takeover"])
        for payload in ({"submission_state": "DELIVERY_QUEUED_AT_RECEIVER", "http_status": 401}, {"budget_below_phase_minimum": True}):
            self.assertFalse(a.classify_failure(payload)["takeover"])

    def test_entry_points_block_before_delivery(self):
        import cmux_bridge
        state_file = self.root / "state.json"
        a.save(state_file, self.outage())
        pack = {"task_id": "task", "availability_state": str(state_file), "report": "unused"}
        with patch.object(cmux_bridge, "validate_task_pack_contract", return_value=pack), patch.object(cmux_bridge, "submit_text") as send:
            with self.assertRaises(a.AvailabilityError):
                cmux_bridge.submit_task_pack("surface:2", "unused", "pack")
            with self.assertRaises(a.AvailabilityError):
                cmux_bridge.submit_completion_callback("pack")
            send.assert_not_called()

    def test_insufficient_balance_is_billing_even_with_login_hint(self):
        for message in (
            "Please run /login · API Error: 403 Insufficient account balance",
            "API Error: 403 insufficient balance",
            "INSUFFICIENT ACCOUNT BALANCE",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    a.classify_failure({"http_status": 403, "message": message}),
                    {"category": "UNAVAILABLE_BILLING", "takeover": True, "retry": False},
                )

    def test_billing_message_does_not_override_transport_or_budget_evidence(self):
        payload = {"http_status": 403, "message": "Insufficient account balance"}
        for state in ("DELIVERY_QUEUED_AT_RECEIVER", "DELIVERY_UNVERIFIED_BY_DETECTOR"):
            with self.subTest(state=state):
                self.assertEqual(
                    a.classify_failure(dict(payload, submission_state=state)),
                    {"category": state, "takeover": False, "retry": False},
                )
        self.assertEqual(
            a.classify_failure(dict(payload, budget_below_phase_minimum=True)),
            {"category": "SUPERVISOR_BUDGET_TOO_SHORT", "takeover": False, "retry": False},
        )

    def test_generic_authentication_errors_stay_authentication(self):
        for code, message in ((401, "Invalid API key"), (403, "Forbidden")):
            with self.subTest(code=code):
                self.assertEqual(
                    a.classify_failure({"http_status": code, "message": message}),
                    {"category": "UNAVAILABLE_AUTH", "takeover": True, "retry": False},
                )

    def test_registry_and_direct_submit_cannot_bypass_artifact_state(self):
        import cmux_bridge
        state_file = self.root / "executor-availability.json"
        a.save(state_file, self.outage())
        pack = {"task_id": "task", "availability_state": str(state_file), "availability_required": True}
        (self.root / "task-pack.json").write_text(json.dumps(pack))
        registry = self.root / "registry"
        registry.mkdir()
        (registry / "task.json").write_text(json.dumps({"artifact_root": str(self.root)}))
        with patch.dict(os.environ, {"MULTI_AGENT_REGISTRY_ROOT": str(registry), "MULTI_AGENT_AVAILABILITY_ROOT": str(self.root / "empty-default")}):
            with self.assertRaises(a.AvailabilityError):
                a.require_action("task", "sentinel")
        with patch.object(cmux_bridge, "validate_task_pack_contract", return_value=pack), patch.object(cmux_bridge, "read_screen") as read:
            with self.assertRaises(a.AvailabilityError):
                cmux_bridge.submit_text("surface:2", "task", task_pack_path="pack")
            read.assert_not_called()

    def test_reobserve_keeps_solo_and_accepts_same_uuid_renumber(self):
        solo = a.transition(self.outage(), "SOLO_TAKEOVER", takeover_manifest=self.manifest())
        solo["expires_at"] = time.time() - 10
        identity = self.ref("fresh", {"task_id": "task", "status": "PASS", "workspace_uuid": solo["workspace_uuid"],
            "executor_surface_uuid": solo["executor_uuid"], "executor_surface": "surface:99", "observed_at": time.time()})
        refreshed = a.reobserve(solo, identity)
        self.assertEqual(refreshed["state"], "SOLO_TAKEOVER")
        self.assertEqual(refreshed["executor_surface"], "surface:99")
        self.assertFalse(refreshed["executor_dispatch_allowed"])


if __name__ == "__main__":
    unittest.main()
