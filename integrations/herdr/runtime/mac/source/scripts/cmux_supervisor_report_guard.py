#!/usr/bin/env python3
"""PostToolUse：认证主管有界发现冻结报告；发现不等于送达、验收或解封。"""
import hashlib
import fcntl
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

import cmux_hook_identity as hook_identity
import cmux_hook_scope as hook_scope
import cmux_consensus_stop_guard as stop_guard
from cmux_evidence_io import MAX_EVIDENCE_BYTES, read_bytes
from executor_closeout import terminal_report
import supervisor_inbox

MAX_MARKERS = 32
MAX_NOTICES = 8
SCAN_SECONDS = 0.75
MAX_DIRECTORY_ENTRIES = 4096


def _workspace_paths(workspace, deadline, stack):
    """绑定目录 fd 后有界枚举；后续读取不能跟随被替换的目录路径。"""
    # 普通 hook payload 也不能把 workspace 当成任意文件系统路径。
    try:
        if not isinstance(workspace, str) or str(uuid.UUID(workspace)) != workspace.lower():
            return {}
    except (ValueError, AttributeError):
        return {}
    directory = stop_guard.ACTIVE_DIR / workspace
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    active_fd = os.open(stop_guard.ACTIVE_DIR, flags)
    stack.callback(os.close, active_fd)
    paths = {}
    try:
        directory_fd = os.open(workspace, flags, dir_fd=active_fd)
        stack.callback(os.close, directory_fd)
        with os.scandir(directory_fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DIRECTORY_ENTRIES or time.monotonic() >= deadline:
                    raise ValueError('active marker directory exceeds discovery budget')
                if not entry.name.startswith('.') and entry.name.endswith('.json'):
                    paths[directory / entry.name] = directory_fd
    except FileNotFoundError:
        pass
    legacy = stop_guard.ACTIVE_DIR / (workspace + '.json')
    try:
        os.stat(legacy.name, dir_fd=active_fd, follow_symlinks=False)
        paths[legacy] = active_fd
    except FileNotFoundError:
        pass
    return dict(sorted(paths.items()))


def _read_marker(path, directory_fd):
    """只从枚举时绑定的目录读取，拒绝特殊文件与读取期间的替换。"""
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path.name, flags, dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_EVIDENCE_BYTES:
                return None
            raw = stream.read(MAX_EVIDENCE_BYTES + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) > MAX_EVIDENCE_BYTES or not stat.S_ISREG(current.st_mode)
                or identity(before) != identity(after)
                or identity(before) != identity(current)):
            return None
        return json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None


def _save_cursor(path, last):
    pending = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.cursor-', delete=False) as handle:
            pending = Path(handle.name)
            json.dump({'last': str(last)}, handle)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _record_once(root, evidence, workspace, supervisor):
    # 持久化的是发现事件，不是 transport receipt；同一报告仅通知一次。
    slot = hashlib.sha256(json.dumps([
        workspace, supervisor, evidence['artifact_root'], evidence['task_id'],
        evidence['task_pack_sha256'], evidence['report_sha256'],
        evidence['completion_nonce']], ensure_ascii=False).encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError('discovery directory must not be a symlink')
    path = root / (slot + '.json')
    notice = dict(evidence, state='REPORT_DISCOVERED', workspace_uuid=workspace,
                  supervisor_uuid=supervisor, discovered_at_epoch=time.time(),
                  input_operations=0, delivery_confirmed=False, accepted=False, disarmed=False)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=root, prefix='.notice-', delete=False) as handle:
            temp = Path(handle.name)
            json.dump(notice, handle, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return None
        return dict(notice, discovery_record=str(path))
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def discover(payload, state_root=None):
    if (payload.get('hook_event_name') != 'PostToolUse'
            or not stop_guard.ACTIVE_DIR.exists()):
        return []
    with hook_identity.evaluation(payload):
        return discover_bound(*hook_identity.identity(payload), state_root=state_root, payload=payload)


def discover_bound(workspace, caller, state_root=None, payload=None):
    """Use the caller authenticated once by this hook; no terminal input."""
    if not stop_guard.ACTIVE_DIR.exists():
        return []
    root = Path(state_root or Path.home() / '.local/state/multi-agent-collaboration'
                / 'supervisor-report-discovery-v1')
    notices = []
    with ExitStack() as directories:
        if not caller or workspace == 'default':
            return []
        deadline = time.monotonic() + SCAN_SECONDS
        paths = _workspace_paths(workspace, deadline, directories)
        if not paths:
            return []
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_absolute() or root.is_symlink():
            raise ValueError('discovery directory must be an absolute nonsymlink directory')
        key = hashlib.sha256(json.dumps([workspace, caller]).encode()).hexdigest()
        cursor_path = root / ('cursor-' + key + '.json')
        fd = os.open(root / ('sweep-' + key + '.lock'),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'wb') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                return []
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return []
            try:
                saved = json.loads(read_bytes(cursor_path, 4096))
                last = saved.get('last', '') if isinstance(saved, dict) else ''
            except (OSError, ValueError, TypeError):
                last = ''
            if not isinstance(last, str):
                last = ''
            # 上次即使全是无关 marker 也前进，防止只看前32个导致饥饿。
            window = [p for p in paths if str(p) > last] + [p for p in paths if str(p) <= last]
            for path in window[:MAX_MARKERS]:
                if len(notices) >= MAX_NOTICES or time.monotonic() >= deadline:
                    break
                _save_cursor(cursor_path, path)
                marker = _read_marker(path, paths[path])
                if (not isinstance(marker, dict) or not stop_guard._marker_fresh(marker)
                        or marker.get('workspace_uuid', workspace) != workspace):
                    continue
                if payload is not None and not hook_scope.applies(marker, payload, workspace, caller):
                    continue
                peers = marker.get('participants', [])
                if not isinstance(peers, list):
                    continue
                supervisors = [p for p in peers if isinstance(p, dict) and p.get('role') == 'supervisor']
                if len(supervisors) != 1 or supervisors[0].get('surface_uuid') != caller:
                    continue
                for peer in peers:
                    if time.monotonic() >= deadline or len(notices) >= MAX_NOTICES:
                        break
                    if not isinstance(peer, dict) or not str(peer.get('role', '')).startswith('executor'):
                        continue
                    evidence = terminal_report(marker, workspace, peer.get('surface_uuid'))
                    if evidence:
                        notice = _record_once(root, evidence, workspace, caller)
                        if notice:
                            notices.append(notice)
    return notices


def main():
    payload = {}
    notices, requests = [], []
    emitted = False
    def emit(items):
        nonlocal emitted
        lines = []
        if notices:
            lines.append('REPORT_DISCOVERED: frozen executor reports need supervisor review. '
                         'This discovery is not native delivery, acceptance or disarm.')
            for item in notices:
                lines.append('TASK_ID=' + item['task_id'] + ' REPORT=' + item['report']
                             + ' ATTEMPT=' + item['attempt'])
        if items:
            lines.append(supervisor_inbox.context(items))
        if lines:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                              'additionalContext': '\n'.join(lines)}}, ensure_ascii=False), flush=True)
            emitted = True
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get('hook_event_name') != 'PostToolUse':
            return 0
        facts = supervisor_inbox.thread_scope.describe(payload)
        if facts['thread_scope'] != 'ROOT_METADATA':
            return 0
        # Resolve once under the existing identity deadline. Two independent
        # scans must not spend two 2.5-second identity budgets in a 5-second hook.
        with hook_identity.evaluation(payload):
            workspace, caller = hook_identity.identity(payload)
            if not supervisor_inbox.thread_scope.authenticated_root(facts, hook_identity.native_proof()):
                return 0
            try:
                notices = discover_bound(workspace, caller, payload=payload)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
                pass
            try:
                requests = supervisor_inbox.discover_bound(
                    workspace, caller, thread_id=facts['actual_thread_id'], emit=emit)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
                pass
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError) + hook_identity.ERRORS:
        # 发现失败不阻止主管纠错；也不生成任何已送达或已验收信号。
        pass
    if not emitted and notices:
        emit([])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
