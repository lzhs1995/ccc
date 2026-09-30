import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import ccc_standby_runner as runner
from ccc_native_standby import write_once


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ccc-runner-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / 'invocation.json'
        self.output = self.root / 'evidence'; self.output.mkdir(mode=0o700)
        self.value = {'version': 1, 'kind': 'standby_production_invocation',
            'invocation_id': str(uuid.uuid4()), 'config_path': str(self.root / 'config.json'),
            'workspace_id': str(uuid.uuid4()), 'mode': 'b', 'argv': ['/not-executed/codex'],
            'provider': 'original', 'upstream_url': 'https://example.invalid/v1',
            'environment': {'HOME': str(self.root), 'API_KEY': 'test-only-never-output'},
            'cmux_binary': '/not-executed/cmux', 'cmux_socket': str(self.root / 'cmux.sock'),
            'lifetime_seconds': 60}
        raw = write_once(self.path, self.value)
        self.sha = hashlib.sha256(raw).hexdigest()
        self.stop = threading.Event()
        self.owner = Mock()
        self.owner.service.preparation = SimpleNamespace(
            selected={'job_id': str(uuid.uuid4()), 'cohort_id': str(uuid.uuid4()),
                'workspace_id': self.value['workspace_id'], 'mode': 'b',
                'boot_id': runner.boot_id(), 'generation': 'a' * 64},
            jobfile=self.root / 'job.json', config_path=Path(self.value['config_path']))
        self.owner.endpoint.spec_path = self.root / 'owner.json'
        self.owner.endpoint.sha256 = 'b' * 64
        self.status = {**self.owner.service.preparation.selected, 'state': 'admitted',
            'action_id': None, 'first_tasks_confirmed': 0, 'job_terminal': False, 'run_terminal': False}
        self.caller = Mock()
        self.caller.admit.return_value = self.owner
        self.factory = Mock(return_value=self.caller)
        self.client_factory = Mock(return_value=Mock())
        def status():
            self.stop.set()
            return copy.deepcopy(self.status)
        self.owner.status.side_effect = status

    def invocation(self):
        return runner.Invocation(self.path, self.sha)

    def make(self, **kwargs):
        return runner.Runner(self.invocation(), self.output, stop=self.stop,
            caller_factory=self.factory, client_factory=self.client_factory, **kwargs)

    def test_default_admits_only_and_emits_original_job_without_secrets(self):
        emitted = []
        self.assertEqual(self.make().run(emit=emitted.append), 0)
        self.owner.prepare.assert_not_called()
        self.caller.close.assert_called_once()
        self.assertEqual(emitted[0]['job_path'], str(self.root / 'job.json'))
        self.assertNotIn('test-only-never-output', json.dumps(emitted))
        self.assertNotIn('action_id', emitted[0])
        self.assertFalse(emitted[0]['run_terminal'])
        self.assertIn(self.path, self.factory.call_args.kwargs['runtime_files'])

    def test_explicit_prepare_starts_once_after_durable_open(self):
        def prepare():
            self.assertTrue((self.output / 'runner-open.json').is_file())
        self.owner.prepare.side_effect = prepare
        obj = self.make()
        obj.run(prepare=True)
        self.owner.prepare.assert_called_once()
        with self.assertRaises(ValueError): obj.run(prepare=True)

    def test_second_runner_cannot_reconsume_original_evidence(self):
        self.make().run()
        self.stop.clear()
        with self.assertRaises(FileExistsError): self.make().run(prepare=True)
        self.factory.assert_called_once()

    def test_cancel_during_controller_discovery_prevents_caller_construction(self):
        self.client_factory.side_effect = lambda _: self.stop.set()
        with self.assertRaises(ValueError): self.make().run(prepare=True)
        self.factory.assert_not_called()
        self.owner.prepare.assert_not_called()

    def test_cancel_during_admission_closes_caller_and_prevents_creation(self):
        def admit(*a, **kw):
            self.stop.set()
            return self.owner
        self.caller.admit.side_effect = admit
        with self.assertRaises(ValueError): self.make().run(prepare=True)
        self.owner.prepare.assert_not_called()
        self.caller.close.assert_called_once()

    def test_signal_reaches_worker_lifetime_callback_without_polling(self):
        def prepare():
            guard = self.factory.call_args.kwargs['lifetime_guard']
            result = []
            def worker():
                result.append(guard())
                self.stop.set()
                result.append(guard())
            thread = threading.Thread(target=worker)
            thread.start(); thread.join(timeout=1)
            self.assertEqual(result, [True, False])
        self.owner.prepare.side_effect = prepare
        self.make().run(prepare=True)

    def test_expiry_reaches_callback_even_without_main_poll(self):
        now = [10.0]
        def prepare():
            guard = self.factory.call_args.kwargs['lifetime_guard']
            self.assertTrue(guard())
            now[0] = 70.0
            self.assertFalse(guard())
        self.owner.prepare.side_effect = prepare
        self.make(clock=lambda: now[0]).run(prepare=True)
        self.caller.close.assert_called_once()

    def test_first_task_terminal_keeps_owner_and_routes_alive(self):
        states = ['first_tasks_observed', 'first_tasks_observed', 'cancelled']
        def status():
            self.caller.close.assert_not_called()
            return {**self.status, 'state': states.pop(0)}
        self.owner.status.side_effect = status
        self.make().run(prepare=True)
        self.assertEqual(self.owner.status.call_count, 3)
        closed = json.loads((self.output / 'runner-closed.json').read_bytes())
        self.assertFalse(closed['job_terminal'])
        self.assertFalse(closed['run_terminal'])
        self.assertFalse(closed['native_processes_terminated'])

    def test_open_persistence_failure_closes_without_creating(self):
        real = runner.write_once
        def write(path, value):
            if path.name == 'runner-open.json': raise OSError('injected disk full')
            return real(path, value)
        with patch.object(runner, 'write_once', side_effect=write):
            with self.assertRaises(OSError): self.make().run(prepare=True)
        self.owner.prepare.assert_not_called()
        self.caller.close.assert_called_once()

    def test_close_failure_is_recorded_and_raised(self):
        self.caller.close.side_effect = OSError('injected close failure')
        with self.assertRaises(OSError): self.make().run()
        closed = json.loads((self.output / 'runner-closed.json').read_bytes())
        self.assertFalse(closed['handles_closed'])
        self.assertEqual(closed['close_error_type'], 'OSError')

    def test_service_failure_exits_nonzero(self):
        self.owner.status.side_effect = None
        self.owner.status.return_value = {**self.status, 'state': 'failed'}
        self.assertEqual(self.make().run(), 1)

    def test_same_bytes_replacement_permanently_invalidates(self):
        obj = self.invocation()
        old = self.root / 'original'
        self.path.rename(old)
        write_once(self.path, self.value)
        with self.assertRaises(ValueError): obj.current()
        os.replace(old, self.path)
        with self.assertRaises(ValueError): obj.current()

    def test_private_permissions_link_and_wrong_hash_rejected(self):
        with self.assertRaises(ValueError): runner.Invocation(self.path, '0' * 64)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError): self.invocation()
        self.path.chmod(0o600)
        link = self.root / 'link'; link.symlink_to(self.path)
        with self.assertRaises(OSError): runner.Invocation(link, self.sha)

    def test_missing_environment_cannot_fall_back_to_parent_shell(self):
        self.path.unlink()
        del self.value['environment']
        raw = write_once(self.path, self.value)
        with self.assertRaises(ValueError): runner.Invocation(self.path, hashlib.sha256(raw).hexdigest())

    def test_wrong_controller_refused_before_any_create(self):
        client = Mock()
        client.capabilities.return_value = {'protocol': 'cmux-socket', 'version': 2,
            'socket_path': '/wrong/socket', 'access_mode': 'automation',
            'methods': ['system.tree', 'system.top', 'surface.create', 'terminal.paste']}
        with patch.object(runner.core, 'CmuxClient', return_value=client):
            with self.assertRaises(ValueError): runner.connect(self.value)
        client.new_codex_surface.assert_not_called()

    def test_cli_failure_restores_signal_handlers_and_omits_exception_secret(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        with patch.object(runner, 'Invocation', side_effect=ValueError('test-only-secret')), \
                patch('builtins.print') as output:
            self.assertEqual(runner.main(['--spec', str(self.path), '--spec-sha256', self.sha,
                                          '--directory', str(self.output)]), 1)
        self.assertNotIn('test-only-secret', str(output.call_args_list))
        self.assertEqual(before, {s: signal.getsignal(s) for s in before})


if __name__ == '__main__':
    unittest.main()
