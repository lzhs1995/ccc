"""Durable, user-triggered creation of 50 Codex tabs in one pinned workspace.

Creation and first-prompt attempts are recorded before I/O. A timeout is never
permission to create another tab or repeat a prompt. The bootstrap receipt can
recover a lost create reply; only the original transcript confirms a start.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

import cmux_codex_watch as core
from ccc_codex_queue import QueueRecovery, epoch

COUNT = 50
PROMPT = "show me u power"


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


def start(config_path, selector, *, client=None, launch=True):
    store = core.ConfigStore(Path(config_path))
    config = store.load()
    client = client or _client(config)
    tree = client.tree()
    workspace = core.find_workspace(tree, selector)
    wid = workspace["workspace_id"]
    # Select a main-area pane in the requested workspace, never the Dock or the
    # active workspace of the panel. UUIDs stay pinned for the whole batch.
    panes = [(win, w, p) for win in tree["windows"] for w in win.get("workspaces", [])
             if w.get("id") == wid for p in w.get("panes", [])
             if p.get("dock_scope") is None and p.get("id")]
    if not panes:
        raise RuntimeError("目标 workspace 没有可用的主区域 pane")
    win, _, pane = next((item for item in panes if item[2].get("focused")), panes[0])
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
            job = {"id": str(uuid.uuid4()), "workspace_id": wid, "window_id": win["id"],
                   "pane_id": pane["id"], "created_at": time.time(), "status": "pending",
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
            path = job_path(config_path, job["id"])
            with (path.parent / "worker.log").open("ab") as log:
                subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "run",
                                  "--config", str(config_path), "--job", job["id"]],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
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
            excluded = rule.setdefault("excluded_surface_ids", [])
            reasons = rule.setdefault("excluded_surface_reasons", {})
            reason = f"batch:{job_id}:initial"
            if sid in excluded and reasons.get(sid) != reason:
                raise RuntimeError("new surface was excluded by its operator")
            if sid not in excluded:
                excluded.append(sid)
            reasons[sid] = reason
        store.mutate(protect)
        with core.FileLock(receipt.with_suffix(".lock"), timeout_sec=5):
            old = core.load_json(receipt, {})
            if old and old.get("surface_id") != sid:
                raise RuntimeError("batch slot already belongs to another surface")
            core.atomic_write_json(receipt, {"surface_id": sid, "workspace_id": wid})


class BatchWorker:
    def __init__(self, config_path, job_id, *, client=None, queue=None, clock=time.time):
        self.config_path = Path(config_path)
        self.path = job_path(config_path, job_id)
        self.store = core.ConfigStore(self.config_path)
        self.job = core.load_json(self.path, {})
        self.client = client or _client(self.store.load())
        self.queue = queue or QueueRecovery(self.path.parent / "unused-queue-ledger.json",
            Path.home() / ".cmuxterm/codex-hook-sessions.json", Path.home() / ".codex/sessions", PROMPT)
        self.processes = {}
        self.queue.process_lookup = lambda target: core.surface_process_label(self.processes, target)
        self.clock = clock

    def save(self):
        self.job["updated_at"] = self.clock()
        core.atomic_write_json(self.path, self.job)

    def _target(self, slot):
        target = core.find_main_surface(self.client.tree(), slot["surface_id"])
        if target["workspace_id"] != self.job["workspace_id"]:
            raise RuntimeError("surface moved out of its authorized workspace")
        return target

    def _create(self, slot):
        wid = self.job["workspace_id"]
        with core.workspace_input_lock(self.config_path, wid, shared=True):
            if not allowed(self.store.load(), self.job):
                return
            # Persist BEFORE creating; an uncertain reply is reconciled from
            # the bootstrap receipt and must never cause a replacement tab.
            slot.update(phase="creating", created_at=self.clock())
            self.save()
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
        path = Path(slot.get("transcript") or "/nonexistent")
        try:
            with path.open("rb") as handle:
                meta = json.loads(handle.readline())
                if meta.get("payload", {}).get("id") != slot.get("session_id"):
                    return False
                handle.seek(slot["transcript_offset"])
                data = handle.read(1024 * 1024)
            started, prompt = False, False
            for line in data.splitlines():
                event = json.loads(line)
                if event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "user_message":
                    if event["payload"].get("message") != PROMPT:
                        return False
                    prompt = True
                if (event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "task_started"
                        and epoch(event["timestamp"]) >= slot["submit_at"]):
                    started = True
                if started and prompt:
                    return True
            return started and prompt
        except (OSError, ValueError, KeyError):
            pass
        return False

    def _release(self, slot):
        def release(config):
            rule = core.workspace_rule_by_id(config, self.job["workspace_id"])
            reasons = rule.get("excluded_surface_reasons", {})
            sid = slot["surface_id"]
            if reasons.get(sid) == f"batch:{self.job['id']}:initial":
                reasons.pop(sid)
                rule["excluded_surface_ids"] = [s for s in rule.get("excluded_surface_ids", []) if s != sid]
        self.store.mutate(release)

    def _advance(self, slot):
        receipt = core.load_json(self.path.parent / f"surface-{slot['index']}.json", {})
        if receipt and receipt.get("workspace_id") == self.job["workspace_id"]:
            if slot.get("surface_id") not in {None, receipt["surface_id"]}:
                raise RuntimeError("create reply and bootstrap receipt disagree")
            slot["surface_id"] = receipt["surface_id"]
            if slot["phase"] in {"creating", "create_unknown"}:
                slot["phase"] = "created"
        if slot["phase"] in {"submitted", "uncertain", "submitting"}:
            if self._confirm(slot):
                self._release(slot)
                slot["phase"] = "confirmed"
            return
        if slot["phase"] != "created":
            return
        target = self._target(slot)
        native = self.queue.current_turn(target)
        if not native or not native.get("session_id") or not native.get("pid"):
            slot["error"] = "等待 Codex 原 session 就绪"
            return
        slot["session_id"] = native["session_id"]
        # Any existing task/user message belongs to an operator, not this
        # unsubmitted batch slot. Never inject the initial prompt into it.
        if native.get("kind") != "unknown":
            slot.update(phase="blocked", error="此 session 已有任务，未发送批量 prompt")
            return
        grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
        if core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty":
            slot["error"] = "等待空输入框；启动确认、草稿或运行中任务不会被覆盖"
            return
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self.store.load()
            if not allowed(config, self.job):
                return
            rule = core.workspace_rule_by_id(config, self.job["workspace_id"])
            reason = rule.get("excluded_surface_reasons", {}).get(slot["surface_id"])
            if reason != f"batch:{self.job['id']}:initial" or any(
                    t.get("surface_id") == slot["surface_id"] and (t.get("paused") or not t.get("enabled", True))
                    for t in config["targets"]):
                slot.update(phase="blocked", error="此路授权已被修改，未发送 prompt")
                return
            target = self._target(slot)
            grid = core.Grid.from_rpc(self.client.replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            if (core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty"
                    or self.queue.current_turn(target) != native):
                return
            path = self._transcript(slot["surface_id"], native)
            if not path:
                return
            slot.update(phase="submitting", submit_at=self.clock(), transcript=path,
                        transcript_offset=Path(path).stat().st_size, pid=native["pid"])
            self.save()
            try:
                self.client.send(target["workspace_id"], target["surface_id"], PROMPT)
                slot["phase"] = "submitted"
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="uncertain", error=str(exc))
            self.save()

    def step(self):
        if not allowed(self.store.load(), self.job):
            self.job["status"] = "cancelled"
            self.save()
            return False
        try:
            self.processes = core.classify_surface_processes(self.client.top_all())
        except core.CmuxError as exc:
            self.job["error"] = str(exc)
            self.save()
            return True
        for slot in self.job["slots"]:
            if slot["phase"] in {"pending", "confirmed", "blocked"}:
                continue
            try:
                self._advance(slot)
            except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
                slot["error"] = str(exc)
            if slot["phase"] == "created" and self.clock() - slot["created_at"] > 25:
                slot["phase"] = "blocked"
            elif slot["phase"] == "creating" and self.clock() - slot["created_at"] > 25:
                slot.update(phase="create_unknown", error="创建结果未确认；不会重复创建")
        active = sum(s["phase"] in {"creating", "create_unknown", "created", "submitted", "submitting", "uncertain"}
                     and self.clock() - s.get("created_at", 0) < 25 for s in self.job["slots"])
        if active < 4:
            pending = next((s for s in self.job["slots"] if s["phase"] == "pending"), None)
            if pending is not None:
                self._create(pending)
        if all(s["phase"] == "confirmed" for s in self.job["slots"]):
            self.job["status"] = "complete"
        elif not any(s["phase"] in {"pending", "creating", "created"} or (
                s["phase"] in {"submitted", "submitting", "uncertain", "create_unknown"}
                and self.clock() - s.get("created_at", 0) < 25) for s in self.job["slots"]):
            self.job["status"] = "partial"
        self.save()
        return self.job["status"] not in {"complete", "partial", "cancelled"}

    def run(self):
        with core.FileLock(self.path.parent / "worker.lock", timeout_sec=0):
            self.job = core.load_json(self.path, {})
            self.job.update(status="running", worker_pid=os.getpid())
            for slot in self.job["slots"]:
                if slot["phase"] == "blocked" and not slot.get("submit_at"):
                    slot.update(phase="created", created_at=self.clock())
            self.save()
            deadline = self.clock() + 360
            while self.clock() < deadline and self.step():
                time.sleep(.2)
            if self.job["status"] == "running":
                self.job["status"] = "partial"
                self.save()


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
