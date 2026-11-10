#!/usr/bin/env python3
"""Check current Git files for common accidental disclosures; never print values.

This intentionally bounded check is not a general secret detector or a history
audit. Test placeholder home paths are allowed only from the explicit list below.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

PATTERNS = {
    'github_token': re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    'github_fine_grained_token': re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{50,}\b'),
    'api_secret': re.compile(rb'\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{24,}\b'),
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'aws_access_key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
}
HOMES = re.compile(rb'/(?:Users|home)/([A-Za-z0-9_.-]+)(?=/|\b)')
PLACEHOLDERS = {b'you', b'user', b'example', b'test', b'tester', b'runner', b'username',
                b'alice', b'bob', b'someone', b'me', b'demo', b'shared', b'Shared', b'x'}
PRIVATE_NAMES = {'auth.json', 'state.json', 'status.json', 'config.local.json', '.env'}
PRIVATE_DIRS = {'profiles', 'handoff', 'observations', 'backups', 'workspace-batches'}
SYNTHETIC = json.loads(Path(__file__).with_name('public_content_fixtures.json').read_text())


def findings(path, data):
    result = []
    parts = Path(path).parts
    if (Path(path).name in PRIVATE_NAMES or PRIVATE_DIRS.intersection(parts)
            or Path(path).suffix in {'.jsonl', '.sqlite', '.sqlite3', '.db', '.pem', '.key', '.log'}):
        result.append({'path': path, 'line': 0, 'kind': 'private_artifact_name'})
    for number, line in enumerate(data.splitlines(), 1):
        for name, pattern in PATTERNS.items():
            matches = pattern.finditer(line)
            permitted = SYNTHETIC.get(name, {}).get(path, [])
            if any(hashlib.sha256(m.group()).hexdigest() not in permitted for m in matches):
                result.append({'path': path, 'line': number, 'kind': name})
        if any(match.group(1) not in PLACEHOLDERS for match in HOMES.finditer(line)):
            result.append({'path': path, 'line': number, 'kind': 'personal_home_path'})
    return result


def self_test():
    cases = [
        ('docs/example.md', b'gh' + b'p_' + b'A' * 36, 'github_token'),
        ('docs/example.md', b'sk-' + b'x' * 32, 'api_secret'),
        ('docs/example.md', b'-----BEGIN ' + b'PRIVATE KEY-----', 'private_key'),
        ('docs/example.md', b'/Users/' + b'private-owner/file', 'personal_home_path'),
        ('state.json', b'{}', 'private_artifact_name'),
        ('private.log', b'diagnostic', 'private_artifact_name'),
        ('private.sqlite3', b'database', 'private_artifact_name'),
        ('docs/example.md', b'/Users/you/example sk-EXAMPLE', None),
    ]
    for path, data, expected in cases:
        actual = findings(path, data)
        assert (not actual) if expected is None else expected in {x['kind'] for x in actual}
    print(json.dumps({'self_test_cases': len(cases), 'passed': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    root = Path(__file__).resolve().parents[1]
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z',
        '--cached', '--others', '--exclude-standard']).decode().split('\0')
    results, total = [], 0
    for name in sorted(set(filter(None, names))):
        path = root / name
        if path.is_symlink():
            results.append({'path': name, 'line': 0, 'kind': 'symlink_requires_review'})
            continue
        if not path.exists():
            continue
        total += 1
        results.extend(findings(name, path.read_bytes()))
    print(json.dumps({'scope': 'current tracked and nonignored proposed files; no history scan',
        'files_scanned': total, 'findings': results, 'passed': not results}, indent=2))
    return 1 if results else 0


if __name__ == '__main__':
    raise SystemExit(main())
