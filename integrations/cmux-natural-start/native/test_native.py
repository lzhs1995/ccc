"""Offline functional fixtures only: no Swift, GUI, builds or network."""
from pathlib import Path
import contextlib
import copy
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch
import contract as c
import evidence_native as e
import download_native as d
import artifact_io
import source_chain

ROOT=Path(__file__).resolve().parent
IDENTITY={'repository':'lzhs1995/ccc','head_sha':'a'*40,'run_id':12345,'run_attempt':1}

def put(root,name,value):
    p=root/name
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,indent=2)+'\n' if not isinstance(value,str) else value)

def seal(root):
    put(root,'MANIFEST.json',{'schema':'cmux-natural-native-evidence-manifest-v1','files':e._members(root)})

@contextlib.contextmanager
def synthetic_artifact():
    with tempfile.TemporaryDirectory(prefix='natural-native-offline-') as temp:
        root=Path(temp)
        value=c.admit(ROOT)
        contract_sha=c.digest(ROOT/'contract.json')
        shutil.copyfile(ROOT/'contract.json',root/'contract.json')
        shutil.copytree(ROOT/'inputs',root/'inputs')
        put(root,'console-source-preflight.log','NESTED_APP_HOST_ENV_OK=/fixture/derived\nRESTART_BUDGET=0\nATTEMPTS=1\n')
        put(root,'app-host-cleanup.log','Synthetic owned test-home cleanup fixture.\n')
        frozen=c.read(ROOT/'inputs/source-freeze.json')
        sources={}
        for i,stage in enumerate(('base','red','green')):
            rows=[]
            for name,pin in frozen['files'].items():
                prefix='candidate' if stage=='green' or (stage=='red' and name=='cmuxTests/SessionPersistenceTests.swift') else 'original'
                rows.append({'path':name,'sha256':pin[prefix+'_sha256'],'bytes':pin[prefix+'_bytes']})
            source={'stage':stage,'commit':str(i+1)*40,'parent_commit':value['source']['upstream_commit'] if i==0 else str(i)*40,'tree':value['source']['base_green_tree' if stage=='base' else stage+'_tree'],'files':rows}
            sources[stage]=source
            put(root,'SOURCE-'+stage+'.json',{'schema':'cmux-natural-native-source-v1','run':IDENTITY,'source':source,'contract_sha256':contract_sha,'fixed_source':value['source']})
        for stage in ('red','green'):
            name='build-'+sources[stage]['commit']+'.log'
            put(root,name,'Synthetic completed test-product compilation fixture.\n')
            put(root,'COMPILE-'+stage+'.json',{'schema':'cmux-natural-native-compile-v1','phase':stage,'run':IDENTITY,'source':sources[stage],'contract_sha256':contract_sha,'exit_code':0,'log':c.pin(root/name)})
        for phase in c.PHASES:
            path=root/phase
            path.mkdir()
            intent={'schema':'cmux-natural-native-phase-v1','phase':phase,'run':IDENTITY,'source':sources['red' if phase=='red' else 'green'],'contract_sha256':contract_sha}
            put(path,'INTENT.json',intent)
            if phase=='package':
                log='\n'.join('✔ Test "'+name+'" passed after 0.001 seconds.' for name in value['package_test_display_names'])
                log+='\n✔ Suite "TerminalSurfaceStartupRestorePolicyTests" passed after 0.01 seconds.\n✔ Test run with 6 tests passed after 0.01 seconds.\n'
                put(path,'wrapper.log','synthetic watchdog completed\n')
                argv=['python3','watchdog','--label','TerminalSurfaceStartupRestorePolicyTests','--','swift','test','--filter','TerminalSurfaceStartupRestorePolicyTests']
                count=6
            else:
                expected=value['phases'][phase]['required_test_ids']
                typed={'testNodes':[{'nodeType':'Test Case','nodeIdentifier':name,'result':'Failed' if phase=='red' and name in value['expected_red_failed_ids'] else 'Passed'} for name in expected]}
                put(path,'tests.json',typed)
                shutil.copyfile(path/'tests.json',path/'result.tests.json')
                put(path,'result.summary.json',{'totalTestCount':len(expected)})
                put(path,'result.tests.err','')
                put(path,'result.summary.err','')
                count=len(expected)
                log='Executed '+str(count)+' tests\n'
                if phase=='red':
                    for name in value['expected_red_failed_ids']:
                        method=name.split('/')[1].removesuffix('()')
                        log+='/fixture/SessionPersistenceTests.swift:1: error: -[cmuxTests.SessionPersistenceTests '+method+'] : XCTAssertTrue failed\n'
                argv=['bash','/fixture/console-in-source.sh','/usr/bin/env','CMUX_XCODEBUILD_NONINTERACTIVE_RESTART_BUDGET=0','./scripts/ci/run-app-host-xcodebuild.sh']
                argv += ['-only-testing:'+s for s in value['phases'][phase]['selectors']]
                argv += ['test-without-building']
            put(path,'execution.log',log)
            put(path,'command-argv.txt','\n'.join(argv)+'\n')
            put(path,'executed-count.txt',str(count)+'\n')
            put(path,'exit-code.txt',('65' if phase=='red' else '0')+'\n')
            judged=c.validate_phase(ROOT,value,phase,path)
            put(path,'VERDICT.json',{**intent,**judged,'status':'EXPECTED_ASSERTION_RED' if phase=='red' else 'PASSED'})
        success=e._verify_content(root,expected_run=IDENTITY,expected_source=value['source'],expected_contract_sha256=contract_sha,success_required=False)
        put(root,'NATIVE-SUCCESS.json',success)
        seal(root)
        yield root,value,contract_sha

def verify(root,value,sha,identity=None):
    return e.verify(root,expected_run=identity or IDENTITY,expected_source=value['source'],expected_contract_sha256=sha)

class NativeContractTests(unittest.TestCase):
    def test_complete_same_attempt_all_seven_phases(self):
        with synthetic_artifact() as (root,value,sha):
            result=verify(root,value,sha)
            self.assertTrue(result['passed'])
            self.assertEqual(result['native_execution_counts'],dict(red=11,green=11,icon=11,regression=11,boundary=6,**{'existing-binding':4},package=6))
            self.assertFalse(result['icon_has_original_api_red_proof'])

    def test_missing_pending_tree_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            value=c.read(ROOT/'contract.json')
            value['status']='SOURCE_PINS_PENDING'
            put(root,'contract.json',value)
            with self.assertRaisesRegex(ValueError,'SOURCE_PINS_PENDING'):c.admit(root)

    def test_early_failure_exports_existing_logs_without_success(self):
        with tempfile.TemporaryDirectory(prefix='native-incomplete-export-') as temp:
            root=Path(temp).resolve();original=root/'original';original.mkdir();output=root/'small'
            put(original,'console-source-preflight.log','native setup failed before compilation\n')
            with patch.dict(os.environ,{'RUNNER_TEMP':str(root)}),patch.object(e.c,'run_identity',return_value=IDENTITY):
                result=e.export(ROOT,original,output)
            self.assertFalse(result['passed'])
            self.assertIn('COMPILE-red.json',result['missing'])
            self.assertEqual((output/'console-source-preflight.log').read_bytes(),(original/'console-source-preflight.log').read_bytes())
            self.assertTrue((output/'MANIFEST.json').is_file())
            self.assertTrue((output/'EXPORT-STATUS.json').is_file())
            self.assertFalse((output/'NATIVE-SUCCESS.json').exists())

    def test_foreign_attempt_and_head_rejected(self):
        with synthetic_artifact() as (root,value,sha):
            for changes in ({'run_attempt':2},{'head_sha':'b'*40}):
                with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'foreign source'):
                    verify(root,value,sha,{**IDENTITY,**changes})

    def test_boolean_run_attempt_cannot_equal_integer_one(self):
        with synthetic_artifact() as (root,value,sha):
            row=c.read(root/'icon/INTENT.json')
            row['run']['run_attempt']=True
            put(root,'icon/INTENT.json',row)
            seal(root)
            with self.assertRaisesRegex(ValueError,'foreign phase intent'):verify(root,value,sha)

    def test_tampered_member_rejected_before_result_interpretation(self):
        with synthetic_artifact() as (root,value,sha):
            put(root,'green/tests.json',{})
            with self.assertRaisesRegex(ValueError,'manifest/member'):verify(root,value,sha)

    def test_missing_skipped_or_unexpected_typed_case_never_passes(self):
        with synthetic_artifact() as (root,value,sha):
            path=root/'green'
            original=c.read(path/'tests.json')
            for kind in ('missing','skipped','unexpected'):
                data=copy.deepcopy(original)
                if kind=='missing':data['testNodes'].pop()
                elif kind=='skipped':data['testNodes'][0]['result']='Skipped'
                else:data['testNodes'].append({'nodeType':'Test Case','nodeIdentifier':'Other/test()','result':'Passed'})
                put(path,'tests.json',data)
                shutil.copyfile(path/'tests.json',path/'result.tests.json')
                seal(root)
                with self.subTest(kind=kind),self.assertRaises(ValueError):verify(root,value,sha)

    def test_compile_failure_is_not_a_behavioral_red(self):
        with synthetic_artifact() as (root,value,sha):
            row=c.read(root/'COMPILE-red.json')
            row['exit_code']=65
            put(root,'COMPILE-red.json',row)
            seal(root)
            with self.assertRaisesRegex(ValueError,'successful native test compilation'):verify(root,value,sha)

    def test_red_wrong_assertion_or_setup_exit_rejected(self):
        with synthetic_artifact() as (root,value,sha):
            put(root,'red/exit-code.txt','124\n')
            seal(root)
            with self.assertRaisesRegex(ValueError,'behavioral assertion'):verify(root,value,sha)
            put(root,'red/exit-code.txt','65\n')
            put(root,'red/execution.log','Executed 11 tests\nsetup failed\n')
            seal(root)
            with self.assertRaisesRegex(ValueError,'assertion evidence'):verify(root,value,sha)

    def test_green_command_must_equal_red(self):
        with synthetic_artifact() as (root,value,sha):
            with (root/'green/command-argv.txt').open('a') as f:f.write('DIFFERENT=1\n')
            seal(root)
            with self.assertRaisesRegex(ValueError,'red/green command'):verify(root,value,sha)

    def test_changed_commit_parent_is_rejected(self):
        with synthetic_artifact() as (root,value,sha):
            row=c.read(root/'SOURCE-green.json')
            row['source']['parent_commit']='9'*40
            put(root,'SOURCE-green.json',row)
            seal(root)
            with self.assertRaisesRegex(ValueError,'commit chain'):verify(root,value,sha)

    def test_small_evidence_symlink_refused(self):
        with synthetic_artifact() as (root,value,sha):
            (root/'alias').symlink_to(root/'contract.json')
            with self.assertRaisesRegex(ValueError,'symlink'):verify(root,value,sha)

    def test_artifact_metadata_binds_needs_digest_and_current_attempt(self):
        digest='e'*64
        run={**{'id':IDENTITY['run_id'],'run_attempt':1,'head_sha':IDENTITY['head_sha']},'repository':{'full_name':IDENTITY['repository']},'head_repository':{'full_name':IDENTITY['repository']}}
        artifact={'id':44,'name':'cmux-natural-native-12345-1','size_in_bytes':100,'digest':'sha256:'+digest,'expired':False,'workflow_run':{'id':12345,'head_sha':IDENTITY['head_sha']}}
        self.assertEqual(d.validate_metadata(run,artifact,IDENTITY,44,digest),100)
        for patch_row in ({'digest':'sha256:'+'f'*64},{'expired':True},{'size_in_bytes':d.LIMIT+1},{'name':'old-artifact'}):
            with self.subTest(patch=patch_row),self.assertRaises(ValueError):d.validate_metadata(run,{**artifact,**patch_row},IDENTITY,44,digest)
        with self.assertRaises(ValueError):d.validate_metadata({**run,'run_attempt':2},artifact,IDENTITY,44,digest)

    def test_zip_rejects_traversal_and_symlink(self):
        for name,mode in (('../escape',stat.S_IFREG|0o600),('link',stat.S_IFLNK|0o777)):
            data=io.BytesIO()
            with zipfile.ZipFile(data,'w') as bundle:
                row=zipfile.ZipInfo(name);row.external_attr=mode<<16;bundle.writestr(row,'x')
            data.seek(0)
            with zipfile.ZipFile(data) as bundle,self.assertRaises(ValueError):artifact_io.regular_archive(bundle,max_bytes=1024,max_files=10)

    def test_wrong_source_tree_stops_before_commit_and_preserves_attempt(self):
        with tempfile.TemporaryDirectory(prefix='native-source-fixture-') as temp:
            root=Path(temp).resolve();workspace=root/'workspace';checkout=workspace/'source';checkout.mkdir(parents=True)
            out=root/'evidence'
            calls=[]
            def fixture_git(_checkout,*args):
                return c.FIXED_SOURCE['upstream_commit'] if args==('rev-parse','HEAD') else ''
            def fixture_command(argv,**kwargs):
                calls.append(argv)
                return SimpleNamespace(returncode=0,stdout='0'*40 if argv[-1]=='write-tree' else '',stderr='')
            with patch.dict(os.environ,{'GITHUB_WORKSPACE':str(workspace),'RUNNER_TEMP':str(root)}),patch.object(source_chain.c,'run_identity',return_value=IDENTITY),patch.object(source_chain.c,'git',side_effect=fixture_git),patch.object(source_chain.subprocess,'run',side_effect=fixture_command):
                with self.assertRaisesRegex(ValueError,'source tree differs'):source_chain.advance('base',ROOT,checkout,out)
                count=len(calls)
                with self.assertRaisesRegex(ValueError,'no replay'):source_chain.advance('base',ROOT,checkout,out)
            self.assertEqual(len(calls),count)
            self.assertFalse(any('commit' in args for args in calls))
            self.assertTrue((out/'SOURCE-base-INTENT.json').is_file())
            self.assertTrue((out/'SOURCE-base-COMMANDS.json').is_file())
            self.assertFalse((out/'SOURCE-base.json').exists())

    def test_fetch_timeout_is_unknown_preserved_and_not_retried(self):
        with tempfile.TemporaryDirectory(prefix='native-fetch-fixture-') as temp:
            root=Path(temp)
            with patch.object(artifact_io.c,'run_identity',return_value=IDENTITY),patch.object(artifact_io.subprocess,'run',side_effect=subprocess.TimeoutExpired(['gh','api'],1)) as runner:
                with self.assertRaises(subprocess.TimeoutExpired):artifact_io.fetch(root,'fixture','repos/lzhs1995/ccc/actions/artifacts/1',maximum=1024,timeout=1)
            self.assertEqual(runner.call_count,1)
            self.assertEqual(c.read(root/'fixture/RESULT.json')['status'],'UNKNOWN')
            self.assertEqual(c.read(root/'fixture/INTENT.json')['retries'],0)

    def test_focused_shell_builds_one_invocation_with_eleven_separate_selectors(self):
        with tempfile.TemporaryDirectory(prefix='native-shell-fixture-') as tmp:
            root=Path(tmp).resolve();prep=root/'prep';prep.mkdir();tools=root/'tools';tools.mkdir()
            (root/'scripts/ci').mkdir(parents=True);evidence=root/'evidence';evidence.mkdir();derived=root/'derived';derived.mkdir()
            for name in ('run-focused.sh','console-in-source.sh'):
                shutil.copyfile(ROOT/name,prep/name)
            # The fixture changes only source/OS admission, while selectors and
            # typed judgement execute the actual contract against actual data.
            proxy=f'''import sys\nfrom pathlib import Path\nsys.path.insert(0,{str(ROOT)!r})\nimport contract as c\noperation,directory,*args=sys.argv[1:]\nif operation=="selectors":print(",".join(c.admit(Path({str(ROOT)!r}))["phases"][args[0]]["selectors"]))\nelif operation=="phase-intent":c.save(Path(args[2])/"INTENT.json",{{"fixture":True}})\nelif operation=="verdict":c.validate_native(Path({str(ROOT)!r}),c.admit(Path({str(ROOT)!r})),args[0],Path(args[2]))\nelse:raise SystemExit(90)\n'''
            (prep/'contract.py').write_text(proxy)
            def executable(path,text):path.write_text(text);path.chmod(0o700)
            executable(root/'scripts/ci/run-in-console-session.sh','#!/bin/bash\nexec "$@"\n')
            executable(root/'scripts/ci/run-app-host-xcodebuild.sh',f'''#!{sys.executable}
import sys,json
from pathlib import Path
args=sys.argv[1:];out=Path(args[args.index('-resultBundlePath')+1]);out.mkdir();(out/'Info.plist').write_text('fixture')
Path({str(root/'calls.json')!r}).write_text(json.dumps(args))
print('Executed 11 tests')
''')
            executable(root/'scripts/ci/require_selected_test_execution.sh','#!/bin/bash\necho 11\n')
            executable(root/'scripts/ci/run_with_timeout.py','import subprocess,sys\nraise SystemExit(subprocess.call(sys.argv[sys.argv.index("--")+1:]))\n')
            (root/'scripts/ci/focused_test_selectors.py').write_text('raise SystemExit(0)\n')
            value=c.admit(ROOT);typed={'testNodes':[{'nodeType':'Test Case','nodeIdentifier':name,'result':'Passed'} for name in value['phases']['icon']['required_test_ids']]}
            executable(tools/'xcrun',f'#!{sys.executable}\nprint({json.dumps(typed)!r})\n')
            executable(tools/'python3',f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
            env={**os.environ,'PATH':str(tools)+':/usr/bin:/bin','RUNNER_ENVIRONMENT':'github-hosted','NESTED_PHASE':'icon','NESTED_PREPARATION':str(prep),'NESTED_EVIDENCE':str(evidence),'NESTED_DERIVED':str(derived),'CMUX_DERIVED_DATA_PATH':str(derived),'CMUX_CI_RUNTIME_SOURCE_ROOT':str(root)}
            run=subprocess.run(['/bin/bash',str(prep/'run-focused.sh')],cwd=root,env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            argv=json.loads((root/'calls.json').read_text())
            self.assertEqual([s for s in argv if s.startswith('-only-testing:')],['-only-testing:'+s for s in value['phases']['icon']['selectors']])
            self.assertEqual(argv.count('test-without-building'),1)

class NativeEvidenceEnvelopeTests(unittest.TestCase):
    def test_both_full_compile_logs_export_and_download_losslessly(self):
        # Match the sizes from the failed native run using synthetic sparse bytes.
        # No original logs or session content are embedded in this fixture.
        with synthetic_artifact() as (root,value,sha):
            for stage,size in (('red',45921893),('green',35659599)):
                receipt=c.read(root/('COMPILE-'+stage+'.json'))
                log=root/receipt['log']['path']
                with log.open('wb') as stream:
                    stream.truncate(size)
                receipt['log']=c.pin(log,log.name)
                put(root,'COMPILE-'+stage+'.json',receipt)
            for stage in ('base','red','green'):
                put(root,'SOURCE-'+stage+'-INTENT.json',{'fixture':True})
                put(root,'SOURCE-'+stage+'-COMMANDS.json',[])
            with tempfile.TemporaryDirectory(prefix='native-envelope-roundtrip-') as temp:
                runner=Path(temp).resolve();output=runner/'export'
                with patch.dict(os.environ,{'RUNNER_TEMP':str(runner)}),patch.object(e.c,'run_identity',return_value=IDENTITY):
                    result=e.export(ROOT,root,output)
                self.assertTrue(result['passed'])
                manifest=c.read(output/'MANIFEST.json')['files']
                self.assertGreater(sum(row['bytes'] for row in manifest.values()),64*1024**2)
                for stage in ('red','green'):
                    row=c.read(root/('COMPILE-'+stage+'.json'))['log']
                    self.assertEqual(c.pin(output/row['path'],row['path']),row)
                archive=runner/'original.zip'
                with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
                    for path in output.rglob('*'):
                        if path.is_file():bundle.write(path,path.relative_to(output))
                self.assertLess(archive.stat().st_size,64*1024**2)
                run={**{'id':IDENTITY['run_id'],'run_attempt':1,'head_sha':IDENTITY['head_sha']},'repository':{'full_name':IDENTITY['repository']},'head_repository':{'full_name':IDENTITY['repository']}}
                digest=c.digest(archive)
                artifact={'id':44,'name':'cmux-natural-native-12345-1','size_in_bytes':archive.stat().st_size,'digest':'sha256:'+digest,'expired':False,'workflow_run':{'id':12345,'head_sha':IDENTITY['head_sha']}}
                put(runner,'run.json',run);put(runner,'artifact.json',artifact)
                responses=[runner/'run.json',runner/'artifact.json',archive]
                with patch.dict(os.environ,{'RUNNER_TEMP':str(runner)}),patch.object(d.c,'run_identity',return_value=IDENTITY),patch.object(d.io,'fetch',side_effect=responses) as fetch:
                    downloaded=d.download(44,digest,runner/'downloaded')
                self.assertTrue(downloaded['passed'])
                self.assertEqual(fetch.call_count,3)
                self.assertEqual(fetch.call_args_list[-1].kwargs['maximum'],64*1024**2)
                self.assertTrue(verify(runner/'downloaded',value,sha)['passed'])

    def test_expanded_archive_still_has_a_finite_boundary(self):
        row=zipfile.ZipInfo('compile.log');row.file_size=128*1024**2+1
        archive=SimpleNamespace(infolist=lambda:[row])
        with self.assertRaisesRegex(ValueError,'expansion/member budget'):
            artifact_io.regular_archive(archive,max_bytes=e.LIMIT,max_files=512)

if __name__=='__main__':unittest.main(verbosity=2)
