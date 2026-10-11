"""Unit tests never send notifications to the operator's desktop.

Keep the asynchronous notification path real so its callers and arguments can
still be tested. Only the final OS boundary is replaced, for the lifetime of
this test process. A module-level patch.start() would be undone by unrelated
tests calling patch.stopall(), so this test-only replacement is deliberate.
The production module is unchanged outside a process importing this package.
"""

import cmux_codex_watch as _watch


_watch_notify_impl = _watch.WatchDaemon._notify


def _discard_desktop_notification(self, title: str, body: str) -> None:
    pass


_watch.WatchDaemon._notify = _discard_desktop_notification
