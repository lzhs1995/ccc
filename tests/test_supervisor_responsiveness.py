"""A stalled cmux inventory must not own the panel's keyboard thread."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cmux_supervisor_tui as tui


class QuietSource:
    def maybe_refresh(self, *args, **kwargs):
        pass

    def snapshot(self):
        return {}


class BlockingInventory:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def tree(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        return {"windows": []}

    def top_all(self):
        return {"windows": []}


class PanelResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = BlockingInventory()
        quiet = QuietSource()
        self.model = tui.SupervisorModel(Path(self.tmp.name) / 'config.json',
            client=self.client, janitor=quiet, sessions=quiet, stack=quiet, collab=quiet)
        self.config = {'targets': [], 'workspace_rules': [], 'version': 1}
        self.model.store.load = lambda: dict(self.config)
        for target in ('cmux_supervisor_tui.core.load_json',
                       'cmux_supervisor_tui.core.ClaudeHookSettingsManager.inspect'):
            patcher = patch(target, return_value={})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.finish)

    def finish(self):
        self.model.close()
        self.client.release.set()
        if self.model._refresh_thread:
            self.model._refresh_thread.join(2)

    def test_refresh_is_nonblocking_and_forced_requests_coalesce(self):
        started = time.monotonic()
        self.assertTrue(self.model.maybe_refresh(force=True))
        self.assertLess(time.monotonic() - started, .2)
        self.assertTrue(self.client.entered.wait(1))
        for _ in range(30):
            self.assertFalse(self.model.maybe_refresh(force=True))
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.model.config, {})  # Never publish half a snapshot.
        self.config['version'] = 2
        self.client.release.set()
        self.model._refresh_thread.join(2)
        self.model.maybe_refresh()
        self.assertEqual(self.model.config, {})  # Old forced result was discarded.
        self.model._refresh_thread.join(2)
        self.model.maybe_refresh()
        self.assertEqual(self.model.config['version'], 2)
        self.assertEqual(self.client.calls, 2)

    def test_filter_refresh_and_quit_work_while_inventory_is_blocked(self):
        entered = self.client.entered
        class Screen:
            keys = iter((ord('f'), ord('R'), ord('q')))
            def keypad(self, value):
                pass
            def timeout(self, value):
                pass
            def getch(self):
                if not entered.wait(1):
                    raise AssertionError('inventory worker did not start')
                return next(self.keys)
        with patch.object(tui.curses, 'curs_set'), patch.object(tui, 'init_colors'), patch.object(tui, '_draw') as draw:
            started = time.monotonic()
            tui._run(Screen(), self.model)
            self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(draw.call_count, 3)
        self.assertFalse(self.client.release.is_set())
        self.assertEqual(self.client.calls, 1)

    def test_close_does_not_join_stalled_io_or_publish_late_result(self):
        self.model.maybe_refresh(force=True)
        self.assertTrue(self.client.entered.wait(1))
        started = time.monotonic()
        self.model.close()
        self.assertLess(time.monotonic() - started, .1)
        self.client.release.set()
        self.model._refresh_thread.join(2)
        self.assertFalse(self.model.maybe_refresh(force=True))
        self.assertEqual(self.model.config, {})

    def test_slow_mutation_does_not_block_keys_or_repeat_the_action(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def operation():
            calls.append('pause')
            entered.set()
            release.wait(5)
        try:
            started = time.monotonic()
            self.model.start_action(operation, 'done')
            self.assertLess(time.monotonic() - started, .2)
            self.assertTrue(entered.wait(1))
            self.model.start_action(operation, 'duplicate')
            self.assertIsNone(self.model.poll_action())
            self.assertEqual(calls, ['pause'])
        finally:
            release.set()
            self.model._action_thread.join(2)
        self.assertEqual(self.model.poll_action(), 'done')

    def test_interrupt_has_priority_over_a_stalled_control_action(self):
        release = threading.Event()
        self.model.start_action(lambda: release.wait(5), 'old result')
        old = self.model._action_thread
        try:
            self.model.start_action(lambda: None, 'pause result', priority=True)
            self.model._action_thread.join(1)
            self.assertEqual(self.model.poll_action(), 'pause result')
        finally:
            release.set()
            old.join(1)
        self.assertIsNone(self.model.poll_action())


if __name__ == '__main__':
    unittest.main()
