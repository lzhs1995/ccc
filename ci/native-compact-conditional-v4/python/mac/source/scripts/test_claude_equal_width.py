"""Regression for Claude's measured equality word-wrap boundary.

Only the renderer geometry is synthetic; this reproduces 128 + 1 + 7 == 136
from the actual 2026-10-09 composer. Native receipt equality is tested separately.
"""
import unittest

import cmux_bridge as b


def screen(first, second, *, border=140, cursor=True):
    return '\n'.join([
        '⏺ Completed the previous task.',
        '✻ Sautéed for 27m 9s',
        '─' * border,
        '❯\u00a0' + first,
        '  ' + second + (' ' if cursor else ''),
        '─' * border,
        '  [claude-opus-5[1M]] │ ~/project git:(main) │ ⏱️  4h 1m',
        '  上下文 ██░░░░░░░░ 18%',
        '  1 CLAUDE.md | 9 MCPs | 7 钩子',
        '  ⏵⏵ bypass permissions on (shift+tab to cycle)',
    ])


class EqualWidthTests(unittest.TestCase):
    first = 'MESSAGE ' + 'x' * 120
    second = 'reading and acceptance are separate.'

    def test_exact_width_word_boundary_accepts_full_original(self):
        self.assertEqual(len(self.first) + 1 + len('reading'), 136)
        for cursor in (True, False):
            with self.subTest(cursor=cursor):
                self.assertTrue(b._exact_pending_text(
                    screen(self.first, self.second, cursor=cursor),
                    self.first + ' ' + self.second))

    def test_missing_extra_and_changed_spaces_are_rejected(self):
        original = self.first + ' ' + self.second
        for value in (original[1:], original[:-1], original + 'x',
                      original[:80], original.replace(' reading', '  reading'),
                      original.replace(' reading', 'reading'),
                      original.replace('and acceptance', 'and  acceptance')):
            with self.subTest(value=value):
                self.assertFalse(b._exact_pending_text(screen(self.first, self.second), value))

    def test_short_boundary_does_not_invent_separator(self):
        first = self.first[:-1]
        self.assertFalse(b._exact_pending_text(
            screen(first, self.second), first + ' ' + self.second))

    def test_display_equivalent_newline_is_not_native_byte_proof(self):
        # Both render the same. The screen matcher never claims native receipt;
        # the original single-line paste and exact native user record govern it.
        self.assertTrue(b._exact_pending_text(
            screen(self.first, self.second), self.first + '\n' + self.second))

    def test_unknown_footer_keeps_draft_protected(self):
        value = screen(self.first, self.second).replace(
            '  1 CLAUDE.md | 9 MCPs | 7 钩子', '  unknown layout')
        self.assertFalse(b._exact_pending_text(value, self.first + ' ' + self.second))

    def test_extra_blank_or_content_line_keeps_draft_protected(self):
        original = self.first + ' ' + self.second
        for line in ('', '  additional input'):
            value = screen(self.first, self.second).replace(
                '\n' + '─' * 140 + '\n  [', '\n' + line + '\n' + '─' * 140 + '\n  [')
            with self.subTest(line=line):
                self.assertFalse(b._exact_pending_text(value, original))


if __name__ == '__main__':
    unittest.main()
