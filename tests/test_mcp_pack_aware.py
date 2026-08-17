from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from opencrab.ontology.query import QueryOutcome, QueryResult

# #145: ontology_query() now calls current_principal() internally; bind a
# fixed test principal for every test in this module (see conftest.py's
# bind_test_principal for why this is opt-in per module, not autouse).
pytestmark = pytest.mark.usefixtures("bind_test_principal")

# The fixed user_id conftest.py's bind_test_principal binds for every test
# in this module.
_TEST_USER_ID = "test-user"


def _real_sql_with_owned_pack(*pack_ids: str) -> Any:
    """A real in-memory SQLStore with each of ``pack_ids`` registry-owned by
    ``_TEST_USER_ID``.

    Issue #147 §3.7: ontology_query now derives its read scope from
    ``ctx["sql"]`` + ``current_principal()`` (``opencrab.pack.read_scope
    .read_scope`` -> ``ownership.readable_pack_ids``), which runs a real SQL
    query against the ``packs``/``users`` tables -- a bare ``MagicMock`` sql
    double can't satisfy that, so the derived scope comes back empty and
    every requested pack_id is reported out-of-scope regardless of what the
    test intends to exercise. Mirrors tests/test_packs_registry.py's
    real-SQLStore + create_pack pattern; the owning user row is inserted
    directly (rather than via ``create_user``, which mints a random id)
    because the FK target must match bind_test_principal's fixed user_id
    for ``readable_pack_ids``'s ``owner_id = :uid`` predicate to find it.
    """
    from sqlalchemy import text

    from opencrab.pack.ownership import create_pack
    from opencrab.stores.sql_store import SQLStore

    sql = SQLStore("sqlite:///:memory:")
    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local) "
                "VALUES (:uid, :dn, :loc)"
            ),
            {"uid": _TEST_USER_ID, "dn": "Test User", "loc": True},
        )
    for pack_id in pack_ids:
        create_pack(sql, _TEST_USER_ID, pack_id, title=pack_id)
    return sql


def _stub_context(hybrid_mock: MagicMock, sql: Any = None) -> dict:
    billing = MagicMock()
    billing.on_query = MagicMock()
    return {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": sql if sql is not None else MagicMock(),
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


def _outcome(pack_id: str | None = "pack-a") -> QueryOutcome:
    """#51: HybridQuery.query() returns QueryOutcome(results, warnings), not a
    bare list — mock the actual contract."""
    return QueryOutcome(results=[_make_result(pack_id)], warnings=[])


def test_t10_ontology_query_includes_envelope_fields():
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=_outcome("pack-a"))
    sql = _real_sql_with_owned_pack("pack-a")

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid, sql=sql)):
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
    hybrid.query = MagicMock(return_value=_outcome(None))

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid)):
        response = tools.ontology_query(question="alpha")

    for key in ("question", "spaces_filter", "subject_id", "tenant_id", "pipeline", "total", "results"):
        assert key in response


def test_t10_pack_ids_take_priority_over_auto_pack():
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=_outcome("pack-a"))
    sql = _real_sql_with_owned_pack("pack-a")

    with patch.object(tools, "_get_context", return_value=_stub_context(hybrid, sql=sql)):
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
    hybrid.query = MagicMock(return_value=_outcome("pack-a"))

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
