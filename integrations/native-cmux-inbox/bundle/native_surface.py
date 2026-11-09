"""Actual cmux-created terminal launcher; never allocates or emulates a PTY."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'reader'))
import fixture_common as f
import cmux_daemon_identity as native


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--launch-sha256',required=True)
    args=parser.parse_args()
    root=f.require_runner(args.root)
    launch=json.loads(f.pinned(root/'LAUNCH.json',args.launch_sha256))
    f.need(os.isatty(0) and os.isatty(1),'real cmux terminal required')
    for key in ('CMUX_WORKSPACE_ID','CMUX_SURFACE_ID'):
        f.need(bool(os.environ.get(key)),'cmux did not assign actual terminal UUIDs')
    env=dict(launch['environment'])
    env.update({k:os.environ[k] for k in ('CMUX_WORKSPACE_ID','CMUX_SURFACE_ID')})
    f.need(bool(os.environ.get('TERM')),'real terminal type missing')
    env['TERM']=os.environ['TERM']
    f.need(os.environ.get('CMUX_SOCKET_PATH')==env['CMUX_SOCKET_PATH'],'terminal socket differs from owned GUI')
    before=native.process(os.getpid())
    f.save_once(root/'NATIVE-SPAWN-INTENT.json',dict(pid=os.getpid(),birth=before['birth'],ppid=before['ppid'],
        tty=os.ttyname(0),environment=env,argv=launch['argv'],launch_sha256=args.launch_sha256))
    os.execve(launch['argv'][0],launch['argv'],env)


if __name__=='__main__': main()
