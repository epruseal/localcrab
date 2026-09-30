"""#428: MCP read tools must not disguise rows the store marked with
``property_decode_error`` as empty nodes or edges.

Corruption is a duplicate-key JSON object: still valid JSON (so the pack
scope predicate reaches the row) but rejected by ``decode_properties``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from opencrab.mcp.tools import ontology_get_node, ontology_list_edges, ontology_list_nodes
from opencrab.pack.ownership import create_pack
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.sql_store import SQLStore

pytestmark = pytest.mark.usefixtures("bind_test_principal")

_DUP = '{"pack_id": "pack-a", "name": "x", "name": "y"}'


@pytest.fixture
def graph(tmp_path):
    store = LocalGraphStore(str(tmp_path / "graph.db"))
    yield store
    store.close()


@pytest.fixture
def sql():
    from tests._pack_fixtures import ensure_test_user

    store = SQLStore("sqlite:///:memory:")
    ensure_test_user(store, "test-user")
    create_pack(store, "test-user", "pack-a")
    return store


def _corrupt_node(graph, node_id):
    graph._conn.execute(
        "UPDATE graph_nodes SET properties = ? WHERE node_id = ?", (_DUP, node_id)
    )
    graph._conn.commit()


def _corrupt_edge(graph, from_id):
    # Malformed JSON: an edge with no pack_id of its own still passes the
    # edge predicate (NULL pack_id is admitted), so it reaches the tool.
    graph._conn.execute(
        "UPDATE graph_edges SET properties = '{' WHERE from_id = ?", (from_id,)
    )
    graph._conn.commit()


def _call(fn, graph, sql, **kw):
    with patch("opencrab.mcp.tools._get_context") as ctx:
        ctx.return_value = {"neo4j": graph, "sql": sql}
        return fn(**kw)


def _seed(graph):
    graph.upsert_node("Lever", "good-1", {"pack_id": "pack-a"})
    graph.upsert_node("Lever", "bad-1", {"pack_id": "pack-a"})
    graph.upsert_node("Lever", "good-2", {"pack_id": "pack-a"})


def test_normal_store_has_no_count_key(graph, sql):
    _seed(graph)
    res = _call(ontology_list_nodes, graph, sql)
    assert sorted(n["node_id"] for n in res["nodes"]) == ["bad-1", "good-1", "good-2"]
    assert "property_decode_error_count" not in res


def test_list_nodes_drops_corrupt_row_and_reports_count(graph, sql):
    _seed(graph)
    _corrupt_node(graph, "bad-1")
    res = _call(ontology_list_nodes, graph, sql)
    ids = sorted(n["node_id"] for n in res["nodes"])
    assert ids == ["good-1", "good-2"]  # control rows intact, no empty node
    assert all(n["node_id"] and n["node_type"] for n in res["nodes"])
    assert res["property_decode_error_count"] == 1
    assert res["total"] == 3


def test_list_nodes_all_corrupt(graph, sql):
    _seed(graph)
    for nid in ("good-1", "bad-1", "good-2"):
        _corrupt_node(graph, nid)
    res = _call(ontology_list_nodes, graph, sql)
    assert res["nodes"] == []
    assert res["property_decode_error_count"] == 3


def test_get_node_flags_corrupt_row_at_top_level(graph, sql):
    _seed(graph)
    _corrupt_node(graph, "bad-1")
    bad = _call(ontology_get_node, graph, sql, node_id="bad-1")
    assert bad["found"] is True
    assert bad["property_decode_error"] is True
    good = _call(ontology_get_node, graph, sql, node_id="good-1")
    assert "property_decode_error" not in good


def test_list_edges_drops_corrupt_edge_and_reports_count(graph, sql):
    _seed(graph)
    graph.upsert_edge("Lever", "good-1", "raises", "Lever", "good-2", {"pack_id": "pack-a"})
    graph.upsert_edge("Lever", "bad-1", "raises", "Lever", "good-2", {"pack_id": "pack-a"})
    clean = _call(ontology_list_edges, graph, sql)
    assert clean["total"] == 2
    assert "property_decode_error_count" not in clean
    _corrupt_edge(graph, "bad-1")
    res = _call(ontology_list_edges, graph, sql)
    assert res["total"] == 1
    survivor = next(
        e for e in clean["edges"] if e["source_props"].get("node_id") == "good-1"
        or e["source_props"].get("id") == "good-1"
    )
    assert res["edges"] == [survivor]
    assert not any(e.get("property_decode_error") for e in res["edges"])
    assert res["property_decode_error_count"] == 1
