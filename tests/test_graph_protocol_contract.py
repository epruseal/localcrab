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
        assert mock_session.run.call_args[1] == {"pack_id": "packA"}

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

    def test_explicit_props_space_is_not_overwritten_by_column(self, backend):
        """The column is a fallback, never an override."""
        _name, store = backend
        store.upsert_node(
            "TextUnit", "n-space-2", {"text": "body", "space": "claim"}, space_id="evidence"
        )

        rows = store.export_nodes()
        row = next(r for r in rows if (r["props"].get("id") == "n-space-2"))

        assert row["props"]["space"] == "claim"

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

    def test_get_node_does_not_override_explicit_space(self, backend):
        _name, store = backend
        store.upsert_node("TextUnit", "g-3", {"text": "b", "space": "claim"}, space_id="evidence")

        assert store.get_node("TextUnit", "g-3")["space"] == "claim"
