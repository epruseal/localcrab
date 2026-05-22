from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencrab.ontology.query import HybridQuery, _build_chroma_where


# ---------------------------------------------------------------------------
# T3 — _build_chroma_where four cases
# ---------------------------------------------------------------------------


def test_t3_build_where_none() -> None:
    assert _build_chroma_where() is None
    assert _build_chroma_where(spaces=None, pack_ids=None) is None


def test_t3_build_where_spaces_only_single() -> None:
    assert _build_chroma_where(spaces=["claim"]) == {"space": "claim"}


def test_t3_build_where_spaces_only_multi() -> None:
    where = _build_chroma_where(spaces=["claim", "policy"])
    assert where == {"space": {"$in": ["claim", "policy"]}}


def test_t3_build_where_pack_ids_only_single() -> None:
    assert _build_chroma_where(pack_ids=["pack-a"]) == {"pack_id": "pack-a"}


def test_t3_build_where_pack_ids_only_multi() -> None:
    where = _build_chroma_where(pack_ids=["pack-a", "pack-b"])
    assert where == {"pack_id": {"$in": ["pack-a", "pack-b"]}}


def test_t3_build_where_combined() -> None:
    where = _build_chroma_where(spaces=["claim"], pack_ids=["pack-a"])
    assert where == {
        "$and": [{"space": "claim"}, {"pack_id": "pack-a"}],
    }


# ---------------------------------------------------------------------------
# T12 — Chroma where fallback on exception
# ---------------------------------------------------------------------------


def _make_hybrid_with_chroma(query_mock: MagicMock) -> HybridQuery:
    chroma = MagicMock()
    chroma.available = True
    chroma.query = query_mock
    neo4j = MagicMock()
    neo4j.available = False
    return HybridQuery(chroma, neo4j)


def test_t12_vector_search_server_side_when_no_unpackaged() -> None:
    hit = {
        "id": "v1",
        "document": "alpha",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    query_mock = MagicMock(return_value=[hit])
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search(
        "alpha", spaces=None, limit=5,
        pack_ids=["pack-a"], include_unpackaged=False,
    )
    # server-side where used
    kwargs = query_mock.call_args.kwargs
    assert kwargs["where"] == {"pack_id": "pack-a"}
    assert len(results) == 1
    assert results[0].node_id == "n1"


def test_t12_vector_search_fallback_on_exception() -> None:
    hit_a = {
        "id": "v1",
        "document": "alpha",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    hit_b = {
        "id": "v2",
        "document": "beta",
        "metadata": {"pack_id": "pack-b", "node_id": "n2"},
        "distance": 0.2,
    }
    call_state = {"first": True}

    def fake_query(**kwargs):
        if call_state["first"]:
            call_state["first"] = False
            raise RuntimeError("simulated where rejection")
        return [hit_a, hit_b]

    query_mock = MagicMock(side_effect=fake_query)
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search(
        "alpha", spaces=None, limit=5,
        pack_ids=["pack-a"], include_unpackaged=False,
    )
    # First call asked for server-side filter; the second was a wider scan.
    assert query_mock.call_count == 2
    # Only pack-a hit survives the Python post-filter.
    assert [r.node_id for r in results] == ["n1"]


def test_t12_vector_search_post_filter_when_unpackaged() -> None:
    hit_a = {
        "id": "v1",
        "document": "alpha",
        "metadata": {"pack_id": "pack-a", "node_id": "n1"},
        "distance": 0.1,
    }
    hit_orphan = {
        "id": "v2",
        "document": "orphan",
        "metadata": {"node_id": "n2"},
        "distance": 0.3,
    }
    hit_foreign = {
        "id": "v3",
        "document": "foreign",
        "metadata": {"pack_id": "pack-b", "node_id": "n3"},
        "distance": 0.4,
    }
    query_mock = MagicMock(return_value=[hit_a, hit_orphan, hit_foreign])
    hybrid = _make_hybrid_with_chroma(query_mock)
    results = hybrid._vector_search(
        "x", spaces=None, limit=5,
        pack_ids=["pack-a"], include_unpackaged=True,
    )
    # Server-side where dropped pack_ids; Python post-filter keeps pack-a
    # and unpackaged but rejects foreign pack-b.
    kwargs = query_mock.call_args.kwargs
    assert kwargs["where"] is None
    ids = sorted(r.node_id for r in results)
    assert ids == ["n1", "n2"]
