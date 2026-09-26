"""Optional B-only response-streak guard, using live native Codex RPC events.

Every model backend belongs to one exact cmux surface. The private websocket
relay owns its stdio, so closing the workspace gate also rejects already queued
turn/start requests. Transcript files and log-database flushes are not clocks.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

from ccc_guard_transport import MAX_MESSAGE, WebSocket
import ccc_guard_scope as scope

VERSION = 1
TERM_AFTER = .350
KILL_AFTER = .650
STOP_DEADLINE = 1.0
MODEL_DELTAS = frozenset({"item/agentMessage/delta", "item/reasoning/summaryTextDelta",
                         "item/reasoning/textDelta", "item/plan/delta"})
# The first-response cut is off. A detected model event must not pause a pool
# or interrupt its sessions. B only creates and authorizes.
CONNECTION_CUT_ENABLED = False
REQUIRED_RESPONSES = 3
# Coverage, membership, protocol and setup failures were pausing every pool
# and aborting B. Only the operator's P key may pause or interrupt a pool.
AUTOMATIC_POOL_STOP = False
TURN_INPUT = frozenset({"turn/start", "turn/steer", "review/start", "thread/fork",
                        "thread/realtime/start", "thread/goal/set", "thread/compact/start",
                        "thread/rollback", "thread/queue/start", "thread/queue/add", "thread/queue/update",
                        "thread/shellCommand", "thread/inject_items", "command/exec"})


def core():
    import cmux_codex_watch
    return cmux_codex_watch


def native_binary():
    # The installed conditional launcher delegates here too. Never recurse
    # through that launcher when starting a guardian-owned native backend.
    link = Path.home() / "Library/Application Support/cmux-codex-continue/codex-launcher.json"
    value = read_json(link).get("native_binary") if link.exists() else "/opt/homebrew/bin/codex"
    candidate = Path(value) if isinstance(value, str) and value else Path()
    wrapper = link.with_name("codex-guard").resolve()
    if (not candidate.is_absolute() or not candidate.is_file()
            or not os.access(candidate, os.X_OK) or candidate.resolve() == wrapper):
        raise RuntimeError("original native Codex executable cannot be proved; no session launched")
    return str(candidate.resolve())


def uid(value):
    return str(uuid.UUID(str(value))).upper()


def guard_root(config_path):
    return Path(config_path).resolve().parent / "batch-guards"


def pool_dir(config_path, workspace_id):
    return guard_root(config_path) / uid(workspace_id)


def socket_dir(config_path):
    digest = hashlib.sha256(str(Path(config_path).resolve()).encode()).hexdigest()[:16]
    return Path("/tmp") / f"ccc-guard-{os.getuid()}-{digest}"


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("guard directory is not private: " + str(path))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def write_json(path, value):
    core().atomic_write_json(Path(path), value)


def valid_job(job, job_id, workspace_id):
    """Require the same complete slot structure for new and migrated B rules."""
    if not isinstance(job, dict) or job.get("id") != job_id:
        return False
    slots = job.get("slots")
    return (uid(job.get("workspace_id")) == uid(workspace_id)
            and isinstance(slots, list) and bool(slots)
            and all(isinstance(slot, dict) and type(slot.get("index")) is int
                    and slot["index"] == index for index, slot in enumerate(slots)))


def provenance(config_path, workspace_id, config=None):
    """Only an actual B job can confer circuit-breaker scope."""
    config = config if config is not None else core().ConfigStore(Path(config_path)).load()
    try:
        wid = uid(workspace_id)
    except (ValueError, TypeError):
        return None
    rule = next((r for r in config.get("workspace_rules", []) if r.get("workspace_id", "").upper() == wid), {})
    setting = rule.get("batch_guard", {})
    jid = setting.get("origin_job_id") if isinstance(setting, dict) else None
    if not jid or setting.get("version") != VERSION:
        return None
    try:
        uuid.UUID(jid)
        job = read_json(Path(config_path).parent / "workspace-batches" / jid / "job.json")
        if not valid_job(job, jid, wid):
            return None
    except (OSError, ValueError, TypeError):
        return None
    return rule


def blocked(config_path, workspace_id):
    # A tiny per-workspace marker, including on the last send boundary. Ordinary
    # workspaces have no marker and never acquire global pause semantics.
    try:
        return (pool_dir(config_path, workspace_id) / "STOP.json").exists()
    except (ValueError, OSError):
        return False


def operator_paused(config_path, workspace_id, config=None):
    """True only for a pause the operator asked for.

    Automatic coverage, setup and migration markers must not keep B from
    opening Codex. Manual P leaves pause_origin empty or operator_pause.
    """
    rule = provenance(config_path, workspace_id, config) or {}
    if rule.get("paused") and rule.get("pause_origin") in {None, "", "user", "operator_pause"}:
        return True
    try:
        marker = read_json(pool_dir(config_path, workspace_id) / "STOP.json")
    except (OSError, ValueError):
        marker = {}
    return marker.get("reason") == "operator_pause"


def snapshot(config_path, workspace_id):
    try:
        return read_json(pool_dir(config_path, workspace_id) / "state.json")
    except (OSError, ValueError):
        return {"phase": "fault", "reason": "protection state unreadable"}


def binding(config_path, target, *, verify=True):
    try:
        path = pool_dir(config_path, target["workspace_id"]) / (uid(target["surface_id"]) + ".json")
        value = read_json(path)
        if (value.get("workspace_id") != uid(target["workspace_id"])
                or value.get("surface_id") != uid(target["surface_id"])):
            return None
        if verify:
            from ccc_codex_queue import process_placement_start
            if (type(value.get("pid")) is not int or value["pid"] <= 1
                    or not isinstance(value.get("process_start"), (int, float)) or value["process_start"] <= 0
                    or process_placement_start(value["pid"], target) != value["process_start"]):
                return None
            if value.get("birth") and scope.birth(value["pid"], codex=True) != value["birth"]:
                return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def model_evidence(message, session_id, turn_id):
    """One complete successful answer, never a delta, thought or tool call."""
    if not CONNECTION_CUT_ENABLED:
        return None
    if not session_id or not turn_id or not isinstance(message, dict):
        return None
    method, p = message.get("method"), message.get("params")
    if method != "turn/completed" or not isinstance(p, dict) or p.get("threadId") != session_id:
        return None
    turn = p.get("turn")
    if (not isinstance(turn, dict) or turn.get("id") != turn_id
            or turn.get("status") != "completed" or turn.get("error") is not None):
        return None
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    answers = [item for item in items if completed_answer(item)]
    if answers:
        return {"method": method, "session_id": session_id, "turn_id": turn_id,
                "characters": sum(len(item["text"].strip()) for item in answers)}
    return None


def completed_answer(item):
    return (isinstance(item, dict) and item.get("type") == "agentMessage"
            and isinstance(item.get("id"), str) and bool(item["id"])
            and item.get("phase") in (None, "final_answer")
            and isinstance(item.get("text"), str) and bool(item["text"].strip()))


class ResponseStreak:
    """Per-native-session evidence. A restart starts at zero, never at old history."""
    def __init__(self):
        self.identity = None
        self.turn = None
        self.seen = set()
        self.answers = {}
        self.successes = []
        self.failed = False
        self.qualified = None

    def reset_failure(self):
        self.successes.clear()
        self.qualified = None
        self.failed = True

    def observe(self, message, identity):
        if identity != self.identity:
            self.__init__()
            self.identity = identity
        p = message.get("params")
        if not isinstance(p, dict) or p.get("threadId") != identity[2]:
            return None
        method = message.get("method")
        turn = p.get("turn") if isinstance(p.get("turn"), dict) else {}
        tid = turn.get("id")
        if method == "turn/started":
            if not isinstance(tid, str) or not tid or tid in self.seen or tid == self.turn:
                return None
            if self.turn is not None:
                self.reset_failure()  # The previous turn never completed.
            self.turn, self.answers, self.failed = tid, {}, False
        elif method == "error" and p.get("turnId") == self.turn and self.turn:
            self.reset_failure()
        elif method == "item/completed" and p.get("turnId") == self.turn and self.turn:
            item = p.get("item")
            if completed_answer(item):
                self.answers[item["id"]] = dict(item)
        elif method == "turn/completed" and tid and tid == self.turn and tid not in self.seen:
            self.seen.add(tid)
            self.turn = None
            items = turn.get("items") if isinstance(turn.get("items"), list) else []
            event = {"method": method, "params": {**p, "turn": {
                **turn, "items": [*self.answers.values(), *items]}}}
            evidence = model_evidence(event, identity[2], tid)
            self.answers = {}
            if not evidence or self.failed:
                self.reset_failure()
                return None
            self.successes.append(tid)
            self.successes = self.successes[-REQUIRED_RESPONSES:]
            if len(self.successes) >= REQUIRED_RESPONSES:
                self.qualified = {**evidence, "consecutive_responses": len(self.successes),
                                  "turn_ids": list(self.successes), "identity": identity}
                return self.qualified
        return None


def request(config_path, command, *, timeout=5, **params):
    address = socket_dir(config_path) / "control.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(timeout)
        stream.connect(str(address))
        stream.sendall((json.dumps({"command": command, **params}) + "\n").encode())
        data = bytearray()
        while b"\n" not in data:
            chunk = stream.recv(65536)
            if not chunk or len(data) > MAX_MESSAGE:
                raise RuntimeError("guard control acknowledgement unavailable")
            data.extend(chunk)
        reply = json.loads(data.split(b"\n", 1)[0])
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "guard request rejected"))
        return reply["result"]


def _guard_script(command):
    try:
        return next(Path(arg).resolve() for arg in shlex.split(command) if arg.endswith("ccc_batch_guard.py"))
    except (StopIteration, ValueError):
        return None


def _same_guard_bytes(other):
    import hashlib
    try:
        current = Path(__file__).resolve().read_bytes()
        return other.is_file() and hashlib.sha256(other.read_bytes()).digest() == hashlib.sha256(current).digest()
    except OSError:
        return False


def _current_guard(state):
    """True only for this guard logic. An older serve process must not stay in charge.

    The release tree and the staged runtime are the same program at two paths.
    Either may launch Codex, so a serve process of these exact bytes is current.
    """
    pid = state.get("pid")
    if state.get("version") != VERSION or type(pid) is not int or pid <= 1:
        return False
    try:
        command = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="], text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return False
    if " serve " not in f" {command} ":
        return False
    script = _guard_script(command)
    if script is None:
        return False
    return script == Path(__file__).resolve() or _same_guard_bytes(script)


def ensure_service(config_path):
    root = guard_root(config_path)
    private_directory(root)
    with core().FileLock(root / "start.lock", timeout_sec=10):
        running = False
        try:
            state = request(config_path, "ping", timeout=.3)
            if state.get("version") == VERSION and (not AUTOMATIC_POOL_STOP or _current_guard(state)):
                return state
            running = True
        except (OSError, ValueError, RuntimeError):
            pass
        if not running:
            with (root / "service.log").open("ab") as log:
                subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "serve", "--config", str(config_path)],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                state = request(config_path, "ping", timeout=.2)
                if state.get("ready") and _current_guard(state):
                    return state
            except (OSError, ValueError, RuntimeError):
                time.sleep(.05)
            else:
                time.sleep(.025)
        raise RuntimeError("B protection service did not become ready; no model request was submitted")


def arm(config_path, workspace_id, *, resume=False):
    if not AUTOMATIC_POOL_STOP:
        if resume:
            # Only the explicit W action reaches here after unpausing its
            # config rule. Preserve the old STOP evidence without letting that
            # marker permanently veto later B starts. Never adopt any process.
            with core().workspace_input_lock(config_path, uid(workspace_id)):
                rule = core().workspace_rule_by_id(core().ConfigStore(Path(config_path)).load(), workspace_id)
                if rule.get("paused") or not rule.get("enabled", True):
                    raise RuntimeError("workspace was paused again; resume cancelled")
                marker = pool_dir(config_path, workspace_id) / "STOP.json"
                if marker.exists():
                    marker.rename(marker.with_name("STOP.resumed-" + uuid.uuid4().hex + ".json"))
        return {"phase": "disabled"}
    private_directory(guard_root(config_path))
    directory = pool_dir(config_path, workspace_id)
    private_directory(directory)
    with core().FileLock(directory / "arm.lock", timeout_sec=180):
        return _arm(config_path, workspace_id, resume=resume)


def _arm(config_path, workspace_id, *, resume=False):
    if not AUTOMATIC_POOL_STOP:
        return {"phase": "disabled"}
    from ccc_guard_migration import adopt_workspace
    # Adoption owns its preflight-versus-post-capture failure policy. Nothing
    # it raises may enter this caller's generic stop fallback before it returns
    # a successfully preserved workspace.
    adoption = adopt_workspace(config_path, workspace_id)
    try:
        ensure_service(config_path)
        if adoption.get("adopted"):
            state = request(config_path, "status", workspace_id=uid(workspace_id))
            if state.get("phase") != "arming" or (state.get("trip") or {}).get("reason") != "session_migration":
                raise RuntimeError("protection changed during original-session adoption")
        return request(config_path, "arm", timeout=90, workspace_id=uid(workspace_id),
                       resume=bool(resume or adoption.get("adopted")))
    except Exception:
        # Failed adoption/observation must never leave the old B sessions
        # consuming upstream while the new batch cannot be protected.
        with contextlib.suppress(Exception):
            request(config_path, "stop", timeout=3, workspace_id=uid(workspace_id), reason="protection_setup_failed")
        raise


def pause(config_path, workspace_id, reason="operator_pause"):
    ensure_service(config_path)
    return request(config_path, "stop", timeout=2, workspace_id=uid(workspace_id), reason=reason)


class Workspace:
    def __init__(self, service, wid):
        self.service, self.wid = service, wid
        self.directory = pool_dir(service.config_path, wid)
        private_directory(self.directory)
        previous = read_json(self.directory / "state.json")
        self.epoch = previous.get("epoch") or uuid.uuid4().hex
        self.phase = "recovering" if previous else "arming"
        self.endpoints = {}
        self.unmanaged = {}
        self.coverage_error = None
        self.stop_targets = previous.get("stop_targets", [])
        self.trip = previous.get("trip")
        self.transitions = previous.get("transitions", [])[-15:]
        self.stop_task = None
        self.pause_task = None
        self.last_saved = None
        self.pending_evidence = set()

    def save(self):
        value = {"version": VERSION, "workspace_id": self.wid, "epoch": self.epoch,
                 "phase": self.phase, "trip": self.trip, "coverage_error": self.coverage_error,
                 "transitions": self.transitions,
                 "stop_targets": self.stop_targets,
                 "surfaces": {sid: e.summary() for sid, e in self.endpoints.items()},
                 "unmanaged": {str(pid): e.summary() for pid, e in self.unmanaged.items()}}
        serialized = json.dumps(value, sort_keys=True)
        if serialized != self.last_saved:
            write_json(self.directory / "state.json", value)
            self.last_saved = serialized

    def ready(self):
        return (self.phase == "watching" and not self.coverage_error
                and self.service.healthy() and not blocked(self.service.config_path, self.wid))

    def open_gate(self):
        # Automatic pause is off. Coverage, watchdog and leftover STOP markers
        # must not refuse the Codex launch B already knew how to start.
        if not AUTOMATIC_POOL_STOP:
            return not operator_paused(self.service.config_path, self.wid)
        return not self.pending_evidence and self.ready()

    def targets(self):
        return [*self.endpoints.values(), *self.unmanaged.values()]

    def trigger(self, reason, source=None, evidence=None, detected=None):
        if not AUTOMATIC_POOL_STOP and reason != "operator_pause":
            return
        if reason == "first_model_response":
            return  # Removed policy: one model event is never connection proof.
        if reason == "three_completed_responses" and (not CONNECTION_CUT_ENABLED or
                not evidence or evidence.get("consecutive_responses", 0) < REQUIRED_RESPONSES):
            return
        if self.stop_task and not self.stop_task.done():
            return
        if self.phase in {"stopped", "failed"} and not any(e.active or e.awaiting_turn for e in self.targets() if e.in_scope):
            return
        now = time.monotonic() if detected is None else detected
        self.transitions = (self.transitions + [{"reason": reason, "previous_phase": self.phase,
            "monotonic": time.monotonic(), "active": [e.sid for e in self.targets() if e.active],
            "pending": [e.sid for e in self.targets() if e.awaiting_turn]}])[-16:]
        self.phase = "stopping"  # Synchronous gate before any await or file I/O.
        self.stop_targets = []
        self.trip = {"reason": reason, "detected_at": time.time(), "detected_monotonic": now,
                     "surface_id": source.sid if source else None,
                     "session_id": source.session_id if source else None,
                     "turn_id": source.turn_id if source else None, "evidence": evidence,
                     "connected": reason == "three_completed_responses"}
        self.stop_task = asyncio.create_task(self.finish_stop(now))
        self.pause_task = asyncio.create_task(self.persist_pause())
        self.service.schedule_save(self)

    async def persist_pause(self):
        try:
            await asyncio.to_thread(write_json, self.directory / "STOP.json", {"epoch": self.epoch, **self.trip})
        except OSError as exc:
            self.trip["persistence_error"] = str(exc)
        def update(config):
            rule = core().workspace_rule_by_id(config, self.wid)
            rule.update(paused=True, paused_at=time.time(), batch_cancelled_at=time.time(),
                        pause_origin="batch_first_response" if self.trip.get("connected") else
                            "operator_pause" if self.trip.get("reason") == "operator_pause" else "batch_guard")
            rule.pop("active_batch_id", None)
            if self.trip.get("connected"):
                rule["batch_success_at"] = self.trip["detected_at"]
        try:
            await asyncio.to_thread(core().ConfigStore(self.service.config_path).mutate, update)
        except Exception as exc:
            self.trip["pause_persistence_error"] = str(exc)

    async def finish_stop(self, started):
        term_sent = kill_sent = False
        affected = {}
        # One fresh membership + process snapshot covers the whole workspace,
        # including any directly launched or not-yet-migrated native process.
        try:
            await self.service.audit(notify=False)
        except Exception as exc:
            self.coverage_error = str(exc)
        async def interrupt_current(endpoint):
            if await self.service.member(self.wid, endpoint.sid, owned=True):
                endpoint.request_interrupt(started)
            elif (self.service.inventory is not None
                  and time.monotonic() - self.service.inventory_at < .2
                  and self.service.inventory.get(endpoint.sid) != self.wid):
                endpoint.in_scope = False
                endpoint.error = "surface moved out; excluded from this workspace stop"
            else:
                endpoint.error = "membership not confirmed; interrupt withheld"
        await asyncio.gather(*(interrupt_current(e) for e in self.targets() if e.in_scope))
        while True:
            affected.update((id(e), e) for e in self.targets())
            pending = [e for e in self.targets() if e.in_scope and not e.stopped()]
            elapsed = time.monotonic() - started
            if not pending:
                try:
                    await self.service.audit(notify=False)
                except Exception as exc:
                    self.coverage_error = str(exc)
                if any(e.in_scope and not e.stopped() for e in self.targets()):
                    if time.monotonic() - started < STOP_DEADLINE:
                        for endpoint in self.targets():
                            if endpoint.in_scope and endpoint.interrupt_at is None:
                                await interrupt_current(endpoint)
                        continue
                    self.phase = "failed"
                    break
                self.phase = "failed" if self.coverage_error else "stopped"
                break
            if elapsed >= STOP_DEADLINE:
                self.phase = "failed"
                break
            if elapsed >= KILL_AFTER and not kill_sent:
                kill_sent = True
                await asyncio.gather(*(e.signal(signal.SIGKILL) for e in pending))
            elif elapsed >= TERM_AFTER and not term_sent:
                term_sent = True
                await asyncio.gather(*(e.signal(signal.SIGTERM) for e in pending))
            await asyncio.sleep(.005)
        self.trip["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.stop_targets = [{"surface_id": e.sid, **e.summary()} for e in affected.values()]
        self.trip["target_count"] = sum(e.in_scope for e in affected.values())
        self.trip["within_deadline"] = self.phase == "stopped" and self.trip["elapsed_ms"] <= 1000
        self.trip["finished_at"] = time.time()
        self.service.schedule_save(self)
        self.service.publish_ownership()


class Endpoint:
    def __init__(self, pool, sid, params):
        self.pool, self.service, self.sid, self.params = pool, pool.service, sid, params
        self.session_id = self.turn_id = self.transcript = None
        self.active = self.awaiting_turn = False
        self.native = self.socket_server = self.front = None
        self.pending = {}
        self.private_ids = set()
        self.interrupt_at = self.confirmed_at = None
        self.stop_proof = self.signal_name = self.error = None
        self.native_start = None
        self.identity = None
        self.kind = "uninitialized"
        self.turn_error = None
        self.event_at = time.time()
        self.expected_session = params.get("resume_session")
        self.start_id = uuid.uuid4().hex
        self.socket_path = self.service.sockets / (hashlib.sha256((pool.wid + sid).encode()).hexdigest()[:20] + ".sock")
        self.reader_task = None
        self.in_scope = True
        self.evidence_task = None
        self.initialization = None
        self.thread_parameters = None
        self.private_waiters = {}
        self.park_requested = False
        self.park_task = None
        self.native_completed_proof = None
        self.frontend_identity = None
        self.materialize_task = None
        self.responses = ResponseStreak()

    def response_identity(self):
        return (self.pool.wid, self.sid, self.session_id,
                self.native.pid if self.native else None, self.native_start,
                tuple((self.identity or {}).get("birth", [])), self.start_id)

    def summary(self):
        return {"session_id": self.session_id, "turn_id": self.turn_id, "active": self.active,
                "pending_turn": self.awaiting_turn, "pid": self.native.pid if self.native else None,
                "process_start": self.native_start, "interrupt_requested": self.interrupt_at is not None,
                "confirmed_at": self.confirmed_at, "stop_proof": self.stop_proof,
                "signal": self.signal_name, "error": self.error, "in_scope": self.in_scope,
                "birth": self.identity.get("birth") if self.identity else None,
                "frontend_birth": self.frontend_identity.get("birth") if self.frontend_identity else None,
                "native_completed_proof": self.native_completed_proof,
                "backend_exited": bool(self.native and self.native.returncode is not None)}

    def save_binding(self):
        write_json(self.pool.directory / (self.sid + ".json"), {
            "version": VERSION, "workspace_id": self.pool.wid, "surface_id": self.sid,
            **self.summary(), "kind": self.kind, "at": self.event_at,
            "transcript": self.transcript, "endpoint": str(self.socket_path),
            "frontend_pid": self.params.get("frontend_pid"), "start_id": self.start_id,
            "guard_pid": os.getpid(), "epoch": self.pool.epoch,
            "turn_error": self.turn_error,
        })
        # Original launch options permit W/restart to recover this same native
        # session without replaying a prompt. Keep private, never include in UI.
        write_json(self.pool.directory / (self.sid + ".launch.json"), {
            **self.params, "resume_session": self.session_id or self.expected_session,
            "initialization": self.initialization, "thread_parameters": self.thread_parameters})

    async def start(self):
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self.socket_server = await asyncio.start_unix_server(self.front_connection, str(self.socket_path), limit=MAX_MESSAGE)
        os.chmod(self.socket_path, 0o600)
        await self.start_backend()

    async def start_backend(self):
        self.identity = None
        env = dict(self.params.get("environment") or os.environ)
        env.update(CMUX_WORKSPACE_ID=self.pool.wid, CMUX_SURFACE_ID=self.sid, CCC_GUARD_BACKEND="1")
        binary = self.params.get("binary") or native_binary()
        command = [binary, "app-server", "--listen", "stdio://"]
        command.extend(self.params.get("config_args", []))
        stderr = (self.pool.directory / (self.sid + ".backend.log")).open("ab")
        try:
            self.native = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=stderr, env=env,
                cwd=self.params["cwd"], start_new_session=True, limit=MAX_MESSAGE)
        finally:
            stderr.close()
        from ccc_codex_queue import codex_process_starts
        self.native_start = codex_process_starts([self.native.pid]).get(self.native.pid)
        if self.native_start is None:
            self.native.kill()
            await self.native.wait()
            raise RuntimeError("cannot pin the native backend process")
        self.identity = scope.process(self.native.pid)
        until = time.monotonic() + .2
        while self.identity is None and self.native.returncode is None and time.monotonic() < until:
            # Darwin can expose the new executable name before KERN_PROCARGS2
            # exposes its argv/environment. No client exists at this stage.
            await asyncio.sleep(.01)
            self.identity = scope.process(self.native.pid)
        if self.identity is None:
            details = {"birth": scope.birth(self.native.pid, codex=True), "returncode": self.native.returncode}
            try:
                argv, env = scope.arguments(self.native.pid)
                details.update(argv0=argv[0] if argv else None, workspace=env.get("CMUX_WORKSPACE_ID"), surface=env.get("CMUX_SURFACE_ID"))
            except Exception as exc:
                details["inspection_error"] = str(exc)
            self.native.kill()
            await self.native.wait()
            raise RuntimeError("cannot pin native backend birth and placement: " + json.dumps(details))
        self.park_requested = False
        self.reader_task = asyncio.create_task(self.read_backend())
        self.service.schedule_save(self)

    def backend_write(self, message):
        if not self.native or self.native.returncode is not None or self.native.stdin.is_closing():
            raise RuntimeError("native backend is not available")
        self.native.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())

    async def front_connection(self, reader, writer):
        front = None
        try:
            if self.front is not None and not self.front.closed:
                raise RuntimeError("this native backend already has a frontend")
            front = await WebSocket.accept(reader, writer)
            self.front = front
            while True:
                text = await front.recv()
                if text is None:
                    break
                message = json.loads(text)
                if isinstance(message, dict) and message.get("method") in TURN_INPUT | {"thread/start", "thread/resume"}:
                    # A slow workspace audit must not drop the Codex socket.
                    # That is what left B on "thread ID was not received".
                    if AUTOMATIC_POOL_STOP:
                        await self.service.audit()
                    if self.in_scope and self.materialize_task and not self.materialize_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(self.materialize_task), 2)
                        except asyncio.TimeoutError:
                            if AUTOMATIC_POOL_STOP:
                                raise
                self.front_message(message)
                if writer.transport.get_write_buffer_size() > MAX_MESSAGE:
                    raise RuntimeError("frontend output backpressure exceeded protection budget")
                await writer.drain()
        except Exception as exc:
            self.error = str(exc)
            if self.in_scope:
                self.fault("frontend_protocol_failure")
        finally:
            if front:
                front.close()
            else:
                writer.close()
            if self.front is front:
                self.front = None
            if self.in_scope and (self.active or self.awaiting_turn):
                self.fault("frontend_disconnected")

    def front_message(self, message):
        if not isinstance(message, dict):
            raise ValueError("invalid native request")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("invalid native request parameters")
        if not self.in_scope:
            # A moved surface keeps its native relay and normal multi-thread
            # behavior. B's primary-session restrictions have no authority in
            # its new workspace, including automatic title threads.
            self.backend_write(message)
            return
        if self.in_scope and method == "thread/start" and params.get("threadSource") == "thread_title":
            # Native TUI auto-titles otherwise spend another model request in
            # a hidden session for every B surface before connection succeeds.
            if "id" in message and self.front:
                self.front.write(json.dumps({"id": message["id"], "error": {
                    "code": -32001, "message": "B 接入保护期间不生成额外模型标题"}}))
            return
        if self.native and self.native.returncode is not None:
            if "id" in message and self.front:
                self.front.write(json.dumps({"id": message["id"], "error": {
                    "code": -32001, "message": "B 会话后端已停止；按 W 重新布防原会话"}}))
            return
        if method in TURN_INPUT and self.in_scope and not self.pool.open_gate():
            if "id" in message and self.front:
                self.front.write(json.dumps({"id": message["id"], "error": {
                    "code": -32001, "message": "B 工作区已停止或保护尚未就绪；按 W 重新布防"}}))
            return
        if method == "initialize":
            params = message.setdefault("params", {})
            caps = params.setdefault("capabilities", {})
            caps["experimentalApi"] = True
            excluded = set(MODEL_DELTAS) | {"thread/started", "turn/started", "turn/completed", "rawResponseItem/completed", "item/started"}
            caps["optOutNotificationMethods"] = [m for m in (caps.get("optOutNotificationMethods") or []) if m not in excluded]
            self.initialization = params.copy()
        if method == "thread/start":
            message.setdefault("params", {})["experimentalRawEvents"] = True
            if self.expected_session:
                raise RuntimeError("original session must be resumed, not replaced")
        if method == "thread/resume" and self.expected_session and params.get("threadId") != self.expected_session:
            raise RuntimeError("migration attempted to resume a different original session")
        if method in {"thread/start", "thread/resume"}:
            self.thread_parameters = dict(message["params"])
        if method in {"thread/start", "thread/resume", "turn/start", "turn/steer"} and "id" in message:
            self.pending[message["id"]] = method
        if method == "turn/start":
            self.awaiting_turn = True
            self.stop_proof = self.confirmed_at = None
        self.backend_write(message)

    async def native_call(self, method, params, timeout=15):
        identifier = "ccc-private-" + uuid.uuid4().hex
        waiter = asyncio.get_running_loop().create_future()
        self.private_waiters[identifier] = waiter
        try:
            self.backend_write({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self.private_waiters.pop(identifier, None)

    async def restore(self):
        if self.native and self.native.returncode is None:
            return
        if self.reader_task:
            await self.reader_task
        original = self.session_id or self.expected_session
        self.session_id, self.turn_id = original, None
        self.active = self.awaiting_turn = False
        self.interrupt_at = self.confirmed_at = self.stop_proof = None
        self.native_completed_proof = self.signal_name = self.error = None
        await self.start_backend()
        if self.initialization:
            await self.native_call("initialize", self.initialization)
            self.backend_write({"method": "initialized"})
            if original:
                params = dict(self.thread_parameters or {})
                for key in ("experimentalRawEvents", "ephemeral"):
                    params.pop(key, None)
                params["threadId"] = original
                params["excludeTurns"] = True  # The unchanged TUI already owns its displayed history.
                resumed = await self.native_call("thread/resume", params)
                self.set_thread(resumed["thread"])
        self.kind, self.event_at = "uninitialized", time.time()
        self.service.schedule_save(self)

    async def read_backend(self):
        try:
            while True:
                line = await self.native.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid native event")
                if message.get("id") in self.private_waiters:
                    waiter = self.private_waiters[message["id"]]
                    if not waiter.done():
                        if "error" in message:
                            waiter.set_exception(RuntimeError(str(message["error"])))
                        else:
                            waiter.set_result(message.get("result"))
                    continue
                if message.get("id") in self.private_ids:
                    self.private_ids.discard(message["id"])
                    # An interrupt RPC response is deliberately NOT stop proof.
                    continue
                self.native_message(message)
                if self.front and not self.front.closed:
                    self.front.write(json.dumps(message, separators=(",", ":")))
                if self.front and self.front.writer.transport.get_write_buffer_size() > MAX_MESSAGE:
                    raise RuntimeError("native frontend stalled")
        except Exception as exc:
            self.error = str(exc)
            self.fault("native_event_failure")
        finally:
            if self.native.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.native.wait(), .05)
            if self.native.returncode is not None:
                self.active = self.awaiting_turn = False
                self.stop_proof = self.native_completed_proof or "backend_exited"
                self.confirmed_at = time.monotonic()
                self.kind = "exited"
                self.service.schedule_save(self)
                if self.front and not self.front.closed:
                    for identifier in self.pending:
                        self.front.write(json.dumps({"id": identifier, "error": {
                            "code": -32001, "message": "B 原生后端已停止；请求未继续"}}))
                    self.pending.clear()
                if self.in_scope and self.pool.phase == "watching" and not self.park_requested:
                    self.fault("native_backend_exited")
            elif self.pool.phase == "watching":
                self.fault("native_stream_closed")

    def set_thread(self, thread):
        if not isinstance(thread, dict):
            raise ValueError("invalid native thread")
        sid = thread.get("id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("native thread identity missing")
        if (self.expected_session and sid != self.expected_session) or (self.session_id and self.session_id != sid):
            raise RuntimeError("native session identity changed inside guarded endpoint")
        first = self.session_id is None and not self.expected_session
        self.session_id = sid
        self.transcript = thread.get("path") or self.transcript
        self.event_at = time.time()
        self.service.schedule_save(self)
        if first:
            async def materialize():
                try:
                    # Paginated native reads explicitly persist empty sessions.
                    # Legacy native naming does the equivalent compatibility
                    # flush. Neither operation starts a model turn.
                    if thread.get("historyMode") == "paginated":
                        try:
                            await self.native_call("thread/read", {"threadId": sid, "includeTurns": True}, timeout=.5)
                        except RuntimeError as exc:
                            # Some native stores persist successfully but do
                            # not implement the subsequent API page listing.
                            # Verify the actual writer/file below in that case.
                            if "list_turns is not supported yet" not in str(exc):
                                raise
                    else:
                        await self.native_call("thread/name/set", {
                            "threadId": sid, "name": "B 接入探测 " + sid[:8]}, timeout=.5)
                    if not self.transcript or not Path(self.transcript).is_file():
                        raise RuntimeError("native empty-session history was not materialized")
                    from ccc_codex_queue import process_writable_files
                    from ccc_codex_goal import linked
                    path = Path(self.transcript).resolve()
                    deadline = time.monotonic() + .25
                    while True:
                        try:
                            files = await asyncio.to_thread(process_writable_files, self.native.pid, identities=True)
                            if path not in files or not linked(path, files[path]):
                                raise RuntimeError("native session history is not the live writer's file")
                            break
                        except OSError:
                            # Startup opens/closes SQLite and hook descriptors.
                            # Keep the request gate closed until one complete
                            # native file inventory can verify persistence.
                            if time.monotonic() >= deadline:
                                raise
                            await asyncio.sleep(.01)
                    with path.open() as handle:
                        metadata = json.loads(handle.readline())
                    if metadata.get("type") != "session_meta" or metadata.get("payload", {}).get("id") != sid:
                        raise RuntimeError("persisted empty-session identity mismatch")
                except Exception as exc:
                    self.error = "empty session persistence failed: " + str(exc)
                    self.fault("session_persistence_failure")
                    raise
            self.materialize_task = asyncio.create_task(materialize())
            self.materialize_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    def native_message(self, message):
        if not self.in_scope:
            return
        method, params = message.get("method"), message.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("invalid native event parameters")
        if "id" in message:
            pending = self.pending.pop(message["id"], None)
            result = message.get("result") or {}
            if not isinstance(result, dict):
                raise ValueError("invalid native result")
            if pending in {"thread/start", "thread/resume"} and isinstance(result.get("thread"), dict):
                self.set_thread(result["thread"])
            if pending == "turn/start" and "error" in message:
                self.awaiting_turn = False
                self.responses.reset_failure()
        if method == "thread/started":
            self.set_thread(params["thread"])
        if params.get("threadId") == self.session_id and method == "turn/started":
            self.turn_id = params["turn"]["id"]
            self.active, self.awaiting_turn = True, False
            self.kind, self.event_at = "task_started", time.time()
            self.turn_error = None
            self.stop_proof = self.confirmed_at = None
            self.service.schedule_save(self)
            if self.in_scope and not self.pool.ready():
                self.fault("late_internal_turn")
        evidence = (self.responses.observe(message, self.response_identity())
                    if CONNECTION_CUT_ENABLED and AUTOMATIC_POOL_STOP else None)
        if evidence and self.in_scope and self.pool.phase == "watching" and (self.evidence_task is None or self.evidence_task.done()):
            # Fence new frontend input synchronously. Membership still has to
            # prove success before any workspace interruption is authorized.
            self.pool.pending_evidence.add(self)
            self.evidence_task = asyncio.create_task(self.verify_evidence(evidence, time.monotonic()))
        if (method == "turn/completed" and params.get("threadId") == self.session_id
                and params.get("turn", {}).get("id") == self.turn_id):
            status = params["turn"].get("status")
            if status not in {"interrupted", "completed", "failed"}:
                raise ValueError("unrecognized native turn terminal status")
            self.active = self.awaiting_turn = False
            self.kind = "turn_aborted" if status == "interrupted" else "task_complete"
            self.turn_error = params["turn"].get("error")
            self.event_at = time.time()
            if self.interrupt_at is not None:
                self.confirmed_at = time.monotonic()
                self.stop_proof = "native_" + status
                self.native_completed_proof = self.stop_proof
                self.park()
            self.service.schedule_save(self)

    async def verify_evidence(self, evidence, detected):
        try:
            if await self.service.member(self.pool.wid, self.sid, fresh=True):
                if (self.responses.qualified is evidence
                        and evidence.get("identity") == self.response_identity()):
                    self.pool.trigger("three_completed_responses", self, evidence, detected=detected)
            elif (self.service.inventory is not None and time.monotonic() - self.service.inventory_at < .2
                  and self.service.inventory.get(self.sid) != self.pool.wid):
                self.in_scope = False
            else:
                self.pool.trigger("membership_unavailable", self, detected=detected)
        except Exception as exc:
            self.error = str(exc)
            self.pool.trigger("evidence_validation_failure", self, detected=detected)
        finally:
            self.pool.pending_evidence.discard(self)

    def park(self):
        # EOF after native cancellation shuts down this backend's internal
        # goals/queues too. The relay/TUI and the original transcript survive.
        if not self.park_requested and self.native and self.native.returncode is None and (not self.park_task or self.park_task.done()):
            original = self.native
            async def close_current():
                if self.materialize_task and not self.materialize_task.done():
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(asyncio.shield(self.materialize_task), .15)
                if (await self.service.member(self.pool.wid, self.sid, fresh=True, owned=True)
                        and self.native is original and original.returncode is None):
                    self.park_requested = True
                    if hasattr(original.stdin, "close"):
                        original.stdin.close()
            self.park_task = asyncio.create_task(close_current())

    def fault(self, reason):
        async def stop_current():
            if await self.service.member(self.pool.wid, self.sid, fresh=True, owned=True):
                self.pool.trigger(reason, self)
            elif self.service.inventory is not None and time.monotonic() - self.service.inventory_at < .2:
                self.in_scope = False
            else:
                self.pool.coverage_error = "membership unavailable during " + reason
                self.pool.trigger("membership_unavailable", self)
        asyncio.create_task(stop_current())

    def request_interrupt(self, now):
        self.interrupt_at = now
        if not self.active and not self.awaiting_turn:
            self.stop_proof, self.confirmed_at = "already_idle", time.monotonic()
            self.native_completed_proof = self.stop_proof
            self.park()
            return
        if self.session_id and self.turn_id:
            identifier = "ccc-guard-" + uuid.uuid4().hex
            self.private_ids.add(identifier)
            try:
                self.backend_write({"id": identifier, "method": "turn/interrupt",
                                    "params": {"threadId": self.session_id, "turnId": self.turn_id}})
            except (OSError, RuntimeError) as exc:
                self.error = str(exc)

    def stopped(self):
        if self.native and self.native.returncode is not None:
            self.active = self.awaiting_turn = False
            self.stop_proof = self.native_completed_proof or "backend_exited"
            self.confirmed_at = self.confirmed_at or time.monotonic()
            if self.native.returncode < 0:
                self.signal_name = signal.Signals(-self.native.returncode).name
        if self.identity:  # A real request-capable backend must also be parked.
            return bool(self.native and self.native.returncode is not None)
        return bool(self.stop_proof and not self.active and not self.awaiting_turn)

    async def signal(self, sig):
        if self.stopped() or not self.native or self.native.returncode is not None:
            return
        from ccc_codex_queue import process_placement_start
        target = {"workspace_id": self.pool.wid, "surface_id": self.sid}
        try:
            # Membership is checked afresh, not inferred from old inherited env.
            if not await self.service.member(self.pool.wid, self.sid, fresh=True, owned=True):
                self.error = "surface moved or membership unavailable; signal refused"
                return
            if ((self.identity and not scope.matches(self.identity))
                    or (not self.identity and process_placement_start(self.native.pid, target) != self.native_start)):
                self.error = "native process generation changed; signal refused"
                return
            self.native.send_signal(sig)
            self.signal_name = signal.Signals(sig).name
        except (OSError, RuntimeError) as exc:
            self.error = str(exc)


class LegacyEndpoint:
    """Stop-only coverage for a native process that bypassed the relay.

    It cannot establish connection success. Its appearance closes the pool's
    gate; migration is required before that pool can be armed again.
    """
    def __init__(self, pool, record):
        self.pool, self.service, self.identity = pool, pool.service, record
        self.sid = record["surface_id"]
        self.session_id = self.turn_id = None
        self.active, self.awaiting_turn, self.in_scope = True, False, True
        self.interrupt_at = self.confirmed_at = self.stop_proof = self.signal_name = None
        self.error = None

    def summary(self):
        return {**self.identity, "active": self.active, "in_scope": self.in_scope,
            "interrupt_requested": self.interrupt_at is not None,
            "stop_proof": self.stop_proof, "confirmed_at": self.confirmed_at,
            "signal": self.signal_name, "error": self.error}

    def request_interrupt(self, now):
        self.interrupt_at = now
        # Raw-mode TUI Escape first. Its ACK is not proof, and neither an
        # exited original process nor a replacement shell may receive the key.
        if self.service.cmux_client:
            async def escape():
                if await self.service.member(self.pool.wid, self.sid, fresh=True) and scope.matches(self.identity):
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.service.cmux_client._control_rpc,
                            "surface.send_key", {"workspace_id": self.pool.wid, "surface_id": self.sid, "key": "escape"}, timeout=.075)
            asyncio.create_task(escape())

    def stopped(self):
        if scope.birth(self.identity["pid"], codex=True) != self.identity["birth"]:
            self.active = False
            self.stop_proof, self.confirmed_at = "original_process_exited", self.confirmed_at or time.monotonic()
            return True
        return False

    async def signal(self, sig):
        if (not self.stopped() and await self.service.member(self.pool.wid, self.sid, fresh=True, owned=True)
                and scope.send(self.identity, sig)):
            self.signal_name = signal.Signals(sig).name


class GuardService:
    def __init__(self, config_path, *, membership=None, process_scan=None):
        self.config_path = Path(config_path).resolve()
        self.sockets = socket_dir(self.config_path)
        self.root = guard_root(self.config_path)
        self.pools = {}
        self.membership = membership
        self.inventory = None
        self.inventory_at = 0
        self.inventory_task = None
        self.control_server = None
        self.shutdown = asyncio.Event()
        self.cmux_client = None
        self.watchdog = None
        self.generation = uuid.uuid4().hex
        self.process_birth = scope.birth(os.getpid())
        self.process_scan = process_scan or (lambda: []) if membership else process_scan or scope.scan
        self.audit_task = None
        self.audit_at = 0
        self.current_records = {}
        self.dirty = set()
        self.save_task = None
        self.watchdog_required = False
        self.watchdog_birth = None
        self.guard_fault = None

    def healthy(self):
        if self.guard_fault:
            return False
        if not self.watchdog_required:
            return True
        try:
            value = read_json(self.root / "watchdog-heartbeat.json")
            return (value.get("generation") == self.generation and self.watchdog and self.watchdog.poll() is None
                    and 0 <= time.monotonic() - value.get("monotonic", 0) < .3)
        except (OSError, ValueError, TypeError):
            return False

    async def repair_watchdog(self):
        if not self.watchdog_required or self.healthy():
            return
        if any(e.in_scope and (e.active or e.awaiting_turn) for p in self.pools.values() for e in p.targets()):
            raise RuntimeError("cannot rearm until the failed monitor's native requests have stopped")
        if self.watchdog and self.watchdog.poll() is None:
            if scope.birth(self.watchdog.pid) != self.watchdog_birth:
                raise RuntimeError("watchdog process generation changed")
            self.watchdog.kill()
            await asyncio.to_thread(self.watchdog.wait, 2)
        (self.root / "watchdog-heartbeat.json").unlink(missing_ok=True)
        self.publish_ownership()
        self.start_watchdog()
        deadline = time.monotonic() + 2
        while not self.healthy() and time.monotonic() < deadline:
            await asyncio.sleep(.025)
        if not self.healthy():
            raise RuntimeError("independent watchdog could not be restored")

    def start_watchdog(self):
        self.watchdog = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("ccc_guard_watchdog.py")),
            "--config", str(self.config_path), "--parent", str(os.getpid()), "--generation", self.generation],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.watchdog_birth = scope.birth(self.watchdog.pid)

    def schedule_save(self, value):
        self.dirty.add(value)
        if self.save_task is None or self.save_task.done():
            self.save_task = asyncio.create_task(self.flush_saves())

    async def flush_saves(self):
        await asyncio.sleep(0)
        while self.dirty:
            values, self.dirty = self.dirty, set()
            for value in values:
                try:
                    await asyncio.to_thread(value.save if isinstance(value, Workspace) else value.save_binding)
                except Exception as exc:
                    self.guard_fault = "guard state persistence failed: " + str(exc)
                    pool = value if isinstance(value, Workspace) else value.pool
                    pool.trigger("state_persistence_failure")

    def publish_ownership(self):
        write_json(self.root / "ownership.json", {"guard_pid": os.getpid(), "generation": self.generation,
            "guard_birth": self.process_birth, "cmux_socket": getattr(getattr(self.cmux_client, "viewport_socket", None), "path", None),
            "workspaces": list(self.pools),
            "backends": [{**e.identity, "workspace_id": p.wid} for p in self.pools.values()
                         for e in p.targets() if e.in_scope and e.identity and not e.stopped()]})

    async def member(self, wid, sid, *, fresh=False, owned=False):
        if self.membership:
            return await self.membership(wid, sid)
        if self.inventory_task is None or self.inventory_task.done():
            if (fresh and time.monotonic() - self.inventory_at > .005) or time.monotonic() - self.inventory_at > .1:
                self.inventory_task = asyncio.create_task(self.load_inventory())
                self.inventory_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        try:
            if self.inventory_task:
                await asyncio.wait_for(asyncio.shield(self.inventory_task), .12)
        except Exception:
            return False
        return self.inventory is not None and (self.inventory.get(sid) == wid or (owned and sid not in self.inventory))

    async def load_inventory(self):
        def collect():
            import cmux_codex_watch as c
            if self.cmux_client is None:
                transport = c.CmuxViewportSocket()
                client = c.CmuxClient(viewport_socket=transport)
                transport.configure(client.capabilities())
                self.cmux_client = client
            return self.cmux_client._control_rpc("system.tree", {"all": True}, timeout=.10)
        tree = await asyncio.to_thread(collect)
        if not tree:
            raise RuntimeError("current cmux membership unavailable")
        self.current_records = scope.records(tree)
        self.inventory = {sid: row["workspace_id"] for sid, row in self.current_records.items()}
        self.inventory_at = time.monotonic()

    async def audit(self, *, notify=True):
        if self.audit_task is None or self.audit_task.done():
            self.audit_task = asyncio.create_task(self._audit(notify=notify))
            self.audit_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return await asyncio.wait_for(asyncio.shield(self.audit_task), .25)

    async def _audit(self, *, notify=True):
        if not self.membership:
            await self.member("", "", fresh=True)
            if self.inventory is None or time.monotonic() - self.inventory_at > .15:
                raise RuntimeError("fresh workspace membership unavailable")
        else:
            for pool in self.pools.values():
                for e in pool.targets():
                    await self.member(pool.wid, e.sid, fresh=True)
        rows = await asyncio.to_thread(self.process_scan)
        locations = self.inventory or {}
        config = await asyncio.to_thread(core().ConfigStore(self.config_path).load)
        for pool in self.pools.values():
            pool.coverage_error = None
            known = {}
            for e in pool.endpoints.values():
                e.in_scope = locations.get(e.sid, pool.wid) == pool.wid
                if e.native:
                    known[e.native.pid] = e
            for row in rows:
                if locations.get(row["surface_id"]) != pool.wid:
                    continue
                if row["pid"] in known and (not known[row["pid"]].identity or row["birth"] == known[row["pid"]].identity["birth"]):
                    pool.unmanaged.pop(row["pid"], None)
                    continue
                e = pool.endpoints.get(row["surface_id"])
                if (e and row.get("remote") and row.get("remote_address") == "unix://" + str(e.socket_path)
                        and e.params.get("frontend_pid") == row["pid"]):
                    if e.frontend_identity != row:
                        e.frontend_identity = row
                        self.schedule_save(e)
                    continue
                # Startup is registered before the child is spawned. The
                # register call has not yet returned, so no TUI can submit.
                if e and e.native is None and row.get("backend"):
                    continue
                if row["pid"] not in pool.unmanaged:
                    pool.unmanaged[row["pid"]] = LegacyEndpoint(pool, row)
            for pid, e in list(pool.unmanaged.items()):
                e.in_scope = locations.get(e.sid, pool.wid) == pool.wid
                if e.stopped():
                    pool.unmanaged.pop(pid)
            # A process scan can contain a short-lived fork that already
            # exec'd another program or exited by the time it is reconciled.
            # Only surviving native processes constitute a coverage gap.
            if AUTOMATIC_POOL_STOP and any(e.in_scope for e in pool.unmanaged.values()):
                pool.coverage_error = "unobserved native Codex requires session-preserving migration"
            rule = provenance(self.config_path, pool.wid, config)
            if notify and pool.phase == "watching":
                if not rule or rule.get("paused") or not rule.get("enabled", True):
                    pool.trigger("authorization_paused")
                elif pool.coverage_error:
                    pool.trigger("coverage_failure", evidence={"unmanaged": [
                        e.summary() for e in pool.unmanaged.values() if e.in_scope]})
            elif notify and pool.phase in {"stopped", "failed"} and any(e.active for e in pool.unmanaged.values() if e.in_scope):
                pool.trigger("late_unmanaged_process")
        self.audit_at = time.monotonic()
        return rows

    def pool(self, workspace_id):
        wid = uid(workspace_id)
        if not provenance(self.config_path, wid):
            raise RuntimeError("workspace has no verified B protection provenance")
        if wid not in self.pools:
            self.pools[wid] = Workspace(self, wid)
        return self.pools[wid]

    async def dispatch(self, message):
        if not isinstance(message, dict):
            raise ValueError("invalid guard control request")
        command = message.get("command")
        if command == "ping":
            return {"version": VERSION, "pid": os.getpid(), "generation": self.generation,
                    "ready": self.healthy(), "time": time.time()}
        pool = self.pool(message.get("workspace_id"))
        if command == "prepare":
            if pool.phase == "watching":
                pool.trigger("session_migration")
                await asyncio.shield(pool.stop_task)
            pool.phase = "arming"
            self.schedule_save(pool)
            return {"phase": pool.phase}
        if command == "arm":
            if not AUTOMATIC_POOL_STOP:
                if operator_paused(self.config_path, pool.wid) and not message.get("resume"):
                    raise RuntimeError("B 工作区已停止；请按 W 重新布防")
                pool.coverage_error = None
                pool.phase = "watching"
                pool.trip = None
                with contextlib.suppress(FileNotFoundError, OSError, ValueError):
                    marker = pool.directory / "STOP.json"
                    if marker.exists() and read_json(marker).get("reason") != "operator_pause":
                        marker.unlink()
                pool.save()
                self.publish_ownership()
                return {"phase": pool.phase, "epoch": pool.epoch}
            if (pool.phase in {"stopping", "stopped", "failed"} or blocked(self.config_path, pool.wid)) and not message.get("resume"):
                raise RuntimeError("B 工作区已停止；请按 W 重新布防")
            await self.audit(notify=False)
            if pool.coverage_error:
                raise RuntimeError(pool.coverage_error)
            await self.repair_watchdog()
            if not self.healthy():
                raise RuntimeError("independent protection watchdog is not ready")
            if any(e.active or e.awaiting_turn for e in pool.targets() if e.in_scope):
                if pool.phase == "watching":
                    return {"phase": pool.phase, "epoch": pool.epoch}
                raise RuntimeError("cannot rearm while native tasks are not stopped")
            pool.phase = "arming"
            permits = asyncio.Semaphore(8)
            async def restore(endpoint):
                async with permits:
                    if endpoint.in_scope and endpoint.native and endpoint.native.returncode is not None:
                        await endpoint.restore()
            await asyncio.gather(*(restore(e) for e in tuple(pool.endpoints.values())))
            if pool.phase != "arming":
                raise RuntimeError("protection tripped while restoring original native sessions")
            await self.audit(notify=False)
            if pool.coverage_error:
                raise RuntimeError(pool.coverage_error)
            pool.epoch, pool.phase, pool.trip = uuid.uuid4().hex, "watching", None
            pool.transitions = (pool.transitions + [{"reason": "armed", "monotonic": time.monotonic()}])[-16:]
            pool.stop_targets = []
            pool.stop_task = None
            with contextlib.suppress(FileNotFoundError):
                (pool.directory / "STOP.json").unlink()
            for endpoint in pool.endpoints.values():
                endpoint.interrupt_at = endpoint.confirmed_at = endpoint.stop_proof = None
            pool.save()
            self.publish_ownership()
            return {"phase": pool.phase, "epoch": pool.epoch}
        if command == "register":
            sid = uid(message.get("surface_id"))
            if not AUTOMATIC_POOL_STOP:
                if operator_paused(self.config_path, pool.wid):
                    raise RuntimeError("B 工作区已停止；不会启动额外请求")
                pool.coverage_error = None
                if pool.phase not in {"watching", "arming"}:
                    pool.phase = "watching"
                    pool.trip = None
            elif not pool.open_gate() and not (message.get("migration") and pool.phase == "arming"):
                raise RuntimeError("B 工作区已停止；不会启动额外请求")
            placed = False
            for _ in range(6):
                placed = await self.member(pool.wid, sid, fresh=True)
                if placed:
                    break
                if not AUTOMATIC_POOL_STOP and self.inventory and self.inventory.get(sid) not in {None, pool.wid}:
                    break
                await asyncio.sleep(.2)
            if not placed:
                raise RuntimeError("surface is not currently inside its authorized B workspace")
            if sid in pool.endpoints:
                existing = pool.endpoints[sid]
                if existing.params.get("frontend_pid") != message.get("frontend_pid"):
                    if existing.front is not None or (existing.frontend_identity and scope.matches(existing.frontend_identity)):
                        raise RuntimeError("surface already has a different guarded frontend")
                    existing.params["frontend_pid"] = message.get("frontend_pid")
                    await existing.restore()
                return {"endpoint": str(existing.socket_path), "pid": existing.native.pid}
            endpoint = Endpoint(pool, sid, message)
            pool.endpoints[sid] = endpoint
            try:
                await endpoint.start()
            except Exception as exc:
                endpoint.error = str(exc)
                pool.trigger("backend_start_failure", endpoint)
                raise
            pool.save()
            self.publish_ownership()
            return {"endpoint": str(endpoint.socket_path), "pid": endpoint.native.pid}
        if command == "stop":
            try:
                await self.audit(notify=False)
            except Exception as exc:
                pool.coverage_error = str(exc)
            pool.trigger(message.get("reason", "operator_pause"))
            if pool.stop_task:
                await asyncio.shield(pool.stop_task)
            if pool.pause_task:
                await asyncio.shield(pool.pause_task)
            pool.save()
            return snapshot(self.config_path, pool.wid)
        if command == "status":
            pool.save()
            return snapshot(self.config_path, pool.wid)
        raise RuntimeError("unknown B guard command")

    async def control(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            result = await self.dispatch(json.loads(line))
            reply = {"ok": True, "result": result}
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(reply) + "\n").encode())
        with contextlib.suppress(OSError):
            await writer.drain()
        writer.close()

    async def maintenance(self):
        while True:
            write_json(self.root / "heartbeat.json", {"pid": os.getpid(), "generation": self.generation,
                "birth": self.process_birth, "monotonic": time.monotonic()})
            for pool in tuple(self.pools.values()):
                if pool.phase == "watching":
                    try:
                        rule = provenance(self.config_path, pool.wid)
                        marker = read_json(pool.directory / "STOP.json")
                        if marker.get("reason") == "guardian_heartbeat_lost":
                            pool.trigger("guardian_heartbeat_lost")
                        elif not rule or rule.get("paused") or not rule.get("enabled", True) or marker:
                            pool.trigger("authorization_paused")
                        elif not self.healthy():
                            pool.trigger("independent_watchdog_lost")
                    except (OSError, ValueError, RuntimeError):
                        pool.trigger("authorization_unavailable")
            await asyncio.sleep(.05)

    async def coverage_loop(self):
        while True:
            try:
                if self.pools:
                    await self.audit()
            except Exception as exc:
                if not AUTOMATIC_POOL_STOP:
                    await asyncio.sleep(.025)
                    continue
                for pool in tuple(self.pools.values()):
                    pool.coverage_error = str(exc)
                    if pool.phase == "watching":
                        pool.trigger("coverage_unavailable")
            await asyncio.sleep(.025)

    async def run(self):
        private_directory(self.root)
        private_directory(self.sockets)
        if not self.membership:
            await self.load_inventory()  # Warm cmux transport before any request.
        address = self.sockets / "control.sock"
        with contextlib.suppress(FileNotFoundError):
            address.unlink()
        self.control_server = await asyncio.start_unix_server(self.control, str(address), limit=MAX_MESSAGE)
        os.chmod(address, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.shutdown.set)
        maintenance = asyncio.create_task(self.maintenance())
        coverage = asyncio.create_task(self.coverage_loop())
        self.publish_ownership()
        write_json(self.root / "heartbeat.json", {"pid": os.getpid(), "generation": self.generation,
            "birth": self.process_birth, "monotonic": time.monotonic()})
        self.start_watchdog()
        self.watchdog_required = True
        try:
            await self.shutdown.wait()
        finally:
            self.control_server.close()
            for pool in self.pools.values():
                pool.trigger("guard_shutdown")
            await asyncio.gather(*(p.stop_task for p in self.pools.values() if p.stop_task), return_exceptions=True)
            # Keep the relay alive for any surface that has moved to another
            # workspace. It is outside this stop's scope, including shutdown.
            while any(e.native and e.native.returncode is None and not e.in_scope
                      for p in self.pools.values() for e in p.endpoints.values()):
                await asyncio.sleep(.1)
            maintenance.cancel()
            coverage.cancel()
            if self.save_task:
                await self.save_task
            if self.watchdog:
                self.watchdog.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(self.watchdog.wait, 2)
            address.unlink(missing_ok=True)


def launch(config_path, *, job_id=None, index=None, resume_session=None, config_args=None,
           record_path=None, cli_args=None):
    wid, sid = uid(os.environ.get("CMUX_WORKSPACE_ID")), uid(os.environ.get("CMUX_SURFACE_ID"))
    record = read_json(record_path) if record_path else {}
    if record and (record.get("workspace_id") != wid or record.get("surface_id") != sid):
        raise RuntimeError("migration record does not match this original surface")
    if not provenance(config_path, wid):
        raise RuntimeError("此 B 工作区的实时保护未就绪；未启动 Codex")
    # Only an operator pause still refuses the original Codex launch.
    refused = operator_paused(config_path, wid) if not AUTOMATIC_POOL_STOP else blocked(config_path, wid)
    if refused and not record:
        raise RuntimeError("此 B 工作区的实时保护未就绪；未启动 Codex")
    args = list(record.get("config_args", config_args or []))
    resume_session = record.get("resume_session", resume_session)
    environment = dict(record.get("environment") or os.environ)
    environment.update(CMUX_WORKSPACE_ID=wid, CMUX_SURFACE_ID=sid)
    frontend_args = list(record.get("frontend_args", []))
    if record:
        os.chdir(record["cwd"])
    for n, arg in enumerate(args):
        if arg.startswith("mcp_servers.cmux-cua.env.CMUX_CUA_STATE_OWNER_PID="):
            args[n] = 'mcp_servers.cmux-cua.env.CMUX_CUA_STATE_OWNER_PID=' + json.dumps(str(os.getpid()))
    if job_id:
        from ccc_workspace_batch import job_path, sqlite_home
        job = read_json(job_path(config_path, job_id))
        if uid(job.get("workspace_id")) != wid or job["slots"][index].get("surface_id", sid).upper() != sid:
            raise RuntimeError("batch launch identity mismatch")
        args += ["-c", "sqlite_home=" + json.dumps(str(sqlite_home(config_path, job_id, index).resolve()))]
    ensure_service(config_path)
    response = request(config_path, "register", timeout=10, workspace_id=wid, surface_id=sid,
        frontend_pid=os.getpid(), cwd=os.getcwd(), environment=environment, config_args=args,
        frontend_args=frontend_args, resume_session=resume_session, migration=bool(record))
    native = [native_binary(), "--remote", "unix://" + response["endpoint"], *(cli_args if cli_args is not None else args + frontend_args)]
    if resume_session and cli_args is None:
        native += ["resume", resume_session]
    os.execve(native[0], native, environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["serve", "launch", "status", "arm", "stop"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--job")
    parser.add_argument("--index", type=int)
    parser.add_argument("--resume-session")
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    if args.action == "serve":
        with core().FileLock(guard_root(args.config) / "service.lock", timeout_sec=0):
            asyncio.run(GuardService(args.config).run())
    elif args.action == "launch":
        launch(args.config, job_id=args.job, index=args.index, resume_session=args.resume_session, record_path=args.record)
    elif args.action == "arm":
        print(json.dumps(arm(args.config, args.workspace)))
    else:
        print(json.dumps(request(args.config, args.action, workspace_id=args.workspace), ensure_ascii=False))


if __name__ == "__main__":
    main()
