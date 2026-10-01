"""Settle a registered, unactivated attempt using original cleanup evidence.

No input or process operation is performed. Missing runner admission records
remain unresolved; absence of a record never establishes resource cleanup.
"""
from pathlib import Path
import os

from ccc_batch_timing import stamp
from ccc_native_standby import identifier, write_once
from ccc_standby_timing import _elapsed, _read, _sha
from tools.standby_run_manifest import RunManifest
from tools.standby_preparation_cleanup import verify as verify_cleanup


def evidence(manifest, batch_id, config_path, job_id, cleanup_path):
    batch_id = identifier(batch_id)
    manifest.current()
    expected = next((r for r in manifest.plan['batches']
                     if identifier(r['batch_id']) == batch_id), None)
    if expected is None:
        raise ValueError('attempt was not declared')
    path, attempt, raw, invocation = manifest._attempt(expected)
    binding = manifest.directory / f'batch-{batch_id}.json'
    if os.path.lexists(binding):
        raise ValueError('activated batch requires job terminal')
    cleanup, cleanup_raw = _read(cleanup_path)
    if cleanup.get('runner_directory') != attempt['runner_directory']:
        raise ValueError('cleanup belongs to another runner')
    verified = verify_cleanup(config_path, job_id, cleanup_path)
    if verified['observation_sha256'] != _sha(cleanup_raw):
        raise ValueError('cleanup changed during verification')
    runner = Path(attempt['runner_directory'])
    intent, intent_raw = _read(runner / 'runner-intent.json')
    closed, closed_raw = _read(runner / 'runner-closed.json')
    # Cleanup verification validates the open/closed resource chain; bind its
    # intent to the invocation registered before any runner work began.
    if (intent.get('invocation_id') != attempt['invocation_id']
            or intent.get('invocation_sha256') != attempt['invocation_sha256']
            or any(intent.get(k) != attempt[k] for k in ('workspace_id', 'mode'))
            or intent.get('boot_id') != manifest.plan['boot_id']):
        raise ValueError('runner differs from registered invocation')
    started = dict(boot_id=intent['boot_id'], wall=intent['started_at'],
                   monotonic=intent['started_monotonic'])
    _elapsed(attempt['registered'], started, manifest.plan['boot_id'])
    reason = closed['reason']
    if reason not in ('failed', 'service_failed', 'lifetime_expired', 'cancelled',
                      'service_cancelled', 'service_closed'):
        raise ValueError('unknown original runner outcome')
    outcome = ('failed' if closed.get('error_type') or reason in ('failed', 'service_failed')
               else 'timed_out' if reason == 'lifetime_expired'
               else 'cancelled' if reason in ('cancelled', 'service_cancelled')
               else 'incomplete')
    if verify_cleanup(config_path, job_id, cleanup_path) != verified:
        raise ValueError('cleanup evidence changed')
    if (manifest._attempt(expected)[2] != raw
            or _read(cleanup_path)[1] != cleanup_raw
            or _read(runner / 'runner-intent.json')[1] != intent_raw
            or _read(runner / 'runner-closed.json')[1] != closed_raw
            or os.path.lexists(binding)):
        raise ValueError('attempt originals changed')
    invocation.current()
    manifest.current()
    return dict(version=1, kind='standby_attempt_terminal', run_id=manifest.plan['run_id'],
        plan_sha256=manifest.sha256, batch_id=batch_id, job_id=job_id,
        config_path=str(config_path), attempt_path=str(path), attempt_sha256=_sha(raw),
        cleanup_path=str(cleanup_path), cleanup_sha256=_sha(cleanup_raw),
        runner_intent_sha256=_sha(intent_raw), runner_closed_sha256=_sha(closed_raw),
        outcome=outcome, attempt_terminal=True, job_terminal=False, run_terminal=False,
        cleanup_finished=verified['finished'])


def capture(run_directory, batch_id, config_path, job_id, cleanup_path, *, clock=stamp):
    manifest = RunManifest(run_directory)
    target = manifest.directory / f'terminal-attempt-{identifier(batch_id)}.json'
    if os.path.lexists(target):
        raise ValueError('attempt terminal already exists')
    result = evidence(manifest, batch_id, config_path, job_id, cleanup_path)
    now = clock()
    _elapsed(result['cleanup_finished'], now, manifest.plan['boot_id'])
    result['observed'] = now
    write_once(target, result)
    return result


def verify(run_directory, batch_id, *, clock=stamp):
    manifest = RunManifest(run_directory)
    target = manifest.directory / f'terminal-attempt-{identifier(batch_id)}.json'
    result, raw = _read(target)
    fresh = evidence(manifest, batch_id, result['config_path'], result['job_id'],
                     result['cleanup_path'])
    if {k: v for k, v in result.items() if k != 'observed'} != fresh:
        raise ValueError('attempt terminal differs from original evidence')
    _elapsed(fresh['cleanup_finished'], result['observed'], manifest.plan['boot_id'])
    _elapsed(result['observed'], clock(), manifest.plan['boot_id'])
    if _read(target)[1] != raw:
        raise ValueError('attempt terminal changed')
    return dict(terminal_path=str(target), terminal_sha256=_sha(raw),
                batch_id=identifier(batch_id), outcome=fresh['outcome'],
                attempt_terminal=True, run_terminal=False)
