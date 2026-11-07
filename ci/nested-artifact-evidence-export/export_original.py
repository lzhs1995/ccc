"""Bounded original native-build evidence export; GitHub Mac only, never launches app.

This bootstrap requires successful original API pins, not an application pin.
It produces authenticated review material, never an approved APP pin.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import sys
import zipfile

from support import stage_fixed_artifact as stage
from support import fixed_app_admission as app

MIB = 1024**2
MAX_SMALL = 96 * MIB
MAX_ENVELOPE = 100 * MIB
MAX_MEMBERS = 100000
PHASES = ('red', 'green', 'boundary', 'existing-binding', 'package')
APP_PHASES = PHASES[:-1]
SMALL = 128 * 1024
SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
need = stage.need
digest = stage.digest
decode = stage.decode
canonical = app.canonical
write_once = stage.write_once

REQUIRED = {
    'red-commit.txt': SMALL,
    'green-commit.txt': SMALL,
    'retained-app/PROVENANCE.json': 8 * MIB,
    'app-host-cleanup.log': 8 * MIB,
}
for phase in PHASES:
    for name in ('VERDICT.json', 'command-argv.txt', 'exit-code.txt', 'executed-count.txt'):
        REQUIRED[phase + '/' + name] = SMALL
    REQUIRED[phase + '/execution.log'] = 8 * MIB
for phase in APP_PHASES:
    for name in ('tests.json', 'result.tests.json'):
        REQUIRED[phase + '/' + name] = 4 * MIB
REQUIRED['package/wrapper.log'] = 8 * MIB
APP_ARCHIVE = 'retained-app/cmux-nested-fixed.zip'


def source_closure(source, wanted):
    source = Path(source)
    need(source.is_absolute() and source == source.resolve(strict=True), 'source root is linked')
    manifest = decode(stage.pinned_bytes(source / 'SOURCE-MANIFEST.json', wanted))
    actual = {}
    for path in source.rglob('*'):
        need(not path.is_symlink(), 'linked exporter source member')
        if path.is_file() and path != source / 'SOURCE-MANIFEST.json':
            actual[path.relative_to(source).as_posix()] = digest(stage.read_regular(path))
        else:
            need(path.is_dir() or path == source / 'SOURCE-MANIFEST.json', 'special exporter source member')
    need(manifest.get('files') == actual, 'exporter complete source closure differs')
    need(stage.read_regular(source / 'export_original.py') == stage.read_regular(Path(__file__).resolve()),
         'running exporter differs from pinned source')
    return manifest


def contract(value):
    need(value.get('schema') == 'original-cmux-build-artifact-export-v1'
         and value.get('status') == 'PINNED_ORIGINAL_SUCCESSFUL_ARTIFACT',
         'original successful artifact pin is pending')
    need(value.get('repository') == 'lzhs1995/ccc'
         and value.get('workflow_path') == '.github/workflows/nested-native-red-green.yml',
         'original repository or workflow differs')
    for key in ('original_run_id', 'original_run_attempt', 'original_job_id', 'artifact_id', 'artifact_size_in_bytes'):
        need(type(value.get(key)) is int and value[key] > 0, 'unfilled numeric original pin: ' + key)
    need(value['artifact_size_in_bytes'] <= stage.MAX_OUTER, 'original archive exceeds bound')
    for key in ('artifact_api_digest_sha256', 'original_run_json_sha256', 'original_jobs_json_sha256',
                'original_artifact_json_sha256', 'tests_patch_sha256', 'fix_patch_sha256'):
        need(isinstance(value.get(key), str) and SHA.fullmatch(value[key]), 'unfilled original digest: ' + key)
    for key in ('original_head_sha', 'upstream_commit', 'expected_green_tree'):
        need(isinstance(value.get(key), str) and COMMIT.fullmatch(value[key]), 'unfilled original source identity')
    need(value['upstream_commit'] == '2e109a1739d320fe36220afb3e7775b59c1acd47', 'wrong upstream')
    need(value.get('artifact_name') == f"nested-native-{value['original_run_id']}-{value['original_run_attempt']}",
         'artifact name differs')
    need(value.get('app_pin_created') is False, 'export may not claim an application pin')
    return value


def api_join(c, raw):
    run, jobs, artifact = (decode(raw[name]) for name in ('BUILD-RUN.json', 'BUILD-JOBS.json', 'ARTIFACT.json'))
    need(run.get('repository', {}).get('full_name') == c['repository']
         and run.get('id') == c['original_run_id'] and run.get('run_attempt') == c['original_run_attempt']
         and run.get('head_sha') == c['original_head_sha'] and run.get('path') == c['workflow_path']
         and run.get('status') == 'completed' and run.get('conclusion') == 'success',
         'original run is not the exact successful attempt')
    rows = jobs.get('jobs', [])
    need(jobs.get('total_count') == 1 and len(rows) == 1, 'original jobs response is partial or unexpected')
    job = rows[0]
    need(job.get('id') == c['original_job_id'] and job.get('run_id') == c['original_run_id']
         and job.get('run_attempt') == c['original_run_attempt'] and job.get('head_sha') == c['original_head_sha']
         and job.get('name') == 'regression' and job.get('status') == 'completed'
         and job.get('conclusion') == 'success', 'original job differs or did not succeed')
    need(artifact.get('id') == c['artifact_id'] and artifact.get('name') == c['artifact_name']
         and artifact.get('size_in_bytes') == c['artifact_size_in_bytes']
         and artifact.get('digest') == 'sha256:' + c['artifact_api_digest_sha256']
         and artifact.get('expired') is False and artifact.get('workflow_run', {}).get('id') == c['original_run_id']
         and artifact.get('workflow_run', {}).get('head_sha') == c['original_head_sha'],
         'original artifact API identity differs')
    return {'run': run, 'job': job, 'artifact': artifact}


def prepare(contract_path, contract_sha, metadata):
    raw_contract = stage.pinned_bytes(contract_path, contract_sha)
    c = contract(decode(raw_contract))  # Pending rejects before metadata or network.
    metadata = Path(metadata)
    need(metadata.is_absolute() and metadata == metadata.resolve(strict=True), 'metadata path is linked')
    raw = {}
    for name, key in [('BUILD-RUN.json', 'original_run_json_sha256'),
                      ('BUILD-JOBS.json', 'original_jobs_json_sha256'),
                      ('ARTIFACT.json', 'original_artifact_json_sha256')]:
        raw[name] = stage.pinned_bytes(metadata / name, c[key])
    joined = api_join(c, raw)
    return {'contract': c, 'raw_contract': raw_contract, 'raw_metadata': raw,
            'contract_sha256': contract_sha, 'api': joined}


def zip_index(z):
    rows = z.infolist()
    need(0 < len(rows) <= MAX_MEMBERS, 'outer ZIP member count exceeds bound')
    names = {}
    for row in rows:
        name = row.filename[:-1] if row.is_dir() else row.filename
        parts = name.split('/')
        need(name and '\\' not in name and not name.startswith('/')
             and all(part not in ('', '.', '..') for part in parts), 'unsafe original ZIP name')
        need(name not in names and not row.flag_bits & 1, 'duplicate or encrypted original ZIP member')
        mode = row.external_attr >> 16
        kind = stat.S_IFMT(mode)
        need(kind in (0, stat.S_IFREG, stat.S_IFDIR)
             and (not row.is_dir() or kind in (0, stat.S_IFDIR))
             and (row.is_dir() or kind != stat.S_IFDIR), 'special original ZIP member')
        need(row.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), 'unsupported ZIP compression')
        names[name] = row
    for name, row in names.items():
        for i in range(1, len(name.split('/'))):
            parent = '/'.join(name.split('/')[:i])
            need(parent not in names or names[parent].is_dir(), 'ZIP file/directory prefix collision')
    return names


def complete_member(z, row, limit):
    need(not row.is_dir() and row.file_size <= limit, 'required original member exceeds bound')
    with z.open(row) as stream:
        raw = stream.read(limit + 1)
    need(len(raw) == row.file_size and len(raw) <= limit, 'incomplete or oversized original member')
    return raw


def collect_originals(z, index):
    result = {}
    for name, limit in REQUIRED.items():
        need(name in index, 'required original member missing: ' + name)
        result[name] = complete_member(z, index[name], limit)
    need(sum(map(len, result.values())) <= MAX_SMALL, 'small original evidence exceeds total bound')
    for phase in APP_PHASES:
        need(result[phase + '/tests.json'] == result[phase + '/result.tests.json'],
             'original typed copy differs from native wrapper export')
    # The package producer has no typed tests JSON. Never synthesize one.
    need('package/tests.json' not in index and 'package/result.tests.json' not in index,
         'unexpected typed package evidence outside original producer contract')
    return result


def validate_provenance(c, originals, expected):
    p = decode(originals['retained-app/PROVENANCE.json'])
    for key, wanted in {
        'status': 'CI_BUILD_ARCHIVE_VERIFIED_NOT_LAUNCHED', 'upstream_commit': c['upstream_commit'],
        'preparation_commit': c['original_head_sha'], 'workflow_run_id': str(c['original_run_id']),
        'workflow_run_attempt': str(c['original_run_attempt']), 'tests_patch_sha256': c['tests_patch_sha256'],
        'fix_patch_sha256': c['fix_patch_sha256'], 'build_configuration': 'Debug', 'backend_mode': 'local',
        'app_name': f"cmux Nested Restore {c['original_run_id']}-{c['original_run_attempt']}.app",
        'bundle_id': f"com.cmuxterm.app.debug.nestedrestore.r{c['original_run_id']}a{c['original_run_attempt']}",
        'tag': f"nested-{c['original_run_id']}-{c['original_run_attempt']}",
        'source_shared_with_green_tests': True, 'same_binary_as_isolated_test_host': False,
        'full_nested_restore_verified': False,
    }.items():
        need(p.get(key) == wanted, 'original provenance identity differs: ' + key)
    for key in ('tests_patch_sha256', 'fix_patch_sha256', 'upstream_commit'):
        need(expected.get(key) == c[key], 'original test contract differs: ' + key)
    for name in ('red', 'green'):
        raw = originals[name + '-commit.txt']
        need(re.fullmatch(rb'[0-9a-f]{40}\n', raw), 'invalid original commit text')
    need(originals['green-commit.txt'].decode().strip() == p.get('green_source_commit')
         and p.get('green_source_tree') == c['expected_green_tree'],
         'green identity differs from independently reconstructed source tree')
    need(originals['red-commit.txt'] != originals['green-commit.txt'], 'red/green commits are identical')
    need(originals['red/command-argv.txt'] == originals['green/command-argv.txt'],
         'original red and green commands differ')
    receipts = p.get('test_receipts', {})
    need(set(receipts) == set(PHASES), 'original phase receipt set differs')
    for phase in PHASES:
        receipt = decode(originals[phase + '/VERDICT.json'])
        need(receipt == receipts[phase] and receipt.get('phase') == phase,
             'provenance differs from original phase receipt')
        need(receipt.get('tests_patch_sha256') == c['tests_patch_sha256']
             and receipt.get('fix_patch_sha256') == c['fix_patch_sha256'], 'phase uses another patch')
        need(int(originals[phase + '/exit-code.txt']) == receipt.get('exit_code'), 'phase original exit differs')
        if phase == 'package':
            names = receipt.get('passed_display_names', [])
            need(len(names) == len(set(names)) == 6
                 and set(names) == set(expected['package_test_display_names'])
                 and receipt.get('executed_test_count') == 6, 'package receipt inventory differs')
        else:
            key = {'red': 'required_green_test_ids', 'green': 'required_green_test_ids',
                   'boundary': 'boundary_test_ids', 'existing-binding': 'existing_binding_test_ids'}[phase]
            results = receipt.get('results', {})
            wanted_results = {name: ('Failed' if phase == 'red' and name in expected['expected_red_test_ids']
                                    else 'Passed') for name in expected[key]}
            need(results == wanted_results and receipt.get('executed_test_count') == len(wanted_results),
                 'app receipt case set or status differs')
            need(receipt.get('exit_code') == (65 if phase == 'red' else 0), 'app receipt exit differs')
    archive = p.get('archive', {})
    need(archive.get('name') == 'cmux-nested-fixed.zip' and type(archive.get('size')) is int
         and 0 < archive['size'] <= stage.MAX_APP and SHA.fullmatch(archive.get('sha256', '')),
         'original app archive facts missing')
    return p


def optional_originals(z, index, originals):
    names = ['retained-app/build.log']
    names += ['build-' + originals[phase + '-commit.txt'].decode().strip() + '.log' for phase in ('red', 'green')]
    facts = {}
    for name in names:
        row = index.get(name)
        if row is None:
            facts[name] = {'exported': False, 'reason': 'not_in_original_artifact'}
        elif row.is_dir() or row.file_size > 8 * MIB or sum(map(len, originals.values())) + row.file_size > MAX_SMALL:
            facts[name] = {'exported': False, 'reason': 'original_exceeds_optional_export_bound',
                           'original_bytes': row.file_size}
        else:
            originals[name] = complete_member(z, row, 8 * MIB)
            facts[name] = {'exported': True, 'bytes': len(originals[name]), 'sha256': digest(originals[name])}
    return facts


def observe_app(archive, workspace, p):
    """Only fresh GitHub runner calls this; inspection commands never execute app."""
    app.inspect_zip(archive, p['app_name'])
    destination = workspace / 'app-readback'
    destination.mkdir(mode=0o700)
    subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(destination)], check=True, timeout=300)
    bundle = app.canonical_child(destination, p['app_name'])
    rows = app.inventory(bundle)
    need(rows == p.get('bundle_members'), 'actual complete archived bundle differs from original manifest')
    plist_path = app.canonical_child(bundle, 'Contents/Info.plist')
    raw_plist = stage.read_regular(plist_path, SMALL)
    info = plistlib.loads(raw_plist)
    need(info.get('CFBundleIdentifier') == p['bundle_id']
         and info.get('LSEnvironment', {}).get('CMUX_TAG') == p['tag'], 'actual plist identity differs')
    executable = info.get('CFBundleExecutable')
    need(isinstance(executable, str) and executable not in ('', '.', '..')
         and '/' not in executable and '\\' not in executable, 'unsafe bundle executable')
    main = app.canonical_child(bundle, 'Contents/MacOS/' + executable)
    cli = app.canonical_child(bundle, 'Contents/Resources/bin/cmux')
    for path in (main, cli):
        need(stat.S_ISREG(path.lstat().st_mode) and path.stat().st_mode & 0o111, 'actual app executable missing')
    verified = app.command(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(bundle)])
    displayed = app.command(['/usr/bin/codesign', '--display', '--verbose=4', str(bundle)])
    entitlements = app.command(['/usr/bin/codesign', '--display', '--entitlements', ':-', str(bundle)])
    text = displayed['stdout'] + displayed['stderr']
    cdhash = re.findall(r'^CDHash=([0-9a-f]{40,64})$', text, re.M)
    need(re.findall(r'^Identifier=(.+)$', text, re.M) == [p['bundle_id']] and len(cdhash) == 1
         and 'Signature=adhoc' in text and re.findall(r'^TeamIdentifier=(.+)$', text, re.M) == ['not set'],
         'actual app signature identity differs')
    original_display = p['signatures']['display']['stdout'] + p['signatures']['display']['stderr']
    need(re.findall(r'^CDHash=(.+)$', original_display, re.M) == cdhash,
         'actual code directory hash differs from original display')
    architecture = app.command(['/usr/bin/lipo', '-archs', str(main)])
    need(architecture['stdout'].split() == p['architecture']['stdout'].split() == ['arm64'],
         'actual architecture differs')
    return raw_plist, {
        'schema': 'original-build-app-observation-v1', 'status': 'OBSERVED_NOT_ADMITTED_OR_LAUNCHED',
        'app_name': p['app_name'], 'bundle_id': p['bundle_id'], 'tag': p['tag'],
        'bundle_executable': executable, 'code_directory_hash': cdhash[0], 'architecture': ['arm64'],
        'application_sha256': app.sha(main), 'cli_sha256': app.sha(cli),
        'info_plist_sha256': digest(raw_plist), 'app_inventory_sha256': digest(canonical(rows)),
        'complete_inventory_matched': True, 'member_count': len(rows),
        'signatures': {'verify': verified, 'display': displayed, 'entitlements': entitlements},
        'architecture_command': architecture, 'app_launched': False, 'app_modified': False,
        'app_pin_created': False, 'gui_recovery_verified': False,
    }


def original_receipt(plan, originals, observation, optional, exporter):
    c = plan['contract']
    return {
        'schema': 'original-build-small-evidence-receipt-v1',
        'status': 'ORIGINAL_BYTES_EXPORTED_REQUIRES_INDEPENDENT_REVIEW',
        'original_build': {key: c[key] for key in ('repository', 'original_run_id', 'original_run_attempt',
            'original_head_sha', 'original_job_id', 'artifact_id', 'artifact_name', 'artifact_size_in_bytes',
            'artifact_api_digest_sha256')},
        'contract_sha256': plan['contract_sha256'], 'exporter': exporter,
        'original_members': {name: {'bytes': len(raw), 'sha256': digest(raw)} for name, raw in sorted(originals.items())},
        'optional_compile_logs': optional, 'app_observation_sha256': digest(canonical(observation)),
        'original_metadata_sha256': {name: digest(raw) for name, raw in plan['raw_metadata'].items()},
        'full_original_zip_digest_verified': True, 'api_requests': 1, 'blob_requests': 1, 'retries': 0,
        'original_typed_tests_not_regenerated': True, 'package_typed_tests_exist': False,
        'independent_typed_log_and_source_tree_review_required': True,
        'commit_texts_prove_parent_chain': False, 'app_pin_created': False, 'app_launched': False,
        'gui_recovery_verified': False,
    }


def seal_payload(members, context, key, output):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    need(len(key) == 32, 'AES key must be exactly 32 bytes')
    need(sum(map(len, members.values())) <= MAX_SMALL, 'export payload exceeds small evidence bound')
    envelope = io.BytesIO()
    with zipfile.ZipFile(envelope, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for name, raw in sorted(members.items()):
            need(name and not PurePosixPath(name).is_absolute() and '\\' not in name
                 and all(x not in ('', '.', '..') for x in name.split('/')), 'unsafe sealed export name')
            zi = zipfile.ZipInfo(name); zi.external_attr = (stat.S_IFREG | 0o600) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, raw)
    plaintext = envelope.getvalue()
    need(len(plaintext) <= MAX_ENVELOPE, 'export ZIP exceeds envelope bound')
    aad = canonical(context)
    nonce = os.urandom(12)
    cipher = nonce + AESGCM(key).encrypt(nonce, plaintext, aad)
    output.mkdir(mode=0o700)
    write_once(output / 'evidence.aesgcm', cipher)
    public = {'schema': 'original-build-small-evidence-envelope-v1', 'aad': context,
              'ciphertext_bytes': len(cipher), 'ciphertext_sha256': digest(cipher),
              'plaintext_zip_sha256': digest(plaintext), 'uncompressed_limit_bytes': MAX_SMALL,
              'member_count': len(members), 'app_pin_created': False, 'app_launched': False}
    write_once(output / 'ENVELOPE.json', canonical(public) + b'\n')
    return public


def open_payload(cipher, public, context, key):
    """Offline small-export verifier; caller supplies independently pinned exporter context."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    need(public.get('aad') == context and public.get('ciphertext_sha256') == digest(cipher)
         and public.get('ciphertext_bytes') == len(cipher) and len(cipher) <= MAX_ENVELOPE + 28,
         'sealed export identity or size differs')
    raw = AESGCM(key).decrypt(cipher[:12], cipher[12:], canonical(context))
    need(digest(raw) == public.get('plaintext_zip_sha256'), 'plaintext export hash differs')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        index = zip_index(z)
        need(len(index) == public.get('member_count') and sum(r.file_size for r in index.values()) <= MAX_SMALL,
             'sealed export expansion differs')
        result = {name: complete_member(z, row, MAX_SMALL) for name, row in index.items()}
    return result


def runner_identity():
    need(sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
         and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
         and os.environ.get('GITHUB_REPOSITORY') == 'lzhs1995/ccc',
         'only a disposable GitHub Mac may acquire original artifact')
    result = {key: os.environ.get(key, '') for key in
              ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA')}
    need(result['GITHUB_RUN_ID'].isdecimal() and result['GITHUB_RUN_ATTEMPT'].isdecimal()
         and COMMIT.fullmatch(result['GITHUB_SHA']), 'exporter identity missing')
    return result


def execute(plan, source, workspace, output, exporter, *, download=stage.download_response, observer=observe_app):
    c = plan['contract']
    trees = decode(stage.read_regular(source / 'EXPECTED-TREES.json'))
    need(trees.get('upstream_commit') == c['upstream_commit']
         and trees.get('expected_trees', {}).get('green') == c['expected_green_tree'],
         'contract source tree differs from independently reviewed source before transfer')
    need(str(c['original_run_id']) != exporter['GITHUB_RUN_ID'], 'exporter must be a separate original run')
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    for path in (workspace, output):
        need(path.is_absolute() and path == path.resolve(strict=False) and path != temporary
             and path.parent.is_relative_to(temporary) and path.parent.is_dir()
             and not path.exists() and not path.is_symlink(), 'fresh contained export paths required')
    need(workspace != output and not output.is_relative_to(workspace), 'public output must be separate from scratch')
    import base64
    try:
        key = base64.b64decode(os.environ.get('CCC_PRIVATE_FIXTURE_KEY', ''), validate=True)
    except ValueError:
        raise ValueError('invalid export encryption key') from None
    need(len(key) == 32, 'export encryption key missing before transfer')
    stage.capacity(workspace.parent, c['artifact_size_in_bytes'] + 2 * stage.MAX_APP + stage.RESERVE)
    workspace.mkdir(mode=0o700)
    archive = workspace / 'original-artifact.zip'
    with download({'pin': {'artifact_id': c['artifact_id']}}) as response:
        length = response.headers.get('Content-Length')
        need(length is None or (length.isdecimal() and int(length) == c['artifact_size_in_bytes']),
             'original download content length differs')
        stage.copy_bounded(response, archive, c['artifact_size_in_bytes'], c['artifact_api_digest_sha256'])
    expected = decode(stage.read_regular(source / 'ORIGINAL-TEST-CONTRACT.json'))
    with zipfile.ZipFile(archive) as z:
        index = zip_index(z)
        originals = collect_originals(z, index)
        provenance = validate_provenance(c, originals, expected)
        optional = optional_originals(z, index, originals)
        need(APP_ARCHIVE in index and index[APP_ARCHIVE].file_size == provenance['archive']['size'],
             'original app archive is absent or size differs')
        inner = workspace / 'original-app.zip'
        with z.open(index[APP_ARCHIVE]) as stream:
            stage.copy_bounded(stream, inner, provenance['archive']['size'], provenance['archive']['sha256'])
    stage.capacity(workspace, stage.MAX_APP + stage.RESERVE)
    raw_plist, observation = observer(inner, workspace, provenance)
    receipt = original_receipt(plan, originals, observation, optional, exporter)
    members = {'original/' + name: raw for name, raw in originals.items()}
    members.update({'metadata/' + name: raw for name, raw in plan['raw_metadata'].items()})
    members['metadata/EXPORT-CONTRACT.json'] = plan['raw_contract']
    members['observed/Info.plist'] = raw_plist
    members['observed/APP-OBSERVATION.json'] = canonical(observation)
    members['EXPORT-RECEIPT.json'] = canonical(receipt)
    context = {'schema': 'original-cmux-build-evidence-export-v1', 'exporter': exporter,
               'original_build': receipt['original_build'], 'contract_sha256': plan['contract_sha256']}
    return seal_payload(members, context, key, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-manifest-sha256', required=True)
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--contract-sha256', required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    exporter = runner_identity()
    source_closure(args.source, args.source_manifest_sha256)
    plan = prepare(args.contract, args.contract_sha256, args.metadata)
    if args.check_only:
        print('Original successful identity/API bytes verified; no network performed.')
        return
    print(json.dumps(execute(plan, args.source, args.workspace, args.output, exporter), sort_keys=True))


if __name__ == '__main__':
    main()
