"""
Tests for LocalDocStore — JSON-file-backed legacy doc store.

Structure mirrors test_local_sql_doc_store.py so both backends are held to
the same functional contract.
"""

from __future__ import annotations

import hashlib
import json
import os
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
        import os

        from opencrab.stores.local_doc_store import LocalDocStore

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

    def test_list_nodes_limit_zero_returns_empty(self, store):
        """issue #120 follow-up: 0 rows requested must mean 0 rows back."""
        self._seed(store, "s1", 3)
        assert store.list_nodes(limit=0) == []

    def test_list_nodes_negative_limit_returns_empty(self, store):
        """``rows[:limit]`` with a negative limit is Python slicing (drops
        the last ``abs(limit)`` rows), not "no limit" -- without the guard
        this would return 2 rows (all but the last), not []."""
        self._seed(store, "s1", 3)
        assert store.list_nodes(limit=-1) == []


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

    def test_list_sources_limit_zero_returns_empty(self, store):
        store.upsert_source("s0", "text", {})
        assert store.list_sources(limit=0) == []

    def test_list_sources_negative_limit_returns_empty(self, store):
        # 2 rows, not 1: with only 1 row, unguarded `rows[:-1]` (dropping
        # "the last row") coincidentally also yields [] and the test would
        # pass for the wrong reason -- this pins the real "all but last"
        # bug the guard fixes.
        store.upsert_source("s0", "text", {})
        store.upsert_source("s1", "text", {})
        assert store.list_sources(limit=-1) == []


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

    def test_get_audit_log_limit_zero_returns_empty(self, store):
        store.log_event("tick", None, {})
        assert store.get_audit_log(limit=0) == []

    def test_get_audit_log_negative_limit_returns_empty(self, store):
        # 2 entries, not 1 -- see test_list_sources_negative_limit_returns_empty.
        store.log_event("tick", None, {})
        store.log_event("tick", None, {})
        assert store.get_audit_log(limit=-1) == []

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


# ---------------------------------------------------------------------------
# Corrupt collection file contract (issue #209)
# ---------------------------------------------------------------------------


class TestCorruptCollection:
    """``_load`` fails closed on a corrupt collection file: it raises
    ``CorruptCollectionError`` and never overwrites the file. Replaces the
    old ``test_corrupt_json_returns_empty`` (pinned the ``{}``-fallback bug
    that let the first write after corruption silently destroy the file)."""

    GARBAGE_MIDDLE = b'{"a": {"b": 1}, XXXX not json XXXX "c": 2}'

    def _seeded(self, tmp_path, *, n=3):
        from opencrab.stores.local_doc_store import LocalDocStore

        s = LocalDocStore(str(tmp_path / "docs"))
        for i in range(n):
            s.upsert_node_doc("s1", "T", f"n{i}", {"i": i})
        return s

    def _builder(self, tmp_path):
        from opencrab.ontology.builder import OntologyBuilder
        from opencrab.pack.ownership import create_pack
        from opencrab.stores.local_doc_store import LocalDocStore
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.sql_store import SQLStore

        graph = LocalGraphStore(db_path=str(tmp_path / "graph.db"))
        doc = LocalDocStore(data_dir=str(tmp_path / "docs"))
        sql = SQLStore(url=f"sqlite:///{tmp_path / 'registry.db'}")
        create_pack(sql, "actor-1", "pack-1")
        return OntologyBuilder(graph, doc, sql), doc

    @staticmethod
    def _write(store, collection: str, content: bytes) -> bytes:
        with open(store._collection_path(collection), "wb") as f:
            f.write(content)
        return content

    @staticmethod
    def _read(store, collection: str) -> bytes:
        with open(store._collection_path(collection), "rb") as f:
            return f.read()

    # -- control ----------------------------------------------------------

    def test_control_uncorrupted_file_reads_and_writes_normally(self, tmp_path):
        s = self._seeded(tmp_path)
        assert len(s.list_nodes(limit=100)) == 3
        s.upsert_node_doc("s1", "T", "n3", {"i": 3})
        assert len(s.list_nodes(limit=100)) == 4

    # -- garbage in middle: nodes -------------------------------------------

    def test_garbage_middle_nodes_reads_raise(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "nodes", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError):
            s.get_node_doc("s1", "n0")
        with pytest.raises(CorruptCollectionError):
            s.list_nodes(limit=100)
        with pytest.raises(CorruptCollectionError):
            s.collection_stats()

    def test_garbage_middle_nodes_writes_raise_and_bytes_unchanged(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        corrupt = self._write(s, "nodes", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError):
            s.upsert_node_doc("s1", "T", "n9", {})
        assert self._read(s, "nodes") == corrupt
        with pytest.raises(CorruptCollectionError):
            s.delete_node_doc("s1", "n0")
        assert self._read(s, "nodes") == corrupt

    def test_garbage_middle_nodes_sibling_collections_unaffected(self, tmp_path):
        s = self._seeded(tmp_path)
        corrupt = self._write(s, "nodes", self.GARBAGE_MIDDLE)
        s.upsert_source("src1", "text", {})
        s.log_event("ev", None, {})
        assert s.get_source("src1") is not None
        assert len(s.list_sources(limit=100)) == 1
        assert len(s.get_audit_log(limit=100)) == 1
        after = self._read(s, "nodes")
        assert after == corrupt
        assert os.path.getsize(s._collection_path("nodes")) == len(corrupt)
        assert hashlib.sha256(after).hexdigest() == hashlib.sha256(corrupt).hexdigest()

    # -- garbage in middle: sources ------------------------------------------

    def test_garbage_middle_sources_reads_and_writes_raise(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        corrupt = self._write(s, "sources", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError):
            s.get_source("x")
        with pytest.raises(CorruptCollectionError):
            s.list_sources(limit=100)
        with pytest.raises(CorruptCollectionError):
            s.upsert_source("x", "text", {})
        assert self._read(s, "sources") == corrupt

    def test_garbage_middle_sources_sibling_writes_succeed_bytes_unchanged(self, tmp_path):
        s = self._seeded(tmp_path)
        corrupt = self._write(s, "sources", self.GARBAGE_MIDDLE)
        s.upsert_node_doc("s1", "T", "n9", {})
        s.log_event("ev", None, {})
        assert self._read(s, "sources") == corrupt

    def test_garbage_middle_sources_collection_stats_raises(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "sources", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError):
            s.collection_stats()

    # -- garbage in middle: audit_log ----------------------------------------

    def test_garbage_middle_audit_log_reads_and_writes_raise(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        corrupt = self._write(s, "audit_log", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError):
            s.get_audit_log(limit=100)
        with pytest.raises(CorruptCollectionError):
            s.log_event("ev", None, {})
        assert self._read(s, "audit_log") == corrupt

    def test_garbage_middle_audit_log_sibling_writes_succeed_bytes_unchanged(self, tmp_path):
        s = self._seeded(tmp_path)
        corrupt = self._write(s, "audit_log", self.GARBAGE_MIDDLE)
        s.upsert_node_doc("s1", "T", "n9", {})
        s.upsert_source("src1", "text", {})
        assert self._read(s, "audit_log") == corrupt

    def test_garbage_middle_audit_log_collection_stats_raises(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "audit_log", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError):
            s.collection_stats()

    # -- real OntologyBuilder callers -----------------------------------

    def test_real_builder_corrupt_nodes_json_blocks_add_node_entirely(self, tmp_path):
        """Corrupt ``nodes.json`` is caught earlier than the design's receipt
        row expected: ``node_identity_conflict``
        (``opencrab/pack/write_gate.py``) probes ``docs.get_node_doc`` BEFORE
        the builder creates any receipt or attempts a graph/sql/vector
        write. ``_check_probes`` there treats any probe exception as
        "cannot verify" (fail-closed) and ``add_node`` raises ``ValueError``
        -- so a corrupt ``nodes.json`` blocks the whole node write, not just
        the doc leg (stronger than the ``stores["docs"] = "error: ..."``
        partial-receipt shape the ``audit_log.json`` case below produces)."""
        from opencrab.auth import Principal, principal_scope

        builder, doc = self._builder(tmp_path)
        corrupt = self._write(doc, "nodes", self.GARBAGE_MIDDLE)
        with principal_scope(Principal(user_id="actor-1", is_local=True, disabled=False)):
            with pytest.raises(ValueError, match="cannot verify existing ownership"):
                builder.add_node(
                    space="subject",
                    node_type="User",
                    node_id="u1",
                    properties={"name": "Alice", "email": "alice@example.com", "role": "admin"},
                    pack_id="pack-1",
                )
        assert self._read(doc, "nodes") == corrupt

    def test_real_builder_corrupt_audit_log_receipt_error_node_still_written(self, tmp_path):
        from opencrab.auth import Principal, principal_scope
        from opencrab.ontology.builder import store_write_succeeded_for

        builder, doc = self._builder(tmp_path)
        corrupt = self._write(doc, "audit_log", self.GARBAGE_MIDDLE)
        with principal_scope(Principal(user_id="actor-1", is_local=True, disabled=False)):
            receipt = builder.add_node(
                space="subject",
                node_type="User",
                node_id="u2",
                properties={"name": "Bob", "email": "bob@example.com", "role": "admin"},
                pack_id="pack-1",
            )
        stores = receipt["stores"]
        assert stores["docs"].startswith("error: ")
        assert "corrupt collection file 'audit_log'" in stores["docs"]
        assert stores["graph"] == "ok"
        assert store_write_succeeded_for(stores, "node") is True
        assert doc.get_node_doc("subject", "u2") is not None
        assert self._read(doc, "audit_log") == corrupt

    # -- logging contract ---------------------------------------------------

    def test_caplog_exactly_one_error_record_no_resetting_warning(self, tmp_path, caplog):
        import logging

        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "nodes", self.GARBAGE_MIDDLE)
        with caplog.at_level(logging.DEBUG, logger="opencrab.stores.local_doc_store"):
            with pytest.raises(CorruptCollectionError):
                s.get_node_doc("s1", "n0")
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "nodes" in error_records[0].getMessage()
        resetting_records = [
            r for r in caplog.records if "resetting" in r.getMessage().lower()
        ]
        assert resetting_records == []

    # -- truncated tail / empty / blank / invalid utf-8 ----------------------

    def test_truncated_tail_raises_bytes_unchanged(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        full = self._read(s, "nodes")
        truncated = full[: len(full) // 2]
        self._write(s, "nodes", truncated)
        with pytest.raises(CorruptCollectionError):
            s.list_nodes(limit=100)
        with pytest.raises(CorruptCollectionError):
            s.upsert_node_doc("s1", "T", "n9", {})
        assert self._read(s, "nodes") == truncated

    def test_empty_file_raises(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "nodes", b"")
        with pytest.raises(CorruptCollectionError):
            s.list_nodes(limit=100)

    def test_blank_lines_only_raises(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "nodes", b"\n\n   \n\t\n")
        with pytest.raises(CorruptCollectionError):
            s.list_nodes(limit=100)

    def test_invalid_utf8_raises(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "nodes", b"\xff\xfe{")
        with pytest.raises(CorruptCollectionError):
            s.list_nodes(limit=100)

    # -- top-level value is not a dict ---------------------------------------

    @pytest.mark.parametrize("value", [[], None, "s", 1, True])
    def test_top_level_not_dict_raises_bytes_unchanged(self, tmp_path, value):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        corrupt = self._write(s, "nodes", json.dumps(value).encode("utf-8"))
        with pytest.raises(CorruptCollectionError):
            s.list_nodes(limit=100)
        with pytest.raises(CorruptCollectionError):
            s.upsert_node_doc("s1", "T", "n9", {})
        assert self._read(s, "nodes") == corrupt

    # -- limit=0 short-circuit is unchanged -----------------------------

    def test_limit_zero_short_circuits_even_when_corrupt(self, tmp_path):
        s = self._seeded(tmp_path)
        self._write(s, "nodes", self.GARBAGE_MIDDLE)
        assert s.list_nodes(limit=0) == []
        self._write(s, "sources", self.GARBAGE_MIDDLE)
        assert s.list_sources(limit=0) == []
        self._write(s, "audit_log", self.GARBAGE_MIDDLE)
        assert s.get_audit_log(limit=0) == []

    # -- missing file is still a fresh store, not corrupt --------------------

    def test_missing_file_returns_empty_not_raise(self, tmp_path):
        from opencrab.stores.local_doc_store import LocalDocStore

        s = LocalDocStore(str(tmp_path / "fresh"))
        assert s.list_nodes(limit=100) == []
        assert s.get_node_doc("s1", "n1") is None

    # -- exception shape ------------------------------------------------

    def test_exception_attributes_collection_and_path(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "sources", self.GARBAGE_MIDDLE)
        with pytest.raises(CorruptCollectionError) as excinfo:
            s.get_source("x")
        exc = excinfo.value
        assert exc.collection == "sources"
        assert exc.path == s._collection_path("sources")
        assert str(exc).startswith("corrupt collection file 'sources'")

    # -- no partial write artifact --------------------------------------

    def test_tmp_sibling_not_created_on_failed_write(self, tmp_path):
        from opencrab.stores.local_doc_store import CorruptCollectionError

        s = self._seeded(tmp_path)
        self._write(s, "nodes", self.GARBAGE_MIDDLE)
        tmp_sibling = s._collection_path("nodes") + ".tmp"
        with pytest.raises(CorruptCollectionError):
            s.upsert_node_doc("s1", "T", "n9", {})
        assert not os.path.exists(tmp_sibling)
