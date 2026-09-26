"""Recover one stranded CCC prompt without adding another queued message.

Codex 0.154 may suppress queue autosend after a provider error. Reuse its
visible edit-queued-message binding only after the original process transcript
proves the turn ended. Every key is write-ahead recorded; ambiguity stops here.
"""
from __future__ import annotations

from datetime import datetime
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid


def epoch(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def writable_open_files(output):
    """lsof names alone include unrelated history being indexed at startup."""
    descriptor, access = "", ""
    paths = set()
    for line in output.splitlines():
        if line.startswith("f"):
            descriptor, access = line[1:], ""
        elif line.startswith("a"):
            access = line[1:]
        elif line.startswith("n") and descriptor.isdecimal() and access in {"w", "u"}:
            paths.add(Path(line[1:]).resolve())
    return paths


def task_snapshot(path, session_id):
    """Read the original thread's latest lifecycle event, with a stable file."""
    path = Path(path)
    before = path.stat()
    with path.open("rb") as handle:
        first = json.loads(handle.readline())
        if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != session_id:
            return None
        handle.seek(max(0, before.st_size - 1024 * 1024))
        tail = handle.read().decode("utf-8", errors="replace")
    latest = None
    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload", {})
        if event.get("type") == "event_msg" and payload.get("type") in {
            "task_started", "task_complete", "turn_aborted", "user_message",
        }:
            latest = event
            break
    if not latest:
        return None
    after = path.stat()
    if (after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
        return None
    try:
        at = epoch(latest.get("timestamp"))
    except (TypeError, ValueError):
        return None
    return {**({"model_provider": first["payload"]["model_provider"]} if first["payload"].get("model_provider") else {}),
            "kind": latest["payload"]["type"], "at": at,
            "turn_id": latest["payload"].get("turn_id"), "error": latest["payload"].get("error"),
            "signature": [after.st_ino, after.st_size, after.st_mtime_ns]}


def _retryable_completed_message(message):
    normalized = " ".join(message.lower().split())
    return (any(x in normalized for x in ("currently experiencing high demand", "rate limit exceeded",
                                          "temporarily unavailable", "stream disconnected before completion"))
            or normalized == "connection failed: error sending request")


def completed_error(path, session_id, now):
    latest = task_snapshot(path, session_id)
    if not latest or latest["kind"] != "task_complete":
        return None
    error = latest["error"]
    message = str(error.get("message") or "").lower() if isinstance(error, dict) else ""
    if not _retryable_completed_message(message):
        return None
    completed = latest["at"]
    if not 5 <= now - completed <= 24 * 3600:
        return None
    return {"completed_at": completed, "turn_id": latest["turn_id"], "signature": latest["signature"]}


class _BsdInfo(ctypes.Structure):
    # Darwin sys/proc_info.h: PROC_PIDTBSDINFO, stable public 136-byte ABI.
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                ("ppid", ctypes.c_uint32), ("ids", ctypes.c_uint32 * 7),
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                ("misc", ctypes.c_uint32 * 6), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64)]


class _FdInfo(ctypes.Structure):
    _fields_ = [("fd", ctypes.c_int32), ("kind", ctypes.c_uint32)]


class _VnodeFdInfo(ctypes.Structure):
    # Darwin sys/proc_info.h: vnode_fdinfowithpath, 1200-byte public ABI.
    # proc_fileinfo is 24 bytes; vnode_info is 152; MAXPATHLEN is 1024.
    _fields_ = [("openflags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                ("offset", ctypes.c_int64), ("kind", ctypes.c_int32),
                ("guardflags", ctypes.c_uint32), ("vnode", ctypes.c_byte * 152),
                ("path", ctypes.c_char * 1024)]


_proc_pidinfo = None
_proc_pidfdinfo = None
_proc_listpids = None
_procargs_sysctl = None
_procargs_bytes = 0
if sys.platform == "darwin":
    try:
        _proc_pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
        _proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.c_void_p, ctypes.c_int]
        _proc_pidinfo.restype = ctypes.c_int
        _proc_listpids = ctypes.CDLL("/usr/lib/libproc.dylib").proc_listpids
        _proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        _proc_listpids.restype = ctypes.c_int
        _proc_pidfdinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidfdinfo
        _proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_void_p, ctypes.c_int]
        _proc_pidfdinfo.restype = ctypes.c_int
    except (OSError, AttributeError):
        pass
    try:
        _procargs_sysctl = ctypes.CDLL(None, use_errno=True).sysctl
        _procargs_sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                    ctypes.c_void_p, ctypes.c_size_t]
        _procargs_sysctl.restype = ctypes.c_int
        _procargs_bytes = os.sysconf("SC_ARG_MAX")
    except (OSError, AttributeError):
        pass


def process_writable_files(pid, *, identities=False):
    """Inspect one process directly, without forking lsof for every CLI poll.

    Incomplete native reads or changing vnode descriptors remain unknown.
    Callers separately recheck PID generation, placement and original session.
    lsof is only the portable fallback when the native API is unavailable.
    """
    if type(pid) is not int or not 0 < pid < 2**31:
        raise OSError("invalid process identity")
    if _proc_pidinfo is None or _proc_pidfdinfo is None:
        if identities:
            raise OSError("native file identities unavailable")
        result = subprocess.run(["/usr/sbin/lsof", "-n", "-P", "-a", "-p", str(pid), "-Ffan"],
                                capture_output=True, text=True, timeout=2)
        if result.returncode:
            raise OSError("process file inventory unavailable")
        return writable_open_files(result.stdout)

    def descriptors():
        item_size = ctypes.sizeof(_FdInfo)
        needed = _proc_pidinfo(pid, 1, 0, None, 0)  # PROC_PIDLISTFDS
        if needed <= 0 or needed % item_size or needed // item_size > 16320:
            raise OSError("incomplete process descriptor inventory")
        entries = (_FdInfo * (needed // item_size + 64))()
        size = ctypes.sizeof(entries)
        count = _proc_pidinfo(pid, 1, 0, entries, size)
        if count <= 0 or count >= size or count % item_size:
            raise OSError("truncated process descriptor inventory")
        return frozenset(e.fd for e in entries[:count // item_size] if e.kind == 1)

    def vnodes(fds):
        result = {}
        for fd in fds:
            info = _VnodeFdInfo()
            size = ctypes.sizeof(info)
            if _proc_pidfdinfo(pid, fd, 2, ctypes.byref(info), size) != size:
                raise OSError("incomplete vnode descriptor")
            # Access mode, path, device and inode also detect reuse of an FD
            # number between the two inventories. Ignore changing timestamps.
            vnode = bytes(info.vnode)
            result[fd] = (info.openflags & 3, os.fsdecode(info.path), vnode[:4], vnode[8:16])
        return result

    before = descriptors()
    files = vnodes(before)
    if descriptors() != before or vnodes(before) != files:
        raise OSError("process vnode descriptors changed")
    paths = {} if identities else set()
    for flags, name, _device, _inode in files.values():
        if flags & 2:  # Kernel FWRITE, not userspace O_WRONLY.
            path = Path(name)
            if not path.is_absolute():
                raise OSError("missing vnode path")
            path = path.resolve()
            if identities:
                paths[path] = {"device": int.from_bytes(_device, sys.byteorder),
                               "inode": int.from_bytes(_inode, sys.byteorder)}
            else:
                paths.add(path)
    return paths


def codex_process_starts(pids):
    """Read only the requested live PID identities, without enumerating the OS.

    On a busy Mac even `ps -p` can stall for seconds. libproc reads each bound
    PID directly; failed/short reads never become identity evidence. The same
    uncached check is used at the send boundary, preserving PID reuse guards.
    """
    pids = {pid for pid in pids if type(pid) is int and 0 < pid < 2**31}
    starts = {}
    if _proc_pidinfo is not None:
        for pid in pids:
            info = _BsdInfo()
            size = ctypes.sizeof(info)
            if (_proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) == size
                    and info.pid == pid and info.status != 5
                    and (info.name or info.comm) == b"codex" and info.start_sec > 0):
                starts[pid] = info.start_sec
        return starts
    if not pids:
        return starts
    try:
        result = subprocess.run(["/bin/ps", "-o", "pid=,lstart=,comm=", "-p",
            ",".join(str(pid) for pid in sorted(pids))], capture_output=True, text=True, timeout=1)
        for line in result.stdout.splitlines():
            fields = line.split(None, 6)
            try:
                if len(fields) == 7 and int(fields[0]) in pids and Path(fields[6]).name == "codex":
                    starts[int(fields[0])] = time.mktime(time.strptime(" ".join(fields[1:6]), "%a %b %d %H:%M:%S %Y"))
            except ValueError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass
    return starts


def process_matches(record):
    pid, started = record.get("pid"), record.get("pidStartSeconds")
    if type(pid) is not int or type(started) not in (int, float):
        return False
    actual = codex_process_starts([pid]).get(pid)
    return actual is not None and abs(actual - started) < 1


def batch_shell_identity(pid):
    """Pin the bootstrap's parent shell without an all-process scan."""
    if _proc_pidinfo is None or type(pid) is not int or not 0 < pid < 2**31:
        return None
    info = _BsdInfo()
    size = ctypes.sizeof(info)
    if (_proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid
            or info.status == 5 or (info.name or info.comm) not in {b'zsh', b'bash', b'sh', b'fish'}):
        return None
    return [info.start_sec, info.start_usec]


def batch_child_label(shell_pid, shell_start, target):
    """Read only one receipt-pinned shell's descendants; fail closed on races.

    This is a discovery hint. QueueRecovery still verifies the native writer,
    session UUID and process placement immediately before every input.
    """
    if (_proc_listpids is None or not shell_start
            or batch_shell_identity(shell_pid) != shell_start):
        return None
    pending, seen, agents = [shell_pid], set(), []
    other = False
    while pending and len(seen) < 64:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        children = (ctypes.c_int * 64)()
        size = ctypes.sizeof(children)
        count = _proc_listpids(6, parent, children, size)  # PROC_PPID_ONLY
        if count < 0 or count >= size or count % ctypes.sizeof(ctypes.c_int):
            return None
        for pid in children[:count // ctypes.sizeof(ctypes.c_int)]:
            info = _BsdInfo()
            if (_proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
                    or info.pid != pid or info.ppid != parent or info.status == 5):
                return None
            if (info.name or info.comm) == b'codex':
                if process_placement_start(pid, target) is None:
                    return None
                agents.append(pid)
            else:
                other |= (info.name or info.comm) not in {b'zsh', b'bash', b'sh', b'fish', b'sleep'}
                pending.append(pid)
    if pending or batch_shell_identity(shell_pid) != shell_start:
        return None
    if len(agents) == 1:
        return {'agent_kind': 'codex', 'agent_pids': agents, 'process_snapshot_present': True}
    if not agents and not other:
        return {'agent_kind': 'shell', 'agent_pids': [], 'process_snapshot_present': True}
    return None


def _process_placement_args(data):
    """Decode Darwin KERN_PROCARGS2, keeping only the two placement variables.

    argv is length-delimited by argc, not by text that resembles environment
    assignments. Never retain or log the rest of the process environment.
    """
    size = ctypes.sizeof(ctypes.c_int)
    if len(data) <= size:
        return None
    argc = ctypes.c_int.from_buffer_copy(data[:size]).value
    if not 1 <= argc <= 65536:
        return None
    end = data.find(b"\0", size)
    if end < 0:
        return None
    position = end + 1
    while position < len(data) and data[position] == 0:
        position += 1
    for index in range(argc):
        end = data.find(b"\0", position)
        if end < 0:
            return None
        if index == 0 and data[position:end].rsplit(b"/", 1)[-1] != b"codex":
            return None
        position = end + 1
    placement = {}
    for value in data[position:].split(b"\0"):
        if not value:
            break
        name, separator, content = value.partition(b"=")
        if separator and name in {b"CMUX_SURFACE_ID", b"CMUX_WORKSPACE_ID"}:
            if name.decode() in placement:
                return None
            placement[name.decode()] = content.decode("utf-8", errors="strict")
    return placement


def process_placement_start(pid, target):
    """Uncached PID/start and exact surface ownership for a legacy session."""
    if _procargs_sysctl is not None and _proc_pidinfo is not None:
        started = codex_process_starts([pid]).get(pid)
        if started is None:
            return None
        # Darwin rejects buffers larger than the host's ARG_MAX with EINVAL.
        # One direct query avoids spawning ps twice for every old session.
        if not 0 < _procargs_bytes <= 2 * 1024 * 1024:
            return None
        buffer = ctypes.create_string_buffer(_procargs_bytes)
        length = ctypes.c_size_t(len(buffer))
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        if _procargs_sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
            return None
        if length.value > len(buffer):
            return None
        try:
            placement = _process_placement_args(buffer.raw[:length.value])
        except UnicodeError:
            return None
        if placement is None or any(placement.get(name) != str(target[key]) for name, key in (
                ("CMUX_SURFACE_ID", "surface_id"), ("CMUX_WORKSPACE_ID", "workspace_id"))):
            return None
        return started
    result = subprocess.run(["/bin/ps", "eww", "-o", "lstart=,command=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=2)
    fields = result.stdout.strip().split(None, 5)
    if (result.returncode or len(fields) != 6 or Path(fields[5].split()[0]).name != "codex"
            or not all(re.search(r"(?:^|\s)" + name + "=" + re.escape(str(target[key])) + r"(?:\s|$)", fields[5])
                       for name, key in (("CMUX_SURFACE_ID", "surface_id"), ("CMUX_WORKSPACE_ID", "workspace_id")))):
        return None
    return time.mktime(time.strptime(" ".join(fields[:5]), "%a %b %d %H:%M:%S %Y"))


class NativeCompletionWatcher:
    """Wake existing viewport checks from bounded, read-only transcript tails.

    Source identities are advisory here. The existing send path re-verifies
    the live process, original failed turn, composer, authorization and ledger.
    This watcher has no terminal-input operation.
    """
    def __init__(self, sources, wake, *, interval=0.25, tail_bytes=16384,
                 retry_needed=None, clock=time.monotonic):
        self.sources, self.wake = sources, wake
        self.interval, self.tail_bytes = interval, tail_bytes
        self.signatures, self.seen_turns = {}, {}
        self.pending, self.retry_needed, self.clock = {}, retry_needed, clock
        self.lifecycle, self.coverage = {}, {}
        self.stop = threading.Event()
        self.thread = None

    def scan(self):
        sources = self.sources()
        active = set()
        covered = {}
        for source in sources:
            key = (source["surface_id"], source["workspace_id"],
                   source["session_id"], str(source["path"]))
            active.add(key)
            path = Path(source["path"])
            try:
                before = path.stat()
                signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                if self.signatures.get(key) == signature:
                    if key in self.lifecycle and source.get("identity_current"):
                        covered[key[:2]] = self.clock()
                    pending = self.pending.get(key)
                    if (pending and self.retry_needed and self.clock() >= pending[1]
                            and self.retry_needed(key[0], key[1], pending[0])):
                        self.wake(key[0], key[1])
                        self.pending[key] = (pending[0], self.clock() + 1)
                    continue
                with path.open("rb") as handle:
                    offset = max(0, before.st_size - self.tail_bytes)
                    handle.seek(offset)
                    tail = handle.read(self.tail_bytes)
                after = path.stat()
                if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    continue
                lines = tail.splitlines()
                if offset:
                    lines = lines[1:]  # Never parse a partial leading record.
                latest = None
                for line in reversed(lines):
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    payload = event.get("payload", {})
                    if event.get("type") == "event_msg" and payload.get("type") in {
                        "task_started", "task_complete", "turn_aborted", "user_message",
                    }:
                        latest = event
                        break
                if latest and latest["payload"].get("type") == "task_complete" and latest["payload"].get("error"):
                    turn = (latest["payload"].get("turn_id"), latest.get("timestamp"))
                    if self.seen_turns.get(key) != turn:
                        if not self.wake(key[0], key[1]):
                            continue  # The scheduler may still be discovering this UUID.
                        self.seen_turns[key] = turn
                        self.pending[key] = (epoch(latest.get("timestamp")), self.clock() + 1)
                elif latest:
                    self.pending.pop(key, None)
                if latest:
                    self.lifecycle[key] = latest["payload"]["type"]
                    if source.get("identity_current"):
                        covered[key[:2]] = self.clock()
                else:
                    self.lifecycle.pop(key, None)
                self.signatures[key] = signature
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        self.signatures = {key: value for key, value in self.signatures.items() if key in active}
        self.seen_turns = {key: value for key, value in self.seen_turns.items() if key in active}
        self.pending = {key: value for key, value in self.pending.items() if key in active}
        self.lifecycle = {key: value for key, value in self.lifecycle.items() if key in active}
        self.coverage = covered

    def observation_interval(self, target, fallback):
        """Healthy native monitoring replaces redundant reads, never send guards.

        A missing/stalled source immediately falls back to regular viewport
        polling. Native failures still request an immediate priority read.
        """
        checked = self.coverage.get((str(target["surface_id"]), str(target["workspace_id"])))
        if checked is not None and 0 <= self.clock() - checked <= max(1, 3 * self.interval):
            return max(fallback, 10.0)
        return fallback

    def start(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.scan()
                except (OSError, ValueError, TypeError, AttributeError, KeyError):
                    pass  # A hint failure cannot disable regular viewport scans.
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=run, name="ccc-native-wakeup", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()


class QueueRecovery:
    def __init__(self, ledger, bindings, sessions_root, message):
        self.ledger, self.bindings, self.sessions_root = Path(ledger), Path(bindings), Path(sessions_root)
        self.message = message
        self.lock = threading.RLock()
        self.next_probe = {}
        self.binding_cache = (0.0, {})
        self.process_lookup = None
        self.guard_config_path = None
        self.open_file_cache = {}
        self.idle_file_cache = {}
        self.open_file_sources = {}
        self.wakeup_process_cache = (0.0, frozenset(), {})
        try:
            self.attempts = json.loads(self.ledger.read_text())
        except FileNotFoundError:
            self.attempts = {}
        except (OSError, ValueError):
            self.attempts = None  # A broken delivery record cannot be reset.

    def records(self):
        with self.lock:
            now = time.monotonic()
            if now - self.binding_cache[0] > 2:
                data = json.loads(self.bindings.read_text()).get("sessions", {})
                self.binding_cache = (now, data)
            return self.binding_cache[1]

    def wakeup_sources(self, targets):
        """Latest known original transcript for each currently enabled UUID."""
        active_targets = {str(t["surface_id"]): t for t in targets
                          if t.get("enabled", True) and not t.get("paused", False)}
        active = {sid: str(t["workspace_id"]) for sid, t in active_targets.items()}
        try:
            records = self.records()
        except (OSError, ValueError):
            records = {}
        with self.lock:
            candidates = list(self.open_file_sources.values())
        candidates.extend({"surface_id": r.get("surfaceId"), "workspace_id": r.get("workspaceId"),
                           "session_id": sid, "path": r.get("transcriptPath"),
                           "pid": r.get("pid"),
                           "process_start": r.get("pidStartSeconds", 0)}
                          for sid, r in records.items())
        chosen = {}
        root = self.sessions_root.resolve()
        for source in candidates:
            sid = source["surface_id"]
            if active.get(sid) != source["workspace_id"] or not source["path"]:
                continue
            try:
                if not Path(source["path"]).resolve().is_relative_to(root):
                    continue
                if sid not in chosen or source["process_start"] > chosen[sid]["process_start"]:
                    chosen[sid] = source
            except (OSError, ValueError, TypeError):
                continue
        # Scheduling must not depend on cmux's expensive GUI process snapshot:
        # its normal refresh gap used to discard every healthy native monitor
        # at once, producing another full-fleet burst of viewport requests.
        # Direct OS queries check the already-bound PID/start identities.
        # This is advisory coverage only; send still performs its full guards.
        pids = frozenset(s["pid"] for s in chosen.values() if type(s.get("pid")) is int and s["pid"] > 0)
        cached_at, cached_pids, starts = self.wakeup_process_cache
        now = time.monotonic()
        if pids != cached_pids or now - cached_at >= 1:
            starts = codex_process_starts(pids)
            self.wakeup_process_cache = (now, pids, starts)
        return [{**source, "identity_current": bool(source.get("pid") in starts
                  and abs(starts[source["pid"]] - source["process_start"]) < 1)}
                for source in chosen.values()]

    def evidence(self, target):
        turn = self.current_turn(target)
        if not turn or turn.get("kind") != "task_complete":
            return None
        error = turn.get("error")
        message = str(error.get("message") or "").lower() if isinstance(error, dict) else ""
        if (not _retryable_completed_message(message)
                or not 5 <= time.time() - turn["at"] <= 24 * 3600):
            return None
        return {"session_id": turn["session_id"], "pid": turn["pid"], "process_start": turn["process_start"],
                "completed_at": turn["at"], "turn_id": turn["turn_id"], "signature": turn["signature"]}

    def current_turn(self, target):
        """Native lifecycle veto for the window between error display and Stop.

        Legacy clients without a native binding retain the viewport guard. Once
        a current PID/start binding exists, missing/changing lifecycle evidence
        must not turn a displayed error into permission to submit early.
        """
        if self.guard_config_path is not None:
            from ccc_batch_guard import binding
            guarded = binding(self.guard_config_path, target)
            if guarded and guarded.get("session_id"):
                return {"session_id": guarded["session_id"], "pid": guarded["pid"],
                        "process_start": guarded["process_start"], "kind": guarded["kind"],
                        "turn_id": guarded.get("turn_id"), "at": guarded.get("at", 0),
                        "error": guarded.get("turn_error"),
                        "signature": [guarded.get("start_id"), guarded.get("turn_id"), guarded.get("at")]}
        try:
            records = self.records()
        except FileNotFoundError:
            return self._open_process_turn(target)
        except (OSError, ValueError):
            return {"kind": "unknown"}
        matches = []
        for sid, record in records.items():
            if (record.get("surfaceId") != target["surface_id"]
                    or record.get("workspaceId") != target["workspace_id"]):
                continue
            try:
                if not process_matches(record):
                    continue
                path = Path(str(record.get("transcriptPath") or "")).resolve()
                if not path.is_relative_to(self.sessions_root.resolve()):
                    return {"kind": "unknown"}
                snapshot = task_snapshot(path, sid)
                matches.append({"session_id": sid, "pid": record["pid"],
                                "process_start": record["pidStartSeconds"],
                                **(snapshot or {"kind": "unknown"})})
            except (OSError, ValueError, subprocess.SubprocessError):
                return {"kind": "unknown"}
        if not matches:
            return self._open_process_turn(target)
        return matches[0] if len(matches) == 1 else {"kind": "unknown"}

    def _open_process_turn(self, target):
        """Recover read-only identity when a real SessionStart Hook was lost.

        The current process must belong to this exact workspace and surface,
        and hold exactly one original rollout open. No Hook is synthesized.
        """
        if self.process_lookup is None or not self.sessions_root.is_dir():
            return None
        label = self.process_lookup(target)
        hint_started = None
        hint_source = None
        if label.get("agent_kind") != "codex":
            # A GUI refresh gap must not erase an already verified original
            # process. Reuse only its PID hint, then recheck the native start,
            # exact CMUX environment and actual open transcript below. A fresh
            # conflicting/absent label still vetoes this path.
            if label.get("summary") not in {"process refresh pending", "process lookup unavailable"}:
                return {"kind": "unknown"}
            with self.lock:
                known = dict(self.open_file_sources.get(str(target["surface_id"]), {}))
            pid = known.get("pid")
            current = codex_process_starts([pid]).get(pid)
            if (known.get("workspace_id") != str(target["workspace_id"])
                    or known.get("surface_id") != str(target["surface_id"])
                    or current is None or current != known.get("process_start")):
                return {"kind": "unknown"}
            hint_started, pids = current, [pid]
            hint_source = known
        else:
            pids = label.get("agent_pids", [])
        if len(pids) != 1:
            return {"kind": "unknown"}
        pid = pids[0]

        def identity():
            return process_placement_start(pid, target)

        try:
            started = identity()
            if started is None or (hint_started is not None and started != hint_started):
                return {"kind": "unknown"}
            cache_key = (pid, started)
            with self.lock:
                cached = self.open_file_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 1:
                path, sid = cached[1:]
            else:
                paths = {path for path in process_writable_files(pid)
                         if path.suffix == ".jsonl" and path.is_relative_to(self.sessions_root.resolve())}
                if not paths:
                    return self._idle_process_turn(target, pid, hint_source)
                if len(paths) != 1:
                    return {"kind": "unknown"}
                path = paths.pop()
                with path.open() as handle:
                    meta = json.loads(handle.readline())
                sid = meta.get("payload", {}).get("id")
                if meta.get("type") != "session_meta" or not isinstance(sid, str) or not sid:
                    return {"kind": "unknown"}
                with self.lock:
                    self.open_file_cache[cache_key] = (time.monotonic(), path, sid)
            if hint_source is not None and (
                    sid != hint_source.get("session_id")
                    or path != Path(str(hint_source.get("path") or "")).resolve()):
                return {"kind": "unknown"}
            snapshot = task_snapshot(path, sid)
            if identity() != started:
                return {"kind": "unknown"}
            with self.lock:
                self.open_file_sources[str(target["surface_id"])] = {
                    "surface_id": str(target["surface_id"]), "workspace_id": str(target["workspace_id"]),
                    "session_id": sid, "path": path, "process_start": started,
                    "pid": pid,
                }
            return {"session_id": sid, "pid": pid, "process_start": started,
                    **(snapshot or {"kind": "unknown"})}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"kind": "unknown"}

    def _idle_process_turn(self, target, pid, hint_source=None):
        """Codex closes its rollout while idle but retains the thread writer lock.

        A missed SessionStart Hook must not make that original failed turn
        permanently unknowable. The live lock's inode and UUID, exact process
        birth/placement, original file identity and current lifecycle all have
        to agree. This never creates a Hook, session, or continuation itself.
        """
        import ccc_guard_scope as scope

        def linked(path, identity):
            info = path.stat()
            return info.st_dev == identity["device"] and info.st_ino == identity["inode"]

        try:
            process = scope.process(pid)
            if (not process or process["surface_id"] != str(target["surface_id"])
                    or process["environment_workspace_id"] != str(target["workspace_id"])
                    or process.get("remote")):
                return {"kind": "unknown"}
            argv, _ = scope.arguments(pid)
            options = argv[:argv.index("--")] if "--" in argv else argv
            if any(arg == "--remote" or arg.startswith("--remote=") for arg in options):
                return {"kind": "unknown"}
            files = process_writable_files(pid, identities=True)
            locks = [p for p in files if p.parent.name == "thread-writer-locks" and p.suffix == ".lock"]
            if len(locks) != 1:
                return {"kind": "unknown"}
            lock = locks[0]
            sid = str(uuid.UUID(lock.stem))
            root = self.sessions_root.resolve()
            native_sessions = (lock.parent.parent / "sessions").resolve()
            if (not native_sessions.is_relative_to(root) or not native_sessions.is_dir()
                    or not linked(lock, files[lock])):
                return {"kind": "unknown"}
            key = (pid, tuple(process["birth"]), sid, files[lock]["device"], files[lock]["inode"])
            with self.lock:
                cached = self.idle_file_cache.get(key)
            if cached:
                path, file_identity = cached
                if not linked(path, file_identity):
                    return {"kind": "unknown"}
            else:
                candidates = list(native_sessions.rglob(f"*-{sid}.jsonl"))
                if len(candidates) != 1:
                    return {"kind": "unknown"}
                path = candidates[0].resolve()
                if not path.is_relative_to(native_sessions):
                    return {"kind": "unknown"}
                info = path.stat()
                file_identity = {"device": info.st_dev, "inode": info.st_ino}
            if hint_source is not None and (
                    sid != hint_source.get("session_id")
                    or path != Path(str(hint_source.get("path") or "")).resolve()):
                return {"kind": "unknown"}
            snapshot = task_snapshot(path, sid)
            born = process["birth"][0] + process["birth"][1] / 1e6
            if not snapshot or snapshot["at"] < born:
                return {"kind": "unknown"}
            current_files = process_writable_files(pid, identities=True)
            current_locks = {p for p in current_files if p.parent.name == "thread-writer-locks" and p.suffix == ".lock"}
            current_rollouts = {p for p in current_files if p.suffix == ".jsonl" and p.is_relative_to(root)}
            if (scope.process(pid) != process or current_locks != {lock}
                    or current_files.get(lock) != files[lock] or not linked(lock, files[lock])
                    or not linked(path, file_identity)
                    or current_rollouts - {path}
                    or (path in current_files and current_files[path] != file_identity)):
                return {"kind": "unknown"}
            with self.lock:
                self.idle_file_cache[key] = (path, file_identity)
                self.open_file_sources[str(target["surface_id"])] = {
                    "surface_id": str(target["surface_id"]), "workspace_id": str(target["workspace_id"]),
                    "session_id": sid, "path": path, "process_start": process["process_start"], "pid": pid,
                }
            return {"session_id": sid, "pid": pid, "process_start": process["process_start"],
                    "birth": process["birth"], **snapshot}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return {"kind": "unknown"}

    def initial_session(self, target, created_after):
        """Identify a newly created, still empty CLI for the explicit batch action.

        Codex 0.154 holds its native UUID writer lock before it creates a rollout.
        This is never failed-turn evidence and cannot authorize guard retries.
        Require the exact live process, a newly minted UUID, and no prior rollout.
        """
        if self.process_lookup is None:
            return None
        label = self.process_lookup(target)
        pids = label.get("agent_pids", [])
        if label.get("agent_kind") != "codex" or len(pids) != 1:
            return None
        pid = pids[0]
        try:
            started = process_placement_start(pid, target)
            if started is None or started < created_after - 1:
                return None
            files = process_writable_files(pid)
            root = self.sessions_root.resolve()
            if any(p.suffix == ".jsonl" and p.is_relative_to(root) for p in files):
                return None
            locks = [p for p in files if p.suffix == ".lock" and p.parent.name == "thread-writer-locks"]
            if len(locks) != 1:
                return None
            lock = locks[0]
            session = uuid.UUID(lock.stem)
            minted = (session.int >> 80) / 1000
            native_sessions = lock.parent.parent / "sessions"
            if (session.version != 7 or not created_after - .001 <= minted <= time.time() + 1
                    or not native_sessions.resolve().is_relative_to(root)
                    or not native_sessions.is_dir()
                    or next(native_sessions.rglob(f"*{session}.jsonl"), None) is not None
                    or process_placement_start(pid, target) != started):
                return None
            return {"kind": "uninitialized", "session_id": str(session), "pid": pid, "process_start": started}
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def write_attempt(self, key, record):
        with self.lock:
            if self.attempts is None:
                raise RuntimeError("queue recovery ledger unreadable")
            self.attempts[key] = record
            temp = self.ledger.with_name(f".{self.ledger.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(self.attempts) + "\n")
            temp.replace(self.ledger)

    def has_pending_draft(self, sid):
        with self.lock:
            return bool(self.attempts and any(r.get("surface_id") == sid and r.get("phase") in {"edited", "unconfirmed"}
                                             for r in self.attempts.values()))

    def recover(self, target, runtime, *, read_view, edit_queued, enter, authorized):
        sid = target["surface_id"]
        with self.lock:
            if self.attempts is None or time.monotonic() < self.next_probe.get(sid, 0):
                return ""
            self.next_probe[sid] = time.monotonic() + 5
        if (runtime.delivery_status in {"unknown", "sending"}
                or (runtime.send_count < 1 and not self.has_pending_draft(sid))
                or target.get("paused") or not target.get("enabled", True)):
            return ""
        try:
            evidence = self.evidence(target)
            if not evidence:
                return ""
            turn = {k: evidence.get(k) for k in ("session_id", "pid", "process_start", "completed_at", "turn_id")}
            key = hashlib.sha256(json.dumps([sid, turn], sort_keys=True).encode()).hexdigest()
            with self.lock:
                previous = self.attempts.get(key)
            if previous:
                # An acknowledged edit with no Enter attempted can be finished
                # from its exact draft. A lost Enter acknowledgement cannot.
                if previous["phase"] in {"edited", "unconfirmed"}:
                    view = read_view()
                    if (not view.get("busy") and not view.get("queued") and view.get("draft") == self.message
                            and authorized() and self.evidence(target) == evidence):
                        record = previous
                        self.write_attempt(key, {**record, "phase": "submitting"})
                        enter()
                        self.write_attempt(key, {**record, "phase": "submitted", "submitted_at": time.time()})
                        return "queue_recovery_submitted"
                return "queue_recovery_unconfirmed" if previous["phase"] != "submitted" else ""
            view = read_view()
            if (not view.get("empty") or view.get("busy") or not view.get("editable")
                    or view.get("queued") != [self.message] or not authorized()):
                return ""
            if self.evidence(target) != evidence:
                return ""
            record = {"surface_id": sid, "workspace_id": target["workspace_id"],
                      **evidence, "phase": "editing", "at": time.time()}
            self.write_attempt(key, record)
            edit_queued()
            self.write_attempt(key, {**record, "phase": "edited"})
            time.sleep(0.15)
            view = read_view()
            if (view.get("busy") or view.get("queued") or view.get("draft") != self.message
                    or not authorized() or self.evidence(target) != evidence):
                self.write_attempt(key, {**record, "phase": "unconfirmed"})
                return "queue_recovery_unconfirmed"
            self.write_attempt(key, {**record, "phase": "submitting"})
            enter()
            self.write_attempt(key, {**record, "phase": "submitted", "submitted_at": time.time()})
            return "queue_recovery_submitted"
        except Exception:
            # Do not repeat a potentially delivered key. The pre-written record
            # survives both transport uncertainty and daemon restarts.
            return "queue_recovery_unconfirmed" if 'record' in locals() else ""
