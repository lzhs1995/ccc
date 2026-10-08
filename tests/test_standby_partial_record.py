import json
import unittest
import ccc_standby_identity as identity
import ccc_workspace_batch as batch
from test_standby_identity import StandbyIdentityTests


class PartialRecordTests(StandbyIdentityTests):
    def pending(self, tail):
        with self.tui.open('ab') as f:
            f.write(tail)
        with self.assertRaises(identity.TuiObservationPending) as raised:
            self.inspect()
        return raised.exception

    def test_benign_partial_stays_pending_then_completes(self):
        pending = self.pending(b'{"dir":"to_tui","variant":"SkillsList')
        pending.recheck()
        with self.tui.open('ab') as f:
            f.write(b'Loaded"}\n')
        pending.recheck()
        self.assertTrue(self.inspect()['startup_observed'])

    def test_partial_user_turn_completed_refuses(self):
        pending = self.pending(b'{"dir":"from_tui","kind":"op","payload":{"UserTurn":')
        with self.tui.open('ab') as f:
            f.write(json.dumps({'items': [{'type': 'text', 'text': batch.PROMPT}]}).encode() + b'}}\n')
        with self.assertRaises(ValueError):
            pending.recheck()
        with self.assertRaises(ValueError):
            self.inspect()

    def test_partial_prefix_rewrite_refuses(self):
        pending = self.pending(b'{"dir":"to_tui","variant":"SkillsList')
        raw = self.tui.read_bytes()
        self.tui.write_bytes(raw.replace(b'SkillsList', b'OtherEvent'))
        with self.assertRaises(ValueError):
            pending.recheck()

    def test_partial_inode_replacement_refuses(self):
        pending = self.pending(b'{"dir":"to_tui"')
        raw = self.tui.read_bytes()
        self.tui.rename(self.tui.with_suffix('.old'))
        self.tui.write_bytes(raw)
        with self.assertRaises(ValueError):
            pending.recheck()

    def test_legacy_parser_preserves_complete_lines(self):
        before = self.tui.read_bytes()
        self.pending(b'{"dir":"to_tui"')
        self.assertEqual(batch._initial_event_prefix(self.claim), (before, False))
        raw, sent = batch._initial_event_prefix(self.claim, retain_partial=True)
        self.assertTrue(raw.endswith(b'{"dir":"to_tui"'))
        self.assertFalse(sent)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(PartialRecordTests(n) for n in PartialRecordTests.__dict__ if n.startswith('test_'))
