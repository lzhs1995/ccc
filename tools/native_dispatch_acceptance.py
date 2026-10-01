#!/usr/bin/env python3
"""Real native CLI/real cmux acceptance, fresh local provider and CCC only.

The control proxy records forwarding/ACK times and rejects every mutation
outside the newly owned workspace(s). It never fabricates a viewport/native
event and never retries a forwarded RPC. Existing identities are only read.
"""
import argparse
from collections import Counter
import datetime
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load_source(source):
    sys.path.insert(0, str(source))
    import cmux_codex_watch as core
    import ccc_workspace_batch as batch
    import ccc_codex_queue as native
    import ccc_guard_scope as scope
    return core, batch, native, scope


def wrapper(root, argv):
    settings = json.loads((root/'settings.json').read_text())
    commands = {'ping', 'capabilities', 'tree', 'top', 'read-screen', 'replay',
                'list-workspaces', 'list-surfaces', 'list-panes', 'identify'}
    command = next((a for a in argv if a in commands), None)
    read_rpc = (len(argv)==4 and argv[:2]==['--json','rpc']
                and argv[2] in {'debug.terminals','terminal.replay','surface.read_text'})
    # No mutation is permitted through CLI fallback, regardless of arguments.
    forbidden = {'send', 'send-key', 'new-surface', 'new-workspace', 'close-workspace',
                 'close-surface', 'respawn-pane', 'rename-tab', 'set-option', 'rpc'}
    if not read_rpc and (command is None or forbidden.intersection(argv)):
        with (root/'denied-cli.ndjson').open('a') as f:
            f.write(json.dumps(argv)+'\n')
        print('fixture CLI denies mutation/unknown command', file=sys.stderr)
        return 2
    result = subprocess.run([settings['cmux_binary'], *argv], capture_output=True)
    if result.returncode == 0 and command == 'capabilities':
        value = json.loads(result.stdout)
        value['socket_path'] = str(root/'rpc.sock')
        sys.stdout.write(json.dumps(value)+'\n')
    else:
        sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def daemon_main(root):
    settings = json.loads((root/'settings.json').read_text())
    core, batch, native, scope = load_source(settings['source'])
    core.DEFAULT_LOG_DIR = root/'logs'
    core.DEFAULT_LOG_CHANNEL_INCIDENT_PATH = root/'log-channel-incidents.jsonl'
    if settings.get('profiling_output'):
        import ccc_native_lanes as lanes
        original = lanes.InterpreterLane
        def measured(source, initial, cells):
            return original(source, {**initial,'profiling_output':settings['profiling_output']}, cells,
                            service='tests.native_profile_fixture:run')
        lanes.InterpreterLane = measured
    # The production run loop, source and interpreter services are unchanged.
    daemon = core.WatchDaemon(root/'ccc/config.json', root/'ccc/state.json')
    return daemon.run()


class Proxy(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 1024


class Forward(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection_id = uuid.uuid4().hex
        self.upstream = None
        try:
            while self.handle_one():
                pass
        finally:
            if self.upstream is not None:
                self.upstream.close()

    def handle_one(self):
        request = None
        phase = 'downstream_read'
        sent = False
        try:
            raw = self.rfile.readline(2*1024*1024)
            if not raw:
                return False
            request = json.loads(raw)
            method, params = request['method'], request.get('params', {})
            mutable = method in {'surface.create', 'surface.send_text', 'surface.send_key', 'terminal.input', 'terminal.paste'}
            row = {'method': method, 'params': params, 'received_at': time.time(),
                   'connection_id': self.connection_id, 'request_id': request.get('id')}
            server = self.server
            if method not in {'system.tree', 'system.top', 'terminal.replay', 'surface.read_text',
                              'surface.create', 'surface.send_text', 'surface.send_key', 'terminal.input', 'terminal.paste'}:
                raise RuntimeError('unrecognized controller method')
            wid = params.get('workspace_id')
            if mutable:
                if wid not in server.workspaces:
                    raise RuntimeError('mutation escaped owned workspace')
                if method == 'surface.create':
                    if (str(server.root/'ccc/config.json') not in params.get('initial_input', '')
                            or params.get('placement') != 'workspace' or params.get('focus') is not False):
                        raise RuntimeError('creation lacks private bootstrap/config binding')
                    # Reconciler-created workers must inherit the same private
                    # provider even if a shell rc file changes its environment.
                    command = params['initial_input'].rstrip('\r')
                    params['initial_input'] = shlex.join(['/usr/bin/env',
                        *(k+'='+v for k,v in server.environment.items()), '/bin/sh','-c',command])+'\r'
                    raw = (json.dumps(request)+'\n').encode()
                elif params.get('surface_id') not in server.surfaces:
                    raise RuntimeError('input escaped acknowledged fixture surfaces')
                if method == 'terminal.paste' and (params.get('submit_key') != 'enter'
                        or params.get('text') != server.prompt):
                    raise RuntimeError('paste escaped exact fixture check')
            if self.upstream is None:
                phase = 'upstream_connect'
                self.upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.upstream.settimeout(10)
                self.upstream.connect(server.upstream)
            # Preserve the production client's connected read/input boundary.
            # An upstream failure ends this channel; never reconnect or replay.
            outgoing = self.upstream
            row['forward_at'] = time.time()
            phase = 'upstream_send'
            outgoing.sendall(raw)
            sent = True
            phase = 'upstream_response'
            data = bytearray()
            while b'\n' not in data:
                chunk = outgoing.recv(65536)
                if not chunk:
                    raise RuntimeError('real controller response incomplete; not retried')
                data.extend(chunk)
                if len(data) > 16*1024*1024:
                    raise RuntimeError('controller response too large')
            row['ack_at'] = time.time()
            with server.record_lock:
                server.rpc_timings.append({'method':method,'surface_id':params.get('surface_id'),
                    'connection_id':self.connection_id,'request_id':request.get('id'),
                    'received_at':row['received_at'],'forward_at':row['forward_at'],
                    'ack_at':row['ack_at'],'response_bytes':len(data)})
            reply = json.loads(data.split(b'\n', 1)[0])
            row['ok'] = reply.get('ok')
            if method == 'terminal.paste':
                row['paste_result'] = reply.get('result')
            if not reply.get('ok'):
                row['error_reply'] = reply
            if method == 'surface.create' and reply.get('ok'):
                result = reply['result']
                if result.get('workspace_id') != wid:
                    raise RuntimeError('create ACK workspace mismatch')
                sid = result['surface_id']
                uuid.UUID(sid)
                server.surfaces.add(sid)
                row['surface_id'] = sid
            if mutable:
                with server.record_lock:
                    server.rows.append(row)
                    with (server.output/'controller-input.ndjson').open('a') as f:
                        f.write(json.dumps(row, ensure_ascii=False)+'\n')
            phase = 'downstream_response'
            self.wfile.write(data)
            return True
        except BaseException as exc:
            row = {'at':time.time(), 'error':f'{type(exc).__name__}: {exc}', 'request':request,
                   'connection_id': self.connection_id, 'phase': phase,
                   'upstream_sendall_returned': sent}
            with self.server.record_lock:
                self.server.errors.append(row)
            if request is not None:
                try:
                    self.wfile.write((json.dumps({'id':request.get('id'), 'ok':False,
                        'error':{'code':'fixture_boundary', 'message':str(exc)}})+'\n').encode())
                except OSError:
                    pass


class Provider(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024


class Response(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        server = self.server
        try:
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if self.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            body = json.loads(raw)
            users = [i for i in body.get('input', []) if i.get('role') == 'user']
            text = '\n'.join(p.get('text', '') for p in users[-1].get('content', []) if p.get('type') == 'input_text')
            sid = self.headers.get('thread-id')
            assert sid and text == server.prompt, 'unexpected provider input/title request'
            with server.condition:
                turn = server.seen[sid]
                server.seen[sid] += 1
                server.requests.append({'at':time.time(),'session_id':sid,'round':turn,
                    'text':text,'body_sha256':hashlib.sha256(raw).hexdigest()})
                server.arrived[turn].add(sid)
                server.condition.notify_all()
                ready = server.condition.wait_for(lambda:server.stopping or
                    server.released and len(server.arrived[turn]) == server.count, timeout=180)
                assert ready and not server.stopping, 'local cohort timeout/stopped'
            failed = turn < server.rounds
            response = {'id':'resp_'+uuid.uuid4().hex,'object':'response','status':'in_progress','output':[]}
            events = [{'type':'response.created','response':response}]
            if failed:
                events.append({'type':'response.failed','response':{**response,'status':'failed',
                    'error':{'code':'server_error','message':'We are currently experiencing high demand.'}}})
            else:
                item={'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','status':'completed',
                      'content':[{'type':'output_text','text':'OK','annotations':[]}]}
                events += [{'type':'response.output_item.added','output_index':0,'item':{**item,'status':'in_progress','content':[]}},
                           {'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':'OK'},
                           {'type':'response.output_item.done','output_index':0,'item':item},
                           {'type':'response.completed','response':{**response,'status':'completed','output':[item]}}]
            data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except BaseException as exc:
            with server.condition:
                server.errors.append({'at':time.time(),'error':f'{type(exc).__name__}: {exc}'})


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def analyze(logs, inputs):
    rows=[]
    input_uses=Counter()
    for slot in logs:
        events=[]
        for line in Path(slot['frozen_transcript']).read_text().splitlines():
            record=json.loads(line)
            if record.get('type')=='event_msg' and record.get('payload',{}).get('type') in {'task_complete','task_started'}:
                events.append({'at':timestamp(record['timestamp']),**record['payload']})
        sid=slot['surface_id']
        sent=[{**x,'input_id':str(i)} for i,x in enumerate(inputs) if x['params'].get('surface_id')==sid and x.get('ok') and
              (x['method']=='surface.send_text' and x['params'].get('text','').endswith(('\n','\r'))
               or x['method']=='surface.send_key' and x['params'].get('key')=='enter'
               or x['method']=='terminal.paste' and x['params'].get('submit_key')=='enter'
                   and isinstance(x.get('paste_result'), dict) and x['paste_result'].get('submitted') is True
               or x['method']=='terminal.input' and x['params'].get('text','').endswith('\x1b[201~\r'))]
        for index,event in enumerate(events):
            if event['type']!='task_complete' or not event.get('error'):
                continue
            later=next((e for e in events[index+1:] if e['type']=='task_started'),None)
            candidates=[x for x in sent if x['forward_at']>=event['at'] and (later is None or x['forward_at']<=later['at'])]
            row={'surface_id':sid,'session_id':slot['session_id'],'failed_turn':event.get('turn_id'),
                 'native_complete_at':event['at'],'next_turn':later.get('turn_id') if later else None,
                 'input_count':len(candidates)}
            if len(candidates)==1:
                row['input_id']=candidates[0]['input_id']
                input_uses[row['input_id']]+=1
                row.update(forward_ms=(candidates[0]['forward_at']-event['at'])*1000,
                    ack_ms=(candidates[0]['ack_at']-event['at'])*1000,
                    native_next_ms=(later['at']-event['at'])*1000 if later else None)
            rows.append(row)
    for row in rows:
        if row.get('input_id') is not None and input_uses[row['input_id']]!=1:
            row['input_count']=input_uses[row['input_id']]
            row['input_reused']=True
    return rows


def main(args):
    source=args.source.resolve();output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    core,batch,native,scope=load_source(source)
    batch.COUNT=args.slots
    from ccc_scheduling import SnapshotCache,SnapshotClient
    from ccc_guard_migration import cmux_client
    from ccc_batch_guard import native_binary
    # Darwin Unix socket names include ccc/claude-events.sock (<104 bytes).
    root=Path(tempfile.mkdtemp(prefix='ccc-native-',dir='/tmp')).resolve()
    home=root/'ccc/.codex';home.mkdir(parents=True);(home/'sessions').mkdir()
    original=scope.scan();original_env=dict(os.environ)
    report={'phase':'preparing','root':str(root),'source':str(source),'native_before':original,
            'production_writes':0,'external_model_requests':0,'count':args.workspaces*args.batches*args.slots,
            'rounds':args.rounds,'review_mode':'solo_self_review'}
    report['source_before']={n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in core.RUNTIME_FILES}
    report['fixture_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    from tools import native_acceptance_metrics
    metrics_path=Path(native_acceptance_metrics.__file__)
    report['acceptance_metrics_sha256']=hashlib.sha256(metrics_path.read_bytes()).hexdigest()
    report['native_binary_sha256']=hashlib.sha256(Path(native_binary()).read_bytes()).hexdigest()
    write(output/'result.json',report)
    real=core.CmuxClient();capabilities=real.capabilities()
    assert capabilities.get('access_mode')=='automation' and 'surface.create' in capabilities.get('methods',[])
    settings={'source':str(source),'cmux_binary':real.binary}
    if args.profile_lanes:
        settings['profiling_output']=str(output/'lane-profiles')
    write(root/'settings.json',settings)
    cli=root/'cmux-fixture'
    cli.write_text('#!/bin/sh\nexec '+shlex.join([sys.executable,'-B',str(Path(__file__).resolve()),'--cli',str(root)])+' "$@"\n')
    cli.chmod(0o700)
    proxy=Proxy(str(root/'rpc.sock'),Forward)
    proxy.root,proxy.output,proxy.upstream=root,output,capabilities['socket_path']
    proxy.workspaces=set();proxy.surfaces=set();proxy.rows=[];proxy.errors=[];proxy.rpc_timings=[];proxy.record_lock=threading.Lock()
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    server=Provider(('127.0.0.1',0),Response)
    server.condition=threading.Condition();server.seen=Counter();server.requests=[];server.errors=[]
    server.arrived={n:set() for n in range(args.rounds+2)}
    server.count=report['count'];server.rounds=args.rounds;server.prompt=batch.PROMPT
    proxy.prompt=batch.PROMPT
    server.released=False;server.stopping=False
    threading.Thread(target=server.serve_forever,daemon=True).start()
    (home/'AGENTS.md').write_text('Private loopback verification. Do not use tools.\n')
    native_config=('model="gpt-6-astra"\nmodel_provider="local_fixture"\napproval_policy="never"\nsandbox_mode="read-only"\n'
        'check_for_update_on_startup=false\n[tui]\nscreen_reader_detection_done=true\n'
        '[features]\nplugins=false\napps=false\nhooks=false\nskip_host_skill_discovery=true\n'
        '[model_providers.local_fixture]\nname="Loopback fixture"\nwire_api="responses"\n'
        f'base_url="http://127.0.0.1:{server.server_port}/v1"\nrequires_openai_auth=false\nsupports_websockets=false\n'
        'request_max_retries=0\nstream_max_retries=0\n')
    (home/'config.toml').write_text(native_config)
    config_path=root/'ccc/config.json'
    config=core.default_config();config.update(mode='armed',global_paused=False,claude_enabled=False,
        cmux_path=str(cli),targets=[],workspace_rules=[],network_guard={'enabled':False})
    core.atomic_write_json(config_path,config)
    env={'CODEX_HOME':str(home),'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost',
         **{k:'' for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy',
                           'OPENAI_API_KEY','OPENAI_BASE_URL','CODEX_SQLITE_HOME')}}
    os.environ.update(env)
    proxy.environment=env
    workers=[];threads=[];created=[];daemon=None;daemon_log=None;cache=None
    worker_errors=[];logs=[];started=None
    try:
        for i in range(args.workspaces):
            name='CCC private latency '+uuid.uuid4().hex
            command=['--json','--id-format','both','new-workspace','--name',name,'--cwd',str(root),
                     '--focus','false','--command','/bin/zsh']
            for key,value in env.items():command+=['--env',key+'='+value]
            try:
                result=real._run(command)
                (output/f'workspace-{i}-ack.txt').write_text(result.stdout)
            except core.CmuxError as exc:
                (output/f'workspace-{i}-ack.txt').write_text(str(exc))
            matches=[w['id'] for win in real.tree().get('windows',[]) for w in win.get('workspaces',[]) if w.get('title')==name]
            confirm_deadline=time.monotonic()+5
            while not matches and time.monotonic()<confirm_deadline:
                time.sleep(.05)
                matches=[w['id'] for win in real.tree().get('windows',[]) for w in win.get('workspaces',[]) if w.get('title')==name]
            assert len(matches)==1,'one-shot workspace creation not uniquely confirmed'
            wid=matches[0];created.append(wid);proxy.workspaces.add(wid)
            write(output/'workspaces.json',created)
        client=cmux_client(config_path);cache=SnapshotCache(workers=4);wrapped=SnapshotClient(client,cache)
        for wid in created:
            batch.authorize_workspace(config_path,wid,client=wrapped)
        daemon_log=(output/'watcher.log').open('w')
        daemon=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--daemon',str(root)],
            env=dict(os.environ),stdout=daemon_log,stderr=subprocess.STDOUT)
        barrier=threading.Barrier(args.workspaces+1)
        def launch_workspace(wid):
            try:
                barrier.wait()
                for n in range(args.batches):
                    job=batch.start(config_path,wid,client=wrapped,launch=False,private_check=True)
                    queue=native.QueueRecovery(root/f'initial-{wid}-{n}.json',root/'missing-hooks',home/'sessions',batch.PROMPT)
                    worker=batch.BatchWorker(config_path,job['job_id'],client=wrapped,queue=queue)
                    workers.append(worker)
                    # Forward pins the environment for every creation, including
                    # reconciled workers. A second shell/env wrapper here can
                    # exceed Darwin's canonical PTY input limit before zsh is ready.
                    worker.run()
                    write(output/f'job-{wid}-{n}.json',worker.job)
                    assert worker.job['status']=='complete',batch.counts(worker.job)
            except BaseException as exc:
                worker_errors.append({'workspace_id':wid,'error':str(exc),'traceback':traceback.format_exc()})
        threads=[threading.Thread(target=launch_workspace,args=(wid,),daemon=True) for wid in created]
        for thread in threads:thread.start()
        started=time.time()
        report['launch_started_at']=started
        report['launch_started_monotonic_ns']=time.monotonic_ns()
        report['startup_measurement_origin']='worker barrier; excludes UI/workspace creation/authorization/daemon startup'
        write(output/'result.json',report)
        barrier.wait()
        deadline=time.monotonic()+args.startup_timeout;last_report=0
        while any(t.is_alive() for t in threads):
            assert not worker_errors,worker_errors
            assert daemon.poll() is None,'watcher exited during startup'
            assert time.monotonic()<deadline,'native startup exceeded fixture deadline'
            if time.monotonic()-last_report>=5:
                print(json.dumps({'stage':'startup','seconds':time.time()-started,
                    'jobs':len(workers),'counts':[batch.counts(w.job) for w in workers],
                    'local_requests':len(server.requests)}),flush=True)
                last_report=time.monotonic()
            time.sleep(.05)
        assert not worker_errors,worker_errors
        slots=[dict(s,workspace_id=w.job['workspace_id'],job_id=w.job['id']) for w in workers for s in w.job['slots']]
        assert len(slots)==server.count and all(s['phase']=='confirmed' for s in slots)
        owned=[r for r in scope.scan() if r['environment_workspace_id'] in created]
        assert len(owned)==server.count and all(scope.arguments(r['pid'])[1].get('CODEX_HOME')==str(home) for r in owned)
        report.update(owned=owned,startup_seconds=time.time()-started,
            create_last_ack_ms=(max(s['create_acknowledged_at'] for s in slots)-started)*1000,
            first_last_submit_ms=(max(s['submit_at'] for s in slots)-started)*1000)
        # Service is live before failures are released. Allow its original
        # native ownership transfer to finish; do not omit cold startup timings.
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            health=core.load_json(root/'ccc/monitoring-health.json',{})
            state=core.load_json(root/'ccc/state.json',{})
            if (len(state)>=server.count and health.get('scheduler',{}).get('native_owned_targets')==server.count
                    and health.get('scheduler',{}).get('native_event_monitored',0)>=server.count):
                break
            time.sleep(.1)
        report['before_release_state_targets']=len(state)
        report['before_release_scheduler']=health.get('scheduler',{})
        assert health.get('scheduler',{}).get('native_event_monitored',0)>=server.count,'not all real natives monitored before failure'
        runtime_samples=[]
        for identity in owned[:3]:
            sample=subprocess.run(['ps','-M','-p',str(identity['pid']),'-o','pid=,pcpu=,state='],
                                  capture_output=True,text=True,timeout=5)
            (output/f"native-threads-{identity['pid']}.txt").write_text(sample.stdout)
            # macOS -M retains its USER/PID header even with -o fields ending '='.
            thread_rows=[line for line in sample.stdout.splitlines()
                         if len(line.split())>=3 and line.split()[-3]==str(identity['pid'])]
            arguments=scope.arguments(identity['pid'])
            runtime_samples.append({'pid':identity['pid'],'birth':identity['birth'],
                'identity_current':scope.matches(identity),
                'exec_environment_tokio_workers':arguments[1].get('TOKIO_WORKER_THREADS'),
                'total_thread_rows':len(thread_rows),'ps_exit':sample.returncode})
            if args.sample_native_stacks and scope.matches(identity):
                stack_sample=subprocess.run(['/usr/bin/sample',str(identity['pid']),'1','10','-file',
                    str(output/f"native-stacks-{identity['pid']}.txt")],capture_output=True,text=True,timeout=10)
                runtime_samples[-1]['stack_sample_exit']=stack_sample.returncode
        write(output/'native-runtime-samples.json',{'samples':runtime_samples,
            'codex_home_dotenv_exists':(home/'.env').exists(),
            'limit':'Exec environment and total threads are not a count of Tokio asynchronous workers.'})
        report['release_at']=time.time();write(output/'before-release-health.json',health)
        with server.condition:
            server.released=True;server.condition.notify_all()
        deadline=time.monotonic()+180;completed={};last_report=0
        while time.monotonic()<deadline:
            assert daemon.poll() is None,'watcher exited during continuations'
            completed={s['surface_id']:native.task_snapshot(Path(s['transcript']),s['session_id']) for s in slots}
            if all(t and t.get('kind')=='task_complete' and not t.get('error') for t in completed.values()):
                break
            if time.monotonic()-last_report>=5:
                print(json.dumps({'stage':'continuation','local_requests':len(server.requests),
                    'arrived':{n:len(v) for n,v in server.arrived.items()},
                    'complete':sum(bool(t and t.get('kind')=='task_complete' and not t.get('error')) for t in completed.values())}),flush=True)
                last_report=time.monotonic()
            time.sleep(.05)
        else:
            raise RuntimeError('not all original natives completed requested rounds')
        before=len(server.requests);time.sleep(5)
        assert len(server.requests)==before,'native success did not self-stop'
        assert all(scope.matches(r) for r in owned),'fixture original identity changed'
        report['success_self_stop_seconds']=5
        report['completed']=len(completed)
        report['phase']='functional_passed'
    except BaseException as exc:
        report.update(phase='failed',error=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc())
    finally:
        # Only private authorization is revoked. No existing user's config is read or changed.
        core.ConfigStore(config_path).mutate(lambda cfg:cfg.update(global_paused=True))
        for worker in workers:
            try:worker.close()
            except BaseException as exc:report.setdefault('cleanup_errors',[]).append(str(exc))
        for thread in threads:thread.join(15)
        if daemon is not None and daemon.poll() is None:
            daemon.terminate()
            try:daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:report.setdefault('cleanup_errors',[]).append('private watcher did not stop')
        report['daemon_exit_code']=daemon.poll() if daemon is not None else None
        if daemon_log:daemon_log.close()
        for worker in workers:
            write(output/f'final-job-{worker.job["id"]}.json',worker.job)
            for slot in worker.job['slots']:
                if report['phase']=='failed' and slot.get('surface_id'):
                    try:
                        write(output/f'failed-viewport-{slot["surface_id"]}.json',
                              real.replay(worker.job['workspace_id'],slot['surface_id'],live=True))
                    except BaseException as exc:
                        write(output/f'failed-viewport-{slot["surface_id"]}.json',{'read_error':str(exc)})
                if slot.get('transcript') and Path(slot['transcript']).exists():
                    target=output/f'native-{slot["session_id"]}.jsonl'
                    target.write_bytes(Path(slot['transcript']).read_bytes())
                    logs.append({**slot,'frozen_transcript':str(target)})
        write(output/'frozen-slots.json',logs)
        output_checks=[];first_tasks=[];timings=[]
        for slot in logs:
            transcript_raw=Path(slot['frozen_transcript']).read_bytes()
            records=[json.loads(line) for line in transcript_raw.splitlines()]
            metadata=next((r.get('payload',{}) for r in records if r.get('type')=='session_meta'),{})
            first=next((r for r in records if r.get('type')=='event_msg'
                        and r.get('payload',{}).get('type')=='task_started'),None)
            if first and metadata.get('session_id',metadata.get('id'))==slot['session_id']:
                first_tasks.append({'surface_id':slot['surface_id'],'session_id':slot['session_id'],
                                    'task_at':timestamp(first['timestamp']),
                                    'turn_id':first['payload'].get('turn_id')})
            completions=[r['payload'] for r in records if r.get('type')=='event_msg'
                         and r.get('payload',{}).get('type')=='task_complete']
            last=completions[-1] if completions else {}
            lifecycle=native_acceptance_metrics.evaluate_native_completion(
                records, slot['session_id'], args.rounds)
            output_checks.append({'surface_id':slot['surface_id'],'session_id':slot['session_id'],
                'turn_id':last.get('turn_id'),'error':last.get('error'),
                'last_agent_message':last.get('last_agent_message'),
                'transcript_sha256':hashlib.sha256(transcript_raw).hexdigest(),
                'lifecycle':lifecycle, 'strict_ok':lifecycle['passed']})
        write(output/'final-output-checks.json',output_checks)
        report['strict_final_ok']=sum(r['strict_ok'] for r in output_checks)
        if logs:
            timings=analyze(logs,proxy.rows);write(output/'continuation-timings.json',timings)
            valid=[r for r in timings if r.get('input_count')==1 and r.get('native_next_ms') is not None]
            report['timing']={'expected':server.count*args.rounds,'failed_turns':len(timings),'bound':len(valid),
                'max_ack_ms':max((r['ack_ms'] for r in valid),default=None),
                'max_forward_ms':max((r['forward_ms'] for r in valid),default=None),
                'max_next_native_ms':max((r['native_next_ms'] for r in valid if r['native_next_ms'] is not None),default=None),
                'ack_over_1s':sum(r['ack_ms']>=1000 for r in valid)}
        report['performance']=native_acceptance_metrics.evaluate_native_timings(
            started,first_tasks,timings,server.count,args.rounds)
        write(output/'first-task-timings.json',first_tasks)
        for name in ('state.json','monitoring-health.json','daemon-runtime.json'):
            path=root/'ccc'/name
            if path.exists():(output/name).write_bytes(path.read_bytes())
        with server.condition:
            server.stopping=True;server.condition.notify_all()
        for wid in created:
            try:
                current={w['id'] for win in real.tree().get('windows',[]) for w in win.get('workspaces',[])}
                if wid in current:real._run(['close-workspace','--workspace',wid],timeout=10)
            except BaseException as exc:report.setdefault('cleanup_errors',[]).append(str(exc))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and any(r['environment_workspace_id'] in created for r in scope.scan()):time.sleep(.1)
        report['fixture_remaining']=[r for r in scope.scan() if r['environment_workspace_id'] in created]
        report['original_identity_changes']=[r for r in original if not scope.matches(r)]
        if cache:cache.close()
        proxy.shutdown();proxy.server_close();server.shutdown();server.server_close()
        os.environ.clear();os.environ.update(original_env)
        write(output/'provider-requests.json',server.requests);write(output/'provider-errors.json',server.errors)
        write(output/'proxy-errors.json',proxy.errors);write(output/'worker-errors.json',worker_errors)
        write(output/'rpc-timings.json',proxy.rpc_timings)
        report['local_requests']=len(server.requests)
        report['source_after']={n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in core.RUNTIME_FILES}
        report['sources_unchanged']=report['source_before']==report['source_after']
        report['acceptance_unchanged']=(report['acceptance_metrics_sha256']==hashlib.sha256(metrics_path.read_bytes()).hexdigest()
            and report['fixture_sha256']==hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        report['functional_passed']=bool(report['phase']=='functional_passed' and not report.get('cleanup_errors')
            and not report['fixture_remaining'] and not report['original_identity_changes'] and not proxy.errors
            and report.get('timing',{}).get('bound')==server.count*args.rounds
            and report['strict_final_ok']==server.count
            and report['sources_unchanged'])
        report['passed']=(report['functional_passed'] and report['performance']['local_performance_passed']
                          and report['acceptance_unchanged'])
        write(output/'result.json',report)
        print(json.dumps({k:v for k,v in report.items() if k not in {'native_before','owned','source_before','source_after'}},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    if len(sys.argv)>2 and sys.argv[1]=='--cli':
        sys.exit(wrapper(Path(sys.argv[2]),sys.argv[3:]))
    if len(sys.argv)==3 and sys.argv[1]=='--daemon':
        sys.exit(daemon_main(Path(sys.argv[2])))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workspaces',type=int,choices=(1,5,10),default=1)
    parser.add_argument('--batches',type=int,choices=(1,2),default=1)
    parser.add_argument('--rounds',type=int,default=5)
    parser.add_argument('--slots',type=int,choices=(1,50),default=50)
    parser.add_argument('--profile-lanes',action='store_true')
    parser.add_argument('--sample-native-stacks',action='store_true')
    parser.add_argument('--startup-timeout',type=float,default=180)
    sys.exit(main(parser.parse_args()))
