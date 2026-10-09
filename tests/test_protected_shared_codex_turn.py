"""Protected rollouts remain usable only while their original native writer holds them."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

import ccc_shared_codex_turn as shared
import ccc_request_key_binding as connection


class ProtectedTurnTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.protected = self.root / 'protected' / 'sessions'
        self.sid = str(uuid.uuid4())
        self.target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        self.process = dict(pid=41, birth=[100, 4], process_start=100,
                            surface_id='surface', environment_workspace_id='workspace', remote=False)
        self.selection = ('ok', self.sid, ('stable',))
        self.files = {}
        self.lock = self.root / 'thread-writer-locks' / (self.sid + '.lock')
        self.lock.parent.mkdir()
        self.lock.touch()
        self.hold(self.lock)
        self.path = self.make_rollout('2026/10/10')
        self.patch(shared, '_protected_root', return_value=self.protected)
        self.patch(shared.scope, 'process', side_effect=lambda pid: dict(self.process) if pid == 41 else None)
        self.birth = self.patch(shared.scope, 'birth',
                                side_effect=lambda pid, **kw: self.process['birth'] if pid == 41 else [50, 1])
        self.patch(shared, 'read_foreground', side_effect=lambda *a: self.selection)
        self.writers = self.patch(shared, 'writer_candidates', return_value=())
        self.inventory = self.patch(shared.native, 'process_writable_files',
                                    side_effect=lambda pid, **kw: dict(self.files) if pid == 41 else {})
        # connected_writer is deliberately real: the observed failure was the
        # self-writer/no-state-database path, not the mocked shared-backend case.

    def patch(self, obj, name, **kw):
        p = patch.object(obj, name, **kw)
        self.addCleanup(p.stop)
        return p.start()

    def metadata(self, **payload):
        return (json.dumps({'type': 'session_meta', 'payload': {'id': self.sid, **payload}}) + '\n').encode()

    def make_rollout(self, day):
        parent = self.protected / day
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / ('new-' + uuid.uuid4().hex)
        temporary.write_bytes(self.metadata() +
                              b'{"type":"event_msg","timestamp":"2026-10-10T00:00:00Z",'
                              b'"payload":{"type":"task_complete","turn_id":"turn"}}\n')
        info = temporary.stat()
        name = f'rollout-{day.replace("/", "-")}T00-31-06-{self.sid}.jsonl.{info.st_dev}-{info.st_ino}'
        path = temporary.rename(parent / name)
        self.hold(path)
        return path

    def hold(self, path):
        info = path.stat()
        self.files[path] = {'device': info.st_dev, 'inode': info.st_ino}

    def bind(self):
        return shared.binding(self.target, 41, self.sessions)[0]

    def rename(self, path):
        identity = self.files.pop(self.path)
        self.path.rename(path)
        self.path = path
        self.files[path] = identity

    def test_self_writer_without_database_reads_foreground_turn(self):
        proof = self.bind()
        self.assertIsNotNone(proof)
        self.assertEqual(proof['writer_pid'], 41)
        self.assertEqual(proof['path'], self.path)
        self.assertIsNone(proof['rollout_database'])
        self.assertEqual(set(proof['paths']), {self.lock, self.path})
        turn = shared.current_turn(self.target, 41, self.sessions)
        self.assertEqual((turn['kind'], turn['session_id']), ('task_complete', self.sid))
        self.assertIsNotNone(shared.binding_for_turn(self.target, turn))

    def test_closed_backup_is_not_discovered_or_accepted_from_database(self):
        del self.files[self.path]
        self.assertIsNone(self.bind())
        db = self.root / 'state_5.sqlite'
        with closing(sqlite3.connect(db)) as conn:
            with conn:
                conn.execute('CREATE TABLE threads(id,rollout_path)')
                conn.execute('INSERT INTO threads VALUES (?,?)', (self.sid, str(self.path)))
        self.hold(db)
        self.assertIsNone(self.bind())

    def test_wrong_sid_filename_rejects(self):
        self.rename(self.path.with_name(self.path.name.replace(self.sid, str(uuid.uuid4()))))
        self.assertIsNone(self.bind())

    def test_wrong_device_or_inode_suffix_rejects(self):
        original = self.path
        identity = self.files[original]
        for suffix in (f"{identity['device'] + 1}-{identity['inode']}",
                       f"{identity['device']}-{identity['inode'] + 1}"):
            with self.subTest(suffix=suffix):
                self.rename(original.with_name(original.name.split('.jsonl.')[0] + '.jsonl.' + suffix))
                self.assertIsNone(self.bind())
                self.rename(original)

    def test_noncanonical_or_trailing_filename_rejects(self):
        original = self.path
        for name in (original.name + '.extra', original.name.replace('2026-10-10', '2026-10-11'),
                     original.name.replace('rollout-', 'other-')):
            with self.subTest(name=name):
                self.rename(original.with_name(name))
                self.assertIsNone(self.bind())
                self.rename(original)

    def test_outside_protected_root_rejects(self):
        self.rename(self.root / self.path.name)
        self.assertIsNone(self.bind())

    def test_leaf_symlink_rejects(self):
        old = self.path.with_name('original')
        self.path.rename(old)
        self.path.symlink_to(old)
        self.assertIsNone(self.bind())

    def test_ancestor_symlink_rejects(self):
        parent = self.path.parent
        old = parent.with_name('original')
        parent.rename(old)
        parent.symlink_to(old, target_is_directory=True)
        self.assertIsNone(self.bind())

    def test_non_regular_file_rejects_without_blocking(self):
        self.path.unlink()
        temporary = self.path.with_name('pipe')
        os.mkfifo(temporary)
        info = temporary.stat()
        del self.files[self.path]
        self.path = temporary.rename(temporary.with_name(
            f'rollout-2026-10-10T00-31-06-{self.sid}.jsonl.{info.st_dev}-{info.st_ino}'))
        self.hold(self.path)
        self.assertIsNone(self.bind())

    def test_missing_malformed_foreign_and_oversized_metadata_reject(self):
        cases = [b'', b'{}\n', b'[]\n', b'{bad json}\n', b'\xff\n',
                 self.metadata(id=str(uuid.uuid4())), self.metadata().rstrip(b'\n'),
                 self.metadata(padding='x' * 65536),
                 json.dumps({'type': 'event_msg', 'payload': {'id': self.sid}}).encode() + b'\n']
        for i, data in enumerate(cases):
            with self.subTest(case=i):
                self.path.write_bytes(data)
                self.assertIsNone(self.bind())

    def test_metadata_limit_includes_newline(self):
        raw = self.metadata(padding='')
        data = self.metadata(padding='x' * (65536 - len(raw)))
        self.assertEqual(len(data), 65536)
        self.path.write_bytes(data)
        proof = self.bind()
        self.assertIsNotNone(proof)
        self.assertEqual(proof['protected_metadata']['bytes'], 65536)
        self.path.write_bytes(data[:-1] + b' \n')
        self.assertIsNone(self.bind())

    def test_missing_and_wrong_writer_lock_reject(self):
        identity = self.files.pop(self.lock)
        self.assertIsNone(self.bind())
        self.files[self.lock] = {**identity, 'inode': identity['inode'] + 1}
        self.assertIsNone(self.bind())

    def test_lost_writable_descriptor_invalidates_proof(self):
        proof = self.bind()
        del self.files[self.path]
        self.assertFalse(shared.valid(self.target, proof))

    def test_two_distinct_held_rollouts_are_ambiguous(self):
        proof = self.bind()
        self.make_rollout('2026/10/11')
        self.assertFalse(shared.valid(self.target, proof))
        self.assertIsNone(self.bind())

    def test_file_replacement_invalidates_proof(self):
        proof = self.bind()
        self.path.rename(self.path.with_name('old'))
        self.path.write_bytes(self.metadata())
        self.assertFalse(shared.valid(self.target, proof))

    def test_ancestor_replacement_with_same_file_inode_invalidates_proof(self):
        proof = self.bind()
        parent = self.path.parent
        old = parent.with_name('old-directory')
        parent.rename(old)
        parent.mkdir()
        os.link(old / self.path.name, self.path)
        self.assertEqual(self.path.stat().st_ino, proof['rollout_identity']['inode'])
        self.assertFalse(shared.valid(self.target, proof))

    def test_same_inode_metadata_rewrite_invalidates_proof(self):
        proof = self.bind()
        self.path.write_bytes(self.metadata(changed=True))
        self.assertFalse(shared.valid(self.target, proof))

    def test_legitimate_append_keeps_binding_valid(self):
        proof = self.bind()
        with self.path.open('ab') as f:
            f.write(b'{"type":"event_msg","payload":{"type":"task_started"}}\n')
        self.assertTrue(shared.valid(self.target, proof))
        self.assertEqual(self.bind()['protected_metadata'], proof['protected_metadata'])

    def test_pid_reuse_invalidates_proof(self):
        proof = self.bind()
        self.process['birth'] = [200, 9]
        self.assertFalse(shared.valid(self.target, proof))

    def test_foreground_change_invalidates_proof(self):
        proof = self.bind()
        self.selection = ('ok', str(uuid.uuid4()), ('changed',))
        self.assertFalse(shared.valid(self.target, proof))

    def test_shared_writer_still_requires_reciprocal_socket(self):
        self.writers.return_value = (81,)
        self.inventory.side_effect = lambda pid, **kw: dict(self.files) if pid == 81 else {}
        pairs = self.patch(connection, 'native_unix_pairs',
                           side_effect=lambda pid: {(1, 2)} if pid == 41 else {(2, 1)})
        proof = self.bind()
        self.assertIsNotNone(proof)
        self.assertEqual(proof['writer_pid'], 81)
        pairs.side_effect = None
        pairs.return_value = set()
        self.assertFalse(shared.valid(self.target, proof))
        self.assertIsNone(self.bind())

    def test_same_inode_rewrite_during_metadata_read_rejects(self):
        proof = self.bind()
        loads = json.loads
        def rewrite(raw):
            value = loads(raw)
            self.path.write_bytes(self.metadata(changed=True))
            return value
        self.patch(shared.json, 'loads', side_effect=rewrite)
        self.assertFalse(shared.valid(self.target, proof))

    def test_second_inventory_must_still_have_one_matching_rollout(self):
        proof = self.bind()
        calls = 0
        def inventory(pid, **kw):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.make_rollout('2026/10/11')
            return dict(self.files)
        self.inventory.side_effect = inventory
        self.assertFalse(shared.valid(self.target, proof))


if __name__ == '__main__':
    unittest.main()
