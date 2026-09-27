"""Lifecycle and exact native binding for the opt-in finite API check mode.

The original B and its native provider are unchanged. Only a new access-check
job receives a loopback endpoint. No global credentials, trust or network
configuration is written, and no process is interrupted on API success.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import resource
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from ccc_access_budget import AccessBudget, AdmissionClosed, Policy
from ccc_access_gateway import BatchChannel, Gateway, Upstream

VERSION = 2
MODE = 'sustained-api-check-v2'
LEGACY_MODE = 'finite-api-check-v1'
AUTHORIZATION_LEASE = 1.0
_started_processes = {}


def root(config_path):
    # A new implementation gets a separate listener/owner. Existing native
    # clients keep their frozen port and service; an upgrade never kills an
    # active connection or rebinds an old descriptor to a new transport.
    generation = hashlib.sha256(json.dumps(fingerprint(), sort_keys=True).encode()).hexdigest()
    return Path(config_path).resolve().parent / 'access-gateway' / ('runtime-' + generation)


def job_root(config_path, job_id):
    if str(uuid.UUID(job_id)) != job_id:
        raise ValueError('invalid access job identifier')
    return Path(config_path).resolve().parent / 'workspace-batches' / job_id


def read_private(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_size > limit):
            raise ValueError('access record must be private, owned and bounded')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        after = os.fstat(fd)
        if len(raw) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('access record changed during read')
        return json.loads(raw)
    finally:
        os.close(fd)


def create_private(path, value):
    raw = (json.dumps(value, sort_keys=True) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(fd)
        directory = os.open(Path(path).parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)


def descriptor_sha(value):
    return hashlib.sha256((json.dumps(value, sort_keys=True) + '\n').encode()).hexdigest()


def verify_descriptor(descriptor, config_path, job):
    record = descriptor.get('policy')
    if not isinstance(record, dict):
        raise ValueError('access descriptor has no explicit policy')
    mode, version = descriptor.get('mode'), descriptor.get('version')
    if mode == MODE and type(version) is int and version == VERSION:
        if (set(record) != {'workspace_id', 'job_id', 'slots', 'max_attempts',
                           'max_output_tokens', 'attempt_mode'}
                or record.get('attempt_mode') != 'sustained' or record.get('max_attempts') is not None):
            raise ValueError('sustained access requires an explicit unbounded-attempt policy')
    elif mode == LEGACY_MODE and type(version) is int and version == 1:
        if record.get('attempt_mode', 'finite') != 'finite':
            raise ValueError('legacy finite access cannot become sustained')
    else:
        raise ValueError('unsupported access descriptor mode')
    policy = Policy(**record)
    declared = job.get('access_policy')
    expected = {'mode': mode, 'version': version, 'max_attempts': policy.max_attempts,
                'max_output_tokens': policy.max_output_tokens, 'descriptor_sha256': descriptor_sha(descriptor)}
    if (job.get('access_mode') != mode or declared != expected
            or descriptor.get('config_path') != str(Path(config_path).resolve())
            or policy.job_id != job['id'] or policy.workspace_id != job['workspace_id']
            or not isinstance(declared, dict)):
        raise ValueError('frozen access descriptor does not match its authorized job')
    return policy


def is_access_job(config_path, job):
    """A missing mode field must never turn a check into a real task session."""
    marked = 'access_mode' in job or 'access_policy' in job
    try:
        path = job_root(config_path, job.get('id', '')) / 'access.json'
    except (ValueError, TypeError, AttributeError):
        if marked:
            raise ValueError('access mode has no valid job identity') from None
        return False
    if not marked and not os.path.lexists(path):
        return False
    descriptor = read_private(path)
    verify_descriptor(descriptor, config_path, job)
    return True


def fingerprint(directory=None):
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ('ccc_access_service.py', 'ccc_access_gateway.py', 'ccc_access_budget.py')}


def owner_alive(owner, config_path, *, check_runtime=True):
    from ccc_guard_scope import birth, arguments
    pid = owner.get('pid')
    if not owner.get('birth') or birth(pid) != owner['birth']:
        return False
    try:
        argv, _ = arguments(pid)
        source = owner.get('source_path', str(Path(__file__).resolve()))
        if not isinstance(source, str) or not Path(source).is_absolute() or Path(source).name != 'ccc_access_service.py':
            return False
        expected = ['-B', source, 'serve', '--config', str(Path(config_path).resolve())]
        if argv[1:] != expected or birth(pid) != owner['birth']:
            return False
        # Package and copied watcher runtime share bytes, not a filesystem
        # path. Verify the recorded live argv and both complete fingerprints;
        # never stop/relaunch that owner just because its caller is a copy.
        return not check_runtime or owner.get('source') == fingerprint() == fingerprint(Path(source).parent)
    except (OSError, ValueError, RuntimeError):
        return False


def ping(owner):
    connection = None
    try:
        if (type(owner.get('port')) is not int or not 0 < owner['port'] < 65536
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', owner.get('health_token', ''))):
            return False
        connection = http.client.HTTPConnection('127.0.0.1', owner['port'], timeout=1)
        connection.request('GET', '/health/' + owner['health_token'])
        response = connection.getresponse()
        return response.status == 200 and response.read(1024) == b'{"ready":true}'
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if connection:
            connection.close()


def ensure_gateway(config_path):
    from cmux_codex_watch import FileLock
    directory = root(config_path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise RuntimeError('access service directory is not private and owned')
    with FileLock(directory / 'start.lock', timeout_sec=15):
        try:
            owner = read_private(directory / 'owner.json')
        except FileNotFoundError:
            owner = {}
        if owner_alive(owner, config_path, check_runtime=False):
            if not owner_alive(owner, config_path) or not ping(owner):
                raise RuntimeError('existing access service needs inspection; it was not restarted')
            return owner
        with (directory / 'service.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'serve',
                '--config', str(Path(config_path).resolve())], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, close_fds=True)
            _started_processes[process.pid] = process
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                owner = read_private(directory / 'owner.json')
                if owner.get('pid') == process.pid and owner_alive(owner, config_path) and ping(owner):
                    return owner
            except (OSError, ValueError):
                pass
            if process.poll() is not None:
                raise RuntimeError('access service did not start; original B remains available')
            time.sleep(.05)
        raise RuntimeError('access service startup could not be confirmed; no duplicate launch was issued')


def native_spec(*, fixture=False):
    try:
        import tomllib
    except ImportError:
        raise RuntimeError('the optional finite API check mode requires Python 3.11 or newer') from None
    directory = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    path = directory / 'config.toml'
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('native configuration is too large')
    config = tomllib.loads(raw.decode())
    if config.get('profile') or config.get('profiles'):
        raise ValueError('ambiguous native profile; no API check was started')
    provider = config.get('model_provider', 'openai')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', provider):
        raise ValueError('unsupported provider identifier')
    entry = config.get('model_providers', {}).get(provider, {})
    base = entry.get('base_url') or (os.environ.get('OPENAI_BASE_URL') if provider == 'openai' else '')
    model = config.get('model')
    if not base or not model or entry.get('wire_api', 'responses') != 'responses':
        raise ValueError('finite checks require an explicit Responses provider and model')
    proxy = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY') or ''
    if fixture:
        proxy = ''
    if proxy and (urlsplit(proxy).username is not None or urlsplit(proxy).password is not None):
        raise ValueError('proxy credentials cannot be stored in an access job')
    upstream = Upstream(base, model, proxy_url=proxy, allow_loopback=fixture)
    if fixture and not (upstream.parsed.hostname == '127.0.0.1' and upstream.parsed.scheme == 'http'):
        raise ValueError('a native fixture must have a literal loopback provider')
    return {'base_url': base, 'model': model, 'proxy_url': proxy, 'allow_loopback': fixture,
            'provider': provider, 'native_config_path': str(path),
            'native_config_sha256': hashlib.sha256(raw).hexdigest()}


def prepare(config_path, job, *, fixture=False, owner=None):
    if 'access_mode' in job and job['access_mode'] != MODE:
        raise ValueError('existing batch mode cannot be changed')
    directory = job_root(config_path, job['id'])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec = native_spec(fixture=fixture)
    owner = owner or ensure_gateway(config_path)
    policy = Policy(job['workspace_id'], job['id'], max_attempts=None, attempt_mode='sustained')
    descriptor = {'version': VERSION, 'mode': MODE, 'config_path': str(Path(config_path).resolve()),
        'policy': asdict(policy), 'upstream': spec, 'gateway_instance': owner['instance'],
        'port': owner['port'], 'tokens': [secrets.token_urlsafe(32) for _ in range(50)]}
    create_private(directory / 'access.json', descriptor)
    job['access_mode'] = MODE
    return {'mode': MODE, 'version': VERSION, 'max_attempts': policy.max_attempts,
            'max_output_tokens': policy.max_output_tokens, 'descriptor_sha256': descriptor_sha(descriptor)}


def launch_arguments(config_path, job, index):
    if not is_access_job(config_path, job):
        raise ValueError('native launch has no validated access mode')
    descriptor = read_private(job_root(config_path, job['id']) / 'access.json')
    policy = verify_descriptor(descriptor, config_path, job)
    if not 0 <= index < policy.slots:
        raise ValueError('native access launch belongs to another job')
    spec = descriptor['upstream']
    active_config = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve() / 'config.toml'
    if str(active_config) != spec['native_config_path']:
        raise RuntimeError('native profile location differs from check preparation')
    if hashlib.sha256(Path(spec['native_config_path']).read_bytes()).hexdigest() != spec['native_config_sha256']:
        raise RuntimeError('native configuration changed after check preparation')
    owner = ensure_gateway(config_path)
    if owner['instance'] != descriptor['gateway_instance'] or owner['port'] != descriptor['port']:
        raise RuntimeError('original access gateway changed; this job is not silently rebound')
    endpoint = f"http://127.0.0.1:{owner['port']}/{job['id']}/{index}/{descriptor['tokens'][index]}/v1"
    provider = 'model_providers.' + spec['provider']
    flags = {'model': spec['model'], 'model_provider': spec['provider'],
        provider + '.base_url': endpoint, provider + '.supports_websockets': False,
        'skills.include_instructions': False, 'agents.enabled': False,
        'features.multi_agent': False, 'features.multi_agent_v2': False,
        'features.plugins': False, 'features.apps': False, 'features.hooks': False,
        'features.skip_host_skill_discovery': True, 'project_doc_max_bytes': 0,
        'include_permissions_instructions': False, 'include_collaboration_mode_instructions': False,
        'include_apps_instructions': False}
    if policy.attempt_mode == 'finite':
        # Historical jobs retain their original contract. New N invocations
        # inherit the provider's native HTTP and stream reconnect policy.
        flags.update({provider + '.request_max_retries': 0, provider + '.stream_max_retries': 0})
    return [value for key, val in flags.items() for value in ('-c', key + '=' + json.dumps(val))]


def bind_slot(config_path, job, slot, target, native):
    if not is_access_job(config_path, job):
        raise ValueError('native binding has no validated access mode')
    if (target['workspace_id'] != job['workspace_id'] or target['surface_id'] != slot['surface_id']
            or slot['session_id'] != native['session_id'] or not native.get('pid')
            or not native.get('process_start')):
        raise ValueError('access slot lacks the original native identity')
    value = {'job_id': job['id'], 'workspace_id': job['workspace_id'], 'index': slot['index'],
             'surface_id': slot['surface_id'], 'session_id': native['session_id'],
             'pid': native['pid'], 'process_start': native['process_start'], 'launch_id': slot.get('launch_id')}
    path = job_root(config_path, job['id']) / f"access-session-{slot['index']}.json"
    try:
        create_private(path, value)
    except FileExistsError:
        if read_private(path) != value:
            raise ValueError('access slot cannot be rebound to a different native session')


def status(config_path, job_id):
    try:
        value = read_private(job_root(config_path, job_id) / 'access-status.json')
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def access_binding(config, target):
    rule = next((r for r in config.get('workspace_rules', [])
                 if r.get('workspace_id') == target.get('workspace_id')), {})
    bindings = rule.get('access_check_slots', {})
    return bindings.get(target.get('surface_id')) if isinstance(bindings, dict) else None


def _status_error(value):
    """Only fixed local descriptions can leave a private status record."""
    from ccc_access_gateway import ProtocolFault, error_detail
    if not isinstance(value, dict):
        return {}
    stage = value.get('stage')
    if not isinstance(stage, str) or stage not in {'native_request', 'native_binding', 'native_body', 'budget_reservation',
                     'upstream_connect', 'cohort_dispatch', 'upstream_response',
                     'budget_completion', 'native_response'}:
        stage = 'upstream_response'
    kind = value.get('type')
    if not isinstance(kind, str):
        return {}
    if kind == 'UpstreamRejected':
        code = value.get('http_status')
        if type(code) is int and 100 <= code <= 599:
            return {'type': kind, 'stage': stage, 'http_status': code,
                    'reason': 'API check rejected with HTTP ' + str(code)}
        return {}
    examples = {'ProtocolFault': ProtocolFault(str(value.get('reason', ''))),
                'OSError': OSError(), 'TimeoutError': TimeoutError(),
                'IncompleteReadError': asyncio.IncompleteReadError(b'', 1),
                'LimitOverrunError': asyncio.LimitOverrunError('', 0),
                'ValidationError': ValueError()}
    return error_detail(examples[kind], stage) if kind in examples else {}


def continuation_decision(binding, value, workspace_id, *, now=None):
    """Pure projection shared by the send guard and panel; no inventory scan.

    A transport result is scoped by workspace, job and slot. Terminal titles
    and a completed *launch* never mean that an API request succeeded.
    """
    def result(phase, *, allowed=False, detail=None):
        return {'phase': phase, 'allowed': allowed,
                'alarming': phase in {'missing', 'invalid', 'stale', 'fault', 'uncertain',
                                     'exhausted', 'rejected', 'retryable'},
                **({'error': detail} if detail else {})}

    if not value:
        return result('missing')
    if not isinstance(binding, dict) or not isinstance(value, dict):
        return result('invalid')
    index = binding.get('index')
    if (type(index) is not int or not 0 <= index < 50 or not binding.get('job_id')
            or value.get('job_id') != binding['job_id'] or value.get('workspace_id') != workspace_id):
        return result('invalid')
    attempts, maximum = value.get('attempts'), value.get('max_attempts')
    attempt_mode = value.get('attempt_mode', 'finite')
    valid_attempts = type(attempts) is int and attempts >= 0
    if attempt_mode == 'sustained':
        valid_attempts = valid_attempts and 'max_attempts' in value and maximum is None
    elif attempt_mode == 'finite':
        valid_attempts = valid_attempts and type(maximum) is int and 50 <= maximum <= 10000 and attempts <= maximum
    else:
        valid_attempts = False
    blocked = value.get('blocked_slots')
    if (not valid_attempts or not isinstance(blocked, list)
            or any(type(slot) is not int or not 0 <= slot < 50 for slot in blocked)
            or len(blocked) != len(set(blocked))):
        return result('invalid')
    for key in ('in_flight', 'forwarded', 'complete'):
        counter = value.get(key, 0)
        if type(counter) is not int or not 0 <= counter <= (50 if key != 'forwarded' else attempts):
            return result('invalid')
    first = value.get('first_complete')
    if first is not None and (not isinstance(first, dict) or not isinstance(first.get('response_id'), str)
            or not first['response_id'] or type(first.get('number')) is not int
            or not 1 <= first['number'] <= attempts):
        return result('invalid')
    updated = value.get('updated_at')
    now = time.time() if now is None else now
    if (type(updated) not in (int, float) or not math.isfinite(updated)
            or not 0 <= now - updated <= 3):
        return result('stale')
    slots = value.get('slot_results', {})
    slot = slots.get(str(index), {}) if isinstance(slots, dict) else None
    if not isinstance(slot, dict):
        return result('invalid')
    outcome = slot.get('outcome')
    if (outcome is not None and not isinstance(outcome, str)) or outcome not in {
            None, 'in_flight', 'complete', 'rejected', 'uncertain', 'cancelled_before_dispatch'}:
        return result('invalid')
    if slot and (type(slot.get('attempt')) is not int or not 1 <= slot['attempt'] <= attempts):
        return result('invalid')
    detail = _status_error(slot.get('error'))
    if index in blocked or outcome == 'uncertain':
        return result('uncertain', detail=detail)
    if value.get('fault') or value.get('closed'):
        return result('fault', detail=detail or _status_error(value.get('last_error')))
    if value.get('first_complete'):
        return result('complete' if outcome == 'complete' else
                      'settling' if outcome == 'in_flight' else 'stopped')
    if maximum is not None and attempts >= maximum:
        return result('exhausted', detail=detail)
    if value.get('authorized') is not True:
        return result('paused', detail=detail)
    if outcome == 'complete':
        return result('invalid')  # A success without its batch gate is corrupt.
    if outcome == 'in_flight':
        return result('in_flight')
    if outcome == 'rejected':
        code = detail.get('http_status')
        if code in (429, 500, 502, 503, 504):
            return result('retryable', allowed=True, detail=detail)
        return result('rejected', detail=detail)
    if outcome == 'cancelled_before_dispatch':
        return result('fault', detail=detail)
    return result('ready', allowed=True)


def continuation_allowed(config_path, config, target):
    binding = access_binding(config, target)
    if binding is None:
        return True
    try:
        value = status(config_path, binding['job_id'])
        return continuation_decision(binding, value, target['workspace_id'])['allowed']
    except (KeyError, TypeError, ValueError):
        return False


async def serve(config_path):
    from cmux_codex_watch import ConfigStore, atomic_write_json, load_json
    from ccc_guard_scope import birth
    directory = root(config_path)
    born = birth(os.getpid())
    if not born:
        raise RuntimeError('access service requires exact local process identity support')
    instance, health_token = str(uuid.uuid4()), secrets.token_urlsafe(32)
    owner = {'pid': os.getpid(), 'birth': born, 'instance': instance, 'source': fingerprint(),
             'source_path': str(Path(__file__).resolve())}
    last_success = time.monotonic()
    current = ConfigStore(Path(config_path)).load()
    def authorized(policy):
        nonlocal current
        if not 0 <= time.monotonic() - last_success <= AUTHORIZATION_LEASE:
            return False
        rule = next((r for r in current.get('workspace_rules', []) if r.get('workspace_id') == policy.workspace_id), {})
        return (current.get('mode') == 'armed' and current.get('global_paused') is False
                and rule.get('enabled', True) and not rule.get('paused')
                and rule.get('active_batch_id') == policy.job_id)
    def load_channel(job_id):
        path = job_root(config_path, job_id)
        descriptor = read_private(path / 'access.json')
        job = load_json(path / 'job.json', {})
        policy = verify_descriptor(descriptor, config_path, job)
        if (policy.job_id != job_id
                or descriptor.get('config_path') != str(Path(config_path).resolve())
                or descriptor.get('gateway_instance') != instance or descriptor.get('port') != owner['port']):
            raise AdmissionClosed('this job is not bound to the current access service')
        spec = descriptor['upstream']
        upstream = Upstream(**{k: spec[k] for k in ('base_url', 'model', 'proxy_url', 'allow_loopback')})
        # An existing journal is always replayed. Missing/pending completion
        # cannot refund attempts or undo a success gate after a process crash.
        journal = path / 'access-journal.jsonl'
        try:
            budget = AccessBudget(journal, policy, create=True)
        except FileExistsError:
            budget = AccessBudget(journal, policy)
        def session(slot):
            binding = read_private(path / f'access-session-{slot}.json')
            if (binding['job_id'] != job_id or binding['workspace_id'] != policy.workspace_id
                    or binding['index'] != slot):
                raise AdmissionClosed('main native session binding changed')
            return binding['session_id']
        return BatchChannel(budget, upstream, descriptor['tokens'], binding_loader=session,
                            dispatch_check=lambda: authorized(policy))
    gateway = Gateway(channel_loader=load_channel, health_token=health_token)
    owner.update(port=await gateway.start(), health_token=health_token)
    atomic_write_json(directory / 'owner.json', owner)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    policy_reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ccc-access-policy')

    async def refresh_authorization():
        nonlocal current, last_success
        while not stop.is_set():
            load_started = time.monotonic()
            try:
                current = await loop.run_in_executor(policy_reader, ConfigStore(Path(config_path)).load)
                # Queueing and read time consume the lease too. A delayed
                # load cannot turn an old authorization into a fresh one.
                last_success = load_started
            except (OSError, ValueError, RuntimeError):
                current = {}
            try:
                await asyncio.wait_for(stop.wait(), .25)
            except asyncio.TimeoutError:
                pass

    refreshing = asyncio.create_task(refresh_authorization())
    try:
        while not stop.is_set():
            snapshots = [(job, {**channel.snapshot(), 'updated_at': time.time(),
                          'instance': instance, 'authorized': bool(authorized(channel.budget.policy))})
                         for job, channel in tuple(gateway.channels.items())]
            def publish():
                for job, value in snapshots:
                    atomic_write_json(job_root(config_path, job) / 'access-status.json', value)
            await gateway.storage(publish)
            try:
                await asyncio.wait_for(stop.wait(), .25)
            except asyncio.TimeoutError:
                pass
    finally:
        stop.set()
        refreshing.cancel()
        await asyncio.gather(refreshing, return_exceptions=True)
        await gateway.close()
        policy_reader.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve'])
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    from cmux_codex_watch import FileLock
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    desired = min(16384, hard) if hard != resource.RLIM_INFINITY else 16384
    if soft < desired:
        resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    with FileLock(root(args.config) / 'owner.lock', timeout_sec=0):
        asyncio.run(serve(args.config.resolve()))


if __name__ == '__main__':
    main()
