"""A legacy orphan key must not suppress every later native Stop."""
import copy
import tempfile
import time
import unittest
from unittest import mock

import cmux_codex_watch as w
from tests import test_claude_provider_guard as provider
from tests.test_watch import claude_hook_event


class OrphanSettlementTests(unittest.TestCase):
    def known_orphan(self, root):
        d, t, r, e, c = provider.ProviderGuardTests().case(root, pending=True)
        r.claude_submit_phase = 'none'
        r.claude_submit_event_id = None
        r.claude_submit_native_identity = None
        self.assertTrue(d._send_claude_enter(
            t, r, c, reason='orphan_watchdog_echo', input_check=lambda: True))
        self.assertEqual(r.claude_submit_phase, 'enter_sent')
        self.assertFalse(r.claude_last_submit_has_receipt)
        r.claude_orphan_enter_count = 2
        return d, t, r, e, c

    def evidence(self, r):
        return {k: v for k, v in r.to_dict().items()
                if k.startswith('claude_last_submit_')
                or k in {'claude_orphan_enter_count', 'send_count'}}

    def test_known_orphan_consumption_after_restart_releases_only_pending_phase(self):
        for kind in ('working', 'menu'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.known_orphan(root)
                d.save()
                restarted = w.WatchDaemon(d.config_path, d.state_path, client=c)
                r = restarted.runtime[t['surface_id']]
                before, sent = copy.deepcopy(self.evidence(r)), list(c.sent)
                restarted._reconcile_claude_submit(t, r, w.ScreenState(kind), c)
                self.assertEqual(r.claude_submit_phase, 'none')
                self.assertEqual(r.claude_submit_confirmed_at, 0)
                self.assertEqual(self.evidence(r), before)
                self.assertEqual(c.sent, sent)

    def test_queue_unknown_write_and_unreadable_frame_do_not_settle_or_send(self):
        cases = [('queued', w.ScreenState('working', claude_native_activity='queued')),
                 ('unknown', w.ScreenState('working')),
                 ('unreadable', w.ScreenState('unknown'))]
        for name, state in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.known_orphan(root)
                r.claude_submit_write_unknown = name == 'unknown'
                sent = list(c.sent)
                d._reconcile_claude_submit(t, r, state, c)
                self.assertEqual(r.claude_submit_phase, 'enter_sent')
                self.assertEqual(r.claude_submit_write_unknown, name == 'unknown')
                self.assertEqual(c.sent, sent)
                self.assertFalse(r.claude_last_submit_has_receipt)

    def test_fresh_native_stop_retires_old_guard_after_known_orphan(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.known_orphan(root)
            provider.ProviderGuardTests().hold(d, t, r, e, provider.RPM, stamp=time.time() - 1000)
            before, sent = copy.deepcopy(self.evidence(r)), list(c.sent)
            stop = claude_hook_event('new-ordinary-stop', 'Stop', completed=False)
            stop.update(agent_pid=1234, process_generation='generation-a',
                        created_at=time.time())
            cleared = []
            original_clear = d._clear_claude_submit

            def observe_clear(runtime, **kwargs):
                original_clear(runtime, **kwargs)
                if kwargs.get('reason') == 'orphan_native_boundary':
                    cleared.append((runtime.claude_submit_phase,
                                    runtime.claude_submit_confirmed_at,
                                    copy.deepcopy(self.evidence(runtime))))

            with mock.patch.object(d, '_notify'), mock.patch.object(d, '_notify_async'), \
                    mock.patch.object(d, '_clear_claude_submit', side_effect=observe_clear):
                d._handle_claude_event(stop, c)
            self.assertEqual(cleared, [('none', 0, before)])
            self.assertEqual(r.claude_provider_guard['status'], 'released')
            # The fresh Stop owns a new legitimate transaction. Its successful
            # send can create its own receipt without rewriting the orphan.
            self.assertEqual(d.claude_event_ledger.status_of(stop['event_id']), 'sent')
            self.assertEqual(r.claude_submit_event_id, stop['event_id'])
            self.assertEqual(len(c.sent), len(sent) + 1)
            self.assertTrue(r.claude_last_submit_has_receipt)

    def test_unknown_or_foreign_native_boundary_cannot_clear_orphan(self):
        for changed in ({'unknown': True}, {'agent_pid': 9999},
                        {'session_id': 'foreign'}, {'created_at': 1.0}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.known_orphan(root)
                r.claude_submit_write_unknown = changed.get('unknown', False)
                stop = claude_hook_event('rejected-stop', 'Stop', completed=False)
                stop.update(agent_pid=1234, process_generation='generation-a',
                            created_at=time.time())
                stop.update({k: v for k, v in changed.items() if k != 'unknown'})
                sent = list(c.sent)
                d._handle_claude_event(stop, c)
                self.assertEqual(r.claude_submit_phase, 'enter_sent')
                self.assertEqual(c.sent, sent)


if __name__ == '__main__':
    unittest.main()
