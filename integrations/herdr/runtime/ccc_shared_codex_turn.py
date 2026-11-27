"""Bind a foreground Codex client to its connected native thread writer.

Startup resume argv and global configuration are never foreground evidence.
Discovery caches PID hints only; every use rechecks identities and connection.
"""
import ctypes
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
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


def _protected_root():
    return Path.home() / 'Library/Application Support/CodexSessionBackup/protected/sessions'


def _held_rollout(path, sid, root):
    return ((path.suffix == '.jsonl' and path.name.endswith('-' + sid + '.jsonl')
             and path.is_relative_to(root))
            or (path.is_relative_to(_protected_root())
                and ('-' + sid + '.jsonl') in path.name))


def _protected_metadata(path, sid, identity):
    """Authenticate only a native-held protected rollout, never a backup search.

    Pin the bounded metadata prefix and ancestor identities, not journal size or
    mtime: the native writer may legitimately append while the binding is live.
    """
    root = _protected_root()
    relative = path.relative_to(root).as_posix()
    pattern = (r'([0-9]{4})/([0-9]{2})/([0-9]{2})/rollout-\1-\2-\3T'
               r'[0-9]{2}-[0-9]{2}-[0-9]{2}-' + re.escape(sid)
               + r'\.jsonl\.' + str(identity['device']) + '-' + str(identity['inode']))
    if not re.fullmatch(pattern, relative):
        raise ValueError('invalid protected rollout name or vnode')

    def placement():
        if path.resolve(strict=True) != path or not _linked(path, identity):
            raise ValueError('protected rollout moved or linked')
        parents = []
        for parent in path.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('protected rollout ancestor is not a directory')
            parents.append((str(parent), info.st_dev, info.st_ino))
        return parents

    parents = placement()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != identity['device']
                or info.st_ino != identity['inode']):
            raise ValueError('protected rollout descriptor changed')
        prefix = os.pread(stream.fileno(), 65536, 0)
        newline = prefix.find(b'\n')
        if newline < 0:
            raise ValueError('protected rollout metadata missing or oversized')
        raw = prefix[:newline + 1]
        meta = json.loads(raw)
        if (not isinstance(meta, dict) or meta.get('type') != 'session_meta'
                or not isinstance(meta.get('payload'), dict)
                or meta['payload'].get('id') != sid):
            raise ValueError('protected rollout session metadata mismatch')
        # Bypass buffered reads: a same-inode rewrite must not be hidden by a
        # seek into the first read's cache. Appending after this prefix is fine.
        if os.pread(stream.fileno(), len(raw), 0) != raw:
            raise ValueError('protected rollout metadata changed during read')
    if placement() != parents:
        raise ValueError('protected rollout ancestors changed')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'parents': parents}


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


def writer_files(pid):
    """Retry a changing complete inventory, never combine partial evidence.

    A shared app-server opens and moves other sessions' files concurrently.
    A rejected snapshot conveys no ownership; each retry starts both native
    passes again. Short reads and persistent drift still fail closed, and
    callers retain all process, connection, selected lock and rollout checks.
    """
    for attempt in range(3):
        try:
            return native.process_writable_files(pid, identities=True)
        except native.VnodeInventoryChanged:
            if attempt == 2:
                raise


def foreground_selection(pid, target):
    """Preserve private observer authority; only absence admits enrolled official TUI."""
    original = read_foreground(pid, request_observation_directory_matches)
    if original[0] != 'absent':
        return original
    from ccc_tui_native_turn import selection
    current = selection(target, pid)
    if current[0] == 'invalid':
        return 'native_tui_invalid', None, None
    return current


class _VnodeInfoPath(ctypes.Structure):
    _fields_ = [('vip_vi', ctypes.c_byte * 152), ('vip_path', ctypes.c_char * 1024)]


class _ProcVnodePathInfo(ctypes.Structure):
    _fields_ = [('pvi_cdir', _VnodeInfoPath), ('pvi_rdir', _VnodeInfoPath)]


def _pid_cwd(pid):
    if native._proc_pidinfo is None:
        return None
    buf = _ProcVnodePathInfo()
    res = native._proc_pidinfo(pid, 9, 0, ctypes.byref(buf), ctypes.sizeof(buf))
    if res != ctypes.sizeof(buf):
        return None
    text = buf.pvi_cdir.vip_path.decode('utf-8', errors='replace')
    return str(Path(text).resolve()) if text else None


def _extract_argv_resume(argv):
    options = argv[1:argv.index('--')] if '--' in argv else argv[1:]
    if 'resume' in options:
        idx = options.index('resume')
        if idx + 1 < len(options):
            try:
                import uuid
                return str(uuid.UUID(options[idx + 1]))
            except (ValueError, TypeError):
                return None
    return None


def _unattended_appserver_sid(pid, process, root):
    """Resolve the unique primary user thread on a connected 0.170 app-server."""
    import uuid
    argv, _ = scope.arguments(pid)
    resume_sid = _extract_argv_resume(argv)
    cwd = _pid_cwd(pid)
    born = process['birth'][0] + process['birth'][1] / 1e6
    all_codex_clients = None
    candidates = []
    for writer in writer_candidates():
        w_birth = scope.birth(writer, codex=True)
        if not w_birth or not connected_writer(pid, writer, process['birth'], w_birth):
            continue
        files = writer_files(writer)
        sibling_resumes = set()
        later_sibling_births = []
        if resume_sid is None:
            if all_codex_clients is None:
                try:
                    all_codex_clients = scope.scan()
                except Exception:
                    all_codex_clients = []
            for sib in all_codex_clients:
                spid = sib['pid']
                if spid in {pid, writer}:
                    continue
                if not connected_writer(spid, writer, sib['birth'], w_birth):
                    continue
                try:
                    sargv, _ = scope.arguments(spid)
                except Exception:
                    continue
                sres = _extract_argv_resume(sargv)
                if sres:
                    sibling_resumes.add(sres)
                elif _pid_cwd(spid) == cwd:
                    sborn = sib['birth'][0] + sib['birth'][1] / 1e6
                    if sborn > born:
                        later_sibling_births.append(sborn)
        next_sibling_born = min(later_sibling_births) if later_sibling_births else None
        for lock in (p for p in files if p.parent.name == 'thread-writer-locks'
                     and p.suffix == '.lock' and (p.parent.parent / 'sessions').resolve() == root):
            sid = lock.stem
            if resume_sid is not None:
                if sid == resume_sid:
                    candidates.append(sid)
                continue
            if not cwd or sid in sibling_resumes:
                continue
            try:
                u = uuid.UUID(sid)
                minted = (u.int >> 80) / 1000.0 if u.version == 7 else None
            except ValueError:
                continue
            if minted is not None:
                if minted < born - 1.0:
                    continue
                if next_sibling_born is not None and minted >= next_sibling_born - 0.5:
                    continue
            rollouts = [p for p in files if _held_rollout(p, sid, root)]
            if len(rollouts) != 1:
                continue
            try:
                fd = os.open(rollouts[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb', buffering=0) as stream:
                    raw = os.pread(stream.fileno(), 65536, 0)
                nl = raw.find(b'\n')
                if nl < 0:
                    continue
                meta = json.loads(raw[:nl + 1]).get('payload', {})
                if (meta.get('id') != sid
                        or meta.get('thread_source') not in (None, 'user')
                        or meta.get('parent_thread_id')
                        or not isinstance(meta.get('cwd'), str)
                        or str(Path(meta['cwd']).resolve()) != cwd):
                    continue
                snap = native.task_snapshot(rollouts[0], sid)
                if snap and isinstance(snap.get('at'), (int, float)) and snap['at'] >= born - 1:
                    candidates.append(sid)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def binding(target, pid, sessions_root):
    """Return binding plus diagnostic; absence never authorizes input."""
    try:
        process = scope.process(pid)
        if (not process or process['surface_id'] != target['surface_id']
                or process['environment_workspace_id'] != target['workspace_id']
                or process.get('remote')):
            return None, 'native client identity unavailable'
        selection = foreground_selection(pid, target)
        status, sid, _ = selection
        root = Path(sessions_root).resolve()
        if status == 'absent' and not sid:
            sid = _unattended_appserver_sid(pid, process, root)
        if status not in {'ok', 'absent'} or not sid:
            return None, ('native foreground observation missing; client upgrade required'
                          if status == 'absent' else 'native foreground observation invalid or cleared')
        matches = []
        for writer in dict.fromkeys((pid, *writer_candidates())):
            born = scope.birth(writer, codex=True)
            if not born or not connected_writer(pid, writer, process['birth'], born):
                continue
            files = writer_files(writer)
            locks = [p for p in files if p.parent.name == 'thread-writer-locks'
                     and p.name == sid + '.lock'
                     and (p.parent.parent / 'sessions').resolve() == root]
            if len(locks) != 1 or not _linked(locks[0], files[locks[0]]):
                continue
            paths = [p for p in files if _held_rollout(p, sid, root)]
            held = {locks[0]: files[locks[0]]}
            database = None
            protected = None
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
            if paths[0].is_relative_to(_protected_root()):
                protected = _protected_metadata(paths[0], sid, identity)
            matches.append({'pid': pid, 'process': process, 'writer_pid': writer,
                            'writer_birth': born, 'selection': selection, 'session_id': sid,
                            'sessions_root': str(root), 'paths': held,
                            'rollout_identity': identity, 'rollout_database': database,
                            'protected_metadata': protected,
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
                or foreground_selection(pid, target) != proof['selection']
                or (proof['selection'][0] == 'absent'
                    and _unattended_appserver_sid(pid, process, Path(proof['sessions_root'])) != proof['session_id'])
                or not connected_writer(pid, writer, process['birth'], proof['writer_birth'])):
            return False
        files = writer_files(writer)
        if any(files.get(p) != identity or not _linked(p, identity)
               for p, identity in proof['paths'].items()):
            return False
        if not _linked(proof['path'], proof['rollout_identity']):
            return False
        if proof.get('protected_metadata') is not None:
            if ({p for p in files if _held_rollout(p, proof['session_id'],
                                                Path(proof['sessions_root']))} != {proof['path']}
                    or _protected_metadata(proof['path'], proof['session_id'],
                                           proof['rollout_identity']) != proof['protected_metadata']):
                return False
            after = writer_files(writer)
            if ({p for p in after if _held_rollout(p, proof['session_id'],
                                                 Path(proof['sessions_root']))} != {proof['path']}
                    or any(after.get(p) != identity or not _linked(p, identity)
                           for p, identity in proof['paths'].items())):
                return False
        if (proof['rollout_database'] is not None
                and _rollout_row(proof['rollout_database'], proof['session_id'],
                                 Path(proof['sessions_root'])) != proof['path']):
            return False
        return (scope.process(pid) == process
                and foreground_selection(pid, target) == proof['selection']
                and connected_writer(pid, writer, process['birth'], proof['writer_birth']))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error):
        return False


def current_turn(target, pid, sessions_root):
    proof, reason = binding(target, pid, sessions_root)
    if proof is None:
        return {'kind': 'unknown', 'reason': reason}
    try:
        from ccc_tui_native_turn import lifecycle_matches
        snapshot = native.task_snapshot(proof['path'], proof['session_id'])
        process = proof['process']
        if (not snapshot or snapshot['at'] < process['birth'][0] + process['birth'][1] / 1e6
                or not lifecycle_matches(proof['selection'], snapshot, proof['path'])
                or not valid(target, proof)
                or native.task_snapshot(proof['path'], proof['session_id']) != snapshot):
            return {'kind': 'unknown', 'reason': 'native foreground lifecycle missing or changed'}
        return {**snapshot, 'pid': pid, 'process_start': process['process_start'],
                'birth': process['birth'], 'session_id': proof['session_id'],
                'transcript_path': str(proof['path']),
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
    from ccc_tui_native_turn import lifecycle_matches
    snapshot = native.task_snapshot(proof['path'], proof['session_id'])
    if (not snapshot or any(snapshot.get(k) != turn.get(k)
                           for k in ('kind', 'turn_id', 'at', 'error', 'signature'))
            or not lifecycle_matches(proof['selection'], snapshot, proof['path'])
            or not valid(target, proof)
            or native.task_snapshot(proof['path'], proof['session_id']) != snapshot):
        return None
    return proof
