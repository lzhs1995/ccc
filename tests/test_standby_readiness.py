"""Preparation observations with synthetic identity and isolated local sockets."""
import hashlib
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import cmux_codex_watch as core
import ccc_standby_readiness as readiness
from tests import test_standby_identity as identity_fixture
from tests.test_watch import grid_payload, span
from tests.test_cmux_viewport_socket import server, send, response


class RefreshBarrierTests(unittest.TestCase):
    def setUp(self):
        self.native = identity_fixture.StandbyIdentityTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.directory = self.native.root / 'preparation'
        self.directory.mkdir(mode=0o700)
        self.reload()
        self.generation = 'a' * 64
        self.boot = str(uuid.uuid4())
        self.allowed = True
        self.lines = []
        self.composer = 'placeholder'
        self.queue = False
        self.writes = []
        self.client = mock.Mock()
        self.client.replay.side_effect = self.replay
        self.clock = 100.0
        self.barrier = self.make()

    def reload(self):
        self.native.events.append(dict(dir='from_tui', kind='op', payload={
            'ListSkills': {'cwds': [str(self.native.root)], 'force_reload': True}}))
        self.native.save_events()

    def make(self, **changes):
        args = dict(claim_sha256=self.native.sha, expected=self.native.expected,
            expected_argv=self.native.argv, sessions_root=self.native.sessions,
            client=self.client, generation_current=lambda: self.generation,
            boot_current=lambda: self.boot, authorized=lambda *_: self.allowed,
            inspect=self.native.inspect, clock=lambda: self.clock)
        args.update(changes)
        return readiness.StandbyRefreshBarrier(self.directory, self.native.claim_path, **args)

    def replay(self, workspace, surface, **kwargs):
        self.assertTrue(kwargs.get('live'))
        payload = grid_payload(self.lines, composer=self.composer, columns=240)
        payload.update(workspace_id=workspace, surface_id=surface)
        payload['render_grid']['surface_id'] = surface
        if self.queue:
            row = payload['render_grid']['cursor']['row']
            payload['render_grid']['row_spans'].append(span(row - 1, 0, '• Queued follow-up inputs'))
        return payload

    def send_control(self, client, row, text, input_id, *, write_guard):
        self.assertEqual(text, '/pwd')
        self.assertEqual(row['session_id'], self.native.session)
        with write_guard():
            self.writes.append((text, input_id))

    def prepare(self, callback=None):
        with mock.patch.object(readiness, 'send_initial', side_effect=callback or self.send_control):
            return self.barrier.prepare()

    def rendered(self):
        self.lines = ['• Current working directory: ' + str(self.native.root)]

    def unavailable_files(self, *, errno=9, mutate=None):
        from ccc_codex_queue import IncompleteVnodeRead
        def read(*args, **kwargs):
            if mutate:
                mutate()
            raise IncompleteVnodeRead(errno, 'incomplete vnode descriptor')
        return lambda **callbacks: self.native.inspect(files_reader=read, **callbacks)

    def test_write_check_uses_two_fresh_file_reads_around_screen(self):
        import copy
        self.barrier._check(before_control=True)
        events = []
        def files(*args, **kwargs):
            events.append('files')
            return copy.deepcopy(self.native.files)
        def screen(*args, **kwargs):
            events.append('screen')
            return self.replay(*args, **kwargs)
        self.client.replay.side_effect = screen
        self.barrier.inspect = lambda **cb: self.native.inspect(files_reader=files, **cb)
        self.barrier._check(before_control=True, before_write=True)
        self.assertEqual(events, ['files', 'screen', 'files'])

    def test_write_check_rejects_missing_or_duplicate_callbacks(self):
        self.barrier._check(before_control=True)
        for which in ('connected_check', 'final_check'):
            for duplicate in (False, True):
                with self.subTest(which=which, duplicate=duplicate):
                    def inspect(**callbacks):
                        callback = callbacks.pop(which)
                        if duplicate:
                            def twice(*args):
                                callback(*args)
                                callback(*args)
                            callbacks[which] = twice
                        return self.native.inspect(**callbacks)
                    self.barrier.inspect = inspect
                    with self.assertRaises(ValueError):
                        self.barrier._check(before_control=True, before_write=True)
                    self.assertFalse(self.writes)

    def test_write_check_rejects_revocation_in_second_inventory(self):
        import copy
        self.barrier._check(before_control=True)
        calls = []
        def files(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.allowed = False
            return copy.deepcopy(self.native.files)
        self.barrier.inspect = lambda **cb: self.native.inspect(files_reader=files, **cb)
        with self.assertRaises(ValueError):
            self.barrier._check(before_control=True, before_write=True)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.writes)

    def test_write_check_rejects_post_screen_identity_and_prefix_changes(self):
        for mutation in ('birth', 'writer', 'prefix'):
            with self.subTest(mutation=mutation):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    f.barrier._check(before_control=True)
                    hits = []
                    def screen(*args, **kwargs):
                        result = f.replay(*args, **kwargs)
                        hits.append(mutation)
                        if mutation == 'birth':
                            f.native.process['birth'][1] += 1
                        elif mutation == 'writer':
                            f.native.lock.rename(f.native.lock.with_suffix('.retired'))
                            f.native.lock.write_bytes(b'')
                        else:
                            f.native.events[1]['variant'] = 'InsertHistoryCell'
                            f.native.save_events()
                        return result
                    f.client.replay.side_effect = screen
                    with self.assertRaises(ValueError):
                        f.barrier._check(before_control=True, before_write=True)
                    self.assertEqual(hits, [mutation])
                    self.assertFalse(f.writes)
                finally:
                    f.doCleanups()

    def test_unavailable_vnode_waits_without_intent_then_sends_once(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)
        self.barrier.inspect = self.native.inspect
        self.assertTrue(self.prepare())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def changing_files(self, mutate=None):
        from ccc_codex_queue import VnodeInventoryChanged
        def read(*args, **kwargs):
            if mutate:
                mutate()
            raise VnodeInventoryChanged('process vnode descriptors changed')
        return lambda **callbacks: self.native.inspect(files_reader=read, **callbacks)

    def test_changed_inventory_waits_for_complete_read_then_sends_once(self):
        self.barrier.inspect = self.changing_files()
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.writes)
        self.barrier.inspect = self.native.inspect
        self.assertTrue(self.prepare())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_changed_inventory_deadline_does_not_extend(self):
        self.barrier.inspect = self.changing_files()
        self.assertFalse(self.prepare())
        self.clock += 29
        self.assertFalse(self.prepare())
        self.clock += 1
        with self.assertRaises(TimeoutError): self.prepare()
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.writes)

    def test_timeout_reports_original_slot_and_exhausted_budget(self):
        for kind in ('return', 'observation', 'attempt'):
            with self.subTest(kind=kind):
                for field in ('return', 'observation', 'attempt'):
                    setattr(self.barrier, '_' + field + '_deadline',
                            self.clock if field == kind else None)
                with self.assertRaisesRegex(TimeoutError,
                        f'index={self.barrier._index} deadline_kind={kind}'):
                    self.barrier._check_observation_deadline()
                self.assertFalse(self.writes)

    def test_complete_identity_ends_outage_before_second_pending(self):
        unavailable = self.unavailable_files()
        self.barrier.inspect = unavailable
        self.assertFalse(self.prepare())
        self.clock = 129
        calls = []
        def inspect():
            calls.append(1)
            return self.native.inspect() if len(calls) == 1 else unavailable()
        self.barrier.inspect = inspect
        self.assertFalse(self.prepare())
        self.assertEqual(self.barrier._observation_deadline, 159)
        self.assertFalse(self.barrier.intent.exists())
        self.clock = 131
        self.barrier.inspect = self.native.inspect
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_fresh_outage_after_complete_identity_still_expires(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.clock = 129
        calls = []
        unavailable = self.unavailable_files()
        def inspect():
            calls.append(1)
            return self.native.inspect() if len(calls) == 1 else unavailable()
        self.barrier.inspect = inspect
        self.assertFalse(self.prepare())
        self.clock = 159
        with self.assertRaises(TimeoutError):
            self.prepare()
        self.assertFalse(self.writes)

    def test_changed_inventory_rechecks_birth(self):
        self.barrier.inspect = self.changing_files(
            lambda: self.native.process['birth'].__setitem__(1, 43))
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_intent_persistence_mutation_rejects_write_and_restart(self):
        for change in ('birth', 'permission', 'generation', 'boot'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    persist = readiness.write_once
                    def changed(path, value):
                        result = persist(path, value)
                        if path == f.barrier.intent:
                            if change == 'birth': f.native.process['birth'][1] += 1
                            if change == 'permission': f.allowed = False
                            if change == 'generation': f.generation = 'b' * 64
                            if change == 'boot': f.boot = str(uuid.uuid4())
                        return result
                    with mock.patch.object(readiness, 'write_once', side_effect=changed):
                        with self.assertRaises(ValueError): f.prepare()
                    self.assertTrue(f.barrier.intent.exists())
                    self.assertTrue(f.barrier.invalid_receipt.exists())
                    self.assertFalse(f.writes)
                    self.assertFalse(f.prepare())
                    self.assertFalse(f.make().prepare())
                finally:
                    f.doCleanups()

    def test_changed_inventory_after_consumption_cannot_resend(self):
        persist = readiness.write_once
        def changed(path, value):
            result = persist(path, value)
            if path == self.barrier.intent:
                self.barrier.inspect = self.changing_files()
            return result
        with mock.patch.object(readiness, 'write_once', side_effect=changed):
            with self.assertRaisesRegex(ValueError,
                    'index=0.*_ObservationPreparing.*ObservationPending.*VnodeInventoryChanged'):
                self.prepare()
        self.assertTrue(self.barrier.intent.exists())
        self.barrier.inspect = self.native.inspect
        self.assertFalse(self.prepare())
        self.assertFalse(self.make().prepare())
        self.assertFalse(self.writes)

    def test_second_inspection_unavailable_does_not_consume(self):
        calls = []
        unavailable = self.unavailable_files()
        def inspect(**callbacks):
            calls.append(1)
            return unavailable(**callbacks) if len(calls) == 2 else self.native.inspect(**callbacks)
        self.barrier.inspect = inspect
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_recovered_second_read_crosses_deadline_without_consumption(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        calls = []
        def inspect():
            calls.append(1)
            if len(calls) == 2:
                self.clock += 30
            return self.native.inspect()
        self.barrier.inspect = inspect
        with self.assertRaises(TimeoutError): self.prepare()
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.writes)

    def test_write_guard_rechecks_deadline_after_complete_reads(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.barrier.inspect = self.native.inspect
        def sender(*args, **kwargs):
            def late(**callbacks):
                self.clock = 131
                return self.native.inspect(**callbacks)
            self.barrier.inspect = late
            return self.send_control(*args, **kwargs)
        with self.assertRaises(TimeoutError): self.prepare(sender)
        self.assertFalse(self.barrier.intent.exists())
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)
        self.assertFalse(self.prepare())

    def test_second_pending_preserves_first_complete_identity_and_events(self):
        for change in ('writer', 'session', 'reload', 'prefix'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    calls = []
                    unavailable = f.unavailable_files()
                    def inspect():
                        calls.append(1)
                        return unavailable() if len(calls) == 2 else f.native.inspect()
                    f.barrier.inspect = inspect
                    self.assertFalse(f.prepare())
                    n = f.native
                    if change in ('writer', 'session'):
                        old = n.lock
                        if change == 'session':
                            n.session = str(uuid.uuid4())
                            n.lock = old.with_name(n.session + '.lock')
                        else:
                            old.rename(old.with_suffix('.retired'))
                        n.lock.write_bytes(b'')
                        n.files.pop(old)
                        n.files[n.lock] = dict(zip(('device', 'inode'), n.file_identity(n.lock)))
                    elif change == 'reload':
                        f.reload()
                    else:
                        n.events[1]['variant'] = 'InsertHistoryCell'
                        n.save_events()
                    f.barrier.inspect = n.inspect
                    with self.assertRaises(ValueError): f.prepare()
                    self.assertFalse(f.writes)
                    self.assertFalse(f.barrier.intent.exists())
                finally:
                    f.doCleanups()

    def test_pending_generation_change_is_terminal(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.generation = 'b' * 64
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_complete_observation_after_deadline_cannot_send(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.clock += 30
        self.barrier.inspect = self.native.inspect
        with self.assertRaises(TimeoutError): self.prepare()
        self.assertFalse(self.writes)

    def test_unavailable_vnode_deadline_does_not_extend_on_poll(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.clock += 29
        self.assertFalse(self.prepare())
        self.clock += 1
        with self.assertRaises(TimeoutError): self.prepare()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)

    def test_unavailable_vnode_rechecks_process_and_authorization(self):
        self.barrier.inspect = self.unavailable_files(
            mutate=lambda: self.native.process['birth'].__setitem__(1, 43))
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_unavailable_vnode_revocation_is_terminal(self):
        self.barrier.inspect = self.unavailable_files()
        self.allowed = False
        with self.assertRaises(ValueError): self.prepare()
        self.assertTrue(self.barrier.invalid_receipt.exists())

    def test_other_vnode_errors_are_not_pending(self):
        self.barrier.inspect = self.unavailable_files(errno=13)
        with self.assertRaises(OSError): self.prepare()
        self.assertTrue(self.barrier.invalid_receipt.exists())

    def test_consumed_vnode_failure_cannot_resend(self):
        persist = readiness.write_once
        def changed(path, value):
            result = persist(path, value)
            if path == self.barrier.intent:
                self.barrier.inspect = self.unavailable_files()
            return result
        with mock.patch.object(readiness, 'write_once', side_effect=changed):
            with self.assertRaises(ValueError): self.prepare()
        self.assertTrue(self.barrier.intent.exists())
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.barrier.inspect = self.native.inspect
        self.assertFalse(self.prepare())
        self.assertFalse(self.make().prepare())
        self.assertFalse(self.writes)

    def test_prewrite_pending_releases_transport_without_consuming_input(self):
        def sender(*args, **kwargs):
            self.barrier.inspect = self.unavailable_files()
            return self.send_control(*args, **kwargs)
        with mock.patch.object(readiness.time, 'sleep', side_effect=AssertionError('must release transport')):
            self.assertFalse(self.prepare(sender))
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)
        self.barrier.inspect = self.native.inspect
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(self.prepare())

    def test_prewrite_pending_rechecks_revocation_before_writing(self):
        def sender(*args, **kwargs):
            self.barrier.inspect = self.unavailable_files()
            return self.send_control(*args, **kwargs)
        self.assertFalse(self.prepare(sender))
        self.allowed = False
        self.barrier.inspect = self.native.inspect
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.make().prepare())

    def test_complete_vnodes_during_startup_end_missing_inventory_episode(self):
        self.barrier.inspect = self.unavailable_files()
        self.assertFalse(self.prepare())
        self.clock += 29
        self.native.events = self.native.events[:2]
        self.native.save_events()
        self.barrier.inspect = self.native.inspect
        self.assertFalse(self.prepare())
        self.assertFalse(self.writes)
        self.clock += 31
        self.assertFalse(self.prepare())
        self.native.events.append(dict(dir='to_tui', kind='app_event', variant='StartupThreadStarted'))
        self.reload()
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_known_ack_pending_recovers_without_another_control(self):
        self.assertTrue(self.prepare())
        self.barrier.inspect = self.unavailable_files()
        self.assertIsNone(self.barrier.observe())
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.clock += 29
        self.barrier.inspect = self.native.inspect
        self.rendered()
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.clock += 31
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.assertEqual(len(self.writes), 1)

    def test_known_ack_pending_deadline_does_not_extend(self):
        self.assertTrue(self.prepare())
        self.barrier.inspect = self.unavailable_files()
        self.assertIsNone(self.barrier.observe())
        self.clock += 29
        self.assertIsNone(self.barrier.observe())
        self.clock += 1
        self.barrier.inspect = self.native.inspect
        self.rendered()
        with self.assertRaises(TimeoutError): self.barrier.observe()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_known_ack_pending_revocation_is_terminal(self):
        self.assertTrue(self.prepare())
        self.barrier.inspect = self.unavailable_files()
        self.allowed = False
        with self.assertRaises(ValueError): self.barrier.observe()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertEqual(len(self.writes), 1)

    def test_known_ack_pending_birth_change_is_terminal(self):
        self.assertTrue(self.prepare())
        self.barrier.inspect = self.unavailable_files(
            mutate=lambda: self.native.process['birth'].__setitem__(1, 43))
        with self.assertRaises(ValueError): self.barrier.observe()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertEqual(len(self.writes), 1)

    def test_pending_after_return_persistence_reuses_original_receipt(self):
        self.assertTrue(self.prepare())
        self.rendered()
        unavailable = self.unavailable_files()
        calls = []
        def inspect():
            calls.append(1)
            return unavailable() if len(calls) in (3, 4) else self.native.inspect()
        self.barrier.inspect = inspect
        self.assertIsNone(self.barrier.observe())
        raw = self.barrier.return_receipt.read_bytes()
        self.barrier.inspect = self.native.inspect
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.assertEqual(self.barrier.return_receipt.read_bytes(), raw)
        self.assertEqual(len(self.writes), 1)

    def test_ack_inventory_race_recovers_in_same_poll_without_resend(self):
        for failure_call in (2, 4):
            with self.subTest(failure_call=failure_call):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    self.assertTrue(f.prepare())
                    f.rendered()
                    unavailable = f.changing_files()
                    calls = []
                    def inspect():
                        calls.append(1)
                        return unavailable() if len(calls) == failure_call else f.native.inspect()
                    f.barrier.inspect = inspect
                    self.assertTrue(f.barrier.observe()['refresh_return_observed'])
                    self.assertEqual(len(calls), 5)
                    self.assertEqual(len(f.writes), 1)
                    self.assertIsNone(f.barrier._return_deadline)
                finally:
                    f.doCleanups()

    def test_ack_inventory_retry_is_bounded(self):
        self.assertTrue(self.prepare())
        inspect = mock.Mock(side_effect=self.unavailable_files())
        self.barrier.inspect = inspect
        self.assertIsNone(self.barrier.observe())
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(self.barrier._return_deadline, 130.0)
        self.assertEqual(len(self.writes), 1)

    def test_ack_retry_rechecks_changed_identity_permission_and_composer(self):
        for change in ('birth', 'permission', 'composer', 'generation'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    self.assertTrue(f.prepare())
                    f.rendered()
                    unavailable = f.unavailable_files()
                    calls = []
                    def inspect():
                        calls.append(1)
                        if len(calls) == 1:
                            return unavailable()
                        if change == 'birth':
                            f.native.process['birth'][1] += 1
                        elif change == 'permission':
                            f.allowed = False
                        elif change == 'composer':
                            f.composer = 'user draft'
                        else:
                            f.generation = 'b' * 64
                        return f.native.inspect()
                    f.barrier.inspect = inspect
                    with self.assertRaises(ValueError):
                        f.barrier.observe()
                    self.assertTrue(f.barrier.invalid_receipt.exists())
                    self.assertEqual(len(f.writes), 1)
                finally:
                    f.doCleanups()

    def test_ack_inventory_retry_cannot_extend_return_deadline(self):
        self.assertTrue(self.prepare())
        unavailable = self.unavailable_files(mutate=lambda: setattr(self, 'clock', 130.0))
        self.barrier.inspect = unavailable
        with self.assertRaises(TimeoutError):
            self.barrier.observe()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertEqual(len(self.writes), 1)

    def test_one_control_and_current_return_witness_is_not_full_readiness(self):
        self.assertTrue(self.prepare())
        self.assertFalse(self.prepare())
        self.assertIsNone(self.barrier.observe())
        self.rendered()
        witness = self.barrier.observe()
        self.assertTrue(witness['refresh_return_observed'])
        self.assertFalse(witness['readiness_proven'])
        self.assertFalse(witness['refresh_success_verified'])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(witness['control_receipt_sha256'],
                         hashlib.sha256(self.barrier.intent.read_bytes()).hexdigest())
        self.assertEqual(witness['return_receipt_sha256'],
                         hashlib.sha256(self.barrier.return_receipt.read_bytes()).hexdigest())
        self.assertEqual(self.barrier.observe()['session_id'], self.native.session)

    def pending_after_return_persistence(self):
        self.assertTrue(self.prepare())
        self.rendered()
        unavailable = self.unavailable_files()
        calls = []
        def inspect():
            calls.append(1)
            return unavailable() if len(calls) in (3, 4) else self.native.inspect()
        self.barrier.inspect = inspect
        self.assertIsNone(self.barrier.observe())
        self.assertTrue(self.barrier.return_receipt.exists())
        self.barrier.inspect = self.native.inspect

    def test_activation_waits_for_post_persistence_return_check(self):
        self.pending_after_return_persistence()
        self.assertIsNone(self.barrier.observe_for_activation())
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.assertTrue(self.barrier.observe_for_activation()['refresh_return_observed'])
        self.assertEqual(len(self.writes), 1)

    def test_activation_cannot_bypass_expired_post_persistence_check(self):
        self.pending_after_return_persistence()
        self.clock += 30
        with self.assertRaises(TimeoutError):
            self.barrier.observe_for_activation()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertEqual(len(self.writes), 1)

    def pending_control_screen(self, *, text='/pwd', menu=True):
        def replay(*args, **kwargs):
            payload = self.replay(*args, **kwargs)
            grid = payload['render_grid']
            row = grid['cursor']['row']
            grid['full'] = True
            grid['cursor']['column'] = len(text) + 2
            grid['row_spans'] = [s for s in grid['row_spans']
                                 if s['row'] != row or s['column'] < 2]
            grid['row_spans'].append(span(row, 2, text))
            if menu:
                grid['row_spans'].append(span(row - 2, 0, '› /pwd  show the current working directory'))
            return payload
        self.client.replay.side_effect = replay

    def test_acknowledged_control_draft_waits_then_returns_without_resend(self):
        self.assertTrue(self.prepare())
        self.pending_control_screen()
        self.assertIsNone(self.barrier.observe())
        self.assertFalse(self.barrier.return_receipt.exists())
        self.assertFalse(self.prepare())
        self.clock += 29
        self.assertIsNone(self.barrier.observe())
        self.client.replay.side_effect = self.replay
        self.rendered()
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.clock += 31
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.assertEqual(len(self.writes), 1)

    def test_control_return_deadline_starts_at_ack_and_never_extends(self):
        self.assertTrue(self.prepare())
        self.clock += 20
        self.pending_control_screen()
        self.assertIsNone(self.barrier.observe())
        self.clock += 9
        self.assertIsNone(self.barrier.observe())
        self.client.replay.side_effect = self.replay
        self.rendered()
        self.clock += 1
        with self.assertRaises(TimeoutError): self.barrier.observe()
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_ack_without_output_also_has_bounded_wait(self):
        self.assertTrue(self.prepare())
        self.assertIsNone(self.barrier.observe())
        self.clock += 30
        with self.assertRaises(TimeoutError): self.barrier.observe()
        self.assertEqual(len(self.writes), 1)

    def test_control_draft_before_ack_is_never_treated_as_pending(self):
        self.pending_control_screen()
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)
        self.assertFalse(self.barrier.intent.exists())

    def test_control_pending_rechecks_final_identity_authorization_and_events(self):
        for change in ('birth', 'permission', 'reload', 'input', 'generation'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    self.assertTrue(f.prepare())
                    f.pending_control_screen()
                    screen = f.client.replay.side_effect
                    def changed(*args, **kwargs):
                        payload = screen(*args, **kwargs)
                        if change == 'birth': f.native.process['birth'][1] += 1
                        if change == 'permission': f.allowed = False
                        if change == 'reload': f.reload()
                        if change == 'generation': f.generation = 'b' * 64
                        if change == 'input':
                            f.native.events.append(dict(dir='from_tui', kind='op', payload={
                                'UserTurn': {'items': [{'type': 'text', 'text': 'user'}]}}))
                            f.native.save_events()
                        return payload
                    f.client.replay.side_effect = changed
                    with self.assertRaises((ValueError, RuntimeError)): f.barrier.observe()
                    self.assertFalse(f.barrier.return_receipt.exists())
                    self.assertTrue(f.barrier.invalid_receipt.exists())
                    self.assertEqual(len(f.writes), 1)
                finally:
                    f.doCleanups()

    def test_control_pending_rejects_other_draft_queue_and_incomplete_frame(self):
        for change in ('text', 'menu', 'queue', 'full', 'working', 'approval'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    self.assertTrue(f.prepare())
                    f.pending_control_screen(text='/pwd x' if change == 'text' else '/pwd',
                                             menu=change != 'menu')
                    f.queue = change == 'queue'
                    screen = f.client.replay.side_effect
                    def changed(*args, **kwargs):
                        payload = screen(*args, **kwargs)
                        grid = payload['render_grid']
                        if change == 'full': grid['full'] = False
                        if change == 'working':
                            grid['row_spans'].append(span(1, 0, 'Working (0s • esc to interrupt)'))
                        if change == 'approval':
                            grid['row_spans'].extend([span(1, 0, 'Implement this plan?'),
                                span(2, 0, '1. Yes, implement this plan')])
                        return payload
                    f.client.replay.side_effect = changed
                    with self.assertRaises(ValueError): f.barrier.observe()
                    self.assertEqual(len(f.writes), 1)
                finally:
                    f.doCleanups()

    def test_control_draft_after_return_witness_is_rejected(self):
        self.assertTrue(self.prepare())
        self.rendered()
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.pending_control_screen()
        with self.assertRaises(ValueError): self.barrier.observe()
        self.assertEqual(len(self.writes), 1)

    def test_missing_reload_is_pending_without_consumption_then_progresses(self):
        self.native.events.pop(); self.native.save_events()
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.reload()
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_cached_startup_catalog_prepares_and_observes_once(self):
        self.native.events[-1]['payload']['ListSkills']['force_reload'] = False
        self.native.save_events()
        self.assertTrue(self.prepare())
        self.rendered()
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_later_cached_dispatch_invalidates_consumed_control(self):
        self.assertTrue(self.prepare())
        self.reload()
        self.native.events[-1]['payload']['ListSkills']['force_reload'] = False
        self.native.save_events()
        with self.assertRaises(ValueError):
            self.barrier.observe()
        self.assertEqual(len(self.writes), 1)

    def test_cached_dispatch_wrong_cwd_cannot_authorize_control(self):
        self.native.events[-1]['payload']['ListSkills'].update(
            force_reload=False, cwds=['/another/workspace'])
        self.native.save_events()
        with self.assertRaises((ValueError, RuntimeError)):
            self.prepare()
        self.assertFalse(self.writes)

    def first_reload_during_replay(self, after_reload=None):
        self.native.events.pop()
        self.native.save_events()
        def replay(*args, **kwargs):
            self.client.replay.side_effect = self.replay
            self.reload()
            if after_reload:
                after_reload()
            return self.replay(*args, **kwargs)
        self.client.replay.side_effect = replay

    def test_first_reload_during_reads_rechecks_before_one_control(self):
        self.first_reload_during_replay()
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)
        self.assertEqual(len(self.barrier._reloads), 1)
        self.assertTrue(self.prepare())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_first_reload_pending_cannot_rebase_or_skip_fresh_guards(self):
        for change in ('reload', 'draft', 'pause', 'birth', 'prefix'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    f.first_reload_during_replay()
                    self.assertFalse(f.prepare())
                    if change == 'reload':
                        f.reload()
                    elif change == 'draft':
                        f.composer = 'typed'
                    elif change == 'pause':
                        f.allowed = False
                    elif change == 'birth':
                        f.native.process['birth'][1] += 1
                    else:
                        f.native.events[1]['variant'] = 'InsertHistoryCell'
                        f.native.save_events()
                    with self.assertRaises(ValueError):
                        f.prepare()
                    self.assertTrue(f.barrier.invalid_receipt.exists())
                    self.assertFalse(f.barrier.intent.exists())
                    self.assertFalse(f.writes)
                finally:
                    f.doCleanups()

    def test_first_reload_arrival_does_not_hide_dangerous_changes(self):
        for change in ('second_reload', 'birth', 'pause', 'prefix', 'turn'):
            with self.subTest(change=change):
                f = RefreshBarrierTests()
                f.setUp()
                try:
                    def mutate():
                        if change == 'second_reload':
                            f.reload()
                        elif change == 'birth':
                            f.native.process['birth'][1] += 1
                        elif change == 'pause':
                            f.allowed = False
                        elif change == 'prefix':
                            f.native.events[1]['variant'] = 'InsertHistoryCell'
                            f.native.save_events()
                        else:
                            f.native.events.append(dict(dir='from_tui', kind='op', payload={
                                'UserTurn': {'items': [{'type': 'text', 'text': 'test'}]}}))
                            f.native.save_events()
                    f.first_reload_during_replay(mutate)
                    with self.assertRaises((ValueError, RuntimeError)):
                        f.prepare()
                    self.assertTrue(f.barrier.invalid_receipt.exists())
                    self.assertFalse(f.barrier.intent.exists())
                    self.assertFalse(f.writes)
                finally:
                    f.doCleanups()

    def test_reload_during_consumed_write_remains_terminal(self):
        def sender(*args, **kwargs):
            self.client.replay.side_effect = lambda *a, **k: (self.reload(), self.replay(*a, **k))[1]
            return self.send_control(*args, **kwargs)
        with self.assertRaises(ValueError):
            self.prepare(sender)
        self.assertFalse(self.barrier.intent.exists())
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)
        self.assertFalse(self.prepare())

    def test_startup_pending_preserves_original_then_progresses_without_early_input(self):
        self.native.events = self.native.events[:2]
        self.native.save_events()
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.writes)
        self.native.events.append(dict(dir='to_tui', kind='app_event', variant='StartupThreadStarted'))
        self.reload()
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_startup_loss_in_connected_precommit_is_terminal(self):
        def sender(*args, **kwargs):
            original = self.barrier.inspect
            def lost(**callbacks):
                return dict(original(**callbacks), startup_observed=False)
            self.barrier.inspect = lost
            return self.send_control(*args, **kwargs)
        with self.assertRaises(ValueError): self.prepare(sender)
        self.assertFalse(self.writes)
        self.assertFalse(self.barrier.intent.exists())
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.make().prepare())

    def test_startup_loss_in_second_identity_read_is_terminal(self):
        calls = []
        def inspect():
            calls.append(1)
            row = self.native.inspect()
            if len(calls) == 2: row['startup_observed'] = False
            return row
        self.barrier.inspect = inspect
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)
        self.assertTrue(self.barrier.invalid_receipt.exists())

    def test_pending_startup_does_not_allow_original_writer_replacement(self):
        self.native.events = self.native.events[:2]
        self.native.save_events()
        self.assertFalse(self.prepare())
        self.native.lock.rename(self.native.lock.with_suffix('.old'))
        self.native.lock.write_bytes(b'')
        self.native.files[self.native.lock] = dict(zip(('device', 'inode'),
            self.native.file_identity(self.native.lock)))
        self.native.events.append(dict(dir='to_tui', kind='app_event', variant='StartupThreadStarted'))
        self.reload()
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_complete_benign_append_during_each_connected_read_allows_one_control(self):
        def append(*args, **kwargs):
            self.native.events.append(dict(dir='to_tui', kind='app_event', variant='InsertHistoryCell'))
            self.native.save_events()
            return self.replay(*args, **kwargs)
        self.client.replay.side_effect = append
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)
        self.rendered()
        self.assertTrue(self.barrier.observe()['refresh_return_observed'])
        self.assertFalse(self.prepare())

    def test_user_turn_append_during_connected_read_is_rejected(self):
        def append(*args, **kwargs):
            self.native.events.append(dict(dir='from_tui', kind='op', payload={
                'UserTurn': {'items': [{'type': 'text', 'text': readiness.core.__name__}]}}))
            self.native.save_events()
            return self.replay(*args, **kwargs)
        self.client.replay.side_effect = append
        with self.assertRaises((ValueError, RuntimeError)): self.prepare()
        self.assertFalse(self.writes)

    def test_benign_append_inside_prefix_read_is_retried_without_replaying_control(self):
        import ccc_workspace_batch as batch
        native_read = batch._initial_event_prefix
        calls = []
        def append(claim, **kwargs):
            data = native_read(claim, **kwargs)
            if len(calls) in (0, 6):
                self.native.events.append(dict(dir='to_tui', kind='app_event', variant='InsertHistoryCell'))
                self.native.save_events()
            calls.append(1)
            return data
        with mock.patch.object(batch, '_initial_event_prefix', side_effect=append):
            self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(self.prepare())

    def test_dangerous_append_inside_prefix_read_is_not_ignored(self):
        import ccc_workspace_batch as batch
        native_read = batch._initial_event_prefix
        def append(claim, **kwargs):
            data = native_read(claim, **kwargs)
            self.native.events.append(dict(dir='from_tui', kind='op', payload={
                'UserTurn': {'items': [{'type': 'text', 'text': batch.PROMPT}]}}))
            self.native.save_events()
            return data
        with mock.patch.object(batch, '_initial_event_prefix', side_effect=append):
            with self.assertRaises((ValueError, RuntimeError)): self.prepare()
        self.assertFalse(self.writes)

    def test_original_change_while_waiting_for_reload_is_permanent(self):
        self.native.events.pop(); self.native.save_events()
        self.assertFalse(self.prepare())
        self.native.process['birth'][1] += 1
        with self.assertRaises(ValueError): self.prepare()
        self.native.process['birth'][1] -= 1
        self.reload()
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_old_output_cannot_become_new_control_witness(self):
        self.rendered()
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_wrong_or_duplicate_cwd_output_is_pending(self):
        self.prepare()
        self.lines = ['Current working directory: ' + str(self.native.root) + '-other']
        self.assertIsNone(self.barrier.observe())
        self.rendered(); self.lines *= 2
        self.assertIsNone(self.barrier.observe())

    def test_composer_and_queued_input_reject_control(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                if self.barrier.invalid_receipt.exists():
                    self.barrier.invalid_receipt.unlink()  # independent synthetic case
                self.barrier = self.make()
                self.queue = queued
                self.composer = 'placeholder' if queued else 'busy'
                with self.assertRaises(ValueError): self.prepare()
                self.assertFalse(self.writes)

    def test_final_guard_rejects_birth_permission_generation_and_boot_changes(self):
        initial_boot = self.boot
        for change in ('birth', 'permission', 'generation', 'boot'):
            with self.subTest(change=change):
                for path in self.directory.iterdir(): path.unlink()
                self.native.process['birth'] = [1000, 42]
                self.allowed, self.generation, self.boot = True, 'a' * 64, initial_boot
                self.barrier = self.make()
                def changing(*args, **kwargs):
                    if change == 'birth': self.native.process['birth'][1] += 1
                    if change == 'permission': self.allowed = False
                    if change == 'generation': self.generation = 'b' * 64
                    if change == 'boot': self.boot = str(uuid.uuid4())
                    self.send_control(*args, **kwargs)
                with self.assertRaises(ValueError): self.prepare(changing)
                self.assertFalse(self.writes)
                self.assertFalse(self.prepare())

    def test_change_during_write_lock_wait_is_rechecked_after_admission(self):
        entered = threading.Event()
        def waiting(*args, **kwargs):
            entered.set()
            self.send_control(*args, **kwargs)
        with ThreadPoolExecutor(1) as pool:
            with self.barrier._write_lock:
                future = pool.submit(self.prepare, waiting)
                self.assertTrue(entered.wait(2))
                self.native.process['birth'][1] += 1
            with self.assertRaises(ValueError): future.result(timeout=3)
        self.assertFalse(self.writes)

    def test_durable_intent_failure_cannot_send_or_replay_after_restart(self):
        with mock.patch.object(readiness.os, 'fsync', side_effect=OSError(28, 'full')):
            with self.assertRaises(OSError): self.prepare()
        self.assertFalse(self.writes)
        self.assertFalse(self.make().prepare())

    def test_lost_ack_is_consumed_without_observation_or_restart_replay(self):
        def lost(*args, **kwargs):
            self.send_control(*args, **kwargs)
            raise core.UncertainDeliveryError('lost ACK')
        with self.assertRaises(core.UncertainDeliveryError): self.prepare(lost)
        self.assertEqual(len(self.writes), 1)
        self.assertIsNone(self.barrier.observe())
        self.assertFalse(self.make().prepare())

    def test_transport_must_enter_guard_only_once(self):
        with self.assertRaises(ValueError): self.prepare(lambda *_args, **_kwargs: None)
        self.assertFalse(self.writes)

    def test_new_reload_after_control_permanently_invalidates_witness(self):
        self.prepare(); self.rendered(); self.barrier.observe()
        self.reload()
        with self.assertRaises(ValueError): self.barrier.observe()
        self.native.events.pop(); self.native.save_events()
        with self.assertRaises(ValueError): self.barrier.observe()

    def test_partial_tui_record_is_not_zero_input_evidence(self):
        self.prepare(); self.rendered()
        with self.native.tui.open('ab') as stream:
            stream.write(b'{"dir":"from_tui","payload":{"UserTurn":')
        self.assertIsNone(self.barrier.observe())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.assertFalse(self.prepare())
        self.assertEqual(len(self.writes), 1)

    def test_truncated_prefix_and_modified_receipt_are_rejected(self):
        self.prepare(); self.rendered(); self.barrier.observe()
        self.barrier.return_receipt.write_text('{}')
        with self.assertRaises(ValueError): self.barrier.observe()

    def test_authorization_callback_cannot_change_generation_before_write(self):
        calls = [0]
        def authorized(*args):
            calls[0] += 1
            if calls[0] == 2: self.generation = 'b' * 64
            return True
        self.barrier = self.make(authorized=authorized)
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.writes)

    def test_actual_socket_receives_exactly_one_atomic_local_command(self):
        with server(lambda c, r: send(c, response(r, submitted=True))) as (transport, requests):
            transport.control_methods = frozenset({'terminal.paste'})
            client = core.CmuxClient(viewport_socket=transport,
                runner=mock.Mock(side_effect=AssertionError('no external cmux')))
            client.replay = mock.Mock(side_effect=self.replay)
            self.barrier = self.make(client=client)
            self.assertTrue(self.barrier.prepare())
            self.assertFalse(self.barrier.prepare())
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['method'], 'terminal.paste')
            self.assertEqual(requests[0]['params']['text'], '/pwd')
            self.assertEqual(requests[0]['params']['submit_key'], 'enter')
            client.runner.assert_not_called()

    def test_actual_socket_connection_window_identity_change_sends_nothing(self):
        connect = core._connect_local_socket
        def changed(*args):
            connect(*args)
            self.native.process['birth'][1] += 1
        with server(lambda *_: self.fail('unexpected write')) as (transport, requests):
            transport.control_methods = frozenset({'terminal.paste'})
            client = core.CmuxClient(viewport_socket=transport,
                runner=mock.Mock(side_effect=AssertionError('no external cmux')))
            client.replay = mock.Mock(side_effect=self.replay)
            self.barrier = self.make(client=client)
            with mock.patch.object(core, '_connect_local_socket', side_effect=changed):
                with self.assertRaises(core.CmuxError): self.barrier.prepare()
            self.assertFalse(requests)

    def test_actual_socket_replay_window_revocation_sends_nothing(self):
        calls = [0]
        def replay(*args, **kwargs):
            payload = self.replay(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 2:
                self.allowed = False
            return payload
        with server(lambda *_: self.fail('unexpected write')) as (transport, requests):
            transport.control_methods = frozenset({'terminal.paste'})
            client = core.CmuxClient(viewport_socket=transport,
                runner=mock.Mock(side_effect=AssertionError('no external cmux')))
            client.replay = mock.Mock(side_effect=replay)
            self.barrier = self.make(client=client)
            with self.assertRaises(core.CmuxError): self.barrier.prepare()
            self.assertFalse(requests)
            self.assertTrue(self.barrier.invalid_receipt.exists())

    def activation_witness(self):
        self.prepare()
        self.rendered()
        self.assertIsNotNone(self.barrier.observe())

    def test_activation_observation_requires_live_owner_witness_and_never_prepares(self):
        self.assertIsNone(self.barrier.observe_for_activation())
        self.assertFalse(self.barrier.intent.exists())
        self.activation_witness()
        self.assertIsNone(self.make().observe_for_activation())
        self.assertEqual(len(self.writes), 1)

    def test_activation_observation_brackets_one_screen_with_two_live_process_reads(self):
        import copy
        self.activation_witness()
        order = []
        def process(*args, **kwargs):
            order.append('process')
            return copy.deepcopy(self.native.process)
        def files(*args, **kwargs):
            order.append('files')
            return copy.deepcopy(self.native.files)
        def replay(*args, **kwargs):
            order.append('screen')
            return self.replay(*args, **kwargs)
        def authorized(*args):
            order.append('authorized')
            return self.allowed
        self.barrier.authorized = authorized
        self.client.replay.side_effect = replay
        self.barrier.inspect = lambda **kwargs: self.native.inspect(
            process_reader=process, files_reader=files, rollout_absent=lambda *_: True, **kwargs)
        with mock.patch.object(type(self.native.root), 'rglob', side_effect=AssertionError('hot path scan')):
            row = self.barrier.observe_for_activation()
        self.assertEqual(order, ['process', 'files', 'authorized', 'screen',
                                 'files', 'authorized', 'process'])
        self.assertTrue(row['idle'] and row['composer_empty'] and row['initialized'])
        self.assertFalse(row['readiness_proven'])
        self.assertNotIn('model_request_count', row)
        self.assertEqual(row['writer_identity'], self.native.file_identity(self.native.lock))
        self.assertEqual(len(self.writes), 1)

    def test_activation_screen_window_identity_change_permanently_refuses(self):
        self.activation_witness()
        def replay(*args, **kwargs):
            payload = self.replay(*args, **kwargs)
            self.native.process['birth'][1] += 1
            return payload
        self.client.replay.side_effect = replay
        with self.assertRaises(ValueError): self.barrier.observe_for_activation()
        self.native.process['birth'][1] -= 1
        self.client.replay.side_effect = self.replay
        with self.assertRaises(ValueError): self.barrier.observe_for_activation()

    def test_activation_new_writer_cannot_replace_preparation_writer(self):
        self.activation_witness()
        self.native.lock.rename(self.native.lock.with_suffix('.previous'))
        self.native.lock.write_bytes(b'')
        self.native.files[self.native.lock] = dict(zip(('device', 'inode'),
            self.native.file_identity(self.native.lock)))
        with self.assertRaises(ValueError): self.barrier.observe_for_activation()

    def test_activation_late_replay_permission_denial_sends_nothing(self):
        import contextlib
        from ccc_standby_transport import send_initial
        self.activation_witness()
        def replay(*args, **kwargs):
            payload = self.replay(*args, **kwargs)
            self.allowed = False
            return payload
        self.client.replay.side_effect = replay
        @contextlib.contextmanager
        def guard():
            self.barrier.observe_for_activation()
            yield
        with server(lambda *_: self.fail('activation must not write')) as (transport, requests):
            transport.control_methods = frozenset({'terminal.paste'})
            client = core.CmuxClient(viewport_socket=transport,
                runner=mock.Mock(side_effect=AssertionError('no external cmux')))
            with self.assertRaises(core.CmuxError):
                send_initial(client, self.native.expected, 'task', str(uuid.uuid4()), write_guard=guard)
            self.assertFalse(requests)
            client.runner.assert_not_called()

    def test_activation_queue_or_composer_is_rejected(self):
        self.activation_witness()
        self.queue = True
        with self.assertRaises(ValueError): self.barrier.observe_for_activation()

    def test_activation_late_turn_or_reload_is_not_accepted_as_benign_append(self):
        self.activation_witness()
        def replay(*args, **kwargs):
            payload = self.replay(*args, **kwargs)
            self.reload()
            return payload
        self.client.replay.side_effect = replay
        with self.assertRaises(ValueError): self.barrier.observe_for_activation()

    def test_activation_benign_progress_preserves_original_receipts(self):
        self.activation_witness()
        before = self.barrier.return_receipt.read_bytes()
        def replay(*args, **kwargs):
            self.native.events.append(dict(dir='to_tui', kind='app_event', variant='InsertHistoryCell'))
            self.native.save_events()
            return self.replay(*args, **kwargs)
        self.client.replay.side_effect = replay
        row = self.barrier.observe_for_activation()
        self.assertTrue(row['refresh_return_observed'])
        self.assertEqual(self.barrier.return_receipt.read_bytes(), before)
        self.assertEqual(len(self.writes), 1)

    def test_activation_inspector_cannot_skip_connected_check(self):
        self.activation_witness()
        self.barrier.inspect = lambda **_kwargs: self.native.inspect()
        with self.assertRaises(ValueError): self.barrier.observe_for_activation()


if __name__ == '__main__':
    unittest.main()
