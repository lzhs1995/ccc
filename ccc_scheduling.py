"""Bounded per-surface scheduling and shared, read-only cmux snapshots.

No process discovery, terminal input or filesystem writes happen in this
module. The watcher supplies those operations; tests use the same scheduler
with controlled clocks and clients.
"""
from __future__ import annotations

import dataclasses
import copy
import math
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Mapping


@dataclasses.dataclass
class _Slot:
    target: dict[str, Any]
    key: Any
    due: float
    cadence_anchor: float = 0.0
    enabled: bool = True
    future: Future | None = None
    submitted_key: Any = None
    phase: str = "idle"
    started: float = 0.0
    completed_at: float | None = None
    candidate: Any = None
    ready_at: float = 0.0
    revision: int = 0
    urgent_at: float | None = None
    observation_interval: float = 1.0


class SurfaceScheduler:
    """One job per UUID, with independent observation and send capacity.

    tick() never waits for I/O. Executors never receive more work than their
    capacity: ready work stays in slots, where removal/pause can invalidate it.
    A slow observation or send therefore cannot create a batch barrier or an
    unbounded executor queue. Callbacks must recheck is_current before input.
    """

    def __init__(self, observe: Callable, send: Callable, *, interval: float = 1,
                 observe_workers: int = 32, send_workers: int = 8,
                 event_workers: int = 0,
                 event_handler: Callable | None = None,
                 supersede_reads: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 on_dispatch: Callable | None = None,
                 on_error: Callable | None = None,
                 observation_interval: Callable | None = None):
        self.observe, self.send, self.clock = observe, send, clock
        self.interval = interval
        self.observe_workers, self.send_workers = observe_workers, send_workers
        self.event_workers = event_workers
        self.event_handler, self.supersede_reads = event_handler, supersede_reads
        self._retired_reads: dict[str, Future] = {}
        self.on_dispatch, self.on_error = on_dispatch, on_error
        self.observation_interval = observation_interval
        self._observe_pool = ThreadPoolExecutor(observe_workers, thread_name_prefix="ccc-read")
        self._send_pool = ThreadPoolExecutor(send_workers, thread_name_prefix="ccc-send")
        self._event_pool = (ThreadPoolExecutor(event_workers, thread_name_prefix="ccc-event")
                            if event_workers else None)
        self._slots: dict[str, _Slot] = {}
        self._registrations = {}
        self._publication = None
        self._membership_refresh_at = 0.0
        self._tokens = {}
        self._hints = queue.SimpleQueue()
        self._lock = threading.RLock()
        self._closed = False
        self._urgent_streak = 0
        self.wakeup = threading.Event()

    def request_observation(self, surface_id: str, workspace_id: str) -> bool:
        """A native completion is a scheduling hint, never input permission.

        Keep hints arriving during I/O until a subsequent fresh observation.
        UUID/workspace matching and normal generation checks still apply.
        """
        if self._closed or self._registrations.get(surface_id) != workspace_id:
            return False
        # File-event ingestion must never wait while tick starts I/O workers.
        # Membership is checked again while draining, before any action.
        self._hints.put((surface_id, workspace_id, self.clock()))
        self.wakeup.set()
        return True

    def _drain_hints(self):
        changed = False
        while True:
            try:
                surface_id, workspace_id, at = self._hints.get_nowait()
            except queue.Empty:
                return changed
            slot = self._slots.get(surface_id)
            if (slot is None or not slot.enabled
                    or str(slot.target.get("workspace_id")) != workspace_id):
                continue
            if slot.urgent_at is None:
                slot.urgent_at = at
            if (self.supersede_reads and self._event_pool is not None
                    and slot.phase == "observe" and slot.future is not None
                    and surface_id not in self._retired_reads):
                # Invalidate the read's permission before replacing its slot.
                # It may finish I/O, but every late effect must fail current().
                # Keep its real worker occupied until it returns; never create
                # an unbounded queue of abandoned reads for this UUID.
                self._retired_reads[surface_id] = slot.future
                slot = dataclasses.replace(slot, future=None, phase="idle",
                    revision=slot.revision + 1, candidate=None, completed_at=None)
                self._slots[surface_id] = slot
                changed = True
            slot.due = min(slot.due, slot.urgent_at)

    def _current(self, sid, key):
        # Immutable published tokens avoid a fleet-wide lock at each native
        # proof/fsync/input boundary. Disk authorization is checked by send.
        return not self._closed and self._tokens.get(sid) == key

    def tick(self, targets, *, generation: Any = None, interval: float | None = None,
             publication: Any = None):
        now = self.clock()
        with self._lock:
            if self._closed:
                return
            self._retired_reads = {sid: future for sid, future in self._retired_reads.items()
                                   if not future.done()}
            interval_changed = interval is not None and interval != self.interval
            if interval is not None:
                self.interval = interval
            # A publication token pins an immutable target/config generation.
            # Send authorization stays live. Legacy callers without a token
            # still reconcile on every tick. Refresh native coverage leases
            # within 50 ms even when registration itself has not changed.
            refresh = (publication is None or publication is not self._publication
                       or interval_changed or now >= self._membership_refresh_at)
            if refresh:
                active = {str(t["surface_id"]): dict(t) for t in targets
                          if t.get("enabled", True) and not t.get("paused", False)}
                for sid, slot in self._slots.items():
                    if slot.enabled and sid not in active:
                        slot.revision += 1
                    slot.enabled = sid in active
                    if not slot.enabled:
                        slot.candidate = None
                        slot.urgent_at = None
                        if slot.future:
                            slot.future.cancel()
                for index, (sid, target) in enumerate(active.items()):
                    target_generation = generation(target) if callable(generation) else generation
                    key = (target_generation, str(target.get("workspace_id")),
                           str(target.get("source")), str(target.get("source_workspace_id")))
                    slot = self._slots.get(sid)
                    cadence = (self.observation_interval(target, self.interval)
                               if self.observation_interval else self.interval)
                    if slot is None:
                        self._slots[sid] = _Slot(target, key, now,
                            cadence_anchor=now + cadence * index / max(1, len(active)),
                            observation_interval=cadence)
                    else:
                        if slot.observation_interval != cadence:
                            if cadence < slot.observation_interval:
                                slot.due = min(slot.due, now)
                            slot.observation_interval = cadence
                            slot.cadence_anchor = now + cadence * index / max(1, len(active))
                        if slot.key != key:
                            slot.revision += 1
                            # A config update invalidates an old result, not the
                            # waiting reader's place in line. Resetting every due
                            # time lets the first worker-sized prefix starve peers
                            # whenever a batch keeps updating its configuration.
                            slot.due = min(slot.due, now)
                            slot.cadence_anchor = now + cadence * index / max(1, len(active))
                            slot.candidate = None
                            slot.urgent_at = None
                            if slot.future:
                                slot.future.cancel()
                            elif slot.phase == "ready":
                                slot.phase = "idle"
                        slot.target, slot.key, slot.enabled = target, key, True

                self._registrations = {sid: str(slot.target.get("workspace_id"))
                                       for sid, slot in self._slots.items() if slot.enabled}
                self._publication = publication
                self._membership_refresh_at = now + .05
            hints_changed = self._drain_hints()
            if refresh or hints_changed:
                self._tokens = {sid: (slot.key, slot.revision)
                                for sid, slot in self._slots.items() if slot.enabled}

            # Only consume completed futures. No future.result() on live work.
            for sid, slot in list(self._slots.items()):
                future = slot.future
                if future is not None and future.done():
                    slot.future = None
                    previous_phase, slot.phase = slot.phase, "idle"
                    current = slot.enabled and (slot.key, slot.revision) == slot.submitted_key
                    try:
                        result = None if future.cancelled() else future.result()
                    except Exception:
                        # Errors are reported by the bounded worker itself.
                        # Never wait for a callback's surface/runtime lock while
                        # holding the fleet scheduler lock.
                        result = None
                    if current and previous_phase == "observe" and result is not None:
                        slot.candidate, slot.ready_at, slot.phase = result, now, "ready"
                    else:
                        # First reads are immediate. Subsequent deadlines are
                        # spread over the period and anchored independently of
                        # I/O completion, so an initial worker-sized burst does
                        # not repeat forever or drift after a slow read. Skip
                        # missed periods; never replay them in a catch-up burst.
                        completed = slot.completed_at if slot.completed_at is not None else now
                        cadence = slot.observation_interval
                        slot.due = (slot.cadence_anchor + cadence *
                                    (math.floor((completed - slot.cadence_anchor) / cadence) + 1)
                                    if current and cadence > 0 else now)
                        if current and slot.urgent_at is not None:
                            slot.due = min(slot.due, slot.urgent_at)
                if not slot.enabled and slot.future is None:
                    del self._slots[sid]

            # A failed native turn does not queue behind periodic viewport
            # scans or the ordinary send pool. One slot still owns each UUID;
            # the event operation runs the same observe and final send gates.
            events = sum(s.phase in {"event", "event_send"} for s in self._slots.values())
            if self._event_pool is not None:
                urgent = sorted(((sid, s) for sid, s in self._slots.items()
                                 if s.enabled and s.phase in {"idle", "ready"} and s.urgent_at is not None),
                                key=lambda pair: pair[1].urgent_at)
                for sid, slot in urgent[:max(0, self.event_workers - events)]:
                    self._submit(sid, slot, "event_send" if slot.phase == "ready" else "event", now)

            reads = sum(s.phase == "observe" for s in self._slots.values()) + len(self._retired_reads)
            sends = sum(s.phase == "send" for s in self._slots.values())
            ready = sorted(((sid, s) for sid, s in self._slots.items()
                            if s.enabled and s.phase == "ready"), key=lambda pair: pair[1].ready_at)
            for sid, slot in ready[:max(0, self.send_workers - sends)]:
                self._submit(sid, slot, "send", now)
            due = sorted(((sid, s) for sid, s in self._slots.items()
                          if s.enabled and s.phase == "idle" and s.due <= now
                          and sid not in self._retired_reads),
                         key=lambda pair: pair[1].due)
            urgent = sorted((pair for pair in due if pair[1].urgent_at is not None),
                            key=lambda pair: pair[1].urgent_at)
            regular = [pair for pair in due if pair[1].urgent_at is None]
            for _ in range(max(0, self.observe_workers - reads)):
                # Bound priority traffic so ordinary scans cannot starve even
                # under continuous errors. No extra threads or queued I/O.
                if urgent and (not regular or self._urgent_streak < 3):
                    sid, slot = urgent.pop(0)
                    self._urgent_streak += 1
                elif regular:
                    sid, slot = regular.pop(0)
                    self._urgent_streak = 0
                else:
                    break
                self._submit(sid, slot, "observe", now)

    def _submit(self, sid, slot, phase, now):
        key = (slot.key, slot.revision)
        current = lambda: self._current(sid, key)
        due = slot.due if phase in {"observe", "event"} else slot.ready_at
        delay = max(0, now - due)
        slot.phase, slot.submitted_key = phase, key
        slot.completed_at = None
        if phase == "event":
            slot.urgent_at = None
            slot.started = now
            slot.future = self._event_pool.submit(self._execute, self._react, slot, phase, delay, dict(slot.target), current)
        elif phase == "event_send":
            slot.urgent_at = None
            candidate, slot.candidate = slot.candidate, None
            slot.future = self._event_pool.submit(self._execute, self.send, slot, phase, delay, dict(slot.target), candidate, current)
        elif phase == "observe":
            slot.urgent_at = None
            slot.started = now
            slot.future = self._observe_pool.submit(self._execute, self.observe, slot, phase, delay, dict(slot.target), current)
        else:
            candidate, slot.candidate = slot.candidate, None
            slot.future = self._send_pool.submit(self._execute, self.send, slot, phase, delay, dict(slot.target), candidate, current)
        slot.future.add_done_callback(lambda _: self.wakeup.set())

    def _react(self, target, current):
        if self.event_handler is not None:
            return self.event_handler(target, current)
        candidate = self.observe(target, current)
        if candidate is not None and current():
            if self.on_dispatch:
                self.on_dispatch(target, "send", 0.0)
            self.send(target, candidate, current)

    def _execute(self, operation, slot, phase, delay, *args):
        # Callbacks share this slot's bounded worker, not the deadline thread.
        # A slow callback consumes only that slot's capacity and cannot lock
        # health snapshots, ownership checks or other surfaces' scheduling.
        target, current = args[0], args[-1]
        try:
            if not current():
                return None
            if self.on_dispatch:
                dispatch = "observe" if phase == "event" else "send" if phase == "event_send" else phase
                self.on_dispatch(target, dispatch, delay)
            if current():
                return operation(*args)
        except Exception as exc:
            if current() and self.on_error:
                self.on_error(target, phase, exc)
            return None
        finally:
            # Capture completion before Future.done() becomes visible. The
            # scheduler may consume that future late; consumption time is not
            # the observation's completion time or its next cadence anchor.
            slot.completed_at = self.clock()

    def snapshot(self):
        with self._lock:
            return {"targets": sum(s.enabled for s in self._slots.values()),
                    "native_event_monitored": sum(s.enabled and s.observation_interval > self.interval
                                                   for s in self._slots.values()),
                    "observing": sum(s.phase == "observe" for s in self._slots.values()),
                    "sending": sum(s.phase == "send" for s in self._slots.values()),
                    "native_event_running": sum(s.phase in {"event", "event_send"} for s in self._slots.values()),
                    "native_event_capacity": self.event_workers,
                    "superseded_reads": len(self._retired_reads),
                    "native_event_pending": sum(s.enabled and s.urgent_at is not None for s in self._slots.values()),
                    "ready_to_send": sum(s.phase == "ready" for s in self._slots.values())}

    def quiescent(self, surface_id):
        """Ownership transfer requires all original work for this UUID done."""
        with self._lock:
            slot = self._slots.get(surface_id)
            retired = self._retired_reads.get(surface_id)
            return (not retired or retired.done()) and (slot is None or slot.future is None or slot.future.done())

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
        if self._event_pool is not None:
            self._event_pool.shutdown(wait=wait, cancel_futures=True)


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
        self._fresh = {}
        self.daemon_threads = True
        self._closed = False

    def fresh(self, key, loader):
        """Share only reads which start after every caller's request.

        A caller arriving during an RPC goes into the next batch. No cached
        value, in-flight old read, or shared inventory can satisfy this gate.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("snapshot cache is closed")
            reader = self._fresh.get(key)
            if reader is None:
                reader = self._fresh[key] = FreshReader(daemon_threads=self.daemon_threads)
        return reader.request(loader)

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
            readers = list(self._fresh.values())
        for reader in readers:
            reader.close()
        self._pool.shutdown(wait=True, cancel_futures=False)


class FreshReader:
    """One fresh RPC per simultaneous group, with no reuse across groups."""
    def __init__(self, *, daemon_threads=True):
        self._condition = threading.Condition()
        self._pending = []
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="ccc-fresh", daemon=daemon_threads)
        self._thread.start()

    def request(self, loader):
        future = Future()
        with self._condition:
            if self._closed:
                raise RuntimeError("fresh reader is closed")
            self._pending.append((loader, future))
            self._condition.notify()
        return future.result()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if not self._pending:
                    return
                end = time.monotonic() + .001
                while not self._closed and time.monotonic() < end:
                    self._condition.wait(max(0, end - time.monotonic()))
                pending, self._pending = self._pending, []
            # Detach before starting I/O, so late callers cannot join a read
            # that might already have sampled placement before their guard.
            try:
                value = pending[0][0]()
            except Exception as exc:
                for _, waiter in pending:
                    waiter.set_exception(exc)
            else:
                for _, waiter in pending:
                    waiter.set_result(value)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join()


class TreeSnapshot(dict):
    """Own one RPC tree and its Dock index; never index a mutable client tree."""
    def __init__(self, value):
        from cmux_codex_watch import dock_surface_records, main_surface_records
        super().__init__(copy.deepcopy(value))
        self.dock_surface_ids = frozenset(row["surface_id"] for row in dock_surface_records(self))
        self.main_surfaces = {}
        for row in main_surface_records(self):
            for selector in (row["surface_id"], row["ref"], row["ref"].removeprefix("surface:")):
                self.main_surfaces.setdefault(selector, row)


class SnapshotClient:
    """Per-worker client; process/topology snapshots are shared across workers."""

    def __init__(self, client, cache: SnapshotCache, shared=None):
        self.client, self.cache, self.shared = client, cache, shared

    def __getattr__(self, name):
        return getattr(self.client, name)

    def tree(self):
        return self.cache.get(("tree",), lambda: TreeSnapshot(
            self._inventory("tree", self.client.tree, 1)), ttl=1.0)

    def fresh_tree(self):
        from cmux_codex_watch import CmuxClient
        if not isinstance(self.client, CmuxClient):
            return TreeSnapshot(self.client.tree())
        endpoint = (self.client.binary, id(self.client.runner), id(self.client.viewport_socket))
        return self.cache.fresh(("tree", endpoint), lambda: TreeSnapshot(self.client.tree()))

    def _inventory(self, name, loader, ttl):
        if self.shared is None:
            return loader()
        # Import lazily: the scheduling primitives also run without cmux.
        from ccc_inventory import InventoryUnavailable
        try:
            return self.shared.get(name, loader, ttl=ttl,
                                   wait_timeout=1.0 if name == "tree" else 0)
        except InventoryUnavailable as exc:
            from cmux_codex_watch import CmuxError
            raise CmuxError(str(exc)) from exc

    def top(self, workspace_id):
        top = self.cached_top(workspace_id, wait=True)
        if not callable(getattr(self.client, "top_all", None)):
            return top
        # Preserve the scoped API for discovery callers. Passing the full
        # fleet through each pane/workspace discovery would reclassify it N
        # times, undoing the benefit of sharing the process-table scan.
        def scoped():
            windows = []
            for window in top.get("windows", []):
                workspaces = [workspace for workspace in window.get("workspaces", [])
                              if str(workspace.get("id") or workspace.get("workspace_id") or "") == workspace_id]
                if workspaces:
                    windows.append({"id": window.get("id"), "kind": "window", "workspaces": workspaces})
            return {**{key: top[key] for key in ("sample", "include_processes") if key in top},
                    "windows": windows}
        return self.cache.get(("workspace_top", workspace_id), scoped, ttl=5.0, source=top)

    def cached_top(self, workspace_id, *, wait=False):
        if callable(getattr(self.client, "top_all", None)):
            # cmux top scans the process table even with a workspace filter.
            # Share one fleet scan; callers still join by exact surface UUID.
            return self.cache.get(("top",), lambda: self._inventory("top", self.client.top_all, 5),
                                  ttl=5.0, wait=wait)
        return self.cache.get(("top", workspace_id), lambda: self.client.top(workspace_id), ttl=5.0, wait=wait)

    def top_all(self):
        return self.cached_top("", wait=True)

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
