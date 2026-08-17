"""
R5 contract: pins that LocalGraphStore/PGGraphStore/KuzuGraphStore/Neo4jStore
all satisfy the ``GraphStore`` + ``GraphStoreExtended`` Protocols declared in
opencrab/stores/_graph_protocol.py.

Neo4jStore's 7 extended methods (get_node_by_id, list_packs,
find_by_relations, export_nodes, export_edges, upsert_nodes_batch,
upsert_edges_batch) were RED-first (xfail(strict)) until D3 implemented them
— this file has since flipped green: the ``TestExtendedMethodsNeo4jPending``
xfail class was replaced by ``TestExtendedMethodsNeo4jNormal`` /
``...Error`` / ``...Edge`` below, which exercise the real Cypher each method
builds against a mocked session.

Neo4j is exercised via a mocked driver/session — no live Neo4j needed. The
mock plumbing (``_make_connected_neo4j``) is the same shape as
tests/test_neo4j_helpers.py's ``_make_connected_store`` (that file's own
docstring says it owns the mocked-session plumbing tests; this file only
needs "does a connected Neo4jStore instance satisfy the Protocol / behave
correctly", so the helper is duplicated locally rather than cross-imported).
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

from opencrab.stores._graph_protocol import GraphStore, GraphStoreExtended
from opencrab.stores.neo4j_store import Neo4jStore

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

    def test_neo4j_satisfies_graph_store_extended(self, neo4j_store):
        # D3 landed the 7 extended methods — Neo4jStore now satisfies the
        # full GraphStoreExtended Protocol like the other three backends.
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
# issue #120: export_nodes' three implementations (SQL backend shared by
# local/pg, Kuzu's no-pack_id branch, Kuzu's pack_id branch) must all treat
# ``limit <= 0`` as "return nothing", checked BEFORE any row is collected --
# not after (Kuzu's pack_id branch used to append its first match, then
# check the limit, so limit=0 still returned 1 row). Negative limit gets the
# same "return nothing" treatment, since it otherwise has backend-specific
# meaning (e.g. SQLite maps a bound LIMIT -1 to "unlimited").
# ---------------------------------------------------------------------------


class TestExportNodesLimitContract:
    def test_limit_zero_returns_empty_list(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {})
        store.upsert_node("Doc", "a1", {})

        assert store.export_nodes(limit=0) == []

    def test_limit_zero_with_pack_id_returns_empty_list(self, backend):
        """Pins the exact issue #120 regression: Kuzu's pack_id branch
        appended its first match before checking the limit."""
        _name, store = backend
        store.upsert_node("Doc", "a0", {"pack_id": "packA"})
        store.upsert_node("Doc", "a1", {"pack_id": "packA"})

        assert store.export_nodes(pack_id="packA", limit=0) == []

    def test_negative_limit_returns_empty_list(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {})
        store.upsert_node("Doc", "a1", {})

        assert store.export_nodes(limit=-1) == []

    def test_negative_limit_with_pack_id_returns_empty_list(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {"pack_id": "packA"})
        store.upsert_node("Doc", "a1", {"pack_id": "packA"})

        assert store.export_nodes(pack_id="packA", limit=-1) == []


# ---------------------------------------------------------------------------
# Normal — Neo4j's 7 newly-implemented extended methods (mocked session)
# ---------------------------------------------------------------------------


class TestExtendedMethodsNeo4jNormal:
    def test_get_node_by_id_returns_props_with_node_type(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value.single.return_value = {
            "props": {"id": "lv1", "name": "Lever One"},
            "lbl": "Lever",
        }

        node = store.get_node_by_id("lv1")

        assert node == {"id": "lv1", "name": "Lever One", "node_type": "Lever"}
        cypher, kwargs = mock_session.run.call_args[0][0], mock_session.run.call_args[1]
        assert "labels(n)[0]" in cypher
        assert kwargs == {"id": "lv1"}

    def test_list_packs_aggregates_by_pack_id(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = [
            {"pack_id": "packA", "node_count": 3, "sample_title": "A"},
            {"pack_id": "packB", "node_count": 1, "sample_title": ""},
        ]

        rows = store.list_packs(min_nodes=1)

        assert {r["pack_id"]: r["node_count"] for r in rows} == {"packA": 3, "packB": 1}
        assert mock_session.run.call_args[1] == {"min_nodes": 1}

    def test_find_by_relations_filters_to_requested_relation_types(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = [
            {"props": {"id": "o1"}, "labels": ["Outcome"], "relation_type": "raises"}
        ]

        rows = store.find_by_relations("lv1", ["raises", "lowers"], "out", 20)

        assert rows == [
            {"properties": {"id": "o1"}, "labels": ["Outcome"], "relation_type": "raises"}
        ]
        cypher = mock_session.run.call_args[0][0]
        assert "-[r:raises|lowers]->" in cypher
        assert mock_session.run.call_args[1] == {"id": "lv1", "limit": 20}

    def test_find_by_relations_in_direction_uses_incoming_arrow(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = []

        store.find_by_relations("o1", ["raises"], "in", 20)

        cypher = mock_session.run.call_args[0][0]
        assert "<-[r:raises]-" in cypher

    def test_export_nodes_filters_by_pack_id(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = [
            {"props": {"id": "a0", "pack_id": "packA"}, "labels": ["Doc", "OpenCrabNode"]}
        ]

        rows = store.export_nodes(pack_id="packA")

        assert rows == [
            {"props": {"id": "a0", "pack_id": "packA"}, "labels": ["Doc", "OpenCrabNode"]}
        ]
        assert mock_session.run.call_args[1] == {"pack_id": "packA", "space": None}

    def test_export_nodes_pushes_space_into_cypher_params(self):
        """issue #54: space must reach the Cypher WHERE clause as a bound
        parameter (real pushdown ahead of LIMIT via the ``n.space`` property
        Neo4jStore writes on upsert), not a Python post-filter."""
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = []

        store.export_nodes(pack_id="packA", space="concept")

        assert mock_session.run.call_args[1] == {"pack_id": "packA", "space": "concept"}
        cypher = mock_session.run.call_args[0][0]
        assert "$space" in cypher

    def test_count_exported_nodes_not_capped_by_limit(self):
        """issue #54: count_exported_nodes is a real count(n) query with the
        same predicate as export_nodes but no LIMIT -- it must report the
        true match count, which a caller cannot get from
        len(export_nodes(..., limit=N)) once N is smaller than the real
        total."""
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value.single.return_value = {"total": 3000}

        total = store.count_exported_nodes(pack_id="packA", space="concept")

        assert total == 3000
        assert mock_session.run.call_args[1] == {"pack_id": "packA", "space": "concept"}
        cypher = mock_session.run.call_args[0][0]
        assert "count(n)" in cypher
        assert "LIMIT" not in cypher

    def test_export_edges_filters_by_pack_id_on_endpoint(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = [
            {
                "source_props": {"id": "a0"},
                "source_labels": ["Doc"],
                "target_props": {"id": "a1"},
                "target_labels": ["Doc"],
                "rel_props": {},
                "relation": "rel",
            }
        ]

        rows = store.export_edges(pack_id="packA")

        assert len(rows) == 1
        assert rows[0]["source_props"]["id"] == "a0"
        assert rows[0]["target_props"]["id"] == "a1"
        assert rows[0]["relation"] == "rel"
        assert mock_session.run.call_args[1] == {"pack_id": "packA"}

    def test_upsert_nodes_batch_writes_all_and_returns_count(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()
        mock_session.run.return_value.single.return_value = {"props": {"id": "n0"}}

        nodes = [
            {"node_type": "Doc", "node_id": f"n{i}", "properties": {"i": i}}
            for i in range(3)
        ]
        count = store.upsert_nodes_batch(nodes)

        assert count == 3
        assert mock_session.run.call_count == 3

    def test_upsert_edges_batch_writes_all_and_returns_count(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()
        mock_session.run.return_value.single.return_value = {"r": "edge"}

        edges = [
            {
                "from_type": "Doc", "from_id": f"n{i}", "relation": "next",
                "to_type": "Doc", "to_id": f"n{i + 1}", "properties": {},
            }
            for i in range(2)
        ]
        count = store.upsert_edges_batch(edges)

        assert count == 2
        assert mock_session.run.call_count == 2

    def test_upsert_edges_batch_counts_only_successful_upserts(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()
        # First edge's MERGE finds/creates a record (success), second's
        # MATCH on a missing endpoint returns no record (failure).
        mock_session.run.return_value.single.side_effect = [{"r": "edge"}, None]

        edges = [
            {"from_type": "Doc", "from_id": "n0", "relation": "next", "to_type": "Doc", "to_id": "n1", "properties": {}},
            {"from_type": "Doc", "from_id": "n1", "relation": "next", "to_type": "Doc", "to_id": "missing", "properties": {}},
        ]
        count = store.upsert_edges_batch(edges)

        assert count == 1


# ---------------------------------------------------------------------------
# Error — Neo4j driver exceptions propagate through the new methods
# ---------------------------------------------------------------------------


class TestExtendedMethodsNeo4jError:
    def test_get_node_by_id_driver_error_propagates(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.side_effect = RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            store.get_node_by_id("x")

    def test_find_by_relations_driver_error_propagates(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.side_effect = RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            store.find_by_relations("x", ["raises"])

    def test_export_nodes_driver_error_propagates(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.side_effect = RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            store.export_nodes()


# ---------------------------------------------------------------------------
# Edge — Neo4j: empty results, empty batches, not-found
# ---------------------------------------------------------------------------


class TestExtendedMethodsNeo4jEdge:
    def test_get_node_by_id_not_found_returns_none(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value.single.return_value = None

        assert store.get_node_by_id("does-not-exist") is None

    def test_list_packs_empty_store_returns_empty_list(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = []

        assert store.list_packs() == []

    def test_find_by_relations_empty_relations_returns_empty_list_without_query(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()

        assert store.find_by_relations("lv1", [], "out") == []
        mock_session.run.assert_not_called()

    def test_export_nodes_no_match_returns_empty_list(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = []

        assert store.export_nodes(pack_id="does-not-exist") == []

    def test_export_edges_no_match_returns_empty_list(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value = []

        assert store.export_edges(pack_id="does-not-exist") == []

    def test_export_nodes_limit_zero_returns_empty_list_without_querying(self):
        """issue #120: the contract says ``limit <= 0`` returns ``[]``
        WITHOUT issuing a query -- Neo4j's own LIMIT is a raw Cypher literal
        (not parameterized), so this can't be proven by result shape alone
        the way the SQL/Kuzu backends can; asserting ``session.run`` was
        never called (after the constructor's own connectivity check, hence
        ``reset_mock()``) is the only way to pin "no query issued" here."""
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()

        assert store.export_nodes(limit=0) == []
        mock_session.run.assert_not_called()

    def test_export_nodes_negative_limit_returns_empty_list_without_querying(self):
        """issue #120: a raw negative Cypher LIMIT literal is invalid and
        would raise at the driver -- the guard must short-circuit before
        that query is ever built, same as limit=0 above."""
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()

        assert store.export_nodes(limit=-1) == []
        mock_session.run.assert_not_called()

    def test_upsert_nodes_batch_empty_list_returns_zero(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()

        assert store.upsert_nodes_batch([]) == 0
        mock_session.run.assert_not_called()

    def test_upsert_edges_batch_empty_list_returns_zero(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.reset_mock()

        assert store.upsert_edges_batch([]) == 0
        mock_session.run.assert_not_called()


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


# ---------------------------------------------------------------------------
# get_edge (#146 P1(a)) -- probe read using each backend's own upsert
# conflict key; see GraphStore.get_edge's docstring for the cross-backend
# contract (SQL/Neo4j key on all 5 args, Kuzu keys on from_id/relation/to_id
# alone). Design doc reproduction test #14: SQL via the real local/pg
# `backend` fixture, Kuzu via importorskip, Neo4j via mocked session.
# ---------------------------------------------------------------------------


class TestGetEdgeContract:
    def test_existing_edge_returns_parsed_properties_dict(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {})
        store.upsert_node("Doc", "a1", {})
        store.upsert_edge("Doc", "a0", "rel", "Doc", "a1", {"weight": 3})

        edge = store.get_edge("Doc", "a0", "rel", "Doc", "a1")

        assert isinstance(edge, dict)
        assert edge["weight"] == 3

    def test_absent_edge_returns_none(self, backend):
        _name, store = backend
        store.upsert_node("Doc", "a0", {})
        store.upsert_node("Doc", "a1", {})

        assert store.get_edge("Doc", "a0", "rel", "Doc", "a1") is None

    def test_wrong_relation_key_returns_none(self, backend):
        """A different relation is a different conflict key on every
        backend -- proves get_edge does not match on endpoints alone."""
        _name, store = backend
        store.upsert_node("Doc", "a0", {})
        store.upsert_node("Doc", "a1", {})
        store.upsert_edge("Doc", "a0", "rel-a", "Doc", "a1", {})

        assert store.get_edge("Doc", "a0", "rel-b", "Doc", "a1") is None

    def test_reversed_endpoints_returns_none(self, backend):
        """Directionality matters -- get_edge must not match the reverse edge."""
        _name, store = backend
        store.upsert_node("Doc", "a0", {})
        store.upsert_node("Doc", "a1", {})
        store.upsert_edge("Doc", "a0", "rel", "Doc", "a1", {})

        assert store.get_edge("Doc", "a1", "rel", "Doc", "a0") is None


class TestGetEdgeKuzuJsonParsing:
    """v2 결함 2 반례: Kuzu's ``e.properties`` column is a JSON-serialized
    string -- get_edge must return a parsed dict, never the raw string, or
    the same-pack re-ingest path (which reads ``pack_id`` back out of it)
    would fail-closed on every re-ingest against a Kuzu backend."""

    def test_properties_returned_as_dict_not_json_string(self, tmp_path):
        pytest.importorskip("ladybug")
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore

        store = KuzuGraphStore(db_path=str(tmp_path / "graph_kuzu_edge"))
        try:
            store.upsert_node("Doc", "a0", {})
            store.upsert_node("Doc", "a1", {})
            store.upsert_edge("Doc", "a0", "rel", "Doc", "a1", {"pack_id": "pack-a"})

            edge = store.get_edge("Doc", "a0", "rel", "Doc", "a1")

            assert isinstance(edge, dict)
            assert edge["pack_id"] == "pack-a"
        finally:
            store.close()

    def test_type_arguments_accepted_but_not_matched_on(self, tmp_path):
        """Kuzu's MERGE key is (from_id, relation, to_id) alone -- passing
        the WRONG from_type/to_type must still find the edge (upsert_edge's
        own MERGE never wrote a type predicate either)."""
        pytest.importorskip("ladybug")
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore

        store = KuzuGraphStore(db_path=str(tmp_path / "graph_kuzu_edge2"))
        try:
            store.upsert_node("Doc", "a0", {})
            store.upsert_node("Doc", "a1", {})
            store.upsert_edge("Doc", "a0", "rel", "Doc", "a1", {"pack_id": "pack-a"})

            edge = store.get_edge("WrongType", "a0", "rel", "AlsoWrong", "a1")

            assert edge is not None
            assert edge["pack_id"] == "pack-a"
        finally:
            store.close()


class TestGetEdgeNeo4j:
    def test_existing_edge_returns_properties(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value.single.return_value = {"props": {"pack_id": "pack-a"}}

        edge = store.get_edge("Doc", "a0", "rel", "Doc", "a1")

        assert edge == {"pack_id": "pack-a"}
        cypher = mock_session.run.call_args[0][0]
        assert "-[r:rel]->" in cypher
        assert mock_session.run.call_args[1] == {"from_id": "a0", "to_id": "a1"}

    def test_absent_edge_returns_none(self):
        store, _driver, mock_session = _make_connected_neo4j()
        mock_session.run.return_value.single.return_value = None

        assert store.get_edge("Doc", "a0", "rel", "Doc", "a1") is None


# ---------------------------------------------------------------------------
# export_* must carry `space` inside props (regression: space_id column dropped)
#
# The SQL and Kuzu backends keep space in a dedicated `space_id` column while
# upsert_node only injects `id` into properties. export_nodes/export_edges used
# to select `properties` alone, so the exported props had no `space` key unless
# a caller had put one there -- measured 2026-07-29 on a live store, 206,817 of
# 248,304 nodes (83.3%) lacked it. Every props-only consumer broke silently:
# the BM25 space filter in ontology/query.py discarded 88% of its candidates,
# ontology_list_nodes and pack export emitted an empty space, and the graph API
# fell back to "concept".
#
# The protocol documents the export shape as {"props": dict, "labels": [str]}
# with space inside props (which is how Neo4j behaves natively), so these pin
# that contract for every backend.
# ---------------------------------------------------------------------------


class TestExportCarriesSpace:
    def test_export_nodes_props_carry_space_from_column(self, backend):
        _name, store = backend
        store.upsert_node("TextUnit", "n-space-1", {"text": "body"}, space_id="evidence")

        rows = store.export_nodes()
        row = next(r for r in rows if (r["props"].get("id") == "n-space-1"))

        assert row["props"]["space"] == "evidence"
        assert row["labels"] == ["TextUnit"]

    def test_explicit_space_id_argument_overwrites_props_space(self, backend):
        """issue #118 codex review [2]: the explicit ``space_id`` ARGUMENT
        wins over a conflicting ``properties["space"]`` key, not the other
        way around -- this used to pin the reverse ("the column is a
        fallback, never an override"), which matched _merge_space's
        read-time precedence but NOT what neo4j_store.py's own upsert_node
        already did (``if space_id: props["space"] = space_id``,
        unconditional). Flipped so all three backends agree with each
        other, using Neo4j's pre-existing behavior (and the less-surprising
        rule: a caller's explicit ``space=`` argument should not be
        silently overridden by an incidental key in an arbitrary
        ``properties`` dict) as the target, not the reverse. See
        opencrab/stores/_graph_common.py's ``_normalize_space`` docstring.
        """
        _name, store = backend
        store.upsert_node(
            "TextUnit", "n-space-2", {"text": "body", "space": "claim"}, space_id="evidence"
        )

        rows = store.export_nodes()
        row = next(r for r in rows if (r["props"].get("id") == "n-space-2"))

        assert row["props"]["space"] == "evidence"

    def test_export_edges_both_endpoints_carry_space(self, backend):
        _name, store = backend
        store.upsert_node("Document", "e-src", {"title": "Doc"}, space_id="resource")
        store.upsert_node("TextUnit", "e-dst", {"text": "body"}, space_id="evidence")
        store.upsert_edge("Document", "e-src", "contains", "TextUnit", "e-dst")

        rows = store.export_edges()
        row = next(
            r for r in rows
            if r["source_props"].get("id") == "e-src" and r["target_props"].get("id") == "e-dst"
        )

        assert row["source_props"]["space"] == "resource"
        assert row["target_props"]["space"] == "evidence"
        assert row["relation"] == "contains"

    def test_missing_space_id_leaves_props_untouched(self, backend):
        """No space column value -> no invented key (callers keep distinguishing).

        Kuzu stores ``space_id or ""``, so an absent space surfaces as an empty
        string there rather than NULL; both must leave props without the key.
        """
        _name, store = backend
        store.upsert_node("TextUnit", "n-space-3", {"text": "body"})

        rows = store.export_nodes()
        row = next(r for r in rows if (r["props"].get("id") == "n-space-3"))

        assert "space" not in row["props"]

    def test_falsy_props_space_is_filled_from_column(self, backend):
        """An empty/None space in props is treated as absent, not as a value.

        Pins the precedence _merge_space actually implements: only a *truthy*
        props value wins. An empty space would otherwise survive and keep
        reproducing the exact breakage this fold exists to fix.
        """
        _name, store = backend
        store.upsert_node(
            "TextUnit", "n-space-4", {"text": "body", "space": ""}, space_id="evidence"
        )

        rows = store.export_nodes()
        row = next(r for r in rows if (r["props"].get("id") == "n-space-4"))

        assert row["props"]["space"] == "evidence"


# ---------------------------------------------------------------------------
# The same fold must apply to the single-node reads, not just the bulk exports
#
# get_node is the funnel for _batch_node_props (and therefore find_neighbors'
# BFS) and for find_path; get_node_by_id backs lookup_node_type and
# ontology/impact.py. Leaving them unfolded kept the defect alive on those
# paths after export_nodes was fixed -- impact.py:126/:170 read props["space"]
# and fell back to a node-type label guess.
# ---------------------------------------------------------------------------


class TestSingleNodeReadsCarrySpace:
    def test_get_node_carries_space(self, backend):
        _name, store = backend
        store.upsert_node("TextUnit", "g-1", {"text": "body"}, space_id="evidence")

        assert store.get_node("TextUnit", "g-1")["space"] == "evidence"

    def test_get_node_by_id_carries_space(self, backend):
        _name, store = backend
        store.upsert_node("TextUnit", "g-2", {"text": "body"}, space_id="evidence")

        props = store.get_node_by_id("g-2")
        assert props["space"] == "evidence"
        assert props["node_type"] == "TextUnit"

    def test_find_neighbors_props_carry_space(self, backend):
        _name, store = backend
        store.upsert_node("Document", "g-anchor", {"title": "Doc"}, space_id="resource")
        store.upsert_node("TextUnit", "g-nb", {"text": "body"}, space_id="evidence")
        store.upsert_edge("Document", "g-anchor", "contains", "TextUnit", "g-nb")

        hits = store.find_neighbors("g-anchor", direction="both", depth=1, limit=10)
        nb = next(h for h in hits if (h.get("properties") or h).get("id") == "g-nb")
        props = nb.get("properties") or nb

        assert props["space"] == "evidence"

    def test_get_node_reflects_space_id_argument_over_conflicting_props_space(self, backend):
        """issue #118 codex review [2]: the explicit space_id ARGUMENT wins
        (see test_graph_protocol_contract.py::TestExportCarriesSpace's
        test_explicit_space_id_argument_overwrites_props_space for the
        export_nodes-level version of this same precedence pin)."""
        _name, store = backend
        store.upsert_node("TextUnit", "g-3", {"text": "b", "space": "claim"}, space_id="evidence")

        assert store.get_node("TextUnit", "g-3")["space"] == "evidence"


# ---------------------------------------------------------------------------
# Exhaustive sweep: EVERY node-returning read must carry space
#
# Two rounds of this fix were scoped by reasoning about call funnels rather than
# by enumerating the readers, and both times a sibling path survived: first the
# single-node reads (after export_* was fixed), then Kuzu's find_by_relations /
# find_path (the SQL backends route those through get_node, Kuzu issues its own
# Cypher). This sweep is the enumeration -- every method that returns node
# properties is exercised against the same fixture, so a newly added or
# refactored reader that forgets the fold fails here instead of silently
# degrading a consumer.
# ---------------------------------------------------------------------------


def _space_of(entry):
    """Node props out of whichever shape a reader returns."""
    if entry is None:
        return None
    props = entry.get("properties") or entry.get("props") or entry
    return props.get("space")


class TestEveryNodeReadCarriesSpace:
    @pytest.fixture
    def seeded(self, backend):
        _name, store = backend
        store.upsert_node("Document", "s-anchor", {"title": "Doc"}, space_id="resource")
        store.upsert_node("TextUnit", "s-far", {"text": "body"}, space_id="evidence")
        store.upsert_edge("Document", "s-anchor", "contains", "TextUnit", "s-far")
        return store

    def test_get_node(self, seeded):
        assert _space_of(seeded.get_node("TextUnit", "s-far")) == "evidence"

    def test_get_node_by_id(self, seeded):
        assert _space_of(seeded.get_node_by_id("s-far")) == "evidence"

    def test_find_neighbors(self, seeded):
        hits = seeded.find_neighbors("s-anchor", direction="both", depth=1, limit=10)
        far = next(h for h in hits if _space_of(h) is not None or True)
        assert _space_of(far) == "evidence"

    def test_find_by_relations(self, seeded):
        hits = seeded.find_by_relations("s-anchor", ["contains"], "out")
        assert hits, "edge should be reachable by relation"
        assert _space_of(hits[0]) == "evidence"

    def test_find_path(self, seeded):
        path = seeded.find_path("s-anchor", "s-far", max_depth=3)
        assert path, "path should exist"
        assert _space_of(path[-1]["node"]) == "evidence"

    def test_export_nodes(self, seeded):
        rows = seeded.export_nodes()
        row = next(r for r in rows if r["props"].get("id") == "s-far")
        assert row["props"]["space"] == "evidence"

    def test_export_edges(self, seeded):
        rows = seeded.export_edges()
        row = next(r for r in rows if r["target_props"].get("id") == "s-far")
        assert row["source_props"]["space"] == "resource"
        assert row["target_props"]["space"] == "evidence"
