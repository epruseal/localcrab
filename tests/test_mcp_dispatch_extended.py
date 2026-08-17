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
from opencrab.pack.ownership import create_pack
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore

# #145: dispatch_tool() now calls current_principal() for every tool
# (read or write); bind a fixed test principal for every test in this
# module (see conftest.py's bind_test_principal).
pytestmark = pytest.mark.usefixtures("bind_test_principal")


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
        # #147: ontology_get_node calls get_node_by_id_scoped, not
        # get_node_by_id.
        graph.get_node_by_id_scoped.side_effect = RuntimeError("db connection lost")
        # No run_cypher attr → the Neo4j Cypher branch is skipped, matching
        # a local/Kuzu-style backend that only exposes get_node_by_id.
        del graph.run_cypher

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            # #147: _current_read_scope(ctx) needs a real SQLStore under
            # ctx["sql"]. No pack needs to be registered here -- the scope
            # value is opaque to graph (a MagicMock) and the RuntimeError
            # fires unconditionally.
            mock_ctx.return_value = {"neo4j": graph, "sql": SQLStore("sqlite:///:memory:")}
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

    @pytest.fixture
    def sql(self):
        # #147: ontology_get_node derives its read scope from ctx["sql"] +
        # current_principal() (bind_test_principal binds "test-user"), then
        # calls get_node_by_id_scoped(node_id, sorted(scope)) -- a node with
        # no pack_id, or one owned by nobody in scope, is structurally
        # unreachable. "pack-a" owned by test-user keeps the fixture data
        # below readable under the new scoped lookup.
        store = SQLStore("sqlite:///:memory:")
        create_pack(store, "test-user", "pack-a")
        return store

    def test_normal_fetch_found(self, graph, sql):
        graph.upsert_node("User", "u1", {"name": "Ada", "pack_id": "pack-a"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "sql": sql}
            result = ontology_get_node("u1")
        assert result["found"] is True
        assert result["node_id"] == "u1"
        assert result["node"]["node_type"] == "User"
        assert result["node"]["name"] == "Ada"

    def test_nonexistent_id_returns_found_false_not_error(self, graph, sql):
        """Symmetric contract for the singular getter: a missing node is a
        normal (found: False) result, not an error dict — distinct from
        ontology_list_edges' backend-unavailable error, and intentional
        (the caller cannot otherwise distinguish "empty" from "missing")."""
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "sql": sql}
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

    @pytest.fixture
    def sql(self):
        # #147: ontology_list_nodes derives its read scope from ctx["sql"] +
        # current_principal() ("test-user", via bind_test_principal). Both
        # the pack_id-given branch (export_nodes_scoped/count_exported_nodes_scoped)
        # and the pack_id-omitted branch (mongo.list_nodes_scoped) now
        # require the fixture data's pack_id to be owned by (or public to)
        # the bound principal, or it is structurally unreadable regardless
        # of what the test seeded.
        store = SQLStore("sqlite:///:memory:")
        create_pack(store, "test-user", "pack-a")
        return store

    def test_pack_id_filter_uses_graph_export_nodes(self, graph, docs, sql):
        graph.upsert_node("Lever", "lev-1", {"pack_id": "pack-a"})
        graph.upsert_node("Lever", "lev-2", {"pack_id": "pack-b"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs, "sql": sql}
            result = ontology_list_nodes(pack_id="pack-a")
        assert result["total"] == 1
        assert result["nodes"][0]["node_id"] == "lev-1"
        assert result["pack_id_filter"] == "pack-a"

    def test_no_pack_id_falls_back_to_doc_store(self, graph, docs, sql):
        # #147: no pack_id argument means "everything test-user can read",
        # not "everything in the store" -- list_nodes_scoped excludes rows
        # with no pack_id (#143 invariant 5), so both docs need one, owned
        # by the bound principal, to still be visible under the new scoped
        # lookup. This still exercises what the test's name asserts (the
        # doc store, not the graph store, answers the no-pack_id case).
        docs.upsert_node_doc("subject", "User", "u1", {"name": "Ada", "pack_id": "pack-a"})
        docs.upsert_node_doc("subject", "User", "u2", {"name": "Bob", "pack_id": "pack-a"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs, "sql": sql}
            result = ontology_list_nodes()
        assert result["total"] == 2
        assert result["pack_id_filter"] is None

    def test_empty_listing_returns_empty_not_error(self, graph, docs, sql):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs, "sql": sql}
            result = ontology_list_nodes()
        assert result == {
            "nodes": [], "total": 0, "space_filter": None, "pack_id_filter": None,
        }

    def test_limit_boundary_caps_rows_but_not_total(self, graph, docs, sql):
        """issue #54: `limit` caps the returned `nodes` page, but `total`
        must still report the true match count (5), not the page size (2).
        Before the fix, `total` was len(nodes) and this asserted total == 2
        -- exactly the bug the issue reported."""
        for i in range(5):
            graph.upsert_node("Lever", f"lev-{i}", {"pack_id": "pack-a"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs, "sql": sql}
            result = ontology_list_nodes(pack_id="pack-a", limit=2)
        assert len(result["nodes"]) == 2
        assert result["total"] == 5

    def test_space_mismatch_no_longer_desyncs_total_from_nodes(self, graph, docs, sql):
        """Issue #118: upsert_node used to store space_id (column) and
        properties["space"] (JSON) independently. count_exported_nodes
        counted by the COLUMN; export_nodes returned that same row but
        _merge_space reported whatever properties["space"] literally said
        back out; this tool's own post-filter (`n_space != cleaned_space`
        below) then dropped rows whose reported space didn't match --
        producing exactly the `total: N, nodes: []`-shaped split the issue
        reports (here: total=4 from the column-only count, but only 1 row
        surviving the post-filter, since 3 of the 4 were mislabeled).

        ``limit=10`` (not capped to the seeded count) so the only possible
        source of a total/len(nodes) mismatch is this correctness bug, not
        ordinary limit truncation (already covered by the pre-existing
        #54 `test_limit_boundary_caps_rows_but_not_total`).
        """
        for i in range(3):
            graph.upsert_node(
                "Lever", f"mis-{i}", {"pack_id": "pack-a", "space": "other"}, space_id="target"
            )
        graph.upsert_node("Lever", "real-1", {"pack_id": "pack-a"}, space_id="target")

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "mongo": docs, "sql": sql}
            result = ontology_list_nodes(pack_id="pack-a", space="target", limit=10)

        assert result["total"] == len(result["nodes"]) == 4
        assert all(n["space"] == "target" for n in result["nodes"])


# ---------------------------------------------------------------------------
# ontology_list_edges — real LocalGraphStore backend
# ---------------------------------------------------------------------------


class TestOntologyListEdgesLocalBackend:
    @pytest.fixture
    def graph(self, tmp_path):
        store = LocalGraphStore(str(tmp_path / "graph.db"))
        yield store
        store.close()

    @pytest.fixture
    def sql(self):
        # #147: ontology_list_edges derives its read scope the same way
        # ontology_list_nodes does; "pack-a" owned by test-user keeps this
        # class's fixture data visible under export_edges_scoped.
        store = SQLStore("sqlite:///:memory:")
        create_pack(store, "test-user", "pack-a")
        return store

    def test_pack_id_filter_uses_graph_export_edges(self, graph, sql):
        graph.upsert_node("Lever", "lev-1", {"pack_id": "pack-a"})
        graph.upsert_node("Outcome", "out-1", {"pack_id": "pack-a"})
        graph.upsert_edge("Lever", "lev-1", "raises", "Outcome", "out-1", {"pack_id": "pack-a"})
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "sql": sql}
            result = ontology_list_edges(pack_id="pack-a")
        assert result["total"] == 1
        assert result["edges"][0]["relation"] == "raises"

    def test_empty_graph_listing_returns_empty_not_error(self, graph, sql):
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "sql": sql}
            result = ontology_list_edges()
        assert result == {"edges": [], "total": 0, "pack_id_filter": None}

    def test_export_edges_failure_without_run_cypher_reports_real_error(self):
        """Regression test for the fixed bug: when export_edges_scoped()
        raises and the backend offers no run_cypher fallback (local/Kuzu-
        only), the real exception message must surface — previously it was
        discarded in favour of the misleading generic "graph store
        unavailable"."""
        graph = MagicMock()
        graph.export_edges_scoped.side_effect = RuntimeError("disk I/O error")
        del graph.run_cypher

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            # No pack needs to be registered: the RuntimeError fires
            # unconditionally, regardless of the (opaque, to a MagicMock)
            # scope value passed in.
            mock_ctx.return_value = {"neo4j": graph, "sql": SQLStore("sqlite:///:memory:")}
            result = ontology_list_edges()

        assert result == {
            "edges": [], "total": 0, "error": "disk I/O error", "pack_id_filter": None,
        }

    # #147 INTENTIONALLY FLIPPED PIN (see DESIGN.md §3.7 -- listed in the PR
    # body's flipped-pin list): the old hasattr()-style graceful fallback
    # this test used to pin ("backend genuinely lacks export_edges/run_cypher
    # -> generic {'error': 'graph store unavailable'} dict") no longer
    # exists. opencrab/mcp/tools/graph.py::ontology_list_edges now re-raises
    # AttributeError instead of folding a missing export_edges_scoped() into
    # the generic except-Exception branch, precisely so a backend wiring
    # defect surfaces loudly instead of being silently absorbed as "0 edges,
    # unavailable". The old contract described a state ("we return a result
    # dict when the method is missing") that is no longer true; asserting it
    # would just re-encode the bug #147 fixed. Renamed to describe the new
    # contract.
    def test_missing_export_edges_scoped_raises_attribute_error(self):
        """A backend that lacks export_edges_scoped() entirely is a wiring
        defect, not a normal "no edges" case -- it must raise, not be
        swallowed into the generic unavailable-dict shape (DESIGN.md §3.7)."""
        graph = MagicMock(spec=[])  # no export_edges_scoped, no run_cypher
        sql = SQLStore("sqlite:///:memory:")

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "sql": sql}
            with pytest.raises(AttributeError):
                ontology_list_edges()


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
        graph.export_edges_scoped.return_value = [
            {
                "source_props": {"id": "a0"}, "source_labels": ["Doc"],
                "target_props": {"id": "a1"}, "target_labels": ["Doc"],
                "rel_props": {"pack_id": "pack-a"}, "relation": "rel",
            }
        ]

        sql = SQLStore("sqlite:///:memory:")
        create_pack(sql, "test-user", "pack-a")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = {"neo4j": graph, "sql": sql}
            result = ontology_list_edges(pack_id="pack-a")

        assert result["total"] == 1
        edge = result["edges"][0]
        assert set(edge) == {
            "source_props", "source_labels", "target_props", "target_labels",
            "rel_props", "relation",
        }
        # #147: ontology_list_edges now forwards the narrowed effective
        # scope (a concrete list) positionally, not pack_id= by keyword —
        # the backend (all four now) owns the a/b/r x pack_id filter
        # widening, not this dispatch function.
        graph.export_edges_scoped.assert_called_once_with(["pack-a"], limit=200)
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
        graph.export_edges_scoped.side_effect = RuntimeError("neo4j session closed")

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            # No pack_id argument and no pack registered: effective scope is
            # [], but export_edges_scoped's side_effect fires unconditionally
            # regardless of the (opaque, to a MagicMock) list passed in.
            mock_ctx.return_value = {"neo4j": graph, "sql": SQLStore("sqlite:///:memory:")}
            result = ontology_list_edges()

        assert result == {
            "edges": [], "total": 0, "error": "neo4j session closed", "pack_id_filter": None,
        }
        graph.run_cypher.assert_not_called()
