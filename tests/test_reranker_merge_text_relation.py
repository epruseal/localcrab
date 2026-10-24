"""#516 G5: how ``_merge_duplicate`` treats a changed representative text.

A merge keeps the private proof only while the displayed text is the proven
text or its exact first 4000 characters. Any other change is a transformation.
The unit cases call the module function ``opencrab.ontology.reranker._merge_duplicate``.
The composed case builds a real proof, merges, caps and reads the ranges back.
"""

from __future__ import annotations

from typing import Any

import pytest

from opencrab.ontology import query as query_mod
from opencrab.ontology.reranker import _merge_duplicate
from opencrab.services import result_caps as caps
from tests import _issue516_flow as flow
from tests import test_query_result_caps as _caps

# Reuse the real-store fixture of the query cap tests. Assigning it keeps the
# fixture visible to pytest without an import that the test arguments shadow.
stack = _caps.stack

pytestmark = pytest.mark.usefixtures("bind_test_principal")

LIMIT = 4000
P = {"source_id": "s1", "pack_id": "p1", "source_digest": "d1"}
Q = {"source_id": "s2", "pack_id": "p1", "source_digest": "d2"}
X = "alpha beta " + ("한글본문 " * 1200)  # more than 4000 characters
T = "t" * 300


def _hit(source: str, text: str, proof: dict[str, str] | None = None) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "source": source, "node_id": "n1", "score": 1.0, "text": text, "metadata": {},
    }
    if proof is not None:
        hit["_query_provenance"] = dict(proof)
    return hit


# name: (existing, [items], expected text, transformed, proof, invalidated, source chars)
CASES = {
    "same-proof-same-text": (
        _hit("vector", T, P), [_hit("keyword", T, P)], T, False, P, False, None,
    ),
    "different-proofs": (
        _hit("vector", T, P), [_hit("keyword", T, Q)], T, False, None, True, None,
    ),
    "large-old-contains-new": (
        _hit("vector", X, P), [_hit("bm25", X[100:200])], X, False, P, False, None,
    ),
    "large-old-graph-appended": (
        _hit("vector", X, P), [_hit("bm25", "n1 TextUnit " + X)], X[:LIMIT], False, P, False, len(X),
    ),
    "prefix-then-same": (
        _hit("vector", X, P),
        [_hit("bm25", "n1 TextUnit " + X), _hit("keyword", X, P)],
        X[:LIMIT], False, P, False, len(X),
    ),
    "short-old-concat": (
        _hit("vector", "o" * 3000, P), [_hit("bm25", "n" * 500)],
        ("o" * 3000 + "\n" + "n" * 500).strip()[:LIMIT], True, None, True, None,
    ),
    "codex-trailing-space": (
        _hit("vector", "abc ", P), [_hit("bm25", "\t")], "abc", True, None, True, None,
    ),
    "leading-space-large-old": (
        _hit("vector", " " + X, P), [_hit("bm25", "n1 TextUnit " + X)],
        (" " + X + "\n" + "n1 TextUnit " + X).strip()[:LIMIT], True, None, True, None,
    ),
    "large-old-without-proof": (
        _hit("vector", X), [_hit("bm25", "n1 TextUnit " + X)], X[:LIMIT], True, None, True, None,
    ),
    "large-old-conflicting-proof": (
        _hit("vector", X, P), [_hit("keyword", "n1 TextUnit " + X, Q)],
        X[:LIMIT], True, None, True, None,
    ),
    "exactly-4000-old": (
        _hit("vector", "a" * LIMIT, P), [_hit("bm25", "graph text")],
        "a" * LIMIT, False, P, False, None,
    ),
    "old-empty": (
        _hit("vector", ""), [_hit("keyword", "  " + "t" * 5000, P)],
        ("  " + "t" * 5000).strip()[:LIMIT], True, None, True, None,
    ),
    "trailing-space-prefix": (
        _hit("vector", "a" * LIMIT + " ", P), [_hit("bm25", "\t")],
        "a" * LIMIT, False, P, False, LIMIT + 1,
    ),
    "trailing-space-no-proof": (
        _hit("vector", "a" * LIMIT + " "), [_hit("bm25", "\t")],
        "a" * LIMIT, True, None, True, None,
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_merge_text_relation(case):
    existing, items, text, transformed, proof, invalidated, chars = CASES[case]
    existing = dict(existing)
    existing["sources"] = [existing["source"]]
    for item in items:
        _merge_duplicate(existing, dict(item))
    assert existing["text"] == text
    assert bool(existing.get("_query_text_transformed")) is transformed
    assert existing.get("_query_provenance") == proof
    assert bool(existing.get("_query_provenance_invalidated")) is invalidated
    assert existing.get("_query_text_source_chars") == chars


def test_trailing_space_prefix_keeps_a_locator_and_reassembles(stack):
    source_id = "src:trail"
    text = "a" * LIMIT + " "
    stack["docs"].upsert_source(
        source_id, text, {"pack_id": _caps.PACK, "space": "evidence"}
    )
    proof = query_mod._source_provenance(
        stack["docs"], source_id, text, permitted_packs={_caps.PACK}
    )
    assert proof is not None  # the real validator accepted the stored row
    existing = {
        "source": "vector", "sources": ["vector"], "node_id": source_id, "score": 1.0,
        "text": text, "metadata": {"pack_id": _caps.PACK}, "_query_provenance": proof,
    }
    _merge_duplicate(existing, _hit("keyword", "\t") | {"node_id": source_id})
    assert existing["text"] == "a" * LIMIT
    assert "_query_text_transformed" not in existing
    assert existing["_query_provenance"] == proof
    assert existing["_query_text_source_chars"] == LIMIT + 1

    item = caps.cap_query_item(existing)
    assert item["text"] == text[:LIMIT]
    assert item["truncation"]["fields"][0] == {"path": ["text"], "original_chars": LIMIT + 1}
    locator = item["truncation"]["source_locator"]
    assert locator["source_id"] == source_id
    assert _caps._read_source(stack, locator) == text
    assert flow.find_reserved(item) == []
