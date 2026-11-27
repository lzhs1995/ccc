#!/usr/bin/env python3
"""Reconcile one late handshake ACK without sending or replacing its controller.

Default: bounded, read-only observation. --apply is restricted to the original
supervisor by the original controller's live identity gate. It archives the
terminal receipt, updates only the original handshake ACK, and runs the original
validator. Native notice reception and business acceptance remain separate.
"""
import argparse
import contextlib
import fcntl
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from types import SimpleNamespace

import observe_handshake_ack as observation
import handshake_panel as panel


def original_modules(controller):
    modules = observation.original_modules(controller)
    harness = importlib.import_module('mac_harness')
    if Path(harness.__file__).resolve().parent != Path(controller) / 'scripts':
        raise ValueError('ACK_RECONCILE_MIXED_CONTROLLER')
    for name in ('_write_handshake_receipt', 'cmd_validate', '_gate_executors',
                 '_recheck_workspace_gate'):
        if not callable(getattr(harness, name, None)):
            raise ValueError('ACK_RECONCILE_UNSUPPORTED_CONTROLLER')
    return (*modules, harness)


def exact_ack_content(content, expected):
    """Match the sole text block; validated reasoning blocks remain opaque."""
    if isinstance(content, str):
        return content == expected
    if not isinstance(content, list):
        return False
    text_count = 0
    for block in content:
        if not isinstance(block, dict):
            return False
        if block == dict(type='text', text=expected):
            text_count += 1
        elif (set(block) == {'type', 'thinking', 'signature'}
              and block['type'] == 'thinking'
              and isinstance(block['thinking'], str)
              and isinstance(block['signature'], str)):
            continue
        elif (set(block) == {'type', 'data'}
              and block['type'] == 'redacted_thinking'
              and isinstance(block['data'], str)):
            continue
        else:
            return False
    return text_count == 1


def exact_ack_record(native, raw, binding, expected, intent_at):
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(record, dict) or record.get('type') != 'assistant'
            or record.get('sessionId') != binding['session_id']
            or record.get('isSidechain', False) is not False
            or record.get('isMeta', False) is not False
            or not isinstance(record.get('uuid'), str) or not record['uuid']):
        return None
    message = record.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return None
    content = message.get('content')
    if not exact_ack_content(content, expected):
        return None
    stamp = native._timestamp(record.get('timestamp'))
    # Native Claude journals use millisecond timestamps.
    if stamp is None or stamp < math.floor(intent_at * 1000) / 1000 or stamp > time.time() + 2:
        return None
    return dict(record_uuid=record['uuid'], timestamp=record['timestamp'],
                session_id=binding['session_id'], ack_line=expected)


def scan_ack(native, binding, text, expected, intent_at, fence, max_bytes=4 * 1024 * 1024):
    """Read only the bound original journal after its original input fence."""
    fenced = native._fenced_binding(binding, text, fence, intent_at)
    pin = fenced['transcript']
    with native._open(pin['path']) as stream:
        native._check_file(stream, binding)
        end = native._check_file(stream, fenced)
        stream.seek(pin['offset'])
        if not pin['at_record_boundary']:
            partial = stream.readline(native.MAX_RECORD + 1)
            if len(partial) > native.MAX_RECORD:
                raise ValueError('ACK_ORIGINAL_RECORD_LIMIT')
            if not partial.endswith(b'\n'):
                return dict(observed=False, state='ACK_RECORD_INCOMPLETE')
        scanned = 0
        while stream.tell() < end and scanned < max_bytes:
            offset = stream.tell()
            raw = stream.readline(min(native.MAX_RECORD + 1, end - offset))
            if len(raw) > native.MAX_RECORD:
                raise ValueError('ACK_ORIGINAL_RECORD_LIMIT')
            if not raw.endswith(b'\n'):
                return dict(observed=False, state='ACK_RECORD_INCOMPLETE', next_offset=offset)
            scanned += len(raw)
            record = exact_ack_record(native, raw, binding, expected, intent_at)
            if record:
                native._check_file(stream, fenced)
                return dict(observed=True, state='ACK_OBSERVED', proof=dict(
                    schema='bound-handshake-assistant-ack-v1', path=pin['path'],
                    device=pin['device'], inode=pin['inode'], offset=offset,
                    length=len(raw), sha256=observation.digest(raw), **record))
        cursor = stream.tell()
        native._check_file(stream, fenced)
        return dict(observed=False, state='ACK_SCAN_BUDGET' if cursor < end else 'ACK_NOT_VISIBLE',
                    next_offset=cursor)


def require_ack_proof(native, binding, text, expected, intent_at, fence, proof):
    fenced = native._fenced_binding(binding, text, fence, intent_at)
    pin = fenced['transcript']
    if (proof.get('schema') != 'bound-handshake-assistant-ack-v1'
            or any(proof.get(k) != pin[k] for k in ('path', 'device', 'inode'))
            or type(proof.get('offset')) is not int or proof['offset'] < pin['offset']
            or type(proof.get('length')) is not int or not 0 < proof['length'] <= native.MAX_RECORD):
        raise ValueError('ACK_BOUND_PROOF_INVALID')
    with native._open(pin['path']) as stream:
        native._check_file(stream, binding)
        end = native._check_file(stream, fenced)
        if proof['offset'] + proof['length'] > end:
            raise ValueError('ACK_BOUND_PROOF_TRUNCATED')
        if proof['offset']:
            stream.seek(proof['offset'] - 1)
            if stream.read(1) != b'\n':
                raise ValueError('ACK_BOUND_PROOF_NOT_RECORD_BOUNDARY')
        stream.seek(proof['offset'])
        raw = stream.read(proof['length'])
        record = exact_ack_record(native, raw, binding, expected, intent_at)
        if (not raw.endswith(b'\n') or observation.digest(raw) != proof['sha256']
                or record is None or any(proof.get(k) != v for k, v in record.items())):
            raise ValueError('ACK_BOUND_PROOF_CHANGED')
        native._check_file(stream, fenced)


@contextlib.contextmanager
def original_locks(root, attempt):
    # Open existing objects only; even a dry run must not create a lock sidecar.
    paths = (Path(attempt).parent / 'delivery.lock', root)
    handles = []
    try:
        for path in paths:
            flags = os.O_NOFOLLOW | os.O_NONBLOCK
            flags |= os.O_RDONLY | os.O_DIRECTORY if path == root else os.O_RDWR
            fd = os.open(path, flags)
            handles.append((path, fd, os.fstat(fd)))
            info = handles[-1][2]
            if path != root and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise ValueError('ACK_ORIGINAL_LOCK_INVALID')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def recheck():
            for path, fd, original in handles:
                current = path.lstat()
                if ((current.st_dev, current.st_ino, current.st_mode)
                        != (original.st_dev, original.st_ino, original.st_mode)
                        or (path != root and current.st_nlink != original.st_nlink)):
                    raise ValueError('ACK_ORIGINAL_LOCK_CHANGED')
        recheck()
        proof = [dict(path=str(path), device=info.st_dev, inode=info.st_ino)
                 for path, _, info in handles]
        yield recheck, proof, handles[-1][1]
        recheck()
    finally:
        for _, fd, _ in reversed(handles):
            os.close(fd)


def write_exclusive(directory_fd, name, raw):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory_fd)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def reconciled_row(row, ack, observed, archive, now):
    result = dict(row)
    previous = {k: row.get(k) for k in (
        'status', 'lifecycle', 'ack_state', 'error', 'terminal_error_at',
        'executor_ack', 'ack_observed_at', 'parser_confirmed_at')}
    result.pop('error', None)
    result.update(status='PASS', lifecycle='ACKED', executor_ack=True,
        ack_line=ack['proof']['ack_line'], ack_source='executor_response_nonce',
        ack_source_detail='bound_original_native_assistant_record',
        ack_response_marker=True, ack_observed_at=ack['proof']['timestamp'],
        parser_confirmed_at=now, terminal_error_at=None,
        ack_state='LATE_ACK_VERIFIED', late_ack_recovery_attempted=True,
        late_ack_recovered=True, attributable_to_executor=False,
        updated_at=now, late_ack_reconciliation=dict(
            schema='original-handshake-reconciliation-v1', archive=str(archive),
            previous=previous, ack_proof=ack['proof'],
            native_notice=observed['native_message'],
            original_controller=observed['controller'],
            original_receipt_sha256=observed['original_receipt_sha256'],
            original_attempt_sha256=observed['original_attempt_sha256'],
            input_operations=0, business_acceptance='NOT_EVALUATED'))
    # submission_state / dispatch_error / dispatch_submitted_at stay original.
    return result


def aggregate_rows(rows, members=None):
    return panel.aggregate(rows, members)


STRANDED_DRAFT_ERROR = 'DRAFT_NOT_READY: original paste preserved; no submission key sent'


def is_stranded_draft_row(row):
    return (row.get('lifecycle') == 'REJECTED'
            and row.get('submission_state') == 'STRANDED_IN_COMPOSE'
            and row.get('error') == STRANDED_DRAFT_ERROR
            and row.get('dispatch_error') == STRANDED_DRAFT_ERROR
            and 'dispatch_submitted_at' in row
            and row['dispatch_submitted_at'] is None)


def is_direct_pending_row(row):
    details = row.get('dispatch_error_details') or {}
    return (row.get('lifecycle') == 'REJECTED'
            and row.get('submission_state') == 'SUPERVISOR_DID_NOT_SUBMIT'
            and row.get('error') == row.get('dispatch_error') == 'MESSAGE_NOT_CONFIRMED'
            and details.get('confirmed') is False
            and details.get('state') in ('NATIVE_QUEUED', 'NATIVE_PENDING')
            and details.get('transport') == 'terminal-direct-marker-v1'
            and details.get('submission_key') == 'enter'
            and type(details.get('extra_enter')) is int and details['extra_enter'] in (0, 1)
            and details.get('input_operations') == 2 + details['extra_enter'])


def require_stranded_draft_attempt(row, attempt):
    if row.get('lifecycle') != 'REJECTED':
        return
    events = attempt.get('events')
    phases = ([event.get('phase') if isinstance(event, dict) else None
               for event in events] if isinstance(events, list) else [])
    if is_direct_pending_row(row):
        binding = attempt.get('native_binding', {})
        inputs = [p for p in phases if p in ('PASTE_INTENT', 'PASTED', 'ENTER_INTENT',
                  'ENTER_SENT', 'EXTRA_ENTER_INTENT', 'EXTRA_ENTER_SENT', 'NATIVE_INPUT_INTENT')
                  or 'TAB' in str(p)]
        wanted = ['PASTE_INTENT', 'PASTED', 'ENTER_INTENT', 'ENTER_SENT']
        if row['dispatch_error_details']['extra_enter']:
            wanted += ['EXTRA_ENTER_INTENT', 'EXTRA_ENTER_SENT']
        if (inputs != wanted or binding.get('receipt_contract') != 'native-user-marker-v1'
                or binding.get('receipt_marker') != row.get('ack_nonce')
                or attempt.get('error') != 'MESSAGE_NOT_CONFIRMED'
                or attempt.get('error_details') != row.get('dispatch_error_details')):
            raise ValueError('ACK_ORIGINAL_DIRECT_INPUT_HISTORY_REQUIRED')
        return
    if (not is_stranded_draft_row(row)
            or attempt.get('phase') != 'DRAFT_OBSERVATION'
            or attempt.get('error') != STRANDED_DRAFT_ERROR
            or phases[:2] != ['PASTE_INTENT', 'PASTED']
            or not phases[2:]
            or any(phase != 'DRAFT_OBSERVATION' for phase in phases[2:])):
        raise ValueError('ACK_ORIGINAL_STRANDED_DRAFT_HISTORY_REQUIRED')


def validate_panel(harness, native, receipt, gate, binding):
    rows = receipt.get('executors')
    members = harness._gate_executors(gate)
    if (gate.get('status') != 'PASS' or not isinstance(rows, list) or not rows
            or not len(rows) <= len(members)
            or gate.get('supervisor_surface_uuid') != binding['identity']['caller_surface_uuid']
            or gate.get('workspace_uuid') != binding['identity']['workspace_uuid']):
        raise ValueError('ACK_ORIGINAL_PANEL_OR_GATE_CHANGED')
    seen = set()
    for row, member in zip(rows, members):
        if (not isinstance(row, dict) or not panel.matches(row, member)
                or row.get('executor') in seen or type(row.get('ordinal')) is not int
                or row['ordinal'] != member['ordinal'] or row.get('task_id') != gate.get('task_id')
                or row.get('supervisor') != gate.get('supervisor')
                or row.get('executor_provider') != member.get('provider')
                or row.get('workspace_ref') != gate.get('workspace_ref')):
            raise ValueError('ACK_ORIGINAL_PANEL_ORDER_OR_BINDING_CHANGED')
        seen.add(row['executor'])
        nonce = row.get('ack_nonce')
        expected = f"PREFLIGHT_ACK|{row['task_id']}|{row['executor_provider']}:identity|READY|INLINE|{nonce}"
        if (not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{8}', nonce)
                or row.get('ack_line_expected') != expected):
            raise ValueError('ACK_ORIGINAL_PANEL_CHALLENGE_CHANGED')
        if row.get('executor_ack') is True:
            if (row.get('status') != 'PASS' or row.get('lifecycle') != 'ACKED'
                    or row.get('ack_line') != expected
                    or row.get('ack_source') != 'executor_response_nonce'
                    or row.get('ack_response_marker') is not True):
                raise ValueError('ACK_ORIGINAL_PANEL_ACK_INVALID')
        elif (row.get('executor_ack') is not False or row.get('status') != 'FAIL'
              or not (row.get('lifecycle') == 'EXPIRED' or is_stranded_draft_row(row)
                      or is_direct_pending_row(row))
              or native._timestamp(row.get('terminal_error_at')) is None):
            raise ValueError('ACK_RECONCILE_REQUIRES_COMPLETE_TERMINAL_PANEL')
    if receipt not in (aggregate_rows(rows), aggregate_rows(rows, members)):
        raise ValueError('ACK_UNSUPPORTED_OR_CHANGED_AGGREGATE')
    selected = [r for r, m in zip(rows, members)
                if m['surface_uuid'] == binding['identity']['target_surface_uuid']]
    if len(selected) != 1:
        raise ValueError('ACK_ORIGINAL_EXECUTOR_NOT_UNIQUE')
    participants = gate.get('native_participants', [])
    target = [p for p in participants if p.get('surface_uuid') == binding['identity']['target_surface_uuid']]
    if (len(target) != 1 or target[0].get('native_session_id') != binding['session_id']
            or target[0].get('provider') != binding['provider']):
        raise ValueError('ACK_ORIGINAL_EXECUTOR_SESSION_CHANGED')
    return rows, selected[0]


def embedded_supervisor(harness, gate, snapshot, expected_thread):
    """Authenticate an embedded original TUI using its controller's readers.

    Older collect() implementations only cover managed daemons. A missing
    proof is not permission to trust environment selectors: require the actual
    tool ancestry, native foreground, UUID/TTY and stable rereads instead.
    No module replacement, environment override or receiver input is involved.
    """
    import cmux_daemon_identity as identity
    import cmux_native_delivery as native
    import cmux_workspace_guard as guard

    current = identity.process(os.getpid())
    chain, daemon = identity._ancestry()
    candidates = [(index, item) for index, item in enumerate(chain)
                  if Path(item.get('executable', '')).name == 'codex']
    if daemon is not None or not candidates:
        raise ValueError('ACK_EMBEDDED_SUPERVISOR_ANCESTRY_REQUIRED')
    index, client = candidates[0]
    if (client.get('argv', [])[1:2] == ['app-server']
            or current.get('ppid') != chain[0]['pid']):
        raise ValueError('ACK_EMBEDDED_SUPERVISOR_ANCESTRY_REQUIRED')
    tool_chain = [current] + chain[:index]
    if (current['env'].get('CODEX_THREAD_ID') != expected_thread
            or any(item['env'].get('CODEX_THREAD_ID') not in (None, expected_thread)
                   for item in tool_chain)):
        raise ValueError('ACK_EMBEDDED_SUPERVISOR_TOOL_THREAD_CHANGED')
    for key, wanted in (('CMUX_SURFACE_ID', gate['supervisor_surface_uuid']),
                        ('CMUX_WORKSPACE_ID', gate['workspace_uuid'])):
        if (client['env'].get(key) != wanted or current['env'].get(key) != wanted
                or snapshot[2].get(key) != wanted
                or any(item['env'].get(key) not in (None, wanted) for item in tool_chain)):
            raise ValueError('ACK_EMBEDDED_SUPERVISOR_UUID_CHANGED')
    selected = identity.foreground.read(client)
    if selected is None or selected.get('thread_id') != expected_thread:
        raise ValueError('ACK_EMBEDDED_SUPERVISOR_NATIVE_FOREGROUND_REQUIRED')

    def scoped(live):
        if live[3] is not None:
            raise ValueError('ACK_EMBEDDED_SUPERVISOR_MODE_CHANGED')
        executors = harness._gate_executors(gate)
        if not executors:
            raise ValueError('ACK_ORIGINAL_EXECUTOR_REQUIRED')
        return guard.resolve_snapshot(live[0], live[1], executors[0]['surface_uuid'],
            env=live[2], expected=dict(workspace_uuid=gate['workspace_uuid'],
                caller_surface_uuid=gate['supervisor_surface_uuid']))

    scope = scoped(snapshot)
    row = native._tree_row(harness.cmux, gate['supervisor_surface_uuid'])
    tty = identity.subprocess.run(['/bin/ps', '-p', str(client['pid']), '-o', 'tty='],
        capture_output=True, text=True, timeout=identity.budget.timeout(), check=True).stdout.strip()
    identity.budget.check()
    if (tty in ('', '?', '??', '-') or row['tty'] != tty
            or row['workspace_uuid'] != scope['workspace_uuid']
            or row['target_pane_uuid'] != scope['caller_pane_uuid']):
        raise ValueError('ACK_EMBEDDED_SUPERVISOR_TTY_CHANGED')
    # Recheck cmux before the final kernel/foreground reads. The original
    # harness also performs its own workspace gate before every writer call.
    if (scoped(guard.caller_snapshot()) != scope
            or native._tree_row(harness.cmux, gate['supervisor_surface_uuid']) != row
            or identity.process(os.getpid()) != current
            or identity._ancestry() != (chain, daemon)
            or identity.foreground.read(client) != selected):
        raise ValueError('ACK_EMBEDDED_SUPERVISOR_CHANGED_DURING_CHECK')
    caller = dict(surface_id=scope['caller_surface_uuid'], workspace_id=scope['workspace_uuid'])
    proof = dict(source='embedded-original-supervisor-v1',
        client=native._public_process(client), tool=native._public_process(current),
        foreground=selected, tty=tty, scope=scope)
    return caller, proof


def require_supervisor(harness, gate, *, proof_out=None):
    import cmux_workspace_guard as guard
    participants = [p for p in gate.get('native_participants', [])
                    if p.get('role') == 'supervisor']
    if (len(participants) != 1 or participants[0].get('provider') != 'codex'
            or participants[0].get('surface_uuid') != gate.get('supervisor_surface_uuid')
            or not participants[0].get('native_session_id')):
        raise ValueError('ACK_ORIGINAL_SUPERVISOR_THREAD_REQUIRED')
    snapshot = guard.caller_snapshot()
    identity, _, _, proof = snapshot
    caller = identity.get('caller', {})
    if proof is None:
        caller, proof = embedded_supervisor(harness, gate, snapshot,
                                           participants[0]['native_session_id'])
    thread = (proof or {}).get('foreground', {}).get('thread_id')
    if (caller.get('surface_id') != gate['supervisor_surface_uuid']
            or caller.get('workspace_id') != gate['workspace_uuid']
            or thread != participants[0]['native_session_id']):
        raise ValueError('ACK_ORIGINAL_SUPERVISOR_THREAD_CHANGED')
    harness._recheck_workspace_gate(gate)
    if proof_out is not None:
        proof_out.update(proof)
    return thread


@contextlib.contextmanager
def archive_directory(root, root_fd, name, create, result):
    """Anchor exclusive archive writes to held directories, never to symlinks."""
    parent_name = 'handshake-reconciliations'
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    parent_fd = archive_fd = None
    try:
        try:
            parent_fd = os.open(parent_name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                yield None
                return
            os.mkdir(parent_name, 0o700, dir_fd=root_fd)
            result['archive_directories_created'] += 1
            os.fsync(root_fd)
            parent_fd = os.open(parent_name, flags, dir_fd=root_fd)
        if create:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            result['archive_directories_created'] += 1
            os.fsync(parent_fd)
        try:
            archive_fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if create:
                raise
            yield None
            return

        def recheck():
            for fd, path in ((parent_fd, root / parent_name),
                             (archive_fd, root / parent_name / name)):
                live, held = path.lstat(), os.fstat(fd)
                if (not stat.S_ISDIR(live.st_mode)
                        or (live.st_dev, live.st_ino) != (held.st_dev, held.st_ino)):
                    raise ValueError('ACK_ARCHIVE_DIRECTORY_CHANGED')
        recheck()
        yield archive_fd, recheck
        recheck()
    finally:
        for fd in (archive_fd, parent_fd):
            if fd is not None:
                os.close(fd)


def optional_snapshot(evidence, path):
    try:
        return evidence.snapshot(path)
    except FileNotFoundError:
        return None


def pin_summary(pin):
    return None if pin is None else dict(sha256=observation.digest(pin[0]), bytes=len(pin[0]))


def same_content(left, right):
    return left is None and right is None or bool(left and right and left[0] == right[0])


def marker_snapshot(harness, evidence, root):
    pins = {}
    paths = harness._workspace_marker_paths()
    if len(paths) > 256:
        raise ValueError('ACK_ACTIVE_MARKER_LIMIT')
    for path in paths:
        pin = evidence.snapshot(path)
        try:
            marker = json.loads(pin[0])
        except (ValueError, UnicodeError):
            # The original writer also skips unreadable marker JSON. It cannot
            # touch that marker, so an unrelated broken task is not a gate.
            continue
        prefix = marker.get('artifact_root') if isinstance(marker, dict) else None
        # Match the original writer's artifact-root prefix. Other tasks' normal
        # heartbeats must neither veto reconciliation nor be attributed to it.
        if not isinstance(prefix, str) or not prefix or not str(
                root / 'handshake-receipt.json').startswith(prefix.rstrip('/') + '/'):
            continue
        if path.is_symlink():
            raise ValueError('ACK_ACTIVE_MARKER_UNSUPPORTED')
        pins[path] = pin
    return pins


def check_marker_changes(harness, evidence, before, root):
    after = marker_snapshot(harness, evidence, root)
    if set(before) != set(after):
        raise ValueError('ACK_ACTIVE_MARKER_SET_CHANGED')
    changes = []
    for path, pin in before.items():
        if after[path] == pin:
            continue
        old, new = json.loads(pin[0]), json.loads(after[path][0])
        previous, current = old.pop('last_activity_at', None), new.pop('last_activity_at', None)
        if old != new or not isinstance(current, str):
            raise ValueError('ACK_ACTIVE_MARKER_CHANGED_BEYOND_HEARTBEAT')
        changes.append(dict(path=str(path), previous=previous, current=current,
                            before=pin_summary(pin), after=pin_summary(after[path]),
                            attribution='OBSERVED_DURING_ORIGINAL_WRITER_CALLS_NOT_EXCLUSIVE'))
    return changes


def read_reentry(args, archive, evidence, native, binding, text, intent_at, fence, request, result):
    """An interrupted operation is inspection-only; never overwrite its archive."""
    result.update(archive=str(archive), status='ACK_RECONCILIATION_INCOMPLETE',
                  reentry=True, recovery='Inspect the preserved operation; no automatic retry.')
    pins = {}

    def snapshot(path):
        pin = optional_snapshot(evidence, path)
        if path in pins and pin != pins[path]:
            raise ValueError('ACK_REENTRY_EVIDENCE_CHANGED')
        pins[path] = pin
        return pin

    def recheck():
        for path, pin in pins.items():
            if optional_snapshot(evidence, path) != pin:
                raise ValueError('ACK_REENTRY_EVIDENCE_CHANGED')

    intent_pin = snapshot(archive / 'INTENT.json')
    done_pin = snapshot(archive / 'RESULT.json')
    original_pin = snapshot(archive / 'handshake-receipt.json.original')
    intent = json.loads(intent_pin[0]) if intent_pin else None
    if intent is not None and intent.get('request') != request:
        raise ValueError('ACK_ARCHIVE_REQUEST_CHANGED')
    if original_pin is not None and observation.digest(original_pin[0]) != args.receipt_sha256:
        raise ValueError('ACK_ARCHIVED_ORIGINAL_CHANGED')
    for name in ('handshake-receipt.json', 'validation.json'):
        current = snapshot(Path(args.receipt).parent / name)
        previous = snapshot(archive / (name + '.original'))
        result.setdefault('preserved_operation_files', {})[name] = dict(
            current=pin_summary(current), archived_original=pin_summary(previous),
            changed_from_archived_original=not same_content(current, previous) if previous else None)
    if not all((done_pin, intent_pin, original_pin)):
        recheck()
        return result
    done = json.loads(done_pin[0])
    if done.get('status') != 'ACK_RECONCILED':
        raise ValueError('ACK_ARCHIVE_RESULT_INVALID')
    root = Path(args.receipt).parent
    published = snapshot(root / 'handshake-receipt.json')
    validation = snapshot(root / 'validation.json')
    stable = (published is not None and validation is not None
              and observation.digest(published[0]) == done.get('receipt_sha256')
              and observation.digest(validation[0]) == done.get('validation', {}).get('sha256'))
    for name in ('identity-gate.json', 'naming-proof.json', 'bridge-test-evidence.json'):
        old = snapshot(archive / (name + '.original'))
        current = snapshot(root / name)
        if (old is None) != (current is None) or (old and old[0] != current[0]):
            stable = False
    require_ack_proof(native, binding, text, done['ack']['proof']['ack_line'],
                      intent_at, fence, done['ack']['proof'])
    recheck()
    result.update(status='ALREADY_RECONCILED' if stable else 'ACK_PUBLISHED_STATE_CHANGED',
                  original_operation=done, current_published_files_match=stable)
    return result


def reconcile(args, modules=None):
    bridge, native, evidence, journal, harness = modules or original_modules(args.controller)
    result = dict(schema='original-handshake-reconciliation-v1', input_operations=0,
                  journal_writes=0, receipt_writes=0, validation_writes=0,
                  receipt_write_attempts=0, validation_write_attempts=0,
                  archive_write_attempts=0, archive_files_written=0,
                  archive_directories_created=0, heartbeat_touch_possible_calls=0,
                  archive_counts_scope='INTENT_AND_ORIGINALS_EXCLUDING_RESULT_COMMIT',
                  business_acceptance='NOT_EVALUATED')
    output, errors = io.StringIO(), io.StringIO()
    originals = {}
    markers = None
    archive_fd = None

    def refresh_effects():
        for label, name in (('receipt', 'handshake-receipt.json'), ('validation', 'validation.json')):
            path = Path(args.receipt).parent / name
            if path not in originals:
                continue
            try:
                after = optional_snapshot(evidence, path)
                changed = after != originals[path]
                result[label + '_writes'] = int(changed) if result[label + '_write_attempts'] else 0
                result.setdefault('file_observations', {})[label] = dict(
                    changed_since_start=changed, current=pin_summary(after),
                    before=pin_summary(originals[path]))
            except (OSError, ValueError) as exc:
                result[label + '_writes'] = None
                result.setdefault('file_observations', {})[label] = dict(state='UNMEASURED', error=str(exc))
        if markers is not None:
            try:
                result['heartbeat_changes'] = check_marker_changes(
                    harness, evidence, markers, Path(args.receipt).parent)
            except (OSError, ValueError) as exc:
                result['heartbeat_observation_error'] = str(exc)

    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            receipt_path, attempt_path, text_path = map(Path, (args.receipt, args.attempt, args.text_file))
            if (receipt_path.name != 'handshake-receipt.json'
                    or any(not p.is_absolute() or p.parent.resolve() != p.parent
                           for p in (receipt_path, attempt_path, text_path))
                    or any(not re.fullmatch(r'[0-9a-f]{64}', v)
                           for v in (args.receipt_sha256, args.attempt_sha256))):
                raise ValueError('ACK_ORIGINAL_PATHS_AND_SHA256_REQUIRED')
            root = receipt_path.parent
            archive = root / 'handshake-reconciliations' / args.receipt_sha256
            with original_locks(root, attempt_path) as (check_locks, lock_proof, root_fd):
                result['locks'] = dict(observed=lock_proof, historical_identity='NOT_BACKFILLED',
                    scope='reconciler instances and existing delivery lock; original harness must have exited')
                for path in (receipt_path, attempt_path, text_path, root / 'identity-gate.json',
                             root / 'naming-proof.json', root / 'bridge-test-evidence.json',
                             root / 'validation.json'):
                    originals[path] = optional_snapshot(evidence, path)
                if any(originals[p] is None for p in (receipt_path, attempt_path, text_path,
                                                       root / 'identity-gate.json')):
                    raise ValueError('ACK_ORIGINAL_EVIDENCE_REQUIRED')
                attempt = json.loads(originals[attempt_path][0])
                if observation.digest(originals[attempt_path][0]) != args.attempt_sha256:
                    raise ValueError('ACK_ORIGINAL_ATTEMPT_CHANGED')
                text = originals[text_path][0].decode('utf-8')
                body_pin = journal.original_body_pin(text, attempt)
                binding = attempt['native_binding']
                target = binding['identity']['target_surface_uuid']
                intent_at, fence = native._original_intent(attempt, binding, text)
                gate = json.loads(originals[root / 'identity-gate.json'][0])
                native.require_bound(bridge, target, binding, text, read_only=not args.apply)
                if args.apply:
                    result['supervisor_thread'] = require_supervisor(harness, gate,
                        proof_out=result.setdefault('supervisor_identity', {}))
                request = dict(controller=str(args.controller), receipt=str(receipt_path),
                    receipt_sha256=args.receipt_sha256, attempt=str(attempt_path),
                    attempt_sha256=args.attempt_sha256, text_file=str(text_path),
                    payload_sha256=observation.digest(originals[text_path][0]))
                with archive_directory(root, root_fd, args.receipt_sha256, False, result) as existing:
                    if existing is not None:
                        _, check_archive = existing
                        read_reentry(args, archive, evidence, native, binding, text,
                                     intent_at, fence, request, result)
                        check_archive()
                        check_locks()
                        if (any(optional_snapshot(evidence, path) != pin
                                for path, pin in originals.items())
                                or journal.original_body_pin(text, attempt) != body_pin):
                            raise ValueError('ACK_ORIGINAL_EVIDENCE_CHANGED')
                        native.require_bound(bridge, target, binding, text, read_only=not args.apply)
                        if args.apply:
                            require_supervisor(harness, gate)
                        return result

                receipt = json.loads(originals[receipt_path][0])
                if observation.digest(originals[receipt_path][0]) != args.receipt_sha256:
                    raise ValueError('ACK_ORIGINAL_RECEIPT_CHANGED')
                rows, selected = validate_panel(harness, native, receipt, gate, binding)
                require_stranded_draft_attempt(selected, attempt)
                observed = observation.observe(args, (bridge, native, evidence, journal))
                ack = scan_ack(native, binding, text, selected['ack_line_expected'], intent_at, fence)
                result.update(observation=observed, ack=ack)
                if not ack['observed']:
                    result['status'] = ack['state']
                    return result
                if selected.get('executor_ack') is True:
                    result['status'] = 'ALREADY_ACKED'
                    return result
                if selected.get('bridge_clear_confirmed') is not True:
                    # Capability belongs to the complete original controller.
                    # Old releases retain their clear requirement; a new label
                    # cannot waive a failed/missing original probe retroactively.
                    without_probe = getattr(harness, 'handshake_without_probe', None)
                    if (not callable(without_probe) or not without_probe(selected,
                            bridge_evidence_present=originals[root / 'bridge-test-evidence.json'] is not None)):
                        raise ValueError('ACK_ORIGINAL_BRIDGE_CLEAR_REQUIRED')

                def recheck(ignore=()):
                    check_locks()
                    for path, pin in originals.items():
                        if path not in ignore and optional_snapshot(evidence, path) != pin:
                            raise ValueError('ACK_ORIGINAL_EVIDENCE_CHANGED_DURING_RECONCILE')
                    if journal.original_body_pin(text, attempt) != body_pin:
                        raise ValueError('ACK_ORIGINAL_BODY_CHANGED')
                    native.require_bound(bridge, target, binding, text, read_only=not args.apply)
                    if args.apply:
                        require_supervisor(harness, gate)
                    require_ack_proof(native, binding, text, selected['ack_line_expected'],
                                      intent_at, fence, ack['proof'])
                    if (root / 'task-pack.json').exists() or (root / 'task-pack.json').is_symlink():
                        raise ValueError('ACK_BUSINESS_PACK_ALREADY_EXISTS')

                recheck()
                if not args.apply:
                    result.update(status='READY_ZERO_INPUT_ACK_RECONCILIATION',
                                  apply_requires_original_supervisor=True,
                                  original_harness_exit_required=True)
                    return result
                if not getattr(args, 'original_harness_exited', False):
                    raise ValueError('ACK_ORIGINAL_HARNESS_EXIT_ATTESTATION_REQUIRED')
                markers = marker_snapshot(harness, evidence, root)
                with archive_directory(root, root_fd, args.receipt_sha256, True, result) as opened:
                    archive_fd, check_archive = opened
                    result['archive'] = str(archive)

                    def archive_write(name, raw):
                        check_archive()
                        result['archive_write_attempts'] += 1
                        try:
                            write_exclusive(archive_fd, name, raw)
                        finally:
                            stored = optional_snapshot(evidence, archive / name)
                            result.setdefault('archive_files', {})[name] = pin_summary(stored)
                            if stored is not None and stored[0] == raw:
                                result['archive_files_written'] += 1

                    # Save intent first so even an interrupted copy has a bound operation.
                    archive_write('INTENT.json', json_bytes(dict(schema=result['schema'],
                        status='PREPARING', request=request, ack=ack,
                        supervisor_identity=result.get('supervisor_identity'),
                        original_harness_exit='ATTESTED_BY_ORIGINAL_SUPERVISOR',
                        original_files={str(path): pin_summary(pin) for path, pin in originals.items()},
                        original_markers={str(path): pin_summary(pin) for path, pin in markers.items()})))
                    for path, pin in originals.items():
                        if pin is not None and path.parent == root:
                            archive_write(path.name + '.original', pin[0])
                    recheck()
                    check_marker_changes(harness, evidence, markers, root)
                    check_archive()
                    updated = [reconciled_row(row, ack, observed, archive, harness._now())
                               if row is selected else row for row in rows]
                    result['receipt_write_attempts'] += 1
                    result['heartbeat_touch_possible_calls'] += 1
                    try:
                        if len(updated) < len(harness._gate_executors(gate)):
                            # The old writer credits only present rows. Publish an explicitly
                            # incomplete panel through its original file writer, never a
                            # transient PASS for the first of two executors.
                            harness._write(receipt_path, aggregate_rows(updated, harness._gate_executors(gate)))
                        else:
                            harness._write_handshake_receipt(root, updated)
                    finally:
                        refresh_effects()
                    published_pin = evidence.snapshot(receipt_path)
                    if json.loads(published_pin[0]) != aggregate_rows(updated, harness._gate_executors(gate)):
                        raise ValueError('ACK_ORIGINAL_WRITER_SHAPE_CHANGED')
                    recheck(ignore=(receipt_path,))
                    if result.get('heartbeat_observation_error'):
                        raise ValueError(result['heartbeat_observation_error'])
                    result['validation_write_attempts'] += 1
                    result['heartbeat_touch_possible_calls'] += 1
                    exit_code = 0
                    try:
                        try:
                            harness.cmd_validate(SimpleNamespace(artifact_root=str(root),
                                task_id=gate['task_id'], json=False))
                        except SystemExit as exc:
                            exit_code = exc.code
                    finally:
                        refresh_effects()
                    if evidence.snapshot(receipt_path) != published_pin:
                        raise ValueError('ACK_PUBLISHED_RECEIPT_CHANGED')
                    recheck(ignore=(receipt_path, root / 'validation.json'))
                    if result.get('heartbeat_observation_error'):
                        raise ValueError(result['heartbeat_observation_error'])
                    validation_raw = evidence.read_bytes(root / 'validation.json')
                    validation = json.loads(validation_raw)
                    if (validation.get('task_id') != gate['task_id']
                            or validation.get('status') not in ('PASS', 'FAIL')
                            or exit_code != (0 if validation['status'] == 'PASS' else 1)):
                        raise ValueError('ACK_ORIGINAL_VALIDATION_INVALID')
                    result.update(status='ACK_RECONCILED',
                        receipt_sha256=observation.digest(published_pin[0]),
                        validation=dict(status=validation['status'], exit_code=exit_code,
                                        sha256=observation.digest(validation_raw)),
                        original_receipt_preserved=True, original_attempt_unchanged=True,
                        original_harness_exit='ATTESTED_BY_ORIGINAL_SUPERVISOR')
                    # The commit record is last; a missing/partial one never authorizes replay.
                    result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())
                    result['archive_commit_record'] = dict(path=str(archive / 'RESULT.json'), state='WRITTEN')
                    raw = json_bytes(result)
                    try:
                        check_archive()
                        write_exclusive(archive_fd, 'RESULT.json', raw)
                        if evidence.read_bytes(archive / 'RESULT.json') != raw:
                            raise ValueError('ACK_ARCHIVE_COMMIT_CHANGED')
                    except (OSError, ValueError):
                        result['archive_commit_record']['state'] = 'UNCONFIRMED'
                        raise
                    check_archive()
                    check_locks()
                    return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        refresh_effects()
        result.update(status='ACK_RECONCILIATION_INCOMPLETE' if (
            result.get('reentry') or result['archive_directories_created'] or result['archive_write_attempts']
            or result['receipt_write_attempts'] or result['validation_write_attempts'])
            else 'ACK_RECONCILIATION_UNAVAILABLE', error=str(exc))
        return result
    finally:
        # Original writer/validator are chatty. The public CLI emits one JSON only.
        if output.getvalue() or errors.getvalue():
            result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('controller', 'receipt', 'receipt-sha256', 'attempt', 'attempt-sha256', 'text-file'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=observation.bounded_integer(1, 45), default=1)
    parser.add_argument('--lines', type=observation.bounded_integer(1, 2000), default=200)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--original-harness-exited', action='store_true',
                        help='original supervisor attests its terminal harness has exited; required for apply')
    args = parser.parse_args(argv)
    try:
        result = reconcile(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] in ('READY_ZERO_INPUT_ACK_RECONCILIATION',
                                        'ACK_RECONCILED', 'ALREADY_RECONCILED', 'ALREADY_ACKED') else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='ACK_RECONCILIATION_UNAVAILABLE', error=str(exc),
                              input_operations=0), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
