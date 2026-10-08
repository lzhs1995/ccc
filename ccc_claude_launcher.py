"""Opt in a selected Claude executable to request observation, without routing changes.

The shell selects the executable (including cmux's shim or ccp's pinned binary).
Only this new process receives the preload; existing processes are untouched.
"""
import os
from pathlib import Path
import re
import shlex
import stat
import sys


def render_shell(python, launcher):
    """Source after the existing launchers; preserve PATH/cmux binary selection."""
    if not Path(python).is_absolute() or not Path(launcher).is_absolute():
        raise ValueError('launcher paths must be absolute')
    return f'''# CCC actual-request observation. No credential/configuration changes.
_ccc_claude_observed() {{
  local executable="$1"; shift
  {shlex.quote(str(python))} -B {shlex.quote(str(launcher))} -- "$executable" "$@"
}}
claude() {{
  local executable
  executable="$(whence -p claude)" || return $?
  [[ -n "$executable" ]] || return 127
  _ccc_claude_observed "$executable" "$@"
}}
Claude() {{ claude "$@"; }}
'''


def render_entry(python, launcher, executable):
    """Wrap one selected executable; direct paths and old shells use it too."""
    paths = (python, launcher, executable)
    if any(not Path(p).is_absolute() or any(c in str(p) for c in '\n\r\x00')
           for p in paths):
        raise ValueError('entry paths must be absolute single-line paths')
    return '#!/bin/sh\nexec ' + ' '.join(shlex.quote(str(p)) for p in
        (python, '-B', launcher, '--', executable)) + ' "$@"\n'


def observation_environment(environment, observer, directory):
    observer, directory = Path(observer), Path(directory)
    if not observer.is_absolute() or not directory.is_absolute():
        raise ValueError('observer and observation directory must be absolute')
    code = observer.lstat()
    if (not stat.S_ISREG(code.st_mode) or code.st_uid != os.getuid()
            or code.st_mode & 0o022):
        raise ValueError('observer must be an owned, non-writable-by-others regular file')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = directory.lstat()
    if (not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid()
            or root.st_mode & 0o077):
        raise ValueError('observation directory must be private and owned')
    env = dict(environment)
    # Nested cmux/profile wrappers may invoke us again. Preserve other Bun
    # options verbatim and append this observer at most once.
    # BUN_OPTIONS uses Bun's argument parser, not a shell. Shell quotes silently
    # disable preload in the native executable; backslash escapes are required.
    if any(c in str(observer) for c in '\n\r\x00'):
        raise ValueError('unsupported observer pathname')
    escaped = ''.join('\\' + c if c in '\\ \t\'"' else c for c in str(observer))
    option = '--preload ' + escaped
    previous = env.get('BUN_OPTIONS', '')
    prior = env.get('CCC_CLAUDE_REQUEST_OBSERVER')
    if prior and prior != str(observer):
        # An older sourced shell may wrap a newly installed absolute entry.
        # Replace only the exact CCC option it added; retain all other options.
        old_escaped = ''.join('\\' + c if c in '\\ \t\'"' else c for c in prior)
        previous = re.sub(r'(?<!\S)' + re.escape('--preload ' + old_escaped) +
                          r'(?=\s|$)', '', previous, count=1).strip()
    if env.get('CCC_CLAUDE_REQUEST_OBSERVER') != str(observer) or option not in previous:
        env['BUN_OPTIONS'] = (previous + ' ' + option).strip()
    env['CCC_CLAUDE_REQUEST_OBSERVER'] = str(observer)
    env['CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR'] = str(directory)
    return env


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ['--']:
        arguments.pop(0)
    if not arguments or not os.path.isabs(arguments[0]):
        raise SystemExit('usage: ccc_claude_launcher.py -- /selected/claude [arguments...]')
    observer = Path(__file__).resolve().with_name('ccc_claude_request_observer.cjs')
    directory = Path(os.environ.get('CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR') or
                     Path.home() / '.local/share/ccc/claude-request-observations')
    try:
        environment = observation_environment(os.environ, observer, directory)
    except (OSError, ValueError) as exc:
        # Observation is auxiliary. Never block or alter an authorized Claude
        # invocation because its observation directory is temporarily unusable.
        print(f'CCC: request observation unavailable ({type(exc).__name__}); starting selected Claude.', file=sys.stderr)
        environment = dict(os.environ)
    os.execve(arguments[0], arguments, environment)


if __name__ == '__main__':
    main()
