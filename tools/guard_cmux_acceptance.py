#!/usr/bin/env python3
"""Actual cmux + native TUI + B worker + local-only upstream acceptance.

Creates and removes only its recorded temporary workspace UUIDs. Every Codex
home/provider/config is isolated. Production CCC configuration is never edited.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import ccc_guard_migration as migration
import ccc_guard_scope as scope
import cmux_codex_watch as core
from guard_native_acceptance import MockServer


def until(callback, *, timeout=60, label="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except (OSError, RuntimeError, KeyError, ValueError) as exc:
            last = exc
        time.sleep(.15)
    raise RuntimeError(label + " timed out: " + str(last))


def run(output, count, legacy, *, early=0, lifecycle=False, fault=None, moved=False):
    output.mkdir(parents=True, exist_ok=True)
    servers = [MockServer(100000 if fault or moved else early or count), MockServer(100000), MockServer(1, complete=True)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="ccc-guard-cmux-") as temporary:
        root = Path(temporary)
        home = root / "codex"
        home.mkdir()
        (output / "fixture.json").write_text(json.dumps({"root": str(root), "codex_home": str(home)}))
        config_path = root / "ccc" / "config.json"
        cfg = core.default_config()
        cfg.update(mode="armed", global_paused=False)
        core.atomic_write_json(config_path, cfg)
        (home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_provider = "guard_mock"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\n'
            '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
            '[model_providers.guard_mock]\nname = "Local guard acceptance"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{servers[0].server_port}/v1"\n'
            'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
        with (home / "config.toml").open("a") as settings:
            settings.write('\n[projects.' + json.dumps(str(root.resolve())) + ']\ntrust_level = "trusted"\n')
        client = migration.cmux_client(config_path)
        created, service_identity = [], None
        starter = None
        suspended = []
        def create(name, command):
            name += " " + root.name[-8:]
            args = ["--json", "--id-format", "both", "new-workspace", "--name", name,
                "--cwd", str(root), "--focus", "false", "--env", "CODEX_HOME=" + str(home),
                "--env", "NO_PROXY=127.0.0.1,localhost", "--env", "no_proxy=127.0.0.1,localhost",
                "--command", command]
            raw = client._run(args).stdout
            (output / (name + "-create.txt")).write_text(raw)
            try:
                result = json.loads(raw)
                wid = core._find_string_key(result, "workspace_id")
            except ValueError:
                # Some cmux builds return a text acknowledgement for creation
                # even with --json. Reconcile the exact unique fixture title;
                # never repeat a successful or ambiguous create operation.
                result = raw
                def found():
                    candidates = [w["id"] for win in client.tree().get("windows", []) for w in win.get("workspaces", [])
                                  if w.get("title") == name]
                    return candidates[0] if len(candidates) == 1 else None
                wid = until(found, timeout=5, label="created workspace UUID")
            assert wid, result
            created.append(wid)
            (output / "created-workspaces.json").write_text(json.dumps(created))
            return wid
        def terminal(wid):
            rows = [r for r in scope.records(client.tree()).values() if r["workspace_id"] == wid and r["type"] == "terminal"]
            return rows[0] if rows else None
        def empty(target):
            grid = core.Grid.from_rpc(client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            return core._composer_status(grid)[0] == "empty" and not core._menu_present(grid.lines)
        def submit(target, prompt):
            until(lambda: empty(target), timeout=90, label="native TUI composer")
            client.send_text(target["workspace_id"], target["surface_id"], prompt)
            def draft_visible():
                grid = core.Grid.from_rpc(client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
                return core._composer_status(grid)[0] == "composer_busy" and prompt in "\n".join(grid.lines)
            until(draft_visible, timeout=5, label="test prompt rendered")
            client.send_key(target["workspace_id"], target["surface_id"], "enter")
        result = {}
        try:
            native = guard.native_binary()
            other = create("CCC guard acceptance control", shlex.join([native, "-c",
                "model_providers.guard_mock.base_url=" + json.dumps(f"http://127.0.0.1:{servers[1].server_port}/v1")]))
            control = until(lambda: terminal(other), label="control surface")
            submit(control, "Wait for local mock response")
            until(lambda: servers[1].requests, label="control upstream request")
            control_pid = next(r for r in scope.scan() if r["surface_id"] == control["surface_id"])
            command = "/bin/zsh"
            if legacy:
                command = shlex.join([native, "-c", "model_providers.guard_mock.base_url=" +
                    json.dumps(f"http://127.0.0.1:{servers[2].server_port}/v1")])
            wid = create("CCC guard acceptance B", command)
            original = until(lambda: terminal(wid), label="test surface")
            old_session = None
            if legacy:
                submit(original, "Historical local success must not trip a later B guard")
                until(lambda: servers[2].disconnected, label="historical model completion")
                old = next(r for r in scope.scan() if r["surface_id"] == original["surface_id"])
                old_session = migration.original_session({**old, "workspace_id": wid}, config_path)[0]
                until(lambda: empty(original), label="legacy composer")
            env = dict(os.environ, CODEX_HOME=str(home))
            code = ('import ccc_workspace_batch as b,sys,json; b.COUNT=int(sys.argv[3]); '
                    'print(json.dumps(b.start(sys.argv[1],sys.argv[2])),flush=True)')
            log = (output / "batch-start.log").open("w")
            starter = subprocess.Popen([sys.executable, "-B", "-c", code, str(config_path), wid, str(count)],
                cwd=Path(__file__).resolve().parents[1], env=env, stdout=log, stderr=log)
            log.close()
            until(lambda: starter.poll() is not None, timeout=180, label="B setup and original-session migration")
            if starter.returncode:
                raise RuntimeError((output / "batch-start.log").read_text())
            status = guard.request(config_path, "ping")
            service_identity = (status["pid"], scope.birth(status["pid"]))
            fault_at, moved_identity = None, None
            if fault or moved:
                until(lambda: len(servers[0].requests) == count, timeout=max(120, count * 10), label="all test requests")
                current = guard.request(config_path, "status", workspace_id=wid)
                last_session = servers[0].requests[-1].get("thread_header") or servers[0].requests[-1].get("session_header")
                last_sid, last_row = next((sid, row) for sid, row in current["surfaces"].items() if row["session_id"] == last_session)
                if moved:
                    sid, row = last_sid, last_row
                    moved_identity = scope.process(row["pid"])
                    client._run(["move-surface", "--surface", sid, "--workspace", other,
                                 "--pane", control["pane_id"], "--focus", "false"])
                    until(lambda: scope.records(client.tree()).get(sid, {}).get("workspace_id") == other,
                          label="moved test surface")
                    until(lambda: not guard.request(config_path, "status", workspace_id=wid)["surfaces"][sid]["in_scope"],
                          label="moved surface excluded")
                if fault:
                    pid = last_row["pid"] if fault == "backend-stall" else service_identity[0] if fault.startswith("guardian") else guard.read_json(
                        guard.guard_root(config_path) / "watchdog-heartbeat.json")["pid"]
                    generation = scope.birth(pid)
                    sig = signal.SIGKILL if fault.endswith("death") else signal.SIGSTOP
                    assert generation
                    fault_at = time.monotonic()
                    os.kill(pid, sig)
                    if sig == signal.SIGSTOP:
                        suspended.append((pid, generation))
                    if fault == "backend-stall":
                        fault_at = None
                        servers[0].ready.set()
                else:
                    servers[0].ready.set()
            last_progress = 0
            deadline = time.monotonic() + max(180, count * 10)
            while time.monotonic() < deadline:
                current = guard.snapshot(config_path, wid)
                if current.get("phase") in {"stopped", "failed"}:
                    break
                if time.monotonic() - last_progress > 10:
                    rule = core.workspace_rule_by_id(core.ConfigStore(config_path).load(), wid)
                    job = guard.read_json(config_path.parent / "workspace-batches" / rule["last_batch_id"] / "job.json")
                    from ccc_workspace_batch import counts
                    print(json.dumps({"counts": counts(job), "requests": len(servers[0].requests),
                                      "guard": current.get("phase")}), flush=True)
                    last_progress = time.monotonic()
                time.sleep(.1)
            requested = len(servers[0].requests)
            expected_disconnects = requested - int(moved)
            until(lambda: len(servers[0].disconnected) == expected_disconnects, timeout=5, label="all scoped upstream streams disconnected")
            until(lambda: guard.snapshot(config_path, wid).get("phase") in {"stopped", "failed"}, timeout=5, label="stop confirmation after disconnect")
            stopped = guard.snapshot(config_path, wid) if fault and fault.startswith("guardian") else guard.request(config_path, "status", workspace_id=wid)
            after = len(servers[0].requests)
            time.sleep(1)
            assert fault_at or servers[0].first_response, {"reason": "pool stopped before any test model response",
                                                          "guard": stopped, "requests": after}
            result = {"count": count, "workspace_id": wid, "guard": stopped,
                "max_upstream_disconnect_ms": max((x - (fault_at or servers[0].first_response)) * 1000 for x in servers[0].disconnected.values()),
                "other_workspace_unchanged": scope.matches(control_pid) and not servers[1].disconnected,
                "no_extra_requests": len(servers[0].requests) == after == requested,
                "requested": requested, "fault": fault,
                "moved_surface_running": bool(moved_identity and scope.matches(moved_identity)),
                "original_session": old_session,
                "same_original_session": not legacy or stopped["surfaces"].get(original["surface_id"], {}).get("session_id") == old_session}
            backup = guard.read_json(guard.guard_root(config_path) / "watchdog-stop.json")
            backup_used = fault == "backend-stall" and backup.get("reason") == "guardian_heartbeat_lost"
            assert stopped["phase"] == "stopped" and stopped["trip"]["connected"] == (not fault or fault == "backend-stall" and not backup_used) and stopped["trip"]["within_deadline"], result
            if fault == "backend-stall":
                if backup_used:
                    assert not backup.get("coverage_error") and all(r.get("stop_proof") for r in backup["backends"] if r["in_scope"])
                    assert next(r for r in backup["backends"] if r["pid"] == last_row["pid"])["signal"] == "SIGKILL"
                    result["watchdog_fallback"] = backup
                else:
                    assert stopped["surfaces"][last_sid]["signal"] == "SIGKILL", result
            assert result["other_workspace_unchanged"] and result["no_extra_requests"] and result["same_original_session"], result
            assert result["max_upstream_disconnect_ms"] <= 1000, result
            if early:
                assert requested < count, result
            if moved:
                assert result["moved_surface_running"], result
            if lifecycle:
                for pid, generation in suspended:
                    if scope.birth(pid) == generation:
                        os.kill(pid, signal.SIGCONT)
                suspended.clear()
                store = core.ConfigStore(config_path)
                old_job = core.workspace_rule_by_id(store.load(), wid)["last_batch_id"]
                session_ids = {sid: row["session_id"] for sid, row in stopped["surfaces"].items()}
                core.cli(["--config", str(config_path), "resume-workspace", wid])
                running = guard.request(config_path, "ping")
                service_identity = (running["pid"], scope.birth(running["pid"]))
                watching = guard.request(config_path, "status", workspace_id=wid)
                assert watching["phase"] == "watching", watching
                assert {sid: row["session_id"] for sid, row in watching["surfaces"].items()} == session_ids
                time.sleep(1)
                assert len(servers[0].requests) == after, "W replayed a prompt or refilled cancelled slots"
                assert len(watching["surfaces"]) == len(stopped["surfaces"])
                servers[0].count = 1
                servers[0].arrived.clear()
                servers[0].disconnected.clear()
                servers[0].ready.clear()
                servers[0].first_response = None
                sid = next(s for s in session_ids if s != original["surface_id"])
                submit(scope.records(client.tree())[sid], "Explicit local test turn after W")
                until(lambda: guard.snapshot(config_path, wid).get("phase") == "stopped", label="W fresh-response interruption")
                assert len(servers[0].requests) == after + 1
                servers[0].count = 2
                servers[0].arrived.clear()
                servers[0].disconnected.clear()
                servers[0].ready.clear()
                servers[0].first_response = None
                with (output / "second-batch.log").open("w") as log:
                    starter = subprocess.Popen([sys.executable, "-B", "-c", code, str(config_path), wid, "2"],
                        cwd=Path(__file__).resolve().parents[1], env=env, stdout=log, stderr=log)
                until(lambda: starter.poll() is not None, timeout=180, label="second B setup")
                assert starter.returncode == 0, (output / "second-batch.log").read_text()
                until(lambda: len(servers[0].requests) == after + 3 and guard.snapshot(config_path, wid).get("phase") == "stopped",
                      timeout=90, label="new B after stopped success")
                assert core.workspace_rule_by_id(store.load(), wid)["last_batch_id"] != old_job
                assert len(guard.snapshot(config_path, wid)["surfaces"]) == len(session_ids) + 2
                result["lifecycle"] = {"W_same_sessions": True, "W_no_prompt_replay": True,
                    "W_no_cancelled_slot_refill": True, "B_new_batch_after_success": True}
            (output / "cmux-result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps({k: v for k, v in result.items() if k != "guard"}), flush=True)
        except BaseException as exc:
            result["error"] = repr(exc)
            (output / "cmux-failure.json").write_text(json.dumps(result, indent=2))
            raise
        finally:
            import shutil
            for pid, generation in suspended:
                if scope.birth(pid) == generation:
                    os.kill(pid, signal.SIGCONT)
            (output / "mock-requests.json").write_text(json.dumps([
                {"port": server.server_port, "requests": server.requests, "disconnected": server.disconnected}
                for server in servers], indent=2))
            for wid in created:
                with contextlib.suppress(Exception):
                    for target in scope.records(client.tree()).values():
                        if target["workspace_id"] == wid and target["type"] == "terminal":
                            screen = client.replay(wid, target["surface_id"])
                            (output / (target["surface_id"] + "-viewport.json")).write_text(json.dumps(screen, indent=2))
                            grid = core.Grid.from_rpc(screen, target["surface_id"])
                            (output / (target["surface_id"] + "-screen.txt")).write_text("\n".join(grid.lines))
            for path in home.glob("sessions/**/*.jsonl"):
                shutil.copyfile(path, output / path.name)
            for path in config_path.parent.rglob("*.log"):
                shutil.copyfile(path, output / (path.parent.name + "-" + path.name))
            for path in config_path.parent.rglob("state.json"):
                shutil.copyfile(path, output / (path.parent.name + "-state.json"))
            for path in guard.guard_root(config_path).glob("*.json"):
                shutil.copyfile(path, output / ("guard-" + path.name))
            shutil.copyfile(config_path, output / "ccc-config.json")
            for path in config_path.parent.glob("workspace-batches/*/job.json"):
                shutil.copyfile(path, output / (path.parent.name + "-job.json"))
            for path in config_path.parent.glob("batch-guards/*/migrations/*/*.json"):
                value = json.loads(path.read_text())
                for key in ("environment", "viewport", "config_args"):
                    value.pop(key, None)
                (output / (path.parent.name + "-migration-" + path.name)).write_text(json.dumps(value, indent=2))
            for wid in created:
                with contextlib.suppress(Exception):
                    if guard.provenance(config_path, wid):
                        guard.request(config_path, "stop", timeout=3, workspace_id=wid, reason="acceptance_cleanup")
            if service_identity is None:
                with contextlib.suppress(Exception):
                    current = guard.request(config_path, "ping", timeout=.5)
                    service_identity = (current["pid"], scope.birth(current["pid"]))
            if service_identity and scope.birth(service_identity[0]) == service_identity[1]:
                os.kill(service_identity[0], signal.SIGTERM)
            for wid in reversed(created):
                with contextlib.suppress(Exception):
                    client._run(["close-workspace", "--workspace", wid], timeout=10)
            if service_identity:
                with contextlib.suppress(Exception):
                    until(lambda: scope.birth(service_identity[0]) != service_identity[1], timeout=5, label="fixture guardian exit")
            if starter and starter.poll() is None:
                starter.terminate()
                starter.wait(timeout=10)
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--early", type=int, default=0)
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--fault", choices=["guardian-stall", "guardian-death", "watchdog-stall", "watchdog-death", "backend-stall"])
    parser.add_argument("--moved", action="store_true")
    args = parser.parse_args()
    run(args.output, args.count, args.legacy, early=args.early, lifecycle=args.lifecycle, fault=args.fault, moved=args.moved)
