#!/usr/bin/env python3
"""Reconstruct the exact conditional compact candidate, without compiling it."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile

MANIFEST_SHA256 = '9063650f11ecb06118a924b83d007c03fd379e70b70ff96e9722032503004c0e'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--upstream-archive', type=Path)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    manifest_path = bundle / 'source-manifest.json'
    if digest(manifest_path) != MANIFEST_SHA256:
        raise ValueError('conditional source manifest changed')
    manifest = json.loads(manifest_path.read_bytes())
    prior = bundle.parent / 'native-compact-v2' / 'prepare.py'
    if digest(prior) != manifest['baseline_prepare_sha256']:
        raise ValueError('pinned baseline reconstruction changed')
    archive = bundle / 'source-overlay.tar.gz'
    if digest(archive) != manifest['archive_sha256']:
        raise ValueError('conditional source archive changed')
    baseline_report = args.report.with_name(args.report.stem + '-baseline.json')
    command = [sys.executable, '-I', '-B', str(prior), '--output', str(args.output),
               '--report', str(baseline_report)]
    if args.upstream_archive:
        command += ['--upstream-archive', str(args.upstream_archive)]
    subprocess.run(command, check=True)
    spec = importlib.util.spec_from_file_location('baseline_prepare', prior)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    files = helpers.snapshot(args.output)
    if helpers.tree_digest(files) != manifest['baseline_source_tree_sha256']:
        raise ValueError('complete baseline tree changed')
    for change in manifest['changes']:
        if files.get(change['path']) != change['before']:
            raise ValueError('conditional precondition failed: ' + change['path'])
    with tarfile.open(archive, 'r:gz') as source:
        entries = source.getmembers()
        expected = {change['path'] for change in manifest['changes']}
        if (len(entries) != len(expected) or {entry.name for entry in entries} != expected
                or any(not entry.isfile() for entry in entries)):
            raise ValueError('conditional source archive paths differ')
        source.extractall(args.output, members=entries, filter='data')
    for change in manifest['changes']:
        files[change['path']] = change['after']
    actual = helpers.snapshot(args.output)
    if (actual != files or len(actual) != manifest['source_file_count']
            or helpers.tree_digest(actual) != manifest['source_tree_sha256']):
        raise ValueError('complete conditional source tree differs')
    report = dict(status='SOURCE_RECONSTRUCTION_VERIFIED',
                  manifest_sha256=MANIFEST_SHA256,
                  source_tree_sha256=manifest['source_tree_sha256'],
                  changed_files=len(manifest['changes']), source_files=len(actual),
                  compilation='NOT_RUN_BY_PREPARATION', tests='NOT_RUN_BY_PREPARATION')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
