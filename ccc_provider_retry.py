"""Durable provider-error remedies, independent of UI episodes and turn IDs.

Reservations precede input. An uncertain input consumes its reservation; neither
an idle frame nor a process restart refunds it. This module never sends input.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import time


RETRYABLE = frozenset({'rate_limit', 'http_500', 'http_502', 'http_503', 'http_504', 'http_524'})
BLOCKED_REASONS = {
    'token_exhausted': 'provider token out of quota; needs a new key or a top-up',
    'invalid_encrypted_content': 'provider rejected encrypted session content; repair required; session preserved',
    'http_400': 'provider request rejected (400); inspect parameters/protocol; session preserved',
    'http_401': 'provider authentication rejected (401); verify credentials/quota; session preserved',
    'http_403': 'provider permission rejected (403); verify access policy; session preserved',
    'resource_mismatch': 'provider rejected an item from a different OpenAI resource; restore original resource affinity; session preserved',
    'invalid_request': 'provider rejected the request; inspect parameters/protocol; session preserved',
    'provider_error_unknown': 'unrecognized structured provider error; inspect original failure before retrying; session preserved',
}
BLOCKED = frozenset(BLOCKED_REASONS)
RATE_LIMIT_RECOVERY_INTERVAL = 0.25
RATE_LIMIT_PROVIDER_SPACING = 0.05


def structured_error_type(message):
    """Classify a complete provider error object, including hard-wrapped JSON.

    Callers must first establish a live error card or a native failed turn.
    This is not a detector for arbitrary transcript, documentation or drafts.
    The error's type/code and resource ownership take priority over a generic
    stream-disconnected wrapper; a missing HTTP status must not imply health.
    """
    compact = re.sub(r'\s+', '', str(message)).lower()
    start = compact.find('{')
    if start < 0:
        return None
    prefix = compact[:start].lstrip('■⚠└')
    if prefix.startswith(('documentation:', 'example:', 'quoted:', 'sample:')):
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(compact[start:])
    except ValueError:
        return None
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict) or not error:
        return None
    codes = {error[key] for key in ('code', 'type') if isinstance(error.get(key), str)}
    detail = error.get('message') if isinstance(error.get('message'), str) else ''
    if (codes & {'resource_mismatch', 'resource_mismatch_error'} or (
            'therequesteditemwascreatedunderadifferent' in detail
            and 'openairesource' in detail and 'usethesameresource' in detail)):
        return 'resource_mismatch'
    if 'insufficient_quota' in codes:
        return 'token_exhausted'
    if 'invalid_encrypted_content' in codes:
        return 'invalid_encrypted_content'
    # Whitespace has been removed to join native hard wraps, so a status
    # description immediately follows the last digit (429TooManyRequests).
    # A word boundary would reject this normal banner; reject another digit.
    status = re.search(
        r'(?:unexpectedstatus(?:code)?|unknownstatuscode|http(?:/\d(?:\.\d)?)?|laststatus)'
        r'[:=]?(400|401|403|429|500|502|503|504|524)(?!\d)', prefix)
    if status and status[1] in {'401', '403'}:
        if status[1] == '401' and any(token in compact for token in (
                '额度已用尽', 'remainquota', 'insufficient_quota', 'quotaexceeded')):
            return 'token_exhausted'
        return 'http_' + status[1]
    # Preserve the known parameter remedy even when providers emit a null
    # code and only put the 400 status in the message. Ownership, encrypted
    # content, quota and authentication failures above remain blockers.
    if ((error.get('param') == 'prompt_cache_retention' or 'prompt_cache_retention' in detail)
            and ('invalid_parameter' in codes or re.search(r'(?<!\d)400(?!\d)', compact))):
        return 'prompt_cache'
    # A specific rate-limit code is more informative than the generic
    # invalid_request_error type used by some providers for every failure.
    if codes & {'rate_limit_exceeded', 'rate_limit_error', 'rate_limited'}:
        return 'rate_limit'
    if codes & {'invalid_request_error', 'invalid_request', 'invalid_parameter'}:
        return 'invalid_request'
    # Explicit status still classifies message-only/unknown envelopes.
    if status:
        return 'rate_limit' if status[1] == '429' else 'http_' + status[1]
    return 'provider_error_unknown'


@dataclass(frozen=True)
class ProviderRetryDecision:
    """One transaction's result; never a shared last-verdict or send grant."""
    allowed: bool
    reason_code: str
    retry_at: float = 0.0


def retry_after(message, observed_at):
    """Return the server's absolute floor (seconds or RFC HTTP date)."""
    deadlines = []
    for match in re.finditer(r'(?im)\bretry-after\s*["\']?\s*[:=]\s*["\']?([^\r\n"\'}]+)', str(message)):
        value = match.group(1).strip()
        try:
            if re.fullmatch(r'\d+(?:\.\d+)?', value):
                deadline = observed_at + float(value)
            else:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    continue
                deadline = parsed.timestamp()
            if math.isfinite(deadline):
                deadlines.append(max(observed_at, deadline))
        except (ValueError, OverflowError, TypeError):
            continue
    return max(deadlines, default=0.0)


class ProviderRetryStore:
    def __init__(self, path, *, clock=time.time, jitter=None):
        self.path = Path(path)
        self.clock = clock
        self.jitter = jitter or (lambda: random.SystemRandom().uniform(0, .1))

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS episodes (identity TEXT PRIMARY KEY, record TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS cooldowns (provider TEXT PRIMARY KEY, until REAL NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS server_floors (provider TEXT PRIMARY KEY, until REAL NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS waiters (identity TEXT PRIMARY KEY, provider TEXT NOT NULL, stamp TEXT NOT NULL, queued_at REAL NOT NULL, seen_at REAL NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS rate_slots (provider TEXT PRIMARY KEY, until REAL NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def identity(session, provider):
        if not isinstance(session, str) or not session or not isinstance(provider, str) or not provider:
            raise ValueError('provider retry requires original session and provider identity')
        return hashlib.sha256(json.dumps([session, provider]).encode()).hexdigest()

    @staticmethod
    def _save(connection, key, record):
        connection.execute('INSERT OR REPLACE INTO episodes VALUES (?, ?)', (key, json.dumps(record)))

    def _pin_legacy_anchor(self, connection, key, record):
        # Older ledgers stored reservation + 120 seconds, but no reservation
        # timestamp. Preserve that conservative estimate before a new failure
        # changes due; otherwise every observation restarts the 900-second wait.
        if (record['count'] >= 4 and 'last_reserved_at' not in record
                and 'legacy_recovery_anchor' not in record):
            record['legacy_recovery_anchor'] = record['due'] - 120
            self._save(connection, key, record)

    def observe(self, session, provider, turn, error_type, message, at):
        """Observe one original failure; repeated polls never move its deadline."""
        if error_type not in RETRYABLE:
            return None
        if not turn or not math.isfinite(at) or at <= 0:
            raise ValueError('provider retry requires dated original failed turn')
        key = self.identity(session, provider)
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (key,)).fetchone()
            record = json.loads(row[0]) if row else dict(count=0, failure_at=0, success_at=0, seen={})
            self._pin_legacy_anchor(connection, key, record)
            if at <= record['success_at'] or at < record['failure_at']:
                return None
            stamp = hashlib.sha256(json.dumps([turn, float(at), error_type, message]).encode()).hexdigest()
            if stamp not in record['seen']:
                if len(record['seen']) >= 256 and error_type != 'rate_limit':
                    raise ValueError('provider failure history limit reached; success proof required')
                now = self.clock()
                delay = (RATE_LIMIT_RECOVERY_INTERVAL if error_type == 'rate_limit'
                         else (15, 30, 60, 120)[min(record['count'], 3)])
                noise = float(self.jitter())
                if not math.isfinite(noise) or not 0 <= noise <= .1:
                    raise ValueError('invalid retry jitter')
                server = retry_after(message, at)
                deadline = max((at + delay if error_type == 'rate_limit' else now + delay * (1 + noise)), server)
                record['seen'][stamp] = deadline
                # Keep bounded history without turning sustained transient load
                # into a permanent stop. Older failures are rejected above.
                while len(record['seen']) > 256:
                    del record['seen'][next(iter(record['seen']))]
                record['due'] = max(record.get('due', 0), deadline)
                record['failure_at'] = max(record['failure_at'], at)
                if server:
                    connection.execute('INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)', (provider, server))
                    connection.execute('INSERT INTO server_floors VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)', (provider, server))
                self._save(connection, key, record)
            return {'identity': key, 'provider': provider, 'stamp': stamp,
                    'error_type': error_type}

    def reserve(self, evidence, attempt):
        """Atomically consume one remedy and serialize same-provider attempts."""
        if not attempt:
            raise ValueError('missing delivery attempt')
        return self._admit(evidence, attempt)

    def ready(self, evidence, attempt=None):
        return self._admit(evidence, attempt, dry_run=True)

    def evaluate(self, evidence, attempt=None, *, reserve=False):
        """Return a decision from the same check that may reserve one remedy.

        A preview keeps the existing ready() fair-waiter lease but never
        consumes a remedy. Callers must use this result directly, not call
        reserve() a second time to obtain the reason for its first result.
        Unlike the legacy bool APIs, malformed evidence and ledger failures
        are represented as fail-closed decisions.
        """
        if (not isinstance(evidence, dict)
                or any(not isinstance(evidence.get(key), str) or not evidence[key]
                       for key in ('identity', 'provider', 'stamp'))
                or reserve and not attempt):
            return ProviderRetryDecision(False, 'provider_retry_evidence_missing')
        try:
            return self._admit_result(dict(evidence), attempt, dry_run=not reserve)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            # A damaged JSON row is an unavailable ledger, not absent native
            # evidence. The transaction rolls back before this verdict exists.
            return ProviderRetryDecision(False, 'provider_retry_ledger_unavailable')

    def _admit(self, evidence, attempt, *, dry_run=False):
        return self._admit_result(evidence, attempt, dry_run=dry_run).allowed

    def _admit_result(self, evidence, attempt, *, dry_run=False):
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (evidence['identity'],)).fetchone()
            if not row:
                return ProviderRetryDecision(False, 'provider_retry_evidence_missing')
            record = json.loads(row[0])
            self._pin_legacy_anchor(connection, evidence['identity'], record)
            now = self.clock()
            floor = connection.execute('SELECT until FROM server_floors WHERE provider=?', (evidence['provider'],)).fetchone()
            # Idempotency is only for the same in-flight operation (paste/Enter).
            if attempt and record.get('attempt') == attempt:
                if record.get('reserved_stamp') != evidence['stamp']:
                    return ProviderRetryDecision(False, 'provider_retry_attempt_mismatch')
                if floor and now < floor[0]:
                    return ProviderRetryDecision(False, 'provider_retry_server_floor', floor[0])
                return ProviderRetryDecision(True, 'provider_retry_same_attempt')
            cooldown = connection.execute('SELECT until FROM cooldowns WHERE provider=?', (evidence['provider'],)).fetchone()
            sustained_rate_limit = evidence.get('error_type') == 'rate_limit'
            # Rate-limit recovery follows the original failure timestamp. Old
            # synthetic 15–900s due/cooldown values remain as historical data,
            # but cannot defer this policy. Server floors remain authoritative.
            if sustained_rate_limit:
                cooldown = connection.execute('SELECT until FROM rate_slots WHERE provider=?',
                                              (evidence['provider'],)).fetchone()
                due = max(record['failure_at'], record.get('last_reserved_at', 0)) + RATE_LIMIT_RECOVERY_INTERVAL
            else:
                due = record.get('due', float('inf'))
            if evidence['stamp'] not in record['seen']:
                return ProviderRetryDecision(False, 'provider_retry_evidence_missing')
            if record.get('reserved_stamp') == evidence['stamp']:
                return ProviderRetryDecision(False, 'provider_retry_failure_reserved')
            if record['count'] >= 4 and not sustained_rate_limit:
                return ProviderRetryDecision(False, 'provider_retry_budget_exhausted')
            # Eligible live callers keep their place across provider cooldowns.
            # A fast polling session must not repeatedly win every shared slot.
            # Leases prevent a paused/exited caller from blocking other sessions.
            connection.execute('DELETE FROM waiters WHERE seen_at < ?', (now - 60,))
            connection.execute('INSERT INTO waiters VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(identity) DO UPDATE SET '
                'queued_at=CASE WHEN stamp=excluded.stamp THEN queued_at ELSE excluded.queued_at END, '
                'provider=excluded.provider, stamp=excluded.stamp, seen_at=excluded.seen_at',
                (evidence['identity'], evidence['provider'], evidence['stamp'], now, now))
            first = connection.execute('SELECT identity FROM waiters WHERE provider=? ORDER BY queued_at, identity LIMIT 1',
                                       (evidence['provider'],)).fetchone()
            if floor and now < floor[0]:
                return ProviderRetryDecision(False, 'provider_retry_server_floor', floor[0])
            if cooldown and now < cooldown[0]:
                return ProviderRetryDecision(False, 'provider_retry_shared_slot', cooldown[0])
            if now < due:
                return ProviderRetryDecision(False, 'provider_retry_local_cooldown', due)
            if first[0] != evidence['identity']:
                return ProviderRetryDecision(False, 'provider_retry_fair_queue')
            if attempt is None or dry_run:
                return ProviderRetryDecision(True, 'provider_retry_ready')
            record['count'] += 1
            record['attempt'] = attempt
            record['reserved_stamp'] = evidence['stamp']
            record['last_reserved_at'] = now
            record['due'] = now + (RATE_LIMIT_RECOVERY_INTERVAL if sustained_rate_limit
                                   else (15, 30, 60, 120)[min(record['count'], 3)])
            self._save(connection, evidence['identity'], record)
            connection.execute('DELETE FROM waiters WHERE identity=?', (evidence['identity'],))
            if sustained_rate_limit:
                connection.execute('INSERT INTO rate_slots VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)',
                                   (evidence['provider'], now + RATE_LIMIT_PROVIDER_SPACING))
            else:
                connection.execute('INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)', (evidence['provider'], record['due']))
            return ProviderRetryDecision(True, 'provider_retry_reserved')

    def success(self, session, provider, *, at, completed_turn, last_agent_message, error):
        """Reset only on a later native completion containing an actual answer.

        Callers must bind this observation to the original live session. Empty
        task_complete, Working, user echoes, timers and ACKs are not success.
        """
        if not completed_turn or error or not isinstance(last_agent_message, str) or not last_agent_message.strip():
            return False
        if not math.isfinite(at):
            return False
        key = self.identity(session, provider)
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (key,)).fetchone()
            if not row:
                return False
            record = json.loads(row[0])
            if at <= max(record['failure_at'], record['success_at']):
                return False
            self._save(connection, key, dict(count=0, failure_at=record['failure_at'], success_at=at, seen={}))
            # A successful session cannot cancel another session's cooldown.
            return True
