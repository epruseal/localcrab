"""
Extended contract tests for the MCP tool dispatch surface (opencrab/mcp/tools.py).

tests/test_mcp.py already covers the main dispatch paths (unknown-tool
KeyError, tools/list shape, ontology_manifest, ontology_add_node success/
validation-error). This module adds what is missing: the error-envelope
contract at the MCP boundary, the unknown-tool message contract, the READ
tool backend branches (ontology_get_node / ontology_list_nodes /
ontology_list_edges) against real local SQLite-backed stores, and empty-
argument dispatch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from opencrab.mcp.server import MCPServer
from opencrab.mcp.tools import (
    _context,
    dispatch_tool,
    ontology_get_node,
    ontology_list_edges,
    ontology_list_nodes,
)
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore


@pytest.fixture(autouse=True)
def _clear_context():
    """Every test in this module supplies its own context; never leak state."""
    _context.clear()
    yield
    _context.clear()


# ---------------------------------------------------------------------------
# Unknown tool — exact message contract
# ---------------------------------------------------------------------------


class TestUnknownToolContract:
    def test_message_lists_available_tools(self):
        from opencrab.mcp.tools import _TOOL_FUNCTIONS

        with pytest.raises(KeyError) as exc_info:
            dispatch_tool("does_not_exist", {})
        message = str(exc_info.value)
        assert "Unknown tool: 'does_not_exist'" in message
        assert "Available:" in message
        for name in _TOOL_FUNCTIONS:
            assert name in message


# ---------------------------------------------------------------------------
# Empty arguments dict dispatch
# ---------------------------------------------------------------------------


class TestEmptyArgumentsDispatch:
    def test_no_arg_tool_accepts_empty_dict(self):
        result = dispatch_tool("ontology_manifest", {})
        assert "spaces" in result

    def test_required_arg_tool_raises_type_error_on_empty_dict(self):
        with pytest.raises(TypeError):
            dispatch_tool("ontology_get_node", {})


# ---------------------------------------------------------------------------
# Error envelope contract — {"error": str(exc)} at the MCP boundary,
# regardless of which layer catches the exception.
# ---------------------------------------------------------------------------


class TestErrorEnvelopeContract:
    def _ctx_with_builder_raising(self, exc: Exception) -> dict:
        builder = MagicMock()
        builder.add_node.side_effect = exc
        builder.add_edge.side_effect = exc
        return {
            "builder": builder,
            "rebac": MagicMock(),
            "impact": MagicMock(),
            "hybrid": MagicMock(),
            "mongo": MagicMock(),
            "billing": MagicMock(),
        }

    def test_add_node_generic_exception_caught_internally(self):
        """ontology_add_node catches broadly and returns {"error": ...}
        directly from dispatch_tool — no "valid" key for non-ValueError
        (that key is only added on the ValueError branch)."""
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = self._ctx_with_builder_raising(
                RuntimeError("store write failed")
            )
            result = dispatch_tool(
                "ontology_add_node",
                {"space": "subject", "node_type": "User", "node_id": "u1"},
            )
        assert result == {"error": "store write failed"}

    def test_add_edge_generic_exception_caught_internally(self):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = self._ctx_with_builder_raising(
                RuntimeError("edge write failed")
            )
            result = dispatch_tool(
                "ontology_add_edge",
                {
                    "from_space": "subject", "from_id": "u1", "relation": "owns",
                    "to_space": "resource", "to_id": "r1",
                },
            )
        assert result == {"error": "edge write failed"}

    def test_get_node_exception_propagates_and_is_wrapped_at_mcp_boundary(self):
        """ontology_get_node has no internal try/except — an exception from
        the graph store propagates through dispatch_tool. The MCP boundary
        (MCPServer._handle_tools_call) is what catches it and produces the
        same {"error": str(exc)} shape, wrapped in the JSON-RPC "result"
        (not a JSON-RPC "error" — the tool call itself succeeded at the
        protocol level, only the tool's own result reports failure)."""
        graph = MagicMock()
        graph.get_node_by_id.side_effect = RuntimeError("db connection lost")
        # No run_cypher attr → the Neo4j Cypher branch is skipped, matching
        # a local/Kuzu-style backend that only exposes get_node_by_id.
        del graph.run_cypher

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            server = MCPServer()
            response = server.handle_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ontology_get_node", "arguments": {"node_id": "n1"}},
            })

        assert "error" not in response  # JSON-RPC level: succeeded
        content_text = response["result"]["content"][0]["text"]
        assert json.loads(content_text) == {"error": "db connection lost"}


# ---------------------------------------------------------------------------
# ontology_get_node — real LocalGraphStore backend
# ---------------------------------------------------------------------------


class TestOntologyGetNodeLocalBackend:
    @pytest.fixture
    def graph(self, tmp_path):
        store = LocalGraphStore(str(tmp_path / "graph.db"))
        yield store
        store.close()

    def test_normal_fetch_found(self, graph):
        graph.upsert_node("User", "u1", {"name": "Ada"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_get_node("u1")
        assert result["found"] is True
        assert result["node_id"] == "u1"
        assert result["node"]["node_type"] == "User"
        assert result["node"]["name"] == "Ada"

    def test_nonexistent_id_returns_found_false_not_error(self, graph):
        """Symmetric contract for the singular getter: a missing node is a
        normal (found: False) result, not an error dict — distinct from
        ontology_list_edges' backend-unavailable error, and intentional
        (the caller cannot otherwise distinguish "empty" from "missing")."""
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_get_node("does-not-exist")
        assert result == {"found": False, "node_id": "does-not-exist"}
        assert "error" not in result


# ---------------------------------------------------------------------------
# ontology_list_nodes — real LocalGraphStore + LocalSQLDocStore backends
# ---------------------------------------------------------------------------


class TestOntologyListNodesLocalBackend:
    @pytest.fixture
    def graph(self, tmp_path):
        store = LocalGraphStore(str(tmp_path / "graph.db"))
        yield store
        store.close()

    @pytest.fixture
    def docs(self, tmp_path):
        store = LocalSQLDocStore(str(tmp_path / "docs.db"))
        yield store
        store.close()

    def test_pack_id_filter_uses_graph_export_nodes(self, graph, docs):
        graph.upsert_node("Lever", "lev-1", {"pack_id": "pack-a"})
        graph.upsert_node("Lever", "lev-2", {"pack_id": "pack-b"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs}
            result = ontology_list_nodes(pack_id="pack-a")
        assert result["total"] == 1
        assert result["nodes"][0]["node_id"] == "lev-1"
        assert result["pack_id_filter"] == "pack-a"

    def test_no_pack_id_falls_back_to_doc_store(self, graph, docs):
        docs.upsert_node_doc("subject", "User", "u1", {"name": "Ada"})
        docs.upsert_node_doc("subject", "User", "u2", {"name": "Bob"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs}
            result = ontology_list_nodes()
        assert result["total"] == 2
        assert result["pack_id_filter"] is None

    def test_empty_listing_returns_empty_not_error(self, graph, docs):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs}
            result = ontology_list_nodes()
        assert result == {
            "nodes": [], "total": 0, "space_filter": None, "pack_id_filter": None,
        }

    def test_limit_boundary_caps_rows_but_not_total(self, graph, docs):
        """issue #54: `limit` caps the returned `nodes` page, but `total`
        must still report the true match count (5), not the page size (2).
        Before the fix, `total` was len(nodes) and this asserted total == 2
        -- exactly the bug the issue reported."""
        for i in range(5):
            graph.upsert_node("Lever", f"lev-{i}", {"pack_id": "pack-a"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs}
            result = ontology_list_nodes(pack_id="pack-a", limit=2)
        assert len(result["nodes"]) == 2
        assert result["total"] == 5


# ---------------------------------------------------------------------------
# ontology_list_edges — real LocalGraphStore backend
# ---------------------------------------------------------------------------


class TestOntologyListEdgesLocalBackend:
    @pytest.fixture
    def graph(self, tmp_path):
        store = LocalGraphStore(str(tmp_path / "graph.db"))
        yield store
        store.close()

    def test_pack_id_filter_uses_graph_export_edges(self, graph):
        graph.upsert_node("Lever", "lev-1", {"pack_id": "pack-a"})
        graph.upsert_node("Outcome", "out-1", {"pack_id": "pack-a"})
        graph.upsert_edge("Lever", "lev-1", "raises", "Outcome", "out-1", {"pack_id": "pack-a"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_list_edges(pack_id="pack-a")
        assert result["total"] == 1
        assert result["edges"][0]["relation"] == "raises"

    def test_empty_graph_listing_returns_empty_not_error(self, graph):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_list_edges()
        assert result == {"edges": [], "total": 0, "pack_id_filter": None}

    def test_export_edges_failure_without_run_cypher_reports_real_error(self):
        """Regression test for the fixed bug: when export_edges() raises and
        the backend offers no run_cypher fallback (local/Kuzu-only), the
        real exception message must surface — previously it was discarded
        in favour of the misleading generic "graph store unavailable"."""
        graph = MagicMock()
        graph.export_edges.side_effect = RuntimeError("disk I/O error")
        del graph.run_cypher

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_list_edges()

        assert result == {
            "edges": [], "total": 0, "error": "disk I/O error", "pack_id_filter": None,
        }

    def test_no_export_edges_and_no_run_cypher_reports_unavailable(self):
        """When the backend genuinely offers neither method, the generic
        "graph store unavailable" message is the correct (and only
        available) contract — distinct from the case above where a real
        error was being masked."""
        graph = MagicMock(spec=[])  # no export_edges, no run_cypher

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_list_edges()

        assert result == {
            "edges": [], "total": 0, "error": "graph store unavailable", "pack_id_filter": None,
        }


# ---------------------------------------------------------------------------
# ontology_list_edges — Neo4j now implements export_edges() natively (R5),
# so it returns the same wide shape as Local/PG/Kuzu instead of the old
# narrow run_cypher fallback shape ({from_id,relation,to_id,props}).
# ---------------------------------------------------------------------------


class TestOntologyListEdgesNeo4jWideShapeContract:
    def test_neo4j_like_backend_returns_wide_shape_not_narrow(self):
        """Pins the contract: every backend's export_edges() (including
        Neo4j's, added in R5) returns the wide shape
        {source_props,source_labels,target_props,target_labels,rel_props,
        relation} — ontology_list_edges must pass it through unchanged, never
        the old narrow Neo4j-only {from_id,relation,to_id,props} shape."""
        graph = MagicMock()
        graph.export_edges.return_value = [
            {
                "source_props": {"id": "a0"}, "source_labels": ["Doc"],
                "target_props": {"id": "a1"}, "target_labels": ["Doc"],
                "rel_props": {"pack_id": "pack-a"}, "relation": "rel",
            }
        ]

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_list_edges(pack_id="pack-a")

        assert result["total"] == 1
        edge = result["edges"][0]
        assert set(edge) == {
            "source_props", "source_labels", "target_props", "target_labels",
            "rel_props", "relation",
        }
        # pack_id/limit are forwarded verbatim to export_edges() — the
        # backend (all four now) owns the a/b/r x pack_id/source/source_id
        # filter widening, not this dispatch function.
        graph.export_edges.assert_called_once_with(pack_id="pack-a", limit=200)
        graph.run_cypher.assert_not_called()

    def test_export_edges_exception_does_not_fall_back_to_run_cypher(self):
        """Regression: before this fix, export_edges() raising fell through
        to a run_cypher narrow-shape query whenever the backend also exposed
        run_cypher (true of every real Neo4jStore) — silently returning a
        DIFFERENT edge shape as if it were a success instead of surfacing
        the real failure. Now that export_edges() is Neo4j's own native
        method (not a Local/Kuzu-only capability), an exception from it is
        always the real error — there is no more capability to fall back to."""
        graph = MagicMock()
        graph.export_edges.side_effect = RuntimeError("neo4j session closed")

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph}
            result = ontology_list_edges()

        assert result == {
            "edges": [], "total": 0, "error": "neo4j session closed", "pack_id_filter": None,
        }
        graph.run_cypher.assert_not_called()
