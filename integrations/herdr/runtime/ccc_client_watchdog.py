"""Bounded native-panic surveillance independent of CCC's Working prefilter.

Only a current-generation compact/orphan-output panic can replace a client.
Screen text and silence alone are diagnostic. This module never types, presses
keys, sends signals, rewrites a rollout, selects a new session or changes routes.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import os
from pathlib import Path
import threading
import time

import ccc_client_panic as panic
import ccc_panic_recovery as recovery
import ccc_request_recovery as request
from ccc_terminal_mouse import NativeBackend


@contextlib.contextmanager
def replacement_lock(config_path, workspace_id):
    name = "workspace-input-" + hashlib.sha256(workspace_id.encode()).hexdigest()[:24] + ".lock"
    fd = os.open(Path(config_path).parent / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def provider_reconnect_observation(core, grid):
    """Report a current structured reconnect; never grant input permission."""
    if not any("reconnecting" in line.casefold() for line in grid.lines):
        return None
    if core._current_working_present(grid.lines):
        return None
    composer, row = core._composer_status(grid)
    if composer == "incompatible" or row is None:
        return None
    error, _ignored, _marker = core._codex_error_scan(grid, row)
    if error is None or not core._is_reconnect_marker(error.block.splitlines()[0]):
        return None
    queued = core._queued_followup_present(grid.lines, row)
    if error.superseded and not queued:
        return None
    return {"error_type": error.error_type, "queued_input_present": queued,
        "composer_status": composer, "reconnect_row": error.block.splitlines()[0],
        "block_sha256": hashlib.sha256(error.block.encode()).hexdigest(),
        "scanner_superseded": bool(error.superseded), "input_authorized": False}


class ClientPanicWatchdog:
    def __init__(self, daemon, core, client_factory, *, interval=5.0, settle_seconds=30.0,
                 clock=time.monotonic, wall_clock=time.time, store=None):
        self.daemon, self.core, self.client_factory = daemon, core, client_factory
        self.interval, self.clock, self.wall_clock = interval, clock, wall_clock
        self.store = (store if store is not None else
                      request.RequestRecoveryStore(daemon.config_path.parent / "codex-panic-recovery"))
        self.debouncer = panic.PanicDebouncer(clock=clock, settle_seconds=settle_seconds)
        self.stop = threading.Event()
        self.thread = None
        self._lock = threading.Lock()
        self._cursor = 0
        self._native_samples = {}
        self._last = {"started_at": wall_clock(), "last_completed_at": None,
                      "rows": {}, "sweeps": 0, "surface_replacements": 0}

    def _record(self, surface_id, status, **values):
        with self._lock:
            self._last["rows"][surface_id] = dict(status=status, observed_at=self.wall_clock(), **values)

    def _terminal(self, target):
        return self.daemon.codex_queue_recovery._open_process_turn(target) or {}

    def _pending_other_repair(self, surface_id):
        other = getattr(self.daemon, "_request_recovery", None)
        pair = other.latest(surface_id) if other is not None else None
        if pair and pair[0]["phase"] in request.ACTIVE:
            return True
        encrypted = getattr(self.daemon, "_encrypted_recovery", None)
        if encrypted is not None and encrypted.path.exists():
            from ccc_encrypted_recovery import ACTIVE
            record = encrypted.get(surface_id)
            if record and record.get("phase") in ACTIVE:
                return True
        return False

    def _observe_original_attempt(self, target, pair):
        state, plan = pair
        if state["phase"] not in request.ACTIVE or not state.get("exec_claim"):
            return state, plan
        import ccc_guard_scope as scope
        turn = self._terminal(target)
        native = scope.process(turn.get("pid")) if turn.get("session_id") == plan["identity"]["session_id"] else None
        binding = None
        if (native and native["birth"] == turn.get("birth")
                and native["surface_id"] == target["surface_id"]
                and native["environment_workspace_id"] == target["workspace_id"]):
            binding = dict(turn, surface_id=target["surface_id"], workspace_id=target["workspace_id"],
                           executable=request.executable_path(native["pid"]))
        return self.store.observe(target["surface_id"], state["key"], binding=binding)

    def observe(self, backend, target, *, apply=True):
        sid = target["surface_id"]
        backend.authorized(target)
        label = self.daemon._native_process_index.lookup(target)
        if not label or label.get("agent_kind") != "codex":
            self._record(sid, "not_local_codex")
            return
        pair = self.store.latest(sid)
        if pair:
            if pair[0]["phase"] == "captured" and not request.rollout_matches(pair[1]["rollout"]):
                pair = self.store.update(sid, pair[0]["key"], expect={"captured"}, phase="held",
                                         reason="later native lifecycle preserved before panic repair")
            pair = self._observe_original_attempt(target, pair)
            if pair[0]["phase"] in request.ACTIVE and pair[0]["phase"] != "captured":
                self._record(sid, "original_recovery_" + pair[0]["phase"],
                    original_key=pair[0]["key"], input_operations=pair[0]["input_operations"],
                    native_receipt=bool(pair[0].get("native_receipt")),
                    matched_tool_progress=bool(pair[0].get("tool_progress")))
                return
        grid = self.core.Grid.from_rpc(backend.client.replay(target["workspace_id"], sid,
                                      live=True, timeout=1.0), sid)
        marker = panic.screen_panic(grid)
        if marker is None:
            self.debouncer.reset(sid)
            sample = self._native_samples.get(sid)
            if sample is None or self.clock() - sample[0] >= 60:
                turn = self._terminal(target)
                sample = self._native_samples[sid] = (self.clock(), turn)
            turn = sample[1]
            age = max(0.0, self.wall_clock() - turn["at"]) if isinstance(turn.get("at"), (int, float)) else None
            working = self.core._current_working_present(grid.lines)
            reconnect = provider_reconnect_observation(self.core, grid)
            status = ("provider_reconnect_with_queued_input" if reconnect and reconnect["queued_input_present"]
                      else "provider_reconnect_visible" if reconnect
                      else "working_without_recent_native_record" if working and age is not None and age >= 180
                      else "native_progress_unknown" if not turn or turn.get("kind") == "unknown"
                      else "no_current_fatal_panic")
            self._record(sid, status, input_operations=0, native_kind=turn.get("kind"),
                session_id=turn.get("session_id"), native_last_record_age_seconds=age,
                silence_is_diagnostic_only=True, provider_reconnect=reconnect)
            return
        proof = backend.probe(target)
        turn = self._terminal(target)
        if (turn.get("pid") != proof["identity"]["pid"]
                or list(turn.get("birth") or []) != proof["identity"]["birth"]
                or turn.get("session_id") != proof["identity"]["session_id"]):
            raise recovery.Refused("original reader changed after panic observation")
        anchor = panic.native_anchor(turn["transcript_path"], turn["session_id"], turn["birth"],
                                     marker, expected_signature=turn.get("signature"))
        identity = dict(proof["identity"], surface_id=sid, workspace_id=target["workspace_id"],
                        pane_id=target["pane_id"])
        settled = self.debouncer.observe(identity, anchor)
        if not settled:
            self._record(sid, "confirmed_panic_settling", session_id=turn["session_id"],
                pid=turn["pid"], birth=turn["birth"], fatal_output=anchor["fatal_output"], input_operations=0)
            return
        if not apply:
            self._record(sid, "confirmed_panic_read_only", session_id=turn["session_id"],
                pid=turn["pid"], fatal_output=anchor["fatal_output"], input_operations=0)
            return
        try:
            self.daemon._client_panic_recovery_step(self, backend, target, proof, turn, anchor)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            original = self.store.latest(sid)
            phase = original[0]["phase"] if original else None
            operations = original[0]["input_operations"] if original else 0
            self._record(sid, "confirmed_panic_recovery_blocked", reason=str(exc)[:300],
                session_id=turn["session_id"], pid=turn["pid"], birth=turn["birth"],
                fatal_output=anchor["fatal_output"], original_phase=phase, input_operations=operations,
                preserved_input=True)

    def recover(self, backend, target, proof, expected, anchor):
        """Run under the core's thread-local shared pane/target delivery scope."""
        sid, base, daemon, core = target["surface_id"], backend.client, self.daemon, self.core
        with daemon._runtime_lock:
            runtime = daemon.runtime.get(sid)
        if runtime is None:
            raise recovery.Refused("enrolled runtime unavailable")
        if (not isinstance(base, core.CmuxClient) or not daemon._connected_native_input(base)
                or "surface.respawn" not in base.viewport_socket.control_methods):
            raise recovery.Refused("connected original-surface replacement unavailable")
        pair = self.store.latest(sid)
        if pair and pair[0]["phase"] in request.ACTIVE and pair[0]["phase"] != "captured":
            raise recovery.Refused("original recovery already has an input intent")

        def authorized(plan=None):
            if self.stop.is_set() or not backend.valid(proof) or self._pending_other_repair(sid):
                return False
            if (daemon._active_send_target(target, None) is None
                    or sid == str(daemon.config.get("manager_surface_id") or "")):
                return False
            with daemon._runtime_lock:
                if (getattr(runtime, "delivery_status", "") == "unknown" or runtime.awaiting
                        or getattr(runtime, "codex_input_phase", "") in {
                            "goal_native_pending", "paste_submit_pending", "enter_pending", "text_retained"}):
                    return False
            current = self._terminal(target)
            if current != expected:
                return False
            from ccc_codex_goal import goal_state_for_open_turn
            if goal_state_for_open_turn(target, current).get("status") not in {"absent", "active", "blocked"}:
                return False
            from ccc_compact_guard import inspect_panic, status
            inspection = inspect_panic(daemon.config_path, target, open_turn=current,
                                       panic_anchor=anchor,
                                       standalone_turn_reader=daemon.codex_queue_recovery._open_process_turn)
            if status(daemon.config_path, target, inspection=inspection):
                return False
            grid = core.Grid.from_rpc(base.replay(target["workspace_id"], sid, live=True, timeout=1), sid)
            # Genuine drafts, missing/cropped composers and menus are retained.
            # The stale Working banner is allowed only with the same fatal proof.
            if (core._composer_status(grid)[0] != "empty" or core._menu_present(grid.lines)
                    or panic.screen_panic(grid) != anchor["panic"]):
                return False
            if panic.native_anchor(current["transcript_path"], current["session_id"], current["birth"],
                    anchor["panic"], expected_signature=current.get("signature")) != anchor:
                return False
            if plan is not None:
                import ccc_guard_scope as scope
                capture = scope.process(expected["pid"], launch=True)
                if (not capture or request.digest(capture) != plan["original_capture_sha256"]
                        or not request.rollout_matches(plan["rollout"])
                        or not all(request.file_matches(plan[k]) for k in (
                            "executable_pin", "argv0_pin", "launcher_pin", "python_pin"))
                        or not all(request.file_matches(p) for p in plan["config_pins"])
                        or any(Path(p).exists() or Path(p).is_symlink() for p in plan["absent_config_paths"])):
                    return False
            return backend.fast_valid(proof) and self._terminal(target) == expected

        if not authorized():
            raise recovery.Refused("panic recovery admission declined; session preserved")
        plan = recovery.capture_panic_plan(target, expected, anchor, daemon.config.get("message") or core.MESSAGE)
        # Hashing native/source files happens before the exclusive structural lease.
        request.verify_bootstrap(plan)
        state = None
        try:
            with replacement_lock(daemon.config_path, target["workspace_id"]):
                if not daemon._acquire_claude_delivery(target, runtime, base) or not authorized(plan):
                    raise recovery.Refused("original pane/target or operator admission declined")
                (state, plan), created = self.store.stage(plan)
                if state["phase"] != "captured" or not authorized(plan):
                    raise recovery.Refused("original incident already consumed or evidence changed")
                state, plan = self.store.update(sid, state["key"], expect={"captured"},
                    phase="respawn_intent", input_operations=1, respawn_at=self.wall_clock())

                def connected_authorized():
                    daemon._verify_claude_delivery_lease()
                    return authorized(plan)

                with base.input_guard(connected_authorized):
                    result = base._control_rpc("surface.respawn", {
                        "workspace_id": target["workspace_id"], "surface_id": sid,
                        "command": request.launcher_command(self.store, state, plan),
                        "working_directory": plan["cwd"], "focus": False}, timeout=base._input_timeout())
                if (not isinstance(result, dict)
                        or any(result.get(k) != target[k] for k in ("workspace_id", "surface_id"))
                        or result.get("surface_type", result.get("type", "terminal")) != "terminal"):
                    raise core.UncertainDeliveryError("original-surface acknowledgement unconfirmed")
            self.store.update(sid, state["key"], respawn_ack={k: result[k] for k in (
                "workspace_id", "surface_id", "pane_id", "window_id", "surface_type", "type") if k in result})
            with self._lock:
                self._last["surface_replacements"] += 1
            self._record(sid, "original_recovery_respawn_intent", original_key=state["key"],
                         session_id=expected["session_id"], input_operations=1)
        except BaseException as exc:
            if state is not None:
                fresh, _ = self.store.get(sid, state["key"])
                if fresh["phase"] == "respawn_intent":
                    self.store.update(sid, state["key"], expect={"respawn_intent"},
                        phase="respawn_unknown", respawn_error=type(exc).__name__)
            raise

    def sweep(self, *, apply=True, budget=3.0):
        began = self.clock()
        client = self.client_factory()
        backend = NativeBackend(self.daemon, self.core, client)
        records = list(backend.records())
        # Fresh native hints are a scheduling filter, never input authority.
        # Keep every discovered surface in diagnostics, but do not spend the
        # bounded recovery rotation on terminals with no local Codex hint.
        candidates = []
        for target in records:
            sid = target["surface_id"]
            try:
                label = self.daemon._native_process_index.lookup(target)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                self._record(sid, "native_hint_unavailable", reason=str(exc)[:300], input_operations=0)
                continue
            if label and label.get("agent_kind") == "codex":
                candidates.append(target)
            else:
                self._record(sid, "no_local_codex_hint", input_operations=0)
        if candidates:
            start = self._cursor % len(candidates)
            for index in range(len(candidates)):
                if self.stop.is_set() or self.clock() - began >= budget:
                    break
                target = candidates[(start + index) % len(candidates)]
                self._cursor = (start + index + 1) % len(candidates)
                sid = target["surface_id"]
                try:
                    self.observe(backend, target, apply=apply)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    self.debouncer.reset(sid)
                    original = self.store.latest(sid)
                    phase = original[0]["phase"] if original else None
                    operations = original[0]["input_operations"] if original else 0
                    self._record(sid, "refused_or_unconfirmed", reason=str(exc)[:300],
                                 original_phase=phase, input_operations=operations)
        with self._lock:
            present = {r["surface_id"] for r in records}
            self._last["rows"] = {k: v for k, v in self._last["rows"].items() if k in present}
            self._native_samples = {k: v for k, v in self._native_samples.items() if k in present}
            self._last.update(last_completed_at=self.wall_clock(), candidates=len(records),
                local_codex_hints=len(candidates),
                local_codex_surface_ids=[r["surface_id"] for r in candidates],
                sweep_elapsed_seconds=self.clock() - began, sweeps=self._last["sweeps"] + 1)

    def _run(self):
        while not self.stop.is_set():
            started = self.clock()
            try:
                self.sweep()
            except Exception as exc:
                with self._lock:
                    self._last["error"] = type(exc).__name__ + ": " + str(exc)[:300]
            self.stop.wait(max(0.1, self.interval - (self.clock() - started)))

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="ccc-client-panic", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def summary(self):
        with self._lock:
            result = copy.deepcopy(self._last)
        confirmed = {sid: row for sid, row in result["rows"].items()
                     if row["status"].startswith("confirmed_panic")}
        local_ids = result.get("local_codex_surface_ids", [])
        local_rows = [result["rows"][sid] for sid in local_ids if sid in result["rows"]]
        ages = [max(0.0, self.wall_clock() - row["observed_at"]) for row in local_rows]
        result.update(maximum_local_codex_observation_age_seconds=max(ages, default=None),
            missing_local_codex_observations=len(local_ids) - len(local_rows),
            interval_scope="bounded scheduler ticks; individual surface ages are reported separately")
        result.update(thread_alive=bool(self.thread and self.thread.is_alive()),
            monitor_interval_seconds=self.interval, required_stable_seconds=self.debouncer.settle_seconds,
            replacement_policy="same-session-original-route-once", terminal_key_operations=0,
            history_rewrites=0, last_completed_age_seconds=(None if result["last_completed_at"] is None
                else max(0.0, self.wall_clock() - result["last_completed_at"])),
            confirmed_panic_surface_ids=sorted(confirmed),
            recovery_blocked_surface_ids=sorted(sid for sid, row in confirmed.items()
                if row["status"] == "confirmed_panic_recovery_blocked"))
        return result
