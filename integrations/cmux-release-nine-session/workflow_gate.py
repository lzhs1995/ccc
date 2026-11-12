"""Reject pending publication pins before dependencies, keys or App transfer."""
from pathlib import Path
import argparse
import os
import re
import supplemental_transport as transport

ORIGINAL_TRANSPORT_SHA = 'f3bf2e8070365ff2d9bb684772d553ad4619a6ccc72e92c3df7535023e598e7f'


def check(root, pin, original_root):
    transport.need(isinstance(pin, str) and re.fullmatch('[0-9a-f]{64}', pin),
                   'PENDING: reviewed deployment pin missing')
    raw = transport.read(root / 'DEPLOYMENT.json')
    transport.need(transport.digest(raw) == pin, 'deployment pin differs')
    value = transport.decode(raw)
    transport.need(value.get('schema') == 'ccc-nine-session-deployment-v1'
        and value.get('status') == 'PINNED_READY_FOR_ORIGINAL_CONTROLLER'
        and transport.same(value.get('producer'), transport.PRODUCER)
        and value.get('source_lock_sha256') == transport.SOURCE_LOCK, 'PENDING: completed original input pins missing')
    for kind, name in (('source', 'SUPPLEMENTAL-TRANSPORT.json'), ('original-inputs', 'INPUTS-TRANSPORT.json')):
        expected = value.get(kind + '_transport_sha256')
        transport.need(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'payload transport pin missing')
        raw = transport.read(root / name)
        transport.need(transport.digest(raw) == expected, 'payload transport contract changed')
        contract = transport.validate_contract(transport.decode(raw), sealed=True)
        transport.need(contract['kind'] == kind, 'wrong supplemental purpose')
        if kind == 'original-inputs':
            transport.need(contract['input_contract_sha256'] == value.get('input_contract_sha256'), 'original input pin differs')
        payload = transport.read(root / (kind + '.aesgcm'), transport.MAX_PAYLOAD + 28)
        transport.need(len(payload) == contract['ciphertext_bytes']
                       and transport.digest(payload) == contract['ciphertext_sha256'], 'ciphertext differs before key read')
    transport.need(transport.digest(transport.read(original_root / 'TRANSPORT.json')) == ORIGINAL_TRANSPORT_SHA
                   and transport.digest(transport.read(original_root / 'secure_bundle.py')) == transport.CRYPTO_SHA,
                   'original R5 source transport changed')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--original-root', type=Path, required=True)
    parser.add_argument('--deployment-sha256', required=True)
    args = parser.parse_args()
    value = check(args.root, args.deployment_sha256, args.original_root)
    with Path(os.environ['GITHUB_ENV']).open('a') as stream:
        for key, field in (('SUPPLEMENTAL_PIN', 'source_transport_sha256'), ('INPUTS_TRANSPORT_PIN', 'original-inputs_transport_sha256'),
                           ('ORIGINAL_INPUT_PIN', 'input_contract_sha256')):
            stream.write(key + '=' + value[field] + '\n')
    print('Complete original producer inputs and source transport pins admitted for decryption.')


if __name__ == '__main__':
    main()
