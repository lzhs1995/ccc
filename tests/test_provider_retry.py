from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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

    def test_four_attempt_budget_survives_new_turns_and_restarts(self):
        for i in range(4):
            e = self.store.observe('s', 'p', str(i), 'http_500', 'HTTP 500', self.now)
            self.now += 200
            self.assertTrue(self.store.reserve(e, str(i)))
            self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.now += 10000
        e = self.store.observe('s', 'p', 'fifth', 'http_500', 'HTTP 500', self.now)
        self.now += 200
        self.assertFalse(self.store.reserve(e, 'fifth'))

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

    def test_failure_history_limit_does_not_discard_consumed_budget(self):
        for i in range(256):
            self.store.observe('s', 'p', str(i), 'http_500', 'HTTP 500', self.now)
        with self.assertRaisesRegex(ValueError, 'history limit'):
            self.store.observe('s', 'p', 'overflow', 'http_500', 'HTTP 500', self.now)

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


if __name__ == '__main__':
    unittest.main()
