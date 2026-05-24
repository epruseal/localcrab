from __future__ import annotations

from unittest.mock import MagicMock, patch

from opencrab.ontology.query import QueryResult


def _stub_context(hybrid_mock: MagicMock) -> dict:
    billing = MagicMock()
    billing.on_query = MagicMock()
    return {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": MagicMock(),
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": hybrid_mock,
        "billing": billing,
    }


def _make_result(pack_id: str | None = "pack-a") -> QueryResult:
    meta = {"node_id": "n1"}
    if pack_id:
        meta["pack_id"] = pack_id
    return QueryResult(
        source="vector",
        node_id="n1",
        score=0.9,
        text="alpha",
        metadata=meta,
    )


def test_t10_ontology_query_includes_envelope_fields():
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=[_make_result("pack-a")])

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid)):
        response = tools.ontology_query(
            question="alpha",
            pack_ids=["pack-a"],
        )

    assert response["question"] == "alpha"
    assert response["total"] == 1
    assert response["results"][0]["metadata"]["pack_id"] == "pack-a"
    assert response["pack_filter"]["pack_ids"] == ["pack-a"]
    assert "selected_packs" in response
    # spaces_filter remains untouched
    assert response["spaces_filter"] is None


def test_t10_legacy_callers_can_ignore_new_fields():
    """All original fields must remain present for backward compatibility."""
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=[_make_result(None)])

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid)):
        response = tools.ontology_query(question="alpha")

    for key in ("question", "spaces_filter", "subject_id", "tenant_id", "pipeline", "total", "results"):
        assert key in response


def test_t10_pack_ids_take_priority_over_auto_pack():
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=[_make_result("pack-a")])

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid)):
        response = tools.ontology_query(
            question="alpha",
            pack_ids=["pack-a"],
            auto_pack=True,
        )

    assert response["pack_filter"]["pack_ids"] == ["pack-a"]
    # auto_pack should be flipped off / unused
    assert response["pack_filter"]["auto_pack"] is False
    assert any("ignoring auto_pack" in w for w in response["pack_filter"].get("warnings", []))


def test_t10_include_pack_provenance_false_drops_envelope_additions():
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=[_make_result("pack-a")])

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid)):
        response = tools.ontology_query(
            question="alpha",
            include_pack_provenance=False,
        )

    assert "selected_packs" not in response
    assert "pack_filter" not in response


def test_t10_schema_advertises_new_parameters():
    from opencrab.mcp.tools import TOOLS

    schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == "ontology_query")
    props = schema["properties"]
    assert "pack_ids" in props
    assert "auto_pack" in props
    assert "include_unpackaged" in props
    assert "include_pack_provenance" in props
