"""Admission/ownership boundaries with real local locks and Unix endpoints."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import ccc_standby_factory as factory
from ccc_standby_service import request


class FactoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ccc-factory-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / 'config.json'
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.wid = str(uuid.uuid4())
        self.wid = getattr(self, 'controller_id', str)(self.wid)
        config = factory.core.default_config()
        config.update(mode='armed', global_paused=False, targets=[], workspace_rules=[{
            'workspace_id': self.wid, 'enabled': True, 'paused': False}])
        factory.core.atomic_write_json(self.config, config)
        self.store = factory.core.ConfigStore(self.config)
        self.client = Mock()
        self.client.workspace_tree.return_value = {'windows': [{'id': str(uuid.uuid4()),
            'workspaces': [{'id': self.wid, 'panes': [{'id': str(uuid.uuid4())}]}]}]}
        self.pin = Mock()
        self.pin.current.return_value = 'a' * 64
        self.reader = Mock(return_value=None)
        self.drafts = []
        self.capture_hook = lambda job: None
        def capture(job, *, environment):
            self.assertEqual(environment, {'HOME': str(self.root)})
            self.drafts.append(copy.deepcopy(job))
            for i in range(50):
                self.assertTrue(factory.batch.working_directory(self.config, job['id'], i).is_dir())
            self.capture_hook(job)
            return self.pin, self.reader
        self.capture = Mock(side_effect=capture)
        p = patch('ccc_standby_prepare.RolloutInventory')
        self.inventory = p.start(); self.addCleanup(p.stop)
        mkdtemp = tempfile.mkdtemp
        p = patch.object(factory.tempfile, 'mkdtemp', side_effect=lambda **kw:
            mkdtemp(prefix='endpoint-', dir=str(self.root)))
        p.start(); self.addCleanup(p.stop)

    def admit(self, **kwargs):
        owner = factory.admit(self.config, self.wid, mode=kwargs.pop('mode', 'b'),
            client=self.client, capture_sources=self.capture, sessions_root=self.sessions,
            target_environment={'HOME': str(self.root)}, **kwargs)
        self.addCleanup(owner.close)
        return owner

    def revoke(self):
        self.store.mutate(lambda cfg: cfg.update(global_paused=True))

    def previous(self, status='complete', standby=False):
        jid = str(uuid.uuid4())
        job = {'id': jid, 'workspace_id': self.wid, 'status': status, 'slots': []}
        if standby:
            job['standby_policy'] = factory.POLICY
        path = factory.batch.job_path(self.config, jid)
        factory.core.atomic_write_json(path, job)
        self.store.mutate(lambda cfg: cfg['workspace_rules'][0].update(
            active_batch_id=jid, last_batch_id=jid))
        return path

    def test_admission_publishes_original_owner_without_native_or_ready(self):
        owner = self.admit()
        prep = owner.service.preparation
        job_raw = prep.jobfile.read_bytes()
        job = json.loads(job_raw)
        self.assertEqual(job['standby_generation'], 'a' * 64)
        from ccc_standby_environment import signature
        self.assertEqual(job['standby_environment_sha256'], signature({'HOME': str(self.root)}))
        self.assertEqual(prep.bridge.environment.current(), {'HOME': str(self.root)})
        self.assertEqual(len({s['launch_id'] for s in job['slots']}), 50)
        self.assertNotIn('initial_prompt_policy', job)
        self.assertNotIn('ui_timing_origin', job)
        rule = self.store.load()['workspace_rules'][0]
        self.assertEqual((rule['active_batch_id'], rule['last_batch_id']), (job['id'], job['id']))
        self.assertEqual(owner.status()['state'], 'admitted')
        self.assertEqual(request(owner.endpoint.spec_path, owner.endpoint.sha256, 'status')['state'], 'admitted')
        self.assertTrue((prep.jobfile.parent / 'standby' / 'owner.json').is_file())
        self.assertEqual(prep.jobfile.read_bytes(), job_raw)
        self.client.new_codex_surface.assert_not_called()
        self.reader.assert_not_called()

    def test_native_n_preserves_explicit_mode(self):
        owner = self.admit(mode='N')
        self.assertEqual(owner.service.preparation.job['native_access_policy'], factory.batch.NATIVE_ACCESS_POLICY)
        self.assertEqual(owner.status()['mode'], 'N')

    def test_repeat_admission_with_live_worker_does_not_capture_or_replace(self):
        owner = self.admit()
        before = self.config.read_bytes()
        with self.assertRaises(RuntimeError): self.admit()
        self.assertEqual(self.config.read_bytes(), before)
        self.capture.assert_called_once()
        self.assertEqual(owner.status()['state'], 'admitted')

    def test_closed_owner_does_not_authorize_replacement(self):
        owner = self.admit()
        owner.close()
        with self.assertRaises(ValueError): self.admit()
        self.capture.assert_called_once()
        self.assertTrue(owner.service.preparation.jobfile.is_file())

    def test_existing_running_batch_preserved(self):
        path = self.previous(status='running')
        before = path.read_bytes()
        with self.assertRaises(ValueError): self.admit()
        self.assertEqual(path.read_bytes(), before)
        self.capture.assert_not_called()

    def test_even_complete_standby_needs_its_separate_settlement(self):
        self.previous(standby=True)
        with self.assertRaises(ValueError): self.admit()
        self.capture.assert_not_called()

    def test_finished_ordinary_job_retains_original_bytes(self):
        path = self.previous()
        before = path.read_bytes()
        owner = self.admit()
        self.assertNotEqual(owner.service.preparation.job['id'], path.parent.name)
        self.assertEqual(path.read_bytes(), before)

    def test_shared_parent_writable_by_others_rejected(self):
        path = self.previous()
        path.parent.parent.chmod(0o777)
        with self.assertRaises(ValueError): self.admit()
        self.capture.assert_not_called()

    def test_shared_parent_symlink_rejected(self):
        path = self.previous()
        batches = path.parent.parent
        moved = batches.with_name('moved-batches')
        batches.rename(moved)
        batches.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError): self.admit()
        self.capture.assert_not_called()

    def test_source_capture_revocation_prevents_admission_and_closes_pin(self):
        self.capture_hook = lambda _: self.revoke()
        with self.assertRaises(ValueError): self.admit()
        self.pin.close.assert_called_once()
        self.assertNotIn('active_batch_id', self.store.load()['workspace_rules'][0])
        self.assertEqual(len(list(self.root.glob('workspace-batches/*/admission-failed.json'))), 1)
        self.client.new_codex_surface.assert_not_called()

    def test_cancel_and_resume_during_capture_does_not_restore_old_request(self):
        self.capture_hook = lambda _: self.store.mutate(lambda cfg:
            cfg['workspace_rules'][0].update(batch_cancelled_at=123, paused=False))
        with self.assertRaises(ValueError): self.admit()
        self.assertNotIn('active_batch_id', self.store.load()['workspace_rules'][0])

    def test_revoke_after_job_write_prevents_config_commit(self):
        real = factory.write_once
        def write(path, value):
            raw = real(path, value)
            if path.name == 'job.json': self.revoke()
            return raw
        with patch.object(factory, 'write_once', side_effect=write):
            with self.assertRaises(ValueError): self.admit()
        self.assertNotIn('active_batch_id', self.store.load()['workspace_rules'][0])

    def test_capture_must_supply_native_readiness_reader(self):
        self.reader = None
        with self.assertRaises(ValueError): self.admit()
        self.pin.close.assert_called_once()
        self.client.new_codex_surface.assert_not_called()

    def test_endpoint_failure_preserves_job_and_closes_service_worker(self):
        with patch.object(factory, 'ServiceEndpoint', side_effect=OSError('no socket')):
            with self.assertRaises(OSError): self.admit()
        jid = self.store.load()['workspace_rules'][0]['active_batch_id']
        path = factory.batch.job_path(self.config, jid)
        self.assertTrue(path.is_file())
        self.assertTrue((path.parent / 'admission-failed.json').is_file())
        with factory.core.FileLock(path.parent / 'worker.lock', timeout_sec=0): pass
        self.pin.close.assert_called_once()
        with self.assertRaises(ValueError): self.admit()

    def test_explicit_prepare_uses_same_service_once(self):
        owner = self.admit()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def prepare():
            entered.set()
            if not release.wait(timeout=3): raise RuntimeError('test release missing')
        with patch.object(owner.service, '_prepare', side_effect=prepare) as run:
            owner.prepare()
            self.assertTrue(entered.wait(timeout=2))
            owner.prepare()
            release.set()
            owner.service._future.result(timeout=2)
        run.assert_called_once()
        self.client.new_codex_surface.assert_not_called()

    def test_source_drift_before_commit_never_authorizes_new_job(self):
        self.pin.current.side_effect = ['a' * 64, 'b' * 64]
        with self.assertRaises(ValueError): self.admit()
        self.assertNotIn('active_batch_id', self.store.load()['workspace_rules'][0])


if __name__ == '__main__':
    unittest.main()
