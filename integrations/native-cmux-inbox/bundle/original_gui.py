"""Exact selected original owned-GUI functions; no cold restart loop."""
import json, os, pwd, socket, stat, subprocess, sys, time
from pathlib import Path
import fixture_common as h

def gui_session():
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("only an isolated GitHub macOS GUI fixture")
    owner = Path("/dev/console").stat().st_uid
    if owner == 0 or owner != os.geteuid():
        raise ValueError("runner is not the logged-in non-root GUI console user")
    manager = subprocess.run(["/bin/launchctl", "managername"], check=True,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    if manager != "Aqua":
        raise ValueError("runner has not entered its real Aqua console session")
    return {"console_uid": owner, "manager": manager}


def persistence_paths(home, bundle_id):
    admission.need(re.fullmatch(r'com\.cmuxterm\.app\.debug\.nestedrestore\.r[0-9]+a[0-9]+', bundle_id),
                   'not a verified fixed-build bundle identity')
    home = Path(home).resolve(strict=True)
    store = home / 'Library/Application Support/cmux'
    return {'store': store, 'state': home / '.local/state/cmux',
            'snapshot': store / ('session-' + bundle_id + '.json'),
            'config': home / '.config/cmux',
            'preferences': home / 'Library/Preferences' / (bundle_id + '.plist'),
            'saved_state': home / 'Library/Saved Application State' / (bundle_id + '.savedState'),
            'startup_log': home / 'Library/Logs/cmux' / ('startup-' + bundle_id + '.log')}


import fixed_app_admission as admission
import re

class OriginalOwnedMethods:
    def exact(self):
        if self.proc is None or self.identity is None: return False
        pid = self.proc.pid
        return (self.scope.birth(pid) == self.identity["birth"]
                and str(self.executable_path(pid)) == str(self.exe)
                and self.scope.arguments(pid)[0] == [str(self.exe)])


    def peer(self):
        before = self.socket.lstat()
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o077:
            raise ValueError("private Nightly socket is unsafe")
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(3);connection.connect(str(self.socket))
            pid = int.from_bytes(connection.getsockopt(0,2,4),sys.byteorder,signed=True)
        after = self.socket.lstat()
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or pid != self.proc.pid or not self.exact():
            raise ValueError("socket does not belong to the exact owned Nightly generation")
        return {**self.identity,"socket_device":after.st_dev,"socket_inode":after.st_ino}


    def cli_call(self, args, timeout=15):
        self.peer()
        reply = subprocess.run([str(self.cli),"--socket",str(self.socket),*args],env=self.env,
                               capture_output=True,text=True,timeout=timeout)
        self.peer()
        if reply.returncode: raise RuntimeError("original private cmux CLI refused: "+reply.stderr[:500])
        return reply


    def close(self):
        if self.proc is None: return
        # The caller must close and verify its original owned workspaces/native
        # processes first. This only stops this exact app Popen, never unknown PID.
        if self.proc.poll() is None:
            if not self.exact(): raise ValueError("refuse cleanup of changed GUI identity")
            self.proc.terminate();self.proc.wait(timeout=20)
        h.save_once(self.runtime / "GUI-PROCESS-CLEANUP.json",{"pid":self.proc.pid,
            "birth":self.identity and self.identity["birth"],"returncode":self.proc.returncode,
            "exact_gui_process_gone":self.identity is not None and self.scope.birth(self.proc.pid)!=self.identity["birth"],
            "native_and_workspace_cleanup_proven":False})
        self.proc=None


    def __exit__(self,*unused): self.close()


    def start(self):
        if self.proc is not None:
            raise ValueError('original GUI still owned; cannot start a second writer')
        self.generation += 1
        if self.generation > 2:
            raise ValueError('fixture permits only the two original GUI generations')
        prefix = 'GEN-' + str(self.generation)
        h.save_once(self.runtime / (prefix + '-LAUNCH-INTENT.json'), {
            'argv': [str(self.exe)], 'environment': self.env,
            'generation': self.generation, 'manual_restore': False})
        with (self.runtime / (prefix + '-STDIO.log')).open('xb') as log:
            self.proc = subprocess.Popen([str(self.exe)], env=self.env, cwd=self.runtime,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, start_new_session=True)
        birth = self.scope.birth(self.proc.pid)
        self.identity = {'pid': self.proc.pid, 'birth': birth, 'executable': str(self.exe)}
        if not birth or not self.exact():
            raise ValueError('new owned GUI identity unproven')
        deadline = time.monotonic() + 40
        last_error = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ValueError('owned GUI exited before readiness')
            try:
                peer = self.peer()
                tree = json.loads(self.cli_call(['--json', '--id-format', 'both', 'tree', '--all']).stdout)
                if tree.get('windows'):
                    h.save_once(self.runtime / (prefix + '-READY.json'), {'peer': peer, 'tree': tree})
                    return self
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                last_error = str(exc)
            time.sleep(.2)
        raise TimeoutError('owned GUI readiness timed out: ' + str(last_error))


    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.close()
            raise

