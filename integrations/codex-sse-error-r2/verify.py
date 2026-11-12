#!/usr/bin/env python3
"""Verify a clean upstream checkout, apply frozen test inputs, and audit logs.

This script neither fetches source nor invokes a compiler. CI runs Cargo explicitly.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

MANIFEST_SHA256 = '7799d884ffc208de82b80c1f651b0ce524bf40474176da6b63baa49993f9134b'
RED_MANIFEST_SHA256 = '8df0e88878f4ec2a3f83b29e9764f38e62506cb4a2effc1edb9f52519c491fd1'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()


def manifest(bundle):
    require(digest(bundle / 'manifest.json') == MANIFEST_SHA256, 'manifest pin differs')
    result = json.loads((bundle / 'manifest.json').read_text())
    require(digest(bundle / 'candidate.patch') == result['candidate_patch_sha256'], 'candidate patch pin differs')
    return result


def initialize(bundle, source, evidence):
    spec = manifest(bundle)
    require(git(source, 'rev-parse', 'HEAD') == spec['upstream_commit'], 'wrong upstream commit')
    require(git(source, 'rev-parse', 'HEAD^{tree}') == spec['upstream_tree'], 'wrong upstream tree')
    require(not git(source, 'status', '--porcelain'), 'upstream checkout is not clean')
    for rel, pin in spec['source_files'].items():
        path = source / rel
        require(not path.is_symlink(), 'source path is symlink')
        require((digest(path) == pin['before_sha256']) if pin['before_sha256'] else not path.exists(), 'preimage differs: ' + rel)
    require(digest(source / 'codex-rs/Cargo.lock') == spec['cargo_lock_sha256'], 'upstream lock differs')
    workspace = source / 'codex-rs/Cargo.toml'
    require(digest(workspace) == spec['workspace_manifest_sha256'], 'upstream manifest differs')
    original = workspace.read_bytes()
    old = b'[workspace.package]\nversion = "0.164.0"'
    require(original.count(old) == 1, 'ambiguous test version replacement')
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'original-workspace.toml').write_bytes(original)
    (evidence / 'original-Cargo.lock').write_bytes((source / 'codex-rs/Cargo.lock').read_bytes())
    workspace.write_bytes(original.replace(old, b'[workspace.package]\nversion = "0.0.0"', 1))
    require(digest(workspace) == spec['test_workspace_manifest_sha256'], 'test manifest pin differs')
    (evidence / 'INPUTS.json').write_text(json.dumps(spec, indent=2) + '\n')


def apply_patch(bundle, source, kind):
    spec = manifest(bundle)
    if kind == 'candidate':
        patch = bundle / 'candidate.patch'
    else:
        require(digest(bundle / 'baseline-red.json') == RED_MANIFEST_SHA256, 'RED manifest pin differs')
        red = json.loads((bundle / 'baseline-red.json').read_text())
        patch = bundle / 'baseline-red.patch'
        require(digest(patch) == red['patch_sha256'], 'baseline regression patch differs')
    reverse = ['--reverse'] if kind == 'remove-red' else []
    subprocess.run(['git', '-C', str(source), 'apply', *reverse, '--check', '--whitespace=error', str(patch)], check=True)
    subprocess.run(['git', '-C', str(source), 'apply', *reverse, '--whitespace=error', str(patch)], check=True)
    if kind == 'candidate':
        # RED appends a test module to responses.rs. Verify the candidate before
        # adding the same RED fixture to the candidate, rather than weakening pins.
        for rel, pin in spec['source_files'].items():
            require(digest(source / rel) == pin['after_sha256'], 'candidate bytes differ: ' + rel)
    graph_unchanged(bundle, source)


def graph_unchanged(bundle, source):
    spec = manifest(bundle)
    require(digest(source / 'codex-rs/Cargo.lock') == spec['cargo_lock_sha256'], 'Cargo.lock changed')
    require(digest(source / 'codex-rs/Cargo.toml') == spec['test_workspace_manifest_sha256'], 'test manifest changed')


def source_snapshot(bundle, source, output):
    paths = set(manifest(bundle)['source_files']) | {
        'codex-rs/Cargo.toml', 'codex-rs/Cargo.lock',
        'codex-rs/codex-api/src/sse/responses_error_delivery_regression_tests.rs',
    }
    values = {path: digest(source / path) if (source / path).is_file() else None for path in sorted(paths)}
    output.write_text(json.dumps(values, indent=2) + '\n')


def audit_log(bundle, log, kind, returncode):
    text = log.read_text()
    spec = manifest(bundle)
    tests = re.findall(r'^test ([\w:]+) \.\.\. (ok|FAILED|ignored)$', text, re.M)
    require(tests, 'no actual Rust test results')
    counts = re.findall(r'test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;', text)
    require(len(counts) == 1, 'missing or multiple test summary')
    if kind == 'targeted':
        require(returncode == 0, 'candidate targeted command failed')
        require(len(tests) == 8 and {name for name, _ in tests} == set(spec['expected_tests']), 'targeted fixture names/count differ')
        require(all(state == 'ok' for _, state in tests), 'targeted case failed/skipped')
        require(counts == [('ok', '8', '0', '0')], 'targeted count mismatch')
    elif kind == 'baseline-red':
        require(digest(bundle / 'baseline-red.json') == RED_MANIFEST_SHA256, 'RED manifest pin differs')
        red = json.loads((bundle / 'baseline-red.json').read_text())
        require(returncode == 101, 'baseline must fail as a test, not compilation/setup')
        require(tests == [(red['test_name'], 'FAILED')], 'wrong baseline failing test')
        require(counts == [('FAILED', '0', '1', '0')], 'baseline failure counts differ')
        require(red['expected_panic'] in text, 'baseline failed for an unrelated reason')
    elif kind == 'candidate-red':
        require(digest(bundle / 'baseline-red.json') == RED_MANIFEST_SHA256, 'RED manifest pin differs')
        red = json.loads((bundle / 'baseline-red.json').read_text())
        require(returncode == 0 and tests == [(red['test_name'], 'ok')], 'candidate did not fix baseline regression')
        require(counts == [('ok', '1', '0', '0')], 'candidate regression count mismatch')
    else:
        require(returncode == 0 and counts[0][0] == 'ok' and counts[0][2] == '0', 'related suite failed')
        require(all(state == 'ok' for _, state in tests), 'related suite skipped or failed')
        require(int(counts[0][1]) == len(tests) and counts[0][3] == '0', 'related summary and test count differ')
        require(len({name for name, _ in tests}) == len(tests), 'related test names repeat')
    result = {'schema': 'codex-sse-real-rust-log-v1', 'kind': kind, 'returncode': returncode,
        'log_sha256': digest(log), 'test_count': len(tests), 'tests': tests,
        'status': 'EXPECTED_BASELINE_FAILURE' if kind == 'baseline-red' else 'PASS'}
    log.with_suffix('.RESULT.json').write_text(json.dumps(result, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['initialize', 'apply-red', 'remove-red', 'apply-candidate', 'graph', 'log', 'snapshot'])
    parser.add_argument('--source', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--log', type=Path)
    parser.add_argument('--kind', choices=['targeted', 'baseline-red', 'candidate-red', 'related'])
    parser.add_argument('--returncode', type=int)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    if args.action == 'initialize':
        initialize(bundle, args.source.resolve(), args.evidence.resolve())
    elif args.action.startswith('apply-') or args.action == 'remove-red':
        apply_patch(bundle, args.source.resolve(), args.action.removeprefix('apply-'))
    elif args.action == 'graph':
        graph_unchanged(bundle, args.source.resolve())
    elif args.action == 'snapshot':
        source_snapshot(bundle, args.source.resolve(), args.log)
    else:
        audit_log(bundle, args.log, args.kind, args.returncode)


if __name__ == '__main__':
    main()
