"""Cross-process locks for writes to a local data directory."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager


def lock_data_dir() -> str:
    """Return and create the data directory used by the shared locks."""
    data_dir = os.environ.get("LOCAL_DATA_DIR")
    if not data_dir:
        from opencrab.config import get_settings

        data_dir = get_settings().local_data_dir
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


@contextmanager
def file_lock(filename: str, data_dir: str | None = None) -> Iterator[None]:
    """Hold an exclusive lock file until the enclosed operation completes."""
    lock_path = os.path.join(data_dir or lock_data_dir(), filename)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@contextmanager
def write_lock() -> Iterator[None]:
    """Serialise writes that share the local stores."""
    with file_lock("write.lock"):
        yield
