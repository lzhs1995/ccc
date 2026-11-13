"""Small GHA-only execution helpers; importing never launches a process."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import time

FLOOR = 30 * 2**30
BUILD_HEADROOM = 512 * 2**20
SMOKE_HEADROOM = 128 * 2**20


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def pin(path):
    p = Path(path)
    require(p.is_file() and not p.is_symlink(), 'expected regular non-symlink file')
    return {'sha256': digest(p), 'bytes': p.stat().st_size}


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def no_portable_selector(binary):
    """Magpie's executable-adjacent portable selector takes precedence over XDG."""
    binary = Path(binary).resolve(strict=True)
    for name in ('data', '.portable'):
        selector = binary.parent / name
        require(not selector.exists() and not selector.is_symlink(),
                'portable selector beside smoke binary would bypass private XDG configuration: ' + name)


def gha_only():
    require(os.environ.get('GITHUB_ACTIONS') == 'true', 'execution requires the disposable GHA runner')
    require(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'requires Darwin arm64')


def verify_package(root, expected):
    root = Path(root)
    require(digest(root / 'PINNED-FILES.json') == expected, 'package pin differs')
    for name, sha in json.loads((root / 'PINNED-FILES.json').read_text()).items():
        rel = Path(name)
        require(not rel.is_absolute() and '..' not in rel.parts, 'unsafe input path')
        require(digest(root / rel) == sha, 'package input changed: ' + name)


def verify_source(source, manifest):
    source = Path(source)
    rows = manifest['source_files']
    expected = {row['path'] for row in rows}
    require(len(expected) == len(rows), 'duplicate source path')
    for row in rows:
        rel = Path(row['path'])
        require(not rel.is_absolute() and '..' not in rel.parts, 'unsafe source path')
        require(pin(source / rel) == {'sha256': row['sha256'], 'bytes': row['bytes']},
                'source bytes changed: ' + str(rel))
    actual = {str(p.relative_to(source)) for p in source.rglob('*')
              if p.is_file() and '.git' not in p.relative_to(source).parts}
    require(actual == expected, 'source membership changed')


def capacity(path):
    return {'elapsed_monotonic': time.monotonic(), 'free_bytes': shutil.disk_usage(path).free}


def admit(path, headroom, samples):
    for _ in range(2):
        value = capacity(path)
        samples.append(value)
        require(value['free_bytes'] >= FLOOR + headroom, 'capacity below admission floor plus headroom')
        time.sleep(.1)


def stop_owned(child):
    if child is None:
        return None
    if child.poll() is not None:
        return child.wait()
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        require(child.poll() is not None, 'owned child still live after missing process group')
    try:
        return child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                require(child.poll() is not None, 'owned child still live after missing process group')
        return child.wait(timeout=5)


def run_guarded(argv, cwd, env, output, seconds, headroom=BUILD_HEADROOM):
    """Only this live direct child/session may be stopped; preserve every attempt."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    result = {'argv': argv, 'started': False, 'status': 'NOT_STARTED', 'samples': [],
              'returncode': None, 'child_reaped': False}
    child = None
    try:
        admit(output, headroom, result['samples'])
        with (output / 'stdout.log').open('x') as out, (output / 'stderr.log').open('x') as err:
            child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=True)
            result['started'] = True
            deadline = time.monotonic() + seconds
            while child.poll() is None:
                sample = capacity(output)
                result['samples'].append(sample)
                require(sample['free_bytes'] >= FLOOR, 'capacity floor crossed')
                require(time.monotonic() < deadline, 'owned child deadline exceeded')
                time.sleep(.5)
            result['returncode'] = child.wait()
        require(result['returncode'] == 0, 'child returned nonzero')
        result['status'] = 'PASS'
    except Exception as error:
        result.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
    finally:
        try:
            result['returncode'] = stop_owned(child)
            result['child_reaped'] = child is None or child.poll() is not None
        except Exception as error:
            result.update(status='FAIL', cleanup_error=str(error))
        result['logs'] = {p.name: pin(p) for p in output.glob('*.log')}
        save(output / 'RESULT.json', result)
    require(result['status'] == 'PASS' and result['child_reaped'], 'owned command failed; original result retained')
    return result
