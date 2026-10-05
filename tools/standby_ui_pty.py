"""Recorded PTY input to the real Supervisor; never synthesizes UI timing.

The caller owns the retained child handle and must observe its actual exit.
This driver never opens a Codex PTY or signals a process group. An unconfirmed
private Supervisor can be terminated through its retained child handle.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import selectors
import struct
import subprocess
import sys
import termios
import time
import uuid


def bound_focus_prefix(workspace, workspace_id, surface_id):
    """Bind the actual compact focus label to one original tree UUID path."""
    if uuid.UUID(workspace['id']) != uuid.UUID(workspace_id):
        raise ValueError('foreign workspace')
    matches = [(pane, surface) for pane in workspace.get('panes', [])
               for surface in pane.get('surfaces', [])
               if uuid.UUID(surface['id']) == uuid.UUID(surface_id)]
    if len(matches) != 1 or matches[0][1].get('type') != 'terminal':
        raise ValueError('unique original terminal required')
    pane, surface = matches[0]
    digits = []
    for kind, item in [('workspace', workspace), ('pane', pane), ('surface', surface)]:
        match = re.fullmatch(kind + r':([0-9]+)', item.get('ref', ''))
        if not match:
            raise ValueError('missing exact cmux reference')
        digits.append(match[1])
    title = str(workspace.get('title') or '').strip()
    if not title or not title.isascii() or len(title) > 30 or any(ord(c) < 32 for c in title):
        raise ValueError('fixture title must fit original focus label')
    return ('ws{}/p{}/s{}  |  '.format(*digits) + title + '  |  ').encode()


class SupervisorPTY:
    def __init__(self, config_path, surface_id, directory):
        config = Path(config_path).resolve(strict=True)
        uuid.UUID(surface_id)
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, exist_ok=False)
        self.output = (self.directory/'output.bin').open('xb', buffering=0)
        self.events = (self.directory/'events.jsonl').open('x', buffering=1)
        os.chmod(self.directory/'output.bin', 0o600)
        os.chmod(self.directory/'events.jsonl', 0o600)
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 60, 360, 0, 0))
        self.child = None
        self.data = bytearray()
        self.action_offset = None
        self.sent = set()
        command = [sys.executable, '-B', '-c',
            'import sys; from pathlib import Path; from cmux_supervisor_tui import run_tui; '
            'raise SystemExit(run_tui(Path(sys.argv[1]), sys.argv[2]))',
            str(config), surface_id]
        try:
            self._record('spawn_intent', command=command)
            self.child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'TERM': 'xterm-256color'}, start_new_session=True)
            self._original_child = self.child
            from ccc_guard_scope import birth
            self._resource_birth = birth(self.child.pid)
        except BaseException:
            os.close(self.master)
            self.output.close()
            self.events.close()
            raise
        finally:
            os.close(slave)

    def _record(self, kind, **fields):
        self.events.write(json.dumps(dict(kind=kind, wall=time.time(),
            monotonic_ns=time.monotonic_ns(), **fields))+'\n')
        self.events.flush()
        os.fsync(self.events.fileno())

    def resource_identity(self):
        """Return the original live UI generation for fleet resource sampling.

        Missing native inspection never turns into a new baseline. The retained
        Popen handle and its startup generation must both still match. This is
        a sequential observation; the resource collector must recheck birth.
        """
        from ccc_guard_scope import birth
        child = self._original_child
        generation = self._resource_birth
        if (self.child is not child or child.poll() is not None
                or not isinstance(generation, list) or len(generation) != 2
                or any(type(x) is not int for x in generation)
                or generation[0] <= 0 or not 0 <= generation[1] < 1000000
                or birth(child.pid) != generation
                or child.poll() is not None or self.child is not child):
            raise ValueError('original live Supervisor generation required')
        return dict(pid=child.pid, birth=list(generation))

    def poll(self, timeout=0):
        """Drain output without input. Timeout never implies child termination."""
        # Source watchers can legitimately place this PTY above FD_SETSIZE.
        # A kernel selector preserves the same read-only timeout semantics.
        with selectors.DefaultSelector() as selector:
            selector.register(self.master, selectors.EVENT_READ)
            readable = selector.select(min(max(timeout, 0), 1))
        if readable:
            try:
                raw = os.read(self.master, 65536)
            except OSError:
                if self.child.poll() is None:
                    raise
                raw = b''
            if raw:
                self.output.write(raw)
                os.fsync(self.output.fileno())
                self.data.extend(raw)
                if len(self.data) > 16 * 1024 * 1024:
                    raise ValueError('Supervisor output exceeded observation budget')
        return self.child.poll()

    def _send_once(self, phase, key):
        if phase in self.sent or self.child.poll() is not None:
            raise ValueError('UI input consumed or Supervisor exited')
        self.sent.add(phase)  # Uncertain writes are never repeated.
        self._record('input_intent', phase=phase, key=key.decode('ascii'))
        count = os.write(self.master, key)
        self._record('input_written', phase=phase, bytes=count)
        if count != len(key):
            raise OSError('partial Supervisor input; do not retry')

    def press_action(self, mode):
        if mode not in ('b', 'N'):
            raise ValueError('only explicit standby activation keys permitted')
        offset = len(self.data)
        self._send_once('action', mode.encode('ascii'))
        self.action_offset = offset

    def confirm(self, expected_prompt):
        """Require full exact original prompt after this action, not old output.

        Fragmented ANSI rendering fails closed; no stripping that could join
        unrelated screen cells into apparent authorization.
        """
        if self.action_offset is None or not expected_prompt:
            raise ValueError('action and bound confirmation prompt required')
        expected = (expected_prompt+' [y/N]').encode('utf-8')
        if expected not in self.data[self.action_offset:]:
            raise ValueError('bound full confirmation not observed')
        self._record('confirmation_observed', prompt=expected_prompt,
                     output_bytes=len(self.data))
        self._send_once('confirmation', b'y')

    def quit(self):
        """Quit only Supervisor, after the caller has collected action evidence."""
        self._send_once('quit', b'q')

    def stop_unconfirmed(self):
        """Close only our private UI before any confirmation write was attempted.

        Do not send a guessed key to an unrecognized confirmation screen. Once
        confirmation is consumed (including unknown ACK), this path is forbidden.
        """
        if 'confirmation' in self.sent:
            raise ValueError('confirmation consumed; cannot stop as unconfirmed')
        if self.child.poll() is None:
            self._record('stop_unconfirmed_supervisor', pid=self.child.pid)
            self.child.terminate()

    def close(self):
        if self.child is not None and self.child.poll() is None:
            raise ValueError('Supervisor still alive; retain handle and observe')
        self._record('exited', returncode=self.child.returncode if self.child else None)
        os.close(self.master)
        self.output.close()
        self.events.close()
