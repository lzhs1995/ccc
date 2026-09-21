"""Bounded per-surface scheduling and shared, read-only cmux snapshots.

No process discovery, terminal input or filesystem writes happen in this
module. The watcher supplies those operations; tests use the same scheduler
with controlled clocks and clients.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Mapping


@dataclasses.dataclass
class _Slot:
    target: dict[str, Any]
    key: Any
    due: float
    enabled: bool = True
    future: Future | None = None
    submitted_key: Any = None
    phase: str = "idle"
    started: float = 0.0
    candidate: Any = None
    ready_at: float = 0.0
    revision: int = 0


class SurfaceScheduler:
    """One job per UUID, with independent observation and send capacity.

    tick() never waits for I/O. Executors never receive more work than their
    capacity: ready work stays in slots, where removal/pause can invalidate it.
    A slow observation or send therefore cannot create a batch barrier or an
    unbounded executor queue. Callbacks must recheck is_current before input.
    """

    def __init__(self, observe: Callable, send: Callable, *, interval: float = 1,
                 observe_workers: int = 32, send_workers: int = 8,
                 clock: Callable[[], float] = time.monotonic,
                 on_dispatch: Callable | None = None,
                 on_error: Callable | None = None):
        self.observe, self.send, self.clock = observe, send, clock
        self.interval = interval
        self.observe_workers, self.send_workers = observe_workers, send_workers
        self.on_dispatch, self.on_error = on_dispatch, on_error
        self._observe_pool = ThreadPoolExecutor(observe_workers, thread_name_prefix="ccc-read")
        self._send_pool = ThreadPoolExecutor(send_workers, thread_name_prefix="ccc-send")
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.wakeup = threading.Event()

    def _current(self, sid, key):
        with self._lock:
            slot = self._slots.get(sid)
            return bool(not self._closed and slot and slot.enabled and (slot.key, slot.revision) == key)

    def tick(self, targets, *, generation: Any = None, interval: float | None = None):
        now = self.clock()
        with self._lock:
            if self._closed:
                return
            if interval is not None:
                self.interval = interval
            active = {str(t["surface_id"]): dict(t) for t in targets
                      if t.get("enabled", True) and not t.get("paused", False)}
            for sid, slot in self._slots.items():
                if slot.enabled and sid not in active:
                    slot.revision += 1
                slot.enabled = sid in active
                if not slot.enabled:
                    slot.candidate = None
                    if slot.future:
                        slot.future.cancel()
            for sid, target in active.items():
                key = (generation, str(target.get("workspace_id")),
                       str(target.get("source")), str(target.get("source_workspace_id")))
                slot = self._slots.get(sid)
                if slot is None:
                    self._slots[sid] = _Slot(target, key, now)
                else:
                    if slot.key != key:
                        slot.revision += 1
                        slot.due = now
                        slot.candidate = None
                        if slot.future:
                            slot.future.cancel()
                        elif slot.phase == "ready":
                            slot.phase = "idle"
                    slot.target, slot.key, slot.enabled = target, key, True

            # Only consume completed futures. No future.result() on live work.
            for sid, slot in list(self._slots.items()):
                future = slot.future
                if future is not None and future.done():
                    slot.future = None
                    previous_phase, slot.phase = slot.phase, "idle"
                    current = slot.enabled and (slot.key, slot.revision) == slot.submitted_key
                    try:
                        result = None if future.cancelled() else future.result()
                    except Exception as exc:
                        result = None
                        if current and self.on_error:
                            self.on_error(slot.target, previous_phase, exc)
                    if current and previous_phase == "observe" and result is not None:
                        slot.candidate, slot.ready_at, slot.phase = result, now, "ready"
                    else:
                        # Retain cadence, but never replay missed ticks in a burst.
                        slot.due = max(slot.started + self.interval, now)
                if not slot.enabled and slot.future is None:
                    del self._slots[sid]

            reads = sum(s.phase == "observe" for s in self._slots.values())
            sends = sum(s.phase == "send" for s in self._slots.values())
            ready = sorted(((sid, s) for sid, s in self._slots.items()
                            if s.enabled and s.phase == "ready"), key=lambda pair: pair[1].ready_at)
            for sid, slot in ready[:max(0, self.send_workers - sends)]:
                self._submit(sid, slot, "send", now)
            due = sorted(((sid, s) for sid, s in self._slots.items()
                          if s.enabled and s.phase == "idle" and s.due <= now),
                         key=lambda pair: pair[1].due)
            for sid, slot in due[:max(0, self.observe_workers - reads)]:
                self._submit(sid, slot, "observe", now)

    def _submit(self, sid, slot, phase, now):
        key = (slot.key, slot.revision)
        current = lambda: self._current(sid, key)
        if self.on_dispatch:
            due = slot.due if phase == "observe" else slot.ready_at
            self.on_dispatch(slot.target, phase, max(0, now - due))
        slot.phase, slot.submitted_key = phase, key
        if phase == "observe":
            slot.started = now
            slot.future = self._observe_pool.submit(self.observe, dict(slot.target), current)
        else:
            candidate, slot.candidate = slot.candidate, None
            slot.future = self._send_pool.submit(self.send, dict(slot.target), candidate, current)
        slot.future.add_done_callback(lambda _: self.wakeup.set())

    def snapshot(self):
        with self._lock:
            return {"targets": sum(s.enabled for s in self._slots.values()),
                    "observing": sum(s.phase == "observe" for s in self._slots.values()),
                    "sending": sum(s.phase == "send" for s in self._slots.values()),
                    "ready_to_send": sum(s.phase == "ready" for s in self._slots.values())}

    def wait_timeout(self, maximum=0.1):
        """Wake at the next deadline instead of rounding it to a polling tick."""
        with self._lock:
            if self._closed:
                return 0.0
            if sum(s.phase == "observe" for s in self._slots.values()) >= self.observe_workers:
                return maximum  # Completion callbacks wake us when capacity returns.
            due = [s.due for s in self._slots.values() if s.enabled and s.phase == "idle"]
            return min(maximum, max(0, min(due) - self.clock())) if due else maximum

    def close(self, *, wait=True):
        with self._lock:
            self._closed = True
            self.wakeup.set()
        self._observe_pool.shutdown(wait=wait, cancel_futures=True)
        self._send_pool.shutdown(wait=wait, cancel_futures=True)


@dataclasses.dataclass
class _Snapshot:
    value: Any = None
    completed: float = float("-inf")
    future: Future | None = None
    error: Exception | None = None
    failed: float = float("-inf")
    source: Any = None


class SnapshotCache:
    """Share one refresh per key, including concurrent cold-cache callers.

    Observation may request a nonblocking refresh and route conservatively
    until it finishes. Discovery can await that *same* refresh. A synchronous
    first caller does its own I/O, so send preflight tree reads cannot get stuck
    behind queued background process scans. Failures are cached for one second.
    """

    def __init__(self, *, workers=2, clock=time.monotonic, failure_ttl=1.0):
        self.clock, self.failure_ttl = clock, failure_ttl
        self._lock = threading.RLock()
        self._entries: dict[Any, _Snapshot] = {}
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="ccc-snapshot")
        self._closed = False

    def get(self, key, loader, *, ttl, wait=True, source=None):
        owner = False
        with self._lock:
            if self._closed:
                return None
            entry = self._entries.get(key)
            if entry is None or entry.source is not source:
                entry = self._entries[key] = _Snapshot(source=source)
            now = self.clock()
            if entry.error is not None and now - entry.failed < self.failure_ttl:
                if wait:
                    raise entry.error
                return None
            if entry.value is not None and entry.error is None and now - entry.completed < ttl:
                return entry.value
            if entry.future is None:
                entry.future = Future()
                owner = True
            future = entry.future
        if owner:
            if wait:
                self._load(entry, future, loader)
            else:
                self._pool.submit(self._load, entry, future, loader)
        return future.result() if wait else (future.result() if future.done() and not future.exception() else None)

    def _load(self, entry, future, loader):
        try:
            value = loader()
        except Exception as exc:
            with self._lock:
                entry.error, entry.failed = exc, self.clock()
                entry.future = None
                future.set_exception(exc)
        else:
            with self._lock:
                entry.value, entry.completed, entry.error = value, self.clock(), None
                entry.future = None
                future.set_result(value)

    def peek(self, key, *, ttl):
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry.error is None and self.clock() - entry.completed < ttl:
                return entry.value
            return None

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=False)


class SnapshotClient:
    """Per-worker client; process/topology snapshots are shared across workers."""

    def __init__(self, client, cache: SnapshotCache):
        self.client, self.cache = client, cache

    def __getattr__(self, name):
        return getattr(self.client, name)

    def tree(self):
        return self.cache.get(("tree",), self.client.tree, ttl=1.0)

    def top(self, workspace_id):
        return self.cached_top(workspace_id, wait=True)

    def cached_top(self, workspace_id, *, wait=False):
        if callable(getattr(self.client, "top_all", None)):
            # cmux top scans the process table even with a workspace filter.
            # Share one fleet scan; callers still join by exact surface UUID.
            return self.cache.get(("top",), self.client.top_all, ttl=5.0, wait=wait)
        return self.cache.get(("top", workspace_id), lambda: self.client.top(workspace_id), ttl=5.0, wait=wait)

    def top_all(self):
        return self.cache.get(("top",), self.client.top_all, ttl=5.0)

    def process_labels(self, workspace_id, classify, *, wait=False):
        top = self.cached_top(workspace_id, wait=wait)
        if top is None:
            return None
        # A bounded derived entry follows the exact raw snapshot. A new top
        # result invalidates old labels immediately, even within their TTL.
        key = ("labels",) if callable(getattr(self.client, "top_all", None)) else ("labels", workspace_id)
        return self.cache.get(key, lambda: classify(top),
                              ttl=5.0, wait=wait, source=top)


class CoalescingWriter:
    """Run an injected durable write once for a batch of concurrent callers.

    A waiting caller returns only after a write that started after its request.
    Requests arriving during I/O require a subsequent write. Failures reach
    every affected waiter, so failed persistence can never authorize input.
    Nonblocking requests let periodic publishing stay off the scheduler thread.
    """

    def __init__(self, write, *, delay=0.003, name="ccc-state", on_error=None):
        self.write, self.delay, self.on_error = write, delay, on_error
        self._condition = threading.Condition()
        self._pending = False
        self._waiters: list[Future] = []
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def request(self, *, wait=True):
        future = Future() if wait else None
        with self._condition:
            if self._closed:
                raise RuntimeError("state writer is closed")
            self._pending = True
            if future is not None:
                self._waiters.append(future)
            self._condition.notify()
        if future is not None:
            future.result()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if not self._pending:
                    return
                end = time.monotonic() + self.delay
                while not self._closed and time.monotonic() < end:
                    self._condition.wait(max(0, end - time.monotonic()))
                waiters, self._waiters = self._waiters, []
                self._pending = False
            try:
                self.write()
            except Exception as exc:
                for future in waiters:
                    future.set_exception(exc)
                if self.on_error:
                    self.on_error(exc)
            else:
                for future in waiters:
                    future.set_result(None)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join()
