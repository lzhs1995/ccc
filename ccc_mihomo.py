"""Optional Mihomo adapter. All probe traffic uses a separate, non-TUN core.

Python dependencies remain standard-library only. YAML subscriptions are read
with macOS's Ruby/Psych safe loader; JSON inputs need no external parser.
This module never reloads or stops the production core.
"""
from __future__ import annotations

import dataclasses
import hashlib
import http.client
import json
import os
import re
import select
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zlib
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def document(path):
    raw = Path(path).read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("subscription exceeds 16 MiB")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        command = ["/usr/bin/ruby", "-rjson", "-ryaml", "-e",
                   "puts JSON.generate(YAML.safe_load(STDIN.read, permitted_classes: [], permitted_symbols: [], aliases: true))"]
        result = subprocess.run(command, input=raw, capture_output=True, timeout=5)
        if result.returncode:
            raise ValueError("cannot safely parse subscription YAML")
        value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value


@dataclasses.dataclass(frozen=True)
class Route:
    id: str
    label: str
    pool: str
    name: str
    proxies: tuple[dict, ...]
    priority: int = 0

    def record(self):
        return dataclasses.asdict(self)

    @classmethod
    def restore(cls, value):
        value = dict(value)
        value["proxies"] = tuple(value["proxies"])
        return cls(**value)


def route(pool, proxy, dependencies=(), priority=0):
    # Identity includes credentials and the entire chain, but is never a secret
    # disclosure: only the digest, display name and health reach status output.
    proxy = dict(proxy)
    label = str(proxy.pop("name"))
    payload = {"pool": pool, "proxy": proxy, "dependencies": dependencies}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    name = f"AR/{pool}/{label} [{identity[:8]}]"
    proxy["name"] = name
    return Route(identity, label, pool, name, (*dependencies, proxy), priority)


def dependency_fingerprint(proxy):
    return hashlib.sha256(json.dumps(proxy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pinned_dependencies(routes):
    return {p["name"]: dependency_fingerprint(p) for r in routes for p in r.proxies[:-1]}


def validate_dependencies(config, routes):
    # Mihomo resolves dialer-proxy only in its GLOBAL proxy map, not within
    # providers. Deployment pins these exact immutable dependencies there.
    # Subscription changes cannot silently replace a complete verified chain.
    pins = config.get("pinned_dependencies")
    if pins is None and config.get("mode") != "manage":
        return
    for name, digest in pinned_dependencies(routes).items():
        if not isinstance(pins, dict) or pins.get(name) != digest:
            raise ValueError("fallback dependency requires a staged Clash profile update: " + name)


def inventory(config):
    found, seen = [], set()
    for source in config.get("sources", []):
        excluded = re.compile(source.get("exclude", r"剩余|重置|到期|套餐|官網|官网"), re.I)
        for node in document(source["path"]).get("proxies", []):
            if not isinstance(node, dict) or not node.get("name") or excluded.search(str(node["name"])):
                continue
            if node.get("type") in {"direct", "reject", "reject-drop"} or node.get("dialer-proxy"):
                continue  # A subscription cannot inject an unverified extra hop.
            item = route(source["pool"], node, priority=len(found))
            if item.id not in seen:
                found.append(item)
                seen.add(item.id)
    fallback = config.get("fallback")
    if fallback:
        nodes = {x["name"]: x for x in document(fallback["path"]).get("proxies", [])}
        transit = dict(nodes[fallback["transit"]])
        if transit.get("dialer-proxy"):
            raise ValueError("fallback transit must not depend on another group")
        digest = hashlib.sha256(json.dumps(transit, sort_keys=True).encode()).hexdigest()[:12]
        transit["name"] = f"AR-DEP/{fallback['pool']}/{digest}"
        for priority, source_name in enumerate(fallback["exits"]):
            node = dict(nodes[source_name])
            node["dialer-proxy"] = transit["name"]
            found.append(route(fallback["pool"], node, (transit,), priority))
    names = [x.name for x in found]
    if len(names) != len(set(names)) or len(found) > 512:
        raise ValueError("duplicate route names or excessive inventory")
    validate_dependencies(config, found)
    return found


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Controller:
    def __init__(self, path, secret="", timeout=3):
        self.path, self.secret, self.timeout = str(path), secret, timeout

    def request(self, method, path, value=None):
        connection = UnixHTTPConnection(self.path, self.timeout)
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        try:
            connection.request(method, path, None if value is None else json.dumps(value), headers)
            response = connection.getresponse()
            data = response.read(4 * 1024 * 1024)
            if response.status >= 400:
                raise RuntimeError(f"Mihomo {method} {path}: HTTP {response.status}")
            return json.loads(data) if data else {}
        finally:
            connection.close()

    def get(self, path):
        return self.request("GET", path)

    def select(self, group, name):
        return self.request("PUT", "/proxies/" + urllib.parse.quote(group, safe=""), {"name": name})

    def refresh(self, provider):
        return self.request("PUT", "/providers/proxies/" + urllib.parse.quote(provider, safe=""))


class ShadowCore:
    def __init__(self, root, binary, interface):
        self.root, self.binary, self.interface = Path(root), str(binary), interface
        digest = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        self.socket_root = Path("/tmp") / f"ccc-net-{os.getuid()}-{digest}"
        self.socket_root.mkdir(mode=0o700, exist_ok=True)
        st = self.socket_root.lstat()
        if self.socket_root.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("shadow socket directory is not private")
        self.socket_path = self.socket_root / "probe.sock"
        self.controller = Controller(self.socket_path)
        self.process = None
        self.ports = {}
        self.log = None
        self.lifeline = None

    def configure(self, routes):
        if not self.interface:
            raise ValueError("an explicit physical interface is required for isolated probes")
        proxies, seen = [], set()
        for item in routes:
            for proxy in item.proxies:
                if proxy["name"] not in seen:
                    proxies.append(proxy)
                    seen.add(proxy["name"])
        allocated = []
        ports = {}
        try:
            for item in routes:
                reservation = socket.socket()
                reservation.bind(("127.0.0.1", 0))
                allocated.append(reservation)
                ports[item.id] = reservation.getsockname()[1]
            config = {
                "mode": "rule", "ipv6": False, "allow-lan": False,
                "log-level": "warning", "interface-name": self.interface,
                "external-controller-unix": str(self.socket_path),
                "tun": {"enable": False}, "profile": {"store-selected": False},
                "dns": {"enable": True, "ipv6": False, "enhanced-mode": "redir-host",
                        "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                        "nameserver": ["https://dns.alidns.com/dns-query", "https://doh.pub/dns-query"],
                        "proxy-server-nameserver": ["https://dns.alidns.com/dns-query", "https://doh.pub/dns-query"]},
                "proxies": proxies,
                "listeners": [{"name": item.id, "type": "http", "listen": "127.0.0.1",
                               "port": ports[item.id], "proxy": item.name} for item in routes],
                "rules": ["MATCH,DIRECT"],
            }
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.root / "config.json"
            atomic_json(path, config)
            check = subprocess.run([self.binary, "-t", "-d", str(self.root), "-f", str(path)],
                                   capture_output=True, timeout=15)
            if check.returncode:
                raise RuntimeError("isolated Mihomo configuration validation failed")
        finally:
            for reservation in allocated:
                reservation.close()
        if self.process is not None and self.process.poll() is None:
            self.controller.request("PUT", "/configs?force=false", {"path": str(path)})
        else:
            self.log = os.fdopen(os.open(self.root / "mihomo.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
            read_fd, self.lifeline = os.pipe()
            try:
                # The supervisor receives EOF even if the guard is SIGKILLed.
                # It only ever terminates the child it created, never a PID
                # guessed from a process name or a stale on-disk record.
                self.process = subprocess.Popen(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "supervise",
                     str(read_fd), self.binary, "-d", str(self.root), "-f", str(path)],
                    pass_fds=(read_fd,), stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                os.close(read_fd)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("isolated Mihomo exited at startup")
                try:
                    self.controller.get("/version")
                    break
                except (OSError, RuntimeError, ValueError):
                    time.sleep(.05)
            else:
                raise RuntimeError("isolated Mihomo controller did not become ready")
        self.ports = ports

    def close(self):
        # Only the Popen-owned probe core can be stopped. No process-name kill.
        if self.lifeline is not None:
            os.close(self.lifeline)
            self.lifeline = None
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log is not None:
            self.log.close()


def supervise_core(fd, command):
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    child = subprocess.Popen(command, close_fds=True)
    try:
        while child.poll() is None and not stopped.is_set():
            readable, _, _ = select.select([fd], [], [], .1)
            if readable and not os.read(fd, 1):
                break
    finally:
        os.close(fd)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)


@dataclasses.dataclass
class ProbeResult:
    kind: str
    status: int = 0
    elapsed_ms: float = 0
    detail: str = ""
    deep: bool = False


def sse_events(text):
    parts = []
    for line in text.splitlines():
        if line.startswith("data:"):
            parts.append(line[5:].lstrip(" "))
        elif not line and parts:
            try:
                event = json.loads("\n".join(parts))
                if isinstance(event, dict):
                    yield event
            except ValueError:
                pass
            parts = []


def _completed(response):
    if not isinstance(response, dict) or response.get("status") != "completed" or not response.get("id"):
        return False
    output = response.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "output_text"
                                             and isinstance(c.get("text"), str) and c["text"].strip() for c in content):
            return True
    return False


def classify(status, content_type, body, *, deep=False):
    text = body.decode("utf-8", "replace")
    lower = text.lower()
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    # Overload/error pages are not evidence that an egress IP was banned.
    if status == 429:
        return ProbeResult("rate_limit", status, detail="upstream rate limit", deep=deep)
    if status >= 500:
        return ProbeResult("upstream", status, detail="upstream failure", deep=deep)
    if status == 401:
        return ProbeResult("auth", status, detail="credential or account rejected", deep=deep)
    if isinstance(value, dict):
        error = value.get("error")
        message = json.dumps(error if error is not None else value, ensure_ascii=False).lower()
        if any(x in message for x in ("invalid_api_key", "invalid api key", "insufficient_quota", "余额不足", "令牌额度")):
            return ProbeResult("auth", status, detail="credential or account rejected", deep=deep)
        if "rate_limit" in message or "rate limit" in message:
            return ProbeResult("rate_limit", status, detail="upstream rate limit", deep=deep)
        if status == 403:
            return ProbeResult("permission", status, detail="structured API permission response", deep=deep)
        # Invalid JSON cannot start a generation. This proves only that the
        # Responses validator is reachable, never that a model completed.
        if not deep and status in (400, 422) and error is not None and any(
                x in message for x in ("json", "parse", "invalid", "decode", "syntax", "请求", "解析")):
            return ProbeResult("accessible", status, detail="Responses JSON validation reached", deep=deep)
        return ProbeResult("contract", status, detail="unexpected API probe response", deep=deep)
    if deep and status == 200 and "text/event-stream" in content_type.lower():
        completed = False
        for event in sse_events(text):
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            if event.get("type") == "response.completed":
                completed = _completed(response)
            elif event.get("type") in {"response.failed", "response.incomplete", "error"}:
                error = event.get("error") or response.get("error") or event
                classified = classify(400, "application/json", json.dumps({"error": error}).encode(), deep=True)
                if classified.kind in {"auth", "permission", "rate_limit"}:
                    classified.status = status
                    return classified
                return ProbeResult("upstream", status, detail="Responses stream ended unsuccessfully", deep=True)
        return ProbeResult("healthy" if completed else "truncated", status,
                           detail="Responses completed" if completed else "missing complete model response", deep=True)
    if any(x in lower for x in ("<html", "<!doctype html", "<script", "cf-chl-", "captcha", "forbidden")):
        return ProbeResult("blocked", status, detail="HTML/WAF response on Responses endpoint", deep=deep)
    return ProbeResult("contract", status, detail="unrecognized probe response", deep=deep)


class _DeadlineConnection(http.client.HTTPConnection):
    """Hard wall clock limit, including CONNECT, TLS and slow-drip bodies."""
    transport = None

    def connect(self):
        super().connect()
        self.transport = self.sock

    def expire(self):
        self.expired.set()
        sock = self.sock or self.transport
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("probe deadline")
        return remaining


class _DeadlineTLSConnection(_DeadlineConnection):
    def connect(self):
        super().connect()
        self.sock.settimeout(self.remaining())
        # Assign before handshake so the deadline can interrupt it too.
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self._tunnel_host,
                                             do_handshake_on_connect=False)
        self.transport = self.sock
        self.sock.do_handshake()
        self.sock.settimeout(self.remaining())


class ResponsesProbe:
    def __init__(self, config, ports):
        self.config, self.ports = config, ports

    def request_body(self):
        # Codex's wire contract, without starting a Codex session or writing
        # native completion events. Some Codex backends reject max_output_tokens.
        body = {
            "model": self.config["model"],
            "instructions": "You are Codex, a coding agent running in the Codex CLI. This is a connectivity check. Reply only OK and do not call tools.",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Reply with OK."}]}],
            "tools": [], "tool_choice": "auto", "parallel_tool_calls": True,
            "stream": True, "store": False, "reasoning": {"effort": "low", "summary": "auto"},
            "include": ["reasoning.encrypted_content"], "prompt_cache_key": "ccc-network-healthcheck",
        }
        if self.config.get("max_output_tokens"):
            body["max_output_tokens"] = int(self.config["max_output_tokens"])
        return body

    def run(self, item, deep=False):
        started = time.monotonic()
        endpoint = urllib.parse.urlsplit(self.config["url"])
        if endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("probe endpoint must be HTTPS or a local test server")
        try:
            auth = json.loads(Path(self.config["auth_file"]).read_text()) if self.config.get("auth_file") else {}
        except (OSError, ValueError):
            return ProbeResult("auth", detail="probe credential unavailable", deep=deep)
        if not isinstance(auth, dict):
            return ProbeResult("auth", detail="probe credential unavailable", deep=deep)
        key = auth.get(self.config.get("auth_key", "OPENAI_API_KEY"), "")
        if self.config.get("auth_env"):
            key = os.environ.get(self.config["auth_env"], "")
        if not key and not self.config.get("allow_unauthenticated_test", False):
            return ProbeResult("auth", detail="probe credential unavailable", deep=deep)
        timeout = float(self.config.get("deep_timeout_sec", 45) if deep else self.config.get("timeout_sec", 5))
        if not deep:
            timeout = float(self.config.get("light_timeout_by_pool", {}).get(item.pool, timeout))
        port = self.ports[item.id]
        if endpoint.scheme == "https":
            connection = _DeadlineTLSConnection("127.0.0.1", port, timeout=timeout)
            connection.context = ssl.create_default_context()
            connection.set_tunnel(endpoint.hostname, endpoint.port or 443)
            path = urllib.parse.urlunsplit(("", "", endpoint.path or "/", endpoint.query, ""))
        else:
            connection = _DeadlineConnection("127.0.0.1", port, timeout=timeout)
            path = self.config["url"]
        connection.deadline = started + timeout
        connection.expired = threading.Event()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if deep else "application/json",
                   "User-Agent": self.config.get("user_agent", "codex_cli_rs/0.156.1"), "originator": "codex_cli_rs",
                   "Accept-Encoding": "identity", "session_id": str(uuid.uuid4())}
        headers.update(self.config.get("headers", {}))
        if key:
            headers["Authorization"] = "Bearer " + key
        payload = self.request_body() if deep else None
        if payload is not None:
            payload["prompt_cache_key"] = headers["session_id"]
        body = json.dumps(payload).encode() if deep else b'{"model":'
        timer = threading.Timer(max(.001, connection.deadline - time.monotonic()), connection.expire)
        timer.daemon = True
        timer.start()
        response = None
        try:
            connection.remaining()
            connection.request("POST", path, body, headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "")
            if deep and response.status == 200 and "text/event-stream" in content_type.lower():
                chunks, size, event_parts = [], 0, []
                while size < 262144:
                    connection.remaining()
                    line = response.readline(65536)
                    if not line:
                        break
                    chunks.append(line)
                    size += len(line)
                    event_parts.append(line.decode("utf-8", "replace"))
                    if not line.strip():
                        if any(event.get("type") in {"response.completed", "response.failed", "response.incomplete", "error"}
                               for event in sse_events("".join(event_parts))):
                            break
                        event_parts = []
                raw = b"".join(chunks)
            else:
                raw = response.read(262144)
            if response.getheader("Content-Encoding") == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = decoder.decompress(raw, 1048576)
                if decoder.unconsumed_tail or not decoder.eof:
                    return ProbeResult("contract", response.status, detail="oversized response", deep=deep)
            if connection.expired.is_set():
                raise TimeoutError("probe deadline")
            result = classify(response.status, content_type, raw, deep=deep)
        except (TimeoutError, socket.timeout):
            result = ProbeResult("timeout", detail="probe deadline exceeded", deep=deep)
        except (OSError, ssl.SSLError, http.client.HTTPException, zlib.error) as exc:
            result = ProbeResult("timeout" if connection.expired.is_set() else "transport",
                                 detail=type(exc).__name__, deep=deep)
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
        result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        return result


if __name__ == "__main__":
    if sys.argv[1:2] != ["supervise"]:
        raise SystemExit("internal shadow-core supervisor only")
    supervise_core(int(sys.argv[2]), sys.argv[3:])
