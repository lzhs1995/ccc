"""Regressions for the observed deferred / fallback double enqueue."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import cmux_codex_watch as w
from tests.test_watch import (FakeClient, claude_armed_daemon, claude_grid_payload,
    claude_idle_screen, process_fixture, claude_hook_event)


class DeliveryRecoveryTests(unittest.TestCase):
    def setup_case(self, root):
        client = FakeClient(claude_grid_payload(), text=claude_idle_screen(),
                            top=process_fixture(('surface-uuid', 'claude')))
        d = claude_armed_daemon(root, client)
        t = d.config['targets'][0]
        r = d.runtime.setdefault('surface-uuid', w.TargetRuntime())
        r.claude_session_id = 'session-uuid'
        r.claude_hook_health = 'healthy'
        r.claude_process_pid = 1001
        r.claude_process_generation = 'generation-a'
        e = dict(claude_hook_event('original', 'StopFailure'), synthetic_fallback=True,
                 episode_id='episode-a', process_generation='generation-a',
                 attempt_number=1, evidence_fingerprint='original-fingerprint')
        return d, t, r, e, client

    def test_deferred_provenance_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event('surface-uuid', r, e, 'working')
            d.save()
            restarted = w.WatchDaemon(d.config_path, d.state_path, client=c)
            parked = restarted.runtime['surface-uuid'].claude_deferred_event
            for key in ('synthetic_fallback','attempt_number','evidence_fingerprint','workspace_id','agent_pid'):
                self.assertEqual(parked.get(key), e.get(key), key)

    def test_different_ids_cannot_reserve_same_episode_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            self.assertEqual(d.claude_event_ledger.claim_detailed(e), w.CLAUDE_CLAIMED)
            d.claude_event_ledger.mark(e, 'reserved')
            ledger = w.ClaudeEventLedger(d.claude_event_ledger.path)
            other = dict(e, event_id='other-id', evidence_fingerprint='changed-chrome')
            self.assertEqual(ledger.claim_detailed(other), w.CLAUDE_DUPLICATE_SAME_EPISODE)
            self.assertEqual(ledger.claim_detailed(dict(other, attempt_number=2)), w.CLAUDE_CLAIMED)
            self.assertEqual(ledger.claim_detailed(dict(other, event_id='new-turn', episode_id='episode-b')), w.CLAUDE_CLAIMED)

    def test_late_enter_accounts_once_and_moves_echo_anchor(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event('surface-uuid', r, e, 'working')
            r.claude_fallback_episode_id = e['episode_id']
            r.claude_fallback_episode_generation = r.claude_process_generation
            r.claude_fallback_episode_session_id = r.claude_session_id
            d.claude_event_ledger.mark(e, 'reserved')
            r.claude_submit_event_id = e['event_id']
            r.claude_submit_message_hash = w._short_hash(w.CLAUDE_MESSAGE)
            r.claude_submit_phase = 'text_written'
            r.claude_submit_since = time.time()
            r.claude_last_submit_at = time.time()-100
            c.send_text(t['workspace_id'], t['surface_id'], w.CLAUDE_MESSAGE)
            before = time.time()
            self.assertTrue(d._send_claude_enter(t,r,c,reason='late',input_check=lambda: True))
            self.assertGreaterEqual(r.claude_last_submit_at, before)
            self.assertEqual(r.send_count, 1)
            self.assertEqual(d.claude_event_ledger.status_of(e['event_id']), 'sent')
            self.assertIsNone(r.claude_deferred_event)
            self.assertGreater(r.claude_fallback_sent_at, 0)
            # Repeated accounting for the original successful key is idempotent.
            d._record_claude_submission(r)
            self.assertEqual(r.send_count, 1)

    def test_confirmation_cannot_leave_same_stop_parked(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d._defer_claude_event('surface-uuid', r, e, 'working')
            r.claude_submit_event_id = e['event_id']
            r.claude_submit_phase = 'text_written'
            d._clear_claude_submit(r, reason='confirmed')
            self.assertIsNone(r.claude_deferred_event)

    def test_enter_ack_loss_preserves_pending_without_replay_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d.claude_event_ledger.mark(e, 'reserved')
            r.claude_submit_event_id = e['event_id']
            r.claude_submit_phase = 'text_written'
            r.claude_submit_since = time.time()-100
            c.send_text(t['workspace_id'], t['surface_id'], w.CLAUDE_MESSAGE)
            c.send_key = mock.Mock(side_effect=w.CmuxError('response lost after write'))
            self.assertFalse(d._send_claude_enter(t,r,c,reason='late',input_check=lambda: True))
            self.assertTrue(r.claude_submit_write_unknown)
            d.save()
            d2 = w.WatchDaemon(d.config_path,d.state_path,client=c)
            r2 = d2.runtime['surface-uuid']
            state = w.ScreenState('composer_busy', message_kind='claude', watchdog_echo=True)
            self.assertTrue(d2._reconcile_claude_submit(t,r2,state,c))
            self.assertEqual(c.send_key.call_count, 1)
            self.assertEqual(r2.send_count, 0)
            self.assertNotEqual(r2.claude_submit_phase, 'none')


    def test_expired_fallback_restores_original_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d.claude_event_ledger.mark(e, 'deferred_expired')
            r.claude_last_event_id = e['event_id']
            r.claude_deferred_reason = 'expired'
            self.assertTrue(d._restore_expired_claude_deferred(r))
            for key in ('synthetic_fallback','attempt_number','evidence_fingerprint','workspace_id','agent_pid'):
                self.assertEqual(r.claude_deferred_event.get(key),e.get(key),key)

    def test_successful_ledger_before_state_crash_does_not_reserve_again(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = self.setup_case(root)
            d.claude_event_ledger.mark(e, 'reserved')
            r.claude_submit_event_id = e['event_id']
            r.claude_submit_phase = 'text_written'
            r.claude_submit_write_unknown = True
            d.save()
            d.claude_event_ledger.mark(e, 'sent')
            d2 = w.WatchDaemon(d.config_path,d.state_path,client=c)
            self.assertTrue(d2.runtime['surface-uuid'].claude_submit_write_unknown)
            self.assertEqual(d2.claude_event_ledger.claim_detailed(dict(e,event_id='new-id')),
                             w.CLAUDE_DUPLICATE_SAME_EPISODE)

    def test_disk_revocation_at_late_key_boundary_sends_no_key(self):
        import contextlib
        for flag in ('claude_auto_discover','claude_enabled','global_paused'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = self.setup_case(root)
                def enable(config):
                    config['targets'] = []
                    config['claude_auto_discover'] = True
                d._mutate_config(enable)
                t = dict(t, source='claude_auto', source_workspace_id=t['workspace_id'], follow_agent='claude')
                d.dynamic_targets[t['surface_id']] = t
                r.claude_submit_event_id = e['event_id']
                r.claude_submit_message_hash = w._short_hash(w.CLAUDE_MESSAGE)
                r.claude_submit_phase = 'text_written'
                c.send_text(t['workspace_id'],t['surface_id'],w.CLAUDE_MESSAGE)
                sent=[]
                boundary=[]
                @contextlib.contextmanager
                def guard(check):
                    def key(*args):
                        boundary.append(True)
                        d.config_store.mutate(lambda cfg: cfg.update({flag: flag=='global_paused'}))
                        if not check():
                            raise w.CmuxError('live authorization revoked before write')
                        sent.append(args)
                    with mock.patch.object(c,'send_key',side_effect=key):
                        yield
                c.input_guard=guard
                r.claude_process_pid = 1234
                identity={'generation':'generation-a','started_epoch':1}
                with mock.patch.object(d,'_claude_send_process_identity',return_value=identity):
                    self.assertFalse(d._send_claude_enter(t,r,c,reason='late'))
                self.assertEqual(boundary,[True])
                self.assertEqual(sent,[])
                self.assertEqual(r.send_count,0)
