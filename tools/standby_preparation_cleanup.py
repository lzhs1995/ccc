"""Observe closed, unactivated preparations; never terminate native work."""
import os
from pathlib import Path
import time

from ccc_batch_timing import stamp
from ccc_native_standby import write_once
from ccc_standby_timing import _elapsed, _read, _sha
import ccc_standby_launch as launch
import ccc_workspace_batch as batch
from tools import standby_cleanup_evidence as cleanup
from tools.standby_completion_evidence import ObservationBudget
from tools.standby_job_terminal import runner_evidence
from tools.standby_preparation_inventory import inventory
from tools.standby_run_manifest import directory_identity


def capture(config_path, job_id, directory, *, runner_directory, preparation_directory,
            baseline_path, client, clock=stamp, seconds=30.0, monotonic=time.monotonic):
    directory = Path(directory)
    identity = directory_identity(directory)
    target = directory / 'preparation-cleanup.json'
    if os.path.lexists(target):
        raise ValueError('preparation cleanup already recorded')
    budget = ObservationBudget(seconds, 1, monotonic)
    job_path = batch.job_path(Path(config_path), job_id)
    job, job_raw = _read(job_path)
    selected = launch.policy(job, config_path)
    closed_at, pinned = runner_evidence(runner_directory, config_path, job_id,
                                        selected, require_success=False)
    opened, _ = _read(Path(runner_directory)/'runner-open.json')
    spec = Path(opened['owner_spec_path'])
    if spec.parent != Path(preparation_directory):
        raise ValueError('preparation directory differs from original owner')
    _, spec_raw = _read(spec)
    if _sha(spec_raw) != opened['owner_spec_sha256']:
        raise ValueError('original owner spec changed')
    baseline, baseline_raw = _read(baseline_path)
    if baseline.get('kind') != 'standby_cleanup_baseline' or baseline.get('version') != 1:
        raise ValueError('original cleanup baseline required')
    membership = cleanup.tree_records(baseline['tree_after'])
    if cleanup.tree_records(baseline['tree_before']) != membership:
        raise ValueError('baseline membership differs')
    intent, _ = _read(Path(runner_directory)/'runner-intent.json')
    start = {'boot_id': selected['boot_id'], 'wall': intent['started_at'],
             'monotonic': intent['started_monotonic']}
    _elapsed(baseline['started'], baseline['finished'], selected['boot_id'])
    _elapsed(baseline['finished'], start, selected['boot_id'])
    activation = job_path.parent/'standby'/'activation-ui.json'
    activation_attempt = job_path.parent/'standby'/'activation-attempt.json'
    if any(os.path.lexists(p) for p in (activation, activation_attempt)):
        raise ValueError('activated preparation requires job completion path')
    original = inventory(config_path, job_id, preparation_directory)
    known = [r for r in original['rows'] if r['state'] == 'identified']
    for row in known:
        pid, sec, usec = row['process_identity']
        if (sec + usec / 1e6 <= baseline['finished']['wall']
                or row['surface_id'] in membership
                or pid in {r['pid'] for r in baseline['processes']}):
            raise ValueError('baseline does not precede preparation identities')
    started = clock()
    _elapsed(closed_at, started, selected['boot_id'])
    passes = []
    for _ in range(2):
        budget.check()
        tree = client.tree()
        current_membership = cleanup.tree_records(tree)
        recognized = cleanup.scope.scan()
        pids = cleanup.pid_inventory()
        rows = []
        for row in known:
            budget.check()
            pid, *birth = row['process_identity']
            current = cleanup.scope.birth(pid) if pid in pids else None
            state = ('absent' if pid not in pids else 'unknown' if current is None
                     else 'original_alive' if current == birth else 'pid_reused')
            rows.append({'index': row['index'], 'pid': pid, 'original_birth': birth,
                         'current_birth': current, 'state': state,
                         'surface_absent': row['surface_id'] not in current_membership})
        preserved = []
        for row in baseline['processes']:
            budget.check()
            preserved.append({'original': row, 'matches': cleanup.scope.matches(row),
                'membership_matches': current_membership.get(row['surface_id']) ==
                                      membership.get(row['surface_id'])})
        passes.append({'tree': tree, 'recognized_processes': recognized, 'pids': pids,
                       'experiment': rows, 'preserved': preserved})
    pinned += [(job_path, job_raw), (Path(baseline_path), baseline_raw), (spec, spec_raw)]
    if (any(_read(p)[1] != b for p, b in pinned)
            or inventory(config_path, job_id, preparation_directory) != original
            or any(os.path.lexists(p) for p in (activation, activation_attempt))
            or directory_identity(directory) != identity):
        raise ValueError('preparation cleanup originals changed')
    finished = clock()
    _elapsed(started, finished, selected['boot_id'])
    unresolved = [r['index'] for r in original['rows']
                  if r['state'] in ('unknown_ack', 'unknown_process')]
    surfaces = {r['surface_id'] for r in known}
    passed = not unresolved and all(
        all(r['state'] in ('absent', 'pid_reused') and r['surface_absent'] for r in p['experiment'])
        and not any(r['surface_id'] in surfaces for r in p['recognized_processes'])
        and all(r['matches'] and r['membership_matches'] for r in p['preserved']) for p in passes)
    result = {'version': 1, 'kind': 'standby_preparation_cleanup', 'job_id': job_id,
        'baseline_path': str(baseline_path), 'runner_directory': str(runner_directory),
        'preparation_directory': str(preparation_directory), 'inventory': original,
        'originals': {str(p): _sha(b) for p, b in pinned},
        'started': started, 'finished': finished, 'passes': passes,
        'unresolved_slots': unresolved, 'passed': passed,
        'job_terminal': False, 'run_terminal': False,
        'scope': 'closed unactivated preparation; two non-atomic cleanup samples only'}
    budget.check()
    write_once(target, result)
    return result


def verify(config_path, job_id, observation_path):
    """Recompute historical samples and reopen originals; no live observation."""
    record, raw = _read(observation_path)
    if (record.get('version') != 1 or record.get('kind') != 'standby_preparation_cleanup'
            or record.get('job_id') != job_id or record.get('passed') is not True
            or record.get('job_terminal') is not False or record.get('run_terminal') is not False):
        raise ValueError('successful original preparation cleanup required')
    job_path = batch.job_path(Path(config_path), job_id)
    job, job_raw = _read(job_path)
    selected = launch.policy(job, config_path)
    runner = Path(record['runner_directory'])
    closed_at, pinned = runner_evidence(runner, config_path, job_id,
                                        selected, require_success=False)
    opened, _ = _read(runner/'runner-open.json')
    spec = Path(opened['owner_spec_path'])
    _, spec_raw = _read(spec)
    if (spec.parent != Path(record['preparation_directory'])
            or _sha(spec_raw) != opened['owner_spec_sha256']):
        raise ValueError('preparation owner binding changed')
    baseline_path = Path(record['baseline_path'])
    baseline, baseline_raw = _read(baseline_path)
    pinned += [(job_path, job_raw), (baseline_path, baseline_raw), (spec, spec_raw)]
    if record.get('originals') != {str(p): _sha(b) for p, b in pinned}:
        raise ValueError('preparation cleanup original hashes changed')
    if baseline.get('version') != 1 or baseline.get('kind') != 'standby_cleanup_baseline':
        raise ValueError('original cleanup baseline required')
    membership = cleanup.tree_records(baseline['tree_after'])
    if cleanup.tree_records(baseline['tree_before']) != membership:
        raise ValueError('baseline membership differs')
    baseline_rows = baseline['processes']
    if len({r['pid'] for r in baseline_rows}) != len(baseline_rows):
        raise ValueError('duplicate baseline process')
    intent, _ = _read(runner/'runner-intent.json')
    start = {'boot_id': selected['boot_id'], 'wall': intent['started_at'],
             'monotonic': intent['started_monotonic']}
    for first, last in ((baseline['started'], baseline['finished']),
                        (baseline['finished'], start), (closed_at, record['started']),
                        (record['started'], record['finished'])):
        _elapsed(first, last, selected['boot_id'])
    original = inventory(config_path, job_id, record['preparation_directory'])
    if (original != record['inventory'] or record.get('unresolved_slots') != []
            or any(r['state'] in ('unknown_ack', 'unknown_process') for r in original['rows'])):
        raise ValueError('preparation inventory unresolved or changed')
    known = [r for r in original['rows'] if r['state'] == 'identified']
    for row in known:
        pid, sec, usec = row['process_identity']
        if (sec + usec / 1e6 <= baseline['finished']['wall']
                or row['surface_id'] in membership or pid in {r['pid'] for r in baseline_rows}):
            raise ValueError('baseline does not precede preparation identities')
    samples = record.get('passes')
    if not isinstance(samples, list) or len(samples) != 2:
        raise ValueError('two complete preparation cleanup samples required')
    for sample in samples:
        current_membership = cleanup.tree_records(sample['tree'])
        pids = sample['pids']
        if (not isinstance(pids, list) or any(type(p) is not int or p <= 0 for p in pids)
                or len(set(pids)) != len(pids) or len(sample['experiment']) != len(known)
                or len(sample['preserved']) != len(baseline_rows)):
            raise ValueError('preparation cleanup coverage incomplete')
        for row, observed in zip(known, sample['experiment']):
            pid, *birth = row['process_identity']
            current = observed['current_birth']
            if current is not None and (not isinstance(current, list) or len(current) != 2
                    or any(type(v) is not int or v < 0 for v in current)):
                raise ValueError('invalid preparation process birth')
            state = ('absent' if pid not in pids else 'unknown' if current is None
                     else 'original_alive' if current == birth else 'pid_reused')
            if (observed['index'] != row['index'] or observed['pid'] != pid
                    or observed['original_birth'] != birth or observed['state'] != state
                    or state not in ('absent', 'pid_reused')
                    or (state == 'absent' and current is not None)
                    or observed['surface_absent'] is not True
                    or row['surface_id'] in current_membership):
                raise ValueError('preparation process cleanup not established')
        for row, observed in zip(baseline_rows, sample['preserved']):
            if (observed['original'] != row or observed['matches'] is not True
                    or observed['membership_matches'] is not True
                    or row['surface_id'] not in membership
                    or current_membership.get(row['surface_id']) != membership[row['surface_id']]):
                raise ValueError('original process preservation not established')
        if any(r['surface_id'] in {k['surface_id'] for k in known}
               for r in sample['recognized_processes']):
            raise ValueError('preparation process remains recognized')
    if (any(os.path.lexists(job_path.parent/'standby'/name)
            for name in ('activation-ui.json', 'activation-attempt.json'))
            or any(_read(p)[1] != b for p, b in pinned)
            or inventory(config_path, job_id, record['preparation_directory']) != original
            or _read(observation_path)[1] != raw):
        raise ValueError('preparation cleanup originals changed')
    return {'observation_path': str(observation_path), 'observation_sha256': _sha(raw),
            'job_id': job_id, 'started': record['started'], 'finished': record['finished'],
            'scope': 'historical two-sample preparation cleanup only'}
