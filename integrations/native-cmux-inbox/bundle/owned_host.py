"""One fixed GUI generation; reuse original Aqua/start/peer/close functions."""
import ctypes
import json
import os
from pathlib import Path
import pwd

import cmux_daemon_identity as native
import fixed_app_admission as admission
import fixture_common as f
from original_args import parse_arguments
from original_gui import OriginalOwnedMethods, gui_session, persistence_paths


class KernelScope:
    @staticmethod
    def birth(pid):
        try:
            return native.process(pid, arguments=False)['birth']
        except native.ProcessExited:
            return None

    @staticmethod
    def arguments(pid):
        before = native.process(pid, arguments=False)
        size = os.sysconf('SC_ARG_MAX')
        f.need(0 < size <= 2 * 1024 * 1024, 'bounded native arguments required')
        buf, length = ctypes.create_string_buffer(size), ctypes.c_size_t(size)
        call = ctypes.CDLL(None, use_errno=True).sysctl
        call.argtypes = [ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t]
        f.need(call((ctypes.c_int*3)(1,49,pid),3,buf,ctypes.byref(length),None,0) == 0,
               'actual process arguments unavailable')
        value = parse_arguments(buf.raw[:length.value])
        f.need(native.process(pid, arguments=False) == before, 'process changed during argument read')
        return value


class OwnedFixedOne(OriginalOwnedMethods):
    def __init__(self, root, proof):
        self.root = f.require_runner(root)
        self.session = gui_session()
        self.pin = admission.require_pin(proof['pin'])
        self.app = Path(proof['app'])
        self.exe, self.cli, _ = admission.validate_bundle(self.app, self.pin, proof['provenance'])
        f.need(proof['consumer_run'] == os.environ['GITHUB_RUN_ID']
               and proof['consumer_attempt'] == os.environ['GITHUB_RUN_ATTEMPT'], 'foreign app admission')
        self.scope = KernelScope
        self.executable_path = lambda pid: native.process(pid)['executable']
        self.runtime = root / 'g'
        self.runtime.mkdir(mode=0o700)
        (self.runtime / 'zsh').mkdir(mode=0o700)
        self.socket = self.runtime / 'c.sock'
        f.need(len(os.fsencode(self.socket)) < 104, 'GUI socket path too long')
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
        f.need(Path(os.environ['HOME']).resolve(strict=True) == home, 'GUI must retain real account HOME')
        paths = persistence_paths(home, self.pin['bundle_id'])
        absent = [paths[k] for k in ('store','state','config','preferences','saved_state')]
        f.need(all(not p.exists() and not p.is_symlink() for p in absent), 'fresh account state required')
        self.env = {k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','LANG','__CF_USER_TEXT_ENCODING') if k in os.environ}
        self.env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin',SHELL='/bin/zsh',ZDOTDIR=str(self.runtime/'zsh'),
            CMUX_ALLOW_SOCKET_OVERRIDE='1',CMUX_SOCKET_ENABLE='1',CMUX_SOCKET_MODE='automation',
            CMUX_SOCKET_PATH=str(self.socket),CMUX_DEBUG_LOG=str(self.runtime/'debug.log'),
            CMUXD_UNIX_PATH=str(self.runtime/'d.sock'),CMUX_TAG=self.pin['tag'],CMUX_BUNDLE_ID=self.pin['bundle_id'],
            CMUX_BUNDLED_CLI_PATH=str(self.cli),CMUX_SHELL_INTEGRATION_DIR=str(self.app/'Contents/Resources/shell-integration'),
            CMUX_REMOTE_DAEMON_ALLOW_LOCAL_BUILD='0')
        self.proc = self.identity = None
        self.generation = 0
        f.save_once(self.runtime/'FRESH-STATE.json',dict(paths=list(map(str,absent)),actual_home=str(home)))

    def start(self):
        f.need(self.generation == 0, 'one GUI generation only; no restart')
        admission.verify_signature(self.app,self.pin)
        result = super().start()
        argv, env = self.scope.arguments(self.proc.pid)
        f.need(argv == [str(self.exe)] and all(env.get(k)==v for k,v in self.env.items()) and self.exact(),
               'actual GUI argv/environment differs')
        f.need(not any(k in env for k in ('CMUXTERM_REPO_ROOT','CMUX_REMOTE_DAEMON_SOURCE_ROOT',
                    'CMUX_REMOTE_DAEMON_BINARY','CMUX_DISABLE_SESSION_RESTORE')), 'foreign GUI source/restore override')
        self.initial_peer = self.peer()
        f.save_once(self.runtime/'ACTUAL-ENVIRONMENT.json',dict(identity=self.identity,environment=self.env,
                     peer=self.initial_peer,signing_scope='original ad-hoc tagged build'))
        return result

    def stable_peer(self):
        value = self.peer()
        f.need(value == self.initial_peer, 'GUI/socket generation changed')
        return value


class OwnedAlias:
    def __init__(self, root, app):
        self.root,self.app = root,Path(app).resolve(strict=True)
        self.path = Path('/Applications/cmux.app')
        self.identity = None

    def __enter__(self):
        f.need(self.app.is_relative_to(self.root) and not self.path.exists() and not self.path.is_symlink(),
               'only an absent disposable-runner alias may be created')
        f.save_once(self.root/'ALIAS-INTENT.json',dict(path=str(self.path),target=str(self.app),overwrite=False))
        os.symlink(self.app,self.path)
        info=self.path.lstat()
        self.identity=(info.st_dev,info.st_ino,os.readlink(self.path))
        try:
            self.check()
            f.save_once(self.root/'ALIAS-CREATED.json',dict(identity=self.identity))
        except BaseException:
            self.check()
            self.path.unlink()
            raise
        return self

    def check(self):
        info=self.path.lstat()
        f.need(self.path.is_symlink() and (info.st_dev,info.st_ino,os.readlink(self.path))==self.identity
               and self.path.resolve(strict=True)==self.app, 'owned alias changed')

    def __exit__(self,*unused):
        self.check()
        self.path.unlink()
        f.save_once(self.root/'ALIAS-CLEANUP.json',dict(removed_original_alias=True,identity=self.identity))
