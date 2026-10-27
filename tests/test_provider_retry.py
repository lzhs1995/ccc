from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from ccc_provider_retry import ProviderRetryStore, retry_after


class ProviderRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'retry.sqlite3'
        self.now = 1000.0
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)

    def observe(self, session='s', turn='t', provider='p', message='rate limit'):
        return self.store.observe(session, provider, turn, 'rate_limit', message, self.now)

    def test_retry_after_seconds_date_and_nested_header(self):
        self.assertEqual(retry_after('Retry-After: 60', 1000), 1060)
        self.assertEqual(retry_after('{"Retry-After":"60"}', 1000), 1060)
        self.assertEqual(retry_after('Retry-After: Thu, 01 Jan 1970 00:30:00 GMT', 1000), 1800)
        self.assertEqual(retry_after('Retry-After: nonsense', 1000), 0)

    def test_observation_is_stable_and_waits_before_first_error_remedy(self):
        e = self.observe()
        self.assertFalse(self.store.reserve(e, 'a'))
        self.now += .249
        self.assertEqual(e, self.store.observe('s', 'p', 't', 'rate_limit', 'rate limit', 1000))
        self.assertFalse(self.store.reserve(e, 'a'))
        self.now += .001
        self.assertTrue(self.store.reserve(e, 'a'))
        self.assertTrue(self.store.reserve(e, 'a'))
        self.assertFalse(self.store.reserve(e, 'b'))

    def test_transient_retries_keep_capped_backoff_across_turns_and_restarts(self):
        for status in ('500', '502', '503', '504', '524'):
            for i, delay in enumerate((15, 30, 60, 120, 120, 120, 120, 120)):
                with self.subTest(status=status, remedy=i + 1):
                    observed = self.now
                    e = self.store.observe(status, status, str(i), 'http_' + status,
                                           'HTTP ' + status, observed)
                    self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
                    result = self.store.evaluate(e, str(i), reserve=True)
                    self.assertFalse(result.allowed)
                    self.assertIn(result.reason_code, {'provider_retry_local_cooldown', 'provider_retry_shared_slot'})
                    self.assertEqual(result.retry_at, observed + delay)
                    self.now = result.retry_at
                    self.assertTrue(self.store.reserve(e, str(i)))
                    self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
                    self.assertTrue(self.store.reserve(e, str(i)))
                    self.assertFalse(self.store.reserve(e, 'duplicate'))
                    self.assertEqual(self.episode(e)['count'], i + 1)

    def test_retry_after_overrides_delay_and_cools_other_sessions(self):
        e = self.observe(message='Retry-After: 600')
        other = self.observe(session='other')
        self.now += 599
        self.assertFalse(self.store.reserve(e, 'a'))
        self.assertFalse(self.store.reserve(other, 'b'))
        self.now += 1
        self.assertTrue(self.store.reserve(e, 'a'))
        self.assertFalse(self.store.reserve(other, 'b'))

    def test_same_provider_reservation_is_atomic(self):
        evidences = [self.observe(session=str(i)) for i in range(8)]
        self.now += 15
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda pair: self.store.reserve(pair[1], str(pair[0])), enumerate(evidences)))
        self.assertEqual(sum(values), 1)

    def test_later_server_floor_revokes_enter_even_after_restart(self):
        evidence = self.observe()
        self.now += 15
        self.assertTrue(self.store.reserve(evidence, 'paste-and-enter'))
        self.observe(session='other', message='Retry-After: 600')
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.assertFalse(self.store.reserve(evidence, 'paste-and-enter'))
        self.now += 600
        self.assertTrue(self.store.reserve(evidence, 'paste-and-enter'))

    def test_resumed_queue_preview_is_idempotent_without_new_reservation(self):
        evidence = self.observe()
        self.now += 15
        self.assertTrue(self.store.ready(evidence, 'queue-key'))
        self.assertTrue(self.store.reserve(evidence, 'queue-key'))
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.assertTrue(self.store.ready(evidence, 'queue-key'))
        self.assertFalse(self.store.ready(evidence, 'different-key'))

    def test_transient_history_is_bounded_and_cannot_replay_pruned_failures(self):
        for i in range(300):
            observed = self.now
            e = self.store.observe('s', 'p', str(i), 'http_503', 'HTTP 503', observed)
            if i == 0:
                oldest = e
            self.now += 200
            self.assertTrue(self.store.reserve(e, str(i)))
        before = self.episode(e)
        self.assertEqual(before['count'], 300)
        self.assertEqual(len(before['seen']), 256)
        self.assertEqual(len(before['seen_at']), 256)
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.assertIsNone(self.store.observe('s', 'p', '0', 'http_503', 'HTTP 503', 1000))
        self.assertFalse(self.store.reserve(oldest, 'replay'))
        self.assertFalse(self.store.reserve(e, 'duplicate'))
        self.assertTrue(self.store.reserve(e, '299'))
        self.assertEqual(self.episode(e), before)

    def test_same_timestamp_pruning_preserves_floor_and_original_inflight_attempt(self):
        for i in range(257):
            e = self.store.observe('s', 'p', str(i), 'http_503', 'HTTP 503', 1000)
            self.assertIsNotNone(e)
            if i == 0:
                oldest = e
        self.now += 200
        self.assertTrue(self.store.reserve(e, 'original'))
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        before = self.episode(e)
        self.assertEqual(before['history_floor_at'], 1000)
        for turn in ('0', 'unknown-same-time'):
            self.assertIsNone(self.store.observe('s', 'p', turn, 'http_503', 'HTTP 503', 1000))
        self.assertFalse(self.store.reserve(oldest, 'old-replay'))
        self.assertTrue(self.store.reserve(e, 'original'))
        self.assertEqual(self.episode(e), before)
        fresh = self.store.observe('s', 'p', 'later', 'http_503', 'HTTP 503', 1001)
        self.assertIsNotNone(fresh)
        self.now += 200
        self.assertTrue(self.store.reserve(fresh, 'later'))

    def test_superseded_reserved_and_unreserved_failures_cannot_become_new_attempts(self):
        for reserved in (False, True):
            with self.subTest(reserved=reserved):
                session = str(reserved)
                old = self.store.observe(session, session, 'old', 'http_503', 'HTTP 503', 1000)
                self.now += 200
                if reserved:
                    self.assertTrue(self.store.reserve(old, 'old-attempt'))
                latest = self.store.observe(session, session, 'latest', 'http_503', 'HTTP 503', 1000)
                self.now += 200
                self.assertTrue(self.store.reserve(latest, 'latest-attempt'))
                self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
                before = self.episode(latest)
                self.assertEqual(self.store.observe(session, session, 'old', 'http_503', 'HTTP 503', 1000), old)
                for attempt in ('old-attempt', 'another-attempt'):
                    result = self.store.evaluate(old, attempt, reserve=True)
                    self.assertFalse(result.allowed)
                    self.assertEqual(result.reason_code, 'provider_retry_superseded')
                self.assertEqual(self.episode(latest), before)

    def test_legacy_latest_observation_is_pinned_once_without_promoting_an_old_failure(self):
        old = self.store.observe('s', 'p', 'old', 'http_503', 'HTTP 503', 1000)
        latest = self.store.observe('s', 'p', 'latest', 'http_503', 'HTTP 503', 1000)
        with self.store.transaction() as db:
            record = json.loads(db.execute('SELECT record FROM episodes').fetchone()[0])
            del record['latest_stamp']
            del record['seen_at']
            self.store._save(db, old['identity'], record)
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.assertEqual(self.store.observe('s', 'p', 'old', 'http_503', 'HTTP 503', 1000), old)
        self.now += 200
        result = self.store.evaluate(old, 'old', reserve=True)
        self.assertEqual(result.reason_code, 'provider_retry_superseded')
        self.assertEqual(self.episode(old)['latest_stamp'], latest['stamp'])
        self.assertTrue(self.store.reserve(latest, 'latest'))

    def test_sustained_rate_limit_recovers_after_bounded_cooldown_across_restart(self):
        for i in range(4):
            e = self.observe(turn=str(i))
            self.now += 200
            self.assertTrue(self.store.reserve(e, str(i)))
        reserved_at = self.now
        e = self.observe(turn='eastus2', message='rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded token rate limit.')
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.now = reserved_at + .249
        self.assertFalse(self.store.reserve(e, 'fifth'))
        self.now += .001
        self.assertTrue(self.store.reserve(e, 'fifth'))
        self.assertTrue(self.store.reserve(e, 'fifth'))
        self.assertFalse(self.store.reserve(e, 'duplicate'))
        self.assertFalse(self.store.reserve(self.observe(session='other'), 'other'))

    def test_rate_limit_history_is_bounded_without_permanent_stop(self):
        for i in range(300):
            e = self.observe(turn=str(i))
            self.now += 1000
            self.assertTrue(self.store.reserve(e, str(i)))
        self.assertIsNone(self.store.observe('s', 'p', '0', 'rate_limit', 'rate limit', 1000))

    def test_installed_legacy_four_attempt_record_recovers_without_erasing_ledger(self):
        import json
        e = self.observe()
        with self.store.transaction() as db:
            record = json.loads(db.execute('SELECT record FROM episodes').fetchone()[0])
            record.update(count=4, due=1120, attempt='old-accepted', reserved_stamp='old')
            self.store._save(db, e['identity'], record)
        self.now = 1000.249
        self.assertFalse(self.store.reserve(e, 'new'))
        self.now = 1000.25
        self.assertTrue(self.store.reserve(e, 'new'))
        with self.store.transaction() as db:
            self.assertEqual(json.loads(db.execute('SELECT record FROM episodes').fetchone()[0])['count'], 5)

    def test_legacy_recovery_anchor_survives_new_failure_and_restart(self):
        import json
        e = self.observe()
        with self.store.transaction() as db:
            record = json.loads(db.execute('SELECT record FROM episodes').fetchone()[0])
            record.update(count=4, due=1120, attempt='old-accepted', reserved_stamp='old')
            self.store._save(db, e['identity'], record)
        self.now = 1500
        self.observe(turn='new-failure')
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.now = 1750
        e = self.observe(turn='another-failure')
        self.now = 1750.249
        self.assertFalse(self.store.reserve(e, 'new'))
        self.now = 1750.25
        self.assertTrue(self.store.reserve(e, 'new'))
        self.assertFalse(self.store.reserve(e, 'duplicate'))
        with self.store.transaction() as db:
            record = json.loads(db.execute('SELECT record FROM episodes').fetchone()[0])
            self.assertEqual(record['count'], 5)
            self.assertEqual(len(record['seen']), 3)

    def test_sustained_rate_limit_still_obeys_server_floor(self):
        for i in range(4):
            e = self.observe(turn=str(i))
            self.now += 200
            self.assertTrue(self.store.reserve(e, str(i)))
        e = self.observe(turn='new', message='Retry-After: 3600')
        self.now += 3599
        self.assertFalse(self.store.reserve(e, 'new'))
        self.now += 1
        self.assertTrue(self.store.reserve(e, 'new'))

    def test_other_provider_and_successful_continuation_are_unaffected(self):
        e = self.observe(message='Retry-After: 600')
        other = self.observe(provider='other')
        self.now += 15
        self.assertTrue(self.store.reserve(other, 'b'))
        self.assertFalse(self.store.reserve(e, 'a'))
        self.assertIsNone(self.store.observe('s', 'p', 'x', 'normal', '', self.now))

    def test_only_later_answer_completion_resets_and_stale_failure_cannot_reopen(self):
        e = self.observe()
        self.now += 15
        self.assertTrue(self.store.reserve(e, 'a'))
        for message, error, at in [('', None, 1020), ('answer', {}, 999), ('answer', {'message':'failed'}, 1020)]:
            self.assertFalse(self.store.success('s', 'p', at=at, completed_turn='done', last_agent_message=message, error=error))
        self.assertTrue(self.store.success('s', 'p', at=1020, completed_turn='done', last_agent_message='answer', error=None))
        self.assertFalse(self.store.reserve(e, 'a'))
        self.assertIsNone(self.store.observe('s', 'p', 't', 'rate_limit', 'rate limit', 1000))
        self.now = 1100
        next_e = self.observe(turn='new')
        self.now += 15
        self.assertTrue(self.store.reserve(next_e, 'new'))

    def test_corrupt_database_and_missing_identity_fail_closed(self):
        with self.assertRaises(ValueError):
            self.observe(session='')
        self.path.write_bytes(b'corrupt')
        with self.assertRaises(Exception):
            self.observe()

    def episode(self, evidence):
        # Inspect the real ledger without using a reservation or observation.
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            return json.loads(db.execute('SELECT record FROM episodes WHERE identity=?',
                                         (evidence['identity'],)).fetchone()[0])

    def test_decision_reports_local_wait_without_consuming_a_remedy(self):
        evidence = self.observe()
        original = self.episode(evidence)
        result = self.store.evaluate(evidence, 'first')
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_local_cooldown')
        self.assertEqual(result.retry_at, 1000.25)
        self.assertEqual(self.episode(evidence), original)
        self.now = result.retry_at
        for _ in range(3):
            result = self.store.evaluate(evidence, 'first')
            self.assertTrue(result.allowed)
            self.assertEqual(result.reason_code, 'provider_retry_ready')
            self.assertEqual(self.episode(evidence), original)
        result = self.store.evaluate(evidence, 'first', reserve=True)
        self.assertTrue(result.allowed)
        reserved = self.episode(evidence)
        self.assertEqual(reserved['count'], 1)
        for reserve in (False, True):
            result = self.store.evaluate(evidence, 'first', reserve=reserve)
            self.assertTrue(result.allowed)
            self.assertEqual(result.reason_code, 'provider_retry_same_attempt')
            self.assertEqual(self.episode(evidence), reserved)

    def test_reserved_failure_is_distinct_from_cooldown_and_budget_after_restart(self):
        evidence = self.observe()
        self.now += .25
        self.assertTrue(self.store.reserve(evidence, 'original'))
        reserved = self.episode(evidence)
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.now += 10000
        for reserve in (False, True):
            result = self.store.evaluate(evidence, 'another', reserve=reserve)
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason_code, 'provider_retry_failure_reserved')
            self.assertEqual(result.retry_at, 0)
            self.assertEqual(self.episode(evidence), reserved)
        # The original operation cannot use its id to reserve a newer failure.
        fresh = self.observe(turn='different-turn')
        result = self.store.evaluate(fresh, 'original', reserve=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_attempt_mismatch')
        self.assertEqual(self.episode(evidence)['count'], 1)

    def test_server_floor_reason_also_applies_to_idempotent_attempt(self):
        evidence = self.observe()
        self.now += .25
        self.assertTrue(self.store.reserve(evidence, 'original'))
        self.observe(session='server', message='Retry-After: 600')
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        before = self.episode(evidence)
        result = self.store.evaluate(evidence, 'original', reserve=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_server_floor')
        self.assertEqual(result.retry_at, 1600.25)
        self.assertEqual(self.episode(evidence), before)
        self.now = result.retry_at
        self.assertTrue(self.store.evaluate(evidence, 'original', reserve=True).allowed)
        self.assertEqual(self.episode(evidence), before)

    def test_shared_slot_and_fair_queue_have_independent_reasons(self):
        first = self.observe(session='first')
        waiting = self.observe(session='waiting')
        self.now += .25
        self.assertTrue(self.store.reserve(first, 'first'))
        result = self.store.evaluate(waiting)
        self.assertEqual(result.reason_code, 'provider_retry_shared_slot')
        self.assertEqual(result.retry_at, 1000.30)
        self.now += .01
        newcomer = self.observe(session='newcomer')
        self.now += .25
        result = self.store.evaluate(newcomer, 'newcomer', reserve=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_fair_queue')
        self.assertEqual(self.episode(newcomer)['count'], 0)
        self.assertTrue(self.store.evaluate(waiting, 'waiting', reserve=True).allowed)

    def test_fifth_transient_remedy_waits_instead_of_permanently_exhausting(self):
        for i in range(4):
            evidence = self.store.observe('s', 'p', str(i), 'http_500', 'HTTP 500', self.now)
            self.now += 200
            self.assertTrue(self.store.reserve(evidence, str(i)))
        evidence = self.store.observe('s', 'p', 'fifth', 'http_500', 'HTTP 500', self.now)
        before = self.episode(evidence)
        result = self.store.evaluate(evidence, 'fifth', reserve=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_shared_slot')
        self.assertEqual(result.retry_at, self.now + 120)
        self.assertEqual(self.episode(evidence), before)
        self.now = result.retry_at
        self.assertTrue(self.store.reserve(evidence, 'fifth'))
        self.assertEqual(self.episode(evidence)['count'], 5)

    def test_missing_evidence_and_broken_ledger_are_separate_fail_closed_results(self):
        evidence = self.observe()
        original = self.episode(evidence)
        for missing in (None, {}, {'identity': 'x'}, {**evidence, 'stamp': 'not-observed'},
                        {**evidence, 'identity': 'no-episode'}):
            with self.subTest(evidence=missing):
                result = self.store.evaluate(missing, 'attempt', reserve=True)
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason_code, 'provider_retry_evidence_missing')
                self.assertEqual(self.episode(evidence), original)
        self.path.write_bytes(b'corrupt')
        result = self.store.evaluate(evidence, 'attempt', reserve=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_ledger_unavailable')
        # Existing bool methods retain their documented failure behavior.
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.ready(evidence)

    def test_corrupt_episode_does_not_become_missing_evidence(self):
        evidence = self.observe()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE episodes SET record=? WHERE identity=?', ('{broken', evidence['identity']))
        result = self.store.evaluate(evidence, 'attempt', reserve=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason_code, 'provider_retry_ledger_unavailable')

    def test_parallel_decisions_carry_their_own_result_and_reserve_once(self):
        evidences = [self.observe(session=str(i)) for i in range(8)]
        self.now += .25
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda pair: self.store.evaluate(pair[1], str(pair[0]), reserve=True),
                                    enumerate(evidences)))
        self.assertEqual(sum(result.allowed for result in results), 1)
        self.assertEqual(sum(self.episode(evidence)['count'] for evidence in evidences), 1)
        for result in results:
            self.assertEqual(result.reason_code,
                'provider_retry_reserved' if result.allowed else 'provider_retry_shared_slot')


if __name__ == '__main__':
    unittest.main()
