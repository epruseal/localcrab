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
held around the whole run narrows that window for every writer that honours
it, but not every writer does -- ``billing.db`` is deliberately written
outside it (#105), and the ownership map has known holes (#141). Say exactly
this and no more, in the output and in the docs.

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
keep it through ``copy2``/``copytree``, and the set directory gets the data
directory's mode at publication. While the set is being written, the staging
directory and each SQLite copy are readable by the owner only.
"""

from __future__ import annotations

import argparse
import math
import os
import secrets
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "BackupDurabilityError",
    "BackupEntry",
    "BackupError",
    "BackupOutcome",
    "BackupTarget",
    "backup_data_dir",
    "backup_sqlite",
    "local_data_dir_inventory",
    "verify_sqlite",
]

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


@dataclass
class BackupOutcome:
    set_dir: Path
    entries: list[BackupEntry] = field(default_factory=list)
    leftover_stagings: list[Path] = field(default_factory=list)

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
    _on_step: Callable[[int, int, int], None] | None = None,
) -> None:
    """Copy ``src`` to ``dst`` with SQLite's online backup API.

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
    try:
        os.close(os.open(str(dst), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError as exc:
        raise BackupError(
            f"backup destination already exists, refusing to overwrite: {dst}"
        ) from exc
    # timeout=0 so a contended source returns SQLITE_BUSY to us IMMEDIATELY.
    # The default 5s busy timeout would be spent inside a single
    # sqlite3_backup_step(), and the progress callback -- the only place the
    # deadline can be enforced -- runs between steps, so a 0.2s deadline
    # measured 5.06s. With timeout=0 the same case measures 0.20s. The
    # waiting is done by the callback below, which is deadline-aware.
    src_conn = sqlite3.connect(_rw_uri(src), uri=True, timeout=0)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:

            def progress(status: int, remaining: int, total: int) -> None:
                remaining_budget = deadline - time.monotonic()
                if remaining_budget <= 0:
                    raise TimeoutError(
                        f"backup of {src} exceeded its deadline with {remaining} page(s) left"
                    )
                if _on_step is not None:
                    _on_step(status, remaining, total)
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    # Compute the remaining budget ONCE and only sleep on a
                    # positive value: recomputing here could go negative
                    # between the two reads and make sleep() raise ValueError.
                    time.sleep(min(0.05, remaining_budget))

            src_conn.backup(dst_conn, pages=pages, progress=progress, sleep=0)
        finally:
            dst_conn.close()
    except sqlite3.DatabaseError as exc:
        # No copy2 fallback. DatabaseError also covers corruption, locking and
        # I/O errors, and a raw byte copy reported as a backup is precisely
        # the "believed to have a backup" harm #128 is about.
        raise BackupError(
            f"{src} could not be copied with SQLite's online backup API ({exc}). "
            "It is not a usable SQLite database; refusing to report a raw copy as a backup."
        ) from exc
    finally:
        src_conn.close()
    # Same access as the source, no wider and no narrower: an operator whose
    # backups are read by another account configured that on the source.
    shutil.copymode(src, dst)


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
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"backup {path} does not open as a SQLite database: {exc}") from exc
    if not result or result[0] != "ok":
        raise BackupError(f"backup {path} failed integrity_check: {result}")


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
) -> BackupOutcome:
    """Back up every present store artifact under ``data_dir`` as one set.

    This function owns the cross-process ``write.lock`` so that no entry
    point -- the migration script, the CLI, a direct caller -- can bypass it.
    The lock is re-entrant within a thread, so an outer holder is fine.

    Guarantee: per-file transactional consistency for every SQLite target.
    NOT a cross-file point-in-time snapshot; see the module docstring.

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
    dest = Path(dest_dir) if dest_dir is not None else data_dir
    say = on_event or (lambda _msg: None)
    timeout = _resolve_timeout(lock_timeout)

    if not data_dir.is_dir():
        raise BackupError(f"data directory does not exist: {data_dir}")

    targets = local_data_dir_inventory(settings)

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

    with write_lock(str(data_dir), timeout=timeout):
        # Exclusive create. Not makedirs(exist_ok=True): a pre-planted
        # directory or symlink here would be written through, and every
        # temporary artifact below relies on this directory being ours.
        # Owner-only while the set is being written; it takes the data
        # directory's own mode right before publication, so the set is
        # exactly as reachable as the data it snapshots.
        os.mkdir(staging, 0o700)
        try:
            outcome = _run_targets(targets, data_dir, staging, final, say, timeout)
            shutil.copymode(data_dir, staging)
            _fsync_tree(staging)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        _publish(staging, final)

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
        "guarantee: per-file transactional consistency for SQLite targets. "
        "Files are NOT aligned to one point in time -- write.lock narrows that window "
        "only for writers that honour it (see issue #141)."
    )
    return outcome


def _run_targets(
    targets: Iterable[BackupTarget],
    data_dir: Path,
    staging: Path,
    final: Path,
    say: Callable[[str], None],
    timeout: float,
) -> BackupOutcome:
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
        destination.parent.mkdir(parents=True, exist_ok=True)
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
            deadline = time.monotonic() + timeout
            backup_sqlite(source, destination, deadline=deadline)
            verify_sqlite(destination)
            status: Status = "verified"
            note = "online .backup() + integrity_check"
        elif target.kind == "directory":
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
            "Back up the local store directory as one consistent set. SQLite files are "
            "copied with the online backup API and verified; see the module docstring "
            "for exactly what is and is not guaranteed."
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
    args = parser.parse_args(argv)

    settings = None

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

    try:
        backup_data_dir(data_dir, args.to, settings=settings, on_event=lambda m: print(m))
    except BackupDurabilityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (BackupError, TimeoutError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    # skipped/unverified/excluded are the declared contract, not failures:
    # the summary names them and the run succeeds.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(_main())
