"""Tests for the count-only capabilities added for issue #407's
``pack_diagnose_residue`` tool: ``count_exported_edges_scoped`` (graph),
``count_nodes_scoped``/``count_sources_scoped`` (doc stores), and
``count_pack_vectors_bounded`` (vectors).

Fixture styles mirror the existing precedents for the sibling
``*_scoped`` methods these count-only ones sit next to:
  - SQL graph: ``LocalGraphStore`` over a real ``tmp_path`` SQLite file,
    same style as ``tests/test_read_scope_isolation.py``'s store-level
    section.
  - Neo4j: mocked driver/session, same style as
    ``tests/test_neo4j_helpers.py``.
  - SQL doc: ``LocalSQLDocStore`` over a real ``tmp_path`` SQLite file,
    same style as ``tests/test_doc_sources_scoped.py``.
  - Mongo doc: mocked collection double, same style as
    ``tests/test_doc_sources_scoped.py``'s Mongo section.
  - Vectors: a minimal fake object exposing the three ``_vec_backend``
    shapes (``_conn``/sql, ``_collection``/chroma, ``_engine``/sqlalchemy),
    same dispatch ``opencrab.pack.fork._vec_backend`` already reads.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

PACK_A = "pack-a"
PACK_B = "pack-b"


# ---------------------------------------------------------------------------
# count_exported_edges_scoped -- SQL (Local/PG share _SqlGraphStoreBase)
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_store(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(db_path=str(tmp_path / "graph.db"))


def _node(store, node_id: str, pack_id: str, node_type: str = "Doc") -> None:
    store.upsert_node(node_type, node_id, {"pack_id": pack_id, "id": node_id})


def _edge(store, a_id: str, b_id: str, relation: str = "rel", pack_id: str | None = None,
          node_type: str = "Doc") -> None:
    props = {"pack_id": pack_id} if pack_id else {}
    store.upsert_edge(node_type, a_id, relation, node_type, b_id, props)


class TestCountExportedEdgesScopedSql:
    def test_counts_only_edges_whose_endpoints_are_both_in_scope(self, graph_store):
        _node(graph_store, "a1", PACK_A)
        _node(graph_store, "a2", PACK_A)
        _node(graph_store, "b1", PACK_B)
        _edge(graph_store, "a1", "a2")  # both endpoints in pack A
        _edge(graph_store, "a1", "b1")  # crosses packs -- excluded

        assert graph_store.count_exported_edges_scoped([PACK_A]) == 1
        assert graph_store.count_exported_edges_scoped([PACK_A, PACK_B]) == 2

    def test_edge_own_pack_id_must_also_be_in_scope(self, graph_store):
        _node(graph_store, "a1", PACK_A)
        _node(graph_store, "a2", PACK_A)
        _edge(graph_store, "a1", "a2", relation="tagged", pack_id=PACK_B)

        # Both endpoints are in PACK_A's scope, but the edge itself is
        # tagged PACK_B -- the AND rule (export_edges_scoped's own
        # predicate) excludes it from PACK_A's count.
        assert graph_store.count_exported_edges_scoped([PACK_A]) == 0
        assert graph_store.count_exported_edges_scoped([PACK_A, PACK_B]) == 1

    def test_edge_with_no_own_pack_id_counts_when_both_endpoints_in_scope(self, graph_store):
        _node(graph_store, "a1", PACK_A)
        _node(graph_store, "a2", PACK_A)
        _edge(graph_store, "a1", "a2")  # no pack_id on the edge itself

        assert graph_store.count_exported_edges_scoped([PACK_A]) == 1

    def test_matches_export_edges_scoped_length_exactly(self, graph_store):
        """The count-only method must not drift from its export sibling --
        same predicate, no LIMIT."""
        _node(graph_store, "a1", PACK_A)
        _node(graph_store, "a2", PACK_A)
        _node(graph_store, "a3", PACK_A)
        _edge(graph_store, "a1", "a2")
        _edge(graph_store, "a2", "a3")

        exported = graph_store.export_edges_scoped([PACK_A], limit=1000)
        assert graph_store.count_exported_edges_scoped([PACK_A]) == len(exported) == 2

    def test_empty_pack_ids_returns_zero_without_querying(self, graph_store, monkeypatch):
        _node(graph_store, "a1", PACK_A)
        calls = []
        monkeypatch.setattr(
            graph_store, "_fetch_one", lambda sql, params: calls.append((sql, params)) or None
        )
        assert graph_store.count_exported_edges_scoped([]) == 0
        assert calls == []

    def test_unavailable_store_raises(self, tmp_path):
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(db_path=str(tmp_path / "dead.db"))
        store._available = False
        with pytest.raises(RuntimeError):
            store.count_exported_edges_scoped([PACK_A])


# ---------------------------------------------------------------------------
# count_exported_edges_scoped -- Neo4j (mocked driver/session)
# ---------------------------------------------------------------------------


def _make_connected_neo4j_store():
    from unittest.mock import patch

    from opencrab.stores.neo4j_store import Neo4jStore

    mock_session = MagicMock(name="session")
    mock_driver = MagicMock(name="driver")
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_driver.session.return_value.__exit__.return_value = False

    with patch("neo4j.GraphDatabase") as mock_gdb:
        mock_gdb.driver.return_value = mock_driver
        store = Neo4jStore("bolt://mock:7687", "neo4j", "pw")
    return store, mock_session


class TestCountExportedEdgesScopedNeo4j:
    def test_cypher_carries_the_and_predicate(self):
        """Checks the AND *conjunction itself*, not just each fragment's
        presence -- three independent ``in cypher`` substring checks on
        the endpoint/own-pack-id clauses would all still pass if the
        connective between them were mutated from AND to OR (mutation
        testing found this gap, #407). Whitespace is normalized first so
        the check is robust to the query's own indentation."""
        store, session = _make_connected_neo4j_store()
        session.run.return_value.single.return_value = {"total": 3}

        result = store.count_exported_edges_scoped(["p1"])

        cypher = " ".join(session.run.call_args[0][0].split())
        assert "a.pack_id IN $pack_ids AND b.pack_id IN $pack_ids" in cypher
        assert (
            "b.pack_id IN $pack_ids AND (r.pack_id IS NULL OR r.pack_id IN $pack_ids)" in cypher
        )
        assert result == 3

    def test_empty_pack_ids_returns_zero_without_querying(self):
        store, session = _make_connected_neo4j_store()
        session.run.reset_mock()

        assert store.count_exported_edges_scoped([]) == 0
        session.run.assert_not_called()

    def test_no_record_returns_zero(self):
        store, session = _make_connected_neo4j_store()
        session.run.return_value.single.return_value = None

        assert store.count_exported_edges_scoped(["p1"]) == 0


# ---------------------------------------------------------------------------
# count_nodes_scoped / count_sources_scoped -- SQL doc store
# ---------------------------------------------------------------------------


@pytest.fixture
def doc_store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "doc_store.db"))


def _doc_node(store, node_id: str, pack_id: str) -> None:
    store.upsert_node_doc("default", "Doc", node_id, {"pack_id": pack_id})


def _source(store, source_id: str, pack_id: str) -> None:
    store.upsert_source(source_id, f"text of {source_id}", {"pack_id": pack_id})


class TestCountNodesScopedSql:
    def test_matches_list_nodes_scoped_length_exactly(self, doc_store):
        _doc_node(doc_store, "a1", PACK_A)
        _doc_node(doc_store, "a2", PACK_A)
        _doc_node(doc_store, "b1", PACK_B)

        listed = doc_store.list_nodes_scoped([PACK_A], limit=1000)
        assert doc_store.count_nodes_scoped([PACK_A]) == len(listed) == 2
        assert doc_store.count_nodes_scoped([PACK_A, PACK_B]) == 3

    def test_no_source_fallback_unlike_doc_sources(self, doc_store):
        """doc_nodes is strict properties.pack_id-only -- a row tagged only
        via a legacy ``source`` key (the doc_sources fallback) must NOT be
        counted here, the deliberate asymmetry vs count_sources_scoped."""
        doc_store.upsert_node_doc("default", "Doc", "legacy1", {"source": PACK_A})

        assert doc_store.count_nodes_scoped([PACK_A]) == 0

    def test_empty_pack_ids_returns_zero_without_querying(self, doc_store, monkeypatch):
        _doc_node(doc_store, "a1", PACK_A)
        calls = []
        monkeypatch.setattr(
            doc_store, "_fetch_one", lambda sql, params: calls.append((sql, params)) or None
        )
        assert doc_store.count_nodes_scoped([]) == 0
        assert calls == []

    def test_unavailable_store_raises(self, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        store = LocalSQLDocStore(str(tmp_path / "dead.db"))
        store._available = False
        with pytest.raises(RuntimeError, match="not available"):
            store.count_nodes_scoped([PACK_A])


class TestCountSourcesScopedSql:
    def test_matches_list_sources_scoped_length_exactly(self, doc_store):
        _source(doc_store, "a1", PACK_A)
        _source(doc_store, "a2", PACK_A)
        _source(doc_store, "b1", PACK_B)

        listed = doc_store.list_sources_scoped([PACK_A], limit=1000)
        assert doc_store.count_sources_scoped([PACK_A]) == len(listed) == 2

    def test_legacy_source_fallback_is_counted(self, doc_store):
        """count_sources_scoped DOES reuse the pack_id-priority/source-
        fallback rule -- the opposite asymmetry from count_nodes_scoped."""
        doc_store.upsert_source("legacy1", "legacy text", {"source": PACK_A})

        assert doc_store.count_sources_scoped([PACK_A]) == 1

    def test_empty_pack_ids_returns_zero_without_querying(self, doc_store, monkeypatch):
        _source(doc_store, "a1", PACK_A)
        calls = []
        monkeypatch.setattr(
            doc_store, "_fetch_one", lambda sql, params: calls.append((sql, params)) or None
        )
        assert doc_store.count_sources_scoped([]) == 0
        assert calls == []

    def test_unavailable_store_raises(self, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        store = LocalSQLDocStore(str(tmp_path / "dead.db"))
        store._available = False
        with pytest.raises(RuntimeError, match="not available"):
            store.count_sources_scoped([PACK_A])


# ---------------------------------------------------------------------------
# count_nodes_scoped / count_sources_scoped -- Mongo (mocked collection)
# ---------------------------------------------------------------------------


class TestCountNodesScopedMongo:
    def _store(self, count: int):
        from opencrab.stores.mongo_store import MongoStore

        store = MongoStore.__new__(MongoStore)
        store._available = True
        collection = MagicMock()
        collection.count_documents.return_value = count
        store._db = {"nodes": collection}
        return store, collection

    def test_query_carries_the_strict_pack_filter_no_source_fallback(self):
        store, collection = self._store(2)
        result = store.count_nodes_scoped([PACK_A, PACK_B])

        query = collection.count_documents.call_args[0][0]
        assert query["properties.pack_id"]["$in"] == [PACK_A, PACK_B]
        assert "$or" not in query
        assert result == 2

    def test_empty_scope_never_queries(self):
        store, collection = self._store(0)
        assert store.count_nodes_scoped([]) == 0
        collection.count_documents.assert_not_called()

    def test_unavailable_store_raises(self):
        from opencrab.stores.mongo_store import MongoStore

        store = MongoStore.__new__(MongoStore)
        store._available = False
        with pytest.raises(RuntimeError, match="not available"):
            store.count_nodes_scoped([PACK_A])


class TestCountSourcesScopedMongo:
    def _store(self, count: int):
        from opencrab.stores.mongo_store import MongoStore

        store = MongoStore.__new__(MongoStore)
        store._available = True
        collection = MagicMock()
        collection.count_documents.return_value = count
        store._db = {"sources": collection}
        return store, collection

    def test_query_carries_pack_id_and_source_fallback(self):
        from opencrab.stores.mongo_store import _scalar_falsy

        store, collection = self._store(3)
        result = store.count_sources_scoped([PACK_A])

        query = collection.count_documents.call_args[0][0]
        pack_clause = query["$or"][0]["metadata.pack_id"]
        assert pack_clause["$in"] == [PACK_A]
        # $and[0] is the "pack_id is absent" guard that makes the source
        # fallback apply ONLY when pack_id is missing -- mutation testing
        # found the original test never asserted this half of $and at all,
        # so a guard swapped for another pack_id-scope check went
        # undetected (#407).
        guard = query["$or"][1]["$and"][0]["metadata.pack_id"]
        assert guard == _scalar_falsy()
        fallback = query["$or"][1]["$and"][1]["metadata.source"]
        assert fallback["$in"] == [PACK_A]
        assert result == 3

    def test_empty_scope_never_queries(self):
        store, collection = self._store(0)
        assert store.count_sources_scoped([]) == 0
        collection.count_documents.assert_not_called()

    def test_unavailable_store_raises(self):
        from opencrab.stores.mongo_store import MongoStore

        store = MongoStore.__new__(MongoStore)
        store._available = False
        with pytest.raises(RuntimeError, match="not available"):
            store.count_sources_scoped([PACK_A])


# ---------------------------------------------------------------------------
# count_pack_vectors_bounded -- sql / chroma / sqlalchemy dispatch
# ---------------------------------------------------------------------------


class _FakeSqlVec:
    """Mimics the sqlite-vec shape ``_vec_backend`` reads: ``.available``
    plus a DB-API-style ``._conn`` with ``.execute(sql, params).fetchone()``."""

    available = True

    def __init__(self, conn: sqlite3.Connection, table: str = "vectors_kure"):
        self._conn = conn
        self._table = table


def _sqlite_vec_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE vectors_kure (id TEXT, pack_id TEXT)")
    return conn


class _FakeChromaVec:
    available = True

    def __init__(self, collection):
        self._collection = collection


class TestCountPackVectorsBoundedSql:
    def test_known_exact_count(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        conn = _sqlite_vec_conn()
        conn.execute("INSERT INTO vectors_kure VALUES ('v1', ?)", (PACK_A,))
        conn.execute("INSERT INTO vectors_kure VALUES ('v2', ?)", (PACK_A,))
        conn.execute("INSERT INTO vectors_kure VALUES ('v3', ?)", (PACK_B,))
        vec = _FakeSqlVec(conn)

        assert count_pack_vectors_bounded(vec, PACK_A, cap=10) == ("known", 2)

    def test_known_zero_is_not_none(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        vec = _FakeSqlVec(_sqlite_vec_conn())
        assert count_pack_vectors_bounded(vec, PACK_A, cap=10) == ("known", 0)

    def test_sql_count_is_never_capped(self):
        """COUNT(*) never materializes rows -- unlike chroma, exceeding the
        cap must still return an EXACT known count, not known_at_least."""
        from opencrab.pack.fork import count_pack_vectors_bounded

        conn = _sqlite_vec_conn()
        for i in range(5):
            conn.execute("INSERT INTO vectors_kure VALUES (?, ?)", (f"v{i}", PACK_A))
        vec = _FakeSqlVec(conn)

        assert count_pack_vectors_bounded(vec, PACK_A, cap=2) == ("known", 5)


class TestCountPackVectorsBoundedChroma:
    def test_known_when_under_cap(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        collection = MagicMock()
        collection.get.return_value = {"ids": ["v1", "v2"]}
        vec = _FakeChromaVec(collection)

        state, count = count_pack_vectors_bounded(vec, PACK_A, cap=10)
        assert (state, count) == ("known", 2)
        _, kwargs = collection.get.call_args
        assert kwargs["where"] == {"pack_id": PACK_A}
        assert kwargs["limit"] == 11
        assert kwargs["include"] == []

    def test_known_exact_when_count_equals_cap(self):
        """Boundary case for the ``> cap`` vs ``>= cap`` cutoff: exactly
        ``cap`` ids (one under the ``limit=cap + 1`` request) is still a
        precise count, not a capped lower bound (mutation testing found
        this gap, #407)."""
        from opencrab.pack.fork import count_pack_vectors_bounded

        cap = 3
        collection = MagicMock()
        collection.get.return_value = {"ids": [f"v{i}" for i in range(cap)]}
        vec = _FakeChromaVec(collection)

        assert count_pack_vectors_bounded(vec, PACK_A, cap=cap) == ("known", cap)

    def test_known_at_least_when_cap_is_hit(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        cap = 3
        collection = MagicMock()
        collection.get.return_value = {"ids": [f"v{i}" for i in range(cap + 1)]}
        vec = _FakeChromaVec(collection)

        state, count = count_pack_vectors_bounded(vec, PACK_A, cap=cap)
        assert state == "known_at_least"
        assert count == cap + 1
        assert count > 0  # design contract: known_at_least's count is a positive lower bound

    def test_malformed_response_is_unknown_not_zero(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        collection = MagicMock()
        collection.get.return_value = {}  # no "ids" key
        vec = _FakeChromaVec(collection)

        assert count_pack_vectors_bounded(vec, PACK_A, cap=10) == ("unknown", None)


class TestCountPackVectorsBoundedUnavailable:
    def test_unavailable_backend_is_unknown_not_zero(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        class _Unavailable:
            available = False

        assert count_pack_vectors_bounded(_Unavailable(), PACK_A, cap=10) == ("unknown", None)

    def test_unrecognized_shape_is_unknown_not_zero(self):
        from opencrab.pack.fork import count_pack_vectors_bounded

        class _Unrecognized:
            available = True

        assert count_pack_vectors_bounded(_Unrecognized(), PACK_A, cap=10) == ("unknown", None)


class TestCountPackVectorsBackwardCompatWrapper:
    def test_private_wrapper_still_returns_bare_int_or_none(self):
        """fork.py's two call sites read a bare int|None -- the public
        state+count function must not change that contract."""
        from opencrab.pack.fork import _count_pack_vectors

        conn = _sqlite_vec_conn()
        conn.execute("INSERT INTO vectors_kure VALUES ('v1', ?)", (PACK_A,))
        vec = _FakeSqlVec(conn)

        assert _count_pack_vectors(vec, PACK_A, cap=10) == 1

    def test_private_wrapper_preserves_the_cap_plus_one_value_on_cap_hit(self):
        from opencrab.pack.fork import _count_pack_vectors

        cap = 2
        collection = MagicMock()
        collection.get.return_value = {"ids": [f"v{i}" for i in range(cap + 1)]}
        vec = _FakeChromaVec(collection)

        # Pre-existing behaviour (fork_pack's `> FORK_MAX_VECTORS` check):
        # a cap-hit chroma read returns exactly cap + 1 as a bare int.
        assert _count_pack_vectors(vec, PACK_A, cap=cap) == cap + 1

    def test_private_wrapper_returns_none_when_unavailable(self):
        from opencrab.pack.fork import _count_pack_vectors

        class _Unavailable:
            available = False

        assert _count_pack_vectors(_Unavailable(), PACK_A, cap=10) is None

    def test_backend_exception_still_propagates_from_shared_counter_and_fork_wrapper(self):
        """Fork preflight must fail on a live backend read failure.

        ``vectors_axis`` converts this exception only for diagnostics. The
        shared counter and its fork-preflight wrapper preserve the hard
        failure so a fork cannot mistake a failed count for an empty pack.
        """
        from opencrab.pack.fork import _count_pack_vectors, count_pack_vectors_bounded

        collection = MagicMock()
        collection.get.side_effect = RuntimeError("chroma connection lost")
        vec = _FakeChromaVec(collection)

        with pytest.raises(RuntimeError, match="chroma connection lost"):
            count_pack_vectors_bounded(vec, PACK_A, cap=10)
        with pytest.raises(RuntimeError, match="chroma connection lost"):
            _count_pack_vectors(vec, PACK_A, cap=10)
