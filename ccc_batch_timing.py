"""Local UI batch timing evidence. Never supplies input or authorization."""
from __future__ import annotations

import functools
import datetime as dt
import hashlib
import json
import logging
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid


ACTIONS = {'batch_workspace': 'B', 'private_batch_workspace': 'b', 'access_batch_workspace': 'N'}
STAGES = {'input_read': 0, 'confirmation_accepted': 10, 'action_enqueued': 20,
          'action_started': 30, 'cli_received': 40, 'job_created': 50, 'job_reused': 50,
          'cli_failed': 50, 'action_finished': 60, 'cancelled': 10,
          'rejected_selection': 10, 'rejected_busy': 20}


@functools.lru_cache(maxsize=1)
def boot_id():
    try:
        if sys.platform == 'darwin':
            value = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                                            text=True, timeout=2).strip()
        else:
            value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return str(uuid.UUID(value))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None  # Missing clock identity cannot prove cross-process latency.


def stamp():
    return {'wall': time.time(), 'monotonic': time.monotonic(), 'boot_id': boot_id()}


def source_hashes():
    import cmux_codex_watch as core
    root = Path(core.__file__).resolve().parent
    names = set(core.RUNTIME_FILES) | {'cmux_supervisor_tui.py', 'ccc_batch_timing.py'}
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(names)}


def new(action, workspace_id, input_kind, row_kind, input_stamp):
    if action not in ACTIONS:
        return None
    try:
        hashes = source_hashes()
    except OSError:
        hashes = {}
    trace = {'version': 1, 'action_id': str(uuid.uuid4()), 'workspace_id': workspace_id,
             'mode': ACTIONS[action], 'input_kind': input_kind, 'row_kind': row_kind,
             'events': [{'phase': 'input_read', **input_stamp}], 'source_hashes': hashes,
             'scope': 'TUI getch receipt; not physical OS event time'}
    return trace


def mark(trace, phase, **details):
    if trace is not None:
        trace['events'].append({'phase': phase, **stamp(), **details})
    return trace


def path(config_path, action_id):
    if not isinstance(action_id, str):
        raise ValueError('missing UI action id')
    identifier = str(uuid.UUID(action_id))
    if identifier != action_id:
        raise ValueError('noncanonical UI action id')
    return Path(config_path).parent / 'ui-actions' / (identifier + '.json')


def validate(trace):
    if not isinstance(trace, dict) or trace.get('version') != 1:
        raise ValueError('invalid UI timing record')
    path(Path('config.json'), trace.get('action_id'))
    if (not isinstance(trace.get('workspace_id'), str) or not trace['workspace_id']
            or trace.get('mode') not in ACTIONS.values()
            or trace.get('input_kind') not in {'keyboard', 'mouse'}
            or trace.get('row_kind') not in {'group', 'candidate'}):
        raise ValueError('invalid UI timing origin')
    events = trace.get('events')
    if not isinstance(events, list) or not 1 <= len(events) <= 32:
        raise ValueError('invalid UI timing stages')
    for row in events:
        if (not isinstance(row, dict) or not isinstance(row.get('phase'), str)
                or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k])
                       or row[k] < 0 for k in ('wall', 'monotonic'))):
            raise ValueError('invalid UI timing clock')
    return trace


def save(config_path, trace):
    if trace is None:
        return
    import cmux_codex_watch as core
    validate(trace)
    core.atomic_write_json(path(config_path, trace['action_id']), trace)


def read(config_path, action_id):
    trace = validate(json.loads(path(config_path, action_id).read_text()))
    if trace['action_id'] != action_id:
        raise ValueError('UI timing action identity changed')
    return trace


def record(config_path, trace, phase, **details):
    mark(trace, phase, **details)
    try:
        save(config_path, trace)
        return True
    except (OSError, ValueError) as exc:
        if trace is not None:
            trace['recording_error'] = type(exc).__name__
        logging.getLogger(__name__).warning('UI timing evidence unavailable: %s', type(exc).__name__)
        return False


def intervals(trace):
    """Report both UI origins. A missing/duplicate stage never becomes zero."""
    validate(trace)
    phases = {}
    problems = []
    if trace.get('recording_error'):
        problems.append('timing_recording_failed')
    previous = None
    rank = -1
    for event in trace['events']:
        phase = event['phase']
        current_rank = STAGES.get(phase, -1)
        if current_rank < rank or current_rank < 0:
            problems.append('invalid_stage_order:' + phase)
        rank = current_rank
        if phase in phases:
            problems.append('duplicate_stage:' + phase)
        phases[phase] = event
        if not event.get('boot_id') or event['boot_id'] != trace['events'][0].get('boot_id'):
            problems.append('clock_boot_unverified')
        if previous:
            elapsed = event['monotonic'] - previous['monotonic']
            if elapsed < 0:
                problems.append('negative_monotonic_elapsed')
            if abs((event['wall'] - previous['wall']) - elapsed) > .25:
                problems.append('wall_clock_jump')
        previous = event
    result = {'problems': sorted(set(problems)), 'scope': trace.get('scope', 'unverified UI source')}
    for start, end, name in [('input_read', 'confirmation_accepted', 'confirmation_seconds'),
                             ('input_read', 'cli_received', 'input_to_cli_seconds'),
                             ('confirmation_accepted', 'cli_received', 'confirmation_to_cli_seconds')]:
        result[name] = (phases[end]['monotonic'] - phases[start]['monotonic']
                        if start in phases and end in phases else None)
        if result[name] is not None and result[name] < 0:
            problems.append('negative_interval:' + name)
    if trace['events'][0]['phase'] != 'input_read':
        problems.append('missing_input_origin')
    if 'job_created' in phases and 'job_reused' in phases:
        problems.append('conflicting_job_outcomes')
    result['problems'] = sorted(set(problems))
    result['startup_passed'] = False  # UI stages alone never prove 50 native tasks.
    return result


def evaluate(trace, job, *, current_hashes=None):
    """An observation under one second proves timely startup; a slow one does not disprove it."""
    result = intervals(trace)
    problems = list(result['problems'])
    phases = {r['phase']: r for r in trace['events']}
    required = ('input_read', 'confirmation_accepted', 'action_enqueued', 'action_started',
                'cli_received', 'job_created', 'action_finished')
    if any(name not in phases for name in required):
        problems.append('incomplete_ui_chain')
    if any(name in phases for name in ('cancelled', 'rejected_busy', 'rejected_selection', 'cli_failed', 'job_reused')):
        problems.append('not_a_new_accepted_batch')
    origin = dict(trace)
    origin['events'] = trace['events'][:next((i for i,r in enumerate(trace['events'])
                                            if r['phase'] == 'job_created'), len(trace['events']))]
    if job.get('ui_timing_origin') != origin:
        # The job stores the exact origin as received by start(), not a later
        # action's timestamps. Allow only the subsequent job/finish stages.
        problems.append('job_origin_mismatch')
    created = phases.get('job_created', {})
    if (created.get('new_job') is not True or created.get('job_id') != job.get('id')
            or trace['workspace_id'] != job.get('workspace_id')):
        problems.append('job_identity_mismatch')
    if (not trace.get('source_hashes')
            or trace['source_hashes'] != (source_hashes() if current_hashes is None else current_hashes)):
        problems.append('source_hash_drift')
    slots = job.get('slots', [])
    if (len(slots) != 50 or any(type(s.get('index')) is not int for s in slots)
            or {s.get('index') for s in slots} != set(range(50))):
        problems.append('incomplete_original_slots')
    rows, surfaces, sessions, turns, pids = [], set(), set(), set(), set()
    for slot in slots:
        proof = slot.get('confirmation', {})
        observed = proof.get('first_task_observed', {})
        identity = proof.get('first_task_observed_identity', {})
        expected = {'surface_id':slot.get('surface_id'), 'workspace_id':job.get('workspace_id'),
                    'session_id':slot.get('session_id'), 'turn_id':proof.get('task_id'),
                    'pid':slot.get('pid'), 'birth':slot.get('native_birth'), 'verified':True}
        try:
            sid, session, turn = (str(uuid.UUID(expected[k])) for k in ('surface_id','session_id','turn_id'))
            native_wall = dt.datetime.fromisoformat(proof['task_at'].replace('Z', '+00:00'))
            valid_identity = (type(expected['pid']) is int and expected['pid'] > 1
                and isinstance(expected['birth'], list) and len(expected['birth']) == 2
                and all(type(n) is int for n in expected['birth']) and expected['birth'][0] > 0
                and 0 <= expected['birth'][1] < 1000000 and native_wall.tzinfo is not None)
        except (TypeError, ValueError, AttributeError, KeyError):
            valid_identity = False
        if (not valid_identity or slot.get('phase') != 'confirmed' or proof.get('confirmed') is not True
                or proof.get('blocked')
                or slot.get('ui_original_identity') != {k:v for k,v in expected.items() if k not in {'turn_id','verified'}}
                or identity.get('verified') is not True
                or proof.get('session_id') != expected['session_id']
                or identity != expected or not expected['birth']
                or any(not expected[k] for k in ('surface_id','session_id','turn_id','pid'))):
            problems.append('incomplete_native_identity')
            continue
        if (sid in surfaces or session in sessions or turn in turns or expected['pid'] in pids):
            problems.append('reused_native_identity')
        surfaces.add(sid); sessions.add(session)
        turns.add(turn); pids.add(expected['pid'])
        row = {'index':slot.get('index'), **expected, 'native_task_wall_timestamp':proof.get('task_at')}
        for origin, field in (('input_read','input_upper_bound_seconds'),
                              ('confirmation_accepted','confirmation_upper_bound_seconds')):
            start = phases.get(origin, {})
            if (any(type(observed.get(k)) not in (int,float) or not math.isfinite(observed[k])
                    for k in ('wall','monotonic')) or not observed.get('boot_id')
                    or observed['boot_id'] != start.get('boot_id') or not start):
                problems.append('native_clock_unverified')
                row[field] = None
                continue
            elapsed = observed['monotonic'] - start['monotonic']
            row[field] = elapsed
            if elapsed < 0:
                problems.append('negative_native_elapsed')
            if abs((observed['wall'] - start['wall']) - elapsed) > .25:
                problems.append('wall_clock_jump')
            if any(observed['monotonic'] < phases[name]['monotonic']
                   for name in ('cli_received','job_created') if name in phases):
                problems.append('native_observed_before_creation')
            if (native_wall.timestamp() > observed['wall'] + .001
                    or native_wall.timestamp() < start['wall'] - .001
                    or native_wall.timestamp() < created.get('wall', 0) - .001):
                problems.append('native_wall_outside_observation_window')
        rows.append(row)
    complete = not problems and len(rows) == 50
    timely = complete and all(0 <= r['input_upper_bound_seconds'] <= 1
                              and 0 <= r['confirmation_upper_bound_seconds'] <= 1 for r in rows)
    result.update(problems=sorted(set(problems)), original_tasks=rows, evidence_complete=complete,
                  startup_passed=timely, verdict='timely_upper_bound' if timely else 'not_proven',
                  limitation='A slow observation is not proof that the native task actually started late.')
    return result
