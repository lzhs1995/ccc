"""Transient process reads preserve the stopped turn without granting input."""
import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as w
from tests.test_claude_delivery_recovery import DeliveryRecoveryTests
from tests.test_watch import claude_hook_event, claude_grid_payload


class IdentityRetryTests(unittest.TestCase):
    def setup_case(self, root):
        d, t, r, e, c = DeliveryRecoveryTests().setup_case(root)
        r.claude_process_pid = 1234
        d._claude_send_process_identity = lambda pid: {
            'pid': pid, 'started_epoch': 1, 'generation': 'generation-a'}
        return d, t, r, e, c

    def retry(self, d, t, r, c):
        return d._maybe_send_deferred_claude_stop(
            t, r, w.ScreenState('claude_hook_waiting'), c)

    def test_initial_unknown_lookup_parks_without_input(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            with mock.patch.object(c, 'top', side_effect=w.CmuxError('temporary RPC failure')), \
                 mock.patch.object(d._native_process_index, 'lookup', return_value=None):
                d._handle_claude_event(e,c)
            self.assertEqual(c.sent, [])
            self.assertEqual(c.sent_keys, [])
            self.assertEqual((r.claude_deferred_event or {}).get('event_id'),e['event_id'])
            self.assertEqual(r.claude_submit_phase,'none')

    def test_initial_unverified_birth_parks_without_input(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            with mock.patch.object(d,'_claude_send_process_identity',side_effect=OSError('proc unavailable')):
                d._handle_claude_event(e,c)
            self.assertEqual(c.sent,[])
            self.assertEqual((r.claude_deferred_event or {}).get('event_id'),e['event_id'])

    def test_parked_event_survives_repeated_unknown_reads_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            original = dict(r.claude_deferred_event)
            since = r.claude_deferred_since
            with mock.patch.object(d,'_candidate_process_label',return_value={'agent_kind':'unknown'}):
                for _ in range(3):
                    self.retry(d,t,r,c)
            self.assertEqual(r.claude_deferred_event,original)
            self.assertEqual(r.claude_deferred_since,since)
            self.assertEqual(c.sent,[])
            d.save()
            d2=w.WatchDaemon(d.config_path,d.state_path,client=c)
            r2=d2.runtime[t['surface_id']]
            self.assertEqual(r2.claude_deferred_event,original)
            self.assertEqual(r2.claude_deferred_since,since)
            self.assertTrue(d2.claude_event_ledger.status_of(e['event_id']).startswith('deferred_'))

    def test_unverified_birth_then_same_process_recovers_only_once_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            with mock.patch.object(d,'_claude_send_process_identity',side_effect=OSError('proc unavailable')):
                self.retry(d,t,r,c)
            self.assertEqual((r.claude_deferred_event or {}).get('event_id'),e['event_id'])
            d.save()
            d2=w.WatchDaemon(d.config_path,d.state_path,client=c)
            r2=d2.runtime[t['surface_id']]
            with mock.patch.object(d2,'_claude_send_process_identity',
                                   return_value={'pid':1234,'generation':'generation-a','started_epoch':1}):
                self.assertTrue(self.retry(d2,t,r2,c))
                self.assertFalse(self.retry(d2,t,r2,c))
            self.assertEqual(len(c.sent_text),1)
            self.assertEqual(len(c.sent_keys),1)
            self.assertEqual(r2.send_count,1)
            self.assertEqual(d2.claude_event_ledger.status_of(e['event_id']),'sent')
            self.assertIsNone(r2.claude_deferred_event)

    def test_only_exact_unavailable_verdicts_are_transient(self):
        for reason in ('process is unknown','process is unverified before preflight'):
            with self.subTest(reason=reason):
                self.assertTrue(w.WatchDaemon._cancel_is_transient(reason))
        for reason in ('process is codex','process is shell','process is other',
                       'process is unverified after write','process is unknown replacement',
                       'process is changed generation','context:claude_context_stalled',
                       'cmux send text failed after reservation: ACK lost'):
            with self.subTest(reason=reason):
                self.assertFalse(w.WatchDaemon._cancel_is_transient(reason))

    def test_proven_other_agent_drops_parked_event_without_input(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            with mock.patch.object(d,'_candidate_process_label',return_value={'agent_kind':'codex'}):
                self.retry(d,t,r,c)
            self.assertIsNone(r.claude_deferred_event)
            self.assertEqual(d.claude_event_ledger.status_of(e['event_id']),'deferred_dropped')
            self.assertEqual(c.sent,[])

    def test_replacement_birth_before_discovery_refresh_cannot_inherit_event(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            with mock.patch.object(d,'_claude_send_process_identity',
                                   return_value={'pid':1234,'generation':'replacement','started_epoch':2}):
                self.retry(d,t,r,c)
            self.assertEqual(c.sent,[])
            self.assertEqual(c.sent_keys,[])
            self.assertIsNone(r.claude_deferred_event)

    def test_missing_birth_keeps_event_without_sending(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            with mock.patch.object(d,'_claude_send_process_identity',
                                   return_value={'pid':1234,'generation':'unverified','started_epoch':0}):
                self.retry(d,t,r,c)
            self.assertEqual(c.sent,[])
            self.assertIsNotNone(r.claude_deferred_event)

    def test_observed_new_generation_session_completed_and_later_send_clear_event(self):
        for changed in ('generation','session','completed','later_send'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = self.setup_case(root)
                d._defer_claude_event(t['surface_id'],r,e,'working')
                if changed=='generation':r.claude_process_generation='new-generation'
                if changed=='session':r.claude_session_id='new-session'
                if changed=='completed':r.claude_completed_latched=True
                if changed=='later_send':r.last_send_at=r.claude_deferred_since+1
                with mock.patch.object(d,'_send_claude_event') as send:
                    self.assertFalse(self.retry(d,t,r,c))
                    send.assert_not_called()
                self.assertIsNone(r.claude_deferred_event)

    def test_human_prompt_clears_original_stop(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            d._handle_claude_event(claude_hook_event('human','UserPromptSubmit'),c)
            self.assertIsNone(r.claude_deferred_event)
            self.assertFalse(self.retry(d,t,r,c))
            self.assertEqual(c.sent,[])

    def test_paused_disabled_and_dry_run_do_not_retry(self):
        for change in ('global_paused','claude_disabled','dry_run','target_paused','target_disabled'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = self.setup_case(root)
                d._defer_claude_event(t['surface_id'],r,e,'working')
                if change=='global_paused':d.config['global_paused']=True
                if change=='claude_disabled':d.config['claude_enabled']=False
                if change=='dry_run':d.config['mode']='observe'
                if change=='target_paused':t['paused']=True
                if change=='target_disabled':t['enabled']=False
                with mock.patch.object(d,'_send_claude_event') as send:
                    self.assertFalse(self.retry(d,t,r,c))
                    send.assert_not_called()
                self.assertIsNotNone(r.claude_deferred_event)

    def test_user_draft_and_native_working_frame_preserve_but_do_not_send(self):
        for frame in (claude_grid_payload(composer='busy'),
                      claude_grid_payload(spinner='✶ Percolating… (2m 0s · ↓ 2.1k tokens)')):
            with self.subTest(frame=frame),tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = self.setup_case(root)
                d._defer_claude_event(t['surface_id'],r,e,'working')
                c.payload=frame
                self.retry(d,t,r,c)
                self.assertEqual(c.sent,[])
                self.assertEqual(c.sent_keys,[])
                self.assertIsNotNone(r.claude_deferred_event)

    def test_unknown_ack_pending_cannot_replay_parked_event(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            r.claude_submit_event_id=e['event_id']
            r.claude_submit_phase='text_written'
            r.claude_submit_write_unknown=True
            d.save()
            d2=w.WatchDaemon(d.config_path,d.state_path,client=c)
            with mock.patch.object(d2,'_send_claude_event') as send:
                self.assertFalse(self.retry(d2,t,d2.runtime[t['surface_id']],c))
                send.assert_not_called()
            self.assertEqual(c.sent,[])

    def test_historical_dropped_event_is_not_resurrected(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event(t['surface_id'],r,e,'working')
            d._clear_claude_deferred(r,reason='undeferrable: process is unknown')
            d.save()
            d2=w.WatchDaemon(d.config_path,d.state_path,client=c)
            r2=d2.runtime[t['surface_id']]
            self.assertFalse(d2._restore_expired_claude_deferred(r2))
            self.assertFalse(self.retry(d2,t,r2,c))
            self.assertEqual(c.sent,[])
