#!/usr/bin/env python3
"""Real Codex + private RPC relay + loopback-only streaming cancellation test.

Uses an isolated Codex home and CCC config. No production workspace, provider,
credential or task is used. Every backend is an owned child and is reaped.
"""
import argparse
import asyncio
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import cmux_codex_watch as core
from ccc_guard_transport import WebSocket


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, count, response="text", *, complete=False):
        super().__init__(("127.0.0.1", 0), Handler)
        self.count = count
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.arrived, self.disconnected, self.first_response = {}, {}, None
        self.requests = []
        self.response = response
        self.complete = complete


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
        with self.server.lock:
            try:
                number = int(self.path.split("/")[2])
            except (ValueError, IndexError):
                number = len(self.server.arrived)
            self.server.requests.append({"surface": number, "at": time.monotonic(), "model": body.get("model"),
                "session_header": self.headers.get("session-id"), "thread_header": self.headers.get("thread-id"),
                "path": self.path, "responses_lite": self.headers.get("x-openai-internal-codex-responses-lite"),
                "input_text": [[c.get("text") for c in x.get("content", []) if isinstance(c, dict)]
                               for x in body.get("input", []) if isinstance(x, dict) and x.get("role") == "user"],
                "header_keys": list(self.headers),
                "input_roles": [x.get("role", x.get("type")) for x in body.get("input", []) if isinstance(x, dict)]})
            self.server.arrived[number] = time.monotonic()
            if len(self.server.arrived) == self.server.count:
                self.server.ready.set()
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
            deadline = time.monotonic() + 600
            while not self.server.ready.is_set() and time.monotonic() < deadline:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(.02)
            if number == 0 and self.server.ready.is_set():
                mid = "msg_" + uuid.uuid4().hex
                if self.server.response == "tool":
                    item = {"id": mid, "type": "function_call", "name": "exec_command", "call_id": "call_" + mid,
                            "arguments": '{"cmd":"true"}', "status": "completed"}
                    self.server.first_response = time.monotonic()
                    event({"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}})
                    event({"type": "response.output_item.done", "output_index": 0, "item": item})
                    event({"type": "response.completed", "response": {**response, "status": "completed", "output": [item]}})
                else:
                    item = {"id": mid, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
                    event({"type": "response.output_item.added", "output_index": 0, "item": item})
                    event({"type": "response.content_part.added", "item_id": mid, "output_index": 0, "content_index": 0,
                           "part": {"type": "output_text", "text": "", "annotations": []}})
                    self.server.first_response = time.monotonic()
                    event({"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": "OK"})
                    if self.server.complete:
                        item.update(status="completed", content=[{"type": "output_text", "text": "OK", "annotations": []}])
                        event({"type": "response.output_item.done", "output_index": 0, "item": item})
                        event({"type": "response.completed", "response": {**response, "status": "completed", "output": [item]}})
            while time.monotonic() < deadline:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(.01)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.server.disconnected[number] = time.monotonic()


async def rpc(ws, method, params, identifier):
    ws.write(json.dumps({"id": identifier, "method": method, "params": params}))
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 45))
        if message.get("id") == identifier:
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message["result"]


async def run(count, output, *, rearm=False, response="text"):
    output.mkdir(parents=True, exist_ok=True)
    server = MockServer(count, response)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="ccc-guard-native-") as temp:
        root = Path(temp)
        test_codex_home = root / "codex"
        test_codex_home.mkdir()
        (test_codex_home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_provider = "guard_mock"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\n'
            '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
            '[model_providers.guard_mock]\nname = "Local guard acceptance"\nwire_api = "responses"\n'
            'base_url = "http://127.0.0.1:1/v1"\nrequires_openai_auth = false\nsupports_websockets = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\n')
        config_path = root / "ccc" / "config.json"
        wid, jid = str(uuid.uuid4()).upper(), str(uuid.uuid4())
        config = core.default_config()
        config.update(mode="armed", global_paused=False,
            workspace_rules=[{"workspace_id": wid, "enabled": True,
                              "batch_guard": {"version": 1, "origin_job_id": jid}}])
        core.atomic_write_json(config_path, config)
        core.atomic_write_json(config_path.parent / "workspace-batches" / jid / "job.json",
            {"id": jid, "workspace_id": wid, "slots": [{"index": i} for i in range(count)]})
        async def membership(w, s):
            return w == wid and s in owned
        service = guard.GuardService(config_path, membership=membership)
        guard.private_directory(service.sockets)
        owned = set()
        clients = []
        sessions = []
        try:
            await service.dispatch({"command": "arm", "workspace_id": wid})
            for number in range(count):
                sid = str(uuid.uuid4()).upper()
                owned.add(sid)
                env = dict(os.environ, CODEX_HOME=str(test_codex_home), NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_SQLITE_HOME"):
                    env.pop(name, None)
                result = await service.dispatch({"command": "register", "workspace_id": wid, "surface_id": sid,
                    "cwd": str(root), "environment": env, "frontend_pid": os.getpid(),
                    "config_args": ["-c", "sqlite_home=" + json.dumps(str(root / f"sqlite-{number}")),
                        "-c", "model_providers.guard_mock.base_url=" + json.dumps(f"http://127.0.0.1:{server.server_port}/v1/{number}")]})
                ws = await WebSocket.connect(result["endpoint"])
                clients.append(ws)
                await rpc(ws, "initialize", {"clientInfo": {"name": "ccc_guard_acceptance", "version": "1"},
                                           "capabilities": {"experimentalApi": True}}, 1)
                ws.write(json.dumps({"method": "initialized"}))
                started = await rpc(ws, "thread/start", {"cwd": str(root), "modelProvider": "guard_mock",
                    "baseInstructions": "Reply OK. Do not use tools.", "approvalPolicy": "never", "sandbox": "read-only"}, 2)
                session_id = started["thread"]["id"]
                sessions.append(session_id)
                await rpc(ws, "turn/start", {"threadId": session_id, "input": [{"type": "text", "text": "OK", "text_elements": []}]}, 3)
                if number % 5 == 0 or number + 1 == count:
                    print(json.dumps({"started": number + 1, "expected": count, "upstream_arrived": len(server.arrived)}), flush=True)
            pool = service.pools[wid]
            deadline = time.monotonic() + 90
            while (pool.phase not in {"stopped", "failed"} or len(server.disconnected) < count) and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            pool.save()
            after_stop = len(server.requests)
            await asyncio.sleep(1)
            result = {"count": count, "phase": pool.phase, "trip": pool.trip,
                      "surfaces": {sid: e.summary() for sid, e in pool.endpoints.items()},
                      "first_upstream_content": server.first_response,
                      "upstream_disconnect_ms": {str(i): round((at - server.first_response) * 1000, 3)
                                                 for i, at in server.disconnected.items()} if server.first_response else {},
                      "requests": server.requests, "no_late_requests": after_stop == len(server.requests)}
            (output / "native-result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps({k: result[k] for k in ("count", "phase", "trip", "upstream_disconnect_ms", "no_late_requests")}), flush=True)
            assert pool.phase == "stopped" and pool.trip["connected"], result
            assert pool.trip["within_deadline"], result
            assert len(server.disconnected) == count and max(result["upstream_disconnect_ms"].values()) <= 1000, result
            assert len(server.requests) == count and result["no_late_requests"], result
            if rearm:
                if pool.pause_task:
                    await pool.pause_task
                core.ConfigStore(config_path).mutate(lambda c: c["workspace_rules"][0].update(paused=False))
                await service.dispatch({"command": "arm", "workspace_id": wid, "resume": True})
                assert [e.session_id for e in pool.endpoints.values()] == sessions
                await asyncio.sleep(.15)
                assert len(server.requests) == count, "restoring a session must not replay its prompt"
                server.arrived.clear()
                server.disconnected.clear()
                server.ready.clear()
                server.first_response = None
                for number in reversed(range(count)):
                    await rpc(clients[number], "turn/start", {"threadId": sessions[number],
                        "input": [{"type": "text", "text": "OK again", "text_elements": []}]}, 20)
                deadline = time.monotonic() + 30
                while pool.phase not in {"stopped", "failed"} and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                assert pool.phase == "stopped" and pool.trip["connected"] and pool.trip["within_deadline"], pool.trip
                assert len(server.requests) == count * 2
                result["rearm"] = {"same_sessions": True, "no_prompt_replay": True, "trip": pool.trip}
                (output / "native-result.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result["rearm"]), flush=True)
        except BaseException as exc:
            import shutil
            details = {"error": repr(exc), "arrived": server.arrived, "requests": server.requests,
                "pools": {wid: {"phase": p.phase, "trip": p.trip, "surfaces": {sid: e.summary() for sid, e in p.endpoints.items()}}
                          for wid, p in service.pools.items()}}
            (output / "native-failure.json").write_text(json.dumps(details, indent=2))
            for log in config_path.parent.glob("batch-guards/*/*.backend.log"):
                shutil.copyfile(log, output / log.name)
            print(json.dumps({"failure": repr(exc), "started": len(sessions), "arrived": len(server.arrived)}), flush=True)
            raise
        finally:
            for pool in service.pools.values():
                for endpoint in pool.endpoints.values():
                    if endpoint.native and endpoint.native.returncode is None:
                        endpoint.native.kill()
                        await endpoint.native.wait()
                    if endpoint.socket_server:
                        endpoint.socket_server.close()
                    endpoint.socket_path.unlink(missing_ok=True)
            for ws in clients:
                ws.close()
            if service.save_task:
                await service.save_task
            server.shutdown()
            server.server_close()
            service.sockets.rmdir()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surfaces", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rearm", action="store_true")
    parser.add_argument("--response", choices=["text", "tool"], default="text")
    args = parser.parse_args()
    asyncio.run(run(args.surfaces, args.output, rearm=args.rearm, response=args.response))
