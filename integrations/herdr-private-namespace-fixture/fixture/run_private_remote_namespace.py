"""Real candidate Herdr server; explicitly simulated agent/SID, no account.
No production API writes, no real AGY or native conversation writes.
"""
from pathlib import Path
import argparse, ctypes, fcntl, hashlib, json, os, pty, re, shutil, signal, socket, struct, subprocess, sys, tempfile, termios, threading, time, uuid
HERE = Path(__file__).resolve().parent
B = None
HERDR = None
RUNTIME = HERE.parent / 'scope'
sys.path.insert(0, str(RUNTIME))
import ccc_guard_scope as scope

def save(path, value):
    with path.open('x') as f: json.dump(value, f, ensure_ascii=False, indent=2); f.write('\n')
def need(value, reason):
    if not value: raise RuntimeError(reason)

def kernel_path(pid):
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    buf=ctypes.create_string_buffer(4096)
    count=lib.proc_pidpath(pid,buf,len(buf))
    need(count>0,'kernel executable unavailable')
    return Path(os.fsdecode(buf.value)).resolve()

class PrivateTui:
    """Actual candidate client on its own PTY. Never writes keyboard input."""
    def __init__(self,binary,session,env,output,phase,server_identity):
        self.binary=binary;self.env=env;self.phase=phase;self.output=output
        self.errors=[];self.bytes=0;self.digest=hashlib.sha256();self.birth=None;self.closing=False
        master,slave=pty.openpty();self.master=master
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
        self.tty=os.ttyname(slave)
        command=[str(binary),'--session',session]
        save(output/(phase+'-TUI-ATTACH-INTENT.json'),dict(argv=command,
            home=env['HOME'],tty=self.tty,rows=24,cols=80,server=server_identity,
            keyboard_input_bytes=0,manual_resume_commands=0))
        def controlling_tty():
            os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
        try:
            self.process=subprocess.Popen(command,env=env,stdin=slave,stdout=slave,stderr=slave,
                                          close_fds=True,preexec_fn=controlling_tty)
        finally:os.close(slave)
        self.stream=(output/(phase+'-TUI-OUTPUT.bin')).open('xb')
        def drain():
            try:
                while True:
                    try:data=os.read(master,65536)
                    except OSError as exc:
                        if exc.errno==5 or (exc.errno==9 and self.closing):break
                        raise
                    if not data:break
                    self.bytes+=len(data);self.digest.update(data)
                    if self.bytes<=64*1024*1024:self.stream.write(data)
                    elif not self.errors:self.errors.append('PTY output exceeded retained 64MiB limit')
            except BaseException as exc:self.errors.append(repr(exc))
            finally:self.stream.close()
        self.reader=threading.Thread(target=drain,daemon=True);self.reader.start()
        try:
            self.birth=scope.birth(self.process.pid)
            self.assert_owned()
            argv,_=scope.arguments(self.process.pid)
            save(output/(phase+'-TUI-IDENTITY.json'),dict(pid=self.process.pid,birth=self.birth,
                argv=argv,kernel_path=str(kernel_path(self.process.pid)),tty=self.tty,
                home=env['HOME'],server=server_identity,keyboard_input_bytes=0))
        except BaseException:
            # Do not signal any process whose original binding was not proven.
            self.closing=True;os.close(self.master);self.master=None
            self.reader.join(timeout=2)
            raise

    def assert_owned(self):
        need(self.process.poll() is None,'private TUI already exited')
        need(self.birth is not None and scope.birth(self.process.pid)==self.birth,'private TUI birth changed')
        argv,env=scope.arguments(self.process.pid)
        need(argv==[str(self.binary),'--session','simulated-agy']
             and kernel_path(self.process.pid)==self.binary
             and env.get('HOME')==self.env['HOME']
             and env.get('HERDR_CONFIG_PATH')==self.env['HERDR_CONFIG_PATH']
             and scope.birth(self.process.pid)==self.birth,'private TUI ownership changed')

    def settle_after_server_stop(self):
        used_term=False
        try:self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.assert_owned()
            save(self.output/(self.phase+'-TUI-TERM-INTENT.json'),dict(pid=self.process.pid,
                birth=self.birth,signal='SIGTERM',reason='original private client remained after official server stop',
                production_signals=0))
            self.assert_owned();os.kill(self.process.pid,signal.SIGTERM);used_term=True
            self.process.wait(timeout=10)
        if self.master is not None:
            self.closing=True;os.close(self.master);self.master=None
        self.reader.join(timeout=3)
        need(not self.reader.is_alive(),'private PTY drain did not finish')
        need(not self.errors,repr(self.errors))
        save(self.output/(self.phase+'-TUI-EXIT.json'),dict(pid=self.process.pid,birth=self.birth,
            exit_code=self.process.returncode,precise_private_term_used=used_term,
            output_bytes=self.bytes,output_sha256=self.digest.hexdigest(),keyboard_input_bytes=0,
            exact_process_exited=scope.birth(self.process.pid)!=self.birth,production_signals=0))
def main():
    global HERDR, B
    sys.path.insert(0, str(HERE.parent))
    from actions_context import require_actions
    require_actions()
    parser=argparse.ArgumentParser()
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--binary',required=True);parser.add_argument('--sha256',required=True)
    parser.add_argument('--run-name',required=True)
    parser.add_argument('--namespace',choices=('herdr',),default='herdr')
    parser.add_argument('--remote-proof',required=True)
    parser.add_argument('--remote-proof-sha256',required=True)
    args=parser.parse_args();HERDR=Path(args.binary).resolve();B=args.assets.resolve()
    need(HERDR.is_file() and hashlib.sha256(HERDR.read_bytes()).hexdigest()==args.sha256,'candidate binary pin mismatch')
    patch=B/'candidate-v3'/'AGY-RECOVERY-V3.patch'
    patch_sha='3a18fdb4c86a77acfe2471bb6610db3f283281e9d4e75c890f8d3a105f661966'
    need(hashlib.sha256(patch.read_bytes()).hexdigest()==patch_sha,'reviewed v3 patch changed')
    production_stage=B
    # This proof represents actual GitHub compilation/tests, never local receipts.
    from remote_proof import verify_saved_proof
    remote = verify_saved_proof(Path(args.remote_proof), args.remote_proof_sha256,
                                HERDR, args.sha256)
    # Do not launch even a private server below the existing 40 GiB reserve.
    filesystem = os.statvfs(production_stage)
    need(filesystem.f_bavail * filesystem.f_frsize >= 40 * 1024**3,
         'private fixture requires at least 40 GiB available; no launch performed')
    need(args.run_name.startswith('candidate-private-server-') and '/' not in args.run_name,'invalid output name')
    out=B/args.run_name;out.mkdir()
    save(out/'CANDIDATE-PINS.json',dict(binary=str(HERDR),binary_sha256=args.sha256,
        patch_sha256=patch_sha,namespace=args.namespace,
        remote_proof=str(Path(args.remote_proof).resolve()),
        remote_proof_sha256=args.remote_proof_sha256,
        remote_build_run=remote['github_run'],
        remote_manifest_sha256=remote['build_manifest_sha256'],
        compile_receipt_origin='GitHub Actions actual compiler artifacts and rustc log',
        unit_receipt_origin='GitHub Actions exact LIST and 3656 successful JUnit cases',
        local_compilation_claimed=False,
        nonoptimized_production_namespace=True))
    home=Path(tempfile.mkdtemp(prefix='hagyd-',dir='/private/tmp')).resolve()
    name='simulated-agy';config=home/'.config'/args.namespace/'config.toml';config.parent.mkdir(parents=True)
    config.write_text('onboarding=false\n[terminal]\ndefault_shell="/bin/sh"\nshell_mode="non_login"\n[session]\nresume_agents_on_restore=true\n[experimental]\nallow_nested=true\n[update]\nversion_check=false\nmanifest_check=false\n')
    agent=home/'bin/agy';agent.parent.mkdir();shutil.copyfile(B/'simulated-agy',agent);agent.chmod(0o755)
    endpoint=home/'.config'/args.namespace/'sessions'/name/'herdr.sock';snapshot=endpoint.parent/'session.json'
    env={'HOME':str(home),'USER':__import__('pwd').getpwuid(os.getuid()).pw_name,'LOGNAME':__import__('pwd').getpwuid(os.getuid()).pw_name,'SHELL':'/bin/sh','PATH':str(home/'bin')+':/usr/bin:/bin:/usr/sbin:/sbin',
         'XDG_CONFIG_HOME':str(home/'.config'),'HERDR_CONFIG_PATH':str(config),'TERM':'xterm-256color','LANG':'en_US.UTF-8','TMPDIR':str(home)}
    calls=[];server=None;peer=None;generation=None;failure=None;fake_pid=None;fake_birth=None;tui=None;restore_log_fence=None
    native_server_log=endpoint.parent/'herdr-server.log'
    def call(*args, allow_deferred=False):
        command=[str(HERDR),'--session',name,*args]
        r=subprocess.run(command,env=env,capture_output=True,text=True,timeout=12)
        calls.append(dict(argv=command,returncode=r.returncode,stdout=r.stdout,stderr=r.stderr,at_epoch=time.time()))
        if r.returncode and allow_deferred:
            try:error=json.loads(r.stderr)
            except (ValueError,TypeError):error={}
            if error.get('error',{}).get('code')=='pane_not_found':return error
        need(r.returncode==0,repr(calls[-1]))
        return {'text':r.stdout} if args[:2]==('pane','read') else (json.loads(r.stdout) if r.stdout.strip() else None)
    def socket_peer():
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
            c.settimeout(3);c.connect(str(endpoint));return int.from_bytes(c.getsockopt(0,2,4),sys.byteorder,signed=True)
    def exact_recovery(row):
        recovery=row.get('agent_recovery') or {}
        return (recovery.get('automatic') is True
            and recovery.get('session')==dict(source='herdr:antigravity_cli',agent='agy',kind='id',value=sid)
            and recovery.get('resume')==dict(source='herdr:antigravity_cli',agent='agy',argv=['agy','--conversation',sid]))
    def snapshot_row():
        value=json.loads(snapshot.read_text())
        rows=[r for w in value['workspaces'] for t in w['tabs'] for r in t['panes'].values() if (r.get('agent_session') or {}).get('value')==sid]
        return value,rows
    save(out/'HARNESS-PIN.json',dict(path=str(Path(__file__).resolve()),sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    log=(out/'server-stdio.log').open('wb')
    try:
        save(out/'INTENT.json',dict(private_home=str(home),endpoint=str(endpoint),herdr_sha256=hashlib.sha256(HERDR.read_bytes()).hexdigest(),simulation=True,real_agy=False,production_inputs=0))
        server=subprocess.Popen([str(HERDR),'--session',name,'server'],env=env,stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+20
        while not endpoint.exists() and time.monotonic()<deadline:time.sleep(.1)
        need(endpoint.exists(),'private server socket not created');peer=socket_peer();generation=scope.birth(peer)
        argv,actual_env=scope.arguments(peer)
        need(actual_env.get('HOME')==str(home) and actual_env.get('HERDR_CONFIG_PATH')==str(config),'server private environment mismatch')
        save(out/'SERVER-IDENTITY.json',dict(pid=peer,birth=generation,argv=argv,endpoint=str(endpoint),home=str(home)))
        reply=call('workspace','create','--no-focus','--cwd',str(home),'--label','explicit simulated AGY')
        pane=reply['result']['root_pane']['pane_id']
        call('pane','run',pane,str(agent),'90')
        time.sleep(1)
        save(out/'INITIAL-PROCESS-INFO.json',call('pane','process-info','--pane',pane))
        save(out/'INITIAL-REPLAY.json',call('pane','read',pane))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            pane_reply=call('pane','get',pane)
            if pane_reply['result']['pane'].get('agent')=='agy':break
            time.sleep(.25)
        need(pane_reply['result']['pane'].get('agent')=='agy','simulated process not detected as agy')
        sid=str(uuid.uuid4());save(out/'SIMULATION-IDENTITY.json',dict(sid=sid,pane=pane,agent_binary=str(agent),agent_binary_sha256=hashlib.sha256(agent.read_bytes()).hexdigest(),compiled_from=str(B/'simulated_agy.rs'),not_a_native_agy_conversation=True))
        call('pane','report-agent-session',pane,'--source','herdr:antigravity_cli','--agent','agy','--agent-session-id',sid,'--','agy','--conversation',sid)
        before=call('pane','get',pane);need(before['result']['pane']['agent_session']['value']==sid,'simulated report not adopted')
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if snapshot.exists():
                before_disk,rows=snapshot_row()
                if rows and rows[0].get('agent_resume') and exact_recovery(rows[0]):break
            time.sleep(.25)
        need(rows and rows[0].get('agent_resume') and exact_recovery(rows[0]),'first genuine save missing active or durable metadata');save(out/'BEFORE-SNAPSHOT.json',before_disk)
        process=call('pane','process-info','--pane',pane);save(out/'PROCESS-INFO.json',process)
        # Resolve exclusively the fixture executable and private HOME generation.
        matches=[]
        def reader(pid):
            born=scope.birth(pid)
            if born is None:return None
            try:
                a,e=scope.arguments(pid)
                if a and a[0]==str(agent) and e.get('HOME')==str(home) and scope.birth(pid)==born:return dict(pid=pid,birth=born,argv=a)
            except (OSError,ValueError):pass
        matches=scope.scan(reader=reader);need(len(matches)==1,'fixture child not unique')
        fake_pid=matches[0]['pid'];fake_birth=matches[0]['birth'];save(out/'SIMULATED-AGENT-EXIT-INTENT.json',matches[0])
        need(scope.birth(fake_pid)==fake_birth,'fixture child generation changed');os.kill(fake_pid,signal.SIGTERM)
        deadline=time.monotonic()+15
        lost=None
        while time.monotonic()<deadline:
            current=call('pane','get',pane)
            if current['result']['pane'].get('agent') is None and current['result']['pane'].get('agent_session') is None:
                lost=current;break
            time.sleep(.25)
        need(lost is not None,'current metadata did not clear');save(out/'AFTER-PANE.json',lost)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            after_disk,rows=snapshot_row()
            all_rows=[r for w in after_disk['workspaces'] for t in w['tabs'] for r in t['panes'].values()]
            if (not rows and after_disk!=before_disk and len(all_rows)==1 and exact_recovery(all_rows[0])
                    and not all_rows[0].get('agent_session') and not all_rows[0].get('agent_resume')):break
            time.sleep(.25)
        need(not rows and after_disk!=before_disk and len(all_rows)==1 and exact_recovery(all_rows[0]) and not all_rows[0].get('agent_session') and not all_rows[0].get('agent_resume'),'genuine post-exit save missing expected active/durable split');save(out/'AFTER-SNAPSHOT.json',after_disk)
        all_rows=[r for w in after_disk['workspaces'] for t in w['tabs'] for r in t['panes'].values()]
        need(len(all_rows)==1,'fixture pane count changed')
        recovery=all_rows[0].get('agent_recovery')
        need(recovery and recovery.get('automatic') is True,'candidate lost automatic recovery intent')
        need(recovery['session']['value']==sid and recovery['resume']['argv']==['agy','--conversation',sid],'candidate recovery identity/argv mismatch')
        generations=[dict(pid=peer,birth=generation)]
        def restart(phase):
            nonlocal server,peer,generation,tui,restore_log_fence
            need(socket_peer()==peer and scope.birth(peer)==generation,'restart old peer changed')
            call('server','stop');server.wait(timeout=15)
            if tui is not None:
                tui.settle_after_server_stop();tui=None
            need(scope.birth(peer)!=generation,'old private server still alive')
            need(hashlib.sha256(HERDR.read_bytes()).hexdigest()==args.sha256,'candidate binary changed')
            with native_server_log.open('rb') as handle:
                stat=os.fstat(handle.fileno());offset=stat.st_size
                handle.seek(max(0,offset-256));boundary=handle.read()
            restore_log_fence=dict(path=str(native_server_log),device=stat.st_dev,inode=stat.st_ino,
                                   offset=offset,boundary_sha256=hashlib.sha256(boundary).hexdigest())
            save(out/(phase+'-SERVER-LOG-FENCE.json'),restore_log_fence)
            server=subprocess.Popen([str(HERDR),'--session',name,'server'],env=env,stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                try:
                    if endpoint.exists():
                        candidate=socket_peer()
                        if candidate!=peer or scope.birth(candidate)!=generation:break
                except (OSError,TimeoutError):pass
                time.sleep(.1)
            else:raise RuntimeError('new private server not available')
            peer=candidate;generation=scope.birth(peer);a,e=scope.arguments(peer)
            need(e.get('HOME')==str(home) and e.get('HERDR_CONFIG_PATH')==str(config),'new server environment mismatch')
            generations.append(dict(pid=peer,birth=generation))
            save(out/(phase+'-SERVER.json'),dict(pid=peer,birth=generation,argv=a))
            restored_pane=call('pane','get',pane)
            need(restored_pane['result']['pane']['pane_id']==pane,'restored pane public id changed')
            save(out/(phase+'-PANE-IDENTITY.json'),restored_pane)
            # Real native client supplies viewport/terminal context to the
            # unchanged deferred restore path. No pane.run, keys or RPC resize.
            need(socket_peer()==peer and scope.birth(peer)==generation,'peer changed before private TUI attach')
            tui=PrivateTui(HERDR,name,env,out,phase,dict(pid=peer,birth=generation))
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                tui.assert_owned()
                process=call('pane','process-info','--pane',pane,allow_deferred=True)
                if 'error' not in process:
                    save(out/(phase+'-RUNTIME-AVAILABLE.json'),process)
                    break
                time.sleep(.25)
            else:raise RuntimeError('real private TUI did not make deferred runtime available')
        restart('RESTORE')
        deadline=time.monotonic()+25;resumed=None
        while time.monotonic()<deadline:
            tui.assert_owned()
            process=call('pane','process-info','--pane',pane)
            for row in process['result']['process_info'].get('foreground_processes',[]):
                if row.get('argv',[])[-2:]==['--conversation',sid]:
                    pid=row['pid'];born=scope.birth(pid);a,e=scope.arguments(pid)
                    if (e.get('HOME')==str(home) and a[-2:]==['--conversation',sid]
                            and kernel_path(pid)==agent.resolve() and scope.birth(pid)==born):
                        resumed=dict(pid=pid,birth=born,argv=a,process_info=process);break
            if resumed:break
            time.sleep(.25)
        need(resumed is not None,'saved resume command did not actually run')
        save(out/'RESUMED-REAL-PROCESS.json',resumed)
        # A short-lived process can finish before the native detector ever
        # observes it. The old initial-unknown policy then deliberately retains
        # its saved SID. Require a real detector acquisition before this test
        # injects the second passive exit; do not change that native policy.
        deadline=time.monotonic()+20;detector_proof=None
        while time.monotonic()<deadline:
            tui.assert_owned()
            need(scope.birth(peer)==generation and socket_peer()==peer,'server changed while awaiting detector')
            need(scope.birth(resumed['pid'])==resumed['birth'],'resumed process exited before detector acquisition')
            argv_now,env_now=scope.arguments(resumed['pid'])
            need(argv_now==resumed['argv'] and env_now.get('HOME')==str(home)
                 and kernel_path(resumed['pid'])==agent.resolve(),'resumed identity changed while awaiting detector')
            pgid=os.getpgid(resumed['pid'])
            need(pgid==resumed['process_info']['result']['process_info']['foreground_process_group_id'],
                 'resumed process group changed')
            with native_server_log.open('rb') as handle:
                stat=os.fstat(handle.fileno());fence=restore_log_fence
                need((stat.st_dev,stat.st_ino)==(fence['device'],fence['inode'])
                     and fence['offset']<=stat.st_size<=fence['offset']+1024*1024,'private server log fence changed')
                handle.seek(max(0,fence['offset']-256));boundary=handle.read(min(256,fence['offset']))
                need(hashlib.sha256(boundary).hexdigest()==fence['boundary_sha256'],'private log boundary changed')
                handle.seek(fence['offset']);raw=handle.read(stat.st_size-fence['offset'])
            startup=re.search(rb'herdr starting event="app.startup" subsystem="server" outcome="started" pid='
                              +str(peer).encode()+rb'(?:\s|$)',raw)
            needle=(rb'^.*herdr::pane: agent changed\b[^\n]*\bagent=Some\(Antigravity\)[^\n]*\bpgid=Some\('
                    +str(pgid).encode()+rb'\)[^\n]*\n')
            detection=re.search(needle,raw,re.MULTILINE)
            if startup and detection and startup.start()<detection.start():
                need(scope.birth(resumed['pid'])==resumed['birth'] and scope.birth(peer)==generation,
                     'detector proof identity changed after read')
                detector_proof=dict(server=dict(pid=peer,birth=generation),process=resumed,
                    pgid=pgid,log_fence=fence,line_offset=fence['offset']+detection.start(),
                    line_sha256=hashlib.sha256(detection.group()).hexdigest(),
                    line=detection.group().decode('utf-8'),observed_at_epoch=time.time())
                break
            time.sleep(.2)
        need(detector_proof is not None,'native detector did not acquire exact resumed process within20seconds')
        save(out/'RESUMED-DETECTOR-ACQUISITION.json',detector_proof)
        # The explicit clear is sent only after a second real passive exit.
        fake_pid=resumed['pid'];fake_birth=resumed['birth']
        need(scope.birth(fake_pid)==fake_birth,'resumed process generation changed')
        save(out/'RESUMED-EXIT-INTENT.json',resumed);os.kill(fake_pid,signal.SIGTERM)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            current=call('pane','get',pane)
            if current['result']['pane'].get('agent') is None and current['result']['pane'].get('agent_session') is None:break
            time.sleep(.25)
        else:raise RuntimeError('resumed passive exit did not clear active state')
        call('pane','release-agent',pane,'--source','herdr:antigravity_cli','--agent','agy','--seq','100')
        deadline=time.monotonic()+12;tombstone=None
        while time.monotonic()<deadline:
            saved=json.loads(snapshot.read_text())
            rows=[r for w in saved['workspaces'] for t in w['tabs'] for r in t['panes'].values()]
            if len(rows)==1 and rows[0].get('agent_recovery',{}).get('automatic') is False:
                tombstone=saved;break
            time.sleep(.25)
        need(tombstone is not None,'explicit release did not save durable tombstone')
        save(out/'TOMBSTONE-SNAPSHOT.json',tombstone)
        restart('TOMBSTONE')
        observations=[];deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            tui.assert_owned()
            process=call('pane','process-info','--pane',pane);observations.append(process)
            need(not any('--conversation' in r.get('argv',[]) for r in process['result']['process_info'].get('foreground_processes',[])), 'tombstone resumed old agent')
            time.sleep(.5)
        save(out/'TOMBSTONE-OBSERVATIONS.json',observations)
        save(out/'CANDIDATE-RESULT.json',dict(passed=True,simulation=True,native_agy=False,real_herdr_server=True,
            passive_exit_retained_exact_recovery=True,real_restored_process=True,
            explicit_release_after_exit_saved_tombstone=True,tombstone_no_agent_observed_seconds=12,
            sid=sid,server_generations=generations,real_private_tui_attach=True,tui_keyboard_input_bytes=0,
            native_detector_acquired_before_second_exit=True,
            manual_resume_commands_after_restart=0,production_inputs=0,production_signals=0))
    except BaseException as exc:
        failure=exc;save(out/'FAILURE.json',dict(error=repr(exc)))
    finally:
        cleanup_errors=[];stop_allowed=False;leftovers=None
        # Unknown starts reconcile only this original private namespace. No
        # second start and no signal if the peer identity cannot be proven.
        if endpoint.exists():
            try:
                observed=socket_peer();born=scope.birth(observed);a,e=scope.arguments(observed)
                need(born is not None and scope.birth(observed)==born,'cleanup peer birth unstable')
                need(a and Path(a[0]).resolve()==HERDR and e.get('HOME')==str(home)
                     and e.get('HERDR_CONFIG_PATH')==str(config),'cleanup private peer identity mismatch')
                peer=observed;generation=born;stop_allowed=True
                save(out/'CLEANUP-RECONCILED-PEER.json',dict(pid=peer,birth=generation,argv=a,home=str(home)))
            except BaseException as exc:
                cleanup_errors.append(dict(phase='peer_reconciliation',error=repr(exc)))
        if stop_allowed:
            try:
                need(scope.birth(peer)==generation and socket_peer()==peer,'cleanup peer changed before stop')
                call('server','stop')
            except BaseException as exc:
                cleanup_errors.append(dict(phase='official_stop',error=repr(exc)))
        if server:
            try:server.wait(timeout=15)
            except BaseException as exc:cleanup_errors.append(dict(phase='wrapper_exit',error=repr(exc)))
        if tui is not None:
            try:tui.settle_after_server_stop()
            except BaseException as exc:cleanup_errors.append(dict(phase='private_tui_exit',error=repr(exc)))
        def cleanup_reader(pid):
            born=scope.birth(pid)
            if born is None:return None
            try:
                a,e=scope.arguments(pid)
                if e.get('HOME')==str(home) and scope.birth(pid)==born:return dict(pid=pid,birth=born,argv=a)
            except (OSError,ValueError):pass
        try:leftovers=scope.scan(reader=cleanup_reader)
        except BaseException as exc:cleanup_errors.append(dict(phase='private_process_scan',error=repr(exc)))
        server_exited=not peer or scope.birth(peer)!=generation
        cleaned=server_exited and leftovers==[] and not cleanup_errors
        # Evidence writes are deliberately outside each failing cleanup action.
        save(out/'CALLS.json',calls)
        save(out/'CLEANUP.json',dict(passed=cleaned,server_exited=server_exited,
            all_private_processes_exited=leftovers==[],remaining_private_processes=leftovers,
            errors=cleanup_errors,production_signals=0))
        log.close()
        if cleanup_errors:failure=failure or RuntimeError(repr(cleanup_errors))
    if failure:raise failure
    need(cleaned,'private cleanup incomplete')
    print(json.dumps(dict(output=str(out),passed=True,cleanup=cleaned)))
if __name__=='__main__':main()
