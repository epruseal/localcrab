"""#482: fold query results with the same pack and the same stored bodies.

Contract: docs/ARCHITECTURE.md, "질의 결과의 동일 본문 접기".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from opencrab.ontology import query as query_mod
from opencrab.ontology.query import (
    HybridQuery,
    QueryOutcome,
    QueryResult,
    _fold_identical_bodies,
    _tag_fold_bodies,
)

from ._vec_helpers import build_vector_store

DUP = "Q: [poll heartbeat]\n\nA: status ok heartbeat reply"
PACK = "p1"


def _item(node_id: str, text: str | None, pack: str | None = PACK, **extra: Any) -> dict:
    meta: dict[str, Any] = {"node_id": node_id}
    if pack:
        meta["pack_id"] = pack
    return {"source": "vector", "node_id": node_id, "score": 1.0, "text": text,
            "metadata": meta, **extra}


# ---------------------------------------------------------------------------
# Unit: _fold_identical_bodies
# ---------------------------------------------------------------------------


def _ids(items):
    return [i["node_id"] for i in items]


def test_same_pack_same_body_folds_into_first():
    items = _tag_fold_bodies([_item("a", "S"), _item("b", "S"), _item("c", "T")])
    out = _fold_identical_bodies(items)
    assert _ids(out) == ["a", "c"]
    assert _ids(out[0]["_folded_members"]) == ["b"]


def test_different_pack_does_not_fold():
    items = _tag_fold_bodies([_item("a", "S", pack="p1"), _item("b", "S", pack="p2")])
    assert _ids(_fold_identical_bodies(items)) == ["a", "b"]


def test_partial_body_overlap_does_not_fold():
    items = _tag_fold_bodies([_item("a", "S"), _item("x", "S")])
    items[1]["_fold_bodies"] = frozenset({"S", "T"})
    assert _ids(_fold_identical_bodies(items)) == ["a", "x"]


def test_items_without_body_key_do_not_fold():
    # BM25 and graph legs are never tagged; empty and None bodies get no key.
    items = [_item("g1", "same synthesized"), _item("g2", "same synthesized")]
    items += _tag_fold_bodies([_item("e1", ""), _item("e2", ""), _item("n1", None), _item("n2", None)])
    assert _ids(_fold_identical_bodies(items)) == ["g1", "g2", "e1", "e2", "n1", "n2"]


def test_existing_metadata_key_blocks_the_group_and_keeps_order():
    a = _item("a", "S")
    a["metadata"]["folded_duplicates"] = "caller-value"
    items = _tag_fold_bodies([a, _item("m", "M"), _item("b", "S")])
    out = _fold_identical_bodies(items)
    assert _ids(out) == ["a", "m", "b"]
    assert out[0]["metadata"]["folded_duplicates"] == "caller-value"
    assert "_folded_members" not in out[0]


def test_fold_keeps_order_of_other_items_and_does_not_mutate_input():
    items = _tag_fold_bodies([_item("a", "S"), _item("m", "M"), _item("b", "S"), _item("z", "Z")])
    before = [dict(i) for i in items]
    out = _fold_identical_bodies(items)
    assert _ids(out) == ["a", "m", "z"]
    assert [dict(i) for i in items] == before
    assert "_folded_members" not in items[0]


# ---------------------------------------------------------------------------
# HybridQuery.query with controlled legs
# ---------------------------------------------------------------------------


class _NoGraph:
    available = False


def _hq_with_legs(vector=(), bm25=(), fts=()) -> HybridQuery:
    hq = HybridQuery(MagicMock(), _NoGraph())  # type: ignore[arg-type]
    hq._doc_store = MagicMock()
    hq._vector_search = lambda *a, **k: [QueryResult(**v) for v in vector]  # type: ignore[method-assign]
    hq._bm25_search_with_warnings = lambda *a, **k: ([dict(b) for b in bm25], [])  # type: ignore[method-assign]
    hq._fts_search = lambda *a, **k: [dict(f) for f in fts]  # type: ignore[method-assign]
    return hq


def _vec(node_id: str, text: str, score: float, **meta: Any) -> dict:
    return {"source": "vector", "node_id": node_id, "score": score, "text": text,
            "metadata": {"node_id": node_id, "pack_id": PACK, **meta}}


def _bm(node_id: str, text: str, score: float) -> dict:
    return {"source": "bm25", "node_id": node_id, "score": score, "text": text,
            "metadata": {"node_id": node_id, "pack_id": PACK}}


def _run(hq: HybridQuery, rerank: bool, fold: bool = True, **kw: Any) -> QueryOutcome:
    if fold:
        return hq.query("question", pack_ids=[PACK], use_rerank=rerank, limit=10, **kw)
    with patch.object(query_mod, "_fold_identical_bodies", lambda items: items):
        return hq.query("question", pack_ids=[PACK], use_rerank=rerank, limit=10, **kw)


@pytest.mark.parametrize("rerank", [True, False])
def test_member_keeps_every_field_it_had_unfolded(rerank):
    # Same stored body, different BM25-joined text and different raw scores.
    legs = dict(
        vector=[_vec("a", "S", 0.9, doc="RED"), _vec("b", "S", 0.6, doc="BLUE")],
        bm25=[_bm("a", "DETAIL_RED", 5.0), _bm("b", "DETAIL_BLUE", 3.0)],
    )
    folded = _run(_hq_with_legs(**legs), rerank)
    unfolded = _run(_hq_with_legs(**legs), rerank, fold=False)

    assert len(folded) == 1
    rep = folded[0]
    block = rep.metadata["folded_duplicates"]
    assert block["count"] == 1 == rep.folded_count
    by_id = {r.node_id: r.to_dict() for r in unfolded}
    assert block["members"] == [by_id[m["node_id"]] for m in block["members"]]
    member = block["members"][0]
    assert member["metadata"]["doc"] == "BLUE"
    if rerank:
        # The reranker joins the BM25 text onto the vector body.
        assert "DETAIL_BLUE" in member["text"]
    else:
        # The flat merge keeps the first occurrence's text (pre-existing rule).
        assert member["raw_score"] is not None
    rep_dict = rep.to_dict()
    rep_dict["metadata"].pop("folded_duplicates")
    assert rep_dict == by_id[rep.node_id]


_X_BODY_ORDERS = {
    "vector_S_then_T": dict(vector=[_vec("a", "S", 0.9), _vec("x", "S", 0.8), _vec("x", "T", 0.7)]),
    "vector_T_then_S": dict(vector=[_vec("a", "S", 0.9), _vec("x", "T", 0.8), _vec("x", "S", 0.7)]),
    "vector_T_then_fts_S": dict(
        vector=[_vec("a", "S", 0.9), _vec("x", "T", 0.8)],
        fts=[dict(_vec("x", "S", 2.0), source="keyword")],
    ),
}


@pytest.mark.parametrize("order", sorted(_X_BODY_ORDERS))
@pytest.mark.parametrize("rerank", [True, False])
def test_node_with_another_stored_body_is_not_folded(rerank, order):
    # x owns two stored bodies S and T; a owns only S. The order of x's
    # bodies must not matter: the key is the union of both.
    legs = _X_BODY_ORDERS[order]
    folded = _run(_hq_with_legs(**legs), rerank)
    unfolded = _run(_hq_with_legs(**legs), rerank, fold=False)
    assert [r.to_dict() for r in folded] == [r.to_dict() for r in unfolded]
    assert all(r.folded_count == 0 for r in folded)


@pytest.mark.parametrize("rerank", [True, False])
def test_fold_key_survives_a_bm25_first_merge(rerank):
    # The flat merge keeps the first occurrence (BM25 here); the FTS body
    # must still reach the kept item's key.
    hq = _hq_with_legs(
        bm25=[_bm("a", "a props", 2.0), _bm("b", "b props", 1.0)],
        fts=[dict(_vec("a", "S", 3.0), source="keyword"), dict(_vec("b", "S", 2.0), source="keyword")],
    )
    out = _run(hq, rerank)
    assert len(out) == 1 and out[0].folded_count == 1


@pytest.mark.parametrize("rerank", [True, False])
def test_policy_filter_runs_before_the_fold(rerank):
    legs = dict(vector=[_vec("a", "S", 0.9), _vec("b", "S", 0.8), _vec("c", "S", 0.7)])
    hq = _hq_with_legs(**legs)
    hq._rebac = MagicMock()
    hq._rebac.check = lambda subject_id, permission, resource_id: SimpleNamespace(
        granted=resource_id != "a"
    )
    out = _run(hq, rerank, subject_id="u1")
    assert [r.node_id for r in out] == ["b"]
    assert [m["node_id"] for m in out[0].metadata["folded_duplicates"]["members"]] == ["c"]


def test_to_dict_top_level_keys_do_not_change():
    hq = _hq_with_legs(vector=[_vec("a", "S", 0.9), _vec("b", "S", 0.8)])
    out = _run(hq, True)
    assert set(out[0].to_dict()) == {
        "source", "node_id", "score", "text", "metadata", "graph_context", "raw_score",
    }


# ---------------------------------------------------------------------------
# Isolated stack: real SqliteVecStore + LocalSQLDocStore, many copies of one
# short body (the #482 heartbeat shape) next to a few real documents.
# ---------------------------------------------------------------------------


@pytest.fixture()
def stack(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    vec = build_vector_store("sqlite-vec", tmp_path)
    doc = LocalSQLDocStore(str(tmp_path / "doc.db"))
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    dup_ids = [f"evidence:turn:{i:03d}" for i in range(30)]
    real = {f"evidence:real:{i}": f"real conversation {i} about status reply topic{i}" for i in range(5)}
    texts = [DUP] * len(dup_ids) + list(real.values())
    ids = dup_ids + list(real)
    metas = [{"pack_id": PACK, "space": "evidence", "document_id": f"doc{i % 3}"} for i in range(len(ids))]
    vec.add_texts(texts, metas, ids)
    for sid, text, meta in zip(ids, texts, metas):
        doc.upsert_source(sid, text, dict(meta, node_id=sid))
    hq = HybridQuery(vec, graph)
    hq._doc_store = doc
    return hq, dup_ids, set(real)


@pytest.mark.parametrize("rerank", [True, False])
def test_isolated_stack_results_are_not_filled_by_one_body(stack, rerank):
    hq, dup_ids, real_ids = stack
    out = hq.query(DUP, pack_ids=[PACK], spaces=["evidence"], limit=10, use_rerank=rerank)
    texts = [r.text for r in out]
    assert texts.count(DUP) == 1, texts
    assert real_ids & {r.node_id for r in out}, "real documents must surface"
    rep = next(r for r in out if r.text == DUP)
    members = [m["node_id"] for m in rep.metadata["folded_duplicates"]["members"]]
    assert rep.folded_count == len(members) == len(dup_ids) - 1
    assert set(members) | {rep.node_id} == set(dup_ids)


# ---------------------------------------------------------------------------
# MCP ontology_query: members get canonical enrichment; a caller-written
# "folded_duplicates" value is not mistaken for a fold block.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("bind_test_principal")
def test_mcp_enriches_members_and_leaves_caller_metadata_alone():
    from opencrab.mcp import tools

    from .test_mcp_pack_aware import _real_sql_with_owned_pack, _stub_context

    caller_value = {"count": 7, "members": [{"node_id": "not-a-member"}]}
    legs = dict(vector=[
        _vec("a", "S", 0.9), _vec("b", "S", 0.8),
        _vec("u", "U", 0.7, folded_duplicates=caller_value),
    ])
    outcome = _run(_hq_with_legs(**legs), True)
    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=outcome)
    ctx = _stub_context(hybrid, sql=_real_sql_with_owned_pack(PACK))

    def fake_enrich(graph, results, pack_ids):
        for item in results:
            item["canonical"] = {"node_id": item["node_id"]}
        return results

    with patch.object(tools, "_get_context", return_value=ctx), \
         patch("opencrab.services.canonical_ids.enrich", fake_enrich):
        response = tools.ontology_query(question="question", pack_ids=[PACK])

    by_id = {r["node_id"]: r for r in response["results"]}
    member = by_id["a"]["metadata"]["folded_duplicates"]["members"][0]
    assert member["node_id"] == "b"
    assert member["canonical"] == {"node_id": "b"}
    assert by_id["u"]["metadata"]["folded_duplicates"] == {
        "count": 7, "members": [{"node_id": "not-a-member"}],
    }


# ---------------------------------------------------------------------------
# #482 follow-up: the rerank cap applies after the policy filter and the
# fold. So copies in a folded group cannot use up the capped slots. A group that the fold leaves unfolded (see the metadata exception
# in docs/ARCHITECTURE.md section 10) still takes one slot per copy.
# ---------------------------------------------------------------------------


def test_reranker_top_k_none_returns_every_merged_result_in_score_order():
    from opencrab.ontology.reranker import Reranker

    items = [{"source": "bm25", "node_id": f"n{i:03d}", "score": float(200 - i),
              "text": f"doc {i}", "metadata": {}} for i in range(200)]
    full = Reranker().rerank("doc", [items], top_k=None)
    assert {i["node_id"] for i in full} == {i["node_id"] for i in items}
    scores = [i["rerank_score"] for i in full]
    assert scores == sorted(scores, reverse=True)
    cut = Reranker().rerank("doc", [items], top_k=80)
    assert [i["node_id"] for i in cut] == [i["node_id"] for i in full[:80]]


@pytest.fixture()
def stack_factory(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    def build(copies: int, real: int) -> HybridQuery:
        root = tmp_path / f"s{copies}-{real}"
        root.mkdir()
        vec = build_vector_store("sqlite-vec", root)
        doc = LocalSQLDocStore(str(root / "doc.db"))
        graph = LocalGraphStore(str(root / "graph.db"))
        ids = [f"evidence:turn:{i:03d}" for i in range(copies)]
        ids += [f"evidence:real:{i:03d}" for i in range(real)]
        # The real documents share words with the copies, so the FTS leg
        # returns them after the copies.
        texts = [DUP] * copies + [f"heartbeat reply note {i} about topic{i}" for i in range(real)]
        metas = [{"pack_id": PACK, "space": "evidence"} for _ in ids]
        vec.add_texts(texts, metas, ids)
        for sid, text, meta in zip(ids, texts, metas):
            doc.upsert_source(sid, text, dict(meta, node_id=sid))
        hq = HybridQuery(vec, graph)
        hq._doc_store = doc
        return hq

    return build


@pytest.mark.parametrize(("copies", "real", "limit"), [(50, 10, 10), (90, 20, 20)])
def test_copies_beyond_the_rerank_cap_do_not_shrink_the_result(stack_factory, copies, real, limit):
    # rerank_limit is smaller than the copy count, so a cut before the fold
    # leaves only the copies and the fold reduces them to one result.
    hq = stack_factory(copies, real)
    assert query_mod._profile_for_query(DUP, limit, 1).rerank_limit < copies
    out = hq.query(DUP, pack_ids=[PACK], spaces=["evidence"], limit=limit, use_rerank=True)
    texts = [r.text for r in out]
    assert len(out) == limit, texts
    assert texts.count(DUP) == 1
    assert sum(1 for r in out if r.node_id.startswith("evidence:real:")) == limit - 1


def test_policy_filter_sees_candidates_beyond_the_rerank_cap():
    limit = 5
    cap = query_mod._profile_for_query("question", limit, 1).rerank_limit
    vector = [_vec(f"s{i:02d}", "S", 1.0 - i / 100) for i in range(cap + 5)]
    hq = _hq_with_legs(vector=vector)
    allowed = vector[-1]["node_id"]
    hq._rebac = MagicMock()
    hq._rebac.check = lambda subject_id, permission, resource_id: SimpleNamespace(
        granted=resource_id == allowed
    )
    out = hq.query("question", pack_ids=[PACK], use_rerank=True, limit=limit, subject_id="u1")
    assert [r.node_id for r in out] == [allowed]


def test_rerank_path_keeps_its_result_cap():
    limit = 100
    cap = query_mod._profile_for_query("question", limit, 1).rerank_limit
    assert cap < limit
    bm25 = [_bm(f"b{i:03d}", f"distinct {i}", float(200 - i)) for i in range(cap + 40)]
    out = _hq_with_legs(bm25=bm25).query("question", pack_ids=[PACK], use_rerank=True, limit=limit)
    assert len(out) == cap


def test_rerank_strip_below_limit_marks_unavailable():
    from tests import _issue516_flow as flow

    flow.assert_merge_cap_cell("strip-below")


def test_rerank_strip_at_limit_marks_unavailable():
    from tests import _issue516_flow as flow

    flow.assert_merge_cap_cell("strip-at")


def test_rerank_source_mix_below_limit_marks_unavailable():
    """The name follows the manifest. Under the lead ruling the mix cells are the control: the
    same body from two legs is no transformation, so nothing is cut and nothing is hidden."""
    from tests import _issue516_flow as flow

    flow.assert_merge_cap_cell("mix-below")


def test_rerank_source_mix_at_limit_marks_unavailable():
    from tests import _issue516_flow as flow

    flow.assert_merge_cap_cell("mix-at")
