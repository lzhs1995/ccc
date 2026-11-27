"""One-use child launcher for a proved original, now-exited Codex session.

Called in its original shell by the original controller, never as a new PTY.
This child replaces only itself with the captured official client. It does not
signal an agent, exit the parent shell, change configuration or send a prompt.
"""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import hashlib
import json
import os
from pathlib import Path
import stat

from ccc_herdr_exit_context import PinnedDirectory, canonical, need, PROTECTED
from ccc_original_exit_boundary import generation_state, digest
from ccc_original_shell_boundary import no_writer


def launch_checked(capsule, directory, *, scope, writer_module, current_plan,
                   current_inputs, policy_check, executor=os.execve, holder_reader=None):
    """Dependencies use the original release; injection exists for isolated tests."""
    document=capsule['document'];plan=capsule['plan'];shell=capsule['shell']
    need(capsule.get('schema')=='ccc-original-exit-launch-capsule-v1', 'unsupported_launch_capsule')
    policy_check()
    need(document['binding']['pid']!=PUBLIC_PROTECTED_PID and document['binding']['cmux_surface_id'].upper()!=PROTECTED,
         'protected_business_surface')
    need(generation_state(scope,document['binding']['pid'],document['binding']['birth'])['state']=='exited',
         'original_generation_not_exited')
    need(os.getppid()==shell['shell_pid'] and scope.birth(os.getppid())==shell['shell_birth'],
         'original_parent_shell_changed')
    need(os.isatty(0) and os.isatty(1) and os.ttyname(0)==os.ttyname(1)==('/dev/'+shell['tty'])
         and os.tcgetpgrp(0)==os.getpgrp(), 'original_foreground_terminal_changed')
    server=document['binding']['server'];owner=capsule['owner']
    need(scope.birth(server['pid'])==server['birth'] and scope.birth(owner['pid'])==owner['birth'],
         'original_controller_or_server_changed')
    need(current_plan(document)==plan, 'original_launch_plan_changed')
    need(current_inputs(document,plan)==capsule['current_inputs'], 'original_current_inputs_changed')
    kwargs={} if holder_reader is None else {'holder_reader':holder_reader}
    no_writer(document,writer_module,**kwargs)
    executable=Path(plan['argv'][0])
    need(executable==Path(document['executable']) and executable.resolve(strict=True)==executable,
         'original_executable_changed')
    fd=os.open(executable,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and not before.st_mode&0o022,
             'original_executable_not_regular')
        sha=hashlib.file_digest(stream,'sha256').hexdigest();after=os.fstat(stream.fileno())
    identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    need(sha==document['executable_sha256'] and identity(before)==identity(after)==identity(executable.lstat()),
         'original_executable_digest_changed')
    need(plan['argv'][-2:]==['resume',document['session_id']], 'not_exact_original_sid_resume')
    need(Path(plan['cwd']).resolve(strict=True)==Path(plan['cwd']), 'original_resume_cwd_changed')
    # This receipt is a launch attempt, not proof that exec or native resume
    # succeeded. Its PID/birth links a later real native receipt to this input.
    receipt=dict(schema='ccc-original-exec-attempt-v1',launch_attempt_key=capsule['attempt_key'],
        session_id=document['session_id'],pid=os.getpid(),birth=scope.birth(os.getpid()),
        shell=shell,plan_sha256=digest(plan),phase='exec_intent',native_resume_verified=False)
    need(isinstance(receipt['birth'],list), 'launcher_native_birth_unavailable')
    with PinnedDirectory(directory) as pinned:
        need(pinned.read(capsule['attempt_key']+'.exec.json') is None,
             'original_launch_already_consumed')
        need(pinned.publish(capsule['attempt_key']+'.exec.json',canonical(receipt)+b'\n') is True,
             'original_launch_already_consumed')
        need(generation_state(scope,document['binding']['pid'],document['binding']['birth'])['state']=='exited'
             and scope.birth(shell['shell_pid'])==shell['shell_birth']
             and scope.birth(owner['pid'])==owner['birth'], 'generation_changed_before_exec')
        no_writer(document,writer_module,**kwargs)
        policy_check()
        need(identity(executable.lstat())==identity(before),'original_client_replaced_before_exec')
        os.chdir(plan['cwd'])
        executor(str(executable),plan['argv'],plan['environment'])


def original_policy_check(config,capsule,transport):
    """Final policy/Goal/history fence; no native client or model call."""
    document=capsule['document']
    need(config.get('original_exit_recovery',{}).get('resume_original_after_error_exit') is True,
         'original_exit_recovery_policy_disabled')
    current=Path(config['ccc_config']).read_bytes()
    need(hashlib.sha256(current).hexdigest()==capsule['policy']['sha256'],
         'original_continuation_policy_changed')
    endpoint=next(e for e in config['observer']['endpoints']
                  if e['session']==document['binding']['session'])
    policy=json.loads(current);allowed,_=transport.policy(policy,endpoint)
    need(config.get('armed') is True and policy.get('mode')=='armed' and allowed
         and endpoint.get('discovered_read_only') is not True,
         'original_continuation_policy_disabled')
    goal=transport.r40_original_goal(str(Path(document['configuration_evidence']['database_locator']['path']).parent),
                                     document['session_id'])
    need(goal==capsule['goal'] and goal['status']=='active','original_goal_changed_before_exec')
    history=transport.r40_original_history(document['history']['path'],document['session_id'])
    need(history['fence']==capsule['expected_history_fence']
         and history['last_native_event']==capsule['source_event'],'original_native_history_changed_before_exec')


def main():
    import argparse
    import sys
    parser=argparse.ArgumentParser()
    parser.add_argument('--capsule',required=True)
    parser.add_argument('--sha256',required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    config=json.loads((root/'guard-config.json').read_bytes())
    source=Path(args.capsule)
    directory=Path(config['state'])/'original-exit-recovery'
    need(source.parent==directory,'capsule_outside_original_state')
    with PinnedDirectory(directory) as pinned:
        raw=pinned.read(source.name)
        need(raw is not None and hashlib.sha256(raw).hexdigest()==args.sha256,'original_capsule_changed')
        capsule=json.loads(raw)
    need(capsule['owner']['service_release']==str(root), 'original_launch_release_changed')
    sys.path.insert(0,config['observer']['dependency_runtime'])
    import ccc_guard_scope as scope
    import ccc_original_writer_check as writer
    from ccc_thread_settings_evidence import read_settings
    from ccc_resume_metadata import read_metadata
    from ccc_original_resume_inputs import normalized_plan
    from ccc_original_current_inputs import current_inputs
    import ccc_herdr_guard as transport
    def fresh_plan(document):
        settings=read_settings(document)
        metadata=read_metadata(document,document['configuration_evidence']['database_locator'])
        return normalized_plan(document,settings,metadata)
    def policy_check():
        original_policy_check(config,capsule,transport)
    launch_checked(capsule,directory,scope=scope,writer_module=writer,
                   current_plan=fresh_plan,current_inputs=current_inputs,policy_check=policy_check)


if __name__=='__main__':
    try:main()
    except Exception as error:
        # Do not include environment, provider tokens or complete arguments.
        raise SystemExit('CCC original recovery held: '+type(error).__name__+': '+str(error)[:160])
