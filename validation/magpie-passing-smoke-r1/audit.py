"""Re-read original case files, reproduce the R8 judgment, and bind both builds."""
import argparse
import json
from pathlib import Path
import fixture
from common import FLOOR, pin, require, save, verify_package
from smoke import CASE_NAMES, PROBE_BASE, PROBE_CODEX, probe_result


def audit(root, package_pin, smoke, candidate, baseline, client):
    verify_package(root, package_pin)
    acceptance = json.loads((smoke / 'ACCEPTANCE.json').read_text())
    require(acceptance['status'] == 'PASS' and acceptance['package_pin'] == package_pin and
            acceptance['cleanup_complete'] is True and acceptance['HOME_unchanged'] is True and
            acceptance['CODEX_HOME_unchanged'] is True and acceptance['old_collector_reused'] is False,
            'aggregate attempt did not complete cleanly')
    require([row['name'] for row in acceptance['cases']] == list(CASE_NAMES), 'four-case membership mismatch')
    spec = json.loads((root / 'CLIENT-ASSET.json').read_text())
    cp = json.loads((client / 'CLIENT.json').read_text())
    require(cp['status'] == 'PASS' and cp['spec'] == spec and cp['package_pin'] == package_pin,
            'official client verification receipt differs')
    require(cp['archive'] == {'sha256': spec['archive_sha256'], 'bytes': spec['archive_bytes']}, 'archive pin differs')
    require(cp['binary'] == {'sha256': spec['binary_sha256'], 'bytes': spec['binary_bytes']}, 'client binary pin differs')
    require(pin(client / spec['binary_name']) == cp['binary'], 'client bytes changed after smoke')
    builds = {}
    for kind, folder in [('candidate', candidate), ('baseline', baseline)]:
        row = json.loads((folder / 'BUILD.json').read_text())
        require(row['status'] == 'PASS' and row['source_after_match'] is True and row['variant'] == kind and
                row['package_pin'] == package_pin and row['same_historical_binary_claimed'] is False,
                'build receipt differs')
        require(row['source_manifest'] == pin(root / (kind + '-source.json')), 'wrong build source')
        require(pin(folder / row['binary_name']) == row['binary'], 'Magpie bytes changed after smoke')
        source = json.loads((root / (kind + '-source.json')).read_text())
        env = row.get('environment', {})
        require(env.get('go') == source['toolchain'] and env.get('target') == 'darwin/arm64' and
                env.get('original_sdk') == source['original_sdk'] and
                all(isinstance(env.get(k), str) and env[k] for k in ['sdk', 'clang']),
                'build target/toolchain/SDK metadata differs')
        for step in ['modules', 'compile']:
            run = json.loads((folder / step / 'RESULT.json').read_text())
            require(run['status'] == 'PASS' and run['returncode'] == 0 and run['child_reaped'] is True,
                    'build child did not complete')
            require(len(run.get('samples', [])) >= 2 and
                    all(type(s.get('free_bytes')) is int and s['free_bytes'] >= FLOOR for s in run['samples']),
                    'build capacity monitoring failed')
            for name, value in run['logs'].items():
                require(pin(folder / step / name) == value, 'build log changed')
        builds[kind] = row
    require(builds['candidate']['binary'] != builds['baseline']['binary'], 'candidate and baseline unexpectedly identical')
    cases = []
    for name, outer in zip(CASE_NAMES, acceptance['cases']):
        folder = smoke / name
        require(pin(folder / 'RESULT.json') == outer['result'], 'original case result changed')
        row = json.loads((folder / 'RESULT.json').read_text())
        require(row['status'] == 'PASS' and row['name'] == name, 'case did not pass')
        for key in ['children_reaped', 'handlers_finished', 'artifact_unchanged', 'codex_unchanged',
                    'installation_unchanged', 'private_removed']:
            require(row.get(key) is True, 'case cleanup/identity guard failed: ' + key)
        require(len(row['owned_children']) == 2, 'wrong owned process count')
        samples = row['samples']
        require(len(samples) >= 2 and all(type(s['free_bytes']) is int and s['free_bytes'] >= FLOOR for s in samples), 'capacity monitoring failed')
        for file, expected in row['files'].items():
            rel = Path(file)
            require(rel.name == file and not rel.is_absolute(), 'unsafe case log reference')
            require(pin(folder / file) == expected, 'case file changed: ' + file)
        require(probe_result(0, (folder / 'gateway-probe.stdout').read_text(), False), 'gateway probe failed')
        require(probe_result(0, (folder / 'codex-probe.stdout').read_text(), True), 'client probe failed')
        success = name != 'baseline-comments'
        expected_binary = builds['candidate' if success else 'baseline']['binary']
        require(row['artifact'] == expected_binary and row['codex'] == cp['binary'], 'wrong binary category')
        result = fixture.judge(row['owned_children'][-1]['returncode'], (folder / 'codex.jsonl').read_text(),
                               (folder / 'codex.stderr').read_text(), row['requests'], success)
        events = [json.loads(line) for line in (folder / 'codex.jsonl').read_text().splitlines() if line.strip()]
        if success:
            require(not any(e.get('type') in ['turn.failed', 'error'] for e in events), 'candidate emitted an error before success')
            require(events[-1].get('type') == 'turn.completed', 'unexpected event after native completion')
        require(result == row['validation'], 'native result differs from original R8 judgment')
        cases.append({'name': name, 'validation': result, 'result': pin(folder / 'RESULT.json')})
    return {'status': 'PASS', 'package_pin': package_pin, 'cases': cases,
            'candidate_build': pin(candidate / 'BUILD.json'), 'baseline_build': pin(baseline / 'BUILD.json'),
            'official_client_receipt': pin(client / 'CLIENT.json'), 'new_artifact_identities': acceptance['inputs'],
            'original_installed_magpie_accepted': False, 'old_collector_reused': False,
            'scope': 'New GHA Magpie candidate/baseline with exact original official Codex bytes; passing heartbeat only'}


def main():
    p = argparse.ArgumentParser()
    for name in ['smoke', 'candidate', 'baseline', 'client']:
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--package-pin', required=True)
    a = p.parse_args()
    try:
        result = audit(Path(__file__).resolve().parent, a.package_pin, a.smoke, a.candidate, a.baseline, a.client)
    except Exception as error:
        result = {'status': 'FAIL', 'error': type(error).__name__ + ': ' + str(error), 'package_pin': a.package_pin}
    a.smoke.mkdir(parents=True, exist_ok=True)
    save(a.smoke / 'AUDIT.json', result)
    require(result['status'] == 'PASS', 'native acceptance audit failed')


if __name__ == '__main__':
    main()
