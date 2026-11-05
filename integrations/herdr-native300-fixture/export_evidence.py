"""Export only new synthetic fixture evidence after complete source verification.

Never copy source bundles, old fixtures, native homes, private guard releases,
arbitrary text files or raw logs. Missing outputs remain explicit, not PASS.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

MANIFEST_SHA='873b74af71bc0a90b235671c7884893d23a5306b5f1ef4938b2475c59626e07d'
MAX_FILE=32*1024**2
ROOT_JSON={
    'RESULT.json':('standalone_case_passed','full_contract_passed','remaining'),
    'STANDALONE-CASE.json':('passed','same_sid','same_client','same_original_controller','actual_summary_and_tool_seen',
                            'shared_case_passed','active_goal_case_passed','unknown_case_passed','complete_180_300_acceptance'),
    'ACTUAL-AQUA-SESSION.json':('console_uid','manager'),
    'OWNED-CLEANUP.json':('phases','passed','unknown_workspace_creation','native_inputs_by_cleanup'),
}
GUI_JSON={
    'nightly/ARTIFACT-PROOF.json':('schema','passed','artifact_id','artifact_sha256','artifact_bytes','source_repository',
        'exact_source_head','admission_repository','admission_run','admission_attempt','original_build_run','original_build_attempt',
        'app_inventory_sha256','application_sha256','cli_sha256','team_identifier','bundle_identifier','build_version',
        'signed_commit_marker','actual_mount_detached','app_launched','production_installation'),
    'gui/GUI-PROCESS-CLEANUP.json':('pid','birth','returncode','exact_gui_process_gone','native_and_workspace_cleanup_proven'),
}
NATIVE_JSON={
    'admissions/native/NATIVE-ADMISSION.json':('repository','head_repository','head_branch','head_sha','run_id','run_attempt',
        'acceptance_contract','freeze_sha256','ci_sha256','workflow_sha256','release_source_sha256','test_source_sha256',
        'run_status','run_conclusion','required_cases_passed','workspace_required_cases_passed','standalone_native_required_cases_passed',
        'schema_files_verified','rustc_version','actual_rustc','artifact_id','artifact_sha256','build_manifest_sha256','sha256','fixture_executed'),
    'admissions/native/evidence/RESULT.json':('passed','build_manifest_sha256'),
    'admissions/native/evidence/FRESH-TEST-RESULT.json':('passed','freeze_sha256','historical_passes_reused','all_affected_libraries',
        'required_cases','release_required_cases','standalone_native'),
    'admissions/herdr/HERDR-ADMISSION.json':('sha256','required_tests_passed','ignored_not_run','source_phases_verified',
        'mirror_phases_verified','run_id','run_attempt','head_sha','remote_proof_sha256','source_contract_sha256'),
}
NATIVE_XML=('affected-libraries.xml','new-and-foreground-tests.xml','release-key-tests.xml','native-libraries.xml','native-required.xml')
HERDR_ZIP_MEMBERS=('RESULT.json','UNIT-RESULT.json','unit.xml','LIST.json',
    'SOURCE-DIFF-after-fetch.json','SOURCE-DIFF-after-server.json','SOURCE-DIFF-after-unit.json',
    'SOURCE-DIFF-after-list.json','SOURCE-DIFF-after-tests.json')
RAW_LOG_SUMMARIES=('fixture.log','factory.log','nightly-admission.log','gui/cmux.log','admissions/herdr/verifier.log')
FORBIDDEN=('inputs','pair','guard-releases','guard-source','original-observer','n','h','source','artifact-proof')

def need(value,reason):
    if not value:raise ValueError(reason)

def digest(raw):return hashlib.sha256(raw).hexdigest()

def read_exact(root,relative):
    path=root/relative
    need(not Path(relative).is_absolute() and '..' not in Path(relative).parts,'non-relative evidence input')
    need(path==path.resolve(strict=True) and path.is_relative_to(root),'linked evidence input')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        a=os.fstat(stream.fileno())
        need(stat.S_ISREG(a.st_mode) and a.st_uid==os.geteuid() and a.st_nlink==1 and not a.st_mode&0o022
             and a.st_size<=MAX_FILE,'unsafe or oversized evidence input')
        raw=stream.read(MAX_FILE+1);b=os.fstat(stream.fileno())
    key=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    need(key(a)==key(b)==key(path.lstat()) and len(raw)==b.st_size,'evidence changed during bounded read')
    return raw

def save(out,name,value):
    path=out/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=json.dumps(value,sort_keys=True,indent=2).encode()+b'\n'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    return digest(raw)

def verify_complete_bundle(bundle,manifest_sha):
    need(manifest_sha==MANIFEST_SHA,'unreviewed source manifest')
    raw=read_exact(bundle,'PUBLIC-SOURCE-MANIFEST.json');need(digest(raw)==manifest_sha,'complete bundle manifest changed')
    manifest=json.loads(raw);need(len(manifest['files'])==547,'complete source member count differs')
    # Full immutable validation is mandatory; none of these source bytes are
    # copied into the public evidence directory.
    for name,pin in manifest['files'].items():need(digest(read_exact(bundle,name))==pin,'complete source member differs')
    actual={p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file() or p.is_symlink()}
    need(actual==set(manifest['files'])|{'PUBLIC-SOURCE-MANIFEST.json'},'complete source closure differs')
    return {'manifest_sha256':manifest_sha,'members_verified':547,'source_bytes_exported':0}

def scrub_errors(value):
    if isinstance(value,dict):
        return {k:( {'sha256':digest(str(v).encode()),'bytes':len(str(v).encode())} if k in ('error','reason','message')
                   else scrub_errors(v)) for k,v in value.items()}
    if isinstance(value,list):return [scrub_errors(v) for v in value]
    return value

def export_selected(root,out):
    """Fixed path enumeration only; never glob inside a native/source home."""
    out.mkdir(mode=0o700,exist_ok=False);exported={};missing=[];rejected=[]
    def project(relative,keys):
        try:
            raw=read_exact(root,relative);obj=json.loads(raw)
            need(isinstance(obj,dict),'evidence is not object')
            value={k:obj[k] for k in keys if k in obj}
            name='results/'+relative.replace('/','__')
            exported[name]=save(out,name,{'original_file_sha256':digest(raw),'value':scrub_errors(value)})
        except FileNotFoundError:missing.append(relative)
        except (OSError,ValueError,KeyError) as error:rejected.append({'path':relative,'type':type(error).__name__})
    for mapping in (ROOT_JSON,GUI_JSON,NATIVE_JSON):
        for relative,keys in mapping.items():project(relative,keys)
    for relative in ('FAILURE.json','RUNNER-FAILURE.json'):
        try:
            raw=read_exact(root,relative);obj=json.loads(raw)
            exported[relative]=save(out,relative,{'original_file_sha256':digest(raw),
                'type':obj.get('type','recorded_failure'),'error_sha256':digest(str(obj.get('error','')).encode()),
                'error_bytes':len(str(obj.get('error','')).encode()),'full_contract_passed':False})
        except FileNotFoundError:pass
        except (OSError,ValueError) as error:rejected.append({'path':relative,'type':type(error).__name__})
    for relative in RAW_LOG_SUMMARIES:
        try:
            raw=read_exact(root,relative)
            name='log-summaries/'+relative.replace('/','__')+'.json'
            exported[name]=save(out,name,{'original_file_sha256':digest(raw),'bytes':len(raw),'lines':raw.count(b'\n'),
                'traceback_count':raw.count(b'Traceback (most recent call last):'), 'raw_log_exported':False})
        except FileNotFoundError:missing.append(relative)
        except (OSError,ValueError) as error:rejected.append({'path':relative,'type':type(error).__name__})
    # These fixed build JUnits describe public native source tests, not the
    # original paired runtime's private terminal sample corpus.
    for name in NATIVE_XML:
        relative='admissions/native/evidence/'+name
        try:
            raw=read_exact(root,relative)
            path=out/'junit'/('native-'+name);path.parent.mkdir(exist_ok=True,mode=0o700)
            with path.open('xb') as stream:stream.write(raw)
            path.chmod(0o600);exported[path.relative_to(out).as_posix()]=digest(raw)
        except FileNotFoundError:missing.append(relative)
        except (OSError,ValueError) as error:rejected.append({'path':relative,'type':type(error).__name__})
    try:
        proof=json.loads(read_exact(root,'admissions/herdr/proof/REMOTE-PROOF.json'))
        archive=root/'admissions/herdr/evidence.zip'
        raw=read_exact(root,'admissions/herdr/evidence.zip')
        need(proof['files'].get(str(archive))==digest(raw),'original verified Herdr archive changed')
        import io
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in HERDR_ZIP_MEMBERS:
                members=[v for v in z.infolist() if v.filename==name]
                need(len(members)==1 and members[0].file_size<=MAX_FILE and not stat.S_ISLNK(members[0].external_attr>>16),
                     'unverified or duplicate original Herdr evidence member')
                data=z.read(members[0]);path=out/'herdr-build'/name;path.parent.mkdir(exist_ok=True,mode=0o700)
                with path.open('xb') as stream:stream.write(data)
                path.chmod(0o600);exported[path.relative_to(out).as_posix()]=digest(data)
    except FileNotFoundError:missing.append('admissions/herdr/evidence.zip + original proof')
    except (OSError,ValueError,KeyError,zipfile.BadZipFile) as error:rejected.append({'path':'admissions/herdr/evidence.zip','type':type(error).__name__})
    try:
        raw=read_exact(root,'STANDALONE-EVIDENCE.json');proof=json.loads(raw)
        need(proof.get('schema')=='actual-original-herdr-standalone300-evidence-v1' and proof.get('passed') is True,'no actual standalone acceptance')
        transcript=read_exact(root,'ORIGINAL-NATIVE-TRANSCRIPT.jsonl')
        info=proof['transcript'];need(digest(transcript)==info['sha256'] and len(transcript)==info['bytes'],'native transcript proof differs')
        spans=[]
        for name in ('task_started','user_message','task_complete'):
            loc=info[name];start=loc['offset'];end=start+loc['bytes'];line=transcript[start:end]
            need(start>=0 and end<=len(transcript) and digest(line)==loc['sha256'],'exact original native span differs')
            value=json.loads(line);need(value.get('type')=='event_msg' and value.get('payload',{}).get('type')==name,'wrong native event span')
            if name=='user_message':need(str(value['payload'].get('message','')).startswith('Run the original private native compaction fixture'),'not synthetic fixture user')
            else:need(value['payload'].get('turn_id')==info['turn_id'],'native original turn differs')
            spans.append({'kind':name,'location':loc,'record':value})
        name='results/STANDALONE-EVIDENCE.json'
        # All fields below are generated solely from this new fixture; exclude
        # raw authority/grant directories and full native session history.
        selected={k:proof[k] for k in ('schema','passed','controller','client','original_expected','replacement_expected','warning',
                  'original_wire_requests','native_grant_sha256','native_intent_sha256','provider_requests_sha256','provider_tcp_eof',
                  'provider_wait_seconds','clock_contract','actual_no_active_goal','complete_180_300_acceptance')}
        selected['native_receipt']={k:proof['native_receipt'][k] for k in ('sha256','value','grant_sha256')}
        selected['transcript_proof']={k:info[k] for k in ('sha256','bytes','session_id','turn_id')}
        selected['original_native_event_spans']=spans
        exported[name]=save(out,name,{'original_file_sha256':digest(raw),'value':selected})
    except FileNotFoundError:missing.append('STANDALONE-EVIDENCE.json + ORIGINAL-NATIVE-TRANSCRIPT.jsonl')
    except (OSError,ValueError,KeyError) as error:rejected.append({'path':'STANDALONE-EVIDENCE.json','type':type(error).__name__})
    # No request body, system prompt or arbitrary current terminal content is
    # published. Keep only the generated provider's explicit outcome fields.
    project('provider/PROVIDER-RESULT.json',('requests','compaction_requests','original_retired','summary_seen_in_actual_followup',
         'actual_final_tool_output_seen','errors','native_control_calls_by_provider','complete_180_300_acceptance'))
    summary={'schema':'new-synthetic-fixture-evidence-whitelist-v1','export_completed':True,
        'native_fixture_passed_not_inferred':True,'exported':exported,'missing':missing,'rejected':rejected,
        'raw_logs_exported':False,'whole_native_history_exported':False,'old_controller_source_or_samples_exported':False,
        'forbidden_private_roots':list(FORBIDDEN)}
    save(out,'EXPORT-MANIFEST.json',summary)
    return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--private-root',type=Path,required=True)
    p.add_argument('--bundle',type=Path,required=True);p.add_argument('--bundle-manifest-sha256',required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    need(sys.platform=='darwin' and os.environ.get('GITHUB_ACTIONS')=='true'
         and os.environ.get('GITHUB_REPOSITORY')=='lzhs1995/ccc','only isolated GitHub macOS export')
    temp=Path(os.environ['RUNNER_TEMP']).resolve(strict=True);root=a.private_root
    need(root==root.resolve(strict=True) and root.is_relative_to(temp) and root!=temp
         and root.stat().st_uid==os.geteuid() and not root.stat().st_mode&0o077,'unsafe current private root')
    need(a.output.parent.resolve(strict=True)==temp and not a.output.exists(),'fresh sibling output under runner temp required')
    proof=verify_complete_bundle(a.bundle.resolve(strict=True),a.bundle_manifest_sha256)
    result=export_selected(root,a.output);save(a.output,'COMPLETE-SOURCE-VERIFIED.json',proof)
    print(json.dumps({'output':str(a.output),'files':len(result['exported']),'missing':len(result['missing']),
                      'rejected':len(result['rejected']),'fixture_passed_claimed':False}))

if __name__=='__main__':main()
