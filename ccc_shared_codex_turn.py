"""Bind a foreground Codex client to its connected native thread writer.

Startup resume argv and global configuration are never foreground evidence.
Discovery caches PID hints only; every use rechecks identities and connection.
"""
import ctypes
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import threading
import time

import ccc_codex_queue as native
import ccc_guard_scope as scope
from ccc_client_thread_observation import read_foreground
from ccc_request_key_binding import connected_writer
from ccc_request_observation_policy import request_observation_directory_matches

_lock = threading.Lock()
_discovery = (0, ())


def writer_candidates():
    global _discovery
    with _lock:
        now = time.monotonic()
        if now - _discovery[0] < 5:
            return _discovery[1]
        if native._proc_listpids is None:
            return ()
        needed = native._proc_listpids(1, 0, None, 0)
        if not 0 < needed < 4 * 1024 * 1024:
            raise OSError('native writer inventory unavailable')
        values = (ctypes.c_int * ((needed + 16384) // 4))()
        count = native._proc_listpids(1, 0, values, ctypes.sizeof(values))
        if not 0 < count < ctypes.sizeof(values) or count % 4:
            raise OSError('native writer inventory incomplete')
        candidates = []
        for pid in native.codex_process_starts(values[:count // 4]):
            try:
                argv, _ = scope.arguments(pid)
                if 'app-server' in argv:
                    candidates.append(pid)
            except (OSError, ValueError, RuntimeError):
                continue
        _discovery = (now, tuple(candidates))
        return _discovery[1]


def _linked(path, identity):
    info = path.lstat()
    return (stat.S_ISREG(info.st_mode) and info.st_dev == identity['device']
            and info.st_ino == identity['inode'])


def _rollout_row(database, sid, root):
    # A closed rollout is located only through the selected writer's own DB.
    # Bound the value as well as the row count; never search all session files.
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
        rows = db.execute('SELECT substr(rollout_path,1,4097) FROM threads WHERE id=? LIMIT 2',
                          (sid,)).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or len(rows[0][0]) > 4096:
        raise ValueError('ambiguous rollout mapping')
    path = Path(rows[0][0])
    if (not path.is_absolute() or not path.is_relative_to(root)
            or path.resolve() != path or path.suffix != '.jsonl'
            or not path.name.endswith('-' + sid + '.jsonl')):
        raise ValueError('invalid rollout mapping')
    return path


def binding(target, pid, sessions_root):
    """Return binding plus diagnostic; absence never authorizes input."""
    try:
        process = scope.process(pid)
        if (not process or process['surface_id'] != target['surface_id']
                or process['environment_workspace_id'] != target['workspace_id']
                or process.get('remote')):
            return None, 'native client identity unavailable'
        selection = read_foreground(pid, request_observation_directory_matches)
        status, sid, _ = selection
        if status != 'ok' or not sid:
            return None, ('native foreground observation missing; client upgrade required'
                          if status == 'absent' else 'native foreground observation invalid or cleared')
        root = Path(sessions_root).resolve()
        matches = []
        for writer in dict.fromkeys((pid, *writer_candidates())):
            born = scope.birth(writer, codex=True)
            if not born or not connected_writer(pid, writer, process['birth'], born):
                continue
            files = native.process_writable_files(writer, identities=True)
            locks = [p for p in files if p.parent.name == 'thread-writer-locks'
                     and p.name == sid + '.lock'
                     and (p.parent.parent / 'sessions').resolve() == root]
            if len(locks) != 1 or not _linked(locks[0], files[locks[0]]):
                continue
            paths = [p for p in files if p.suffix == '.jsonl'
                     and p.name.endswith('-' + sid + '.jsonl') and p.is_relative_to(root)]
            held = {locks[0]: files[locks[0]]}
            database = None
            if not paths:
                databases = [p for p in files if p.name == 'state_5.sqlite'
                             and p.parent == locks[0].parent.parent]
                if len(databases) != 1 or not _linked(databases[0], files[databases[0]]):
                    continue
                database = databases[0]
                paths = [_rollout_row(database, sid, root)]
                info = paths[0].lstat()
                identity = {'device': info.st_dev, 'inode': info.st_ino}
                held[database] = files[database]
            elif len(paths) == 1:
                identity = files[paths[0]]
                held[paths[0]] = identity
            if len(paths) != 1 or not _linked(paths[0], identity):
                continue
            matches.append({'pid': pid, 'process': process, 'writer_pid': writer,
                            'writer_birth': born, 'selection': selection, 'session_id': sid,
                            'sessions_root': str(root), 'paths': held,
                            'rollout_identity': identity, 'rollout_database': database,
                            'path': paths[0]})
        if len(matches) != 1 or not valid(target, matches[0]):
            return None, 'native foreground writer missing, ambiguous or changed'
        return matches[0], ''
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error):
        return None, 'native foreground writer verification failed'


def valid(target, proof):
    try:
        pid, writer = proof['pid'], proof['writer_pid']
        process = scope.process(pid)
        if (process != proof['process'] or not process
                or process['surface_id'] != target['surface_id']
                or process['environment_workspace_id'] != target['workspace_id']
                or read_foreground(pid, request_observation_directory_matches) != proof['selection']
                or not connected_writer(pid, writer, process['birth'], proof['writer_birth'])):
            return False
        files = native.process_writable_files(writer, identities=True)
        if any(files.get(p) != identity or not _linked(p, identity)
               for p, identity in proof['paths'].items()):
            return False
        if not _linked(proof['path'], proof['rollout_identity']):
            return False
        if (proof['rollout_database'] is not None
                and _rollout_row(proof['rollout_database'], proof['session_id'],
                                 Path(proof['sessions_root'])) != proof['path']):
            return False
        return (scope.process(pid) == process
                and read_foreground(pid, request_observation_directory_matches) == proof['selection']
                and connected_writer(pid, writer, process['birth'], proof['writer_birth']))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error):
        return False


def current_turn(target, pid, sessions_root):
    proof, reason = binding(target, pid, sessions_root)
    if proof is None:
        return {'kind': 'unknown', 'reason': reason}
    try:
        snapshot = native.task_snapshot(proof['path'], proof['session_id'])
        process = proof['process']
        if (not snapshot or snapshot['at'] < process['birth'][0] + process['birth'][1] / 1e6
                or not valid(target, proof)
                or native.task_snapshot(proof['path'], proof['session_id']) != snapshot):
            return {'kind': 'unknown', 'reason': 'native foreground lifecycle missing or changed'}
        return {**snapshot, 'pid': pid, 'process_start': process['process_start'],
                'birth': process['birth'], 'session_id': proof['session_id'],
                'shared_writer': {'pid': proof['writer_pid'], 'birth': proof['writer_birth'],
                                  'sessions_root': proof['sessions_root']}}
    except (OSError, ValueError, KeyError, TypeError):
        return {'kind': 'unknown', 'reason': 'native foreground lifecycle read failed'}


def binding_for_turn(target, turn):
    """Re-establish the same binding for provider/goal reads, never trust a hint."""
    hint = turn.get('shared_writer')
    if not isinstance(hint, dict):
        return None
    proof, _ = binding(target, turn['pid'], hint['sessions_root'])
    if (not proof or proof['writer_pid'] != hint['pid'] or proof['writer_birth'] != hint['birth']
            or proof['session_id'] != turn['session_id']
            or proof['process']['process_start'] != turn['process_start']):
        return None
    snapshot = native.task_snapshot(proof['path'], proof['session_id'])
    if (not snapshot or any(snapshot.get(k) != turn.get(k)
                           for k in ('kind', 'turn_id', 'at', 'error', 'signature'))
            or not valid(target, proof)
            or native.task_snapshot(proof['path'], proof['session_id']) != snapshot):
        return None
    return proof
