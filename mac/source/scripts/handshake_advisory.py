"""Bounded original-handshake reminders at tool boundaries, never a sender.

Only a sender-returned attempt can bind a notice to an executor. A current
process check authorizes cursor rotation, not replacement of an old binding.
The original controller, nonce, journal, task marker and mailbox remain intact.
"""
from contextlib import ExitStack
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time

import cmux_bridge as bridge
import cmux_hook_identity as identity
import cmux_hook_thread as thread_scope
import cmux_message_journal as messages
import cmux_native_delivery as native
import cmux_supervisor_report_guard as markers
import executor_ready as ready
import executor_reply_advisory as executors
import handshake_evidence as evidence
from cmux_evidence_io import open_regular_lock, snapshot as read_snapshot
from delivery_receipts import snapshot
from executor_reask_scheduler import secure_directory

SCHEMA = 'handshake-advisory-v1'
MAX_MARKERS = 32
MAX_NOTICES = 8
SCAN_SECONDS = 1.5
HOOK_SECONDS = 4.25
REMINDER_SECONDS = 60.0
MAX_NATIVE_BYTES = 256 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def read_object(path):
    raw, file_identity = read_snapshot(path)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('evidence must be an object')
    pin = dict(path=str(path), bytes=len(raw), identity=list(file_identity),
               sha256=hashlib.sha256(raw).hexdigest())
    if snapshot(path) != pin:
        raise ValueError('evidence changed during read')
    return value, pin


def _claude_caller(payload):
    """Authenticate this automatic hook for fair enumeration only."""
    pid = os.getppid()
    for _ in range(3):
        process = executors.maintenance.identity.process(pid, validate_argv=False)
        if native._provider(process) == 'claude':
            break
        if Path(process['executable']).name not in ('sh', 'bash', 'zsh', 'rtk'):
            return None
        pid = process['ppid']
    else:
        return None
    session = native._live_session(process, 'claude')
    if payload.get('session_id') != session:
        return None
    workspace = process['env'].get('CMUX_WORKSPACE_ID')
    caller = process['env'].get('CMUX_SURFACE_ID')
    row = native._tree_row(bridge, caller)
    current = dict(schema=executors.IDENTITY_SCHEMA, provider='claude',
                   caller_surface_uuid=caller, workspace_uuid=workspace,
                   pane_uuid=row['target_pane_uuid'], tty=row['tty'],
                   process=native._public_process(process), session_id=session,
                   transcript=native._pin_file(Path(payload['transcript_path']), 'claude', session))
    executors.check_executor(current, payload)
    # Deliberately do not return or persist this newly observed transcript pin.
    return workspace, caller, dict(provider='claude', session_id=session)


def _executor_binding(binding):
    """Translate only the original native binding; never capture a new target."""
    original = binding['identity']
    return dict(schema=executors.IDENTITY_SCHEMA, provider=binding['provider'],
                caller_surface_uuid=original['target_surface_uuid'],
                workspace_uuid=original['workspace_uuid'], pane_uuid=original['target_pane_uuid'],
                **{k: copy.deepcopy(binding[k]) for k in ('tty', 'process', 'session_id', 'transcript')})


def _check_original_executor(binding, payload):
    if binding.get('provider') == 'claude':
        return executors.check_executor(_executor_binding(binding), payload)
    proof = identity.native_proof()
    if binding.get('provider') != 'codex' or not isinstance(proof, dict):
        raise ValueError('original authenticated executor required')
    original = binding['identity']
    # Hook identity exposes the authenticated public proof, not the daemon's
    # private client object. Compare its original process/session coordinates;
    # require_bound() below independently checks the full process file identity.
    if (proof.get('session') != binding['session_id']
            or (proof.get('foreground') or {}).get('thread_id') != binding['session_id']
            or payload.get('session_id') != binding['session_id']
            or payload.get('transcript_path') != binding['transcript']['path']
            or any(proof.get(key) != binding['process'][key]
                   for key in ('pid', 'birth', 'executable'))
            or proof.get('tty') != binding['tty']
            or proof.get('surface_uuid') != original['target_surface_uuid']
            or identity.identity(payload) != (original['workspace_uuid'],
                                               original['target_surface_uuid'])):
        raise ValueError('hook does not belong to the original Codex executor')
    # discover() has authenticated the root's native metadata/foreground. The
    # caller still runs require_bound() below against the original pinned file,
    # process, pane and session; this proof never creates a receiver binding.
    return True


def _participants(marker, path, workspace):
    if (type(marker.get('marker_version')) is not int or marker['marker_version'] != 2
            or marker.get('workspace_uuid') != workspace
            or not thread_scope.uuid_value(marker.get('collaboration_id'))
            or not isinstance(marker.get('task_id'), str) or not marker['task_id']
            or path.name != marker['collaboration_id'] + '.json'):
        raise ValueError('not a bound v2 marker')
    peers = marker.get('participants')
    if (not isinstance(peers, list) or len(peers) < 2
            or not all(isinstance(p, dict) for p in peers)):
        raise ValueError('missing participants')
    owners = [p for p in peers if p.get('role') == 'supervisor']
    workers = [p for p in peers if p.get('role') == 'executor']
    if (len(owners) != 1 or owners[0].get('ordinal') != 0
            or len(workers) + 1 != len(peers)
            or any(type(p.get('ordinal')) is not int for p in peers)
            or sorted(p['ordinal'] for p in workers) != list(range(1, len(workers) + 1))
            or any(not thread_scope.uuid_value(p.get('surface_uuid'))
                   or not thread_scope.uuid_value(p.get('native_session_id')) for p in peers)
            or len({p['surface_uuid'] for p in peers}) != len(peers)):
        raise ValueError('ambiguous original participants')
    return owners[0], workers


def _scan(attempt, wire, scans):
    binding = attempt['native_binding']
    at, fence = native._original_intent(attempt, binding, wire)
    key = digest([binding, at, fence])
    previous = scans.get(key, {})
    if not isinstance(previous, dict):
        previous = {}
    if previous.get('confirmed') is True:
        if not native.validate_proof(binding, wire, previous.get('native_proof'),
                                     not_before=at, paste_fence=fence):
            raise ValueError('saved native proof changed')
        return previous
    result = native.probe(binding, wire, cursor=previous.get('next_offset'),
                          max_bytes=MAX_NATIVE_BYTES, not_before=at, paste_fence=fence)
    queued = previous.get('queued_proof')
    if queued and not result.get('queued_proof') and not result.get('confirmed'):
        check = native.probe(binding, wire, cursor=queued['offset'], max_bytes=1,
                             not_before=at, paste_fence=fence)
        if check.get('queued_proof') != queued:
            raise ValueError('saved queued proof changed')
        result['queued_proof'] = queued
        if result['state'] == 'NATIVE_PENDING':
            result['state'] = 'NATIVE_QUEUED'
    scans[key] = result
    return result


def _pack_state(root, marker, owner, worker, pins):
    """Positive receipt proof can end a reminder; absence never proves no send."""
    path = root / 'task-pack.json'
    try:
        pack, pin = read_object(path)
    except FileNotFoundError:
        return 'ABSENT_DISPATCH_UNKNOWN'
    pins.append(pin)
    if (pack.get('task_id') != marker['task_id']
            or pack.get('executor_uuid') != worker['surface_uuid']):
        return 'UNVERIFIED'
    if pack.get('draft') is True:
        return 'DRAFT_ON_DISK_DISPATCH_UNKNOWN'
    try:
        wire = bridge.task_pack_notice(path)
        journal = (Path.home() / '.local/state/multi-agent-collaboration/task-dispatch-v1'
                   / messages.digest(json.dumps([owner['surface_uuid'], marker['task_id']])))
        receipt, receipt_pin = read_object(journal / 'receipt.json')
        original = Path(receipt['attempt'])
        if (original.parent != journal or not original.name.startswith('attempt-')
                or not original.name.endswith('.json')):
            return 'ON_DISK_DISPATCH_UNVERIFIED'
        attempt, attempt_pin = read_object(original)
        bound = attempt['binding']
        native_binding = attempt['native_binding']
        expected = dict(workspace_uuid=marker['workspace_uuid'],
                        caller_surface_uuid=owner['surface_uuid'],
                        target_surface_uuid=worker['surface_uuid'],
                        target_pane_uuid=native_binding['identity']['target_pane_uuid'])
        if (receipt.get('binding') != bound or bound.get('identity') != expected
                or bound.get('task_id') != marker['task_id']
                or bound.get('task_pack_sha256') != pin['sha256']
                or bound.get('payload_sha256') != messages.digest(wire)
                or native_binding.get('session_id') != worker['native_session_id']):
            return 'ON_DISK_DISPATCH_UNVERIFIED'
        if native.receipt_evidence(bridge, worker['surface_uuid'], wire, attempt, receipt, read_only=True):
            pins.extend([receipt_pin, attempt_pin])
            return 'NATIVE_RECEIVED'
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
        pass
    return 'ON_DISK_DISPATCH_UNVERIFIED'


def _notices(path, directory_fd, workspace, caller, facts, payload, scans, checked):
    marker = markers._read_marker(path, directory_fd)
    if not isinstance(marker, dict):
        return []
    owner, workers = _participants(marker, path, workspace)
    # A Codex root can be an executor. Roles come from the authenticated marker,
    # not the provider name or a borrowed foreground/thread selector.
    supervisor = (owner['surface_uuid'] == caller
                  and owner['native_session_id'] == facts['session_id']
                  and owner.get('provider') == facts['provider'])
    if not supervisor:
        workers = [w for w in workers if w['surface_uuid'] == caller
                   and w['native_session_id'] == facts['session_id']
                   and w.get('provider') == facts['provider']]
        if not workers:
            return []
    root = Path(marker['artifact_root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        return []
    receipt, receipt_pin = read_object(root / 'handshake-receipt.json')
    rows = receipt.get('executors')
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return []
    notices = []
    for worker in workers:
        matching = [row for row in rows if row.get('executor') == worker.get('surface_ref')
                    and row.get('ordinal') == worker['ordinal']]
        if len(matching) != 1:
            continue
        row = matching[0]
        if (row.get('task_id') != marker['task_id'] or row.get('supervisor') != owner.get('surface_ref')
                or row.get('executor_provider') != worker.get('provider')
                or type(row.get('ordinal')) is not int
                or not isinstance(row.get('ack_nonce'), str) or not row['ack_nonce']):
            continue
        pins = [receipt_pin]
        try:
            saved = row['dispatch_attempt']
            attempt_path = Path(saved['path'])
            key = messages.digest(json.dumps([owner['surface_uuid'], row['ack_nonce']], sort_keys=True))
            journal = Path.home() / '.local/state/multi-agent-collaboration/message-dispatch-v1' / key
            if (attempt_path.parent != journal or not attempt_path.name.startswith('attempt-')
                    or not attempt_path.name.endswith('.json')):
                raise ValueError('attempt is outside the original sender journal')
            attempt, attempt_pin = read_object(attempt_path)
            if attempt_pin != saved:
                raise ValueError('original sender-returned attempt changed')
            source = evidence.verify_controller(row['dispatch_controller_pin'],
                        attempt['controller_provenance'], checked=checked)
            if row.get('dispatch_controller_provenance') != attempt['controller_provenance']:
                raise ValueError('original controller mismatch')
            wire = row['dispatch_payload']
            if not isinstance(wire, str) or messages.digest(wire) != row['dispatch_payload_sha256']:
                raise ValueError('original wire changed')
            body_pin = messages.original_body_pin(wire, attempt)
            if row.get('dispatch_body_reference') != messages.prompt_reference.wire_reference(wire):
                raise ValueError('original reference changed')
            body = (messages.read_bytes(body_pin['path']).decode('utf-8') if body_pin else wire)
            if body != evidence.challenge(row, root, source / 'SKILL.md'):
                raise ValueError('handshake body does not match this receipt row')
            binding = attempt['native_binding']
            original = binding['identity']
            if (original.get('workspace_uuid') != workspace
                    or original.get('caller_surface_uuid') != owner['surface_uuid']
                    or original.get('target_surface_uuid') != worker['surface_uuid']
                    or binding.get('session_id') != worker['native_session_id']
                    or attempt['binding'].get('marker') != row['ack_nonce']
                    or attempt['binding'].get('payload_sha256') != messages.digest(wire)):
                raise ValueError('original handshake endpoint mismatch')
            if not supervisor:
                _check_original_executor(binding, payload)
            native.require_bound(bridge, worker['surface_uuid'], binding, wire, read_only=True)
            result = _scan(attempt, wire, scans)
            pins.append(attempt_pin)
            if body_pin:
                # Body pins predate the generic snapshot shape. Its byte count
                # is already fixed in the original wire reference; do not
                # capture a new pin or compare unlike snapshot dictionaries.
                pins.append(dict(body_pin, bytes=attempt['body_reference']['bytes']))
            controller_state = 'VERIFIED_ORIGINAL_CONTROLLER'
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
            if not supervisor:
                continue
            # The authenticated owner may see a missing-evidence diagnostic.
            # This is not authority to repair a pin, replay or expose to Claude.
            result, body_pin = dict(state='UNKNOWN', confirmed=False), None
            controller_state = 'ORIGINAL_EVIDENCE_UNAVAILABLE'
        pack_state = _pack_state(root, marker, owner, worker, pins)
        if pack_state == 'NATIVE_RECEIVED' and row.get('executor_ack') is True:
            continue
        notice = dict(schema=SCHEMA, kind=('ACKED_RESERVATION' if row.get('executor_ack') is True
                                         else 'PENDING_HANDSHAKE'),
                      task_id=marker['task_id'], collaboration_id=marker['collaboration_id'],
                      workspace_uuid=workspace, supervisor_uuid=owner['surface_uuid'],
                      executor_uuid=worker['surface_uuid'], executor_session=worker['native_session_id'],
                      ordinal=worker['ordinal'], ack_nonce=row['ack_nonce'],
                      handshake_receipt=str(root / 'handshake-receipt.json'),
                      handshake_receipt_sha256=receipt_pin['sha256'],
                      original_attempt=row.get('dispatch_attempt'), controller_state=controller_state,
                      body_reference=row.get('dispatch_body_reference') if body_pin else None,
                      notice_native_state=result['state'],
                      notice_native_received=result.get('confirmed') is True,
                      notice_queued_proof=result.get('queued_proof'), body_read_confirmed=False,
                      receipt_reports_ack=row.get('executor_ack') is True,
                      ack_independently_verified=False, task_pack_state=pack_state,
                      task_marker_present=True, release_verified=False, business_accepted=False,
                      input_operations=0, ask_count_increment=0,
                      _marker=marker, _pins=pins)
        notices.append(notice)
    return notices


def emit_context(notices):
    text = ('Original handshake / reservation reminder. Read the exact referenced body and your '
            'own executors[] row before deciding the next explicit action. A file reminder, body '
            'reading, native user reception, ACK, dispatched task and business acceptance are '
            'separate facts. A pack on disk does not prove dispatch; absent evidence remains '
            'UNKNOWN and never authorizes replay. Reconcile only through the original controller. '
            'This hook sends no ACK/STATUS, consumes no reply, starts no inquiry, releases no '
            'reservation, supplies no ETA and does not continue a turn.\n'
            + '\n'.join(json.dumps(n, ensure_ascii=False) for n in notices))
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                     'additionalContext': text}}, ensure_ascii=False), flush=True)


def _discover(workspace, caller, facts, payload, *, emit, now, state_root):
    if not thread_scope.uuid_value(workspace) or not thread_scope.uuid_value(caller):
        return []
    directory = Path(state_root or ready.state_root() / SCHEMA)
    secure_directory(directory)
    key = digest([workspace, caller, facts['session_id']])
    cursor_path = directory / (key + '.json')
    with open_regular_lock(directory / (key + '.lock')) as lock, ExitStack() as stack:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = ready.read_json(cursor_path) or {}
        last = old.get('last', '')
        last = last if isinstance(last, str) else ''
        resume = old.get('resume', '')
        resume = resume if isinstance(resume, str) else ''
        when = time.time() if now is None else now
        if type(when) not in (int, float) or not math.isfinite(when):
            return []
        offered = old.get('offered', {})
        offered = {k: v for k, v in offered.items() if isinstance(k, str)
                   and type(v) in (int, float) and math.isfinite(v)
                   and 0 <= when - v < REMINDER_SECONDS} if isinstance(offered, dict) else {}
        scans = old.get('scans', {})
        scans = scans if isinstance(scans, dict) and len(scans) <= 4096 else {}
        deadline = time.monotonic() + SCAN_SECONDS
        paths = markers._workspace_paths(workspace, deadline, stack)
        window = [p for p in paths if str(p) > last] + [p for p in paths if str(p) <= last]
        # If a marker has more workers than the per-hook notice budget, visit
        # its remaining workers at the very next event, before rotating away.
        pending = [p for p in window if str(p) == resume]
        window = pending + [p for p in window if str(p) != resume]
        resume = ''
        candidates, checked = [], {}
        for path in window[:MAX_MARKERS]:
            if len(candidates) >= MAX_NOTICES or time.monotonic() >= deadline:
                break
            last = str(path)
            try:
                with ready.runtime_limit(max(0.001, deadline - time.monotonic())):
                    notices = _notices(path, paths[path], workspace, caller, facts, payload, scans, checked)
                for notice in notices:
                    # The same original handshake shares one reminder budget.
                    # Queue, ACK and pack-state changes do not grant an earlier
                    # reminder or a new terminal/input operation.
                    token = digest([notice[k] for k in ('collaboration_id', 'executor_uuid', 'ack_nonce')])
                    if token not in offered:
                        if len(candidates) < MAX_NOTICES:
                            candidates.append((path, token, notice))
                        else:
                            resume = str(path)
            except ready.WaitBudgetExpired:
                # Publish neither progress nor offers after an interrupted
                # scan. The next event retries from the original cursor.
                return []
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
                continue
        notices, tokens = [], []
        for path, token, notice in candidates:
            try:
                if (markers._read_marker(path, paths[path]) != notice.pop('_marker')
                        or any(snapshot(pin['path']) != pin for pin in notice.pop('_pins'))):
                    continue
                notices.append(notice)
                tokens.append(token)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                continue
        if notices:
            emit(notices)
            offered.update({token: when for token in tokens})
        ready.write_json(cursor_path, dict(schema=SCHEMA, last=last, resume=resume,
                                          offered=offered, scans=scans))
        return notices


def discover(payload, *, emit=emit_context, now=None, state_root=None):
    try:
        if not isinstance(payload, dict) or payload.get('hook_event_name') != 'PostToolUse':
            return []
        with ready.runtime_limit(HOOK_SECONDS):
            facts = thread_scope.describe(payload)
            if facts['thread_scope'] == 'ROOT_METADATA':
                with identity.evaluation(payload):
                    workspace, caller = identity.identity(payload)
                    if not thread_scope.authenticated_root(facts, identity.native_proof()):
                        return []
                    return _discover(workspace, caller, dict(provider='codex',
                        session_id=facts['actual_thread_id']), payload, emit=emit, now=now,
                        state_root=state_root)
            bound = _claude_caller(payload)
            if bound is not None:
                workspace, caller, facts = bound
                return _discover(workspace, caller, facts, payload, emit=emit, now=now,
                                 state_root=state_root)
    except ready.WaitBudgetExpired:
        pass
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError) + identity.ERRORS:
        pass
    return []


if __name__ == '__main__':
    import sys
    try:
        discover(json.load(sys.stdin))
    except BaseException:
        pass
