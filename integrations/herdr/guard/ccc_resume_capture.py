"""Supplement the original historical capture; never a second observer/writer."""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
from pathlib import Path
import time
from ccc_resume_metadata import read_metadata, logical_fidelity
from ccc_thread_settings_evidence import read_settings


def collect(guard, document):
    result=dict(schema='ccc-original-resume-configuration-capture-v1',state='UNKNOWN',
        input_operations=0,execution_authorized=False,current_thread_authority=False)
    try:
        binding=document['binding']
        if (binding['pid']==PUBLIC_PROTECTED_PID or binding['cmux_surface_id'].upper()
                ==PUBLIC_PROTECTED_SURFACE):
            raise ValueError('protected_business_surface')
        if guard.scope.birth(binding['pid'])!=binding['birth']:
            raise ValueError('original_process_changed_before_configuration_capture')
        # Reuse the guard's existing native double descriptor inventory. Its
        # process context already supplies the selected writer proof. The
        # deployed lsof lookup failed while isolated calls succeeded; this
        # path does not require an additional subprocess to find the database.
        observed=guard.scope.native.process_writable_files(binding['pid'],
            identities=True,writer_identity_only=True)
        files={(str(path),identity['device'],identity['inode'])
               for path,identity in observed.items() if Path(path).name=='state_5.sqlite'}
        source='original_native_open_file'
        anchor=None
        if not files:
            # Codex 0.164.0 SqliteConfig places queue and state in the same
            # sqlite_home. Logs can use a separate log_dir, so never use them.
            queues=[(Path(path),identity) for path,identity in observed.items()
                    if Path(path).name=='queue_1.sqlite']
            if len(queues)!=1:raise ValueError('original_process_sqlite_home_not_unique')
            queue,identity=queues[0]
            if queue.resolve(strict=True)!=queue:raise ValueError('queue_anchor_not_canonical')
            q=queue.lstat()
            if (q.st_dev,q.st_ino)!=(identity['device'],identity['inode']):
                raise ValueError('queue_anchor_identity_changed')
            path=str(queue.parent/'state_5.sqlite')
            state_info=Path(path).lstat()
            device,inode=state_info.st_dev,state_info.st_ino
            source='original_native_sqlite_sibling'
            anchor=dict(path=str(queue),**identity)
        else:
            if len(files)!=1:raise ValueError('original_process_database_not_unique')
            path,device,inode=next(iter(files))
        locator=dict(source=source,pid=binding['pid'],birth=binding['birth'],
                     path=path,device=device,inode=inode,observed_at_epoch=time.time(),
                     observation_method='original_native_double_writer_inventory',
                     sqlite_home_anchor=anchor)
        metadata=read_metadata(document,locator)
        settings=read_settings(document)
        if guard.scope.birth(binding['pid'])!=binding['birth']:
            raise ValueError('original_process_changed_during_configuration_capture')
        fidelity=logical_fidelity(document,settings,metadata)
        result.update(database_locator=locator,
            metadata=metadata,settings=settings,logical_fidelity=fidelity,
            provider_route_fidelity='REQUIRES_CURRENT_INPUTS_AT_RECOVERY',
            historical_endpoint='UNKNOWN_UNLESS_SEPARATELY_CAPTURED')
        if fidelity.get('logical_session_fidelity') is not True:
            result['reason']=fidelity.get('reason','original_configuration_incomplete')
        else:
            result['state']='HISTORICAL_CONFIGURATION_OBSERVED'
    except (OSError,ValueError,KeyError,TypeError,AttributeError,AssertionError,RuntimeError) as error:
        result.update(reason=str(error)[:240],error_type=type(error).__name__)
    return result
