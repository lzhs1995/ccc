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


def epoch(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


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
    return {"kind": latest["payload"]["type"], "at": at,
            "turn_id": latest["payload"].get("turn_id"), "error": latest["payload"].get("error"),
            "signature": [after.st_ino, after.st_size, after.st_mtime_ns]}


def completed_error(path, session_id, now):
    latest = task_snapshot(path, session_id)
    if not latest or latest["kind"] != "task_complete":
        return None
    error = latest["error"]
    message = str(error.get("message") or "").lower() if isinstance(error, dict) else ""
    if not any(x in message for x in ("currently experiencing high demand", "rate limit exceeded", "temporarily unavailable")):
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


_proc_pidinfo = None
if sys.platform == "darwin":
    try:
        _proc_pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
        _proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.c_void_p, ctypes.c_int]
        _proc_pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError):
        pass


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
        self.open_file_cache = {}
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
        if (not any(x in message for x in ("currently experiencing high demand", "rate limit exceeded", "temporarily unavailable"))
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
        else:
            pids = label.get("agent_pids", [])
        if len(pids) != 1:
            return {"kind": "unknown"}
        pid = pids[0]

        def identity():
            result = subprocess.run(["/bin/ps", "eww", "-o", "lstart=,command=", "-p", str(pid)],
                                    capture_output=True, text=True, timeout=2)
            fields = result.stdout.strip().split(None, 5)
            if (result.returncode or len(fields) != 6 or Path(fields[5].split()[0]).name != "codex"
                    or not all(re.search(r"(?:^|\s)" + name + "=" + re.escape(str(target[key])) + r"(?:\s|$)", fields[5])
                               for name, key in (("CMUX_SURFACE_ID", "surface_id"), ("CMUX_WORKSPACE_ID", "workspace_id")))):
                return None
            return time.mktime(time.strptime(" ".join(fields[:5]), "%a %b %d %H:%M:%S %Y"))

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
                result = subprocess.run(["/usr/sbin/lsof", "-n", "-P", "-a", "-p", str(pid), "-Fn"],
                                        capture_output=True, text=True, timeout=2)
                paths = {Path(line[1:]).resolve() for line in result.stdout.splitlines()
                         if line.startswith("n") and line.endswith(".jsonl")
                         and Path(line[1:]).resolve().is_relative_to(self.sessions_root.resolve())}
                if result.returncode or len(paths) != 1:
                    return {"kind": "unknown"}
                path = paths.pop()
                with path.open() as handle:
                    meta = json.loads(handle.readline())
                sid = meta.get("payload", {}).get("id")
                if meta.get("type") != "session_meta" or not isinstance(sid, str) or not sid:
                    return {"kind": "unknown"}
                with self.lock:
                    self.open_file_cache[cache_key] = (time.monotonic(), path, sid)
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
