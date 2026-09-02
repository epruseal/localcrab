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
    # A tiny page cache (16 KiB in the negative form) forces dirty pages out
    # of memory and into the database file before any commit.
    conn.execute("PRAGMA cache_size=-16")
    conn.execute("BEGIN IMMEDIATE")
    # An UPDATE, not an INSERT. Inserted rows land on NEW pages past the old
    # end of file, and SQLite simply ignores pages beyond the header's page
    # count -- a journal-less copy of that still reads as the committed state,
    # which would make this whole test prove nothing. An UPDATE overwrites
    # EXISTING pages, so the pre-image survives only in the rollback journal.
    conn.execute("UPDATE t SET v = 'UNCOMMITTED-' || v")
    # Die without unwinding: the rollback journal is left behind with no
    # owning connection, which is what makes it a *hot* journal.
    os._exit(1)
    """
)

#: Big enough that the UPDATE above cannot fit in the page cache and must
#: spill into the database file.
_HOT_JOURNAL_ROWS = 4000


@pytest.fixture
def hot_journal_db(tmp_path: Path) -> tuple[Path, list[tuple[int, str]]]:
    """A database with a real hot rollback journal, plus its committed rows."""
    db = tmp_path / "hot.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (id, v) VALUES (?, ?)",
            [(i, "committed-" + "y" * 2000) for i in range(_HOT_JOURNAL_ROWS)],
        )
        conn.commit()
    finally:
        conn.close()
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


def _uncommitted_rows(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT count(*) FROM t WHERE v LIKE 'UNCOMMITTED-%'").fetchone()[0]
    finally:
        conn.close()


class TestHotJournal:
    def test_raw_copy_without_journal_keeps_uncommitted_data(
        self, hot_journal_db: tuple[Path, list[tuple[int, str]]], tmp_path: Path
    ) -> None:
        """Control group: the damage is caused by not copying the journal.

        Two copies differing ONLY in whether the rollback journal came along.
        Both are taken BEFORE anything opens the source read-write, because
        that open is what makes SQLite recover the journal and would destroy
        the state under test.

        Note what the bad copy looks like: it passes ``integrity_check``. The
        structure is fine; the CONTENTS are a transaction that was never
        committed. That is issue #128's "believed to have a backup" precisely
        -- nothing warns you until you restore it.
        """
        db, committed = hot_journal_db
        journal = db.with_name("hot.db-journal")

        raw_only = tmp_path / "raw_only.db"
        shutil.copy2(db, raw_only)

        raw_with_journal = tmp_path / "raw_with_journal.db"
        shutil.copy2(db, raw_with_journal)
        shutil.copy2(journal, raw_with_journal.with_name("raw_with_journal.db-journal"))

        # With the journal, SQLite rolls back and the copy IS the committed state.
        assert _uncommitted_rows(raw_with_journal) == 0
        assert _rows(raw_with_journal) == committed

        # Without it -- which is exactly what shutil.copy2 of a live database
        # produced before this fix -- uncommitted rows survive into the backup.
        assert _uncommitted_rows(raw_only) > 0, (
            "fixture precondition failed: the uncommitted UPDATE did not spill "
            "into the database file, so this test would prove nothing"
        )
        assert _rows(raw_only) != committed

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
        assert _uncommitted_rows(dst) == 0, "the backup kept a transaction that never committed"
        assert _rows(dst) == committed


# ---------------------------------------------------------------------------
# 3-2  Backup while another connection writes
# ---------------------------------------------------------------------------


class TestVerifySqlite:
    """verify_sqlite is the guarantee this change advertises, so pin it directly.

    Every other test reached it indirectly or injected a fake failure, which
    meant replacing the whole function body with `return` left the suite
    green. A backup is only "verified" because this function refuses bad
    input, so that refusal needs a test of its own.
    """

    def test_accepts_a_sound_database(self, tmp_path: Path) -> None:
        db = tmp_path / "sound.db"
        _make_db(db, rows=3)
        bk.verify_sqlite(db)  # must not raise

    def test_rejects_a_file_that_is_not_a_database(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus.db"
        bogus.write_bytes(b"this is not a sqlite database at all")
        with pytest.raises(bk.BackupError):
            bk.verify_sqlite(bogus)

    def test_rejects_a_corrupted_database(self, tmp_path: Path) -> None:
        """Structurally damaged, not merely foreign: integrity_check must catch it."""
        db = tmp_path / "corrupt.db"
        # Large enough that the table spans many pages, so overwriting the
        # middle of the file is certain to land on pages actually in use.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            conn.executemany(
                "INSERT INTO t (id, v) VALUES (?, ?)",
                [(i, "x" * 400) for i in range(2000)],
            )
            conn.commit()
        finally:
            conn.close()
        assert _integrity(db) == "ok", "fixture precondition: the database starts sound"

        data = bytearray(db.read_bytes())
        # Everything from a quarter in to three quarters through, leaving the
        # header intact so the file still opens as a database.
        data[len(data) // 4 : len(data) * 3 // 4] = b"\xde\xad\xbe\xef" * (
            (len(data) * 3 // 4 - len(data) // 4) // 4
        )
        db.write_bytes(bytes(data))

        with pytest.raises(bk.BackupError) as excinfo:
            bk.verify_sqlite(db)
        message = str(excinfo.value)
        assert "integrity_check" in message or "does not open" in message, message

    def test_rejects_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(bk.BackupError):
            bk.verify_sqlite(tmp_path / "absent.db")


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

        class ObserverError(RuntimeError):
            pass

        def observer(status: int, remaining: int, total: int) -> None:
            raise ObserverError("stop")

        with pytest.raises(ObserverError):
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

            budget = 1.0
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                bk.backup_sqlite(src, dst, deadline=time.monotonic() + budget)
            elapsed = time.monotonic() - started
            # budget + 2s, not a loose ceiling: the source connection's default
            # 5s busy timeout used to swallow the deadline entirely (a 0.2s
            # deadline measured 5.06s). A 20s tolerance would not have caught
            # that, so it would not catch it coming back.
            assert elapsed < budget + 2, (
                f"backup did not honour its deadline (budget {budget}s, took {elapsed:.2f}s)"
            )
        finally:
            holder.close()


# ---------------------------------------------------------------------------
# 3-3  The canonical inventory (#123)
# ---------------------------------------------------------------------------


class TestInventory:
    def test_vector_db_is_a_target(self) -> None:
        """#123: vectors.db was missing from the backup list entirely.

        Asserted against a settings object that names NO vector file, so the
        default target has to come from the fixed inventory itself. Checking
        it with the usual _Settings() would pass even if the fixed list lost
        the entry, because the settings path would silently re-add it.
        """

        class _NoVectorSettings:
            local_data_dir = ""

        labels = {t.label for t in bk.local_data_dir_inventory(_NoVectorSettings())}
        assert "vectors.db" in labels
        assert {t.label for t in bk.local_data_dir_inventory(_Settings())} >= labels

    def test_core_stores_are_targets(self) -> None:
        labels = {t.label for t in bk.local_data_dir_inventory(_Settings())}
        assert {"opencrab.db", "graph.db", "doc_store.db", "billing.db"} <= labels

    @staticmethod
    def _data_dir_artifacts(source: str) -> set[str]:
        """Store filenames joined onto LOCAL_DATA_DIR in ``source``.

        A HEURISTIC, not a proof. It covers the three idioms factory.py
        actually uses -- ``os.path.join(..., "x.db")``, ``Path(...) / "x.db"``
        and ``.joinpath("x.db")``. A name reached through a constant, string
        concatenation or another indirection would slip past it. Closing that
        needs real static analysis, which is out of scope here; the guard is
        worth having anyway because it catches the ordinary way a new store
        gets added, which is how vectors.db went missing (#123).
        """
        import re

        found = set(re.findall(r"local_data_dir[,)]\s*[\"']([^\"']+)[\"']", source))
        found |= set(re.findall(r"local_data_dir\)?\s*/\s*[\"']([^\"']+)[\"']", source))
        found |= set(re.findall(r"local_data_dir\)?\s*\.joinpath\(\s*[\"']([^\"']+)[\"']", source))
        return found

    def test_inventory_covers_every_factory_data_dir_artifact(self) -> None:
        """Guard against #123 recurring: a new store must update the inventory.

        ``factory.py`` is the single place that joins a store file onto
        LOCAL_DATA_DIR. Every name it joins there must appear in the
        inventory, or a future store silently drops out of the backup the way
        ``vectors.db`` did.
        """
        factory_src = Path(bk.__file__).with_name("factory.py").read_text(encoding="utf-8")
        joined = self._data_dir_artifacts(factory_src)
        assert joined, "could not extract any data-dir artifact from factory.py"

        known = {t.label for t in bk.local_data_dir_inventory(_Settings())}
        missing = joined - known
        assert not missing, (
            f"factory.py writes these under LOCAL_DATA_DIR but the inventory omits them: "
            f"{sorted(missing)}"
        )

    def test_artifact_extractor_catches_each_supported_idiom(self) -> None:
        """The guard above is only worth having if it actually detects a new store.

        Feeds the extractor a factory-shaped source for each idiom in use. An
        extractor that quietly matched nothing would make the guard above pass
        forever.
        """
        for idiom in (
            'db_path = os.path.join(settings.local_data_dir, "future.db")',
            'db_path = Path(settings.local_data_dir) / "future.db"',
            'db_path = Path(settings.local_data_dir).joinpath("future.db")',
        ):
            assert "future.db" in self._data_dir_artifacts(idiom), idiom
            known = {t.label for t in bk.local_data_dir_inventory(_Settings())}
            assert "future.db" not in known

    def test_renamed_vector_file_is_followed_and_default_still_covered(self) -> None:
        targets = bk.local_data_dir_inventory(_Settings(vector_db_file="renamed.db"))
        labels = {t.label for t in targets}
        assert "renamed.db" in labels, "VECTOR_DB_FILE rename is not followed"
        assert "vectors.db" in labels, "settings must only ADD targets, never remove them"

    def test_unreadable_configuration_fails_instead_of_dropping_targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A settings failure must not silently shrink the backup.

        Swallowing it and continuing with the fixed list would omit a renamed
        VECTOR_DB_FILE while still reporting success, which is issue #123's
        failure mode reintroduced from the inside.
        """
        import opencrab.config as config

        def boom() -> object:
            raise ValueError("induced configuration error")

        monkeypatch.setattr(config, "get_settings", boom)

        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=2)

        with pytest.raises(bk.BackupError) as excinfo:
            bk.local_data_dir_inventory()
        message = str(excinfo.value)
        assert "induced configuration error" in message
        assert "vector" in message.lower(), "the message must name what would go missing"

        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(d)
        assert _published_sets(d) == [], "a set was published despite an unusable inventory"

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
        """.backup() destinations are standalone; sidecars would mislead.

        The WAL connection is held OPEN across the backup on purpose. Closing
        the last connection checkpoints and removes the sidecars, so a test
        that backs up afterwards proves nothing: there would be no -wal to
        exclude. The precondition below fails loudly if the sidecar is absent.
        """
        (populated_dir / "graph.db").unlink()
        _make_db(populated_dir / "graph.db", rows=4, wal=True)

        holder = sqlite3.connect(str(populated_dir / "graph.db"))
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("INSERT INTO t (id, v) VALUES (99, 'uncheckpointed')")
            holder.commit()
            assert (populated_dir / "graph.db-wal").is_file(), (
                "fixture precondition: no -wal exists, so this test would prove nothing"
            )

            bk.backup_data_dir(populated_dir, settings=_Settings())
        finally:
            holder.close()

        s = _set_dir(populated_dir)
        assert not list(s.glob("*-wal")), "a -wal sidecar was copied into the set"
        assert not list(s.glob("*-shm")), "a -shm sidecar was copied into the set"
        # The copy must still be complete: .backup() reads through the WAL, so
        # the uncheckpointed row has to be present without its sidecar.
        assert (99, "uncheckpointed") in _rows(s / "graph.db")

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

    def test_a_corrupt_chroma_catalog_aborts_the_backup(self, tmp_path: Path) -> None:
        """Known-bad is not the same as not-fully-verified.

        A failing integrity_check on the copied catalog is positive evidence
        the copy is unusable. Publishing it as a success let the migration
        overwrite the live store next, leaving the operator with a backup
        already known to be broken.
        """
        d = tmp_path / "data"
        (d / "chroma").mkdir(parents=True)
        _make_db(d / "graph.db", rows=3)
        (d / "chroma" / "chroma.sqlite3").write_bytes(b"this is not a sqlite database")
        (d / "chroma" / "index.bin").write_bytes(b"vec")

        with pytest.raises(bk.BackupError) as excinfo:
            bk.backup_data_dir(d, settings=_Settings())
        assert "chroma" in str(excinfo.value)
        assert _published_sets(d) == [], "a set was published despite a known-bad catalog"
        assert _stagings(d) == []

    def test_a_sound_chroma_catalog_is_unverified_not_failed(self, tmp_path: Path) -> None:
        """Passing the catalog check still does not prove the segments agree."""
        d = tmp_path / "data"
        (d / "chroma").mkdir(parents=True)
        _make_db(d / "chroma" / "chroma.sqlite3", rows=2)
        (d / "chroma" / "index.bin").write_bytes(b"vec")

        outcome = bk.backup_data_dir(d, settings=_Settings())
        entry = next(e for e in outcome.entries if e.label == "chroma")
        assert entry.status == "unverified"
        assert "does not prove" in entry.note

    def test_a_chroma_directory_without_a_catalog_still_passes(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        (d / "chroma").mkdir(parents=True)
        (d / "chroma" / "index.bin").write_bytes(b"vec")

        outcome = bk.backup_data_dir(d, settings=_Settings())
        entry = next(e for e in outcome.entries if e.label == "chroma")
        assert entry.status == "unverified"
        assert "no chroma.sqlite3" in entry.note
        assert (_set_dir(d) / "chroma" / "index.bin").is_file()

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
        # The link's TARGET must not have been copied into the set. Checked by
        # walking the set without following links, so the assertion cannot be
        # satisfied by the link merely resolving.
        names = {
            f
            for _root, _dirs, files in os.walk(_set_dir(populated_dir), followlinks=False)
            for f in files
        }
        assert "secret.txt" not in names, "the symlink's target content was copied into the set"

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

    def test_symlinked_alias_keeps_its_own_restore_path(self, tmp_path: Path) -> None:
        """An aliased target still needs its own slot in the set.

        Deduping on the resolved source alone left only graph.db in the set,
        so restoring into an empty data directory produced no alias.db and
        the vector store could not reopen -- while the backup reported
        success. Two different destinations do not collide, so there is
        nothing to dedupe; the cost is copying the same bytes twice.
        """
        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=3)
        (d / "alias.db").symlink_to(d / "graph.db")

        outcome = bk.backup_data_dir(d, settings=_Settings(vector_db_file="alias.db"))
        s = _set_dir(d)

        assert (s / "graph.db").is_file()
        assert (s / "alias.db").is_file(), (
            "the configured alias path is missing, so this set cannot be restored"
        )
        # Both are real, independently openable copies, not links.
        assert not (s / "alias.db").is_symlink()
        assert _rows(s / "alias.db") == _rows(s / "graph.db")
        assert _integrity(s / "alias.db") == "ok"
        assert set(outcome.copied) == {str(d / "graph.db"), str(d / "alias.db")}

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

    def test_vector_file_configured_inside_a_directory_store_fails_loudly(
        self, tmp_path: Path
    ) -> None:
        """A pathological config must fail with a message that names the cause.

        VECTOR_DB_FILE pointing inside a directory store lands where that
        store's copytree already wrote. Aborting is right -- the alternative
        is overwriting part of a copied store -- but the operator needs to be
        told which target is misconfigured, not just shown a path collision.
        """
        d = tmp_path / "data"
        d.mkdir()
        (d / "chroma").mkdir()
        _make_db(d / "chroma" / "vectors.db", rows=2)

        with pytest.raises(bk.BackupError) as excinfo:
            bk.backup_data_dir(d, settings=_Settings(vector_db_file="chroma/vectors.db"))
        message = str(excinfo.value)
        assert "chroma/vectors.db" in message
        assert "another store" in message or "another target" in message, message
        assert _published_sets(d) == []
        assert _stagings(d) == []

    def test_symlinked_destination_itself_is_refused(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=2)
        outside = tmp_path / "outside"
        outside.mkdir()

        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(d, dest_dir=d / "missing", settings=_Settings())

        (d / "escape").symlink_to(outside)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(d, dest_dir=d / "escape", settings=_Settings())

    def test_destination_via_a_symlinked_ancestor_is_deliberately_allowed(
        self, tmp_path: Path
    ) -> None:
        """The destination is the operator's chosen location, not a threat.

        Reaching it through a symlinked ancestor is a normal layout (a backup
        volume mounted that way), so it is followed on purpose. This test
        exists so the behaviour is a documented decision rather than a silent
        one: containment is enforced INSIDE the set, not on where the operator
        pointed the tool. See test_target_inside_the_set_cannot_escape.
        """
        d = tmp_path / "data"
        d.mkdir()
        _make_db(d / "graph.db", rows=2)
        outside = tmp_path / "outside"
        outside.mkdir()
        (d / "volume").symlink_to(outside)
        (outside / "sub").mkdir()

        outcome = bk.backup_data_dir(d, dest_dir=d / "volume" / "sub", settings=_Settings())
        assert outcome.set_dir.resolve().is_relative_to(outside.resolve())
        assert (outcome.set_dir / "graph.db").is_file()

    def test_a_relative_symlink_inside_a_store_cannot_write_outside_the_set(
        self, tmp_path: Path
    ) -> None:
        """End-to-end containment, which the unit test below does NOT cover.

        test_target_inside_the_set_cannot_escape calls _require_contained
        directly with the right arguments, so it stays green even if the CALL
        SITE in _run_targets passes the wrong root. That mutation is not
        theoretical: a relative symlink resolves differently from the staging
        tree than from the data directory, so it lets a backup publish
        successfully while writing a file OUTSIDE the set.

        docs/link -> ../../foo escapes the data directory once copytree has
        preserved it inside staging, and routing VECTOR_DB_FILE through that
        link aims a destination at it.
        """
        d = tmp_path / "data"
        (d / "docs").mkdir(parents=True)
        (d / "docs" / "link").symlink_to(Path("..") / ".." / "foo")
        escape_target = tmp_path / "foo" / "sub"
        escape_target.mkdir(parents=True)
        _make_db(escape_target / "x.db", rows=2)
        assert (d / "docs" / "link" / "sub" / "x.db").is_file(), (
            "fixture precondition: the escaping source must be reachable through the link"
        )

        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(d, settings=_Settings(vector_db_file="docs/link/sub/x.db"))

        assert _published_sets(d) == []
        # The assertion that matters. Checking only that it raised would pass
        # for an unrelated reason; what must be true is that nothing was
        # written through the link to a path outside the backup set.
        assert not (escape_target / "backup").exists()
        assert sorted(p.name for p in escape_target.iterdir()) == ["x.db"], (
            "the backup wrote through the symlink to a path outside the set"
        )

    def test_target_inside_the_set_cannot_escape(self, tmp_path: Path) -> None:
        """The #212 invariant, checked against a root fixed by the caller.

        _require_contained is only meaningful when its root does not come
        from the path being checked. Here the staging root is fixed, so a
        destination resolving outside it must be refused.
        """
        staging = tmp_path / "staging"
        staging.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        bk._require_contained(staging / "inside", staging)  # contained: no raise

        (staging / "escape").symlink_to(outside)
        with pytest.raises(bk.BackupError):
            bk._require_contained(staging / "escape", staging)
        # An escaping ANCESTOR is caught too, because the root is independent.
        with pytest.raises(bk.BackupError):
            bk._require_contained(staging / "escape" / "deeper", staging)

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

    def test_publish_rename_failure_leaves_staging_and_no_set(
        self, populated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documented behaviour, now pinned.

        If the publishing rename itself fails, the staging directory survives
        on purpose: its name cannot be mistaken for a finished backup, its
        contents are intact and worth diagnosing, and the next run reports it.
        What must NOT survive is a published set.
        """

        def failing_publish(staging, final):  # type: ignore[no-untyped-def]
            raise OSError("induced rename failure")

        monkeypatch.setattr(bk, "_publish", failing_publish)
        with pytest.raises(OSError):
            bk.backup_data_dir(populated_dir, settings=_Settings())

        assert _published_sets(populated_dir) == []
        leftovers = _stagings(populated_dir)
        assert len(leftovers) == 1, f"staging should survive a failed publish, found {leftovers}"
        assert (leftovers[0] / "graph.db").is_file(), "the surviving staging should be intact"

    def test_directory_target_that_is_itself_a_symlink_is_announced(
        self, tmp_path: Path
    ) -> None:
        """Links inside a directory store are preserved; the store's own link is followed.

        That asymmetry is deliberate, so it has to be visible in the report
        rather than discovered during a restore.
        """
        d = tmp_path / "data"
        d.mkdir()
        real_docs = tmp_path / "elsewhere_docs"
        real_docs.mkdir()
        (real_docs / "nodes.json").write_text("{}", encoding="utf-8")
        (d / "docs").symlink_to(real_docs)

        outcome = bk.backup_data_dir(d, settings=_Settings())
        entry = next(e for e in outcome.entries if e.label == "docs")
        assert "symlink" in entry.note and str(real_docs) in entry.note, entry.note
        # It keeps its natural slot in the set: a restore must put it back at
        # docs/, not somewhere derived from where the operator moved it to.
        assert (outcome.set_dir / "docs" / "nodes.json").is_file()

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


def _run_cli(*args: str, data_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the CLI hermetically.

    LOCAL_DATA_DIR and VECTOR_DB_FILE are pinned in the child's environment:
    the inventory reads vector_db_file from real settings, so without this a
    developer's ambient configuration could change what the test observes.
    """
    env = dict(os.environ)
    env["VECTOR_DB_FILE"] = "vectors.db"
    if data_dir is not None:
        env["LOCAL_DATA_DIR"] = str(data_dir)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "opencrab.stores.backup", *args],
        capture_output=True,
        text=True,
        cwd=str(Path(bk.__file__).parents[2]),
        env=env,
    )


class TestCli:
    def test_list_writes_nothing(self, populated_dir: Path) -> None:
        proc = _run_cli("--data-dir", str(populated_dir), "--list", data_dir=populated_dir)
        assert proc.returncode == 0, proc.stderr
        assert "vectors.db" in proc.stdout
        assert _published_sets(populated_dir) == []
        assert _stagings(populated_dir) == []

    def test_default_run_publishes_a_set(self, populated_dir: Path) -> None:
        proc = _run_cli("--data-dir", str(populated_dir), data_dir=populated_dir)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        s = _set_dir(populated_dir)
        assert (s / "vectors.db").is_file()

    def test_to_places_the_set_elsewhere(self, populated_dir: Path, tmp_path: Path) -> None:
        dest = tmp_path / "backups"
        dest.mkdir()
        proc = _run_cli("--data-dir", str(populated_dir), "--to", str(dest), data_dir=populated_dir)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert (_set_dir(dest) / "graph.db").is_file()
        assert _published_sets(populated_dir) == []

    def test_failure_exits_non_zero(self, populated_dir: Path) -> None:
        (populated_dir / "graph.db").write_bytes(b"not a database")
        proc = _run_cli("--data-dir", str(populated_dir), data_dir=populated_dir)
        assert proc.returncode != 0
        assert _published_sets(populated_dir) == []

    def test_skipped_and_unverified_and_excluded_still_exit_zero(
        self, populated_dir: Path
    ) -> None:
        """Those three are the declared contract, not failures."""
        (populated_dir / "billing.db").unlink()  # -> skipped
        proc = _run_cli("--data-dir", str(populated_dir), data_dir=populated_dir)
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
