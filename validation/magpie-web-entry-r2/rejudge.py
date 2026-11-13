"""Rejudge one immutable archived Web attempt; never execute a binary or HTTP call."""
import argparse
import copy
import json
from pathlib import Path
from common import pin, require, save, verify_package
import web_entry

ORIGINAL_RUN = 43257559926
ORIGINAL_COMMIT = '758f6e23c372dc607963a0bbf07e34c3718c4962'
ORIGINAL_ARTIFACT = 13962716444
ORIGINAL_ARCHIVE = 'sha256:4228003c853e152a8e34410e4c35aaa09007cd9551e7f3616deab20524e71038'
ORIGINAL_PACKAGE = '6660fdc12a54273f1d5ceb8d9b434712d5d13da2f92f5dfd6fa4bc1c1d1551d6'
ORIGINAL_RECEIPT = {'sha256': 'aa261e465fa4d432e339f5b2669a2110009d6aac5ec9678ecb418b879e2e4d54', 'bytes': 7456}
ORIGINAL_BUILD = {'sha256': '384bb00e0399039ccb982074d45303a9803bcc2c2bafa741893e73b2795f8863', 'bytes': 26870}
ORIGINAL_BINARY = {'sha256': 'd80d86579151c848a93857472973daddf6968f433b8b668ac7d5081e89fecc45', 'bytes': 51571554}
ORIGINAL_ERROR = 'RuntimeError: synthetic auth cookie differs'


def rejudge(artifact_root, candidate_folder, artifact, package_pin):
    root = Path(artifact_root)
    candidate = Path(candidate_folder)
    require(root.is_absolute() and root.resolve() == root and candidate == root / 'results/candidate',
            'original artifact root/candidate differs')
    package = Path(__file__).resolve().parent
    verify_package(package, package_pin)
    require(artifact == ORIGINAL_BINARY, 'original binary identity differs')
    metadata = json.loads((root / 'ARTIFACT-METADATA.json').read_text())
    require(metadata['id'] == ORIGINAL_ARTIFACT and metadata['digest'] == ORIGINAL_ARCHIVE and
            metadata['name'] == f'magpie-passing-native-{ORIGINAL_RUN}-1' and
            metadata['workflow_run']['id'] == ORIGINAL_RUN and
            metadata['workflow_run']['head_sha'] == ORIGINAL_COMMIT and
            metadata['workflow_run']['head_branch'] == 'ci/magpie-passing-smoke-20261112',
            'rejudgment is restricted to one original archive/run')
    verify_package(root / 'validation/magpie-web-entry-r1', ORIGINAL_PACKAGE)
    web = root / 'results/web-entry'
    original_path = web / 'WEB-ACCEPTANCE.json'
    require(pin(original_path) == ORIGINAL_RECEIPT and pin(candidate / 'BUILD.json') == ORIGINAL_BUILD,
            'original receipt/build bytes differ')
    original = json.loads(original_path.read_text())
    require(original['status'] == 'FAIL' and original['error'] == ORIGINAL_ERROR and
            original['package_pin'] == ORIGINAL_PACKAGE and
            not any(k in original for k in ('cleanup_error', 'verification_error', 'http_validation')),
            'original failure was not solely the known cookie judge')
    # All six requests and cleanup happened before the original judge failed.
    # Normalize only an in-memory view for the strict receipt verifier. The
    # original file and its FAIL remain intact; the separate result records both.
    rows = {name: (json.loads((web / (name + '.json')).read_text()),
                   (web / (name + '.body')).read_bytes()) for name in web_entry.HTTP_NAMES}
    verdict = web_entry.judge_http(rows)
    normalized = copy.deepcopy(original)
    normalized['status'] = 'PASS'
    del normalized['error']
    normalized['http_validation'] = verdict
    verification = web_entry.verify_receipt(web, normalized, candidate / 'BUILD.json', artifact)
    require(pin(original_path) == ORIGINAL_RECEIPT, 'original receipt changed during rejudgment')
    return {'schema': 'magpie-gha-web-rejudgment-v1', 'status': 'PASS_ORIGINAL_EVIDENCE_REJUDGED',
            'original_run': ORIGINAL_RUN, 'original_commit': ORIGINAL_COMMIT,
            'original_artifact_id': ORIGINAL_ARTIFACT, 'original_archive_digest': ORIGINAL_ARCHIVE,
            'original_status': original['status'], 'original_error': original['error'],
            'original_receipt': ORIGINAL_RECEIPT, 'original_build': ORIGINAL_BUILD,
            'original_web_package_pin': ORIGINAL_PACKAGE, 'corrected_package_pin': package_pin,
            'artifact': artifact, 'http_validation': verdict, 'verification': verification,
            'original_file_pins': original['files'], 'model_posts': 0,
            'binary_executions': 0, 'http_requests': 0, 'original_files_modified': False,
            'scope': 'One archived Web attempt rejudged after correcting Priority=High parsing; original workflow and receipt remain FAIL.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-root', type=Path, required=True)
    parser.add_argument('--package-pin', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.artifact_root.resolve(strict=True)
    output = args.output.absolute()
    require(not output.exists() and not output.is_relative_to(root),
            'write only a fresh result outside original evidence')
    result = rejudge(root, root / 'results/candidate', ORIGINAL_BINARY, args.package_pin)
    save(output, result)
    print(json.dumps({'status': result['status'], 'original_status': result['original_status'],
                      'output': str(output)}))


if __name__ == '__main__':
    main()
