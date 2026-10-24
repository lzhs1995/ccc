"""Historical pre-0.4.54 terminal admission/reconciliation fixtures.

Restart/uncertainty regression tests for supervisor-to-executor dispatch."""
import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import cmux_bridge as b
import cmux_task_journal as j
import cmux_submit_confirmation_guard as guard
from native_test_support import LegacyTerminalFixture

IDLE = '❯ \n[claude-opus-5]'
TEXT = 'STATUS: task-journal-test run original task'
CONSUMED = '❯ ' + TEXT + '\n⏺ Read original task\n' + IDLE


class TaskDispatchJournalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / 'task-pack.json'
        self.pack = dict(task_id='task-journal-test', executor_uuid='EXECUTOR')
        self.path.write_text(json.dumps(self.pack))
        self.proof = dict(workspace_uuid='WS', caller_surface_uuid='SUPERVISOR',
                          target_surface_uuid='EXECUTOR', target_pane_uuid='PANE')
        for item in (patch.object(Path, 'home', return_value=self.home),
                     patch.object(b, 'validate_task_pack_contract', return_value=self.pack),
                     patch('availability_contract.require_action'),
                     patch.object(b, 'pin_workspace', return_value=self.proof),
                     patch.object(b.time, 'sleep')):
            item.start()
            self.addCleanup(item.stop)
        self.native = LegacyTerminalFixture.attach(self, home=self.home, identity=self.proof, provider='claude')

    def call(self, **kw):
        return b.submit_task_pack('peer', TEXT, str(self.path), **kw)

    def uncertain(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(TEXT, 'claude'), '⏺ Read unrelated\n'+IDLE)), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                self.call()
            send.assert_called_once()
            key.assert_called_once()

    def test_confirmed_dispatch_is_not_sent_twice(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(TEXT, 'claude'), CONSUMED)), \
                patch.object(b, 'send_text') as send, \
                patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(TEXT)) as key:
            result = self.call()
            self.assertTrue(Path(result['receipt']).is_file())
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_called_once()
            key.assert_called_once()

    def hook_proof(self):
        return guard._attempt_evidence(dict(kind='task', surface='peer',
            text=TEXT, pack=str(self.path)), 'peer', b)

    def test_post_hook_reads_new_journal_without_input(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(TEXT, 'claude'), CONSUMED)), \
                patch.object(b, 'send_text'), patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(TEXT)):
            self.call()
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            self.assertEqual(self.hook_proof()['source'], 'revalidated_task_dispatch_v1')
            send.assert_not_called()
            key.assert_not_called()

    def test_post_hook_accepts_original_readonly_reconciliation(self):
        self.uncertain()
        self.native.append_user(TEXT)
        with patch.object(b, 'read_screen', return_value=CONSUMED), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            self.call(reconcile_only=True)
            self.assertIsNotNone(self.hook_proof())
            send.assert_not_called()
            key.assert_not_called()

    def test_post_hook_rejects_tampered_observation_and_pack(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(TEXT, 'claude'), CONSUMED)), \
                patch.object(b, 'send_text'), patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(TEXT)):
            result = self.call()
        attempt_path = Path(result['attempt'])
        original = attempt_path.read_bytes()
        data = json.loads(original)
        data['events'][0]['screen'] = 'unrelated original intent'
        attempt_path.write_text(json.dumps(data))
        self.assertIsNone(self.hook_proof())
        attempt_path.write_bytes(original)
        self.path.write_text('changed task pack')
        self.assertIsNone(self.hook_proof())

    def test_post_hook_requires_receipt_not_only_confirmation(self):
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(TEXT, 'claude'), CONSUMED)), \
                patch.object(b, 'send_text'), patch.object(b, 'send_key', side_effect=self.native.receipt_on_key(TEXT)):
            result = self.call()
        Path(result['receipt']).unlink()
        self.assertIsNone(self.hook_proof())

    def test_restart_after_uncertainty_is_read_only(self):
        self.uncertain()
        self.native.append_user(TEXT)
        with patch.object(b, 'read_screen', return_value=CONSUMED), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            self.assertTrue(self.call(reconcile_only=True)['reconciled_read_only'])
            send.assert_not_called()
            key.assert_not_called()

    def test_partial_cross_block_and_queue_never_reconcile(self):
        self.uncertain()
        self.native.append_user('task-journal-test')
        self.native.append_user(TEXT + ' extra')
        self.native.append_queued(TEXT)
        for screen in ['❯ task-journal-test\n⏺ Read original task\n'+IDLE,
                       '❯ '+TEXT+'\n❯ other task\n⏺ Read original task\n'+IDLE,
                       'Messages to be submitted after next tool call\n'+TEXT+'\n'+IDLE]:
            with self.subTest(screen=screen), patch.object(b, 'read_screen', return_value=screen), \
                    patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                with self.assertRaises(b.DispatchUnconfirmed):
                    self.call(reconcile_only=True)
                send.assert_not_called()
                key.assert_not_called()

    def test_crash_after_paste_intent_never_repastes(self):
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text', side_effect=RuntimeError('ACK lost')) as send, \
                patch.object(b, 'send_key') as key:
            with self.assertRaises(RuntimeError):
                self.call()
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_called_once()
            key.assert_not_called()
        attempts = list(self.home.rglob('attempt-*.json'))
        self.assertEqual(json.loads(attempts[0].read_text())['phase'], 'PASTE_INTENT')

    def test_changed_prompt_cannot_create_fresh_attempt(self):
        self.uncertain()
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                b.submit_task_pack('peer', TEXT+' changed', str(self.path))
            send.assert_not_called()
            key.assert_not_called()

    def test_pack_change_between_paste_and_enter_stops_key(self):
        def mutate(*args):
            self.path.write_text('changed after paste')
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text', side_effect=mutate) as send, \
                patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_called_once()
            key.assert_not_called()

    def test_target_change_before_paste_stops_all_input(self):
        changed = dict(self.proof, target_pane_uuid='DIFFERENT')
        with patch.object(b, 'pin_workspace', side_effect=[self.proof, changed]), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_not_called()
            key.assert_not_called()

    def test_no_input_retry_is_bounded(self):
        with patch.object(b, 'read_screen', return_value='host ~ %'), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            for _ in range(2):
                with self.assertRaises(b.DispatchUnconfirmed):
                    self.call()
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_not_called()
            key.assert_not_called()

    def test_no_input_writer_pins_original_delivery_lock(self):
        with patch.object(b, 'read_screen', return_value='host ~ %'), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.DispatchUnconfirmed):
                self.call()
            attempt_path = next(self.home.rglob('attempt-*.json'))
            attempt = json.loads(attempt_path.read_text())
            lock = attempt_path.parent / 'delivery.lock'
            info = lock.stat()
            self.assertEqual(attempt['delivery_lock_identity'],
                             dict(device=info.st_dev, inode=info.st_ino))
            lock.rename(lock.with_name('original-lock'))
            lock.touch()
            with self.assertRaisesRegex(b.TaskPackContractError, 'ORIGINAL_LOCK_CHANGED'):
                self.call()
            send.assert_not_called()
            key.assert_not_called()

    def test_lock_replacement_after_paste_stops_enter(self):
        def replace(*args):
            attempt_path = next(self.home.rglob('attempt-*.json'))
            lock = attempt_path.parent / 'delivery.lock'
            lock.rename(lock.with_name('original-lock'))
            lock.touch()
        with patch.object(b, 'read_screen', return_value=IDLE), \
                patch.object(b, 'send_text', side_effect=replace) as send, \
                patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.TaskPackContractError, 'ORIGINAL_LOCK_CHANGED'):
                self.call()
            send.assert_called_once()
            key.assert_not_called()

    def test_missing_attempt_cannot_be_reconciled(self):
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                self.call(reconcile_only=True)
            send.assert_not_called()
            key.assert_not_called()

    def test_original_delivery10_attempt_requires_original_controller(self):
        key = j.digest(json.dumps(['SUPERVISOR', self.pack['task_id']], sort_keys=True))
        old = self.home / '.local/state/multi-agent-collaboration/deliveries-v1' / (key+'.json')
        old.parent.mkdir(parents=True)
        old.write_text('truncated')
        with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaises(b.TaskPackContractError):
                self.call()
            send.assert_not_called()
            key.assert_not_called()

    def test_target_lock_prevents_concurrent_different_task(self):
        root = self.home / '.local/state/multi-agent-collaboration/task-dispatch-v1'
        root.mkdir(parents=True)
        with (root / ('target-'+j.digest('EXECUTOR')+'.lock')).open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                with self.assertRaises(b.TaskPackContractError):
                    self.call()
                send.assert_not_called()
                key.assert_not_called()

    def test_external_sender_holding_shared_pane_or_target_defers_without_input(self):
        root = self.home / '.local/state/multi-agent-collaboration/deliveries-v1'
        root.mkdir(parents=True)
        for name in ('pane-' + j.digest('WS:PANE'), 'target-' + j.digest('EXECUTOR')):
            with self.subTest(lock=name), (root / (name + '.lock')).open('a+b') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
                    with self.assertRaisesRegex(b.TaskPackContractError, 'DISPATCH_IN_PROGRESS'):
                        self.call()
                    send.assert_not_called()
                    key.assert_not_called()
                    self.assertEqual(list(self.home.rglob('attempt-*.json')), [])

    def test_foreign_continuation_after_paste_never_authorizes_enter_or_replay(self):
        foreign = '任务中断了么？如果是就请继续。'
        mixed = self.native.draft(TEXT + foreign, 'claude')
        screens = iter([IDLE])
        with patch.object(b, 'read_screen', side_effect=lambda *a, **k: next(screens, mixed)), \
                patch.object(b, 'send_text') as send, patch.object(b, 'send_key') as key:
            with self.assertRaisesRegex(b.DispatchUnconfirmed, 'DRAFT_NOT_READY'):
                self.call()
            send.assert_called_once()
            key.assert_not_called()
            attempt = next(self.home.rglob('attempt-*.json'))
            original = attempt.read_bytes()
            # An external sender may later submit the mixed draft. It is real
            # native input, but never an exact receipt for the original notice.
            self.native.append_user(TEXT + foreign)
            with self.assertRaisesRegex(b.DispatchUnconfirmed, 'DISPATCH_NOT_YET_CONFIRMED'):
                self.call(reconcile_only=True)
            with self.assertRaisesRegex(b.TaskPackContractError, 'DISPATCH_ATTEMPT_EXISTS'):
                self.call()
            self.assertEqual(attempt.read_bytes(), original)
            self.assertFalse((attempt.parent / 'receipt.json').exists())
            send.assert_called_once()
            key.assert_not_called()
