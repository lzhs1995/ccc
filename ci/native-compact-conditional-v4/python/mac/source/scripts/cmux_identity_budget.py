"""One monotonic budget shared by every read in a hook caller resolution."""
from contextlib import contextmanager
from contextvars import ContextVar
import subprocess
import time

_deadline = ContextVar('cmux_identity_deadline', default=None)


def timeout(default=5.0):
    deadline = _deadline.get()
    if deadline is None:
        return default
    left = deadline - time.monotonic()
    if left <= 0:
        raise subprocess.TimeoutExpired('hook caller resolution', 0)
    return min(default, left)


def check():
    timeout()


@contextmanager
def limit(seconds):
    deadline = time.monotonic() + seconds
    previous = _deadline.get()
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        check()
        yield
        check()
    finally:
        _deadline.reset(token)
