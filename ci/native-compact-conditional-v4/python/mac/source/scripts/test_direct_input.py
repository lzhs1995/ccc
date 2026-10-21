"""Real journals/transcripts: direct fallback, recovery budget and receipt scope."""
import errno
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_direct_input as direct
import cmux_native_delivery as native
from native_test_support import NativeFixture, ScreenSequence


class DirectInputTests(unittest.TestCase):
    def setUp(self):
        self.f = NativeFixture.attach(self)
        self.marker = 'DIRECT_POLICY_0454_ABC123'
        self.text = 'STATUS: ' + self.marker + ' release verification'
        self.f.rpc.side_effect = FileNotFoundError(errno.ENOENT, 'missing control socket')

    def send(self, **kwargs):
        return bridge.submit_text('target', self.text, marker=self.marker,
                                  wait_seconds=0, **kwargs)

    def test_missing_socket_immediate_enter_and_wrapped_user_marker_confirms(self):
        self.f.key.side_effect = self.f.receipt_on_key('queued context\n' + self.text + '\n')
        with patch.object(bridge, 'read_screen', side_effect=AssertionError('no admission read')):
            result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], direct.TRANSPORT)
        self.assertEqual(result['terminal_input_operations'], 2)
        self.f.send.assert_called_once_with('target', self.text)
        self.f.key.assert_called_once_with('target', 'enter')
        before = self.f.send.call_count
        with self.assertRaisesRegex(bridge.TaskPackContractError, 'RECEIPT_EXISTS'):
            self.send(reconcile_only=True)
        self.assertEqual(before, self.f.send.call_count)

    def test_refused_protected_submit_never_issues_a_followup_key(self):
        self.f.submit.side_effect = RuntimeError('cmux refused an existing receiver draft')
        with self.assertRaisesRegex(RuntimeError, 'existing receiver draft'):
            self.send()
        self.f.send.assert_not_called()
        self.f.key.assert_not_called()
        with self.assertRaises(bridge.TaskPackContractError):
            self.send()
        self.f.submit.assert_called_once()

    def test_one_extra_enter_after_compaction_ends_without_second_paste(self):
        def key(surface, value):
            if self.f.key.call_count == 2:
                self.f.append_user(self.text)
        self.f.key.side_effect = key
        with patch.object(bridge, 'read_screen', side_effect=ScreenSequence(['Compacting context', '> ' + self.text])):
            result = bridge.submit_text('target', self.text, marker=self.marker, wait_seconds=1)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['extra_enter'], 1)
        self.assertEqual(self.f.key.call_count, 2)
        self.f.send.assert_called_once()

    def test_active_compaction_preserves_extra_enter_budget(self):
        with patch.object(bridge, 'read_screen', return_value='Compacting context'):
            with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
                self.send()
        self.f.send.assert_called_once()
        self.f.key.assert_called_once()
        self.assertNotIn('EXTRA_ENTER_INTENT',
            [e['phase'] for e in json.loads(next(self.f.home.rglob('attempt-0001.json')).read_text())['events']])

    def test_non_socket_enoent_never_causes_fallback(self):
        with patch('cmux_codex_input._send_native_once', side_effect=FileNotFoundError(errno.ENOENT, 'transcript gone')):
            with self.assertRaises(FileNotFoundError):
                self.send()
        self.f.send.assert_not_called()
        self.f.key.assert_not_called()

    def deferred(self):
        with patch.object(bridge, 'read_screen', return_value='Compacting context'):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.send()

    def test_deferred_then_public_recovery_confirms_once(self):
        self.deferred()
        self.f.key.side_effect = self.f.receipt_on_key(self.text)
        with patch.object(bridge, 'read_screen', return_value='> ' + self.text):
            self.assertTrue(self.send(recover_stranded=True)['confirmed'])
            with self.assertRaisesRegex(bridge.TaskPackContractError, 'RECEIPT_EXISTS'):
                self.send(recover_stranded=True)
        self.f.send.assert_called_once()
        self.assertEqual(self.f.key.call_count, 2)

    def test_deferred_still_compacting_or_marker_absent_spends_no_key(self):
        self.deferred()
        for screen in ('Compacting context ' + self.text, 'different text'):
            with patch.object(bridge, 'read_screen', return_value=screen):
                with self.assertRaises(bridge.DispatchUnconfirmed):
                    self.send(recover_stranded=True)
        self.f.key.assert_called_once()
        self.f.key.side_effect = self.f.receipt_on_key(self.text)
        with patch.object(bridge, 'read_screen', return_value='> ' + self.text):
            self.assertTrue(self.send(recover_stranded=True)['confirmed'])
        self.assertEqual(self.f.key.call_count, 2)
        self.f.send.assert_called_once()

    def test_deferred_recovery_key_failure_does_not_replenish_budget(self):
        self.deferred()
        self.f.key.side_effect = TimeoutError('uncertain extra key')
        with patch.object(bridge, 'read_screen', return_value='> ' + self.text):
            with self.assertRaises(TimeoutError):
                self.send(recover_stranded=True)
            with self.assertRaisesRegex(bridge.TaskPackContractError, 'ALREADY_USED'):
                self.send(recover_stranded=True)
        self.assertEqual(self.f.key.call_count, 2)
        self.f.send.assert_called_once()

    def test_deferred_receipt_before_recovery_needs_no_key(self):
        self.deferred()
        self.f.append_user(self.text)
        with patch.object(bridge, 'read_screen', side_effect=AssertionError('already received')):
            self.assertTrue(self.send(recover_stranded=True)['confirmed'])
        self.f.key.assert_called_once()

    def test_deferred_receiver_change_gets_no_recovery_key(self):
        self.deferred()
        self.f.process_state['birth'] = 'changed'
        with self.assertRaises(native.NativeDeliveryError):
            self.send(recover_stranded=True)
        self.f.key.assert_called_once()

    def test_body_compacting_word_does_not_block_automatic_extra_enter(self):
        self.text += ' compacting 时不补键'
        def key(*args):
            if self.f.key.call_count == 2:
                self.f.append_user(self.text)
        self.f.key.side_effect = key
        with patch.object(bridge, 'read_screen', return_value='› ' + self.text + '\n gpt-5.4'):
            self.assertTrue(self.send()['confirmed'])
        self.assertEqual(self.f.key.call_count, 2)
        self.f.send.assert_called_once()

    def test_body_compacting_word_does_not_block_later_recovery(self):
        self.text += ' compacting 时不补键'
        self.deferred()
        self.f.key.side_effect = self.f.receipt_on_key(self.text)
        with patch.object(bridge, 'read_screen', return_value='❯ ' + self.text):
            self.assertTrue(self.send(recover_stranded=True)['confirmed'])
        self.assertEqual(self.f.key.call_count, 2)

    def test_history_marker_cannot_submit_other_draft(self):
        self.deferred()
        screen = '❯ ' + self.text + '\n⏺ prior reply\n❯ other draft'
        with patch.object(bridge, 'read_screen', return_value=screen):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.send(recover_stranded=True)
        self.f.key.assert_called_once()

    def test_mixed_receiver_draft_never_gets_automatic_extra_enter(self):
        for draft in ('unfinished user question ' + self.text,
                      self.text + ' user is still typing', self.marker):
            with self.subTest(draft=draft):
                events, keys, pastes = [], [], []
                result = direct.send_once(self.text, self.marker,
                    recheck=lambda: None, emit=lambda phase, *args: events.append(phase),
                    paste=pastes.append, key=keys.append,
                    observe=lambda: {'confirmed': False, 'state': 'NATIVE_PENDING'},
                    read_screen=lambda: '> ' + draft, wait_seconds=0)
                self.assertEqual(pastes, [self.text])
                self.assertEqual(keys, ['enter'])
                self.assertEqual(result['extra_enter'], 0)
                self.assertNotIn('EXTRA_ENTER_INTENT', events)

    def test_mixed_draft_cannot_spend_deferred_original_budget(self):
        self.deferred()
        for draft in ('unfinished question ' + self.text,
                      self.text + ' extra receiver words', self.marker):
            with self.subTest(draft=draft), patch.object(
                    bridge, 'read_screen', return_value='❯ ' + draft):
                with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
                    self.send(recover_stranded=True)
                self.assertEqual(caught.exception.details['extra_enter_deferred'],
                                 'ORIGINAL_DRAFT_NOT_OWNED')
        self.f.key.assert_called_once()
        self.f.send.assert_called_once()

    def test_user_edit_after_receipt_check_prevents_extra_enter(self):
        self.deferred()
        screens = ScreenSequence(['> ' + self.text, '> ' + self.text + ' user edit'])
        with patch.object(bridge, 'read_screen', side_effect=screens):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.send(recover_stranded=True)
        self.f.key.assert_called_once()
        original = json.loads(next(self.f.home.rglob('attempt-0001.json')).read_text())
        self.assertNotIn('EXTRA_ENTER_INTENT', [e['phase'] for e in original['events']])

    def test_owned_body_does_not_require_idle_or_fullscreen_stability(self):
        for status in ('• Working (11s)', '✻ Thinking… (2m)', '◎ /goal active (2h)'):
            self.assertTrue(direct.owned_draft(status + '\n❯ ' + self.text, self.text))
        for text in (' ' + self.text, self.text + ' ', self.text + '\nextra'):
            self.assertFalse(direct.owned_draft('❯ ' + text, self.text))

    def test_status_scope_separates_compaction_from_payload_and_old_activity(self):
        for status in ('Compacting context', '• Compacting context (31s • esc to interrupt)',
                       '✻ Compacting conversation… (23s)'):
            with self.subTest(status=status):
                self.assertTrue(direct.is_compacting(status + '\n❯ ' + self.text))
                self.assertFalse(direct.is_compacting(status + '\n• Working (2s)\n› ' + self.text))
        for prompt in ('>', '›', '❯'):
            self.assertFalse(direct.is_compacting(prompt + ' compacting context ' + self.text))
        self.assertFalse(direct.is_compacting('⏺ We discussed compacting context\n❯ ' + self.text))
        self.assertFalse(direct.residual_marker(self.text + '\n❯ unrelated', self.marker))
        self.assertFalse(direct.residual_marker(self.text, self.marker))

    def test_real_compaction_background_terminal_suffix_preserves_key_budget(self):
        self.deferred()
        for suffix in ('1 background terminal running', '2 background terminals running'):
            screen = ('• Compacting context (37s • esc to interrupt) · ' + suffix
                      + ' · /ps to view\n› ' + self.text)
            self.assertTrue(direct.is_compacting(screen))
            with patch.object(bridge, 'read_screen', return_value=screen):
                with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
                    self.send(recover_stranded=True)
            self.assertEqual(caught.exception.details['extra_enter_deferred'], 'COMPACTION_STILL_ACTIVE')
        self.f.key.assert_called_once()
        self.assertFalse(direct.is_compacting('• Compactingly is a quoted word\n› ' + self.text))

    def test_consumed_extra_key_never_repeats_on_reconcile_or_resubmit(self):
        with patch.object(bridge, 'read_screen', return_value='> ' + self.text):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.send()
        self.assertEqual(self.f.key.call_count, 2)
        with self.assertRaises(bridge.TaskPackContractError):
            self.send()
        with self.assertRaises(bridge.DispatchUnconfirmed):
            self.send(reconcile_only=True)
        self.assertEqual(self.f.key.call_count, 2)
        self.f.send.assert_called_once()

    def test_receipt_race_before_extra_key_returns_without_key(self):
        def screen(*args, **kwargs):
            self.f.append_user(self.text)
            return '> ' + self.text
        with patch.object(bridge, 'read_screen', side_effect=screen):
            self.assertTrue(self.send()['confirmed'])
        self.f.key.assert_called_once()

    def test_uncertain_paste_never_sends_key_or_repastes(self):
        self.f.send.side_effect = TimeoutError('paste outcome unknown')
        with self.assertRaises(TimeoutError):
            self.send()
        with self.assertRaises(bridge.TaskPackContractError):
            self.send()
        self.f.send.assert_called_once()
        self.f.key.assert_not_called()

    def test_process_change_during_host_submit_prevents_receipt_or_further_key(self):
        def paste(*args):
            self.f.process_state['birth'] = 'different process'
        self.f.send.side_effect = paste
        with self.assertRaisesRegex(native.NativeDeliveryError, 'BIRTH'):
            self.send()
        # The host request has already attempted its paste/return. A later
        # identity change cannot undo it, and must never earn a receipt or
        # authorize another key.
        self.f.submit.assert_called_once()
        self.f.key.assert_called_once()

    def test_old_marker_and_assistant_or_tool_echo_do_not_confirm(self):
        self.f.append_user(self.text)  # before original intent
        def key(*args):
            from datetime import datetime, timezone
            for role in ('assistant', 'tool'):
                self.f.append_record({'type': 'response_item',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'payload': {'type': 'message', 'role': role,
                        'content': [{'type': 'input_text', 'text': self.text}]}})
        self.f.key.side_effect = key
        with patch.object(bridge, 'read_screen', return_value='busy without draft'):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.send()
        self.f.key.assert_called_once()

    def test_new_policy_is_pinned_before_paste_and_durable(self):
        def paste(*args):
            paths = list(self.f.home.rglob('attempt-0001.json'))
            attempt = json.loads(paths[0].read_text())
            self.assertEqual(attempt['native_binding']['receipt_marker'], self.marker)
            self.assertEqual(attempt['events'][-1]['phase'], 'ENTER_INTENT')
            self.assertEqual([e['phase'] for e in attempt['events']].count('PASTE_INTENT'), 1)
            native.original_input_event(attempt['events'], attempt['native_binding'], self.text)
        self.f.send.side_effect = paste
        self.f.key.side_effect = self.f.receipt_on_key(self.text)
        self.assertTrue(self.send()['confirmed'])


class DirectFormalDeliveryTests(unittest.TestCase):
    def enterContext(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def prepare(self, provider, callback):
        self.f = self.enterContext(NativeFixture(provider=provider))
        self.marker = 'FORMAL_DIRECT_NONCE_0454'
        self.packpath = self.f.home / 'pack.json'
        report = self.f.home / 'report.md'
        report.write_text('Completed work for this exact task.')
        self.receipt = self.f.home / 'completion-receipt.json'
        self.pack = dict(task_id='formal-direct-test', completion_nonce=self.marker,
            executor_uuid=self.f.identity['caller_surface_uuid'] if callback
                          else self.f.identity['target_surface_uuid'],
            report=str(report), callback_target='target', completion_receipt=str(self.receipt))
        self.pack['completion_callback'] = f'DONE|formal-direct-test|{self.marker}|REPORT={report}'
        self.packpath.write_text(json.dumps(self.pack))
        self.enterContext(patch.object(bridge, 'validate_task_pack_contract', return_value=self.pack))
        self.enterContext(patch('availability_contract.require_action'))
        self.f.rpc.side_effect = FileNotFoundError(errno.ENOENT, 'control socket absent')
        self.text = self.pack['completion_callback'] if callback else 'STATUS: ' + self.marker
        self.call = (lambda **kw: bridge.submit_completion_callback(str(self.packpath), wait_seconds=0, **kw)) if callback else (
            lambda **kw: bridge.submit_task_pack('target', self.text, str(self.packpath),
                marker=self.marker, wait_seconds=0, **kw))

    def test_task_pack_receipt_accepts_user_marker_during_claude_work(self):
        self.prepare('claude', False)
        self.f.key.side_effect = self.f.receipt_on_key('Queued context\n' + self.text)
        with patch.object(bridge, 'read_screen', side_effect=AssertionError('no idle gate')):
            result = self.call()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], direct.TRANSPORT)
        self.f.send.assert_called_once()
        self.f.key.assert_called_once()
        with self.assertRaises(bridge.TaskPackContractError):
            self.call()
        self.f.send.assert_called_once()

    def test_callback_socket_missing_delivers_once_without_duplicate(self):
        self.prepare('codex', True)
        self.f.key.side_effect = self.f.receipt_on_key('Context wrapper\n' + self.text + '\n')
        with patch.object(bridge, 'read_screen', side_effect=AssertionError('no idle gate')):
            result = self.call()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], direct.TRANSPORT)
        self.assertTrue(self.receipt.is_file())
        with self.assertRaisesRegex(bridge.TaskPackContractError, 'RECEIPT_EXISTS'):
            self.call(reconcile_only=True)
        self.f.send.assert_called_once()
        self.f.key.assert_called_once()

    def test_claude_queue_is_not_user_receipt_and_does_not_trigger_extra_enter(self):
        self.prepare('claude', False)
        self.f.key.side_effect = lambda *args: self.f.append_queued(self.text)
        with patch.object(bridge, 'read_screen', side_effect=AssertionError('queue needs no key')):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.call()
        self.f.key.assert_called_once()
        self.f.append_user('Delivered after queued attachment: ' + self.text)
        self.assertTrue(self.call(reconcile_only=True)['confirmed'])
        self.f.key.assert_called_once()
        self.f.send.assert_called_once()

    def deferred_recovery(self, callback):
        self.prepare('codex' if callback else 'claude', callback)
        with patch.object(bridge, 'read_screen', return_value='Compacting context'):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.call()
            with self.assertRaises(bridge.DispatchUnconfirmed):
                self.call(recover_stranded=True)
        self.f.key.assert_called_once()
        self.f.key.side_effect = self.f.receipt_on_key(self.text)
        with patch.object(bridge, 'read_screen', return_value='> ' + self.text):
            self.assertTrue(self.call(recover_stranded=True)['confirmed'])
            with self.assertRaises(bridge.TaskPackContractError):
                self.call(recover_stranded=True)
        self.assertEqual(self.f.key.call_count, 2)
        self.f.send.assert_called_once()

    def test_task_deferred_recovery_uses_original_attempt(self):
        self.deferred_recovery(False)

    def test_callback_deferred_recovery_uses_original_attempt(self):
        self.deferred_recovery(True)


if __name__ == '__main__':
    unittest.main()
