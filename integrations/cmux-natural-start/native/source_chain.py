"""Reconstruct the accepted base, then make two current behavioral commits.

Only disposable GitHub checkouts are mutated. Each stage is a single original
attempt; a receipt or dirty index refuses replay. No builds are launched here.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import contract as c

def advance(stage, root, checkout, evidence):
    value = c.admit(root)
    identity = c.run_identity()
    need = c.need
    workspace = Path(os.environ['GITHUB_WORKSPACE']).resolve(strict=True)
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    checkout = checkout.resolve(strict=True)
    need(checkout.is_relative_to(workspace) and checkout != workspace, 'separate disposable source checkout required')
    need(evidence.resolve().is_relative_to(temporary) and not evidence.is_symlink(), 'owned runner evidence required')
    evidence.mkdir(mode=0o700,exist_ok=True)
    receipt = evidence/('SOURCE-'+stage+'.json')
    intent = evidence/('SOURCE-'+stage+'-INTENT.json')
    need(not receipt.exists() and not intent.exists(), 'original source attempt exists; no replay')
    need(not c.git(checkout,'diff','--name-only') and not c.git(checkout,'diff','--cached','--name-only'), 'dirty source before patch')
    if stage=='base':
        need(c.git(checkout,'rev-parse','HEAD') == value['source']['upstream_commit'], 'base must start at exact upstream')
        patch_names = ['original-tests.patch','original-fix.patch']
        message = 'chore: reconstruct accepted nested restore base'
        expected_paths = None
    elif stage in ('red','green'):
        previous = 'base' if stage=='red' else 'red'
        prev = c.read(evidence/('SOURCE-'+previous+'.json'))
        need(c.same(prev['run'],identity) and c.same(prev['source'],c.source_snapshot(root,checkout,previous)), 'previous source phase differs')
        patch_names = ['restore-tests.patch' if stage=='red' else 'full-fix.patch']
        frozen = c.read(root/'inputs/source-freeze.json')
        expected_paths = frozen['patches'][patch_names[0]]['paths']
        message = 'test: reproduce restore refusal with icon configuration flag' if stage=='red' else 'fix: allow configuration-only restore and signed icon policy'
    else:
        raise ValueError('unknown source phase')
    parent = c.git(checkout,'rev-parse','HEAD')
    c.save(intent,{'run':identity,'stage':stage,'parent_commit':parent,'patches':patch_names,'contract_sha256':c.digest(root/'contract.json')})
    commands = []
    def run(args):
        p = subprocess.run(['git','-C',str(checkout),*args],capture_output=True,text=True,timeout=120)
        commands.append({'argv':['git',*args],'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        need(p.returncode==0,'source command failed; original intent preserved')
        return p.stdout.strip()
    try:
        for name in patch_names:
            path = root/'inputs'/name
            run(['apply','--check',str(path)])
            run(['apply','--index',str(path)])
        if expected_paths is not None:
            need(set(run(['diff','--cached','--name-only']).splitlines())==set(expected_paths),'patch stage changes unexpected files')
        expected_tree = value['source']['base_green_tree' if stage=='base' else stage+'_tree']
        need(run(['write-tree'])==expected_tree,'reconstructed source tree differs')
        run(['-c','user.name=NaturalStartValidation','-c','user.email=native-validation@example.invalid','-c','commit.gpgsign=false','commit','-m',message])
        source = c.source_snapshot(root,checkout,stage)
        need(source['parent_commit']==parent,'new commit has wrong parent')
        c.save(receipt,{'schema':'cmux-natural-native-source-v1','run':identity,'source':source,'contract_sha256':c.digest(root/'contract.json'),'fixed_source':value['source']})
    finally:
        c.save(evidence/('SOURCE-'+stage+'-COMMANDS.json'),commands)
    print(json.dumps({'stage':stage,'commit':source['commit'],'tree':source['tree']}))

if __name__=='__main__':
    stage, root, checkout, evidence = sys.argv[1:]
    advance(stage,Path(root).resolve(),Path(checkout),Path(evidence))
