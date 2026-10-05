"""A controller rejection before input must not become permanent ACK uncertainty."""
import tempfile
import time
import unittest
from unittest import mock
import cmux_codex_watch as w
from tests import test_claude_delivery_recovery as recovery
from tests import test_cmux_control_socket as control
from tests.test_cmux_viewport_socket import server, send, response


class SubmitNotSentTests(unittest.TestCase):
    def setup_case(self, root):
        d,t,r,e,c = recovery.DeliveryRecoveryTests().setup_case(root)
        d.claude_event_ledger.mark(e, 'reserved')
        r.claude_submit_event_id = e['event_id']
        r.claude_submit_message_hash = w._short_hash(w.CLAUDE_MESSAGE)
        r.claude_submit_phase = 'text_written'
        r.claude_submit_since = time.time()
        c.send_text(t['workspace_id'], t['surface_id'], w.CLAUDE_MESSAGE)
        return d,t,r,e,c

    def test_known_key_rejection_recovers_once_after_restart_without_paste(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            with mock.patch.object(c, 'send_key', side_effect=w.InputNotSentError('admission rejected')):
                self.assertFalse(d._send_claude_enter(t,r,c,reason='first',input_check=lambda: True))
            self.assertFalse(r.claude_submit_write_unknown)
            d2 = w.WatchDaemon(d.config_path,d.state_path,client=c)
            r2 = d2.runtime[t['surface_id']]
            self.assertFalse(r2.claude_submit_write_unknown)
            self.assertEqual(r2.claude_submit_event_id,e['event_id'])
            with mock.patch.object(c, 'send_text', side_effect=AssertionError('duplicate paste')):
                self.assertTrue(d2._send_claude_enter(t,r2,c,reason='recovered',input_check=lambda: True))
            self.assertEqual(r2.send_count,1)
            self.assertEqual(d2.claude_event_ledger.status_of(e['event_id']),'sent')

    def test_known_rejection_retains_original_event_beyond_timeout_and_backs_off(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            r.claude_submit_since = time.time()-1000
            original = r.claude_submit_since
            with mock.patch.object(c,'send_key',side_effect=w.InputNotSentError('not written')):
                d._send_claude_enter(t,r,c,reason='late',input_check=lambda: True)
            with mock.patch.object(d,'_send_claude_enter') as enter:
                d._reconcile_claude_submit(t,r,w.ScreenState('composer_busy',watchdog_echo=True),c)
                enter.assert_not_called()
            self.assertEqual(r.claude_submit_event_id,e['event_id'])
            self.assertEqual(r.claude_submit_since,original)
            r.claude_submit_last_attempt_at -= 10
            with mock.patch.object(d,'_send_claude_enter') as enter:
                d._reconcile_claude_submit(t,r,w.ScreenState('composer_busy',watchdog_echo=True),c)
                enter.assert_called_once()

    def test_config_message_change_does_not_submit_a_different_transaction(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d.config['claude_message']='new continuation message'
            c.send_text(t['workspace_id'],t['surface_id'],d.config['claude_message'])
            with mock.patch.object(c,'send_key') as key:
                self.assertFalse(d._send_claude_enter(t,r,c,reason='changed',input_check=lambda: True))
                key.assert_not_called()

    def test_lost_ack_is_still_unknown_across_restart(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            with mock.patch.object(c,'send_key',side_effect=w.UncertainDeliveryError('ACK lost')) as key:
                self.assertFalse(d._send_claude_enter(t,r,c,reason='lost',input_check=lambda: True))
                self.assertTrue(r.claude_submit_write_unknown)
                d2=w.WatchDaemon(d.config_path,d.state_path,client=c)
                d2._reconcile_claude_submit(t,d2.runtime[t['surface_id']],w.ScreenState('composer_busy',watchdog_echo=True),c)
                key.assert_called_once()

    def test_message_reload_during_either_identity_check_withholds_enter(self):
        for changed_at in (1, 2):
            with self.subTest(changed_at=changed_at),tempfile.TemporaryDirectory() as root:
                d,t,r,e,c=self.setup_case(root)
                calls=0
                def reloading_identity():
                    nonlocal calls
                    calls+=1
                    if calls==changed_at:
                        d.config['claude_message']='different continuation message'
                    return True
                with mock.patch.object(c,'send_key') as key:
                    self.assertFalse(d._send_claude_enter(t,r,c,reason='reload',input_check=reloading_identity))
                    key.assert_not_called()
                self.assertFalse(r.claude_submit_write_unknown)
                self.assertTrue(r.claude_submit_not_sent)

    def test_known_text_rejection_defers_original_stop(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = recovery.DeliveryRecoveryTests().setup_case(root)
            with mock.patch.object(c,'send_text',side_effect=w.InputNotSentError('connect failed')):
                d._handle_claude_event(e,c)
            self.assertFalse(r.claude_submit_write_unknown)
            self.assertEqual(r.claude_submit_phase,'none')
            self.assertEqual((r.claude_deferred_event or {}).get('event_id'),e['event_id'])

    def test_connected_rejection_sends_zero_inputs(self):
        with server(lambda c,r: send(c,response(r))) as (transport,requests):
            client=control.ControlSocketTests().client(transport)
            with client.input_guard(lambda: False), self.assertRaises(w.InputNotSentError):
                client.send_key('workspace','surface','enter')
            self.assertEqual(requests,[])

    def test_connection_failure_is_known_not_sent(self):
        with server(lambda c,r: send(c,response(r))) as (transport,requests):
            client=control.ControlSocketTests().client(transport)
            transport.path += '.missing'
            with self.assertRaises(w.InputNotSentError):
                client.send_text('workspace','surface',w.CLAUDE_MESSAGE)
            self.assertEqual(requests,[])

    def test_read_guard_failure_is_known_not_sent(self):
        with server(lambda c,r: send(c,response(r))) as (transport,requests):
            client=control.ControlSocketTests().client(transport)
            def unavailable():
                raise w.CmuxError('connected read unavailable')
            with client.input_guard(unavailable),self.assertRaises(w.InputNotSentError):
                client.send_key('workspace','surface','enter')
            self.assertEqual(requests,[])

    def test_guard_cannot_fall_back_to_unguarded_cli(self):
        c=w.CmuxClient(runner=mock.Mock(side_effect=AssertionError('unguarded fallback')))
        with c.input_guard(lambda: True),self.assertRaises(w.InputNotSentError):
            c.send_key('workspace','surface','enter')
        c.runner.assert_not_called()

    def test_text_not_sent_then_original_event_recovers_without_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = recovery.DeliveryRecoveryTests().setup_case(root)
            with mock.patch.object(c,'send_text',side_effect=w.InputNotSentError('socket busy')):
                d._handle_claude_event(e,c)
            d.save()
            d2=w.WatchDaemon(d.config_path,d.state_path,client=c)
            r2=d2.runtime[t['surface_id']]
            with mock.patch.object(d2,'_claude_send_process_identity',return_value={'generation':'generation-a','started_epoch':1}):
                self.assertTrue(d2._maybe_send_deferred_claude_stop(t,r2,w.ScreenState('claude_hook_waiting'),c))
            self.assertEqual(r2.send_count,1)
            self.assertIsNone(r2.claude_deferred_event)
            self.assertEqual(d2.claude_event_ledger.status_of(e['event_id']),'sent')

    def test_text_ack_loss_does_not_open_deferred_paste(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = recovery.DeliveryRecoveryTests().setup_case(root)
            with mock.patch.object(c,'send_text',side_effect=w.UncertainDeliveryError('lost')) as paste:
                d._handle_claude_event(e,c)
                self.assertTrue(r.claude_submit_write_unknown)
                d._reconcile_claude_submit(t,r,w.ScreenState('composer_busy',watchdog_echo=True),c)
                paste.assert_called_once()

    def test_paused_disabled_generation_and_user_draft_cannot_recover_enter(self):
        for condition in ('paused','disabled','generation','draft'):
            with self.subTest(condition=condition),tempfile.TemporaryDirectory() as root:
                d,t,r,e,c=self.setup_case(root)
                r.claude_process_pid=1234
                if condition=='paused':d._mutate_config(lambda cfg:cfg.update(global_paused=True))
                if condition=='disabled':d._mutate_config(lambda cfg:cfg.update(claude_enabled=False))
                identity={'generation':'other' if condition=='generation' else 'generation-a','started_epoch':1}
                if condition=='draft':
                    c._submit_echo=False
                    c.payload=recovery.claude_grid_payload(composer='busy')
                with mock.patch.object(d,'_claude_send_process_identity',return_value=identity),mock.patch.object(c,'send_key') as key:
                    self.assertFalse(d._send_claude_enter(t,r,c,reason='retry'))
                    key.assert_not_called()
                self.assertFalse(r.claude_submit_write_unknown)

    def test_new_attempt_loses_ack_after_known_rejection_stays_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c=self.setup_case(root)
            for error in (w.InputNotSentError('withheld'),w.UncertainDeliveryError('lost')):
                with mock.patch.object(c,'send_key',side_effect=error):
                    self.assertFalse(d._send_claude_enter(t,r,c,reason='retry',input_check=lambda:True))
            self.assertTrue(r.claude_submit_write_unknown)
            self.assertFalse(r.claude_submit_not_sent)

    def test_failed_key_does_not_falsely_move_echo_anchor(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c=self.setup_case(root)
            r.claude_last_submit_at=123
            with mock.patch.object(c,'send_key',side_effect=w.InputNotSentError('withheld')):
                d._send_claude_enter(t,r,c,reason='retry',input_check=lambda:True)
            self.assertEqual(r.claude_last_submit_at,123)

    def test_real_socket_rejection_then_enter_only_recovery(self):
        with tempfile.TemporaryDirectory() as root,server(lambda c,r: send(c,response(r))) as (transport,requests):
            d,t,r,e,c=self.setup_case(root)
            live=control.ControlSocketTests().client(transport)
            frame=c.replay(t['workspace_id'],t['surface_id'])
            with mock.patch.object(live,'replay',return_value=frame):
                self.assertFalse(d._send_claude_enter(t,r,live,reason='connected',
                    input_check=mock.Mock(side_effect=[True,True,False])))
                self.assertFalse(r.claude_submit_write_unknown)
                self.assertEqual(requests,[])
                self.assertTrue(d._send_claude_enter(t,r,live,reason='recovered',input_check=lambda:True))
            self.assertEqual([row['method'] for row in requests],['surface.send_key'])
            self.assertEqual(r.send_count,1)
