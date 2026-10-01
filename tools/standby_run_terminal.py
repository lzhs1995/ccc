"""Aggregate all predeclared attempts; completion is distinct from success.

Revalidates originals without controlling sessions. This does not establish
simultaneous liveness, zero model requests or the latency acceptance criteria.
"""
from pathlib import Path
import os

from ccc_batch_timing import stamp
from ccc_native_standby import identifier, write_once
from ccc_standby_timing import _elapsed, _read, _sha
from tools.standby_run_manifest import RunManifest
from tools import standby_attempt_terminal as attempts
from tools import standby_job_terminal as jobs


def collect(manifest, terminals, *, clock=stamp):
    registered = manifest.attempts()
    expected_ids = {identifier(r['batch_id']) for r in manifest.plan['batches']}
    if not isinstance(terminals, dict) or set(terminals) != expected_ids:
        raise ValueError('terminal paths must cover every declared batch exactly')
    paths = [Path(p) for p in terminals.values()]
    if len(set(paths)) != len(paths):
        raise ValueError('terminal cannot be reused by another batch')
    rows, pinned, bindings = [], [], set()
    seen = {k: set() for k in ('job_id', 'action_id', 'cohort_id')}
    for expected, registered_row in zip(manifest.plan['batches'], registered):
        bid = identifier(expected['batch_id'])
        path = Path(terminals[bid])
        terminal, raw = _read(path)
        attempt = registered_row['attempt']
        binding_path = manifest.directory / f'batch-{bid}.json'
        if terminal.get('kind') == 'standby_attempt_terminal':
            required = manifest.directory / f'terminal-attempt-{bid}.json'
            if path != required:
                raise ValueError('original attempt terminal path required')
            proof = attempts.verify(manifest.directory, bid, clock=clock)
        elif terminal.get('kind') == 'standby_job_terminal':
            bindings.add(binding_path)
            binding, binding_raw = manifest._binding_record(binding_path, expected)
            manifest._original_binding(binding)
            if (terminal.get('runner_directory') != attempt['runner_directory']
                    or any(terminal.get(k) != binding[k] for k in
                           ('job_id', 'action_id', 'cohort_id', 'workspace_id', 'mode', 'boot_id'))):
                raise ValueError('job terminal differs from registered batch')
            intent_path = Path(attempt['runner_directory']) / 'runner-intent.json'
            intent, intent_raw = _read(intent_path)
            if (intent.get('invocation_id') != attempt['invocation_id']
                    or intent.get('invocation_sha256') != attempt['invocation_sha256']):
                raise ValueError('job runner differs from registered invocation')
            started = dict(boot_id=intent['boot_id'], wall=intent['started_at'],
                           monotonic=intent['started_monotonic'])
            _elapsed(attempt['registered'], started, manifest.plan['boot_id'])
            proof = jobs.verify(binding['config_path'], binding['job_id'], path, clock=clock)
            pinned.extend([(binding_path, binding_raw), (intent_path, intent_raw)])
        else:
            raise ValueError('original job or attempt terminal required')
        if proof['terminal_sha256'] != _sha(raw):
            raise ValueError('terminal changed during original verification')
        for key, values in seen.items():
            if key in terminal:
                value = identifier(terminal[key])
                if value in values:
                    raise ValueError('original identity reused by another batch')
                values.add(value)
        _elapsed(terminal['observed'], clock(), manifest.plan['boot_id'])
        pinned.append((path, raw))
        rows.append(dict(batch_id=bid, attempt_sha256=registered_row['attempt_sha256'],
                         terminal_path=str(path), terminal_sha256=_sha(raw),
                         outcome=proof['outcome'], activated=terminal['kind']=='standby_job_terminal'))
    if set(manifest.directory.glob('batch-*.json')) != bindings:
        raise ValueError('activation binding set differs from terminal coverage')
    if (manifest.attempts() != registered
            or any(_read(p)[1] != raw for p, raw in pinned)):
        raise ValueError('run originals changed during collection')
    manifest.current()
    counts = {k: sum(r['outcome'] == k for r in rows)
              for k in ('succeeded', 'failed', 'timed_out', 'cancelled', 'incomplete')}
    if sum(counts.values()) != len(rows):
        raise ValueError('unknown batch outcome')
    return dict(version=1, kind='standby_run_terminal', run_id=manifest.plan['run_id'],
        boot_id=manifest.plan['boot_id'], plan_sha256=manifest.sha256,
        planned_sessions=manifest.plan['planned_sessions'],
        workspace_slots=manifest.plan['workspace_slots'], batches=rows, outcomes=counts,
        run_terminal=True, succeeded=counts['succeeded']==len(rows),
        scope='complete declared attempt outcomes; no simultaneous-liveness or latency claim')


def capture(directory, terminals, *, clock=stamp):
    manifest = RunManifest(directory)
    target = manifest.directory / 'run-terminal.json'
    if os.path.lexists(target):
        raise ValueError('run terminal already exists')
    result = collect(manifest, terminals, clock=clock)
    # Reopen terminal dependencies after the whole first pass, not just JSON
    # hashes. Transcripts or cleanup originals may change during aggregation.
    if collect(manifest, terminals, clock=clock) != result:
        raise ValueError('run evidence changed between passes')
    result['observed'] = clock()
    _elapsed(manifest.plan['declared'], result['observed'], manifest.plan['boot_id'])
    write_once(target, result)
    return result


def verify(directory, *, clock=stamp):
    manifest = RunManifest(directory)
    path = manifest.directory / 'run-terminal.json'
    result, raw = _read(path)
    rows = result['batches']
    terminals = {r['batch_id']: r['terminal_path'] for r in rows}
    if len(terminals) != len(rows):
        raise ValueError('duplicate batch in run terminal')
    for _ in range(2):
        fresh = collect(manifest, terminals, clock=clock)
        if {k: v for k, v in result.items() if k != 'observed'} != fresh:
            raise ValueError('run terminal differs from original evidence')
    for terminal in terminals.values():
        child, _ = _read(terminal)
        _elapsed(child['observed'], result['observed'], manifest.plan['boot_id'])
    _elapsed(result['observed'], clock(), manifest.plan['boot_id'])
    if _read(path)[1] != raw:
        raise ValueError('run terminal changed')
    return dict(terminal_path=str(path), terminal_sha256=_sha(raw),
                run_id=manifest.plan['run_id'], run_terminal=True,
                succeeded=result['succeeded'], outcomes=result['outcomes'])
