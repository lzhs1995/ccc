"""New GHA adapter around exact R8 fake SSE and judgment functions.

No original attempt/collector is loaded. HOME and CODEX_HOME retain their exact
inherited values; only a fresh, owned installation UUID is created and removed.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import fixture
from common import FLOOR, SMOKE_HEADROOM, admit, capacity, digest, gha_only, pin, require, save, stop_owned, verify_package

CASE_NAMES = ('baseline-comments', 'silent', 'comments', 'half-frame')


def child_environment(private):
    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
           'RUST_LOG': 'warn', 'MAGPIE_NO_ACCOUNTS': '1'}
    for key in ('HOME', 'CODEX_HOME'):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, leaf in [('TMPDIR', 'tmp'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'),
                      ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state'), ('XDG_RUNTIME_DIR', 'runtime')]:
        env[key] = str(private / leaf)
    return env


def installation_target(environ):
    require('HOME' in environ and Path(environ['HOME']).is_absolute(), 'missing absolute inherited HOME')
    home = Path(environ.get('CODEX_HOME') or str(Path(environ['HOME']) / '.codex'))
    require(home.is_absolute() and home.parent.resolve() == home.parent, 'installation parent alias rejected')
    return home / 'installation_id'


def identity(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_uid == os.getuid() and
                stat.S_IMODE(s.st_mode) == 0o644 and 0 < s.st_size <= 128,
                'installation is not a single owned 0644 bounded file')
        raw = os.read(fd, 129)
        try:
            uuid.UUID(raw.decode('ascii').strip())
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('installation contents are not a valid UUID') from None
        fields = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                             st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        require(fields(s) == fields(os.fstat(fd)) == fields(path.lstat()), 'installation changed while observing')
        import hashlib
        return {'dev': s.st_dev, 'ino': s.st_ino, 'mode': s.st_mode, 'uid': s.st_uid,
                'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns,
                'sha256': hashlib.sha256(raw).hexdigest()}
    finally:
        os.close(fd)


def create_installation(environ):
    """Only a nonexistent GHA Codex home is eligible; never inspect an existing one."""
    target = installation_target(environ)
    require(not target.parent.exists() and not target.parent.is_symlink(), 'existing Codex home cannot be used for this fixture')
    target.parent.mkdir(mode=0o700)
    try:
        with target.open('x') as stream:
            stream.write(str(uuid.uuid4()))
        target.chmod(0o644)
        original = identity(target)
    except BaseException as error:
        # Report the exact newly allocated scope; do not silently claim cleanup.
        raise RuntimeError('fresh installation allocation failed; preserve owned scope: ' + str(target.parent)) from error
    return target, original


def remove_installation(target, original, reaped):
    require(reaped and identity(target) == original, 'installation identity changed or children remain')
    require(list(target.parent.iterdir()) == [target], 'unexpected new Codex-home contents; preserve for reconciliation')
    target.unlink()
    target.parent.rmdir()


def policy(executable, private, network_port, bind_port=None, installation=None, python_prefix=None):
    quote = lambda value: json.dumps(str(value))
    extra_read = '' if python_prefix is None else '(subpath ' + quote(python_prefix) + ')'
    value = f'''(version 1)
(deny default)
(allow file-read-metadata)
(allow file-read-data file-map-executable
 (subpath "/System") (subpath "/usr") (subpath "/bin") (subpath "/sbin")
 (subpath "/opt/homebrew/Cellar") (subpath "/opt/homebrew/opt")
 (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom")
 (subpath {quote(private)}) (literal {quote(executable)}) {extra_read})
(allow file-write* (subpath {quote(private)}) (literal "/dev/null"))
(allow process-exec (literal {quote(executable)}))
(allow process-info* (target self))
(allow sysctl-read
 (sysctl-name "hw.ncpu") (sysctl-name "hw.logicalcpu") (sysctl-name "hw.activecpu")
 (sysctl-name "hw.memsize") (sysctl-name "hw.pagesize") (sysctl-name "hw.pagesize_compat")
 (sysctl-name "hw.machine") (sysctl-name "kern.osrelease") (sysctl-name "kern.osversion")
 (sysctl-name "kern.ostype") (sysctl-name "kern.version") (sysctl-name "kern.argmax")
 (sysctl-name "kern.hostname"))
(allow mach-lookup (global-name "com.apple.system.logger"))
(allow network-outbound (remote ip "localhost:{network_port}"))
'''
    if bind_port is not None:
        value += f'(allow network-bind network-inbound (local ip "localhost:{bind_port}"))\n'
    if installation is not None:
        value += f'''(allow file-read-data file-write-data (literal {quote(installation)}))
(allow user-preference-read (preference-domain "com.openai.codex"))
(allow mach-lookup (global-name "com.apple.cfprefsd.agent") (global-name "com.apple.cfprefsd.daemon"))
(allow ipc-posix-shm-read-data (ipc-posix-name "apple.cfprefs.{os.getuid()}v1")
 (ipc-posix-name "apple.cfprefs.daemonv1"))
'''
    return value


PROBE = r'''import ctypes,json,os,pathlib,socket,subprocess,sys,threading,uuid
j=json.loads(pathlib.Path(sys.argv[1]).read_text());r={}
def denied(name,fn):
 try: fn();r[name]=False
 except OSError:r[name]=True
def connect(port):
 with socket.create_connection(('127.0.0.1',port),timeout=1):pass
connect(j['allowed']);r['allowed_connect']=True
denied('other_socket_denied',lambda:connect(j['forbidden']))
denied('outside_read_denied',lambda:pathlib.Path(j['outside']).read_bytes())
denied('outside_write_denied',lambda:pathlib.Path(j['outside']).write_text('bad'))
denied('fork_denied',lambda:subprocess.run(['/usr/bin/true'],check=True))
pathlib.Path(j['private'],'probe-write').write_text('synthetic');r['private_write']=True
if j.get('installation'):
 p=pathlib.Path(j['installation']);before=p.read_bytes();uuid.UUID(before.decode());fd=os.open(p,os.O_RDWR);os.close(fd)
 r['installation_valid_open_unchanged']=before==p.read_bytes()
 r['uname_available']=bool(os.uname().machine)
 cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
 cf.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32];cf.CFStringCreateWithCString.restype=ctypes.c_void_p
 cf.CFPreferencesAppValueIsForced.argtypes=[ctypes.c_void_p,ctypes.c_void_p];cf.CFPreferencesAppValueIsForced.restype=ctypes.c_bool
 cf.CFPreferencesAppSynchronize.argtypes=[ctypes.c_void_p];cf.CFPreferencesAppSynchronize.restype=ctypes.c_bool
 cf.CFRelease.argtypes=[ctypes.c_void_p]
 domain=cf.CFStringCreateWithCString(None,b'com.openai.codex',0x08000100)
 for name in (b'config',b'requirements'):
  key=cf.CFStringCreateWithCString(None,name,0x08000100);cf.CFPreferencesAppValueIsForced(key,domain);cf.CFRelease(key)
 r['managed_forced_queries']=True
 def sync():r['managed_sync']=bool(cf.CFPreferencesAppSynchronize(domain))
 t=threading.Thread(target=sync);t.start();t.join(5);r['managed_worker_finished']=not t.is_alive();cf.CFRelease(domain)
print(json.dumps(r))
'''
PROBE_BASE = {'allowed_connect', 'other_socket_denied', 'outside_read_denied',
              'outside_write_denied', 'fork_denied', 'private_write'}
PROBE_CODEX = PROBE_BASE | {'installation_valid_open_unchanged', 'uname_available',
                          'managed_forced_queries', 'managed_sync', 'managed_worker_finished'}


def probe_result(returncode, stdout, codex):
    value = json.loads(stdout)
    return (type(returncode) is int and returncode == 0 and type(value) is dict and
            set(value) == (PROBE_CODEX if codex else PROBE_BASE) and all(v is True for v in value.values()))


def python_runtime():
    prefix = Path(sys.base_prefix).resolve()
    direct = prefix / 'Resources/Python.app/Contents/MacOS/Python'
    executable = direct if direct.is_file() else Path(sys.executable).resolve()
    require(prefix != Path(os.environ['HOME']) and prefix != Path('/'), 'Python runtime grant too broad')
    return executable, prefix


def run_probe(private, output, allowed, forbidden, outside, installation, samples):
    executable, prefix = python_runtime()
    script = private / ('codex-probe.py' if installation else 'gateway-probe.py')
    script.write_text(PROBE)
    inp = script.with_suffix('.json')
    save(inp, {'allowed': allowed, 'forbidden': forbidden, 'outside': str(outside),
               'private': str(private), 'installation': str(installation) if installation else None})
    sb = script.with_suffix('.sb')
    sb.write_text(policy(executable, private, allowed, installation=installation, python_prefix=prefix))
    child = None
    with (output / (script.stem + '.stdout')).open('x') as out, (output / (script.stem + '.stderr')).open('x') as err:
        try:
            child = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(sb), str(executable), '-BEI', str(script), str(inp)],
                                     env=child_environment(private), cwd=private, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=True)
            deadline = time.monotonic() + 15
            while child.poll() is None:
                sample = capacity(output);samples.append(sample)
                require(sample['free_bytes'] >= FLOOR and time.monotonic() < deadline, 'probe capacity/deadline failed')
                time.sleep(.05)
            rc = child.wait()
        finally:
            stop_owned(child)
    require(probe_result(rc, (output / (script.stem + '.stdout')).read_text(), installation is not None), 'actual sandbox probe controls failed')
    return {'passed': True, 'policy': pin(sb), 'python': pin(executable), 'controls': sorted(PROBE_CODEX if installation else PROBE_BASE)}


def reserve():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    return sock


def run_case(name, binary, binary_pin, codex, private_parent, output, installation, installation_before):
    output.mkdir(parents=True, exist_ok=False)
    success = name != 'baseline-comments'
    case = 'comments' if not success else name
    result = {'name': name, 'case': case, 'status': 'NOT_STARTED', 'expected_success': success,
              'artifact': binary_pin, 'codex': pin(codex), 'samples': [], 'children_reaped': False}
    private = None
    children = []
    server = server_thread = reservation = negative = None
    try:
        admit(output, SMOKE_HEADROOM, result['samples'])
        require(pin(binary) == binary_pin, 'binary changed before case')
        require(identity(installation) == installation_before, 'installation changed before case')
        private = Path(tempfile.mkdtemp(prefix='passing-case-', dir=private_parent)).resolve()
        for leaf in ['tmp', 'config', 'cache', 'data', 'state', 'runtime', 'codex-log', 'codex-sqlite']:
            (private / leaf).mkdir()
        outside = private_parent / ('outside-' + name)
        outside.write_text('synthetic denied sentinel')
        reservation = reserve(); gateway = reservation.getsockname()[1]
        negative = reserve();negative.listen(1)
        server = fixture.Fake(('127.0.0.1', 0), fixture.Handler)
        server.records = [];server.stop = threading.Event()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True);server_thread.start()
        root = Path(__file__).resolve().parent
        cfg = private / 'config/magpie';cfg.mkdir()
        shutil.copyfile(root / 'settings.json', cfg / 'settings.json')
        shutil.copyfile(root / 'plugins.yml', cfg / 'plugins.yml')
        save(cfg / 'providers.json', {'noAutoGroups': True, 'providers': [{
            'id': 'fixture-' + case, 'name': 'Synthetic native Codex check', 'key': 'synthetic-' + case,
            'models': ['gpt-6-astra'], 'responses': f'http://127.0.0.1:{server.server_port}/v1',
            'nativeCompaction': False, 'maxConcurrency': 1}], 'groups': []})
        result['gateway_probe'] = run_probe(private, output, server.server_port, negative.getsockname()[1], outside, None, result['samples'])
        env = child_environment(private);env['MAGPIE_ADDR'] = f'127.0.0.1:{gateway}'
        sb = private / 'gateway.sb';sb.write_text(policy(binary, private, server.server_port, bind_port=gateway))
        reservation.close();reservation = None
        with (output / 'gateway.stdout').open('x') as out, (output / 'gateway.stderr').open('x') as err:
            gateway_child = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(sb), str(binary), 'serve'],
                                            cwd=private, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=err, start_new_session=True)
        children.append(gateway_child)
        deadline = time.monotonic() + 20
        while True:
            require(gateway_child.poll() is None, 'gateway exited before readiness')
            listing = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{gateway}', '-sTCP:LISTEN', '-Fp'],
                                     capture_output=True, text=True, timeout=3)
            listeners = {int(line[1:]) for line in listing.stdout.splitlines() if line.startswith('p')}
            if listeners:
                require(listeners == {gateway_child.pid}, 'gateway port belongs to another process')
                break
            require(time.monotonic() < deadline, 'gateway listener deadline')
            sample=capacity(output);result['samples'].append(sample);require(sample['free_bytes'] >= FLOOR, 'capacity floor crossed')
            time.sleep(.2)
        result['codex_probe'] = run_probe(private, output, gateway, server.server_port, outside, installation, result['samples'])
        require(not select.select([negative], [], [], 0)[0], 'forbidden probe connection reached negative listener')
        fixture.CODEX = codex
        cp = private / 'codex.sb';cp.write_text(policy(codex, private, gateway, installation=installation))
        command = ['/usr/bin/sandbox-exec', '-f', str(cp), *fixture.codex_command(private, gateway, case)]
        result['argv'] = command
        with (output / 'codex.jsonl').open('x') as out, (output / 'codex.stderr').open('x') as err:
            client = subprocess.Popen(command, cwd=private, env=child_environment(private), stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=err, start_new_session=True)
        children.append(client)
        deadline = time.monotonic() + 50
        while client.poll() is None:
            sample=capacity(output);result['samples'].append(sample)
            require(sample['free_bytes'] >= FLOOR and time.monotonic() < deadline, 'client floor/deadline exceeded')
            time.sleep(.1)
        result['validation'] = fixture.judge(client.wait(), (output/'codex.jsonl').read_text(),
                                              (output/'codex.stderr').read_text(), server.records, success)
        result['status'] = 'PASS'
    except Exception as error:
        result.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
    finally:
        for child in reversed(children):
            try:stop_owned(child)
            except Exception as error:result.update(status='FAIL', cleanup_error=str(error))
        result['children_reaped'] = all(child.poll() is not None for child in children)
        if server is not None:
            server.stop.set();server.shutdown();server.server_close()
            if server_thread is not None:server_thread.join(timeout=2)
            deadline=time.monotonic()+3
            while server.active_handlers and time.monotonic()<deadline:time.sleep(.05)
        result['requests'] = server.records if server is not None else []
        result['handlers_finished'] = server is None or (server.active_handlers == 0 and
            not server_thread.is_alive() and all('finished_at' in row for row in server.records))
        for sock in (reservation, negative):
            if sock is not None:sock.close()
        try:
            result['artifact_unchanged'] = pin(binary) == binary_pin
            result['codex_unchanged'] = pin(codex) == result['codex']
            result['installation_unchanged'] = identity(installation) == installation_before
            require(result['artifact_unchanged'] and result['codex_unchanged'] and result['installation_unchanged'], 'identity changed during case')
            require(result['children_reaped'] and result['handlers_finished'], 'owned runtime did not fully stop')
            if private is not None:
                inputs = {str(p.relative_to(private)): {'pin': pin(p), 'text':p.read_text()}
                          for p in private.rglob('*') if p.is_file() and (p.suffix in ['.sb'] or p.parent == private/'config/magpie')}
                save(output/'SYNTHETIC-INPUTS.json', inputs)
                shutil.rmtree(private)
            result['private_removed'] = private is None or not private.exists()
        except Exception as error:result.update(status='FAIL', reconciliation_error=str(error))
        result['owned_children'] = [{'pid':child.pid,'returncode':child.poll()} for child in children]
        result['files'] = {p.name:pin(p) for p in output.iterdir() if p.is_file()}
        save(output/'RESULT.json', result)
    return result


def main():
    p=argparse.ArgumentParser()
    for name in ['candidate','baseline','client','output']:
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--package-pin',required=True)
    a=p.parse_args();gha_only()
    root=Path(__file__).resolve().parent;verify_package(root,a.package_pin)
    output=a.output.resolve();output.mkdir(parents=True,exist_ok=False)
    result={'status':'NOT_STARTED','package_pin':a.package_pin,'cases':[],
            'HOME_unchanged':True,'CODEX_HOME_unchanged':True,'production_operations':0,
            'new_artifacts_only':True,'old_collector_reused':False}
    target=original=None
    private_parent=Path(tempfile.mkdtemp(prefix='gha-passing-runtime-')).resolve()
    try:
        binaries={}
        for kind,folder in [('candidate',a.candidate),('baseline',a.baseline)]:
            folder=folder.resolve(strict=True);receipt=json.loads((folder/'BUILD.json').read_text())
            require(receipt['status']=='PASS' and receipt['source_after_match'] is True and receipt['variant']==kind and receipt['package_pin']==a.package_pin,'unaccepted build')
            require(receipt['source_manifest']==pin(root/(kind+'-source.json')),'build source does not match package')
            binary=folder/receipt['binary_name'];require(pin(binary)==receipt['binary'],'new build binary changed')
            binaries[kind]=(binary,receipt['binary'])
        folder=a.client.resolve(strict=True);receipt=json.loads((folder/'CLIENT.json').read_text());spec=json.loads((root/'CLIENT-ASSET.json').read_text())
        codex=folder/spec['binary_name'];require(receipt['status']=='PASS' and receipt['package_pin']==a.package_pin and receipt['spec']==spec,'client receipt not accepted')
        require(pin(codex)=={'sha256':spec['binary_sha256'],'bytes':spec['binary_bytes']},'fixed original client identity differs')
        result['inputs']={'candidate':binaries['candidate'][1],'baseline':binaries['baseline'][1],'codex':pin(codex)}
        # Keep the parent path in failure evidence even if creating the UUID fails.
        result['owned_installation_parent'] = str(installation_target(os.environ).parent)
        target,original=create_installation(os.environ)
        for name in CASE_NAMES:
            kind='baseline' if name=='baseline-comments' else 'candidate'
            binary,binary_pin=binaries[kind]
            row=run_case(name,binary,binary_pin,codex,private_parent,output/name,target,original)
            result['cases'].append({'name':name,'result':pin(output/name/'RESULT.json'),'status':row['status']})
            require(row['status']=='PASS','case failed; original attempt retained: '+name)
        result['status']='PASS'
    except Exception as error:result.update(status='FAIL',error=type(error).__name__+': '+str(error))
    finally:
        reaped=all(json.loads((output/row['name']/'RESULT.json').read_text()).get('children_reaped') is True
                   and json.loads((output/row['name']/'RESULT.json').read_text()).get('handlers_finished') is True for row in result['cases'])
        try:
            require(reaped,'owned children remain; preserve runtime roots')
            if target is not None:remove_installation(target,original,reaped)
            elif 'owned_installation_parent' in result:
                require(not Path(result['owned_installation_parent']).exists(), 'installation allocation failed; scope remains for reconciliation')
            shutil.rmtree(private_parent)
            result['cleanup_complete']=True
        except Exception as error:result.update(status='FAIL',cleanup_complete=False,cleanup_error=str(error))
        verify_package(root,a.package_pin)
        save(output/'ACCEPTANCE.json',result)
    require(result['status']=='PASS' and len(result['cases'])==4,'four-case native smoke failed')


if __name__=='__main__':
    main()
