"""Download the official client on GHA only; both archive and extracted bytes are pinned."""
import argparse
import json
from pathlib import Path
import tarfile
import time
import urllib.request
from common import FLOOR, SMOKE_HEADROOM, admit, capacity, gha_only, pin, require, save, verify_package


def extract(archive, output, spec):
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        require(len(members) == 1 and members[0].name == spec['binary_name'] and
                members[0].isfile() and members[0].size == spec['binary_bytes'], 'unexpected archive membership')
        stream = tar.extractfile(members[0])
        require(stream is not None, 'missing binary stream')
        with output.open('xb') as dest:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                dest.write(block)
    require(pin(output) == {'sha256': spec['binary_sha256'], 'bytes': spec['binary_bytes']},
            'official extracted binary differs from original fixed client; no substitution allowed')
    output.chmod(0o755)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--package-pin', required=True)
    a = p.parse_args()
    gha_only()
    root = Path(__file__).resolve().parent
    verify_package(root, a.package_pin)
    spec = json.loads((root / 'CLIENT-ASSET.json').read_text())
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {'status': 'NOT_STARTED', 'package_pin': a.package_pin, 'spec': spec, 'samples': []}
    archive = output / 'official.tar.gz'
    binary = output / spec['binary_name']
    try:
        admit(output, SMOKE_HEADROOM, result['samples'])
        deadline = time.monotonic() + 240
        with urllib.request.urlopen(spec['url'], timeout=20) as stream, archive.open('xb') as dest:
            count = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                count += len(chunk)
                require(count <= spec['archive_bytes'], 'archive exceeds pinned size')
                sample = capacity(output)
                result['samples'].append(sample)
                require(sample['free_bytes'] >= FLOOR and time.monotonic() < deadline, 'download floor/deadline failed')
                dest.write(chunk)
        require(pin(archive) == {'sha256': spec['archive_sha256'], 'bytes': spec['archive_bytes']}, 'official archive pin differs')
        extract(archive, binary, spec)
        result.update(status='PASS', archive=pin(archive), binary=pin(binary), binary_name=binary.name)
    except Exception as error:
        result.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
    finally:
        save(output / 'CLIENT.json', result)
    require(result['status'] == 'PASS', 'client identity verification failed; no alternative client may be used')


if __name__ == '__main__':
    main()
