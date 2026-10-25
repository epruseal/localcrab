"""#550: BM25+ lower bound on the term-frequency normalisation.

Design: docs/issue-550-bm25-length.md (section 7 lists these tests).
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections import Counter

import pytest

from opencrab.ontology.bm25 import _B, _DELTA, _K1, BM25Index, bm25_idf, bm25_score
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

FTS5_DELETE_OK = sqlite3.sqlite_version_info >= (3, 43, 0)


def _node(nid: str, text: str, pack: str = "A") -> dict:
    return {"node_id": nid, "space": "evidence", "node_type": "Note",
            "properties": {"text": text, "pack_id": pack}}


def _filler(rng: random.Random, n: int) -> str:
    return " ".join(f"w{rng.randrange(400)}" for _ in range(n))


def _pressure_corpus() -> list[dict]:
    """A long document with a rare and a common term, and short decoys.

    The decoys repeat the common term five times. The shape follows the
    #542 harness query ``top-k-pressure-1``.
    """
    rng = random.Random(550)
    nodes = [_node("gold", "rarex commonx " + _filler(rng, 300))]
    nodes += [_node(f"decoy{i:02d}", " ".join(["commonx"] * 5) + " " + _filler(rng, 30))
              for i in range(20)]
    nodes += [_node(f"fill{i:03d}", _filler(rng, 80)) for i in range(300)]
    return nodes


def test_long_document_with_the_rare_term_is_not_pushed_out_by_short_decoys() -> None:
    hits = BM25Index.build(_pressure_corpus()).search("rarex commonx", limit=10, pack_ids=["A"])
    assert [h["node_id"] for h in hits].index("gold") < 10


@pytest.mark.skipif(not FTS5_DELETE_OK, reason="FTS5 contentless_delete needs SQLite 3.43")
def test_db_index_also_keeps_the_long_document_in_the_top_limit(tmp_path) -> None:
    store = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert store.supports_node_bm25
    for node in _pressure_corpus():
        store.upsert_node_doc(node["space"], node["node_type"], node["node_id"], node["properties"])
    result = store.node_bm25_search("rarex commonx", pack_ids=["A"], spaces=None, limit=10,
                                    per_pack=False)
    assert result is not None
    hits, warnings = result
    assert warnings == []
    ids = [h["node_id"] for h in hits]
    assert "gold" in ids
    memory = BM25Index.build(store.list_nodes(limit=10**9)).search(
        "rarex commonx", limit=10, pack_ids=["A"])
    assert [(h["node_id"], h["score"]) for h in hits] == [
        (h["node_id"], h["score"]) for h in memory]


def test_every_matched_term_contributes_at_least_idf_times_delta() -> None:
    idf = {"a": 2.5, "b": 0.75}
    # A very long document still gets idf * delta per matched query term.
    assert bm25_score(["a", "b"], {"a": 1, "b": 3}, idf, 10**9, 10.0) >= (2.5 + 0.75) * _DELTA
    # Terms the document lacks add nothing, and a document with no query term scores 0.
    assert bm25_score(["a", "b"], {"b": 1}, idf, 10, 10.0) < 2.5 * _DELTA
    assert bm25_score(["a", "b"], {"c": 4}, idf, 10, 10.0) == 0.0
    # Terms outside the idf map are skipped, as before.
    assert bm25_score(["zz"], {"zz": 3}, idf, 10, 10.0) == 0.0


def _old_score(q_tokens, tf_map, idf, dl, avgdl) -> float:
    """The pre-#550 BM25 term sum, without the lower bound."""
    score = 0.0
    for term in q_tokens:
        if term not in idf:
            continue
        tf = tf_map.get(term, 0)
        score += idf[term] * (tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * dl / max(avgdl, 1))))
    return score


def _order_changes_only_at_ties(q_tokens, docs, idf, avgdl) -> list[tuple[str, str]]:
    """Return the pairs whose relative order differs between the two formulas.

    Assert that each such pair has an exactly equal old or new score
    (design 3.3).
    """
    old = {k: _old_score(q_tokens, tf, idf, dl, avgdl) for k, (tf, dl) in docs.items()}
    new = {k: bm25_score(q_tokens, tf, idf, dl, avgdl) for k, (tf, dl) in docs.items()}
    assert {k for k, s in old.items() if s > 0} == {k for k, s in new.items() if s > 0}
    changed = []
    keys = [k for k, s in old.items() if s > 0]
    for i, x in enumerate(keys):
        for y in keys[i + 1:]:
            if (old[x] > old[y]) != (new[x] > new[y]) or (old[x] < old[y]) != (new[x] < new[y]):
                assert old[x] == old[y] or new[x] == new[y], (x, y, old[x], old[y], new[x], new[y])
                changed.append((x, y))
    return changed


@pytest.mark.parametrize("seed", range(6))
def test_single_term_queries_keep_their_order(seed) -> None:
    rng = random.Random(seed)
    docs = {f"d{i}": (Counter({"t": rng.randint(1, 9)}), rng.randint(1, 400)) for i in range(200)}
    docs.update({f"z{i}": (Counter({"u": 1}), rng.randint(1, 50)) for i in range(20)})
    idf = {"t": bm25_idf(220, 200), "u": bm25_idf(220, 20)}
    avgdl = sum(dl for _, dl in docs.values()) / len(docs)
    for repeat in (1, 2, 7):
        _order_changes_only_at_ties(["t"] * repeat, docs, idf, avgdl)


@pytest.mark.parametrize("repeat", [1, 7])
def test_single_term_order_can_change_only_where_scores_tie(repeat) -> None:
    """The boundary the design verifier found (design 3.3).

    With these inputs the new scores of the two documents are equal, so the
    tie order key decides and may differ from the old order.
    """
    idf = {"t": bm25_idf(100, 2)}
    docs = {"n0": (Counter({"t": 1}), 95), "n1": (Counter({"t": 2}), 193)}
    old = [_old_score(["t"] * repeat, tf, idf, dl, 9.0) for tf, dl in docs.values()]
    new = [bm25_score(["t"] * repeat, tf, idf, dl, 9.0) for tf, dl in docs.values()]
    assert old[0] < old[1]
    assert math.isclose(old[0], old[1], rel_tol=1e-14)
    assert new[0] == new[1]
    assert _order_changes_only_at_ties(["t"] * repeat, docs, idf, 9.0) == [("n0", "n1")]
