"""Resolve a standby original's later rollout from its live writable files.

Reuses the acceptance observer's PID/birth/argv/workspace/writer checks;
does not manufacture a transcript filename or require an activation manifest.
"""
import copy
import hashlib
import json
from pathlib import Path
import threading

from ccc_standby_acceptance import FirstTaskObserver
from ccc_codex_queue import IncompleteVnodeRead, VnodeInventoryChanged


class OriginalTranscript(FirstTaskObserver):
    def __init__(self, witness, claim_path):
        self.row = copy.deepcopy(witness)
        self.claim_path = Path(claim_path)
        self.claim_bytes = self.claim_path.read_bytes()
        if hashlib.sha256(self.claim_bytes).hexdigest() != self.row['claim_sha256']:
            raise ValueError('original claim hash differs from witness')
        self.claim = json.loads(self.claim_bytes)
        for key in ('job_id', 'index', 'launch_id', 'surface_id', 'workspace_id'):
            if self.claim[key] != self.row[key]:
                raise ValueError('original claim identity differs from witness')
        if (self.claim['bootstrap_pid'] != self.row['pid']
                or self.claim['bootstrap_birth'] != self.row['birth']):
            raise ValueError('original claim process differs from witness')
        self.hook = {'sessions_root': str(
            Path(self.row['writer_lock']).parent.parent / 'sessions')}
        self._resolved = None
        self._resolver_lock = threading.Lock()

    def __call__(self):
        try:
            return self._resolve()
        except (IncompleteVnodeRead, VnodeInventoryChanged):
            # No cached path can stand in for a complete current observation.
            # Preserve the original identity for the next deadline-bound poll.
            return None

    def _resolve(self):
        with self._resolver_lock:
            if self.claim_path.read_bytes() != self.claim_bytes:
                raise ValueError('original claim changed')
            root, files = self._live(self.row, self.claim, self.hook)
            path = self._transcript_path(self.row, self.hook, root, files)
            observed = None
            if path is not None:
                info = path.lstat()
                observed = (path, info.st_dev, info.st_ino)
                expected = {'device': info.st_dev, 'inode': info.st_ino}
                if files.get(path) != expected:
                    raise ValueError('original rollout is not writable by bound process')
            if self._resolved is not None and observed != self._resolved:
                error = ValueError('original rollout changed or disappeared')
                previous_path, device, inode = self._resolved
                try:
                    current = previous_path.lstat()
                    disk = {'device': current.st_dev, 'inode': current.st_ino,
                            'mode': current.st_mode, 'bytes': current.st_size}
                except OSError as exc:
                    disk = {'error': type(exc).__name__, 'errno': exc.errno}
                error.rollout_event = {
                    'kind': 'original_transcript_binding_lost',
                    'index': self.row['index'], 'session_id': self.row['session_id'],
                    'pid': self.row['pid'], 'birth': self.row['birth'],
                    'previous': {'path': str(previous_path), 'device': device, 'inode': inode},
                    'observed': None if observed is None else {
                        'path': str(observed[0]), 'device': observed[1], 'inode': observed[2]},
                    'previous_path_on_disk': disk,
                    'previous_path_writable': files.get(previous_path),
                    'writable_file_count': len(files),
                }
                raise error
            final_root, final_files = self._live(self.row, self.claim, self.hook)
            if final_root != root or self.claim_path.read_bytes() != self.claim_bytes:
                raise ValueError('original binding changed during resolution')
            if path is not None:
                info = path.lstat()
                if ((path, info.st_dev, info.st_ino) != observed
                        or final_files.get(path) != expected):
                    raise ValueError('original rollout changed during resolution')
                self._resolved = observed
            return path
