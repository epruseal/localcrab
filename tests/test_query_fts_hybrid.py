"""키워드(FTS5) 하이브리드 레그 테스트 — 정상/오류/실패/엣지.

대상:
- LocalSQLDocStore.keyword_search / supports_keyword (sqlite FTS5)
- HybridQuery._fts_search 의 capability 게이팅·graceful fallback

설계 의도: 약어·영어·표준번호(JASO M345, FB, FC) 같은 다중어 질의에서 본문
다중어 매칭 문서가 단일어 충돌 문서보다 상위로 와야 한다(전역 검색 정확도).
"""
from __future__ import annotations

import pytest

from opencrab.stores.local_sql_doc_store import LocalSQLDocStore


@pytest.fixture()
def store(tmp_path):
    s = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert s.available
    return s


def _seed(store):
    # 다중어(전부 포함) 문서 — oil 팩
    store.upsert_source(
        "src-oil-m345",
        "JASO M345 two-stroke 2T motorcycle engine oil standard. "
        "Classification FB FC FD detergency smoke lubricity exhaust torque.",
        {"pack_id": "oil-standards-auto-moto", "node_id": "n-oil-m345"},
    )
    # 단일어(FC만) 충돌 문서 — moto 팩
    store.upsert_source(
        "src-husq-fc",
        "Husqvarna FC 350 motocross bike specifications.",
        {"pack_id": "moto-catalog-husqvarna", "node_id": "n-husq-fc"},
    )
    # 한국어 문서
    store.upsert_source(
        "src-ko",
        "JASO MA2 습식클러치 마찰 규격 모터사이클 오일.",
        {"pack_id": "oil-standards-auto-moto", "node_id": "n-ko"},
    )


# ── capability ──
def test_supports_keyword_flag(store):
    # FTS5 가용 시 True. (FTS5 미빌드 SQLite면 False로 정직하게 노출되어야 함)
    assert isinstance(store.supports_keyword, bool)


# ── 정상: 다중어 매칭이 단일어 충돌보다 상위 ──
def test_keyword_multiterm_outranks_single(store):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")
    _seed(store)
    hits = store.keyword_search("JASO M345 detergency smoke lubricity FB FC", limit=10)
    ids = [h["source_id"] for h in hits]
    assert "src-oil-m345" in ids, "다중어 문서가 검색돼야 함"
    if "src-husq-fc" in ids:
        assert ids.index("src-oil-m345") < ids.index("src-husq-fc"), \
            "다중어 문서가 단일어 충돌 문서보다 상위여야 함"
    # 반환 구조
    h0 = hits[0]
    for k in ("source_id", "text", "metadata", "score"):
        assert k in h0


# ── 정상: 표준번호 토큰 단독 ──
def test_keyword_standard_number(store):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable")
    _seed(store)
    hits = store.keyword_search("M345", limit=5)
    assert any(h["source_id"] == "src-oil-m345" for h in hits)


# ── pack 필터 ──
def test_keyword_pack_filter(store):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable")
    _seed(store)
    hits = store.keyword_search("FC", pack_ids=["oil-standards-auto-moto"], limit=10)
    for h in hits:
        assert h["metadata"].get("pack_id") == "oil-standards-auto-moto"
    # moto 팩 문서는 제외
    assert all(h["source_id"] != "src-husq-fc" for h in hits)


# ── 오류: FTS 특수문자/구문 → 예외 없이 처리 ──
@pytest.mark.parametrize("q", ['"', "*", "AND OR NOT", "M345:", "(FB", 'a"b*c', "  "])
def test_keyword_special_chars_no_crash(store, q):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable")
    _seed(store)
    out = store.keyword_search(q, limit=5)  # 예외 발생하면 실패
    assert isinstance(out, list)


# ── 엣지: 빈 질의·무매칭 ──
def test_keyword_empty_and_nomatch(store):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable")
    _seed(store)
    assert store.keyword_search("", limit=5) == []
    assert store.keyword_search("   ", limit=5) == []
    assert store.keyword_search("zzzznonexistenttoken", limit=5) == []


# ── 동기화: upsert_source 후 FTS 갱신 ──
def test_keyword_sync_on_upsert(store):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable")
    store.upsert_source("s1", "alpha bravo charlie", {"pack_id": "p"})
    assert any(h["source_id"] == "s1" for h in store.keyword_search("bravo"))
    # 본문 교체 → 옛 토큰 사라지고 새 토큰 검색됨
    store.upsert_source("s1", "delta echo foxtrot", {"pack_id": "p"})
    assert store.keyword_search("bravo") == [] or all(
        h["source_id"] != "s1" for h in store.keyword_search("bravo"))
    assert any(h["source_id"] == "s1" for h in store.keyword_search("echo"))


# ── 엣지: limit 준수 ──
def test_keyword_limit(store):
    if not store.supports_keyword:
        pytest.skip("FTS5 unavailable")
    for i in range(15):
        store.upsert_source(f"m{i}", "common token here", {"pack_id": "p"})
    assert len(store.keyword_search("common", limit=5)) <= 5


# ── HybridQuery._fts_search capability 게이팅·폴백 ──
class _FakeStoreNoKeyword:
    supports_keyword = False
    def keyword_search(self, *a, **k):  # 호출되면 안 됨
        raise AssertionError("미지원 백엔드에서 호출되면 안 됨")


class _FakeStoreRaises:
    supports_keyword = True
    def keyword_search(self, *a, **k):
        raise RuntimeError("backend error")


class _FakeStoreOK:
    supports_keyword = True
    def keyword_search(self, query, pack_ids=None, include_unpackaged=False, limit=20):
        return [{"source_id": "x", "node_id": "x", "text": "JASO M345 ...",
                 "metadata": {"pack_id": "oil-standards-auto-moto", "node_id": "x"},
                 "score": 1.0}]


def _hybrid():
    from unittest.mock import MagicMock
    from opencrab.ontology.query import HybridQuery
    hq = HybridQuery(MagicMock(), MagicMock())
    return hq


def test_fts_leg_unsupported_backend_fallback():
    hq = _hybrid(); hq._doc_store = _FakeStoreNoKeyword()
    assert hq._fts_search("q", None, 10) == []   # 폴백, 크래시 없음


def test_fts_leg_backend_error_graceful():
    hq = _hybrid(); hq._doc_store = _FakeStoreRaises()
    assert hq._fts_search("q", None, 10) == []   # 예외 흡수


def test_fts_leg_ok_returns_keyword_source():
    hq = _hybrid(); hq._doc_store = _FakeStoreOK()
    out = hq._fts_search("JASO M345", None, 10)
    assert out and out[0].get("source") == "keyword"
    assert out[0]["metadata"]["pack_id"] == "oil-standards-auto-moto"


def test_fts_leg_no_doc_store():
    hq = _hybrid(); hq._doc_store = None
    assert hq._fts_search("q", None, 10) == []
