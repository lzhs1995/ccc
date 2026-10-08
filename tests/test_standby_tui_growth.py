"""Real append races with synthetic native identity and transport."""
import json
import unittest
from unittest import mock
import ccc_workspace_batch as batch
import ccc_standby_readiness as readiness
from tests.test_standby_readiness import RefreshBarrierTests

class TuiGrowthTests(RefreshBarrierTests):
    def growing(self):
        raw=batch._initial_event_prefix
        def read(claim, **kwargs):
            value=raw(claim, **kwargs)
            with self.native.tui.open('ab') as h:
                h.write((json.dumps({'dir':'to_tui','kind':'app_event','variant':'InsertHistoryCell'})+'\n').encode())
            return value
        return mock.patch.object(batch,'_initial_event_prefix',side_effect=read)

    def test_growth_fixed_deadline_no_intent(self):
        with self.growing():
            self.assertFalse(self.prepare())
            deadline=self.barrier._tui_deadline
            self.clock+=29
            self.assertFalse(self.prepare())
            self.assertEqual(self.barrier._tui_deadline,deadline)
            self.clock+=1
            with self.assertRaises(TimeoutError):self.prepare()
        self.assertEqual(self.writes,[])
        self.assertFalse(self.barrier.intent.exists())

    def test_growth_then_stable_same_original_one_control(self):
        with self.growing():self.assertFalse(self.prepare())
        self.assertTrue(self.prepare())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes),1)
        self.assertIsNone(self.barrier._tui_deadline)

    def test_growth_permission_revoked_during_pending(self):
        with self.growing():
            self.assertFalse(self.prepare())
            self.allowed=False
            with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.writes,[])

    def test_growth_birth_changed_before_stable_observation(self):
        with self.growing():self.assertFalse(self.prepare())
        self.native.process['birth'][1]+=1
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.writes,[])

    def test_growth_after_ack_observation_only_then_recover(self):
        self.assertTrue(self.prepare());self.rendered()
        with self.growing():self.assertIsNone(self.barrier.observe())
        self.assertFalse(self.prepare())
        self.assertIsNotNone(self.barrier.observe())
        self.assertEqual(len(self.writes),1)

    def test_growth_activation_propagates_pending_then_recovers(self):
        self.assertTrue(self.prepare());self.rendered();self.barrier.observe()
        with self.growing():
            with self.assertRaises(readiness.TuiObservationPending):self.barrier.observe_for_activation()
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertIsNotNone(self.barrier.observe_for_activation())
        self.assertEqual(len(self.writes),1)

    def test_growth_events_after_complete_inspection_keeps_deadline(self):
        original=self.barrier._events
        def events(*a,**k):
            with self.growing():return original(*a,**k)
        with mock.patch.object(self.barrier,'_events',side_effect=events):
            self.assertFalse(self.prepare())
            deadline=self.barrier._tui_deadline
            self.clock+=29
            self.assertFalse(self.prepare())
            self.assertEqual(self.barrier._tui_deadline,deadline)
            self.clock+=1
            with self.assertRaises(TimeoutError):self.prepare()
        self.assertEqual(self.writes,[])

    def test_growth_pending_prefix_rewrite_recheck_refuses(self):
        with self.growing():
            with self.assertRaises(readiness.TuiObservationPending) as caught:self.native.inspect()
        pending=caught.exception
        data=self.native.tui.read_bytes().replace(b'InsertHistoryCell',b'InsertHistoryFail',1)
        self.native.tui.write_bytes(data)
        with self.assertRaises(ValueError):pending.recheck()

    def test_growth_after_durable_intent_never_retries_input(self):
        original=readiness.write_once
        raw=batch._initial_event_prefix
        def read(claim, **kwargs):
            value=raw(claim, **kwargs)
            self.clock+=1
            with self.native.tui.open('ab') as h:
                h.write((json.dumps({'dir':'to_tui','kind':'app_event','variant':'InsertHistoryCell'})+'\n').encode())
            return value
        def persist(path,value):
            result=original(path,value)
            if path==self.barrier.intent:
                patcher=mock.patch.object(batch,'_initial_event_prefix',side_effect=read)
                patcher.start();self.addCleanup(patcher.stop)
            return result
        self.client.connected_input_remaining.side_effect=lambda:130-self.clock
        with mock.patch.object(readiness,'write_once',side_effect=persist):
            with self.assertRaises((ValueError,TimeoutError)):self.prepare()
        self.assertTrue(self.barrier.intent.exists())
        self.assertFalse(self.prepare())
        self.assertEqual(self.writes,[])



def load_tests(loader, tests, pattern):
    names=[n for n in TuiGrowthTests.__dict__ if n.startswith('test_')]
    return unittest.TestSuite(TuiGrowthTests(n) for n in names)
