"""Standby lifecycle ledger. Native preparation and UI adapters are separate.

The ledger never creates a process. Readiness is an in-memory capability earned
from a complete fresh observation; persisted activation/input claims are only
evidence that an attempt has been consumed, never permission to replay it.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import threading
import uuid

import cmux_codex_watch as core


POLICY = 'native-standby-v1'
COUNT = 50
MAX_OBSERVATION_AGE = 2.0


class ObservationExpired(ValueError):
    """No input authority: reacquire observations before an unconsumed action."""
    def __init__(self, message, indexes=()):
        super().__init__(message)
        self.indexes = tuple(indexes)


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)).lower() != value.lower():
        raise ValueError('invalid standby identity')
    return value.lower()


def controller_identifier(value):
    """Validate cmux identity without changing its rule, lock or RPC spelling.

    cmux exports uppercase UUIDs. Its existing controller paths and permission
    records use those exact strings; only our generated IDs are canonicalized.
    """
    identifier(value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def generation(value):
    # The adapter hashes the complete effective configuration, binary/runtime,
    # profile and skill/rule inventory. An absent generation is never current.
    if (not isinstance(value, str) or len(value) != 64
            or any(c not in '0123456789abcdef' for c in value)):
        raise ValueError('missing standby configuration generation')
    return value


def original(value, workspace_id):
    row = {k: value[k] for k in ('index', 'launch_id', 'surface_id', 'workspace_id',
                                'session_id', 'pid', 'birth', 'claim_sha256', 'argv_sha256')}
    if type(row['index']) is not int or not 0 <= row['index'] < COUNT:
        raise ValueError('invalid standby slot')
    for key in ('launch_id', 'session_id'):
        row[key] = identifier(row[key])
    for key in ('surface_id', 'workspace_id'):
        row[key] = controller_identifier(row[key])
    if row['workspace_id'] != controller_identifier(workspace_id):
        raise ValueError('foreign standby workspace')
    if (type(row['pid']) is not int or not 1 < row['pid'] < 2**31
            or not isinstance(row['birth'], list) or len(row['birth']) != 2
            or any(type(n) is not int for n in row['birth'])
            or row['birth'][0] <= 0 or not 0 <= row['birth'][1] < 1000000):
        raise ValueError('invalid standby process birth')
    generation(row['claim_sha256'])
    generation(row['argv_sha256'])
    if 'writer_lock' in value or 'writer_identity' in value:
        lock = Path(value['writer_lock'])
        identity = value['writer_identity']
        if (not lock.is_absolute() or lock.name != row['session_id'] + '.lock'
                or not isinstance(identity, list) or len(identity) != 2
                or any(type(n) is not int or n < 0 for n in identity) or identity[1] == 0):
            raise ValueError('invalid standby original writer identity')
        row.update(writer_lock=str(lock), writer_identity=list(identity))
    return copy.deepcopy(row)


def fresh(observation, boot_id, now):
    at = observation.get('observed_monotonic')
    if (type(at) not in (int, float) or not math.isfinite(at)
            or type(now) not in (int, float) or not math.isfinite(now)
            or at < 0 or now < at
            or observation.get('boot_id') != boot_id):
        valid_clock = (type(at) in (int, float) and math.isfinite(at)
                       and type(now) in (int, float) and math.isfinite(now))
        age = now - at if valid_clock else None
        raise ValueError('stale or foreign standby observation: '
                         f'age_seconds={age!r}, max_age_seconds={MAX_OBSERVATION_AGE}, '
                         f'boot_matches={observation.get("boot_id") == boot_id}')
    if (observation.get('initialized') is not True or observation.get('idle') is not True
            or observation.get('composer_empty') is not True
            or observation.get('pending_approval') is not False
            or observation.get('task_count') != 0
            or type(observation.get('task_count')) is not int
            or observation.get('user_input_count') != 0
            or type(observation.get('user_input_count')) is not int
            or observation.get('model_request_count') != 0
            or type(observation.get('model_request_count')) is not int):
        raise ValueError('standby is not an untouched idle native')
    if now - at > MAX_OBSERVATION_AGE:
        raise ObservationExpired(f'standby observation expired: age_seconds={now - at!r}, '
                                 f'max_age_seconds={MAX_OBSERVATION_AGE}')


def write_once(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # An empty/partial file left by an error remains consumed.
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return raw


class StandbyLedger:
    def __init__(self, directory, *, clock=time.monotonic):
        if Path(directory).is_symlink():
            raise ValueError('standby directory cannot be a symlink')
        self.directory = Path(directory).resolve(strict=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('standby ledger must be private and owned')
        self._directory_identity = (info.st_dev, info.st_ino)
        self.clock = clock
        self._manifest_raw = (self.directory / 'cohort.json').read_bytes()
        self.manifest = json.loads(self._manifest_raw)
        if self.manifest.get('policy') != POLICY or self.manifest.get('count') != COUNT:
            raise ValueError('invalid standby policy or count')
        for key in ('cohort_id', 'workspace_id', 'boot_id'):
            identifier(self.manifest[key])
        generation(self.manifest['generation'])
        if self.manifest.get('mode') not in ('b', 'N', 'B'):
            raise ValueError('invalid standby mode')
        self._ready = None  # Never restored from a file or an earlier manager.
        self._invalid = False
        self._attempted = set()
        self._write_lock = threading.RLock()
        self._writes_drained = threading.Condition(self._write_lock)
        self._active_writes = 0
        self._originals_raw = None
        if os.path.lexists(self.directory / 'originals.json'):
            if (self.directory / 'originals.json').is_symlink():
                raise ValueError('standby original roster cannot be a symlink')
            self._originals_raw = (self.directory / 'originals.json').read_bytes()
            json.loads(self._originals_raw)

    @classmethod
    def create(cls, directory, *, cohort_id, workspace_id, boot_id, mode, prompt, config_generation, clock=time.monotonic):
        value = {'policy': POLICY, 'count': COUNT, 'cohort_id': identifier(cohort_id),
                 'workspace_id': controller_identifier(workspace_id), 'boot_id': identifier(boot_id),
                 'mode': mode, 'prompt': prompt, 'generation': generation(config_generation)}
        if mode not in ('b', 'N', 'B') or not isinstance(prompt, str) or not prompt or '\0' in prompt:
            raise ValueError('invalid standby mode')
        directory = Path(directory)
        directory.mkdir(mode=0o700, exist_ok=False)
        write_once(directory / 'cohort.json', value)
        return cls(directory, clock=clock)

    def _unchanged(self):
        current = self.directory.lstat()
        if ((current.st_dev, current.st_ino) != self._directory_identity
                or not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid()
                or stat.S_IMODE(current.st_mode) & 0o077
                or (self.directory / 'cohort.json').is_symlink()
                or (self.directory / 'cohort.json').read_bytes() != self._manifest_raw):
            raise ValueError('standby ledger identity changed')
        if self._originals_raw is not None and (
                (self.directory / 'originals.json').is_symlink()
                or (self.directory / 'originals.json').read_bytes() != self._originals_raw):
            raise ValueError('standby original roster changed')

    def _invalidate(self, reason):
        with self._write_lock:
            self._invalid = True
            self._ready = None
            # Close admission first. Already admitted socket writes precede
            # this revocation; drain them before returning to its caller.
            self._writes_drained.notify_all()
            self._writes_drained.wait_for(lambda: self._active_writes == 0)
        try:
            write_once(self.directory / 'invalidated.json', {'reason': str(reason)})
        except FileExistsError:
            pass

    def _consumed(self):
        return any(os.path.lexists(self.directory / name) for name in
                   ('activation-attempt.json', 'activation.json')) or any(self.directory.glob('input-*.json'))

    def observe_ready(self, observations, *, config_generation, boot_id, authorized):
        with core.FileLock(self.directory / 'ledger.lock', timeout_sec=5):
            if self._invalid or os.path.lexists(self.directory / 'invalidated.json'):
                raise ValueError('standby generation invalidated')
            if self._consumed():
                raise ValueError('standby activation already consumed')
            try:
                self._unchanged()
                if (generation(config_generation) != self.manifest['generation']
                        or identifier(boot_id) != self.manifest['boot_id'] or authorized is not True):
                    raise ValueError('standby authorization or configuration changed')
                if len(observations) != COUNT:
                    raise ValueError('standby cohort incomplete')
                rows, expired = [], []
                for observation in observations:
                    try:
                        fresh(observation, self.manifest['boot_id'], self.clock())
                    except ObservationExpired:
                        expired.append(observation['index'])
                    if observation.get('generation') != self.manifest['generation']:
                        raise ValueError('standby original configuration changed')
                    rows.append(original(observation, self.manifest['workspace_id']))
                if ({r['index'] for r in rows} != set(range(COUNT))
                        or len({r['pid'] for r in rows}) != COUNT
                        or any(len({identifier(r[k]) for r in rows}) != COUNT for k in
                               ('launch_id', 'surface_id', 'session_id'))):
                    raise ValueError('standby originals are not unique')
                rows.sort(key=lambda r: r['index'])
                if self._originals_raw is not None and json.loads(self._originals_raw) != rows:
                    raise ValueError('standby original identity changed')
                # Check every identity/config/idle premise before allowing a
                # caller to reacquire aged observations. Expiration must not
                # hide a different slot's permanent failure.
                if expired:
                    raise ObservationExpired('standby cohort observations expired', expired)
                if self._originals_raw is None:
                    self._originals_raw = write_once(self.directory / 'originals.json', rows)
                self._ready = {'originals': rows, 'observed_at': min(
                    observation['observed_monotonic'] for observation in observations)}
            except ObservationExpired:
                self._ready = None
                raise
            except Exception as exc:
                self._invalidate(exc)
                raise
        return copy.deepcopy(self._ready)

    def consume_activation(self, *, action_id, mode, prompt, config_generation, boot_id, authorized):
        with core.FileLock(self.directory / 'ledger.lock', timeout_sec=5):
            path = self.directory / 'activation.json'
            if self._consumed():
                # Returning False grants no delivery permission, even for the same action.
                return False
            if self._invalid or os.path.lexists(self.directory / 'invalidated.json'):
                raise ValueError('standby generation invalidated')
            if self._ready is None:
                raise ValueError('fresh readiness required after manager restart')
            try:
                self._unchanged()
                action_id = identifier(action_id)
                if (mode != self.manifest['mode'] or prompt != self.manifest.get('prompt')
                        or generation(config_generation) != self.manifest['generation']
                        or identifier(boot_id) != self.manifest['boot_id'] or authorized is not True):
                    raise ValueError('standby activation rejected')
                age = self.clock() - self._ready['observed_at']
                if not math.isfinite(age) or age < 0:
                    raise ValueError('standby activation rejected')
                if age > MAX_OBSERVATION_AGE:
                    raise ObservationExpired('standby activation observations expired')
                value = {**self.manifest, 'action_id': action_id, 'prompt': prompt,
                         'originals': self._ready['originals'], 'committed_monotonic': self.clock()}
                write_once(self.directory / 'activation-attempt.json', {
                    'action_id': action_id, 'activation_sha256': digest(value)})
                write_once(path, value)
                self._activation_raw = path.read_bytes()
                self._activation = copy.deepcopy(value)
                self._ready = None
                return True
            except ObservationExpired:
                # Nothing has been consumed or written. Only a complete new
                # observation may earn readiness again; never renew its time.
                self._ready = None
                raise
            except Exception as exc:
                self._invalidate(exc)
                raise

    def deliver(self, index, *, action_id, observe, authorized, send):
        """Only this live activation owner can attempt one original input.

        Adapters supply fresh scoped identity/permission checks. The transport
        is called once only after durable consumption and a second live check.
        Exceptions, false ACKs and process crashes never make a slot retryable.
        send must use its write_guard around the transport write only, not the
        ACK wait. Each slot earns a separate write permit; cohort invalidation
        closes admission and drains all outstanding writes before returning.
        """
        if not hasattr(self, '_activation'):
            raise ValueError('activation is observation-only in this manager')
        if type(index) is not int or not 0 <= index < COUNT:
            raise ValueError('invalid standby slot')
        activation = self._activation
        if identifier(action_id) != activation['action_id']:
            raise ValueError('activation action mismatch')
        expected = activation['originals'][index]

        def evidence_current():
            self._unchanged()
            if self._invalid or os.path.lexists(self.directory / 'invalidated.json'):
                raise ValueError('standby activation invalidated')
            if ((self.directory / 'activation.json').is_symlink()
                    or (self.directory / 'activation.json').read_bytes() != self._activation_raw):
                raise ValueError('activation evidence changed')

        def check():
            evidence_current()
            # Authorization may perform blocking connected reads or invoke
            # the caller's input guard. Observe the original after it returns,
            # so an identity/queue change during authorization is not hidden.
            if authorized(index) is not True:
                raise ValueError('original standby authorization changed')
            current = observe(index)
            fresh(current, activation['boot_id'], self.clock())
            if (original(current, activation['workspace_id']) != expected
                    or current.get('generation') != activation['generation']):
                raise ValueError('original standby identity or authorization changed')
            evidence_current()  # Callbacks may block while another slot invalidates the cohort.

        claim = self.directory / f'input-{index}.json'
        # Activation is already durably committed. Only competing claims for
        # this slot need exclusion; a slow authorization for another original
        # must not exhaust this slot's lock wait. Cohort invalidation is still
        # checked after callbacks and ordered against the final guarded write.
        with core.FileLock(self.directory / f'input-{index}.lock', timeout_sec=5):
            if index in self._attempted or os.path.lexists(claim):
                return False
            try:
                check()
            except Exception as exc:
                self._invalidate(exc)
                raise
            self._attempted.add(index)
            payload = write_once(claim, {'action_id': activation['action_id'], 'original': expected,
                                        'activation_sha256': hashlib.sha256(self._activation_raw).hexdigest(),
                                        'input_id': str(uuid.uuid4()), 'prompt': activation['prompt']})
        try:
            check()
            if claim.is_symlink() or claim.read_bytes() != payload:
                raise ValueError('standby input claim changed')
        except Exception as exc:
            self._invalidate(exc)
            raise
        used = False

        @contextlib.contextmanager
        def write_guard():
            nonlocal used
            acquired = False
            try:
                while not acquired:
                    # Connected reads for independent originals may run in
                    # parallel. Never carry their result across a lock wait:
                    # a contended admission discards it and checks again.
                    check()
                    acquired = self._write_lock.acquire(blocking=False)
                    if not acquired:
                        with self._write_lock:
                            pass
                # check() just re-read all immutable evidence, after callbacks;
                # successful nonblocking admission introduced no lock wait.
                # Recheck the state that this lock protects before bytes. A
                # second full roster read here would serialize independent
                # slots again. External files were never protected by this
                # process-local lock; every contended admission still repeats
                # their full validation above.
                if self._invalid or os.path.lexists(self.directory / 'invalidated.json'):
                    raise ValueError('standby activation invalidated')
                if used or claim.is_symlink() or claim.read_bytes() != payload:
                    raise ValueError('standby transport permit consumed or changed')
                used = True
                self._active_writes += 1
                # Keep the cohort lock for admission only. A blocked sendall
                # must not discard another slot's complete connected checks
                # or consume that slot's control deadline with repeated reads.
                self._write_lock.release()
                acquired = False
                try:
                    yield
                except BaseException:
                    # Close new admission before dropping our permit. Do not
                    # wait here: two failing writes must both be able to leave.
                    with self._writes_drained:
                        self._invalid = True
                        self._ready = None
                    raise
                finally:
                    with self._writes_drained:
                        self._active_writes -= 1
                        self._writes_drained.notify_all()
            except BaseException as exc:
                self._invalidate(exc)
                raise
            finally:
                if acquired:
                    self._write_lock.release()

        send(copy.deepcopy(expected), activation['prompt'], json.loads(payload)['input_id'],
             write_guard=write_guard)
        if not used:
            self._invalidate('transport did not enter its guarded write boundary')
            raise ValueError('standby transport write was not guarded')
        return True
