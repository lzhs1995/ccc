import contextlib
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import ResponsesProbe, classify, inventory, route, pinned_dependencies, validate_dependencies


def completion():
    response = {"id": "resp-test", "status": "completed", "output": [{"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "OK"}]}]}
    return ("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()


class ProbeClassifierTests(unittest.TestCase):
    def test_http_200_challenge_is_not_healthy(self):
        self.assertEqual(classify(200, "text/html", b"<html><script>challenge()</script></html>").kind, "blocked")
        self.assertEqual(classify(200, "application/json", b'{"success": true}', deep=True).kind, "contract")

    def test_light_validation_never_proves_generation(self):
        body = b'{"error":{"message":"invalid JSON"}}'
        self.assertEqual(classify(400, "application/json", body).kind, "accessible")
        self.assertEqual(classify(400, "application/json", body, deep=True).kind, "contract")

    def test_html_overload_and_structured_auth_are_not_ip_bans(self):
        for status, kind in ((401, "auth"), (429, "rate_limit"), (502, "upstream"), (503, "upstream")):
            with self.subTest(status=status):
                self.assertEqual(classify(status, "text/html", b"<html>Forbidden</html>").kind, kind)
        self.assertEqual(classify(403, "application/json", b'{"error":{"message":"model permission"}}').kind, "permission")

    def test_only_complete_model_sse_passes(self):
        self.assertEqual(classify(200, "text/event-stream", completion(), deep=True).kind, "healthy")
        for raw in (b"data: [DONE]\n\n", b'data: {"type":"response.created"}\n\n',
                    b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
                    completion().rstrip(), b"data: []\n\n"):
            self.assertEqual(classify(200, "text/event-stream", raw, deep=True).kind, "truncated")

    def test_sse_error_cannot_be_counted_as_native_success(self):
        raw = b'data: {"type":"error","code":"rate_limit_reached","message":"rate limit"}\n\n'
        self.assertEqual(classify(200, "text/event-stream", raw, deep=True).kind, "rate_limit")
        raw = completion() + b'data: {"type":"response.failed","response":{"error":{"message":"failed"}}}\n\n'
        self.assertEqual(classify(200, "text/event-stream", raw, deep=True).kind, "upstream")


class ProbeTransportTests(unittest.TestCase):
    def serve(self, response, *, slow=False, tunnel=False):
        captured = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self):
                self.send_response(200)
                self.end_headers()
                time.sleep(1)  # Never completes TLS.
            def do_POST(self):
                captured.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200 if not slow else 400)
                self.send_header("Content-Type", "text/event-stream" if not slow else "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    if slow:
                        for byte in response:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.03)
                    else:
                        self.wfile.write(response)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port, captured

    def probe(self, port, *, tls=False, timeout=.2):
        item = route("test", {"name": "test", "type": "http", "server": "127.0.0.1", "port": 1234})
        config = {"url": "https://example.invalid/v1/responses" if tls else "http://127.0.0.1/v1/responses",
                  "model": "test-model", "allow_unauthenticated_test": True,
                  "timeout_sec": timeout, "deep_timeout_sec": timeout}
        return item, ResponsesProbe(config, {item.id: port})

    def test_wire_request_finishes_sse_and_contains_no_real_session(self):
        port, captured = self.serve(completion())
        item, probe = self.probe(port, timeout=2)
        result = probe.run(item, deep=True)
        self.assertEqual(result.kind, "healthy")
        payload = json.loads(captured[0][2])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"], [])
        self.assertTrue(payload["stream"])
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(payload["prompt_cache_key"], captured[0][1]["session_id"])

    def test_slow_drip_body_has_total_deadline(self):
        port, _ = self.serve(b'{"error":{"message":"invalid JSON"}}', slow=True)
        item, probe = self.probe(port)
        start = time.monotonic()
        result = probe.run(item)
        self.assertEqual(result.kind, "timeout")
        self.assertLess(time.monotonic() - start, .55)

    def test_tls_handshake_has_total_deadline(self):
        port, _ = self.serve(b"", tunnel=True)
        item, probe = self.probe(port, tls=True)
        start = time.monotonic()
        self.assertEqual(probe.run(item).kind, "timeout")
        self.assertLess(time.monotonic() - start, .55)

    def test_unreadable_credentials_do_not_crash_or_probe(self):
        item, probe = self.probe(1)
        probe.config.update(auth_file="/nonexistent/ccc-test-auth.json", allow_unauthenticated_test=False)
        self.assertEqual(probe.run(item).kind, "auth")


class InventoryTests(unittest.TestCase):
    def test_management_requires_exact_global_dependency_pins(self):
        dependency = {"name": "AR-DEP/Tokyo/fixture", "type": "http", "server": "127.0.0.1", "port": 1}
        item = route("Tokyo", {"name": "exit", "type": "http", "server": "127.0.0.1", "port": 2,
                               "dialer-proxy": dependency["name"]}, (dependency,))
        with self.assertRaisesRegex(ValueError, "staged Clash"):
            validate_dependencies({"mode": "manage"}, [item])
        config = {"mode": "manage", "pinned_dependencies": pinned_dependencies([item])}
        validate_dependencies(config, [item])
        changed = route("Tokyo", item.proxies[-1], ({**dependency, "port": 3},))
        with self.assertRaisesRegex(ValueError, "staged Clash"):
            validate_dependencies(config, [changed])

    def test_deduplication_and_complete_tokyo_chain_excludes_unwanted_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nodes.json"
            path.write_text(json.dumps({"proxies": [
                {"name": "Tokyo", "type": "ss", "server": "tokyo.example", "port": 8444, "password": "test"},
                {"name": "us11", "type": "socks5", "server": "us11.example", "port": 11, "dialer-proxy": "old"},
                {"name": "us178", "type": "socks5", "server": "us178.example", "port": 178},
                {"name": "us78", "type": "socks5", "server": "us78.example", "port": 78},
            ]}))
            source = Path(temp) / "source.json"
            source.write_text(json.dumps({"proxies": [
                {"name": "one", "type": "ss", "server": "n.example", "port": 1, "password": "test"},
                {"name": "alias", "type": "ss", "server": "n.example", "port": 1, "password": "test"},
                {"name": "剩余套餐", "type": "ss", "server": "bad", "port": 2},
                {"name": "injected chain", "type": "ss", "server": "bad", "port": 2, "dialer-proxy": "DIRECT"},
            ]}))
            config = {"sources": [{"pool": "NTHU", "path": str(source)}],
                      "fallback": {"path": str(path), "pool": "Tokyo", "transit": "Tokyo", "exits": ["us11", "us178"]}}
            routes = inventory(config)
            self.assertEqual(len(routes), 3)
            self.assertEqual([r.label for r in routes], ["one", "us11", "us178"])
            for item in routes[1:]:
                self.assertEqual(item.proxies[-1]["dialer-proxy"], item.proxies[0]["name"])
                self.assertEqual(item.proxies[0]["server"], "tokyo.example")
            old = routes[1].id
            data = json.loads(path.read_text())
            data["proxies"][0]["password"] = "changed credential"
            path.write_text(json.dumps(data))
            self.assertNotEqual(inventory(config)[1].id, old)

    def test_shadow_supervisor_closes_only_its_child_on_parent_pipe_eof(self):
        if os.name != "posix":
            self.skipTest("POSIX supervisor")
        script = Path(__file__).resolve().parents[1] / "ccc_mihomo.py"
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "child.pid"
            read_fd, write_fd = os.pipe()
            child_code = "import os,time,pathlib; pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(20)"
            parent = subprocess.Popen([sys.executable, "-B", str(script), "supervise", str(read_fd), sys.executable, "-c", child_code], pass_fds=(read_fd,))
            os.close(read_fd)
            self.addCleanup(lambda: parent.kill() if parent.poll() is None else None)
            deadline = time.monotonic() + 3
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(pidfile.exists())
            child = int(pidfile.read_text())
            os.close(write_fd)  # Simulates guard exit, including SIGKILL.
            parent.wait(timeout=4)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertEqual(parent.returncode, 0)


if __name__ == "__main__":
    unittest.main()
