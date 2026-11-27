"""Own the children of one future original terminal launch.

This is an actuator, not a recovery policy or scheduler. Only the original
guard's authenticated command can request a successor after a child exits.
It never types into a shell, discovers a SID from argv, resets retry budgets,
signals an existing business process, or creates another terminal.

Production assembly must supply validate_resume from the original controller.
That validator verifies its durable intent, latest selected SID, Goal, native
exit/error, saved configuration and no-other-writer evidence. No validator
means no recovery. Initial argv is the user's actual natural-start invocation.
"""
from contextlib import contextmanager
import array
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import sys
import termios
import time

from ccc_herdr_exit_context import PinnedDirectory, canonical, need


class OriginalChildHost:
    def __init__(self, directory, *, owner, native_birth, validate_resume=None):
        self.directory=Path(directory)
        self.owner=owner.copy();self.birth=native_birth;self.validate_resume=validate_resume
        self.pid=os.getpid();self.host_birth=self.birth(self.pid)
        need(type(self.host_birth) is list and len(self.host_birth)==2,'host_birth_unavailable')
        need(self.owner.get('pid')!=self.pid and self.birth(self.owner['pid'])==self.owner['birth'],
             'original_controller_unavailable')
        self.terminal=self.terminal_identity()
        self.original_termios=termios.tcgetattr(0)
        self.child=None;self.exit=None;self.sequence=0;self.revoked=None
        self.phase='created';self.attempts={};self.server=None
        self.waiting_mode=False;self.waiting_input_blocked=False;self.pending_input_bytes=0
        self.record=dict(schema='ccc-original-child-host-v1',host_pid=self.pid,
            host_birth=self.host_birth,owner=self.owner,terminal=self.terminal,
            shell_input_operations=0,original_controller_required=True)

    @staticmethod
    def terminal_identity():
        need(os.isatty(0) and os.isatty(1),'host_requires_original_terminal')
        a,b=os.fstat(0),os.fstat(1)
        need(stat.S_ISCHR(a.st_mode) and a.st_rdev==b.st_rdev
             and os.ttyname(0)==os.ttyname(1),'host_terminal_mismatch')
        pgid=os.getpgrp()
        need(os.tcgetpgrp(0)==pgid,'host_not_original_foreground_group')
        return dict(tty=os.ttyname(0),device=a.st_rdev,pgid=pgid,session_id=os.getsid(0))

    def terminal_recheck(self):
        need(self.birth(self.pid)==self.host_birth and self.terminal_identity()==self.terminal,
             'original_terminal_ownership_changed')

    def save(self):
        value=dict(self.record,phase=self.phase,child=self.child,last_exit=self.exit,
                   sequence=self.sequence,revoked=self.revoked,attempts=self.attempts,
                   waiting_input_blocked=self.waiting_input_blocked,
                   pending_input_bytes=self.pending_input_bytes)
        with PinnedDirectory(self.directory) as directory:
            previous=directory.read('host.json')
            directory.publish('host.json',canonical(value)+b'\n',replace=previous is not None,
                              expected=previous)

    def begin_waiting(self):
        # Canonical FIONREAD/select hides an unsubmitted half-line. The host
        # owns this foreground terminal while no child is running, so expose
        # bytes without reading, flushing, forwarding, or implementing an editor.
        attributes=termios.tcgetattr(0)
        attributes[3]&=~termios.ICANON
        attributes[6][termios.VMIN]=1;attributes[6][termios.VTIME]=0
        termios.tcsetattr(0,termios.TCSANOW,attributes)
        self.waiting_mode=True
        self.observe_waiting_input()

    def observe_waiting_input(self):
        if not self.waiting_mode:return
        before=(self.waiting_input_blocked,self.pending_input_bytes,self.phase)
        self.terminal_recheck()
        if termios.tcgetattr(0)[3]&termios.ICANON:
            self.waiting_input_blocked=True
        else:
            count=array.array('i',[0])
            fcntl.ioctl(0,termios.FIONREAD,count,True)
            need(count[0]>=0,'waiting_input_count_unknown')
            self.pending_input_bytes=count[0]
            if count[0]:self.waiting_input_blocked=True
        if self.waiting_input_blocked:
            # Sticky until user termination. Clearing/draining a queue from
            # elsewhere does not make the old automatic command safe to replay.
            self.phase='input_attention'
            if before!=(self.waiting_input_blocked,self.pending_input_bytes,self.phase):self.save()
            if not before[0]:
                print('\r\nCCC：检测到等待期间的输入，已暂停自动恢复；输入保持未提交。\r\n',flush=True)

    def require_empty_waiting_input(self):
        need(self.waiting_mode,'waiting_terminal_mode_unverified')
        self.observe_waiting_input()
        need(not self.waiting_input_blocked,'waiting_user_input_preserved')

    def restore_entry_mode(self):
        self.terminal_recheck()
        termios.tcsetattr(0,termios.TCSANOW,self.original_termios)
        self.waiting_mode=False

    def spawn(self,plan,key):
        """Fork gate keeps exec behind a durable PID/birth receipt.

        Closing the permit pipe on a persistence failure makes only our newly
        created, still-unexecuted child exit. No signals or business kill.
        """
        need(self.child is None and self.revoked is None,'child_active_or_host_revoked')
        need(key not in self.attempts,'original_launch_already_consumed')
        need(len(self.attempts)<512,'original_host_attempt_capacity_attention')
        self.terminal_recheck()
        if key!='initial':self.require_empty_waiting_input()
        argv,env,cwd=plan['argv'],plan['environment'],plan['cwd']
        need(isinstance(argv,list) and argv and all(isinstance(a,str) and '\0' not in a for a in argv),
             'invalid_child_argv')
        need(isinstance(env,dict) and all(isinstance(k,str) and isinstance(v,str)
             and '\0' not in k+v and '=' not in k for k,v in env.items()),'invalid_child_environment')
        need(Path(argv[0]).is_absolute() and Path(cwd).is_absolute(),'child_paths_not_absolute')
        self.attempts[key]={'phase':'spawn_intent','plan_sha256':hashlib.sha256(canonical(plan)).hexdigest()}
        self.save()
        read_gate,write_gate=os.pipe()
        try:
            pid=os.fork()
        except BaseException:
            os.close(read_gate);os.close(write_gate);raise
        if pid==0:
            # No application logic or Python callbacks in the forked child.
            # Host has no threads; exec occurs only after the parent persists.
            try:
                os.close(write_gate)
                for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,signal.SIG_DFL)
                permit=os.read(read_gate,1);os.close(read_gate)
                if permit!=b'1':os._exit(125)
                os.chdir(cwd)
                os.execve(argv[0],argv,env)
            except BaseException:
                os._exit(126)
        os.close(read_gate)
        released=False
        try:
            child_birth=self.birth(pid)
            need(type(child_birth) is list and len(child_birth)==2,'new_child_birth_unavailable')
            self.child=dict(pid=pid,birth=child_birth,launch_attempt_key=key)
            self.attempts[key].update(phase='exec_intent',child=self.child.copy())
            self.phase='child_running';self.save()
            self.terminal_recheck()
            need(self.revoked is None and self.birth(self.owner['pid'])==self.owner['birth'],
                 'original_controller_or_user_revoked_before_exec')
            if key!='initial':
                self.require_empty_waiting_input()
                # No flush. The last queue observation precedes release; this
                # is not an atomic exclusion of concurrent physical keystrokes.
                self.restore_entry_mode()
            os.write(write_gate,b'1');released=True
        finally:
            os.close(write_gate)
            if not released:
                os.waitpid(pid,0)
                self.child=None
                self.phase='input_attention' if self.waiting_input_blocked else 'attention'
                self.save()
                # Keep the durable intent; no retry even if recording fails.
        return self.child.copy()

    def reap(self):
        if self.child is None:return
        pid,status=os.waitpid(self.child['pid'],os.WNOHANG)
        if pid==0:return
        need(pid==self.child['pid'],'unexpected_child_reap')
        previous=self.child;self.child=None;self.sequence+=1
        code=os.waitstatus_to_exitcode(status)
        self.exit=dict(child=previous,exit_status=code,sequence=self.sequence,
                       host_pid=self.pid,host_birth=self.host_birth)
        self.exit['exit_id']=hashlib.sha256(canonical(self.exit)).hexdigest()
        self.attempts[previous['launch_attempt_key']]['phase']='child_exited'
        self.attempts[previous['launch_attempt_key']]['exit_status']=code
        self.terminal_recheck()
        # Restore this host's own entry state, without flushing keyboard input.
        termios.tcsetattr(0,termios.TCSANOW,self.original_termios)
        if code==0 or code<0 or self.revoked is not None:
            self.phase='finished'
        else:
            # Exit code alone NEVER proves an upstream error or active Goal.
            self.phase='awaiting_original_controller'
            self.begin_waiting()
        self.save()

    def handle(self,request,peer_pid):
        need(peer_pid==self.owner['pid'] and self.birth(peer_pid)==self.owner['birth'],
             'not_original_controller')
        need(isinstance(request,dict) and request.get('op') in {'observe','lookup','resume'},'unknown_host_operation')
        self.terminal_recheck()
        if self.child is None:self.observe_waiting_input()
        if request['op']=='observe':
            return dict(phase=self.phase,child=self.child,last_exit=self.exit,
                        host_pid=self.pid,host_birth=self.host_birth,terminal=self.terminal,
                        identity=self.record,waiting_input_blocked=self.waiting_input_blocked,
                        pending_input_bytes=self.pending_input_bytes)
        if request['op']=='lookup':
            return dict(identity=self.record,attempt=self.attempts.get(request.get('attempt_key')),
                        phase=self.phase,shell_input_operations=0)
        key=request.get('attempt_key')
        need(isinstance(key,str) and len(key)==64 and all(c in '0123456789abcdef' for c in key),
             'invalid_original_attempt_key')
        fingerprint=hashlib.sha256(canonical(request)).hexdigest()
        if key in self.attempts:
            need(self.attempts[key].get('request_sha256')==fingerprint,'original_attempt_content_changed')
            return dict(phase='already_consumed',attempt=self.attempts[key],new_launches=0)
        need(self.revoked is None and self.phase=='awaiting_original_controller' and self.child is None,
             'original_child_not_waiting_after_exit')
        need(request.get('exit_id')==self.exit['exit_id'],'original_exit_generation_changed')
        need(callable(self.validate_resume),'original_resume_validator_unavailable')
        self.reap()
        plan=self.validate_resume(request,dict(self.record,terminal=self.terminal),self.exit.copy())
        need(isinstance(plan,dict) and plan.get('session_id') and
             plan.get('argv',[])[-2:]==['resume',plan['session_id']],'original_selected_sid_resume_required')
        self.terminal_recheck()
        child=self.spawn(plan,key)
        self.attempts[key]['request_sha256']=fingerprint
        # A lost reply or this persistence failure cannot cause another spawn:
        # spawn_intent is already durable and the child is owned/reaped here.
        self.save()
        return dict(phase='exec_released',child=child,new_launches=1,shell_input_operations=0,
                    native_resume_verified=False)

    @contextmanager
    def signals(self):
        previous={}
        def revoke(sig,frame):
            # TTY-generated signals reach the child in the same foreground
            # group directly. Never kill a process discovered elsewhere.
            self.revoked=signal.Signals(sig).name
        try:
            for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
                previous[sig]=signal.getsignal(sig);signal.signal(sig,revoke)
            yield
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)

    def serve(self,initial_plan):
        path=self.directory/'host.sock'
        need(len(os.fsencode(path))<100,'host_socket_path_too_long')
        with PinnedDirectory(self.directory) as directory:
            need(directory.read('host.json') is None,'host_identity_already_used')
        with self.signals(),socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
            self.server=server;server.bind(str(path));os.chmod(path,0o600);server.listen(4)
            identity=path.lstat()
            try:
                self.spawn(initial_plan,'initial')
                while self.phase!='finished':
                    self.reap()
                    if self.child is None:self.observe_waiting_input()
                    if self.revoked is not None and self.child is None:
                        self.phase='finished';self.save();break
                    reads=[server]
                    if self.waiting_mode and not self.waiting_input_blocked:reads.append(0)
                    ready,_,_=select.select(reads,[],[],0.05)
                    if not ready:continue
                    if 0 in ready:self.observe_waiting_input()
                    if server not in ready:continue
                    connection,_=server.accept()
                    with connection:
                        connection.settimeout(1)
                        peer=int.from_bytes(connection.getsockopt(0,2,4),sys.byteorder,signed=True)
                        try:
                            # Authenticate before consuming any command bytes.
                            need(peer==self.owner['pid'] and self.birth(peer)==self.owner['birth'],
                                 'not_original_controller')
                            data=b''
                            while b'\n' not in data and len(data)<=16384:
                                block=connection.recv(min(4096,16385-len(data)))
                                need(bool(block),'incomplete_host_request');data+=block
                            line,extra=data.split(b'\n',1)
                            need(len(data)<=16384 and not extra.strip(),'invalid_host_request_envelope')
                            response=self.handle(json.loads(line),peer)
                        except (ValueError,KeyError,TypeError,OSError) as error:
                            response={'phase':'refused_or_unknown','error':type(error).__name__+': '+str(error)[:160],
                                      'do_not_replay':True}
                        try:connection.sendall(canonical(response)+b'\n')
                        except OSError:pass # lost reply never retries execution
                return self.exit
            finally:
                if self.child is None:self.restore_entry_mode()
                # Do not unlink any replacement endpoint. Never signal a child
                # if cleanup fails or observation of the host is interrupted.
                current=path.lstat()
                if (current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino):path.unlink()
