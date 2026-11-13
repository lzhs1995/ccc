"""Lightweight preparation checks only: no Go, Codex, download, or listener."""
import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import fixture
import smoke
from common import FLOOR, SMOKE_HEADROOM, admit, no_portable_selector, pin, stop_owned, verify_source
from fetch_client import extract
from audit import audit

ROOT = Path(__file__).resolve().parent


def stdout_success():
    return '\n'.join(json.dumps(x) for x in [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': fixture.EXPECTED}},
        {'type': 'turn.completed'}])


def record():
    return {'first_real_delta_sent': 10, 'upstream_ping_events': 0,
            'completed_sent': True, 'completed_at': 30}


class FixtureTests(unittest.TestCase):
    def test_extracted_functions_byte_pinned(self):
        origin = json.loads((ROOT / 'FIXTURE-ORIGIN.json').read_text())
        raw = (ROOT / 'fixture.py').read_text()
        tree = ast.parse(raw)
        for name, row in origin['functions'].items():
            node = next(n for n in tree.body if getattr(n, 'name', None) == name)
            text = '\n'.join(raw.splitlines()[node.lineno-1:node.end_lineno]) + '\n'
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), row['sha256'])

    def test_original_positive_judge(self):
        self.assertTrue(fixture.judge(0, stdout_success(), '', [record()], True)['passed'])

    def test_original_baseline_judge(self):
        self.assertTrue(fixture.judge(1, '', 'idle timeout waiting for SSE', [record()], False)['passed'])

    def test_success_requires_native_terminal(self):
        with self.assertRaises(RuntimeError): fixture.judge(0, '', '', [record()], True)

    def test_success_requires_exact_text(self):
        with self.assertRaises(RuntimeError): fixture.judge(0, stdout_success().replace(fixture.EXPECTED, 'partial'), '', [record()], True)

    def test_success_requires_supplier_completion(self):
        row = record();row['completed_sent'] = False
        with self.assertRaises(RuntimeError): fixture.judge(0, stdout_success(), '', [row], True)

    def test_success_requires_quiet_interval(self):
        row = record();row['completed_at'] = 28
        with self.assertRaises(RuntimeError): fixture.judge(0, stdout_success(), '', [row], True)

    def test_retries_rejected(self):
        with self.assertRaises(RuntimeError): fixture.judge(0, stdout_success(), '', [record(), record()], True)

    def test_upstream_ping_rejected(self):
        row = record();row['upstream_ping_events'] = 1
        with self.assertRaises(RuntimeError): fixture.judge(0, stdout_success(), '', [row], True)

    def test_unrelated_baseline_failure_rejected(self):
        with self.assertRaises(RuntimeError): fixture.judge(1, '', 'permission denied', [record()], False)

    def test_baseline_cannot_complete(self):
        with self.assertRaises(RuntimeError): fixture.judge(0, stdout_success(), '', [record()], False)

    def test_tool_event_rejected(self):
        extra = json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution'}})
        with self.assertRaises(RuntimeError): fixture.judge(0, extra + '\n' + stdout_success(), '', [record()], True)

    def test_half_frame_bytes_rejoin(self):
        a, b = fixture.half_frame()
        self.assertNotIn(b'\n\n', a)
        self.assertIn(b'LOCAL_PASSING_COMPLETE', a+b)

    def test_no_supplier_generated_ping(self):
        self.assertNotIn(b'event: ping', fixture.prefix() + fixture.finish(True))


class IsolationTests(unittest.TestCase):
    def test_environment_preserves_home(self):
        with patch.dict(os.environ, {'HOME': '/synthetic-home', 'CODEX_HOME': '/synthetic-codex'}, clear=True):
            env = smoke.child_environment(Path('/private/fixture'))
            self.assertEqual(env['HOME'], '/synthetic-home')
            self.assertEqual(env['CODEX_HOME'], '/synthetic-codex')

    def test_absent_codex_home_stays_absent(self):
        with patch.dict(os.environ, {'HOME': '/synthetic-home'}, clear=True):
            self.assertNotIn('CODEX_HOME', smoke.child_environment(Path('/private/fixture')))

    def test_parent_credentials_not_copied(self):
        with patch.dict(os.environ, {'HOME': '/synthetic-home', 'OPENAI_API_KEY': 'synthetic-no-real-key'}, clear=True):
            self.assertNotIn('OPENAI_API_KEY', smoke.child_environment(Path('/private/fixture')))

    def test_policy_exact_network_and_id(self):
        text = smoke.policy(Path('/fixture/bin'), Path('/fixture/private'), 39101, installation=Path('/fixture/id'))
        self.assertIn('(remote ip "localhost:39101")', text)
        self.assertNotIn('(allow process-fork', text)
        self.assertNotIn('(subpath "/fixture")', text)
        self.assertIn('(literal "/fixture/id")', text)
        self.assertNotIn('501', text.replace(str(os.getuid()), 'UID'))

    def test_all_probe_controls_required(self):
        good = {k: True for k in smoke.PROBE_CODEX}
        self.assertTrue(smoke.probe_result(0, json.dumps(good), True))
        for name in good:
            bad = dict(good);del bad[name]
            self.assertFalse(smoke.probe_result(0, json.dumps(bad), True))

    def test_probe_bool_return_not_zero(self):
        self.assertFalse(smoke.probe_result(False, json.dumps({k: True for k in smoke.PROBE_BASE}), False))

    def test_probe_int_truth_not_true(self):
        self.assertFalse(smoke.probe_result(0, json.dumps({k: 1 for k in smoke.PROBE_BASE}), False))

    def test_own_installation_created_and_cas_removed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            target, before = smoke.create_installation({'HOME': str(root)})
            self.assertEqual(smoke.identity(target), before)
            smoke.remove_installation(target, before, True)
            self.assertFalse(target.parent.exists())

    def test_existing_codex_home_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve();(root/'.codex').mkdir()
            with self.assertRaises(RuntimeError):smoke.create_installation({'HOME': str(root)})

    def test_changed_id_not_removed(self):
        with tempfile.TemporaryDirectory() as d:
            target, before = smoke.create_installation({'HOME': str(Path(d).resolve())})
            target.write_text('synthetic changed')
            with self.assertRaises(RuntimeError):smoke.remove_installation(target, before, True)
            self.assertTrue(target.exists())

    def test_live_child_no_id_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            target, before = smoke.create_installation({'HOME': str(Path(d).resolve())})
            with self.assertRaises(RuntimeError):smoke.remove_installation(target, before, False)
            self.assertTrue(target.exists())


class CapacitySourceAndArchiveTests(unittest.TestCase):
    def test_binary_without_portable_selector(self):
        with tempfile.TemporaryDirectory() as d:
            binary=Path(d)/'magpie';binary.write_text('synthetic')
            (Path(d)/'xdg-data').mkdir()
            no_portable_selector(binary)

    def test_build_data_selector_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            binary=Path(d)/'magpie';binary.write_text('synthetic');(Path(d)/'data').mkdir()
            with self.assertRaises(RuntimeError):no_portable_selector(binary)

    def test_portable_marker_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            binary=Path(d)/'magpie';binary.write_text('synthetic');(Path(d)/'.portable').touch()
            with self.assertRaises(RuntimeError):no_portable_selector(binary)

    def test_dangling_selector_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            binary=Path(d)/'magpie';binary.write_text('synthetic');(Path(d)/'data').symlink_to(Path(d)/'missing')
            with self.assertRaises(RuntimeError):no_portable_selector(binary)

    def test_floor_rejects_before_command(self):
        with patch('common.capacity', return_value={'free_bytes': FLOOR + SMOKE_HEADROOM - 1}), patch('common.time.sleep'):
            with self.assertRaises(RuntimeError): admit(Path('.'), SMOKE_HEADROOM, [])

    def test_two_capacity_samples(self):
        samples = []
        with patch('common.capacity', return_value={'free_bytes': FLOOR + SMOKE_HEADROOM}), patch('common.time.sleep'):
            admit(Path('.'), SMOKE_HEADROOM, samples)
        self.assertEqual(len(samples), 2)

    @patch('common.os.killpg')
    def test_exited_child_never_signalled(self, kill):
        self.assertEqual(stop_owned(Mock(poll=Mock(return_value=0), wait=Mock(return_value=0))), 0)
        kill.assert_not_called()

    @patch('common.os.killpg')
    def test_owned_child_stopped(self, kill):
        self.assertEqual(stop_owned(Mock(pid=12, poll=Mock(return_value=None), wait=Mock(return_value=-15))), -15)
        kill.assert_called_once()

    def test_source_membership_strict(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'a').write_text('fixture')
            m={'source_files':[dict(path='a', **pin(root/'a'))]}
            verify_source(root,m);(root/'extra').write_text('extra')
            with self.assertRaises(RuntimeError):verify_source(root,m)

    def archive(self, root, name='codex-test', payload=b'fixture', symlink=False):
        p=root/'asset.tgz'
        with tarfile.open(p, 'w:gz') as tar:
            info=tarfile.TarInfo(name)
            if symlink:info.type=tarfile.SYMTYPE;info.linkname='/unrelated'
            else:info.size=len(payload)
            tar.addfile(info, None if symlink else io.BytesIO(payload))
        return p

    def test_fixed_binary_extract(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=self.archive(root)
            spec={'binary_name':'codex-test','binary_bytes':7,'binary_sha256':hashlib.sha256(b'fixture').hexdigest()}
            extract(p,root/'binary',spec)
            self.assertEqual((root/'binary').read_bytes(),b'fixture')

    def test_different_binary_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=self.archive(root)
            with self.assertRaises(RuntimeError):extract(p,root/'binary',{'binary_name':'codex-test','binary_bytes':7,'binary_sha256':'0'*64})

    def test_archive_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=self.archive(root,symlink=True)
            with self.assertRaises(RuntimeError):extract(p,root/'binary',{'binary_name':'codex-test','binary_bytes':0,'binary_sha256':'0'*64})

    def test_archive_path_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=self.archive(root,name='../codex-test')
            with self.assertRaises(RuntimeError):extract(p,root/'binary',{'binary_name':'codex-test','binary_bytes':7,'binary_sha256':'0'*64})


class EvidenceEncodingTests(unittest.TestCase):
    def test_binary_generated_file_is_hashed_not_decoded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root/'config/magpie'; config.mkdir(parents=True)
            (config/'providers.json').write_text('{"providers": []}')
            binary = config/'generated-cache'; binary.write_bytes(b'\xca\xfe\xba\xbe\x00')
            result = smoke.collect_inputs(root)
            self.assertEqual(result['config/magpie/generated-cache'],
                             {'pin': pin(binary), 'encoding': 'digest-only'})
            self.assertEqual(result['config/magpie/providers.json']['text'], '{"providers": []}')

    def test_invalid_known_text_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'gateway.sb').write_bytes(b'\xca\xfe')
            with self.assertRaises(UnicodeDecodeError): smoke.collect_inputs(root)

    def test_symlink_is_rejected_without_following(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root/'config/magpie'; config.mkdir(parents=True)
            (config/'cache').symlink_to(root/'missing')
            with self.assertRaises(RuntimeError): smoke.collect_inputs(root)

    def test_only_fixed_policies_are_decoded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'gateway-probe.sb').write_text('(deny default)')
            (root/'unknown.sb').write_bytes(b'\xca\xfe')
            result = smoke.collect_inputs(root)
            self.assertEqual(set(result), {'gateway-probe.sb'})
            self.assertEqual(result['gateway-probe.sb']['text'], '(deny default)')


    def test_config_parent_symlink_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base/'owned'; root.mkdir()
            outside = base/'outside'; (outside/'magpie').mkdir(parents=True)
            (outside/'magpie/providers.json').write_text('synthetic outside')
            (root/'config').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'ancestor became a symlink'):
                smoke.collect_inputs(root)

    def test_config_parent_dangling_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'config').symlink_to(root/'missing')
            with self.assertRaisesRegex(RuntimeError, 'ancestor became a symlink'):
                smoke.collect_inputs(root)

    def test_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base/'owned'; root.mkdir()
            alias = base/'alias'; alias.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'owned directory'):
                smoke.collect_inputs(alias)

    def test_runtime_fifo_is_rejected_without_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root/'config/magpie'; config.mkdir(parents=True)
            os.mkfifo(config/'generated-cache')
            with self.assertRaisesRegex(RuntimeError, 'not regular'):
                smoke.collect_inputs(root)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pkg = self.root/'pkg';self.pkg.mkdir()
        self.out = self.root/'smoke';self.out.mkdir()
        self.client = self.root/'client';self.client.mkdir()
        self.folders = {}
        def save(path, value):path.write_text(json.dumps(value))
        self.save = save
        (self.client/'codex').write_bytes(b'client-fixture')
        self.spec = dict(binary_name='codex',binary_sha256=pin(self.client/'codex')['sha256'],binary_bytes=14,
                         archive_sha256='a'*64,archive_bytes=1)
        save(self.pkg/'CLIENT-ASSET.json',self.spec)
        save(self.client/'CLIENT.json',dict(status='PASS',spec=self.spec,package_pin='PIN',binary=pin(self.client/'codex'),
                                           archive={'sha256':'a'*64,'bytes':1}))
        for kind in ['candidate','baseline']:
            folder=self.root/kind;folder.mkdir();self.folders[kind]=folder
            save(self.pkg/(kind+'-source.json'),{'source':'synthetic '+kind,'toolchain':'go1.26.8','original_sdk':'MacOSX26.5.sdk'})
            (folder/'magpie').write_text(kind)
            save(folder/'BUILD.json',dict(status='PASS',source_after_match=True,variant=kind,package_pin='PIN',
                 same_historical_binary_claimed=False,source_manifest=pin(self.pkg/(kind+'-source.json')),
                 binary_name='magpie',binary=pin(folder/'magpie'),
                 environment=dict(go='go1.26.8',target='darwin/arm64',original_sdk='MacOSX26.5.sdk',sdk='15.5',clang='synthetic clang')))
            for step in ['modules','compile']:
                sub=folder/step;sub.mkdir()
                save(sub/'RESULT.json',dict(status='PASS',returncode=0,child_reaped=True,logs={},samples=[{'free_bytes':40*2**30}]*2))
        rows=[]
        for name in smoke.CASE_NAMES:
            folder=self.out/name;folder.mkdir();success=name!='baseline-comments';kind='candidate' if success else 'baseline'
            (folder/'codex.jsonl').write_text(stdout_success() if success else '')
            (folder/'codex.stderr').write_text('' if success else 'idle timeout waiting for SSE')
            for prefix,controls in [('gateway',smoke.PROBE_BASE),('codex',smoke.PROBE_CODEX)]:
                (folder/(prefix+'-probe.stdout')).write_text(json.dumps({k:True for k in controls}))
            validation=fixture.judge(0 if success else 1,(folder/'codex.jsonl').read_text(),
                                      (folder/'codex.stderr').read_text(),[record()],success)
            row=dict(status='PASS',name=name,children_reaped=True,handlers_finished=True,artifact_unchanged=True,
                     codex_unchanged=True,installation_unchanged=True,private_removed=True,
                     owned_children=[{'returncode':-15},{'returncode':0 if success else 1}],
                     samples=[{'free_bytes':40*2**30}]*2,artifact=pin(self.folders[kind]/'magpie'),
                     codex=pin(self.client/'codex'),requests=[record()],validation=validation,
                     files={p.name:pin(p) for p in folder.iterdir()})
            save(folder/'RESULT.json',row);rows.append(dict(name=name,result=pin(folder/'RESULT.json')))
        save(self.out/'ACCEPTANCE.json',dict(status='PASS',package_pin='PIN',cleanup_complete=True,
             HOME_unchanged=True,CODEX_HOME_unchanged=True,old_collector_reused=False,cases=rows,inputs={}))

    def call(self):
        with patch('audit.verify_package'):
            return audit(self.pkg,'PIN',self.out,self.folders['candidate'],self.folders['baseline'],self.client)

    def mutate(self, field, value):
        path=self.out/'comments/RESULT.json';row=json.loads(path.read_text());row[field]=value;self.save(path,row)
        path2=self.out/'ACCEPTANCE.json';outer=json.loads(path2.read_text());outer['cases'][2]['result']=pin(path);self.save(path2,outer)

    def test_complete_synthetic_graph(self):self.assertEqual(self.call()['status'],'PASS')

    def test_changed_case_raw_log(self):
        (self.out/'silent/codex.jsonl').write_text('changed')
        with self.assertRaises(RuntimeError):self.call()

    def test_unsafe_case_reference(self):
        self.mutate('files',{'../unrelated':{'sha256':'0'*64,'bytes':1}})
        with self.assertRaises(RuntimeError):self.call()

    def test_wrong_binary_category(self):
        self.mutate('artifact',pin(self.folders['baseline']/'magpie'))
        with self.assertRaises(RuntimeError):self.call()

    def test_floor_dropped(self):
        self.mutate('samples',[{'free_bytes':29*2**30}]*2)
        with self.assertRaises(RuntimeError):self.call()

    def test_cleanup_not_complete(self):
        self.mutate('children_reaped',False)
        with self.assertRaises(RuntimeError):self.call()

    def test_client_changed(self):
        (self.client/'codex').write_text('changed')
        with self.assertRaises(RuntimeError):self.call()

    def test_subset_not_all_four(self):
        path=self.out/'ACCEPTANCE.json';d=json.loads(path.read_text());d['cases'].pop();self.save(path,d)
        with self.assertRaises(RuntimeError):self.call()

    def test_build_capacity_missing(self):
        path=self.folders['candidate']/'compile/RESULT.json';d=json.loads(path.read_text());d['samples']=[];self.save(path,d)
        with self.assertRaises(RuntimeError):self.call()

    def test_build_sdk_missing(self):
        path=self.folders['baseline']/'BUILD.json';d=json.loads(path.read_text());del d['environment']['sdk'];self.save(path,d)
        with self.assertRaises(RuntimeError):self.call()


if __name__ == '__main__':
    unittest.main(verbosity=2)
