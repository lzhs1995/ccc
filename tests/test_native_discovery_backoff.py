"""Slow native discovery cannot monopolize observation or reuse another generation."""
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ccc_native_lanes import NativeDispatcher


class ControlledPool:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        future = Future()
        self.calls.append((future, function, args))
        return future

    def finish(self, index):
        future, function, args = self.calls[index]
        future.set_running_or_notify_cancel()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)

    def shutdown(self, **kwargs):
        for future, _, _ in self.calls:
            if not future.done():
                future.cancel()


class DiscoveryBackoffTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.clock = patch('ccc_native_lanes.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.reader = Mock(return_value={'kind': 'unknown'})
        self.dispatch = NativeDispatcher(SimpleNamespace(
            codex_queue_recovery=SimpleNamespace(current_turn=self.reader)), {})
        self.pool = ControlledPool()
        self.dispatch._discovery_pool = self.pool
        self.addCleanup(self.dispatch.close)
        self.targets = {'surface': {'surface_id': 'surface', 'workspace_id': 'workspace', 'pane_id': 'pane'}}
        self.hints = {'surface': self.hint()}

    def hint(self, pid=7, birth=(100, 123), thread='original'):
        return {'agent_kind': 'codex', 'agent_pids': [pid], 'agent_birth': birth,
                'foreground_generation': ('ok', thread, 'epoch')}

    def discover(self, bindings=None):
        self.dispatch._discover_unbound(self.targets, bindings or {}, self.hints)

    def test_slow_unknown_backoff_starts_at_actual_completion(self):
        self.discover()
        self.now = 37.0
        self.discover()
        self.assertEqual(len(self.pool.calls), 1)
        self.pool.finish(0)
        self.discover()
        self.now = 37.249
        self.discover()
        self.assertEqual(len(self.pool.calls), 1)
        self.now = 37.25
        self.discover()
        self.assertEqual(len(self.pool.calls), 2)

    def test_exception_uses_completion_time_and_repeated_failure_caps_at_two_seconds(self):
        self.reader.side_effect = OSError('original writer temporarily unavailable')
        self.discover()
        for index, delay in enumerate((.25, .5, 1, 2, 2)):
            self.now += 37
            self.pool.finish(index)
            self.discover()
            self.assertAlmostEqual(self.dispatch._discovery_state['surface']['due'], self.now + delay)
            self.now += delay - .001
            self.discover()
            self.assertEqual(len(self.pool.calls), index + 1)
            self.now += .001
            self.discover()
            self.assertEqual(len(self.pool.calls), index + 2)

    def test_success_with_old_bindings_snapshot_never_immediately_rescans(self):
        self.reader.return_value = {'kind': 'task_started', 'session_id': 'original',
                                   'pid': 7, 'process_start': 100, 'birth': [100, 123]}
        self.discover()
        self.pool.finish(0)
        self.now = 99  # completion predates this refresh; its source snapshot was empty
        self.discover()
        self.assertEqual(len(self.pool.calls), 1)
        self.now += .25
        self.discover({'surface': NativeDispatcher.binding(self.reader.return_value)})
        self.assertEqual(len(self.pool.calls), 1)
        self.assertNotIn('surface', self.dispatch._discovery_state)

    def test_identical_hint_and_temporary_hint_gap_preserve_backoff(self):
        self.discover()
        self.pool.finish(0)
        self.discover()
        self.hints['surface'] = None
        self.now = .05
        self.discover()
        self.hints['surface'] = self.hint()
        self.now = .1
        self.discover()
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.dispatch._discovery_state['surface']['failures'], 1)

    def test_new_foreground_resets_only_its_own_old_backoff(self):
        self.discover()
        self.pool.finish(0)
        self.discover()
        self.now = .01
        self.hints['surface'] = self.hint(thread='new-foreground')
        self.discover()
        self.assertEqual(len(self.pool.calls), 2)
        self.assertEqual(self.dispatch._discovery_state['surface']['failures'], 0)

    def test_old_inflight_generation_cannot_delay_or_authorize_replacement(self):
        for change in ('birth', 'pid', 'workspace', 'foreground'):
            with self.subTest(change=change):
                self.set_generation_case(change)

    def set_generation_case(self, change):
        start = len(self.pool.calls)
        self.dispatch._discovery_state.clear()
        self.discover()
        if change == 'birth':
            self.hints['surface']['agent_birth'] = (100, 124)
        elif change == 'pid':
            self.hints['surface']['agent_pids'] = [8]
        elif change == 'workspace':
            self.targets['surface']['workspace_id'] = 'moved'
        else:
            self.hints['surface']['foreground_generation'] = ('ok', 'new', 'epoch')
        self.discover()
        self.assertEqual(len(self.pool.calls), start + 1)
        self.now += 37
        self.pool.finish(start)
        self.discover()
        self.assertEqual(len(self.pool.calls), start + 2)
        self.assertEqual(self.dispatch._discovery_state['surface']['failures'], 0)
        self.pool.finish(start + 1)
        self.targets.clear()
        self.discover()
        self.assertFalse(self.dispatch._discovery_state)
        self.targets = {'surface': {'surface_id': 'surface', 'workspace_id': 'workspace', 'pane_id': 'pane'}}
        self.hints = {'surface': self.hint()}

    def test_removed_or_paused_target_gets_no_new_read(self):
        self.discover()
        self.targets.clear()
        self.pool.finish(0)
        self.discover()
        self.assertFalse(self.dispatch._discovery_state)
        self.targets = {'surface': {'surface_id': 'surface', 'workspace_id': 'workspace', 'paused': True}}
        self.discover()
        self.assertEqual(len(self.pool.calls), 1)

    def test_two_inflight_limit_has_no_unbounded_queue_and_rotates_fairly(self):
        self.targets = {str(i): {'surface_id': str(i), 'workspace_id': 'workspace'} for i in range(8)}
        self.hints = {sid: self.hint(pid=i + 10) for i, sid in enumerate(self.targets)}
        self.discover()
        for _ in range(10):
            self.now += 37
            self.discover()
        self.assertEqual(len(self.pool.calls), 2)
        for first in (0, 2, 4):
            self.pool.finish(first)
            self.pool.finish(first + 1)
            self.discover()
            self.assertEqual(len(self.dispatch._discovering), 2)
        self.assertEqual([call[2][0]['surface_id'] for call in self.pool.calls], list(self.targets))

    def test_bound_source_bypasses_discovery_backoff(self):
        self.discover()
        self.pool.finish(0)
        self.discover()
        self.discover({'surface': {'session_id': 'original', 'pid': 7, 'process_start': 100}})
        self.assertNotIn('surface', self.dispatch._discovery_state)
        self.assertEqual(len(self.pool.calls), 1)


if __name__ == '__main__':
    unittest.main()
