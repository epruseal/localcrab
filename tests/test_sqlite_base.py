"""
Contract tests for opencrab.stores._sqlite_base._SqliteConnMixin.

Uses a minimal fake store to exercise the mixin in isolation from any real
store's DDL/business logic.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

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


# ---------------------------------------------------------------------------
# WAL checkpoint policy (issue #94)
# ---------------------------------------------------------------------------


class TestWalCheckpoint:
    """``_checkpoint_if_wal_large`` — explicit WAL TRUNCATE checkpoint policy.

    SQLite's own auto-checkpoint (PASSIVE) reruns on every over-threshold commit
    but never truncates the WAL file even when it fully succeeds — the file only
    shrinks when the last connection closes. These tests pin the explicit-TRUNCATE
    behavior this mixin adds on top of it, and the busy/timeout/exception-isolation
    contracts it must uphold while doing so.
    """

    def _make_table(self, store: _FakeStore) -> None:
        store._conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        store._conn.commit()

    def _wal_size(self, store: _FakeStore) -> int:
        path = store._db_path + "-wal"
        return os.path.getsize(path) if os.path.exists(path) else 0

    def _boom_on(self, match: str, exc: BaseException):
        orig = sqlite3.Connection.execute

        def wrapper(self_conn, sql, *args, **kwargs):
            if match in sql:
                raise exc
            return orig(self_conn, sql, *args, **kwargs)

        return wrapper

    def _spy_on(self, match: str, calls: list):
        orig = sqlite3.Connection.execute

        def wrapper(self_conn, sql, *args, **kwargs):
            if match in sql:
                calls.append(sql)
            return orig(self_conn, sql, *args, **kwargs)

        return wrapper

    # -- 정상 --------------------------------------------------------------

    def test_checkpoint_truncates_wal_once_threshold_crossed(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        assert self._wal_size(store) == 0

    def test_below_threshold_no_checkpoint_attempted(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        self._make_table(store)
        calls: list = []
        monkeypatch.setattr(sqlite3.Connection, "execute", self._spy_on("wal_checkpoint", calls))
        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        assert calls == []

    def test_single_large_transaction_crosses_threshold_at_once(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        with store._tx() as conn:
            for i in range(50):
                conn.execute("INSERT INTO t (v) VALUES (?)", (str(i),))
        assert self._wal_size(store) == 0

    def test_busy_timeout_restored_after_normal_checkpoint(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        original = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == original

    # -- 오류/엣지 (busy) ----------------------------------------------------

    def test_busy_blocker_does_not_fail_commit_and_retries_after_release(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        blocker = sqlite3.connect(store._db_path)
        blocker.execute("BEGIN")
        blocker.execute("SELECT count(*) FROM t").fetchall()

        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        rows = [r[0] for r in store._conn.execute("SELECT v FROM t").fetchall()]
        assert rows == ["x"]  # commit itself did not fail despite busy checkpoint
        assert self._wal_size(store) > 0  # blocked reader prevented the truncate

        blocker.rollback()
        blocker.close()
        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('y')")
        assert self._wal_size(store) == 0  # next commit retries and succeeds

    def test_busy_timeout_restored_after_busy_checkpoint(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        original = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        blocker = sqlite3.connect(store._db_path)
        blocker.execute("BEGIN")
        blocker.execute("SELECT count(*) FROM t").fetchall()

        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == original

        blocker.rollback()
        blocker.close()

    def test_busy_checkpoint_returns_quickly_not_after_default_timeout(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        blocker = sqlite3.connect(store._db_path)
        blocker.execute("BEGIN")
        blocker.execute("SELECT count(*) FROM t").fetchall()

        t0 = time.time()
        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        dt = time.time() - t0

        blocker.rollback()
        blocker.close()
        assert dt < 1.0  # busy_timeout=0 during the attempt avoids the ~5s stall

    # -- 엣지 (WAL 사이드카 부재/체크포인트 실행 실패) ------------------------

    def test_no_wal_sidecar_yet_returns_quietly(self, tmp_path):
        store = _FakeStore(str(tmp_path / "a.db"))
        store._checkpoint_if_wal_large(store._conn)  # must not raise

    def test_checkpoint_exec_failure_does_not_flip_commit_to_failure(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        monkeypatch.setattr(
            sqlite3.Connection, "execute",
            self._boom_on("wal_checkpoint", sqlite3.OperationalError("boom")),
        )
        with store._tx() as conn:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        rows = [r[0] for r in store._conn.execute("SELECT v FROM t").fetchall()]
        assert rows == ["x"]

    def test_checkpoint_exec_baseexception_does_not_trigger_tx_rollback(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        monkeypatch.setattr(
            sqlite3.Connection, "execute",
            self._boom_on("wal_checkpoint", KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            with store._tx() as conn:
                conn.execute("INSERT INTO t (v) VALUES ('x')")
        # commit already succeeded before the checkpoint call raised — data persisted,
        # and _tx()'s except BaseException/rollback branch must not have run.
        monkeypatch.undo()
        rows = [r[0] for r in store._conn.execute("SELECT v FROM t").fetchall()]
        assert rows == ["x"]

    # -- 엣지 (busy_timeout 복원 실패 → 커넥션 폐기·재생성) -------------------

    def test_restore_failure_discards_connection_and_recreates_with_default_timeout(
        self, tmp_path, monkeypatch, caplog
    ):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        old_conn = store._conn
        original = old_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        monkeypatch.setattr(
            sqlite3.Connection, "execute",
            self._boom_on(f"busy_timeout={original}", ValueError("restore boom")),
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="opencrab.stores._sqlite_base"):
            with store._tx() as conn:
                conn.execute("INSERT INTO t (v) VALUES ('x')")
        # write itself still succeeded
        monkeypatch.undo()
        rows = [r[0] for r in store._conn.execute("SELECT v FROM t").fetchall()]
        assert rows == ["x"]
        # the poisoned connection was discarded — a fresh one was created, with
        # the default busy_timeout restored (not left at 0)
        new_conn = store._conn
        assert new_conn is not old_conn
        assert new_conn.execute("PRAGMA busy_timeout").fetchone()[0] == original
        assert old_conn not in store._all_conns
        # the restore failure was logged loudly (warning), not lost in debug noise
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_restore_baseexception_discards_connection_then_reraises(self, tmp_path, monkeypatch):
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        old_conn = store._conn
        original = old_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        monkeypatch.setattr(
            sqlite3.Connection, "execute",
            self._boom_on(f"busy_timeout={original}", KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            with store._tx() as conn:
                conn.execute("INSERT INTO t (v) VALUES ('x')")
        monkeypatch.undo()
        # cleanup ran before the interrupt propagated: the poisoned connection is gone
        assert old_conn not in store._all_conns
        # data from the already-committed write survived
        rows = [r[0] for r in store._conn.execute("SELECT v FROM t").fetchall()]
        assert rows == ["x"]
        # the thread's next connection has a normal (non-zero) busy_timeout
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == original

    def test_all_conns_removal_survives_concurrent_new_conn(self, tmp_path, monkeypatch):
        """The design's earlier revision wrongly proposed guarding ``_all_conns``
        removal with ``self._lock`` (the write-serializing lock); the real
        invariant, enforced by ``_new_conn``/``close()``, is ``self._conns_lock``.
        A concurrent ``_new_conn()`` (from another thread reading in parallel)
        must never see a torn/corrupted ``_all_conns`` list while the discard
        path removes the poisoned connection — this would surface as a
        ``ValueError`` from ``list.remove`` racing a concurrent ``append``, or a
        duplicate/missing entry, if the discard path used the wrong lock.
        """
        store = _FakeStore(str(tmp_path / "a.db"))
        monkeypatch.setattr(store, "_WAL_CHECKPOINT_THRESHOLD_BYTES", 1)
        self._make_table(store)
        old_conn = store._conn
        original = old_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        monkeypatch.setattr(
            sqlite3.Connection, "execute",
            self._boom_on(f"busy_timeout={original}", ValueError("restore boom")),
        )

        errors: list = []
        stop = threading.Event()

        def other_thread_new_conns():
            local_store_conns = []
            while not stop.is_set():
                try:
                    t = threading.local()
                    conn = store._new_conn()
                    local_store_conns.append(conn)
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)
            for conn in local_store_conns:
                try:
                    conn.close()
                except Exception:
                    pass

        t = threading.Thread(target=other_thread_new_conns)
        t.start()
        try:
            with store._tx() as conn:
                conn.execute("INSERT INTO t (v) VALUES ('x')")
        finally:
            stop.set()
            t.join()
        monkeypatch.undo()

        assert errors == []
        assert old_conn not in store._all_conns
        # no duplicate entries — a torn list from a lock mismatch could double-add
        assert len(store._all_conns) == len(set(id(c) for c in store._all_conns))
