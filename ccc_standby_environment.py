"""Carry the selected native environment without inheriting another shell's keys.

Only terminal identity comes from the new surface. Configuration, credentials,
PATH and provider settings come from the captured target environment. The
private envelope is job-bound; commands and public claims contain hashes only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from ccc_native_standby import identifier, write_once


LIMIT = 256 * 1024
SURFACE_KEYS = frozenset({
    'CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID', 'CMUX_PANEL_ID', 'CMUX_TAB_ID',
    'CMUX_TERMINAL_LIFECYCLE_ID', 'GHOSTTY_SURFACE_ID',
    'CMUX_PORT', 'CMUX_PORT_END', 'CMUX_PORT_RANGE',
})
# These identify the submitting agent, not configuration for the new native.
PARENT_KEYS = frozenset({'CODEX_SESSION_ID', 'CODEX_THREAD_ID',
    'CMUX_CODEX_PID', 'CMUX_CODEX_INVOCATION_ID', 'PWD', 'OLDPWD', '_', 'SHLVL'})
FIXED_KEYS = frozenset({'TOKIO_WORKER_THREADS', 'CODEX_TUI_RECORD_SESSION',
                        'CODEX_TUI_SESSION_LOG_PATH', 'CODEX_CLIENT_THREAD_OBSERVER'})


def _serialized(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False, allow_nan=False) + '\n').encode()


def _mapping(value):
    if (not isinstance(value, dict)
            or any(not isinstance(k, str) or not k or '=' in k or '\0' in k
                   or not isinstance(v, str) or '\0' in v for k, v in value.items())
            or len(_serialized(value)) > LIMIT):
        raise ValueError('invalid or oversized native environment')
    return dict(value)


def template(environment):
    """Return the fixed configuration part; never inspect ambient os.environ."""
    value = _mapping(environment)
    if not value.get('HOME') or not Path(value['HOME']).is_absolute():
        raise ValueError('target native HOME must be absolute')
    if 'CODEX_HOME' in value and not Path(value['CODEX_HOME']).is_absolute():
        raise ValueError('target CODEX_HOME must be absolute')
    return {k: v for k, v in value.items()
            if k not in SURFACE_KEYS | PARENT_KEYS | FIXED_KEYS
            and not k.startswith('CMUX_AGENT_LAUNCH_')}


def signature(environment):
    return hashlib.sha256(_serialized(_mapping(environment))).hexdigest()


def compose(environment, surface_environment, *, workspace_id, surface_id, cwd, tui_log):
    """Bind fixed configuration to the actual registered new terminal identity."""
    value = template(environment)
    surface = _mapping(surface_environment)
    for key, expected in (('CMUX_WORKSPACE_ID', workspace_id), ('CMUX_SURFACE_ID', surface_id)):
        if identifier(surface.get(key)) != identifier(expected):
            raise ValueError('native target terminal identity mismatch')
    if not Path(cwd).is_absolute() or not Path(tui_log).is_absolute():
        raise ValueError('absolute native working and event paths required')
    value.update({k: surface[k] for k in SURFACE_KEYS if k in surface})
    value.update(PWD=str(cwd), TOKIO_WORKER_THREADS='2', CODEX_TUI_RECORD_SESSION='1',
                 CODEX_TUI_SESSION_LOG_PATH=str(tui_log), CODEX_CLIENT_THREAD_OBSERVER='1')
    # Standby execs the verified native binary directly. Retain the managed
    # launcher's request observer even when the submitting shell lacks it.
    return _mapping(value)


def _identity(path, directory=False):
    info = path.lstat()
    if ((stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode) is not True
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ValueError('native environment envelope must be private and owned')
    return info.st_dev, info.st_ino


class EnvironmentFile:
    """Bounded immutable private storage, retained as consumed launch evidence."""
    def __init__(self, path, sha256, binding):
        self.path = Path(path)
        self._failed = False
        if self.path.parent.resolve(strict=True) != self.path.parent:
            raise ValueError('native environment parent must be canonical')
        self._parent = _identity(self.path.parent, directory=True)
        self._file = _identity(self.path)
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != self._file:
                raise ValueError('native environment envelope replaced while opening')
            self._raw = stream.read(LIMIT + 1)
        if len(self._raw) > LIMIT or hashlib.sha256(self._raw).hexdigest() != sha256:
            raise ValueError('native environment envelope hash mismatch')
        record = json.loads(self._raw)
        if (set(record) != {'schema', 'kind', 'binding', 'environment'}
                or record['schema'] != 1 or record['kind'] != 'standby_target_environment'
                or record['binding'] != binding or not isinstance(binding, dict) or not binding):
            raise ValueError('native environment envelope binding mismatch')
        self._environment = template(record['environment'])
        if self._environment != record['environment']:
            raise ValueError('native environment envelope contains parent identity')
        self.sha256 = sha256
        self.current()

    @classmethod
    def create(cls, path, *, binding, environment):
        path = Path(path)
        if path.parent.resolve(strict=True) != path.parent:
            raise ValueError('native environment parent must be canonical')
        _identity(path.parent, directory=True)
        record = {'schema': 1, 'kind': 'standby_target_environment',
                  'binding': binding, 'environment': template(environment)}
        if len(_serialized(record)) > LIMIT:
            raise ValueError('native environment envelope exceeds size limit')
        raw = write_once(path, record)
        return cls(path, hashlib.sha256(raw).hexdigest(), binding)

    def current(self):
        if self._failed:
            raise ValueError('native environment envelope permanently invalidated')
        try:
            if (self.path.parent.resolve(strict=True) != self.path.parent
                    or _identity(self.path.parent, directory=True) != self._parent
                    or _identity(self.path) != self._file):
                raise ValueError('native environment envelope replaced')
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                raw = stream.read(LIMIT + 1)
            if ((opened.st_dev, opened.st_ino) != self._file or raw != self._raw
                    or _identity(self.path) != self._file
                    or self.path.parent.resolve(strict=True) != self.path.parent
                    or _identity(self.path.parent, directory=True) != self._parent):
                raise ValueError('native environment envelope changed')
            return dict(self._environment)
        except BaseException:
            self._failed = True
            raise
