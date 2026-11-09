"""Single real cmux/root/child empty-inbox fixture, not a transport sender."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import time
import uuid

import fixture_common as f
import cmux_daemon_identity as native
import host_registration
from fixture import FixtureProvider, capture_private_sources
from owned_host import OwnedFixedOne, OwnedAlias, KernelScope
from original_workspace import workspace_from_reply

HERE=Path(__file__).resolve().parent
READER=HERE/'reader'


def role_result(root,role):
    paths=list(root.glob('host-hook-*/RESULT.json'))
    rows=[json.loads(f.strict_file(p)) for p in paths]
    matches=[r for r in rows if r.get('role')==role]
    f.need(len(matches)==1,'one actual '+role+' Post hook result required')
    r=matches[0]
    f.need(not list(root.glob('HOST-HOOK-FAILURE-*.json')),'actual hook failure preserved')
    f.need(r.get('mailbox_mutations')==0 and r.get('terminal_inputs')==0
           and r.get('host_mocks') is False and r.get('manual_hook') is False
           and not r.get('emitted_contexts') and not r.get('notices'),'wrong empty-inbox scope')
    if role=='root':
        f.need(r.get('status')=='FULL_READER_CALLED_NO_NEW_CONTEXT'
               and len(r.get('full_reader_calls',[]))==1
               and len(r.get('actual_parsed_host_queries',[]))==1
               and r['actual_parsed_host_queries'][0].get('messages')==[],
               'NO_NEW_CONTEXT is insufficient: actual messages=[] required')
    else:
        f.need(r.get('status')=='ACTUAL_CHILD_REFUSED_BEFORE_INBOX'
               and r.get('full_reader_calls')==[] and r.get('actual_parsed_host_queries')==[],
               'child reached root reader/inbox')
    return r


class HostProvider(FixtureProvider):
    """Reuse original HTTP/SSE/turn/spawn orchestration; change only exec payloads."""
    def __init__(self,root,project,python,registration,gui,alias):
        super().__init__(root,project,python)
        self.registration,self.gui,self.alias=registration,gui,alias
        self.markers={}

    def executable(self,body,role,sid):
        self.gui.stable_peer();self.alias.check()
        call='host_'+role+'_'+self.nonce
        self.calls[role]=call
        if role=='root':
            command=self.registration['bootstrap_command']
            self.markers[role]='HOST_FIXTURE_BOOTSTRAP:'
        else:
            self.markers[role]='HOST_EMPTY_CHILD_OK:'+self.nonce
            command="printf '%s\\n' "+shlex.quote(self.markers[role])  # shell builtin, no extra process
        args=dict(cmd=command,workdir=str(self.project),login=False,max_output_tokens=2000,yield_time_ms=10000)
        item=f.tool(body,call,'unused',self.project)
        if item['type']=='function_call':item['arguments']=json.dumps(args)
        else:item['input']='text(await tools.exec_command('+json.dumps(args)+'));'
        self.emitted[role]=item
        f.save_once(self.root/(role+'-ISSUED.json'),dict(sid=sid,role=role,command=command,item=item))
        return item

    def output_present(self,body,role):
        self.gui.stable_peer();self.alias.check()
        rows=f.items(body,'function_call_output')+f.items(body,'custom_tool_call_output')
        output=[r for r in rows if r.get('call_id')==self.calls[role]]
        f.need(len(output)==1 and self.markers[role] in str(output[0]),'original host tool result missing')
        result=role_result(self.root,role)
        f.need(result['native_origin']['reply']['threadId']==(self.root_sid if role=='root' else self.child_sid),
               'automatic hook actual thread differs from provider role')
        if role=='root':
            binding=f.strict_file(self.root/'BINDING.json')
            f.need('HOST_FIXTURE_BOOTSTRAP:'+f.sha(binding) in str(output[0]),'bootstrap bytes not in actual tool result')
        return True


def histories(root):
    paths=list((root/'h/.codex/sessions').rglob('*.jsonl'))
    f.need(len(paths)<=2,'unexpected native thread')
    result={}
    for p in paths:
        f.need(p.stat().st_size<=16*1024**2,'private rollout bound')
        raw=p.read_bytes()
        records=[json.loads(line) for line in raw.splitlines(keepends=True) if line.endswith(b'\n')]
        metadata=[r['payload'] for r in records if r.get('type')=='session_meta']
        f.need(len(metadata)==1,'unique native header required')
        sid=metadata[0]['id'];f.need(sid not in result,'duplicate native SID')
        result[sid]=(p,raw,records,metadata[0])
    return result


def complete(root,provider):
    values=histories(root)
    for sid,marker in ((provider.root_sid,'AUTOHOOK_FIXTURE_DONE:'+provider.nonce),
                       (provider.child_sid,'AUTOHOOK_CHILD_DONE:'+provider.nonce)):
        if sid not in values:return False
        if not any(r.get('type')=='event_msg' and r.get('payload',{}).get('type')=='task_complete'
                   and marker in str(r['payload'].get('last_agent_message','')) for r in values[sid][2]):return False
    return True


def freeze_history(root,provider,client):
    values=histories(root)
    f.need(set(values)=={provider.root_sid,provider.child_sid},'two original threads required')
    f.need(len(list(root.glob('host-hook-*')))==2,'exactly two native Post invocations required')
    result=[]
    binding=json.loads(f.strict_file(root/'BINDING.json'))
    for role,sid in (('root',provider.root_sid),('child',provider.child_sid)):
        path,raw,records,meta=values[sid]
        f.need(raw.endswith(b'\n'),'incomplete final native record')
        post=role_result(root,role)
        reply=post['native_origin']['reply']
        f.need(post['native_origin']['client']==dict(pid=client['pid'],birth=client['birth'])
               and reply['threadId']==sid and reply['origin']['kind']=='native-command-hook'
               and reply['origin']['event']=='PostToolUse','actual hook generation/provenance mismatch')
        if role=='root':
            f.need(meta.get('parent_thread_id') is None and isinstance(meta.get('source'),str)
                   and binding['actor']['session_id']==sid
                   and binding['bootstrap']['reply']['callId']==reply['callId'],'root bootstrap/Post mismatch')
        else:
            f.need(meta.get('parent_thread_id')==provider.root_sid
                   and meta.get('source',{}).get('subagent',{}).get('thread_spawn',{}).get('parent_thread_id')==provider.root_sid,
                   'actual native child parent relation missing')
        issued=provider.emitted[role];outer=issued['call_id']
        pairs=[(i,r) for i,r in enumerate(records) if r.get('type')=='response_item' and r.get('payload',{}).get('call_id')==outer]
        begin=[(i,r) for i,r in pairs if r['payload'].get('type')==issued['type']]
        end_type='function_call_output' if issued['type']=='function_call' else 'custom_tool_call_output'
        end=[(i,r) for i,r in pairs if r['payload'].get('type')==end_type]
        key='arguments' if issued['type']=='function_call' else 'input'
        f.need(len(begin)==len(end)==1 and begin[0][0]<end[0][0]
               and all(begin[0][1]['payload'].get(k)==issued.get(k) for k in ('name','namespace',key))
               and provider.markers[role] in str(end[0][1]['payload'].get('output')),'actual tool call/output pair missing')
        if issued['type']=='function_call':f.need(outer==reply['callId'],'direct inner call ID differs')
        f.write_new(root/(role+'-native.jsonl'),raw,0o600)
        result.append(dict(role=role,sid=sid,metadata=meta,bytes=len(raw),sha256=f.sha(raw),
                           raw_call_output_pairs=pairs,outer_call_id=outer,native_call_id=reply['callId']))
    return result


def terminate_native(root,client,prefix='NATIVE'):
    if client is None:return dict(tracked=False,original_gone=False)
    for label, sig, limit in (('TERM',signal.SIGTERM,10),('KILL',signal.SIGKILL,5)):
        birth=KernelScope.birth(client['pid'])
        if birth!=client['birth']:return dict(tracked=True,original_gone=True,last_birth=birth)
        observed=native.process(client['pid'])
        f.need(observed['birth']==client['birth'] and observed['executable']==client['executable'],
               'refuse signal of changed original native client')
        f.save_once(root/(prefix+'-'+label+'-INTENT.json'),dict(client=client,signal=label))
        f.need(KernelScope.birth(client['pid'])==client['birth'],'native generation changed after intent')
        os.kill(client['pid'],sig)
        deadline=time.monotonic()+limit
        while time.monotonic()<deadline:
            birth=KernelScope.birth(client['pid'])
            if birth!=client['birth']:return dict(tracked=True,original_gone=True,last_birth=birth)
            time.sleep(.1)
    raise RuntimeError('original native client still present after bounded cleanup')


def execute(root,args):
    root=f.require_runner(root)
    proof=json.loads(f.pinned(root/'FIXED-APP-ADMISSION.json',args['fixed_proof_sha256']))
    launch_root=root/'h';launch_root.mkdir(mode=0o700)
    home=f.prepare_native_home(root,launch_root/'.codex')
    f.need(len(os.fsencode(home/'credential-observations/compaction-recovery/c-99999-00000000.sock'))<104,
           'actual canonical native socket path too long')
    f.write_new(home/'credential-observations/enabled-v1',b'ccc-request-credentials-v1\n',0o600)
    project=root/'p';project.mkdir(mode=0o700)
    package=root/'bin';package.mkdir(mode=0o700)
    binary=package/'codex'
    shutil.copy2(args['binary'],binary);shutil.copy2(args['host'],package/'codex-code-mode-host')
    f.pinned(binary,args['native_binary_sha256']);f.pinned(package/'codex-code-mode-host',args['host_sha256'])
    python=str(Path(sys.executable).resolve(strict=True))
    registration=host_registration.prepare(root,READER,python,args['rtk'])
    f.save_once(root/'HOOK-REGISTRATION.json',registration)
    f.write_new(home/'hooks.json',(json.dumps(registration['hooks'],sort_keys=True,indent=2)+'\n').encode(),0o600)
    native_pin=dict(schema='native-hook-caller-pin-v1',binary_path=str(binary),binary_sha256=args['native_binary_sha256'],handler_id=registration['handler_id'])
    pin_sha=f.save_once(root/'NATIVE-PIN.json',native_pin)
    client=None;workspace=None;failure=None;cleanup={};provider=None
    with OwnedAlias(root,proof['app']) as alias:
        with OwnedFixedOne(root,proof) as gui:
            with HostProvider(root,project,python,registration,gui,alias) as provider:
                config=('model="gpt-6-astra"\nmodel_provider="local_fixture"\napproval_policy="never"\nsandbox_mode="danger-full-access"\n'
                    'check_for_update_on_startup=false\nmodel_auto_compact_token_limit=100000\n[tui]\nscreen_reader_detection_done=true\nshow_tooltips=false\n'
                    '[features]\nasync_compaction=false\nplugins=false\napps=false\nskip_host_skill_discovery=true\nmulti_agent_v2=true\nhooks=true\n'
                    '[model_providers.local_fixture]\nname="Deterministic private host fixture"\nwire_api="responses"\nbase_url='+json.dumps(provider.url)+'\n'
                    'requires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n'
                    '[projects.'+json.dumps(str(project))+']\ntrust_level="trusted"\n'+registration['state_toml'])
                f.write_new(home/'config.toml',config.encode(),0o600)
                f.write_new(home/'AGENTS.md',b'Only this isolated empty-inbox fixture is authorized.\n',0o600)
                run_contract=dict(schema='private-native-hook-host-run-v1',run_id=os.environ['GITHUB_RUN_ID'],run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],
                    fixture_only=True,business_controller_migration=False,source_manifest_sha256=args['reader_manifest_sha256'],native_pin_sha256=pin_sha,
                    fixed_app_path=proof['app'],fixed_cli_sha256=proof['pin']['cli_sha256'],external_native_admission_sha256=args['native_admission_sha256'],
                    external_fixed_app_admission_sha256=args['fixed_proof_sha256'])
                f.save_once(root/'RUN-CONTRACT.json',run_contract)
                env={k:os.environ[k] for k in ('GITHUB_ACTIONS','GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','RUNNER_TEMP','RUNNER_ENVIRONMENT')}
                env.update(HOME=str(launch_root),CODEX_HOME=str(home),CODEX_CLIENT_THREAD_OBSERVER='1',PATH=str(Path(python).parent)+':/usr/bin:/bin:/usr/sbin:/sbin',
                    SHELL='/bin/zsh',LANG='en_US.UTF-8',TMPDIR=str(root),CMUX_SOCKET_PATH=str(gui.socket),NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost')
                private_before,private_before_sha=capture_private_sources(root,home,'BEFORE')
                f.need(private_before['complete'],'private prelaunch source snapshot incomplete')
                launch=dict(private_source_before_sha256=private_before_sha,argv=[str(binary),'--no-daemon','--no-alt-screen','-C',str(project),provider.prompt],environment=env,
                    config_sha256=f.sha(config.encode()),hooks_sha256=f.sha(f.strict_file(home/'hooks.json')))
                launch_sha=f.save_once(root/'LAUNCH.json',launch)
                try:
                    before=json.loads(gui.cli_call(['--json','--id-format','both','tree','--all']).stdout)
                    before_ids={str(uuid.UUID(w['id'])).upper() for win in before.get('windows',[]) for w in win.get('workspaces',[])}
                    cmd=['--json','--id-format','both','workspace','create','--name','private-empty-'+provider.nonce[:8],
                         '--cwd',str(project),'--focus','false','--command',shlex.join([python,'-BE',str(HERE/'native_surface.py'),'--root',str(root),'--launch-sha256',launch_sha])]
                    for k,v in env.items():cmd.extend(['--env',k+'='+v])
                    f.save_once(root/'WORKSPACE-CREATE-INTENT.json',dict(argv=cmd,invocations=1,retries=0))
                    started=time.monotonic();deadline=started+70  # reserve 50 seconds for bounded cleanup
                    reply=gui.cli_call(cmd,timeout=20)
                    f.save_once(root/'WORKSPACE-CREATE-REPLY.json',dict(stdout=reply.stdout,stderr=reply.stderr,returncode=reply.returncode))
                    tree=json.loads(gui.cli_call(['--json','--id-format','both','tree','--all']).stdout)
                    workspace=workspace_from_reply(reply,tree,before_ids)
                    ready=False
                    while time.monotonic()<deadline:
                        gui.stable_peer();alias.check()
                        f.need(not provider.errors,'provider failure: '+str(provider.errors))
                        spawned=root/'NATIVE-SPAWN-INTENT.json'
                        if client is None and spawned.is_file():
                            row=json.loads(f.strict_file(spawned))
                            observed=native.process(row['pid'])
                            f.need(observed['birth']==row['birth'],'launcher generation changed')
                            if observed['executable']==str(binary):
                                f.need(observed['argv']==launch['argv'],'actual TUI argv differs')
                                actual_socket=home/'credential-observations/compaction-recovery'/('c-%s-00000000.sock'%row['pid'])
                                f.need(len(os.fsencode(actual_socket))<104,'actual native PID makes Unix socket path too long')
                                argv,actual_env=KernelScope.arguments(row['pid'])
                                f.need(all(actual_env.get(k)==v for k,v in env.items()) and actual_env['CMUX_WORKSPACE_ID'].upper()==workspace,
                                       'actual native environment differs')
                                client=dict(pid=row['pid'],birth=row['birth'],executable=str(binary))
                                provider.client=client
                                f.save_once(root/'CLIENT.json',dict(**client,argv=argv,environment={k:actual_env[k] for k in env},
                                    surface=actual_env['CMUX_SURFACE_ID'],workspace=actual_env['CMUX_WORKSPACE_ID'],tty=row['tty']))
                        if client is not None:
                            f.need(KernelScope.birth(client['pid'])==client['birth'],'original TUI exited early')
                            path=home/'credential-observations/compaction-recovery'/('client-%s-control.json'%client['pid'])
                            if not ready and path.is_file():
                                cap=json.loads(f.strict_file(path))
                                f.need(cap['pid']==client['pid'] and cap['birth']==client['birth'],'native capability generation differs')
                                f.save_once(root/'NATIVE-CAPABILITY.json',cap)
                                ready=True;provider.ready.set()
                        if provider.done.is_set() and provider.child_done.is_set() and complete(root,provider):break
                        time.sleep(.1)
                    f.need(provider.done.is_set() and provider.child_done.is_set() and complete(root,provider),'120-second original host attempt incomplete; no retry')
                    private_before=json.loads(f.pinned(root/'PRIVATE-SOURCE-BEFORE.json',launch['private_source_before_sha256']))
                    capture_private_sources(root,home,'AFTER',private_before)
                    f.need(f.sha(f.strict_file(home/'config.toml'))==launch['config_sha256']
                           and f.sha(f.strict_file(home/'hooks.json'))==launch['hooks_sha256'],'private hooks/config changed')
                    f.save_once(root/'HOST-ENDPOINT-FINAL.json',dict(peer=gui.stable_peer(),tree=json.loads(gui.cli_call(['--json','--id-format','both','tree','--all']).stdout)))
                except BaseException as error:
                    failure=dict(type=type(error).__name__,error=str(error),replay_allowed=False)
                    f.save_once(root/'FAILURE.json',failure)
                finally:
                    # Stop only the proven original native generation. Never
                    # infer missing writer/UNKNOWN as a reusable input attempt.
                    if client is None and (root/'NATIVE-SPAWN-INTENT.json').is_file():
                        try:
                            issued=json.loads(f.strict_file(root/'NATIVE-SPAWN-INTENT.json'))
                            observed=native.process(issued['pid'])
                            if observed['birth']==issued['birth'] and observed['executable']==str(binary):
                                client=dict(pid=issued['pid'],birth=issued['birth'],executable=str(binary))
                                f.save_once(root/'CLEANUP-RECONCILED-NATIVE.json',client)
                        except native.ProcessExited:
                            pass
                        except BaseException as error:
                            cleanup['reconcile']=dict(passed=False,error=str(error))
                    try:cleanup['native']=terminate_native(root,client)
                    except BaseException as error:cleanup['native']=dict(passed=False,error=str(error))
                    if workspace is not None:
                        try:
                            gui.stable_peer();alias.check()
                            f.save_once(root/'WORKSPACE-CLOSE-INTENT.json',dict(workspace=workspace))
                            closed=gui.cli_call(['--json','workspace','close','--workspace',workspace],timeout=5)
                            final=json.loads(gui.cli_call(['--json','--id-format','both','tree','--all'],timeout=5).stdout)
                            remaining={str(uuid.UUID(w['id'])).upper() for win in final.get('windows',[]) for w in win.get('workspaces',[])}
                            f.need(workspace not in remaining,'owned workspace remains')
                            cleanup['workspace']=dict(passed=True,stdout=closed.stdout)
                        except BaseException as error:cleanup['workspace']=dict(passed=False,error=str(error))
                    f.save_once(root/'TRACKED-CLEANUP.json',cleanup)
            if failure:raise RuntimeError(failure['error'])
            f.need(cleanup.get('native',{}).get('original_gone') is True and cleanup.get('workspace',{}).get('passed') is True,'owned cleanup incomplete')
            records=freeze_history(root,provider,client)
    f.need(not Path('/Applications/cmux.app').exists() and not Path('/Applications/cmux.app').is_symlink(),'owned alias remains')
    elapsed=time.monotonic()-started
    f.need(elapsed<=120,'task and cleanup exceeded 120 seconds; no acceptance')
    f.save_once(root/'RESULT.json',dict(passed=True,scope='single-root-real-child-full-reader-empty-inbox',native_history=records,
        root_sid=provider.root_sid,child_sid=provider.child_sid,automatic_post_hooks=2,actual_messages_empty=True,
        full_native_host_reader_verified=True,child_root_inbox_refused=True,context_emission_verified=False,
        body_read_verified=False,bidirectional_receipt_verified=False,live_business_hook_verified=False,
        source_tests_rerun=0,old_nine_sid_gui_rerun=False,supplier_requests=0,mailbox_writes=0,production_changes=0,task_and_cleanup_seconds=elapsed))
