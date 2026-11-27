"""Bounded, read-only Herdr adapter. Never sends text, keys or signals.

The user explicitly selected these external cmux workspaces. Selection uses
named Herdr sessions, original socket peers and native foreground processes;
neither UI focus nor Herdr's reported session ID supplies process ownership.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def target_key(endpoint, pane):
    return str(Path(endpoint).absolute()) + '#' + pane


def readonly_args(args):
    return (args == ['api', 'snapshot'] or
            len(args) == 4 and args[:3] == ['pane', 'process-info', '--pane'] or
            len(args) == 7 and args[:2] == ['pane', 'read'] and
            args[3:] == ['--source', 'visible', '--lines', '160'])


def placement_matches(endpoint, pane, environment):
    return (environment.get('HERDR_ENV') == '1'
            and environment.get('HERDR_SOCKET_PATH') == endpoint['socket']
            and environment.get('HERDR_PANE_ID') == pane['pane_id']
            and environment.get('HERDR_WORKSPACE_ID') == pane['workspace_id']
            and environment.get('HERDR_TAB_ID') == pane['tab_id']
            and environment.get('HERDR_SESSION', 'default') == endpoint['session']
            and environment.get('CMUX_WORKSPACE_ID') == endpoint['cmux_workspace_id']
            and environment.get('CMUX_SURFACE_ID') == endpoint['cmux_surface_id'])


def resume_id(argv, kind):
    values = []
    if kind == 'codex' and len(argv) > 2 and argv[1] == 'resume':
        values = [argv[2]]
    if kind == 'claude':
        values = [argv[i + 1] for i, option in enumerate(argv[:-1])
                  if option in {'--resume', '--session-id'}]
    try:
        return str(uuid.UUID(values[0])) if len(values) == 1 else None
    except (ValueError, TypeError):
        return None


def interactive_kind(argv, executable):
    """Use the kernel executable as well as argv; helper names are not agents."""
    if not argv:
        return None
    executable = Path(executable)
    options = argv[1:argv.index('--')] if '--' in argv else argv[1:]
    native_names = {'codex', 'codex-aarch64-apple-darwin', 'codex-x86_64-apple-darwin'}
    if executable.name in native_names and Path(argv[0]).name in native_names:
        if (any(a == '--remote' or a.startswith('--remote=') for a in options)
                or set(options) & {'exec', 'e', 'review', 'mcp', 'mcp-server', 'app-server',
                                   'login', 'logout', 'completion', 'sandbox', 'debug',
                                   'apply', 'cloud', 'features', '--version', '-V', '--help', '-h'}):
            return None
        return 'codex'
    versions = Path.home() / '.local/share/claude/versions'
    if (executable.parent == versions and re.fullmatch(r'2\.\d+\.\d+', executable.name)
            and (Path(argv[0]).name in {'claude', 'claude-code'} or argv[0] == str(executable))):
        if (any(a == '--print' or a.startswith('--print=') for a in options)
                or set(options) & {'-p', '--version', '-v', '--help', '-h', 'mcp',
                                   'install', 'update', 'doctor', 'setup-token'}):
            return None
        return 'claude'
    return None


def select_foreground(processes, endpoint, pane, pgid):
    agents = [p for p in processes if p.get('agent_kind') in {'codex', 'claude'}]
    if (type(pgid) is not int or pgid <= 0 or len(agents) != 1
            or agents[0]['agent_kind'] != pane['agent'] or agents[0]['pgid'] != pgid
            or not placement_matches(endpoint, pane, agents[0]['environment'])):
        raise RuntimeError('Native foreground agent is missing, ambiguous or changed')
    return agents[0]


def screen_indicators(text, kind):
    lines = text.splitlines()
    footer = '\n'.join(lines[-15:])
    dialog = ('Select model' in text and 'Esc to cancel' in text
              or 'Do you trust' in footer or 'Permission required' in footer)
    reconnect = bool(re.search(r'^\s*.*Reconnecting\s+\d+/\d+\b', footer, re.M))
    draft = 'unknown'
    if kind == 'codex':
        prompts = [line.strip()[1:].strip() for line in lines if line.lstrip()[:1] in {'›', '»'}]
        if prompts:
            draft = 'empty' if prompts[-1] == 'Ask Codex to do anything' else 'not_empty_or_other_hint'
    # These are viewport diagnostics only. A historical/example error or a
    # restored title must never become a native recovery authorization.
    errors = [i for i, line in enumerate(lines) if line.lstrip().startswith('■')]
    hint = None
    if errors:
        block = []
        for line in lines[errors[-1]:]:
            if block and (not line.strip() or line.lstrip()[:1] in {'›', '»'}):
                break
            block.append(line.strip())
        error_text = ''.join(block).lstrip('■').strip()
        if len(error_text) <= 16000:
            try:
                error = json.loads(error_text).get('error')
                if (isinstance(error, dict) and error.get('type') == 'invalid_request_error'
                        and error.get('code') == 'invalid_encrypted_content'):
                    hint = 'invalid_encrypted_content'
            except (ValueError, AttributeError):
                if 'exceeded token rate limit' in error_text or 'rate limit exceeded' in error_text:
                    hint = 'rate_limit'
                elif 'currently experiencing high demand' in error_text:
                    hint = 'high_demand'
    mouse_fragment = bool(re.search(r'\[<\d+;\d+;\d+[mM]', footer))
    return {'dialog_observed': bool(dialog), 'provider_reconnecting_observed': reconnect,
            'draft_observation': draft, 'visible_error_hint': hint,
            'sgr_mouse_fragment_observed': mouse_fragment,
            'viewport_hint_authorizes_input': False}


class Observer:
    def __init__(self, config):
        self.config = config
        self.binary = Path(config['herdr_binary']).resolve(strict=True)
        if digest(self.binary) != config['herdr_binary_sha256']:
            raise RuntimeError('Herdr CLI changed')
        runtime = Path(config['dependency_runtime']).resolve(strict=True)
        manifest = runtime / 'manifest.json'
        if digest(manifest) != config['dependency_manifest_sha256']:
            raise RuntimeError('Original CCC dependency manifest changed')
        files = json.loads(manifest.read_bytes())['files']
        for name, expected in files.items():
            if digest(runtime / name) != expected:
                raise RuntimeError('Original CCC dependency changed: ' + name)
        sys.path.insert(0, str(runtime))
        import ccc_guard_scope
        import ccc_codex_queue
        import ccc_codex_goal
        from ccc_request_recovery import executable_path
        self.scope, self.native, self.goal = ccc_guard_scope, ccc_codex_queue, ccc_codex_goal
        self.executable_path = executable_path
        self.sources = len(files)

    def cli(self, session, args):
        if not readonly_args(args):
            raise RuntimeError('This observer has no input/control method')
        result = subprocess.run([str(self.binary), '--session', session, *args],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=5, check=False)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise RuntimeError('Herdr read refused or exceeded its bound')
        if args[:2] == ['pane', 'read']:
            return {'text': result.stdout.decode('utf-8')}
        body = json.loads(result.stdout)
        if not isinstance(body.get('result'), dict) or 'error' in body:
            raise RuntimeError('Herdr read returned an unknown envelope')
        return body['result']

    def native_info(self, pid):
        info = self.native._BsdInfo()
        if self.native._proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            raise RuntimeError('Incomplete native process identity')
        if info.pid != pid or info.status == 5:
            raise RuntimeError('Original process disappeared')
        return info

    def process(self, pid):
        before = self.scope.birth(pid)
        if before is None:
            raise RuntimeError('Original process birth unavailable')
        argv, environment = self.scope.arguments(pid)
        info = self.native_info(pid)
        executable = self.executable_path(pid)
        pgid = os.getpgid(pid)
        if (info.ids[0] != os.getuid() or self.scope.birth(pid) != before
                or self.scope.arguments(pid) != (argv, environment)
                or self.executable_path(pid) != executable):
            raise RuntimeError('Original process birth changed')
        return {'pid': pid, 'birth': before, 'ppid': info.ppid,
                'argv': argv, 'environment': environment, 'pgid': pgid,
                'executable': str(executable), 'agent_kind': interactive_kind(argv, executable)}

    def server(self, endpoint, membership):
        source = Path(endpoint['socket'])
        before = source.lstat()
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
            raise RuntimeError('Original private server socket unavailable')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(source))
            pid = int.from_bytes(connection.getsockopt(0, 2, 4), sys.byteorder, signed=True)
        server = self.process(pid)
        env = server['environment']
        member = membership.get(endpoint['cmux_surface_id'], {})
        if (server['argv'][1:] != ['server'] or Path(server['argv'][0]).name != 'herdr'
                or Path(server['executable']).resolve(strict=True) != self.binary
                or env.get('CMUX_SURFACE_ID') != endpoint['cmux_surface_id']
                or env.get('CMUX_WORKSPACE_ID') != endpoint['cmux_workspace_id']
                or env.get('HERDR_SESSION', 'default') != endpoint['session']
                or member.get('workspace_id') != endpoint['cmux_workspace_id']
                or member.get('type') != 'terminal'):
            raise RuntimeError('Server or current cmux membership disagrees')
        after = source.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError('Original private server socket changed')
        return {'pid': pid, 'birth': server['birth'], 'socket_device': before.st_dev,
                'socket_inode': before.st_ino}

    def foreground(self, endpoint, pane, server):
        view = self.cli(endpoint['session'], ['pane', 'process-info', '--pane', pane['pane_id']])['process_info']
        candidates = view.get('foreground_processes', [])
        if view.get('pane_id') != pane['pane_id'] or not 1 <= len(candidates) <= 64:
            raise RuntimeError('Foreground is missing, ambiguous or changed')
        # MCP helpers share Claude's foreground process group. Inspect native
        # identities and keep the one interactive agent, never the first row.
        processes = [self.process(candidate['pid']) for candidate in candidates]
        process = select_foreground(processes, endpoint, pane, view.get('foreground_process_group_id'))
        argv = process['argv']
        current = process['pid']
        for _ in range(8):
            if current == server['pid']:
                break
            current = self.native_info(current).ppid
        else:
            raise RuntimeError('Agent is not a child of the original Herdr server')
        tty = subprocess.run(['/bin/ps', '-p', str(process['pid']), '-o', 'tty='],
                             capture_output=True, timeout=2, check=True).stdout.decode().strip()
        if not re.fullmatch(r'ttys\d+', tty) or self.scope.birth(process['pid']) != process['birth']:
            raise RuntimeError('Native terminal ownership unavailable')
        process['tty'] = tty
        process['native_resume_id'] = resume_id(argv, pane['agent'])
        return process

    def codex_turn(self, endpoint, pane, process, state):
        # Each real Herdr pane gets an independent reader cache. The original
        # cmux UUID remains placement evidence; it is never a send destination.
        root = Path(process['environment'].get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
        reader = self.native.QueueRecovery(state / 'unused-ledger.json',
                                          state / 'unused-bindings.json', root, '')
        reader.process_lookup = lambda _: {'agent_kind': 'codex', 'agent_pids': [process['pid']]}
        target = {'workspace_id': endpoint['cmux_workspace_id'], 'surface_id': endpoint['cmux_surface_id']}
        turn = reader._open_process_turn(target, fresh=True)
        if not isinstance(turn, dict) or not turn.get('session_id'):
            from ccc_client_thread_observation import read_foreground
            from ccc_request_observation_policy import request_observation_directory_matches
            foreground = read_foreground(process['pid'], request_observation_directory_matches)[0]
            return {'native_turn_kind': 'unknown', 'native_reader_available': False,
                    'native_foreground_observation': foreground,
                    'native_reader_reason': (turn or {}).get('reason') or
                        ('native selection unavailable and no independently held rollout')}
        if (turn.get('pid') != process['pid'] or turn.get('birth') != process['birth']
                or process['native_resume_id'] and turn['session_id'] != process['native_resume_id']):
            raise RuntimeError('Original native thread disagrees with its process')
        error = turn.get('error')
        message = error.get('message', '') if isinstance(error, dict) else ''
        result = {'native_turn_kind': turn['kind'], 'native_reader_available': True,
                  'native_session_id': turn['session_id'], 'native_turn_id': turn.get('turn_id'),
                  'native_at': turn.get('at'), 'native_signature': turn.get('signature'),
                  'native_error_type': self.native.structured_error_type(message) if message else None,
                  'native_error_message': message[:700]}
        goal = (self.goal.goal_state_for_turn(target, turn) if turn['kind'] == 'task_complete'
                else self.goal.goal_state_for_open_turn(target, turn)
                if turn['kind'] in {'task_started', 'native_activity'} else {'status': 'unknown'})
        result['native_goal'] = goal
        return result

    def cmux_membership(self, endpoint):
        # Ask the installed cmux for this explicitly registered workspace only.
        # A busy or unavailable unrelated workspace cannot stall all Herdrs.
        # This remains placement evidence; native process and thread readers
        # still prove every inner agent independently.
        raw = subprocess.run([self.config['cmux_binary'], '--json', '--id-format', 'both',
                              'tree', '--workspace', endpoint['cmux_workspace_id']],
                             stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=5, check=True).stdout
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError('Scoped cmux placement exceeded its read bound')
        tree = json.loads(raw)
        if not isinstance(tree, dict):
            raise RuntimeError('Scoped cmux placement has an invalid root shape')
        try:
            return self.scope.records(tree)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError('Scoped cmux placement has an invalid nested shape') from error

    def capture(self, state):
        started = time.time()
        rows, endpoints = [], []
        for endpoint in self.config['endpoints']:
            try:
                membership = self.cmux_membership(endpoint)
                server = self.server(endpoint, membership)
                snapshot = self.cli(endpoint['session'], ['api', 'snapshot'])['snapshot']
                if snapshot.get('protocol') != 22 or snapshot.get('version') != '0.9.5' or len(snapshot['panes']) > 32:
                    raise RuntimeError('Unsupported server version, protocol or pane count')
                endpoint_rows = []
                for pane in snapshot['panes']:
                    if pane.get('agent') not in {'codex', 'claude'}:
                        continue
                    session_info = pane.get('agent_session')
                    reported_session_id = session_info.get('value') if isinstance(session_info, dict) else None
                    row = {'key': target_key(endpoint['socket'], pane['pane_id']),
                           'session': endpoint['session'], 'cmux_workspace_id': endpoint['cmux_workspace_id'],
                           'pane_id': pane['pane_id'], 'terminal_id': pane['terminal_id'],
                           'agent': pane['agent'], 'reported_status': pane.get('agent_status'),
                           'reported_session_id': reported_session_id,
                           'server': server, 'auto_input_enabled': False, 'captured_at': time.time()}
                    try:
                        process = self.foreground(endpoint, pane, server)
                        text = self.cli(endpoint['session'], ['pane', 'read', pane['pane_id'], '--source', 'visible', '--lines', '160'])
                        text = text.get('read', text.get('output', text.get('text', '')))
                        if isinstance(text, dict):
                            text = text.get('text', '')
                        row.update(pid=process['pid'], birth=process['birth'], tty=process['tty'],
                                   native_executable=process['executable'], native_pgid=process['pgid'],
                                   native_resume_id=process['native_resume_id'], process_bound=True)
                        row.update(screen_indicators(str(text), pane['agent']))
                        if pane['agent'] == 'codex':
                            row.update(self.codex_turn(endpoint, pane, process, state))
                        else:
                            row.update(native_reader_available=False,
                                       native_reader_reason='Independent Herdr Claude hook binding not installed')
                        actual = row.get('native_session_id') or process['native_resume_id']
                        row['reported_session_disagrees'] = bool(actual and row['reported_session_id']
                                                                and actual != row['reported_session_id'])
                        after = self.foreground(endpoint, pane, server)
                        if any(after.get(k) != process.get(k) for k in
                               ('pid', 'birth', 'pgid', 'tty', 'argv', 'environment', 'executable')):
                            raise RuntimeError('Foreground changed during the observation')
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                        row.update(process_bound=False, observation_error=str(error)[:200])
                    endpoint_rows.append(row)
                # Re-read actual placement after potentially slow native/Goal
                # inspection. Reusing the first tree cannot detect a surface
                # moved or closed while its inner panes were being inspected.
                if self.server(endpoint, self.cmux_membership(endpoint)) != server:
                    raise RuntimeError('Server or cmux placement changed during the observation')
                rows.extend(endpoint_rows)
                endpoints.append({'session': endpoint['session'], 'status': 'observed',
                                  'all_panes': len(snapshot['panes']), 'codex_claude_panes': len(endpoint_rows)})
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                endpoints.append({'session': endpoint['session'], 'status': 'unknown', 'reason': str(error)[:200]})
        return {'schema': 'ccc-herdr-observation-v172', 'started_at': started, 'at_epoch': time.time(),
                'pid': os.getpid(), 'dependency_files_verified': self.sources,
                'endpoints': endpoints, 'agents': rows, 'input_operations': 0,
                'client_signals': 0, 'server_restarts': 0, 'focus_changes': 0,
                'mode': 'read_only', 'all_continuation_rules_active': False}


def publish(state, snapshot):
    temporary = state / 'status.tmp'
    with temporary.open('w') as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temporary, state / 'status.json')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    if args.manifest_sha256:
        root = Path(__file__).resolve().parent
        manifest = root / 'manifest.json'
        if digest(manifest) != args.manifest_sha256:
            raise RuntimeError('Installed observer manifest changed')
        files = json.loads(manifest.read_bytes())['files']
        if set(files) != {'ccc_herdr_observer.py', 'observer-config.json'}:
            raise RuntimeError('Installed observer file set changed')
        if args.config.resolve(strict=True) != root / 'observer-config.json':
            raise RuntimeError('Installed observer configuration path changed')
        for name, expected in files.items():
            if digest(root / name) != expected:
                raise RuntimeError('Installed observer source/configuration changed')
    args.state.mkdir(parents=True, exist_ok=True)
    with (args.state / 'observer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observer = Observer(json.loads(args.config.read_bytes()))
        while True:
            try:
                snapshot = observer.capture(args.state)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                snapshot = {'schema': 'ccc-herdr-observation-v172', 'at_epoch': time.time(),
                            'mode': 'read_only', 'pid': os.getpid(), 'agents': [],
                            'endpoints': [], 'observation_error': str(error)[:200],
                            'input_operations': 0, 'all_continuation_rules_active': False}
            snapshot['observer_source_sha256'] = digest(Path(__file__))
            publish(args.state, snapshot)
            if args.once:
                print(json.dumps({'mode': 'read_only', 'status': str(args.state / 'status.json'),
                                  'endpoints': snapshot['endpoints'], 'agents': len(snapshot['agents']),
                                  'bound': sum(bool(r.get('process_bound')) for r in snapshot['agents']),
                                  'input_operations': 0}))
                return
            time.sleep(15)


if __name__ == '__main__':
    main()
