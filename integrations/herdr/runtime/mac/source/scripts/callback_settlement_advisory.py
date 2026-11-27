"""Offer verified callbacks to their original supervisor; never settle or send.

A retained marker, including one past its TTL, still needs explicit owner action.
Marker disappearance is not RELEASE evidence. This observer records reminders
only; it cannot accept a report, consume a reply, or resume an inquiry.
"""
from contextlib import ExitStack
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time

import cmux_bridge as bridge
import cmux_callback_journal as callbacks
import cmux_hook_identity as identity
import cmux_hook_thread as thread_scope
import cmux_supervisor_report_guard as markers
import executor_ready as ready
from cmux_evidence_io import open_regular_lock, read_bytes
from delivery_receipts import snapshot
from executor_reask_scheduler import secure_directory

SCHEMA = 'callback-settlement-advisory-v1'
MAX_MARKERS = 32
MAX_NOTICES = 8
SCAN_SECONDS = 1.5
HOOK_SECONDS = 4.25
REMINDER_SECONDS = 60.0
SUPPORTED_MARKER_VERSIONS = (1, 2)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _notice(path, directory_fd, workspace, caller, facts):
    marker = markers._read_marker(path, directory_fd)
    if (not isinstance(marker, dict) or type(marker.get('marker_version')) is not int
            or marker['marker_version'] not in SUPPORTED_MARKER_VERSIONS
            or marker.get('workspace_uuid') != workspace
            or not isinstance(marker.get('task_id'), str) or not marker['task_id']
            or not thread_scope.uuid_value(marker.get('collaboration_id'))):
        return None
    if path.name not in (marker['collaboration_id'] + '.json', workspace + '.json'):
        return None
    peers = marker.get('participants')
    if not isinstance(peers, list) or not all(isinstance(p, dict) for p in peers):
        return None
    owners = [p for p in peers if p.get('role') == 'supervisor']
    if (len(owners) != 1 or owners[0].get('surface_uuid') != caller
            or owners[0].get('native_session_id') != facts['actual_thread_id']):
        return None
    root = Path(marker['artifact_root'])
    if not root.is_absolute() or root.resolve() != root:
        return None
    pack_path = root / 'task-pack.json'
    pack_raw = read_bytes(pack_path)
    pack = json.loads(pack_raw)
    executors = [p for p in peers if str(p.get('role', '')).startswith('executor')
                 and p.get('surface_uuid') == pack.get('executor_uuid')]
    if (pack.get('task_id') != marker['task_id'] or len(executors) != 1
            or len({p.get('surface_uuid') for p in peers}) != len(peers)
            or pack.get('collaboration_id', marker['collaboration_id']) != marker['collaboration_id']
            or pack.get('callback_target') not in (caller, owners[0].get('surface_ref'))):
        return None
    report_path, receipt_path = Path(pack['report']), Path(pack['completion_receipt'])
    # The frozen pack owns these exact paths. Executors may legitimately keep
    # their output beside the pack directory. The original journal below binds
    # the pack, report and receipt bytes; directory proximity is not authority.
    if not report_path.is_absolute() or not receipt_path.is_absolute():
        return None
    receipt_raw = read_bytes(receipt_path)
    receipt = json.loads(receipt_raw)
    binding = receipt.get('native_binding', {})
    if (binding.get('provider') != 'codex'
            or binding.get('session_id') != facts['actual_thread_id']
            or binding.get('transcript', {}).get('path') != facts['transcript_path']):
        return None
    # Preserve the pack's original target spelling (surface:N or UUID). The
    # validator verifies live reverse identity as well as the original EOF fence.
    evidence = callbacks.verified_receipt(bridge, pack['callback_target'], pack_path)
    if evidence is None:
        return None
    original = evidence['identity']
    if (original['workspace_uuid'] != workspace
            or original['target_surface_uuid'] != caller
            or original['caller_surface_uuid'] != executors[0]['surface_uuid']
            or evidence['pack']['sha256'] != hashlib.sha256(pack_raw).hexdigest()
            or evidence['receipt']['sha256'] != hashlib.sha256(receipt_raw).hexdigest()
            or markers._read_marker(path, directory_fd) != marker):
        return None
    return dict(schema=SCHEMA, kind='CALLBACK_SETTLEMENT', task_id=marker['task_id'],
                collaboration_id=marker['collaboration_id'], workspace_uuid=workspace,
                supervisor_uuid=caller, thread_id=facts['actual_thread_id'],
                executor_uuid=executors[0]['surface_uuid'], marker=str(path),
                task_pack=str(pack_path), task_pack_sha256=evidence['pack']['sha256'],
                report=str(report_path), report_sha256=evidence['report']['sha256'],
                receipt=str(receipt_path), receipt_sha256=evidence['receipt']['sha256'],
                attempt=receipt['attempt'], callback_native_confirmed=True,
                task_marker_present=True, released=False, accepted=False,
                input_operations=0, ask_count_increment=0,
                _marker=marker, _evidence=evidence)


def emit_context(notices):
    text = ('CALLBACK_SETTLEMENT: the original callback has verified native reception '
            'and its task marker remains. Read the report, record the scoped verdict, '
            'and explicitly RELEASE only at the proper boundary. TTL and marker '
            'disappearance do not prove RELEASE. This reminder performs no acceptance, '
            'release, reply consumption, new inquiry or terminal input.\n'
            + '\n'.join(json.dumps(n, ensure_ascii=False) for n in notices))
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                            'additionalContext': text}},
                     ensure_ascii=False), flush=True)


def _discover_bound(workspace, caller, facts, *, emit, now, state_root):
    if not thread_scope.uuid_value(workspace) or not thread_scope.uuid_value(caller):
        return []
    directory = Path(state_root or ready.state_root() / SCHEMA)
    secure_directory(directory)
    key = _digest([workspace, caller, facts['actual_thread_id']])
    cursor_path = directory / (key + '.json')
    with open_regular_lock(directory / (key + '.lock')) as lock, ExitStack() as stack:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = ready.read_json(cursor_path) or {}
        last = previous.get('last', '')
        last = last if isinstance(last, str) else ''
        offered = previous.get('offered', {})
        offered = offered if isinstance(offered, dict) else {}
        when = time.time() if now is None else now
        if type(when) not in (int, float) or not math.isfinite(when):
            return []
        # Bound cursor growth; retaining only recent reminders cannot suppress
        # one beyond the 60-second repeat boundary.
        offered = {k: v for k, v in offered.items() if isinstance(k, str)
                   and type(v) in (int, float) and math.isfinite(v)
                   and when - v < REMINDER_SECONDS}
        deadline = time.monotonic() + SCAN_SECONDS
        paths = markers._workspace_paths(workspace, deadline, stack)
        window = [p for p in paths if str(p) > last] + [p for p in paths if str(p) <= last]
        candidates = []
        for path in window[:MAX_MARKERS]:
            if len(candidates) >= MAX_NOTICES or time.monotonic() >= deadline:
                break
            # Advance across unrelated, bad and slow markers too. A per-marker
            # timeout cannot monopolize every later tool event.
            last = str(path)
            try:
                with ready.runtime_limit(max(0.001, deadline - time.monotonic())):
                    notice = _notice(path, paths[path], workspace, caller, facts)
                if notice is None:
                    continue
                token = _digest([notice[k] for k in ('task_id', 'collaboration_id',
                                 'task_pack_sha256', 'report_sha256', 'receipt_sha256')])
                if token not in offered:
                    candidates.append((path, token, notice))
            except ready.WaitBudgetExpired:
                break
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
                continue
        notices, tokens = [], []
        for path, token, notice in candidates:
            try:
                if (markers._read_marker(path, paths[path]) != notice.pop('_marker')
                        or any(snapshot(pin['path']) != pin
                               for k, pin in notice.pop('_evidence').items()
                               if k in ('pack', 'report', 'receipt'))):
                    continue
                notices.append(notice)
                tokens.append(token)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                continue
        if notices:
            emit(notices)  # Only a successful output flush authorizes reminder bookkeeping.
            offered.update({token: when for token in tokens})
        ready.write_json(cursor_path, dict(schema=SCHEMA, last=last, offered=offered))
        return notices


def discover(payload, *, emit=emit_context, now=None, state_root=None):
    try:
        if not isinstance(payload, dict) or payload.get('hook_event_name') != 'PostToolUse':
            return []
        with ready.runtime_limit(HOOK_SECONDS):
            facts = thread_scope.describe(payload)
            if facts['thread_scope'] != 'ROOT_METADATA':
                return []
            with identity.evaluation(payload):
                workspace, caller = identity.identity(payload)
                if not thread_scope.authenticated_root(facts, identity.native_proof()):
                    return []
                return _discover_bound(workspace, caller, facts, emit=emit, now=now,
                                       state_root=state_root)
    except ready.WaitBudgetExpired:
        return []
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError) + identity.ERRORS:
        return []


if __name__ == '__main__':
    import sys
    try:
        discover(json.load(sys.stdin))
    except BaseException:
        pass  # Standalone use, like the registered wrapper, never blocks a turn.
