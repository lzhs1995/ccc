"""Resolve a standby original's later rollout from its live writable files.

Reuses the acceptance observer's PID/birth/argv/workspace/writer checks;
does not manufacture a transcript filename or require an activation manifest.
"""
import copy
import hashlib
import json
from pathlib import Path
import stat
import threading
import time

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
        # An observed inode pins later polls even before both inventories agree.
        # This is never a path returned to a reader or a sender.
        self._provisional = None
        self.last_pending_event = None
        self._resolver_lock = threading.Lock()

    def __call__(self):
        try:
            return self._resolve()
        except (IncompleteVnodeRead, VnodeInventoryChanged):
            # No cached path can stand in for a complete current observation.
            # Preserve the original identity for the next deadline-bound poll.
            return None

    @staticmethod
    def _disk_binding(binding):
        path, device, inode = binding
        try:
            info = path.lstat()
            disk = {'device': info.st_dev, 'inode': info.st_ino,
                    'mode': info.st_mode, 'bytes': info.st_size}
            same = (stat.S_ISREG(info.st_mode) and path.resolve(strict=True) == path
                    and (info.st_dev, info.st_ino) == (device, inode))
            return disk, same
        except OSError as exc:
            return {'error': type(exc).__name__, 'errno': exc.errno}, False

    def _pending(self, event):
        event = dict(event, kind='original_transcript_writable_pending')
        self.last_pending_event = event
        return None

    def _resolve(self):
        with self._resolver_lock:
            self.last_pending_event = None
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
            anchor = self._resolved or self._provisional
            pending_event = None
            if anchor is not None and observed != anchor:
                error = ValueError('original rollout changed or disappeared')
                previous_path, device, inode = anchor
                disk, same = self._disk_binding(anchor)
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
                    'wall': time.time(), 'monotonic': time.monotonic(),
                }
                if observed is not None or not same or previous_path in files:
                    raise error
                pending_event = error.rollout_event
            if observed is not None:
                self._provisional = observed
            final_root, final_files = self._live(self.row, self.claim, self.hook)
            if final_root != root or self.claim_path.read_bytes() != self.claim_bytes:
                raise ValueError('original binding changed during resolution')
            final_path = self._transcript_path(self.row, self.hook, final_root, final_files)
            if pending_event is not None:
                disk, same = self._disk_binding(anchor)
                expected = dict(zip(('device', 'inode'), anchor[1:]))
                pending_event.update(final_path_on_disk=disk,
                                     final_path_writable=final_files.get(anchor[0]))
                if (not same or final_path not in (None, anchor[0])
                        or (anchor[0] in final_files and final_files[anchor[0]] != expected)):
                    error.rollout_event = pending_event
                    raise error
                # Both live identity checks passed; missing rollout ownership
                # is only pending. Even a reopened FD needs the next full poll.
                return self._pending(pending_event)
            if path is not None:
                disk, same = self._disk_binding(observed)
                if not same or final_path != path or final_files.get(path) != expected:
                    error = ValueError('original rollout changed during resolution')
                    error.rollout_event = {
                        'kind': 'original_transcript_final_binding_lost',
                        'index': self.row['index'], 'session_id': self.row['session_id'],
                        'pid': self.row['pid'], 'birth': self.row['birth'],
                        'path': str(path), 'expected': expected,
                        'path_on_disk': disk,
                        'initial_path_writable': files.get(path),
                        'final_path_writable': final_files.get(path),
                        'initial_writable_file_count': len(files),
                        'final_writable_file_count': len(final_files),
                        'wall': time.time(), 'monotonic': time.monotonic(),
                    }
                    if same and final_path is None and path not in final_files:
                        return self._pending(error.rollout_event)
                    raise error
                self._resolved = observed
            return path
