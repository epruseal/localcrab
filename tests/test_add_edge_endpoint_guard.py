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


@pytest.fixture
def builder(tmp_path):
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
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


def test_edge_between_existing_nodes_keeps_real_types(builder):
    b, graph = builder
    b.add_node("resource", "Document", "doc1", {"title": "Doc"})
    b.add_node("evidence", "TextUnit", "tu1", {"text": "body"})

    result = b.add_edge("resource", "doc1", "contains", "evidence", "tu1")

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
def test_missing_endpoint_is_refused_not_defaulted(builder, from_id, to_id, expected_missing):
    b, graph = builder
    b.add_node("resource", "Document", "doc1", {"title": "Doc"})
    b.add_node("evidence", "TextUnit", "tu1", {"text": "body"})

    result = b.add_edge("resource", from_id, "contains", "evidence", to_id)

    assert result["stores"]["graph"].startswith("no match")
    assert result["missing_nodes"] == [expected_missing]
    # The SQL registry must not list an edge the graph refused.
    assert result["stores"]["sql"] == "skipped (missing node)"
    # Nothing written -- in particular no "Project"/"User" space-default row.
    assert _edges(graph) == []


def test_both_endpoints_missing_reports_both_sides(builder):
    b, _ = builder
    result = b.add_edge("resource", "ghost-a", "contains", "evidence", "ghost-b")
    assert result["missing_nodes"] == ["resource/ghost-a", "evidence/ghost-b"]


def test_guard_is_inert_when_store_unavailable(tmp_path):
    """An unavailable store cannot distinguish 'absent' from 'down'.

    It writes nothing to the graph regardless, so the guard must not fire and
    must not block the SQL registry write.
    """

    class UnavailableGraph:
        available = False

        def lookup_node_type(self, node_id: str) -> str | None:  # soft guard
            return None

    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    b = OntologyBuilder(UnavailableGraph(), docs, sql)

    result = b.add_edge("subject", "u1", "owns", "resource", "p1")

    assert result["stores"]["graph"] == "unavailable"
    assert result["stores"]["sql"] == "ok"
    assert "missing_nodes" not in result
    docs.close()
