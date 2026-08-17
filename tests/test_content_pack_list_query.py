"""content_pack_list(query=...) — 관련도 필터/정렬 계약 + #146 조인 역전 계약.

#146 C: ``packs`` 등록부(``list_packs_for``)가 후보의 원천이고,
``graph.list_packs(0)``는 node_count/title 보조 집계일 뿐이다. graph에만
있고 등록부 응답(list_packs_for)에 없는 pack_id는 절대 노출되지 않는다.
graph 불가 또는 집계 예외 시 top-level "error" 대신 node_count_known=False로
전부-아니면-전무(all-or-none) 응답한다 -- 부분적으로 아는 팩과 모르는 팩을
섞지 않는다.

query가 없으면 known 상태에서 node_count desc, pack_id asc로, unknown
상태에서 pack_id asc로 정렬한다. query가 있으면 양쪽 다 score desc,
pack_id asc다 (node_count는 더 이상 동점 기준이 아니다 -- null일 수 있어서).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.mcp.tools import content_pack_list
from opencrab.ontology.pack_registry import PackInfo

# Fixed test principal for every call through _call() below.
_PRINCIPAL = Principal(user_id="u1", is_local=True, disabled=False)


def _reg(pack_id, title="", description="", owner_id="u1", visibility="private"):
    """A ``list_packs_for`` row shape (opencrab.pack.ownership._row_to_dict)."""
    return {
        "pack_id": pack_id,
        "owner_id": owner_id,
        "visibility": visibility,
        "title": title,
        "description": description,
        "forked_from": None,
        "created_at": None,
        "updated_at": None,
    }


def _agg(pack_id, node_count=1, title="", description=""):
    """A ``graph.list_packs()`` aggregate row shape."""
    return {
        "pack_id": pack_id,
        "node_count": node_count,
        "sample_title": title,
        "sample_description": description,
    }


def _graph(agg_rows=(), available=True, raises=None):
    g = MagicMock()
    g.available = available
    if raises is not None:
        g.list_packs.side_effect = raises
    else:
        g.list_packs.return_value = list(agg_rows)
    return g


def _ctx(graph, sql=None):
    return {"neo4j": graph, "sql": sql or MagicMock()}


def _call(
    registry_rows,
    agg_rows=(),
    manifest=(),
    graph_available=True,
    graph_raises=None,
    **kwargs,
):
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.pack.ownership.list_packs_for") as mock_list_for,
    ):
        mock_ctx.return_value = _ctx(_graph(agg_rows, available=graph_available, raises=graph_raises))
        mock_reg.return_value = list(manifest)
        mock_list_for.return_value = list(registry_rows)
        with principal_scope(_PRINCIPAL):
            return content_pack_list(**kwargs)


# ---------------------------------------------------------------------------
# query 없음 = known/unknown 응답 모양
# ---------------------------------------------------------------------------


def test_no_query_known_counts_shape():
    result = _call(
        [_reg("biomed", "Biomed ontology pack", "생명의학")],
        [_agg("biomed", 5, "Biomed ontology pack", "생명의학")],
    )
    assert result == {
        "total": 1,
        "node_count_known": True,
        "min_nodes_applied": True,
        "packs": [{"pack_id": "biomed", "node_count": 5, "title": "Biomed"}],
    }
    # 관련도 전용 키가 새어나오면 안 된다.
    assert "score" not in result["packs"][0]
    assert "query" not in result and "scanned" not in result


def test_no_query_with_limit_truncates():
    registry = [_reg(f"p{i}") for i in range(5)]
    agg = [_agg(f"p{i}", 10 - i) for i in range(5)]
    result = _call(registry, agg, limit=2)
    assert [p["pack_id"] for p in result["packs"]] == ["p0", "p1"]
    assert result["total"] == 2
    assert result["truncated"] is True


def test_no_query_known_sorts_by_node_count_desc_then_pack_id_asc():
    registry = [_reg("b"), _reg("a"), _reg("c")]
    agg = [_agg("b", 5), _agg("a", 5), _agg("c", 9)]
    result = _call(registry, agg)
    assert [p["pack_id"] for p in result["packs"]] == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# query 있음
# ---------------------------------------------------------------------------


def test_query_keeps_only_matching_packs():
    registry = [_reg("coffee-roastery"), _reg("astronomy")]
    agg = [
        _agg("coffee-roastery", 30, "커피 로스터리", "로스팅 원두 카페"),
        _agg("astronomy", 900, "천문학", "별자리와 우주론"),
    ]
    result = _call(registry, agg, query="커피 로스터리")
    assert [p["pack_id"] for p in result["packs"]] == ["coffee-roastery"]
    assert result["scanned"] == 2
    assert result["total"] == 1
    assert result["query"] == "커피 로스터리"
    assert result["packs"][0]["score"] > 0
    assert result["packs"][0]["matched"]


def test_query_scores_description_from_graph_anchor():
    """registry 행에 title/description이 비어 있으면 graph anchor로
    fallback한다."""
    registry = [_reg("claude")]
    agg = [_agg("claude", 9709, "Claude 세션 기록", "Claude Code 작업 세션 히스토리")]
    result = _call(registry, agg, query="세션 히스토리")
    assert [p["pack_id"] for p in result["packs"]] == ["claude"]


def test_query_registry_title_wins_over_graph_anchor():
    """등록부에 title/description이 있으면 graph anchor보다 우선한다."""
    registry = [_reg("claude", title="에스프레소 노트", description="원두 로스팅 기록")]
    agg = [_agg("claude", 1, "Claude 세션 기록", "Claude Code 작업 세션 히스토리")]
    result = _call(registry, agg, query="에스프레소")
    assert [p["pack_id"] for p in result["packs"]] == ["claude"]
    miss = _call(registry, agg, query="세션 히스토리")
    assert miss["packs"] == []


def test_query_drops_zero_score_packs_without_falling_back():
    registry = [_reg("astronomy")]
    agg = [_agg("astronomy", 900, "천문학", "별자리")]
    result = _call(registry, agg, query="오토바이 정비")
    assert result["packs"] == []
    assert result["total"] == 0
    assert result["scanned"] == 1


def test_query_ordering_is_deterministic_by_score_then_pack_id():
    """동점이면 pack_id asc (#146 C: node_count는 더 이상 동점 기준이
    아니다 -- node_count가 null일 수 있으므로 순서 안정성을 node_count에
    기대면 안 된다). 입력 순서와 무관."""
    registry = [_reg("bravo"), _reg("alpha"), _reg("charlie")]
    agg = [_agg("bravo", 10, "커피"), _agg("alpha", 10, "커피"), _agg("charlie", 50, "커피")]
    forward = _call(registry, agg, query="커피")
    reverse = _call(list(reversed(registry)), list(reversed(agg)), query="커피")
    assert [p["pack_id"] for p in forward["packs"]] == ["alpha", "bravo", "charlie"]
    assert forward["packs"] == reverse["packs"]


def test_query_limit_defaults_to_ten_and_reports_truncation():
    registry = [_reg(f"coffee-{i:02d}") for i in range(15)]
    agg = [_agg(f"coffee-{i:02d}", 100 - i, "커피") for i in range(15)]
    result = _call(registry, agg, query="커피")
    assert len(result["packs"]) == 10
    assert result["truncated"] is True
    assert result["total"] == 10

    exact = _call(registry, agg, query="커피", limit=15)
    assert len(exact["packs"]) == 15
    assert "truncated" not in exact


# ---------------------------------------------------------------------------
# manifest join
# ---------------------------------------------------------------------------


def test_manifest_only_pack_is_never_surfaced():
    """manifest(온-디스크)에만 있고 ``packs`` 등록부에 없는 pack_id는 절대
    노출되지 않는다 -- #146 이후로는 graph에도 없고 등록부에도 없으면
    이중으로 차단된다."""
    registry = [_reg("in-graph")]
    agg = [_agg("in-graph", 3, "그래프 적재 팩", "커피")]
    manifest = [PackInfo(pack_id="manifest-only", title="커피 매뉴얼", description="커피 로스팅")]
    result = _call(registry, agg, manifest=manifest, query="커피")
    assert [p["pack_id"] for p in result["packs"]] == ["in-graph"]


def test_manifest_keywords_and_category_boost_existing_pack():
    registry = [_reg("roastery")]
    agg = [_agg("roastery", 3, "로스터리", "")]
    plain = _call(registry, agg, query="에스프레소")
    assert plain["packs"] == []

    manifest = [
        PackInfo(
            pack_id="roastery",
            keywords=["에스프레소"],
            tags=["원두"],
            raw={"category": "카페"},
        )
    ]
    boosted = _call(registry, agg, manifest=manifest, query="에스프레소")
    assert [p["pack_id"] for p in boosted["packs"]] == ["roastery"]

    by_category = _call(registry, agg, manifest=manifest, query="카페")
    assert [p["pack_id"] for p in by_category["packs"]] == ["roastery"]


def test_manifest_registry_is_loaded_once_per_call():
    registry = [_reg(f"p{i}") for i in range(20)]
    agg = [_agg(f"p{i}", 5, "커피") for i in range(20)]
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.pack.ownership.list_packs_for") as mock_list_for,
    ):
        mock_ctx.return_value = _ctx(_graph(agg))
        mock_reg.return_value = []
        mock_list_for.return_value = registry
        with principal_scope(_PRINCIPAL):
            content_pack_list(query="커피")
    assert mock_reg.call_count == 1


def test_manifest_failure_degrades_to_graph_only():
    registry = [_reg("coffee")]
    agg = [_agg("coffee", 3, "커피", "")]
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.pack.ownership.list_packs_for") as mock_list_for,
    ):
        mock_ctx.return_value = _ctx(_graph(agg))
        mock_reg.side_effect = OSError("boom")
        mock_list_for.return_value = registry
        with principal_scope(_PRINCIPAL):
            result = content_pack_list(query="커피")
    assert [p["pack_id"] for p in result["packs"]] == ["coffee"]


# ---------------------------------------------------------------------------
# graph 불가 / 집계 예외 -- #146 C + C7: 전부-아니면-전무, top-level error 없음
# ---------------------------------------------------------------------------


def test_graph_unavailable_returns_unknown_counts_not_error():
    """graph unavailable은 더 이상 top-level "error"로 조기 반환하지 않는다
    -- 등록부에서 읽을 수 있는 팩은 여전히 반환되고, node_count_known=False,
    모든 node_count가 null이다."""
    result = _call([_reg("coffee", title="커피")], graph_available=False, query="커피")
    assert "error" not in result
    assert result["node_count_known"] is False
    assert result["min_nodes_applied"] is False
    assert [p["pack_id"] for p in result["packs"]] == ["coffee"]
    assert result["packs"][0]["node_count"] is None


def test_graph_aggregation_exception_also_returns_unknown_counts():
    """#146 C7: 집계 호출이 예외를 던지는 경우도 unavailable과 동일하게
    unknown 전체 응답이다 -- 절대 일부만 아는 상태로 섞이지 않는다."""
    result = _call([_reg("coffee", title="커피")], graph_raises=RuntimeError("boom"), query="커피")
    assert "error" not in result
    assert result["node_count_known"] is False
    assert result["min_nodes_applied"] is False
    assert result["packs"][0]["node_count"] is None


def test_graph_unavailable_no_query_returns_all_readable_packs_unknown():
    """min_nodes는 unknown 상태에서 적용되지 않는다 (모르는 값으로 거를 수
    없다) -- min_nodes=5를 줘도 두 팩 다 반환된다."""
    result = _call([_reg("a"), _reg("b")], graph_available=False, min_nodes=5)
    assert result["node_count_known"] is False
    assert result["min_nodes_applied"] is False
    assert {p["pack_id"] for p in result["packs"]} == {"a", "b"}
    assert all(p["node_count"] is None for p in result["packs"])
    # unknown 정렬은 pack_id asc.
    assert [p["pack_id"] for p in result["packs"]] == ["a", "b"]


def test_min_nodes_filters_against_registry_candidates_not_the_store_call():
    """#146 C: graph.list_packs는 항상 인자 없이(0) 호출된다 -- min_nodes
    필터링은 content_pack_list 자신이 등록부 기반 후보 목록에 대해
    수행한다 (min_nodes=7이 실제로 low를 거르는 양성 케이스)."""
    registry = [_reg("low", title="커피"), _reg("high", title="커피")]
    agg = [_agg("low", 2, "커피"), _agg("high", 9, "커피")]
    graph = _graph(agg)
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.pack.ownership.list_packs_for") as mock_list_for,
    ):
        mock_ctx.return_value = _ctx(graph)
        mock_reg.return_value = []
        mock_list_for.return_value = registry
        with principal_scope(_PRINCIPAL):
            result = content_pack_list(min_nodes=7, query="커피")
    graph.list_packs.assert_called_once_with(0)
    assert [p["pack_id"] for p in result["packs"]] == ["high"]


# ---------------------------------------------------------------------------
# 경계
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_query_is_treated_as_no_query(blank):
    result = _call([_reg("coffee", title="커피")], [_agg("coffee", 3, "커피")], query=blank)
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
# #146: 등록부가 원천 -- graph에만 있고 등록부 응답 밖인 팩은 드롭
# ---------------------------------------------------------------------------


def test_pack_in_graph_but_not_in_registry_scope_is_dropped():
    """#146 C: 후보는 오직 list_packs_for가 돌려주는 행뿐이다. graph에
    적재돼 있어도 (list_packs_for가 스코핑해 돌려주지 않는 이상) 응답에
    새지 않는다 -- graph.list_packs()가 그 pack_id를 집계에 포함하고
    있어도 마찬가지다."""
    registry = [_reg("mine"), _reg("someone-elses-public")]  # list_packs_for가 이미 스코핑한 결과
    agg = [
        _agg("mine", 3, "내 팩"),
        _agg("someone-elses-public", 5, "남의 공개 팩"),
        _agg("someone-elses-private", 7, "남의 비공개 팩"),  # graph에는 있지만 등록부 응답엔 없음
    ]
    result = _call(registry, agg)
    assert {p["pack_id"] for p in result["packs"]} == {"mine", "someone-elses-public"}


def test_list_packs_for_called_with_real_principal_and_sql():
    """list_packs_for가 ctx["sql"]과 current_principal()로 호출된다 --
    다른 principal이면 다른 스코핑 결과가 나온다는 계약의 배선 확인."""
    sql_sentinel = MagicMock()
    with (
        patch("opencrab.mcp.tools._get_context") as mock_ctx,
        patch("opencrab.ontology.pack_registry.load_pack_registry") as mock_reg,
        patch("opencrab.pack.ownership.list_packs_for") as mock_list_for,
    ):
        mock_ctx.return_value = {"neo4j": _graph([_agg("p1", 1)]), "sql": sql_sentinel}
        mock_reg.return_value = []
        mock_list_for.return_value = []
        with principal_scope(_PRINCIPAL):
            result = content_pack_list()
    mock_list_for.assert_called_once_with(sql_sentinel, _PRINCIPAL)
    assert result["packs"] == []
