"""Cross-process, timestamped cmux discovery snapshots (never input grants).

The watcher normally collects them. Readers may take over after its heartbeat
expires. A nonblocking file lock coalesces misses, including failed requests,
so opening more panels or batches cannot multiply process-table scans.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time


class InventoryUnavailable(RuntimeError):
    pass


class SharedInventory:
    def __init__(self, directory, *, owner=False, clock=time.time):
        self.root = Path(directory) / "inventory"
        self.owner, self.clock = owner, clock
        self._heartbeat_at = 0.0
        self._files = {}

    def _read(self, name):
        path = self.root / f"{name}.json"
        try:
            stat = path.stat()
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            old = self._files.get(name)
            if old and old[0] == stamp:
                return old[1]
            value = json.loads(path.read_bytes())
            self._files[name] = (stamp, value)
            return value
        except (OSError, ValueError):
            return {}

    def _write(self, name, value):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=f".{name}-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(value, out, separators=(",", ":"))
            os.replace(path, self.root / f"{name}.json")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    def heartbeat(self):
        now = self.clock()
        if self.owner and now - self._heartbeat_at >= 1:
            self._write("owner", {"pid": os.getpid(), "at": now})
            self._heartbeat_at = now

    def _owner_alive(self):
        record = self._read("owner")
        if not 0 <= self.clock() - record.get("at", 0) < 10:
            return False
        try:
            os.kill(int(record["pid"]), 0)
            return True
        except (OSError, ValueError, KeyError):
            return False

    def peek(self, name, *, max_age):
        """A bounded last good value, for display only when max_age > TTL."""
        record = self._read(name)
        age = self.clock() - record.get("collected_at", 0)
        return record.get("value") if 0 <= age <= max_age else None

    def get(self, name, loader, *, ttl):
        self.heartbeat()
        now = self.clock()
        record = self._read(name)
        if record.get("value") is not None and 0 <= now - record.get("collected_at", 0) < ttl:
            return record["value"]
        if record.get("error") and 0 <= now - record.get("attempt_at", 0) < ttl:
            raise InventoryUnavailable(record["error"])
        # Tree reads do not scan the OS process table. Let a batch fill the
        # one-second topology gap when the watcher's next discovery is due in
        # five seconds; the same file lock still bounds it to one collection.
        if name == "top" and not self.owner and self._owner_alive():
            raise InventoryUnavailable(f"{name} refresh pending")
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{name}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InventoryUnavailable(f"{name} refresh pending") from None
            # Another process may have completed between the read and lock.
            record = self._read(name)
            now = self.clock()
            if record.get("value") is not None and 0 <= now - record.get("collected_at", 0) < ttl:
                return record["value"]
            if 0 <= now - record.get("attempt_at", 0) < ttl:
                raise InventoryUnavailable(record.get("error") or f"{name} refresh pending")
            try:
                value = loader()
            except Exception as exc:
                self._write(name, {**record, "attempt_at": self.clock(), "error": str(exc)})
                raise InventoryUnavailable(str(exc)) from exc
            self._write(name, {"value": value, "collected_at": self.clock(),
                               "attempt_at": now, "error": ""})
            return value
