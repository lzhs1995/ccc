import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import cmux_bridge as b
from cmux_native_delivery import NativeDeliveryError
from native_test_support import LegacyTerminalFixture, ScreenSequence

IDLE = '› Ask Codex to do anything\nGPT-6-Astra high\n? for shortcuts  ⚠ 5 warnings · f2 to view'


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        report = root / 'REPORT.md'
        report.write_text('real report')
        self.packpath = root / 'task-pack.json'
        self.receipt = root / 'completion-callback-receipt.json'
        self.pack = dict(task_id='test', completion_nonce='nonce12345',
                         report=str(report), callback_target='surface:46',
                         completion_receipt=str(self.receipt), executor_uuid='EXECUTOR')
        self.pack['completion_callback'] = f'DONE|test|nonce12345|REPORT={report}'
        self.packpath.write_text(json.dumps(self.pack))
        self.proof = dict(workspace_uuid='WS', caller_surface_uuid='EXECUTOR',
                          target_surface_uuid='SUPERVISOR', target_pane_uuid='PANE')
        for p in (patch.object(Path, 'home', return_value=root),
                  patch.object(b, 'validate_task_pack_contract', return_value=self.pack),
                  patch('availability_contract.require_action'),
                  patch.object(b, 'pin_workspace', return_value=self.proof),
                  patch.object(b.time, 'sleep')):
            p.start()
            self.addCleanup(p.stop)
        self.native = LegacyTerminalFixture.attach(self, home=root, identity=self.proof)
        self.journal = self.receipt.with_name(self.receipt.stem + '-attempts')

    def confirmed_screen(self):
        return '› ' + self.pack['completion_callback'] + '\n• Read report\n' + IDLE

    def pending_screen(self, *, hint=True):
        screen = '• Working (3s • esc to interrupt)\n› ' + self.pack['completion_callback'] + '\nGPT-6 high'
        return screen + ('\ntab to queue message' if hint else '')

    def queued_pending_screen(self, *, hint=True):
        # queue 区新增后，live composer 保持原内容与空行，单独验证排队撤销资格。
        queue = 'Messages to be submitted after next tool call\n' + self.pack['completion_callback']
        return self.pending_screen(hint=hint).replace('\n› ', '\n' + queue + '\n› ', 1)

    def pending_original(self, observations=None):
        screen = self.pending_screen(hint=False)
        after = [screen] if observations is None else observations
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(self.pack['completion_callback']), *after)), \
                patch.object(b, 'send_text'), patch.object(b, 'send_key') as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                self.call()
            key.assert_called_once_with('surface:46', 'enter')
        return screen + '\ntab to queue message'

    @staticmethod
    def add_rows(screen, added):
        return screen.replace('\nGPT-6 high', '\n' + added + '\nGPT-6 high', 1)

    def assert_resume_refuses(self, screens):
        with patch.object(b, 'read_screen', side_effect=ScreenSequence(screens)), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises((b.DispatchUnconfirmed, b.TaskPackContractError)):
                self.call(resume_queue_only=True)
            send.assert_not_called()
            key.assert_not_called()
        attempts = list(self.journal.glob('attempt-*.json'))
        self.assertEqual(len(attempts), 1)
        events = json.loads(attempts[0].read_text())['events']
        self.assertEqual(sum(e['phase'] == 'PASTE_INTENT' for e in events), 1)
        self.assertFalse(any(e['phase'] in ('QUEUE_TAB_INTENT', 'EXTRA_ENTER_INTENT') for e in events))
        self.assertFalse(self.receipt.exists())

    def test_original_enter_resume_one_tab_no_paste(self):
        pending = self.pending_original()
        with patch.object(b, 'read_screen', return_value=pending), \
                patch.object(b, 'send_text') as send, \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(self.pack['completion_callback'])) as key:
            self.assertTrue(self.call(resume_queue_only=True)['confirmed'])
            send.assert_not_called()
            key.assert_called_once_with('surface:46', 'tab')

    def test_queue_observation_does_not_create_receipt(self):
        pending = self.pending_original()
        queue = 'Messages to be submitted after next tool call\n' + self.pack['completion_callback'] + '\n' + IDLE
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key, \
                patch.object(b, 'read_screen', side_effect=lambda *a, **kw: queue if key.called else pending):
            with self.assertRaises(b.DispatchUnconfirmed):
                self.call(resume_queue_only=True)
            key.assert_called_once(); send.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_replaced_lock_blocks_recovery(self):
        pending = self.pending_original()
        def screen(*args, **kwargs):
            lock = self.journal / 'delivery.lock'
            lock.rename(lock.with_suffix('.retired'))
            lock.touch()
            return pending
        with patch.object(b, 'read_screen', side_effect=screen), patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.TaskPackContractError, 'LOCK_CHANGED'):
                self.call(resume_queue_only=True)
            key.assert_not_called()

    def test_failed_queue_key_is_never_retried(self):
        pending = self.pending_original()
        with patch.object(b, 'read_screen', return_value=pending), patch.object(b, 'send_text') as send, patch.object(b, 'send_key', side_effect=RuntimeError('transport lost')) as key:
            with self.assertRaises(RuntimeError):
                self.call(resume_queue_only=True)
            with self.assertRaisesRegex(b.TaskPackContractError, 'NO_RECOVERABLE'):
                self.call(resume_queue_only=True)
            key.assert_called_once(); send.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_changed_composer_cannot_resume(self):
        pending = self.pending_original().replace('DONE|', 'CHANGED|')
        with patch.object(b, 'read_screen', return_value=pending), patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                self.call(resume_queue_only=True)
            key.assert_not_called(); send.assert_not_called()

    def test_current_added_blank_line_cannot_resume(self):
        pending = self.pending_original()
        self.assert_resume_refuses([self.add_rows(pending, '')])

    def test_current_added_multiple_blank_lines_cannot_resume(self):
        pending = self.pending_original()
        self.assert_resume_refuses([self.add_rows(pending, '\n')])

    def test_current_added_gutter_blank_line_cannot_resume(self):
        pending = self.pending_original()
        self.assert_resume_refuses([self.add_rows(pending, '  ')])

    def test_original_post_enter_blank_change_blocks_restored_callback(self):
        intact = self.pending_screen(hint=False)
        pending = self.pending_original([self.add_rows(intact, ''), intact])
        self.assert_resume_refuses([pending])

    def test_original_post_enter_unknown_blocks_restored_callback(self):
        intact = self.pending_screen(hint=False)
        pending = self.pending_original(['render unavailable', intact])
        self.assert_resume_refuses([pending])

    def test_original_post_enter_compaction_blocks_restored_callback(self):
        intact = self.pending_screen(hint=False)
        pending = self.pending_original([intact.replace('Working', 'Compacting context'), intact])
        self.assert_resume_refuses([pending])

    def test_original_post_enter_queue_blocks_restored_callback(self):
        intact = self.pending_screen(hint=False)
        queued = self.queued_pending_screen(hint=False)
        self.assertTrue(b.pending_queue_holds(queued, self.pack['completion_callback']))
        pending = self.pending_original([queued, intact])
        self.assert_resume_refuses([pending])

    def test_queue_during_resume_then_restored_callback_still_refuses(self):
        pending = self.pending_original()
        self.assert_resume_refuses([pending, self.queued_pending_screen(), pending])

    def test_rejected_queue_callback_cannot_be_retried_after_restore(self):
        pending = self.pending_original()
        self.assert_resume_refuses([self.queued_pending_screen()])
        self.assert_resume_refuses([pending])

    def test_current_compaction_blocks_callback_without_waiting_for_restore(self):
        pending = self.pending_original()
        self.assert_resume_refuses([pending.replace('Working', 'Compacting context'), pending])

    def test_changed_during_resume_then_restored_callback_still_refuses(self):
        pending = self.pending_original()
        self.assert_resume_refuses([pending, self.add_rows(pending, ''), pending])

    def test_unknown_during_resume_then_restored_callback_still_refuses(self):
        pending = self.pending_original()
        self.assert_resume_refuses([pending, 'render unavailable', pending])

    def test_partial_during_resume_then_restored_callback_still_refuses(self):
        pending = self.pending_original()
        partial = pending.replace(self.pack['completion_callback'], 'DONE|test|nonce12345')
        self.assert_resume_refuses([pending, partial, pending])

    def test_changed_during_compaction_then_restored_callback_still_refuses(self):
        pending = self.pending_original()
        changed = self.add_rows(pending.replace('Working', 'Compacting context'), '')
        self.assert_resume_refuses([pending, changed, pending])

    def test_unchanged_compaction_during_resume_still_refuses(self):
        pending = self.pending_original()
        self.assert_resume_refuses([pending, pending.replace('Working', 'Compacting context'), pending])

    def test_rejected_changed_callback_cannot_be_retried_after_restore(self):
        pending = self.pending_original()
        self.assert_resume_refuses([self.add_rows(pending, '')])
        self.assert_resume_refuses([pending])

    def test_recovery_rejects_tampered_event(self):
        self.pending_original()
        path = self.journal / 'attempt-0001.json'
        data = json.loads(path.read_text())
        data['events'][0]['screen'] = 'tampered'
        path.write_text(json.dumps(data))
        with patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(NativeDeliveryError, 'ORIGINAL_PASTE_INTENT_REQUIRED'):
                self.call(resume_queue_only=True)
            key.assert_not_called()

    def test_other_sender_target_lock_blocks_callback_without_input(self):
        import hashlib
        root = Path.home() / '.local/state/multi-agent-collaboration/deliveries-v1'
        root.mkdir(parents=True)
        key = hashlib.sha256(b'SUPERVISOR').hexdigest()
        with (root / ('target-' + key + '.lock')).open('a+b') as lock, \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as keys:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(b.TaskPackContractError, 'CALLBACK_TARGET_IN_PROGRESS'):
                self.call()
            send.assert_not_called(); keys.assert_not_called()

    def call(self, **kw):
        return b.submit_completion_callback(str(self.packpath), **kw)

    def queued(self):
        screen = 'Messages to be submitted after current tool\n' + self.pack['completion_callback'] + '\n' + IDLE
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(self.pack['completion_callback']), screen)), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                self.call()
            send.assert_called_once()
            key.assert_called_once()

    def test_confirmed_report_hash_and_duplicate_refusal(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(self.pack['completion_callback']), self.confirmed_screen())), \
                patch.object(b, 'send_text') as send, \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(self.pack['completion_callback'])) as key:
            result = self.call()
            self.assertTrue(result['confirmed'])
            self.assertEqual(result['report_sha256'], b._sha256_file(self.pack['report']))
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_called_once()
            key.assert_called_once()

    def test_zero_input_persists_and_one_explicit_retry(self):
        with patch.object(b, 'read_screen', return_value='host@mac ~ %'), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            for _ in range(2):
                with self.assertRaises(b.DispatchUnconfirmed):
                    self.call()
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            self.assertEqual(json.loads((self.journal / 'attempt-0001.json').read_text())['phase'], 'NO_INPUT')
            send.assert_not_called()
            key.assert_not_called()

    def test_queued_never_repasted_and_read_only_reconcile(self):
        self.queued()
        self.native.append_user(self.pack['completion_callback'])
        with patch.object(b, 'read_screen', return_value=self.confirmed_screen()), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            result = self.call(reconcile_only=True)
            self.assertTrue(result['reconciled_read_only'])
            send.assert_not_called()
            key.assert_not_called()

    def test_pending_compose_unknown_and_queue_cannot_reconcile(self):
        self.queued()
        for screen in ['› '+self.pack['completion_callback']+'\nGPT-6 high',
                       'Messages to be submitted after current tool\n'+self.confirmed_screen(),
                       'render missing', IDLE, '• Read nonce12345 unrelated output\n'+IDLE]:
            with self.subTest(screen=screen), patch.object(b, 'read_screen', return_value=screen), \
                    patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                with self.assertRaises(b.DispatchUnconfirmed):
                    self.call(reconcile_only=True)
                send.assert_not_called()
                key.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_partial_or_cross_block_callback_cannot_reconcile(self):
        self.queued()
        callback = self.pack['completion_callback']
        self.native.append_user('nonce12345')
        self.native.append_user(callback + ' extra')
        screens = [
            '› nonce12345\n• Read report\n' + IDLE,
            '› ' + callback + '\n› unrelated\n• Read report\n' + IDLE,
            '› ' + callback + ' extra\n• Read report\n' + IDLE,
        ]
        for screen in screens:
            with self.subTest(screen=screen), patch.object(b, 'read_screen', return_value=screen), \
                    patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                with self.assertRaises(b.DispatchUnconfirmed):
                    self.call(reconcile_only=True)
                send.assert_not_called()
                key.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_report_change_refuses_reconcile(self):
        self.queued()
        Path(self.pack['report']).write_text('different report')
        with patch.object(b, 'send_text') as send:
            with self.assertRaises(b.TaskPackContractError):
                self.call(reconcile_only=True)
            send.assert_not_called()

    def test_original_executor_only(self):
        self.proof['caller_surface_uuid'] = 'OTHER'
        with patch.object(b, 'send_text') as send:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_not_called()

    def test_reconcile_cannot_invent_legacy_attempt(self):
        with patch.object(b, 'read_screen', return_value=self.confirmed_screen()), \
                patch.object(b, 'send_text') as send:
            with self.assertRaises(b.TaskPackContractError):
                self.call(reconcile_only=True)
            send.assert_not_called()

    def test_concurrent_callback_lock_refuses_input(self):
        self.journal.mkdir()
        with (self.journal / 'delivery.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(b, 'send_text') as send:
                with self.assertRaises(b.TaskPackContractError):
                    self.call()
                send.assert_not_called()

    def test_crash_after_paste_intent_requires_reconciliation(self):
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text', side_effect=RuntimeError('lost reply')):
            with self.assertRaises(RuntimeError):
                self.call()
        with patch.object(b, 'send_text') as send:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_not_called()

    def test_legacy_pending_never_resends_or_fabricates_receipt(self):
        legacy = Path(str(self.receipt) + '.pending.json')
        for content in ('{}', '{truncated'):
            legacy.write_text(content)
            for reconcile in (False, True):
                with self.subTest(content=content, reconcile=reconcile), \
                        patch.object(b, 'send_text') as send, \
                        patch.object(b, 'send_key') as key:
                    with self.assertRaisesRegex(b.TaskPackContractError, 'LEGACY_CALLBACK_PENDING'):
                        self.call(reconcile_only=reconcile)
                    send.assert_not_called()
                    key.assert_not_called()
                    self.assertFalse(self.receipt.exists())
                    self.assertEqual(legacy.read_text(), content)

    def hook_proof(self):
        from cmux_submit_confirmation_guard import _attempt_evidence
        return _attempt_evidence(dict(kind='callback', pack=str(self.packpath)), 'surface:46', b)

    def complete(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(self.pack['completion_callback']), self.confirmed_screen())), \
                patch.object(b, 'send_text'), \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(self.pack['completion_callback'])):
            self.call()

    def test_posthook_original_receipt_readonly(self):
        self.complete()
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            self.assertEqual(self.hook_proof()['source'], 'revalidated_callback_journal')
            send.assert_not_called()
            key.assert_not_called()
        Path(self.pack['report']).write_text('changed report')
        self.assertIsNone(self.hook_proof())

    def test_posthook_reconciliation_observation(self):
        self.queued()
        self.native.append_user(self.pack['completion_callback'])
        with patch.object(b, 'read_screen', return_value=self.confirmed_screen()):
            result = self.call(reconcile_only=True)
        self.assertIsNotNone(self.hook_proof())
        observation = Path(result['observation'])
        data = json.loads(observation.read_text())
        data['input_operations'] = True
        observation.write_text(json.dumps(data))
        self.assertIsNone(self.hook_proof())

    def test_posthook_missing_or_changed_receipt(self):
        self.complete()
        data = json.loads(self.receipt.read_text())
        data['report_sha256'] = 'wrong'
        self.receipt.write_text(json.dumps(data))
        self.assertIsNone(self.hook_proof())
        self.receipt.unlink()
        self.assertIsNone(self.hook_proof())

    def test_posthook_changed_pack_and_identity(self):
        self.complete()
        original = self.packpath.read_text()
        self.packpath.write_text(original + ' ')
        self.assertIsNone(self.hook_proof())
        self.packpath.write_text(original)
        self.proof['caller_surface_uuid'] = 'OTHER'
        self.assertIsNone(self.hook_proof())

    def test_cli_read_only_flag(self):
        with patch.object(b, 'submit_completion_callback', return_value={}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b._cli_main(['submit-completion-callback', '--task-pack', str(self.packpath), '--reconcile-only']), 0)
            self.assertTrue(call.call_args.kwargs['reconcile_only'])


if __name__ == '__main__':
    unittest.main()
