import json
from pathlib import Path
import unittest
import cmux_bridge as bridge


class Claude303Reference180Tests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parents[1] / 'tests/fixtures/claude303-reference180.json'
        data = json.loads(fixture.read_text())
        self.screen, self.wire = data['screen'], data['wire']

    def test_long_maintenance_marker_and_three_digit_body(self):
        self.assertTrue(bridge._exact_pending_text(self.screen, self.wire))
        self.assertIn(' ' * 13 + '\n', self.screen)

    def test_short_nonce_four_digit_body_keeps_original_geometry(self):
        old_marker = 'S1000020040_B_RELEASE0452_RENEWAL_V179'
        screen = self.screen.replace(old_marker, 'abc12345').replace('BYTES=842', 'BYTES=1842')
        screen = screen.replace(' ' * 13 + '\n', ' ' * 12 + '\n')
        wire = self.wire.replace(old_marker, 'abc12345').replace('BYTES=842', 'BYTES=1842')
        self.assertTrue(bridge._exact_pending_text(screen, wire))

    def test_padding_is_bound_to_the_complete_observed_third_row(self):
        for count in (1, 11, 12, 14, 16, 17, 32):
            with self.subTest(count=count):
                altered = self.screen.replace(' ' * 13 + '\n', ' ' * count + '\n')
                self.assertFalse(bridge._exact_pending_text(altered, self.wire))

    def test_canonical_unpadded_rendering_remains_valid(self):
        canonical = self.screen.replace(' ' * 13 + '\n', '\n')
        self.assertTrue(bridge._exact_pending_text(canonical, self.wire))

    def test_missing_or_changed_payload_and_contaminated_rows_reject(self):
        changes = [
            ('/Users/test/', '/Users/evil/'),
            ('SHA256=' + 'a' * 64, 'SHA256=' + 'a' * 63 + 'b'),
            ('BYTES=842', 'BYTES=843'),
            ('RENEWAL_V179', 'RENEWAL_V178'),
            ('follow its reply contract', 'follow a reply contract'),
            ('BODY=', 'BODY= '),
            ('authority.\n', 'authority. \n'),
            ('are separate. ', 'are separate. injected'),
        ]
        for old, new in changes:
            with self.subTest(change=old):
                self.assertNotEqual(self.screen.replace(old, new), self.screen)
                self.assertFalse(bridge._exact_pending_text(self.screen.replace(old, new), self.wire))

    def test_unknown_border_and_incomplete_frame_reject(self):
        for width in (178, 179, 181, 182):
            with self.subTest(width=width):
                self.assertFalse(bridge._exact_pending_text(self.screen.replace('─' * 180, '─' * width), self.wire))
        self.assertFalse(bridge._exact_pending_text(self.screen.replace('─' * 180, '', 1), self.wire))

    def test_only_optional_single_final_cursor_cell_is_accepted(self):
        suffix = 'are separate.'
        self.assertTrue(bridge._exact_pending_text(self.screen.replace(suffix + ' ', suffix), self.wire))
        self.assertFalse(bridge._exact_pending_text(self.screen.replace(suffix + ' ', suffix + '  '), self.wire))

    def test_stability_keeps_original_renderer_cells(self):
        stable = bridge._draft_structure(self.screen, self.wire)
        without_cursor = self.screen.replace('are separate. ', 'are separate.')
        self.assertTrue(bridge._exact_pending_text(without_cursor, self.wire))
        self.assertNotEqual(stable, bridge._draft_structure(without_cursor, self.wire))


if __name__ == '__main__':
    unittest.main()
