"""Independent native interpreters with parent-revocable input ownership.

The parent retains the cmux service, configuration and UI publication. A lane
has its own GIL and at most fifty native identities, but uses the same native
turn, private-check, workspace and durable input guards as the main watcher.
No Codex process, model request or workspace is created here.
"""
from __future__ import annotations

import json
import queue
from pathlib import Path
import subprocess
import threading
import time


def _binding_matches(value, binding):
    return isinstance(value, dict) and all(value.get(key) == expected
                                          for key, expected in binding.items())


def _valid_birth(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(part) is int for part in value)
            and value[0] > 0 and 0 <= value[1] < 1000000)


def _binding_process_current(target, binding):
    from ccc_codex_queue import process_placement_start
    from ccc_guard_scope import birth
    return (process_placement_start(binding["pid"], target) == binding["process_start"]
            and ("birth" not in binding or birth(binding["pid"], codex=True) == binding["birth"]))


def _bound_native_turn(target, binding, reader):
    from ccc_codex_queue import process_placement_start
    from ccc_guard_scope import birth

    def fresh_birth():
        if process_placement_start(binding["pid"], target) != binding["process_start"]:
            return None
        actual = birth(binding["pid"], codex=True)
        return actual if _valid_birth(actual) and actual == binding["birth"] else None

    try:
        if "birth" not in binding:
            value = reader(target)
            if not _binding_matches(value, binding):
                return {"kind": "unknown"}
            # Portable readers may omit birth. An explicit value, however,
            # cannot be dropped or trusted without a fresh process check.
            if "birth" in value and (not _valid_birth(value["birth"])
                    or not _binding_process_current(target, {**binding, "birth": value["birth"]})):
                return {"kind": "unknown"}
            return value
        if not _valid_birth(binding["birth"]) or fresh_birth() is None:
            return {"kind": "unknown"}
        value = reader(target)
        actual = fresh_birth()
        if actual is None or not isinstance(value, dict):
            return {"kind": "unknown"}
        # Older Hook readers omit birth. Revalidate the original process on
        # both sides of that single read before supplying the fresh sample.
        # An explicit empty/conflicting birth is evidence, never a missing field.
        if "birth" not in value:
            value = {**value, "birth": list(actual)}
        elif not _valid_birth(value["birth"]):
            return {"kind": "unknown"}
        return value if _binding_matches(value, binding) else {"kind": "unknown"}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        return {"kind": "unknown"}


class AuthorizationCells:
    """Never reuse an assignment cell: a revoked old callback stays revoked."""
    def __init__(self, capacity=1024 * 1024):
        self._bytes = bytearray(capacity)
        self._next = 0
        self._lock = threading.Lock()

    def allocate(self):
        with self._lock:
            if self._next == len(self._bytes):
                raise RuntimeError("native assignment cells exhausted")
            index = self._next
            self._next += 1
            self._bytes[index] = 1
            return index

    def revoke(self, index):
        self._bytes[index] = 0

    def revoke_all(self):
        with self._lock:
            self._bytes[:self._next] = bytes(self._next)

    def shared(self):
        return memoryview(self._bytes).toreadonly()


class InterpreterLane:
    """One context-bearing service interpreter, closed only after quiescence."""
    def __init__(self, source, initial, cells, *, service="ccc_native_lanes:run_native_lane"):
        from concurrent import interpreters
        self.cells = cells
        self.cell = cells.allocate()
        self.commands = interpreters.create_queue()
        self._wire_results = interpreters.create_queue()
        self.results = queue.SimpleQueue()
        self.error = None
        self._initial = json.dumps(initial)
        self._source, self._service = str(Path(source).resolve()), service
        self._thread = threading.Thread(target=self._run, name="ccc-native-lane", daemon=True)
        self._thread.start()

    def _run(self):
        from concurrent import interpreters
        interpreter = relay = None
        relay_stop = threading.Event()
        def receive():
            from concurrent.interpreters import QueueEmpty
            while True:
                try:
                    self.results.put(self._wire_results.get_nowait())
                except QueueEmpty:
                    if relay_stop.wait(.005):
                        # exec() has returned: finish copying every final row
                        # into the parent before destroying its source interpreter.
                        try:
                            while True:
                                self.results.put(self._wire_results.get_nowait())
                        except QueueEmpty:
                            return
        try:
            interpreter = interpreters.create()
            interpreter.prepare_main(source=self._source, initial=self._initial,
                flags=self.cells.shared(), lane_cell=self.cell,
                commands=self.commands, results=self._wire_results, service=self._service)
            relay = threading.Thread(target=receive, name="ccc-native-results", daemon=True)
            relay.start()
            interpreter.exec("import sys, importlib\nsys.path.insert(0, source)\n"
                "module, function = service.split(':', 1)\n"
                "getattr(importlib.import_module(module), function)(initial, flags, lane_cell, commands, results)")
        except BaseException as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.results.put({"type":"failure", "error":self.error})
        finally:
            self.cells.revoke(self.cell)
            relay_stop.set()
            if relay is not None:
                relay.join()
            if interpreter is not None:
                try:
                    interpreter.close()
                except BaseException as exc:
                    self.error = f"interpreter cleanup: {type(exc).__name__}: {exc}"
            self.results.put({"type":"stopped", "error":self.error})

    def update(self, assignments):
        if not self._thread.is_alive() or not self.cells.shared()[self.cell]:
            raise RuntimeError("native lane is not accepting assignments")
        self.commands.put({"type":"assign", "assignments":assignments})

    def drain(self):
        rows = []
        while True:
            try:
                rows.append(self.results.get_nowait())
            except queue.Empty:
                return rows

    def close(self, timeout=30):
        self.cells.revoke(self.cell)
        self.commands.put({"type":"stop"})
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("native lane has not quiesced; its ownership must remain blocked")


class NativeDispatcher:
    """Transfer only quiescent UUIDs; retain ownership until final receipts."""
    def __init__(self, daemon, capabilities):
        self.daemon, self.capabilities = daemon, dict(capabilities)
        self.cells = AuthorizationCells()
        self.lanes, self.owned = [], {}
        self._next_refresh = 0.0
        self.revision = 0
        self._filtered = None
        self._closed = False
        self._discovery_pool = None
        self._discovering = {}
        self._discovery_state = {}
        self._discovery_cursor = None
        self._discovery_limit = 2

    def owns(self, sid):
        return str(sid) in self.owned

    @staticmethod
    def binding(source):
        if (not isinstance(source, dict)
                or not all(key in source for key in ("session_id", "pid", "process_start"))
                or ("birth" in source and not _valid_birth(source["birth"]))):
            return None
        value = {key:source[key] for key in ("session_id", "pid", "process_start")}
        if "birth" in source:
            value["birth"] = list(source["birth"])
        return value

    @staticmethod
    def _missing_source_lost(target, binding):
        # Inventory absence alone is not death. Probe only the bound PID;
        # an unavailable inspector retains exclusion until a later verdict.
        try:
            return not _binding_process_current(target, binding)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            return False

    @classmethod
    def _source_binding_changed(cls, target, current, candidate):
        if current == candidate:
            return False
        if ("birth" in current and "birth" not in candidate
                and all(candidate.get(key) == current.get(key)
                        for key in ("session_id", "pid", "process_start"))):
            # A legacy Hook can remain less precise than the native reader
            # that admitted this lane. Keep the exact assignment and verify
            # its own PID; missing metadata must not cause constant churn.
            return cls._missing_source_lost(target, current)
        return True

    @staticmethod
    def _usable_hint(hint):
        if not isinstance(hint, dict) or hint.get("agent_kind") != "codex":
            return False
        pids = hint.get("agent_pids")
        if (not isinstance(pids, (list, tuple)) or len(pids) != 1
                or type(pids[0]) is not int or pids[0] <= 0):
            return False
        born = hint.get("agent_birth")
        if born and (not isinstance(born, (list, tuple)) or not _valid_birth(list(born))):
            return False
        foreground = hint.get("foreground_generation")
        return (not foreground or (isinstance(foreground, (list, tuple))
                and len(foreground) >= 2 and isinstance(foreground[0], str)
                and (foreground[1] is None or isinstance(foreground[1], str))))

    @staticmethod
    def _hint_conflicts(binding, hint):
        """A cached disagreement requests a fresh check, never a verdict."""
        if not NativeDispatcher._usable_hint(hint):
            return False
        born = hint.get("agent_birth")
        foreground = hint.get("foreground_generation")
        return (hint["agent_pids"][0] != binding["pid"]
                or (born and "birth" in binding and list(born) != binding["birth"])
                or (foreground and foreground[0] == "ok"
                    and foreground[1] != binding["session_id"]))

    @classmethod
    def _current_hint_conflicts(cls, target, binding, hint):
        """Recheck only the two named processes when an index disagrees.

        The index can describe the old foreground even after a complete native
        reader has admitted its successor. Its publication time cannot order
        those observations. These bounded checks may withhold/revoke ownership;
        only the complete native reader below can admit or authorize input.
        """
        if not cls._hint_conflicts(binding, hint):
            return False
        from ccc_codex_queue import process_placement_start
        from ccc_guard_scope import birth
        from ccc_client_thread_observation import read_foreground
        from ccc_request_observation_policy import request_observation_directory_matches

        try:
            if not _binding_process_current(target, binding):
                return True
            hinted_pid = hint["agent_pids"][0]
            if hinted_pid != binding["pid"]:
                hinted_birth = birth(hinted_pid, codex=True)
                hinted_start = process_placement_start(hinted_pid, target)
                if (hinted_birth and hinted_start == hinted_birth[0]
                        and birth(hinted_pid, codex=True) == hinted_birth):
                    # Two currently placed clients are ambiguous. A dead or
                    # moved old PID, however, cannot evict the current source.
                    return True
            status, selected, _ = read_foreground(
                binding["pid"], request_observation_directory_matches)
            if not _binding_process_current(target, binding):
                return True
            # Missing/invalid selection is not proof of a switch. The full
            # reader retains its unknown veto at admission and before input.
            return status == "ok" and selected != binding["session_id"]
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            return False

    def _merge(self, sid, cell, value):
        owned = self.owned.get(sid)
        if owned is None or owned["assignment"]["cell"] != cell:
            return
        from cmux_codex_watch import TargetRuntime
        record = TargetRuntime.from_dict(value)
        with self.daemon._runtime_lock:
            previous = self.daemon.runtime.get(sid)
            if previous is None or record.delivery_revision >= previous.delivery_revision:
                self.daemon.runtime[sid] = record

    def _drain(self):
        for state in list(self.lanes):
            for row in state["lane"].drain():
                if row["type"] == "ready":
                    state["ready"] = True
                elif row["type"] == "runtime":
                    state["published_count"] = len(row["rows"])
                    state["scheduler"] = row.get("scheduler", {})
                    for sid, value in row["rows"].items():
                        self._merge(sid, value["cell"], value["runtime"])
                elif row["type"] == "released":
                    sid, cell = row["surface_id"], row["cell"]
                    self._merge(sid, cell, row["runtime"])
                    owned = self.owned.get(sid)
                    if owned and owned["assignment"]["cell"] == cell:
                        del self.owned[sid]
                        self.revision += 1
                elif row["type"] == "failure":
                    state["failed"] = row["error"]
                    for assignment in state["assignments"].values():
                        self.cells.revoke(assignment["cell"])
                    self.daemon.logger.error("native lane failed; waiting for quiescence: %s",row["error"])
                elif row["type"] == "stopped":
                    # All interpreter threads and final durable writes have
                    # finished. A new lane will restore the original rows.
                    for sid, owned in list(self.owned.items()):
                        if owned["state"] is state:
                            self.cells.revoke(owned["assignment"]["cell"])
                            del self.owned[sid]
                            self.revision += 1
                    self.lanes.remove(state)

    def refresh(self, targets):
        self._drain()
        if self._closed or time.monotonic() < self._next_refresh:
            return
        self._next_refresh = time.monotonic() + .25
        active = {str(t["surface_id"]):t for t in targets
                  if t.get("enabled", True) and not t.get("paused", False)}
        hints = {sid:self.daemon._native_process_index.lookup(target) for sid,target in active.items()}
        sources = self.daemon.codex_queue_recovery.wakeup_sources(targets)
        bindings = {s["surface_id"]:self.binding(s) for s in sources
                    if s.get("identity_current") and all(k in s for k in ("session_id","pid","process_start"))}
        # Reuse only this refresh's same target/binding/hint verdict. A stale
        # conflicting hint otherwise repeats the bounded OS probes twice.
        conflicts = {}
        def hint_conflicts(sid, binding):
            key = (sid, binding["session_id"], binding["pid"], binding["process_start"],
                   tuple(binding.get("birth", ())))
            if key not in conflicts:
                conflicts[key] = self._current_hint_conflicts(active[sid], binding, hints.get(sid))
            return conflicts[key]
        bindings = {sid:binding for sid,binding in bindings.items()
                    if binding is not None and sid in active and not hint_conflicts(sid, binding)}
        self._discover_unbound(active, bindings, hints)
        changed = []
        def dirty(state):
            if not any(item is state for item in changed):
                changed.append(state)
        for sid, owned in list(self.owned.items()):
            assignment, state = owned["assignment"], owned["state"]
            if (sid not in active or any(active[sid].get(key) != assignment["target"].get(key)
                    for key in ("workspace_id", "source", "source_workspace_id", "pane_id"))
                    or (sid in bindings and self._source_binding_changed(
                        active[sid], assignment["binding"], bindings[sid]))
                    or hint_conflicts(sid, assignment["binding"])
                    or (sid not in bindings and self._missing_source_lost(active[sid], assignment["binding"]))):
                self.cells.revoke(assignment["cell"])
                if sid in state["assignments"]:
                    del state["assignments"][sid]
                    dirty(state)
                # Keep parent input blocked until the lane acknowledges the
                # original surface's final transaction has finished.
        for sid, target in active.items():
            if sid in self.owned or sid not in bindings:
                continue
            lock = self.daemon._surface_lock(sid)
            if not lock.acquire(blocking=False):
                continue
            try:
                if (not self.daemon._scheduler.quiescent(sid)
                        or self.daemon._active_send_target(target) is None):
                    continue
                # Hook indexes are advisory. A stale Codex binding must not
                # steal ownership from a replacement Claude/shell process.
                native = _bound_native_turn(target, bindings[sid],
                                            self.daemon.codex_queue_recovery.current_turn)
                if (not _binding_matches(native, bindings[sid])
                        or native.get("kind") == "unknown"):
                    continue
                state = next((s for s in self.lanes if not s.get("failed")
                    and s["workspace_id"] == target["workspace_id"] and len(s["assignments"]) < 25), None)
                if state is None:
                    import cmux_codex_watch as core
                    lane = InterpreterLane(Path(core.__file__).parent,
                        {"config_path":str(self.daemon.config_path), "state_path":str(self.daemon.state_path),
                         "capabilities":self.capabilities,
                         "native_sessions_root":str(self.daemon.codex_queue_recovery.sessions_root),
                         "native_bindings_path":str(self.daemon.codex_queue_recovery.bindings)}, self.cells)
                    state = {"lane":lane, "workspace_id":target["workspace_id"], "assignments":{}}
                    self.lanes.append(state)
                runtime = self.daemon.runtime.get(sid)
                assignment = {"target":dict(target), "binding":self.binding(native),
                    "cell":self.cells.allocate(), "runtime":runtime.to_dict() if runtime else {}}
                # Publish exclusion in the parent before waking the child.
                self.owned[sid] = {"state":state, "assignment":assignment}
                state["assignments"][sid] = assignment
                self.revision += 1
                dirty(state)
            finally:
                lock.release()
        for state in changed:
            try:
                state["lane"].update(list(state["assignments"].values()))
            except RuntimeError:
                # The stopped notification releases ownership only after the
                # interpreter has copied its final receipts into the parent.
                state["failed"] = "native lane stopped before assignment publication"

    @staticmethod
    def _discovery_identity(target, hint):
        if not NativeDispatcher._usable_hint(hint):
            return None
        return (tuple(target.get(key) for key in
                      ("surface_id", "workspace_id", "source", "source_workspace_id", "pane_id")),
                hint["agent_pids"][0], tuple(hint.get("agent_birth") or ()),
                tuple(hint.get("foreground_generation") or ()))

    def _read_unbound(self, target, completion):
        try:
            return self.daemon.codex_queue_recovery.current_turn(target)
        finally:
            completion["at"] = time.monotonic()

    def _discover_unbound(self, active, bindings, hints=None):
        """Bind a new working CLI before its first error, without GUI polling.

        Hookless native clients have no advisory source until current_turn
        verifies their original writer. Discover only OS-indexed candidates in
        background readers. Admission below still repeats the complete proof.
        """
        hints = hints if hints is not None else {
            sid:self.daemon._native_process_index.lookup(target) for sid,target in active.items()}
        candidates = {}
        for sid, target in active.items():
            hint = hints.get(sid)
            if (sid in bindings or sid in self.owned or not target.get("enabled", True)
                    or target.get("paused", False) or not self._usable_hint(hint)):
                continue
            identity = self._discovery_identity(target, hint)
            candidates[sid] = (target, identity)
            state = self._discovery_state.get(sid)
            if state is None or state["identity"] != identity:
                self._discovery_state[sid] = {"identity":identity, "failures":0, "due":0.0}
        completed = set()
        for sid, attempt in list(self._discovering.items()):
            future = attempt["future"]
            if future.done():
                try:
                    value = future.result()
                except Exception:
                    value = None
                del self._discovering[sid]
                # A removed/replaced generation cannot delay or approve its
                # successor. Keep at most one read per surface even across a
                # generation change; its old result remains advisory only.
                if sid not in candidates or candidates[sid][1] != attempt["identity"]:
                    continue
                state = self._discovery_state[sid]
                binding = self.binding(value)
                success = (binding is not None and value.get("kind") not in (None, "unknown")
                           and not self._current_hint_conflicts(active[sid], binding, hints[sid]))
                state["failures"] = 0 if success else min(state["failures"] + 1, 4)
                delay = .25 if success else .25 * 2 ** (state["failures"] - 1)
                state["due"] = attempt["completion"].get("at", time.monotonic()) + delay
                # wakeup_sources was read before consuming this future. Even
                # when its completion was earlier, allow the next refresh to
                # see the source instead of immediately repeating a full read.
                completed.add(sid)
        now = time.monotonic()
        order = list(candidates)
        if self._discovery_cursor in order:
            offset = order.index(self._discovery_cursor) + 1
            order = order[offset:] + order[:offset]
        for sid in order:
            if len(self._discovering) >= self._discovery_limit:
                break
            if (sid in completed or sid in self._discovering
                    or self._discovery_state[sid]["due"] > now):
                continue
            if self._discovery_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                self._discovery_pool = ThreadPoolExecutor(self._discovery_limit, thread_name_prefix="ccc-native-bind")
            target, identity = candidates[sid]
            completion = {}
            future = self._discovery_pool.submit(self._read_unbound, dict(target), completion)
            self._discovering[sid] = {"future":future, "identity":identity, "completion":completion}
            self._discovery_cursor = sid
        self._discovery_state = {sid:state for sid,state in self._discovery_state.items()
                                 if sid in active and sid not in bindings and sid not in self.owned}

    def filter(self, targets, publication):
        cached = self._filtered
        if cached is None or cached[0] is not publication or cached[1] != self.revision:
            cached = (publication, self.revision, [t for t in targets if not self.owns(t["surface_id"])], object())
            self._filtered = cached
        return cached[2], cached[3]

    def metrics(self):
        combined = {"native_interpreters":len(self.lanes), "native_owned_targets":len(self.owned),
                    "native_discovery_inflight":len(self._discovering)}
        for state in self.lanes:
            for key, value in state.get("scheduler", {}).items():
                if isinstance(value, int):
                    combined[key] = combined.get(key, 0) + value
        return combined

    def close(self):
        self._closed = True
        self.cells.revoke_all()
        if self._discovery_pool is not None:
            self._discovery_pool.shutdown(wait=True, cancel_futures=True)
        for state in list(self.lanes):
            state["lane"].close()
        self._drain()


def refresh_native_bindings(recovery, targets):
    """Retry transient initial writer reads; never grant input authorization."""
    known = {s["surface_id"] for s in recovery.wakeup_sources(targets) if s.get("identity_current")}
    for target in targets:
        if target["surface_id"] not in known:
            recovery.current_turn(target)


def _create_lane_daemon(settings):
    import cmux_codex_watch as core

    class LaneDaemon(core.WatchDaemon):
        def _load_config_at_startup(self):
            # The parent performs migration and global service maintenance.
            return self._load_config()

        def _load_runtime(self):
            # Parent snapshots retain all history. An empty lane must not
            # copy/serialize it, or seven lanes retain seven full fleet copies.
            # Durable send state is loaded before each new assignment below.
            return {}

    return LaneDaemon(Path(settings["config_path"]), Path(settings["state_path"]))


def _restore_assignment_runtime(daemon, assignment):
    import cmux_codex_watch as core

    sid = assignment["target"]["surface_id"]
    previous = daemon.runtime.get(sid)
    supplied = core.TargetRuntime.from_dict(assignment["runtime"])
    chosen = (supplied if previous is None or previous.delivery_revision <= supplied.delivery_revision
              else previous)
    rows = {sid: chosen}
    # The parent can publish a same-revision mutation before its durable write.
    # Re-read this identity's original durable fields, including UNKNOWN and
    # absent-field defaults. Newer durable revisions always win, as at startup.
    daemon._delivery_store.restore(rows, core.TargetRuntime, surface_ids=(sid,))
    daemon.runtime[sid] = rows[sid]


def run_native_lane(initial, flags, lane_cell, commands, results):
    """Run only parent-assigned, freshly verified original native identities."""
    import time
    import sys
    from concurrent.interpreters import QueueEmpty
    import cmux_codex_watch as core
    from ccc_codex_queue import NativeCompletionWatcher
    from ccc_scheduling import SurfaceScheduler

    settings = json.loads(initial)
    if not flags[lane_cell]:
        return
    # Subinterpreters have independent logging state. The parent's handlers
    # are not inherited, so INFO state transitions and send receipts otherwise
    # disappear once a surface is assigned to a lane. Use inherited stderr;
    # launchd already retains it, without opening a competing rotating file.
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.setswitchinterval(min(sys.getswitchinterval(), .001))
    daemon = _create_lane_daemon(settings)
    if settings.get("native_sessions_root"):
        from ccc_private_check import PrivateChecks
        daemon.codex_queue_recovery.sessions_root = Path(settings["native_sessions_root"])
        daemon.private_checks = PrivateChecks(daemon.config_path, daemon.codex_queue_recovery.sessions_root)
    if settings.get("native_bindings_path"):
        from ccc_codex_queue import _shared_binding_file
        daemon.codex_queue_recovery.bindings = Path(settings["native_bindings_path"])
        daemon.codex_queue_recovery._binding_file = _shared_binding_file(daemon.codex_queue_recovery.bindings)
    daemon._delivery_store.daemon_threads = False
    daemon._process_snapshots.daemon_threads = False
    daemon.config_store.migrate_deprecated = lambda: False
    # This is a socket resource window, not a task-start permit or cadence.
    # All fifty tasks prepare concurrently; input keeps its original deadline.
    daemon._viewport_socket = core.CmuxViewportSocket(max_connections=8)
    daemon._viewport_socket.configure(settings["capabilities"])
    daemon._viewport_socket.live_native_frames = True
    assignments = {}
    retiring = {}
    publication = object()
    last_published = 0.0
    last_binding_refresh = 0.0

    def current(target):
        assigned = assignments.get(str(target["surface_id"]))
        return bool(flags[lane_cell] and assigned and flags[assigned["cell"]]
                    and assigned["target"]["workspace_id"] == target["workspace_id"])

    original_active = daemon._active_send_target
    def active(target, is_current=None):
        return original_active(target, lambda: current(target) and (is_current is None or is_current()))
    daemon._active_send_target = active

    def native_label(target, *_):
        if not current(target):
            return {"agent_kind":"unknown", "agent_pids":[]}
        binding = assignments[str(target["surface_id"])]["binding"]
        if not _binding_process_current(target, binding):
            return {"agent_kind":"unknown", "agent_pids":[]}
        return {"agent_kind":"codex", "agent_pid":binding["pid"], "agent_pids":[binding["pid"]]}
    daemon._candidate_process_label = native_label
    daemon.codex_queue_recovery.process_lookup = native_label
    original_turn = daemon.codex_queue_recovery.current_turn
    def turn(target):
        if not current(target):
            return {"kind":"unknown"}
        bound = assignments[str(target["surface_id"])]["binding"]
        return _bound_native_turn(target, bound, original_turn)
    daemon.codex_queue_recovery.current_turn = turn

    class Publisher:
        def request(self, *, wait=True):
            # Codex delivery rows are persisted separately before input. Only
            # the parent may publish the combined observer state.json.
            pass
    daemon._state_writer = Publisher()
    daemon._delivery_store.start()
    scheduler = SurfaceScheduler(daemon._scheduled_native, lambda *_:None,
        interval=.1, observe_workers=4, send_workers=1, event_workers=50,
        event_handler=daemon._scheduled_native_event, supersede_reads=False,
        on_dispatch=daemon._record_dispatch, on_error=daemon._scheduled_error)
    daemon._scheduler = scheduler
    native = NativeCompletionWatcher(
        lambda:daemon.codex_queue_recovery.wakeup_sources(
            [a["target"] for a in assignments.values() if current(a["target"])]),
        scheduler.request_observation, interval=.05, retry_interval=.1,
        retry_needed=daemon._native_retry_needed, on_failure=daemon._native_failed,
        daemon_threads=False)
    scheduler.observation_interval = native.observation_interval
    native.start()
    results.put({"type":"ready"})
    try:
        while flags[lane_cell]:
            while True:
                try:
                    message = commands.get_nowait()
                except QueueEmpty:
                    break
                if message["type"] == "stop":
                    return
                incoming = message["assignments"]
                if len(incoming) > 50 or len({a["target"]["surface_id"] for a in incoming}) != len(incoming):
                    raise ValueError("native lane requires at most fifty unique assignments")
                replacement = {a["target"]["surface_id"]:a for a in incoming}
                for sid, assignment in replacement.items():
                    if not 0 <= assignment["cell"] < len(flags):
                        raise ValueError("invalid native ownership cell")
                    if sid in assignments and assignments[sid]["cell"] != assignment["cell"]:
                        # Replacement must use a new lane after the old lane
                        # and its pending inputs have completely quiesced.
                        raise ValueError("cannot replace active native identity in place")
                    if sid in assignments and assignments[sid]["binding"] != assignment["binding"]:
                        raise ValueError("native identity requires a new ownership cell")
                    if sid in retiring:
                        raise ValueError("original native input has not quiesced")
                    if sid not in assignments:
                        _restore_assignment_runtime(daemon, assignment)
                retiring.update({sid:a for sid,a in assignments.items() if sid not in replacement})
                new_targets = [a["target"] for sid,a in replacement.items() if sid not in assignments]
                assignments = replacement
                daemon.dynamic_targets = {sid:a["target"] for sid,a in assignments.items()}
                publication = object()
                # Each interpreter owns its own hookless-source cache. Bind
                # working originals now, before their first failure, rather
                # than waiting for a periodic error viewport to populate it.
                for target in new_targets:
                    daemon.codex_queue_recovery.current_turn(target)
            daemon._reload_config_if_changed()
            targets = [a["target"] for a in assignments.values() if current(a["target"])]
            if time.monotonic() - last_binding_refresh >= .25:
                refresh_native_bindings(daemon.codex_queue_recovery, targets)
                last_binding_refresh = time.monotonic()
            scheduler.tick(targets, generation=daemon._observation_policy.key, publication=publication)
            for sid, assignment in list(retiring.items()):
                if scheduler.quiescent(sid):
                    results.put({"type":"released", "surface_id":sid, "cell":assignment["cell"],
                        "runtime":daemon.runtime[sid].to_dict()})
                    del retiring[sid]
            now = time.monotonic()
            if now - last_published >= .25:
                rows = {sid:{"cell":a["cell"], "runtime":daemon.runtime[sid].to_dict()}
                        for sid,a in assignments.items() if sid in daemon.runtime}
                results.put({"type":"runtime", "rows":rows, "scheduler":scheduler.snapshot()})
                last_published = now
            scheduler.wakeup.wait(scheduler.wait_timeout(.025))
            scheduler.wakeup.clear()
    finally:
        daemon.stop_requested = True
        native.close()
        scheduler.close()
        daemon._process_snapshots.close()
        daemon._delivery_store.close()
        results.put({"type":"runtime", "rows":{sid:{"cell":a["cell"],
            "runtime":daemon.runtime[sid].to_dict()} for sid,a in assignments.items() if sid in daemon.runtime}})
