"""One admitted GUI, two original native root TUIs, one A child, A→B→A."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
import uuid

import fixture_common as f
import cmux_daemon_identity as native
import host_registration
from fixture import capture_private_sources
from owned_host import OwnedFixedOne, OwnedAlias, KernelScope
from original_workspace import workspace_from_reply
from workspace_cleanup import close_workspace
from host_runtime import histories, terminate_native
from duplex_provider import DuplexProvider, Exchange
import duplex_protocol as p

HERE = Path(__file__).resolve().parent


class Actor:
    def __init__(self, root, label, args, proof, gui, alias, exchange):
        root.mkdir(mode=0o700)
        self.root, self.label, self.args, self.gui, self.alias = root, label, args, gui, alias
        self.client = self.workspace = None
        launch_root = root/'h'; launch_root.mkdir(mode=0o700)
        self.home = f.prepare_native_home(root, launch_root/'.codex')
        f.need(len(os.fsencode(self.home/'credential-observations/compaction-recovery/c-99999-00000000.sock')) < 104,
               'actual two-root native socket path too long')
        f.write_new(self.home/'credential-observations/enabled-v1', b'ccc-request-credentials-v1\n', 0o600)
        self.project = root/'p'; self.project.mkdir(mode=0o700)
        package = root/'bin'; package.mkdir(mode=0o700)
        self.binary = package/'codex'
        shutil.copy2(args['binary'], self.binary)
        shutil.copy2(args['host'], package/'codex-code-mode-host')
        f.pinned(self.binary, args['native_binary_sha256'])
        f.pinned(package/'codex-code-mode-host', args['host_sha256'])
        python = str(Path(sys.executable).resolve(strict=True))
        self.registration = host_registration.prepare(root, HERE/'reader', python, args['rtk'])
        f.save_once(root/'HOOK-REGISTRATION.json', self.registration)
        f.write_new(self.home/'hooks.json', (json.dumps(self.registration['hooks'], sort_keys=True, indent=2)+'\n').encode(), 0o600)
        pin_sha = f.save_once(root/'NATIVE-PIN.json', dict(schema='native-hook-caller-pin-v1',
            binary_path=str(self.binary), binary_sha256=args['native_binary_sha256'], handler_id=self.registration['handler_id']))
        self.provider = DuplexProvider(root, self.project, python, self.registration, gui, alias, exchange, label, args['rtk'])
        self.python, self.launch_root, self.proof, self.pin_sha = python, launch_root, proof, pin_sha

    def prepare(self):
        root, home, provider = self.root, self.home, self.provider
        config = ('model="gpt-6-astra"\nmodel_provider="local_fixture"\napproval_policy="never"\nsandbox_mode="danger-full-access"\n'
            'check_for_update_on_startup=false\nmodel_auto_compact_token_limit=100000\n[tui]\nscreen_reader_detection_done=true\nshow_tooltips=false\n'
            '[features]\nasync_compaction=false\nplugins=false\napps=false\nskip_host_skill_discovery=true\nmulti_agent_v2=true\nhooks=true\n'
            '[model_providers.local_fixture]\nname="Deterministic private duplex fixture"\nwire_api="responses"\nbase_url='+json.dumps(provider.url)+'\n'
            'requires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n'
            '[projects.'+json.dumps(str(self.project))+']\ntrust_level="trusted"\n'+self.registration['state_toml'])
        f.write_new(home/'config.toml', config.encode(), 0o600)
        f.write_new(home/'AGENTS.md', b'Only the isolated two-root A-to-B-to-A fixture and A child negative control are authorized.\n', 0o600)
        f.save_once(root/'RUN-CONTRACT.json', dict(schema='private-native-hook-host-run-v1',
            run_id=os.environ['GITHUB_RUN_ID'], run_attempt=os.environ['GITHUB_RUN_ATTEMPT'], fixture_only=True,
            business_controller_migration=False, source_manifest_sha256=self.args['reader_manifest_sha256'],
            native_pin_sha256=self.pin_sha, fixed_app_path=self.proof['app'], fixed_cli_sha256=self.proof['pin']['cli_sha256'],
            external_native_admission_sha256=self.args['native_admission_sha256'],
            external_fixed_app_admission_sha256=self.args['fixed_proof_sha256']))
        self.env = {k:os.environ[k] for k in ('GITHUB_ACTIONS','GITHUB_REPOSITORY','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','RUNNER_TEMP','RUNNER_ENVIRONMENT')}
        self.env.update(HOME=str(self.launch_root), CODEX_HOME=str(home), CODEX_CLIENT_THREAD_OBSERVER='1',
            PATH=str(Path(self.python).parent)+':/usr/bin:/bin:/usr/sbin:/sbin', SHELL='/bin/zsh', LANG='en_US.UTF-8',
            TMPDIR=str(root), CMUX_SOCKET_PATH=str(self.gui.socket), NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
        before, before_sha = capture_private_sources(root, home, 'BEFORE')
        f.need(before['complete'], 'private prelaunch snapshot incomplete')
        self.launch = dict(private_source_before_sha256=before_sha,
            argv=[str(self.binary),'--no-daemon','--no-alt-screen','-C',str(self.project),provider.prompt], environment=self.env,
            config_sha256=f.sha(config.encode()), hooks_sha256=f.sha(f.strict_file(home/'hooks.json')))
        self.launch_sha = f.save_once(root/'LAUNCH.json', self.launch)

    def create(self):
        before = json.loads(self.gui.cli_call(['--json','--id-format','both','tree','--all']).stdout)
        ids = {str(uuid.UUID(w['id'])).upper() for win in before.get('windows',[]) for w in win.get('workspaces',[])}
        cmd = ['--json','--id-format','both','workspace','create','--name','private-duplex-'+self.label+'-'+self.provider.nonce[:8],
               '--cwd',str(self.project),'--focus','false','--command',shlex.join([self.python,'-BE',str(HERE/'native_surface.py'),
                    '--root',str(self.root),'--launch-sha256',self.launch_sha])]
        for k,v in self.env.items(): cmd += ['--env', k+'='+v]
        f.save_once(self.root/'WORKSPACE-CREATE-INTENT.json', dict(argv=cmd,invocations=1,retries=0))
        reply = self.gui.cli_call(cmd, timeout=20)
        f.save_once(self.root/'WORKSPACE-CREATE-REPLY.json', dict(stdout=reply.stdout,stderr=reply.stderr,returncode=reply.returncode))
        tree = json.loads(self.gui.cli_call(['--json','--id-format','both','tree','--all']).stdout)
        self.workspace = workspace_from_reply(reply,tree,ids)

    def observe(self):
        self.gui.stable_peer(); self.alias.check()
        f.need(not self.provider.errors, 'provider failure: '+str(self.provider.errors))
        spawned = self.root/'NATIVE-SPAWN-INTENT.json'
        if self.client is None and spawned.is_file():
            row = json.loads(f.strict_file(spawned))
            observed = native.process(row['pid'])
            f.need(observed['birth'] == row['birth'], 'original launcher generation changed')
            if observed['executable'] == str(self.binary):
                f.need(observed['argv'] == self.launch['argv'], 'actual TUI argv differs')
                f.need(len(os.fsencode(self.home/'credential-observations/compaction-recovery'/('c-%s-00000000.sock'%row['pid']))) < 104,
                       'actual native PID exceeds socket length bound')
                argv, env = KernelScope.arguments(row['pid'])
                f.need(all(env.get(k) == v for k,v in self.env.items()) and env['CMUX_WORKSPACE_ID'].upper() == self.workspace,
                       'actual actor environment differs')
                self.client = dict(pid=row['pid'],birth=row['birth'],executable=str(self.binary))
                self.provider.client = self.client
                f.save_once(self.root/'CLIENT.json', dict(**self.client,argv=argv,environment={k:env[k] for k in self.env},
                    surface=env['CMUX_SURFACE_ID'],workspace=env['CMUX_WORKSPACE_ID'],tty=row['tty']))
        if self.client is not None:
            f.need(KernelScope.birth(self.client['pid']) == self.client['birth'], 'original TUI exited early')
            path = self.home/'credential-observations/compaction-recovery'/('client-%s-control.json'%self.client['pid'])
            if not self.provider.ready.is_set() and path.is_file():
                cap = json.loads(f.strict_file(path))
                f.need(cap['pid'] == self.client['pid'] and cap['birth'] == self.client['birth'], 'native generation changed')
                f.save_once(self.root/'NATIVE-CAPABILITY.json',cap)
                self.provider.ready.set()

    def completed(self):
        if not self.provider.done.is_set(): return False
        values = histories(self.root)
        pairs = [(self.provider.root_sid, 'AUTOHOOK_FIXTURE_DONE:'+self.provider.nonce)]
        if self.label == 'A': pairs += [(self.provider.child_sid, 'AUTOHOOK_CHILD_DONE:'+self.provider.nonce)]
        return all(sid in values and any(r.get('type') == 'event_msg' and r.get('payload',{}).get('type') == 'task_complete'
            and r['payload'].get('last_agent_message') == marker for r in values[sid][2]) for sid,marker in pairs)

    def freeze(self):
        provider, root = self.provider, self.root
        values = histories(root)
        wanted = {provider.root_sid} | ({provider.child_sid} if self.label == 'A' else set())
        f.need(set(values) == wanted and len(list(root.glob('host-hook-*'))) == (5 if self.label == 'A' else 4),
               'unexpected native SID or automatic hook count')
        result = []
        for sid, (path, raw, records, meta) in values.items():
            role = 'root' if sid == provider.root_sid else 'child'
            f.need(raw.endswith(b'\n'), 'unfinished native record')
            if role == 'root':
                f.need(meta.get('parent_thread_id') is None and isinstance(meta.get('source'),str), 'actual second root required')
            else:
                f.need(meta.get('parent_thread_id') == provider.root_sid
                       and meta.get('source',{}).get('subagent',{}).get('thread_spawn',{}).get('parent_thread_id') == provider.root_sid,
                       'original child parent metadata differs')
            done_marker = ('AUTOHOOK_FIXTURE_DONE:' if role == 'root' else 'AUTOHOOK_CHILD_DONE:')+provider.nonce
            completed = [r['payload'] for r in records if r.get('type') == 'event_msg'
                         and r.get('payload',{}).get('type') == 'task_complete']
            f.need(len(completed) == 1 and completed[0].get('last_agent_message') == done_marker,
                   'one original successful native task completion required')
            pairs = []
            for step, issued in provider.steps.items():
                if issued['sid'] != sid: continue
                accepted = provider.acceptances[step]
                item = issued['item']; key = 'arguments' if item['type'] == 'function_call' else 'input'
                begin = [(i,r['payload']) for i,r in enumerate(records) if r.get('type') == 'response_item'
                         and r.get('payload',{}).get('call_id') == item['call_id'] and r['payload'].get('type') == item['type']]
                end_type = 'function_call_output' if item['type'] == 'function_call' else 'custom_tool_call_output'
                end = [(i,r['payload']) for i,r in enumerate(records) if r.get('type') == 'response_item'
                       and r.get('payload',{}).get('call_id') == item['call_id'] and r['payload'].get('type') == end_type]
                commands = [(i,r['payload']) for i,r in enumerate(records) if r.get('type') == 'event_msg'
                            and r.get('payload',{}).get('type') == 'item_completed'
                            and r['payload'].get('item',{}).get('type') == 'CommandExecution'
                            and r['payload']['item'].get('id') == accepted['native_call_id']]
                f.need(len(begin) == len(end) == len(commands) == 1
                       and begin[0][0] < commands[0][0] < end[0][0]
                       and all(begin[0][1].get(k) == item.get(k) for k in ('name','namespace',key))
                       and issued['marker'] in str(end[0][1].get('output')), 'native full call/output pair incomplete')
                event = commands[0][1]; command = event['item']
                f.need(event['thread_id'] == sid and command['status'] == 'completed' and command['exit_code'] == 0
                       and command['command'][-1] == issued['command'] and issued['marker'] in command['aggregated_output'],
                       'actual native ToolCtx command/output differs')
                if item['type'] == 'function_call': f.need(item['call_id'] == command['id'], 'direct call identity differs')
                post = json.loads(f.pinned(root/accepted['hook_path'], accepted['hook_sha256']))
                f.need(post['native_origin']['reply']['origin']['turnId'] == event['turn_id'], 'hook/native turn mismatch')
                pairs.append(dict(step=step,outer_call_id=item['call_id'],native_call_id=command['id'],
                                  begin_line=begin[0][0]+1,command_line=commands[0][0]+1,output_line=end[0][0]+1))
            actual_commands = [r['payload']['item']['id'] for r in records if r.get('type') == 'event_msg'
                and r.get('payload',{}).get('type') == 'item_completed'
                and r['payload'].get('item',{}).get('type') == 'CommandExecution']
            f.need(len(actual_commands) == len(pairs) and set(actual_commands) == {v['native_call_id'] for v in pairs},
                   'unaccounted actual native command')
            if role == 'root' and self.label == 'A':
                spawn = provider.spawn_item
                issued_spawn = [r['payload'] for r in records if r.get('type') == 'response_item'
                    and r.get('payload',{}).get('type') == 'function_call' and r['payload'].get('call_id') == spawn['call_id']]
                spawn_output = [r['payload'] for r in records if r.get('type') == 'response_item'
                    and r.get('payload',{}).get('type') == 'function_call_output' and r['payload'].get('call_id') == spawn['call_id']]
                activity = [r['payload']['item'] for r in records if r.get('type') == 'event_msg'
                    and r.get('payload',{}).get('type') == 'item_completed'
                    and r['payload'].get('item',{}).get('type') == 'SubAgentActivity'
                    and r['payload']['item'].get('id') == spawn['call_id']]
                f.need(len(issued_spawn) == len(spawn_output) == len(activity) == 1
                    and all(issued_spawn[0].get(k) == spawn.get(k) for k in ('name','namespace','arguments'))
                    and provider.child_sid in str(spawn_output[0])
                    and activity[0]['agent_thread_id'] == provider.child_sid
                    and activity[0]['agent_path'] == '/root/caller_identity_fixture', 'original child spawn trace incomplete')
            f.write_new(root/(role+'-native.jsonl'),raw,0o600)
            result.append(dict(role=role,sid=sid,metadata=meta,bytes=len(raw),sha256=f.sha(raw),pairs=pairs))
        before = json.loads(f.pinned(root/'PRIVATE-SOURCE-BEFORE.json',self.launch['private_source_before_sha256']))
        after, unused_after_sha = capture_private_sources(root,self.home,'AFTER',before)
        f.need(after['complete'] and after['all_match_before'], 'complete private source equality failed')
        f.need(f.sha(f.strict_file(self.home/'config.toml')) == self.launch['config_sha256']
               and f.sha(f.strict_file(self.home/'hooks.json')) == self.launch['hooks_sha256'], 'actor config drifted')
        return result

    def cleanup(self, deadline):
        result = {}
        if self.client is None and (self.root/'NATIVE-SPAWN-INTENT.json').is_file():
            try:
                row = json.loads(f.strict_file(self.root/'NATIVE-SPAWN-INTENT.json'))
                observed = native.process(row['pid'])
                if observed['birth'] == row['birth'] and observed['executable'] == str(self.binary):
                    self.client = dict(pid=row['pid'],birth=row['birth'],executable=str(self.binary))
                    f.save_once(self.root/'CLEANUP-RECONCILED-NATIVE.json',self.client)
            except native.ProcessExited: pass
            except BaseException as error: result['reconcile'] = dict(passed=False,error=str(error))
        try: result['native'] = terminate_native(self.root,self.client)
        except BaseException as error: result['native'] = dict(passed=False,error=str(error))
        if self.workspace is not None:
            try: result['workspace'] = close_workspace(self.root,self.gui,self.alias,self.workspace,deadline)
            except BaseException as error: result['workspace'] = dict(passed=False,error=str(error))
        f.save_once(self.root/'TRACKED-CLEANUP.json',result)
        return result


def execute(root,args):
    root = f.require_runner(root)
    proof = json.loads(f.pinned(root/'FIXED-APP-ADMISSION.json',args['fixed_proof_sha256']))
    exchange = Exchange(root)
    actors, cleanup, records = [], {}, {}
    failure = None; started = None
    with OwnedAlias(root,proof['app']) as alias:
        with OwnedFixedOne(root,proof) as gui:
            with ExitStack() as stack:
                try:
                    for label,directory in (('A','x'),('B','y')):
                        actor = Actor(root/directory,label,args,proof,gui,alias,exchange)
                        actors.append(actor)
                        stack.enter_context(actor.provider)
                        actor.prepare()
                    started = time.monotonic(); deadline = started+70
                    for actor in actors: actor.create()
                    while time.monotonic() < deadline:
                        for actor in actors: actor.observe()
                        if all(actor.completed() for actor in actors): break
                        time.sleep(.1)
                    f.need(all(actor.completed() for actor in actors), 'original duplex attempt incomplete; no retry')
                    pair, pair_sha = exchange.freeze_pair(); p.endpoints(pair)
                    for actor in actors: records[actor.label] = actor.freeze()
                    f.save_once(root/'HOST-ENDPOINT-FINAL.json',dict(peer=gui.stable_peer(),
                        tree=json.loads(gui.cli_call(['--json','--id-format','both','tree','--all']).stdout)))
                except BaseException as error:
                    failure = dict(type=type(error).__name__,error=str(error),replay_allowed=False)
                    f.save_once(root/'FAILURE.json',failure)
                finally:
                    began = time.monotonic(); limit = min(began+50,started+120 if started else began+50)
                    f.save_once(root/'CLEANUP-BUDGET.json',dict(started_monotonic=began,deadline_monotonic=limit,
                        original_task_started_monotonic=started,total_task_limit_seconds=120,cleanup_limit_seconds=50))
                    for actor in actors: actor.provider.stopping.set()
                    for actor in actors: cleanup[actor.label] = actor.cleanup(limit)
                    f.save_once(root/'TRACKED-CLEANUP.json',cleanup)
            if failure: raise RuntimeError(failure['error'])
            f.need(set(cleanup) == {'A','B'} and all(v.get('native',{}).get('original_gone') is True
                and v.get('workspace',{}).get('passed') is True for v in cleanup.values()), 'original owned resources incomplete')
    f.need(not Path('/Applications/cmux.app').exists() and not Path('/Applications/cmux.app').is_symlink(), 'owned alias remains')
    elapsed = time.monotonic()-started
    f.need(elapsed <= 120, 'duplex task and cleanup exceeded original 120 seconds')
    f.save_once(root/'RESULT.json',dict(schema='real-cmux-duplex-body-runtime-v1',
        passed=True,scope='isolated-two-root-A-to-B-to-A-context-and-complete-body',
        actual_native_roots=2,automatic_post_hooks=9,native_history=records,pair_sha256=exchange.pair_sha,
        child_root_inbox_refused=True,full_native_host_reader_verified=True,context_emission_verified=True,
        body_read_verified=True,bidirectional_receipt_verified=True,native_user_receipt_verified=False,
        live_business_hook_verified=False,physical_power_loss_verified=False,mailbox_writes=2,terminal_inputs=0,
        mailbox_claims=0,mailbox_mark_read=0,task_acceptance=False,source_tests_rerun=0,
        supplier_requests=0,production_changes=0,task_and_cleanup_seconds=elapsed))


def emergency(root):
    """Preserve outer failure; signal only original recorded private generations."""
    result = {}
    for label,directory in (('A','x'),('B','y')):
        base = root/directory
        path = base/'CLIENT.json'
        if not path.is_file():
            result[label] = dict(status='UNKNOWN_NO_ORIGINAL_IDENTITY_NO_SIGNAL'); continue
        try:
            row = json.loads(f.strict_file(path)); identity = {k:row[k] for k in ('pid','birth','executable')}
            f.need(identity['executable'] == str(base/'bin/codex'), 'foreign emergency native path')
            result[label] = terminate_native(base,identity,'DUPLEX-EMERGENCY-NATIVE')
        except BaseException as error: result[label] = dict(status='FAILED_NO_BROAD_SIGNAL',error=str(error))
    # Original GUI/alias emergency code remains unchanged and is never acceptance.
    from emergency_cleanup import reconcile
    result['original_gui_reconciliation'] = reconcile(root)
    if all(result[k].get('original_gone') is True for k in ('A','B')) \
            and result['original_gui_reconciliation'].get('gui',{}).get('original_gone') is True:
        alias = Path('/Applications/cmux.app')
        if alias.is_symlink():
            original = json.loads(f.strict_file(root/'ALIAS-CREATED.json'))['identity']
            info = alias.lstat()
            f.need([info.st_dev,info.st_ino,os.readlink(alias)] == original, 'foreign emergency alias')
            alias.unlink(); result['original_alias_removed'] = True
    f.save_once(root/'DUPLEX-EMERGENCY-CLEANUP.json',result)
    return result
