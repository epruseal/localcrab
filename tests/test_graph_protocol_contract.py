"""
R5 contract: pins that LocalGraphStore/PGGraphStore/KuzuGraphStore satisfy
the ``GraphStore`` + ``GraphStoreExtended`` Protocols declared in
opencrab/stores/_graph_protocol.py, and that Neo4jStore satisfies only the
base ``GraphStore`` — the 7 extended methods are RED-first (xfail(strict))
until D3 implements them on Neo4jStore.

Neo4j is exercised via a mocked driver/session — no live Neo4j needed. The
mock plumbing (``_make_connected_store``) is the same shape as
tests/test_neo4j_helpers.py's ``_make_connected_store`` (that file's own
docstring says it owns the mocked-session plumbing tests; this file only
needs "does a connected Neo4jStore instance satisfy the Protocol", so the
helper is duplicated locally rather than cross-imported).

xfail(strict=True) is used for every "Neo4j should support extended method
X" case: today those assertions fail (AttributeError / isinstance False),
so xfail marks them as an *expected* failure and keeps this file green. Once
D3 implements a method, its xfail case starts unexpectedly passing (XPASS),
which strict=True turns into a hard failure — that failure is D3's signal to
delete that one xfail marker.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

from opencrab.stores._graph_protocol import GraphStore, GraphStoreExtended
from opencrab.stores.neo4j_store import Neo4jStore

NEO4J_XFAIL_REASON = (
    "Neo4jStore does not implement this method yet — Stage 4 R5 worklist for D3. "
    "Remove this xfail once D3 lands the implementation."
)

BACKENDS = ["local", "pg", "kuzu"]


# ---------------------------------------------------------------------------
# Backend fixtures — local/pg/kuzu real stores (same gating as
# test_find_neighbors_contract.py: pg env-gated, kuzu importorskip)
# ---------------------------------------------------------------------------


def _make_local(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(str(tmp_path / "graph.db"))


def _make_pg():
    pg_url = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not pg_url:
        pytest.skip("OPENCRAB_PG_TEST_URL not set — PG protocol contract skipped")
    from opencrab.stores.pg_graph_store import PGGraphStore

    schema = f"t4gp_{uuid.uuid4().hex[:8]}"
    return PGGraphStore(pg_url, schema=schema), pg_url, schema


def _make_kuzu(tmp_path):
    pytest.importorskip("ladybug")
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    return KuzuGraphStore(db_path=str(tmp_path / "graph_kuzu"))


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path):
    """Yields (backend_name, store) for local/pg/kuzu — mirrors
    test_find_neighbors_contract.py's fixture of the same name/shape."""
    name = request.param
    if name == "local":
        store = _make_local(tmp_path)
        yield name, store
        store.close()
    elif name == "pg":
        store, pg_url, schema = _make_pg()
        yield name, store
        store.close()
        from sqlalchemy import create_engine, text

        engine = create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
    else:
        store = _make_kuzu(tmp_path)
        yield name, store
        store.close()


def _make_connected_neo4j() -> tuple[Neo4jStore, MagicMock, MagicMock]:
    """Same mocked-driver shape as test_neo4j_helpers.py's
    ``_make_connected_store`` — duplicated locally (see module docstring)."""
    mock_session = MagicMock(name="session")
    mock_driver = MagicMock(name="driver")
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_driver.session.return_value.__exit__.return_value = False

    with patch("neo4j.GraphDatabase") as mock_gdb:
        mock_gdb.driver.return_value = mock_driver
        store = Neo4jStore("bolt://mock:7687", "neo4j", "pw")
    return store, mock_driver, mock_session


@pytest.fixture
def neo4j_store():
    store, _driver, _session = _make_connected_neo4j()
    return store


# ---------------------------------------------------------------------------
# Normal — Protocol conformance (isinstance runtime-checkable)
# ---------------------------------------------------------------------------


class TestProtocolConformanceNormal:
    def test_backend_satisfies_graph_store(self, backend):
        _name, store = backend
        assert isinstance(store, GraphStore)

    def test_backend_satisfies_graph_store_extended(self, backend):
        _name, store = backend
        assert isinstance(store, GraphStoreExtended)

    def test_neo4j_satisfies_base_graph_store(self, neo4j_store):
        # Neo4jStore already implements all 13 base methods today.
        assert isinstance(neo4j_store, GraphStore)

    @pytest.mark.xfail(strict=True, reason=NEO4J_XFAIL_REASON)
    def test_neo4j_satisfies_graph_store_extended(self, neo4j_store):
        assert isinstance(neo4j_store, GraphStoreExtended)


# ---------------------------------------------------------------------------
# Normal — extended methods' happy path (local/pg/kuzu, already implemented)
# ---------------------------------------------------------------------------


class TestExtendedMethodsNormal:
    def test_get_node_by_id_returns_props_with_node_type(self, backend):
        _name, store = backend
        store.upsert_node("Lever", "lv1", {"name": "Lever One"})

        node = store.get_node_by_id("lv1")

        assert node is not None
        assert node["node_type"] == "Lever"
        assert node["name"] == "Lever One"

    def test_list_packs_aggregates_by_pack_id(self, backend):
        _name, store = backend
        for i in range(3):
            store.upsert_node("Doc", f"a{i}", {"pack_id": "packA"})
        store.upsert_node("Doc", "b0", {"pack_id": "packB"})

        rows = store.list_packs(min_nodes=1)

        by_pack = {r["pack_id"]: r["node_count"] for r in rows}
        assert by_pack == {"packA": 3, "packB": 1}

    def test_list_packs_min_nodes_filters_small_packs(self, backend):
        _name, store = backend
        for i in range(3):
            store.upsert_node("Doc", f"a{i}", {"pack_id": "packA"})
        store.upsert_node("Doc", "b0", {"pack_id": "packB"})

        rows = store.list_packs(min_nodes=2)

        assert {r["pack_id"] for r in rows} == {"packA"}

    def test_find_by_relations_filters_to_requested_relation_types(self, backend):
        _name, store = backend
        store.upsert_node("Lever", "lv1", {})
        store.upsert_node("Outcome", "o1", {})
        store.upsert_node("Outcome", "o2", {})
        store.upsert_edge("Lever", "lv1", "raises", "Outcome", "o1", {})
        store.upsert_edge("Lever", "lv1", "unrelated", "Outcome", "o2", {})

        rows = store.find_by_relations("lv1", ["raises", "lowers"], "out", 20)

        assert {r["properties"]["id"] for r in rows} == {"o1"}
        assert rows[0]["relation_type"] == "raises"

    def test_export_nodes_filters_by_pack_id(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {"pack_id": "packA"})
        store.upsert_node("Doc", "b0", {"pack_id": "packB"})

        rows = store.export_nodes(pack_id="packA")

        assert len(rows) == 1
        assert rows[0]["props"]["id"] == "a0"
        assert rows[0]["labels"] == ["Doc"]

    def test_export_edges_filters_by_pack_id_on_endpoint(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {"pack_id": "packA"})
        store.upsert_node("Doc", "a1", {"pack_id": "packA"})
        store.upsert_node("Doc", "b0", {"pack_id": "packB"})
        store.upsert_node("Doc", "b1", {"pack_id": "packB"})
        store.upsert_edge("Doc", "a0", "rel", "Doc", "a1", {})
        store.upsert_edge("Doc", "b0", "rel", "Doc", "b1", {})

        rows = store.export_edges(pack_id="packA")

        assert len(rows) == 1
        assert rows[0]["source_props"]["id"] == "a0"
        assert rows[0]["target_props"]["id"] == "a1"
        assert rows[0]["relation"] == "rel"

    def test_upsert_nodes_batch_writes_all_and_returns_count(self, backend):
        _name, store = backend
        nodes = [
            {"node_type": "Doc", "node_id": f"n{i}", "properties": {"i": i}}
            for i in range(3)
        ]

        count = store.upsert_nodes_batch(nodes)

        assert count == 3
        assert store.get_node("Doc", "n1") is not None

    def test_upsert_edges_batch_writes_all_and_returns_count(self, backend):
        _name, store = backend
        for i in range(3):
            store.upsert_node("Doc", f"n{i}", {})
        edges = [
            {
                "from_type": "Doc",
                "from_id": f"n{i}",
                "relation": "next",
                "to_type": "Doc",
                "to_id": f"n{i + 1}",
                "properties": {},
            }
            for i in range(2)
        ]

        count = store.upsert_edges_batch(edges)

        assert count == 2
        assert {r["properties"]["id"] for r in store.find_by_relations("n0", ["next"], "out")} == {"n1"}


# ---------------------------------------------------------------------------
# Normal — Neo4j RED-first: extended methods expected to work once D3 lands
# ---------------------------------------------------------------------------


class TestExtendedMethodsNeo4jPending:
    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_get_node_by_id(self, neo4j_store):
        neo4j_store.get_node_by_id("x")

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_list_packs(self, neo4j_store):
        neo4j_store.list_packs()

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_find_by_relations(self, neo4j_store):
        neo4j_store.find_by_relations("x", ["raises"])

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_export_nodes(self, neo4j_store):
        neo4j_store.export_nodes()

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_export_edges(self, neo4j_store):
        neo4j_store.export_edges()

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_upsert_nodes_batch(self, neo4j_store):
        neo4j_store.upsert_nodes_batch([])

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason=NEO4J_XFAIL_REASON)
    def test_upsert_edges_batch(self, neo4j_store):
        neo4j_store.upsert_edges_batch([])


# ---------------------------------------------------------------------------
# Error — not-found / driver-error paths
# ---------------------------------------------------------------------------


class TestExtendedMethodsError:
    def test_get_node_by_id_unknown_id_returns_none(self, backend):
        _name, store = backend
        assert store.get_node_by_id("does-not-exist") is None

    def test_neo4j_driver_error_propagates_through_base_method(self, neo4j_store):
        # Sanity anchor for "driver/store errors": on the base Protocol
        # (already implemented), a raw driver exception still propagates
        # rather than being swallowed — mirrors
        # test_neo4j_helpers.py::test_driver_exception_propagates_from_run_cypher.
        with patch.object(neo4j_store, "_session") as mock_session_ctx:
            mock_session_ctx.side_effect = RuntimeError("connection reset")
            with pytest.raises(RuntimeError, match="connection reset"):
                neo4j_store.run_cypher("RETURN 1")


# ---------------------------------------------------------------------------
# Edge — empty results, empty batches
# ---------------------------------------------------------------------------


class TestExtendedMethodsEdge:
    def test_list_packs_empty_store_returns_empty_list(self, backend):
        _name, store = backend
        assert store.list_packs() == []

    def test_find_by_relations_empty_relations_returns_empty_list(self, backend):
        _name, store = backend
        store.upsert_node("Lever", "lv1", {})
        assert store.find_by_relations("lv1", [], "out") == []

    def test_export_nodes_no_match_returns_empty_list(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {"pack_id": "packA"})
        assert store.export_nodes(pack_id="does-not-exist") == []

    def test_export_edges_no_match_returns_empty_list(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {"pack_id": "packA"})
        store.upsert_node("Doc", "a1", {"pack_id": "packA"})
        store.upsert_edge("Doc", "a0", "rel", "Doc", "a1", {})
        assert store.export_edges(pack_id="does-not-exist") == []

    def test_upsert_nodes_batch_empty_list_returns_zero(self, backend):
        _name, store = backend
        assert store.upsert_nodes_batch([]) == 0

    def test_upsert_edges_batch_empty_list_returns_zero(self, backend):
        _name, store = backend
        assert store.upsert_edges_batch([]) == 0
