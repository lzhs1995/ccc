import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.standby_original_transcript import OriginalTranscript


class OriginalTranscriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = self.root/'sessions'
        self.sessions.mkdir()
        claim = dict(job_id='job', index=0, launch_id='launch', surface_id='surface',
                     workspace_id='workspace', bootstrap_pid=123, bootstrap_birth=[1, 2])
        self.claim = self.root/'claim.json'
        self.claim.write_text(json.dumps(claim))
        self.row = dict(claim, pid=123, birth=[1, 2], session_id='session',
                        writer_lock=str(self.root/'thread-writer-locks/session.lock'),
                        claim_sha256=hashlib.sha256(self.claim.read_bytes()).hexdigest())
        self.resolver = OriginalTranscript(self.row, self.claim)
        self.files = {}
        self.live = patch.object(self.resolver, '_live', side_effect=lambda *args: (self.sessions, dict(self.files)))
        self.live.start()
        self.addCleanup(self.live.stop)

    def rollout(self, name='rollout-session.jsonl'):
        path = self.sessions/name
        path.write_text('')
        info = path.stat()
        self.files[path] = dict(device=info.st_dev, inode=info.st_ino)
        return path

    def test_late_writable_original_then_disappearance(self):
        self.assertIsNone(self.resolver())
        path = self.rollout()
        self.assertEqual(self.resolver(), path)
        self.files.clear()
        with self.assertRaises(ValueError):
            self.resolver()

    def test_unowned_file_not_discovered_and_ambiguity_rejected(self):
        path = self.rollout()
        self.files.clear()
        self.assertIsNone(self.resolver())
        self.rollout('second-session.jsonl')
        info = path.stat()
        self.files[path] = dict(device=info.st_dev, inode=info.st_ino)
        with self.assertRaises(ValueError):
            self.resolver()

    def test_final_writer_drift_rejected(self):
        self.rollout()
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, dict(self.files)), (self.sessions, {})]):
            with self.assertRaises(ValueError):
                self.resolver()

    def test_incomplete_inventory_never_returns_cached_path(self):
        from ccc_codex_queue import IncompleteVnodeRead, VnodeInventoryChanged
        path = self.rollout()
        self.assertEqual(self.resolver(), path)
        for error in (IncompleteVnodeRead(9, 'closed fd'), VnodeInventoryChanged('changed')):
            for final in (False, True):
                effects = [(self.sessions, dict(self.files)), error] if final else [error]
                with patch.object(self.resolver, '_live', side_effect=effects):
                    self.assertIsNone(self.resolver())
                self.assertEqual(self.resolver(), path)
        self.files.clear()
        with self.assertRaises(ValueError):
            self.resolver()

    def test_claim_change_rejected(self):
        self.claim.write_text('{}')
        with self.assertRaises(ValueError):
            self.resolver()

    def test_replaced_rollout_rejected(self):
        path = self.rollout()
        self.resolver()
        path.rename(self.sessions/'saved')
        self.rollout()
        with self.assertRaises(ValueError):
            self.resolver()

    def test_binding_loss_diagnostic_distinguishes_closed_deleted_and_replaced(self):
        path = self.rollout()
        self.resolver()
        original_inode = path.stat().st_ino
        self.files.clear()
        with self.assertRaises(ValueError) as closed:
            self.resolver()
        diagnostic = closed.exception.rollout_event
        self.assertEqual(diagnostic['index'], 0)
        self.assertEqual(diagnostic['previous_path_on_disk']['inode'], original_inode)
        self.assertIsNone(diagnostic['previous_path_writable'])
        path.rename(self.sessions/'saved')
        with self.assertRaises(ValueError) as deleted:
            self.resolver()
        self.assertEqual(deleted.exception.rollout_event['previous_path_on_disk']['errno'], 2)
        self.rollout()
        with self.assertRaises(ValueError) as replaced:
            self.resolver()
        self.assertNotEqual(replaced.exception.rollout_event['observed']['inode'], original_inode)
        json.dumps(replaced.exception.rollout_event)
