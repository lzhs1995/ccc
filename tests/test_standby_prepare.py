"""Preparation composition with an isolated real generation socket."""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import ccc_standby_prepare as prep
from tests.test_standby_bootstrap import descriptor


class PreparationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ccc-prep-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / 'config.json'
        self.job = descriptor(self.config)
        self.sid = str(uuid.uuid4())
        self.sid = getattr(self, 'controller_id', str)(self.sid)
        self.job['workspace_id'] = getattr(self, 'controller_id', str)(self.job['workspace_id'])
        self.wid = self.job['workspace_id']
        config = prep.core.default_config()
        config.update(mode='armed', global_paused=False, targets=[], workspace_rules=[{
            'workspace_id': self.wid, 'enabled': True, 'active_batch_id': self.job['id'],
            'batch_start_holds': {self.sid: {'job_id': self.job['id'], 'index': 0}}}])
        prep.core.atomic_write_json(self.config, config)
        self.jobfile = prep.batch.job_path(self.config, self.job['id'])
        prep.core.atomic_write_json(self.jobfile, self.job)
        self.directory = self.root / 'owner'
        self.directory.mkdir(mode=0o700)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.pin = Mock()
        self.pin.current.return_value = self.job['standby_generation']
        self.client = Mock()
        self.client.workspace_tree.return_value = {'windows': [{'id': str(uuid.uuid4()),
            'workspaces': [{'id': self.wid, 'panes': [{'id': str(uuid.uuid4()),
                'surfaces': [{'id': self.sid, 'type': 'terminal'}]}]}]}]}
        self.writes = []
        self.guard = None
        @contextlib.contextmanager
        def guarded(check):
            self.guard = check
            yield
        self.client.input_guard.side_effect = guarded
        def create(*args, **kwargs):
            if self.before_write:
                self.before_write()
            if not self.guard():
                raise ValueError('actual create guard refused')
            self.writes.append((args, kwargs))
            if self.unknown:
                raise OSError('ACK lost')
            return self.sid
        self.before_write = None
        self.unknown = False
        self.client.new_codex_surface.side_effect = create
        self.rollouts = Mock()
        p = patch.object(prep, 'RolloutInventory', return_value=self.rollouts)
        self.inventory = p.start(); self.addCleanup(p.stop)
        p = patch.object(prep, 'boot_id', return_value=self.job['standby_boot_id'])
        p.start(); self.addCleanup(p.stop)
        self.owner = prep.PreparationOwner(self.config, self.job['id'], directory=self.directory,
            client=self.client, source_pin=self.pin, sessions_root=self.sessions,
            target_environment={'HOME': str(self.root)})
        self.addCleanup(self.owner.close)

    def revoke(self):
        prep.core.ConfigStore(self.config).mutate(lambda cfg: cfg.update(global_paused=True))

    def test_original_creation_intent_precedes_write_and_is_never_replayed(self):
        def check():
            value = json.loads((self.directory / 'create-intent-0.json').read_bytes())
            self.assertEqual(value['launch_id'], self.job['slots'][0]['launch_id'])
            self.assertEqual(value['job_id'], self.job['id'])
        self.before_write = check
        self.assertEqual(self.owner.launch_one(0), self.sid)
        self.assertTrue(self.writes[0][1]['clean_shell'])
        self.assertIn(str(self.owner.bridge.spec_path), self.writes[0][0][3])
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(json.loads(self.jobfile.read_bytes()), self.job)

    def test_unknown_create_permanently_consumes_original(self):
        self.unknown = True
        with self.assertRaises(OSError): self.owner.launch_one(0)
        self.unknown = False
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertEqual(len(self.writes), 1)
        self.assertTrue((self.directory / 'create-intent-0.json').exists())
        self.assertFalse((self.directory / 'create-ack-0.json').exists())

    def test_pause_during_connection_refuses_create_and_does_not_revive(self):
        self.before_write = self.revoke
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        prep.core.ConfigStore(self.config).mutate(lambda cfg: cfg.update(global_paused=False))
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertFalse(self.writes)

    def test_job_change_observed_then_restore_does_not_revive(self):
        raw = self.jobfile.read_bytes()
        self.jobfile.write_bytes(raw + b' ')
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.jobfile.write_bytes(raw)
        with self.assertRaises(ValueError): self.owner.launch_one(0)
        self.assertFalse(self.writes)

    def test_missing_hold_is_denied(self):
        prep.core.ConfigStore(self.config).mutate(lambda cfg:
            cfg['workspace_rules'][0].update(batch_start_holds={}))
        self.assertFalse(self.owner._authorized(0, surface_id=self.sid))
        self.assertTrue(self.owner._failed.is_set())

    def test_connected_read_revocation_is_rechecked(self):
        def read(_):
            self.revoke()
            return self.client.workspace_tree.return_value
        self.client.workspace_tree.side_effect = read
        with patch.object(prep.core, 'find_main_surface', return_value={'workspace_id': self.wid}):
            self.assertFalse(self.owner._authorized(0, surface_id=self.sid, connected=self.client))

    def test_shared_inventory_is_passed_to_actual_identity_helper(self):
        self.owner.launch_one(0)
        argv = ['/private/native']
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        claim = {**self.owner.selected, 'index': 0, 'launch_id': self.job['slots'][0]['launch_id'],
            'target_environment_sha256': self.owner.environment_sha256, 'environment_sha256': 'f' * 64,
            'surface_id': self.sid, 'argv': argv, 'bootstrap_pid': 123, 'bootstrap_birth': [4, 5]}
        path.write_text(json.dumps(claim))
        with patch.object(prep.launch, 'launch_argv', return_value=argv), \
                patch('ccc_guard_scope.birth', return_value=[4, 5]), \
                patch('ccc_guard_scope.process', return_value={'pid': 123}), \
                patch('ccc_codex_queue.process_writable_files', return_value={
                    self.root / 'thread-writer-locks' / 'session.lock': {}}), \
                patch.object(prep, 'StandbyRefreshBarrier') as factory:
            factory.return_value.observe.return_value = {'readiness_proven': False}
            self.assertEqual(self.owner.poll(0), {'readiness_proven': False})
            kwargs = factory.call_args.kwargs
            self.assertIs(kwargs['inspect'].keywords['rollout_absent'], self.rollouts.absent)
            self.assertEqual(kwargs['claim_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.owner.poll(0)
            factory.assert_called_once()
            self.inventory.assert_called_once_with(self.sessions)

    def test_close_revokes_reader_and_releases_inventory_and_pin(self):
        from ccc_standby_bootstrap import live_reader
        _, read = live_reader(self.owner.bridge.spec_path, self.owner.bridge.sha256, 0, self.config)
        self.assertEqual(read(), self.job['standby_generation'])
        self.owner.close()
        with self.assertRaises((OSError, ValueError)): read()
        self.rollouts.close.assert_called_once()
        self.pin.close.assert_called_once()

    def writer_observation(self):
        self.owner.launch_one(0)
        argv = ['/private/native']
        path = prep.launch.claim_path(self.config, self.job['id'], 0)
        path.write_text(json.dumps({**self.owner.selected, 'index': 0,
            'launch_id': self.job['slots'][0]['launch_id'],
            'target_environment_sha256': self.owner.environment_sha256,
            'environment_sha256': 'f' * 64, 'surface_id': self.sid,
            'argv': argv, 'bootstrap_pid': 123, 'bootstrap_birth': [4, 5]}))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(prep.launch, 'launch_argv', return_value=argv))
        birth = stack.enter_context(patch('ccc_guard_scope.birth', return_value=[4, 5]))
        stack.enter_context(patch('ccc_guard_scope.process', return_value={'pid': 123}))
        files = stack.enter_context(patch('ccc_codex_queue.process_writable_files'))
        barrier = stack.enter_context(patch.object(prep, 'StandbyRefreshBarrier'))
        return files, barrier, birth

    def test_incomplete_writer_inventory_waits_then_uses_full_barrier_once(self):
        files, barrier, _ = self.writer_observation()
        files.side_effect = [OSError('incomplete vnode descriptor'), {
            self.root / 'thread-writer-locks' / 'session.lock': {}}]
        self.assertIsNone(self.owner.poll(0))
        barrier.assert_not_called()
        self.assertFalse(self.owner._failed.is_set())
        barrier.return_value.observe.return_value = {'fresh': True}
        self.assertEqual(self.owner.poll(0), {'fresh': True})
        barrier.assert_called_once()
        barrier.return_value.prepare.assert_called_once()
        self.assertEqual(len(self.writes), 1)

    def test_persistently_unknown_writer_inventory_never_sends(self):
        files, barrier, _ = self.writer_observation()
        files.side_effect = OSError('incomplete vnode descriptor')
        for _ in range(3):
            self.assertIsNone(self.owner.poll(0))
        barrier.assert_not_called()
        self.assertEqual(len(self.writes), 1)

    def test_writer_inventory_error_with_replaced_pid_is_terminal(self):
        files, barrier, birth = self.writer_observation()
        files.side_effect = OSError('incomplete vnode descriptor')
        birth.side_effect = [[4, 5], [4, 6]]
        with self.assertRaises(ValueError):
            self.owner.poll(0)
        self.assertTrue(self.owner._failed.is_set())
        barrier.assert_not_called()

    def test_writer_inventory_error_with_pause_is_terminal(self):
        files, barrier, _ = self.writer_observation()
        def fail():
            self.revoke()
            raise OSError('incomplete vnode descriptor')
        files.side_effect = lambda *a, **k: fail()
        with self.assertRaises(ValueError):
            self.owner.poll(0)
        self.assertTrue(self.owner._failed.is_set())
        barrier.assert_not_called()

    def test_full_barrier_error_still_fails_without_replay(self):
        files, barrier, _ = self.writer_observation()
        files.return_value = {self.root / 'thread-writer-locks' / 'session.lock': {}}
        barrier.return_value.prepare.side_effect = OSError('identity evidence unavailable')
        with self.assertRaises(OSError):
            self.owner.poll(0)
        with self.assertRaises(ValueError):
            self.owner.poll(0)
        barrier.return_value.prepare.assert_called_once()

    def test_activation_observation_uses_original_barrier_without_launch_or_prepare(self):
        self.assertIsNone(self.owner.observe_for_activation(0))
        barrier = Mock()
        witness = {'index': 0, 'readiness_proven': False}
        barrier.observe_for_activation.return_value = witness
        self.owner._barriers[0] = barrier
        self.assertEqual(self.owner.observe_for_activation(0), witness)
        barrier.prepare.assert_not_called()
        self.client.new_codex_surface.assert_not_called()
        self.inventory.assert_called_once()

    def test_activation_observer_failure_permanently_revokes_original_owner(self):
        barrier = Mock()
        barrier.observe_for_activation.side_effect = ValueError('lost original')
        self.owner._barriers[0] = barrier
        with self.assertRaises(ValueError): self.owner.observe_for_activation(0)
        barrier.observe_for_activation.side_effect = None
        with self.assertRaises(ValueError): self.owner.observe_for_activation(0)
        self.assertTrue(self.owner._failed.is_set())


if __name__ == '__main__':
    unittest.main()
