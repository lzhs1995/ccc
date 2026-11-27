#!/usr/bin/env python3
"""Finite executor idle observation and an explicit, single ready request.

`persist` only reads the original request, mailbox and bound transcript. It
does not send input. `ask` (`request` is the historical spelling) may prepare
and submit one request; all later invocations preserve its marker and use
read-only reconciliation. A received reply, dispatch, stop or finite budget
ends observation. Timeout is an idle lifecycle outcome, never task completion
or evidence of message delivery.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import cmux_prompt_reference as prompt_reference
import executor_reply as reply_contract

PREFIX = 'EXECUTOR_READY'
ASK_INTERVAL = 60.0  # 兼容旧参数：现在仅为原请求的只读核收间隔。
POLL_SECONDS = 5.0
DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 300.0
MAX_TICKS = 301
HEARTBEAT_MAX_AGE = 180.0
ASK_HISTORY = 20
MAX_JSON_BYTES = 1024 * 1024
MAX_TRANSCRIPT_BYTES = 256 * 1024
MAX_REQUEST_SCAN = 128
ASKING, ANSWERED, STOPPED = 'ASKING', 'ANSWERED', 'STOPPED'  # ASKING 仅用于识别旧进程。
WAITING_DEPENDENCY, TIMED_OUT = 'WAITING_DEPENDENCY', 'TIMED_OUT'
REPLY_HEAD = re.compile(r'^\s*(?:STATUS\s*:|TASK(?:[\s:|]|$)|BLOCKED\b|WAITING_DEPENDENCY\b|SOLO\b|ACK\b)')
REPLY_STATES = reply_contract.REPLY_STATES
SKIP_PREFIXES = ('This session is being continued', 'Stop hook feedback', '<command-',
                 '<local-command', '[Request interrupted', 'EXECUTOR_IDLE', PREFIX)


def state_root():
    return Path.home() / '.local/state/multi-agent-collaboration'


def idle_root():
    return state_root() / 'executor-idle-v1'


def requests_dir():
    return idle_root() / 'requests'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def request_key(caller_uuid, marker):
    return digest(json.dumps([caller_uuid, marker], sort_keys=True))


def loop_path(caller_uuid):
    return idle_root() / 'loops' / (digest(caller_uuid) + '.json')


def stop_path(caller_uuid):
    # 保留旧路径和字段，仍可停止升级前已运行的循环。
    return idle_root() / 'loops' / (digest(caller_uuid) + '.stop.json')


def mailbox_dir(caller_uuid):
    return idle_root() / 'mailbox' / digest(caller_uuid)[:16]


def _finite(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(value, maximum))


class WaitBudgetExpired(BaseException):
    """不能被 bridge 的普通错误重试吞掉的时间上限。"""


@contextmanager
def runtime_limit(seconds):
    """Bound blocking bridge calls too; subprocess.run unwinds and kills its child."""
    if threading.current_thread() is not threading.main_thread():
        raise ValueError('FINITE_WAIT_REQUIRES_MAIN_THREAD')
    seconds = _finite(seconds, DEFAULT_WAIT_SECONDS, 0.001, MAX_WAIT_SECONDS)
    old_handler = signal.getsignal(signal.SIGALRM)
    old_delay, old_repeat = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise WaitBudgetExpired()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_delay) if old_delay > 0 else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_delay > 0:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.001, old_delay - (time.monotonic() - started)), old_repeat)


def read_json(path):
    """固定大小、普通文件读取；坏状态不得授权新的输入。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
                return None
            data = handle.read(MAX_JSON_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (len(data) > MAX_JSON_BYTES or
                (before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_ino, after.st_size, after.st_mtime_ns)):
            return None
        value = json.loads(data)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def sync_directory(path):
    """Persist a rename/unlink; a file fsync alone does not persist its name."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.' + path.name + '.' + secrets.token_hex(4) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        # Exceptions here are AFTER the visible replacement. Transaction
        # callers inspect their commit token, never roll back on this error.
        sync_directory(path.parent)
    finally:
        # A deadline or write failure keeps the original state and cannot
        # accumulate temporary files on the next scheduled sweep.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def build_text(executor_ref, marker, task_id='', note='', attempt=1, mailbox='', caller_uuid='',
               episode_id='', supervisor=''):
    text = (f'{PREFIX}|{executor_ref}|{marker} ask #{attempt}: previous task '
            f'{task_id or "(unknown)"} is no longer armed; this executor is idle. '
            'Please review this single request and dispatch the next task, or reply '
            'WAITING_DEPENDENCY or SOLO with its trigger condition. The executor turn '
            'may end while waiting; this request will not be repeated automatically.')
    if mailbox:
        reply = dict(marker=marker, caller_surface_uuid=caller_uuid or executor_ref,
                     supervisor_uuid=supervisor,
                     task_id=task_id, episode_id=episode_id, status=WAITING_DEPENDENCY,
                     trigger='REPLACE_WITH_CONCRETE_TRIGGER')
        text += (f' Reply here, or write {mailbox}/{marker}.json containing '
                 + json.dumps(reply, ensure_ascii=False) + '.')
    note = ' '.join(str(note).split())
    return text + (' ' + note if note else '')


def write_record(caller_uuid, marker, text, target_surface, task_id='', episode_id='', *,
                 controller=None, workspace=''):
    path = requests_dir() / (request_key(caller_uuid, marker) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared = prompt_reference.plan(text, marker, state_root() / 'message-bodies-v1')
    prompt_reference.persist(prepared['reference'], text)
    record = dict(version=4, marker=marker, caller_surface_uuid=caller_uuid,
                  target_surface=target_surface, task_id=task_id, episode_id=episode_id,
                  payload_sha256=digest(prepared['text']), text=prepared['text'], created_at_epoch=time.time())
    if prepared['reference']:
        record.update(body_reference=prepared['reference'], original_payload_sha256=digest(text))
    if controller is not None:
        record['controller'] = controller
        record['workspace_uuid'] = workspace
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(record, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    return path, record


def process_command(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return ''
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True,
                             text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else ''
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        return ''


def pid_runs_loop(pid, argv=''):
    argv = ' '.join(str(argv).split())
    return bool(argv) and argv == ' '.join(process_command(pid).split())


def loop_live(record, now=None):
    if (not isinstance(record, dict) or record.get('state') != WAITING_DEPENDENCY
            or record.get('monitoring') is not True):
        return False
    now = time.time() if now is None else now
    try:
        age = now - float(record['heartbeat_epoch'])
        duration = float(record['deadline_epoch']) - float(record['started_epoch'])
        remaining = float(record['deadline_epoch']) - now
        poll = float(record['poll_seconds'])
        if not all(math.isfinite(v) for v in (age, duration, remaining, poll)):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return (0 <= age <= HEARTBEAT_MAX_AGE and 0 < duration <= MAX_WAIT_SECONDS
            and 0 < remaining <= MAX_WAIT_SECONDS and 1 <= poll <= 60
            and pid_runs_loop(record.get('pid'), record.get('argv', '')))


class Tail:
    """Read a bounded number of appended bytes from the same transcript inode."""

    def __init__(self, path, position=None):
        self.path = Path(path) if path else None
        st = self.path.stat() if self.path else None
        if st and not stat.S_ISREG(st.st_mode):
            raise ValueError('REPLY_TRANSCRIPT_NOT_REGULAR')
        self.identity = [st.st_dev, st.st_ino] if st else None
        self.offset = st.st_size if st else 0
        if position is not None:
            if (not isinstance(position, dict) or position.get('identity') != self.identity
                    or type(position.get('offset')) is not int
                    or not 0 <= position['offset'] <= self.offset):
                raise ValueError('REPLY_TRANSCRIPT_CHANGED: preserve idle episode')
            self.offset = position['offset']

    def position(self):
        return dict(identity=self.identity, offset=self.offset)

    def lines(self):
        if not self.path:
            return []
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            st = os.fstat(handle.fileno())
            if (not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] != self.identity
                    or st.st_size < self.offset):
                raise ValueError('REPLY_TRANSCRIPT_CHANGED')
            handle.seek(self.offset)
            data = handle.read(MAX_TRANSCRIPT_BYTES)
        boundary = data.rfind(b'\n') + 1
        if not boundary and len(data) == MAX_TRANSCRIPT_BYTES:
            raise ValueError('REPLY_RECORD_EXCEEDS_READ_BUDGET')
        self.offset += boundary
        return data[:boundary].splitlines()


def user_texts(entry):
    """只有真实 user 事件算入站；queued_command、工具结果及 hook 均不算。"""
    if entry.get('type') != 'user' or entry.get('isMeta'):
        return []
    attachment = entry.get('attachment')
    if isinstance(attachment, dict) and attachment.get('type') == 'queued_command':
        return []
    message = entry.get('message')
    if not isinstance(message, dict) or message.get('role', 'user') != 'user':
        return []
    content = message.get('content')
    texts = ([content] if isinstance(content, str) else
             [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
             if isinstance(content, list) else [])
    return [t for t in texts if isinstance(t, str) and t
            and not t.lstrip().startswith(SKIP_PREFIXES) and '<system-reminder>' not in t]


def looks_like_reply(text, markers, **binding):
    return reply_contract.valid(reply_contract.from_text(text), markers, **binding)


def find_reply(mailbox, tail, markers, since_epoch, caller_uuid='', task_id='', episode_id='', supervisor=''):
    markers = [m for m in markers[-ASK_HISTORY:] if isinstance(m, str)
               and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', m)]
    if mailbox:
        mailbox = Path(mailbox)
        for name in [m + '.json' for m in markers] + ['reply.json']:
            path = mailbox / name
            try:
                if path.stat().st_mtime < since_epoch:
                    continue
            except OSError:
                continue
            body = read_json(path)
            if not reply_contract.valid(body, markers, caller=caller_uuid,
                                        task_id=task_id, episode_id=episode_id, supervisor=supervisor):
                continue
            return dict(source='mailbox', path=str(path), status=body['status'],
                        body_sha256=digest(json.dumps(body, sort_keys=True)),
                        text=json.dumps(body, ensure_ascii=False)[:800], at_epoch=time.time())
    for line in tail.lines() if tail else []:
        if b'"user"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            for text in user_texts(entry):
                if looks_like_reply(text, markers, caller=caller_uuid,
                                    task_id=task_id, episode_id=episode_id, supervisor=supervisor):
                    return dict(source='transcript', status=reply_contract.from_text(text)['status'], text=text[:800],
                                line_sha256=hashlib.sha256(line).hexdigest(),
                                position=tail.position(), at_epoch=time.time())
    return None


def _bridge():
    import cmux_bridge
    return cmux_bridge


def _receipt(bridge, supervisor, text, marker):
    from cmux_message_journal import verified_receipt
    return verified_receipt(bridge, supervisor, text, marker)


def _unconfirmed(exc):
    """Retain the bridge's original diagnostics without granting another send."""
    original = getattr(exc, 'details', None)
    details = dict(original) if isinstance(original, dict) else {}
    details.setdefault('reconcile_guidance', dict(
        mode='--reconcile-only', input_operations=0, reuse_original_parameters=True,
        instruction='Preserve the original request, controller, target and marker. '
                    'Observe an existing attempt only; do not resend or recover input.'))
    result = dict(outcome='UNCONFIRMED_DO_NOT_RESEND', detail=str(exc)[:400], details=details)
    if getattr(exc, 'state', None) is not None:
        result['dispatch_state'] = exc.state
    return result


def _submit(bridge, supervisor, text, marker, reconcile_only=False, recover_stranded=False):
    if recover_stranded:
        return dict(outcome='UNCONFIRMED_DO_NOT_RESEND', detail='AUTOMATIC_RECOVERY_DISABLED')
    try:
        receipt = _receipt(bridge, supervisor, text, marker)
        if receipt:
            return dict(outcome='CONFIRMED', receipt=receipt)
        result = bridge.submit_text(supervisor, text, marker=marker,
                                    reconcile_only=reconcile_only, recover_stranded=False)
        receipt = _receipt(bridge, supervisor, text, marker)
        if receipt:
            return dict(outcome='CONFIRMED', receipt=receipt, result=result)
        return dict(outcome='UNCONFIRMED_DO_NOT_RESEND', detail='NATIVE_RECEIPT_REQUIRED')
    except Exception as exc:
        return _unconfirmed(exc)


def prepare_ask(bridge, supervisor, caller_uuid, executor_ref, task_id='', note='', attempt=1,
                mailbox='', clock=time.time, episode_id=''):
    marker = f'{PREFIX}_{secrets.token_hex(8)}'
    text = build_text(executor_ref, marker, task_id, note, attempt, mailbox, caller_uuid,
                      episode_id, supervisor)
    if bridge._looks_like_task_dispatch(text):
        raise RuntimeError('ready request must never look like a task dispatch')
    path, _record = write_record(caller_uuid, marker, text, supervisor, task_id, episode_id)
    return dict(marker=marker, attempt=attempt, outcome='PREPARED', at_epoch=clock(), record=str(path))


def _original_request(caller_uuid, supervisor, marker):
    path = requests_dir() / (request_key(caller_uuid, marker) + '.json')
    original = read_json(path)
    if (not original or original.get('caller_surface_uuid') != caller_uuid
            or original.get('target_surface') != supervisor or original.get('marker') != marker
            or not isinstance(original.get('text'), str)
            or digest(original['text']) != original.get('payload_sha256')):
        raise ValueError('ORIGINAL_REQUEST_REQUIRED: preserve marker; no new request')
    if original.get('version') == 4 and original.get('body_reference'):
        full, _pin = prompt_reference.read_body(original['body_reference'])
        planned = prompt_reference.plan(full, marker, state_root() / 'message-bodies-v1')
        if (digest(full) != original.get('original_payload_sha256')
                or planned != dict(text=original['text'], reference=original['body_reference'])):
            raise ValueError('ORIGINAL_REFERENCE_CHANGED: no input')
    elif original.get('version') == 4:
        prompt_reference.require_inline(original['text'])
    return original


def advance_ask(bridge, supervisor, caller_uuid, ask, *, allow_send=False):
    """默认只读；只有本调用刚创建的显式请求可获得一次发送机会。"""
    try:
        marker = ask['marker']
        original = _original_request(caller_uuid, supervisor, marker)
        live = bridge.pin_workspace(supervisor, caller_uuid=caller_uuid)
        if str(live.get('caller_surface_uuid', '')).upper() != caller_uuid.upper():
            raise ValueError('REQUEST_CALLER_CHANGED')
        receipt = _receipt(bridge, supervisor, original['text'], marker)
        if receipt:
            if _original_request(caller_uuid, supervisor, marker) != original:
                raise ValueError('ORIGINAL_REQUEST_CHANGED')
            ask.update(outcome='CONFIRMED', receipt=receipt)
            if original.get('body_reference'):
                ask.update(confirmation_scope='reference_notice', body_read_confirmed=False)
            return ask
        journal = state_root() / 'message-dispatch-v1' / request_key(live['caller_surface_uuid'], marker)
        attempts = []
        if journal.is_dir():
            for index, entry in enumerate(journal.iterdir()):
                if index >= MAX_REQUEST_SCAN:
                    raise ValueError('ORIGINAL_ATTEMPT_SCAN_BUDGET_EXHAUSTED')
                if entry.name.startswith('attempt-') and entry.suffix == '.json':
                    attempts.append(entry)
                    if len(attempts) > 2:
                        raise ValueError('ORIGINAL_ATTEMPT_SCAN_BUDGET_EXHAUSTED')
        if attempts and not all(read_json(p) for p in attempts):
            raise ValueError('ORIGINAL_ATTEMPT_UNREADABLE')
        if allow_send and not attempts and original.get('version') == 4:
            result = _submit(bridge, supervisor, original['text'], marker)
        elif attempts:
            result = _submit(bridge, supervisor, original['text'], marker, reconcile_only=True)
        else:
            raise ValueError('LEGACY_REQUEST_UNVERIFIABLE: no original attempt; do not resend')
        if _original_request(caller_uuid, supervisor, marker) != original:
            raise ValueError('ORIGINAL_REQUEST_CHANGED')
        ask.update(result)
        if original.get('body_reference'):
            ask.update(confirmation_scope='reference_notice', body_read_confirmed=False)
    except Exception as exc:
        ask.update(_unconfirmed(exc))
    return ask


def _orphan_request(caller_uuid, supervisor, task_id):
    """恢复写请求后、写 loop 前崩溃的窗口；不读无限历史，不发送输入。"""
    if not requests_dir().is_dir():
        return None
    found = []
    for index, path in enumerate(requests_dir().iterdir()):
        if index >= MAX_REQUEST_SCAN:
            raise ValueError('REQUEST_SCAN_BUDGET_EXHAUSTED: preserve existing requests')
        if path.suffix != '.json':
            continue
        record = read_json(path)
        if not record or not isinstance(record.get('caller_surface_uuid'), str):
            raise ValueError('REQUEST_RECORD_UNREADABLE: do not create another request')
        if record.get('caller_surface_uuid') != caller_uuid:
            continue
        if record.get('target_surface') != supervisor or record.get('task_id', '') != task_id:
            raise ValueError('UNRESOLVED_IDLE_EPISODE: original request binding differs')
        found.append(dict(marker=record.get('marker'), attempt=1, record=str(path),
                          outcome='UNCONFIRMED_DO_NOT_RESEND',
                          at_epoch=record.get('created_at_epoch', 0)))
    if len(found) > 1:
        raise ValueError('MULTIPLE_ORIGINAL_REQUESTS: explicit reconciliation required')
    return found[0] if found else None


def _stop_request(caller_uuid):
    path = stop_path(caller_uuid)
    value = read_json(path)
    if os.path.lexists(path) and (not value or not isinstance(value.get('reason'), str)):
        raise ValueError('STOP_RECORD_UNREADABLE: preserve stop signal')
    return value


def run_loop(bridge, supervisor, caller_uuid, executor_ref='', task_id='', note='',
             transcript='', mailbox=None, interval=ASK_INTERVAL, poll=POLL_SECONDS,
             max_ticks=None, clock=time.time, sleep=time.sleep, *,
             max_seconds=DEFAULT_WAIT_SECONDS, send_once=False, one_shot=False):
    """Observe one durable episode within a deadline; never periodically re-ask."""
    seconds = _finite(max_seconds, DEFAULT_WAIT_SECONDS, 0.001, MAX_WAIT_SECONDS)
    interval = _finite(interval, ASK_INTERVAL, 1, ASK_INTERVAL)
    poll = _finite(poll, POLL_SECONDS, 1, 60)
    tick_limit = int(_finite(max_ticks, MAX_TICKS, 1, MAX_TICKS))
    path, started = loop_path(caller_uuid), clock()
    invocation_started, monotonic_started = started, time.monotonic()
    previous = read_json(path)
    if os.path.lexists(path) and previous is None:
        raise ValueError('UNREADABLE_IDLE_EPISODE: preserve original record')
    mailbox = Path(mailbox) if mailbox else mailbox_dir(caller_uuid)
    mailbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = dict(version=3, pid=os.getpid(), argv=' '.join(sys.argv),
                  caller_surface_uuid=caller_uuid, supervisor=supervisor,
                  executor_ref=executor_ref or caller_uuid, task_id=task_id,
                  mailbox=str(mailbox), transcript=str(transcript or ''),
                  interval_seconds=interval, poll_seconds=poll, started_epoch=started,
                  deadline_epoch=started + seconds, budget_seconds=seconds,
                  heartbeat_epoch=started, state=WAITING_DEPENDENCY,
                  lifecycle_state=WAITING_DEPENDENCY, monitoring=True,
                  ask_count=0, asks=[], reply=None, stop=None, last_error=None, ticks_total=0,
                  elapsed_wait_seconds=0,
                  episode_id=digest(json.dumps([caller_uuid, supervisor, task_id, started])))
    if previous:
        if any(previous.get(k, '') != record.get(k, '') for k in
               ('caller_surface_uuid', 'supervisor', 'task_id', 'transcript')):
            raise ValueError('UNRESOLVED_IDLE_EPISODE: preserve original binding')
        record.update(previous)
        record.update(version=3, pid=os.getpid(), argv=' '.join(sys.argv), monitoring=True,
                      interval_seconds=interval, poll_seconds=poll)
        started = float(record['started_epoch'])
        deadline = _finite(record.get('deadline_epoch', started + seconds), started + seconds,
                           started, started + MAX_WAIT_SECONDS)
        record['deadline_epoch'] = min(deadline, started + seconds)
        record['budget_seconds'] = record['deadline_epoch'] - started
        if record['state'] == ASKING:
            record['state'] = WAITING_DEPENDENCY
    if not isinstance(record.get('asks'), list) or len(record['asks']) > ASK_HISTORY:
        raise ValueError('ORIGINAL_REQUEST_HISTORY_INVALID')
    elapsed_before = _finite(record.get('elapsed_wait_seconds', 0), MAX_WAIT_SECONDS,
                             0, MAX_WAIT_SECONDS)
    available = min(record['deadline_epoch'] - clock(), record['budget_seconds'] - elapsed_before)
    # 预算已结束时只容许一次有限的迟到回复/停止文件读取，不恢复等待。
    operation_seconds = min(seconds, available) if available > 0 else min(seconds, 1.0)
    write_json(path, record)
    tail, ticks = None, 0
    try:
        with runtime_limit(operation_seconds):
            stop = _stop_request(caller_uuid)
            if stop is not None:
                record.update(state=STOPPED, stop=stop, exit_reason='STOP_SIGNAL')
                return record
            if transcript:
                tail = Tail(transcript, record.get('tail_position'))
                record['tail_position'] = tail.position()
                write_json(path, record)
            if not record['asks']:
                orphan = _orphan_request(caller_uuid, supervisor, task_id)
                if orphan:
                    record.update(asks=[orphan], ask_count=1)
                    write_json(path, record)
            while ticks < tick_limit:
                now = clock()
                record['heartbeat_epoch'] = now
                stop = _stop_request(caller_uuid)
                if stop is not None:
                    record.update(state=STOPPED, stop=stop, exit_reason='STOP_SIGNAL')
                    break
                markers = [a.get('marker', '') for a in record['asks'] if isinstance(a, dict)]
                reply = find_reply(mailbox, tail, markers, started, caller_uuid, task_id,
                                   record['episode_id'], supervisor)
                if reply:
                    record.update(state=ANSWERED, reply=reply, exit_reason='REPLY_RECEIVED')
                    break
                if record['state'] in (ANSWERED, STOPPED):
                    break
                elapsed = max(0, now - invocation_started, time.monotonic() - monotonic_started)
                if (record['state'] == TIMED_OUT or now >= record['deadline_epoch']
                        or elapsed_before + elapsed >= record['budget_seconds']
                        or int(record['ticks_total']) >= MAX_TICKS):
                    record.update(state=TIMED_OUT, exit_reason='WAIT_BUDGET_EXHAUSTED')
                    break
                last = record['asks'][-1] if record['asks'] else None
                if last is None and send_once:
                    request = prepare_ask(bridge, supervisor, caller_uuid, record['executor_ref'],
                                          task_id, note, 1, str(mailbox), clock,
                                          episode_id=record['episode_id'])
                    record.update(ask_count=1, asks=[request])
                    write_json(path, record)  # 任何输入之前已持久化原 marker。
                    advance_ask(bridge, supervisor, caller_uuid, request, allow_send=True)
                elif last and now - float(last.get('reconciled_epoch', 0)) >= interval:
                    last['reconciled_epoch'] = now
                    write_json(path, record)
                    advance_ask(bridge, supervisor, caller_uuid, last)
                ticks += 1
                record['ticks_total'] = int(record['ticks_total']) + 1
                record['elapsed_wait_seconds'] = elapsed_before + max(
                    0, clock() - invocation_started, time.monotonic() - monotonic_started)
                if tail:
                    record['tail_position'] = tail.position()
                write_json(path, record)
                if one_shot:
                    record['exit_reason'] = 'SINGLE_PASS'
                    break
                if ticks >= tick_limit:
                    record.update(state=TIMED_OUT, exit_reason='TICK_BUDGET_EXHAUSTED')
                    break
                remaining = min(record['deadline_epoch'] - clock(),
                                record['budget_seconds'] - record['elapsed_wait_seconds'])
                if remaining <= 0:
                    record.update(state=TIMED_OUT, exit_reason='WAIT_BUDGET_EXHAUSTED')
                    break
                sleep(min(poll, remaining))
    except WaitBudgetExpired:
        record.update(state=TIMED_OUT, exit_reason='WALL_CLOCK_BUDGET_EXHAUSTED')
    except Exception as exc:
        # 迟到观察失败不能把已结束的预算/回复/停止重新改成等待。
        state = record['state'] if record['state'] in (TIMED_OUT, ANSWERED, STOPPED) else WAITING_DEPENDENCY
        record.update(state=state, exit_reason='OBSERVATION_BLOCKED',
                      last_error=(type(exc).__name__ + ': ' + str(exc))[:400])
    finally:
        if tail:
            record['tail_position'] = tail.position()
        record['elapsed_wait_seconds'] = elapsed_before + max(
            0, clock() - invocation_started, time.monotonic() - monotonic_started)
        record.update(monitoring=False, heartbeat_epoch=clock(), ended_epoch=clock())
        write_json(path, record)
    return record


def persist(supervisor, caller_uuid=None, task_id='', note='', transcript='', mailbox='',
            interval=ASK_INTERVAL, poll=POLL_SECONDS, max_ticks=None, *,
            max_seconds=DEFAULT_WAIT_SECONDS, send_once=False, one_shot=False):
    seconds = _finite(max_seconds, DEFAULT_WAIT_SECONDS, 0.001, MAX_WAIT_SECONDS)
    try:
        with runtime_limit(seconds):
            bridge = _bridge()
            caller = caller_uuid or bridge.pin_workspace(supervisor)['caller_surface_uuid']
            lock_path = loop_path(caller).with_suffix('.lock')
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with lock_path.open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return dict(outcome='ALREADY_RUNNING', record=str(loop_path(caller)))
                existing = read_json(loop_path(caller))
                if (existing and existing.get('pid') != os.getpid()
                        and (loop_live(existing) or (existing.get('state') == ASKING
                             and pid_runs_loop(existing.get('pid'), existing.get('argv', ''))))):
                    return dict(outcome='ALREADY_RUNNING', pid=existing.get('pid'),
                                record=str(loop_path(caller)), stop_file=str(stop_path(caller)))
                record = run_loop(bridge, supervisor, caller,
                                  os.environ.get('CMUX_SURFACE_ID') or caller,
                                  task_id, note, transcript, mailbox or None, interval, poll,
                                  max_ticks, max_seconds=seconds, send_once=send_once,
                                  one_shot=one_shot)
            return dict(outcome=record['state'], lifecycle_state=record['lifecycle_state'],
                        asks=record['ask_count'], reply=record.get('reply'),
                        last_ask=(record.get('asks') or [None])[-1],
                        stop=record.get('stop'), exit_reason=record.get('exit_reason'),
                        record=str(loop_path(caller)))
    except WaitBudgetExpired:
        outcome = dict(outcome=TIMED_OUT, lifecycle_state=WAITING_DEPENDENCY,
                       reason='IDLE_OPERATION_BUDGET_EXHAUSTED',
                       caller_surface_uuid=caller_uuid, supervisor=supervisor, task_id=task_id)
        path = idle_root() / 'outcomes' / (request_key(caller_uuid or '', supervisor) + '.json')
        write_json(path, outcome)
        return dict(outcome, record=str(path))


def request(supervisor, task_id='', note='', **kwargs):
    return persist(supervisor, task_id=task_id, note=note, send_once=True, one_shot=True,
                   max_ticks=1, **kwargs)


def reconcile(supervisor, marker):
    try:
        with runtime_limit(DEFAULT_WAIT_SECONDS):
            bridge = _bridge()
            caller = bridge.pin_workspace(supervisor)['caller_surface_uuid']
            return advance_ask(bridge, supervisor, caller, dict(marker=marker))
    except WaitBudgetExpired:
        return dict(marker=marker, outcome='UNCONFIRMED_DO_NOT_RESEND',
                    lifecycle_state=TIMED_OUT, detail='RECONCILE_BUDGET_EXHAUSTED')


def stop_loop(caller_uuid, reason='OPERATOR'):
    previous = read_json(stop_path(caller_uuid))
    if not previous or previous.get('reason') != reason:
        write_json(stop_path(caller_uuid), dict(reason=reason, requested_at_epoch=time.time()))
    return dict(outcome='STOP_REQUESTED', reason=reason, caller_surface_uuid=caller_uuid)


def status(caller_uuid):
    record = read_json(loop_path(caller_uuid))
    if not record:
        return dict(outcome='NO_LOOP_RECORD', caller_surface_uuid=caller_uuid)
    return dict(outcome=record.get('state'), live=loop_live(record), pid=record.get('pid'),
                asks=record.get('ask_count'), deadline_epoch=record.get('deadline_epoch'),
                interval_seconds=record.get('interval_seconds'),
                last_ask=(record.get('asks') or [None])[-1], reply=record.get('reply'),
                stop=record.get('stop'), last_error=record.get('last_error'),
                record=str(loop_path(caller_uuid)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    req = sub.add_parser('request', aliases=['ask'])
    req.add_argument('--supervisor', required=True)
    req.add_argument('--task', default='')
    req.add_argument('--note', default='')
    req.add_argument('--caller-uuid', default='')
    req.add_argument('--transcript', default='')
    req.add_argument('--max-seconds', type=float, default=DEFAULT_WAIT_SECONDS)
    rec = sub.add_parser('reconcile')
    rec.add_argument('--supervisor', required=True)
    rec.add_argument('--marker', required=True)
    per = sub.add_parser('persist')
    per.add_argument('--supervisor', required=True)
    per.add_argument('--caller-uuid', default='')
    per.add_argument('--task', default='')
    per.add_argument('--note', default='')
    per.add_argument('--transcript', default='')
    per.add_argument('--mailbox', default='')
    per.add_argument('--interval', type=float, default=ASK_INTERVAL)
    per.add_argument('--poll', type=float, default=POLL_SECONDS)
    per.add_argument('--max-ticks', type=int, default=None)
    per.add_argument('--max-seconds', type=float, default=DEFAULT_WAIT_SECONDS)
    for mode in ('stop', 'status'):
        item = sub.add_parser(mode)
        item.add_argument('--caller-uuid', required=True)
        if mode == 'stop':
            item.add_argument('--reason', default='OPERATOR')
    args = parser.parse_args(argv)
    if args.mode in ('request', 'ask'):
        out = request(args.supervisor, args.task, args.note, caller_uuid=args.caller_uuid or None,
                      transcript=args.transcript, max_seconds=args.max_seconds)
    elif args.mode == 'reconcile':
        out = reconcile(args.supervisor, args.marker)
    elif args.mode == 'persist':
        out = persist(args.supervisor, args.caller_uuid or None, args.task, args.note,
                      args.transcript, args.mailbox, args.interval, args.poll, args.max_ticks,
                      max_seconds=args.max_seconds)
    elif args.mode == 'stop':
        out = stop_loop(args.caller_uuid, args.reason)
    else:
        out = status(args.caller_uuid)
    print(json.dumps(out, ensure_ascii=False, default=str))
    if args.mode in ('request', 'ask', 'reconcile'):
        latest = out.get('last_ask') or out
        if latest.get('outcome') in ('PREPARED', 'UNCONFIRMED_DO_NOT_RESEND'):
            return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
