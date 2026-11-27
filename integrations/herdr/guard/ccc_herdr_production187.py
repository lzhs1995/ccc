"""Prospective paired Herdr service; original stores and one original lock.

This entry is separate from the permanently input-disabled diagnostic. Serving
requires an exact installer activation receipt. Verification never starts a
service, opens a shared database, writes a journal, or contacts an agent.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import subprocess
import time

sys.dont_write_bytecode = True
ROLE = 'ccc-herdr-paired-original-store-service-v187'
MEMBERS = frozenset({'ccc_public_protection.py', 'ccc_herdr_production187.py', 'ccc_herdr_guard.py',
    'ccc_herdr_codex.py', 'ccc_herdr_lifecycle.py', 'ccc_herdr_selection187.py',
    'ccc_herdr_paired_guard187.py', 'ccc_herdr_paired_observer187.py',
    'SOURCE-ORIGINS.json', 'guard-config.json', 'ccc_herdr_native_query.py', 'ccc_herdr_exit_context.py',
    'ccc_resume_capture.py', 'ccc_resume_metadata.py', 'ccc_thread_settings_evidence.py',
    'ccc_original_host_service.py', 'ccc_original_host_registration.py', 'ccc_original_host_entry.py',
    'ccc_hosted_exit_driver.py', 'ccc_hosted_provider_budget.py', 'ccc_hosted_resume_validation.py',
    'ccc_original_child_host.py', 'ccc_original_host_client.py', 'ccc_original_exit_driver.py',
    'ccc_original_exit_boundary.py', 'ccc_original_current_inputs.py', 'ccc_original_resume_inputs.py',
    'ccc_original_launch.py', 'ccc_original_shell_boundary.py', 'ccc_original_writer_check.py',
    'ccc_resume_branch.py', 'ccc_host_lifetime_gate.py', 'ccc_host_claim_freeze.py',
    'ccc_recovery_progress.py', 'ccc_recovery_progress_runtime.py'})


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate production configuration member')
        result[key] = value
    return result


def regular(path, limit=128 * 1024 * 1024):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError('Noncanonical production file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o022
                or before.st_size > limit):
            raise ValueError('Unsafe or oversized production file')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    if identity(before) != identity(after) or identity(after) != identity(path.lstat()):
        raise ValueError('Production file changed while reading')
    return raw


def verify_release(root, config_path, pin):
    root, config_path = Path(root), Path(config_path)
    raw = regular(root / 'manifest.json')
    manifest = json.loads(raw, object_pairs_hook=unique)
    if (sha(raw) != pin or manifest.get('role') != ROLE
            or set(manifest.get('files', {})) != MEMBERS
            or {p.name for p in root.iterdir()} != MEMBERS | {'manifest.json'}
            or config_path != root / 'guard-config.json'):
        raise ValueError('Production release or configuration selection changed')
    canonical = json.dumps(manifest['files'], sort_keys=True, separators=(',', ':')).encode()
    if root.name != sha(canonical):
        raise ValueError('Production release content identity changed')
    for name, expected in manifest['files'].items():
        if sha(regular(root / name)) != expected:
            raise ValueError('Production release member changed: ' + name)
    config = json.loads(regular(config_path), object_pairs_hook=unique)
    if config.get('role') != ROLE or type(config.get('armed')) is not bool:
        raise ValueError('Production role or input gate is invalid')
    verify_store_locations(config)
    return manifest, config


def directory_identity(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError('Original store directory changed')
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise ValueError('Original store directory ownership changed')
    return [st.st_dev, st.st_ino]


def verify_store_locations(config):
    state, control = Path(config['state']), Path(config['ccc_config'])
    stores = config['original_stores']
    expected = {'state': str(state), 'lock': str(state / 'observer.lock'),
        'claude_ledger': str(state / 'herdr-input-ledger.json'),
        'codex_ledger': str(state / 'herdr-codex-input-ledger.json'),
        'panel_repairs': str(state / 'codex-panels'),
        'provider_db': str(control.parent / 'codex-provider-retry.sqlite3'),
        'claude_journal': str(control.parent / 'claude-events.jsonl')}
    if {key: stores.get(key) for key in expected} != expected:
        raise ValueError('Original ledger, provider or journal namespace was forked')
    if any(Path(value) != Path(value).resolve() for value in expected.values()):
        raise ValueError('Original store path contains a link')
    if directory_identity(state) != stores['state_identity']:
        raise ValueError('Original state directory was replaced')
    directory_identity(control.parent)
    regular(control)
    provider = Path(stores['provider_db']).lstat()
    if (not stat.S_ISREG(provider.st_mode) or provider.st_uid != os.geteuid()
            or provider.st_mode & 0o022
            or [provider.st_dev, provider.st_ino] != stores['provider_identity']):
        raise ValueError('Original provider database identity changed')
    return expected


def check_outer_isolation(policy, endpoints):
    excluded = policy.get('discovery_excluded_surface_ids')
    if (not isinstance(excluded, list) or not all(
            e['cmux_surface_id'] in excluded for e in endpoints)):
        raise ValueError('Original outer cmux input isolation is missing')


class OriginalOwner:
    """Hold the existing inode; neither create nor replace an ownership lock."""
    def __init__(self, config):
        self.config = config
        self.fd = None

    def recheck(self):
        verify_store_locations(self.config)
        lock = Path(self.config['original_stores']['lock'])
        current = lock.lstat()
        opened = os.fstat(self.fd)
        expected = self.config['original_stores']['lock_identity']
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1 or opened.st_mode & 0o022
                or [opened.st_dev, opened.st_ino] != expected
                or [current.st_dev, current.st_ino] != expected):
            raise ValueError('Original observer lock inode changed')
        policy = json.loads(regular(self.config['ccc_config']), object_pairs_hook=unique)
        check_outer_isolation(policy, self.config['observer']['endpoints'])
        return policy

    def __enter__(self):
        verify_store_locations(self.config)
        self.fd = os.open(self.config['original_stores']['lock'],
                          os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            self.recheck()
            for attempt in range(40):
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt == 39:
                        raise
                    time.sleep(0.05)
            self.recheck()
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *unused):
        os.close(self.fd)
        self.fd = None


def verify_activation(config, manifest_pin, path, pin, birth):
    if str(path) != config['activation_path']:
        raise ValueError('Foreign activation receipt path')
    raw = regular(path)
    value = json.loads(raw, object_pairs_hook=unique)
    if config.get('public_installation'):
        expected = {'schema': 'ccc-public-herdr-install-v1',
                    'installation': config['public_installation'],
                    'manifest_sha256': manifest_pin, 'original_stores': config['original_stores'],
                    'armed': config['armed'], 'ledger_preimages': {}}
        if sha(raw) != pin or value != expected:
            raise ValueError('Public instance binding changed')
        return value
    if (sha(raw) != pin or value.get('schema') != 'ccc-original-guard-handoff-v187'
            or value.get('manifest_sha256') != manifest_pin
            or value.get('original_stores') != config['original_stores']
            or value.get('old_guard') != config['old_guard']
            or value.get('lock_acquired_same_inode') is not True
            or type(value.get('client_operations')) is not int
            or value.get('client_operations') != 0
            or value.get('ledger_reset') is not False
            or value.get('budget_reset') is not False
            or value.get('authorized_action') != f"replace_only_guard_{config['old_guard']['pid']}"):
        raise ValueError('Exact original-state handoff is not proved')
    if set(value.get('ledger_preimages', {})) != {'herdr-input-ledger.json', 'herdr-codex-input-ledger.json'}:
        raise ValueError('Complete original ledger preimages are required')
    old = config['old_guard']
    if birth(old['pid']) == old['birth']:
        raise ValueError('Original guard remains alive; no second service')
    adopted = Path(config['adoption_path'])
    if adopted.exists():
        acknowledgement = json.loads(regular(adopted), object_pairs_hook=unique)
        if (acknowledgement.get('schema') != 'ccc-original-guard-adopted-v187'
                or acknowledgement.get('activation_sha256') != pin
                or acknowledgement.get('manifest_sha256') != manifest_pin
                or acknowledgement.get('original_stores') != config['original_stores']
                or acknowledgement.get('ledger_preimages') != value['ledger_preimages']):
            raise ValueError('Original adoption receipt changed')
    else:
        for name, record in value['ledger_preimages'].items():
            target = Path(config['state']) / name
            if record['exists'] != target.exists() or (record['exists'] and
                    sha(regular(target)) != record['sha256']):
                raise ValueError('Original pending state changed before first adoption')
    provider = Path(config['original_stores']['provider_db']).lstat()
    if [provider.st_dev, provider.st_ino] != value['provider_identity']:
        raise ValueError('Original provider budget database was replaced')
    return value


def publish_first_adoption(config, activation, activation_pin, manifest_pin):
    path = Path(config['adoption_path'])
    if path.exists():
        return
    value = {'schema': 'ccc-original-guard-adopted-v187',
        'activation_sha256': activation_pin, 'manifest_sha256': manifest_pin,
        'original_stores': config['original_stores'],
        'ledger_preimages': activation['ledger_preimages'],
        'pid': os.getpid(), 'at_epoch': time.time()}
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    # The original observer lock covers this exclusive durable publication.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_dependencies(root, config, manifest):
    paired = importlib.import_module('ccc_herdr_paired_observer187')
    runtime, payload = paired.verify_pair(config['observer'])
    members = paired.module_members(runtime, payload)
    if any(name in sys.modules for name in members):
        raise ValueError('Production process contains cached paired dependencies')
    for name, pin in manifest['files'].items():
        if name.endswith('.py'):
            members.setdefault(Path(name).stem, {})[str(root / name)] = pin
    private = importlib.import_module('ccc_herdr_paired_guard187')
    original, old = private.load_original(config, paired)
    members['ccc_herdr_observer'] = {str(Path(config['original_observer_release']) /
        'ccc_herdr_observer.py'): old['files']['ccc_herdr_observer.py']}
    sys.path.insert(0, str(runtime))
    transport = importlib.import_module('ccc_herdr_guard')
    codex = importlib.import_module('ccc_herdr_codex')
    importlib.import_module('ccc_compact_runtime').libraries()
    importlib.import_module('cmux_codex_watch').validate_runtime_release(runtime)
    # Load the complete new assembly before recording origins. These modules
    # have no start/registration side effects at import time.
    for name in ('ccc_original_host_service', 'ccc_original_host_entry',
                 'ccc_original_child_host', 'ccc_hosted_resume_validation',
                 'ccc_host_lifetime_gate', 'ccc_host_claim_freeze', 'ccc_recovery_progress_runtime'):
        importlib.import_module(name)
    origins = paired.loaded_origins(members)
    return paired, runtime, payload, members, original, transport, codex, origins


def serve(root, config_path, manifest_pin, activation_path, activation_pin, once=False):
    manifest, config = verify_release(root, config_path, manifest_pin)
    paired, runtime, payload, members, original, transport, codex, origins = load_dependencies(root, config, manifest)
    import ccc_guard_scope as scope
    with OriginalOwner(config) as owner:
        activation = verify_activation(config, manifest_pin, activation_path, activation_pin, scope.birth)
        publish_first_adoption(config, activation, activation_pin, manifest_pin)
        original_request = transport.request
        def owned_request(endpoint, server, method, params, current_scope, **kwargs):
            callback = kwargs.get('before_write')
            if method not in transport.READ_METHODS:
                if callback is None:
                    raise ValueError('Production write has no original admission')
                def before_write():
                    control = owner.recheck()
                    check_outer_isolation(control, [endpoint])
                    verify_release(root, config_path, manifest_pin)
                    paired.verify_pair(config['observer'])
                    paired.loaded_origins(members)
                    callback()
                    owner.recheck()
                kwargs['before_write'] = before_write
            return original_request(endpoint, server, method, params, current_scope, **kwargs)
        transport.request = owned_request
        guard = codex.build_guard_class(transport.build_guard_class(original.Observer))(config)
        state = Path(config['state'])
        from ccc_herdr_exit_context import ExitContextMonitor
        exit_context_monitor = ExitContextMonitor(state, clock=time.monotonic, provenance={
            'service_release': str(root), 'service_manifest_sha256': manifest_pin,
            'pid': os.getpid(), 'birth': scope.birth(os.getpid()),
            'original_stores': config['original_stores'], 'paired_runtime': str(runtime),
            'paired_manifest_sha256': config['observer']['dependency_manifest_sha256']})
        from ccc_original_host_service import OriginalHostedService
        from ccc_compact_runtime import libraries, control_locks
        original_libraries = libraries()
        def hosted_sources():
            verify_release(root, config_path, manifest_pin)
            paired.verify_pair(config['observer'])
            paired.loaded_origins(members)
        hosted_service = OriginalHostedService(guard, root, owner_recheck=owner.recheck,
            transport=transport, verify_sources=hosted_sources,
            control_scope=lambda target: control_locks(original_libraries, target,
                                                        config_path=Path(config['ccc_config'])))
        from ccc_recovery_progress_runtime import ProgressMonitor
        progress_monitor = ProgressMonitor(state, scope.birth)
        while True:
            cycle_started = time.monotonic()
            try:
                owner.recheck()
                verify_release(root, config_path, manifest_pin)
                paired.verify_pair(config['observer'])
                paired.loaded_origins(members)
                snapshot = guard.capture(state)
                owner.recheck()
                claude_attempts = transport.step(guard, snapshot, state)
                owner.recheck()
                codex.step(guard, snapshot, state)
                owner.recheck()
                snapshot['original_exit_context_capture'] = exit_context_monitor.tick(
                    guard, codex, snapshot, state, owner_recheck=owner.recheck)
                owner.recheck()
                snapshot['original_hosted_exit_recovery'] = hosted_service.tick(snapshot)
                owner.recheck()
                snapshot['codex_recovery_progress'] = progress_monitor.tick(
                    snapshot, claude_attempts=claude_attempts)
                origins = paired.loaded_origins(members)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                # Failure never invokes another send, clears UNKNOWN or rewrites
                # any pending ledger. Only this guard's status is published.
                snapshot = {'agents': [], 'mode': 'held', 'observation_error': str(error)[:240]}
            snapshot.update(schema='ccc-herdr-observation-v174', runtime_schema='ccc-herdr-production-runtime-v187', role=ROLE,
                pid=os.getpid(), at_epoch=time.time(), observed_at_epoch=time.time(), guard_manifest_sha256=manifest_pin,
                guard_source_sha256=sha(regular(root / 'ccc_herdr_production187.py')),
                paired_runtime=str(runtime), paired_manifest_sha256=config['observer']['dependency_manifest_sha256'],
                paired_payloads=len(payload['files']), paired_mac=payload['mac'], loaded_dependencies=origins,
                original_lock_identity=config['original_stores']['lock_identity'],
                original_stores=config['original_stores'], activation_sha256=activation_pin,
                original_guard_pid=(activation.get('old_guard') or {}).get('pid'), all_continuation_rules_active=False,
                client_restart_by_guard=False, standalone_scheduler=False)
            # Work counts toward the five-second cadence. A slow observation
            # never triggers catch-up inputs or skips fresh admission checks.
            elapsed = max(0.0, time.monotonic() - cycle_started)
            next_poll = max(0.25, 5.0 - elapsed)
            snapshot['cycle_metrics'] = {'elapsed_seconds': elapsed,
                'target_interval_seconds': 5.0, 'next_poll_in_seconds': next_poll,
                'observation_held': snapshot.get('mode') == 'held'}
            # Check again before publishing even status through the owned path.
            owner.recheck()
            transport.atomic(state / 'status.json', snapshot)
            if once:
                return snapshot
            time.sleep(next_poll)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--serve', action='store_true')
    parser.add_argument('--activation', type=Path)
    parser.add_argument('--activation-sha256')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.verify_only:
        manifest, config = verify_release(root, args.config, args.manifest_sha256)
        loaded = load_dependencies(root, config, manifest)
        print(json.dumps({'verified': True, 'role': ROLE, 'paired_payloads': len(loaded[2]['files']),
            'loaded_dependencies': loaded[-1], 'state_writes': 0, 'agent_input': 0, 'service_started': False}))
    else:
        if not args.activation or not args.activation_sha256:
            parser.error('Serving requires the exact installer activation receipt')
        snapshot = serve(root, args.config, args.manifest_sha256, args.activation,
                         args.activation_sha256, once=args.once)
        if args.once:
            print(json.dumps({key: snapshot.get(key) for key in
                              ('mode', 'observation_error', 'cycle_metrics', 'input_operations')}))
            if snapshot.get('mode') == 'held':
                raise SystemExit(1)


if __name__ == '__main__':
    main()
