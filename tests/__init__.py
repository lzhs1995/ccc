"""Unit tests never send notifications or take the operator's delivery locks.

Keep the asynchronous notification path real so its callers and arguments can
still be tested. Only the final OS boundary is replaced, for the lifetime of
this test process. A module-level patch.start() would be undone by unrelated
tests calling patch.stopall(), so this test-only replacement is deliberate.
The production module is unchanged outside a process importing this package.

The shared MAC deliveries-v1 root is redirected the same way: every lock any
test takes (pane→target leases included) lands in a private 0700 directory of
this process, never under the operator's HOME. The lock code itself is the
production code, so O_NOFOLLOW, regular/single-link/owner and inode checks
still run. Tests that need their own root keep patching it locally.
"""

import atexit
import shutil
import tempfile
from pathlib import Path

import cmux_codex_watch as _watch


_watch_notify_impl = _watch.WatchDaemon._notify


def _discard_desktop_notification(self, title: str, body: str) -> None:
    pass


_watch.WatchDaemon._notify = _discard_desktop_notification

PRODUCTION_DELIVERY_LOCK_ROOT = _watch.DELIVERY_LOCK_ROOT
ISOLATED_DELIVERY_LOCK_ROOT = Path(tempfile.mkdtemp(prefix="ccc-test-deliveries-")).resolve()
atexit.register(shutil.rmtree, ISOLATED_DELIVERY_LOCK_ROOT, True)
_watch.DELIVERY_LOCK_ROOT = ISOLATED_DELIVERY_LOCK_ROOT
