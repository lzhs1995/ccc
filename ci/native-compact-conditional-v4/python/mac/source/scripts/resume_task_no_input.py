#!/usr/bin/env python3
"""Resume a task whose complete journal proves no input was attempted.

The default is a read-only plan. Apply pins that plan's history hash, keeps every
old byte, and appends one attempt through the complete original controller.
PASTE_INTENT, native RPC intent, queue, receipt and UNKNOWN are never retryable.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from repair_task_notice import controller_modules


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def history_pin(records):
    members = [dict(name=path.name, bytes=len(raw), sha256=sha(raw))
               for path, raw in records]
    raw = json.dumps(members, sort_keys=True, separators=(',', ':')).encode()
    return dict(attempts=members, history_sha256=sha(raw))


def validate_history(records, binding, lock_identity):
    if not records:
        raise ValueError('NO_INPUT_ORIGINAL_ATTEMPTS_REQUIRED')
    parsed = []
    for number, (path, raw) in enumerate(records, 1):
        if path.name != 'attempt-%04d.json' % number:
            raise ValueError('NO_INPUT_HISTORY_NOT_CONTIGUOUS')
        row = json.loads(raw)
        if (row.get('phase') != 'NO_INPUT' or row.get('events') != []
                or not isinstance(row.get('error'), str)
                or not row['error'].startswith(('COMPOSE_OCCUPIED:', 'RECEIVER_COMPACTING:'))):
            raise ValueError('NO_INPUT_HISTORY_HAS_INPUT_OR_UNKNOWN')
        if row.get('binding') != binding:
            raise ValueError('NO_INPUT_ORIGINAL_BINDING_CHANGED')
        if row.get('delivery_lock_identity') != lock_identity:
            raise ValueError('NO_INPUT_ORIGINAL_LOCK_CHANGED')
        native = row.get('native_binding', {})
        if (native.get('identity') != binding['identity']
                or native.get('payload_sha256') != binding['payload_sha256']
                or native.get('provider') != 'claude'):
            raise ValueError('NO_INPUT_ORIGINAL_CLAUDE_BINDING_REQUIRED')
        parsed.append(row)
    return parsed


def resume(args, modules=None):
    bridge, native, evidence, journal_io, availability = modules or controller_modules(args.controller)
    pack_path, text_path = Path(args.task_pack), Path(args.text_file)
    pack_raw, text_raw = evidence.read_bytes(pack_path), evidence.read_bytes(text_path)
    text = text_raw.decode('utf-8')
    pack = bridge.validate_task_pack_contract(pack_path, prompt_text=text)
    availability.require_action(pack['task_id'], 'dispatch', pack)
    marker = args.marker or pack['task_id']
    if not isinstance(marker, str) or not marker or marker not in text:
        raise ValueError('NO_INPUT_ORIGINAL_MARKER_REQUIRED')
    if not pack.get('completion_nonce'):
        raise ValueError('NO_INPUT_ORIGINAL_NONCE_REQUIRED')
    if args.apply and not args.history_sha256:
        raise ValueError('NO_INPUT_REVIEWED_HISTORY_SHA256_REQUIRED')
    live = bridge.pin_workspace(args.surface)
    identity = {k: live[k] for k in ('workspace_uuid', 'caller_surface_uuid',
                                    'target_surface_uuid', 'target_pane_uuid')}
    if str(pack.get('executor_uuid', '')).upper() != identity['target_surface_uuid'].upper():
        raise ValueError('NO_INPUT_WRONG_EXECUTOR')
    binding = dict(task_id=pack['task_id'], marker=marker, payload_sha256=sha(text_raw),
                   task_pack_sha256=sha(pack_raw), identity=identity)
    root = Path.home() / '.local/state/multi-agent-collaboration'
    task_root, legacy = root / 'task-dispatch-v1', root / 'deliveries-v1'
    digest = lambda value: sha(value.encode())
    directory = task_root / digest(json.dumps([identity['caller_surface_uuid'], pack['task_id']]))
    paths = (legacy / ('pane-' + digest(identity['workspace_uuid'] + ':' + identity['target_pane_uuid']) + '.lock'),
             legacy / ('target-' + digest(identity['target_surface_uuid']) + '.lock'),
             task_root / ('target-' + digest(identity['target_surface_uuid']) + '.lock'),
             directory / 'delivery.lock')
    receipt = directory / 'receipt.json'
    legacy_path = legacy / (digest(json.dumps([identity['caller_surface_uuid'], marker], sort_keys=True)) + '.json')
    with contextlib.ExitStack() as stack:
        held_locks = []
        for path in paths:
            if not path.exists():
                raise ValueError('NO_INPUT_ORIGINAL_LOCK_MISSING')
            handle = stack.enter_context(evidence.open_regular_lock(path))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(handle.fileno())
            held_locks.append((path, st.st_dev, st.st_ino))
        lock_identity = dict(device=held_locks[-1][1], inode=held_locks[-1][2])
        prior_paths = evidence.attempt_paths(directory)
        records = [(path, evidence.read_bytes(path)) for path in prior_paths]
        rows = validate_history(records, binding, lock_identity)
        plan = history_pin(records)
        if args.history_sha256 and args.history_sha256 != plan['history_sha256']:
            raise ValueError('NO_INPUT_REVIEWED_HISTORY_CHANGED')
        if len(prior_paths) >= evidence.MAX_ATTEMPTS:
            raise ValueError('NO_INPUT_EVIDENCE_BUDGET_REQUIRES_MAINTENANCE')
        expected_paths = list(prior_paths)

        def recheck():
            if (receipt.exists() or receipt.is_symlink()
                    or legacy_path.exists() or legacy_path.is_symlink()):
                raise ValueError('NO_INPUT_RECEIPT_OR_LEGACY_DELIVERY_EXISTS')
            if evidence.attempt_paths(directory) != expected_paths:
                raise ValueError('NO_INPUT_HISTORY_CHANGED')
            if (evidence.read_bytes(pack_path) != pack_raw
                    or evidence.read_bytes(text_path) != text_raw
                    or any(evidence.read_bytes(path) != raw for path, raw in records)):
                raise ValueError('NO_INPUT_ORIGINAL_BYTES_CHANGED')
            if bridge.pin_workspace(args.surface) != live:
                raise ValueError('NO_INPUT_LIVE_CALLER_CHANGED')
            for path, device, inode in held_locks:
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (device, inode):
                    raise ValueError('NO_INPUT_LOCK_CHANGED')
            for row in rows:
                native.require_bound(bridge, args.surface, row['native_binding'], text)
            availability.require_action(pack['task_id'], 'dispatch', pack)

        recheck()
        before = bridge.read_screen(args.surface)
        bridge.require_agent_input(before, args.surface)
        if not bridge.compose_block_is_empty(before) or bridge.receiver_cannot_submit_now(before):
            raise ValueError('NO_INPUT_RECEIVER_NOT_READY: preserve the original draft')
        recheck()
        plan.update(schema='task-zero-input-resume-v1', controller=str(args.controller),
                    journal=str(directory), binding=binding, completion_nonce=pack['completion_nonce'])
        if not args.apply:
            return dict(status='READY_ZERO_INPUT', plan=plan, input_operations=0)
        attempt_path = directory / ('attempt-%04d.json' % (len(prior_paths) + 1))
        attempt = dict(binding=binding, phase='PREPARED', events=[], started_at_epoch=time.time(),
                       delivery_lock_identity=lock_identity, zero_input_resume=plan)
        # Reserve exactly one new slot; every input intent is durable before I/O.
        fd = os.open(attempt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(attempt, f); f.flush(); os.fsync(f.fileno())
        expected_paths.append(attempt_path)

        def observe(phase, screen=None, *, evidence=None):
            recheck()
            fields = native.input_event_fields(attempt['native_binding'], text, phase, evidence)
            event = dict(fields, phase=phase, at_epoch=time.time())
            if phase == 'PASTE_INTENT':
                event['native_paste_fence'] = native.capture_paste_fence(attempt['native_binding'])
                event['at_epoch'] = time.time()
            if screen is not None:
                event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
            attempt['phase'] = phase
            attempt['events'].append(event)
            journal_io.write_json(attempt_path, attempt)

        try:
            attempt['native_binding'] = native.bind_target(bridge, args.surface, text)
            original_native = rows[-1]['native_binding']
            if any(attempt['native_binding'][k] != original_native[k]
                   for k in ('provider', 'session_id', 'process', 'tty', 'identity')):
                raise ValueError('NO_INPUT_ORIGINAL_RUNTIME_CHANGED')
            journal_io.write_json(attempt_path, attempt)
            recheck()
            result = bridge.submit_text(args.surface, text, marker=marker,
                task_pack_path=str(pack_path), delivery_observer=observe,
                native_binding=attempt['native_binding'], native_attempt=attempt_path)
            if result.get('confirmed') is not True:
                raise bridge.DispatchUnconfirmed('NO_INPUT_RESUME_NATIVE_RECEIPT_PENDING')
            recheck()
            if not native.receipt_evidence(bridge, args.surface, text, attempt, result):
                raise bridge.DispatchUnconfirmed('NO_INPUT_RESUME_NATIVE_PROOF_REQUIRED')
            attempt.update(phase='CONFIRMED', ended_at_epoch=time.time())
            journal_io.write_json(attempt_path, attempt)
            result = dict(result, binding=binding, attempt=str(attempt_path),
                          zero_input_resume=plan, at_epoch=time.time())
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(result, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
            return dict(result, receipt=str(receipt), original_attempts_preserved=True)
        except BaseException as exc:
            if attempt['phase'] == 'PREPARED':
                attempt['phase'] = 'NO_INPUT'
            attempt.update(error=str(exc), ended_at_epoch=time.time())
            journal_io.write_json(attempt_path, attempt)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('controller', 'surface', 'task-pack', 'text-file'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--marker')
    parser.add_argument('--history-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(resume(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='NOT_CONFIRMED', error=str(exc)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
