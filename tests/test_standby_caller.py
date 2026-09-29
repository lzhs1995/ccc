import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import ccc_standby_caller as caller
import ccc_standby_factory as factory
from ccc_native_standby import COUNT, digest, write_once
from ccc_standby_sources import FileSources


class CallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ccc-caller-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.native = self.root / 'native'; self.native.mkdir()
        self.system = self.root / 'system'; self.system.mkdir()
        (self.native / 'sessions').mkdir()
        self.native_config = self.native / 'config.toml'
        self.native_config.write_text('model_provider="original"\nmodel="keep"\n'
            '[model_providers.original]\nbase_url="https://provider.invalid/v1"\n')
        self.binary = self.root / 'codex'; self.binary.write_text('test-only-executable')
        self.runtime = self.root / 'runtime.py'; self.runtime.write_text('test-only-runtime')
        self.env = {'HOME': str(self.root), 'CODEX_HOME': str(self.native), 'API_KEY': 'test-only'}
        self.wid = str(uuid.uuid4())
        self.config = self.root / 'config.json'
        cfg = factory.core.default_config()
        cfg.update(mode='armed', global_paused=False, targets=[], workspace_rules=[{
            'workspace_id': self.wid, 'enabled': True, 'paused': False}])
        factory.core.atomic_write_json(self.config, cfg)
        self.client = Mock()
        self.client.workspace_tree.return_value = {'windows': [{'id': str(uuid.uuid4()),
            'workspaces': [{'id': self.wid, 'panes': [{'id': str(uuid.uuid4())}]}]}]}
        p = patch('ccc_standby_prepare.RolloutInventory')
        p.start(); self.addCleanup(p.stop)
        mkdtemp = tempfile.mkdtemp
        p = patch.object(factory.tempfile, 'mkdtemp', side_effect=lambda **kw:
            mkdtemp(prefix='endpoint-', dir=str(self.root)))
        p.start(); self.addCleanup(p.stop)

    def make(self, **kwargs):
        value = caller.ProductionCaller(argv=[str(self.binary)], provider='original',
            upstream_url='https://provider.invalid/v1', environment=self.env,
            runtime_files=[self.runtime], system_dir=self.system, **kwargs)
        self.addCleanup(value.close)
        return value

    def admit(self):
        value = self.make()
        owner = value.admit(self.config, self.wid, mode='b', client=self.client)
        return value, owner

    def test_admitted_original_target_reaches_all_50_real_launch_argv(self):
        value, owner = self.admit()
        from ccc_standby_launch import launch_argv
        prep = owner.service.preparation
        self.assertEqual(prep.job['standby_target'], value.target)
        self.assertEqual(len(value.sources.discoveries), COUNT)
        for i, discovery in enumerate(value.sources.discoveries):
            argv = launch_argv(self.config, prep.job, i)
            self.assertEqual(discovery.argv, tuple(argv))
            self.assertEqual(argv[0], str(self.binary))
            self.assertIn(value.routes.urls[i], ' '.join(argv))
            self.assertEqual(discovery.environment, value.environment)
        self.assertEqual(len({d.cwd for d in value.sources.discoveries}), COUNT)
        self.client.new_codex_surface.assert_not_called()
        self.assertEqual(owner.status()['state'], 'admitted')

    def test_slot49_source_change_permanently_invalidates_whole_cohort(self):
        value, _ = self.admit()
        value.sources.current()
        path = value.sources.discoveries[49].cwd / 'AGENTS.md'
        path.write_text('new original slot instructions')
        with self.assertRaises(ValueError): value.sources.current()
        path.unlink()
        with self.assertRaises(ValueError): value.sources.current()

    def test_hot_current_never_discovers_or_rescans(self):
        value, _ = self.admit()
        with patch.object(caller.NativeFileSources, 'discover', side_effect=AssertionError('hot discovery')), \
                patch.object(value.sources.pin, '_snapshot', side_effect=AssertionError('hot snapshot')):
            for _ in range(10): value.sources.current()

    def test_external_lifetime_reaches_generation_and_cannot_revive(self):
        allowed = [True]
        value = self.make(lifetime_guard=lambda: allowed[0])
        owner = value.admit(self.config, self.wid, mode='b', client=self.client)
        value.sources.current()
        allowed[0] = False
        with self.assertRaises(ValueError): value.sources.current()
        allowed[0] = True
        with self.assertRaises(ValueError): value.sources.current()
        self.client.new_codex_surface.assert_not_called()

    def test_lifetime_revoked_inside_route_read_is_checked_after_return(self):
        allowed = [True]
        value = self.make(lifetime_guard=lambda: allowed[0])
        real = value.routes.current
        def current():
            result = real()
            allowed[0] = False
            return result
        with patch.object(value.routes, 'current', side_effect=current):
            with self.assertRaises(ValueError): value._live()
        allowed[0] = True
        with self.assertRaises(ValueError): value._live()
        value.close()
        self.assertTrue(value.routes.report()['closed'])

    def test_native_route_used_before_activation_refuses_zero_proof(self):
        value, _ = self.admit()
        import http.client
        from urllib.parse import urlsplit
        url = urlsplit(value.routes.urls[49])
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=2)
        self.addCleanup(conn.close)
        conn.request('POST', url.path + '/responses', body=b'{}')
        response = conn.getresponse(); response.read()
        self.assertNotEqual(response.status, 200)
        with self.assertRaises(ValueError): value.routes.zero(49)

    def test_source_drift_during_capture_closes_new_routes(self):
        value = self.make()
        real = caller.CohortSources
        def mutate(*args, **kwargs):
            pin = real(*args, **kwargs)
            self.native_config.write_text(self.native_config.read_text().replace('provider.invalid', 'changed.invalid'))
            return pin
        with patch.object(caller, 'CohortSources', side_effect=mutate):
            with self.assertRaises(ValueError):
                value.admit(self.config, self.wid, mode='b', client=self.client)
        self.assertTrue(value.routes.report()['closed'])
        self.client.new_codex_surface.assert_not_called()

    def test_close_during_capture_does_not_publish_or_leak_pin(self):
        value = self.make()
        real = caller.CohortSources
        pins = []
        def stop(*args, **kwargs):
            pin = real(*args, **kwargs); pins.append(pin); value.close(); return pin
        with patch.object(caller, 'CohortSources', side_effect=stop):
            with self.assertRaises(ValueError):
                value.admit(self.config, self.wid, mode='b', client=self.client)
        self.assertTrue(value.routes.report()['closed'])
        with self.assertRaises(ValueError): pins[0].current()

    def commit_fixture(self):
        value = self.make()
        source = Mock(); source.current.return_value = 'a' * 64
        value.sources = source
        value._job = {'id': str(uuid.uuid4()), 'standby_boot_id': str(uuid.uuid4()),
                      'workspace_id': self.wid, 'generation': 'a' * 64}
        jobdir = self.root / 'job'; jobdir.mkdir(mode=0o700)
        directory = jobdir / 'standby'; directory.mkdir(mode=0o700)
        action, cohort = str(uuid.uuid4()), str(uuid.uuid4())
        prep = SimpleNamespace(source_pin=source, job={'id': value._job['id']},
            jobfile=jobdir / 'job.json', selected={'cohort_id': cohort})
        active = {'action_id': action, 'generation': 'a' * 64, 'workspace_id': self.wid,
                  'cohort_id': cohort, 'boot_id': value._job['standby_boot_id']}
        attempt = {'action_id': action, 'activation_sha256': digest(active)}
        inputs = {name: write_once(directory / (name + '.json'), data)
                  for name, data in [('activation', active), ('activation-attempt', attempt)]}
        receipt = directory / 'activation-ui.json'
        raw = write_once(receipt, {'action_id': action})
        timing = SimpleNamespace(jobfile=prep.jobfile, origin={'action_id': action},
            _inputs=inputs, receipt=receipt, _receipt_raw=raw)
        return value, prep, action, timing

    def test_durable_originals_required_before_single_route_release(self):
        value, prep, action, timing = self.commit_fixture()
        self.assertIsNone(value.routes.report()['action_id'])
        value.committed(prep, action, timing=timing, authorized=lambda: True)
        self.assertEqual(value.routes.report()['action_id'], action)
        with self.assertRaises(ValueError):
            value.committed(prep, action, timing=timing, authorized=lambda: True)

    def test_self_consistent_rewrite_cannot_replace_original_commit(self):
        value, prep, action, timing = self.commit_fixture()
        path = prep.jobfile.parent / 'standby' / 'activation.json'
        active = json.loads(path.read_bytes()); active['extra'] = 'rewritten'
        path.write_text(json.dumps(active))
        (path.parent / 'activation-attempt.json').write_text(json.dumps({
            'action_id': action, 'activation_sha256': digest(active)}))
        with self.assertRaises(ValueError):
            value.committed(prep, action, timing=timing, authorized=lambda: True)
        self.assertIsNone(value.routes.report()['action_id'])

    def test_authorization_callback_cannot_replace_same_bytes_inode(self):
        value, prep, action, timing = self.commit_fixture()
        def guard():
            path = timing.receipt
            replacement = path.with_suffix('.new')
            replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600)
            os.replace(replacement, path)
            return True
        with self.assertRaises(ValueError):
            value.committed(prep, action, timing=timing, authorized=guard)
        self.assertIsNone(value.routes.report()['action_id'])

    def test_cancel_during_final_authorization_prevents_release(self):
        value, prep, action, timing = self.commit_fixture()
        def guard(): value.close(); return True
        with self.assertRaises(ValueError):
            value.committed(prep, action, timing=timing, authorized=guard)
        self.assertIsNone(value.routes.report()['action_id'])

    def test_absent_or_nonhex_refresh_receipt_is_not_ready(self):
        value, _ = self.admit()
        row = {'job_id': value._job['id'], 'index': 0, 'boot_id': value._job['standby_boot_id'],
               'workspace_id': self.wid, 'generation': value._job['generation']}
        self.assertIsNone(value.readiness(0, row))
        row.update(refresh_return_observed=True, return_receipt_sha256='g' * 64)
        with self.assertRaises(ValueError): value.readiness(0, row)

    def test_original_complete_refresh_row_produces_scoped_readiness(self):
        value, _ = self.admit()
        row = {'job_id': value._job['id'], 'index': 49, 'boot_id': value._job['standby_boot_id'],
               'workspace_id': self.wid, 'generation': value._job['generation'],
               'launch_id': str(uuid.uuid4()), 'surface_id': str(uuid.uuid4()),
               'session_id': str(uuid.uuid4()), 'pid': 1234, 'birth': [1000, 123456],
               'claim_sha256': 'b' * 64, 'argv_sha256': 'c' * 64,
               'refresh_return_observed': True, 'return_receipt_sha256': 'd' * 64}
        proof = value.readiness(49, row)
        self.assertTrue(proof['readiness_proven'])
        self.assertFalse(proof['refresh_success_verified'])
        self.assertEqual(proof['original']['birth'], row['birth'])
        self.assertEqual(proof['model_request_count'], 0)
        for key, changed in [('index', 0), ('job_id', str(uuid.uuid4())),
                             ('boot_id', str(uuid.uuid4())), ('generation', 'f' * 64)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                value.readiness(49, {**row, key: changed})


if __name__ == '__main__': unittest.main()
