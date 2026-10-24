"""Offline real journal + current Claude sender regressions; no live I/O."""
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cmux_bridge as b
import cmux_message_journal as journal
import mac_harness as h
from native_test_support import NativeFixture

class HandshakeContractRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.native = NativeFixture.attach(self, home=self.home, provider='claude')
        self.root = self.home / 'task'
        self.root.mkdir()
        self.args = types.SimpleNamespace(task_id='offline-contract-error', lines=200, timeout=None, force_compose=False)
        self.gate = {'supervisor':'caller', 'workspace_ref':'workspace'}
        self.item = {'surface_ref':'target','ordinal':1,'provider':'claude'}
        self.rows = []
        self.caught = []
        self.before_harness = {}
        self.details_mode = None
        self.real_submit = b.submit_text
        self.sender = patch.object(b, 'submit_text', side_effect=self.capture).start()
        self.addCleanup(patch.stopall)
        self.ack = patch.object(b, 'wait_for_ack', side_effect=AssertionError('must not poll ACK after contract failure')).start()
        self.nonce = Mock(return_value='abcdef01')
        real_token_hex = h.secrets.token_hex
        def harness_token_hex(nbytes=None):
            # Atomic receipt writes also use token_hex(4); count only the
            # handshake nonce, retaining real unique storage temp names.
            if sys._getframe(1).f_code is h._handshake_one.__code__:
                return self.nonce(nbytes)
            return real_token_hex(nbytes)
        patch.object(h, 'secrets', types.SimpleNamespace(token_hex=harness_token_hex)).start()
        patch.object(b, 'read_screen', return_value='❯ \n[claude-opus-5]').start()

    def capture(self, *args, **kwargs):
        try:
            return self.real_submit(*args, **kwargs)
        except b.TaskPackContractError as exc:
            self.caught.append(exc)
            details = getattr(exc, 'details', None)
            if isinstance(details, dict) and details.get('attempt'):
                path = Path(details['attempt'])
                self.before_harness[path] = path.read_bytes()
                if self.details_mode == 'missing':
                    exc.details = None
                elif self.details_mode == 'unreadable':
                    exc.details = dict(details, attempt=str(self.home / 'nonexistent.json'))
            raise

    def invoke(self):
        return h._handshake_one(self.args, self.root, self.gate, self.item, self.rows)

    def assert_settled(self, *, host_submissions=0):
        saved = json.loads((self.root / 'handshake-receipt.json').read_text())
        for row in (saved, saved['executors'][0]):
            self.assertEqual((row['status'], row['lifecycle']), ('FAIL','REJECTED'))
            self.assertEqual(row['detector_side'], 'supervisor')
            self.assertIs(row['attributable_to_executor'], False)
            self.assertEqual(row['dispatch_error_type'], 'TaskPackContractError')
            self.assertTrue(row['terminal_error_at'])
            self.assertIsNone(row['dispatch_submitted_at'])
            self.assertIsNone(row['submission_state'])
            self.assertFalse(row['executor_ack'])
            self.assertNotIn('confirmed', row)
            self.assertNotIn('input_operations', row)
            self.assertNotIn('native_binding', row)
        self.assertEqual(self.nonce.call_count, 1)
        self.sender.assert_called_once()
        self.ack.assert_not_called()
        self.assertEqual(self.native.key.call_count, host_submissions)
        self.native.rpc.assert_not_called()
        self.assertEqual(saved['ack_nonce'], 'abcdef01')
        self.assertEqual(saved['dispatch_payload_sha256'], hashlib.sha256(saved['dispatch_payload'].encode()).hexdigest())
        return saved

    @contextlib.contextmanager
    def held_pane_lock(self):
        root = self.home / '.local/state/multi-agent-collaboration/deliveries-v1'
        root.mkdir(parents=True, exist_ok=True)
        path = root / ('pane-' + journal.digest('workspace:pane') + '.lock')
        code = "import fcntl,sys; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print('LOCKED',flush=True); sys.stdin.read()"
        proc = subprocess.Popen([sys.executable,'-I','-B','-c',code,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),'LOCKED')
            yield
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()

    def test_real_lock_refusal_before_attempt_settles_without_input(self):
        with self.held_pane_lock():
            self.assertFalse(self.invoke())
        saved = self.assert_settled()
        self.assertIn('MESSAGE_IN_PROGRESS', saved['dispatch_error'])
        self.assertEqual(saved['dispatch_evidence_status'], 'UNAVAILABLE')
        self.native.send.assert_not_called()
        self.assertEqual(list((self.home / '.local/state/multi-agent-collaboration/message-dispatch-v1').glob('*/attempt-*.json')), [])
        self.assertNotIn('dispatch_attempt', saved)

    def mutate_after_paste(self, *args, **kwargs):
        self.native.identity['target_pane_uuid'] = 'changed-after-paste'

    def check_after_paste(self, mode):
        self.details_mode = mode
        self.native.send.side_effect = self.mutate_after_paste
        self.assertFalse(self.invoke())
        # The single host paste/submit request may finish before identity drift
        # is visible to us. It earns no receipt and no second command/key.
        saved = self.assert_settled(host_submissions=1)
        self.native.send.assert_called_once()
        self.assertIn('MESSAGE_IDENTITY_CHANGED', saved['dispatch_error'])
        self.assertEqual(len(self.before_harness), 1)
        path, raw = next(iter(self.before_harness.items()))
        self.assertEqual(path.read_bytes(), raw)
        attempt = json.loads(raw)
        self.assertEqual(attempt['phase'], 'ENTER_INTENT')
        self.assertEqual([e['phase'] for e in attempt['events']], ['PASTE_INTENT', 'ENTER_INTENT'])
        self.assertIn('native_paste_fence', attempt['events'][0])
        self.assertEqual(attempt['native_binding']['provider'], 'claude')
        if mode is None:
            self.assertEqual(saved['dispatch_attempt']['path'], str(path))
            self.assertEqual(saved['dispatch_attempt']['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(saved['dispatch_evidence_status'], 'ORIGINAL_ATTEMPT_PINNED')
        else:
            self.assertEqual(saved['dispatch_evidence_status'], 'UNAVAILABLE')
            self.assertNotIn('dispatch_attempt', saved)

    def test_after_paste_error_keeps_original_attempt_without_resend(self):
        self.check_after_paste(None)

    def test_after_paste_missing_details_stays_unavailable(self):
        self.check_after_paste('missing')

    def test_after_paste_unreadable_details_stays_unavailable(self):
        self.check_after_paste('unreadable')

    def test_terminal_receipt_write_failure_propagates(self):
        original = h._write_handshake_receipt
        def fail_final(root, rows):
            if rows and rows[-1]['status'] == 'FAIL':
                raise OSError('simulated receipt storage failure')
            return original(root, rows)
        with self.held_pane_lock(), patch.object(h, '_write_handshake_receipt', side_effect=fail_final):
            with self.assertRaisesRegex(OSError, 'simulated receipt storage failure'):
                self.invoke()
        self.ack.assert_not_called()
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()
        self.sender.assert_called_once()
        saved = json.loads((self.root / 'handshake-receipt.json').read_text())
        self.assertEqual(saved['lifecycle'], 'PENDING')

    def check_aggregate_failure(self, *, after_paste):
        items = [dict(self.item, surface_ref="first", ordinal=1), self.item,
                 dict(self.item, surface_ref="never-dispatched", ordinal=3)]
        items[1] = dict(self.item, ordinal=2)
        gate = dict(self.gate, status="PASS")
        h._write(self.root / "identity-gate.json", gate)
        self.nonce.side_effect = ["aaaa0001", "bbbb0002"]

        def submit(surface, text, **kwargs):
            if surface == "first":
                return {"fixture": "first executor successful submission"}
            self.assertEqual(surface, "target")
            return self.capture(surface, text, **kwargs)

        def ack(surface, **kwargs):
            self.assertEqual(surface, "first")
            return (f"PREFLIGHT_ACK|{self.args.task_id}|claude:identity|"
                    f"READY|INLINE|{kwargs['nonce']}")

        self.sender.side_effect = submit
        self.ack.side_effect = ack
        if after_paste:
            self.native.send.side_effect = self.mutate_after_paste
        lock = contextlib.nullcontext() if after_paste else self.held_pane_lock()
        with lock, patch.object(h, "_artifact_root", return_value=self.root), \
                patch.object(h, "_recheck_workspace_gate"), \
                patch.object(h, "_gate_executors", return_value=items):
            with self.assertRaises(SystemExit) as stopped:
                h.cmd_handshake(self.args)
        self.assertEqual(stopped.exception.code, 1)
        saved = json.loads((self.root / "handshake-receipt.json").read_text())
        self.assertEqual((saved["status"], saved["lifecycle"]), ("FAIL", "REJECTED"))
        self.assertFalse(saved["executor_ack"])
        self.assertFalse(saved["all_executors_acked"])
        self.assertEqual(saved["unproven_executors"], ["target", "never-dispatched"])
        self.assertEqual(saved["unstarted_executors"], ["never-dispatched"])
        self.assertEqual(saved["executor_count"], 2)
        first, failed = saved["executors"]
        self.assertEqual((first["status"], first["lifecycle"]), ("PASS", "ACKED"))
        self.assertEqual(first["ack_nonce"], "aaaa0001")
        self.assertEqual(failed["ack_nonce"], "bbbb0002")
        self.assertEqual(failed["dispatch_error_type"], "TaskPackContractError")
        self.assertEqual(failed["detector_side"], "supervisor")
        self.assertFalse(failed["attributable_to_executor"])
        self.assertIsNone(failed["dispatch_submitted_at"])
        self.assertIsNone(failed["submission_state"])
        self.assertEqual(self.sender.call_count, 2)
        self.ack.assert_called_once()
        self.assertEqual(self.nonce.call_count, 2)
        self.assertEqual(self.native.key.call_count, 1 if after_paste else 0)
        self.native.rpc.assert_not_called()
        if after_paste:
            self.native.send.assert_called_once()
            self.assertEqual(len(self.before_harness), 1)
            path, raw = next(iter(self.before_harness.items()))
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(json.loads(raw)["phase"], "ENTER_INTENT")
            self.assertEqual(failed["dispatch_attempt"]["sha256"],
                             hashlib.sha256(raw).hexdigest())
        else:
            self.native.send.assert_not_called()
            self.assertEqual(failed["dispatch_evidence_status"], "UNAVAILABLE")
            self.assertNotIn("dispatch_attempt", failed)

    def test_cmd_handshake_second_lock_failure_cannot_credit_first_ack_to_panel(self):
        self.check_aggregate_failure(after_paste=False)

    def test_cmd_handshake_second_postpaste_failure_preserves_original_intent(self):
        self.check_aggregate_failure(after_paste=True)

if __name__ == '__main__':
    unittest.main(verbosity=2)
