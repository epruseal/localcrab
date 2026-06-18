"""
Tests for LocalSQLDocStore — 100% coverage target.

Fixture: tmp_path creates a fresh DB file per test; no shared state.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest


@pytest.fixture
def store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "doc_store.db"))


# ---------------------------------------------------------------------------
# Initialisation & availability
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_db_file(self, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        db_path = str(tmp_path / "doc_store.db")
        LocalSQLDocStore(db_path)
        import os

        assert os.path.exists(db_path)

    def test_available_true_on_init(self, store):
        assert store.available is True

    def test_ping_returns_true(self, store):
        assert store.ping() is True

    def test_wal_mode_enabled(self, store):
        row = store._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_available_false_on_bad_path(self):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        # A path whose parent directory does not exist cannot be created.
        store = LocalSQLDocStore("/nonexistent_dir_xyz/subdir/doc.db")
        assert store.available is False

    def test_ping_false_when_unavailable(self):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        store = LocalSQLDocStore("/nonexistent_dir_xyz/subdir/doc.db")
        assert store.ping() is False


# ---------------------------------------------------------------------------
# Node document operations
# ---------------------------------------------------------------------------


class TestNodeDoc:
    def test_upsert_node_doc_stores_doc(self, store):
        store.upsert_node_doc("s1", "Person", "alice", {"name": "Alice"})
        doc = store.get_node_doc("s1", "alice")
        assert doc is not None
        assert doc["node_id"] == "alice"
        assert doc["properties"]["name"] == "Alice"

    def test_upsert_node_doc_overwrites_on_conflict(self, store):
        store.upsert_node_doc("s1", "Person", "alice", {"name": "Alice"})
        store.upsert_node_doc("s1", "Person", "alice", {"name": "Alice Updated"})
        doc = store.get_node_doc("s1", "alice")
        assert doc["properties"]["name"] == "Alice Updated"

    def test_get_node_doc_returns_none_for_missing(self, store):
        result = store.get_node_doc("s1", "nonexistent")
        assert result is None

    def test_get_node_doc_returns_correct_doc(self, store):
        store.upsert_node_doc("space_a", "Concept", "c1", {"title": "Foo"})
        doc = store.get_node_doc("space_a", "c1")
        assert doc["space"] == "space_a"
        assert doc["node_type"] == "Concept"
        assert doc["node_id"] == "c1"
        assert doc["properties"]["title"] == "Foo"
        assert "updated_at" in doc

    def test_upsert_node_doc_updated_at_refreshed(self, store):
        store.upsert_node_doc("s1", "T", "n1", {"v": 1})
        doc1 = store.get_node_doc("s1", "n1")
        time.sleep(0.01)  # ensure clock advances
        store.upsert_node_doc("s1", "T", "n1", {"v": 2})
        doc2 = store.get_node_doc("s1", "n1")
        assert doc2["updated_at"] >= doc1["updated_at"]

    def test_upsert_node_doc_returns_stored_properties(self, store):
        props = {"x": 1, "y": [1, 2, 3]}
        store.upsert_node_doc("s1", "T", "n2", props)
        doc = store.get_node_doc("s1", "n2")
        assert doc["properties"] == props


# ---------------------------------------------------------------------------
# list_nodes
# ---------------------------------------------------------------------------


class TestListNodes:
    def _seed(self, store, space: str, count: int, prefix: str = "n") -> None:
        for i in range(count):
            store.upsert_node_doc(space, "T", f"{prefix}{i}", {"i": i})

    def test_list_nodes_returns_all_when_no_space_filter(self, store):
        self._seed(store, "s1", 3)
        self._seed(store, "s2", 2)
        result = store.list_nodes(limit=100)
        assert len(result) == 5

    def test_list_nodes_filters_by_space(self, store):
        self._seed(store, "s1", 3)
        self._seed(store, "s2", 2)
        result = store.list_nodes(space="s1", limit=100)
        assert len(result) == 3
        assert all(r["space"] == "s1" for r in result)

    def test_list_nodes_respects_limit(self, store):
        self._seed(store, "s1", 10)
        result = store.list_nodes(limit=5)
        assert len(result) == 5

    def test_list_nodes_empty_store_returns_empty(self, store):
        result = store.list_nodes()
        assert result == []

    def test_list_nodes_limit_equals_total(self, store):
        self._seed(store, "s1", 3)
        result = store.list_nodes(limit=3)
        assert len(result) == 3

    def test_list_nodes_limit_exceeds_total(self, store):
        self._seed(store, "s1", 2)
        result = store.list_nodes(limit=1000)
        assert len(result) == 2

    def test_list_nodes_multiple_spaces(self, store):
        self._seed(store, "alpha", 4, "a")
        self._seed(store, "beta", 6, "b")
        assert len(store.list_nodes(space="alpha", limit=100)) == 4
        assert len(store.list_nodes(space="beta", limit=100)) == 6
        assert len(store.list_nodes(limit=100)) == 10


# ---------------------------------------------------------------------------
# delete_node_doc
# ---------------------------------------------------------------------------


class TestDeleteNodeDoc:
    def test_delete_node_doc_returns_true(self, store):
        store.upsert_node_doc("s1", "T", "d1", {})
        assert store.delete_node_doc("s1", "d1") is True

    def test_delete_node_doc_missing_returns_false(self, store):
        assert store.delete_node_doc("s1", "does_not_exist") is False

    def test_delete_node_doc_removes_from_list(self, store):
        store.upsert_node_doc("s1", "T", "x1", {})
        store.upsert_node_doc("s1", "T", "x2", {})
        store.delete_node_doc("s1", "x1")
        ids = [r["node_id"] for r in store.list_nodes(limit=100)]
        assert "x1" not in ids
        assert "x2" in ids


# ---------------------------------------------------------------------------
# Source operations
# ---------------------------------------------------------------------------


class TestSource:
    def test_upsert_source_stores_source(self, store):
        store.upsert_source("src1", "hello world", {"user_id": "u1"})
        src = store.get_source("src1")
        assert src is not None
        assert src["source_id"] == "src1"
        assert src["text"] == "hello world"
        assert src["metadata"]["user_id"] == "u1"
        assert "ingested_at" in src

    def test_get_source_returns_none_for_missing(self, store):
        assert store.get_source("nope") is None

    def test_list_sources_respects_limit(self, store):
        for i in range(10):
            store.upsert_source(f"s{i}", f"text {i}", {})
        result = store.list_sources(limit=3)
        assert len(result) == 3

    def test_upsert_source_overwrites_existing(self, store):
        store.upsert_source("src1", "original", {"v": 1})
        store.upsert_source("src1", "updated", {"v": 2})
        src = store.get_source("src1")
        assert src["text"] == "updated"
        assert src["metadata"]["v"] == 2

    def test_list_sources_empty_returns_empty(self, store):
        assert store.list_sources() == []


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_log_event_stores_event(self, store):
        store.log_event("create", "u1", {"node": "n1"})
        log = store.get_audit_log(limit=10)
        assert len(log) == 1
        assert log[0]["event_type"] == "create"
        assert log[0]["subject_id"] == "u1"
        assert log[0]["details"]["node"] == "n1"

    def test_log_event_returns_event_id(self, store):
        eid = store.log_event("update", None, {})
        assert isinstance(eid, str)
        assert len(eid) == 36  # uuid4 string length

    def test_get_audit_log_sorted_desc(self, store):
        for i in range(5):
            store.log_event("ev", None, {"i": i})
            time.sleep(0.005)  # ensure distinct timestamps
        log = store.get_audit_log(limit=10)
        timestamps = [e["timestamp"] for e in log]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_audit_log_filter_by_event_type(self, store):
        store.log_event("login", "u1", {})
        store.log_event("logout", "u1", {})
        store.log_event("login", "u2", {})
        result = store.get_audit_log(limit=100, event_type="login")
        assert all(e["event_type"] == "login" for e in result)
        assert len(result) == 2

    def test_get_audit_log_limit(self, store):
        for _ in range(20):
            store.log_event("tick", None, {})
        result = store.get_audit_log(limit=5)
        assert len(result) == 5

    def test_get_audit_log_empty(self, store):
        assert store.get_audit_log() == []


# ---------------------------------------------------------------------------
# collection_stats
# ---------------------------------------------------------------------------


class TestCollectionStats:
    def test_collection_stats_returns_counts(self, store):
        store.upsert_node_doc("s1", "T", "n1", {})
        store.upsert_node_doc("s1", "T", "n2", {})
        store.upsert_source("src1", "text", {})
        store.log_event("ev", None, {})
        stats = store.collection_stats()
        assert stats["nodes"] == 2
        assert stats["sources"] == 1
        assert stats["audit_log"] == 1

    def test_collection_stats_empty_store(self, store):
        stats = store.collection_stats()
        assert stats == {"nodes": 0, "sources": 0, "audit_log": 0}


# ---------------------------------------------------------------------------
# Edge / boundary cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_large_properties_json(self, store):
        """1 MB+ properties dict stored and retrieved correctly."""
        big_props: dict[str, Any] = {f"key_{i}": "x" * 100 for i in range(10_000)}
        store.upsert_node_doc("s1", "T", "big", big_props)
        doc = store.get_node_doc("s1", "big")
        assert doc is not None
        assert len(doc["properties"]) == 10_000
        assert doc["properties"]["key_0"] == "x" * 100

    def test_unicode_properties(self, store):
        """Korean + emoji strings round-trip through SQLite without corruption."""
        props = {"label": "안녕하세요", "emoji": "🐙"}
        store.upsert_node_doc("s1", "T", "uni", props)
        doc = store.get_node_doc("s1", "uni")
        assert doc["properties"]["label"] == "안녕하세요"
        assert doc["properties"]["emoji"] == "🐙"

    def test_node_id_with_special_chars(self, store):
        """node_id containing '::' (LocalDocStore composite key separator) is OK."""
        store.upsert_node_doc("s1", "T", "space::node::extra", {"ok": True})
        doc = store.get_node_doc("s1", "space::node::extra")
        assert doc is not None
        assert doc["node_id"] == "space::node::extra"

    def test_concurrent_writes_thread_safe(self, store):
        """10 threads × 100 upserts should complete without errors."""
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                for i in range(100):
                    store.upsert_node_doc("s1", "T", f"t{tid}_n{i}", {"tid": tid, "i": i})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == [], f"Concurrent write errors: {errors}"
        stats = store.collection_stats()
        assert stats["nodes"] == 1000

    def test_close_and_reopen(self, tmp_path):
        """Data persists after close() + re-initialisation."""
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        db_path = str(tmp_path / "persist.db")
        s1 = LocalSQLDocStore(db_path)
        s1.upsert_node_doc("s1", "T", "persistent", {"v": 42})
        s1.close()

        s2 = LocalSQLDocStore(db_path)
        doc = s2.get_node_doc("s1", "persistent")
        assert doc is not None
        assert doc["properties"]["v"] == 42


# ---------------------------------------------------------------------------
# Unavailable store — all mutating / reading methods raise RuntimeError
# ---------------------------------------------------------------------------


class TestUnavailableStore:
    """Force _available=False to cover all RuntimeError guard branches."""

    @pytest.fixture
    def dead_store(self, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        s = LocalSQLDocStore(str(tmp_path / "dead.db"))
        # Simulate a store that initialised fine but became unavailable.
        s._available = False
        return s

    def test_upsert_node_doc_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.upsert_node_doc("s", "T", "n", {})

    def test_get_node_doc_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.get_node_doc("s", "n")

    def test_list_nodes_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.list_nodes()

    def test_delete_node_doc_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.delete_node_doc("s", "n")

    def test_upsert_source_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.upsert_source("src", "text", {})

    def test_get_source_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.get_source("src")

    def test_list_sources_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.list_sources()

    def test_log_event_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.log_event("ev", None, {})

    def test_get_audit_log_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.get_audit_log()

    def test_collection_stats_raises(self, dead_store):
        with pytest.raises(RuntimeError, match="not available"):
            dead_store.collection_stats()

    def test_close_exception_path(self, tmp_path):
        """close() swallows exceptions raised by sqlite3.Connection.close()."""
        from unittest.mock import MagicMock

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        s = LocalSQLDocStore(str(tmp_path / "ex.db"))
        mock_conn = MagicMock()
        mock_conn.close.side_effect = Exception("forced close error")
        # 스레드-로컬 커넥션 구조: close() 는 _all_conns 를 순회하며 각 커넥션을 닫는다.
        s._all_conns.append(mock_conn)
        # Should NOT raise — exception is swallowed silently.
        s.close()
        mock_conn.close.assert_called()
