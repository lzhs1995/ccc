"""Read-only web entry acceptance for one newly built Magpie artifact on GHA."""
import argparse
import http.client
import http.cookies
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from common import FLOOR, SMOKE_HEADROOM, admit, capacity, gha_only, no_portable_selector, pin, require, save, stop_owned, verify_package

SCHEMA = 'magpie-gha-web-entry-v1'
SYNTHETIC_KEY = 'synthetic-web-entry-only-20261112'
MODEL = 'fixture-web/gpt-6-astra'
HTTP_NAMES = ('health', 'models', 'unauthenticated', 'bad_key', 'login', 'page')


def policy(binary, private, gateway, web, upstream):
    q = lambda value: json.dumps(str(value))
    return f'''(version 1)
(deny default)
(allow file-read-metadata)
(allow file-read-data file-map-executable
 (subpath "/System") (subpath "/usr") (subpath "/bin") (subpath "/sbin")
 (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom")
 (subpath {q(private)}) (literal {q(binary)}))
(allow file-write* (subpath {q(private)}) (literal "/dev/null"))
(allow process-exec (literal {q(binary)}))
(allow process-info* (target self))
(allow sysctl-read
 (sysctl-name "hw.ncpu") (sysctl-name "hw.logicalcpu") (sysctl-name "hw.activecpu")
 (sysctl-name "hw.memsize") (sysctl-name "hw.pagesize") (sysctl-name "hw.pagesize_compat")
 (sysctl-name "hw.machine") (sysctl-name "kern.osrelease") (sysctl-name "kern.osversion")
 (sysctl-name "kern.ostype") (sysctl-name "kern.version") (sysctl-name "kern.argmax")
 (sysctl-name "kern.hostname"))
(allow mach-lookup (global-name "com.apple.system.logger"))
(allow network-bind network-inbound (local ip "localhost:{gateway}") (local ip "localhost:{web}"))
(allow network-outbound (remote ip "localhost:{upstream}") (remote ip "localhost:{gateway}"))
'''


def environment(private, gateway):
    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
           'MAGPIE_NO_ACCOUNTS': '1', 'MAGPIE_ADDR': f'127.0.0.1:{gateway}',
           'MAGPIE_WEB_KEY': SYNTHETIC_KEY}
    for key in ('HOME', 'CODEX_HOME'):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, leaf in [('TMPDIR','tmp'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),
                      ('XDG_DATA_HOME','xdg-data'),('XDG_STATE_HOME','state'),('XDG_RUNTIME_DIR','runtime')]:
        env[key] = str(private / leaf)
    return env


class Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        super().__init__(('127.0.0.1', 0), Fake)
        self.requests = []
        self.active = 0
        self.lock = threading.Lock()
    def process_request(self, request, address):
        with self.lock:self.active += 1
        try:super().process_request(request,address)
        except BaseException:
            with self.lock:self.active -= 1
            raise
    def process_request_thread(self, request, address):
        try:super().process_request_thread(request,address)
        finally:
            with self.lock:self.active -= 1


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_GET(self):
        self.server.requests.append({'method':'GET','path':self.path})
        body=b'{"object":"list","data":[{"id":"gpt-6-astra","object":"model"}]}'
        self.send_response(200 if self.path.endswith('/models') else 404)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        self.server.requests.append({'method':'POST','path':self.path})
        self.send_error(405)


def reserve():
    s=socket.socket();s.bind(('127.0.0.1',0));return s


def listeners(port):
    r=subprocess.run(['/usr/sbin/lsof','-nP',f'-iTCP:{port}','-sTCP:LISTEN','-Fp'],capture_output=True,text=True,timeout=3)
    require(r.returncode in (0,1),'listener inventory failed')
    return sorted({int(x[1:]) for x in r.stdout.splitlines() if x.startswith('p')})


def http_get(folder, name, port, path, headers=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
    try:
        c.request('GET',path,headers=headers or {})
        r=c.getresponse();body=r.read(2*1024*1024+1)
        require(len(body)<=2*1024*1024,'HTTP fixture response too large')
        (folder/(name+'.body')).write_bytes(body)
        metadata={'method':'GET','path':path,'port':port,'status':r.status,'headers':list(r.getheaders()),'body':pin(folder/(name+'.body'))}
        save(folder/(name+'.json'),metadata)
        return metadata,body
    finally:c.close()


def header(row, name):
    return ', '.join(value for key,value in row['headers'] if key.lower()==name.lower())


def judge_http(rows):
    require(set(rows)==set(HTTP_NAMES),'web HTTP case set differs')
    meta,body=rows['health'];require(meta['status']==200 and b'"magpie"' in body,'gateway health failed')
    meta,body=rows['models'];data=json.loads(body)
    require(meta['status']==200 and data.get('object')=='list' and
            [row['id'] for row in data.get('data',[])]==[MODEL],'private fake model was not loaded exactly')
    meta,body=rows['unauthenticated'];require(meta['status']==401 and b'magpie web' in body and b'Sign in' in body,'unauthenticated web guard failed')
    meta,body=rows['bad_key'];require(meta['status']==401,'wrong web key accepted')
    meta,body=rows['login'];require(meta['status']==303 and header(meta,'Location')=='/','synthetic login did not redirect locally')
    cookies=http.cookies.SimpleCookie();cookies.load(header(meta,'Set-Cookie'))
    require(len(cookies)==1 and next(iter(cookies.values())).value==SYNTHETIC_KEY,'synthetic auth cookie differs')
    meta,body=rows['page'];require(meta['status']==200 and 'text/html' in header(meta,'Content-Type') and
                               b'<title>magpie</title>' in body.lower(),'authenticated real web page missing')
    return {'health':True,'fake_model_list':True,'unauthenticated_rejected':True,'wrong_key_rejected':True,
            'login_cookie':True,'authenticated_page':True,'model_posts':0}


def verify_receipt(root, receipt, build, artifact):
    """Read-only receipt revalidation callable by the original next-load adapter."""
    root=Path(root);build=Path(build)
    expected_artifact=artifact if isinstance(artifact,dict) else pin(artifact)
    row=json.loads(Path(receipt).read_text()) if not isinstance(receipt,dict) else receipt
    require(row['schema']==SCHEMA and row['status']=='PASS','web receipt is not accepted')
    require(row['artifact']==expected_artifact and row['build_receipt']==pin(build),'web/build/artifact identity differs')
    spec=json.loads(build.read_text())
    require(spec['status']=='PASS' and spec['variant']=='candidate' and spec['source_after_match'] is True and
            spec['binary']==row['artifact'] and spec['package_pin']==row['build_package_pin'],'unaccepted candidate build')
    require(row['argv']==[row['artifact_path'],'serve',f"--web=127.0.0.1:{row['ports']['web']}"],'wrong real launcher entry')
    require(row['signature']['argv']==['/usr/bin/codesign','--verify','--strict',row['artifact_path']] and
            type(row['signature']['returncode']) is int and row['signature']['returncode']==0,'codesign verification failed')
    require(set(row['signature']['logs'])=={'codesign.stdout','codesign.stderr'},'signature log set differs')
    for name,value in row['signature']['logs'].items():require(pin(root/name)==value,'signature log changed')
    for flag in ['children_reaped','fake_handlers_finished','private_removed','artifact_unchanged','no_residual_listeners']:
        require(row.get(flag) is True,'web cleanup/integrity failed: '+flag)
    require(type(row['model_posts']) is int and row['model_posts']==0 and not any(x['method']!='GET' for x in row['upstream_requests']),'unexpected model POST')
    require(row['listener_pids']=={'gateway':[row['owned_pid']],'web':[row['owned_pid']]},'ports were not exclusively owned')
    require(row['after_listener_pids']=={'gateway':[],'web':[]},'listener remained after reap')
    require(len(row['capacity_samples'])>=2 and all(type(s['free_bytes']) is int and s['free_bytes']>=FLOOR for s in row['capacity_samples']),'capacity records failed')
    require(all(s['free_bytes']>=FLOOR+SMOKE_HEADROOM for s in row['capacity_samples'][:2]),'admission headroom failed')
    require(row['policy_text']==policy(Path(row['artifact_path']),Path(row['private_path']),
            row['ports']['gateway'],row['ports']['web'],row['ports']['upstream']),'sandbox differs from exact owned-port policy')
    require(len(set(row['ports'].values()))==3 and all(type(v) is int and 0<v<65536 for v in row['ports'].values()),'invalid owned-port set')
    require(pin(root/'gateway.sb')==row['policy'],'sandbox pin differs')
    require((root/'gateway.sb').read_text()==row['policy_text'],'sandbox text changed')
    required_files={name+suffix for name in HTTP_NAMES for suffix in ('.json','.body')} | {'gateway.sb','providers.json','settings.json','plugins.yml','codesign.stdout','codesign.stderr','gateway.stdout','gateway.stderr'}
    require(set(row['files'])==required_files,'web evidence file set differs')
    for name,value in row['files'].items():
        require(Path(name).name==name,'unsafe receipt file reference')
        require(pin(root/name)==value,'web evidence changed: '+name)
    http_rows={}
    for name in HTTP_NAMES:
        meta=json.loads((root/(name+'.json')).read_text());body=(root/(name+'.body')).read_bytes()
        require(meta['body']==pin(root/(name+'.body')) and meta['method']=='GET','HTTP response identity differs')
        expected_port=row['ports']['gateway'] if name in ('health','models') else row['ports']['web']
        expected_paths={'health':'/','models':'/v1/models','unauthenticated':'/','bad_key':'/?k=wrong-synthetic-key','login':'/?k='+SYNTHETIC_KEY,'page':'/'}
        require(meta['port']==expected_port and meta['path']==expected_paths[name],'HTTP endpoint identity differs')
        http_rows[name]=(meta,body)
    require(judge_http(http_rows)==row['http_validation'],'HTTP judgment changed')
    config=json.loads((root/'providers.json').read_text())
    require(config==provider_config(row['ports']['upstream']),'fake provider scope changed')
    package=Path(__file__).resolve().parent
    for name in ['settings.json','plugins.yml']:
        require((root/name).read_bytes()==(package/name).read_bytes(),'synthetic seed differs: '+name)
    return {'schema':SCHEMA,'status':'PASS','artifact':row['artifact'],'build_receipt':row['build_receipt'],
            'receipt':pin(receipt) if not isinstance(receipt,dict) else None,'model_posts':0}


def verify_web(web_root, candidate_folder, artifact):
    """Rejudge archived original web evidence; never executes the candidate.

    The caller pins this public package and independently validates the candidate
    build/source and four-case receipt. No downloaded executable is needed.
    """
    root=Path(web_root);candidate=Path(candidate_folder)
    row=json.loads((root/'WEB-ACCEPTANCE.json').read_text())
    verify_package(Path(__file__).resolve().parent,row['package_pin'])
    return verify_receipt(root,root/'WEB-ACCEPTANCE.json',candidate/'BUILD.json',artifact)


def provider_config(upstream):
    return {'noAutoGroups':True,'providers':[{'id':'fixture-web','name':'Synthetic web fixture','key':'synthetic-web-only',
             'models':['gpt-6-astra'],'responses':f'http://127.0.0.1:{upstream}/v1','nativeCompaction':False,'maxConcurrency':1}],'groups':[]}


def main():
    p=argparse.ArgumentParser();p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--package-pin',required=True);a=p.parse_args();gha_only()
    package=Path(__file__).resolve().parent;verify_package(package,a.package_pin)
    build=a.build.resolve(strict=True);spec=json.loads(build.read_text());artifact=build.parent/spec['binary_name']
    require(spec['status']=='PASS' and spec['variant']=='candidate' and spec['source_after_match'] is True and pin(artifact)==spec['binary'],'candidate build not accepted')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    result={'schema':SCHEMA,'status':'NOT_STARTED','artifact':pin(artifact),'artifact_path':str(artifact),
            'build_receipt':pin(build),'build_package_pin':spec['package_pin'],'package_pin':a.package_pin,'capacity_samples':[],
            'production_operations':0,'historical_smoke_reused':False,'model_posts':0}
    private=None;child=None;server=None;worker=None;gateway_sock=web_sock=None
    try:
        admit(out,SMOKE_HEADROOM,result['capacity_samples']);no_portable_selector(artifact)
        command=['/usr/bin/codesign','--verify','--strict',str(artifact)]
        r=subprocess.run(command,capture_output=True,timeout=20)
        (out/'codesign.stdout').write_bytes(r.stdout);(out/'codesign.stderr').write_bytes(r.stderr)
        result['signature']={'argv':command,'returncode':r.returncode,'logs':{n:pin(out/n) for n in ['codesign.stdout','codesign.stderr']}}
        require(r.returncode==0,'new artifact failed codesign --verify --strict')
        private=Path(tempfile.mkdtemp(prefix='magpie-web-entry-')).resolve();result['private_path']=str(private)
        for name in ['tmp','config','cache','xdg-data','state','runtime']:(private/name).mkdir()
        server=Upstream();worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        gateway_sock=reserve();web_sock=reserve();gateway=gateway_sock.getsockname()[1];web=web_sock.getsockname()[1]
        result['ports']={'gateway':gateway,'web':web,'upstream':server.server_port}
        cfg=private/'config/magpie';cfg.mkdir()
        for name in ['settings.json','plugins.yml']:
            shutil.copyfile(package/name,cfg/name);shutil.copyfile(package/name,out/name)
        save(cfg/'providers.json',provider_config(server.server_port));shutil.copyfile(cfg/'providers.json',out/'providers.json')
        sb=out/'gateway.sb';sb.write_text(policy(artifact,private,gateway,web,server.server_port))
        result['policy']=pin(sb);result['policy_text']=sb.read_text();result['argv']=[str(artifact),'serve',f'--web=127.0.0.1:{web}']
        gateway_sock.close();gateway_sock=None;web_sock.close();web_sock=None
        no_portable_selector(artifact)
        with (out/'gateway.stdout').open('x') as stdout,(out/'gateway.stderr').open('x') as stderr:
            child=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(sb),*result['argv']],cwd=private,
                                    env=environment(private,gateway),stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
        result['owned_pid']=child.pid;deadline=time.monotonic()+25
        while True:
            require(child.poll() is None,'web entry exited before readiness')
            owned={'gateway':listeners(gateway),'web':listeners(web)}
            require(all(not ids or ids==[child.pid] for ids in owned.values()),'port belongs to other process')
            if all(ids==[child.pid] for ids in owned.values()):break
            sample=capacity(out);result['capacity_samples'].append(sample)
            require(sample['free_bytes']>=FLOOR and time.monotonic()<deadline,'web readiness floor/deadline')
            time.sleep(.2)
        result['listener_pids']=owned
        rows={}
        def checked_get(name, port, path, headers=None):
            sample=capacity(out);result['capacity_samples'].append(sample)
            require(sample['free_bytes']>=FLOOR and child.poll() is None,'web capacity or process check failed')
            value=http_get(out,name,port,path,headers)
            sample=capacity(out);result['capacity_samples'].append(sample)
            require(sample['free_bytes']>=FLOOR and child.poll() is None,'web capacity or process check failed')
            return value
        rows['health']=checked_get('health',gateway,'/')
        rows['models']=checked_get('models',gateway,'/v1/models',{'Authorization':'Bearer magpie'})
        rows['unauthenticated']=checked_get('unauthenticated',web,'/',{'Accept':'text/html'})
        rows['bad_key']=checked_get('bad_key',web,'/?k=wrong-synthetic-key')
        rows['login']=checked_get('login',web,'/?k='+SYNTHETIC_KEY)
        cookies=http.cookies.SimpleCookie();cookies.load(header(rows['login'][0],'Set-Cookie'))
        cookie='; '.join(k+'='+v.value for k,v in cookies.items())
        rows['page']=checked_get('page',web,'/',{'Accept':'text/html','Cookie':cookie})
        result['http_validation']=judge_http(rows)
        result['status']='PASS'
    except Exception as error:result.update(status='FAIL',error=type(error).__name__+': '+str(error))
    finally:
        try:
            result['returncode']=stop_owned(child);result['children_reaped']=child is None or child.poll() is not None
            if server is not None:
                server.shutdown();server.server_close();worker.join(timeout=2)
                deadline=time.monotonic()+2
                while server.active and time.monotonic()<deadline:time.sleep(.05)
            result['fake_handlers_finished']=server is None or (server.active==0 and not worker.is_alive())
            result['upstream_requests']=server.requests if server is not None else []
            result['model_posts']=sum(x['method']=='POST' for x in result['upstream_requests'])
            result['artifact_unchanged']=pin(artifact)==result['artifact']
            if 'ports' in result:
                result['after_listener_pids']={name:listeners(result['ports'][name]) for name in ['gateway','web']}
                result['no_residual_listeners']=all(not ids for ids in result['after_listener_pids'].values())
            require(result['children_reaped'] and result['fake_handlers_finished'] and result['artifact_unchanged'] and
                    result.get('no_residual_listeners') is True and result['model_posts']==0,'web cleanup/integrity failed')
            if private is not None:shutil.rmtree(private)
            result['private_removed']=private is None or not private.exists()
        except Exception as error:result.update(status='FAIL',cleanup_error=str(error))
        for sock in [gateway_sock,web_sock]:
            if sock is not None:sock.close()
        result['files']={p.name:pin(p) for p in out.iterdir() if p.is_file()}
        save(out/'WEB-ACCEPTANCE.json',result)
    if result['status']=='PASS':
        try:save(out/'WEB-VERIFY.json',verify_web(out,build.parent,pin(artifact)))
        except Exception as error:
            result.update(status='FAIL',verification_error=str(error));save(out/'WEB-ACCEPTANCE.json',result)
    require(result['status']=='PASS','real web entry acceptance failed; original evidence preserved')


if __name__=='__main__':main()
