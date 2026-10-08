"""Durable, user-triggered creation of 50 Codex tabs in one pinned workspace.

Creation and first-prompt attempts are recorded before I/O. A timeout is never
permission to create another tab or repeat a prompt. The bootstrap receipt can
recover a lost create reply; only the original transcript confirms a start.
"""
from __future__ import annotations

import argparse
import ast
import base64
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import contextlib
import copy
import ctypes
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import sqlite3
import stat
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
WORKER_VERSION = 28
LEGACY_PROMPT = "show me u power"
PROMPT = "Reply only OK. Do not use tools. End the turn."
EMPTY_CWD_POLICY = "private-empty-v1"
INITIALIZING = {"creating", "create_unknown", "created", "restarting", "restart_unknown",
                "submitted", "submitting", "uncertain"}
STARTABLE = {"pending", "restart_pending", "pty_wait"}
STARTUP_POLL_SEC = 0.025
IMMEDIATE_START_POLICY = "parallel-native-v1"
NATIVE_ACCESS_POLICY = "direct-native-v1"
ARGV_INITIAL_POLICY = "native-argv-first-task-v1"
NATIVE_RUNTIME_POLICY = "tokio-workers-2-v1"
NATIVE_TRACE_POLICY = "per-launch-log-v1"
CONFIRMABLE = {"submitted", "submitting", "uncertain", "confirmed"}
CONFIRM_READ_BYTES = 1024 * 1024
CONTEXT_PARSER_VERSION = 2
RECONCILE_CACHE_LIMIT = 16


def _file_generation(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)



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
    # Native 0.156 also emits global instructions without a directory suffix.
    # Only a complete response-item envelope is context; explicit user events
    # and text appended outside the envelope still belong to the operator.
    instructions = r"# AGENTS\.md instructions(?: for [^\r\n]+)?\r?\n\s*<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>"
    return bool(re.fullmatch(environment, message) or
                re.fullmatch(instructions + r"(?:\s*" + environment + r")?", message))


def job_path(config_path, job_id):
    uuid.UUID(job_id)
    return Path(config_path).parent / "workspace-batches" / job_id / "job.json"


def job_prompt(job):
    # Already-submitted jobs must retain their exact original confirmation and
    # draft-recovery contract across upgrades.
    value = job.get("initial_prompt", LEGACY_PROMPT)
    if not isinstance(value, str) or value not in {LEGACY_PROMPT, PROMPT}:
        raise RuntimeError("unknown B initial prompt policy")
    return value


def startup_mode(job, config_path=None):
    from ccc_access_service import is_access_job
    access = is_access_job(config_path or job.get('config_path', core.DEFAULT_CONFIG_PATH), job)
    if "native_access_policy" in job:
        from ccc_private_check import POLICY
        if (job["native_access_policy"] != NATIVE_ACCESS_POLICY or access
                or job.get("cwd_policy") != EMPTY_CWD_POLICY or job.get("initial_prompt") != PROMPT
                or job.get("check_retry_policy") != POLICY):
            raise ValueError("invalid direct native check policy")
    if access:
        return 'access_check'
    return 'private_check' if job.get('cwd_policy') == EMPTY_CWD_POLICY else 'existing'


def argv_initial(job, config_path=None):
    if "initial_prompt_policy" not in job:
        return False
    from ccc_private_check import POLICY
    if (job["initial_prompt_policy"] != ARGV_INITIAL_POLICY
            or startup_mode(job, config_path) != "private_check"
            or job.get("check_retry_policy") != POLICY
            or job.get("initial_prompt") != PROMPT
            or job.get("name_policy") is not None
            or job.get("guard_version") is not None):
        raise RuntimeError("invalid native argv initial prompt policy")
    return True


def access_cohort_prepared(job):
    slots = job.get('slots', [])
    return (len(slots) == COUNT and {s.get('index') for s in slots} == set(range(COUNT))
            and len({s.get('surface_id') for s in slots if s.get('surface_id')}) == COUNT
            and all(s.get('access_ready_at') is not None
                    and s.get('phase') in CONFIRMABLE | {'access_ready'} for s in slots))


def allowed(config, job):
    from ccc_batch_guard import blocked
    try:
        startup_mode(job)
    except (OSError, ValueError, KeyError, TypeError):
        return False
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


def preparation_progress(job):
    """Preparation is native task acceptance, not a model success counter."""
    slots = job.get("slots", [])
    def record(value):
        return value if isinstance(value, dict) else {}
    named = (sum(bool(record(s.get("naming")).get("confirmed_at")) for s in slots)
             if job.get("name_policy") else None)
    progress_times = [job.get("created_at", 0)]
    for slot in slots:
        progress_times.extend(slot.get(key, 0) for key in
                              ("created_at", "launched_at", "submit_at", "access_ready_at", "hold_released_at"))
        progress_times.extend((record(slot.get("naming")).get("confirmed_at", 0),
                               record(slot.get("confirmation")).get("confirmed_at", 0)))
    last_progress = max((v for v in progress_times if type(v) in {int, float} and 0 <= v < float("inf")), default=0)
    wait = job.get("preparation_wait")
    # Older job.error strings can outlive the wait which produced them. Only
    # expose a current, explicitly maintained wait as a present-day diagnosis.
    wait = dict(wait) if job.get("status") == "waiting" and isinstance(wait, dict) else {}
    if not wait and job.get("status") in {"running", "queued", "waiting"}:
        if any(s.get("phase") in {"created", "startup_wait"}
               and record(s.get("naming")) and not s["naming"].get("confirmed_at") for s in slots):
            wait = {"reason": "naming", "message": "等待原生命名确认"}
        elif any(s.get("phase") in {"submitted", "submitting", "uncertain"} for s in slots):
            wait = {"reason": "first_task", "message": "等待原生接受首条任务"}
        elif any(s.get("phase") in INITIALIZING | {"startup_wait", "pty_wait"} for s in slots):
            wait = {"reason": "native", "message": "等待原会话就绪或空输入框"}
        elif any(s.get("phase") in STARTABLE for s in slots):
            wait = {"reason": "scheduled", "message": "等待启动调度"}
    return {"named": named, "last_progress_at": last_progress, "wait": wait}


def snapshots(config_path, config, *, workspace_ids=None):
    result = {}
    for rule in config.get("workspace_rules", []):
        if workspace_ids is not None and rule.get("workspace_id") not in workspace_ids:
            continue
        jid = rule.get("last_batch_id")
        if jid:
            try:
                job = core.load_json(job_path(config_path, jid), {})
                result[rule["workspace_id"]] = {
                    "id": jid, "status": job.get("status"),
                    "startup_mode": startup_mode(job, config_path),
                    "native_access_policy": job.get("native_access_policy"),
                    **counts(job), **preparation_progress(job)}
                if result[rule['workspace_id']]['startup_mode'] == 'access_check':
                    from ccc_access_service import status
                    result[rule['workspace_id']]['access'] = status(config_path, jid)
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


def _bootstrap_endpoint(client, config):
    """Carry batch discovery, not input authorization, into its bootstraps."""
    base = client.client if isinstance(client, SnapshotClient) else client
    if not isinstance(base, core.CmuxClient):
        return None
    transport = base.viewport_socket
    if transport is None or not transport.path or "system.tree" not in transport.control_methods:
        return None
    info = os.stat(transport.path)
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError("bootstrap controller is not a socket")
    return {"path": transport.path, "device": info.st_dev, "inode": info.st_ino,
            "binary": config.get("cmux_path", core.DEFAULT_CMUX)}


def _bootstrap_endpoint_current(endpoint, config):
    try:
        info = os.stat(endpoint["path"])
        return (stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) ==
                (endpoint["device"], endpoint["inode"])
                and endpoint["binary"] == config.get("cmux_path", core.DEFAULT_CMUX))
    except (OSError, KeyError, TypeError):
        return False


def _bootstrap_client(config, job):
    endpoint = job.get("bootstrap_endpoint")
    if endpoint is None:
        return _client(config)
    if not _bootstrap_endpoint_current(endpoint, config):
        raise RuntimeError("original bootstrap controller identity changed")
    transport = core.CmuxViewportSocket()
    transport.configure({"protocol": "cmux-socket", "version": 2,
                         "socket_path": endpoint["path"], "access_mode": "automation",
                         "methods": ["system.tree"]})
    def no_fallback(*args, **kwargs):
        raise RuntimeError("original bootstrap controller unavailable; no CLI fallback")
    return core.CmuxClient(endpoint["binary"], runner=no_fallback, viewport_socket=transport)


def _launch(config_path, job):
    if 'standby_policy' in job:
        raise RuntimeError('standby requires its own activation manager')
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
        with contextlib.suppress(ValueError):
            latest.get("claude_excluded_workspace_ids", []).remove(record["workspace_id"])
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


def start(config_path, selector, *, client=None, launch=True, private_check=False,
          access_check=False, native_access=False, _access_fixture=False, ui_trace=None,
          _native_trace=False):
    if (any(type(mode) is not bool for mode in (private_check, access_check, native_access))
            or sum((private_check, access_check, native_access)) > 1):
        raise RuntimeError("B private-check mode must be explicitly selected")
    from ccc_batch_guard import AUTOMATIC_POOL_STOP
    guarded = launch and AUTOMATIC_POOL_STOP
    if (type(_native_trace) is not bool or
            (_native_trace and (guarded or not (private_check or native_access)))):
        raise RuntimeError('native trace requires an explicit new unguarded native check')
    store = core.ConfigStore(Path(config_path))
    config = store.load()
    workspace = workspace_record(config_path, selector, config, client)
    wid = workspace["workspace_id"]
    if ui_trace is not None:
        from ccc_batch_timing import validate
        try:
            validate(ui_trace)
            if (ui_trace['workspace_id'] != wid
                    or ui_trace['mode'] != ('N' if native_access else 'b' if private_check else 'B')
                    or access_check):
                raise ValueError('UI timing workspace or mode mismatch')
        except (ValueError, TypeError):
            ui_trace = None  # Timing metadata never changes the selected batch operation.
    new_job = False
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
            if access_check:
                raise RuntimeError("本池的暂停和原会话已保留；请在新的 workspace 使用节费50，不要为试用恢复本池旧批次")
            raise RuntimeError("本池已暂停；请先按 W 恢复，再创建或补做")
        cancel_epoch = rule.get("batch_cancelled_at")
        previous = core.load_json(job_path(config_path, rule["last_batch_id"]), {}) if rule.get("last_batch_id") else {}
        if 'standby_policy' in previous:
            if not launch or access_check or not (private_check or native_access):
                raise RuntimeError('本池保留待机批次；仅原 b/N 按钮可激活，不能复用普通启动')
            from ccc_standby_entry import activate_existing
            return activate_existing(config_path, previous,
                mode='N' if native_access else 'b', origin=ui_trace)
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
            if _native_trace and job.get('native_trace_policy') != NATIVE_TRACE_POLICY:
                raise RuntimeError('existing batch has no native trace policy; original batch preserved')
            if (native_access and job.get("native_access_policy") != NATIVE_ACCESS_POLICY
                    or access_check and startup_mode(job, config_path) != 'access_check'):
                raise RuntimeError('本池尚有原 B 批次，已保留；节费50须在新批次使用，不能将旧任务静默改成检查')
        else:
            new_job = True
            job = {"id": str(uuid.uuid4()), "workspace_id": wid,
                   "created_at": time.time(), "status": "pending",
                   "startup_policy": IMMEDIATE_START_POLICY,
                   "slots": [{"index": i, "phase": "pending"} for i in range(COUNT)]}
            if ui_trace is not None:
                job['ui_timing_origin'] = copy.deepcopy(ui_trace)
            if private_check or access_check or native_access:
                job.update(cwd_policy=EMPTY_CWD_POLICY, initial_prompt=PROMPT)
            if access_check:
                # Only the legacy gateway contract retains its title-cost
                # preparation. New native b/N starts its first check directly.
                job["name_policy"] = "before-first-turn-v1"
            if private_check or native_access:
                from ccc_private_check import POLICY
                job["check_retry_policy"] = POLICY
                if not guarded:
                    job["initial_prompt_policy"] = ARGV_INITIAL_POLICY
                    job["native_runtime_policy"] = NATIVE_RUNTIME_POLICY
                    endpoint = _bootstrap_endpoint(client if client is not None else _client(config), config)
                    if endpoint is not None:
                        job["bootstrap_endpoint"] = endpoint
            if native_access:
                # Explicit new native N. Do not reuse the legacy gateway
                # marker names or reinterpret any existing N descriptor.
                job["native_access_policy"] = NATIVE_ACCESS_POLICY
            if _native_trace:
                job['native_trace_policy'] = NATIVE_TRACE_POLICY
            job_locks.enter_context(core.FileLock(job_path(config_path, job["id"]).parent / "worker.lock", timeout_sec=0))
            if access_check:
                from ccc_access_service import prepare
                job['access_policy'] = prepare(config_path, job, fixture=_access_fixture)
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
        if ui_trace is not None:
            from ccc_batch_timing import record
            with contextlib.suppress(OSError):
                record(config_path, ui_trace, 'job_created' if new_job else 'job_reused',
                       job_id=job['id'], new_job=new_job)
        if launch:
            _launch(config_path, job)
        return {"job_id": job["id"], "workspace_id": wid,
                "startup_mode": startup_mode(job, config_path),
                **counts(job)}


def sqlite_home(config_path, job_id, index):
    # Keep CODEX_HOME, transcripts, hooks and credentials in their normal
    # locations. Only the native SQLite writers of this new CLI are isolated.
    if type(index) is not int or not 0 <= index < COUNT:
        raise RuntimeError("invalid batch SQLite index")
    return job_path(config_path, job_id).parent / "native-db" / "slots" / str(index)


def sqlite_seed_home(config_path, job_id):
    # Old native processes may still own native-db/*.sqlite. They retain their
    # exact argv and files; only future processes use the new per-slot copies.
    return job_path(config_path, job_id).parent / "native-db" / "template"


def _copy_seed(source, target):
    """APFS copy-on-write copy, never a shared writable inode."""
    if sys.platform == "darwin":
        clone = ctypes.CDLL(None, use_errno=True).clonefile
        clone.argtypes, clone.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
        if clone(os.fsencode(source), os.fsencode(target), 0) == 0:
            return
        code = ctypes.get_errno()
        if code not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL}:
            raise OSError(code, os.strerror(code), str(target))
    shutil.copyfile(source, target)


def prepare_slot_sqlite_home(config_path, job_id, index):
    prepare_sqlite_home(config_path, job_id)
    directory = sqlite_home(config_path, job_id, index)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = directory / "seed.json"
    if marker.exists():
        return directory
    with core.FileLock(directory / "seed.lock", timeout_sec=5):
        if marker.exists():
            return directory
        source = sqlite_seed_home(config_path, job_id)
        metadata = core.load_json(source / "seed.json", {}).get("metadata", [])
        for name in metadata:
            if not isinstance(name, str) or not re.fullmatch(r"(?:state|goals|memories|queue)_\d+\.sqlite", name):
                raise RuntimeError("invalid original native metadata seed")
            target = directory / name
            if target.exists():
                continue  # A native process may already have opened this copy.
            temporary = directory / ("." + name + ".seed")
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            try:
                _copy_seed(source / name, temporary)
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
        core.atomic_write_json(marker, {"at": time.time(), "metadata": metadata,
                                       "source": str(source), "layout": "per-slot-v1"})
    return directory


def working_directory(config_path, job_id, index):
    """A private per-slot root, separate from batch records and native databases."""
    if type(index) is not int or not 0 <= index < COUNT:
        raise RuntimeError("invalid batch workspace index")
    return job_path(Path(config_path).resolve(), job_id).parent / "work" / str(index)


def prepare_working_directory(config_path, job, index):
    if job.get("cwd_policy") is None:
        return None  # Original B does not create or clean the inherited cwd.
    if job["cwd_policy"] != EMPTY_CWD_POLICY:
        raise RuntimeError("unknown B working-directory policy")
    directory = working_directory(config_path, job["id"], index)
    if directory.parent.parent.is_symlink():
        raise RuntimeError("B private workspace parent is a symbolic link")
    for path in (directory.parent, directory):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise RuntimeError("B private workspace is not an owned private directory")
    if any(directory.iterdir()):
        raise RuntimeError("B private workspace is not empty; existing files were preserved")
    return directory


def workspace_launch_context(config_path, job, index):
    """The explicit B action grants this invocation access to its exact cwd."""
    if job.get("cwd_policy") is None:
        # Resolve inside the registered terminal, immediately before exec.
        # B inherits that terminal's directory; it must not stop at a folder
        # confirmation after the operator already authorized the batch.
        directory = Path.cwd().resolve(strict=True)
    elif job["cwd_policy"] == EMPTY_CWD_POLICY:
        directory = working_directory(config_path, job["id"], index)
    else:
        raise RuntimeError("unknown B working-directory policy")
    # CLI dotted keys do not parse quoted path components. Use a TOML inline
    # table instead; no trust entry is written into the user's config.toml.
    trust = "projects={" + json.dumps(str(directory), ensure_ascii=False) + '={trust_level="trusted"}}'
    return directory, ["-c", trust]


def native_trace_directory(config_path, job, index):
    """An opt-in log destination bound to the original job/slot/launch."""
    if 'native_trace_policy' not in job:
        return None
    if job['native_trace_policy'] != NATIVE_TRACE_POLICY or not argv_initial(job, config_path):
        raise RuntimeError('invalid native trace policy')
    if type(index) is not int or not 0 <= index < len(job['slots']):
        raise RuntimeError('invalid native trace slot')
    launch_id = job['slots'][index].get('launch_id')
    if not isinstance(launch_id, str) or str(uuid.UUID(launch_id)) != launch_id:
        raise RuntimeError('invalid native trace launch identity')
    root = job_path(Path(config_path).resolve(), job['id']).parent
    directory = root / 'native-trace' / str(index) / launch_id
    if directory.resolve() != directory:
        raise RuntimeError('native trace directory identity changed')
    return directory


def native_trace_identity(config_path, job, index, *, prepare=False):
    directory = native_trace_directory(config_path, job, index)
    if directory is None:
        return None
    identities = {}
    for path in (directory.parent.parent, directory.parent, directory):
        if prepare:
            path.mkdir(mode=0o700, exist_ok=True)
        value = path.lstat()
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) & 0o077):
            raise RuntimeError('native trace directory must be owned and private')
        identities[str(path)] = [value.st_dev, value.st_ino]
    if any(directory.iterdir()):
        raise RuntimeError('native trace directory is not empty before exec')
    return identities


def native_launch_argv(config_path, job, index, *, native_argv=None):
    """Resolve the exact native executable without putting long argv in a PTY."""
    mode = startup_mode(job, config_path)
    from ccc_batch_guard import AUTOMATIC_POOL_STOP, native_binary
    if job.get("guard_version") == 1 and AUTOMATIC_POOL_STOP:
        return [sys.executable, "-B", str(Path(__file__).with_name("ccc_batch_guard.py")),
                "launch", "--config", str(config_path), "--job", job["id"], "--index", str(index)]
    directory, context = workspace_launch_context(config_path, job, index)
    argv = [*(native_argv if native_argv is not None else [native_binary()]),
            *(["--cd", str(directory)] if directory else []), *context,
            "-c", "sqlite_home=" + json.dumps(str(sqlite_home(config_path, job["id"], index).resolve()))]
    trace_directory = native_trace_directory(config_path, job, index)
    if trace_directory is not None:
        argv.extend(['-c', 'log_dir=' + json.dumps(str(trace_directory))])
    if mode == 'access_check':
        from ccc_access_service import launch_arguments
        argv.extend(launch_arguments(config_path, job, index))
    if argv_initial(job, config_path):
        # Native's skill_search is a shadow ranking experiment, not skill
        # discovery or actual skill selection. Avoid running its catalog-wide
        # algorithms on every short-check turn in explicit new native jobs.
        argv.extend(["--disable", "skill_search"])
        hook = shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), "bind-initial",
                           "--config", str(config_path), "--job", job["id"],
                           "--index", str(index), "--launch-id", job["slots"][index]["launch_id"]])
        argv.extend(initial_hook_arguments(hook))
        argv.append(PROMPT)
    return argv


def initial_hook_arguments(hook):
    # Native 0.156's normalized hook identity uses sorted JSON via
    # version_for_toml; trust only this session-flags handler, not other hooks.
    normalized = {"event_name": "session_start", "matcher": "startup", "hooks": [
        {"type": "command", "command": hook, "timeout": 5, "async": False}]}
    trust = "sha256:" + hashlib.sha256(json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    key = "/<session-flags>/config.toml:session_start:0:0"
    return ["--enable", "hooks", "-c", 'hooks.SessionStart=[{matcher="startup",hooks=[{type="command",command='
            + json.dumps(hook) + ',timeout=5}]}]', "-c",
            "hooks.state={" + json.dumps(key) + '={enabled=true,trusted_hash=' + json.dumps(trust) + '}}']


def _claim_argv_initial(config_path, job_id, index, record):
    """Permanently consume one slot before any initial-prompt exec.

    A partial file is deliberately a consumed claim. Neither a different
    launch ID nor a process restart can turn uncertain execution into retry.
    This primitive does not authorize an exec or enable the new job protocol.
    """
    if type(index) is not int or not 0 <= index < COUNT:
        raise RuntimeError("invalid initial prompt slot")
    if (record.get("job_id") != job_id or record.get("index") != index
            or record.get("policy") != ARGV_INITIAL_POLICY):
        raise RuntimeError("initial prompt claim identity mismatch")
    path = job_path(config_path, job_id).parent / f"initial-argv-{index}.json"
    return _write_once_json(path, record)


def _write_once_json(path, record):
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    # No replace/rename: all competing bootstraps contend on the permanent
    # slot name, including a bootstrap with a newer launch_id.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        # Even an empty claim is evidence of uncertainty, never unlink it.
        raise
    return path, payload


def _initial_event_prefix(claim, *, require_turn=False, retain_partial=False):
    path = Path(claim["tui_log"])
    st = path.stat()
    if [st.st_dev, st.st_ino] != claim["tui_log_identity"]:
        raise RuntimeError("initial native event file identity changed")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if [opened.st_dev, opened.st_ino] != claim["tui_log_identity"]:
            raise RuntimeError("initial event file changed while opening")
        data = handle.read(2 * 1024 * 1024 + 1)
        after = os.fstat(handle.fileno())
    current = path.stat()
    if (after.st_size < opened.st_size or current.st_size < after.st_size
            or [current.st_dev, current.st_ino] != claim["tui_log_identity"]):
        raise RuntimeError("initial event file replaced or truncated")
    if len(data) > 2 * 1024 * 1024:
        raise RuntimeError("initial native event prefix exceeded bound")
    # A concurrent write may have emitted JSON but not its terminating newline.
    raw_prefix = data
    data = data[:data.rfind(b"\n") + 1]
    events = [json.loads(line) for line in data.splitlines()]
    if (not events or events[0].get("dir") != "meta" or events[0].get("kind") != "session_start"
            or Path(events[0]["cwd"]).resolve() != Path(claim["cwd"]).resolve()
            or epoch(events[0]["ts"]) < claim["at"] - .001):
        raise RuntimeError("initial native event header missing")
    switches = {"ResumeSessionByIdOrName", "ResumeSession", "ForkCurrentSession",
                "NewAgentsOverviewSession", "SelectAgentThread", "SelectAgentsOverviewThread",
                "ClearUiAndSubmitUserMessage"}
    starts = 0
    first_turn = False
    end = len(data.splitlines(keepends=True)[0])
    for event, line in zip(events[1:], data.splitlines(keepends=True)[1:]):
        end += len(line)
        if (event.get("kind") in {"new_session", "clear_ui", "session_end", "session_start"}
                or event.get("variant") in switches):
            raise RuntimeError("native session changed before the initial task")
        if event.get("variant") == "StartupThreadStarted":
            starts += 1
        if event.get("dir") == "from_tui" and event.get("kind") == "op":
            payload = event.get("payload")
            if (isinstance(payload, dict) and set(payload) == {"ListSkills"}
                    and payload["ListSkills"].get("cwds") == [claim["cwd"]]):
                continue  # Native startup lists skills before submitting argv.
            user = event.get("payload", {}).get("UserTurn") if isinstance(event.get("payload"), dict) else None
            if (not user or len(user.get("items", [])) != 1
                    or user["items"][0].get("type") != "text"
                    or user["items"][0].get("text") != PROMPT
                    or starts != 1 or first_turn):
                raise RuntimeError("native first outbound task is not the fixed initial task")
            first_turn = True
            if require_turn:
                return data, True
    if starts > 1:
        raise RuntimeError("multiple native startup events before hook binding")
    return (raw_prefix if retain_partial else data), first_turn


def bind_initial_session(config_path, job_id, index, launch_id, payload):
    """Synchronous native startup hook; later clear/resume can never replace it."""
    from ccc_guard_scope import process, birth
    jobfile = job_path(config_path, job_id)
    job = core.load_json(jobfile, {})
    if not argv_initial(job, config_path):
        raise RuntimeError("initial hook requires explicit argv protocol")
    raw = (jobfile.parent / f"initial-argv-{index}.json").read_bytes()
    claim = json.loads(raw)
    if (claim.get("launch_id") != launch_id or claim.get("job_id") != job_id
            or claim.get("index") != index or claim.get("state") != "exec_intent"
            or payload.get("hook_event_name") != "SessionStart" or payload.get("source") != "startup"):
        raise RuntimeError("not the original native startup hook")
    # Consume even a failed first callback. A later /new must not repair an
    # identity failure by presenting another session with source=startup.
    _write_once_json(jobfile.parent / f"initial-hook-attempt-{index}.json", {
        "claim_sha256": hashlib.sha256(raw).hexdigest(), "at": time.time()})
    session = str(uuid.UUID(payload["session_id"]))
    native = process(claim["bootstrap_pid"], launch=True)
    if (not native or native["birth"] != claim["bootstrap_birth"]
            or native["surface_id"].upper() != claim["surface_id"].upper()
            or native["environment_workspace_id"].upper() != claim["workspace_id"].upper()
            or native["argv"] != claim["argv"]
            or Path(payload["cwd"]).resolve() != Path(claim["cwd"]).resolve()):
        raise RuntimeError("initial hook original native identity changed")
    from ccc_codex_queue import process_writable_files
    tui = Path(claim["tui_log"])
    st = tui.stat()
    fd = process_writable_files(native["pid"], identities=True).get(tui.resolve(), {})
    if ([st.st_dev, st.st_ino] != claim["tui_log_identity"]
            or [fd.get("device"), fd.get("inode")] != claim["tui_log_identity"]):
        raise RuntimeError("initial hook original native event writer missing")
    data, _ = _initial_event_prefix(claim)
    root = (Path(native["environment"].get("CODEX_HOME") or Path.home() / ".codex") / "sessions").resolve()
    transcript = payload.get("transcript_path")
    if transcript:
        transcript = str(Path(transcript).resolve())
        if not Path(transcript).is_relative_to(root):
            raise RuntimeError("initial hook transcript outside original sessions")
    record = {"claim_sha256": hashlib.sha256(raw).hexdigest(), "session_id": session,
              "pid": native["pid"], "birth": native["birth"], "surface_id": claim["surface_id"],
              "workspace_id": claim["workspace_id"], "sessions_root": str(root),
              "transcript": transcript, "source": "startup", "at": time.time()}
    record.update(tui_prefix_bytes=len(data), tui_prefix_sha256=hashlib.sha256(data).hexdigest())
    if (birth(native["pid"], codex=True) != native["birth"]
            or (jobfile.parent / f"initial-argv-{index}.json").read_bytes() != raw):
        raise RuntimeError("initial hook identity changed before persistence")
    _write_once_json(jobfile.parent / f"initial-session-{index}.json", record)


def _exec_claimed_argv(config_path, job_id, index, record, argv, final_guard, final_authorized=None):
    """Consume the slot, then validate after persistence and just before exec.

    final_guard owns live authorization, original bootstrap identity, current
    main-area membership and the bound receipt/cwd/argv evidence. An exec error
    leaves the same claim consumed. Callers must only observe it thereafter.
    """
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(value, str) or "\0" in value for value in argv)
            or not Path(argv[0]).is_absolute() or record.get("argv") != argv):
        raise RuntimeError("initial prompt argv identity mismatch")
    argv = list(argv)  # Caller/guard mutation cannot alter the consumed payload.
    path, payload = _claim_argv_initial(config_path, job_id, index, record)
    generation = _file_generation(path)
    if not final_guard():
        raise RuntimeError("initial prompt authorization changed before exec")
    # The final guard may perform I/O. Reject a replaced or truncated claim
    # after it returns without granting another attempt.
    if (path.is_symlink() or _file_generation(path) != generation
            or path.read_bytes() != payload or _file_generation(path) != generation):
        raise RuntimeError("initial prompt claim changed before exec")
    if not (final_authorized or final_guard)():
        raise RuntimeError("initial prompt authorization changed at exec boundary")
    os.execv(argv[0], argv)


def native_thread_name(target, native):
    """Read only this live process's original native name index."""
    from ccc_guard_scope import process, birth
    record = process(native.get("pid"), launch=True)
    if (not record or record.get("remote")
            or record["process_start"] != native.get("process_start")
            or record["surface_id"] != str(target["surface_id"]).upper()
            or record["environment_workspace_id"] != str(target["workspace_id"]).upper()):
        return None
    env = record["environment"]
    root = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
    if not root.is_absolute():
        return None
    path = root / "session_index.jsonl"
    name = ""
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            offset = max(0, before.st_size - 1024 * 1024)
            handle.seek(offset)
            data = handle.read(1024 * 1024)
            after = os.fstat(handle.fileno())
        current = path.stat()
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if identity(before) != identity(after) or identity(before) != identity(current):
            return None
        if data and not data.endswith(b"\n"):
            return None
        if offset:
            data = data.partition(b"\n")[2]
        for line in reversed(data.splitlines()):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                return None
            if isinstance(value, dict) and value.get("id") == native.get("session_id"):
                if not isinstance(value.get("thread_name"), str):
                    return None
                name = value["thread_name"]
                break
    except FileNotFoundError:
        pass
    except OSError:
        return None
    if birth(native["pid"], codex=True) != record["birth"]:
        return None
    return {"name": name, "birth": record["birth"]}


def launch_registered(config_path, job_id, index, launch_id):
    if 'standby_policy' in core.load_json(job_path(config_path, job_id), {}):
        raise RuntimeError('standby requires its own guarded no-prompt launcher')
    register(config_path, job_id, index, launch_id)
    job = core.load_json(job_path(config_path, job_id), {})
    if (job["slots"][index].get("launch_id") != launch_id
            or not allowed(core.ConfigStore(Path(config_path)).load(), job)):
        raise RuntimeError("batch authorization changed before native launch")
    # exec retains the registered shell parent and original terminal. There is
    # no relay, alternate session, shell expansion, or global config write.
    argv = native_launch_argv(config_path, job, index)
    if argv_initial(job, config_path):
        return _launch_initial_registered(config_path, job, index, launch_id, argv)
    if startup_mode(job, config_path) == 'access_check':
        for name in ('NO_PROXY', 'no_proxy'):
            os.environ[name] = ','.join(filter(None, (os.environ.get(name), '127.0.0.1', 'localhost')))
    os.execv(argv[0], argv)


def native_launch_environment(job):
    policy = job.get("native_runtime_policy")
    if policy is None:
        return {}  # Historical jobs keep their original runtime environment.
    if policy != NATIVE_RUNTIME_POLICY or job.get("initial_prompt_policy") != ARGV_INITIAL_POLICY:
        raise RuntimeError("unknown native runtime policy")
    return {"TOKIO_WORKER_THREADS": "2"}


def _launch_initial_registered(config_path, job, index, launch_id, argv):
    """The original registered bootstrap becomes the initial native writer."""
    from ccc_guard_scope import birth
    requested_environment = native_launch_environment(job)
    os.environ.update(requested_environment)
    path = job_path(config_path, job["id"])
    receipt_path = path.parent / f"surface-{index}.json"
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    receipt_generation = _file_generation(receipt_path)
    sid, wid = os.environ.get("CMUX_SURFACE_ID"), os.environ.get("CMUX_WORKSPACE_ID")
    if (receipt.get("surface_id") != sid or receipt.get("workspace_id") != wid
            or wid != job["workspace_id"] or receipt.get("launch_id") != launch_id):
        raise RuntimeError("initial prompt registration identity changed")
    pid = os.getpid()
    identity = birth(pid)
    if not identity:
        raise RuntimeError("initial prompt bootstrap birth unavailable")
    cwd = working_directory(config_path, job["id"], index)
    cwd_generation = _file_generation(cwd)
    executable = Path(argv[0]).resolve(strict=True)
    executable_generation = _file_generation(executable)
    trace_directory = native_trace_directory(config_path, job, index)
    trace_identity = None
    trace_argv = None
    if trace_directory is not None:
        trace_argv = tuple(native_launch_argv(config_path, job, index))
        if tuple(argv) != trace_argv:
            raise RuntimeError('native trace argv differs from original launch policy')
        trace_identity = native_trace_identity(config_path, job, index, prepare=True)
    client = _bootstrap_client(core.ConfigStore(Path(config_path)).load(), job)
    tui_log = path.parent / f"initial-native-events-{index}.jsonl"
    with os.fdopen(os.open(tui_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        os.fsync(handle.fileno())
    tui_stat = tui_log.stat()
    os.environ["CODEX_TUI_RECORD_SESSION"] = "1"
    os.environ["CODEX_TUI_SESSION_LOG_PATH"] = str(tui_log)
    record = {"policy": ARGV_INITIAL_POLICY, "job_id": job["id"], "index": index,
              "workspace_id": wid, "surface_id": sid, "launch_id": launch_id,
              "bootstrap_pid": pid, "bootstrap_birth": identity,
              "receipt_sha256": hashlib.sha256(raw).hexdigest(),
              "cwd": str(cwd), "cwd_generation": list(cwd_generation),
              "argv": argv, "executable": str(executable),
              "executable_generation": list(executable_generation),
              "at": time.time(), "state": "exec_intent"}
    record.update(tui_log=str(tui_log), tui_log_identity=[tui_stat.st_dev, tui_stat.st_ino])
    # Native dotenv can override this request. This records exec input, not a
    # claim about the eventual number of native or blocking-pool threads.
    record["requested_environment"] = requested_environment
    if trace_directory is not None:
        record.update(native_trace_policy=NATIVE_TRACE_POLICY,
                      native_trace_directory=str(trace_directory), native_trace_identity=trace_identity)

    def permission_current():
        if (receipt_path.is_symlink() or _file_generation(receipt_path) != receipt_generation
                or cwd.is_symlink() or _file_generation(cwd) != cwd_generation
                or Path(argv[0]).resolve(strict=True) != executable
                or _file_generation(executable) != executable_generation):
            return False
        current = core.load_json(path, {})
        if current.get('native_trace_policy') != job.get('native_trace_policy'):
            return False
        if trace_directory is not None:
            try:
                if native_trace_directory(config_path, current, index) != trace_directory:
                    return False
                if tuple(native_launch_argv(config_path, current, index)) != trace_argv:
                    return False
                if native_trace_identity(config_path, current, index) != trace_identity:
                    return False
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                return False
        if (native_launch_environment(current) != requested_environment
                or any(os.environ.get(key) != value for key, value in requested_environment.items())):
            return False
        if (not argv_initial(current, config_path) or current.get("workspace_id") != wid
                or current["slots"][index].get("launch_id") != launch_id
                or current["slots"][index].get("surface_id") not in {None, sid}):
            return False
        config = core.ConfigStore(Path(config_path)).load()
        if job.get("bootstrap_endpoint") is not None and not _bootstrap_endpoint_current(job["bootstrap_endpoint"], config):
            return False
        rule = core.workspace_rule_by_id(config, wid)
        hold = core.batch_start_hold(rule, sid)
        return (allowed(config, current) and hold.get("job_id") == job["id"]
                and hold.get("index") == index
                and not (sid in rule.get("excluded_surface_ids", [])
                         and rule.get("excluded_surface_reasons", {}).get(sid)
                         != f"batch:{job['id']}:initial")
                and not any(t.get("surface_id") == sid
                            and (t.get("paused") or not t.get("enabled", True))
                            for t in config["targets"]) and birth(pid) == identity)

    def authorized():
        current = core.load_json(path, {})
        if (not argv_initial(current, config_path) or current.get("workspace_id") != wid
                or current["slots"][index].get("launch_id") != launch_id
                or current["slots"][index].get("surface_id") not in {None, sid}
                or receipt_path.read_bytes() != raw
                or birth(pid) != identity
                or Path(argv[0]).resolve(strict=True) != executable
                or _file_generation(executable) != executable_generation
                or _file_generation(cwd) != cwd_generation or any(cwd.iterdir())):
            return False
        tree = client.workspace_tree(wid)
        target = core.find_main_surface(tree, sid)
        if target.get("workspace_id") != wid:
            return False
        return permission_current()

    with core.workspace_input_lock(config_path, wid, shared=True):
        _exec_claimed_argv(config_path, job["id"], index, record, argv, authorized, permission_current)


def prepare_sqlite_home(config_path, job_id):
    """Seed only small native metadata, never the multi-GB logs/history DBs.

    An empty state DB makes Codex synchronously reindex every old rollout.
    SQLite backup preserves the real completed backfill and selected rollouts;
    no native status is fabricated. Each native gets its own writable copy.
    """
    directory = sqlite_seed_home(config_path, job_id)
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


class _RegistrationUnchanged(Exception):
    pass


def _lock_busy(exc):
    return (isinstance(exc, RuntimeError) and isinstance(exc.__cause__, OSError)
            and exc.__cause__.errno in {errno.EACCES, errno.EAGAIN})


def _registration_outcome(directory, request):
    receipt = core.load_json(directory / f"surface-{request['index']}.json", {})
    if all(receipt.get(key) == request.get(key) for key in
           ("request_id", "surface_id", "workspace_id", "launch_id")):
        return receipt
    result = core.load_json(directory / f"registration-result-{request['index']}.json", {})
    if result.get("request_id") == request["request_id"]:
        raise RuntimeError(result.get("error") or "batch registration was rejected")
    return None


def _publish_registration(config_path, request):
    """Keep the original request and publish one short-lived readiness entry."""
    directory = job_path(config_path, request['job_id']).parent
    source = directory / f"registration-{request['index']}.json"
    core.atomic_write_json(source, request)
    ready = Path(config_path).parent / 'batch-registration-ready'
    ready.mkdir(mode=0o700, exist_ok=True)
    info = ready.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError('batch registration queue is not an owned directory')
    core.atomic_write_json(ready / f"{request['request_id']}.json", {
        'job_id': request['job_id'], 'index': request['index'], 'request_id': request['request_id'],
        'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})


def _commit_registration_requests(config_path, job_id):
    """One bootstrap commits all already-ready requests across active batches.

    The queue contains pending bootstrap requests, never historical jobs. No
    cohort wait is added. Each native awaits its own durable hold/receipt and
    repeats current authorization before exec.
    """
    path = job_path(config_path, job_id)
    store = core.ConfigStore(Path(config_path))
    try:
        with core.FileLock(Path(config_path).parent / "batch-registration.lock", timeout_sec=0,
                           purpose="batch registration group"):
            requests = []
            ready = Path(config_path).parent / 'batch-registration-ready'
            for marker in ready.glob('*.json'):
                try:
                    marker_data = marker.read_bytes()
                    entry = json.loads(marker_data)
                    uuid.UUID(entry['job_id'])
                    uuid.UUID(entry['request_id'])
                    index = entry['index']
                    if (type(index) is not int or not 0 <= index < COUNT
                            or marker.stem != entry['request_id']):
                        raise ValueError('invalid registration queue identity')
                    source = job_path(config_path, entry['job_id']).parent / f'registration-{index}.json'
                    data = source.read_bytes()
                    request = json.loads(data)
                    if (hashlib.sha256(data).hexdigest() != entry['sha256']
                            or any(request.get(key) != entry[key] for key in ('job_id', 'index', 'request_id'))):
                        marker.rename(marker.with_suffix('.stale'))
                        continue
                except FileNotFoundError:
                    continue
                except (ValueError, KeyError, TypeError, AttributeError):
                    marker.rename(marker.with_suffix('.invalid'))
                    continue
                try:
                    if _registration_outcome(source.parent, request):
                        marker.unlink()
                        continue
                except RuntimeError:
                    marker.unlink()
                    continue  # This exact request already has a durable veto.
                requests.append((marker, marker_data, source, data, request))
            if not requests:
                return True
            accepted, rejected = [], []
            def protect(latest):
                # Atomic job replacements may happen while other slots reserve
                # their launch IDs. Bind every request to the current slot.
                jobs, job_errors = {}, {}
                changed = False
                for marker, marker_data, source, data, request in requests:
                    job_id = request['job_id']
                    try:
                        index, sid = request["index"], request["surface_id"]
                        uuid.UUID(sid)
                        if type(request.get('requested_at')) not in (int, float):
                            raise ValueError('invalid registration time')
                        if job_id in job_errors:
                            raise RuntimeError(job_errors[job_id])
                        if job_id not in jobs:
                            try:
                                job = core.load_json(job_path(config_path, job_id), {})
                                mode = startup_mode(job, config_path)
                                if job.get('id') != job_id or not allowed(latest, job):
                                    raise RuntimeError('batch paused or cancelled before launch')
                                rule = core.workspace_rule_by_id(latest, job['workspace_id'])
                            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                                job_errors[job_id] = str(exc)
                                raise
                            jobs[job_id] = job, mode, rule
                        job, rule_mode, rule = jobs[job_id]
                        if (request["workspace_id"] != job["workspace_id"]
                                or index >= len(job["slots"])
                                or job["slots"][index].get("launch_id", "") != request["launch_id"]):
                            raise RuntimeError("stale batch launch")
                        if job["slots"][index].get("surface_id") not in (None, sid):
                            raise RuntimeError("batch slot already has its original surface")
                        old = core.load_json(source.parent / f"surface-{index}.json", {})
                        if old and old.get("surface_id") != sid:
                            raise RuntimeError("batch slot already belongs to another surface")
                        previous = core.batch_start_hold(rule, sid)
                        if sid in rule.get("excluded_surface_ids", []) and not (previous or {}).get("legacy"):
                            raise RuntimeError("new surface was excluded by its operator")
                        if previous and previous.get("job_id") != job_id:
                            raise RuntimeError("surface already belongs to another batch")
                        if previous and not previous.get("legacy") and previous.get("index") != index:
                            raise RuntimeError("surface already belongs to another batch slot")
                        if any(other != sid and isinstance(hold, dict)
                               and hold.get("job_id") == job_id and hold.get("index") == index
                               for other, hold in rule.get("batch_start_holds", {}).items()):
                            raise RuntimeError("batch slot already protects its original surface")
                        if rule_mode == "access_check":
                            record = {"job_id": job_id, "index": index}
                            slots = rule.setdefault("access_check_slots", {})
                            if slots.get(sid) != record:
                                slots[sid] = record
                                changed = True
                        if job["slots"][index].get("phase") != "confirmed" and not previous:
                            rule.setdefault("batch_start_holds", {})[sid] = {
                                "job_id": job_id, "index": index, "created_at": request["requested_at"]}
                            changed = True
                        accepted.append((marker, marker_data, source, data, request))
                    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                        rejected.append((marker, marker_data, source, data, request, str(exc)))
                if not changed:
                    # Skip a full config rewrite when a late bootstrap already
                    # has its exact hold or all requests were vetoed.
                    raise _RegistrationUnchanged()
            try:
                store.mutate(protect)
            except _RegistrationUnchanged:
                pass
            # No receipt is visible before the entire config commit succeeds.
            # A subsequent attempt reconciles an interrupted receipt write.
            for marker, marker_data, source, data, request in accepted:
                if source.read_bytes() != data:
                    continue
                core.atomic_write_json(source.parent / f"surface-{request['index']}.json",
                    {**request, "registered_at": time.time(), "registration_policy": "coalesced-v1"})
                if marker.read_bytes() == marker_data:
                    marker.unlink()
            for marker, marker_data, source, data, request, error in rejected:
                if source.read_bytes() == data:
                    core.atomic_write_json(source.parent / f"registration-result-{request['index']}.json",
                        {"request_id": request["request_id"], "error": error, "at": time.time()})
                    if marker.read_bytes() == marker_data:
                        marker.unlink()
            return True
    except RuntimeError as exc:
        if _lock_busy(exc):
            return False
        raise


def register(config_path, job_id, index, launch_id=""):
    """Runs in the newly created shell before Codex starts (without a prompt)."""
    path = job_path(config_path, job_id)
    job = core.load_json(path, {})
    startup_mode(job, config_path)
    if not 0 <= index < len(job["slots"]):
        raise RuntimeError("invalid batch slot")
    if (job["slots"][index].get("launch_id") or "") != launch_id:
        raise RuntimeError("stale batch launch")
    sid, wid = os.environ.get("CMUX_SURFACE_ID", ""), os.environ.get("CMUX_WORKSPACE_ID", "")
    uuid.UUID(sid)
    if wid != job["workspace_id"]:
        raise RuntimeError("new surface workspace does not match its batch")
    receipt = path.parent / f"surface-{index}.json"
    with core.workspace_input_lock(config_path, wid, shared=True):
        with core.FileLock(receipt.with_suffix(".lock"), timeout_sec=5):
            job = core.load_json(path, {})
            if (job["slots"][index].get("launch_id") or "") != launch_id:
                raise RuntimeError("stale batch launch")
            old = core.load_json(receipt, {})
            if old and old.get("surface_id") != sid:
                raise RuntimeError("batch slot already belongs to another surface")
            prepare_slot_sqlite_home(config_path, job_id, index)
            directory = prepare_working_directory(config_path, job, index)
            request = {"job_id": job_id, "index": index, "request_id": str(uuid.uuid4()),
                       "surface_id": sid, "workspace_id": wid, "launch_id": launch_id,
                       "requested_at": time.time(), "shell_pid": os.getppid(),
                       "shell_start": batch_shell_identity(os.getppid()),
                       **({"working_directory": str(directory)} if directory else {})}
            _publish_registration(config_path, request)
            next_check = time.monotonic() + .25
            while not _registration_outcome(path.parent, request):
                _commit_registration_requests(config_path, job_id)
                if _registration_outcome(path.parent, request):
                    break
                if time.monotonic() >= next_check:
                    current = core.load_json(path, {})
                    if (not allowed(core.ConfigStore(Path(config_path)).load(), current)
                            or current["slots"][index].get("launch_id", "") != launch_id):
                        raise RuntimeError("batch authorization changed before native launch")
                    next_check = time.monotonic() + .25
                time.sleep(.005)


class _JobCommitter:
    """Commit a group of immutable job snapshots before releasing its senders.

    Snapshots are submitted under the worker state lock, so the last snapshot
    includes every earlier slot mutation. The writer never acquires that lock.
    No new receipt format or asynchronous permission to create/send is added.
    """
    def __init__(self, path):
        self.path = path
        self.condition = threading.Condition()
        self.pending = []
        self.closed = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name="ccc-batch-commit", daemon=True)
        self.thread.start()

    def submit(self, snapshot):
        with self.condition:
            if self.closed or self.error is not None:
                raise RuntimeError("batch persistence is unavailable") from self.error
            future = Future()
            self.pending.append((snapshot, future))
            self.condition.notify()
            return future

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.closed)
                if not self.pending:
                    return
                end = time.monotonic() + .002
                while not self.closed and time.monotonic() < end:
                    self.condition.wait(max(0, end - time.monotonic()))
                batch, self.pending = self.pending, []
            try:
                core.atomic_write_json(self.path, batch[-1][0])
            except BaseException as exc:
                with self.condition:
                    self.error, self.closed = exc, True
                    batch.extend(self.pending)
                    self.pending = []
                for _, future in batch:
                    if not future.done():
                        future.set_exception(exc)
                return
            for _, future in batch:
                if not future.done():
                    future.set_result(None)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.thread.join()


class BatchWorker:
    def __init__(self, config_path, job_id, *, client=None, queue=None, clock=time.time, pty_probe=None):
        self.config_path = Path(config_path)
        self.path = job_path(config_path, job_id)
        self.store = core.ConfigStore(self.config_path)
        self.job = core.load_json(self.path, {})
        if 'standby_policy' in self.job:
            raise RuntimeError('standby requires its own activation manager')
        startup_mode(self.job, self.config_path)
        self.cache = SnapshotCache(workers=1)
        self.client = client or SnapshotClient(_client(self.store.load()), self.cache,
                                               SharedInventory(self.config_path.parent))
        # Reconciliation adapters share their client's fleet snapshot. Retaining
        # a separate parsed top for each historical job multiplied memory use.
        self.inventory = (self.client.shared if isinstance(self.client, SnapshotClient)
                          and self.client.shared is not None else SharedInventory(self.config_path.parent))
        self.queue = queue or QueueRecovery(self.path.parent / "unused-queue-ledger.json",
            Path.home() / ".cmuxterm/codex-hook-sessions.json", Path.home() / ".codex/sessions", job_prompt(self.job))
        self.processes = {}
        self.queue.process_lookup = self._process_label
        self.clock = clock
        self.pty_probe = pty_probe or pty_available
        self.name_lookup = native_thread_name
        self._top_due = 0.0
        self._saved = self._serialized_job()
        self._job_committer = None
        self._queued_serialized = self._saved
        self._queued_commit = None
        self._shell_hints = {}
        self._wait_observed = False
        self._state_lock = threading.RLock()
        self._slot_parent = None
        self._slot_index = None
        self._slot_pool = None
        self._inflight = {}
        self._slot_due = {}
        self._release_pending = set()
        self._release_pool = None
        self._release_future = None
        self._release_retry_at = 0
        self._wakeup = threading.Event()
        self._closed = False
        self._close_done = False
        self._config_read_lock = threading.RLock()
        self._config_snapshot = None

    def _configuration(self):
        owner = self._slot_parent or self
        with owner._config_read_lock:
            before = _file_generation(owner.config_path)
            cached = owner._config_snapshot
            if cached is not None and cached[0] == before and _file_generation(owner.config_path) == before:
                return cached[1]
            value = owner.store.load()
            if _file_generation(owner.config_path) != before:
                raise RuntimeError("批次授权正在更新；等待原文件代际稳定")
            owner._config_snapshot = (before, value)
            return value

    def _wait(self, reason, message):
        previous = self.job.get("preparation_wait", {})
        previous = previous if isinstance(previous, dict) else {}
        since = previous.get("since", self.clock()) if previous.get("reason") == reason else self.clock()
        self.job.update(status="waiting", error=message,
                        preparation_wait={"reason": reason, "message": message, "since": since})
        self._wait_observed = True

    def _clear_wait(self):
        self.job.pop("preparation_wait", None)
        self.job.pop("error", None)

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

    def _serialized_job(self):
        value = {k: v for k, v in self.job.items() if k != "updated_at"}
        return json.dumps(value, sort_keys=True)

    def save(self):
        if self._slot_parent is not None:
            parent = self._slot_parent
            # Each task owns a private slot snapshot. Only its slot is merged;
            # an older view cannot overwrite another slot's durable intent.
            with parent._state_lock:
                current = parent.job["slots"][self._slot_index]
                snapshot = self.job["slots"][self._slot_index]
                queued = getattr(parent, "_queued_job_snapshot", None)
                unchanged = (parent._queued_commit is not None
                             and queued is not None
                             and queued["slots"][self._slot_index] == snapshot
                             and current == snapshot and not self._wait_observed
                             and all(key not in self.job or parent.job.get(key) == self.job[key]
                                     and queued.get(key) == self.job[key]
                                     for key in ("window_id", "pane_id")))
                if unchanged:
                    # Repeated startup polls must still await an outstanding
                    # commit, but need not serialize the whole job again.
                    commit = parent._queued_commit
                else:
                    current.clear()
                    current.update(copy.deepcopy(snapshot))
                    for key in ("window_id", "pane_id"):
                        if key in self.job:
                            parent.job[key] = self.job[key]
                    if self._wait_observed:
                        parent._wait_observed = True
                        parent.job["preparation_wait"] = copy.deepcopy(self.job["preparation_wait"])
                        parent.job["error"] = self.job["error"]
                    commit = parent._queue_job_commit()
            # Other slots can publish their own intents into this commit while
            # this slot waits. No slot reaches I/O before its snapshot is saved.
            parent._wait_job_commit(commit)
            return
        with self._state_lock:
            if self._job_committer is not None:
                commit = self._queue_job_commit()
            else:
                serialized = self._serialized_job()
                if serialized == self._saved:
                    return
                self.job["updated_at"] = self.clock()
                core.atomic_write_json(self.path, self.job)
                self._saved = self._queued_serialized = serialized
                return
        self._wait_job_commit(commit)

    def _queue_job_commit(self):
        """Called under the original worker's state lock, in mutation order."""
        serialized = self._serialized_job()
        if serialized == self._queued_serialized:
            return self._queued_commit
        if self._job_committer is None:
            self._job_committer = _JobCommitter(self.path)
        self.job["updated_at"] = self.clock()
        snapshot = copy.deepcopy(self.job)
        commit = self._job_committer.submit(snapshot)
        self._queued_job_snapshot = snapshot
        self._queued_serialized, self._queued_commit = serialized, commit
        return commit

    def _wait_job_commit(self, commit):
        if commit is None:
            return
        commit.result()
        with self._state_lock:
            if commit is self._queued_commit:
                self._saved = self._queued_serialized

    def _slot_action(self, index, *, confirmation_only=False):
        with self._state_lock:
            view = copy.copy(self)
            view.job = dict(self.job)
            # Other slots are read only for the legacy N cohort gate. Do not
            # duplicate fifty growing transcript cursors for each slot poll.
            view.job["slots"] = [{key: slot.get(key) for key in
                                   ("index", "phase", "surface_id", "access_ready_at")}
                                  for slot in self.job["slots"]]
            view.job["slots"][index] = copy.deepcopy(self.job["slots"][index])
            view._slot_parent, view._slot_index = self, index
            view._wait_observed = False
        slot = view.job["slots"][index]
        previous_phase = slot["phase"]
        try:
            if slot["phase"] == "pending" and not confirmation_only:
                view._create(slot)
            else:
                view._advance(slot, confirmation_only=confirmation_only)
            if slot["phase"] == "creating":
                slot.update(phase="create_unknown", error="等待原创建回执；不会重复创建")
        except (OSError, ValueError, core.CmuxError, RuntimeError) as exc:
            slot["error"] = str(exc)
            if previous_phase == "pending":
                view._wait("create_preflight", "等待启动前核验：" + str(exc))
        finally:
            # A native/menu wait affects only this original slot. It never
            # withholds launch capacity or schedules the other 49 behind it.
            delay = 0 if slot["phase"] != previous_phase else STARTUP_POLL_SEC
            view.save()
            with self._state_lock:
                self._slot_due[index] = view.clock() + delay

    def _dispatch_slots(self, indices, *, confirmation_only=False):
        if self._closed:
            return
        if self._slot_pool is None:
            self._slot_pool = ThreadPoolExecutor(COUNT, thread_name_prefix="ccc-batch-slot")
        for index in indices:
            if index in self._inflight:
                continue
            future = self._slot_pool.submit(self._slot_action, index, confirmation_only=confirmation_only)
            self._inflight[index] = future
            future.add_done_callback(lambda _: self._wakeup.set())

    def _collect_slots(self):
        for index, future in list(self._inflight.items()):
            if not future.done():
                continue
            # A failed durable write must not be treated as a completed
            # dispatch. Propagate it, preserving the original disk receipt.
            future.result()
            self._inflight.pop(index)
        self._flush_releases()

    def _flush_releases(self, *, schedule=True):
        future = self._release_future
        if future is not None:
            if not future.done():
                return
            self._release_future = None
            try:
                slots = future.result()
            except (OSError, ValueError, RuntimeError) as exc:
                # A config lock timeout is known unsent input. Keep every hold
                # and pending release; other slots continue while this retries.
                self._release_retry_at = self.clock() + STARTUP_POLL_SEC
                with self._state_lock:
                    self._wait("hold_release", "等待原启动保护移交：" + str(exc))
                    self.save()
                return
            with self._state_lock:
                for saved in slots:
                    slot = self.job["slots"][saved["index"]]
                    if all(slot.get(key) == saved.get(key) for key in ("surface_id", "session_id", "launch_id")):
                        slot["hold_released_at"] = saved["hold_released_at"]
                        self._release_pending.discard(saved["index"])
                self.save()
        if not schedule or self.clock() < self._release_retry_at:
            return
        with self._state_lock:
            ready = [index for index in self._release_pending if index not in self._inflight]
            slots = [copy.deepcopy(self.job["slots"][index]) for index in ready]
            slots = [s for s in slots if s.get("phase") == "confirmed" and s.get("confirmation", {}).get("confirmed")]
        if not slots:
            return
        # A different pool's config writer must not occupy the slot dispatcher.
        # Exactly one coalesced release writer runs per original worker.
        if self._release_pool is None:
            self._release_pool = ThreadPoolExecutor(1, thread_name_prefix="ccc-batch-release")
        def release():
            self._release_many(slots)
            return slots
        self._release_future = self._release_pool.submit(release)
        self._release_future.add_done_callback(lambda _: self._wakeup.set())
        if self._release_future.done():
            self._flush_releases(schedule=False)

    def close(self):
        if self._close_done:
            return
        self._closed = True
        try:
            if self._slot_pool is not None:
                self._slot_pool.shutdown(wait=True, cancel_futures=False)
                self._collect_slots()
        finally:
            try:
                if self._release_pool is not None:
                    self._release_pool.shutdown(wait=True, cancel_futures=False)
                    self._flush_releases(schedule=False)
            finally:
                try:
                    if self._job_committer is not None:
                        self._job_committer.close()
                finally:
                    self.cache.close()
                    self._close_done = True

    def _stopping(self):
        return self._closed or (self._slot_parent is not None and self._slot_parent._closed)

    def _target(self, slot, *, fresh=False):
        tree = (self.client.fresh_tree() if fresh and isinstance(self.client, SnapshotClient)
                else self.client.tree())
        target = core.find_main_surface(tree, slot["surface_id"])
        if target["workspace_id"] != self.job["workspace_id"]:
            raise RuntimeError("surface moved out of its authorized workspace")
        return target

    def _create(self, slot):
        if argv_initial(self.job, self.config_path) and os.path.lexists(
                self.path.parent / f"initial-argv-{slot['index']}.json"):
            slot.update(phase="uncertain", error="首发启动已有永久记录；仅核对原会话，不重建")
            return
        if self._stopping() or slot.get("phase") != "pending" or self.clock() < slot.get("retry_at", 0):
            return
        wid = self.job["workspace_id"]
        with core.workspace_input_lock(self.config_path, wid, shared=True):
            if self._stopping() or not allowed(self._configuration(), self.job):
                return
            if not self.pty_probe():
                self._wait("pty", "系统 PTY 名额已满；保留剩余名额，空位释放后自动继续")
                return
            prepare_slot_sqlite_home(self.config_path, self.job["id"], slot["index"])
            prepare_working_directory(self.config_path, self.job, slot["index"])
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
            if self._stopping() or not allowed(self._configuration(), self.job):
                # The reservation is durable, but the RPC has not been
                # entered. Cancellation must not create a fresh terminal.
                slot.update(phase="pending", create_not_sent_at=self.clock())
                self.save()
                return
            try:
                slot["create_dispatched_at"] = self.clock()
                shell_options = {"clean_shell": True} if argv_initial(self.job, self.config_path) else {}
                base = self.client.client if isinstance(self.client, SnapshotClient) else self.client
                guard = (base.input_guard(lambda: not self._stopping() and allowed(self._configuration(), self.job))
                         if isinstance(base, core.CmuxClient) else contextlib.nullcontext())
                with guard:
                    slot["surface_id"] = self.client.new_codex_surface(
                        self.job["window_id"], wid, self.job["pane_id"], command, **shell_options)
                slot["phase"] = "created"
                slot["create_acknowledged_at"] = self.clock()
                # register() installs the hold before exec. Writing the same
                # full config again for each create acknowledgement serialized
                # all 50 launches and could race bootstrap's own protection.
            except core.InputNotSentError as exc:
                slot.update(phase="pending", create_not_sent_at=self.clock(), error=str(exc))
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
        # cmux submits this while the new shell may still be starting. A long
        # command can overflow its canonical input line and never execute.
        # Construct cwd/trust/database argv inside this short owned bootstrap.
        return shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), "register", "--launch-native",
                                "--config", str(self.config_path), "--job", self.job["id"],
                                "--index", str(slot["index"]), "--launch-id", slot["launch_id"]])

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
        if argv_initial(self.job, self.config_path):
            return  # This protocol never respawns or supplements an argv task.
        # Only pre-session startup failures owned by this batch may be
        # relaunched, in the same terminal. Never replace an existing session.
        if self.clock() < slot.get("retry_at", 0):
            return
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self._configuration()
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
            grid = core.Grid.from_rpc(self._replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            guard_refusal = False if no_pty else self._guard_refusal(grid)
            if slot.get("restart_attempt_at"):
                return
            if guard_refusal and slot.get("guard_refusal_attempts", 0) >= 5:
                return
            if not ((self._pty_failure(grid) if no_pty else self._startup_failure(grid)) or guard_refusal):
                return
            prepare_slot_sqlite_home(self.config_path, self.job["id"], slot["index"])
            prepare_working_directory(self.config_path, self.job, slot["index"])
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
            proof = slot.setdefault("confirmation", {"offset": slot["transcript_offset"],
                "session_id": slot["session_id"], "context_parser_version": CONTEXT_PARSER_VERSION})
            identity = [stat.st_dev, stat.st_ino]
            if proof.get("session_id", slot["session_id"]) != slot["session_id"]:
                proof["blocked"] = "original session changed"
            if proof.get("identity", identity) != identity or stat.st_size < proof["offset"]:
                proof["blocked"] = "original transcript changed or truncated"
            if proof.get("blocked"):
                return self._recheck_context_proof(slot, proof)
            if proof.get("confirmed"):
                return True
            proof["context_parser_version"] = CONTEXT_PARSER_VERSION
            if proof.get("identity") and stat.st_size == proof["offset"]:
                return False
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if [opened.st_dev, opened.st_ino] != identity:
                    proof["blocked"] = "original transcript changed while opening"
                    return False
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
                    if proof.get("expected_task_id") and payload.get("turn_id") != proof["expected_task_id"]:
                        proof["blocked"] = "original task changed"
                        return False
                    if epoch(event["timestamp"]) < slot["submit_at"] - .001 or proof.get("started"):
                        proof["blocked"] = "different task before batch prompt"
                        return False
                    proof.update(started=True, task_id=payload.get("turn_id"), task_at=event["timestamp"])
                    if self.job.get('ui_timing_origin'):
                        from ccc_batch_timing import stamp
                        from ccc_guard_scope import birth
                        proof['first_task_observed'] = stamp()
                        observed_birth = None
                        with contextlib.suppress(OSError, ValueError, RuntimeError):
                            observed_birth = birth(slot.get('pid'))
                        proof['first_task_observed_identity'] = {
                            'surface_id': slot.get('surface_id'), 'workspace_id': self.job['workspace_id'],
                            'session_id': slot.get('session_id'), 'turn_id': payload.get('turn_id'),
                            'pid': slot.get('pid'), 'birth': observed_birth,
                            'verified': bool(observed_birth and observed_birth == slot.get('native_birth'))}
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
                    if message != job_prompt(self.job):
                        proof["blocked"] = "different user prompt"
                        return False
                    proof["prompt"] = True
                if proof.get("started") and proof.get("prompt"):
                    current = path.stat()
                    if [current.st_dev, current.st_ino] != identity or current.st_size < proof["offset"]:
                        proof["blocked"] = "original transcript changed while confirming"
                        return False
                    proof.update(confirmed=True, confirmed_at=self.clock())
                    proof.pop("partial", None)
                    return True
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return False

    def _recheck_context_proof(self, slot, proof):
        """Read an old rejected start again, without replaying any input.

        The caller has already checked the saved session and file identity and
        rejected truncation. Keep the old rejection while a bounded, restartable
        cursor revalidates the original submitted task. Genuine operator input
        remains blocked and is not rescanned forever.
        """
        if (proof.get("blocked") != "different user prompt" or not proof.get("identity")
                or int(proof.get("context_parser_version") or 0) >= CONTEXT_PARSER_VERSION
                or proof.get("context_rechecked_version") == CONTEXT_PARSER_VERSION):
            return False
        origin = {"session_id": slot["session_id"], "identity": proof["identity"],
                  "transcript_offset": slot["transcript_offset"], "submit_at": slot["submit_at"],
                  "task_id": proof.get("task_id")}
        retry = proof.setdefault("context_recheck", {
            "offset": slot["transcript_offset"], "session_id": slot["session_id"],
            "identity": proof["identity"],
            "context_parser_version": CONTEXT_PARSER_VERSION,
            "expected_task_id": proof.get("task_id"), "origin": origin,
        })
        if (not isinstance(retry, dict) or retry.get("origin") != origin
                or retry.get("session_id") != slot["session_id"]
                or retry.get("context_parser_version") != CONTEXT_PARSER_VERSION
                or retry.get("expected_task_id") != proof.get("task_id")
                or type(retry.get("offset")) is not int
                or retry["offset"] < slot["transcript_offset"]
                or retry.get("identity", proof["identity"]) != proof["identity"]
                or retry.get("confirmed")):
            proof["context_recheck_error"] = "invalid original context recheck cursor"
            return False
        candidate = {**slot, "confirmation": retry}
        if self._confirm(candidate):
            previous = {key: value for key, value in proof.items() if key != "context_recheck"}
            slot["confirmation"] = {**candidate["confirmation"], "legacy_context_revalidation": {
                "parser_version": CONTEXT_PARSER_VERSION, "at": self.clock(), "previous": previous}}
            return True
        if retry.get("blocked"):
            proof["context_rechecked_version"] = CONTEXT_PARSER_VERSION
        return False

    def _release(self, slot):
        if slot.get("phase") != "confirmed" or not slot.get("confirmation", {}).get("confirmed"):
            return
        if self._slot_parent is not None:
            with self._slot_parent._state_lock:
                self._slot_parent._release_pending.add(slot["index"])
            return
        self._release_many([slot])

    def _release_many(self, slots):
        def release(config):
            rule = next((r for r in config["workspace_rules"] if r.get("workspace_id") == self.job["workspace_id"]), {})
            reasons = rule.get("excluded_surface_reasons", {})
            for slot in slots:
                sid = slot["surface_id"]
                if reasons.get(sid) == f"batch:{self.job['id']}:initial":
                    reasons.pop(sid)
                    rule["excluded_surface_ids"] = [s for s in rule.get("excluded_surface_ids", []) if s != sid]
                if rule.get("batch_start_holds", {}).get(sid, {}).get("job_id") == self.job["id"]:
                    rule["batch_start_holds"].pop(sid)
        self.store.mutate(release)
        for slot in slots:
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
    def _own_prompt_draft(grid, prompt=PROMPT):
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
        if cursor.column != (2 + len(prompt) if row == cursor.row else 2):
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
        return "".join(cells[2:]).rstrip() == prompt

    def _finish_submission(self, slot):
        if argv_initial(self.job, self.config_path):
            return
        if slot.get("enter_attempt_at") or self.clock() - slot.get("submit_at", self.clock()) < .2:
            return
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self._configuration()
            if not allowed(config, self.job):
                return
            if not self._protected(config, slot):
                return
            target = self._target(slot, fresh=True)
            native = self._native(target, slot)
            if (not native or native.get("kind") not in {"unknown", "uninitialized"}
                    or any(native.get(k) != slot.get(k) for k in ("session_id", "pid", "process_start"))):
                return
            grid = core.Grid.from_rpc(self._replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            if not self._own_prompt_draft(grid, job_prompt(self.job)) or self._native(target, slot) != native:
                return
            slot["enter_attempt_at"] = self.clock()
            self.save()  # A missing Enter acknowledgement is never retried.
            if not self._initial_input_ready(slot, target, native, draft=job_prompt(self.job)):
                slot.pop("enter_attempt_at", None)
                slot["enter_not_sent_at"] = self.clock()
                self.save()
                return
            try:
                with self._initial_connected_guard(slot, target, native, draft=job_prompt(self.job)):
                    self.client.send_key(target["workspace_id"], target["surface_id"], "enter")
            except core.InputNotSentError as exc:
                slot.pop("enter_attempt_at", None)
                slot.update(enter_not_sent_at=self.clock(), error=str(exc))
                self.save()
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="uncertain", error=str(exc))

    def _initial_connected_guard(self, slot, target, native, *, draft=None):
        client = self.client.client if isinstance(self.client, SnapshotClient) else self.client
        if isinstance(client, core.CmuxClient):
            return client.input_guard(lambda: self._initial_input_ready(
                slot, target, native, draft=draft, connected_client=client))
        return contextlib.nullcontext()

    def _initial_input_ready(self, slot, target, native, *, draft=None, connected_client=None):
        """Recheck after durable intent, immediately before actual input."""
        try:
            config = self._configuration()
            if self._stopping() or not allowed(config, self.job) or not self._protected(config, slot):
                return False
            current = (core.find_main_surface(connected_client.workspace_tree(target["workspace_id"]),
                                             target["surface_id"]) if connected_client
                       else self._target(slot, fresh=True))
            if (any(current.get(key) != target.get(key) for key in
                    ("surface_id", "workspace_id", "pane_id", "window_id"))
                    or self._native(target, slot) != native):
                return False
            replay = (connected_client.replay(target["workspace_id"], target["surface_id"], live=True)
                      if connected_client else self._replay(target["workspace_id"], target["surface_id"]))
            grid = core.Grid.from_rpc(replay, target["surface_id"])
            empty = core.classify_grid(grid).kind == "idle" and core._composer_status(grid)[0] == "empty"
            if not (empty if draft is None else self._own_prompt_draft(grid, draft)):
                return False
            config = self._configuration()
            return (not self._stopping() and allowed(config, self.job) and self._protected(config, slot)
                    and self._native(target, slot) == native)
        except (OSError, ValueError, core.CmuxError, RuntimeError):
            return False

    def _send_name_input(self, slot, target, native, field, send):
        naming = slot["naming"]
        naming[field] = self.clock()
        self.save()
        # A final read can fail after the intent was saved. Until send() is
        # called we KNOW no input was attempted. Keep that distinction on disk
        # so an ordinary observation race cannot strand the slot forever.
        try:
            config = self._configuration()
            ready = (allowed(config, self.job) and self._protected(config, slot)
                     and self._native(target, slot) == native)
            if ready and naming.get("birth"):
                from ccc_guard_scope import birth
                ready = birth(native["pid"], codex=True) == naming["birth"]
            if ready:
                ready = self._initial_input_ready(slot, target, native,
                    draft=naming["command"] if field == "enter_at" else None)
        except (OSError, ValueError, core.CmuxError, RuntimeError):
            ready = False
        if not ready:
            naming.pop(field, None)
            naming.update(deferred_at=self.clock(), deferred_stage=field)
            slot["error"] = "命名前检查暂未通过；本次未发送输入"
            self.save()
            return False
        try:
            with self._initial_connected_guard(slot, target, native,
                    draft=naming["command"] if field == "enter_at" else None):
                send()
        except core.InputNotSentError as exc:
            naming.pop(field, None)
            naming.update(deferred_at=self.clock(), deferred_stage=field)
            slot["error"] = "命名前最终连接检查未通过；零输入：" + str(exc)
        except (core.CmuxError, RuntimeError) as exc:
            # The transport was entered: do not reset or replay this attempt.
            slot["error"] = "原生命名输入待核验：" + str(exc)
        else:
            naming[field + "_acknowledged"] = self.clock()
            slot["error"] = "等待原生命名确认；未发送模型请求"
        self.save()
        return False

    def _prepare_name(self, slot, target, native):
        """A local /rename avoids a hidden paid title-generation turn.

        Only a fresh B session can reach this path. Both the draft and Enter
        have durable one-shot records; an uncertain acknowledgement never
        permits a second command or bypasses an operator's input.
        """
        policy = self.job.get("name_policy")
        if policy is None:
            return True
        if policy != "before-first-turn-v1":
            raise RuntimeError("unknown B session-name policy")
        if native.get("kind") not in {"unknown", "uninitialized"} or not native.get("session_id"):
            return False
        expected = {"session_id": native["session_id"], "pid": native["pid"],
                    "process_start": native.get("process_start")}
        naming = slot.get("naming")
        if naming and any(naming.get(key) != value for key, value in expected.items()):
            slot["error"] = "原命名 session 已改变；未发送批量任务"
            return False
        with core.workspace_input_lock(self.config_path, self.job["workspace_id"], shared=True):
            config = self._configuration()
            if not allowed(config, self.job) or not self._protected(config, slot):
                return False
            target = self._target(slot, fresh=True)
            if self._native(target, slot) != native:
                return False
            known = self.name_lookup(target, native)
            if known is None:
                slot["error"] = "等待原生名称身份核验；未发送模型请求"
                return False
            if naming and naming.get("birth") and naming["birth"] != known.get("birth"):
                slot["error"] = "原命名进程身份已改变；未发送批量任务"
                return False
            if known.get("birth"):
                expected["birth"] = known["birth"]
            if known.get("name"):
                # A manually chosen name also suppresses automatic generation.
                # Preserve it rather than overwriting it with our label.
                slot["naming"] = {**(naming or expected), "confirmed_name": known["name"],
                                  "confirmed_at": self.clock()}
                self.save()
                return True
            grid = core.Grid.from_rpc(self._replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            if naming and naming.get("submitted_at"):
                if naming.get("enter_at") or self.clock() - naming["submitted_at"] < .2:
                    return False
                if not self._own_prompt_draft(grid, naming["command"]) or self._native(target, slot) != native:
                    return False
                return self._send_name_input(slot, target, native, "enter_at", lambda:
                    self.client.send_key(target["workspace_id"], target["surface_id"], "enter"))
            if core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty":
                return False
            if self._native(target, slot) != native:
                return False
            command = f"/rename B-check-{self.job['id'][:8]}-{slot['index'] + 1:02d}"
            slot["naming"] = {**(naming or {}), **expected, "command": command}
            return self._send_name_input(slot, target, native, "submitted_at", lambda:
                self.client.draft_batch_session_name(target["workspace_id"], target["surface_id"],
                                                     self.job["id"], slot["index"]))

    def _observe_argv_initial(self, slot, receipt):
        """Read the immutable first startup hook, including after native exit."""
        path = self.path.parent / f"initial-argv-{slot['index']}.json"
        try:
            raw = path.read_bytes()
            claim = json.loads(raw)
            digest = hashlib.sha256(raw).hexdigest()
            expected = {"policy": ARGV_INITIAL_POLICY, "job_id": self.job["id"],
                        "index": slot["index"], "workspace_id": self.job["workspace_id"],
                        "surface_id": slot.get("surface_id"), "launch_id": slot.get("launch_id")}
            if (any(claim.get(k) != v for k, v in expected.items())
                    or claim.get("state") != "exec_intent"
                    or claim.get("argv", [])[-1:] != [PROMPT]
                    or slot.get("argv_claim_sha256", digest) != digest):
                raise ValueError("initial prompt claim identity changed")
            receipt_raw = (self.path.parent / f"surface-{slot['index']}.json").read_bytes()
            if (hashlib.sha256(receipt_raw).hexdigest() != claim.get("receipt_sha256")
                    or json.loads(receipt_raw) != receipt):
                raise ValueError("initial prompt registration changed")
            if slot.get("argv_claim_sha256"):
                return True
            identity = claim["bootstrap_birth"]
            if not isinstance(identity, list) or len(identity) != 2:
                raise ValueError("initial bootstrap birth missing")
            first_path = self.path.parent / f"initial-session-{slot['index']}.json"
            first_raw = first_path.read_bytes()
            first = json.loads(first_raw)
            if (first.get("claim_sha256") != digest or first.get("pid") != claim["bootstrap_pid"]
                    or first.get("birth") != identity or first.get("source") != "startup"
                    or first.get("surface_id") != slot["surface_id"]
                    or first.get("workspace_id") != self.job["workspace_id"]):
                raise ValueError("original startup hook identity changed")
            event_data, sent = _initial_event_prefix(claim, require_turn=True)
            if (len(event_data) < first["tui_prefix_bytes"] or hashlib.sha256(
                    event_data[:first["tui_prefix_bytes"]]).hexdigest() != first["tui_prefix_sha256"]):
                raise ValueError("original startup event prefix changed")
            if not sent:
                return False
            session = str(uuid.UUID(first["session_id"]))
            root = Path(first["sessions_root"])
            if root != self.queue.sessions_root.resolve():
                raise ValueError("initial hook sessions root changed")
            if first.get("transcript"):
                transcript = Path(first["transcript"])
            else:
                paths = list(root.rglob(f"*{session}.jsonl"))
                if len(paths) != 1:
                    return False
                transcript = paths[0]
            if transcript.resolve() != transcript or not transcript.is_relative_to(root):
                raise ValueError("initial transcript outside original sessions")
            if first_path.read_bytes() != first_raw:
                raise ValueError("original startup hook changed while reading")
            slot.update(argv_claim_sha256=digest, session_id=session,
                        argv_first_hook_sha256=hashlib.sha256(first_raw).hexdigest(),
                        argv_initial_event_sha256=hashlib.sha256(event_data).hexdigest(),
                        pid=claim["bootstrap_pid"], process_start=identity[0],
                        native_birth=identity, transcript=str(transcript), transcript_offset=0,
                        submit_at=claim["at"], phase="submitted")
            if self.job.get('ui_timing_origin'):
                slot.setdefault('ui_original_identity', {
                    'surface_id':slot['surface_id'], 'workspace_id':self.job['workspace_id'],
                    'session_id':session, 'pid':claim['bootstrap_pid'], 'birth':identity})
            self.save()
            return True
        except FileNotFoundError:
            slot["error"] = "等待首发永久记录或原生首会话；不发送界面输入"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            slot["error"] = "首发证据尚不可确认：" + str(exc)
        return False

    def _advance(self, slot, *, confirmation_only=False):
        receipt = core.load_json(self.path.parent / f"surface-{slot['index']}.json", {})
        initial = argv_initial(self.job, self.config_path)
        if not initial and not confirmation_only and self._recover_rejected_start(slot, receipt):
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
        if initial and not self._observe_argv_initial(slot, receipt):
            return
        if slot["phase"] in CONFIRMABLE:
            if self._confirm(slot):
                slot["phase"] = "confirmed"
                slot.pop("error", None)
                # Proof and phase are durable before changing authorization.
                # A crash on either side is repaired without replaying input.
                self.save()
                if self.job.get("check_retry_policy"):
                    from ccc_private_check import record_origin
                    record_origin(self.config_path, self.job, slot)
                rule = next((r for r in self._configuration()["workspace_rules"]
                             if r.get("workspace_id") == self.job["workspace_id"]), {})
                if core.batch_start_hold(rule, slot["surface_id"]):
                    self._release(slot)
                    self.save()
            elif not confirmation_only and not initial:
                self._finish_submission(slot)
            return
        if confirmation_only:
            return
        if slot["phase"] not in {"created", "startup_wait", "restart_pending", "pty_wait", "access_ready"}:
            return
        if slot['phase'] == 'access_ready' and (
                not access_cohort_prepared(self.job) or not getattr(self, '_access_membership_ready', False)):
            return  # Native startup can take minutes across many workspaces.
        if not receipt:
            slot["error"] = "等待新 shell 原始回执"
            if self.clock() - slot["created_at"] >= 3 and slot.get("surface_id"):
                target = self._target(slot)
                grid = core.Grid.from_rpc(self._replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
                if self._pty_failure(grid):
                    self._protect_created(slot)
                    slot.update(phase="pty_wait", error="系统 PTY 名额已满；等待空位后在原 surface 补做")
                    self._restart_failed(slot, no_pty=True)
            return
        if not self._protected(self._configuration(), slot):
            slot.update(phase="blocked", error="此路授权已被修改，未发送 prompt")
            return
        target = self._target(slot)
        native = self._native(target, slot)
        if (native or {}).get("session_id"):
            slot["native_seen_session_id"] = native["session_id"]
        if native and native.get("session_id") and native.get("kind") not in {"unknown", "uninitialized"}:
            slot.update(phase="blocked", error="此 session 已有任务，未发送批量 prompt")
            return
        if (self.job.get("name_policy") is not None and native and native.get("session_id") and native.get("pid")
                and not self._prepare_name(slot, target, native)):
            return
        grid = core.Grid.from_rpc(self._replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
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
            config = self._configuration()
            if not allowed(config, self.job):
                return
            if not self._protected(config, slot):
                slot.update(phase="blocked", error="此路授权已被修改，未发送 prompt")
                return
            target = self._target(slot, fresh=True)
            grid = core.Grid.from_rpc(self._replay(target["workspace_id"], target["surface_id"]), target["surface_id"])
            if (core.classify_grid(grid).kind != "idle" or core._composer_status(grid)[0] != "empty"
                    or self._native(target, slot) != native):
                return
            if startup_mode(self.job, self.config_path) == 'access_check' and slot['phase'] != 'access_ready':
                from ccc_access_service import bind_slot
                slot.update(pid=native['pid'], process_start=native.get('process_start'))
                bind_slot(self.config_path, self.job, slot, target, native)
                # Prepared sessions no longer occupy a cold-start permit. All
                # fifty must be prepared before any prompt starts a local HTTP
                # request or its much shorter connection/cohort deadline.
                slot.update(phase='access_ready', access_ready_at=self.clock())
                self.save()
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
            unsent = copy.deepcopy(slot)
            slot.update(phase="submitting", submit_at=self.clock(), transcript=path,
                        transcript_offset=offset, pid=native["pid"],
                        process_start=native.get("process_start"), native_uninitialized=uninitialized)
            if self.job.get('ui_timing_origin'):
                from ccc_guard_scope import birth
                born = None
                with contextlib.suppress(OSError, ValueError, RuntimeError):
                    born = birth(native['pid'])
                slot['native_birth'] = born if born and born[0] == native.get('process_start') else None
                slot.setdefault('ui_original_identity', {
                    'surface_id':slot['surface_id'], 'workspace_id':self.job['workspace_id'],
                    'session_id':slot['session_id'], 'pid':native['pid'], 'birth':slot['native_birth']})
            self.save()
            if startup_mode(self.job, self.config_path) == 'access_check':
                from ccc_access_service import bind_slot
                bind_slot(self.config_path, self.job, slot, target, native)
            if not self._initial_input_ready(slot, target, native):
                # No transport entry occurred. Preserve that distinction so a
                # pause or late user draft cannot become an automatic retry of
                # uncertain I/O, or strand a still-unsent authorized session.
                slot.clear()
                slot.update(unsent, submit_not_sent_at=self.clock(),
                            submit_not_sent_count=unsent.get("submit_not_sent_count", 0) + 1)
                self.save()
                return
            try:
                slot["send_dispatched_at"] = self.clock()
                with self._initial_connected_guard(slot, target, native):
                    self.client.send_text(target["workspace_id"], target["surface_id"], job_prompt(self.job))
                slot["phase"] = "submitted"
            except core.InputNotSentError as exc:
                slot.pop("submit_at", None)
                slot.pop("send_dispatched_at", None)
                slot.update(phase="created", input_not_sent_at=self.clock(), error=str(exc))
            except (core.CmuxError, RuntimeError) as exc:
                slot.update(phase="uncertain", error=str(exc))
            self.save()

    def _reserve_start(self, slot, *, restarting=False):
        # Per-slot single flight and the job's worker lock replace the old
        # global four-permit/rate/round-robin budget. No historical job scan
        # or other pool's startup/naming latency belongs on this path.
        expected = {"restart_pending", "pty_wait", "created"} if restarting else {"pending"}
        if slot.get("phase") not in expected:
            return False
        if not allowed(self._configuration(), self.job):
            self._wait("authorization", "批次授权已变化；未预约新的启动")
            return False
        now = self.clock()
        self._clear_wait()
        self._wait_observed = False
        self.job["status"] = "running"
        slot.update(phase="restarting" if restarting else "creating", launched_at=now,
                    launch_id=str(uuid.uuid4()))
        slot.setdefault("created_at", now)
        self.save()
        return True

    def _replay(self, workspace_id, surface_id):
        # Startup is tied to the live native composer, never a scrolled-back
        # historical viewport. Keep this per-call so shared observers retain
        # their original read mode.
        client = self.client.client if isinstance(self.client, SnapshotClient) else self.client
        if isinstance(client, core.CmuxClient):
            return client.replay(workspace_id, surface_id, live=True)
        return self.client.replay(workspace_id, surface_id)

    def _refresh_processes(self):
        if argv_initial(self.job, self.config_path):
            # This protocol only observes its pinned claim/hook/transcript.
            # It never enters _native() or the legacy UI startup path that
            # consumes these labels. Avoid a global process RPC per batch;
            # bootstrap authorization and original-session proof stay fresh.
            return
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
                          and s.get("phase") in INITIALIZING | {"startup_wait", "restart_pending", "pty_wait", "surface_closed", "access_ready"}
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
            if slot["index"] in self._inflight:
                continue
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

    def _access_ready_preflight(self, tree):
        """Check the whole prepared batch before releasing its first prompt.

        This startup-only read is outside input locks. Each eventual send still
        repeats its own identity, composer and current authorization checks.
        """
        rows = {row['surface_id']: row for row in
                core.workspace_surface_records(tree, self.job['workspace_id']).values()}
        for slot in self.job['slots']:
            target = rows.get(slot['surface_id'])
            if target is None:
                return False
            native = self._native(target, slot)
            if (not native or native.get('kind') not in {'unknown', 'uninitialized'}
                    or any(native.get(key) != slot.get(key) for key in ('pid', 'process_start', 'session_id'))):
                return False
            grid = core.Grid.from_rpc(self._replay(target['workspace_id'], target['surface_id']), target['surface_id'])
            if core.classify_grid(grid).kind != 'idle' or core._composer_status(grid)[0] != 'empty':
                return False
        return True

    def step(self):
        self._collect_slots()
        self._access_membership_ready = False
        self._wait_observed = False
        try:
            config = self._configuration()
        except (OSError, ValueError, RuntimeError) as exc:
            # Parallel shell registrations can replace the config during a
            # read. Keep this worker and every in-flight slot; retry the
            # authorization read, never a possibly delivered command.
            with self._state_lock:
                self._wait("authorization", "等待当前授权核验：" + str(exc))
                self.save()
            return True
        rule = next((r for r in config["workspace_rules"]
                     if r.get("workspace_id") == self.job["workspace_id"]), {})
        with self._state_lock:
            confirmations = [s["index"] for s in self.job["slots"]
                             if s["phase"] in CONFIRMABLE and s["index"] not in self._inflight
                             and not (s["phase"] == "confirmed" and not core.batch_start_hold(rule, s.get("surface_id")))
                             and self.clock() >= max(s.get("retry_at", 0), self._slot_due.get(s["index"], 0))]
        if not allowed(config, self.job):
            # Read-only original transcript confirmation remains possible after
            # pause. No newly created slot or initial input is scheduled.
            self._dispatch_slots(confirmations, confirmation_only=True)
            from ccc_batch_guard import snapshot
            guard = snapshot(self.config_path, self.job["workspace_id"])
            with self._state_lock:
                self.job["status"] = "stopped_success" if (guard.get("trip") or {}).get("connected") else "cancelled"
                self._clear_wait()
                self.save()
            return False
        try:
            tree = self._membership_tree()
            workspaces = [w.get("id") for win in tree.get("windows", []) for w in win.get("workspaces", [])]
            if self.job["workspace_id"] not in workspaces:
                self._dispatch_slots(confirmations, confirmation_only=True)
                with self._state_lock:
                    self.job["status"] = "workspace_closed"
                    self._clear_wait()
                    self.save()
                return False
        except (core.CmuxError, RuntimeError) as exc:
            self._dispatch_slots(confirmations, confirmation_only=True)
            with self._state_lock:
                self._wait("topology", "等待工作区拓扑：" + str(exc))
                self.save()
            return True
        with self._state_lock:
            self._restore_present_slots(tree)
        try:
            self._refresh_processes()
        except (core.CmuxError, RuntimeError) as exc:
            self.processes = {}
            with self._state_lock:
                self._wait("process_inventory", "等待原进程核验：" + str(exc))
        present = {r["surface_id"] for r in core.workspace_surface_records(tree, self.job["workspace_id"]).values()}
        with self._state_lock:
            access_ready = startup_mode(self.job, self.config_path) == 'access_check' and access_cohort_prepared(self.job)
        if access_ready:
            self._access_membership_ready = all(s['surface_id'] in present for s in self.job['slots'])
            if self._access_membership_ready and not any(s.get('submit_at') for s in self.job['slots']):
                try:
                    self._access_membership_ready = self._access_ready_preflight(tree)
                except (OSError, ValueError, core.CmuxError, RuntimeError):
                    self._access_membership_ready = False
            if not self._access_membership_ready:
                self._dispatch_slots(confirmations, confirmation_only=True)
                with self._state_lock:
                    self._wait('access_cohort', '等待本池全部50个原会话和空输入框；未发送新的接入检查')
                    self.save()
                return True
        ready = []
        with self._state_lock:
            for slot in self.job["slots"]:
                if (slot["index"] in self._inflight
                        or self.clock() < max(slot.get("retry_at", 0), self._slot_due.get(slot["index"], 0))):
                    continue
                if slot["phase"] == "confirmed":
                    if slot["index"] in confirmations:
                        ready.append(slot["index"])
                    continue
                if slot["phase"] not in INITIALIZING | {"pending", "startup_wait", "restart_pending", "pty_wait", "access_ready"}:
                    continue
                if (slot.get("surface_id") and slot["surface_id"] not in present
                        and self.clock() - slot.get("launched_at", slot.get("created_at", self.clock())) >= 5):
                    slot.update(phase="surface_closed", error="原 surface 已关闭或移出本池；不补建替代会话")
                    continue
                ready.append(slot["index"])
        # Submit the entire ready set. A slow/uncertain RPC retains exactly its
        # own slot future and cannot stall another slot's next startup phase.
        self._dispatch_slots(ready)
        self._collect_slots()
        with self._state_lock:
            if (not self._inflight and not self._release_pending and self._release_future is None
                    and all(s["phase"] == "confirmed" for s in self.job["slots"])):
                self.job["status"] = "complete"
                self._clear_wait()
            elif (not self._inflight and not self._release_pending and self._release_future is None
                    and all(s["phase"] in {"confirmed", "blocked", "surface_closed"} for s in self.job["slots"])):
                self.job["status"] = "needs_attention"
                self._clear_wait()
            elif self._wait_observed:
                self.job["status"] = "waiting"
            else:
                self.job["status"] = "running"
                self._clear_wait()
            self.save()
            return self.job["status"] not in {"complete", "needs_attention", "cancelled", "workspace_closed"}

    def run(self):
        from ccc_guard_scope import birth
        try:
            with core.FileLock(self.path.parent / "worker.lock", timeout_sec=0):
                try:
                    self.job = core.load_json(self.path, {})
                    self.job.update(status="running", worker_pid=os.getpid(),
                                    worker_birth=birth(os.getpid()), worker_version=WORKER_VERSION)
                    for slot in self.job["slots"]:
                        # v0.2.12 marked slow starts blocked at 25 s. Explicit
                        # operator-task/authorization vetoes remain blocked.
                        if (slot["phase"] == "blocked" and not slot.get("submit_at")
                                and slot.get("error") not in {"此 session 已有任务，未发送批量 prompt", "此路授权已被修改，未发送 prompt"}):
                            slot["phase"] = "created"
                    self.save()
                    while True:
                        self._wakeup.clear()
                        if not self.step():
                            break
                        self._wakeup.wait(STARTUP_POLL_SEC)
                finally:
                    self.close()
        finally:
            self.close()


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
        self.workers, self.launched = OrderedDict(), {}
        self._config_cache = None
        self.thread = threading.Thread(target=self._run, name="ccc-batch-reconcile", daemon=True)

    def _config(self):
        """Reuse validation only while the exact file identity is unchanged.

        Every authorization check still stats the file; a concurrent B/P/config
        edit invalidates the cache. Actual worker input and config mutation keep
        their existing locked, fresh authorization checks.
        """
        def stamp():
            value = self.path.stat()
            return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_size
        identity = stamp()
        if self._config_cache is not None and self._config_cache[0] == identity:
            return self._config_cache[1]
        config = self.store.load()
        self._config_cache = (identity, config) if stamp() == identity else None
        return config

    def _worker(self, jid):
        worker = self.workers.pop(jid, None)
        if worker is None:
            worker = BatchWorker(self.path, jid, client=self.client)
        self.workers[jid] = worker
        # These are idle reconciliation adapters, never native session owners.
        # Historical jobs must not retain an unbounded number of hook/process
        # caches. Proof cursors already persist in each original job file.
        while len(self.workers) > RECONCILE_CACHE_LIMIT:
            _, old = self.workers.popitem(last=False)
            old.cache.close()
        return worker

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        for worker in self.workers.values():
            worker.cache.close()

    def cycle(self):
        config = self._config()
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
                    if 'standby_policy' in job:
                        continue  # Preparation must never submit through the old worker.
                    current_config = self._config()
                    rule = next((r for r in current_config["workspace_rules"] if r.get("workspace_id") == job["workspace_id"]), {})
                    if (job.get("status") == "complete"
                            and all(s.get("phase") == "confirmed" for s in job.get("slots", []))
                            and not any(core.batch_start_hold(rule, s.get("surface_id")) for s in job.get("slots", []))):
                        old = self.workers.pop(jid, None)
                        if old is not None:
                            old.cache.close()
                        continue
                    worker = self._worker(jid)
                    worker.job = job
                    # This is the snapshot just read under worker.lock. A
                    # recreated idle adapter must not rewrite unchanged jobs;
                    # later proof/receipt changes still require a durable save.
                    worker._saved = worker._serialized_job()
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
                    if (self.launch and allowed(self._config(), job)
                            and job.get("status") not in {"complete", "needs_attention", "workspace_closed"}
                            and time.monotonic() - self.launched.get(jid, 0) >= 10):
                        _launch(self.path, job)
                        self.launched[jid] = time.monotonic()
            except (OSError, ValueError, RuntimeError) as exc:
                # A running worker holds the lock; it owns both job and proof.
                if "lock" in str(exc).lower() and self.launch:
                    job = core.load_json(path, {})
                    if job and 'standby_policy' not in job and allowed(self._config(), job):
                        retire_old_worker(job, config_path=self.path)
                else:
                    logging.getLogger(core.APP_NAME).warning("batch=%s reconciliation: %s", jid, exc)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except (OSError, ValueError, RuntimeError) as exc:
                logging.getLogger(core.APP_NAME).warning("batch reconciliation: %s", exc)
            self.stop.wait(2)


def retire_old_worker(job, *, config_path=None):
    """An old panel cannot keep a pre-upgrade helper alive indefinitely.

    Only the exact batch helper is retired. Native Codex processes and
    independently owned acceptance controllers are never signalled. A newly
    launched helper may hold the lock before it publishes its claim: its
    actual source version, not the stale job version, vetoes retirement.
    """
    pid = job.get("worker_pid")
    if job.get("worker_version", 0) >= WORKER_VERSION or type(pid) is not int or pid <= 1:
        return False
    try:
        from ccc_guard_scope import birth, arguments
        generation = birth(pid)
        if generation is None or (job.get("worker_birth") is not None and job["worker_birth"] != generation):
            return False
        args, _ = arguments(pid)
        if not args or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(args[0]).name, re.IGNORECASE):
            return False
        index = 2 if args[1:2] == ["-B"] else 1
        tail = args[index + 1:]
        if len(tail) != 5 or tail[0:2] != ["run", "--config"] or tail[3:] != ["--job", job["id"]]:
            return False
        expected_config = config_path or job.get("config_path")
        if not expected_config or Path(tail[2]).resolve() != Path(expected_config).resolve():
            return False
        script = Path(args[index])
        if not script.is_absolute() or script.name != "ccc_workspace_batch.py":
            return False
        source = script.read_bytes()
        versions = [node.value for node in ast.parse(source).body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "WORKER_VERSION" for t in node.targets)]
        if (len(versions) != 1 or not isinstance(versions[0], ast.Constant)
                or type(versions[0].value) is not int or not 0 < versions[0].value < WORKER_VERSION):
            return False
        if script.read_bytes() != source or arguments(pid)[0] != args or birth(pid) != generation:
            return False
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ValueError, IndexError, RuntimeError, SyntaxError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "register", "bind-initial"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--launch-id", default="")
    parser.add_argument("--launch-native", action="store_true")
    args = parser.parse_args()
    if args.action == "bind-initial":
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise RuntimeError("initial hook payload too large")
        try:
            bind_initial_session(args.config, args.job, args.index, args.launch_id, json.loads(raw))
        except Exception as exc:
            error_path = job_path(args.config, args.job).parent / f"initial-hook-error-{args.index}.json"
            with contextlib.suppress(FileExistsError):
                _write_once_json(error_path, {"at": time.time(), "error_type": type(exc).__name__,
                                              "error": str(exc)})
            raise
    elif args.action == "register":
        if args.launch_native:
            launch_registered(args.config, args.job, args.index, args.launch_id)
        else:
            register(args.config, args.job, args.index, args.launch_id)
    else:
        BatchWorker(args.config, args.job).run()


if __name__ == "__main__":
    main()
