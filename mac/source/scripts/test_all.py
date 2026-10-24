#!/usr/bin/env python3
"""Offline test entrypoint; capture fixture chatter without hiding failures."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


class OfflineResult(unittest.TextTestResult):
    """Fixture environments must never inspect the invoking live CLI process.

    Identity tests explicitly install their own ancestry fixtures inside each
    test, overriding this ordinary-client default. Production collection and
    its refusal rules are unchanged.
    """

    def startTest(self, test):
        import cmux_daemon_identity
        self.ancestry = patch.object(cmux_daemon_identity, "process", return_value={
            "pid": 2, "ppid": 1, "argv": ["/offline/test-client"],
            "executable": "/offline/test-client", "env": {},
        })
        self.ancestry.start()
        super().startTest(test)

    def stopTest(self, test):
        try:
            super().stopTest(test)
        finally:
            self.ancestry.stop()

if __name__ == "__main__":
    chatter = io.StringIO()
    # CLI fixtures use offline_test_hook to isolate process/marker inputs too.
    # Preserve the invoking environment; identity cases supply explicit inputs.
    with contextlib.redirect_stdout(chatter), contextlib.redirect_stderr(chatter):
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
        result = unittest.TextTestRunner(stream=sys.__stderr__, verbosity=1,
                                         resultclass=OfflineResult).run(suite)
    print(f"OFFLINE_TESTS total={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
    sys.exit(not result.wasSuccessful())
