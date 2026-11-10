"""Authenticate one already-built Release artifact for a distinct validation run.

The original failure, package bytes and source-run identity are never changed.
All downloads are one-shot, bounded and restricted to the isolated GitHub Mac.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import stat
import subprocess
import time
import zipfile

import source_contract as shared
import release_package as package
import release_paired as paired
import evidence_release as evidence

need, save, digest = shared.need, shared.save, shared.digest
PIN_SHA256 = '91749e91cb07e194ad9a826d0c7d6c3168c61bc942aee50fd4e8cb8ef152f10c'
MAX_SMALL = 64 * 1024**2
MAX_APP = 3 * 1024**3


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, 'duplicate JSON member')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def relative(name):
    need(isinstance(name, str) and name and '\\' not in name and '\x00' not in name,
         'unsafe retained member name')
    value = PurePosixPath(name)
    need(not value.is_absolute() and all(p not in ('', '.', '..') for p in name.split('/'))
         and value.as_posix() == name, 'escaping retained member')
    return value


def pins():
    source = shared.ROOT / 'RETAINED-SOURCE.json'
    need(digest(source) == PIN_SHA256, 'retained source selection changed')
    value = shared.read_json(source)
    need(value.get('schema') == 'cmux-retained-release-source-v1', 'retained source schema changed')
    return value


def verify_source_closure():
    manifest = shared.ROOT / 'SOURCE-MANIFEST.json'
    frozen = shared.read_json(shared.ROOT / 'FROZEN.json')
    need(frozen['source_manifest_sha256'] == digest(manifest), 'validation source freeze differs')
    rows = shared.read_json(manifest)['files']
    need(isinstance(rows, list) and 0 < len(rows) <= 100, 'validation source closure out of bound')
    root = shared.ROOT.parents[1]
    seen = set()
    for row in rows:
        name = str(relative(row['public_path']))
        need(name not in seen, 'duplicate validation source member')
        seen.add(name)
        path = root / name
        need(not path.is_symlink() and path.resolve(strict=True) == path and path.is_file()
             and path.stat().st_size == row['bytes'] and digest(path) == row['sha256'],
             'validation source changed: ' + name)
    actual = {p.relative_to(root).as_posix() for p in shared.ROOT.rglob('*') if p.is_file()}
    control = {str(p.relative_to(root)) for p in (manifest, shared.ROOT / 'FROZEN.json')}
    need(actual == {n for n in seen if n.startswith('integrations/cmux-release-package/')} | control,
         'unexpected or missing validation source')
    return {'source_manifest_sha256': digest(manifest), 'source_freeze_sha256': digest(shared.ROOT / 'FROZEN.json'),
            'files': len(rows)}


def validate_context(value, selection, *, branch=None, event=None):
    original = selection['source_run']
    need(isinstance(value, dict) and set(value) == {'repository', 'head_sha', 'run_id', 'run_attempt'}
         and value['repository'] == original['repository']
         and re.fullmatch(r'[0-9a-f]{40}', value.get('head_sha', ''))
         and type(value['run_id']) is int and value['run_id'] > 0
         and type(value['run_attempt']) is int and value['run_attempt'] == selection['validation_run_attempt'],
         'invalid validation run context')
    need(value['run_id'] != original['run_id'] and value['head_sha'] != original['head_sha'],
         'source run cannot be relabeled as validation run')
    if branch is not None:
        need(branch == 'refs/heads/' + selection['validation_branch'], 'foreign validation branch')
    if event is not None:
        need(event in ('push', 'workflow_dispatch'), 'foreign validation trigger')
    return {'source_run': original, 'validation_run': value}


def validate_run_api(row, context, selection, *, source=False):
    prefix = 'source' if source else 'validation'
    need(row.get('id') == context['run_id'] and row.get('run_attempt') == context['run_attempt']
         and row.get('head_sha') == context['head_sha']
         and row.get('head_branch') == selection[prefix + '_branch']
         and row.get('path') == selection[prefix + '_workflow']
         and row.get('repository', {}).get('full_name') == context['repository']
         and row.get('repository', {}).get('id') == selection['repository_id'],
         'run API does not bind the selected ' + prefix + ' attempt')
    if source:
        need(row.get('event') == selection['source_event'] and row.get('status') == 'completed'
             and row.get('conclusion') == selection['source_conclusion'] == 'failure',
             'original known failed run must remain terminal and unchanged')
    else:
        need(row.get('event') in ('push', 'workflow_dispatch') and row.get('status') in ('queued', 'in_progress'),
             'validation must be its own current original attempt')


def validate_artifact(row, expected, selection, *, limit):
    source = selection['source_run']
    need(type(row.get('id')) is int and row['id'] == expected['id']
         and row.get('name') == expected['name'] and row.get('expired') is False
         and type(row.get('size_in_bytes')) is int and 0 < row['size_in_bytes'] <= limit
         and row['size_in_bytes'] == expected['size_in_bytes'] and row.get('digest') == expected['digest'],
         'retained artifact id/name/size/digest/expiry differs')
    run = row.get('workflow_run', {})
    need(run.get('id') == source['run_id'] and run.get('head_sha') == source['head_sha']
         and run.get('head_branch') == selection['source_branch']
         and run.get('repository_id') == run.get('head_repository_id') == selection['repository_id'],
         'artifact belongs to another source run/repository/head')
    need(row.get('archive_download_url') == 'https://api.github.com/repos/' + source['repository']
         + '/actions/artifacts/' + str(expected['id']) + '/zip', 'foreign artifact URL')


def fetch(root, name, endpoint, *, maximum, exact=None, sha256=None, timeout=600):
    """Native stdout goes to a bounded file; timeout/partial is kept, never retried."""
    shared.original_admission().runner_only()
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


def unpack_evidence(archive_path, destination, selection):
    need(archive_path.stat().st_size == selection['evidence_artifact']['size_in_bytes']
         and digest(archive_path) == selection['evidence_artifact']['digest'][7:], 'small evidence ZIP changed')
    destination.mkdir(mode=0o700, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        rows = regular_archive(archive, max_bytes=MAX_SMALL, max_files=4096)
        for row in rows:
            target = destination / row.filename
            if row.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(row) as reader, target.open('xb') as writer:
                shutil.copyfileobj(reader, writer, 1024**2)
    inventory = shared.read_json(destination / 'EVIDENCE-MANIFEST.json')
    need(inventory.get('run') == selection['source_run'], 'evidence manifest source context differs')
    actual = {p.relative_to(destination).as_posix(): {'sha256': digest(p), 'bytes': p.stat().st_size}
              for p in destination.rglob('*') if p.is_file()
              and p.name not in ('EVIDENCE-MANIFEST.json', 'EVIDENCE-MANIFEST-SHA256.json')}
    expected = {}
    for row in inventory['files']:
        name = str(relative(row['path']))
        need(name not in expected, 'duplicate evidence manifest entry')
        expected[name] = {'sha256': row['sha256'], 'bytes': row['bytes']}
    need(actual == expected, 'full original evidence inventory differs')
    need(shared.read_json(destination / 'EVIDENCE-MANIFEST-SHA256.json') == {
        'sha256': digest(destination / 'EVIDENCE-MANIFEST.json')}, 'evidence manifest SHA receipt differs')
    export = shared.read_json(destination / 'EXPORT.json')
    need(export.get('schema') == 'cmux-local-release-evidence-export-v1'
         and export.get('passed') is True and export.get('run') == selection['source_run']
         and export.get('full_app_in_evidence') is False
         and export.get('original_command_streams_lossless') is True, 'source export contract differs')
    original_names, exported_names = set(), set()
    for row in export['source_members']:
        name, origin = str(relative(row['exported'])), str(relative(row['original']))
        need(name not in exported_names and origin not in original_names and row.get('lossless') is True
             and ((row['encoding'] == 'identity' and name == origin)
                  or (row['encoding'] == 'gzip' and name == origin + '.gz' and origin.endswith(('.stdout', '.stderr'))))
             and actual[name] == {'sha256': row['sha256'], 'bytes': row['bytes']}, 'source lossless stream mapping differs')
        original_names.add(origin); exported_names.add(name)
    total = evidence.verify_exported(destination, export['source_members'])
    need(total == export['evidence_bytes_without_export_manifest'], 'source export total differs')
    return validate_source_package(destination, selection)


def validate_source_package(root, selection):
    for name, expected in selection['members'].items():
        path = root / relative(name)
        need(path.is_file() and not path.is_symlink() and path.stat().st_size == expected['bytes']
             and digest(path) == expected['sha256'], 'retained source evidence changed: ' + name)
    pin, rows = paired.read_pin(root / 'package/PACKAGE-PIN.json',
        selection['members']['package/PACKAGE-PIN.json']['sha256'], root / 'package/PACKAGE-MANIFEST.json')
    build = shared.read_json(root / 'build/RESULT.json')
    command = shared.read_json(root / 'build/BUILD-COMMAND.json')
    source = selection['source_run']
    need(build.get('build_passed') is True and build.get('configuration') == 'Release'
         and build.get('run') == pin.get('run') == source and pin.get('passed') is True
         and pin['source_tree'] == build['source_tree'] == selection['source_tree'] == shared.GREEN_TREE
         and pin['source_upstream'] == selection['source_upstream'] == shared.UPSTREAM,
         'original source build/package did not pass the fixed source chain')
    need(command.get('status') == 'COMPLETED' and command.get('returncode') == 0
         and command['argv'][0] == '/usr/bin/xcodebuild' and command['argv'][-1] == 'build',
         'original build command did not succeed')
    need(pin['build_result_sha256'] == digest(root / 'build/RESULT.json')
         and pin['resolved_settings_sha256'] == build['resolved_settings_sha256']
         == digest(root / 'build/RESOLVED-RELEASE-SETTINGS.json')
         and pin['unsigned_manifest_sha256'] == build['unsigned_manifest_sha256']
         == digest(root / 'build/UNSIGNED-BUNDLE-MANIFEST.json')
         and pin['signature_closure_sha256'] == digest(root / 'package/SIGNATURE-CLOSURE.json')
         and pin['transform_sha256'] == digest(root / 'package/TRANSFORM.json'), 'original package hash chain differs')
    failure = shared.read_json(root / 'FAILURE.json')
    need(failure.get('run') == source and failure.get('stage') == selection['source_failure_stage']
         and failure.get('error') == selection['source_failure_reason'] and failure.get('passed') is False,
         'known original runtime failure was replaced or relabeled')
    binding = shared.read_json(root / 'APP-ARTIFACT-BINDING.json')
    app = shared.read_json(root / 'APP-ARTIFACT-API.json')
    validate_artifact(app, selection['app_artifact'], selection, limit=MAX_APP)
    bound = binding['app_artifact']
    need(binding.get('run') == source and binding.get('status') == 'UPLOAD_BOUND_FINAL_RUN_CONCLUSION_NOT_YET_OBSERVED'
         and bound['id'] == app['id'] and bound['name'] == app['name']
         and 'sha256:' + bound['sha256'] == app['digest'] and bound['bytes'] == app['size_in_bytes']
         and bound['metadata_sha256'] == digest(root / 'APP-ARTIFACT-API.json')
         and binding['package_pin_sha256'] == digest(root / 'package/PACKAGE-PIN.json')
         and binding['package_manifest_sha256'] == digest(root / 'package/PACKAGE-MANIFEST.json')
         and binding['run_attempt_metadata_sha256'] == digest(root / 'RUN-ATTEMPT-API.json')
         and binding['inner_archive'] == pin['archive'] == selection['inner_archive'], 'retained App binding differs')
    need(shared.read_json(root / 'EXPORT.json')['app_artifact_binding'] == binding,
         'source exporter refers to another App')
    return pin, rows


def extract_inner(outer, destination, selection):
    app, inner = selection['app_artifact'], selection['inner_archive']
    need(outer.stat().st_size == app['size_in_bytes'] and digest(outer) == app['digest'][7:],
         'full App outer ZIP differs')
    with zipfile.ZipFile(outer) as archive:
        rows = regular_archive(archive, max_bytes=inner['bytes'], max_files=1)
        need(len(rows) == 1 and rows[0].filename == inner['name'] and not rows[0].is_dir()
             and rows[0].file_size == inner['bytes'], 'exact inner App archive member differs')
        with archive.open(rows[0]) as reader, destination.open('xb') as writer:
            shutil.copyfileobj(reader, writer, 1024**2)
    need(destination.stat().st_size == inner['bytes'] and digest(destination) == inner['sha256'],
         'full App inner ZIP differs')
    package.original_admission().inspect_zip(destination, package.APP_NAME)


def prepare(root, selection, validation):
    downloads = root / 'downloads'; downloads.mkdir(mode=0o700)
    prefix = 'repos/' + selection['source_run']['repository'] + '/actions/'
    identities = {}
    for name, context, original in (('source-run', selection['source_run'], True), ('validation-run', validation, False)):
        path = fetch(downloads, name, prefix + f'runs/{context["run_id"]}/attempts/{context["run_attempt"]}',
                     maximum=16 * 1024**2, timeout=35)
        row = decode(path.read_bytes()); validate_run_api(row, context, selection, source=original)
        identities[name] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    for kind in ('evidence', 'app'):
        expected = selection[kind + '_artifact']
        path = fetch(downloads, kind + '-metadata', prefix + 'artifacts/' + str(expected['id']),
                     maximum=16 * 1024**2, timeout=35)
        validate_artifact(decode(path.read_bytes()), expected, selection,
                          limit=MAX_SMALL if kind == 'evidence' else MAX_APP)
        identities[kind + '-metadata'] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    small = selection['evidence_artifact']
    archive = fetch(downloads, 'evidence-archive', prefix + f'artifacts/{small["id"]}/zip',
        maximum=small['size_in_bytes'], exact=small['size_in_bytes'], sha256=small['digest'][7:])
    original = root / 'original-evidence'
    pin, _ = unpack_evidence(archive, original, selection)
    app = selection['app_artifact']
    outer = fetch(downloads, 'app-archive', prefix + f'artifacts/{app["id"]}/zip',
        maximum=app['size_in_bytes'], exact=app['size_in_bytes'], sha256=app['digest'][7:])
    package_root = root / 'package'; package_root.mkdir(mode=0o700)
    for name in ('PACKAGE-PIN.json', 'PACKAGE-PIN-SHA256.json', 'PACKAGE-MANIFEST.json', 'SIGNATURE-CLOSURE.json', 'TRANSFORM.json'):
        with (package_root / name).open('xb') as writer:
            writer.write((original / 'package' / name).read_bytes())
    inner = package_root / pin['archive']['name']
    extract_inner(outer, inner, selection)
    commands = shared.Commands(package_root / 'commands')
    commands.run(['/usr/bin/ditto', '-x', '-k', str(inner), str(package_root)], timeout=120)
    _, rows = paired.read_pin(package_root / 'PACKAGE-PIN.json',
        selection['members']['package/PACKAGE-PIN.json']['sha256'], package_root / 'PACKAGE-MANIFEST.json')
    need(shared.original_admission().inventory(package_root / package.APP_NAME) == rows,
         'retained complete App readback differs before install')
    receipt = {'schema': 'cmux-retained-source-admission-v1', 'source_run': selection['source_run'],
        'validation_run': validation, 'source_conclusion': 'failure', 'source_build_passed': True,
        'source_package_passed': True, 'source_runtime_passed': False, 'source_failure_preserved': True,
        'source_pin_sha256': PIN_SHA256, 'package_pin_sha256': digest(package_root / 'PACKAGE-PIN.json'),
        'app_artifact': selection['app_artifact'], 'inner_archive': pin['archive'], 'metadata_reads': identities,
        'original_failure_sha256': digest(original / 'FAILURE.json'), 'new_builds': 0,
        'signature_revalidation_pending': True, 'runtime_passed': False, 'business_adoption_ready': False}
    save(root / 'RETAINED-SOURCE-ADMISSION.json', receipt)
    return receipt
