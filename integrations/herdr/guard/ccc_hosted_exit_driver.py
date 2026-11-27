"""Original guard adapter for a registered future child host.

There is no scheduler or host discovery here. The original production loop
supplies its bound endpoint/pane/host client under the existing owner lock.
Only hosted_process exit replaces the unavailable shell-editor admission;
all original selected-SID, error, Goal, configuration and intent checks stay.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from ccc_herdr_exit_context import PinnedDirectory,canonical,need
from ccc_original_exit_driver import ExitRecoveryDriver,fresh_plan
from ccc_original_exit_boundary import select_context,generation_state,assess,OriginalExitAttempts,digest
from ccc_original_current_inputs import current_inputs
from ccc_original_shell_boundary import no_writer
import ccc_original_writer_check as writer


class HostedExitRecoveryDriver(ExitRecoveryDriver):
    def prepare_hosted(self,guard,endpoint,pane,terminal,client,*,publish=True):
        policy=self.enabled(guard,endpoint)
        view=client.call({'op':'observe'})
        need(view.get('identity',{}).get('owner')==self.owner,'host_not_owned_by_original_controller')
        need(view.get('phase')=='awaiting_original_controller' and view.get('child') is None,
             'original_child_host_not_waiting')
        exited=view['last_exit'];child=exited['child']
        sha,doc=select_context(self.state,guard.guard_config['original_stores'],terminal['binding'],
                               expected_generation={k:child[k] for k in ('pid','birth')})
        generation=generation_state(guard.scope,doc['binding']['pid'],doc['binding']['birth'])
        history=self.transport.r40_original_history(doc['history']['path'],doc['session_id'])
        goal=self.transport.r40_original_goal(str(Path(doc['configuration_evidence']['database_locator']['path']).parent),doc['session_id'])
        admission=assess(doc,terminal,generation,history,goal,
                         policy={'resume_original_after_error_exit':True},owned_host=view)
        need(admission['phase']=='exit_verified',admission['reason'])
        ledger=json.loads((self.state/'herdr-codex-input-ledger.json').read_bytes())
        for entry in ledger['attempts'].values():
            if entry.get('source',{}).get('turn',{}).get('session_id')!=doc['session_id']:continue
            unsent=(entry.get('phase')=='prewrite_refused' and entry.get('input_operations')==0
                    and entry.get('wire_requests')==0)
            need(entry.get('phase')=='native_received' or unsent,'original_input_attempt_unresolved')
        plan=fresh_plan(doc);inputs=current_inputs(doc,plan);absence=no_writer(doc,writer)
        request_plan=dict(session_id=doc['session_id'],binding=doc['binding'],
                          source_event=history['last_native_event'],history_identity=history['file_id'],
                          actuation_kind='owned_child_host')
        key=OriginalExitAttempts.attempt_key(request_plan)
        capsule=dict(schema='ccc-original-hosted-launch-capsule-v1',attempt_key=key,document=doc,
            context_sha256=sha,plan=plan,current_inputs=inputs,host=view['identity'],host_exit=exited,
            owner=self.owner,policy=policy,goal=goal,source_event=history['last_native_event'],
            expected_history_fence=history['fence'])
        need(client.call({'op':'observe'})==view,'original_host_changed_during_admission')
        again=self.transport.terminal_program_observation(guard,endpoint,pane,terminal['binding']['server'])
        # Sampling time necessarily advances. Every identity, phase and native
        # process field must still match; no other field is discarded.
        stable=lambda value:{k:v for k,v in value.items() if k!='observed_at'}
        need(stable(again)==stable(terminal),'original_host_terminal_changed_during_admission')
        self.owner_recheck()
        raw=canonical(capsule)+b'\n';cap_sha=hashlib.sha256(raw).hexdigest()
        if publish:
            with PinnedDirectory(self.directory) as directory:directory.publish(cap_sha+'.capsule.json',raw)
        request_plan.update(capsule=str(self.directory/(cap_sha+'.capsule.json')),capsule_sha256=cap_sha,
                            host_exit_id=exited['exit_id'],host_identity=view['identity'],
                            original_writer_observation=absence)
        return request_plan,capsule

    def validate_hosted(self,guard,endpoint,pane,terminal,client,plan,capsule):
        current,again=self.prepare_hosted(guard,endpoint,pane,terminal,client,publish=False)
        need(current==plan and again==capsule,'original_hosted_admission_changed')
        return {'ready':True,'terminal_input_operations':0,'capsule_sha256':plan['capsule_sha256']}

    def run_hosted(self,guard,endpoint,pane,terminal,client,*,reconcile,provider_admission=None,on_submit=None):
        """One finite original-controller action, invoked by its existing loop.

        Caller creates the pinned original recovery directory using the same
        parent-directory admission as the R2 driver. This adapter never opens
        an independent empty ledger. reconcile must supply real native proof;
        a host response alone can only remain awaiting_native_receipt.
        """
        self.owner_recheck()
        need(callable(provider_admission),'original_provider_budget_adapter_unavailable')
        plan,capsule=self.prepare_hosted(guard,endpoint,pane,terminal,client)
        request={'op':'resume','attempt_key':OriginalExitAttempts.attempt_key(plan),
            'exit_id':plan['host_exit_id'],'capsule':plan['capsule'],'sha256':plan['capsule_sha256']}
        def validate(p):
            checked=self.validate_hosted(guard,endpoint,pane,terminal,client,p,capsule)
            need(provider_admission(p,reserve=False).get('allowed') is True,'original_provider_budget_refused')
            return checked
        def submit(p,record):
            if on_submit is not None:on_submit()
            def before_write():
                validate(p)
                # Adapter must use the ORIGINAL shared provider ledger and
                # bind its durable reservation to this existing attempt key.
                # It cannot refund/replay if subsequent delivery is unknown.
                budget=provider_admission(p,reserve=True)
                need(budget.get('allowed') is True and budget.get('durable_reservation') is True
                     and budget.get('original_attempt_key')==request['attempt_key'],
                     'original_provider_reservation_unverified')
            reply=client.call(request,before_write=before_write)
            need(reply.get('phase')=='exec_released' and reply.get('new_launches')==1
                 and reply.get('shell_input_operations')==0,'original_host_submission_unknown')
            return {'payload_sent':True,'input_operations':0,'process_launches':1,'host':reply}
        return OriginalExitAttempts(self.directory,owner_recheck=self.owner_recheck).run(
            plan,validate=validate,submit=submit,reconcile=reconcile)

    def host_launch_evidence(self,record,client):
        """Lookup can reconcile a lost response; it never submits another launch."""
        plan=record['plan'];key=record['attempt_key']
        reply=client.call({'op':'lookup','attempt_key':key})
        need(reply.get('identity')==plan['host_identity'],'original_host_lookup_identity_changed')
        attempt=reply.get('attempt')
        if not isinstance(attempt,dict) or not attempt.get('child'):return None
        with PinnedDirectory(self.directory) as directory:
            raw=directory.read(Path(plan['capsule']).name)
        need(raw is not None and hashlib.sha256(raw).hexdigest()==plan['capsule_sha256'],
             'original_host_lookup_capsule_changed')
        capsule=json.loads(raw)
        need(attempt.get('plan_sha256')==digest(capsule['plan'])
             and attempt['child'].get('launch_attempt_key')==key,'original_host_launch_plan_changed')
        return {'child':attempt['child'],'host_phase':attempt['phase'],
                'native_resume_verified':False,'same_sid_native_receipt':False}

    def reconcile_hosted(self,snapshot,record,client):
        evidence=self.host_launch_evidence(record,client)
        if evidence is None:return None
        with PinnedDirectory(self.directory) as directory:
            raw=directory.read(Path(record['plan']['capsule']).name)
        need(raw is not None and hashlib.sha256(raw).hexdigest()==record['plan']['capsule_sha256'],
             'original_host_receipt_capsule_changed')
        capsule=json.loads(raw)
        return self.receipt_for_launch(snapshot,record,capsule,evidence['child'])

    def tick_registered(self,guard,snapshot,registrations):
        """One pass of the ORIGINAL owner loop over explicitly registered hosts.

        Registration carries the original terminal binding and authenticated
        client, never a title search or a new process chosen by modification
        time. Pending attempts are reconciled BEFORE asking for another exit;
        this also works while the successfully resumed child is still running.
        No host registration means no adoption of an existing business agent.
        """
        from ccc_hosted_provider_budget import OriginalHostedProviderBudget
        result=dict(mode='original_hosted_exit_recovery',input_operations=0,
                    process_launches_upper_bound=0,native_resumes=0,held=[],phase='observed')
        self.owner_recheck()
        need(isinstance(registrations,(list,tuple)) and len(registrations)<=128,
             'original_host_registry_invalid')
        need(callable(self.input_scope),'original_input_locks_unavailable')
        if not registrations:return result
        stores=guard.guard_config['original_stores']
        with PinnedDirectory(self.state,expected=stores['state_identity'],forbidden_mode=0o022) as parent:
            try:os.mkdir(self.directory.name,0o700,dir_fd=parent.fd)
            except FileExistsError:pass
            parent.check()
        attempts=OriginalExitAttempts(self.directory,owner_recheck=self.owner_recheck)
        for registered in registrations:
            binding=registered.get('binding',{})
            try:
                endpoints=[e for e in guard.config['endpoints'] if all(e.get(k)==binding.get(k)
                    for k in ('session','cmux_surface_id','cmux_workspace_id'))
                    and e['socket']+'#'+binding['pane_id']==binding.get('key')]
                need(len(endpoints)==1,'original_host_endpoint_unbound')
                endpoint=endpoints[0];self.enabled(guard,endpoint)
                items=[r for r in snapshot.get('panel_inventory',[]) if r.get('session')==binding['session']
                       and r.get('pane_id')==binding['pane_id'] and r.get('terminal_id')==binding['terminal_id']]
                need(len(items)==1,'original_host_terminal_unbound')
                terminal=items[0].get('native_terminal_observation') or {}
                need(terminal.get('binding')==binding,'original_host_binding_changed')
                panes=[p for p in guard.pane_snapshots[binding['session']]['panes']
                       if p['pane_id']==binding['pane_id'] and p['terminal_id']==binding['terminal_id']]
                need(len(panes)==1,'original_host_pane_unbound')
                pane=panes[0];client=registered['client']
                view=client.call({'op':'observe'})
                need(view.get('identity')==registered.get('host_identity')
                     and view['identity'].get('owner')==self.owner,'original_registered_host_changed')
                with self.input_scope(endpoint,pane,terminal):
                    self.owner_recheck()
                    with PinnedDirectory(self.directory) as directory:
                        names=os.listdir(directory.fd)
                        need(len(names)<=2048,'original_exit_store_capacity_attention')
                        records=[json.loads(directory.read(n)) for n in names
                                 if len(n)==69 and n.endswith('.json')]
                    pending=False
                    for record in records:
                        plan=record.get('plan',{})
                        if plan.get('actuation_kind')!='owned_child_host':continue
                        if plan.get('host_identity')!=view['identity']:continue
                        if record['phase'] in {'native_resumed','native_work_observed','refused_before_input'}:continue
                        updated=attempts.run(plan,validate=lambda p:None,submit=lambda p,r:None,
                            reconcile=lambda r:self.reconcile_hosted(snapshot,r,client))
                        if updated['phase'] in {'native_resumed','native_work_observed'}:
                            result['native_resumes']+=1
                        else:pending=True
                    if view.get('waiting_input_blocked') is True:
                        result['held'].append(dict(key=binding['key'],reason='waiting_user_input_preserved',
                                                   attention_required=True))
                        continue
                    if pending:
                        result['held'].append(dict(key=binding['key'],reason='original_host_input_pending; no replay'))
                        continue
                    if view.get('phase')!='awaiting_original_controller':continue
                    need(stores['provider_db']==str(Path(guard.guard_config['ccc_config']).parent/
                                                    'codex-provider-retry.sqlite3'),
                         'original_registered_provider_store_changed')
                    budget=OriginalHostedProviderBudget(guard,endpoint,self.state,
                        ledger_identity=stores['provider_identity'],owner_recheck=self.owner_recheck)
                    def mark_submit():
                        result['process_launches_upper_bound']=1
                    record=self.run_hosted(guard,endpoint,pane,terminal,client,
                        reconcile=lambda r:self.reconcile_hosted(snapshot,r,client),
                        provider_admission=budget,on_submit=mark_submit)
                    result['phase']=record['phase']
                    if result['process_launches_upper_bound']:break
            except (OSError,ValueError,KeyError,TypeError,RuntimeError,StopIteration,
                    sqlite3.Error,subprocess.SubprocessError) as error:
                result['held'].append(dict(key=binding.get('key'),reason=str(error)[:200]))
                if result['process_launches_upper_bound']:
                    result['phase']='attention';break
        return result
