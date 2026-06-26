from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from opencrab.ontology.bm25 import BM25Index, compute_fingerprint
from opencrab.ontology.query import HybridQuery


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until true or timeout (for background rebuild tests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


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


def _hybrid(doc_store) -> HybridQuery:
    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    hybrid = HybridQuery(chroma, neo4j)
    hybrid._doc_store = doc_store
    hybrid._bm25_debounce = 0.0  # no debounce delay in tests
    return hybrid


def test_t8_background_rebuild_on_fingerprint_change() -> None:
    """A diverged fingerprint schedules a background rebuild; the query serves
    the (stale) cache immediately and the worker swaps in the new index."""
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(side_effect=[
        [_node("a", pack_id="A")],                       # cold build → (1, "")
        [_node("a", pack_id="A"), _node("b", pack_id="A")],  # bg rebuild → (2, "")
    ])
    # Cheap probe reports the grown corpus, diverging from the cached (1, "").
    doc_store.bm25_fingerprint = MagicMock(return_value=(2, ""))

    hybrid = _hybrid(doc_store)
    try:
        # First search: cold synchronous build from the 1-node list.
        hybrid._bm25_search("alpha", spaces=None, limit=5)
        fp_first = hybrid._bm25_cache.fingerprint
        assert fp_first == (1, "")

        # Second search: probe (2,"") != cached (1,"") → schedule bg rebuild,
        # return the stale cache without blocking.
        hybrid._bm25_search("alpha", spaces=None, limit=5)

        # Background worker rebuilds from the 2-node list and atomically swaps.
        assert _wait_until(lambda: hybrid._bm25_cache.fingerprint == (2, ""))
        assert hybrid._bm25_cache_size == 2
    finally:
        hybrid.shutdown_bm25()


def test_t8_invalidate_marks_dirty() -> None:
    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    hybrid = HybridQuery(chroma, neo4j)
    # No doc store attached → inert: invalidate marks dirty but spawns no thread.
    hybrid._bm25_dirty = False
    hybrid.invalidate_bm25_cache()
    assert hybrid._bm25_dirty is True
    assert hybrid._bm25_worker is None


def test_t8_bm25_fingerprint_matches_compute_fingerprint(tmp_path) -> None:
    """The cheap SQL fingerprint must equal compute_fingerprint(list_nodes)."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    ds.upsert_node_doc("claim", "Claim", "a", {"name": "alpha"})
    ds.upsert_node_doc("claim", "Claim", "b", {"name": "beta"})

    # Full set and a smaller cap (mirrors BM25's _BM25_NODE_LIMIT slicing).
    for lim in (50000, 1):
        assert ds.bm25_fingerprint(limit=lim) == compute_fingerprint(
            ds.list_nodes(limit=lim)
        )


def test_t8_coalesces_burst_invalidations() -> None:
    """A burst of invalidations collapses into a couple of rebuild passes, not
    one per invalidation (measured via list_nodes calls on the worker)."""
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))

    hybrid = _hybrid(doc_store)
    hybrid._bm25_debounce = 0.05  # small window so the burst coalesces
    try:
        for _ in range(10):
            hybrid.invalidate_bm25_cache()
        # Worker wakes and reads the corpus at least once.
        assert _wait_until(lambda: doc_store.list_nodes.call_count >= 1, timeout=2.0)
        time.sleep(0.2)  # let any re-scheduled pass settle
        # Coalesced: a handful of scans, not 10.
        assert doc_store.list_nodes.call_count <= 3
    finally:
        hybrid.shutdown_bm25()
