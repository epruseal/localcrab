"""
Tests for the MCP server and tool dispatcher.

All tests mock the underlying stores so no live services are required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# #145: dispatch_tool()/the tool handlers now call current_principal()
# internally; bind a fixed test principal for every test in this module (see
# conftest.py's bind_test_principal for why this is opt-in per module, not
# autouse -- tests/test_auth.py needs a genuinely empty scope for its own
# "no principal bound" tests).
pytestmark = pytest.mark.usefixtures("bind_test_principal")

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

        # 17 exposed tools after reorder + dedup + 3 new READ tools + #146's pack_publish.
        # 비노출(주석처리): query_bm25, rebac, workflow×2, approval, billing×2,
        #   identity×5, canonicalize×2, promotion×4, ontology_extract, ontology_ingest
        assert len(TOOLS) == 17
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
        assert "pack_publish" in names
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
            billing = MagicMock()
            builder.add_node.return_value = {
                "node_id": "u1", "space": "subject", "node_type": "User",
                "properties": {}, "stores": {"graph": "ok"}
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            result = dispatch_tool("ontology_add_node", {
                "space": "subject", "node_type": "User", "node_id": "u1",
                "properties": {"name": "Alice", "email": "alice@example.com", "role": "admin"}
            })
            assert result["node_id"] == "u1"
            assert "stores" in result
            # #66 codex re-review [8]: on_node_write is the sibling of
            # on_edge_write and had the same fail-open gap. Pin that a real
            # graph success ("graph": "ok") does bill. subject_id is the
            # bind_test_principal fixture's principal (#145: it can no
            # longer be a client argument -- see the rejection test below).
            billing.on_node_write.assert_called_once_with("default", "test-user", "subject", "User")

    def test_ontology_add_node_forwards_principal_to_builder_audit(self):
        """Issue #119 sibling check, inverted for #145: ontology_add_node
        already forwarded subject_id to builder.add_node (unlike
        ontology_add_edge, which did not). Pin that the SAME subject_id
        reaches both builder.add_node (audit) and on_node_write (billing) --
        now sourced from the caller's server-derived principal instead of a
        client-supplied argument."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_node.return_value = {"node_id": "u1", "stores": {"graph": "ok"}}
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            with principal_scope(Principal(user_id="actor-1", is_local=True, disabled=False)):
                dispatch_tool("ontology_add_node", {
                    "space": "subject", "node_type": "User", "node_id": "u1",
                })
            assert builder.add_node.call_args.kwargs["subject_id"] == "actor-1"
            billing.on_node_write.assert_called_once_with("default", "actor-1", "subject", "User")

    def test_ontology_add_node_rejects_client_subject_id(self):
        """#145, #143 invariant 2: a client-supplied subject_id in
        `arguments` is rejected outright, not silently ignored -- a
        silently-dropped value would make the caller believe it took
        effect. Was TestToolDispatch::
        test_ontology_add_node_forwards_subject_id_to_builder_audit before
        #145, which pinned the opposite (now-insecure) behaviour."""
        from opencrab.mcp.tools import ForbiddenArgumentError, dispatch_tool

        with pytest.raises(ForbiddenArgumentError):
            dispatch_tool("ontology_add_node", {
                "space": "subject", "node_type": "User", "node_id": "u1",
                "subject_id": "actor-1",
            })

    def test_ontology_add_node_graph_store_failure_does_not_bill(self):
        """#66 codex re-review, finding [8]: add_node() doesn't raise for a
        per-store failure — the exact same silent-failure shape already
        fixed on ontology_add_edge, just never applied to this sibling."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_node.return_value = {
                "node_id": "u1", "stores": {"graph": "error: disk down"}
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            result = dispatch_tool("ontology_add_node", {
                "space": "subject", "node_type": "User", "node_id": "u1",
            })
            assert result["node_id"] == "u1"  # handler returns the result unchanged
            billing.on_node_write.assert_not_called()

    def test_ontology_add_node_malformed_receipt_does_not_bill(self):
        """#66 codex re-review, finding [3] (applied here too since the same
        gate — graph_write_failed — is shared by add_node and add_edge):
        a "stores" map with no "graph" key at all is a receipt shape this
        code doesn't recognize, not a positive success signal. Fail-closed:
        unrecognized must not bill, only a literal "graph": "ok" does."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_node.return_value = {"node_id": "u1", "stores": {"sql": "ok"}}
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            dispatch_tool("ontology_add_node", {
                "space": "subject", "node_type": "User", "node_id": "u1",
            })
            billing.on_node_write.assert_not_called()

    def test_ontology_add_edge_success(self):
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.return_value = {
                "from": {"space": "subject", "id": "u1"},
                "relation": "owns",
                "to": {"space": "resource", "id": "doc1"},
                "stores": {"graph": "ok"},
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            result = dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "owns",
                "to_space": "resource", "to_id": "doc1",
            })
            assert result["relation"] == "owns"
            # subject_id is the bind_test_principal fixture's principal
            # (#145: no longer a client argument).
            builder.add_edge.assert_called_once_with(
                from_space="subject",
                from_id="u1",
                relation="owns",
                to_space="resource",
                to_id="doc1",
                properties={},
                subject_id="test-user",
            )
            # #66: on_edge_write had zero callers repo-wide before this fix —
            # every edge write went unbilled. Pin the call with the caller's
            # principal (tenant_id fixed at 'default') matching
            # ontology_add_node's on_node_write wiring pattern.
            billing.on_edge_write.assert_called_once_with("default", "test-user", "owns")

    def test_ontology_add_edge_forwards_principal_to_builder_audit(self):
        """Issue #119, inverted for #145: subject_id was billed via
        on_edge_write but never forwarded to builder.add_edge, so the edge's
        audit event (builder.py's mongo.log_event("edge_upsert",
        subject_id=...)) recorded a null actor even though the billing row
        named one. Pin that the SAME subject_id -- now the caller's
        server-derived principal, not a client-supplied argument -- reaches
        both builder.add_edge (audit) and on_edge_write (billing)."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.return_value = {"relation": "owns", "stores": {"graph": "ok"}}
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            with principal_scope(Principal(user_id="actor-1", is_local=True, disabled=False)):
                dispatch_tool("ontology_add_edge", {
                    "from_space": "subject", "from_id": "u1",
                    "relation": "owns",
                    "to_space": "resource", "to_id": "doc1",
                })
            assert builder.add_edge.call_args.kwargs["subject_id"] == "actor-1"
            billing.on_edge_write.assert_called_once_with("default", "actor-1", "owns")

    def test_ontology_add_edge_rejects_client_tenant_and_subject_id(self):
        """#145, #143 invariant 2: client-supplied tenant_id and/or
        subject_id in `arguments` are rejected outright, never silently
        accepted -- there is no more "explicit tenant" client capability at
        all (tenant_id is fixed server-side at 'default'). Was
        TestToolDispatch::test_ontology_add_edge_billing_uses_explicit_tenant_and_subject
        before #145, which pinned the opposite (now-insecure) behaviour."""
        from opencrab.mcp.tools import ForbiddenArgumentError, dispatch_tool

        with pytest.raises(ForbiddenArgumentError):
            dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "owns",
                "to_space": "resource", "to_id": "doc1",
                "tenant_id": "acme", "subject_id": "u1",
            })

    def test_ontology_add_edge_malformed_receipt_does_not_bill(self):
        """Fail-closed pin (finding [3], applies equally to add_edge): a
        "stores" map missing the "graph" key entirely must not bill — an
        unrecognized receipt shape is not a positive success signal."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.return_value = {"stores": {"docs": "ok"}}
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "owns",
                "to_space": "resource", "to_id": "doc1",
            })
            billing.on_edge_write.assert_not_called()

    def test_ontology_add_edge_billing_persist_failure_is_logged_but_write_still_succeeds(self, caplog):
        """#105: a failed billing emit() must not vanish silently and must not
        fail the (already-succeeded) edge write — this pins the observability
        fix made alongside #66's wiring: the handler now inspects
        on_edge_write's returned {"ok": ...} dict instead of discarding it."""
        import logging

        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.return_value = {"relation": "owns", "stores": {"graph": "ok"}}
            billing.on_edge_write.return_value = {"ok": False, "error": "database is locked"}
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            with caplog.at_level(logging.WARNING):
                result = dispatch_tool("ontology_add_edge", {
                    "from_space": "subject", "from_id": "u1",
                    "relation": "owns",
                    "to_space": "resource", "to_id": "doc1",
                })
        assert result["relation"] == "owns"  # write itself still succeeded
        assert any("on_edge_write" in rec.message and "database is locked" in rec.message
                    for rec in caplog.records)

    def test_ontology_add_edge_failure_does_not_bill(self):
        """A grammar-invalid edge must not emit a billing event — mirrors
        ontology_add_node's on_node_write, which only fires after a
        successful builder.add_edge call."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.side_effect = ValueError("Relation 'mentions' is not valid")
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "mentions",
                "to_space": "resource", "to_id": "doc1",
            })
            billing.on_edge_write.assert_not_called()

    def test_ontology_add_edge_graph_store_failure_does_not_bill(self):
        """#66/#105 review: builder.add_edge() doesn't raise for a per-store
        failure — a missing endpoint or a down graph store comes back as a
        status STRING inside result["stores"]["graph"] (builder.py's module
        docstring), not an exception. The earlier failure-does-not-bill test
        only covered the raised-exception branch; this pins the (more
        common) silent-failure branch that codex's adversarial review
        flagged as still billing a write that never landed."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.return_value = {
                "relation": "owns",
                "stores": {"graph": "no match (missing node: resource/doc1)"},
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            result = dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "owns",
                "to_space": "resource", "to_id": "doc1",
            })
            assert result["relation"] == "owns"  # handler returns the result unchanged
            billing.on_edge_write.assert_not_called()

    def test_ontology_add_edge_optional_store_failure_still_bills(self):
        """Graph write succeeded; only an optional store (docs) failed. The
        edge exists and is queryable, so this must still bill — matching
        pack_create's own graph-is-system-of-record split."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            billing = MagicMock()
            builder.add_edge.return_value = {
                "relation": "owns",
                "stores": {"graph": "ok", "docs": "error: mongo down"},
            }
            mock_ctx.return_value = {
                "builder": builder, "rebac": MagicMock(),
                "impact": MagicMock(), "hybrid": MagicMock(), "mongo": MagicMock(),
                "billing": billing,
            }
            dispatch_tool("ontology_add_edge", {
                "from_space": "subject", "from_id": "u1",
                "relation": "owns",
                "to_space": "resource", "to_id": "doc1",
            })
            billing.on_edge_write.assert_called_once_with("default", "test-user", "owns")

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
            from opencrab.ontology.query import QueryOutcome

            mock_result = QueryResult(
                source="vector", node_id="n1", score=0.9, text="Test text", metadata={}
            )
            hybrid = MagicMock()
            # #51: query() returns QueryOutcome(results, warnings), not a bare list.
            hybrid.query.return_value = QueryOutcome(results=[mock_result], warnings=[])
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
        """ontology_rebac_check는 MCP 비노출 — 휴면 코드로 삭제됨(git history 보존)."""
        from opencrab.mcp.tools import dispatch_tool

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
        Dormant code was deleted outright (git history preserves it)."""
        from opencrab.mcp.tools import dispatch_tool

        with pytest.raises(KeyError, match="Unknown tool"):
            dispatch_tool("ontology_ingest", {"text": "t", "source_id": "s"})

    def test_ontology_extract_not_exposed_via_mcp(self):
        """ontology_extract is no longer dispatched via MCP.
        Dormant code was deleted outright (git history preserves it)."""
        from opencrab.mcp.tools import dispatch_tool

        with pytest.raises(KeyError, match="Unknown tool"):
            dispatch_tool("ontology_extract", {"text": "t", "source_id": "s"})

    def test_pack_ingest_text_creates_evidence_node(self):
        """pack_ingest with text materialises an evidence/TextUnit node via builder.add_node."""
        from opencrab.mcp.tools import dispatch_tool
        from opencrab.packs.registry import create_pack as _register_pack
        from opencrab.stores.sql_store import SQLStore

        sql = SQLStore("sqlite:///:memory:")
        _register_pack(sql, "test-user", "test-pack")

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            hybrid = MagicMock()
            hybrid.invalidate_bm25_cache = MagicMock()
            mongo = MagicMock()
            mongo.available = False
            graph = MagicMock()
            graph.available = True
            mock_ctx.return_value = {
                "neo4j": graph,
                "sql": sql,
                "builder": builder,
                "hybrid": hybrid,
                "mongo": mongo,
                "rebac": MagicMock(),
                "impact": MagicMock(),
                "billing": MagicMock(),
            }

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
        from opencrab.packs.registry import create_pack as _register_pack
        from opencrab.stores.sql_store import SQLStore

        sql = SQLStore("sqlite:///:memory:")
        _register_pack(sql, "test-user", "test-pack")

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            builder = MagicMock()
            hybrid = MagicMock()
            hybrid.ingest.return_value = {"stores": {"chromadb": "ok"}}
            hybrid.invalidate_bm25_cache = MagicMock()
            mongo = MagicMock()
            mongo.available = False
            graph = MagicMock()
            graph.available = True
            mock_ctx.return_value = {
                "neo4j": graph,
                "sql": sql,
                "builder": builder,
                "hybrid": hybrid,
                "mongo": mongo,
                "rebac": MagicMock(),
                "impact": MagicMock(),
                "billing": MagicMock(),
            }

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
        """ontology_list_nodes filters by pack_id via the graph store."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            # pack_id 있을 때는 graph.export_nodes(pack_id=..., space=...) 경로 사용
            # (limit-before-filter 버그 회피용 인덱스 쿼리, issue #54: space 도
            # 서버 쪽에서 함께 걸린다 -- 아래 assert_called_once_with 의 space=None
            # 이 그 계약을 검증한다). total 은 별도로 count_exported_nodes 에서 온다.
            graph = MagicMock()
            graph.count_exported_nodes.return_value = 2
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
            graph.export_nodes.assert_called_once_with(pack_id="pack-a", limit=100, space=None)
            graph.count_exported_nodes.assert_called_once_with(pack_id="pack-a", space=None)
            mongo.list_nodes.assert_not_called()  # doc store는 pack_id 있을 때 사용 안 함

    def test_ontology_list_nodes_pack_and_space_filter_pushes_space_kwarg(self):
        """issue #54 contract: when both pack_id and space are given,
        ontology_list_nodes MUST forward space= to export_nodes (and to
        count_exported_nodes) so the backend can push it into its native
        query ahead of limit — a regression that silently drops the kwarg
        (e.g. reverting to a Python-only post-filter) is caught here via
        assert_called_once_with, not just by the return value."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            graph = MagicMock()
            graph.count_exported_nodes.return_value = 1
            # Mock stands in for a backend that already pushed the space
            # filter server-side -- only matching rows come back.
            graph.export_nodes.return_value = [
                {"props": {"node_id": "n3", "pack_id": "pack-a", "space": "concept"}, "labels": ["Entity"]},
            ]
            mongo = MagicMock()
            mock_ctx.return_value = {
                "neo4j": graph, "mongo": mongo, "builder": MagicMock(),
                "hybrid": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "billing": MagicMock(),
            }
            result = dispatch_tool(
                "ontology_list_nodes", {"pack_id": "pack-a", "space": "concept", "limit": 10}
            )
            assert result["total"] == 1
            assert result["nodes"][0]["space"] == "concept"
            graph.export_nodes.assert_called_once_with(pack_id="pack-a", limit=10, space="concept")
            graph.count_exported_nodes.assert_called_once_with(pack_id="pack-a", space="concept")

    def test_ontology_list_nodes_total_not_capped_by_limit(self):
        """issue #54's actual complaint: `total` must report the TRUE match
        count even when it is larger than `limit` (i.e. more rows match than
        the page returned). Before this fix, total was len(nodes) -- capped
        at whatever page export_nodes(limit=...) returned, so this exact
        case (3000 real matches, only 10 returned) silently reported
        total=10 instead of 3000."""
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            graph = MagicMock()
            # count_exported_nodes (no LIMIT) sees the true total; export_nodes
            # (LIMIT 10) returns only a page of it -- the two are intentionally
            # different sizes here to prove `total` is NOT derived from `nodes`.
            graph.count_exported_nodes.return_value = 3000
            graph.export_nodes.return_value = [
                {"props": {"node_id": f"n{i}", "pack_id": "pack-a", "space": "concept"}, "labels": ["Entity"]}
                for i in range(10)
            ]
            mock_ctx.return_value = {
                "neo4j": graph, "mongo": MagicMock(), "builder": MagicMock(),
                "hybrid": MagicMock(), "rebac": MagicMock(),
                "impact": MagicMock(), "billing": MagicMock(),
            }
            result = dispatch_tool(
                "ontology_list_nodes", {"pack_id": "pack-a", "space": "concept", "limit": 10}
            )
            assert result["total"] == 3000
            assert len(result["nodes"]) == 10

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
        assert len(tools) == 17  # 재정렬 후 16개 + #146 pack_publish (비노출 주석처리 + READ 3개 신규)

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
    def builder(self, fast_mongo_timeout):
        # fast_mongo_timeout (tests/conftest.py) 는 이 fixture 재구성 시마다
        # MongoStore가 실제로 5s씩 기다리던 연결 실패를 ~100ms로 단축한다
        # (Neo4jStore는 DNS 실패로 이미 빠름, 병목이 아니었음).
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
