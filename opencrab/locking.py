"""Cross-process locks for writes to a local data directory."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic, sleep
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_process_locks: dict[str, threading.RLock] = {}
_process_locks_guard = threading.Lock()
_held = threading.local()


def lock_data_dir() -> str:
    """Return and create the data directory used by the shared locks."""
    data_dir = os.environ.get("LOCAL_DATA_DIR")
    if not data_dir:
        from opencrab.config import get_settings

        data_dir = get_settings().local_data_dir
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _lock_path(filename: str, data_dir: str | None) -> str:
    return os.path.abspath(os.path.join(data_dir or lock_data_dir(), filename))


def _process_lock(path: str) -> threading.RLock:
    with _process_locks_guard:
        return _process_locks.setdefault(path, threading.RLock())


def _open_lock(path: str) -> BinaryIO:
    try:
        return open(path, "r+b")
    except FileNotFoundError:
        # O_CREAT without O_TRUNC keeps concurrent first-open calls from
        # clobbering the lock file before either process acquires it.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        return os.fdopen(fd, "r+b")


def _acquire(fh: BinaryIO, *, shared: bool, timeout: float | None) -> None:
    if os.name != "nt":
        fcntl.flock(fh, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        return

    # msvcrt has no reader/writer lock.  A shared request is therefore an
    # exclusive lock on Windows; callers must use a timeout so a second MCP
    # process gets a clear startup error instead of hanging forever.
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    fh.seek(0)
    deadline = None if timeout is None else monotonic() + timeout
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError("timed out waiting for Windows file lock") from exc
            sleep(0.1)


def _release(fh: BinaryIO) -> None:
    if os.name == "nt":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def file_lock(
    filename: str,
    data_dir: str | None = None,
    *,
    shared: bool = False,
    timeout: float | None = None,
) -> Iterator[None]:
    """Hold a cross-process lock until the enclosed operation completes.

    The lock is re-entrant within a thread, so low-level writers and their
    endpoint/script callers can share one ownership boundary safely.
    """
    path = _lock_path(filename, data_dir)
    process_lock = _process_lock(path)
    process_lock.acquire()
    held = getattr(_held, "locks", None)
    if held is None:
        held = _held.locks = {}
    current = held.get(path)
    if current is not None:
        held[path] = (current[0] + 1, current[1])
        try:
            yield
        finally:
            depth, fh = held[path]
            if depth == 1:
                del held[path]
            else:
                held[path] = (depth - 1, fh)
            process_lock.release()
        return

    fh = _open_lock(path)
    acquired = False
    try:
        _acquire(fh, shared=shared, timeout=timeout)
        acquired = True
        held[path] = (1, fh)
        try:
            yield
        finally:
            held.pop(path, None)
            if acquired:
                try:
                    _release(fh)
                finally:
                    fh.close()
            else:
                fh.close()
    except BaseException:
        if not acquired:
            fh.close()
        raise
    finally:
        process_lock.release()


def acquire_file_lock(
    filename: str,
    data_dir: str | None = None,
    *,
    shared: bool = False,
    timeout: float | None = None,
) -> BinaryIO:
    """Acquire a lock and return its open handle for lifetime-scoped use."""
    fh = _open_lock(_lock_path(filename, data_dir))
    try:
        _acquire(fh, shared=shared, timeout=timeout)
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
def write_lock(data_dir: str | None = None, *, timeout: float | None = None) -> Iterator[None]:
    """Serialise writes that share the local stores."""
    with file_lock("write.lock", data_dir, timeout=timeout):
        yield
