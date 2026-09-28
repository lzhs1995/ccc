"""Original idle identity only; no native process is launched by these tests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import ccc_standby_identity as identity
import ccc_workspace_batch as batch
from ccc_native_standby import POLICY


class StandbyIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        locks = self.root / 'thread-writer-locks'
        locks.mkdir()
        self.session = '01a0e8aa-25f3-79d1-ab3f-bbe626e84426'
        self.lock = locks / (self.session + '.lock')
        self.lock.write_bytes(b'')
        self.tui = self.root / 'tui.jsonl'
        self.events = [dict(ts='2026-09-29T00:00:00Z', dir='meta', kind='session_start', cwd=str(self.root)),
                       dict(dir='to_tui', kind='app_event', variant='SkillsListLoaded'),
                       dict(dir='to_tui', kind='app_event', variant='StartupThreadStarted')]
        self.save_events()
        self.argv = ['/native/codex', '--cd', str(self.root)]
        self.expected = dict(job_id=str(uuid.uuid4()), index=0, launch_id=str(uuid.uuid4()),
                             surface_id=str(uuid.uuid4()).upper(), workspace_id=str(uuid.uuid4()).upper())
        self.claim = {**self.expected, 'policy': POLICY, 'state': 'exec_intent', 'argv': self.argv,
                      'bootstrap_pid': 2345, 'bootstrap_birth': [1000, 42], 'cwd': str(self.root),
                      'tui_log': str(self.tui), 'tui_log_identity': self.file_identity(self.tui), 'at': 1000}
        self.claim_path = self.root / 'claim.json'
        self.claim_path.write_text(json.dumps(self.claim))
        self.sha = hashlib.sha256(self.claim_path.read_bytes()).hexdigest()
        self.process = dict(pid=2345, birth=[1000, 42], argv=self.argv, remote=False,
                            surface_id=self.expected['surface_id'],
                            environment_workspace_id=self.expected['workspace_id'], cwd=str(self.root))
        self.files = {p: dict(zip(('device', 'inode'), self.file_identity(p))) for p in (self.lock, self.tui)}

    def file_identity(self, path):
        st = path.stat()
        return [st.st_dev, st.st_ino]

    def save_events(self):
        self.tui.write_text(''.join(json.dumps(e) + '\n' for e in self.events))

    def inspect(self, **changes):
        args = dict(claim_sha256=self.sha, expected=self.expected, expected_argv=self.argv,
                    sessions_root=self.sessions,
                    process_reader=lambda *a, **k: copy.deepcopy(self.process),
                    files_reader=lambda *a, **k: copy.deepcopy(self.files))
        args.update(changes)
        return identity.inspect_original(self.claim_path, **args)

    def test_original_writer_and_startup_prefix_bind_without_claiming_readiness(self):
        row = self.inspect()
        self.assertEqual(row['session_id'], self.session)
        self.assertEqual(row['birth'], [1000, 42])
        self.assertEqual(row['writer_identity'], self.file_identity(self.lock))
        self.assertTrue(row['startup_observed'])
        self.assertFalse(row['readiness_proven'])

    def test_microsecond_birth_change_between_inspections_is_rejected(self):
        calls = []
        def read(*args, **kwargs):
            value = copy.deepcopy(self.process)
            if calls:
                value['birth'][1] += 1
            calls.append(1)
            return value
        with self.assertRaises(ValueError):
            self.inspect(process_reader=read)

    def test_foreign_workspace_and_changed_argv_are_rejected(self):
        self.process['environment_workspace_id'] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.inspect()
        self.process['environment_workspace_id'] = self.expected['workspace_id']
        self.process['argv'] = ['/native/codex', 'resume', self.session]
        with self.assertRaises(ValueError):
            self.inspect()

    def test_readonly_or_ambiguous_writer_lock_is_rejected(self):
        self.files.pop(self.lock)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_lock_replacement_is_rejected(self):
        self.lock.rename(self.lock.with_suffix('.old'))
        self.lock.write_bytes(b'')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_existing_rollout_rejects_previously_used_session(self):
        (self.sessions / (self.session + '.jsonl')).write_text('{}\n')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_any_user_turn_before_activation_is_rejected(self):
        self.events.append(dict(dir='from_tui', kind='op', payload={
            'UserTurn': {'items': [{'type': 'text', 'text': batch.PROMPT}]}}))
        self.save_events()
        with self.assertRaises(ValueError):
            self.inspect()

    def test_user_turn_arriving_during_observation_is_rejected(self):
        calls = []
        def read(*args, **kwargs):
            if calls:
                self.events.append(dict(dir='from_tui', kind='op', payload={
                    'UserTurn': {'items': [{'type': 'text', 'text': batch.PROMPT}]}}))
                self.save_events()
            calls.append(1)
            return copy.deepcopy(self.files)
        with self.assertRaises(ValueError):
            self.inspect(files_reader=read)

    def test_session_switch_or_unfinished_startup_is_rejected(self):
        self.events.pop()
        self.save_events()
        with self.assertRaises(ValueError):
            self.inspect()
        self.events.append(dict(dir='to_tui', kind='app_event', variant='ResumeSession'))
        self.save_events()
        with self.assertRaises((ValueError, RuntimeError)):
            self.inspect()

    def test_changed_claim_hash_is_rejected(self):
        self.claim_path.write_text('{}')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_closed_rollout_created_during_inspection_is_rejected(self):
        calls = []
        def read(*args, **kwargs):
            if calls:
                (self.sessions / (self.session + '.jsonl')).write_text('{}\n')
            calls.append(1)
            return copy.deepcopy(self.files)
        with self.assertRaises(ValueError):
            self.inspect(files_reader=read)

    def test_partial_user_turn_tail_is_not_negative_input_evidence(self):
        with self.tui.open('ab') as handle:
            handle.write(b'{"dir":"from_tui","kind":"op","payload":{"UserTurn":')
        with self.assertRaises(ValueError):
            self.inspect()


if __name__ == '__main__':
    unittest.main()
