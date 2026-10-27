"""Fetch pinned collaboration scripts for isolated tests; never install hooks.

Upstream: https://github.com/lzhs1995/multi-agent-collaboration
Commit: bb1bae346f765d8abfface19b7c29858ebf133bc
These files match the collaboration 0.4.24 source used by the local regression.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.request

FIXTURES = {
    'executor_closeout.py': (
        'cee18f8c3c5ef01bae0851f4da9d408a37d88197',
        '7d36ed85f2d920ea875461a61d89da4401b065dd61c7b3039c976e9ce8bd4743'),
    'cmux_evidence_io.py': (
        '9fdee57f29c61605ea1d5e86bd8607a851080eb7',
        '67d6f03426971f833e1e744b935d8d9190cfa4240cb0bbbe190e758b13f0ad4c'),
    'cmux_executor_closeout_guard.py': (
        'f1d2ac2ab282a5ecd1ca163c417793e7b889eae5',
        '1373bc5cdab1f3fd02b37dda862d0192cbf62450010b1e8fd1a34232ab91f79c'),
    'cmux_consensus_stop_guard.py': (
        'e1b0d465e194bf4df6020d5f55dbd29c64ad6590',
        '276746b95bdb2495df444899f5cdeae9ade25bec57ac56f64a5f8b11bbb1b12f'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, (expected_blob, expected_sha256) in FIXTURES.items():
        request = urllib.request.Request(
            'https://api.github.com/repos/lzhs1995/multi-agent-collaboration/git/blobs/' + expected_blob,
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ccc-test-fixture'})
        if os.environ.get('GH_TOKEN'):
            request.add_header('Authorization', 'Bearer ' + os.environ['GH_TOKEN'])
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError('oversized upstream fixture: ' + name)
        blob = json.loads(raw)
        if blob.get('sha') != expected_blob or blob.get('encoding') != 'base64':
            raise RuntimeError('upstream fixture identity mismatch: ' + name)
        payload = base64.b64decode(blob['content'])
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise RuntimeError('upstream fixture digest mismatch: ' + name)
        path = args.output / name
        with path.open('xb') as stream:
            stream.write(payload)
        path.chmod(0o600)
        print(json.dumps({'file': name, 'blob': expected_blob,
                          'sha256': expected_sha256, 'bytes': len(payload)}))


if __name__ == '__main__':
    main()
