"""The installed workflow entrypoints observe every failure without blocking."""
import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cmux_hook_runtime_audit as audit
import cmux_workflow_advisory as advisory
import manage_install as manager
import executor_ready as ready
import executor_reask as reask
import supervisor_inbox as inbox
from offline_hook_thread_fixture import native_thread


class WorkflowAdvisoryTests(unittest.TestCase):
    def test_real_stop_wrapper_persists_proactive_and_failure_classification(self):
        # Stub only the task decision. Exercise the actual wrapper, entrypoint
        # validation and audit file writer instead of mocking audit.record.
        cases = [('continue', 'TASK_CONTINUATION_REQUIRED'),
                 ('episode', 'TASK_CONTINUATION_EPISODE_BUSY'),
                 ('cadence', 'TASK_CONTINUATION_CADENCE_ACTIVE'),
                 ('unavailable', 'TASK_CONTINUATION_UNAVAILABLE')]
        for case, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                wrapper = Path(advisory.__file__).resolve()
                driver = '''
from pathlib import Path
import runpy, sys
root, script, case = sys.argv[1:4]
Path.home = staticmethod(lambda: Path(root))
sys.path.insert(0, str(Path(script).parent))
import executor_proactive_hook as proactive
def decide(payload):
    if case == 'continue':
        return dict(decision='block', reason='Fixture task continuation')
    if case == 'episode':
        raise proactive.reask.EpisodeBusy('private task data')
    if case == 'cadence':
        raise proactive.CadenceActive('private task data')
    raise ValueError('private task data')
proactive.decide = decide
sys.argv = [script, '--hook', 'cmux_executor_idle_guard.py']
runpy.run_path(script, run_name='__main__')
'''
                payload = dict(session_id='11111111-1111-4111-8111-111111111111',
                               hook_event_name='Stop')
                result = subprocess.run([sys.executable, '-B', '-c', driver,
                                         directory, str(wrapper), case],
                                        input=json.dumps(payload), text=True,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                if case == 'continue':
                    self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                else:
                    self.assertEqual(result.stdout, '')
                receipts = list(Path(directory).rglob('*.json'))
                self.assertEqual(len(receipts), 1)
                raw = receipts[0].read_text()
                receipt = json.loads(raw)
                self.assertEqual(receipt['scope_status'], expected)
                self.assertEqual(receipt['mode'], 'advisory')
                self.assertFalse(receipt['native_receipt'])
                self.assertNotIn('private task data', raw)

    def test_all_installed_workflow_commands_allow_fault_payloads(self):
        events = dict(manager.GUARDS, **manager.OPTIONAL_GUARDS)
        for name in sorted(advisory.WORKFLOW_HOOKS):
            event = events[Path(name).stem]
            faults = ['{broken', '[]', json.dumps({
                'session_id': 'unresolved-native-session',
                'hook_event_name': event,
                'tool_name': 'Bash',
                'tool_input': {'command': 'cmux-agent self'},
                'stop_hook_active': True,
                'last_assistant_message': 'Report frozen; callback could not be resolved.',
                'error': 'HOOK_CALLER_UNRESOLVED',
            })]
            for raw in faults:
                with self.subTest(hook=name, payload=raw[:30]):
                    command = shlex.split(manager.command(Path(name).stem))
                    self.assertEqual(Path(command[2]).name, 'cmux_workflow_advisory.py')
                    run = subprocess.run(command, input=raw, text=True,
                                         capture_output=True, timeout=10)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    # Empty output cannot deny a tool, force continuation,
                    # request another task or misrepresent delivery evidence.
                    self.assertEqual((run.stdout, run.stderr), ('', ''))

    def test_invalid_options_payload_or_observer_error_never_blocks(self):
        for argv, raw in [([], '{}'), (['--hook', 'foreign.py'], '{}'),
                          (['--hook', 'cmux_consensus_stop_guard.py'], 'null'),
                          (['--hook', 'cmux_consensus_stop_guard.py'],
                           'x' * (advisory.MAX_INPUT_CHARS + 1))]:
            with self.subTest(argv=argv, chars=len(raw)), \
                    patch.object(sys, 'argv', ['advisory', *argv]), \
                    patch.object(sys, 'stdin', io.StringIO(raw)), \
                    patch.object(audit, 'record') as record:
                self.assertEqual(advisory.main(), 0)
                record.assert_not_called()
        with patch.object(sys, 'argv', ['advisory', '--hook', 'cmux_consensus_stop_guard.py']), \
                patch.object(sys, 'stdin', io.StringIO('{}')), \
                patch.object(audit, 'record', side_effect=OSError('state unavailable')):
            self.assertEqual(advisory.main(), 0)

    def test_real_wrapper_records_original_hook_without_claiming_native_delivery(self):
        # Run the product wrapper/main and audit in a separate interpreter.
        # Only the storage home is isolated; no process provenance is forged.
        with tempfile.TemporaryDirectory(prefix='advisory-audit-') as directory:
            wrapper = Path(advisory.__file__).resolve()
            driver = (
                'from pathlib import Path; import runpy,sys; '
                'root=Path(sys.argv.pop(1)); '
                'Path.home=staticmethod(lambda: root); '
                'script=sys.argv.pop(1); sys.argv[0]=script; '
                'sys.path.insert(0,str(Path(script).parent)); '
                'runpy.run_path(script,run_name="__main__")'
            )
            session = '11111111-1111-4111-8111-111111111111'
            hook = 'cmux_native_delivery_guard.py'
            payload = dict(session_id=session, hook_event_name='PostToolUse',
                           tool_input={'command': 'private-command-data'})
            run = subprocess.run([sys.executable, '-B', '-c', driver, directory,
                                  str(wrapper), '--hook', hook], input=json.dumps(payload),
                                 text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((run.stdout, run.stderr), ('', ''))
            receipts = list(Path(directory).rglob('*.json'))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt['mode'], 'advisory')
            self.assertEqual(receipt['hook'], hook)
            self.assertEqual(receipt['original_hook'], hook)
            self.assertEqual(receipt['entrypoint'], wrapper.name)
            self.assertEqual(receipt['event'], 'PostToolUse')
            self.assertFalse(receipt['native_receipt'])
            self.assertNotIn('automatic_client_adoption', receipt)
            self.assertNotIn('private-command-data', receipts[0].read_text())


class SupervisorInboxAdvisoryTests(unittest.TestCase):
    """Exercise the installed entry with the real file writer/reader protocol."""
    CALLER = '11111111-1111-4111-8111-111111111111'
    SUPERVISOR = '22222222-2222-4222-8222-222222222222'
    WORKSPACE = '33333333-3333-4333-8333-333333333333'

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.payload, self.proof = native_thread(self.home)
        self.stack.enter_context(patch.object(inbox.identity, 'native_proof', return_value=self.proof))
        self.stack.enter_context(patch.object(inbox.identity, 'evaluation',
                                              return_value=contextlib.nullcontext()))
        self.identity = self.stack.enter_context(patch.object(
            inbox.identity, 'identity', return_value=(self.WORKSPACE, self.SUPERVISOR)))
        self.record = self.stack.enter_context(patch.object(audit, 'record'))
        self.state = reask.start(self.CALLER, self.SUPERVISOR, 'completed-task',
                                 now=1.0, workspace=self.WORKSPACE)
        self.marker = 'EXECUTOR_READY_advisory'
        result = reask.write_channels(self.state, self.marker, 'request next fixed pack', 2.0)
        self.assertEqual(result['errors'], [])

    def invoke(self, hook='cmux_supervisor_report_guard.py', event='PostToolUse'):
        payload = dict(self.payload, hook_event_name=event)
        out, err = io.StringIO(), io.StringIO()
        command = shlex.split(manager.command(Path(hook).stem))
        self.assertEqual(Path(command[2]).resolve(), Path(advisory.__file__).resolve())
        with patch.object(sys, 'argv', command[2:]), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(advisory.main(), 0)
        self.assertEqual(err.getvalue(), '')
        return out.getvalue()

    def test_real_file_request_reaches_supported_context_and_is_deduplicated(self):
        source = reask.inbox_path(self.state)
        original = source.read_bytes()
        raw = self.invoke()
        result = json.loads(raw)
        self.assertEqual(set(result), {'hookSpecificOutput'})
        context = result['hookSpecificOutput']
        self.assertEqual(set(context), {'hookEventName', 'additionalContext'})
        self.assertEqual(context['hookEventName'], 'PostToolUse')
        self.assertIn(self.marker, context['additionalContext'])
        request_line = next(line for line in context['additionalContext'].splitlines()
                            if line.startswith('REQUEST='))
        request, fields = request_line.removeprefix('REQUEST=').split(' SHA256=', 1)
        digest = fields.split(' ', 1)[0]
        snapshot = Path(request)
        self.assertNotEqual(snapshot, source)
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), digest)
        self.assertIn('File discovery is not native transport', context['additionalContext'])
        self.assertEqual(self.invoke(), '')
        # The producer may replace CURRENT before the supervisor first reads
        # the emitted notice. Its immutable request must still match that SHA.
        result = reask.write_channels(self.state, 'EXECUTOR_READY_rotated',
                                      'next request', 3.0)
        self.assertEqual(result['errors'], [])
        self.assertNotEqual(source.read_bytes(), original)
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), digest)
        self.assertFalse((ready.state_root() / 'message-dispatch-v1').exists())

    def test_telemetry_failure_does_not_suppress_file_request(self):
        self.record.side_effect = OSError('audit storage unavailable')
        self.assertIn(self.marker, self.invoke())

    def test_reopened_foreign_inbox_emits_nothing_and_preserves_cursors(self):
        # Establish a real native directory offset through the registered hook.
        self.assertIn(self.marker, self.invoke())
        cursor_root = ready.state_root() / 'supervisor-inbox-discovery-v2'
        before = {path: path.read_bytes() for path in cursor_root.rglob('*.json')}
        self.assertTrue(before)
        marker = 'EXECUTOR_READY_after_reopen'
        result = reask.write_channels(self.state, marker, 'new request', 3.0)
        self.assertEqual(result['errors'], [])
        other = self.home / 'foreign-inbox'
        other.mkdir()
        # A foreign page with a real request name could otherwise cause exposure
        # and cursor commits. Its contents are never trusted as request bytes.
        (other / reask.inbox_path(self.state).name).write_text('{}')
        native_open = os.open
        reopened = []

        def replace_reopened_stream(name, flags, *args, **kwargs):
            if name == '.' and 'dir_fd' in kwargs:
                fd = native_open(other, flags)
                reopened.append(fd)
                return fd
            return native_open(name, flags, *args, **kwargs)

        with patch.object(inbox.os, 'open', side_effect=replace_reopened_stream):
            self.assertEqual(self.invoke(), '')
        self.assertEqual(len(reopened), 1)
        self.assertEqual({path: path.read_bytes() for path in cursor_root.rglob('*.json')},
                         before)
        for fd in reopened:
            with self.assertRaises(OSError) as closed:
                os.fstat(fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)
        # The failed observation did not lose the request or poison the cursor.
        self.assertIn(marker, self.invoke())
        self.assertFalse((ready.state_root() / 'message-dispatch-v1').exists())

    def test_discovery_error_never_blocks_or_emits_a_control_response(self):
        with patch.object(inbox, 'discover', side_effect=OSError('inbox unavailable')):
            self.assertEqual(self.invoke(), '')
        self.record.assert_called_once()

    def test_unresolved_or_foreign_identity_emits_no_request(self):
        for binding in [(None, None), ('unresolved', self.SUPERVISOR),
                        (self.WORKSPACE, self.CALLER)]:
            with self.subTest(binding=binding):
                self.identity.return_value = binding
                self.assertEqual(self.invoke(), '')
        self.identity.side_effect = RuntimeError('HOOK_CALLER_UNRESOLVED')
        self.assertEqual(self.invoke(), '')

    def test_other_hooks_and_stop_do_not_discover_or_force_a_reask(self):
        with patch.object(inbox, 'discover') as discover:
            for hook in advisory.WORKFLOW_HOOKS:
                if hook != 'cmux_supervisor_report_guard.py':
                    self.assertEqual(self.invoke(hook=hook), '')
            for event in ('PreToolUse', 'Stop', 'SubagentStop'):
                self.assertEqual(self.invoke(event=event), '')
            discover.assert_not_called()

    def test_an_exact_reply_suppresses_discovery(self):
        body = ready.read_json(reask.inbox_path(self.state))
        ready.write_json(Path(body['reply_mailbox']), dict(
            body['reply_template'], trigger='Current chapter review must finish.'))
        self.assertEqual(self.invoke(), '')


if __name__ == '__main__':
    unittest.main()
