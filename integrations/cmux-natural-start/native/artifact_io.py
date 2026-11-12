"""Reviewed retained_release one-shot fetch/ZIP primitives, without old run pins.

Copied from retained_release.py SHA256
3b70d8250419e8a706759c44114af077aa8e0bfb23384e41ec164c1c015a8a44.
Only change inside the three functions: use the current native runner guard.
MAX_APP is narrowed to the native 64 MiB envelope. No network configuration,
credentials, old sourceRun, package admission or retry path is copied.
"""
from pathlib import Path, PurePosixPath
import re
import resource
import stat
import subprocess
import time
import contract as c
need, save, digest = c.need, c.save, c.digest
MAX_APP = 64 * 1024**2

def relative(name):
    need(isinstance(name, str) and name and '\\' not in name and '\x00' not in name,
         'unsafe retained member name')
    value = PurePosixPath(name)
    need(not value.is_absolute() and all(p not in ('', '.', '..') for p in name.split('/'))
         and value.as_posix() == name, 'escaping retained member')
    return value

def fetch(root, name, endpoint, *, maximum, exact=None, sha256=None, timeout=600):
    """Native stdout goes to a bounded file; timeout/partial is kept, never retried."""
    c.run_identity()
    need(re.fullmatch(r'[a-z0-9-]+', name) and 0 < maximum <= MAX_APP, 'invalid fetch boundary')
    directory = root / name
    directory.mkdir(mode=0o700, exist_ok=False)
    target = directory / ('response.zip' if sha256 is not None else 'response.json')
    argv = ['gh', 'api', '--method', 'GET', endpoint]
    save(directory / 'INTENT.json', {'argv': argv, 'maximum_bytes': maximum, 'exact_bytes': exact,
                                   'sha256': sha256, 'timeout_seconds': timeout, 'retries': 0})
    def cap_file():
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
    started = time.monotonic()
    result = {'status': 'UNKNOWN', 'returncode': None}
    error = None
    with target.open('xb') as stdout, (directory / 'stderr').open('xb') as stderr:
        try:
            completed = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=timeout, preexec_fn=cap_file)
            result.update(status='COMPLETED', returncode=completed.returncode)
        except BaseException as caught:
            error = caught
            result['error'] = repr(caught)
    result.update(bytes=target.stat().st_size, sha256=digest(target), elapsed_seconds=time.monotonic() - started,
                  stderr_sha256=digest(directory / 'stderr'), retries=0)
    save(directory / 'RESULT.json', result)
    if error is not None:
        raise error
    need(result['returncode'] == 0 and result['bytes'] <= maximum, 'retained fetch failed; preserve original record')
    if exact is not None:
        need(result['bytes'] == exact, 'retained download length differs')
    if sha256 is not None:
        need(result['sha256'] == sha256, 'retained download digest differs')
    return target

def regular_archive(archive, *, max_bytes, max_files):
    rows = archive.infolist()
    need(len(rows) <= max_files and sum(r.file_size for r in rows) <= max_bytes,
         'retained archive exceeds expansion/member budget')
    seen = set()
    for row in rows:
        name = row.filename[:-1] if row.is_dir() else row.filename
        relative(name)
        need(name not in seen and not row.flag_bits & 1
             and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG, stat.S_IFDIR),
             'duplicate, encrypted or nonregular retained archive member')
        seen.add(name)
    return rows
