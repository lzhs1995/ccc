#!/usr/bin/env python3
"""Reconstruct frozen v5 and add only the failed child exit-status diagnostic."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


V5_PREPARE_SHA256 = '06531cc8bc303498b9f5e2ba78928601f1095e8b338e7619ac14a4327d2dded1'
V5_MANIFEST_SHA256 = 'c21b1870721d2f35ceefe0848efd782e387c01b5df04737f2107991b80f2a4e8'
V5_TREE_SHA256 = '2dfe43955fb77426c5eade230041ce0830a58e0fab72a85d07e6c62e3103c82a'
BWRAP_SHA256 = '0ace0dddb89bae7ebae2f4d552ce091474ed768f15638b7215035c250c4b1cbb'
MEMBER = 'codex-rs/linux-sandbox/src/bwrap.rs'
BEFORE = '''            "actual /dev/fd mask failed: {}\\n{}",
            String::from_utf8_lossy(&output.stdout),
'''
AFTER = '''            "actual /dev/fd mask failed (status: {:?}): {}\\n{}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--upstream-archive', type=Path)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    v5 = bundle.parent / 'native-compact-conditional-v5'
    if digest(v5 / 'prepare.py') != V5_PREPARE_SHA256:
        raise ValueError('frozen v5 preparer changed')
    if digest(v5 / 'source-manifest.json') != V5_MANIFEST_SHA256:
        raise ValueError('frozen v5 manifest changed')
    command = [sys.executable, '-I', '-B', str(v5 / 'prepare.py'),
               '--output', str(args.output),
               '--report', str(args.report.with_name('v5-reconstruction.json'))]
    if args.upstream_archive:
        command += ['--upstream-archive', str(args.upstream_archive)]
    subprocess.run(command, check=True)
    helpers_path = bundle.parent / 'native-compact-v2' / 'prepare.py'
    spec = importlib.util.spec_from_file_location('frozen_native_snapshot', helpers_path)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    before_tree = helpers.snapshot(args.output)
    if helpers.tree_digest(before_tree) != V5_TREE_SHA256:
        raise ValueError('full frozen v5 tree changed')
    path = args.output / MEMBER
    if digest(path) != BWRAP_SHA256:
        raise ValueError('frozen bwrap source changed')
    original = path.read_text()
    if original.count(BEFORE) != 1:
        raise ValueError('diagnostic must replace exactly one message')
    updated = original.replace(BEFORE, AFTER, 1)
    path.write_text(updated)
    after_tree = helpers.snapshot(args.output)
    changed = [name for name in set(before_tree) | set(after_tree)
               if before_tree.get(name) != after_tree.get(name)]
    if changed != [MEMBER] or len(before_tree) != len(after_tree):
        raise ValueError('diagnostic changed more than the one source member')
    if after_tree[MEMBER]['git_mode'] != before_tree[MEMBER]['git_mode']:
        raise ValueError('diagnostic changed source mode')
    record = dict(
        status='DIAGNOSTIC_ONLY_SOURCE_VERIFIED_NOT_ADOPTED',
        frozen_v5_source_tree_sha256=V5_TREE_SHA256,
        source_tree_sha256=helpers.tree_digest(after_tree),
        changed_files=changed, source_files=len(after_tree),
        before=before_tree[MEMBER], after=after_tree[MEMBER],
        status_success_assertion='UNCHANGED',
        device_denial_and_marker_assertions='UNCHANGED',
        signal='NOT_MEASURED_BY_PREPARATION', compilation='NOT_RUN',
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
