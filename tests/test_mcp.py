"""
Tests for the MCP server and tool dispatcher.

All tests mock the underlying stores so no live services are required.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Tool dispatcher tests
# ---------------------------------------------------------------------------


class TestToolDispatch:
    def test_dispatch_unknown_tool_raises(self):
        from opencrab.mcp.tools import dispatch_tool

        with pytest.raises(KeyError, match="Unknown tool"):
            dispatch_tool("nonexistent_tool", {})

    def test_tools_list_not_empty(self):
        from opencrab.mcp.tools import TOOLS

        # 16 exposed tools after reorder + dedup + 3 new READ tools.
        # 비노출(주석처리): query_bm25, rebac, workflow×2, approval, billing×2,
        #   identity×5, canonicalize×2, promotion×4, ontology_extract, ontology_ingest
        assert len(TOOLS) == 16
        names = [t["name"] for t in TOOLS]
        # Core exposed
        assert "ontology_manifest" in names
        assert "ontology_add_node" in names
        assert "ontology_add_edge" in names
        assert "ontology_query" in names
        assert "ontology_impact" in names
        assert "ontology_lever_simulate" in names
        assert "harness_promotion_apply" in names
        assert "pack_create" in names
        assert "pack_ingest" in names
        assert "content_pack_list" in names
        # New READ tools
        assert "ontology_get_node" in names
        assert "ontology_list_nodes" in names
        assert "ontology_list_edges" in names
        # Soft-removed (비노출): functions importable but not dispatched
        assert "query_bm25" not in names
        assert "ontology_rebac_check" not in names
        assert "ontology_ingest" not in names
        assert "ontology_extract" not in names
        assert "identity_add_alias" not in names
        assert "promotion_promote" not in names
        assert "billing_get_usage" not in names
        assert "workflow_create_run" not in names
        assert "approval_request" not in names

    def test_tools_have_required_schema_keys(self):
        from opencrab.mcp.tools import TOOLS

        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            schema = tool["inputSchema"]
            assert "type" in schema
            assert "properties" in schema

    def test_ontology_manifest_returns_grammar(self):
        from opencrab.mcp.tools import dispatch_tool

        result = dispatch_tool("ontology_manifest", {})
        assert "spaces" in result
        assert "meta_edges" in result
        assert "impact_categories" in result
        assert "rebac" in result

    def test_ontology_add_node_validation_error(self):
        """Adding a node with invalid space returns an error dict (no exception)."""
        from opencrab.mcp.tools import _context, dispatch_tool

        # Clear context so it re-initialises with mocked stores
        _context.clear()

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            builder.add_node.side_effect = ValueError("Unknown space 'badspace'.")
            mock_ctx.return_value = {
                "builder": builder,
                "rebac": MagicMock(),
                "impact": MagicMock(),
                "hybrid": MagicMock(),
                "mongo": MagicMock(),
                "billing": MagicMock(),
            }

            result = dispatch_tool("ontology_add_node", {
                "space": "badspace", "node_type": "User", "node_id": "u1"
            })
            assert "error" in result
            assert result.get("valid") is False

    def test_ontology_add_node_success(self):
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            builder.add_node.return_value = {
                "node_id": "u1", "space": "subject", "node_type": "User",
                "properties": {}, "stores": {"neo4j": "ok"}
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_add_node", {
                "space": "subject", "node_type": "User", "node_id": "u1",
                "properties": {"name": "Alice", "email": "alice@example.com", "role": "admin"}
            })
            assert result["node_id"] == "u1"
            assert "stores" in result

    def test_ontology_add_edge_success(self):
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            builder.add_edge.return_value = {
                "from": {"space": "subject", "id": "u1"},
                "relation": "owns",
                "to": {"space": "resource", "id": "doc1"},
                "stores": {"neo4j": "ok"},
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "owns",
                "to_space": "resource", "to_id": "doc1",
            })
            assert result["relation"] == "owns"

    def test_ontology_add_edge_invalid_relation(self):
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            builder.add_edge.side_effect = ValueError("Relation 'mentions' is not valid")
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "mentions",
                "to_space": "resource", "to_id": "doc1",
            })
            assert "error" in result
            assert result.get("valid") is False

    def test_ontology_query_returns_results(self):
        from opencrab.mcp.tools import dispatch_tool
        from opencrab.ontology.query import QueryResult

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_result = QueryResult(
                source="vector", node_id="n1", score=0.9, text="Test text", metadata={}
            )
            hybrid = MagicMock()
            hybrid.query.return_value = [mock_result]
            mock_ctx.return_value = {
                "builder": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": hybrid, "mongo": MagicMock(),
                "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_query", {"question": "What is a lever?"})
            assert "results" in result
            assert result["total"] == 1
            assert result["results"][0]["node_id"] == "n1"

    def test_ontology_impact_returns_analysis(self):
        from opencrab.mcp.tools import dispatch_tool
        from opencrab.ontology.impact import ImpactResult

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_impact = ImpactResult(
                node_id="n1", change_type="update", space="concept", node_type="Concept",
                triggered=[{"id": "I1", "name": "Data impact"}],
                summary="Test summary",
            )
            impact_engine = MagicMock()
            impact_engine.analyse.return_value = mock_impact
            mock_ctx.return_value = {
                "builder": MagicMock(), "rebac": MagicMock(),
                "impact": impact_engine, "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_impact", {"node_id": "n1", "change_type": "update"})
            assert result["node_id"] == "n1"
            assert len(result["triggered_impacts"]) == 1

    def test_ontology_rebac_check_not_exposed_via_mcp(self):
        """ontology_rebac_check는 MCP 비노출 (현재 워크플로 미사용). 함수 본체는 보존."""
        from opencrab.mcp.tools import dispatch_tool, ontology_rebac_check  # noqa: F401

        assert callable(ontology_rebac_check)
        with pytest.raises(KeyError, match="Unknown tool"):
            dispatch_tool("ontology_rebac_check", {
                "subject_id": "u1", "permission": "view", "resource_id": "doc1"
            })

    def test_ontology_lever_simulate(self):
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            impact_engine = MagicMock()
            impact_engine.lever_simulate.return_value = {
                "lever_id": "lev1", "direction": "raises", "magnitude": 0.8,
                "predicted_outcome_changes": [], "confidence": 0.86,
            }
            mock_ctx.return_value = {
                "builder": MagicMock(), "rebac": MagicMock(),
                "impact": impact_engine, "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_lever_simulate", {
                "lever_id": "lev1", "direction": "raises", "magnitude": 0.8
            })
            assert result["lever_id"] == "lev1"
            assert result["confidence"] == 0.86

    def test_ontology_ingest_not_exposed_via_mcp(self):
        """ontology_ingest is no longer dispatched via MCP (pack_ingest로 일원화).
        Function body is retained in tools.py but removed from _TOOL_FUNCTIONS."""
        from opencrab.mcp.tools import dispatch_tool, ontology_ingest  # noqa: F401

        # Function body must still be importable (code preserved)
        assert callable(ontology_ingest)

        # MCP dispatch must raise (not in _TOOL_FUNCTIONS)
        with pytest.raises(KeyError, match="Unknown tool"):
            dispatch_tool("ontology_ingest", {"text": "t", "source_id": "s"})

    def test_ontology_extract_not_exposed_via_mcp(self):
        """ontology_extract is no longer dispatched via MCP.
        Function body is retained in tools.py but removed from _TOOL_FUNCTIONS."""
        from opencrab.mcp.tools import dispatch_tool, ontology_extract  # noqa: F401

        assert callable(ontology_extract)
        with pytest.raises(KeyError, match="Unknown tool"):
            dispatch_tool("ontology_extract", {"text": "t", "source_id": "s"})

    def test_pack_ingest_text_creates_evidence_node(self):
        """pack_ingest with text materialises an evidence/TextUnit node via builder.add_node."""
        from opencrab.mcp.tools import dispatch_tool

        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            builder = MagicMock()
            hybrid = MagicMock()
            hybrid.invalidate_bm25_cache = MagicMock()
            mongo = MagicMock()
            mongo.available = False
            mock_ctx.return_value = {
                "builder": builder,
                "hybrid": hybrid,
                "mongo": mongo,
                "rebac": MagicMock(),
                "impact": MagicMock(),
                "billing": MagicMock(),
            }
            mock_list.return_value = {"packs": [{"pack_id": "test-pack", "title": "Test"}]}

            result = dispatch_tool("pack_ingest", {
                "pack_id": "test-pack",
                "text": "대화 중 발생한 인사이트.",
                "title": "conv-2026-05-31",
            })

            assert result["status"] == "ok"
            assert result["evidence_node"] is not None
            assert result["added_nodes"] == 1

            # builder.add_node must have been called with evidence/TextUnit
            call_kwargs = builder.add_node.call_args
            assert call_kwargs is not None
            args = call_kwargs[1] if call_kwargs[1] else {}
            if not args:
                args = {
                    "space": call_kwargs[0][0],
                    "node_type": call_kwargs[0][1],
                    "node_id": call_kwargs[0][2],
                }
            assert builder.add_node.call_args.kwargs.get("space") == "evidence" or \
                   builder.add_node.call_args[0][0] == "evidence"
            assert builder.add_node.call_args.kwargs.get("node_type") == "TextUnit" or \
                   builder.add_node.call_args[0][1] == "TextUnit"

            # hybrid.ingest must NOT have been called (text_as_node=True skips vector-only path)
            hybrid.ingest.assert_not_called()

    def test_pack_ingest_text_as_node_false_legacy(self):
        """pack_ingest with text_as_node=False uses legacy vector-only path."""
        from opencrab.mcp.tools import dispatch_tool

        with (
            patch("opencrab.mcp.tools._get_context") as mock_ctx,
            patch("opencrab.mcp.tools.content_pack_list") as mock_list,
        ):
            builder = MagicMock()
            hybrid = MagicMock()
            hybrid.ingest.return_value = {"stores": {"chromadb": "ok"}}
            hybrid.invalidate_bm25_cache = MagicMock()
            mongo = MagicMock()
            mongo.available = False
            mock_ctx.return_value = {
                "builder": builder,
                "hybrid": hybrid,
                "mongo": mongo,
                "rebac": MagicMock(),
                "impact": MagicMock(),
                "billing": MagicMock(),
            }
            mock_list.return_value = {"packs": [{"pack_id": "test-pack", "title": "Test"}]}

            result = dispatch_tool("pack_ingest", {
                "pack_id": "test-pack",
                "text": "레거시 벡터 경로 테스트.",
                "text_as_node": False,
            })

            assert result["status"] == "ok"
            assert result["evidence_node"] is None
            # legacy path: hybrid.ingest called, builder.add_node NOT called for text
            hybrid.ingest.assert_called_once()
            builder.add_node.assert_not_called()


    def test_ontology_get_node_found(self):
        """ontology_get_node returns found=True when graph store returns a node."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            graph = MagicMock()
            graph.get_node_by_id.return_value = {
                "node_id": "dataset:test", "node_type": "Dataset",
                "space": "resource", "pack_id": "test",
            }
            mock_ctx.return_value = {
                "neo4j": graph, "builder": MagicMock(), "hybrid": MagicMock(),
                "mongo": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_get_node", {"node_id": "dataset:test"})
            assert result["found"] is True
            assert result["node_id"] == "dataset:test"
            assert "node" in result

    def test_ontology_get_node_not_found(self):
        """ontology_get_node returns found=False when node does not exist."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            graph = MagicMock()
            graph.get_node_by_id.return_value = None
            mock_ctx.return_value = {
                "neo4j": graph, "builder": MagicMock(), "hybrid": MagicMock(),
                "mongo": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_get_node", {"node_id": "nonexistent"})
            assert result["found"] is False

    def test_ontology_list_nodes_pack_filter(self):
        """ontology_list_nodes filters by pack_id in Python."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            # pack_id 있을 때는 graph.export_nodes(pack_id=...) 경로 사용
            # (limit-before-filter 버그 회피용 인덱스 쿼리)
            graph = MagicMock()
            graph.export_nodes.return_value = [
                {"props": {"node_id": "n1", "pack_id": "pack-a", "space": "evidence"}, "labels": ["TextUnit"]},
                {"props": {"node_id": "n3", "pack_id": "pack-a", "space": "concept"}, "labels": ["Entity"]},
            ]
            mongo = MagicMock()
            mock_ctx.return_value = {
                "neo4j": graph, "mongo": mongo, "builder": MagicMock(),
                "hybrid": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_list_nodes", {"pack_id": "pack-a"})
            assert result["total"] == 2
            assert result["pack_id_filter"] == "pack-a"
            graph.export_nodes.assert_called_once_with(pack_id="pack-a", limit=100)
            mongo.list_nodes.assert_not_called()  # doc store는 pack_id 있을 때 사용 안 함

    def test_ontology_list_edges_local_backend(self):
        """ontology_list_edges uses export_edges on Local/Kuzu backends."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            graph = MagicMock()
            graph.export_edges.return_value = [
                {"from_id": "n1", "relation": "related_to", "to_id": "n2"},
            ]
            mock_ctx.return_value = {
                "neo4j": graph, "mongo": MagicMock(), "builder": MagicMock(),
                "hybrid": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "billing": MagicMock(),
            }
            result = dispatch_tool("ontology_list_edges", {"pack_id": "test-pack"})
            assert result["total"] == 1
            assert result["pack_id_filter"] == "test-pack"
            graph.export_edges.assert_called_once_with(pack_id="test-pack", limit=200)


# ---------------------------------------------------------------------------
# MCP Server protocol tests
# ---------------------------------------------------------------------------


class TestMCPServer:
    @pytest.fixture
    def server(self):
        from opencrab.mcp.server import MCPServer

        with patch("opencrab.mcp.server.get_settings") as mock_cfg:
            mock_cfg.return_value = MagicMock(
                mcp_server_name="opencrab-test",
                mcp_server_version="0.0.1",
            )
            return MCPServer()

    def test_handle_parse_error(self, server):
        response = server._handle_raw("not json {{{")
        assert response["error"]["code"] == -32700  # PARSE_ERROR

    def test_handle_missing_method(self, server):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "params": {}})
        response = server._handle_raw(request)
        assert response["error"]["code"] == -32600  # INVALID_REQUEST

    def test_handle_unknown_method(self, server):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "unknown/method"})
        response = server._handle_raw(request)
        assert response["error"]["code"] == -32601  # METHOD_NOT_FOUND

    def test_handle_initialize(self, server):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        response = server._handle_raw(request)
        assert response["id"] == 1
        result = response["result"]
        assert "protocolVersion" in result
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "opencrab-test"
        assert "capabilities" in result
        assert "tools" in result["capabilities"]

    def test_handle_tools_list(self, server):
        request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response = server._handle_raw(request)
        assert response["id"] == 2
        assert "tools" in response["result"]
        tools = response["result"]["tools"]
        assert len(tools) == 16  # 재정렬 후 16개 (비노출 주석처리 + READ 3개 신규)

    def test_handle_tools_call_manifest(self, server):
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "ontology_manifest",
                "arguments": {},
            },
        })
        response = server._handle_raw(request)
        assert response["id"] == 3
        assert "content" in response["result"]
        content = response["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"

        # The text should be valid JSON containing the grammar
        grammar = json.loads(content[0]["text"])
        assert "spaces" in grammar
        assert "meta_edges" in grammar

    def test_handle_tools_call_missing_name(self, server):
        request = json.dumps({
            "jsonrpc": "2.0", "id": 4,
            "method": "tools/call",
            "params": {"arguments": {}},
        })
        response = server._handle_raw(request)
        # Missing name → invalid params or internal error
        assert "error" in response

    def test_handle_tools_call_unknown_tool(self, server):
        request = json.dumps({
            "jsonrpc": "2.0", "id": 5,
            "method": "tools/call",
            "params": {"name": "unknown_tool", "arguments": {}},
        })
        response = server._handle_raw(request)
        # Should return method not found
        assert "error" in response

    def test_handle_ping(self, server):
        request = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"})
        response = server._handle_raw(request)
        assert response["result"]["status"] == "ok"

    def test_empty_line_returns_none(self, server):
        result = server._handle_raw("")
        assert result is None

    def test_response_id_matches_request(self, server):
        for req_id in [1, 42, "abc", None]:
            request = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "unknown"})
            response = server._handle_raw(request)
            assert response["id"] == req_id


# ---------------------------------------------------------------------------
# OntologyBuilder unit tests (with SQLite)
# ---------------------------------------------------------------------------


class TestOntologyBuilder:
    @pytest.fixture
    def builder(self):
        from opencrab.ontology.builder import OntologyBuilder
        from opencrab.stores.mongo_store import MongoStore
        from opencrab.stores.neo4j_store import Neo4jStore
        from opencrab.stores.sql_store import SQLStore

        neo4j = Neo4jStore("bolt://invalid:7687", "neo4j", "pw")
        mongo = MongoStore("mongodb://invalid:27017", "db")
        sql = SQLStore("sqlite:///:memory:")
        return OntologyBuilder(neo4j, mongo, sql)

    def test_add_node_valid(self, builder):
        result = builder.add_node("subject", "User", "u1", {
            "name": "Alice", "email": "alice@example.com", "role": "admin"
        })
        assert result["node_id"] == "u1"
        assert result["space"] == "subject"
        assert result["node_type"] == "User"
        assert "stores" in result
        # neo4j and mongo are unavailable, but the SQL registry should be ok.
        # stores keys are role-based (graph/docs/sql/vector) since §1.3.
        assert result["stores"]["sql"] == "ok"

    def test_add_node_invalid_space(self, builder):
        with pytest.raises(ValueError, match="badspace"):
            builder.add_node("badspace", "User", "u1")

    def test_add_node_invalid_type(self, builder):
        with pytest.raises(ValueError, match="Document"):
            builder.add_node("subject", "Document", "u1")

    def test_add_edge_valid(self, builder):
        builder.add_node("subject", "User", "u1", {"name": "Alice", "email": "a@ex.com", "role": "admin"})
        builder.add_node("resource", "Project", "p1", {"name": "Project X"})
        result = builder.add_edge("subject", "u1", "owns", "resource", "p1")
        assert result["relation"] == "owns"
        assert result["stores"]["sql"] == "ok"

    def test_add_edge_invalid_relation(self, builder):
        with pytest.raises(ValueError):
            builder.add_edge("subject", "u1", "mentions", "resource", "p1")

    def test_add_edge_invalid_space_pair(self, builder):
        with pytest.raises(ValueError):
            builder.add_edge("outcome", "o1", "owns", "subject", "u1")


# ---------------------------------------------------------------------------
# ReBACEngine unit tests (with SQLite)
# ---------------------------------------------------------------------------


class TestReBACEngine:
    @pytest.fixture
    def engine(self):
        from opencrab.ontology.rebac import ReBACEngine
        from opencrab.stores.neo4j_store import Neo4jStore
        from opencrab.stores.sql_store import SQLStore

        neo4j = Neo4jStore("bolt://invalid:7687", "neo4j", "pw")
        sql = SQLStore("sqlite:///:memory:")
        return ReBACEngine(neo4j, sql)

    def test_check_denied_when_no_policy_no_graph(self, engine):
        decision = engine.check("u1", "view", "doc1")
        assert decision.granted is False
        assert "Default deny" in decision.reason

    def test_explicit_grant(self, engine):
        engine.grant("u1", "view", "doc1")
        decision = engine.check("u1", "view", "doc1")
        assert decision.granted is True

    def test_explicit_deny(self, engine):
        engine.grant("u1", "edit", "doc2")
        engine.deny("u1", "edit", "doc2")
        decision = engine.check("u1", "edit", "doc2")
        assert decision.granted is False
        assert "DENY" in decision.reason

    def test_invalid_permission_returns_deny(self, engine):
        decision = engine.check("u1", "delete", "doc1")
        assert decision.granted is False
        # The reason should contain either "Invalid permission" or "Unknown permission"
        assert "permission" in decision.reason.lower()

    def test_list_policies(self, engine):
        engine.grant("u2", "view", "r1")
        engine.grant("u2", "edit", "r2")
        policies = engine.list_subject_policies("u2")
        assert len(policies) == 2


# ---------------------------------------------------------------------------
# ImpactEngine unit tests (with SQLite, no Neo4j)
# ---------------------------------------------------------------------------


class TestImpactEngine:
    @pytest.fixture
    def engine(self):
        from opencrab.ontology.impact import ImpactEngine
        from opencrab.stores.neo4j_store import Neo4jStore
        from opencrab.stores.sql_store import SQLStore

        neo4j = Neo4jStore("bolt://invalid:7687", "neo4j", "pw")
        sql = SQLStore("sqlite:///:memory:")
        return ImpactEngine(neo4j, sql)

    def test_analyse_returns_impact_result(self, engine):
        from opencrab.ontology.impact import ImpactResult

        result = engine.analyse("n1", "update")
        assert isinstance(result, ImpactResult)
        assert result.node_id == "n1"
        assert result.change_type == "update"
        assert len(result.triggered) > 0

    def test_analyse_always_triggers_i1(self, engine):
        result = engine.analyse("n2", "create")
        triggered_ids = {t["id"] for t in result.triggered}
        assert "I1" in triggered_ids

    def test_analyse_delete_triggers_multiple(self, engine):
        result = engine.analyse("n3", "delete")
        triggered_ids = {t["id"] for t in result.triggered}
        # Delete should trigger data, relation, and logic impacts
        assert len(triggered_ids) >= 3

    def test_analyse_persists_to_sql(self, engine):
        from opencrab.stores.sql_store import SQLStore

        engine.analyse("n4", "update")
        records = engine._sql.get_impacts("n4")
        assert len(records) >= 1

    def test_lever_simulate_invalid_direction(self, engine):
        with pytest.raises(ValueError, match="invalid_dir"):
            engine.lever_simulate("lev1", "invalid_dir", 0.5)

    def test_lever_simulate_returns_dict(self, engine):
        result = engine.lever_simulate("lev1", "raises", 0.7)
        assert result["lever_id"] == "lev1"
        assert result["direction"] == "raises"
        assert result["magnitude"] == 0.7
        assert "confidence" in result
        assert result["confidence"] > 0
