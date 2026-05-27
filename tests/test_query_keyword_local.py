"""
Tests for search_nodes_by_keyword() in local (LocalGraphStore) mode.

HybridQuery는 LocalGraphStore를 neo4j 인자로 받아 인스턴스화하며,
ChromaStore는 MagicMock으로 대체한다.
"""

from __future__ import annotations

import os
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


def _insert_node(
    store: LocalGraphStore,
    node_type: str,
    node_id: str,
    props: dict,
    space_id: str | None = None,
) -> None:
    store.upsert_node(node_type, node_id, props, space_id=space_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_keyword_match_name_field(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """name 필드에서 키워드가 일치하는 노드를 반환한다."""
    _insert_node(local_store, "Concept", "node-1", {"name": "machine learning", "description": "AI subfield"})
    _insert_node(local_store, "Concept", "node-2", {"name": "deep sea fishing"})

    results = hybrid.keyword_search("machine learning")

    assert len(results) == 1
    assert results[0]["node"]["name"] == "machine learning"
    assert results[0]["label"] == "Concept"


def test_keyword_match_description_field(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """description 필드에서 키워드가 일치하는 노드를 반환한다."""
    _insert_node(local_store, "Doc", "doc-1", {"name": "alpha", "description": "contains the term ontology"})
    _insert_node(local_store, "Doc", "doc-2", {"name": "beta", "description": "irrelevant content"})

    results = hybrid.keyword_search("ontology")

    assert len(results) == 1
    assert results[0]["node"]["name"] == "alpha"


def test_keyword_case_insensitive(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """키워드 검색은 대소문자를 무시해야 한다."""
    _insert_node(local_store, "Entity", "e-1", {"name": "GraphDatabase"})
    _insert_node(local_store, "Entity", "e-2", {"name": "vector store"})

    # 소문자로 검색해도 대소문자 혼합 name 노드가 일치해야 함
    results_lower = hybrid.keyword_search("graphdatabase")
    assert len(results_lower) == 1
    assert results_lower[0]["node"]["name"] == "GraphDatabase"

    # 대문자로 검색해도 소문자 name 노드가 일치해야 함
    results_upper = hybrid.keyword_search("VECTOR")
    assert len(results_upper) == 1
    assert results_upper[0]["node"]["name"] == "vector store"


def test_keyword_space_filter(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """spaces 파라미터가 주어지면 해당 space의 노드만 반환한다."""
    _insert_node(local_store, "Node", "n-claim", {"name": "claim node", "space": "claim"}, space_id="claim")
    _insert_node(local_store, "Node", "n-policy", {"name": "policy node", "space": "policy"}, space_id="policy")

    # "node" 키워드는 두 노드 모두 매칭되지만 space="claim"으로 필터
    results = hybrid.keyword_search("node", spaces=["claim"])

    assert len(results) == 1
    assert results[0]["node"]["space"] == "claim"


def test_keyword_space_filter_multiple_spaces(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """spaces 리스트에 여러 값이 있을 때 해당 space들의 노드만 반환한다."""
    _insert_node(local_store, "Node", "n-a", {"name": "alpha item", "space": "claim"}, space_id="claim")
    _insert_node(local_store, "Node", "n-b", {"name": "beta item", "space": "policy"}, space_id="policy")
    _insert_node(local_store, "Node", "n-c", {"name": "gamma item", "space": "other"}, space_id="other")

    results = hybrid.keyword_search("item", spaces=["claim", "policy"])

    returned_spaces = {r["node"]["space"] for r in results}
    assert returned_spaces == {"claim", "policy"}
    assert len(results) == 2


def test_keyword_limit(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """limit 파라미터가 반환 결과 수를 제한한다."""
    for i in range(10):
        _insert_node(local_store, "Item", f"item-{i}", {"name": f"target item {i}"})

    results = hybrid.keyword_search("target", limit=3)

    assert len(results) == 3


def test_keyword_no_match(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """일치하는 노드가 없으면 빈 리스트를 반환한다."""
    _insert_node(local_store, "Node", "x-1", {"name": "completely unrelated"})

    results = hybrid.keyword_search("zzz_nonexistent_keyword_zzz")

    assert results == []


def test_keyword_empty_store(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """노드가 없으면 빈 리스트를 반환한다."""
    results = hybrid.keyword_search("anything")

    assert results == []


def test_keyword_match_text_field(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """text 필드에서도 키워드가 매칭되어야 한다."""
    _insert_node(local_store, "Doc", "d-1", {"name": "unrelated", "text": "The ontology defines concepts."})
    _insert_node(local_store, "Doc", "d-2", {"name": "also unrelated", "text": "No matching content here."})

    results = hybrid.keyword_search("defines concepts")

    assert len(results) == 1
    assert results[0]["node"]["text"] == "The ontology defines concepts."


def test_keyword_result_format(hybrid: HybridQuery, local_store: LocalGraphStore) -> None:
    """반환 결과의 형식이 {'node': dict, 'label': str} 이어야 한다."""
    _insert_node(local_store, "Concept", "c-1", {"name": "knowledge graph"})

    results = hybrid.keyword_search("knowledge")

    assert len(results) == 1
    result = results[0]
    assert "node" in result
    assert "label" in result
    assert isinstance(result["node"], dict)
    assert isinstance(result["label"], str)
    assert result["label"] == "Concept"
