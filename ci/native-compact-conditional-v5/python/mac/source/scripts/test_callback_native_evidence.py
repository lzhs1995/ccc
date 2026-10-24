"""原 receiver 核收回归：原 attempt 来自真实 submit，不追补 native 证据。"""
import copy
import fcntl
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cmux_bridge as b
from callback_native_evidence import validate, reconcile_received
from cmux_callback_journal import verified_receipt
from cmux_native_delivery import NativeDeliveryError
from delivery_receipts import ReceiptError
from native_test_support import LegacyTerminalFixture


IDLE = '› Ask Codex to do anything\nGPT-6-Astra high\n? for shortcuts'


class _NativeCallbackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.packfile = self.root / 'pack.json'
        self.report = self.root / 'report.md'
        self.report.write_text('frozen report')
        self.gatefile = self.root / 'gate.json'
        self.gate = dict(status='PASS', task_id='task', supervisor='surface:1',
                         supervisor_surface_uuid='SUP', workspace_uuid='WS',
                         executor='surface:2', executor_surface_uuid='EXEC')
        self.write(self.gatefile, self.gate)
        self.pack = dict(
            draft=False, task_id='task', completion_nonce='nonce12345',
            callback_target='surface:1', executor='surface:2', executor_uuid='EXEC',
            completion_callback='DONE|task|nonce12345|REPORT=' + str(self.report),
            completion_receipt=str(self.root / 'receipt.json'), report=str(self.report),
            identity_gate=str(self.gatefile), finalized_at='2026-01-01T00:00:00+00:00',
            required_skill=str(b.COLLABORATION_SKILL_PATH),
            completion_delivery=dict(transport='cmux_bridge.submit_completion_callback',
                                     require_confirmed=True),
            availability_state=str(self.root / 'availability.json'))
        self.write(self.packfile, self.pack)
        self.receipt = Path(self.pack['completion_receipt'])
        self.original_identity = dict(
            caller_surface_uuid='EXEC', target_surface_uuid='SUP',
            workspace_uuid='WS', target_pane_uuid='PANE')
        self.native = LegacyTerminalFixture.attach(self, home=self.root,
                                           identity=copy.deepcopy(self.original_identity))
        # 在原绑定产生之前选定 receiver 的真实原生目录；之后不改 binding/fence。
        self.native.native_root = self.root / '.codex/sessions'
        self.native.native_root.mkdir(parents=True)
        destination = self.native.native_root / self.native.transcript.name
        self.native.transcript.rename(destination)
        self.native.transcript = destination
        self.transcript = destination
        self.meta = destination.read_bytes()
        self.native.pin.side_effect = lambda *args, **kwargs: copy.deepcopy(self.native.identity)
        self.journal = self.receipt.with_name(self.receipt.stem + '-attempts')

        # task-pack、availability、native 绑定、PASTE fence、journal 都走真实实现。
        with patch.object(b, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(self.pack['completion_callback']), IDLE)):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_completion_callback(self.packfile)
        self.native.send.assert_called_once_with('surface:1', self.pack['completion_callback'])
        self.native.key.assert_called_once_with('surface:1', 'enter')
        self.assertFalse(self.receipt.exists())
        self.assertEqual(len(list(self.journal.glob('attempt-*.json'))), 1)
        self.attemptfile = self.journal / 'attempt-0001.json'
        self.attempt = json.loads(self.attemptfile.read_text())
        self.lockfile = self.journal / 'delivery.lock'
        pastes = [e for e in self.attempt['events'] if e['phase'] == 'PASTE_INTENT']
        self.assertEqual(len(pastes), 1)
        self.assertIn('native_paste_fence', pastes[0])
        self.assertIn('native_binding', self.attempt)

        # 首次提交没有 user 入站；核收用例必须显式追加完整 user 记录。
        self.native.append_user(self.pack['completion_callback'])
        self.record = json.loads(self.transcript.read_bytes().splitlines()[1])
        self.native.send.reset_mock()
        self.native.key.reset_mock()
        guard = patch.object(b, 'read_screen',
                             side_effect=AssertionError('native settlement must not read screen'))
        self.screen = guard.start()
        self.addCleanup(guard.stop)
        env = patch.dict(os.environ, {'CODEX_THREAD_ID': self.native.session_id})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def transcribe(self):
        # 仅修改待校验的原生记录；保留原 inode、meta 和原 fence 之前的字节。
        self.transcript.write_bytes(self.meta + json.dumps(self.record).encode() + b'\n')

    def files(self, excluding=()):
        excluded = {Path(p) for p in excluding}
        result = {}
        for path in sorted(self.root.rglob('*')):
            if path in excluded:
                continue
            stat = path.lstat()
            if path.is_dir():
                value = ('directory', stat.st_dev, stat.st_ino, stat.st_mode)
            else:
                value = ('file', stat.st_dev, stat.st_ino, stat.st_mode,
                         stat.st_mtime_ns, stat.st_ctime_ns, path.read_bytes())
            result[str(path.relative_to(self.root))] = value
        return result

    def assert_no_input(self):
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()
        self.screen.assert_not_called()

    def check(self, *, line=2, session_id=None):
        return validate(self.packfile, self.attemptfile, self.transcript, line,
                        session_id or self.native.session_id)

    def assert_rejected(self, action=None, error=(ReceiptError, NativeDeliveryError)):
        before = self.files()
        with self.assertRaises(error):
            (action or self.check)()
        self.assertEqual(self.files(), before)
        self.assert_no_input()

    def omit_binding(self):
        self.attempt.pop('native_binding')
        self.write(self.attemptfile, self.attempt)

    def omit_fence(self):
        paste = next(e for e in self.attempt['events'] if e['phase'] == 'PASTE_INTENT')
        paste.pop('native_paste_fence')
        self.write(self.attemptfile, self.attempt)

    def write_legacy_attempt(self):
        # 故意保留旧人工 journal 形状作为负例，绝不伪造原 binding/fence。
        self.write(self.attemptfile, dict(binding=self.attempt['binding'], events=[
            dict(phase='PASTE_INTENT', screen='idle',
                 screen_sha256=hashlib.sha256(b'idle').hexdigest()[:16]),
            dict(phase='ENTER_INTENT', at_epoch=1767225601)]))


class NativeEvidenceTests(_NativeCallbackCase):
    def test_exact_native_record_requires_original_fence_and_creates_no_receipt(self):
        before = self.files()
        evidence = self.check()
        self.assertEqual(evidence['status'], 'EXACT_NATIVE_RECEPTION')
        self.assertEqual(evidence['native_proof']['session_id'], self.native.session_id)
        self.assertEqual(evidence['native_proof']['path'], str(self.transcript))
        self.assertEqual(evidence['native_proof']['sha256'],
                         hashlib.sha256(self.transcript.read_bytes().splitlines(keepends=True)[1]).hexdigest())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.files(), before)
        self.assert_no_input()

    def test_quote_or_tool_output_is_not_reception(self):
        for role in ('assistant', 'tool'):
            with self.subTest(role=role):
                self.record['payload']['role'] = role
                self.transcribe()
                self.assert_rejected()

    def test_truncated_or_extended_payload_is_not_exact(self):
        for text in ('DONE|task|nonce12345|…', self.pack['completion_callback'] + ' extra'):
            with self.subTest(text=text):
                self.record['payload']['content'][0]['text'] = text
                self.transcribe()
                self.assert_rejected()

    def test_record_after_finalization_but_before_original_paste_is_rejected(self):
        self.record['timestamp'] = '2026-01-01T00:00:02Z'
        self.transcribe()
        self.assert_rejected()

    def test_changed_report_rejected(self):
        self.report.write_text('changed report')
        self.assert_rejected()

    def test_changed_pack_rejected(self):
        self.packfile.write_text(self.packfile.read_text() + ' ')
        self.assert_rejected()

    def test_wrong_original_sender_rejected(self):
        self.attempt['binding']['identity']['caller_surface_uuid'] = 'OTHER'
        self.write(self.attemptfile, self.attempt)
        self.assert_rejected()

    def test_no_enter_or_duplicate_paste_rejected(self):
        original = copy.deepcopy(self.attempt['events'])
        paste = next(e for e in original if e['phase'] == 'PASTE_INTENT')
        for events in ([e for e in original if e['phase'] != 'ENTER_INTENT'],
                       original + [copy.deepcopy(paste)]):
            with self.subTest(phases=[e['phase'] for e in events]):
                self.attempt['events'] = events
                self.write(self.attemptfile, self.attempt)
                self.assert_rejected()

    def test_newer_attempt_rejected(self):
        self.write(self.attemptfile.with_name('attempt-0002.json'), self.attempt)
        self.assert_rejected()

    def test_missing_original_binding_rejected_without_backfill(self):
        self.omit_binding()
        self.assert_rejected()

    def test_missing_original_fence_rejected_without_backfill(self):
        self.omit_fence()
        self.assert_rejected()

    def test_missing_original_paste_time_rejected_without_backfill(self):
        paste = next(e for e in self.attempt['events'] if e['phase'] == 'PASTE_INTENT')
        paste.pop('at_epoch')
        self.write(self.attemptfile, self.attempt)
        self.assert_rejected()

    def test_manual_legacy_journal_cannot_gain_native_confirmation(self):
        self.write_legacy_attempt()
        self.assert_rejected()

    def test_wrong_session_rejected(self):
        self.assert_rejected(lambda: self.check(session_id='other-session'))

    def test_other_transcript_with_identical_content_rejected(self):
        other = self.transcript.with_name('copy-' + self.transcript.name)
        other.write_bytes(self.transcript.read_bytes())
        self.transcript = other
        self.assert_rejected()

    def test_matching_later_user_does_not_replace_the_explicit_line(self):
        self.record['payload']['role'] = 'assistant'
        self.transcribe()
        self.native.append_user(self.pack['completion_callback'])
        self.assert_rejected()
        self.assertEqual(self.check(line=3)['status'], 'EXACT_NATIVE_RECEPTION')
        self.assert_no_input()

    def test_replaced_transcript_inode_rejected(self):
        data = self.transcript.read_bytes()
        self.transcript.rename(self.transcript.with_suffix('.original'))
        self.transcript.write_bytes(data)
        self.assert_rejected()


class NativeSettlementTests(_NativeCallbackCase):
    def setUp(self):
        super().setUp()
        self.live = dict(caller_surface_uuid='SUP', target_surface_uuid='EXEC',
                         workspace_uuid='WS', caller_pane_uuid='PANE',
                         target_pane_uuid='EXEC-PANE')
        self.native.identity = self.live

    def settle(self):
        return reconcile_received(self.packfile, b, transcript=self.transcript, line=2)

    def read_receipt(self):
        before = self.files()
        result = verified_receipt(b, 'surface:1', self.packfile)
        self.assertEqual(self.files(), before)
        self.assert_no_input()
        return result

    def test_publish_preserves_original_attempt_lock_and_send_budget(self):
        before = self.files()
        receipt = self.settle()
        self.assertTrue(receipt['confirmed'])
        self.assertEqual(receipt['confirmation_source'], 'native_user_message_v1')
        self.assertEqual(receipt['input_operations'], 0)
        self.assertEqual(receipt['native_binding'], self.attempt['native_binding'])
        self.assertEqual(receipt['native_proof']['session_id'], self.native.session_id)
        self.assertEqual(self.files(excluding=(self.receipt,)), before)
        self.assert_no_input()

    def test_active_sender_lock_rejects(self):
        with self.lockfile.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_rejected(self.settle)
        self.assertFalse(self.receipt.exists())

    def test_wrong_receiver_rejects(self):
        self.live['caller_surface_uuid'] = 'OTHER'
        self.assert_rejected(self.settle)

    def test_replaced_lock_during_validation_rejects_without_receipt(self):
        original_attempt = self.attemptfile.read_bytes()
        original_inode = self.lockfile.stat().st_ino
        retired = self.lockfile.with_suffix('.original')
        real_open = os.open
        replaced = []

        def replace_lock(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            if Path(path) == self.transcript and not replaced:
                # 只在原生文件 I/O 边界注入锁替换；proof/validate 均运行真实代码。
                self.lockfile.rename(retired)
                new_fd = real_open(self.lockfile, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(new_fd)
                replaced.append(True)
            return descriptor

        with patch.object(os, 'open', side_effect=replace_lock):
            with self.assertRaisesRegex(ReceiptError, 'lock was replaced'):
                self.settle()
        self.assertEqual(replaced, [True])
        self.assertEqual(retired.stat().st_ino, original_inode)
        self.assertEqual(self.attemptfile.read_bytes(), original_attempt)
        self.assertFalse(self.receipt.exists())
        self.assert_no_input()

    def test_duplicate_settlement_preserves_receipt(self):
        self.settle()
        self.assert_rejected(self.settle)

    def test_wrong_session_rejects(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'other'}):
            self.assert_rejected(self.settle)

    def test_missing_original_lock_never_creates_one(self):
        self.lockfile.unlink()
        self.assert_rejected(self.settle, error=FileNotFoundError)

    def test_missing_original_binding_never_publishes_or_backfills(self):
        self.omit_binding()
        self.assert_rejected(self.settle)

    def test_missing_original_fence_never_publishes_or_backfills(self):
        self.omit_fence()
        self.assert_rejected(self.settle)

    def test_manual_legacy_journal_never_publishes_or_backfills(self):
        self.write_legacy_attempt()
        self.assert_rejected(self.settle)

    def test_reader_accepts_unified_native_receipt_without_screen_or_input(self):
        self.settle()
        self.assertEqual(self.read_receipt()['source'], 'revalidated_callback_journal')

    def test_reader_rejects_changed_native_record(self):
        self.settle()
        self.record['payload']['role'] = 'assistant'
        self.transcribe()
        self.assertIsNone(self.read_receipt())

    def test_reader_rejects_changed_report(self):
        self.settle()
        self.report.write_text('new report')
        self.assertIsNone(self.read_receipt())

    def test_reader_rejects_receipt_identity_or_evidence_tampering(self):
        self.settle()
        original = json.loads(self.receipt.read_text())
        for field in ('receiver', 'hash', 'input', 'proof', 'binding'):
            with self.subTest(field=field):
                value = copy.deepcopy(original)
                if field == 'receiver':
                    value['receiver_identity']['caller_surface_uuid'] = 'OTHER'
                elif field == 'hash':
                    value['native_evidence']['native']['record_sha256'] = '0' * 64
                elif field == 'input':
                    value['input_operations'] = True
                elif field == 'proof':
                    value['native_proof']['sha256'] = '0' * 64
                else:
                    value['native_binding']['session_id'] = 'other'
                self.write(self.receipt, value)
                self.assertIsNone(self.read_receipt())

    def test_reader_rejects_old_confirmation_sources_even_with_new_proof(self):
        self.settle()
        original = json.loads(self.receipt.read_text())
        for source in ('original_journal_native_user_record', 'supervisor_native_user_record'):
            with self.subTest(source=source):
                value = dict(original, confirmation_source=source)
                self.write(self.receipt, value)
                self.assertIsNone(self.read_receipt())

    def test_reader_rejects_removed_original_fence(self):
        self.settle()
        self.omit_fence()
        self.assertIsNone(self.read_receipt())

    def test_reader_accepts_unrelated_later_native_records(self):
        self.settle()
        self.native.append_user('unrelated later message')
        self.assertIsNotNone(self.read_receipt())


if __name__ == '__main__':
    unittest.main()
