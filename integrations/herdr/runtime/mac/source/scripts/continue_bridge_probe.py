#!/usr/bin/env python3
"""Continue one original, never-typed bridge token under its original controller.

Default is a static, read-only plan. Only the original supervisor can --apply,
after its original harness has exited. One durable slot precedes the probe;
partial results never authorize a replay. No handshake or task is submitted.
"""
import argparse
import contextlib
from datetime import datetime
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

import reconcile_handshake_ack as recovery

ARCHIVE = 'bridge-probe-continuation'
FILES = ('identity-gate.json', 'bridge-test-evidence.json',
         'naming-proof.json', 'helper-parity.json', 'validation.json')


def original_modules(controller):
    modules = recovery.original_modules(controller)
    mutex = importlib.import_module('cmux_input_mutex')
    if Path(mutex.__file__).resolve().parent != Path(controller) / 'scripts':
        raise ValueError('BRIDGE_CONTINUATION_MIXED_CONTROLLER')
    for name in ('_bridge_test_one_locked', '_write_bridge_evidence'):
        if not callable(getattr(modules[-1], name, None)):
            raise ValueError('BRIDGE_CONTINUATION_UNSUPPORTED_CONTROLLER')
    return (*modules, mutex)


def absent(path):
    return not os.path.lexists(path)


def digest(raw):
    return recovery.observation.digest(raw)


def eligible(harness, gate, original):
    rows = original.get('executors')
    peers = harness._gate_executors(gate)
    layout_ok = gate.get('side_panel') is True
    if 'terminal_channels_version' in gate:
        from cmux_terminal_channels import check_gate
        layout_ok = check_gate(gate)['pass']
    if (gate.get('status') != 'PASS' or not layout_ok
            or len(peers) != 1 or peers[0].get('provider') != 'claude'
            or not isinstance(rows, list) or len(rows) != 1
            or {k: v for k, v in original.items() if k != 'executors'} != rows[0]):
        raise ValueError('BRIDGE_ORIGINAL_SINGLE_CLAUDE_REQUIRED')
    row, peer = rows[0], peers[0]
    if (row.get('task_id') != gate.get('task_id') or not gate.get('task_id')
            or row.get('executor') != peer['surface_ref']
            or type(row.get('ordinal')) is not int or row['ordinal'] != 1
            or not re.fullmatch(r'B1_[0-9a-f]{8}', str(row.get('token', '')))
            or any(row.get(k) != 'COMPOSE_OCCUPIED' for k in ('status', 'submission_state'))
            or any(row.get(k) is not False for k in
                   ('token_sent', 'clear_confirmed', 'observed_in_screen'))
            or row.get('pre_read_performed') is not True
            or any(type(row.get(k)) is not int or row[k] != 0 for k in
                   ('clear_attempts', 'clear_key_count'))
            or row.get('clear_verified_at') is not None):
        raise ValueError('BRIDGE_ORIGINAL_NEVER_TYPED_TOKEN_REQUIRED')
    override = row.get('override', {})
    if (override.get('force_compose_override') is not False
            or any(override.get(k) for k in
                   ('clear_actions', 'delete_count', 'clear_confirmed',
                    'direct_replace_after_clear_attempts'))):
        raise ValueError('BRIDGE_ORIGINAL_ZERO_OVERRIDE_REQUIRED')
    participants = gate.get('native_participants', [])
    if (not isinstance(participants, list) or len(participants) != 2
            or [(p.get('role'), p.get('provider'), p.get('surface_uuid')) for p in participants]
            != [('supervisor', 'codex', gate.get('supervisor_surface_uuid')),
                ('executor', 'claude', peer.get('surface_uuid'))]
            or not all(p.get('surface_uuid') and p.get('native_session_id') for p in participants)):
        raise ValueError('BRIDGE_ORIGINAL_NATIVE_PARTICIPANTS_REQUIRED')
    return peer, row


def check_reservation(marker, gate, root, reservation_id):
    expected = [dict(p, ordinal=i) for i, p in enumerate(gate['native_participants'])]
    keys = ('role', 'ordinal', 'provider', 'surface_uuid', 'native_session_id')
    if (marker.get('marker_version') != 2 or marker.get('collaboration_id') != reservation_id
            or marker.get('task_id') != gate['task_id'] or marker.get('artifact_root') != str(root)
            or any(marker.get(k) != gate['workspace_uuid'] for k in ('workspace_id', 'workspace_uuid'))
            or [{k: p.get(k) for k in keys} for p in marker.get('participants', [])]
            != [{k: p.get(k) for k in keys} for p in expected]):
        raise ValueError('BRIDGE_ORIGINAL_RESERVATION_CHANGED')
    armed = datetime.fromisoformat(marker['armed_at'])
    ttl = marker.get('ttl_seconds')
    if (armed.tzinfo is None or type(ttl) not in (int, float) or not math.isfinite(ttl)
            or ttl <= 0 or not 0 <= time.time() - armed.timestamp() <= ttl):
        raise ValueError('BRIDGE_RESERVATION_EXPIRED_OR_INVALID_OWNER_SETTLEMENT_REQUIRED')


@contextlib.contextmanager
def root_lock(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = os.fstat(fd)

        def check():
            live = root.lstat()
            if (not stat.S_ISDIR(live.st_mode)
                    or (live.st_dev, live.st_ino) != (held.st_dev, held.st_ino)):
                raise ValueError('BRIDGE_TASK_DIRECTORY_CHANGED')
        check()
        yield fd, check
        check()
    finally:
        os.close(fd)


def require_ready(bridge, harness, target, token, lines):
    screen = bridge.read_screen(target, lines=lines)
    if not bridge.compose_block_is_empty(screen):
        raise ValueError('BRIDGE_RECEIVER_NOT_READY_PRESERVE_COMPOSE')
    if bridge._queued_or_active_input(screen):
        # Retain exactly the original controller's native interruption exception.
        boundary = harness.interrupted_probe.InterruptedBashBoundary(bridge, target, screen, token)
        if not boundary.check(screen):
            raise ValueError('BRIDGE_RECEIVER_NOT_READY_WAIT_FOR_IDLE')


def continue_probe(args, modules=None):
    result = dict(schema='original-bridge-probe-continuation-v1',
        status='BRIDGE_CONTINUATION_UNAVAILABLE', controller=str(args.controller),
        input_operations=0, submission_operations=0, probe_invocations=0,
        archive_created=False, bridge_write_attempts=0, bridge_writes=0,
        handshake='NOT_ATTEMPTED', business_acceptance='NOT_EVALUATED')
    output, errors = io.StringIO(), io.StringIO()
    try:
        bridge, native, evidence, _, harness, mutex = modules or original_modules(args.controller)
        root = Path(args.artifact_root)
        if (not root.is_absolute() or root.resolve() != root or not root.is_dir()
                or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in
                       (args.identity_sha256, args.bridge_sha256))
                or not re.fullmatch(r'[0-9a-fA-F-]{36}', args.reservation_id)):
            raise ValueError('BRIDGE_ORIGINAL_PATHS_AND_PINS_REQUIRED')
        archive = root / ARCHIVE
        result['archive'] = str(archive)
        if not absent(archive):
            raise ValueError('BRIDGE_CONTINUATION_SLOT_EXISTS_NO_REPLAY')
        pins = {name: recovery.optional_snapshot(evidence, root / name) for name in FILES}
        if (pins['identity-gate.json'] is None or pins['bridge-test-evidence.json'] is None
                or digest(pins['identity-gate.json'][0]) != args.identity_sha256
                or digest(pins['bridge-test-evidence.json'][0]) != args.bridge_sha256):
            raise ValueError('BRIDGE_ORIGINAL_EVIDENCE_PIN_CHANGED')
        gate = json.loads(pins['identity-gate.json'][0])
        peer, row = eligible(harness, gate, json.loads(pins['bridge-test-evidence.json'][0]))
        marker_path = harness._ACTIVE_DIR / gate['workspace_uuid'] / (args.reservation_id + '.json')
        marker_pin = evidence.snapshot(marker_path)
        marker = json.loads(marker_pin[0])
        check_reservation(marker, gate, root, args.reservation_id)
        stable_marker = {k: v for k, v in marker.items() if k != 'last_activity_at'}
        request = dict(task_id=gate['task_id'], artifact_root=str(root),
            controller=str(args.controller), identity_sha256=args.identity_sha256,
            bridge_sha256=args.bridge_sha256, reservation_id=args.reservation_id,
            executor_uuid=peer['surface_uuid'], token=row['token'])
        result['request'] = request

        def check_files(*, published=None):
            for name, pin in pins.items():
                wanted = published if name == 'bridge-test-evidence.json' and published is not None else pin
                if recovery.optional_snapshot(evidence, root / name) != wanted:
                    raise ValueError('BRIDGE_ORIGINAL_EVIDENCE_CHANGED_DURING_CONTINUATION')
            if (not absent(root / 'handshake-receipt.json')
                    or any(root.glob('*task-pack*.json'))):
                raise ValueError('BRIDGE_HANDSHAKE_OR_TASK_PACK_ALREADY_EXISTS')
            live_marker = json.loads(evidence.read_bytes(marker_path))
            check_reservation(live_marker, gate, root, args.reservation_id)
            if {k: v for k, v in live_marker.items() if k != 'last_activity_at'} != stable_marker:
                raise ValueError('BRIDGE_RESERVATION_MUTATED_DURING_CONTINUATION')
            return live_marker

        check_files()
        if not args.apply:
            result.update(status='STATIC_ELIGIBLE_NO_INPUT', live_readiness='NOT_OBSERVED',
                apply_requires_original_supervisor=True, original_harness_exit_required=True)
            return result
        if not args.original_harness_exited:
            raise ValueError('BRIDGE_ORIGINAL_HARNESS_EXIT_ATTESTATION_REQUIRED')
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result['supervisor_thread'] = recovery.require_supervisor(harness, gate,
                proof_out=result.setdefault('supervisor_identity', {}))
            binding = native.bind_target(bridge, peer['surface_uuid'], row['token'])
            expected = gate['native_participants'][1]
            if (binding['session_id'] != expected['native_session_id']
                    or binding['provider'] != expected['provider']
                    or any(binding['identity'].get(k) != v for k, v in dict(
                        workspace_uuid=gate['workspace_uuid'],
                        caller_surface_uuid=gate['supervisor_surface_uuid'],
                        target_surface_uuid=peer['surface_uuid'],
                        target_pane_uuid=gate['workspace_proofs'][peer['surface_ref']]['target_pane_uuid']).items())):
                raise ValueError('BRIDGE_ORIGINAL_EXECUTOR_SESSION_CHANGED')
            with root_lock(root) as (root_fd, check_root), mutex.hold(binding['identity']) as check_mutex:
                check_archive = lambda: None

                def check(*, published=None):
                    check_root()
                    check_mutex()
                    check_archive()
                    check_files(published=published)
                    recovery.require_supervisor(harness, gate)
                    native.require_bound(bridge, peer['surface_uuid'], binding, row['token'])

                check()
                if not absent(archive):
                    raise ValueError('BRIDGE_CONTINUATION_SLOT_EXISTS_NO_REPLAY')
                require_ready(bridge, harness, peer['surface_ref'], row['token'], args.lines)
                check()
                os.mkdir(ARCHIVE, mode=0o700, dir_fd=root_fd)
                result['archive_created'] = True
                os.fsync(root_fd)
                archive_fd = os.open(ARCHIVE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    archive_stat = os.fstat(archive_fd)
                    archive_pins = {}

                    def check_archive():
                        check_root()
                        live = archive.lstat()
                        if (not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino)
                                != (archive_stat.st_dev, archive_stat.st_ino)):
                            raise ValueError('BRIDGE_ARCHIVE_DIRECTORY_CHANGED')
                        for name, pin in archive_pins.items():
                            if evidence.snapshot(archive / name) != pin:
                                raise ValueError('BRIDGE_ARCHIVE_FILE_CHANGED')

                    def write(name, raw):
                        check_archive()
                        recovery.write_exclusive(archive_fd, name, raw)
                        pin = evidence.snapshot(archive / name)
                        if pin[0] != raw:
                            raise ValueError('BRIDGE_ARCHIVE_WRITE_UNCONFIRMED')
                        archive_pins[name] = pin

                    try:
                        write('INTENT.json', recovery.json_bytes(dict(schema=result['schema'],
                            request=request, native_binding=binding,
                            original_harness_exit='ATTESTED_BY_ORIGINAL_SUPERVISOR',
                            supervisor_identity=result['supervisor_identity'],
                            original_files={k: recovery.pin_summary(v) for k, v in pins.items()},
                            original_reservation_path=str(marker_path),
                            original_reservation=recovery.pin_summary(marker_pin))))
                        for name, pin in pins.items():
                            if pin is not None:
                                write(name + '.original', pin[0])
                        write('reservation.json.original', marker_pin[0])
                        check()
                        # This fence belongs to this probe, never to the historical refusal.
                        write('PROBE-INTENT.json', recovery.json_bytes(dict(
                            at_epoch=time.time(), native_paste_fence=native.capture_paste_fence(binding))))
                        check()
                        result.update(probe_invocations=1, input_operations=None)
                        observed, ok = harness._bridge_test_one_locked(
                            SimpleNamespace(task_id=gate['task_id'], lines=args.lines, force_compose=False),
                            peer['surface_ref'], row['token'], 1, check)
                        result['probe'] = observed
                        if observed.get('token_sent') is False:
                            result['input_operations'] = 0
                        write('PROBE-RESULT.json', recovery.json_bytes(dict(evidence=observed, ok=ok)))
                        check()
                        if not ok:
                            result['status'] = 'BRIDGE_CONTINUATION_FAILED_NO_REPLAY'
                        else:
                            if (observed.get('token') != row['token'] or observed.get('token_sent') is not True
                                    or observed.get('clear_confirmed') is not True):
                                raise ValueError('BRIDGE_ORIGINAL_PROBE_RESULT_INVALID')
                            result['bridge_write_attempts'] = 1
                            try:
                                harness._write_bridge_evidence(root, [observed])
                            finally:
                                after = evidence.snapshot(root / 'bridge-test-evidence.json')
                                result['bridge_writes'] = int(after != pins['bridge-test-evidence.json'])
                                result['published_bridge'] = recovery.pin_summary(after)
                                result['heartbeat_after'] = json.loads(evidence.read_bytes(marker_path)).get('last_activity_at')
                            if json.loads(after[0]) != dict(observed, executors=[observed]):
                                raise ValueError('BRIDGE_ORIGINAL_WRITER_SHAPE_CHANGED')
                            check(published=after)
                            result.update(status='BRIDGE_CONTINUED', original_failure_archived=True,
                                original_reservation_terms_preserved=True,
                                next_action='original supervisor runs original-controller handshake in this root')
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
                        result.update(status='BRIDGE_CONTINUATION_INCOMPLETE_NO_REPLAY', error=str(exc))
                    result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())
                    write('RESULT.json', recovery.json_bytes(result))
                finally:
                    os.close(archive_fd)
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        result.update(status='BRIDGE_CONTINUATION_INCOMPLETE_NO_REPLAY' if result['archive_created']
                      else 'BRIDGE_CONTINUATION_UNAVAILABLE', error=str(exc))
        return result
    finally:
        if output.getvalue() or errors.getvalue():
            result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('controller', 'artifact-root', 'identity-sha256', 'bridge-sha256', 'reservation-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--lines', type=recovery.observation.bounded_integer(1, 2000), default=200)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--original-harness-exited', action='store_true')
    result = continue_probe(parser.parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('STATIC_ELIGIBLE_NO_INPUT', 'BRIDGE_CONTINUED') else 2


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
