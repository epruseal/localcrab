"""Regression tests for OntologyBuilder.add_edge's endpoint-existence guard.

Defect this pins down (observed 2026-07-29 on a fresh local-mode deployment,
265 corrupted rows):

``add_edge`` resolved each endpoint's node type via ``lookup_node_type`` and
fell back to ``_space_to_default_type(space)`` whenever the lookup returned
None. A None lookup means *the node does not exist*, so the fallback invented a
type and the SQL backends wrote the edge anyway -- their ``upsert_edge`` is a
plain INSERT with no endpoint check, unlike ``Neo4jStore.upsert_edge`` which
uses MATCH and writes nothing.

The invented type was the space's first declared node type (resource ->
Project, subject -> User). Because ``graph_edges``' primary key includes
from_type/to_type, re-running the ingest could not correct those rows: they
persisted as dangling edges pointing at non-existent nodes.

The guard must therefore:
  - refuse the write when either endpoint is missing (both sides checked),
  - keep the real node types when both endpoints exist,
  - stay inert when the store cannot answer (unavailable), since nothing is
    written to the graph in that case anyway.
"""

from __future__ import annotations

import pytest

from opencrab.ontology.builder import OntologyBuilder
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore

# #148: add_node/add_edge now call current_principal() and
# authorize(sql, principal, pack_id) internally -- bind a fixed test
# principal for every test in this module (see conftest.py's
# bind_test_principal), matching tests/test_mcp.py's TestOntologyBuilder.
pytestmark = pytest.mark.usefixtures("bind_test_principal")


@pytest.fixture
def sql(tmp_path):
    return SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")


@pytest.fixture
def pack_id(sql):
    # The pack must be registered and owned by the bound principal
    # ("test-user") before any write here reaches the endpoint guard.
    from opencrab.pack.ownership import create_pack

    return create_pack(sql, "test-user", "guard-test-pack")


@pytest.fixture
def builder(tmp_path, sql):
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert graph.available
    yield OntologyBuilder(graph, docs, sql), graph
    graph.close()
    docs.close()


def _edges(graph: LocalGraphStore) -> list[tuple]:
    # row_factory is sqlite3.Row on this store, so normalise to plain tuples.
    with graph._conn as conn:  # noqa: SLF001 - direct read for assertion only
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT from_type, from_id, relation, to_type, to_id FROM graph_edges"
            )
        ]


def test_edge_between_existing_nodes_keeps_real_types(builder, pack_id):
    b, graph = builder
    b.add_node("resource", "Document", "doc1", {"title": "Doc"}, pack_id=pack_id)
    b.add_node("evidence", "TextUnit", "tu1", {"text": "body"}, pack_id=pack_id)

    result = b.add_edge("resource", "doc1", "contains", "evidence", "tu1", pack_id=pack_id)

    assert result["stores"]["graph"] == "ok"
    assert result["stores"]["sql"] == "ok"
    assert "missing_nodes" not in result
    # Real types, not the space defaults (resource -> Project, evidence -> TextUnit).
    assert _edges(graph) == [("Document", "doc1", "contains", "TextUnit", "tu1")]


@pytest.mark.parametrize(
    "from_id, to_id, expected_missing",
    [
        ("doc1", "ghost", "evidence/ghost"),
        ("ghost", "tu1", "resource/ghost"),
    ],
)
def test_missing_endpoint_is_refused_not_defaulted(builder, pack_id, from_id, to_id, expected_missing):
    b, graph = builder
    b.add_node("resource", "Document", "doc1", {"title": "Doc"}, pack_id=pack_id)
    b.add_node("evidence", "TextUnit", "tu1", {"text": "body"}, pack_id=pack_id)

    result = b.add_edge("resource", from_id, "contains", "evidence", to_id, pack_id=pack_id)

    assert result["stores"]["graph"].startswith("no match")
    assert result["missing_nodes"] == [expected_missing]
    # The SQL registry must not list an edge the graph refused.
    assert result["stores"]["sql"] == "skipped (missing node)"
    # Nothing written -- in particular no "Project"/"User" space-default row.
    assert _edges(graph) == []


def test_both_endpoints_missing_reports_both_sides(builder, pack_id):
    b, _ = builder
    result = b.add_edge("resource", "ghost-a", "contains", "evidence", "ghost-b", pack_id=pack_id)
    assert result["missing_nodes"] == ["resource/ghost-a", "evidence/ghost-b"]


def test_guard_is_inert_when_store_unavailable(tmp_path, sql, pack_id):
    """An unavailable store cannot distinguish 'absent' from 'down', so the
    endpoint guard itself must not fire (no `missing_nodes` key).

    #148 point 6: separately from the guard, a graph-unavailable write is now
    refused end-to-end -- the builder no longer falls through to the SQL
    registry when the graph (system of record) is down, so `sql` here is
    "skipped (graph unavailable)", not "ok" (this refusal is a fan-out-level
    decision made before the guard's own missing-endpoint check ever runs).
    """

    class UnavailableGraph:
        available = False

        def lookup_node_type(self, node_id: str) -> str | None:  # soft guard
            return None

    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    b = OntologyBuilder(UnavailableGraph(), docs, sql)

    result = b.add_edge("subject", "u1", "owns", "resource", "p1", pack_id=pack_id)

    assert result["stores"]["graph"] == "unavailable"
    assert result["stores"]["sql"] == "skipped (graph unavailable)"
    assert "missing_nodes" not in result
    docs.close()
