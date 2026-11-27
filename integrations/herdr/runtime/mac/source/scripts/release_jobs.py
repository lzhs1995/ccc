"""Read-only consistency check for the two separately managed launchd jobs."""
import json
from pathlib import Path
import plistlib


def check(home):
    home = Path(home)
    manifest = home / '.local/state/multi-agent-collaboration/CURRENT.json'
    if not manifest.exists():
        return dict(ok=True, applicable=False, jobs={}, execution_proven=False)
    try:
        current = json.loads(manifest.read_text())
        source, python = current['source'], current['python']
        jobs = {}
        for name, script, mode in (
                ('configuration-guardian', 'configuration_guardian.py', 'reconcile'),
                ('executor-reask-scheduler', 'executor_reask_scheduler.py', 'sweep')):
            label = 'org.multi-agent-collaboration.' + name
            path = home / 'Library/LaunchAgents' / (label + '.plist')
            row = dict(path=str(path), present=path.exists(), configured=False)
            jobs[label] = row
            if path.is_symlink() or not path.is_file():
                continue
            spec = plistlib.loads(path.read_bytes())
            expected = [python, '-B', str(Path(source) / 'scripts' / script), mode]
            if name == 'configuration-guardian':
                expected += ['--home', str(home)]
            expected += ['--manifest', str(manifest)]
            if name == 'configuration-guardian':
                expected += ['--apply']
            row.update(program_arguments=spec.get('ProgramArguments'),
                       configured=spec.get('Label') == label and spec.get('ProgramArguments') == expected)
        return dict(ok=all(r['configured'] for r in jobs.values()), applicable=True,
                    jobs=jobs, source=source, execution_proven=False)
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException) as exc:
        return dict(ok=False, applicable=True, error_type=type(exc).__name__, execution_proven=False)
