"""Bounded supplemental payloads using the unchanged original R5 crypto.

This helper has no publisher, network, application launch or key discovery.
Contracts are external pins. Pending contracts fail before loading crypto/key.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import zipfile

MAX_PAYLOAD = 32 * 1024**2
MAX_NATIVE_EXPANDED = 128 * 1024**2
SOURCE_LOCK = '76fcd85b8bc51f18eee43192e2e4a675e16a322ecd3669e3447ef21489775f2e'
CRYPTO_SHA = '2a3c1485ed52bba1bff71fc3df1376383bf05fbe78aecbb7a90459dc78bd06d1'
PRODUCER = {'repository': 'lzhs1995/ccc', 'run_id': 43160452629, 'run_attempt': 1,
            'head_sha': '68c7e04ac9fa68d3ec8750bec4dc01d1b4f39ae7'}


def need(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def same(left, right):
    return json.dumps(left, sort_keys=True, separators=(',', ':')) == json.dumps(right, sort_keys=True, separators=(',', ':'))


def decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            need(key not in value, 'duplicate JSON key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


def read(path, maximum=MAX_PAYLOAD):
    path = Path(path).absolute()
    need(path == path.resolve(strict=True) and not path.is_symlink(), 'linked input')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= maximum,
             'unsafe or oversized input')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    signature = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    need(signature(before) == signature(after) == signature(path.lstat())
         and len(raw) == before.st_size, 'input changed during read')
    return raw


def write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def safe_name(name):
    need(isinstance(name, str) and name and '\\' not in name and '\x00' not in name,
         'unsafe payload member')
    path = PurePosixPath(name)
    need(not path.is_absolute() and all(part not in ('', '.', '..') for part in name.split('/'))
         and path.as_posix() == name, 'escaping payload member')
    return path


def expanded_limit(kind):
    return MAX_NATIVE_EXPANDED + MAX_PAYLOAD if kind == 'original-inputs' else MAX_PAYLOAD


def validate_expanded_files(kind, files):
    if kind == 'original-inputs':
        native = sum(row['bytes'] for name, row in files.items() if name.startswith('native-small/'))
        other = sum(row['bytes'] for name, row in files.items() if not name.startswith('native-small/'))
        need(native <= MAX_NATIVE_EXPANDED and other <= MAX_PAYLOAD, 'expanded original-input budget differs')
    else:
        need(sum(row['bytes'] for row in files.values()) <= MAX_PAYLOAD, 'expanded payload too large')


def validate_contract(value, *, sealed):
    need(value.get('schema') == 'ccc-nine-session-supplemental-transport-v1'
         and value.get('kind') in ('source', 'original-inputs')
         and value.get('source_lock_sha256') == SOURCE_LOCK
         and same(value.get('producer'), PRODUCER), 'foreign supplemental contract')
    status = 'SEALED_ORIGINAL_PAYLOAD' if sealed else 'PREPARED_PRIVATE_ARCHIVE'
    need(value.get('status') == status, 'PENDING: original payload contract is incomplete')
    files = value.get('files')
    need(isinstance(files, dict) and 0 < len(files) <= 4096, 'payload member count differs')
    for name, row in files.items():
        safe_name(name)
        need(isinstance(row, dict) and type(row.get('bytes')) is int and row['bytes'] >= 0
             and isinstance(row.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', row['sha256']),
             'invalid payload member pin')
    validate_expanded_files(value['kind'], files)
    if value['kind'] == 'source':
        need(len(files) == 65 and 'adapter/SOURCE-LOCK.json' in files, 'supplemental source closure differs')
    else:
        need({'INPUT-CONTRACT.json', 'RUN.json', 'JOBS.json', 'APP-ARTIFACT.json',
              'native-small/NATIVE-SUCCESS.json', 'native-small/MANIFEST.json',
              'release-evidence/package/PACKAGE-PIN.json'} <= set(files), 'original proof inputs incomplete')
        need(all(name in ('INPUT-CONTRACT.json', 'RUN.json', 'JOBS.json', 'APP-ARTIFACT.json')
                 or name.startswith(('native-small/', 'release-evidence/')) for name in files),
             'unexpected original proof input')
        need(value.get('input_contract_sha256') == files['INPUT-CONTRACT.json']['sha256'],
             'input contract pin differs')
    need(type(value.get('archive_bytes')) is int and 0 < value['archive_bytes'] <= MAX_PAYLOAD
         and isinstance(value.get('archive_sha256'), str) and re.fullmatch('[0-9a-f]{64}', value['archive_sha256']),
         'payload archive pin missing')
    if sealed:
        need(type(value.get('ciphertext_bytes')) is int and value['ciphertext_bytes'] == value['archive_bytes'] + 28
             and isinstance(value.get('ciphertext_sha256'), str)
             and re.fullmatch('[0-9a-f]{64}', value['ciphertext_sha256']), 'ciphertext pin missing')
    return value


def aad(value):
    return json_bytes({'schema': 'ccc-nine-session-supplemental-aad-v1', 'kind': value['kind'],
        'source_lock_sha256': SOURCE_LOCK, 'producer': PRODUCER,
        'archive_sha256': value['archive_sha256']})


def checked_archive(raw, value):
    need(len(raw) == value['archive_bytes'] and digest(raw) == value['archive_sha256'], 'payload ZIP pin differs')
    archive = zipfile.ZipFile(io.BytesIO(raw))
    infos = archive.infolist()
    need(len(infos) == len(value['files']) and sum(row.file_size for row in infos) <= expanded_limit(value['kind']),
         'payload ZIP member or expansion budget differs')
    validate_expanded_files(value['kind'], {row.filename: {'bytes': row.file_size} for row in infos})
    seen = set()
    for row in infos:
        safe_name(row.filename)
        mode = row.external_attr >> 16
        need(row.filename not in seen and row.filename in value['files'] and not row.is_dir()
             and not row.flag_bits & 1 and stat.S_IFMT(mode) in (0, stat.S_IFREG), 'unsafe ZIP member')
        seen.add(row.filename)
        expected = value['files'][row.filename]
        need(row.file_size == expected['bytes'] and digest(archive.read(row)) == expected['sha256'],
             'payload member differs')
    need(seen == set(value['files']), 'payload ZIP closure differs')
    if value['kind'] == 'source':
        raw_lock = archive.read('adapter/SOURCE-LOCK.json')
        need(digest(raw_lock) == SOURCE_LOCK, 'reviewed source lock differs')
        lock = decode(raw_lock)
        expected = {'adapter/SOURCE-LOCK.json': value['files']['adapter/SOURCE-LOCK.json']}
        for group in ('natural', 'adapter'):
            expected.update({group + '/' + name: row for name, row in lock['files'][group].items()})
        need(same(expected, value['files']), 'reviewed supplemental closure differs')
    return archive


def crypto(path):
    raw = read(path, 1024**2)
    need(digest(raw) == CRYPTO_SHA, 'original R5 crypto source differs')
    spec = importlib.util.spec_from_file_location('nine_original_crypto', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    raw_contract = read(args.contract, 2 * 1024**2)
    need(digest(raw_contract) == args.contract_sha256, 'external transport contract pin differs')
    value = validate_contract(decode(raw_contract), sealed=args.action == 'decrypt')
    need(not args.output.exists(), 'payload output already exists')
    if args.action == 'decrypt':
        need(sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
             and os.environ.get('GITHUB_REPOSITORY') == PRODUCER['repository']
             and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'fresh GitHub-hosted Mac required')
        temp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
        need(args.output.is_absolute() and args.output == args.output.resolve(strict=False)
             and args.output.is_relative_to(temp) and args.output != temp, 'foreign payload output')
        encrypted = read(args.payload, MAX_PAYLOAD + 28)
        need(len(encrypted) == value['ciphertext_bytes'] and digest(encrypted) == value['ciphertext_sha256'],
             'ciphertext differs')
        original = crypto(args.crypto)
        raw = original.unseal(encrypted, original.key_from_environment(), aad(value))
        archive = checked_archive(raw, value)
        args.output.mkdir(mode=0o700)
        with archive:
            for member in archive.infolist():
                path = args.output / member.filename
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                write(path, archive.read(member))
    else:
        raw = read(args.payload)
        with checked_archive(raw, value):
            pass
        original = crypto(args.crypto)
        encrypted = original.seal(raw, original.key_from_environment(), aad(value))
        args.output.mkdir(mode=0o700)
        write(args.output / 'payload.aesgcm', encrypted)
        result = dict(value, status='SEALED_ORIGINAL_PAYLOAD', ciphertext_bytes=len(encrypted),
                      ciphertext_sha256=digest(encrypted))
        write(args.output / 'TRANSPORT.json', json_bytes(result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('seal', 'decrypt'))
    parser.add_argument('--crypto', required=True, type=Path)
    parser.add_argument('--contract', required=True, type=Path)
    parser.add_argument('--contract-sha256', required=True)
    parser.add_argument('--payload', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
