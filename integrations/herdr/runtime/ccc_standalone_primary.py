"""Authenticate a legacy standalone primary among its native child writers.

No selection is synthesized. A live native selection always wins. The only
optional-record exception is a fully authenticated prior PID generation.
"""
from collections import OrderedDict
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid


_THREAD_ROW_CACHE = OrderedDict()
_THREAD_ROW_LOCK = threading.Lock()
_ROLLOUT_LOCATOR_CACHE = OrderedDict()


def _locate_rollout_by_sid(root, sid):
    key = (str(root), sid)
    with _THREAD_ROW_LOCK:
        cached = _ROLLOUT_LOCATOR_CACHE.get(key)
        if cached is not None and cached.is_file():
            _ROLLOUT_LOCATOR_CACHE.move_to_end(key)
            return [cached]
    paths = list(root.rglob('*-' + sid + '.jsonl'))
    if len(paths) == 1:
        with _THREAD_ROW_LOCK:
            _ROLLOUT_LOCATOR_CACHE[key] = paths[0]
            _ROLLOUT_LOCATOR_CACHE.move_to_end(key)
            while len(_ROLLOUT_LOCATOR_CACHE) > 512:
                _ROLLOUT_LOCATOR_CACHE.popitem(last=False)
    return paths


def _cached_thread_rows(database, db_identity, sid, get_db):
    key = (db_identity['device'], db_identity['inode'], sid)
    with _THREAD_ROW_LOCK:
        cached = _THREAD_ROW_CACHE.get(key)
        if cached is not None:
            _THREAD_ROW_CACHE.move_to_end(key)
            return cached
    rows = get_db().execute(
        'SELECT substr(rollout_path,1,4097),substr(source,1,65537) FROM threads WHERE id=? LIMIT 2',
        (sid,),
    ).fetchall()
    if (len(rows) == 1 and all(isinstance(v, str) for v in rows[0])
            and len(rows[0][0]) <= 4096 and len(rows[0][1]) <= 65536):
        with _THREAD_ROW_LOCK:
            _THREAD_ROW_CACHE[key] = rows
            _THREAD_ROW_CACHE.move_to_end(key)
            while len(_THREAD_ROW_CACHE) > 512:
                _THREAD_ROW_CACHE.popitem(last=False)
    return rows


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('noncanonical thread identity')
    return value


def _identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.resolve(strict=True) != path:
        raise ValueError('not a regular canonical native file')
    return {'device': info.st_dev, 'inode': info.st_ino}


def _metadata(path, sid, identity):
    if _identity(path) != identity:
        raise ValueError('native metadata vnode changed')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if {'device': info.st_dev, 'inode': info.st_ino} != identity:
            raise ValueError('native metadata descriptor changed')
        raw = os.pread(stream.fileno(), 65537, 0)
        end = raw.find(b'\n')
        if end < 0 or end >= 65536:
            raise ValueError('native metadata missing or oversized')
        raw = raw[:end + 1]
        item = json.loads(raw)
        data = item.get('payload') if isinstance(item, dict) else None
        if (not isinstance(item, dict) or item.get('type') != 'session_meta'
                or not isinstance(data, dict) or data.get('id') != sid):
            raise ValueError('native metadata identity mismatch')
        if os.pread(stream.fileno(), len(raw), 0) != raw or _identity(path) != identity:
            raise ValueError('native metadata changed')
    return data, hashlib.sha256(raw).hexdigest()


def primary_metadata(path, sid):
    """Legacy single-open-file paths must never promote a child to primary."""
    try:
        data, _ = _metadata(Path(path), sid, _identity(Path(path)))
        return (data.get('source') == 'cli'
                and data.get('thread_source') in (None, 'user')
                and not data.get('parent_thread_id'))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _admission(pid):
    from ccc_client_thread_observation import read_foreground, stale_optional_foreground
    from ccc_request_observation_policy import request_observation_directory_matches
    from ccc_guard_scope import arguments
    _, env = arguments(pid)
    if env.get('CODEX_CLIENT_THREAD_OBSERVER') not in (None, '1'):
        raise ValueError('native selection is required')
    selection = read_foreground(pid, request_observation_directory_matches)
    if selection[0] == 'absent':
        return {'kind': 'absent'}
    if selection[0] != 'invalid':
        raise ValueError('native selection takes priority')
    old = stale_optional_foreground(pid, request_observation_directory_matches)
    if old is None:
        raise ValueError('invalid foreground is not proven stale')
    # JSON-stable proof survives persistence without tuple/list drift.
    return {'kind': 'prior_generation', 'evidence': json.loads(json.dumps(old))}


def _inventory(target, pid, sessions_root):
    from ccc_guard_scope import process, arguments
    from ccc_native_processes import display_process
    from ccc_codex_goal import writer_files, _db_root, _root_pin
    client = process(pid)
    interactive = display_process(pid)
    argv, env = arguments(pid)
    options = argv[1:argv.index('--')] if '--' in argv else argv[1:]
    if (not client or not interactive or interactive.get('agent_kind') != 'codex'
            or client.get('remote') or client.get('backend')
            or client.get('surface_id') != target['surface_id']
            or client.get('environment_workspace_id') != target['workspace_id']
            or not argv or (len(argv) > 1 and argv[1] == 'app-server')
            or any(v == '--remote' or v.startswith('--remote=') for v in options)):
        raise ValueError('not the exact local standalone client')
    admission = _admission(pid)
    root = Path(sessions_root).resolve(strict=True)
    files = writer_files(pid)
    locks = {p: i for p, i in files.items()
             if p.parent.name == 'thread-writer-locks' and p.suffix == '.lock'}
    if not 1 <= len(locks) <= 64:
        raise ValueError('native lock inventory missing or too large')
    homes = {p.parent.parent for p in locks}
    if len(homes) != 1 or (next(iter(homes)) / 'sessions').resolve(strict=True) != root:
        raise ValueError('mixed native homes')
    first = next(iter(locks))
    database_root = _db_root(pid, first)
    database_pin = _root_pin(pid, database_root)
    databases = [p for p in files if p.name == 'state_5.sqlite' and p.parent == database_root]
    if len(databases) > 1:
        raise ValueError('multiple writers require their actual native database')
    database = databases[0] if databases else None
    disk_database = False
    if database is not None and _identity(database) != files[database]:
        raise ValueError('native database vnode changed')
    if database is None and len(locks) > 1 and (database_root / 'state_5.sqlite').is_file():
        database = database_root / 'state_5.sqlite'
        disk_database = True
    nodes = {}
    db_identity = (files[database] if database in files
                   else (_identity(database) if database is not None else None))
    db_handle = None
    try:
        def open_db():
            nonlocal db_handle
            if db_handle is None:
                db_handle = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.5)
            return db_handle

        for lock, held in sorted(locks.items()):
            sid = _uuid(lock.stem)
            if _identity(lock) != held:
                raise ValueError('native lock replaced')
            rows = None
            if database is not None:
                rows = _cached_thread_rows(database, db_identity, sid, open_db)
                if len(rows) == 1 and all(isinstance(v, str) for v in rows[0]) and len(rows[0][0]) <= 4096 and len(rows[0][1]) <= 65536:
                    path = Path(rows[0][0])
                elif disk_database:
                    rows = None
                else:
                    raise ValueError('native thread row unavailable')
            if rows is None:
                # Preserve the prior writer contract when its idle SQLite
                # connection is closed. Each real held UUID lock is the locator.
                paths = [p for p in files if p.suffix == '.jsonl' and p.is_relative_to(root) and p.name.endswith('-' + sid + '.jsonl')]
                if not paths:
                    paths = _locate_rollout_by_sid(root, sid)
                if len(paths) != 1:
                    raise ValueError('single writer rollout missing or ambiguous')
                path = paths[0]
            if (not path.is_absolute() or not path.is_relative_to(root)
                    or path.suffix != '.jsonl' or not path.name.endswith('-' + sid + '.jsonl')):
                raise ValueError('native rollout placement mismatch')
            identity = _identity(path)
            if files.get(path, identity) != identity:
                raise ValueError('held rollout replaced')
            data, prefix = _metadata(path, sid, identity)
            source = rows[0][1] if rows is not None else data.get('source')
            if rows is not None:
                source = source if source == 'cli' else json.loads(source)
            if data.get('source') != source:
                raise ValueError('native header and database source disagree')
            nodes[sid] = {'path': str(path), 'identity': identity, 'lock': str(lock),
                          'lock_identity': held, 'source': source, 'header_sha256': prefix,
                          'parent': data.get('parent_thread_id'),
                          'thread_source': data.get('thread_source'),
                          'session_id': data.get('session_id')}
    finally:
        if db_handle is not None:
            db_handle.close()
    roots = [sid for sid, n in nodes.items() if n['source'] == 'cli'
             and n['thread_source'] in (None, 'user') and not n['parent']]
    if len(roots) != 1:
        raise ValueError('primary CLI root is missing or ambiguous')
    sid = roots[0]
    for child, node in nodes.items():
        if child == sid:
            continue
        if node['thread_source'] != 'subagent' or node['session_id'] != sid:
            raise ValueError('native child source is inconsistent')
        seen, cursor, distance = set(), child, 0
        while cursor != sid:
            if cursor in seen or distance > 16:
                raise ValueError('native child ancestry is incomplete or cyclic')
            seen.add(cursor)
            if cursor in nodes:
                n = nodes[cursor]
            else:
                anc_paths = None
                if database is not None:
                    anc_db = None
                    try:
                        def open_anc_db():
                            nonlocal anc_db
                            if anc_db is None:
                                anc_db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.5)
                            return anc_db
                        anc_rows = _cached_thread_rows(database, db_identity, cursor, open_anc_db)
                    finally:
                        if anc_db is not None:
                            anc_db.close()
                    if len(anc_rows) == 1 and isinstance(anc_rows[0][0], str) and Path(anc_rows[0][0]).is_file():
                        anc_paths = [Path(anc_rows[0][0])]
                if not anc_paths:
                    anc_paths = _locate_rollout_by_sid(root, cursor)
                if len(anc_paths) != 1:
                    raise ValueError('native child ancestry is incomplete or cyclic')
                anc_data, _ = _metadata(anc_paths[0], cursor, _identity(anc_paths[0]))
                n = {'source': anc_data.get('source'), 'parent': anc_data.get('parent_thread_id'),
                     'thread_source': anc_data.get('thread_source'), 'session_id': anc_data.get('session_id')}
            spawn = n['source']['subagent']['thread_spawn']
            parent = _uuid(spawn['parent_thread_id'])
            if (n['thread_source'] != 'subagent' or n['parent'] != parent
                    or type(spawn.get('depth')) is not int or spawn['depth'] <= 0
                    or n['session_id'] != sid):
                raise ValueError('native child source is inconsistent')
            cursor, distance = parent, distance + 1
        if node['source']['subagent']['thread_spawn']['depth'] != distance:
            raise ValueError('native child depth disagrees with ancestry')
    # Resume argv is only a veto against the independently proved CLI root.
    # It is never a source of identity and --last/--all have no selecting role.
    if 'resume' in options:
        following = options[options.index('resume') + 1:]
        ids = []
        for value in following:
            try: ids.append(_uuid(value))
            except (ValueError, TypeError): pass
        if ids and ids != [sid]:
            raise ValueError('startup locator conflicts with native primary')
    rollouts = {p for p in files if p.suffix == '.jsonl' and p.is_relative_to(root)}
    if rollouts - {Path(n['path']) for n in nodes.values()}:
        raise ValueError('unaccounted native rollout writer')
    if (process(pid) != client or display_process(pid) != interactive
            or arguments(pid) != (argv, env) or _admission(pid) != admission
            or _root_pin(pid, database_root) != database_pin):
        raise ValueError('native process or selection changed')
    return {'version': 1, 'pid': pid, 'process': client, 'argv': argv,
            'sessions_root': str(root), 'admission': admission,
            'database': str(database) if database is not None else None,
            'database_identity': files[database] if database in files else (_identity(database) if database is not None else None),
            'database_root_identity': list(database_pin[2]), 'session_id': sid, 'nodes': nodes}


def current_turn(target, pid, sessions_root):
    from ccc_codex_queue import task_snapshot
    try:
        proof = _inventory(target, pid, sessions_root)
        sid = proof['session_id']
        path = Path(proof['nodes'][sid]['path'])
        snapshot = task_snapshot(path, sid)
        client = proof['process']
        if (not snapshot or snapshot.get('kind') == 'unknown'
                or snapshot['at'] < client['birth'][0] + client['birth'][1] / 1e6
                or _inventory(target, pid, sessions_root) != proof
                or task_snapshot(path, sid) != snapshot):
            raise ValueError('native primary lifecycle changed or predates process')
        return {**snapshot, 'pid': pid, 'process_start': client['process_start'],
                'birth': client['birth'], 'session_id': sid, 'transcript_path': str(path),
                'standalone_primary': proof}
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, sqlite3.Error):
        return {'kind': 'unknown', 'reason': 'standalone primary identity unavailable or changed'}


def valid_turn(target, turn):
    """Reauthenticate at the Goal/provider/input boundary; never trust a hint."""
    if not turn.get('standalone_primary'):
        return True
    proof = turn['standalone_primary']
    fresh = current_turn(target, turn['pid'], proof['sessions_root'])
    return (fresh.get('kind') != 'unknown' and all(fresh.get(k) == turn.get(k) for k in
            ('kind', 'turn_id', 'at', 'error', 'signature', 'terminal_tail', 'pid',
             'process_start', 'birth', 'session_id', 'transcript_path', 'standalone_primary')))
