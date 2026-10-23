"""CURRENT discovers paused legacy replies without taking the original consumer."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_protocol as protocol
import executor_reask_scheduler as scheduler
import test_executor_reask_lifecycle as lifecycle_fixtures
import test_executor_reask_protocol as protocol_fixtures
from test_executor_reask_protocol import digest, identity, issuer
from test_executor_reask_scheduler import CALLER


class LegacyPausedReplyTests(unittest.TestCase):
    setUp = lifecycle_fixtures.PausedReplyTests.setUp
    begin = lifecycle_fixtures.PausedReplyTests.begin
    live = lifecycle_fixtures.PausedReplyTests.live
    task_marker = lifecycle_fixtures.PausedReplyTests.task_marker
    paused_wait = lifecycle_fixtures.PausedReplyTests.paused_wait
    make_release = protocol_fixtures.ProtocolTests.make_release
    write_pin = protocol_fixtures.ProtocolTests.write_pin

    def legacy_paused(self, *, continuing=True, modern=False):
        state, body, mailbox = self.paused_wait(continuing=continuing)
        name = '0.4.32-c7338a400bf4' if modern else '0.4.31-79ae83e9d5da'
        self.base = self.home / '.local/share/multi-agent-collaboration/releases'
        source, pins, raw = self.make_release(name)
        self.stack.enter_context(patch.dict(protocol.LEGACY, {
            name: {key: pins[key]['sha256'] for key in (*protocol.LEGACY_FILES, protocol.SCHEDULER)}}))
        self.stack.enter_context(patch.dict(protocol.LEGACY_MANIFESTS, {name: digest(raw)}))
        original = identity(source, pins)
        state['version'] = 3 if modern else 2
        if modern:
            state['controller'] = copy.deepcopy(original)
        else:
            for key in ('controller', 'file_protocol', 'registration'):
                state.pop(key, None)
        if continuing:
            state['continuation']['controller'] = copy.deepcopy(original)
        path = scheduler.enrollment_path(CALLER)
        state['file_schedule'] = dict(enrollment=str(path), registered_epoch=1000.)
        record = dict(schema=scheduler.SCHEMA if modern else scheduler.LEGACY_SCHEMA, status='ENROLLED',
                      authorization='EXPLICIT_REASK_START', uid=os.getuid(),
                      binding=scheduler.binding(state), live_binding=self.live(),
                      controller=original, scheduler=issuer(source, pins), enrolled_epoch=1000.)
        if modern:
            record.update(registration=copy.deepcopy(state['registration']),
                          file_protocol=protocol.descriptor())
        reask.save(state)
        ready.write_json(path, record)
        self.state_bytes = reask.state_path(CALLER).read_bytes()
        self.enrollment_bytes = path.read_bytes()
        self.assertTrue(scheduler.matches(record, state, path))
        return state, record, body, mailbox

    def assert_original_preserved(self, before, record, after):
        for key in ('version', 'episode_id', 'task_id', 'supervisor', 'workspace_uuid',
                    'asks', 'ask_count', 'attempt_count', 'terminal_pending', 'paused_tasks',
                    'transcript', 'tail_position', 'continuation', 'file_schedule'):
            self.assertEqual(after[key], before[key], key)
        for key in ('controller', 'registration', 'file_protocol'):
            if key in before:
                self.assertEqual(after[key], before[key], key)
            else:
                self.assertNotIn(key, after)
        current = ready.read_json(scheduler.enrollment_path(CALLER))
        for key in ('schema', 'authorization', 'binding', 'live_binding', 'controller',
                    'scheduler', 'enrolled_epoch'):
            self.assertEqual(current[key], record[key], key)
        for key in ('registration', 'file_protocol'):
            if key in record:
                self.assertEqual(current[key], record[key], key)
        snapshot = (reask.root() / 'legacy-snapshots' / ready.digest(CALLER)
                    / ready.digest(before['episode_id']))
        self.assertEqual((snapshot / 'state.json').read_bytes(), self.state_bytes)
        self.assertEqual((snapshot / 'enrollment.json').read_bytes(), self.enrollment_bytes)
        self.transport.assert_not_called()

    def check_paused_discovery(self, *, modern=False):
        original, record, body, _mailbox = self.legacy_paused(modern=modern)
        with patch('importlib.util.spec_from_file_location', side_effect=AssertionError('old import')), \
                patch('subprocess.run', side_effect=AssertionError('old process')):
            result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(result['episodes'][0]['status'], reask.ANSWERED)
        answered = reask.load(CALLER)
        self.assertEqual(answered['reply']['body'], body)
        self.assertEqual(answered['reply_detected_epoch'], 1002.)
        self.assertIsNone(answered['reply_consumed_epoch'])
        self.assert_original_preserved(original, record, answered)
        before_consume = reask.state_path(CALLER).read_bytes()
        refused = lifecycle.consume(CALLER, answered['reply']['body_sha256'], now=1003.)
        self.assertEqual(refused['outcome'], 'ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before_consume)
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)
        self.assert_original_preserved(original, record, reask.load(CALLER))
        self.assertEqual(ready.read_json(scheduler.enrollment_path(CALLER))['status'], 'ENROLLED')

    def test_reviewed_031_paused_reply_is_discovered_without_code_execution_or_consume(self):
        self.check_paused_discovery()

    def test_reviewed_032_paused_v3_reply_keeps_original_registration_and_consumer(self):
        self.check_paused_discovery(modern=True)

    def test_reviewed_031_paused_reply_keeps_exact_binding(self):
        original, record, body, mailbox = self.legacy_paused()
        ready.write_json(mailbox, dict(body, episode_id='a-different-episode'))
        self.assertEqual(scheduler.sweep(now=1002.)['file_posts'], 0)
        waiting = reask.load(CALLER)
        self.assertEqual(waiting['state'], reask.WAITING)
        self.assertIsNone(waiting['reply'])
        self.assert_original_preserved(original, record, waiting)
        ready.write_json(mailbox, body)
        self.assertEqual(scheduler.sweep(now=1003.)['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['state'], reask.ANSWERED)

    def test_reviewed_031_noncontinuing_pause_keeps_original_controller_in_enrollment(self):
        original, record, body, _mailbox = self.legacy_paused(continuing=False)
        self.assertEqual(scheduler.sweep(now=1002.)['file_posts'], 0)
        answered = reask.load(CALLER)
        self.assertEqual(answered['state'], reask.ANSWERED)
        self.assertEqual(answered['reply']['body'], body)
        self.assert_original_preserved(original, record, answered)
        self.assertEqual(ready.read_json(scheduler.enrollment_path(CALLER))['status'], 'FINISHED')
        self.assertEqual(lifecycle.consume(CALLER, now=1003.)['outcome'],
                         'ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)


if __name__ == '__main__':
    unittest.main()
