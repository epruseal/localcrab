"""Live backup (#510): the service keeps writing while a backup set is made.

The design under test: ``write.lock`` is held only while the files are
captured. WAL SQLite sources get a pinned read snapshot inside that window and
are copied after the lock is released. Each test that shows a property also
shows a control that loses it, so the test can fail for the right reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from opencrab.stores import backup as bk
from tests.test_backup_consistency import _make_db, _Settings

_ROOT = str(Path(bk.__file__).parents[2])


def _wal_db(path: Path, *, tables: tuple[str, ...] = ("t",), rows: int = 3000) -> None:
    """A WAL database big enough that a copy takes many 64-page steps."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for name in tables:
            conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, v TEXT)")
            conn.executemany(
                f"INSERT INTO {name} (id, v) VALUES (?, ?)",
                [(i, "x" * 2000) for i in range(rows)],
            )
        conn.commit()
    finally:
        conn.close()


def _count(path: Path, table: str = "t") -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def live_dir(tmp_path: Path) -> Path:
    """A data directory shaped like the service's: WAL stores, rollback opencrab.db."""
    d = tmp_path / "data"
    d.mkdir()
    _make_db(d / "opencrab.db", rows=3)
    _make_db(d / "billing.db", rows=3)
    for name in ("graph.db", "doc_store.db", "vectors.db"):
        _wal_db(d / name, rows=300)
    return d


@pytest.fixture
def big_live_dir(live_dir: Path) -> Path:
    """``live_dir`` with a graph.db of several MB: its copy takes dozens of 64-page steps."""
    conn = sqlite3.connect(str(live_dir / "graph.db"))
    try:
        conn.executemany(
            "INSERT INTO t (id, v) VALUES (?, ?)", [(10_000_000 + i, "x" * 2000) for i in range(4000)]
        )
        conn.commit()
    finally:
        conn.close()
    return live_dir


def _set_dir(dest: Path) -> Path:
    sets = [p for p in dest.iterdir() if p.is_dir() and p.name.startswith("backup.")]
    assert len(sets) == 1, sets
    return sets[0]


def _stagings(dest: Path) -> list[Path]:
    return [p for p in dest.iterdir() if p.name.startswith(".backup-staging.")]


_PAIR_WRITER = textwrap.dedent(
    """
    import os, sqlite3, sys, time
    sys.path.insert(0, {root!r})
    from opencrab.locking import write_lock
    d, mode, stop = sys.argv[1], sys.argv[2], sys.argv[3]
    a = sqlite3.connect(os.path.join(d, "graph.db"), timeout=10)
    b = sqlite3.connect(os.path.join(d, "doc_store.db"), timeout=10)
    for c in (a, b):
        c.execute("PRAGMA journal_mode=WAL")
    n = 1000
    def one():
        global n
        n += 1
        a.execute("INSERT INTO t (id, v) VALUES (?, 'w')", (n,)); a.commit()
        time.sleep(0.03)  # the gap between the two stores
        b.execute("INSERT INTO t (id, v) VALUES (?, 'w')", (n,)); b.commit()
    print("ready", flush=True)
    while not os.path.exists(stop):
        if mode == "locked":
            with write_lock(d, timeout=30):
                one()
            time.sleep(0.002)
        else:
            one()
    """
).format(root=_ROOT)


@contextmanager
def _writer(script_dir: Path, data_dir: Path, mode: str) -> Iterator[None]:
    script = script_dir / f"writer_{mode}.py"
    script.write_text(_PAIR_WRITER, encoding="utf-8")
    stop = script_dir / f"stop_{mode}"
    stop.unlink(missing_ok=True)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script), str(data_dir), mode, str(stop)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        yield
    finally:
        stop.write_text("x")
        proc.wait(timeout=30)


def _ids(path: Path) -> set[int]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM t WHERE id > 1000")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T1 / T8: a pinned snapshot survives a writer that never takes write.lock
# ---------------------------------------------------------------------------


def _rows(path_or_conn: Any) -> set[tuple[int, str]]:
    """Every ``(id, v)`` row of table ``t`` in a database file or on a given connection."""
    if isinstance(path_or_conn, sqlite3.Connection):
        return {(int(i), str(v)) for i, v in path_or_conn.execute("SELECT id, v FROM t")}
    conn = sqlite3.connect(str(path_or_conn))
    try:
        return {(int(i), str(v)) for i, v in conn.execute("SELECT id, v FROM t")}
    finally:
        conn.close()


def _assert_copy_equals_pin(expected: set[tuple[int, str]], copy: Path) -> None:
    assert _rows(copy) == expected, "the copy differs from the rows visible at the pin"


class TestPinnedSnapshot:
    def test_copy_completes_and_matches_the_pin_while_a_writer_keeps_committing(
        self, tmp_path: Path, big_live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live_dir = big_live_dir
        marks: dict[str, int] = {}
        pinned: dict[str, set[tuple[int, str]]] = {}
        real = bk.backup_sqlite

        def spy(src: Path, dst: Path, **kw: Any) -> None:
            if kw.get("src_conn") is not None and src.name == "graph.db":
                # Read on the pinned connection itself: this is the pin-time row set.
                pinned["rows"] = _rows(kw["src_conn"])
                marks["before"] = _count(src, "t")
                kw["_on_step"] = lambda *_a: time.sleep(0.004)
                real(src, dst, **kw)
                marks["after"] = _count(src, "t")
                return
            real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", spy)
        with _writer(tmp_path, live_dir, "unlocked"):
            bk.backup_data_dir(live_dir, settings=_Settings(), lock_timeout=5.0)
        copy = _set_dir(live_dir) / "graph.db"
        assert marks["after"] > marks["before"], "the writer did not run during the copy"
        _assert_copy_equals_pin(pinned["rows"], copy)
        # A set made while the service was writing passes the existing verifier.
        assert "graph.db" in bk.verify_backup_set(_set_dir(live_dir))["sqlite_integrity_checked"]
        conn = sqlite3.connect(str(copy))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    @pytest.mark.parametrize("damage", ["drop_row", "change_value"])
    def test_control_the_row_comparison_detects_a_damaged_copy(
        self, tmp_path: Path, big_live_dir: Path, damage: str
    ) -> None:
        """The comparison must fail for a missing row and for a changed value."""
        bk.backup_data_dir(big_live_dir, settings=_Settings(), lock_timeout=5.0)
        copy = _set_dir(big_live_dir) / "graph.db"
        expected = _rows(copy)
        _assert_copy_equals_pin(expected, copy)  # undamaged copy passes
        conn = sqlite3.connect(str(copy))
        try:
            if damage == "drop_row":
                conn.execute("DELETE FROM t WHERE id = (SELECT min(id) FROM t)")
            else:
                conn.execute("UPDATE t SET v = 'changed' WHERE id = (SELECT min(id) FROM t)")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(AssertionError):
            _assert_copy_equals_pin(expected, copy)

    def test_control_without_the_pin_the_same_copy_cannot_finish(
        self, tmp_path: Path, big_live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remove the pin and the writer restarts the copy until the deadline."""
        live_dir = big_live_dir
        real_open = bk._SnapshotPins.open_snapshot

        def never_pin(self: Any, source: Path) -> Any:
            mode, snap = real_open(self, source)
            if snap is not None:
                self.release(snap.connection)
            return mode, None

        monkeypatch.setattr(bk._SnapshotPins, "open_snapshot", never_pin)
        real = bk.backup_sqlite

        def slow(src: Path, dst: Path, **kw: Any) -> None:
            kw["_on_step"] = lambda *_a: time.sleep(0.004)
            real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", slow)
        with _writer(tmp_path, live_dir, "unlocked"), pytest.raises(TimeoutError):
            bk.backup_data_dir(live_dir, settings=_Settings(), lock_timeout=1.5)

    def test_a_snapshot_copy_may_outlast_the_lock_timeout(
        self, big_live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = bk.backup_sqlite
        spent: dict[str, float] = {}

        def slow(src: Path, dst: Path, **kw: Any) -> None:
            if src.name == "graph.db":
                kw["_on_step"] = lambda *_a: time.sleep(0.04)
            t = time.monotonic()
            try:
                real(src, dst, **kw)
            finally:
                spent[src.name] = time.monotonic() - t

        monkeypatch.setattr(bk, "backup_sqlite", slow)
        # ~8 MB is ~30 steps of 64 pages; at 40 ms each the copy takes longer than the
        # 1 s lock timeout, while the snapshot budget (bytes at 1 MiB/s) is ~8 s.
        bk.backup_data_dir(big_live_dir, settings=_Settings(), lock_timeout=1.0)
        assert spent["graph.db"] > 1.0, "the copy was too fast to show the point"

    def test_a_locked_copy_is_still_bounded_by_the_lock_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "data"
        d.mkdir()
        conn = sqlite3.connect(str(d / "graph.db"))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 2000) for i in range(4000)])
        conn.commit()
        conn.close()
        real = bk.backup_sqlite

        def slow(src: Path, dst: Path, **kw: Any) -> None:
            kw["_on_step"] = lambda *_a: time.sleep(0.04)
            real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", slow)
        with pytest.raises(TimeoutError):
            bk.backup_data_dir(d, settings=_Settings(), lock_timeout=1.0)

    def test_snapshot_budget_follows_the_snapshot_size_not_the_main_file_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "data"
        d.mkdir()
        src = d / "graph.db"
        keeper = sqlite3.connect(str(src), isolation_level=None)
        try:
            keeper.execute("PRAGMA journal_mode=WAL")
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            keeper.execute("BEGIN")
            keeper.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 2000) for i in range(6000)])
            keeper.execute("COMMIT")
            assert src.stat().st_size < 1 << 20, "precondition: the main file must stay small"
            budgets: list[float] = []
            real = bk.backup_sqlite

            def spy(s: Path, dst: Path, **kw: Any) -> None:
                if kw.get("src_conn") is not None:
                    budgets.append(kw["deadline"] - time.monotonic())
                real(s, dst, **kw)

            monkeypatch.setattr(bk, "backup_sqlite", spy)
            bk.backup_data_dir(d, settings=_Settings(), lock_timeout=1.0)
        finally:
            keeper.close()
        assert budgets and budgets[0] > 8.0, budgets


# ---------------------------------------------------------------------------
# T10: a snapshot copy is flushed in steps, not by one fsync at the end
# ---------------------------------------------------------------------------


class TestPacedFlush:
    @staticmethod
    def _count_flushes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Python-level ``os.fsync`` calls made while a pinned copy runs."""
        seen = {"during_copy": 0}
        real_fsync = os.fsync
        real_copy = bk.backup_sqlite
        inside = {"on": False}

        def fsync(fd: int) -> None:
            if inside["on"]:
                seen["during_copy"] += 1
            real_fsync(fd)

        def copy(src: Path, dst: Path, **kw: Any) -> None:
            inside["on"] = kw.get("src_conn") is not None
            try:
                real_copy(src, dst, **kw)
            finally:
                inside["on"] = False

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(bk, "backup_sqlite", copy)
        return seen

    def test_a_pinned_copy_is_flushed_every_chunk_and_stays_correct(
        self, big_live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._count_flushes(monkeypatch)
        monkeypatch.setattr(bk, "_COPY_FLUSH_BYTES", 256 * 1024)
        bk.backup_data_dir(big_live_dir, settings=_Settings(), lock_timeout=5.0)
        # graph.db alone is several MB: far more than one chunk.
        assert seen["during_copy"] >= 10, seen
        assert _count(_set_dir(big_live_dir) / "graph.db") == _count(big_live_dir / "graph.db")
        bk.verify_backup_set(_set_dir(big_live_dir))

    def test_control_without_pacing_the_copy_makes_no_flush_calls(
        self, big_live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._count_flushes(monkeypatch)
        monkeypatch.setattr(bk, "_COPY_FLUSH_BYTES", 0)
        bk.backup_data_dir(big_live_dir, settings=_Settings(), lock_timeout=5.0)
        assert seen["during_copy"] == 0, seen

    def test_a_failing_flush_aborts_the_run_and_leaves_no_set(
        self, tmp_path: Path, big_live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        real_copy = bk.backup_sqlite

        def failing_copy(src: Path, dst: Path, **kw: Any) -> None:
            if kw.get("src_conn") is not None:
                monkeypatch.setattr(os, "fsync", _raise_io)
            real_copy(src, dst, **kw)

        def _raise_io(fd: int) -> None:
            raise OSError("injected flush failure")

        monkeypatch.setattr(bk, "backup_sqlite", failing_copy)
        monkeypatch.setattr(bk, "_COPY_FLUSH_BYTES", 256 * 1024)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(big_live_dir, dest, settings=_Settings(), lock_timeout=5.0)
        assert list(dest.iterdir()) == []


# ---------------------------------------------------------------------------
# T2: the lock really is free while the snapshots are copied
# ---------------------------------------------------------------------------

_TRY_LOCK = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from opencrab.locking import acquire_file_lock, release_file_lock
    try:
        fh = acquire_file_lock("write.lock", sys.argv[1], timeout=1.5)
    except TimeoutError:
        print("busy")
    else:
        release_file_lock(fh)
        print("free")
    """
).format(root=_ROOT)


def _lock_state(tmp_path: Path, data_dir: Path) -> str:
    script = tmp_path / "try_lock.py"
    script.write_text(_TRY_LOCK, encoding="utf-8")
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(data_dir)], capture_output=True, text=True, timeout=60
    )
    return out.stdout.strip()


class TestLockWindow:
    def test_write_lock_is_free_during_the_snapshot_copy_and_held_during_a_locked_copy(
        self, tmp_path: Path, live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}
        real = bk.backup_sqlite

        def spy(src: Path, dst: Path, **kw: Any) -> None:
            kind = "snapshot" if kw.get("src_conn") is not None else "locked"
            seen.setdefault(f"{kind}:{src.name}", _lock_state(tmp_path, live_dir))
            real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", spy)
        bk.backup_data_dir(live_dir, settings=_Settings())
        assert seen["snapshot:graph.db"] == "free"
        assert seen["snapshot:doc_store.db"] == "free"
        assert seen["snapshot:vectors.db"] == "free"
        # Controls: a rollback-journal file and billing.db are copied inside the window.
        assert seen["locked:opencrab.db"] == "busy"
        assert seen["locked:billing.db"] == "busy"

    def test_manifest_records_the_lock_window(self, live_dir: Path) -> None:
        out = bk.backup_data_dir(live_dir, settings=_Settings())
        lock = out.manifest["lock"]
        assert set(lock) == {"wait_seconds", "held_seconds", "acquired_at", "released_at"}
        assert 0 <= lock["held_seconds"] < 30


# ---------------------------------------------------------------------------
# T3: two stores written under write.lock come out aligned
# ---------------------------------------------------------------------------


class TestCrossStoreAlignment:
    def test_writers_that_hold_write_lock_never_leave_the_two_stores_apart(
        self, tmp_path: Path, live_dir: Path
    ) -> None:
        with _writer(tmp_path, live_dir, "locked"):
            for i in range(4):
                dest = tmp_path / f"dest{i}"
                dest.mkdir()
                bk.backup_data_dir(live_dir, dest, settings=_Settings(), lock_timeout=30)
                s = _set_dir(dest)
                assert _ids(s / "graph.db") == _ids(s / "doc_store.db"), f"round {i}"
        assert _ids(live_dir / "graph.db"), "the writer never ran"

    def test_control_writers_that_skip_write_lock_can_leave_the_stores_apart(
        self, tmp_path: Path, live_dir: Path
    ) -> None:
        """Shows the check above can fail: without the lock the two pins straddle a commit gap."""
        with _writer(tmp_path, live_dir, "unlocked"):
            for i in range(12):
                dest = tmp_path / f"cdest{i}"
                dest.mkdir()
                bk.backup_data_dir(live_dir, dest, settings=_Settings(), lock_timeout=30)
                s = _set_dir(dest)
                if _ids(s / "graph.db") != _ids(s / "doc_store.db"):
                    return
        pytest.fail("an unlocked writer never produced a misaligned set; the control proves nothing")


# ---------------------------------------------------------------------------
# T4 / T5: the manifest and the verifier
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_matches_the_files_and_names_no_absolute_path(
        self, tmp_path: Path, live_dir: Path
    ) -> None:
        external = tmp_path / "elsewhere"
        external.mkdir()
        _make_db(external / "extvec.db", rows=2)
        out = bk.backup_data_dir(
            live_dir, settings=_Settings(vector_db_file=str(external / "extvec.db"))
        )
        s = _set_dir(live_dir)
        text = (s / bk.MANIFEST_NAME).read_text(encoding="utf-8")
        assert json.loads(text) == json.loads(json.dumps(out.manifest))
        assert str(tmp_path) not in text
        assert str(Path.home()) not in text
        for rel, meta in out.manifest["files"].items():
            f = s / rel
            assert f.stat().st_size == meta["size"]
            assert bk._hash_file(f) == meta["sha256"]
        labels = {e["label"] for e in out.manifest["entries"]}
        assert "extvec.db" in labels
        by = {e["label"]: e for e in out.manifest["entries"]}
        assert by["graph.db"]["capture"] == "wal_snapshot"
        assert by["opencrab.db"]["capture"] == "copied_under_lock"
        assert by["billing.db"]["mcp_write_tools_hold_write_lock"] is False
        assert by["graph.db"]["mcp_write_tools_hold_write_lock"] is True

    def test_manifest_is_not_listed_in_its_own_files(self, live_dir: Path) -> None:
        out = bk.backup_data_dir(live_dir, settings=_Settings())
        assert bk.MANIFEST_NAME not in out.manifest["files"]


class TestVerify:
    @pytest.fixture
    def made(self, live_dir: Path) -> Path:
        bk.backup_data_dir(live_dir, settings=_Settings())
        return _set_dir(live_dir)

    def test_a_sound_set_verifies(self, made: Path) -> None:
        report = bk.verify_backup_set(made)
        assert report["unlisted"] == []
        assert "graph.db" in report["sqlite_integrity_checked"]

    def test_verify_does_not_touch_the_set(self, made: Path) -> None:
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in made.iterdir()}
        bk.verify_backup_set(made)
        assert before == {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in made.iterdir()}
        assert not any(p.name.endswith(("-wal", "-shm", "-journal")) for p in made.iterdir())

    def test_one_flipped_byte_is_named(self, made: Path) -> None:
        f = made / "graph.db"
        raw = bytearray(f.read_bytes())
        raw[-1] ^= 0xFF
        f.write_bytes(bytes(raw))
        with pytest.raises(bk.BackupError, match=r"graph\.db: sha256 differs") as excinfo:
            bk.verify_backup_set(made)
        # Only the damaged file is named: files that were not touched stay out of the message.
        for untouched in ("doc_store.db", "vectors.db", "opencrab.db", "billing.db"):
            assert untouched not in str(excinfo.value)

    def test_a_deleted_file_is_named(self, made: Path) -> None:
        (made / "vectors.db").unlink()
        with pytest.raises(bk.BackupError, match=r"vectors\.db: missing"):
            bk.verify_backup_set(made)

    def test_an_extra_file_is_reported_not_failed(self, made: Path) -> None:
        (made / "apply-state.db").write_bytes(b"other tool state")
        assert bk.verify_backup_set(made)["unlisted"] == ["apply-state.db"]

    def test_unreadable_and_unknown_manifests_fail(self, made: Path) -> None:
        manifest = made / bk.MANIFEST_NAME
        good = manifest.read_text(encoding="utf-8")
        manifest.write_text("{ not json", encoding="utf-8")
        with pytest.raises(bk.BackupError, match="cannot be read"):
            bk.verify_backup_set(made)
        doc = json.loads(good)
        doc["format"] = 99
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(bk.BackupError, match="unsupported format"):
            bk.verify_backup_set(made)
        manifest.unlink()
        with pytest.raises(bk.BackupError, match="missing"):
            bk.verify_backup_set(made)

    def test_a_manifest_path_that_escapes_the_set_is_rejected(self, made: Path) -> None:
        manifest = made / bk.MANIFEST_NAME
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        doc["files"]["../outside"] = {"size": 1, "sha256": "0"}
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(bk.BackupError, match="unsafe"):
            bk.verify_backup_set(made)

    def test_a_symlink_is_recorded_and_not_followed(self, tmp_path: Path, live_dir: Path) -> None:
        (live_dir / "docs").mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (live_dir / "docs" / "link").symlink_to(outside)
        out = bk.backup_data_dir(live_dir, settings=_Settings())
        assert out.manifest["files"]["docs/link"] == {"symlink": True}
        bk.verify_backup_set(_set_dir(live_dir))


# ---------------------------------------------------------------------------
# T6 / T7: exclude and the free-space check
# ---------------------------------------------------------------------------


class TestExcludeAndSpace:
    def test_excluded_label_is_not_copied_and_is_recorded(self, live_dir: Path) -> None:
        (live_dir / "chroma").mkdir()
        (live_dir / "chroma" / "index.bin").write_bytes(b"vec")
        out = bk.backup_data_dir(live_dir, settings=_Settings(), exclude=["chroma"])
        s = _set_dir(live_dir)
        assert not (s / "chroma").exists()
        by = {e["label"]: e for e in out.manifest["entries"]}
        assert by["chroma"]["status"] == "excluded"
        assert (s / "graph.db").is_file()

    def test_excluding_a_target_that_is_absent_records_skipped(self, live_dir: Path) -> None:
        assert not (live_dir / "chroma").exists()
        out = bk.backup_data_dir(live_dir, settings=_Settings(), exclude=["chroma"])
        by = {e["label"]: e for e in out.manifest["entries"]}
        assert by["chroma"]["status"] == "skipped"
        assert "path" not in by["chroma"]

    def test_unknown_label_is_refused_before_anything_is_written(self, live_dir: Path) -> None:
        with pytest.raises(bk.BackupError, match="unknown --exclude label"):
            bk.backup_data_dir(live_dir, settings=_Settings(), exclude=["nope.db"])
        assert _stagings(live_dir) == []
        assert not [p for p in live_dir.iterdir() if p.name.startswith("backup.")]

    def test_insufficient_destination_space_is_refused_before_anything_is_written(
        self, live_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bk, "_FREE_SPACE_RESERVE", 1 << 60)
        with pytest.raises(bk.BackupError, match="not enough free space on the destination"):
            bk.backup_data_dir(live_dir, settings=_Settings())
        assert _stagings(live_dir) == []

    def test_a_separate_data_volume_needs_its_own_reserve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest, data = tmp_path / "dest", tmp_path / "data"
        dest.mkdir()
        data.mkdir()

        class _Usage:
            def __init__(self, free: int) -> None:
                self.free = free

        monkeypatch.setattr(
            bk.shutil, "disk_usage", lambda p: _Usage(10 << 30 if Path(p) == dest else 1)
        )
        real_stat = os.stat

        class _Stat:
            def __init__(self, dev: int) -> None:
                self.st_dev = dev

        monkeypatch.setattr(
            bk.os, "stat", lambda p, *a, **k: _Stat(1 if Path(p) == dest else 2)
            if Path(p) in (dest, data)
            else real_stat(p, *a, **k)
        )
        with pytest.raises(bk.BackupError, match="not enough free space on the data volume"):
            bk._require_free_space(dest, data, 1000)


# ---------------------------------------------------------------------------
# T7c: snapshot connections are closed on every path
# ---------------------------------------------------------------------------


class _Proxy:
    """Stands in for a sqlite3 connection with scripted rollback/close failures."""

    def __init__(self, real: sqlite3.Connection, *, fail_close: int, fail_rollback: int) -> None:
        self._real = real
        self.fail_close = fail_close
        self.fail_rollback = fail_rollback
        self.close_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def execute(self, sql: str, *a: Any) -> Any:
        if sql == "ROLLBACK" and self.fail_rollback > 0:
            self.fail_rollback -= 1
            raise sqlite3.OperationalError("injected rollback failure")
        return self._real.execute(sql, *a)

    def close(self) -> None:
        self.close_calls += 1
        if self.fail_close > 0:
            self.fail_close -= 1
            raise sqlite3.OperationalError("injected close failure")
        self._real.close()


class _Watched(list):  # type: ignore[type-arg]
    plan: dict[str, int]
    #: Every connection opened through a ``mode=rw`` URI, recorded the moment
    #: ``sqlite3.connect`` returns it, before any statement runs on it.
    created: list[sqlite3.Connection]


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> _Watched:
    """Every snapshot connection the run opens, optionally wrapped by ``watched.plan``."""
    conns = _Watched()
    conns.created = []
    real_connect = sqlite3.connect

    def recording_connect(database: Any, *a: Any, **k: Any) -> sqlite3.Connection:
        conn = real_connect(database, *a, **k)
        if isinstance(database, str) and database.startswith("file:") and "mode=rw" in database:
            conns.created.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    real_open = bk._SnapshotPins.open_snapshot
    plan: dict[str, int] = {"fail_close": 0, "fail_rollback": 0, "only": 0}

    def spy(self: Any, source: Path) -> Any:
        mode, snap = real_open(self, source)
        if snap is not None:
            conn: Any = snap.connection
            if plan["fail_close"] or plan["fail_rollback"]:
                if plan["only"] and source.name != "doc_store.db":
                    conns.append(conn)
                    return mode, snap
                proxy = _Proxy(
                    conn, fail_close=plan["fail_close"], fail_rollback=plan["fail_rollback"]
                )
                self._open[self._open.index(conn)] = proxy
                snap.connection = proxy  # type: ignore[assignment]
                conn = proxy
            conns.append(conn)
        return mode, snap

    monkeypatch.setattr(bk._SnapshotPins, "open_snapshot", spy)
    conns.plan = plan
    return conns


def _checkpoint_busy(path: Path) -> int:
    """Commit a new row, then try a TRUNCATE checkpoint. busy=1 means a reader is still pinned."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=1)
    try:
        conn.execute("INSERT INTO t (id, v) VALUES (abs(random() / 4), 'post')")
        return int(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
    finally:
        conn.close()


def _assert_all_closed(conns: list[Any]) -> None:
    assert conns, "no snapshot connection was opened"
    for c in conns:
        real = c._real if isinstance(c, _Proxy) else c
        try:
            real.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue  # "Cannot operate on a closed database": the expected state
        raise AssertionError("a snapshot connection is still open")


def _assert_created_closed(watched: _Watched, expect_open: tuple[Any, ...] = ()) -> None:
    """Every connection the run created is closed, except those a test names as left open.

    ``watched.created`` is filled where ``sqlite3.connect`` returns, so a
    connection whose owner registration came late (or never) is still seen.
    """
    assert watched.created, "no source connection was created"
    for c in watched.created:
        if any(c is e or c is getattr(e, "_real", None) for e in expect_open):
            c.execute("SELECT 1")  # the one connection the test expects to be open
            continue
        try:
            c.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        raise AssertionError("a connection created by the run is still open")


def _assert_unpinned(live_dir: Path) -> None:
    for name in ("graph.db", "doc_store.db", "vectors.db"):
        assert _checkpoint_busy(live_dir / name) == 0, f"{name} is still pinned"


class TestConnectionCleanup:
    def test_normal_run_closes_every_connection(self, live_dir: Path, watched: _Watched) -> None:
        bk.backup_data_dir(live_dir, settings=_Settings())
        _assert_all_closed(watched)
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)

    def test_normal_run_logs_no_rollback_warning(
        self, live_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rollback-journal file never began a transaction, so there is nothing to warn about."""
        with caplog.at_level("WARNING", logger=bk.logger.name):
            bk.backup_data_dir(live_dir, settings=_Settings())
        assert [r.getMessage() for r in caplog.records if "rollback" in r.getMessage()] == []

    def test_failure_while_still_capturing(self, live_dir: Path, watched: _Watched) -> None:
        # vectors.db is processed after graph.db and doc_store.db are pinned.
        (live_dir / "vectors.db").write_bytes(b"not a database at all")
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(live_dir, settings=_Settings())
        _assert_all_closed(watched)
        # The unusable vectors.db was opened too: it is in ``created`` but never
        # returned from open_snapshot, so only the creation-time record sees it.
        assert len(watched) == 2 and len(watched.created) > len(watched)
        _assert_created_closed(watched)
        assert _stagings(live_dir) == []
        for name in ("graph.db", "doc_store.db"):
            assert _checkpoint_busy(live_dir / name) == 0

    def test_control_a_late_registered_connection_is_caught_by_the_creation_record(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Register the connection only after its first statement: the leak must be visible."""

        def late_open(self: Any, source: Path) -> Any:
            conn = sqlite3.connect(bk._rw_uri(source), uri=True, timeout=5, isolation_level=None)
            conn.execute("PRAGMA journal_mode")  # raises on the corrupt file
            self._open.append(conn)  # too late
            return "wal", None

        monkeypatch.setattr(bk._SnapshotPins, "open_snapshot", late_open)
        (live_dir / "graph.db").write_bytes(b"not a database at all")
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(live_dir, settings=_Settings())
        try:
            with pytest.raises(AssertionError):
                _assert_created_closed(watched)
        finally:
            for c in watched.created:
                with contextlib.suppress(sqlite3.Error):
                    c.close()

    @pytest.mark.parametrize("which", [1, 2])
    def test_failure_while_copying_a_snapshot(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch, which: int
    ) -> None:
        real = bk.backup_sqlite
        calls: list[str] = []

        def failing(src: Path, dst: Path, **kw: Any) -> None:
            if kw.get("src_conn") is not None:
                calls.append(src.name)
                if len(calls) == which:
                    raise OSError("induced copy failure")
            real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", failing)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(live_dir, settings=_Settings())
        _assert_all_closed(watched)
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)
        assert _stagings(live_dir) == []

    def test_failure_while_writing_the_manifest(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise OSError("induced manifest failure")

        monkeypatch.setattr(bk, "_write_manifest", boom)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(live_dir, settings=_Settings())
        _assert_all_closed(watched)
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)
        assert _stagings(live_dir) == []

    def test_failure_while_releasing_the_lock(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import opencrab.locking as locking

        real_lock = locking.write_lock

        @contextmanager
        def failing_release(*a: Any, **k: Any) -> Iterator[None]:
            with real_lock(*a, **k):
                yield
            raise OSError("induced release failure")

        monkeypatch.setattr(locking, "write_lock", failing_release)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(live_dir, settings=_Settings())
        _assert_all_closed(watched)
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)
        assert _stagings(live_dir) == []

    def test_control_without_the_cleanup_the_checks_fail(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The checks above can fail: with close_all disabled the unfinished pins survive."""
        monkeypatch.setattr(bk._SnapshotPins, "close_all", lambda self: 0)
        real = bk.backup_sqlite
        calls: list[str] = []

        def failing(src: Path, dst: Path, **kw: Any) -> None:
            if kw.get("src_conn") is not None:
                calls.append(src.name)
                if len(calls) == 2:
                    raise OSError("induced copy failure")
            real(src, dst, **kw)

        monkeypatch.setattr(bk, "backup_sqlite", failing)
        with pytest.raises(bk.BackupError):
            bk.backup_data_dir(live_dir, settings=_Settings())
        try:
            # doc_store.db and vectors.db were still pinned when the run died.
            assert _checkpoint_busy(live_dir / "doc_store.db") == 1
            assert _checkpoint_busy(live_dir / "vectors.db") == 1
            with pytest.raises(AssertionError):
                _assert_all_closed(watched)
        finally:
            for c in watched:
                (c._real if isinstance(c, _Proxy) else c).close()

    def test_control_rollback_without_close_is_caught_by_the_closed_check(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def rollback_only(self: Any, conn: Any) -> bool:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            if conn in self._open:
                self._open.remove(conn)
            return True

        monkeypatch.setattr(bk._SnapshotPins, "release", rollback_only)
        bk.backup_data_dir(live_dir, settings=_Settings())
        try:
            # The checkpoint probe is blind to this leak ...
            _assert_unpinned(live_dir)
            # ... the closed-connection check is not.
            with pytest.raises(AssertionError):
                _assert_all_closed(watched)
        finally:
            for c in watched:
                c.close()

    def test_one_failed_close_is_retried_and_the_backup_succeeds(
        self, live_dir: Path, watched: _Watched
    ) -> None:
        watched.plan["fail_close"] = 1
        watched.plan["only"] = 1
        bk.backup_data_dir(live_dir, settings=_Settings())
        proxies = [c for c in watched if isinstance(c, _Proxy)]
        assert len(proxies) == 1 and proxies[0].close_calls == 2
        _assert_all_closed(watched)
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)
        assert _set_dir(live_dir)

    def test_rollback_and_close_failing_together_once_still_end_closed(
        self, live_dir: Path, watched: _Watched
    ) -> None:
        watched.plan["fail_close"] = 1
        watched.plan["fail_rollback"] = 1
        watched.plan["only"] = 1
        bk.backup_data_dir(live_dir, settings=_Settings())
        _assert_all_closed(watched)
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)

    def test_a_close_that_never_succeeds_aborts_the_run_and_closes_the_others(
        self, live_dir: Path, watched: _Watched
    ) -> None:
        watched.plan["fail_close"] = 10**6
        watched.plan["only"] = 1
        with pytest.raises(bk.BackupError, match="could not be closed"):
            bk.backup_data_dir(live_dir, settings=_Settings())
        stuck = [c for c in watched if isinstance(c, _Proxy)]
        assert len(stuck) == 1 and stuck[0].close_calls >= 3  # two in close_checked, one more in close_all
        others = [c for c in watched if not isinstance(c, _Proxy)]
        _assert_all_closed(others)
        # Every created connection but the one this test deliberately keeps open.
        _assert_created_closed(watched, expect_open=tuple(stuck))
        assert _stagings(live_dir) == []
        assert not [p for p in live_dir.iterdir() if p.name.startswith("backup.")]
        stuck[0].fail_close = 0
        stuck[0].close()  # test cleanup: release the connection the production path gave up on


class TestPublishRenameFailureKeepsStaging:
    def test_rename_failure_keeps_the_staging_directory(
        self, live_dir: Path, watched: _Watched, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_publish(staging: Path, final: Path) -> None:
            raise OSError("induced rename failure")

        monkeypatch.setattr(bk, "_publish", failing_publish)
        with pytest.raises(bk.BackupError, match="no set was published"):
            bk.backup_data_dir(live_dir, settings=_Settings())
        assert len(_stagings(live_dir)) == 1
        _assert_all_closed(watched)  # connections close before the rename, not after
        _assert_created_closed(watched)
        _assert_unpinned(live_dir)


# ---------------------------------------------------------------------------
# T9: the CLI
# ---------------------------------------------------------------------------


def _cli(*args: str, data_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["VECTOR_DB_FILE"] = "vectors.db"
    if data_dir is not None:
        env["LOCAL_DATA_DIR"] = str(data_dir)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "opencrab.stores.backup", *args],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env=env,
        timeout=120,
    )


class TestCli:
    def test_json_prints_one_object_on_stdout_and_progress_on_stderr(self, live_dir: Path) -> None:
        proc = _cli("--data-dir", str(live_dir), "--json")
        assert proc.returncode == 0, proc.stderr
        doc = json.loads(proc.stdout)
        assert doc["format"] == 1 and Path(doc["set_dir"]).is_dir()
        assert "backup set" in proc.stderr

    def test_verify_exit_codes(self, live_dir: Path) -> None:
        assert _cli("--data-dir", str(live_dir)).returncode == 0
        s = _set_dir(live_dir)
        ok = _cli("--verify", str(s))
        assert ok.returncode == 0, ok.stderr
        raw = bytearray((s / "graph.db").read_bytes())
        raw[-1] ^= 0xFF
        (s / "graph.db").write_bytes(bytes(raw))
        bad = _cli("--verify", str(s))
        assert bad.returncode == 1 and "graph.db" in bad.stderr

    def test_exclude_and_lock_timeout_flags_reach_the_backup(self, live_dir: Path) -> None:
        proc = _cli(
            "--data-dir", str(live_dir), "--exclude", "billing.db", "--lock-timeout", "20"
        )
        assert proc.returncode == 0, proc.stderr
        assert not (_set_dir(live_dir) / "billing.db").exists()
        bad = _cli("--data-dir", str(live_dir), "--exclude", "nope")
        assert bad.returncode == 1 and "unknown --exclude" in bad.stderr
        worse = _cli("--data-dir", str(live_dir), "--lock-timeout", "-1")
        assert worse.returncode == 1
