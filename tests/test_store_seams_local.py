"""
Seam contract tests for LocalGraphStore / LocalSQLDocStore, written BEFORE the
mechanical adoption of ``_SqliteConnMixin`` / ``_graph_common`` (see
opencrab/stores/_sqlite_base.py, opencrab/stores/_graph_common.py).

Pins down the behaviour the adoption must NOT change:
  - WAL journal_mode + same-thread connection reuse (conn scaffolding).
  - Every public method that guards on unavailability raises
    ``RuntimeError("<ClassName> is not available.")`` — exact message, since
    the guard swap (``if not self._available or not self._conn: raise ...``
    -> ``self._require_available()``) must not change wording.
  - The two soft-guard methods (``lookup_node_type``, ``keyword_search``)
    that return a falsy value instead of raising when unavailable.
  - Double-close idempotency, reopen-after-close, and independence between
    two instances of the same store class.

Must stay green before AND after the adoption.
"""

from __future__ import annotations

import sqlite3

import pytest

from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_store(tmp_path):
    s = LocalGraphStore(str(tmp_path / "graph.db"))
    assert s.available
    yield s
    s.close()


@pytest.fixture
def doc_store(tmp_path):
    s = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert s.available
    yield s
    s.close()


# Representative call for every guarded public method, keyed by method name.
GRAPH_GUARDED_CALLS: list[tuple[str, tuple]] = [
    ("upsert_node", ("T", "n1", {})),
    ("get_node", ("T", "n1")),
    ("delete_node", ("T", "n1")),
    ("upsert_edge", ("T", "a", "REL", "T", "b")),
    ("find_neighbors", ("n1",)),
    ("find_path", ("a", "b")),
    ("count_nodes", ()),
    ("list_packs", ()),
    ("find_by_relations", ("n1", ["rel"])),
    ("get_node_by_id", ("n1",)),
    ("export_nodes", ()),
    ("export_edges", ()),
    ("upsert_nodes_batch", ([],)),
    ("upsert_edges_batch", ([],)),
]

DOC_GUARDED_CALLS: list[tuple[str, tuple]] = [
    ("upsert_node_doc", ("s", "T", "n1", {})),
    ("get_node_doc", ("s", "n1")),
    ("list_nodes", ()),
    ("bm25_fingerprint", ()),
    ("delete_node_doc", ("s", "n1")),
    ("upsert_source", ("sid", "text", {})),
    ("get_source", ("sid",)),
    ("list_sources", ()),
    ("log_event", ("evt", None, {})),
    ("get_audit_log", ()),
    ("collection_stats", ()),
]


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestNormal:
    def test_graph_store_wal_journal_mode_active(self, graph_store):
        mode = graph_store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_graph_store_same_thread_conn_reuse(self, graph_store):
        assert graph_store._conn is graph_store._conn

    def test_doc_store_wal_journal_mode_active(self, doc_store):
        mode = doc_store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_doc_store_same_thread_conn_reuse(self, doc_store):
        assert doc_store._conn is doc_store._conn


# ---------------------------------------------------------------------------
# Error — unavailability guards
# ---------------------------------------------------------------------------


class TestError:
    @pytest.mark.parametrize("method,args", GRAPH_GUARDED_CALLS, ids=[c[0] for c in GRAPH_GUARDED_CALLS])
    def test_graph_store_guard_raises_when_unavailable(self, graph_store, method, args):
        graph_store._available = False
        with pytest.raises(RuntimeError, match="^LocalGraphStore is not available.$"):
            getattr(graph_store, method)(*args)

    @pytest.mark.parametrize("method,args", DOC_GUARDED_CALLS, ids=[c[0] for c in DOC_GUARDED_CALLS])
    def test_doc_store_guard_raises_when_unavailable(self, doc_store, method, args):
        doc_store._available = False
        with pytest.raises(RuntimeError, match="^LocalSQLDocStore is not available.$"):
            getattr(doc_store, method)(*args)

    def test_graph_store_lookup_node_type_raises_when_unavailable(self, graph_store):
        """#162: lookup_node_type() must NOT return None for "store is
        down" -- None is reserved for "node genuinely absent". An
        unavailable store cannot tell the two apart, so it raises
        GraphReadCapabilityUnavailable instead (fail-closed for callers
        that used to fall back to a guessed default type)."""
        from opencrab.common.graph_identity import GraphReadCapabilityUnavailable

        graph_store._available = False
        with pytest.raises(GraphReadCapabilityUnavailable):
            graph_store.lookup_node_type("n1")

    def test_doc_store_keyword_search_soft_guard_returns_empty(self, doc_store):
        """keyword_search() returns [] rather than raising when unavailable
        (mirrors its own fts_ok-unavailable fallback). The availability
        guard is checked before the (issue #147, now-required) ``pack_ids``
        short-circuit, so an unavailable store returns [] regardless of
        scope."""
        doc_store._available = False
        assert doc_store.keyword_search("query", pack_ids=["p1"]) == []


# ---------------------------------------------------------------------------
# Edge — close()/reopen/instance-independence semantics
# ---------------------------------------------------------------------------


class TestEdge:
    def test_graph_store_double_close_idempotent(self, tmp_path):
        s = LocalGraphStore(str(tmp_path / "g.db"))
        _ = s._conn
        s.close()
        s.close()  # must not raise
        assert s._all_conns == []

    def test_doc_store_double_close_idempotent(self, tmp_path):
        s = LocalSQLDocStore(str(tmp_path / "d.db"))
        _ = s._conn
        s.close()
        s.close()  # must not raise
        assert s._all_conns == []

    def test_graph_store_stale_conn_after_close_raises_programming_error(self, tmp_path):
        """close() does not flip ``_available`` — the guard stays open, but the
        thread-local slot still holds the now-closed connection object, so the
        NEXT same-thread call reaches sqlite3 itself (ProgrammingError), not
        the RuntimeError guard. Must survive the adoption unchanged."""
        s = LocalGraphStore(str(tmp_path / "g.db"))
        s.close()
        assert s.available  # guard flag untouched by close()
        with pytest.raises(sqlite3.ProgrammingError):
            s.get_node("T", "n1")

    def test_doc_store_stale_conn_after_close_raises_programming_error(self, tmp_path):
        s = LocalSQLDocStore(str(tmp_path / "d.db"))
        s.close()
        assert s.available
        with pytest.raises(sqlite3.ProgrammingError):
            s.get_node_doc("s", "n1")

    def test_graph_store_reopen_after_close_sees_persisted_data(self, tmp_path):
        db_path = str(tmp_path / "g.db")
        s1 = LocalGraphStore(db_path)
        s1.upsert_node("T", "n1", {"v": 1})
        s1.close()

        s2 = LocalGraphStore(db_path)
        assert s2.get_node("T", "n1") == {"id": "n1", "v": 1}
        s2.close()

    def test_doc_store_reopen_after_close_sees_persisted_data(self, tmp_path):
        db_path = str(tmp_path / "d.db")
        s1 = LocalSQLDocStore(db_path)
        s1.upsert_node_doc("s", "T", "n1", {"v": 1})
        s1.close()

        s2 = LocalSQLDocStore(db_path)
        doc = s2.get_node_doc("s", "n1")
        assert doc is not None and doc["properties"] == {"v": 1}
        s2.close()

    def test_two_graph_store_instances_independent(self, tmp_path):
        a = LocalGraphStore(str(tmp_path / "a.db"))
        b = LocalGraphStore(str(tmp_path / "b.db"))
        conn_a, conn_b = a._conn, b._conn

        a.close()

        with pytest.raises(sqlite3.ProgrammingError):
            conn_a.execute("SELECT 1")
        conn_b.execute("SELECT 1")  # store b untouched
        b.close()

    def test_two_doc_store_instances_independent(self, tmp_path):
        a = LocalSQLDocStore(str(tmp_path / "a.db"))
        b = LocalSQLDocStore(str(tmp_path / "b.db"))
        conn_a, conn_b = a._conn, b._conn

        a.close()

        with pytest.raises(sqlite3.ProgrammingError):
            conn_a.execute("SELECT 1")
        conn_b.execute("SELECT 1")  # store b untouched
        b.close()
