#!/usr/bin/env python3
"""Verify AnyTLS provider refresh/pruning with two live local HTTPS streams.

Only a newly created loopback Mihomo core is controlled. No production socket,
profile, TUN, API credential or model request is used. GC is explicitly forced
on the fixture after pruning so adapter finalizers are included in acceptance.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import http.server
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import Controller, Route, atomic_json
from ccc_network_guard import ProviderServer


def run(binary, expected_sha256=None):
    binary = Path(binary).resolve()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("fixture binary differs from the required production digest")
    root = Path(tempfile.mkdtemp(prefix="ccc-anytls-accept-", dir="/tmp"))
    core = publisher = upstream = None
    clients, readers, errors = [], [], []
    frames, sent, completed, end_received = {}, {}, set(), set()
    finish = threading.Event()
    once_calls = []
    checkpoints = []
    log = (root / "core.log").open("wb")
    started = time.monotonic()

    def wait_for(predicate, description, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors:
                raise AssertionError("old stream failed: " + repr(errors))
            if core is not None and core.poll() is not None:
                raise AssertionError("fixture core exited: " + (root / "core.log").read_text())
            if time.monotonic() >= deadline:
                raise AssertionError(description)
            time.sleep(.02)

    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path.startswith("/stream/"):
                name = self.path.rsplit("/", 1)[-1]
                sent[name] = []
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for index in range(2400):
                        data = f"{index}\n".encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                        sent[name].append(index)
                        if finish.is_set() and index >= 20:
                            break
                        time.sleep(.025)
                    self.wfile.write(b"4\r\nEND\n\r\n0\r\n\r\n")
                    self.wfile.flush()
                    completed.add(name)
                except OSError as exc:
                    errors.append(("upstream", name, type(exc).__name__))
            else:
                once_calls.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

        def log_message(self, *_):
            pass

    try:
        certificate, key = root / "certificate.pem", root / "key.pem"
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                        "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-keyout", str(key), "-out", str(certificate)],
                       check=True, capture_output=True, timeout=20)
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream.daemon_threads = True
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate, key)
        upstream.socket = server_context.wrap_socket(upstream.socket, server_side=True)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        with contextlib.ExitStack() as stack:
            sockets = [stack.enter_context(socket.socket()) for _ in range(3)]
            for reserved in sockets:
                reserved.bind(("127.0.0.1", 0))
            listener_port, before_port, after_port = [s.getsockname()[1] for s in sockets]
        publisher = ProviderServer(0, "fixture")
        provider_port = publisher.server.server_port

        def node(name, port):
            # Keep the real default session reuse behavior. Certificate bypass
            # applies only to these self-signed loopback fixture endpoints.
            proxy = {"name": name, "type": "anytls", "server": "127.0.0.1", "port": port,
                     "password": "local-fixture-password", "sni": "localhost", "skip-cert-verify": True}
            return Route(name, name, "fixture", name, (proxy,))

        first, second = node("AR/AnyTLS-Before", before_port), node("AR/AnyTLS-After", after_port)
        spare = node("AR/AnyTLS-Spare", after_port)
        publisher.set([first, second])
        listeners = [{"name": "client", "type": "http", "listen": "127.0.0.1", "port": listener_port,
                      "proxy": "Transit-Auto-Select"}]
        for name, port in (("peer-before", before_port), ("peer-after", after_port)):
            listeners.append({"name": name, "type": "anytls", "listen": "127.0.0.1", "port": port,
                              "users": {"fixture": "local-fixture-password"},
                              "certificate": str(certificate), "private-key": str(key), "proxy": "DIRECT"})
        config = {"mode": "rule", "allow-lan": False, "ipv6": False,
                  "interface-name": "lo0" if sys.platform == "darwin" else "lo", "log-level": "debug",
                  "external-controller-unix": str(root / "core.sock"),
                  "tun": {"enable": False}, "dns": {"enable": False}, "listeners": listeners,
                  "proxy-providers": {"Verified": {"type": "http",
                      "url": f"http://127.0.0.1:{provider_port}/fixture/proxies", "path": "./verified.json",
                      "proxy": "DIRECT", "interval": 86400, "health-check": {"enable": False}}},
                  "proxy-groups": [{"name": "Transit-Auto-Select", "type": "select",
                                    "use": ["Verified"], "filter": "^AR/"}],
                  "rules": ["IP-CIDR,127.0.0.0/8,DIRECT,no-resolve", "MATCH,REJECT"]}
        path = root / "config.json"
        atomic_json(path, config)
        check = subprocess.run([str(binary), "-t", "-d", str(root), "-f", str(path)],
                               capture_output=True, timeout=20)
        if check.returncode:
            raise AssertionError(check.stdout.decode() + check.stderr.decode())
        core = subprocess.Popen([str(binary), "-d", str(root), "-f", str(path)], stdout=log, stderr=log)
        controller = Controller(root / "core.sock")

        def ready():
            try:
                return first.name in controller.get("/proxies/Transit-Auto-Select").get("all", [])
            except (OSError, RuntimeError):
                return False

        wait_for(ready, "fixture did not become ready")
        version = controller.get("/version")
        original_configs = controller.get("/configs")
        controller.select("Transit-Auto-Select", first.name)
        client_context = ssl._create_unverified_context()

        def connect(path):
            client = http.client.HTTPSConnection("127.0.0.1", listener_port, timeout=8, context=client_context)
            client.set_tunnel("127.0.0.1", upstream.server_port)
            try:
                client.request("GET", path)
                return client, client.getresponse()
            except Exception:
                client.close()
                raise

        def once():
            client, response = connect("/once")
            try:
                assert response.status == 200 and response.read() == b"OK"
            finally:
                client.close()

        # Exercise sequential reuse before keeping two old HTTPS streams open.
        once()
        once()
        for name in ("one", "two"):
            client, response = connect("/stream/" + name)
            assert response.status == 200
            clients.append(client)
            frames[name] = []

            def read_stream(name=name, response=response):
                try:
                    while line := response.readline():
                        if line == b"END\n":
                            end_received.add(name)
                            continue
                        if name in end_received:
                            raise AssertionError("data received after stream end marker")
                        frames[name].append(int(line))
                except Exception as exc:
                    errors.append(("client", name, type(exc).__name__))

            reader = threading.Thread(target=read_stream, daemon=True)
            reader.start()
            readers.append(reader)
        wait_for(lambda: all(len(row) >= 10 for row in frames.values()), "old streams did not start")
        originals = {row["id"] for row in (controller.get("/connections").get("connections") or [])
                     if first.name in row.get("chains", [])}
        assert len(originals) == 2, "expected two old AnyTLS client connections"

        def checkpoint(label):
            assert core.poll() is None
            present = {row["id"] for row in (controller.get("/connections").get("connections") or [])}
            assert originals.issubset(present), "old AnyTLS connection removed at " + label
            # Demand frames produced AFTER this checkpoint (including GC),
            # not merely buffered data generated before the mutation.
            counts = {name: len(row) for name, row in sent.items()}
            wait_for(lambda: all(len(frames[name]) >= count + 5 for name, count in counts.items()),
                     "old stream stopped advancing at " + label)
            checkpoints.append({"phase": label, "elapsed_sec": round(time.monotonic() - started, 3),
                                "produced_before_checkpoint": counts,
                                "frames": {name: len(row) for name, row in frames.items()}})

        publisher.close()
        publisher = ProviderServer(provider_port, "fixture")
        try:
            controller.refresh("Verified")
        except RuntimeError:
            pass
        else:
            raise AssertionError("unreconciled publisher did not refuse refresh")
        assert controller.get("/proxies/Transit-Auto-Select")["now"] == first.name
        once()
        checkpoint("publisher_startup_503")

        # Change the payload so Mihomo actually reparses and replaces adapters.
        publisher.set([first, second, spare])
        controller.refresh("Verified")
        group = controller.get("/proxies/Transit-Auto-Select")
        assert group["now"] == first.name and spare.name in group["all"]
        checkpoint("changed_provider_refreshed")
        controller.select("Transit-Auto-Select", second.name)
        once()
        publisher.set([second])
        controller.refresh("Verified")
        group = controller.get("/proxies/Transit-Auto-Select")
        assert group["now"] == second.name and first.name not in group["all"]
        for _ in range(2):
            controller.request("PUT", "/debug/gc")
            time.sleep(.1)
        checkpoint("old_anytls_pruned_then_two_gc_cycles")

        controller.select("Transit-Auto-Select", "AR/Offline")
        publisher.set([])
        controller.refresh("Verified")
        group = controller.get("/proxies/Transit-Auto-Select")
        assert group["now"] == "AR/Offline" and group["all"] == ["AR/Offline"]
        count_before = len(once_calls)
        denied = False
        try:
            client, response = connect("/must-not-arrive")
            try:
                denied = response.status != 200
            finally:
                client.close()
        except (OSError, http.client.HTTPException):
            denied = True
        assert denied and len(once_calls) == count_before, "empty provider allowed a new request"
        for _ in range(2):
            controller.request("PUT", "/debug/gc")
            time.sleep(.1)
        checkpoint("offline_rejects_new_calls_but_old_streams_continue")
        assert controller.get("/configs") == original_configs
        finish.set()
        for reader in readers:
            reader.join(timeout=8)
            assert not reader.is_alive(), "old stream did not finish"
        assert not errors, errors
        assert completed == end_received == {"one", "two"}
        for name in completed:
            assert frames[name] == sent[name] == list(range(len(sent[name])))
            assert len(frames[name]) >= 30
        return {"binary_sha256": digest, "version": version, "fixture": str(root), "core_pid": core.pid,
                "protocol": "anytls", "session_reuse": "default", "https_streams": 2,
                "frames_preserved": {name: len(row) for name, row in frames.items()},
                "connection_ids_preserved": sorted(originals), "forced_gc_cycles": 4,
                "stream_end_markers_received": sorted(end_received),
                "publisher_startup_503_preserved_streams": True, "changed_provider_preserved_streams": True,
                "pruned_adapter_gc_preserved_streams": True, "offline_rejected_new_request": True,
                "core_config_unchanged": True, "core_pid_unchanged": True,
                "production_touched": False, "checkpoints": checkpoints}
    finally:
        finish.set()
        for client in clients:
            client.close()
        if core is not None and core.poll() is None:
            core.terminate()
            try:
                core.wait(timeout=5)
            except subprocess.TimeoutExpired:
                core.kill()
                core.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=1)
        if publisher:
            publisher.close()
        if upstream:
            upstream.shutdown()
            upstream.server_close()
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.binary, args.expected_sha256)
    if args.output:
        atomic_json(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
