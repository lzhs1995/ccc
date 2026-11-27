#!/usr/bin/env python3
"""Explicitly enrolled, durable file-only inquiries; no terminal/native input.

This is a separate launchd job, not a configuration guardian or Stop hook.
Enrollment occurs only after executor_reask start authenticates its live pair.
The background job preserves that authorization, never supplies a foreground
identity, and never reconciles or repeats an original transport attempt.
"""
import time

# Earliest Python module boundary; interpreter startup and time before this
# module was scheduled are NOT measured. Keep heavy imports below this fence.
_MODULE_ENTRY_EPOCH = time.time()
_MODULE_ENTRY_MONOTONIC = time.monotonic()

import argparse
import copy
from bisect import bisect_right
from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import uuid

import configuration_guardian as guardian
import executor_ready as ready
import executor_reask as reask
import executor_reask_protocol as protocol
from cmux_evidence_io import open_regular_lock, read_bytes

_IMPORTS_FINISHED_MONOTONIC = time.monotonic()

LABEL = 'org.multi-agent-collaboration.executor-reask-scheduler'
SCHEMA = 'executor-reask-enrollment-v2'
LEGACY_SCHEMA = 'executor-reask-enrollment-v1'
TRANSACTION_SCHEMA = 'executor-reask-registration-transaction-v1'
MAX_ENROLLMENTS = 256
MAX_CHANNELS = 8
MAX_SWEEP_SECONDS = 10.0
SWEEP_INTERVAL_SECONDS = 60.0
HISTORY_SCHEMA = 'executor-reask-sweep-history-v1'
MAX_HISTORY_ENTRIES = 120
MAX_HISTORY_BYTES = 512 * 1024
MAX_HISTORY_SECONDS = 0.25
BINDING_KEYS = ('caller_surface_uuid', 'supervisor', 'workspace_uuid', 'task_id',
                'purpose', 'episode_id', 'channels', 'mailbox')
LIVE_KEYS = ('workspace_uuid', 'caller_surface_uuid', 'target_surface_uuid', 'target_pane_uuid')


def root():
    return ready.state_root() / 'executor-reask-scheduler-v1'


def enrollment_path(caller):
    return root() / 'enrolled' / (ready.digest(caller) + '.json')


def secure_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError('scheduler state must be a private canonical directory')


class RegistryBusy(BlockingIOError):
    """Only failure to acquire the scheduler registry lock."""


@contextmanager
def registry_lock():
    """Serialize registrations so simultaneous callers cannot exceed the cap."""
    secure_directory(root())
    fd = os.open(root() / 'registry.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RegistryBusy('scheduler registry is busy') from exc
        yield


def registration_result(function):
    """Expose lock contention without reclassifying failures inside the body."""
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RegistryBusy:
            return dict(outcome='REGISTRY_BUSY', input_operations=0,
                        retry_same_arguments=True)
    return invoke


def check_capacity(directory):
    # Stop reading as soon as the finite cap is reached, even in a corrupt or
    # unexpectedly crowded directory. Never enumerate the complete directory.
    with os.scandir(directory) as entries:
        for index, _entry in enumerate(entries):
            if index + 1 >= MAX_ENROLLMENTS:
                raise ValueError('scheduler enrollment capacity reached')


def canonical_uuid(value):
    return str(uuid.UUID(value)).upper()


def provenance():
    path = Path(__file__).resolve()
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                python=str(Path(sys.executable).resolve()), label=LABEL)


def approved_release(manifest=None):
    """Verify CURRENT and all immutable members, including this scheduler."""
    current, pins = guardian.verify_release(
        Path.home(), manifest or ready.state_root() / 'CURRENT.json')
    path = Path(current['source']) / 'scripts/executor_reask_scheduler.py'
    if (path != Path(__file__).resolve()
            or 'scripts/executor_reask_scheduler.py' not in pins['files']):
        raise ValueError('scheduler is not pinned in CURRENT; do not migrate episodes')
    return current


def binding(state):
    value = {key: state.get(key) for key in BINDING_KEYS}
    # An old enrollment must compare exactly as issued. Never infer or add a
    # native identity when registering/reusing a historical inquiry.
    if 'executor_identity' in state:
        value['executor_identity'] = copy.deepcopy(state['executor_identity'])
    if state.get('continuation'):
        from executor_reask_lifecycle import binding as lifecycle_binding
        value['continuation'] = lifecycle_binding(state['continuation'])
    for key in ('caller_surface_uuid', 'supervisor', 'workspace_uuid'):
        if canonical_uuid(value[key]) != value[key]:
            raise ValueError('episode binding must use canonical UUIDs')
    if (not isinstance(value['task_id'], str) or len(value['task_id']) > 4096
            or not isinstance(value['episode_id'], str) or not value['episode_id']
            or len(value['episode_id']) > 128
            or value['purpose'] not in ('REQUEST_NEXT_TASK', 'PENDING_TASK_REPLY')):
        raise ValueError('invalid inquiry scope')
    channels = value['channels']
    if not isinstance(channels, list) or len(channels) > MAX_CHANNELS:
        raise ValueError('inquiry channel budget exceeded')
    for channel in channels:
        if (not isinstance(channel, str) or not Path(channel).is_absolute()
                or Path(channel).resolve() != Path(channel)):
            raise ValueError('file channels must be canonical absolute directories')
    if value['mailbox'] != str(ready.mailbox_dir(value['caller_surface_uuid'])):
        raise ValueError('noncanonical reply mailbox')
    return value


def _raw(path):
    try:
        return read_bytes(path, limit=ready.MAX_JSON_BYTES)
    except FileNotFoundError:
        return None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else 'ABSENT'


def _live_pins(live, *, refresh=False):
    pins = {key: canonical_uuid(live[key]) for key in LIVE_KEYS}
    if refresh:
        fresh = ready._bridge().pin_workspace(pins['target_surface_uuid'],
                                              caller_uuid=pins['caller_surface_uuid'])
        if pins != {key: canonical_uuid(fresh[key]) for key in LIVE_KEYS}:
            raise ValueError('live pair changed during registration')
    return pins


def _pair_matches(state, pins):
    return (pins['caller_surface_uuid'] == state['caller_surface_uuid']
            and pins['target_surface_uuid'] == state['supervisor']
            and pins['workspace_uuid'] == state['workspace_uuid'])


def _registration_matches(record, state, path):
    if record.get('schema') == LEGACY_SCHEMA:
        schedule = state.get('file_schedule') or {}
        return (not state.get('registration') and schedule.get('enrollment') == str(path)
                and schedule.get('registered_epoch') == record.get('enrolled_epoch'))
    registration = record.get('registration')
    return (isinstance(registration, dict) and registration == state.get('registration')
            and set(registration) == {'token', 'enrollment'}
            and isinstance(registration['token'], str) and len(registration['token']) == 32
            and all(c in '0123456789abcdef' for c in registration['token'])
            and registration['enrollment'] == str(path))


def _transaction(envelope, path):
    """Read the exact immutable intent; PREPARING alone is never a commit."""
    caller, token = envelope['caller_surface_uuid'], envelope['token']
    directory = root() / 'transactions' / ready.digest(caller) / token
    pin = envelope['intent']
    if (path != enrollment_path(caller) or Path(pin['path']) != directory / 'intent.json'
            or len(token) != 32 or any(c not in '0123456789abcdef' for c in token)):
        raise ValueError('invalid registration intent binding')
    raw = read_bytes(directory / 'intent.json', limit=ready.MAX_JSON_BYTES)
    if _sha(raw) != pin['sha256']:
        raise ValueError('registration intent changed')
    tx = json.loads(raw)
    if (tx.get('schema') != TRANSACTION_SCHEMA or tx.get('token') != token
            or tx.get('caller_surface_uuid') != caller
            or any(tx.get(key) != envelope.get(key) for key in ('active', 'prepared'))):
        raise ValueError('registration envelope differs from its intent')
    if _sha(_raw(directory / 'INDEX.json')) != envelope.get('snapshot_sha256'):
        raise ValueError('registration snapshot index changed')
    index = reask.snapshot_files(
        directory, {}, dict(schema=TRANSACTION_SCHEMA, caller=caller, token=token))
    if set(index['files']).union(index['absent']) != {
            'intent.json', 'previous-state.json', 'previous-enrollment.json',
            'previous-stop.json', 'prepared-state.json'}:
        raise ValueError('registration snapshot is incomplete')
    for name, key in (('previous-state.json', 'previous_state_sha256'),
                      ('previous-enrollment.json', 'previous_enrollment_sha256')):
        if _sha(_raw(directory / name)) != tx.get(key):
            raise ValueError('registration preimage differs from its intent')
    return tx, directory


def _select(record, state, path):
    if not isinstance(record, dict):
        return None
    if record.get('schema') != TRANSACTION_SCHEMA:
        return record
    tx, _directory = _transaction(record, path)
    # Always consult the disk state, never an uncommitted in-memory candidate.
    latest = reask.load(tx['caller_surface_uuid']) or {}
    if state and (binding(latest) != binding(state)
                  or latest.get('registration') != state.get('registration')):
        return None
    for key in ('prepared', 'active'):
        choice = tx.get(key)
        if (choice and latest and choice.get('binding') == binding(latest)
                and _registration_matches(choice, latest, path)):
            return choice
    return None


def matches(record, state, path, *, verification=None):
    try:
        if not isinstance(record, dict) or record.get('schema') == TRANSACTION_SCHEMA:
            return False
        latest = reask.load(state['caller_surface_uuid'])
        if (not latest or binding(latest) != binding(state)
                or latest.get('registration') != state.get('registration')):
            return False
        record = _select(record, latest, path)
        if not record:
            return False
        expected = binding(latest)
        pins = record['live_binding']
        finished = (record.get('status') == 'FINISHED' and latest.get('state') != reask.WAITING
                    and not _continuing(latest))
        return (record.get('schema') in (SCHEMA, LEGACY_SCHEMA)
                and (record.get('status') == 'ENROLLED' or finished)
                and record.get('authorization') in ('EXPLICIT_REASK_START', 'EXPLICIT_REASK_ENROLL_EXISTING')
                and (record.get('schema') != LEGACY_SCHEMA or
                     record.get('authorization') == 'EXPLICIT_REASK_START')
                and record.get('uid') == os.getuid() and record.get('binding') == expected
                and path == enrollment_path(expected['caller_surface_uuid'])
                and all(canonical_uuid(pins[key]) == pins[key] for key in LIVE_KEYS)
                and _pair_matches(latest, pins) and _registration_matches(record, latest, path)
                and protocol.supports_record(record, latest, provenance(), verification=verification))
    except (KeyError, ValueError, TypeError, AttributeError, OSError):
        return False


def registered(state):
    path = enrollment_path(state['caller_surface_uuid'])
    return matches(ready.read_json(path), state, path)


def registration_observation(state, *, verification=None):
    """A committed token and completed registration are distinct observations."""
    path = enrollment_path(state['caller_surface_uuid'])
    record = ready.read_json(path) or {}
    if record.get('schema') == TRANSACTION_SCHEMA:
        try:
            selected = _select(record, state, path)
            if selected and selected == record.get('prepared'):
                return 'COMMITTED_RECOVERY_REQUIRED'
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return 'REGISTRATION_UNCOMMITTED'
    return ('REGISTERED' if matches(record, state, path, verification=verification)
            else 'REGISTRATION_UNCOMMITTED')


def _bound_record(record, state, path):
    selected = _select(record, state, path)
    if (selected and selected.get('binding') == binding(state)
            and _registration_matches(selected, state, path)):
        return selected
    return None


def _continuing(state):
    return (state.get('continuation') or {}).get('status') == 'ACTIVE'


def _available(state, pins, now, *, preview_resume=False):
    if not _pair_matches(state, pins) or state.get('state') != reask.WAITING:
        raise ValueError('live pair and waiting episode do not match')
    from executor_reask_lifecycle import (poll_authorization, observe_formal_tasks,
                                         reservation_inquiries_allowed)
    preview = copy.deepcopy(state)
    poll_authorization(preview, now, publish=False)
    observe_formal_tasks(preview, now, publish=False, preview_resume=preview_resume)
    if (preview.get('state') != reask.WAITING or preview.get('paused_tasks')
            and not reservation_inquiries_allowed(preview)):
        raise ValueError('inquiry authorization ended or a formal task owns the executor')


def _post_commit(tx, directory):
    """Retire only the old episode's own channel, after the live commit."""
    latest = reask.load(tx['caller_surface_uuid']) or {}
    if not _registration_matches(tx['prepared'], latest, enrollment_path(tx['caller_surface_uuid'])):
        return
    if tx.get('resume_stopped'):
        path = reask.stop_file(tx['caller_surface_uuid'])
        before = _raw(directory / 'previous-stop.json')
        if before is not None and _raw(path) == before:
            path.unlink()
            ready.sync_directory(path.parent)
    previous = _raw(directory / 'previous-state.json')
    if previous is not None:
        old = json.loads(previous)
        if old.get('episode_id') != latest.get('episode_id'):
            marker = old['asks'][-1]['marker'] if old.get('asks') else ''
            reask.write_channels(old, marker, 'superseded inquiry; original attempts remain held',
                                 tx['at_epoch'], status='SUPERSEDED')


def _recover(record, state, path):
    if record.get('schema') != TRANSACTION_SCHEMA:
        return record
    tx, directory = _transaction(record, path)
    selected = _select(record, state, path)
    if selected and selected == tx['prepared']:
        # Leave the intent reachable until all idempotent post-commit work has
        # succeeded. A failed fsync/cleanup must not turn into a lost transaction.
        _post_commit(tx, directory)
        ready.write_json(path, selected)
        return selected
    if selected or _sha(_raw(reask.state_path(tx['caller_surface_uuid']))) == tx['previous_state_sha256']:
        # No new token was committed. Restore only the exact old enrollment;
        # never roll back live state or generate another episode in a sweep.
        previous = _raw(directory / 'previous-enrollment.json')
        _restore_enrollment(path, previous)
        return json.loads(previous) if previous is not None else None
    return None


def _restore_enrollment(path, raw):
    if raw is None:
        path.unlink(missing_ok=True)
        ready.sync_directory(path.parent)
        return
    fd, pending = tempfile.mkstemp(prefix='.registration-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        ready.sync_directory(path.parent)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def _commit(state, pins, previous, registration_raw, *, authorization, original, now,
            resume_stopped=False):
    caller = state['caller_surface_uuid']
    path = enrollment_path(caller)
    secure_directory(path.parent)
    if registration_raw is None:
        check_capacity(path.parent)
    before = _raw(reask.state_path(caller))
    if (json.loads(before) if before is not None else None) != previous:
        raise ValueError('inquiry state changed before registration')
    if _raw(path) != registration_raw:
        raise ValueError('registration changed before commit')
    old_record = json.loads(registration_raw) if registration_raw is not None else None
    active = _bound_record(old_record, previous, path) if previous else None
    if (previous and previous.get('episode_id') == state.get('episode_id')
            and (state.get('continuation') or {}).get('reservation_inquiries') is True
            and active is None):
        raise ValueError('reservation inquiries require the original enrollment or a new authenticated episode')
    token = uuid.uuid4().hex
    registration = dict(token=token, enrollment=str(path))
    candidate = copy.deepcopy(state)
    candidate['registration'] = registration
    candidate['file_schedule'] = dict(enrollment=str(path), registered_epoch=now,
                                       execution_proven=False, label=LABEL)
    prepared = dict(schema=SCHEMA, status='ENROLLED', authorization=authorization,
                    binding=binding(candidate), live_binding=pins, controller=original,
                    scheduler=provenance(), file_protocol=protocol.descriptor(), registration=registration,
                    uid=os.getuid(), enrolled_epoch=now, input_operations=0)
    if not protocol.supports_record(prepared, candidate, provenance()):
        raise ValueError('original controller is not supported by the file protocol')
    tx = dict(schema=TRANSACTION_SCHEMA, token=token, caller_surface_uuid=caller,
              active=active, prepared=prepared, at_epoch=now, resume_stopped=resume_stopped,
              previous_state_sha256=_sha(before), previous_enrollment_sha256=_sha(registration_raw))
    directory = root() / 'transactions' / ready.digest(caller) / token
    raw_tx = json.dumps(tx, ensure_ascii=False).encode()
    files = {'intent.json': raw_tx, 'previous-state.json': before,
             'previous-enrollment.json': registration_raw,
             'previous-stop.json': _raw(reask.stop_file(caller)),
             'prepared-state.json': json.dumps(candidate, ensure_ascii=False).encode()}
    reask.snapshot_files(directory, files, dict(schema=TRANSACTION_SCHEMA, caller=caller, token=token))
    envelope = dict(tx, status='PREPARING',
                    intent=dict(path=str(directory / 'intent.json'), sha256=_sha(raw_tx)),
                    snapshot_sha256=_sha(_raw(directory / 'INDEX.json')))
    try:
        # PREPARING has a recoverable old enrollment. The one commit point is
        # replacement of the live state with the matching registration token.
        ready.write_json(path, envelope)
        if _raw(reask.state_path(caller)) != before:
            raise ValueError('inquiry state changed during preparation')
        reask.save(candidate)
        _post_commit(tx, directory)
        ready.write_json(path, prepared)
    except (Exception, ready.WaitBudgetExpired) as exc:
        visible = reask.load(caller) or {}
        if not _registration_matches(prepared, visible, path):
            return dict(outcome='REGISTRATION_UNCOMMITTED', input_operations=0,
                        error_type=type(exc).__name__, intent=str(directory / 'intent.json'))
        # replace may have succeeded before fsync raised. Never overwrite that
        # live commit with the old state, nor repeat the start as a new episode.
        return dict(visible, registration_observation='COMMITTED_RECOVERY_REQUIRED',
                    registration_error_type=type(exc).__name__)
    return candidate


@registration_result
def start(live, *, now=None, authenticate_executor=False, **kwargs):
    """Atomically register an explicitly authenticated new inquiry episode."""
    now = time.time() if now is None else now
    caller = canonical_uuid(live['caller_surface_uuid'])
    with registry_lock(), reask.episode_lock(caller):
        current = approved_release()
        pins = _live_pins(live, refresh=True)
        if reask.controller()['source'] != current['source']:
            raise ValueError('scheduler and inquiry controller differ')
        previous = reask.load(caller)
        path = enrollment_path(caller)
        record = ready.read_json(path)
        if record and record.get('schema') == TRANSACTION_SCHEMA:
            _recover(record, previous, path)
            record = ready.read_json(path)
            if record and record.get('schema') == TRANSACTION_SCHEMA:
                return dict(outcome='REGISTRATION_UNCOMMITTED', input_operations=0)
        state = reask.prepare_start_locked(caller, pins['target_surface_uuid'],
                                            workspace=pins['workspace_uuid'], now=now, **kwargs)
        if state.get('state') != reask.WAITING:
            return state
        raw = _raw(path)
        record = json.loads(raw) if raw is not None else None
        if previous and state.get('episode_id') == previous.get('episode_id'):
            if record and matches(record, previous, path):
                return previous
            if record:
                raise ValueError('use explicit enroll-existing for the original registration')
        elif authenticate_executor:
            # Capture only a genuinely new episode, inside both locks and
            # before the registration transaction commits. This is the
            # foreground CLI; the background scheduler never reads transcripts.
            from executor_reply_advisory import capture_executor
            native_identity = capture_executor(live, state=state)
            if not isinstance(native_identity, dict):
                raise ValueError('new foreground inquiry requires its original executor identity')
            state['executor_identity'] = native_identity
        # The old stop file stays intact until the new registration commits.
        # Only an explicitly resumed, genuinely new episode may preview past it.
        preview_resume = bool(kwargs.get('resume_stopped') and (
            not previous or state.get('episode_id') != previous.get('episode_id')))
        _available(state, pins, now, preview_resume=preview_resume)
        return _commit(state, pins, previous, raw, authorization='EXPLICIT_REASK_START',
                       original=reask.controller(), now=now,
                       resume_stopped=kwargs.get('resume_stopped', False))


@registration_result
def enroll(state, live, *, now=None):
    """Compatibility API for already authenticated, locally created episodes."""
    now = time.time() if now is None else now
    caller = state['caller_surface_uuid']
    with registry_lock(), reask.episode_lock(caller):
        current = approved_release()
        latest = reask.load(caller)
        pins = _live_pins(live)
        if not latest or binding(latest) != binding(state) or not _pair_matches(latest, pins):
            raise ValueError('live pair and episode do not match')
        if reask.controller()['source'] != current['source']:
            raise ValueError('scheduler and inquiry controller differ')
        path = enrollment_path(caller)
        raw = _raw(path)
        old = json.loads(raw) if raw is not None else None
        if old and old.get('schema') == TRANSACTION_SCHEMA:
            _recover(old, latest, path)
            raw = _raw(path)
            old = json.loads(raw) if raw is not None else None
            if old and old.get('schema') == TRANSACTION_SCHEMA:
                return dict(outcome='REGISTRATION_UNCOMMITTED', input_operations=0)
        if old and old.get('binding', {}).get('episode_id') == state['episode_id']:
            if not matches(old, latest, path):
                raise ValueError('existing enrollment belongs to its original controller/binding')
            state.update(latest)
            return _select(old, latest, path)
        if reask.stop_file(caller).exists():
            raise ValueError('operator stop is in effect')
        _available(latest, pins, now)
        result = _commit(latest, pins, latest, raw, authorization='EXPLICIT_REASK_START',
                         original=reask.controller(), now=now)
        if result.get('outcome') or result.get('registration_observation'):
            return result
        state.update(result)
        return _select(ready.read_json(path), result, path)


@registration_result
def enroll_existing(live, episode, enrollment_sha256, *, now=None):
    """Original executor explicitly registers exact old evidence; never ticks."""
    now = time.time() if now is None else now
    caller = canonical_uuid(live['caller_surface_uuid'])
    with registry_lock(), reask.episode_lock(caller):
        approved_release()
        pins = _live_pins(live, refresh=True)
        state = reask.load(caller)
        path = enrollment_path(caller)
        raw = _raw(path)
        if not state or state.get('episode_id') != episode or _sha(raw) != enrollment_sha256:
            raise ValueError('original episode or registration SHA changed')
        record = json.loads(raw) if raw is not None else None
        if record and record.get('schema') == TRANSACTION_SCHEMA:
            _recover(record, state, path)
            raw = _raw(path)
            record = json.loads(raw) if raw is not None else None
            if record and record.get('schema') == TRANSACTION_SCHEMA:
                return dict(outcome='REGISTRATION_UNCOMMITTED', input_operations=0)
        old = _bound_record(record, state, path) if record is not None else None
        if record is not None and old is None:
            prior_binding = record.get('binding') if isinstance(record, dict) else None
            prior_episode = prior_binding.get('episode_id') if isinstance(prior_binding, dict) else None
            # An authenticated original episode may replace a pinned enrollment
            # left by a different episode. It cannot repair a changed binding
            # for this episode or acquire reservation privileges by re-enrolling.
            if (not isinstance(record, dict) or record.get('schema') not in (SCHEMA, LEGACY_SCHEMA)
                    or not isinstance(prior_episode, str) or not prior_episode
                    or prior_episode == state['episode_id']
                    or (state.get('continuation') or {}).get('reservation_inquiries') is True):
                raise ValueError('existing enrollment belongs to its original controller/binding')
        if (reask.stop_file(caller).exists() or state.get('state') != reask.WAITING
                or old and old.get('status') == 'FINISHED'):
            raise ValueError('stopped or finished inquiries cannot be re-enrolled')
        _available(state, pins, now)
        original = protocol.state_controller(state, old)
        if (not protocol.supports_state(state, old) or
                old and not protocol.supports_record(old, state, provenance())):
            raise ValueError('unsupported original file controller')
        if old and registered(state):
            return state
        reask.preserve_legacy_snapshot(state, old)
        return _commit(state, pins, state, raw, authorization='EXPLICIT_REASK_ENROLL_EXISTING',
                       original=original, now=now)


def never_send(*_args, **_kwargs):
    raise AssertionError('file scheduler must never invoke transport')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _cadence(previous, now):
    result = dict(status='FIRST_OBSERVATION', interval_seconds=SWEEP_INTERVAL_SECONDS)
    before = previous.get('at_epoch')
    if _finite(before) and _finite(now):
        gap = now - before
        result.update(previous_sweep_epoch=before, interval_observed_seconds=gap,
                      late_by_seconds=max(0, gap - SWEEP_INTERVAL_SECONDS),
                      status=('CLOCK_REGRESSED' if gap < 0 else 'SWEEP_LATE'
                              if gap > 2 * SWEEP_INTERVAL_SECONDS else 'ON_TIME'))
    return result


def _release_observation(state, previous, now, *, verification=None):
    """Observe the latest exact RELEASE; bounded history is not a full ledger.

    A new boundary carries an atomic file-post watermark. Without that proof,
    or once multiple posts have already passed, report only the first *visible*
    post. Never infer RELEASE from marker expiry/absence or an idle screen.
    """
    boundary = state.get('last_task_boundary')
    if (not isinstance(boundary, dict) or boundary.get('event') != 'RELEASED'
            or not _finite(boundary.get('at_epoch')) or not _finite(now)):
        return None
    if (isinstance(previous, dict) and previous.get('boundary') == boundary
            and previous.get('first_visible_post')):
        return copy.deepcopy(previous)
    result = dict(boundary=copy.deepcopy(boundary), observed_epoch=now,
                  episode_state=state.get('state'), first_is_actual_first=False,
                  proof='EPISODE_RECORDED_CHANNEL_SUCCESS', input_operations=0)
    released = boundary['at_epoch']
    watermark = boundary.get('file_posts_at_release')
    attempt_at_release = boundary.get('attempt_count_at_release')
    has_watermark = (type(watermark) is int and watermark >= 0
                     and type(attempt_at_release) is int and attempt_at_release >= 0)
    posts = []
    for ask in state.get('asks', []):
        if (not isinstance(ask, dict) or not _finite(ask.get('at_epoch'))
                or not isinstance(ask.get('channels'), dict)
                or not ask['channels'].get('written')):
            continue
        if has_watermark:
            if type(ask.get('attempt')) is not int or ask['attempt'] <= attempt_at_release:
                continue
        elif ask['at_epoch'] < released:
            continue
        posts.append(ask)
    if posts:
        first = posts[0]  # retain issued order, not wall-clock sort order
        delay = first['at_epoch'] - released
        exact = has_watermark and state.get('file_posts') == watermark + 1
        result.update(status='FIRST_RESUMED_POST_OBSERVED' if exact
                      else 'FIRST_VISIBLE_POST_OBSERVED', first_is_actual_first=exact,
                      first_visible_post={k: copy.deepcopy(first.get(k))
                                          for k in ('marker', 'attempt', 'at_epoch', 'channels')},
                      first_visible_delay_seconds=delay,
                      delay_status=('CLOCK_REGRESSED' if delay < 0 else 'RESUME_LATE'
                                    if delay > 2 * SWEEP_INTERVAL_SECONDS else 'ON_TIME'))
        if exact:
            result['release_to_first_file_post_seconds'] = delay
    else:
        from executor_reask_lifecycle import reservation_inquiries_allowed
        eligible = (state.get('state') == reask.WAITING
                    and (not state.get('paused_tasks') or
                         reservation_inquiries_allowed(state, verification=verification)))
        elapsed = now - released
        result.update(status='AWAITING_FILE_POST' if eligible else 'RESUME_NOT_CURRENTLY_ELIGIBLE',
                      elapsed_since_release_seconds=elapsed,
                      delay_status=('CLOCK_REGRESSED' if elapsed < 0 else 'RESUME_LATE'
                                    if eligible and elapsed > 2 * SWEEP_INTERVAL_SECONDS else 'PENDING'))
    return result


def _sweep_one(path, now, execution, *, verification=None):
    row = dict(enrollment=path.name, status='INVALID_ENROLLMENT', file_posts=0)
    lock_scope = 'REGISTRY_BUSY'
    try:
        # Registration and task writers use the same order. Reread only after
        # acquiring each lock; a PREPARING envelope has no top-level binding.
        with registry_lock():
            record = ready.read_json(path) or {}
            caller = (record.get('caller_surface_uuid') if record.get('schema') == TRANSACTION_SCHEMA
                      else record.get('binding', {}).get('caller_surface_uuid'))
            if not isinstance(caller, str) or canonical_uuid(caller) != caller or path != enrollment_path(caller):
                return row
            row['caller_surface_uuid'] = caller
            lock_scope = 'EPISODE_BUSY'
            with reask.episode_lock(caller):
                record = ready.read_json(path) or {}
                state = reask.load(caller)
                if state and state.get('caller_surface_uuid') != caller:
                    return dict(row, status='INVALID_EPISODE')
                if state and reask.stop_file(caller).exists():
                    state = reask.tick_locked(caller, now, never_send, file_only=True, file_record=record)
                if record.get('schema') == TRANSACTION_SCHEMA:
                    try:
                        # An interrupted first registration has no live state.
                        # Recover its exact ABSENT preimage under the same locks
                        # before requiring an episode; never create one here.
                        record = _recover(record, state, path)
                    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                        observed = (registration_observation(state) if state
                                    else 'REGISTRATION_UNCOMMITTED')
                        return dict(row, status=observed, error_type=type(exc).__name__)
                    if not record or record.get('schema') == TRANSACTION_SCHEMA:
                        observed = (registration_observation(state) if state
                                    else 'REGISTRATION_UNCOMMITTED')
                        return dict(row, status=observed)
                if not state:
                    return dict(row, status='INVALID_EPISODE')
                # Formal ownership must stop old inquiries even when their
                # historical producer is outside the file compatibility list.
                reask.observe_boundaries_locked(state, time.time() if now is None else now)
                bound = _bound_record(record, state, path)
                if state.get('state') == reask.STOPPED:
                    row.update(status=reask.STOPPED, episode_id=state.get('episode_id'))
                    if record.get('status') == 'FINISHED' or bound is None:
                        return row
                elif record.get('status') == 'FINISHED':
                    return dict(row, status='FINISHED', episode_id=state.get('episode_id'))
                elif matches(record, state, path, verification=verification):
                    previous_posts = state.get('file_posts', 0)
                    state = reask.tick_locked(caller, now, never_send, file_only=True,
                                              file_record=record, verification=verification)
                    if state.get('outcome') or state.get('registration_observation'):
                        return dict(row, status=reask.summary(state)['outcome'])
                    row.update(status=state['state'], episode_id=state['episode_id'],
                               file_posts=state.get('file_posts', 0) - previous_posts,
                               next_due_epoch=state.get('next_due_epoch'))
                    if state.get('paused_tasks'):
                        row['paused_tasks'] = copy.deepcopy(state['paused_tasks'])
                        from executor_reask_lifecycle import reservation_inquiries_allowed
                        row['reservation_inquiries_authorized'] = reservation_inquiries_allowed(
                            state, verification=verification)
                else:
                    return dict(row, status='BINDING_OR_CONTROLLER_MISMATCH')
                if ready.read_json(path) != record:
                    return dict(row, status='ENROLLMENT_CHANGED')
                observation = _release_observation(
                    state, record.get('release_resume_observation'),
                    time.time() if now is None else now, verification=verification)
                if observation is not None:
                    row['release_resume_observation'] = observation
                    record['release_resume_observation'] = observation
                record['last_execution'] = dict(execution, state=state['state'],
                                               file_posts=row['file_posts'], input_operations=0)
                if state['state'] != reask.WAITING and not (
                        _continuing(state) and state['state'] == reask.ANSWERED):
                    record['status'] = 'FINISHED'
                ready.write_json(path, record)
    except BlockingIOError:
        row['status'] = lock_scope
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        row.update(status='INVALID_EPISODE', error_type=type(exc).__name__)
    return row


def _history_entry(result):
    """Diagnostic facts only: no prompt bodies, reservation payloads or authority."""
    keys = ('at_epoch', 'source', 'status', 'phase', 'phase_seconds', 'duration_seconds',
            'file_posts', 'file_posts_complete', 'status_record_unchanged',
            'previous_enrollment', 'last_enrollment', 'pending_enrollment',
            'enrollment_order', 'cadence', 'startup_observation', 'error_type')
    entry = {key: result[key] for key in keys if key in result}
    entry.update(pid=os.getpid(), input_operations=0, episodes=[])
    row_keys = ('enrollment', 'caller_surface_uuid', 'episode_id', 'status',
                'file_posts', 'duration_seconds', 'next_due_epoch',
                'reservation_inquiries_authorized', 'error_type')
    for row in result.get('episodes', []):
        compact = {key: row[key] for key in row_keys if key in row}
        if 'paused_tasks' in row:
            compact['paused_task_count'] = len(row['paused_tasks'])
        entry['episodes'].append(compact)
    return entry


def record_history(result):
    """Bound history independently; contention/failure cannot change sweep work.

    STATUS retains the worker's rotation state. A competing worker may append
    SCHEDULER_BUSY here but must never replace that STATUS. Missing history is
    not proof that launchd did not run: this write has its own finite budget.
    """
    started = time.monotonic()
    receipt = dict(status='HISTORY_UNCONFIRMED', phase='LOCK')
    try:
        with ready.runtime_limit(MAX_HISTORY_SECONDS):
            secure_directory(root())
            with open_regular_lock(root() / 'history.lock') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    receipt['status'] = 'HISTORY_BUSY'
                    return receipt
                receipt['phase'] = 'READ'
                path = root() / 'SWEEP-HISTORY.json'
                try:
                    history = json.loads(read_bytes(path, limit=MAX_HISTORY_BYTES))
                except FileNotFoundError:
                    history = dict(schema=HISTORY_SCHEMA, entries=[])
                if (not isinstance(history, dict) or history.get('schema') != HISTORY_SCHEMA
                        or not isinstance(history.get('entries'), list)
                        or len(history['entries']) > MAX_HISTORY_ENTRIES
                        or not all(isinstance(entry, dict) for entry in history['entries'])):
                    raise ValueError('invalid bounded sweep history')
                receipt['phase'] = 'COMPACT'
                entries = history['entries'] + [_history_entry(result)]
                payload = dict(schema=HISTORY_SCHEMA, entries=[])
                # Exact json.dump byte size, computed once per retained entry.
                # Do not repeatedly serialize a full history while trimming it.
                size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                for entry in reversed(entries[-MAX_HISTORY_ENTRIES:]):
                    extra = len(json.dumps(entry, ensure_ascii=False).encode('utf-8'))
                    extra += 2 if payload['entries'] else 0
                    if size + extra > MAX_HISTORY_BYTES:
                        if not payload['entries']:
                            raise ValueError('one sweep exceeds the history byte budget')
                        break
                    payload['entries'].append(entry)
                    size += extra
                payload['entries'].reverse()
                receipt['phase'] = 'WRITE'
                ready.write_json(path, payload)
                receipt.update(status='HISTORY_RECORDED', phase='COMPLETE',
                               retained_entries=len(payload['entries']), bytes=size)
    except ready.WaitBudgetExpired:
        receipt['status'] = 'HISTORY_BUDGET_EXHAUSTED'
    except Exception as exc:
        # write_json can raise after replacement but before directory fsync.
        # UNCONFIRMED does not assert that the record is absent.
        receipt.update(status='HISTORY_UNCONFIRMED', error_type=type(exc).__name__)
    finally:
        receipt['duration_seconds'] = max(0, time.monotonic() - started)
    return receipt


def sweep(*, manifest=None, now=None, observe_startup=False):
    """A bounded zero-input sweep, skipping busy/mismatched/unenrolled episodes."""
    started = time.monotonic()
    result = dict(status='SWEPT', label=LABEL, at_epoch=time.time() if now is None else now,
                  input_operations=0, file_posts=0, episodes=[],
                  hook_exposure_proven=False, model_reading_proven=False,
                  native_reception_proven=False, phase='LOCK', phase_seconds={})
    if observe_startup:
        result['startup_observation'] = dict(
            boundary='PYTHON_MODULE_ENTRY_AFTER_TIME_IMPORT',
            module_entry_epoch=_MODULE_ENTRY_EPOCH,
            module_to_sweep_seconds=max(0, started - _MODULE_ENTRY_MONOTONIC),
            module_import_seconds=max(0, _IMPORTS_FINISHED_MONOTONIC - _MODULE_ENTRY_MONOTONIC),
            interpreter_start_observed=False, pid=os.getpid())
    phase_started = started
    cursor_loaded = False
    verification = protocol.ReleaseVerification()

    def phase(name):
        nonlocal phase_started
        current = time.monotonic()
        previous = result['phase']
        result['phase_seconds'][previous] = (
            result['phase_seconds'].get(previous, 0) + max(0, current - phase_started))
        result['phase'] = name
        phase_started = current

    secure_directory(root())
    fd = os.open(root() / 'sweep.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            phase('LOCK')
            result.update(status='SCHEDULER_BUSY', file_posts_complete=True,
                          status_record_unchanged=True,
                          duration_seconds=max(0, time.monotonic() - started))
            result['history'] = record_history(result)
            return result
        try:
            with ready.runtime_limit(MAX_SWEEP_SECONDS):
                # Preserve rotation before potentially slow full-release I/O.
                # An early verification deadline must not reset a prior cursor.
                phase('LOAD_CURSOR')
                previous = ready.read_json(root() / 'STATUS.json') or {}
                result['cadence'] = _cadence(previous, result['at_epoch'])
                last = previous.get('last_enrollment', '')
                if not isinstance(last, str):
                    last = ''
                result['last_enrollment'] = last
                result['previous_enrollment'] = last
                cursor_loaded = True
                phase('VERIFY_RELEASE')
                current = approved_release(manifest)
                result['source'] = current['source']
                execution = dict(at_epoch=result['at_epoch'], source=current['source'],
                                 scheduler=provenance())
                phase('ENUMERATE')
                directory = root() / 'enrolled'
                paths = []
                if directory.exists():
                    secure_directory(directory)
                    with os.scandir(directory) as entries:
                        for index, entry in enumerate(entries):
                            if index >= MAX_ENROLLMENTS:
                                raise ValueError('scheduler enrollment capacity exceeded')
                            if entry.is_file(follow_symlinks=False) and entry.name.endswith('.json'):
                                paths.append(Path(entry.path))
                paths.sort()
                start = bisect_right([path.name for path in paths], last)
                ordered = paths[start:] + paths[:start]
                result['enrollment_order'] = [path.name for path in ordered]
                for path in ordered:
                    # Advance before work. A deadline inside a busy/slow caller
                    # must not starve everyone behind it on the next sweep.
                    result['last_enrollment'] = path.name
                    result['pending_enrollment'] = path.name
                    phase('EPISODE')
                    row_started = time.monotonic()
                    row = _sweep_one(path, now, execution, verification=verification)
                    row['duration_seconds'] = max(0, time.monotonic() - row_started)
                    result['file_posts'] += row['file_posts']
                    result['episodes'].append(row)
                    result.pop('pending_enrollment', None)
                phase('COMPLETE')
        except ready.WaitBudgetExpired:
            result['status'] = 'SWEEP_BUDGET_EXHAUSTED'
        except Exception as exc:
            result.update(status='SCHEDULER_FAILED', error_type=type(exc).__name__)
        phase(result['phase'])
        result['duration_seconds'] = max(0, time.monotonic() - started)
        result['release_verification'] = verification.summary()
        # A tick may have committed files before the budget interrupted its
        # return. Partial counters must never be presented as exact zero work.
        result['file_posts_complete'] = result['status'] == 'SWEPT'
        result['status_record_unchanged'] = not cursor_loaded
        result['history'] = record_history(result)
        # If reading the legacy cursor itself timed out, retain its exact
        # source bytes and publish failure separately for the next sweep.
        target = 'STATUS.json' if cursor_loaded else 'LAST-ERROR.json'
        ready.write_json(root() / target, result)
    return result


def launchd_spec(home, source, python, manifest):
    return dict(Label=LABEL, RunAtLoad=True, StartInterval=60, ThrottleInterval=10,
                ProgramArguments=[str(python), '-B', str(Path(source) / 'scripts/executor_reask_scheduler.py'),
                                  'sweep', '--manifest', str(manifest)],
                # A 60-second coordination deadline should not inherit the
                # CPU/I/O throttling of discretionary Background work. Retain
                # the bounded sweep, nonblocking locks and normal QoS.
                StandardOutPath='/dev/null', StandardErrorPath='/dev/null', ProcessType='Standard')


def install_job(manifest=None, *, apply=False):
    manifest = manifest or ready.state_root() / 'CURRENT.json'
    current = approved_release(manifest)
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    raw = plistlib.dumps(launchd_spec(Path.home(), current['source'], current['python'], manifest), sort_keys=True)
    result = dict(status='JOB_PLANNED', plist=str(path), label=LABEL, interval_seconds=60,
                  input_operations=0, enrollment_implied=False, execution_proven=False)
    if not apply:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('symlinked launchd job refused')
    old = path.read_bytes() if path.exists() else None
    domain = 'gui/' + str(os.getuid())
    loaded = subprocess.run(['launchctl', 'print', domain + '/' + LABEL],
                            capture_output=True, timeout=10).returncode == 0
    if old != raw:
        fd, pending = tempfile.mkstemp(prefix='.reask-scheduler-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if path.is_symlink() or (path.read_bytes() if path.exists() else None) != old:
                raise RuntimeError('launchd job changed concurrently')
            os.replace(pending, path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        if loaded:
            subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL],
                           check=True, capture_output=True, timeout=10)
            loaded = False
    if not loaded:
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)],
                       check=True, capture_output=True, timeout=10)
    return dict(result, status='JOB_REGISTERED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['sweep', 'install-job'])
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--apply', action='store_true', help='install-job only')
    args = parser.parse_args(argv)
    try:
        result = (sweep(manifest=args.manifest, observe_startup=True) if args.mode == 'sweep'
                  else install_job(args.manifest, apply=args.apply))
    except Exception as exc:
        result = dict(status='SCHEDULER_FAILED', error_type=type(exc).__name__, input_operations=0)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if result['status'] == 'SCHEDULER_FAILED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
