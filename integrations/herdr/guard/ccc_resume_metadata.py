"""Original SID metadata through a read-only WAL-aware SQLite transaction.

No client/server is started, no schema migration or fallback database is used.
The caller supplies a database locator authenticated from the original native
process (or an earlier such capture). Historical provenance stays historical.
"""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

FIELDS = ('id','rollout_path','model','model_provider','reasoning_effort',
          'cwd','updated_at','updated_at_ms','archived','cli_version')
QUERY = 'SELECT '+','.join(FIELDS)+' FROM threads WHERE id = ? LIMIT 2'


def need(value, reason):
    if not value:
        raise ValueError(reason)


def inode(info):
    return info.st_dev,info.st_ino


def read_metadata(document, database):
    result=dict(schema='ccc-original-resume-metadata-v1',state='UNKNOWN',
        database_row_writes=0, schema_writes=0, model_requests=0, client_starts=0,
        current_thread_authority=False, execution_authorized=False,
        connection_fd_identity_verified=False,
        concurrent_path_replacement_excluded=False,
        sqlite_internal_sidecar_activity_excluded=False,
        database_binding_scope='native locator plus namespace checks before/after; '
            'Python SQLite reopens the pathname independently')
    dbfd=None
    conn=None
    try:
        need(document.get('schema')=='ccc-original-exit-context-v1','wrong_context_schema')
        sid=document['session_id']
        need(isinstance(sid,str) and str(uuid.UUID(sid))==sid,'invalid_original_sid')
        need(document['binding'].get('pid')!=PUBLIC_PROTECTED_PID
             and document['binding'].get('cmux_surface_id','').upper()
             !=PUBLIC_PROTECTED_SURFACE,'protected_business_surface')
        need(database.get('source') in {'original_native_open_file','original_native_sqlite_sibling'}
             and database.get('pid')==document['binding']['pid']
             and database.get('birth')==document['binding']['birth'], 'database_locator_unbound')
        path=Path(database['path'])
        need(path.is_absolute() and path.resolve(strict=True)==path,'database_path_not_canonical')
        anchor=database.get('sqlite_home_anchor')
        def check_anchor():
            if database['source']!='original_native_sqlite_sibling':return
            need(isinstance(anchor,dict),'native_sqlite_home_anchor_missing')
            queue=Path(anchor['path']);q=queue.lstat()
            need(queue.name=='queue_1.sqlite' and path==queue.parent/'state_5.sqlite'
                 and queue.resolve(strict=True)==queue
                 and inode(q)==(anchor['device'],anchor['inode'])
                 and stat.S_ISREG(q.st_mode) and q.st_uid==os.getuid()
                 and not q.st_mode&0o022,'native_sqlite_home_anchor_changed')
        check_anchor()
        dbfd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        info=os.fstat(dbfd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
             and not info.st_mode&0o022 and inode(info)==(database['device'],database['inode']),
             'database_kernel_identity_changed')
        header=os.pread(dbfd,100,0)
        need(header.startswith(b'SQLite format 3\0') and len(header)==100,'invalid_sqlite_header')
        sidecars={}
        if header[18:20]==b'\x02\x02':
            # Refuse already missing sidecars. Their existence check is not
            # atomic with SQLite opening the pathname below, and mode=ro does
            # not exclude SQLite's internal WAL read locks/SHM activity.
            for suffix in ('-wal','-shm'):
                sidecar=Path(str(path)+suffix)
                s=sidecar.lstat()
                need(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid()
                     and not s.st_mode&0o022,'unsafe_sqlite_sidecar')
                sidecars[str(sidecar)]=inode(s)
        # mode=ro sees committed WAL frames; immutable=1 would miss them.
        conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1,isolation_level=None)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('BEGIN')
        definition=conn.execute("SELECT type,sql FROM sqlite_schema WHERE name='threads'").fetchall()
        need(len(definition)==1 and definition[0][0]=='table'
             and 'VIRTUAL' not in definition[0][1].upper(),'unsupported_threads_table')
        query_plan=conn.execute('EXPLAIN QUERY PLAN '+QUERY,(sid,)).fetchall()
        need(any('SEARCH threads' in item[3] and '(id=?)' in item[3]
                 for item in query_plan),'original_sid_lookup_not_indexed')
        rows=conn.execute(QUERY,(sid,)).fetchall()
        need(len(rows)==1,'original_metadata_missing_or_ambiguous')
        row=dict(zip(FIELDS,rows[0]))
        need(row['id']==sid and row['rollout_path']==document['history']['path'],
             'metadata_original_history_mismatch')
        need(row['archived']==0,'original_thread_archived')
        need(all(isinstance(row[k],str) and row[k] for k in ('model','model_provider','cwd')),
             'metadata_model_provider_or_cwd_missing')
        need(Path(row['cwd']).is_absolute()
             and (row['reasoning_effort'] is None or isinstance(row['reasoning_effort'],str))
             and type(row['updated_at']) is int
             and (row['updated_at_ms'] is None or type(row['updated_at_ms']) is int),
             'invalid_metadata_values')
        data_version=conn.execute('PRAGMA data_version').fetchone()[0]
        schema_sha=hashlib.sha256(definition[0][1].encode()).hexdigest()
        conn.execute('ROLLBACK')
        # The DB is active: content/WAL changes are normal. Namespace swaps are
        # not. This is a bounded identity check, not atomic exclusion of writers.
        need(inode(path.lstat())==inode(info)==inode(os.fstat(dbfd))
             and path.resolve(strict=True)==path,'database_replaced_during_read')
        need(all(inode(Path(p).lstat())==ident for p,ident in sidecars.items()),
             'sqlite_sidecar_replaced_during_read')
        check_anchor()
        hist=Path(row['rollout_path']).stat()
        need(inode(hist)==(document['history']['device'],document['history']['inode']),
             'metadata_rollout_inode_changed')
        result.update(state='ORIGINAL_METADATA_OBSERVED',session_id=sid,metadata=row,
            database=dict(database),schema_sha256=schema_sha,
            sqlite_data_version=data_version,transaction='mode=ro; query_only; BEGIN read snapshot; ROLLBACK',
            query=QUERY,whole_table_scan=False,wal_visible=True)
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        result.update(reason=str(error),error_type=type(error).__name__)
    finally:
        if conn is not None:conn.close()
        if dbfd is not None:os.close(dbfd)
    return result


def logical_fidelity(document, settings, metadata):
    """Compare independent persisted inputs; do not conflate route fidelity."""
    result=dict(state='UNKNOWN',logical_session_fidelity=False,
        provider_route_fidelity='SEPARATE_CHECK_REQUIRED',execution_authorized=False)
    if (settings.get('status')!='PERSISTED_THREAD_SETTINGS_OBSERVED'
            or metadata.get('state')!='ORIGINAL_METADATA_OBSERVED'
            or settings.get('session_id')!=document['session_id']
            or metadata.get('session_id')!=document['session_id']):
        return dict(result,reason='missing_original_persisted_evidence')
    row=metadata['metadata'];snapshot=settings['snapshot']['settings']
    keys={'model':'model','model_provider':'model_provider_id','reasoning_effort':'reasoning_effort'}
    mismatch=[key for key,other in keys.items() if row.get(key)!=snapshot.get(other)]
    if mismatch:return dict(result,reason='persisted_model_settings_conflict',mismatches=mismatch)
    return dict(result,state='PERSISTED_MODEL_TRIPLE_MATCHES',logical_session_fidelity=True,
                session_id=document['session_id'],model=row['model'],provider=row['model_provider'],
                effort=row['reasoning_effort'],reason='native_snapshot_and_original_metadata_agree',
                database_connection_fd_identity_verified=metadata.get('connection_fd_identity_verified') is True,
                historical_observation_only=True)
