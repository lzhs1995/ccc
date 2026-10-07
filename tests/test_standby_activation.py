"""Activation composition against local sockets, with synthetic native readiness.

No native model or cmux process is started. The fixture explicitly supplies
readiness premises; these tests certify delivery/authorization, not readiness.
"""
import contextlib
import copy
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import cmux_codex_watch as core
import ccc_standby_activation as activation
from ccc_native_standby import original
from tests import test_native_standby as ledger_fixture
from tests.test_cmux_viewport_socket import server, send, response
from tests import test_standby_readiness as readiness_fixture


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ledger_fixture.StandbyLedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ledger = self.fixture.ledger
        self.rows = self.fixture.rows
        self.job_id = str(uuid.uuid4())
        for row in self.rows:
            row.update(job_id=self.job_id, writer_lock='/synthetic/' + row['session_id'] + '.lock',
                writer_identity=[1, row['pid']], return_receipt_sha256='d' * 64,
                readiness_proven=False)
        self.allowed = True
        self.pending = set()
        self.calls = []
        self.during_observation = lambda i: None
        self.proof_change = lambda proof: proof
        patch = mock.patch.object(activation, 'boot_id', return_value=self.fixture.boot)
        patch.start(); self.addCleanup(patch.stop)

    def guard(self):
        self.calls.append(threading.get_ident())
        return self.allowed

    def proof(self, index, row):
        if index in self.pending:
            return None
        return self.proof_change(dict(readiness_proven=True, sources_complete=True,
            job_id=self.job_id, generation=self.fixture.gen, boot_id=self.fixture.boot,
            original=original(row, self.fixture.workspace),
            return_receipt_sha256=row['return_receipt_sha256'], model_request_count=0))

    def observe(self, index, *, connected_check, final_check):
        before = copy.deepcopy(self.rows[index])
        connected_check(copy.deepcopy(before))
        self.during_observation(index)
        final_check()
        if before != self.rows[index]:
            raise ValueError('synthetic original changed')
        return before

    def owner(self, transport):
        transport.control_methods = frozenset({'terminal.paste'})
        self.client = core.CmuxClient(viewport_socket=transport,
            runner=mock.Mock(side_effect=AssertionError('real cmux/CLI forbidden')))
        selected = {key: self.ledger.manifest[key] for key in
            ('policy', 'cohort_id', 'workspace_id', 'boot_id', 'mode', 'generation')}
        preparation = SimpleNamespace(client=self.client,
            jobfile=self.ledger.directory.parent / 'job.json',
            config_path=self.ledger.directory.parent / 'config.json',
            selected={**selected, 'job_id': self.job_id},
            job={'initial_prompt': 'fixed prompt', 'slots': copy.deepcopy(self.rows)},
            _surfaces={r['index']: r['surface_id'] for r in self.rows},
            _current=lambda: self.fixture.gen, _authorized=lambda *a, **kw: True,
            observe_for_activation=self.observe, close=lambda: None)
        # The ledger fixture's directory is named cohort. Production requires
        # the original job's standby directory, so rename only this empty fixture.
        target = self.ledger.directory.with_name('standby')
        self.ledger.directory.rename(target)
        self.ledger.directory = target
        owner = activation.ActivationOwner(preparation, self.ledger, readiness_proof=self.proof)
        self.addCleanup(owner.close)
        return owner

    def start(self, owner):
        return owner.manager.activate(action_id=self.fixture.action, mode='b', prompt='fixed prompt')

    def test_fifty_real_pastes_use_worker_caller_guard_and_never_replay(self):
        main = threading.get_ident()
        with server(lambda c, r: send(c, response(r, submitted=True)), backlog=128, workers=50) as (transport, requests):
            owner = self.owner(transport)
            with self.client.input_guard(self.guard):
                self.assertEqual(owner.manager.refresh()['ready_originals'], 50)
                self.assertEqual(self.start(owner)['delivery']['acknowledged_inputs'], 50)
                self.assertFalse(self.start(owner)['new_activation'])
            self.assertEqual(len(requests), 50)
            self.assertEqual({r['params']['surface_id'] for r in requests},
                {r['surface_id'] for r in self.rows})
            self.assertTrue(any(t != main for t in self.calls))
            self.assertFalse(owner._operation_active)
            self.assertIsNone(owner._operation_guard)
            self.client.runner.assert_not_called()

    def test_caller_revoked_after_connect_has_zero_socket_writes(self):
        connect = core._connect_local_socket
        def revoke(*args):
            connect(*args)
            self.allowed = False
        with server(lambda *_: self.fail('paste after revocation'), backlog=128, workers=50) as (transport, requests):
            owner = self.owner(transport)
            with self.client.input_guard(self.guard):
                owner.manager.refresh()
                with mock.patch.object(core, '_connect_local_socket', side_effect=revoke):
                    self.assertEqual(self.start(owner)['delivery']['acknowledged_inputs'], 0)
            self.assertFalse(requests)
            self.assertFalse(owner._operation_active)

    def test_old_worker_guard_is_retained(self):
        with server(lambda *_: self.fail('worker denied paste'), backlog=128, workers=50) as (transport, requests):
            owner = self.owner(transport)
            sender = owner.manager.sender
            def restricted(*args, **kwargs):
                with self.client.input_guard(lambda: False):
                    return sender(*args, **kwargs)
            owner.manager.sender = restricted
            with self.client.input_guard(self.guard):
                owner.manager.refresh()
                self.assertEqual(self.start(owner)['delivery']['acknowledged_inputs'], 0)
            self.assertFalse(requests)

    def test_one_lost_ack_never_replays_any_input(self):
        first = self.rows[0]['surface_id']
        def handler(connection, request):
            if request['params']['surface_id'] != first:
                send(connection, response(request, submitted=True))
        with server(handler, backlog=128, workers=50) as (transport, requests):
            owner = self.owner(transport)
            with self.client.input_guard(self.guard):
                owner.manager.refresh()
                result = self.start(owner)
                self.assertEqual(result['delivery']['acknowledged_inputs'], 49)
                self.assertEqual(result['state'], 'partial')
                self.assertFalse(self.start(owner)['new_activation'])
            self.assertEqual(len(requests), 50)

    def test_missing_one_proof_cannot_activate(self):
        with server(lambda *_: self.fail('incomplete ready')) as (transport, requests):
            owner = self.owner(transport)
            self.pending.add(49)
            with self.client.input_guard(self.guard):
                self.assertEqual(owner.manager.refresh()['ready_originals'], 49)
                with self.assertRaises(ValueError): self.start(owner)
            self.assertFalse(requests)
            self.assertFalse(owner._operation_active)

    def test_ready_manager_rechecks_transient_before_returning_ready(self):
        from ccc_standby_identity import ObservationPending
        with server(lambda *_: self.fail('refresh cannot send input')) as (transport, requests):
            owner = self.owner(transport)
            with self.client.input_guard(self.guard):
                self.assertEqual(owner.manager.refresh()['ready_originals'], 50)
                failures = []
                def observe(index, **kwargs):
                    if index == 0 and not failures:
                        failures.append(index)
                        raise ObservationPending(self.rows[0], lambda: None)
                    return self.observe(index, **kwargs)
                owner.preparation.observe_for_activation = observe
                self.assertEqual(owner.manager.refresh()['ready_originals'], 50)
            self.assertEqual(failures, [0])
            self.assertFalse(owner._invalid.is_set())
            self.assertFalse(requests)

    def test_nonzero_model_proof_is_rejected(self):
        with server(lambda *_: self.fail('already used native')) as (transport, requests):
            owner = self.owner(transport)
            self.proof_change = lambda p: {**p, 'model_request_count': 1}
            with self.client.input_guard(self.guard), self.assertRaises(ValueError):
                owner.manager.refresh()
            self.assertFalse(requests)
            self.assertFalse(owner._operation_active)

    def test_observer_error_waits_for_other_workers_before_clearing_action(self):
        entered, release, failed = threading.Event(), threading.Event(), threading.Event()
        with server(lambda *_: self.fail('refresh sends nothing')) as (transport, requests):
            owner = self.owner(transport)
            def observe(index):
                if index == 1:
                    entered.set()
                    if not release.wait(3): raise AssertionError('blocked test callback')
                    self.assertTrue(owner._operation_active)
                if index == 0:
                    if not entered.wait(3): raise AssertionError('worker never entered')
                    failed.set()
                    raise ValueError('early result failed')
                return None
            owner.manager.observer = observe
            with ThreadPoolExecutor(1) as executor:
                def refresh():
                    with self.client.input_guard(self.guard):
                        return owner.manager.refresh()
                future = executor.submit(refresh)
                try:
                    self.assertTrue(failed.wait(2))
                    self.assertFalse(future.done())
                    self.assertTrue(owner._operation_active)
                finally:
                    release.set()
                with self.assertRaises(ValueError): future.result(3)
            self.assertFalse(owner._operation_active)
            self.assertIsNone(owner._operation_guard)
            self.assertFalse(requests)

    def test_later_slot_failure_survives_earlier_slot_invalidation(self):
        # Results are collected in slot order. Force slot 49 to fail first,
        # then let slot 0 notice the cohort invalidation before it returns.
        with server(lambda *_: self.fail('failed refresh sends nothing')) as (transport, requests):
            owner = self.owner(transport)
            def observe(index, **kwargs):
                if index == 49:
                    raise OSError('slot 49 original inventory unavailable')
                if index == 0:
                    self.assertTrue(owner._invalid.wait(3))
                    owner._current()
                return None
            owner.preparation.observe_for_activation = observe
            with self.client.input_guard(self.guard):
                with self.assertRaisesRegex(ValueError, 'slot 49 original inventory unavailable'):
                    owner.manager.refresh()
            saved = json.loads((self.ledger.directory / 'invalidated.json').read_text())
            self.assertIn('slot 49 original inventory unavailable', saved['reason'])
            with self.assertRaisesRegex(ValueError, 'slot 49 original inventory unavailable'):
                owner._current()
            self.assertEqual(owner.manager.status()['state'], 'invalidated')
            self.assertFalse(requests)

    def test_partial_submission_also_drains_existing_workers(self):
        entered, release, failed = threading.Event(), threading.Event(), threading.Event()
        with server(lambda *_: self.fail('refresh sends nothing')) as (transport, _):
            owner = self.owner(transport)
            def observe(index):
                entered.set()
                if not release.wait(3): raise AssertionError('blocked test callback')
                self.assertTrue(owner._operation_active)
                return None
            owner.manager.observer = observe
            submit = owner.manager._executor.submit
            def selective(callback, index):
                if index == 1:
                    if not entered.wait(2): raise AssertionError('worker never entered')
                    failed.set()
                    raise RuntimeError('executor rejected second submission')
                return submit(callback, index)
            with ThreadPoolExecutor(1) as executor, mock.patch.object(owner.manager._executor, 'submit', side_effect=selective):
                future = executor.submit(owner.manager.refresh)
                try:
                    self.assertTrue(failed.wait(2))
                    self.assertFalse(future.done())
                finally:
                    release.set()
                with self.assertRaises(RuntimeError): future.result(3)
            self.assertFalse(owner._operation_active)


class ActivationTailTests(unittest.TestCase):
    def test_joined_observation_keeps_two_topology_boundaries_and_live_revocation(self):
        from ccc_standby_prepare import PreparationOwner, FreshTopology
        for mode in ('stable', 'proof_revoke', 'replay_revoke', 'last_tree_move'):
            with self.subTest(mode=mode):
                native = readiness_fixture.RefreshBarrierTests()
                native.setUp()
                try:
                    self.assertTrue(native.prepare())
                    native.rendered()
                    row = native.barrier.observe()
                    wid, sid = row['workspace_id'], row['surface_id']
                    client = core.CmuxClient(runner=mock.Mock(side_effect=AssertionError('no CLI')))
                    trees = []
                    def tree(workspace):
                        trees.append(workspace)
                        surfaces = [] if mode == 'last_tree_move' and len(trees) == 2 else [
                            {'id': sid, 'ref': 'surface:1', 'type': 'terminal'}]
                        return {'windows': [{'id': str(uuid.uuid4()), 'workspaces': [
                            {'id': wid, 'panes': [{'id': str(uuid.uuid4()), 'surfaces': surfaces}]}]}]}
                    client.workspace_tree = tree
                    prep = SimpleNamespace(job={'workspace_id': wid, 'slots': [row]},
                        client=client, _topology=FreshTopology(lambda: tree(wid)),
                        _current=lambda: native.generation,
                        _permission=lambda *_: native.allowed, _failed=threading.Event())
                    prep._authorized = lambda *a, **kw: PreparationOwner._authorization(prep, *a, **kw)
                    prep._surfaces = {0: sid}
                    prep.selected = dict(generation=native.generation, boot_id=native.boot,
                        workspace_id=wid, job_id=row['job_id'])
                    prep.observe_for_activation = lambda index, **kw: native.barrier.observe_for_activation(**kw)
                    native.barrier.authorized = lambda _, observed: prep._authorized(
                        0, surface_id=observed['surface_id'], connected=client)
                    replay = native.client.replay.side_effect
                    def screen(*args, **kwargs):
                        value = replay(*args, **kwargs)
                        if mode == 'replay_revoke': native.allowed = False
                        return value
                    native.client.replay.side_effect = screen
                    owner = activation.ActivationOwner.__new__(activation.ActivationOwner)
                    owner.preparation, owner.client = prep, client
                    owner._selected = copy.deepcopy(prep.selected)
                    owner._invalid, owner._lock = threading.Event(), threading.RLock()
                    owner._originals = {}
                    owner._operation_active, owner._operation_guard = True, lambda: native.allowed
                    owner.ledger = SimpleNamespace(clock=lambda: native.clock)
                    def proof(index, observed):
                        if mode == 'proof_revoke': native.allowed = False
                        return dict(readiness_proven=True, sources_complete=True,
                            job_id=row['job_id'], generation=native.generation, boot_id=native.boot,
                            original=original(observed, wid),
                            return_receipt_sha256=observed['return_receipt_sha256'], model_request_count=0)
                    owner.proof_reader = proof
                    if mode == 'stable':
                        self.assertTrue(owner.observe(0)['readiness_proven'])
                        self.assertEqual(trees, [wid] * 2)
                        self.assertTrue(owner.authorized(0))
                        self.assertEqual(trees, [wid] * 3)  # send authorization still reads topology
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            owner.observe(0)
                        self.assertTrue(owner._invalid.is_set())
                        self.assertEqual(len(trees), {'proof_revoke': 0,
                            'replay_revoke': 1, 'last_tree_move': 2}[mode])
                    client.runner.assert_not_called()
                finally:
                    native.doCleanups()

    def test_real_identity_fd_revocation_and_final_authorization_identity_changes(self):
        for mode in ('stable', 'revoke', 'birth', 'user_turn'):
            with self.subTest(mode=mode):
                native = readiness_fixture.RefreshBarrierTests()
                native.setUp()
                try:
                    self.assertTrue(native.prepare())
                    native.rendered()
                    self.assertIsNotNone(native.barrier.observe())
                    inspected = []
                    def inspect(**kwargs):
                        reads = []
                        def files(*args, **kw):
                            if reads and mode == 'revoke': native.allowed = False
                            reads.append(True)
                            return copy.deepcopy(native.native.files)
                        return native.native.inspect(files_reader=files, **kwargs)
                    native.barrier.inspect = inspect
                    def final():
                        inspected.append(True)
                        if mode == 'birth': native.native.process['birth'][1] += 1
                        if mode == 'user_turn':
                            with native.native.tui.open('ab') as out:
                                out.write(b'{"dir":"from_tui","kind":"op","op":"UserTurn"}\n')
                    if mode == 'stable':
                        self.assertIsNotNone(native.barrier.observe_for_activation(final_check=final))
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            native.barrier.observe_for_activation(final_check=final)
                    self.assertEqual(inspected, [True])
                finally:
                    native.doCleanups()


if __name__ == '__main__':
    unittest.main()
