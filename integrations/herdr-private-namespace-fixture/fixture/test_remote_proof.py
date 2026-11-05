"""Offline negative controls only; no Herdr/agent/real compiler is started."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('remote_proof', HERE/'remote_proof.py')
p = importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class ProofGates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.contract = {'run_id':123,'run_attempt':1,'head_sha':'a'*40}
        self.run = {'id':123,'run_attempt':1,'head_sha':'a'*40,'head_branch':p.BRANCH,
                    'repository':{'full_name':p.REPOSITORY},'head_repository':{'full_name':p.REPOSITORY},
                    'path':p.WORKFLOW,'status':'completed','conclusion':'success','event':'push'}
        self.jobs = {'jobs':[{'name':'candidate','conclusion':'success','head_sha':'a'*40,
                             'steps':[{'name':p.BUILD_STEP,'conclusion':'success'}]}]}

    def test_exact_success_run_accepts(self):
        p.validate_run(self.run,self.jobs,self.contract)

    def test_failed_in_progress_other_head_attempt_or_repo_rejected(self):
        for key,value in [('conclusion','failure'),('status','in_progress'),('head_sha','b'*40),
                          ('run_attempt',2),('path','other.yml'),('repository',{'full_name':'other/repo'})]:
            run=copy.deepcopy(self.run);run[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):p.validate_run(run,self.jobs,self.contract)

    def test_job_without_actual_successful_build_rejected(self):
        for jobs in [{'jobs':[]},{'jobs':self.jobs['jobs']*2},copy.deepcopy(self.jobs)]:
            if len(jobs['jobs'])==1:jobs['jobs'][0]['steps'][0]['conclusion']='skipped'
            with self.assertRaises(ValueError):p.validate_run(self.run,jobs,self.contract)

    def make_zip(self):
        z=self.root/'evidence.zip'
        with zipfile.ZipFile(z,'w') as a:a.writestr('one.json','{}')
        artifact={'name':'expected','expired':False,'size_in_bytes':z.stat().st_size,
                  'workflow_run':{'id':123,'head_sha':'a'*40,'head_branch':p.BRANCH},
                  'digest':'sha256:'+p.sha(z)}
        return z,artifact

    def test_exact_github_zip_digest_accepts(self):
        z,a=self.make_zip();self.assertEqual(p.validate_zip(z,a,self.run,'expected'),{'one.json':b'{}'})

    def test_digest_origin_and_expiry_rejected(self):
        z,good=self.make_zip()
        for key,value in [('digest','sha256:'+'0'*64),('expired',True),('size_in_bytes',1),
                          ('workflow_run',{'id':9,'head_sha':'a'*40,'head_branch':p.BRANCH})]:
            a=copy.deepcopy(good);a[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):p.validate_zip(z,a,self.run,'expected')

    def test_path_and_duplicate_zip_entries_rejected(self):
        for names in [['../outside'],['same','same']]:
            z=self.root/'bad.zip'
            with zipfile.ZipFile(z,'w') as a:
                for name in names:a.writestr(name,'{}')
            artifact={'name':'expected','expired':False,'size_in_bytes':z.stat().st_size,
                      'workflow_run':{'id':123,'head_sha':'a'*40,'head_branch':p.BRANCH},'digest':'sha256:'+p.sha(z)}
            with self.assertRaises(ValueError):p.validate_zip(z,artifact,self.run,'expected')

    def compiled(self,unit):
        artifact={'reason':'compiler-artifact','target':{'name':'herdr','kind':['bin']},
                  'profile':{'test':unit},'executable':'/runner/herdr'}
        command=['cargo','+1.99.0','rustc','--locked','--offline','-p','herdr','--bin','herdr',
                 '--message-format=json','--verbose',*(['--profile','test'] if unit else []),
                 '--','-C','debug-assertions=off']
        running='Running `rustc --crate-name herdr '+('--test ' if unit else '')+'-C debug-assertions=off src/main.rs`'
        value={'command':command,'sha256':'b'*64,'root_crate_debug_assertions':False,'nonoptimized':True,
               'bytes':5,'artifact':artifact,'path':'/runner/frozen-unit','actual_rustc_command':running}
        label='unit' if unit else 'server'
        files={label+'-compile.json':json.dumps(value).encode(),label+'-build.log':
               (running+'\n'+json.dumps(artifact)+'\n'+json.dumps({'reason':'build-finished','success':True})).encode()}
        return files

    def test_actual_same_cfg_rustc_artifact_accepts_both_targets(self):
        for unit in [False,True]:p.validate_compile(self.compiled(unit),'unit' if unit else 'server','b'*64)

    def test_receipt_flag_without_real_rustc_flag_rejected(self):
        files=self.compiled(False);files['server-build.log']=files['server-build.log'].replace(b'debug-assertions=off',b'debug-assertions=on')
        with self.assertRaises(ValueError):p.validate_compile(files,'server','b'*64)

    def test_wrong_cfg_target_or_build_failure_rejected(self):
        for old,new in [(b'"success": true',b'"success": false'),(b'--crate-name herdr ',b'--crate-name other ')]:
            files=self.compiled(True);files['unit-build.log']=files['unit-build.log'].replace(old,new)
            with self.assertRaises(ValueError):p.validate_compile(files,'unit','b'*64)

    def cases_fixture(self):
        expected=p.read(p.BUNDLE/'EXPECTED-CASES.json')
        unit={'path':'/runner/frozen-unit','sha256':'b'*64}
        listing={'rust-suites':{'herdr::bin/herdr':{'binary-path':unit['path'],'testcases':{
            **{x:{'ignored':False} for x in expected['nonignored']},
            **{x:{'ignored':True} for x in expected['ignored']}}}}}
        xml=p.ET.Element('testsuites');suite=p.ET.SubElement(xml,'testsuite')
        for name in expected['nonignored']:
            p.ET.SubElement(suite,'testcase',{'name':name,'classname':'herdr::bin/herdr'})
        raw=p.ET.tostring(xml)
        result={'passed':True,'successful_cases':3656,'ignored_not_run':11,'unit_binary_sha256':unit['sha256'],
                'junit_sha256':hashlib.sha256(raw).hexdigest(),'cargo_build_invoked_by_nextest':False}
        files={'LIST.json':json.dumps(listing).encode(),'unit.xml':raw,'UNIT-RESULT.json':json.dumps(result).encode(),
               'unit-compile.json':b'{}','cargo-metadata.json':b'{}',
               'binaries-metadata.json':json.dumps({'rust-binaries':{'herdr::bin/herdr':{'binary-path':unit['path']}}}).encode()}
        provenance={key:hashlib.sha256(files[name]).hexdigest() for name,key in [
            ('unit-compile.json','unit_compile_sha256'),('cargo-metadata.json','cargo_metadata_sha256'),
            ('binaries-metadata.json','binary_metadata_sha256')]}
        files['METADATA-PROVENANCE.json']=json.dumps(provenance).encode()
        return files,expected,unit

    def test_exact_real_frozen_case_names_gate_accepts_synthetic_successes(self):
        files,expected,unit=self.cases_fixture()
        self.assertEqual(p.validate_cases(files,expected,unit)['successful_cases'],3656)

    def test_junit_skipped_failed_missing_or_duplicate_rejected(self):
        original,expected,unit=self.cases_fixture()
        for mutation in ['skip','failure','missing','duplicate','wrong-binary']:
            files=copy.deepcopy(original);xml=p.ET.fromstring(files['unit.xml']);suite=xml.find('testsuite');case=suite[0]
            if mutation=='skip':p.ET.SubElement(case,'skipped')
            elif mutation=='failure':p.ET.SubElement(case,'failure')
            elif mutation=='missing':suite.remove(case)
            elif mutation=='duplicate':suite.append(copy.deepcopy(case))
            else:case.set('classname','other')
            files['unit.xml']=p.ET.tostring(xml)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):p.validate_cases(files,expected,unit)

    def test_list_wrong_unit_artifact_or_ignored_cfg_rejected(self):
        files,expected,unit=self.cases_fixture()
        for mutation in ['binary','ignored']:
            mutated=copy.deepcopy(files);listing=json.loads(mutated['LIST.json']);suite=listing['rust-suites']['herdr::bin/herdr']
            if mutation=='binary':suite['binary-path']='/other/unit'
            else:suite['testcases'][expected['ignored'][0]]['ignored']=False
            mutated['LIST.json']=json.dumps(listing).encode()
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):p.validate_cases(mutated,expected,unit)

    def source_fixture(self):
        expected = {'files': [{'path': 'Cargo.lock', 'kind': 'file', 'sha256': 'c' * 64,
                               'executable': False}], 'sha256': 'd' * 64}
        files = {}
        for phase in ('after-fetch', 'after-server', 'after-unit', 'after-list', 'after-tests'):
            files['SOURCE-' + phase + '.json'] = json.dumps(expected).encode()
            files['SOURCE-DIFF-' + phase + '.json'] = json.dumps({
                'phase': phase, 'expected_sha256': expected['sha256'],
                'actual_sha256': expected['sha256'], 'added': [], 'removed': [], 'changed': []}).encode()
        return files, expected

    def test_all_five_actual_source_checkpoints_required(self):
        files, expected = self.source_fixture()
        p.validate_source_checkpoints(files, expected)
        for name in files:
            missing = dict(files);del missing[name]
            with self.subTest(missing=name),self.assertRaises(ValueError):
                p.validate_source_checkpoints(missing, expected)

    def test_source_file_added_changed_removed_rejected_even_with_unchanged_claimed_digest(self):
        files, expected = self.source_fixture()
        for phase in ('after-fetch', 'after-server', 'after-unit', 'after-list', 'after-tests'):
            for mutation in ('added', 'removed', 'changed'):
                altered = copy.deepcopy(expected)
                if mutation == 'added':altered['files'].append({'path': 'target/nextest/cache'})
                elif mutation == 'removed':altered['files'].clear()
                else:altered['files'][0]['sha256'] = 'e' * 64
                bad = dict(files);bad['SOURCE-' + phase + '.json'] = json.dumps(altered).encode()
                with self.subTest(phase=phase,mutation=mutation),self.assertRaises(ValueError):
                    p.validate_source_checkpoints(bad, expected)

    def test_nonempty_mislabelled_or_wrong_digest_diff_rejected(self):
        files, expected = self.source_fixture()
        name = 'SOURCE-DIFF-after-tests.json'
        for key, value in [('phase', 'after-server'), ('actual_sha256', '0' * 64),
                           ('expected_sha256', '0' * 64), ('added', [{'path':'extra'}])]:
            bad = dict(files);difference = json.loads(bad[name]);difference[key] = value
            bad[name] = json.dumps(difference).encode()
            with self.subTest(key=key),self.assertRaises(ValueError):
                p.validate_source_checkpoints(bad, expected)

    def mirror_evidence_fixture(self):
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        encode = lambda value: (json.dumps(value, indent=2) + '\n').encode()
        expected = p.read(p.BUNDLE/'CANDIDATE-SOURCE.json')
        prefix = 'vendor/libghostty-vt/'
        rows = [{**r, 'path':r['path'][len(prefix):]} for r in expected['files'] if r['path'].startswith(prefix)]
        inputs = {'files':rows, 'sha256':hashlib.sha256(canonical(rows)).hexdigest()}
        files = {'ZIG-MIRROR-INPUTS.json':encode(inputs)}
        spec = {'schema':'herdr-zig-output-mirror-v1','original_vendor':'/runner/source/vendor/libghostty-vt',
                'mirror':'/runner/output/zig-vendor-inputs','inputs':'/runner/output/ZIG-MIRROR-INPUTS.json',
                'inputs_sha256':hashlib.sha256(files['ZIG-MIRROR-INPUTS.json']).hexdigest(),
                'target_dir':'/runner/output/target','real_zig':'/runner/output/zig/zig-aarch64-macos-0.16.0/zig',
                'real_zig_sha256':'e6cd688d25664983833aae272f501d4bceeae304875b8f1741209d15fd13a4ec',
                'ci_sha256':'a'*64,'ci_path':'/checkout/ci.py','wrapper':'/runner/output/zig-output-wrapper',
                'invocations':'/runner/output/zig-invocations.log','build_tail':['-Demit-lib-vt',
                '-Doptimize=ReleaseFast','-Dsimd=true','-Dtarget=aarch64-macos',
                '-Dversion-string=1.3.2-HEAD-+44f2a44df','-Demit-xcframework=false']}
        files['ZIG-MIRROR-SPEC.json']=encode(spec);spec_sha=hashlib.sha256(files['ZIG-MIRROR-SPEC.json']).hexdigest()
        wrapper=("#!/usr/bin/python3\nimport hashlib, pathlib, runpy, sys\n"
            +"ci_path = pathlib.Path("+repr(spec['ci_path'])+")\n"
            +"assert hashlib.sha256(ci_path.read_bytes()).hexdigest() == "+repr(spec['ci_sha256'])+"\n"
            +"runpy.run_path(str(ci_path))['zig_wrapper_main']("+repr('/runner/output/ZIG-MIRROR-SPEC.json')
            +", "+repr(spec_sha)+", sys.argv[1:])\n")
        prep={'wrapper_source':wrapper,'wrapper_sha256':hashlib.sha256(wrapper.encode()).hexdigest(),
              'spec_sha256':spec_sha,'inputs_sha256':spec['inputs_sha256'],'frozen_input_count':1339,
              'real_zig_sha256':spec['real_zig_sha256'],'system_package_mode':False}
        files['ZIG-MIRROR-PREPARATION.json']=encode(prep)
        for phase in ['after-server','after-unit','after-tests']:
            files['ZIG-MIRROR-'+phase+'.json']=encode(inputs)
            files['ZIG-PACKAGES-'+phase+'.json']=encode({'files':[],'sha256':hashlib.sha256(canonical([])).hexdigest()})
        event={'event':'exec_intent','argv':[spec['real_zig'],'build','--prefix',
            '/runner/output/target/debug/build/ghostty-vt/out/zig-out',*spec['build_tail']],
            'original_cwd':spec['original_vendor'],'execution_cwd':spec['mirror'],
            'spec_sha256':spec_sha,'wrapper_sha256':prep['wrapper_sha256'],'real_zig_sha256':spec['real_zig_sha256']}
        files['zig-invocations.log']=(json.dumps(event)+'\n').encode()
        return files,expected,{'artifact':{'manifest_path':'/runner/source/Cargo.toml'}},{'ci_sha256':'a'*64}

    def test_exact_mirror_complete_inputs_wrapper_and_real_zig_accept(self):
        p.validate_zig_mirror_evidence(*self.mirror_evidence_fixture())

    def test_mirror_changed_original_input_or_extra_outside_package_rejected(self):
        for mutation in ['input','extra']:
            files,expected,server,contract=self.mirror_evidence_fixture()
            name='ZIG-MIRROR-after-tests.json';value=json.loads(files[name])
            if mutation=='input':value['files'][0]['sha256']='0'*64
            else:value['files'].append({'path':'outside','kind':'file','sha256':'a'*64,'executable':False})
            value['sha256']=hashlib.sha256(json.dumps(value['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            files[name]=json.dumps(value).encode()
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                p.validate_zig_mirror_evidence(files,expected,server,contract)

    def test_mirror_wrong_cwd_system_flags_or_no_invocation_rejected(self):
        for mutation in ['cwd','system','missing']:
            files,expected,server,contract=self.mirror_evidence_fixture();event=json.loads(files['zig-invocations.log'])
            if mutation=='cwd':event['execution_cwd']=event['original_cwd']
            elif mutation=='system':event['argv']+=['--system','/tmp/pkg']
            files['zig-invocations.log']=b'' if mutation=='missing' else json.dumps(event).encode()
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                p.validate_zig_mirror_evidence(files,expected,server,contract)

    def test_mirror_missing_phase_or_real_zig_pin_rejected(self):
        for mutation in ['missing','zig']:
            files,expected,server,contract=self.mirror_evidence_fixture()
            if mutation=='missing':del files['ZIG-PACKAGES-after-unit.json']
            else:
                spec=json.loads(files['ZIG-MIRROR-SPEC.json']);spec['real_zig_sha256']='0'*64
                files['ZIG-MIRROR-SPEC.json']=json.dumps(spec).encode()
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                p.validate_zig_mirror_evidence(files,expected,server,contract)

    def test_original_execution_cleanup_and_tui_port_invariant(self):
        # The Actions port changes only CLI transport and runner USER/LOGNAME.
        # Validate against the original normalized AST pins, without publishing
        # the original fixture's private absolute local paths.
        spec=importlib.util.spec_from_file_location('test_port',HERE.parent/'test_port.py')
        module=importlib.util.module_from_spec(spec)
        import sys
        sys.path.insert(0,str(HERE.parent))
        spec.loader.exec_module(module)
        case=module.PortGates();case.setUp()
        case.test_private_tui_class_unchanged()
        case.test_runtime_and_cleanup_body_unchanged_except_command_transport_and_username()

    def test_syntax_only_without_pycache(self):
        for name in ['remote_proof.py','run_private_remote_namespace.py']:
            compile((HERE/name).read_text(),name,'exec')

if __name__=='__main__':unittest.main(verbosity=2)
