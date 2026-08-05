"""issue #52 — spaces 필터가 그래프 확장/FTS leg 에서 무시되던 결함의 회귀 테스트.

대상 (issue 본문의 코드 앵커 기준):
    - HybridQuery._fts_search: spaces 를 받지만 keyword_search 로 전달하지 않음.
    - HybridQuery._graph_expand: spaces 파라미터 자체가 없어 find_neighbors 에
      전달 불가능했음.

이 파일의 각 테스트는 fix 이전 코드에서 실패한다(직접 stash 로 확인, 커밋 메시지
참조): _graph_expand 관련 테스트는 이전 시그니처에 ``spaces`` 키워드 인자 자체가
없어 TypeError 로 실패하고, _fts_search 관련 테스트는 spaces 가 전달되지 않아
어서션이 실패한다. 통합 경로 테스트는 space B 데이터가 결과에 섞여 들어와 실패한다.

``tests/test_query_fts_hybrid.py`` 의 4개 _fts_search 테스트는 전부 spaces=None
으로만 호출하고, ``tests/test_query_keyword_local.py`` 의 space 필터 테스트는
레거시 ``HybridQuery.keyword_search()`` (다른 코드 경로, issue #86 소유)를 겨냥한
것이라 이 갭을 덮지 못했다 — 이 파일이 그 갭을 채운다.

PRE-BACKFILL GAP (codex 적대검증 2라운드): FTS leg 의 space 필터 메커니즘은
정상이고, opencrab/mcp/tools/pack.py 의 legacy ingest(text_as_node=False)
가 이제 ``meta.setdefault("space", "evidence")`` 로 신규 데이터를 태깅한다
(apps/api/main.py 의 ingest_text 는 이미 호출자 metadata 를 그대로 흘려보내
호출자가 지정한 space 도 그대로 존중된다). #51 의 벡터 leg 와 동일한 종류의
갭만 남는다: 이 fix 이전에 적재된 행은 space 태그가 없어 backfill 전까지
spaces 필터에서 제외된다. 아래
``test_fts_leg_pre_backfill_legacy_row_excluded_until_backfill`` 이 그 구
데이터 갭을 pin 하고,
``test_ingest_into_pack_legacy_path_tags_space_and_filter_finds_it`` /
``test_ingest_into_pack_legacy_path_respects_caller_supplied_space`` 가 실제
프로덕션 함수(``_ingest_into_pack``)로 적재한 신규 데이터는 필터가 정상
동작함을 증명한다. ``test_hybrid_query_warns_about_fts_space_gap`` 은
HybridQuery.query 가 이 backfill 갭을 QueryOutcome.warnings 로 명시적으로
경고함을 검증한다(조용한 0건이 아니라).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.ontology.query import HybridQuery
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

# ---------------------------------------------------------------------------
# Leg 1 — HybridQuery._fts_search must forward `spaces` to keyword_search
# ---------------------------------------------------------------------------


class _FakeDocStoreCapturesSpaces:
    supports_keyword = True

    def __init__(self) -> None:
        self.received_spaces: list[list[str] | None] = []

    def keyword_search(self, query, pack_ids=None, include_unpackaged=False, limit=20, spaces=None):
        self.received_spaces.append(spaces)
        return [{"node_id": "x", "score": 1.0, "text": "t", "metadata": {"space": "A"}}]


def test_fts_search_forwards_spaces_to_keyword_search():
    """Pins the exact bug: _fts_search accepted `spaces` (2nd positional
    param) but never passed it into ds.keyword_search(...). Pre-fix, this
    fails because `received_spaces == [None]`."""
    hq = HybridQuery(MagicMock(), MagicMock())
    fake = _FakeDocStoreCapturesSpaces()
    hq._doc_store = fake

    hq._fts_search("JASO M345", ["A"], 10)

    assert fake.received_spaces == [["A"]]


# ---------------------------------------------------------------------------
# Leg 2 — HybridQuery._graph_expand must accept and forward `spaces`
# ---------------------------------------------------------------------------


class _FakeGraphCapturesSpaces:
    available = True

    def __init__(self) -> None:
        self.received_spaces: list[list[str] | None] = []

    def find_neighbors(self, node_id, direction="both", depth=1, limit=50, spaces=None):
        self.received_spaces.append(spaces)
        return []


def test_graph_expand_accepts_and_forwards_spaces():
    """Pins the exact bug: _graph_expand had no `spaces` parameter at all.
    Pre-fix, calling with spaces=[...] raises TypeError (unexpected keyword
    argument) since the parameter didn't exist."""
    hq = HybridQuery(MagicMock(), _FakeGraphCapturesSpaces())

    hq._graph_expand(["anchor"], depth=1, limit=10, spaces=["A"])

    assert hq._neo4j.received_spaces == [["A"]]


# ---------------------------------------------------------------------------
# Leg 1 (real backend) — LocalSQLDocStore.keyword_search space pushdown
# ---------------------------------------------------------------------------


@pytest.fixture()
def doc_store(tmp_path):
    s = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert s.available
    return s


def _seed_two_spaces(store):
    store.upsert_source(
        "src-a", "JASO M345 apple oil standard text",
        {"space": "A", "node_id": "n-a"},
    )
    store.upsert_source(
        "src-b", "JASO M345 banana oil standard text",
        {"space": "B", "node_id": "n-b"},
    )


def test_local_sql_doc_store_keyword_search_space_filter(doc_store):
    if not doc_store.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")
    _seed_two_spaces(doc_store)

    hits = doc_store.keyword_search("JASO M345", spaces=["A"], limit=10)

    assert hits, "space A 문서가 검색돼야 함"
    assert all(h["metadata"].get("space") == "A" for h in hits)
    assert all(h["source_id"] != "src-b" for h in hits)


def test_local_sql_doc_store_keyword_search_no_spaces_is_unfiltered(doc_store):
    """spaces=None 은 기존처럼 전 space 를 검색한다(하위호환)."""
    if not doc_store.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")
    _seed_two_spaces(doc_store)

    hits = doc_store.keyword_search("JASO M345", limit=10)

    ids = {h["source_id"] for h in hits}
    assert {"src-a", "src-b"} <= ids


# ---------------------------------------------------------------------------
# Leg 2 (real backend) — LocalGraphStore.find_neighbors space pushdown,
# including the #62-style hub-fanout-beyond-limit push-before-limit proof.
# ---------------------------------------------------------------------------


@pytest.fixture()
def graph_store(tmp_path):
    return LocalGraphStore(str(tmp_path / "graph.db"))


def test_local_graph_store_find_neighbors_space_filter(graph_store):
    graph_store.upsert_node("Claim", "anchor", {"title": "anchor"}, space_id="A")
    graph_store.upsert_node("Claim", "a1", {"title": "a1"}, space_id="A")
    graph_store.upsert_node("Claim", "b1", {"title": "b1"}, space_id="B")
    graph_store.upsert_edge("Claim", "anchor", "rel", "Claim", "a1", {})
    graph_store.upsert_edge("Claim", "anchor", "rel", "Claim", "b1", {})

    rows = graph_store.find_neighbors("anchor", direction="out", depth=1, spaces=["A"])

    ids = {r["properties"].get("id") for r in rows}
    assert ids == {"a1"}


def test_local_graph_store_anchor_outside_space_returns_empty(graph_store):
    graph_store.upsert_node("Claim", "anchor", {"title": "anchor"}, space_id="B")
    graph_store.upsert_node("Claim", "a1", {"title": "a1"}, space_id="A")
    graph_store.upsert_edge("Claim", "anchor", "rel", "Claim", "a1", {})

    rows = graph_store.find_neighbors("anchor", spaces=["A"])

    assert rows == []


def test_local_graph_store_space_filter_survives_hub_fanout_beyond_limit(graph_store):
    """Mirrors test_find_neighbors_contract.py's issue #62 pack-filter proof,
    for the space filter: the space filter must apply BEFORE limit, not
    after. 100 foreign-space edges are upserted first, then 5 in-space
    edges last — a backend that applies LIMIT before the space filter (a
    Python post-filter over a LIMIT-capped SQL result) sees only
    foreign-space rows and returns nothing; the SQL-pushdown fix finds all
    5 regardless of insertion order."""
    graph_store.upsert_node("Hub", "h", {}, space_id="A")
    for i in range(100):
        graph_store.upsert_node("Other", f"o{i}", {}, space_id="B")
        graph_store.upsert_edge("Hub", "h", "touches", "Other", f"o{i}", {})
    for i in range(5):
        graph_store.upsert_node("Target", f"p{i}", {}, space_id="A")
        graph_store.upsert_edge("Hub", "h", "touches", "Target", f"p{i}", {})

    rows = graph_store.find_neighbors("h", direction="out", depth=1, limit=10, spaces=["A"])

    ids = {r["properties"].get("id") for r in rows}
    assert ids == {f"p{i}" for i in range(5)}


# ---------------------------------------------------------------------------
# Integrated path — HybridQuery.query(spaces=["A"]) end to end, real
# LocalSQLDocStore (FTS anchor) + real LocalGraphStore (graph expansion).
# Space B data is seeded on both the anchor's own leg and the graph
# neighbourhood, so a leak on EITHER leg makes this fail.
# ---------------------------------------------------------------------------


class _UnavailableChroma:
    available = False


@pytest.fixture()
def hybrid(tmp_path):
    doc = LocalSQLDocStore(str(tmp_path / "doc.db"))
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    hq = HybridQuery(_UnavailableChroma(), graph)  # type: ignore[arg-type]
    hq._doc_store = doc
    return hq, doc, graph


def test_hybrid_query_spaces_filter_end_to_end(hybrid):
    hq, doc, graph = hybrid
    if not doc.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")

    # FTS anchor: space A only fires on the exact query text below.
    doc.upsert_source(
        "src-anchor-a",
        "JASO M345 apple oil standard classification",
        {"space": "A", "node_id": "anchor-a"},
    )
    doc.upsert_source(
        "src-anchor-b",
        "JASO M345 banana oil standard classification",
        {"space": "B", "node_id": "anchor-b"},
    )

    # Graph: anchor-a (space A) has neighbours in BOTH spaces.
    graph.upsert_node("Claim", "anchor-a", {"title": "anchor-a"}, space_id="A")
    graph.upsert_node("Claim", "neigh-a", {"title": "neigh-a"}, space_id="A")
    graph.upsert_node("Claim", "neigh-b", {"title": "neigh-b"}, space_id="B")
    graph.upsert_edge("Claim", "anchor-a", "rel", "Claim", "neigh-a", {})
    graph.upsert_edge("Claim", "anchor-a", "rel", "Claim", "neigh-b", {})

    outcome = hq.query("JASO M345 apple oil standard classification", spaces=["A"], limit=10)
    result_ids = {r.node_id for r in outcome}

    assert "anchor-b" not in result_ids, "FTS leg leaked space B"
    assert "neigh-b" not in result_ids, "graph leg leaked space B"
    assert "anchor-a" in result_ids or "neigh-a" in result_ids, "space A data should still surface"
    for r in outcome:
        space = (r.metadata or {}).get("space")
        assert space in (None, "A"), f"result {r.node_id!r} has foreign space {space!r}"


# ---------------------------------------------------------------------------
# PRE-BACKFILL LEGACY ROWS (codex hostile review, round 2) — the FTS leg's
# space filter is now correctly populated by production writers going
# forward (opencrab/mcp/tools/pack.py's legacy ingest path defaults
# meta.setdefault("space", "evidence"); apps/api/main.py's ingest_text
# endpoint already passes caller-supplied metadata straight through, so a
# caller-set "space" is honored too). What's still true, and what this test
# pins, is that ROWS WRITTEN BEFORE THIS FIX have no 'space' key in their
# metadata (nobody could have written one) and so are strictly excluded by
# any spaces filter, matching the BM25/vector legs' strict semantics, until
# a backfill runs. This is the same shape of gap as #51's vectors, not a
# "structurally unfixable" one — see
# test_ingest_into_pack_legacy_path_tags_space_and_filter_finds_it below for
# proof that NEW writes through the real production function are found.
# ---------------------------------------------------------------------------


def test_fts_leg_pre_backfill_legacy_row_excluded_until_backfill(doc_store):
    if not doc_store.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")

    # Simulates a row written BEFORE this fix shipped — no `space` key,
    # because no writer populated one yet at that point in time.
    doc_store.upsert_source(
        "src-untagged", "JASO M345 apple oil standard classification",
        {"pack_id": "oil-standards-auto-moto", "node_id": "n-untagged"},
    )

    unfiltered = doc_store.keyword_search("JASO M345", limit=10)
    assert unfiltered, "unfiltered search must still find real, untagged legacy data"

    filtered = doc_store.keyword_search("JASO M345", spaces=["A"], limit=10)
    assert filtered == [], (
        "documents the pre-backfill gap: untagged doc_sources rows are "
        "strictly excluded by any spaces filter until backfilled — "
        "see the passing test below for post-fix new-write behavior"
    )


def test_ingest_into_pack_legacy_path_tags_space_and_filter_finds_it(tmp_path):
    """Proves the root-cause fix through the REAL production function, not
    a hand-seeded store: opencrab.mcp.tools._ingest_into_pack's legacy
    branch (text_as_node=False) now defaults space="evidence", so content
    ingested through it is found by a spaces=["evidence"]-filtered FTS
    query — no manual metadata seeding involved."""
    from opencrab.mcp.tools import _ingest_into_pack

    doc = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not doc.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")

    ctx = {
        "neo4j": MagicMock(available=False),
        "chroma": MagicMock(available=False),
        "mongo": doc,
        "sql": MagicMock(),
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        _ingest_into_pack(
            "pack-a",
            text="JASO M345 apple oil standard classification",
            source_id="src-real-ingest",
            text_as_node=False,
        )

    hits = doc.keyword_search("JASO M345", spaces=["evidence"], limit=10)
    assert any(h["source_id"] == "src-real-ingest" for h in hits), (
        "data ingested via the real production legacy path must be "
        "findable through a spaces-filtered FTS query"
    )
    assert doc.keyword_search("JASO M345", spaces=["other-space"], limit=10) == []


def test_ingest_into_pack_legacy_path_respects_caller_supplied_space(tmp_path):
    """apps/api/main.py's ingest_text passes caller metadata straight
    through — a caller-set "space" must win over the "evidence" default."""
    from opencrab.mcp.tools import _ingest_into_pack

    doc = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not doc.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")

    ctx = {
        "neo4j": MagicMock(available=False),
        "chroma": MagicMock(available=False),
        "mongo": doc,
        "sql": MagicMock(),
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        _ingest_into_pack(
            "pack-a",
            text="JASO M345 banana oil standard classification",
            source_id="src-custom-space",
            metadata={"space": "resource"},
            text_as_node=False,
        )

    assert doc.keyword_search("JASO M345", spaces=["resource"], limit=10)
    assert doc.keyword_search("JASO M345", spaces=["evidence"], limit=10) == []


def test_hybrid_query_warns_about_fts_space_gap(hybrid):
    hq, doc, _graph = hybrid
    if not doc.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")

    doc.upsert_source(
        "src-untagged", "JASO M345 apple oil standard classification",
        {"pack_id": "oil-standards-auto-moto", "node_id": "n-untagged"},
    )

    outcome = hq.query("JASO M345 apple oil standard classification", spaces=["A"], limit=10)

    assert "n-untagged" not in {r.node_id for r in outcome}
    assert any("FTS/keyword leg" in w for w in outcome.warnings), (
        "spaces-filtered query must warn that the FTS leg cannot match "
        "any real doc_sources data today, not fail silently"
    )
