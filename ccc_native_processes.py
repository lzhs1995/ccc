"""Read-only local Codex discovery, independent of cmux's GUI process scan.

These short-lived hints never authorize input. The watcher still joins a
current cmux surface/workspace and verifies the original native writer and
failed turn before sending. No shell process or model request is started.
"""
from __future__ import annotations

import threading
import time


def local_processes():
    import ccc_guard_scope as scope

    rows = []
    for row in scope.scan():
        if row.get("remote") or row.get("backend"):
            continue
        try:
            argv, _ = scope.arguments(row["pid"])
        except (OSError, ValueError, RuntimeError):
            continue
        options = argv[1:argv.index("--")] if "--" in argv else argv[1:]
        # Conservative filtering also excludes noninteractive child commands
        # launched by another agent in the same terminal. A missed hint keeps
        # the existing cmux discovery path; it is never proof of process exit.
        if (any(a == "--remote" or a.startswith("--remote=") for a in options)
                or set(options) & {"exec", "e", "review", "mcp", "mcp-server", "app-server",
                                   "login", "logout", "completion", "sandbox", "debug", "apply",
                                   "cloud", "features", "--version", "-V", "--help", "-h"}
                or scope.birth(row["pid"], codex=True) != row["birth"]):
            continue
        rows.append(row)
    return rows


class NativeProcessIndex:
    """One independent, bounded refresh; readers only copy an in-memory map."""

    def __init__(self, *, loader=local_processes, clock=time.monotonic, interval=2, max_age=5):
        self.loader, self.clock = loader, clock
        self.interval, self.max_age = interval, max_age
        self._lock = threading.Lock()
        self._snapshot, self._started = None, float("-inf")
        self.stop = threading.Event()
        self.thread = None

    def refresh(self):
        started = self.clock()
        try:
            rows = self.loader()
            groups = {}
            for row in rows:
                sid, wid = row["surface_id"], row["environment_workspace_id"]
                groups.setdefault(sid, []).append((wid, row["pid"]))
            snapshot = {
                sid: {"workspace_id": group[0][0], "agent_kind": "codex",
                      "agent_pids": [group[0][1]], "summary": "local native process hint"}
                for sid, group in groups.items() if len(group) == 1
            }
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            snapshot = None
        with self._lock:
            # Age from the start: a slow/incomplete refresh cannot publish old
            # ownership as fresh when it finally returns.
            self._snapshot, self._started = snapshot, started

    def snapshot(self):
        with self._lock:
            if self._snapshot is None or not 0 <= self.clock() - self._started <= self.max_age:
                return None
            return dict(self._snapshot)

    def lookup(self, target):
        # Single-target checks are frequent while GUI discovery is stalled.
        # Avoid copying the whole fleet map for each original-session check.
        with self._lock:
            if self._snapshot is None or not 0 <= self.clock() - self._started <= self.max_age:
                return None
            row = self._snapshot.get(str(target.get("surface_id")))
            if row and row["workspace_id"] == str(target.get("workspace_id")):
                return dict(row)
        return None

    def start(self):
        def run():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=run, name="ccc-native-discovery", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
