#!/usr/bin/env python3
"""空闲 executor 每 60 秒主动求派，直到 Codex 主管答复（用户 2026-10-09 明令）。

每一轮都是带新 marker 的新请求，从不重贴旧消息：
- 文件通道：每轮覆盖写 <channel>/EXECUTOR_REASK_<caller8>_CURRENT.json（主管轮内也能读）；
- 显式发送：Codex 经原 thread native steer/start，Claude 经既有保护桥；未知旧次只保全。
- 持久调度：仅对显式认证登记的时段，每60秒更新文件通道，绝不调用传输或冒用前台身份。
答复必须精确匹配 caller/supervisor/task/episode/marker；无关 user turn 不算。
只有绑定答复、已认证的新派发或 operator stop 结束一段；单次 run 的轮询预算最多
540 秒，同步 I/O 可延长实际耗时。退出时段仍可等待。全局 Stop 仅观察，不启动或强迫循环；新派发出口依赖旧 idle
binding，而现役观察器不更新它。主管先写原 mailbox 答复，让显式等待返回，再做新鲜派发。
"""
import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import time
import uuid

import executor_ready as ready
import executor_reply as replies

INTERVAL = 60.0
POLL = 5.0
MAX_RUN_SECONDS = 540.0
MAX_TEXT = ready.prompt_reference.MAX_INLINE_BYTES  # 保守的 UTF-8 字节预算，非通用 UI 字数阈值。
HISTORY = 20
CHANNEL_SCAN = 64
CHANNEL_FILE_BYTES = 256 * 1024
WAITING, ANSWERED, CONSUMED, STOPPED = 'WAITING_REPLY', 'ANSWERED', 'CONSUMED', 'STOPPED'
HELD = ('QUEUED', 'COMPOSE')


def root():
    return ready.state_root() / 'executor-reask-v1'


def state_path(caller):
    return root() / (ready.digest(caller) + '.json')


def stop_file(caller):
    return root() / (ready.digest(caller) + '.stop.json')


def config_path():
    return root() / 'config.json'


def load(caller):
    return ready.read_json(state_path(caller))


def save(state):
    ready.write_json(state_path(state['caller_surface_uuid']), state)


class EpisodeBusy(BlockingIOError):
    """Only failure to acquire the episode lock, never an error in its body."""


@contextmanager
def episode_lock(caller):
    """Serialize explicit episode operations without waiting on another holder."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root() / (ready.digest(caller) + '.lock'),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EpisodeBusy('executor inquiry episode is busy') from exc
        yield


def inbox_path(state):
    return (ready.state_root() / 'supervisor-inbox-v1' / ready.digest(state['supervisor'])
            / (ready.digest(state['caller_surface_uuid']) + '.json'))


def idle_binding(caller, workspace=''):
    from cmux_executor_idle_guard import _binding_path, bindings_dir
    if workspace:
        body = ready.read_json(_binding_path(workspace, caller))
        return body if body and body.get('surface_uuid') == caller and body.get('workspace_uuid') == workspace else None
    # Legacy explicit episodes may lack workspace. Do not pick between matches.
    found = []
    try:
        for index, path in enumerate(bindings_dir().iterdir()):
            if index >= 256:
                return None
            body = ready.read_json(path)
            if body and body.get('surface_uuid') == caller:
                found.append(body)
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


def current_name(caller):
    return f'EXECUTOR_REASK_{caller[:8]}_CURRENT.json'


def configured_channels(supervisor):
    conf = ready.read_json(config_path()) or {}
    value = (conf.get('channels') or {}).get(supervisor, [])
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def start(caller, supervisor, task_id='', transcript='', channels=None, executor_ref='',
          now=None, reason='MANUAL', own_prefixes=(), workspace='', purpose='REQUEST_NEXT_TASK',
          resume_stopped=False, continuing=False, continuation=None):
    with episode_lock(caller):
        return start_locked(caller, supervisor, task_id, transcript, channels, executor_ref,
                            now, reason, own_prefixes, workspace, purpose, resume_stopped,
                            continuing, continuation)


def start_locked(caller, supervisor, task_id='', transcript='', channels=None, executor_ref='',
                 now=None, reason='MANUAL', own_prefixes=(), workspace='',
                 purpose='REQUEST_NEXT_TASK', resume_stopped=False, continuing=False,
                 continuation=None):
    """Local unscheduled API. The authenticated CLI uses scheduler.start()."""
    old = load(caller)
    state = prepare_start_locked(caller, supervisor, task_id, transcript, channels,
                                executor_ref, now, reason, own_prefixes, workspace,
                                purpose, resume_stopped, continuing, continuation)
    if state == old or state.get('state') != WAITING:
        return state
    if old:
        archive_episode(old)
    stop_file(caller).unlink(missing_ok=True)
    Path(state['mailbox']).mkdir(parents=True, exist_ok=True, mode=0o700)
    save(state)
    if old:
        last = old.get('asks', [])[-1]['marker'] if old.get('asks') else ''
        write_channels(old, last, 'superseded inquiry; original attempts remain held',
                       state['started_epoch'], status='SUPERSEDED')
    return state


def prepare_start_locked(caller, supervisor, task_id='', transcript='', channels=None,
                         executor_ref='', now=None, reason='MANUAL', own_prefixes=(),
                         workspace='', purpose='REQUEST_NEXT_TASK', resume_stopped=False,
                         continuing=False, continuation=None):
    """Only the same complete inquiry binding can reuse a waiting episode."""
    now = time.time() if now is None else now
    if purpose not in ('REQUEST_NEXT_TASK', 'PENDING_TASK_REPLY'):
        raise ValueError('unknown inquiry purpose')
    reservation_inquiries = (continuation or {}).get('reservation_inquiries', False)
    if type(reservation_inquiries) is not bool or reservation_inquiries and not continuing:
        raise ValueError('reservation inquiries require explicit continuing authorization')
    old = load(caller)
    binding = dict(caller_surface_uuid=caller, supervisor=supervisor, task_id=task_id,
                   workspace_uuid=workspace, purpose=purpose)
    if (old and all(old.get(key) == value for key, value in binding.items())
            and (old.get('state') == WAITING or old.get('continuation') and old.get('state') != STOPPED)):
        if continuing and not old.get('continuation'):
            raise ValueError('existing episode must close before enabling continuation')
        if (continuing and reservation_inquiries !=
                (old.get('continuation') or {}).get('reservation_inquiries', False)):
            raise ValueError('existing episode must close before changing reservation inquiry authorization')
        return old
    if (stop_file(caller).exists() or old and old.get('state') == STOPPED) and not resume_stopped:
        return old or dict(caller_surface_uuid=caller, state=STOPPED, ask_count=0)
    continuation = validate_continuation(continuation, now) if continuing else None
    if continuation and continuation['owner'] != supervisor:
        raise ValueError('continuation owner must be the authenticated supervisor')
    tail = None
    if transcript:
        try:
            tail = ready.Tail(transcript).position()
        except (OSError, ValueError):
            transcript = ''
    chans = list(channels) if channels else configured_channels(supervisor)
    from executor_reask_protocol import descriptor
    state = dict(version=3, controller=controller(), file_protocol=descriptor(),
                 caller_surface_uuid=caller, executor_ref=executor_ref or caller,
                 workspace_uuid=workspace, purpose=purpose,
                 supervisor=supervisor, task_id=task_id, state=WAITING, reason=reason,
                 started_epoch=now, next_due_epoch=now,
                 episode_id=ready.digest(json.dumps([binding, now, secrets.token_hex(8)])),
                 marker_secret=secrets.token_hex(16),
                 transcript=transcript, tail_position=tail, channels=chans,
                 mailbox=str(ready.mailbox_dir(caller)), ask_count=0, asks=[],
                 terminal_sends=0, terminal_attempts=0, native_receipts=0, file_posts=0,
                 check_count=0, attempt_count=0,
                 continuation=continuation, rearm_count=0, reply_history=[],
                 reply_detected_epoch=None, reply_consumed_epoch=None,
                 terminal_pending=copy.deepcopy(old.get('terminal_pending')) if old else None,
                 reply=None, own_prefixes=list(own_prefixes))
    # Restarting an inquiry does not release a formal task. Keep the original
    # pair's recorded reservations even if its marker is temporarily absent.
    if old and all(old.get(key) == binding[key] for key in (
            'caller_surface_uuid', 'supervisor', 'workspace_uuid')):
        state['paused_tasks'] = copy.deepcopy(old.get('paused_tasks', []))
    return state


def validate_continuation(value, now=None):
    """Pin explicit authorization independently of one inquiry or child task."""
    from executor_reask_lifecycle import validate_authorization
    return validate_authorization(value, controller(), time.time() if now is None else now)


def archive_episode(state):
    """One bounded snapshot per real transition, never an archive per poll."""
    path = root() / 'episodes' / ready.digest(state['caller_surface_uuid']) / (
        ready.digest(state['episode_id']) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        previous = ready.read_json(path)
        if not previous or previous.get('episode_id') != state['episode_id']:
            raise ValueError('episode archive identity mismatch')
        return path
    raw = json.dumps(state, ensure_ascii=False).encode()
    if len(raw) > ready.MAX_JSON_BYTES:
        raise ValueError('episode archive exceeds bounded state limit')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def snapshot_files(directory, files, identity):
    """Publish one complete immutable snapshot, preserving the original bytes."""
    from cmux_evidence_io import read_bytes
    directory = Path(directory)
    index_path = directory / 'INDEX.json'
    if directory.exists():
        index = ready.read_json(index_path)
        if not index or index.get('identity') != identity:
            raise ValueError('snapshot identity mismatch')
        members, absent = index.get('files'), index.get('absent')
        if (not isinstance(members, dict) or not isinstance(absent, list)
                or not all(isinstance(name, str) for name in absent)
                or len(absent) != len(set(absent)) or set(members).intersection(absent)):
            raise ValueError('invalid snapshot index')
        for name in (*members, *absent):
            if not name or Path(name).name != name or name == 'INDEX.json':
                raise ValueError('noncanonical snapshot member')
        for name in absent:
            if (directory / name).exists() or (directory / name).is_symlink():
                raise ValueError('absent snapshot member appeared')
        if files and set(files) != set(members).union(absent):
            raise ValueError('snapshot member set changed')
        for name, pin in index['files'].items():
            if (not isinstance(pin, dict) or set(pin) != {'bytes', 'sha256'}
                    or type(pin['bytes']) is not int or pin['bytes'] < 0):
                raise ValueError('invalid snapshot member pin')
            raw = read_bytes(directory / name, limit=ready.MAX_JSON_BYTES)
            if len(raw) != pin['bytes'] or hashlib.sha256(raw).hexdigest() != pin['sha256']:
                raise ValueError('snapshot content changed')
            if files and files[name] != raw:
                raise ValueError('snapshot differs from the supplied preimage')
        if files and any(files[name] is not None for name in absent):
            raise ValueError('snapshot preimage was not absent')
        return index
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=directory.parent))
    try:
        index = dict(identity=identity, files={}, absent=[])
        for name, raw in files.items():
            if Path(name).name != name or name == 'INDEX.json':
                raise ValueError('invalid snapshot member')
            if raw is None:
                index['absent'].append(name)
                continue
            if len(raw) > ready.MAX_JSON_BYTES:
                raise ValueError('snapshot member exceeds state limit')
            fd = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fchmod(handle.fileno(), 0o400)
                os.fsync(handle.fileno())
            index['files'][name] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        ready.write_json(temporary / 'INDEX.json', index)
        (temporary / 'INDEX.json').chmod(0o400)
        ready.sync_directory(temporary)
        os.rename(temporary, directory)
        ready.sync_directory(directory.parent)
        return index
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def preserve_legacy_snapshot(state, record=None):
    """Before CURRENT first writes old data, retain all history and pending IO.

    This is evidence preservation, not permission to interpret or execute an
    old controller. One snapshot per episode, never a copy on every minute.
    Call while holding the caller lock, before mutating the loaded state.
    """
    from cmux_evidence_io import read_bytes
    from executor_reask_protocol import state_controller
    if state.get('version') == 3 and state_controller(state, record) == controller():
        return None
    caller, episode = state['caller_surface_uuid'], state['episode_id']
    directory = root() / 'legacy-snapshots' / ready.digest(caller) / ready.digest(episode)
    identity = dict(schema='executor-reask-original-files-v1', caller=caller, episode=episode)
    if directory.exists():
        snapshot_files(directory, {}, identity)
        return directory
    raw = read_bytes(state_path(caller), limit=ready.MAX_JSON_BYTES)
    if json.loads(raw) != state:
        raise ValueError('snapshot requires the unchanged original live state')
    from executor_reask_scheduler import enrollment_path
    path = enrollment_path(caller)
    registration = read_bytes(path, limit=ready.MAX_JSON_BYTES) if path.exists() else None
    snapshot_files(directory, {'state.json': raw, 'enrollment.json': registration}, identity)
    return directory


def _recent_files(directory, since, limit=CHANNEL_SCAN):
    try:
        entries = []
        with os.scandir(directory) as stream:
            for index, entry in enumerate(stream):
                if index >= 256:
                    break
                if entry.is_file(follow_symlinks=False):
                    entries.append(entry)
    except OSError:
        return []
    fresh = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if st.st_mtime >= since and st.st_size <= CHANNEL_FILE_BYTES:
            fresh.append((st.st_mtime, Path(e.path)))
    return [p for _m, p in sorted(fresh, reverse=True)[:limit]]


def issue_marker(state, ordinal):
    """Bounded state can still authenticate a late reply to any issued inquiry."""
    signature = hmac.new(state['marker_secret'].encode(), str(ordinal).encode(),
                         hashlib.sha256).hexdigest()[:16]
    return f'{ready.PREFIX}_{ordinal:x}_{signature}'


def open_marker(state, marker):
    if not state.get('continuation'):
        return True
    try:
        ordinal = int(marker.rsplit('_', 2)[1], 16)
        return ordinal > state.get('replies_closed_through', 0) and issued_marker(state, marker)
    except (ValueError, TypeError, IndexError):
        return False


def reply_record(body, source, **extra):
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return dict(source=source, body=body, body_sha256=ready.digest(raw),
                marker=body['marker'], status=body['status'], text=raw[:800], **extra)


def valid_reply(state, body, markers, **binding):
    from executor_reask_lifecycle import valid_wait_contract
    return (replies.valid(body, markers, **binding)
            and open_marker(state, body.get('marker'))
            and (not state.get('continuation') or valid_wait_contract(body, state)))


def issued_marker(state, marker):
    if not isinstance(marker, str) or len(marker) > 160 or not state.get('marker_secret'):
        return False
    try:
        prefix, ordinal_hex, _signature = marker.rsplit('_', 2)
        ordinal = int(ordinal_hex, 16)
        return (prefix == ready.PREFIX and 0 < ordinal <= state.get('attempt_count', 0)
                and hmac.compare_digest(marker, issue_marker(state, ordinal)))
    except (ValueError, TypeError):
        return False


def mailbox_reply(state):
    """Direct canonical paths keep fresh and late replies live in large mailboxes.

    Recent history is checked every poll. Older HMAC ordinals are checked in
    rotating bounded batches; the episode persists the next position. Directory
    scans are only a legacy compatibility fallback, not the primary reply path.
    """
    caller = state['caller_surface_uuid']
    markers = [a['marker'] for a in state.get('asks', []) if isinstance(a, dict) and a.get('marker')]
    count = max(0, int(state.get('attempt_count', 0)))
    if count and state.get('marker_secret'):
        start = max(1, min(int(state.get('reply_scan_ordinal', 1)), count))
        ordinals = [1 + (start - 1 + offset) % count for offset in range(min(count, CHANNEL_SCAN))]
        markers.extend(issue_marker(state, ordinal) for ordinal in ordinals)
        state['reply_scan_ordinal'] = 1 + (start - 1 + len(ordinals)) % count
    binding = dict(caller=caller, supervisor=state['supervisor'],
                   task_id=state.get('task_id', ''), episode_id=state['episode_id'])
    for marker in dict.fromkeys(markers):
        # Old/custom markers still need a safe basename before a filesystem read.
        if (not isinstance(marker, str) or not marker or len(marker) > 160
                or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in marker)):
            continue
        path = ready.mailbox_dir(caller) / (marker + '.json')
        body = ready.read_json(path)
        if valid_reply(state, body, [marker], **binding):
            return reply_record(body, 'mailbox', path=str(path))
    return None


def find_reply(state, *, file_only=False):
    """主管答复的三种来源；本人写的 CURRENT 和本人文件不算。"""
    caller, since = state['caller_surface_uuid'], float(state['started_epoch'])
    markers = [a['marker'] for a in state.get('asks', []) if isinstance(a, dict) and a.get('marker')]
    pending = state.get('terminal_pending') or {}
    if pending.get('marker') and pending['marker'] not in markers:
        markers.append(pending['marker'])
    binding = dict(caller=caller, supervisor=state['supervisor'],
                   task_id=state.get('task_id', ''), episode_id=state['episode_id'])
    def matched(body):
        allowed = markers
        if isinstance(body, dict) and issued_marker(state, body.get('marker')):
            allowed = [body['marker']]
        return valid_reply(state, body, allowed, **binding)
    direct = mailbox_reply(state)
    if direct:
        return direct
    for path in _recent_files(state['mailbox'], since):
        body = ready.read_json(path)
        if matched(body):
            return reply_record(body, 'mailbox', path=str(path))
    own = tuple([current_name(caller), 'EXECUTOR_REASK_'] + list(state.get('own_prefixes', [])))
    for channel in state.get('channels', []):
        for path in _recent_files(channel, since):
            if path.name.startswith(own) or path.suffix not in ('.json', '.md', '.txt'):
                continue
            try:
                text = path.read_text(errors='replace')
            except OSError:
                continue
            body = replies.from_text(text)
            if matched(body):
                return reply_record(body, 'channel', path=str(path))
    if not file_only and state.get('transcript') and state.get('tail_position'):
        try:
            tail = ready.Tail(state['transcript'], state['tail_position'])
        except (OSError, ValueError) as exc:
            state['transcript_error'] = str(exc)[:200]
            return None
        for line in tail.lines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            texts = ready.user_texts(entry) if isinstance(entry, dict) else []
            for text in texts:
                body = replies.from_text(text)
                if matched(body):
                    state['tail_position'] = tail.position()
                    return reply_record(body, 'transcript')
        state['tail_position'] = tail.position()
    return None


def build_text(state, marker, attempt):
    mailbox = state['mailbox']
    reservations = state.get('paused_tasks', [])
    purpose = ('RESERVATION_STATUS' if reservations
               else state.get('purpose', 'REQUEST_NEXT_TASK'))
    action = (
        'Formal reservations remain active: ' + json.dumps(reservations, ensure_ascii=False)
        + '. Reply with task, dependency or callback-settlement status and the next expected action/time. '
        'This file inquiry neither dispatches work nor releases a reservation. '
        if reservations else
        'Reply to this inquiry or dispatch an authorized task; this does not repeat a callback. ')
    text = (f"{ready.PREFIX}|{state['executor_ref']}|{marker} ask #{attempt}"
            f" (every {int(INTERVAL)}s until you reply): executor "
            f"{state['caller_surface_uuid'][:8]} requests {purpose}"
            + (f" after {state['task_id']}" if state.get('task_id') else '')
            + '. ' + action
            + f"Write {mailbox}/{marker}.json with the exact bindings and a concrete trigger: "
            + json.dumps(replies.template(state, marker), ensure_ascii=False))
    # 全文不截断。write_record 会为超过字节预算的新请求固定正文并发送短通知；
    # 此处只规划校验，文件通道仍记录完整请求，旧 attempt 不迁移。
    ready.prompt_reference.plan(text, marker, ready.state_root() / 'message-bodies-v1')
    return text


def write_channels(state, marker, text, now, status=WAITING):
    written, errors = [], []
    paths = [inbox_path(state)] + [Path(c) / current_name(state['caller_surface_uuid'])
                                  for c in state.get('channels', [])]
    for path in dict.fromkeys(paths):
        try:
            if status == 'SUPERSEDED':
                previous = ready.read_json(path) or {}
                if (any(previous.get(key) != state.get(key) for key in
                        ('caller_surface_uuid', 'supervisor', 'episode_id'))
                        or previous.get('status') == 'SUPERSEDED'):
                    continue
            ready.write_json(path, dict(
                schema='executor-reask-current-v2', status=status, at_epoch=now,
                episode_id=state['episode_id'], workspace_uuid=state.get('workspace_uuid', ''),
                purpose=state.get('purpose', 'REQUEST_NEXT_TASK'),
                inquiry_kind=('RESERVATION_STATUS' if state.get('paused_tasks')
                              else state.get('purpose', 'REQUEST_NEXT_TASK')),
                reservations=copy.deepcopy(state.get('paused_tasks', [])),
                at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
                caller_surface_uuid=state['caller_surface_uuid'], supervisor=state['supervisor'],
                task_id=state.get('task_id', ''), ask_count=state['ask_count'], marker=marker,
                request=text, reply_mailbox=f"{state['mailbox']}/{marker}.json",
                reply_template=replies.template(state, marker),
                native_delivery_confirmed=False,
                terminal_inputs_this_round=0 if status != WAITING else None))
            written.append(str(path))
        except OSError as exc:
            errors.append(dict(path=str(path), error_type=type(exc).__name__))
    return dict(written=written, errors=errors)


def _unreceived_on_screen(bridge, supervisor, caller, ask, flat_screen):
    """历史 marker 无回执只表示未核实，不能据此宣布排队或发送成功。

    已原生收到的请求也会留在屏幕历史区；原次回执优先。
    未核实时保守只走文件通道，实际队列由同版 bridge 单独识别。
    """
    marker = ask.get('marker', '')
    if not marker or ask.get('outcome') == 'CONFIRMED':
        return False
    if marker not in flat_screen:
        return False
    if not origin_matches(ask, caller, supervisor):
        return True
    try:
        original = ready._original_request(caller, supervisor, marker)
        receipt = ready._receipt(bridge, supervisor, original['text'], marker)
    except Exception:
        return True                  # 无法核实时按仍待投递处理：宁可只走文件通道
    if receipt:
        ask.update(outcome='CONFIRMED', receipt=receipt)
        return False
    return True


def _original_no_input(bridge, supervisor, caller, ask):
    """Release only positively verified zero-input holds, never queued attempts."""
    try:
        if not origin_matches(ask, caller, supervisor):
            return False
        from cmux_message_journal import verified_no_input
        marker = ask['marker']
        original = ready._original_request(caller, supervisor, marker)
        proof = verified_no_input(bridge, supervisor, original['text'], marker)
        if (proof is None
                or proof['binding']['identity']['caller_surface_uuid'] != caller
                or ready._original_request(caller, supervisor, marker) != original):
            return False
        ask.update(outcome='NO_INPUT', zero_input_verified=True, no_input_proof=proof,
                   terminal=False, native_input_intent=False, paste_intent=False)
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return False


def _advance_original(bridge, supervisor, caller, ask):
    if not origin_matches(ask, caller, supervisor):
        return False
    if _original_no_input(bridge, supervisor, caller, ask):
        return True
    ready.advance_ask(bridge, supervisor, caller, ask)
    return ask.get('outcome') == 'CONFIRMED'


def controller():
    from executor_reask_protocol import FILES
    source = Path(__file__).resolve().parents[1]
    return dict(source=str(source), python=str(Path(sys.executable).resolve()),
                files={name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in FILES})


def origin(state):
    return dict(caller_surface_uuid=state['caller_surface_uuid'], supervisor=state['supervisor'],
                workspace_uuid=state.get('workspace_uuid', ''), task_id=state.get('task_id', ''),
                episode_id=state['episode_id'], controller=controller())


def origin_matches(ask, caller, supervisor):
    saved = ask.get('origin')
    if not (isinstance(saved, dict) and saved.get('caller_surface_uuid') == caller
            and saved.get('supervisor') == supervisor and saved.get('workspace_uuid')
            and saved.get('controller') == controller()):
        return False
    try:
        original = ready._original_request(caller, supervisor, ask['marker'])
        return all(original.get(key) == saved.get(key) for key in
                   ('workspace_uuid', 'task_id', 'episode_id', 'controller'))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _held(bridge, supervisor, asks, caller='', *, provider=None):
    """旧请求只读核收；新 Codex 原生输入不受 composer 或忙碌横幅限制。"""
    if provider == 'codex':
        for ask in asks:
            if (ask.get('terminal') or ask.get('native_input_intent')) and ask.get('outcome') != 'CONFIRMED':
                if not _advance_original(bridge, supervisor, caller, ask):
                    return 'UNVERIFIED', None
        return None, None
    screen = bridge.read_screen(supervisor, lines=120)
    flat = ''.join(screen.split())   # 长行折行会拆开 marker
    for ask in asks:
        marker = ask.get('marker', '')
        if not ask.get('terminal'):
            continue
        if bridge.pending_queue_holds(screen, marker):
            return 'QUEUED', screen
        if caller and _unreceived_on_screen(bridge, supervisor, caller, ask, flat):
            return 'UNVERIFIED', screen
    if not bridge.compose_block_is_empty(screen):
        return 'COMPOSE', screen
    if bridge.receiver_cannot_submit_now(screen):
        return 'BUSY', screen
    return None, screen


def detach_unrelated_pending(state, pending):
    """Preserve a prior task/recipient attempt without locking a new inquiry.

    Read the original exclusive request, not a screen claim or an inferred ACK.
    A controller change alone is insufficient. Same-task uncertainty remains held.
    Nothing here reconciles, sends, or changes the original request/journal.
    """
    caller = state['caller_surface_uuid']
    marker = pending.get('marker')
    if not isinstance(marker, str) or not marker:
        return False
    record = ready.read_json(ready.requests_dir() / (ready.request_key(caller, marker) + '.json'))
    if not isinstance(record, dict) or not record.get('target_surface'):
        return False
    try:
        original = ready._original_request(caller, record['target_surface'], marker)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if original != record or not isinstance(original.get('task_id'), str):
        return False
    if (original['target_surface'] == state['supervisor']
            and original['task_id'] == state.get('task_id', '')):
        return False
    evidence = dict(schema='unrelated-pending-preserved-v1',
                    original_pending=pending, original_request=original,
                    original_status_preserved=True, input_operations=0)
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()
    if len(raw) > ready.MAX_JSON_BYTES:
        raise ValueError('pending archive exceeds bounded state limit')
    path = root() / 'preserved-pending' / ready.digest(caller) / (hashlib.sha256(raw).hexdigest() + '.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if ready.read_json(path) != evidence:
            raise ValueError('preserved pending archive changed')
    else:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    # Archive durable before releasing the *new episode's* transport slot.
    state['preserved_pending'] = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                                      marker=marker, reason='DIFFERENT_TASK_OR_SUPERVISOR')
    state['terminal_pending'] = None
    save(state)
    return True


def _terminal_cadence(bridge, state):
    """Revalidate the last native receipt; file rounds have their own clock.

    Keep this original marker outside the rolling ask history. A cached epoch
    alone never authorizes another input, even after a restart or late receipt.
    """
    last = state.get('terminal_last_confirmed')
    if not last:
        last = next((ask for ask in reversed(state.get('asks', []))
                     if ask.get('outcome') == 'CONFIRMED'), None)
    if not last:
        return None
    caller, supervisor = state['caller_surface_uuid'], state['supervisor']
    if not origin_matches(last, caller, supervisor):
        return 'ORIGINAL_CONTROLLER_REQUIRED'
    if (not _advance_original(bridge, supervisor, caller, last)
            or last.get('outcome') != 'CONFIRMED'):
        return 'UNVERIFIED'
    received_at = (last.get('receipt') or {}).get('native_received_at_epoch')
    if type(received_at) not in (int, float) or not math.isfinite(received_at):
        return 'NATIVE_RECEIPT_TIME_REQUIRED'
    state['terminal_last_confirmed'] = dict(last)
    state['terminal_next_due_epoch'] = received_at + INTERVAL
    if time.time() < state['terminal_next_due_epoch']:
        return 'NATIVE_RECEIPT_INTERVAL'
    return None


def default_send(state, marker, text):
    """一次 journaled 发送；结果按 executor_ready 原样保留，不重试。"""
    bridge = ready._bridge()
    pending = state.get('terminal_pending') or {}
    if pending.get('marker') != marker and detach_unrelated_pending(state, pending):
        pending = {}
    if pending.get('marker') and pending['marker'] != marker:
        # An uncertain original attempt stays pinned even when the short history
        # rolls over. Reconciliation is read-only; no repaste or recovery key.
        if not origin_matches(pending, state['caller_surface_uuid'], state['supervisor']):
            return dict(outcome='SKIPPED_ORIGINAL_CONTROLLER_REQUIRED', terminal=False)
        if not _advance_original(bridge, state['supervisor'], state['caller_surface_uuid'], pending):
            return dict(outcome='SKIPPED_UNVERIFIED', terminal=False)
        state['terminal_pending'] = None
        if pending.get('outcome') == 'CONFIRMED':
            state['terminal_last_confirmed'] = dict(pending)
            state['native_receipts'] = state.get('native_receipts', 0) + 1
    from cmux_native_delivery import bind_target
    provider = bind_target(bridge, state['supervisor'], text)['provider']
    held, _screen = _held(bridge, state['supervisor'], state.get('asks', []),
                          state['caller_surface_uuid'], provider=provider)
    if held:
        return dict(outcome='SKIPPED_' + held, terminal=False)
    cadence = _terminal_cadence(bridge, state)
    if cadence:
        return dict(outcome='SKIPPED_' + cadence, terminal=False, input_operations=0,
                    terminal_next_due_epoch=state.get('terminal_next_due_epoch'))
    owner = origin(state)
    state['terminal_pending'] = dict(marker=marker, terminal=True, outcome='INTENT_UNRESOLVED', origin=owner)
    save(state)
    ready.write_record(state['caller_surface_uuid'], marker, text, state['supervisor'],
                       state.get('task_id', ''), state['episode_id'], controller=owner['controller'],
                       workspace=state.get('workspace_uuid', ''))
    ask = dict(marker=marker, origin=owner)
    ready.advance_ask(bridge, state['supervisor'], state['caller_surface_uuid'], ask,
                       allow_send=True)
    if ask.get('outcome') != 'CONFIRMED':
        _original_no_input(bridge, state['supervisor'], state['caller_surface_uuid'], ask)
    else:
        state['terminal_last_confirmed'] = dict(ask)
        received_at = (ask.get('receipt') or {}).get('native_received_at_epoch')
        if type(received_at) in (int, float) and math.isfinite(received_at):
            state['terminal_next_due_epoch'] = received_at + INTERVAL
    journal = ready.state_root() / 'message-dispatch-v1' / ready.request_key(state['caller_surface_uuid'], marker)
    from cmux_evidence_io import attempt_paths
    phases = []
    if journal.is_dir():
        for path in attempt_paths(journal):
            record = ready.read_json(path) or {}
            phases.extend(e.get('phase') for e in record.get('events', []) if isinstance(e, dict))
    result = dict(outcome=ask.get('outcome'), detail=ask.get('detail', ''), origin=owner,
                  terminal='PASTED' in phases, terminal_attempted=provider != 'codex',
                  native_input_intent='NATIVE_INPUT_INTENT' in phases,
                  transport='codex-native-input-v1' if provider == 'codex' else 'terminal',
                  paste_intent='PASTE_INTENT' in phases,
                  native_received=ask.get('outcome') == 'CONFIRMED')
    for key in ('confirmation_scope', 'body_read_confirmed', 'receipt',
                'details', 'dispatch_state'):
        if key in ask:
            result[key] = ask[key]
    if ask.get('zero_input_verified') is True:
        result.update(zero_input_verified=True, no_input_proof=ask['no_input_proof'])
    return result


def poll_locked(state, now, *, file_only=False):
    """No input: operator stop, new authenticated binding, then exact reply."""
    caller = state['caller_surface_uuid']
    if stop_file(caller).exists() and state['state'] != STOPPED:
        state.update(state=STOPPED, stopped_epoch=now)
        if state.get('continuation'):
            state['continuation']['status'] = 'STOPPED'
        last = state['asks'][-1]['marker'] if state['asks'] else ''
        write_channels(state, last, 'episode stopped by operator', now, status=STOPPED)
    from executor_reask_lifecycle import poll_authorization, observe_formal_tasks
    poll_authorization(state, now)
    observe_formal_tasks(state, now)
    if state['state'] != WAITING:
        return state
    # A reservation limits inquiries to explicitly authorized file status
    # requests. Reply observation also uses files only, so a legacy
    # idle binding cannot consume the inquiry or imply that the task released.
    files_only = file_only or bool(state.get('paused_tasks'))
    binding = {} if files_only or state.get('continuation') else idle_binding(caller, state.get('workspace_uuid', '')) or {}
    if (binding.get('state') == 'BOUND' and binding.get('supervisor_uuid') == state['supervisor']
            and binding.get('task_id') and binding['task_id'] != state.get('task_id')
            and float(binding.get('last_bound_epoch', 0)) > float(state['started_epoch'])):
        state.update(state=CONSUMED, exit_reason='AUTHENTICATED_NEW_DISPATCH',
                     new_task_id=binding['task_id'], completed_epoch=now)
        last = state['asks'][-1]['marker'] if state['asks'] else ''
        write_channels(state, last, 'authenticated new dispatch', now, status=CONSUMED)
        return state
    reply = find_reply(state, file_only=files_only)
    if reply:
        state.update(state=ANSWERED, reply=dict(reply, at_epoch=now), reply_detected_epoch=now)
    if state['state'] != WAITING:
        last = state['asks'][-1]['marker'] if state['asks'] else ''
        write_channels(state, last, 'episode closed', now, status=state['state'])
    return state


def tick(caller, now=None, send=default_send, *, file_only=False):
    try:
        with episode_lock(caller):
            return tick_locked(caller, now, send, file_only=file_only)
    except BlockingIOError:
        return dict(outcome='EPISODE_BUSY', state=WAITING)


def observe_boundaries_locked(state, now):
    """Honor revocation and formal ownership even for an unsupported producer.

    This grants no permission to interpret replies or invoke transport. Callers
    hold the episode lock; lifecycle preserves original evidence before writes.
    """
    from executor_reask_lifecycle import poll_authorization, observe_formal_tasks
    before = copy.deepcopy(state)
    poll_authorization(state, now)
    observe_formal_tasks(state, now)
    if state != before:
        save(state)
    return state


def tick_locked(caller, now=None, send=default_send, *, file_only=False, file_record=None,
                verification=None):
    realtime = now is None
    now = time.time() if realtime else now
    tick_start = time.monotonic()
    state = load(caller)
    if not state:
        return dict(outcome='NO_EPISODE')
    # An explicit stop is effective even if registration or compatibility is
    # incomplete. It grants no transport or reply-consumption authority.
    if stop_file(caller).exists():
        if state.get('state') != STOPPED:
            preserve_legacy_snapshot(state, file_record)
            state.update(state=STOPPED, stopped_epoch=now, exit_reason='OPERATOR_STOP')
            if state.get('continuation'):
                state['continuation']['status'] = 'STOPPED'
            marker = state['asks'][-1]['marker'] if state.get('asks') else ''
            write_channels(state, marker, 'episode stopped by operator', now, status=STOPPED)
            save(state)
        return state
    observe_boundaries_locked(state, now)
    if state.get('state') != WAITING:
        return state
    if (state.get('registration') or
            (state.get('continuation') or {}).get('reservation_inquiries') is True):
        from executor_reask_scheduler import registration_observation
        observation = registration_observation(state, verification=verification)
        if observation != 'REGISTERED':
            return dict(state, registration_observation=observation, input_operations=0)
    from executor_reask_protocol import state_controller, supports_state
    original = state_controller(state, file_record)
    if file_only:
        if not supports_state(state, file_record, verification=verification):
            return dict(outcome='UNSUPPORTED_FILE_PROTOCOL', input_operations=0)
        preserve_legacy_snapshot(state, file_record)
    else:
        invoked = controller()
        if original != invoked:
            from executor_reask_protocol import controller_diagnostic
            return dict(outcome='ORIGINAL_CONTROLLER_REQUIRED', input_operations=0,
                        controller_diagnostic=controller_diagnostic(original, invoked))
    state['check_count'] = state.get('check_count', 0) + 1
    poll_locked(state, now, file_only=file_only)
    poll_end = time.monotonic()
    if realtime:
        now = time.time()
    state['last_poll'] = dict(at_epoch=now, duration_seconds=max(0, poll_end - tick_start))
    save(state)
    if state['state'] != WAITING:
        return state
    if state.get('paused_tasks'):
        from executor_reask_lifecycle import reservation_inquiries_allowed
        if not reservation_inquiries_allowed(state, verification=verification):
            return state
        # Historical registration remains file-only. New explicit proactive
        # authority permits foreground status communication during reservations;
        # the scheduler still always calls with file_only=True.
        if not state['continuation'].get('proactive_hook', False):
            file_only = True
    last = state['asks'][-1] if state['asks'] else None
    due = float(state.get('next_due_epoch', float(last['at_epoch']) + INTERVAL if last else now))
    if now < due:
        save(state)
        return state
    attempt = state.get('attempt_count', state['ask_count']) + 1
    # Legacy waiting episodes gain a seed without replacing any original marker.
    state.setdefault('marker_secret', secrets.token_hex(16))
    marker = issue_marker(state, attempt)
    text = build_text(state, marker, attempt)
    state['attempt_count'] = attempt
    ask = dict(marker=marker, attempt=attempt, at_epoch=now, due_epoch=due,
               due_delay_seconds=max(0, now - due), text_sha256=ready.digest(text),
                origin=origin(state) if not file_only else None)
    if state.get('paused_tasks'):
        ask.update(inquiry_kind='RESERVATION_STATUS',
                   reservations=copy.deepcopy(state['paused_tasks']))
    state['asks'] = (state['asks'] + [ask])[-HISTORY:]
    state['next_due_epoch'] = now + INTERVAL  # no catch-up burst after a stalled runner
    if not file_only and not state.get('terminal_pending'):
        state['terminal_pending'] = dict(ask, terminal=True, outcome='INTENT_UNRESOLVED')
    save(state)                      # 任何输入之前先落盘新 marker
    channel_start = time.monotonic()
    ask['channels'] = write_channels(state, marker, text, now)
    channel_end = time.monotonic()
    ask['channel_duration_seconds'] = max(0, channel_end - channel_start)
    try:
        if file_only:
            ask.update(outcome='FILE_ONLY_SCHEDULED', terminal=False, input_operations=0)
        else:
            ask.update(send(state, marker, text))
    except Exception as exc:         # 终端失败不影响文件通道，也不重试同一轮
        ask.update(outcome='TERMINAL_ERROR', detail=f'{type(exc).__name__}: {exc}'[:400],
                   terminal=False)
    ask['send_duration_seconds'] = max(0, time.monotonic() - channel_end)
    ask['tick_duration_seconds'] = max(0, time.monotonic() - tick_start)
    if ask.get('terminal'):
        state['terminal_sends'] = state.get('terminal_sends', 0) + 1
    if ask.get('terminal_attempted'):
        state['terminal_attempts'] = state.get('terminal_attempts', 0) + 1
    if ask.get('native_received'):
        state['native_receipts'] = state.get('native_receipts', 0) + 1
    posted = bool(ask['channels']['written'])
    state['file_posts'] = state.get('file_posts', 0) + int(posted)
    state['ask_count'] += int(posted or bool(ask.get('terminal')) or bool(ask.get('native_received')))
    pending = state.get('terminal_pending') or {}
    if not file_only and pending.get('marker') == marker:
        zero_input = (ask.get('zero_input_verified') is True and _original_no_input(
            ready._bridge(), state['supervisor'], caller, ask))
        if (ask.get('native_received') or str(ask.get('outcome', '')).startswith('SKIPPED_')
                or zero_input):
            state['terminal_pending'] = None
        else:
            state['terminal_pending'] = dict(ask, terminal=True, origin=pending.get('origin') or ask.get('origin'))
    save(state)
    return state


def run(caller, max_seconds=MAX_RUN_SECONDS, poll=POLL, send=default_send,
        clock=time.time, sleep=time.sleep):
    deadline = clock() + max(1.0, min(float(max_seconds), MAX_RUN_SECONDS))
    state = tick(caller, clock(), send)
    while (state.get('state') == WAITING and not state.get('outcome')
           and (not state.get('paused_tasks')
                or (state.get('continuation') or {}).get('proactive_hook') is True)
           and not state.get('registration_observation') and clock() + poll < deadline):
        sleep(poll)
        state = tick(caller, clock(), send)
    # A formal reservation owns the next action. Keeping the model in a polling
    # tool makes a fresh preflight observe a busy executor forever. Return the
    # invocation without stopping/rearming the durable episode or its scheduler.
    # This is not an assertion that the terminal is idle or a task is accepted.
    reason = ('REPLY_REQUIRES_ORIGINAL_CONSUME' if state.get('state') == ANSWERED
              else 'FORMAL_RESERVATION_WAIT' if state.get('paused_tasks')
              and (state.get('continuation') or {}).get('proactive_hook') is not True
              else 'OBSERVATION_DEADLINE' if state.get('state') == WAITING
              and not state.get('outcome') and not state.get('registration_observation')
              else 'EPISODE_RETURNED')
    return dict(state, runner_returned=True, run_return_reason=reason,
                wait_boundary=dict(when='ONLY_SUPERVISOR_ACTION_REMAINS',
                                   next_action=('CONTINUE_AUTHORIZED_TASK_OR_BOUNDED_REASK'
                                       if (state.get('continuation') or {}).get('proactive_hook') is True
                                       and state.get('state') == WAITING
                                       else 'END_WAITING_TURN_NORMALLY'),
                                   idle_observed=False, goal_state_changed=False,
                                   fresh_preflight_required=True))


def stop(caller, reason='OPERATOR'):
    with episode_lock(caller):
        ready.write_json(stop_file(caller), dict(reason=reason, at_epoch=time.time()))
        return tick_locked(caller)


def summary(state, *, verification=None):
    if not isinstance(state, dict) or 'caller_surface_uuid' not in state:
        return state
    last = state['asks'][-1] if state.get('asks') else None
    from executor_reask_lifecycle import reservation_inquiries_allowed
    reservation_authority = reservation_inquiries_allowed(state, verification=verification)
    result = dict(outcome=state.get('registration_observation') or state.get('outcome') or state.get('state'),
                ask_count=state.get('ask_count', 0),
                check_count=state.get('check_count', 0), attempt_count=state.get('attempt_count', 0),
                file_posts=state.get('file_posts', 0), native_receipts=state.get('native_receipts', 0),
                terminal_attempts=state.get('terminal_attempts', 0),
                terminal_sends=state.get('terminal_sends', 0), last_ask=last,
                reply=state.get('reply'), supervisor=state.get('supervisor'),
                continuation=state.get('continuation'), paused_tasks=state.get('paused_tasks', []),
                exit_reason=state.get('exit_reason'),
                reservation_paused=bool(state.get('paused_tasks')),
                reservation_inquiries_authorized=reservation_authority,
                inquiries_paused=bool(state.get('paused_tasks')) and not reservation_authority,
                rearm_count=state.get('rearm_count', 0),
                reply_detected_epoch=state.get('reply_detected_epoch'),
                reply_consumed_epoch=state.get('reply_consumed_epoch'),
                next_ask_epoch=state.get('next_due_epoch'), last_poll=state.get('last_poll'),
                next_terminal_ask_epoch=state.get('terminal_next_due_epoch'),
                state_file=str(state_path(state['caller_surface_uuid'])))
    for key in ('registration_observation', 'registration_error_type', 'input_operations', 'intent',
                'consume_result', 'runner_returned', 'run_return_reason', 'wait_boundary'):
        if key in state:
            result[key] = state[key]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start')
    s.add_argument('--supervisor', required=True)
    s.add_argument('--task', default='')
    s.add_argument('--transcript', default='')
    s.add_argument('--channel', action='append', default=[])
    s.add_argument('--own-prefix', action='append', default=[])
    s.add_argument('--purpose', choices=['REQUEST_NEXT_TASK', 'PENDING_TASK_REPLY'], default='REQUEST_NEXT_TASK')
    s.add_argument('--continuing', action='store_true', help='explicitly authorized ongoing file inquiries')
    s.add_argument('--reservation-inquiries', action='store_true',
                   help='explicit authorization for file status inquiries during formal reservations; requires --continuing')
    s.add_argument('--proactive-hook', action='store_true',
                   help='enforce task-scoped Stop continuation and foreground status during reservations; requires continuing authorization')
    s.add_argument('--overall-task', default='')
    s.add_argument('--authorization-file', type=Path)
    s.add_argument('--authorization-until', type=float)
    s.add_argument('--resume-stopped', action='store_true', help='explicit operator resume only')
    s.add_argument('--file-only', action='store_true',
                   help='register and issue through files; pin own identity for advisory, no input')
    r = sub.add_parser('run')
    r.add_argument('--max-seconds', type=float, default=MAX_RUN_SECONDS)
    for name in ('tick', 'status'):
        sub.add_parser(name)
    consume_parser = sub.add_parser('consume')
    consume_parser.add_argument('--reply-sha256', default='')
    st = sub.add_parser('stop')
    st.add_argument('--reason', default='OPERATOR')
    e = sub.add_parser('enroll-existing', help='authenticate and register one existing episode; no tick or send')
    e.add_argument('--episode', required=True)
    e.add_argument('--enrollment-sha256', required=True,
                   help='exact original registration SHA256; ABSENT only if no registration exists')
    for p in sub.choices.values():
        p.add_argument('--caller-uuid', default=os.environ.get('CMUX_SURFACE_ID', ''))
    args = parser.parse_args(argv)
    if not args.caller_uuid:
        parser.error('--caller-uuid or CMUX_SURFACE_ID required')
    caller = args.caller_uuid
    if args.cmd == 'start':
        if args.proactive_hook and not (args.continuing and args.reservation_inquiries):
            parser.error('--proactive-hook requires --continuing --reservation-inquiries')
        if args.reservation_inquiries and not args.continuing:
            parser.error('--reservation-inquiries requires --continuing and its authorization file')
        if args.file_only and args.transcript:
            parser.error('--file-only cannot be combined with --transcript')
        live = ready._bridge().pin_workspace(args.supervisor, caller_uuid=caller)
        if live.get('caller_surface_uuid', '').upper() != caller.upper():
            raise ValueError('caller identity mismatch')
        # Persist UUIDs from the authenticated pair, never the user's surface:N
        # alias. Background file work has no authority to re-resolve aliases.
        caller = str(uuid.UUID(live['caller_surface_uuid'])).upper()
        supervisor = str(uuid.UUID(live['target_surface_uuid'])).upper()
        workspace = str(uuid.UUID(live['workspace_uuid'])).upper()
        continuation = None
        if args.continuing:
            continuation = dict(enabled=True, overall_task=args.overall_task,
                                authorization_file=str(args.authorization_file or ''),
                                owner=supervisor, until_epoch=args.authorization_until,
                                reservation_inquiries=args.reservation_inquiries,
                                proactive_hook=args.proactive_hook)
        import executor_reask_scheduler as scheduler
        state = scheduler.start(live, task_id=args.task, transcript=args.transcript,
                                authenticate_executor=True,
                                channels=args.channel, executor_ref=caller,
                                own_prefixes=args.own_prefix, purpose=args.purpose,
                                resume_stopped=args.resume_stopped,
                                continuing=args.continuing, continuation=continuation)
        if (state.get('state') == WAITING and not state.get('registration_observation')
                and scheduler.registered(state)):
            state = tick(caller, file_only=args.file_only)
    elif args.cmd == 'enroll-existing':
        import executor_reask_scheduler as scheduler
        original = load(caller) or {}
        if original.get('episode_id') != args.episode:
            raise ValueError('existing episode changed; reread its original evidence')
        live = ready._bridge().pin_workspace(original['supervisor'], caller_uuid=caller)
        state = scheduler.enroll_existing(live, args.episode, args.enrollment_sha256)
    elif args.cmd == 'run':
        state = run(caller, args.max_seconds)
    elif args.cmd == 'tick':
        state = tick(caller)
    elif args.cmd == 'stop':
        state = stop(caller, args.reason)
    elif args.cmd == 'consume':
        from executor_reask_lifecycle import consume
        state = consume(caller, args.reply_sha256)
    else:
        state = load(caller) or dict(outcome='NO_EPISODE')
        if state.get('registration'):
            from executor_reask_scheduler import registration_observation
            observation = registration_observation(state)
            if observation != 'REGISTERED':
                state = dict(state, registration_observation=observation, input_operations=0)
    result = summary(state)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if args.cmd == 'consume' and result.get('consume_result', {}).get('consumed') is not True:
        return 2
    return 2 if result.get('outcome') in {
        'REGISTRATION_UNCOMMITTED', 'COMMITTED_RECOVERY_REQUIRED',
        'ORIGINAL_CONTROLLER_REQUIRED', 'UNSUPPORTED_FILE_PROTOCOL', 'EPISODE_BUSY',
        'REGISTRY_BUSY',
    } else 0


if __name__ == '__main__':
    raise SystemExit(main())
