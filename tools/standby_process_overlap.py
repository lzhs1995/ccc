"""Observe a common lifetime interval for the ten declared native cohorts.

Two complete live sweeps bracket one common interval. Matching OS process
births prove original process coexistence, not continuous session ownership,
atomic writer/configuration state, task performance or complete 500 acceptance.
No sessions are started, interrupted, resumed or closed by this observer.
"""
from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

from ccc_batch_timing import stamp
from ccc_native_standby import COUNT, identifier, write_once
from ccc_standby_acceptance import FirstTaskObserver
from ccc_standby_runner import connect
from ccc_standby_settlement import live_settlement
from ccc_standby_timing import _elapsed
from tools.standby_completion_evidence import ObservationBudget
from tools.standby_run_manifest import RunManifest, directory_identity
from tools.standby_run_observer import original


def validate_topology(plan):
    """Require the requested five workspaces, two fifty-slot cohorts each."""
    from tools.standby_run_manifest import validate_plan
    validate_plan(plan)
    if (len(plan['batches']) != 10 or plan['planned_sessions'] != 500
            or len(plan['workspace_slots']) != 5
            or set(plan['workspace_slots'].values()) != {100}):
        raise ValueError('five workspaces with two fifty-slot cohorts each required')


def observe_sweeps(entries, check_bindings, *, boot, clock=stamp,
                   seconds=120, monotonic=time.monotonic, budget=None):
    """In-memory observation; entries contain already bound original rows.

    The supplied live checks must verify birth/argv/surface/writer identity,
    as FirstTaskObserver._live does. Injected checks are for offline tests and
    cannot turn synthetic results into native evidence.
    """
    if budget is None:
        budget = ObservationBudget(seconds, 1, monotonic)
    budget.check()
    if len(entries) != 500:
        raise ValueError('all 500 original entries required')
    seen = {key: set() for key in ('pid', 'session_id', 'surface_id', 'writer_identity')}
    originals = []
    for row, live in entries:
        budget.check()
        if not callable(live) or type(row.get('pid')) is not int or row['pid'] <= 0:
            raise ValueError('bound original live process required')
        birth = row.get('birth')
        if (not isinstance(birth, list) or len(birth) != 2
                or any(type(x) is not int for x in birth)
                or birth[0] <= 0 or not 0 <= birth[1] < 1000000):
            raise ValueError('original microsecond process birth required')
        for key, values in seen.items():
            value = row[key]
            if key in ('session_id', 'surface_id'):
                value = identifier(value)
            elif key == 'writer_identity':
                if (not isinstance(value, list) or len(value) != 2
                        or any(type(x) is not int or x < 0 for x in value)):
                    raise ValueError('original writer identity required')
                value = tuple(value)
            if value in values:
                raise ValueError('duplicate original ' + key)
            values.add(value)
        originals.append(copy.deepcopy(row))

    def check():
        budget.check()
        check_bindings()
        budget.check()

    def sweep():
        spans = []
        for index, (row, live) in enumerate(entries):
            budget.check()
            if row != originals[index]:
                raise ValueError('original binding mutated')
            start = clock()
            live()
            end = clock()
            _elapsed(start, end, boot)
            if row != originals[index]:
                raise ValueError('original binding mutated during live read')
            budget.check()
            spans.append({'index': index, 'started': start, 'finished': end})
        return spans

    check()
    before = sweep()
    lower = clock()
    check()
    upper = clock()
    duration = _elapsed(lower, upper, boot)
    if duration <= 0:
        raise ValueError('positive common overlap interval required')
    after = sweep()
    check()
    for first, last in zip(before, after):
        _elapsed(first['finished'], lower, boot)
        _elapsed(upper, last['started'], boot)
    return {'originals': originals, 'before': before, 'after': after,
            'overlap_started': lower, 'overlap_finished': upper,
            'overlap_seconds': duration, 'process_overlap_proven': True,
            'continuous_session_ownership_proven': False,
            'atomic_configuration_or_writer_snapshot': False,
            'full_500_acceptance': False, 'run_terminal': False}


def capture(run_directory, output_directory, *, seconds=120):
    """Reopen declared original receipts and use actual live process checks."""
    budget = ObservationBudget(seconds, 1, time.monotonic)
    manifest = RunManifest(run_directory)
    validate_topology(manifest.plan)
    directory = Path(output_directory)
    identity = directory_identity(directory)
    attempts = manifest.attempts()
    bindings = manifest.resolve()
    observers, entries, settlements = [], [], []
    for item in bindings['batches']:
        budget.check()
        binding = item['binding']
        config, job = binding['config_path'], binding['job_id']
        pinned, attempt, invocation, opened = original(run_directory, binding['batch_id'])
        if (pinned.sha256 != manifest.sha256 or opened['config_path'] != config
                or opened['job_id'] != job):
            raise ValueError('original invocation differs from bound batch')
        saved, digest = live_settlement(config, job)
        if (digest != binding['settlement_sha256']
                or saved['action_id'] != binding['action_id']):
            raise ValueError('original settlement differs from run binding')
        observer = FirstTaskObserver(config, job, saved['action_id'],
                                     client=connect(invocation.value))
        observers.append(observer)
        settlements.append((config, job, saved, digest))
        for index in range(COUNT):
            budget.check()
            row, claim, hook = observer._bind(index)
            budget.check()
            entries.append((row, lambda o=observer, r=row, c=claim, h=hook: o._live(r, c, h)))

    def current():
        manifest.current()
        if manifest.attempts() != attempts or manifest.resolve() != bindings:
            raise ValueError('declared run evidence changed')
        for config, job, saved, digest in settlements:
            if live_settlement(config, job) != (saved, digest):
                raise ValueError('original owner/route changed')
        for observer in observers:
            observer._current()

    value = observe_sweeps(entries, current, boot=manifest.plan['boot_id'], budget=budget)
    if directory_identity(directory) != identity:
        raise ValueError('output directory changed')
    record = {'version': 1, 'kind': 'standby_original_process_overlap',
              'run_id': manifest.plan['run_id'], 'plan_sha256': manifest.sha256,
              'boot_id': manifest.plan['boot_id'], 'bindings': bindings,
              'native_live_checks': True, **value}
    budget.check()
    write_once(directory / 'process-overlap.json', record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-directory', required=True)
    parser.add_argument('--output-directory', required=True)
    parser.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args()
    result = capture(args.run_directory, args.output_directory, seconds=args.seconds)
    print(json.dumps({k: result[k] for k in ('run_id', 'process_overlap_proven',
                                           'full_500_acceptance', 'overlap_seconds')}))


if __name__ == '__main__':
    main()
