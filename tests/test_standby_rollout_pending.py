"""Bounded real Darwin events, with synthetic native identity and input."""
from contextlib import contextmanager
from pathlib import Path
import os
import select
import tempfile
import unittest
from unittest import mock

from ccc_standby_rollouts import RolloutInventory, RolloutObservationPending
from ccc_standby_identity import ObservationPending
from tests import test_standby_identity as identity_fixture
from tests import test_standby_readiness as readiness_fixture


@contextmanager
def busy(pin, path):
    queue = pin._queue
    calls = []
    def control(changes, limit, timeout):
        if limit:
            (path / f'lock-{len(calls)}').touch()
            calls.append(1)
        return queue.control(changes, limit, timeout)
    with mock.patch.object(pin, '_queue', wraps=queue) as proxy:
        proxy.control.side_effect = control
        yield calls


@unittest.skipUnless(hasattr(select, 'kqueue'), 'Darwin kqueue required')
class RolloutPendingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / 'native' / 'sessions'
        self.root.mkdir(parents=True)
        self.session = '01a0e8aa-25f3-79d1-ab3f-bbe626e84426'
        self.pin = RolloutInventory(self.root)
        self.addCleanup(self.pin.close)

    def test_home_churn_waits_then_quiet_absence_recovers(self):
        with busy(self.pin, self.root.parent) as calls:
            with self.assertRaises(RolloutObservationPending):
                self.pin.absent(self.root, self.session)
        self.assertEqual(len(calls), self.pin.max_rounds)
        self.assertFalse(self.pin._invalid)
        self.assertTrue(self.pin.absent(self.root, self.session))

    def test_rollout_created_after_pending_cannot_use_cached_absence(self):
        with busy(self.pin, self.root.parent):
            with self.assertRaises(RolloutObservationPending):
                self.pin.absent(self.root, self.session)
        (self.root / (self.session + '.jsonl')).touch()
        self.assertFalse(self.pin.absent(self.root, self.session))
        (self.root / (self.session + '.jsonl')).unlink()
        self.assertFalse(self.pin.absent(self.root, self.session))

    def test_identity_event_after_pending_is_permanent(self):
        with busy(self.pin, self.root.parent):
            with self.assertRaises(RolloutObservationPending):
                self.pin.absent(self.root, self.session)
        mode = self.root.stat().st_mode & 0o777
        self.root.chmod(mode ^ 0o020)
        self.root.chmod(mode)
        with self.assertRaises(ValueError): self.pin.absent(self.root, self.session)
        self.assertTrue(self.pin._invalid)
        with self.assertRaises(ValueError): self.pin.absent(self.root, self.session)

    def test_content_budget_exhaustion_remains_permanent(self):
        self.pin.max_update_entries = 1
        with busy(self.pin, self.root):
            with self.assertRaisesRegex(ValueError, 'update bound exceeded'):
                self.pin.absent(self.root, self.session)
        self.assertTrue(self.pin._invalid)

    def test_pending_close_releases_original_descriptors(self):
        fds = tuple(self.pin._paths)
        with busy(self.pin, self.root.parent):
            with self.assertRaises(RolloutObservationPending):
                self.pin.absent(self.root, self.session)
        self.pin.close()
        for fd in fds:
            with self.assertRaises(OSError): os.fstat(fd)

    def test_construction_pending_must_recheck_before_any_absence(self):
        queue = select.kqueue()
        calls = []
        def control(changes, limit, timeout):
            if limit:
                (self.root.parent / f'construction-lock-{len(calls)}').touch()
                calls.append(1)
            return queue.control(changes, limit, timeout)
        proxy = mock.Mock(wraps=queue)
        proxy.control.side_effect = control
        with mock.patch('ccc_standby_rollouts.select.kqueue', return_value=proxy):
            pin = RolloutInventory(self.root)
        self.addCleanup(pin.close)
        with self.assertRaises(RolloutObservationPending):
            pin.absent(self.root, self.session)
        proxy.control.side_effect = None
        (self.root / (self.session + '.jsonl.zst')).touch()
        self.assertFalse(pin.absent(self.root, self.session))


class RolloutPendingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.native = identity_fixture.StandbyIdentityTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)

    def test_both_absence_boundaries_wait_then_reinspect_entire_original(self):
        for position in (1, 2):
            with self.subTest(position=position):
                absence = mock.Mock(side_effect=[True] * (position - 1) + [
                    RolloutObservationPending('busy')])
                connected, final = mock.Mock(), mock.Mock()
                with self.assertRaises(ObservationPending) as caught:
                    self.native.inspect(rollout_absent=absence,
                        connected_check=connected, final_check=final)
                caught.exception.recheck()
                row = self.native.inspect(rollout_absent=lambda *_: True,
                    connected_check=connected, final_check=final)
                self.assertFalse(row['readiness_proven'])
                self.assertEqual(connected.call_count, position)
                self.assertEqual(final.call_count, position)

    def test_pending_identity_change_refuses_wait(self):
        def changed(*_):
            self.native.process['birth'][1] += 1
            raise RolloutObservationPending('busy')
        with self.assertRaises(ValueError):
            self.native.inspect(rollout_absent=changed)

    def test_pending_recheck_rejects_new_user_input(self):
        with self.assertRaises(ObservationPending) as caught:
            self.native.inspect(rollout_absent=mock.Mock(
                side_effect=RolloutObservationPending('busy')))
        self.native.events.append(dict(dir='from_tui', kind='op', payload={
            'UserTurn': {'items': [{'type': 'text', 'text': 'new input'}]}}))
        self.native.save_events()
        with self.assertRaisesRegex(RuntimeError, 'first outbound task'):
            caught.exception.recheck()


class RolloutPendingBarrierTests(unittest.TestCase):
    def setUp(self):
        self.fixture = readiness_fixture.RefreshBarrierTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def pending(self, **callbacks):
        return self.fixture.native.inspect(rollout_absent=mock.Mock(
            side_effect=RolloutObservationPending('busy')), **callbacks)

    def test_pending_never_sends_then_recovers_once(self):
        f = self.fixture
        f.barrier.inspect = self.pending
        self.assertFalse(f.prepare())
        self.assertFalse(f.writes)
        self.assertFalse(f.barrier.intent.exists())
        f.barrier.inspect = f.native.inspect
        self.assertTrue(f.prepare())
        self.assertFalse(f.prepare())
        self.assertEqual(len(f.writes), 1)

    def test_connected_pending_rechecks_pause_before_send(self):
        f = self.fixture
        def sender(*args, **kwargs):
            f.barrier.inspect = self.pending
            return f.send_control(*args, **kwargs)
        def pause(_):
            f.allowed = False
        with mock.patch.object(readiness_fixture.readiness.time, 'sleep', side_effect=pause):
            with self.assertRaises(ValueError): f.prepare(sender)
        self.assertFalse(f.writes)

    def test_pending_uses_fixed_deadline(self):
        f = self.fixture
        f.barrier.inspect = self.pending
        self.assertFalse(f.prepare())
        f.clock += 29
        self.assertFalse(f.prepare())
        f.clock += 1
        with self.assertRaises(TimeoutError): f.prepare()
        self.assertFalse(f.writes)


if __name__ == '__main__':
    unittest.main()
