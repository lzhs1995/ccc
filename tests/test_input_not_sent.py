"""Zero-write recovery through the daemon, real SQLite and a private Unix socket."""
import json
from contextlib import closing
from pathlib import Path
import socketserver
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from ccc_delivery import DeliveryStore
from ccc_provider_retry import ProviderRetryStore
from tests import test_native_enter as native
from tests.test_watch import HIGH_DEMAND_TEXT, grid_payload


ERROR = 'rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded token rate limit.'


class InputNotSentTests(unittest.TestCase):
    def setUp(self):
        self.f = f = native.NativeEnterTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.append_turn('operator', time.time() - 5, message='Fix my program')
        f.path.write_text(f.path.read_text().replace(HIGH_DEMAND_TEXT, ERROR))
        original = f.read_turn
        f.daemon.codex_queue_recovery.current_turn = lambda _: {**original(), 'model_provider': 'test-provider'}
        f.client.payload = grid_payload([], error=ERROR, columns=180)
        f.client.payload['render_grid']['surface_id'] = f.sid
        f.state = core.classify_grid(core.Grid.from_rpc(f.client.payload, f.sid))
        self.assertEqual((f.state.kind, f.state.error_type), ('recoverable_error', 'rate_limit'))
        f.daemon._delivery_store.start()
        self.addCleanup(f.daemon._delivery_store.close)
        f.daemon.config_store.mutate(lambda c: c.update(repeat_send_delay_sec=0, send_interval_sec=0))
        f.daemon._reload_config_if_changed()
        self.now = [time.time()]
        f.daemon._provider_retry = ProviderRetryStore(f.daemon._provider_retry.path,
            clock=lambda: self.now[0], jitter=lambda: 0)
        self.rows, self.pending = [], []
        self.deny = True
        self.fault = None
        self.atomic = True
        self.after_input = lambda method: None
        case = self

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while line := self.rfile.readline():
                    req = json.loads(line)
                    method = req['method']
                    case.rows.append(method)
                    result = {'workspace_id': f.wid, 'surface_id': f.sid}
                    if method == 'system.tree':
                        result = f.client.tree()
                    elif method == 'terminal.replay':
                        result.update(native.draft_frame(f.sid, 'User draft') if case.deny else f.client.payload)
                    elif method in {'terminal.paste', 'surface.send_text', 'surface.send_key'}:
                        restored = case.restored()
                        case.pending.append((method, restored.delivery_status, restored.codex_input_phase))
                        if method == 'surface.send_text':
                            f.client.payload = native.draft_frame(f.sid, core.MESSAGE)
                            if case.fault == 'exit_after_text': case.deny = True
                        if case.fault == 'eof': return
                        if method == 'terminal.paste':
                            result.update(delivery='delivered', submitted=case.fault != 'partial')
                        case.after_input(method)
                    else:
                        raise AssertionError(method)
                    self.wfile.write((json.dumps({'id': req['id'], 'ok': True, 'result': result})+'\n').encode())

        self.server = Server(str(f.root/'control.sock'), Handler)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); thread.join()
        self.addCleanup(close)
        self.transport = core.CmuxViewportSocket(max_connections=1)
        self.configure()
        self.client = core.CmuxClient(runner=lambda *a, **kw: self.fail('CLI fallback'), viewport_socket=self.transport)

    def configure(self):
        methods = ['system.tree', 'surface.send_text', 'surface.send_key']
        if self.atomic: methods.append('terminal.paste')
        self.transport.configure({'protocol': 'cmux-socket', 'version': 2,
            'socket_path': str(self.server.server_address), 'access_mode': 'automation', 'methods': methods})

    def restored(self):
        rows = {}
        DeliveryStore(self.f.daemon._delivery_store.root).restore(rows, core.TargetRuntime)
        return rows[self.f.sid]

    def restart_records(self):
        f = self.f
        old = f.daemon
        read_turn = old.codex_queue_recovery.current_turn
        old._delivery_store.close()
        old._process_snapshots.close()
        f.daemon = core.WatchDaemon(old.config_path, old.state_path, f.client)
        self.addCleanup(f.daemon._delivery_store.close)
        self.addCleanup(f.daemon._process_snapshots.close)
        f.daemon._delivery_store.start()
        f.daemon.codex_queue_recovery.current_turn = read_turn
        f.runtime = f.daemon.runtime[f.sid]
        f.daemon._provider_retry = ProviderRetryStore(f.daemon._provider_retry.path,
            clock=lambda: self.now[0], jitter=lambda: 0)

    def send(self):
        f = self.f
        f.daemon._reload_config_if_changed()
        f.daemon._handle_state(f.target, f.runtime, f.state, self.client, send_guard_tree=f.client.tree())

    def episode(self):
        with closing(sqlite3.connect(self.f.daemon._provider_retry.path)) as c:
            rows = c.execute('SELECT record FROM episodes').fetchall()
        self.assertEqual(len(rows), 1)
        return json.loads(rows[0][0])

    def initial_refusal(self):
        self.send()
        self.assertFalse(self.pending)
        self.assertIn('input not sent', self.f.runtime.last_send_error)
        self.assertEqual(self.episode()['count'], 1)
        return self.f.runtime.send_attempt_id

    def exercise_recovery(self, restart=False):
        attempt = self.initial_refusal()
        if restart: self.restart_records()
        self.deny = False
        self.send()
        self.assertEqual(self.f.runtime.delivery_status, 'accepted')
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)
        self.assertEqual(self.episode()['count'], 1)
        self.assertEqual(self.pending, [('terminal.paste', 'sending', 'paste_submit_pending')])
        self.send()
        self.assertEqual(len(self.pending), 1)

    def test_same_process_reuses_only_proven_unwritten_attempt(self): self.exercise_recovery()
    def test_restored_delivery_and_provider_reuse_same_attempt(self): self.exercise_recovery(True)

    def test_server_floor_still_blocks_original_attempt(self):
        self.initial_refusal()
        self.deny = False
        with self.f.daemon._provider_retry.transaction() as c:
            c.execute('INSERT INTO server_floors VALUES (?, ?)', ('test-provider', self.now[0]+30))
        self.send()
        self.assertFalse(self.pending)
        self.now[0] += 31
        self.send()
        self.assertEqual(len(self.pending), 1)

    def test_pause_and_draft_still_block_recovery(self):
        self.initial_refusal()
        self.deny = False
        self.f.daemon.config_store.mutate(lambda c: c.update(global_paused=True))
        self.send()
        self.assertFalse(self.pending)
        self.f.daemon.config_store.mutate(lambda c: c.update(global_paused=False))
        self.deny = True
        self.send()
        self.assertFalse(self.pending)
        self.deny = False
        self.send()
        self.assertEqual(len(self.pending), 1)

    def test_lost_ack_on_second_attempt_never_replays(self):
        self.initial_refusal()
        self.deny, self.fault = False, 'eof'
        self.send()
        self.assertEqual(self.f.runtime.delivery_status, 'unknown')
        self.restart_records()
        self.fault = None
        self.send()
        self.assertEqual(len(self.pending), 1)

    def test_partial_submit_on_second_attempt_never_replays(self):
        self.initial_refusal()
        self.deny, self.fault = False, 'partial'
        self.send()
        self.assertEqual(self.f.runtime.delivery_status, 'unknown')
        self.restart_records()
        self.send()
        self.assertEqual(len(self.pending), 1)

    def test_text_zero_write_can_retry_but_enter_refusal_cannot(self):
        self.atomic = False
        self.configure()
        attempt = self.initial_refusal()
        self.deny, self.fault = False, 'exit_after_text'
        self.send()
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)
        self.assertEqual(self.f.runtime.delivery_status, 'unknown')
        self.assertEqual([r[0] for r in self.pending], ['surface.send_text'])
        self.restart_records()
        self.deny = False
        self.send()
        self.assertEqual([r[0] for r in self.pending], ['surface.send_text'])

    def test_new_turn_never_reuses_old_attempt(self):
        attempt = self.initial_refusal()
        self.f.append_turn('new', time.time()-2, message='New work')
        self.f.path.write_text(self.f.path.read_text().replace(HIGH_DEMAND_TEXT, ERROR))
        self.now[0] += 2
        self.deny = False
        self.send()
        self.assertEqual(self.f.runtime.delivery_status, 'accepted')
        self.assertNotEqual(self.f.runtime.send_attempt_id, attempt)
        self.assertEqual(self.episode()['count'], 2)

    def test_pending_commit_failure_prevents_recovery_input(self):
        self.initial_refusal()
        self.deny = False
        with patch.object(self.f.daemon, '_save_delivery', side_effect=OSError('fsync failed')):
            self.send()
        self.assertFalse(self.pending)

    def test_old_failed_pending_record_is_not_zero_write_proof(self):
        self.initial_refusal()
        self.f.runtime.codex_input_phase = 'paste_submit_pending'
        self.f.runtime.delivery_status = 'failed'
        self.f.daemon._save_delivery(self.f.sid, self.f.runtime, True)
        self.restart_records()
        self.deny = False
        self.send()
        self.assertFalse(self.pending)

    def test_changed_process_identity_cannot_borrow_unwritten_proof(self):
        self.initial_refusal()
        self.deny = False
        read = self.f.daemon.codex_queue_recovery.current_turn
        for changes in ({'pid': 999}, {'process_start': 1}, {'model_provider': 'another-provider'}):
            with self.subTest(changes=changes):
                self.f.daemon.codex_queue_recovery.current_turn = lambda _, c=changes: {**read(_), **c}
                self.send()
                self.assertFalse(self.pending)
        self.f.daemon.codex_queue_recovery.current_turn = read

    def test_reconnect_and_new_user_input_do_not_reuse_proof(self):
        self.initial_refusal()
        self.deny = False
        read = self.f.daemon.codex_queue_recovery.current_turn
        for kind in ('task_started', 'user_message', 'unknown'):
            with self.subTest(kind=kind):
                self.f.daemon.codex_queue_recovery.current_turn = lambda _, k=kind: {**read(_), 'kind': k}
                self.send()
                self.assertFalse(self.pending)

    def change_provider(self):
        read = self.f.daemon.codex_queue_recovery.current_turn
        self.f.daemon.codex_queue_recovery.current_turn = lambda target: {
            **read(target), 'model_provider': 'another-provider'}

    def test_transport_downgrade_cannot_reuse_zero_write_proof(self):
        self.initial_refusal()
        self.deny = False
        with patch.object(self.f.daemon, '_connected_native_input', return_value=False):
            self.send()
        self.assertFalse(self.pending)

    def test_identity_drift_during_private_selection_cannot_create_new_attempt(self):
        attempt = self.initial_refusal()
        self.deny = False
        def select(*args):
            self.change_provider()
        with patch.object(self.f.daemon.private_checks, 'select', side_effect=select):
            self.send()
        self.assertFalse(self.pending)
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)

    def test_identity_drift_after_pending_commit_blocks_actual_paste(self):
        self.initial_refusal()
        self.deny = False
        persist = self.f.daemon._delivery_store.persist
        def drift(sid, runtime):
            persist(sid, runtime)
            if runtime.delivery_status == 'sending':
                self.change_provider()
        with patch.object(self.f.daemon._delivery_store, 'persist', side_effect=drift):
            self.send()
        self.assertFalse(self.pending)
        self.assertEqual(self.episode()['count'], 1)

    def test_identity_drift_after_text_blocks_enter(self):
        self.atomic = False
        self.configure()
        self.initial_refusal()
        self.deny = False
        self.after_input = lambda method: self.change_provider() if method == 'surface.send_text' else None
        self.send()
        self.assertEqual([r[0] for r in self.pending], ['surface.send_text'])
        self.assertEqual(self.f.runtime.delivery_status, 'unknown')

    def test_high_demand_identity_mismatch_and_missing_turn_do_not_fall_back(self):
        self.f.path.write_text(self.f.path.read_text().replace(ERROR, HIGH_DEMAND_TEXT))
        self.f.client.payload = grid_payload([], error=HIGH_DEMAND_TEXT, columns=180)
        self.f.client.payload['render_grid']['surface_id'] = self.f.sid
        self.f.state = core.classify_grid(core.Grid.from_rpc(self.f.client.payload, self.f.sid))
        # This non-provider error does not need a provider reservation.
        self.send()
        self.assertEqual(self.f.runtime.codex_input_phase, 'input_not_sent')
        self.deny = False
        self.change_provider()
        self.send()
        self.assertFalse(self.pending)
        self.f.daemon.codex_queue_recovery.current_turn = lambda _: None
        self.send()
        self.assertFalse(self.pending)

    def test_guarded_turn_resolves_provider_and_refuses_changed_database_binding(self):
        read = self.f.daemon.codex_queue_recovery.current_turn
        self.f.daemon.codex_queue_recovery.current_turn = lambda target: {
            k: v for k, v in read(target).items() if k != 'model_provider'}
        with patch('ccc_codex_goal.provider_for_turn', return_value='test-provider'):
            attempt = self.initial_refusal()
        self.deny = False
        for provider in (None, 'another-provider'):
            with self.subTest(provider=provider):
                with patch('ccc_codex_goal.provider_for_turn', return_value=provider):
                    self.send()
                self.assertFalse(self.pending)
        with patch('ccc_codex_goal.provider_for_turn', return_value='test-provider'):
            self.send()
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)
        self.assertEqual(len(self.pending), 1)

    def test_proof_commit_failure_does_not_leave_in_memory_authority(self):
        persist = self.f.daemon._delivery_store.persist
        def fail_proof(sid, runtime):
            if runtime.codex_input_phase == 'input_not_sent':
                raise RuntimeError('proof commit failed')
            return persist(sid, runtime)
        with patch.object(self.f.daemon._delivery_store, 'persist', side_effect=fail_proof):
            with self.assertRaisesRegex(RuntimeError, 'proof commit failed'):
                self.send()
        self.assertFalse(self.f.runtime.codex_input_not_sent)
        self.assertEqual(self.restored().codex_input_phase, 'paste_submit_pending')
        self.deny = False
        self.send()
        self.restart_records()
        self.send()
        self.assertFalse(self.pending)

    def test_pending_commit_is_restored_if_process_dies_before_socket_write(self):
        self.initial_refusal()
        self.deny = False
        with patch.object(self.client, '_control_rpc', side_effect=SystemExit('process died')):
            with self.assertRaises(SystemExit):
                self.send()
        self.restart_records()
        self.send()
        self.assertFalse(self.pending)

    def test_provider_observation_is_readiness_not_a_second_reservation(self):
        attempt = self.initial_refusal()
        self.f.daemon.config_store.mutate(lambda c: c.update(global_paused=True))
        with patch.object(self.f.daemon._provider_retry, 'reserve', wraps=self.f.daemon._provider_retry.reserve) as reserve:
            self.send()
        reserve.assert_not_called()
        self.assertEqual(self.f.runtime.send_attempt_id, attempt)
        self.assertEqual(self.episode()['count'], 1)


if __name__ == '__main__':
    unittest.main()
