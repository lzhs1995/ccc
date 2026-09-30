"""Latency policy regressions: original failure time, legacy ledger, no replay."""
import json
import tempfile
import unittest
from pathlib import Path
from ccc_provider_retry import ProviderRetryStore

class RateLatencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.sqlite3"
        self.now = 1000.
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now, jitter=lambda: .1)

    def evidence(self, session="s", turn="t", message="rate limit exceeded", at=1000.):
        return self.store.observe(session, "custom", turn, "rate_limit", message, at)

    def test_both_reported_errors_eligible_within_one_second(self):
        for i, text in enumerate(("rate limit exceeded: The system is currently experiencing high demand and cannot process your request. Your request exceeds the maximum usage size allowed during peak load.", "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded token rate limit.")):
            e = self.evidence(str(i), message=text)
            self.now = 1000.5 + i * .1
            self.assertTrue(self.store.reserve(e, str(i)))

    def test_late_observation_does_not_restart_delay(self):
        self.now = 1100.
        self.assertTrue(self.store.reserve(self.evidence(), "a"))

    def test_legacy_due_and_shared_cooldown_do_not_delay_or_erase_consumption(self):
        e = self.evidence()
        with self.store.transaction() as db:
            r = json.loads(db.execute("select record from episodes").fetchone()[0])
            r.update(count=40, due=1900., last_reserved_at=999., attempt="old", reserved_stamp="old")
            self.store._save(db, e["identity"], r)
            db.execute("insert into cooldowns values ('custom',1900)")
        self.now = 1000.5
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now)
        self.assertTrue(self.store.reserve(e, "new"))
        with self.store.transaction() as db:
            self.assertEqual(json.loads(db.execute("select record from episodes").fetchone()[0])["count"], 41)
            self.assertEqual(db.execute("select until from cooldowns").fetchone()[0], 1900.)
        self.now = 9999.
        self.assertFalse(self.store.reserve(e, "duplicate"))
        self.assertTrue(self.store.reserve(e, "new"))

    def test_legacy_server_floor_survives_restart(self):
        e = self.evidence(message="Retry-After: 120")
        self.now = 1001.
        self.store = ProviderRetryStore(self.path, clock=lambda: self.now)
        self.assertFalse(self.store.reserve(e, "a"))
        self.now = 1120.
        self.assertTrue(self.store.reserve(e, "a"))

    def test_live_queue_rotates_without_minutes_of_shared_delay(self):
        evidences = [self.evidence(str(i)) for i in range(10)]
        for e in evidences:
            self.assertFalse(self.store.ready(e))
        self.now = 1000.25
        with self.store.transaction() as db:
            order = [r[0] for r in db.execute("select identity from waiters order by queued_at,identity")]
        by_id = {e["identity"]:e for e in evidences}
        for i, identity in enumerate(order):
            self.assertTrue(self.store.reserve(by_id[identity], str(i)))
            self.now += .051
        self.assertLess(self.now - 1000., 1.)

if __name__ == "__main__":
    unittest.main()
