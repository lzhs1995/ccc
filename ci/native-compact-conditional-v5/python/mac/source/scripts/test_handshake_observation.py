import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cmux_evidence_io as evidence
import cmux_bridge as bridge
import mac_harness as harness
import observe_handshake_ack as observer


class HandshakeObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = mock.patch.object(Path, 'home', return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        self.nonce = 'b90a4192'
        self.task = 'original-handshake'
        self.target = 'executor-uuid'
        self.identity = dict(workspace_uuid='workspace-uuid', caller_surface_uuid='supervisor-uuid',
                             target_surface_uuid=self.target, target_pane_uuid='pane-uuid')
        self.text = 'DELIVERY_NONCE=' + self.nonce + '. Original handshake'
        self.payload_sha = observer.digest(self.text.encode())
        self.expected = f'PREFLIGHT_ACK|{self.task}|claude:identity|READY|INLINE|{self.nonce}'
        self.binding = dict(identity=self.identity, provider='claude', payload_sha256=self.payload_sha)
        self.attempt = dict(binding=dict(identity=self.identity, marker=self.nonce,
                                        payload_sha256=self.payload_sha),
                            native_binding=self.binding, events=[{'phase': 'PASTE_INTENT'}])
        self.row = dict(executor=self.target, task_id=self.task, executor_provider='claude',
                        ack_nonce=self.nonce, ack_line_expected=self.expected,
                        dispatch_payload_sha256=self.payload_sha, dispatch_body_reference=None,
                        status='FAIL', lifecycle='EXPIRED')
        self.receipt = dict(executors=[self.row])
        key = observer.digest(json.dumps([self.identity['caller_surface_uuid'], self.nonce], sort_keys=True).encode())
        directory = self.root / '.local/state/multi-agent-collaboration/message-dispatch-v1' / key
        directory.mkdir(parents=True)
        self.args = SimpleNamespace(controller='/original/release/source', receipt=str(self.root / 'receipt.json'),
                                    attempt=str(directory / 'attempt-0001.json'),
                                    text_file=str(self.root / 'wire.txt'), timeout=1, lines=200)
        self.save()
        self.b = mock.Mock()
        self.b.wait_for_ack.return_value = self.expected
        self.n = mock.Mock(MAX_SCAN=4 * 1024 * 1024)
        self.n._original_intent.return_value = (1.0, {'schema': 'original-fence'})
        self.native_path = self.root / 'native.jsonl'
        raw = (json.dumps(dict(type='user', message=dict(role='user', content=self.text))) + '\n').encode()
        self.native_path.write_bytes(raw)
        self.n._fenced_binding.return_value = dict(transcript=dict(path=str(self.native_path)))
        self.n._open.side_effect = lambda path: open(path, 'rb')
        self.n.probe.return_value = dict(confirmed=True, state='NATIVE_RECEIVED',
            native_proof=dict(offset=0, length=len(raw), sha256=observer.digest(raw)))
        self.n.validate_proof.return_value = True
        self.j = mock.Mock()
        self.j.original_body_pin.return_value = None
        self.j.prompt_reference.wire_reference.return_value = None
        self.modules = (self.b, self.n, evidence, self.j)

    def save(self):
        Path(self.args.receipt).write_text(json.dumps(self.receipt))
        Path(self.args.attempt).write_text(json.dumps(self.attempt))
        Path(self.args.text_file).write_text(self.text)
        self.args.receipt_sha256 = observer.digest(Path(self.args.receipt).read_bytes())
        self.args.attempt_sha256 = observer.digest(Path(self.args.attempt).read_bytes())

    def run_observation(self):
        return observer.observe(self.args, self.modules)

    def test_late_ack_preserves_expired_receipt_and_has_no_writes_or_input(self):
        before = {p: evidence.snapshot(p) for p in self.root.rglob('*') if p.is_file()}
        with mock.patch.object(Path, 'write_text', side_effect=AssertionError('write forbidden')):
            result = self.run_observation()
        self.assertEqual(before, {p: evidence.snapshot(p) for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(result['original_receipt_lifecycle'], 'EXPIRED')
        self.assertTrue(result['ack']['observed'])
        self.assertTrue(result['native_message']['confirmed'])
        self.assertEqual(result['body']['executor_read'], 'NOT_EVALUATED')
        self.assertEqual(result['business_acceptance'], 'NOT_EVALUATED')
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['journal_writes'], 0)
        self.b.submit_text.assert_not_called()
        self.b.send_key.assert_not_called()
        self.n.scan_original.assert_not_called()
        self.n.bind_target.assert_not_called()
        self.n.require_bound.assert_called_with(self.b, self.target, self.binding, self.text, read_only=True)
        self.b.wait_for_ack.assert_called_once_with(self.target, task_id=self.task, provider='claude',
                                                   nonce=self.nonce, timeout=1, poll=1, lines=200)

    def test_ack_does_not_promote_pending_or_queued_or_scan_budget(self):
        for state in ('NATIVE_PENDING', 'NATIVE_QUEUED', 'NATIVE_SCAN_BUDGET'):
            with self.subTest(state=state):
                self.n.probe.return_value = dict(confirmed=False, state=state)
                result = self.run_observation()
                self.assertTrue(result['ack']['observed'])
                self.assertFalse(result['native_message']['confirmed'])
                self.assertEqual(result['native_message']['state'], state)

    def test_original_observer_summary_receipt_is_excluded_without_losing_ack(self):
        raw = (json.dumps(dict(type='user', isCompactSummary=True,
                isVisibleInTranscriptOnly=True, message=dict(role='user', content=self.text))) + '\n').encode()
        self.native_path.write_bytes(raw)
        proof = dict(offset=0, length=len(raw), sha256=observer.digest(raw))
        self.n.probe.return_value = dict(confirmed=True, state='NATIVE_RECEIVED', native_proof=proof)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.run_observation()
        self.assertTrue(result['ack']['observed'])
        self.assertFalse(result['native_message']['confirmed'])
        self.assertEqual(result['native_message']['state'], 'NATIVE_GENERATED_SUMMARY_EXCLUDED')
        self.assertEqual(result['native_message']['excluded_proof'], proof)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.b.submit_text.assert_not_called()

    def test_native_received_does_not_promote_missing_ack(self):
        self.b.wait_for_ack.side_effect = TimeoutError('not visible')
        result = self.run_observation()
        self.assertEqual(result['status'], 'OBSERVED')
        self.assertEqual(result['ack']['state'], 'ACK_NOT_VISIBLE')
        self.assertTrue(result['native_message']['confirmed'])

    def test_modified_receipt_and_attempt_rejected_before_any_observation(self):
        for key in ('receipt', 'attempt'):
            with self.subTest(key=key):
                self.save()
                path = Path(getattr(self.args, key))
                path.write_bytes(path.read_bytes() + b' ')
                with self.assertRaisesRegex(ValueError, 'ORIGINAL_.*_CHANGED'):
                    self.run_observation()
                self.b.wait_for_ack.assert_not_called()
                self.n.require_bound.assert_not_called()

    def test_changed_payload_or_nonce_or_executor_or_provider_is_not_observed(self):
        original = copy.deepcopy(self.row)
        changes = {'ack_nonce': '12345678', 'executor': 'different-executor',
                   'executor_provider': 'codex', 'dispatch_payload_sha256': '0' * 64,
                   'ack_line_expected': 'other-ack'}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.row.clear()
                self.row.update(original, **{field: value})
                self.save()
                with self.assertRaises(ValueError):
                    self.run_observation()
                self.b.wait_for_ack.assert_not_called()

    def test_duplicate_executor_not_resolved_by_top_level_receipt(self):
        self.receipt.update(self.row)
        self.receipt['executors'].append(copy.deepcopy(self.row))
        self.save()
        with self.assertRaisesRegex(ValueError, 'NOT_UNIQUE'):
            self.run_observation()

    def test_selected_executor_not_first_executor_top_level(self):
        unrelated = dict(self.row, executor='other-executor', ack_nonce='12345678')
        self.receipt.update(unrelated)
        self.receipt['executors'].insert(0, unrelated)
        self.save()
        self.assertEqual(self.run_observation()['original_nonce'], self.nonce)

    def test_attempt_outside_original_journal_is_rejected(self):
        path = self.root / 'copied-attempt.json'
        path.write_bytes(Path(self.args.attempt).read_bytes())
        self.args.attempt = str(path)
        with self.assertRaisesRegex(ValueError, 'ORIGINAL_MESSAGE_ATTEMPT_REQUIRED'):
            self.run_observation()

    def test_no_original_input_fence_no_observation(self):
        self.n._original_intent.side_effect = ValueError('original fence missing')
        with self.assertRaisesRegex(ValueError, 'original fence missing'):
            self.run_observation()
        self.b.wait_for_ack.assert_not_called()

    def test_parser_result_must_be_exact_original_nonce(self):
        self.b.wait_for_ack.return_value = self.expected + '0'
        with self.assertRaisesRegex(ValueError, 'PARSER_RESULT_CHANGED'):
            self.run_observation()

    def test_concurrent_receipt_edit_is_not_rewritten_or_accepted(self):
        def change(*a, **kw):
            Path(self.args.receipt).write_text('newer owner state')
            return self.expected
        self.b.wait_for_ack.side_effect = change
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_CHANGED_DURING_OBSERVATION'):
            self.run_observation()
        self.assertEqual(Path(self.args.receipt).read_text(), 'newer owner state')

    def test_body_reference_and_pin_must_remain_original(self):
        self.row['dispatch_body_reference'] = {'path': 'different-body'}
        self.save()
        with self.assertRaisesRegex(ValueError, 'BODY_REFERENCE_CHANGED'):
            self.run_observation()
        self.row['dispatch_body_reference'] = None
        self.save()
        self.j.original_body_pin.side_effect = [{'sha': 'old'}, {'sha': 'new'}]
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_CHANGED_DURING_OBSERVATION'):
            self.run_observation()

    def test_native_proof_must_revalidate_after_poll(self):
        self.n.validate_proof.return_value = False
        with self.assertRaisesRegex(ValueError, 'NATIVE_PROOF_CHANGED'):
            self.run_observation()

    def test_real_original_claude_parser_rejects_echo_and_accepts_response(self):
        for screen, expected in [(f'❯ {self.expected}', False), (f'⏺ {self.expected}', True)]:
            with self.subTest(screen=screen), mock.patch.object(bridge, 'read_screen', return_value=screen), \
                    mock.patch.object(bridge.time, 'time', side_effect=[0, 0, 2]), \
                    mock.patch.object(bridge.time, 'sleep'):
                self.modules = (bridge, self.n, evidence, self.j)
                # The observer's timestamp shares Python's time module; leave
                # enough samples after the bounded original parser completes.
                with mock.patch.object(observer.time, 'time', side_effect=[0, 0, 2, 3]):
                    result = self.run_observation()
                self.assertEqual(result['ack']['observed'], expected)


class SupervisorProviderArgumentTests(unittest.TestCase):
    def test_invalid_surface_and_uuid_rejected_before_tool_action(self):
        for value in ('surface:8701', 'E765373A-4081-4038-ACC8-EACEE2540F08'):
            with self.subTest(value=value), mock.patch('sys.argv', ['mac_harness.py', 'doctor', '--supervisor', value]), \
                    mock.patch.object(harness, 'cmd_doctor') as action, contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as exc:
                    harness.main()
                self.assertEqual(exc.exception.code, 2)
                self.assertIn('for a Codex supervisor use --supervisor codex', err.getvalue())
                action.assert_not_called()

    def test_provider_alias_passes_valid_provider(self):
        for option in ('--supervisor', '--supervisor-provider'):
            with self.subTest(option=option), mock.patch('sys.argv', ['mac_harness.py', 'doctor', option, 'CODEX']), \
                    mock.patch.object(harness, 'cmd_doctor') as action:
                harness.main()
                self.assertEqual(action.call_args.args[0].supervisor, 'codex')


if __name__ == '__main__':
    unittest.main()
