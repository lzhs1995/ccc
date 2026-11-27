# Codex → agentrouter 本地净化代理（修复 "The requested item was created under a different Azure OpenAI resource"）
# 原因：agentrouter 把请求分流到多个 Azure 资源；Codex 在 store=false 下每轮重发全部历史，
# 其中 reasoning/compaction 的 encrypted_content 和各 item 的 id 绑定在建项的那个资源上，
# 被路由到别的资源时就整轮失败。本代理在转发前：去掉所有 input item 的 id、丢弃 reasoning
# 与 compaction 加密项、去掉 previous_response_id；其余原样转发，响应（含 SSE）逐块直通。
# v2.1（V22 候选）：本地 GET /health；未剥任何项时原字节+原 Content-Encoding 转发；
# 上游中途断（异常或短于 Content-Length）时不写结束块，断开连接，不把截断伪装成正常结束。
import gzip
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("SANITIZER_UPSTREAM", "https://agentrouter.org")
PORT = int(os.environ.get("SANITIZER_PORT", "18765"))
LOG = os.environ.get("SANITIZER_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.log"))
DROP_TYPES = {"reasoning", "compaction", "compaction_summary"}
HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "keep-alive", "proxy-connection"}
_log_lock = threading.Lock()
with open(os.path.abspath(__file__), "rb") as _fh:
    SCRIPT_SHA256 = hashlib.sha256(_fh.read()).hexdigest()
STARTED_AT = time.time()


def sanitize(payload):
    """返回 (新 payload, 统计)。只改 input 列表和 previous_response_id，不改其他字段。"""
    stats = {"items_in": 0, "items_out": 0, "dropped": {}, "ids_removed": 0, "prev_id_removed": False}
    if not isinstance(payload, dict):
        return payload, stats
    out = dict(payload)
    if out.pop("previous_response_id", None) is not None:
        stats["prev_id_removed"] = True
    items = out.get("input")
    if isinstance(items, list):
        stats["items_in"] = len(items)
        kept = []
        for item in items:
            if isinstance(item, dict):
                kind = item.get("type")
                if kind in DROP_TYPES:
                    stats["dropped"][kind] = stats["dropped"].get(kind, 0) + 1
                    continue
                if "id" in item:
                    item = {k: v for k, v in item.items() if k != "id"}
                    stats["ids_removed"] += 1
            kept.append(item)
        out["input"] = kept
        stats["items_out"] = len(kept)
    return out, stats


RESOURCE_ERRORS = (b"different Azure OpenAI resource", b"could not be verified")
_POISONED = set()          # 撞过跨资源错误的请求里出现过的 item id（只在内存里，进程重启后重新学习）
_POISONED_MAX = 200000
_poison_lock = threading.Lock()


def is_resource_error(body):
    return any(marker in (body or b"") for marker in RESOURCE_ERRORS)


def remember_poisoned(payload):
    items = payload.get("input") if isinstance(payload, dict) else None
    with _poison_lock:
        if len(_POISONED) > _POISONED_MAX:
            _POISONED.clear()
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                _POISONED.add(item["id"])


def strip_poisoned(payload):
    """只处理已知中毒项：消息/工具项去 id，reasoning/compaction 整条丢；其余原样。"""
    stats = {"items_in": 0, "items_out": 0, "dropped": {}, "ids_removed": 0}
    items = payload.get("input") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return payload, stats
    with _poison_lock:
        poisoned = set(_POISONED)
    stats["items_in"] = len(items)
    kept = []
    for item in items:
        if isinstance(item, dict) and item.get("id") in poisoned:
            if item.get("type") in DROP_TYPES:
                stats["dropped"][item["type"]] = stats["dropped"].get(item["type"], 0) + 1
                continue
            item = {k: v for k, v in item.items() if k != "id"}
            stats["ids_removed"] += 1
        kept.append(item)
    stats["items_out"] = len(kept)
    out = dict(payload)
    out["input"] = kept
    return out, stats


def _log(entry):
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _log_lock, open(LOG, "a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        # 兼容 Content-Length 与 chunked 两种请求体
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                parts.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(parts)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _forward(self, method):
        try:
            self._forward_inner(method)
        except Exception as exc:  # 任何异常都留痕，避免静默 502
            import traceback
            _log({"method": method, "path": self.path, "exception": repr(exc)[:300],
                  "trace": traceback.format_exc()[-1200:]})
            raise

    def _forward_inner(self, method):
        entry = {"method": method, "path": self.path,
                 "req_headers": {k: v for k, v in self.headers.items()
                                 if k.lower() in ("content-length", "transfer-encoding", "content-encoding", "upgrade",
                                                  "connection", "user-agent", "accept", "expect")}}
        _log(dict(entry, phase="received"))
        body = self._read_body()
        entry["req_bytes"] = len(body)
        encoding = (self.headers.get("Content-Encoding") or "").lower()
        payload = None
        if body and method == "POST":
            try:
                if encoding == "zstd":
                    import compression.zstd as zstd
                    raw = zstd.decompress(body)
                else:
                    raw = gzip.decompress(body) if encoding == "gzip" else body
                if encoding in ("", "identity", "gzip", "zstd"):
                    payload = json.loads(raw)
                    encoding = ""
                else:
                    entry["passthrough"] = "content-encoding " + encoding
            except (ValueError, OSError) as exc:
                entry["passthrough"] = "not json: " + type(exc).__name__
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
        if encoding:
            headers["Content-Encoding"] = encoding
        headers["Accept-Encoding"] = "identity"
        # v2 近无损：① 原样（只去掉已知中毒项）→ ② 撞「跨资源」400 时全量净化重试一次，
        # 并把本次请求里的全部 id 记为中毒，下一轮只剥这些，新生成的 reasoning 保留。
        attempts = [("as_is", lambda p: strip_poisoned(p))]
        if isinstance(payload, dict):
            attempts.append(("full_sanitize", lambda p: sanitize(p)))
        else:
            attempts = [("raw", None)]
        original_encoding = (self.headers.get("Content-Encoding") or "").lower()
        upstream = None
        prefetched = None
        for mode, transform in attempts:
            data = body
            stats = {}
            send_headers = dict(headers)
            if transform is not None:
                new_payload, stats = transform(payload)
                if mode == "as_is" and not stats.get("ids_removed") and not stats.get("dropped"):
                    # 没剥任何项：原字节、原压缩头，不重新序列化
                    if original_encoding:
                        send_headers["Content-Encoding"] = original_encoding
                    stats["bytes_preserved"] = True
                else:
                    data = json.dumps(new_payload, ensure_ascii=False).encode()
            entry.setdefault("modes", []).append({"mode": mode, **stats})
            request = urllib.request.Request(UPSTREAM + self.path, data=data if data else None, headers=send_headers, method=method)
            started = time.time()
            try:
                upstream = urllib.request.urlopen(request, timeout=900)
                prefetched = None
            except urllib.error.HTTPError as exc:
                upstream = exc
                prefetched = exc.read()
            except Exception as exc:  # 上游不可达：返回 502，Codex 会按自身策略重试
                entry["status"] = 502
                entry["error"] = repr(exc)[:200]
                _log(entry)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            entry["modes"][-1]["status"] = upstream.status if hasattr(upstream, "status") else upstream.code
            entry["modes"][-1]["ms"] = int((time.time() - started) * 1000)
            if prefetched is not None and is_resource_error(prefetched) and mode != attempts[-1][0]:
                remember_poisoned(payload)
                continue
            break
        entry["status"] = upstream.status if hasattr(upstream, "status") else upstream.code
        self.send_response(entry["status"])
        for key, value in upstream.headers.items():
            if key.lower() not in HOP and key.lower() != "content-encoding":
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if entry["status"] >= 400:
            entry["error_body"] = b""
        pending = [prefetched] if prefetched is not None else []
        expected = None if prefetched is not None else upstream.headers.get("Content-Length")
        received = 0
        try:
            while True:
                if pending:
                    chunk = pending.pop()
                elif prefetched is not None:
                    chunk = b""  # 错误体已整段读出
                else:
                    try:
                        chunk = upstream.read1(65536) if hasattr(upstream, "read1") else upstream.read(65536)
                    except (http.client.HTTPException, OSError) as exc:
                        # 上游中途断：不写结束块，让客户端看到截断而非「正常结束」
                        entry["upstream_aborted"] = repr(exc)[:200]
                        self.close_connection = True
                        return
                if not chunk:
                    break
                received += len(chunk)
                if entry["status"] >= 400 and len(entry["error_body"]) < 600:
                    entry["error_body"] += chunk[:600]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            if expected is not None and expected.isdigit() and received < int(expected):
                entry["upstream_aborted"] = "short body %d<%s" % (received, expected)
                self.close_connection = True
                return
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            entry["client_disconnected"] = True
        finally:
            if isinstance(entry.get("error_body"), bytes):
                entry["error_body"] = entry["error_body"].decode("utf-8", "replace")
            _log(entry)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        if self.path == "/health":
            # 本地应答，不转发上游；不含任何密钥
            body = json.dumps({"service": "codex-azure-item-sanitizer", "pid": os.getpid(),
                               "script_sha256": SCRIPT_SHA256, "port": PORT, "upstream": UPSTREAM,
                               "started_at": STARTED_AT, "poisoned_ids": len(_POISONED)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._forward("GET")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print("listening 127.0.0.1:%d -> %s" % (PORT, UPSTREAM), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
