"""One isolated, read-only observation using an explicitly pinned CCC/MAC pair.

This entry never composes a guard, opens a retry journal, invokes a scheduler,
submits input, or manages a client. The running V184 service remains separate.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Ambiguous manifest/configuration member')
        result[name] = value
    return result


def regular_bytes(path):
    path = Path(path)
    if path.absolute() != path.resolve(strict=True):
        raise ValueError('Linked or noncanonical source path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1):
            raise ValueError('Source is not a same-owner regular single-link file')
        raw = stream.read()
        after = os.fstat(stream.fileno())
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                            info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
    if identity(before) != identity(after) or identity(after) != identity(path.lstat()):
        raise ValueError('Source changed during verification')
    return raw


def verify_pair(config):
    root = Path(config['dependency_runtime'])
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise ValueError('Paired runtime must have a canonical absolute path')
    raw = regular_bytes(root / 'manifest.json')
    if sha(raw) != config['dependency_manifest_sha256']:
        raise ValueError('Paired manifest changed')
    manifest = json.loads(raw, object_pairs_hook=unique)
    if manifest.get('version') != 2:
        raise ValueError('A complete version-two paired runtime is required')
    actual = {}
    for path in sorted(root.rglob('*')):
        if '__pycache__' in path.parts:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            if path != path.resolve(strict=True):
                raise ValueError('Linked runtime directory')
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('Linked or special runtime member')
        if path.name == 'manifest.json' and path.parent == root:
            continue
        actual[path.relative_to(root).as_posix()] = sha(regular_bytes(path))
    mac = sorted([name.removeprefix('mac/source/'), digest]
                 for name, digest in actual.items() if name.startswith('mac/source/'))
    mac_pin = {'source': 'mac/source', 'files': len(mac), 'sha256': sha(canonical(mac))}
    if (actual != manifest.get('files') or len(actual) != config['dependency_payloads']
            or mac_pin != manifest.get('mac') or mac_pin != config['dependency_mac']
            or root.name != sha(canonical(actual))
            or regular_bytes(root / 'manifest.json') != raw):
        raise ValueError('Complete paired runtime, MAC corpus or release ID changed')
    return root, manifest


def module_members(root, manifest):
    members = {}
    for name, digest in manifest['files'].items():
        path = Path(name)
        if path.suffix == '.py' and (len(path.parts) == 1
                                    or path.parent == Path('mac/source/scripts')):
            members.setdefault(path.stem, {})[str(root / path)] = digest
    return members


def loaded_origins(members):
    result = {}
    for name, paths in sorted(members.items()):
        module = sys.modules.get(name)
        if module is None:
            continue
        origin = getattr(module, '__file__', None)
        spec = getattr(module, '__spec__', None)
        if (origin not in paths or getattr(spec, 'origin', None) != origin
                or getattr(spec, 'loader', None) is None
                or sha(regular_bytes(origin)) != paths[origin]):
            raise ValueError('Foreign or changed loaded dependency: ' + name)
        result[name] = {'path': origin, 'sha256': paths[origin], 'spec_origin': spec.origin}
    return result


def native_selection_observer(base, foreground, opt_in):
    """Keep startup argv separate from a later committed native selection.

    Only an authenticated schema2 lease permits replacing the legacy locator
    passed to the original read-only turn reader. Its complete native reader
    still verifies the process, writer, lock, rollout and current lifecycle.
    The original argv and public process dictionary remain unchanged.
    """
    class NativeSelectionObserver(base):
        def codex_turn(self, endpoint, pane, process, state):
            record = {}
            before = foreground.read_foreground(process['pid'], opt_in, _record=record)
            if before[0] not in {'ok', 'absent'}:
                return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                        'native_foreground_observation': before[0],
                        'native_reader_reason': 'Native foreground observation invalid or expired'}
            if before[0] == 'absent':
                # Preserve the independently owned local legacy reader only
                # while native foreground publication remains optional/absent.
                result = super().codex_turn(endpoint, pane, process, state)
                if foreground.read_foreground(process['pid'], opt_in)[0] != 'absent':
                    return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                            'native_reader_reason': 'Native foreground changed during legacy observation'}
                return result
            if before[1] is None:
                return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                        'native_foreground_observation': 'clear',
                        'native_reader_reason': 'Native foreground selection is cleared'}
            if record.get('schema') != 2:
                result = super().codex_turn(endpoint, pane, process, state)
                after_record = {}
                after = foreground.read_foreground(process['pid'], opt_in, _record=after_record)
                if (after[0] != 'ok' or after_record.get('schema') != record.get('schema')
                        or before[1:] != after[1:]):
                    return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                            'native_reader_reason': 'Legacy native foreground changed during observation'}
                return result
            selected = dict(process, native_resume_id=before[1])
            result = super().codex_turn(endpoint, pane, selected, state)
            after_record = {}
            after = foreground.read_foreground(process['pid'], opt_in, _record=after_record)
            if (after[0] != 'ok' or after_record.get('schema') != 2
                    or before[1:] != after[1:]
                    or (result.get('native_reader_available')
                        and result.get('native_session_id') != before[1])):
                return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                        'native_reader_reason': 'Native selection changed or expired during observation'}
            result.update(native_startup_resume_id=process.get('native_resume_id'),
                          native_selection_source='schema2_committed_foreground',
                          native_selection_evidence=after[2],
                          native_selection_expires_at_ms=after_record['expires_at_ms'])
            return result

    return NativeSelectionObserver


class PairedObserver:
    def __init__(self, config):
        if config.get('role') != 'ccc-embedded-mac-readonly-selection-v1':
            raise ValueError('Only the isolated read-only role is supported')
        self.config = config
        self.root, self.manifest = verify_pair(config)
        self.members = module_members(self.root, self.manifest)
        # A fresh observer proves its own import order; a cached module cannot
        # acquire that proof by merely claiming a matching file or version.
        if any(name in sys.modules for name in self.members):
            raise ValueError('Fresh observer already contains cached paired dependencies')
        original = Path(config['original_observer_release'])
        raw = regular_bytes(original / 'manifest.json')
        if sha(raw) != config['original_observer_manifest_sha256']:
            raise ValueError('Original read-only observer manifest changed')
        old = json.loads(raw, object_pairs_hook=unique)
        if set(old.get('files', {})) != {'ccc_herdr_observer.py', 'observer-config.json'}:
            raise ValueError('Original read-only observer corpus changed')
        for name, pin in old['files'].items():
            if sha(regular_bytes(original / name)) != pin:
                raise ValueError('Original read-only observer member changed')
        spec = importlib.util.spec_from_file_location(
            '_ccc_plain_readonly_observer187', original / 'ccc_herdr_observer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.path.insert(0, str(self.root))
        foreground = importlib.import_module('ccc_client_thread_observation')
        policy = importlib.import_module('ccc_request_observation_policy')
        observer_type = native_selection_observer(
            module.Observer, foreground, policy.request_observation_directory_matches)
        observer = observer_type(config)
        self.plain = observer
        # Exercise the actual reader -> bundled MAC import boundary. Loading
        # these utilities does not invoke their input or scheduler operations.
        importlib.import_module('ccc_compact_runtime').libraries()
        importlib.import_module('cmux_codex_watch').validate_runtime_release(self.root)
        self.origins = loaded_origins(self.members)

    def capture(self, state):
        verify_pair(self.config)
        loaded_origins(self.members)
        snapshot = self.plain.capture(state)
        verify_pair(self.config)
        self.origins = loaded_origins(self.members)
        snapshot.update(
            role=self.config['role'], paired_runtime=str(self.root),
            paired_manifest_sha256=self.config['dependency_manifest_sha256'],
            paired_payloads=len(self.manifest['files']), paired_mac=self.manifest['mac'],
            loaded_dependencies=self.origins, standalone_scheduler=False,
            input_operations=0, history_migration=False, control_operations=0,
            state_namespace=str(state), all_continuation_rules_active=False)
        return snapshot


@contextmanager
def isolated_state(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError('State namespace must be canonical and absolute')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('State namespace must be private and same-owner')
    lock_path = path / 'observer.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+b') as lock:
        locked = os.fstat(lock.fileno())
        if (not stat.S_ISREG(locked.st_mode) or locked.st_uid != os.geteuid()
                or locked.st_nlink != 1 or locked.st_mode & 0o077):
            raise ValueError('State lock is not a private regular single-link file')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = lock_path.lstat()
        if (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino):
            raise ValueError('State lock changed')
        yield path, {'device': locked.st_dev, 'inode': locked.st_ino, 'pid': os.getpid()}


def publish(state, snapshot):
    destination = state / 'status.json'
    if destination.exists() or destination.is_symlink():
        regular_bytes(destination)
    temporary = state / ('status-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(snapshot, ensure_ascii=False, indent=2).encode() + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--once', action='store_true', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    raw = regular_bytes(root / 'manifest.json')
    if sha(raw) != args.manifest_sha256:
        raise ValueError('New observer release manifest changed')
    own = json.loads(raw, object_pairs_hook=unique)
    required = {'ccc_herdr_paired_observer187.py', 'observer-config.json'}
    if (set(own.get('files', {})) != required or args.config.absolute() != root / 'observer-config.json'
            or {p.name for p in root.iterdir()} != required | {'manifest.json'}
            or root.name != sha(canonical(own['files']))):
        raise ValueError('New observer release/configuration identity changed')
    for name, pin in own['files'].items():
        if sha(regular_bytes(root / name)) != pin:
            raise ValueError('New observer member changed')
    config = json.loads(regular_bytes(args.config), object_pairs_hook=unique)
    if args.state.absolute() != Path(config['state']):
        raise ValueError('State is not the original new-observer namespace')
    with isolated_state(args.state) as (state, identity):
        observer = PairedObserver(config)
        snapshot = observer.capture(state)
        snapshot.update(observer_lock=identity, observer_source_sha256=sha(regular_bytes(Path(__file__))),
                        observer_manifest_sha256=args.manifest_sha256, observed_at_epoch=time.time())
        publish(state, snapshot)
        print(json.dumps({'status': str(state / 'status.json'), 'role': config['role'],
                          'endpoints': snapshot['endpoints'], 'agents': len(snapshot['agents']),
                          'loaded_dependencies': len(observer.origins), 'input_operations': 0}))


if __name__ == '__main__':
    main()
