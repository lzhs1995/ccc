"""Durable, user-triggered creation of 50 Codex tabs in one pinned workspace.

Creation and first-prompt attempts are recorded before I/O. A timeout is never
permission to create another tab or repeat a prompt. The bootstrap receipt can
recover a lost create reply; only the original transcript confirms a start.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import cmux_codex_watch as core
from ccc_codex_queue import QueueRecovery, epoch, batch_shell_identity, batch_child_label
from ccc_inventory import SharedInventory
from ccc_scheduling import SnapshotCache, SnapshotClient

COUNT = 50
WORKER_VERSION = 23
PROMPT = "show me u power"
INITIALIZING = {"creating", "create_unknown", "created", "restarting", "restart_unknown",
                "submitted", "submitting", "uncertain"}
STARTABLE = {"pending", "restart_pending", "pty_wait"}
STARTUP_LEASE_SEC = 30
CONFIRMABLE = {"submitted", "submitting", "uncertain", "confirmed"}
CONFIRM_READ_BYTES = 1024 * 1024


def pty_available():
    """Reserve no terminal when the host has reached its PTY limit."""
    try:
        master, slave = os.openpty()
    except OSError:
        return False
    os.close(slave)
    os.close(master)
    return True


def _startup_context(message):
    message = message.strip()
    environment = r"<environment_context>[\s\S]*?</environment_context>"
    instructions = r"# AGENTS\.md instructions for [^\n]+\n\s*<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>"
    return bool(re.fullmatch(environment, message) or
                re.fullmatch(instructions + r"(?:\s*" + environment + r")?", message))


def job_path(config_path, job_id):
    uuid.UUID(job_id)
    return Path(config_path).parent / "workspace-batches" / job_id / "job.json"


def allowed(config, job):
    from ccc_batch_guard import blocked
    rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == job["workspace_id"]), {})
    return (config.get("mode") == "armed" and not config.get("global_paused")
            and rule.get("enabled", True) and not rule.get("paused")
            and rule.get("active_batch_id") == job["id"]
            and not blocked(job.get("config_path", core.DEFAULT_CONFIG_PATH), job["workspace_id"]))


def counts(job):
    slots = job.get("slots", [])
    return {"created": sum(bool(s.get("surface_id")) for s in slots),
            "ready": sum(bool(s.get("session_id")) for s in slots),
            "submitted": sum(s.get("phase") in {"submitted", "confirmed"} for s in slots),
            "started": sum(s.get("phase") == "confirmed" for s in slots),
            "failed": sum(s.get("phase") in {"blocked", "surface_closed", "uncertain", "create_unknown"} for s in slots),
            "total": len(slots)}


def snapshots(config_path, config):
    result = {}
    for rule in config.get("workspace_rules", []):
        jid = rule.get("last_batch_id")
        if jid:
            try:
                job = core.load_json(job_path(config_path, jid), {})
                result[rule["workspace_id"]] = {"id": jid, "status": job.get("status"), **counts(job)}
                if rule.get("batch_guard"):
                    from ccc_batch_guard import snapshot
                    result[rule["workspace_id"]]["protection"] = snapshot(config_path, rule["workspace_id"])
            except (OSError, ValueError, KeyError, RuntimeError):
                continue
    return result


def _client(config):
    transport = core.CmuxViewportSocket()
    client = core.CmuxClient(config.get("cmux_path", core.DEFAULT_CMUX), viewport_socket=transport)
    transport.configure(client.capabilities())
    return client


def _launch(config_path, job):
    path = job_path(config_path, job["id"])
    with (path.parent / "worker.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "run",
                          "--config", str(config_path), "--job", job["id"]],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def workspace_record(config_path, selector, config, client=None):
    """UUID actions persist immediately; topology is checked by the worker."""
    try:
        rule = core.workspace_rule_by_id(config, selector)
        return {**rule, "workspace_id": rule["workspace_id"], "ref": rule.get("ref", ""),
                "title": rule.get("title_snapshot", rule.get("name", ""))}
    except RuntimeError:
        pass
    if client is None:
        try:
            uuid.UUID(selector)
            return {"workspace_id": selector, "ref": "", "title": ""}
        except ValueError:
            pass
        tree = SharedInventory(Path(config_path).parent).peek("tree", max_age=5)
        if tree is not None:
            return core.find_workspace(tree, selector)
        client = _client(config)
    return core.find_workspace(client.tree(), selector)


def authorize_workspace(config_path, selector, name=None, *, client=None):
    store = core.ConfigStore(Path(config_path))
    record = workspace_record(config_path, selector, store.load(), client)
    def authorize(latest):
        rule = next((r for r in latest["workspace_rules"] if r.get("workspace_id") == record["workspace_id"]), None)
        if rule is None:
            rule = core._workspace_rule_from_record(record, name)
            latest["workspace_rules"].append(rule)
        # w is idempotent. Only W can undo P; manual exclusions also survive.
        rule["batch_reconcile_requested_at"] = time.time()
        return dict(rule)
    _, rule, _ = store.mutate(authorize)
    return {"rule": rule, "reconciliation": "queued"}


def settled_job(config_path, previous, config, client):
    """An explicit new B may follow a finished batch with definitive failures.

    Running/uncertain jobs keep their original 50 slots. A live tab previously
    labelled closed is also retained for reconciliation. Only a new topology
    read can distinguish it from an actually closed tab; failure to read keeps
    the old job, without creating replacements or resetting its delivery log.
    The caller holds this job's worker lock through the authorization change.
    """
    if previous.get("status") != "needs_attention":
        return False
    try:
        slots = previous.get("slots", [])
        if not slots:
            return False
        for slot in slots:
            if slot.get("phase") not in {"confirmed", "surface_closed", "blocked"}:
                return False
            if slot.get("phase") == "blocked" and slot.get("error") not in {
                "此 session 已有任务，未发送批量 prompt", "此路授权已被修改，未发送 prompt",
            }:
                return False  # Legacy recoverable waits are still resumed.
        closed = {s.get("surface_id") for s in slots if s.get("phase") == "surface_closed"}
        if closed:
            reader = client or _client(config)
            tree = reader.fresh_tree() if isinstance(reader, SnapshotClient) else reader.tree()
            present = {r["surface_id"] for r in core.workspace_surface_records(tree, previous["workspace_id"]).values()}
            if closed & present:
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def start(config_path, selector, *, client=None, launch=True):
    from ccc_batch_guard import AUTOMATIC_POOL_STOP
    guarded = launch and AUTOMATIC_POOL_STOP
    store = core.ConfigStore(Path(config_path))
    config = store.load()
    workspace = workspace_record(config_path, selector, config, client)
    wid = workspace["workspace_id"]
    with core.FileLock(Path(config_path).parent / f"batch-start-{wid}.lock", timeout_sec=5), contextlib.ExitStack() as job_locks:
        config = store.load()
        rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == wid), {})
        if config.get("mode") != "armed" or config.get("global_paused"):
            raise RuntimeError("全局当前未开启续跑；请先按 A 开启，再创建本池")
        resume_success = False
        if guarded and rule.get("pause_origin") == "batch_first_response":
            from ccc_batch_guard import snapshot
            state = snapshot(config_path, wid)
            trip = state.get("trip") or {}
            resume_success = (state.get("phase") == "stopped" and trip.get("connected") is True
                              and trip.get("within_deadline") is True)
        if (rule.get("paused") and not resume_success) or not rule.get("enabled", True):
            raise RuntimeError("本池已暂停；请先按 W 恢复，再创建或补做")
        cancel_epoch = rule.get("batch_cancelled_at")
        previous = core.load_json(job_path(config_path, rule["last_batch_id"]), {}) if rule.get("last_batch_id") else {}
        writable = True
        if previous:
            path = job_path(config_path, previous["id"])
            try:
                job_locks.enter_context(core.FileLock(path.parent / "worker.lock", timeout_sec=0))
            except (OSError, RuntimeError):
                # A worker owns this job. Reuse its identity, but never replace
                # its progress with the snapshot read before taking the lock.
                writable = False
            else:
                previous = core.load_json(path, {})
        if (previous and (not writable or (
                previous.get("status") not in {"complete", "stopped_success"}
                and previous.get("created_at", 0) > rule.get("batch_success_at", 0)
                and not settled_job(config_path, previous, config, client)))):
            job = previous  # Repeated clicks and retries reuse the same 50 slots.
        else:
            job = {"id": str(uuid.uuid4()), "workspace_id": wid,
                   "created_at": time.time(), "status": "pending",
                   "slots": [{"index": i, "phase": "pending"} for i in range(COUNT)]}
            job_locks.enter_context(core.FileLock(job_path(config_path, job["id"]).parent / "worker.lock", timeout_sec=0))
        if writable:
            job["config_path"] = str(Path(config_path).resolve())
            if guarded:
                job["guard_version"] = 1
            if launch:
                job["launch_mode"] = "guarded" if guarded else "native"
            core.atomic_write_json(job_path(config_path, job["id"]), job)
        def authorize(latest):
            current = next((r for r in latest["workspace_rules"] if r.get("workspace_id") == wid), None)
            if (latest.get("mode") != "armed" or latest.get("global_paused")
                    or (current and current.get("paused") and not
                        (resume_success and current.get("pause_origin") == "batch_first_response"))):
                raise RuntimeError("授权状态已改变，批量创建已取消")
            if (current or {}).get("batch_cancelled_at") != cancel_epoch:
                raise RuntimeError("本池刚被暂停，旧的创建请求已取消")
            if current is None:
                current = core._workspace_rule_from_record(workspace)
                latest["workspace_rules"].append(current)
            if not current.get("enabled", True):
                raise RuntimeError("本池已禁用")
            current.update(active_batch_id=job["id"], last_batch_id=job["id"])
            if resume_success:
                current.update(paused=False)
                current.pop("paused_at", None)
            if guarded:
                current.setdefault("batch_guard", {"version": 1, "origin_job_id": job["id"]})
        store.mutate(authorize)
        if guarded:
            from ccc_batch_guard import arm
            arm(config_path, wid, resume=resume_success)
        # Neither the old reconciler nor the new helper may run until the
        # authorization commit above is durable. Release before spawning.
        job_locks.close()
        if launch:
            _launch(config_path, job)
        return {"job_id": job["id"], "workspace_id": wid, **counts(job)}


def sqlite_home(config_path, job_id, index):
    # Keep CODEX_HOME, transcripts, hooks and credentials in their normal
    # locations. Only the native SQLite writers of this new CLI are isolated.
    return job_path(config_path, job_id).parent / "native-db"


def prepare_sqlite_home(config_path, job_id):
    """Seed only small native metadata, never the multi-GB logs/history DBs.

    An empty state DB makes Codex synchronously reindex every old rollout.
    SQLite backup preserves the real completed backfill and selected rollouts;
    no native status is fabricated. One batch shares this new runtime.
    """
    directory = sqlite_home(config_path, job_id, 0)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "seed.json"
    if marker.exists():
        return
    with core.FileLock(directory / "seed.lock", timeout_sec=5):
        if marker.exists():
            return
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        source = Path(os.environ.get("CODEX_SQLITE_HOME") or codex_home)
        try:
            import tomllib
            configured = tomllib.loads((codex_home / "config.toml").read_text()).get("sqlite_home")
            if configured:
                source = Path(configured)
        except (ImportError, OSError, ValueError):
            pass
        copied = []
        for path in sorted(source.glob("*.sqlite")):
            if not re.fullmatch(r"(?:state|goals|memories|queue)_\d+\.sqlite", path.name):
                continue
            target = directory / path.name
            if target.exists():
                continue  # Never overwrite a runtime already opened by Codex.
            temp = directory / ("." + path.name + ".seed")
            deadline = time.monotonic() + 5
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise RuntimeError("等待原生数据库元数据快照；尚未创建新 terminal")
            try:
                with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
                    if path.name.startswith("state_"):
                        status = src.execute("SELECT status FROM backfill_state WHERE id=1").fetchone()
                        if not status or status[0] != "complete":
                            continue
                    with contextlib.closing(sqlite3.connect(temp)) as dst:
                        src.backup(dst, pages=256, progress=progress, sleep=.05)
                temp.chmod(0o600)
                temp.replace(target)
                copied.append(path.name)
            except sqlite3.Error as exc:
                raise RuntimeError("等待原生数据库元数据快照：" + str(exc)) from exc
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temp.unlink()
        core.atomic_write_json(marker, {"at": time.time(), "metadata": copied})


def register(config_path, job_id, index, launch_id=""):
    """Runs in the newly created shell before Codex starts (without a prompt)."""
    path = job_path(config_path, job_id)
    job = core.load_json(path, {})
    if not 0 <= index < len(job["slots"]):
        raise RuntimeError("invalid batch slot")
    if (job["slots"][index].get("launch_id") or "") != launch_id:
        raise RuntimeError("stale batch launch")
    sid, wid = os.environ.get("CMUX_SURFACE_ID", ""), os.environ.get("CMUX_WORKSPACE_ID", "")
    uuid.UUID(sid)
    if wid != job["workspace_id"]:
        raise RuntimeError("new surface workspace does not match its batch")
    store = core.ConfigStore(Path(config_path))
    receipt = path.parent / f"surface-{index}.json"
    with core.workspace_input_lock(config_path, wid, shared=True):
        def protect(latest):
            if not allowed(latest, job):
                raise RuntimeError("batch paused or cancelled before launch")
            rule = core.workspace_rule_by_id(latest, wid)
            previous = core.batch_start_hold(rule, sid)
            if sid in rule.get("excluded_surface_ids", []) and not (previous or {}).get("legacy"):
                raise RuntimeError("new surface was excluded by its operator")
            if previous and previous.get("job_id") != job_id:
                raise RuntimeError("surface already belongs to another batch")
            # A late/repeated bootstrap cannot put a proven session on hold.
            if job["slots"][index].get("phase") != "confirmed":
                rule.setdefault("batch_start_holds", {})[sid] = {
                    "job_id": job_id, "index": index, "created_at": time.time()}
        with core.FileLock(receipt.with_suffix(".lock"), timeout_sec=5):
            job = core.load_json(path, {})
            if (job["slots"][index].get("launch_id") or "") != launch_id:
                raise RuntimeError("stale batch launch")
            old = core.load_json(receipt, {})
            if old and old.get("surface_id") != sid:
                raise RuntimeError("batch slot already belongs to another surface")
            store.mutate(protect)
            prepare_sqlite_home(config_path, job_id)
            core.atomic_write_json(receipt, {"surface_id": sid, "workspace_id": wid,
                                            "launch_id": launch_id, "registered_at": time.time(),
                                            "shell_pid": os.getppid(),
                                            "shell_start": batch_shell_identity(os.getppid())})


class BatchWorker:
    def __init__(self, config_path, job_id, *, client=None, queue=None, clock=time.time, pty_probe=None):
        self.config_path = Path(config_path)
        self.path = job_path(config_path, job_id)
        self.store = core.ConfigStore(self.config_path)
        self.job = core.load_json(self.path, {})
        self.cache = SnapshotCache(workers=1)
        self.inventory = SharedInventory(self.config_path.parent)
        self.client = client or SnapshotClient(_client(self.store.load()), self.cache,
                                               SharedInventory(self.config_path.parent))
        self.queue = queue or QueueRecovery(self.path.parent / "unused-queue-ledger.json",
            Path.home() / ".cmuxterm/codex-hook-sessions.json", Path.home() / ".codex/sessions", PROMPT)
        self.processes = {}
        self.queue.process_lookup = self._process_label
        self.clock = clock
        self.pty_probe = pty_probe or pty_available
        self._top_due = 0.0
        self._saved = None
        self._shell_hints = {}

    def _process_label(self, target):
        from ccc_batch_guard import binding
        guarded = binding(self.config_path, target)
        if guarded:
            return {"agent_kind": "codex", "agent_pids": [guarded["pid"]], "summary": "guarded native backend"}
        slot = next((s for s in self.job["slots"] if s.get("surface_id") == target["surface_id"]), {})
        if slot:
            receipt = core.load_json(self.path.parent / f"surface-{slot['index']}.json", {})
            if (receipt.get("surface_id") == target["surface_id"]
                    and receipt.get("workspace_id") == target["workspace_id"]):
                hint = (receipt.get("shell_pid"), receipt.get("shell_start"))
                if not hint[1]:
                    hint = self._shell_hints.get(target["surface_id"], hint)
                if not hint[1]:
                    # Legacy receipts did not pin their shell. A recent top
                    # is only a PID hint; direct generation/child checks are
                    # the evidence, so another full scan is unnecessary.
                    record = self.inventory._read("top")
                    if 0 <= self.clock() - record.get("collected_at", 0) < 120:
                        surface = next((r for r in core._walk_objects(record.get("value", {}))
                                        if r.get("kind") == "surface" and r.get("id") == target["surface_id"]), {})
                        for proc in core._walk_objects(surface.get("processes", [])):
                            started = batch_shell_identity(proc.get("pid"))
                            if started and started[0] <= record["collected_at"]:
                                hint = (proc["pid"], started)
                                self._shell_hints[target["surface_id"]] = hint
                                break
                label = batch_child_label(*hint, target)
                if label:
                    return label
                if hint[1] and sys.platform == "darwin":
                    return {"agent_kind": "unknown", "summary": "等待原启动进程确认"}
        if self.processes:
            return core.surface_process_label(self.processes, target)
        # A persisted submit pins an original PID/start/session. It is only a
        # lookup hint: QueueRecovery rechecks placement, start and writable
        # rollout, and _confirm checks all three identity fields again.
        if slot.get("submit_at") and slot.get("pid") and slot.get("process_start"):
            return {"agent_kind": "codex", "agent_pids": [slot["pid"]]}
        return {"agent_kind": "unknown", "summary": "process lookup unavailable"}

    def save(self):
        value = {k: v for k, v in self.job.items() if k != "updated_at"}
        serialized = json.dumps(value, sort_keys=True)
        if serialized == self._saved:
            return
        self.job["updated_at"] = self.clock()
        core.atomic_write_json(self.path, self.job)
        self._saved = serialized

    def _target(self, slot, *, fresh=False):
        tree = (self.client.fresh_tree() if fresh and isinstance(self.client, SnapshotClient)
                else self.client.tree())
        target = core.find_main_surface(tree, slot["surface_id"])
        if target["workspace_id"] != self.job["workspace_id"]:
            raise RuntimeError("surface moved out of its authorized workspace")
        return target

    def _create(self, slot):
        if self.clock() < slot.get("retry_at", 0):
            return
        wid = self.job["workspace_id"]
        with core.workspace_input_lock(self.config_path, wid, shared=True):
            if not allowed(self.store.load(), self.job):
                return
            if not self.pty_probe():
                self.job.update(status="waiting", error="系统 PTY 名额已满；保留剩余名额，空位释放后自动继续")
                return
            prepare_sqlite_home(self.config_path, self.job["id"])
            tree = self.client.tree()
            panes = [(win, p) for win in tree.get("windows", []) for w in win.get("workspaces", [])
                     if w.get("id") == wid for p in w.get("panes", [])
                     if p.get("dock_scope") is None and p.get("id")]
            if not panes:
                raise RuntimeError("等待目标 workspace 主区域 pane")
            win, pane = next((pair for pair in panes if pair[1].get("id") == self.job.get("pane_id")),
                             next((pair for pair in panes if pair[1].get("focused")), panes[0]))
            self.job.update(window_id=win["id"], pane_id=pane["id"])
            if not self._reserve_start(slot):
                return
            # Persist BEFORE creating; an uncertain reply is reconciled from
            # the bootstrap receipt and must never cause a replacement tab.
            command = self._launch_command(slot)
            try:
                slot["surface_id"] = self.client.new_codex_surface(
                    self.job["window_id"], wid, self.job["pane_id"], command)
                slot["phase"] = "created"
                self._protect_created(slot)
            except core.CmuxRequestRejected as exc:
                self._defer_rejected_start(slot, str(exc))
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="create_unknown", error=str(exc))
            self.save()

    def _defer_rejected_start(self, slot, error, *, restarting=False):
        attempts = slot.get("rejected_start_attempts", 0) + 1
        slot.update(phase="restart_pending" if restarting else "pending", error=error,
                    rejected_start_attempts=attempts, rejected_start_at=self.clock(),
                    retry_at=self.clock() + min(30, 2 ** min(attempts, 5)))
        # The rejected request did not consume the one allowed restart. Every
        # retry must still pass _restart_failed's original-session checks.
        if restarting:
            slot.pop("restart_attempt_at", None)

    def _recover_rejected_start(self, slot, receipt):
        """Recover v16's persisted refusal without replaying uncertain I/O."""
        phase = slot["phase"]
        if phase not in {"create_unknown", "restart_unknown"}:
            return False
        command = "respawn-pane --window" if phase == "restart_unknown" else "--json --id-format"
        if slot.get("error") != f"cmux {command} failed: {core.CmuxRequestRejected.POLLING_RATE_LIMIT}":
            return False
        if (slot.get("session_id") or slot.get("native_seen_session_id") or slot.get("submit_at")
                or (phase == "create_unknown" and slot.get("surface_id"))):
            return False
        if receipt:
            # A receipt from this launch is stronger than the stored error;
            # let the ordinary receipt path reconcile it, never replay it.
            if (receipt.get("workspace_id") != self.job["workspace_id"]
                    or receipt.get("launch_id") == slot.get("launch_id")
                    or receipt.get("surface_id") != slot.get("surface_id")):
                return False
        self._defer_rejected_start(slot, slot["error"], restarting=phase == "restart_unknown")
        return True

    def _launch_command(self, slot):
        bootstrap = shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), "register",
                                "--config", str(self.config_path), "--job", self.job["id"],
                                "--index", str(slot["index"]), "--launch-id", slot["launch_id"]])
        from ccc_batch_guard import AUTOMATIC_POOL_STOP, native_binary
        native = shlex.join([native_binary(), "-c", "sqlite_home=" + json.dumps(
            str(sqlite_home(self.config_path, self.job["id"], slot["index"]).resolve()))])
        # The guard relay is only the auto-pause cut. With that cut off, Codex
        # starts in this terminal. A dead unix endpoint never receives the prompt.
        if self.job.get("guard_version") == 1 and AUTOMATIC_POOL_STOP:
            native = shlex.join([sys.executable, "-B", str(Path(__file__).with_name("ccc_batch_guard.py")),
                "launch", "--config", str(self.config_path), "--job", self.job["id"], "--index", str(slot["index"])])
        return bootstrap + " && " + native

    def _protect_created(self, slot):
        def protect(config):
            if not allowed(config, self.job):
                return
            rule = core.workspace_rule_by_id(config, self.job["workspace_id"])
            sid = slot["surface_id"]
            if sid in rule.get("excluded_surface_ids", []):
                return
            rule.setdefault("batch_start_holds", {}).setdefault(sid, {
                "job_id": self.job["id"], "index": slot["index"], "created_at": self.clock()})
        self.store.mutate(protect)

    @staticmethod
    def _pty_failure(grid):
        return "Your system cannot allocate any more pty devices." in " ".join("\n".join(grid.lines).split())

    @staticmethod
    def _startup_failure(grid):
        """A native DB startup error immediately followed by an empty shell.

        A quoted error in a Codex response, a menu or a shell draft is not a
        launch failure. The process-table check is separate and mandatory.
        """
        cursor = grid.cursor
        if not cursor.visible or not 0 <= cursor.row < len(grid.lines):
            return False
        line = grid.lines[cursor.row].ljust(grid.columns)
        prompt = line[:cursor.column]
        if (line[cursor.column:].strip() or not re.fullmatch(
                r"(?:[^\s%$#]+@[^\s%$#]+ [^%$#\r\n]* )?[%$#] ", prompt)):
            return False
        before = " ".join("\n".join(grid.lines[:cursor.row]).split())
        return ("Codex couldn't start because another Codex process is using its local data." in before
                and "ERROR: failed to initialize sqlite local db" in before
                and "database is locked" in before.rsplit("ERROR:", 1)[-1])

    @staticmethod
    def _guard_refusal(grid):
        """The original launch already ran and the guard rejected it.

        The shell is sitting on that traceback. Codex never started, so the
        same surface can run the original command again.
        """
        cursor = grid.cursor
        if not cursor.visible or not 0 <= cursor.row < len(grid.lines):
            return False
        line = grid.lines[cursor.row].ljust(grid.columns)
        prompt = line[:cursor.column]
        if (line[cursor.column:].strip() or not re.fullmatch(
                r"(?:[^\s%$#]+@[^\s%$#]+ [^%$#\r\n]* )?[%$#] ", prompt)):
            return False
        before = " ".join("\n".join(grid.lines[:cursor.row]).split())
        return (("B 工作区已停止" in before and "不会启动额外请求" in before)
                or "surface is not currently inside its authorized B workspace" in before)

    def _restart_failed(self, slot, *, no_pty=False):
        # Only pre-session startup failures owned by this batch may be
        # relaunched, in the same terminal. Never replace an existing session.
        if self.clock() < slot.get("retry_at", 0):
            return
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self.store.load()
            if not allowed(config, self.job) or not self._protected(config, slot):
                return
            if slot.get("session_id") or slot.get("native_seen_session_id") or slot.get("submit_at"):
                return
            if any(r.get("surfaceId") == slot["surface_id"] for r in self.queue.records().values()):
                return
            target = self._target(slot, fresh=True)
            if no_pty:
                if not self.pty_probe():
                    return
            else:
                label = self._process_label(target)
                if label.get("agent_kind") != "shell" or not label.get("process_snapshot_present"):
                    return
            if (self._native(target, slot) or {}).get("session_id"):
                return
            grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            guard_refusal = False if no_pty else self._guard_refusal(grid)
            if slot.get("restart_attempt_at"):
                return
            if guard_refusal and slot.get("guard_refusal_attempts", 0) >= 5:
                return
            if not ((self._pty_failure(grid) if no_pty else self._startup_failure(grid)) or guard_refusal):
                return
            prepare_sqlite_home(self.config_path, self.job["id"])
            if not self._reserve_start(slot, restarting=True):
                return
            if guard_refusal:
                slot["guard_refusal_attempts"] = slot.get("guard_refusal_attempts", 0) + 1
            slot["restart_attempt_at"] = self.clock()
            self.save()  # An ambiguous restart acknowledgement is not replayed.
            try:
                self.client.respawn_surface(target["window_id"], slot["surface_id"], self._launch_command(slot))
                slot["phase"] = "restart_unknown"
                slot["error"] = "等待原 surface 的启动回执"
            except core.CmuxRequestRejected as exc:
                self._defer_rejected_start(slot, str(exc), restarting=True)
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="restart_unknown", error=str(exc))
            self.save()

    def _transcript(self, sid, native):
        from ccc_batch_guard import binding
        guarded = binding(self.config_path, {"surface_id": sid, "workspace_id": self.job["workspace_id"]})
        if guarded and guarded.get("session_id") == native.get("session_id") and guarded.get("transcript"):
            return guarded["transcript"]
        try:
            binding = self.queue.records().get(native["session_id"], {})
        except (OSError, ValueError):
            binding = {}
        if binding.get("surfaceId") == sid and binding.get("workspaceId") == self.job["workspace_id"]:
            return str(binding.get("transcriptPath") or "")
        return str(self.queue.open_file_sources.get(sid, {}).get("path") or "")

    def _confirm(self, slot):
        """Incrementally prove the original first task, never a later prompt.

        The cursor and partial JSON line survive worker restarts. Context may
        span several reads; an unchanged file causes no payload reread.
        """
        if not slot.get("transcript") and slot.get("native_uninitialized"):
            # Finding the first rollout is read-only and already pinned to
            # the submitted PID/start/session. A topology refresh must not
            # delay proof or keep its startup permit occupied. QueueRecovery
            # checks the live native binding; input still requires fresh tree.
            target = {"surface_id": slot["surface_id"], "workspace_id": self.job["workspace_id"]}
            native = self.queue.current_turn(target)
            if not native or any(native.get(key) != slot.get(key) for key in ("session_id", "pid", "process_start")):
                return False
            slot["transcript"] = self._transcript(slot["surface_id"], native)
        path = Path(slot.get("transcript") or "/nonexistent")
        try:
            stat = path.stat()
            proof = slot.setdefault("confirmation", {"offset": slot["transcript_offset"], "session_id": slot["session_id"]})
            identity = [stat.st_dev, stat.st_ino]
            if proof.get("session_id", slot["session_id"]) != slot["session_id"]:
                proof["blocked"] = "original session changed"
            if proof.get("identity", identity) != identity or stat.st_size < proof["offset"]:
                proof["blocked"] = "original transcript changed or truncated"
            if proof.get("blocked"):
                return False
            if proof.get("confirmed"):
                return True
            if proof.get("identity") and stat.st_size == proof["offset"]:
                return False
            with path.open("rb") as handle:
                if not proof.get("identity"):
                    meta = json.loads(handle.readline())
                    if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != slot.get("session_id"):
                        proof["blocked"] = "original session mismatch"
                        return False
                    proof["identity"] = identity
                    proof["session_id"] = slot["session_id"]
                handle.seek(proof["offset"])
                data = handle.read(CONFIRM_READ_BYTES)
                proof["offset"] = handle.tell()
            data = base64.b64decode(proof.pop("partial", "")) + data
            lines = data.split(b"\n")
            tail = lines.pop()
            if tail:
                proof["partial"] = base64.b64encode(tail).decode("ascii")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("invalid native event")
                    if event.get("type") not in {"event_msg", "response_item"}:
                        continue
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        raise ValueError("invalid native payload")
                except (ValueError, KeyError, TypeError):
                    proof["blocked"] = "invalid original transcript event"
                    return False
                kind = payload.get("type") if event.get("type") == "event_msg" else ""
                if kind == "task_started":
                    if epoch(event["timestamp"]) < slot["submit_at"] - .001 or proof.get("started"):
                        proof["blocked"] = "different task before batch prompt"
                        return False
                    proof.update(started=True, task_id=payload.get("turn_id"), task_at=event["timestamp"])
                if kind in {"task_complete", "task_aborted"} and not proof.get("prompt"):
                    proof["blocked"] = "task ended before batch prompt"
                    return False
                message = None
                if kind == "user_message":
                    message = payload.get("message")
                elif event.get("type") == "response_item" and payload.get("role") == "user":
                    content = payload.get("content", [])
                    if not content or any(p.get("type") != "input_text" for p in content):
                        proof["blocked"] = "operator input before batch prompt"
                        return False
                    message = "\n".join(p.get("text", "") for p in content)
                    if _startup_context(message):
                        continue
                if message is not None:
                    if message != PROMPT:
                        proof["blocked"] = "different user prompt"
                        return False
                    proof["prompt"] = True
                if proof.get("started") and proof.get("prompt"):
                    proof.update(confirmed=True, confirmed_at=self.clock())
                    proof.pop("partial", None)
                    return True
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return False

    def _release(self, slot):
        if slot.get("phase") != "confirmed" or not slot.get("confirmation", {}).get("confirmed"):
            return
        def release(config):
            rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == self.job["workspace_id"]), {})
            reasons = rule.get("excluded_surface_reasons", {})
            sid = slot["surface_id"]
            if reasons.get(sid) == f"batch:{self.job['id']}:initial":
                reasons.pop(sid)
                rule["excluded_surface_ids"] = [s for s in rule.get("excluded_surface_ids", []) if s != sid]
            if rule.get("batch_start_holds", {}).get(sid, {}).get("job_id") == self.job["id"]:
                rule["batch_start_holds"].pop(sid)
        self.store.mutate(release)
        slot["hold_released_at"] = self.clock()

    def _protected(self, config, slot):
        rule = core.workspace_rule_by_id(config, self.job["workspace_id"])
        sid = slot["surface_id"]
        hold = core.batch_start_hold(rule, sid)
        if not hold or hold.get("job_id") != self.job["id"]:
            return False
        reason = rule.get("excluded_surface_reasons", {}).get(sid)
        return (not (sid in rule.get("excluded_surface_ids", []) and reason != f"batch:{self.job['id']}:initial")
                and not any(t.get("surface_id") == sid and (t.get("paused") or not t.get("enabled", True))
                            for t in config["targets"]))

    def _native(self, target, slot):
        from ccc_batch_guard import binding
        guarded = binding(self.config_path, target)
        if guarded and guarded.get("session_id"):
            return {k: guarded.get(k) for k in ("kind", "session_id", "pid", "process_start")}
        native = self.queue.current_turn(target)
        if (not native or not native.get("session_id")) and hasattr(self.queue, "initial_session"):
            native = self.queue.initial_session(target, slot.get("launched_at", slot["created_at"]))
        return native

    @staticmethod
    def _own_prompt_draft(grid):
        """Only the exact recorded ASCII prompt, including a legacy pasted newline."""
        cursor = grid.cursor
        if not cursor.visible or core._menu_present(grid.lines) or core._working_present(grid.lines):
            return False
        if core._queued_followup_present(grid.lines, cursor.row):
            return False
        starts = [s.row for s in grid.spans if cursor.row - 1 <= s.row <= cursor.row
                  and s.column == 0 and s.text.startswith("›")
                  and not grid.style(s.style_id).get("faint", False)]
        if not starts:
            return False
        row = max(starts)
        if cursor.column != (2 + len(PROMPT) if row == cursor.row else 2):
            return False
        cells = [" "] * grid.columns
        for span in grid.spans:
            if not row <= span.row <= cursor.row or span.column + span.cell_width <= 2:
                continue
            if not span.text.strip() or core._is_spinner_overlay_span(grid, span):
                continue
            # cmux coalesces adjacent cells of the same style.  The padding
            # at column 1 can therefore share a span with the entire draft.
            # Clip only verified prompt chrome, never arbitrary input text.
            offset = max(0, 2 - span.column)
            if offset and span.text[:offset] != "› "[span.column:2]:
                return False
            text = span.text[offset:]
            style = grid.style(span.style_id)
            if (span.row != row or style.get("faint") or style.get("invisible")
                    or not text.isascii() or len(span.text) != span.cell_width):
                return False
            cells[max(2, span.column):span.column + span.cell_width] = text
        return "".join(cells[2:]).rstrip() == PROMPT

    def _finish_submission(self, slot):
        if slot.get("enter_attempt_at") or self.clock() - slot.get("submit_at", self.clock()) < .2:
            return
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self.store.load()
            if not allowed(config, self.job):
                return
            if not self._protected(config, slot):
                return
            target = self._target(slot, fresh=True)
            native = self._native(target, slot)
            if (not native or native.get("kind") not in {"unknown", "uninitialized"}
                    or any(native.get(k) != slot.get(k) for k in ("session_id", "pid", "process_start"))):
                return
            grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            if not self._own_prompt_draft(grid) or self._native(target, slot) != native:
                return
            slot["enter_attempt_at"] = self.clock()
            self.save()  # A missing Enter acknowledgement is never retried.
            try:
                self.client.send_key(target["workspace_id"], target["surface_id"], "enter")
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="uncertain", error=str(exc))

    def _advance(self, slot, *, confirmation_only=False):
        receipt = core.load_json(self.path.parent / f"surface-{slot['index']}.json", {})
        if not confirmation_only and self._recover_rejected_start(slot, receipt):
            return
        if receipt and receipt.get("workspace_id") == self.job["workspace_id"]:
            if slot.get("surface_id") not in {None, receipt["surface_id"]}:
                raise RuntimeError("create reply and bootstrap receipt disagree")
            slot["surface_id"] = receipt["surface_id"]
            if slot["phase"] in {"creating", "create_unknown"}:
                slot["phase"] = "created"
            if slot["phase"] in {"restarting", "restart_unknown"}:
                if receipt.get("launch_id") != slot.get("launch_id"):
                    return
                slot.update(phase="created", launched_at=receipt["registered_at"])
        if slot["phase"] in CONFIRMABLE:
            if self._confirm(slot):
                slot["phase"] = "confirmed"
                slot.pop("error", None)
                # Proof and phase are durable before changing authorization.
                # A crash on either side is repaired without replaying input.
                self.save()
                rule = next((r for r in self.store.load()["workspace_rules"]
                             if r.get("workspace_id") == self.job["workspace_id"]), {})
                if core.batch_start_hold(rule, slot["surface_id"]):
                    self._release(slot)
                    self.save()
            elif not confirmation_only:
                self._finish_submission(slot)
            return
        if confirmation_only:
            return
        if slot["phase"] not in {"created", "startup_wait", "restart_pending", "pty_wait"}:
            return
        if not receipt:
            slot["error"] = "等待新 shell 原始回执"
            if self.clock() - slot["created_at"] >= 3 and slot.get("surface_id"):
                target = self._target(slot)
                grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
                if self._pty_failure(grid):
                    self._protect_created(slot)
                    slot.update(phase="pty_wait", error="系统 PTY 名额已满；等待空位后在原 surface 补做")
                    self._restart_failed(slot, no_pty=True)
            return
        if not self._protected(self.store.load(), slot):
            slot.update(phase="blocked", error="此路授权已被修改，未发送 prompt")
            return
        target = self._target(slot)
        native = self._native(target, slot)
        if (native or {}).get("session_id"):
            slot["native_seen_session_id"] = native["session_id"]
        if native and native.get("session_id") and native.get("kind") not in {"unknown", "uninitialized"}:
            slot.update(phase="blocked", error="此 session 已有任务，未发送批量 prompt")
            return
        grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
        if core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty":
            if not (native or {}).get("session_id") and (self._startup_failure(grid) or self._guard_refusal(grid)):
                slot.update(phase="restart_pending", error=(
                    "B 启动被停止标记拒绝，等待在原 surface 重开 Codex"
                    if self._guard_refusal(grid) else
                    "Codex 数据库锁导致启动失败，等待在原 surface 补做"))
                self._restart_failed(slot)
                return
            if core._menu_present(grid.lines) or core._composer_status(grid)[0] == "composer_busy":
                slot["phase"] = "startup_wait"
            elif self._process_label(target).get("agent_kind") == "codex":
                slot["phase"] = "created"
            slot["error"] = "等待空输入框；启动确认、草稿或运行中任务不会被覆盖"
            return
        if not native or not native.get("session_id") or not native.get("pid"):
            slot["error"] = "等待 Codex 原 session 就绪"
            return
        slot["session_id"] = native["session_id"]
        # Any existing task/user message belongs to an operator, not this
        # unsubmitted batch slot. Never inject the initial prompt into it.
        if native.get("kind") not in {"unknown", "uninitialized"}:
            slot.update(phase="blocked", error="此 session 已有任务，未发送批量 prompt")
            return
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self.store.load()
            if not allowed(config, self.job):
                return
            if not self._protected(config, slot):
                slot.update(phase="blocked", error="此路授权已被修改，未发送 prompt")
                return
            target = self._target(slot, fresh=True)
            grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            if (core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty"
                    or self._native(target, slot) != native):
                return
            path = self._transcript(slot["surface_id"], native)
            uninitialized = native.get("kind") == "uninitialized"
            if not path and not uninitialized:
                return
            try:
                offset = Path(path).stat().st_size if path else 0
            except FileNotFoundError:
                # Native thread/start returns the future rollout path before
                # its first turn creates the file. Only a proven fresh native
                # session may start with offset zero.
                if not uninitialized:
                    raise
                offset = 0
            slot.update(phase="submitting", submit_at=self.clock(), transcript=path,
                        transcript_offset=offset, pid=native["pid"],
                        process_start=native.get("process_start"), native_uninitialized=uninitialized)
            self.save()
            try:
                self.client.send_text(target["workspace_id"], target["surface_id"], PROMPT)
                slot["phase"] = "submitted"
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="uncertain", error=str(exc))
            self.save()

    def _reserve_start(self, slot, *, restarting=False):
        # All batch processes share the same capacity and rate limit. Save the
        # reservation before releasing the lock, without holding it over RPC.
        with core.FileLock(self.config_path.parent / "batch-capacity.lock", timeout_sec=2):
            path = self.config_path.parent / "batch-capacity.json"
            budget = core.load_json(path, {})
            now = self.clock()
            if now - budget.get("last_start", 0) < .5:
                return False
            config = self.store.load()
            ids = sorted({r["active_batch_id"] for r in config["workspace_rules"] if r.get("active_batch_id")})
            active = 0
            pending_jobs = []
            for jid in ids:
                job = self.job if jid == self.job["id"] else core.load_json(job_path(self.config_path, jid), {})
                if not job or job.get("status") in {"cancelled", "workspace_closed"} or not allowed(config, job):
                    continue
                # An ambiguous RPC remains durable, but cannot monopolize a
                # startup permit forever. It is still reconciled, never replayed.
                # Native initialization still consumes capacity after cmux
                # creates the tab. Bound its lease so a stalled startup cannot
                # monopolize every pool, but do not flood 50 cold SQLite writers.
                active += sum(s.get("phase") in INITIALIZING and
                              now - s.get("launched_at", s.get("created_at", now)) < STARTUP_LEASE_SEC
                              for s in job.get("slots", []))
                if any(s.get("phase") in STARTABLE for s in job.get("slots", [])):
                    pending_jobs.append(jid)
            if active >= 4 or now - budget.get("last_start", 0) < .5:
                return False
            # A busy first pool cannot consume every available startup slot.
            last = budget.get("last_job", "")
            if not pending_jobs:
                return False
            next_job = next((jid for jid in pending_jobs if jid > last), pending_jobs[0])
            if next_job != self.job["id"] and now - budget.get("last_start", 0) < 1.5:
                return False
            slot.update(phase="restarting" if restarting else "creating", launched_at=now,
                        launch_id=str(uuid.uuid4()))
            slot.setdefault("created_at", now)
            self.save()
            core.atomic_write_json(path, {"last_start": now, "last_job": self.job["id"]})
            return True

    def _refresh_processes(self):
        if isinstance(self.client, SnapshotClient):
            labels = self.client.process_labels(self.job["workspace_id"], core.classify_surface_processes, wait=False)
            self.processes = labels or {}
        elif self.clock() >= self._top_due:
            self._top_due = self.clock() + 5
            self.processes = core.classify_surface_processes(self.client.top_all())

    def _membership_tree(self):
        """A cached absence is not evidence that a newly created tab closed.

        A tree request can start before creation and finish after its reply.
        Its cache TTL and the five-second startup grace do not order those
        events. Confirm an absence with a request started by this worker now.
        The result is still read-only; prompt delivery keeps its own preflight.
        """
        tree = self.client.tree()
        if isinstance(self.client, SnapshotClient):
            records = core.workspace_surface_records(tree, self.job["workspace_id"])
            present = {r["surface_id"] for r in records.values()}
            workspace_present = any(w.get("id") == self.job["workspace_id"]
                                    for win in tree.get("windows", []) for w in win.get("workspaces", []))
            missing = any(s.get("surface_id") and s["surface_id"] not in present
                          and s.get("phase") in INITIALIZING | {"startup_wait", "restart_pending", "pty_wait", "surface_closed"}
                          and self.clock() - s.get("launched_at", s.get("created_at", self.clock())) >= 5
                          for s in self.job.get("slots", []))
            if not workspace_present or missing:
                tree = self.client.fresh_tree()
        return tree

    def _restore_present_slots(self, tree):
        """Recheck old false closures in the same tab; never create a replacement.

        Require both current membership and the original launch receipt.
        Submitted/uncertain work returns only to its confirmation path, and
        the normal authorization, native session and composer checks remain.
        """
        present = {r["surface_id"] for r in core.workspace_surface_records(tree, self.job["workspace_id"]).values()}
        restored = False
        for slot in self.job.get("slots", []):
            if slot.get("phase") != "surface_closed" or slot.get("surface_id") not in present:
                continue
            receipt = core.load_json(self.path.parent / f"surface-{slot['index']}.json", {})
            if (not slot.get("launch_id") or receipt.get("launch_id") != slot["launch_id"]
                    or receipt.get("surface_id") != slot["surface_id"]
                    or receipt.get("workspace_id") != self.job["workspace_id"]):
                continue
            phase = "uncertain" if slot.get("submit_at") else "created"
            slot.update(phase=phase, closure_rechecked_at=self.clock())
            slot.pop("retry_at", None)
            slot.pop("error", None)
            restored = True
        if restored:
            self.job["status"] = "running"
        return restored

    def step(self):
        # Disk evidence is independent of cmux's process-table RPC and of B/P.
        # Releasing a proven hold does not override P or any manual exclusion.
        rule = next((r for r in self.store.load()["workspace_rules"]
                     if r.get("workspace_id") == self.job["workspace_id"]), {})
        for slot in self.job["slots"]:
            if slot.get("phase") == "confirmed" and not core.batch_start_hold(rule, slot.get("surface_id")):
                continue
            if slot.get("phase") in CONFIRMABLE:
                try:
                    self._advance(slot, confirmation_only=True)
                except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
                    slot["error"] = str(exc)
        if not allowed(self.store.load(), self.job):
            from ccc_batch_guard import snapshot
            guard = snapshot(self.config_path, self.job["workspace_id"])
            self.job["status"] = "stopped_success" if (guard.get("trip") or {}).get("connected") else "cancelled"
            self.save()
            return False
        try:
            tree = self._membership_tree()
            workspaces = [w.get("id") for win in tree.get("windows", []) for w in win.get("workspaces", [])]
            if self.job["workspace_id"] not in workspaces:
                # Only a successful, fresh inventory proves a closed pool.
                self.job["status"] = "workspace_closed"
                self.save()
                return False
        except (core.CmuxError, RuntimeError) as exc:
            self.job.update(status="waiting", error=str(exc))
            self.save()
            return True
        self._restore_present_slots(tree)
        try:
            self._refresh_processes()
        except (core.CmuxError, RuntimeError) as exc:
            self.processes = {}
            self.job["error"] = str(exc)
        self.job["status"] = "running"
        present = {r["surface_id"] for r in core.workspace_surface_records(tree, self.job["workspace_id"]).values()}
        for slot in self.job["slots"]:
            if slot["phase"] not in INITIALIZING | {"startup_wait", "restart_pending", "pty_wait"} or self.clock() < slot.get("retry_at", 0):
                continue
            if (slot.get("surface_id") and slot["surface_id"] not in present
                    and self.clock() - slot.get("launched_at", slot.get("created_at", self.clock())) >= 5):
                slot.update(phase="surface_closed", error="原 surface 已关闭或移出本池；不补建替代会话")
                continue
            try:
                self._advance(slot)
            except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
                slot["error"] = str(exc)
            # Recoverable waits have no abandonment deadline. Slow startup,
            # partial logs and timeouts do not create replacement sessions.
            slot["retry_at"] = max(slot.get("retry_at", 0),
                self.clock() + (5 if slot["phase"] in {"startup_wait", "pty_wait"} else 1))
            if slot["phase"] == "creating":
                slot.update(phase="create_unknown", error="等待原创建回执；不会重复创建")
        pending = next((s for s in self.job["slots"]
                        if s["phase"] == "pending" and self.clock() >= s.get("retry_at", 0)), None)
        if pending is not None:
            try:
                self._create(pending)
            except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
                self.job.update(status="waiting", error=str(exc))
        if all(s["phase"] == "confirmed" for s in self.job["slots"]):
            self.job["status"] = "complete"
            self.job.pop("error", None)
        elif all(s["phase"] in {"confirmed", "blocked", "surface_closed"} for s in self.job["slots"]):
            self.job["status"] = "needs_attention"
        self.save()
        return self.job["status"] not in {"complete", "needs_attention", "cancelled", "workspace_closed"}

    def run(self):
        try:
            with core.FileLock(self.path.parent / "worker.lock", timeout_sec=0):
                self.job = core.load_json(self.path, {})
                self.job.update(status="running", worker_pid=os.getpid(), worker_version=WORKER_VERSION)
                for slot in self.job["slots"]:
                    # v0.2.12 marked slow starts blocked at 25 s. Explicit
                    # operator-task/authorization vetoes remain blocked.
                    if (slot["phase"] == "blocked" and not slot.get("submit_at")
                            and slot.get("error") not in {"此 session 已有任务，未发送批量 prompt", "此路授权已被修改，未发送 prompt"}):
                        slot["phase"] = "created"
                self.save()
                while self.step():
                    time.sleep(.5)
        finally:
            self.cache.close()


def relevant_job_ids(config_path, config):
    ids = set()
    for rule in config.get("workspace_rules", []):
        ids.update(rule.get(key) for key in ("active_batch_id", "last_batch_id") if rule.get(key))
        ids.update(h["job_id"] for h in rule.get("batch_start_holds", {}).values() if isinstance(h, dict) and h.get("job_id"))
        for sid in rule.get("excluded_surface_ids", []):
            hold = core.batch_start_hold(rule, sid)
            if hold:
                ids.add(hold["job_id"])
    # Include every historical/partial batch in authorized pools, not only
    # last_batch_id. This also catches late registrations from an old worker.
    workspaces = {r.get("workspace_id") for r in config.get("workspace_rules", [])}
    for path in (Path(config_path).parent / "workspace-batches").glob("*/job.json"):
        job = core.load_json(path, {})
        if job.get("workspace_id") in workspaces and job.get("status") not in {"complete", "workspace_closed"}:
            ids.add(path.parent.name)
    return sorted(ids)


class BatchReconciler:
    """Repair durable holds and revive orphaned jobs without blocking watch I/O."""
    def __init__(self, config_path, client, *, launch=True):
        self.path, self.client, self.launch = Path(config_path), client, launch
        self.store = core.ConfigStore(self.path)
        self.stop = threading.Event()
        self.workers, self.launched = {}, {}
        self.thread = threading.Thread(target=self._run, name="ccc-batch-reconcile", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        for worker in self.workers.values():
            worker.cache.close()

    def cycle(self):
        config = self.store.load()
        ids = relevant_job_ids(self.path, config)
        membership = None
        for jid in ids:
            if self.stop.is_set():
                break
            path = job_path(self.path, jid)
            try:
                with core.FileLock(path.parent / "worker.lock", timeout_sec=0):
                    job = core.load_json(path, {})
                    if not job:
                        continue
                    worker = self.workers.get(jid)
                    if worker is None:
                        worker = self.workers[jid] = BatchWorker(self.path, jid, client=self.client)
                    worker.job = job
                    current_config = self.store.load()
                    if allowed(current_config, job) and any(s.get("phase") == "surface_closed" for s in job.get("slots", [])):
                        if membership is None:
                            membership = self.client.fresh_tree() if isinstance(self.client, SnapshotClient) else self.client.tree()
                        worker._restore_present_slots(membership)
                    rule = next((r for r in current_config["workspace_rules"] if r.get("workspace_id") == job["workspace_id"]), {})
                    for slot in job.get("slots", []):
                        if slot.get("phase") == "confirmed" and not core.batch_start_hold(rule, slot.get("surface_id")):
                            continue
                        if slot.get("phase") in CONFIRMABLE:
                            try:
                                worker._advance(slot, confirmation_only=True)
                            except (OSError, ValueError, RuntimeError) as exc:
                                slot["error"] = str(exc)
                    if job.get("slots") and all(s["phase"] == "confirmed" for s in job["slots"]):
                        job["status"] = "complete"
                    worker.save()
                    if (self.launch and allowed(self.store.load(), job)
                            and job.get("status") not in {"complete", "needs_attention", "workspace_closed"}
                            and time.monotonic() - self.launched.get(jid, 0) >= 10):
                        _launch(self.path, job)
                        self.launched[jid] = time.monotonic()
            except (OSError, ValueError, RuntimeError) as exc:
                # A running worker holds the lock; it owns both job and proof.
                if "lock" in str(exc).lower() and self.launch:
                    job = core.load_json(path, {})
                    if job and allowed(self.store.load(), job):
                        retire_old_worker(job)
                else:
                    logging.getLogger(core.APP_NAME).warning("batch=%s reconciliation: %s", jid, exc)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except (OSError, ValueError, RuntimeError) as exc:
                logging.getLogger(core.APP_NAME).warning("batch reconciliation: %s", exc)
            self.stop.wait(2)


def retire_old_worker(job):
    """An old panel cannot keep a pre-upgrade helper alive indefinitely.

    Only the exact batch helper is retired. Native Codex processes and
    independently owned acceptance controllers are never signalled.
    """
    pid = job.get("worker_pid")
    if job.get("worker_version", 0) >= WORKER_VERSION or type(pid) is not int or pid <= 1:
        return False
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, timeout=1)
        args = shlex.split(result.stdout.strip())
        index = next((i for i, arg in enumerate(args) if Path(arg).name == "ccc_workspace_batch.py"), -1)
        if index < 0 or args[index + 1:index + 2] != ["run"] or "--job" not in args:
            return False
        if args[args.index("--job") + 1] != job["id"]:
            return False
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "register"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--launch-id", default="")
    args = parser.parse_args()
    if args.action == "register":
        register(args.config, args.job, args.index, args.launch_id)
    else:
        BatchWorker(args.config, args.job).run()


if __name__ == "__main__":
    main()
