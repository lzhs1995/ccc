"""Same-owner service assembly, using the paired original input mutex."""
from contextlib import contextmanager
from pathlib import Path
import os
import sys

from ccc_herdr_exit_context import need
from ccc_hosted_exit_driver import HostedExitRecoveryDriver
from ccc_original_host_registration import OriginalHostRegistry


class OriginalHostedService:
    def __init__(self,guard,root,*,owner_recheck,transport,control_scope,verify_sources):
        self.guard=guard;self.root=Path(root);self.recheck=owner_recheck
        self.transport=transport;self.control_scope=control_scope;self.verify_sources=verify_sources
        self.active_checks=[]
        self.owner=dict(pid=os.getpid(),birth=guard.scope.birth(os.getpid()),service_release=str(root))
        self.driver=HostedExitRecoveryDriver(Path(guard.guard_config['state']),self.root,
            owner_recheck=self.check,owner_identity={k:self.owner[k] for k in ('pid','birth')},
            transport=transport,input_scope=self.input_scope)
        self.registry=OriginalHostRegistry(Path(guard.guard_config['state']),self.root,
            owner_identity=self.owner,owner_recheck=self.check,
            entry_prefix=[str(Path(sys.executable).resolve()),'-BE',str(self.root/'ccc_original_host_entry.py')])

    def check(self):
        self.recheck();self.verify_sources()
        for check in self.active_checks:check()

    def placement(self,endpoint):
        member=self.guard.cmux_membership(endpoint).get(endpoint['cmux_surface_id'],{})
        need(member.get('type')=='terminal' and member.get('workspace_id')==endpoint['cmux_workspace_id']
             and member.get('surface_id')==endpoint['cmux_surface_id'] and member.get('pane_id'),
             'original_outer_input_placement_changed')
        return dict(workspace_uuid=member['workspace_id'],target_pane_uuid=member['pane_id'],
                    target_surface_uuid=member['surface_id'])

    @contextmanager
    def input_scope(self,endpoint,pane,terminal):
        self.check();identity=self.placement(endpoint)
        # Original CCC workspace lock followed by paired deliveries-v1
        # pane/target mutex. Same outer surface serializes its inner panels.
        with self.control_scope(dict(workspace_id=identity['workspace_uuid'],identity=identity)) as check:
            def check_input():
                check()
                need(self.placement(endpoint)==identity,'outer_input_moved_under_lock')
            self.active_checks.append(check_input)
            try:
                self.check();need(self.placement(endpoint)==identity,'outer_input_moved_under_lock')
                yield
                self.check()
            finally:self.active_checks.pop()

    def tick(self,snapshot):
        self.check()
        registrations,held=self.registry.collect(self.guard,snapshot)
        if not registrations:
            return dict(mode='original_hosted_exit_recovery',registered_hosts=0,
                        input_operations=0,process_launches_upper_bound=0,native_resumes=0,
                        phase='unregistered' if not held else 'held',held=held)
        result=self.driver.tick_registered(self.guard,snapshot,registrations)
        result['registered_hosts']=len(registrations);result['held'].extend(held)
        return result
