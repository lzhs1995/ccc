"""Read original live standby task completions; never send, stop or close routes.

This is the bounded-loopback acceptance policy (N failures then final OK), not
an inference that arbitrary user work is complete. The observation is a
prerequisite to an independent job/run terminal, not that terminal itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import math
import time

import cmux_codex_watch as core
import ccc_workspace_batch as batch
from ccc_batch_timing import stamp
from ccc_native_standby import COUNT, write_once
from ccc_standby_acceptance import FirstTaskObserver
from ccc_standby_settlement import live_settlement, read_settlement
from ccc_standby_timing import _elapsed, _read, _sha
from tools.native_acceptance_metrics import evaluate_native_completion

TRANSCRIPT_LIMIT = 64 * 1024 * 1024
TOTAL_TRANSCRIPT_LIMIT = 256 * 1024 * 1024
OBSERVATION_SECONDS = 30.0


class ObservationBudget:
    """Cooperative deadline: reject late results, never interrupt native work.

    This cannot preempt an OS read or another helper's call. Those retain
    their own bounds; checks bracket each slot and the final validation.
    """
    def __init__(self, seconds, byte_limit, monotonic):
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds <= 0
                or type(byte_limit) is not int or byte_limit <= 0):
            raise ValueError('positive finite observation budgets required')
        self.monotonic = monotonic
        self.started = monotonic()
        self.seconds, self.byte_limit, self.bytes = seconds, byte_limit, 0

    def check(self):
        elapsed = self.monotonic() - self.started
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed >= self.seconds:
            raise ValueError('completion observation deadline exceeded')

    def reserve(self, size):
        self.check()
        if size > self.byte_limit - self.bytes:
            raise ValueError('completion observation total transcript budget exceeded')
        self.bytes += size


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_transcript(path, *, budget=None):
    """One complete, stable, bounded original transcript, with exact byte hash."""
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('original transcript path required')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > TRANSCRIPT_LIMIT):
            raise ValueError('original transcript exceeds observation policy')
        if budget is not None:
            budget.reserve(info.st_size)
        # Read at most the observed size plus one drift-detection byte.
        raw = stream.read(info.st_size + 1)
        if budget is not None:
            budget.check()
        if (len(raw) != info.st_size or _stamp(info) != _stamp(os.fstat(stream.fileno()))
                or _stamp(info) != _stamp(path.lstat())):
            raise ValueError('original transcript changed during observation')
    if not raw.endswith(b'\n'):
        raise ValueError('incomplete original transcript tail')
    records = [json.loads(line) for line in raw.splitlines()]
    return records, raw, _stamp(info)


def capture(config_path, job_id, directory, *, failed_rounds, client=None, clock=stamp,
            seconds=OBSERVATION_SECONDS, byte_limit=TOTAL_TRANSCRIPT_LIMIT,
            monotonic=time.monotonic):
    """Persist one complete observation for the original activated fifty slots.

    Nothing is written for an incomplete, changed or unavailable cohort. The
    caller must declare the fixture's expected rounds; no production stop or
    recovery authorization is created. Cross-slot checks are not OS-atomic.
    """
    if type(failed_rounds) is not int or failed_rounds < 1:
        raise ValueError('explicit positive fixture failure rounds required')
    budget = ObservationBudget(seconds, byte_limit, monotonic)
    directory = Path(directory)
    info = directory.lstat()
    if (not directory.is_absolute() or directory.resolve(strict=True) != directory
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('private original observation directory required')
    directory_identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
    target = directory / 'completion-observation.json'
    if os.path.lexists(target):
        raise ValueError('original observation already exists; never overwrite')
    saved, settlement_sha = live_settlement(config_path, job_id)
    budget.check()
    if client is None:
        config = core.ConfigStore(config_path).load()
        client = core.CmuxClient(str(config.get('cmux_path', core.DEFAULT_CMUX)))
    observer = FirstTaskObserver(config_path, job_id, saved['action_id'], client=client)
    started = clock()
    if started['boot_id'] != saved['boot_id']:
        raise ValueError('observation boot differs from original cohort')
    observations, pinned = [], []
    for index in range(COUNT):
        budget.check()
        row, claim, hook = observer._bind(index)
        root, files = observer._live(row, claim, hook)
        path = observer._transcript_path(row, hook, root, files)
        if path is None:
            raise ValueError('original transcript unavailable')
        first_path = observer.path.parent / f'standby-first-task-{index}.json'
        first, first_raw = _read(first_path)
        records, raw, identity = read_transcript(path, budget=budget)
        prefix = first['transcript_prefix']
        length = prefix['bytes']
        if (first.get('original') != row or first.get('action_id') != saved['action_id']
                or first.get('transcript') != str(path)
                or type(length) is not int or not 0 <= length <= len(raw)
                or prefix['identity'] != list(identity[:2])
                or hashlib.sha256(raw[:length]).hexdigest() != prefix['sha256']):
            raise ValueError('completion differs from original activation transcript')
        lifecycle = evaluate_native_completion(records, row['session_id'], failed_rounds)
        if not lifecycle['passed']:
            raise ValueError('original activated task chain is not complete')
        first_started = next((r['payload'].get('turn_id') for r in records
                              if r.get('type') == 'event_msg'
                              and r['payload'].get('type') == 'task_started'), None)
        if not lifecycle['passed'] or first_started != first['confirmation']['task_id']:
            raise ValueError('original activated task chain is not complete')
        observer._live(row, claim, hook)
        budget.check()
        observations.append({'index': index, 'original': row, 'transcript': str(path),
            'transcript_sha256': _sha(raw), 'transcript_bytes': len(raw),
            'transcript_identity': list(identity), 'first_task_sha256': _sha(first_raw),
            'lifecycle': lifecycle})
        pinned.append((path, identity, first_path, first_raw, row, claim, hook))
    if live_settlement(config_path, job_id) != (saved, settlement_sha):
        raise ValueError('original owner or settlement changed')
    observer._current()
    for path, identity, first_path, first_raw, row, claim, hook in pinned:
        budget.check()
        observer._live(row, claim, hook)
        if _stamp(path.lstat()) != identity or _read(first_path)[1] != first_raw:
            raise ValueError('completion evidence changed before persistence')
        budget.check()
    finished = clock()
    elapsed = _elapsed(started, finished, saved['boot_id'])
    now = directory.lstat()
    if (directory.resolve(strict=True) != directory or
            (now.st_dev, now.st_ino, now.st_mode, now.st_uid) != directory_identity):
        raise ValueError('observation directory changed')
    record = {'version': 1, 'kind': 'standby_completion_observation',
        'job_id': saved['job_id'], 'action_id': saved['action_id'],
        'cohort_id': saved['cohort_id'], 'workspace_id': saved['workspace_id'],
        'boot_id': saved['boot_id'], 'mode': saved['mode'],
        'settlement_sha256': settlement_sha, 'failed_rounds': failed_rounds,
        'started': started, 'finished': finished, 'elapsed_seconds': elapsed,
        'budget': {'seconds': seconds, 'transcript_byte_limit': byte_limit,
                   'transcript_bytes': budget.bytes, 'deadline_kind': 'cooperative'},
        'slots': observations,
        'job_terminal': False, 'run_terminal': False,
        'scope': 'live original task observations, not atomic across slots; no cleanup, route closure or performance claim'}
    budget.check()
    write_once(target, record)
    return record


def verify_capture(config_path, job_id, path, *, clock=stamp,
                   seconds=OBSERVATION_SECONDS, byte_limit=TOTAL_TRANSCRIPT_LIMIT,
                   monotonic=time.monotonic):
    """Revalidate the full original task chain after owner/native cleanup.

    Unlike capture(), this does not require a live owner. It reopens every
    transcript and first-task receipt and re-evaluates the lifecycle, rather
    than trusting saved passed flags. It proves neither cleanup nor resource
    release; a job terminal producer must obtain those independently.
    """
    budget = ObservationBudget(seconds, byte_limit, monotonic)
    path = Path(path)
    value, raw = _read(path)
    saved, settlement_sha = read_settlement(config_path, job_id)
    budget.check()
    if (value.get('version') != 1 or value.get('kind') != 'standby_completion_observation'
            or value.get('job_id') != job_id
            or value.get('settlement_sha256') != settlement_sha
            or any(value.get(key) != saved[key] for key in
                   ('job_id', 'action_id', 'cohort_id', 'workspace_id', 'boot_id', 'mode'))
            or type(value.get('failed_rounds')) is not int or value['failed_rounds'] < 1):
        raise ValueError('completion observation differs from original settlement')
    _elapsed(value['started'], value['finished'], saved['boot_id'])
    _elapsed(value['finished'], clock(), saved['boot_id'])
    jobpath = batch.job_path(Path(config_path), job_id)
    ui_path = jobpath.parent / 'standby' / 'activation-ui.json'
    ui, ui_raw = _read(ui_path)
    if _sha(ui_raw) != saved['activation_ui_sha256']:
        raise ValueError('original activation differs from settled receipt')
    originals = ui['originals']
    slots = value.get('slots')
    if (not isinstance(slots, list) or len(slots) != COUNT
            or len(originals) != COUNT
            or [row.get('index') for row in slots] != list(range(COUNT))
            or len({row['session_id'] for row in originals}) != COUNT):
        raise ValueError('complete unique original task set required')
    pinned = []
    for index, row in enumerate(slots):
        budget.check()
        original = originals[index]
        first_path = jobpath.parent / f'standby-first-task-{index}.json'
        first, first_raw = _read(first_path)
        transcript = Path(row['transcript'])
        records, content, identity = read_transcript(transcript, budget=budget)
        prefix = first['transcript_prefix']
        length = prefix['bytes']
        if (row['original'] != original or first['original'] != original
                or first['action_id'] != value['action_id']
                or first['transcript'] != str(transcript)
                or _sha(first_raw) != row['first_task_sha256']
                or _sha(content) != row['transcript_sha256']
                or len(content) != row['transcript_bytes']
                or list(identity) != row['transcript_identity']
                or type(length) is not int or not 0 <= length <= len(content)
                or prefix['identity'] != list(identity[:2])
                or _sha(content[:length]) != prefix['sha256']):
            raise ValueError('original completion bytes or identity changed')
        lifecycle = evaluate_native_completion(records, original['session_id'], value['failed_rounds'])
        first_task = next((r['payload'].get('turn_id') for r in records
                           if r.get('type') == 'event_msg'
                           and r['payload'].get('type') == 'task_started'), None)
        if (not lifecycle['passed'] or lifecycle != row['lifecycle']
                or first_task != first['confirmation']['task_id']):
            raise ValueError('original task lifecycle no longer complete')
        pinned.append((transcript, identity, first_path, first_raw))
    if read_settlement(config_path, job_id) != (saved, settlement_sha):
        raise ValueError('original settlement changed during completion verification')
    for transcript, identity, first_path, first_raw in pinned:
        budget.check()
        if _stamp(transcript.lstat()) != identity or _read(first_path)[1] != first_raw:
            raise ValueError('completion evidence changed before final observation')
    if _read(path)[1] != raw or _read(ui_path)[1] != ui_raw:
        raise ValueError('original completion or activation receipt changed')
    verified = clock()
    _elapsed(value['finished'], verified, saved['boot_id'])
    budget.check()
    return {'observation_path': str(path), 'observation_sha256': _sha(raw),
            'job_id': job_id, 'action_id': value['action_id'], 'slots': COUNT,
            'verified': verified, 'job_terminal': False, 'run_terminal': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--failed-rounds', required=True, type=int)
    args = parser.parse_args()
    result = capture(args.config, args.job_id, args.directory, failed_rounds=args.failed_rounds)
    print(json.dumps({'kind': result['kind'], 'slots': len(result['slots']),
                      'job_terminal': False, 'run_terminal': False}))


if __name__ == '__main__':
    main()
