#!/usr/bin/env python3
"""Repair one rejected NO_INPUT task notice using its complete original controller.

This explicit coordinator never edits an original attempt or task pack. It pins
the rejected text and appends the original journal's sole second attempt, using
the original release's bridge/native reader for all terminal actions and proof.
The default is read-only. It does not recover a pasted, queued or uncertain send.
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import time


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def controller_modules(controller):
    """Verify the complete immutable controller before importing any member."""
    source = Path(controller)
    base = Path.home() / '.local/share/multi-agent-collaboration/releases'
    relative = source.relative_to(base)
    if len(relative.parts) != 2 or relative.parts[-1] != 'source' or source.resolve() != source:
        raise ValueError('REPAIR_CONTROLLER_NOT_IMMUTABLE_RELEASE')
    manifest = json.loads((source.parent / 'MANIFEST.json').read_text())
    for name, pin in manifest['files'].items():
        path = source / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink():
            raise ValueError('REPAIR_CONTROLLER_MEMBER_INVALID')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
            raise ValueError('REPAIR_CONTROLLER_WRITABLE')
        raw = path.read_bytes()
        if len(raw) != pin['bytes'] or sha(raw) != pin['sha256']:
            raise ValueError('REPAIR_CONTROLLER_PIN_CHANGED')
    if any(name in sys.modules for name in ('cmux_bridge', 'cmux_native_delivery', 'cmux_task_journal')):
        raise ValueError('REPAIR_REQUIRES_FRESH_PROCESS')
    # All operational imports below come from the same original release. This
    # script is only a coordinator; it does not replace its modules or validators.
    sys.path.insert(0, str(source / 'scripts'))
    names = ('cmux_bridge', 'cmux_native_delivery', 'cmux_evidence_io',
             'cmux_callback_journal', 'availability_contract')
    modules = [importlib.import_module(name) for name in names]
    if any(Path(m.__file__).resolve().parent != source / 'scripts' for m in modules):
        raise ValueError('REPAIR_MIXED_CONTROLLER')
    return modules


def eligible(attempt, *, original_sha, raw, pack_sha, original_text, identity):
    """Only a first, provably zero-input wire-format rejection can be repaired."""
    if sha(raw) != original_sha:
        raise ValueError('REPAIR_ORIGINAL_ATTEMPT_CHANGED')
    if (attempt.get('phase') != 'NO_INPUT' or attempt.get('events') != []
            or not attempt.get('error', '').startswith('LONG_MESSAGE_REFERENCE_REQUIRED:')):
        raise ValueError('REPAIR_REQUIRES_WIRE_REJECTION_WITH_ZERO_EVENTS')
    binding = attempt.get('binding', {})
    if (binding.get('task_pack_sha256') != pack_sha
            or binding.get('payload_sha256') != sha(original_text.encode())
            or binding.get('identity') != identity):
        raise ValueError('REPAIR_ORIGINAL_BINDING_CHANGED')
    native = attempt.get('native_binding', {})
    if native.get('identity') != identity or native.get('payload_sha256') != sha(original_text.encode()):
        raise ValueError('REPAIR_ORIGINAL_NATIVE_BINDING_REQUIRED')


def repair(args, modules=None):
    bridge, native, evidence, journal_io, availability = modules or controller_modules(args.controller)
    pack_path = Path(args.task_pack)
    original = Path(args.original_attempt)
    original_text = evidence.read_bytes(Path(args.original_text_file)).decode('utf-8')
    pack_raw = evidence.read_bytes(pack_path)
    pack = bridge.validate_task_pack_contract(pack_path, prompt_text=original_text)
    availability.require_action(pack['task_id'], 'dispatch', pack)
    text = bridge.task_pack_notice(pack_path)
    marker = pack['task_id']
    live = bridge.pin_workspace(args.surface)
    identity = {k: live[k] for k in ('workspace_uuid', 'caller_surface_uuid', 'target_surface_uuid', 'target_pane_uuid')}
    if str(pack.get('executor_uuid', '')).upper() != identity['target_surface_uuid'].upper():
        raise ValueError('REPAIR_WRONG_EXECUTOR')
    digest = lambda value: sha(value.encode())
    root = Path.home() / '.local/state/multi-agent-collaboration'
    task_root = root / 'task-dispatch-v1'
    directory = task_root / digest(json.dumps([identity['caller_surface_uuid'], pack['task_id']]))
    if original != directory / 'attempt-0001.json':
        raise ValueError('REPAIR_REQUIRES_ORIGINAL_FIRST_ATTEMPT')
    original_raw = evidence.read_bytes(original)
    old = json.loads(original_raw)
    eligible(old, original_sha=args.original_attempt_sha256, raw=original_raw,
             pack_sha=sha(pack_raw), original_text=original_text, identity=identity)
    legacy = root / 'deliveries-v1'
    paths = (legacy / ('pane-' + digest(identity['workspace_uuid'] + ':' + identity['target_pane_uuid']) + '.lock'),
             legacy / ('target-' + digest(identity['target_surface_uuid']) + '.lock'),
             task_root / ('target-' + digest(identity['target_surface_uuid']) + '.lock'), directory / 'delivery.lock')
    with contextlib.ExitStack() as stack:
        held_locks = []
        for path in paths:
            if not path.exists():
                raise ValueError('REPAIR_ORIGINAL_LOCK_MISSING')
            handle = stack.enter_context(evidence.open_regular_lock(path))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(handle.fileno())
            held_locks.append((path, st.st_dev, st.st_ino))
        lock_identity = dict(device=held_locks[-1][1], inode=held_locks[-1][2])
        if old.get('delivery_lock_identity') != lock_identity:
            raise ValueError('REPAIR_ORIGINAL_LOCK_CHANGED')

        def recheck():
            if evidence.read_bytes(original) != original_raw or evidence.read_bytes(pack_path) != pack_raw:
                raise ValueError('REPAIR_ORIGINAL_BYTES_CHANGED')
            if bridge.pin_workspace(args.surface) != live:
                raise ValueError('REPAIR_LIVE_CALLER_CHANGED')
            for path, device, inode in held_locks:
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (device, inode):
                    raise ValueError('REPAIR_LOCK_CHANGED')
            native.require_bound(bridge, args.surface, old['native_binding'], original_text)
            availability.require_action(pack['task_id'], 'dispatch', pack)

        recheck()
        if evidence.attempt_paths(directory) != [original] or (directory / 'receipt.json').exists():
            raise ValueError('REPAIR_RETRY_BUDGET_USED_OR_RECEIPT_EXISTS')
        legacy_key = digest(json.dumps([identity['caller_surface_uuid'], old['binding']['marker']], sort_keys=True))
        if (legacy / (legacy_key + '.json')).exists():
            raise ValueError('REPAIR_LEGACY_DELIVERY_EXISTS')
        before = bridge.read_screen(args.surface)
        bridge.require_agent_input(before, args.surface)
        if not bridge.compose_block_is_empty(before) or bridge.receiver_cannot_submit_now(before):
            raise ValueError('REPAIR_RECEIVER_NOT_READY_ZERO_INPUT')
        repair_pin = dict(schema='no-input-task-notice-repair-v1', original_attempt=str(original),
                          original_attempt_sha256=sha(original_raw), original_controller=str(args.controller),
                          task_pack_sha256=sha(pack_raw), original_payload_sha256=sha(original_text.encode()),
                          corrected_payload_sha256=sha(text.encode()), corrected_marker=marker,
                          task_id=pack['task_id'], completion_nonce=pack['completion_nonce'])
        if not args.apply:
            return dict(status='READY_ZERO_INPUT', repair=repair_pin, text=text, input_operations=0)
        # A second slot is durable before any input; a crash never resets it.
        attempt_path = directory / 'attempt-0002.json'
        binding = dict(old['binding'], marker=marker, payload_sha256=sha(text.encode()))
        attempt = dict(binding=binding, phase='PREPARED', events=[], started_at_epoch=time.time(),
                       delivery_lock_identity=lock_identity, notice_repair=repair_pin)
        fd = os.open(attempt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(attempt, f); f.flush(); os.fsync(f.fileno())

        def observe(phase, screen=None):
            recheck()
            fence = native.capture_paste_fence(attempt['native_binding']) if phase == 'PASTE_INTENT' else None
            event = dict(phase=phase, at_epoch=time.time())
            if fence is not None:
                event['native_paste_fence'] = fence
            if screen is not None:
                event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
            attempt['phase'] = phase
            attempt['events'].append(event)
            journal_io.write_json(attempt_path, attempt)

        try:
            attempt['native_binding'] = native.bind_target(bridge, args.surface, text)
            journal_io.write_json(attempt_path, attempt)
            recheck()
            result = bridge.submit_text(args.surface, text, marker=marker,
                task_pack_path=str(pack_path), delivery_observer=observe,
                native_binding=attempt['native_binding'], native_attempt=attempt_path)
            if result.get('confirmed') is not True:
                raise bridge.DispatchUnconfirmed('REPAIR_NATIVE_RECEIPT_PENDING')
            recheck()
            if not native.receipt_evidence(bridge, args.surface, text, attempt, result):
                raise bridge.DispatchUnconfirmed('REPAIR_NATIVE_PROOF_REQUIRED')
            attempt.update(phase='CONFIRMED', ended_at_epoch=time.time())
            journal_io.write_json(attempt_path, attempt)
            result = dict(result, binding=binding, attempt=str(attempt_path), notice_repair=repair_pin, at_epoch=time.time())
            receipt = directory / 'receipt.json'
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(result, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
            return dict(result, receipt=str(receipt), original_attempt_preserved=True)
        except BaseException as exc:
            if attempt['phase'] == 'PREPARED':
                attempt['phase'] = 'NO_INPUT'
            attempt.update(error=str(exc), ended_at_epoch=time.time())
            journal_io.write_json(attempt_path, attempt)
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ('controller', 'surface', 'task-pack', 'original-attempt', 'original-attempt-sha256', 'original-text-file'):
        p.add_argument('--' + arg, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    try:
        print(json.dumps(repair(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='NOT_CONFIRMED', error=str(exc)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
