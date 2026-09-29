"""Independent activation clocks for immutable standby preparation jobs.

The UI origin stays a v1 input receipt. It never receives job_created/new_job.
This v2 bridge binds it to the durable activation and original native tasks.
Delivery ACK, action_finished and collection time cannot supply a terminal.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

import ccc_batch_timing as ui
import ccc_workspace_batch as batch
import ccc_standby_launch as launch
from ccc_native_standby import COUNT, digest, identifier, write_once

ORIGIN_PHASES = ('input_read', 'confirmation_accepted', 'action_enqueued',
                 'action_started', 'cli_received')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _serialized(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _read(path):
    path = Path(path)
    if path.resolve(strict=True) != path:
        raise ValueError('activation timing evidence path changed')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077 or before.st_size > 4 * 1024 * 1024):
            raise ValueError('activation timing requires bounded private evidence')
        raw = stream.read()
        after = os.fstat(stream.fileno())
    fields = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if fields(before) != fields(after) or fields(after) != fields(path.lstat()):
        raise ValueError('activation timing evidence changed during read')
    return json.loads(raw), raw


def _clock(value, boot):
    if (not isinstance(value, dict) or value.get('boot_id') != identifier(boot)
            or any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k])
                   or value[k] < 0 for k in ('wall', 'monotonic'))):
        raise ValueError('activation timing clock is not bound to boot')
    return value


def _elapsed(start, end, boot):
    _clock(start, boot); _clock(end, boot)
    delta = end['monotonic'] - start['monotonic']
    if delta < 0 or abs(end['wall'] - start['wall'] - delta) > .25:
        raise ValueError('activation timing clock order or wall clock changed')
    return delta


def _origin(value, selected):
    ui.validate(value)
    if (value.get('recording_error') or value['workspace_id'] != selected['workspace_id']
            or value['mode'] != selected['mode'] or not value.get('source_hashes')
            or [e['phase'] for e in value['events']] != list(ORIGIN_PHASES)):
        raise ValueError('standby activation requires the original accepted UI action')
    for event in value['events']:
        _elapsed(value['events'][0], event, selected['boot_id'])
    for before, after in zip(value['events'], value['events'][1:]):
        _elapsed(before, after, selected['boot_id'])
    return value


class ActivationTiming:
    def __init__(self, config_path, job_id, origin, *, clock=ui.stamp, current_hashes=ui.source_hashes):
        self.config_path = Path(config_path).resolve(strict=True)
        self.jobfile = batch.job_path(self.config_path, identifier(job_id))
        self.job, self._job_raw = _read(self.jobfile)
        self.selected = launch.policy(self.job, self.config_path)
        self.directory = self.jobfile.parent / 'standby'
        self.origin = copy.deepcopy(_origin(origin, self.selected))
        if self.origin['source_hashes'] != current_hashes():
            raise ValueError('activation UI runtime source changed')
        self.clock = clock
        self.receipt = self.directory / 'activation-ui.json'
        self.terminal = self.directory / 'activation-terminal.json'
        self._inputs = {}
        self._receipt_raw = None

    def _binding(self):
        job, raw = _read(self.jobfile)
        if raw != self._job_raw:
            raise ValueError('original standby preparation job changed')
        values, hashes = {}, {}
        for name in ('cohort', 'originals', 'activation', 'activation-attempt'):
            value, raw = _read(self.directory / (name + '.json'))
            previous = self._inputs.setdefault(name, raw)
            if raw != previous:
                raise ValueError('original activation timing evidence changed')
            values[name], hashes[name] = value, _sha(raw)
        manifest, active = values['cohort'], values['activation']
        if (any(manifest.get(k) != self.selected[k] for k in
                ('policy', 'cohort_id', 'workspace_id', 'boot_id', 'mode', 'generation'))
                or manifest.get('count') != COUNT or manifest.get('prompt') != batch.PROMPT
                or any(active.get(k) != v for k, v in manifest.items())
                or active.get('action_id') != self.origin['action_id']
                or active.get('originals') != values['originals']
                or values['activation-attempt'] != {'action_id': self.origin['action_id'],
                    'activation_sha256': digest(active)}
                or len(values['originals']) != COUNT
                or [r['index'] for r in values['originals']] != list(range(COUNT))
                or any(r['launch_id'] != job['slots'][i]['launch_id']
                    for i, r in enumerate(values['originals']))):
            raise ValueError('UI action differs from original durable activation')
        return values, {'job_id': job['id'], 'job_sha256': _sha(self._job_raw),
            'action_id': self.origin['action_id'], 'cohort_id': manifest['cohort_id'],
            'workspace_id': manifest['workspace_id'], 'boot_id': manifest['boot_id'],
            'mode': manifest['mode'], 'evidence_sha256': hashes}

    def committed(self):
        """Manager callback: durable consumption exists, zero send workers yet."""
        values, binding = self._binding()
        now = self.clock()
        _elapsed(self.origin['events'][-1], now, binding['boot_id'])
        committed = values['activation']['committed_monotonic']
        if not self.origin['events'][-1]['monotonic'] <= committed <= now['monotonic']:
            raise ValueError('activation consumption is outside the UI interval')
        record = {'version': 2, 'kind': 'standby_activated', 'new_activation': True,
            **binding, 'origin': self.origin, 'committed_monotonic': committed,
            'originals': values['originals'],
            'event': {'phase': 'standby_activated', **now}}
        self._receipt_raw = write_once(self.receipt, record)
        self._binding()
        return copy.deepcopy(record)

    def finish(self, *, outcome, reason=None):
        """Persist a separate native observation terminal, never an ACK time.

        `complete` requires all 50 original confirmed task receipts. An explicit
        timeout/cancellation/failure remains non-passing even with 50 receipts.
        """
        if outcome not in {'complete', 'timeout', 'cancelled', 'failed'}:
            raise ValueError('invalid activation observation terminal')
        if outcome != 'complete' and not reason:
            raise ValueError('noncomplete activation terminal requires a reason')
        values, binding = self._binding()
        receipt, raw = _read(self.receipt)
        if (self._receipt_raw is None or raw != self._receipt_raw
                or any(receipt.get(k) != v for k, v in binding.items())
                or receipt.get('origin') != self.origin
                or receipt.get('originals') != values['originals']
                or receipt.get('version') != 2 or receipt.get('kind') != 'standby_activated'
                or receipt.get('new_activation') is not True):
            raise ValueError('activation UI receipt changed')
        tasks, task_files = [], {}
        for index in range(COUNT):
            path = self.jobfile.parent / f'standby-first-task-{index}.json'
            if not path.exists():
                continue
            record, task_raw = _read(path)
            observed_path = self.jobfile.parent / f'standby-first-observation-{index}.json'
            observed, observed_raw = _read(observed_path)
            task_files.update({path: task_raw, observed_path: observed_raw})
            original = values['originals'][index]
            proof = record.get('confirmation', {})
            if (any(record.get(k) != self.selected[k] for k in self.selected)
                    or any(observed.get(k) != self.selected[k] for k in self.selected)
                    or record.get('action_id') != binding['action_id']
                    or observed.get('action_id') != binding['action_id']
                    or record.get('original') != original or observed.get('original') != original
                    or record.get('first_task_observed') != observed.get('observed')
                    or observed.get('task_id') != proof.get('task_id')
                    or observed.get('task_at') != proof.get('task_at')
                    or observed.get('transcript') != record.get('transcript')
                    or proof.get('session_id') != original['session_id']
                    or not all(proof.get(k) is True for k in ('confirmed', 'started', 'prompt'))
                    or proof.get('blocked')):
                raise ValueError('activation task receipt differs from original observation')
            # A terminal cannot bless a cached proof after transcript replacement.
            from ccc_standby_acceptance import FirstTaskObserver
            for prefix in (record['transcript_prefix'], observed['transcript_prefix']):
                if FirstTaskObserver._prefix(Path(record['transcript']), prefix['bytes']) != prefix:
                    raise ValueError('activation task transcript prefix changed')
            tasks.append({'index': index, 'original': original, 'turn_id': proof['task_id'],
                'task_at': proof['task_at'], 'observed': observed['observed'],
                'task_receipt_sha256': _sha(task_raw), 'observation_receipt_sha256': _sha(observed_raw)})
        if outcome == 'complete' and (len(tasks) != COUNT
                or os.path.lexists(self.directory / 'invalidated.json')):
            raise ValueError('complete terminal requires 50 valid original tasks')
        terminal = {'version': 2, 'kind': 'standby_activation_terminal', **binding,
            'activation_ui_sha256': _sha(raw), 'outcome': outcome, 'reason': reason,
            'event': {'phase': 'activation_terminal', **self.clock()}, 'tasks': tasks}
        result = evaluate(receipt, terminal, current_hashes=self.origin['source_hashes'])
        if result['problems']:
            raise ValueError('invalid activation terminal: ' + ', '.join(result['problems']))
        self._binding()
        if _read(self.receipt)[1] != raw or any(_read(p)[1] != data for p, data in task_files.items()):
            raise ValueError('activation terminal inputs changed during observation')
        if self.terminal.exists():
            saved, _ = _read(self.terminal)
            if (any(saved.get(k) != v for k, v in terminal.items() if k != 'event')
                    or evaluate(receipt, saved, current_hashes=self.origin['source_hashes'])['problems']):
                raise ValueError('persisted activation terminal differs')
            return saved  # Never replace the original independent terminal time.
        write_once(self.terminal, terminal)
        return copy.deepcopy(terminal)


def evaluate(receipt, terminal, *, current_hashes=None):
    """Conservative upper bounds from both UI origins; no native-late inference."""
    problems, rows = [], []
    try:
        if (receipt.get('version') != 2 or receipt.get('kind') != 'standby_activated'
                or receipt.get('new_activation') is not True
                or terminal.get('activation_ui_sha256') != _sha(_serialized(receipt))
                or terminal.get('version') != 2 or terminal.get('kind') != 'standby_activation_terminal'
                or terminal.get('event', {}).get('phase') != 'activation_terminal'
                or receipt.get('event', {}).get('phase') != 'standby_activated'
                or any(terminal.get(k) != receipt[k] for k in ('job_id', 'job_sha256', 'action_id',
                    'cohort_id', 'workspace_id', 'boot_id', 'mode', 'evidence_sha256'))):
            raise ValueError('activation_terminal_binding')
        origin = _origin(receipt['origin'], receipt)
        if (len(receipt['originals']) != COUNT
                or [r['index'] for r in receipt['originals']] != list(range(COUNT))
                or _sha(_serialized(receipt['originals'])) != receipt['evidence_sha256']['originals']):
            raise ValueError('activation_originals_binding')
        if origin['action_id'] != receipt['action_id']:
            raise ValueError('activation_action_binding')
        expected = ui.source_hashes() if current_hashes is None else current_hashes
        if origin['source_hashes'] != expected:
            raise ValueError('source_hash_drift')
        boot = receipt['boot_id']
        _elapsed(origin['events'][-1], receipt['event'], boot)
        _elapsed(receipt['event'], terminal['event'], boot)
        if not origin['events'][-1]['monotonic'] <= receipt['committed_monotonic'] <= receipt['event']['monotonic']:
            raise ValueError('activation_consumption_clock')
        seen = {k: set() for k in ('index', 'surface_id', 'session_id', 'turn_id', 'pid', 'launch_id')}
        for task in terminal['tasks']:
            if any(not isinstance(task.get(key), str)
                    or re.fullmatch(r'[0-9a-f]{64}', task[key]) is None
                    for key in ('task_receipt_sha256', 'observation_receipt_sha256')):
                raise ValueError('native_receipt_hash_missing_or_invalid')
            original = task['original']
            if (type(task['index']) is not int or not 0 <= task['index'] < COUNT
                    or original != receipt['originals'][task['index']]
                    or original['index'] != task['index'] or original['workspace_id'] != receipt['workspace_id']
                    or type(original['pid']) is not int or original['pid'] <= 1
                    or len(original['birth']) != 2
                    or any(type(n) is not int for n in original['birth'])
                    or original['birth'][0] <= 0 or not 0 <= original['birth'][1] < 1000000):
                raise ValueError('native_identity_invalid')
            for key in seen:
                value = task[key] if key in ('index', 'turn_id') else original[key]
                if key not in ('index', 'pid'):
                    identifier(value)
                if value in seen[key]:
                    raise ValueError('reused_native_identity')
                seen[key].add(value)
            _elapsed(receipt['event'], task['observed'], boot)
            _elapsed(task['observed'], terminal['event'], boot)
            native = dt.datetime.fromisoformat(task['task_at'].replace('Z', '+00:00'))
            if (native.tzinfo is None or native.timestamp() < receipt['event']['wall'] - .001
                    or native.timestamp() > task['observed']['wall'] + .001):
                raise ValueError('native_wall_outside_activation_window')
            rows.append({'index': task['index'], 'surface_id': original['surface_id'],
                'session_id': original['session_id'], 'turn_id': task['turn_id'],
                'input_upper_bound_seconds': _elapsed(origin['events'][0], task['observed'], boot),
                'confirmation_upper_bound_seconds': _elapsed(origin['events'][1], task['observed'], boot)})
        if terminal['outcome'] not in {'complete', 'timeout', 'cancelled', 'failed'}:
            raise ValueError('invalid_terminal_outcome')
        if terminal['outcome'] == 'complete' and len(rows) != COUNT:
            raise ValueError('incomplete_original_tasks')
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        problems.append(str(exc))
    complete = not problems and terminal.get('outcome') == 'complete' and len(rows) == COUNT
    timely = complete and all(0 <= row['input_upper_bound_seconds'] <= 1
        and 0 <= row['confirmation_upper_bound_seconds'] <= 1 for row in rows)
    return {'version': 2, 'problems': problems, 'original_tasks': rows,
        'evidence_complete': complete, 'startup_passed': timely,
        'verdict': 'timely_upper_bound' if timely else 'not_proven',
        'limitation': 'Slow observation does not prove late native startup; this does not prove 500 live sessions.'}
