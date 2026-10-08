"""Transient native FD reads through the real preparation/activation join.

Only identity/process data are synthetic; no native client or model is started.
"""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import ccc_standby_activation as activation
from ccc_codex_queue import IncompleteVnodeRead, VnodeInventoryChanged
from ccc_native_standby import original
from ccc_standby_prepare import PreparationOwner
from tests import test_standby_readiness as readiness_fixture


class ActivationPendingTests(unittest.TestCase):
    def setUp(self):
        self.native = n = readiness_fixture.RefreshBarrierTests()
        n.setUp()
        self.addCleanup(n.doCleanups)
        self.assertTrue(n.prepare())
        n.rendered()
        row = n.barrier.observe()
        self.receipt = n.barrier.return_receipt.read_bytes()
        self.prep = prep = SimpleNamespace(
            client=SimpleNamespace(_input_guard_local=threading.local()),
            job={'slots': [row]}, _surfaces={0: row['surface_id']},
            _operations=[threading.RLock()], _barriers={0: n.barrier},
            _failed=threading.Event(), _current=lambda: n.generation,
            _authorized=lambda *a, **kw: n.allowed,
            selected=dict(generation=n.generation, boot_id=n.boot,
                          workspace_id=row['workspace_id'], job_id=row['job_id']))
        prep.observe_for_activation = lambda i, **kw: PreparationOwner.observe_for_activation(prep, i, **kw)
        owner = self.owner = activation.ActivationOwner.__new__(activation.ActivationOwner)
        owner.preparation, owner.client = prep, prep.client
        owner._selected = copy.deepcopy(prep.selected)
        owner._invalid, owner._lock = threading.Event(), threading.RLock()
        owner._failure, owner._originals = None, {}
        owner._operation_active, owner._operation_guard = True, lambda: n.allowed
        owner.ledger = SimpleNamespace(clock=lambda: n.clock, _consumed=lambda: False)
        self.proofs = []
        def proof(index, observed):
            self.proofs.append(copy.deepcopy(observed))
            return dict(readiness_proven=True, sources_complete=True,
                job_id=row['job_id'], generation=n.generation, boot_id=n.boot,
                original=original(observed, row['workspace_id']),
                return_receipt_sha256=observed['return_receipt_sha256'], model_request_count=0)
        owner.proof_reader = proof
        self.reads = 0
        self.fail_at = {1}
        self.failure = VnodeInventoryChanged('process vnode descriptors changed')
        def files(*a, **kw):
            self.reads += 1
            if self.fail_at is None or self.reads in self.fail_at:
                raise self.failure
            return copy.deepcopy(n.native.files)
        n.barrier.inspect = lambda **kw: n.native.inspect(files_reader=files, **kw)
        self.after_wait = lambda: None
        def wait(_):
            n.clock += 10
            self.after_wait()
            return owner._invalid.is_set()
        patches = [mock.patch.object(owner._invalid, 'wait', side_effect=wait),
                   mock.patch.object(activation, 'time', SimpleNamespace(monotonic=lambda: n.clock), create=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def assert_unchanged_control(self):
        self.assertEqual(len(self.native.writes), 1)
        self.assertEqual(self.native.barrier.return_receipt.read_bytes(), self.receipt)

    def assert_recovers(self):
        self.assertTrue(self.owner.observe(0)['readiness_proven'])
        self.assertFalse(self.owner._invalid.is_set())
        self.assertFalse(self.prep._failed.is_set())
        self.assertFalse(self.native.barrier.invalid_receipt.exists())
        self.assert_unchanged_control()

    def test_first_inventory_read_recovers(self):
        self.assert_recovers()
        self.assertEqual(len(self.proofs), 1)

    def test_second_inventory_read_rebuilds_proof_callbacks(self):
        self.fail_at = {2}
        self.failure = IncompleteVnodeRead(9, 'initial vnode read EBADF')
        self.assert_recovers()
        self.assertEqual(len(self.proofs), 2)

    def test_ready_original_waits_for_new_complete_observation(self):
        self.fail_at = set()
        self.assert_recovers()
        self.fail_at = {self.reads + 2}
        self.assert_recovers()
        self.assertEqual(len(self.proofs), 3)

    def test_pending_deadline_does_not_renew(self):
        self.fail_at = None
        with self.assertRaises(TimeoutError):
            self.owner.observe(0)
        self.assertEqual(self.native.clock, 130)
        self.assertTrue(self.owner._invalid.is_set())
        self.assert_unchanged_control()

    def rejects_after_wait(self, mutate):
        self.after_wait = mutate
        with self.assertRaises((ValueError, RuntimeError)):
            self.owner.observe(0)
        self.assertTrue(self.owner._invalid.is_set())
        self.assert_unchanged_control()

    def test_revocation_during_wait_refuses(self):
        self.rejects_after_wait(lambda: setattr(self.native, 'allowed', False))

    def test_birth_change_during_wait_refuses(self):
        self.rejects_after_wait(lambda: self.native.native.process['birth'].__setitem__(1, 99))

    def test_generation_change_during_wait_refuses(self):
        self.rejects_after_wait(lambda: setattr(self.native, 'generation', 'b' * 64))

    def test_writer_change_during_wait_refuses(self):
        def change():
            native = self.native.native
            moved = native.lock.with_suffix('.old')
            native.lock.rename(moved)
            native.lock.write_bytes(b'')
            native.files[native.lock] = dict(zip(('device', 'inode'), native.file_identity(native.lock)))
        self.rejects_after_wait(change)

    def test_user_turn_during_wait_refuses(self):
        def change():
            with self.native.native.tui.open('ab') as out:
                out.write(b'{"dir":"from_tui","kind":"op","op":"UserTurn"}\n')
        self.rejects_after_wait(change)

    def test_other_io_error_is_terminal(self):
        self.failure = IncompleteVnodeRead(13, 'permission denied')
        with self.assertRaises(IncompleteVnodeRead):
            self.owner.observe(0)
        self.assertTrue(self.prep._failed.is_set())
        self.assertTrue(self.owner._invalid.is_set())
        self.assert_unchanged_control()

    def expire_first_ready_read(self):
        self.fail_at = set()
        calls = []
        def clock():
            calls.append(True)
            if len(calls) == 1:
                self.native.clock += 2.1
            return self.native.clock
        self.owner.ledger.clock = clock
        return calls

    def test_preconsumption_age_rebuilds_complete_connected_proof(self):
        clocks = self.expire_first_ready_read()
        self.assert_recovers()
        self.assertEqual(len(clocks), 2)
        self.assertEqual(len(self.proofs), 2)

    def test_age_during_delivery_never_retries(self):
        clocks = self.expire_first_ready_read()
        self.owner.ledger._consumed = lambda: True
        with self.assertRaises(activation.ObservationExpired):
            self.owner.observe(0)
        self.assertEqual(len(clocks), 1)
        self.assertTrue(self.owner._invalid.is_set())
        self.assert_unchanged_control()

    def test_age_retry_keeps_live_authorization(self):
        self.expire_first_ready_read()
        self.rejects_after_wait(lambda: setattr(self.native, 'allowed', False))

    def test_age_retry_rejects_changed_writer(self):
        self.expire_first_ready_read()
        def change():
            native = self.native.native
            native.lock.rename(native.lock.with_suffix('.old'))
            native.lock.write_bytes(b'')
            native.files[native.lock] = dict(zip(('device', 'inode'), native.file_identity(native.lock)))
        self.rejects_after_wait(change)
