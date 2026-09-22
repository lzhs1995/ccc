"""Recover one stranded CCC prompt without adding another queued message.

Codex 0.154 may suppress queue autosend after a provider error. Reuse its
visible edit-queued-message binding only after the original process transcript
proves the turn ended. Every key is write-ahead recorded; ambiguity stops here.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
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
    for line in tail.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload", {})
        if event.get("type") == "event_msg" and payload.get("type") in {
            "task_started", "task_complete", "turn_aborted", "user_message",
        }:
            latest = event
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


def process_matches(record):
    pid, started = record.get("pid"), record.get("pidStartSeconds")
    if type(pid) is not int or type(started) not in (int, float):
        return False
    result = subprocess.run(["/bin/ps", "-o", "lstart=,comm=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=2)
    fields = result.stdout.strip().split(None, 5)
    if result.returncode or len(fields) != 6 or Path(fields[5]).name != "codex":
        return False
    actual = time.mktime(time.strptime(" ".join(fields[:5]), "%a %b %d %H:%M:%S %Y"))
    return abs(actual - started) < 1


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
        self.stop = threading.Event()
        self.thread = None

    def scan(self):
        sources = self.sources()
        active = set()
        for source in sources:
            key = (source["surface_id"], source["workspace_id"],
                   source["session_id"], str(source["path"]))
            active.add(key)
            path = Path(source["path"])
            try:
                before = path.stat()
                signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                if self.signatures.get(key) == signature:
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
                self.signatures[key] = signature
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        self.signatures = {key: value for key, value in self.signatures.items() if key in active}
        self.seen_turns = {key: value for key, value in self.seen_turns.items() if key in active}
        self.pending = {key: value for key, value in self.pending.items() if key in active}

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
        active = {str(t["surface_id"]): str(t["workspace_id"]) for t in targets
                  if t.get("enabled", True) and not t.get("paused", False)}
        try:
            records = self.records()
        except (OSError, ValueError):
            records = {}
        with self.lock:
            candidates = list(self.open_file_sources.values())
        candidates.extend({"surface_id": r.get("surfaceId"), "workspace_id": r.get("workspaceId"),
                           "session_id": sid, "path": r.get("transcriptPath"),
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
        return list(chosen.values())

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
        if label.get("agent_kind") != "codex":
            # A cold/expired shared process snapshot is not proof of a legacy
            # client. Returning None lets the caller submit from the viewport
            # alone, including while the native task is still reconnecting.
            return {"kind": "unknown"}
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
            if started is None:
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
