#!/usr/bin/env python3
"""Prepare or install the separate CCC logs maintenance component.

Default only writes a reviewable deployment bundle. --install writes launchd;
--enable is required in addition to --install for automatic log deletion.
Existing cmux snapshot janitor scripts/config/retention are not rewritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    if args.enable and not args.install:
        parser.error('--enable requires --install')
    source = Path(__file__).with_name('batch_log_cleanup.py')
    destination = Path.home() / '.config/cmux-janitor/ccc-batch-logs'
    prefix = (os.environ.get('CCC_LABEL_PREFIX')
              or f"com.{os.environ.get('USER') or Path.home().name or 'user'}")
    label = f'{prefix}.cmux-ccc-batch-logs'
    policy = {'version': 1, 'enabled': args.enable,
              'app': str(Path.home() / 'Library/Application Support/cmux-codex-continue'),
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'idle_hours': 168, 'pressure_idle_hours': 24, 'min_free_gib': 10, 'max_jobs': 32}
    plist = {'Label': label, 'ProgramArguments': ['/opt/homebrew/opt/python@3.14/bin/python3.14', '-B',
             str(destination / source.name), '--scheduled-policy', str(destination / 'policy.json')],
             'StartInterval': 1800, 'RunAtLoad': False,
             'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
             'ProcessType': 'Background'}
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copyfile(source, args.output / source.name)
    (args.output / 'policy.json').write_text(json.dumps(policy, indent=2) + '\n')
    (args.output / (label + '.plist')).write_bytes(plistlib.dumps(plist))
    if args.install:
        if destination.exists():
            raise RuntimeError('existing component; review before replacing it')
        destination.mkdir(mode=0o700)
        (destination / 'history').mkdir(mode=0o700)
        for name in (source.name, 'policy.json'):
            shutil.copyfile(args.output / name, destination / name)
            os.chmod(destination / name, 0o600)
        (destination / 'sweep.lock').touch(mode=0o600)
        launch = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        with launch.open('xb') as handle:
            handle.write(plistlib.dumps(plist))
        subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(launch)], check=True)
    print(json.dumps({'bundle': str(args.output), 'installed': args.install, 'enabled': args.enable}))


if __name__ == '__main__':
    main()
