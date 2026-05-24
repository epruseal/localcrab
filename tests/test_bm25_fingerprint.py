from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencrab.ontology.bm25 import BM25Index, compute_fingerprint
from opencrab.ontology.query import HybridQuery


def _node(node_id: str, *, pack_id: str | None = None, updated_at: str | None = None,
          space: str = "claim", text: str = "alpha beta") -> dict:
    props: dict = {"name": text}
    if pack_id is not None:
        props["pack_id"] = pack_id
    doc: dict = {
        "node_id": node_id,
        "space": space,
        "node_type": "Claim",
        "properties": props,
    }
    if updated_at:
        doc["updated_at"] = updated_at
    return doc


# ---------------------------------------------------------------------------
# T8 — fingerprint detection
# ---------------------------------------------------------------------------


def test_t8_fingerprint_changes_with_count() -> None:
    fp1 = compute_fingerprint([_node("a")])
    fp2 = compute_fingerprint([_node("a"), _node("b")])
    assert fp1 != fp2


def test_t8_fingerprint_changes_with_timestamp() -> None:
    fp1 = compute_fingerprint([_node("a", updated_at="2026-01-01T00:00:00")])
    fp2 = compute_fingerprint([_node("a", updated_at="2026-01-02T00:00:00")])
    assert fp1 != fp2


def test_t8_bm25_search_filters_by_pack_id() -> None:
    index = BM25Index.build([
        _node("a", pack_id="A", text="alpha"),
        _node("b", pack_id="B", text="alpha"),
    ])
    hits = index.search("alpha", pack_ids=["A"], limit=5)
    assert [h["node_id"] for h in hits] == ["a"]


def test_t8_bm25_include_unpackaged_passes_legacy() -> None:
    index = BM25Index.build([
        _node("a", pack_id="A", text="alpha"),
        _node("legacy", pack_id=None, text="alpha"),
    ])
    hits = index.search("alpha", pack_ids=["A"], include_unpackaged=True, limit=5)
    ids = {h["node_id"] for h in hits}
    assert ids == {"a", "legacy"}


def test_t8_lazy_rebuild_when_fingerprint_changes() -> None:
    """HybridQuery._bm25_search must rebuild when doc store fingerprint diverges."""
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(side_effect=[
        [_node("a", pack_id="A")],
        [_node("a", pack_id="A"), _node("b", pack_id="A")],
    ])

    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    hybrid = HybridQuery(chroma, neo4j)
    hybrid._doc_store = doc_store

    # First search builds the index from the first list (1 node).
    hits1 = hybrid._bm25_search("alpha", spaces=None, limit=5)
    assert hybrid._bm25_cache is not None
    fp_first = hybrid._bm25_cache.fingerprint

    # Second search: fingerprint check sees the doc store grew → rebuild.
    hits2 = hybrid._bm25_search("alpha", spaces=None, limit=5)
    fp_second = hybrid._bm25_cache.fingerprint
    assert fp_first != fp_second
    # list_nodes called twice for fingerprint comparison + rebuild
    assert doc_store.list_nodes.call_count >= 2


def test_t8_invalidate_marks_dirty() -> None:
    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    hybrid = HybridQuery(chroma, neo4j)
    hybrid._bm25_dirty = False
    hybrid.invalidate_bm25_cache()
    assert hybrid._bm25_dirty is True
