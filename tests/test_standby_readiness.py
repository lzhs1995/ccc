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
        return lambda: self.native.inspect(files_reader=read)

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

    def test_second_inspection_unavailable_does_not_consume(self):
        calls = []
        unavailable = self.unavailable_files()
        def inspect():
            calls.append(1)
            return unavailable() if len(calls) == 2 else self.native.inspect()
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
            def late():
                self.clock = 131
                return self.native.inspect()
            self.barrier.inspect = late
            return self.send_control(*args, **kwargs)
        with self.assertRaises(TimeoutError): self.prepare(sender)
        self.assertTrue(self.barrier.intent.exists())
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
        def send_with_failure(*args, **kwargs):
            self.barrier.inspect = self.unavailable_files()
            return self.send_control(*args, **kwargs)
        with self.assertRaises(OSError): self.prepare(send_with_failure)
        self.assertTrue(self.barrier.intent.exists())
        self.assertTrue(self.barrier.invalid_receipt.exists())
        self.barrier.inspect = self.native.inspect
        self.assertFalse(self.prepare())
        self.assertFalse(self.writes)

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

    def test_missing_reload_is_pending_without_consumption_then_progresses(self):
        self.native.events.pop(); self.native.save_events()
        self.assertFalse(self.prepare())
        self.assertFalse(self.barrier.intent.exists())
        self.assertFalse(self.barrier.invalid_receipt.exists())
        self.reload()
        self.assertTrue(self.prepare())
        self.assertEqual(len(self.writes), 1)

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
        with self.assertRaises(ValueError): self.barrier.observe()

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
        self.client.replay.side_effect = replay
        self.barrier.inspect = lambda **kwargs: self.native.inspect(
            process_reader=process, files_reader=files, rollout_absent=lambda *_: True, **kwargs)
        with mock.patch.object(type(self.native.root), 'rglob', side_effect=AssertionError('hot path scan')):
            row = self.barrier.observe_for_activation()
        self.assertEqual(order, ['process', 'files', 'screen', 'files', 'process'])
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
