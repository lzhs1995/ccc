"""GHA-only retrieval of the exact artifact already tested by all four native cases."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import zipfile
from common import FLOOR, SMOKE_HEADROOM, admit, capacity, gha_only, pin, require, save, stop_owned, verify_package
from rejudge import ORIGINAL_ARTIFACT, ORIGINAL_ARCHIVE, ORIGINAL_BINARY, ORIGINAL_BUILD, ORIGINAL_COMMIT, ORIGINAL_RUN


def verify_fetch(folder, package_pin):
    """Recheck original input provenance from archived small evidence only."""
    folder = Path(folder)
    row = json.loads((folder / 'ORIGINAL-INPUT.json').read_text())
    require(row['schema'] == 'magpie-web-original-artifact-input-v1' and row['status'] == 'PASS' and
            row['original_run'] == ORIGINAL_RUN and row['original_commit'] == ORIGINAL_COMMIT and
            row['original_artifact_id'] == ORIGINAL_ARTIFACT and row['archive_digest'] == ORIGINAL_ARCHIVE and
            row['package_pin'] == package_pin, 'original fetch identity differs')
    require(row['archive'] == {'sha256': ORIGINAL_ARCHIVE.split(':')[1], 'bytes': 146500547} and
            row['build'] == ORIGINAL_BUILD and row['binary'] == ORIGINAL_BINARY and
            pin(folder / 'BUILD.json') == ORIGINAL_BUILD and
            row['original_metadata'] == pin(folder / 'ORIGINAL-ARTIFACT-METADATA.json'),
            'original ZIP/build/binary input changed')
    metadata = json.loads((folder / 'ORIGINAL-ARTIFACT-METADATA.json').read_text())
    require(metadata['id'] == ORIGINAL_ARTIFACT and metadata['digest'] == ORIGINAL_ARCHIVE and
            metadata['name'] == f'magpie-passing-native-{ORIGINAL_RUN}-1' and
            metadata['size_in_bytes'] == 146500547 and metadata['workflow_run']['id'] == ORIGINAL_RUN and
            metadata['workflow_run']['head_sha'] == ORIGINAL_COMMIT and
            metadata['workflow_run']['head_branch'] == 'ci/magpie-passing-smoke-20261112',
            'original metadata changed')
    require(row['child_reaped'] is True and
            all(type(row[k]) is int and row[k] == 0 for k in ('compilations', 'model_requests', 'native_cases_repeated')),
            'unexpected build/model/repetition or incomplete cleanup')
    samples = row['capacity_samples']
    require(len(samples) >= 2 and all(type(s['free_bytes']) is int and s['free_bytes'] >= FLOOR for s in samples) and
            all(s['free_bytes'] >= FLOOR + SMOKE_HEADROOM for s in samples[:2]), 'fetch capacity records failed')
    return {'schema': row['schema'], 'status': 'PASS', 'original_run': ORIGINAL_RUN,
            'original_artifact_id': ORIGINAL_ARTIFACT, 'archive_digest': ORIGINAL_ARCHIVE,
            'build': ORIGINAL_BUILD, 'binary': ORIGINAL_BINARY, 'receipt': pin(folder / 'ORIGINAL-INPUT.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package-pin', required=True)
    args = parser.parse_args()
    gha_only()
    verify_package(Path(__file__).resolve().parent, args.package_pin)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = {'schema': 'magpie-web-original-artifact-input-v1', 'status': 'NOT_STARTED',
               'original_run': ORIGINAL_RUN, 'original_commit': ORIGINAL_COMMIT,
               'original_artifact_id': ORIGINAL_ARTIFACT, 'archive_digest': ORIGINAL_ARCHIVE,
               'package_pin': args.package_pin, 'capacity_samples': [], 'compilations': 0,
               'model_requests': 0, 'native_cases_repeated': 0}
    child = None
    archive = output / 'original.zip'
    try:
        admit(output, SMOKE_HEADROOM, receipt['capacity_samples'])
        env = os.environ.copy()
        env.pop('GH_DEBUG', None)
        endpoint = f'repos/lzhs1995/ccc/actions/artifacts/{ORIGINAL_ARTIFACT}'
        metadata = json.loads(subprocess.check_output(['gh', 'api', endpoint], env=env, timeout=30))
        require(metadata['id'] == ORIGINAL_ARTIFACT and metadata['digest'] == ORIGINAL_ARCHIVE and
                metadata['name'] == f'magpie-passing-native-{ORIGINAL_RUN}-1' and
                metadata['size_in_bytes'] == 146500547 and metadata['expired'] is False and
                metadata['workflow_run']['id'] == ORIGINAL_RUN and
                metadata['workflow_run']['head_sha'] == ORIGINAL_COMMIT and
                metadata['workflow_run']['head_branch'] == 'ci/magpie-passing-smoke-20261112',
                'original artifact metadata differs')
        save(output / 'ORIGINAL-ARTIFACT-METADATA.json', metadata)
        with archive.open('xb') as stream, (output / 'fetch.stderr').open('xb') as errors:
            child = subprocess.Popen(['gh', 'api', endpoint + '/zip'], env=env,
                                     stdin=subprocess.DEVNULL, stdout=stream, stderr=errors,
                                     start_new_session=True)
            deadline = time.monotonic() + 180
            while child.poll() is None:
                sample = capacity(output)
                receipt['capacity_samples'].append(sample)
                require(sample['free_bytes'] >= FLOOR and time.monotonic() < deadline,
                        'artifact retrieval capacity/deadline failed')
                time.sleep(.5)
            require(type(child.returncode) is int and child.returncode == 0, 'artifact download failed')
        receipt['archive'] = pin(archive)
        require(receipt['archive'] == {'sha256': ORIGINAL_ARCHIVE.split(':')[1], 'bytes': 146500547},
                'full original ZIP digest differs')
        wanted = {'results/candidate/BUILD.json': ORIGINAL_BUILD,
                  'results/candidate/magpie-candidate': ORIGINAL_BINARY}
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            for name, expected in wanted.items():
                require(names.count(name) == 1, 'original archive member missing or duplicated')
                entry = bundle.getinfo(name)
                require(entry.file_size == expected['bytes'] and
                        not stat.S_ISLNK(entry.external_attr >> 16) and not entry.is_dir(),
                        'original archive member type/size differs')
                path = output / Path(name).name
                with bundle.open(entry) as source, path.open('xb') as destination:
                    shutil.copyfileobj(source, destination)
                require(pin(path) == expected, 'extracted original artifact member differs')
        binary = output / 'magpie-candidate'
        binary.chmod(0o755)
        receipt.update(status='PASS', build=pin(output / 'BUILD.json'), binary=pin(binary),
                       original_metadata=pin(output / 'ORIGINAL-ARTIFACT-METADATA.json'))
    except Exception as error:
        receipt.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
    finally:
        try:
            stop_owned(child)
            receipt['child_reaped'] = child is None or child.poll() is not None
        except Exception as error:
            receipt.update(status='FAIL', child_reaped=False,
                           cleanup_error=type(error).__name__ + ': ' + str(error))
        save(output / 'ORIGINAL-INPUT.json', receipt)
    require(receipt['status'] == 'PASS' and receipt['child_reaped'],
            'original artifact retrieval failed; preserve original receipt')


if __name__ == '__main__':
    main()
