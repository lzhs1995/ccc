"""Bind one real Supervisor PTY activation to an original retained cohort.

Confirmation written is not task consumption. The coordinator must subsequently
verify the original owner settlement and each native first task. The caller keeps
the PTY handle through actual child exit and uses the existing PTY close protocol.
"""
import copy

from ccc_native_standby import identifier
from ccc_standby_runner import connect
from cmux_supervisor_tui import private_batch_prompt, access_batch_prompt
from tools.standby_ui_pty import SupervisorPTY, bound_focus_prefix


class BoundActivation:
    def __init__(self, pool, batch_id, directory, *, ui_factory=SupervisorPTY):
        self.pool, self.key = pool, identifier(batch_id)
        pool._health()
        self.handle = pool.handles[self.key]
        if self.handle.get('ui_activation_consumed'):
            raise ValueError('original UI activation already consumed')
        if self.handle['opening'] is None or self.handle['settlement'] is not None:
            raise ValueError('unsettled original open cohort required')
        self.invocation = pool.specs[self.key][0]
        self.client = connect(self.invocation.value)
        self.mode = pool.expected[self.key]['mode']
        if self.mode not in ('b', 'N'):
            raise ValueError('only explicit standby activation modes permitted')
        self.original = self._target()
        workspace = self._workspace()
        self.prefix = bound_focus_prefix(workspace, self.original['workspace_id'],
                                         self.original['surface_id'])
        self.prompt = (private_batch_prompt if self.mode == 'b' else access_batch_prompt)(workspace['ref'])
        self.workspace = copy.deepcopy(workspace)
        # A spawn or write with an unknown outcome consumes this batch's driver.
        self.handle['ui_activation_consumed'] = True
        self.ui = None
        self.failure = None
        self.written = set()
        self.closing = False
        self.close_attempted = False
        self.closed = False
        self.close_error = None
        self.original_child = None
        self.handle['ui_activation'] = self
        try:
            self.ui = ui_factory(self.invocation.value['config_path'],
                                 self.original['surface_id'], directory)
            self.original_child = self.ui.child
        except BaseException as exc:
            self.failure = type(exc).__name__
            raise

    def _target(self):
        self.pool._health()
        owner = self.handle['runner'].owner
        if owner is None or owner.status()['state'] != 'ready':
            raise ValueError('original cohort is not ready for UI activation')
        row = owner.service.preparation.observe_for_activation(0)
        if (not isinstance(row, dict)
                or row.get('workspace_id') != self.invocation.value['workspace_id']):
            raise ValueError('original slot zero witness unavailable')
        identifier(row['surface_id'])
        return {key: copy.deepcopy(row[key]) for key in
                ('workspace_id', 'surface_id', 'session_id', 'pid', 'birth')}

    def _workspace(self):
        tree = self.client.tree()
        matches = [w for window in tree.get('windows', []) for w in window.get('workspaces', [])
                   if identifier(w['id']) == identifier(self.original['workspace_id'])]
        if len(matches) != 1:
            raise ValueError('unique original workspace required')
        return matches[0]

    def _current(self):
        self.invocation.current()
        if self._target() != self.original:
            raise ValueError('original activation target changed')
        workspace = self._workspace()
        if (bound_focus_prefix(workspace, self.original['workspace_id'],
                               self.original['surface_id']) != self.prefix
                or workspace['ref'] != self.workspace['ref']):
            raise ValueError('original UI target moved or changed')

    def poll(self, *, timeout=0):
        """At most one action/confirmation each; errors never permit a resend.

        Return means only PTY write status. It never substitutes for settlement.
        Caller supplies the total observation deadline and retains this handle.
        """
        if self.closing:
            raise ValueError('activation closing; no further input permitted')
        if self.failure is not None:
            raise ValueError('activation failed or outcome unknown; do not retry: ' + self.failure)
        try:
            return self._poll(timeout)
        except BaseException as exc:
            self.failure = type(exc).__name__
            raise

    def close(self, *, timeout=0):
        """Observe closure of only the retained private Supervisor child.

        No terminal key is safe after uncertain action/confirmation writes.
        Signal this owned child once, then keep polling the same handle. This
        never signals its process group, a Runner or a native model process.
        """
        import math
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 <= timeout <= 1):
            raise ValueError('private UI close poll must be zero to one second')
        self.closing = True
        if self.closed:
            return True
        if self.ui is None:
            # The factory owns cleanup of an incomplete construction.
            self.closed = True
            return True
        if self.ui.child is not self.original_child:
            raise ValueError('private Supervisor child handle changed')
        child = self.original_child
        if child.poll() is None and not self.close_attempted:
            self.close_attempted = True
            try:
                self.ui._record('retained_supervisor_stop_intent', pid=child.pid)
                child.terminate()
            except BaseException as exc:
                self.close_error = type(exc).__name__
                raise
        if child.poll() is None:
            self.ui.poll(timeout)
        if child.poll() is None:
            return False
        if self.close_error is not None:
            raise ValueError('private UI closure evidence failed: '+self.close_error)
        try:
            self.ui.close()
        except BaseException as exc:
            self.close_error = type(exc).__name__
            raise
        self.closed = True
        return True

    def _poll(self, timeout):
        if self.ui.sent != self.written:
            raise ValueError('UI input attempted outside this activation or outcome unknown')
        if 'confirmation' in self.written:
            return 'confirmation_written'
        if self.ui.poll(timeout) is not None:
            raise ValueError('original Supervisor exited before confirmation')
        if 'action' not in self.written:
            if self.prefix not in self.ui.data:
                return 'waiting_focus'
            self._current()
            self.ui.press_action(self.mode)
            self.written.add('action')
            return 'action_written'
        expected = (self.prompt+' [y/N]').encode()
        if expected not in self.ui.data[self.ui.action_offset:]:
            return 'waiting_confirmation'
        self._current()
        self.ui.confirm(self.prompt)
        self.written.add('confirmation')
        return 'confirmation_written'
