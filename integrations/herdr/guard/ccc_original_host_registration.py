"""Explicit natural-start host claims, authenticated by the original guard.

A file is only a claim. The guard binds it to the actual inner foreground,
the immutable entry command, original owner, private socket peer and native
TTY before exposing it to the recovery driver. No SID comes from this registry.
"""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import hashlib
import json
import os
from pathlib import Path

from ccc_herdr_exit_context import PinnedDirectory, canonical, need, PROTECTED
from ccc_original_host_client import OriginalHostClient


SCHEMA='ccc-original-natural-host-claim-v1'


def publish_claim(config, *, binding, host_identity, host_directory, directory_identity):
    """Natural entry only; publishing creates no recovery authority by itself."""
    state=Path(config['state']);stores=config['original_stores']
    need(stores['state']==str(state),'original_registry_state_changed')
    need(binding['cmux_surface_id'].upper()!=PROTECTED,'protected_business_surface')
    directory=state/'original-child-hosts'
    with PinnedDirectory(state,expected=stores['state_identity'],forbidden_mode=0o022) as parent:
        try:os.mkdir(directory.name,0o700,dir_fd=parent.fd)
        except FileExistsError:pass
        parent.check()
    claim=dict(schema=SCHEMA,binding=binding,host_identity=host_identity,
               host_directory=str(host_directory),directory_identity=list(directory_identity),
               original_state_identity=stores['state_identity'])
    key=hashlib.sha256(canonical(host_identity)).hexdigest()
    raw=canonical(claim)+b'\n'
    with PinnedDirectory(directory) as owned:
        old=owned.read(key+'.json')
        need(old is None or old==raw,'original_host_claim_changed')
        if old is None:need(owned.publish(key+'.json',raw),'original_host_claim_created_elsewhere')
        need(owned.read(key+'.json')==raw,'original_host_claim_not_durable')
    return dict(path=str(directory/(key+'.json')),sha256=hashlib.sha256(raw).hexdigest())


class OriginalHostRegistry:
    def __init__(self,state,service_release,*,owner_identity,owner_recheck,entry_prefix):
        self.state=Path(state);self.root=Path(service_release)
        self.owner=owner_identity;self.recheck=owner_recheck
        self.entry_prefix=entry_prefix

    def collect(self,guard,snapshot):
        self.recheck();stores=guard.guard_config['original_stores']
        directory=self.state/'original-child-hosts'
        registrations=[];held=[]
        with PinnedDirectory(self.state,expected=stores['state_identity'],forbidden_mode=0o022) as parent:
            try:os.stat(directory.name,dir_fd=parent.fd,follow_symlinks=False)
            except FileNotFoundError:return registrations,held
            with PinnedDirectory(directory) as owned:
                names=os.listdir(owned.fd)
                need(len(names)<=128,'original_host_registry_capacity_attention')
                for name in sorted(names):
                    try:
                        need(len(name)==69 and name.endswith('.json') and
                             all(x in '0123456789abcdef' for x in name[:-5]),'invalid_host_claim_name')
                        raw=owned.read(name,128*1024);claim=json.loads(raw)
                        need(claim.get('schema')==SCHEMA
                             and claim.get('original_state_identity')==stores['state_identity'],
                             'original_host_claim_schema_changed')
                        identity=claim['host_identity'];binding=claim['binding']
                        need(hashlib.sha256(canonical(identity)).hexdigest()+'.json'==name
                             and identity.get('owner')==self.owner,'original_host_claim_owner_changed')
                        pid=identity['host_pid'];born=identity['host_birth']
                        need(pid!=PUBLIC_PROTECTED_PID and binding['cmux_surface_id'].upper()!=PROTECTED,
                             'protected_business_surface')
                        endpoints=[e for e in guard.config['endpoints']
                            if e['session']==binding['session'] and e['cmux_surface_id']==binding['cmux_surface_id']
                            and e['cmux_workspace_id']==binding['cmux_workspace_id']
                            and e['socket']+'#'+binding['pane_id']==binding['key']]
                        need(len(endpoints)==1 and endpoints[0].get('discovered_read_only') is not True,
                             'host_endpoint_not_originally_enrolled')
                        rows=[r for r in snapshot.get('panel_inventory',[])
                              if r.get('session')==binding['session'] and r.get('pane_id')==binding['pane_id']
                              and r.get('terminal_id')==binding['terminal_id']]
                        need(len(rows)==1,'original_host_terminal_missing')
                        terminal=rows[0].get('native_terminal_observation') or {}
                        # A resumed child and its host share the original
                        # foreground group. Keep registration readable while
                        # that child runs so pending receipt reconciliation is
                        # not postponed until yet another exit.
                        need(terminal.get('phase') in {'program_foreground','agent_foreground'}
                             and terminal.get('binding')==binding
                             and any(p.get('pid')==pid and p.get('birth')==born
                                     for p in terminal.get('processes',[])),
                             'original_host_not_native_foreground')
                        process=guard.process(pid)
                        need(process.get('birth')==born and guard.scope.birth(pid)==born
                             and process.get('argv',[])[:len(self.entry_prefix)]==self.entry_prefix,
                             'original_host_entry_generation_changed')
                        tty=identity['terminal']
                        need(terminal.get('tty')==tty['tty'].removeprefix('/dev/')
                             and terminal.get('foreground_pgid')==tty['pgid'],
                             'original_host_controlling_terminal_changed')
                        host_directory=Path(claim['host_directory'])
                        need(host_directory.is_absolute() and len(os.fsencode(host_directory/'host.sock'))<100,
                             'original_host_socket_path_invalid')
                        client=OriginalHostClient(host_directory,directory_identity=claim['directory_identity'],
                            host_pid=pid,host_birth=born,scope=guard.scope)
                        view=client.call({'op':'observe'})
                        need(view.get('identity')==identity,'original_host_peer_claim_changed')
                        self.recheck()
                        need(guard.process(pid)==process and owned.read(name,128*1024)==raw,
                             'original_host_changed_during_registration_read')
                        registrations.append(dict(binding=binding,host_identity=identity,client=client))
                    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as error:
                        held.append(dict(claim=name,reason=str(error)[:200]))
            parent.check()
        return registrations,held
