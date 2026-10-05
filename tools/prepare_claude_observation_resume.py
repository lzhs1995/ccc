"""Capture an existing Claude's resume inputs without stopping or starting it.

The private record is evidence, never an executable command or an API-key
observation. In particular, cmux's compact launch metadata is not a substitute
for actual argv: it may omit merged settings and contains no fresh MCP binding.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid


def identifier(value):
    return str(uuid.UUID(value)).lower()


def file_identity(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_mode,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def read_file(path):
    """Pin both the named path and its resolved regular file, including bytes."""
    path = Path(path).absolute()
    before = file_identity(path.lstat())
    resolved = path.resolve(strict=True)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or info.st_size > 4 * 1024 * 1024):
            raise ValueError('unsafe or oversized configuration input')
        raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or file_identity(os.fstat(stream.fileno())) != file_identity(info):
            raise ValueError('configuration changed during read')
    if (file_identity(path.lstat()) != before or path.resolve(strict=True) != resolved
            or file_identity(resolved.stat()) != file_identity(info)):
        raise ValueError('configuration path changed during read')
    return dict(path=str(path), resolved=str(resolved), path_identity=before,
                identity=file_identity(info), sha256=hashlib.sha256(raw).hexdigest(),
                raw_utf8=raw.decode('utf-8'))


def json_input(value, cwd):
    if value.lstrip().startswith(('{', '[')):
        row = dict(kind='inline', raw_utf8=value,
                   sha256=hashlib.sha256(value.encode()).hexdigest())
    else:
        path = Path(value)
        row = dict(kind='file', **read_file(path if path.is_absolute() else Path(cwd) / path))
    data = json.loads(row['raw_utf8'])
    if not isinstance(data, dict):
        raise ValueError('configuration must be a JSON object')
    return row, data


def option_values(argv, name, *, variadic=False):
    """Read actual options, respecting -- and Claude's variadic MCP option."""
    values = []
    i = 1
    while i < len(argv):
        item = argv[i]
        if item == '--':
            break
        if item.startswith(name + '='):
            values.append(item[len(name) + 1:])
        elif item == name:
            i += 1
            if i == len(argv) or argv[i].startswith('-'):
                raise ValueError('missing option value: ' + name)
            values.append(argv[i])
            if variadic:
                while i + 1 < len(argv) and not argv[i + 1].startswith('-'):
                    i += 1
                    values.append(argv[i])
        i += 1
    return values


def session_from_argv(argv):
    # Interactive continue, forks and an ambiguous original session need an
    # independent native binding; never guess one from a directory or title.
    options = argv[1:]
    if '--' in options:
        options = options[:options.index('--')]
    flags = [item.split('=', 1)[0] for item in options]
    if any(flag in flags for flag in ('--continue', '-c', '--fork-session', '--session-id')):
        raise ValueError('resume requires one explicit original session')
    sessions = option_values(argv, '--resume') + option_values(argv, '-r')
    if len(sessions) != 1:
        raise ValueError('resume requires one explicit original session')
    return identifier(sessions[0])


def inspect_inputs(process):
    argv, env, cwd = process['argv'], process['environment'], process['cwd']
    settings, mcp, bindings = [], [], []
    for value in option_values(argv, '--settings'):
        row, data = json_input(value, cwd)
        row['cmux_managed'] = (isinstance(data.get('__cmux'), dict)
                               and data['__cmux'].get('managed') == 'claude-hooks')
        # Retain the entire original settings object: permissions, env, skills
        # and user hooks must not be lost while cmux regenerates its own hooks.
        settings.append(row)
    for value in option_values(argv, '--mcp-config', variadic=True):
        row, data = json_input(value, cwd)
        servers = data.get('mcpServers')
        if not isinstance(servers, dict):
            raise ValueError('MCP input lacks mcpServers')
        row['servers'] = list(servers)
        for name, server in servers.items():
            if not isinstance(server, dict):
                raise ValueError('invalid MCP server')
            server_env = server.get('env', {})
            if not isinstance(server_env, dict):
                raise ValueError('invalid MCP environment')
            if name == 'cmux-cua' or any(k.startswith('CMUX_CUA_') for k in server_env):
                owner = server_env.get('CMUX_CUA_STATE_OWNER_PID')
                args = server.get('args')
                exact = (name == 'cmux-cua' and owner == str(process['pid'])
                         and server_env.get('CMUX_CUA_DEFAULT_SESSION') == 'cmux-' + env['CMUX_SURFACE_ID']
                         and server_env.get('CMUX_CUA_MCP_FORCE_PROXY') == '1'
                         and server_env.get('CMUX_CUA_EXTERNAL_PERMISSION_FLOW') == '1'
                         and isinstance(args, list) and len(args) == 3 and args[:2] == ['mcp', '--socket']
                         and isinstance(server.get('command'), str) and Path(server['command']).is_absolute())
                bindings.append(dict(name=name, input_sha256=row['sha256'],
                    matches_original_owner=exact, action='regenerate_with_cmux' if exact else 'unresolved_binding'))
        mcp.append(row)
    compact = env.get('CMUX_AGENT_LAUNCH_ARGV_B64')
    compact_argv = None
    if compact:
        try:
            raw = base64.b64decode(compact, validate=True)
            if not raw.endswith(b'\0'):
                raise ValueError('unterminated compact argv')
            compact_argv = [part.decode('utf-8') for part in raw[:-1].split(b'\0')]
        except (ValueError, UnicodeError):
            compact_argv = None
    config_root = Path(env.get('CLAUDE_CONFIG_DIR') or Path(env['HOME']) / '.claude')
    if not config_root.is_absolute():
        config_root = Path(cwd) / config_root
    return dict(settings=settings, mcp=mcp, process_bound_mcp=bindings,
        compact_argv=compact_argv, compact_argv_is_authoritative=False,
        compact_omits_actual_settings=bool(settings and compact_argv is not None
            and not option_values(compact_argv, '--settings')),
        config_root=str(config_root), config_root_source='CLAUDE_CONFIG_DIR' if env.get('CLAUDE_CONFIG_DIR') else 'HOME',
        profile_inferred_from_root=False,
        wrapper_auth_preservation=dict(CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV='1',
            CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV_KEYS=''),
        environment_source='original_process_only')


def capture(expected, read_process):
    before = read_process(expected['pid'])
    if not before or before['birth'] != expected['birth']:
        raise ValueError('original process is absent or has changed')
    if identifier(before['environment'].get('CMUX_SURFACE_ID', '')) != identifier(expected['surface_id']):
        raise ValueError('original surface binding changed')
    if session_from_argv(before['argv']) != identifier(expected['session_id']):
        raise ValueError('original session binding changed')
    inputs = inspect_inputs(before)
    # Close both the slow-file-read and native-identity windows. This is a
    # bounded non-atomic capture; execution needs its own fresh live checks.
    for row in inputs['settings'] + inputs['mcp']:
        if row['kind'] == 'file' and read_file(row['path']) != {
                k: row[k] for k in ('path', 'resolved', 'path_identity', 'identity', 'sha256', 'raw_utf8')}:
            raise ValueError('configuration changed before capture completed')
    if read_process(expected['pid']) != before:
        raise ValueError('process changed before capture completed')
    return dict(version=1, state='captured_not_launched', expected=expected,
        original_process=before, inputs=inputs, executable_plan=False,
        launch_blockers=['fresh_live_surface_and_input_state_required',
            'original_process_must_exit_at_authorized_boundary',
            'revalidate_configuration_and_executable_before_resume'] +
            (['regenerate_process_bound_mcp'] if inputs['process_bound_mcp'] else []),
        observed_api_key=None, model_requests=0, terminal_inputs=0,
        limitations=['Configuration bytes are captured now, not proof of startup-loaded values.',
            'This record never authorizes a restart or a write to another surface.',
            'Actual request-key observation requires a subsequent instrumented process request.'])


def native_process(pid):
    import ccc_guard_scope as scope
    before = scope.birth(pid)
    if before is None:
        return None
    argv, env = scope.arguments(pid)
    library = ctypes.CDLL('/usr/lib/libproc.dylib')
    function = library.proc_pidpath
    function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    if function(pid, buffer, len(buffer)) <= 0:
        raise ValueError('native executable path unavailable')
    executable = Path(buffer.value.decode('utf-8'))
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise ValueError('native executable is unavailable')
    result = dict(pid=pid, birth=before, argv=argv, environment=env, cwd=scope.cwd(pid),
                  executable=str(executable), executable_identity=file_identity(info))
    if scope.birth(pid) != before:
        raise ValueError('native process identity changed')
    return result


def write_private(path, data):
    path = Path(path).absolute()
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError('output requires an existing owned private directory')
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        def check_parent():
            # Directory content changes affect timestamps/size; bind identity,
            # ownership and permissions, not those mutable metadata fields.
            identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
            if (identity(os.fstat(directory)) != identity(parent)
                    or identity(path.parent.lstat()) != identity(parent)):
                raise ValueError('output directory changed')

        check_parent()
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            check_parent()
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        check_parent()
    finally:
        os.close(directory)
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--birth', type=int, nargs=2, required=True)
    parser.add_argument('--surface-id', required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    expected = dict(pid=args.pid, birth=args.birth, surface_id=identifier(args.surface_id),
                    session_id=identifier(args.session_id))
    data = capture(expected, native_process)
    sha = write_private(args.output, data)
    print(json.dumps(dict(path=str(args.output.absolute()), sha256=sha,
                         state=data['state'], launched=False)))


if __name__ == '__main__':
    main()
