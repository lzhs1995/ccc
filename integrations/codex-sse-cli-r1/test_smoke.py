#!/usr/bin/env python3
"""Small fake metadata checks. No installed Codex or compiler is invoked."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import smoke


def valid():
    events = [{"type": "thread.started", "thread_id": "synthetic"}, {"type": "turn.started"},
              {"type": "error", "message": smoke.MESSAGE},
              {"type": "turn.failed", "error": {"message": "stream disconnected: " + smoke.MESSAGE}}]
    record = {"error_sent_at": 10.0, "peer_closed_at": 10.2,
              "server_closed_before_release": False, "handler_error": None}
    return {"returncode": 1, "events": events, "records": [record], "exit_at": 10.5, "killed": False}


class VerdictTests(unittest.TestCase):
    def test_accept_precise_failure(self):
        result = smoke.judge(**valid())
        self.assertTrue(result["accepted"])
        self.assertFalse(result["structured_code_observable_in_cli"])

    def test_reject_bad_terminal(self):
        for change in (lambda v: v.update(returncode=0), lambda v: v.update(killed=True),
                       lambda v: v.update(exit_at=18.0), lambda v: v.update(exit_at=9.0),
                       lambda v: v.update(records=[]),
                       lambda v: v["records"].append(copy.deepcopy(v["records"][0])),
                       lambda v: v["records"][0].update(error_sent_at=None),
                       lambda v: v["records"][0].update(peer_closed_at=None),
                       lambda v: v["records"][0].update(server_closed_before_release=True),
                       lambda v: v["records"][0].update(handler_error="EOF"),
                       lambda v: v["events"].pop(0),
                       lambda v: v["events"].append({"type": "turn.failed", "error": {"message": smoke.MESSAGE}}),
                       lambda v: v["events"].append({"type": "turn.completed"}),
                       lambda v: v["events"].insert(2, {"type": "item.completed", "item": {"type": "command_execution"}}),
                       lambda v: v["events"][-1]["error"].update(message="idle timeout"),
                       lambda v: v["events"][-1]["error"].update(code=smoke.CODE)):
            value = valid()
            change(value)
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                smoke.judge(**value)

    def test_environment_has_no_inherited_secrets_or_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            env = smoke.child_environment(private)
            self.assertEqual(set(env), {"PATH", "LANG", "RUST_LOG", "HOME", "CODEX_HOME", "TMPDIR",
                                       "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"})
            for key in ("HOME", "CODEX_HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                self.assertTrue(Path(env[key]).is_relative_to(private))

    def test_policy_has_only_one_network_destination(self):
        text = smoke.policy(Path("/fixture/codex"), Path("/fixture/private"), 12345)
        self.assertEqual(text.count("(allow network-"), 1)
        self.assertIn('(remote ip "localhost:12345")', text)
        self.assertNotIn("process-fork", text)
        self.assertNotIn("keychain", text)

    def test_command_disables_auth_and_retries(self):
        args = smoke.command(Path("/fixture/codex"), Path("/fixture/private"), 12345)
        cfg = dict(value.split("=", 1) for i, value in enumerate(args) if i > 0 and args[i - 1] == "-c")
        self.assertEqual(cfg["model_providers.fixture.requires_openai_auth"], "false")
        self.assertEqual(cfg["model_providers.fixture.supports_websockets"], "false")
        self.assertEqual(cfg["model_providers.fixture.stream_max_retries"], "0")
        self.assertEqual(cfg["model_providers.fixture.request_max_retries"], "0")
        self.assertGreater(json.loads(cfg["model_providers.fixture.stream_idle_timeout_ms"]), smoke.ERROR_DEADLINE * 1000)
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
