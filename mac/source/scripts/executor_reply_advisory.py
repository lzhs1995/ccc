"""Authenticated, nonblocking ANSWERED reminders; never consume or send.

New CLI inquiries bind their Claude process before registration commits. Old
inquiries require an explicit sidecar with original native Bash/result evidence.
Installing a hook neither enrolls nor migrates an old inquiry.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid
from datetime import datetime

import claude_maintenance_context as maintenance
import cmux_native_delivery as native
import executor_ready as ready
import executor_reask as reask
import executor_reask_protocol as protocol
import executor_reask_scheduler as scheduler
import successor_rebind
from cmux_evidence_io import open_regular_lock, read_bytes

SCHEMA = 'executor-reply-advisory-v1'
IDENTITY_SCHEMA = 'executor-reask-native-identity-v1'
REMINDER_SECONDS = 60.0
MAX_CREATION_BYTES = 64 * 1024


def root():
    return ready.state_root() / SCHEMA


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def capture_executor(live, *, state):
    """Foreground-only identity pin. Called inside new-episode registration."""
    from executor_reask_identity import capture_executor as capture_consumer
    return capture_consumer(live, state=state)


def capture_target(surface, workspace, pane):
    """Capture a named target; this does not impersonate it as the caller."""
    bridge = ready._bridge()
    row = native._tree_row(bridge, surface)
    if (row['workspace_uuid'] != workspace or row['target_surface_uuid'] != surface
            or (pane is not None and pane != row['target_pane_uuid'])):
        raise ValueError('inquiry executor moved during registration')
    process = native._target_process(row)
    if native._provider(process) != 'claude':
        return None  # This observer only exposes Claude PostToolUse context.
    session = native._live_session(process, 'claude')
    result = dict(schema=IDENTITY_SCHEMA, provider='claude',
                  caller_surface_uuid=row['target_surface_uuid'],
                  workspace_uuid=row['workspace_uuid'], pane_uuid=row['target_pane_uuid'],
                  tty=row['tty'], process=native._public_process(process),
                  session_id=session, captured_epoch=time.time(),
                  transcript=native._pin_file(native._transcript_path('claude', session),
                                              'claude', session))
    check_executor(result)
    return result


def check_executor(binding, payload=None):
    from executor_reask_identity import SCHEMA as CONSUMER_SCHEMA, check_live
    if binding.get('schema') == CONSUMER_SCHEMA:
        if binding.get('provider') != 'claude':
            raise ValueError('this observer requires the original Claude executor')
        check_live(binding)
        if payload is not None and (
                payload.get('session_id') != binding['session_id']
                or payload.get('transcript_path') != binding['transcript']['path']
                or not maintenance.caller_matches(binding)):
            raise ValueError('not the original automatic Claude hook')
        return True
    if (binding.get('schema') != IDENTITY_SCHEMA or binding.get('provider') != 'claude'
            or str(uuid.UUID(binding['session_id'])) != binding['session_id']):
        raise ValueError('original Claude identity required')
    path = Path(binding['transcript']['path'])
    if (native._native_root('claude').resolve() not in path.parents
            or path.name != binding['session_id'] + '.jsonl'):
        raise ValueError('inquiry transcript is outside the original native session')
    if payload is not None:
        if (payload.get('session_id') != binding['session_id']
                or payload.get('transcript_path') != str(path)
                or not maintenance.caller_matches(binding)):
            raise ValueError('not the original automatic Claude hook')
    process = native.identity.process(binding['process']['pid'], validate_argv=False)
    if (native._public_process(process) != binding['process']
            or native._provider(process) != 'claude'
            or native._live_session(process, 'claude') != binding['session_id']
            or process['env'].get('CMUX_SURFACE_ID') != binding['caller_surface_uuid']
            or process['env'].get('CMUX_WORKSPACE_ID') != binding['workspace_uuid']):
        raise ValueError('executor process, birth, surface or live session changed')
    tty = subprocess.run(['/bin/ps', '-p', str(process['pid']), '-o', 'tty='],
                         capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    if tty != binding['tty']:
        raise ValueError('executor tty changed')
    row = native._tree_row(ready._bridge(), binding['caller_surface_uuid'])
    if (row['target_surface_uuid'] != binding['caller_surface_uuid']
            or row['workspace_uuid'] != binding['workspace_uuid']
            or row['target_pane_uuid'] != binding['pane_uuid']
            or row['tty'] != binding['tty']):
        raise ValueError('executor live pane or workspace changed')
    with native._open(path) as stream:
        native._check_file(stream, binding)
    return True


def _record(stream, pin, binding):
    offset, size = pin['offset'], pin['bytes']
    if (type(offset) is not int or offset < 0 or type(size) is not int
            or not 0 < size <= MAX_CREATION_BYTES
            or offset + size > binding['transcript']['offset']):
        raise ValueError('creation record must precede explicit enrollment')
    if offset:
        stream.seek(offset - 1)
        if stream.read(1) != b'\n':
            raise ValueError('creation offset is not a native record boundary')
    stream.seek(offset)
    raw = stream.read(size)
    if sha(raw) != pin['sha256'] or not raw.endswith(b'\n'):
        raise ValueError('original creation record changed')
    record = json.loads(raw)
    if (record.get('sessionId') != binding['session_id']
            or record.get('isSidechain') is True):
        raise ValueError('creation belongs to another native session')
    return record


def verify_creation(state, binding, proof):
    """Prove original episode ownership, not historical wrapper source bytes."""
    if (proof.get('session_id') != binding['session_id']
            or proof.get('episode_id') != state['episode_id']
            or proof.get('controller') != protocol.state_controller(state)
            or proof.get('first_marker') != reask.issue_marker(state, 1)
            or state.get('attempt_count', 0) < 1):
        raise ValueError('creation proof does not bind the original episode')
    with native._open(binding['transcript']['path']) as stream:
        native._check_file(stream, binding)
        call = _record(stream, proof['tool_use'], binding)
        result = _record(stream, proof['tool_result'], binding)
        native._check_file(stream, binding)
    if (call.get('type') != 'assistant' or result.get('type') != 'user'
            or proof['tool_use']['offset'] + proof['tool_use']['bytes'] > proof['tool_result']['offset']):
        raise ValueError('creation call/result ordering is invalid')
    start = datetime.fromisoformat(call['timestamp'].replace('Z', '+00:00')).timestamp()
    end = datetime.fromisoformat(result['timestamp'].replace('Z', '+00:00')).timestamp()
    if not (start <= state['started_epoch'] <= end and 0 <= end - start <= 60):
        raise ValueError('creation timestamp does not surround episode creation')
    calls = [b for b in call['message']['content'] if isinstance(b, dict)
             and b.get('type') == 'tool_use' and b.get('id') == proof['tool_use_id']]
    results = [b for b in result['message']['content'] if isinstance(b, dict)
               and b.get('type') == 'tool_result' and b.get('tool_use_id') == proof['tool_use_id']]
    if (len(calls) != 1 or calls[0].get('name') != 'Bash'
            or not isinstance(calls[0].get('input', {}).get('command'), str)
            or not calls[0]['input']['command'].strip()
            or len(results) != 1 or results[0].get('is_error') is not False
            or not isinstance(results[0].get('content'), str)):
        raise ValueError('original matching successful Bash result required')
    text = results[0]['content'].strip()
    summary, _ = json.JSONDecoder().raw_decode(text)
    last = summary.get('last_ask')
    if last is None:
        lines = [line[len('last_ask: '):] for line in text.splitlines()
                 if line.startswith('last_ask: ')]
        if len(lines) != 1:
            raise ValueError('original first marker result missing')
        last = json.loads(lines[0])
    if (summary.get('outcome') != reask.WAITING or summary.get('ask_count') != 1
            or summary.get('supervisor') != state['supervisor']
            or summary.get('state_file') != str(reask.state_path(state['caller_surface_uuid']))
            or last.get('marker') != proof['first_marker']
            or summary.get('file_posts') != 1 or summary.get('terminal_sends') != 0
            or summary.get('terminal_attempts') != 0):
        raise ValueError('original result does not prove this file inquiry')
    return True


def _authority(record, state):
    """Revalidate the exact, still-live authority; no inferred proxy caller."""
    auth = record['authorization']
    maintenance.pinned(auth).decode('utf-8')
    if record.get('enrollment_mode') == 'executor_self':
        original = (state.get('continuation') or {}).get('authorization')
        if (auth != original or record.get('enrolled_by', {}).get('caller_surface_uuid')
                != state['caller_surface_uuid']):
            raise ValueError('self enrollment requires the original authorization')
        if not inquiry_active(state):
            raise ValueError('original authorization is no longer active')
        return None
    if record.get('enrollment_mode') != successor_rebind.MODE_V2:
        raise ValueError('explicit maintenance enrollment required')
    artifact = json.loads(maintenance.pinned(record['maintenance_authorization']))
    if artifact.get('schema') != successor_rebind.SCHEMA_V2:
        raise ValueError('maintenance enrollment requires successor-rebind-v2')
    successor_rebind.validate_artifact(artifact)
    original = artifact['authorization_record']
    owner = record.get('enrolled_by', {})
    if (auth['path'] != original['path'] or auth['sha256'] != original['sha256']
            or auth['sha256'] != artifact['authorization_record_sha256']
            or artifact['target_executor_uuid'] != state['caller_surface_uuid']
            or artifact['target_workspace_uuid'] != state['workspace_uuid']
            or owner.get('caller_surface_uuid') != artifact['successor_surface_uuid']
            or owner.get('caller_workspace_uuid') != artifact['successor_workspace_uuid']
            or owner.get('caller_pane_uuid') != artifact['successor_pane_uuid']):
        raise ValueError('maintenance authority or actual enroller changed')
    return artifact


def inquiry_active(state, *, now=None):
    """Observe stop/revocation/expiry without publishing an episode transition."""
    if (state.get('state') not in (reask.WAITING, reask.ANSWERED)
            or os.path.lexists(reask.stop_file(state['caller_surface_uuid']))):
        return False
    from executor_reask_lifecycle import poll_authorization
    preview = copy.deepcopy(state)
    poll_authorization(preview, time.time() if now is None else now, publish=False)
    continuation = preview.get('continuation')
    return (preview.get('state') != reask.STOPPED
            and (continuation is None or continuation.get('status') == 'ACTIVE'))


def _read_sidecar(state):
    path = root() / (ready.digest(state['caller_surface_uuid']) + '.enrollment.json')
    value = json.loads(read_bytes(path, limit=MAX_CREATION_BYTES))
    if (value.get('schema') != SCHEMA or value.get('binding') != scheduler.binding(state)
            or value.get('controller') != protocol.state_controller(state)):
        raise ValueError('explicit sidecar belongs to another original inquiry')
    artifact = _authority(value, state)
    identity = value['executor_identity']
    if (identity['caller_surface_uuid'] != state['caller_surface_uuid']
            or identity['workspace_uuid'] != state['workspace_uuid']
            or (artifact is not None and identity['pane_uuid'] != artifact['target_pane_uuid'])):
        raise ValueError('sidecar executor is outside its authorized target')
    proof = json.loads(maintenance.pinned(value['creation_proof']))
    verify_creation(state, identity, proof)
    return identity


def enroll_existing(live, episode, state_sha256, authorization, creation_proof,
                    creation_sha256, *, maintenance_authorization=None):
    """Explicit sidecar only. Original state/registration/controllers stay intact."""
    is_maintenance = maintenance_authorization is not None
    caller = live['target_surface_uuid'] if is_maintenance else live['caller_surface_uuid']
    with reask.episode_lock(caller):
        path = reask.state_path(caller)
        before = read_bytes(path, limit=ready.MAX_JSON_BYTES)
        state = json.loads(before)
        if (sha(before) != state_sha256 or state.get('episode_id') != episode
                or state.get('executor_identity') is not None
                or not protocol.supports_state(state) or not inquiry_active(state)):
            raise ValueError('original episode changed or is not eligible for sidecar enrollment')
        record = dict(schema=SCHEMA, binding=scheduler.binding(state),
                      controller=protocol.state_controller(state),
                      authorization=maintenance.pin(authorization),
                      enrollment_mode=(successor_rebind.MODE_V2 if is_maintenance else 'executor_self'),
                      enrolled_by=dict(caller_surface_uuid=live['caller_surface_uuid'],
                                       caller_workspace_uuid=(live['caller_workspace_uuid'] if is_maintenance
                                                              else live['workspace_uuid']),
                                       caller_pane_uuid=live.get('caller_pane_uuid')))
        if is_maintenance:
            record['maintenance_authorization'] = maintenance.pin(maintenance_authorization)
            artifact = _authority(record, state)
            fresh = ready._bridge().pin_successor_pair(
                artifact, expected_caller=live['caller_surface_uuid'], expected_target=caller)
            keys = ('caller_surface_uuid', 'caller_workspace_uuid', 'caller_pane_uuid',
                    'target_surface_uuid', 'target_workspace_uuid', 'target_pane_uuid')
            if any(fresh.get(key) != live.get(key) for key in keys):
                raise ValueError('maintenance pair changed during enrollment')
            workspace, pane = fresh['target_workspace_uuid'], fresh['target_pane_uuid']
        else:
            if not scheduler._pair_matches(state, scheduler._live_pins(live, refresh=True)):
                raise ValueError('only the original executor may self enroll')
            _authority(record, state)
            workspace, pane = live['workspace_uuid'], live.get('caller_pane_uuid')
        proof_pin = maintenance.pin(creation_proof)
        if proof_pin['sha256'] != creation_sha256:
            raise ValueError('creation proof changed')
        identity = capture_target(caller, workspace, pane)
        if identity is None:
            raise ValueError('sidecar requires the existing Claude session')
        verify_creation(state, identity, json.loads(maintenance.pinned(proof_pin)))
        record.update(executor_identity=identity, creation_proof=proof_pin,
                      enrolled_epoch=time.time(), input_operations=0,
                      historical_wrapper_source_verified=False)
        directory = root()
        scheduler.secure_directory(directory)
        target = directory / (ready.digest(caller) + '.enrollment.json')
        if read_bytes(path, limit=ready.MAX_JSON_BYTES) != before:
            raise ValueError('episode changed before sidecar publication')
        _authority(record, state)
        if not inquiry_active(state):
            raise ValueError('inquiry stopped before sidecar publication')
        check_executor(identity)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        return dict(status='ENROLLED_ADVISORY_ONLY', path=str(target), episode_id=episode,
                    input_operations=0, consumed=False, migrated=False)


def reply_notice(state):
    reply = state.get('reply') or {}
    body, marker = reply.get('body'), reply.get('marker')
    if (state.get('state') != reask.ANSWERED or not reask.issued_marker(state, marker)
            or not reask.valid_reply(state, body, [marker],
                                    caller=state['caller_surface_uuid'],
                                    supervisor=state['supervisor'],
                                    task_id=state.get('task_id', ''), episode_id=state['episode_id'])
            or sha(canonical(body).encode()) != reply.get('body_sha256')
            or not protocol.supports_state(state)):
        return None
    controller = protocol.state_controller(state)
    if controller is None:
        return None
    # The complete immutable reply object is always in this bound episode.
    # Raw mailbox bytes and canonical JSON digests are deliberately distinct.
    path = reask.state_path(state['caller_surface_uuid'])
    command = shlex.join([controller['python'], '-B',
                          str(Path(controller['source']) / 'scripts/executor_reask.py'),
                          'consume', '--caller-uuid', state['caller_surface_uuid'],
                          '--reply-sha256', reply['body_sha256']])
    return dict(status='ANSWERED_UNCONSUMED', episode_id=state['episode_id'], marker=marker,
                reply_file=str(path), json_pointer='/reply/body',
                body_sha256=reply['body_sha256'], sha256_encoding='canonical-json-utf8',
                consume_command=command, reservation_paused=bool(state.get('paused_tasks')),
                input_operations=0, consumed=False)


def emit_context(notice):
    text = ('ANSWERED_UNCONSUMED: read the complete reply at REPLY_FILE /reply/body before '
            'using the original controller command below. BODY_SHA256 hashes UTF-8 JSON '
            'with ensure_ascii=False, sort_keys=True, separators=(\',\', \':\'); it is not '
             'the raw file SHA256. Only you may acknowledge this exact reply. A TASK reply '
             'does not dispatch a pack; consumption never releases a formal reservation. '
             'After reading and exact original-controller consumption, if only the supervisor '
             'can act, end this waiting turn normally and state the pending dependency. Do not '
             'start another model wait loop solely to look active. Preserve Goal/loop and the '
             'authorized file scheduler; its enrollment is not proof of a running sweep. '
             'A separate new pack requires fresh identity, handshake and validation preflight. '
             'Both agents should send ready messages through the supported entrypoint during '
             'normal work or native Goal; do not wait for idle or pause Goal to communicate. '
             'This reminder sends no input and proves neither reading nor acceptance.\n'
            + json.dumps(notice, ensure_ascii=False))
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                            'additionalContext': text}},
                     ensure_ascii=False), flush=True)


def discover(payload, *, emit=emit_context, now=None):
    try:
        if not isinstance(payload, dict) or payload.get('hook_event_name') != 'PostToolUse':
            return None
        caller = str(uuid.UUID(os.environ.get('CMUX_SURFACE_ID', ''))).upper()
        if not reask.state_path(caller).is_file():
            return None
        with reask.episode_lock(caller):
            state = reask.load(caller) or {}
            if state.get('state') != reask.ANSWERED or not inquiry_active(state, now=now):
                return None
            binding = state.get('executor_identity') or _read_sidecar(state)
            if (binding['caller_surface_uuid'] != caller
                    or binding['workspace_uuid'] != state['workspace_uuid']):
                return None
            check_executor(binding, payload)
            notice = reply_notice(state)
            if notice is None:
                return None
            directory = root()
            scheduler.secure_directory(directory)
            path = directory / (ready.digest(caller) + '.cursor.json')
            with open_regular_lock(directory / (ready.digest(caller) + '.lock')) as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                previous = ready.read_json(path) or {}
                token = sha(canonical([notice['episode_id'], notice['marker'],
                                       notice['body_sha256']]).encode())
                when = time.time() if now is None else now
                last = previous.get('offered_epoch', 0)
                if (not math.isfinite(when) or type(last) not in (int, float)
                        or not math.isfinite(last)):
                    return None
                if previous.get('token') == token and when - last < REMINDER_SECONDS:
                    return None
                check_executor(binding, payload)
                if not inquiry_active(state, now=now):
                    return None
                if 'executor_identity' not in state:
                    _read_sidecar(state)  # Recheck pinned maintenance authority before output.
                emit(notice)  # A failed flush never consumes the cursor.
                ready.write_json(path, dict(token=token, offered_epoch=when,
                                            episode_id=state['episode_id'], consumed=False))
            return notice
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caller-uuid', required=True)
    parser.add_argument('--episode', required=True)
    parser.add_argument('--state-sha256', required=True)
    parser.add_argument('--authorization-file', required=True)
    parser.add_argument('--creation-proof', required=True)
    parser.add_argument('--creation-proof-sha256', required=True)
    parser.add_argument('--maintenance-authorization',
                        help='absolute successor-rebind-v2 artifact for the actual maintenance owner')
    args = parser.parse_args(argv)
    state = reask.load(args.caller_uuid) or {}
    # Explicitly authorized maintenance owner may register the proven existing
    # executor. No business write ownership or consumption authority transfers.
    bridge = ready._bridge()
    if args.maintenance_authorization:
        artifact = json.loads(read_bytes(Path(args.maintenance_authorization), limit=MAX_CREATION_BYTES))
        if artifact.get('schema') != successor_rebind.SCHEMA_V2:
            raise ValueError('maintenance enrollment requires successor-rebind-v2')
        live = bridge.pin_successor_pair(artifact, expected_target=args.caller_uuid)
    else:
        live = bridge.pin_workspace(state['supervisor'], caller_uuid=args.caller_uuid)
    print(json.dumps(enroll_existing(live, args.episode, args.state_sha256,
                                    args.authorization_file, args.creation_proof,
                                    args.creation_proof_sha256,
                                    maintenance_authorization=args.maintenance_authorization),
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
