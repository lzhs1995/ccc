"""Authenticate the original passing tests before a separate release build."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
import zipfile

REPOSITORY = 'lzhs1995/ccc'
RUN = 41559398095
SOURCE_COMMIT = '9a59995c7943126b93ccced8c217fc118c67b5ed'
INTEGRATION_PIN = '7d8017754da19290801c5f956a7700158987e2e9865db5eff1d802386ba02254'
ARTIFACTS = {
    'full': (13198330411, 'codex-tui-test-evidence-v187', 515934,
             'be5f5d999bc39891df11536b56e497b3d6a5016d8edbde97bd3b55572db5a11c'),
    'release': (13197767432, 'codex-tui-release-evidence-v187', 94330,
                '535ac44375f0392a3ca2250388f77eddc81517fb675e25509f0d596b63859de4'),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def github(path):
    result = subprocess.run(['gh', 'api', path], stdin=subprocess.DEVNULL,
                            capture_output=True, check=True, timeout=60)
    return result.stdout


def authenticate_archives(output):
    prefix = 'repos/' + REPOSITORY + '/actions/'
    run = json.loads(github(prefix + 'runs/' + str(RUN)))
    if (run['head_sha'] != SOURCE_COMMIT or run['head_branch'] != 'ccc/herdr-native-foreground-v187'
            or run['status'] != 'completed' or run['run_attempt'] != 1):
        raise ValueError('Original test run identity changed')
    jobs = json.loads(github(prefix + 'runs/' + str(RUN) + '/jobs?per_page=100'))['jobs']
    publishers = [job for job in jobs if job['name'].startswith('publisher (')]
    if len(publishers) != 2 or any(job['conclusion'] != 'success' for job in publishers):
        raise ValueError('Original cross-platform publisher tests did not succeed')
    native = [job for job in jobs if job['id'] == 125666224968 and job['name'] == 'tui-macos']
    if len(native) != 1:
        raise ValueError('Original native test job is missing')
    required = {'Actual App lifecycle and complete TUI library tests',
                'Require the candidate suite',
                'Verify the actual foreground lifecycle at the release version'}
    passed = {step['name'] for step in native[0]['steps'] if step['conclusion'] == 'success'}
    if not required <= passed:
        raise ValueError('Original native test steps did not succeed')
    records = {}
    for kind, (identifier, name, size, pin) in ARTIFACTS.items():
        metadata = json.loads(github(prefix + 'artifacts/' + str(identifier)))
        owner = metadata.get('workflow_run', {})
        if (metadata['id'] != identifier or metadata['name'] != name or metadata['expired']
                or metadata['size_in_bytes'] != size or metadata['digest'] != 'sha256:' + pin
                or owner.get('id') != RUN or owner.get('head_sha') != SOURCE_COMMIT
                or owner.get('repository_id') != 1356084046):
            raise ValueError('Original artifact identity changed')
        raw = github(prefix + 'artifacts/' + str(identifier) + '/zip')
        if len(raw) != size or sha(raw) != pin:
            raise ValueError('Original artifact archive bytes changed')
        destination = output / kind
        destination.mkdir(mode=0o700)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (archive.testzip() is not None or len(entries) > 16
                    or sum(entry.file_size for entry in entries) > 16 * 1024 * 1024
                    or len({entry.filename for entry in entries}) != len(entries)
                    or any(entry.is_dir() or Path(entry.filename).name != entry.filename
                           or entry.filename in {'.', '..'} for entry in entries)):
                raise ValueError('Original artifact archive layout is unsafe')
            files = {}
            for entry in entries:
                data = archive.read(entry)
                (destination / entry.filename).write_bytes(data)
                files[entry.filename] = sha(data)
        records[kind] = dict(id=identifier, name=name, archive_sha256=pin, files=files,
                             workflow_run=owner)
    return run, records


def passed_cases(path, expected):
    root = ET.parse(path).getroot()
    cases = root.findall('.//testcase')
    if (root.get('tests') != str(expected) or root.get('failures') != '0'
            or root.get('errors') != '0' or len(cases) != expected
            or any(int(node.get(key, '0')) != 0
                   for node in [root, *root.findall('.//testsuite')]
                   for key in ('failures', 'errors', 'skipped', 'disabled'))
            or any(case.find('failure') is not None or case.find('error') is not None
                   or case.find('skipped') is not None for case in cases)):
        raise ValueError('Original test results are incomplete or did not pass')
    names = [case.get('name') for case in cases]
    if len(set(names)) != len(names):
        raise ValueError('Original test cases are duplicated')
    return names


def verify_results(ccc, codex, output, run, artifacts):
    bundle = ccc / 'integrations/codex-foreground'
    raw = (bundle / 'manifest.json').read_bytes()
    if sha(raw) != INTEGRATION_PIN:
        raise ValueError('Build integration differs from the passing test source')
    integration = json.loads(raw)
    if sha((bundle / 'integration.patch').read_bytes()) != integration['patch']['sha256']:
        raise ValueError('Build patch differs from the passing test source')
    for name, pin in integration['native_files'].items():
        if sha((ccc / name).read_bytes()) != pin or sha((codex / name).read_bytes()) != pin:
            raise ValueError('Build native publisher differs from the passing test source')
    for name, pins in integration['patched_files'].items():
        if sha((codex / name).read_bytes()) != pins['after_sha256']:
            raise ValueError('Applied build source differs from the passing test source: ' + name)
    full = output / 'full'
    release = output / 'release'
    expected = {
        full / 'candidate-release-before-test.lock': integration['release_lock']['candidate_sha256'],
        full / 'test-candidate.lock': integration['test_version_setup']['candidate_lock_sha256'],
        full / 'candidate-workspace-release.toml': integration['test_version_setup']['workspace_release_manifest_sha256'],
        release / 'release-candidate.lock': integration['release_lock']['candidate_sha256'],
        codex / 'codex-rs/Cargo.toml': integration['test_version_setup']['workspace_release_manifest_sha256'],
        codex / 'codex-rs/Cargo.lock': integration['release_lock']['candidate_sha256'],
    }
    if any(sha(path.read_bytes()) != pin for path, pin in expected.items()):
        raise ValueError('Original test graph or build release graph changed')
    full_names = passed_cases(full / 'tui-candidate-tests.xml', 5837)
    release_names = passed_cases(release / 'tui-release-foreground-tests.xml', 13)
    actual = {name for name in full_names if name.startswith('app::tests::foreground_observation::')}
    if actual != set(release_names) or len(actual) != integration['expected_app_foreground_tests']:
        raise ValueError('Release lifecycle coverage differs from the full passing suite')
    return dict(schema='ccc-native-foreground-reused-test-evidence-v187',
                test_source_commit=SOURCE_COMMIT, test_run_id=RUN, test_run_attempt=1,
                test_run_conclusion=run['conclusion'], cross_platform_publisher_tests_passed=True,
                full_tui_library_tests_passed=True, full_tui_library_tests=5837,
                full_tui_library_test_version=integration['test_version_setup']['version'],
                release_foreground_tests_passed=True, release_foreground_tests=13,
                integration_manifest_sha256=sha(raw), artifacts=artifacts,
                native_test_step_success_independent_of_cancelled_release_build=True,
                test_runner_reexecuted=False, runtime_installed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ccc', type=Path, required=True)
    parser.add_argument('--codex', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    run, artifacts = authenticate_archives(args.output)
    proof = verify_results(args.ccc, args.codex, args.output, run, artifacts)
    (args.output / 'VERIFIED-TEST-EVIDENCE.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps({key: value for key, value in proof.items() if key != 'artifacts'}))


if __name__ == '__main__':
    main()
