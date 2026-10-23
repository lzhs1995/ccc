#!/usr/bin/env python3
"""Test _touch_active_marker concurrency (task ccc-collab-fable-gap-20260903)."""
import json
from contextlib import ExitStack
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import mac_harness as HARNESS

class HeartbeatConcurrencyTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise standalone marker storage, not the live daemon.
        # Restore both globals even if an assertion fails mid-test.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        stack.enter_context(mock.patch.object(HARNESS, "_ACTIVE_DIR", HARNESS._ACTIVE_DIR))
        stack.enter_context(mock.patch("cmux_daemon_identity.collect", return_value=None))

    """_touch_active_marker must handle concurrent beats without data loss."""
    
    def test_interleaved_beats_from_different_pids_preserve_marker_integrity(self):
        """Exercise the real heartbeat path with deterministic cross-PID beats.

        The harness uses a per-PID temporary name before os.replace(). Calling
        the helper directly keeps this test deterministic while still covering
        the operation that the old probe accidentally skipped.
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CMUX_WORKSPACE_ID"] = "ws-concurrent"
            HARNESS._ACTIVE_DIR = Path(tmp) / "_active"
            
            root = Path(tmp) / "artifacts"
            root.mkdir(parents=True)
            
            # Arm a task
            marker = HARNESS.arm_task(
                task_id="heartbeat-concurrent",
                root=root,
                supervisor="surface:1",
                executor="surface:2",
                supervisor_provider="codex",
                executor_provider="claude",
            )
            
            collaboration_id = marker["collaboration_id"]
            marker_path = HARNESS._ACTIVE_DIR / "ws-concurrent" / f"{collaboration_id}.json"
            
            # Interleave beats from two different process identities.
            with mock.patch('os.getpid', return_value=1001):
                HARNESS._touch_active_marker(root / "receipt1.json")
            with mock.patch('os.getpid', return_value=1002):
                HARNESS._touch_active_marker(root / "receipt2.json")
            
            # Verify marker is still valid JSON
            self.assertTrue(marker_path.exists())
            final_marker = json.loads(marker_path.read_text())
            
            # Must have all required fields
            self.assertIn("collaboration_id", final_marker)
            self.assertIn("task_id", final_marker)
            self.assertIn("last_activity_at", final_marker)
            self.assertEqual(final_marker["collaboration_id"], collaboration_id)
            
            # No temp files left behind
            temp_files = list(marker_path.parent.glob(".*.hb.*"))
            self.assertEqual(len(temp_files), 0,
                           f"heartbeat must not leave temp files: {temp_files}")
    
    def test_heartbeat_updates_last_activity_timestamp(self):
        """Verify heartbeat actually updates the timestamp."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CMUX_WORKSPACE_ID"] = "ws-timestamp"
            HARNESS._ACTIVE_DIR = Path(tmp) / "_active"
            
            root = Path(tmp) / "artifacts"
            root.mkdir(parents=True)
            
            marker = HARNESS.arm_task(
                task_id="heartbeat-timestamp",
                root=root,
                supervisor="surface:1",
                executor="surface:2",
                supervisor_provider="codex",
                executor_provider="claude",
            )
            
            collaboration_id = marker["collaboration_id"]
            marker_path = HARNESS._ACTIVE_DIR / "ws-timestamp" / f"{collaboration_id}.json"
            
            initial_time = marker["last_activity_at"]
            
            # Call the heartbeat itself (the artifact write path delegates to
            # this helper, but the contract under test is the helper).
            import time
            time.sleep(0.01)  # Ensure time advances
            HARNESS._touch_active_marker(root / "receipt.json")
            
            # Read marker and verify timestamp changed
            updated_marker = json.loads(marker_path.read_text())
            self.assertNotEqual(updated_marker["last_activity_at"], initial_time,
                              "heartbeat must update last_activity_at")

if __name__ == "__main__":
    unittest.main()
