"""
Tests for LocalDocStore — JSON-file-backed legacy doc store.

Structure mirrors test_local_sql_doc_store.py so both backends are held to
the same functional contract.
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def store(tmp_path):
    from opencrab.stores.local_doc_store import LocalDocStore

    return LocalDocStore(str(tmp_path / "doc_store"))


# ---------------------------------------------------------------------------
# Initialisation & availability
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_data_dir(self, tmp_path):
        from opencrab.stores.local_doc_store import LocalDocStore
        import os

        data_dir = str(tmp_path / "mystore")
        LocalDocStore(data_dir)
        assert os.path.isdir(data_dir)

    def test_available_true_on_init(self, store):
        assert store.available is True

    def test_ping_returns_true(self, store):
        assert store.ping() is True

    def test_ping_false_when_dir_removed(self, store):
        import shutil
        shutil.rmtree(store._data_dir)
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
        time.sleep(0.01)
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

    def test_get_audit_log_sorted_desc(self, store):
        for i in range(5):
            store.log_event("ev", None, {"i": i})
            time.sleep(0.005)
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
    def test_unicode_properties(self, store):
        """Korean + emoji strings round-trip through JSON without corruption."""
        props = {"label": "안녕하세요", "emoji": "🐙"}
        store.upsert_node_doc("s1", "T", "uni", props)
        doc = store.get_node_doc("s1", "uni")
        assert doc["properties"]["label"] == "안녕하세요"
        assert doc["properties"]["emoji"] == "🐙"

    def test_node_id_with_special_chars(self, store):
        """node_id containing '::' stored and retrieved correctly."""
        store.upsert_node_doc("s1", "T", "a::b::c", {"ok": True})
        # Note: LocalDocStore uses f"{space}::{node_id}" as the dict key,
        # so "s1::a::b::c" is stored and retrieved by the same composite key.
        doc = store.get_node_doc("s1", "a::b::c")
        assert doc is not None
        assert doc["node_id"] == "a::b::c"

    def test_corrupt_json_returns_empty(self, tmp_path):
        """Corrupt JSON file is handled gracefully by returning an empty dict."""
        from opencrab.stores.local_doc_store import LocalDocStore

        s = LocalDocStore(str(tmp_path / "corrupt"))
        # Write corrupt JSON to the nodes file.
        nodes_path = s._collection_path("nodes")
        with open(nodes_path, "w") as f:
            f.write("{bad json}")
        # _load should catch JSONDecodeError and return {} → get_node_doc = None
        result = s.get_node_doc("s1", "n1")
        assert result is None

    def test_source_text_truncated_at_4096(self, store):
        """LocalDocStore truncates source text to 4096 chars on upsert."""
        long_text = "x" * 10_000
        store.upsert_source("long_src", long_text, {})
        src = store.get_source("long_src")
        assert len(src["text"]) == 4096

    def test_safe_str_handles_non_string(self, store):
        """_safe_str converts non-str values to str."""
        result = store._safe_str(42)
        assert result == "42"
