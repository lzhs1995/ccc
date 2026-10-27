"""Independent boundary cases for an undelivered task's Stop exemption."""
import fcntl
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cmux_consensus_stop_guard as guard
import test_stop_dispatch_lifecycle as fixture


class StopDispatchProofTests(unittest.TestCase):
    setUp = fixture.StopDispatchLifecycleTests.setUp
    write = fixture.StopDispatchLifecycleTests.write
    sha = fixture.StopDispatchLifecycleTests.sha
    dispatch_attempt = fixture.StopDispatchLifecycleTests.dispatch_attempt
    stop = fixture.StopDispatchLifecycleTests.stop

    def test_report_without_callback_attempt_still_owes_callback(self):
        self.dispatch_attempt('NO_INPUT')
        self.report.write_text('completed work')
        result = self.stop()
        self.assertEqual(result.returncode, 2)
        self.assertIn('REPORT_WITHOUT_CALLBACK', result.stderr)

    def test_caller_and_workspace_must_both_match(self):
        for field in ('workspace_uuid', 'caller_surface_uuid', 'target_pane_uuid'):
            self.dispatch_attempt('NO_INPUT')
            p = self.dispatch / 'attempt-0001.json'
            value = json.loads(p.read_text())
            value['binding']['identity'][field] = '' if field == 'target_pane_uuid' else 'WRONG'
            self.write(p, value)
            with self.subTest(field=field):
                self.assertEqual(self.stop().returncode, 2)

    def test_original_sender_lock_replaced_before_observation_blocks(self):
        self.dispatch_attempt('NO_INPUT')
        p = self.dispatch / 'delivery.lock'
        with p.open('a+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p.rename(self.dispatch / 'old-lock')
            p.touch()
            self.assertEqual(self.stop().returncode, 2)

    def test_legacy_attempt_without_lock_pin_is_unknown(self):
        self.dispatch_attempt('NO_INPUT')
        p = self.dispatch / 'attempt-0001.json'
        value = json.loads(p.read_text())
        del value['delivery_lock_identity']
        self.write(p, value)
        self.assertEqual(self.stop().returncode, 2)
        self.assertIn('UNKNOWN', self.stop().stderr)

    def test_symlink_attempt_is_not_original_evidence(self):
        self.dispatch_attempt('NO_INPUT')
        p = self.dispatch / 'attempt-0001.json'
        target = self.root / 'copy.json'
        p.rename(target)
        p.symlink_to(target)
        self.assertEqual(self.stop().returncode, 2)

    def test_missing_events_is_unknown_submission(self):
        self.dispatch_attempt('NO_INPUT')
        p = self.dispatch / 'attempt-0001.json'
        value = json.loads(p.read_text())
        del value['events']
        self.write(p, value)
        self.assertEqual(self.stop().returncode, 2)

    def test_pack_executor_must_match_even_if_journal_matches_marker(self):
        self.pack['executor_uuid'] = 'OTHER'
        self.write(self.pack_path, self.pack)
        self.dispatch_attempt('NO_INPUT')
        self.assertEqual(self.stop().returncode, 2)

    def test_dangling_receipt_symlink_does_not_count_as_absence(self):
        self.dispatch_attempt('NO_INPUT')
        (self.dispatch / 'receipt.json').symlink_to(self.root / 'absent')
        self.assertEqual(self.stop().returncode, 2)

    def test_attempt_content_changed_during_read_is_unknown(self):
        self.dispatch_attempt('NO_INPUT')
        original = guard._lifecycle_snapshot
        count = 0
        def changed(p):
            nonlocal count
            value = original(p)
            count += 1
            if count == 1:
                obj = json.loads(value[0])
                obj['phase'] = 'PASTED'
                self.write(p, obj)
            return value
        with patch.object(Path, 'home', return_value=self.home), \
                patch.object(guard, '_lifecycle_snapshot', side_effect=changed):
            state = guard._task_dispatch_state(self.marker, self.pack_path.read_bytes(), self.surface)
        self.assertEqual(state, 'UNKNOWN')

    def test_original_lock_changed_during_read_is_unknown(self):
        self.dispatch_attempt('NO_INPUT')
        original = guard._lifecycle_snapshot
        count = 0
        def changed(p):
            nonlocal count
            value = original(p)
            count += 1
            if count == 1:
                lock = self.dispatch / 'delivery.lock'
                lock.rename(self.dispatch / 'old-lock')
                lock.touch()
            return value
        with patch.object(Path, 'home', return_value=self.home), \
                patch.object(guard, '_lifecycle_snapshot', side_effect=changed):
            state = guard._task_dispatch_state(self.marker, self.pack_path.read_bytes(), self.surface)
        self.assertEqual(state, 'UNKNOWN')

