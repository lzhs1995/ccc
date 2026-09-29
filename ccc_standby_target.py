"""An explicit original native invocation and its per-slot observed route.

Native retains profile/model/skills/permissions. Only the selected provider's
base_url changes, to the local listener assigned to that slot. Upstream proxy
selection uses the original environment before adding native loopback bypass.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment

from ccc_native_standby import COUNT
from ccc_standby_sources import _launch_options


def normalize(value):
    if (not isinstance(value, dict)
            or set(value) != {'argv', 'provider', 'upstream_url', 'route_urls'}):
        raise ValueError('explicit original native target required')
    value = copy.deepcopy(value)
    argv, provider, urls = value['argv'], value['provider'], value['route_urls']
    if (not isinstance(argv, list) or not isinstance(provider, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]+', provider)):
        raise ValueError('invalid original provider or argv')
    # This launch gets its cwd from the immutable job. Prompts, subcommands,
    # resume and extra working-directory selectors are never inherited.
    docs, _ = _launch_options(argv, Path('/__ccc_explicit_target__'))
    for document in docs:
        if 'model_provider' in document and document['model_provider'] != provider:
            raise ValueError('target provider differs from original CLI selection')
        original_url = document.get('model_providers', {}).get(provider, {}).get('base_url')
        if original_url is not None and original_url != value['upstream_url']:
            raise ValueError('target URL differs from original CLI provider')
    if not isinstance(value['upstream_url'], str):
        raise ValueError('original HTTPS provider endpoint required')
    parsed = urlsplit(value['upstream_url'])
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or '\\' in value['upstream_url'] or any(ord(c) <= 32 for c in value['upstream_url'])):
        raise ValueError('original HTTPS provider endpoint required')
    parsed.port
    if (not isinstance(urls, list) or len(urls) != COUNT
            or not all(isinstance(url, str) for url in urls) or len(set(urls)) != COUNT):
        raise ValueError('one unique local route per original slot required')
    for index, url in enumerate(urls):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or not re.fullmatch(r'/[a-f0-9]{48}/' + str(index), parsed.path)):
            raise ValueError('route does not bind its original slot')
    if len({urlsplit(url).netloc for url in urls}) != 1:
        raise ValueError('cohort routes require one original observer')
    return value


def slot_argv(target, index):
    target = normalize(target)
    if type(index) is not int or not 0 <= index < COUNT:
        raise ValueError('invalid original native slot')
    return [*target['argv'], '-c', 'model_providers.' + target['provider'] +
            '.base_url=' + json.dumps(target['route_urls'][index])]


def network_environment(environment, upstream_url):
    """Keep original upstream proxy selection separate from native bypass.

No ambient environment, system proxy discovery or global environment writes.
Lowercase variables take precedence, including an explicit empty setting.
"""
    proxies = {}
    for key, value in environment.items():
        if key.lower().endswith('_proxy') and value:
            proxies[key.lower()[:-6]] = value
    for key, value in environment.items():
        if key.endswith('_proxy'):
            if value:
                proxies[key[:-6]] = value
            else:
                proxies.pop(key[:-6], None)
    upstream = urlsplit(upstream_url)
    proxy = None if proxy_bypass_environment(upstream.netloc, proxies) else (
        proxies.get(upstream.scheme) or proxies.get('all'))
    if proxy:
        selected = urlsplit(proxy)
        if selected.scheme != 'http' or not selected.hostname:
            raise ValueError('selected original proxy requires HTTP CONNECT support')
    result = dict(environment)
    prior = environment.get('no_proxy', environment.get('NO_PROXY', ''))
    bypass = ','.join(filter(None, (prior, '127.0.0.1')))
    result['NO_PROXY'] = result['no_proxy'] = bypass
    if environment.get('SSL_CERT_DIR'):
        raise ValueError('original certificate directory requires observer support')
    # Native custom_ca prefers its dedicated setting; an empty value falls
    # through to the generic certificate file.
    ca = environment.get('CODEX_CA_CERTIFICATE') or environment.get('SSL_CERT_FILE') or None
    return result, proxy, ca


def _merge(left, right):
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            _merge(left[key], value)
        else:
            left[key] = copy.deepcopy(value)


def _document(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ValueError('original route config link is broken')
        return {}
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024**2:
            raise ValueError('original route config must be a bounded file')
        raw = stream.read(8 * 1024**2 + 1)
        after = os.fstat(stream.fileno())
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if (len(raw) > 8 * 1024**2 or stamp(before) != stamp(after)
            or stamp(after) != stamp(path.stat())):
        raise ValueError('original route config changed during read')
    return tomllib.loads(raw.decode())


def _route(config):
    provider = config.get('model_provider', 'openai')
    if not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', provider):
        raise ValueError('invalid original provider selection')
    entry = config.get('model_providers', {}).get(provider, {})
    if not isinstance(entry, dict):
        raise ValueError('original provider table required')
    url = entry.get('base_url')
    if not isinstance(url, str) or not url:
        raise ValueError('original selected provider requires an explicit base URL')
    if entry.get('base_url_env_key') or entry.get('websocket_base_url'):
        raise ValueError('separate native provider endpoint needs route support')
    return provider, url


def select_provider(argv, environment, *, cwd=None, system_dir=Path('/etc/codex')):
    """Resolve local system/user/profile/CLI routing, preserving native flags.

    Project and managed route overrides must agree with that selection. This
    avoids guessing project trust or silently choosing another endpoint. It
    is a local route check, not a native effective-config export.
    """
    cwd = Path(cwd) if cwd is not None else None
    overrides, profiles = _launch_options(argv, cwd or Path('/__ccc_explicit_target__'))
    home = Path(environment.get('CODEX_HOME') or str(Path(environment['HOME']) / '.codex'))
    config = _document(home / 'config.toml')
    selected = _document(Path(system_dir) / 'config.toml')
    _merge(selected, config)
    if profiles:
        name = next(iter(profiles))
        if config.get('profile') == name or name in config.get('profiles', {}):
            raise ValueError('native v2 CLI profile conflicts with legacy profile')
        path = home / (name + '.config.toml')
        if not path.is_file():
            raise ValueError('original selected profile file is unavailable')
        _merge(selected, _document(path))
    elif selected.get('profile') is not None:
        entry = selected.get('profiles', {}).get(selected['profile'])
        if not isinstance(entry, dict):
            raise ValueError('original selected profile is unavailable')
        _merge(selected, entry)
    for override in overrides:
        _merge(selected, override)
    route = _route(selected)
    paths = [Path(system_dir) / 'managed_config.toml', home / 'managed_config.toml']
    if cwd is not None:
        paths += [cwd / 'config.toml', *(p / '.codex' / 'config.toml'
                    for p in (cwd, *cwd.parents))]
    for path in dict.fromkeys(paths):
        if path == home / 'config.toml':
            continue
        document = _document(path)
        candidate = copy.deepcopy(selected)
        _merge(candidate, document)
        # CLI flags outrank project config. Legacy managed policy may override
        # flags, so evaluate it without reapplying CLI overrides.
        if path.name != 'managed_config.toml':
            for override in overrides:
                _merge(candidate, override)
        if _route(candidate) != route:
            raise ValueError('project or managed route differs from original selection')
    return route
