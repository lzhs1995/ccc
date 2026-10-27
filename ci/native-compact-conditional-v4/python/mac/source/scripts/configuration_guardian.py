#!/usr/bin/env python3
"""Reconcile owned hooks after provider switches; never run a transport command.

One launchd job follows a pinned CURRENT.json, watches live hook files,
and checks profiles every 60 seconds. API/provider values are neither copied nor logged.
The existing installation lock serializes this job with the shared installer.
"""
import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

LABEL = 'org.multi-agent-collaboration.configuration-guardian'
MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_RELEASE_BYTES = 64 * 1024 * 1024
MAX_PROFILES = 1024
MAX_PROFILE_SNAPSHOT_BYTES = 32 * 1024 * 1024
PROFILE_BUDGET_SECONDS = 5.0
WRITE_BUDGET_SECONDS = 1.0
DATABASE_BUSY_SECONDS = 0.25


class ProfileBudgetExceeded(RuntimeError):
    pass


class ProfileSnapshotLimit(RuntimeError):
    pass


class ProfileConflict(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def state_root(home):
    return Path(home) / '.local/state/multi-agent-collaboration'


def read_json(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_BYTES:
            raise ValueError('configuration is not a bounded regular file')
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('configuration must be an object')
    return raw, value


def atomic_json(path, value, expected=None, compare=False):
    """CAS against exact bytes; one bounded, private replacement, no backup growth."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('symlinked configuration refused')
    def current():
        return read_json(path)[0] if path.exists() else None
    if compare and current() != expected:
        raise RuntimeError('configuration changed concurrently')
    fd, pending = tempfile.mkstemp(prefix='.guardian-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink() or compare and current() != expected:
            raise RuntimeError('configuration changed during replacement')
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def verify_release(home, manifest_path):
    """Read and authenticate every immutable member, without loading an installer.

    Return the manifest from this verification pass so read-only callers can
    check their own membership without a second, potentially different read.
    No cross-invocation cache replaces content verification.
    """
    _raw, current = read_json(manifest_path)
    source = Path(current['source'])
    base = Path(home) / '.local/share/multi-agent-collaboration/releases'
    relative = source.relative_to(base)
    if (len(relative.parts) != 2 or relative.parts[-1] != 'source'
            or source.resolve() != source or '..' in source.parts):
        raise ValueError('CURRENT source is outside the immutable release layout')
    raw, manifest = read_json(source.parent / 'MANIFEST.json')
    if digest(raw) != current.get('manifest_sha256'):
        raise ValueError('release manifest hash mismatch')
    files = manifest.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 2048:
        raise ValueError('invalid release file manifest')
    total = 0
    for name, pin in files.items():
        path = source / name
        if (Path(name).is_absolute() or '..' in Path(name).parts
                or path.is_symlink() or not path.is_file()):
            raise ValueError('invalid release member')
        st = path.stat()
        total += st.st_size
        if total > MAX_RELEASE_BYTES or st.st_mode & 0o222:
            raise ValueError('release is writable or exceeds verification budget')
        if st.st_size != pin['bytes'] or digest(path.read_bytes()) != pin['sha256']:
            raise ValueError('release content mismatch: ' + name)
    required = {'scripts/configuration_guardian.py', 'scripts/manage_install.py'}
    if not required.issubset(files):
        raise ValueError('release lacks guardian/installer pins')
    if source / 'scripts/configuration_guardian.py' != Path(__file__).resolve():
        raise ValueError('guardian job is not from CURRENT release; reinstall the single job')
    if Path(current['python']).resolve() != Path(sys.executable).resolve():
        raise ValueError('guardian Python does not match CURRENT')
    return current, manifest


def load_release(home, manifest_path):
    """Only the configuration owner needs to execute the verified installer."""
    current, _manifest = verify_release(home, manifest_path)
    source = Path(current['source'])
    spec = importlib.util.spec_from_file_location('guardian_approved_install', source / 'scripts/manage_install.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    return current, manager


def foreign_hooks(doc, manager, home):
    hooks = copy.deepcopy(doc.get('hooks', {}))
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    out = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise ValueError('hook event must be a list')
        kept_entries = []
        for entry in entries:
            original = entry.get('hooks', [])
            kept = [h for h in original if not manager.owned_command(h.get('command'), home)]
            if kept or len(kept) == len(original):
                entry['hooks'] = kept
                kept_entries.append(entry)
        if kept_entries:
            out[event] = kept_entries
    return out


def normalized(doc, manager, home, reask):
    result = manager.transform(doc, home=home, executor_reask=reask)
    if {k: v for k, v in doc.items() if k != 'hooks'} != {k: v for k, v in result.items() if k != 'hooks'}:
        raise AssertionError('non-hook field changed')
    if foreign_hooks(doc, manager, home) != foreign_hooks(result, manager, home):
        raise AssertionError('foreign hook changed')
    return result


def reconcile_files(home, manager, apply):
    results = []
    for client, name, reask in [('claude', 'settings.json', True), ('codex', 'hooks.json', False)]:
        path = Path(home) / ('.' + client) / name
        # A missing live config can be repaired with hooks only, never from a
        # selected provider snapshot that might replace credentials or model.
        raw, doc = read_json(path) if path.exists() or path.is_symlink() else (None, {})
        new = normalized(doc, manager, home, reask)
        changed = doc != new
        result = dict(client=client, change_needed=changed, changed=False,
                      hooks_disabled=doc.get('disableAllHooks') is True)
        if changed and apply:
            atomic_json(state_root(home) / 'configuration-guardian-v1' / (client + '-previous-hooks.json'),
                        {'config_sha256': digest(raw) if raw is not None else None,
                         'hooks': doc.get('hooks'), 'at_epoch': time.time()})
            atomic_json(path, new, raw, compare=True)
            result['changed'] = True
        results.append(result)
    return results


def check_profile_budget(deadline):
    if time.monotonic() >= deadline:
        raise ProfileBudgetExceeded('profile reconcile budget exhausted')


def profile_snapshot(database, deadline):
    """Copy bounded source bytes, then release the read lock before planning."""
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True,
                         timeout=DATABASE_BUSY_SECONDS)
    expired = [False]

    def progress():
        expired[0] = time.monotonic() >= deadline
        return int(expired[0])

    db.set_progress_handler(progress, 1000)
    try:
        db.execute('BEGIN')
        cursor = db.execute(
            "SELECT id, CASE WHEN typeof(settings_config)='text' "
            "AND length(CAST(settings_config AS BLOB))<=? THEN settings_config END "
            "FROM providers WHERE app_type='claude' ORDER BY id LIMIT ?",
            (MAX_CONFIG_BYTES, MAX_PROFILES + 1))
        rows, total = [], 0
        for key, raw in cursor:
            check_profile_budget(deadline)
            if not isinstance(raw, str):
                raise ValueError('profile is not bounded JSON')
            total += len(raw.encode())
            if len(rows) >= MAX_PROFILES or total > MAX_PROFILE_SNAPSHOT_BYTES:
                raise ProfileSnapshotLimit('profile snapshot limit exceeded')
            rows.append((key, raw))
        common = db.execute(
            "SELECT CASE WHEN value IS NULL OR value='' THEN NULL "
            "WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=? "
            "THEN value ELSE 0 END FROM settings WHERE key='common_config_claude'",
            (MAX_CONFIG_BYTES,)).fetchone()
        if common and common[0] is not None:
            if not isinstance(common[0], str):
                raise ValueError('common config is not bounded JSON')
            total += len(common[0].encode())
            if total > MAX_PROFILE_SNAPSHOT_BYTES:
                raise ProfileSnapshotLimit('profile snapshot limit exceeded')
        check_profile_budget(deadline)
        return rows, common
    except sqlite3.OperationalError:
        if expired[0]:
            raise ProfileBudgetExceeded('profile snapshot budget exhausted') from None
        raise
    finally:
        # Clear an expired progress callback before cleaning up the snapshot.
        db.set_progress_handler(None, 0)
        try:
            db.rollback()
        finally:
            db.close()


def profile_plan(rows, common, manager, home, deadline):
    """Normalization and serialization run with no database connection open."""
    plans, disabled = [], 0
    for key, raw in rows:
        check_profile_budget(deadline)
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError('profile must be an object')
        new = normalized(doc, manager, home, True)
        disabled += doc.get('disableAllHooks') is True
        if doc != new:
            plans.append((key, raw, json.dumps(new, ensure_ascii=False)))
    common_next = None
    if common and common[0]:
        doc = json.loads(common[0])
        if not isinstance(doc, dict):
            raise ValueError('common config must be an object')
        # Profiles own our hooks; the common snippet retains all foreign ones.
        if 'hooks' in doc:
            new = copy.deepcopy(doc)
            new['hooks'] = foreign_hooks(doc, manager, home)
            if new != doc:
                common_next = json.dumps(new, ensure_ascii=False)
    check_profile_budget(deadline)
    return plans, common_next, disabled


def apply_profile_plan(database, plans, common, common_next, deadline, timings):
    """One short CAS transaction. Any conflict rolls back every planned change."""
    db = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True,
                         timeout=DATABASE_BUSY_SECONDS)
    write_started = None
    expired = [False]
    write_deadline = deadline

    def progress():
        expired[0] = time.monotonic() >= write_deadline
        return int(expired[0])

    try:
        check_profile_budget(deadline)
        lock_started = time.monotonic()
        try:
            db.execute('BEGIN IMMEDIATE')
            write_started = time.monotonic()
            timings['write_transaction_started'] = True
        finally:
            timings['lock_wait_seconds'] = time.monotonic() - lock_started
        write_deadline = min(deadline, write_started + WRITE_BUDGET_SECONDS)
        db.set_progress_handler(progress, 1000)
        for key, raw, serialized in plans:
            check_profile_budget(write_deadline)
            count = db.execute(
                "UPDATE providers SET settings_config=? WHERE id=? "
                "AND app_type='claude' AND settings_config=?",
                (serialized, key, raw)).rowcount
            if count != 1:
                raise ProfileConflict('profile compare-and-swap failed')
        if common_next is not None:
            check_profile_budget(write_deadline)
            count = db.execute(
                "UPDATE settings SET value=? WHERE key='common_config_claude' AND value=?",
                (common_next, common[0])).rowcount
            if count != 1:
                raise ProfileConflict('common-hook compare-and-swap failed')
        check_profile_budget(write_deadline)
        db.commit()
    except sqlite3.OperationalError:
        if expired[0]:
            raise ProfileBudgetExceeded('profile write budget exhausted') from None
        raise
    finally:
        # A progress timeout must not interrupt rollback itself.
        db.set_progress_handler(None, 0)
        try:
            if db.in_transaction:
                db.rollback()
        finally:
            db.close()
            if write_started is not None:
                timings['transaction_seconds'] = time.monotonic() - write_started


def database_busy(exc):
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return code & 255 in (5, 6)  # SQLITE_BUSY / SQLITE_LOCKED, including extensions.
    # Python 3.10 has no sqlite_errorcode attribute. Do not log exception text.
    return str(exc).startswith(('database is locked', 'database table is locked',
                                'database schema is locked'))


def reconcile_profiles(home, manager, apply):
    database = Path(home) / '.cc-switch/cc-switch.db'
    if not database.exists():
        return dict(status='DATABASE_ABSENT', profiles=0, change_needed=0, changed=0)
    if database.is_symlink() or not database.is_file():
        raise ValueError('cc-switch database must be a regular nonsymlink file')
    original = database.stat()
    started = time.monotonic()
    deadline = started + PROFILE_BUDGET_SECONDS
    timings = dict(snapshot_seconds=0.0, planning_seconds=0.0,
                   lock_wait_seconds=0.0, transaction_seconds=0.0,
                   write_transaction_started=False)
    result = dict(status='CHECKED', profiles=None, change_needed=None, changed=0,
                  write_committed=False, hooks_disabled=0, common_hook_cleanup=False,
                  provider_selection_unchanged=True, non_hook_fields_preserved=True,
                  timings=timings)
    try:
        stage = time.monotonic()
        try:
            rows, common = profile_snapshot(database, deadline)
        finally:
            timings['snapshot_seconds'] = time.monotonic() - stage
        result['profiles'] = len(rows)
        stage = time.monotonic()
        try:
            plans, common_next, disabled = profile_plan(rows, common, manager, home, deadline)
        finally:
            timings['planning_seconds'] = time.monotonic() - stage
        result.update(change_needed=len(plans), hooks_disabled=disabled,
                      common_hook_cleanup=common_next is not None)
        # No-op checks never open a writable connection or reserve the writer.
        if apply and (plans or common_next is not None):
            latest = database.stat()
            if database.is_symlink() or (original.st_dev, original.st_ino) != (latest.st_dev, latest.st_ino):
                raise ProfileConflict('database changed during planning')
            apply_profile_plan(database, plans, common, common_next, deadline, timings)
            result.update(changed=len(plans), write_committed=True)
    except ProfileConflict:
        result['status'] = 'CONCURRENT_EDIT'
    except ProfileBudgetExceeded:
        result['status'] = 'PROFILE_BUDGET_EXHAUSTED'
    except ProfileSnapshotLimit:
        result['status'] = 'PROFILE_SNAPSHOT_LIMIT'
    except sqlite3.OperationalError as exc:
        if not database_busy(exc):
            raise
        result['status'] = 'LOCK_BUSY'
    timings['total_seconds'] = time.monotonic() - started
    return result


def reconcile(home, manifest_path, apply=False):
    home = Path(home).expanduser().resolve()
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='INSTALLER_BUSY', changed=False, input_operations=0)
        current, manager = load_release(home, manifest_path)
        result = dict(status='RECONCILED', source=current['source'], at_epoch=time.time(),
                      apply=apply, input_operations=0, runtime_reload_proven=False)
        result['profiles'] = reconcile_profiles(home, manager, apply)
        result['files'] = reconcile_files(home, manager, apply)
        if result['profiles']['status'] not in ('CHECKED', 'DATABASE_ABSENT'):
            result['status'] = result['profiles']['status']
        elif any(x['hooks_disabled'] for x in result['files']) or result['profiles'].get('hooks_disabled'):
            result['status'] = 'HOOKS_DISABLED_EXTERNALLY'
        if apply:
            atomic_json(root / 'configuration-guardian-v1/STATUS.json', result)
        return result


def profile_path(settings):
    """Explicit existing --settings file only; never follow a profile alias."""
    path = Path(settings)
    if (not path.is_absolute() or '..' in path.parts or path.is_symlink()
            or path.resolve(strict=True) != path):
        raise ValueError('profile must be a canonical absolute nonsymlink path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('profile must be an owned regular file')
    return path


def profile_backup(home, path, raw):
    """Keep exact private rollback bytes once per path/content, never in output."""
    directory = state_root(home) / 'configuration-guardian-v1/profile-backups'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.resolve() != directory:
        raise ValueError('symlinked profile backup directory refused')
    backup = directory / (digest(os.fsencode(path)) + '-' + digest(raw) + '.json')
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        info = backup.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or read_json(backup)[0] != raw):
            raise ValueError('existing profile backup is not private and exact')
    else:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    return backup


def reconcile_profile(home, manifest_path, settings, apply=False, expected_sha256=None):
    """Preview or CAS-repair one explicit profile, without touching the guardian job."""
    if expected_sha256 is not None and (
            len(expected_sha256) != 64
            or any(c not in '0123456789abcdef' for c in expected_sha256)):
        raise ValueError('expected SHA256 must be a lowercase complete digest')
    if apply and expected_sha256 is None:
        raise ValueError('apply requires the exact preview SHA256')
    path = profile_path(settings)
    home = Path(home).expanduser().resolve()
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='INSTALLER_BUSY', changed=False, input_operations=0)
        current, manager = load_release(home, manifest_path)
        profile_path(path)
        raw, doc = read_json(path)
        before_sha256 = digest(raw)
        result = dict(status='PROFILE_PLANNED', path=str(path), source=current['source'],
                      before_sha256=before_sha256, bytes=len(raw), apply=apply,
                      changed=False, input_operations=0, guardian_job_changed=False,
                      runtime_reload_proven=False, hooks_disabled=doc.get('disableAllHooks') is True)
        if expected_sha256 is not None and expected_sha256 != before_sha256:
            result['status'] = 'CONCURRENT_EDIT'
            return result
        new = normalized(doc, manager, home, True)
        result.update(change_needed=doc != new, non_hook_fields_preserved=True,
                      foreign_hooks_preserved=True,
                      planned_sha256=digest((json.dumps(new, ensure_ascii=False, indent=2) + '\n').encode()))
        if apply and result['change_needed']:
            # Retain both original rollback bytes and any later external edits.
            result['backup'] = str(profile_backup(home, path, raw))
            profile_path(path)
            try:
                atomic_json(path, new, raw, compare=True)
            except RuntimeError:
                result['status'] = 'CONCURRENT_EDIT'
                return result
            result.update(changed=True, replacement_performed=True)
            try:
                profile_path(path)
                result['after_sha256'] = digest(read_json(path)[0])
            except (OSError, ValueError):
                result['status'] = 'POST_WRITE_VERIFICATION_FAILED'
                return result
            if result['after_sha256'] != result['planned_sha256']:
                # A later writer owns these bytes. Never roll them back or
                # report our planned hooks as the current configuration.
                result['status'] = 'CONCURRENT_EDIT'
                return result
            result['status'] = 'PROFILE_RECONCILED'
        elif not result['change_needed']:
            result['status'] = 'PROFILE_UNCHANGED'
        if result['hooks_disabled']:
            result['status'] = 'HOOKS_DISABLED_EXTERNALLY'
        return result


def launchd_spec(home, source, python, manifest):
    home = Path(home)
    return dict(Label=LABEL, RunAtLoad=True, StartInterval=60, ThrottleInterval=60,
                ProgramArguments=[str(python), '-B', str(Path(source) / 'scripts/configuration_guardian.py'),
                                  'reconcile', '--home', str(home), '--manifest', str(manifest), '--apply'],
                # The database also contains frequently written request logs.
                # Discover profile imports on the interval, not every DB/WAL event.
                WatchPaths=[str(home / '.claude/settings.json'),
                            str(home / '.codex/hooks.json'), str(manifest)],
                StandardOutPath='/dev/null', StandardErrorPath='/dev/null', ProcessType='Background')


def install_job(home, manifest, apply=False):
    current, _manager = load_release(home, manifest)
    path = Path(home) / 'Library/LaunchAgents' / (LABEL + '.plist')
    spec = launchd_spec(home, current['source'], current['python'], manifest)
    raw = plistlib.dumps(spec, sort_keys=True)
    result = dict(status='JOB_PLANNED', plist=str(path), label=LABEL, interval_seconds=60)
    if not apply:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('symlinked launchd job refused')
    old = path.read_bytes() if path.exists() else None
    domain = 'gui/' + str(os.getuid())
    loaded = subprocess.run(['launchctl', 'print', domain + '/' + LABEL], capture_output=True, timeout=10).returncode == 0
    if old != raw:
        fd, pending = tempfile.mkstemp(prefix='.guardian-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if (path.read_bytes() if path.exists() else None) != old:
                raise RuntimeError('launchd job changed concurrently')
            os.replace(pending, path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        if loaded:
            subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], check=True, capture_output=True, timeout=10)
            loaded = False
    if not loaded:
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True, timeout=10)
    result['status'] = 'JOB_REGISTERED'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['reconcile', 'install-job', 'reconcile-profile'])
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--settings', type=Path,
                        help='one explicit absolute Claude --settings JSON; never auto-discovered')
    parser.add_argument('--expected-sha256', help='exact original profile bytes required for apply')
    args = parser.parse_args(argv)
    if args.mode == 'reconcile-profile':
        if args.settings is None or args.apply and args.expected_sha256 is None:
            parser.error('reconcile-profile requires --settings and apply requires --expected-sha256')
    elif args.settings is not None or args.expected_sha256 is not None:
        parser.error('--settings and --expected-sha256 belong only to reconcile-profile')
    manifest = args.manifest or state_root(args.home) / 'CURRENT.json'
    try:
        if args.mode == 'reconcile-profile':
            result = reconcile_profile(args.home, manifest, args.settings, args.apply,
                                       args.expected_sha256)
        else:
            result = (reconcile(args.home, manifest, args.apply) if args.mode == 'reconcile'
                      else install_job(args.home, manifest, args.apply))
    except Exception as exc:
        # Do not emit exception text: JSON/provider parsing errors can contain
        # credentials. Record only the class and keep the next reconcile alive.
        result = dict(status='RECONCILE_FAILED', error_type=type(exc).__name__,
                      at_epoch=time.time(), input_operations=0)
        if args.apply and args.mode != 'reconcile-profile':
            atomic_json(state_root(args.home) / 'configuration-guardian-v1/STATUS.json', result)
    print(json.dumps(result, ensure_ascii=False))
    if args.mode == 'reconcile-profile' and result['status'] == 'INSTALLER_BUSY':
        return 2
    return 0 if result['status'] in ('RECONCILED', 'INSTALLER_BUSY', 'JOB_PLANNED',
                                     'JOB_REGISTERED', 'PROFILE_PLANNED',
                                     'PROFILE_RECONCILED', 'PROFILE_UNCHANGED') else 2


if __name__ == '__main__':
    raise SystemExit(main())
