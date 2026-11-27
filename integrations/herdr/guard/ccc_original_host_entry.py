"""Natural-start wrapper for the one installed official Codex program.

This entry never adopts a running process. An unavailable preflight executes
the original user invocation normally, without claiming recovery coverage.
After registration publication begins there is no fallback launch or retry:
a failed publication can leave an authenticated claim on disk.
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

from ccc_herdr_exit_context import PinnedDirectory, need


def original_client(argv):
    # Caller first authenticates the complete pair. Use its actual installed
    # launcher resolver, not a second path convention that can become stale.
    import ccc_batch_guard
    binary=Path(ccc_batch_guard.native_binary()).resolve(strict=True)
    need(argv and Path(argv[0]).is_absolute() and Path(argv[0]).resolve(strict=True)==binary,
         'natural_entry_requires_original_official_client')
    need(all(isinstance(a,str) and '\0' not in a for a in argv),'invalid_natural_client_arguments')
    return [str(binary),*argv[1:]]


def authenticate(root,config_path,manifest_pin):
    import ccc_herdr_production187 as production
    manifest,config=production.verify_release(root,config_path,manifest_pin)
    return manifest,config,production.load_dependencies(root,config,manifest)


def prepare(root,config_path,manifest_pin,argv,*,authenticated=None,before_claim=None):
    import ccc_herdr_production187 as production
    manifest,config,loaded=authenticated or authenticate(root,config_path,manifest_pin)
    paired,runtime,payload,members,original,transport,codex,origins=loaded
    import ccc_guard_scope as scope
    from ccc_original_child_host import OriginalChildHost
    from ccc_hosted_resume_validation import HostedResumeValidator
    from ccc_original_host_registration import publish_claim
    argv=original_client(argv)
    # The entry only needs read-only placement. Do not construct CodexGuard
    # and hence another writable ProviderRetryStore outside the owner loop.
    guard=transport.build_guard_class(original.Observer)(config)
    environment=dict(os.environ)
    need(environment.get('HERDR_ENV')=='1','natural_entry_not_inside_herdr')
    endpoint=transport.endpoint_for(guard.config,environment.get('HERDR_SESSION','default'))
    need(endpoint.get('discovered_read_only') is not True,'natural_entry_endpoint_monitor_only')
    status=json.loads((Path(config['state'])/'status.json').read_bytes())
    need(status.get('guard_manifest_sha256')==manifest_pin
         and status.get('original_stores')==config['original_stores']
         and 0<=time.time()-status['at_epoch']<=120,'natural_entry_original_guard_status_unavailable')
    process=guard.process(status['pid'])
    expected=str(root/'ccc_herdr_production187.py')
    arguments=process['argv']
    need(expected in arguments and '--serve' in arguments and '--config' in arguments
         and arguments[arguments.index('--config')+1]==str(config_path)
         and '--manifest-sha256' in arguments
         and arguments[arguments.index('--manifest-sha256')+1]==manifest_pin,
         'natural_entry_guard_command_changed')
    owner=dict(pid=process['pid'],birth=process['birth'],service_release=str(root))
    def check():
        production.verify_release(root,config_path,manifest_pin)
        paired.verify_pair(config['observer']);paired.loaded_origins(members)
        need(scope.birth(owner['pid'])==owner['birth'],'natural_entry_original_owner_gone')
        need(guard.process(owner['pid'])['argv']==arguments,'natural_entry_original_owner_command_changed')
    check()
    server=guard.server(endpoint,guard.cmux_membership(endpoint))
    panes=guard.cli(endpoint['session'],['api','snapshot'])['snapshot']['panes']
    selected=[p for p in panes if p.get('pane_id')==environment.get('HERDR_PANE_ID')]
    need(len(selected)==1,'natural_entry_inner_pane_unbound')
    terminal=transport.terminal_program_observation(guard,endpoint,selected[0],server)
    need(terminal.get('phase')=='program_foreground'
         and any(p.get('pid')==os.getpid() and p.get('birth')==scope.birth(os.getpid())
                 for p in terminal.get('processes',[])),'natural_entry_not_foreground_owner')
    host_root=Path.home()/'.local/state/ccc-original-child-hosts'
    host_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with PinnedDirectory(host_root) as parent:
        name=secrets.token_hex(12)
        os.mkdir(name,0o700,dir_fd=parent.fd);parent.check()
    directory=host_root/name
    need(len(os.fsencode(directory/'host.sock'))<100,'natural_host_socket_path_too_long')
    validator=HostedResumeValidator(config,scope=scope,transport=transport,owner_recheck=check)
    host=OriginalChildHost(directory,owner=owner,native_birth=scope.birth,validate_resume=validator)
    need(terminal['tty']==host.terminal['tty'].removeprefix('/dev/')
         and terminal['foreground_pgid']==host.terminal['pgid'],'natural_host_terminal_changed')
    info=directory.stat()
    # Tell the entry before any shared claim write. publish_claim can fail
    # after link/fsync, so a returned claim is too late to establish this fence.
    if before_claim is not None:before_claim()
    claim=publish_claim(config,binding=terminal['binding'],host_identity=host.record,
                        host_directory=directory,directory_identity=[info.st_dev,info.st_ino])
    check()
    plan=dict(argv=argv,environment=environment,cwd=str(Path.cwd().resolve()))
    return host,plan,claim


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    argv=args.command[1:] if args.command[:1]==['--'] else args.command
    # A trusted wrapper must pass the canonical installed client as argv[0].
    # Invalid executable selection is never an excuse to exec an arbitrary path.
    authenticated=authenticate(root,args.config,args.manifest_sha256)
    argv=original_client(argv)
    # Import only after the original complete paired loader was authenticated.
    from ccc_host_lifetime_gate import HostLifetimeGate
    lease=None
    registration_started=False
    def before_claim():
        nonlocal registration_started
        lease.check()
        registration_started=True
    try:
        lease=HostLifetimeGate(authenticated[1]['host_lifetime_gate'])
        lease.acquire()
        host,plan,claim=prepare(root,args.config,args.manifest_sha256,argv,
                                authenticated=authenticated,before_claim=before_claim)
        lease.check()
    except Exception as error:
        if lease is not None:lease.close()
        if registration_started:
            print('CCC：启动登记未完成核验，Codex 尚未启动；已保留现场，请检查启动错误。',file=sys.stderr)
            raise
        print('CCC：启动保护暂不可用，按原命令启动 Codex。'+type(error).__name__,file=sys.stderr)
        os.execve(argv[0],argv,dict(os.environ))
        return
    # No fallback after serve begins: a response/persistence failure does not
    # prove that its initial child was never released.
    try:result=host.serve(plan)
    finally:lease.close()
    code=(result or {}).get('exit_status',0)
    raise SystemExit(128-code if code<0 else code)


if __name__=='__main__':main()
