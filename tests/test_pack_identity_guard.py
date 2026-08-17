"""#146 P1(a): cross-pack identity-ownership guard.

``_ingest_into_pack``/``pack_create`` upsert nodes/edges/sources keyed by a
CALLER-supplied identity (node_id / edge endpoints / source_id), not a
server-generated key. Before this guard, a caller writing into their own
(writable) pack could silently overwrite a slot already attributed to a
DIFFERENT pack simply by naming the same identity -- an upsert with no
ownership check (#143 invariant 4). These tests reproduce the design doc's
"재현 테스트 (신규)" list for P1(a) (items 1-13; item 14 -- the get_edge
3-backend contract itself -- lives in test_graph_protocol_contract.py).

Uses a REAL LocalGraphStore for the graph slot wherever the test needs to
prove "the original row is truly unchanged" or exercise the real upsert/get
conflict keys -- a MagicMock graph would only prove the mock was or wasn't
called, not that a write actually landed or didn't. mongo/chroma/sql/builder
stay MagicMock (this module isn't testing THEIR write paths, only whether
the identity guard gates the call to ``builder.add_node``/``add_edge``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.mcp.tools import _ingest_into_pack
from opencrab.stores.local_graph_store import LocalGraphStore

pytestmark = pytest.mark.usefixtures("bind_test_principal")


def _ctx(graph, **overrides):
    """A ctx with a REAL graph store (caller-provided) and MagicMock
    everything else, pre-configured to the "no conflicting store" baseline
    (see test_tools_handlers_direct.py's _base_ctx for the same convention)
    so a test only has to override the ONE slot it wants to exercise."""
    builder = MagicMock()
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    builder.add_edge.return_value = {"stores": {"graph": "ok"}}
    mongo = MagicMock()
    mongo.available = True
    mongo.get_node_doc.return_value = None
    mongo.get_source.return_value = None
    chroma = MagicMock()
    chroma.available = True
    chroma.get_by_id.return_value = None
    ctx = {
        "neo4j": graph,
        "mongo": mongo,
        "chroma": chroma,
        "sql": MagicMock(),
        "builder": builder,
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    ctx.update(overrides)
    return ctx


@pytest.fixture
def graph(tmp_path):
    store = LocalGraphStore(str(tmp_path / "graph.db"))
    yield store
    store.close()


# ---------------------------------------------------------------------------
# 1. Foreign node identity -> rejected, nothing written, original row intact.
# ---------------------------------------------------------------------------


def test_foreign_node_identity_rejected(graph):
    graph.upsert_node("Entity", "e1", {"pack_id": "other-pack", "name": "orig"})
    before = graph.get_node("Entity", "e1")

    ctx = _ctx(graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["added_nodes"] == 0
    assert result["node_errors"] == [
        "e1: identity is already attributed to a different pack"
    ]
    ctx["builder"].add_node.assert_not_called()
    assert graph.get_node("Entity", "e1") == before


# ---------------------------------------------------------------------------
# 2. Same-pack identity -> passes (regression guard).
# ---------------------------------------------------------------------------


def test_same_pack_identity_passes(graph):
    graph.upsert_node("Entity", "e1", {"pack_id": "my-pack"})

    ctx = _ctx(graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["added_nodes"] == 1
    assert result["node_errors"] == []
    ctx["builder"].add_node.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Unattributed legacy row (no pack_id) -> passes.
# ---------------------------------------------------------------------------


def test_legacy_unattributed_node_passes(graph):
    graph.upsert_node("Entity", "e1", {})  # no pack_id at all

    ctx = _ctx(graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["added_nodes"] == 1
    assert result["node_errors"] == []


# ---------------------------------------------------------------------------
# 4. Same node_id, different node_type, foreign pack -> rejected.
#    get_node(type, id) misses (wrong type) but get_node_by_id(id) catches
#    it -- the Kuzu-MERGE-key / vector-slot reproduction case.
# ---------------------------------------------------------------------------


def test_same_node_id_different_type_foreign_pack_rejected(graph):
    graph.upsert_node("OtherType", "shared-id", {"pack_id": "other-pack"})

    ctx = _ctx(graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "shared-id"}],
        )

    assert result["added_nodes"] == 0
    assert result["node_errors"] == [
        "shared-id: identity is already attributed to a different pack"
    ]
    ctx["builder"].add_node.assert_not_called()


# ---------------------------------------------------------------------------
# 5. doc_nodes slot ALONE is foreign (properties.pack_id); graph/vector are
#    clean -> rejected (v2 결함 1 반례: extraction path must be
#    properties.pack_id, not top-level).
# ---------------------------------------------------------------------------


def test_doc_nodes_slot_only_foreign_rejected(graph):
    ctx = _ctx(graph)
    ctx["mongo"].get_node_doc.return_value = {"properties": {"pack_id": "other-pack"}}

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["added_nodes"] == 0
    assert result["node_errors"] == [
        "e1: identity is already attributed to a different pack"
    ]
    ctx["builder"].add_node.assert_not_called()


# ---------------------------------------------------------------------------
# 6. vector slot ALONE is foreign (metadata.pack_id) -> rejected.
# ---------------------------------------------------------------------------


def test_vector_slot_only_foreign_rejected(graph):
    ctx = _ctx(graph)
    ctx["chroma"].get_by_id.return_value = {"metadata": {"pack_id": "other-pack"}}

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["added_nodes"] == 0
    assert result["node_errors"] == [
        "e1: identity is already attributed to a different pack"
    ]
    ctx["builder"].add_node.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Foreign edge identity -> rejected, original edge properties unchanged.
# ---------------------------------------------------------------------------


def test_foreign_edge_identity_rejected(graph):
    graph.upsert_node("Entity", "a", {"pack_id": "my-pack"})
    graph.upsert_node("Entity", "b", {"pack_id": "my-pack"})
    graph.upsert_edge("Entity", "a", "related_to", "Entity", "b", {"pack_id": "other-pack"})
    before = graph.get_edge("Entity", "a", "related_to", "Entity", "b")

    ctx = _ctx(graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            edges=[{
                "from_space": "concept", "from_id": "a", "relation": "related_to",
                "to_space": "concept", "to_id": "b",
            }],
        )

    assert result["added_edges"] == 0
    assert result["edge_errors"] == [
        "a->b: edge identity is already attributed to a different pack"
    ]
    ctx["builder"].add_edge.assert_not_called()
    assert graph.get_edge("Entity", "a", "related_to", "Entity", "b") == before


# ---------------------------------------------------------------------------
# 8. source_id targets a foreign evidence/TextUnit node -> rejected,
#    stores["evidence_node"] carries the same message.
# ---------------------------------------------------------------------------


def test_foreign_evidence_textunit_rejected(graph):
    graph.upsert_node(
        "TextUnit", "victim-src", {"pack_id": "other-pack"}, space_id="evidence"
    )

    ctx = _ctx(graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack", text="hijack attempt", source_id="victim-src",
        )

    assert result["evidence_node"] is None
    assert result["node_errors"] == [
        "victim-src: identity is already attributed to a different pack"
    ]
    assert result["stores"]["evidence_node"] == (
        "victim-src: identity is already attributed to a different pack"
    )
    assert result["text_ingested"] is True  # attempted, same as any other node_errors case
    ctx["builder"].add_node.assert_not_called()


# ---------------------------------------------------------------------------
# 9. text_as_node=False + foreign source_id (doc_sources.metadata.pack_id)
#    -> rejected, hybrid.ingest AND mongo.upsert_source both skipped.
# ---------------------------------------------------------------------------


def test_legacy_text_path_foreign_source_rejected(graph):
    ctx = _ctx(graph)
    ctx["mongo"].get_source.return_value = {"metadata": {"pack_id": "other-pack"}}

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack", text="legacy text", source_id="victim-src", text_as_node=False,
        )

    assert result["text_ingested"] is False
    assert result["node_errors"] == [
        "victim-src: source identity is already attributed to a different pack"
    ]
    ctx["hybrid"].ingest.assert_not_called()
    ctx["mongo"].upsert_source.assert_not_called()


# ---------------------------------------------------------------------------
# 10. fail-closed: missing probe method / probe exception / non-dict return
#     -> each rejected, never silently treated as "no conflict".
# ---------------------------------------------------------------------------


def test_probe_method_missing_fails_closed(graph):
    """spec=["available"] makes every OTHER attribute raise AttributeError
    on access -- getattr(store, "get_node", None) must resolve that to
    "method absent", not propagate the AttributeError."""
    no_probe_graph = MagicMock(spec=["available"])
    no_probe_graph.available = True

    ctx = _ctx(no_probe_graph)
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["node_errors"] == [
        "e1: cannot verify existing ownership on this backend"
    ]
    ctx["builder"].add_node.assert_not_called()


def test_probe_exception_fails_closed(graph):
    ctx = _ctx(graph)
    ctx["mongo"].get_node_doc.side_effect = RuntimeError("mongo down")

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["node_errors"] == [
        "e1: cannot verify existing ownership on this backend"
    ]
    ctx["builder"].add_node.assert_not_called()


def test_probe_non_dict_return_fails_closed(graph):
    ctx = _ctx(graph)
    ctx["mongo"].get_node_doc.return_value = "not-a-dict"

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["node_errors"] == [
        "e1: cannot verify existing ownership on this backend"
    ]
    ctx["builder"].add_node.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Unavailable store -> its probe is skipped (never fail-closed, never
#     consulted for a conflict).
# ---------------------------------------------------------------------------


def test_unavailable_store_probe_is_skipped(graph):
    ctx = _ctx(graph)
    ctx["mongo"].available = False
    # If this were probed despite being unavailable, it would fail-closed
    # (an exception) -- proving it is instead skipped entirely.
    ctx["mongo"].get_node_doc.side_effect = RuntimeError("would fail-close if probed")

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "my-pack",
            nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1"}],
        )

    assert result["added_nodes"] == 1
    assert result["node_errors"] == []


# ---------------------------------------------------------------------------
# 12. Kuzu get_edge returns a parsed dict (not a raw JSON string), and a
#     same-pack edge re-ingest passes end-to-end through the real guard
#     (v2 결함 2 반례).
# ---------------------------------------------------------------------------


def test_kuzu_same_pack_edge_reingest_passes(tmp_path):
    pytest.importorskip("ladybug")
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    kuzu_graph = KuzuGraphStore(db_path=str(tmp_path / "graph_kuzu_reingest"))
    try:
        kuzu_graph.upsert_node("Entity", "a", {"pack_id": "my-pack"})
        kuzu_graph.upsert_node("Entity", "b", {"pack_id": "my-pack"})
        kuzu_graph.upsert_edge(
            "Entity", "a", "related_to", "Entity", "b", {"pack_id": "my-pack"}
        )

        ctx = _ctx(kuzu_graph)
        with patch("opencrab.mcp.tools._get_context", return_value=ctx):
            result = _ingest_into_pack(
                "my-pack",
                edges=[{
                    "from_space": "concept", "from_id": "a", "relation": "related_to",
                    "to_space": "concept", "to_id": "b",
                }],
            )

        assert result["added_edges"] == 1
        assert result["edge_errors"] == []
        ctx["builder"].add_edge.assert_called_once()
    finally:
        kuzu_graph.close()


# ---------------------------------------------------------------------------
# 13. pack_create + graph unavailable -> pre-blocked BEFORE the registry row
#     is created; no store written at all (v2 결함 4 반례).
# ---------------------------------------------------------------------------


def test_pack_create_graph_unavailable_blocks_before_registry_row(tmp_path):
    from opencrab.mcp.tools import pack_create
    from opencrab.pack.ownership import get_pack
    from opencrab.stores.sql_store import SQLStore

    sql = SQLStore("sqlite:///:memory:")
    unavailable_graph = MagicMock()
    unavailable_graph.available = False
    builder = MagicMock()
    ctx = {
        "neo4j": unavailable_graph,
        "mongo": MagicMock(),
        "chroma": MagicMock(),
        "sql": sql,
        "builder": builder,
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_create(title="Unavailable Graph", pack_id="unavail-pack")

    assert result == {"error": "graph store unavailable"}
    assert get_pack(sql, "unavail-pack") is None
    builder.add_node.assert_not_called()
