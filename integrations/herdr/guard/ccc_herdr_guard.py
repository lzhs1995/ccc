"""Herdr-specific transport around the original CCC reader and continuation rules.

The selected server/pane/process generation and original native turn are checked
again immediately before one API submission. Herdr 0.9.5 has no expected-PID or
composer CAS; the transport records that limitation and never retries an unknown
write, presses a recovery Enter, changes provider settings or restarts an agent.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True

import ccc_herdr_lifecycle as life

READ_METHODS = frozenset({'session.snapshot', 'pane.process_info', 'pane.read'})
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
PROTECTED_PID = PUBLIC_PROTECTED_PID
PROTECTED_SURFACE = PUBLIC_PROTECTED_SURFACE


def discovery_directory(path):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or path != path.resolve(strict=True)
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError('Herdr discovery directory is not canonical and owner-controlled')
    return path


def discovery_candidates(root, *, limit=128):
    """Find only owner-controlled private sockets; never .sock.agent links."""
    root = discovery_directory(root)
    candidates, refusals = [('default', root / 'herdr.sock')], []
    sessions = root / 'sessions'
    if not sessions.exists():
        return candidates, refusals
    discovery_directory(sessions)
    with os.scandir(sessions) as directory:
        names = []
        for item in directory:
            if len(names) >= limit:
                raise ValueError('Herdr session directory count exceeds discovery bound')
            names.append(item.name)
    for name in sorted(names):
        if (name == 'default' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name)):
            refusals.append({'session': name[:64], 'reason': 'noncanonical_session_name'})
            continue
        path = sessions / name
        try:
            discovery_directory(path)
        except (OSError, ValueError) as error:
            refusals.append({'session': name, 'reason': str(error)[:160]})
            continue
        candidates.append((name, path / 'herdr.sock'))
    return candidates, refusals


def _allowed_herdr_binaries(observer):
    allowed = {observer.binary}
    brew_link = Path('/opt/homebrew/bin/herdr')
    try:
        if brew_link.exists():
            allowed.add(brew_link.resolve(strict=True))
    except OSError:
        pass
    return allowed


def discovery_endpoint(observer, session, source, *, read_only=False):
    """Read the actual peer's native placement, sending zero application bytes.

    The normal capture independently reauthenticates that peer, its live cmux
    membership and each inner foreground process. Discovery never supplies a
    selected agent thread or enrolls collaboration.
    """
    source = Path(source)
    before = source.lstat()
    if (source != source.resolve(strict=True) or not stat.S_ISSOCK(before.st_mode)
            or before.st_uid != os.getuid()):
        raise ValueError('Discovery refuses a link, foreign or non-socket endpoint')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(str(source))
        pid = int.from_bytes(connection.getsockopt(0, 2, 4), sys.byteorder, signed=True)
        process = observer.process(pid)
        if (pid <= 0 or process.get('pid') != pid
                or process.get('argv', [])[1:] != ['server']
                or not process.get('argv') or Path(process['argv'][0]).name != 'herdr'
                or Path(process['executable']).resolve(strict=True) not in _allowed_herdr_binaries(observer)
                or observer.scope.birth(pid) != process['birth']):
            raise ValueError('Discovery peer is not the pinned native Herdr server')
        environment = process['environment']
        if environment.get('HERDR_SESSION', 'default') != session:
            raise ValueError('Private socket session disagrees with its native peer')
        placement = {}
        for field in ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID'):
            value = environment.get(field)
            if not isinstance(value, str) or str(uuid.UUID(value)).upper() != value:
                raise ValueError('Discovery peer has no canonical native cmux placement')
            placement[field] = value
        if pid == PROTECTED_PID or placement['CMUX_SURFACE_ID'] == PROTECTED_SURFACE:
            raise ValueError('Protected surface is outside Herdr discovery scope')
        after = source.lstat()
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or observer.scope.birth(pid) != process['birth']):
            raise ValueError('Discovery socket or native peer changed during observation')
    return {'session': session, 'socket': str(source),
            'cmux_surface_id': placement['CMUX_SURFACE_ID'],
            'cmux_workspace_id': placement['CMUX_WORKSPACE_ID'],
            'discovered_read_only': bool(read_only), 'discovery_source': 'owned_private_socket_native_peer'}


def discover_endpoints(observer, registered, settings):
    started = time.monotonic()
    input_mode = settings.get('input_mode', 'armed')
    read_only = input_mode != 'armed'
    report = {'schema': 'ccc-herdr-private-discovery-r35', 'mode': input_mode,
              'observed_at': time.time(), 'registered_sessions': [e['session'] for e in registered],
              'registered_endpoints': len(registered), 'new_endpoints': [], 'refusals': [],
              'application_input_operations': 0, 'collaboration_enrollments': 0}
    if not settings.get('enabled'):
        report.update(status='disabled', discovery_enabled=False)
        return list(registered), report
    if input_mode not in {'read_only', 'armed'}:
        raise ValueError('Automatic discovery input_mode is invalid')
    bound = settings.get('max_endpoints', 64)
    budget = settings.get('scan_budget_sec', 5.0)
    if (type(bound) is not int or not 1 <= bound <= 128
            or type(budget) not in (float, int) or not 0 < budget <= 10):
        raise ValueError('Discovery endpoint or time bound is invalid')
    candidates, report['refusals'] = discovery_candidates(settings['root'])
    registered_names = {e['session'] for e in registered}
    registered_sockets = {e['socket'] for e in registered}
    discovered = []
    report.update(discovery_enabled=True, status='complete', socket_candidates=len(candidates))
    for session, source in candidates:
        if session in registered_names or str(source) in registered_sockets:
            continue
        if time.monotonic() - started >= budget or len(registered) + len(discovered) >= bound:
            report.update(status='bounded', remaining_candidates_not_observed=True)
            break
        try:
            endpoint = discovery_endpoint(observer, session, source, read_only=read_only)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            report['refusals'].append({'session': session, 'reason': str(error)[:160]})
            continue
        discovered.append(endpoint)
    report['new_endpoints'] = discovered
    report['discovered_endpoints'] = len(discovered)
    report['elapsed_seconds'] = time.monotonic() - started
    return list(registered) + discovered, report


def atomic(path, value):
    path = Path(path)
    raw = life.canonical(value) + b'\n'
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def request(endpoint, expected_server, method, params, scope, *, before_write=None):
    """Use the selected private server socket; never the shared .sock.agent link."""
    if method not in READ_METHODS and endpoint.get('discovered_read_only') is True:
        raise ValueError('Discovered endpoint is monitored without input authorization')
    if method not in READ_METHODS and not (method in {'agent.prompt', 'pane.send_input'} and before_write):
        raise ValueError('Herdr method is outside the guarded allowlist')
    source = Path(endpoint['socket'])
    info = source.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) !=
                (expected_server['socket_device'], expected_server['socket_inode'])):
        raise ValueError('Selected private server socket changed')
    marker = 'ccc-herdr-' + uuid.uuid4().hex
    raw = life.canonical({'id':marker, 'method':method, 'params':params}) + b'\n'
    @contextmanager
    def checked_connection():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(source))
            pid = int.from_bytes(connection.getsockopt(0, 2, 4), sys.byteorder, signed=True)
            if pid != expected_server['pid'] or scope.birth(pid) != expected_server['birth']:
                raise ValueError('Selected private server peer changed')
            current = source.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('Selected private socket replaced before request')
            yield connection
    if before_write is not None:
        # Herdr 0.9.5 closes an idle connection after five seconds. Authenticate
        # a zero-payload probe, close it, then do native admission and durable
        # intent work. Reauthenticate a fresh peer for the sole payload request.
        # Failure after the durable intent stays UNKNOWN; there is no retry.
        with checked_connection():
            pass
        before_write()
    with checked_connection() as connection:
        connection.sendall(raw)
        chunks, length = [], 0
        while True:
            chunk = connection.recv(min(65536, 2 * 1024 * 1024 + 1 - length))
            if not chunk:
                raise ValueError('Herdr response ended before its complete envelope')
            chunks.append(chunk)
            length += len(chunk)
            if length > 2 * 1024 * 1024:
                raise ValueError('Herdr response exceeds bound')
            if b'\n' in chunk:
                break
    data = b''.join(chunks)
    line, remainder = data.split(b'\n', 1)
    if remainder.strip():
        raise ValueError('Herdr returned multiple response envelopes')
    response = json.loads(line)
    if not isinstance(response, dict) or response.get('id') != marker:
        raise ValueError('Herdr response identity changed')
    return response


def endpoint_for(config, session):
    matches = [e for e in config['endpoints'] if e['session'] == session]
    if len(matches) != 1:
        raise ValueError('Original Herdr endpoint is missing or ambiguous')
    return matches[0]


ZERO_INPUT_AGENT_REFUSALS = frozenset({
    'agent_blocked', 'agent_not_found', 'agent_not_ready', 'empty_agent_prompt'})


def zero_input_refusal(attempt, response):
    """Only agent.prompt's documented pre-input refusals prove no input."""
    error = response.get('error') if isinstance(response, dict) else None
    method = attempt.get('last_wire_method')
    if method is None:
        # Old releases did not tag the last route and treated pane errors as
        # agent refusals. Such a two-wire record must not gain replay rights.
        if attempt.get('agent_prompt_refusal') or attempt.get('wire_requests', 1) != 1:
            return False
        method = 'agent.prompt'
    return (isinstance(response, dict)
            and method == 'agent.prompt'
            and 'result' not in response
            and isinstance(error, dict) and error.get('code') in ZERO_INPUT_AGENT_REFUSALS)


def retryable_zero_input(attempt):
    if not attempt or attempt.get('input_operations') != 0:
        return False
    if attempt.get('phase') == 'prewrite_refused':
        return (attempt.get('wire_requests', 0) == 0
                and not attempt.get('provider_reservation_uncertain')
                and not attempt.get('repair_finalization_unknown'))
    return (attempt.get('phase') == 'host_refused'
            and zero_input_refusal(attempt, attempt.get('host_response'))
            and not attempt.get('provider_reservation_uncertain')
            and not attempt.get('repair_finalization_unknown'))


def same_generation(attempt, row):
    binding = attempt.get('binding') or {}
    return all(binding.get(k) == row.get(k) for k in ('pid', 'birth', 'server', 'terminal_id'))


def preserve_refused_attempt(attempts, key):
    """Retain the original refusal when its unused logical intent is retried."""
    if key in attempts:
        attempts[key + ':refused:' + uuid.uuid4().hex] = attempts[key]


def single_input_wire_proven(attempt):
    """Two wire calls still mean one input only with a bound refusal chain."""
    if attempt.get('input_operations') != 1:
        return False
    if attempt.get('wire_requests') == 1:
        return True  # Existing original-attempt/native-receipt checks still apply.
    wires = attempt.get('wire_subattempts')
    if (attempt.get('wire_requests') != 2 or not isinstance(wires, list)
            or len(wires) != 2 or not all(isinstance(w, dict) for w in wires)):
        return False
    first, second = wires
    try:
        pane = attempt['binding']['pane_id']
        params = ({'target': pane, 'text': attempt['message']},
                  {'pane_id': pane, 'text': attempt['message'], 'keys': ['enter']})
        for index, (wire, expected) in enumerate(zip(wires, params), 1):
            if (wire.get('schema') != 'herdr-original-wire-intent-v1'
                    or not isinstance(wire.get('wire_intent_id'), str)
                    or not re.fullmatch(r'[0-9a-f]{32}', wire['wire_intent_id'])
                    or wire.get('ordinal') != index or wire.get('parent_intent_id') != attempt['intent_id']
                    or wire.get('pane_id') != pane or wire.get('socket') != attempt['binding']['socket']
                    or wire.get('binding_sha256') != life.digest(life.canonical(attempt['binding']))
                    or wire.get('params') != expected
                    or wire.get('params_sha256') != life.digest(life.canonical(expected))
                    or wire.get('wire_requests') != 1):
                return False
        refusal = first.get('host_response')
        return (first.get('method') == 'agent.prompt' and first.get('phase') == 'host_refused'
                and first.get('input_operations') == 0
                and zero_input_refusal({'last_wire_method': 'agent.prompt'}, refusal)
                and refusal['error']['code'] == 'agent_not_found'
                and first.get('wire_intent_id') != second.get('wire_intent_id')
                and second.get('method') == 'pane.send_input'
                and second.get('input_operations') == 1
                and second.get('phase') == 'host_accepted'
                and second.get('host_response') == attempt.get('host_response'))
    except (KeyError, TypeError):
        return False


def submit_with_fallback(endpoint, server, pane_id, message, scope, attempt,
                         persist, before_first_write, revalidate_fallback):
    """Each wire has its own fresh admission and durable prospective count.

    A missing agent registration permits a pane route only after the first
    authoritative zero-input response is durable. Unknown outcomes never fall
    back. Herdr still lacks composer/PID CAS, so checks cannot exclude typing
    that races the final checked connection.
    """
    # This helper is for a new original intent, never recovery of an UNKNOWN
    # wire. A retry of a proven refusal receives its own attempt object; its
    # predecessor remains in the original ledger under preserve_refused_attempt.
    if (attempt.get('phase') != 'intent' or attempt.get('input_operations') != 0
            or attempt.get('wire_requests', 0) != 0 or attempt.get('wire_subattempts')):
        raise ValueError('Original wire attempt already started; no replay')
    wires = attempt['wire_subattempts'] = []

    def new_wire(method, params):
        item = {'schema': 'herdr-original-wire-intent-v1',
                'wire_intent_id': uuid.uuid4().hex, 'parent_intent_id': attempt['intent_id'],
                'ordinal': len(wires) + 1, 'method': method, 'params': params,
                'params_sha256': life.digest(life.canonical(params)),
                'socket': endpoint['socket'], 'pane_id': pane_id,
                'binding_sha256': life.digest(life.canonical(attempt['binding'])),
                'phase': 'intent', 'input_operations': 0, 'wire_requests': 0,
                'created_at': time.time()}
        wires.append(item)
        persist()
        return item

    def record_response(item, response):
        item['host_response'] = response
        item['response_at'] = time.time()
        # A contradictory or incomplete envelope is not a zero-input refusal.
        # request() already authenticated the response ID and private peer.
        if (not isinstance(response, dict)
                or ('error' in response) == ('result' in response)):
            item['phase'] = 'write_unknown'
            persist()
            raise ValueError('Herdr response outcome is ambiguous; no replay')
        refused = zero_input_refusal({'last_wire_method': item['method']}, response)
        item['phase'] = ('host_refused' if refused else
                         'write_unknown' if 'error' in response else 'host_accepted')
        if refused:
            item['input_operations'] = 0
        persist()

    first = new_wire('agent.prompt', {'target': pane_id, 'text': message})
    attempt['last_wire_method'] = 'agent.prompt'

    def before_agent_write():
        before_first_write()
        first.update(phase='write_unknown', input_operations=1, wire_requests=1,
                     write_started_at=time.time())
        persist()

    response = request(endpoint, server, 'agent.prompt', first['params'],
                       scope, before_write=before_agent_write)
    record_response(first, response)
    if not (zero_input_refusal(attempt, response)
            and response['error']['code'] == 'agent_not_found'):
        return response
    attempt.update(agent_prompt_refusal=response, host_response=response,
                   phase='host_refused', input_operations=0)
    persist()
    fallback = new_wire('pane.send_input',
                        {'pane_id': pane_id, 'text': message, 'keys': ['enter']})

    def before_fallback_write():
        revalidate_fallback()
        attempt.update(phase='write_unknown', input_operations=1,
                       wire_requests=attempt.get('wire_requests', 1) + 1,
                       last_wire_method='pane.send_input', write_started_at=time.time())
        fallback.update(phase='write_unknown', input_operations=1, wire_requests=1,
                        write_started_at=attempt['write_started_at'])
        persist()

    try:
        result = request(endpoint, server, 'pane.send_input', fallback['params'],
                         scope, before_write=before_fallback_write)
        record_response(fallback, result)
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration,
            ImportError, sqlite3.Error, subprocess.SubprocessError) as error:
        if attempt['phase'] != 'host_refused':
            raise  # Prospective fallback write is durable: preserve UNKNOWN.
        fallback.update(phase='prewrite_refused', reason=str(error)[:240])
        attempt['fallback_refused_reason'] = str(error)[:240]
        persist()
        return response  # Proven first refusal, zero second payloads.


def policy(config, endpoint):
    if endpoint.get('discovered_read_only') is True:
        return False, 'discovered_endpoint_monitor_only'
    if config.get('mode') != 'armed':
        return False, 'global_mode_not_armed'
    if config.get('global_paused') or not config.get('claude_enabled', True):
        return False, 'global_pause_or_claude_disabled'
    if endpoint['cmux_workspace_id'] in config.get('claude_excluded_workspace_ids', []):
        return False, 'workspace_excluded'
    for rule in config.get('workspace_rules', []):
        if rule.get('workspace_id') == endpoint['cmux_workspace_id'] and (
                rule.get('paused') or rule.get('enabled', True) is not True):
            return False, 'workspace_paused_or_disabled'
    # V172 excluded the *outer* transport to prevent an outer-window paste.
    # That isolation is not an authorization to touch another inner panel.
    return True, None


def generation(endpoint, pane, process, server):
    return {'socket':endpoint['socket'], 'session':endpoint['session'],
            'pane_id':pane['pane_id'], 'terminal_id':pane['terminal_id'],
            'pid':process['pid'], 'birth':process['birth'], 'server':server,
            'cmux_workspace_id':endpoint['cmux_workspace_id'],
            'cmux_surface_id':endpoint['cmux_surface_id']}


def verify_membership(observer, endpoint, server):
    members = observer.cmux_membership(endpoint)
    if observer.server(endpoint, members) != server:
        raise ValueError('Original server or current outer cmux membership changed')


def verify_original(observer, endpoint, pane, process, server, journal, proof, config_path):
    """Re-resolve the current pane; old snapshot nodes cannot select a new agent."""
    if process['pid'] == PROTECTED_PID or endpoint['cmux_surface_id'] == PROTECTED_SURFACE:
        raise ValueError('Protected session is outside all Herdr input scope')
    verify_membership(observer, endpoint, server)
    snapshot = observer.cli(endpoint['session'], ['api','snapshot'])['snapshot']
    matches = [p for p in snapshot['panes'] if p['pane_id'] == pane['pane_id']]
    if (len(matches) != 1 or matches[0].get('terminal_id') != pane['terminal_id']
            or matches[0].get('agent') != 'claude'
            or any(matches[0].get(k) != pane.get(k) for k in ('workspace_id','tab_id'))):
        raise ValueError('Original Herdr pane or terminal changed')
    current = observer.foreground(endpoint, matches[0], server)
    if any(current.get(k) != process.get(k) for k in
           ('pid','birth','pgid','tty','argv','environment','executable')):
        raise ValueError('Original Herdr foreground process changed')
    if not journal.refresh():
        raise ValueError('Original native journal is currently unavailable')
    row, reason = journal.lookup(endpoint, current)
    if row is None:
        raise ValueError(reason)
    view, fresh_proof = life.native_observation(row, current)
    if proof != fresh_proof:
        raise ValueError('Original native event or transcript changed')
    text = observer.cli(endpoint['session'], ['pane','read',pane['pane_id'],
                                             '--source','visible','--lines','160'])['text']
    config = json.loads(Path(config_path).read_bytes())
    enabled, reason = policy(config, endpoint)
    allowed, reason = life.admission(view, text, enabled=enabled)
    if not allowed:
        raise ValueError(reason)
    verify_membership(observer, endpoint, server)
    after = observer.foreground(endpoint, matches[0], server)
    if any(after.get(k) != process.get(k) for k in
           ('pid','birth','pgid','tty','argv','environment','executable')):
        raise ValueError('Original foreground changed during final input checks')
    if not journal.refresh():
        raise ValueError('Original native journal changed during final input checks')
    latest, reason = journal.lookup(endpoint, after)
    if latest is None or life.native_observation(latest, after)[1] != proof:
        raise ValueError(reason or 'Original native source changed during final input checks')
    return view, text, config


def receipt(intent):
    """Only a fresh original mainline user message proves receiving our prompt."""
    import ccc_claude_closeout as closeout
    import claude_dispatch_recovery as dispatch
    source = intent['native_proof']['native_boundary']
    path = Path(source['path'])
    raw, current = closeout.native_tail(path)
    if current['stat'][:2] != source['stat'][:2] or current['stat'][2] < source['stat'][2]:
        raise ValueError('Original transcript replaced or truncated after submission')
    offset = current['offset_bytes']
    for line in raw.splitlines(keepends=True):
        line_offset = offset
        offset += len(line)
        if line_offset < source['stat'][2]:
            continue
        row = json.loads(line)
        if (row.get('type') == 'user' and row.get('isSidechain') is False
                and row.get('sessionId') == intent['session_id']
                and not any(row.get(k) for k in ('isMeta','isSynthetic','isCompactSummary'))
                and isinstance(row.get('message'), dict)
                and row['message'].get('role') == 'user'
                and dispatch._text(row) == intent['message']):
            return {'schema':'herdr-native-user-receipt-v174',
                    'transcript_path':str(path), 'device_inode':current['stat'][:2],
                    'offset':line_offset, 'bytes':len(line), 'sha256':life.digest(line),
                    'native_uuid':row.get('uuid'), 'native_timestamp':row.get('timestamp')}
    return None


def native_terminal_name(observer, process):
    """Resolve the controlling PTY by kernel device identity, without ps.

    The selected process, foreground group and parent chain are still checked
    by the original reader. A device name is only the native TTY locator; it
    neither selects a Codex thread nor permits input.
    """
    pid, birth = process['pid'], process['birth']

    def kernel():
        info = observer.native_info(pid)
        actual = [info.start_sec, info.start_usec]
        if (actual != birth or info.ids[0] != os.getuid()
                or info.misc[1] != process['pgid']
                or info.misc[4] != process['pgid']
                or info.misc[3] in (0, 0xffffffff)):
            raise RuntimeError('Native controlling terminal or foreground group changed')
        return (info.pid, tuple(actual), info.ids[0], info.misc[1],
                info.misc[3], info.misc[4])

    before = kernel()
    matches = []
    with os.scandir('/dev') as devices:
        for count, device in enumerate(devices, 1):
            if count > 4096:
                raise RuntimeError('Native terminal device inventory exceeds bound')
            if not re.fullmatch(r'ttys\d+', device.name):
                continue
            info = device.stat(follow_symlinks=False)
            if stat.S_ISCHR(info.st_mode) and info.st_rdev == before[4]:
                matches.append((device.name, info))
    if len(matches) != 1:
        raise RuntimeError('Native terminal device is missing or ambiguous')
    name, device = matches[0]
    current = os.stat('/dev/' + name, follow_symlinks=False)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mode,
                              value.st_rdev, value.st_uid, value.st_gid)
    if (identity(current) != identity(device) or kernel() != before
            or observer.scope.birth(pid) != birth):
        raise RuntimeError('Native terminal identity changed during observation')
    return name


def terminal_program_observation(observer, endpoint, pane, server):
    """Distinguish a shell foreground from an unverified agent reader.

    These are native program/placement facts only. A shell foreground cannot
    prove that a saved Codex SID has no writer elsewhere, that its task should
    resume, or that the shell's line editor is empty. Never grant input here.
    """
    now = time.time()
    result = {'schema': 'ccc-herdr-terminal-program-r38', 'mode': 'read_only',
              'phase': 'unknown', 'observed_at': now,
              'native_thread_authority': False, 'control_verified': False,
              'recovery_available': False, 'input_operations': 0}
    try:
        from ccc_herdr_observer import placement_matches
        if endpoint['cmux_surface_id'] == PROTECTED_SURFACE:
            raise ValueError('Protected surface is outside terminal observation')
        fields = ('pane_id', 'terminal_id', 'workspace_id', 'tab_id')
        binding = {name: pane[name] for name in fields}
        if not all(isinstance(value, str) and value for value in binding.values()):
            raise ValueError('Current inner terminal placement is incomplete')
        binding.update(key=endpoint['socket'] + '#' + pane['pane_id'],
            session=endpoint['session'], server=server,
            cmux_workspace_id=endpoint['cmux_workspace_id'],
            cmux_surface_id=endpoint['cmux_surface_id'])
        def read_view():
            response = request(endpoint, server, 'pane.process_info',
                               {'pane_id': pane['pane_id']}, observer.scope)
            if 'error' in response or not isinstance(response.get('result'), dict):
                raise ValueError('Native terminal process observation refused')
            view = response['result']['process_info']
            entries = view.get('foreground_processes')
            if (view.get('pane_id') != pane['pane_id']
                    or not isinstance(entries, list) or not 1 <= len(entries) <= 64
                    or type(view.get('shell_pid')) is not int or view['shell_pid'] <= 0
                    or type(view.get('foreground_process_group_id')) is not int
                    or view['foreground_process_group_id'] <= 0
                    or any(not isinstance(item, dict) or type(item.get('pid')) is not int
                           or item['pid'] <= 0 or item['pid'] == PROTECTED_PID for item in entries)
                    or len({item['pid'] for item in entries}) != len(entries)):
                raise ValueError('Native terminal foreground is empty or ambiguous')
            return view
        first = read_view()
        processes = [observer.process(item['pid']) for item in first['foreground_processes']]
        terminals = []
        for process in processes:
            environment = process['environment']
            placement = placement_matches(endpoint, pane, environment)
            if process.get('agent_kind') not in {'codex', 'claude'}:
                # A Herdr-owned login shell can lack every integration variable.
                # Authenticate it with the actual server/pane, process ancestry
                # and kernel controlling PTY. Present conflicting hints refuse.
                expected = {'HERDR_ENV': '1', 'HERDR_SOCKET_PATH': endpoint['socket'],
                    'HERDR_PANE_ID': pane['pane_id'], 'HERDR_WORKSPACE_ID': pane['workspace_id'],
                    'HERDR_TAB_ID': pane['tab_id'], 'HERDR_SESSION': endpoint['session'],
                    'CMUX_WORKSPACE_ID': endpoint['cmux_workspace_id'],
                    'CMUX_SURFACE_ID': endpoint['cmux_surface_id']}
                placement = all(name not in environment or environment[name] == value
                                for name, value in expected.items())
            if process['pgid'] != first['foreground_process_group_id'] or not placement:
                raise ValueError('Native foreground placement or process group changed')
            current = process['pid']
            for _ in range(8):
                if current == server['pid']:
                    break
                current = observer.native_info(current).ppid
            else:
                raise ValueError('Terminal program is not a child of the original server')
            terminals.append(native_terminal_name(observer, process))
        if len(set(terminals)) != 1:
            raise ValueError('Foreground programs disagree on the controlling terminal')
        second = read_view()
        if (first['shell_pid'] != second['shell_pid']
                or first['foreground_process_group_id'] != second['foreground_process_group_id']
                or sorted(item['pid'] for item in first['foreground_processes'])
                   != sorted(item['pid'] for item in second['foreground_processes'])):
            raise ValueError('Current terminal foreground changed during observation')
        later = request(endpoint, server, 'session.snapshot', {}, observer.scope)
        if 'error' in later or not isinstance(later.get('result'), dict):
            raise ValueError('Current terminal placement recheck refused')
        snapshot = later['result']['snapshot']
        if (snapshot.get('protocol') != 22 or snapshot.get('version') != '0.9.5'
                or not isinstance(snapshot.get('panes'), list) or len(snapshot['panes']) > 32):
            raise ValueError('Current terminal snapshot shape changed')
        matches = [item for item in snapshot['panes'] if item.get('pane_id') == pane['pane_id']]
        if (len(matches) != 1 or any(matches[0].get(name) != pane[name] for name in fields)
                or observer.scope.birth(server['pid']) != server['birth']
                or any(observer.process(process['pid']) != process for process in processes)):
            raise ValueError('Original terminal or native program generation changed')
        shells = {'/bin/zsh': 'zsh', '/bin/bash': 'bash', '/bin/sh': 'sh'}
        single = processes[0] if len(processes) == 1 else None
        shell = bool(single and single['pid'] == first['shell_pid']
                     and single['executable'] in shells and single['argv']
                     and Path(single['argv'][0].lstrip('-')).name == shells[single['executable']]
                     and all(option in {'-l', '-i', '-il', '-li', '--login'}
                             for option in single['argv'][1:]))
        agents = [process for process in processes if process.get('agent_kind') in {'codex', 'claude'}]
        phase = 'shell_foreground' if shell else 'agent_foreground' if len(agents) == 1 else 'program_foreground'
        result.update(phase=phase, binding=binding, tty=terminals[0],
            processes=[{'pid': process['pid'], 'birth': process['birth'],
                        'executable': process['executable'], 'agent_kind': process.get('agent_kind')}
                       for process in processes],
            shell_pid=first['shell_pid'], foreground_pgid=first['foreground_process_group_id'],
            reason='shell_foreground_original_resume_not_authorized' if shell else
                   'program_observation_is_not_current_thread_proof')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError,
            subprocess.SubprocessError) as error:
        result.update(phase='unknown', reason=str(error)[:160])
    return result



R40_GOAL_STATES = frozenset({'active', 'blocked', 'paused', 'complete', 'budget_limited', 'usage_limited'})
R40_EXIT_REQUIREMENTS = ('explicit_original_respawn_policy', 'authentic_saved_launch_context', 'original_input_line_empty_proof', 'fresh_no_writer_proof', 'one_use_original_controller_intent', 'native_resumed_sid_receipt')

def r40_canonical_sid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Original SID must be one canonical UUID')
    return value

def r40_file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_uid, info.st_mode)

def r40_owned_regular(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Original evidence path must be absolute and canonical')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 18:
        raise ValueError('Original evidence must be an owned regular file')
    return info

def r40_original_goal(home, sid, *, timeout=0.5, clock=time.monotonic):
    """Read the actual native schema, retaining refusal separately from absence.

    The Goal is task status metadata for the saved SID, never proof of the
    current selected thread. An incompatible/missing schema is UNKNOWN.
    """
    sid = r40_canonical_sid(sid)
    path = Path(home) / 'goals_1.sqlite'
    result = {'source': 'native_thread_goals_readonly', 'session_id': sid, 'status': 'unknown', 'current_thread_authority': False, 'goal_writes': 0}
    try:
        before = r40_owned_regular(path)
        deadline = clock() + timeout
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=min(timeout, 0.15))) as db:
            db.set_progress_handler(lambda: int(clock() >= deadline), 1000)
            db.execute('PRAGMA query_only=ON')
            db.row_factory = sqlite3.Row
            table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='thread_goals'").fetchone()
            if table is None:
                raise ValueError('Native thread_goals schema is unavailable')
            columns = {row[1] for row in db.execute('PRAGMA table_info(thread_goals)')}
            names = ('thread_id', 'goal_id', 'status', 'created_at_ms', 'updated_at_ms', 'token_budget', 'tokens_used', 'time_used_seconds')
            if not set(names) <= columns:
                raise ValueError('Native Goal columns are unavailable')
            rows = db.execute('SELECT ' + ','.join(names) + ' FROM thread_goals WHERE thread_id=? LIMIT 2', (sid,)).fetchall()
            if clock() >= deadline:
                raise ValueError('Native Goal observation exceeded its deadline')
        after = r40_owned_regular(path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError('Native Goal database was replaced during observation')
        if len(rows) > 1:
            raise ValueError('Original Goal is ambiguous')
        if not rows:
            return dict(result, status='absent', database_file_id=[after.st_dev, after.st_ino])
        row = dict(rows[0])
        if row['thread_id'] != sid or row['status'] not in R40_GOAL_STATES or (not isinstance(row['goal_id'], str)) or (not row['goal_id']) or any((type(row[k]) is not int or row[k] < 0 for k in ('created_at_ms', 'updated_at_ms', 'tokens_used', 'time_used_seconds'))) or (row['updated_at_ms'] < row['created_at_ms']) or (row['token_budget'] is not None and (type(row['token_budget']) is not int or row['token_budget'] <= 0)):
            raise ValueError('Original native Goal has invalid fields')
        return dict(result, **row, database_file_id=[after.st_dev, after.st_ino])
    except (OSError, ValueError, sqlite3.Error) as error:
        return dict(result, reason=str(error)[:240])

def r40_original_history(path, sid, *, tail_limit=256 * 1024):
    """Freeze a bounded header/EOF witness; never select a thread by mtime."""
    sid = r40_canonical_sid(sid)
    path = Path(path)
    before = r40_owned_regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if r40_file_identity(os.fstat(stream.fileno())) != r40_file_identity(before):
            raise ValueError('Original history changed before observation')
        header = stream.readline(256 * 1024)
        if not header.endswith(b'\n'):
            raise ValueError('Original history header is incomplete')
        metadata = json.loads(header)
        if (not isinstance(metadata, dict) or metadata.get('type') != 'session_meta'
                or not isinstance(metadata.get('payload'), dict)
                or metadata['payload'].get('id') != sid):
            raise ValueError('Original history SID mismatch')
        start = max(0, before.st_size - tail_limit)
        stream.seek(start)
        tail = stream.read(before.st_size - start)
        if not tail.endswith(b'\n'):
            raise ValueError('Original history EOF has an incomplete record')
        if r40_file_identity(os.fstat(stream.fileno())) != r40_file_identity(before):
            raise ValueError('Original history changed during observation')
    if r40_file_identity(r40_owned_regular(path)) != r40_file_identity(before):
        raise ValueError('Original history path was replaced during observation')
    lines = tail.splitlines()
    if start:
        lines = lines[1:]
    lifecycle = []
    for line in lines:
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError('Original history contains a non-object record')
        payload = event.get('payload')
        if event.get('type') != 'event_msg' or not isinstance(payload, dict):
            continue
        kind = payload.get('type')
        if kind in {'task_started', 'task_complete', 'turn_aborted', 'user_message'}:
            lifecycle.append({'kind': kind, 'at': event.get('timestamp'), 'turn_id': payload.get('turn_id'), 'reason': payload.get('reason'), 'error': payload.get('error'), 'line_sha256': hashlib.sha256(line).hexdigest()})
    return {'path': str(path), 'session_id': sid, 'file_id': [before.st_dev, before.st_ino], 'fence': before.st_size, 'header_sha256': hashlib.sha256(header).hexdigest(), 'tail_start': start, 'tail_sha256': hashlib.sha256(tail).hexdigest(), 'last_native_event': lifecycle[-1] if lifecycle else None, 'bounded_lifecycle': lifecycle[-12:], 'saved_launch_cwd_advisory': metadata['payload'].get('cwd'), 'current_thread_authority': False}

def r40_recovery_preparation(history, goal, *, has_original_launch_context=False, shell_foreground=False, original_no_restart=True):
    """Describe what is missing, without converting observations to permission."""
    if not isinstance(history, dict) or not isinstance(goal, dict):
        raise ValueError('Original history and Goal observations must be explicit')
    if history.get('session_id') != goal.get('session_id'):
        raise ValueError('Original history and Goal name different sessions')
    event = history.get('last_native_event') or {}
    status = goal.get('status', 'unknown')
    phase, reason = ('held', 'original_recovery_policy_disabled')
    if event.get('kind') == 'turn_aborted':
        phase, reason = ('user_abort_preserved', 'native_abort_requires_origin_review')
    elif status == 'complete':
        phase, reason = ('goal_complete', 'original_goal_already_complete')
    elif status in {'paused', 'budget_limited', 'usage_limited'}:
        phase, reason = ('goal_fenced', 'original_goal_' + status)
    elif status == 'unknown':
        phase, reason = ('goal_unknown', 'original_goal_not_verified')
    elif not shell_foreground:
        phase, reason = ('placement_unverified', 'original_shell_foreground_not_verified')
    elif not has_original_launch_context:
        phase, reason = ('launch_context_missing', 'saved_history_does_not_contain_original_launch')
    elif original_no_restart:
        phase, reason = ('policy_disabled', 'all_business_agents_no_restart')
    else:
        phase, reason = ('prepared_not_authorized', 'original_input_and_writer_boundaries_still_required')
    attention = bool(shell_foreground and status in {'active', 'blocked'} and (event.get('kind') != 'turn_aborted'))
    return {'schema': 'ccc-original-terminal-recovery-preparation-r40', 'phase': phase, 'reason': reason, 'original_goal_status': status, 'advisory_session_id': history['session_id'], 'last_native_event_kind': event.get('kind'), 'last_native_event_has_error': bool(event.get('error')), 'attention_required': attention, 'original_launch_context_available': bool(has_original_launch_context), 'requirements': list(R40_EXIT_REQUIREMENTS), 'recovery_available': False, 'current_thread_authority': False, 'input_operations': 0, 'new_attempt_created': False, 'business_no_restart_constraint': bool(original_no_restart)}


def original_exit_diagnostic(observer, endpoint, pane, server, terminal):
    """Show original-task recovery preparation on a proved foreground shell.

    Locators are authenticated observations of historical files, not an input
    enrollment. Even a completed plan cannot provide a current native SID.
    """
    now = time.time()
    fields = ('key', 'session', 'pane_id', 'terminal_id', 'server',
              'cmux_workspace_id', 'cmux_surface_id', 'workspace_id', 'tab_id')
    binding = dict(terminal.get('binding') or {})
    result = {'schema': 'ccc-herdr-original-exit-diagnostic-r40',
        'mode': 'read_only', 'observed_at': now, 'binding': binding,
        'phase': 'unavailable', 'current_thread_authority': False,
        'control_verified': False, 'recovery_available': False,
        'input_operations': 0, 'attention_required': False,
        'new_attempt_created': False, 'goal_writes': 0}
    try:
        if (terminal.get('schema') != 'ccc-herdr-terminal-program-r38'
                or terminal.get('phase') != 'shell_foreground'
                or terminal.get('mode') != 'read_only'
                or terminal.get('native_thread_authority') is not False
                or terminal.get('control_verified') is not False
                or terminal.get('recovery_available') is not False
                or terminal.get('input_operations') != 0
                or endpoint['cmux_surface_id'] == PROTECTED_SURFACE
                or binding.get('server') != server
                or binding.get('key') != endpoint['socket'] + '#' + pane['pane_id']
                or binding.get('terminal_id') != pane['terminal_id']
                or binding.get('cmux_workspace_id') != endpoint['cmux_workspace_id']
                or binding.get('cmux_surface_id') != endpoint['cmux_surface_id']
                or binding.get('workspace_id') != pane['workspace_id']
                or binding.get('tab_id') != pane['tab_id']):
            raise ValueError('Original exit diagnostic has no current shell binding')
        locators = observer.guard_config.get('original_exit_locators', [])
        if not isinstance(locators, list) or len(locators) > 32:
            raise ValueError('Original diagnostic locator scope is invalid')
        selected = [x for x in locators if isinstance(x, dict)
                    and x.get('key') == binding['key']]
        if len(selected) != 1:
            raise ValueError('No unique original diagnostic locator for this shell')
        locator = selected[0]
        if (locator.get('source') != 'frozen_original_history_advisory'
                or locator.get('terminal_id') != pane['terminal_id']
                or locator.get('current_thread_authority') is not False
                or locator.get('input_enrollment') is not False):
            raise ValueError('Original locator cannot supply input or thread authority')
        sid = r40_canonical_sid(locator['sid'])
        history = r40_original_history(locator['history'], sid)
        if (history['header_sha256'] != locator.get('header_sha256')
                or history['file_id'] != locator.get('file_id')
                or history['fence'] < locator.get('fence', 0)):
            raise ValueError('Original diagnostic history was replaced or truncated')
        goal = r40_original_goal(locator['codex_home'], sid)
        prepared = r40_recovery_preparation(history, goal,
            has_original_launch_context=False, shell_foreground=True,
            original_no_restart=True)
        current = terminal_program_observation(observer, endpoint, pane, server)
        if (current.get('phase') != 'shell_foreground'
                or current.get('binding') != binding
                or current.get('processes') != terminal.get('processes')
                or current.get('shell_pid') != terminal.get('shell_pid')
                or current.get('foreground_pgid') != terminal.get('foreground_pgid')):
            raise ValueError('Original shell changed during exit preparation')
        return dict(result, phase=prepared['phase'], reason=prepared['reason'],
            advisory_session_id=sid, original_goal_status=goal['status'],
            original_goal_revision=goal.get('updated_at_ms'),
            history_file_id=history['file_id'], history_fence=history['fence'],
            last_native_event_kind=prepared['last_native_event_kind'],
            last_native_event_has_error=prepared['last_native_event_has_error'],
            attention_required=prepared['attention_required'],
            original_launch_context_available=False,
            business_no_restart_constraint=True,
            requirements=prepared['requirements'],
            no_writer_proof_complete=False)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error,
            subprocess.SubprocessError) as error:
        return dict(result, phase='unavailable', reason=str(error)[:240])


def claude_reader_health(row, current, process, now):
    """Expose an aged, unresolved original Hook without granting input authority.

    Age is measured from the original event, not claimed as time spent in this
    reader. No old event is skipped and no journal/attempt/budget is changed.
    """
    if (row.get('agent') != 'claude' or row.get('process_bound') is not True
            or row.get('native_reader_available') is not False
            or not isinstance(current, dict)):
        return None
    event = current.get('latest', {}).get('event', {})
    at = event.get('created_at')
    birth = process.get('birth')
    if (not life.valid_event(event, now) or event.get('event_name') not in {'Stop', 'StopFailure'}
            or event.get('agent_pid') != process.get('pid') or row.get('pid') != process.get('pid')
            or row.get('birth') != birth or not isinstance(birth, list) or len(birth) != 2
            or any(type(n) is not int for n in birth)
            or event.get('surface_id') != row.get('cmux_surface_id')
            or event.get('workspace_id') != row.get('cmux_workspace_id')
            or not birth[0] + birth[1] / 1e6 <= at <= now):
        return None
    elapsed = now - at
    fields = ('key', 'session', 'pane_id', 'terminal_id', 'pid', 'birth',
              'server', 'cmux_workspace_id', 'cmux_surface_id')
    return {'schema': 'ccc-claude-hook-reader-health-r6', 'mode': 'read_only',
            'purpose': 'claude_hook_native_binding', 'age_basis': 'original_hook_timestamp',
            'binding': {name: row.get(name) for name in fields},
            'event_id': event['event_id'], 'event_name': event['event_name'],
            'event_at': at, 'observed_at': now, 'elapsed_seconds': elapsed,
            'phase': 'deadline_exceeded' if elapsed >= 300 else 'warning' if elapsed >= 180 else 'observing',
            'attention_required': elapsed >= 180, 'reason': row.get('native_reader_reason'),
            'control_verified': False, 'recovery_available': False, 'input_operations': 0}


def _herdr_placement_matches(endpoint, pane, environment):
    return (environment.get('HERDR_ENV') in {'0', '1'}
            and environment.get('HERDR_SOCKET_PATH') == endpoint['socket']
            and environment.get('HERDR_PANE_ID') == pane['pane_id']
            and environment.get('HERDR_WORKSPACE_ID') == pane['workspace_id']
            and environment.get('HERDR_TAB_ID') == pane['tab_id']
            and environment.get('HERDR_SESSION', 'default') == endpoint['session']
            and environment.get('CMUX_WORKSPACE_ID') == endpoint['cmux_workspace_id']
            and environment.get('CMUX_SURFACE_ID') == endpoint['cmux_surface_id'])


def _extract_resume_id(argv, kind):
    if kind == 'codex':
        options = argv[1:argv.index('--')] if '--' in argv else argv[1:]
        if 'resume' in options:
            idx = options.index('resume')
            if idx + 1 < len(options):
                try:
                    return str(uuid.UUID(options[idx + 1]))
                except (ValueError, TypeError):
                    return None
        return None
    from ccc_herdr_observer import resume_id
    return resume_id(argv, kind)


def _select_herdr_foreground(processes, endpoint, pane, pgid):
    agents = [p for p in processes if p.get('agent_kind') in {'codex', 'claude'}]
    if (type(pgid) is not int or pgid <= 0 or len(agents) != 1
            or agents[0]['agent_kind'] != pane['agent'] or agents[0]['pgid'] != pgid
            or not _herdr_placement_matches(endpoint, pane, agents[0]['environment'])):
        raise RuntimeError('Native foreground agent is missing, ambiguous or changed')
    return agents[0]


def _ensure_outer_surface_isolation(ccc_config_path, endpoints):
    if not endpoints:
        return
    try:
        config_path = Path(ccc_config_path)
        current = json.loads(config_path.read_bytes())
        existing = set(current.get('discovery_excluded_surface_ids', []))
        needed = [e['cmux_surface_id'] for e in endpoints
                  if isinstance(e.get('cmux_surface_id'), str) and e['cmux_surface_id'] not in existing]
        if not needed:
            return
        from cmux_codex_watch import ConfigStore
        def _exclude_herdr_surfaces(latest):
            excluded = latest.setdefault('discovery_excluded_surface_ids', [])
            for sid in needed:
                if sid not in excluded:
                    excluded.append(sid)
        ConfigStore(config_path).mutate(_exclude_herdr_surfaces)
    except Exception:
        pass


def rollover_ledger(path, ledger, final_phases, *, keep=1000, trigger=2000):
    attempts = ledger.get('attempts', {})
    if len(attempts) <= trigger:
        return False
    finished = [(k, v) for k, v in attempts.items()
                if isinstance(v, dict) and v.get('phase') in final_phases]
    if not finished:
        return False
    finished.sort(key=lambda kv: float(kv[1].get('attempted_at') or 0.0))
    to_remove = max(0, len(attempts) - keep)
    archived = finished[:to_remove]
    if not archived:
        return False
    archive_dir = Path(path).parent / 'archive'
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_file = archive_dir / f"{Path(path).stem}-{time.strftime('%Y%m')}.jsonl"
    lines = b''.join(life.canonical({'id': k, **v}) + b'\n' for k, v in archived)
    fd = os.open(archive_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as stream:
        stream.write(lines)
        stream.flush()
        os.fsync(stream.fileno())
    for k, _ in archived:
        attempts.pop(k, None)
    atomic(path, ledger)
    return True


def build_guard_class(original):
    class Guard(original):
        def __init__(self, config):
            super().__init__(config['observer'])
            self._registered_endpoints = tuple(dict(e) for e in self.config['endpoints'])
            self.config = dict(self.config, endpoints=list(self._registered_endpoints))
            self.guard_config = config
            self.servers, self.screens = {}, {}
            self.pane_snapshots = {}
            self._discovery = None
            self._last_discovery = float('-inf')
            self._capture_process_cache = None
            self.journal = life.NativeJournal(Path(config['ccc_config']).parent / 'claude-events.jsonl')

        def process(self, pid):
            cache = self._capture_process_cache
            if cache is not None:
                now_birth = self.scope.birth(pid)
                if now_birth is not None:
                    try:
                        pgid = os.getpgid(pid)
                        info = self.native_info(pid)
                    except (OSError, RuntimeError):
                        cache.pop(pid, None)
                    else:
                        hit = cache.get(pid)
                        if (hit is not None and hit['birth'] == now_birth
                                and hit['pgid'] == pgid and hit['ppid'] == info.ppid):
                            return copy.deepcopy(hit['value'])
                        val = super().process(pid)
                        cache[pid] = {'birth': val['birth'], 'pgid': val['pgid'],
                                      'ppid': val['ppid'], 'value': copy.deepcopy(val)}
                        return val
            return super().process(pid)

        def cmux_membership(self, endpoint):
            cmux_bin = str(self.config.get('cmux_binary', ''))
            if cmux_bin in {'/opt/homebrew/bin/cmux', '/usr/local/bin/cmux'}:
                sock_candidates = []
                env_sock = os.environ.get('CMUX_SOCKET_PATH')
                if env_sock:
                    sock_candidates.append(Path(env_sock))
                sock_candidates.append(Path.home() / '.local/state/cmux/cmux.sock')
                for sock_path in sock_candidates:
                    try:
                        st = sock_path.lstat()
                        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
                            continue
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                            conn.settimeout(1.5)
                            conn.connect(str(sock_path))
                            req = {'id': '1', 'method': 'system.tree',
                                   'params': {'workspace_id': endpoint['cmux_workspace_id'],
                                              'id_format': 'both'}}
                            conn.sendall(json.dumps(req, separators=(',', ':')).encode('utf-8') + b'\n')
                            buf = bytearray()
                            while not buf.endswith(b'\n') and len(buf) <= 2 * 1024 * 1024:
                                chunk = conn.recv(65536)
                                if not chunk:
                                    break
                                buf.extend(chunk)
                        if len(buf) <= 2 * 1024 * 1024 and buf.endswith(b'\n'):
                            reply = json.loads(buf.decode('utf-8'))
                            if reply.get('ok') is True and isinstance(reply.get('result'), dict):
                                records = self.scope.records(reply['result'])
                                if endpoint['cmux_surface_id'] in records:
                                    return records
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        pass
            return super().cmux_membership(endpoint)

        def server(self, endpoint, membership):
            result = super().server(endpoint, membership)
            self.servers[endpoint['session']] = result
            return result

        def foreground(self, endpoint, pane, server):
            view = self.cli(endpoint['session'], ['pane', 'process-info', '--pane', pane['pane_id']])['process_info']
            candidates = view.get('foreground_processes', [])
            if view.get('pane_id') != pane['pane_id'] or not 1 <= len(candidates) <= 64:
                raise RuntimeError('Foreground is missing, ambiguous or changed')
            processes = [self.process(candidate['pid']) for candidate in candidates]
            process = _select_herdr_foreground(processes, endpoint, pane, view.get('foreground_process_group_id'))
            argv = process['argv']
            current = process['pid']
            for _ in range(8):
                if current == server['pid']:
                    break
                current = self.native_info(current).ppid
            else:
                raise RuntimeError('Agent is not a child of the original Herdr server')
            process['tty'] = native_terminal_name(self, process)
            process['native_resume_id'] = _extract_resume_id(argv, pane['agent'])
            return process

        def cli(self, session, args):
            from ccc_herdr_observer import readonly_args
            if not readonly_args(args):
                raise ValueError('Guard observation method is not read-only')
            endpoint = endpoint_for(self.config, session)
            server = self.servers.get(session)
            if server is None:
                return super().cli(session,args)
            if args == ['api','snapshot']:
                method, params = 'session.snapshot', {}
            elif args[:2] == ['pane','process-info']:
                method, params = 'pane.process_info', {'pane_id':args[3]}
            else:
                method, params = 'pane.read', {'pane_id':args[2], 'source':'visible',
                                              'lines':160, 'format':'text', 'strip_ansi':True}
            value = request(endpoint,server,method,params,self.scope)
            if 'error' in value or not isinstance(value.get('result'),dict):
                raise ValueError('Herdr read refused or returned an unknown envelope')
            if method == 'pane.read':
                result = value['result'].get('read')
                if not isinstance(result,dict) or not isinstance(result.get('text'),str):
                    raise ValueError('Herdr visible read shape changed')
                text = result['text']
                self.screens[(session,args[2])] = text
                return {'text':text}
            if method == 'session.snapshot':
                snap = value['result'].get('snapshot', {})
                for p in snap.get('panes', []) if isinstance(snap, dict) else []:
                    if isinstance(p, dict) and p.get('agent') is None and isinstance(p.get('agent_session'), dict):
                        sess_agent = p['agent_session'].get('agent')
                        if sess_agent in {'codex', 'claude'}:
                            p['agent'] = sess_agent
                self.pane_snapshots[session] = snap
            return value['result']

        def capture(self,state):
            self._capture_process_cache = {}
            try:
                return self._capture_impl(state)
            finally:
                self._capture_process_cache = None

        def _capture_impl(self,state):
            settings = self.guard_config.get('discovery', {})
            interval = settings.get('scan_interval_sec', 30)
            if type(interval) not in (float, int) or not 5 <= interval <= 60:
                raise ValueError('Discovery scan interval is invalid')
            if self._discovery is None or time.monotonic() - self._last_discovery >= interval:
                try:
                    endpoints, self._discovery = discover_endpoints(self, self._registered_endpoints, settings)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                    endpoints = list(self._registered_endpoints)
                    self._discovery = {'schema': 'ccc-herdr-private-discovery-r35', 'mode': 'read_only',
                        'status': 'held', 'observed_at': time.time(), 'reason': str(error)[:160],
                        'new_endpoints': [], 'application_input_operations': 0,
                        'collaboration_enrollments': 0}
                self.config = dict(self.config, endpoints=endpoints)
                _ensure_outer_surface_isolation(self.guard_config['ccc_config'], endpoints)
                self._last_discovery = time.monotonic()
            self.pane_snapshots = {}
            self.journal.refresh()
            result = super().capture(state)
            for row in result['agents']:
                endpoint = endpoint_for(self.config, row['session'])
                row.update(transport='herdr_private_api', cmux_surface_id=endpoint['cmux_surface_id'],
                           discovery_source=endpoint.get('discovery_source', 'registered_original_endpoint'),
                           discovered_read_only=endpoint.get('discovered_read_only') is True)
                if row.get('process_bound') is not True:
                    matches = [pane for pane in self.pane_snapshots.get(row['session'], {}).get('panes', [])
                               if pane.get('pane_id') == row['pane_id']]
                    if len(matches) == 1:
                        row.update(inner_workspace_id=matches[0].get('workspace_id'),
                                   inner_tab_id=matches[0].get('tab_id'))
                        row['native_terminal_observation'] = terminal_program_observation(
                            self, endpoint, matches[0], row['server'])
                        if row['agent'] == 'codex' and row['native_terminal_observation']['phase'] == 'shell_foreground':
                            row.update(native_reader_available=False, continuation_eligible=False,
                                auto_input_enabled=False,
                                recovery_reason='codex_not_foreground_original_resume_disabled')
                if row['agent'] != 'claude' or not row.get('process_bound'):
                    continue
                endpoint = endpoint_for(self.config, row['session'])
                process = self.process(row['pid'])
                current, reason = self.journal.lookup(endpoint, process)
                try:
                    if current is None:
                        raise ValueError(reason)
                    view, proof = life.native_observation(current, process, allow_cache=True)
                    row.update(view)
                    row['native_reader_reason'] = None
                    text = self.screens.get((row['session'],row['pane_id']),'')
                    row['draft_observation'] = life.composer_state(text,'claude')
                    config = json.loads(Path(self.guard_config['ccc_config']).read_bytes())
                    enabled, _ = policy(config,endpoint)
                    allowed, reason = life.admission(view,text,enabled=enabled)
                    row.update(continuation_eligible=allowed,recovery_reason=reason,
                               auto_input_enabled=bool(self.guard_config.get('armed') and enabled))
                    if endpoint.get('discovered_read_only') is True:
                        row.update(continuation_eligible=False, auto_input_enabled=False,
                                   recovery_reason='discovered_endpoint_monitor_only')
                except (OSError,ValueError,TypeError,KeyError,AttributeError,ImportError,sqlite3.Error,subprocess.SubprocessError) as error:
                    row.update(native_reader_available=False,native_reader_reason=str(error)[:160],
                               continuation_eligible=False,recovery_reason='native_evidence_held')
                row['claude_reader_health'] = claude_reader_health(row, current, process, time.time())
            current_config = json.loads(Path(self.guard_config['ccc_config']).read_bytes())
            armed = self.guard_config.get('armed') and current_config.get('mode') == 'armed'
            result.update(schema='ccc-herdr-observation-v174',mode='armed' if armed else 'read_only',
                          native_journal_error=self.journal.error,
                          claude_reader_attention_required=sum(
                              item.get('agent') == 'claude'
                              and (item.get('claude_reader_health') or {}).get('attention_required') is True
                              for item in result['agents']),
                          active_rules={'claude_native_incomplete_continuation':bool(armed),
                                        'codex_continuation':False,'codex_native_observation':True},
                          all_continuation_rules_active=False, input_cas_available=False)
            observed = {e['session'] for e in result['endpoints'] if e.get('status') == 'observed'}
            inventory = []
            for endpoint in self.config['endpoints']:
                if endpoint['session'] not in observed:
                    continue
                for pane in self.pane_snapshots.get(endpoint['session'], {}).get('panes', []):
                    session_info = pane.get('agent_session')
                    item = {'key': endpoint['socket'] + '#' + pane['pane_id'],
                        'session': endpoint['session'], 'pane_id': pane['pane_id'],
                        'terminal_id': pane['terminal_id'], 'reported_agent': pane.get('agent'),
                        'reported_status': pane.get('agent_status'),
                        'reported_session_id': session_info.get('value') if isinstance(session_info, dict) else None,
                        'cmux_surface_id': endpoint['cmux_surface_id'],
                        'cmux_workspace_id': endpoint['cmux_workspace_id'],
                        'source': 'current_authenticated_herdr_snapshot',
                        'native_thread_authority': False, 'respawn_available': False,
                        'agent_reported_missing': pane.get('agent') is None}
                    if pane.get('agent') not in {'codex', 'claude'}:
                        server = self.servers.get(endpoint['session'])
                        if server:
                            item.update(server=server, inner_workspace_id=pane.get('workspace_id'),
                                inner_tab_id=pane.get('tab_id'),
                                native_terminal_observation=terminal_program_observation(self, endpoint, pane, server))
                    if item.get('native_terminal_observation', {}).get('phase') == 'shell_foreground':
                        item['native_original_exit_diagnostic'] = original_exit_diagnostic(
                            self, endpoint, pane, item['server'], item['native_terminal_observation'])
                    inventory.append(item)
            result.update(discovery=dict(self._discovery, observed_sessions=sorted(observed),
                current_scope_endpoints=len(self.config['endpoints']),
                observed_endpoints=len(observed), observed_codex_claude_panels=len(result['agents']),
                cache_age_seconds=max(0, time.time() - self._discovery['observed_at'])),
                panel_inventory=inventory,
                original_exit_attention_required=sum(
                    item.get('native_original_exit_diagnostic', {}).get('attention_required') is True
                    for item in inventory),
                terminal_observation_mode='read_only',
                shell_foreground_panels=sum(item.get('native_terminal_observation', {}).get('phase') == 'shell_foreground'
                                            for item in inventory),
                rule_details={'codex_original_terminal_respawn': {
                    'implemented': False, 'enabled': False, 'recovery_available': False,
                    'reason': 'no_guarded_original_terminal_respawn',
                    'business_no_restart_constraint': True},
                    'original_exit_diagnostics': {'implemented': True, 'mode': 'read_only',
                        'input_enrollment': False, 'recovery_available': False},
                    'herdr_dynamic_discovery': {'enabled': settings.get('enabled') is True,
                        'input_mode': 'read_only', 'new_collaboration_enrolled': False}})
            return result

    return Guard


def step(guard,snapshot,state):
    """Persistent per-inner-panel transaction; no unknown submission is retried."""
    path = state / 'herdr-input-ledger.json'
    ledger = json.loads(path.read_bytes()) if path.exists() else {'schema':'herdr-input-ledger-v174','attempts':{}}
    rollover_ledger(path, ledger, frozenset({'native_received', 'host_refused', 'prewrite_refused'}))
    attempts = ledger['attempts']
    if len(attempts)>8192:
        raise ValueError('Herdr original input ledger exceeds bound; history preserved')
    for row in snapshot['agents']:
        if row.get('agent')!='claude' or not row.get('process_bound'):
            continue
        endpoint = endpoint_for(guard.config, row['session'])
        if endpoint.get('discovered_read_only') is True:
            row.update(continuation_eligible=False, auto_input_enabled=False,
                       recovery_reason='discovered_endpoint_monitor_only')
            continue
        related = [a for a in attempts.values() if a.get('key')==row['key']]
        pending = False
        ledger_changed = False
        isolated = []
        for attempt in related:
            if attempt['phase'] not in {'intent','submitted','write_unknown','receipt_unknown'}:
                continue
            if not same_generation(attempt, row):
                isolated.append(attempt.get('intent_id'))
                continue
            try:
                observed = receipt(attempt)
            except (OSError,ValueError,KeyError,TypeError,ImportError) as error:
                observed=None
                attempt.update(phase='receipt_unknown',reason=str(error)[:160])
                ledger_changed = True
            if observed:
                attempt.update(phase='native_received',receipt=observed,confirmed_at=time.time())
                atomic(path,ledger)
            else:
                pending=True
        if ledger_changed:
            atomic(path,ledger)
        row.update(send_count=len(related),submit_phase='pending' if pending else 'none',
                   native_receipts=sum(a['phase']=='native_received' for a in related),
                   isolated_generation_intents=isolated)
        if pending:
            row.update(continuation_eligible=False,recovery_reason='original_input_pending')
            continue
        if not guard.guard_config.get('armed') or not row.get('continuation_eligible'):
            continue
        key=life.digest(life.canonical({'key':row['key'],'server':row['server'],
                                       'pid':row['pid'],'birth':row['birth'],'event':row['native_event_id']}))
        previous = attempts.get(key)
        unused = retryable_zero_input(previous)
        if previous and not unused:
            row.update(recovery_reason='original_event_already_attempted')
            continue
        config=json.loads(Path(guard.guard_config['ccc_config']).read_bytes())
        last=max((a.get('attempted_at',0) for a in related),default=0)
        if time.time()-last<max(1.0,float(config.get('send_interval_sec',1))):
            row.update(recovery_reason='original_retry_interval')
            continue
        try:
            snapshot_now=guard.cli(endpoint['session'],['api','snapshot'])['snapshot']
            pane=next(p for p in snapshot_now['panes'] if p['pane_id']==row['pane_id'])
            if pane['terminal_id']!=row['terminal_id']:
                raise ValueError('Original terminal changed')
            native=guard.foreground(endpoint,pane,row['server'])
            if native['pid']!=row['pid'] or native['birth']!=row['birth']:
                raise ValueError('Original inner foreground generation changed')
            current,reason=guard.journal.lookup(endpoint,native)
            if not current:
                raise ValueError(reason)
            view,proof=life.native_observation(current,native)
            if view['native_event_id']!=row['native_event_id']:
                raise ValueError('Original event superseded')
            view,text,config=verify_original(guard,endpoint,pane,native,row['server'],guard.journal,
                                            proof,guard.guard_config['ccc_config'])
            message=config.get('claude_message')
            if not isinstance(message,str) or not message.strip() or len(message.encode())>700 or any(c in message for c in '\r\n\t\x1b'):
                raise ValueError('Configured continuation prompt is outside the single-line wire bound')
            attempt={'schema':'herdr-original-input-intent-v174','key':row['key'],'phase':'intent',
                     'intent_id':previous['intent_id'] if unused else uuid.uuid4().hex,'attempted_at':time.time(),
                     'binding':generation(endpoint,pane,native,row['server']),
                     'session_id':view['native_session_id'],'source_event_id':view['native_event_id'],
                     'native_proof':proof,'message':message,'message_sha256':life.digest(message.encode()),
                     'input_operations':0,'wire_requests':0,
                     'preflight_rechecks':previous.get('preflight_rechecks',0)+1 if unused else 0,
                     'transport':'herdr_agent_prompt','input_cas_available':False}
            if unused:
                preserve_refused_attempt(attempts, key)
            attempts[key]=attempt
            atomic(path,ledger)
            def revalidate():
                _, _, fresh_config = verify_original(guard,endpoint,pane,native,row['server'],guard.journal,
                                                     proof,guard.guard_config['ccc_config'])
                if fresh_config.get('claude_message') != message:
                    raise ValueError('Original continuation prompt changed before submission')
            def before_write():
                revalidate()
                attempt.update(input_operations=1,wire_requests=1,phase='write_unknown',write_started_at=time.time())
                atomic(path,ledger)
            response=submit_with_fallback(endpoint,row['server'],pane['pane_id'],message,guard.scope,
                                          attempt,lambda: atomic(path,ledger),before_write,revalidate)
            attempt['host_response']=response
            if 'error' in response:
                if zero_input_refusal(attempt,response):
                    attempt.update(phase='host_refused',input_operations=0)
                else:
                    attempt['phase']='write_unknown'
            else:
                attempt['phase']='submitted'
                attempt['submitted_at']=time.time()
            atomic(path,ledger)
            row.update(submit_phase=attempt['phase'],recovery_reason='original_prompt_submission',
                       send_count=len(related)+1,continuation_eligible=False)
        except (OSError,ValueError,KeyError,TypeError,StopIteration,ImportError,sqlite3.Error,RuntimeError,subprocess.SubprocessError) as error:
            if key in attempts:
                attempts[key]['reason']=str(error)[:160]
                if attempts[key].get('input_operations')==0 and attempts[key].get('wire_requests',0)==0:
                    attempts[key]['phase']='prewrite_refused'
                atomic(path,ledger)
            row.update(continuation_eligible=False,recovery_reason=str(error)[:160])
    snapshot['input_operations']=sum(a.get('input_operations',0) for a in attempts.values())
    snapshot['native_receipts']=sum(a.get('phase')=='native_received' for a in attempts.values())
    snapshot['original_ledger_mutations']=0
    # Advisory progress inspection reuses this completed original ledger read.
    # The caller must not mutate the returned attempts.
    return attempts


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    manifest=root/'manifest.json'
    if life.digest(manifest.read_bytes())!=args.manifest_sha256:
        raise ValueError('Herdr guard release manifest changed')
    manifest_value=json.loads(manifest.read_bytes())
    if set(manifest_value['files'])!={'ccc_herdr_guard.py','ccc_herdr_lifecycle.py','ccc_herdr_codex.py','guard-config.json'}:
        raise ValueError('Herdr guard release file set changed')
    for name,expected in manifest_value['files'].items():
        if life.digest((root/name).read_bytes())!=expected:
            raise ValueError('Herdr guard release file changed: '+name)
    if args.config.resolve(strict=True)!=root/'guard-config.json':
        raise ValueError('Herdr guard configuration selection changed')
    config=json.loads(args.config.read_bytes())
    original=Path(config['original_observer_release'])
    old_manifest=original/'manifest.json'
    if life.digest(old_manifest.read_bytes())!=config['original_observer_manifest_sha256']:
        raise ValueError('Original observer release changed')
    for name,expected in json.loads(old_manifest.read_bytes())['files'].items():
        if life.digest((original/name).read_bytes())!=expected:
            raise ValueError('Original observer source changed')
    sys.path.insert(0,str(original))
    from ccc_herdr_observer import Observer
    import ccc_herdr_codex as codex
    state=Path(config['state'])
    state.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (state/'observer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        guard=codex.build_guard_class(build_guard_class(Observer))(config)
        while True:
            try:
                snapshot=guard.capture(state)
                step(guard,snapshot,state)
                codex.step(guard,snapshot,state)
            except (OSError,ValueError,KeyError,TypeError,StopIteration,ImportError,sqlite3.Error,RuntimeError,subprocess.SubprocessError) as error:
                snapshot={'schema':'ccc-herdr-observation-v174','at_epoch':time.time(),
                          'pid':os.getpid(),'mode':'armed' if config.get('armed') else 'read_only',
                          'agents':[],'endpoints':[],'observation_error':str(error)[:200],
                          'all_continuation_rules_active':False}
            snapshot['guard_source_sha256']=life.digest(Path(__file__).read_bytes())
            atomic(state/'status.json',snapshot)
            if args.once:
                print(json.dumps({'agents':len(snapshot['agents']),'mode':snapshot['mode'],
                                  'error':snapshot.get('observation_error'),
                                  'input_operations':snapshot.get('input_operations',0)}))
                return
            time.sleep(5)


if __name__=='__main__':
    main()
