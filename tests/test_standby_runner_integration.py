"""Runner through the real caller, factory and private Unix owner endpoint."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock
import uuid

import ccc_standby_runner as runner
from ccc_native_standby import write_once
from ccc_standby_service import request


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ccc-ri-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.native = self.root / 'native'; self.native.mkdir()
        (self.native / 'sessions').mkdir()
        (self.native / 'config.toml').write_text('model_provider="original"\n'
            '[model_providers.original]\nbase_url="https://provider.invalid/v1"\n')
        self.system = self.root / 'system'; self.system.mkdir()
        self.binary = self.root / 'codex'; self.binary.write_text('never executed')
        self.wid = str(uuid.uuid4())
        self.config = self.root / 'config.json'
        config = runner.core.default_config()
        config.update(mode='armed', global_paused=False, targets=[], workspace_rules=[{
            'workspace_id': self.wid, 'enabled': True, 'paused': False}])
        runner.core.atomic_write_json(self.config, config)
        self.client = Mock()
        self.client.workspace_tree.return_value = {'windows': [{'id': str(uuid.uuid4()),
            'workspaces': [{'id': self.wid, 'panes': [{'id': str(uuid.uuid4())}]}]}]}
        self.output = self.root / 'evidence'; self.output.mkdir(mode=0o700)
        self.spec = self.root / 'invocation.json'
        raw = write_once(self.spec, {'version': 1, 'kind': 'standby_production_invocation',
            'invocation_id': str(uuid.uuid4()), 'config_path': str(self.config),
            'workspace_id': self.wid, 'mode': 'b', 'argv': [str(self.binary)],
            'provider': 'original', 'upstream_url': 'https://provider.invalid/v1',
            'environment': {'HOME': str(self.root), 'CODEX_HOME': str(self.native)},
            'cmux_binary': '/never-executed/cmux', 'cmux_socket': str(self.root / 'cmux.sock'),
            'lifetime_seconds': 10})
        self.invocation = runner.Invocation(self.spec, hashlib.sha256(raw).hexdigest())
        self.callers = []
        def factory(**kwargs):
            value = runner.ProductionCaller(**kwargs, system_dir=self.system)
            self.callers.append(value)
            self.addCleanup(value.close)
            return value
        self.stop = threading.Event()
        self.instance = runner.Runner(self.invocation, self.output, stop=self.stop,
            caller_factory=factory, client_factory=lambda _: self.client)

    def test_real_owner_socket_status_and_cancel_keep_immutable_job(self):
        original = []
        def emit(row):
            if row.get('kind') == 'standby_runner_open':
                job = Path(row['job_path'])
                original.append((job, job.read_bytes()))
                status = request(row['owner_spec_path'], row['owner_spec_sha256'], 'status')
                self.assertEqual(status['state'], 'admitted')
                self.assertEqual(status['job_id'], row['job_id'])
                request(row['owner_spec_path'], row['owner_spec_sha256'], 'cancel')
        self.assertEqual(self.instance.run(emit=emit), 0)
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0][0].read_bytes(), original[0][1])
        self.client.new_codex_surface.assert_not_called()
        self.assertTrue(self.callers[0].routes.report()['closed'])
        closed = json.loads((self.output / 'runner-closed.json').read_bytes())
        self.assertEqual(closed['reason'], 'service_cancelled')
        self.assertFalse(closed['run_terminal'])

    def test_real_source_callback_observes_runner_stop_from_worker_before_prepare(self):
        results = []
        def emit(row):
            if row.get('kind') != 'standby_runner_open':
                return
            caller = self.callers[0]
            caller.sources.current()
            def worker():
                self.stop.set()
                try:
                    caller.sources.current()
                except ValueError:
                    results.append('rejected')
            thread = threading.Thread(target=worker)
            thread.start(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        with self.assertRaises(ValueError):
            self.instance.run(prepare=True, emit=emit)
        self.assertEqual(results, ['rejected'])
        self.client.new_codex_surface.assert_not_called()
        self.assertTrue(self.callers[0].routes.report()['closed'])

    def test_staged_runner_loads_without_checkout_or_starting_native(self):
        source = Path(runner.__file__).resolve().parent
        runtime = self.root / 'runtime'
        release = runner.core.stage_runtime_release(source_dir=source, runtime_root=runtime)
        runner.core.validate_runtime_release(release)
        result = subprocess.run([sys.executable, '-B', '-E', '-s',
            str(release / 'ccc_standby_runner.py'), '--help'], cwd=self.root,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--prepare', result.stdout)
        self.assertFalse((runtime / 'current').exists())
        self.assertFalse((self.output / 'runner-intent.json').exists())

    def test_staged_preparation_starts_and_reaps_its_inventory_worker(self):
        source = Path(runner.__file__).resolve().parent
        runtime = self.root / 'runtime'
        release = runner.core.stage_runtime_release(source_dir=source, runtime_root=runtime)
        runner.core.validate_runtime_release(release)
        # Help and daemon construction do not exercise preparation's lazy imports.
        # Only staged siblings are available, including to the isolated worker.
        script = '''
from pathlib import Path
import sys
release = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(release))
import ccc_standby_prepare as prepare
import ccc_standby_inventory as inventory
assert Path(prepare.__file__).parent == release
assert Path(inventory.__file__).parent == release
assert prepare.ProcessInventoryReader is inventory.ProcessInventoryReader
reader = prepare.ProcessInventoryReader()
child = reader._child
try:
    assert child.poll() is None
finally:
    reader.close()
assert child.poll() is not None
assert child.stdin.closed and child.stdout.closed
print('staged preparation worker reaped')
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script,
            str(release)], cwd=self.root, env={}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'staged preparation worker reaped')
        self.assertFalse((runtime / 'current').exists())
        self.client.new_codex_surface.assert_not_called()


if __name__ == '__main__':
    unittest.main()
