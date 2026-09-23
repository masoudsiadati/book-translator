"""Bounded POSIX advisory locks for local queue coordination."""

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class QueueLockTimeout(TimeoutError):
    """Raised when a local queue lock cannot be acquired in time."""


@contextmanager
def queue_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    if timeout_seconds < 0:
        raise ValueError("lock timeout must not be negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise QueueLockTimeout(f"timed out acquiring queue lock: {path}") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
