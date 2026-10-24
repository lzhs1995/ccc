"""Compatibility facade and explicit v1 migration regression tests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import executor_availability as a

class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_outage_is_never_silently_reactivated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.json"
            path.write_text(json.dumps({"task_id": "legacy", "state": "SOLO_TAKEOVER", "executor_surface": "surface:9"}))
            reference = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            result = a.migrate(reference, "legacy", workspace_uuid=str(uuid.uuid4()), executor_uuid=str(uuid.uuid4()), executor_surface="surface:9")
            self.assertEqual(result["state"], "EXECUTOR_DEGRADED")
            self.assertFalse(result["executor_dispatch_allowed"])
            self.assertEqual(result["migration"]["previous_state"], "SOLO_TAKEOVER")
            self.assertTrue(Path(result["failure_receipt"]["path"]).is_file())

    def test_legacy_transition_requires_explicit_migration(self):
        with self.assertRaisesRegex(a.AvailabilityError, "INITIALIZE_V2_STATE_REQUIRED"):
            a.transition({"state": "UNAVAILABLE_BILLING"}, "SOLO_TAKEOVER", reason="user asked")

if __name__ == "__main__":
    unittest.main()
