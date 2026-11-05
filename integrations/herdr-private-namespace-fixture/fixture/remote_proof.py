"""Verify actual GitHub Herdr artifacts; never synthesize local build/test receipts."""
import argparse
import base64
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import xml.etree.ElementTree as ET
import zipfile

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parent / 'github-build-preparation'
FREEZE_SHA = 'c977160d40cf0f2ee829cbdd710bfaa8a8eb86c0273ff088610133c69bbd0527'
REPOSITORY = 'lzhs1995/ccc'
WORKFLOW = '.github/workflows/herdr-production-namespace.yml'
BRANCH = 'codex/herdr-v3-production-namespace-20261105'
BUILD_STEP = 'Build exact candidate and execute same-cfg complete unit suite'


def need(value, reason):
    if not value:
        raise ValueError(reason)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def gh_json(endpoint):
    result = subprocess.run(['gh', 'api', endpoint], capture_output=True,
                            timeout=60, check=True)
    return json.loads(result.stdout)


def validate_run(run, jobs, contract):
    need(run['id'] == contract['run_id'] and run['run_attempt'] == contract['run_attempt']
         and run['head_sha'] == contract['head_sha'] and run['head_branch'] == BRANCH
         and run['repository']['full_name'] == REPOSITORY
         and run['head_repository']['full_name'] == REPOSITORY
         and run['path'] == WORKFLOW and run['status'] == 'completed'
         and run['conclusion'] == 'success' and run['event'] in ('push', 'workflow_dispatch'),
         'GitHub run is not the reviewed successful original head/attempt/workflow')
    selected = [j for j in jobs['jobs'] if j['name'] == 'candidate']
    need(len(selected) == 1 and selected[0]['conclusion'] == 'success'
         and selected[0]['head_sha'] == contract['head_sha'], 'candidate job not successful')
    steps = [s for s in selected[0]['steps'] if s['name'] == BUILD_STEP]
    need(len(steps) == 1 and steps[0]['conclusion'] == 'success', 'real build/test step not successful')


def validate_zip(path, artifact, run, expected_name):
    need(artifact['name'] == expected_name and artifact['expired'] is False
         and artifact['size_in_bytes'] == Path(path).stat().st_size
         and artifact['workflow_run']['id'] == run['id']
         and artifact['workflow_run']['head_sha'] == run['head_sha']
         and artifact['workflow_run']['head_branch'] == BRANCH
         and artifact['digest'] == 'sha256:' + sha(path), 'GitHub artifact digest/origin mismatch')
    entries = {}
    with zipfile.ZipFile(path) as archive:
        total = 0
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            name = entry.filename
            # upload-artifact roots are flat; no symlink, path traversal or duplicate.
            mode = entry.external_attr >> 16
            need(name == Path(name).name and name not in entries and not stat.S_ISLNK(mode),
                 'unsafe or duplicate artifact ZIP member')
            total += entry.file_size
            need(entry.file_size <= 1024**3 and total <= 2 * 1024**3, 'artifact ZIP exceeds bound')
            entries[name] = archive.read(entry)
    return entries


def parse_json(files, name):
    need(name in files, 'artifact is missing ' + name)
    return json.loads(files[name])


def validate_compile(files, label, pinned_sha, size=None):
    value = parse_json(files, label + '-compile.json')
    log = re.sub(r'\x1b\[[0-9;]*m', '', files[label + '-build.log'].decode())
    unit = label == 'unit'
    expected_command = ['cargo', '+1.99.0', 'rustc', '--locked', '--offline', '-p', 'herdr',
                        '--bin', 'herdr', '--message-format=json', '--verbose',
                        *(['--profile', 'test'] if unit else []), '--', '-C', 'debug-assertions=off']
    need(value['command'] == expected_command and value['sha256'] == pinned_sha
         and value['root_crate_debug_assertions'] is False and value['nonoptimized'] is True,
         label + ' compile contract differs')
    if size is not None:
        need(value['bytes'] == size, 'server byte count differs')
    commands = [line for line in log.splitlines() if 'Running ' in line and '--crate-name herdr ' in line]
    need(len(commands) == 1 and commands[0] == value['actual_rustc_command']
         and '-C debug-assertions=off' in commands[0]
         and ((' --test ' in commands[0]) is unit)
         and not re.search(r'(?:-C\s+)?debug-assertions(?:=|\s+)(?:on|yes|true)', commands[0])
         and not re.search(r'opt-level=(?:[123sz])', commands[0]),
         label + ' actual root rustc command is not production namespace/nonoptimized')
    messages = []
    for line in log.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            messages.append(item)
    artifacts = [x for x in messages if x.get('reason') == 'compiler-artifact'
                 and x.get('target', {}).get('name') == 'herdr' and x.get('executable')]
    need(len(artifacts) == 1 and artifacts[0] == value['artifact']
         and artifacts[0]['profile']['test'] is unit
         and artifacts[0]['target']['kind'] == ['bin'], label + ' actual compiler artifact differs')
    need(any(x.get('reason') == 'build-finished' and x.get('success') is True for x in messages),
         label + ' compiler did not finish successfully')
    return value


def validate_cases(files, expected, unit):
    listing = parse_json(files, 'LIST.json')
    suites = listing['rust-suites']
    need(list(suites) == ['herdr::bin/herdr'], 'unexpected unit suite')
    suite = suites['herdr::bin/herdr']
    need(suite['binary-path'] == unit['path'], 'listed unit binary differs from actual compile')
    actual = sorted(n for n, x in suite['testcases'].items() if x['ignored'] is False)
    ignored = sorted(n for n, x in suite['testcases'].items() if x['ignored'] is True)
    need(actual == expected['nonignored'] and ignored == expected['ignored']
         and len(actual) == 3656 and len(ignored) == 11, 'exact cfg unit discovery differs')
    root = ET.fromstring(files['unit.xml'])
    cases = root.findall('.//testcase')
    need(not any(c.find(tag) is not None for c in cases for tag in ('error', 'failure', 'skipped'))
         and all(c.get('classname') == 'herdr::bin/herdr' for c in cases)
         and Counter(c.get('name') for c in cases) == Counter(actual), 'exact JUnit successes differ')
    result = parse_json(files, 'UNIT-RESULT.json')
    need(result['passed'] is True and result['successful_cases'] == 3656
         and result['ignored_not_run'] == 11 and result['unit_binary_sha256'] == unit['sha256']
         and result['junit_sha256'] == hashlib.sha256(files['unit.xml']).hexdigest()
         and result['cargo_build_invoked_by_nextest'] is False, 'unit result not tied to JUnit/binary')
    meta = parse_json(files, 'binaries-metadata.json')['rust-binaries']
    need(list(meta) == ['herdr::bin/herdr'] and meta['herdr::bin/herdr']['binary-path'] == unit['path'],
         'nextest original reuse metadata differs')
    provenance = parse_json(files, 'METADATA-PROVENANCE.json')
    for name, key in [('unit-compile.json', 'unit_compile_sha256'), ('cargo-metadata.json', 'cargo_metadata_sha256'),
                      ('binaries-metadata.json', 'binary_metadata_sha256')]:
        need(hashlib.sha256(files[name]).hexdigest() == provenance[key], 'metadata provenance differs')
    return result


def validate_source_checkpoints(files, expected):
    # Preserve the full source gate: never exclude build outputs from inventory.
    for phase in ('after-fetch', 'after-server', 'after-unit', 'after-list', 'after-tests'):
        actual = parse_json(files, 'SOURCE-' + phase + '.json')
        need(actual == expected, 'complete frozen source differs at ' + phase)
        difference = parse_json(files, 'SOURCE-DIFF-' + phase + '.json')
        need(difference == {'phase': phase, 'expected_sha256': expected['sha256'],
                            'actual_sha256': expected['sha256'],
                            'added': [], 'removed': [], 'changed': []},
             'source checkpoint diff is not exact and empty at ' + phase)


def validate_zig_mirror_evidence(files, expected_source, server, contract):
    prefix = 'vendor/libghostty-vt/'
    rows = [{**row, 'path': row['path'][len(prefix):]} for row in expected_source['files']
            if row['path'].startswith(prefix)]
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    expected = {'files': rows, 'sha256': hashlib.sha256(canonical(rows)).hexdigest()}
    inputs = parse_json(files, 'ZIG-MIRROR-INPUTS.json')
    need(inputs == expected and len(rows) == 1339, 'exact frozen Zig mirror inputs differ')
    spec = parse_json(files, 'ZIG-MIRROR-SPEC.json')
    prep = parse_json(files, 'ZIG-MIRROR-PREPARATION.json')
    spec_sha = hashlib.sha256(files['ZIG-MIRROR-SPEC.json']).hexdigest()
    input_sha = hashlib.sha256(files['ZIG-MIRROR-INPUTS.json']).hexdigest()
    output = Path(spec['inputs']).parent
    original = Path(server['artifact']['manifest_path']).parent / 'vendor/libghostty-vt'
    need(spec['schema'] == 'herdr-zig-output-mirror-v1' and spec['ci_sha256'] == contract['ci_sha256']
         and Path(spec['original_vendor']) == original
         and Path(spec['mirror']) == output / 'zig-vendor-inputs'
         and Path(spec['target_dir']) == output / 'target'
         and Path(spec['wrapper']) == output / 'zig-output-wrapper'
         and Path(spec['invocations']) == output / 'zig-invocations.log'
         and Path(spec['inputs']) == output / 'ZIG-MIRROR-INPUTS.json'
         and spec['inputs_sha256'] == input_sha
         and Path(spec['real_zig']).is_relative_to(output / 'zig')
         and spec['real_zig_sha256'] == 'e6cd688d25664983833aae272f501d4bceeae304875b8f1741209d15fd13a4ec',
         'Zig real binary or isolated mirror path/CI binding differs')
    need(spec['build_tail'][:4] == ['-Demit-lib-vt', '-Doptimize=ReleaseFast', '-Dsimd=true', '-Dtarget=aarch64-macos']
         and len(spec['build_tail']) == 6 and spec['build_tail'][4] == '-Dversion-string=1.3.2-HEAD-+44f2a44df'
         and spec['build_tail'][5] == '-Demit-xcframework=false', 'Zig build semantics changed')
    source = prep['wrapper_source']
    interpreter = source.splitlines()[0]
    need(interpreter.startswith('#!/') and '\n' not in interpreter, 'Zig wrapper interpreter is not absolute')
    exact_wrapper = (interpreter + "\nimport hashlib, pathlib, runpy, sys\n"
        + "ci_path = pathlib.Path(" + repr(spec['ci_path']) + ")\n"
        + "assert hashlib.sha256(ci_path.read_bytes()).hexdigest() == " + repr(spec['ci_sha256']) + "\n"
        + "runpy.run_path(str(ci_path))['zig_wrapper_main'](" + repr(str(output / 'ZIG-MIRROR-SPEC.json'))
        + ", " + repr(spec_sha) + ", sys.argv[1:])\n")
    need(source == exact_wrapper and prep['wrapper_sha256'] == hashlib.sha256(source.encode()).hexdigest()
         and prep['spec_sha256'] == spec_sha and prep['inputs_sha256'] == input_sha
         and prep['frozen_input_count'] == 1339 and prep['real_zig_sha256'] == spec['real_zig_sha256']
         and prep['system_package_mode'] is False, 'actual Zig wrapper preparation differs')
    frozen = {row['path']: row for row in rows}
    for phase in ('after-server', 'after-unit', 'after-tests'):
        actual = parse_json(files, 'ZIG-MIRROR-' + phase + '.json')
        need(actual['sha256'] == hashlib.sha256(canonical(actual['files'])).hexdigest(), 'Zig mirror digest differs')
        observed = {row['path']: row for row in actual['files']}
        need(len(observed) == len(actual['files']) and all(observed.get(k) == v for k, v in frozen.items()),
             'frozen Zig mirror input changed or disappeared at ' + phase)
        extras = [row for row in actual['files'] if row['path'] not in frozen]
        need(all(row['path'].startswith('zig-pkg/') for row in extras), 'unexpected mutable mirror output')
        packages = parse_json(files, 'ZIG-PACKAGES-' + phase + '.json')
        need(packages == {'files': extras, 'sha256': hashlib.sha256(canonical(extras)).hexdigest()},
             'complete external package inventory differs')
    events = [json.loads(line) for line in files['zig-invocations.log'].splitlines() if line.strip()]
    builds = 0
    for event in events:
        argv = event['argv']
        need(event['event'] == 'exec_intent' and event['spec_sha256'] == spec_sha
             and event['wrapper_sha256'] == prep['wrapper_sha256']
             and event['real_zig_sha256'] == spec['real_zig_sha256'] and argv[0] == spec['real_zig'],
             'Zig invocation evidence not tied to original wrapper')
        if argv[1:] == ['version']:
            need(event['original_cwd'] == event['execution_cwd'], 'version unexpectedly changed cwd')
            continue
        need(len(argv) == 10 and argv[1:3] == ['build', '--prefix'] and argv[4:] == spec['build_tail']
             and Path(argv[3]).is_absolute() and Path(argv[3]).is_relative_to(output / 'target')
             and Path(argv[3]).name == 'zig-out' and Path(argv[3]).parent.name == 'out'
             and event['original_cwd'] == spec['original_vendor'] and event['execution_cwd'] == spec['mirror'],
             'actual Zig build changed args or escaped mirror')
        builds += 1
    need(builds >= 1, 'no original vendor Zig build invocation was recorded')


def validate_artifact_contents(evidence, package, run, contract):
    need(sha(BUNDLE / 'freeze.json') == FREEZE_SHA == contract['freeze_sha256'], 'reviewed freeze differs')
    freeze = read(BUNDLE / 'freeze.json')
    for name, key in [('CANDIDATE-SOURCE.json', 'candidate_manifest_sha256'),
                      ('EXPECTED-CASES.json', 'expected_cases_sha256')]:
        need(sha(BUNDLE / name) == freeze[key], 'reviewed proof member changed')
    need(package['freeze.json'] == (BUNDLE / 'freeze.json').read_bytes(), 'remote freeze bytes differ')
    need(package['EXPECTED-CASES.json'] == (BUNDLE / 'EXPECTED-CASES.json').read_bytes(), 'remote exact cases differ')
    need(not any(k == 'FAILURE.json' for k in evidence), 'remote compilation recorded failure')
    manifest = parse_json(package, 'BUILD-MANIFEST.json')
    result = parse_json(evidence, 'RESULT.json')
    source = parse_json(evidence, 'SOURCE.json')
    expected_source = read(BUNDLE / 'CANDIDATE-SOURCE.json')
    need(source == expected_source and source['sha256'] == manifest['full_source_sha256'], 'actual complete source differs')
    validate_source_checkpoints(evidence, expected_source)
    need(manifest['schema'] == 'herdr-production-namespace-artifact-v1'
         and manifest['freeze_sha256'] == FREEZE_SHA and manifest['patch_sha256'] == freeze['patch_sha256']
         and manifest['cargo_lock_sha256'] == freeze['cargo_lock_sha256']
         and manifest['actions_run_id'] == str(run['id'])
         and manifest['actions_run_attempt'] == str(run['run_attempt'])
         and manifest['commit'] == run['head_sha'] and manifest['ci_sha256'] == contract['ci_sha256']
         and manifest['namespace'] == 'herdr' and manifest['root_crate_debug_assertions'] is False
         and manifest['nonoptimized'] is True and manifest['official_release'] is False
         and manifest['production_installed'] is False and manifest['actual_rust'] == '1.99.0'
         and manifest['rustc'].startswith('rustc 1.99.0 ')
         and manifest['nextest'].startswith('cargo-nextest 0.9.146 ') and manifest['zig'] == '0.16.0',
         'build manifest identity or namespace differs')
    need(result['passed'] is True
         and result['build_manifest_sha256'] == hashlib.sha256(package['BUILD-MANIFEST.json']).hexdigest()
         and result['archive_sha256'] == hashlib.sha256(package['herdr-candidate.tar.gz']).hexdigest(),
         'actual build result is not tied to candidate package')
    server = validate_compile(evidence, 'server', manifest['server_sha256'], manifest['server_bytes'])
    validate_zig_mirror_evidence(evidence, expected_source, server, contract)
    unit = validate_compile(evidence, 'unit', manifest['unit_sha256'])
    expected = read(BUNDLE / 'EXPECTED-CASES.json')
    unit_result = validate_cases(evidence, expected, unit)
    need(manifest['unit_results'] == {k: unit_result[k] for k in ('successful_cases', 'ignored_not_run', 'junit_sha256')},
         'packaged unit summary differs')
    with tarfile.open(fileobj=io.BytesIO(package['herdr-candidate.tar.gz']), mode='r:gz') as archive:
        members = archive.getmembers()
        need(len(members) == 1 and members[0].name == 'herdr' and members[0].isfile()
             and members[0].size == manifest['server_bytes'] and members[0].mode & 0o111,
             'candidate archive is not one exact executable')
        binary = archive.extractfile(members[0]).read()
    need(hashlib.sha256(binary).hexdigest() == manifest['server_sha256'], 'actual server archive bytes differ')
    return manifest, binary


def verify_files(contract, run, jobs, artifacts, evidence_zip, package_zip):
    validate_run(run, jobs, contract)
    by_name = {}
    for a in artifacts['artifacts']:
        need(a['name'] not in by_name, 'ambiguous artifact name')
        by_name[a['name']] = a
    names = [f'herdr-namespace-evidence-{run["id"]}-{run["run_attempt"]}',
             f'herdr-namespace-arm64-{run["id"]}-{run["run_attempt"]}']
    need(all(name in by_name for name in names), 'missing successful candidate/evidence artifacts')
    evidence = validate_zip(evidence_zip, by_name[names[0]], run, names[0])
    package = validate_zip(package_zip, by_name[names[1]], run, names[1])
    return validate_artifact_contents(evidence, package, run, contract)


def verify_saved_proof(proof_path, proof_sha256, binary_path, binary_sha256):
    proof_path = proof_path.resolve()
    need(sha(proof_path) == proof_sha256, 'remote proof pin differs')
    proof = read(proof_path)
    need(proof['schema'] == 'herdr-remote-fixture-proof-v1' and proof['passed'] is True,
         'unaccepted remote proof')
    for p, pin in proof['files'].items():
        need(sha(p) == pin, 'pinned remote evidence changed: ' + p)
    contract = read(proof['contract'])
    manifest, binary = verify_files(contract, read(proof['run']), read(proof['jobs']),
                                    read(proof['artifacts']), proof['evidence_zip'], proof['package_zip'])
    need(Path(proof['binary']).resolve() == binary_path.resolve()
         and sha(binary_path) == binary_sha256 == manifest['server_sha256']
         and len(binary) == Path(binary_path).stat().st_size, 'fixture binary differs from remote artifact')
    return proof


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--contract-sha256', required=True)
    parser.add_argument('--evidence-zip', type=Path, required=True)
    parser.add_argument('--package-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    need(sha(args.contract) == args.contract_sha256, 'reviewed remote contract changed')
    contract = read(args.contract)
    need(contract['repository'] == REPOSITORY and re.fullmatch('[0-9a-f]{40}', contract['head_sha'])
         and isinstance(contract['run_id'], int) and isinstance(contract['run_attempt'], int), 'invalid remote contract')
    run_id = contract['run_id']
    source_proofs = {}
    for name, key in [('integrations/herdr-production-namespace/ci.py', 'ci_sha256'),
                      (WORKFLOW, 'workflow_sha256')]:
        content = gh_json(f'repos/{REPOSITORY}/contents/{name}?ref={contract["head_sha"]}')
        need(content['type'] == 'file' and content['encoding'] == 'base64', 'GitHub source not a file')
        raw = base64.b64decode(content['content'])
        need(hashlib.sha256(raw).hexdigest() == contract[key], 'reviewed GitHub source differs')
        source_proofs[name] = {'blob_sha':content['sha'], 'sha256':contract[key], 'bytes':len(raw)}
    run = gh_json(f'repos/{REPOSITORY}/actions/runs/{run_id}')
    jobs = gh_json(f'repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{contract["run_attempt"]}/jobs?per_page=100')
    artifacts = gh_json(f'repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100')
    manifest, binary = verify_files(contract, run, jobs, artifacts, args.evidence_zip, args.package_zip)
    output = args.output.resolve()
    need(not output.exists(), 'one-use proof output already exists')
    output.mkdir()
    binary_path = output / 'herdr'
    with binary_path.open('xb') as stream:
        stream.write(binary);stream.flush();os.fsync(stream.fileno())
    binary_path.chmod(0o555)
    for name, value in [('RUN.json', run), ('JOBS.json', jobs), ('ARTIFACTS.json', artifacts), ('CONTRACT.json', contract), ('GITHUB-SOURCE-PROOFS.json', source_proofs)]:
        save(output / name, value)
    files = [output / x for x in ['RUN.json','JOBS.json','ARTIFACTS.json','CONTRACT.json','GITHUB-SOURCE-PROOFS.json']]
    files += [args.evidence_zip.resolve(), args.package_zip.resolve(), Path(__file__).resolve(), binary_path]
    proof = {'schema':'herdr-remote-fixture-proof-v1','passed':True,'github_run':run,
             'contract':str(output/'CONTRACT.json'),'run':str(output/'RUN.json'),'jobs':str(output/'JOBS.json'),
             'artifacts':str(output/'ARTIFACTS.json'),'evidence_zip':str(args.evidence_zip.resolve()),
             'package_zip':str(args.package_zip.resolve()),'binary':str(binary_path),
             'binary_sha256':manifest['server_sha256'], 'build_manifest_sha256':hashlib.sha256(
                 validate_zip(args.package_zip, next(a for a in artifacts['artifacts'] if a['name']==f'herdr-namespace-arm64-{run_id}-{run["run_attempt"]}'),
                              run,f'herdr-namespace-arm64-{run_id}-{run["run_attempt"]}')[ 'BUILD-MANIFEST.json']).hexdigest(),
             'files':{str(p):sha(p) for p in files},'local_compilation_claimed':False,'herdr_fixture_started':False}
    save(output/'REMOTE-PROOF.json',proof)
    print(json.dumps({'proof':str(output/'REMOTE-PROOF.json'),'proof_sha256':sha(output/'REMOTE-PROOF.json'),
                      'binary':str(binary_path),'binary_sha256':manifest['server_sha256'],'fixture_started':False}))


if __name__ == '__main__':
    main()
