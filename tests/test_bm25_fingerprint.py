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
    the (stale) cache immediately and the worker swaps in the new index.

    The probe reflects the store's real state at each point in time (the
    index's own recorded fingerprint is stamped FROM this same probe, not
    from compute_fingerprint(indexed_nodes) — see #63): first the 1-node
    state, then the grown 2-node state, then the rebuild loop re-probes and
    finds the same 2-node state again (nothing changed since the mismatch
    that woke it), matching what it just built.
    """
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(side_effect=[
        [_node("a", pack_id="A")],                       # cold build (1 node)
        [_node("a", pack_id="A"), _node("b", pack_id="A")],  # bg rebuild (2 nodes)
    ])
    doc_store.bm25_fingerprint = MagicMock(side_effect=[
        (1, ""),  # stamped onto the cold-built index
        (2, ""),  # hot-path probe on the 2nd search: diverges from (1, "") → invalidate
        (2, ""),  # rebuild loop's own probe: matches what it's about to build
    ])

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
    """The cheap SQL fingerprint always reflects the WHOLE table (#63), so it
    matches compute_fingerprint(list_nodes(<uncapped>)) regardless of the
    `limit` kwarg passed to bm25_fingerprint — that kwarg is kept only for
    call-site compatibility with BM25's _BM25_NODE_LIMIT and is not applied."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    ds.upsert_node_doc("claim", "Claim", "a", {"name": "alpha"})
    ds.upsert_node_doc("claim", "Claim", "b", {"name": "beta"})

    whole_table = compute_fingerprint(ds.list_nodes(limit=50000))
    for lim in (50000, 1):
        assert ds.bm25_fingerprint(limit=lim) == whole_table


def _seed_ordered(ds, count: int, space: str = "s1") -> None:
    """Seed ``count`` nodes with strictly increasing, controlled
    ``updated_at`` so cap selection (ORDER BY updated_at DESC) is
    predictable rather than depending on ``datetime.now()`` call spacing."""
    for i in range(count):
        ds.upsert_node_doc(space, "T", f"n{i}", {"name": "alpha"})
        ds._conn.execute(
            "UPDATE doc_nodes SET updated_at=? WHERE node_id=?",
            (f"2026-01-01T00:00:{i:02d}", f"n{i}"),
        )
    ds._conn.commit()


def test_t8_fingerprint_fetched_before_nodes(tmp_path, monkeypatch) -> None:
    """#63 follow-up (codex High): pins the CALL ORDER — the fingerprint
    probe must run before ``list_nodes``, not after.

    Why the order matters: if ``list_nodes`` ran first and a write landed
    right after it (before the fingerprint probe), the fingerprint would be
    stamped from the POST-write store state while the indexed nodes are the
    STALE pre-write snapshot. The next probe would then agree with that
    already-current stamp and never reschedule a rebuild — the write would
    be lost forever. Fingerprint-first flips the risk: any write in the gap
    makes the stamped fingerprint OLDER than what got indexed, so the next
    probe disagrees and schedules one extra (harmless) rebuild instead.

    Caveat: this asserts the order of the two calls (via a write injected as
    a ``list_nodes`` side effect, which by construction still lands before
    the real snapshot). It does NOT reproduce genuine concurrent
    interleaving between two threads/processes — that would need a hook
    inside the store itself, which is more machinery than this regression
    guard is worth. Reversing the order below makes this test fail, which is
    the property it actually protects.
    """
    from opencrab.ontology import query as query_module
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    for i in range(5):
        ds.upsert_node_doc("s1", "T", f"n{i}", {"name": "alpha"})

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 100)  # no cap pressure here

    real_list_nodes = ds.list_nodes
    injected = {"done": False}

    def racy_list_nodes(*args, **kwargs):
        # A write lands here: after the fingerprint probe already ran
        # (call #1 in the fixed order), before list_nodes (call #2) returns.
        if not injected["done"]:
            injected["done"] = True
            ds.upsert_node_doc("s1", "T", "race", {"name": "alpha"})
        return real_list_nodes(*args, **kwargs)

    ds.list_nodes = racy_list_nodes

    hybrid = _hybrid(ds)
    try:
        hybrid._bm25_search("alpha", spaces=None, limit=25)  # cold build

        indexed_ids = {h["node_id"] for h in hybrid._bm25_cache.search("alpha", limit=25)}
        assert "race" in indexed_ids, "the race write must already be in the index"

        live_fp = ds.bm25_fingerprint()
        assert hybrid._bm25_cache.fingerprint != live_fp, (
            "the stamped fingerprint must be the OLDER pre-write value, "
            "not the post-write value — that's what schedules the "
            "self-correcting follow-up rebuild instead of hiding the write"
        )

        # Next query: probe diverges from the stale stamp → one more
        # (harmless) rebuild → fingerprint converges.
        hybrid._bm25_search("alpha", spaces=None, limit=25)
        assert _wait_until(lambda: hybrid._bm25_cache.fingerprint == ds.bm25_fingerprint())
    finally:
        hybrid.shutdown_bm25()


def test_t8_no_rebuild_scheduled_when_over_cap_and_unchanged(tmp_path, monkeypatch) -> None:
    """Regression (#63 follow-up): the lead reproduced build-time capped
    fingerprint (10, ts) vs whole-table probe (20, ts) never comparing equal,
    which scheduled a background rebuild on every single query forever, even
    with nothing changed. Fixed by stamping the index's own fingerprint from
    the same whole-table probe at build time (BM25Index.build(fingerprint=)),
    so once nothing changes, probe and cache.fingerprint agree again."""
    from opencrab.ontology import query as query_module
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    _seed_ordered(ds, 20)

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 10)  # cap < corpus (20)

    hybrid = _hybrid(ds)
    list_nodes_spy = MagicMock(wraps=ds.list_nodes)
    ds.list_nodes = list_nodes_spy
    try:
        hybrid._bm25_search("alpha", spaces=None, limit=5)  # cold build
        calls_after_cold = list_nodes_spy.call_count

        for _ in range(5):
            hybrid._bm25_search("alpha", spaces=None, limit=5)
        time.sleep(0.2)  # let any (incorrectly) scheduled rebuild run

        assert list_nodes_spy.call_count == calls_after_cold, (
            "nothing changed; a background rebuild must not be scheduled "
            "just because the corpus exceeds the BM25 cap"
        )
    finally:
        hybrid.shutdown_bm25()


def test_t8_rebuild_scheduled_when_row_outside_cap_updated(tmp_path, monkeypatch) -> None:
    """Complement to the above: a real change outside the cap window must
    still be picked up (this is the original #63 bug, kept as a regression
    test at the HybridQuery integration level, not just the store level)."""
    from opencrab.ontology import query as query_module
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    _seed_ordered(ds, 20)

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 10)

    hybrid = _hybrid(ds)
    try:
        hybrid._bm25_search("alpha", spaces=None, limit=25)  # cold build: top 10 (n10..n19)
        indexed_ids = {h["node_id"] for h in hybrid._bm25_cache.search("alpha", limit=25)}
        assert "n5" not in indexed_ids

        ds._conn.execute(
            "UPDATE doc_nodes SET updated_at=? WHERE node_id=?",
            ("2026-01-02T00:00:00", "n5"),
        )
        ds._conn.commit()

        hybrid._bm25_search("alpha", spaces=None, limit=25)  # probe now diverges

        def _n5_indexed() -> bool:
            hits = hybrid._bm25_cache.search("alpha", limit=25)
            return "n5" in {h["node_id"] for h in hits}

        assert _wait_until(_n5_indexed)
    finally:
        hybrid.shutdown_bm25()


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
