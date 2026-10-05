"""CLI for original run evidence at its actual lifecycle boundaries.

Does not start, activate, stop or interrupt sessions. Baseline/register must
precede Runner.run; completion must precede owner closure; settle follows
closure and experimental resource cleanup. Missing evidence stays unresolved.
"""
import argparse
from pathlib import Path
import os

from ccc_native_standby import identifier
from ccc_standby_runner import connect
from ccc_standby_timing import _read
from tools.standby_run_manifest import RunManifest
from tools import standby_cleanup_evidence as cleanup
from tools import standby_completion_evidence as completion
from tools import standby_preparation_cleanup as preparation
from tools import standby_job_terminal as job_terminal
from tools import standby_attempt_terminal as attempt_terminal
from tools import standby_run_terminal as run_terminal


def original(directory, batch_id):
    manifest = RunManifest(directory)
    bid = identifier(batch_id)
    expected = next((r for r in manifest.plan['batches']
                     if identifier(r['batch_id']) == bid), None)
    if expected is None:
        raise ValueError('batch was not predeclared')
    _, attempt, _, invocation = manifest._attempt(expected)
    opened, _ = _read(Path(attempt['runner_directory'])/'runner-open.json')
    if (opened.get('invocation_id') != invocation.value['invocation_id']
            or opened.get('invocation_sha256') != invocation.sha256
            or opened.get('config_path') != invocation.value['config_path']
            or any(opened.get(k) != expected[k] for k in ('workspace_id', 'mode'))):
        raise ValueError('runner open differs from registered invocation')
    return manifest, attempt, invocation, opened


def observe_completion(directory, batch_id, output, *, failed_rounds):
    manifest, attempt, invocation, opened = original(directory, batch_id)
    if os.path.lexists(Path(attempt['runner_directory'])/'runner-closed.json'):
        raise ValueError('completion must be captured while owner is alive')
    config, job_id = opened['config_path'], opened['job_id']
    manifest.bind(batch_id, config, job_id)
    # The completion producer uses the original owner API and its own live
    # checks. Do not infer liveness merely from absence of runner-closed.
    return completion.capture(config, job_id, output, failed_rounds=failed_rounds)


def settle(directory, batch_id, output, baseline_path, *, completion_path=None, client=None):
    manifest, attempt, invocation, opened = original(directory, batch_id)
    runner = Path(attempt['runner_directory'])
    # Require the actual closure record before opening a controller or
    # beginning any potentially slow cleanup observation.
    _read(runner/'runner-closed.json')
    config, job_id = opened['config_path'], opened['job_id']
    standby = Path(opened['job_path']).parent/'standby'
    activated = any(os.path.lexists(standby/name) for name in
                    ('activation-ui.json', 'activation-attempt.json'))
    if client is None:
        client = connect(invocation.value)
    settled = os.path.lexists(standby/'submission-settled.json')
    if activated and settled:
        manifest.bind(batch_id, config, job_id)
        return job_terminal.capture(config, job_id, output, runner_directory=runner,
            completion_path=completion_path, baseline_path=baseline_path, client=client)
    if completion_path is not None:
        raise ValueError('unactivated or unsettled attempt cannot consume completion evidence')
    preparation.capture(config, job_id, output, runner_directory=runner,
        preparation_directory=Path(opened['owner_spec_path']).parent,
        baseline_path=baseline_path, client=client, partial_activation=activated)
    return attempt_terminal.capture(directory, batch_id, config, job_id,
                                    Path(output)/'preparation-cleanup.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-directory', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    baseline = commands.add_parser('baseline')
    baseline.add_argument('--invocation', required=True)
    baseline.add_argument('--sha256', required=True)
    baseline.add_argument('--output', required=True)
    observe = commands.add_parser('completion')
    observe.add_argument('--batch-id', required=True)
    observe.add_argument('--output', required=True)
    observe.add_argument('--failed-rounds', type=int, required=True)
    terminal = commands.add_parser('settle')
    terminal.add_argument('--batch-id', required=True)
    terminal.add_argument('--output', required=True)
    terminal.add_argument('--baseline', required=True)
    terminal.add_argument('--completion')
    final = commands.add_parser('finalize')
    final.add_argument('--terminals', required=True, help='private JSON batch-id to original terminal path map')
    commands.add_parser('verify')
    args = parser.parse_args()
    if args.command == 'baseline':
        from ccc_standby_runner import Invocation
        invocation = Invocation(args.invocation, args.sha256)
        result = cleanup.capture_baseline(args.output, client=connect(invocation.value))
    elif args.command == 'completion':
        result = observe_completion(args.run_directory, args.batch_id, args.output,
                                    failed_rounds=args.failed_rounds)
    elif args.command == 'settle':
        result = settle(args.run_directory, args.batch_id, args.output, args.baseline,
                        completion_path=args.completion)
    elif args.command == 'finalize':
        result = run_terminal.capture(args.run_directory, _read(args.terminals)[0])
    else:
        result = run_terminal.verify(args.run_directory)
    # Do not print invocation environments or full process inventories.
    import json
    print(json.dumps({k: result[k] for k in ('kind', 'job_id', 'run_id', 'outcome',
        'run_terminal', 'succeeded') if k in result}))


if __name__ == '__main__':
    main()
