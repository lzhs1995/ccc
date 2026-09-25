import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_network_client import NetworkClient, configured_host, error_host


class NetworkClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "network.json"
        self.path.write_text(json.dumps({"state_dir": str(self.root), "service_host": "anyrouter.test"}))
        self.options = {"enabled": True, "config_path": str(self.path)}
        self.client = NetworkClient()
        self.target = {"surface_id": "surface-a", "workspace_id": "workspace-a"}
        self.turn = {"kind": "task_complete", "session_id": "original", "pid": 42, "process_start": 100,
                     "error": {"message": "error sending request for url (https://anyrouter.test/v1/responses)"}}
        self.status()

    def status(self, phase="network_wait", at=None, mode="manage"):
        value = {"version": 1, "service_host": "anyrouter.test", "mode": mode,
                 "phase": phase, "at": time.time() if at is None else at}
        (self.root / "status.json").write_text(json.dumps(value))
        self.client.cache = (0, "", {})

    def test_disabled_feature_has_no_io(self):
        with mock.patch("ccc_network_client.bounded_json", side_effect=AssertionError("must not read")):
            self.assertFalse(self.client.verdict({"enabled": False}, self.target, self.turn)["blocked"])

    def test_only_matching_service_gets_network_wait(self):
        self.assertTrue(self.client.verdict(self.options, self.target, self.turn)["blocked"])
        other = {**self.turn, "error": {"message": "error for url (https://api.openai.com/v1/responses)"}}
        verdict = self.client.verdict(self.options, self.target, other)
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["service_host"], "api.openai.com")

    def test_unknown_binding_does_not_pause_other_providers(self):
        with mock.patch("ccc_network_client.configured_host", return_value=""):
            verdict = self.client.verdict(self.options, self.target, {**self.turn, "error": {"message": "network error"}})
        self.assertEqual(verdict["phase"], "unbound")
        self.assertFalse(verdict["blocked"])

    def test_missing_stale_future_or_observe_snapshot_does_not_claim_outage(self):
        for stamp, mode in ((time.time() - 11, "manage"), (time.time() + 50, "manage"), (time.time(), "observe")):
            self.status(at=stamp, mode=mode)
            self.assertFalse(self.client.verdict(self.options, self.target, self.turn)["blocked"])
        (self.root / "status.json").write_text("partial")
        self.client.cache = (0, "", {})
        self.assertFalse(self.client.verdict(self.options, self.target, self.turn)["blocked"])

    def test_error_url_requires_an_exact_host(self):
        fake = {"error": {"message": "url (https://anyrouter.test.evil.invalid/v1/responses)"}}
        self.assertEqual(error_host(fake), "anyrouter.test.evil.invalid")
        self.assertFalse(self.client.verdict(self.options, self.target, fake)["blocked"])

    def process_config(self, extra_args=(), *, config_mtime=99, birth_change=False):
        codex_dir = self.root / "codex"
        codex_dir.mkdir(exist_ok=True)
        path = codex_dir / "config.toml"
        path.write_text('model_provider="custom"\n[model_providers.custom]\nbase_url="https://anyrouter.test/v1"\n')
        os.utime(path, (config_mtime, config_mtime))
        argv = ["codex", *extra_args]
        env = {"CODEX_HOME": str(codex_dir), "CMUX_SURFACE_ID": "surface-a", "CMUX_WORKSPACE_ID": "workspace-a"}
        with mock.patch("ccc_guard_scope.birth", side_effect=[[100, 1], [101, 2]] if birth_change else None,
                        return_value=[100, 1]), mock.patch("ccc_guard_scope.arguments", return_value=(argv, env)):
            return configured_host({**self.turn, "error": None, "model_provider": "custom"}, self.target)

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_startup_overrides_and_config_drift_are_respected(self):
        self.assertEqual(self.process_config(), "anyrouter.test")
        self.assertEqual(self.process_config(("-c", 'model_providers.custom.base_url="https://other.test/v1"')), "other.test")
        self.assertEqual(self.process_config(config_mtime=102), "")
        self.assertEqual(self.process_config(birth_change=True), "")


class NetworkContinuationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_codex_status_chrome import status_payload, visible_text, ERRORS
        from tests.test_watch import FakeClient, armed_daemon
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        payload = status_payload("high_demand")
        self.client = FakeClient(payload, visible_text(payload))
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon._mutate_config(lambda c: c.update(network_guard={"enabled": True, "config_path": str(Path(self.tmp.name) / "network.json")}))
        self.turn = {"kind": "task_complete", "session_id": "original", "turn_id": "one", "at": 100,
                     "error": {"message": ERRORS["high_demand"]}}
        self.daemon.codex_queue_recovery.current_turn = mock.Mock(return_value=self.turn)
        self.verdict = mock.Mock(return_value={"blocked": True, "phase": "network_wait", "service_host": "anyrouter.test"})
        self.daemon.network.verdict = self.verdict

    def recover(self):
        self.verdict.return_value = {"blocked": False, "phase": "healthy", "service_host": "anyrouter.test"}

    def test_wait_and_recovery_preserve_failed_turn_deduplication(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        self.assertEqual(self.client.sent, [])
        self.assertEqual(runtime.state, "network_wait")
        self.assertEqual(runtime.send_count, 0)
        self.recover()
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        runtime.awaiting, runtime.last_send_at = False, 0
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_network_recovery_cannot_override_manual_pause(self):
        self.daemon.process_once(self.client)
        self.daemon._mutate_config(lambda c: c.update(global_paused=True))
        self.recover()
        self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_network_recovery_cannot_revive_a_completed_or_new_native_turn(self):
        self.daemon.process_once(self.client)
        self.recover()
        for replacement in ({**self.turn, "error": None}, {"kind": "task_started", "session_id": "new"}):
            self.daemon.codex_queue_recovery.current_turn.return_value = replacement
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_b_stop_remains_an_independent_earlier_gate(self):
        target = self.daemon.config["targets"][0]
        with mock.patch("ccc_batch_guard.blocked", return_value=True):
            self.assertIsNone(self.daemon._active_send_target(target))
        self.verdict.assert_not_called()


if __name__ == "__main__":
    unittest.main()
