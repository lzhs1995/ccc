"""Record and archive a built app on CI; never launches or re-signs it."""
from pathlib import Path
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
from datetime import datetime, timezone


def run(arguments, *, cwd=None, timeout=90):
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {arguments[0]}\n{result.stderr}')
    return {'argv': arguments, 'stdout': result.stdout, 'stderr': result.stderr}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(app):
    entries = []
    for path in sorted(app.rglob('*')):
        info = path.lstat()
        entry = {'path': path.relative_to(app).as_posix(), 'mode': stat.S_IMODE(info.st_mode)}
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(app):
                raise ValueError('nonportable app symlink: ' + entry['path'])
            entry.update(type='symlink', target=target)
        elif path.is_file():
            entry.update(type='file', size=info.st_size, sha256=digest(path))
        elif path.is_dir():
            entry.update(type='directory')
        else:
            raise ValueError('special file in app: ' + entry['path'])
        entries.append(entry)
    if not entries:
        raise ValueError('empty app')
    return entries


def main():
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('only disposable GitHub runner admitted')
    app_arg, bundle_id, tag, preparation_arg, evidence_arg, checkout_arg = sys.argv[1:]
    app = Path(app_arg).resolve(strict=True)
    preparation = Path(preparation_arg).resolve(strict=True)
    evidence = Path(evidence_arg).resolve(strict=True)
    checkout = Path(checkout_arg).resolve(strict=True)
    output = evidence / 'retained-app'
    runner_temp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    if not app.is_relative_to(runner_temp / 'nested-retained-derived/Build/Products/Debug'):
        raise ValueError('app outside admitted build path')
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != bundle_id:
        raise ValueError('unexpected app identity')
    if info.get('LSEnvironment', {}).get('CMUX_TAG') != tag:
        raise ValueError('unexpected app tag')
    binary = app / 'Contents/MacOS' / info['CFBundleExecutable']
    binary.resolve(strict=True).relative_to(app)
    signatures = {
        'verify': run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)]),
        'display': run(['/usr/bin/codesign', '--display', '--verbose=4', str(app)]),
        'entitlements': run(['/usr/bin/codesign', '--display', '--entitlements', ':-', str(app)]),
    }
    entries = inventory(app)
    archive = output / 'cmux-nested-fixed.zip'
    if archive.exists():
        raise ValueError('archive already exists')
    run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], timeout=300)
    readback = output / 'archive-readback'
    if readback.exists():
        raise ValueError('readback path already exists')
    run(['/usr/bin/ditto', '-x', '-k', str(archive), str(readback)], timeout=300)
    extracted_app = readback / app.name
    if inventory(extracted_app) != entries:
        raise ValueError('archived app differs from signed source app')
    signatures['archive_verify'] = run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(extracted_app)])
    contract = json.loads((preparation / 'contract.json').read_text())
    receipts = {phase: json.loads((evidence / phase / 'VERDICT.json').read_text())
                for phase in ('red', 'green', 'boundary', 'existing-binding', 'package')}
    for phase, receipt in receipts.items():
        if receipt.get('phase') != phase or receipt.get('executed_test_count', 0) <= 0:
            raise ValueError('invalid test receipt: ' + phase)
        for key in ('tests_patch_sha256', 'fix_patch_sha256'):
            if receipt.get(key) != contract[key]:
                raise ValueError('test receipt belongs to another patch: ' + phase)
    provenance = {
        'status': 'CI_BUILD_ARCHIVE_VERIFIED_NOT_LAUNCHED',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'upstream_commit': contract['upstream_commit'],
        'preparation_commit': os.environ['GITHUB_SHA'],
        'green_source_commit': run(['git', 'rev-parse', 'HEAD'], cwd=checkout)['stdout'].strip(),
        'green_source_tree': run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=checkout)['stdout'].strip(),
        'tests_patch_sha256': digest(preparation / 'tests.patch'),
        'fix_patch_sha256': digest(preparation / 'fix.patch'),
        'workflow_run_id': os.environ['GITHUB_RUN_ID'],
        'workflow_run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
        'app_name': app.name, 'bundle_id': bundle_id, 'tag': tag,
        'build_configuration': 'Debug', 'backend_mode': 'local',
        'source_shared_with_green_tests': True,
        'same_binary_as_isolated_test_host': False,
        'signature_scope': 'Official reload ad-hoc signature; no Developer ID or notarization claimed.',
        'signatures': signatures,
        'architecture': run(['/usr/bin/lipo', '-archs', str(binary)]),
        'xcode': run(['/usr/bin/xcodebuild', '-version']),
        'swift': run(['/usr/bin/xcrun', 'swift', '--version']),
        'test_receipts': receipts,
        'archive': {'name': archive.name, 'size': archive.stat().st_size, 'sha256': digest(archive)},
        'bundle_members': entries,
        'full_nested_restore_verified': False,
        'relocation_note': 'Tag Info.plist contains original CI paths. Future GUI harness must rebind runtime paths explicitly or run from the retained original path; copying alone is not a GUI admission proof.',
    }
    (output / 'PROVENANCE.json').write_text(json.dumps(provenance, indent=2) + '\n')
    # Keep readback out of artifact upload without deleting any original evidence.
    readback.rename(runner_temp / ('nested-verified-readback-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']))


if __name__ == '__main__':
    main()
