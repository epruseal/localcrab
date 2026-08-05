"""
Contract tests for opencrab.stores._sqlite_base._SqliteConnMixin.

Uses a minimal fake store to exercise the mixin in isolation from any real
store's DDL/business logic.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from opencrab.stores._sqlite_base import _SqliteConnMixin


class _FakeStore(_SqliteConnMixin):
    """Minimal store exercising the mixin's public surface."""

    def __init__(self, db_path: str, available: bool = True) -> None:
        self._available = available
        self._init_conn_state(db_path)

    def op(self) -> str:
        self._require_available()
        return "ok"


class _ConfigurableStore(_SqliteConnMixin):
    """Store whose _configure_connection is overridable per-test."""

    def __init__(self, db_path: str, configure) -> None:
        self._available = True
        self._configure = configure
        self._init_conn_state(db_path)

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        self._configure(conn)


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestNormal:
    def test_conn_creates_and_caches_per_thread(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        conn1 = store._conn
        conn2 = store._conn
        assert conn1 is conn2  # same thread reuses conn

    def test_wal_pragma_active(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_synchronous_normal(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        # NORMAL == 1 in sqlite3's PRAGMA synchronous encoding
        val = store._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert val == 1

    def test_per_thread_distinct_connections(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        main_conn = store._conn
        other_conn: list = []

        def worker():
            other_conn.append(store._conn)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert other_conn[0] is not main_conn
        assert len(store._all_conns) == 2

    def test_close_closes_all_registered_conns(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        conn = store._conn
        store.close()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        assert store._all_conns == []


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestError:
    def test_require_available_raises_when_unavailable(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"), available=False)
        with pytest.raises(RuntimeError, match="_FakeStore is not available."):
            store.op()

    def test_require_available_passes_when_available(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"), available=True)
        assert store.op() == "ok"

    def test_configure_connection_exception_propagates(self, tmp_path):
        def boom(conn):
            raise ValueError("bad extension")

        store = _ConfigurableStore(str(tmp_path / "a.db"), boom)
        with pytest.raises(ValueError, match="bad extension"):
            _ = store._conn


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestTx:
    """``_tx()`` — commit-on-success / rollback-on-exception transaction boundary.

    Regression for the issue where ``_exec_write_many``/``_exec_write_batch``
    committed after the loop with no except/rollback: a mid-batch exception
    left partially-executed statements sitting uncommitted in the thread's
    connection, and the NEXT unrelated successful write's commit() would
    silently persist them too.
    """

    def _make_table(self, store: _FakeStore) -> None:
        store._conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, val TEXT NOT NULL)")
        store._conn.commit()

    def test_commits_on_success(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        self._make_table(store)
        with store._tx() as conn:
            conn.execute("INSERT INTO t VALUES ('a', '1')")
        rows = store._conn.execute("SELECT id FROM t").fetchall()
        assert [r[0] for r in rows] == ["a"]

    def test_rolls_back_partial_batch_on_exception(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        self._make_table(store)
        with pytest.raises(sqlite3.IntegrityError):
            with store._tx() as conn:
                conn.execute("INSERT INTO t VALUES ('a', '1')")
                conn.execute("INSERT INTO t VALUES ('b', '2')")
                # NOT NULL violation — third statement of the batch fails.
                conn.execute("INSERT INTO t (id, val) VALUES ('c', NULL)")
        # pre-exception state: none of the batch's rows (not even 'a'/'b') persisted.
        rows = store._conn.execute("SELECT id FROM t").fetchall()
        assert rows == []

    def test_later_successful_write_does_not_smuggle_in_failed_batch(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        self._make_table(store)
        with pytest.raises(sqlite3.IntegrityError):
            with store._tx() as conn:
                conn.execute("INSERT INTO t VALUES ('a', '1')")
                conn.execute("INSERT INTO t (id, val) VALUES ('b', NULL)")
        # A later, unrelated write on the SAME thread connection succeeds.
        with store._tx() as conn:
            conn.execute("INSERT INTO t VALUES ('z', '9')")
        # Only the later write's row is present — the failed batch's 'a' did
        # not get smuggled in on the back of this commit.
        rows = sorted(r[0] for r in store._conn.execute("SELECT id FROM t").fetchall())
        assert rows == ["z"]


class TestEdge:
    def test_double_close_idempotent(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        _ = store._conn
        store.close()
        store.close()  # must not raise
        assert store._all_conns == []

    def test_close_with_zero_connections(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        store.close()  # never accessed _conn — must not raise
        assert store._all_conns == []

    def test_two_instances_independent(self, tmp_path):
        store_a = _FakeStore(str(tmp_path / "a.db"))
        store_b = _FakeStore(str(tmp_path / "b.db"))
        conn_a = store_a._conn
        conn_b = store_b._conn

        store_a.close()

        with pytest.raises(sqlite3.ProgrammingError):
            conn_a.execute("SELECT 1")
        # store_b untouched
        conn_b.execute("SELECT 1")
        assert len(store_b._all_conns) == 1

    def test_thread_creates_conn_after_close_of_other_instance(self, tmp_path):
        store_a = _FakeStore(str(tmp_path / "a.db"))
        store_b = _FakeStore(str(tmp_path / "b.db"))
        store_a.close()

        result = []

        def worker():
            result.append(store_b._conn.execute("SELECT 1").fetchone())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert result[0][0] == 1
