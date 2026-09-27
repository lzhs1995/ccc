import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import ccc_access_service as service
import cmux_codex_watch as core


class AccessServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / 'ccc/config.json'
        self.native = self.root / 'native'
        self.native.mkdir()
        self.native_config = self.native / 'config.toml'
        self.native_config.write_text('model="gpt-6-astra"\nmodel_provider="custom"\n'
            '[model_providers.custom]\nname="fixture"\nbase_url="https://anyrouter.test/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=true\n')
        self.job = {'id': str(uuid.uuid4()), 'workspace_id': str(uuid.uuid4()).upper()}
        self.owner = {'instance': str(uuid.uuid4()), 'port': 23456}
        self.environment = patch.dict(os.environ, {'CODEX_HOME': str(self.native), 'HTTPS_PROXY': '', 'https_proxy': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_paused_check_keeps_old_job_and_directs_to_new_workspace(self):
        import ccc_workspace_batch as batch
        store = core.ConfigStore(self.config)
        store.mutate(lambda c: c.update(mode='armed', global_paused=False))
        old = batch.start(self.config, self.job['workspace_id'], launch=False)
        store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        path = batch.job_path(self.config, old['job_id'])
        before_config, before_job = self.config.read_bytes(), path.read_bytes()
        with patch.object(service, 'ensure_gateway') as gateway, patch.object(batch, '_launch') as launch:
            with self.assertRaisesRegex(RuntimeError, '新的 workspace') as error:
                batch.start(self.config, self.job['workspace_id'], access_check=True)
            self.assertNotIn(' W ', str(error.exception))
            for private in (False, True):
                with self.assertRaisesRegex(RuntimeError, '按 W 恢复'):
                    batch.start(self.config, self.job['workspace_id'], private_check=private)
            gateway.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(path.read_bytes(), before_job)

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'new mode explicitly requires stdlib tomllib')
    def test_prepare_and_launch_only_override_this_invocation(self):
        before = self.native_config.read_bytes()
        policy = service.prepare(self.config, self.job, owner=self.owner)
        self.job['access_policy'] = policy
        self.assertEqual(policy['mode'], service.MODE)
        with patch.object(service, 'ensure_gateway', return_value=self.owner):
            argv = service.launch_arguments(self.config, self.job, 4)
        overrides = argv[1::2]
        self.assertIn('model_provider="custom"', overrides)
        endpoint = next(a for a in overrides if a.startswith('model_providers.custom.base_url='))
        self.assertIn(f"127.0.0.1:23456/{self.job['id']}/4/", endpoint)
        self.assertFalse(any('auth' in a or 'api_key' in a for a in overrides))
        self.assertEqual(self.native_config.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            service.prepare(self.config, self.job, owner=self.owner)

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'new mode explicitly requires stdlib tomllib')
    def test_changed_native_config_or_gateway_cannot_silently_rebind_a_job(self):
        self.job['access_policy'] = service.prepare(self.config, self.job, owner=self.owner)
        with patch.object(service, 'ensure_gateway', return_value={**self.owner, 'instance': str(uuid.uuid4())}):
            with self.assertRaises(RuntimeError):
                service.launch_arguments(self.config, self.job, 0)
        self.native_config.write_text(self.native_config.read_text() + '\n# changed\n')
        with self.assertRaises(RuntimeError):
            service.launch_arguments(self.config, self.job, 0)

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'new mode explicitly requires stdlib tomllib')
    def test_modified_descriptor_cannot_change_the_credential_destination(self):
        self.job['access_policy'] = service.prepare(self.config, self.job, owner=self.owner)
        path = service.job_root(self.config, self.job['id']) / 'access.json'
        descriptor = service.read_private(path)
        descriptor['upstream']['base_url'] = 'https://changed.example/v1'
        core.atomic_write_json(path, descriptor)
        with self.assertRaises(ValueError):
            service.launch_arguments(self.config, self.job, 0)

    def test_binding_is_exact_and_cannot_be_reassigned(self):
        # Binding validation is independent of TOML/native-provider discovery.
        policy = service.Policy(self.job['workspace_id'], self.job['id'])
        descriptor = {'version': service.VERSION, 'mode': service.MODE,
                      'config_path': str(self.config), 'policy': service.asdict(policy)}
        service.job_root(self.config, self.job['id']).mkdir(parents=True)
        service.create_private(service.job_root(self.config, self.job['id']) / 'access.json', descriptor)
        self.job.update(access_mode=service.MODE, access_policy={
            'mode': service.MODE, 'version': service.VERSION, 'max_attempts': policy.max_attempts,
            'max_output_tokens': policy.max_output_tokens, 'descriptor_sha256': service.descriptor_sha(descriptor)})
        target = {'surface_id': str(uuid.uuid4()), 'workspace_id': self.job['workspace_id']}
        native = {'session_id': str(uuid.uuid4()), 'pid': 1234, 'process_start': 1000}
        slot = {'index': 1, 'surface_id': target['surface_id'], 'session_id': native['session_id']}
        service.bind_slot(self.config, self.job, slot, target, native)
        service.bind_slot(self.config, self.job, slot, target, native)
        changed = {**native, 'session_id': str(uuid.uuid4())}
        with self.assertRaises(ValueError):
            service.bind_slot(self.config, self.job, {**slot, 'session_id': changed['session_id']}, target, changed)

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'new mode requires stdlib tomllib')
    def test_persisted_check_mode_cannot_fall_back_when_policy_is_missing_or_empty(self):
        import copy
        import ccc_workspace_batch as batch
        self.job['access_policy'] = service.prepare(self.config, self.job, owner=self.owner)
        self.job.update(config_path=str(self.config), slots=[{'index': 0, 'launch_id': 'fixture'}])
        before = self.native_config.read_bytes()
        # This configuration test constructs argv without executing a native CLI.
        # Binary identity is exercised by separate native acceptance tests.
        with patch.object(service, 'ensure_gateway', return_value=self.owner), \
                patch('ccc_batch_guard.native_binary', return_value='/fixture/codex'):
            self.assertEqual(batch.startup_mode(self.job, self.config), 'access_check')
            self.assertTrue(any('127.0.0.1:23456' in value for value in batch.native_launch_argv(self.config, self.job, 0)))
        for missing, value in ((True, None), (False, None), (False, {})):
            changed = copy.deepcopy(self.job)
            if missing:
                changed.pop('access_policy')
            else:
                changed['access_policy'] = value
            for marker in (True, False):
                damaged = dict(changed)
                if not marker:
                    damaged.pop('access_mode')
                with self.subTest(missing=missing, value=value, marker=marker):
                    core.atomic_write_json(batch.job_path(self.config, self.job['id']), damaged)
                    with self.assertRaises(ValueError):
                        batch.startup_mode(damaged, self.config)
                    with self.assertRaises(ValueError):
                        batch.native_launch_argv(self.config, damaged, 0)
                    with self.assertRaises(ValueError):
                        batch.register(self.config, self.job['id'], 0, 'fixture')
                    with self.assertRaises(ValueError):
                        batch.BatchWorker(self.config, self.job['id'], client=object())
        self.assertEqual(self.native_config.read_bytes(), before)

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'new mode requires stdlib tomllib')
    def test_missing_descriptor_and_mode_marker_never_authorize_direct_launch(self):
        import ccc_workspace_batch as batch
        self.job['access_policy'] = service.prepare(self.config, self.job, owner=self.owner)
        path = service.job_root(self.config, self.job['id']) / 'access.json'
        path.unlink()
        with self.assertRaises(OSError):
            batch.native_launch_argv(self.config, self.job, 0)
        # Original B and b still work when no trace of N exists for their job.
        legacy = {'id': str(uuid.uuid4()), 'workspace_id': self.job['workspace_id']}
        self.assertEqual(batch.startup_mode(legacy, self.config), 'existing')
        self.assertEqual(batch.startup_mode({**legacy, 'cwd_policy': batch.EMPTY_CWD_POLICY}, self.config), 'private_check')

    def test_continuation_gate_only_applies_to_this_new_mode_and_surface(self):
        target = {'surface_id': 'native-check', 'workspace_id': self.job['workspace_id']}
        config = {'workspace_rules': [{'workspace_id': target['workspace_id'], 'access_check_slots': {
            target['surface_id']: {'job_id': self.job['id'], 'index': 4}}}]}
        path = service.job_root(self.config, self.job['id']) / 'access-status.json'
        snapshot = {'workspace_id': target['workspace_id'], 'job_id': self.job['id'], 'updated_at': time.time(),
                    'blocked_slots': [], 'attempts': 50, 'max_attempts': 1000, 'authorized': True}
        core.atomic_write_json(path, snapshot)
        self.assertTrue(service.continuation_allowed(self.config, config, target))
        for change in ({'first_complete': {'response_id': 'real'}}, {'blocked_slots': [4]}, {'attempts': 1000},
                       {'updated_at': time.time() - 10}, {'authorized': False}, {'closed': True}):
            with self.subTest(change=change):
                core.atomic_write_json(path, {**snapshot, **change})
                self.assertFalse(service.continuation_allowed(self.config, config, target))
                self.assertTrue(service.continuation_allowed(self.config, config, {**target, 'surface_id': 'old-B'}))

    def test_private_record_rejects_symlink_and_truncation(self):
        path = self.root / 'private.json'
        service.create_private(path, {'ok': True})
        link = self.root / 'link.json'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            service.read_private(link)
        path.write_text('{')
        with self.assertRaises(ValueError):
            service.read_private(path)


@unittest.skipUnless(__import__('sys').platform == 'darwin', 'exact process identity uses Darwin proc_pidinfo')
class GatewayLifecycleTests(unittest.TestCase):
    def test_repeated_starts_reuse_one_private_service_without_network_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp).resolve() / 'config.json'
            core.atomic_write_json(config, core.default_config())
            owner = service.ensure_gateway(config)
            try:
                self.assertTrue(service.ping(owner))
                self.assertEqual(service.ensure_gateway(config), owner)
                self.assertTrue(service.owner_alive(owner, config))
                self.assertFalse(list(config.parent.glob('workspace-batches/*/access-journal.jsonl')))
            finally:
                if service.owner_alive(owner, config):
                    os.kill(owner['pid'], signal.SIGTERM)
                    until = time.monotonic() + 5
                    while service.owner_alive(owner, config) and time.monotonic() < until:
                        time.sleep(.05)
                    self.assertFalse(service.owner_alive(owner, config))
                    service._started_processes.pop(owner['pid']).wait(timeout=1)


if __name__ == '__main__':
    unittest.main()
