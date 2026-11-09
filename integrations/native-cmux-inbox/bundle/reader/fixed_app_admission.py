"""Separate fixed-build admission. All app consumption is on a disposable GitHub Mac.

No download, launch, plist mutation or re-signing. A reviewed external pin is
required before any artifact can be consumed; build provenance is not its own pin.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import sys
import zipfile


class AdmissionError(ValueError):
    pass


def need(value, message):
    if not value:
        raise AdmissionError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs)


def require_pin(pin):
    need(pin.get('schema') == 'cmux-fixed-gui-artifact-pin-v1'
         and pin.get('status') == 'PINNED_ORIGINAL_BUILD', 'real fixed-app pin is not ready')
    for key in ('tests_patch_sha256', 'fix_patch_sha256', 'artifact_api_digest_sha256',
                'artifact_download_zip_sha256', 'provenance_sha256', 'retained_app_zip_sha256',
                'app_inventory_sha256', 'application_sha256', 'cli_sha256',
                'build_run_json_sha256', 'artifact_json_sha256', 'test_receipts_sha256'):
        need(isinstance(pin.get(key), str) and re.fullmatch('[0-9a-f]{64}', pin[key]), 'missing SHA pin: ' + key)
    for key in ('upstream_commit', 'build_preparation_commit', 'green_source_commit', 'green_source_tree'):
        need(isinstance(pin.get(key), str) and re.fullmatch('[0-9a-f]{40}', pin[key]), 'missing source pin: ' + key)
    for key in ('build_run_id', 'build_run_attempt', 'artifact_id', 'retained_app_zip_bytes'):
        need(type(pin.get(key)) is int and pin[key] > 0, 'missing numeric pin: ' + key)
    need(pin.get('build_repository') == 'lzhs1995/ccc', 'foreign build repository')
    need(pin.get('architecture') == ['arm64'], 'require original arm64 build')
    need(re.fullmatch(r'com\.cmuxterm\.app\.debug\.nestedrestore\.r[0-9]+a[0-9]+', pin.get('bundle_id', '')),
         'fixed build must have its own tagged bundle identity')
    need(pin['bundle_id'] == f"com.cmuxterm.app.debug.nestedrestore.r{pin['build_run_id']}a{pin['build_run_attempt']}",
         'bundle does not belong to original build run')
    need(pin.get('tag') == f"nested-{pin['build_run_id']}-{pin['build_run_attempt']}", 'tag differs from original build')
    for key in ('app_name', 'bundle_executable'):
        value = pin.get(key)
        need(isinstance(value, str) and value not in ('', '.', '..') and '/' not in value and '\\' not in value,
             'unsafe bundle component: ' + key)
    need(pin['app_name'].endswith('.app'), 'app name must include .app')
    need(re.fullmatch('[0-9a-f]{40,64}', pin.get('code_directory_hash', '')), 'missing code directory pin')
    need(pin.get('source_shared_with_green_tests') is True
         and pin.get('same_binary_as_isolated_test_host') is False
         and pin.get('full_nested_restore_verified') is False, 'unsupported build provenance claims')
    need(pin.get('artifact_members') == {'provenance': 'retained-app/PROVENANCE.json',
         'app_archive': 'retained-app/cmux-nested-fixed.zip'}, 'original artifact member contract differs')
    return pin


def canonical_child(root, relative, *, exists=True):
    need(isinstance(relative, str) and relative and '\\' not in relative, 'invalid relative artifact path')
    part = PurePosixPath(relative)
    need(not part.is_absolute() and all(x not in ('', '.', '..') for x in relative.split('/')), 'artifact path escapes root')
    root = Path(root).resolve(strict=True)
    target = root / relative
    need(not target.is_symlink(), 'artifact leaf is a symlink')
    resolved = target.resolve(strict=exists)
    need(resolved.is_relative_to(root) and resolved != root and resolved == target, 'artifact path is not contained')
    return target


def inventory(app):
    app = Path(app).resolve(strict=True)
    rows = []
    for item in sorted(app.rglob('*')):
        info = item.lstat()
        row = {'path': item.relative_to(app).as_posix(), 'mode': stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(item)
            need(not Path(target).is_absolute() and item.resolve(strict=True).is_relative_to(app), 'bundle symlink escapes app')
            row.update(type='symlink', target=target)
        elif stat.S_ISREG(info.st_mode):
            need(info.st_nlink == 1, 'bundle file has external hardlink')
            row.update(type='file', size=info.st_size, sha256=sha(item))
        elif stat.S_ISDIR(info.st_mode):
            row.update(type='directory')
        else:
            raise AdmissionError('special bundle member')
        rows.append(row)
    need(bool(rows), 'empty bundle')
    return rows


def validate_metadata(pin, run, artifact, provenance):
    require_pin(pin)
    need(run.get('id') == pin['build_run_id'] and run.get('run_attempt') == pin['build_run_attempt']
         and run.get('head_sha') == pin['build_preparation_commit']
         and run.get('repository', {}).get('full_name') == pin['build_repository']
         and run.get('status') == 'completed' and run.get('conclusion') == 'success', 'original build run mismatch')
    need(artifact.get('id') == pin['artifact_id'] and artifact.get('expired') is False
         and artifact.get('digest') == 'sha256:' + pin['artifact_api_digest_sha256']
         and artifact.get('workflow_run', {}).get('id') == pin['build_run_id']
         and artifact.get('workflow_run', {}).get('head_sha') == pin['build_preparation_commit'], 'original artifact mismatch')
    need(pin['artifact_api_digest_sha256'] == pin['artifact_download_zip_sha256'], 'API/download archive mismatch')
    for key, expected in {
        'status': 'CI_BUILD_ARCHIVE_VERIFIED_NOT_LAUNCHED',
        'upstream_commit': pin['upstream_commit'], 'preparation_commit': pin['build_preparation_commit'],
        'green_source_commit': pin['green_source_commit'], 'green_source_tree': pin['green_source_tree'],
        'tests_patch_sha256': pin['tests_patch_sha256'], 'fix_patch_sha256': pin['fix_patch_sha256'],
        'app_name': pin['app_name'], 'bundle_id': pin['bundle_id'], 'tag': pin['tag'],
        'build_configuration': 'Debug', 'backend_mode': 'local',
        'source_shared_with_green_tests': True, 'same_binary_as_isolated_test_host': False,
        'full_nested_restore_verified': False,
    }.items():
        need(provenance.get(key) == expected, 'provenance mismatch: ' + key)
    need(str(provenance.get('workflow_run_id')) == str(pin['build_run_id'])
         and str(provenance.get('workflow_run_attempt')) == str(pin['build_run_attempt']), 'provenance run mismatch')
    archive = provenance.get('archive', {})
    need(archive.get('name') == 'cmux-nested-fixed.zip' and archive.get('sha256') == pin['retained_app_zip_sha256']
         and archive.get('size') == pin['retained_app_zip_bytes'], 'retained archive mismatch')
    members = provenance.get('bundle_members')
    need(isinstance(members, list) and hashlib.sha256(canonical(members)).hexdigest() == pin['app_inventory_sha256'],
         'pinned member manifest mismatch')
    need(hashlib.sha256(canonical(provenance.get('test_receipts'))).hexdigest() == pin['test_receipts_sha256'],
         'independently reviewed original test receipts differ')
    expectations = read_json(Path(__file__).with_name('FIXED-TEST-EXPECTATIONS.json'))
    need(pin['upstream_commit'] == expectations['upstream_commit'], 'test contract upstream differs')
    for phase in ('red', 'green', 'boundary', 'existing-binding', 'package'):
        receipt = provenance.get('test_receipts', {}).get(phase, {})
        need(receipt.get('phase') == phase and type(receipt.get('executed_test_count')) is int
             and receipt['executed_test_count'] > 0, 'missing executed test receipt: ' + phase)
        for key in ('tests_patch_sha256', 'fix_patch_sha256'):
            need(receipt.get(key) == pin[key], 'test receipt source mismatch')
        if phase in ('green', 'boundary', 'existing-binding'):
            results = receipt.get('results', {})
            need(receipt.get('exit_code') == 0 and len(results) == receipt['executed_test_count']
                 and all(v == 'Passed' for v in results.values()), 'incomplete green receipt')
            need(set(results) == set(expectations[phase]), 'green test case inventory differs: ' + phase)
        elif phase == 'red':
            results = receipt.get('results', {})
            need(receipt.get('exit_code') == 65 and len(results) == receipt['executed_test_count']
                 and 'Failed' in results.values() and set(results.values()) <= {'Failed', 'Passed'},
                 'behavioral red receipt missing')
            need({k for k, v in results.items() if v == 'Failed'} == set(expectations['red_failed'])
                 and {k for k, v in results.items() if v == 'Passed'} == set(expectations['red_passed']),
                 'exact six red failures and five passes differ')
        else:
            names = receipt.get('passed_display_names', [])
            need(len(names) == len(set(names)) == receipt['executed_test_count']
                 and (receipt.get('exit_code') == 0 or (receipt.get('exit_code') == 1
                 and receipt.get('cosmetic_ghostty_binary_diagnostic') is True)), 'package receipt incomplete')
            need(set(names) == set(expectations['package']), 'package test case inventory differs')


def validate_artifact_members(archive, pin):
    """Join separately retained inputs to the actual API archive bytes."""
    expected = {pin['artifact_members']['provenance']: pin['provenance_sha256'],
                pin['artifact_members']['app_archive']: pin['retained_app_zip_sha256']}
    with zipfile.ZipFile(archive) as source:
        rows = source.infolist()
        names = [row.filename for row in rows]
        need(len(names) == len(set(names)), 'duplicate original artifact member')
        for name, digest in expected.items():
            need(names.count(name) == 1, 'original artifact member missing: ' + name)
            row = source.getinfo(name)
            need(not row.is_dir() and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG),
                 'original artifact member is not regular')
            bound = pin['retained_app_zip_bytes'] if name.endswith('.zip') else 32 * 1024**2
            need(row.file_size <= bound and (not name.endswith('.zip') or row.file_size == bound),
                 'original artifact member size differs')
            with source.open(row) as stream:
                need(hashlib.file_digest(stream, 'sha256').hexdigest() == digest, 'original artifact member bytes differ')


def validate_bundle(app, pin, provenance):
    require_pin(pin)
    app = Path(app)
    need(not app.is_symlink() and app.resolve(strict=True).name == pin['app_name'], 'wrong bundle path/name')
    app = app.resolve(strict=True)
    info = plistlib.loads(canonical_child(app, 'Contents/Info.plist').read_bytes())
    need(info.get('CFBundleIdentifier') == pin['bundle_id'], 'wrong bundle identifier')
    need(info.get('CFBundleExecutable') == pin['bundle_executable'], 'wrong bundle executable')
    need(info.get('LSEnvironment', {}).get('CMUX_TAG') == pin['tag'], 'wrong embedded tag')
    need(inventory(app) == provenance['bundle_members'], 'bundle member manifest mismatch')
    exe = canonical_child(app, 'Contents/MacOS/' + pin['bundle_executable'])
    cli = canonical_child(app, 'Contents/Resources/bin/cmux')
    for item, key in ((exe, 'application_sha256'), (cli, 'cli_sha256')):
        need(item.is_file() and os.access(item, os.X_OK) and sha(item) == pin[key], 'executable identity mismatch')
    canonical_child(app, 'Contents/Resources/shell-integration')
    return exe, cli, info


def inspect_zip(archive, app_name):
    """Validate entries before ditto preserves the signed bundle/resource forks."""
    with zipfile.ZipFile(archive) as z:
        names = set(); symlinks = set(); total = 0
        for row in z.infolist():
            name = row.filename.rstrip('/')
            parts = name.split('/')
            need(name not in names and name and '\\' not in name
                 and all(x not in ('', '.', '..') for x in parts), 'unsafe or duplicate archive member')
            names.add(name)
            need(parts[0] == app_name or (name == '__MACOSX' and row.is_dir())
                 or (parts[0] == '__MACOSX' and len(parts) > 1
                 and parts[1] in (app_name, '._' + app_name)), 'foreign archive root')
            total += row.file_size
            need(total <= 3 * 1024**3 and len(names) <= 100000, 'archive expansion exceeds bound')
            mode = row.external_attr >> 16
            need(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK), 'special archive member')
            if stat.S_ISLNK(mode):
                target = z.read(row).decode('utf-8')
                need(not target.startswith('/') and '\\' not in target, 'unsafe archive symlink')
                resolved = list(parts[:-1])
                for bit in target.split('/'):
                    if bit == '..':
                        need(len(resolved) > 1, 'archive symlink escapes app'); resolved.pop()
                    elif bit not in ('', '.'):
                        resolved.append(bit)
                need(resolved[0] == app_name, 'archive symlink escapes app')
                symlinks.add(name)
        for name in names:
            need(not any('/'.join(name.split('/')[:i]) in symlinks for i in range(1, len(name.split('/')))),
                 'archive writes through symlink directory')


def runner_only():
    need(sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
         and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
         and os.environ.get('GITHUB_REPOSITORY') == 'lzhs1995/ccc', 'fixed app is consumed only on isolated GitHub Mac')


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=90, check=True)
    return {'argv': argv, 'stdout': result.stdout, 'stderr': result.stderr}


def verify_signature(app, pin):
    verified = command(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)])
    displayed = command(['/usr/bin/codesign', '--display', '--verbose=4', str(app)])
    text = displayed['stdout'] + displayed['stderr']
    need(re.findall(r'^Identifier=(.+)$', text, re.M) == [pin['bundle_id']], 'signature identifier differs')
    need(re.findall(r'^CDHash=(.+)$', text, re.M) == [pin['code_directory_hash']], 'code directory hash differs')
    need('Signature=adhoc' in text and re.findall(r'^TeamIdentifier=(.+)$', text, re.M) == ['not set'],
         'expected original reload ad-hoc signature')
    arch = command(['/usr/bin/lipo', '-archs', str(app / 'Contents/MacOS' / pin['bundle_executable'])])
    need(arch['stdout'].split() == pin['architecture'], 'binary architecture differs')
    return {'verify': verified, 'display': displayed, 'architecture': arch}


def admit(root, inputs, pin_path, pin_sha256):
    """Consume already downloaded original evidence; never acquires an app locally."""
    runner_only()
    need(re.fullmatch('[0-9a-f]{64}', pin_sha256 or ''), 'external app pin hash missing')
    need(sha(pin_path) == pin_sha256, 'reviewed app pin changed')
    pin = require_pin(read_json(pin_path))
    root = Path(root).resolve(strict=True)
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    need(root != temporary and root.is_relative_to(temporary) and root.stat().st_uid == os.geteuid()
         and not root.stat().st_mode & 0o077, 'fresh private consumer root required')
    inputs = Path(inputs).resolve(strict=True)
    need(inputs.is_relative_to(temporary) and inputs != temporary, 'artifact inputs outside runner temp')
    objects = {}
    for name, key in [('BUILD-RUN.json', 'build_run_json_sha256'), ('ARTIFACT.json', 'artifact_json_sha256'),
                      ('PROVENANCE.json', 'provenance_sha256'), ('artifact.zip', 'artifact_download_zip_sha256'),
                      ('cmux-nested-fixed.zip', 'retained_app_zip_sha256')]:
        item = canonical_child(inputs, name)
        need(sha(item) == pin[key], 'input content mismatch: ' + name)
        if name.endswith('.json'):
            objects[name] = read_json(item)
    validate_metadata(pin, objects['BUILD-RUN.json'], objects['ARTIFACT.json'], objects['PROVENANCE.json'])
    validate_artifact_members(inputs / 'artifact.zip', pin)
    archive = inputs / 'cmux-nested-fixed.zip'
    need(archive.stat().st_size == pin['retained_app_zip_bytes'], 'retained archive size mismatch')
    inspect_zip(archive, pin['app_name'])
    output = root / 'fixed-app'
    need(not output.exists() and not output.is_symlink(), 'fixed app output already exists')
    output.mkdir(mode=0o700)
    subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(output)], check=True, timeout=300)
    app = canonical_child(output, pin['app_name'])
    exe, cli, info = validate_bundle(app, pin, objects['PROVENANCE.json'])
    signatures = verify_signature(app, pin)
    return {'schema': 'fixed-app-consumer-admission-v1', 'passed': True, 'pin': pin,
            'pin_sha256': pin_sha256, 'app': str(app), 'exe': str(exe), 'cli': str(cli),
            'provenance': objects['PROVENANCE.json'], 'signatures': signatures,
            'info_plist_lsenvironment': info.get('LSEnvironment', {}),
            'consumer_run': os.environ['GITHUB_RUN_ID'], 'consumer_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
            'app_launched': False, 'full_nested_restore_verified': False}
