"""Read the original inner TUI's native compaction ingress.

The wire format is the frozen native/client-foreground candidate, not the old
v4 app-server transport. No control, enrollment, launcher, text input, Goal
write, configuration change, legacy fallback or background loop exists here.
The original guard supplies its kernel/private-peer panel recheck; this port
does not replace that controller or infer a selected thread from metadata.
"""
from __future__ import annotations

from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time
import uuid


METHOD = "compaction/recovery/query"
PROTECTED_SURFACE = PUBLIC_PROTECTED_SURFACE
MAX_BYTES = 65536
MAX_TIMEOUT = 14.0


class NativeQueryUnavailable(ValueError):
    """Read-only refusal. It grants no terminal or native control fallback."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise NativeQueryUnavailable("canonical native UUID required")
    return value.lower()


def _birth(value):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(v) is not int for v in value)
            or value[0] <= 0 or not 0 <= value[1] < 1_000_000):
        raise NativeQueryUnavailable("exact native process birth required")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeQueryUnavailable("duplicate native JSON field")
        result[key] = value
    return result


def decode(raw):
    def invalid_constant(_value):
        raise NativeQueryUnavailable("non-finite native JSON value")
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_constant=invalid_constant)
    except (UnicodeError, ValueError) as error:
        raise NativeQueryUnavailable("invalid native response") from error
    if not isinstance(value, dict):
        raise NativeQueryUnavailable("native response must be an object")
    return value


def _private_directory(path, *, allow_readable_home=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise NativeQueryUnavailable("native namespace must be canonical")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        value = os.fstat(fd)
        after = path.lstat()
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid()
                or value.st_mode & (0o022 if allow_readable_home else 0o077)
                or (value.st_dev, value.st_ino) != (after.st_dev, after.st_ino)):
            raise NativeQueryUnavailable("native namespace is not private and stable")
        return value.st_dev, value.st_ino
    finally:
        os.close(fd)


def _private_file(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise NativeQueryUnavailable("native capability must be canonical")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o777 != 0o600 or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_BYTES):
            raise NativeQueryUnavailable("invalid private native capability")
        chunks, remaining = [], before.st_size + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if (len(raw) != before.st_size or identity(before) != identity(after)
                or identity(after) != identity(named)):
            raise NativeQueryUnavailable("native capability changed during read")
        return (identity(before), hashlib.sha256(raw).hexdigest()), raw
    finally:
        os.close(fd)


def _peer(stream):
    if sys.platform != "darwin":
        raise NativeQueryUnavailable("native Darwin peer authentication unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    getpeereid = libc.getpeereid
    getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if getpeereid(stream.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise NativeQueryUnavailable("native peer UID unavailable")
    raw = stream.getsockopt(0, 2, struct.calcsize("i"))  # LOCAL_PEERPID
    pid = struct.unpack("i", raw)[0]
    if pid <= 1 or uid.value != os.geteuid():
        raise NativeQueryUnavailable("foreign or unidentified native peer")
    return pid


def _kernel_birth(pid):
    from ccc_guard_scope import birth
    return birth(pid)


def inner_control_policy(config, scope, sid):
    """Preserve outer isolation and every current explicit pause/exclusion.

    Passing is only a policy prerequisite. Native authority, original
    enrollment, actual input/Goal/ledger checks and atomic control are separate.
    This function never edits or projects the old native v4 configuration.
    """
    surface, workspace = scope["surface_id"], scope["workspace_id"]
    if (surface.upper() == PROTECTED_SURFACE or config.get("global_paused", False) is not False
            or config.get("mode", "armed") != "armed"):
        raise NativeQueryUnavailable("protected, paused or unarmed original scope")
    isolation = config.get("discovery_excluded_surface_ids", [])
    if not isinstance(isolation, list) or surface.upper() not in {
            str(value).upper() for value in isolation}:
        raise NativeQueryUnavailable("required outer input isolation is absent")
    selected = [scope]
    for row in config.get("targets", []):
        if str(row.get("surface_id", "")).upper() == surface.upper():
            selected.append(row)
    for row in config.get("workspace_rules", []):
        if str(row.get("workspace_id", "")).upper() == workspace.upper():
            selected.append(row)
            if surface.upper() in {str(value).upper() for value in row.get("excluded_surface_ids", [])}:
                raise NativeQueryUnavailable("original workspace explicitly excludes this surface")
    selected.append(config.get("sessions", {}).get(sid, {}))
    if any(row.get("paused", False) is not False or row.get("enabled", True) is not True
           for row in selected):
        raise NativeQueryUnavailable("original session, target or workspace paused/disabled")
    return True


class NativeQuery:
    def __init__(self, home, scope, verify_original_binding):
        self.home, self.scope = Path(home), copy.deepcopy(scope)
        self.verify_original_binding = verify_original_binding
        if (self.scope["surface_id"].upper() == PROTECTED_SURFACE
                or self.scope.get("agent_kind") != "codex"
                or type(self.scope.get("pid")) is not int or self.scope["pid"] <= 1):
            raise NativeQueryUnavailable("original inner Codex scope required")
        _birth(self.scope["birth"])
        for name in ("surface_id", "workspace_id"):
            _uuid(self.scope[name])
        self._binding()

    def _binding(self):
        # Supplied by the sole original guard: private Herdr peer, exact
        # server/pane/terminal/outer membership and kernel foreground process.
        if self.verify_original_binding() != self.scope:
            raise NativeQueryUnavailable("original inner binding changed")
        if _kernel_birth(self.scope["pid"]) != self.scope["birth"]:
            raise NativeQueryUnavailable("original native process generation changed")

    def _capability(self):
        root = self.home / "credential-observations/compaction-recovery"
        directories = {str(path): _private_directory(path, allow_readable_home=path == self.home) for path in
                       (self.home, root.parent, root)}
        path = root / f"client-{self.scope['pid']}-control.json"
        pin, raw = _private_file(path)
        cap = decode(raw)
        if (type(cap.get("schema")) is not int or cap.get("schema") != 1
                or cap.get("purpose") != "native_compaction_recovery_control"
                or cap.get("pid") != self.scope["pid"] or cap.get("birth") != self.scope["birth"]
                or cap.get("controlRequiresNativeBackend") is not True
                or cap.get("methods") != [METHOD, "compaction/recovery/control"]):
            raise NativeQueryUnavailable("native ingress capability is unavailable")
        _birth(cap["birth"])
        epoch = _uuid(cap.get("client_epoch"))
        socket_path = root / f"c-{self.scope['pid']}-{epoch[:8]}.sock"
        if (cap.get("socket") != str(socket_path) or len(os.fsencode(socket_path)) >= 104
                or socket_path.resolve(strict=True) != socket_path):
            raise NativeQueryUnavailable("original native socket path changed")
        info = socket_path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o777 != 0o600 or info.st_nlink != 1
                or info.st_dev != cap.get("socket_device") or info.st_ino != cap.get("socket_inode")):
            raise NativeQueryUnavailable("native socket identity changed")
        return path, pin, cap, directories

    def query(self, sid_hint, *, timeout=MAX_TIMEOUT):
        sid = _uuid(sid_hint)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= MAX_TIMEOUT):
            raise NativeQueryUnavailable("bounded native query timeout required")
        self._binding()
        path, pin, cap, directories = self._capability()
        # Complete potentially slow original panel checks before connecting.
        self._binding()
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(max(.001, deadline - time.monotonic()))
            stream.connect(cap["socket"])
            if (_peer(stream) != self.scope["pid"]
                    or _kernel_birth(self.scope["pid"]) != self.scope["birth"]
                    or _private_file(path)[0] != pin):
                raise NativeQueryUnavailable("native peer changed before query")
            wire = {"method": METHOD, "threadId": sid, "clientEpoch": cap["client_epoch"]}
            if time.monotonic() >= deadline:
                raise TimeoutError("fixed native query deadline exceeded before transmission")
            stream.sendall(canonical(wire) + b"\n")
            raw = bytearray()
            while b"\n" not in raw:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("fixed native query deadline exceeded")
                stream.settimeout(remaining)
                chunk = stream.recv(min(4096, MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    raise NativeQueryUnavailable("incomplete native response")
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise NativeQueryUnavailable("native response exceeds limit")
            line, trailing = bytes(raw).split(b"\n", 1)
            if trailing.strip():
                raise NativeQueryUnavailable("multiple native responses are not accepted")
            reply = decode(line)
        self._binding()
        if (_private_file(path)[0] != pin
                or any(_private_directory(Path(name), allow_readable_home=Path(name) == self.home) != identity for name, identity in directories.items())):
            raise NativeQueryUnavailable("original native namespace changed after query")
        if "error" in reply or reply.get("nativeControlSocket") is not True:
            raise NativeQueryUnavailable("original native query refused/unavailable")
        selection = reply.get("selection", {})
        if not isinstance(selection, dict):
            raise NativeQueryUnavailable("native selection must be an object")
        _birth(selection.get("expectedClientBirth"))
        if (selection.get("expectedClientPid") != self.scope["pid"]
                or selection.get("expectedClientBirth") != self.scope["birth"]
                or selection.get("expectedClientEpoch") != cap["client_epoch"]
                or selection.get("threadId") != sid
                or not isinstance(selection.get("expectedConnectionEpoch"), str)
                or not selection["expectedConnectionEpoch"]
                or type(selection.get("expectedSelectionGeneration")) is not int
                or selection["expectedSelectionGeneration"] < 0):
            raise NativeQueryUnavailable("native current selection disagrees with original panel")
        native = reply.get("native", {})
        expected = native.get("identity", {}) if isinstance(native, dict) else {}
        if (not isinstance(expected, dict)
                or set(expected) != {"nativeEpoch", "threadId", "turnId", "occurrence", "generation"}
                or expected.get("threadId") != sid
                or any(not isinstance(expected.get(key), str) or not expected[key]
                       for key in ("nativeEpoch", "turnId", "occurrence"))
                or type(expected.get("generation")) is not int or expected["generation"] < 0):
            raise NativeQueryUnavailable("original native compaction occurrence is unavailable")
        return {"schema": "ccc-herdr-native-ingress-query-r45",
                "selection": copy.deepcopy(selection), "native": copy.deepcopy(native),
                "binding": copy.deepcopy(self.scope), "capability_path": str(path),
                "capability_sha256": pin[1], "observed_at": time.time(),
                "query_only": True, "input_operations": 0,
                "control_verified": False, "recovery_available": False,
                "selected_thread_from_native_ingress": True}


    def inspection(self):
        """Inspect only publication metadata; never open the listener.

        A capability file is not authenticated current selection or actor
        support. The regular query method performs the separate native check.
        """
        self._binding()
        path, pin, cap, directories = self._capability()
        self._binding()
        if (_private_file(path)[0] != pin
                or any(_private_directory(Path(name), allow_readable_home=Path(name) == self.home) != identity
                       for name, identity in directories.items())):
            raise NativeQueryUnavailable("native ingress changed during inspection")
        return {"schema": "ccc-herdr-native-ingress-metadata-r45",
                "phase": "published_selection_not_queried", "mode": "read_only",
                "capability_path": str(path), "capability_sha256": pin[1],
                "client_epoch": cap["client_epoch"], "observed_at": time.time(),
                "namespace_metadata_only": True, "network_queries": 0,
                "input_operations": 0, "current_selection_verified": False,
                "control_verified": False, "recovery_available": False}


def observe_ingress(guard, row):
    """Cheap per-panel namespace health under the original observation.

    This intentionally performs no native request. It does not change reader
    selection, enrollment, authority, clocks, provider budgets or any ledger.
    """
    result = {"schema": "ccc-herdr-native-ingress-health-r45", "mode": "read_only",
              "phase": "unavailable", "observed_at": time.time(),
              "namespace_metadata_only": True, "network_queries": 0,
              "input_operations": 0, "current_selection_verified": False,
              "control_verified": False, "recovery_available": False,
              "business_restart_by_guard": False, "attention_required": False}
    if (str(row.get("cmux_surface_id", "")).upper() == PROTECTED_SURFACE
            or row.get("pid") == PUBLIC_PROTECTED_PID):
        return dict(result, phase="protected", reason="protected_original_surface")
    if row.get("agent") != "codex" or row.get("process_bound") is not True:
        return dict(result, reason="current_inner_codex_process_unbound")
    try:
        process = guard.process(row["pid"])
        if (not isinstance(process, dict) or process.get("pid") != row["pid"]
                or process.get("birth") != row["birth"]):
            raise NativeQueryUnavailable("original observed process generation changed")
        environment = process.get("environment", {})
        if not isinstance(environment, dict):
            raise NativeQueryUnavailable("native launch environment unavailable")
        home = Path(environment.get("CODEX_HOME") or str(
            Path(environment.get("HOME") or Path.home()) / ".codex"))
        scope = {"pid": row["pid"], "birth": copy.deepcopy(row["birth"]),
                 "agent_kind": "codex", "surface_id": row["cmux_surface_id"],
                 "workspace_id": row["cmux_workspace_id"]}
        def same_process():
            if guard.process(row["pid"]) != process:
                raise NativeQueryUnavailable("original native process or launch context changed")
            return copy.deepcopy(scope)
        inspected = NativeQuery(home, scope, same_process).inspection()
        return dict(result, **{key: value for key, value in inspected.items()
                               if key not in {"schema"}},
                    reason="native_ingress_published_requires_separate_current_selection_query")
    except FileNotFoundError:
        return dict(result, phase="capability_absent", attention_required=True,
                    reason="current_client_native_ingress_not_loaded",
                    required_capability="native_compaction_recovery_control")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError) as error:
        return dict(result, phase="unavailable", attention_required=True,
                    reason=str(error)[:160])


def fleet_ingress_health(rows):
    selected = [row for row in rows if row.get("agent") == "codex"]
    phases = {}
    for row in selected:
        phase = (row.get("native_ingress_health") or {}).get("phase", "unavailable")
        phases[phase] = phases.get(phase, 0) + 1
    return {"schema": "ccc-herdr-native-ingress-fleet-r45", "mode": "read_only",
            "codex_panels": len(selected), "phases": phases,
            "attention_required_panels": sum(
                (row.get("native_ingress_health") or {}).get("attention_required") is True
                for row in selected), "network_queries": 0, "input_operations": 0,
            "current_selection_verified_panels": 0,
            "native_control_verified_panels": 0, "business_restart_by_guard": False}
