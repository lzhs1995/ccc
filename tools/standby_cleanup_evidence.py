"""Read-only process/window evidence; never stop, signal or submit native work."""
from pathlib import Path
import os
import stat
import uuid
import ctypes
import sys

import ccc_guard_scope as scope
from ccc_batch_timing import stamp
from ccc_native_standby import write_once
from ccc_standby_timing import _elapsed, _read, _sha
from ccc_standby_settlement import read_settlement
import ccc_workspace_batch as batch
from ccc_native_standby import COUNT
from tools.standby_completion_evidence import ObservationBudget
import time


def tree_records(tree):
    """Reject partial trees instead of treating missing collections as empty."""
    def walk(node, level):
        if not isinstance(node, dict):
            raise ValueError('incomplete original window tree')
        if level:
            uuid.UUID(node['id'])
        if level == 4:
            return
        key = ('windows', 'workspaces', 'panes', 'surfaces')[level]
        children = node.get(key)
        if not isinstance(children, list):
            raise ValueError('incomplete original window tree')
        ids = []
        for child in children:
            walk(child, level + 1)
            ids.append(str(uuid.UUID(child['id'])))
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate original window identity')
    walk(tree, 0)
    return scope.records(tree)


def capture_baseline(directory, *, client, clock=stamp, seconds=30.0,
                     monotonic=time.monotonic):
    """Call before creating experimental sessions, not retrospectively.

    Stores the exact returned tree and recognized Codex inventory. Cross-checks
    each identity at the end; this is not an atomic OS snapshot or a claim that
    scan discovers every process. A future cleanup producer must check each
    experimental PID independently of this recognized-process inventory.
    """
    directory = Path(directory)
    info = directory.lstat()
    if (not directory.is_absolute() or directory.resolve(strict=True) != directory
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('private original evidence directory required')
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
    target = directory / 'cleanup-baseline.json'
    if os.path.lexists(target):
        raise ValueError('baseline already exists')
    budget = ObservationBudget(seconds, 1, monotonic)
    started = clock()
    if not started.get('boot_id'):
        raise ValueError('baseline boot identity unavailable')
    before_tree = client.tree()
    membership = tree_records(before_tree)
    budget.check()
    processes = scope.scan()
    if len({row['pid'] for row in processes}) != len(processes):
        raise ValueError('duplicate baseline process')
    for row in processes:
        budget.check()
        if row['surface_id'] not in membership or not scope.matches(row):
            raise ValueError('baseline process identity or membership unavailable')
    after_tree = client.tree()
    if tree_records(after_tree) != membership:
        raise ValueError('baseline window membership changed')
    for row in processes:
        budget.check()
        if not scope.matches(row):
            raise ValueError('baseline process changed during final observation')
    finished = clock()
    _elapsed(started, finished, started['boot_id'])
    current = directory.lstat()
    if (directory.resolve(strict=True) != directory or
            (current.st_dev, current.st_ino, current.st_mode, current.st_uid) != identity):
        raise ValueError('baseline directory changed')
    result = {'version': 1, 'kind': 'standby_cleanup_baseline',
              'started': started, 'finished': finished,
              'tree_before': before_tree, 'tree_after': after_tree,
              'processes': processes, 'job_terminal': False, 'run_terminal': False,
              'scope': 'recognized native identities before creation; non-atomic; no cleanup claim'}
    budget.check()
    write_once(target, result)
    return result


def pid_inventory():
    """Unfiltered Darwin PID inventory, independent of argv/environment matching."""
    native = scope.native
    if sys.platform != 'darwin' or native._proc_listpids is None:
        raise RuntimeError('native PID inventory unavailable')
    size = native._proc_listpids(1, 0, None, 0)
    if not 0 < size < 4 * 1024 * 1024:
        raise RuntimeError('native PID inventory size unavailable')
    values = (ctypes.c_int * ((size + 16384) // 4))()
    count = native._proc_listpids(1, 0, values, ctypes.sizeof(values))
    if count <= 0 or count >= ctypes.sizeof(values) or count % 4:
        raise RuntimeError('native PID inventory incomplete')
    return sorted(set(pid for pid in values[:count // 4] if pid > 0))


def capture_cleanup(config_path, job_id, baseline_path, directory, *, client,
                    clock=stamp, seconds=30.0, monotonic=time.monotonic):
    """Observe cleanup, without performing it; preserve failed observations too.

    Original experiment identities come from the settled activation. PID
    absence is checked independently of recognized Codex scans. Both passes
    must agree on cleanup and preservation; no OS-atomic guarantee is made.
    """
    directory = Path(directory)
    info = directory.lstat()
    if (not directory.is_absolute() or directory.resolve(strict=True) != directory
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('private original evidence directory required')
    target = directory / 'cleanup-observation.json'
    if os.path.lexists(target):
        raise ValueError('cleanup observation already exists')
    budget = ObservationBudget(seconds, 1, monotonic)
    baseline, baseline_raw = _read(baseline_path)
    saved, settlement_sha = read_settlement(config_path, job_id)
    ui_path = batch.job_path(Path(config_path), job_id).parent / 'standby' / 'activation-ui.json'
    ui, ui_raw = _read(ui_path)
    if (baseline.get('kind') != 'standby_cleanup_baseline' or baseline.get('version') != 1
            or _sha(ui_raw) != saved['activation_ui_sha256']):
        raise ValueError('original baseline or activation binding invalid')
    original_membership = tree_records(baseline['tree_after'])
    originals = ui['originals']
    if (len(originals) != COUNT or len({r['pid'] for r in originals}) != COUNT
            or len({r['surface_id'] for r in originals}) != COUNT
            or len({r['session_id'] for r in originals}) != COUNT):
        raise ValueError('complete unique experiment identities required')
    for row in originals:
        birth = row['birth']
        if (not isinstance(birth, list) or len(birth) != 2
                or any(type(v) is not int for v in birth)
                or birth[0] + birth[1] / 1e6 <= baseline['finished']['wall']
                or row['surface_id'] in original_membership
                or row['pid'] in {r['pid'] for r in baseline['processes']}):
            raise ValueError('baseline must precede experimental process creation')
    started = clock()
    _elapsed(baseline['started'], baseline['finished'], saved['boot_id'])
    _elapsed(baseline['finished'], started, saved['boot_id'])
    passes = []
    for _ in range(2):
        budget.check()
        tree = client.tree()
        membership = tree_records(tree)
        recognized = scope.scan()
        pids = pid_inventory()
        experiment = []
        for row in originals:
            budget.check()
            current = scope.birth(row['pid']) if row['pid'] in pids else None
            state = ('absent' if row['pid'] not in pids else
                     'unknown' if current is None else
                     'original_alive' if current == row['birth'] else 'pid_reused')
            experiment.append({'pid': row['pid'], 'original_birth': row['birth'],
                               'current_birth': current, 'state': state,
                               'surface_absent': row['surface_id'] not in membership})
        preserved = []
        for row in baseline['processes']:
            budget.check()
            preserved.append({'original': row, 'matches': scope.matches(row),
                'membership_matches': membership.get(row['surface_id']) ==
                                      original_membership.get(row['surface_id'])})
        passes.append({'tree': tree, 'recognized_processes': recognized, 'pids': pids,
                       'experiment': experiment, 'preserved': preserved})
    if (_read(baseline_path)[1] != baseline_raw or _read(ui_path)[1] != ui_raw
            or read_settlement(config_path, job_id) != (saved, settlement_sha)):
        raise ValueError('original cleanup binding changed')
    finished = clock()
    _elapsed(started, finished, saved['boot_id'])
    current = directory.lstat()
    if (directory.resolve(strict=True) != directory or
            (current.st_dev, current.st_ino, current.st_mode, current.st_uid) !=
            (info.st_dev, info.st_ino, info.st_mode, info.st_uid)):
        raise ValueError('cleanup directory changed')
    experiment_surfaces = {r['surface_id'] for r in originals}
    passed = all(not any(r['surface_id'] in experiment_surfaces
                         for r in p['recognized_processes']) and
                 all(r['state'] in ('absent', 'pid_reused') and r['surface_absent']
                         for r in p['experiment']) and
                 all(r['matches'] and r['membership_matches'] for r in p['preserved'])
                 for p in passes)
    result = {'version': 1, 'kind': 'standby_cleanup_observation', 'job_id': job_id,
              'action_id': saved['action_id'], 'settlement_sha256': settlement_sha,
              'baseline_path': str(baseline_path), 'baseline_sha256': _sha(baseline_raw),
              'activation_ui_sha256': _sha(ui_raw), 'started': started, 'finished': finished,
              'passes': passes, 'passed': passed, 'job_terminal': False, 'run_terminal': False,
              'scope': 'two non-atomic read-only samples; no task completion or resource closure claim'}
    budget.check()
    write_once(target, result)
    return result


def verify_cleanup(config_path, job_id, observation_path):
    """Recompute historical cleanup samples, never infer current OS liveness.

    Reopen the original baseline/UI/settlement and require complete sample
    coverage. Saved booleans alone cannot hide an omitted or still-live PID.
    """
    record, raw = _read(observation_path)
    saved, settlement_sha = read_settlement(config_path, job_id)
    ui_path = batch.job_path(Path(config_path), job_id).parent / 'standby' / 'activation-ui.json'
    ui, ui_raw = _read(ui_path)
    baseline_path = Path(record['baseline_path'])
    baseline, baseline_raw = _read(baseline_path)
    if (record.get('version') != 1 or record.get('kind') != 'standby_cleanup_observation'
            or record.get('job_id') != job_id or record.get('action_id') != saved['action_id']
            or record.get('settlement_sha256') != settlement_sha
            or record.get('activation_ui_sha256') != _sha(ui_raw)
            or saved['activation_ui_sha256'] != _sha(ui_raw)
            or record.get('baseline_sha256') != _sha(baseline_raw)
            or baseline.get('kind') != 'standby_cleanup_baseline' or baseline.get('version') != 1):
        raise ValueError('cleanup original binding invalid')
    membership = tree_records(baseline['tree_after'])
    if tree_records(baseline['tree_before']) != membership:
        raise ValueError('baseline membership inconsistent')
    originals = ui['originals']
    baseline_rows = baseline['processes']
    if (len(originals) != COUNT or any(len({r[k] for r in originals}) != COUNT
            for k in ('pid', 'surface_id', 'session_id'))
            or len({r['pid'] for r in baseline_rows}) != len(baseline_rows)):
        raise ValueError('cleanup identity coverage incomplete')
    for row in originals:
        birth = row['birth']
        if (not isinstance(birth, list) or len(birth) != 2
                or any(type(v) is not int for v in birth)
                or birth[0] + birth[1] / 1e6 <= baseline['finished']['wall']
                or row['surface_id'] in membership
                or row['pid'] in {r['pid'] for r in baseline_rows}):
            raise ValueError('cleanup baseline did not precede creation')
    for start, end in ((baseline['started'], baseline['finished']),
                       (baseline['finished'], record['started']),
                       (record['started'], record['finished'])):
        _elapsed(start, end, saved['boot_id'])
    passes = record.get('passes')
    if not isinstance(passes, list) or len(passes) != 2:
        raise ValueError('two complete cleanup samples required')
    for sample in passes:
        current_membership = tree_records(sample['tree'])
        pids = sample['pids']
        if (not isinstance(pids, list) or any(type(p) is not int or p <= 0 for p in pids)
                or len(pids) != len(set(pids))
                or len(sample['experiment']) != COUNT
                or len(sample['preserved']) != len(baseline_rows)):
            raise ValueError('cleanup sample coverage incomplete')
        for original, observed in zip(originals, sample['experiment']):
            current = observed['current_birth']
            state = ('absent' if original['pid'] not in pids else
                     'unknown' if current is None else
                     'original_alive' if current == original['birth'] else 'pid_reused')
            if (current is not None and (not isinstance(current, list) or len(current) != 2
                    or any(type(v) is not int for v in current))):
                raise ValueError('invalid sampled process birth')
            if (observed['pid'] != original['pid'] or observed['original_birth'] != original['birth']
                    or observed['state'] != state or state not in ('absent', 'pid_reused')
                    or (state == 'absent' and current is not None)
                    or observed['surface_absent'] is not True
                    or original['surface_id'] in current_membership):
                raise ValueError('experimental process cleanup not established')
        for original, observed in zip(baseline_rows, sample['preserved']):
            if (observed['original'] != original or observed['matches'] is not True
                    or observed['membership_matches'] is not True
                    or original['surface_id'] not in membership
                    or current_membership.get(original['surface_id']) != membership[original['surface_id']]):
                raise ValueError('original process preservation not established')
        if any(r['surface_id'] in {o['surface_id'] for o in originals}
               for r in sample['recognized_processes']):
            raise ValueError('experimental process remains recognized')
    if (record.get('passed') is not True or _read(observation_path)[1] != raw
            or _read(baseline_path)[1] != baseline_raw or _read(ui_path)[1] != ui_raw
            or read_settlement(config_path, job_id) != (saved, settlement_sha)):
        raise ValueError('cleanup originals changed or failed')
    return {'observation_sha256': _sha(raw), 'started': record['started'],
            'finished': record['finished'], 'scope': 'historical two-sample cleanup only'}
