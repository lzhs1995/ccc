"""旧核收入口退役、只读原生行解析与原子回执发布的离线回归。"""
import contextlib
import fcntl
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cmux_bridge as b
import delivery_receipts as D
from cmux_delivery_evidence import digest


class _ReceiptCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        sessions = self.root / '.codex/sessions'
        sessions.mkdir(parents=True)
        self.log = sessions / 'test.jsonl'
        self.report = self.root / 'report.md'
        self.report.write_text('review\n')
        self.gate = self.root / 'gate.json'
        self.gate.write_text('{}')
        self.path = self.root / 'task-pack.json'
        self.pack = dict(
            task_id='task-test', completion_nonce='nonce-123',
            report=str(self.report), identity_gate=str(self.gate),
            completion_receipt=str(self.root / 'receipt.json'),
            completion_callback='DONE|task-test|nonce-123|REPORT=' + str(self.report),
            callback_target='surface:1', finalized_at='2026-10-04T10:00:00Z')
        self.path.write_text(json.dumps(self.pack))
        self.event = dict(
            type='response_item', timestamp='2026-10-04T10:01:00Z',
            payload=dict(type='message', role='user', content=[
                dict(type='input_text', text=self.pack['completion_callback'])]))
        self.write_log()
        self.bridge = mock.Mock()
        for patcher in (
                mock.patch.object(Path, 'home', return_value=self.root),
                mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'native-session'})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_log(self, session='native-session'):
        self.log.write_text(
            json.dumps(dict(type='session_meta', payload=dict(id=session))) +
            '\n' + json.dumps(self.event) + '\n')

    def files(self):
        result = {}
        for path in sorted(self.root.rglob('*')):
            stat = path.lstat()
            if path.is_dir():
                value = ('directory', stat.st_dev, stat.st_ino, stat.st_mode)
            else:
                value = ('file', stat.st_dev, stat.st_ino, stat.st_mode,
                         stat.st_mtime_ns, stat.st_ctime_ns, path.read_bytes())
            result[str(path.relative_to(self.root))] = value
        return result

    def ack(self):
        return D.acknowledge_received_callback(
            self.path, self.bridge, transcript=self.log, line=2,
            received_callback=self.pack['completion_callback'])

    def assert_retired(self, action=None):
        before = self.files()
        with self.assertRaisesRegex(D.ReceiptError, 'original controller --reconcile-only'):
            (action or self.ack)()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.bridge.mock_calls, [])

    def prepare_legacy_attempt(self):
        # 旧 durable 形状仅作拒绝夹具；不伪造 native binding 或 paste fence。
        root = self.root / '.local/state/multi-agent-collaboration/deliveries-v1'
        root.mkdir(parents=True, exist_ok=True)
        key = digest(json.dumps(['executor', self.pack['completion_nonce']], sort_keys=True))
        self.attempt_path = root / (key + '.json')
        self.attempt = dict(
            version=1, marker=self.pack['completion_nonce'],
            identity=dict(workspace_uuid='ws', caller_surface_uuid='executor',
                          target_surface_uuid='supervisor', target_pane_uuid='pane'),
            payload_sha256=digest(self.pack['completion_callback']), paste_intent=True,
            enter_attempts=2, queue_key_attempted=True,
            binding={str(self.path): D.snapshot(self.path)['sha256'],
                     str(self.report): D.snapshot(self.report)['sha256']})
        self.attempt_path.write_text(json.dumps(self.attempt))
        self.lock_path = self.attempt_path.with_suffix('.lock')
        self.lock_path.write_bytes(b'original sender lock')


class RetiredAcknowledgementTests(_ReceiptCase):
    def test_exact_user_record_without_attempt_cannot_create_receipt_or_lock(self):
        self.assert_retired()
        self.assertFalse(Path(self.pack['completion_receipt']).exists())
        self.assertFalse((self.root / '.local').exists())

    def test_valid_legacy_durable_attempt_cannot_create_receipt(self):
        self.prepare_legacy_attempt()
        self.assert_retired()
        self.assertFalse(Path(self.pack['completion_receipt']).exists())

    def test_repeated_calls_never_reset_sender_budget_or_create_attempt(self):
        self.prepare_legacy_attempt()
        before = self.files()
        self.assert_retired()
        self.assert_retired()
        self.assertEqual(self.files(), before)
        self.assertEqual(json.loads(self.attempt_path.read_text())['enter_attempts'], 2)
        self.assertTrue(json.loads(self.attempt_path.read_text())['queue_key_attempted'])

    def test_active_original_lock_stays_unchanged_and_is_not_replaced(self):
        self.prepare_legacy_attempt()
        with self.lock_path.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_retired()
        self.assert_retired()

    def test_existing_journal_pending_and_attempt_files_are_preserved(self):
        receipt = Path(self.pack['completion_receipt'])
        journal = receipt.with_name(receipt.stem + '-attempts')
        journal.mkdir()
        (journal / 'attempt-0001.json').write_text('preserve original journal')
        (journal / 'delivery.lock').write_bytes(b'original callback lock')
        Path(str(receipt) + '.pending.json').write_text('preserve original pending')
        Path(str(receipt) + '.attempt.json').write_text('preserve original attempt')
        self.assert_retired()
        self.assertFalse(receipt.exists())

    def test_existing_foreign_receipt_is_preserved(self):
        receipt = Path(self.pack['completion_receipt'])
        receipt.write_text('{}')
        self.assert_retired()
        self.assertEqual(receipt.read_text(), '{}')

    def test_former_legacy_receipt_does_not_make_retired_api_idempotently_successful(self):
        self.prepare_legacy_attempt()
        receipt = Path(self.pack['completion_receipt'])
        receipt.write_text(json.dumps(dict(
            confirmed=True, confirmation_source='supervisor_native_user_record',
            completion_callback=self.pack['completion_callback'],
            evidence=dict(legacy_without_attempt=True))))
        self.assert_retired()
        self.assert_retired()

    def test_retirement_precedes_missing_task_pack_lookup(self):
        self.path.rename(self.path.with_suffix('.original'))
        self.assert_retired()

    def test_retirement_precedes_receiver_session_lookup(self):
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': ''}):
            self.assert_retired()

    def test_public_cli_refuses_without_receipt_stdout_or_terminal_input(self):
        args = [
            '--task-pack', str(self.path), '--transcript', str(self.log),
            '--line', '2', '--received-callback', self.pack['completion_callback']]
        output = io.StringIO()
        with mock.patch.object(b, 'send_text') as send, \
                mock.patch.object(b, 'send_key') as key, \
                mock.patch.object(b, 'read_screen') as screen, \
                contextlib.redirect_stdout(output):
            self.assert_retired(lambda: D.main(args))
        self.assertEqual(output.getvalue(), '')
        send.assert_not_called()
        key.assert_not_called()
        screen.assert_not_called()


class NativeRecordTests(_ReceiptCase):
    def record(self, *, line=2):
        return D.native_user_record(
            self.log, line, session_id='native-session',
            exact_text=self.pack['completion_callback'], after=self.pack['finalized_at'])

    def assert_record_rejected(self, *, line=2):
        before = self.files()
        with self.assertRaises(D.ReceiptError):
            self.record(line=line)
        self.assertEqual(self.files(), before)

    def test_exact_native_user_row_only_returns_read_only_evidence(self):
        before = self.files()
        evidence = self.record()
        self.assertEqual(evidence['path'], str(self.log))
        self.assertEqual(evidence['line'], 2)
        self.assertEqual(evidence['record'], self.event)
        self.assertEqual(self.files(), before)

    def test_assistant_or_tool_echo_is_not_native_user_evidence(self):
        for role in ('assistant', 'tool'):
            with self.subTest(role=role):
                self.event['payload']['role'] = role
                self.write_log()
                self.assert_record_rejected()

    def test_queued_event_is_not_native_user_evidence(self):
        self.event['type'] = 'event_msg'
        self.write_log()
        self.assert_record_rejected()

    def test_quoted_callback_is_not_exact_evidence(self):
        self.event['payload']['content'][0]['text'] += '\nquoted earlier callback'
        self.write_log()
        self.assert_record_rejected()

    def test_wrong_native_session_refused(self):
        self.write_log('other-session')
        self.assert_record_rejected()

    def test_record_before_finalization_refused(self):
        self.event['timestamp'] = '2026-10-03T10:00:00Z'
        self.write_log()
        self.assert_record_rejected()

    def test_incomplete_native_row_refused(self):
        self.log.write_bytes(self.log.read_bytes().rstrip(b'\n'))
        self.assert_record_rejected()

    def test_invalid_line_number_refused(self):
        for line in (True, 1, 0, -1):
            with self.subTest(line=line):
                self.assert_record_rejected(line=line)


class AtomicPublicationTests(_ReceiptCase):
    def test_partial_write_never_publishes_or_leaves_temporary_file(self):
        path = self.root / 'new.json'
        before = self.files()
        with mock.patch.object(D.os, 'fsync', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                D.publish(path, {'one': 1})
        self.assertFalse(path.exists())
        self.assertEqual(self.files(), before)

    def test_publish_exclusive_preserves_existing_receipt_bytes(self):
        path = self.root / 'new.json'
        D.publish(path, {'one': 1})
        before = self.files()
        with self.assertRaises(FileExistsError):
            D.publish(path, {'two': 2})
        self.assertEqual(self.files(), before)
        self.assertEqual(json.loads(path.read_text()), {'one': 1})

    def test_successful_publication_exposes_one_complete_json_file(self):
        path = self.root / 'new.json'
        D.publish(path, {'one': 1, 'message': '完整回执'})
        self.assertEqual(json.loads(path.read_text()), {'one': 1, 'message': '完整回执'})
        self.assertEqual(list(self.root.glob('.new.json-*')), [])


if __name__ == '__main__':
    unittest.main()
