"""Protect foreign drafts and retain the distinction between paste and submit."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cmux_bridge as bridge
import cmux_direct_input as direct


class ProtectedSubmitTests(unittest.TestCase):
    def setUp(self):
        self.marker = 'DRAFT_INCIDENT_REGRESSION'
        self.text = 'STATUS: ' + self.marker + ' 任务请继续'
        self.proof = {'workspace_uuid': 'WORKSPACE', 'target_surface_uuid': 'TARGET'}
        self.events, self.keys = [], []
        self.ack = {'submitted': True}
        self.input_state = dict(surface_id='TARGET', workspace_id='WORKSPACE',
            agent=True, terminal=True, state='empty', blocks_typing=False,
            lifecycle='running')
        self.rc = 0
        self.stderr = 'refused draft'
        self.pin = patch.object(bridge, 'pin_workspace', return_value=self.proof)
        self.pin.start(); self.addCleanup(self.pin.stop)
        self.run = patch.object(bridge.subprocess, 'run', side_effect=self.process).start()
        self.addCleanup(patch.stopall)

    def process(self, argv, **kwargs):
        if 'surface.input_state' in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.input_state), '')
        return subprocess.CompletedProcess(argv, self.rc, json.dumps(self.ack) if self.ack is not None else '', self.stderr)

    def send(self, screen='Compacting context'):
        return direct.send_once(self.text, self.marker, recheck=lambda: None,
            preflight=lambda: bridge.require_prompt_free('target'),
            emit=lambda phase, *args: self.events.append({'phase': phase}),
            submit=lambda text: bridge.submit_text_protected('target', text),
            paste=Mock(side_effect=AssertionError('no raw fallback')),
            key=self.keys.append,
            observe=lambda: {'state': 'NATIVE_PENDING', 'confirmed': False},
            read_screen=lambda: screen, wait_seconds=0)

    def test_nonzero_refusal_never_sends_a_separate_enter(self):
        self.rc = 1
        with self.assertRaisesRegex(RuntimeError, 'rc=1'):
            self.send()
        self.assertEqual(self.run.call_count, 2)  # Read-only state, then one paste request.
        self.assertEqual(self.keys, [])
        self.assertNotIn('ENTER_SENT', [e['phase'] for e in self.events])

    def test_unknown_submission_never_falls_back_or_gets_recovery_permission(self):
        self.ack = {}
        with self.assertRaisesRegex(RuntimeError, 'OUTCOME_UNKNOWN'):
            self.send()
        self.assertEqual(self.keys, [])
        self.assertNotIn('PASTED', [e['phase'] for e in self.events])

    def test_exact_cli_prepaste_refusal_has_zero_actual_input_and_keeps_intents(self):
        self.rc, self.ack = 1, None
        self.stderr = 'Error: paste: the agent prompt in TARGET holds text someone is typing, so nothing was sent. Retry when the prompt is clear, or pass --force to send anyway.'
        with self.assertRaises(direct.ProtectedSubmitRefused) as caught:
            self.send()
        details = caught.exception.details
        self.assertEqual(details['input_operations'], 0)
        self.assertEqual(details['actual_input_operations'], 0)
        self.assertEqual(details['input_intents_recorded'], 2)
        self.assertEqual(details['input_outcome'], 'REFUSED_BEFORE_PASTE')
        self.assertEqual([e['phase'] for e in self.events],
                         ['PASTE_INTENT', 'ENTER_INTENT', 'PROTECTED_SUBMIT_REFUSED'])
        self.assertTrue(details['no_repaste'])
        self.assertEqual(self.keys, [])
        original = {'native_binding': {'receipt_contract': 'native-user-marker-v1',
                    'receipt_marker': self.marker}, 'events': self.events}
        with self.assertRaisesRegex(ValueError, 'BUDGET_UNAVAILABLE'):
            direct.recover_once(self.text, self.marker, original=original,
                recheck=lambda: None, emit=Mock(), key=Mock(), observe=Mock(), read_screen=Mock())

    def test_generic_nonzero_or_mismatched_refusal_stays_unknown(self):
        self.rc, self.ack = 1, None
        for stderr in ('draft', 'timeout after paste',
                       'paste: the agent prompt in OTHER holds text someone is typing, so nothing was sent. Retry when the prompt is clear, or pass --force to send anyway.'):
            self.stderr = stderr
            with self.assertRaises(RuntimeError) as caught:
                self.send()
            self.assertNotIsInstance(caught.exception, direct.ProtectedSubmitRefused)
            self.assertEqual(caught.exception.details['input_operations'], 2)
            self.assertEqual(caught.exception.details['input_outcome'], 'UNKNOWN')
            self.assertIsNone(caught.exception.details['actual_input_operations'])
            self.assertEqual(self.keys, [])

    def test_refusal_classifier_requires_exact_diagnostic_status_and_empty_stdout(self):
        text = 'paste: TARGET is waiting on a question or dialog, so nothing was sent. Retry once it is answered, or pass --force to send anyway.'
        self.assertIsInstance(direct.protected_submit_refusal(
            subprocess.CompletedProcess([], 1, '', text), 'TARGET'), direct.ProtectedSubmitRefused)
        for rc, out, err in ((-9, '', text), (2, '', text), (1, '{}', text),
                              (1, '', text + '\nlate failure')):
            self.assertIsNone(direct.protected_submit_refusal(
                subprocess.CompletedProcess([], rc, out, err), 'TARGET'))

    def test_query_failure_or_unknown_fields_precede_all_input_intents(self):
        for state in ({}, dict(self.input_state, blocks_typing=None),
                      dict(self.input_state, surface_id='OTHER')):
            self.input_state = state
            with self.assertRaisesRegex(RuntimeError, 'PROMPT_STATE_UNKNOWN'):
                self.send()
            self.assertEqual(self.events, [])
            self.assertEqual(self.keys, [])
        self.run.side_effect = RuntimeError('input_state unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.send()
        self.assertEqual(self.events, [])

    def test_foreign_draft_and_dialog_are_refused_before_paste(self):
        for state in ('draft', 'dialog'):
            self.input_state.update(state=state, blocks_typing=True)
            with self.assertRaisesRegex(RuntimeError, 'PROMPT_NOT_EMPTY'):
                self.send()
            self.assertEqual(self.events, [])
            self.assertEqual(self.keys, [])

    def test_host_queued_paste_never_authorizes_additional_key(self):
        self.ack = {'delivery': 'queued'}
        result = self.send('❯ ' + self.text)
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['extra_enter_deferred'], 'HOST_INPUT_QUEUED')
        self.assertIn('PASTE_QUEUED', [e['phase'] for e in self.events])
        self.assertNotIn('PASTED', [e['phase'] for e in self.events])
        self.assertEqual(self.keys, [])
        original = {'native_binding': {'receipt_contract': 'native-user-marker-v1',
                    'receipt_marker': self.marker}, 'events': self.events}
        with self.assertRaisesRegex(ValueError, 'BUDGET_UNAVAILABLE'):
            direct.recover_once(self.text, self.marker, original=original,
                recheck=lambda: None, emit=Mock(), key=self.keys.append,
                observe=Mock(), read_screen=Mock(), wait_seconds=0)

    def test_zero_exit_with_declined_submit_does_not_claim_enter_sent(self):
        self.ack = {'submitted': False, 'submit_error': 'compacting'}
        result = self.send()
        self.assertFalse(result['confirmed'])
        phases = [e['phase'] for e in self.events]
        self.assertIn('PASTED', phases)
        self.assertIn('ENTER_NOT_SENT', phases)
        self.assertNotIn('ENTER_SENT', phases)
        self.assertEqual(self.keys, [])
        original = {'native_binding': {'receipt_contract': 'native-user-marker-v1',
                    'receipt_marker': self.marker}, 'events': self.events}
        def observe():
            return {'confirmed': bool(self.keys),
                    'state': 'NATIVE_RECEIVED' if self.keys else 'NATIVE_PENDING'}
        result = direct.recover_once(self.text, self.marker, original=original,
            recheck=lambda: None, emit=lambda phase, *args: self.events.append({'phase': phase}),
            key=self.keys.append, observe=observe, read_screen=lambda: '❯ ' + self.text,
            wait_seconds=0)
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.keys, ['enter'])
        with self.assertRaisesRegex(ValueError, 'BUDGET_UNAVAILABLE'):
            direct.recover_once(self.text, self.marker, original=original,
                recheck=lambda: None, emit=Mock(), key=self.keys.append,
                observe=observe, read_screen=Mock(), wait_seconds=0)

    def test_protected_command_preserves_literal_text_and_pins_target(self):
        text = '--force literal \\n 中文 "quote"\nsecond line'
        result = bridge.submit_text_protected('target', text)
        self.assertTrue(result['submitted'])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[1:3], ['--json', 'paste'])
        self.assertEqual(argv[argv.index('--workspace') + 1], 'WORKSPACE')
        self.assertEqual(argv[argv.index('--surface') + 1], 'TARGET')
        self.assertIn('--submit', argv)
        self.assertNotIn('--force', argv[:-1])
        self.assertEqual(argv[-2:], ['--', text])

    def test_sanitized_controls_are_refused_before_transport(self):
        for char in ('\x00', '\x03', '\x1b', '\x7f'):
            with self.assertRaisesRegex(ValueError, 'CONTROL_CHARACTER'):
                bridge.submit_text_protected('target', self.text + char)
        self.run.assert_not_called()

    def test_successor_state_query_pins_both_endpoints_without_granting_input(self):
        target = '00000000-0000-4000-8000-000000000003'
        workspace = '00000000-0000-4000-8000-000000000001'
        proof = dict(target_surface_uuid=target, target_workspace_uuid=workspace)
        params = dict(surface_id=target, workspace_id=workspace)
        with patch.object(bridge, 'pin_successor_pair', return_value=proof):
            bridge._run_successor('rpc', 'surface.input_state', json.dumps(params),
                artifact={}, receipt_contract='native-user-marker-v1')
            self.assertIn('surface.input_state', self.run.call_args.args[0])
            self.run.reset_mock()
            params['surface_id'] = '00000000-0000-4000-8000-000000000004'
            with self.assertRaisesRegex(bridge.WorkspaceScopeError, 'state target override'):
                bridge._run_successor('rpc', 'surface.input_state', json.dumps(params),
                    artifact={}, receipt_contract='native-user-marker-v1')
            self.run.assert_not_called()

    def test_untracked_native_claude_requires_complete_empty_frame(self):
        state = json.dumps(dict(self.input_state, agent=False, lifecycle='unknown'))
        empty = '✽ Flowing…\n◎ /goal active (3h)\n' + '─' * 20 + '\n❯\u00a0 \n' + '─' * 20
        result = direct.require_prompt_state(state, 'TARGET', 'WORKSPACE',
                                              read_claude_screen=lambda: empty)
        self.assertEqual(result['draft_check_source'], 'authenticated_claude_empty_frame')
        for screen in (empty.replace('❯\u00a0 ', '❯ user half question'),
                       empty.rsplit('\n', 1)[0], '❯ ',
                       empty.replace('❯\u00a0 ', '❯\u00a0 \nuser continuation')):
            with self.assertRaisesRegex(RuntimeError, 'PROMPT_STATE_UNKNOWN'):
                direct.require_prompt_state(state, 'TARGET', 'WORKSPACE',
                                             read_claude_screen=lambda: screen)
        with self.assertRaisesRegex(RuntimeError, 'PROMPT_STATE_UNKNOWN'):
            direct.require_prompt_state(state, 'TARGET', 'WORKSPACE')


    def test_measured_claude303_frame_has_positive_and_negative_draft_controls(self):
        template = (Path(__file__).parent / 'fixtures/claude303-reviewer-input.txt').read_text()
        rows = template.splitlines()
        prompt = next(i for i, row in enumerate(rows) if row.lstrip().startswith('❯'))
        def frame(body):
            value = list(rows)
            value[prompt] = '❯ ' + body
            return '\n'.join(value)
        self.assertTrue(direct.empty_claude_input(frame('')))
        self.assertFalse(direct.owned_draft(frame(''), self.text))
        self.assertTrue(direct.owned_draft(frame(self.text), self.text))
        for body in ('user half question', '[Pasted text #1 +3 lines]',
                     self.text + ' extra user text', 'user prefix ' + self.text):
            with self.subTest(body=body):
                self.assertFalse(direct.empty_claude_input(frame(body)))
                self.assertFalse(direct.owned_draft(frame(body), self.text))


if __name__ == '__main__':
    unittest.main()
