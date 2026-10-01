"""Join original completion, owner closure and fresh cleanup into a job terminal.

This producer never closes an owner or native. A successful fixture terminal
is not a latency/SLA result, nor proof of simultaneous sessions across jobs.
"""
from pathlib import Path
import os
import json

from ccc_batch_timing import stamp
from ccc_native_standby import write_once
from ccc_standby_settlement import read_settlement
from ccc_standby_timing import _elapsed, _read, _sha
import ccc_workspace_batch as batch
from tools.standby_completion_evidence import verify_capture
from tools.standby_cleanup_evidence import capture_cleanup, verify_cleanup
from tools.standby_run_manifest import directory_identity


def runner_evidence(directory, config_path, job_id, settlement, *, require_success=True):
    directory = Path(directory)
    values, pinned = {}, []
    for name in ('intent', 'open', 'closed'):
        path = directory / f'runner-{name}.json'
        value, raw = _read(path)
        if value.get('kind') != f'standby_runner_{name}' or value.get('version') != 1:
            raise ValueError('original runner record required')
        values[name] = value
        pinned.append((path, raw))
    intent, opened, closed = (values[k] for k in ('intent', 'open', 'closed'))
    if (not intent.get('invocation_id') or any(v.get('invocation_id') != intent['invocation_id']
            for v in (opened, closed))
            or not intent.get('invocation_sha256')
            or opened.get('invocation_sha256') != intent['invocation_sha256']
            or opened.get('config_path') != str(config_path)
            or opened.get('job_path') != str(batch.job_path(Path(config_path), job_id))
            or any(opened.get(k) != settlement[k] for k in
                   ('job_id', 'cohort_id', 'workspace_id', 'mode', 'boot_id'))
            or any(intent.get(k) != settlement[k] for k in ('workspace_id', 'mode', 'boot_id'))):
        raise ValueError('runner belongs to different original job')
    started = {'boot_id': intent['boot_id'], 'wall': intent['started_at'],
               'monotonic': intent['started_monotonic']}
    finished = {'boot_id': intent['boot_id'], 'wall': closed['closed_at'],
                'monotonic': closed['closed_monotonic']}
    _elapsed(started, finished, settlement['boot_id'])
    reports = closed.get('communication_resources', {})
    if (closed.get('handles_closed') is not True or closed.get('close_error_type') is not None
            or (require_success and closed.get('error_type') is not None)
            or closed.get('reason') not in ('cancelled', 'service_closed', 'lifetime_expired',
                                          'failed', 'service_failed', 'service_cancelled')
            or (require_success and closed.get('reason') in ('failed', 'service_failed'))
            or set(reports) != {'routes', 'owner_endpoint'}
            or any(r.get('resources_released') is not True for r in reports.values())
            or closed.get('source_resources', {}).get('resources_released') is not True
            or closed.get('all_resources_released') is not True):
        raise ValueError('original runner resource closure not confirmed')
    return finished, pinned


def capture(config_path, job_id, directory, *, runner_directory, completion_path,
            baseline_path, client, clock=stamp):
    """Settle an activated job; success requires verified completion evidence.

    With completion_path=None, derive a non-success outcome from original closure.
    No outcome authorizes interrupting or killing native work.
    """
    directory = Path(directory)
    identity = directory_identity(directory)
    target = directory / 'job-terminal.json'
    if os.path.lexists(target):
        raise ValueError('job terminal already exists')
    saved, settlement_sha = read_settlement(config_path, job_id)
    closed_at, pinned = runner_evidence(runner_directory, config_path, job_id, saved,
                                         require_success=completion_path is not None)
    _, baseline_raw = _read(baseline_path)
    pinned.append((Path(baseline_path), baseline_raw))
    verified = None
    outcome = 'succeeded'
    if completion_path is not None:
        completion, completion_raw = _read(completion_path)
        verified = verify_capture(config_path, job_id, completion_path, clock=clock)
        if verified['observation_sha256'] != _sha(completion_raw):
            raise ValueError('completion changed during terminal collection')
        _elapsed(completion['finished'], closed_at, saved['boot_id'])
    else:
        closed = json.loads(pinned[2][1])
        reason = closed['reason']
        outcome = ('failed' if closed.get('error_type') or reason in ('failed', 'service_failed')
                   else 'timed_out' if reason == 'lifetime_expired'
                   else 'cancelled' if reason in ('cancelled', 'service_cancelled')
                   else 'incomplete')
    cleanup = capture_cleanup(config_path, job_id, baseline_path, directory,
                              client=client, clock=clock)
    if cleanup['passed'] is not True:
        raise ValueError('cleanup failed; original observation retained')
    _elapsed(closed_at, cleanup['started'], saved['boot_id'])
    cleanup_path = directory / 'cleanup-observation.json'
    cleanup_saved, cleanup_raw = _read(cleanup_path)
    if cleanup_saved != cleanup:
        raise ValueError('cleanup observation changed')
    # Re-evaluate original transcripts after the potentially slow cleanup scan.
    last = (verify_capture(config_path, job_id, completion_path, clock=clock)
            if completion_path is not None else None)
    if ((verified is not None and last['observation_sha256'] != verified['observation_sha256'])
            or read_settlement(config_path, job_id) != (saved, settlement_sha)
            or any(_read(path)[1] != raw for path, raw in pinned)
            or _read(cleanup_path)[1] != cleanup_raw):
        raise ValueError('terminal originals changed during collection')
    now = clock()
    _elapsed(cleanup['finished'], now, saved['boot_id'])
    if directory_identity(directory) != identity:
        raise ValueError('terminal directory changed')
    result = {'version': 1, 'kind': 'standby_job_terminal',
        **{k: saved[k] for k in ('job_id', 'action_id', 'cohort_id', 'workspace_id', 'boot_id', 'mode')},
        'outcome': outcome, 'job_terminal': True, 'run_terminal': False,
        'settlement_sha256': settlement_sha, 'observed': now,
        'runner_directory': str(runner_directory), 'baseline_path': str(baseline_path),
        'completion': verified, 'cleanup_path': str(cleanup_path),
        'cleanup_sha256': _sha(cleanup_raw),
        'closure_originals': {str(path): _sha(raw) for path, raw in pinned},
        'scope': 'activated job outcome and sampled cleanup/resource closure; no latency or simultaneous-fleet claim'}
    write_once(target, result)
    return result


def verify(config_path, job_id, terminal_path, *, clock=stamp):
    """Reopen all job-terminal originals; no live work or cleanup is initiated."""
    result, raw = _read(terminal_path)
    saved, settlement_sha = read_settlement(config_path, job_id)
    if (result.get('version') != 1 or result.get('kind') != 'standby_job_terminal'
            or result.get('job_terminal') is not True or result.get('run_terminal') is not False
            or result.get('settlement_sha256') != settlement_sha
            or any(result.get(k) != saved[k] for k in
                   ('job_id', 'action_id', 'cohort_id', 'workspace_id', 'boot_id', 'mode'))):
        raise ValueError('terminal original job binding invalid')
    success = result['outcome'] == 'succeeded'
    closed_at, pinned = runner_evidence(result['runner_directory'], config_path, job_id,
                                         saved, require_success=success)
    _, baseline_raw = _read(result['baseline_path'])
    pinned.append((Path(result['baseline_path']), baseline_raw))
    if result.get('closure_originals') != {str(p): _sha(b) for p, b in pinned}:
        raise ValueError('terminal closure originals changed')
    if success:
        completion = result['completion']
        fresh = verify_capture(config_path, job_id, completion['observation_path'], clock=clock)
        if ({k: v for k, v in fresh.items() if k != 'verified'} !=
                {k: v for k, v in completion.items() if k != 'verified'}):
            raise ValueError('terminal completion changed')
        observed, _ = _read(completion['observation_path'])
        _elapsed(observed['finished'], closed_at, saved['boot_id'])
    else:
        closed = json.loads(pinned[2][1])
        reason = closed['reason']
        outcome = ('failed' if closed.get('error_type') or reason in ('failed', 'service_failed')
                   else 'timed_out' if reason == 'lifetime_expired'
                   else 'cancelled' if reason in ('cancelled', 'service_cancelled') else 'incomplete')
        if result['outcome'] != outcome or result.get('completion') is not None:
            raise ValueError('terminal non-success outcome differs from original runner')
    cleanup = verify_cleanup(config_path, job_id, result['cleanup_path'])
    cleanup_record, cleanup_raw = _read(result['cleanup_path'])
    if (cleanup['observation_sha256'] != result['cleanup_sha256']
            or _sha(cleanup_raw) != result['cleanup_sha256']
            or cleanup_record['baseline_path'] != result['baseline_path']):
        raise ValueError('terminal cleanup binding changed')
    _elapsed(closed_at, cleanup['started'], saved['boot_id'])
    _elapsed(cleanup['finished'], result['observed'], saved['boot_id'])
    _elapsed(result['observed'], clock(), saved['boot_id'])
    # Cleanup verification may be slow. Re-evaluate the transcript originals,
    # not just the saved observation hash, after that observation window.
    if success:
        last = verify_capture(config_path, job_id, completion['observation_path'], clock=clock)
        if ({k: v for k, v in last.items() if k != 'verified'} !=
                {k: v for k, v in completion.items() if k != 'verified'}):
            raise ValueError('terminal completion changed during cleanup verification')
    if (_read(terminal_path)[1] != raw or any(_read(p)[1] != b for p, b in pinned)
            or _read(result['cleanup_path'])[1] != cleanup_raw
            or read_settlement(config_path, job_id) != (saved, settlement_sha)):
        raise ValueError('terminal originals changed during verification')
    return {'terminal_path': str(terminal_path), 'terminal_sha256': _sha(raw),
            'job_id': job_id, 'outcome': result['outcome'], 'job_terminal': True,
            'run_terminal': False}
