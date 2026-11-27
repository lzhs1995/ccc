"""One request repair of an original Codex SID after a failed IEC compact.

The native host replaces the original terminal surface directly. A durable,
one-use launcher preserves the original argv/environment/cwd and starts the
same SID with an explicit continuation prompt. No shell paste or signals.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid

LIMIT = 4 * 1024 * 1024
ACTIVE = frozenset({"captured", "respawn_intent", "respawn_unknown",
                    "launch_intent", "bound", "running"})
FINAL = frozenset({"done", "failed", "held", "cancelled", "superseded"})
TRANSITIONS = {
    "captured": {"respawn_intent", "held", "cancelled", "superseded"},
    "respawn_intent": {"respawn_unknown", "launch_intent", "held", "cancelled", "superseded"},
    "respawn_unknown": {"launch_intent", "bound", "held", "cancelled", "superseded"},
    "launch_intent": {"bound", "held", "cancelled", "superseded"},
    "bound": {"running", "held", "failed", "cancelled", "superseded"},
    "running": {"done", "failed", "held", "cancelled", "superseded"},
}
UPDATE_FIELDS = frozenset({"phase", "reason", "input_operations", "respawn_at",
    "respawn_ack", "respawn_error", "exec_claim", "launch_pid", "launch_birth", "exec_attempt_at"})
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
LAUNCH_DEPENDENCIES = ("ccc_guard_scope.py", "ccc_codex_queue.py",
                       "ccc_encrypted_recovery.py", "ccc_provider_retry.py")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def read_private(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > LIMIT or info.st_mode & 0o022):
            raise ValueError("unsafe request recovery file")
        raw = stream.read(LIMIT + 1)
        if _file_identity(os.fstat(stream.fileno())) != _file_identity(info) or len(raw) != info.st_size:
            raise ValueError("request recovery file changed during read")
    if len(raw) > LIMIT:
        raise ValueError("oversized request recovery file")
    return json.loads(raw)


def atomic(path, value, mode=0o600):
    path = Path(path)
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def file_pin(path):
    path = Path(path).absolute()
    resolved = path.resolve(strict=True)
    link, before = path.lstat(), resolved.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("recovery file is not regular")
    sha = hashlib.sha256()
    with resolved.open("rb") as stream:
        if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
            raise ValueError("recovery file changed before read")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
        if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
            raise ValueError("recovery file changed during read")
    return {"path": str(path), "resolved": str(resolved),
            "link": [link.st_dev, link.st_ino, link.st_mtime_ns, link.st_ctime_ns],
            "identity": [before.st_dev, before.st_ino, before.st_size,
                         before.st_mtime_ns, before.st_ctime_ns],
            "sha256": sha.hexdigest()}


def _file_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_matches(pin, *, content=False):
    try:
        path = Path(pin["path"])
        resolved = path.resolve(strict=True)
        link, info = path.lstat(), resolved.stat()
        return (str(resolved) == pin["resolved"]
                and [link.st_dev, link.st_ino, link.st_mtime_ns, link.st_ctime_ns] == pin["link"]
                and [info.st_dev, info.st_ino, info.st_size,
                     info.st_mtime_ns, info.st_ctime_ns] == pin["identity"]
                and (not content or file_pin(path)["sha256"] == pin["sha256"]))
    except (OSError, ValueError, KeyError):
        return False


def executable_path(pid):
    import ccc_codex_queue as native
    if native._proc_pidpath is None:
        raise ValueError("native executable inspection unavailable")
    buffer = ctypes.create_string_buffer(4096)
    count = native._proc_pidpath(pid, buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise ValueError("native executable path unavailable")
    return str(Path(buffer.value.decode("utf-8")).resolve(strict=True))


def rollout_pin(path, turn, record):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        header = json.loads(stream.readline(1024 * 1024))
        signature = turn.get("signature", [])
        if (header.get("type") != "session_meta"
                or header.get("payload", {}).get("id") != turn["session_id"]
                or [info.st_dev, info.st_ino] != record["file_id"]
                or len(signature) < 2 or signature[0] != info.st_ino
                or signature[1] != info.st_size or record["cursor"] != info.st_size
                or str(path) != str(Path(record["path"]).resolve(strict=True))):
            raise ValueError("failed compact native EOF changed")
        start = max(0, info.st_size - 4096)
        stream.seek(start)
        tail = stream.read()
        if (not tail.endswith(b"\n")
                or _file_identity(os.fstat(stream.fileno())) != _file_identity(info)):
            raise ValueError("failed compact EOF not stable")
    return {"path": str(path), "file_id": [info.st_dev, info.st_ino],
            "session_id": turn["session_id"], "fence": info.st_size,
            "boundary_start": start, "boundary_sha256": hashlib.sha256(tail).hexdigest()}


def rollout_matches(pin, *, exact=True):
    try:
        with Path(pin["path"]).open("rb") as stream:
            info = os.fstat(stream.fileno())
            if ([info.st_dev, info.st_ino] != pin["file_id"]
                    or info.st_size < pin["fence"] or exact and info.st_size != pin["fence"]):
                return False
            header = json.loads(stream.readline(1024 * 1024))
            if (header.get("type") != "session_meta"
                    or header.get("payload", {}).get("id") != pin["session_id"]):
                return False
            stream.seek(pin["boundary_start"])
            boundary = stream.read(pin["fence"] - pin["boundary_start"])
            return (hashlib.sha256(boundary).hexdigest() == pin["boundary_sha256"]
                    and os.fstat(stream.fileno()).st_size == info.st_size)
    except (OSError, ValueError, KeyError):
        return False


def process_absent(pid):
    # Signal 0 queries existence only. A failed libproc read never proves exit.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class RequestRecoveryStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock

    @staticmethod
    def key(identity):
        return digest(identity)

    def directory(self, surface, key=None):
        surface = str(uuid.UUID(surface)).upper()
        if key is not None and not KEY_RE.fullmatch(key):
            raise ValueError("invalid request repair key")
        return self.path / surface / key if key else self.path / surface

    @contextmanager
    def lock(self, surface, key=None):
        directory = self.directory(surface, key)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(fd)

    def _records_locked(self, surface):
        directory = self.directory(surface)
        keys = [p.name for p in directory.iterdir() if KEY_RE.fullmatch(p.name)]
        if len(keys) > 256:
            raise ValueError("request recovery history limit; diagnosis required")
        records = []
        for key in keys:
            if (directory / key / "state.json").exists():
                records.append(self.get(surface, key))
        return records

    def _latest_locked(self, surface):
        index = self.directory(surface) / "current.json"
        # state.json is authoritative. Repair an absent or older index after
        # a crash between the two fsynced publications, under the surface lock.
        records = self._records_locked(surface)
        if not records:
            return None
        latest = max(records, key=lambda pair: (pair[0]["created_at"], pair[0]["key"]))
        if not index.exists() or read_private(index).get("key") != latest[0]["key"]:
            atomic(index, {"key": latest[0]["key"]})
        return latest

    def latest(self, surface):
        if not self.directory(surface).exists():
            return None
        with self.lock(surface):
            return self._latest_locked(surface)

    def get(self, surface, key):
        directory = self.directory(surface, key)
        state = read_private(directory / "state.json")
        plan = read_private(directory / "plan.json")
        if state.get("plan_sha256") != digest(plan) or state.get("key") != key:
            raise ValueError("request repair plan identity changed")
        return state, plan

    def stage(self, plan):
        identity = plan["identity"]
        key, surface = self.key(identity), identity["surface_id"]
        with self.lock(surface) as surface_dir:
            self._latest_locked(surface)
            for prior in self._records_locked(surface):
                if prior[0]["key"] == key:
                    return prior, False
                if (prior[1]["identity"]["session_id"] == identity["session_id"]
                        and prior[0]["phase"] in ACTIVE):
                    raise ValueError("original request repair still pending")
            with self.lock(surface, key) as directory:
                if (directory / "state.json").exists():
                    return self.get(surface, key), False
                state = {"key": key, "phase": "captured", "plan_sha256": digest(plan),
                         "created_at": self.clock(), "cursor": plan["rollout"]["fence"],
                         "input_operations": 0, "replay_authorized": False}
                atomic(directory / "plan.json", plan, 0o400)
                atomic(directory / "state.json", state)
                atomic(surface_dir / "current.json", {"key": key})
                return (state, plan), True

    def update(self, surface, key, *, expect=None, **changes):
        with self.lock(surface, key) as directory:
            state, plan = self.get(surface, key)
            if set(changes) - UPDATE_FIELDS:
                raise ValueError("request recovery immutable field update refused")
            if expect is not None and state["phase"] not in expect:
                raise ValueError("request recovery phase changed")
            phase = changes.get("phase", state["phase"])
            if phase != state["phase"] and (expect is None or phase not in TRANSITIONS.get(state["phase"], set())):
                raise ValueError("request recovery phase cannot be rearmed")
            if "input_operations" in changes and not (
                    state["phase"] == "captured" and phase == "respawn_intent"
                    and state["input_operations"] == 0 and changes["input_operations"] == 1):
                raise ValueError("request recovery input claim is monotonic")
            if "exec_claim" in changes and (state.get("exec_claim") or phase != "launch_intent"
                    or state["phase"] not in {"respawn_intent", "respawn_unknown"}):
                raise ValueError("request recovery exec claim already consumed")
            if set(changes) & {"launch_pid", "launch_birth", "exec_attempt_at"} and (
                    state.get("exec_claim") or not changes.get("exec_claim")
                    or phase != "launch_intent"):
                raise ValueError("request recovery launch identity is immutable")
            if changes.get("exec_claim") and (
                    changes.get("launch_pid") != changes["exec_claim"].get("pid")
                    or changes.get("launch_birth") != changes["exec_claim"].get("birth")):
                raise ValueError("request recovery exec claim identity mismatch")
            state.update(changes, updated_at=self.clock())
            atomic(directory / "state.json", state)
            return state, plan

    def observe(self, surface, key, *, binding=None):
        with self.lock(surface, key) as directory:
            state, plan = self.get(surface, key)
            if state["phase"] in FINAL or not binding_matches(state, plan, binding):
                return state, plan
            if state["phase"] in {"launch_intent", "respawn_unknown"}:
                state.update(phase="bound", bound_at=self.clock())
            if state["phase"] not in {"bound", "running"}:
                return state, plan
            try:
                stream = Path(plan["rollout"]["path"]).open("rb")
            except OSError:
                state.update(phase="held", reason="original native file unavailable")
                atomic(directory / "state.json", state)
                return state, plan
            with stream:
                # Boundary validation and event consumption use the same open
                # descriptor; a path replacement between two opens is not proof.
                if (not _stream_matches(stream, plan["rollout"], exact=False)
                        or not _evidence_matches(stream, state, plan["rollout"])):
                    state.update(phase="held", reason="original rollout changed")
                    atomic(directory / "state.json", state)
                    return state, plan
                stream.seek(state["cursor"])
                consumed = 0
                while consumed < LIMIT:
                    offset = stream.tell()
                    raw = stream.readline(LIMIT - consumed + 1)
                    if not raw or not raw.endswith(b"\n") or len(raw) > LIMIT - consumed:
                        break
                    consumed += len(raw)
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        state.update(phase="held", reason="malformed native recovery record")
                        break
                    if not isinstance(event, dict):
                        state.update(phase="held", reason="invalid native recovery record")
                        break
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        state.update(phase="held", reason="invalid native recovery payload")
                        break
                    kind = payload.get("type")
                    outer = event.get("type")
                    proof = {"path": plan["rollout"]["path"], "file_id": plan["rollout"]["file_id"],
                             "offset": offset, "length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                    if event.get("type") == "response_item" and kind == "message" and payload.get("role") == "user":
                        content = payload.get("content")
                        if (not isinstance(content, list) or any(not isinstance(item, dict)
                                or not isinstance(item.get("text", ""), str) for item in content)):
                            state.update(phase="held", reason="invalid native recovery user message")
                            break
                        text = "\n".join(item.get("text", "") for item in payload.get("content", [])
                                         if isinstance(item, dict))
                        if text == plan["prompt"]:
                            if not state.get("native_receipt"):
                                state.update(phase="running", native_receipt=proof)
                                if state.get("candidate_turn"):
                                    state["started_turn"] = state["candidate_turn"]
                        else:
                            from ccc_encrypted_recovery import _native_goal_context
                            if _native_goal_context(payload):
                                state['pending_goal_message'] = {
                                    'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                                    'proof': proof}
                            else:
                                state.update(phase="superseded", reason="later native user input preserved")
                    elif outer == "event_msg" and kind == "user_message":
                        # This event is advisory; only response_item proves receipt.
                        text = str(payload.get("message") or "")
                        passive = state.pop('pending_goal_message', None)
                        paired_goal = (isinstance(passive, dict)
                            and hashlib.sha256(text.encode('utf-8')).hexdigest() == passive.get('text_sha256'))
                        if text != plan["prompt"] and not paired_goal:
                            state.update(phase="superseded", reason="later native user input preserved")
                    elif event.get("type") == "event_msg" and kind == "turn_aborted":
                        state.update(phase="cancelled", reason="native user abort preserved")
                    elif outer == "event_msg" and kind == "task_started":
                        turn = payload.get("turn_id")
                        if not isinstance(turn, str) or not turn:
                            state.update(phase="held", reason="native start lacks turn identity")
                        elif state.get("candidate_turn") and turn != state["candidate_turn"]:
                            state.update(phase="superseded", reason="another native turn preserved")
                        else:
                            state["candidate_turn"] = turn
                            state["start_proof"] = proof
                            if state.get("native_receipt"):
                                state["started_turn"] = turn
                    elif (outer == "event_msg" and kind in {"exec_command_begin", "mcp_tool_call_begin"}):
                        # Begin alone is not success, nor an executed tool result.
                        if _owned_event(state, payload):
                            call = payload.get("call_id")
                            if isinstance(call, str) and call:
                                state.setdefault("pending_native_calls", {})[call] = proof
                    elif (state.get("started_turn") and event.get("type") == "response_item"
                          and kind in {"function_call", "custom_tool_call"} and _owned_event(state, payload)):
                        call = payload.get("call_id")
                        if isinstance(call, str):
                            state.setdefault("pending_tool_calls", {})[call] = proof
                    elif (state.get("started_turn") and event.get("type") == "response_item"
                          and kind in {"function_call_output", "custom_tool_call_output"}
                          and _owned_event(state, payload)
                          and payload.get("call_id") in state.get("pending_tool_calls", {})):
                        state["tool_progress"] = {"begin": state["pending_tool_calls"][payload["call_id"]],
                                                   "output": proof, "turn_id": state["started_turn"]}
                    elif (outer == "event_msg" and kind in {"exec_command_end", "mcp_tool_call_end"}
                          and _owned_event(state, payload)
                          and payload.get("call_id") in state.get("pending_native_calls", {})):
                        state["tool_progress"] = {"begin": state["pending_native_calls"][payload["call_id"]],
                                                   "output": proof, "turn_id": state["started_turn"]}
                    elif (state.get("started_turn") and event.get("type") == "event_msg"
                          and kind == "task_complete" and payload.get("turn_id") == state["started_turn"]):
                        state.update(phase="failed" if payload.get("error") else "done",
                                     completion=proof, completion_error=payload.get("error"))
                    state["cursor"] = stream.tell()
                    if state["phase"] in FINAL:
                        break
                final = os.fstat(stream.fileno())
                if (final.st_size < state["cursor"]
                        or not _path_identity_matches(plan["rollout"])
                        or not _stream_matches(stream, plan["rollout"], exact=False)
                        or not _evidence_matches(stream, state, plan["rollout"])):
                    state.update(phase="held", reason="native rollout identity changed")
            atomic(directory / "state.json", state)
            return state, plan


def _owned_event(state, payload):
    return bool(state.get("native_receipt") and state.get("started_turn")
                and state.get("candidate_turn") == state["started_turn"]
                and (not payload.get("turn_id") or payload["turn_id"] == state["started_turn"]))


def _stream_matches(stream, pin, *, exact=True):
    try:
        info = os.fstat(stream.fileno())
        if ([info.st_dev, info.st_ino] != pin["file_id"] or info.st_size < pin["fence"]
                or exact and info.st_size != pin["fence"]):
            return False
        stream.seek(0)
        header = json.loads(stream.readline(1024 * 1024))
        if (not isinstance(header, dict) or header.get("type") != "session_meta"
                or not isinstance(header.get("payload"), dict)
                or header["payload"].get("id") != pin["session_id"]):
            return False
        stream.seek(pin["boundary_start"])
        return hashlib.sha256(stream.read(pin["fence"] - pin["boundary_start"])).hexdigest() == pin["boundary_sha256"]
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _path_identity_matches(pin):
    try:
        current = Path(pin["path"]).stat()
        return [current.st_dev, current.st_ino] == pin["file_id"]
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _evidence_matches(stream, state, pin):
    proofs = [state.get(k) for k in ("native_receipt", "start_proof", "completion")]
    progress = state.get("tool_progress") or {}
    proofs.extend(progress.get(k) for k in ("begin", "output"))
    proofs.append((state.get('pending_goal_message') or {}).get('proof'))
    try:
        size = os.fstat(stream.fileno()).st_size
        for proof in proofs:
            if proof is None:
                continue
            if (proof["file_id"] != pin["file_id"] or proof["path"] != pin["path"]
                    or proof["offset"] < pin["fence"] or proof["length"] < 1
                    or proof["offset"] + proof["length"] > size):
                return False
            stream.seek(proof["offset"])
            if hashlib.sha256(stream.read(proof["length"])).hexdigest() != proof["sha256"]:
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def binding_matches(state, plan, binding):
    if not isinstance(binding, dict) or not state.get("exec_claim"):
        return False
    identity = plan["identity"]
    return (all(binding.get(k) == identity[k] for k in ("surface_id", "workspace_id", "session_id"))
            and binding.get("pid") == state["exec_claim"].get("pid") == state.get("launch_pid")
            and binding.get("birth") == state["exec_claim"].get("birth") == state.get("launch_birth")
            and binding.get("executable") == plan["executable"]
            and binding.get("transcript_path") == plan["rollout"]["path"])


def _launch_dependencies(plan):
    if plan.get('recovery_kind') == 'native_client_panic':
        return LAUNCH_DEPENDENCIES + ('ccc_client_panic.py', 'ccc_panic_recovery.py')
    return LAUNCH_DEPENDENCIES


def dependency_pins(*, recovery_kind=None):
    directory = Path(__file__).resolve().parent
    return [file_pin(directory / name) for name in _launch_dependencies({'recovery_kind': recovery_kind})]


def verify_dependencies(plan):
    directory = Path(__file__).resolve().parent
    pins = plan.get('dependency_pins')
    names = _launch_dependencies(plan)
    if (plan.get('launcher_pin', {}).get('resolved') != str(Path(__file__).resolve())
            or not isinstance(pins, list) or len(pins) != len(names)
            or {Path(pin['path']).name for pin in pins} != set(names)
            or any(Path(pin['path']).parent != directory or not file_matches(pin, content=True)
                   for pin in pins)):
        raise ValueError('original launcher dependencies changed')
    if str(directory) not in sys.path:
        # The one-use launcher uses isolated Python; local imports are enabled
        # only after verifying every module in their complete import closure.
        sys.path.insert(0, str(directory))


def verify_bootstrap(plan):
    """Verify every executable/import source before replacing the old PTY."""
    if (not all(file_matches(plan[name], content=True)
                for name in ('launcher_pin', 'python_pin'))
            or plan['python_pin']['resolved'] != str(Path(sys.executable).resolve())):
        raise ValueError('original launcher or Python changed')
    verify_dependencies(plan)


def launch(store, surface, key, plan_sha256, *, env=None, birth=None,
           absent=process_absent, verify_route=None, execve=os.execve):
    """Claim one exec attempt before execve; all uncertainties retain that claim."""
    env = os.environ if env is None else env
    state, plan = store.get(surface, key)
    identity = plan["identity"]
    if (state["phase"] not in {"respawn_intent", "respawn_unknown"}
            or state.get("exec_claim") or state["input_operations"] != 1
            or state["plan_sha256"] != plan_sha256
            or env.get("CMUX_SURFACE_ID", "").upper() != identity["surface_id"]
            or env.get("CMUX_WORKSPACE_ID", "").upper() != identity["workspace_id"]):
        raise ValueError("launcher does not own original surface intent")
    try:
        verify_bootstrap(plan)
        if birth is None:
            import ccc_guard_scope as scope
            birth = scope.birth
        return _launch_verified(store, surface, key, plan, env=env, birth=birth,
            absent=absent, verify_route=verify_route, execve=execve)
    except BaseException as exc:
        fresh, _ = store.get(surface, key)
        if fresh['phase'] in {'respawn_intent', 'respawn_unknown'} and not fresh.get('exec_claim'):
            store.update(surface, key, expect={fresh['phase']}, phase='held',
                reason='original launcher preflight failed: ' + type(exc).__name__)
        raise


def _launch_verified(store, surface, key, plan, *, env, birth, absent, verify_route, execve):
    identity = plan['identity']
    original = plan["original_process"]
    deadline = time.monotonic() + 3
    while (birth(original["pid"]) is not None or not absent(original["pid"])) and time.monotonic() < deadline:
        time.sleep(0.05)
    if birth(original["pid"]) is not None or not absent(original["pid"]):
        raise ValueError("original process still exists; no duplicate launch")
    if (not rollout_matches(plan["rollout"]) or not file_matches(plan["executable_pin"], content=True)
            or plan["executable"] != plan["executable_pin"]["resolved"]
            or plan["argv"][0] != plan["argv0_pin"]["path"]
            or not file_matches(plan["argv0_pin"], content=True)
            or not all(file_matches(pin, content=True) for pin in plan.get("config_pins", []))
            or not all(file_matches(plan[name], content=True) for name in ("launcher_pin", "python_pin"))
            or any(Path(path).exists() or Path(path).is_symlink() for path in plan.get("absent_config_paths", []))
            or not os.access(plan["executable"], os.X_OK)
            or not Path(plan["cwd"]).is_dir()
            or not callable(verify_route) or not verify_route(plan["route"])):
        raise ValueError("original launch source, configuration or repair route changed")
    if (plan["argv"][-3:] != ["resume", identity["session_id"], plan["prompt"]]
            or plan["argv"].count("resume") != 1):
        raise ValueError("launcher requires exactly one original SID and initial prompt")
    pid = os.getpid()
    generation = birth(pid)
    if not generation:
        raise ValueError("launcher native birth unavailable")
    store.update(surface, key, expect={"respawn_intent", "respawn_unknown"}, phase="launch_intent",
                 exec_claim={"pid": pid, "birth": generation, "nonce": uuid.uuid4().hex},
                 launch_pid=pid, launch_birth=generation, exec_attempt_at=store.clock())
    # No exception or missing host response makes this phase launchable again.
    try:
        os.chdir(plan["cwd"])
        execve(plan["executable"], plan["argv"], plan["env"])
    except BaseException as exc:
        store.update(surface, key, expect={"launch_intent"}, phase="held",
                     reason="original exec failed: " + type(exc).__name__)
        raise


def route_matches(route):
    if route.get('kind') == 'original_client_launch_configuration':
        from ccc_panic_recovery import original_route_matches
        return original_route_matches(route)
    import ccc_guard_scope as scope
    try:
        if (route.get("port") != 18790 or route.get("upstream") != "https://anyrouter.top"
                or route.get("endpoint") != "http://127.0.0.1:18790/v1"
                or not file_matches(route["approval_pin"], content=True)):
            return False
        approval = read_private(route["approval_pin"]["path"])
        argv, environment = scope.arguments(route["pid"])
        if (scope.birth(route["pid"]) != route["birth"]
                or digest(argv) != route["argv_sha256"]
                or digest(environment) != route["environment_sha256"]
                or route["source_pin"]["sha256"] != approval["source_sha256"]
                or approval.get("pid") != route["pid"] or approval.get("port") != 18790
                or approval.get("upstream") != "https://anyrouter.top"
                or not file_matches(route["source_pin"], content=True)):
            return False
        if (environment.get('SANITIZER_UPSTREAM') != route['upstream']
                or environment.get('SANITIZER_PORT') != '18790'):
            return False
        listener = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(route['pid']),
            '-iTCP:18790', '-sTCP:LISTEN', '-Fpn'], capture_output=True, text=True, timeout=1)
        if (listener.returncode != 0 or listener.stdout.splitlines().count('p' + str(route['pid'])) != 1
                or listener.stdout.splitlines().count('n127.0.0.1:18790') != 1
                or scope.birth(route['pid']) != route['birth']):
            return False
        return True
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        return False


def capture_plan(record, turn, target, *, route_file, message):
    """Join a failed native compact, original process, configuration and route.

    The caller supplies the authentic current-turn reader result and rechecks
    native/UI/workspace gates inside the admitted input connection.
    """
    import ccc_guard_scope as scope
    import ccc_iec_plan as planner
    from ccc_encrypted_recovery import same_session
    current = dict(turn, surface_id=str(target["surface_id"]), workspace_id=str(target["workspace_id"]))
    if not same_session(record, current):
        raise ValueError("failed compact no longer belongs to the original process")
    capture = scope.process(current["pid"], launch=True)
    if not capture:
        raise ValueError("original native launch capture unavailable")
    pin = rollout_pin(record["path"], current, record)
    executable = executable_path(current["pid"])
    argv0 = file_pin(capture["argv"][0])
    native_pin = file_pin(executable)
    if argv0["resolved"] != executable:
        raise ValueError("original argv0 does not bind the native executable")
    provider, config_pins, absent_configs = planner.resolve_provider(capture)
    route = read_private(route_file)
    if not route_matches(route):
        raise ValueError("approved original IEC proxy no longer authenticated")
    eof = {"authenticated": True, "path": record["path"], "file_id": pin["file_id"]}
    launch_plan = planner.build_plan(capture, record, current, current, eof,
        provider, route["endpoint"], {"file_id": native_pin["identity"][:2], "sha256": native_pin["sha256"]}, {})
    identity = dict(record["identity"], failed_compact_turn=record["compact_turn"])
    prompt = str(message) + " CCC_IEC_REQUEST_" + digest(identity)[:16]
    launch_plan["argv"].append(prompt)
    launch_plan.update(identity=identity, prompt=prompt, rollout=pin,
        original_capture_sha256=digest(capture),
        original_compact_key=record["key"], expected_turn=current,
        original_process={key: capture[key] for key in ("pid", "birth", "process_start")},
        executable_pin=native_pin, argv0_pin=argv0, config_pins=config_pins,
        absent_config_paths=absent_configs, route=route, provider=provider,
        launcher_pin=file_pin(__file__), python_pin=file_pin(sys.executable),
        dependency_pins=dependency_pins())
    launch_plan["env"] = dict(capture["environment"])
    if scope.process(current["pid"], launch=True) != capture or not rollout_matches(pin):
        raise ValueError("original native identity or EOF changed during capture")
    return launch_plan


def launcher_command(store, state, plan):
    return "exec " + shlex.join([plan["python_pin"]["resolved"], "-IB", plan["launcher_pin"]["resolved"],
        "launch", str(store.path.absolute()), plan["identity"]["surface_id"], state["key"], state["plan_sha256"]])


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "launch":
        raise SystemExit("usage: ccc_request_recovery.py launch STORE SURFACE KEY PLAN_SHA256")
    store = RequestRecoveryStore(sys.argv[2])
    launch(store, sys.argv[3], sys.argv[4], sys.argv[5], verify_route=route_matches)
