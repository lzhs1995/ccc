import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

import cmux_codex_watch as core
import tests


class NotificationIsolationTests(unittest.TestCase):
    def test_discovery_isolates_notifications_before_loading_selected_tests(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", """
import sys
import unittest
from unittest import mock
import cmux_codex_watch as core
assert 'tests' not in sys.modules
real_notify = core.WatchDaemon._notify
loader = unittest.TestLoader()
suite = loader.discover('tests', pattern='test_watch.py', top_level_dir='.')
assert suite.countTestCases() > 0
assert not loader.errors, loader.errors
assert 'tests' in sys.modules
assert core.WatchDaemon._notify is not real_notify
mock.patch.stopall()
with mock.patch.object(core.subprocess, 'run') as run:
    core.WatchDaemon._notify(object.__new__(core.WatchDaemon), 'CCC test', 'isolated')
    run.assert_not_called()
"""],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_async_notification_keeps_its_arguments_without_os_process(self):
        daemon = object.__new__(core.WatchDaemon)
        thread_type = threading.Thread
        threads = []

        def thread_factory(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            threads.append(thread)
            return thread

        with mock.patch.object(daemon, "_notify", wraps=daemon._notify) as notify, \
                mock.patch.object(core.threading, "Thread", side_effect=thread_factory), \
                mock.patch.object(core.subprocess, "run") as run:
            daemon._notify_async("CCC 已修复 Claude Hook", "测试事件", name="test-notify")
            self.assertEqual(len(threads), 1)
            threads[0].join(timeout=2)
            self.assertFalse(threads[0].is_alive())
            self.assertEqual(threads[0].name, "test-notify")
            notify.assert_called_once_with("CCC 已修复 Claude Hook", "测试事件")
            run.assert_not_called()

    def test_unrelated_stopall_cannot_restore_desktop_notifications(self):
        # Run independently: an unrelated test's stopall must not remove the
        # suite-wide isolation, and must not touch this test's own patches.
        result = subprocess.run(
            [sys.executable, "-B", "-c", """
import tests
from unittest import mock
import cmux_codex_watch as core
mock.patch.stopall()
daemon = object.__new__(core.WatchDaemon)
with mock.patch.object(core.subprocess, 'run') as run:
    daemon._notify('CCC test', 'no desktop side effect')
    run.assert_not_called()
"""],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_notification_boundary_is_verified_with_mocked_subprocess(self):
        daemon = object.__new__(core.WatchDaemon)
        title, body = 'CCC "notice"', "Hook 修复\n下一行"
        with mock.patch.object(core.subprocess, "run") as run:
            tests._watch_notify_impl(daemon, title, body)
        run.assert_called_once_with(
            ["/usr/bin/osascript", "-e",
             f"display notification {json.dumps(body)} with title {json.dumps(title)}"],
            check=False, timeout=3, capture_output=True,
        )


if __name__ == "__main__":
    unittest.main()
