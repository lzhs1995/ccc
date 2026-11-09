"""One bounded GitHub macOS fixture; local deterministic Responses provider.

This is preparation source. Importing it starts nothing. It never connects to
business cmux/Herdr, uses real credentials, or invokes native control methods.
"""
from __future__ import annotations
import argparse
import fcntl
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

import fixture_common as h
import fixture_common as f
import fixture_common as cp
from native_query import capability, identity, need, observe
from hook_probe import EVENTS

REJECTED_FAILED_BUILD_RUN = "42553480280"
EXPECTED_BUILD_RUN = "42561656291"
BUILD_HEAD = "b977911d8596c311b70d43a0c8b9c920aa2f4fd1"
BUILD_REQUEST = "5cd620c961a3ebc6f7655a474b601387b7295ace06d8e1444f2f65580e0edfea"
OFFICIAL_HOST_SHA = "416721e33af8822041dc650793512b3e58d43e0be4f3a5c6187eb4dfaad3148a"


def message(text):
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": text,
                                                    "annotations": []}]}


class FixtureProvider:
    def __init__(self, root, project, python):
        self.root, self.project, self.python = root, project, python
        self.nonce = uuid.uuid4().hex
        self.prompt = "AUTOHOOK_ROOT_" + self.nonce + ": run only this isolated automatic hook identity fixture."
        self.child_prompt = "AUTOHOOK_CHILD_" + self.nonce + ": run only this isolated automatic hook identity fixture."
        self.root_sid = self.child_sid = None
        self.stages = {}
        self.calls = {}
        self.emitted = {}
        self.requests = []
        self.errors = []
        self.spawn_item = None
        self.rejection_recorded = False
        self.ready = threading.Event()
        self.child_done = threading.Event()
        self.done = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.client = None

    def executable(self, body, role, sid):
        path = self.root / (role + "-ticket.json")
        call = "probe_" + role + "_" + self.nonce
        self.calls[role] = call
        command = "exec " + shlex.join([self.python, "-BE", str(Path(__file__).with_name("hook_probe.py")),
                                        "--ticket", str(path)])
        row = {"root": str(self.root), "role": role, "sid": sid,
               "root_sid": self.root_sid, "nonce": self.nonce,
               "client": self.client, "command": command}
        h.save_once(path, row)
        args = {"cmd": command, "workdir": str(self.project), "login": False,
                "max_output_tokens": 2000, "yield_time_ms": 10000}
        item = cp.tool(body, call, "unused", self.project)
        if item["type"] == "function_call":
            item["arguments"] = json.dumps(args)
        else:
            item["input"] = "text(await tools.exec_command(" + json.dumps(args) + "));"
        self.emitted[role] = item
        return item

    def output_present(self, body, role):
        outputs = cp.items(body, "function_call_output") + cp.items(body, "custom_tool_call_output")
        values = [r for r in outputs if r.get("call_id") == self.calls[role]]
        tool_ok = len(values) == 1 and ("AUTOHOOK_TOOL_OK:" + role + ":" + self.nonce) in str(values[0])
        for event in EVENTS:
            proof = json.loads(f.strict_file(self.root / (role + "-" + event + "-hook.json")))
            need(proof.get("passed") is True, "automatic hook proof missing")
            # Marker was only emitted by native hook stdout; it is not in tool args.
            need(proof["additional_context"] in json.dumps(body["input"]),
                 "real automatic hook additionalContext absent from subsequent provider request")
        return tool_ok

    def child_request_matches(self, body):
        # Direct native spawn_agent preserves its opaque message argument in
        # AgentMessageInputContent::EncryptedContent; it is not a user message.
        # This gates only this private provider. Native parent/registry proofs
        # are still required by the unchanged final acceptance code.
        issued = self.spawn_item
        if not isinstance(issued, dict) or issued.get("call_id") != "spawn_" + self.nonce:
            return False
        if (issued.get("type") != "function_call" or issued.get("name") != "spawn_agent"
                or json.loads(issued["arguments"]) != {"task_name": "caller_identity_fixture",
                    "message": self.child_prompt, "fork_turns": "none"}):
            return False
        items = [item for item in body.get("input", [])
                 if isinstance(item, dict) and item.get("type") == "agent_message"]
        expected = [
            {"type": "input_text", "text": "Message Type: NEW_TASK\n"
             "Task name: /root/caller_identity_fixture\nSender: /root\nPayload:\n"},
            {"type": "encrypted_content", "encrypted_content": self.child_prompt},
        ]
        return (len(items) == 1 and "role" not in items[0]
                and items[0].get("author") == "/root"
                and items[0].get("recipient") == "/root/caller_identity_fixture"
                and items[0].get("content") == expected)

    def respond(self, body, headers):
        users = ["".join(p.get("text", "") for p in item.get("content", []) if isinstance(p, dict))
                 for item in body.get("input", []) if isinstance(item, dict) and item.get("role") == "user"]
        if users and users[-1].startswith("Generate a concise, single-line task title"):
            return message('{"title":"Caller identity fixture"}')
        sid = headers.get("thread-id")
        need(isinstance(sid, str) and str(uuid.UUID(sid)) == sid, "native thread-id header missing")
        with self.lock:
            if self.root_sid is None:
                need(any(self.prompt in value for value in users), "foreign initial request")
                self.root_sid = sid
            role = "root" if sid == self.root_sid else "child"
            if role == "child":
                need(self.child_request_matches(body), "unrecognized child request")
                need(self.child_sid in (None, sid), "unexpected third fixture thread")
                self.child_sid = sid
            stage = self.stages.get(role, 0)
            need(stage < (3 if role == "root" else 2), "unexpected extra fixture model turn")
            self.stages[role] = stage + 1
            index = len(self.requests) + 1
            row = {"index": index, "role": role, "stage": stage, "thread_id": sid,
                   "body": body, "native_metadata": headers.get("x-codex-turn-metadata")}
            self.requests.append(row)
            h.save_once(self.root / ("REQUEST-%02d.json" % index), row)
        need(self.ready.wait(30) and not self.stopping.is_set(), "TUI readiness not observed")
        if stage == 0:
            return self.executable(body, role, sid)
        if role == "child":
            need(self.output_present(body, role), "child actual tool output missing")
            self.child_done.set()
            return message("AUTOHOOK_CHILD_DONE:" + self.nonce)
        if stage == 1:
            need(self.output_present(body, role), "root actual tool output missing")
            candidates = [(ns, tool) for ns, tool in cp.tool_entries(body)
                          if tool.get("type") == "function" and tool.get("name") == "spawn_agent"]
            need(len(candidates) == 1, "actual spawn_agent missing or ambiguous")
            namespace, tool = candidates[0]
            properties = tool.get("parameters", {}).get("properties", {})
            need("task_name" in properties and "fork_turns" in properties, "V2 spawn route not advertised")
            item = {"id": "spawn_" + self.nonce, "type": "function_call", "name": "spawn_agent",
                    "call_id": "spawn_" + self.nonce, "status": "completed",
                    "arguments": json.dumps({"task_name": "caller_identity_fixture",
                        "message": self.child_prompt, "fork_turns": "none"})}
            if namespace:
                item["namespace"] = namespace
            with self.lock:
                need(self.spawn_item is None, "duplicate fixture spawn")
                h.save_once(self.root / "SPAWN-ISSUED.json", item)
                self.spawn_item = item
            return item
        outputs = cp.items(body, "function_call_output")
        need(any(x.get("call_id") == "spawn_" + self.nonce and "caller_identity_fixture" in str(x)
                 for x in outputs), "real spawn result missing")
        need(self.child_done.wait(45) and not self.stopping.is_set(), "child did not complete actual tool")
        self.done.set()
        return message("AUTOHOOK_FIXTURE_DONE:" + self.nonce)

    def record_rejection(self, raw, headers, path, error):
        # Preserve the first actual failed wire body, including gzip bytes.
        # No reconstructed history is ever labelled as a received HTTP body.
        with self.lock:
            if len(self.errors) >= 4:
                return
            row = {"type": type(error).__name__, "error": str(error)[:2048]}
            self.errors.append(row)
            if self.rejection_recorded:
                return
            self.rejection_recorded = True
            try:
                captured = isinstance(raw, bytes) and len(raw) <= 8 * 1024**2
                proof = {"fixture_only": True, "path": path[:256],
                         "headers_projection": {k: headers[k][:4096] for k in
                            ("content-type", "content-encoding", "content-length", "thread-id",
                             "x-codex-turn-metadata") if isinstance(headers.get(k), str)},
                         "raw_body_captured": captured, "replay_allowed": False,
                         "error_type": type(error).__name__, "error": str(error)[:2048]}
                if captured:
                    f.write_new(self.root / "REJECTED-REQUEST.body", raw, 0o600)
                    proof.update(raw_body_bytes=len(raw), raw_body_sha256=f.sha(raw))
                else:
                    proof["raw_body_absence"] = "not read or exceeds 8 MiB bound"
                row["rejected_request_sha256"] = h.save_once(
                    self.root / "REJECTED-REQUEST.json", proof)
            except Exception as capture_error:
                row["rejection_evidence_error"] = str(capture_error)[:2048]

    def __enter__(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *unused):
                pass
            def do_GET(self):
                self.send_error(404)
            def do_POST(self):
                raw = None
                headers = {k.lower(): v for k, v in self.headers.items()}
                try:
                    need(self.path == "/v1/responses", "unexpected nonfixture API")
                    length = int(self.headers.get("Content-Length", "0"))
                    need(0 < length <= 8 * 1024**2, "request exceeds bound")
                    raw = self.rfile.read(length)
                    need(len(raw) == length, "incomplete request body")
                    decoded = gzip.decompress(raw) if headers.get("content-encoding") == "gzip" else raw
                    need(len(decoded) <= 8 * 1024**2, "expanded request exceeds bound")
                    item = owner.respond(json.loads(decoded), headers)
                    data = cp.sse(cp.completed_events(item, 2))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                except BaseException as error:
                    owner.record_rejection(raw, headers, self.path, error)
                    self.send_error(500, "fixture rejected")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_port
        return self

    def __exit__(self, *unused):
        self.stopping.set()
        self.ready.set()
        self.child_done.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)


def freeze_history(root, provider):
    rows = []
    launch = json.loads(f.strict_file(root / "LAUNCH.json"))
    registration = json.loads(f.strict_file(root / "HOOK-REGISTRATION.json"))
    need(f.sha(f.strict_file(root / "n/config.toml")) == launch["config_sha256"]
         and f.sha(f.strict_file(root / "HOOK-REGISTRATION.json")) == launch["hook_registration_sha256"]
         and f.sha(f.strict_file(root / "n/hooks.json")) == registration["sha256"],
         "private hook/config source changed during the run")
    paths = list((root / "n/sessions").rglob("*.jsonl"))
    need(len(paths) == 2, "expected only native root and child rollout")
    hook_dirs = list(root.glob("hook-*"))
    need(len(hook_dirs) == 4 and all((p / "RESULT.json").is_file() for p in hook_dirs),
         "automatic hook cardinality or completion differs; no retries")
    for path in paths:
        need(path.stat().st_size <= 16 * 1024**2, "fixture rollout bound")
        raw = path.read_bytes()
        need(raw.endswith(b"\n"), "incomplete terminal native record")
        records = [json.loads(line) for line in raw.splitlines()]
        headers = [r["payload"] for r in records if r.get("type") == "session_meta"]
        need(len(headers) == 1, "nonunique native metadata")
        meta = headers[0]
        role = "root" if meta["id"] == provider.root_sid else "child"
        need(meta["id"] == (provider.root_sid if role == "root" else provider.child_sid), "foreign rollout")
        if role == "child":
            need(meta.get("parent_thread_id") == provider.root_sid
                 and meta.get("source", {}).get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
                 == provider.root_sid, "actual child parent relation absent")
        tool = json.loads(f.strict_file(root / (role + "-tool.json")))
        pre, post = [json.loads(f.strict_file(root / (role + "-" + event + "-hook.json")))
                     for event in EVENTS]
        native_id = tool["positive"]["reply"]["callId"]
        need(pre["positive"]["reply"]["callId"] == native_id == post["positive"]["reply"]["callId"]
             and pre["payload"]["turn_id"] == post["payload"]["turn_id"]
             and all(p["positive"]["reply"]["threadId"] == meta["id"] for p in (pre, post)),
             "actual hook trigger or thread continuity differs")
        need(post["pure_reader"]["accepted"] is (role == "root")
             and tool["refusal"]["accepted"] is False,
             "pure reader scope differs")
        issued = provider.emitted[role]
        outer = issued["call_id"]
        raw_calls = [(i, r) for i, r in enumerate(records) if r.get("type") == "response_item"
                     and r.get("payload", {}).get("call_id") == outer]
        begin = [(i, r) for i, r in raw_calls if r["payload"].get("type") == issued["type"]]
        out_type = "function_call_output" if issued["type"] == "function_call" else "custom_tool_call_output"
        end = [(i, r) for i, r in raw_calls if r["payload"].get("type") == out_type]
        key = "arguments" if issued["type"] == "function_call" else "input"
        pair = (len(begin) == len(end) == 1 and begin[0][0] < end[0][0]
                and all(begin[0][1]["payload"].get(k) == issued.get(k) for k in ("name", "namespace", key))
                and "AUTOHOOK_TOOL_OK:" + role + ":" + provider.nonce in str(end[0][1]["payload"].get("output")))
        if issued["type"] == "function_call":
            need(native_id == outer, "direct native call ID differs")
        f.write_new(root / (role + "-native.jsonl"), raw, 0o600)
        rows.append({"role": role, "sid": meta["id"], "metadata": meta,
                     "native_registry_call_id": native_id, "outer_provider_call_id": outer,
                     "native_tool_roundtrip_verified": pair, "raw_call_output_pair": raw_calls,
                     "actual_auto_hook_events": [p["event"] for p in (pre, post)],
                     "root_reader_pure_acceptance": post["pure_reader"],
                     "bytes": len(raw), "sha256": f.sha(raw)})
    need({r["role"] for r in rows} == {"root", "child"}, "missing native role")
    return rows


def native_complete(root, sid, marker):
    if sid is None:
        return False
    paths = list((root / "n/sessions").rglob("*" + sid + ".jsonl"))
    need(len(paths) <= 1, "ambiguous new fixture history")
    if not paths:
        return False
    path = paths[0]
    size = path.stat().st_size
    need(size <= 16 * 1024**2, "fixture history exceeds bound")
    with path.open("rb") as stream:
        raw = stream.read(size)
    lines = raw.splitlines(keepends=True)
    return any((record := json.loads(line)).get("type") == "event_msg"
               and record.get("payload", {}).get("type") == "task_complete"
               and marker in str(record["payload"].get("last_agent_message", ""))
               for line in lines if line.endswith(b"\n"))


def validate_build(build, build_run, reviewed_source):
    """Pure admission shape check; artifact/API authenticity is pinned by root."""
    need(isinstance(build_run, str) and build_run.isdecimal()
         and int(build_run) > 0 and build_run != REJECTED_FAILED_BUILD_RUN,
         "missing new build run or explicit original failed-run rejection")
    need(build_run == EXPECTED_BUILD_RUN, "not the designated new revision build run")
    need(isinstance(reviewed_source, str) and len(reviewed_source) == 64
         and all(c in "0123456789abcdef" for c in reviewed_source), "missing reviewed effective source")
    need(build.get("passed") is True and build.get("run_id") == build_run
         and build.get("run_attempt") == 1 and build.get("head") == BUILD_HEAD
         and build.get("request_sha256") == BUILD_REQUEST
         and build.get("source_sha256") == reviewed_source
         and build.get("production_installed") is False
         and build.get("old_atomic_tests_rerun") is False,
         "not the successful reviewed new automatic-hook build")


def prepare_hooks(root, home, python):
    """Private user config only. No trust bypass and no existing user home writes."""
    hooks_path = home / "hooks.json"
    hooks = {}
    handlers = {}
    fragments = []
    for order, event in enumerate(EVENTS):
        label = {"PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use"}[event]
        command = "exec " + shlex.join([python, "-BE", str(Path(__file__).with_name("hook_probe.py")),
                                        "--root", str(root), "--event", event])
        normalized = {"event_name": label, "matcher": "Bash",
                      "hooks": [{"type": "command", "command": command, "timeout": 10, "async": False}]}
        # Exact version_for_toml sorted compact JSON. Option::None fields are
        # absent after TOML serialization; all paths/commands here are ASCII.
        normalized_bytes = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        trust = "sha256:" + f.sha(normalized_bytes)
        hook = dict(normalized["hooks"][0])
        hooks[event] = [{"matcher": "Bash", "hooks": [hook]}]
        state_key = str(hooks_path) + ":" + label + ":0:0"
        handler_id = label.replace("_", "-") + ":" + str(order) + ":" + str(hooks_path)
        handlers[event] = {"handler_id": handler_id, "state_key": state_key,
                           "trusted_hash": trust, "normalized": normalized, "command": command}
        fragments.append("[hooks.state." + json.dumps(state_key) + "]\nenabled=true\ntrusted_hash="
                         + json.dumps(trust) + "\n")
    raw = json.dumps({"hooks": hooks}, sort_keys=True, indent=2).encode() + b"\n"
    f.write_new(hooks_path, raw, 0o600)
    h.save_once(root / "HOOK-REGISTRATION.json", {"path": str(hooks_path), "sha256": f.sha(raw),
                "handlers": handlers, "feature": "hooks", "bypass_hook_trust": False,
                "state": "private_prelaunch_registration_not_yet_loading_proof"})
    return "".join(fragments)


def cleanup_owned_tui(process, master, client, root):
    """Reap this Popen child while draining its PTY, keeping unknown identity held."""
    result = {"client": client, "returncode": None, "owned_tui_reaped": False,
              "business_process_signals": 0, "no_process_group_signals": True,
              "signals": [], "pty_drain_bytes": 0, "pty_retained_bytes": 0,
              "pty_capture_truncated": False, "errors": []}
    if process is None:
        result["no_child_started"] = True
        return result
    eof = master is None
    capture_limit = 1024 * 1024

    def signal_owned(name):
        if process.poll() is not None:
            return
        try:
            actual = identity(process.pid)
        except Exception:
            # Exit may occur between the poll and native identity lookup.
            # Popen.poll reaps only this known direct child; missing identity
            # by itself never permits a signal or a success conclusion.
            if process.poll() is not None:
                return
            raise
        need(client is not None and actual == client, "refuse cleanup of changed owned process")
        if process.poll() is not None:
            return
        try:
            if name == "TERM":
                process.terminate()
            else:
                process.kill()
            result["signals"].append(name)
        except ProcessLookupError:
            if process.poll() is None:
                raise

    def reap_with_drain(seconds):
        nonlocal eof
        deadline = time.monotonic() + seconds
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if eof:
                time.sleep(min(0.05, remaining))
                continue
            ready, _, _ = select.select([master], [], [], min(0.05, remaining))
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if error.errno != 5:  # EIO is the Darwin/Linux PTY end condition.
                    raise
                eof = True
                continue
            if not data:
                eof = True
                continue
            result["pty_drain_bytes"] += len(data)
            retain = data[:max(0, capture_limit - result["pty_retained_bytes"])]
            if retain:
                with (root / "PTY.bin").open("ab") as output:
                    output.write(retain)
                result["pty_retained_bytes"] += len(retain)
            if len(retain) != len(data):
                result["pty_capture_truncated"] = True
        return True

    try:
        signal_owned("TERM")
        if not reap_with_drain(10):
            signal_owned("KILL")
            need(reap_with_drain(5), "owned child did not reap within cleanup budget")
    except Exception as error:
        result["errors"].append({"type": type(error).__name__, "error": str(error)[:2048]})
    finally:
        result["returncode"] = process.poll()
        result["owned_tui_reaped"] = result["returncode"] is not None
    return result


def run(args):
    root = h.require_runner(args.root)
    need(not list(root.iterdir()), "fixture root is not fresh; no attempt replay")
    build = json.loads(f.pinned(args.build_result, args.build_result_sha256))
    validate_build(build, args.build_run, args.reviewed_source_sha256)
    native = Path(args.binary).resolve(strict=True)
    host = Path(args.host).resolve(strict=True)
    f.pinned(native, build["binary_sha256"])
    f.pinned(host, OFFICIAL_HOST_SHA)
    package = root / "package"
    package.mkdir(mode=0o700)
    binary = package / "codex"
    shutil.copy2(native, binary)
    shutil.copy2(host, package / "codex-code-mode-host")
    f.pinned(binary, build["binary_sha256"])
    project = root / "project"
    project.mkdir(mode=0o700)
    (root / "h").mkdir(mode=0o700)
    home = f.prepare_native_home(root, root / "n")
    need(len(os.fsencode(home / "credential-observations/compaction-recovery/c-99999999-12345678.sock")) < 104,
         "use a shorter private RUNNER_TEMP fixture path for the native Unix socket")
    f.write_new(home / "credential-observations/enabled-v1", b"ccc-request-credentials-v1\n", 0o600)
    python = str(Path(sys.executable).resolve(strict=True))
    process = None
    master = None
    client = None
    failure = None
    outcome = None
    with FixtureProvider(root, project, python) as provider:
        config = ('model="gpt-6-astra"\nmodel_provider="local_fixture"\n'
                  'approval_policy="never"\nsandbox_mode="danger-full-access"\n'
                  'check_for_update_on_startup=false\nmodel_auto_compact_token_limit=100000\n'
                  '[tui]\nscreen_reader_detection_done=true\n'
                  '[features]\nasync_compaction=false\nplugins=false\napps=false\n'
                  'skip_host_skill_discovery=true\nmulti_agent_v2=true\nhooks=true\n'
                  '[model_providers.local_fixture]\nname="Deterministic local caller fixture"\n'
                  'wire_api="responses"\nbase_url=' + json.dumps(provider.url) + '\n'
                  'requires_openai_auth=false\nsupports_websockets=false\n'
                  'request_max_retries=0\nstream_max_retries=0\n'
                  '[projects.' + json.dumps(str(project)) + ']\ntrust_level="trusted"\n')
        config += prepare_hooks(root, home, python)
        f.write_new(home / "config.toml", config.encode(), 0o600)
        f.write_new(home / "AGENTS.md", b"Only this private deterministic caller fixture is authorized.\n", 0o600)
        argv = [str(binary), "--no-daemon", "--no-alt-screen", "-C", str(project), provider.prompt]
        env = {k: os.environ[k] for k in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
               "GITHUB_RUN_ATTEMPT", "RUNNER_TEMP")}
        env.update(HOME=str(root / "h"), CODEX_HOME=str(home), CODEX_CLIENT_THREAD_OBSERVER="1",
                   PATH=str(Path(python).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin", SHELL="/bin/zsh",
                   TERM="xterm-256color", LANG="en_US.UTF-8", TMPDIR=str(root),
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        h.save_once(root / "LAUNCH.json", {"argv": argv, "environment": env, "build": build,
                    "config_sha256": f.sha(config.encode()),
                    "hook_registration_sha256": f.sha(f.strict_file(root / "HOOK-REGISTRATION.json")),
                    "host_sha256": OFFICIAL_HOST_SHA, "fixture_only": True})
        try:
            master, slave = os.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen([python, "-BE", str(Path(__file__).resolve()),
                                        "--pty-child", str(root / "LAUNCH.json")],
                                       stdin=slave, stdout=slave, stderr=slave, env=env, cwd=project, close_fds=True)
            os.close(slave)
            client = identity(process.pid)
            provider.client = client
            h.save_once(root / "CLIENT.json", client)
            deadline = time.monotonic() + 120
            ready = False
            dsr = 0
            pending = b""
            with (root / "PTY.bin").open("xb") as terminal:
                while time.monotonic() < deadline:
                    need(process.poll() is None, "owned TUI exited unexpectedly")
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        part = os.read(master, 65536)
                        need(part, "owned PTY EOF")
                        terminal.write(part)
                        pending += part
                        while b"\x1b[6n" in pending:
                            _, pending = pending.split(b"\x1b[6n", 1)
                            os.write(master, b"\x1b[1;1R")
                            dsr += 1
                            need(dsr <= 16, "unbounded terminal interrogation")
                        pending = pending[-16:]
                    if not ready:
                        path = home / "credential-observations/compaction-recovery" / (
                            "client-%s-control.json" % client["pid"])
                        if path.is_file():
                            ticket = {"root": str(root), "client": client}
                            _, _, cap = capability(ticket)
                            outside = observe(ticket)
                            need(isinstance(outside["reply"].get("error"), str)
                                 and outside["reply"].get("replayAllowed") is False
                                 and "threadId" not in outside["reply"], "unregistered runner accepted")
                            h.save_once(root / "OUTSIDER-REFUSED.json", outside)
                            h.save_once(root / "NATIVE-CAPABILITY.json", cap)
                            ready = True
                            provider.ready.set()
                    need(not provider.errors, "provider rejected: " + str(provider.errors))
                    if (provider.done.is_set()
                            and native_complete(root, provider.root_sid, "AUTOHOOK_FIXTURE_DONE:" + provider.nonce)
                            and native_complete(root, provider.child_sid, "AUTOHOOK_CHILD_DONE:" + provider.nonce)):
                        break
                need(provider.done.is_set() and provider.child_done.is_set()
                     and native_complete(root, provider.root_sid, "AUTOHOOK_FIXTURE_DONE:" + provider.nonce)
                     and native_complete(root, provider.child_sid, "AUTOHOOK_CHILD_DONE:" + provider.nonce),
                     "fixture/native completion timeout; no retry")
            outcome = {"fixture_only": True, "root_sid": provider.root_sid, "child_sid": provider.child_sid,
                       "provider_requests": len(provider.requests), "terminal_control_responses": dsr,
                       "business_input_operations": 0, "native_control_requests": 0,
                       "registry_injected": False, "supplier_model_requests": 0}
        except BaseException as error:
            failure = {"type": type(error).__name__, "error": str(error), "provider_errors": provider.errors}
        finally:
            if outcome is not None:
                h.save_once(root / "COMPLETION-BEFORE-CLEANUP.json", outcome)
            cleanup = cleanup_owned_tui(process, master, client, root)
            if master is not None:
                os.close(master)
            h.save_once(root / "CLEANUP.json", cleanup)
            if cleanup["errors"] or not cleanup["owned_tui_reaped"]:
                if failure is None:
                    failure = {"type": "RuntimeError", "error": "owned fixture cleanup failed",
                               "provider_errors": provider.errors}
                failure["cleanup"] = cleanup
        if failure:
            h.save_once(root / "FAILURE.json", failure)
            raise RuntimeError(failure["error"])
        history = freeze_history(root, provider)
        outcome["native_history"] = history
        outcome["runtime_probe_passed"] = True
        outcome["complete_native_exec_pair_verified"] = all(r["native_tool_roundtrip_verified"] for r in history)
        outcome["actual_native_tool_roundtrips"] = sum(r["native_tool_roundtrip_verified"] for r in history)
        outcome["passed"] = outcome["complete_native_exec_pair_verified"]
        outcome["production_installed"] = False
        outcome["isolated_automatic_root_and_child_hooks_verified"] = True
        outcome["actual_automatic_hook_events"] = 4
        outcome["live_root_hook_verified"] = False
        outcome["cmux_host_verified"] = False
        outcome["complete_inbox_or_bidirectional_receipt_verified"] = False
        outcome["original_pure_reader_only"] = True
        outcome["installed_hook_trust_or_loading_changed"] = False
        outcome["old_180_300_rerun"] = False
        h.save_once(root / "RESULT.json", outcome)
        need(outcome["passed"], "native event mapping still needs review; raw evidence preserved")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--pty-child":
        launch = json.loads(Path(sys.argv[2]).read_bytes())
        h.require_runner(Path(sys.argv[2]).parent)
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execve(launch["argv"][0], launch["argv"], launch["environment"])
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--build-result", type=Path, required=True)
    parser.add_argument("--build-run", required=True)
    parser.add_argument("--build-result-sha256", required=True)
    parser.add_argument("--reviewed-source-sha256", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
