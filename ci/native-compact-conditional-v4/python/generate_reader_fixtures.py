"""Write real Python producer files for the compiled Rust reader contract gate.

Native-shaped rollout/process fixtures are synthetic. The producer, durable
input ledgers, authority writer and file boundary checks are the real modules.
No kernel identity, native input or live model request is simulated as passed.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(args.runtime.resolve()))
    from ccc_client_thread_observation import COMPACT_INTERRUPT_CAPABILITY
    from ccc_compact_inputs import PendingInputs
    from ccc_compact_intents import IntentIndex
    from ccc_compact_native_client import pinned_file
    from ccc_compact_sensor import RolloutFacts,native_compaction_start
    from ccc_compact_support import Journal,OwnedDirectory
    from ccc_compact_watchdog import Worker,make_episode
    output=args.output.resolve()
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    cases=[]
    for name in ('no-goal','active-goal','paused-config','duplicate-enrollment','pending-input','settings-drift','disabled-recovery'):
        base=output/name;base.mkdir(mode=0o700)
        sid=str(uuid.uuid4());started=time.time()-410;now=time.time()
        stamp=lambda at: dt.datetime.fromtimestamp(at,dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        occurrence=str(uuid.uuid4())
        rollout=base/'rollout.jsonl'
        records=[{'timestamp':stamp(started-100),'type':'session_meta','payload':{'id':sid,'timestamp':stamp(started-100)}},
          {'timestamp':stamp(started-10),'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-1'}},
          {'timestamp':stamp(started-9),'type':'turn_context','payload':{'turn_id':'turn-1','model':'gpt-5.5','effort':'high'}},
          {'timestamp':stamp(started),'type':'event_msg','payload':{'type':'item_started','thread_id':sid,'turn_id':'turn-1',
            'started_at_ms':int(started*1000),'item':{'type':'ContextCompaction','id':occurrence}}}]
        rollout.write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in records));os.chmod(rollout,0o600)
        facts=RolloutFacts(rollout);sample=facts.refresh()
        db=base/'logs.sqlite'
        with sqlite3.connect(db) as sql:
            sql.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,thread_id TEXT,target TEXT,feedback_log_body TEXT)')
            sql.execute('CREATE INDEX idx_logs_thread_id ON logs(thread_id)')
            sql.execute('CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id,ts,ts_nanos)')
            sql.execute('INSERT INTO logs VALUES(1,?,?,?,?,?)',(int(started-200),0,sid,'native_fixture','created'))
        os.chmod(db,0o600)
        journal=base/sid;journal.mkdir(mode=0o700)
        index=IntentIndex(base/'native-input-intents.json',state_root=base/'mac-state',active_root=base/'active')
        inputs=PendingInputs(journal/'pending-inputs.json',sid,facts,db,intent_index=index)
        guard=inputs.refresh(final=True)
        assert guard['safe'],guard
        process=dict(pid=os.getpid(),birth=[int(started-200),123],tty='/dev/ttys_fixture')
        target=dict(session_id=sid,surface_id=str(uuid.uuid4()).upper(),workspace_id=str(uuid.uuid4()).upper(),
                    pane_id=str(uuid.uuid4()).upper(),model='gpt-5.5',effort='high',path=str(rollout),
                    process=process,identity={'tty':process['tty']},recovery_enabled=True)
        config={'mode':'armed','global_paused':False}
        enrollment={'scope':'existing_compact_sessions_180_300_v2','targets':[target]}
        if name=='paused-config': config['global_paused']=True
        if name=='duplicate-enrollment': enrollment['targets'].append(dict(target))
        if name=='pending-input': guard=dict(guard,pending=[99],safe=False)
        if name=='settings-drift': enrollment['targets'][0]=dict(target,model='other')
        if name=='disabled-recovery': enrollment['targets'][0]=dict(target,recovery_enabled=False)
        epoch=str(uuid.uuid4());fg=base/'foreground.json'
        goal=None;revision=None
        if name=='active-goal':
            goal=dict(threadId=sid,objective='original',status='active',tokenBudget=10000,tokensUsed=50,
                      timeUsedSeconds=600,createdAt=int(started-200),updatedAt=int(started-100))
            revision=dict(goal_id=str(uuid.uuid4()),created_at_ms=goal['createdAt']*1000+3,
                          updated_at_ms=goal['updatedAt']*1000+7)
        with OwnedDirectory(base) as owned:
            owned.write_once('config.json',config);owned.write_once('enrollment.json',enrollment)
            owned.write_once('foreground.json',dict(schema=1,purpose='client_foreground_thread',pid=process['pid'],
                client_epoch=epoch,thread_id=sid,compact_interrupt_capability=COMPACT_INTERRUPT_CAPABILITY,
                model=target['model'],effort=target['effort'],published_at_ms=int(now*1000)))
        turn=dict(id='turn-1',status='inProgress',startedAt=started-10)
        snap=dict(compact=native_compaction_start(sid,turn,sample),facts=sample,turn=turn,input_guard=guard,
                  goal=goal,goal_full=goal,goal_revision=revision,client_pid=process['pid'],client_epoch=epoch,
                  client_capability=COMPACT_INTERRUPT_CAPABILITY,foreground=pinned_file(fg)[0],paused=False)
        episode=make_episode(snap);episode['phase']='control_pending'
        request=dict(threadId=sid,expectedTurnId='turn-1',attemptId=episode['key'],
                     intentPath=str(journal/(episode['key']+'-interrupt.json')),
                     clientPid=process['pid'],compactionOccurrence=occurrence)
        worker=object.__new__(Worker);worker.directory=journal;worker.target=target
        worker.config_path=base/'config.json';worker.enrollment_path=base/'enrollment.json';worker.inputs=inputs
        worker.state=dict(episode=episode,authenticated_binding={'process':process})
        with OwnedDirectory(journal) as owned:
            worker.journal_directory=owned;worker.journal=Journal(journal/'events.jsonl',directory=owned)
            owned.write_once('state.json',worker.state)
            try:
                value=worker.intent(episode,'interrupt',{'threadId':sid,'turnId':'turn-1'},snap,request=request)
                producer='written';reason=None
            except ValueError as error:
                producer='rejected';reason=str(error)
        expect='written' if name in ('no-goal','active-goal') else 'rejected'
        assert producer==expect,(name,producer,reason)
        cases.append(dict(name=name,producer=producer,reason=reason,request=request,rollout=str(rollout),
                          expected_reader='accept' if producer=='written' else 'no-intent'))
    sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in args.runtime.glob('ccc*compact*.py')}
    report=dict(schema='ccc-real-python-producer-fixtures-v4',cases=cases,producer_sources=sources,
                identity_scope='synthetic process and native-shaped rollout; real Python producer/files/input ledgers',
                rust_reader='NOT_RUN',live_native_admission='NOT_RUN')
    (output/'manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'cases':len(cases),'written':sum(c['producer']=='written' for c in cases),
                      'rejected':sum(c['producer']=='rejected' for c in cases),'manifest':str(output/'manifest.json')}))

if __name__=='__main__': main()
