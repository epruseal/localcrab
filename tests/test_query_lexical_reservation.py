"""#556: leg top hits in the combined result (docs/issue-556-fusion.md).

The lexical legs (BM25, FTS) keep their top hit inside the returned window.
A leg top hit that stays outside the window adds a ``fusion:`` warning.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from opencrab.ontology.query import (
    _FOLDED_MEMBERS,
    HybridQuery,
    QueryResult,
    _reserve_leg_tops,
)

FUSION = "fusion:"


def _hybrid(vector=(), bm25=(), fts=()):
    """Build a HybridQuery whose legs return fixed lists."""
    hybrid = HybridQuery(MagicMock(available=False), MagicMock(available=False))
    hybrid._doc_store = MagicMock()
    hybrid._vector_search = MagicMock(return_value=[
        QueryResult(source="vector", node_id=nid, score=score, text=f"v {nid}")
        for nid, score in vector
    ])
    hybrid._bm25_search = MagicMock(return_value=[
        {"source": "bm25", "node_id": nid, "score": score, "text": f"b {nid}", "metadata": {}}
        for nid, score in bm25
    ])
    hybrid._fts_search = MagicMock(return_value=[
        {"source": "keyword", "node_id": nid, "score": score, "text": f"k {nid}",
         "metadata": {}}
        for nid, score in fts
    ])
    return hybrid


def _decoys(n):
    return [f"d{i:02d}" for i in range(n)]


def _fusion(warnings):
    return [w for w in warnings if w.startswith(FUSION)]


def test_rerank_keeps_bm25_top_hit_that_only_one_leg_returns():
    decoys = _decoys(12)
    hybrid = _hybrid(
        vector=[(d, 1.0 - i * 0.01) for i, d in enumerate(decoys)],
        bm25=[("gold", 50.0)] + [(d, 40.0 - i) for i, d in enumerate(decoys)],
        fts=[(d, 9.0 - i * 0.1) for i, d in enumerate(decoys)],
    )
    outcome = hybrid.query("rare common", pack_ids=["pack-a"], limit=5)
    ids = [r.node_id for r in outcome.results]
    assert len(ids) == 5
    assert "gold" in ids
    assert ids[-1] == "gold"
    assert _fusion(outcome.warnings) == []
    scores = [r.score for r in outcome.results]
    assert scores == sorted(scores, reverse=True)


def test_flat_merge_keeps_bm25_top_hit_that_vector_ranked_low():
    decoys = _decoys(8)
    hybrid = _hybrid(
        vector=[(d, 0.9 - i * 0.01) for i, d in enumerate(decoys)] + [("gold", 0.01)],
        bm25=[("gold", 50.0)],
    )
    outcome = hybrid.query("q", pack_ids=["pack-a"], limit=4, use_rerank=False, use_fts=False)
    ids = [r.node_id for r in outcome.results]
    assert ids == ["d00", "d01", "d02", "gold"]
    assert _fusion(outcome.warnings) == []


def test_unchanged_when_every_leg_top_is_inside_the_window(monkeypatch):
    legs = dict(
        vector=[("a", 0.9), ("b", 0.8)],
        bm25=[("a", 9.0), ("c", 8.0)],
        fts=[("b", 3.0)],
    )
    outcome = _hybrid(**legs).query("q", pack_ids=["pack-a"], limit=10)
    monkeypatch.setattr(
        "opencrab.ontology.query._reserve_leg_tops",
        lambda items, place_ids, warn_ids, window: (items, 0),
    )
    plain = _hybrid(**legs).query("q", pack_ids=["pack-a"], limit=10)
    assert [r.node_id for r in outcome.results] == [r.node_id for r in plain.results]
    assert _fusion(outcome.warnings) == []


def _items(*ids):
    return [{"node_id": nid} for nid in ids]


def test_window_four_keeps_a_lexical_top_that_was_already_inside():
    items = _items("A", "B", "C", "F", "M")
    out, outside = _reserve_leg_tops(items, ["M", "F"], [], 4)
    assert [i["node_id"] for i in out] == ["A", "B", "F", "M", "C"]
    assert outside == 0
    assert len(out) == len(items)
    assert {id(x) for x in out} == {id(x) for x in items}


def test_window_two_moves_one_top_and_counts_the_displaced_one():
    out, outside = _reserve_leg_tops(_items("A", "F", "M"), ["M", "F"], [], 2)
    assert [i["node_id"] for i in out[:2]] == ["A", "M"]
    assert outside == 1


def test_window_one_moves_nothing_and_counts_the_top():
    hybrid = _hybrid(
        vector=[("d00", 0.9)],
        bm25=[("gold", 50.0), ("d00", 40.0)],
        fts=[("d00", 3.0)],
    )
    outcome = hybrid.query("q", pack_ids=["pack-a"], limit=1)
    assert [r.node_id for r in outcome.results] == ["d00"]
    assert len(_fusion(outcome.warnings)) == 1


def test_vector_top_outside_the_window_is_warned_not_moved():
    out, outside = _reserve_leg_tops(_items("A", "B", "C", "V"), [], ["V"], 2)
    assert [i["node_id"] for i in out] == ["A", "B", "C", "V"]
    assert outside == 1


def test_folded_member_top_moves_its_group_once():
    rep = {"node_id": "R", _FOLDED_MEMBERS: [{"node_id": "M1"}, {"node_id": "M2"}]}
    items = [*_items("A", "B", "C", "D"), rep]
    out, outside = _reserve_leg_tops(items, ["M1", "M2"], [], 4)
    assert [i["node_id"] for i in out[:4]] == ["A", "B", "C", "R"]
    assert outside == 0
    flat = [x["node_id"] for i in out for x in [i, *i.get(_FOLDED_MEMBERS, [])]]
    assert len(flat) == len(set(flat))


def test_policy_filtered_top_is_neither_moved_nor_warned():
    decoys = _decoys(6)
    hybrid = _hybrid(
        vector=[(d, 0.9 - i * 0.01) for i, d in enumerate(decoys)],
        bm25=[("hidden", 50.0)] + [(d, 40.0 - i) for i, d in enumerate(decoys)],
        fts=[(d, 3.0 - i * 0.1) for i, d in enumerate(decoys)],
    )
    hybrid._rebac = MagicMock()
    hybrid._rebac.check = MagicMock(
        side_effect=lambda subject_id, permission, resource_id: SimpleNamespace(
            granted=(resource_id != "hidden")
        )
    )
    outcome = hybrid.query("q", pack_ids=["pack-a"], limit=4, subject_id="u1")
    assert "hidden" not in [r.node_id for r in outcome.results]
    assert _fusion(outcome.warnings) == []


def test_limit_zero_and_negative_keep_the_old_slice():
    decoys = _decoys(6)
    legs = dict(
        vector=[(d, 0.9 - i * 0.01) for i, d in enumerate(decoys)],
        bm25=[("gold", 50.0)] + [(d, 40.0 - i) for i, d in enumerate(decoys)],
        fts=[(d, 3.0 - i * 0.1) for i, d in enumerate(decoys)],
    )
    zero = _hybrid(**legs).query("q", pack_ids=["pack-a"], limit=0)
    assert zero.results == []
    assert _fusion(zero.warnings) == []
    neg = _hybrid(**legs).query("q", pack_ids=["pack-a"], limit=-2)
    full = _hybrid(**legs).query("q", pack_ids=["pack-a"], limit=100)
    assert [r.node_id for r in neg.results] == [r.node_id for r in full.results][:-2]
    assert _fusion(neg.warnings) == []
