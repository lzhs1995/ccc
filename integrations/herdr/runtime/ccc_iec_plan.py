# 纯函数候选 v2（基于 V116 e3ebab74）：IEC 压缩失败后的同会话 resume 计划；不启动进程、不联网、不写共享状态
import hashlib, os, re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path
from urllib.parse import urlsplit

from ccc_encrypted_recovery import same_session, structured_error_type  # 已装 runtime 的现成判定

APPROVED_LOCAL = 'http://127.0.0.1:18790/v1'
FLAGS_WITH_VALUE = {'-m', '--model', '-c', '--config', '-p', '--profile', '-C', '--cd', '-s', '--sandbox',
                    '-a', '--ask-for-approval', '--disable', '--enable', '-i', '--image', '--add-dir'}
FLAGS_BARE = {'--full-auto', '--dangerously-bypass-approvals-and-sandbox', '--search', '--oss', '--no-alt-screen'}
PROVIDER_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
REQUIRED_ID = ('session_id', 'surface_id', 'workspace_id', 'pid', 'process_start', 'turn_id')


class Refused(ValueError):
    pass


def _need(cond, why):
    if not cond:
        raise Refused(why)


def _config_value(value, provider):
    key, _, val = value.partition('=')
    key = key.strip()
    _need(all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', part) for part in key.split('.')),
          'unsupported config override key')
    _need(not key.endswith('base_url'), 'original argv already overrides a base_url')
    if key == 'model_provider':
        _need(_provider_value(val) == provider, 'argv provider selection differs from supplied provider')


def _split(argv, sid, provider):
    """Keep known flags verbatim; strip one `resume <same SID>`; refuse anything else (incl. options after resume)."""
    out, i = [argv[0]], 1
    while i < len(argv):
        a = argv[i]
        name, eq, inline = a.partition('=')
        if a in FLAGS_WITH_VALUE:
            _need(i + 1 < len(argv), 'flag without value: ' + a)
            if a in ('-c', '--config'):
                _config_value(argv[i + 1], provider)
            out += [a, argv[i + 1]]; i += 2; continue
        if eq and name in FLAGS_WITH_VALUE:                       # --config=k=v / -c=k=v
            if name in ('-c', '--config'):
                _config_value(inline, provider)
            out.append(a); i += 1; continue
        if a.startswith('-c') and len(a) > 2 and not a.startswith('--'):   # -ck=v
            _config_value(a[2:], provider); out.append(a); i += 1; continue
        if a.startswith('-p') and len(a) > 2 and not a.startswith('--'):
            out.append(a); i += 1; continue
        if a in FLAGS_BARE:
            out.append(a); i += 1; continue
        _need(not a.startswith('-'), 'unrecognized flag: ' + a)
        _need(a == 'resume', 'unsafe positional/subcommand: ' + a)
        _need(argv[i + 1:] == [sid], 'resume must be last and name exactly the original SID')
        break
    return out


def _route_ok(route, provider_base_url):
    _need(route == APPROVED_LOCAL, 'route is not the approved local repair endpoint')
    u = urlsplit(provider_base_url or '')
    _need(u.scheme == 'https' and u.hostname == 'anyrouter.top' and u.port is None, 'original upstream is not anyrouter')
    _need(u.username is None and u.password is None and not u.query and not u.fragment, 'userinfo/query/fragment')
    _need(u.path in ('/v1', '/v1/') and u.netloc == 'anyrouter.top', 'unexpected path or authority')


def build_plan(capture, record, turn, current, eof_proof, provider, route, executable_pin, state):
    """capture=scope.process(pid,launch=True); record=raw failed store record; turn=raw native task_complete;
    current=authenticated current_turn identity; eof_proof=caller-authenticated EOF; provider={'name','base_url'}."""
    ident = record.get('identity') or {}
    _need(all(ident.get(k) for k in REQUIRED_ID), 'record identity incomplete')
    sid = ident['session_id']
    _need(UUID_RE.match(sid), 'bad session id')
    _need(record.get('phase') == 'failed' and record.get('compact_turn'), 'record is not a failed compact')
    _need(same_session(record, current), 'current turn is not the recorded session identity')
    for k in ('pid', 'process_start'):
        _need(capture.get(k) == ident[k] == current.get(k), 'binding mismatch: ' + k)
    _need(capture.get('birth') == current.get('birth') and ident.get('birth', current.get('birth')) == current.get('birth'),
          'binding mismatch: birth')
    _need(capture.get('surface_id') == ident['surface_id'], 'binding mismatch: surface')
    _need(capture.get('environment_workspace_id') == ident['workspace_id'], 'binding mismatch: workspace')
    _need(not capture.get('remote') and not capture.get('backend'), 'remote/backend client')
    err = turn.get('error') if isinstance(turn.get('error'), dict) else {}
    _need(turn.get('kind') == 'task_complete' and turn.get('turn_id') == record['compact_turn'], 'turn is not the failed compact turn')
    _need(structured_error_type(err.get('message')) == 'invalid_encrypted_content', 'turn error is not strict IEC')
    _need(eof_proof.get('authenticated') is True and eof_proof.get('path') == record.get('path')
          and list(eof_proof.get('file_id') or []) == list(record.get('file_id') or []), 'EOF proof does not match record')
    for flag in ('draft', 'paused', 'aborted', 'active_user_input', 'unknown_delivery'):
        _need(not state.get(flag), 'state forbids repair: ' + flag)
    env = dict(capture['environment'])
    _need(env.get('CMUX_SURFACE_ID', '').upper() == capture['surface_id'], 'env surface mismatch')
    _need(env.get('CMUX_WORKSPACE_ID', '').upper() == capture['environment_workspace_id'], 'env workspace mismatch')
    _need(PROVIDER_RE.match(provider.get('name') or ''), 'invalid provider key')
    _route_ok(route, provider.get('base_url'))
    argv0 = capture['argv'][0]
    target = os.path.realpath(argv0)
    _need(os.path.isfile(target) and os.access(target, os.X_OK), 'executable missing or not executable')
    st = os.stat(target)
    _need([st.st_dev, st.st_ino] == list(executable_pin['file_id']), 'executable file identity changed')
    with open(target, 'rb') as f:
        _need(hashlib.sha256(f.read()).hexdigest() == executable_pin['sha256'], 'executable content changed')
    base = _split(list(capture['argv']), sid, provider['name'])
    override = 'model_providers.%s.base_url="%s"' % (provider['name'], route)
    return {'executable': target, 'argv': base + ['-c', override, 'resume', sid], 'env': dict(env, PWD=capture['cwd']),
            'cwd': capture['cwd'], 'session_id': sid, 'surface_id': capture['surface_id'], 'new_session_id': False}


def _provider_value(raw):
    """Native -c accepts TOML values, with an unquoted string fallback."""
    try:
        value = tomllib.loads('value=' + raw)['value']
    except (ValueError, KeyError):
        value = raw.strip()
    _need(isinstance(value, str) and PROVIDER_RE.fullmatch(value), 'invalid provider selection')
    return value


def resolve_provider(capture):
    """Authenticate the conservative supported config shape; no routing write.

    Existing project/managed/env provider overrides are refused. Their absence
    is retained and rechecked by the launcher, including every cwd ancestor.
    """
    from ccc_request_recovery import file_pin, file_matches
    env, argv = capture['environment'], capture['argv']
    home = env.get('CODEX_HOME') or str(Path(env.get('HOME', '')) / '.codex')
    _need(Path(home).is_absolute(), 'original CODEX_HOME is not absolute')
    config = Path(home) / 'config.toml'
    config = config.absolute()
    pin = file_pin(config)
    raw = config.read_bytes()
    _need(hashlib.sha256(raw).hexdigest() == pin['sha256'] and file_matches(pin),
          'original config changed during provider resolution')
    cfg = tomllib.loads(raw.decode('utf-8'))
    _need(isinstance(cfg, dict), 'invalid original config')
    absent = []
    extra_pins = []
    directory = Path(capture['cwd']).resolve(strict=True)
    managed = [Path('/etc/codex/config.toml'), Path('/Library/Application Support/Codex/config.toml')]
    for candidate in [directory / '.codex/config.toml',
                      *(parent / '.codex/config.toml' for parent in directory.parents), *managed]:
        if candidate == config or candidate.resolve() == config.resolve():
            continue
        if candidate.exists() or candidate.is_symlink():
            # A comments-only project placeholder contributes no TOML values.
            # Keep its exact file pin for the original launcher's final check.
            _need(candidate not in managed and not candidate.is_symlink(),
                  'project or managed config inclusion is not authenticated')
            extra_pin = file_pin(candidate)
            extra_raw = candidate.read_bytes()
            _need(hashlib.sha256(extra_raw).hexdigest() == extra_pin['sha256']
                  and file_matches(extra_pin),
                  'original project config changed during provider resolution')
            _need(tomllib.loads(extra_raw.decode('utf-8')) == {},
                  'project or managed config inclusion is not authenticated')
            extra_pins.append(extra_pin)
            continue
        absent.append(str(candidate))
    for key in env:
        _need(not key.endswith('_BASE_URL') and key not in {'OPENAI_API_BASE',
              'CODEX_PROFILE', 'CODEX_MODEL_PROVIDER', 'CODEX_CONFIG_OVERRIDES', 'CODEX_CONFIG'},
              'original environment overrides provider configuration')
    profile, selected = None, None
    options, i = list(argv), 1
    while i < len(options):
        item = options[i]
        if item == 'resume':
            break
        override = None
        if item in {'-c', '--config', '-p', '--profile'}:
            _need(i + 1 < len(options), 'original config/profile flag has no value')
            value = options[i + 1]
            if item in {'-p', '--profile'}:
                _need(profile is None, 'multiple original profiles')
                profile = value
            else:
                override = value
            i += 2
        elif item.startswith('--config=') or item.startswith('-c') and not item.startswith('--'):
            override = item.split('=', 1)[1] if item.startswith('--config=') or item.startswith('-c=') else item[2:]
            i += 1
        elif item.startswith('--profile=') or item.startswith('-p') and not item.startswith('--'):
            _need(profile is None, 'multiple original profiles')
            profile = item.split('=', 1)[1] if '=' in item else item[2:]
            i += 1
        else:
            i += 1
        if override is not None:
            key, sep, value = override.partition('=')
            key = key.strip()
            _need(sep and all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', part) for part in key.split('.')),
                  'unsupported original config override')
            _need(not key.endswith('base_url') and key.split('.')[0] not in {'model_providers', 'profiles', 'profile'},
                  'original argv overrides provider/profile routing')
            if key == 'model_provider':
                _need(selected is None, 'multiple original provider selections')
                selected = _provider_value(value)
    profile = profile if profile is not None else cfg.get('profile')
    prof = {}
    if profile is not None:
        _need(isinstance(profile, str) and isinstance(cfg.get('profiles'), dict)
              and isinstance(cfg['profiles'].get(profile), dict), 'original profile unavailable')
        prof = cfg['profiles'][profile]
        _need(not any(key in prof for key in ('model_providers', 'base_url', 'profile')),
              'unsupported original profile routing')
    name = selected or prof.get('model_provider') or cfg.get('model_provider')
    _need(isinstance(name, str) and PROVIDER_RE.fullmatch(name), 'no authenticated explicit provider')
    providers = cfg.get('model_providers', {})
    _need(isinstance(providers, dict) and isinstance(providers.get(name), dict), 'original provider table missing')
    base = providers[name].get('base_url')
    _route_ok(APPROVED_LOCAL, base)
    return {'name': name, 'base_url': base, 'profile': profile}, [pin, *extra_pins], absent
