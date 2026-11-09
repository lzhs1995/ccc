"""Pending-by-default, one real GitHub cmux empty-inbox runtime invocation."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

HERE=Path(__file__).resolve().parent
APP_PIN='cc1f7d5223d96b135955c39ee2955c27966d52f458918c7c2096546912f7a4b0'


def need(value,reason):
    if not value:raise ValueError(reason)


def pinned(path,wanted):
    need(isinstance(wanted,str) and re.fullmatch('[0-9a-f]{64}',wanted),'unfilled external pin')
    path=Path(path)
    need(path.is_absolute() and path.resolve(strict=True)==path and not path.is_symlink(),'canonical source/input required')
    raw=path.read_bytes()
    need(hashlib.sha256(raw).hexdigest()==wanted,'pinned source/input differs: '+str(path))
    return raw


def source_manifest(wanted):
    raw=pinned(HERE/'MANIFEST.json',wanted)
    manifest=json.loads(raw)
    actual={p.relative_to(HERE).as_posix() for p in HERE.rglob('*') if p.is_file() and p!=HERE/'MANIFEST.json'}
    need(actual==set(manifest['files']) and not any(p.is_symlink() for p in HERE.rglob('*')),'full source membership differs')
    for name,row in manifest['files'].items():pinned(HERE/name,row['sha256'])
    return manifest


def load_runtime():
    # Called only after complete source validation. Whole original admission
    # module is imported; its old main/PTY fixture entry is never invoked.
    sys.path.insert(0,str(HERE/'reader'));sys.path.insert(0,str(HERE/'native'))
    spec=importlib.util.spec_from_file_location('reviewed_original_native_admission',HERE/'native/runner.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def validate_root_contract(c):
    need(c.get('schema')=='real-cmux-empty-inbox-runtime-v1'
         and c.get('status')=='ROOT_REVIEWED_NATIVE_BUILD_READY','actual native artifact review is pending')
    need(c.get('repository')=='lzhs1995/ccc' and c.get('attempt')==1 and c.get('retries')==0
         and c.get('business_operations')==0 and c.get('send_operations')==0,'unexpected runtime scope')
    need(isinstance(c.get('runtime_branch'),str) and c['runtime_branch'].startswith('ccc/'),'unfilled runtime branch')
    for k in ('native_contract_sha256','host_source_review_sha256','source_manifest_sha256','reader_manifest_sha256','automatic_hook_result_sha256'):
        need(isinstance(c.get(k),str) and re.fullmatch('[0-9a-f]{64}',c[k]),'unfilled pin: '+k)
    need(c.get('fixed_app_pin_sha256')==APP_PIN,'fixed app source differs')


def require_runner(c):
    need(sys.version_info>=(3,11) and sys.platform=='darwin' and platform.machine()=='arm64'
         and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
         and os.environ.get('GITHUB_REPOSITORY')=='lzhs1995/ccc' and os.environ.get('GITHUB_RUN_ATTEMPT')=='1'
         and os.environ.get('GITHUB_REF_NAME')==c['runtime_branch'] and os.environ.get('GITHUB_EVENT_NAME')=='push',
         'only the reviewed fresh GitHub macOS arm64 runtime attempt')


def execute(root,arguments_sha,source_sha):
    source_manifest(source_sha)
    native=load_runtime()
    import fixture_common as f
    import host_runtime
    root=f.require_runner(root)
    args=json.loads(f.pinned(root/'HOST-ARGUMENTS.json',arguments_sha))
    c=json.loads(f.pinned(root/'ROOT-CONTRACT.json',args['root_contract_sha256']))
    validate_root_contract(c);require_runner(c)
    need(source_sha==c['source_manifest_sha256']==args['source_manifest_sha256'],'execution source differs')
    original=json.loads(f.pinned(root/'NATIVE-CONTRACT.json',c['native_contract_sha256']))
    f.pinned(root/'AUTOMATIC-HOOK-RESULT.json',c['automatic_hook_result_sha256'])
    native.validate_contract(original)
    receipt=json.loads(f.pinned(root/'a/ADMISSION.json',args['native_admission_sha256']))
    need(receipt.get('passed') is True and receipt.get('build_run')==42561656291
         and receipt.get('build_attempt')==1 and receipt.get('binary_sha256')==original['binary_sha256']
         and receipt.get('effective_source_sha256')==original['effective_source_sha256'], 'actual original native admission missing')
    f.pinned(args['binary'],original['binary_sha256'])
    need(args['native_binary_sha256']==original['binary_sha256'] and args['reader_manifest_sha256']==c['reader_manifest_sha256'],
         'execution arguments differ from original reviewed build/reader')
    f.save_once(root/'EXECUTION-INTENT.json',dict(invocations=1,retries=0,source_sha256=source_sha,
                pid=os.getpid(),mailbox_writes=0,production_changes=0))
    host_runtime.execute(root,args)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract',type=Path)
    parser.add_argument('--contract-sha256')
    parser.add_argument('--native-contract',type=Path)
    parser.add_argument('--automatic-hook-result',type=Path)
    parser.add_argument('--source-manifest-sha256',required=True)
    parser.add_argument('--check-contract',action='store_true')
    parser.add_argument('--execute-root',type=Path)
    parser.add_argument('--arguments-sha256')
    args=parser.parse_args()
    if args.execute_root is not None:
        need(args.contract is None and not args.check_contract and args.arguments_sha256,'mixed entry modes')
        execute(args.execute_root,args.arguments_sha256,args.source_manifest_sha256);return 0
    need(args.contract is not None and args.contract_sha256,'explicit reviewed contract required')
    raw=pinned(args.contract,args.contract_sha256);c=json.loads(raw)
    if c.get('status')!='ROOT_REVIEWED_NATIVE_BUILD_READY':
        print(json.dumps(dict(status='PENDING_NATIVE_ARTIFACT_REVIEW',network_calls=0,gui_started=False,source_imports=0)))
        return 75
    validate_root_contract(c)
    need(args.source_manifest_sha256==c['source_manifest_sha256'],'external source manifest differs')
    source_manifest(args.source_manifest_sha256)
    need(args.native_contract is not None,'original native build contract required')
    native_raw=pinned(args.native_contract,c['native_contract_sha256'])
    need(args.automatic_hook_result is not None,'actual reviewed automatic-hook runtime result required')
    hook_raw=pinned(args.automatic_hook_result,c['automatic_hook_result_sha256'])
    hook_result=json.loads(hook_raw)
    need(hook_result.get('passed') is True and hook_result.get('isolated_automatic_root_and_child_hooks_verified') is True
         and hook_result.get('actual_automatic_hook_events')==4 and hook_result.get('production_installed') is False
         and hook_result.get('cmux_host_verified') is False, 'bare-PTY automatic hook prerequisite is not accepted')
    native=load_runtime()
    native_c=json.loads(native_raw);native.validate_contract(native_c)
    for name,pin in native_c['runner_files'].items():pinned(HERE/'native'/name,pin)
    pinned(HERE/'reader/MANIFEST.json',c['reader_manifest_sha256'])
    pinned(HERE/'fixed-inputs/APP-PIN.json',APP_PIN)
    if args.check_contract:
        print(json.dumps(dict(status='LOCAL_SOURCE_AND_CONTRACT_CHECKED_NO_RUNTIME',network_calls=0,gui_started=False)))
        return 0
    require_runner(c)
    import fixture_common as f
    import artifact_io as io
    import fixed_stage
    import fixed_app_admission as app_admission
    import rtk_admission
    from original_aqua import aqua_command
    root=Path(os.environ['RUNNER_TEMP']).resolve(strict=True)/'h'
    need(len(os.fsencode(root/'h/.codex/credential-observations/compaction-recovery/c-99999-00000000.sock'))<104,
         'canonical RUNNER_TEMP path exceeds native Unix socket bound')
    root.mkdir(mode=0o700,exist_ok=False)
    started=False
    try:
        f.write_new(root/'ROOT-CONTRACT.json',raw,0o400)
        f.write_new(root/'NATIVE-CONTRACT.json',native_raw,0o400)
        f.write_new(root/'AUTOMATIC-HOOK-RESULT.json',hook_raw,0o400)
        f.save_once(root/'ADMISSION-INTENT.json',dict(contract_sha256=args.contract_sha256,native_contract_sha256=c['native_contract_sha256'],fixture_started=False))
        binary,host,result=native.admit(root,native_c)
        plan=fixed_stage.preflight(HERE/'fixed-inputs/APP-PIN.json',APP_PIN,HERE/'fixed-inputs',app_admission)
        fixed_stage.stage_plan(plan,root/'fixed-inputs',app_admission)
        proof=app_admission.admit(root,root/'fixed-inputs',HERE/'fixed-inputs/APP-PIN.json',APP_PIN)
        fixed_sha=f.save_once(root/'FIXED-APP-ADMISSION.json',proof)
        rtk=rtk_admission.admit(root)
        run_args=dict(root_contract_sha256=args.contract_sha256,source_manifest_sha256=args.source_manifest_sha256,
            reader_manifest_sha256=c['reader_manifest_sha256'],binary=str(binary),host=str(host),
            native_binary_sha256=native_c['binary_sha256'],host_sha256=native.fixture.OFFICIAL_HOST_SHA,
            native_admission_sha256=io.sha(root/'a/ADMISSION.json'),fixed_proof_sha256=fixed_sha,rtk=str(rtk))
        run_sha=f.save_once(root/'HOST-ARGUMENTS.json',run_args)
        env={k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','LANG','__CF_USER_TEXT_ENCODING','PATH',
            'GITHUB_ACTIONS','GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_SHA','RUNNER_TEMP',
            'RUNNER_ENVIRONMENT','GITHUB_REF_NAME','GITHUB_EVENT_NAME') if k in os.environ}
        command=[sys.executable,'-BE',str(Path(__file__).resolve()),'--execute-root',str(root),'--arguments-sha256',run_sha,
                 '--source-manifest-sha256',args.source_manifest_sha256]
        command=aqua_command(command,env)
        f.save_once(root/'AQUA-EXEC-INTENT.json',dict(argv=command,credential_environment_forwarded=False,invocations=1))
        started=True
        with (root/'runtime.log').open('xb') as log:
            subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=240)
        actual=json.loads(f.strict_file(root/'RESULT.json'))
        need(actual.get('passed') is True and actual.get('actual_messages_empty') is True
             and actual.get('automatic_post_hooks')==2 and actual.get('child_root_inbox_refused') is True
             and actual.get('full_native_host_reader_verified') is True
             and actual.get('body_read_verified') is False and actual.get('bidirectional_receipt_verified') is False,
             'limited empty-inbox runtime result incomplete')
        f.save_once(root/'RUNNER-RESULT.json',dict(passed=True,actual_result_sha256=io.sha(root/'RESULT.json'),
            source_sha256=args.source_manifest_sha256,original_native_contract_sha256=c['native_contract_sha256'],
            runtime_invocations=1,retries=0,production_operations=0,mailbox_writes=0))
    except BaseException as error:
        f.save_once(root/'RUNNER-FAILURE.json',dict(type=type(error).__name__,error=str(error),runtime_started=started,replay_allowed=False))
        if started:
            try:
                from emergency_cleanup import reconcile
                reconcile(root)
            except BaseException as cleanup_error:
                f.save_once(root/'EMERGENCY-CLEANUP-FAILURE.json',dict(error=str(cleanup_error),unknown_preserved=True))
        raise
    return 0


if __name__=='__main__':raise SystemExit(main())
