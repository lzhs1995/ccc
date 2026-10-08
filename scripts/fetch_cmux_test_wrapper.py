"""Fetch an immutable upstream wrapper for isolated handoff tests only.

The blob is the cmux wrapper exercised by the original local regression.
It is not installed, distributed in CCC, or allowed to launch a real client.
Upstream: https://github.com/manaflow-ai/cmux (GPL-3.0-or-later).
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.request

BLOB = '7e275aeb4da8a2a7b0456d562dbc5a89656d0d94'
SHA256 = '95f363c75c5d8311bc456151eb85a9b910b130e940c537e04560b041b7cf4f1d'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = urllib.request.Request(
        'https://api.github.com/repos/manaflow-ai/cmux/git/blobs/' + BLOB,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ccc-test-fixture'})
    if os.environ.get('GH_TOKEN'):
        request.add_header('Authorization', 'Bearer ' + os.environ['GH_TOKEN'])
    with urllib.request.urlopen(request, timeout=30) as response:
        blob = json.load(response)
    if blob.get('sha') != BLOB or blob.get('encoding') != 'base64':
        raise RuntimeError('upstream fixture identity mismatch')
    payload = base64.b64decode(blob['content'])
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise RuntimeError('upstream fixture digest mismatch')
    with args.output.open('xb') as stream:
        stream.write(payload)
    args.output.chmod(0o700)
    print(json.dumps({'blob': BLOB, 'sha256': SHA256, 'bytes': len(payload)}))


if __name__ == '__main__':
    main()
