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

    def test_late_writable_original_then_close_and_reopen(self):
        self.assertIsNone(self.resolver())
        path = self.rollout()
        self.assertEqual(self.resolver(), path)
        original = dict(self.files)
        self.files.clear()
        self.assertIsNone(self.resolver())
        self.assertIsNotNone(self.resolver.last_pending_event)
        self.files.update(original)
        self.assertEqual(self.resolver(), path)
        self.assertIsNone(self.resolver.last_pending_event)

    def test_unowned_file_not_discovered_and_ambiguity_rejected(self):
        path = self.rollout()
        self.files.clear()
        self.assertIsNone(self.resolver())
        self.rollout('second-session.jsonl')
        info = path.stat()
        self.files[path] = dict(device=info.st_dev, inode=info.st_ino)
        with self.assertRaises(ValueError):
            self.resolver()

    def test_final_rollout_fd_close_waits_without_resolved_path(self):
        path = self.rollout()
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, dict(self.files)), (self.sessions, {})]):
            self.assertIsNone(self.resolver())
        detail = self.resolver.last_pending_event
        self.assertEqual(detail['kind'], 'original_transcript_writable_pending')
        self.assertEqual(detail['path'], str(path))
        self.assertEqual(detail['expected'], self.files[path])
        self.assertEqual(detail['path_on_disk']['inode'], path.stat().st_ino)
        self.assertIsNone(detail['final_path_writable'])
        self.assertEqual(detail['pid'], self.row['pid'])
        self.assertGreater(detail['wall'], 0)
        self.assertGreater(detail['monotonic'], 0)
        self.assertIsNone(self.resolver._resolved)
        json.dumps(detail)
        self.assertEqual(self.resolver(), path)
        self.assertIsNone(self.resolver.last_pending_event)

    def test_final_disk_change_reports_identity_and_never_binds(self):
        for change in ('deleted', 'replaced'):
            with self.subTest(change=change):
                # Each scenario owns a new witness; a rejected provisional
                # inode deliberately remains pinned in the previous resolver.
                self.resolver = OriginalTranscript(self.row, self.claim)
                path = self.rollout()
                original = dict(self.files)
                def final(*args):
                    path.rename(self.sessions/'saved-final')
                    if change == 'replaced':
                        path.write_text('replacement')
                    return self.sessions, original
                with patch.object(self.resolver, '_live', side_effect=[
                        (self.sessions, original), None]) as live:
                    live.side_effect = lambda *args: (self.sessions, original) if live.call_count == 1 else final()
                    with self.assertRaises(ValueError) as caught:
                        self.resolver()
                detail = caught.exception.rollout_event
                if change == 'deleted':
                    self.assertEqual(detail['path_on_disk']['errno'], 2)
                else:
                    self.assertNotEqual(detail['path_on_disk']['inode'], original[path]['inode'])
                self.assertEqual(detail['final_path_writable'], original[path])
                self.assertIsNone(self.resolver._resolved)
                json.dumps(detail)

    def test_append_during_resolution_preserves_original_binding(self):
        path = self.rollout()
        def live(*args):
            with path.open('a') as handle:
                handle.write('appended\n')
            return self.sessions, dict(self.files)
        with patch.object(self.resolver, '_live', side_effect=live):
            self.assertEqual(self.resolver(), path)
        self.assertEqual(path.read_text(), 'appended\nappended\n')

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
        self.assertIsNone(self.resolver())

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
        self.assertIsNone(self.resolver())
        diagnostic = self.resolver.last_pending_event
        self.assertEqual(diagnostic['index'], 0)
        self.assertEqual(diagnostic['previous_path_on_disk']['inode'], original_inode)
        self.assertIsNone(diagnostic['previous_path_writable'])
        self.assertGreater(diagnostic['wall'], 0)
        self.assertGreater(diagnostic['monotonic'], 0)
        path.rename(self.sessions/'saved')
        with self.assertRaises(ValueError) as deleted:
            self.resolver()
        self.assertEqual(deleted.exception.rollout_event['previous_path_on_disk']['errno'], 2)
        self.rollout()
        with self.assertRaises(ValueError) as replaced:
            self.resolver()
        self.assertNotEqual(replaced.exception.rollout_event['observed']['inode'], original_inode)
        json.dumps(replaced.exception.rollout_event)

    def provisional(self):
        path = self.rollout()
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, dict(self.files)), (self.sessions, {})]):
            self.assertIsNone(self.resolver())
        self.assertIsNone(self.resolver._resolved)
        return path

    def test_provisional_binding_rejects_subsequent_replacement(self):
        path = self.provisional()
        path.rename(self.sessions/'saved')
        self.rollout()
        with self.assertRaises(ValueError):
            self.resolver()

    def test_provisional_binding_rejects_alternate_writable_path(self):
        self.provisional()
        self.files.clear()
        self.rollout('other-session.jsonl')
        with self.assertRaises(ValueError):
            self.resolver()

    def test_missing_fd_still_checks_final_live_binding(self):
        self.provisional()
        self.files.clear()
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, {}), ValueError('original writer changed')]) as live:
            with self.assertRaisesRegex(ValueError, 'writer changed'):
                self.resolver()
            self.assertEqual(live.call_count, 2)

    def test_missing_fd_still_checks_final_claim(self):
        self.provisional()
        self.files.clear()
        def live(*args):
            self.claim.write_text('{}')
            return self.sessions, {}
        with patch.object(self.resolver, '_live', side_effect=live):
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                self.resolver()

    def test_missing_fd_rejects_final_disk_replacement(self):
        path = self.provisional()
        self.files.clear()
        def live(*args):
            if fake.call_count == 2:
                path.rename(self.sessions/'saved')
                path.write_text('replacement')
            return self.sessions, {}
        with patch.object(self.resolver, '_live', side_effect=live) as fake:
            with self.assertRaises(ValueError):
                self.resolver()

    def test_missing_fd_rejects_symlink_even_to_original_inode(self):
        path = self.provisional()
        self.files.clear()
        saved = self.sessions/'saved'
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(ValueError):
            self.resolver()

    def test_final_alternate_rollout_is_not_pending(self):
        self.rollout()
        original = dict(self.files)
        self.files.clear()
        self.rollout('other-session.jsonl')
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, original), (self.sessions, dict(self.files))]):
            with self.assertRaises(ValueError):
                self.resolver()

    def test_reopened_only_at_final_observation_still_waits(self):
        path = self.provisional()
        original = dict(self.files)
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, {}), (self.sessions, original)]):
            self.assertIsNone(self.resolver())
        self.assertEqual(self.resolver(), path)

    def test_initial_observation_pins_before_incomplete_final_inventory(self):
        from ccc_codex_queue import IncompleteVnodeRead
        path = self.rollout()
        with patch.object(self.resolver, '_live', side_effect=[
                (self.sessions, dict(self.files)), IncompleteVnodeRead(9, 'closed fd')]):
            self.assertIsNone(self.resolver())
        path.rename(self.sessions/'saved')
        self.rollout()
        with self.assertRaises(ValueError):
            self.resolver()
