#!/usr/bin/env python3
"""Run only a newly built GHA Codex binary against one isolated fake supplier."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import select
import signal
import subprocess
import threading
import time

MESSAGE = "SYNTHETIC_WRITER_REJECTED_NO_REAL_ACCOUNT"
CODE = "continuation_writer_unavailable"
METADATA_DIAGNOSTIC = (
    "Model metadata for `gpt-test` not found. Defaulting to fallback metadata; "
    "this can degrade performance and cause issues."
)
IDLE_MS = 60000
ERROR_DEADLINE = 8.0
START_DEADLINE = 60.0
FLOOR = 30 * 2**30


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def policy(executable, private, port):
    quote = lambda value: json.dumps(str(value))
    return f'''(version 1)
(deny default)
(allow file-read-metadata)
(allow file-read-data file-map-executable
 (subpath "/System") (subpath "/usr") (subpath "/bin") (subpath "/sbin")
 (subpath "/opt/homebrew/Cellar") (subpath "/opt/homebrew/opt")
 (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom")
 (subpath {quote(private)}) (literal {quote(executable)}))
(allow file-write* (subpath {quote(private)}) (literal "/dev/null"))
(allow process-exec (literal {quote(executable)}))
(allow process-info* (target self))
(allow sysctl-read
 (sysctl-name "hw.ncpu") (sysctl-name "hw.logicalcpu") (sysctl-name "hw.activecpu")
 (sysctl-name "hw.memsize") (sysctl-name "hw.pagesize") (sysctl-name "hw.pagesize_compat")
 (sysctl-name "hw.machine") (sysctl-name "kern.osrelease") (sysctl-name "kern.osversion")
 (sysctl-name "kern.ostype") (sysctl-name "kern.version") (sysctl-name "kern.argmax")
 (sysctl-name "kern.hostname"))
(allow mach-lookup (global-name "com.apple.system.logger")
 (global-name "com.apple.cfprefsd.agent") (global-name "com.apple.cfprefsd.daemon"))
(allow user-preference-read (preference-domain "com.openai.codex"))
(allow ipc-posix-shm-read-data (ipc-posix-name "apple.cfprefs.{os.getuid()}v1")
 (ipc-posix-name "apple.cfprefs.daemonv1"))
(allow network-outbound (remote ip "localhost:{port}"))
'''


def child_environment(private):
    # Per-child disposable GHA directories; never change this controller's HOME.
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "RUST_LOG": "warn"}
    for key, subdir in {"HOME": "home", "CODEX_HOME": "codex-home", "TMPDIR": "tmp",
                        "XDG_CONFIG_HOME": "xdg-config", "XDG_CACHE_HOME": "xdg-cache",
                        "XDG_DATA_HOME": "xdg-data", "XDG_STATE_HOME": "xdg-state",
                        "XDG_RUNTIME_DIR": "xdg-runtime"}.items():
        destination = private / subdir
        destination.mkdir()
        env[key] = str(destination)
    return env


def command(binary, private, port):
    argv = [str(binary), "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
            "-C", str(private)]
    settings = {
        "model_provider": "fixture", "model": "gpt-test",
        "log_dir": str(private / "codex-log"), "sqlite_home": str(private / "codex-sqlite"),
        "check_for_update_on_startup": False,
        "model_providers.fixture.name": "No-auth loopback fixture",
        "model_providers.fixture.base_url": f"http://127.0.0.1:{port}/v1",
        "model_providers.fixture.wire_api": "responses",
        "model_providers.fixture.requires_openai_auth": False,
        "model_providers.fixture.supports_websockets": False,
        "model_providers.fixture.stream_idle_timeout_ms": IDLE_MS,
        "model_providers.fixture.stream_max_retries": 0,
        "model_providers.fixture.request_max_retries": 0,
    }
    for name, value in settings.items():
        argv.extend(["-c", name + "=" + json.dumps(value)])
    return argv + ["Synthetic transport check only. Do not call tools."]


class Supplier(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), SupplierHandler)
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.sent = threading.Event()
        self.finished = threading.Event()
        self.records = []


class SupplierHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.close_connection = True
        self.send_error(404)

    def do_POST(self):
        record = {"path": self.path, "received_at": time.monotonic(),
                  "auth_header_present": bool(self.headers.get("Authorization")),
                  "error_sent_at": None, "peer_closed_at": None,
                  "server_closed_before_release": False, "handler_error": None}
        with self.server.lock:
            self.server.records.append(record)
        try:
            self.connection.settimeout(5)
            size = int(self.headers.get("Content-Length", "0"))
            require(self.path == "/v1/responses", "unexpected fake supplier path")
            require(not record["auth_header_present"], "unexpected authorization header")
            require(0 < size <= 2 * 1024 * 1024, "unexpected request size")
            require(len(self.rfile.read(size)) == size, "truncated request body")
            # Discard the synthetic prompt. Record only timing/count metadata.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            payload = {"error": {"code": CODE, "type": "upstream_error", "message": MESSAGE}}
            self.wfile.write(("event: error\ndata: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
            record["error_sent_at"] = time.monotonic()
            self.server.sent.set()
            # No Content-Length, no terminal chunk, no [DONE] and no EOF.
            while not self.server.release.is_set():
                ready, _, _ = select.select([self.connection], [], [], 0.05)
                if ready and self.connection.recv(1) == b"":
                    record["peer_closed_at"] = time.monotonic()
                    break
        except (ConnectionResetError, BrokenPipeError) as error:
            if record["error_sent_at"] is not None:
                record["peer_closed_at"] = time.monotonic()
            else:
                record["handler_error"] = type(error).__name__ + ": " + str(error)
        except Exception as error:
            record["handler_error"] = type(error).__name__ + ": " + str(error)
        finally:
            record["server_closed_before_release"] = (
                not self.server.release.is_set() and record["peer_closed_at"] is None)
            record["finished_at"] = time.monotonic()
            self.close_connection = True
            self.server.finished.set()


def judge(returncode, events, records, exit_at, killed):
    require(not killed, "the harness killed the CLI; this is not a natural terminal result")
    require(returncode == 1, "CLI did not return its real failure exit code 1")
    require(len(records) == 1, "exactly one fake supplier request is required")
    record = records[0]
    require(record["error_sent_at"] is not None, "supplier did not flush event:error")
    require(not record["handler_error"] and not record["server_closed_before_release"], "fake supplier failed or closed first")
    require(record["peer_closed_at"] is not None, "CLI did not release its supplier connection")
    elapsed = exit_at - record["error_sent_at"]
    require(0 <= elapsed < ERROR_DEADLINE < IDLE_MS / 1000, "CLI failed only after the immediate-error deadline")
    require(sum(e.get("type") == "thread.started" for e in events) == 1, "missing or repeated real thread start")
    require(sum(e.get("type") == "turn.started" for e in events) == 1, "missing or repeated turn start")
    failed = [e for e in events if e.get("type") == "turn.failed"]
    require(len(failed) == 1 and MESSAGE in failed[0].get("error", {}).get("message", ""), "original message or exact turn.failed terminal missing")
    require(events[-1].get("type") == "turn.failed", "events appeared after terminal failure")
    items = [(index, event) for index, event in enumerate(events) if event.get("type", "").startswith("item.")]
    if items:
        require(len(items) == 1, "unexpected number of item events")
        index, item = items[0]
        require(index == 1 and events[0].get("type") == "thread.started"
                and len(events) > 2 and events[2].get("type") == "turn.started"
                and item == {"type": "item.completed", "item": {
                    "id": "item_0", "type": "error", "message": METADATA_DIAGNOSTIC}},
                "unexpected item; only the exact pre-turn gpt-test metadata diagnostic is permitted")
    require(not any(e.get("type") == "turn.completed" for e in events), "unexpected completed turn")
    require(set(failed[0]["error"]) == {"message"}, "R3 CLI schema unexpectedly differs from the audited message-only schema")
    return {"accepted": True, "error_to_exit_seconds": elapsed, "supplier_calls": 1,
            "turn_failed_count": 1, "structured_code_observable_in_cli": False,
            "structured_code_evidence": "separate existing R3 API fixtures only"}


def stop_owned(child):
    if child.poll() is not None:
        return child.wait()
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        if child.poll() is None:
            raise
    try:
        return child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        return child.wait(timeout=5)


def run(binary, binary_sha, output):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "CLI execution is restricted to the disposable GHA runner")
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "requires Darwin arm64")
    binary = binary.resolve(strict=True)
    require(digest(binary) == binary_sha, "compiled binary pin differs")
    output.mkdir(parents=True, exist_ok=False)
    private = (output / "private").resolve()
    private.mkdir()
    result = {"status": "NOT_STARTED", "binary_sha256": binary_sha, "returncode": None,
              "runner_error": None, "runner_killed_cli": False, "child_reaped": False,
              "samples": [], "idle_ms": IDLE_MS, "error_deadline_seconds": ERROR_DEADLINE}
    child = server = worker = None
    started = time.monotonic()
    try:
        for _ in range(2):
            stat = os.statvfs(output)
            available = stat.f_bavail * stat.f_frsize
            result["samples"].append({"elapsed": time.monotonic() - started, "free_bytes": available})
            require(available >= FLOOR, "below 30 GiB floor before CLI start")
            time.sleep(0.1)
        server = Supplier()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        sandbox = output / "codex.sb"
        sandbox.write_text(policy(binary, private, server.server_port))
        argv = ["/usr/bin/sandbox-exec", "-f", str(sandbox.resolve()), *command(binary, private, server.server_port)]
        result["argv"] = argv
        with (output / "stdout.jsonl").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
            child = subprocess.Popen(argv, env=child_environment(private), cwd=private,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            while child.poll() is None:
                now = time.monotonic()
                with server.lock:
                    sent_at = next((r["error_sent_at"] for r in server.records if r["error_sent_at"] is not None), None)
                deadline = sent_at + ERROR_DEADLINE if sent_at is not None else started + START_DEADLINE
                stat = os.statvfs(output)
                available = stat.f_bavail * stat.f_frsize
                result["samples"].append({"elapsed": now - started, "free_bytes": available})
                if child.poll() is not None:
                    break
                if now >= deadline or available < FLOOR:
                    result["runner_killed_cli"] = True
                    raise RuntimeError("CLI deadline exceeded" if now >= deadline else "disk floor crossed")
                time.sleep(0.05)
            result["returncode"] = child.wait()
            result["exit_at"] = time.monotonic()
        server.finished.wait(timeout=2)
        events = [json.loads(line) for line in (output / "stdout.jsonl").read_text().splitlines() if line.strip()]
        result["validation"] = judge(result["returncode"], events, server.records,
                                      result["exit_at"], result["runner_killed_cli"])
        result["status"] = "PASS"
    except Exception as error:
        result["runner_error"] = type(error).__name__ + ": " + str(error)
        result["status"] = "FAIL"
    finally:
        try:
            if child is not None:
                if child.poll() is None:
                    result["runner_killed_cli"] = True
                result["returncode"] = stop_owned(child)
                result["child_reaped"] = child.poll() is not None
            if server is not None:
                server.release.set()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                result["handler_stopped"] = server.finished.wait(timeout=6) if server.records else True
                result["supplier_records"] = server.records
                result["server_thread_stopped"] = not worker.is_alive()
                require(result["server_thread_stopped"], "owned fake server remained live")
                require(result["handler_stopped"], "owned fake response handler remained live")
            require(digest(binary) == binary_sha, "binary changed during smoke")
        except Exception as error:
            result["cleanup_error"] = type(error).__name__ + ": " + str(error)
            result["status"] = "FAIL"
        result["elapsed_seconds"] = time.monotonic() - started
        result["log_hashes"] = {name: digest(output / name) for name in ("stdout.jsonl", "stderr.log") if (output / name).is_file()}
        (output / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("samples", "argv")}, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.binary, args.binary_sha256, args.output.resolve()))
