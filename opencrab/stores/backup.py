"""Consistent, complete backup of the local store directory.

WHY THIS MODULE EXISTS (issues #128 and #123)
---------------------------------------------
``scripts/migrate_to_local.py#backup_local_data()`` used to copy live SQLite
files with ``shutil.copy2``. That is not a snapshot:

* ``SQLStore`` and friends do not enable WAL (#105), so they use a rollback
  journal. When a writer dies mid-transaction its dirty pages have already
  spilled into the ``.db`` file and a *hot* ``<db>-journal`` is left beside
  it. Copying the ``.db`` alone keeps those uncommitted pages and throws away
  the information needed to roll them back. The copy looks fine and is not.
* ``<db>-shm`` is a transient shared-memory index that must be rebuilt.
  Copying it alongside proved nothing and made the result look safer than it
  was.

``sqlite3.Connection.backup()`` fixes both: it opens the source read-write so
SQLite recovers a hot journal first, reads through the WAL, and produces a
destination that opens standalone with no sidecar files. If another connection
writes mid-copy, SQLite restarts the copy rather than emitting a torn page.

WHAT THIS DOES NOT GUARANTEE
----------------------------
Per-file transactional consistency only. ``.backup()`` says nothing about two
different files agreeing on a point in time. The cross-process ``write.lock``
held while the files are captured narrows that window for every writer that
honours it, but not every writer does -- ``billing.db`` is deliberately
written outside it (#105), and the ownership map has known holes (#141). Say
exactly this and no more, in the output and in the docs.

LIVE BACKUP (#510)
------------------
The service keeps running during a backup. ``write.lock`` is held while the
files are CAPTURED. For WAL-mode SQLite files that is only the time to pin a
snapshot, not the time to copy. Files that cannot be pinned are copied inside
the window, so for them the hold time grows with their size (see below):

1. With ``write.lock`` held, every WAL-mode SQLite source gets a read
   transaction (``BEGIN`` plus one read) that stays open. A WAL reader sees one
   fixed snapshot for as long as the transaction lives, and writers keep
   committing beside it. Files that are not in WAL mode (a rollback journal
   would let a held read transaction block that file's writers) and the
   directory and opaque targets are copied inside this window.
2. The lock is released. Each pinned snapshot is copied with the online backup
   API on the connection that holds the transaction, so the copy is a single
   snapshot that concurrent writers cannot restart. The connection is closed
   as soon as its copy ends. The copy is flushed to disk every
   ``_COPY_FLUSH_BYTES`` while it is written. In a measurement with a store of
   about a gigabyte, flushing once at the end of each file made the service's
   reads and writes pause together for about a second; flushing in steps
   removed the pause. The cause is probably the single large fsync; that was
   not confirmed directly.
3. Every SQLite copy made by this run is verified with ``integrity_check``
   (directory targets are not, except a copied chroma catalog), and
   ``MANIFEST.json`` records sizes, sha256, how each SQLite file was captured
   and how long the lock was held. ``verify_backup_set()`` re-checks a set
   later without writing to it. Its checks are limited: see its docstring.

While a snapshot connection is open the WAL file of that database grows,
because a checkpoint cannot pass the snapshot; it is checkpointed again after
the close. The disk check in ``backup_data_dir`` keeps room for that.

TARGET LIST (#123)
------------------
``vectors.db`` was missing from the backup entirely: the list was hardcoded in
the migration script AND in ``docs/ARCHITECTURE.md``, and both went stale when
``SqliteVecStore`` replaced chroma as the default vector store. Losing it costs
a full re-embed. ``local_data_dir_inventory()`` is now the single canonical
list; the docs point at it instead of repeating it.

The inventory is FIXED, not derived from the active backend. Backend-conditional
targets look tidier and are wrong here: ``migrate_to_local.py`` overwrites
``graph.db``/``doc_store.db``/``chroma/``/``opencrab.db`` regardless of the
configured backend, ``--local-data-dir`` may differ from ``Settings.local_data_dir``,
and ``get_settings()`` is ``lru_cache``d -- filtering on settings would drop
targets that are about to be destroyed. Settings may only ADD a target (a
renamed ``VECTOR_DB_FILE``), never remove one.

PUBLISH PROTOCOL
----------------
Everything is staged in ``<dest>/.backup-staging.{set_id}/``, verified, fsynced,
then published with ONE ``os.rename`` to ``<dest>/backup.{set_id}/``. A set is
therefore either completely there or not there at all, whatever moment a crash
lands on. Per-item publishing cannot offer that: a crash between two renames
leaves a half-set under names that claim success.

The fsync steps are not optional. A directory rename is atomic in the namespace
only; without flushing the data first, a power loss can leave the set present
but its files empty.

ACCESS MODE
-----------
Every artifact in the set carries the access mode of its source: SQLite copies
get the source file's mode (``backup_sqlite``), directory and opaque copies
keep it through ``copy2``/``copytree``, every directory this module creates
inside the set gets the mode of its counterpart under the data directory
(``0700`` when there is none, as for the parked ``external-vector/``), and
the set directory gets the data directory's mode at publication. While the
set is being written, the staging directory, every new directory and each
SQLite copy are readable by the owner only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "MANIFEST_NAME",
    "BackupDurabilityError",
    "BackupEntry",
    "BackupError",
    "BackupOutcome",
    "BackupTarget",
    "backup_data_dir",
    "backup_sqlite",
    "local_data_dir_inventory",
    "verify_backup_set",
    "verify_sqlite",
]

logger = logging.getLogger(__name__)

Kind = Literal["sqlite", "directory", "opaque", "excluded"]
Status = Literal["verified", "unverified", "skipped", "excluded"]

#: Wait this long for the cross-process write.lock, and for a contended
#: SQLite source, before giving up. Overridable so an operator with a very
#: large store can raise it rather than patch the code.
DEFAULT_LOCK_TIMEOUT = 60.0
_TIMEOUT_ENV = "OPENCRAB_BACKUP_LOCK_TIMEOUT"

_SET_PREFIX = "backup."
_STAGING_PREFIX = ".backup-staging."
_EXTERNAL_DIR = "external-vector"

#: The set's own description: file sizes, sha256, how each SQLite file was
#: captured, and how long write.lock was held (#510).
MANIFEST_NAME = "MANIFEST.json"
_MANIFEST_FORMAT = 1

#: Busy wait, in seconds, when opening a source for a snapshot. Spent while
#: write.lock is held, so it is short on purpose.
_SNAPSHOT_OPEN_TIMEOUT = 5.0

#: The copy of a pinned snapshot gets ``max(lock_timeout, bytes / this)``
#: seconds. A pinned copy never waits on SQLITE_BUSY, so the budget is only
#: a ceiling for a stalled disk; the deadline is checked between steps and
#: cannot interrupt a single step.
_MIN_COPY_BYTES_PER_SECOND = 1 << 20

#: A pinned copy is flushed to disk every this many bytes while it is written.
#: Measured: one fsync of a whole large copy at the end paused the service's
#: reads and writes for about a second; stepwise flushing removed the pause.
_COPY_FLUSH_BYTES = 32 << 20

#: Free space that must remain on the destination volume (and on the data
#: volume, while a snapshot connection lets its WAL grow) after the estimated
#: copy. A backup must not fill the disk under a running service.
_FREE_SPACE_RESERVE = 1 << 30

_UNLOCKED_WRITER_LABELS = frozenset({"billing.db"})


class BackupError(RuntimeError):
    """The backup did not produce a set that can be trusted."""


class BackupDurabilityError(BackupError):
    """The set was published but could not be flushed to stable storage.

    Distinct from every other failure because the set IS already visible: the
    rename happened. Withdrawing it would destroy a backup that is probably
    fine, so it stays and the caller is told the durability is unknown.
    """

    def __init__(self, message: str, set_dir: Path) -> None:
        super().__init__(message)
        self.set_dir = set_dir


class _CallbackError(Exception):
    """Carrier for an exception raised by a CALLER-supplied callback.

    ``on_event`` (and the private ``_on_step``) run inside the error boundary
    that turns this module's own failures into ``BackupError``. A callback's
    failure is not a backup failure -- the CLI's ``print`` raising
    ``BrokenPipeError`` after the set was published must not be reported as
    "the backup failed" -- so it is carried past the boundary and re-raised
    unchanged there.
    """

    def __init__(self, exc: Exception) -> None:
        super().__init__(str(exc))
        self.exc = exc


def _guard_callback(fn: Callable[..., None]) -> Callable[..., None]:
    def guarded(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # not BaseException: KeyboardInterrupt stays itself
            raise _CallbackError(exc) from exc

    return guarded


@dataclass(frozen=True)
class BackupTarget:
    """One artifact that can live under the local data directory.

    ``location`` is USUALLY relative to the data directory but may be
    absolute: ``VECTOR_DB_FILE`` accepts an absolute path and
    ``factory.make_vector_store`` passes it straight through, so the type
    cannot promise otherwise.
    """

    label: str
    location: str
    kind: Kind
    reason: str


@dataclass
class BackupEntry:
    label: str
    status: Status
    source: Path | None = None
    destination: Path | None = None
    note: str = ""
    #: ``wal_snapshot`` | ``copied_under_lock`` for SQLite entries, else "".
    capture: str = ""
    journal_mode: str = ""


@dataclass
class BackupOutcome:
    set_dir: Path
    entries: list[BackupEntry] = field(default_factory=list)
    leftover_stagings: list[Path] = field(default_factory=list)
    #: The content of ``MANIFEST.json`` as written into the set (#510).
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def copied(self) -> dict[str, str]:
        """``{source path: destination path}`` for artifacts actually copied."""
        return {
            str(e.source): str(e.destination)
            for e in self.entries
            if e.source is not None and e.destination is not None
        }

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

_FIXED_INVENTORY: tuple[BackupTarget, ...] = (
    BackupTarget("opencrab.db", "opencrab.db", "sqlite", "SQLStore (config.sqlite_url)"),
    BackupTarget("graph.db", "graph.db", "sqlite", "LocalGraphStore"),
    BackupTarget("doc_store.db", "doc_store.db", "sqlite", "LocalSQLDocStore"),
    BackupTarget("billing.db", "billing.db", "sqlite", "billing_events, split out by #105"),
    BackupTarget(
        "vectors.db",
        "vectors.db",
        "sqlite",
        "SqliteVecStore default vector file -- omitted before #123",
    ),
    BackupTarget("chroma", "chroma", "directory", "ChromaStore PersistentClient directory"),
    BackupTarget("docs", "docs", "directory", "LocalDocStore JSON fallback directory"),
    BackupTarget(
        "graph.kuzu",
        "graph.kuzu",
        "opaque",
        "kuzu graph artifact -- not SQLite, so copied byte-wise without a consistency guarantee",
    ),
    BackupTarget(
        "packs",
        "packs",
        "excluded",
        "externally provisioned pack staging content: nothing in this repo writes it and the "
        "migration does not overwrite it. Preserve it separately.",
    ),
)


def local_data_dir_inventory(settings: Any | None = None) -> list[BackupTarget]:
    """Every artifact known to live under LOCAL_DATA_DIR, and what to do with it.

    This is the canonical list. ``docs/ARCHITECTURE.md`` points here instead
    of repeating it -- the duplicated hardcoded lists are what let
    ``vectors.db`` fall out of the backup (#123).

    ``settings`` may only ADD a target. See the module docstring for why
    filtering on the active backend is wrong.
    """
    targets = list(_FIXED_INVENTORY)
    known = {t.location for t in targets}

    if settings is None:
        try:
            from opencrab.config import get_settings

            settings = get_settings()
        except Exception as exc:
            # Fail closed. Falling back to the fixed list here would drop a
            # renamed VECTOR_DB_FILE and still report a complete backup --
            # which is exactly the harm #123 describes. This module already
            # refuses to fall back to a raw copy when the online backup
            # fails, for the same reason: never present an incomplete backup
            # as a finished one.
            raise BackupError(
                f"cannot read the configuration to determine the backup targets ({exc}). "
                "Refusing to continue: the configured vector database filename is unknown, "
                "so a renamed VECTOR_DB_FILE would be silently left out of the backup. "
                "Fix the configuration, or call this API with an explicit settings object."
            ) from exc

    vector_file = getattr(settings, "vector_db_file", None)
    if isinstance(vector_file, str) and vector_file and vector_file not in known:
        targets.append(
            BackupTarget(
                vector_file,
                vector_file,
                "sqlite",
                "VECTOR_DB_FILE names a vector store file other than the default",
            )
        )
    return targets


# ---------------------------------------------------------------------------
# SQLite copy and verification
# ---------------------------------------------------------------------------


def _rw_uri(path: Path) -> str:
    """A ``file:`` URI opening an EXISTING database read-write.

    ``mode=rw`` rather than a bare path: ``sqlite3.connect(path)`` CREATES an
    empty database when the file is gone, which would report an empty backup
    as a success. Read-write rather than ``mode=ro``: recovering a hot
    rollback journal requires write access, and ``mode=ro`` fails with
    SQLITE_READONLY_ROLLBACK in exactly the situation a backup matters most.

    Built with ``as_uri()``, not string concatenation -- a path containing
    ``?``, ``#`` or ``%`` would otherwise resolve to a different file or fail.
    """
    return path.resolve().as_uri() + "?mode=rw"


def backup_sqlite(
    src: Path,
    dst: Path,
    *,
    deadline: float,
    pages: int = 64,
    src_conn: sqlite3.Connection | None = None,
    flush_every: int = 0,
    _on_step: Callable[[int, int, int], None] | None = None,
) -> None:
    """Copy ``src`` to ``dst`` with SQLite's online backup API.

    ``src_conn`` (#510) is an already-open source connection that the CALLER
    owns and keeps open. It is used as is and never closed here. When it holds
    an open read transaction on a WAL database, the copy reads that one fixed
    snapshot from the first page to the last, so writers that keep committing
    cannot restart it. ``src`` is then used for messages only.

    ``deadline`` is an absolute ``time.monotonic()`` value. It is enforced
    from the progress callback, which is the only place it CAN be enforced:
    CPython's ``Connection.backup()`` retries ``SQLITE_BUSY``/``SQLITE_LOCKED``
    forever, and the connection's own busy timeout does not bound that outer
    loop. CPython does call ``progress`` on every iteration including the busy
    ones and propagates an exception raised there, so a deadline check in the
    callback is what actually stops an unbounded wait.

    ``sleep=0`` is passed and the waiting is done inside the callback instead:
    the built-in sleep happens AFTER the callback returns, so leaving it at
    the default would overshoot the deadline by up to one sleep interval. The
    source connection is opened with ``timeout=0`` for the same reason -- see
    there. A single ``backup_step()`` or filesystem write still cannot be
    interrupted, so the real bound is "deadline plus one step", and both
    settings exist to keep that step short.

    ``_on_step`` is a private test observer, not API. The deadline is checked
    before it runs so a test cannot mask a timeout.

    Access mode: the destination is created exclusively as ``0600`` BEFORE
    SQLite opens it, and receives the source file's mode once the copy is
    complete. Left to ``sqlite3.connect()`` a new file gets SQLite's default
    ``0644`` minus umask, so a ``0600`` source came out world-readable in a
    set written to a shared directory -- a regression from the ``copy2`` path
    this replaced, which preserved the mode. During the copy the file (and
    the transient ``-journal`` SQLite derives from it) is readable by the
    owner only.
    """
    if not math.isfinite(deadline):
        # A NaN or infinite deadline can never expire; refuse it before the
        # destination exists rather than wait forever on a busy source.
        raise ValueError(f"deadline must be finite, got {deadline!r}")
    on_step = _guard_callback(_on_step) if _on_step is not None else None
    # ONE error boundary for the whole copy. Everything below -- the
    # exclusive create, opening the source (which fails with
    # OperationalError when the path is a directory or not writable), the
    # copy itself and the final copymode -- reports as BackupError. The
    # deadline (TimeoutError) and the observer's own exception pass through.
    try:
        os.close(os.open(str(dst), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        # timeout=0 so a contended source returns SQLITE_BUSY to us
        # IMMEDIATELY. The default 5s busy timeout would be spent inside a
        # single sqlite3_backup_step(), and the progress callback -- the only
        # place the deadline can be enforced -- runs between steps, so a 0.2s
        # deadline measured 5.06s. With timeout=0 the same case measures
        # 0.20s. The waiting is done by the callback below, which is
        # deadline-aware.
        owns_src = src_conn is None
        if src_conn is None:
            src_conn = sqlite3.connect(_rw_uri(src), uri=True, timeout=0)
        try:
            dst_conn = sqlite3.connect(str(dst))
            flush_fd = -1
            try:
                flush_pages = 0
                if flush_every > 0:
                    # Paced flush: SQLite would otherwise fsync the whole copy in
                    # one commit at the end. Measured on a large copy, that
                    # paused the service's own commits for about a second (the
                    # single fsync is the likely cause). The caller owns the
                    # final fsync of the finished file.
                    dst_conn.execute("PRAGMA synchronous=OFF")
                    page_size = int(src_conn.execute("PRAGMA page_size").fetchone()[0])
                    flush_pages = max(1, flush_every // page_size)
                    flush_fd = os.open(str(dst), os.O_RDONLY)
                flushed_at = 0

                def progress(status: int, remaining: int, total: int) -> None:
                    nonlocal flushed_at
                    remaining_budget = deadline - time.monotonic()
                    if remaining_budget <= 0:
                        raise TimeoutError(
                            f"backup of {src} exceeded its deadline with {remaining} page(s) left"
                        )
                    if on_step is not None:
                        on_step(status, remaining, total)
                    if flush_pages and total - remaining - flushed_at >= flush_pages:
                        flushed_at = total - remaining
                        os.fsync(flush_fd)
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        # Compute the remaining budget ONCE and only sleep on
                        # a positive value: recomputing here could go negative
                        # between the two reads and make sleep() raise
                        # ValueError.
                        time.sleep(min(0.05, remaining_budget))

                src_conn.backup(dst_conn, pages=pages, progress=progress, sleep=0)
            finally:
                if flush_fd >= 0:
                    os.close(flush_fd)
                dst_conn.close()
        finally:
            if owns_src:
                src_conn.close()
        # Same access as the source, no wider and no narrower: an operator
        # whose backups are read by another account configured that on the
        # source.
        shutil.copymode(src, dst)
    except _CallbackError as cf:
        raise cf.exc
    except TimeoutError:
        raise
    except FileExistsError as exc:
        raise BackupError(
            f"backup destination already exists, refusing to overwrite: {dst}"
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        # No copy2 fallback. This also covers corruption, locking and I/O
        # errors, and a raw byte copy reported as a backup is precisely the
        # "believed to have a backup" harm #128 is about.
        raise BackupError(
            f"{src} could not be copied with SQLite's online backup API ({exc}). "
            "It is not a usable SQLite database; refusing to report a raw copy as a backup."
        ) from exc


def verify_sqlite(path: Path) -> None:
    """Reopen a backup and require ``PRAGMA integrity_check`` to say ``ok``.

    "Backed up" is not a claim worth making until the copy has been opened.

    Opened through a ``mode=rw`` URI for the same reason the source is (see
    ``_rw_uri``): ``sqlite3.connect(path)`` CREATES an empty database when the
    file is absent, and an empty database passes ``integrity_check``. That
    would let a missing backup verify successfully, which is the exact shape
    of failure this whole module exists to prevent.
    """
    if not path.is_file():
        raise BackupError(f"backup {path} does not exist, so it cannot be verified")
    try:
        conn = sqlite3.connect(_rw_uri(path), uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        # sqlite3.Error, not DatabaseError: InterfaceError is outside it, and
        # _rw_uri's resolve() can raise OSError. "Verified" must never be
        # answered with a foreign exception type.
        raise BackupError(f"backup {path} does not open as a SQLite database: {exc}") from exc
    if not result or result[0] != "ok":
        raise BackupError(f"backup {path} failed integrity_check: {result}")


# ---------------------------------------------------------------------------
# Snapshot pins (#510)
# ---------------------------------------------------------------------------


@dataclass
class _Snapshot:
    """A source connection holding an open read transaction on a WAL database."""

    connection: sqlite3.Connection
    journal_mode: str
    copy_bytes: int


@dataclass
class _Job:
    """One SQLite target whose copy and verification run after write.lock is released."""

    entry: BackupEntry
    source: Path
    destination: Path
    snapshot: _Snapshot | None  # None: already copied under the lock, only verification is left


class _SnapshotPins:
    """Owner of every snapshot connection this run opened.

    A connection joins the list the moment it exists, before any statement
    runs on it, and leaves it only after ``close()`` succeeded. A connection
    whose close failed therefore stays owned and is tried again by
    ``close_all``. A held read transaction keeps a WAL database from being
    checkpointed past the snapshot, so an unclosed connection is a real cost
    to the running service, not a cosmetic leak.
    """

    def __init__(self) -> None:
        self._open: list[sqlite3.Connection] = []

    def open_snapshot(self, source: Path) -> tuple[str, _Snapshot | None]:
        """Open ``source``; pin a read snapshot when it is a WAL database.

        Returns ``(journal_mode, snapshot)``. ``snapshot`` is None when the
        database is not in WAL mode: a held read transaction on a rollback
        journal database would block its writers after write.lock is gone, so
        such a file must be copied while the lock is still held.
        """
        try:
            conn = sqlite3.connect(
                _rw_uri(source),
                uri=True,
                timeout=_SNAPSHOT_OPEN_TIMEOUT,
                isolation_level=None,
            )
        except (sqlite3.Error, OSError) as exc:
            raise self._unusable(source, exc) from exc
        self._open.append(conn)
        try:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                self.close_checked(conn)
                return mode, None
            conn.execute("BEGIN")
            # Any read statement starts the read transaction; BEGIN alone is lazy.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        except (sqlite3.Error, OSError) as exc:
            raise self._unusable(source, exc) from exc
        return mode, _Snapshot(conn, mode, page_count * page_size)

    @staticmethod
    def _unusable(source: Path, exc: BaseException) -> BackupError:
        # Same wording contract as backup_sqlite: no raw-copy fallback.
        return BackupError(
            f"{source} could not be opened for a snapshot ({exc}). "
            "It is not a usable SQLite database; refusing to report a raw copy as a backup."
        )

    def release(self, conn: sqlite3.Connection) -> bool:
        """End the transaction and close ``conn``. True when the close succeeded.

        A failing rollback does not skip the close, and the connection leaves
        the owner list only after the close succeeded.
        """
        try:
            # A connection that never began a transaction (a non-WAL source
            # closed right after its journal-mode probe) has nothing to roll back.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except Exception as exc:  # noqa: BLE001 - the close below ends the transaction anyway
            logger.warning("snapshot rollback failed, closing anyway: %s", exc)
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001 - reported through the return value
            logger.warning("snapshot connection close failed: %s", exc)
            return False
        if conn in self._open:
            self._open.remove(conn)
        return True

    def close_checked(self, conn: sqlite3.Connection) -> None:
        """``release`` with one retry; a connection that stays open aborts the run."""
        if self.release(conn) or self.release(conn):
            return
        raise BackupError(
            "a snapshot connection could not be closed; refusing to publish a backup "
            "while it still holds a read transaction on the live database"
        )

    def close_all(self) -> int:
        """Try once more to close everything still owned. Never raises.

        Returns the number of connections that are still open. The caller is
        already handling an exception and this must not replace it.
        """
        for conn in list(self._open):
            self.release(conn)
        if self._open:
            logger.error("%d snapshot connection(s) could not be closed", len(self._open))
        return len(self._open)


def _tree_bytes(path: Path) -> int:
    """Sum of regular file sizes under ``path``; links are not followed."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            f = Path(dirpath) / name
            if not f.is_symlink():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    return total


def _estimate_bytes(targets: Iterable[BackupTarget], data_dir: Path) -> int:
    """Rough bytes the set will take: main file plus ``-wal`` for SQLite, tree size otherwise.

    An estimate only. The copy budget of a pinned snapshot uses the page
    count of the snapshot itself.
    """
    total = 0
    for target in targets:
        if target.kind == "excluded":
            continue
        source = _source_for(target, data_dir)
        if not source.exists():
            continue
        if target.kind == "sqlite":
            total += source.resolve().stat().st_size
            wal = Path(str(source) + "-wal")
            if wal.is_file():
                total += wal.stat().st_size
        else:
            total += _tree_bytes(source)
    return total


def _require_free_space(dest: Path, data_dir: Path, needed: int) -> None:
    """Refuse before writing anything when the volumes cannot take the copy."""
    reserve = _FREE_SPACE_RESERVE
    dest_free = shutil.disk_usage(dest).free
    if dest_free < needed + reserve:
        raise BackupError(
            f"not enough free space on the destination volume: the backup needs about "
            f"{needed} bytes plus a {reserve} byte reserve, {dest_free} bytes are free"
        )
    if os.stat(dest).st_dev != os.stat(data_dir).st_dev:
        # While a snapshot connection is open the WAL files grow on the data volume.
        data_free = shutil.disk_usage(data_dir).free
        if data_free < reserve:
            raise BackupError(
                f"not enough free space on the data volume: {data_free} bytes are free, "
                f"{reserve} bytes are kept in reserve for WAL growth during the backup"
            )


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_files(root: Path) -> dict[str, dict[str, Any]]:
    """Every file under ``root`` except the manifest. Links are recorded, not followed."""
    files: dict[str, dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for d in list(dirnames):
            if (here / d).is_symlink():
                files[(here / d).relative_to(root).as_posix()] = {"symlink": True}
                dirnames.remove(d)
        for name in filenames:
            f = here / name
            rel = f.relative_to(root).as_posix()
            if rel == MANIFEST_NAME:
                continue
            if f.is_symlink():
                files[rel] = {"symlink": True}
            else:
                files[rel] = {"size": f.stat().st_size, "sha256": _hash_file(f)}
    return dict(sorted(files.items()))


def _manifest_label(label: str) -> str:
    """A label safe to publish: an absolute VECTOR_DB_FILE shrinks to its file name."""
    return Path(label).name if os.path.isabs(label) else label


def _write_manifest(
    staging: Path,
    outcome: BackupOutcome,
    *,
    set_id: str,
    lock_info: dict[str, Any],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for e in outcome.entries:
        item: dict[str, Any] = {"label": _manifest_label(e.label), "status": e.status}
        if e.destination is not None:
            item["path"] = e.destination.relative_to(staging).as_posix()
        if e.capture:
            item["capture"] = e.capture
            item["journal_mode"] = e.journal_mode
            item["mcp_write_tools_hold_write_lock"] = e.label not in _UNLOCKED_WRITER_LABELS
        entries.append(item)
    manifest: dict[str, Any] = {
        "format": _MANIFEST_FORMAT,
        "set_id": set_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "lock": lock_info,
        "entries": entries,
        "files": _manifest_files(staging),
    }
    fd = os.open(str(staging / MANIFEST_NAME), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return manifest


def _readonly_integrity(path: Path) -> None:
    """``integrity_check`` through an immutable read-only open: the file is not touched."""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"{path.name} does not open as a SQLite database: {exc}") from exc
    if not result or result[0] != "ok":
        raise BackupError(f"{path.name} failed integrity_check: {result}")


def verify_backup_set(set_dir: str | Path) -> dict[str, Any]:
    """Check a published set against its ``MANIFEST.json``. Writes nothing.

    Checks that every listed file exists, that links are still links, that
    sizes and sha256 match, and that every SQLite entry that declares a
    ``capture`` passes ``integrity_check`` (opened immutable and read-only). A
    file that is in the set but not in the manifest is reported as ``unlisted``
    and is not a failure: other tools add their own state beside a set. Raises
    ``BackupError`` naming every mismatch.

    Limits (the stronger structural checks are tracked in issue #522): the
    manifest is trusted for its own structure. An entry whose ``capture`` is
    missing is not integrity-checked, an entry's ``path`` is not required to
    appear in ``files``, a set directory replaced by a link is not detected,
    and a file removed from both the set and ``files`` goes unnoticed. The
    check finds damage and loss of the files the manifest lists; it is not
    meant to resist someone who edits the manifest.
    """
    root = Path(set_dir)
    if root.is_symlink() or not root.is_dir():
        raise BackupError(f"backup set directory does not exist: {root}")
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackupError(f"{MANIFEST_NAME} is missing from {root.name}") from exc
    except (OSError, ValueError) as exc:
        raise BackupError(f"{MANIFEST_NAME} in {root.name} cannot be read: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != _MANIFEST_FORMAT:
        raise BackupError(
            f"{MANIFEST_NAME} in {root.name} has an unsupported format: "
            f"{manifest.get('format') if isinstance(manifest, dict) else type(manifest).__name__}"
        )
    files = manifest.get("files")
    entries = manifest.get("entries")
    if not isinstance(files, dict) or not isinstance(entries, list):
        raise BackupError(f"{MANIFEST_NAME} in {root.name} is malformed")

    problems: list[str] = []
    intact: set[str] = set()
    for rel, meta in files.items():
        parts = Path(rel).parts
        if os.path.isabs(rel) or ".." in parts or not isinstance(meta, dict):
            problems.append(f"{rel}: unsafe or malformed manifest entry")
            continue
        path = root / rel
        if meta.get("symlink"):
            if not path.is_symlink():
                problems.append(f"{rel}: expected a symbolic link")
            continue
        if path.is_symlink() or not path.is_file():
            problems.append(f"{rel}: missing or not a regular file")
            continue
        if path.stat().st_size != meta.get("size"):
            problems.append(f"{rel}: size differs from the manifest")
            continue
        if _hash_file(path) != meta.get("sha256"):
            problems.append(f"{rel}: sha256 differs from the manifest")
            continue
        intact.add(rel)

    checked: list[str] = []
    for e in entries:
        if not isinstance(e, dict) or "capture" not in e or "path" not in e:
            continue
        rel = e["path"]
        if rel not in intact:
            continue  # already reported above
        try:
            _readonly_integrity(root / rel)
        except BackupError as exc:
            problems.append(str(exc))
        else:
            checked.append(rel)

    unlisted = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if not p.is_dir()
        and p.relative_to(root).as_posix() not in files
        and p.name != MANIFEST_NAME
    )
    if problems:
        raise BackupError(f"backup set {root.name} failed verification: " + "; ".join(problems))
    return {
        "set": root.name,
        "files_verified": len(intact),
        "sqlite_integrity_checked": checked,
        "unlisted": unlisted,
    }


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def _fsync_path(path: Path) -> None:
    """Flush one file or directory to stable storage.

    Directories are opened with ``O_RDONLY``; that is how a rename is made
    durable on POSIX. Never called on a symlink -- following one would reach
    outside the backup set.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _directory_fsync_supported(directory: Path) -> bool:
    """Whether this FILESYSTEM lets us fsync a directory handle.

    Checked BEFORE anything is written. Skipping the flush and returning
    success anyway is not an option: the caller would believe it holds a
    durable backup when it does not.

    Deliberately calls os.fsync directly rather than going through
    ``_fsync_path``: this probes the platform, and routing it through the
    same helper the real flush uses would let a stubbed helper turn a genuine
    flush failure into a pre-flight refusal, hiding which of the two broke.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def _fsync_tree(root: Path) -> None:
    """fsync every regular file, then every directory, bottom-up.

    Symlinks are skipped rather than followed: the set preserves links as
    links, and opening a link's target would flush something outside the set.
    """
    dirs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirs.append(here)
        for name in filenames:
            f = here / name
            if f.is_symlink():
                continue
            _fsync_path(f)
        # Keep os.walk from descending into symlinked directories.
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        _fsync_path(d)


def _publish(staging: Path, final: Path) -> Path:
    """Make the whole set visible with one atomic rename."""
    os.rename(str(staging), str(final))
    return final


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def _new_set_id() -> str:
    """A set identifier that will not collide.

    The timestamp alone collides between back-to-back runs and with a crashed
    run's leftovers, and the gap between "the destination does not exist" and
    ``os.rename`` is a race POSIX rename does not close for an empty target
    directory. The random suffix closes it in practice.
    """
    return f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.{secrets.token_hex(3)}"


def _require_contained(path: Path, root: Path) -> None:
    """Refuse a path that is a symlink or resolves outside ``root``.

    Keeps the #212 containment invariant: a dangling symlink makes
    ``exists()`` false while ``sqlite3.connect()`` still follows it.

    Resolving also catches an escaping ANCESTOR directory, but ONLY when
    ``root`` is supplied independently of ``path``. Deriving the root from
    the path being checked -- ``_require_contained(p, p.parent)`` -- makes
    this function a tautology, because an ancestor symlink moves both sides
    equally; it would then catch nothing but a symlinked leaf. Always pass a
    root that is fixed by the caller, as ``_run_targets`` does with the
    staging directory.
    """
    if path.is_symlink():
        raise BackupError(f"backup path is a symlink, refusing to use it: {path}")
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not (resolved == root_resolved or resolved.is_relative_to(root_resolved)):
        raise BackupError(
            f"backup path escapes its destination: {path} -> {resolved} is outside {root}"
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_timeout(explicit: float | None) -> float:
    """The lock and per-file budget in seconds: finite and not negative.

    Checked here, at the input boundary, because nothing downstream can
    cope with anything else: a NaN deadline makes ``remaining_budget <= 0``
    false forever, so a busy source would be retried without end -- the
    exact contention this budget exists to bound -- and ``inf`` does the
    same. An unparsable value is refused rather than replaced by the
    default: silently running with a budget the operator did not set is
    the same class of failure as silently dropping a configured target.

    The same number is also the per-file copy budget in ``_run_targets``,
    so ``0`` is accepted ("do not wait for the lock") and then every copy
    expires before its first page with ``TimeoutError``. That is the
    documented deadline behaviour, not a boundary rejection.
    """
    if explicit is not None:
        origin, candidate = "lock_timeout", explicit
    else:
        raw = os.environ.get(_TIMEOUT_ENV)
        if not raw:
            return DEFAULT_LOCK_TIMEOUT
        origin, candidate = _TIMEOUT_ENV, raw
    try:
        value = float(candidate)
    except (TypeError, ValueError) as exc:
        raise BackupError(
            f"{origin} must be a number of seconds, got {candidate!r}"
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise BackupError(
            f"{origin} must be a finite, non-negative number of seconds, got {candidate!r}"
        )
    return value


def _source_for(target: BackupTarget, data_dir: Path) -> Path:
    location = Path(target.location)
    if location.is_absolute():
        return location
    return data_dir / location


def _destination_for(target: BackupTarget, source: Path, data_dir: Path, staging: Path) -> Path:
    """Where ``source`` lands inside the staging set.

    Keyed on the target's CONFIGURED location, not on where the source
    resolves. A target whose location is an ordinary relative name has a
    natural slot in the set and keeps it even when the data directory holds a
    symlink there: an operator who moved ``docs/`` to another volume still
    wants it restored to ``docs/``, and parking it elsewhere would quietly
    change where a restore puts it.

    Only a location with no natural slot -- an absolute ``VECTOR_DB_FILE``, or
    one reached through ``..`` -- is parked in a subdirectory. It is parked
    rather than renamed with a prefix because a prefix can push an
    already-long basename past NAME_MAX, and a bare basename could collide
    with a core file's destination.
    """
    location = Path(target.location)
    if location.is_absolute() or ".." in location.parts:
        return staging / _EXTERNAL_DIR / source.name
    return staging / location


def _make_parents(parent: Path, staging: Path, data_dir: Path, target: BackupTarget) -> None:
    """Create the missing directories between ``staging`` and ``parent``.

    Each one is created ``0700`` and then given the mode of the directory it
    corresponds to under the data directory, so a ``0700`` parent guarding a
    ``0644`` database stays ``0700`` in the set. ``Path.mkdir(parents=True)``
    used the process umask instead and turned that parent into ``0755``.
    The parked ``external-vector/`` directory has no source counterpart and
    stays ``0700``. Directories that already exist -- another target's
    ``copytree`` output, whose modes ``copystat`` already preserved -- are
    left alone.
    """
    location = Path(target.location)
    has_source_tree = not (location.is_absolute() or ".." in location.parts)
    relative = parent.relative_to(staging)
    current = staging
    for part in relative.parts:
        current = current / part
        if current.is_dir():
            continue
        os.mkdir(current, 0o700)
        if has_source_tree:
            counterpart = data_dir / current.relative_to(staging)
            if counterpart.is_dir():
                shutil.copymode(counterpart, current)


def _leftover_stagings(dest_dir: Path) -> list[Path]:
    if not dest_dir.is_dir():
        return []
    return sorted(p for p in dest_dir.iterdir() if p.name.startswith(_STAGING_PREFIX))


def backup_data_dir(
    data_dir: str | Path,
    dest_dir: str | Path | None = None,
    *,
    settings: Any | None = None,
    lock_timeout: float | None = None,
    on_event: Callable[[str], None] | None = None,
    exclude: Iterable[str] = (),
) -> BackupOutcome:
    """Back up every present store artifact under ``data_dir`` as one set.

    This function owns the cross-process ``write.lock`` so that no entry
    point -- the migration script, the CLI, a direct caller -- can bypass it.
    The lock is re-entrant within a thread, so an outer holder is fine.

    Two phases (#510). While ``write.lock`` is held the function captures:
    directory and opaque targets and SQLite files that are not in WAL mode are
    copied, and every WAL SQLite file gets a pinned read snapshot. The lock is
    then released and the pinned snapshots are copied, verified and described
    in ``MANIFEST.json``. The writers that honour ``write.lock`` are blocked
    only for the first phase.

    ``exclude`` lists inventory labels to skip (recorded as ``excluded``). An
    unknown label is refused before anything is written.

    Per-file transactional consistency holds for every SQLite target. Alignment
    across files depends on every writer honouring ``write.lock``; see the
    module docstring.

    Trust boundary: ``dest_dir`` is the location the CALLER chose and is not
    containment-checked (a symlinked ancestor is a normal backup-volume
    layout). Everything written INSIDE the set is contained against the
    staging directory this run exclusively created.

    One artifact can outlive a failure: if the publishing ``os.rename``
    itself fails, the fsynced staging directory stays. That is deliberate --
    its ``.backup-staging.`` name cannot be mistaken for a finished backup,
    its contents are intact and worth diagnosing, and the next run reports it
    rather than deleting someone else's data.
    """
    from opencrab.locking import write_lock

    data_dir = Path(data_dir)
    # The implicit destination is the data directory's REAL path: a data
    # root that is itself a symlink is a normal layout, and the symlink
    # refusal below is for a destination the operator pointed at explicitly.
    # Refusing the implicit one blocked every backup on such a layout,
    # including the migration script, which never passes a destination.
    dest = Path(dest_dir) if dest_dir is not None else data_dir.resolve()
    say = on_event or (lambda _msg: None)
    timeout = _resolve_timeout(lock_timeout)

    say = _guard_callback(say)
    # Bound BEFORE the boundary: the handler describes the failure in terms
    # of these, and a failure can happen before the set is even named.
    final: Path | None = None
    published: Path | None = None
    # ONE error boundary from here to the end. This module's own failures
    # (sqlite3, os, shutil, pathlib) become BackupError; BackupError and
    # TimeoutError pass through unchanged; a caller-supplied callback's
    # failure is re-raised as itself. Preconditions, not converted: the
    # caller's path and settings objects behave like plain ones (__fspath__,
    # attribute access), and the package is installed intact.
    try:
        if not data_dir.is_dir():
            raise BackupError(f"data directory does not exist: {data_dir}")

        targets = local_data_dir_inventory(settings)
        wanted = set(exclude)
        unknown = wanted - {t.label for t in targets}
        if unknown:
            raise BackupError(
                f"unknown --exclude label(s) {sorted(unknown)}; "
                f"valid labels: {sorted(t.label for t in targets)}"
            )
        targets = [
            replace(t, kind="excluded", reason="excluded by the operator (--exclude)")
            if t.label in wanted
            else t
            for t in targets
        ]

        # A destination inside a directory-kind source would put staging in a tree
        # this run is about to copy, and copytree would recurse into its own output.
        dest_resolved = dest.resolve() if dest.exists() else dest.parent.resolve() / dest.name
        for t in targets:
            if t.kind not in ("directory", "opaque"):
                continue
            src = _source_for(t, data_dir)
            if not src.is_dir():
                continue
            src_resolved = src.resolve()
            if dest_resolved == src_resolved or dest_resolved.is_relative_to(src_resolved):
                raise BackupError(
                    f"destination {dest} is inside the directory source {src}; "
                    "the backup would copy its own staging directory"
                )

        if dest.is_symlink():
            raise BackupError(f"destination is a symlink, refusing to use it: {dest}")
        if not dest.is_dir():
            raise BackupError(f"destination directory does not exist: {dest}")
        # The destination is NOT containment-checked beyond the symlink refusal
        # above. It is the location the operator chose, and reaching it through a
        # symlinked ancestor (a backup volume mounted that way) is a normal
        # layout, not an attack. Containment is enforced where it means
        # something: INSIDE the set, against the staging root this run created.

        if not _directory_fsync_supported(dest):
            raise BackupError(
                f"cannot fsync a directory under {dest} on this filesystem, so a published "
                "backup set could not be made durable. Refusing to write a backup this "
                "module cannot stand behind."
            )

        _require_free_space(dest, data_dir, _estimate_bytes(targets, data_dir))

        leftovers = _leftover_stagings(dest)
        for p in leftovers:
            # Another run's interrupted staging. Reported, never deleted: it is
            # not this run's to remove, and destroying someone else's partial
            # backup is worse than leaving a directory behind.
            say(f"! leftover staging from an interrupted backup, check and remove manually: {p}")

        set_id = _new_set_id()
        staging = dest / f"{_STAGING_PREFIX}{set_id}"
        final = dest / f"{_SET_PREFIX}{set_id}"

        if final.exists() or final.is_symlink():
            raise BackupError(f"backup set already exists, refusing to overwrite: {final}")

        pins = _SnapshotPins()
        jobs: list[_Job] = []
        staging_owned = False
        wait_started = time.monotonic()
        try:
            with write_lock(str(data_dir), timeout=timeout):
                acquired = time.monotonic()
                acquired_at = datetime.now(UTC)
                # Exclusive create. Not makedirs(exist_ok=True): a pre-planted
                # directory or symlink here would be written through, and every
                # temporary artifact below relies on this directory being ours.
                # Owner-only while the set is being written; it takes the data
                # directory's own mode right before publication, so the set is
                # exactly as reachable as the data it snapshots.
                os.mkdir(staging, 0o700)
                staging_owned = True
                outcome = _run_targets(
                    targets, data_dir, staging, final, say, timeout, pins=pins, jobs=jobs
                )
            released = time.monotonic()
            released_at = datetime.now(UTC)
            lock_info = {
                "wait_seconds": round(acquired - wait_started, 3),
                "held_seconds": round(released - acquired, 3),
                "acquired_at": acquired_at.isoformat(timespec="milliseconds"),
                "released_at": released_at.isoformat(timespec="milliseconds"),
            }
            _finish_jobs(jobs, pins, staging, say, timeout)
            outcome.manifest = _write_manifest(
                staging, outcome, set_id=set_id, lock_info=lock_info
            )
            shutil.copymode(data_dir, staging)
            _fsync_tree(staging)
        except BaseException:
            # One cleanup scope for the lock block AND the unlocked phase, so a
            # failure anywhere -- including the lock release itself -- closes
            # every snapshot connection and removes only a staging directory
            # this run created. _publish below stays outside it: a failed
            # rename keeps the intact staging for diagnosis.
            pins.close_all()
            if staging_owned:
                shutil.rmtree(staging, ignore_errors=True)
            raise

        _publish(staging, final)
        published = final

        # Rewrite the recorded destinations from staging to the published set.
        for entry in outcome.entries:
            if entry.destination is not None:
                entry.destination = final / entry.destination.relative_to(staging)
        outcome.set_dir = final
        outcome.leftover_stagings = leftovers

        try:
            _fsync_path(dest)
        except OSError as exc:
            # The rename already happened, so the set IS visible. Removing it
            # would throw away a backup that is very probably intact; the honest
            # outcome is "published, durability unknown" and a failing exit.
            raise BackupDurabilityError(
                f"backup set {final} was published but the destination directory could not be "
                f"flushed ({exc}); its durability across a power loss is unknown",
                final,
            ) from exc

        counts = outcome.counts()
        say(
            "backup set "
            f"{final.name}: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        say(
            f"write.lock: waited {lock_info['wait_seconds']}s, held {lock_info['held_seconds']}s. "
            "SQLite targets were captured inside that window; each file is transactionally "
            "consistent. Alignment across files covers only writers that honour write.lock "
            "(billing.db and some scripts do not, see issue #141)."
        )
    except _CallbackError as cf:
        raise cf.exc
    except (BackupError, TimeoutError):
        raise
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(_describe_failure(data_dir, final, published, exc)) from exc
    return outcome


def _describe_failure(
    data_dir: Path, final: Path | None, published: Path | None, exc: BaseException
) -> str:
    """Say what is known about the set after a failure, by looking, not assuming.

    A failed ``os.rename`` does not prove nothing was published: on a network
    filesystem the server can apply the rename and the client still receive
    an error. So the set directory is probed here instead of trusting the
    ``published`` flag alone.
    """
    if published is not None:
        return f"backup set {published} was published, but a later step failed ({exc})"
    if final is None:
        # Failed before the set was even named: nothing can exist.
        return f"backup of {data_dir} failed and no set was published ({exc})"
    state = _set_state(final)
    if state == "unknown":
        return (
            f"backup of {data_dir} failed ({exc}); whether a set was published "
            "could not be determined"
        )
    if state == "present":
        return (
            f"the publishing rename of {final} reported an error ({exc}) but the set "
            "directory exists; its state is unknown, treat it as unverified"
        )
    return f"backup of {data_dir} failed and no set was published ({exc})"


def _set_state(final: Path) -> Literal["present", "absent", "unknown"]:
    """Whether the set directory exists, without hiding a failed lookup.

    A True/False lookup such as ``Path.is_dir()`` folds "missing" and,
    depending on the Python version, some lookup errors into False, and
    could turn a permission problem on the destination into "no set was
    published". ``stat`` makes the three cases explicit: only
    FileNotFoundError means absent, any other OSError means unknown.
    """
    try:
        final.stat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _run_targets(
    targets: Iterable[BackupTarget],
    data_dir: Path,
    staging: Path,
    final: Path,
    say: Callable[[str], None],
    timeout: float,
    *,
    pins: _SnapshotPins,
    jobs: list[_Job],
) -> BackupOutcome:
    """Capture every target while write.lock is held (#510).

    Directory and opaque targets, and SQLite files that are not in WAL mode,
    are copied here. A WAL SQLite file is only pinned (a read snapshot is
    opened) and queued in ``jobs``; its bytes are copied after the lock is
    released. Every SQLite copy is verified later, from ``jobs``.
    """
    outcome = BackupOutcome(set_dir=final)
    # Keyed on (resolved source, destination), NOT the source alone. Deduping
    # by source discarded an aliased target's own slot: with
    # VECTOR_DB_FILE=alias.db symlinked to graph.db the set held only
    # graph.db, so restoring into an empty directory left no alias.db and the
    # vector store could not reopen -- while the backup reported success.
    # Two destinations do not collide, so there is nothing to dedupe.
    seen_sources: dict[tuple[Path, Path], Path] = {}

    for target in targets:
        source = _source_for(target, data_dir)

        if target.kind == "excluded":
            if source.exists():
                outcome.entries.append(
                    BackupEntry(target.label, "excluded", source=source, note=target.reason)
                )
                say(f"  excluded  {target.label} -- {target.reason}")
            else:
                outcome.entries.append(BackupEntry(target.label, "skipped", note="not present"))
            continue

        if not source.exists():
            outcome.entries.append(BackupEntry(target.label, "skipped", note="not present"))
            continue

        resolved = source.resolve()
        destination = _destination_for(target, source, data_dir, staging)
        if (resolved, destination) in seen_sources:
            # The same file AND the same slot, e.g. VECTOR_DB_FILE naming a
            # core file outright. Copying again would collide on the
            # destination and add nothing.
            outcome.entries.append(
                BackupEntry(
                    target.label,
                    "skipped",
                    note=f"same file and same slot as an earlier target, already backed up to "
                    f"{seen_sources[(resolved, destination)].name}",
                )
            )
            continue

        _require_contained(destination.parent, staging)
        _make_parents(destination.parent, staging, data_dir, target)
        if destination.exists() or destination.is_symlink():
            # Reachable for a pathological configuration: a relative
            # VECTOR_DB_FILE pointing INSIDE a directory store (say
            # "chroma/vectors.db") lands where that store's copytree already
            # wrote. Name the configuration rather than leaving the operator
            # with a bare path collision.
            raise BackupError(
                f"backup target already exists in the set: {destination}. "
                f"Target {target.label!r} resolves to a path another target already "
                f"wrote; check whether it is configured inside another store's directory."
            )

        if target.kind == "sqlite":
            journal_mode, snapshot = pins.open_snapshot(source)
            if snapshot is None:
                backup_sqlite(source, destination, deadline=time.monotonic() + timeout)
                capture = "copied_under_lock"
                note = "online .backup() under write.lock + integrity_check"
            else:
                capture = "wal_snapshot"
                note = "online .backup() from a pinned WAL snapshot + integrity_check"
            entry = BackupEntry(
                target.label,
                "verified",
                source=source,
                destination=destination,
                note=note,
                capture=capture,
                journal_mode=journal_mode,
            )
            jobs.append(_Job(entry, source, destination, snapshot))
            seen_sources[(resolved, destination)] = destination
            outcome.entries.append(entry)
            continue

        status: Status
        if target.kind == "directory":
            _copy_directory(source, destination)
            status = "unverified"
            note = _directory_note(target, destination, source)
        else:  # opaque
            if source.is_dir():
                _copy_directory(source, destination)
            else:
                shutil.copy2(source, destination)
            status = "unverified"
            note = (
                "byte-wise copy: not a SQLite database, so this is not a consistent snapshot"
            )

        seen_sources[(resolved, destination)] = destination
        outcome.entries.append(
            BackupEntry(target.label, status, source=source, destination=destination, note=note)
        )
        say(f"  {status:10} {target.label} -> {destination.relative_to(staging)}  ({note})")

    return outcome


def _finish_jobs(
    jobs: list[_Job],
    pins: _SnapshotPins,
    staging: Path,
    say: Callable[[str], None],
    timeout: float,
) -> None:
    """Copy the pinned snapshots, then verify every SQLite copy. Runs without write.lock.

    Each snapshot connection is closed as soon as its copy ends, so the WAL of
    that database can be checkpointed again while the others are still being
    copied.
    """
    for job in jobs:
        snapshot = job.snapshot
        if snapshot is None:
            continue
        budget = max(timeout, snapshot.copy_bytes / _MIN_COPY_BYTES_PER_SECOND)
        backup_sqlite(
            job.source,
            job.destination,
            deadline=time.monotonic() + budget,
            src_conn=snapshot.connection,
            flush_every=_COPY_FLUSH_BYTES,
        )
        pins.close_checked(snapshot.connection)
    for job in jobs:
        verify_sqlite(job.destination)
        say(
            f"  {job.entry.status:10} {job.entry.label} -> "
            f"{job.destination.relative_to(staging)}  ({job.entry.note})"
        )


def _copy_directory(source: Path, destination: Path) -> None:
    """Copy a directory store, preserving symlinks instead of following them.

    ``copytree``'s default follows links, which would pull content from
    outside the data directory into the backup. Preserving the link is the
    faithful snapshot; the link's TARGET content is not backed up, which the
    docs say out loud. The same rule applies to the catalog check below: a
    linked ``chroma.sqlite3`` is preserved and not verified, so nothing
    outside the set is read or written.

    A copied chroma catalog that FAILS ``integrity_check`` aborts the whole
    backup. "Could not fully verify" and "known to be bad" are different
    facts: a passing catalog still proves nothing about the HNSW index and
    segment files, which is why the entry stays ``unverified``, but a failing
    one is positive evidence that the copy is unusable. Publishing that as a
    success let the migration overwrite the live store next, leaving the
    operator holding a backup already known to be broken.
    """
    shutil.copytree(source, destination, symlinks=True)
    catalog = destination / "chroma.sqlite3"
    # A catalog that is itself a preserved link is NOT verified: ``is_file()``
    # follows links, and ``verify_sqlite`` opens its target read-write, so
    # the check would read (and, with a hot journal, WRITE) something outside
    # the set -- and abort the backup when the target is missing or not
    # SQLite, contradicting "preserve internal links, never follow them".
    if catalog.is_symlink():
        return
    if catalog.is_file():
        try:
            verify_sqlite(catalog)
        except BackupError as exc:
            raise BackupError(
                f"the copied chroma catalog at {catalog} is not usable ({exc}). "
                "Refusing to publish a backup whose vector catalog is already known bad."
            ) from exc


def _directory_note(target: BackupTarget, destination: Path, source: Path) -> str:
    """Describe the copy. Verification already happened in ``_copy_directory``.

    Kept description-only on purpose: when this function both verified and
    described, a failed catalog check turned into a warning inside a string
    and the backup was published anyway.
    """
    note = (
        "directory copy: consistent only if nothing wrote to it during the run "
        "(chroma.lock is a shared lock with the offline batch loader, see issue #140)"
    )
    inner = destination / "chroma.sqlite3"
    if inner.is_symlink():
        note += (
            "; its chroma.sqlite3 is a preserved symlink and was NOT verified "
            "(the link target was not followed or verified)"
        )
    elif inner.is_file():
        # Reaching here means it already passed; a failure aborted the run.
        note += "; its chroma.sqlite3 passes integrity_check, which does not prove the "
        note += "HNSW index and segment files agree with it"
    else:
        note += "; it holds no chroma.sqlite3 catalog to check"
    if source.is_symlink():
        # Links found INSIDE a directory store are preserved as links, but the
        # store's own top-level link is followed: an operator who relocated
        # the store to another volume wants its contents, and a backup holding
        # only a link would be useless. Announce the asymmetry rather than
        # leaving it for someone to discover during a restore.
        note += (
            f"; NOTE this target is itself a symlink and was followed to "
            f"{source.resolve()} (links found inside it are preserved as links)"
        )
    return note


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m opencrab.stores.backup",
        description=(
            "Back up the local store directory as one set while the services keep running. "
            "SQLite files are copied with the online backup API and verified; see the "
            "module docstring and docs/live-backup.md for exactly what is and is not "
            "guaranteed."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="directory to back up (default: LOCAL_DATA_DIR from settings)",
    )
    parser.add_argument(
        "--to",
        default=None,
        help=(
            "where to create the backup set (default: alongside the data itself). "
            "This is taken as the location you chose: it is not containment-checked, "
            "so a symlinked ancestor is followed."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the canonical target inventory and exit without writing anything",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "how long to wait for write.lock before giving up "
            f"(default: ${_TIMEOUT_ENV} or {DEFAULT_LOCK_TIMEOUT:g})"
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="LABEL",
        help=(
            "skip one inventory target by its label (see --list); repeatable. The set "
            "records it as excluded, and a restore of that set will not contain it."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "print one JSON object (the set's manifest plus set_dir) on stdout; "
            "progress messages go to stderr"
        ),
    )
    parser.add_argument(
        "--verify",
        default=None,
        metavar="SET_DIR",
        help=(
            "check a published backup set against its MANIFEST.json instead of making "
            "one: sizes, sha256 and integrity_check. Writes nothing."
        ),
    )
    args = parser.parse_args(argv)

    settings = None

    if args.verify is not None:
        try:
            report = verify_backup_set(args.verify)
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report))
        else:
            print(
                f"OK {report['set']}: {report['files_verified']} file(s) match the manifest, "
                f"{len(report['sqlite_integrity_checked'])} SQLite file(s) pass integrity_check"
            )
            for rel in report["unlisted"]:
                print(f"  unlisted (not in the manifest, not checked): {rel}")
        return 0

    if args.list:
        # Handled BEFORE resolving the data directory: listing the targets
        # does not need one, and resolving it would call get_settings()
        # outside the error handling below, so a malformed configuration
        # printed a traceback instead of the reason. local_data_dir_inventory
        # raises BackupError for that case on its own.
        try:
            targets = local_data_dir_inventory(settings)
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        for t in targets:
            print(f"{t.kind:10} {t.label:24} {t.reason}")
        return 0

    data_dir = args.data_dir
    if data_dir is None:
        try:
            from opencrab.config import get_settings

            settings = get_settings()
        except Exception as exc:
            print(
                f"ERROR: cannot read the configuration to locate the data directory ({exc}). "
                "Pass --data-dir explicitly, or fix the configuration.",
                file=sys.stderr,
            )
            return 1
        data_dir = settings.local_data_dir

    # With --json, stdout carries only the final object; progress goes to stderr.
    progress_stream = sys.stderr if args.json else None

    try:
        # flush=True so a closed stdout fails HERE, inside the callback, not
        # silently at interpreter exit with a bare exit code.
        outcome = backup_data_dir(
            data_dir,
            args.to,
            settings=settings,
            lock_timeout=args.lock_timeout,
            exclude=args.exclude,
            on_event=lambda m: print(m, file=progress_stream, flush=True),
        )
        if args.json:
            print(json.dumps({"set_dir": str(outcome.set_dir), **outcome.manifest}), flush=True)
    except BackupDurabilityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (BackupError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # NOT from the backup: backup_data_dir converts its own OS failures
        # to BackupError. This is the print callback above (a closed stdout
        # raises BrokenPipeError), which the boundary re-raises unchanged.
        # The set may already be published; say so rather than guess.
        _detach_stdout()
        print(
            f"ERROR: output failed ({exc}); the backup may have run to completion, "
            "check the destination for a published set",
            file=sys.stderr,
        )
        return 1
    # skipped/unverified/excluded are the declared contract, not failures:
    # the summary names them and the run succeeds.
    try:
        # sys.stdout is None when fd 1 was closed outright (``>&-``); print()
        # was a no-op all along and there is nothing to flush.
        if sys.stdout is not None:
            sys.stdout.flush()
    except OSError as exc:
        _detach_stdout()
        print(f"ERROR: output failed after the backup ran ({exc})", file=sys.stderr)
        return 1
    return 0


def _detach_stdout() -> None:
    """Point stdout at /dev/null after a write failure.

    A BrokenPipeError leaves unflushed data in the buffer; the interpreter
    flushes again at exit, fails again, and turns the exit code into 120.
    Replacing the descriptor (the Python docs' recipe) makes that final
    flush succeed so the exit code we return is the one the user sees.
    """
    if sys.stdout is None:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except OSError:
        pass


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(_main())
