"""Original guard integration; one original terminal launch at most per pass.

Requires the original owner lock, existing CCC policy and an explicitly enabled
same-session exit-recovery rule. This module never creates an observer/server,
changes the Goal, resets retry budgets or substitutes a different session.
"""
import sys
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

from ccc_herdr_exit_context import PinnedDirectory, canonical, need
from ccc_original_exit_boundary import (select_context, generation_state, assess,
                                        OriginalExitAttempts, digest)
from ccc_original_resume_inputs import normalized_plan
from ccc_original_current_inputs import current_inputs
from ccc_original_shell_boundary import inspect_shell, no_writer
from ccc_resume_metadata import read_metadata
from ccc_thread_settings_evidence import read_settings
import ccc_original_writer_check as writer


def settings_after_birth(timestamp,birth):
    """A same-second settings event may still predate this exact process."""
    try:
        moment=datetime.datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        if moment.tzinfo is None or moment.utcoffset() is None:return False
        if (not isinstance(birth,list) or len(birth)!=2
                or any(type(x) is not int for x in birth)
                or birth[0]<=0 or not 0<=birth[1]<1000000):return False
        born=datetime.datetime.fromtimestamp(birth[0],datetime.timezone.utc)+datetime.timedelta(microseconds=birth[1])
        return moment>=born
    except (ValueError,TypeError,AttributeError,OverflowError):
        return False


def fresh_plan(document):
    settings=read_settings(document)
    metadata=read_metadata(document,document['configuration_evidence']['database_locator'])
    return normalized_plan(document,settings,metadata)


class ExitRecoveryDriver:
    def __init__(self, state, service_release, *, owner_recheck, owner_identity, transport,
                 input_scope=None, original_launch_sender=None):
        self.state=Path(state);self.directory=self.state/'original-exit-recovery'
        self.root=Path(service_release);self.owner_recheck=owner_recheck
        self.owner=dict(owner_identity,service_release=str(self.root));self.transport=transport
        # Supplied by the original controller, never a replacement lock or an
        # independent sender. No adapter means observation only.
        self.input_scope=input_scope
        # Existing R5 request() accepts agent.prompt, not pane.send_input.
        # Require an explicitly supplied original-controller adapter; do not
        # broaden that allowlist or consume intent on a known unsupported call.
        self.original_launch_sender=original_launch_sender

    def enabled(self,guard,endpoint):
        self.owner_recheck()
        config_path=Path(guard.guard_config['ccc_config'])
        raw=config_path.read_bytes();config=json.loads(raw)
        enabled,_=self.transport.policy(config,endpoint)
        need(guard.guard_config.get('armed') is True and config.get('mode')=='armed'
             and enabled and endpoint.get('discovered_read_only') is not True,
             'original_continuation_policy_disabled')
        need(guard.guard_config.get('original_exit_recovery',{}).get('resume_original_after_error_exit') is True,
             'original_exit_recovery_policy_disabled')
        return dict(path=str(config_path),sha256=hashlib.sha256(raw).hexdigest())

    def prepare(self,guard,endpoint,pane,terminal,*,publish=True):
        policy=self.enabled(guard,endpoint)
        sha,doc=select_context(self.state,guard.guard_config['original_stores'],terminal['binding'])
        generation=generation_state(guard.scope,doc['binding']['pid'],doc['binding']['birth'])
        need(generation['state']=='exited',generation['reason'])
        history=self.transport.r40_original_history(doc['history']['path'],doc['session_id'])
        goal=self.transport.r40_original_goal(str(Path(doc['configuration_evidence']['database_locator']['path']).parent),doc['session_id'])
        state=assess(doc,terminal,generation,history,goal,policy={'resume_original_after_error_exit':True})
        need(state['phase']=='exit_verified',state['reason'])
        # Do not create a second recovery path around an unresolved original
        # Codex input. A known zero-input prewrite refusal remains distinct.
        path=self.state/'herdr-codex-input-ledger.json'
        ledger=json.loads(path.read_bytes())
        for entry in ledger['attempts'].values():
            if entry.get('source',{}).get('turn',{}).get('session_id')!=doc['session_id']:continue
            known_unsent=(entry.get('phase')=='prewrite_refused' and entry.get('input_operations')==0
                          and entry.get('wire_requests')==0)
            need(entry.get('phase')=='native_received' or known_unsent,'original_input_attempt_unresolved')
        plan=fresh_plan(doc);inputs=current_inputs(doc,plan)
        absence=no_writer(doc,writer)
        shell=inspect_shell(guard,self.transport,endpoint,pane,terminal)
        need(shell.get('editor_empty_verified') is True,'shell_editor_phase_unavailable')
        event=history['last_native_event']
        request_plan=dict(session_id=doc['session_id'],binding=doc['binding'],
            source_event=event,history_identity=history['file_id'])
        key=OriginalExitAttempts.attempt_key(request_plan)
        capsule=dict(schema='ccc-original-exit-launch-capsule-v1',attempt_key=key,
            document=doc,context_sha256=sha,plan=plan,current_inputs=inputs,shell=shell,
            owner=self.owner,policy=policy,goal=goal,source_event=event,
            expected_history_fence=history['fence'])
        raw=canonical(capsule)+b'\n';cap_sha=hashlib.sha256(raw).hexdigest()
        if publish:
            with PinnedDirectory(self.directory) as directory:
                directory.publish(cap_sha+'.capsule.json',raw)
        request_plan.update(capsule=str(self.directory/(cap_sha+'.capsule.json')),capsule_sha256=cap_sha,
            original_writer_observation=absence,original_shell_observation=shell)
        return request_plan,capsule

    def validate(self,guard,endpoint,pane,terminal,plan,capsule):
        # Redo all mutable admission checks before the original transport's
        # sole write. The capsule and original attempt are never regenerated
        # after a possible send.
        current,again=self.prepare(guard,endpoint,pane,terminal,publish=False)
        need(current==plan and again==capsule,'original_exit_admission_changed')
        return {'ready':True,'input_cas_available':False,'capsule_sha256':plan['capsule_sha256']}

    def submit(self,guard,endpoint,pane,terminal,plan,capsule):
        command=shlex.join([sys.executable,'-BE',
            str(self.root/'ccc_original_launch.py'),'--capsule',plan['capsule'],'--sha256',plan['capsule_sha256']])
        need(len(command.encode())<=700 and not any(c in command for c in '\n\r\t\x1b'),
             'original_launch_command_outside_wire_bound')
        def before_write():
            self.validate(guard,endpoint,pane,terminal,plan,capsule)
            self.owner_recheck()
        need(callable(self.original_launch_sender),'original_exit_transport_unavailable')
        response=self.original_launch_sender(endpoint,terminal['binding']['server'],'pane.send_input',
            {'pane_id':pane['pane_id'],'text':command,'keys':['enter']},guard.scope,before_write=before_write)
        need('error' not in response and isinstance(response.get('result'),dict),
             'original_launch_host_response_not_success')
        return dict(payload_sent=True,input_operations=1,response=response)

    def reconcile(self,guard,snapshot,record):
        plan=record['plan'];key=record['attempt_key']
        with PinnedDirectory(self.directory) as directory:
            raw=directory.read(key+'.exec.json')
            capsule_raw=directory.read(Path(plan['capsule']).name)
        if raw is None:return None
        launch=json.loads(raw);capsule=json.loads(capsule_raw)
        need(hashlib.sha256(capsule_raw).hexdigest()==plan['capsule_sha256']
             and launch.get('launch_attempt_key')==key
             and launch.get('session_id')==plan['session_id']
             and launch.get('plan_sha256')==digest(capsule['plan'])
             and launch.get('shell')==capsule['shell']
             and launch.get('phase')=='exec_intent', 'original_launch_evidence_changed')
        return self.receipt_for_launch(snapshot,record,capsule,launch)

    def receipt_for_launch(self,snapshot,record,capsule,launch):
        """Native resume proof common to the original shell and owned host."""
        plan=record['plan'];key=record['attempt_key']
        matches=[r for r in snapshot.get('agents',[]) if r.get('pid')==launch['pid']
                 and r.get('birth')==launch['birth'] and r.get('native_session_id')==plan['session_id']
                 and r.get('native_reader_available') is True and r.get('process_bound') is True
                 and all(r.get(k)==plan['binding'].get(k) for k in
                         ('session','pane_id','terminal_id','cmux_surface_id','cmux_workspace_id','server'))]
        if len(matches)!=1:return None
        row=matches[0];proof=row.get('native_selection_evidence') or {}
        if proof.get('schema')!='ccc-official-statusline-native-writer-v1':return None
        expected_binding=dict(capsule['document']['binding'],pid=launch['pid'],birth=launch['birth'])
        if proof.get('binding')!=expected_binding or proof.get('session_id')!=plan['session_id']:return None
        rollout=proof.get('rollout') or {}
        if [rollout.get('device'),rollout.get('inode')]!=plan['history_identity']:return None
        settings=read_settings(capsule['document'])
        if settings.get('status')!='PERSISTED_THREAD_SETTINGS_OBSERVED':return None
        values=settings['snapshot']['settings'];expected=capsule['plan']['expected_model_triple']
        if any(values.get(k)!=expected[v] for k,v in
               [('model','model'),('model_provider_id','provider'),('reasoning_effort','effort')]):return None
        if values['cwd']!=capsule['plan']['cwd']:return None
        if not settings_after_birth(settings['snapshot']['timestamp'],launch['birth']):return None
        return dict(same_sid_native_receipt=True,session_id=plan['session_id'],
            original_history_identity=plan['history_identity'],new_pid_birth_verified=True,
            launch_attempt_key=key,new_pid=launch['pid'],new_birth=launch['birth'],
            native_settings_sha256=settings['snapshot']['sha256'],
            natural_work_observed=False)

    def tick(self,guard,snapshot):
        result={'mode':'original_exit_recovery','input_operations':0,'held':[],
                'native_resumes':0,'business_restarts':0}
        try:
            return self._tick(guard,snapshot,result)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError) as error:
            # Preserve the original agents and continuation steps even if this
            # additional recovery store is corrupt or a local query times out.
            return dict(result,phase='attention',recovery_available=False,
                        reason=type(error).__name__+': '+str(error)[:200])

    def _tick(self,guard,snapshot,result):
        if guard.guard_config.get('original_exit_recovery',{}).get('resume_original_after_error_exit') is not True:
            return dict(result,phase='disabled')
        self.owner_recheck()
        with PinnedDirectory(self.state,expected=guard.guard_config['original_stores']['state_identity'],
                             forbidden_mode=0o022) as parent:
            try:os.mkdir(self.directory.name,0o700,dir_fd=parent.fd)
            except FileExistsError:pass
            parent.check()
        attempts=OriginalExitAttempts(self.directory,owner_recheck=self.owner_recheck)
        with PinnedDirectory(self.directory) as directory:
            names=os.listdir(directory.fd)
            need(len(names)<=512,'original_exit_store_capacity_attention')
            records=[json.loads(directory.read(n)) for n in names
                     if n.endswith('.json') and len(n)==69] # 64-hex attempt names
        for record in records:
            if record['phase'] in {'native_resumed','native_work_observed','refused_before_input'}:continue
            observed=attempts.run(record['plan'],validate=lambda p:None,submit=lambda p,r:None,
                reconcile=lambda r:self.reconcile(guard,snapshot,r))
            result['native_resumes']+=observed['phase']=='native_resumed'
        for item in snapshot.get('panel_inventory',[]):
            terminal=item.get('native_terminal_observation') or {}
            if terminal.get('phase')!='shell_foreground':continue
            try:
                endpoint=next(e for e in guard.config['endpoints'] if e['session']==item['session'])
                pane=next(p for p in guard.pane_snapshots[item['session']]['panes']
                          if p['pane_id']==item['pane_id'] and p['terminal_id']==item['terminal_id'])
                need(callable(self.input_scope),'original_input_locks_unavailable')
                need(callable(self.original_launch_sender),'original_exit_transport_unavailable')
                with self.input_scope(endpoint,pane,terminal):
                    plan,capsule=self.prepare(guard,endpoint,pane,terminal)
                    def submit(p,r):
                        # Set before calling transport. Persistence/owner
                        # failures afterward must not report zero input or
                        # permit another pane's send in this pass.
                        result.update(input_operations=1,
                            input_count_semantics='new_call_upper_bound_until_native_receipt')
                        return self.submit(guard,endpoint,pane,terminal,p,capsule)
                    record=attempts.run(plan,
                        validate=lambda p:self.validate(guard,endpoint,pane,terminal,p,capsule),
                        submit=submit,
                        reconcile=lambda r:self.reconcile(guard,snapshot,r))
                result.update(phase=record['phase'])
                break
            except (OSError,ValueError,KeyError,TypeError,RuntimeError,StopIteration,subprocess.SubprocessError) as error:
                result['held'].append({'key':item['key'],'reason':str(error)[:160]})
                if result['input_operations']:
                    result.update(phase='attention',recovery_available=False)
                    break
        return dict(result,phase=result.get('phase','observed'))
