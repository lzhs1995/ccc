"""Unit tests for 50-way unthrottled AnyRouter burst retry and zero-config Multi-Herdr adaptation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from ccc_herdr_auto import (
    CODEX_OPEN_TURN_KINDS,
    CODEX_OPTIONAL_THREAD_SETTINGS,
    codex_herdr_screen_admission,
    discover_herdr_socket_candidates,
    extract_resume_uuid,
    herdr_placement_matches,
    normalize_herdr_pane_agent,
    prune_orphaned_pre_io_intents,
    rollover_ledger,
    safe_regular_bytes,
)
from ccc_network_client import NetworkClient
from ccc_provider_retry import ProviderRetryStore, structured_error_type
import cmux_codex_watch as watch


class BurstAndMultiHerdrTests(unittest.TestCase):
    def test_50_way_concurrent_burst_retry_never_serializes_or_drops_slots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ProviderRetryStore(Path(td) / "provider-retry.sqlite3")
            decisions_r1 = []
            for idx in range(50):
                ev = store.observe(
                    f"01a1f100-0000-7000-8000-{idx:012d}",
                    "anyrouter",
                    f"turn-r1-{idx}",
                    "high_demand",
                    "We’re currently experiencing high demand, which may cause temporary errors.",
                    1795050000.0 + idx * 0.001,
                )
                self.assertIsNotNone(ev)
                d = store.evaluate(ev, f"attempt-r1-{idx}", reserve=True)
                decisions_r1.append(d.allowed)
                # Duplicate retry on the same failed turn must be blocked
                dup = store.evaluate(ev, f"attempt-r1-dup-{idx}", reserve=True)
                self.assertFalse(dup.allowed)
            self.assertEqual(sum(decisions_r1), 50)

            time.sleep(0.26)
            decisions_r2 = []
            for idx in range(50):
                ev2 = store.observe(
                    f"01a1f100-0000-7000-8000-{idx:012d}",
                    "anyrouter",
                    f"turn-r2-{idx}",
                    "rate_limit",
                    "429 rate limit exceeded",
                    1795050005.0 + idx * 0.001,
                )
                d2 = store.evaluate(ev2, f"attempt-r2-{idx}", reserve=True)
                decisions_r2.append(d2.allowed)
            self.assertEqual(sum(decisions_r2), 50)

    def test_codex_0170_footers_and_plan_mode_admission(self) -> None:
        screen_fast = (
            "› Reply only OK. Do not use tools. End the turn.\n\n"
            "■ We’re currently experiencing high demand, which may cause temporary errors.\n\n"
            "› Ask Codex to do anything\n\n"
            "  GPT-6-Astra max fast · ~/work/15 · Context 0% used · Fast\n"
            "  ← for agents · ? for shortcuts   ⚠ 2 warnings · f2 to view\n"
        )
        ok, reason = codex_herdr_screen_admission(screen_fast)
        self.assertTrue(ok, reason)

        screen_plan = (
            "› Continue\n\n"
            "■ We’re currently experiencing high demand, which may cause temporary errors.\n\n"
            "› Ask Codex to do anything\n\n"
            "  GPT-6-Astra medium fast · ~/Documents/project/…\n"
            "  ← for agents  Plan mode (⇧tab to cycle) · ⚠ 5 warnings · f2 to view\n"
        )
        ok2, reason2 = codex_herdr_screen_admission(screen_plan)
        self.assertTrue(ok2, reason2)

    def test_multi_herdr_discovery_wrapper_env_and_ledger_rollover(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / "herdr"
            (root / "sessions" / "stata").mkdir(parents=True, mode=0o700)
            (root / "sessions" / "c3-llm-wiki").mkdir(parents=True, mode=0o700)
            os.chmod(root, 0o700)
            os.chmod(root / "sessions", 0o700)
            cands, refs = discover_herdr_socket_candidates(root)
            self.assertEqual([c[0] for c in cands], ["default", "c3-llm-wiki", "stata"])
            self.assertEqual(refs, [])

            ep = {
                "session": "stata",
                "socket": str(root / "sessions" / "stata" / "herdr.sock"),
                "cmux_workspace_id": "00000000-0000-4000-8000-000000000005",
                "cmux_surface_id": "00000000-0000-4000-8000-000000000004",
            }
            pane = {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}
            env = {
                "HERDR_ENV": "0",
                "HERDR_SOCKET_PATH": ep["socket"],
                "HERDR_PANE_ID": "w1:p1",
                "HERDR_WORKSPACE_ID": "w1",
                "HERDR_TAB_ID": "w1:t1",
                "HERDR_SESSION": "stata",
                "CMUX_WORKSPACE_ID": ep["cmux_workspace_id"],
                "CMUX_SURFACE_ID": ep["cmux_surface_id"],
            }
            self.assertTrue(herdr_placement_matches(ep, pane, env))
            sid = "00000000-0000-4000-8000-000000000001"
            argv = ["codex", "-c", 'model_providers.custom.base_url="http://127.0.0.1:3515/v1"', "resume", sid]
            self.assertEqual(extract_resume_uuid(argv, "codex"), sid)

            ledger_path = Path(td).resolve() / "herdr-input-ledger.json"
            attempts = {
                f"done-{i}": {"phase": "native_received", "attempted_at": float(i)}
                for i in range(2500)
            }
            attempts["pending-1"] = {"phase": "submitted", "attempted_at": 9999.0}
            ledger = {"schema": "herdr-input-ledger-v174", "attempts": attempts}
            archived = rollover_ledger(ledger_path, ledger, frozenset({"native_received"}), keep=1000, trigger=2000)
            self.assertEqual(archived, 1501)
            self.assertEqual(len(ledger["attempts"]), 1000)
            self.assertIn("pending-1", ledger["attempts"])

    def test_network_client_tolerates_25s_probe_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            cfg_path = state_dir / "network.json"
            now = 1795050000.0
            cfg_path.write_text(json.dumps({
                "state_dir": str(state_dir),
                "service_host": "anyrouter.example",
                "probe": {"validation_mode": "reachability"},
            }))
            (state_dir / "status.json").write_text(json.dumps({
                "version": 1,
                "service_host": "anyrouter.example",
                "validation_mode": "reachability",
                "phase": "healthy",
                "at": now - 22.0,
            }))
            client = NetworkClient()
            snap = client.snapshot({"enabled": True, "config_path": str(cfg_path)}, now=now)
            self.assertEqual(snap["phase"], "healthy")

    def test_orphaned_pre_io_intent_pruning_and_herdr_22mb_ledger_guard(self) -> None:
        ledger = {
            "acc-1": {"failed_turn_id": "turn-0", "phase": "accepted", "io_started_at": 100.0},
            "orph-1": {"failed_turn_id": "turn-1", "phase": "intent"},
            "sent-2": {"failed_turn_id": "turn-2", "phase": "intent", "io_started_at": 200.0},
        }
        removed = prune_orphaned_pre_io_intents(ledger, "turn-1", transcript_unchanged=True)
        self.assertEqual(removed, ["orph-1"])
        self.assertIn("acc-1", ledger)
        self.assertIn("sent-2", ledger)
        self.assertEqual(prune_orphaned_pre_io_intents(ledger, "turn-2", transcript_unchanged=True), [])

        pane = {"pane_id": "w1:p3", "agent": None, "agent_session": {"agent": "codex", "value": "00000000-0000-4000-8000-000000000002"}}
        self.assertEqual(normalize_herdr_pane_agent(pane)["agent"], "codex")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td).resolve() / "ledger-22mb.json"
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(b"x" * (22 * 1024 * 1024))
            self.assertEqual(len(safe_regular_bytes(p)), 22 * 1024 * 1024)

    def test_anyrouter_bad_response_400_invalid_encrypted_content_auto_continues(self) -> None:
        wrapped_banner = (
            '■ {"error":{"message":"bad response status code 400 (request id:\n'
            '20261119194608146851059KqBH5yfb)","type":"invalid_request_error","para\n'
            'm":"","code":"invalid_encrypted_content"}}'
        )
        self.assertEqual(structured_error_type(wrapped_banner), "encrypted_function_output")
        self.assertEqual(watch._match_error_block(wrapped_banner), "encrypted_function_output")
        with tempfile.TemporaryDirectory() as td:
            store = ProviderRetryStore(Path(td) / "provider-retry.sqlite3")
            ev = store.observe(
                "00000000-0000-4000-8000-000000000003",
                "custom",
                "turn-anyrouter-iec",
                "encrypted_function_output",
                wrapped_banner,
                1795092000.0,
            )
            self.assertIsNotNone(ev)
            decision = store.evaluate(ev, "attempt-anyrouter-iec-1", reserve=True)
            self.assertTrue(decision.allowed)

        strict_iec = (
            '■ unexpected status 400 Bad Request: {"error":{"message":"The encrypted content could not be verified.",'
            '"type":"invalid_request_error","param":"input","code":"invalid_encrypted_content"}}'
        )
        self.assertEqual(structured_error_type(strict_iec), "invalid_encrypted_content")
        self.assertEqual(watch._match_error_block(strict_iec), "invalid_encrypted_content")

    def test_narrow_herdr_split_pane_footer_wraps_and_codex_0170_goal_schema(self) -> None:
        narrow_spatial = (
            "■ stream disconnected before completion: cooldown probe incomplete (504: you can retry your request)\n"
            "» Ask Codex to do anything\n"
            "  GPT-6.2-Astra ultra fast · ~/Documents/example/tasks/01_R_analysis ·\n"
            "  Context 65% used · 258K window · Fast · Main [default]\n"
            "  ← for agents                Goal stalled (/goal resume) · ⚠ 4 · f2\n"
        )
        ok, reason = codex_herdr_screen_admission(narrow_spatial)
        self.assertTrue(ok, reason)

        wrapped_window = (
            "■ stream disconnected before completion (504: you can retry your request)\n"
            "» Ask Codex to do anything\n"
            "  GPT-6.2-Astra ultra fast · ~/Documents/example/tasks/01_R_analysis · Fast · Context 35% used · 258K\n"
            "  window · Main [default]\n"
            "  ← for agents · ? for shortcuts                 Goal stalled (/goal resume) · ⚠ 8 warnings · f2 to view\n"
        )
        ok2, reason2 = codex_herdr_screen_admission(wrapped_window)
        self.assertTrue(ok2, reason2)

        self.assertIn("user_message", CODEX_OPEN_TURN_KINDS)
        self.assertIn("requested_approvals_reviewer", CODEX_OPTIONAL_THREAD_SETTINGS)

    def test_model_unavailable_for_group_is_classified_as_blocked(self) -> None:
        samples = [
            '■ unexpected status 404 Not Found: {"error":{"message":"Model \\"gpt-5.6-sol\\" is not available for this group.","type":"one_api_error","param":"","code":"model_not_found"}}',
            '■ stream disconnected before completion: gateway: magpie knows no model "claude-opus-4-6".',
            '■ unexpected status 403 Forbidden: {"error":{"message":"Model \\"gpt-5.6-sol\\" is not supported by any configured account in this group","type":"new_api_error"}}',
            '■ stream disconnected before completion: model "gpt-5.6-sol" is set not to be served by any account or key.',
            '■ unexpected status 403 Forbidden: model not allowed for the gateway key',
        ]
        for msg in samples:
            self.assertEqual(structured_error_type(msg), "model_unavailable_for_group", msg)


if __name__ == "__main__":
    unittest.main()
