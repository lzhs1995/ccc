"""One private, input-disabled diagnostic of the complete paired Herdr guard.

This is not a second service and cannot be armed. The original guard remains
the live owner; native history and its pending budgets are never imported.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

sys.dont_write_bytecode = True

ROLE = 'ccc-herdr-paired-guard-private-diagnostic-v1'
MEMBERS = frozenset({'ccc_herdr_guard.py', 'ccc_herdr_codex.py',
    'ccc_herdr_lifecycle.py', 'ccc_herdr_selection187.py',
    'ccc_herdr_paired_observer187.py', 'ccc_herdr_paired_guard187.py',
    'SOURCE-ORIGINS.json', 'guard-config.json'})


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate guard release/configuration member')
        result[key] = value
    return result


def regular(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError('Guard source/configuration path is not canonical')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1):
            raise ValueError('Guard source is not a same-owner regular single-link file')
        raw = stream.read()
        after = os.fstat(stream.fileno())
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                           st.st_ctime_ns, st.st_mode)
    if identity(before) != identity(after) or identity(after) != identity(path.lstat()):
        raise ValueError('Guard source/configuration changed during verification')
    return raw


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError('Diagnostic store directory is not canonical')
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError('Diagnostic store directory is not private and same-owner')
    return path, (st.st_dev, st.st_ino)


class PrivateWrites:
    """Audit SQLite and Python filesystem writes before their native calls."""
    def __init__(self, control, state):
        self.roots = [private_directory(control), private_directory(state)]
        self.writes = []
        self.refusals = []
        self.null_device_identity = self._null_identity()
        self.null_device_open_events = 0

    @staticmethod
    def _null_identity():
        expected = {'darwin': os.makedev(3, 2),
                    'linux': os.makedev(1, 3)}.get(sys.platform)
        path = Path('/dev/null')
        if expected is None or path != path.resolve(strict=True):
            raise ValueError('Null device is not a supported canonical device')
        before = path.lstat()
        if (not stat.S_ISCHR(before.st_mode) or before.st_uid != 0
                or before.st_rdev != expected):
            raise ValueError('Null device has an unexpected kernel identity')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            after = path.lstat()
        finally:
            os.close(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_rdev,
                               st.st_mode, st.st_uid, st.st_gid)
        if identity(before) != identity(opened) or identity(opened) != identity(after):
            raise ValueError('Null device changed during descriptor verification')
        return identity(opened)

    def _null_open(self, path, flags):
        safe = (os.O_ACCMODE | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                | getattr(os, 'O_LARGEFILE', 0))
        if (os.fsdecode(path) != '/dev/null' or flags & ~safe
                or flags & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR)):
            return False
        if self._null_identity() != self.null_device_identity:
            raise ValueError('Pinned null device identity changed')
        self.null_device_open_events += 1
        return True

    def allowed(self, candidate):
        candidate = Path(os.fsdecode(candidate)).absolute()
        if candidate != candidate.resolve():
            raise ValueError('Linked diagnostic write path')
        for root, identity in self.roots:
            if private_directory(root)[1] != identity:
                raise ValueError('Diagnostic store root changed')
            if candidate == root or candidate.is_relative_to(root):
                return str(candidate)
        raise ValueError('Write outside the private diagnostic stores')

    def __call__(self, event, args):
        try:
            candidates = []
            if event == 'open':
                path, mode, flags = args
                if not isinstance(path, int) and flags & (
                        os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    if not self._null_open(path, flags):
                        candidates = [path]
            elif event == 'sqlite3.connect':
                source = os.fsdecode(args[0])
                if source == ':memory:':
                    return
                if source.startswith('file:'):
                    uri = urlsplit(source)
                    if parse_qs(uri.query).get('mode') == ['ro']:
                        return
                    candidates = [unquote(uri.path)]
                else:
                    candidates = [source]
            elif event in {'os.rename', 'os.replace'}:
                if any(fd != -1 for fd in args[2:]):
                    raise ValueError('Relative-FD diagnostic rename is not supported')
                candidates = list(args[:2])
            elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod',
                           'os.chown', 'os.truncate', 'os.utime'}:
                candidates = [args[0]]
            elif event in {'os.link', 'os.symlink', 'os.kill', 'os.killpg'}:
                raise ValueError('Diagnostic process/control/link mutation is forbidden')
            if candidates:
                paths = [self.allowed(path) for path in candidates]
                if len(self.writes) < 1024:
                    self.writes.append({'event': event, 'paths': paths})
        except (OSError, ValueError, TypeError) as error:
            if len(self.refusals) < 64:
                self.refusals.append({'event': event, 'reason': str(error)})
            raise


def verify_own(root, config_path, pin):
    raw = regular(root / 'manifest.json')
    if digest(raw) != pin:
        raise ValueError('Private guard manifest changed')
    manifest = json.loads(raw, object_pairs_hook=unique)
    if (manifest.get('role') != ROLE or set(manifest.get('files', {})) != MEMBERS
            or {p.name for p in root.iterdir()} != MEMBERS | {'manifest.json'}
            or config_path != root / 'guard-config.json'
            or root.name != digest(canonical(manifest['files']))):
        raise ValueError('Private guard corpus/configuration selection changed')
    for name, pin in manifest['files'].items():
        if digest(regular(root / name)) != pin:
            raise ValueError('Private guard member changed: ' + name)
    config = json.loads(regular(config_path), object_pairs_hook=unique)
    if config.get('role') != ROLE or config.get('armed') is not False:
        raise ValueError('Private guard diagnostic cannot be armed')
    control = Path(config['ccc_config'])
    state = Path(config['state'])
    private_root, _ = private_directory(config['private_root'])
    if (control != private_root / 'private-control' / 'config.json'
            or state != private_root / 'private-state'):
        raise ValueError('Diagnostic control/state namespaces changed')
    private_directory(control.parent)
    private_directory(state)
    if digest(regular(control)) != config['ccc_config_sha256']:
        raise ValueError('Private input-disabled control configuration changed')
    policy = json.loads(regular(control), object_pairs_hook=unique)
    if policy.get('mode') != 'read_only' or policy.get('global_paused') is not True:
        raise ValueError('Private control configuration permits automatic input')
    if config_path != config_path.resolve(strict=True):
        raise ValueError('Linked guard configuration selection')
    return manifest, config


def load_original(config, paired):
    original = Path(config['original_observer_release'])
    raw = paired.regular_bytes(original / 'manifest.json')
    if paired.sha(raw) != config['original_observer_manifest_sha256']:
        raise ValueError('Original observer provenance changed')
    manifest = json.loads(raw, object_pairs_hook=unique)
    if set(manifest.get('files', {})) != {'ccc_herdr_observer.py', 'observer-config.json'}:
        raise ValueError('Original observer corpus changed')
    for name, pin in manifest['files'].items():
        if paired.sha(paired.regular_bytes(original / name)) != pin:
            raise ValueError('Original observer source changed')
    spec = importlib.util.spec_from_file_location('ccc_herdr_observer',
                                                 original / 'ccc_herdr_observer.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--once', action='store_true', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest, config = verify_own(root, args.config.absolute(), args.manifest_sha256)
    boundary = PrivateWrites(Path(config['ccc_config']).parent, config['state'])
    sys.addaudithook(boundary)

    paired = importlib.import_module('ccc_herdr_paired_observer187')
    runtime, payload = paired.verify_pair(config['observer'])
    members = paired.module_members(runtime, payload)
    for name, pin in manifest['files'].items():
        if name.endswith('.py'):
            members.setdefault(Path(name).stem, {})[str(root / name)] = pin
    if any(name in sys.modules for name in paired.module_members(runtime, payload)):
        raise ValueError('Private guard already contains cached paired dependencies')
    if any(name in sys.modules for name in ('ccc_herdr_guard', 'ccc_herdr_codex',
                                           'ccc_herdr_lifecycle', 'ccc_herdr_observer')):
        raise ValueError('Private guard already contains cached original modules')
    original, old = load_original(config, paired)
    members['ccc_herdr_observer'] = {str(Path(config['original_observer_release']) /
        'ccc_herdr_observer.py'): old['files']['ccc_herdr_observer.py']}
    sys.path.insert(0, str(runtime))
    transport = importlib.import_module('ccc_herdr_guard')
    codex = importlib.import_module('ccc_herdr_codex')
    request = transport.request
    write_requests = []
    def diagnostic_request(endpoint, expected_server, method, params, scope, **kwargs):
        if method not in transport.READ_METHODS or kwargs.get('before_write') is not None:
            write_requests.append(method)
            raise ValueError('Private diagnostic refuses every Herdr write method')
        return request(endpoint, expected_server, method, params, scope, **kwargs)
    transport.request = diagnostic_request
    guard = codex.build_guard_class(transport.build_guard_class(original.Observer))(config)
    importlib.import_module('ccc_compact_runtime').libraries()
    importlib.import_module('cmux_codex_watch').validate_runtime_release(runtime)
    paired.loaded_origins(members)
    state = Path(config['state'])
    with paired.isolated_state(state) as (_, identity):
        verify_own(root, args.config.absolute(), args.manifest_sha256)
        paired.verify_pair(config['observer'])
        snapshot = guard.capture(state)
        # These complete original transaction routines may update only the
        # private stores. The independent transport kill switch forbids input.
        transport.step(guard, snapshot, state)
        codex.step(guard, snapshot, state)
        verify_own(root, args.config.absolute(), args.manifest_sha256)
        paired.verify_pair(config['observer'])
        origins = paired.loaded_origins(members)
        snapshot.update(role=ROLE, observed_at_epoch=time.time(), observer_lock=identity,
            paired_runtime=str(runtime), paired_manifest_sha256=config['observer']['dependency_manifest_sha256'],
            paired_payloads=len(payload['files']), paired_mac=payload['mac'],
            loaded_dependencies=origins, guard_manifest_sha256=args.manifest_sha256,
            store_write_events=boundary.writes, store_write_refusals=boundary.refusals,
            null_device_open_events=boundary.null_device_open_events,
            null_device_identity=boundary.null_device_identity,
            blocked_write_methods=write_requests, client_control_operations=0,
            input_operations_this_run=0, diagnostic_only=True, standalone_scheduler=False,
            v184_reconfigured=False, all_continuation_rules_active=False)
        paired.publish(state, snapshot)
        print(json.dumps({'status': str(state / 'status.json'), 'agents': len(snapshot['agents']),
                          'input_operations': snapshot.get('input_operations', 0),
                          'input_operations_this_run': 0, 'client_control_operations': 0,
                          'blocked_write_methods': write_requests, 'store_write_refusals': boundary.refusals}))


if __name__ == '__main__':
    main()
