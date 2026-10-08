"""Resume a captured Claude in its own terminal, retaining its launch inputs.

Preparation never signals a process or writes terminal input. Application is
only possible from the original terminal after its Claude exits; the selected
cmux wrapper refreshes hooks/MCP while the original native executable is pinned.
No prompt is submitted and no credential is inferred from global configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import shlex
import sys
import secrets
import copy
import re

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import prepare_claude_observation_resume as capture_tool
from ccc_claude_launcher import observation_environment


def session_pin(root, session):
    """Require the original root's unique transcript; never search other roots."""
    root = Path(root).absolute()
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('unsafe original Claude config root')
    candidates = []
    for depth in range(1, 6):
        candidates.extend((root / 'projects').glob('*/' * depth + session + '.jsonl'))
    if len(candidates) != 1:
        raise ValueError('original root must contain exactly one original transcript')
    transcript = binary_pin(candidates[0])
    if not transcript['identity'][4] or not Path(transcript['resolved']).is_relative_to(root.resolve()):
        raise ValueError('original transcript is empty or outside original root')
    return dict(root=str(root), root_identity=[info.st_dev, info.st_ino, info.st_uid, info.st_mode],
                transcript=transcript)


def private_wrapper(source, destination, plan_path):
    """Narrow private adapter; leave cmux's installed wrapper unchanged."""
    source = Path(source).absolute()
    text = source.read_text()
    start, end = 'find_real_claude() {\n', '# Return 0 only when CMUX_SOCKET_PATH'
    if text.count(start) != 1 or text.count(end) != 1 or text.count('exec "$target" "$@"') != 2:
        raise ValueError('unrecognized cmux wrapper execution boundaries')
    begin, finish = text.index(start), text.index(end)
    text = text[:begin] + '''find_real_claude() {
    local custom="${CMUX_CUSTOM_CLAUDE_PATH:-}"
    [[ -n "$custom" && -f "$custom" && -x "$custom" ]] || return 1
    cmux_claude_wrapper_is_self_or_shim "$custom" && return 1
    printf '%s' "$custom"
}

''' + text[finish:]
    root_check = '    cmux_claude_config_dir_has_session "$current_root" "$sid" && return 0\n'
    if text.count(root_check) != 1:
        raise ValueError('unrecognized cmux resume root boundary')
    text = text.replace(root_check, root_check + '    echo "CCC: original session missing in original root" >&2; exit 78\n')
    # Bundled MCP/helpers still resolve relative to the original app directory.
    text = text.replace('$(dirname "$0")', '$(dirname ' + shlex.quote(str(source)) + ')')
    command = 'exec ' + ' '.join(shlex.quote(str(x)) for x in
        (sys.executable, '-B', Path(__file__).resolve(), '_exec', '--plan', plan_path))
    command += ' --plan-sha256 "$CCC_OBSERVATION_RESUME_SHA" -- "$target" "$@"'
    text = text.replace('exec "$target" "$@"', command)
    # Save the freshly generated cmux base before user settings are merged.
    # The final handoff reconstructs the expected merge from these bytes and
    # the original inputs, rather than trusting the resulting argv alone.
    anchor = '    CMUX_SETTINGS_PATH="$CMUX_SETTINGS_BASE_PATH"\n'
    if text.count(anchor) != 1:
        raise ValueError('unrecognized cmux settings base boundary')
    snapshot = ' '.join(shlex.quote(str(x)) for x in
        (sys.executable, '-B', Path(__file__).resolve(), '_capture_base', '--plan', plan_path))
    snapshot += ' --plan-sha256 "$CCC_OBSERVATION_RESUME_SHA" --base "$CMUX_SETTINGS_BASE_PATH"'
    text = text.replace(anchor, anchor + '    ' + snapshot + ' || exit 78\n')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    return binary_pin(destination)


def binary_pin(path):
    path = Path(path).absolute()
    link = capture_tool.file_identity(path.lstat())
    resolved = path.resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022):
            raise ValueError('unsafe executable or observer input')
        while block := stream.read(1024 * 1024):
            digest.update(block)
        if capture_tool.file_identity(os.fstat(stream.fileno())) != capture_tool.file_identity(info):
            raise ValueError('executable changed during read')
    if (capture_tool.file_identity(path.lstat()) != link or path.resolve(strict=True) != resolved
            or capture_tool.file_identity(resolved.stat()) != capture_tool.file_identity(info)):
        raise ValueError('executable pathname changed during read')
    return dict(path=str(path), resolved=str(resolved), path_identity=link,
                identity=capture_tool.file_identity(info), sha256=digest.hexdigest())


def verify_capture(record):
    if record.get('state') != 'captured_not_launched' or record.get('executable_plan') is not False:
        raise ValueError('expected an original non-executable capture')
    original, expected = record['original_process'], record['expected']
    if (original['pid'] != expected['pid'] or original['birth'] != expected['birth']
            or capture_tool.identifier(original['environment']['CMUX_SURFACE_ID']) != expected['surface_id'].lower()
            or capture_tool.session_from_argv(original['argv']) != expected['session_id'].lower()):
        raise ValueError('capture identity does not match original process')
    # Re-read original file inputs. Never silently use a stale copy after the
    # user changes settings or substitutes a pathname, even with equal bytes.
    if capture_tool.inspect_inputs(original) != record['inputs']:
        raise ValueError('original settings or MCP inputs changed')
    current = binary_pin(original['executable'])
    if current['identity'] != original['executable_identity']:
        raise ValueError('original native executable changed')
    if not os.access(current['path'], os.X_OK):
        raise ValueError('original native executable is not executable')
    return current


def transformed_arguments(record, directory):
    """Keep every user option; remove only an exactly attributed cmux server."""
    original, inputs = record['original_process'], record['inputs']
    if any(not binding['matches_original_owner'] for binding in inputs['process_bound_mcp']):
        raise ValueError('unresolved process-bound MCP input')
    rows = {'--settings': iter(inputs['settings']), '--mcp-config': iter(inputs['mcp'])}
    output, files = [], []
    argv = original['argv']
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            output.extend(argv[index:])
            break
        option = arg.split('=', 1)[0]
        if option not in rows:
            output.append(arg)
            index += 1
            continue
        count = 1
        if '=' not in arg:
            index += 1
            if option == '--mcp-config':
                end = index + 1
                while end < len(argv) and not argv[end].startswith('-'):
                    end += 1
                count = end - index
        for _ in range(count):
            row = next(rows[option])
            data = json.loads(row['raw_utf8'])
            if option == '--mcp-config':
                # Other servers and all top-level fields remain unchanged.
                if 'cmux-cua' in data['mcpServers']:
                    data['mcpServers'].pop('cmux-cua')
                if not data['mcpServers'] and set(data) == {'mcpServers'}:
                    if '=' not in arg:
                        index += 1
                    continue
            path = Path(directory) / ('settings-' if option == '--settings' else 'mcp-')
            path = path.with_name(path.name + str(len(files)) + '.json')
            capture_tool.write_private(path, data)
            files.append(capture_tool.read_file(path))
            # Single-token MCP is essential: the option is variadic in Claude.
            output.append(option + '=' + str(path))
            if '=' not in arg:
                index += 1
        if '=' in arg:
            index += 1
    for values in rows.values():
        if next(values, None) is not None:
            raise ValueError('unconsumed configuration input')
    if capture_tool.session_from_argv(['Claude', *output]) != record['expected']['session_id'].lower():
        raise ValueError('original resume session lost')
    return output, files


# These values describe an old wrapper pass or process, not user configuration.
STALE_WRAPPER_KEYS = ('CMUX_CLAUDE_PID', 'CMUX_AGENT_LAUNCH_ARGV_B64',
    'CMUX_AGENT_LAUNCH_EXECUTABLE', 'CMUX_AGENT_LAUNCH_CWD', 'CMUX_AGENT_LAUNCH_KIND',
    'CMUX_AGENT_RESTORE_LAUNCH', 'CMUX_CLAUDE_TEAMS_WRAPPER_LAUNCH',
    'cmux_claude_wrapper_reexec_guard', 'cmux_claude_wrapper_reexec_targets')


def prepare(record, directory, wrapper, observer, observations):
    executable = verify_capture(record)
    for row in record['inputs']['settings']:
        validate_merge_keys(json.loads(row['raw_utf8']))
    wrapper_pin, observer_pin = binary_pin(wrapper), binary_pin(observer)
    session = session_pin(record['inputs']['config_root'], record['expected']['session_id'].lower())
    if not os.access(wrapper_pin['path'], os.X_OK):
        raise ValueError('cmux wrapper is not executable')
    directory = Path(directory).absolute()
    directory.mkdir(mode=0o700, exist_ok=False)
    adapted = private_wrapper(wrapper, directory / 'cmux-claude-resume', directory / 'resume.json')
    args, files = transformed_arguments(record, directory)
    env = dict(record['original_process']['environment'])
    removed = {key: env.pop(key) for key in STALE_WRAPPER_KEYS if key in env}
    env.update(CMUX_CUSTOM_CLAUDE_PATH=executable['path'],
               CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV='1',
               CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV_KEYS='')
    # A stale token from a generated MCP object must not override the current
    # cmux-owned credential file. Its bytes are read by cmux at launch time.
    if record['inputs']['process_bound_mcp']:
        token_file = env.get('CMUX_CUA_AUTH_TOKEN_FILE')
        if not token_file or not Path(token_file).is_absolute():
            raise ValueError('fresh cmux MCP token file unavailable')
        token_info = Path(token_file).lstat()
        if (not stat.S_ISREG(token_info.st_mode) or token_info.st_uid != os.getuid()
                or stat.S_IMODE(token_info.st_mode) != 0o600):
            raise ValueError('unsafe cmux MCP token file')
        env.pop('CMUX_CUA_SOCKET_AUTH_TOKEN', None)
    env = observation_environment(env, observer_pin['path'], observations)
    # Preserve absence as well as value: explicitly setting the default directory
    # changes Claude's global config path from ~/.claude.json to
    # ~/.claude/.claude.json. The resolved transcript root is pinned separately.
    claim_root = Path(observations).absolute() / 'resume-claims'
    claim_root.mkdir(mode=0o700, exist_ok=True)
    claim_identity = hashlib.sha256(json.dumps(record['expected'], sort_keys=True).encode()).hexdigest()
    dependencies = [binary_pin(p) for p in (Path(__file__).resolve(),
        capture_tool.__file__, ROOT / 'ccc_claude_launcher.py', sys.executable)]
    bundled = Path(wrapper).absolute().parent.parent.parent / 'Resources' / 'bin'
    for name in ('cmux', 'cmux-cua'):
        if (bundled / name).exists():
            dependencies.append(binary_pin(bundled / name))
    plan = dict(version=1, state='prepared_not_launched', expected=record['expected'],
        original_process=record['original_process'], original_inputs=record['inputs'],
        cwd=record['original_process']['cwd'], argv=[adapted['path'], *args],
        environment=env, pins=[executable, wrapper_pin, observer_pin, adapted, *dependencies], generated_inputs=files,
        session=session, plan_path=str(directory / 'resume.json'),
        claim_path=str(claim_root / (claim_identity + '.json')),
        removed_wrapper_keys=sorted(removed), observed_api_key=None,
        terminal_inputs=0, model_requests=0,
        application_boundary='original terminal, original process exited, no other agent; no automatic stop')
    capture_tool.write_private(directory / 'resume.json', plan)
    return plan


def revalidate(plan):
    record = dict(state='captured_not_launched', executable_plan=False, expected=plan['expected'],
                  original_process=plan['original_process'], inputs=plan['original_inputs'])
    verify_capture(record)
    if session_pin(plan['session']['root'], plan['expected']['session_id'].lower()) != plan['session']:
        raise ValueError('original session or config root changed')
    for pin in plan['pins']:
        if binary_pin(pin['path']) != pin:
            raise ValueError('pinned executable, wrapper or observer changed')
    for row in plan['generated_inputs']:
        if capture_tool.read_file(row['path']) != row:
            raise ValueError('prepared configuration changed')


def durable_claim(path, data):
    sha = capture_tool.write_private(path, data)
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return sha


def apply(plan, boundary, *, execute=os.execve):
    """Callers supply a fresh read-only boundary; do not stop/paste/press Enter."""
    if plan.get('state') != 'prepared_not_launched':
        raise ValueError('plan was not prepared')
    boundary(plan)
    plan_file = capture_tool.read_file(plan['plan_path'])
    if json.loads(plan_file['raw_utf8']) != plan:
        raise ValueError('plan changed before launch')
    revalidate(plan)
    boundary(plan)
    token = secrets.token_hex(24)
    # An exception or unknown exec outcome consumes the attempt too. The same
    # original PID/session cannot be launched again via a copy of this plan.
    durable_claim(plan['claim_path'], dict(state='launch_claimed', pid=os.getpid(),
        token=token, plan_sha256=plan_file['sha256'], expected=plan['expected']))
    env = dict(plan['environment'], CCC_OBSERVATION_RESUME_SHA=plan_file['sha256'],
               CCC_OBSERVATION_RESUME_TOKEN=token)
    # Claim fsync can be slow too. A rejected final check retains this claim.
    boundary(plan)
    previous = os.getcwd()
    try:
        os.chdir(plan['cwd'])
        execute(plan['argv'][0], plan['argv'], env)
    finally:
        os.chdir(previous)


def exec_native(plan, argv, boundary, *, environment=None, execute=os.execve):
    """Last wrapper handoff: exact executable, session, root and single use."""
    env = dict(os.environ if environment is None else environment)
    claim = json.loads(capture_tool.read_file(plan['claim_path'])['raw_utf8'])
    if (claim['pid'] != os.getpid() or claim['token'] != env.get('CCC_OBSERVATION_RESUME_TOKEN')
            or claim['plan_sha256'] != env.get('CCC_OBSERVATION_RESUME_SHA')):
        raise ValueError('native handoff is not the original launch attempt')
    identity_checks = {
        'executable': bool(argv) and argv[0] == plan['pins'][0]['path'],
        'session': capture_tool.session_from_argv(argv) == plan['expected']['session_id'].lower(),
        'config root': all(
            (key in env) == (key in plan['original_process']['environment'])
            and env.get(key) == plan['original_process']['environment'].get(key)
            for key in ('CLAUDE_CONFIG_DIR', 'HOME')),
        'surface': env.get('CMUX_SURFACE_ID') == plan['environment'].get('CMUX_SURFACE_ID'),
    }
    for field, matches in identity_checks.items():
        if not matches:
            raise ValueError('wrapper changed original ' + field)
    for key in ('CCC_CLAUDE_REQUEST_OBSERVER', 'CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR', 'BUN_OPTIONS'):
        if env.get(key) != plan['environment'].get(key):
            raise ValueError('wrapper lost request observation')
    auth_keys = {key for key in (*env, *plan['environment']) if key.startswith(
        ('ANTHROPIC_', 'AWS_', 'GOOGLE_', 'CLAUDE_CODE_USE_'))}
    if any(env.get(key) != plan['environment'].get(key) for key in auth_keys):
        raise ValueError('wrapper changed original authentication environment')
    revalidate(plan)
    final_inputs = validate_final_inputs(plan, argv, env)
    boundary(plan)
    durable_claim(plan['claim_path'] + '.native', dict(state='native_exec_claimed', pid=os.getpid(),
        plan_sha256=claim['plan_sha256']))
    if validate_final_inputs(plan, argv, env) != final_inputs:
        raise ValueError('final settings or MCP changed during native claim')
    boundary(plan)
    env.pop('CCC_OBSERVATION_RESUME_TOKEN', None)
    env.pop('CCC_OBSERVATION_RESUME_SHA', None)
    execute(argv[0], argv, env)


def capture_base(plan, path):
    """Private wrapper checkpoint; never authorizes a client or model launch."""
    claim = json.loads(capture_tool.read_file(plan['claim_path'])['raw_utf8'])
    if (claim['pid'] != os.getppid()
            or claim['token'] != os.environ.get('CCC_OBSERVATION_RESUME_TOKEN')
            or claim['plan_sha256'] != os.environ.get('CCC_OBSERVATION_RESUME_SHA')):
        raise ValueError('settings checkpoint is not the original wrapper')
    row = capture_tool.read_file(path)
    if not isinstance(json.loads(row['raw_utf8']), dict):
        raise ValueError('invalid generated cmux base')
    durable_claim(plan['claim_path'] + '.base', dict(pid=claim['pid'],
        plan_sha256=claim['plan_sha256'], base=row))


def validate_merge_keys(value):
    """Reject keys the pinned JS merger cannot preserve, before preparation."""
    if isinstance(value, dict):
        if '__proto__' in value:
            raise ValueError('cmux settings merger cannot preserve __proto__; original settings unchanged')
        for child in value.values():
            validate_merge_keys(child)
    elif isinstance(value, list):
        for child in value:
            validate_merge_keys(child)


def json_equal(left, right):
    """Compare JSON values without Python's boolean/number coercion.

    JSON has one number kind: 1 and 1.0 survive JS roundtrips as the same
    value. Booleans must remain distinct, including in nested user hooks/MCP.
    """
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    return left == right


def merge_settings(base, originals):
    """Reconstruct cmux's documented merge, retaining user hooks and options."""
    validate_merge_keys(base)
    for original in originals:
        validate_merge_keys(original)
    def canonical(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    def merge(a, b):
        if isinstance(a, list) and isinstance(b, list):
            return a + b
        if isinstance(a, dict) and isinstance(b, dict):
            out = copy.deepcopy(a)
            for key, value in b.items():
                out[key] = merge(a[key], value) if key in a else copy.deepcopy(value)
            return out
        return copy.deepcopy(a if isinstance(a, (dict, list)) else b)
    result = copy.deepcopy(base)
    base_hooks = base.get('hooks', {})
    for original in reversed(originals):
        data = copy.deepcopy(original)
        marker = data.get('__cmux', {})
        fingerprints = marker.get('hookFingerprints', []) if marker.get('managed') == 'claude-hooks' else []
        for event, groups in data.get('hooks', {}).items():
            if isinstance(groups, list):
                base_groups = base_hooks.get(event, [])
                data['hooks'][event] = [g for g in groups
                    if not any(json_equal(g, base_group) for base_group in base_groups)
                    and canonical(g) not in fingerprints]
        result = merge(result, data)
    if originals:
        marker = copy.deepcopy(base.get('__cmux', {'managed':'claude-hooks', 'version':1}))
        marker['hookFingerprints'] = [canonical(g) for groups in base_hooks.values() for g in groups]
        result['__cmux'] = marker
    return result


def ordinary_arguments(argv):
    output, index = [], 1
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            return output + argv[index:]
        option = arg.split('=', 1)[0]
        if option not in ('--settings', '--mcp-config'):
            output.append(arg)
        elif '=' not in arg:
            index += 1
            if index >= len(argv) or argv[index].startswith('-'):
                raise ValueError('missing configuration option value')
            if option == '--mcp-config':
                while index + 1 < len(argv) and not argv[index + 1].startswith('-'):
                    index += 1
        index += 1
    return output


def expected_managed_mcp(plan, snapshots):
    """Rebuild the pinned wrapper's exact managed server from launch inputs."""
    env = plan['environment']
    client = Path(plan['pins'][1]['path']).parent.parent.parent / 'Resources/bin/cmux-cua'
    pin = binary_pin(client)
    if pin not in plan['pins'] or not os.access(client, os.X_OK):
        raise ValueError('cmux MCP executable is not the pinned bundled client')
    snapshots.append(pin)
    token = ''
    token_path = env.get('CMUX_CUA_AUTH_TOKEN_FILE', '')
    if token_path and Path(token_path).is_absolute():
        info = Path(token_path).lstat()
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600):
            row = capture_tool.read_file(token_path)
            snapshots.append(row)
            # Match IFS= read -r, not strip(): spaces and CR are token bytes.
            token = row['raw_utf8'].split('\n', 1)[0]
    token = token or env.get('CMUX_CUA_SOCKET_AUTH_TOKEN', '')
    if not token:
        raise ValueError('cmux MCP credential unavailable')
    scope = env.get('CMUX_CUA_RUNTIME_SCOPE') or env.get('CMUX_TAG') or 'default'
    scope = re.sub(rb'[^A-Za-z0-9_.-]', b'-', scope.encode()).decode().strip('.-')[:64] or 'default'
    state_dir = env.get('CMUX_CUA_STATE_DIR') or (
        env['HOME'] + '/Library/Application Support/cmux/cmux-cua/runtime/' + scope + '/state')
    socket_path = env.get('CMUX_CUA_SOCKET_PATH') or f'/tmp/cmux-cua-{os.getuid()}/{scope}/cmux-cua.sock'
    return dict(command=str(client), args=['mcp', '--socket', socket_path], env={
        'CMUX_CUA_MCP_FORCE_PROXY': '1', 'CMUX_CUA_EXTERNAL_PERMISSION_FLOW': '1',
        'CMUX_CUA_SOCKET_AUTH_TOKEN': token,
        'CMUX_CUA_DEFAULT_SESSION': 'cmux-' + env['CMUX_SURFACE_ID'],
        'CMUX_CUA_STATE_OWNER_PID': str(os.getpid()),
        'CMUX_CUA_TELEMETRY_ENABLED': 'false', 'CMUX_CUA_UPDATE_CHECK': 'false',
        'CMUX_CUA_CURSOR_GRADIENT': '#12c7f5,#2d8cff,#6c5cff',
        'CMUX_CUA_CURSOR_BLOOM': '#2d8cff', 'CMUX_CUA_CURSOR_LABEL': 'cmux',
        'CMUX_CUA_STATE_DIR': state_dir, 'NODE_OPTIONS': '', 'BUN_OPTIONS': '',
    })


def validate_final_inputs(plan, argv, env):
    if ordinary_arguments(argv) != ordinary_arguments(plan['argv']):
        raise ValueError('wrapper changed original non-managed arguments')
    snapshots = []
    def inputs(args, name, variadic=False):
        result = []
        for value in capture_tool.option_values(args, name, variadic=variadic):
            row, data = capture_tool.json_input(value, plan['cwd'])
            snapshots.append(row)
            result.append(data)
        return result
    original_settings = [json.loads(row['raw_utf8']) for row in plan['original_inputs']['settings']]
    final_settings = inputs(argv, '--settings')
    base_path = Path(plan['claim_path'] + '.base')
    if base_path.exists():
        row = capture_tool.read_file(base_path)
        snapshots.append(row)
        saved = json.loads(row['raw_utf8'])
        if saved['pid'] != os.getpid() or saved['plan_sha256'] != env['CCC_OBSERVATION_RESUME_SHA']:
            raise ValueError('cmux base checkpoint belongs to another launch')
        expected = [merge_settings(json.loads(saved['base']['raw_utf8']), original_settings)]
    else:
        # Wrapper's no-hooks fallback must preserve the original user settings.
        expected = original_settings
    if not json_equal(final_settings, expected):
        raise ValueError('wrapper changed original settings, permissions or user hooks')
    expected_mcps = inputs(plan['argv'], '--mcp-config', True)
    final_mcps = inputs(argv, '--mcp-config', True)
    if not json_equal(final_mcps, expected_mcps):
        if len(final_mcps) != len(expected_mcps) + 1 or not json_equal(final_mcps[1:], expected_mcps):
            raise ValueError('wrapper changed original user MCP configuration')
        added = final_mcps[0]
        if set(added) != {'mcpServers'} or set(added['mcpServers']) != {'cmux-cua'}:
            raise ValueError('wrapper added an unrecognized MCP server')
        server = added['mcpServers']['cmux-cua']
        if not json_equal(server, expected_managed_mcp(plan, snapshots)):
            raise ValueError('new cmux MCP changed its command, route, credential or process binding')
    elif plan['original_inputs']['process_bound_mcp']:
        raise ValueError('wrapper lost the original managed MCP')
    return snapshots


def local_terminal_boundary(plan):
    """No remote terminal writes: this tool must itself run in the old PTY."""
    import ccc_guard_scope as scope
    import ccc_observation as observation
    from cmux_codex_watch import CmuxClient
    if not os.isatty(0) or not os.isatty(1):
        raise ValueError('resume requires the original interactive terminal')
    expected = plan['expected']
    if capture_tool.identifier(os.environ.get('CMUX_SURFACE_ID', '')) != expected['surface_id'].lower():
        raise ValueError('resume cannot run from another surface')
    if scope.birth(expected['pid']) is not None:
        raise ValueError('original PID still exists; never interrupt or replace it')
    client = CmuxClient()
    identity = client.identify().get('caller', {})
    surface = identity.get('surface_id') or identity.get('surface_uuid')
    if not surface or capture_tool.identifier(surface) != expected['surface_id'].lower():
        raise ValueError('live cmux caller does not match original surface')
    entries = [(entry, workspace) for entry, workspace in observation.surface_entries(client.top_all())
               if str(entry.get('id') or entry.get('surface_id') or '').lower() == expected['surface_id'].lower()]
    if len(entries) != 1:
        raise ValueError('live surface is absent or ambiguous')
    processes = entries[0][0].get('processes')
    if not isinstance(processes, list) or not processes:
        raise ValueError('live surface process snapshot unavailable')
    # Require this invocation and only its parent chain. Other foreground or
    # background clients prevent a launch, even when the UI looks like a shell.
    if str(entries[0][0].get('tty', '')).removeprefix('/dev/') != os.ttyname(0).removeprefix('/dev/'):
        raise ValueError('original surface TTY changed')
    pids = {p['pid'] for p in observation.objects(processes)
            if p.get('kind') == 'process' and type(p.get('pid')) is int}
    if os.getpid() not in pids:
        raise ValueError('current process is not in the live surface')
    # cmux top may omit an intermediate shell; use OS parentage rather than
    # requiring its UI inventory to contain every ancestor.
    raw = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid='], check=True,
                         capture_output=True, text=True, timeout=3).stdout
    parents = {int(a): int(b) for a, b in (line.split() for line in raw.splitlines())}
    chain, pid = set(), os.getpid()
    while pid in parents and pid not in chain:
        chain.add(pid)
        pid = parents[pid]
    if any(pid not in chain and scope.birth(pid) is not None for pid in pids):
        raise ValueError('another process remains in the original terminal')
    if not Path(plan['cwd']).is_dir():
        raise ValueError('original working directory unavailable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    build = sub.add_parser('prepare')
    build.add_argument('--capture', type=Path, required=True)
    build.add_argument('--capture-sha256', required=True)
    build.add_argument('--output-directory', type=Path, required=True)
    build.add_argument('--wrapper', type=Path, required=True)
    build.add_argument('--observer', type=Path, required=True)
    build.add_argument('--observations', type=Path, required=True)
    launch = sub.add_parser('apply')
    launch.add_argument('--plan', type=Path, required=True)
    launch.add_argument('--plan-sha256', required=True)
    native = sub.add_parser('_exec', help=argparse.SUPPRESS)
    native.add_argument('--plan', type=Path, required=True)
    native.add_argument('--plan-sha256', required=True)
    native.add_argument('native_argv', nargs=argparse.REMAINDER)
    base = sub.add_parser('_capture_base', help=argparse.SUPPRESS)
    base.add_argument('--plan', type=Path, required=True)
    base.add_argument('--plan-sha256', required=True)
    base.add_argument('--base', type=Path, required=True)
    args = parser.parse_args()
    path = args.capture if args.action == 'prepare' else args.plan
    row = capture_tool.read_file(path)
    expected_sha = args.capture_sha256 if args.action == 'prepare' else args.plan_sha256
    if row['sha256'] != expected_sha:
        raise ValueError('input differs from the reviewed SHA256')
    data = json.loads(row['raw_utf8'])
    if args.action == 'prepare':
        plan = prepare(data, args.output_directory, args.wrapper, args.observer, args.observations)
        output = args.output_directory / 'resume.json'
        print(json.dumps(dict(path=str(output.absolute()), sha256=capture_tool.read_file(output)['sha256'],
                              state=plan['state'], launched=False)))
    elif args.action == 'apply':
        apply(data, local_terminal_boundary)
    elif args.action == '_capture_base':
        capture_base(data, args.base)
    else:
        argv = args.native_argv
        if argv[:1] == ['--']:
            argv = argv[1:]
        exec_native(data, argv, local_terminal_boundary)


if __name__ == '__main__':
    main()
