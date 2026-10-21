"""Read-only closeout evidence, not a delivery receipt or product acceptance.

The existing callback journal freezes the report. Once that attempt returns,
the executor may hand uncertain transport to its supervisor and stop. No network,
polling, receipt creation, disarming, or new completion protocol is needed here.
"""
import fcntl
import hashlib
import json
import math
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from cmux_evidence_io import read_bytes, attempt_paths

# The user's own completion sentence. Only the executor may supply it when its
# bounded task is genuinely complete; it never proves delivery or acceptance.
COMPLETION_SENTENCE = '完成，建议检查 usage: /context'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def terminal_report(marker, workspace, surface):
    """A frozen terminal attempt for this marker's exact executor, else None.

    Missing, legacy, malformed or in-flight evidence never seals tools. In
    particular, ended_at cannot settle a resumed callback holding delivery.lock.
    """
    pins = {}

    def read(path):
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('noncanonical evidence path')
        raw = read_bytes(path)
        pins[path] = raw
        return raw

    def doc(path):
        value = json.loads(read(path))
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value

    try:
        root = Path(marker['artifact_root'])
        if not root.is_absolute() or marker.get('workspace_uuid', workspace) != workspace:
            return None
        peers = marker['participants']
        executors = [p for p in peers if p.get('surface_uuid') == surface
                     and str(p.get('role', '')).startswith('executor')]
        supervisors = [p for p in peers if p.get('role') == 'supervisor']
        if len(executors) != 1 or len(supervisors) != 1:
            return None
        supervisor = supervisors[0]
        pack_path = root / 'task-pack.json'
        pack = doc(pack_path)
        required = ('task_id', 'executor_uuid', 'completion_nonce', 'completion_callback',
                    'callback_target', 'report', 'completion_receipt')
        if (pack.get('draft') is not False
                or any(not isinstance(pack.get(k), str) or not pack[k] for k in required)
                or pack['task_id'] != marker['task_id'] or pack['executor_uuid'] != surface
                or pack['callback_target'] not in (supervisor.get('surface_ref'),
                                                   supervisor.get('surface_uuid'))):
            return None
        report = Path(pack['report'])
        receipt = Path(pack['completion_receipt'])
        if report.parent != root or receipt.parent != root:
            return None
        raw_report = read(report)
        if not raw_report.strip():
            return None
        journal = receipt.with_name(receipt.stem + '-attempts')
        lock_path = journal / 'delivery.lock'
        # Never create/unlink the persistent lock. Nonblocking shared lock
        # excludes the existing sender's exclusive delivery operation.
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as lock:
            opened = os.fstat(lock.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return None
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)

            def same_inode():
                current = os.lstat(lock_path)
                return (opened.st_dev, opened.st_ino, 1) == (
                    current.st_dev, current.st_ino, current.st_nlink)

            if not same_inode():
                return None
            attempts = attempt_paths(journal)
            if not attempts:
                return None
            attempt = doc(attempts[-1])
            binding = attempt['binding']
            expected = {k: pack[k] for k in ('task_id', 'completion_nonce',
                        'completion_callback', 'callback_target', 'report')}
            expected.update(task_pack_sha256=hashlib.sha256(pins[pack_path]).hexdigest(),
                            report_sha256=hashlib.sha256(raw_report).hexdigest(),
                            report_bytes=len(raw_report))
            identity = binding['identity']
            if (any(binding.get(k) != v for k, v in expected.items())
                    or identity.get('workspace_uuid') != workspace
                    or identity.get('caller_surface_uuid') != surface
                    or identity.get('target_surface_uuid') != supervisor.get('surface_uuid')
                    or not identity.get('target_pane_uuid')):
                return None
            start, end = attempt['started_at_epoch'], attempt['ended_at_epoch']
            if not _number(start) or not _number(end) or not 0 < start <= end:
                return None
            phase = attempt['phase']
            events = attempt['events']
            phases = {'PASTE_INTENT', 'PASTED', 'ENTER_INTENT', 'ENTER_SENT',
                      'NATIVE_INPUT_INTENT', 'NATIVE_INPUT_RESULT',
                      'DRAFT_OBSERVATION', 'RECOVERY_OBSERVATION',
                      'POST_ENTER_OBSERVATION', 'EXTRA_ENTER_INTENT',
                      'QUEUE_TAB_INTENT', 'POST_QUEUE_TAB_OBSERVATION'}
            if not isinstance(events, list) or phase not in phases | {'NO_INPUT', 'CONFIRMED'}:
                return None
            effective_end = end
            if (phase in {'QUEUE_TAB_INTENT', 'POST_QUEUE_TAB_OBSERVATION'}
                    and events and _number(events[-1].get('at_epoch'))
                    and events[-1]['at_epoch'] > end):
                # Older original controllers leave ended_at at the first
                # uncertain Enter when queue-only continuation also returns
                # uncertainly. The shared lock above excludes a live sender.
                # Seal further tools without treating that return as delivery.
                names = [event['phase'] for event in events]
                tail = ['QUEUE_TAB_INTENT'] + (
                    ['POST_QUEUE_TAB_OBSERVATION'] if phase == 'POST_QUEUE_TAB_OBSERVATION' else [])
                if (names.count('QUEUE_TAB_INTENT') != 1
                        or names.count('PASTE_INTENT') != 1
                        or names.count('ENTER_INTENT') != 1
                        or names.count('POST_ENTER_OBSERVATION') != 1
                        or 'EXTRA_ENTER_INTENT' in names
                        or names[-len(tail):] != tail
                        or names[-len(tail)-1] != 'POST_ENTER_OBSERVATION'):
                    return None
                if any(not _number(event['at_epoch']) or event['at_epoch'] > end
                       for event in events[:-len(tail)]):
                    return None
                at = events[-1]['at_epoch']
                if not _number(at):
                    return None
                effective_end = max(end, at)
            previous = start
            for event in events:
                at = event['at_epoch']
                if not _number(at) or not previous <= at <= effective_end or event['phase'] not in phases:
                    return None
                previous = at
                if 'screen' in event or 'screen_sha256' in event:
                    if hashlib.sha256(event['screen'].encode()).hexdigest()[:16] != event['screen_sha256']:
                        return None
            if phase == 'CONFIRMED':
                if attempt.get('result', {}).get('confirmed') is not True or not events:
                    return None
            elif not isinstance(attempt.get('error'), str) or not attempt['error']:
                return None
            if phase == 'NO_INPUT':
                if events:
                    return None
            elif phase != 'CONFIRMED' and (not events or events[-1]['phase'] != phase):
                return None
            if (not same_inode() or attempt_paths(journal) != attempts
                    or any(read_bytes(p) != raw for p, raw in pins.items())):
                return None
            return dict(task_id=pack['task_id'], report=str(report),
                        attempt=str(attempts[-1]), report_sha256=expected['report_sha256'],
                        attempt_sha256=hashlib.sha256(pins[attempts[-1]]).hexdigest(),
                        artifact_root=str(root), ended_at_epoch=effective_end,
                        supervisor_uuid=supervisor.get('surface_uuid'),
                        # 只返回已经校验并在锁内重核的原任务绑定。
                        completion_nonce=pack['completion_nonce'],
                        completion_callback=pack['completion_callback'],
                        attempt_phase=phase,
                        task_pack_sha256=expected['task_pack_sha256'],
                        started_at_epoch=start)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def handoff_line(evidence):
    # Exact honest template accepted by Stop; no delivery/quality claim.
    return ('STATUS: REPORT_READY TASK_ID=' + evidence['task_id']
            + ' CALLBACK_UNCONFIRMED REPORT=' + evidence['report']
            + ' supervisor_reconciliation_required')


def honest_closeout(final, evidence):
    """Only the two exact turn-ends; neither confirms delivery or acceptance.

    The completion sentence is the executor's own statement that its bounded
    task is complete. Nothing here derives it from a report or a returned call.
    """
    line = handoff_line(evidence)
    return final.strip().split('\n') in ([line], [line, COMPLETION_SENTENCE])


def closeout_instructions(evidence):
    # 模板仅供引用；正常说明也可以结束等待，不把措辞变成解锁条件。
    return ('End with an honest explanation that delivery remains unconfirmed, '
            'or use this optional handoff line:\n' + handoff_line(evidence) + '\n'
            'Ordinary explanations and corrections are allowed. Stop enters '
            'WAITING_SUPERVISOR without receipt creation, acceptance or disarm. '
            'Only if your own bounded task is actually complete may you append "'
            + COMPLETION_SENTENCE + '". The hook never supplies that sentence. '
            'Do not claim callback confirmation or consensus without evidence.')


def _epoch(stamp):
    """POSIX seconds for an offset-aware ISO-8601 stamp, else None."""
    try:
        moment = datetime.fromisoformat(stamp)
        return moment.timestamp() if moment.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def superseded(evidence, markers, workspace, surface, now=None):
    """True once this supervisor armed a later, different task for this executor.

    Free text cannot unseal a frozen closeout: a user's new request and an
    automatic continuation prompt look the same to a hook. The protected signal
    is a fresh marker whose workspace, executor, supervisor, task, root and
    arming time (strictly after the original attempt, including queue-only
    continuation, ended) all match. Recheck TTL here: malformed legacy marker
    dates may remain visible to the caller, but never prove a new task boundary.
    This only moves tool/Stop scope; it never settles the original callback.
    """
    now = time.time() if now is None else now
    ended = evidence.get('ended_at_epoch')
    supervisor = evidence.get('supervisor_uuid')
    if not surface or not supervisor or not _number(ended) or not _number(now):
        return False
    for marker in markers:
        try:
            peers = [p for p in marker['participants'] if isinstance(p, dict)]
            executors = [p for p in peers if p.get('surface_uuid') == surface
                         and str(p.get('role', '')).startswith('executor')]
            supervisors = [p for p in peers if p.get('role') == 'supervisor']
            armed = _epoch(marker.get('armed_at'))
            ttl = marker.get('ttl_seconds')
            root, task = marker.get('artifact_root'), marker.get('task_id')
            if (marker.get('workspace_uuid') == workspace
                    and len(executors) == 1 and len(supervisors) == 1
                    and supervisors[0].get('surface_uuid') == supervisor
                    and isinstance(task, str) and task and task != evidence['task_id']
                    and isinstance(root, str) and Path(root).is_absolute()
                    and not Path(root).is_symlink()
                    and Path(root).resolve() != Path(evidence['artifact_root']).resolve()
                    and _number(armed) and ended < armed <= now
                    and _number(ttl) and ttl > 0 and now - armed <= ttl):
                return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return False
