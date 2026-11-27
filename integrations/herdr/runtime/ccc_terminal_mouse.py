"""Suppress mouse-report leakage in an enrolled local foreground Codex TTY.

This is terminal output, not agent input or task recovery.  A successful OFF
observation permits a later, independently observed ON episode.  A partial write,
crash or unknown postcondition permanently holds that identity's output budget.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

MOUSE_CODES = (9, 1000, 1002, 1003, 1005, 1006, 1015, 1016)
GENERATING_CODES = (9, 1000, 1002, 1003)
# Preserve 1007 scroll mapping and all keyboard, paste, focus and screen modes.
CONTROLS = b"".join(b"\x1b[?" + str(code).encode("ascii") + b"l" for code in MOUSE_CODES)
KNOWN_KINDS = frozenset({"native_activity", "task_started", "task_complete", "turn_aborted", "user_message"})


class Refused(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity_key(proof):
    # Publication nonces, writer restarts, argv, policy and panel moves cannot
    # grant another write after an unknown output for this original client.
    return digest({"surface_id": proof["target"]["surface_id"],
                   "pid": proof["identity"]["pid"], "birth": proof["identity"]["birth"],
                   "session_id": proof["identity"]["session_id"]})


def mode_values(grid):
    raw = grid.raw
    if raw.get("anchor") != "screen" or not isinstance(raw.get("modes"), list):
        raise Refused("current screen modes unavailable")
    found = {}
    for item in raw["modes"]:
        if not isinstance(item, dict) or item.get("ansi") is not False:
            continue
        code = item.get("code")
        if type(code) is int and code in MOUSE_CODES:
            if code in found or type(item.get("on")) is not bool:
                raise Refused("duplicate or invalid mouse mode")
            found[code] = item["on"]
    if set(found) != set(MOUSE_CODES):
        raise Refused("incomplete current mouse modes")
    return found


class OutputLedger:
    """Durable original output intent; UNKNOWN is never a retry permission."""
    def __init__(self, path):
        self.path = Path(path)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Refused("unsafe mouse ledger")
        finally:
            os.close(fd)
        self.connection = sqlite3.connect(str(self.path), timeout=0.5, check_same_thread=False)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS episodes (key TEXT PRIMARY KEY, sequence INTEGER NOT NULL, phase TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS events (key TEXT NOT NULL, sequence INTEGER NOT NULL, phase TEXT NOT NULL, proof TEXT NOT NULL, result TEXT, PRIMARY KEY (key, sequence))")
        self.connection.commit()
        self.lock = threading.Lock()

    def phase(self, key):
        with self.lock:
            row = self.connection.execute("SELECT phase FROM episodes WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def reserve(self, key, proof):
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("SELECT sequence, phase FROM episodes WHERE key=?", (key,)).fetchone()
            if row and row[1] != "verified_off":
                return None
            sequence = row[0] + 1 if row else 1
            evidence = {"at_epoch": time.time(), "proof": proof, "controls_hex": CONTROLS.hex(),
                        "input_operations": 0, "output_budget": 1,
                        "authorization": "User requested automatic repair of recurring cmux Codex mouse reports."}
            self.connection.execute("INSERT INTO events VALUES (?, ?, 'intent', ?, NULL)",
                                    (key, sequence, json.dumps(evidence, sort_keys=True)))
            self.connection.execute("INSERT INTO episodes VALUES (?, ?, 'intent') ON CONFLICT(key) DO UPDATE SET sequence=excluded.sequence, phase=excluded.phase", (key, sequence))
            return sequence

    def finish(self, key, sequence, result):
        phase = "verified_off" if result.get("status") == "verified_off" else "unknown"
        with self.lock, self.connection:
            row = self.connection.execute("SELECT sequence, phase FROM episodes WHERE key=?", (key,)).fetchone()
            if row != (sequence, "intent"):
                raise Refused("original mouse intent changed")
            self.connection.execute("UPDATE events SET phase=?, result=? WHERE key=? AND sequence=? AND phase='intent'",
                                    (phase, json.dumps(result, sort_keys=True), key, sequence))
            self.connection.execute("UPDATE episodes SET phase=? WHERE key=? AND sequence=?", (phase, key, sequence))

    def close(self):
        self.connection.close()


class NativeBackend:
    """Use actual CCC admission, native reader, topology and kernel TTY checks."""
    def __init__(self, daemon, core, client):
        self.daemon, self.core, self.client = daemon, core, client

    def records(self):
        tree = self.client.tree()
        return self.core.display_surface_records(tree, self.core.main_surface_records(tree))

    def authorized(self, record):
        if getattr(self.daemon, "stop_requested", False):
            raise Refused("daemon stopping")
        path = self.daemon.config_path
        if path.is_symlink():
            raise Refused("symlink policy")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read()
            after = os.fstat(handle.fileno())
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or stamp(before) != stamp(after)
                or stamp(after) != stamp(path.stat(follow_symlinks=False))):
            raise Refused("unstable policy")
        config = self.core.validate_config(json.loads(raw))
        if config.get("global_paused") or config.get("mode") != "armed":
            raise Refused("global pause")
        with self.daemon._targets_lock:
            dynamic = copy.deepcopy(list(self.daemon.dynamic_targets.values()))
        rows = [t for t in self.core.effective_targets(config, dynamic)
                if t.get("surface_id") == record.get("surface_id")]
        if len(rows) != 1:
            raise Refused("not enrolled")
        target = rows[0]
        sid, wid = record["surface_id"], record["workspace_id"]
        rules = [r for r in config.get("workspace_rules", []) if r.get("workspace_id") == wid]
        if (target.get("source") in {"workspace_rule", "codex_auto", "claude_auto", "pane_follow"}
                and not self.core.dynamic_target_authorized(config, target)):
            raise Refused("original discovery grant revoked")
        if any(sid in r.get("excluded_surface_ids", []) or self.core.batch_start_hold(r, sid) for r in rules):
            raise Refused("workspace exclusion or startup hold")
        stop_marked = getattr(self.daemon, "_workspace_stop_marked", None)
        if stop_marked is not None and stop_marked(wid):
            raise Refused("workspace operator stop")
        if (target.get("workspace_id") != record.get("workspace_id")
                or (target.get("pane_id") and target["pane_id"] != record.get("pane_id"))
                or target.get("paused") or not target.get("enabled", True)
                or record["surface_id"] in self.daemon._local_paused_surface_ids):
            raise Refused("placement or enrollment pause")
        runtime_lock = getattr(self.daemon, "_runtime_lock", None)
        if runtime_lock is not None:
            with runtime_lock:
                runtime = self.daemon.runtime.get(sid)
                if runtime and (getattr(runtime, "state", "") == "delivery_unknown"
                        or getattr(runtime, "codex_input_phase", "") in {
                            "goal_native_pending", "paste_submit_pending", "enter_pending", "text_retained"}):
                    raise Refused("original pending input or delivery uncertainty preserved")
        return hashlib.sha256(raw).hexdigest()

    def standalone_selection(self, pid, session_id, transcript_path):
        """Older direct writers select their original thread through its held lock.

        This uses complete native descriptor inventories and vnode identities,
        never an absent publication alone or a rollout filename search.
        """
        from ccc_shared_codex_turn import writer_files
        root = self.daemon.codex_queue_recovery.sessions_root.resolve(strict=True)
        path = Path(transcript_path)
        if (not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
                or not path.is_relative_to(root)):
            raise Refused("original standalone rollout locator unavailable")
        files = writer_files(pid)
        locks = [p for p in files if p.parent.name == "thread-writer-locks" and p.suffix == ".lock"]
        if len(locks) != 1 or locks[0].name != session_id + ".lock":
            raise Refused("original standalone selected writer lock unavailable")
        lock = locks[0]
        info, rollout = lock.lstat(), path.lstat()
        vnode = lambda st: {"device": st.st_dev, "inode": st.st_ino}
        if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(rollout.st_mode)
                or lock.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022
                or files[lock] != vnode(info)):
            raise Refused("standalone writer or rollout vnode changed")
        rollouts = {p for p in files if p.suffix == ".jsonl" and p.is_relative_to(root)}
        if rollouts - {path} or (path in files and files[path] != vnode(rollout)):
            raise Refused("standalone writer has another original rollout")
        return {"lock_path": str(lock), "lock_vnode": vnode(info),
                "transcript_path": str(path), "transcript_vnode": vnode(rollout), "sessions_root": str(root)}

    def probe(self, original):
        import ccc_native_processes as native
        surface = original["surface_id"]
        rows = [r for r in self.records() if r.get("surface_id") == surface]
        if len(rows) != 1:
            raise Refused("surface missing or ambiguous")
        target = rows[0]
        for field in ("surface_id", "workspace_id", "pane_id"):
            if target.get(field) != original.get(field):
                raise Refused("original placement changed")
        policy = self.authorized(target)
        label = self.daemon._native_process_index.lookup(target)
        if not label or label.get("agent_kind") != "codex" or len(label.get("agent_pids", [])) != 1:
            raise Refused("local Codex identity unknown")
        pid = label["agent_pids"][0]
        row = native.display_process(pid)
        tty = target.get("native_display_target", {}).get("tty") or target.get("tty")
        if not isinstance(tty, str) or not re.fullmatch(r"ttys[0-9]{3}", tty):
            raise Refused("TTY unknown")
        display = {"surface_id": surface, "workspace_id": target["workspace_id"],
                   "pane_id": target["pane_id"], "tty": tty}
        if (not row or row.get("agent_kind") != "codex"
                or tuple(row.get("birth", ())) != tuple(label.get("agent_birth", ()))
                or not native.display_identity_matches({"process": row, "target": display})):
            raise Refused("native foreground TTY changed")
        turn = self.daemon.codex_queue_recovery.current_turn(target)
        selection = native.foreground_generation(pid)
        if (not turn or turn.get("kind") not in KNOWN_KINDS or turn.get("pid") != pid
                or tuple(turn.get("birth", ())) != tuple(row["birth"])
                or len(selection) != 3
                or tuple(label.get("foreground_generation", ())) != tuple(selection)):
            raise Refused("authentic original session reader unavailable")
        standalone = None
        if selection[0] == "ok":
            if selection[1] != turn.get("session_id"):
                raise Refused("original native foreground session changed")
        elif selection == ("absent", None, None) and not turn.get("shared_writer"):
            standalone = self.standalone_selection(pid, turn["session_id"], turn["transcript_path"])
        else:
            raise Refused("original selected session unavailable")
        identity = {k: turn.get(k) for k in ("session_id", "pid", "birth", "transcript_path", "shared_writer")}
        proof = {"target": display, "process": row, "selection": selection,
                 "identity": identity, "policy_sha256": policy}
        if standalone is not None:
            proof["standalone_selection"] = standalone
        # Normalize tuples exactly once, for durable comparisons across restarts.
        proof = json.loads(json.dumps(proof))
        if not self.valid(proof):
            raise Refused("native identity changed after original reader")
        return proof

    def valid(self, proof):
        import ccc_native_processes as native
        rows = [r for r in self.records() if r.get("surface_id") == proof["target"]["surface_id"]]
        if len(rows) != 1:
            return False
        target = rows[0]
        if any(target.get(k) != proof["target"].get(k) for k in ("surface_id", "workspace_id", "pane_id")):
            return False
        tty = target.get("native_display_target", {}).get("tty") or target.get("tty")
        if tty != proof["target"]["tty"] or self.authorized(target) != proof["policy_sha256"]:
            return False
        return self.fast_valid(proof)

    def fast_valid(self, proof):
        """No RPC or rollout scan while holding the short workspace lease."""
        if getattr(self.daemon, "stop_requested", False):
            return False
        import ccc_native_processes as native
        if self.authorized(proof["target"]) != proof["policy_sha256"]:
            return False
        row = {**proof["process"], "birth": tuple(proof["process"]["birth"])}
        if (not native.display_identity_matches({"process": row, "target": proof["target"]})
                or list(native.foreground_generation(row["pid"])) != proof["selection"]):
            return False
        if "standalone_selection" in proof:
            identity = proof["identity"]
            return self.standalone_selection(row["pid"], identity["session_id"],
                identity["transcript_path"]) == proof["standalone_selection"]
        return True

    def modes(self, proof):
        target = proof["target"]
        frame = self.client.replay(target["workspace_id"], target["surface_id"], live=True, timeout=1.0)
        return mode_values(self.core.Grid.from_rpc(frame, target["surface_id"]))

    @contextlib.contextmanager
    def output_lock(self, proof):
        # Pause drains take this same workspace lock exclusively. Output takes
        # a nonblocking shared lease; it never delays a user pause for polling.
        wid = proof["target"]["workspace_id"]
        name = "workspace-input-" + hashlib.sha256(wid.encode()).hexdigest()[:24] + ".lock"
        fd = os.open(self.daemon.config_path.parent / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def open_tty(self, proof):
        path = "/dev/" + proof["target"]["tty"]
        fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info, linked = os.fstat(fd), os.stat(path, follow_symlinks=False)
            if (not stat.S_ISCHR(info.st_mode) or info.st_rdev != proof["process"]["tdev"]
                    or (info.st_dev, info.st_ino, info.st_rdev) != (linked.st_dev, linked.st_ino, linked.st_rdev)):
                raise Refused("original TTY descriptor changed")
            return fd
        except BaseException:
            os.close(fd)
            raise

    write = staticmethod(os.write)
    close_tty = staticmethod(os.close)


class TerminalMouseGuard:
    def __init__(self, ledger, backend_factory, *, interval=1.0, on_error=None):
        self.ledger, self.backend_factory = ledger, backend_factory
        self.interval, self.on_error = interval, on_error
        self.stop = threading.Event()
        self.thread = None
        self.last_sweep = None
        self._rotation = 0
        self.output_operations = 0

    def observe(self, backend, target, *, apply=True):
        result = {"surface_id": target.get("surface_id"), "input_operations": 0, "output_operations": 0}
        key = sequence = None
        try:
            if self.stop.is_set():
                raise Refused("mouse guard stopping")
            # A mode read grants no output authority. Avoid reading every large
            # active rollout each second when the terminal is already OFF.
            values = backend.modes({"target": target})
            result["before_modes"] = values
            if not any(values[code] for code in GENERATING_CODES):
                result["status"] = "already_off"
                return result
            proof = backend.probe(target)
            key = identity_key(proof)
            if self.ledger.phase(key) not in {None, "verified_off"}:
                result["status"] = "original_output_unknown_preserved"
                return result
            result.update(status="ready_output_only", proof=proof)
            if not apply:
                return result
            fd = backend.open_tty(proof)
            try:
                fresh = backend.probe(target)
                if fresh != proof:
                    raise Refused("original identity or policy changed before output")
                current = backend.modes(fresh)
                if not any(current[code] for code in GENERATING_CODES):
                    result["status"] = "became_off"
                    return result
                if not backend.valid(fresh):
                    raise Refused("authorization changed before intent")
                with backend.output_lock(proof):
                    if not backend.fast_valid(fresh):
                        raise Refused("authorization changed before short output lease")
                    if self.stop.is_set():
                        raise Refused("mouse guard stopping before original intent")
                    sequence = self.ledger.reserve(key, {**fresh, "before_modes": current})
                    if sequence is None:
                        result["status"] = "original_output_unknown_preserved"
                        return result
                    result["sequence"] = sequence
                    if self.stop.is_set() or not backend.fast_valid(fresh):
                        raise Refused("authorization changed after original intent")
                    # Exactly one write to the slave: emulator output, never
                    # the master's agent input. No key, text, focus or flush.
                    result["output_operations"] = 1
                    count = backend.write(fd, CONTROLS)
                    result["bytes_written"] = count
                # Slow terminal verification and result fsync occur after the
                # lease. A stuck observer must not delay Goal Enter or a pause.
                result["status"] = "unknown"
                if count == len(CONTROLS):
                    after = backend.modes(fresh)
                    result["after_modes"] = after
                    if not any(after.values()) and backend.valid(fresh):
                        result["status"] = "verified_off"
            finally:
                backend.close_tty(fd)
        except Exception as exc:
            result.update(status="unknown" if sequence is not None else "refused_before_output",
                          error=type(exc).__name__ + ": " + str(exc))
        finally:
            if sequence is not None:
                self.ledger.finish(key, sequence, result)
        return result

    def sweep(self, *, apply=True, budget=3.0):
        started = time.monotonic()
        backend = self.backend_factory()
        # The independent native index limits reads to real local Codex hints.
        records = backend.records()
        candidates = []
        for record in records:
            label = backend.daemon._native_process_index.lookup(record)
            if label and label.get("agent_kind") == "codex":
                candidates.append(record)
        if candidates:
            offset = self._rotation % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        rows = []
        for target in candidates:
            if self.stop.is_set() or time.monotonic() - started >= budget:
                break
            rows.append(self.observe(backend, target, apply=apply))
        self._rotation += max(1, len(rows))
        self.last_sweep = {"at_epoch": time.time(), "elapsed_seconds": time.monotonic() - started,
                           "candidate_count": len(candidates), "observed_count": len(rows), "rows": rows,
                           "input_operations": 0, "output_operations": sum(r["output_operations"] for r in rows)}
        self.output_operations += self.last_sweep["output_operations"]
        return self.last_sweep

    def summary(self):
        sweep = self.last_sweep or {}
        return {"thread_alive": bool(self.thread and self.thread.is_alive()),
                "observed_at": sweep.get("at_epoch"), "elapsed_seconds": sweep.get("elapsed_seconds"),
                "candidate_count": sweep.get("candidate_count"), "observed_count": sweep.get("observed_count"),
                "rows": [{"surface_id": r["surface_id"], "status": r["status"],
                          "output_operations": r["output_operations"], "error": r.get("error")}
                         for r in sweep.get("rows", [])],
                "input_operations": 0, "output_operations_since_start": self.output_operations,
                "limits": "Polling suppression; host mode changes can occur between observations. UNKNOWN output is held."}

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self.sweep()
                except Exception as exc:
                    if self.on_error:
                        self.on_error(exc)
                self.stop.wait(max(0.1, self.interval - (time.monotonic() - started)))
        self.thread = threading.Thread(target=run, name="ccc-terminal-mouse", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        # A still-finishing output keeps its original durable intent. Do not
        # close its SQLite connection under it or replay after daemon exit.
        if self.thread is None or not self.thread.is_alive():
            self.ledger.close()
