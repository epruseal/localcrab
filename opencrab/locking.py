"""Cross-process locks for writes to a local data directory."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from time import sleep
from typing import BinaryIO

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


def _open_lock(filename: str, data_dir: str | None = None) -> BinaryIO:
    lock_path = os.path.join(data_dir or lock_data_dir(), filename)
    try:
        return open(lock_path, "r+b")
    except FileNotFoundError:
        return open(lock_path, "w+b")


def _acquire(fh: BinaryIO, *, shared: bool) -> None:
    if os.name == "nt":
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        while True:
            try:
                # Windows msvcrt has no shared flock; exclusive is the safe
                # fallback for the Chroma lifetime lock.
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                sleep(0.1)
    else:
        fcntl.flock(fh, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)


def _release(fh: BinaryIO) -> None:
    if os.name == "nt":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def file_lock(
    filename: str, data_dir: str | None = None, *, shared: bool = False
) -> Iterator[None]:
    """Hold a cross-process lock until the enclosed operation completes."""
    fh = _open_lock(filename, data_dir)
    try:
        _acquire(fh, shared=shared)
        yield
    finally:
        _release(fh)
        fh.close()


def acquire_file_lock(
    filename: str, data_dir: str | None = None, *, shared: bool = False
) -> BinaryIO:
    """Acquire a lock and return its open handle for lifetime-scoped use."""
    fh = _open_lock(filename, data_dir)
    try:
        _acquire(fh, shared=shared)
    except BaseException:
        fh.close()
        raise
    return fh


def release_file_lock(fh: BinaryIO) -> None:
    """Release and close a handle returned by :func:`acquire_file_lock`."""
    try:
        _release(fh)
    finally:
        fh.close()


@contextmanager
def write_lock() -> Iterator[None]:
    """Serialise writes that share the local stores."""
    with file_lock("write.lock"):
        yield
