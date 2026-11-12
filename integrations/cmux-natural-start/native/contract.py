"""Current native source admission and typed behavioral verdicts.

The reviewed r10 console/build/result recipe is reused. No old test receipt
can stand in for a test on the current green tree.
"""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PHASES = ('red', 'green', 'icon', 'regression', 'boundary', 'existing-binding', 'package')
FIXED_SOURCE = {
    'upstream_commit': '2e109a1739d320fe36220afb3e7775b59c1acd47',
    'base_green_tree': '40e8ea03ce1435f11e90b35f98d554a13b9b8685',
}
FIXED_INPUTS = {
    'inputs/original-tests.patch':'bdebf64e054476f3ea0476df3ec7acb91ddbfe86fc2927bb85686720c6a7bf8e',
    'inputs/original-fix.patch':'db4f2c96ff9e06fd47ddb55a102f0ccb45d00c691e9e3681ca722f3f97144422',
    'inputs/source-freeze.json':'40e64aa4d69d465cd0139dc2f39ecac7921b03124c24bc9e361615097b6df598',
    'inputs/source-correction.json':'8799f239727e966704b58e87758b266003ca2c9f4db21f0b71773237ed242ebd',
    'inputs/test-plan.json':'5d881d6a31cac56659a683e655b464cfe3af07cfbd282add513484e29e5b6f82',
    'inputs/restore-tests.patch':'a36c6c5b14c515b4e10b46dc642639aa7416968b79448f7579d5f49f4d9d06b1',
    'inputs/full-fix.patch':'40092c6b45f0c773e241035eba032b20a9d16b9e1574bc53f40d4c9afc24250b',
    'inputs/expected-trees.json':'d112d09cab944d6a5a078bac9fe7d69ec1f2fbeddb1e9c99d1acc846c44e6f37',
}

def need(ok, message):
    if not ok:
        raise ValueError(message)

def same(left, right):
    """JSON type-strict equality; bool must never authenticate an integer ID."""
    return json.dumps(left,sort_keys=True,separators=(',',':'),allow_nan=False) == json.dumps(right,sort_keys=True,separators=(',',':'),allow_nan=False)

def read(path):
    def pairs(rows):
        obj = {}
        for key, value in rows:
            need(key not in obj, 'duplicate JSON key: '+key)
            obj[key] = value
        return obj
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs)

def save(path, obj):
    with Path(path).open('x') as stream:
        stream.write(json.dumps(obj, ensure_ascii=False, indent=2)+'\n')

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def pin(path, relative=None):
    path = Path(path)
    need(path.is_file() and not path.is_symlink(), 'regular evidence required: '+str(path))
    return {'path': relative or path.name, 'bytes':path.stat().st_size, 'sha256':digest(path)}

def contained(root, relative):
    path = root / relative
    need(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'unsafe evidence path')
    need(path.resolve(strict=True).is_relative_to(root.resolve(strict=True)) and not path.is_symlink(), 'escaped evidence path')
    return path

def run_identity():
    need(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and os.environ.get('GITHUB_ACTIONS') == 'true', 'disposable GitHub runner required')
    value = {key:os.environ.get(env) for key,env in [('repository','GITHUB_REPOSITORY'),('head_sha','GITHUB_SHA'),('run_id','GITHUB_RUN_ID'),('run_attempt','GITHUB_RUN_ATTEMPT')]}
    need(value['repository'] == 'lzhs1995/ccc' and re.fullmatch('[0-9a-f]{40}', value['head_sha'] or ''), 'original repository/preparation head missing')
    need(str(value['run_id']).isdigit() and str(value['run_attempt']).isdigit(), 'run/attempt missing')
    value.update(run_id=int(value['run_id']),run_attempt=int(value['run_attempt']))
    need(value['run_id'] > 0 and value['run_attempt'] > 0, 'invalid run/attempt')
    return value

def admit(root=ROOT):
    root = Path(root)
    value = read(root/'contract.json')
    need(value.get('schema') == 'cmux-natural-start-native-contract-v1', 'wrong native contract schema')
    need(value.get('status') == 'FROZEN_READY_FOR_NATIVE_EXECUTION', 'SOURCE_PINS_PENDING: current source trees are not frozen')
    source = value['source']
    for key,wanted in FIXED_SOURCE.items():
        need(source.get(key) == wanted, 'original base changed: '+key)
    for key in ('red_tree','green_tree'):
        need(re.fullmatch('[0-9a-f]{40}', source.get(key) or ''), 'SOURCE_PINS_PENDING: '+key)
    need(len({source['base_green_tree'],source['red_tree'],source['green_tree']}) == 3, 'source phases must differ')
    for relative,wanted in FIXED_INPUTS.items():
        need(value['input_pins'].get(relative,{}).get('sha256') == wanted, 'reviewed input pin differs: '+relative)
    for relative, wanted in value['input_pins'].items():
        observed = pin(contained(root,relative),relative)
        need(observed['sha256'] == wanted['sha256'] and observed['bytes'] == wanted['bytes'], 'input changed: '+relative)
    old = read(root/'inputs/original-contract.json')
    trees = read(root/'inputs/expected-trees.json')
    need(source['red_tree']==trees['new_red_tree'] and source['green_tree']==trees['new_green_tree'], 'source differs from independent tree reconstruction')
    plan = read(root/'inputs/test-plan.json')
    for phase, plan_phase in [('red','red'),('green','green_restore'),('icon','green_icon')]:
        p = value['phases'][phase]
        need(p['selectors'] == plan['phases'][plan_phase]['selectors'] and len(p['selectors']) == 11, 'current selectors changed: '+phase)
        need(p['required_test_ids'] == [s.removeprefix('cmuxTests/')+'()' for s in p['selectors']], 'typed identifiers changed')
    need(value['phases']['red']['selectors'] == value['phases']['green']['selectors'], 'red/green selectors differ')
    for phase,key in [('regression','required_green_test_ids'),('boundary','boundary_test_ids'),('existing-binding','existing_binding_test_ids')]:
        need(value['phases'][phase]['required_test_ids'] == old[key], 'adjacent inventory changed: '+phase)
    need(value['package_test_display_names'] == old['package_test_display_names'], 'package inventory changed')
    expected = [s.removeprefix('cmuxTests/')+'()' for s in plan['phases']['red']['expected_failed_selectors']]
    need(value['expected_red_failed_ids'] == expected and len(expected) == 2, 'red failure contract changed')
    need(value['expected_red_assertion'] == 'XCTAssertTrue failed' and value['expected_red_assertions_per_failed_method'] == 1, 'red assertion contract changed')
    for phase,p in value['phases'].items():
        need(len(p['selectors']) == len(set(p['selectors'])) and len(p['required_test_ids']) == len(set(p['required_test_ids'])), 'duplicate selectors/test IDs')
        need(all(re.fullmatch(r'cmuxTests/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+(?:\(\))?)?', s) for s in p['selectors']), 'invalid exact selector')
    return value

def module(root, name):
    path = root/'upstream'/name
    spec = importlib.util.spec_from_file_location('natural_'+path.stem,path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded

def git(checkout, *args):
    p = subprocess.run(['git', '-C', str(checkout), *args],capture_output=True,text=True,timeout=30)
    need(p.returncode == 0, 'git source check failed: '+p.stderr)
    return p.stdout.strip()

def source_snapshot(root, checkout, stage):
    value = admit(root)
    expected_tree = value['source']['base_green_tree' if stage=='base' else stage+'_tree']
    commit = git(checkout,'rev-parse','HEAD')
    tree = git(checkout,'rev-parse','HEAD^{tree}')
    need(tree == expected_tree and git(checkout,'write-tree') == tree, 'source tree differs: '+stage)
    need(not git(checkout,'diff','--name-only') and not git(checkout,'diff','--cached','--name-only'), 'tracked source changed')
    frozen = read(root/'inputs/source-freeze.json')
    rows = []
    for relative,row in frozen['files'].items():
        prefix = 'candidate' if stage=='green' or (stage=='red' and relative=='cmuxTests/SessionPersistenceTests.swift') else 'original'
        observed = pin(contained(checkout,relative),relative)
        need(observed['sha256'] == row[prefix+'_sha256'] and observed['bytes'] == row[prefix+'_bytes'], 'phase source file differs: '+relative)
        rows.append(observed)
    need(digest(checkout/'cmux.xcodeproj/project.pbxproj') == 'c2ce7b22d2bb1f7746a5fd4483bd78d12846335a836972bee8fd1878f8f7e2fe', 'project wiring changed')
    return {'stage':stage,'commit':commit,'parent_commit':git(checkout,'rev-parse','HEAD^'),'tree':tree,'files':rows}

def context(root, checkout, phase, evidence):
    need(phase in PHASES, 'unknown phase')
    stage = 'red' if phase=='red' else 'green'
    source = source_snapshot(root,checkout,stage)
    chain = read(evidence.parent/('SOURCE-'+stage+'.json'))
    need(same(chain['source'],source) and same(chain['run'],run_identity()), 'phase differs from original source attempt')
    compiled = read(evidence.parent/('COMPILE-'+stage+'.json'))
    need(type(compiled['exit_code']) is int and compiled['exit_code'] == 0 and same(compiled['source'],source) and same(compiled['run'],run_identity()), 'missing successful current test compilation')
    return {'schema':'cmux-natural-native-phase-v1','phase':phase,'run':run_identity(),'source':source,'contract_sha256':digest(root/'contract.json')}

def validate_native(root, value, phase, evidence):
    accounting = module(root,'app_host_result_accounting.py')
    data = read(evidence/'tests.json')
    results = accounting.parse_xcresult_tests(data)
    expected = value['phases'][phase]['required_test_ids']
    status = int((evidence/'exit-code.txt').read_text())
    log = (evidence/'execution.log').read_text()
    complete, reason = accounting.run_is_complete(log)
    need(complete and 'Post-test timed out after' not in log, 'incomplete native run: '+reason)
    need(set(results) == set(expected), 'actual test inventory differs from reviewed contract')
    need(all(result in ('Passed','Failed') for result in results.values()), 'skipped/unknown typed test')
    if phase=='red':
        failures = {test for test,result in results.items() if result=='Failed'}
        need(status == 65 and failures == set(value['expected_red_failed_ids']), 'red requires exact behavioral assertion failures')
        for test in failures:
            method = test.split('/')[1].removesuffix('()')
            hits = [line for line in log.splitlines() if re.search(r'\b'+re.escape(method)+r'\b',line) and ': error:' in line and value['expected_red_assertion'] in line]
            need(len(hits) == 1, 'red assertion evidence missing or repeated: '+test)
    else:
        need(status == 0 and all(result=='Passed' for result in results.values()), 'green requires every typed test passed')
    return {'exit_code':status,'executed_test_count':len(results),'results':results,'selectors':value['phases'][phase]['selectors'],'proof_scope':value['phases'][phase]['proof_scope']}

def validate_package(root,value,evidence):
    parser = module(root,'require_swift_test_execution.py')
    log = (evidence/'execution.log').read_text()
    clean = parser.ANSI.sub('',log)
    wrapper = (evidence/'wrapper.log').read_text()
    need(not any(marker in log+wrapper for marker in ('Exited with unexpected signal code','Restarting after unexpected exit','Post-test timed out after','Idle timed out after')), 'package interrupted')
    count = parser.execution_count(log)
    passed = re.findall(r'^\s*✔ Test "([^"\n]+)" passed after .+\.$',clean,re.M)
    need(count == 6 and len(passed)==len(set(passed))==6 and set(passed)==set(value['package_test_display_names']), 'package execution inventory differs')
    need(re.search(r'^\s*✔ Suite "?TerminalSurfaceStartupRestorePolicyTests"? passed after .+\.$',clean,re.M), 'package suite did not complete')
    status = int((evidence/'exit-code.txt').read_text())
    errors = [line for line in clean.splitlines() if re.search(r'(^|[^a-zA-Z])error:',line) and not re.match(r'\s*warning:|.*:\d+:\d+:\s+warning:',line)]
    cosmetic = status==1 and bool(errors) and all('unexpected binary' in line for line in errors)
    need(status==0 or cosmetic, 'package failed outside original cosmetic exception')
    return {'exit_code':status,'executed_test_count':count,'passed_display_names':passed,'cosmetic_ghostty_binary_diagnostic':cosmetic,'proof_scope':'six package cases newly executed on current green tree'}

def validate_phase(root,value,phase,evidence):
    return validate_package(root,value,evidence) if phase=='package' else validate_native(root,value,phase,evidence)

def verdict(root,phase,checkout,evidence):
    value = admit(root)
    intent = read(evidence/'INTENT.json')
    need(same(intent,context(root,checkout,phase,evidence)), 'test source/run changed after intent')
    judged = validate_phase(root,value,phase,evidence)
    save(evidence/'VERDICT.json',{**intent,**judged,'status':'EXPECTED_ASSERTION_RED' if phase=='red' else 'PASSED'})

def main():
    operation, directory, *args = sys.argv[1:]
    root = Path(directory).resolve()
    if operation=='admit':
        admit(root)
    elif operation=='selectors':
        print(','.join(admit(root)['phases'][args[0]]['selectors']))
    elif operation=='phase-intent':
        phase, checkout, evidence = args
        save(Path(evidence)/'INTENT.json',context(root,Path(checkout),phase,Path(evidence)))
    elif operation in ('verdict','package-verdict'):
        if operation=='package-verdict': args=['package',*args]
        verdict(root,args[0],Path(args[1]),Path(args[2]))
    elif operation=='compile-receipt':
        phase, checkout, evidence, status, log = args
        need(phase in ('red','green'), 'invalid compile phase')
        source = source_snapshot(root,Path(checkout),phase)
        chain = read(Path(evidence)/('SOURCE-'+phase+'.json'))
        need(same(chain['source'],source) and same(chain['run'],run_identity()), 'compile source/attempt mismatch')
        save(Path(evidence)/('COMPILE-'+phase+'.json'),{'schema':'cmux-natural-native-compile-v1','phase':phase,'run':run_identity(),'source':source,'exit_code':int(status),'log':pin(Path(log)),'contract_sha256':digest(root/'contract.json')})
    else:
        raise ValueError('unknown operation')

if __name__=='__main__':
    main()
