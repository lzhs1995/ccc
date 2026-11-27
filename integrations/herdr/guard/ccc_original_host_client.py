"""Original guard to its own future terminal child host; no terminal input."""
import json
import os
from pathlib import Path
import socket
import stat
import sys

from ccc_herdr_exit_context import PinnedDirectory,canonical,need


class OriginalHostClient:
    def __init__(self,directory,*,directory_identity,host_pid,host_birth,scope):
        self.directory=Path(directory);self.directory_identity=directory_identity
        self.pid=host_pid;self.birth=host_birth;self.scope=scope

    def call(self,request,*,before_write=None):
        need(request.get('op') in {'observe','lookup','resume'},'unknown_original_host_operation')
        need(request['op']!='resume' or callable(before_write),'host_resume_requires_original_admission')
        raw=canonical(request)+b'\n';need(len(raw)<=16384,'host_request_too_large')
        with PinnedDirectory(self.directory,expected=self.directory_identity) as directory:
            path=self.directory/'host.sock';before=path.lstat()
            need(stat.S_ISSOCK(before.st_mode) and before.st_uid==os.getuid()
                 and not before.st_mode&0o077,'host_endpoint_not_private')
            if before_write is not None:before_write()
            directory.check()
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(15);connection.connect(str(path))
                peer=int.from_bytes(connection.getsockopt(0,2,4),sys.byteorder,signed=True)
                need(peer==self.pid and self.scope.birth(peer)==self.birth,'original_host_peer_changed')
                after=path.lstat()
                need((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'original_host_socket_changed')
                connection.sendall(raw)
                data=b''
                while b'\n' not in data and len(data)<=65536:
                    block=connection.recv(min(4096,65537-len(data)))
                    need(bool(block),'original_host_reply_incomplete');data+=block
                line,extra=data.split(b'\n',1)
                need(len(data)<=65536 and not extra.strip(),'original_host_reply_invalid')
                response=json.loads(line)
                need(isinstance(response,dict),'original_host_reply_not_object')
                return response
