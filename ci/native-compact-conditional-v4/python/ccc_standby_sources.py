"""Discover file inputs for the original native launch, without merging config.

The union deliberately includes disabled/local layers: native owns precedence,
trust, warnings and skill enablement. Runtime data directories are not sources.
This is the filesystem part of the adapter, not a claim that cloud, managed or
thread layers have been exported by the TUI. Those require separate evidence.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 test/acceptance environment
    import tomli as tomllib

from ccc_standby_generation import SCOPES, StandbyGeneration


REFERENCE_COMMIT = 'b412ff32c417f855c2b2d1581b77058eed87c84b'
_FILE_KEYS = ('model_instructions_file', 'model_catalog_json',
              'experimental_compact_prompt_file', 'js_repl_node_path')
_VALUE_OPTIONS = {'--cd', '-C', '--profile', '-p', '--model', '-m',
                  '--sandbox', '-s', '--ask-for-approval', '-a',
                  '--enable', '--disable', '--add-dir'}
_SWITCHES = {'--no-alt-screen', '--full-auto',
             '--dangerously-bypass-approvals-and-sandbox'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _path(value, base):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('invalid native source path')
    # Do not expand using the manager's HOME or resolve away original links.
    if value.startswith('~'):
        raise ValueError('source path needs native expansion before capture')
    path = Path(value)
    path = path if path.is_absolute() else base / path
    if '..' in path.parts:
        # Lexically collapsing .. across a symlink changes its meaning.
        raise ValueError('source path with parent traversal needs native resolution')
    return path


def _launch_options(argv, cwd):
    if (not isinstance(argv, (list, tuple)) or not argv
            or not all(isinstance(v, str) and v and '\0' not in v for v in argv)
            or not Path(argv[0]).is_absolute()):
        raise ValueError('absolute original native argv required')
    documents, profiles = [], set()
    index = 1
    while index < len(argv):
        argument = argv[index]
        option, equal, attached = argument.partition('=')
        if option in {'-c', '--config'} | _VALUE_OPTIONS:
            if equal:
                value = attached
            else:
                index += 1
                if index == len(argv):
                    raise ValueError('native option has no value')
                value = argv[index]
            if option in ('-c', '--config'):
                # Native permits an unquoted string fallback. Preserve the
                # dotted key while applying that same fallback for discovery.
                key, separator, raw = value.partition('=')
                if not separator:
                    raise ValueError('invalid native config override')
                try:
                    document = tomllib.loads(value)
                except tomllib.TOMLDecodeError:
                    document = tomllib.loads(key + '=' + json.dumps(raw))
                documents.append(document)
            elif option in ('-p', '--profile'):
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
                    raise ValueError('unsupported native profile name')
                profiles.add(value)
            elif option in ('-C', '--cd') and _path(value, cwd) != cwd:
                raise ValueError('source cwd differs from original native argv')
        elif argument not in _SWITCHES:
            raise ValueError('unsupported or prompted native launch')
        index += 1
    if len(profiles) > 1:
        raise ValueError('ambiguous original native profile')
    return documents, profiles


@dataclass(frozen=True)
class FileSources:
    roots: dict
    documents: dict
    launch_sha256: str
    reference_commit: str = REFERENCE_COMMIT

    def signature(self):
        return _digest({'roots': self.roots, 'documents': self.documents,
                        'launch': self.launch_sha256, 'reference': self.reference_commit})


class NativeFileSources:
    """Bounded preparation-time discovery for a fixed original environment.

    No filesystem writes, native process, sidecar or config merge. Native argv
    and environment are supplied as they will be passed at exec, including
    launcher's fixed overrides; the ambient manager environment is never read.
    Dynamic layers must be inventoried by the caller before overall readiness.
    """
    def __init__(self, *, argv, environment, cwd, runtime_files,
                 system_dir=Path('/etc/codex'), max_documents=256,
                 max_document_bytes=8 * 1024**2):
        self.argv = tuple(argv)
        runtime_files = tuple(runtime_files)
        self.environment = copy.deepcopy(environment)
        if (not isinstance(self.environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in self.environment.items())):
            raise ValueError('exact target environment required')
        if (not Path(self.environment.get('HOME', '')).is_absolute()
                or not Path(self.environment.get('CODEX_HOME', '/')).is_absolute()
                or not Path(cwd).is_absolute() or not Path(system_dir).is_absolute()
                or any(not Path(p).is_absolute() for p in runtime_files)):
            raise ValueError('absolute target source locations required')
        self.home = _path(self.environment.get('HOME'), Path('/'))
        self.native_home = _path(self.environment.get('CODEX_HOME', str(self.home / '.codex')), self.home)
        self.cwd = _path(str(cwd), Path('/'))
        self.system_dir = _path(str(system_dir), Path('/'))
        self.runtime_files = tuple(_path(str(p), Path('/')) for p in runtime_files)
        if not self.runtime_files or max_documents < 1 or max_document_bytes < 1:
            raise ValueError('runtime inputs and positive source bounds required')
        self.max_documents, self.max_document_bytes = max_documents, max_document_bytes
        self.overrides, self.profiles = _launch_options(self.argv, self.cwd)

    def discover(self):
        roots = {scope: set() for scope in SCOPES}
        documents, pending, visited = {}, [], set()
        total_bytes = 0

        def add(scope, path):
            roots[scope].add(str(path))

        def config(path, scope='codex_config'):
            add(scope, path)
            if str(path) not in visited:
                pending.append(path)

        def folder(path):
            config(path / 'config.toml')
            add('skills', path / 'skills')
            add('rules', path / 'rules')

        def profile(value):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
                raise ValueError('unsupported configured profile name')
            config(self.native_home / (value + '.config.toml'), 'profile')

        def dependencies(document, base):
            if not isinstance(document, dict):
                raise ValueError('native config table required')
            for key in _FILE_KEYS:
                if key in document:
                    add('codex_config', _path(document[key], base))
            for value in document.get('js_repl_node_module_dirs', []):
                add('codex_config', _path(value, base))
            if 'profile' in document:
                profile(document['profile'])
            # Pin all legacy profiles without choosing or merging one.
            for value in document.get('profiles', {}).values():
                dependencies(value, base)
            for role in document.get('agents', {}).values():
                if isinstance(role, dict) and 'config_file' in role:
                    config(_path(role['config_file'], base))
            skills = document.get('skills', {})
            for item in skills.get('config', []):
                if isinstance(item, dict) and 'path' in item:
                    add('skills', _path(item['path'], base))
            for value in skills.get('extra_roots', []):
                add('skills', _path(value, base))
            for marketplace in document.get('marketplaces', {}).values():
                if marketplace.get('source_type') == 'local' and marketplace.get('source'):
                    add('skills', _path(marketplace['source'], base))
            for name in document.get('project_doc_fallback_filenames', []):
                if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
                    raise ValueError('unsupported project document fallback name')
                for parent in (self.cwd, *self.cwd.parents):
                    add('codex_config', parent / name)

        for path in self.runtime_files:
            add('runtime', path)
        add('native_binary', Path(self.argv[0]))
        folder(self.system_dir)
        folder(self.native_home)
        config(self.system_dir / 'requirements.toml')
        config(self.system_dir / 'managed_config.toml')
        add('codex_config', self.native_home / 'auth.json')
        add('profile', self.native_home / 'config.toml')
        add('skills', self.home / '.agents' / 'skills')
        # .system is included through the original skills root. Plugin cache
        # is input; plugins/data, sessions, logs and SQLite are runtime output.
        add('skills', self.native_home / 'plugins' / 'cache')
        add('skills', self.native_home / 'plugins' / 'marketplaces')
        # Installed git marketplaces and the bundled/local plugin repository
        # have separate roots from the executable plugin cache. Native may
        # load their manifests/skills without a new config.toml write.
        add('skills', self.native_home / '.tmp' / 'marketplaces')
        add('skills', self.native_home / '.tmp' / 'plugins')
        for parent in (self.cwd, *self.cwd.parents):
            folder(parent / '.codex')
            add('skills', parent / '.agents' / 'skills')
            for name in ('AGENTS.md', 'AGENTS.override.md'):
                add('codex_config', parent / name)
        config(self.cwd / 'config.toml')
        for name in ('AGENTS.md', 'AGENTS.override.md'):
            add('codex_config', self.native_home / name)
        for value in self.profiles:
            profile(value)
        for document in self.overrides:
            dependencies(document, self.cwd)

        while pending:
            path = pending.pop()
            key = str(path)
            if key in visited:
                continue
            visited.add(key)
            if len(visited) > self.max_documents:
                raise ValueError('native config document bound exceeded')
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except FileNotFoundError:
                if os.path.lexists(path):
                    raise ValueError('native config link does not resolve')
                documents[key] = None
                continue
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('native config is not a regular file')
                data = stream.read(self.max_document_bytes - total_bytes + 1)
                after = os.fstat(stream.fileno())
            current = path.stat()
            total_bytes += len(data)
            if total_bytes > self.max_document_bytes:
                raise ValueError('native config read bound exceeded')
            stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if stamp(before) != stamp(after) or stamp(after) != stamp(current):
                raise ValueError('native config changed during discovery')
            documents[key] = hashlib.sha256(data).hexdigest()
            dependencies(tomllib.loads(data.decode('utf-8')), path.parent)
        return FileSources({k: tuple(sorted(v)) for k, v in roots.items()}, documents,
            _digest({'argv': self.argv, 'environment': self.environment, 'cwd': str(self.cwd)}))

    def capture_files(self, dynamic_current, **kwargs):
        """Join discovered inputs to a generation; does not certify completeness.

        dynamic_current must describe target non-file inputs. Discover again
        after arming so a config path introduced during scanning cannot escape
        the graph. No discovery or TOML parsing runs in generation.current().
        """
        if not callable(dynamic_current):
            raise ValueError('live non-file source reader required')
        before = self.discover()

        def effective():
            dynamic = dynamic_current()
            if not isinstance(dynamic, dict) or not dynamic:
                raise ValueError('non-file native sources unavailable')
            return {'launch_sha256': before.launch_sha256,
                    'file_source_signature': before.signature(), 'dynamic': dynamic}

        pin = StandbyGeneration(before.roots, effective, **kwargs)
        try:
            if self.discover().signature() != before.signature():
                raise ValueError('native source graph changed during capture')
            pin.current()
        except BaseException:
            pin.close()
            raise
        return pin
