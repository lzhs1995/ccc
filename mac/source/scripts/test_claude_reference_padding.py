"""A/B measured BODY-row padding, with negative payload/geometry controls."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_prompt_reference as reference


FIXTURES = json.loads((Path(__file__).resolve().parents[1] / 'verification/fixtures' /
                       'claude-reference-body-padding-20261013.json').read_text())


def material(sample, padding=16):
    rows = list(sample['rows'])
    token = rows[2][2:-16]
    path = json.loads(token.removeprefix('BODY='))
    ref = dict(format=reference.SINGLE_LINE_FORMAT, path=path,
               sha256=Path(path).stem, bytes=sample['body_bytes'])
    text = reference._wire(sample['marker'], ref)
    rows[2] = '  ' + token + ' ' * padding
    return '\n'.join(rows) + '\n', text


class ClaudeReferencePaddingTests(unittest.TestCase):
    def test_both_complete_frames_accept_measured_padding_and_final_cursor_cell(self):
        for sample in FIXTURES:
            for padding in (16, 17):
                value, text = material(sample, padding)
                with self.subTest(marker=sample['marker'], padding=padding):
                    self.assertTrue(bridge._exact_pending_text(value, text))
                    border = '\n' + '─' * 183
                    cursor = value.replace(border, ' ' + border, 1)
                    self.assertTrue(bridge._exact_pending_text(cursor, text))

    def test_unmeasured_padding_and_geometry_remain_unverified(self):
        for sample in FIXTURES:
            for padding in (1, 2, 15, 18, 32):
                with self.subTest(marker=sample['marker'], padding=padding):
                    value, text = material(sample, padding)
                    self.assertFalse(bridge._exact_pending_text(value, text))
            value, text = material(sample)
            for border in (182, 184, 250):
                self.assertFalse(bridge._exact_pending_text(
                    value.replace('─' * 183, '─' * border), text))

    def test_changed_payload_whitespace_and_unknown_footer_are_not_hidden(self):
        for sample in FIXTURES:
            value, text = material(sample)
            changes = [value.replace('BODY=', 'BODY= ', 1),
                       value.replace(' ' * 16 + '\n', ' ' * 15 + '\u00a0\n', 1),
                       value.replace('.txt"', '.txtX"', 1),
                       value.replace('SHA256=', 'SHA256=x', 1),
                       value.replace('  SHA256=', '   SHA256=', 1),
                       value.replace('BODY=', 'EXTRA BODY=', 1),
                       value.replace('  SHA256=', '  \n  SHA256=', 1),
                       value.replace('bypass permissions on', 'unknown permissions', 1)]
            for changed in changes:
                with self.subTest(marker=sample['marker'], changed=changed):
                    self.assertFalse(bridge._exact_pending_text(changed, text))
            for changed in (text.replace(' BODY=', '  BODY=', 1),
                            text.replace('STATUS: ', 'NOTE: ', 1), text + ' '):
                self.assertFalse(bridge._exact_pending_text(value, changed))

    def test_partial_or_extra_content_never_becomes_a_complete_composer(self):
        for sample in FIXTURES:
            value, text = material(sample)
            border = '\n' + '─' * 183
            for changed in (value.replace(border, '\n  extra' + border, 1),
                            value.replace('body reading and acceptance are separate.', 'body ...', 1),
                            value.replace(border, '\n' + '─' * 182, 1)):
                self.assertFalse(bridge._exact_pending_text(changed, text))

    def test_two_read_stability_retains_raw_padding_and_is_zero_io(self):
        for sample in FIXTURES:
            value, text = material(sample, 16)
            changed, _ = material(sample, 17)
            with patch.object(reference, 'read_body') as read, \
                    patch.object(bridge, 'send_text') as paste, \
                    patch.object(bridge, 'send_key') as key:
                first = bridge._draft_structure(value, text)
                self.assertEqual(first, bridge._draft_structure(value, text))
                self.assertNotEqual(first, bridge._draft_structure(changed, text))
            read.assert_not_called()
            paste.assert_not_called()
            key.assert_not_called()


if __name__ == '__main__':
    unittest.main()
