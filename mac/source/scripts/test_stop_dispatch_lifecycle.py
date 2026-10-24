"""Stop guard lifecycle: a finalized pack is not a delivered task.

Real hook stdin/exit-code tests. HOME is private so the supervisor's
task-dispatch journal is a fixture written in the producer's exact shape.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

import executor_closeout as closeout
import offline_test_hook
import cmux_bridge as BRIDGE
from native_test_support import NativeFixture, native_hook_command

HOOK = Path(os.environ.get('STOP_GUARD_UNDER_TEST',
                           Path(__file__).with_name('cmux_consensus_stop_guard.py')))


class StopDispatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='stop-lifecycle-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native_state = None
        self.home = self.root / 'home'
        self.workspace, self.surface, self.supervisor = [str(uuid.uuid4()).upper() for _ in range(3)]
        self.marker = dict(task_id='life-task', workspace_uuid=self.workspace,
                           artifact_root=str(self.root), participants=[
                               dict(role='supervisor', surface_ref='surface:1',
                                    surface_uuid=self.supervisor),
                               dict(role='executor', surface_uuid=self.surface)])
        self.active = self.root / 'active'
        self.active.mkdir()
        self.write(self.active / (self.workspace + '.json'), self.marker)
        self.report = self.root / 'report.md'
        self.receipt = self.root / 'receipt.json'
        self.pack_path = self.root / 'task-pack.json'
        self.pack = dict(draft=False, task_id='life-task', executor_uuid=self.surface,
                         completion_nonce='nonce', callback_target='surface:1',
                         completion_callback='DONE|life-task|nonce', report=str(self.report),
                         completion_receipt=str(self.receipt))
        self.write(self.pack_path, self.pack)
        key = hashlib.sha256(json.dumps([self.supervisor, 'life-task']).encode()).hexdigest()
        self.dispatch = self.home / '.local/state/multi-agent-collaboration/task-dispatch-v1' / key
        self.env = dict(os.environ, HOME=str(self.home), CMUX_WORKSPACE_ID=self.workspace,
                        CMUX_SURFACE_ID=self.surface, PYTHONDONTWRITEBYTECODE='1')

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def sha(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def dispatch_attempt(self, phase, events=(), name='attempt-0001.json', **binding):
        """Producer shape from cmux_task_journal.deliver."""
        self.dispatch.mkdir(parents=True, exist_ok=True)
        (self.dispatch / 'delivery.lock').touch()
        value = dict(task_id='life-task', marker='nonce', payload_sha256='p' * 64,
                     task_pack_sha256=self.sha(self.pack_path),
                     identity=dict(workspace_uuid=self.workspace, caller_surface_uuid=self.supervisor,
                                   target_surface_uuid=self.surface, target_pane_uuid='pane'))
        value.update(binding)
        lock_stat = (self.dispatch / 'delivery.lock').stat()
        self.write(self.dispatch / name, dict(binding=value, phase=phase,
                                              delivery_lock_identity=dict(device=lock_stat.st_dev,
                                                                          inode=lock_stat.st_ino),
                                              events=[dict(phase=e, at_epoch=1.0) for e in events],
                                              started_at_epoch=1.0, ended_at_epoch=2.0,
                                              error='COMPOSE_OCCUPIED'))

    def stop(self, final='Status reply before the task arrived.', **extra):
        data = dict(hook_event_name='Stop', last_assistant_message=final, stop_hook_active=False)
        data.update(extra)
        command = (native_hook_command(HOOK, self.active, self.native_state)
                   if self.native_state else offline_test_hook.command(HOOK, self.active))
        return subprocess.run(command, input=json.dumps(data),
                              text=True, capture_output=True, env=self.env, timeout=10)

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    # finalized, never delivered -------------------------------------------------
    def test_finalized_explicit_no_input_allows_without_writing(self):
        self.dispatch_attempt('NO_INPUT')
        before = self.snapshot()
        r = self.stop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '')
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.receipt.exists())

    def test_two_no_input_attempts_allow(self):
        self.dispatch_attempt('NO_INPUT')
        self.dispatch_attempt('NO_INPUT', name='attempt-0002.json')
        self.assertEqual(self.stop().returncode, 0)

    def test_no_input_does_not_license_evidence_claims(self):
        self.dispatch_attempt('NO_INPUT')
        self.assertEqual(self.stop(final='consensus-validation PASS').returncode, 2)

    # missing record is not "not started" ------------------------------------
    def test_finalized_without_dispatch_journal_blocks_as_unknown(self):
        r = self.stop()
        self.assertEqual(r.returncode, 2)
        self.assertIn('task dispatch state: UNKNOWN', r.stderr)

    def test_empty_journal_or_prepared_blocks(self):
        self.dispatch.mkdir(parents=True)
        (self.dispatch / 'delivery.lock').touch()
        self.assertIn('UNKNOWN', self.stop().stderr)
        self.dispatch_attempt('PREPARED')
        r = self.stop()
        self.assertEqual(r.returncode, 2)
        self.assertIn('SUBMITTED_OR_UNKNOWN', r.stderr)

    def test_missing_lock_and_malformed_attempt_block(self):
        self.dispatch_attempt('NO_INPUT')
        (self.dispatch / 'delivery.lock').unlink()
        self.assertEqual(self.stop().returncode, 2)
        self.assertFalse((self.dispatch / 'delivery.lock').exists())
        (self.dispatch / 'delivery.lock').touch()
        (self.dispatch / 'attempt-0001.json').write_text('{')
        self.assertEqual(self.stop().returncode, 2)

    # pasted / queued / unknown / consumed -----------------------------------
    def test_submitted_queued_or_later_attempt_blocks(self):
        cases = [('PASTED', ['PASTE_INTENT', 'PASTED']),
                 ('POST_ENTER_OBSERVATION', ['PASTE_INTENT', 'PASTED', 'ENTER_INTENT',
                                             'ENTER_SENT', 'POST_ENTER_OBSERVATION']),
                 ('POST_QUEUE_TAB_OBSERVATION', ['PASTE_INTENT', 'QUEUE_TAB_INTENT',
                                                 'POST_QUEUE_TAB_OBSERVATION']),
                 ('NO_INPUT', ['PASTE_INTENT'])]
        for phase, events in cases:
            with self.subTest(phase=phase, events=events):
                self.dispatch_attempt(phase, events)
                r = self.stop()
                self.assertEqual(r.returncode, 2)
                self.assertIn('SUBMITTED_OR_UNKNOWN', r.stderr)

    def test_no_input_then_submitted_retry_blocks(self):
        self.dispatch_attempt('NO_INPUT')
        self.dispatch_attempt('POST_ENTER_OBSERVATION', ['PASTE_INTENT'], name='attempt-0002.json')
        self.assertEqual(self.stop().returncode, 2)

    def test_confirmed_dispatch_receipt_blocks(self):
        self.dispatch_attempt('NO_INPUT')
        self.write(self.dispatch / 'receipt.json', dict(confirmed=True))
        r = self.stop()
        self.assertEqual(r.returncode, 2)
        self.assertIn('CONFIRMED_OR_RECONCILED', r.stderr)

    def test_in_flight_sender_lock_blocks(self):
        self.dispatch_attempt('NO_INPUT')
        with open(self.dispatch / 'delivery.lock', 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = self.stop()
        self.assertEqual(r.returncode, 2)
        self.assertIn('IN_FLIGHT', r.stderr)

    def test_binding_to_other_pack_executor_or_task_blocks(self):
        for change in (dict(task_pack_sha256='0' * 64), dict(task_id='other-task'),
                       dict(identity=dict(target_surface_uuid=str(uuid.uuid4())))):
            with self.subTest(change=list(change)):
                self.dispatch_attempt('NO_INPUT', **change)
                self.assertEqual(self.stop().returncode, 2)

    def test_pack_change_after_no_input_blocks(self):
        self.dispatch_attempt('NO_INPUT')
        self.write(self.pack_path, dict(self.pack, completion_nonce='new'))
        self.assertEqual(self.stop().returncode, 2)

    # executed but no callback / legal completion / terminal / reentry -------
    def test_executed_business_without_callback_blocks_even_if_dispatch_no_input(self):
        self.dispatch_attempt('NO_INPUT')
        self.report.write_text('work done\n')
        (self.root / 'receipt-attempts').mkdir()
        r = self.stop()
        self.assertEqual(r.returncode, 2)
        self.assertIn('CALLBACK_ATTEMPTED', r.stderr)

    def test_legal_confirmed_completion_receipt_allows(self):
        nonce = 'life-native-001'
        self.pack.update(completion_nonce=nonce,
                         completion_callback=f'DONE|life-task|{nonce}|REPORT={self.report}',
                         required_skill=str(BRIDGE.COLLABORATION_SKILL_PATH),
                         completion_delivery=dict(transport='cmux_bridge.submit_completion_callback',
                                                  require_confirmed=True))
        self.write(self.pack_path, self.pack)
        self.dispatch_attempt('POST_ENTER_OBSERVATION', ['PASTE_INTENT'])
        self.report.write_text('done\n')
        native = NativeFixture.attach(self, home=self.home, provider='claude',
            identity=dict(workspace_uuid=self.workspace, caller_surface_uuid=self.surface,
                          target_surface_uuid=self.supervisor, target_pane_uuid='pane'))
        callback = self.pack['completion_callback']
        idle = native.draft('', provider='claude')
        with mock.patch.object(BRIDGE, 'read_screen', side_effect=native.ready_screens(
                idle, native.draft(callback, provider='claude'), idle)):
            native.key.side_effect = native.receipt_on_key(callback)
            receipt = BRIDGE.submit_completion_callback(self.pack_path)
        self.assertTrue(receipt['confirmed'])
        native.send.assert_called_once()
        native.key.assert_called_once()
        self.native_state = native.export_state(self.root / 'native-state.json')
        before = self.snapshot()
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot())
        self.write(self.receipt, dict(receipt, confirmed=False))
        before = self.snapshot()
        result = self.stop(final='报告已冻结，回调尚未确认，等待主管核收。')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WAITING_SUPERVISOR', json.loads(result.stdout)['stopReason'])
        self.assertEqual(self.stop(final='callback confirmed').returncode, 2)
        self.assertEqual(before, self.snapshot())

    def test_terminal_report_allows_honest_wait_without_exact_template(self):
        self.dispatch_attempt('POST_ENTER_OBSERVATION', ['PASTE_INTENT'])
        self.report.write_text('bounded result\n')
        journal = self.root / 'receipt-attempts'
        journal.mkdir()
        (journal / 'delivery.lock').touch()
        self.write(journal / 'attempt-0001.json', dict(
            binding={**{k: self.pack[k] for k in ('task_id', 'completion_nonce',
                     'completion_callback', 'callback_target', 'report')},
                     'task_pack_sha256': self.sha(self.pack_path),
                     'report_sha256': self.sha(self.report), 'report_bytes': self.report.stat().st_size,
                     'identity': dict(workspace_uuid=self.workspace, caller_surface_uuid=self.surface,
                                      target_surface_uuid=self.supervisor, target_pane_uuid='pane')},
            phase='NO_INPUT', events=[], started_at_epoch=1.0, ended_at_epoch=2.0,
            error='COMPOSE_OCCUPIED'))
        line = closeout.handoff_line(dict(task_id='life-task', report=str(self.report)))
        before = self.snapshot()
        for active in (False, True):
            result = self.stop(final=line, stop_hook_active=active)
            self.assertEqual((result.returncode, result.stderr), (0, ''))
            self.assertEqual(json.loads(result.stdout), {
                'continue': False,
                'suppressOutput': True,
                'stopReason': (
                    'WAITING_SUPERVISOR: report frozen; original callback returned. '
                    'Await supervisor receipt reconciliation and acceptance. '
                    'Delivery and task completion remain unconfirmed.'
                ),
            })
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.receipt.exists())
        for final in (line + ' extra', '报告已冻结，回调尚未确认，等待主管核收。'):
            r = self.stop(final=final)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('WAITING_SUPERVISOR', json.loads(r.stdout)['stopReason'])
        for final in ('callback confirmed', 'consensus-validation PASS'):
            r = self.stop(final=final)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stdout, '')
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.receipt.exists())

    def test_stop_hook_active_reentry_only_for_boolean_true(self):
        result = self.stop(stop_hook_active=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(self.stop(stop_hook_active='true').returncode, 2)
        self.assertEqual(self.stop().returncode, 2)


if __name__ == '__main__':
    unittest.main()
