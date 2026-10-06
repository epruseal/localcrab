"""Vector request size and graph expansion budget (#53, #59).

#53: the vector leg asked its store for at most 20 candidates whatever the
profile computed. The reranker scores every candidate it receives, so a hit at
vector rank 21 or later can be the best answer. #59: the profile owns the
expansion anchor budget and keeps its current values.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from opencrab.ontology import query as q
from opencrab.ontology.query import HybridQuery, _profile_for_query


def _vector_store(fail_first: bool = False) -> MagicMock:
    store = MagicMock()
    store.available = True
    hit = {
        "id": "v1",
        "document": "a",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    if fail_first:
        store.query = MagicMock(side_effect=[RuntimeError("where rejected"), [hit]])
    else:
        store.query = MagicMock(return_value=[hit])
    return store


def _hybrid(store: MagicMock) -> HybridQuery:
    neo4j = MagicMock()
    neo4j.available = False
    return HybridQuery(store, neo4j)


def _requested(store: MagicMock) -> list[int]:
    return [call.kwargs["n_results"] for call in store.query.call_args_list]


# --- #53 helper ------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "overfetch", "expected"),
    [
        (1, False, 1),
        (1, True, 20),  # the minimum 20 decides
        (5, False, 5),
        (5, True, 20),  # 5 * 4 = 20, minimum and product agree
        (25, False, 25),
        (25, True, 100),
        (80, False, 80),
        (80, True, 320),
        (200, False, 80),
        (200, True, 320),  # both caps
    ],
)
def test_request_size_helper(limit: int, overfetch: bool, expected: int) -> None:
    assert q._vector_request_size(limit, overfetch=overfetch) == expected


def test_product_relation_of_the_constants() -> None:
    """This test reads the unpatched module, before any test patches a constant."""
    assert q._VECTOR_FETCH_MAX == q._VECTOR_LIMIT_MAX * q._VECTOR_OVERFETCH


def test_profile_and_helper_read_the_shared_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the constant to 40: a literal 80 left in either place shows."""
    monkeypatch.setattr(q, "_VECTOR_LIMIT_MAX", 40)
    profile = _profile_for_query("why connect chain", limit=500, graph_depth=1)
    assert profile.vector_limit == 40
    assert q._vector_request_size(500, overfetch=False) == 40
    assert q._vector_request_size(500, overfetch=True) == 160


@pytest.mark.parametrize("rerank", [False, True])
@pytest.mark.parametrize("limit", [30, 50])
def test_public_query_returns_the_requested_count(limit: int, rerank: bool) -> None:
    """The store honors n_results over 120 distinct hits; the old cap returned 20."""

    def fake_query(query_text: str, n_results: int = 10, where=None):
        return [
            {
                "id": f"v{i}",
                "document": f"document {i}",
                "metadata": {"pack_id": "pack-a", "node_id": f"n{i}"},
                "distance": 0.1 + i * 0.001,
            }
            for i in range(min(n_results, 120))
        ]

    store = MagicMock()
    store.available = True
    store.query = fake_query
    outcome = _hybrid(store).query(
        "alpha", pack_ids=["pack-a"], limit=limit, use_bm25=False, use_fts=False, use_rerank=rerank
    )
    assert len(outcome.results) == limit


# --- #53 call paths: (first request, error fallback) -----------------------


@pytest.mark.parametrize(
    ("limit", "post_filter", "expected"),
    [
        (5, False, [5, 20]),
        (25, False, [25, 100]),
        (200, False, [80, 320]),
        (1, True, [20, 20]),
        (5, True, [20, 20]),
        (25, True, [100, 100]),
        (200, True, [320, 320]),
    ],
)
def test_first_request_and_fallback_pairs(
    limit: int, post_filter: bool, expected: list[int]
) -> None:
    store = _vector_store(fail_first=True)
    _hybrid(store)._vector_search(
        "x", spaces=None, limit=limit, pack_ids=["pack-a"], include_unpackaged=post_filter
    )
    assert _requested(store) == expected


@pytest.mark.parametrize(("limit", "expected"), [(10, 10), (40, 40), (80, 80), (200, 80)])
def test_normal_path_requests_the_profile_value(limit: int, expected: int) -> None:
    store = _vector_store()
    _hybrid(store)._vector_search("x", spaces=None, limit=limit, pack_ids=["pack-a"])
    assert _requested(store) == [expected]


def test_query_passes_profile_vector_limit_to_the_store() -> None:
    relation = _vector_store()
    _hybrid(relation).query(
        "why connect chain", pack_ids=["pack-a"], limit=10, use_bm25=False, use_fts=False
    )
    assert _requested(relation) == [80]
    plain = _vector_store()
    _hybrid(plain).query("alpha", pack_ids=["pack-a"], limit=10, use_bm25=False, use_fts=False)
    assert _requested(plain) == [40]


def test_candidate_beyond_rank_20_reaches_the_top_when_reranked() -> None:
    """A lexically strong hit at vector rank 30 is only reachable with a request above 20."""
    texts = [f"generic filler {i}" for i in range(29)] + ["quartz resonance cascade quartz"]

    def fake_query(query_text: str, n_results: int = 10, where=None):
        return [
            {
                "id": f"v{i}",
                "document": texts[i],
                "metadata": {"pack_id": "pack-a", "node_id": f"n{i}"},
                "distance": 0.1 + i * 0.01,
            }
            for i in range(min(n_results, len(texts)))
        ]

    store = MagicMock()
    store.available = True
    store.query = fake_query
    outcome = _hybrid(store).query(
        "quartz resonance cascade", pack_ids=["pack-a"], limit=10, use_bm25=False, use_fts=False
    )
    assert "n29" in [r.node_id for r in outcome.results][:3]


# --- #59 expansion budget ---------------------------------------------------


@pytest.mark.parametrize(
    ("question", "depth_in", "expected"),
    [
        ("alpha", 1, 5),  # plain, depth 1
        ("alpha", 2, 3),  # plain question with a caller depth of 2 (REST)
        ("why alpha", 1, 3),  # relation cue raises depth to 2
        ("connect chain", 1, 3),  # multihop cue raises depth to 3
    ],
)
def test_budget_follows_the_final_graph_depth(question: str, depth_in: int, expected: int) -> None:
    profile = _profile_for_query(question, limit=10, graph_depth=depth_in)
    assert profile.max_expand_anchors == expected


def _graph_fixture(anchor_count: int):
    store = MagicMock()
    store.available = True
    store.query = MagicMock(
        return_value=[
            {
                "id": f"v{i}",
                "document": f"d{i}",
                "metadata": {"pack_id": "pack-a", "node_id": f"n{i}"},
                "distance": 0.1 + i * 0.01,
            }
            for i in range(anchor_count)
        ]
    )
    neo4j = MagicMock()
    neo4j.available = True
    neo4j.find_neighbors = MagicMock(return_value=[])
    return HybridQuery(store, neo4j), neo4j


def test_default_budget_is_unchanged_through_query() -> None:
    hybrid, neo4j = _graph_fixture(anchor_count=12)
    hybrid.query("connect chain", pack_ids=["pack-a"], limit=10, use_bm25=False, use_fts=False)
    assert neo4j.find_neighbors.call_count == 3
    hybrid, neo4j = _graph_fixture(anchor_count=12)
    hybrid.query("alpha", pack_ids=["pack-a"], limit=10, use_bm25=False, use_fts=False)
    assert neo4j.find_neighbors.call_count == 5


def test_profile_budget_reaches_the_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a budget that differs from every default; the anchor pool holds 12 (>= 7)."""
    original = q._profile_for_query
    monkeypatch.setattr(
        q, "_profile_for_query", lambda *a, **k: replace(original(*a, **k), max_expand_anchors=7)
    )
    hybrid, neo4j = _graph_fixture(anchor_count=12)
    hybrid.query("connect chain", pack_ids=["pack-a"], limit=10, use_bm25=False, use_fts=False)
    assert neo4j.find_neighbors.call_count == 7


def test_graph_expand_parameter_and_default() -> None:
    hybrid, neo4j = _graph_fixture(anchor_count=1)
    ids = [f"n{i}" for i in range(9)]
    hybrid._graph_expand(ids, 2, 50, pack_ids=["pack-a"])
    assert neo4j.find_neighbors.call_count == 3  # default rule, depth 2
    neo4j.find_neighbors.reset_mock()
    hybrid._graph_expand(ids, 1, 50, pack_ids=["pack-a"])
    assert neo4j.find_neighbors.call_count == 5  # default rule, depth 1
    neo4j.find_neighbors.reset_mock()
    hybrid._graph_expand(ids, 2, 50, pack_ids=["pack-a"], max_anchors=8)
    assert neo4j.find_neighbors.call_count == 8
