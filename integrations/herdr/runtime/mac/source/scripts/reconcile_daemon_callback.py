"""Zero-input recovery for the pinned classifier209 callback journal.

Keep the original pack, report, bridge, journal, budgets and native-record
validation. In this fresh process only, use the reviewed caller guard dependency
to resolve a shared daemon. This entry cannot send or retry terminal input.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

PINS = {
    'cmux_bridge.py': '00e95643eb951a28426b2c6cc2682dfb097d4080966092682ae47c1863c2f3d9',
    'cmux_callback_journal.py': '043e3f5ea42404282dde43c2ac3ae0a07a23588920429395dbe68429a18e8097',
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--task-pack', required=True, type=Path)
    ap.add_argument('--transcript', required=True, type=Path)
    ap.add_argument('--line', required=True, type=int)
    args = ap.parse_args()
    # Import the revised guard before the original bridge. No identity stub or
    # os.environ override: every pin_workspace call runs the actual live guard.
    import cmux_workspace_guard
    pack = json.loads(args.task_pack.read_text())
    source = Path(pack['required_skill']).parent / 'scripts'
    if not source.is_absolute() or source.resolve() != source:
        raise RuntimeError('original source must not be an alias')
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.glob('*.py') if not p.is_symlink()}
    if any(before.get(str(source / name)) != digest for name, digest in PINS.items()):
        raise RuntimeError('unsupported original controller; preserve journal')
    if any(name in sys.modules for name in ('cmux_bridge', 'cmux_callback_journal')):
        raise RuntimeError('fresh recovery process required')
    sys.path.insert(0, str(source))
    import cmux_bridge
    import cmux_callback_journal
    def check():
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in source.glob('*.py') if not p.is_symlink()}
        if after != before:
            raise RuntimeError('original runtime changed')
    check()
    result = cmux_callback_journal.reconcile_native(
        cmux_bridge, args.task_pack, transcript=args.transcript,
        line=args.line, received_callback=pack['completion_callback'])
    check()
    print(json.dumps(dict(confirmed=result['confirmed'], input_operations=0,
                          receipt=pack['completion_receipt'],
                          original_sources_stable=True), ensure_ascii=False))


if __name__ == '__main__': main()
