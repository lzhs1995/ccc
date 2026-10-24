"""沿用42原 helper 端到端用例，仅改接现役 native PostToolUse API。"""
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import cmux_agent_adapter as A
import cmux_bridge as B
import cmux_helper_evidence as E
import cmux_message_journal as J
import cmux_native_delivery as N
import cmux_native_delivery_guard as G
import cmux_submission_inputs as I
from native_test_support import historical_terminal_dispatch

ROOT = Path(__file__).resolve().parents[1]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value

F = module("test_sender_path42_v1", ROOT / "tests/delivery_paths/test_sender_path42_v1.py")
H = module("test_helper_native_guard43_v1", ROOT / "tests/delivery_paths/test_helper_native_guard43_v1.py")
F.B, F.J, F.N = B, J, N
H.A, H.B, H.E, H.G, H.I, H.J, H.N = A, B, E, G, I, J, N

class HookEnforcementCases(H.HelperGuardCases):
    def setUp(self):
        super().setUp()
        # These inherited cases exercise old paste/key journals, never the live
        # daemon. Native routing has its own explicitly fake-RPC test suite.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch("cmux_codex_input.DaemonRPC",
            side_effect=AssertionError("offline fixture forbids live Codex RPC")))
        stack.enter_context(mock.patch.object(B, "_submit_text_once",
            side_effect=historical_terminal_dispatch))

    # 继承原业务反例；补验真实 main 的阻断退出码与只读恢复文案。
    def test_hook_main_blocks_newline_and_only_offers_original_readonly_intent(self):
        self.on_key = lambda key, count: setattr(self, "view", self.compose(self.payload + "\n"))
        self.assertEqual(self.invoke()[0], 75)
        payload = json.dumps({"tool_input": {"command": self.command}})
        before = self.files(), list(self.keys), list(self.pastes)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = G.main()
        self.assertEqual(code, 2)
        self.assertIn("NATIVE_UNVERIFIABLE", err.getvalue())
        self.assertIn(str(self.intent), err.getvalue())
        self.assertNotIn("--recover-stranded", err.getvalue())
        self.assertEqual((self.files(), self.keys, self.pastes), before)

    def test_hook_main_accepts_full_native_receipt(self):
        self.receive()
        payload = json.dumps({"tool_input": {"command": self.command}})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = G.main()
        self.assertEqual(code, 0, err.getvalue())
        wire = json.loads(out.getvalue())
        self.assertEqual(set(wire), {"hookSpecificOutput"})
        specific = wire["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        result = json.loads(specific["additionalContext"])
        self.assertEqual(result["action"], "pass")
        self.assertEqual(result["results"][0]["state"], "NATIVE_RECEIVED")
        self.assertEqual(result["results"][0]["helper_proof"]["source"], "revalidated_helper_original_intent")

    def test_ordinary_read_needs_no_delivery_or_identity(self):
        with mock.patch.object(G.cmux_hook_identity, "identity", side_effect=AssertionError("ordinary read resolved identity")):
            result = G.evaluate({"tool_input": {"command": "rtk git status"}})
        self.assertEqual(result, {"action": "skip", "results": []})

def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(HookEnforcementCases)

if __name__ == "__main__":
    unittest.main()
