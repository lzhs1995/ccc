"""Actual native metadata, adoption ownership and per-evaluation proof isolation."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import cmux_hook_identity as identity
import cmux_hook_runtime_audit as audit
import cmux_hook_thread as thread
import supervisor_inbox as inbox
from offline_hook_thread_fixture import native_processes, native_thread, ROOT_THREAD

CHILD = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'


class HookThreadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.payload, self.proof = native_thread(self.home)

    def test_root_requires_actual_metadata_and_matching_live_foreground_proof(self):
        facts = thread.describe(self.payload)
        self.assertEqual(facts['actual_thread_id'], ROOT_THREAD)
        self.assertEqual(facts['thread_scope'], 'ROOT_METADATA')
        self.assertTrue(thread.authenticated_root(facts, self.proof))
        for bad in (None, {}, dict(session=ROOT_THREAD),
                    dict(session=OTHER, foreground=dict(thread_id=ROOT_THREAD)),
                    dict(session=ROOT_THREAD, foreground=dict(thread_id=OTHER))):
            with self.subTest(proof=bad):
                self.assertFalse(thread.authenticated_root(facts, bad))

    def test_real_identity_path_preserves_only_authenticated_foreground_thread(self):
        # Mock only kernel, foreground and cmux-tree inputs. Keep collect_hook,
        # caller_snapshot, public_proof and the hook resolver connected.
        with native_processes(), identity.evaluation(self.payload):
            proof = identity.native_proof()
            self.assertEqual(proof['foreground'], {'thread_id': ROOT_THREAD})
            self.assertTrue(thread.authenticated_root(thread.describe(self.payload), proof))
            self.assertFalse({'client', 'env', 'chain', 'daemon'} & proof.keys())
            child, _ = native_thread(self.home, CHILD, inherited=ROOT_THREAD,
                                     parent=ROOT_THREAD)
            self.assertFalse(thread.authenticated_root(thread.describe(child), proof))

    def test_legacy_resume_without_live_foreground_is_not_a_root_discovery_proof(self):
        with native_processes(), patch.object(
                identity.workspace.daemon_identity.foreground, 'read', return_value=None), \
                identity.evaluation(self.payload):
            proof = identity.native_proof()
            self.assertNotIn('foreground', proof)
            self.assertFalse(thread.authenticated_root(thread.describe(self.payload), proof))

    def test_inherited_parent_session_never_identifies_the_child_as_supervisor(self):
        for inherited, parent in ((ROOT_THREAD, ROOT_THREAD),
                                  (CHILD, ROOT_THREAD), (ROOT_THREAD, None)):
            with self.subTest(inherited=inherited, parent=parent):
                payload, proof = native_thread(self.home, CHILD,
                                               inherited=inherited, parent=parent)
                facts = thread.describe(payload)
                self.assertEqual(facts['actual_thread_id'], CHILD)
                self.assertEqual(facts['inherited_session_id'], inherited)
                self.assertEqual(facts['parent_thread_id'], parent)
                self.assertEqual(facts['thread_scope'], 'CHILD_METADATA')
                self.assertFalse(thread.authenticated_root(facts, proof))
                with patch.object(inbox.identity, 'evaluation') as resolve:
                    self.assertEqual(inbox.discover(payload), [])
                    resolve.assert_not_called()

    def test_symlink_fifo_oversize_and_noncanonical_metadata_are_unverified(self):
        path = Path(self.payload['transcript_path'])
        original = path.read_bytes()
        cases = ('symlink', 'fifo', 'oversize', 'no_newline', 'malformed', 'wrong_id')
        for case in cases:
            with self.subTest(case=case):
                path.unlink(missing_ok=True)
                if case == 'symlink':
                    target = path.with_suffix('.saved')
                    target.write_bytes(original)
                    path.symlink_to(target)
                elif case == 'fifo':
                    os.mkfifo(path)
                elif case == 'oversize':
                    path.write_bytes(b' ' * (thread.MAX_META_BYTES + 1) + b'\n')
                elif case == 'no_newline':
                    path.write_bytes(original.rstrip(b'\n'))
                elif case == 'malformed':
                    path.write_bytes(b'{broken\n')
                else:
                    entry = json.loads(original)
                    entry['payload']['id'] = OTHER
                    path.write_text(json.dumps(entry) + '\n')
                self.assertEqual(thread.describe(self.payload)['thread_scope'], 'UNVERIFIED')
        self.assertEqual(thread.describe(dict(self.payload, transcript_path='relative.jsonl'))[
            'thread_scope'], 'UNVERIFIED')

    def test_append_only_transcript_does_not_require_reading_full_history(self):
        path = Path(self.payload['transcript_path'])
        with path.open('ab') as handle:
            handle.write(b'not metadata' * thread.MAX_META_BYTES)
        self.assertEqual(thread.describe(self.payload)['thread_scope'], 'ROOT_METADATA')

    def test_child_and_unverified_adoption_receipts_cannot_replace_root_receipt(self):
        hook = 'cmux_supervisor_report_guard.py'
        wrapper = str(Path(audit.__file__).with_name('cmux_workflow_advisory.py'))
        with patch.object(audit, '_entrypoint', return_value=hook), \
                patch.object(audit, '_parent_chain', return_value=[]), \
                patch.object(sys.modules['__main__'], '__file__', wrapper, create=True):
            audit.record(self.payload, 'ADVISORY_OBSERVED', original_hook=hook, mode='advisory')
            directory = self.home / '.local/state/multi-agent-collaboration/runtime-adoption-v1'
            root = directory / (ROOT_THREAD + '.PostToolUse.' + hook + '.json')
            before = root.read_bytes()
            child, _proof = native_thread(self.home, CHILD,
                                          inherited=ROOT_THREAD, parent=ROOT_THREAD)
            audit.record(child, 'ADVISORY_OBSERVED', original_hook=hook, mode='advisory')
            invalid = dict(child, transcript_path=str(self.home / 'absent-transcript.jsonl'))
            audit.record(invalid, 'ADVISORY_OBSERVED', original_hook=hook, mode='advisory')
        self.assertEqual(root.read_bytes(), before)
        child_receipt = directory / (CHILD + '.PostToolUse.' + hook + '.json')
        self.assertEqual(json.loads(child_receipt.read_text())['actual_thread_id'], CHILD)
        self.assertEqual(len(list(directory.glob(ROOT_THREAD + '.unverified-*.json'))), 1)
        self.assertEqual(len(list(directory.glob('*.json'))), 3)

    def test_nested_evaluation_restores_identity_and_proof_after_error(self):
        prior = dict(session='prior')
        old_identity = identity._current.set(('prior-workspace', 'prior-surface'))
        old_proof = identity._proof.set(prior)
        try:
            def resolve(payload):
                identity._proof.set(dict(session=payload['session_id']))
                return ('workspace', payload['session_id'])
            with patch.object(identity, 'resolve', side_effect=resolve):
                with identity.evaluation(self.payload):
                    self.assertEqual(identity.native_proof()['session'], ROOT_THREAD)
                    with self.assertRaises(RuntimeError):
                        with identity.evaluation(dict(session_id=CHILD)):
                            self.assertEqual(identity.identity({}), ('workspace', CHILD))
                            raise RuntimeError('child failed')
                    self.assertEqual(identity.identity({}), ('workspace', ROOT_THREAD))
                    self.assertEqual(identity.native_proof()['session'], ROOT_THREAD)
            self.assertEqual(identity.identity({}), ('prior-workspace', 'prior-surface'))
            self.assertEqual(identity.native_proof(), prior)
        finally:
            identity._current.reset(old_identity)
            identity._proof.reset(old_proof)

    def test_unresolved_collector_cannot_reuse_stale_native_proof(self):
        token = identity._proof.set(self.proof)
        try:
            with patch.object(identity.workspace.daemon_identity, 'collect_hook', return_value=None), \
                    patch.dict(os.environ, {}, clear=True):
                self.assertEqual(identity._resolve({}), ('default', None))
                self.assertIsNone(identity.native_proof())
        finally:
            identity._proof.reset(token)


if __name__ == '__main__':
    unittest.main()
