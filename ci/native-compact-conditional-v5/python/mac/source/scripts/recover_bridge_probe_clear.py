#!/usr/bin/env python3
"""Clear one failed original probe, without pasting or submitting anything.

Preview is static. Apply belongs to the original supervisor and uses the
archived native binding and complete original controller. An exclusive slot
survives interruption; there is no retry/reset/rebind path for uncertain keys.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

import continue_bridge_probe as continuation
import probe_cursor_observation as cursor_observation

recovery = continuation.recovery
ARCHIVE = 'bridge-probe-clear-recovery'
SCHEMA = 'original-bridge-probe-clear-v1'
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError)


def original_plan(args, modules):
    """Only immutable files: no caller, terminal, native-log scan or lock."""
    _, native, evidence, _, harness, _ = modules
    root = Path(args.artifact_root)
    if (not root.is_absolute() or root.resolve() != root or not root.is_dir()
            or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in
                   (args.identity_sha256, args.bridge_sha256))):
        raise ValueError('PROBE_CLEAR_ORIGINAL_PATHS_AND_PINS_REQUIRED')
    archive = root / continuation.ARCHIVE
    if archive.resolve() != archive or not stat.S_ISDIR(archive.lstat().st_mode):
        raise ValueError('PROBE_CLEAR_ORIGINAL_ARCHIVE_REQUIRED')
    names = sorted(p.name for p in archive.iterdir())
    if len(names) > len(continuation.FILES) + 5:
        raise ValueError('PROBE_CLEAR_UNEXPECTED_ARCHIVE_FILES')
    pins = {name: evidence.snapshot(archive / name) for name in names}
    objects = {name: json.loads(pins[name][0]) for name in
               ('INTENT.json', 'PROBE-INTENT.json', 'PROBE-RESULT.json', 'RESULT.json')}
    intent, probe_intent, probe, result = (objects[name] for name in
        ('INTENT.json', 'PROBE-INTENT.json', 'PROBE-RESULT.json', 'RESULT.json'))
    originals = {name: recovery.optional_snapshot(evidence, root / name)
                 for name in continuation.FILES}
    expected_names = {'INTENT.json', 'PROBE-INTENT.json', 'PROBE-RESULT.json',
                      'RESULT.json', 'reservation.json.original'}
    for name, pin in originals.items():
        if intent['original_files'].get(name) != recovery.pin_summary(pin):
            raise ValueError('PROBE_CLEAR_ORIGINAL_FILES_CHANGED')
        if pin is not None:
            expected_names.add(name + '.original')
            if pins[name + '.original'][0] != pin[0]:
                raise ValueError('PROBE_CLEAR_ARCHIVED_FILE_CHANGED')
    if set(names) != expected_names:
        raise ValueError('PROBE_CLEAR_UNEXPECTED_ARCHIVE_FILES')
    if (continuation.digest(originals['identity-gate.json'][0]) != args.identity_sha256
            or continuation.digest(originals['bridge-test-evidence.json'][0]) != args.bridge_sha256):
        raise ValueError('PROBE_CLEAR_ORIGINAL_EVIDENCE_PIN_CHANGED')
    gate = json.loads(originals['identity-gate.json'][0])
    peer, row = continuation.eligible(harness, gate,
                                     json.loads(originals['bridge-test-evidence.json'][0]))
    request = dict(task_id=gate['task_id'], artifact_root=str(root),
        controller=str(args.controller), identity_sha256=args.identity_sha256,
        bridge_sha256=args.bridge_sha256, reservation_id=args.reservation_id,
        executor_uuid=peer['surface_uuid'], token=row['token'])
    if (intent.get('schema') != 'original-bridge-probe-continuation-v1'
            or intent.get('request') != request or result.get('request') != request
            or result.get('controller') != str(args.controller)
            or result.get('archive') != str(archive)
            or result.get('status') != 'BRIDGE_CONTINUATION_FAILED_NO_REPLAY'
            or result.get('handshake') != 'NOT_ATTEMPTED'
            or any(type(result.get(k)) is not int or result[k] != v for k, v in
                   dict(probe_invocations=1, submission_operations=0,
                        bridge_write_attempts=0, bridge_writes=0).items())
            or probe.get('ok') is not False or result.get('probe') != probe.get('evidence')):
        raise ValueError('PROBE_CLEAR_COMPLETED_FAILED_CONTINUATION_REQUIRED')
    observed = probe['evidence']
    if (any(observed.get(k) != row[k] for k in ('task_id', 'executor', 'ordinal', 'token'))
            or observed.get('token_sent') is not True
            or observed.get('pre_read_performed') is not True
            or observed.get('compose_was_empty_before_send') is not True
            or observed.get('clear_confirmed') is not False
            or type(observed.get('clear_key_count')) is not int
            or observed['clear_key_count'] != 0
            or observed.get('clear_verified_at') is not None
            or observed.get('override', {}).get('force_compose_override') is not False
            or any(observed.get('override', {}).get(k) for k in
                   ('clear_actions', 'delete_count', 'clear_confirmed',
                    'direct_replace_after_clear_attempts'))):
        raise ValueError('PROBE_CLEAR_ORIGINAL_TOKEN_WITH_ZERO_CLEAR_KEYS_REQUIRED')
    binding, fence = intent['native_binding'], probe_intent['native_paste_fence']
    native._fenced_binding(binding, row['token'], fence, fence['captured_at_epoch'])
    if (binding.get('provider') != 'claude'
            or binding.get('session_id') != gate['native_participants'][1]['native_session_id']
            or binding.get('identity') != {k: v for k, v in dict(
                workspace_uuid=gate['workspace_uuid'],
                caller_surface_uuid=gate['supervisor_surface_uuid'],
                target_surface_uuid=peer['surface_uuid'],
                target_pane_uuid=gate['workspace_proofs'][peer['surface_ref']]['target_pane_uuid']).items()}
            or fence['transcript'].get('at_record_boundary') is not True
            or not binding['bound_at_epoch'] <= probe_intent['at_epoch'] <= fence['captured_at_epoch']):
        raise ValueError('PROBE_CLEAR_ORIGINAL_NATIVE_BINDING_CHANGED')
    marker_path = harness._ACTIVE_DIR / gate['workspace_uuid'] / (args.reservation_id + '.json')
    marker_pin = pins['reservation.json.original']
    if (intent.get('original_reservation_path') != str(marker_path)
            or intent.get('original_reservation') != recovery.pin_summary(marker_pin)):
        raise ValueError('PROBE_CLEAR_ARCHIVED_RESERVATION_CHANGED')
    marker = json.loads(marker_pin[0])
    # Validate original terms at their own armed time, without changing or
    # extending them. Released/expired tasks may clear, never publish PASS.
    expected_participants = [dict(p, ordinal=i) for i, p in enumerate(gate['native_participants'])]
    keys = ('role', 'ordinal', 'provider', 'surface_uuid', 'native_session_id')
    if (marker.get('marker_version') != 2 or marker.get('collaboration_id') != args.reservation_id
            or marker.get('task_id') != gate['task_id'] or marker.get('artifact_root') != str(root)
            or any(marker.get(k) != gate['workspace_uuid'] for k in ('workspace_id', 'workspace_uuid'))
            or [{k: p.get(k) for k in keys} for p in marker.get('participants', [])]
            != [{k: p.get(k) for k in keys} for p in expected_participants]):
        raise ValueError('PROBE_CLEAR_ARCHIVED_RESERVATION_CHANGED')
    summary = {name: recovery.pin_summary(pin) for name, pin in pins.items()}
    return dict(root=root, archive=archive, pins=pins, originals=originals,
        gate=gate, peer=peer, row=observed, request=request, binding=binding,
        fence=fence, marker=marker, marker_path=marker_path,
        archive_summary=summary,
        archive_sha256=continuation.digest(recovery.json_bytes(summary)))


class NativeAbsence:
    """Bounded incremental native scan, including queue-operation records.

    A queue event is not a receipt, but any event containing this probe means
    it is unsafe to treat the probe as an unsubmitted draft. Unknown records
    never authorize keys. Keep the original fence, not a new capture.
    """
    def __init__(self, native, binding, fence):
        self.native, self.binding, self.fence = native, binding, fence
        self.cursor = fence['transcript']['offset']

    def check(self):
        native, b, fence = self.native, self.binding, self.fence
        token = self.token
        remaining = min(native.MAX_SCAN, 32 * 1024 * 1024)
        while remaining > 0:
            start = self.cursor
            value = native.probe(b, token, cursor=start,
                max_bytes=min(remaining, 4 * 1024 * 1024),
                not_before=fence['captured_at_epoch'], paste_fence=fence)
            if (value.get('confirmed') is not False or value.get('queued_proof')
                    or value.get('state') not in ('NATIVE_PENDING', 'NATIVE_SCAN_BUDGET')):
                raise ValueError('PROBE_CLEAR_NATIVE_RECEIVED_QUEUED_OR_UNKNOWN: ' + str(value.get('state')))
            stop = value['next_offset']
            fenced = native._fenced_binding(b, token, fence, fence['captured_at_epoch'])
            with native._open(b['transcript']['path']) as stream:
                native._check_file(stream, b)
                native._check_file(stream, fenced)
                stream.seek(start)
                while stream.tell() < stop:
                    raw = stream.readline(min(native.MAX_RECORD + 1, stop - stream.tell()))
                    if not raw.endswith(b'\n'):
                        raise ValueError('PROBE_CLEAR_NATIVE_RECORD_UNKNOWN')
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError('PROBE_CLEAR_NATIVE_RECORD_UNKNOWN')
                    if record.get('type') == 'queue-operation' and token.encode() in raw:
                        raise ValueError('PROBE_CLEAR_NATIVE_QUEUE_OPERATION_PRESERVE')
                native._check_file(stream, b)
                native._check_file(stream, fenced)
            self.cursor = stop
            remaining -= max(1, stop - start)
            if value['state'] == 'NATIVE_PENDING':
                return value
        raise ValueError('PROBE_CLEAR_NATIVE_SCAN_BUDGET_NO_INPUT')


def recover_clear(args, modules=None):
    result = dict(schema=SCHEMA, status='PROBE_CLEAR_UNAVAILABLE',
        controller=str(args.controller), input_operations=0, key_intents=0,
        paste_operations=0, submission_operations=0, probe_invocations=0,
        archive_created=False, bridge_write_attempts=0, bridge_writes=0,
        clear_confirmed=False, handshake='NOT_ATTEMPTED', business_acceptance='NOT_EVALUATED')
    output, errors = io.StringIO(), io.StringIO()
    try:
        modules = modules or continuation.original_modules(args.controller)
        bridge, native, evidence, _, harness, mutex = modules
        plan = original_plan(args, modules)
        root, archive = plan['root'], plan['archive']
        slot = root / ARCHIVE
        result.update(request=plan['request'], archive=str(slot),
            original_archive=str(archive), archive_sha256=plan['archive_sha256'],
            archive_summary=plan['archive_summary'])
        if not continuation.absent(slot):
            raise ValueError('PROBE_CLEAR_SLOT_EXISTS_NO_REPLAY')
        originals, pins = plan['originals'], plan['pins']
        gate, peer, row = plan['gate'], plan['peer'], plan['row']
        binding, fence = plan['binding'], plan['fence']
        target, token = peer['surface_uuid'], row['token']
        archive_stat = archive.lstat()
        live_marker = recovery.optional_snapshot(evidence, plan['marker_path'])
        stable_marker = {k: v for k, v in plan['marker'].items() if k != 'last_activity_at'}

        def check_files(published=None):
            live = archive.lstat()
            if (not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino)
                    != (archive_stat.st_dev, archive_stat.st_ino)):
                raise ValueError('PROBE_CLEAR_ARCHIVE_DIRECTORY_CHANGED')
            if sorted(p.name for p in archive.iterdir()) != sorted(pins):
                raise ValueError('PROBE_CLEAR_ARCHIVE_CONTENTS_CHANGED')
            for name, pin in pins.items():
                if evidence.snapshot(archive / name) != pin:
                    raise ValueError('PROBE_CLEAR_ARCHIVE_FILE_CHANGED')
            for name, pin in originals.items():
                wanted = published if published is not None and name == 'bridge-test-evidence.json' else pin
                if recovery.optional_snapshot(evidence, root / name) != wanted:
                    raise ValueError('PROBE_CLEAR_ORIGINAL_FILES_CHANGED')
            if not continuation.absent(root / 'handshake-receipt.json') or any(root.glob('*task-pack*.json')):
                raise ValueError('PROBE_CLEAR_HANDSHAKE_OR_TASK_EXISTS')
            current = recovery.optional_snapshot(evidence, plan['marker_path'])
            if (current is None) != (live_marker is None):
                raise ValueError('PROBE_CLEAR_RESERVATION_BOUNDARY_CHANGED')
            if current is not None:
                marker = json.loads(current[0])
                if {k: v for k, v in marker.items() if k != 'last_activity_at'} != stable_marker:
                    raise ValueError('PROBE_CLEAR_RESERVATION_TERMS_CHANGED')
                if args.publish_bridge_evidence:
                    continuation.check_reservation(marker, gate, root, args.reservation_id)
            elif args.publish_bridge_evidence:
                raise ValueError('PROBE_CLEAR_RELEASED_TASK_CANNOT_PUBLISH')
            return current

        check_files()
        result['reservation_present'] = live_marker is not None
        if not args.apply:
            result.update(status='STATIC_ELIGIBLE_NO_INPUT', live_readiness='NOT_OBSERVED',
                apply_requires_original_supervisor=True, original_harness_exit_required=True)
            return result
        if args.archive_sha256 != plan['archive_sha256']:
            raise ValueError('PROBE_CLEAR_PREVIEW_ARCHIVE_PIN_REQUIRED')
        if not args.original_harness_exited:
            raise ValueError('PROBE_CLEAR_ORIGINAL_HARNESS_EXIT_ATTESTATION_REQUIRED')
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result['supervisor_thread'] = recovery.require_supervisor(
                harness, gate, proof_out=result.setdefault('supervisor_identity', {}))
            native.require_bound(bridge, target, binding, token)
            with (continuation.root_lock(root) as (root_fd, check_root),
                  mutex.hold(binding['identity']) as check_mutex,
                  harness._marker_update_lock(plan['marker_path'], plan['marker'])):
                # The same participant/directory locks used by arm/release keep
                # another task from claiming this executor during cleanup.
                paths = list(plan['marker_path'].parent.glob('*.json'))
                legacy = harness._ACTIVE_DIR / (gate['workspace_uuid'] + '.json')
                if os.path.lexists(legacy):
                    paths.append(legacy)
                if len(paths) > 1024:
                    raise ValueError('PROBE_CLEAR_RESERVATION_SCAN_LIMIT')
                for path in paths:
                    if path == plan['marker_path']:
                        continue
                    other = json.loads(evidence.read_bytes(path))
                    participants = other.get('participants')
                    if (not isinstance(participants, list) or not participants
                            or any(not isinstance(p, dict) or not p.get('surface_uuid')
                                   or p.get('surface_uuid') == target for p in participants)):
                        raise ValueError('PROBE_CLEAR_OTHER_OR_UNKNOWN_RESERVATION')
                absence = NativeAbsence(native, binding, fence)
                absence.token = token
                slot_pins = {}
                slot_stat = None
                cursor_geometry = None

                def check(published=None):
                    check_root()
                    check_mutex()
                    check_files(published)
                    if slot_stat is not None:
                        live = slot.lstat()
                        if (not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino)
                                != (slot_stat.st_dev, slot_stat.st_ino)):
                            raise ValueError('PROBE_CLEAR_SLOT_CHANGED')
                        for name, pin in slot_pins.items():
                            if evidence.snapshot(slot / name) != pin:
                                raise ValueError('PROBE_CLEAR_STEP_EVIDENCE_CHANGED')
                    recovery.require_supervisor(harness, gate)
                    native.require_bound(bridge, target, binding, token)
                    result['native_absence'] = absence.check()

                def observe(expected):
                    check()
                    screen = bridge.read_screen(target, lines=args.lines)
                    frame = bridge._claude_bordered_compose(screen)
                    busy = bridge._queued_or_active_input(screen) if frame is not None else None
                    rows = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', screen).splitlines()
                    value = dict(at_epoch=time.time(), screen=screen,
                        body=frame['body'] if frame is not None else None,
                        expected_prefix=expected, complete_frame=frame is not None, busy=busy,
                        structure=rows[frame['start'] - 1:frame['end'] + 1] if frame else None)
                    # Even a zero-key preflight rejection preserves its full
                    # failed frame in the returned diagnostic.
                    result['last_observation'] = value
                    check()
                    return value

                def stable(expected, record=None, previous=None, *, cursor_mode=False,
                           position=None, previous_position=None, permit_padding=False):
                    nonlocal cursor_geometry
                    last = None
                    for index in range(8):
                        value = observe(expected)
                        using_cursor = cursor_mode or (permit_padding and value['body'] == expected + ' ')
                        if using_cursor and value['complete_frame'] and value['busy'] is False:
                            try:
                                value['grid_reply'] = cursor_observation.read(bridge, binding)
                                value['cursor_proof'] = cursor_observation.inspect(
                                    bridge, value['grid_reply'], binding, value['body'])
                                check()
                            except ERRORS as exc:
                                value['grid_error'] = str(exc)
                                if record is not None:
                                    record(index, value)
                                raise
                        if record is not None:
                            record(index, value)
                        if not value['complete_frame'] or value['busy'] is not False:
                            raise ValueError('PROBE_CLEAR_COMPOSER_BUSY_OR_UNKNOWN')
                        if using_cursor:
                            proof = value['cursor_proof']
                            if cursor_geometry is None:
                                cursor_geometry = proof['geometry']
                            elif proof['geometry'] != cursor_geometry:
                                raise ValueError('PROBE_CLEAR_GRID_GEOMETRY_CHANGED')
                            wanted = len(expected) if position is None else position
                            exact_body = value['body'] in (expected, expected + ' ')
                            if not exact_body or proof['position'] != wanted:
                                old_body = previous if previous is not None else expected
                                if (previous_position is not None
                                        and proof['position'] == previous_position
                                        and value['body'] in (old_body, old_body + ' ')):
                                    last = None
                                    time.sleep(0.1)
                                    continue
                                raise ValueError('PROBE_CLEAR_CURSOR_OR_CONTENT_CHANGED_PRESERVE')
                            if last is not None and proof == last.get('cursor_proof'):
                                return value
                            last = value
                            time.sleep(0.1)
                            continue
                        if value['body'] != expected:
                            # After a key, only its exact previous screen may
                            # remain briefly. Never issue another key to retry.
                            if previous is not None and value['body'] == previous:
                                last = None
                                time.sleep(0.1)
                                continue
                            raise ValueError('PROBE_CLEAR_COMPOSER_CHANGED_PRESERVE')
                        if last is not None and value['structure'] == last['structure']:
                            return value
                        last = value
                        time.sleep(0.1)
                    raise ValueError('PROBE_CLEAR_COMPOSER_NOT_STABLE')

                check()
                first = stable(token, permit_padding=getattr(args, 'verify_cursor_navigation', False))
                cursor_mode = 'cursor_proof' in first
                check()
                os.mkdir(ARCHIVE, 0o700, dir_fd=root_fd)
                result['archive_created'] = True
                os.fsync(root_fd)
                slot_fd = os.open(ARCHIVE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    slot_stat = os.fstat(slot_fd)

                    def write(name, value):
                        raw = recovery.json_bytes(value)
                        recovery.write_exclusive(slot_fd, name, raw)
                        pin = evidence.snapshot(slot / name)
                        if pin[0] != raw:
                            raise ValueError('PROBE_CLEAR_STEP_WRITE_UNCONFIRMED')
                        slot_pins[name] = pin

                    try:
                        write('INTENT.json', dict(schema=SCHEMA, request=plan['request'],
                            archive_sha256=plan['archive_sha256'], original_archive=str(archive),
                            archive_summary=plan['archive_summary'], native_binding=binding,
                            original_paste_fence=fence,
                            original_harness_exit='ATTESTED_BY_ORIGINAL_SUPERVISOR',
                            publish_bridge_evidence=args.publish_bridge_evidence,
                            cursor_navigation=cursor_mode,
                            reservation_present=live_marker is not None,
                            supervisor_identity=result['supervisor_identity'], first_observation=first))
                        expected = token
                        cursor_position = len(token)
                        keys = (['home', 'end'] if cursor_mode else ['end']) + ['backspace'] * len(token)
                        for step, key in enumerate(keys):
                            prefix = f'{step:02d}'
                            stable(expected, lambda i, v: write(f'{prefix}-before-{i}.json', v),
                                   cursor_mode=cursor_mode, position=cursor_position)
                            next_prefix = expected if key in ('home', 'end') else expected[:-1]
                            next_position = 0 if key == 'home' else len(next_prefix)
                            write(f'{prefix}-KEY-INTENT.json', dict(at_epoch=time.time(), key=key,
                                expected_prefix=expected, next_prefix=next_prefix,
                                cursor_position=cursor_position if cursor_mode else None,
                                next_cursor_position=next_position if cursor_mode else None))
                            result['key_intents'] += 1
                            check()
                            # Exactly one original-controller key. An exception
                            # or uncertain result leaves the exclusive slot.
                            result['input_operations'] = None
                            bridge.send_key(target, key)
                            result['input_operations'] = step + 1
                            write(f'{prefix}-KEY-RESULT.json', dict(at_epoch=time.time(),
                                key=key, returned=True, native_reception='NOT_CLAIMED'))
                            stable(next_prefix, lambda i, v: write(f'{prefix}-after-{i}.json', v),
                                   previous=expected if key == 'backspace' else None,
                                   cursor_mode=cursor_mode, position=next_position,
                                   previous_position=cursor_position if cursor_mode else None)
                            if cursor_mode and key == 'end':
                                result['cursor_navigation'] = 'HOME_THEN_END_ACKNOWLEDGED'
                            expected = next_prefix
                            cursor_position = next_position
                        check()
                        result.update(clear_confirmed=True, status='PROBE_CLEARED_ONLY',
                            clear_verified_at=harness._now())
                        write('CLEAR-PROOF.json', dict(schema=SCHEMA, token=token,
                            archive_sha256=plan['archive_sha256'],
                            clear_verified_at=result['clear_verified_at'],
                            clear_confirmed=True, key_count=result['input_operations'],
                            native_absence=result['native_absence']))
                        if args.publish_bridge_evidence:
                            check()
                            updated = dict(row, clear_confirmed=True,
                                clear_confirmed_by='ORIGINAL_PROBE_CLEAR_RECOVERY',
                                clear_verified_at=result['clear_verified_at'],
                                clear_key_count=row['clear_key_count'] + result['input_operations'],
                                clear_recovery=dict(schema=SCHEMA, archive=str(slot),
                                    original_archive=str(archive), archive_sha256=plan['archive_sha256']))
                            write('PUBLISH-INTENT.json', dict(at_epoch=time.time(), evidence=updated))
                            check()
                            result['bridge_write_attempts'] = 1
                            try:
                                harness._write_bridge_evidence(root, [updated])
                            finally:
                                after = evidence.snapshot(root / 'bridge-test-evidence.json')
                                result['bridge_writes'] = int(after != originals['bridge-test-evidence.json'])
                                result['published_bridge'] = recovery.pin_summary(after)
                            if json.loads(after[0]) != dict(updated, executors=[updated]):
                                raise ValueError('PROBE_CLEAR_ORIGINAL_WRITER_SHAPE_CHANGED')
                            check(published=after)
                            result.update(status='PROBE_CLEARED_BRIDGE_PUBLISHED',
                                next_action='original supervisor runs original-controller handshake; no new probe')
                    except ERRORS as exc:
                        result.update(status='PROBE_CLEAR_INCOMPLETE_NO_REPLAY', error=str(exc))
                    result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())
                    write('RESULT.json', result)
                finally:
                    os.close(slot_fd)
        return result
    except ERRORS as exc:
        result.update(status='PROBE_CLEAR_INCOMPLETE_NO_REPLAY' if result['archive_created']
                      else 'PROBE_CLEAR_UNAVAILABLE', error=str(exc))
        return result
    finally:
        if output.getvalue() or errors.getvalue():
            result['controller_output'] = dict(stdout=output.getvalue(), stderr=errors.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('controller', 'artifact-root', 'identity-sha256', 'bridge-sha256', 'reservation-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--archive-sha256')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--original-harness-exited', action='store_true')
    parser.add_argument('--publish-bridge-evidence', action='store_true')
    parser.add_argument('--verify-cursor-navigation', action='store_true',
                        help='one Home/End acknowledgement for the original ASCII probe cursor cell')
    parser.add_argument('--lines', type=recovery.observation.bounded_integer(1, 2000), default=200)
    result = recover_clear(parser.parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('STATIC_ELIGIBLE_NO_INPUT', 'PROBE_CLEARED_ONLY',
                                   'PROBE_CLEARED_BRIDGE_PUBLISHED') else 2


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
