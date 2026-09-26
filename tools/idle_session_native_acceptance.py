#!/usr/bin/env python3
"""Verify original-session retry using a native CLI and a private loopback API."""
import argparse
import fcntl
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import ccc_codex_queue as native
import ccc_guard_scope as scope
from ccc_native_processes import NativeProcessIndex
import cmux_codex_watch as core

ERROR = "We’re currently experiencing high demand, which may cause temporary errors."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        body = json.loads(raw)
        messages = [item for item in body.get("input", []) if item.get("role") == "user"]
        user_text = "\n".join(part.get("text", "") for part in (messages[-1].get("content", []) if messages else [])
                              if part.get("type") == "input_text")
        title = user_text.startswith("Generate a concise, single-line task title")
        self.server.requests.append({"model": body.get("model"), "at": time.time(),
                                     "user_text": user_text, "native_title": title,
                                     "thread_id": self.headers.get("thread-id"),
                                     "body_sha256": hashlib.sha256(raw).hexdigest()})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "status": "in_progress", "output": []}
        events = [{"type": "response.created", "response": response}]
        if getattr(self.server, "fail_first", True) and len(self.server.requests) == 1:
            events.append({"type": "response.failed", "response": {
                **response, "status": "failed", "error": {"code": "server_error", "message": ERROR}}})
        else:
            reply = json.dumps({"title": "Show power"}) if title else "OK"
            item = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": reply, "annotations": []}]}
            events.extend([{"type": "response.output_item.added", "output_index": 0,
                            "item": {**item, "status": "in_progress", "content": []}},
                           {"type": "response.output_text.delta", "item_id": item["id"],
                            "output_index": 0, "content_index": 0, "delta": reply},
                           {"type": "response.output_item.done", "output_index": 0, "item": item},
                           {"type": "response.completed", "response": {**response, "status": "completed", "output": [item]}}])
        for event in events:
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="ccc-idle-native-") as temp:
        root = Path(temp).resolve()
        native_home = root / "codex"
        native_home.mkdir()
        (native_home / "config.toml").write_text(
            'model = "gpt-6-astra"\nmodel_provider = "local_fixture"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\ncheck_for_update_on_startup = false\n'
            '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
            '[model_providers.local_fixture]\nname = "Loopback fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n')
        target = {"surface_id": str(uuid.uuid4()).upper(), "workspace_id": str(uuid.uuid4()).upper()}
        env = dict(os.environ, CODEX_HOME=str(native_home), TERM="xterm-256-color",
                   CMUX_SURFACE_ID=target["surface_id"], CMUX_WORKSPACE_ID=target["workspace_id"],
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                     "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_SQLITE_HOME", "CMUX_SOCKET_PATH"):
            env.pop(name, None)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        binary = Path(guard.native_binary())
        process = subprocess.Popen([str(binary), "--no-alt-screen", "-C", str(root), "Reply OK. Do not use tools."],
            stdin=slave, stdout=slave, stderr=slave, cwd=root, env=env, start_new_session=True)
        os.close(slave)
        raw_screen = bytearray()
        def drain(seconds=.05):
            if select.select([master], [], [], seconds)[0]:
                data = os.read(master, 262144)
                raw_screen.extend(data)
                if b"\x1b[6C" in data:
                    pass
                if b"\x1b[6R" in data or b"\x1b[6n" in data:
                    os.write(master, b"\x1b[1;1R")
        try:
            queue = native.QueueRecovery(root / "ledger", root / "missing-hooks", native_home / "sessions", "continue")
            index = NativeProcessIndex()
            index.start()
            queue.process_lookup = lambda target: index.lookup(target) or {"agent_kind": "unknown", "summary": "process refresh pending"}
            deadline = time.monotonic() + 90
            turn = None
            while time.monotonic() < deadline:
                drain()
                if process.poll() is not None:
                    raise RuntimeError("owned native fixture exited")
                turn = queue.current_turn(target)
                if turn and turn.get("kind") == "task_complete" and turn.get("error"):
                    break
            else:
                raise RuntimeError("native failed turn not observed")
            assert ERROR in turn["error"]["message"], turn
            birth = scope.birth(process.pid, codex=True)
            # Exercise the exact writer-lock path even if this native build
            # still has its original rollout open at this instant.
            idle_deadline = time.monotonic() + 3
            while True:
                idle = queue._idle_process_turn(target, process.pid)
                if idle.get("kind") != "unknown" or time.monotonic() >= idle_deadline:
                    break
                drain()
            assert idle.get("session_id") == turn["session_id"] and idle.get("turn_id") == turn["turn_id"], idle
            observer = core.WatchDaemon.__new__(core.WatchDaemon)
            observer.config = core.default_config()
            observer.codex_queue_recovery = queue
            observer._record_state = lambda *args: None
            runtime = core.TargetRuntime()
            state = core.ScreenState("recoverable_error", error_type="high_demand", message_kind="codex")
            assert observer._codex_turn_ready(target, runtime, state)
            runtime.codex_sent_turn_key = runtime.codex_observed_turn_key
            runtime.delivery_status = "accepted"
            assert not observer._codex_turn_ready(target, runtime, state), "same turn was retryable twice"
            os.write(master, "\x1b[200~任务请继续\x1b[201~".encode())
            drain(.1)
            os.write(master, b"\r")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                drain()
                current = queue.current_turn(target)
                if (current and current.get("kind") == "task_complete"
                        and current.get("turn_id") != turn["turn_id"] and not current.get("error")):
                    break
            else:
                raise RuntimeError("continued original native session did not complete")
            assert current["session_id"] == turn["session_id"] and scope.birth(process.pid, codex=True) == birth
            assert len(server.requests) == 2, server.requests
            result = {"result": "passed", "native_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                      "pid": process.pid, "birth": birth, "session_id": turn["session_id"],
                      "failed_turn": turn["turn_id"], "continued_turn": current["turn_id"],
                      "writer_lock_binding_verified": True, "duplicate_retry_blocked": True,
                      "local_requests": len(server.requests), "automatic_pause": guard.AUTOMATIC_POOL_STOP}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(result, ensure_ascii=False))
        except BaseException:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.with_suffix(".screen").write_bytes(raw_screen)
            print(json.dumps({"requests": server.requests, "screen_tail": raw_screen[-2000:].decode(errors="replace")}))
            raise
        finally:
            if 'index' in locals():
                index.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            os.close(master)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
