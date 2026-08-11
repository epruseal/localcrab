"""Cross-process locks for writes to a local data directory."""

from __future__ import annotations

import errno
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
    path = os.path.realpath(os.path.abspath(os.path.join(data_dir or lock_data_dir(), filename)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


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
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if timeout is None:
            fcntl.flock(fh, operation)
            return

        deadline = monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, operation | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for file lock") from exc
                sleep(min(0.1, remaining))

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
            if deadline is None:
                sleep(0.1)
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for Windows file lock") from exc
            # Same clamp as the POSIX branch: a flat sleep(0.1) would make a
            # timeout smaller than the poll interval overshoot by up to 100ms.
            sleep(min(0.1, remaining))


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
    deadline = None if timeout is None else monotonic() + max(timeout, 0)
    if deadline is None:
        process_lock.acquire()
    elif not process_lock.acquire(timeout=max(0, deadline - monotonic())):
        raise TimeoutError("timed out waiting for in-process file lock")
    try:
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
            return

        # Three failure boundaries, deliberately separate: a failed open
        # must not close a handle that does not exist, a failed acquire
        # must close the handle but NOT unlock a region it never owned,
        # and a successful acquire must always unlock and close. Every one
        # of them still releases ``process_lock`` via the outer finally.
        fh = _open_lock(path)
        try:
            remaining = None if deadline is None else max(0, deadline - monotonic())
            _acquire(fh, shared=shared, timeout=remaining)
        except BaseException:
            fh.close()
            raise
        held[path] = (1, fh)
        try:
            yield
        finally:
            held.pop(path, None)
            try:
                _release(fh)
            finally:
                fh.close()
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
