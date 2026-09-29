#!/usr/bin/env python3
"""Preview/reclaim only inactive CCC logs databases. Never delete batch state.

Standalone so maintenance does not load or upgrade the running CCC daemon.
An explicit plan digest authorizes manual deletion; scheduled mode needs an
owner-only policy file. Both paths recheck live ownership under worker.lock.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

APP = Path.home() / 'Library/Application Support/cmux-codex-continue'
JANITOR = Path.home() / '.config/cmux-janitor'
LOG_NAME = re.compile(r'logs_\d+\.sqlite(?:-wal|-shm)?\Z')
TERMINAL = {'complete', 'cancelled', 'workspace_closed'}
SOURCE_AT_LOAD = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stamp(path, *, directory=False):
    s = path.lstat()
    valid = stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)
    if not valid or s.st_uid != os.getuid() or s.st_mode & 0o022:
        raise ValueError(f'unsafe file identity: {path}')
    if not directory and s.st_nlink != 1:
        raise ValueError(f'hardlinked file: {path}')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def safe_chain(path):
    for p in reversed([path, *path.parents]):
        if not stat.S_ISDIR(p.lstat().st_mode):
            raise ValueError(f'non-directory ancestor: {p}')
    stamp(path, directory=True)


def read_json(path):
    before = stamp(path)
    data = path.read_bytes()
    if stamp(path) != before:
        raise ValueError('file changed while reading')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    return value, digest(data)


@contextlib.contextmanager
def locked(path):
    # Existing CCC lock inode only: maintenance never manufactures a lock or
    # deletes/replaces one. A missing or held lock is not clearance.
    before = stamp(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if [s.st_dev, s.st_ino] != before[:2]:
            raise ValueError('lock replaced')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if stamp(path)[:2] != before[:2]:
            raise ValueError('lock path changed')
        yield
    finally:
        os.close(fd)


def run(argv):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if p.returncode != 0 or p.stderr.strip():
        raise RuntimeError(f'ownership query failed: {argv[0]} ({p.returncode})')
    return p.stdout


def topology(tree):
    workspaces, surfaces = set(), set()
    windows = tree.get('windows')
    if not isinstance(windows, list) or not windows:
        raise ValueError('incomplete cmux tree')
    for window in windows:
        for workspace in window['workspaces']:
            workspaces.add(str(uuid.UUID(workspace['id'])))
            for pane in workspace['panes']:
                for surface in pane['surfaces']:
                    surfaces.add(str(uuid.UUID(surface['id'])))
    return workspaces, surfaces


def observe(root):
    started = time.monotonic()
    tree = json.loads(run(['/opt/homebrew/bin/cmux', '--json', '--id-format',
                           'both', 'tree', '--all']))
    workspaces, surfaces = topology(tree)
    processes = {}
    for row in run(['/bin/ps', '-axo', 'pid=,command=']).splitlines():
        pid, command = row.strip().split(None, 1)
        processes[int(pid)] = command
    if not processes:
        raise ValueError('empty process table')
    opened = []
    owner = None
    for row in run(['/usr/sbin/lsof', '-nP', '-Fpn']).splitlines():
        if row.startswith('p'):
            owner = int(row[1:])
        if row.startswith('n') and (row[1:] == str(root) or row[1:].startswith(str(root) + '/')):
            if not (owner == os.getpid() and row.endswith('/worker.lock')):
                opened.append(row[1:])
    if time.monotonic() - started > 10:
        raise ValueError('ownership snapshot took too long')
    return dict(workspaces=workspaces, surfaces=surfaces, processes=processes,
                opened=opened, observed=time.monotonic())


def inactive(job, directory, world):
    if time.monotonic() - world['observed'] > 10:
        raise ValueError('stale ownership observation')
    if job.get('status') not in TERMINAL:
        return 'nonterminal'
    if str(uuid.UUID(job['workspace_id'])) in world['workspaces']:
        return 'live_workspace'
    slots = job.get('slots')
    if not isinstance(slots, list):
        raise ValueError('invalid slot roster')
    for slot in slots:
        if slot.get('surface_id') and str(uuid.UUID(slot['surface_id'])) in world['surfaces']:
            return 'live_surface'
    # Conservative for legacy jobs without reliable process birth metadata:
    # even an unrelated process reusing a recorded PID vetoes this sweep.
    pids = {job.get('worker_pid')} | {s.get('pid') for s in slots}
    if pids & world['processes'].keys():
        return 'recorded_pid_alive'
    sessions = {s.get('session_id') for s in slots if s.get('session_id')}
    needles = {job['id'], str(directory)} | sessions
    if any(needle in command for command in world['processes'].values() for needle in needles):
        return 'process_reference'
    if any(p == str(directory) or p.startswith(str(directory) + '/') for p in world['opened']):
        return 'open_file'
    return None


def log_files(directory):
    db = directory / 'native-db'
    if not db.exists():
        return []
    safe_chain(db)
    files = []
    for parent, dirs, names in os.walk(db, followlinks=False):
        parent = Path(parent)
        stamp(parent, directory=True)
        for name in dirs:
            stamp(parent / name, directory=True)
        for name in names:
            if LOG_NAME.fullmatch(name):
                path = parent / name
                identity = stamp(path)
                files.append({'path': str(path.relative_to(directory)), 'identity': identity,
                              'allocated_bytes': path.lstat().st_blocks * 512})
    return sorted(files, key=lambda item: item['path'])


def batch_candidate(directory, config, world, idle_hours):
    safe_chain(directory)
    job, job_sha = read_json(directory / 'job.json')
    if job.get('id') != directory.name or str(uuid.UUID(job['id'])) != directory.name:
        raise ValueError('job identity mismatch')
    if job.get('config_path') != str(config):
        raise ValueError('foreign configuration')
    reason = inactive(job, directory, world)
    if reason:
        return None, reason
    files = log_files(directory)
    if not files:
        return None, 'no_logs'
    newest = max([directory.joinpath('job.json').stat().st_mtime] +
                 [f['identity'][3] / 1e9 for f in files])
    if time.time() - newest < idle_hours * 3600:
        return None, 'recent'
    return {'job_id': job['id'], 'workspace_id': job['workspace_id'], 'status': job['status'],
            'job_sha256': job_sha, 'directory_identity': stamp(directory, directory=True)[:2],
            'allocated_bytes': sum(f['allocated_bytes'] for f in files), 'files': files}, None


def preview(app, idle_hours=24, observer=observe):
    if not math.isfinite(idle_hours) or idle_hours < 24:
        raise ValueError('minimum idle time is 24 hours')
    if digest(Path(__file__).read_bytes()) != SOURCE_AT_LOAD:
        raise ValueError('loaded cleanup source changed')
    safe_chain(app)
    root, config = app / 'workspace-batches', app / 'config.json'
    safe_chain(root)
    _, config_sha = read_json(config)
    result = {'version': 1, 'app': str(app), 'created_at': time.time(),
              'idle_hours': idle_hours, 'config_sha256': config_sha,
              'source_sha256': SOURCE_AT_LOAD,
              'candidates': [], 'skipped': [], 'allocated_bytes': 0}
    world = observer(root)
    for directory in sorted(root.iterdir()):
        try:
            uuid.UUID(directory.name)
            if time.monotonic() - world['observed'] > 5:
                world = observer(root)
            with locked(directory / 'worker.lock'):
                candidate, reason = batch_candidate(directory, config, world, idle_hours)
            if candidate:
                result['candidates'].append(candidate)
                result['allocated_bytes'] += candidate['allocated_bytes']
            else:
                result['skipped'].append({'job_id': directory.name, 'reason': reason})
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            result['skipped'].append({'job_id': directory.name, 'reason': type(exc).__name__,
                                      'detail': str(exc)})
    if read_json(config)[1] != config_sha:
        raise ValueError('configuration changed during preview')
    if digest(Path(__file__).read_bytes()) != SOURCE_AT_LOAD:
        raise ValueError('cleanup source changed during preview')
    result['skip_counts'] = dict(collections.Counter(x['reason'] for x in result['skipped']))
    return result


def emit(handle, value):
    handle.write(json.dumps(value, sort_keys=True) + '\n')
    handle.flush()
    os.fsync(handle.fileno())


def unlink_log(directory, item, world):
    """Walk from / with O_NOFOLLOW, unlink relative to the pinned parent FD."""
    relative = Path(item['path'])
    if (relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'native-db'
            or not LOG_NAME.fullmatch(relative.name)):
        raise ValueError('not an allowed logs database path')
    path = directory / relative
    with contextlib.ExitStack() as stack:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
        chain = []
        for name in path.parent.parts[1:]:
            parent_fd = fd
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            stack.callback(os.close, fd)
            opened = os.fstat(fd)
            chain.append((parent_fd, name, opened.st_dev, opened.st_ino))
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        stack.callback(os.close, file_fd)
        s = os.fstat(file_fd)
        identity = [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
        if (identity != item['identity'] or not stat.S_ISREG(s.st_mode)
                or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o022):
            raise ValueError('log identity changed')
        for parent_fd, name, device, inode in chain:
            s = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(s.st_mode) or (s.st_dev, s.st_ino) != (device, inode):
                raise ValueError('log parent replaced')
        s = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns] != identity:
            raise ValueError('log path replaced')
        if time.monotonic() - world['observed'] > 10:
            raise ValueError('ownership observation expired before unlink')
        os.unlink(path.name, dir_fd=fd)


def apply(plan, receipt, *, observer=observe, max_jobs=32, authorized=lambda: True):
    if (plan.get('version') != 1 or not 0 <= time.time() - plan['created_at'] <= 1800
            or plan['source_sha256'] != SOURCE_AT_LOAD
            or digest(Path(__file__).read_bytes()) != SOURCE_AT_LOAD):
        raise ValueError('expired or foreign plan')
    if not math.isfinite(plan['idle_hours']) or plan['idle_hours'] < 24:
        raise ValueError('invalid retention')
    app = Path(plan['app'])
    safe_chain(app)
    root, config = app / 'workspace-batches', app / 'config.json'
    safe_chain(root)
    before_free = os.statvfs(app).f_bavail * os.statvfs(app).f_frsize
    deleted, total = 0, 0
    # Receipt must be durable before deleting any file; no database copies.
    with receipt.open('x', encoding='utf-8') as out:
        os.chmod(receipt, 0o600)
        emit(out, {'event': 'start', 'time': time.time(), 'free_bytes': before_free})
        for entry in plan['candidates'][:max_jobs]:
            if not authorized() or any((JANITOR / name).exists() for name in ('DISABLED', 'GUARD_TRIPPED', 'GUARD_UNAVAILABLE')):
                emit(out, {'event': 'stopped', 'reason': 'janitor_disabled_or_guard'});
                break
            jid = entry['job_id']
            if str(uuid.UUID(jid)) != jid:
                raise ValueError('invalid job ID')
            directory = root / jid
            try:
                # Same lock order as CCC batch mutation: worker, then config.
                with locked(directory / 'worker.lock'), locked(app / 'config.lock'):
                    if read_json(config)[1] != plan['config_sha256']:
                        raise ValueError('configuration changed')
                    current, reason = batch_candidate(directory, config, observer(root), plan['idle_hours'])
                    if reason or current != entry:
                        raise ValueError(reason or 'batch/file identity changed')
                    emit(out, {'event': 'delete_intent', 'job_id': jid, 'files': entry['files'],
                               'job_sha256': entry['job_sha256']})
                    # fsync may block. The pre-intent observation cannot
                    # authorize deletion after a new process or writer appears.
                    final_world = observer(root)
                    if read_json(config)[1] != plan['config_sha256']:
                        raise ValueError('configuration changed after intent')
                    final, reason = batch_candidate(directory, config, final_world, plan['idle_hours'])
                    if reason or final != entry:
                        raise ValueError(reason or 'batch changed after intent')
                    # Recheck every file before any group mutation. Reject an
                    # unsafe path, changed inode, size, ctime or mtime.
                    for item in entry['files']:
                        path = directory / item['path']
                        safe_chain(path.parent)
                        if stamp(path) != item['identity']:
                            raise ValueError('log changed before delete')
                    for item in entry['files']:
                        if not authorized() or any((JANITOR / name).exists() for name in ('DISABLED', 'GUARD_TRIPPED', 'GUARD_UNAVAILABLE')):
                            raise ValueError('cleanup disabled before unlink')
                        path = directory / item['path']
                        if stamp(path) != item['identity']:
                            raise ValueError('log changed during delete')
                        unlink_log(directory, item, final_world)
                        deleted += 1
                        total += item['allocated_bytes']
                    emit(out, {'event': 'deleted', 'job_id': jid, 'allocated_bytes': entry['allocated_bytes']})
            except (OSError, ValueError, RuntimeError) as exc:
                emit(out, {'event': 'skipped_or_partial', 'job_id': jid, 'reason': str(exc)})
        after_free = os.statvfs(app).f_bavail * os.statvfs(app).f_frsize
        result = {'event': 'finished', 'deleted_files': deleted, 'unlinked_allocated_bytes': total,
                  'free_bytes_before': before_free, 'free_bytes_after': after_free,
                  'observed_free_delta': after_free - before_free}
        emit(out, result)
    return result


def write_plan(path, value):
    with path.open('x', encoding='utf-8') as f:
        os.chmod(path, 0o600)
        json.dump(value, f, sort_keys=True, indent=2)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    return digest(path.read_bytes())


def scheduled(policy_path):
    policy, policy_sha = read_json(policy_path)
    if policy.get('enabled') is not True:
        return {'event': 'disabled', 'deleted_files': 0}
    if (policy.get('version') != 1 or policy.get('app') != str(APP)
            or policy.get('source_sha256') != digest(Path(__file__).read_bytes())):
        raise ValueError('unrecognized scheduled policy or source')
    for key, low, high in [('idle_hours', 168, 8760), ('pressure_idle_hours', 24, 168),
                          ('min_free_gib', 1, 100), ('max_jobs', 1, 32)]:
        if type(policy.get(key)) is not int or not low <= policy[key] <= high:
            raise ValueError('invalid scheduled limit: ' + key)

    def authorized():
        if (read_json(policy_path)[1] != policy_sha
                or digest(Path(__file__).read_bytes()) != policy['source_sha256']
                or SOURCE_AT_LOAD != policy['source_sha256']):
            return False
        modes = re.findall(r'^MODE=(\w+)\s*$', (JANITOR / 'config.env').read_text(), re.M)
        state, _ = read_json(JANITOR / 'guard-state.json')
        age = time.time() - datetime.datetime.fromisoformat(state['observed_at'].replace('Z', '+00:00')).timestamp()
        return modes == ['apply'] and state.get('health') == 'healthy' and 0 <= age <= 300

    if not authorized() or any((JANITOR / n).exists() for n in ('DISABLED', 'GUARD_TRIPPED', 'GUARD_UNAVAILABLE')):
        return {'event': 'paused_or_guard_unavailable', 'deleted_files': 0}
    home = policy_path.parent
    safe_chain(home)
    history = home / 'history'
    safe_chain(history)
    with locked(home / 'sweep.lock'):
        vfs = os.statvfs(APP)
        pressure = vfs.f_bavail * vfs.f_frsize < policy['min_free_gib'] * 1024**3
        plan = preview(APP, policy['pressure_idle_hours'] if pressure else policy['idle_hours'])
        # Bounded per-run work and small receipts. Select largest eligible logs
        # first to relieve pressure; never relax the live-ownership gates.
        plan['candidates'] = sorted(plan['candidates'], key=lambda x: x['allocated_bytes'], reverse=True)[:policy['max_jobs']]
        plan['allocated_bytes'] = sum(x['allocated_bytes'] for x in plan['candidates'])
        plan['skipped'] = []  # aggregate reasons suffice for scheduled status
        run_id = str(uuid.uuid4())
        output = history / (run_id + '.plan.json')
        write_plan(output, plan)
        result = apply(plan, history / (run_id + '.receipt.jsonl'), max_jobs=policy['max_jobs'], authorized=authorized)
        records = sorted(history.iterdir(), key=lambda p: p.lstat().st_mtime, reverse=True)
        for path in records[32:]:
            if re.fullmatch(r'[0-9a-f-]{36}\.(plan\.json|receipt\.jsonl)', path.name):
                stamp(path)
                path.unlink()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=APP)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--scheduled-policy', type=Path)
    parser.add_argument('--idle-hours', type=float, default=24)
    parser.add_argument('--apply-plan', type=Path)
    parser.add_argument('--approve-sha256')
    parser.add_argument('--max-jobs', type=int, default=32)
    args = parser.parse_args()
    if args.scheduled_policy:
        if args.output or args.apply_plan or args.approve_sha256:
            parser.error('scheduled mode cannot be combined with manual output/apply')
        try:
            result = scheduled(args.scheduled_policy)
        except Exception as exc:
            result = {'event': 'failed', 'error': str(exc), 'error_type': type(exc).__name__}
        result['observed_at'] = time.time()
        safe_chain(args.scheduled_policy.parent)
        status = args.scheduled_policy.parent / 'last-status.json'
        temporary = status.with_name('.status-' + str(uuid.uuid4()))
        write_plan(temporary, result)
        os.replace(temporary, status)
        print(json.dumps(result, sort_keys=True))
        if result['event'] == 'failed':
            raise SystemExit(1)
        return
    if not args.output:
        parser.error('--output is required for manual mode')
    if not 1 <= args.max_jobs <= 700:
        parser.error('max-jobs must be 1..700')
    if args.apply_plan:
        plan, sha = read_json(args.apply_plan)
        if sha != args.approve_sha256 or plan['app'] != str(args.app):
            parser.error('exact preview digest and app path required')
        result = apply(plan, args.output, max_jobs=args.max_jobs)
    else:
        if args.approve_sha256:
            parser.error('approve-sha256 requires apply-plan')
        result = preview(args.app, args.idle_hours)
        sha = write_plan(args.output, result)
        result = {'candidate_jobs': len(result['candidates']), 'allocated_bytes': result['allocated_bytes'],
                  'skip_counts': result['skip_counts'], 'plan_sha256': sha, 'plan': str(args.output)}
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
