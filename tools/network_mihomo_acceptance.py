#!/usr/bin/env python3
"""Exercise provider updates, complete chains and hot reload on a NEW core.

All upstreams are loopback fixtures. No production socket, API key, profile,
TUN, Codex process, or cmux surface is used. The binary is never restarted.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import http.client
import http.server
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import Controller, atomic_json
from ccc_network_guard import ProviderServer
from ccc_mihomo import Route


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve(handler):
    server = Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def proxy_server(label, seen):
    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):
            host, port = self.path.rsplit(":", 1)
            seen.append((label, host, int(port)))
            with socket.create_connection((host, int(port)), timeout=3) as upstream:
                self.send_response(200)
                self.end_headers()
                channels = [self.connection, upstream]
                while True:
                    readable, _, _ = select.select(channels, [], [], 10)
                    if not readable:
                        return
                    for source in readable:
                        raw = source.recv(65536)
                        if not raw:
                            return
                        (upstream if source is self.connection else self.connection).sendall(raw)
        def log_message(self, *_):
            pass
    return serve(Proxy)


def run(binary):
    seen, servers = [], []
    finish_stream = threading.Event()
    sent_frames = []
    timings = {}
    started = time.monotonic()
    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                with contextlib.suppress(OSError):
                    for index in range(600):
                        data = f"{index}\n".encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                        sent_frames.append(index)
                        if finish_stream.is_set() and index >= 10:
                            break
                        time.sleep(.05)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")
        def log_message(self, *_):
            pass
    core = None
    publisher = None
    root = Path(tempfile.mkdtemp(prefix="ccc-network-accept-", dir="/tmp"))
    log = (root / "core.log").open("wb")
    try:
        upstream = serve(Upstream)
        before, after = proxy_server("before", seen), proxy_server("after", seen)
        servers.extend([upstream, before, after])
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            listener_port = reserve.getsockname()[1]
        publisher = ProviderServer(0, "fixture")
        provider_port = publisher.server.server_port
        def node(name, port, **extra):
            return {"name": name, "type": "http", "server": "127.0.0.1", "port": port, **extra}
        first = Route("first", "before", "fixture", "AR/Before", (node("AR/Before", before.server_port),))
        second = Route("second", "after", "fixture", "AR/After", (node("AR/After", after.server_port),))
        dependency = node("AR-DEP/Tokyo", before.server_port)
        chain = Route("chain", "chain", "Tokyo", "AR/Tokyo/us11", (
            dependency, node("AR/Tokyo/us11", after.server_port, **{"dialer-proxy": dependency["name"]})))
        publisher.set([first, second, chain])
        config = {
            "mode": "rule", "allow-lan": False, "ipv6": False, "interface-name": "lo0",
            "log-level": "warning", "external-controller-unix": str(root / "core.sock"),
            "tun": {"enable": False}, "dns": {"enable": False}, "profile": {"store-selected": True},
            # Provider children cannot resolve sibling dialer-proxy names.
            # Immutable dependencies must also exist in the global proxy map.
            "proxies": [{"name": "Offline", "type": "reject"}, dependency],
            "proxy-providers": {"Verified": {"type": "http", "url": f"http://127.0.0.1:{provider_port}/fixture/proxies",
                "path": "./verified.json", "proxy": "DIRECT", "interval": 86400, "health-check": {"enable": False}}},
            "proxy-groups": [{"name": "Transit-Auto-Select", "type": "select", "proxies": ["Offline"],
                              "use": ["Verified"], "filter": "^AR/"}],
            "listeners": [{"name": "fixture", "type": "http", "listen": "127.0.0.1", "port": listener_port,
                           "proxy": "Transit-Auto-Select"}],
            "rules": ["MATCH,Offline"],
        }
        path = root / "config.json"
        atomic_json(path, config)
        check = subprocess.run([str(binary), "-t", "-d", str(root), "-f", str(path)], capture_output=True, timeout=20)
        if check.returncode:
            raise RuntimeError(check.stdout.decode() + check.stderr.decode())
        core = subprocess.Popen([str(binary), "-d", str(root), "-f", str(path)], stdout=log, stderr=log)
        controller = Controller(root / "core.sock")
        deadline = time.monotonic() + 10
        while True:
            try:
                version = controller.get("/version")
                group = controller.get("/proxies/Transit-Auto-Select")
                if first.name in group.get("all", []):
                    break
            except (OSError, RuntimeError):
                pass
            if core.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("fixture core failed startup: " + (root / "core.log").read_text())
            time.sleep(.05)
        def once():
            connection = http.client.HTTPConnection("127.0.0.1", listener_port, timeout=5)
            try:
                connection.request("GET", f"http://127.0.0.1:{upstream.server_port}/once")
                response = connection.getresponse()
                assert response.status == 200 and response.read() == b"OK"
            finally:
                connection.close()
        controller.select("Transit-Auto-Select", first.name)
        stream = http.client.HTTPConnection("127.0.0.1", listener_port, timeout=8)
        stream.request("GET", f"http://127.0.0.1:{upstream.server_port}/stream")
        response = stream.getresponse()
        assert response.status == 200
        frames = [int(response.readline())]
        while len(sent_frames) < 10:
            time.sleep(.01)
        timings["stream_started"] = time.monotonic() - started
        originals = {c["id"] for c in controller.get("/connections")["connections"]}
        assert originals
        # An automatic provider refresh must retain the same current name.
        controller.refresh("Verified")
        assert controller.get("/proxies/Transit-Auto-Select")["now"] == first.name
        assert originals.issubset({c["id"] for c in controller.get("/connections")["connections"] or []})
        controller.select("Transit-Auto-Select", second.name)
        once()
        publisher.set([second, chain])
        controller.refresh("Verified")
        assert controller.get("/proxies/Transit-Auto-Select")["now"] == second.name
        assert first.name not in controller.get("/proxies/Transit-Auto-Select")["all"]
        frames.append(int(response.readline()))
        assert originals.issubset({c["id"] for c in controller.get("/connections")["connections"] or []})
        timings["provider_pruned"] = time.monotonic() - started
        # A legacy cached selection disappears on initial deployment. The
        # first provider entry must select the SAME verified physical path,
        # without briefly falling through to Offline or DIRECT.
        legacy = Route("legacy", "legacy", "fixture", "AR/Legacy-Selected",
                       (node("AR/Legacy-Selected", before.server_port),))
        replacement = Route("replacement", "replacement", "fixture", "AR/Before-New",
                            (node("AR/Before-New", before.server_port),))
        publisher.set([legacy, second, chain])
        controller.refresh("Verified")
        controller.select("Transit-Auto-Select", legacy.name)
        publisher.set([replacement, second, chain])
        # This is the only config reload in this tool; its Unix socket is
        # constructed from this private fixture directory, never user input.
        updated = copy.deepcopy(config)
        updated["rules"] = ["DOMAIN,unused.invalid,Offline", "MATCH,Offline"]
        # v1.19.31's proxySetProvider.Initial closes EVERY existing connection
        # with its provider name. A migration must use fresh provider names;
        # force=false by itself does not preserve provider-backed streams.
        updated["proxy-providers"]["VerifiedReloaded"] = updated["proxy-providers"].pop("Verified")
        updated["proxy-providers"]["VerifiedReloaded"]["path"] = "./verified-reloaded.json"
        updated["proxy-groups"][0]["use"] = ["VerifiedReloaded"]
        updated["proxy-groups"][0]["proxies"] = []
        atomic_json(path, updated)
        controller.request("PUT", "/configs?force=false", {"path": str(path)})
        assert core.poll() is None
        assert controller.get("/proxies/Transit-Auto-Select")["now"] == replacement.name
        timings["config_reloaded"] = time.monotonic() - started
        remaining = {c["id"] for c in (controller.get("/connections")["connections"] or [])}
        assert originals.issubset(remaining), f"hot reload discarded an active connection: {timings}, frames={len(sent_frames)}"
        checkpoint = len(seen)
        once()
        assert ("before", "127.0.0.1", upstream.server_port) in seen[checkpoint:]
        controller.select("Transit-Auto-Select", chain.name)
        checkpoint = len(seen)
        once()
        assert ("before", "127.0.0.1", after.server_port) in seen[checkpoint:]
        assert ("after", "127.0.0.1", upstream.server_port) in seen[checkpoint:]
        controller.select("Transit-Auto-Select", "AR/Offline")
        publisher.set([])
        controller.refresh("VerifiedReloaded")
        assert controller.get("/proxies/Transit-Auto-Select")["all"] == ["AR/Offline"]
        assert controller.get("/proxies/Transit-Auto-Select")["now"] == "AR/Offline"
        time.sleep(.2)
        assert originals.issubset({c["id"] for c in controller.get("/connections")["connections"] or []})
        finish_stream.set()
        while line := response.readline():
            frames.append(int(line))
        stream.close()
        assert frames == list(range(len(sent_frames))) and len(frames) >= 10, "stream truncated or lost frames during update"
        assert core.poll() is None
        return {"version": version, "pid": core.pid, "frames_preserved": len(frames),
                "provider_refresh_preserved_selection": True, "provider_prune_preserved_connection": True,
                "hot_reload_preserved_connection_ids": sorted(originals),
                "hot_reload_uses_new_provider_names": True,
                "missing_legacy_selection_keeps_same_physical_path": True,
                "empty_provider_has_only_explicit_offline": True,
                "provider_dependency_chain_verified": True, "production_touched": False, "timings": timings}
    finally:
        finish_stream.set()
        if core is not None and core.poll() is None:
            core.terminate()
            try:
                core.wait(timeout=5)
            except subprocess.TimeoutExpired:
                core.kill()
                core.wait(timeout=2)
        log.close()
        if publisher:
            publisher.close()
        for server in servers:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.binary)
    if args.output:
        atomic_json(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
