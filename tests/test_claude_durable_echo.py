"""Queued delivery ownership survives compaction without swallowing human input."""
import copy
import json
import time
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests import test_claude_handshake_wait as fixture
from tests.test_watch import claude_hook_event


class DurableEchoTests(unittest.TestCase):
    write_pack = fixture.HandshakeWaitTests.write_pack
    setUp = fixture.HandshakeWaitTests.setUp

    def send(self):
        event = claude_hook_event('sent-stop', 'Stop')
        sent, reason = self.daemon._send_claude_event(event, self.target, self.runtime, self.client)
        self.assertTrue(sent, reason)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('sent-stop'), 'sent')
        self.daemon._clear_claude_submit(self.runtime, reason='user_input_conflict')
        self.runtime.claude_completed_latched = True
        self.runtime.claude_report_ready_task_id = 'report-old'
        return copy.deepcopy(self.daemon.claude_event_ledger.events['sent-stop'])

    def echo(self, event_id='delayed', delay=226, **changes):
        return fixture.native_event(event_id, 'UserPromptSubmit', core.CLAUDE_MESSAGE,
            created_at=self.runtime.claude_last_submit_at + delay, **changes)

    def handle_later(self, event):
        with mock.patch.object(core.time, 'time', return_value=event['created_at'] + 1):
            self.daemon._handle_claude_event(event, self.client)

    def test_delayed_echo_after_cancel_keeps_report_count_and_sent_outcome(self):
        self.send()
        count = self.runtime.send_count
        event = self.echo()
        self.handle_later(event)
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.claude_report_ready_task_id, 'report-old')
        self.assertEqual(self.runtime.send_count, count)
        row = self.daemon.claude_event_ledger.events['sent-stop']
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['watchdog_submission']['consumed_by'], event['event_id'])
        self.assertEqual(self.runtime.claude_last_prompt_attribution, 'watchdog_echo_durable_correlated')

    def test_second_identical_human_prompt_clears_latch_even_inside_window(self):
        self.send()
        self.handle_later(self.echo('first', delay=2))
        self.handle_later(self.echo('human', delay=3))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.send_count, 0)
        self.assertEqual(self.runtime.claude_last_prompt_attribution, 'human_exact_prompt')

    def test_pending_delayed_echo_confirms_without_resending(self):
        self.send()
        self.runtime.claude_submit_phase = 'enter_sent'
        self.runtime.claude_submit_event_id = 'sent-stop'
        sends = len(self.client.sent)
        self.handle_later(self.echo())
        self.assertEqual(self.runtime.claude_submit_phase, 'none')
        self.assertEqual(len(self.client.sent), sends)
        self.assertTrue(self.runtime.claude_completed_latched)

    def test_receipt_and_consumption_survive_restart(self):
        self.send()
        self.runtime = core.TargetRuntime.from_dict(self.runtime.to_dict())
        self.daemon.runtime['surface-uuid'] = self.runtime
        self.daemon.claude_event_ledger = core.ClaudeEventLedger(self.daemon.claude_event_ledger.path)
        event = self.echo()
        self.handle_later(event)
        self.daemon.claude_event_ledger = core.ClaudeEventLedger(self.daemon.claude_event_ledger.path)
        before = self.runtime.send_count
        self.handle_later(event)  # duplicate Hook cannot consume or send again
        self.assertEqual(self.runtime.send_count, before)
        self.handle_later(self.echo('second', delay=227))
        self.assertFalse(self.runtime.claude_completed_latched)

    def test_human_or_new_task_invalidates_unconsumed_receipt(self):
        self.send()
        human = fixture.native_event('new-task', 'UserPromptSubmit', 'Work on a new task',
            created_at=self.runtime.claude_last_submit_at + 10)
        self.handle_later(human)
        self.runtime.claude_completed_latched = True
        self.handle_later(self.echo())
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertNotIn('consumed_by', self.daemon.claude_event_ledger.events['sent-stop']['watchdog_submission'])

    def test_identity_hash_status_changes_cannot_consume(self):
        original = self.send()
        cases = [({'session_id': 'other'}, {}), ({'agent_pid': 999}, {}),
                 ({'process_generation': 'other'}, {}), ({'surface_id': 'other'}, {}),
                 ({'message_hash': 'f' * 24}, {}), ({}, {'status': 'reserved'})]
        for changes, row_changes in cases:
            with self.subTest(changes=changes, row=row_changes):
                self.daemon.claude_event_ledger.events['sent-stop'] = {**copy.deepcopy(original), **row_changes}
                event = self.echo(**changes)
                with mock.patch.object(core.time, 'time', return_value=event['created_at'] + 1):
                    self.assertEqual(self.daemon._attribute_exact_prompt(self.runtime, event), 'human_exact_prompt')
                self.assertNotIn('consumed_by', self.daemon.claude_event_ledger.events['sent-stop']['watchdog_submission'])

    def test_storage_failure_cannot_clear_report_or_consume_in_memory(self):
        self.send()
        event = self.echo()
        with mock.patch.object(core, 'atomic_write_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError), mock.patch.object(core.time, 'time', return_value=event['created_at'] + 1):
                self.daemon._attribute_exact_prompt(self.runtime, event)
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertNotIn('consumed_by', self.daemon.claude_event_ledger.events['sent-stop']['watchdog_submission'])

    def test_failed_reservation_or_consumption_cannot_swallow_human_after_restart(self):
        for field in ('reserved_by', 'consumed_by'):
            with self.subTest(write=field):
                self.setUp()
                self.send()
                event = self.echo('failed-echo')
                write = core.atomic_write_json
                failed = []

                def fail_once(path, data, *args, **kwargs):
                    receipt = data.get('events', {}).get('sent-stop', {}).get('watchdog_submission', {})
                    if receipt.get(field) == event['event_id'] and not failed:
                        failed.append(True)
                        raise OSError('injected ' + field + ' fsync failure')
                    return write(path, data, *args, **kwargs)

                with mock.patch.object(core, 'atomic_write_json', side_effect=fail_once):
                    with mock.patch.object(core.time, 'time', return_value=event['created_at'] + 1):
                        self.daemon._handle_claude_event_safely(event, self.client)
                self.assertTrue(failed)
                self.assertTrue(self.runtime.claude_completed_latched)
                self.daemon.claude_event_ledger = core.ClaudeEventLedger(self.daemon.claude_event_ledger.path)
                self.runtime = core.TargetRuntime.from_dict(self.runtime.to_dict())
                self.daemon.runtime['surface-uuid'] = self.runtime
                self.handle_later(event)  # original failed Hook remains deduplicated
                row = self.daemon.claude_event_ledger.events['sent-stop']
                self.assertEqual(row['status'], 'sent')
                self.assertEqual(row['watchdog_submission']['reserved_by'], event['event_id'])
                self.assertNotIn('consumed_by', row['watchdog_submission'])
                self.assertEqual(self.daemon.claude_event_ledger.status_of(event['event_id']), 'failed')
                self.handle_later(self.echo('real-human', delay=227))
                self.assertFalse(self.runtime.claude_completed_latched)
                self.assertIsNone(self.runtime.claude_report_ready_task_id)
                self.assertEqual(self.runtime.send_count, 0)
                self.assertEqual(self.runtime.claude_last_prompt_attribution, 'human_exact_prompt')
                self.assertEqual(len(self.client.sent_text), 1)
                self.assertEqual(len(self.client.sent_keys), 1)

    def test_unresolved_receipt_survives_capacity_pruning(self):
        self.send()
        ledger = self.daemon.claude_event_ledger
        for i in range(5):
            ledger.events[f'other-{i}'] = dict(status='completed', handled_at=time.time() + i)
        with mock.patch.object(core, 'CLAUDE_EVENT_LEDGER_LIMIT', 2):
            ledger._prune()
        self.assertIn('sent-stop', ledger.events)
        self.assertEqual(len(ledger.events), 2)
        self.handle_later(self.echo())
        self.assertTrue(self.runtime.claude_completed_latched)

    def test_claim_and_echo_reservation_remain_atomic_when_all_writes_fail(self):
        self.send()
        event = self.echo('failed-claim')
        ledger = self.daemon.claude_event_ledger
        before = ledger.path.read_bytes()
        write = core.atomic_write_json
        attempts = []

        def unavailable(path, data, *args, **kwargs):
            if path == ledger.path:
                events = data['events']
                if event['event_id'] in events:
                    attempts.append(events[event['event_id']]['status'])
                    self.assertEqual(events['sent-stop']['watchdog_submission']['reserved_by'],
                                     event['event_id'])
                    raise OSError('ledger unavailable throughout Hook handling')
            return write(path, data, *args, **kwargs)

        with mock.patch.object(core, 'atomic_write_json', side_effect=unavailable):
            with mock.patch.object(core.time, 'time', return_value=event['created_at'] + 1):
                with self.assertRaises(OSError):
                    self.daemon._handle_claude_event_safely(event, self.client)
        self.assertEqual(attempts, ['handling', 'failed'])
        self.assertEqual(ledger.path.read_bytes(), before)
        self.assertTrue(self.runtime.claude_completed_latched)
        self.daemon.claude_event_ledger = core.ClaudeEventLedger(ledger.path)
        self.runtime = core.TargetRuntime.from_dict(self.runtime.to_dict())
        self.daemon.runtime['surface-uuid'] = self.runtime
        self.handle_later(event)
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.daemon.claude_event_ledger.events['sent-stop']
                         ['watchdog_submission']['consumed_by'], event['event_id'])
        self.handle_later(self.echo('human-after-storage-recovery', delay=227))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.send_count, 0)
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)

    def test_no_legacy_fallback_when_new_receipt_is_missing(self):
        self.send()
        self.daemon.claude_event_ledger.events.pop('sent-stop')
        self.handle_later(self.echo(delay=3))
        self.assertFalse(self.runtime.claude_completed_latched)


class ForeignHandshakeTests(unittest.TestCase):
    write_pack = fixture.HandshakeWaitTests.write_pack
    setUp = fixture.HandshakeWaitTests.setUp

    def event(self, name, text, offset):
        return fixture.native_event(str(offset), name, text, created_at=self.at + offset)

    def test_wrong_provider_challenge_and_ack_hold_without_sending(self):
        text = fixture.challenge_text(self.tmp.name).replace('claude:identity', 'codex:identity').replace(
            'executor_provider == claude', 'executor_provider == codex')
        challenge = self.event('UserPromptSubmit', text, 1)
        self.assertIsNone(challenge['handshake_challenge'])
        self.daemon._handle_claude_event(challenge, self.client)
        ack = self.event('Stop', fixture.ack_text(agent='codex:identity'), 2)
        self.assertIsNone(ack['handshake_ack'])
        self.daemon._handle_claude_event(ack, self.client)
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assertTrue(self.runtime.claude_handshake_provider_conflict)
        self.assertFalse(self.client.sent)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('2'), 'handshake_provider_conflict')
        # A task envelope cannot authorize sending while provider binding is wrong.
        self.daemon._handle_claude_event(self.event('UserPromptSubmit', fixture.inline_dispatch(self.tmp.name), 3), self.client)
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.daemon._handle_claude_event(self.event('UserPromptSubmit', 'New user task', 4), self.client)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertFalse(self.runtime.claude_handshake_provider_conflict)

    def test_fresh_valid_challenge_resolves_hold(self):
        self.daemon._handle_claude_event(self.event('Stop', fixture.ack_text(agent='codex:identity'), 1), self.client)
        self.daemon._handle_claude_event(self.event('UserPromptSubmit', fixture.challenge_text(self.tmp.name), 2), self.client)
        self.assertFalse(self.runtime.claude_handshake_provider_conflict)
        self.daemon._handle_claude_event(self.event('Stop', fixture.ack_text(), 3), self.client)
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assertFalse(self.client.sent)

    def test_foreign_or_stale_challenge_cannot_hold_current_task(self):
        self.runtime.claude_turn_started_at = self.at + 5
        event = self.event('Stop', fixture.ack_text(agent='codex:identity'), 1)
        self.daemon._handle_claude_event(event, self.client)
        self.assertFalse(self.runtime.claude_handshake_wait)
        event = self.event('Stop', fixture.ack_text(agent='codex:identity'), 6)
        event['agent_pid'] = 999
        self.daemon._handle_claude_event(event, self.client)
        self.assertFalse(self.runtime.claude_handshake_wait)


if __name__ == '__main__':
    unittest.main()
