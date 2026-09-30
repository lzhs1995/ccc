"""A repair may adopt only an idle, never-dispatched original N generation."""
from dataclasses import asdict
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from ccc_access_budget import AccessBudget, Policy
import ccc_access_service as service
import cmux_codex_watch as core


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.config = self.base / 'ccc/config.json'
        self.policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()), max_attempts=None, attempt_mode='sustained')
        self.directory = service.job_root(self.config, self.policy.job_id)
        self.directory.mkdir(parents=True)
        source = self.base / 'old-source'
        source.mkdir()
        for name in service.fingerprint():
            shutil.copyfile(Path(service.__file__).parent / name, source / name)
        with (source / 'ccc_access_gateway.py').open('a') as handle:
            handle.write('\n# distinct prior test generation\n')
        self.old = {'pid': 123456789, 'birth': [1, 2], 'instance': str(uuid.uuid4()),
                    'port': 23456, 'source': service.fingerprint(source),
                    'source_path': str(source / 'ccc_access_service.py')}
        generation = hashlib.sha256(json.dumps(self.old['source'], sort_keys=True).encode()).hexdigest()
        self.old_root = self.config.parent / 'access-gateway' / ('runtime-' + generation)
        self.old_root.mkdir(parents=True)
        core.atomic_write_json(self.old_root / 'owner.json', self.old)
        self.descriptor = {'version': service.VERSION, 'mode': service.MODE,
            'config_path': str(self.config), 'policy': asdict(self.policy),
            'gateway_instance': self.old['instance'], 'port': self.old['port']}
        service.create_private(self.directory / 'access.json', self.descriptor)
        self.job = {'id': self.policy.job_id, 'workspace_id': self.policy.workspace_id,
            'access_mode': service.MODE, 'access_policy': {'mode': service.MODE, 'version': service.VERSION,
                'max_attempts': None, 'max_output_tokens': 128,
                'descriptor_sha256': service.descriptor_sha(self.descriptor)}}
        core.atomic_write_json(self.directory / 'job.json', self.job)
        self.journal = self.directory / 'access-journal.jsonl'
        budget = AccessBudget(self.journal, self.policy, create=True)
        try:
            for slot in range(50):
                session = str(uuid.uuid4())
                reservation = budget.reserve(slot, session)
                budget.finish(reservation, 'cancelled_before_dispatch')
                service.create_private(self.directory / f'access-session-{slot}.json',
                    {'job_id': self.policy.job_id, 'workspace_id': self.policy.workspace_id,
                     'index': slot, 'session_id': session})
        finally:
            budget.close()
        store = core.ConfigStore(self.config)
        store.mutate(lambda c: c.update(mode='armed', global_paused=False, workspace_rules=[{
            'workspace_id': self.policy.workspace_id, 'active_batch_id': self.policy.job_id,
            'enabled': True, 'paused': False}]))
        self.plan = {'version': 1, 'purpose': 'recover-sustained-before-first-dispatch',
            'config_path': str(self.config), 'config_sha256': self.sha(self.config),
            'source': service.fingerprint(), 'old_owner': self.old,
            'restore_jobs': [self.policy.job_id], 'jobs': {self.policy.job_id: {
                'descriptor_sha256': service.descriptor_sha(self.descriptor),
                'job_sha256': self.sha(self.directory / 'job.json'),
                'journal': service.cancelled_setup_history(self.journal, self.policy),
                'bindings': {str(i): self.sha(self.directory / f'access-session-{i}.json') for i in range(50)}}}}
        self.path = self.base / 'recovery-plan.json'
        core.atomic_write_json(self.path, self.plan)

    @staticmethod
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def validate(self, *, birth=None):
        with patch('ccc_guard_scope.birth', return_value=birth):
            return service.validate_recovery_plan(self.config, self.path)

    def test_valid_repair_keeps_every_original_journal_byte_and_attempt(self):
        before = self.journal.read_bytes()
        plan, root = self.validate()
        self.assertEqual(root, self.old_root)
        self.assertEqual(plan['jobs'][self.policy.job_id]['journal']['attempts'], 50)
        self.assertEqual(self.journal.read_bytes(), before)
        budget = AccessBudget(self.journal, self.policy)
        try:
            self.assertEqual(budget.snapshot()['attempts'], 50)
            self.assertEqual(budget.snapshot()['in_flight'], 0)
            self.assertFalse(budget.snapshot()['fault'])
        finally:
            budget.close()
        self.assertEqual(self.journal.read_bytes(), before)

    def test_original_owner_must_be_stopped_before_replacement_starts(self):
        with self.assertRaisesRegex(ValueError, 'still alive'):
            self.validate(birth=self.old['birth'])

    def test_changed_source_descriptor_job_binding_or_configuration_refuses_adoption(self):
        files = [self.config, self.directory / 'access.json', self.directory / 'job.json',
                 self.directory / 'access-session-49.json', self.old_root / 'owner.json',
                 Path(self.old['source_path'])]
        for path in files:
            before = path.read_bytes()
            try:
                if path.suffix == '.json':
                    value = json.loads(before)
                    value['changed'] = True
                    core.atomic_write_json(path, value)
                else:
                    path.write_bytes(before + b'\n# changed\n')
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    self.validate()
            finally:
                path.write_bytes(before)

    def test_new_active_job_cannot_join_between_inventory_and_stopping_the_gateway(self):
        jid = str(uuid.uuid4())
        new_dir = service.job_root(self.config, jid)
        new_dir.mkdir(parents=True)
        service.create_private(new_dir / 'access.json', {'gateway_instance': self.old['instance']})
        core.ConfigStore(self.config).mutate(lambda c: c['workspace_rules'].append({
            'workspace_id': str(uuid.uuid4()), 'active_batch_id': jid}))
        self.plan['config_sha256'] = self.sha(self.config)
        core.atomic_write_json(self.path, self.plan)
        with self.assertRaisesRegex(ValueError, 'new active job'):
            self.validate()

    def test_paused_original_job_is_not_reactivated_by_repair(self):
        core.ConfigStore(self.config).mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        self.plan['config_sha256'] = self.sha(self.config)
        core.atomic_write_json(self.path, self.plan)
        with self.assertRaisesRegex(ValueError, 'unauthorized'):
            self.validate()

    def test_inactive_historical_job_on_same_instance_must_not_be_omitted(self):
        jid = str(uuid.uuid4())
        directory = service.job_root(self.config, jid)
        directory.mkdir()
        service.create_private(directory / 'access.json', {'gateway_instance': self.old['instance']})
        with self.assertRaisesRegex(ValueError, 'historical job'):
            self.validate()

    def test_missing_descriptor_with_n_mode_is_not_treated_as_b(self):
        jid = str(uuid.uuid4())
        directory = service.job_root(self.config, jid)
        directory.mkdir()
        core.atomic_write_json(directory / 'job.json', {'access_mode': service.MODE})
        with self.assertRaisesRegex(ValueError, 'no descriptor'):
            self.validate()

    def test_dispatched_or_unresolved_requests_are_never_eligible(self):
        original = self.journal.read_bytes()
        records = [json.loads(line) for line in original.splitlines()]
        for outcome in ('rejected', 'complete', 'uncertain', 'missing', 'truncated', 'null'):
            rows = copy.deepcopy(records)
            if outcome == 'missing':
                rows.pop()
            elif outcome not in ('truncated', 'null'):
                rows[-1]['outcome'] = outcome
            raw = b''.join((json.dumps(row) + '\n').encode() for row in rows)
            if outcome == 'truncated':
                raw = raw[:-1]
            elif outcome == 'null':
                raw = raw[:raw.index(b'\n') + 1] + b'null\n' + raw[raw.index(b'\n') + 1:]
            self.journal.write_bytes(raw)
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                service.cancelled_setup_history(self.journal, self.policy)
        self.journal.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
