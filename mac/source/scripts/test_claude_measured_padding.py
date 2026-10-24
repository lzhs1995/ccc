"""Sanitized measured-renderer frames and actual draft/submission gates."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_prompt_reference as reference
from native_test_support import NativeFixture


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / 'verification/fixtures' /
                      'claude-formal-and-goal-padding-20261014.json').read_text())
CASES = FIXTURE['cases']


def composer_indices(screen):
    rows = screen.splitlines()
    start = next(i for i, row in enumerate(rows) if row.startswith('❯'))
    end = rows.index(rows[start - 1], start + 1)
    return rows, start, end


class MeasuredPaddingTests(unittest.TestCase):
    def test_all_pinned_frames_pass_actual_stable_draft_gate(self):
        self.assertEqual([c['case'] for c in CASES],
                         ['B0016', 'B0015', 'GOAL_1D_EXAMPLE', 'B0018', 'B0019', 'B0021', 'B0022'])
        self.assertEqual([len(c['frames']) for c in CASES], [20] * len(CASES))
        with patch.object(bridge.time, 'sleep'), patch.object(bridge, 'send_text') as paste, \
                patch.object(bridge, 'send_key') as key, patch.object(reference, 'read_body') as read:
            for case in CASES:
                text = case['notice']
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), case['notice_sha256'])
                for frame in case['frames']:
                    value = frame['screen']
                    with self.subTest(case=case['case'], event=frame['event_index']):
                        self.assertEqual(hashlib.sha256(value.encode()).hexdigest(), frame['screen_sha256'])
                        with patch.object(bridge, 'read_screen', return_value=value) as screen:
                            self.assertEqual(bridge._stable_owned_draft('offline', text, 200, None), value)
                            self.assertEqual(screen.call_count, 2)
                        if case['case'].startswith('GOAL'):
                            self.assertTrue(bridge._queued_or_active_input(value))
            paste.assert_not_called()
            key.assert_not_called()
            read.assert_not_called()

    def test_every_frame_rejects_changed_payload_padding_and_footer(self):
        for case in CASES:
            for frame in case['frames']:
                value, text = frame['screen'], case['notice']
                rows, start, end = composer_indices(value)
                padded = next(i for i in range(start, end) if rows[i].endswith(' ' * 10))
                mutations = []
                for index, changed in (
                    (padded, rows[padded] + 'X'),
                    (padded, rows[padded][:-1] + '\u00a0'),
                    (start + 1, rows[start + 1].replace('=', '= ', 1)),
                    (end - 1, rows[end - 1][:-1]),
                ):
                    new = list(rows)
                    new[index] = changed
                    mutations.append('\n'.join(new) + '\n')
                mutations += [value.replace('SHA256=', 'SHA256=0', 1),
                              value.replace('bypass permissions on', 'unknown permissions', 1),
                              value.replace(rows[end], rows[end][:-1]),
                              '\n'.join(rows[:end]) + '\n',
                              value.replace(rows[end], ' ' + rows[end])]
                for number, changed in enumerate(mutations):
                    with self.subTest(case=case['case'], frame=frame['event_index'], mutation=number):
                        self.assertFalse(bridge._exact_pending_text(changed, text))
                for expected in (text + ' ', text.replace(' SHA256=', '  SHA256=', 1)):
                    self.assertFalse(bridge._exact_pending_text(value, expected))

    def test_unmeasured_geometry_and_padding_stay_unverified(self):
        for case in CASES:
            value, text = case['frames'][0]['screen'], case['notice']
            rows, start, end = composer_indices(value)
            width = len(rows[start - 1])
            for size in (width - 1, width + 1, width + 12):
                self.assertFalse(bridge._exact_pending_text(value.replace('─' * width, '─' * size), text))
            padded = next(i for i in range(start, end) if rows[i].endswith(' ' * 10))
            clean = rows[padded].rstrip(' ')
            count = len(rows[padded]) - len(clean)
            invalid = (15, 18) if width == 183 else (count - 1, count + 1)
            for pad in invalid:
                changed = list(rows)
                changed[padded] = clean + ' ' * pad
                self.assertFalse(bridge._exact_pending_text('\n'.join(changed) + '\n', text))

    def test_raw_padding_and_full_original_structure_remain_required_for_recovery(self):
        case = next(c for c in CASES if c['case'].startswith('GOAL'))
        value, text = case['frames'][0]['screen'], case['notice']
        changed = value.replace(' ' * 17 + '\n', ' ' * 16 + '\n', 1)
        self.assertNotEqual(value, changed)
        self.assertTrue(bridge._exact_pending_text(changed, text))
        original = bridge._draft_structure(value, text)
        self.assertNotEqual(original, bridge._draft_structure(changed, text))
        with patch.object(bridge.time, 'sleep'), patch.object(bridge, 'send_key') as key, \
                patch.object(bridge, 'read_screen', return_value=changed):
            with self.assertRaisesRegex(bridge.DispatchUnconfirmed, 'COMPOSE_STRUCTURE_CHANGED'):
                bridge._stable_owned_draft('offline', text, 200, None, expected_structure=original)
            key.assert_not_called()

    def test_sha_padding_is_preserved_in_two_read_stability_checks(self):
        for case in (c for c in CASES if c['case'] in ('B0021', 'B0022')):
            value, text = case['frames'][0]['screen'], case['notice']
            rows, start, end = composer_indices(value)
            original = bridge._draft_structure(value, text)
            self.assertEqual(original[1][3], rows[start + 3])
            self.assertTrue(original[1][3].endswith(' ' * 11))
            rows[start + 3] = rows[start + 3][:-11]
            changed = '\n'.join(rows) + '\n'
            self.assertTrue(bridge._exact_pending_text(changed, text))
            self.assertNotEqual(original, bridge._draft_structure(changed, text))
            # An ordinary submission keeps observing until two consecutive raw
            # structures agree. Alternating padding must exhaust that budget.
            with self.subTest(case=case['case'], mode='unstable'), \
                    NativeFixture(provider='claude') as native, patch.object(
                        bridge, 'read_screen', side_effect=[value, changed] * 10) as screen:
                with self.assertRaisesRegex(bridge.DispatchUnconfirmed, 'DRAFT_NOT_READY'):
                    bridge._stable_owned_draft('offline', text, 200, None)
                self.assertEqual(screen.call_count, 20)
                native.send.assert_not_called()
                native.key.assert_not_called()
            # Recovery additionally pins the original structure; any change
            # revokes its authority immediately, even with an equal payload.
            with self.subTest(case=case['case'], mode='recovery'), \
                    NativeFixture(provider='claude') as native, patch.object(
                        bridge, 'read_screen', side_effect=[value, changed]) as screen:
                with self.assertRaisesRegex(bridge.DispatchUnconfirmed, 'COMPOSE_STRUCTURE_CHANGED'):
                    bridge._stable_owned_draft('offline', text, 200, None,
                                               expected_structure=original)
                self.assertEqual(screen.call_count, 2)
                native.send.assert_not_called()
                native.key.assert_not_called()

    def test_unknown_or_truncated_frame_never_authorizes_a_key(self):
        for case in CASES:
            frame, text = case['frames'][0]['screen'], case['notice']
            bad = frame.replace('SHA256=', 'SHA256=x', 1)
            with NativeFixture(provider='claude') as native, \
                    patch.object(bridge, 'read_screen', return_value=bad):
                with self.assertRaisesRegex(bridge.DispatchUnconfirmed, 'DRAFT_NOT_READY'):
                    bridge._stable_owned_draft('offline', text, 200, None)
                native.send.assert_not_called()
                native.key.assert_not_called()

    def test_formal_dispatch_uses_padding_gate_and_exact_native_receipt(self):
        for case in (c for c in CASES if c['case'].startswith('B00')):
            # Portable task files with the measured display-cell geometry. Only
            # process/UI I/O is replaced; pack contract, journals and proof run.
            with self.subTest(case=case['case']), NativeFixture(provider='claude') as native:
                source, start, end = composer_indices(case['frames'][0]['screen'])
                token_cells = {'B0016': 158, 'B0015': 166, 'B0018': 159, 'B0019': 161,
                               'B0021': 156, 'B0022': 160}[case['case']]
                base = str(native.home / '文' / 'task-pack.json')
                fill = token_cells - bridge._display_cells('PACK=' + json.dumps(base, ensure_ascii=False))
                self.assertGreater(fill, 0)
                sha_row_padding = len(source[start - 1]) == 105
                if sha_row_padding:
                    # The measured hard wrap leaves 100 cells before a wide
                    # character. ASCII-only redaction changes that boundary.
                    prefix = 'PACK=' + json.dumps(str(native.home / '文'), ensure_ascii=False)[:-1]
                    before_wrap = 100 - bridge._display_cells(prefix)
                    self.assertGreaterEqual(before_wrap, 0)
                    self.assertGreaterEqual(fill - before_wrap, 2)
                    filler = 'a' * before_wrap + '文' + 'a' * (fill - before_wrap - 2)
                else:
                    filler = 'a' * fill
                path = native.home / ('文' + filler) / 'task-pack.json'
                path.parent.mkdir()
                task = json.loads(case['notice'].split(' PACK=', 1)[0].removeprefix('TASK_PACK_V2 TASK='))
                report = native.home / 'report.md'
                report.write_text('Frozen independent output.\n')
                pack = dict(task_id=task, completion_nonce='test-nonce-12345', draft=False,
                            executor_uuid='target', report=str(report),
                            required_skill=str(bridge.COLLABORATION_SKILL_PATH), callback_target='surface:1',
                            completion_receipt=str(path.parent / 'completion.json'),
                            completion_delivery=dict(transport='cmux_bridge.submit_completion_callback', require_confirmed=True),
                            completion_callback='DONE|' + task + '|test-nonce-12345|REPORT=' + str(report))
                path.write_text(json.dumps(pack, ensure_ascii=False) + '\n')
                text = bridge.task_pack_notice(path)
                token = 'PACK=' + json.dumps(str(path), ensure_ascii=False)
                split_cells = 100 if sha_row_padding else 114
                split = next(i for i in range(len(token)) if bridge._display_cells(token[:i]) == split_cells)
                self.assertEqual(bridge._display_cells(token), token_cells)
                tail = 'SHA256=' + text.split(' SHA256=', 1)[1]
                if sha_row_padding:
                    self.assertEqual(bridge._display_cells(token[split]), 2)
                    sha, scope = tail.split(' ', 1)
                    before_call, call = scope.rsplit(' ', 1)
                    draft_rows = ['❯\u00a0TASK_PACK_V2 TASK=' + json.dumps(task),
                                  '  ' + token[:split], '  ' + token[split:],
                                  '  ' + sha + ' ' * 11, '  ' + before_call, '  ' + call]
                else:
                    end_cell = 72 if case['case'] == 'B0018' else 96
                    pad = end_cell - bridge._display_cells(token[split:])
                    cut = tail.index(' pack SHA;')
                    draft_rows = ['❯\u00a0TASK_PACK_V2 TASK=' + json.dumps(task), '  ' + token[:split],
                                  '  ' + token[split:] + ' ' * pad, '  ' + tail[:cut], '  ' + tail[cut + 1:]]
                draft = '\n'.join(source[:start] + draft_rows + source[end:]) + '\n'
                empty = '\n'.join(source[:start] + ['❯\u00a0'] + source[end:]) + '\n'
                native.key.side_effect = native.receipt_on_key(text)
                with patch('availability_contract.require_action'), patch.object(
                        bridge, 'read_screen', side_effect=native.ready_screens(empty, draft)):
                    result = bridge.submit_task_pack('offline', text, str(path))
                    self.assertTrue(result['confirmed'])
                    self.assertEqual(result['confirmation_source'], 'native_user_message_v1')
                    with self.assertRaises(bridge.TaskPackContractError):
                        bridge.submit_task_pack('offline', text, str(path))
                native.send.assert_called_once_with('offline', text)
                native.key.assert_called_once_with('offline', 'enter')


if __name__ == '__main__':
    unittest.main()
