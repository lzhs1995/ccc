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
import subprocess
import sys
import threading
import time
import uuid

import cmux_codex_watch as core
from ccc_codex_queue import QueueRecovery, epoch
from ccc_inventory import SharedInventory
from ccc_scheduling import SnapshotCache, SnapshotClient

COUNT = 50
PROMPT = "show me u power"
INITIALIZING = {"creating", "create_unknown", "created", "submitted", "submitting", "uncertain"}
CONFIRMABLE = {"submitted", "submitting", "uncertain", "confirmed"}
CONFIRM_READ_BYTES = 1024 * 1024


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
    rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == job["workspace_id"]), {})
    return (config.get("mode") == "armed" and not config.get("global_paused")
            and rule.get("enabled", True) and not rule.get("paused")
            and rule.get("active_batch_id") == job["id"])


def counts(job):
    slots = job.get("slots", [])
    return {"created": sum(bool(s.get("surface_id")) for s in slots),
            "ready": sum(bool(s.get("session_id")) for s in slots),
            "submitted": sum(s.get("phase") in {"submitted", "confirmed"} for s in slots),
            "started": sum(s.get("phase") == "confirmed" for s in slots),
            "failed": sum(s.get("phase") in {"blocked", "uncertain", "create_unknown"} for s in slots),
            "total": len(slots)}


def snapshots(config_path, config):
    result = {}
    for rule in config.get("workspace_rules", []):
        jid = rule.get("last_batch_id")
        if jid:
            try:
                job = core.load_json(job_path(config_path, jid), {})
                result[rule["workspace_id"]] = {"id": jid, "status": job.get("status"), **counts(job)}
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


def start(config_path, selector, *, client=None, launch=True):
    store = core.ConfigStore(Path(config_path))
    config = store.load()
    workspace = workspace_record(config_path, selector, config, client)
    wid = workspace["workspace_id"]
    with core.FileLock(Path(config_path).parent / f"batch-start-{wid}.lock", timeout_sec=5):
        config = store.load()
        rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == wid), {})
        if config.get("mode") != "armed" or config.get("global_paused"):
            raise RuntimeError("全局当前未开启续跑；请先按 A 开启，再创建本池")
        if rule.get("paused") or not rule.get("enabled", True):
            raise RuntimeError("本池已暂停；请先按 W 恢复，再创建或补做")
        cancel_epoch = rule.get("batch_cancelled_at")
        previous = core.load_json(job_path(config_path, rule["last_batch_id"]), {}) if rule.get("last_batch_id") else {}
        if previous and previous.get("status") != "complete":
            job = previous  # Repeated clicks and retries reuse the same 50 slots.
        else:
            job = {"id": str(uuid.uuid4()), "workspace_id": wid,
                   "created_at": time.time(), "status": "pending",
                   "slots": [{"index": i, "phase": "pending"} for i in range(COUNT)]}
            core.atomic_write_json(job_path(config_path, job["id"]), job)
        def authorize(latest):
            current = next((r for r in latest["workspace_rules"] if r.get("workspace_id") == wid), None)
            if latest.get("mode") != "armed" or latest.get("global_paused") or (current and current.get("paused")):
                raise RuntimeError("授权状态已改变，批量创建已取消")
            if (current or {}).get("batch_cancelled_at") != cancel_epoch:
                raise RuntimeError("本池刚被暂停，旧的创建请求已取消")
            if current is None:
                current = core._workspace_rule_from_record(workspace)
                latest["workspace_rules"].append(current)
            if not current.get("enabled", True):
                raise RuntimeError("本池已禁用")
            current.update(active_batch_id=job["id"], last_batch_id=job["id"])
        store.mutate(authorize)
        if launch:
            _launch(config_path, job)
        return {"job_id": job["id"], "workspace_id": wid, **counts(job)}


def register(config_path, job_id, index):
    """Runs in the newly created shell before Codex starts (without a prompt)."""
    path = job_path(config_path, job_id)
    job = core.load_json(path, {})
    if not 0 <= index < len(job["slots"]):
        raise RuntimeError("invalid batch slot")
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
            old = core.load_json(receipt, {})
            if old and old.get("surface_id") != sid:
                raise RuntimeError("batch slot already belongs to another surface")
            store.mutate(protect)
            core.atomic_write_json(receipt, {"surface_id": sid, "workspace_id": wid})


class BatchWorker:
    def __init__(self, config_path, job_id, *, client=None, queue=None, clock=time.time):
        self.config_path = Path(config_path)
        self.path = job_path(config_path, job_id)
        self.store = core.ConfigStore(self.config_path)
        self.job = core.load_json(self.path, {})
        self.cache = SnapshotCache(workers=1)
        self.client = client or SnapshotClient(_client(self.store.load()), self.cache,
                                               SharedInventory(self.config_path.parent))
        self.queue = queue or QueueRecovery(self.path.parent / "unused-queue-ledger.json",
            Path.home() / ".cmuxterm/codex-hook-sessions.json", Path.home() / ".codex/sessions", PROMPT)
        self.processes = {}
        self.queue.process_lookup = self._process_label
        self.clock = clock
        self._top_due = 0.0
        self._saved = None

    def _process_label(self, target):
        if self.processes:
            return core.surface_process_label(self.processes, target)
        # A persisted submit pins an original PID/start/session. It is only a
        # lookup hint: QueueRecovery rechecks placement, start and writable
        # rollout, and _confirm checks all three identity fields again.
        slot = next((s for s in self.job["slots"] if s.get("surface_id") == target["surface_id"]), {})
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
        wid = self.job["workspace_id"]
        with core.workspace_input_lock(self.config_path, wid, shared=True):
            if not allowed(self.store.load(), self.job):
                return
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
            command = shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), "register",
                                  "--config", str(self.config_path), "--job", self.job["id"],
                                  "--index", str(slot["index"])]) + " && codex"
            try:
                slot["surface_id"] = self.client.new_codex_surface(
                    self.job["window_id"], wid, self.job["pane_id"], command)
                slot["phase"] = "created"
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="create_unknown", error=str(exc))
            self.save()

    def _transcript(self, sid, native):
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
            target = self._target(slot)
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
        if receipt and receipt.get("workspace_id") == self.job["workspace_id"]:
            if slot.get("surface_id") not in {None, receipt["surface_id"]}:
                raise RuntimeError("create reply and bootstrap receipt disagree")
            slot["surface_id"] = receipt["surface_id"]
            if slot["phase"] in {"creating", "create_unknown"}:
                slot["phase"] = "created"
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
        if slot["phase"] != "created":
            return
        if not receipt:
            slot["error"] = "等待新 shell 原始回执"
            return
        if not self._protected(self.store.load(), slot):
            slot.update(phase="blocked", error="此路授权已被修改，未发送 prompt")
            return
        target = self._target(slot)
        native = self._native(target, slot)
        if native and native.get("session_id") and native.get("kind") not in {"unknown", "uninitialized"}:
            slot.update(phase="blocked", error="此 session 已有任务，未发送批量 prompt")
            return
        grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
        if core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty":
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
            slot.update(phase="submitting", submit_at=self.clock(), transcript=path,
                        transcript_offset=Path(path).stat().st_size if path else 0, pid=native["pid"],
                        process_start=native.get("process_start"), native_uninitialized=uninitialized)
            self.save()
            try:
                self.client.send_text(target["workspace_id"], target["surface_id"], PROMPT)
                slot["phase"] = "submitted"
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="uncertain", error=str(exc))
            self.save()

    def _reserve_start(self, slot):
        # All batch processes share the same capacity and rate limit. Save the
        # reservation before releasing the lock, without holding it over RPC.
        with core.FileLock(self.config_path.parent / "batch-capacity.lock", timeout_sec=.1):
            config = self.store.load()
            ids = relevant_job_ids(self.config_path, config)
            active = 0
            pending_jobs = []
            for jid in ids:
                job = self.job if jid == self.job["id"] else core.load_json(job_path(self.config_path, jid), {})
                if not job or job.get("status") in {"cancelled", "workspace_closed"} or not allowed(config, job):
                    continue
                active += sum(s.get("phase") in INITIALIZING for s in job.get("slots", []))
                if any(s.get("phase") == "pending" for s in job.get("slots", [])):
                    pending_jobs.append(jid)
            path = self.config_path.parent / "batch-capacity.json"
            budget = core.load_json(path, {})
            now = self.clock()
            if active >= 4 or now - budget.get("last_start", 0) < .5:
                return False
            # A busy first pool cannot consume every available startup slot.
            last = budget.get("last_job", "")
            next_job = next((jid for jid in pending_jobs if jid > last), pending_jobs[0])
            if next_job != self.job["id"]:
                return False
            slot.update(phase="creating", created_at=now)
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
            self.job["status"] = "cancelled"
            self.save()
            return False
        try:
            tree = self.client.tree()
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
        try:
            self._refresh_processes()
        except (core.CmuxError, RuntimeError) as exc:
            self.processes = {}
            self.job["error"] = str(exc)
        self.job["status"] = "running"
        for slot in self.job["slots"]:
            if slot["phase"] not in INITIALIZING or self.clock() < slot.get("retry_at", 0):
                continue
            try:
                self._advance(slot)
            except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
                slot["error"] = str(exc)
            # Recoverable waits have no abandonment deadline. Slow startup,
            # partial logs and timeouts do not create replacement sessions.
            slot["retry_at"] = self.clock() + 1
            if slot["phase"] == "creating":
                slot.update(phase="create_unknown", error="等待原创建回执；不会重复创建")
        pending = next((s for s in self.job["slots"] if s["phase"] == "pending"), None)
        if pending is not None:
            try:
                self._create(pending)
            except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
                self.job.update(status="waiting", error=str(exc))
        if all(s["phase"] == "confirmed" for s in self.job["slots"]):
            self.job["status"] = "complete"
            self.job.pop("error", None)
        elif all(s["phase"] in {"confirmed", "blocked"} for s in self.job["slots"]):
            self.job["status"] = "needs_attention"
        self.save()
        return self.job["status"] not in {"complete", "needs_attention", "cancelled", "workspace_closed"}

    def run(self):
        try:
            with core.FileLock(self.path.parent / "worker.lock", timeout_sec=0):
                self.job = core.load_json(self.path, {})
                self.job.update(status="running", worker_pid=os.getpid(), worker_version=13)
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
                    rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == job["workspace_id"]), {})
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
                if "lock" not in str(exc).lower():
                    logging.getLogger(core.APP_NAME).warning("batch=%s reconciliation: %s", jid, exc)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except (OSError, ValueError, RuntimeError) as exc:
                logging.getLogger(core.APP_NAME).warning("batch reconciliation: %s", exc)
            self.stop.wait(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "register"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    if args.action == "register":
        register(args.config, args.job, args.index)
    else:
        BatchWorker(args.config, args.job).run()


if __name__ == "__main__":
    main()
