"""Read-only native checks before an original terminal recovery.

The whole native shell, exact default prompt and unchanged frame are observed.
They cannot prove that ZLE is active or that its BUFFER is empty: a shell
builtin such as read can present the same frame in the same process.
No suffix matching, clear-screen, Ctrl-C, Enter or terminal mode repair exists
here. The host has no input CAS; this function does not claim atomic exclusion
of simultaneous human typing. The sender must recheck immediately before input.
"""
import hashlib
import os
from pathlib import Path
import pwd
import socket
import subprocess

from ccc_herdr_exit_context import need


def shell_frame(response, pane, expected_prompt):
    result=response.get('result',{})
    # Herdr wraps pane.read as result.read in protocol 22.
    if 'read' in result:result=result['read']
    need(isinstance(result,dict) and all(result.get(k)==pane.get(k)
         for k in ('pane_id','workspace_id','tab_id')),'shell_frame_binding_changed')
    need(result.get('source')=='visible' and result.get('format')=='text'
         and result.get('truncated') is False and type(result.get('revision')) is int,
         'shell_frame_incomplete')
    text=result.get('text')
    need(isinstance(text,str) and len(text.encode())<=128*1024
         and not any(c in text for c in '\0\x1b\r'), 'shell_frame_not_plain_text')
    lines=text.splitlines()
    while lines and not lines[-1].strip():lines.pop()
    need(bool(lines) and lines[-1].rstrip(' ')==expected_prompt,
         'shell_input_nonempty_or_prompt_unrecognized')
    return dict(revision=result['revision'],sha256=hashlib.sha256(text.encode()).hexdigest(),
        expected_prompt=expected_prompt,source='exact_native_shell_and_visible_frame',
        input_cas_available=False, editor_empty_verified=False)


def observe_shell(guard, transport, endpoint, pane, expected_terminal):
    server=expected_terminal['binding']['server']
    first=transport.terminal_program_observation(guard,endpoint,pane,server)
    fields=('phase','binding','shell_pid','foreground_pgid','processes','tty')
    need(first.get('phase')=='shell_foreground' and all(first.get(k)==expected_terminal.get(k)
         for k in fields),'original_shell_changed')
    pid=first['shell_pid'];process=guard.process(pid)
    need(process['executable']=='/bin/zsh','shell_type_requires_review')
    cwd=guard.scope.cwd(pid)
    home=pwd.getpwuid(os.getuid()).pw_dir
    display='~' if cwd==home else Path(cwd).name or '/'
    # Default zsh %n@%m %1~ %#. Other prompts remain UNKNOWN; do not inspect
    # the suffix alone, because a draft can itself end with a percent sign.
    expected=pwd.getpwuid(os.getuid()).pw_name+'@'+socket.gethostname().split('.')[0]+' '+display+' %'
    params={'pane_id':pane['pane_id'],'source':'visible','format':'text','strip_ansi':True}
    one=shell_frame(transport.request(endpoint,server,'pane.read',params,guard.scope),pane,expected)
    two=shell_frame(transport.request(endpoint,server,'pane.read',params,guard.scope),pane,expected)
    need(one==two,'shell_input_changed_during_observation')
    again=transport.terminal_program_observation(guard,endpoint,pane,server)
    need(all(again.get(k)==first.get(k) for k in fields)
         and guard.process(pid)==process and guard.scope.cwd(pid)==cwd,'shell_generation_or_directory_changed')
    return dict(schema='ccc-original-shell-frame-observation-v2',state='DEFAULT_PROMPT_FRAME_OBSERVED',
        shell_pid=pid,shell_birth=process['birth'],tty=first['tty'],cwd=cwd,
        binding=first['binding'],frame=one,input_cas_available=False,
        editor_empty_verified=False, reason='shell_editor_phase_unavailable')


class ShellEditorPhaseUnavailable(ValueError):
    def __init__(self, observation):
        super().__init__('shell_editor_phase_unavailable')
        self.observation=observation


def inspect_shell(guard, transport, endpoint, pane, expected_terminal):
    """Do not let any caller treat the current screen reader as admission."""
    observation=observe_shell(guard,transport,endpoint,pane,expected_terminal)
    raise ShellEditorPhaseUnavailable(observation)


def writable_holders(history_path, *, runner=subprocess.run):
    """One full kernel vnode query; does not scan or tail transcript contents."""
    result=runner(['/usr/sbin/lsof','-nP','-F0pfa',str(history_path)],
        capture_output=True,timeout=8)
    need(result.returncode in (0,1) and not result.stderr.strip(),
         'original_history_holder_inventory_unavailable')
    need(len(result.stdout)<=2*1024*1024,'original_history_holder_inventory_too_large')
    if result.returncode==1:
        need(not result.stdout.strip(),'ambiguous_lsof_empty_result')
        return []
    pid=None;fd=None;holders=[];saw_process=False;access_seen=False
    for raw in result.stdout.split(b'\0'):
        field=raw.lstrip(b'\n')
        if not field:continue
        code,value=field[:1],field[1:]
        if code==b'p':
            need(fd is None or access_seen,'holder_access_mode_missing')
            need(value.isdigit(),'invalid_holder_pid');pid=int(value);fd=None;saw_process=True
        elif code==b'f':
            need(fd is None or access_seen,'holder_access_mode_missing')
            need(pid is not None and value.isdigit(),'holder_file_without_process')
            fd=value.decode('ascii');access_seen=False
        elif code==b'a':
            need(pid is not None and fd is not None and value in {b'r',b'w',b'u'},
                 'holder_access_mode_unknown')
            access_seen=True
            if value in {b'w',b'u'}:holders.append({'pid':pid,'fd':fd,'access':value.decode()})
        else:
            raise ValueError('unexpected_holder_inventory_field')
    need(saw_process and fd is not None and access_seen,'missing_holder_inventory_records')
    return holders


def no_writer(document, writer_module, *, holder_reader=writable_holders):
    target={'agent':'codex','sid':document['session_id'],'history':document['history']['path']}
    first=writer_module.inspect_writer(target,codex_home=document['codex_home'])
    need(first.get('state')=='IDLE_AT_CHECK','original_native_writer_not_idle')
    holders=holder_reader(target['history'])
    need(not holders,'original_history_has_writable_holder')
    again=writer_module.inspect_writer(target,codex_home=document['codex_home'])
    need(again==first,'original_native_writer_changed')
    return dict(schema='ccc-original-writer-absence-observation-v1',state='NO_WRITER_OBSERVED',
        session_id=document['session_id'],native_home=first['codex_home'],
        native_lock_reason=first['reason'],writable_vnode_holders=holders,
        atomic_exclusion=False,final_native_lock_required=True)
