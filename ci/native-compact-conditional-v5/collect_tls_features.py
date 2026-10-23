"""Record compiled dependency feature fingerprints without changing a build."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    fingerprints = []
    for pattern in ('reqwest-*/lib-reqwest.json', 'codex-http-client-*/lib-codex_http_client.json'):
        for path in sorted((args.target / 'debug/.fingerprint').glob(pattern)):
            raw = path.read_bytes()
            value = json.loads(raw)
            fingerprints.append({
                'path': str(path.relative_to(args.target)),
                'sha256': hashlib.sha256(raw).hexdigest(),
                'features': value['features'],
                'declared_features': value.get('declared_features'),
                'deps': value.get('deps'),
                'profile': value.get('profile'),
                'rustflags': value.get('rustflags'),
            })
    if not any('reqwest-' in record['path'] for record in fingerprints):
        raise ValueError('compiled reqwest feature fingerprints were not found')
    result = {
        'schema': 'compiled-tls-feature-evidence-v1',
        'scope': 'Existing target fingerprints; multiple versions or feature sets remain separate.',
        'fingerprints': fingerprints,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
