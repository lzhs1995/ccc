"""Original bridge probe and writer, with only external I/O replaced.

Run with --original-controller /immutable/release/source to verify a complete
historical controller in a fresh process. No test may reach a live terminal.
"""
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import continue_bridge_probe as continuation

if __name__ == '__main__' and '--original-controller' in sys.argv:
    index = sys.argv.index('--original-controller')
    continuation.original_modules(sys.argv[index + 1])
    del sys.argv[index:index + 2]

import cmux_bridge as bridge
import cmux_evidence_io as evidence
import cmux_input_mutex as mutex
import cmux_message_journal as journal
import cmux_native_delivery as native
import cmux_workspace_guard as guard
import mac_harness as harness
from native_test_support import NativeFixture


class BridgeContinuationTests(unittest.TestCase):
    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.fixture = NativeFixture.attach(self, provider='claude')
        self.fixture.pin.side_effect = lambda *a, **k: copy.deepcopy(self.fixture.identity)
        self.home = self.fixture.home
        self.root = self.home / 'original-task'
        self.root.mkdir()
        self.task = 'original-bridge-task'
        self.token = 'B1_8a1e9e3a'
        self.thread = 'original-supervisor-thread'
        self.reservation = 'e8865131-0f4e-4ad4-a838-1c63cd620e92'
        self.gate = dict(task_id=self.task, status='PASS', side_panel=True,
            workspace_uuid='workspace', workspace_ref='workspace:1',
            supervisor_surface_uuid='caller', supervisor='caller',
            executors=[dict(surface_ref='target', surface_uuid='target', provider='claude')],
            workspace_proofs={'target': dict(self.fixture.identity)},
            native_participants=[dict(role='supervisor', surface_uuid='caller', provider='codex',
                                     native_session_id=self.thread),
                                 dict(role='executor', surface_uuid='target', provider='claude',
                                      native_session_id=self.fixture.session_id)])
        self.row = dict(task_id=self.task, executor='target', ordinal=1, token=self.token,
            status='COMPOSE_OCCUPIED', submission_state='COMPOSE_OCCUPIED', token_sent=False,
            observed_in_screen=False, clear_confirmed=False, clear_attempts=0, clear_key_count=0,
            clear_verified_at=None, pre_read_performed=True, compose_was_empty_before_send=True,
            active_or_queued_before_send=True, override=dict(force_compose_override=False))
        self.marker_path = self.home / 'active/workspace' / (self.reservation + '.json')
        self.marker_path.parent.mkdir(parents=True)
        self.marker = dict(marker_version=2, collaboration_id=self.reservation,
            task_id=self.task, artifact_root=str(self.root), workspace_id='workspace',
            workspace_uuid='workspace', armed_at=datetime.now(timezone.utc).isoformat(),
            ttl_seconds=21600, last_activity_at='original-heartbeat',
            participants=[dict(p, ordinal=i, surface_ref=p['surface_uuid'])
                          for i, p in enumerate(self.gate['native_participants'])])
        self.patch(harness, '_ACTIVE_DIR', new=self.home / 'active')
        self.patch(harness, '_workspace_key', return_value='workspace')
        self.caller = self.patch(guard, 'caller_snapshot', return_value=(
            dict(caller=dict(surface_id='caller', workspace_id='workspace')), None,
            dict(CMUX_WORKSPACE_ID='workspace'), dict(foreground=dict(thread_id=self.thread))))
        self.compose = ''
        self.read_action = lambda: None
        self.screen = self.patch(bridge, 'read_screen', side_effect=self.read_screen)
        self.fixture.send.side_effect = self.send
        self.fixture.key.side_effect = self.key
        self.modules = (bridge, native, evidence, journal, harness, mutex)
        self.args = SimpleNamespace(controller=str(Path(harness.__file__).resolve().parent.parent),
            artifact_root=str(self.root), reservation_id=self.reservation,
            lines=200, apply=False, original_harness_exited=True)
        self.save()

    def save(self):
        data = {'identity-gate.json': self.gate,
                'bridge-test-evidence.json': dict(self.row, executors=[self.row]),
                'naming-proof.json': dict(status='PASS')}
        for name, value in data.items():
            (self.root / name).write_bytes(continuation.recovery.json_bytes(value))
        self.marker_path.write_bytes(continuation.recovery.json_bytes(self.marker))
        self.args.identity_sha256 = continuation.digest((self.root / 'identity-gate.json').read_bytes())
        self.args.bridge_sha256 = continuation.digest((self.root / 'bridge-test-evidence.json').read_bytes())

    def read_screen(self, *a, **k):
        self.read_action()
        return '────────────────────────\n❯ ' + self.compose + '\n────────────────────────\n[claude-opus-5]'

    def send(self, surface, text):
        self.assertEqual((surface, text), ('target', self.token))
        self.assertEqual(self.compose, '')
        self.compose = text

    def key(self, surface, key):
        self.assertEqual(surface, 'target')
        self.assertIn(key, ('end', 'backspace'))
        if key == 'backspace':
            self.assertTrue(self.compose and self.token.startswith(self.compose))
            self.compose = self.compose[:-1]

    def run_probe(self, apply=False):
        self.args.apply = apply
        result = continuation.continue_probe(self.args, self.modules)
        self.fixture.rpc.assert_not_called()
        self.assertEqual(result['submission_operations'], 0)
        return result

    def files(self):
        return {str(p): evidence.snapshot(p) for p in self.home.rglob('*') if p.is_file()}

    def assert_unavailable_without_writes(self, error):
        before = self.files()
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'BRIDGE_CONTINUATION_UNAVAILABLE', result)
        self.assertIn(error, result['error'])
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(before, self.files())
        self.fixture.send.assert_not_called()
        self.fixture.key.assert_not_called()
        return result

    def test_static_preview_is_read_only_without_live_identity_or_mutex(self):
        before = self.files()
        result = self.run_probe()
        self.assertEqual(result['status'], 'STATIC_ELIGIBLE_NO_INPUT', result)
        self.assertEqual(result['live_readiness'], 'NOT_OBSERVED')
        self.assertEqual(before, self.files())
        self.caller.assert_not_called()
        self.screen.assert_not_called()
        self.fixture.send.assert_not_called()

    def test_new_shared_pane_gate_uses_complete_channel_evidence(self):
        workspace, caller, target, pane = [
            f'00000000-0000-4000-8000-{i:012d}' for i in range(1, 5)]
        self.gate.update(workspace_uuid=workspace, supervisor_surface_uuid=caller,
                         supervisor_pane_ref='pane:1', supervisor_pane_uuid=pane,
                         supervisor_surface_type='terminal', side_panel=False,
                         terminal_channels_version=1, executor='target',
                         executor_surface_uuid=target, executor_pane_ref='pane:1')
        self.gate['executors'][0].update(surface_uuid=target, pane_ref='pane:1',
                                         pane_uuid=pane, surface_type='terminal',
                                         workspace_ref='workspace:1', workspace_uuid=workspace)
        self.gate['workspace_proofs']['target'] = dict(
            workspace_uuid=workspace, workspace_ref='workspace:1',
            caller_surface_uuid=caller, caller_surface_ref='caller', caller_pane_uuid=pane,
            target_surface_uuid=target, target_surface_ref='target', target_pane_uuid=pane)
        for row, surface in zip(self.gate['native_participants'], (caller, target)):
            row['surface_uuid'] = surface
        original = dict(self.row, executors=[self.row])
        peer, row = continuation.eligible(harness, self.gate, original)
        self.assertEqual(peer['surface_uuid'], target)
        self.assertEqual(row, self.row)
        self.gate['workspace_proofs']['target']['target_pane_uuid'] = workspace
        with self.assertRaisesRegex(ValueError, 'BRIDGE_ORIGINAL_SINGLE_CLAUDE_REQUIRED'):
            continuation.eligible(harness, self.gate, original)
        self.fixture.send.assert_not_called()
        self.fixture.key.assert_not_called()

    def test_original_token_once_archived_original_writer_and_same_reservation(self):
        original = (self.root / 'bridge-test-evidence.json').read_bytes()
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'BRIDGE_CONTINUED', result)
        self.fixture.send.assert_called_once_with('target', self.token)
        self.assertEqual(self.fixture.key.call_count, len(self.token) + 1)
        self.assertEqual(self.compose, '')
        self.assertEqual(result['bridge_writes'], 1)
        archive = self.root / continuation.ARCHIVE
        self.assertEqual((archive / 'bridge-test-evidence.json.original').read_bytes(), original)
        current = json.loads((self.root / 'bridge-test-evidence.json').read_bytes())
        self.assertTrue(current['clear_confirmed'])
        self.assertTrue(current['token_sent'])
        self.assertEqual(current['token'], self.token)
        self.assertFalse((self.root / 'handshake-receipt.json').exists())
        marker = json.loads(self.marker_path.read_bytes())
        self.assertEqual({k: v for k, v in marker.items() if k != 'last_activity_at'},
                         {k: v for k, v in self.marker.items() if k != 'last_activity_at'})
        self.assertEqual(json.loads((archive / 'RESULT.json').read_bytes()), result)

    def test_wrong_supervisor_thread(self):
        self.caller.return_value[3]['foreground']['thread_id'] = 'maintenance-thread'
        self.assert_unavailable_without_writes('SUPERVISOR_THREAD_CHANGED')

    def test_wrong_original_session(self):
        self.gate['native_participants'][1]['native_session_id'] = 'different-session'
        self.marker['participants'][1]['native_session_id'] = 'different-session'
        self.save()
        self.assert_unavailable_without_writes('EXECUTOR_SESSION_CHANGED')

    def test_changed_pane(self):
        self.gate['workspace_proofs']['target']['target_pane_uuid'] = 'different-pane'
        self.save()
        self.assert_unavailable_without_writes('EXECUTOR_SESSION_CHANGED')

    def test_expired_reservation(self):
        self.marker['armed_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self.save()
        self.assert_unavailable_without_writes('EXPIRED_OR_INVALID_OWNER_SETTLEMENT_REQUIRED')

    def test_original_harness_exit_is_required(self):
        self.args.original_harness_exited = False
        self.assert_unavailable_without_writes('HARNESS_EXIT_ATTESTATION_REQUIRED')

    def test_evidence_pin_change(self):
        (self.root / 'bridge-test-evidence.json').write_text('{}')
        self.assert_unavailable_without_writes('EVIDENCE_PIN_CHANGED')

    def test_token_was_already_sent(self):
        self.row['token_sent'] = True
        self.save()
        self.assert_unavailable_without_writes('NEVER_TYPED_TOKEN_REQUIRED')

    def test_prior_clear_or_force_is_ineligible(self):
        self.row['override']['clear_actions'] = ['escape']
        self.save()
        self.assert_unavailable_without_writes('ZERO_OVERRIDE_REQUIRED')

    def test_handshake_or_pack_stops_probe(self):
        for name in ('handshake-receipt.json', 'task-pack.json'):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text('{}')
                self.assert_unavailable_without_writes('HANDSHAKE_OR_TASK_PACK_ALREADY_EXISTS')
                path.unlink()

    def test_real_input_lock_contention(self):
        with mutex.hold(self.fixture.identity):
            self.assert_unavailable_without_writes('INPUT_CHANNEL_BUSY')

    def test_real_task_directory_lock_contention(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_unavailable_without_writes('temporarily unavailable')
        finally:
            os.close(fd)

    def test_existing_foreign_draft_preserved_without_using_slot(self):
        self.compose = 'existing user draft'
        result = self.run_probe(apply=True)
        self.assertIn('PRESERVE_COMPOSE', result['error'])
        self.assertFalse((self.root / continuation.ARCHIVE).exists())
        self.assertEqual(self.compose, 'existing user draft')
        self.fixture.send.assert_not_called()
        self.fixture.key.assert_not_called()

    def test_evidence_race_before_archive(self):
        self.read_action = lambda: (self.root / 'identity-gate.json').write_text('{}')
        result = self.run_probe(apply=True)
        self.assertIn('EVIDENCE_CHANGED_DURING', result['error'])
        self.assertFalse(result['archive_created'])
        self.fixture.send.assert_not_called()

    def test_crash_after_intent_blocks_replay(self):
        self.fixture.send.side_effect = KeyboardInterrupt('simulated process interruption')
        with self.assertRaises(KeyboardInterrupt):
            self.run_probe(apply=True)
        archive = self.root / continuation.ARCHIVE
        self.assertTrue((archive / 'PROBE-INTENT.json').exists())
        self.assertFalse((archive / 'RESULT.json').exists())
        self.fixture.send.reset_mock()
        self.assert_unavailable_without_writes('SLOT_EXISTS_NO_REPLAY')

    def test_success_cannot_repeat(self):
        self.assertEqual(self.run_probe(apply=True)['status'], 'BRIDGE_CONTINUED')
        self.fixture.send.reset_mock()
        self.fixture.key.reset_mock()
        self.assert_unavailable_without_writes('SLOT_EXISTS_NO_REPLAY')

    def test_foreign_text_during_cleanup_never_deleted_or_published(self):
        original = (self.root / 'bridge-test-evidence.json').read_bytes()
        self.fixture.send.side_effect = lambda *a: setattr(self, 'compose', 'foreign text')
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'BRIDGE_CONTINUATION_FAILED_NO_REPLAY', result)
        self.assertEqual(self.compose, 'foreign text')
        self.fixture.key.assert_not_called()
        self.assertEqual((self.root / 'bridge-test-evidence.json').read_bytes(), original)
        self.assertEqual(result['bridge_writes'], 0)

    def test_mutation_after_paste_prevents_cleanup_and_publish(self):
        def mutate(surface, text):
            self.send(surface, text)
            (self.root / 'identity-gate.json').write_text('{}')
        self.fixture.send.side_effect = mutate
        result = self.run_probe(apply=True)
        self.assertEqual(result['status'], 'BRIDGE_CONTINUATION_INCOMPLETE_NO_REPLAY', result)
        self.fixture.key.assert_not_called()
        self.assertIsNone(result['input_operations'])
        self.assertEqual(result['bridge_writes'], 0)


if __name__ == '__main__':
    unittest.main()
