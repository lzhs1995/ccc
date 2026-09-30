import json
import tempfile
import unittest
from pathlib import Path

from ccc_provider_retry import ProviderRetryStore


class ProviderFairnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'retry.sqlite3'
        self.now = 1000.0
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        with self.store.transaction() as db:
            for session in ('fast', 'waiting'):
                self.store._save(db, self.store.identity(session, 'p'),
                    dict(count=4, failure_at=1, success_at=0, seen={}, due=1000, last_reserved_at=100))
        self.a = self.store.observe('fast', 'p', 'a', 'rate_limit', 'rate limit', self.now)
        self.b = self.store.observe('waiting', 'p', 'b', 'rate_limit', 'rate limit', self.now)
        self.now = 1120
        self.assertTrue(self.store.reserve(self.a, 'first'))
        self.assertFalse(self.store.ready(self.b))
        self.now = 1121
        self.a = self.store.observe('fast', 'p', 'a2', 'rate_limit', 'rate limit', self.now)

    def test_waiting_session_wins_even_when_previous_winner_polls_first(self):
        for now in range(1150, 2020, 30):
            self.now = now
            self.assertFalse(self.store.ready(self.b))
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: 0)
        self.now = 2020
        self.assertFalse(self.store.reserve(self.a, 'second-fast'))
        self.assertTrue(self.store.reserve(self.b, 'waiting-send'))
        self.assertTrue(self.store.reserve(self.b, 'waiting-send'))
        self.assertFalse(self.store.reserve(self.a, 'second-fast'))

    def test_departed_waiter_does_not_block_live_session(self):
        self.now = 2020
        self.assertTrue(self.store.reserve(self.a, 'second-fast'))

    def test_retry_after_still_blocks_every_waiter(self):
        self.now = 2000
        self.store.observe('server', 'p', 'server-turn', 'rate_limit', 'Retry-After: 300', self.now)
        self.now = 2020
        self.assertFalse(self.store.reserve(self.a, 'second-fast'))
        self.assertFalse(self.store.reserve(self.b, 'waiting-send'))


if __name__ == '__main__':
    unittest.main()
