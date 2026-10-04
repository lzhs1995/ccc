"""Reversibly wrap existing Claude symlinks, preserving selected executables.

Only subsequent launches change. No client signals, configuration edits, or
model calls. An existing concrete executable is never overwritten.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import uuid


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def snapshot(path):
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('entry must be an owned symlink: ' + str(path))
    target = path.resolve(strict=True)
    data = target.stat()
    if not stat.S_ISREG(data.st_mode) or not os.access(target, os.X_OK):
        raise ValueError('entry must select an executable file')
    return dict(link=os.readlink(path), identity=[info.st_dev, info.st_ino,
                info.st_uid, info.st_mtime_ns, info.st_ctime_ns], target=str(target),
                target_identity=[data.st_dev, data.st_ino, data.st_mode,
                                 data.st_size, data.st_mtime_ns, data.st_ctime_ns],
                target_sha256=digest(target))


def atomic_bytes(path, raw, mode=0o600):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_link(path, target):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save(journal, data):
    atomic_bytes(journal, (json.dumps(data, indent=2) + '\n').encode())


def restore(journal):
    data = json.loads(journal.read_bytes())
    if data['state'] not in ('committed', 'prepared'):
        raise ValueError('journal is not an active install')
    selected = []
    for row in data['entries']:
        path = Path(row['path'])
        # The recorded after identity protects later user/package-manager edits.
        current = snapshot(path)
        if current == row['before']:
            continue
        if current != row.get('after'):
            raise ValueError('entry changed after installation: ' + str(path))
        selected.append(row)
    for row in reversed(selected):
        path = Path(row['path'])
        if snapshot(path) != row['after']:
            raise ValueError('entry changed during rollback')
        replace_link(path, row['before']['link'])
    data['state'] = 'rolled_back'
    save(journal, data)
    return data


def install(package, entries, journal, python=sys.executable, apply=False, prepared=None):
    package, journal = package.resolve(strict=True), journal.absolute()
    if journal.exists():
        raise ValueError('journal already exists')
    if not entries or len(set(map(str, entries))) != len(entries):
        raise ValueError('provide unique entries')
    manifest = json.loads((package / 'RELEASE-MANIFEST.json').read_bytes())
    def validate_package():
        for name in ('ccc_claude_launcher.py', 'ccc_claude_request_observer.cjs'):
            path = package / name
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or digest(path) != manifest['files'][name]):
                raise ValueError('observer package differs from manifest')
    validate_package()
    spec = importlib.util.spec_from_file_location('entry_launcher', package / 'ccc_claude_launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = dict(state='prepared', package=str(package), clients_restarted=False,
                configurations_changed=False, entries=[])
    wrapper_dir = journal.parent / (journal.stem + '-wrappers')
    for i, path in enumerate(entries):
        path = path.absolute()
        before = snapshot(path)
        wrapper = wrapper_dir / f'claude-{i}'
        raw = module.render_entry(python, package / 'ccc_claude_launcher.py', before['target']).encode()
        data['entries'].append(dict(path=str(path), before=before, wrapper=str(wrapper),
                                   wrapper_sha256=hashlib.sha256(raw).hexdigest()))
    if not apply:
        return data
    wrapper_dir.mkdir(mode=0o700)
    for row in data['entries']:
        raw = module.render_entry(python, package / 'ccc_claude_launcher.py', row['before']['target']).encode()
        atomic_bytes(Path(row['wrapper']), raw, 0o700)
    save(journal, data)
    changed = []
    try:
        if prepared:
            prepared()
        validate_package()
        for row in data['entries']:
            if snapshot(Path(row['path'])) != row['before']:
                raise ValueError('entry changed during preparation')
        for row in data['entries']:
            path = Path(row['path'])
            validate_package()
            if digest(Path(row['wrapper'])) != row['wrapper_sha256']:
                raise ValueError('prepared wrapper changed')
            if snapshot(path) != row['before']:
                raise ValueError('entry changed before replacement')
            replace_link(path, row['wrapper'])
            changed.append(row)
            row['after'] = snapshot(path)
            if row['after']['target_sha256'] != row['wrapper_sha256']:
                raise ValueError('installed entry mismatch')
            save(journal, data)
        validate_package()
        data['state'] = 'committed'
        save(journal, data)
    except BaseException:
        errors = []
        for row in reversed(changed):
            try:
                path = Path(row['path'])
                if snapshot(path) != row.get('after'):
                    raise ValueError('changed entry cannot be rolled back automatically')
                replace_link(path, row['before']['link'])
            except Exception as exc:
                errors.append(str(exc))
        data.update(state='rollback_failed' if errors else 'rolled_back', errors=errors)
        save(journal, data)
        raise
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--entry', action='append', type=Path, default=[])
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    data = (restore(args.journal) if args.rollback else
            install(args.package, args.entry, args.journal, args.python, args.apply))
    print(json.dumps(data, indent=2))


if __name__ == '__main__':
    main()
