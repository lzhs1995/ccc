"""Read-only final-input gate for explicitly enrolled compaction targets.

Controller ownership, observer health and input permission are independent.
A cancelled controller owns nothing; it cannot clear an unknown input or
make an unavailable observer evidence of safe input.
"""
import copy
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from ccc_compact_inputs import LEDGER_VERSION, clear_guard
from ccc_compact_intents import read_json, signature

HELD = {"compacting", "control_pending", "uncertain", "exhausted"}
RELEASED = {"cancelled", "observe_only", "compacted"}
MAX_AGE = 45
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RuntimeError)

# The final-input gate may inspect a large, settled rollout on many polls.
# Keep bounded progress for the exact immutable file/process generation rather
# than restarting a 16 MiB scan on every poll. Changes still require a full scan.
_NEW_SESSION_READERS = OrderedDict()
_NEW_SESSION_READERS_LOCK = threading.Lock()
_NEW_SESSION_READER_LIMIT = 32


def _new_session_rollout_facts(path, current_sid, live, pin):
    from ccc_compact_sensor import RolloutFacts

    path = Path(path)
    name = str(path)
    identity = (current_sid, live['pid'], tuple(live['birth']), tuple(pin))
    with _NEW_SESSION_READERS_LOCK:
        if signature(path) != pin:
            _NEW_SESSION_READERS.pop(name, None)
            raise ValueError('new-session rollout changed before bounded observation')
        cached = _NEW_SESSION_READERS.get(name)
        if cached is None or cached[0] != identity:
            _NEW_SESSION_READERS.pop(name, None)
            cached = (identity, RolloutFacts(path))
            _NEW_SESSION_READERS[name] = cached
        _NEW_SESSION_READERS.move_to_end(name)
        while len(_NEW_SESSION_READERS) > _NEW_SESSION_READER_LIMIT:
            _NEW_SESSION_READERS.popitem(last=False)
        try:
            facts = cached[1].refresh()
            if signature(path) != pin:
                raise ValueError('new-session rollout changed during bounded observation')
        except ERRORS:
            _NEW_SESSION_READERS.pop(name, None)
            raise
        return facts


def _uuid(value):
    return str(uuid.UUID(value)).upper()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _ownership(value, now):
    if 'episode' not in value or 'progress_after' not in value:
        return dict(state='unknown', phase=None, reason='compaction episode missing')
    ep = value['episode']
    if ep is None and value['progress_after'] is None:
        return dict(state='none', phase=None)
    if not isinstance(ep, dict) or not isinstance(ep.get('phase'), str):
        return dict(state='unknown', phase=None, reason='invalid compaction episode')
    phase = ep['phase']
    if phase in HELD or (phase == 'compacted' and value['progress_after'] is not None):
        started = ep.get('started_at')
        seconds = max(0, int(now - started)) if _finite(started) else 0
        return dict(state='held', phase=phase, elapsed_floor=seconds,
                    reason='original compaction controller owns recovery; no duplicate continuation')
    if phase in RELEASED:
        return dict(state='none', phase=phase)
    return dict(state='unknown', phase=phase, reason='unknown compaction phase or progress fence')


def _observer(value, health, sid, surface, now):
    native = value.get('native')
    if not isinstance(native, dict) or native.get('binding_ok') is not True:
        raise ValueError('compaction binding not verified')
    at = value.get('observed_at')
    if not _finite(at) or not 0 <= now - at <= MAX_AGE:
        return dict(state='stale', reason='compaction observation heartbeat expired')
    if (_uuid(health['session_id']) != _uuid(sid)
            or _uuid(health['surface_id']) != surface):
        raise ValueError('compaction observer identity mismatch')
    health_at = health.get('at')
    if not _finite(health_at) or not 0 <= now - health_at <= MAX_AGE:
        return dict(state='stale', reason='compaction worker heartbeat expired')
    retained_states = {'pinned_before_submission', 'pinned_not_transmitted', 'native_receipt_pending'}
    if health.get('state') in retained_states:
        # A current retained-control heartbeat is useful operator evidence,
        # never permission to submit or release the original input.
        return dict(state=health['state'],
                    reason='original control evidence retained: ' + health['state'],
                    observed_at=at, control_evidence=health.get('control_evidence'))
    if (value.get('episode') or {}).get('phase') == 'control_pending':
        return dict(state='control_evidence_unavailable',
                    reason='original control pending without a matching health heartbeat',
                    observed_at=at)
    if health.get('state') != 'observing' or health.get('binding_verified') is not True:
        raise ValueError(str(health.get('error') or 'compaction observer unavailable')[:160])
    facts = native.get('facts')
    if not isinstance(facts, dict) or facts.get('caught_up') is not True:
        raise ValueError('compaction native observation not caught up')
    if 'compact' not in native or (native['compact'] is not None
                                  and not isinstance(native['compact'], dict)):
        raise ValueError('current native compaction state unavailable')
    return dict(state='healthy', observed_at=at)


def _input_identity(value, ledger, item, sid):
    """Authenticate the observed ledger and original transcript, even if UNKNOWN."""
    if ledger.get('version') != LEDGER_VERSION:
        raise ValueError('external input ledger version or coverage unverified')
    native = value.get('native') or {}
    facts = native.get('facts') or {}
    binding = ledger.get('binding')
    if (not isinstance(binding, dict) or binding.get('session_id') != sid
            or not binding.get('path') or (item.get('path') is not None
                                          and binding.get('path') != item['path'])
            or binding.get('path') != facts.get('path')
            or not isinstance(binding.get('file_id'), list) or len(binding['file_id']) != 2
            or binding['file_id'] != facts.get('file_id')):
        raise ValueError('external input ledger identity mismatch')
    st = signature(Path(binding['path']))
    if st[:2] != binding['file_id']:
        raise ValueError('external input transcript identity changed')
    return native


def _goal_status_evidence(value, ledger, item, sid):
    """Status-only Goal control need not resolve historical input receipts.

    Identity, a caught-up observation and its exact ledger token still apply.
    This verdict never authorizes input, interruption or compaction.
    """
    native = _input_identity(value, ledger, item, sid)
    if (not isinstance(ledger.get('unknown'), list)
            or type(ledger.get('initial_coverage')) is not bool
            or any(not isinstance(ledger.get(k), dict) for k in ('entries', 'intents'))):
        raise ValueError('external input ledger malformed')
    guard = native.get('input_guard')
    if not isinstance(guard, dict) or guard.get('caught_up') is not True:
        raise ValueError('external input observation not caught up')
    identity = {k: ledger[k] for k in ('binding', 'entries', 'intents', 'unknown', 'initial_coverage')}
    token = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if token != guard.get('token'):
        raise ValueError('external input ledger changed after observation')
    return dict(state='clear', reason='status-only Goal; historical input evidence retained')


def _input_evidence(value, ledger, item, sid):
    """Check durable evidence without adopting, reconciling or writing it."""
    native = _input_identity(value, ledger, item, sid)
    if not isinstance(ledger.get('unknown'), list) or ledger['unknown']:
        raise ValueError('external input ledger UNKNOWN')
    if ledger.get('initial_coverage') is not True:
        raise ValueError('external input ledger initial coverage unproven')
    for name in ('entries', 'intents'):
        entries = ledger.get(name)
        if not isinstance(entries, dict):
            raise ValueError('external input ledger malformed ' + name)
        for entry in entries.values():
            if not isinstance(entry, dict) or entry.get('unknown'):
                raise ValueError('external input ledger UNKNOWN ' + name)
            if not isinstance(entry.get('receipt'), dict) or not entry['receipt']:
                raise ValueError('external input ledger pending ' + name)
    guard = native.get('input_guard')
    if (not clear_guard(native) or guard.get('initial_coverage') is not True
            or guard.get('unmatched_native_receipts') is not False
            or guard.get('pending') != [] or guard.get('intent_pending') != []
            or guard.get('unknown') is not False):
        raise ValueError('current external input observation unsafe or UNKNOWN')
    identity = {k: ledger[k] for k in ('binding', 'entries', 'intents', 'unknown', 'initial_coverage')}
    token = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if token != guard.get('token'):
        raise ValueError('external input ledger changed after observation')
    return dict(state='clear', reason='current external input evidence clear')


def _terminal_evidence(read, directory, value, ledger, item, sid, surface, target, turn, now):
    """Check an independently ended failure in the current terminal process.

    A read-only sensor does not need a Codex control socket. The sender still
    checks the exact composer and repeats current_turn at the actual input.
    Old unresolved input is retained, never acknowledged or replayed here.
    Only evidence frozen before this process existed can be isolated from a
    failure that began and ended in this process. Current-process UNKNOWN
    continues to veto input.
    """
    from ccc_codex_queue import task_snapshot
    from ccc_guard_scope import process

    if not isinstance(turn, dict) or turn.get('kind') != 'task_complete':
        raise ValueError('current terminal failed-turn completion unavailable')
    error = turn.get('error')
    if not isinstance(error, dict) or not isinstance(error.get('message'), str) or not error['message']:
        raise ValueError('current terminal turn is not a failed turn')
    if turn.get('session_id') != sid or not _finite(turn.get('at')) or turn['at'] > now:
        raise ValueError('terminal failure identity or time invalid')
    live = process(turn.get('pid'))
    if (not isinstance(live, dict) or live.get('surface_id') != surface
            or live.get('environment_workspace_id') != _uuid(target.get('workspace_id'))
            or not isinstance(live.get('birth'), list) or len(live['birth']) != 2
            or any(type(x) is not int for x in live['birth'])
            or live['birth'] != turn.get('birth') or live['birth'][0] != turn.get('process_start')):
        raise ValueError('current terminal process identity changed')
    born = live['birth'][0] + live['birth'][1] / 1e6
    born_ns = live['birth'][0] * 1_000_000_000 + live['birth'][1] * 1000
    observation = read(directory / 'read-only-observation.json')
    health = read(directory / 'worker-health.json')
    at, health_at = observation.get('at'), health.get('at')
    if (not _finite(at) or not 0 <= now - at <= MAX_AGE
            or not _finite(health_at) or not 0 <= now - health_at <= MAX_AGE):
        raise ValueError('current terminal observation heartbeat expired')
    binding = observation.get('process_binding') or {}
    identity, observed_process = binding.get('identity') or {}, binding.get('process') or {}
    if (observation.get('state') != 'read_only_observing'
            or observation.get('process_binding_verified') is not True
            or observation.get('session_id') != sid or _uuid(observation.get('surface_id')) != surface
            or _uuid(identity.get('workspace_uuid')) != _uuid(target.get('workspace_id'))
            or _uuid(identity.get('target_surface_uuid')) != surface
            or observed_process.get('pid') != turn.get('pid')
            or observed_process.get('birth') != live['birth']
            or health.get('state') not in {'read_only_observing', 'observing'}
            or health.get('session_id') != sid or _uuid(health.get('surface_id')) != surface):
        raise ValueError('current terminal observation identity unavailable')
    facts = observation.get('facts') or {}
    observed_turn = facts.get('observed_turn') or {}
    path = Path(facts.get('path', ''))
    if (facts.get('session_id') != sid or facts.get('caught_up') is not True
            or facts.get('receipt_overflow') is not False or facts.get('lifecycle_error') is not None
            or not path.is_absolute() or (item.get('path') is not None and item['path'] != str(path))
            or observed_turn.get('id') != turn.get('turn_id')
            or observed_turn.get('status') != 'task_complete'
            or not _finite(observed_turn.get('started_at'))
            or not born <= observed_turn['started_at'] <= turn['at']
            or observed_turn.get('ended_at') != turn['at']
            or observation.get('paused') is not False
            or 'unsettled_compaction' not in observation or observation['unsettled_compaction'] is not None):
        raise ValueError('current terminal observation is paused, compacting, incomplete or changed')
    pin = signature(path)
    if (facts.get('file_id') != pin[:2] or facts.get('offset') != pin[2]
            or turn.get('signature') != pin[1:4]):
        raise ValueError('current terminal rollout advanced or changed')
    tail = turn.get('terminal_tail')
    if (not isinstance(tail, dict) or tail.get('end') != pin[2]
            or tail.get('offset') != observed_turn.get('end_offset')):
        raise ValueError('current terminal completion is not at the stable EOF')
    snapshot = task_snapshot(path, sid)
    if (not snapshot or any(snapshot.get(k) != turn.get(k) for k in
            ('kind', 'at', 'turn_id', 'error', 'signature', 'terminal_tail'))):
        raise ValueError('current terminal failure no longer matches the original rollout')

    # Prefer complete contemporary evidence. A stale lifetime ledger may be
    # isolated only across a proven process boundary, not merely by its age.
    try:
        evidence = _input_evidence(value, ledger, item, sid)
        evidence['scope'] = 'current_input_ledger'
    except ERRORS:
        old_binding = ledger.get('binding') or {}
        if (ledger.get('version') != LEDGER_VERSION or old_binding.get('session_id') != sid
                or old_binding.get('path') != str(path)
                or not isinstance(ledger.get('unknown'), list)
                or any(not isinstance(x, str) for x in ledger['unknown'])
                or type(ledger.get('initial_coverage')) is not bool
                or any(not isinstance(ledger.get(k), dict) for k in ('entries', 'intents'))):
            raise ValueError('historical input ledger identity or structure unproven')
        for name in ('state.json', 'pending-inputs.json'):
            old_pin = signature(directory / name)
            if max(old_pin[3], old_pin[4]) >= born_ns:
                raise ValueError('input or controller evidence changed in current process lifetime')
        if not _finite(value.get('observed_at')) or value['observed_at'] >= born:
            raise ValueError('historical controller boundary unproven')
        entries = list(ledger['entries'].values()) + list(ledger['intents'].values())
        for entry in entries:
            if (not isinstance(entry, dict) or not _finite(entry.get('at')) or entry['at'] >= born
                    or any(entry.get(k) == turn['turn_id'] for k in
                           ('expected_turn_id', 'receipt_turn_id', 'submission_id'))):
                raise ValueError('current-process pending input or UNKNOWN retained')
        evidence = dict(state='clear', scope='new_process_failed_turn',
                        reason='current failed turn independently verified; pre-process input evidence retained',
                        historical_unknown_retained=bool(ledger['unknown'] or not ledger['initial_coverage']),
                        historical_entries_retained=len(entries), process_birth=live['birth'])
    if process(turn['pid']) != live or signature(path) != pin:
        raise ValueError('terminal process or original rollout changed during final check')
    return evidence, dict(state='healthy', observed_at=at, mode='read_only'), observation


def _generation(record):
    if (not isinstance(record, dict) or type(record.get('pid')) is not int
            or not 1 < record['pid'] < 2**31
            or not isinstance(record.get('birth'), list) or len(record['birth']) != 2
            or any(type(x) is not int for x in record['birth'])
            or record['birth'][0] <= 0 or not 0 <= record['birth'][1] < 1_000_000):
        raise ValueError('historical process generation unavailable')
    return record['pid'], tuple(record['birth'])


def _historical_start_second(pid):
    """Read a stable kernel start second when libproc cannot inspect a reused PID.

    Coarse equality cannot retire a generation: the microsecond birth remains
    required for it. Unavailable, malformed or changing observations stay unknown.
    """
    import calendar
    import re
    import subprocess

    pattern = (r"\s*([0-9]+)\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
               r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
               r"([0-9]{1,2})\s+([0-9]{2}):([0-9]{2}):([0-9]{2})\s+([0-9]{4})\s*")
    months = dict(zip(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), range(1, 13)))
    observations = []
    for _ in range(2):
        try:
            result = subprocess.run(
                ['/bin/ps', '-p', str(pid), '-o', 'pid=,lstart='],
                capture_output=True, text=True, timeout=1,
                env={'LC_ALL': 'C', 'LANG': 'C', 'TZ': 'UTC'})
            match = re.fullmatch(pattern, result.stdout)
            if result.returncode != 0 or result.stderr.strip() or match is None:
                return None
            actual_pid, month, day, hour, minute, second, year = match.groups()
            if int(actual_pid) != pid:
                return None
            # datetime validates all fields; UTC avoids DST/local-time ambiguity.
            from datetime import datetime, timezone
            value = datetime(int(year), months[month], int(day), int(hour),
                             int(minute), int(second), tzinfo=timezone.utc)
            epoch = calendar.timegm(value.utctimetuple())
            if epoch <= 0 or epoch > time.time():
                return None
            observations.append(epoch)
        except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
            return None
    return observations[0] if observations[0] == observations[1] else None


def _retired_generations(records):
    """A failed process query is not evidence that an old process has exited."""
    import ctypes
    import sys
    import ccc_codex_queue as native
    from ccc_guard_scope import birth

    expected = {_generation(record) for record in records}
    if not expected or sys.platform != 'darwin' or native._proc_listpids is None:
        raise ValueError('complete process generation inventory unavailable')
    for _ in range(2):
        size = native._proc_listpids(1, 0, None, 0)
        if not 0 < size < 4 * 1024 * 1024:
            raise ValueError('complete process generation inventory unavailable')
        values = (ctypes.c_int * ((size + 16384) // 4))()
        count = native._proc_listpids(1, 0, values, ctypes.sizeof(values))
        if not 0 < count < ctypes.sizeof(values) or count % 4:
            raise ValueError('process generation inventory incomplete')
        present = set(values[:count // 4])
        for pid, previous in expected:
            if pid not in present:
                continue
            first, second = birth(pid), birth(pid)
            if first is None or first != second:
                if first is None and second is None:
                    coarse = _historical_start_second(pid)
                    if coarse is not None and coarse > previous[0]:
                        continue
                raise ValueError('historical process query uncertain')
            if tuple(first) == previous:
                raise ValueError('historical process generation still live')


def _current_config_clear(config, target, surface):
    """Keep current CCC pauses; an old enrollment's STOP is not a new-SID pause."""
    if config.get('global_paused', False) is not False or config.get('mode', 'armed') != 'armed':
        raise ValueError('current CCC mode is paused or not armed')
    selected = [target]
    for row in config.get('targets', []):
        if _uuid(row.get('surface_id')) == surface:
            selected.append(row)
    for row in config.get('workspace_rules', []):
        if _uuid(row.get('workspace_id')) == _uuid(target.get('workspace_id')):
            selected.append(row)
            if surface in row.get('excluded_surface_ids', []):
                raise ValueError('current surface excluded from CCC continuation')
    if surface in config.get('discovery_excluded_surface_ids', []):
        raise ValueError('current surface excluded from CCC discovery')
    if any(row.get('paused', False) is not False or row.get('enabled', True) is not True
           for row in selected):
        raise ValueError('current CCC workspace or target paused')


def _new_session_terminal_evidence(read, directory, value, ledger, item, sid,
                                   surface, target, turn, now, config):
    """Scope an ended failure to its authenticated new process and new session.

    This grants only a terminal-input prerequisite. It does not transfer the old
    enrollment, settle its ledger, publish a heartbeat or authorize Goal status
    writes. The existing sender must still check Goal, composer, queue, provider,
    current cmux membership and configuration immediately before input.
    """
    import ccc_guard_scope as scope
    import ccc_shared_codex_turn as shared
    from ccc_codex_queue import task_snapshot

    current_sid = str(uuid.UUID(turn['session_id']))
    if current_sid == sid or turn['session_id'] != current_sid:
        raise ValueError('new-session boundary unavailable')
    if (turn.get('kind') != 'task_complete' or not _finite(turn.get('at'))
            or turn['at'] > now or not isinstance(turn.get('error'), dict)
            or not isinstance(turn['error'].get('message'), str) or not turn['error']['message']):
        raise ValueError('new session has no current terminal failed turn')
    if _ownership(value, now)['state'] != 'none' or value['progress_after'] is not None:
        raise ValueError('historical compaction controller or progress fence still owns recovery')
    _current_config_clear(config, target, surface)
    live = scope.process(turn.get('pid'))
    if (not live or _generation(live) != _generation(turn)
            or live['birth'][0] != turn.get('process_start')
            or live.get('surface_id') != surface
            or live.get('environment_workspace_id') != _uuid(target.get('workspace_id'))):
        raise ValueError('current terminal process identity changed')
    born = live['birth'][0] + live['birth'][1] / 1e6
    born_ns = live['birth'][0] * 1_000_000_000 + live['birth'][1] * 1000
    previous = read(directory / 'read-only-observation.json')
    if (previous.get('session_id') != sid or _uuid(previous.get('surface_id')) != surface
            or previous.get('process_binding_verified') is not True
            or not _finite(previous.get('at')) or not previous['at'] < born):
        raise ValueError('historical observer session boundary unproven')
    old_processes = []
    for binding in (value.get('authenticated_binding'), previous.get('process_binding')):
        if (not isinstance(binding, dict) or not isinstance(binding.get('identity'), dict)
                or _uuid(binding['identity'].get('workspace_uuid')) != _uuid(target.get('workspace_id'))
                or _uuid(binding['identity'].get('target_surface_uuid')) != surface):
            raise ValueError('historical enrollment process identity mismatch')
        record = binding.get('process')
        if _generation(record) == _generation(live):
            raise ValueError('session changed inside the historical process; isolation denied')
        old_processes.append(record)
    _retired_generations(old_processes)

    # Retain even UNKNOWN history, but require its original, pre-process scope.
    old_binding = (ledger or {}).get('binding') or {}
    old_facts = (value.get('native') or {}).get('facts') or {}
    if (not isinstance(ledger, dict) or ledger.get('version') != LEDGER_VERSION
            or old_binding.get('session_id') != sid or not old_binding.get('path')
            or old_binding.get('path') != old_facts.get('path')
            or (item.get('path') is not None and old_binding['path'] != item['path'])
            or not isinstance(old_binding.get('file_id'), list) or len(old_binding['file_id']) != 2
            or old_binding['file_id'] != old_facts.get('file_id')
            or not isinstance(ledger.get('unknown'), list)
            or any(not isinstance(x, str) for x in ledger['unknown'])
            or type(ledger.get('initial_coverage')) is not bool
            or any(not isinstance(ledger.get(k), dict) for k in ('entries', 'intents'))):
        raise ValueError('historical input ledger session identity or structure unproven')
    for name in ('state.json', 'pending-inputs.json'):
        if max(signature(directory / name)[3:5]) >= born_ns:
            raise ValueError('historical evidence changed in current process lifetime')
    if not _finite(value.get('observed_at')) or value['observed_at'] >= born:
        raise ValueError('historical controller boundary unproven')
    entries = list(ledger['entries'].values()) + list(ledger['intents'].values())
    if any(not isinstance(entry, dict) or not _finite(entry.get('at')) or entry['at'] >= born
           or any(entry.get(k) == turn.get('turn_id') for k in
                  ('expected_turn_id', 'receipt_turn_id', 'submission_id')) for entry in entries):
        raise ValueError('current-process pending input cannot be isolated')

    proof = shared.binding_for_turn(target, turn)
    if (not proof or proof.get('process') != live or proof.get('session_id') != current_sid
            or str(proof.get('path')) != turn.get('transcript_path')
            or str(proof.get('path')) == old_binding['path']
            or not shared.valid(target, proof)):
        raise ValueError('current foreground writer or session binding unverified')
    path = Path(proof['path'])
    pin = signature(path)
    facts = _new_session_rollout_facts(path, current_sid, live, pin)
    observed = facts.get('observed_turn') or {}
    tail = turn.get('terminal_tail') or {}
    if (facts.get('session_id') != current_sid or facts.get('caught_up') is not True
            or facts.get('receipt_overflow') is not False or facts.get('lifecycle_error') is not None
            or facts.get('file_id') != pin[:2] or facts.get('offset') != pin[2]
            or turn.get('signature') != pin[1:4]
            or observed.get('id') != turn.get('turn_id') or observed.get('status') != 'task_complete'
            or not _finite(observed.get('started_at'))
            or not _finite(facts.get('session_created_at')) or facts['session_created_at'] <= 0
            or facts['session_created_at'] > observed['started_at']
            or not born <= observed['started_at'] <= turn['at']
            or observed.get('ended_at') != turn['at']
            or tail.get('end') != pin[2] or tail.get('offset') != observed.get('end_offset')
            or facts.get('user_offset', -1) >= observed.get('end_offset', -1)):
        raise ValueError('new-session rollout is incomplete, changed or not a current failed turn')
    snapshot = task_snapshot(path, current_sid)
    if (not snapshot or any(snapshot.get(k) != turn.get(k) for k in
            ('kind', 'at', 'turn_id', 'error', 'signature', 'terminal_tail'))):
        raise ValueError('new-session failure no longer matches the stable rollout')
    _retired_generations(old_processes)
    if (scope.process(turn['pid']) != live or not shared.valid(target, proof)
            or signature(path) != pin):
        raise ValueError('new-session process, writer or rollout changed during final check')
    evidence = dict(state='clear', scope='new_session_failed_turn', session_id=current_sid,
        reason='new-session failure independently verified; retired enrollment evidence retained',
        historical_session_id=sid, historical_unknown_retained=bool(ledger['unknown'] or not ledger['initial_coverage']),
        historical_entries_retained=len(entries), process_birth=live['birth'],
        retired_processes=[{'pid': row['pid'], 'birth': row['birth']} for row in old_processes])
    return evidence, dict(state='healthy', observed_at=now, mode='inline_native_failure'), {
        'unsettled_compaction': None, 'paused': False}


def _heartbeat_refresh(before, after, fields, now):
    """Only a fresh, monotonic clock update can justify a full reinspection."""
    if set(before) != set(after):
        return False
    stable = lambda value: json.dumps(
        {key: item for key, item in value.items() if key not in fields},
        sort_keys=True, separators=(',', ':'), allow_nan=False)
    # JSON comparison is type-sensitive: True and 1 are different evidence.
    if stable(before) != stable(after):
        return False
    for key in fields & before.keys():
        previous, current = before[key], after[key]
        if previous is None and current is None and key != 'at':
            continue
        if (not _finite(previous) or not _finite(current)
                or not previous <= current <= now or now - current > MAX_AGE):
            return False
    return True


class _InspectionSnapshot:
    """Single-read snapshots, with strict anchors across at most one retry."""
    def __init__(self):
        self.anchors = {}
        self.heartbeats = {}
        self.observed_heartbeats = {}
        self.begin()

    def begin(self):
        self.values, self.pins, self.errors = {}, {}, {}

    def enroll_heartbeats(self, directory):
        self.heartbeats = {
            directory / 'worker-health.json': frozenset(('at', 'read_only_observed_at')),
            directory / 'read-only-observation.json': frozenset(('at',)),
        }

    def read(self, path):
        path = Path(path)
        if path in self.errors:
            raise self.errors[path]
        if path in self.values:
            return self.values[path]
        try:
            value, pin = read_json(path)
            if path in self.anchors:
                original, original_pin = self.anchors[path]
                if pin != original_pin and path not in self.heartbeats:
                    raise ValueError('compaction or input evidence changed during final check: ' + path.name)
                if path in self.heartbeats:
                    previous = self.observed_heartbeats.get(path, original)
                    if not _heartbeat_refresh(previous, value, self.heartbeats[path], time.time()):
                        raise ValueError('compaction observation semantics changed during final check: ' + path.name)
            else:
                self.anchors[path] = (copy.deepcopy(value), pin)
            if path in self.heartbeats:
                self.observed_heartbeats[path] = copy.deepcopy(value)
            self.values[path], self.pins[path] = value, pin
            return value
        except ERRORS as exc:
            self.errors[path] = exc
            raise

    def changed(self):
        heartbeat_changed = False
        # Include anchors even if a later branch did not consume that file.
        for path, (original, original_pin) in self.anchors.items():
            pin = self.pins.get(path, original_pin)
            if signature(path) == pin:
                continue
            if path not in self.heartbeats:
                raise ValueError('compaction or input evidence changed during final check: ' + path.name)
            fresh, _ = read_json(path)
            previous = self.observed_heartbeats.get(path, self.values.get(path, original))
            if not _heartbeat_refresh(previous, fresh,
                                      self.heartbeats[path], time.time()):
                raise ValueError('compaction observation semantics changed during final check: ' + path.name)
            # Preserve every observed timestamp, including this intermediate
            # read. The next inspection cannot regress to an earlier value.
            self.observed_heartbeats[path] = copy.deepcopy(fresh)
            heartbeat_changed = True
        return heartbeat_changed


def inspect(config_path, target, *, terminal_turn=None):
    """Only input_permission.allowed admits input to the existing native gates.

    goal_status_permission admits only Goal status recovery without objective updates.
    Neither verdict replaces fresh native Goal/turn/process checks.
    """
    snapshot = _InspectionSnapshot()
    denials = {}
    permissions = ('input_permission', 'goal_status_permission')
    for attempt in range(2):
        snapshot.begin()
        result = _inspect_once(config_path, target, terminal_turn, snapshot)
        result['heartbeat_retries'] = attempt
        if attempt == 0:
            denials = {key: dict(result[key]) for key in permissions if not result[key]['allowed']}
        else:
            # A clock refresh must never upgrade an earlier semantic denial,
            # even when the other operation had valid permission.
            result.update(denials)
        try:
            if not snapshot.changed():
                return result
            if not any(result[key]['allowed'] for key in permissions):
                return result
            if attempt == 0:
                continue
            reason = 'compaction heartbeat changed again during final check; bounded recheck exhausted'
        except ERRORS as exc:
            reason = str(exc)[:160]
        for key in permissions:
            result[key] = dict(allowed=False, state='blocked', reason=reason)
        return result


def _inspect_once(config_path, target, terminal_turn, snapshot):
    result = dict(enrolled=None, policy_version=2, checked_at=time.time(),
                  strategy='terminal_continue' if terminal_turn is not None else 'legacy',
                  controller_ownership=dict(state='unknown', phase=None),
                  observer_health=dict(state='unavailable'),
                  goal_status_permission=dict(allowed=False, state='unknown', reason='compaction enrollment unverified'),
                  input_permission=dict(allowed=False, state='unknown', reason='compaction enrollment unverified'))
    read = snapshot.read

    try:
        config = read(Path(config_path))
        cfg = config.get('codex_compact_watchdog', {})
        if not isinstance(cfg, dict):
            raise ValueError('invalid compaction enrollment')
        if cfg.get('enabled') is not True:
            if cfg.get('enabled') not in (None, False):
                raise ValueError('invalid compaction enabled flag')
            item = None
        else:
            surface = _uuid(target.get('surface_id'))
            items = cfg.get('targets')
            if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
                raise ValueError('invalid compaction targets')
            matches = [x for x in items if _uuid(x.get('surface_id')) == surface]
            if len(matches) > 1:
                raise ValueError('duplicate compaction enrollment')
            item = matches[0] if matches else None
        if item is None:
            return dict(enrolled=False, policy_version=2, checked_at=result['checked_at'],
                        strategy=result['strategy'], controller_ownership=dict(state='none', phase=None),
                        observer_health=dict(state='not_enrolled'),
                        goal_status_permission=dict(allowed=True, state='not_enrolled', reason='no compaction enrollment'),
                        input_permission=dict(allowed=True, state='not_enrolled', reason='no compaction enrollment'))
        result['enrolled'] = True
        sid = str(uuid.UUID(item['session_id']))
        if _uuid(item['workspace_id']) != _uuid(target.get('workspace_id')):
            raise ValueError('enrolled surface moved')
        directory = Path(cfg['state_dir']) / sid
        snapshot.enroll_heartbeats(directory)
        value = read(directory / 'state.json')
        if (value.get('session_id') != sid or _uuid(value.get('surface_id')) != surface
                or _uuid(value.get('workspace_id')) != _uuid(item['workspace_id'])):
            raise ValueError('compaction state identity mismatch')
        now = time.time()
        result['controller_ownership'] = _ownership(value, now)
        try:
            health = read(directory / 'worker-health.json')
            result['observer_health'] = _observer(value, health, sid, surface, now)
        except ERRORS as exc:
            result['observer_health'] = dict(state='unavailable', reason=str(exc)[:160])
        ledger = None
        try:
            ledger = read(directory / 'pending-inputs.json')
            result['input_evidence'] = _input_evidence(value, ledger, item, sid)
        except ERRORS as exc:
            result['input_evidence'] = dict(state='unknown', reason=str(exc)[:160])
        try:
            goal_evidence = _goal_status_evidence(value, ledger, item, sid)
        except ERRORS as exc:
            goal_evidence = dict(state='unknown', reason=str(exc)[:160])
        ownership = result['controller_ownership']
        observer = result['observer_health']
        evidence = result['input_evidence']
        native = value.get('native') or {}
        goal_observer, goal_native = observer, native
        different_session = (terminal_turn is not None and (
            not isinstance(terminal_turn, dict) or terminal_turn.get('session_id') != sid))
        if different_session:
            # Reject the old Goal proof before attempting the new-session
            # checks: a failed check must not fall back to another session.
            goal_evidence = dict(state='unknown', reason='Goal status evidence belongs to another session')
        if terminal_turn is not None and ownership['state'] == 'none':
            try:
                if different_session:
                    evidence, observer, current = _new_session_terminal_evidence(
                        read, directory, value, ledger, item, sid, surface, target, terminal_turn, now, config)
                else:
                    evidence, observer, current = _terminal_evidence(
                        read, directory, value, ledger, item, sid, surface, target, terminal_turn, now)
                    goal_observer, goal_native = observer, dict(
                        compact=current['unsettled_compaction'], paused=current['paused'])
                result['input_evidence'], result['observer_health'] = evidence, observer
                native = dict(compact=current['unsettled_compaction'], paused=current['paused'])
                result['failure_fingerprint'] = hashlib.sha256(json.dumps(
                    {k: terminal_turn.get(k) for k in ('session_id', 'pid', 'birth', 'turn_id', 'at', 'signature')},
                    sort_keys=True).encode()).hexdigest()
            except ERRORS as exc:
                evidence = result['input_evidence'] = dict(state='unknown', reason=str(exc)[:160])
        for permission, proof in (('input_permission', evidence), ('goal_status_permission', goal_evidence)):
            permission_observer = observer if permission == 'input_permission' else goal_observer
            permission_native = native if permission == 'input_permission' else goal_native
            if ownership['state'] != 'none':
                reason = ownership.get('reason', 'compaction ownership unknown')
            elif proof['state'] != 'clear':
                reason = proof['reason']
            elif permission_observer['state'] != 'healthy':
                reason = permission_observer['reason']
            elif permission_native.get('compact') is not None:
                reason = 'current native compaction active; automatic control blocked'
            elif value['progress_after'] is not None:
                reason = 'post-compaction progress fence still pending'
            elif permission_native.get('paused') is not False:
                reason = 'compaction target paused or pause state unknown'
            else:
                result[permission] = dict(allowed=True, state='clear', reason=proof['reason'])
                continue
            result[permission] = dict(allowed=False, state='blocked', reason=reason)
    except ERRORS as exc:
        reason = str(exc)[:160]
        result['observer_health'] = dict(state='unavailable', reason=reason)
        result['input_permission'] = dict(allowed=False, state='unknown', reason=reason)
        result['goal_status_permission'] = dict(allowed=False, state='unknown', reason=reason)
    return result


def owner(config_path, target):
    """Actual controller ownership only; never use this as input permission."""
    held = inspect(config_path, target)['controller_ownership']
    return held if held['state'] == 'held' else None


def status(config_path, target, *, inspection=None, goal_status_only=False):
    verdict = inspection if inspection is not None else inspect(config_path, target)
    permission = verdict['goal_status_permission' if goal_status_only else 'input_permission']
    if permission['allowed']:
        return None
    ownership = verdict['controller_ownership']
    observer = verdict['observer_health']
    phase = ownership.get('phase') if ownership['state'] == 'held' else 'unavailable'
    controller = ownership['state'] + (':' + ownership['phase'] if ownership.get('phase') else '')
    reason = (f"controller={controller}; observer={observer['state']}; "
              f"{'goal_status' if goal_status_only else 'input'}=blocked: {permission['reason']}")
    if ownership['state'] == 'held':
        reason = f"compact {ownership['elapsed_floor']}s; warn 180s / recover 300s; " + reason
    return dict(kind='codex_compact_' + phase, reason=reason)
