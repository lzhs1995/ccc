"""Re-use original exit intent/config/Goal checks for an owned child host."""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import hashlib
import json
import os
from pathlib import Path
import stat

from ccc_herdr_exit_context import PinnedDirectory, canonical, need, PROTECTED
from ccc_original_exit_boundary import OriginalExitAttempts, digest, generation_state
from ccc_original_exit_driver import fresh_plan
from ccc_original_current_inputs import current_inputs
from ccc_original_launch import original_policy_check
from ccc_original_shell_boundary import no_writer
from ccc_hosted_provider_budget import verify_reserved_provider
import ccc_original_writer_check as writer


class HostedResumeValidator:
    """Only consume the original controller's already-durable launch capsule.

    Reaping a child proves its exit, not the selected SID or why it exited.
    Those still come from the original captured reader, terminal event and
    Goal. This callable has no input, Goal write, refund or route mutation.
    """
    def __init__(self, config, *, scope, transport, owner_recheck,
                 plan_reader=fresh_plan, inputs_reader=current_inputs,
                 writer_reader=None,provider_reader=verify_reserved_provider):
        self.config=config;self.scope=scope;self.transport=transport
        self.owner_recheck=owner_recheck;self.plan_reader=plan_reader;self.inputs_reader=inputs_reader
        self.writer_reader=writer_reader or (lambda doc:no_writer(doc,writer))
        self.provider_reader=provider_reader

    def __call__(self,request,host,exit_record):
        self.owner_recheck()
        need(set(request)=={'op','attempt_key','exit_id','capsule','sha256'},'unreviewed_host_request_fields')
        need(exit_record['exit_status']>0 and request['exit_id']==exit_record['exit_id'],
             'host_exit_not_error_generation')
        directory=Path(self.config['state'])/'original-exit-recovery'
        path=Path(request['capsule'])
        need(path.parent==directory and path.name==request['sha256']+'.capsule.json',
             'host_capsule_outside_original_state')
        with PinnedDirectory(directory) as pinned:
            raw=pinned.read(path.name)
            need(raw is not None and hashlib.sha256(raw).hexdigest()==request['sha256'],
                 'host_capsule_digest_changed')
            capsule=json.loads(raw)
            intent_raw=pinned.read(request['attempt_key']+'.json')
            need(intent_raw is not None,'original_exit_intent_missing')
            intent=json.loads(intent_raw)
            need(intent.get('schema')=='ccc-original-exit-attempt-v1'
                 and intent.get('phase')=='input_intent'
                 and intent.get('attempt_key')==request['attempt_key']
                 and intent.get('plan_sha256')==digest(intent['plan'])
                 and OriginalExitAttempts.attempt_key(intent['plan'])==request['attempt_key']
                 and intent['plan']['capsule']==str(path)
                 and intent['plan']['capsule_sha256']==request['sha256'],
                 'original_exit_intent_not_current')
            need(capsule.get('schema')=='ccc-original-hosted-launch-capsule-v1'
                 and capsule.get('attempt_key')==request['attempt_key']
                 and capsule.get('host')==host and capsule.get('host_exit')==exit_record,
                 'original_host_or_exit_capsule_changed')
            doc=capsule['document'];plan=capsule['plan'];binding=doc['binding']
            need(binding['pid']==exit_record['child']['pid']
                 and binding['birth']==exit_record['child']['birth']
                 and binding['pid']!=PUBLIC_PROTECTED_PID and binding['cmux_surface_id'].upper()!=PROTECTED,
                 'capsule_not_original_owned_child')
            need(intent['session_id']==doc['session_id']==plan['session_id']
                 and intent['plan']['binding']==binding
                 and intent['plan']['source_event']==capsule['source_event']
                 and capsule['source_event'].get('kind')=='task_complete'
                 and capsule['source_event'].get('error'), 'original_selected_error_event_unverified')
            need(generation_state(self.scope,binding['pid'],binding['birth'])['state']=='exited',
                 'original_child_not_exited')
            need(capsule['owner']==host['owner']
                 and self.scope.birth(host['owner']['pid'])==host['owner']['birth'],
                 'original_host_controller_changed')
            original_policy_check(self.config,capsule,self.transport)
            self.provider_reader(self.config,directory,request['attempt_key'],capsule)
            need(self.plan_reader(doc)==plan,'original_host_resume_plan_changed')
            need(self.inputs_reader(doc,plan)==capsule['current_inputs'],'original_host_launch_inputs_changed')
            self.writer_reader(doc)
            executable=Path(plan['argv'][0])
            need(str(executable)==doc['executable'] and executable.resolve(strict=True)==executable,
                 'original_official_client_path_changed')
            fd=os.open(executable,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as stream:
                before=os.fstat(stream.fileno())
                need(stat.S_ISREG(before.st_mode) and not before.st_mode&0o022,'original_client_not_regular')
                sha=hashlib.file_digest(stream,'sha256').hexdigest();after=os.fstat(stream.fileno())
            identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
            need(sha==doc['executable_sha256'] and identity(before)==identity(after)==identity(executable.lstat()),
                 'original_official_client_digest_changed')
            # Recheck mutable authorization after local reads/hashing. The host
            # separately checks foreground ownership just before fork/release.
            original_policy_check(self.config,capsule,self.transport)
            self.writer_reader(doc);self.owner_recheck()
            self.provider_reader(self.config,directory,request['attempt_key'],capsule)
            need(pinned.read(path.name)==raw and pinned.read(request['attempt_key']+'.json')==intent_raw,
                 'original_intent_changed_during_host_validation')
        return plan
