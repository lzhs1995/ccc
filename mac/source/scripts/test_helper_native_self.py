"""Self diagnostics must agree with the transport's native foreground caller."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_agent_adapter as adapter
import cmux_workspace_guard as guard
import render_cmux_agent as renderer


class HelperNativeSelfTests(unittest.TestCase):
    def test_self_uses_native_snapshot_without_input(self):
        current = {"caller": {"surface_ref": "surface:5994", "workspace_ref": "workspace:1"}}
        out = io.StringIO()
        with patch.object(guard, "caller_snapshot", return_value=(current, {}, {}, {"native": True})) as snap, \
                patch.object(adapter.Adapter, "deliver", side_effect=AssertionError("self sent input")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(adapter.main(["self"]), 0)
        snap.assert_called_once_with()
        self.assertEqual(json.loads(out.getvalue())["caller"], current["caller"])

    def test_unresolved_self_never_returns_inherited_identity(self):
        out = io.StringIO()
        with patch.object(guard, "caller_snapshot", side_effect=RuntimeError("unresolved")), \
                contextlib.redirect_stderr(out):
            self.assertEqual(adapter.main(["self"]), 75)
        self.assertNotIn("caller", json.loads(out.getvalue()))

    def test_renderer_routes_self_before_legacy_case(self):
        root = Path(renderer.__file__).resolve().parents[1]
        import sys
        raw = renderer.render((root / "tests/fixtures/cmux-agent-legacy.sh").read_bytes(),
            expected_sha256=renderer.BASELINE_SHA256, python=sys.executable,
            adapter=root / "scripts/cmux_agent_adapter.py")
        text = raw.decode()
        self.assertLess(text.index("self|ask|send|broadcast|reconcile)"), text.index("  self)"))
        self.assertTrue(renderer.verify_route(raw, root)["route_verified"])


if __name__ == "__main__":
    unittest.main()
