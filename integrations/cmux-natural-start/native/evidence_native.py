"""Export and independently recheck bounded native evidence.

Verification uses this published module and its pinned parser dependencies;
it never imports executable code from the downloaded artifact.
"""
from pathlib import Path
import argparse
import importlib.util
import json
import os
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cmux_natural_native_contract',ROOT/'contract.py')
c = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = c
spec.loader.exec_module(c)
# Two full app-host compile logs exceeded the former shared 64 MiB ceiling.
# Keep raw original bytes; transport separately limits the compressed ZIP.
LIMIT = 128 * 1024**2
COMMON = ('INTENT.json','VERDICT.json','execution.log','exit-code.txt','executed-count.txt','command-argv.txt')
NATIVE = ('tests.json','result.tests.json','result.summary.json','result.tests.err','result.summary.err')
LIFECYCLE = ('console-source-preflight.log','app-host-cleanup.log')

def _members(root):
    members = {}
    for path in sorted(root.rglob('*')):
        c.need(not path.is_symlink(), 'symlink in native evidence')
        if path.is_file() and path != root/'MANIFEST.json':
            relative = str(path.relative_to(root))
            members[relative] = c.pin(path,relative)
        elif not path.is_file():
            c.need(path.is_dir(), 'special native evidence member')
    c.need(len(members) <= 256 and sum(r['bytes'] for r in members.values()) <= LIMIT, 'native evidence exceeds 128 MiB/256 files')
    return members

def _source_chain(root, value, identity, contract_sha):
    rows = {}
    frozen = c.read(ROOT/'inputs/source-freeze.json')
    for stage in ('base','red','green'):
        row = c.read(root/('SOURCE-'+stage+'.json'))
        c.need(row['schema']=='cmux-natural-native-source-v1' and c.same(row['run'],identity) and row['contract_sha256']==contract_sha and c.same(row['fixed_source'],value['source']), 'foreign source receipt')
        snap = row['source']
        expected_tree = value['source']['base_green_tree' if stage=='base' else stage+'_tree']
        c.need(snap['stage']==stage and snap['tree']==expected_tree, 'wrong source phase/tree')
        c.need(all(re.fullmatch('[0-9a-f]{40}',snap.get(k,'')) for k in ('commit','parent_commit')), 'missing actual source commit')
        expected_files = []
        for name, pin in frozen['files'].items():
            prefix = 'candidate' if stage=='green' or (stage=='red' and name=='cmuxTests/SessionPersistenceTests.swift') else 'original'
            expected_files.append({'path':name,'bytes':pin[prefix+'_bytes'],'sha256':pin[prefix+'_sha256']})
        c.need(c.same(snap['files'],expected_files), 'source member receipt differs')
        rows[stage]=snap
    c.need(rows['base']['parent_commit']==value['source']['upstream_commit'], 'accepted base parent differs')
    c.need(rows['red']['parent_commit']==rows['base']['commit'] and rows['green']['parent_commit']==rows['red']['commit'], 'source commit chain differs')
    c.need(len({rows[s]['commit'] for s in rows})==3, 'source commits are not distinct')
    return rows

def _verify_content(root, *, expected_run, expected_source, expected_contract_sha256, success_required=True):
    value = c.admit(ROOT)
    c.need(c.digest(ROOT/'contract.json')==expected_contract_sha256, 'published native contract differs')
    c.need(c.digest(root/'contract.json')==expected_contract_sha256, 'artifact native contract differs')
    c.need(c.same(value['source'],expected_source), 'expected current source differs')
    identity = expected_run
    c.need(set(identity)=={'repository','head_sha','run_id','run_attempt'} and identity['repository']=='lzhs1995/ccc' and re.fullmatch('[0-9a-f]{40}',identity['head_sha']), 'invalid expected run')
    c.need(type(identity['run_id']) is int and type(identity['run_attempt']) is int and identity['run_id']>0 and identity['run_attempt']>0, 'invalid expected run/attempt')
    preflight=(root/LIFECYCLE[0]).read_text()
    c.need(all(marker in preflight for marker in ('NESTED_APP_HOST_ENV_OK=','RESTART_BUDGET=0','ATTEMPTS=1')), 'successful console preflight evidence missing')
    c.pin(root/LIFECYCLE[1])
    for relative,wanted in value['input_pins'].items():
        if relative.startswith('inputs/'):
            observed = c.pin(c.contained(root,relative),relative)
            c.need(observed['sha256']==wanted['sha256'] and observed['bytes']==wanted['bytes'], 'exported input differs: '+relative)
    sources = _source_chain(root,value,identity,expected_contract_sha256)
    for phase in ('red','green'):
        row = c.read(root/('COMPILE-'+phase+'.json'))
        c.need(row['schema']=='cmux-natural-native-compile-v1' and row['phase']==phase and c.same(row['run'],identity) and c.same(row['source'],sources[phase]) and row['contract_sha256']==expected_contract_sha256, 'foreign test compilation')
        c.need(type(row['exit_code']) is int and row['exit_code']==0, 'missing successful native test compilation')
        path = c.contained(root,row['log']['path'])
        c.need(c.same(c.pin(path,row['log']['path']),row['log']), 'compile log changed')
    phase_pins = {}
    for phase in c.PHASES:
        path = root/phase
        source = sources['red' if phase=='red' else 'green']
        expected_intent = {'schema':'cmux-natural-native-phase-v1','phase':phase,'run':identity,'source':source,'contract_sha256':expected_contract_sha256}
        c.need(c.same(c.read(path/'INTENT.json'),expected_intent), 'foreign phase intent: '+phase)
        if phase!='package':
            c.need((path/'tests.json').read_bytes()==(path/'result.tests.json').read_bytes(), 'typed export copies differ')
            c.read(path/'result.summary.json')
        judged = c.validate_phase(ROOT,value,phase,path)
        c.need(int((path/'executed-count.txt').read_text())==judged['executed_test_count'], 'aggregate/typed execution counts differ')
        expected_verdict = {**expected_intent,**judged,'status':'EXPECTED_ASSERTION_RED' if phase=='red' else 'PASSED'}
        c.need(c.same(c.read(path/'VERDICT.json'),expected_verdict), 'typed verdict differs from original evidence: '+phase)
        argv = (path/'command-argv.txt').read_text().splitlines()
        if phase!='package':
            actual = [s.removeprefix('-only-testing:') for s in argv if s.startswith('-only-testing:')]
            c.need(actual==value['phases'][phase]['selectors'], 'executed command selectors differ: '+phase)
            c.need(argv.count('test-without-building')==1 and argv.count('CMUX_XCODEBUILD_NONINTERACTIVE_RESTART_BUDGET=0')==1, 'test invocation/budget differs')
        else:
            c.need(argv.count('TerminalSurfaceStartupRestorePolicyTests')==2 and 'swift' in argv and 'test' in argv, 'package invocation differs')
        phase_pins[phase]=c.pin(path/'VERDICT.json',phase+'/VERDICT.json')
    c.need((root/'red/command-argv.txt').read_bytes()==(root/'green/command-argv.txt').read_bytes(), 'red/green command differs')
    source = {**expected_source, **{stage+'_commit':sources[stage]['commit'] for stage in sources}, **{stage+'_parent_commit':sources[stage]['parent_commit'] for stage in sources}}
    success = {'schema':'cmux-natural-start-native-success-v1','status':'NATIVE_RED_GREEN_AND_ADJACENT_PASSED','run':identity,'source':source,'contract_sha256':expected_contract_sha256,'phases':phase_pins,'native_execution_counts':{'red':11,'green':11,'icon':11,'regression':11,'boundary':6,'existing-binding':4,'package':6},'icon_has_original_api_red_proof':False,'release_build_executed':False}
    if success_required:
        c.need(c.same(c.read(root/'NATIVE-SUCCESS.json'),success), 'native success receipt differs')
    return success

def verify(root: Path, *, expected_run: dict, expected_source: dict, expected_contract_sha256: str) -> dict:
    root = Path(root)
    c.need(root.is_dir() and not root.is_symlink(), 'native evidence directory required')
    manifest = c.read(root/'MANIFEST.json')
    c.need(manifest.get('schema')=='cmux-natural-native-evidence-manifest-v1', 'native manifest schema differs')
    observed = _members(root)
    c.need(c.same(manifest.get('files'),observed), 'native manifest/member mismatch')
    success = _verify_content(root,expected_run=expected_run,expected_source=expected_source,expected_contract_sha256=expected_contract_sha256)
    return {**success,'passed':True,'manifest_sha256':c.digest(root/'MANIFEST.json'),'success_sha256':c.digest(root/'NATIVE-SUCCESS.json')}

def export(preparation, evidence, output):
    c.need(preparation.resolve()==ROOT, 'export must use the published preparation directory')
    identity = c.run_identity()
    output = output.absolute()
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    c.need(output.parent.resolve(strict=True).is_relative_to(temporary) and not output.exists() and not output.is_symlink(), 'fresh owned runner export required')
    output.mkdir(mode=0o700)
    errors=[]
    missing=[]
    try:
        value=c.admit(ROOT)
    except Exception as exc:
        value=None
        errors.append({'stage':'contract-admission','error':str(exc)})
    # Trusted preparation inputs remain useful when source admission failed.
    # Do not require a compile/phase receipt merely to export its absence.
    wanted=['contract.json']+[str(p.relative_to(ROOT)) for p in sorted((ROOT/'inputs').iterdir()) if p.is_file()]
    source_files = ['SOURCE-'+stage+'.json' for stage in ('base','red','green')]
    source_files += ['SOURCE-'+stage+'-INTENT.json' for stage in ('base','red','green')]
    source_files += ['SOURCE-'+stage+'-COMMANDS.json' for stage in ('base','red','green')]
    source_files += ['COMPILE-red.json','COMPILE-green.json']
    source_files += list(LIFECYCLE)
    source_files += [p.name for p in sorted(evidence.glob('build-*.log'))]
    for stage in ('red','green'):
        path=evidence/('COMPILE-'+stage+'.json')
        if path.exists():
            try:
                name=c.read(path)['log']['path']
                c.need(Path(name).name==name and name.startswith('build-') and name.endswith('.log'),'unsafe compile log path')
                source_files.append(name)
            except Exception as exc:
                errors.append({'stage':'compile-log-discovery','file':path.name,'error':str(exc)})
    source_files += [phase+'/'+name for phase in c.PHASES for name in (*COMMON, *(('wrapper.log',) if phase=='package' else NATIVE))]
    inputs = [(ROOT/name,name) for name in wanted]+[(evidence/name,name) for name in dict.fromkeys(source_files)]
    c.need(len({name for _,name in inputs})==len(inputs), 'duplicate export path')
    c.need(len(inputs)<=240,'native export member budget exceeded')
    copied_bytes=0
    for path,name in inputs:
        if not path.exists():
            missing.append(name)
            continue
        try:
            before=c.pin(path,name)
            c.need(copied_bytes+before['bytes']<=LIMIT-256*1024,'original file exceeds remaining small envelope; retained in original evidence')
            target=output/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
            copied_bytes+=target.stat().st_size
            c.need(c.same(before,c.pin(path,name)) and c.same(before,c.pin(target,name)),'source changed during export')
        except Exception as exc:
            errors.append({'stage':'copy-original','file':name,'error':str(exc)})
    success=None
    if value is not None and not missing and not errors:
        try:
            success=_verify_content(output,expected_run=identity,expected_source=value['source'],expected_contract_sha256=c.digest(ROOT/'contract.json'),success_required=False)
        except Exception as exc:
            errors.append({'stage':'native-verification','error':str(exc)})
    status={'schema':'cmux-natural-native-export-status-v1','run':identity,'passed':success is not None,'status':'COMPLETE' if success is not None else 'INCOMPLETE_OR_FAILED_NO_NATIVE_SUCCESS','missing':missing,'errors':errors,'raw_xcresult_bundles_exported':False,'copied_bytes':copied_bytes}
    c.save(output/'EXPORT-STATUS.json',status)
    if success is not None:
        c.save(output/'NATIVE-SUCCESS.json',success)
    c.save(output/'MANIFEST.json',{'schema':'cmux-natural-native-evidence-manifest-v1','files':_members(output)})
    if success is None:
        return status
    return verify(output,expected_run=identity,expected_source=value['source'],expected_contract_sha256=c.digest(ROOT/'contract.json'))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='operation',required=True)
    p=sub.add_parser('export')
    p.add_argument('--preparation',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('verify')
    p.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    if args.operation=='export': result=export(args.preparation,args.evidence,args.output)
    else: result=verify(args.evidence,expected_run=c.run_identity(),expected_source=c.admit(ROOT)['source'],expected_contract_sha256=c.digest(ROOT/'contract.json'))
    print(json.dumps(result,indent=2))
    if not result.get('passed'):
        raise SystemExit(1)

if __name__=='__main__': main()
