"""Persist native Goal notifications without changing the shared MAC client.

A thread/goal/get result is current state, not evidence of which turn wrote it.
Only a received notification naming the thread and turn can supply that evidence.
Connections are retained between snapshots; reconnects leave an explicit gap and
never promote a notification from an earlier connection into a new observation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from ccc_compact_support import owned_file, sync_directory

SCHEMA = 'ccc-native-goal-notice-v1'
MAX_RECORD = 256 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


class GoalNotifications:
    def __init__(self, path, session_id):
        self.path = Path(path).resolve()
        self.session_id = session_id
        self.connection_id = None
        self.socket_identity = None
        self.cursor = 0
        self.last = None
        self.file_id = None
        self.end = None
        self.healthy = False

    def _append(self, kind, **fields):
        value = dict(schema=SCHEMA, kind=kind, observed_at=time.time(),
                     session_id=self.session_id, connection_id=self.connection_id,
                     socket_identity=self.socket_identity, cursor=self.cursor, **fields)
        raw = canonical(value) + b'\n'
        if len(raw) > MAX_RECORD:
            raise ValueError('oversized native Goal notification')
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+b') as stream:
            st = os.fstat(stream.fileno())
            identity = [st.st_dev, st.st_ino]
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                    or st.st_mode & 0o022 or st.st_nlink != 1
                    or self.file_id is not None and self.file_id != identity
                    or self.end is not None and st.st_size != self.end):
                raise ValueError('native Goal journal changed')
            if st.st_size:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b'\n':
                    raise ValueError('incomplete native Goal journal record')
            offset = st.st_size
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            after = self.path.stat(follow_symlinks=False)
            if ([after.st_dev, after.st_ino] != identity
                    or after.st_size != offset + len(raw)):
                raise ValueError('native Goal journal replaced during append')
            self.file_id, self.end = identity, after.st_size
        sync_directory(self.path.parent)
        return dict(path=str(self.path), file_id=identity, offset=offset, bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(), connection_id=self.connection_id,
                    cursor=self.cursor, socket_identity=self.socket_identity)

    def connect(self, socket_identity):
        self.healthy = False
        self.last = None
        self.connection_id = uuid.uuid4().hex
        self.socket_identity = dict(socket_identity)
        self.cursor = 0
        self._append('CONNECTED_WITH_PRIOR_GAP', reason='no claim of observations before this connection')
        self.healthy = True

    def disconnect(self, reason):
        self.healthy = False
        self.last = None
        try:
            if self.connection_id is not None:
                self._append('DISCONNECTED_GAP', reason=str(reason)[:240])
        finally:
            self.connection_id = None

    def observe(self, envelope):
        if envelope.get('method') != 'thread/goal/updated' or 'id' in envelope:
            return
        params = envelope.get('params')
        if not isinstance(params, dict) or params.get('threadId') != self.session_id:
            return
        # Even a notice without a turn invalidates an older causal anchor.
        # Preserve the complete parsed envelope, not a reconstructed Goal.
        self.last = None
        self.cursor += 1
        try:
            source = self._append('GOAL_NOTIFICATION', envelope=envelope)
            self.last = dict(thread_id=params.get('threadId'), turn_id=params.get('turnId'),
                             goal=params.get('goal'), source=source)
        except BaseException:
            self.healthy = False
            raise

    def blocked_event(self, full_goal, turn_id):
        event = self.last
        if (not self.healthy or not event or not turn_id
                or event.get('thread_id') != self.session_id or event.get('turn_id') != turn_id
                or not isinstance(full_goal, dict) or full_goal.get('status') != 'blocked'
                or event.get('goal') != full_goal):
            return None
        # Verify the persisted bytes before granting any use of this anchor.
        verify_event(event, self.session_id, turn_id, full_goal)
        return json.loads(canonical(event))


def verify_event(event, session_id, turn_id, full_goal):
    source = event['source']
    if (event.get('thread_id') != session_id or event.get('turn_id') != turn_id
            or event.get('goal') != full_goal or not turn_id
            or not isinstance(full_goal, dict) or full_goal.get('status') != 'blocked'
            or full_goal.get('threadId') != session_id
            or full_goal.get('createdAt') is None or full_goal.get('updatedAt') is None
            or not isinstance(source.get('cursor'), int) or source['cursor'] <= 0
            or not isinstance(source.get('offset'), int) or source['offset'] < 0
            or not isinstance(source.get('bytes'), int) or not 0 < source['bytes'] <= MAX_RECORD):
        raise ValueError('native Goal event does not match the complete blocked revision')
    with owned_file(source['path']) as (stream, st):
        if [st.st_dev, st.st_ino] != source.get('file_id'):
            raise ValueError('native Goal evidence replaced')
        stream.seek(source['offset'])
        raw = stream.read(source['bytes'])
        if len(raw) != source['bytes'] or hashlib.sha256(raw).hexdigest() != source['sha256']:
            raise ValueError('native Goal evidence changed')
        record = json.loads(raw)
    envelope = record.get('envelope') or {}
    socket = source.get('socket_identity') or {}
    if (record.get('schema') != SCHEMA or record.get('kind') != 'GOAL_NOTIFICATION'
            or record.get('session_id') != session_id
            or record.get('connection_id') != source.get('connection_id')
            or not source.get('connection_id') or record.get('cursor') != source['cursor']
            or record.get('socket_identity') != socket
            or socket.get('uid') != os.geteuid() or socket.get('peer_uid') != os.geteuid()
            or socket.get('mode') != 0o600 or not socket.get('inode') or not socket.get('path')
            or envelope.get('method') != 'thread/goal/updated' or 'id' in envelope
            or (envelope.get('params') or {}).get('threadId') != session_id
            or (envelope.get('params') or {}).get('turnId') != turn_id
            or (envelope.get('params') or {}).get('goal') != full_goal):
        raise ValueError('unverified native Goal notification source')
    return True


def observing_rpc(base, notices):
    """Adapt only this CCC connection; server requests remain unacknowledged."""
    class ObservedRPC(base):
        def __init__(self, **kwargs):
            self.notice_connected = False
            self.notice_closed = False
            try:
                super().__init__(**kwargs)
                self._notice_connect()
            except BaseException:
                # The socket can be connected even if persisting CONNECTED
                # fails. No failed observer may leave a live, untracked socket.
                try:
                    self.close()
                except Exception:
                    pass
                raise

        def _notice_connect(self):
            if not self.notice_connected:
                notices.connect(self.socket_identity)
                self.notice_connected = True

        def _receive_json(self):
            envelope = super()._receive_json()
            if isinstance(envelope, dict) and envelope.get('method') == 'thread/goal/updated':
                self._notice_connect()
                notices.observe(envelope)
            return envelope

        def close(self):
            if self.notice_closed:
                return
            self.notice_closed = True
            try:
                notices.disconnect('observation connection closed')
            finally:
                self.notice_connected = False
                # Base construction may fail before it creates a socket.
                if hasattr(self, 'sock'):
                    super().close()

    return ObservedRPC
