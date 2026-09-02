"""Consistency and completeness of the local store backup (issues #128, #123).

#128: ``backup_local_data()`` copied live SQLite files with ``shutil.copy2``,
which is not a consistent snapshot -- a hot rollback journal is left behind
(``SQLStore`` does not enable WAL, see #105), so the copy keeps pages that
SQLite would have rolled back. #123: the target list was hardcoded in two
places and neither listed ``vectors.db``.

The decisive reproduction is ``TestHotJournal``: it builds a REAL hot journal
(a separate process dies mid-transaction after its dirty pages spill into the
database file) and shows, against a control group, that the difference between
a raw copy and an online ``.backup()`` is caused by the missing journal.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from opencrab.stores import backup as bk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(path: Path, rows: int = 5, *, wal: bool = False) -> None:
    """A small committed SQLite database at ``path``."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (id, v) VALUES (?, ?)", [(i, f"row-{i}") for i in range(rows)]
        )
        conn.commit()
    finally:
        conn.close()


def _rows(path: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(conn.execute("SELECT id, v FROM t").fetchall())
    finally:
        conn.close()


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def _set_dir(dest: Path) -> Path:
    """The single published backup set under ``dest``."""
    sets = [p for p in dest.iterdir() if p.is_dir() and p.name.startswith("backup.")]
    assert len(sets) == 1, f"expected exactly one published set, found {sets}"
    return sets[0]


def _published_sets(dest: Path) -> list[Path]:
    return [p for p in dest.iterdir() if p.is_dir() and p.name.startswith("backup.")]


def _stagings(dest: Path) -> list[Path]:
    return [p for p in dest.iterdir() if p.name.startswith(".backup-staging.")]


class _Settings:
    """Minimal stand-in for ``opencrab.config.Settings``.

    Only the fields the inventory consults. A real Settings object is not
    used so a test never depends on ambient environment/config state.
    """

    def __init__(self, *, vector_db_file: str = "vectors.db", local_data_dir: str = "") -> None:
        self.vector_db_file = vector_db_file
        self.local_data_dir = local_data_dir


# ---------------------------------------------------------------------------
# 3-1  Hot rollback journal -- the decisive reproduction (#128)
# ---------------------------------------------------------------------------

_CRASHER = textwrap.dedent(
    """
    import os, sqlite3, sys
    db = sys.argv[1]
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA cache_spill=ON")
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN IMMEDIATE")
    blob = "x" * 4096
    for i in range(1000, 1400):
        conn.execute("INSERT INTO t (id, v) VALUES (?, ?)", (i, blob))
    # Dirty pages have spilled into the main database file by now; the
    # transaction is NOT committed. Die without unwinding so the rollback
    # journal is left behind as a *hot* journal (no owning connection).
    os._exit(1)
    """
)


@pytest.fixture
def hot_journal_db(tmp_path: Path) -> tuple[Path, list[tuple[int, str]]]:
    """A database with a real hot rollback journal, plus its committed rows."""
    db = tmp_path / "hot.db"
    _make_db(db, rows=5)
    committed = _rows(db)
    assert _integrity(db) == "ok", "fixture precondition: baseline database is sound"

    script = tmp_path / "crasher.py"
    script.write_text(_CRASHER, encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(db)], capture_output=True, text=True
    )
    assert proc.returncode == 1, f"crasher did not die as expected: {proc.returncode} {proc.stderr}"
    assert (tmp_path / "hot.db-journal").is_file(), (
        "fixture precondition: no hot rollback journal was produced"
    )
    return db, committed


class TestHotJournal:
    def test_raw_copy_without_journal_loses_the_committed_state(
        self, hot_journal_db: tuple[Path, list[tuple[int, str]]], tmp_path: Path
    ) -> None:
        """Control group: the difference is caused by the missing journal.

        Both copies are taken BEFORE anything opens the source read-write --
        that open is what makes SQLite recover the journal, which would
        destroy the very state under test.
        """
        db, committed = hot_journal_db

        raw_only = tmp_path / "raw_only.db"
        shutil.copy2(db, raw_only)

        raw_with_journal = tmp_path / "raw_with_journal.db"
        shutil.copy2(db, raw_with_journal)
        shutil.copy2(db.with_name("hot.db-journal"), raw_with_journal.with_name(
            "raw_with_journal.db-journal"
        ))

        # With the journal, SQLite rolls back and the copy IS the committed state.
        assert _rows(raw_with_journal) == committed

        # Without it, the copy is not the committed state. This is exactly what
        # shutil.copy2 of a live database produces, and it is issue #128.
        try:
            observed = _rows(raw_only)
        except sqlite3.DatabaseError:
            observed = None
        assert observed != committed, (
            "fixture precondition failed: the uncommitted transaction did not "
            "spill into the database file, so this test would prove nothing"
        )

    def test_read_only_connection_cannot_recover_a_hot_journal(
        self, hot_journal_db: tuple[Path, list[tuple[int, str]]]
    ) -> None:
        """Why the source is opened rw, not ro: ro fails exactly when it matters."""
        db, _ = hot_journal_db
        uri = db.resolve().as_uri() + "?mode=ro"
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            conn = sqlite3.connect(uri, uri=True)
            try:
                conn.execute("SELECT count(*) FROM t").fetchone()
            finally:
                conn.close()
        assert excinfo.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK

    def test_online_backup_yields_exactly_the_committed_state(
        self, hot_journal_db: tuple[Path, list[tuple[int, str]]], tmp_path: Path
    ) -> None:
        db, committed = hot_journal_db
        dst = tmp_path / "backed_up.db"

        bk.backup_sqlite(db, dst, deadline=time.monotonic() + 30)
        bk.verify_sqlite(dst)

        assert _integrity(dst) == "ok"
        assert _rows(dst) == committed


# ---------------------------------------------------------------------------
# 3-2  Backup while another connection writes
# ---------------------------------------------------------------------------


class TestConcurrentWrite:
    def test_snapshot_is_transactionally_consistent(self, tmp_path: Path) -> None:
        """A commit landing mid-backup must not tear the snapshot.

        The write is committed from the production backup's own step
        observer, at a point where pages still remain -- a write on the final
        DONE step would not overlap the copy at all and would prove nothing.
        """
        src = tmp_path / "src.db"
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("CREATE TABLE a (k INTEGER PRIMARY KEY, v INTEGER)")
            conn.execute("CREATE TABLE b (k INTEGER PRIMARY KEY, v INTEGER)")
            # Enough pages that pages=1 leaves work remaining for many steps.
            for i in range(400):
                conn.execute("INSERT INTO a VALUES (?, ?)", (i, 0))
                conn.execute("INSERT INTO b VALUES (?, ?)", (i, 0))
            conn.commit()
        finally:
            conn.close()

        state = {"overlapped": False, "done": False}

        def observer(status: int, remaining: int, total: int) -> None:
            if state["overlapped"] or remaining <= 0:
                return
            state["overlapped"] = True
            # One finite transaction touching BOTH tables: the invariant the
            # snapshot must preserve is that a reader never sees one table
            # updated and the other not.
            w = sqlite3.connect(str(src), timeout=30)
            try:
                w.execute("BEGIN IMMEDIATE")
                w.execute("UPDATE a SET v = 1")
                w.execute("UPDATE b SET v = 1")
                w.commit()
            finally:
                w.close()
            state["done"] = True

        dst = tmp_path / "dst.db"
        bk.backup_sqlite(src, dst, deadline=time.monotonic() + 60, pages=1, _on_step=observer)
        bk.verify_sqlite(dst)

        assert state["overlapped"], "the write never overlapped the backup"
        assert state["done"]
        assert _integrity(dst) == "ok"

        out = sqlite3.connect(str(dst))
        try:
            a_vals = {r[0] for r in out.execute("SELECT DISTINCT v FROM a")}
            b_vals = {r[0] for r in out.execute("SELECT DISTINCT v FROM b")}
        finally:
            out.close()
        assert len(a_vals) == 1 and len(b_vals) == 1, "snapshot tore within a table"
        assert a_vals == b_vals, f"snapshot is not transactionally consistent: a={a_vals} b={b_vals}"

    def test_step_observer_exception_aborts_the_backup(self, tmp_path: Path) -> None:
        src = tmp_path / "src.db"
        _make_db(src, rows=200)
        dst = tmp_path / "dst.db"

        class Boom(RuntimeError):
            pass

        def observer(status: int, remaining: int, total: int) -> None:
            raise Boom("stop")

        with pytest.raises(Boom):
            bk.backup_sqlite(src, dst, deadline=time.monotonic() + 30, pages=1, _on_step=observer)

    def test_backup_deadline_is_enforced_under_a_write_lock(self, tmp_path: Path) -> None:
        """A writer holding EXCLUSIVE must not make the backup wait forever."""
        src = tmp_path / "src.db"
        _make_db(src, rows=50)
        dst = tmp_path / "dst.db"

        holder = sqlite3.connect(str(src), isolation_level=None, timeout=30)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute("INSERT INTO t (id, v) VALUES (9999, 'held')")
            # Confirm the lock is really held: an independent writer must fail fast.
            probe = sqlite3.connect(str(src), timeout=0.1)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    probe.execute("BEGIN IMMEDIATE")
            finally:
                probe.close()

            started = time.monotonic()
            with pytest.raises(TimeoutError):
                bk.backup_sqlite(src, dst, deadline=time.monotonic() + 1.0)
            elapsed = time.monotonic() - started
            assert elapsed < 20, f"backup did not honour its deadline (took {elapsed:.1f}s)"
        finally:
            holder.close()


# ---------------------------------------------------------------------------
# 3-3  The canonical inventory (#123)
# ---------------------------------------------------------------------------


class TestInventory:
    def test_vector_db_is_a_target(self) -> None:
        """#123: vectors.db was missing from the backup list entirely."""
        labels = {t.label for t in bk.local_data_dir_inventory(_Settings())}
        assert "vectors.db" in labels

    def test_core_stores_are_targets(self) -> None:
        labels = {t.label for t in bk.local_data_dir_inventory(_Settings())}
        assert {"opencrab.db", "graph.db", "doc_store.db", "billing.db"} <= labels

    def test_inventory_covers_every_factory_data_dir_artifact(self) -> None:
        """Guard against #123 recurring: a new store must update the inventory.

        ``factory.py`` is the single place that joins a store file onto
        LOCAL_DATA_DIR. Every name it joins there must appear in the
        inventory, or a future store silently drops out of the backup the
        way ``vectors.db`` did.
        """
        import re

        factory_src = Path(bk.__file__).with_name("factory.py").read_text(encoding="utf-8")
        joined = set(re.findall(r"local_data_dir[,)]\s*[\"']([^\"']+)[\"']", factory_src))
        joined |= set(re.findall(r"local_data_dir\)\s*/\s*[\"']([^\"']+)[\"']", factory_src))
        assert joined, "could not extract any data-dir artifact from factory.py"

        known = {t.label for t in bk.local_data_dir_inventory(_Settings())}
        missing = joined - known
        assert not missing, f"factory.py writes these under LOCAL_DATA_DIR but the inventory omits them: {sorted(missing)}"

    def test_renamed_vector_file_is_followed_and_default_still_covered(self) -> None:
        targets = bk.local_data_dir_inventory(_Settings(vector_db_file="renamed.db"))
        labels = {t.label for t in targets}
        assert "renamed.db" in labels, "VECTOR_DB_FILE rename is not followed"
        assert "vectors.db" in labels, "settings must only ADD targets, never remove them"

    def test_kinds_are_declared(self) -> None:
        kinds = {t.label: t.kind for t in bk.local_data_dir_inventory(_Settings())}
        assert kinds["opencrab.db"] == "sqlite"
        assert kinds["chroma"] == "directory"
        assert kinds["docs"] == "directory"
        assert kinds["graph.kuzu"] == "opaque"
        assert kinds["packs"] == "excluded"

    def test_inventory_never_targets_its_own_output(self) -> None:
        labels = {t.label for t in bk.local_data_dir_inventory(_Settings())}
        assert not any(x.startswith(("backup.", ".backup-staging.")) for x in labels)


# ---------------------------------------------------------------------------
# backup_data_dir: the published set
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    for name in ("opencrab.db", "graph.db", "doc_store.db", "billing.db", "vectors.db"):
        _make_db(d / name, rows=3)
    (d / "chroma").mkdir()
    (d / "chroma" / "chroma.sqlite3").touch()
    _make_db(d / "chroma" / "chroma.sqlite3", rows=2)
    (d / "chroma" / "index.bin").write_bytes(b"vec")
    (d / "docs").mkdir()
    (d / "docs" / "nodes.json").write_text("{}", encoding="utf-8")
    (d / "packs").mkdir()
    (d / "packs" / "p1").mkdir()
    return d


class TestPublishedSet:
    def test_set_contains_every_present_store_including_vectors(
        self, populated_dir: Path
    ) -> None:
        outcome = bk.backup_data_dir(populated_dir, settings=_Settings())
        s = _set_dir(populated_dir)
        assert outcome.set_dir == s
        for name in ("opencrab.db", "graph.db", "doc_store.db", "billing.db", "vectors.db"):
            assert (s / name).is_file(), f"{name} missing from the published set"
        assert (s / "chroma" / "index.bin").is_file()
        assert (s / "docs" / "nodes.json").is_file()

    def test_every_sqlite_copy_opens_and_passes_integrity_check(
        self, populated_dir: Path
    ) -> None:
        bk.backup_data_dir(populated_dir, settings=_Settings())
        s = _set_dir(populated_dir)
        for name in ("opencrab.db", "graph.db", "doc_store.db", "billing.db", "vectors.db"):
            assert _integrity(s / name) == "ok"
            assert _rows(s / name) == _rows(populated_dir / name)

    def test_every_sqlite_target_went_through_backup_and_verify(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exhaustiveness: no sqlite target may bypass the online path."""
        backed: list[str] = []
        verified: list[str] = []
        real_backup, real_verify = bk.backup_sqlite, bk.verify_sqlite

        def spy_backup(src, dst, **kw):  # type: ignore[no-untyped-def]
            backed.append(Path(src).name)
            return real_backup(src, dst, **kw)

        def spy_verify(path):  # type: ignore[no-untyped-def]
            verified.append(Path(path).name)
            return real_verify(path)

        monkeypatch.setattr(bk, "backup_sqlite", spy_backup)
        monkeypatch.setattr(bk, "verify_sqlite", spy_verify)
        bk.backup_data_dir(populated_dir, settings=_Settings())

        expected = {"opencrab.db", "graph.db", "doc_store.db", "billing.db", "vectors.db"}
        assert expected <= set(backed), f"not copied via .backup(): {expected - set(backed)}"
        assert expected <= set(verified), f"not verified: {expected - set(verified)}"

    def test_no_wal_or_shm_sidecars_are_written(self, populated_dir: Path) -> None:
        """.backup() destinations are standalone; sidecars would mislead."""
        _make_db(populated_dir / "graph.db", rows=4, wal=True)
        bk.backup_data_dir(populated_dir, settings=_Settings())
        s = _set_dir(populated_dir)
        assert not list(s.glob("*-wal"))
        assert not list(s.glob("*-shm"))

    def test_missing_targets_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        outcome = bk.backup_data_dir(d, settings=_Settings())
        statuses = {e.label: e.status for e in outcome.entries}
        assert statuses["graph.db"] == "skipped"
        assert outcome.copied == {}

    def test_packs_is_announced_not_silently_omitted(self, populated_dir: Path) -> None:
        outcome = bk.backup_data_dir(populated_dir, settings=_Settings())
        entry = next(e for e in outcome.entries if e.label == "packs")
        assert entry.status == "excluded"
        assert str(populated_dir / "packs") in str(entry.source)
        assert entry.note, "an excluded target must say why"
        assert not (_set_dir(populated_dir) / "packs").exists()

    def test_directory_targets_report_unverified_not_verified(
        self, populated_dir: Path
    ) -> None:
        """chroma's SQLite integrity does not prove its HNSW segments agree."""
        outcome = bk.backup_data_dir(populated_dir, settings=_Settings())
        by_label = {e.label: e for e in outcome.entries}
        assert by_label["chroma"].status == "unverified"
        assert by_label["docs"].status == "unverified"
        assert by_label["opencrab.db"].status == "verified"

    def test_directory_symlinks_are_preserved_not_followed(
        self, populated_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("must not be copied", encoding="utf-8")
        (populated_dir / "docs" / "link").symlink_to(outside)

        bk.backup_data_dir(populated_dir, settings=_Settings())
        copied = _set_dir(populated_dir) / "docs" / "link"
        assert copied.is_symlink(), "symlink was followed; external content was pulled in"
        assert not (copied / "secret.txt").is_file() or copied.is_symlink()

    def test_graph_kuzu_handled_as_file_and_as_directory(self, tmp_path: Path) -> None:
        as_file = tmp_path / "f"
        as_file.mkdir()
        (as_file / "graph.kuzu").write_bytes(b"not sqlite")
        out = bk.backup_data_dir(as_file, settings=_Settings())
        assert next(e for e in out.entries if e.label == "graph.kuzu").status == "unverified"
        assert (_set_dir(as_file) / "graph.kuzu").is_file()

        as_dir = tmp_path / "d"
        as_dir.mkdir()
        (as_dir / "graph.kuzu").mkdir()
        (as_dir / "graph.kuzu" / "data").write_bytes(b"x")
        out = bk.backup_data_dir(as_dir, settings=_Settings())
        assert next(e for e in out.entries if e.label == "graph.kuzu").status == "unverified"
        assert (_set_dir(as_dir) / "graph.kuzu" / "data").is_file()

    def test_return_maps_each_source_to_its_destination(self, populated_dir: Path) -> None:
        outcome = bk.backup_data_dir(populated_dir, settings=_Settings())
        s = _set_dir(populated_dir)
        assert outcome.copied[str(populated_dir / "graph.db")] == str(s / "graph.db")


# ---------------------------------------------------------------------------
# Path normalisation and containment (#212 invariant)
# ---------------------------------------------------------------------------


class TestPathHandling:
    def test_vector_alias_of_a_core_file_is_copied_once(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=3)
        outcome = bk.backup_data_dir(d, settings=_Settings(vector_db_file="graph.db"))
        s = _set_dir(d)
        assert (s / "graph.db").is_file()
        # One source, one destination -- not a collision, not a double copy.
        assert list(outcome.copied) == [str(d / "graph.db")]

    def test_symlinked_vector_alias_is_deduped_by_resolved_path(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=3)
        (d / "alias.db").symlink_to(d / "graph.db")
        outcome = bk.backup_data_dir(d, settings=_Settings(vector_db_file="alias.db"))
        assert len(outcome.copied) == 1, f"aliased source copied twice: {outcome.copied}"

    def test_absolute_vector_path_is_parked_inside_the_set(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir()
        external = tmp_path / "elsewhere"
        external.mkdir()
        _make_db(external / "graph.db", rows=3)
        bk.backup_data_dir(d, settings=_Settings(vector_db_file=str(external / "graph.db")))
        s = _set_dir(d)
        parked = s / "external-vector" / "graph.db"
        assert parked.is_file(), "an external vector source must be parked, not dropped"
        assert _integrity(parked) == "ok"

    def test_symlinked_set_destination_is_refused(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=2)
        outside = tmp_path / "outside"
        outside.mkdir()
        # Force the containment check to see an escaping destination.
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(d, dest_dir=d / "escape", settings=_Settings())
        (d / "escape").symlink_to(outside)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(d, dest_dir=d / "escape", settings=_Settings())

    def test_destination_inside_a_directory_source_is_refused(self, populated_dir: Path) -> None:
        """Staging inside chroma/ would make copytree recurse into itself."""
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(
                populated_dir, dest_dir=populated_dir / "chroma", settings=_Settings()
            )

    def test_preexisting_staging_directory_aborts_without_writing(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-planted staging path must never be written through."""
        monkeypatch.setattr(bk, "_new_set_id", lambda: "FIXED")
        (populated_dir / ".backup-staging.FIXED").mkdir()
        with pytest.raises(FileExistsError):
            bk.backup_data_dir(populated_dir, settings=_Settings())
        assert _published_sets(populated_dir) == []

    def test_existing_set_directory_is_not_overwritten(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bk, "_new_set_id", lambda: "FIXED")
        (populated_dir / "backup.FIXED").mkdir()
        (populated_dir / "backup.FIXED" / "keep").write_text("keep", encoding="utf-8")
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(populated_dir, settings=_Settings())
        assert (populated_dir / "backup.FIXED" / "keep").read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------------------
# Failure leaves nothing behind
# ---------------------------------------------------------------------------


class TestAtomicity:
    def test_a_corrupt_store_file_aborts_instead_of_falling_back_to_a_raw_copy(
        self, populated_dir: Path
    ) -> None:
        """A raw copy reported as a backup is the harm #128 describes."""
        (populated_dir / "graph.db").write_bytes(b"this is not a sqlite database at all")
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(populated_dir, settings=_Settings())
        assert _published_sets(populated_dir) == []
        assert _stagings(populated_dir) == []

    def test_failure_on_a_later_item_publishes_nothing(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the first item: earlier successes must not survive either."""
        real = bk.backup_sqlite
        seen: list[str] = []

        def failing(src, dst, **kw):  # type: ignore[no-untyped-def]
            seen.append(Path(src).name)
            if len(seen) == 3:
                raise bk.BackupError(f"induced failure on {Path(src).name}")
            return real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", failing)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(populated_dir, settings=_Settings())

        assert len(seen) == 3, "the induced failure did not land on a later item"
        assert _published_sets(populated_dir) == [], "a partial set was published"
        assert _stagings(populated_dir) == [], "staging was not cleaned up"

    def test_verification_failure_publishes_nothing(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def bad_verify(path):  # type: ignore[no-untyped-def]
            raise bk.BackupError(f"integrity_check failed for {path}")

        monkeypatch.setattr(bk, "verify_sqlite", bad_verify)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(populated_dir, settings=_Settings())
        assert _published_sets(populated_dir) == []
        assert _stagings(populated_dir) == []

    def test_fsync_failure_before_publish_publishes_nothing(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def bad_fsync(path):  # type: ignore[no-untyped-def]
            raise OSError("induced fsync failure")

        monkeypatch.setattr(bk, "_fsync_path", bad_fsync)
        with pytest.raises(OSError):
            bk.backup_data_dir(populated_dir, settings=_Settings())
        assert _published_sets(populated_dir) == []

    def test_fsync_failure_after_publish_reports_indeterminate_durability(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The set IS visible once renamed; deleting it would be worse.

        The run still fails, and it must say the durability is unknown rather
        than claim a durable backup.
        """
        real = bk._fsync_path
        state = {"published": False}
        real_publish = bk._publish

        def tracking_publish(staging, final):  # type: ignore[no-untyped-def]
            out = real_publish(staging, final)
            state["published"] = True
            return out

        def maybe_bad(path):  # type: ignore[no-untyped-def]
            if state["published"]:
                raise OSError("induced post-publish fsync failure")
            return real(path)

        monkeypatch.setattr(bk, "_publish", tracking_publish)
        monkeypatch.setattr(bk, "_fsync_path", maybe_bad)

        with pytest.raises(bk.BackupDurabilityError) as excinfo:
            bk.backup_data_dir(populated_dir, settings=_Settings())
        assert len(_published_sets(populated_dir)) == 1, "a published set must not be withdrawn"
        assert excinfo.value.set_dir == _published_sets(populated_dir)[0]

    def test_fsync_covers_files_and_directories_bottom_up(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        synced: list[Path] = []
        real = bk._fsync_path

        def spy(path):  # type: ignore[no-untyped-def]
            synced.append(Path(path))
            return real(path)

        monkeypatch.setattr(bk, "_fsync_path", spy)
        outcome = bk.backup_data_dir(populated_dir, settings=_Settings())

        names = [p.name for p in synced]
        assert "graph.db" in names, "sqlite destinations were not fsynced"
        assert "index.bin" in names, "copytree output was not fsynced"
        dirs = [p for p in synced if p.is_dir()]
        assert dirs, "no directory was fsynced"
        assert outcome.set_dir.parent in [Path(p) for p in synced], (
            "the destination parent was not fsynced after the rename"
        )

    def test_leftover_staging_is_reported_and_not_deleted(
        self, populated_dir: Path
    ) -> None:
        """A crashed run's staging belongs to that run, not to this one."""
        leftover = populated_dir / ".backup-staging.CRASHED"
        leftover.mkdir()
        (leftover / "partial.db").write_text("half a backup", encoding="utf-8")

        outcome = bk.backup_data_dir(populated_dir, settings=_Settings())
        assert leftover.is_dir(), "another run's staging was deleted"
        assert (leftover / "partial.db").is_file()
        assert leftover in outcome.leftover_stagings
        assert len(_published_sets(populated_dir)) == 1

    def test_crash_before_publish_leaves_no_set(self, populated_dir: Path) -> None:
        """SIGKILL mid-run: staging may remain, a published set may not."""
        script = tmp_script = populated_dir.parent / "kill_mid_backup.py"
        tmp_script.write_text(
            textwrap.dedent(
                f"""
                import os, sys
                sys.path.insert(0, {str(Path(bk.__file__).parents[2])!r})
                from opencrab.stores import backup as bk

                real = bk.backup_sqlite
                calls = []

                def killer(src, dst, **kw):
                    calls.append(src)
                    out = real(src, dst, **kw)
                    if len(calls) == 2:
                        os.kill(os.getpid(), 9)
                    return out

                bk.backup_sqlite = killer

                class S:
                    vector_db_file = "vectors.db"
                    local_data_dir = ""

                bk.backup_data_dir({str(populated_dir)!r}, settings=S())
                """
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(script)], capture_output=True, text=True
        )
        assert proc.returncode < 0, f"the run was not killed: rc={proc.returncode} {proc.stderr}"
        assert _published_sets(populated_dir) == [], "a set was published by a killed run"


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


class TestLocking:
    def test_backup_times_out_when_another_process_holds_write_lock(
        self, populated_dir: Path, tmp_path: Path
    ) -> None:
        holder_script = tmp_path / "hold_lock.py"
        holder_script.write_text(
            textwrap.dedent(
                f"""
                import sys, time
                sys.path.insert(0, {str(Path(bk.__file__).parents[2])!r})
                from opencrab.locking import acquire_file_lock
                fh = acquire_file_lock("write.lock", {str(populated_dir)!r})
                print("held", flush=True)
                time.sleep(30)
                """
            ),
            encoding="utf-8",
        )
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(holder_script)], stdout=subprocess.PIPE, text=True
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "held"
            with pytest.raises(TimeoutError):
                bk.backup_data_dir(populated_dir, settings=_Settings(), lock_timeout=1.0)
            assert _published_sets(populated_dir) == []
            assert _stagings(populated_dir) == []
        finally:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "opencrab.stores.backup", *args],
        capture_output=True,
        text=True,
        cwd=str(Path(bk.__file__).parents[2]),
    )


class TestCli:
    def test_list_writes_nothing(self, populated_dir: Path) -> None:
        proc = _run_cli("--data-dir", str(populated_dir), "--list")
        assert proc.returncode == 0, proc.stderr
        assert "vectors.db" in proc.stdout
        assert _published_sets(populated_dir) == []
        assert _stagings(populated_dir) == []

    def test_default_run_publishes_a_set(self, populated_dir: Path) -> None:
        proc = _run_cli("--data-dir", str(populated_dir))
        assert proc.returncode == 0, proc.stderr + proc.stdout
        s = _set_dir(populated_dir)
        assert (s / "vectors.db").is_file()

    def test_to_places_the_set_elsewhere(self, populated_dir: Path, tmp_path: Path) -> None:
        dest = tmp_path / "backups"
        dest.mkdir()
        proc = _run_cli("--data-dir", str(populated_dir), "--to", str(dest))
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert (_set_dir(dest) / "graph.db").is_file()
        assert _published_sets(populated_dir) == []

    def test_failure_exits_non_zero(self, populated_dir: Path) -> None:
        (populated_dir / "graph.db").write_bytes(b"not a database")
        proc = _run_cli("--data-dir", str(populated_dir))
        assert proc.returncode != 0
        assert _published_sets(populated_dir) == []

    def test_skipped_and_unverified_and_excluded_still_exit_zero(
        self, populated_dir: Path
    ) -> None:
        """Those three are the declared contract, not failures."""
        (populated_dir / "billing.db").unlink()  # -> skipped
        proc = _run_cli("--data-dir", str(populated_dir))
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert "skipped" in proc.stdout
        assert "unverified" in proc.stdout
        assert "excluded" in proc.stdout


# ---------------------------------------------------------------------------
# sqlite-vec virtual tables survive the page-level copy
# ---------------------------------------------------------------------------


class TestSqliteVec:
    def test_virtual_table_database_round_trips(self, tmp_path: Path) -> None:
        sqlite_vec = pytest.importorskip("sqlite_vec")
        src = tmp_path / "vectors.db"
        conn = sqlite3.connect(str(src))
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[4])")
            conn.execute("INSERT INTO v (rowid, embedding) VALUES (1, ?)", (b"\x00" * 16,))
            conn.commit()
        finally:
            conn.close()

        dst = tmp_path / "copy.db"
        bk.backup_sqlite(src, dst, deadline=time.monotonic() + 30)
        bk.verify_sqlite(dst)

        out = sqlite3.connect(str(dst))
        try:
            out.enable_load_extension(True)
            sqlite_vec.load(out)
            out.enable_load_extension(False)
            assert out.execute("SELECT count(*) FROM v").fetchone()[0] == 1
        finally:
            out.close()
