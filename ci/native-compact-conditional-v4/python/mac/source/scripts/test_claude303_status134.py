import json
from pathlib import Path
import unittest
import cmux_bridge as bridge


class Status134Tests(unittest.TestCase):
    def setUp(self):
        x=json.loads((Path(__file__).parents[1]/'tests/fixtures/claude303-status134.json').read_text())
        self.screen,self.wire=x['screen'],x['wire']

    def test_observed_status_and_canonical_unpadded_both_match(self):
        self.assertTrue(bridge._exact_pending_text(self.screen,self.wire))
        self.assertTrue(bridge._exact_pending_text(self.screen.replace(' '*18+'\n','\n'),self.wire))

    def test_other_padding_counts_reject(self):
        for n in (1,12,17,19,20,32):
            with self.subTest(n=n):
                self.assertFalse(bridge._exact_pending_text(self.screen.replace(' '*18+'\n',' '*n+'\n'),self.wire))

    def test_complete_contents_are_still_required(self):
        for old,new in [('7063B683','7063B682'),('/Users/test/','/Users/evil/'),('SHA256='+'a'*64,'SHA256='+'a'*63+'b'),('Read and verify','Read or verify'),('pending input','pending inputs')]:
            with self.subTest(old=old):
                self.assertFalse(bridge._exact_pending_text(self.screen.replace(old,new),self.wire))

    def test_unknown_width_and_missing_frame_reject(self):
        for n in (0,132,133,135,136):
            with self.subTest(n=n):
                self.assertFalse(bridge._exact_pending_text(self.screen.replace('─'*134,'─'*n),self.wire))

    def test_padding_in_other_rows_is_not_removed(self):
        for old in ('ccc-successor-cl\n','instructions and\n'):
            self.assertIn(old,self.screen)
            self.assertFalse(bridge._exact_pending_text(self.screen.replace(old,old[:-1]+' '*18+'\n'),self.wire))

    def test_one_final_cursor_cell_and_raw_stability(self):
        other=self.screen.replace('renewal. \n','renewal.\n')
        self.assertTrue(bridge._exact_pending_text(other,self.wire))
        self.assertNotEqual(bridge._draft_structure(self.screen,self.wire),bridge._draft_structure(other,self.wire))
        self.assertFalse(bridge._exact_pending_text(self.screen.replace('renewal. \n','renewal.  \n'),self.wire))


if __name__=='__main__':unittest.main()
