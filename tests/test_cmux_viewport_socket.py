"""Exercise real local socket framing without connecting to the user's cmux."""
import contextlib
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import FakeClient, armed_daemon, grid_payload


@contextlib.contextmanager
def server(handler):
    with tempfile.TemporaryDirectory(prefix="ccc-socket-", dir="/tmp") as directory:
        path = str(Path(directory) / "rpc.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(8)
        listener.settimeout(0.05)
        stopped = threading.Event()
        requests, errors = [], []
        with ThreadPoolExecutor(4) as pool:
            def handle(connection):
                try:
                    with connection:
                        connection.settimeout(2)
                        data = bytearray()
                        while b"\n" not in data:
                            chunk = connection.recv(65536)
                            if not chunk:
                                return
                            data.extend(chunk)
                        request = json.loads(data)
                        requests.append(request)
                        handler(connection, request)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Timeout tests close their clients before releasing us.
                except Exception as exc:
                    errors.append(exc)

            def accept():
                while not stopped.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    pool.submit(handle, connection)

            worker = threading.Thread(target=accept, daemon=True)
            worker.start()
            transport = core.CmuxViewportSocket()
            transport.configure({"protocol": "cmux-socket", "version": 2, "socket_path": path})
            try:
                yield transport, requests
            finally:
                stopped.set()
                listener.close()
                worker.join(2)
        if errors:
            raise errors[0]


def response(request, **extra):
    result = {key: request["params"][key] for key in ("workspace_id", "surface_id")}
    result.update(text="正常画面\n›", **extra)
    return {"id": request["id"], "ok": True, "result": result}


def send(connection, payload):
    connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())


class ViewportSocketTests(unittest.TestCase):
    def test_fragmented_unicode_read_and_replay_use_only_explicit_viewport_identity(self):
        def handle(connection, request):
            payload = response(request)
            if request["method"] == "terminal.replay":
                payload["result"]["render_grid"] = grid_payload([])["render_grid"]
            encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
            for start in range(0, len(encoded), 23):
                connection.sendall(encoded[start:start + 23])
        with server(handle) as (transport, requests):
            runner = mock.Mock(side_effect=AssertionError("unexpected CLI call"))
            client = core.CmuxClient(runner=runner, viewport_socket=transport)
            self.assertEqual(client.read_screen("workspace-uuid", "surface-uuid"), "正常画面\n›")
            self.assertEqual(client.last_viewport_source, "surface.read_text")
            self.assertIn("render_grid", client.replay("workspace-uuid", "surface-uuid"))
            self.assertEqual(requests[0]["params"], {"workspace_id": "workspace-uuid",
                                                    "surface_id": "surface-uuid", "scrollback": False})
            self.assertEqual(requests[1]["params"]["anchor"], "viewport")
            self.assertNotEqual(requests[0]["id"], requests[1]["id"])

    def test_slow_surface_does_not_hold_other_socket_readers(self):
        entered, release = threading.Event(), threading.Event()
        def handle(connection, request):
            if request["params"]["surface_id"] == "slow":
                entered.set()
                release.wait(3)
            send(connection, response(request))
        with server(handle) as (transport, requests), ThreadPoolExecutor(2) as pool:
            client = core.CmuxClient(viewport_socket=transport)
            slow = pool.submit(client.read_screen, "workspace", "slow")
            try:
                self.assertTrue(entered.wait(1))
                fast = pool.submit(client.read_screen, "workspace", "fast")
                self.assertEqual(fast.result(1), "正常画面\n›")
                self.assertFalse(slow.done())
            finally:
                release.set()
            slow.result(2)
            self.assertEqual(len(requests), 2)

    def test_unusable_protocol_falls_back_to_cli_with_shared_probe_backoff(self):
        for bad in ("id", "auth", "eof", "json", "oversized", "identity_missing", "text_missing"):
            with self.subTest(bad=bad):
                def handle(connection, request):
                    payload = response(request)
                    if bad == "eof":
                        return
                    if bad == "json":
                        connection.sendall(b"not-json\n")
                        return
                    if bad == "oversized":
                        connection.sendall(b"x" * 2048)
                        return
                    if bad == "id":
                        payload["id"] = "wrong-response"
                    elif bad == "auth":
                        payload.update(ok=False, error={"code": "unauthorized"})
                    elif bad == "identity_missing":
                        payload["result"].pop("surface_id")
                    elif bad == "text_missing":
                        payload["result"].pop("text")
                    send(connection, payload)
                with server(handle) as (transport, requests):
                    transport.MAX_RESPONSE_BYTES = 1024
                    runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "CLI viewport", ""))
                    client = core.CmuxClient(runner=runner, viewport_socket=transport)
                    self.assertEqual(client.read_screen("workspace", "surface"), "CLI viewport")
                    self.assertEqual(client.read_screen("workspace", "surface"), "CLI viewport")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(runner.call_count, 2)
                    self.assertEqual(runner.call_args.args[0][1:],
                                     ["read-screen", "--workspace", "workspace", "--surface", "surface"])

    def test_wrong_surface_or_workspace_is_rejected_without_using_its_text(self):
        for key in ("surface_id", "workspace_id"):
            with self.subTest(key=key):
                def handle(connection, request):
                    payload = response(request)
                    payload["result"][key] = "another-target"
                    send(connection, payload)
                with server(handle) as (transport, _):
                    runner = mock.Mock()
                    client = core.CmuxClient(runner=runner, viewport_socket=transport)
                    with self.assertRaisesRegex(core.IncompatibleError, "identity mismatch"):
                        client.read_screen("workspace", "surface")
                    runner.assert_not_called()

    def test_timeout_is_not_followed_by_another_cli_read(self):
        release = threading.Event()
        def handle(connection, request):
            release.wait(2)
            send(connection, response(request))
        with server(handle) as (transport, _):
            request = transport.request
            runner = mock.Mock()
            client = core.CmuxClient(runner=runner, viewport_socket=transport)
            try:
                with mock.patch.object(transport, "request", side_effect=lambda method, params, **kw:
                                       request(method, params, timeout=0.05)):
                    with self.assertRaises(core.CmuxError) as caught:
                        client.read_screen("workspace", "surface")
                self.assertIsInstance(caught.exception.__cause__, TimeoutError)
                runner.assert_not_called()
            finally:
                release.set()

    def test_input_and_history_are_never_allowed_on_the_read_transport(self):
        transport = core.CmuxViewportSocket()
        target = {"workspace_id": "workspace", "surface_id": "surface"}
        for method, params in (("surface.send_text", {**target, "text": "continue"}),
                               ("surface.read_text", {**target, "scrollback": True}),
                               ("terminal.replay", {**target, "anchor": "history"})):
            with self.subTest(method=method), self.assertRaises(ValueError):
                transport.request(method, params, timeout=1)
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        client = core.CmuxClient(runner=runner, viewport_socket=transport)
        with mock.patch.object(transport, "request", side_effect=AssertionError("input on read transport")):
            client.send("workspace", "surface", "任务请继续")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0][-1], "任务请继续\n")

    def test_changing_configured_binary_invalidates_the_discovered_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(grid_payload([])))
            self.addCleanup(daemon._process_snapshots.close)
            for external in (True, False):
                daemon._viewport_socket.configure({"protocol": "cmux-socket", "version": 2,
                                                    "socket_path": "/tmp/discovered.sock"})
                mutate = daemon.config_store.mutate if external else daemon._mutate_config
                mutate(lambda c: c.update(cmux_path="/tmp/other-cmux" if external else "/tmp/third-cmux"))
                daemon._reload_config_if_changed()
                self.assertIsNone(daemon._viewport_socket.path)


if __name__ == "__main__":
    unittest.main()
