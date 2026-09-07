"""One cross-process lock for a runtime's human review read/modify/write work.

Locks are cooperative and never replace their inode. Raw editor writes are not
transactions; callers must use the review APIs. Provider calls belong outside
this lock so a slow model cannot block a human decision.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_HELD = threading.local()


class ReviewConflict(RuntimeError):
    """The review inputs changed; no stale decision may replace them."""


def _lock_path(path: Path) -> Path:
    path = path.resolve()
    for parent in (path.parent, *path.parents):
        if parent.name in {"review", "personas"}:
            return parent.parent / ".review-state.lock"
    return path.parent / ".review-state.lock"


@contextmanager
def review_lock(path: Path, *, timeout: float = 15.0) -> Iterator[None]:
    lock_path = _lock_path(path)
    key = os.path.normcase(str(lock_path))
    with _GUARD:
        local = _LOCKS.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not local.acquire(timeout=max(0.0, timeout)):
        raise ReviewConflict("Review state is busy; retry after the current write completes")
    held = getattr(_HELD, "keys", None)
    if held is None:
        held = _HELD.keys = set()
    descriptor = None
    acquired = False
    try:
        if key in held:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise ReviewConflict(
                        "Review state is busy; retry after the current write completes"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
    finally:
        if descriptor is not None:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        local.release()


def review_locked(path: Callable):
    """Decorate a short transaction, resolving its runtime from the arguments."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with review_lock(path(*args, **kwargs)):
                return function(*args, **kwargs)
        return wrapped
    return decorate
