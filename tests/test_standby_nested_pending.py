import unittest
from unittest import mock
import ccc_standby_identity as identity
import ccc_standby_readiness as readiness
from tests.test_standby_readiness import RefreshBarrierTests

class NestedPendingTests(RefreshBarrierTests):
    def pending_inspection(self, **kwargs):
        observation = {**self.native.expected, 'pid': self.native.claim['bootstrap_pid'],
                       'birth': self.native.claim['bootstrap_birth']}
        def recheck():
            with self.native.tui.open('ab') as stream:
                stream.write(b'{"dir":')
            identity._idle_prefix(self.native.claim)
        raise readiness.ObservationPending(observation, recheck)

    def test_partial_during_inventory_recheck_waits_without_input(self):
        with mock.patch.object(self.barrier, 'inspect', side_effect=self.pending_inspection):
            self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertEqual(self.writes, [])
        self.assertEqual(self.barrier._tui_deadline, 130)
        self.native.save_events()
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)


    def test_nested_pending_retains_deadline(self):
        with mock.patch.object(self.barrier, 'inspect', side_effect=self.pending_inspection):
            self.assertFalse(self.prepare())
            self.native.save_events()
            self.clock = 129
            self.assertFalse(self.prepare())
            self.assertEqual(self.barrier._tui_deadline, 130)
            self.clock = 130
            with self.assertRaises(TimeoutError): self.prepare()
        self.assertFalse(self.barrier.intent.exists())
        self.assertEqual(self.writes, [])

    def test_nested_pending_rechecks_authorization(self):
        original = self.pending_inspection
        def inspect(**kwargs):
            try: original(**kwargs)
            except readiness.ObservationPending as pending:
                recheck = pending.recheck
                def revoke():
                    self.allowed = False
                    recheck()
                pending.recheck = revoke
                raise
        with mock.patch.object(self.barrier, 'inspect', side_effect=inspect):
            with self.assertRaisesRegex(ValueError, 'authorization refused'): self.prepare()
        self.assertEqual(self.writes, [])

    def test_recheck_identity_failure_remains_terminal(self):
        observation = {**self.native.expected}
        def recheck(): raise ValueError('original identity changed')
        pending = readiness.ObservationPending(observation, recheck)
        with mock.patch.object(self.barrier, 'inspect', side_effect=pending):
            with self.assertRaisesRegex(ValueError, 'identity changed'): self.prepare()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertEqual(self.writes, [])

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(NestedPendingTests(n) for n in NestedPendingTests.__dict__ if n.startswith('test_'))
