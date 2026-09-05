"""Cross-process locks for writes to a local data directory."""

from __future__ import annotations

import errno
import logging
import math
import os
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from time import monotonic, sleep
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


logger = logging.getLogger(__name__)

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


def chroma_lock_wait_timeout() -> float:
    """Seconds to wait for chroma.lock before giving up with a clear error.

    One derivation for every waiter (#140), so a future change cannot move one
    site and leave another behind. It exceeds CHROMA_LOCK_TIMEOUT because a
    peer blocked on chroma.lock while holding write.lock takes that long to
    withdraw; waiting less would mistake a normal hand-off for a stuck peer.

    Factored out as a function so tests can shorten the bound without feeding
    the product an abnormal setting.
    """
    from opencrab.config import get_settings

    return get_settings().chroma_lock_timeout + 60.0


def default_lock_wait_timeout() -> float:
    """Seconds to wait for a named file lock when the caller omits ``timeout``.

    One derivation for every ``file_lock``/``acquire_file_lock`` caller that
    does not choose its own bound (#69) -- write.lock among them, but also
    the per-store ``<name>.db.lock`` files. Before this, an omitted timeout
    meant ``fcntl.flock(LOCK_EX)`` with no bound at all, so one slow or
    crashed holder could stall every other acquirer of that lock forever.

    Validated the same way an explicit timeout is (see ``_resolve_timeout``)
    so a poisoned ``WRITE_LOCK_TIMEOUT`` (NaN, inf, negative) cannot recreate
    the unbounded wait through the *default* path instead of an explicit one
    (#291, found reviewing this same defect class).
    """
    from opencrab.config import get_settings

    value = get_settings().write_lock_timeout
    if not math.isfinite(value) or value < 0:
        raise ValueError(
            f"WRITE_LOCK_TIMEOUT must be a finite, non-negative number of seconds, got {value!r}"
        )
    return value


def _resolve_timeout(timeout: float | None) -> float:
    """Fill in the default wait bound, or validate a caller-supplied one (#69, #291).

    ``None`` no longer means "wait forever" -- it means "use
    ``default_lock_wait_timeout()``". ``file_lock`` and ``acquire_file_lock``
    are the only two places a raw ``timeout`` argument becomes a lock
    acquisition, so resolving it here is the one place that covers every
    caller, including ones that bypass ``write_lock()`` and call
    ``file_lock`` directly.

    NaN and +inf are rejected even when the caller supplies them: both defeat
    the ``remaining <= 0`` deadline check the same way an absent timeout
    does, just as a value instead of an omission (#291). A negative value
    could satisfy that check immediately, but that would hide a caller bug
    behind lock behaviour indistinguishable from "no wait", so it is
    rejected too rather than silently treated as zero.
    """
    if timeout is None:
        return default_lock_wait_timeout()
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(
            f"lock timeout must be a finite, non-negative number of seconds, got {timeout!r}"
        )
    return timeout


def write_lock_busy_message(lock_path: str, timeout: float) -> str:
    """Operator-facing text for a write.lock acquisition that timed out."""
    return (
        f"timed out after {timeout}s waiting for {lock_path}. Another process "
        "holds write.lock while writing to the local stores. Wait for it to "
        "finish, or stop it if it is stuck, then run this again."
    )


def chroma_lock_busy_message(lock_path: str, timeout: float) -> str:
    """Operator-facing text for a chroma.lock wait that timed out.

    Deliberately does NOT name the kind of holder. An exclusive claim is
    blocked both by a server holding the lock SHARED and by another migration
    or loader holding it EXCLUSIVE, and the lock API cannot tell them apart.
    Asserting the first would send an operator to the wrong place in the
    second.
    """
    return (
        f"timed out after {timeout}s waiting for {lock_path}. Another process "
        "holds chroma.lock: either a server or command has the local chroma "
        "store open (shared), or another migration or pack load holds it "
        "exclusively. Stop that process, then run this again."
    )


@contextmanager
def chroma_lock_held(
    data_dir: str, *, shared: bool, timeout: float | None = None
) -> Iterator[None]:
    """Hold chroma.lock for a block, converting an acquisition timeout.

    The conversion covers ONLY the acquisition. A timeout raised by anything
    inside the block -- write.lock, say -- keeps its own message, because
    telling an operator to stop the chroma holder would send them to the wrong
    process.
    """
    if timeout is None:
        timeout = chroma_lock_wait_timeout()
    path = _lock_path("chroma.lock", data_dir)
    # ExitStack rather than __enter__/__exit__ by hand. It does NOT make
    # acquisition and cleanup registration atomic -- enter_context() calls
    # __enter__ and only then pushes the exit callback, so the same
    # interrupt window survives one level down. Nothing in Python closes it.
    # What this buys is that the release is arranged by the stdlib on every
    # reachable path, including BaseException and generator finalisation,
    # instead of by three hand-written branches that must each stay correct.
    with ExitStack() as stack:
        try:
            stack.enter_context(
                file_lock("chroma.lock", data_dir, shared=shared, timeout=timeout)
            )
        except TimeoutError as exc:
            raise TimeoutError(chroma_lock_busy_message(path, timeout)) from exc
        yield


def close_quietly(obj: object, what: str, *, log: object = None, reraise: bool = False) -> None:
    """Close ``obj`` if it has a ``close``, without pinning it in this frame.

    Exists so callers never write ``close = getattr(obj, "close", None)``
    themselves (#140). A bound method keeps its object alive, so that local
    holds the object for as long as its frame does -- and on an exception the
    traceback holds the frame. A resource meant to die before a lock is
    released then outlives it. Here the reference dies with this frame instead,
    and the ``finally`` clears both names even when the exception is re-raised,
    so this helper's own frame never becomes the new anchor.

    ``reraise`` selects the caller's error contract, which is not uniform:
    the migration scripts swallow a failing close and carry on, while
    ``ChromaStore.close`` propagates it (a caller asked for the close and is
    entitled to learn it failed). ``log`` takes a logger; without one the
    message goes to this module's logger.
    """
    if obj is None:
        return
    close = getattr(obj, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - the policy is the caller's
        if reraise:
            raise
        (log or logger).warning("%s close failed: %s", what, exc)
    finally:
        close = None
        obj = None


# --- chroma.lock process-wide ownership on Windows (#140) -------------------
#
# POSIX needs none of this: LOCK_SH is a real shared lock, so several
# ChromaStore instances in one process hold it side by side and each releases
# its own handle. Windows has no reader/writer lock, so `_acquire` emulates a
# shared request as an EXCLUSIVE byte-range lock -- and a second local
# ChromaStore for the same path then waits on the first one in its OWN process
# until it times out. The combined REST + MCP app hits exactly that: REST
# startup builds one vector store, and the first store-using /mcp call lazily
# builds another.
#
# So on Windows the lock is taken ONCE per persist path per process and kept
# until the process exits. No refcount: nothing counts down, so no failure path
# can forget to, which is the trap issue #140 calls strictly worse than the
# defect it would fix.
#
# NOT VERIFIED ON WINDOWS. There is no Windows runner here, so the registry
# algorithm below is tested by injecting the platform decision, and the msvcrt
# behaviour itself is not exercised.
_chroma_registry: dict[str, BinaryIO] = {}
_chroma_registry_guard = threading.Lock()
_chroma_init_locks: dict[str, threading.RLock] = {}


@contextmanager
def chroma_init_guard(lock_path: str, *, windows: bool | None = None) -> Iterator[None]:
    """Serialise chroma client initialisation per path (Windows only).

    Covers acquisition AND the client init that follows, not just acquisition.
    Guarding only the acquisition leaves this interleaving: A registers, A is
    still initialising, B sees the registration and initialises with no lock at
    all, B succeeds, A then fails and retracts -- leaving B's live client
    unprotected. Holding the guard until A has succeeded or failed means B
    always reads a settled result.

    Re-entrant because the failure path releases under the same guard.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        yield
        return
    # Normalise the key the same way the registry does. Without this, two
    # aliases of one directory share a registry entry but get DIFFERENT guards,
    # which reopens the very interleaving this guard exists to close.
    key = os.path.realpath(os.path.abspath(lock_path))
    with _chroma_registry_guard:
        guard = _chroma_init_locks.setdefault(key, threading.RLock())
    with guard:
        yield


def acquire_chroma_lock(
    filename: str,
    data_dir: str,
    *,
    timeout: float | None = None,
    windows: bool | None = None,
) -> tuple[BinaryIO | None, bool]:
    """Acquire the shared chroma lock. Returns ``(handle, owns_registration)``.

    POSIX returns a fresh handle every time and ``False``: ownership is
    per instance and there is no registry.

    Windows registers the first handle for a path and returns ``True`` to that
    caller only. Later callers get ``(None, False)`` -- they neither hold nor
    may retract anything. That ownership flag matters: without it a LATER
    instance whose init fails would tear down the registration the FIRST one is
    still relying on.
    """
    if windows is None:
        windows = os.name == "nt"
    path = _lock_path(filename, data_dir)
    if not windows:
        return acquire_file_lock(filename, data_dir, shared=True, timeout=timeout), False
    with _chroma_registry_guard:
        if path in _chroma_registry:
            return None, False
    handle = acquire_file_lock(filename, data_dir, shared=True, timeout=timeout)
    with _chroma_registry_guard:
        existing = _chroma_registry.get(path)
        if existing is not None:
            release_file_lock(handle)
            return None, False
        _chroma_registry[path] = handle
    return handle, True


def release_chroma_lock(
    handle: BinaryIO | None,
    filename: str,
    data_dir: str,
    *,
    owns_registration: bool = False,
    initialisation_failed: bool = False,
    windows: bool | None = None,
) -> None:
    """Release a handle from :func:`acquire_chroma_lock`.

    POSIX releases every time -- the handle's lifetime is the instance's.

    Windows keeps a SUCCESSFUL registration for the life of the process and
    retracts it only when the owner's initialisation failed. If a plain
    ``close()`` retracted it, then A registering, B skipping and A closing
    first would strip the exclusion out from under a still-live B. The cost is
    that a Windows process holds the lock until it exits, so running the
    offline loader there means stopping the server -- which is that workflow's
    documented procedure anyway.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        if handle is not None:
            release_file_lock(handle)
        return
    if not (owns_registration and initialisation_failed):
        return
    path = _lock_path(filename, data_dir)
    with _chroma_registry_guard:
        registered = _chroma_registry.get(path)
        if registered is not handle:
            return
        del _chroma_registry[path]
    release_file_lock(handle)


def chroma_lock_dir(local_path: str) -> str:
    """Return the directory holding ``chroma.lock`` for a local chroma persist path.

    Every owner of a local chroma ``PersistentClient`` must agree on one lock
    file or the exclusion does not hold -- which is exactly the defect issue
    #140 exists to fix, so the derivation lives here rather than being
    reassembled at each call site.

    The convention it encodes: the factory
    (``opencrab/stores/factory.py:make_vector_store``) and both migration
    scripts build the persist path as ``<local_data_dir>/chroma``, so the
    parent of the persist path IS the data directory that already holds
    ``write.lock``. The offline batch loader takes its exclusive lock on that
    same ``<local_data_dir>/chroma.lock``.

    Only ``abspath`` is applied, deliberately. ``_lock_path`` below already
    resolves the finished lock-file path with ``realpath``, which unifies two
    processes reaching one data directory through different symlink aliases.
    Resolving the persist path itself instead would BREAK the agreement when
    ``<local_data_dir>/chroma`` is a symlink pointing elsewhere: the store
    would lock beside the link target while the migrations lock in
    ``local_data_dir``. Measured both ways; see tests for the pinned cases.
    """
    return os.path.dirname(os.path.abspath(local_path))


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

    ``timeout`` is resolved through ``_resolve_timeout`` before either the
    in-process ``threading.RLock`` or the OS-level flock is touched (#69),
    so an omitted timeout bounds BOTH waits instead of leaving one of them
    unbounded.
    """
    timeout = _resolve_timeout(timeout)
    path = _lock_path(filename, data_dir)
    process_lock = _process_lock(path)
    deadline = monotonic() + max(timeout, 0)
    if not process_lock.acquire(timeout=max(0, deadline - monotonic())):
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
            _acquire(fh, shared=shared, timeout=max(0, deadline - monotonic()))
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
    """Acquire a lock and return its open handle for lifetime-scoped use.

    ``timeout`` is resolved through ``_resolve_timeout`` (#69): an omitted
    timeout now bounds the wait instead of blocking on ``fcntl.flock``
    forever.
    """
    timeout = _resolve_timeout(timeout)
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
    """Serialise writes that share the local stores.

    An omitted ``timeout`` gets ``default_lock_wait_timeout()`` from
    ``file_lock`` (#69) -- it used to mean an unbounded wait. A caller that
    already computed its own bound (the backup module, a migration composing
    it with a chroma.lock wait) passes it explicitly and keeps that value.

    The ``TimeoutError`` -> :func:`write_lock_busy_message` conversion below
    covers ONLY the acquisition, the same restriction ``chroma_lock_held``
    documents for chroma.lock: a ``TimeoutError`` raised by the protected
    block itself must keep its own message, or an unrelated failure would be
    misreported as "another process holds write.lock".
    """
    path = _lock_path("write.lock", data_dir or lock_data_dir())
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock("write.lock", data_dir, timeout=timeout))
        except TimeoutError as exc:
            resolved = timeout if timeout is not None else default_lock_wait_timeout()
            raise TimeoutError(write_lock_busy_message(path, resolved)) from exc
        yield
