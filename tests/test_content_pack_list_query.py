"""content_pack_list(query=...) — 관련도 필터/정렬 계약.

query가 없으면 기존 전체 목록 응답과 바이트 동일해야 하고, query가 있으면
graph에 실제 적재된 팩만 후보로 삼아 결정론적 순서로 정렬해야 한다.
manifest에만 있고 graph에 없는 팩은 절대 노출되지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.mcp.tools import content_pack_list
from opencrab.ontology.pack_registry import PackInfo

# Fixed test principal for every call through _call() below. #146:
# content_pack_list() now scopes candidates to readable_pack_ids(sql,
# current_principal()) -- _call()'s default registry (every row's pack_id,
# see below) makes this principal own everything, reproducing the pre-#146
# "no scoping" behaviour for every test that doesn't care about ownership.
# Tests that DO care about scoping (bottom of file) bind other principals
# and/or pass an explicit `readable` set.
_PRINCIPAL = Principal(user_id="u1", is_local=True, disabled=False)


def _graph(rows):
    g = MagicMock()
    g.available = True
    g.list_packs.return_value = rows
    return g


def _row(pack_id, node_count=1, title="", description=""):
    return {
        "pack_id": pack_id,
        "node_count": node_count,
        "sample_title": title,
        "sample_description": description,
    }


def _ctx(graph):
    return {"neo4j": graph, "sql": MagicMock()}


def _call(rows, registry=(), readable=None, **kwargs):
    """``readable`` defaults to every pack_id in ``rows`` (i.e. no scoping —
    the legacy contract every pre-#146 test in this file exercises)."""
    if readable is None:
        readable = {r["pack_id"] for r in rows}
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.packs.registry.readable_pack_ids") as mock_readable,
    ):
        mock_ctx.return_value = _ctx(_graph(rows))
        mock_reg.return_value = list(registry)
        mock_readable.return_value = set(readable)
        with principal_scope(_PRINCIPAL):
            return content_pack_list(**kwargs)


# ---------------------------------------------------------------------------
# query 없음 = 기존 계약
# ---------------------------------------------------------------------------


def test_no_query_keeps_legacy_shape():
    result = _call([_row("biomed", 5, "Biomed ontology pack", "생명의학")])
    assert result == {
        "total": 1,
        "packs": [{"pack_id": "biomed", "node_count": 5, "title": "Biomed"}],
    }
    # 관련도 전용 키가 새어나오면 안 된다.
    assert "score" not in result["packs"][0]
    assert "query" not in result and "scanned" not in result


def test_no_query_with_limit_truncates():
    rows = [_row(f"p{i}", 10 - i) for i in range(5)]
    result = _call(rows, limit=2)
    assert [p["pack_id"] for p in result["packs"]] == ["p0", "p1"]
    assert result["total"] == 2
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# query 있음
# ---------------------------------------------------------------------------


def test_query_keeps_only_matching_packs():
    rows = [
        _row("coffee-roastery", 30, "커피 로스터리", "로스팅 원두 카페"),
        _row("astronomy", 900, "천문학", "별자리와 우주론"),
    ]
    result = _call(rows, query="커피 로스터리")
    assert [p["pack_id"] for p in result["packs"]] == ["coffee-roastery"]
    assert result["scanned"] == 2
    assert result["total"] == 1
    assert result["query"] == "커피 로스터리"
    assert result["packs"][0]["score"] > 0
    assert result["packs"][0]["matched"]


def test_query_scores_description_from_graph_anchor():
    """description은 manifest가 아니라 graph anchor에서 온다."""
    rows = [_row("claude", 9709, "Claude 세션 기록", "Claude Code 작업 세션 히스토리")]
    result = _call(rows, query="세션 히스토리")
    assert [p["pack_id"] for p in result["packs"]] == ["claude"]


def test_query_drops_zero_score_packs_without_falling_back():
    rows = [_row("astronomy", 900, "천문학", "별자리")]
    result = _call(rows, query="오토바이 정비")
    assert result["packs"] == []
    assert result["total"] == 0
    assert result["scanned"] == 1


def test_query_ordering_is_deterministic_and_total():
    """동점이면 node_count desc, 그다음 pack_id asc. 입력 순서와 무관."""
    rows = [
        _row("bravo", 10, "커피", ""),
        _row("alpha", 10, "커피", ""),
        _row("charlie", 50, "커피", ""),
    ]
    forward = _call(rows, query="커피")
    reverse = _call(list(reversed(rows)), query="커피")
    assert [p["pack_id"] for p in forward["packs"]] == ["charlie", "alpha", "bravo"]
    assert forward["packs"] == reverse["packs"]


def test_query_limit_defaults_to_ten_and_reports_truncation():
    rows = [_row(f"coffee-{i:02d}", 100 - i, "커피", "") for i in range(15)]
    result = _call(rows, query="커피")
    assert len(result["packs"]) == 10
    assert result["truncated"] is True
    assert result["total"] == 10

    exact = _call(rows, query="커피", limit=15)
    assert len(exact["packs"]) == 15
    assert "truncated" not in exact


# ---------------------------------------------------------------------------
# manifest join
# ---------------------------------------------------------------------------


def test_manifest_only_pack_is_never_surfaced():
    rows = [_row("in-graph", 3, "그래프 적재 팩", "커피")]
    registry = [
        PackInfo(pack_id="manifest-only", title="커피 매뉴얼", description="커피 로스팅"),
    ]
    result = _call(rows, registry=registry, query="커피")
    assert [p["pack_id"] for p in result["packs"]] == ["in-graph"]


def test_manifest_keywords_and_category_boost_existing_pack():
    rows = [_row("roastery", 3, "로스터리", "")]
    plain = _call(rows, query="에스프레소")
    assert plain["packs"] == []

    registry = [
        PackInfo(
            pack_id="roastery",
            keywords=["에스프레소"],
            tags=["원두"],
            raw={"category": "카페"},
        )
    ]
    boosted = _call(rows, registry=registry, query="에스프레소")
    assert [p["pack_id"] for p in boosted["packs"]] == ["roastery"]

    by_category = _call(rows, registry=registry, query="카페")
    assert [p["pack_id"] for p in by_category["packs"]] == ["roastery"]


def test_manifest_registry_is_loaded_once_per_call():
    rows = [_row(f"p{i}", 5, "커피", "") for i in range(20)]
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.packs.registry.readable_pack_ids") as mock_readable,
    ):
        mock_ctx.return_value = _ctx(_graph(rows))
        mock_reg.return_value = []
        mock_readable.return_value = {r["pack_id"] for r in rows}
        with principal_scope(_PRINCIPAL):
            content_pack_list(query="커피")
    assert mock_reg.call_count == 1


def test_manifest_failure_degrades_to_graph_only():
    rows = [_row("coffee", 3, "커피", "")]
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.packs.registry.readable_pack_ids") as mock_readable,
    ):
        mock_ctx.return_value = _ctx(_graph(rows))
        mock_reg.side_effect = OSError("boom")
        mock_readable.return_value = {r["pack_id"] for r in rows}
        with principal_scope(_PRINCIPAL):
            result = content_pack_list(query="커피")
    assert [p["pack_id"] for p in result["packs"]] == ["coffee"]


# ---------------------------------------------------------------------------
# 경계
# ---------------------------------------------------------------------------


def test_graph_unavailable_still_errors():
    graph = MagicMock()
    graph.available = False
    with patch("opencrab.mcp.tools._get_context") as mock_ctx:
        mock_ctx.return_value = _ctx(graph)
        assert content_pack_list(query="커피") == {"error": "graph store unavailable"}


def test_min_nodes_is_forwarded_to_store():
    rows = [_row("coffee", 3, "커피", "")]
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.packs.registry.readable_pack_ids") as mock_readable,
    ):
        graph = _graph(rows)
        mock_ctx.return_value = _ctx(graph)
        mock_reg.return_value = []
        mock_readable.return_value = {r["pack_id"] for r in rows}
        with principal_scope(_PRINCIPAL):
            content_pack_list(min_nodes=7, query="커피")
    graph.list_packs.assert_called_once_with(7)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_query_is_treated_as_no_query(blank):
    result = _call([_row("coffee", 3, "커피", "")], query=blank)
    assert "query" not in result
    assert result["packs"][0]["pack_id"] == "coffee"


# ---------------------------------------------------------------------------
# 실 store 연동 (list_packs가 anchor description을 실제로 투영하는지)
# ---------------------------------------------------------------------------


def test_local_store_projects_anchor_description(tmp_path: Path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    store = LocalGraphStore(str(tmp_path / "graph.db"))
    try:
        store.upsert_node(
            "Dataset",
            "dataset:roastery",
            {"pack_id": "roastery", "title": "로스터리", "description": "커피 원두 로스팅"},
            space_id="resource",
        )
        store.upsert_node(
            "TextUnit", "roastery/1", {"pack_id": "roastery"}, space_id="evidence"
        )
        rows = store.list_packs(1)
    finally:
        store.close()

    assert rows == [
        {
            "pack_id": "roastery",
            "node_count": 2,
            "sample_title": "로스터리",
            "sample_description": "커피 원두 로스팅",
        }
    ]


# ---------------------------------------------------------------------------
# #146: 등록부 스코핑 — 자기 팩 + 공개 팩만
# ---------------------------------------------------------------------------


def test_scopes_to_owned_and_public_packs_only():
    """graph에는 세 팩이 모두 적재돼 있어도, readable_pack_ids가 돌려주는
    (owner=principal ∪ visibility!=private) 집합 밖의 팩은 응답에서 빠진다."""
    rows = [
        _row("mine", 3, "내 팩", ""),
        _row("someone-elses-public", 5, "남의 공개 팩", ""),
        _row("someone-elses-private", 7, "남의 비공개 팩", ""),
    ]
    result = _call(rows, readable={"mine", "someone-elses-public"})
    assert {p["pack_id"] for p in result["packs"]} == {"mine", "someone-elses-public"}


def test_readable_pack_ids_called_with_real_principal():
    """readable_pack_ids가 current_principal()로 스코프된다 -- ctx["sql"]과
    함께 호출되는지, 그리고 다른 principal이면 다른 결과가 나오는지 확인."""
    rows = [_row("p1", 1, "", "")]
    sql_sentinel = MagicMock()
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.packs.registry.readable_pack_ids") as mock_readable,
    ):
        mock_ctx.return_value = {"neo4j": _graph(rows), "sql": sql_sentinel}
        mock_reg.return_value = []
        mock_readable.return_value = set()
        with principal_scope(_PRINCIPAL):
            result = content_pack_list()
    mock_readable.assert_called_once_with(sql_sentinel, _PRINCIPAL)
    assert result["packs"] == []
