#!/usr/bin/env python3
"""Test arm_task workspace_uuid alignment (task ccc-collab-fable-gap-20260903)."""
import json
from contextlib import ExitStack
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mac_harness as HARNESS

class WorkspaceUuidAlignmentTests(unittest.TestCase):
    """arm_task must write to the directory named by workspace_uuid."""

    def setUp(self):
        # Fixture workspace values are intentionally not real cmux identities.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        stack.enter_context(mock.patch.object(HARNESS, "_ACTIVE_DIR", HARNESS._ACTIVE_DIR))
        stack.enter_context(mock.patch("cmux_daemon_identity.collect", return_value=None))
    
    def test_arm_task_uses_passed_workspace_uuid_for_directory(self):
        """POISON: arm_task(..., workspace_uuid='X') must write under _active/X/."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CMUX_WORKSPACE_ID"] = "env-workspace"
            HARNESS._ACTIVE_DIR = Path(tmp) / "_active"
            
            root = Path(tmp) / "artifacts"
            root.mkdir(parents=True)
            
            marker = HARNESS.arm_task(
                task_id="uuid-align-test",
                root=root,
                supervisor="surface:1",
                executor="surface:2",
                supervisor_provider="codex",
                executor_provider="claude",
                workspace_uuid="explicit-workspace-uuid"
            )
            
            # The marker must be written to _active/explicit-workspace-uuid/, not env-workspace
            collaboration_id = marker["collaboration_id"]
            expected_path = HARNESS._ACTIVE_DIR / "explicit-workspace-uuid" / f"{collaboration_id}.json"
            wrong_path = HARNESS._ACTIVE_DIR / "env-workspace" / f"{collaboration_id}.json"
            
            self.assertTrue(expected_path.exists(),
                           f"marker must be written to {expected_path}")
            self.assertFalse(wrong_path.exists(),
                            "marker must NOT use env CMUX_WORKSPACE_ID when workspace_uuid is passed")
            
            # Verify the marker contains the correct workspace_uuid
            written = json.loads(expected_path.read_text())
            self.assertEqual(written["workspace_uuid"], "explicit-workspace-uuid")
    
    def test_arm_task_defaults_to_env_workspace_when_uuid_omitted(self):
        """Control: omitting workspace_uuid still works as before."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CMUX_WORKSPACE_ID"] = "env-workspace-default"
            HARNESS._ACTIVE_DIR = Path(tmp) / "_active"
            
            root = Path(tmp) / "artifacts"
            root.mkdir(parents=True)
            
            marker = HARNESS.arm_task(
                task_id="uuid-default-test",
                root=root,
                supervisor="surface:1",
                executor="surface:2",
                supervisor_provider="codex",
                executor_provider="claude",
            )
            
            collaboration_id = marker["collaboration_id"]
            expected_path = HARNESS._ACTIVE_DIR / "env-workspace-default" / f"{collaboration_id}.json"
            
            self.assertTrue(expected_path.exists())
            written = json.loads(expected_path.read_text())
            self.assertEqual(written["workspace_uuid"], "env-workspace-default")

if __name__ == "__main__":
    unittest.main()
