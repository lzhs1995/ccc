import json
from contextlib import closing
import sqlite3
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.standby_five_workspace import Experiment, digest, preflight


class FiveWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'
        self.source.mkdir()
        self.module = self.source/'original.py'
        self.module.write_text('original = True\n')
        (self.source/'cmux_codex_watch.py').write_text('RUNTIME_FILES = ("original.py", "cmux_codex_watch.py")\n')
        (self.source/'tools').mkdir()
        for name in ('ccc_claude_request_key.py', 'cmux_supervisor_tui.py',
                     'tools/native_acceptance_metrics.py', 'tools/native_dispatch_acceptance.py'):
            (self.source/name).write_text('# original\n')
        self.receipt = self.root/'fifty.json'
        hashes = {str(p): digest(p) for p in self.source.rglob('*.py')}
        self.result = dict(passed=True, originals_requested=50, activations=50, recoveries=50,
                           startup_timing={'startup_passed': True}, run_verification={'succeeded': True},
                           source_before=hashes, source_after=hashes)
        self.receipt.write_text(json.dumps(self.result))
        # These tests cover source/capacity admission; original replay has its
        # own IO/terminal contract tests and must not be bypassed in production.
        replay = patch('tools.standby_fifty_prerequisite.verify',
                       return_value={'original_ui50_replayed': True})
        self.original_replay = replay.start()
        self.addCleanup(replay.stop)

    def test_green_summary_without_original_proof_refused(self):
        self.original_replay.side_effect = ValueError('missing original terminal')
        result = preflight(self.source, self.receipt, free_bytes=50*1024**3)
        self.assertFalse(result['eligible'])
        self.assertIn('original evidence replay failed', result['reasons'][0])

    def test_source_change_during_original_replay_refused(self):
        def replay(path):
            self.module.write_text('changed = True\n')
            return {'original_ui50_replayed': True}
        self.original_replay.side_effect = replay
        result = preflight(self.source, self.receipt, free_bytes=50*1024**3)
        self.assertFalse(result['eligible'])
        self.assertIn('source changed during', result['reasons'][0])

    def test_preflight_never_launches_and_requires_successful_fifty(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no process permitted')):
            self.assertTrue(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])
            self.result['passed'] = False
            self.receipt.write_text(json.dumps(self.result))
            self.assertFalse(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])

    def test_changed_source_and_capacity_each_block(self):
        self.assertFalse(preflight(self.source, self.receipt, free_bytes=10*1024**3)['eligible'])
        self.module.write_text('changed = True\n')
        self.assertFalse(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])

    def test_partial_manifest_new_tool_and_external_drift_each_reject(self):
        original = self.receipt.read_bytes()
        self.result['source_before'].pop(str(self.module))
        self.receipt.write_text(json.dumps(self.result))
        self.assertFalse(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])
        self.receipt.write_bytes(original)
        extra = self.source/'tools/standby_new.py'
        extra.write_text('# untested\n')
        self.assertFalse(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])
        extra.unlink()
        config = self.root/'original-config.toml'
        config.write_text('model = "original"\n')
        receipt = json.loads(original)
        for field in ('source_before', 'source_after'):
            receipt[field][str(config)] = digest(config)
        self.receipt.write_text(json.dumps(receipt))
        self.assertTrue(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])
        config.write_text('model = "changed"\n')
        self.assertFalse(preflight(self.source, self.receipt, free_bytes=50*1024**3)['eligible'])

    def experiment(self):
        return Experiment(self.source, SimpleNamespace(), self.root/'output', seconds=60)

    def resource(self):
        return dict(daemon=None, daemon_log=None, proxy=Mock(), proxy_started=True, provider=Mock())

    def test_failed_owner_close_preserves_native_workspace_and_providers(self):
        experiment = self.experiment()
        experiment.pool = Mock()
        experiment.pool.close.side_effect = ValueError('unknown UI outcome')
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        resource = self.resource()
        experiment.resources = {'batch': resource}
        experiment.cleanup()
        experiment.real.tree.assert_not_called()
        resource['proxy'].shutdown.assert_not_called()
        resource['provider'].close.assert_not_called()
        self.assertEqual(experiment.report['cleanup_errors'][-1]['step'], 'preserve_live_resources')

    def test_unknown_daemon_stop_does_not_close_native_workspace(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        resource = self.resource()
        child = resource['daemon'] = Mock()
        child.poll.return_value = None
        child.wait.side_effect = TimeoutError('still live')
        experiment.resources = {'batch': resource}
        experiment.cleanup()
        experiment.cleanup()
        child.terminate.assert_called_once()
        experiment.real.tree.assert_not_called()
        resource['provider'].close.assert_not_called()

    def test_cleanup_only_exact_owned_workspace_and_retained_resources(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        before = {'windows': [{'workspaces': [
            dict(id='original', title='owned'), dict(id='other', title='unrelated')]}]}
        after = {'windows': [{'workspaces': [dict(id='other', title='unrelated')]}]}
        experiment.real.tree.side_effect = [before, before, after]
        resource = self.resource()
        experiment.resources = {'batch': resource}
        experiment.cleanup()
        experiment.real._run.assert_called_once_with(['close-workspace', '--workspace', 'original'], timeout=10)
        resource['proxy'].shutdown.assert_called_once()
        resource['provider'].close.assert_called_once()
        self.assertTrue(experiment.workspaces[0]['closure_observed'])

    def test_close_ack_without_absence_preserves_routes_and_never_resends(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        experiment.real.tree.return_value = {'windows': [{'workspaces': [dict(id='original', title='owned')]}]}
        resource = self.resource()
        experiment.resources = {'batch': resource}
        with patch('tools.standby_five_workspace.time.monotonic', side_effect=[0, 11, 12, 23]):
            experiment.cleanup()
            experiment.cleanup()
        experiment.real._run.assert_called_once()
        resource['provider'].close.assert_not_called()
        self.assertFalse(experiment.report.get('scoped_cleanup_complete', False))

    def test_unknown_close_and_malformed_tree_do_not_release_routes(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        before = {'windows': [{'workspaces': [dict(id='original', title='owned')]}]}
        experiment.real.tree.side_effect = [before, before, {}]
        experiment.real._run.side_effect = TimeoutError('unknown ACK')
        resource = self.resource()
        experiment.resources = {'batch': resource}
        experiment.cleanup()
        experiment.cleanup()
        experiment.real._run.assert_called_once()
        resource['provider'].close.assert_not_called()

    def test_intent_write_failure_consumes_close_without_sending(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        experiment.real.tree.return_value = {'windows': [{'workspaces': [dict(id='original', title='owned')]}]}
        experiment.write = Mock(side_effect=OSError('disk full'))
        with patch('tools.standby_five_workspace.time.monotonic', side_effect=[0, 11]):
            experiment.cleanup()
            experiment.cleanup()
        experiment.real._run.assert_not_called()

    def test_late_absence_or_replaced_workspace_preserves_routes(self):
        for late in (False, True):
            with self.subTest(late=late):
                experiment = Experiment(self.source, SimpleNamespace(), self.root/str(late), seconds=60)
                experiment.real = Mock()
                experiment.workspaces = [dict(id='original', title='owned')]
                before = {'windows': [{'workspaces': [dict(id='original', title='owned')]}]}
                after = {'windows': []} if late else {'windows': [{'workspaces': [dict(id='other', title='owned')]}]}
                experiment.real.tree.side_effect = [before, after]
                resource = self.resource()
                experiment.resources = {'batch': resource}
                with patch('tools.standby_five_workspace.time.monotonic', side_effect=[0, 1, 11 if late else 2]):
                    experiment.cleanup()
                resource['provider'].close.assert_not_called()
                self.assertTrue(experiment.report['cleanup_errors'])

    def test_already_absent_workspace_needs_no_close_command(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.real.tree.return_value = {'windows': []}
        experiment.workspaces = [dict(id='original', title='owned')]
        resource = self.resource()
        experiment.resources = {'batch': resource}
        experiment.cleanup()
        experiment.real._run.assert_not_called()
        resource['provider'].close.assert_called_once()
        self.assertTrue(experiment.report['scoped_cleanup_complete'])

    def test_changed_workspace_title_rejected(self):
        experiment = self.experiment()
        experiment.real = Mock()
        experiment.workspaces = [dict(id='original', title='owned')]
        experiment.real.tree.return_value = {'windows': [{'workspaces': [dict(id='original', title='changed')]}]}
        experiment.cleanup()
        experiment.real._run.assert_not_called()
        self.assertTrue(experiment.report['cleanup_errors'])

    def test_setup_failure_still_records_failed_result_and_cleans_partial_resources(self):
        experiment = self.experiment()
        resource = self.resource()
        experiment.resources = {'partial': resource}
        with patch.object(experiment, 'setup', side_effect=RuntimeError('partial setup')):
            result = experiment.run()
        resource['provider'].close.assert_called_once()
        self.assertFalse(result['passed'])
        self.assertFalse(result['full_500_acceptance'])
        self.assertIn('partial setup', json.loads((experiment.output/'result.json').read_text())['error'])

    def test_actual_setup_wires_five_workspaces_ten_isolated_resources(self):
        import uuid
        fixture_path = self.root/'fixture.py'
        fixture_path.write_text('# fixture\n')
        fixture_path.with_name('native_dispatch_trace99.py').write_text('# wrapper\n')
        proxy_list, providers = [], []
        def proxy_factory(*args):
            proxy = Mock()
            proxy_list.append(proxy)
            return proxy
        def provider_factory(root, prompt):
            root.mkdir(parents=True)
            value = SimpleNamespace(url='https://127.0.0.1:'+str(18000+len(providers)),
                                    cert=root/'cert.pem', prompt=prompt, close=Mock())
            providers.append(value)
            return value
        workspace_ids = [str(uuid.uuid4()).upper() for _ in range(5)]
        sessions_binding = dict(path=str(self.root/'.codex/sessions'), device=1, inode=2)
        hooks_binding = dict(path=str(self.root/'.cmuxterm'), device=1, inode=3)
        fixture = SimpleNamespace(__file__=str(fixture_path), Server=proxy_factory, Forward=object,
            session_root_binding=Mock(side_effect=[sessions_binding, hooks_binding]),
            await_created_workspace=Mock(side_effect=workspace_ids),
            write=lambda p, v: p.write_text(json.dumps(v)))
        experiment = Experiment(self.source, fixture, self.root/'wired', seconds=60)
        real = Mock(binary='/fake/cmux')
        real.capabilities.return_value = {'socket_path': '/fake/socket'}
        real._run.return_value = SimpleNamespace(stdout='workspace-created')
        native_root = self.root/'temporary-native'
        native_root.mkdir()
        with patch('ccc_guard_scope.scan', return_value=[]), \
             patch('cmux_codex_watch.CmuxClient', return_value=real), \
             patch('tools.standby_cleanup_evidence.capture_baseline'), \
             patch('tempfile.mkdtemp', return_value=str(native_root)), \
             patch('ccc_standby_environment.template', return_value={'HOME': str(self.root)}), \
             patch('ccc_batch_guard.native_binary', return_value='/fake/codex'), \
             patch('ccc_standby_target.select_provider', return_value=('original', 'https://unused')), \
             patch('tools.standby_test_provider.LocalProvider', side_effect=provider_factory), \
             patch('ccc_workspace_batch.authorize_workspace') as authorize, \
             patch('ccc_guard_migration.cmux_client', return_value=Mock()), \
             patch('ccc_standby_runner.Invocation', side_effect=lambda p, h: SimpleNamespace(path=p, sha256=h)), \
             patch('tools.standby_run_manifest.declare') as declare, \
             patch('tools.standby_multi_owner.RetainedOwners') as pool:
            experiment.setup()
        self.assertEqual(real._run.call_count, 5)
        self.assertEqual([c.args[0] for c in fixture.session_root_binding.call_args_list],
                         [self.root/'.codex/sessions', self.root/'.cmuxterm'])
        for resource in experiment.resources.values():
            settings = json.loads((resource['root']/'settings.json').read_text())
            self.assertEqual(settings['original_sessions'], sessions_binding)
            self.assertEqual(settings['original_hooks'], hooks_binding)
        self.assertEqual(authorize.call_count, 10)
        self.assertEqual(len(providers), 10)
        self.assertEqual(len(proxy_list), 10)
        plan = declare.call_args.args[1]
        self.assertEqual([sum(r['workspace_id'] == w for r in plan) for w in workspace_ids], [2]*5)
        invocations = pool.call_args.args[1]
        values = [json.loads(invocation.path.read_text()) for _, invocation, _ in invocations]
        for field in ('config_path', 'upstream_url', 'cmux_socket', 'invocation_id'):
            self.assertEqual(len({v[field] for v in values}), 10)
        self.assertTrue(all(v['mode'] == 'b' and v['provider'] == 'original' for v in values))
        self.assertTrue(all(v['environment']['HOME'] == str(self.root) for v in values))
        self.assertTrue(all(r['daemon'] is None for r in experiment.resources.values()))
        for resource in experiment.resources.values():
            resource['proxy_thread'].join(timeout=1)

    def metric_experiment(self):
        experiment = self.experiment()
        keys = [str(i) for i in range(10)]
        experiment.pool = SimpleNamespace(order=keys, handles={})
        experiment.health = Mock()
        experiment.native_setup = SimpleNamespace(resolvers={}, witnesses={})
        for key in keys:
            directory = experiment.output/key
            directory.mkdir()
            timing = directory/'standby'
            timing.mkdir()
            (timing/'activation-ui.json').write_text('{}')
            (timing/'activation-terminal.json').write_text('{}')
            (directory/'rpc-responses.ndjson').write_text('{}\n')
            database_dir = directory/'codex-delivery'
            database_dir.mkdir()
            with closing(sqlite3.connect(database_dir/'delivery.sqlite3')) as db:
                db.execute('CREATE TABLE delivery(surface_id TEXT, record TEXT)')
                db.executemany('INSERT INTO delivery VALUES(?,?)', [(str(i), '{}') for i in range(50)])
                db.commit()
            experiment.resources[key] = dict(directory=directory, config=directory/'config.json')
            routes = Mock()
            routes.report.return_value = dict(failed=False, pending_connections=0,
                unattributed_requests=0, slots=[{'before_activation': 0} for _ in range(50)])
            experiment.pool.handles[key] = {'runner': SimpleNamespace(caller=SimpleNamespace(routes=routes),
                owner=SimpleNamespace(service=SimpleNamespace(preparation=SimpleNamespace(jobfile=directory/'job.json'))))}
            resolvers = {}
            for i in range(50):
                path = directory/f'rollout-{i}.jsonl'
                path.write_text('{}\n')
                resolvers[i] = SimpleNamespace(path=path, row={'surface_id': str(i)})
            experiment.native_setup.resolvers[key] = resolvers
            experiment.native_setup.witnesses[key] = {i: dict(r.row) for i, r in resolvers.items()}
        experiment.completions = dict.fromkeys(keys, Path('/synthetic/completion.json'))
        experiment.fixture.require_original_transcript = lambda resolver, *a: resolver.path
        return experiment

    def test_collects_ten_original_sqlite_snapshots_and_500_chains(self):
        experiment = self.metric_experiment()
        with patch('ccc_standby_timing.evaluate', return_value={'startup_passed': True}), \
             patch('tools.standby_recovery_chain.evaluate', return_value={
                 'causal_chain_verified': True, 'performance_passed': True}) as evaluate, \
             patch('tools.standby_performance_replay.verify', return_value={}) as replay:
            experiment.collect_performance()
        self.assertEqual(evaluate.call_count, 500)
        self.assertEqual(replay.call_count, 10)
        for call, key in zip(replay.call_args_list, experiment.pool.order):
            self.assertIs(call.kwargs['witnesses'], experiment.native_setup.witnesses[key])
            self.assertTrue(callable(call.kwargs['check']))
        self.assertTrue(experiment.report['performance_passed'])
        self.assertFalse(experiment.report['full_500_acceptance'])
        self.assertEqual(len(list(experiment.output.glob('*/original-rollout-*.jsonl'))), 500)

    def test_replay_failure_does_not_accept_metrics(self):
        experiment = self.metric_experiment()
        with patch('ccc_standby_timing.evaluate', return_value={'startup_passed': True}), \
             patch('tools.standby_recovery_chain.evaluate', return_value={
                 'causal_chain_verified': True, 'performance_passed': True}), \
             patch('tools.standby_performance_replay.verify', side_effect=ValueError('replay failed')):
            with self.assertRaisesRegex(ValueError, 'replay failed'):
                experiment.collect_performance()
        self.assertEqual(experiment.metrics, {})
        self.assertTrue((experiment.output/'0/performance.json').exists())
        self.assertFalse((experiment.output/'0/performance-replay.json').exists())

    def test_changed_rpc_evidence_prevents_performance_acceptance(self):
        experiment = self.metric_experiment()
        rpc = experiment.resources['0']['directory']/'rpc-responses.ndjson'
        def changed(*args):
            rpc.write_text('{"changed":true}\n')
            return {'causal_chain_verified': True, 'performance_passed': True}
        with patch('ccc_standby_timing.evaluate', return_value={'startup_passed': True}), \
             patch('tools.standby_recovery_chain.evaluate', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                experiment.collect_performance()
        self.assertEqual(experiment.metrics, {})

    def test_performance_deadline_stops_after_first_late_chain(self):
        experiment = self.metric_experiment()
        clock = [100.0]
        def late(*args):
            clock[0] = 161.0
            return {'causal_chain_verified': True, 'performance_passed': True}
        with patch('tools.standby_five_workspace.time.monotonic', side_effect=lambda: clock[0]), \
             patch('ccc_standby_timing.evaluate', return_value={'startup_passed': True}), \
             patch('tools.standby_recovery_chain.evaluate', side_effect=late) as evaluate:
            with self.assertRaisesRegex(TimeoutError, 'performance observation deadline'):
                experiment.collect_performance(deadline=160)
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(experiment.metrics, {})
        self.assertFalse(list(experiment.output.glob('*/original-rollout-*.jsonl')))
        self.assertFalse(list(experiment.output.glob('*/performance.json')))

    def test_expired_performance_deadline_reads_no_transcripts(self):
        experiment = self.metric_experiment()
        experiment.fixture.require_original_transcript = Mock()
        with patch('tools.standby_five_workspace.time.monotonic', return_value=160):
            with self.assertRaises(TimeoutError):
                experiment.collect_performance(deadline=160)
        experiment.fixture.require_original_transcript.assert_not_called()
        self.assertEqual(experiment.metrics, {})

    def test_failed_route_or_one_slow_chain_cannot_pass_aggregate(self):
        experiment = self.metric_experiment()
        experiment.pool.handles['0']['runner'].caller.routes.report.return_value['slots'][0]['before_activation'] = 1
        with patch('ccc_standby_timing.evaluate', return_value={'startup_passed': True}), \
             patch('tools.standby_recovery_chain.evaluate', return_value={
                 'causal_chain_verified': True, 'performance_passed': False}), \
             patch('tools.standby_performance_replay.verify', return_value={}):
            experiment.collect_performance()
        self.assertFalse(experiment.report['performance_passed'])
        self.assertFalse(experiment.metrics['0']['zero_requests_before_activation'])

    def test_missing_terminal_preserves_other_outcomes_and_refuses_whole_run(self):
        experiment = self.experiment()
        experiment.pool = SimpleNamespace(order=['a','b'])
        experiment.plan = self.root/'run'
        experiment.resources = {}
        experiment.report['scoped_cleanup_complete'] = True
        for key in experiment.pool.order:
            directory = experiment.output/key
            directory.mkdir()
            experiment.resources[key] = {'directory': directory}
        def settle(plan, key, directory, *args, **kwargs):
            if key == 'b':
                raise ValueError('missing original closure')
            (directory/'job-terminal.json').write_text('{}')
        with patch('tools.standby_run_observer.settle', side_effect=settle), \
             patch('tools.standby_run_terminal.capture') as capture:
            with self.assertRaisesRegex(ValueError, 'complete declared'):
                experiment.collect_terminals()
        capture.assert_not_called()
        record = json.loads((experiment.output/'terminal-collection.json').read_text())
        self.assertEqual(set(record['terminals']), {'a'})
        self.assertEqual(record['failures'][0]['batch_id'], 'b')

    def test_terminal_collection_requires_closure_then_keeps_full_declared_set(self):
        experiment = self.experiment()
        experiment.pool = SimpleNamespace(order=[str(i) for i in range(10)])
        experiment.plan = self.root/'run'
        with self.assertRaisesRegex(ValueError, 'closure required'):
            experiment.collect_terminals()
        experiment.report['scoped_cleanup_complete'] = True
        for key in experiment.pool.order:
            directory = experiment.output/key
            directory.mkdir()
            experiment.resources[key] = dict(directory=directory)
        def settle(plan, key, directory, *args, **kwargs):
            (directory/'job-terminal.json').write_text('{}')
        with patch('tools.standby_run_observer.settle', side_effect=settle) as collect, \
             patch('tools.standby_run_terminal.capture') as capture, \
             patch('tools.standby_run_terminal.verify', return_value={'succeeded': False}):
            experiment.collect_terminals()
        self.assertEqual(collect.call_count, 10)
        self.assertEqual(set(capture.call_args.args[1]), set(experiment.pool.order))
        self.assertFalse(experiment.report['run_verification']['succeeded'])
        self.assertFalse(experiment.report['full_500_acceptance'])


class LifecycleIntegrationTests(unittest.TestCase):
    """Exercise enclosing execute(), using synthetic native observations only."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.clock = [100.0]
        self.events = []
        self.experiment = Experiment(root, SimpleNamespace(CohortGate=object), root/'output', seconds=60)
        self.experiment.plan = root/'plan'
        self.experiment.pool = Mock(order=[str(i) for i in range(10)])
        self.experiment.health = Mock()
        transcript = root/'original.jsonl'
        transcript.write_text('{"synthetic": true}\n')
        self.resolvers = {}
        for key in self.experiment.pool.order:
            directory = self.experiment.output/key
            directory.mkdir()
            self.experiment.resources[key] = {'directory': directory}
            self.resolvers[key] = {i: Mock(return_value=transcript, row={'session_id': key+'-'+str(i)})
                                   for i in range(50)}
        self.setup = self._patch('tools.standby_retained_activation.NativeSetup',
                                 return_value=SimpleNamespace(resolvers=self.resolvers, before_prepare=Mock()))
        driver = self._patch('tools.standby_retained_activation.ActivationRun')
        driver.return_value.step.return_value = {'all_settled': True}
        self._patch('tools.standby_five_workspace.time.monotonic', side_effect=lambda: self.clock[0])
        self._patch('tools.native_acceptance_metrics.evaluate_native_completion', return_value={'passed': True})
        self.observe = self._patch('tools.standby_run_observer.observe_completion',
                                  side_effect=lambda plan, key, *a, **kw: self.events.append(key))
        self.experiment.collect_performance = Mock(side_effect=self.performance)
        self.experiment.collect_identity_join = Mock(side_effect=lambda **kw: self.events.append('identity_join'))
        self.experiment.collect_resources = Mock(side_effect=lambda **kw: self.events.append('resources'))
        self.experiment.collect_bindings = Mock(side_effect=lambda phase, **kw: self.events.append(phase))
        self.experiment.pool.observe_overlap.side_effect = lambda *a, **kw: self.events.append('overlap')

    def _patch(self, target, **kwargs):
        item = patch(target, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def performance(self, *, deadline):
        self.assertEqual(deadline, 160)
        self.assertEqual(set(self.experiment.completions), set(self.experiment.pool.order))
        self.events.append('performance')

    def test_ten_completions_precede_performance(self):
        self.experiment.execute()
        self.assertEqual(self.events, ['settled', 'overlap', 'resources', *self.experiment.pool.order,
                                       'completed', 'performance', 'identity_join'])
        self.assertTrue(self.experiment.report['lifecycle_completed'])
        self.assertFalse(self.experiment.report['full_500_acceptance'])
        self.assertLessEqual(self.experiment.pool.observe_overlap.call_args.kwargs['seconds'], 60)

    def test_late_overlap_does_not_start_completion(self):
        self.experiment.pool.observe_overlap.side_effect = lambda *a, **kw: self.clock.__setitem__(0, 161)
        with self.assertRaises(TimeoutError):
            self.experiment.execute()
        self.observe.assert_not_called()
        self.experiment.collect_resources.assert_not_called()
        self.experiment.collect_performance.assert_not_called()

    def test_late_last_completion_is_not_accepted(self):
        def observe(plan, key, *a, **kw):
            if key == '9':
                self.clock[0] = 161
        self.observe.side_effect = observe
        with self.assertRaises(TimeoutError):
            self.experiment.execute()
        self.assertEqual(len(self.experiment.completions), 9)
        self.experiment.collect_performance.assert_not_called()

    def test_late_resources_prevents_completion(self):
        self.experiment.collect_resources.side_effect = lambda **kw: self.clock.__setitem__(0, 161)
        with self.assertRaises(TimeoutError):
            self.experiment.execute()
        self.observe.assert_not_called()

    def prepare_resource_collection(self):
        originals = [dict(pid=i+10, birth=[100, i]) for i in range(500)]
        self.experiment.report['process_overlap'] = dict(process_overlap_proven=True, originals=originals)
        self.experiment.root = self.experiment.output
        self.experiment.resource_owner = dict(pid=1, birth=[100, 1])
        auxiliaries = [self.experiment.resource_owner]
        self.experiment.pool.handles = {}
        for i, resource in enumerate(self.experiment.resources.values()):
            resource['daemon'] = Mock(pid=1000+i)
            resource['daemon'].poll.return_value = None
            resource['daemon_identity'] = dict(pid=1000+i, birth=[100, i])
            auxiliaries.append(resource['daemon_identity'])
        for i, key in enumerate(self.experiment.pool.order):
            child = Mock(pid=2000+i)
            ui = Mock(child=child)
            identity = dict(pid=child.pid, birth=[200, i])
            ui.resource_identity.return_value = identity
            self.experiment.pool.handles[key] = dict(ui_activation=SimpleNamespace(
                ui=ui, original_child=child))
            auxiliaries.append(identity)
        return originals, auxiliaries

    def test_resource_collection_binds_exact_originals_and_persists(self):
        originals, auxiliaries = self.prepare_resource_collection()
        result = dict(process_count=500, full_500_acceptance=False)
        with patch('tools.standby_resource_sample.capture', return_value=result) as capture:
            Experiment.collect_resources(self.experiment, seconds=12)
        capture.assert_called_once_with(originals, self.experiment.root, expected_count=500,
                                        seconds=12, auxiliaries=auxiliaries)
        self.assertEqual(json.loads((self.experiment.output/'original-process-resources.json').read_text()), result)
        self.assertEqual(result['retained_ui_count'], 10)

    def test_missing_or_exited_ui_refuses_before_sample(self):
        for condition in ('missing', 'exited'):
            with self.subTest(condition=condition):
                self.prepare_resource_collection()
                handle = self.experiment.pool.handles['9']
                if condition == 'missing':
                    del handle['ui_activation']
                else:
                    handle['ui_activation'].ui.resource_identity.side_effect = ValueError('exited')
                with patch('tools.standby_resource_sample.capture') as capture:
                    with self.assertRaises(ValueError):
                        Experiment.collect_resources(self.experiment, seconds=12)
                    capture.assert_not_called()
                self.assertFalse((self.experiment.output/'original-process-resources.json').exists())

    def test_ui_replacement_or_exit_during_sample_not_persisted(self):
        for condition in ('activation', 'ui', 'child', 'birth', 'exit', 'handle'):
            with self.subTest(condition=condition):
                self.prepare_resource_collection()
                handle = self.experiment.pool.handles['9']
                activation = handle['ui_activation']
                def capture(*args, **kwargs):
                    if condition == 'activation':
                        handle['ui_activation'] = SimpleNamespace(ui=activation.ui)
                    elif condition == 'ui':
                        activation.ui = Mock(child=activation.original_child)
                    elif condition == 'child':
                        activation.ui.child = Mock(pid=activation.original_child.pid)
                    elif condition == 'birth':
                        activation.ui.resource_identity.return_value = dict(pid=2009, birth=[999, 0])
                    elif condition == 'exit':
                        activation.ui.resource_identity.side_effect = ValueError('exited')
                    else:
                        self.experiment.pool.handles['9'] = dict(handle)
                    return {}
                with patch('tools.standby_resource_sample.capture', side_effect=capture):
                    with self.assertRaises(ValueError):
                        Experiment.collect_resources(self.experiment, seconds=12)
                self.assertFalse((self.experiment.output/'original-process-resources.json').exists())

    def test_dead_daemon_prevents_resource_acceptance(self):
        self.experiment.report['process_overlap'] = dict(process_overlap_proven=True, originals=[])
        self.experiment.resource_owner = dict(pid=100, birth=[100, 1])
        for i, resource in enumerate(self.experiment.resources.values()):
            resource['daemon'] = Mock(pid=1000+i)
            resource['daemon'].poll.return_value = 1 if i == 9 else None
            resource['daemon_identity'] = dict(pid=1000+i, birth=[100, i])
        with patch('tools.standby_resource_sample.capture') as capture:
            with self.assertRaisesRegex(ValueError, 'daemon identity'):
                Experiment.collect_resources(self.experiment, seconds=12)
            capture.assert_not_called()
        self.assertFalse((self.experiment.output/'original-process-resources.json').exists())


class ObserverBindingTests(unittest.TestCase):
    def setUp(self):
        from ccc_standby_routes import RouteObserver
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.experiment = Experiment(root, SimpleNamespace(), root/'output')
        self.routes = RouteObserver(['http://127.0.0.1:1/v1'] * 50, allow_local=True)
        self.addCleanup(self.routes.close)
        self.sources = Mock()
        self.sources.current.return_value = {'original': 'generation'}
        self.sources.resource_report.return_value = dict(closed=False, watcher=dict(
            close_started=False, queue_closed=False, close_errors=[], remaining_owned_fds=7))
        self.caller = SimpleNamespace(sources=self.sources, routes=self.routes)
        self.handle = dict(runner=SimpleNamespace(caller=self.caller), settlement=None)
        self.experiment.pool = SimpleNamespace(handles={'batch': self.handle}, order=['batch'])
        self.experiment.resources = {'batch': {}}
        self.experiment.health = Mock()

    def bind(self):
        return self.experiment.observe_binding('batch', before_activation=True)

    def release(self):
        import uuid
        action = str(uuid.uuid4())
        self.routes.release(action)
        self.handle['settlement'] = ('config', 'job', {'action_id': action}, 'digest')

    def test_real_routes_baseline_and_settlement_without_model_requests(self):
        value = self.bind()
        self.release()
        after = self.experiment.observe_binding('batch')
        self.assertEqual(value['generation_sha256'], after['generation_sha256'])
        self.assertEqual(after['routes']['accepted_connections'], 0)
        self.assertTrue((self.experiment.output/'observer-baseline-batch.json').is_file())

    @unittest.skipUnless(sys.platform == 'darwin', 'requires original Darwin vnode watcher')
    def test_real_source_watcher_and_routes_then_source_mutation(self):
        from ccc_standby_generation import StandbyGeneration, SCOPES
        source = self.experiment.output.parent/'config.txt'
        source.write_text('original')
        generation = StandbyGeneration({scope: [str(source)] for scope in SCOPES},
                                       lambda: {'profile': 'original'}, use_events=True)
        self.addCleanup(generation.close)
        self.caller.sources = generation
        self.bind()
        self.release()
        self.experiment.observe_binding('batch')
        source.write_text('changed')
        with self.assertRaises(ValueError):
            self.experiment.observe_binding('batch')
        self.assertEqual(self.routes.report()['accepted_connections'], 0)

    def test_changed_generation_rejects(self):
        self.bind()
        self.release()
        self.sources.current.return_value = {'original': 'changed'}
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.experiment.observe_binding('batch')

    def test_replaced_equal_sources_rejects(self):
        self.bind()
        self.release()
        replacement = Mock()
        replacement.current.return_value = self.sources.current.return_value
        replacement.resource_report.return_value = self.sources.resource_report.return_value
        self.caller.sources = replacement
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.experiment.observe_binding('batch')

    def test_closed_source_watcher_rejects(self):
        self.sources.resource_report.return_value['watcher']['queue_closed'] = True
        with self.assertRaisesRegex(ValueError, 'watcher no longer live'):
            self.bind()

    def test_replaced_observer_during_report_rejects_before_baseline(self):
        original = self.sources.resource_report.return_value
        def report():
            self.caller.sources = Mock()
            return original
        self.sources.resource_report.side_effect = report
        with self.assertRaisesRegex(ValueError, 'changed during collection'):
            self.bind()
        self.assertNotIn('observer_binding', self.experiment.resources['batch'])

    def test_foreign_action_rejects(self):
        self.bind()
        self.release()
        self.handle['settlement'][2]['action_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'original settlement'):
            self.experiment.observe_binding('batch')

    def test_duplicate_baseline_rejects(self):
        self.bind()
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            self.bind()

    def test_late_last_fleet_sample_not_persisted(self):
        self.experiment.pool.order = [str(i) for i in range(10)]
        now = [1]
        def observe(key):
            if key == '9':
                now[0] = 11
            return {}
        self.experiment.observe_binding = Mock(side_effect=observe)
        with patch('tools.standby_five_workspace.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError):
                self.experiment.collect_bindings('settled', deadline=10)
        self.assertFalse((self.experiment.output/'observer-bindings-settled.json').exists())


class FleetWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.experiment = Experiment(root, SimpleNamespace(), root/'output')
        self.experiment.health = Mock()
        self.experiment.pool = SimpleNamespace(order=[str(i) for i in range(10)])
        self.experiment.native_setup = SimpleNamespace(resolvers={}, witnesses={})
        self.path = root/'original.jsonl'
        self.path.write_text('original')
        for key in self.experiment.pool.order:
            witnesses, resolvers = {}, {}
            for index in range(50):
                n = int(key)*50+index
                row = dict(index=index, session_id='session-'+str(n),
                           pid=1000+n, birth=[100, n], surface_id='surface-'+str(n))
                witnesses[index] = dict(row)
                resolvers[index] = Mock(row=dict(row), return_value=self.path)
            self.experiment.native_setup.witnesses[key] = witnesses
            self.experiment.native_setup.resolvers[key] = resolvers

    def collect(self):
        with patch('tools.standby_five_workspace.time.monotonic', return_value=1):
            return self.experiment.collect_writers(deadline=10)

    def test_all_500_originals_reobserved(self):
        rows = self.collect()
        self.assertEqual(sum(map(len, rows.values())), 500)
        for cohort in self.experiment.native_setup.resolvers.values():
            for resolver in cohort.values():
                resolver.assert_called_once_with()

    def test_pending_last_writer_cannot_pass(self):
        self.experiment.native_setup.resolvers['9'][49].return_value = None
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.collect()

    def test_late_last_writer_cannot_pass(self):
        now = [1]
        def late():
            now[0] = 11
            return self.path
        self.experiment.native_setup.resolvers['9'][49].side_effect = late
        with patch('tools.standby_five_workspace.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError):
                self.experiment.collect_writers(deadline=10)

    def test_duplicate_process_or_session_or_surface_refused(self):
        for field in ('pid', 'session_id', 'surface_id'):
            with self.subTest(field=field):
                self.setUp()
                for collection in (self.experiment.native_setup.resolvers,
                                   self.experiment.native_setup.witnesses):
                    row = collection['9'][49]
                    row = row.row if isinstance(row, Mock) else row
                    row[field] = {'pid': 1000, 'session_id': 'session-0',
                                  'surface_id': 'surface-0'}[field]
                    if field == 'pid':
                        row['birth'] = [100, 0]
                with self.assertRaisesRegex(ValueError, 'duplicate'):
                    self.collect()

    def test_replaced_resolver_or_mutated_witness_during_read_refused(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.setUp()
                def mutate():
                    if replacement:
                        self.experiment.native_setup.resolvers['9'][49] = Mock()
                    else:
                        self.experiment.native_setup.witnesses['9'][49]['pid'] = 9999
                    return self.path
                self.experiment.native_setup.resolvers['9'][49].side_effect = mutate
                with self.assertRaisesRegex(ValueError, 'changed during'):
                    self.collect()


class AcceptanceProgressTests(unittest.TestCase):
    def test_absent_evidence_never_passes_and_names_missing_stages(self):
        experiment = Experiment.__new__(Experiment)
        experiment.pool = None
        experiment.completions, experiment.metrics = {}, {}
        experiment.report = {'cleanup_errors': []}
        experiment.acceptance_progress()
        self.assertFalse(experiment.report['passed'])
        self.assertIn('resource_sample', experiment.report['remaining_acceptance'])
        self.assertIn('bindings_completed', experiment.report['remaining_acceptance'])
        self.assertIn('run_terminal', experiment.report['remaining_acceptance'])

    def test_wrong_cohort_cannot_hide_behind_count_or_top_level_pass(self):
        experiment = Experiment.__new__(Experiment)
        keys = [str(i) for i in range(10)]
        experiment.pool = SimpleNamespace(order=keys)
        experiment.completions = dict.fromkeys(keys)
        experiment.metrics = {k: {'passed': True} for k in keys}
        experiment.metrics['foreign'] = experiment.metrics.pop('9')
        experiment.report = {'performance_passed': True, 'cleanup_errors': [],
                            'run_verification': {'succeeded': True}}
        experiment.acceptance_progress()
        self.assertFalse(experiment.report['acceptance_progress']['performance'])
        self.assertFalse(experiment.report['acceptance_progress']['run_terminal'])
        self.assertFalse(experiment.report['full_500_acceptance'])

    def test_resource_boolean_and_execution_error_remain_failures(self):
        experiment = Experiment.__new__(Experiment)
        experiment.pool = None
        experiment.completions, experiment.metrics = {}, {}
        experiment.report = {'error': '', 'cleanup_errors': [],
            'original_process_resources': {'native_process_count': 500,
                'auxiliary_process_count': 21, 'process_count': 521, 'retained_ui_count': True}}
        experiment.acceptance_progress()
        self.assertFalse(experiment.report['acceptance_progress']['resource_sample'])
        self.assertFalse(experiment.report['acceptance_progress']['no_execution_error'])


if __name__ == '__main__':
    unittest.main()
