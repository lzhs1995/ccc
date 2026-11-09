"""Bounded reconciliation of recorded fixture generations after outer failure."""
import json
import os
from pathlib import Path
import fixture_common as f
from host_runtime import terminate_native


def reconcile(root):
    result={}
    # No process inventory, name lookup or arbitrary PID supplied by a caller.
    sources=(('native',root/'CLIENT.json'),('gui',root/'g/GEN-1-READY.json'))
    for kind,path in sources:
        if not path.is_file():
            result[kind]=dict(status='UNKNOWN_NO_ORIGINAL_IDENTITY_NO_SIGNAL')
            continue
        try:
            row=json.loads(f.strict_file(path))
            row=row['peer'] if kind=='gui' else row
            identity={k:row[k] for k in ('pid','birth','executable')}
            expected=(root/'bin/codex') if kind=='native' else Path(json.loads(f.strict_file(root/'FIXED-APP-ADMISSION.json'))['exe'])
            f.need(identity['executable']==str(expected) and expected.is_relative_to(root),'foreign recorded cleanup path')
            result[kind]=terminate_native(root,identity,'EMERGENCY-'+kind.upper())
        except BaseException as error:
            result[kind]=dict(status='FAILED_NO_BROAD_SIGNAL',error=str(error))
    if all(result[k].get('original_gone') is True for k in ('native','gui')):
        path=Path('/Applications/cmux.app')
        if path.is_symlink():
            original=json.loads(f.strict_file(root/'ALIAS-CREATED.json'))['identity']
            info=path.lstat()
            f.need([info.st_dev,info.st_ino,os.readlink(path)]==original,'refuse changed emergency alias')
            path.unlink();result['alias_removed']=True
    f.save_once(root/'EMERGENCY-CLEANUP.json',result)
    return result
