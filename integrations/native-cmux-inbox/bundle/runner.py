"""Pending-by-default, one isolated two-root cmux context/body runtime invocation."""
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
SCHEMA='real-cmux-duplex-body-runtime-v1'
EMPTY_INBOX_AGY_REVIEW_SHA='898c4f2585a2ac86d5f8edde21cb2425a5d08c0b1d8295da0ce66b33c8cdefea'
EMPTY_INBOX_ROOT_ACCEPTANCE_SHA='6e5f93eb5574af5237f8fc00a203d20be65afd580ec9d630f509eb4ae9f5aeed'
EMPTY_INBOX_CHECKS=(
    'original_attempt_and_artifact_bound',
    'actual_fixed_aqua_host_and_client_bound',
    'real_root_child_native_automatic_hooks_and_tool_pairs',
    'root_full_reader_empty_child_refused',
    'complete_one_close_and_bounded_same_peer_tree_evidence',
    'normal_owned_cleanup_and_120_second_gate',
)


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
    need(c.get('schema')==SCHEMA
         and c.get('status')=='ROOT_REVIEWED_NATIVE_BUILD_READY','actual native artifact review is pending')
    need(c.get('repository')=='lzhs1995/ccc' and c.get('attempt')==1 and c.get('retries')==0
         and c.get('business_operations')==0 and c.get('send_operations')==2,'unexpected runtime scope')
    need(c.get('expected_native_roots')==2 and c.get('expected_automatic_post_hooks')==9
         and c.get('total_task_limit_seconds')==120,'unexpected duplex runtime bounds')
    need(c.get('native_user_receipt_verified') is False and c.get('live_business_hook_verified') is False,
         'context/body fixture cannot claim native user delivery or live business loading')
    need(isinstance(c.get('runtime_branch'),str) and c['runtime_branch'].startswith('ccc/'),'unfilled runtime branch')
    for k in ('native_contract_sha256','host_source_review_sha256','source_manifest_sha256','reader_manifest_sha256','automatic_hook_result_sha256'):
        need(isinstance(c.get(k),str) and re.fullmatch('[0-9a-f]{64}',c[k]),'unfilled pin: '+k)
    need(c.get('fixed_app_pin_sha256')==APP_PIN,'fixed app source differs')
    need(c.get('empty_inbox_agy_review_sha256')==EMPTY_INBOX_AGY_REVIEW_SHA
         and c.get('empty_inbox_root_acceptance_sha256')==EMPTY_INBOX_ROOT_ACCEPTANCE_SHA,
         'original empty-inbox review/acceptance pins differ')


def validate_empty_inbox_prerequisites(review,acceptance):
    need(review.get('passed') is True
         and review.get('status')=='PASS_ORIGINAL_R2_REAL_CMUX_AUTOMATIC_EMPTY_INBOX_AND_OWNED_CLEANUP',
         'original independent empty-inbox review was not accepted')
    # ROOT-ACCEPTANCE has a status and six_checks, not an invented "passed" field.
    need(acceptance.get('status')=='ROOT_ACCEPTED_ORIGINAL_REAL_CMUX_EMPTY_INBOX_AND_NORMAL_CLEANUP'
         and acceptance.get('independent_review_sha256')==EMPTY_INBOX_AGY_REVIEW_SHA,
         'original Root acceptance is not bound to the independent review')
    expected=dict(run=42659904239,attempt=1,head='8c12cc35479903c65ca2e4f7eaa4f17287398944',
                  artifact=13694325650,artifact_members=76,
                  actual_result_sha256='03ccc85e86efa7a9c9601b42cd8b7c2411a320909dd4b7212b51388d2acaad3d',
                  runner_result_sha256='304018655bac93efadc1e20e978222dc28c56a481ac19bcaf357f24794b814cf')
    for label,record in (('independent review',review),('Root acceptance',acceptance)):
        need(all(record.get(key)==value for key,value in expected.items()),label+' original attempt/artifact differs')
        checks=record.get('six_checks',{})
        need(isinstance(checks,dict) and all(checks.get(key) is True for key in EMPTY_INBOX_CHECKS),
             label+' original six checks incomplete')
        limits=record.get('not_proven',{})
        need(isinstance(limits,dict) and all(limits.get(key) is True for key in (
            'nonempty_context','full_body_read','bidirectional_receipt','live_business_hook_loading',
            'physical_power_loss_or_cold_recovery','original_failed_run_root_cause')),
            label+' original limitations must remain explicit')
        seconds=record.get('task_and_cleanup_seconds')
        need(type(seconds) in (int,float) and 0<=seconds<=120,label+' original 120-second gate missing')
        need(record.get('production_changes')==0,label+' unexpected production change')
    need(review.get('original_files_modified') is False and review.get('old_tests_rerun')==0
         and review.get('runtime_invocations_by_reviewer')==0
         and acceptance.get('old_run_replayed') is False and acceptance.get('tests_rerun')==0
         and acceptance.get('runtime_invocations_by_root_adoption')==0
         and acceptance.get('whole_goal_complete') is False,'original review scope differs')


def load_empty_inbox_prerequisites(c,directory=None):
    directory=HERE/'prerequisites' if directory is None else Path(directory)
    need(c.get('empty_inbox_agy_review_sha256')==EMPTY_INBOX_AGY_REVIEW_SHA
         and c.get('empty_inbox_root_acceptance_sha256')==EMPTY_INBOX_ROOT_ACCEPTANCE_SHA,
         'unreviewed empty-inbox prerequisite pins')
    raw={
        'AGY-REVIEW.json':pinned(directory/'AGY-REVIEW.json',EMPTY_INBOX_AGY_REVIEW_SHA),
        'ROOT-ACCEPTANCE.json':pinned(directory/'ROOT-ACCEPTANCE.json',EMPTY_INBOX_ROOT_ACCEPTANCE_SHA),
    }
    validate_empty_inbox_prerequisites(json.loads(raw['AGY-REVIEW.json']),json.loads(raw['ROOT-ACCEPTANCE.json']))
    return raw


def validate_runtime_result(actual):
    need(actual.get('schema')==SCHEMA,'unexpected duplex result schema')
    for name in ('passed','child_root_inbox_refused','full_native_host_reader_verified',
                 'context_emission_verified','body_read_verified','bidirectional_receipt_verified'):
        need(actual.get(name) is True,'duplex runtime proof incomplete: '+name)
    for name in ('native_user_receipt_verified','live_business_hook_verified',
                 'physical_power_loss_verified','task_acceptance'):
        need(actual.get(name) is False,'duplex runtime scope overstated: '+name)
    counts=dict(actual_native_roots=2,automatic_post_hooks=9,mailbox_writes=2,terminal_inputs=0,
                mailbox_claims=0,mailbox_mark_read=0,source_tests_rerun=0,supplier_requests=0,production_changes=0)
    for name,value in counts.items():
        need(type(actual.get(name)) is int and actual[name]==value,'duplex runtime count differs: '+name)
    seconds=actual.get('task_and_cleanup_seconds')
    need(type(seconds) in (int,float) and 0<=seconds<=120,'duplex task and cleanup exceeded original 120-second gate')


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
    import duplex_runtime
    root=f.require_runner(root)
    args=json.loads(f.pinned(root/'HOST-ARGUMENTS.json',arguments_sha))
    c=json.loads(f.pinned(root/'ROOT-CONTRACT.json',args['root_contract_sha256']))
    validate_root_contract(c);require_runner(c)
    load_empty_inbox_prerequisites(c,root/'prerequisites')
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
                pid=os.getpid(),mailbox_writes=0,planned_mailbox_writes=2,planned_native_roots=2,
                planned_automatic_post_hooks=9,total_task_limit_seconds=120,production_changes=0))
    duplex_runtime.execute(root,args)


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
        print(json.dumps(dict(status='PENDING_ROOT_DUPLEX_RUNTIME_REVIEW',network_calls=0,gui_started=False,source_imports=0)))
        return 75
    validate_root_contract(c)
    need(args.source_manifest_sha256==c['source_manifest_sha256'],'external source manifest differs')
    source_manifest(args.source_manifest_sha256)
    prerequisites=load_empty_inbox_prerequisites(c)
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
        (root/'prerequisites').mkdir(mode=0o700,exist_ok=False)
        for name,contents in prerequisites.items():f.write_new(root/'prerequisites'/name,contents,0o400)
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
        validate_runtime_result(actual)
        f.save_once(root/'RUNNER-RESULT.json',dict(schema=SCHEMA,passed=True,actual_result_sha256=io.sha(root/'RESULT.json'),
            source_sha256=args.source_manifest_sha256,original_native_contract_sha256=c['native_contract_sha256'],
            empty_inbox_agy_review_sha256=EMPTY_INBOX_AGY_REVIEW_SHA,
            empty_inbox_root_acceptance_sha256=EMPTY_INBOX_ROOT_ACCEPTANCE_SHA,
            runtime_invocations=1,retries=0,production_operations=0,actual_native_roots=2,
            automatic_post_hooks=9,mailbox_writes=2,native_user_receipt_verified=False,
            live_business_hook_verified=False,task_and_cleanup_seconds=actual['task_and_cleanup_seconds']))
    except BaseException as error:
        f.save_once(root/'RUNNER-FAILURE.json',dict(type=type(error).__name__,error=str(error),runtime_started=started,replay_allowed=False))
        if started:
            try:
                import duplex_runtime
                duplex_runtime.emergency(root)
            except BaseException as cleanup_error:
                f.save_once(root/'EMERGENCY-CLEANUP-FAILURE.json',dict(error=str(cleanup_error),unknown_preserved=True))
        raise
    return 0


if __name__=='__main__':raise SystemExit(main())
