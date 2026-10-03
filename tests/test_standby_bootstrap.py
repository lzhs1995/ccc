import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import ccc_workspace_batch as batch
import ccc_native_standby as ledger
import ccc_standby_bootstrap as bridge
import ccc_standby_environment as native_environment
from ccc_private_check import POLICY as CHECK


def descriptor(config):
    return dict(id=str(uuid.uuid4()), workspace_id=str(uuid.uuid4()),
        standby_environment_sha256=native_environment.signature({'HOME': str(config.parent)}),
        standby_cohort_id=str(uuid.uuid4()), standby_boot_id=str(uuid.uuid4()),
        standby_generation='a'*64, standby_mode='b', standby_policy=ledger.POLICY,
        initial_prompt=batch.PROMPT, cwd_policy=batch.EMPTY_CWD_POLICY,
        check_retry_policy=CHECK, native_runtime_policy=batch.NATIVE_RUNTIME_POLICY,
        config_path=str(config), slots=[dict(index=i, launch_id=str(uuid.uuid4())) for i in range(50)])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ccc-bridge-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.root.chmod(0o700)
        self.config = self.root / 'config.json'
        self.config.write_text('{}')
        self.job = descriptor(self.config)
        self.value = 'a'*64
        self.allowed = True
        self.server = bridge.GenerationBridge(self.root, config_path=self.config, job=self.job,
            current=lambda: self.value, authorized=lambda _: self.allowed,
            target_environment={'HOME': str(self.root)})
        self.addCleanup(self.server.close)
        self.spec, self.current = bridge.live_reader(self.server.spec_path,
            self.server.sha256, 0, self.config)

    def test_actual_socket_and_cli_bind_same_original(self):
        self.assertEqual(self.current(), self.value)
        command = shlex.split(self.server.command(0))
        self.assertNotIn(batch.PROMPT, command)
        def launch(config, job, index, launch_id, *, generation_current, environment_current):
            self.assertEqual((config, job, index, launch_id),
                (self.config, self.job['id'], 0, self.job['slots'][0]['launch_id']))
            self.assertEqual(generation_current(), self.value)
            self.assertEqual(environment_current(), {'HOME': str(self.root)})
            self.allowed = False
            with self.assertRaises(ValueError):
                generation_current()
        with patch.object(bridge.launch, 'launch_registered', side_effect=launch) as called:
            with patch('sys.argv', command[2:]):
                bridge.main()
            called.assert_called_once()

    def test_fifty_originals_reach_both_exec_guards_concurrently(self):
        barrier = threading.Barrier(50)
        def read(index):
            _, current = bridge.live_reader(self.server.spec_path,
                self.server.sha256, index, self.config)
            for _ in range(2):
                barrier.wait(timeout=5)
                self.assertEqual(current(), self.value)
            return index
        with ThreadPoolExecutor(max_workers=50) as executor:
            self.assertEqual(sorted(executor.map(read, range(50))), list(range(50)))

    def test_launch_failure_is_saved_without_relaunch(self):
        command = shlex.split(self.server.command(0))
        error = ValueError('original exec guard refused')
        with patch.object(bridge.launch, 'launch_registered', side_effect=error) as called:
            with patch('sys.argv', command[2:]), self.assertRaises(ValueError) as caught:
                bridge.main()
        self.assertIs(caught.exception, error)
        called.assert_called_once()
        record = json.loads((self.root / 'bootstrap-failure-0.json').read_text())
        self.assertEqual(record['job_id'], self.job['id'])
        self.assertEqual(record['spec_sha256'], self.server.sha256)
        self.assertEqual(record['launch_id'], self.job['slots'][0]['launch_id'])
        self.assertIn('original exec guard refused', record['traceback'])

    def test_existing_failure_is_not_overwritten_or_retried(self):
        path = self.root / 'bootstrap-failure-0.json'
        path.write_bytes(b'original failure')
        command = shlex.split(self.server.command(0))
        error = ValueError('later failure')
        with patch.object(bridge.launch, 'launch_registered', side_effect=error) as called:
            with patch('sys.argv', command[2:]), self.assertRaises(ValueError) as caught:
                bridge.main()
        self.assertIs(caught.exception, error)
        self.assertEqual(path.read_bytes(), b'original failure')
        called.assert_called_once()
        self.assertTrue(error.bootstrap_diagnostic_error)

    def test_failure_identity_preserves_unknown_and_drift_without_retry(self):
        command = shlex.split(self.server.command(0))
        for samples in (([123, 45], [123, 45]), (None, None), ([123, 45], [124, 0])):
            with self.subTest(samples=samples):
                path = self.root / 'bootstrap-failure-0.json'
                path.unlink(missing_ok=True)
                error = ValueError('generation refused before registration')
                with patch.object(bridge, 'birth', side_effect=samples), \
                     patch.dict(bridge.os.environ, CMUX_SURFACE_ID='original-surface',
                                CMUX_WORKSPACE_ID='original-workspace'), \
                     patch.object(bridge.launch, 'launch_registered', side_effect=error) as called, \
                     patch('sys.argv', command[2:]), self.assertRaises(ValueError) as caught:
                    bridge.main()
                self.assertIs(caught.exception, error)
                called.assert_called_once()
                observed = json.loads(path.read_text())['bootstrap_identity']
                self.assertEqual(observed['birth_before'], samples[0])
                self.assertEqual(observed['birth_after'], samples[1])
                self.assertEqual(observed['surface_id'], 'original-surface')
                self.assertEqual(observed['workspace_id_after'], 'original-workspace')

    def test_source_a_b_a_never_revives_reader_or_owner(self):
        self.value = 'b'*64
        with self.assertRaises(ValueError):
            self.current()
        self.value = 'a'*64
        for read in (self.current, bridge.live_reader(self.server.spec_path,
                self.server.sha256, 1, self.config)[1]):
            with self.assertRaises(ValueError):
                read()

    def test_authorization_callback_cannot_change_source_and_admit(self):
        def authorize(_):
            self.value = 'b'*64
            return True
        self.server.authorized = authorize
        with self.assertRaises(ValueError):
            self.current()

    def test_environment_replaced_during_authorization_permanently_refuses(self):
        path = self.server.environment.path
        original = path.read_bytes()
        def authorize(_):
            path.write_bytes(original.replace(b'"HOME"', b'"FAIL"'))
            return True
        self.server.authorized = authorize
        with self.assertRaises(ValueError):
            self.current()
        path.write_bytes(original)
        self.server.authorized = lambda _: True
        with self.assertRaises(ValueError):
            self.server.command(0)

    def test_current_callback_cannot_replace_endpoint(self):
        def current():
            self.server.spec_path.rename(self.root / 'old.json')
            self.server.spec_path.write_bytes(self.server._raw)
            self.server.spec_path.chmod(0o600)
            return 'a'*64
        self.server.current = current
        with self.assertRaises(ValueError):
            self.current()

    def test_closed_owner_has_no_persisted_generation_fallback(self):
        self.server.close()
        with self.assertRaises(OSError):
            self.current()
        self.assertTrue(self.server.spec_path.exists())
        with self.assertRaises(ValueError):
            bridge.GenerationBridge(self.root, config_path=self.config, job=self.job,
                current=lambda: 'a'*64, authorized=lambda _: True,
                target_environment={'HOME': str(self.root)})

    def test_wrong_slot_or_launch_or_job_never_receives_generation(self):
        base = dict(nonce=self.spec['nonce'], request_id=str(uuid.uuid4()),
            index=0, job_id=self.job['id'], launch_id=self.job['slots'][0]['launch_id'],
            spec_sha256=self.server.sha256)
        for key, value in [('index', 1), ('index', True), ('job_id', str(uuid.uuid4())),
                           ('launch_id', str(uuid.uuid4())), ('nonce', str(uuid.uuid4()))]:
            with self.subTest(key=key), socket.socket(socket.AF_UNIX) as sock:
                sock.connect(str(self.server.socket_path))
                sock.sendall(bridge._encode({**base, key:value}))
                with sock.makefile('rb') as stream:
                    self.assertEqual(bridge._read(stream), {'ok':False})
        self.assertEqual(self.current(), 'a'*64)

    def test_authorized_false_is_permanent(self):
        self.allowed = False
        with self.assertRaises(ValueError):
            self.current()
        self.allowed = True
        with self.assertRaises(ValueError):
            self.server.command(0)

    def test_wrong_spec_hash_and_config_are_refused(self):
        for sha, config in [('b'*64, self.config), (self.server.sha256, self.root)]:
            with self.assertRaises(ValueError):
                bridge.live_reader(self.server.spec_path, sha, 0, config)

    def test_socket_replacement_is_not_deleted_by_owner(self):
        self.server.socket_path.rename(self.root / 'old.sock')
        with socket.socket(socket.AF_UNIX) as foreign:
            foreign.bind(str(self.server.socket_path))
            self.server.socket_path.chmod(0o600)
            with self.assertRaises(ValueError):
                self.current()
            self.server.close()
            self.assertTrue(self.server.socket_path.exists())


if __name__ == '__main__':
    unittest.main()
