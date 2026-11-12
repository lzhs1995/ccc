"""Preserve only bounded original App-transfer receipts with original R5 sealer."""
from pathlib import Path
import argparse
import supplemental_transport as transport


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--crypto', required=True, type=Path)
    parser.add_argument('--private-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    original = transport.crypto(args.crypto)
    original.DIAGNOSTICS = ('INTENT.json', 'RESULT.json', 'ADMISSION.json')
    original.seal_diagnostics(args.private_root, args.output)


if __name__ == '__main__':
    main()
