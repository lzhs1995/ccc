"""Seal only named outputs of the synthetic nested restoration job."""
import argparse
import json
from pathlib import Path
import re
import secure_bundle as secure

FIXED = ('FIXTURE-JOB.json', 'ACTUAL-AQUA-SESSION.json', 'SIMULATION-COMPILE.json',
    'simulation-compile.log', 'NESTED-ARGUMENTS.json', 'nightly-admission.log',
    'nightly/ARTIFACT-PROOF.json', 'nightly/GUI-ARTIFACT-CONTRACT.json',
    'herdr-admission/HERDR-ADMISSION.json', 'herdr-admission/proof/REMOTE-PROOF.json',
    'herdr-admission/verifier.log', 'EXPECTED-MANIFEST.json', 'BEFORE-CHILDREN.json',
    'AFTER-CHILDREN.json', 'BEFORE-SNAPSHOTS.json', 'ACTUAL-RESTORE-INPUT-SNAPSHOTS.json',
    'AFTER-SNAPSHOTS.json', 'BEFORE-SERVER-EXITS.json', 'BEFORE-CHILD-EXITS.json',
    'CLEANUP-SERVER-EXITS.json', 'CLEANUP-CHILD-EXITS.json', 'CLEANUP.json', 'HERDR-CALLS.json',
    'RESULT.json', 'NESTED-FAILURE.json', 'RUNNER-FAILURE.json', 'nested-fixture.log',
    'gui/FRESH-ACCOUNT-PERSISTENCE.json')
FIXED += tuple(phase + '-LAST-OUTER-OBSERVATION.json' for phase in ('BEFORE', 'ACTUAL-RESTORE-INPUT', 'AFTER'))
FIXED += tuple(f'gui/GEN-{n}-{suffix}' for n in (1, 2) for suffix in
               ('LAUNCH-INTENT.json', 'READY.json', 'STDIO.log', 'STOP-INTENT.json', 'EXIT.json', 'NATURAL-EXIT.json'))
FIXED += tuple(f'{group}-CREATE-{suffix}.json' for group in ('nested-three', 'nested-two', 'nested-four')
               for suffix in ('INTENT', 'REPLY'))
RECEIPT = re.compile(r'nested-fixture-[0-9a-f]{12}-nested-(three|two|four)-[0-3]-[1-9][0-9]*-[1-9][0-9]*\.json')


def selected(root):
    directory = root / 'receipts'
    if directory.is_symlink(): raise ValueError('linked receipt directory')
    paths = sorted(directory.iterdir()) if directory.exists() else []
    if len(paths) > 40: raise ValueError('unexpected receipt count')
    for path in paths:
        if not RECEIPT.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            raise ValueError('foreign receipt member')
    return FIXED + tuple('receipts/' + path.name for path in paths)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--private-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.private_root.resolve(strict=True)
    secure.DIAGNOSTICS = selected(root)
    secure.seal_diagnostics(root, args.output)
    # Public status contains only strict booleans/counts. Raw paths, GUI state,
    # process records and all original source stay inside authenticated crypto.
    public = {'case': 'three-herdr-nine-synthetic-sessions', 'remote_case_passed': False,
              'physical_power_loss_tested': False, 'vendor_sessions_tested': False}
    try:
        data = secure.read_diagnostic(root, 'RESULT.json', 1024 * 1024)
        value = json.loads(data)
        public['remote_case_passed'] = value.get('passed') is True
        public['outer_workspaces'] = value.get('outer_workspaces') if type(value.get('outer_workspaces')) is int else None
        public['same_synthetic_ids'] = value.get('children', {}).get('same_synthetic_ids')
        if type(public['same_synthetic_ids']) is not int: public['same_synthetic_ids'] = None
    except (OSError, ValueError, TypeError):
        public['result_missing_or_unreadable'] = True
    secure.write_new(args.output / 'NESTED-PUBLIC.json', (json.dumps(public, indent=2) + '\n').encode())


if __name__ == '__main__': main()
