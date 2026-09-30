"""Offline origin, identity and clock counterexamples; no native/model requests."""
import copy
import contextlib
import datetime as dt
import json
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

import ccc_batch_timing as timing
import ccc_workspace_batch as batch
import cmux_codex_watch as core
import cmux_supervisor_tui as tui
from tests import test_workspace_batch as workspace_fixtures


def clock(value):
    return {'wall':10000+value, 'monotonic':value, 'boot_id':'boot-fixture'}


def fixture():
    wid = str(uuid.uuid4())
    with patch.object(timing, 'source_hashes', return_value={'fixture':'a'*64}):
        trace = timing.new('private_batch_workspace', wid, 'keyboard', 'group', clock(100))
    for i,phase in enumerate(('confirmation_accepted','action_enqueued','action_started','cli_received'), 1):
        trace['events'].append({'phase':phase, **clock(100+i*.1)})
    job = {'id':str(uuid.uuid4()), 'workspace_id':wid, 'ui_timing_origin':copy.deepcopy(trace), 'slots':[]}
    trace['events'].append({'phase':'job_created', **clock(100.5), 'new_job':True, 'job_id':job['id']})
    trace['events'].append({'phase':'action_finished', **clock(100.6)})
    for i in range(50):
        sid, session, turn = (str(uuid.uuid4()) for _ in range(3))
        born = [9000, i]
        identity = dict(surface_id=sid, workspace_id=wid, session_id=session, turn_id=turn,
                        pid=1000+i, birth=born, verified=True)
        job['slots'].append(dict(index=i, phase='confirmed', surface_id=sid, session_id=session,
            pid=1000+i, native_birth=born,
            ui_original_identity={k:v for k,v in identity.items() if k not in {'turn_id','verified'}},
            confirmation=dict(confirmed=True, session_id=session,
                task_id=turn, task_at=dt.datetime.fromtimestamp(10100.7,dt.timezone.utc).isoformat(), first_task_observed=clock(100.8),
                first_task_observed_identity=identity)))
    return trace, job


class TimingEvidenceTests(unittest.TestCase):
    def evaluate(self, trace, job):
        return timing.evaluate(trace, job, current_hashes={'fixture':'a'*64})

    def test_both_origins_and_fifty_bound_tasks_prove_timely_upper_bound(self):
        trace, job = fixture()
        result = self.evaluate(trace, job)
        self.assertTrue(result['startup_passed'])
        self.assertEqual(result['verdict'], 'timely_upper_bound')
        self.assertEqual(len(result['original_tasks']), 50)
        self.assertAlmostEqual(result['confirmation_seconds'], .1)
        self.assertAlmostEqual(result['original_tasks'][0]['input_upper_bound_seconds'], .8)
        self.assertAlmostEqual(result['original_tasks'][0]['confirmation_upper_bound_seconds'], .7)

    def test_slow_observation_is_unproven_not_proof_of_actual_lateness(self):
        trace, job = fixture()
        job['slots'][0]['confirmation']['first_task_observed'] = clock(102)
        result = self.evaluate(trace, job)
        self.assertTrue(result['evidence_complete'])
        self.assertFalse(result['startup_passed'])
        self.assertEqual(result['verdict'], 'not_proven')

    def test_old_job_missing_task_reused_turn_and_drift_cannot_pass(self):
        for fault in ('reused_job','old_origin','missing','duplicate_turn','pid','birth','hash','clock','boot'):
            trace, job = fixture()
            proof = job['slots'][0]['confirmation']
            if fault == 'reused_job':trace['events'][-2]['phase'] = 'job_reused'
            elif fault == 'old_origin':job['ui_timing_origin']['action_id'] = str(uuid.uuid4())
            elif fault == 'missing':proof.pop('first_task_observed')
            elif fault == 'duplicate_turn':
                proof['task_id'] = job['slots'][1]['confirmation']['task_id']
                proof['first_task_observed_identity']['turn_id'] = proof['task_id']
            elif fault == 'pid':job['slots'][0]['pid'] = 9999
            elif fault == 'birth':proof['first_task_observed_identity']['birth'] = [9000, 999]
            elif fault == 'hash':trace['source_hashes']['fixture'] = 'b'*64
            elif fault == 'clock':proof['first_task_observed']['wall'] += 100
            elif fault == 'boot':proof['first_task_observed']['boot_id'] = 'another-boot'
            with self.subTest(fault=fault):
                self.assertFalse(self.evaluate(trace, job)['startup_passed'])

    def test_phase_semantics_reject_negative_intervals_even_when_clocks_increase(self):
        trace, _ = fixture()
        trace['events'][0]['phase'], trace['events'][1]['phase'] = 'confirmation_accepted', 'input_read'
        result = timing.intervals(trace)
        self.assertIn('negative_interval:confirmation_seconds', result['problems'])
        self.assertIn('missing_input_origin', result['problems'])
        self.assertFalse(result['startup_passed'])

    def test_native_wall_creation_order_and_blocked_proof_reject_false_green(self):
        for fault, expected in (
            ('future', 'native_wall_outside_observation_window'),
            ('early', 'native_observed_before_creation'),
            ('blocked', 'incomplete_native_identity'),
        ):
            trace, job = fixture()
            proof = job['slots'][0]['confirmation']
            if fault == 'future':
                proof['task_at'] = dt.datetime.fromtimestamp(13700.7, dt.timezone.utc).isoformat()
            elif fault == 'early':
                proof['first_task_observed'] = clock(100.3)
            else:
                proof['blocked'] = 'original transcript changed or truncated'
            with self.subTest(fault=fault):
                result = self.evaluate(trace, job)
                self.assertFalse(result['startup_passed'])
                self.assertIn(expected, result['problems'])

    def test_nonfinite_clock_and_path_escape_are_rejected(self):
        trace, _ = fixture()
        trace['events'][0]['monotonic'] = float('nan')
        with self.assertRaises(ValueError):timing.validate(trace)
        for identifier in ('../other', None, 'not-a-uuid'):
            with self.assertRaises(ValueError):timing.path(Path('config.json'), identifier)

    def test_collector_binds_report_to_exact_action_job_and_boot(self):
        from tools import ui_batch_acceptance as collector
        with tempfile.TemporaryDirectory() as directory:
            config, output = Path(directory)/'config.json', Path(directory)/'result.json'
            trace, job = fixture()
            timing.save(config, trace)
            core.atomic_write_json(batch.job_path(config, job['id']), job)
            with patch.object(sys, 'argv', ['collector', '--config', str(config), '--action-id',
                    trace['action_id'], '--output', str(output)]), \
                    patch.object(timing, 'source_hashes', return_value={'fixture':'a'*64}):
                self.assertEqual(collector.main(), 0)
                original = output.read_bytes()
                with self.assertRaises(FileExistsError):
                    collector.main()
                self.assertEqual(output.read_bytes(), original)
            report = json.loads(output.read_text())
            self.assertEqual(report['action_id'], trace['action_id'])
            self.assertEqual(report['job_id'], job['id'])
            self.assertEqual(report['boot_id'], 'boot-fixture')
            self.assertEqual(report['events'], trace['events'])
            self.assertEqual(len(report['trace_sha256']), 64)
            self.assertEqual(len(report['job_sha256']), 64)

    def test_collector_rechecks_runtime_and_its_own_source_before_output(self):
        from tools import ui_batch_acceptance as collector
        for fault in ('runtime', 'collector'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                config, output = Path(directory)/'config.json', Path(directory)/'result.json'
                trace, job = fixture()
                timing.save(config, trace)
                core.atomic_write_json(batch.job_path(config, job['id']), job)
                with patch.object(sys, 'argv', ['collector', '--config', str(config), '--action-id',
                        trace['action_id'], '--output', str(output)]), \
                        patch.object(timing, 'source_hashes', side_effect=[{'fixture':'a'*64},
                            {'fixture':('b' if fault == 'runtime' else 'a')*64}]), \
                        patch.object(collector, 'collector_hash', side_effect=['a'*64,
                            ('b' if fault == 'collector' else 'a')*64]):
                    self.assertEqual(collector.main(), 1)
                result = json.loads(output.read_text())
                self.assertFalse(result['startup_passed'])
                self.assertIn('source_changed_during_collection', result['problems'])


class BatchOriginTests(unittest.TestCase):
    setUp = workspace_fixtures.WorkspaceBatchTests.setUp

    def test_first_task_observation_waits_for_prompt_and_is_never_restamped(self):
        self.worker.job['ui_timing_origin'] = {'action_id':str(uuid.uuid4())}
        session, turn = str(uuid.uuid4()), str(uuid.uuid4())
        transcript = self.root/'timing.jsonl'
        transcript.write_text(json.dumps({'type':'session_meta', 'payload':{'id':session}})+'\n')
        slot = dict(surface_id=str(uuid.uuid4()), session_id=session, pid=1234, native_birth=[9000,1],
                    transcript=str(transcript), transcript_offset=0, submit_at=10100)
        def append(payload):
            with transcript.open('a') as handle:
                handle.write(json.dumps({'type':'event_msg','timestamp':'1970-01-01T02:48:20.700000+00:00',
                                         'payload':payload})+'\n')
        append({'type':'task_started','turn_id':turn})
        with patch.object(timing,'stamp',return_value=clock(100.8)) as stamp, \
                patch('ccc_guard_scope.birth',return_value=[9000,1]):
            self.assertFalse(self.worker._confirm(slot))
            original = copy.deepcopy(slot['confirmation']['first_task_observed'])
            append({'type':'user_message','message':batch.job_prompt(self.worker.job)})
            self.assertTrue(self.worker._confirm(slot))
            self.assertTrue(self.worker._confirm(slot))
            stamp.assert_called_once()
        self.assertEqual(slot['confirmation']['first_task_observed'], original)
        self.assertTrue(slot['confirmation']['first_task_observed_identity']['verified'])

    def test_birth_query_failure_invalidates_timing_without_blocking_confirmation(self):
        self.worker.job['ui_timing_origin'] = {'action_id':str(uuid.uuid4())}
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch('ccc_guard_scope.birth',side_effect=OSError('fixture birth unavailable')):
            self.worker._advance(slot)
            self.worker._advance(slot)
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertFalse(slot['confirmation']['first_task_observed_identity']['verified'])

    def test_new_job_keeps_first_origin_and_repeated_action_cannot_replace_it(self):
        self.wid = str(uuid.uuid4())
        trace, _ = fixture();trace['workspace_id'] = self.wid
        trace['events'] = trace['events'][:5]
        first = batch.start(self.config, self.wid, client=self.client, launch=False,
                            private_check=True, ui_trace=trace)
        original = core.load_json(batch.job_path(self.config, first['job_id']), {})['ui_timing_origin']
        again = copy.deepcopy(original);again['action_id'] = str(uuid.uuid4())
        second = batch.start(self.config, self.wid, client=self.client, launch=False,
                             private_check=True, ui_trace=again)
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(core.load_json(batch.job_path(self.config, first['job_id']), {})['ui_timing_origin'], original)
        self.assertEqual(trace['events'][-1]['phase'], 'job_created')
        self.assertEqual(again['events'][-1]['phase'], 'job_reused')
        self.assertEqual(self.client.calls, [])

    def test_workspace_and_mode_mismatch_invalidates_only_timing(self):
        trace, _ = fixture()
        for mode, wid in (('N', self.wid), ('b', 'wrong-workspace')):
            trace.update(mode=mode, workspace_id=wid)
            result = batch.start(self.config, self.wid, client=self.client, launch=False,
                                 private_check=True, ui_trace=trace)
            self.assertNotIn('ui_timing_origin', core.load_json(batch.job_path(self.config,result['job_id']), {}))
        self.assertEqual(self.client.calls, [])

    def test_timing_write_failure_does_not_prevent_authorized_launch(self):
        self.wid = str(uuid.uuid4())
        trace, _ = fixture();trace['workspace_id'] = self.wid;trace['events'] = trace['events'][:5]
        with patch.object(timing,'record',side_effect=OSError(28,'fixture full disk')), \
                patch.object(batch,'_launch') as launch:
            result = batch.start(self.config,self.wid,client=self.client,private_check=True,ui_trace=trace)
        launch.assert_called_once()
        self.assertTrue(batch.job_path(self.config,result['job_id']).exists())


class PanelActionTimingTests(unittest.TestCase):
    def test_real_input_loop_preserves_keyboard_mouse_group_and_member_origins(self):
        for kind in ('group', 'member'):
            for mode in ('B', 'b', 'N'):
                for input_kind in ('keyboard', 'mouse'):
                    with self.subTest(kind=kind, mode=mode, input_kind=input_kind), tempfile.TemporaryDirectory() as directory:
                        wid = str(uuid.uuid4())
                        candidate = tui.Candidate({'workspace_id':wid, 'workspace_ref':'workspace:5',
                            'surface_id':str(uuid.uuid4()), 'ref':'surface:1'}, 'explicit', '', '', 0, False)
                        row = tui.ViewRow(kind, wid, 'workspace:5', 'fixture', {},
                                          candidate if kind == 'member' else None)
                        model = Mock(config_path=Path(directory)/'config.json', candidates=[candidate],
                                     suggested_surface='')
                        model.poll_action.return_value = None
                        keys = iter((tui.curses.KEY_MOUSE if input_kind == 'mouse' else ord(mode), ord('q')))
                        screen = Mock()
                        screen.getch.side_effect = lambda: next(keys)
                        screen.getmaxyx.return_value = (40, 200)
                        model.start_action.return_value = 'fixture action'
                        with contextlib.ExitStack() as stack:
                            for name in ('curs_set','mousemask','mouseinterval'):
                                stack.enter_context(patch.object(tui.curses, name))
                            stack.enter_context(patch.object(tui, 'init_colors'))
                            stack.enter_context(patch.object(tui, '_draw'))
                            stack.enter_context(patch.object(tui, 'build_view_rows', return_value=[row]))
                            stack.enter_context(patch.object(tui, 'workspace_buttons', return_value=[(ord(mode), 0, 10, mode)]))
                            stack.enter_context(patch.object(tui.curses, 'getmouse', return_value=(0, 1,
                                tui.layout(40)['focus_keys'], 0, tui.curses.BUTTON1_CLICKED)))
                            stack.enter_context(patch.object(tui, '_confirm', return_value=True))
                            tui._run(screen, model)
                        model.start_action.assert_called_once()
                        trace = model.start_action.call_args.kwargs['ui_trace']
                        self.assertEqual((trace['mode'], trace['workspace_id'], trace['input_kind'], trace['row_kind']),
                            (mode, wid, input_kind, 'group' if kind == 'group' else 'candidate'))
                        self.assertEqual([e['phase'] for e in trace['events']], ['input_read','confirmation_accepted'])
                        model.start_action.call_args.args[0]()
                        mutation = model.mutate_workspace if kind == 'group' else model.mutate_selected
                        mutation.assert_called_once()

    def test_evidence_save_failure_keeps_action_running_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            model = tui.SupervisorModel(Path(directory)/'config.json')
            trace, _ = fixture(); trace['events'] = trace['events'][:2]
            called = []
            with patch.object(timing, 'save', side_effect=OSError(28, 'fixture full disk')):
                model.start_action(lambda: called.append(True), 'done', ui_trace=trace)
                model._action_thread.join(2)
            self.assertEqual(called, [True])
            self.assertEqual(trace['recording_error'], 'OSError')
            self.assertIn('timing_recording_failed', timing.intervals(trace)['problems'])
            model.close()

    def test_busy_action_is_recorded_without_running_the_second_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            model = tui.SupervisorModel(Path(directory)/'config.json')
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            def operation():entered.set();release.wait(5)
            model.start_action(operation, 'done')
            self.assertTrue(entered.wait(1))
            trace, _ = fixture();trace['events'] = trace['events'][:2]
            called = []
            result = model.start_action(lambda:called.append(True), 'done', ui_trace=trace)
            self.assertIn('上一操作', result)
            self.assertEqual(called, [])
            self.assertEqual(timing.read(model.config_path, trace['action_id'])['events'][-1]['phase'], 'rejected_busy')
            release.set();model._action_thread.join(2);model.close()

    def test_action_context_reaches_only_its_internal_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            model = tui.SupervisorModel(Path(directory)/'config.json')
            trace, _ = fixture();trace['events'] = trace['events'][:2]
            result = type('Result', (), {'returncode':0,'stdout':'{}','stderr':''})()
            with patch.object(tui.subprocess, 'run', return_value=result) as run:
                model.start_action(lambda:model.run_cli(['batch-workspace',trace['workspace_id']]),
                                   'done', ui_trace=trace)
                model._action_thread.join(2)
                self.assertFalse(model._action_thread.is_alive())
                self.assertEqual(run.call_args.args[0][-2:], ['--ui-action-id',trace['action_id']])
                model.run_cli(['batch-workspace',trace['workspace_id']])
                self.assertNotIn('--ui-action-id', run.call_args.args[0])
            model.close()


class CliTimingTests(unittest.TestCase):
    def test_cli_binds_action_and_preserves_selected_mode_when_evidence_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.json'
            for missing in (False, True):
                trace, _ = fixture(); trace['events'] = trace['events'][:4]
                timing.save(config, trace)
                if missing:
                    timing.path(config, trace['action_id']).unlink()
                with patch.object(batch, 'start', return_value={'job_id':'fixture'}) as start:
                    self.assertEqual(core.cli(['--config', str(config), 'batch-workspace',
                        trace['workspace_id'], '--private-check', '--ui-action-id', trace['action_id']]), 0)
                self.assertEqual(start.call_args.args, (config, trace['workspace_id']))
                self.assertTrue(start.call_args.kwargs['private_check'])
                actual = start.call_args.kwargs['ui_trace']
                if missing:
                    self.assertIsNone(actual)
                else:
                    self.assertEqual(actual['action_id'], trace['action_id'])
                    self.assertEqual(actual['events'][-1]['phase'], 'cli_received')
