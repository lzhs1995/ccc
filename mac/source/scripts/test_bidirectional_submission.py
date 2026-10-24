"""Real send/key-count assertions for both directions; no live terminal input."""
import unittest
from unittest.mock import patch
import cmux_bridge as b
from native_test_support import LegacyTerminalFixture


class BidirectionalSubmissionTests(unittest.TestCase):
    def states(self, glyph):
        model = 'GPT-6 high' if glyph == '›' else '[claude-opus-5]'
        idle = glyph + ' \n' + model
        prompt = 'STATUS: unique-marker-20261004'
        pending = glyph + ' ' + prompt + '\n' + model
        activity = '• Read evidence' if glyph == '›' else '⏺ Read evidence'
        consumed = glyph + ' ' + prompt + '\n' + activity + '\n' + idle
        return idle, prompt, pending, consumed

    def run_case(self, glyph, screens, expected, keys, sends=1, *, original_draft=None):
        _, text, draft, _ = self.states(glyph)
        if original_draft is not None:
            draft = original_draft
        with LegacyTerminalFixture(provider='codex' if glyph == '›' else 'claude') as native:
            reads, sent_keys = 0, 0
            def observe(*args, **kwargs):
                nonlocal reads
                reads += 1
                if reads == 1:
                    return screens[0]
                if sent_keys == 0:
                    return draft
                return screens[min(sent_keys, len(screens) - 1)]
            def receive(surface, key):
                nonlocal sent_keys
                sent_keys += 1
                if expected and sent_keys == keys:
                    native.append_user(text)
            with patch.object(b, 'read_screen', side_effect=observe), \
                    patch.object(b, 'send_text') as send, \
                    patch.object(b, 'send_key', side_effect=receive) as key:
                if expected:
                    result = b.submit_text('peer', text, marker='unique-marker-20261004')
                    self.assertTrue(result['confirmed'])
                    self.assertEqual(result['confirmation_source'], 'native_user_message_v1')
                else:
                    with self.assertRaises((b.DispatchUnconfirmed, b.TaskPackContractError)):
                        b.submit_text('peer', text, marker='unique-marker-20261004')
                self.assertEqual(send.call_count, sends)
                if sends:
                    send.assert_called_once_with('peer', text)
                self.assertEqual(key.call_count, keys)
                self.assertTrue(all(c.args == ('peer', 'enter') for c in key.call_args_list))

    def test_prompt_and_callback_confirm_only_after_consumption(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, _, consumed = self.states(glyph)
                self.run_case(glyph, [idle, consumed], True, 1)

    def test_idle_pending_never_gets_automatic_second_enter(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, pending, consumed = self.states(glyph)
                # 后一帧只有第二个键才会出现；首次投递不得为等它而补键。
                self.run_case(glyph, [idle, pending, consumed], False, 1)

    def test_still_pending_after_first_enter_is_never_success(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, pending, _ = self.states(glyph)
                self.run_case(glyph, [idle, pending, pending], False, 1)

    def test_busy_pending_is_preserved_without_extra_enter(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, pending, _ = self.states(glyph)
                self.run_case(glyph, [idle, '• Compacting context (34s • esc to interrupt)\n'+pending], False, 1)

    def test_unrelated_new_activity_does_not_prove_delivery(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, _, _ = self.states(glyph)
                self.run_case(glyph, [idle, '• Read unrelated data\n'+idle], False, 1)

    def test_stale_echo_does_not_prove_a_second_delivery(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                _, _, _, consumed = self.states(glyph)
                self.run_case(glyph, [consumed, consumed], False, 0, sends=0)

    def test_visible_queue_never_counts_as_consumption(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, prompt, _, _ = self.states(glyph)
                screen = 'Messages to be submitted after next tool call\n'+prompt+'\n• Read unrelated data\n'+idle
                self.run_case(glyph, [idle, screen], False, 1)

    def test_marker_wrapping_retains_correct_pending_and_consumed_states(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, pending, consumed = self.states(glyph)
                wrapped = lambda s: s.replace('unique-marker-20261004', 'unique-marker-\n  20261004')
                self.assertTrue(b.compose_contains(wrapped(pending), 'unique-marker-20261004'))
                self.run_case(glyph, [idle, wrapped(pending), wrapped(consumed)], False, 1)
                self.run_case(glyph, [idle, wrapped(consumed)], True, 1)

    def test_enter_added_blank_rows_never_trigger_another_key(self):
        for glyph in ['›', '❯']:
            idle, _, pending, _ = self.states(glyph)
            for added in ['', '\n', ' ', '  ', '\n  ', ' \n  ']:
                altered = pending.replace('\n', '\n' + added + '\n', 1)
                with self.subTest(glyph=glyph, added=repr(added)):
                    self.run_case(glyph, [idle, altered], False, 1)

    def test_bordered_claude_enter_added_blank_rows_never_trigger_another_key(self):
        _, text, _, _ = self.states('❯')
        border = '─' * 72
        idle = border + '\n❯ \n' + border + '\n[claude-opus-5]'
        draft = border + '\n❯ ' + text + '\n' + border + '\n[claude-opus-5]'
        for added in ['', '\n', ' ', '  ', '\n  ']:
            altered = draft.replace(text + '\n', text + '\n' + added + '\n', 1)
            with self.subTest(added=repr(added)):
                self.run_case('❯', [idle, altered], False, 1, original_draft=draft)

    def test_unrelated_draft_after_enter_is_not_confirmed(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, _, consumed = self.states(glyph)
                self.run_case(glyph, [idle, consumed.replace(glyph+' \n', glyph+' please preserve my draft\n')], False, 1)

    def test_unknown_receiver_after_enter_is_not_success(self):
        for glyph in ['›', '❯']:
            with self.subTest(glyph=glyph):
                idle, _, _, consumed = self.states(glyph)
                self.run_case(glyph, [idle, consumed+'\nunknown interpreter'], False, 1)

    def test_codex_explicit_tab_queues_exact_own_payload_once(self):
        idle, text, pending, consumed = self.states('›')
        busy = '• Working (3s • esc to interrupt)\n' + pending + '\ntab to queue message'
        queue = 'Messages to be submitted after next tool call\n' + text + '\n' + idle
        for after, accepted in [(queue, False), (consumed, True), (busy, False)]:
            with self.subTest(after=after), LegacyTerminalFixture() as native, \
                    patch.object(b, 'read_screen', side_effect=native.ready_screens(idle, busy, after)), \
                    patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key, \
                    patch.object(b.time, 'sleep'):
                if accepted:
                    key.side_effect = native.receipt_on_key(text)
                    self.assertTrue(b.submit_text('peer', text, marker='unique-marker-20261004')['confirmed'])
                else:
                    with self.assertRaises(b.DispatchUnconfirmed):
                        b.submit_text('peer', text, marker='unique-marker-20261004')
                self.assertEqual([c.args for c in key.call_args_list], [('peer', 'tab')])
                send.assert_called_once()

    def test_tab_never_operates_on_compaction_changed_or_additional_draft(self):
        idle, text, pending, _ = self.states('›')
        for change in ['Compacting context', 'Reconnecting', 'extra text', 'GPT-this is my draft']:
            if change in ['Compacting context', 'Reconnecting']:
                after = change + '\n' + pending + '\ntab to queue message'
            else:
                after = pending.replace('\nGPT-6 high', '\n' + change + '\nGPT-6 high') + '\ntab to queue message'
            after = '• Working (3s • esc to interrupt)\n' + after
            with self.subTest(change=change):
                self.run_case('›', [idle, after], False, 1)

    def test_queue_hint_must_be_exact_and_codex(self):
        for glyph in ['›', '❯']:
            idle, text, pending, _ = self.states(glyph)
            for hint in ['tab to queue', 'tab to edit message', 'tab to queue message extra']:
                self.assertFalse(b._codex_tab_queue_allowed(pending+'\n'+hint,text))
        self.assertFalse(b._codex_tab_queue_allowed('❯ '+text+'\n[claude-opus-5]\ntab to queue message',text))


if __name__ == '__main__':
    unittest.main()

class DraftOwnershipTests(unittest.TestCase):
    def test_model_or_path_looking_first_row_remains_occupied(self):
        for draft in ['GPT-this is my draft', 'claude/my-draft', '[Claude draft]']:
            screen='› '+draft+'\nGPT-6 high'
            with self.subTest(draft=draft), LegacyTerminalFixture(), patch.object(b, 'read_screen', return_value=screen), patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                with self.assertRaises(b.DispatchUnconfirmed): b.submit_text('peer','new',marker='new')
                send.assert_not_called(); key.assert_not_called()

    def test_first_enter_never_repeats_after_foreign_text_with_own_marker(self):
        for glyph in ['›','❯']:
            idle,prompt,pending,_=BidirectionalSubmissionTests().states(glyph)
            altered=pending.replace(prompt,prompt+' user added words')
            with self.subTest(glyph=glyph):
                BidirectionalSubmissionTests().run_case(glyph, [idle, altered], False, 1)


class ExactDraftWhitespaceTests(unittest.TestCase):
    def test_bordered_renderer_requires_complete_content(self):
        border = '─' * 16
        screen = border + '\n❯\u00a0STATUS: ok\n' + border + '\n[claude-opus-5]'
        self.assertTrue(b._exact_pending_text(screen, 'STATUS: ok'))
        self.assertFalse(b._exact_pending_text(screen, 'STATUS:ok'))
        self.assertFalse(b._exact_pending_text(screen, 'STATUS: ok extra'))
        cursor = screen.replace('STATUS: ok\n', 'STATUS: ok \n')
        self.assertTrue(b._exact_pending_text(cursor, 'STATUS: ok'))
        self.assertFalse(b._exact_pending_text(cursor.replace('ok \n', 'ok  \n'), 'STATUS: ok'))

    def test_known_empty_footer_gap_but_not_whitespace_content(self):
        for gap in ['\n', '\n\n']:
            self.assertTrue(b._exact_pending_text('› STATUS: ok\n' + gap + 'GPT-6 high', 'STATUS: ok'))
        self.assertFalse(b._exact_pending_text('› STATUS: ok\n  \nGPT-6 high', 'STATUS: ok'))

    def test_changed_whitespace_never_authorizes_an_extra_key(self):
        for glyph in ['›', '❯']:
            case = BidirectionalSubmissionTests()
            idle, prompt, pending, _ = case.states(glyph)
            for altered in [prompt.replace('STATUS: ', 'STATUS:'),
                            prompt.replace('STATUS: ', 'STATUS:  '),
                            prompt + ' ', prompt + '\n  ']:
                with self.subTest(glyph=glyph, altered=altered):
                    case.run_case(glyph, [idle, pending.replace(prompt, altered)], False, 1)

    def test_changed_whitespace_never_authorizes_tab(self):
        idle, prompt, pending, _ = BidirectionalSubmissionTests().states('›')
        changed = pending.replace('STATUS: ', 'STATUS:  ')
        busy = '• Working (3s • esc to interrupt)\n' + changed + '\ntab to queue message'
        BidirectionalSubmissionTests().run_case('›', [idle, busy], False, 1)

    def test_exact_multiline_gutter_preserves_payload_spaces(self):
        text = 'STATUS: first\n  second'
        screen = '› STATUS: first\n    second\nGPT-6 high'
        self.assertTrue(b._exact_pending_text(screen, text))
        self.assertFalse(b._exact_pending_text(screen, 'STATUS: first\nsecond'))

    def test_unknown_footer_and_folded_paste_are_not_owned(self):
        for screen in ['› STATUS: original\nunknown footer',
                       '› [Pasted text #1 +7 lines]\nGPT-6 high']:
            self.assertFalse(b._exact_pending_text(screen, 'STATUS: original'))


class StaleMarkerProgressTests(unittest.TestCase):
    def test_marker_without_complete_original_message_is_not_consumption(self):
        for glyph in ['›', '❯']:
            case = BidirectionalSubmissionTests()
            idle, prompt, pending, consumed = case.states(glyph)
            for replacement in ['unique-marker-20261004',
                                prompt + ' extra foreign content',
                                'changed STATUS unique-marker-20261004']:
                with self.subTest(glyph=glyph, replacement=replacement):
                    case.run_case(glyph, [idle, consumed.replace(prompt, replacement)], False, 1)

    def test_payload_in_different_prompt_block_does_not_prove_this_marker(self):
        for glyph in ['›', '❯']:
            case = BidirectionalSubmissionTests()
            idle, prompt, pending, consumed = case.states(glyph)
            after = glyph + ' ' + prompt + '\n' + consumed.replace(prompt, 'unrelated')
            case.run_case(glyph, [idle, after], False, 1)

    def test_activity_before_complete_payload_is_not_consumption(self):
        for glyph in ['›', '❯']:
            case = BidirectionalSubmissionTests()
            idle, prompt, pending, consumed = case.states(glyph)
            after = consumed.replace(prompt, 'unique-marker-20261004')
            after = after.replace(idle, 'full message quoted later: ' + prompt + '\n' + idle)
            case.run_case(glyph, [idle, after], False, 1)

    def test_old_marker_plus_unrelated_new_activity_is_not_consumption(self):
        for glyph in ['›', '❯']:
            case = BidirectionalSubmissionTests()
            idle, prompt, pending, consumed = case.states(glyph)
            after = consumed.replace(idle, '• Read unrelated next file\n' + idle)
            case.run_case(glyph, [consumed, after], False, 0, sends=0)
