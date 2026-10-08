import copy
import unittest
from tools.standby_ui_pty import bound_focus_prefix


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.wid = '19CB5289-85C3-43DE-89AC-BEB7BB7C3F17'
        self.sid = '96648E29-BF82-44E6-B468-1CF3BA4F3948'
        self.tree = dict(id=self.wid, ref='workspace:213', title='CCC standby fifty130 abc123456',
                         panes=[dict(ref='pane:242', surfaces=[dict(
                             id=self.sid, ref='surface:5348', type='terminal')])])

    def test_exact_focus(self):
        prefix = bound_focus_prefix(self.tree, self.wid, self.sid)
        self.assertEqual(prefix, b'ws213/p242/s5348  |  CCC standby fifty130 abc123456  |  ')
        self.assertNotIn(prefix, prefix.replace(b's5348 ', b's53480 '))
        self.assertNotIn(prefix, prefix.replace(b'p242', b'p243'))
        self.assertNotIn(prefix, prefix.replace(b'abc123456', b'abc123457'))

    def test_foreign_and_ambiguous(self):
        with self.assertRaises(ValueError):
            bound_focus_prefix(self.tree, self.sid, self.sid)
        with self.assertRaises(ValueError):
            bound_focus_prefix(self.tree, self.wid, self.wid)
        self.tree['panes'].append(copy.deepcopy(self.tree['panes'][0]))
        with self.assertRaises(ValueError):
            bound_focus_prefix(self.tree, self.wid, self.sid)

    def test_invalid_reference_and_truncated_title(self):
        self.tree['ref'] = 'workspace:213junk'
        with self.assertRaises(ValueError):
            bound_focus_prefix(self.tree, self.wid, self.sid)
        self.tree['ref'] = 'workspace:213'
        self.tree['title'] += ' too long'
        with self.assertRaises(ValueError):
            bound_focus_prefix(self.tree, self.wid, self.sid)
