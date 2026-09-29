"""Read-only preactivation native identity; never manufactures a Hook or ready state."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import ccc_workspace_batch as batch
from ccc_native_standby import POLICY, identifier
from ccc_standby_environment import signature


class StartupPending(ValueError):
    """The original writer is verified but startup has not yet been observed.

    observation is identity evidence only. Callers must not publish readiness
    or send input on the strength of this exception.
    """
    def __init__(self, observation):
        super().__init__('standby native startup not yet observed')
        self.observation = observation


def _private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or path.is_symlink()):
        raise ValueError('standby evidence is not an owned regular file')
    return [info.st_dev, info.st_ino]


def _idle_prefix(claim):
    # The argv-first-task parser intentionally tolerates a concurrent partial
    # record. Negative standby evidence cannot discard a possible UserTurn.
    path = Path(claim['tui_log'])
    previous = b''
    for _ in range(3):
        before = batch._file_generation(path)
        data, sent = batch._initial_event_prefix(claim)
        after = batch._file_generation(path)
        if sent:
            raise ValueError('standby native already submitted a user turn')
        if (before[:2] != after[:2] or after[2] < before[2]
                or len(data) < before[2] or not data.startswith(previous)):
            raise ValueError('standby TUI observation incomplete or changed')
        if after == before and len(data) == after[2]:
            return data, False
        # Only bounded append progress may retry. Parse the entire new prefix
        # again, so a turn/switch/reload cannot hide behind a benign append.
        if after[2] <= before[2]:
            raise ValueError('standby TUI prefix rewritten during observation')
        previous = data
    raise ValueError('standby TUI observation did not settle within read bound')


def _rollout_absent(root, session):
    for directory in (root, root.parent / 'archived_sessions'):
        for suffix in ('.jsonl', '.jsonl.zst'):
            if next(directory.rglob(f'*{session}{suffix}'), None) is not None:
                return False
    return True


def inspect_original(claim_path, *, claim_sha256, expected, expected_argv,
                     sessions_root, process_reader=None, files_reader=None,
                     rollout_absent=None, connected_check=None, final_check=None,
                     expected_environment_sha256=None):
    """Join exact no-prompt launch to its original open native UUID writer lock.

    The caller supplies the immutable job/slot/launch and generated argv.
    This does not establish profile freshness, completed skills refresh,
    composer state or zero outbound model requests; the adapter must prove
    those independently before it publishes readiness.

    connected_check, when supplied, runs between the two original process/FD
    observations. This lets an activation adapter inspect the actual screen
    and live permissions without repeating the entire identity inspection.
    Its return value cannot replace or certify the original evidence.
    """
    if process_reader is None:
        from ccc_guard_scope import process as process_reader
    if files_reader is None:
        from ccc_codex_queue import process_writable_files as files_reader
    if rollout_absent is None:
        rollout_absent = _rollout_absent
    if not callable(rollout_absent):
        raise ValueError('live rollout absence reader required')
    if connected_check is not None and not callable(connected_check):
        raise ValueError('connected inspection must be callable')
    if final_check is not None and not callable(final_check):
        raise ValueError('final inspection must be callable')
    claim_path = Path(claim_path)
    claim_identity = _private_file(claim_path)
    raw = claim_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != claim_sha256:
        raise ValueError('standby claim hash changed')
    claim = json.loads(raw)
    if (claim.get('policy') != POLICY or claim.get('state') != 'exec_intent'
            or claim.get('argv') != expected_argv
            or any(claim.get(k) != expected[k] for k in
                   ('job_id', 'index', 'launch_id', 'surface_id', 'workspace_id'))):
        raise ValueError('standby launch policy or original identity changed')
    if (expected_environment_sha256 is not None
            and (claim.get('target_environment_sha256') != expected_environment_sha256
                 or not isinstance(claim.get('environment_sha256'), str))):
        raise ValueError('standby claim target environment differs from preparation')
    # A generated no-prompt argv is necessary. The launcher must also reject
    # resume/fork and arbitrary positional input, not just this fixed prompt.
    if not expected_argv or batch.PROMPT in expected_argv or batch.LEGACY_PROMPT in expected_argv:
        raise ValueError('standby argv contains an initial task')
    pid = claim['bootstrap_pid']
    before = process_reader(pid, launch=True)
    mismatches = ['missing_process'] if not before else [name for name, changed in (
        ('birth', before.get('birth') != claim['bootstrap_birth']),
        ('argv', before.get('argv') != expected_argv),
        ('remote', bool(before.get('remote'))),
        ('environment', 'environment_sha256' in claim and
            signature(before.get('environment')) != claim['environment_sha256']),
        ('surface', identifier(before['surface_id']) != identifier(expected['surface_id'])),
        ('workspace', identifier(before['environment_workspace_id']) != identifier(expected['workspace_id'])),
        ('cwd', Path(before['cwd']).resolve() != Path(claim['cwd']).resolve())) if changed]
    if mismatches:
        error = ValueError('standby native process does not match original launch: ' + ','.join(mismatches))
        # Retain only identity fields, never the process environment or keys.
        error.process_observation = None if not before else {k: before.get(k) for k in
            ('pid', 'birth', 'argv', 'remote', 'surface_id', 'environment_workspace_id', 'cwd')}
        raise error
    files = files_reader(pid, identities=True)
    root = Path(sessions_root).resolve(strict=True)
    native_home = root.parent
    locks = [p for p in files if p.parent == native_home / 'thread-writer-locks' and p.suffix == '.lock']
    if len(locks) != 1:
        raise ValueError('standby native writer lock is ambiguous')
    lock = locks[0]
    session = str(uuid.UUID(lock.stem))
    if uuid.UUID(session).version != 7:
        raise ValueError('standby session is not a native UUID')
    lock_identity = _private_file(lock)
    if lock_identity != [files[lock]['device'], files[lock]['inode']]:
        raise ValueError('standby native writer inode changed')
    rollout_roots = (root, native_home / 'archived_sessions')
    def rollout_open(paths):
        return any((p.name.endswith('.jsonl') or p.name.endswith('.jsonl.zst'))
                   and any(p.is_relative_to(r) for r in rollout_roots) for p in paths)
    if rollout_open(files) or rollout_absent(root, session) is not True:
        raise ValueError('standby session already owns a rollout')
    tui = Path(claim['tui_log'])
    if (files.get(tui) != {'device': claim['tui_log_identity'][0], 'inode': claim['tui_log_identity'][1]}
            or _private_file(tui) != claim['tui_log_identity']):
        raise ValueError('standby native TUI writer changed')
    event_data, sent = _idle_prefix(claim)
    if sent:
        raise ValueError('standby native already submitted a user turn')
    if connected_check is not None:
        # Detached values prevent a screen/permission callback from changing
        # the baseline used by the final process, file and prefix checks.
        connected_check({'job_id': claim['job_id'], 'index': claim['index'],
            'launch_id': claim['launch_id'], 'surface_id': claim['surface_id'],
            'workspace_id': claim['workspace_id'], 'session_id': session,
            'pid': pid, 'birth': list(before['birth']), 'claim_sha256': claim_sha256,
            'argv_sha256': hashlib.sha256(json.dumps(expected_argv,
                separators=(',', ':')).encode()).hexdigest(),
            'writer_lock': str(lock), 'writer_identity': list(lock_identity),
            'tui_prefix_bytes': len(event_data),
            'tui_prefix_sha256': hashlib.sha256(event_data).hexdigest()})
    current_files = files_reader(pid, identities=True)
    current_locks = {p for p in current_files if p.parent == native_home / 'thread-writer-locks'
                     and p.suffix == '.lock'}
    # FD enumeration may block. Recheck live permission on return, while
    # retaining the process, path and input checks after that callback.
    if final_check is not None:
        final_check()
    if (process_reader(pid, launch=True) != before or current_locks != {lock}
            or current_files.get(lock) != files[lock] or _private_file(lock) != lock_identity
            or current_files.get(tui) != files[tui] or _private_file(tui) != claim['tui_log_identity']
            or rollout_open(current_files)
            or _private_file(claim_path) != claim_identity or claim_path.read_bytes() != raw):
        raise ValueError('standby original changed during observation')
    after_data, after_sent = _idle_prefix(claim)
    if (after_sent or not after_data.startswith(event_data)
            or rollout_absent(root, session) is not True):
        raise ValueError('standby native received input during observation')
    events = [json.loads(line) for line in after_data.splitlines()]
    starts = sum(e.get('variant') == 'StartupThreadStarted' for e in events)
    if starts > 1:
        raise ValueError('standby native startup changed')
    observation = {'job_id': claim['job_id'], 'index': claim['index'], 'launch_id': claim['launch_id'],
            'surface_id': claim['surface_id'], 'workspace_id': claim['workspace_id'],
            'session_id': session, 'pid': pid, 'birth': list(before['birth']),
            'claim_sha256': claim_sha256, 'argv_sha256': hashlib.sha256(
                json.dumps(expected_argv, separators=(',', ':')).encode()).hexdigest(),
            'writer_lock': str(lock), 'writer_identity': lock_identity,
            'tui_prefix_bytes': len(after_data), 'tui_prefix_sha256': hashlib.sha256(after_data).hexdigest(),
            'startup_observed': starts == 1,
            'initial_skills_event_observed': any(e.get('variant') == 'SkillsListLoaded' for e in events),
            'readiness_proven': False,
            'scope': 'Original preactivation identity only; not skills/composer/zero-request readiness.'}
    if not starts:
        raise StartupPending(observation)
    return observation
