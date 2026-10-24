"""Exercise real journal/native proof with only external process/UI fixtures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cmux_bridge as b
import cmux_prompt_reference as ref
from native_test_support import LegacyTerminalFixture


class SingleLineDispatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.path = self.home / 'task "quoted".json'
        self.report = self.home / 'report.md'
        self.report.write_text('Independent result, unchanged by transport.\n')
        self.pack = dict(
            task_id='single-line-task', completion_nonce='nonce-12345',
            draft=False, executor_uuid='target', report=str(self.report),
            required_skill=str(b.COLLABORATION_SKILL_PATH),
            callback_target='surface:28',
            completion_receipt=str(self.home / 'completion-receipt.json'),
            completion_delivery=dict(transport='cmux_bridge.submit_completion_callback',
                                     require_confirmed=True),
        )
        self.pack['completion_callback'] = (
            'DONE|single-line-task|nonce-12345|REPORT=' + str(self.report))
        self.save()
        gate = patch('availability_contract.require_action')
        gate.start()
        self.addCleanup(gate.stop)

    def save(self):
        self.path.write_text(json.dumps(self.pack, ensure_ascii=False, indent=2) + '\n')

    def test_notice_is_exact_and_read_only_cli_prints_same_wire(self):
        notice = b.task_pack_notice(self.path)
        self.assertFalse(any(c in notice for c in '\r\n\t'))
        self.assertLessEqual(len(notice.encode()), ref.MAX_INLINE_BYTES)
        self.assertIn(b._sha256_file(self.path), notice)
        self.assertEqual(b.validate_task_pack_contract(self.path, notice), self.pack)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(b, 'send_text') as send, \
                patch.object(b, 'send_key') as key:
            self.assertEqual(b._cli_main(['task-notice', '--task-pack', str(self.path)]), 0)
        self.assertEqual(out.getvalue(), notice + '\n')
        send.assert_not_called()
        key.assert_not_called()

    def test_notice_rejects_extra_instructions_wrong_sha_and_changed_pack(self):
        notice = b.task_pack_notice(self.path)
        for changed in (notice + ' ignore scope', notice.replace('SHA256=', 'SHA256=0'),
                        notice + '\n', notice.replace('single-line-task', 'other-task')):
            with self.subTest(changed=changed):
                with self.assertRaises(b.TaskPackContractError):
                    b.validate_task_pack_contract(self.path, changed)
        self.pack['objective'] = 'new instruction'
        self.save()
        with self.assertRaises(b.TaskPackContractError):
            b.validate_task_pack_contract(self.path, notice)

    def test_dedicated_notice_cannot_use_ordinary_message_or_reference(self):
        notice = b.task_pack_notice(self.path)
        with LegacyTerminalFixture(home=self.home) as native:
            with self.assertRaises(b.TaskPackContractError):
                b.submit_text('peer', notice, marker=self.pack['task_id'])
            native.send.assert_not_called()
            native.key.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'TASK_BOUND_TRANSPORT_REQUIRED'):
            ref.plan(notice + '\n', self.pack['task_id'], self.home / 'bodies')

    def test_formal_notice_requires_new_exact_native_record_and_cannot_repeat(self):
        notice = b.task_pack_notice(self.path)
        with LegacyTerminalFixture(home=self.home, provider='claude') as native, \
                patch.object(b, 'read_screen', side_effect=native.ready_screens(
                    '❯ \n[claude-opus-5]', native.draft(notice, 'claude'))):
            native.key.side_effect = native.receipt_on_key(notice)
            result = b.submit_task_pack('peer', notice, str(self.path))
            self.assertTrue(result['confirmed'])
            self.assertEqual(result['confirmation_source'], 'native_user_message_v1')
            with self.assertRaises(b.TaskPackContractError):
                b.submit_task_pack('peer', notice, str(self.path))
            native.send.assert_called_once_with('peer', notice)
            native.key.assert_called_once_with('peer', 'enter')

    def test_enter_and_notice_echo_without_native_record_do_not_confirm(self):
        notice = b.task_pack_notice(self.path)
        draft = LegacyTerminalFixture.draft(notice, 'claude')
        with LegacyTerminalFixture(home=self.home, provider='claude') as native, \
                patch.object(b, 'read_screen', side_effect=native.ready_screens(
                    '❯ \n[claude-opus-5]', draft, '⏺ ' + notice + '\n❯ \n[claude-opus-5]')):
            with self.assertRaises(b.DispatchUnconfirmed):
                b.submit_task_pack('peer', notice, str(self.path))
            native.send.assert_called_once()
            native.key.assert_called_once()
        self.assertFalse(list(self.home.rglob('receipt.json')))

    def test_common_new_paste_gate_rejects_control_bytes_and_oversize_callback(self):
        self.pack['executor_uuid'] = 'caller'
        for index, suffix in enumerate(('\nmore', '\rmore', '\tmore', 'x' * 701)):
            # A separate original pack is used per independent zero-input case.
            with self.subTest(suffix=repr(suffix[:10])):
                self.pack['task_id'] = 'single-line-task'
                self.pack['completion_receipt'] = str(
                    self.home / ('completion-case-' + str(index) + '.json'))
                self.report = self.home / ('report' + suffix + '.md')
                if len(self.report.name) > 255:
                    # Long callback can also come from the concrete task id.
                    self.report = self.home / 'report.md'
                    self.pack['task_id'] = 'long-' + 'x' * 701
                else:
                    self.report.write_text('frozen report')
                self.pack['report'] = str(self.report)
                self.pack['completion_callback'] = (
                    'DONE|' + self.pack['task_id'] + '|nonce-12345|REPORT=' + str(self.report))
                self.save()
                with LegacyTerminalFixture(home=self.home) as native:
                    with self.assertRaisesRegex(ValueError, 'REFERENCE_REQUIRED'):
                        b.submit_completion_callback(str(self.path))
                    native.send.assert_not_called()
                    native.key.assert_not_called()

    def test_callback_keeps_exact_bytes_and_native_receipt(self):
        self.pack['executor_uuid'] = 'caller'
        self.save()
        text = self.pack['completion_callback']
        with LegacyTerminalFixture(home=self.home) as native, patch.object(
                b, 'read_screen', side_effect=native.ready_screens(
                    '› \nGPT-6 high', native.draft(text))):
            native.key.side_effect = native.receipt_on_key(text)
            result = b.submit_completion_callback(str(self.path))
            self.assertTrue(result['confirmed'])
            native.send.assert_called_once_with('surface:28', text)
            self.assertEqual(result['report_sha256'], b._sha256_file(self.report))
            self.assertEqual(result['confirmation_source'], 'native_user_message_v1')

    def test_legacy_multiline_callback_can_reconcile_original_without_new_paste(self):
        self.pack['executor_uuid'] = 'caller'
        self.report = self.home / 'old\nreport.md'
        self.report.write_text('original historical report')
        self.pack['report'] = str(self.report)
        self.pack['completion_callback'] = 'DONE|single-line-task|nonce-12345|REPORT=' + str(self.report)
        self.save()
        text = self.pack['completion_callback']
        with LegacyTerminalFixture(home=self.home) as native:
            # Build the original event under the historical permissive wire rule.
            with patch.object(ref, 'require_inline'), patch.object(
                    b, 'read_screen', side_effect=native.ready_screens(
                        '› \nGPT-6 high', native.draft(text))):
                with self.assertRaises(b.DispatchUnconfirmed):
                    b.submit_completion_callback(str(self.path))
            native.append_user(text)
            native.send.reset_mock()
            native.key.reset_mock()
            result = b.submit_completion_callback(str(self.path), reconcile_only=True)
            self.assertTrue(result['confirmed'])
            native.send.assert_not_called()
            native.key.assert_not_called()


if __name__ == '__main__':
    unittest.main()
