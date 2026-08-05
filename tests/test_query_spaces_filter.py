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

KNOWN GAP (codex 적대검증, 최초 수정 이후 발견): FTS leg 의 space 필터는
메커니즘상 정상이지만 실제 프로덕션 데이터에는 무용하다 — doc_sources 를 쓰는
유일한 경로(opencrab/mcp/tools/pack.py 의 legacy ingest, text_as_node=False)
가 space 를 쓸 방법 자체가 없기 때문(함수 시그니처에 space 파라미터가 없음).
#51 의 벡터 leg 와 달리 "구 데이터만 비어있고 신규는 채워짐" 이 아니라 신규
데이터도 영구히 비어있다 — pack.py API 변경(이 fix 의 소유 범위 밖) 없이는
고칠 수 없다. 아래
``test_fts_leg_gap_real_ingestion_shape_has_no_space_tag`` /
``test_hybrid_query_warns_about_fts_space_gap`` 가 이 갭을 실제 ingest 경로와
동일한 메타데이터 형태로 pin 하고, HybridQuery.query 가 이를 QueryOutcome.warnings
로 명시적으로 경고함을 검증한다(조용한 0건이 아니라).
"""
from __future__ import annotations

from unittest.mock import MagicMock

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
# KNOWN GAP (codex hostile review on #52's first pass) — the FTS leg's
# space filter is correct-but-useless against real production data, and
# that severity was originally mis-reported as a "non-blocking caveat".
#
# doc_sources (the table keyword_search reads) has no `space` column; the
# filter reads it from the JSON `metadata` blob. The ONLY production writer
# of doc_sources is opencrab/mcp/tools/pack.py's legacy ingest path
# (`text_as_node=False`) — and that function's signature has no `space`
# parameter anywhere in scope to write one. (The grammar-compliant
# `text_as_node=True` path never touches doc_sources at all — it embeds via
# builder.add_node and explicitly skips upsert_source "to avoid duplicate
# writes".) So unlike #51's vectors (an old-data backfill gap — new writes
# ARE tagged), doc_sources content is *structurally* untaggable today: NO
# row, old or new, ever gets a `space` key without a caller-side API change
# to pack.py — which is owned elsewhere and out of this fix's file
# ownership (opencrab/ontology/query.py's _fts_search/_graph_expand,
# opencrab/stores/*).
#
# These two tests pin that reality using the exact realistic metadata shape
# pack.py's legacy path produces (pack_id only, no space key) — a
# spaces-filtered FTS query strictly and correctly excludes it (matching
# the BM25/vector legs' strict semantics), and HybridQuery.query surfaces
# that as an explicit warning rather than a silent "nothing matched".
# ---------------------------------------------------------------------------


def test_fts_leg_gap_real_ingestion_shape_has_no_space_tag(doc_store):
    if not doc_store.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")

    # Mirrors opencrab/mcp/tools/pack.py's `_ingest_into_pack` legacy path
    # verbatim: `meta = _clean_meta(metadata or {}); meta["pack_id"] = pack_id`
    # — no `space` key, because that function has no space parameter at all.
    doc_store.upsert_source(
        "src-untagged", "JASO M345 apple oil standard classification",
        {"pack_id": "oil-standards-auto-moto", "node_id": "n-untagged"},
    )

    unfiltered = doc_store.keyword_search("JASO M345", limit=10)
    assert unfiltered, "unfiltered search must still find real, untagged production data"

    filtered = doc_store.keyword_search("JASO M345", spaces=["A"], limit=10)
    assert filtered == [], (
        "documents the known gap: untagged doc_sources rows are strictly "
        "excluded by any spaces filter today — there is no production path "
        "that would make this pass with real data (see comment block above)"
    )


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
