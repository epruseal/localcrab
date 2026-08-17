"""
Tests for search_nodes_by_keyword() in local (LocalGraphStore) mode.

HybridQuery는 LocalGraphStore를 neo4j 인자로 받아 인스턴스화하며,
ChromaStore는 MagicMock으로 대체한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencrab.ontology.query import HybridQuery
from opencrab.stores.local_graph_store import LocalGraphStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_store(tmp_path) -> LocalGraphStore:
    """임시 디렉토리에 LocalGraphStore 인스턴스를 생성한다."""
    db_path = str(tmp_path / "graph.db")
    return LocalGraphStore(db_path)


@pytest.fixture()
def hybrid(local_store: LocalGraphStore) -> HybridQuery:
    """LocalGraphStore를 사용하는 HybridQuery 인스턴스."""
    chroma = MagicMock()
    chroma.available = False
    return HybridQuery(chroma, local_store)


_PACK_ID = "p1"  # single shared pack across this file's fixtures (issue #147:
# search_nodes now requires a concrete, non-empty pack_ids scope and only
# returns rows whose properties carry a matching pack_id -- these tests are
# about keyword matching / case-insensitivity / space filtering / limit, not
# about pack scoping, so every inserted node is tagged with the same pack and
# every keyword_search call passes that pack's id, keeping the pack predicate
# a true no-op for the behaviour under test instead of leaving pack_ids
# required-but-untested.


def _insert_node(
    store: LocalGraphStore,
    node_type: str,
    node_id: str,
    props: dict,
    space_id: str | None = None,
    pack_id: str = _PACK_ID,
) -> None:
    store.upsert_node(node_type, node_id, {**props, "pack_id": pack_id}, space_id=space_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_keyword_match_name_field(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """name 필드에서 키워드가 일치하는 노드를 반환한다."""
    _insert_node(local_store, "Concept", "node-1", {"name": "machine learning", "description": "AI subfield"})
    _insert_node(local_store, "Concept", "node-2", {"name": "deep sea fishing"})

    results = hybrid.keyword_search("machine learning", pack_ids=[_PACK_ID])

    assert len(results) == 1
    assert results[0]["node"]["name"] == "machine learning"
    assert results[0]["label"] == "Concept"


def test_keyword_match_description_field(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """description 필드에서 키워드가 일치하는 노드를 반환한다."""
    _insert_node(local_store, "Doc", "doc-1", {"name": "alpha", "description": "contains the term ontology"})
    _insert_node(local_store, "Doc", "doc-2", {"name": "beta", "description": "irrelevant content"})

    results = hybrid.keyword_search("ontology", pack_ids=[_PACK_ID])

    assert len(results) == 1
    assert results[0]["node"]["name"] == "alpha"


def test_keyword_case_insensitive(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """키워드 검색은 대소문자를 무시해야 한다."""
    _insert_node(local_store, "Entity", "e-1", {"name": "GraphDatabase"})
    _insert_node(local_store, "Entity", "e-2", {"name": "vector store"})

    # 소문자로 검색해도 대소문자 혼합 name 노드가 일치해야 함
    results_lower = hybrid.keyword_search("graphdatabase", pack_ids=[_PACK_ID])
    assert len(results_lower) == 1
    assert results_lower[0]["node"]["name"] == "GraphDatabase"

    # 대문자로 검색해도 소문자 name 노드가 일치해야 함
    results_upper = hybrid.keyword_search("VECTOR", pack_ids=[_PACK_ID])
    assert len(results_upper) == 1
    assert results_upper[0]["node"]["name"] == "vector store"


def test_keyword_space_filter(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """spaces 파라미터가 주어지면 해당 space의 노드만 반환한다."""
    _insert_node(local_store, "Node", "n-claim", {"name": "claim node", "space": "claim"}, space_id="claim")
    _insert_node(local_store, "Node", "n-policy", {"name": "policy node", "space": "policy"}, space_id="policy")

    # "node" 키워드는 두 노드 모두 매칭되지만 space="claim"으로 필터
    results = hybrid.keyword_search("node", pack_ids=[_PACK_ID], spaces=["claim"])

    assert len(results) == 1
    assert results[0]["node"]["space"] == "claim"


def test_keyword_space_filter_multiple_spaces(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """spaces 리스트에 여러 값이 있을 때 해당 space들의 노드만 반환한다."""
    _insert_node(local_store, "Node", "n-a", {"name": "alpha item", "space": "claim"}, space_id="claim")
    _insert_node(local_store, "Node", "n-b", {"name": "beta item", "space": "policy"}, space_id="policy")
    _insert_node(local_store, "Node", "n-c", {"name": "gamma item", "space": "other"}, space_id="other")

    results = hybrid.keyword_search("item", pack_ids=[_PACK_ID], spaces=["claim", "policy"])

    returned_spaces = {r["node"]["space"] for r in results}
    assert returned_spaces == {"claim", "policy"}
    assert len(results) == 2


def test_keyword_limit(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """limit 파라미터가 반환 결과 수를 제한한다."""
    for i in range(10):
        _insert_node(local_store, "Item", f"item-{i}", {"name": f"target item {i}"})

    results = hybrid.keyword_search("target", pack_ids=[_PACK_ID], limit=3)

    assert len(results) == 3


def test_keyword_no_match(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """일치하는 노드가 없으면 빈 리스트를 반환한다."""
    _insert_node(local_store, "Node", "x-1", {"name": "completely unrelated"})

    results = hybrid.keyword_search("zzz_nonexistent_keyword_zzz", pack_ids=[_PACK_ID])

    assert results == []


def test_keyword_empty_store(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """노드가 없으면 빈 리스트를 반환한다."""
    results = hybrid.keyword_search("anything", pack_ids=[_PACK_ID])

    assert results == []


def test_keyword_match_text_field(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """text 필드에서도 키워드가 매칭되어야 한다."""
    _insert_node(local_store, "Doc", "d-1", {"name": "unrelated", "text": "The ontology defines concepts."})
    _insert_node(local_store, "Doc", "d-2", {"name": "also unrelated", "text": "No matching content here."})

    results = hybrid.keyword_search("defines concepts", pack_ids=[_PACK_ID])

    assert len(results) == 1
    assert results[0]["node"]["text"] == "The ontology defines concepts."


def test_keyword_search_finds_matches_beyond_any_node_scan_cap(
    hybrid: HybridQuery, local_store: LocalGraphStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #86: keyword_search used to call
    ``export_nodes(limit=_BM25_NODE_LIMIT)`` (50,000 by default) and search
    only THOSE rows in Python -- a corpus larger than that cap had its tail
    permanently unreachable by keyword search, silently (no error, just
    fewer/no results). The fix pushes the keyword predicate into the
    store's own query (SQL WHERE / Cypher+Python scan) instead of
    truncating the candidate set first, so it no longer matters where in
    scan order the matches fall -- proved here by patching the cap down to
    10 (seeding 50,000 rows for a unit test isn't practical) and seeding 20
    non-matching nodes first, then 5 matching nodes last: the old
    limit-before-filter code would have scanned only the first 10 rows
    (all noise) and found zero matches."""
    import opencrab.ontology.query as query_module

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 10)
    for i in range(20):
        _insert_node(local_store, "Item", f"noise{i:03d}", {"name": f"unrelated {i}"})
    for i in range(5):
        _insert_node(local_store, "Item", f"hit{i:02d}", {"name": f"needle-in-haystack {i}"})

    results = hybrid.keyword_search("needle", pack_ids=[_PACK_ID], limit=10)

    assert len(results) == 5
    assert all("needle" in r["node"]["name"] for r in results)


def test_keyword_search_matches_non_ascii_case_insensitively(
    hybrid: HybridQuery, local_store: LocalGraphStore
) -> None:
    """issue #86 verifier finding: search_nodes()'s SQL predicate lowers the
    stored value with SQLite's builtin ``LOWER()``, which is ASCII-only -- a
    stored "FÜR" would stay "FÜR" (Ü untouched) and never match a "für"
    keyword even though the keyword side IS lowered correctly in Python. The
    OLD keyword_search lowered both sides in Python (Unicode-aware), so this
    would have passed before the SQL-pushdown fix -- LocalGraphStore now
    overrides SQLite's ``lower`` SQL function with Python's Unicode-aware
    ``str.lower`` (_configure_connection) to keep parity."""
    _insert_node(local_store, "Doc", "d-1", {"name": "Grundgesetz FÜR die Bundesrepublik"})
    _insert_node(local_store, "Doc", "d-2", {"name": "unrelated"})

    results = hybrid.keyword_search("für", pack_ids=[_PACK_ID])

    assert len(results) == 1
    assert results[0]["node"]["name"] == "Grundgesetz FÜR die Bundesrepublik"


def test_keyword_search_tolerates_non_string_property_values(
    hybrid: HybridQuery, local_store: LocalGraphStore
) -> None:
    """issue #86 2nd verifier finding: a non-string value in a searched
    field (e.g. a numeric "name") crashed the LOWER() UDF with
    AttributeError, which sqlite3 surfaces as OperationalError out of the
    whole query -- failing keyword_search for EVERY node in the store, not
    just the offending one, regardless of the search keyword. The OLD
    Python-only keyword_search (``str(val).lower()``), Kuzu's search_nodes
    (``str(props[f]).lower()``), and SQLite's own builtin ``LOWER()``
    (implicit text coercion, e.g. ``LOWER(123) = '123'``) all tolerate this;
    the UDF must too."""
    _insert_node(local_store, "Item", "n-int", {"name": 12345})
    _insert_node(local_store, "Item", "n-float", {"name": 3.14159})
    _insert_node(local_store, "Item", "n-str", {"name": "unrelated text"})

    results = hybrid.keyword_search("123", pack_ids=[_PACK_ID])

    assert len(results) == 1
    assert results[0]["node"]["name"] == 12345


def test_keyword_result_format(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """반환 결과의 형식이 {'node': dict, 'label': str} 이어야 한다."""
    _insert_node(local_store, "Concept", "c-1", {"name": "knowledge graph"})

    results = hybrid.keyword_search("knowledge", pack_ids=[_PACK_ID])

    assert len(results) == 1
    result = results[0]
    assert "node" in result
    assert "label" in result
    assert isinstance(result["node"], dict)
    assert isinstance(result["label"], str)
    assert result["label"] == "Concept"
