"""Reuse original R5 one-shot App downloader with explicit new producer pins.

No metadata discovery or retry. Keep failed/unknown original transfer bytes.
The original private run_release_actions admission still verifies full native,
package, runtime and App ZIP before any GUI action.
"""
from pathlib import Path
import argparse
import importlib.util
import json
import os
import sys
import time
import supplemental_transport as transport


def prepare(args):
    inputs = args.inputs.resolve(strict=True)
    raw_contract = transport.read(inputs / 'INPUT-CONTRACT.json')
    transport.need(transport.digest(raw_contract) == args.input_contract_sha256, 'original input contract differs')
    contract = transport.decode(raw_contract)
    transport.need(contract.get('status') == 'PINNED_COMPLETED_ORIGINAL_RELEASE', 'PENDING: original Release inputs incomplete')
    adapter = args.adapter.resolve(strict=True)
    sys.path.insert(0, str(adapter))
    # Authenticate the complete source closure before loading its execution entry.
    raw_lock = transport.read(adapter / 'SOURCE-LOCK.json')
    transport.need(transport.digest(raw_lock) == transport.SOURCE_LOCK, 'source lock differs')
    lock = transport.decode(raw_lock)
    roots = {'legacy': args.legacy.resolve(strict=True), 'natural': args.natural.resolve(strict=True), 'adapter': adapter}
    for group, members in lock['files'].items():
        for name, row in members.items():
            raw = transport.read(roots[group] / name)
            transport.need(len(raw) == row['bytes'] and transport.digest(raw) == row['sha256'], 'reviewed source differs')
    import run_release_actions as entry
    entry.dependencies(roots['legacy'], roots['natural'] / 'package', transport.SOURCE_LOCK)
    entry.validate_contract(contract)
    expected = transport.PRODUCER
    run = entry.load(inputs / 'RUN.json', contract['run_api_sha256'])
    jobs = entry.load(inputs / 'JOBS.json', contract['jobs_api_sha256'])
    artifact = entry.load(inputs / 'APP-ARTIFACT.json', contract['artifact_api_sha256'])
    transport.need(transport.same(run.get('id'), expected['run_id'])
        and transport.same(run.get('run_attempt'), expected['run_attempt'])
        and run.get('head_sha') == expected['head_sha'] and run.get('repository', {}).get('full_name') == expected['repository']
        and run.get('status') == 'completed' and run.get('conclusion') == 'success', 'original producer has not succeeded')
    transport.need(type(jobs.get('total_count')) is int and jobs['total_count'] == 2
        and len(jobs.get('jobs', [])) == 2 and {row.get('name') for row in jobs['jobs']} == {'native', 'release'},
        'original producer job closure differs')
    for row in jobs['jobs']:
        transport.need(transport.same(row.get('run_id'), expected['run_id'])
            and transport.same(row.get('run_attempt'), expected['run_attempt']) and row.get('head_sha') == expected['head_sha']
            and row.get('status') == 'completed' and row.get('conclusion') == 'success', 'original job has not succeeded')
    transport.need(transport.same(artifact.get('id'), contract['artifact_id']) and artifact.get('expired') is False
        and artifact.get('name') == f"cmux-release-app-{expected['run_id']}-{expected['run_attempt']}"
        and transport.same(artifact.get('workflow_run', {}).get('id'), expected['run_id'])
        and artifact.get('workflow_run', {}).get('head_sha') == expected['head_sha']
        and transport.same(artifact.get('size_in_bytes'), contract['artifact_zip_bytes'])
        and artifact.get('digest') == 'sha256:' + contract['artifact_zip_sha256'], 'original artifact API binding differs')
    import natural_source_admission as natural
    import release_paired as paired
    pin, _ = paired.read_pin(inputs / 'release-evidence/package/PACKAGE-PIN.json', contract['package_pin_sha256'],
                            inputs / 'release-evidence/package/PACKAGE-MANIFEST.json')
    summary = natural.admit_native_evidence(inputs / 'native-small', expected_run=expected)
    transport.need(transport.same(pin['run'], expected) and transport.same(summary, pin['native_evidence']),
                   'complete native originals do not match the admitted package')
    for name, field in (('RESULT.json', 'release_result_sha256'), ('runtime/RESULT.json', 'release_runtime_sha256'),
                        ('runtime/CLEANUP.json', 'release_cleanup_sha256')):
        result = entry.load(inputs / 'release-evidence' / name, contract[field])
        transport.need(result.get('passed') is True, 'original Release/runtime/cleanup has not passed')
        if name != 'runtime/CLEANUP.json':
            transport.need(transport.same(result.get('run'), expected)
                           and result.get('package_pin_sha256') == contract['package_pin_sha256'],
                           'original Release/runtime source binding differs')
    import stage_fixed_artifact as original
    return inputs, contract, entry, original


def run(args):
    # This first check precedes source imports, credentials and network.
    raw = transport.read(args.inputs / 'INPUT-CONTRACT.json')
    transport.need(transport.digest(raw) == args.input_contract_sha256
                   and transport.decode(raw).get('status') == 'PINNED_COMPLETED_ORIGINAL_RELEASE',
                   'PENDING: completed original input contract required')
    transport.need(sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
                   and os.environ.get('GITHUB_REPOSITORY') == transport.PRODUCER['repository']
                   and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'disposable GitHub Mac required')
    temp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    inputs = args.inputs.resolve(strict=True)
    transport.need(inputs.is_relative_to(temp) and inputs != temp and args.record.is_absolute()
                   and args.record == args.record.resolve(strict=False)
                   and args.record.is_relative_to(temp) and args.record != temp, 'foreign input/transfer root')
    inputs, contract, entry, original = prepare(args)
    target = inputs / 'app-artifact.zip'
    transport.need(not target.exists() and not args.record.exists(), 'original transfer already started; no retry')
    original.capacity(temp, contract['artifact_zip_bytes'] + 8 * 1024**3)
    args.record.mkdir(mode=0o700)
    transport.write(args.record / 'INTENT.json', transport.json_bytes({'producer': contract['producer_run'],
        'artifact_id': contract['artifact_id'], 'bytes': contract['artifact_zip_bytes'],
        'sha256': contract['artifact_zip_sha256'], 'api_requests': 1, 'blob_requests': 1, 'retries': 0}))
    started = time.monotonic()
    result = {'status': 'UNKNOWN', 'passed': False, 'retries': 0}
    try:
        with original.download_response({'pin': {'artifact_id': contract['artifact_id']}}) as response:
            original.copy_bounded(response, target, contract['artifact_zip_bytes'], contract['artifact_zip_sha256'])
        proof = entry.admit(inputs, contract)
        transport.write(args.record / 'ADMISSION.json', transport.json_bytes(proof))
        result.update(status='COMPLETED', passed=True)
    except BaseException as exc:
        result['error_type'] = type(exc).__name__
        raise
    finally:
        result['elapsed_seconds'] = time.monotonic() - started
        result['retained_partial_bytes'] = target.stat().st_size if target.exists() else None
        transport.write(args.record / 'RESULT.json', transport.json_bytes(result))


def main():
    parser = argparse.ArgumentParser()
    for name in ('inputs', 'adapter', 'legacy', 'natural', 'record'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--input-contract-sha256', required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
