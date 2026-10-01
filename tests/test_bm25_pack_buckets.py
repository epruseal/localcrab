"""Issue #411: BM25Index scopes a search to the requested packs' documents.

The equivalence tests compare ``BM25Index.search`` against the pre-#411
algorithm (a full scan with ``in_pack_scope`` per document), kept here as an
oracle. The visit test pins the performance property: a search over one pack
must not walk every indexed document.
"""

from __future__ import annotations

import itertools
import random
from collections import Counter
from typing import Any

from opencrab.ontology.bm25 import _B, _K1, BM25Index, _node_text
from opencrab.ontology.pack_provenance import in_pack_scope, scope_pack_id
from opencrab.ontology.text_cues import tokenize

_WORDS = ["alpha", "beta", "gamma", "delta", "common"]


def _oracle_search(
    index: BM25Index,
    query: str,
    spaces: list[str] | None,
    limit: int,
    pack_ids: list[str],
) -> list[dict[str, Any]]:
    """The pre-#411 search loop: scan every document, filter per document."""
    q_tokens = tokenize(query)
    if not q_tokens or not index._docs:
        return []
    pack_set = set(pack_ids or ())
    scores: list[tuple[int, float]] = []
    for i, (doc, toks) in enumerate(zip(index._docs, index._tokens)):
        if spaces and doc.get("space") not in spaces:
            continue
        if not in_pack_scope(doc, pack_set):
            continue
        dl = len(toks)
        tf_map = Counter(toks)
        score = 0.0
        for term in q_tokens:
            if term not in index._idf:
                continue
            tf = tf_map.get(term, 0)
            num = tf * (_K1 + 1)
            den = tf + _K1 * (1 - _B + _B * dl / max(index._avgdl, 1))
            score += index._idf[term] * (num / den)
        if score > 0:
            scores.append((i, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    return [
        {
            "node_id": index._docs[i].get("node_id"),
            "space": index._docs[i].get("space"),
            "node_type": index._docs[i].get("node_type"),
            "score": round(s, 4),
            "properties": index._docs[i].get("properties") or {},
            "text": _node_text(index._docs[i]),
            "pack_id": scope_pack_id(index._docs[i]),
        }
        for i, s in scores[:limit]
    ]


def _node(node_id: str, text: str, *, space: str = "claim", **where: Any) -> dict[str, Any]:
    """``where`` may set properties_pack, metadata_pack or top_pack."""
    props: dict[str, Any] = {"name": text}
    node: dict[str, Any] = {
        "node_id": node_id,
        "space": space,
        "node_type": "Claim",
        "properties": props,
    }
    if "properties_pack" in where:
        props["pack_id"] = where["properties_pack"]
    if "metadata_pack" in where:
        node["metadata"] = {"pack_id": where["metadata_pack"]}
    if "top_pack" in where:
        node["pack_id"] = where["top_pack"]
    return node


def _mixed_nodes() -> list[dict[str, Any]]:
    """Documents covering every pack_id shape scope_pack_id distinguishes."""
    rng = random.Random(411)
    shapes: list[dict[str, Any]] = [
        {"properties_pack": "A"},
        {"properties_pack": "B"},
        {"properties_pack": "C"},
        {"properties_pack": 1},
        {"properties_pack": True},
        {"properties_pack": ""},
        {"properties_pack": 0},
        {"properties_pack": None},
        {"metadata_pack": "A", "properties_pack": "B"},
        {"metadata_pack": "", "properties_pack": "B"},
        {"top_pack": "C"},
        {},
    ]
    nodes = []
    for i in range(120):
        text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4)))
        space = rng.choice(["claim", "evidence"])
        nodes.append(_node(f"n{i}", text, space=space, **shapes[i % len(shapes)]))
    return nodes


def test_equivalence_with_full_scan_oracle() -> None:
    index = BM25Index.build(_mixed_nodes())
    scopes = [
        ["A"],
        ["B"],
        ["A", "B"],
        ["B", "A"],
        ["A", "A", "B"],
        ["C", "1", "True"],
        ["1"],
        ["True"],
        ["0"],
        ["missing"],
        ["A", "missing"],
        [],
    ]
    queries = ["alpha", "alpha beta", "common gamma delta", "zzz"]
    for scope, query, spaces, limit in itertools.product(
        scopes, queries, [None, ["claim"], ["evidence"], ["claim", "evidence"]], [1, 3, 7, 50]
    ):
        got = index.search(query, spaces=spaces, limit=limit, pack_ids=scope)
        want = _oracle_search(index, query, spaces, limit, scope)
        assert got == want, (scope, query, spaces, limit)


def test_ties_across_packs_keep_document_order_at_limit_boundary() -> None:
    nodes = [_node(f"t{i}", "alpha", properties_pack="A" if i % 2 == 0 else "B") for i in range(10)]
    index = BM25Index.build(nodes)
    got = index.search("alpha", limit=3, pack_ids=["B", "A"])
    assert [h["node_id"] for h in got] == ["t0", "t1", "t2"]
    assert got == _oracle_search(index, "alpha", None, 3, ["B", "A"])


def test_document_moved_out_of_scope_after_build_is_not_returned() -> None:
    nodes = [_node("x", "alpha", properties_pack="A"), _node("y", "alpha", properties_pack="A")]
    index = BM25Index.build(nodes)
    nodes[0]["properties"]["pack_id"] = "B"
    got = index.search("alpha", limit=5, pack_ids=["A"])
    assert [h["node_id"] for h in got] == ["y"]


def test_document_moved_into_scope_after_build_is_not_found_until_rebuild() -> None:
    # Pack membership is part of the build-time snapshot, like the token lists.
    nodes = [_node("x", "alpha", properties_pack="A"), _node("y", "alpha", properties_pack="B")]
    index = BM25Index.build(nodes)
    nodes[1]["properties"]["pack_id"] = "A"
    assert [h["node_id"] for h in index.search("alpha", limit=5, pack_ids=["A"])] == ["x"]
    rebuilt = BM25Index.build(nodes)
    assert [h["node_id"] for h in rebuilt.search("alpha", limit=5, pack_ids=["A"])] == ["x", "y"]


class _CountingList(list):
    """list that counts element reads, through iteration and indexing alike."""

    def __init__(self, items: list[Any]) -> None:
        super().__init__(items)
        self.reads = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        for item in super().__iter__():
            self.reads += 1
            yield item

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        self.reads += 1
        return super().__getitem__(key)


def test_single_pack_search_visits_only_that_packs_documents() -> None:
    total, small_pack_size = 300, 5
    nodes = [
        _node(
            f"n{i}",
            "alpha common",
            properties_pack="small" if i < small_pack_size else f"big{i % 7}",
        )
        for i in range(total)
    ]
    index = BM25Index.build(nodes)
    index._docs = _CountingList(index._docs)
    index._tokens = _CountingList(index._tokens)

    limit = 2
    hits = index.search("alpha", limit=limit, pack_ids=["small"])

    assert len(hits) == limit
    assert index._tokens.reads <= small_pack_size
    assert index._docs.reads <= small_pack_size + limit
