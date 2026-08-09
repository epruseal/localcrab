"""Cross-process locks for writes to a local data directory."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from time import sleep

if os.name == "nt":
    import msvcrt
else:
    import fcntl


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
    fh = open(lock_path, "a+" if os.name == "nt" else "w")
    try:
        if os.name == "nt":
            fh.seek(0)
            fh.write("\0")
            fh.flush()
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    sleep(0.1)
        else:
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@contextmanager
def write_lock() -> Iterator[None]:
    """Serialise writes that share the local stores."""
    with file_lock("write.lock"):
        yield
