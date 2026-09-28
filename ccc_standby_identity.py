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
    before = batch._file_generation(path)
    data, sent = batch._initial_event_prefix(claim)
    if batch._file_generation(path) != before or len(data) != before[2]:
        raise ValueError('standby TUI observation incomplete or changing')
    return data, sent


def inspect_original(claim_path, *, claim_sha256, expected, expected_argv,
                     sessions_root, process_reader=None, files_reader=None):
    """Join exact no-prompt launch to its original open native UUID writer lock.

    The caller supplies the immutable job/slot/launch and generated argv.
    This does not establish profile freshness, completed skills refresh,
    composer state or zero outbound model requests; the adapter must prove
    those independently before it publishes readiness.
    """
    if process_reader is None:
        from ccc_guard_scope import process as process_reader
    if files_reader is None:
        from ccc_codex_queue import process_writable_files as files_reader
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
    # A generated no-prompt argv is necessary. The launcher must also reject
    # resume/fork and arbitrary positional input, not just this fixed prompt.
    if not expected_argv or batch.PROMPT in expected_argv or batch.LEGACY_PROMPT in expected_argv:
        raise ValueError('standby argv contains an initial task')
    pid = claim['bootstrap_pid']
    before = process_reader(pid, launch=True)
    if (not before or before.get('birth') != claim['bootstrap_birth']
            or before.get('argv') != expected_argv or before.get('remote')
            or identifier(before['surface_id']) != identifier(expected['surface_id'])
            or identifier(before['environment_workspace_id']) != identifier(expected['workspace_id'])
            or Path(before['cwd']).resolve() != Path(claim['cwd']).resolve()):
        raise ValueError('standby native process does not match original launch')
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
    if (any(p.suffix == '.jsonl' and p.is_relative_to(root) for p in files)
            or next(root.rglob(f'*{session}.jsonl'), None) is not None):
        raise ValueError('standby session already owns a rollout')
    tui = Path(claim['tui_log'])
    if (files.get(tui) != {'device': claim['tui_log_identity'][0], 'inode': claim['tui_log_identity'][1]}
            or _private_file(tui) != claim['tui_log_identity']):
        raise ValueError('standby native TUI writer changed')
    event_data, sent = _idle_prefix(claim)
    if sent:
        raise ValueError('standby native already submitted a user turn')
    events = [json.loads(line) for line in event_data.splitlines()]
    if sum(e.get('variant') == 'StartupThreadStarted' for e in events) != 1:
        raise ValueError('standby native startup not observed')
    current_files = files_reader(pid, identities=True)
    current_locks = {p for p in current_files if p.parent == native_home / 'thread-writer-locks'
                     and p.suffix == '.lock'}
    if (process_reader(pid, launch=True) != before or current_locks != {lock}
            or current_files.get(lock) != files[lock] or _private_file(lock) != lock_identity
            or current_files.get(tui) != files[tui] or _private_file(tui) != claim['tui_log_identity']
            or any(p.suffix == '.jsonl' and p.is_relative_to(root) for p in current_files)
            or _private_file(claim_path) != claim_identity or claim_path.read_bytes() != raw):
        raise ValueError('standby original changed during observation')
    after_data, after_sent = _idle_prefix(claim)
    if (after_sent or not after_data.startswith(event_data)
            or next(root.rglob(f'*{session}.jsonl'), None) is not None):
        raise ValueError('standby native received input during observation')
    return {'job_id': claim['job_id'], 'index': claim['index'], 'launch_id': claim['launch_id'],
            'surface_id': claim['surface_id'], 'workspace_id': claim['workspace_id'],
            'session_id': session, 'pid': pid, 'birth': list(before['birth']),
            'claim_sha256': claim_sha256, 'argv_sha256': hashlib.sha256(
                json.dumps(expected_argv, separators=(',', ':')).encode()).hexdigest(),
            'writer_lock': str(lock), 'writer_identity': lock_identity,
            'tui_prefix_bytes': len(after_data), 'tui_prefix_sha256': hashlib.sha256(after_data).hexdigest(),
            'startup_observed': True,
            'initial_skills_event_observed': any(e.get('variant') == 'SkillsListLoaded' for e in events),
            'readiness_proven': False,
            'scope': 'Original preactivation identity only; not skills/composer/zero-request readiness.'}
