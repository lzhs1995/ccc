"""Advisory receipt follow-up for the original guard; no recovery decisions."""
import copy
import json
import os
from pathlib import Path
import stat
import time

import ccc_recovery_progress as progress

BINDING = ('key', 'session', 'pane_id', 'terminal_id', 'pid', 'birth',
           'server', 'cmux_workspace_id', 'cmux_surface_id')
SCAN_BYTES = 256 * 1024
MAX_INSPECTIONS = 2
TIME_BUDGET = 0.20
REFRESH_SECONDS = 30
CLAUDE_WARNING_SECONDS = 180
CLAUDE_ATTENTION_SECONDS = 300


class ProgressMonitor:
    def __init__(self, state, birth, *, clock=time.monotonic, wall=time.time, inspect=progress.inspect,
                 inspect_claude=progress.inspect_claude):
        self.path = Path(state) / 'herdr-codex-input-ledger.json'
        self.birth, self.clock, self.wall, self.inspect = birth, clock, wall, inspect
        self.cache, self.prior_cache, self.cursor = {}, {}, 0
        self.inspect_claude = inspect_claude
        self.claude_cache, self.claude_cursors = {}, {}

    def codex_observe(self, attempt, row):
        try:
            return self.inspect(attempt, row, self.birth, max_span=SCAN_BYTES)
        except Exception as error:
            return {'phase': 'evidence_unknown', 'native_receipt_verified': False,
                    'reason': str(error)[:180]}

    def codex_attempt_index(self, attempts, now):
        # Keep the existing latest/prior ordering, but isolate all validation
        # and indexing failures to this provider, including malformed values.
        latest, prior = {}, {}
        for key, attempt in attempts.items():
            if not isinstance(key, str) or not isinstance(attempt, dict) or not isinstance(attempt.get('key'), str):
                raise ValueError('invalid original progress attempt')
            at = attempt.get('attempted_at')
            if type(at) not in (int, float) or not 0 < at <= now + 1:
                raise ValueError('invalid original attempt time')
            old = latest.get(attempt['key'])
            if old is None or (at, key) > (old[1]['attempted_at'], old[0]):
                latest[attempt['key']] = (key, attempt)
        for key, attempt in attempts.items():
            if (attempt.get('phase') != 'native_received' or attempt.get('route') != 'continue'
                    or latest[attempt['key']][0] == key):
                continue
            previous = prior.get(attempt['key'])
            if previous is None or (attempt['attempted_at'], key) > (previous[1]['attempted_at'], previous[0]):
                prior[attempt['key']] = (key, attempt)
        return latest, prior

    def claude_attempt_index(self, attempts, now):
        # The original guard supplies this exact in-memory mapping. Do not read
        # its ledger again, attach it to snapshots, or modify any of its values.
        if not isinstance(attempts, dict) or len(attempts) > 65536:
            raise ValueError('original Claude attempts unavailable or over bound')
        latest, prior = {}, {}
        for attempt_id, attempt in attempts.items():
            if not isinstance(attempt_id, str) or not isinstance(attempt, dict):
                raise ValueError('invalid original Claude attempt')
            key, at = attempt.get('key'), attempt.get('attempted_at')
            if not isinstance(key, str) or type(at) not in (int, float) or not 0 < at <= now + 1:
                raise ValueError('invalid original Claude attempt identity/time')
            previous = latest.get(key)
            if previous is None or (at, attempt_id) > (previous[1]['attempted_at'], previous[0]):
                latest[key] = (attempt_id, attempt)
        for attempt_id, attempt in attempts.items():
            key = attempt['key']
            if attempt.get('phase') != 'native_received' or latest[key][0] == attempt_id:
                continue
            previous = prior.get(key)
            if previous is None or (attempt['attempted_at'], attempt_id) > (previous[1]['attempted_at'], previous[0]):
                prior[key] = (attempt_id, attempt)
        return latest, prior

    def claude_observe(self, pair, row, metrics, started, now, *, slot=None, allow_scan=True):
        attempt_id, attempt = pair
        cache_key = (row['key'], attempt_id)
        identity = progress.claude_identity(attempt, row)
        cached = self.claude_cache.get(cache_key)
        if cached and cached['identity'] != identity:
            self.claude_cache.pop(cache_key, None)
            self.claude_cursors.pop(cache_key, None)
            cached = None
        # Cached evidence is historical, but endpoint/process changes invalidate
        # even that current presentation; no stale positive binding survives.
        progress.claude_binding(attempt, row, self.birth)
        if cached and 0 <= now - cached['at'] < REFRESH_SECONDS:
            metrics['cache_hits'] += 1
            return dict(copy.deepcopy(cached['summary']), evidence_is_cached=True)
        if not allow_scan or metrics['inspections'] >= MAX_INSPECTIONS or self.clock() - started >= TIME_BUDGET:
            metrics['deferred'] += 1
            pending = {'attempt_id': attempt_id, 'phase': 'pending_observation',
                       'native_receipt_verified': False}
            if cached:
                pending['last_observed_outcome'] = copy.deepcopy(cached['summary'])
            return pending
        metrics['inspections'] += 1
        metrics['claude_inspections'] += 1
        if slot is not None:
            metrics['_last_inspection_slot'] = slot
        result = self.inspect_claude(attempt, row, self.birth, max_span=SCAN_BYTES,
                                     cursor=self.claude_cursors.get(cache_key))
        summary = {name: copy.deepcopy(result.get(name)) for name in
                   ('phase', 'native_receipt_verified', 'receipt', 'receipt_at', 'reason', 'fixed_span',
                    'scan_limited', 'partial_record', 'next_user', 'terminal', 'last_activity',
                    'substantive_assistant_count', 'tool_calls_observed', 'tool_errors_observed',
                    'tool_outcomes_unknown', 'tool_content_missing_observed')}
        summary.update(attempt_id=attempt_id, evidence_observed_at=now,
                       evidence_is_cached=False,
                       tool_pairs_observed=len(result.get('tool_pairs', [])),
                       errors_observed=len(result.get('errors', [])),
                       api_errors=copy.deepcopy(result.get('errors', [])),
                       last_tool_pair=copy.deepcopy((result.get('tool_pairs') or [None])[-1]),
                       last_reply=copy.deepcopy((result.get('replies') or [None])[-1]),
                       historical_spans_not_revalidated=True)
        if cached and result.get('phase') == 'evidence_unknown':
            old = cached['summary']
            summary['last_observed_outcome'] = copy.deepcopy(old.get('last_observed_outcome', old))
        cursor = result.get('cursor')
        if cursor is not None:
            self.claude_cursors[cache_key] = copy.deepcopy(cursor)
            summary['cursor'] = {'next_offset': cursor['state']['next_offset'],
                                 'observed_file_size': cursor['observed_file_size'],
                                 'anchor': copy.deepcopy(cursor['anchor'])}
        # On uncertainty retain the prior cursor, not partly decoded new state.
        self.claude_cache[cache_key] = {'identity': identity, 'at': now, 'summary': copy.deepcopy(summary)}
        return summary

    def claude_row(self, row, latest, prior, ledger_error, metrics, started, now,
                   *, row_slot=None, allow_current_scan=True):
        health = {'schema': 'ccc-native-claude-progress-r17', 'provider': 'claude', 'mode': 'read_only',
                  'binding': {name: copy.deepcopy(row.get(name)) for name in BINDING},
                  'native_session_id': copy.deepcopy(row.get('native_session_id')),
                  'input_operations': 0, 'budget_changes': 0, 'business_success_claimed': False,
                  'control_verified': False, 'recovery_available': False, 'observed_at': now,
                  'phase': 'no_original_receipt', 'native_receipt_verified': False,
                  'substantive_assistant_count': 0, 'tool_pairs_observed': 0, 'errors_observed': 0,
                  'attention_required': False, 'advisory_only': True}
        if ledger_error:
            health.update(phase='pending_original_ledger', reason=ledger_error)
            return health
        pair, previous = latest.get(row['key']), prior.get(row['key'])
        if pair:
            attempt_id, attempt = pair
            health.update(attempt_id=attempt_id, attempt_phase=attempt.get('phase'))
            if attempt.get('phase') != 'native_received':
                health['phase'] = 'awaiting_native_receipt'
            else:
                try:
                    health.update(self.claude_observe(pair, row, metrics, started, now,
                                                     slot=row_slot, allow_scan=allow_current_scan))
                except Exception as error:
                    health.update(phase='evidence_unknown', reason=str(error)[:180])
        if previous:
            try:
                history = self.claude_observe(previous, row, metrics, started, now,
                                             slot=row_slot + 1 if row_slot is not None else None)
                health['previous_attempt_outcome'] = history
            except Exception as error:
                health['previous_attempt_outcome'] = {'attempt_id': previous[0], 'phase': 'evidence_unknown',
                                                      'reason': str(error)[:180]}
        receipt_at = health.get('receipt_at')
        if health.get('native_receipt_verified') and type(receipt_at) in (int, float) and receipt_at <= now:
            health['seconds_since_receipt'] = now - receipt_at
            activity = health.get('last_activity')
            health['seconds_since_native_activity'] = (max(0.0, now - progress.native_time(activity['at']))
                                                       if activity else None)
            silent = health['phase'] == 'no_assistant_yet'
            health['no_assistant_warning'] = silent and now - receipt_at >= CLAUDE_WARNING_SECONDS
            health['prolonged_no_assistant'] = silent and now - receipt_at >= CLAUDE_ATTENTION_SECONDS
        else:
            health.update(seconds_since_receipt=None, seconds_since_native_activity=None,
                          no_assistant_warning=False, prolonged_no_assistant=False)
        history = health.get('previous_attempt_outcome', {})
        if history.get('phase') in ('pending_observation', 'evidence_unknown'):
            history = history.get('last_observed_outcome', {})
        health['previous_failure_without_current_progress'] = bool(
            history.get('native_receipt_verified') and history.get('errors_observed', 0) > 0
            and not (health.get('substantive_assistant_count') or health.get('tool_pairs_observed')))
        health['attention_required'] = bool(health.get('errors_observed') or health.get('tool_errors_observed')
                                            or health.get('prolonged_no_assistant')
                                            or health['previous_failure_without_current_progress'])
        return health

    def ledger(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022
                    or st.st_size > 16 * 1024 * 1024):
                raise ValueError('original progress ledger unsafe or over bound')
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('original progress ledger over bound')
        d = progress.decode(raw)
        if d.get('schema') != 'herdr-codex-input-ledger-v176' or not isinstance(d.get('attempts'), dict) or len(d['attempts']) > 4096:
            raise ValueError('original progress ledger schema changed')
        return d['attempts']

    def tick(self, snapshot, claude_attempts=None):
        started, now = self.clock(), self.wall()
        metrics = {'schema': 'ccc-recovery-progress-monitor-r10', 'mode': 'read_only',
                   'input_operations': 0, 'budget_changes': 0, 'business_success_claimed': False,
                   'inspections': 0, 'cache_hits': 0, 'deferred': 0,
                   'max_scan_bytes_per_panel': SCAN_BYTES, 'max_panels_per_cycle': MAX_INSPECTIONS,
                   'time_budget_seconds': TIME_BUDGET, 'attention_required': 0,
                   'claude_attention_required': 0, 'claude_inspections': 0}
        rows = [r for r in snapshot.get('agents', []) if r.get('agent') in ('codex', 'claude')]
        try:
            if len(rows) > 96:
                raise ValueError('panel bound exceeded')
            latest, prior, codex_ledger_error = {}, {}, None
            if any(r.get('agent') == 'codex' for r in rows):
                try:
                    latest, prior = self.codex_attempt_index(self.ledger(), now)
                except Exception as error:
                    codex_ledger_error = str(error)[:180]
                    metrics['codex_observation_error'] = codex_ledger_error
            claude_latest, claude_prior, claude_ledger_error = {}, {}, None
            if any(r.get('agent') == 'claude' for r in rows):
                try:
                    claude_latest, claude_prior = self.claude_attempt_index(claude_attempts, now)
                except Exception as error:
                    claude_ledger_error = str(error)[:180]
                    metrics['claude_observation_error'] = claude_ledger_error
            # Each row owns two positions: current, then prior. Resume after
            # the actual inspected attempt, including a deferred prior when
            # the current attempt consumed the final slot of the last tick.
            slot_count = max(1, 2 * len(rows))
            self.cursor %= slot_count
            start_row = self.cursor // 2
            ordered = list(enumerate(rows))[start_row:] + list(enumerate(rows))[:start_row]
            next_cursor = (self.cursor + 1) % slot_count
            seen = set()
            for row_index, row in ordered:
                key = row['key']
                seen.add(key)
                row_slot = 2 * row_index
                allow_current_scan = not (row_index == start_row and self.cursor % 2)
                if row.get('agent') == 'claude':
                    row['native_recovery_progress'] = self.claude_row(
                        row, claude_latest, claude_prior, claude_ledger_error, metrics, started, now,
                        row_slot=row_slot, allow_current_scan=allow_current_scan)
                    metrics['claude_attention_required'] += bool(row['native_recovery_progress']['attention_required'])
                    continue
                health = {'schema': 'ccc-native-recovery-progress-r10', 'mode': 'read_only',
                          'control_verified': False, 'recovery_available': False,
                          'input_operations': 0, 'budget_changes': 0, 'business_success_claimed': False,
                          'binding': {name: copy.deepcopy(row.get(name)) for name in BINDING},
                          'native_session_id': row.get('native_session_id'), 'observed_at': now,
                          'attention_required': False, 'phase': 'no_original_receipt',
                          'tool_pairs_observed': 0, 'errors_observed': 0}
                pair = latest.get(key)
                if codex_ledger_error:
                    health.update(phase='pending_original_ledger', reason=codex_ledger_error)
                if pair:
                    attempt_id, a = pair
                    health.update(attempt_id=attempt_id, attempt_phase=a.get('phase'))
                    if a.get('phase') != 'native_received':
                        health['phase'] = ('awaiting_native_receipt' if a.get('phase') in
                            {'submitted', 'write_unknown', 'receipt_unknown', 'intent'} else 'no_original_receipt')
                        self.cache.pop(key, None)
                    elif a.get('route') != 'continue':
                        health['phase'] = 'not_continue_receipt'
                    elif not row.get('native_reader_available') or not progress.selection_verified(row):
                        health['phase'] = 'selection_unverified'
                        self.cache.pop(key, None)
                    else:
                        identity = json.dumps([attempt_id, health['binding'], row.get('native_session_id'),
                                               row.get('native_signature'), a.get('receipt')], sort_keys=True)
                        cached = self.cache.get(key)
                        if cached and cached['identity'] == identity and 0 <= now - cached['at'] < REFRESH_SECONDS:
                            health.update(copy.deepcopy(cached['summary']))
                            metrics['cache_hits'] += 1
                        elif not allow_current_scan or metrics['inspections'] >= MAX_INSPECTIONS or self.clock() - started >= TIME_BUDGET:
                            health['phase'] = 'pending_observation'
                            metrics['deferred'] += 1
                        else:
                            result = self.codex_observe(a, row)
                            metrics['inspections'] += 1
                            metrics['_last_inspection_slot'] = row_slot
                            summary = {'phase': result['phase'],
                                       'native_receipt_verified': result.get('native_receipt_verified', False),
                                       'tool_pairs_observed': len(result.get('tool_pairs', [])),
                                       'errors_observed': len(result.get('errors', [])),
                                       'opaque_writer_refused': any(e.get('opaque_writer_refused') for e in result.get('errors', [])),
                                       'fixed_span': result.get('fixed_span'),
                                       'receipt': result.get('receipt'), 'reason': result.get('reason'),
                                       'attention_required': result['phase'] in ('failed_again', 'error_after_tool_activity')}
                            self.cache[key] = {'identity': identity, 'at': now, 'summary': copy.deepcopy(summary)}
                            health.update(summary)
                previous = prior.get(key)
                if previous and row.get('native_reader_available') and progress.selection_verified(row):
                    prior_id, prior_attempt = previous
                    identity = json.dumps([prior_id, health['binding'], row.get('native_session_id'),
                                           prior_attempt], sort_keys=True)
                    cached = self.prior_cache.get(key)
                    history = None
                    if (cached and cached['identity'] == identity
                            and 0 <= now-cached['at'] < REFRESH_SECONDS):
                        # Closed old spans are historical evidence, not claims
                        # that the current turn has failed or recovered.
                        history = copy.deepcopy(cached['summary'])
                        metrics['cache_hits'] += 1
                    elif metrics['inspections'] < MAX_INSPECTIONS and self.clock()-started < TIME_BUDGET:
                        result = self.codex_observe(prior_attempt, row)
                        metrics['inspections'] += 1
                        metrics['_last_inspection_slot'] = row_slot + 1
                        history = {'attempt_id': prior_id, 'phase': result['phase'],
                                   'native_receipt_verified': result.get('native_receipt_verified', False),
                                   'tool_pairs_observed': len(result.get('tool_pairs', [])),
                                   'errors_observed': len(result.get('errors', [])),
                                   'evidence_observed_at': now, 'receipt': result.get('receipt'),
                                   'fixed_span': result.get('fixed_span')}
                        self.prior_cache[key] = {'identity': identity, 'at': now, 'summary': copy.deepcopy(history)}
                    else:
                        metrics['deferred'] += 1
                    if history:
                        health['previous_attempt_outcome'] = history
                history = health.get('previous_attempt_outcome', {})
                health['previous_failure_without_current_progress'] = bool(
                    history.get('native_receipt_verified') and history.get('errors_observed', 0) > 0
                    and history.get('phase') in {'superseded_after_failure', 'failed_again', 'error_after_tool_activity'}
                    and health['tool_pairs_observed'] == 0
                    and health['phase'] in {'awaiting_native_receipt', 'pending_observation', 'no_tool_result_yet',
                                            'evidence_unknown', 'scan_limit_unknown', 'partial_record_unknown'})
                row['native_recovery_progress'] = health
                metrics['attention_required'] += bool(health['attention_required'] or health['previous_failure_without_current_progress'])
            self.cache = {k: v for k, v in self.cache.items() if k in seen}
            self.prior_cache = {k: v for k, v in self.prior_cache.items() if k in seen}
            active_claude = {(key, pair[0]) for index in (claude_latest, claude_prior)
                            for key, pair in index.items() if key in seen}
            # Keep only current/prior attempts for each active row. No dynamic
            # attempts or cursor graph are published into the shared snapshot.
            if claude_ledger_error is None:
                self.claude_cache = {k: v for k, v in self.claude_cache.items() if k in active_claude}
                self.claude_cursors = {k: v for k, v in self.claude_cursors.items() if k in active_claude}
            if '_last_inspection_slot' in metrics:
                next_cursor = (metrics['_last_inspection_slot'] + 1) % slot_count
            self.cursor = next_cursor
        except Exception as error:
            # This diagnostic cannot suppress the original capture/send path or
            # mark its receipt ledger failed. Its own uncertainty is visible.
            metrics['observation_error'] = str(error)[:180]
            metrics['attention_required'] = 0
            metrics['claude_attention_required'] = 0
            for row in rows:
                row.pop('native_recovery_progress', None)
        metrics.pop('_last_inspection_slot', None)
        metrics['elapsed_seconds'] = max(0.0, self.clock() - started)
        snapshot['codex_progress_attention_required'] = metrics['attention_required']
        snapshot['claude_progress_attention_required'] = metrics['claude_attention_required']
        metrics['total_attention_required'] = metrics['attention_required'] + metrics['claude_attention_required']
        return metrics
