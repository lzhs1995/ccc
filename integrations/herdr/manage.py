#!/usr/bin/env python3
"""Install, verify, run or deactivate a first-install multi-Herdr guard."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

SOURCE = Path(__file__).resolve().parent
ROLE = 'ccc-herdr-paired-original-store-service-v187'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def put(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(encoded(value))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(f.name, path)
    finally:
        Path(f.name).unlink(missing_ok=True)


def identity(path):
    st = path.stat()
    return [st.st_dev, st.st_ino]


def store(root, payload, headers):
    files = {name: digest(data) for name, data in payload.items()}
    release = root / digest(encoded(files))
    release.mkdir(mode=0o700, parents=True)
    for name, data in payload.items():
        path = release / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
    meta = dict(headers, files=files)
    put(release / 'manifest.json', meta)
    return release, digest(encoded(meta))


def source_files(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def binary(arg, name):
    value = arg or shutil.which(name)
    if not value:
        raise ValueError(f'specify --{name} or install {name} on PATH')
    path = Path(value).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f'{name} is not executable')
    return path


def install(args, root):
    herdr, cmux = binary(args.herdr, 'herdr'), binary(args.cmux, 'cmux')
    discovery = args.discovery_root.expanduser().resolve(strict=True)
    if not discovery.is_dir():
        raise ValueError('Herdr discovery root must be an existing directory')
    if args.arm and args.control is None:
        raise ValueError('--arm requires the original direct CCC --control config')
    control = args.control.expanduser().absolute() if args.control else root / 'control' / 'config.json'
    if args.control:
        if control != control.resolve(strict=True) or control.name != 'config.json':
            raise ValueError('use a canonical existing CCC config.json')
        policy = json.loads(control.read_bytes())
        if args.arm and (policy.get('mode') != 'armed' or policy.get('global_paused') is not False):
            raise ValueError('direct CCC control must already be armed and unpaused')
    if args.protected_pid is not None and args.protected_pid < 2:
        raise ValueError('invalid protected PID')
    protected_surface = str(uuid.UUID(args.protected_surface)).upper() if args.protected_surface else None
    # Existing production state must retain its controller and handoff record.
    state = control.parent / 'herdr-observer' / 'state' if args.control else root / 'state'
    if state.exists() or state.is_symlink():
        raise ValueError('existing observer requires its original controller handoff')
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        root.joinpath('manager.lock').touch(mode=0o600, exist_ok=False)
        if not args.control:
            control.parent.mkdir(mode=0o700)
            policy = json.loads((SOURCE.parents[1] / 'config.example.json').read_bytes())
            policy.update(discovery_excluded_surface_ids=[], cmux_path=str(cmux))
            put(control, policy)
        elif 'discovery_excluded_surface_ids' not in policy:
            raise ValueError('original CCC must define discovery_excluded_surface_ids')
        if state.parent != root:
            state.parent.mkdir(mode=0o700, exist_ok=True)
        state.mkdir(mode=0o700, exist_ok=False)
        lock = state / 'observer.lock'
        lifetime = state / 'original-host-lifetime.lock'
        lock.touch(mode=0o600, exist_ok=False)
        lifetime.touch(mode=0o600, exist_ok=False)
        provider = control.parent / 'codex-provider-retry.sqlite3'
        if not provider.exists():
            with sqlite3.connect(provider) as conn:
                conn.execute('PRAGMA user_version')
        stores = {'state': str(state), 'state_identity': identity(state),
                  'lock': str(lock), 'lock_identity': identity(lock),
                  'claude_ledger': str(state / 'herdr-input-ledger.json'),
                  'codex_ledger': str(state / 'herdr-codex-input-ledger.json'),
                  'panel_repairs': str(state / 'codex-panels'),
                  'provider_db': str(provider), 'provider_identity': identity(provider),
                  'claude_journal': str(control.parent / 'claude-events.jsonl')}
        payload = source_files(SOURCE / 'runtime')
        prefix = 'mac/source/'
        rows = sorted([k[len(prefix):], digest(v)] for k, v in payload.items() if k.startswith(prefix))
        mac = {'source': 'mac/source', 'files': len(rows), 'sha256': digest(encoded(rows))}
        paired, pair_pin = store(root / 'runtime', payload, {'version': 2, 'mac': mac})
        reader = {'mode': 'read_only', 'endpoints': [], 'herdr_binary': str(herdr),
                  'herdr_binary_sha256': digest(herdr.read_bytes()), 'cmux_binary': str(cmux),
                  'dependency_runtime': str(paired), 'dependency_manifest_sha256': pair_pin,
                  'dependency_payloads': len(payload), 'dependency_mac': mac}
        original, original_pin = store(root / 'observer', dict(source_files(SOURCE / 'observer'),
            **{'observer-config.json': encoded(reader)}), {'mode': 'read_only'})
        installation = str(uuid.uuid4())
        activation = root / 'activation.json'
        config = {'role': ROLE, 'armed': args.arm, 'public_installation': installation,
                  'state': str(state), 'ccc_config': str(control), 'original_stores': stores,
                  'observer': reader, 'original_observer_release': str(original),
                  'original_observer_manifest_sha256': original_pin,
                  'activation_path': str(activation), 'adoption_path': str(root / 'adopted.json'),
                  'host_lifetime_gate': {'schema': 'ccc-original-host-lifetime-lock-v1',
                      'filename': lifetime.name, 'lock_identity': identity(lifetime),
                      'state': str(state), 'state_identity': identity(state)},
                  'discovery': {'enabled': True, 'input_mode': 'armed' if args.arm else 'read_only',
                      'root': str(discovery), 'max_endpoints': 64, 'scan_interval_sec': 10,
                      'scan_budget_sec': 5}}
        guard = source_files(SOURCE / 'guard')
        guard['guard-config.json'] = encoded(config)
        guard['SOURCE-ORIGINS.json'] = encoded({'source': 'public CCC distribution'})
        guard['ccc_public_protection.py'] = (
            f'PUBLIC_PROTECTED_PID = {args.protected_pid!r}\n'
            f'PUBLIC_PROTECTED_SURFACE = {protected_surface!r}\n').encode()
        release, pin = store(root / 'guard', guard, {'role': ROLE})
        receipt = {'schema': 'ccc-public-herdr-install-v1', 'installation': installation,
                   'manifest_sha256': pin, 'original_stores': stores,
                   'armed': args.arm, 'ledger_preimages': {}}
        put(activation, receipt)
        selected = dict(release=release.name, manifest_sha256=pin, activation_sha256=digest(encoded(receipt)))
        verify = run(root, selected, 'verify')
        if verify:
            raise ValueError(f'installed dependency verification returned {verify}')
        put(root / 'active.json', selected)
        print(json.dumps({'instance': str(root), 'release': release.name, 'armed': args.arm}))
    except BaseException:
        # No service can start until selection is published. First-install
        # failure only unclaims the empty lockfiles this invocation created.
        if not (root / 'active.json').exists():
            if args.control and 'lifetime' in locals() and state.is_dir():
                for file in (lifetime, lock):
                    if file.exists() and file.stat().st_size == 0:
                        file.unlink()
                if not any(state.iterdir()):
                    state.rmdir()
            shutil.rmtree(root)
        raise


def run(root, selected, action):
    release = root / 'guard' / selected['release']
    if release.parent != root / 'guard' or release != release.resolve(strict=True):
        raise ValueError('invalid selected guard')
    manifest = release / 'manifest.json'
    if digest(manifest.read_bytes()) != selected['manifest_sha256']:
        raise ValueError('modified service manifest')
    entry = release / 'ccc_herdr_production187.py'
    if digest(entry.read_bytes()) != json.loads(manifest.read_bytes())['files'][entry.name]:
        raise ValueError('modified service entry')
    argv = [sys.executable, '-BE', str(entry), '--config', str(release / 'guard-config.json'),
            '--manifest-sha256', selected['manifest_sha256']]
    if action == 'verify':
        argv.append('--verify-only')
    else:
        argv += ['--serve', '--activation', str(root / 'activation.json'),
                 '--activation-sha256', selected['activation_sha256']]
        if action == 'once':
            argv.append('--once')
    if action == 'verify':
        check = subprocess.run(argv, stdout=subprocess.PIPE, check=False)
        if check.returncode == 0:
            receipt = json.loads(check.stdout)
            print(json.dumps({'verified': receipt['verified'], 'paired_payloads': receipt['paired_payloads'],
                              'loaded_modules': len(receipt['loaded_dependencies'])}))
        return check.returncode
    return subprocess.run(argv, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('install', 'verify', 'once', 'serve', 'rollback'):
        p = sub.add_parser(action)
        p.add_argument('--instance', type=Path, required=True)
        if action == 'install':
            p.add_argument('--herdr')
            p.add_argument('--cmux')
            p.add_argument('--arm', action='store_true')
            p.add_argument('--control', type=Path)
            p.add_argument('--discovery-root', type=Path, default=Path.home() / '.config/herdr')
            p.add_argument('--protected-surface')
            p.add_argument('--protected-pid', type=int)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.instance.expanduser().absolute()
    if root != root.resolve():
        parser.error('use a canonical path for the instance')
    if args.action == 'install':
        install(args, root)
        return 0
    with (root / 'manager.lock').open('rb') as lock:
        exclusive = args.action == 'rollback'
        fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        pointer = root / 'active.json'
        if exclusive:
            if pointer.exists():
                os.replace(pointer, root / 'deactivated.json')
            elif not (root / 'deactivated.json').exists():
                raise ValueError('missing selection')
            print(json.dumps({'active': False, 'ledgers_retained': True}))
            return 0
        return run(root, json.loads(pointer.read_bytes()), args.action)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error))
