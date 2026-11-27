#!/usr/bin/env python3
"""Continue only the next never-started peer of an original partial handshake.

The original supervisor applies a SHA-pinned preview under the complete original
controller. A permanent slot precedes its single handshake invocation. Existing
nonces, attempts and reservations are neither replayed nor replaced.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import handshake_panel as panel
import reconcile_handshake_ack as recovery
from continue_bridge_probe import root_lock, check_reservation

FILES = ('identity-gate.json', 'handshake-receipt.json', 'naming-proof.json',
         'bridge-test-evidence.json', 'validation.json')


def eligible(harness, gate, receipt):
    peers = harness._gate_executors(gate)
    rows = receipt.get('executors')
    if (gate.get('status') != 'PASS' or not isinstance(rows, list)
            or len(rows) != 1 or len(peers) != 2
            or receipt != panel.aggregate(rows, peers)):
        raise ValueError('HANDSHAKE_CONTINUATION_PARTIAL_PANEL_REQUIRED')
    if len({p['surface_uuid'] for p in peers}) != len(peers):
        raise ValueError('HANDSHAKE_CONTINUATION_DUPLICATE_PEER')
    for index, (row, peer) in enumerate(zip(rows, peers), 1):
        nonce = row.get('ack_nonce')
        expected = f"PREFLIGHT_ACK|{gate['task_id']}|{peer['provider']}:identity|READY|INLINE|{nonce}"
        if (not panel.matches(row, peer) or row.get('ordinal') != index
                or row.get('task_id') != gate['task_id']
                or row.get('supervisor') != gate.get('supervisor')
                or row.get('workspace_ref') != gate.get('workspace_ref')
                or row.get('executor_provider') != peer['provider']
                or not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{8}', nonce)
                or row.get('status') != 'PASS' or row.get('lifecycle') != 'ACKED'
                or row.get('executor_ack') is not True
                or row.get('ack_line') != expected or row.get('ack_line_expected') != expected
                or row.get('ack_source') != 'executor_response_nonce'
                or row.get('ack_response_marker') is not True):
            raise ValueError('HANDSHAKE_CONTINUATION_PREFIX_ACK_REQUIRED')
    participants = gate.get('native_participants', [])
    expected = [('supervisor', 'codex', gate.get('supervisor_surface_uuid'))] + [
        ('executor', p['provider'], p['surface_uuid']) for p in peers]
    if ([(p.get('role'), p.get('provider'), p.get('surface_uuid')) for p in participants] != expected
            or not all(p.get('native_session_id') for p in participants)):
        raise ValueError('HANDSHAKE_CONTINUATION_NATIVE_PANEL_REQUIRED')
    return peers, rows, peers[len(rows)]


def continue_handshake(args, modules=None):
    result = dict(schema='original-handshake-continuation-v1',
        status='HANDSHAKE_CONTINUATION_UNAVAILABLE', input_operations=0,
        handshake_invocations=0, archive_created=False, business_acceptance='NOT_EVALUATED')
    output, errors = io.StringIO(), io.StringIO()
    try:
        bridge, native, evidence, journal, harness = modules or recovery.original_modules(args.controller)
        root = Path(args.artifact_root)
        if (not root.is_absolute() or root.resolve() != root or not root.is_dir()
                or any(not re.fullmatch(r'[0-9a-f]{64}', value)
                       for value in (args.identity_sha256, args.receipt_sha256))):
            raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_PINS_REQUIRED')
        with root_lock(root) as (root_fd, check_root):
            pins = {name: recovery.optional_snapshot(evidence, root / name) for name in FILES}
            if (pins['identity-gate.json'] is None or pins['handshake-receipt.json'] is None
                    or recovery.observation.digest(pins['identity-gate.json'][0]) != args.identity_sha256
                    or recovery.observation.digest(pins['handshake-receipt.json'][0]) != args.receipt_sha256):
                raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_PINS_CHANGED')
            gate = json.loads(pins['identity-gate.json'][0])
            peers, rows, peer = eligible(harness, gate, json.loads(pins['handshake-receipt.json'][0]))
            archive_name = 'handshake-continuation-' + str(peer['ordinal'])
            archive = root / archive_name
            if os.path.lexists(archive):
                raise ValueError('HANDSHAKE_CONTINUATION_SLOT_EXISTS_NO_REPLAY')
            marker_path = harness._ACTIVE_DIR / gate['workspace_uuid'] / (args.reservation_id + '.json')
            marker_pin = evidence.snapshot(marker_path)
            marker = json.loads(marker_pin[0])
            check_reservation(marker, gate, root, args.reservation_id)
            terms = {k: v for k, v in marker.items() if k != 'last_activity_at'}
            attempt_pins = {}
            ack_checks = []
            for row, member in zip(rows, peers):
                path = Path(row['dispatch_attempt']['path'])
                pin = evidence.snapshot(path)
                expected_sha = (row.get('late_ack_reconciliation') or {}).get(
                    'original_attempt_sha256', row['dispatch_attempt']['sha256'])
                if recovery.observation.digest(pin[0]) != expected_sha:
                    raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_ATTEMPT_CHANGED')
                attempt = json.loads(pin[0])
                binding = attempt['native_binding']
                text = row['dispatch_payload']
                participant = gate['native_participants'][member['ordinal']]
                if (binding['identity']['target_surface_uuid'] != member['surface_uuid']
                        or binding['session_id'] != participant['native_session_id']
                        or binding['provider'] != member['provider']
                        or attempt['controller_provenance']['source'] != args.controller
                        or recovery.observation.digest(text.encode()) != row['dispatch_payload_sha256']):
                    raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_BINDING_CHANGED')
                stamp, fence = native._original_intent(attempt, binding, text)
                body_pin = journal.original_body_pin(text, attempt)
                proof = (row.get('late_ack_reconciliation') or {}).get('ack_proof')
                if proof is None:
                    found = recovery.scan_ack(native, binding, text, row['ack_line_expected'], stamp, fence)
                    if not found['observed']:
                        raise ValueError('HANDSHAKE_CONTINUATION_PREFIX_NATIVE_ACK_UNPROVEN')
                    proof = found['proof']
                ack_checks.append((member, binding, text, row['ack_line_expected'], stamp, fence,
                                   proof, attempt, body_pin))
                attempt_pins[path] = pin

            def check(*, receipt=None, validation=None):
                check_root()
                for name, pin in pins.items():
                    expected = (receipt if name == 'handshake-receipt.json' and receipt is not None else
                                validation if name == 'validation.json' and validation is not None else pin)
                    if recovery.optional_snapshot(evidence, root / name) != expected:
                        raise ValueError('HANDSHAKE_CONTINUATION_FILES_CHANGED')
                # Validate the next peer's original diagnostic postcondition
                # before preview and before allocating its one-use slot. This
                # uses no clear-recovery argument and therefore never probes or
                # clears: absent evidence or one clear-confirmed row is needed.
                harness._handshake_bridge_context(
                    SimpleNamespace(task_id=gate['task_id'], bridge_clear_recovery=None),
                    root, gate, peer['surface_ref'])
                if any(root.glob('*task-pack*.json')):
                    raise ValueError('HANDSHAKE_CONTINUATION_TASK_PACK_EXISTS')
                current = json.loads(evidence.read_bytes(marker_path))
                check_reservation(current, gate, root, args.reservation_id)
                if {k: v for k, v in current.items() if k != 'last_activity_at'} != terms:
                    raise ValueError('HANDSHAKE_CONTINUATION_RESERVATION_CHANGED')
                for path, pin in attempt_pins.items():
                    if evidence.snapshot(path) != pin:
                        raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_ATTEMPT_CHANGED')
                for member, binding, text, expected, stamp, fence, proof, attempt, body_pin in ack_checks:
                    native.require_bound(bridge, member['surface_uuid'], binding, text, read_only=not args.apply)
                    recovery.require_ack_proof(native, binding, text, expected, stamp, fence, proof)
                    if journal.original_body_pin(text, attempt) != body_pin:
                        raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_BODY_CHANGED')
                if args.apply:
                    recovery.require_supervisor(harness, gate)

            check()
            result.update(executor_uuid=peer['surface_uuid'], ordinal=peer['ordinal'],
                          original_nonces=[row['ack_nonce'] for row in rows], archive=str(archive))
            if not args.apply:
                result.update(status='READY_ORIGINAL_HANDSHAKE_CONTINUATION',
                              apply_requires_original_supervisor=True)
                return result
            if not args.original_harness_exited:
                raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_HARNESS_EXIT_REQUIRED')
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                harness._recheck_workspace_gate(gate)
                probe_text = 'STATUS: Original partial handshake continuation identity check'
                binding = native.bind_target(bridge, peer['surface_uuid'], probe_text)
                participant = gate['native_participants'][peer['ordinal']]
                if (binding['session_id'] != participant['native_session_id']
                        or binding['provider'] != participant['provider']
                        or any(binding['identity'].get(k) != v for k, v in dict(
                            workspace_uuid=gate['workspace_uuid'],
                            caller_surface_uuid=gate['supervisor_surface_uuid'],
                            target_surface_uuid=peer['surface_uuid']).items())):
                    raise ValueError('HANDSHAKE_CONTINUATION_NEXT_SESSION_CHANGED')
                check()
                os.mkdir(archive_name, 0o700, dir_fd=root_fd)
                result['archive_created'] = True
                os.fsync(root_fd)
                fd = os.open(archive_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    archive_stat = os.fstat(fd)
                    archive_pins = {}
                    def check_archive():
                        live = archive.lstat()
                        if (live.st_dev, live.st_ino, live.st_mode) != (
                                archive_stat.st_dev, archive_stat.st_ino, archive_stat.st_mode):
                            raise ValueError('HANDSHAKE_CONTINUATION_ARCHIVE_CHANGED')
                        for name, pin in archive_pins.items():
                            if evidence.snapshot(archive / name) != pin:
                                raise ValueError('HANDSHAKE_CONTINUATION_ARCHIVE_CHANGED')
                    def write(name, raw):
                        check_archive()
                        recovery.write_exclusive(fd, name, raw)
                        pin = evidence.snapshot(archive / name)
                        if pin[0] != raw:
                            raise ValueError('HANDSHAKE_CONTINUATION_ARCHIVE_WRITE_UNCONFIRMED')
                        archive_pins[name] = pin
                    write('INTENT.json', recovery.json_bytes(dict(
                        schema=result['schema'], controller=args.controller,
                        receipt_sha256=args.receipt_sha256, identity_sha256=args.identity_sha256,
                        executor=peer, native_binding=binding, reservation_id=args.reservation_id,
                        original_harness_exit='ATTESTED_BY_ORIGINAL_SUPERVISOR')))
                    for name, pin in pins.items():
                        if pin is not None:
                            write(name + '.original', pin[0])
                    write('reservation.json.original', marker_pin[0])
                    check()
                    check_archive()
                    native.require_bound(bridge, peer['surface_uuid'], binding, probe_text)
                    result.update(handshake_invocations=1, input_operations=None)
                    work = json.loads(json.dumps(rows))
                    ok = harness._handshake_one(SimpleNamespace(task_id=gate['task_id'],
                        lines=args.lines, timeout=None, handshake_timeout=args.handshake_timeout,
                        force_compose=False, bridge_clear_recovery=None), root, gate, peer, work)
                    if (work[:len(rows)] != rows or len(work) != len(rows) + 1
                            or not panel.matches(work[-1], peer)):
                        raise ValueError('HANDSHAKE_CONTINUATION_ORIGINAL_PREFIX_CHANGED')
                    harness._write_handshake_receipt(root, work)
                    published = evidence.snapshot(root / 'handshake-receipt.json')
                    # Old writers do not count never-started peers. Keep the panel
                    # incomplete even when one invocation succeeds before a third peer.
                    expected = panel.aggregate(work, peers)
                    if json.loads(published[0]) != expected:
                        harness._write(root / 'handshake-receipt.json', expected)
                        published = evidence.snapshot(root / 'handshake-receipt.json')
                    check(receipt=published)
                    native.require_bound(bridge, peer['surface_uuid'], binding, probe_text)
                    try:
                        harness.cmd_validate(SimpleNamespace(task_id=gate['task_id'],
                            artifact_root=str(root), json=False))
                    except SystemExit as exc:
                        if exc.code != 1:
                            raise
                    validation = evidence.snapshot(root / 'validation.json')
                    check(receipt=published, validation=validation)
                    outcome = ('HANDSHAKE_CONTINUED' if ok else
                               'HANDSHAKE_CONTINUED_PENDING_ACK'
                               if work[-1].get('lifecycle') == 'PENDING'
                               and work[-1].get('status') != 'FAIL'
                               else 'HANDSHAKE_CONTINUED_TERMINAL')
                    result.update(status=outcome, executor_lifecycle=work[-1].get('lifecycle'),
                        receipt_sha256=recovery.observation.digest(published[0]),
                        validation=json.loads(validation[0]).get('status'), original_attempts_unchanged=True,
                        next_action='Observe/reconcile the new original nonce; never replay this slot.')
                    write('RESULT.json', recovery.json_bytes(result))
                finally:
                    os.close(fd)
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, SystemExit) as exc:
        result.update(status='HANDSHAKE_CONTINUATION_INCOMPLETE_NO_REPLAY' if result['archive_created']
                      else 'HANDSHAKE_CONTINUATION_UNAVAILABLE', error=str(exc))
        return result
    finally:
        if output.getvalue() or errors.getvalue():
            result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('controller', 'artifact-root', 'identity-sha256', 'receipt-sha256', 'reservation-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--lines', type=recovery.observation.bounded_integer(1, 2000), default=200)
    parser.add_argument('--handshake-timeout', type=recovery.observation.bounded_integer(600, 3600), default=600)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--original-harness-exited', action='store_true')
    result = continue_handshake(parser.parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('READY_ORIGINAL_HANDSHAKE_CONTINUATION', 'HANDSHAKE_CONTINUED') else 2


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
