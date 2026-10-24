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


# 2026-10-20 user rule: Azure "different resource" also auto-sends 任务请继续 under the same bounded backoff.
RETRYABLE = frozenset({'rate_limit', 'resource_mismatch', 'upstream_invalid', 'budget_pool_exhausted', 'http_500', 'http_502', 'http_503', 'http_504', 'http_524'})
BLOCKED_REASONS = {
    'token_exhausted': 'provider token out of quota; needs a new key or a top-up',
    'invalid_encrypted_content': 'provider rejected encrypted session content; repair required; session preserved',
    'http_400': 'provider request rejected (400); inspect parameters/protocol; session preserved',
    'http_401': 'provider authentication rejected (401); verify credentials/quota; session preserved',
    'http_403': 'provider permission rejected (403); verify access policy; session preserved',
    'http_413': 'provider rejected an oversized request (413); resolve payload or upstream size limit before retrying; session preserved',
    'deployment_not_found': 'provider has no deployment matching the requested model; restore the deployment; session and model preserved',
    'billing_exhausted': 'provider explicitly reported insufficient balance; top up the original account; session preserved',
    'invalid_request': 'provider rejected the request; inspect parameters/protocol; session preserved',
    'provider_error_unknown': 'unrecognized structured provider error; inspect original failure before retrying; session preserved',
}
BLOCKED = frozenset(BLOCKED_REASONS)
RATE_LIMIT_RECOVERY_INTERVAL = 0.25
RATE_LIMIT_PROVIDER_SPACING = 0.05
TRANSIENT_RECOVERY_DELAYS = (15, 30, 60, 120)


class ProviderRetryEvidenceError(ValueError):
    """The caller lacks a dated, bound failure; the ledger was not accessed."""


BUDGET_POOL_SENTENCE = 'budgetpoolquotahasbeenexhausted.'
BUDGET_POOL_BANNER_RE = re.compile(r'unexpectedstatus(?:code)?[:=]?402paymentrequired:' + re.escape(BUDGET_POOL_SENTENCE))


def _budget_pool_error(error, prefix, payload):
    """Parsed error object: exact budget-pool message plus an authoritative 402.

    error.status and any numeric error.code must all be 402; without them the
    native banner prefix must say 402. A top-level or nested example 402 is
    never authority, and specific blockers are classified before this check.
    """
    detail = error.get('message')
    if not isinstance(detail, str) or not detail.startswith(BUDGET_POOL_SENTENCE):
        return False
    outer = [str(payload[key]) for key in ('status', 'code')
             if type(payload.get(key)) is int or (isinstance(payload.get(key), str) and payload[key].isdigit())]
    if any(value != '402' for value in outer):
        return False
    inner = [str(error[key]) for key in ('status', 'code')
             if type(error.get(key)) is int or (isinstance(error.get(key), str) and error[key].isdigit())]
    if inner:
        return all(value == '402' for value in inner)
    return bool(re.fullmatch(r'unexpectedstatus(?:code)?[:=]?402paymentrequired:?', prefix))


def structured_error_type(message):
    """Classify specific provider failures, including hard-wrapped JSON/text.

    Callers must first establish a live error card or a native failed turn.
    This is not a detector for arbitrary transcript, documentation or drafts.
    The error's type/code and resource ownership take priority over a generic
    stream-disconnected wrapper; a missing HTTP status must not imply health.
    """
    compact = re.sub(r'\s+', '', str(message)).lower()
    if compact.lstrip('■⚠└').startswith(('documentation:', 'example:', 'quoted:', 'sample:')):
        return None
    # A completed remote compact can fail with a plain nginx HTML 413, without
    # a JSON error envelope. Repeating that payload cannot fix its size. Keep
    # the exact status ahead of generic stream/high-demand wrappers, including
    # terminal hard wraps, so the shared UI/native classifier reports a block.
    if re.search(
            r'(?:unexpectedstatus(?:code)?|unknownstatuscode|http(?:/\d(?:\.\d)?)?|laststatus)'
            r'[:=]?413(?!\d)', compact):
        return 'http_413'
    # These measured failures have no JSON envelope. Preserve their concrete
    # cause before callers consider an outer stream/high-demand/HTTP wrapper.
    deployment = ('couldnotfindanexistingdeploymenttomatchthemodelintherequest.'
                  'pleaseverifythemodelmatchesanexistingdeploymentintheaccount.')
    if deployment in compact:
        return 'deployment_not_found'
    if any(token in compact for token in (
            '用户额度不足', '余额不足', 'insufficientbalance', 'insufficient_balance',
            'creditbalancetoolow', 'insufficientcredits', 'insufficient_credits')):
        return 'billing_exhausted'
    start = compact.find('{')
    if start < 0:
        # 2026-10-20 user rule: only the complete native budget-pool 402 banner is retryable.
        return 'budget_pool_exhausted' if BUDGET_POOL_BANNER_RE.match(compact.lstrip('■⚠└')) else None
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
    if codes & {'deployment_not_found', 'deploymentnotfound', 'model_not_found'}:
        return 'deployment_not_found'
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
    if _budget_pool_error(error, prefix, payload):
        return 'budget_pool_exhausted'
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
    # The measured masked upstream failure may succeed after the upstream
    # recovers. Only this exact null-code, empty-param envelope is retryable;
    # concrete parameter, ownership, quota and authentication errors keep
    # their existing classifications. A trace identifies the failed request,
    # but is not evidence that a local parameter or model needs changing.
    if (set(payload) == {'error'} and set(error) == {'message', 'type', 'param', 'code'}
            and error['type'] == 'invalid_request_error'
            and error['param'] == '' and error['code'] is None
            and re.fullmatch(r'upstreamrejectedtherequestasinvalid\[trace_id=[0-9a-f]{32}\]', detail)
            and (status is None or status[1] in {'400', '500', '502', '503', '504', '524'})):
        return 'upstream_invalid'
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
        if (not isinstance(session, str) or not session.strip()
                or not isinstance(provider, str) or not provider.strip()):
            raise ProviderRetryEvidenceError('provider retry requires original session and provider identity')
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

    def _pin_latest_failure(self, connection, key, record):
        # JSON preserves the original insertion order. Older ledgers retained
        # observations in that order, but only remembered the last reservation.
        # Pin their latest observation once; an old caller must not promote an
        # earlier unreserved (or previously reserved) failure after a restart.
        if 'latest_stamp' not in record:
            record['latest_stamp'] = next(reversed(record['seen']), None)
            self._save(connection, key, record)

    def observe(self, session, provider, turn, error_type, message, at):
        """Observe one original failure; repeated polls never move its deadline."""
        if error_type not in RETRYABLE:
            return None
        if (not isinstance(turn, str) or not turn.strip() or type(at) not in (int, float)
                or not math.isfinite(at) or at <= 0):
            raise ProviderRetryEvidenceError('provider retry requires dated original failed turn')
        key = self.identity(session, provider)
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (key,)).fetchone()
            record = json.loads(row[0]) if row else dict(count=0, failure_at=0, success_at=0, seen={})
            self._pin_legacy_anchor(connection, key, record)
            self._pin_latest_failure(connection, key, record)
            if at <= record['success_at'] or at < record['failure_at']:
                return None
            stamp = hashlib.sha256(json.dumps([turn, float(at), error_type, message]).encode()).hexdigest()
            if stamp not in record['seen']:
                # Once a time bucket has been pruned, an unknown stamp in it
                # cannot be distinguished from replay. Keep a durable time
                # floor instead of forgetting that ambiguity on restart.
                if at <= record.get('history_floor_at', 0):
                    return None
                now = self.clock()
                delay = (RATE_LIMIT_RECOVERY_INTERVAL if error_type == 'rate_limit'
                         else TRANSIENT_RECOVERY_DELAYS[min(record['count'], 3)])
                noise = float(self.jitter())
                if not math.isfinite(noise) or not 0 <= noise <= .1:
                    raise ValueError('invalid retry jitter')
                server = retry_after(message, at)
                deadline = max((at + delay if error_type == 'rate_limit' else now + delay * (1 + noise)), server)
                record['seen'][stamp] = deadline
                observed_at = record.setdefault('seen_at', {})
                observed_at[stamp] = at
                # Keep bounded history without turning sustained transient load
                # into a permanent stop. Legacy observations have no individual
                # timestamp, so retire their last known time conservatively.
                while len(record['seen']) > 256:
                    retired = next(iter(record['seen']))
                    del record['seen'][retired]
                    record['history_floor_at'] = max(record.get('history_floor_at', 0),
                        observed_at.pop(retired, record['failure_at']))
                record['latest_stamp'] = stamp
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
            self._pin_latest_failure(connection, evidence['identity'], record)
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
            if record['latest_stamp'] != evidence['stamp']:
                return ProviderRetryDecision(False, 'provider_retry_superseded')
            # A transient outage can outlast four remedies. Keep the durable
            # count and reservation, but continue at the capped delay for each
            # newly proved failed turn. This never refunds an uncertain input
            # or permits replay of a previously reserved failure.
            # Eligible live callers keep their place across provider cooldowns.
            # A fast polling session must not repeatedly win every shared slot.
            # Leases prevent a paused/exited caller from blocking other sessions.
            # Fairness follows the two independent provider admission clocks.
            # A transient waiter in its 120s backoff must not occupy the head
            # of the rate-limit queue, which ignores that synthetic cooldown.
            # Encode both cohorts to prevent collisions with literal provider
            # names. Same-stamp refresh retains its original queued_at.
            fair_provider = json.dumps([evidence['provider'],
                'rate_limit' if sustained_rate_limit else 'transient'], separators=(',', ':'))
            connection.execute('DELETE FROM waiters WHERE seen_at < ?', (now - 60,))
            connection.execute('INSERT INTO waiters VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(identity) DO UPDATE SET '
                'queued_at=CASE WHEN stamp=excluded.stamp THEN queued_at ELSE excluded.queued_at END, '
                'provider=excluded.provider, stamp=excluded.stamp, seen_at=excluded.seen_at',
                (evidence['identity'], fair_provider, evidence['stamp'], now, now))
            first = connection.execute('SELECT identity FROM waiters WHERE provider=? ORDER BY queued_at, identity LIMIT 1',
                                       (fair_provider,)).fetchone()
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
                                   else TRANSIENT_RECOVERY_DELAYS[min(record['count'], 3)])
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
