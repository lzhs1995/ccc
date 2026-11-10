"""Revalidate the exact retained Release on one fresh hosted Mac; never compile."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import pwd
import sys

import ci_release as original_ci
import source_contract as shared
import retained_release as retained
import release_package as package
import release_smoke as smoke

need, save = shared.need, shared.save


def context():
    selected = retained.pins()
    current = shared.run_identity()
    retained.validate_context(current, selected, branch=os.environ.get('GITHUB_REF'),
                              event=os.environ.get('GITHUB_EVENT_NAME'))
    need(os.environ.get('GITHUB_REF') == 'refs/heads/' + selected['validation_branch'], 'validation ref missing')
    need(os.environ.get('GITHUB_WORKFLOW_REF') == current['repository'] + '/' + selected['validation_workflow']
         + '@refs/heads/' + selected['validation_branch'], 'validation workflow ref differs')
    return selected, current


def admitted_runtime(root, selected, current):
    admission = shared.read_json(root / 'RETAINED-SOURCE-ADMISSION.json')
    need(admission['source_run'] == selected['source_run'] and admission['validation_run'] == current
         and admission['source_pin_sha256'] == retained.PIN_SHA256
         and admission['source_conclusion'] == 'failure'
         and admission['source_build_passed'] is True and admission['source_package_passed'] is True
         and admission['source_runtime_passed'] is False, 'foreign or relabeled source admission')
    retained.validate_source_package(root / 'original-evidence', selected)
    return smoke.runtime(root / 'runtime', root / 'package', root / 'old-cli/cmux',
        source_run=selected['source_run'],
        package_pin_sha256=selected['members']['package/PACKAGE-PIN.json']['sha256'])


def in_aqua(root, phase, selected, current):
    commands = shared.Commands(root / (phase + '-aqua-commands'))
    manager = commands.text(['/bin/launchctl', 'managername'], timeout=5).strip()
    if manager == 'Aqua':
        if phase == 'preflight':
            save(root / 'RUNTIME-PREFLIGHT.json', smoke.runtime_preflight())
        else:
            admitted_runtime(root, selected, current)
        return
    uid = Path('/dev/console').stat().st_uid
    need(uid == os.geteuid() and uid > 0, 'real hosted console owner required')
    environment = original_ci.aqua_environment()
    for key in ('GITHUB_REF', 'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME'):
        environment[key] = os.environ[key]
    argv = ['/usr/bin/sudo', '-n', '/bin/launchctl', 'asuser', str(uid), '/usr/bin/sudo', '-n', '-u',
            pwd.getpwuid(uid).pw_name, '/usr/bin/env', '-i',
            *[key + '=' + value for key, value in environment.items()], sys.executable, '-BE',
            str(Path(__file__).resolve()), '--output', str(root), '--' + phase + '-only']
    commands.run(argv, timeout=60 if phase == 'preflight' else smoke.RUNTIME_SECONDS + 120)


def run(root, selected, current):
    phase = 'source-contract'
    try:
        closure = retained.verify_source_closure()
        shared.check_contract()
        save(root / 'RUN-CONTEXT.json', {'schema': 'cmux-retained-runtime-context-v1',
            'source_run': selected['source_run'], 'validation_run': current, 'run': current,
            'retained_selection_sha256': retained.PIN_SHA256, 'new_builds': 0,
            'old_tests_rerun': 0, 'business_adoption_ready': False, **closure})
        phase = 'aqua-preflight'; in_aqua(root, 'preflight', selected, current)
        phase = 'retained-artifact-admission'; retained.prepare(root, selected, current)
        phase = 'old-cli-admission'; package.download_prior_cli(root / 'old-cli')
        phase = 'retained-new-package-runtime'; in_aqua(root, 'runtime', selected, current)
        runtime = shared.read_json(root / 'runtime/RESULT.json')
        cleanup = shared.read_json(root / 'runtime/CLEANUP.json')
        pin_sha = selected['members']['package/PACKAGE-PIN.json']['sha256']
        need(runtime.get('passed') is True and cleanup.get('passed') is True
             and runtime.get('run') == runtime.get('validation_run') == current
             and runtime.get('source_run') == selected['source_run']
             and runtime.get('package_pin_sha256') == pin_sha
             and runtime.get('gui_generations') == 2 and runtime.get('retained_app_revalidation') is True,
             'complete new runtime result does not bind both attempts and retained package')
        result = {'schema': 'cmux-retained-release-validation-result-v1', 'passed': True,
            'source_run': selected['source_run'], 'validation_run': current, 'run': current,
            'source_run_conclusion': 'failure', 'source_build_passed': True, 'source_package_passed': True,
            'original_runtime_failure_preserved': True, 'new_builds': 0,
            'retained_selection_sha256': retained.PIN_SHA256, 'package_pin_sha256': pin_sha,
            'retained_source_admission_sha256': shared.digest(root / 'RETAINED-SOURCE-ADMISSION.json'),
            'runtime_result_sha256': shared.digest(root / 'runtime/RESULT.json'),
            'cleanup_result_sha256': shared.digest(root / 'runtime/CLEANUP.json'),
            'original_failure_sha256': shared.digest(root / 'original-evidence/FAILURE.json'),
            'app_artifact': selected['app_artifact'], 'inner_archive': selected['inner_archive'],
            'gui_generations': 2, 'business_adoption_ready': False, 'local_user_install': False,
            'final_external_validation_run_check_required': True, **closure}
        save(root / 'RESULT.json', result)
        return result
    except BaseException as error:
        save(root / 'FAILURE.json', smoke.error_record(error) | {'phase': phase, 'passed': False,
            'source_run': selected['source_run'], 'validation_run': current, 'run': current,
            'new_builds': 0, 'source_run_conclusion': 'failure', 'business_adoption_ready': False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight-only', action='store_true')
    modes.add_argument('--runtime-only', action='store_true')
    args = parser.parse_args()
    shared.original_admission().runner_only()
    selected, current = context()
    resumed = args.preflight_only or args.runtime_only
    root = original_ci.contained_attempt(args.output, fresh=not resumed)
    if resumed:
        saved = shared.read_json(root / 'RUN-CONTEXT.json')
        need(saved['validation_run'] == current and saved['source_run'] == selected['source_run']
             and saved['retained_selection_sha256'] == retained.PIN_SHA256, 'foreign Aqua continuation')
        retained.verify_source_closure()
        if args.preflight_only:
            save(root / 'RUNTIME-PREFLIGHT.json', smoke.runtime_preflight())
        else:
            admitted_runtime(root, selected, current)
    else:
        run(root, selected, current)


if __name__ == '__main__':
    main()
