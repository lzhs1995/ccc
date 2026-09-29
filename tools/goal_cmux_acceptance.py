#!/usr/bin/env python3
"""Real TUI goal recovery with an intentionally unlinked, isolated rollout."""
import argparse
import contextlib
import dataclasses
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import ccc_codex_goal as goal
import ccc_guard_migration as migration
import ccc_guard_scope as scope
import cmux_codex_watch as core
from guard_cmux_acceptance import until

ERROR = "Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit."


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        data = b'{"data":[{"id":"gpt-6-astra","object":"model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        body = json.loads(data)
        title = bool(body.get("text", {}).get("format", {}).get("type") == "json_schema")
        with self.server.lock:
            number = len(self.server.requests)
            self.server.requests.append({"number": number, "title": title,
                "session_id": self.headers.get("session-id"), "thread_id": self.headers.get("thread-id"),
                "input": body.get("input", []), "at": time.monotonic()})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def event(value):
            self.wfile.write(("event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n").encode())
            self.wfile.flush()

        try:
            response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "status": "in_progress", "output": []}
            event({"type": "response.created", "response": response})
            if title:
                item = {"id": "msg_title", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": '{"title":"Local goal test"}', "annotations": []}]}
                event({"type": "response.output_item.done", "output_index": 0, "item": item})
                event({"type": "response.completed", "response": {**response, "status": "completed", "output": [item]}})
                return
            if not self.server.errored:
                self.server.errored = True
                while not self.server.release_error.wait(.02):
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                event({"type": "response.failed", "response": {**response, "status": "failed",
                    "error": {"code": "rate_limit_exceeded", "message": ERROR}}})
                return
            while not self.server.done.wait(.02):
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.lock, server.release_error, server.done = threading.Lock(), threading.Event(), threading.Event()
    server.requests, server.errored = [], False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="ccc-goal-cmux-") as temp:
        root = Path(temp)
        home = root / "codex"
        home.mkdir()
        config_path = root / "ccc" / "config.json"
        cfg = core.default_config()
        cfg.update(mode="armed", global_paused=False)
        core.atomic_write_json(config_path, cfg)
        (home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_provider = "local_goal"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\n'
            '[features]\ngoals = true\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
            '[model_providers.local_goal]\nname = "Local goal test"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            '\n[projects.' + json.dumps(str(root.resolve())) + ']\ntrust_level = "trusted"\n')
        client = migration.cmux_client(config_path)
        wid, daemon = None, None
        name = "CCC goal recovery test " + root.name
        try:
            raw = client._run(["--json", "new-workspace", "--name", name, "--cwd", str(root),
                "--focus", "false", "--env", "CODEX_HOME=" + str(home), "--env", "NO_PROXY=127.0.0.1,localhost",
                "--env", "no_proxy=127.0.0.1,localhost", "--command", shlex.join([guard.native_binary()])]).stdout
            def find():
                return next((w["id"] for win in client.tree().get("windows", []) for w in win.get("workspaces", [])
                             if w.get("title") == name), None)
            wid = until(find, label="test workspace")
            (output / "created-workspace.json").write_text(json.dumps({"workspace_id": wid, "root": str(root)}))
            target = until(lambda: next((r for r in scope.records(client.tree()).values()
                if r["workspace_id"] == wid and r["type"] == "terminal"), None), label="test surface")
            def grid():
                return core.Grid.from_rpc(client.replay(wid, target["surface_id"]), target["surface_id"])
            until(lambda: core._composer_status(grid())[0] == "empty", label="native composer")
            prompt = "/goal Continue this local recovery test until it is complete"
            client._run(["send", "--workspace", wid, "--surface", target["surface_id"], prompt])
            until(lambda: prompt in "\n".join(grid().lines), label="goal draft")
            client.send_key(wid, target["surface_id"], "enter")
            until(lambda: any(not r["title"] for r in server.requests), label="goal upstream request")
            native = until(lambda: next((r for r in scope.scan() if r["surface_id"] == target["surface_id"]), None), label="original native process")
            def read_goal():
                with contextlib.closing(sqlite3.connect(home / "goals_1.sqlite")) as db:
                    return db.execute("SELECT thread_id,goal_id,status FROM thread_goals LIMIT 1").fetchone()
            original = until(read_goal, label="original native goal")
            rollout = until(lambda: next(home.glob("sessions/**/rollout-*" + original[0] + ".jsonl"), None), label="original rollout")
            before = rollout.stat()
            content = rollout.read_bytes()
            rollout.unlink()
            rollout.write_bytes(content)
            assert rollout.stat().st_ino != before.st_ino
            server.release_error.set()
            proof = until(lambda: goal.blocked_goal(target, native["pid"]), timeout=60, label="native stalled-goal evidence")
            state = until(lambda: (s if (s := core.classify_grid(grid())).native_goal_stalled and s.error_type == "rate_limit" else None),
                          label="rate limit and native stalled footer")
            (output / "before.txt").write_text("\n".join(grid().lines))
            cfg["targets"] = [{**target, "enabled": True, "paused": False, "agent_kind": "codex", "name": "Local goal test"}]
            core.atomic_write_json(config_path, cfg)
            daemon = core.WatchDaemon(config_path, root / "ccc" / "state.json", client=client)
            def process_label(_):
                return {"agent_kind": "codex", "agent_pids": [native["pid"]]} if scope.matches(native) else {}
            daemon.codex_queue_recovery.process_lookup = process_label
            def recovered():
                daemon.process_once(client)
                return len([r for r in server.requests if not r["title"]]) == 2
            until(recovered, timeout=30, label="automatic native goal resume")
            runtime = daemon.runtime[target["surface_id"]]
            after = read_goal()
            assert after[:2] == original[:2] and after[2] == "active", after
            assert scope.matches(native), "original process was replaced"
            assert runtime.codex_sent_turn_key.startswith("goal:") and runtime.send_count == 1
            for _ in range(3):
                daemon.process_once(client)
                time.sleep(.2)
            assert len([r for r in server.requests if not r["title"]]) == 2, "duplicate continuation"
            result = {"original_session": original[0], "original_goal": original[1], "same_process": True,
                "unlinked_rollout": True, "restored_path_is_older_inode": True, "native_resume_once": True,
                "goal_status": after[2], "error_type": state.error_type, "proof": proof,
                "runtime": dataclasses.asdict(runtime)}
            (output / "goal-result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps({k: v for k, v in result.items() if k not in {"runtime", "proof"}}), flush=True)
        finally:
            server.release_error.set()
            server.done.set()
            (output / "requests.json").write_text(json.dumps(server.requests, indent=2))
            if wid:
                with contextlib.suppress(Exception):
                    (output / "last-screen.txt").write_text("\n".join(grid().lines))
                client._run(["close-workspace", "--workspace", wid])
            for path in home.glob("*.sqlite"):
                shutil.copyfile(path, output / path.name)
            if daemon:
                (output / "runtime.json").write_text(json.dumps({sid: dataclasses.asdict(r) for sid, r in daemon.runtime.items()}, indent=2))
                daemon._process_snapshots.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
