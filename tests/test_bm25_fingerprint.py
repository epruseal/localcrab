from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock

import pytest

from opencrab.ontology.bm25 import BM25Index, compute_fingerprint
from opencrab.ontology.query import Bm25CacheState, HybridQuery


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
    """issue #147 §3.6: BM25Index.search's pack filter is now unconditional
    (``if not in_pack_scope(doc, pack_set): continue``, no more
    ``if pack_ids and ...`` guard) and ``include_unpackaged`` is accepted
    only for call-site signature compatibility -- it no longer has any
    effect. A row with no detectable pack_id is outside every read scope
    (#143 invariant 5) and is excluded even when the caller passes
    ``include_unpackaged=True``."""
    index = BM25Index.build([
        _node("a", pack_id="A", text="alpha"),
        _node("legacy", pack_id=None, text="alpha"),
    ])
    hits = index.search("alpha", pack_ids=["A"], include_unpackaged=True, limit=5)
    ids = {h["node_id"] for h in hits}
    assert ids == {"a"}


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
        hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["A"])
        fp_first = hybrid._bm25_cache.fingerprint
        assert fp_first == (1, "")

        # Second search: probe (2,"") != cached (1,"") → schedule bg rebuild,
        # return the stale cache without blocking.
        hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["A"])

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
        ds.upsert_node_doc(space, "T", f"n{i}", {"name": "alpha", "pack_id": "p1"})
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
        ds.upsert_node_doc("s1", "T", f"n{i}", {"name": "alpha", "pack_id": "p1"})

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 100)  # no cap pressure here

    real_list_nodes = ds.list_nodes
    injected = {"done": False}

    def racy_list_nodes(*args, **kwargs):
        # A write lands here: after the fingerprint probe already ran
        # (call #1 in the fixed order), before list_nodes (call #2) returns.
        if not injected["done"]:
            injected["done"] = True
            ds.upsert_node_doc("s1", "T", "race", {"name": "alpha", "pack_id": "p1"})
        return real_list_nodes(*args, **kwargs)

    ds.list_nodes = racy_list_nodes

    hybrid = _hybrid(ds)
    try:
        hybrid._bm25_search("alpha", spaces=None, limit=25, pack_ids=["p1"])  # cold build

        indexed_ids = {
            h["node_id"]
            for h in hybrid._bm25_cache.search("alpha", limit=25, pack_ids=["p1"])
        }
        assert "race" in indexed_ids, "the race write must already be in the index"

        live_fp = ds.bm25_fingerprint()
        assert hybrid._bm25_cache.fingerprint != live_fp, (
            "the stamped fingerprint must be the OLDER pre-write value, "
            "not the post-write value — that's what schedules the "
            "self-correcting follow-up rebuild instead of hiding the write"
        )

        # Next query: probe diverges from the stale stamp → one more
        # (harmless) rebuild → fingerprint converges.
        hybrid._bm25_search("alpha", spaces=None, limit=25, pack_ids=["p1"])
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
        hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["p1"])  # cold build
        calls_after_cold = list_nodes_spy.call_count

        for _ in range(5):
            hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["p1"])
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
        # cold build: top 10 (n10..n19)
        hybrid._bm25_search("alpha", spaces=None, limit=25, pack_ids=["p1"])
        indexed_ids = {
            h["node_id"]
            for h in hybrid._bm25_cache.search("alpha", limit=25, pack_ids=["p1"])
        }
        assert "n5" not in indexed_ids

        ds._conn.execute(
            "UPDATE doc_nodes SET updated_at=? WHERE node_id=?",
            ("2026-01-02T00:00:00", "n5"),
        )
        ds._conn.commit()

        # probe now diverges
        hybrid._bm25_search("alpha", spaces=None, limit=25, pack_ids=["p1"])

        def _n5_indexed() -> bool:
            hits = hybrid._bm25_cache.search("alpha", limit=25, pack_ids=["p1"])
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


# ---------------------------------------------------------------------------
# #398 (BM25 global index cap excludes whole packs) -- small-scale repro.
#
# This is a diagnostic fixture, not a fix: #397's auto_pack candidate-pool
# correction (SQL packs as the candidate source) does not close #398, since
# a pack can be correctly scoped and still score zero BM25 hits if its docs
# were already pushed out of the GLOBAL cap window before pack_ids filtering
# ever runs (list_nodes() orders by updated_at DESC across ALL packs, with
# no per-pack floor). #397 measured this live as 0/2798 hits for an
# "acupoint-medical" pack query. The two tests below reproduce the same
# shape at a 20-row scale, borrowing the cap-injection pattern from
# test_t8_no_rebuild_scheduled_when_over_cap_and_unchanged above.
# ---------------------------------------------------------------------------


def _seed_two_packs_ordered(ds, *, old_count: int, new_count: int) -> None:
    """``old_count`` nodes in ``old-pack`` (oldest updated_at, pushed out of
    a small cap first) followed by ``new_count`` nodes in ``new-pack``
    (newest updated_at, always inside the cap). Old-pack text is "acupoint
    meridian" and new-pack text is "widget catalog" so a query can target
    one pack's content without the other's matching by accident."""
    for i in range(old_count):
        ds.upsert_node_doc(
            "s1", "T", f"old{i}", {"name": "acupoint meridian", "pack_id": "old-pack"}
        )
        ds._conn.execute(
            "UPDATE doc_nodes SET updated_at=? WHERE node_id=?",
            (f"2026-01-01T00:00:{i:02d}", f"old{i}"),
        )
    for i in range(new_count):
        ds.upsert_node_doc(
            "s1", "T", f"new{i}", {"name": "widget catalog", "pack_id": "new-pack"}
        )
        ds._conn.execute(
            "UPDATE doc_nodes SET updated_at=? WHERE node_id=?",
            (f"2026-01-02T00:00:{i:02d}", f"new{i}"),
        )
    ds._conn.commit()


def test_398_capped_global_index_excludes_a_correctly_scoped_pack(tmp_path, monkeypatch) -> None:
    """#398 diagnostic, NOT fixed by this PR: old-pack is correctly named in
    pack_ids (so #397's scoping fix has already done its job), but every one
    of its 10 rows falls outside a cap of 10 that keeps only the 10 newest
    rows across BOTH packs. The BM25 leg still returns zero hits for
    old-pack's own content -- the root cause is the global (not per-pack)
    cap, tracked separately as #398 and left unfixed here (design doc s.7).
    """
    from opencrab.ontology import query as query_module
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    _seed_two_packs_ordered(ds, old_count=10, new_count=10)

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 10)  # cap < corpus (20)

    hybrid = _hybrid(ds)
    try:
        hits = hybrid._bm25_search(
            "acupoint", spaces=None, limit=25, pack_ids=["old-pack"]
        )
        assert hits == [], (
            "#398 not yet fixed: old-pack's rows are all outside the global "
            "cap window, so a correctly scoped query still finds nothing"
        )
    finally:
        hybrid.shutdown_bm25()


def test_398_reproduction_fixture_confirms_hits_once_the_cap_is_lifted(
    tmp_path, monkeypatch
) -> None:
    """Reusable acceptance check for whoever fixes #398: the SAME fixture as
    above, with the cap raised to cover the whole corpus, must recover
    old-pack's hits. This documents the fixture as a valid reproduction of
    the bug (it is the cap, not the query or the pack scoping, that hides
    the rows) and gives #398's fix a concrete "before/after" pair to check
    against."""
    from opencrab.ontology import query as query_module
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    _seed_two_packs_ordered(ds, old_count=10, new_count=10)

    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 50)  # cap >= corpus (20)

    hybrid = _hybrid(ds)
    try:
        hits = hybrid._bm25_search(
            "acupoint", spaces=None, limit=25, pack_ids=["old-pack"]
        )
        assert {h["node_id"] for h in hits} == {f"old{i}" for i in range(10)}
    finally:
        hybrid.shutdown_bm25()


def test_398_capped_global_index_warns_for_missing_requested_pack(tmp_path, monkeypatch) -> None:
    from opencrab.ontology import query as query_module
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    ds = LocalSQLDocStore(str(tmp_path / "doc.db"))
    if not getattr(ds, "_available", False):
        pytest.skip("LocalSQLDocStore unavailable")
    _seed_two_packs_ordered(ds, old_count=10, new_count=10)
    monkeypatch.setattr(query_module, "_BM25_NODE_LIMIT", 10)
    hybrid = _hybrid(ds)
    try:
        hits, warnings = hybrid._bm25_search_with_warnings(
            "acupoint", spaces=None, limit=25, pack_ids=["old-pack"]
        )
        assert hits == []
        assert any("old-pack" in warning and "missing" in warning for warning in warnings)
    finally:
        hybrid.shutdown_bm25()


def test_398_empty_store_does_not_warn_about_missing_pack() -> None:
    """#398 CLI regression: an empty store has complete coverage (0 rows
    total, 0 rows indexed -- nothing was left out by the cap), so a
    requested pack_id that simply has no rows anywhere must not produce a
    'missing requested pack ids' warning. That framing implies an indexing
    problem; an empty pack has none."""
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[])
    doc_store.bm25_fingerprint = MagicMock(return_value=(0, ""))
    hybrid = _hybrid(doc_store)
    try:
        _hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["nonexistent-pack"]
        )
        assert not any("missing" in warning for warning in warnings)
    finally:
        hybrid.shutdown_bm25()


def test_398_legacy_build_reads_nodes_once_and_reports_unknown_total() -> None:
    nodes = [_node("a", pack_id="A")]

    class LegacyStore:
        def __init__(self) -> None:
            self.calls = 0

        def list_nodes(self, limit):
            self.calls += 1
            return nodes

    store = LegacyStore()
    hybrid = _hybrid(store)
    try:
        _hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert store.calls == 1
        assert hybrid._bm25.state.index._docs is nodes
        assert hybrid._bm25.state.total_rows is None
        assert any("total unknown" in warning for warning in warnings)
    finally:
        hybrid.shutdown_bm25()


def test_398_all_covered_partial_rows_warns() -> None:
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(2, "latest"))
    hybrid = _hybrid(doc_store)
    try:
        _hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert not any("missing" in warning for warning in warnings)
        assert any("partial rows" in warning for warning in warnings)
    finally:
        hybrid.shutdown_bm25()


def test_398_ready_probe_failure_keeps_state_and_its_warning() -> None:
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(2, "latest"))
    hybrid = _hybrid(doc_store)
    try:
        _hits, first_warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        first_state = hybrid._bm25.state
        doc_store.bm25_fingerprint.side_effect = RuntimeError("probe failed")
        _hits, second_warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert hybrid._bm25.state is first_state
        assert second_warnings == first_warnings
    finally:
        hybrid.shutdown_bm25()


def test_398_same_fingerprint_keeps_the_complete_state_reference() -> None:
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))
    hybrid = _hybrid(doc_store)
    try:
        hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["A"])
        first_state = hybrid._bm25.state
        reads_after_cold_build = doc_store.list_nodes.call_count
        hybrid.invalidate_bm25_cache()
        assert _wait_until(lambda: hybrid._bm25_dirty is False)
        assert hybrid._bm25.state is first_state
        assert doc_store.list_nodes.call_count == reads_after_cold_build
    finally:
        hybrid.shutdown_bm25()


def test_398_ready_worker_probe_failure_preserves_the_published_state() -> None:
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))
    hybrid = _hybrid(doc_store)
    try:
        hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["A"])
        first_state = hybrid._bm25.state
        doc_store.bm25_fingerprint.side_effect = RuntimeError("probe failed")
        hybrid.invalidate_bm25_cache()
        assert _wait_until(lambda: hybrid._bm25_dirty is False)
        assert hybrid._bm25.state is first_state
    finally:
        hybrid.shutdown_bm25()


def test_398_search_keeps_hits_and_warnings_from_one_state(monkeypatch) -> None:
    doc_store = MagicMock()
    hybrid = _hybrid(doc_store)
    first_index = MagicMock()
    first_index.search = MagicMock(return_value=[{"node_id": "first"}])
    second_index = MagicMock()
    second_index.search = MagicMock(return_value=[{"node_id": "second"}])
    first_state = Bm25CacheState(
        index=first_index,
        probe_fingerprint=(1, ""),
        indexed_rows=1,
        total_rows=2,
        covered_pack_ids=frozenset({"A"}),
        generation=1,
    )
    second_state = Bm25CacheState(
        index=second_index,
        probe_fingerprint=(2, ""),
        indexed_rows=2,
        total_rows=2,
        covered_pack_ids=frozenset({"A", "B"}),
        generation=2,
    )
    hybrid._bm25.state = first_state

    def search_then_swap(*args, **kwargs):
        hybrid._bm25.state = second_state
        return [{"node_id": "first"}]

    first_index.search.side_effect = search_then_swap
    monkeypatch.setattr(hybrid._bm25, "_native_probe", lambda _store: None)
    try:
        hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A", "B"]
        )
        assert [hit["node_id"] for hit in hits] == ["first"]
        assert any("missing" in warning and "B" in warning for warning in warnings)
        assert any("partial rows" in warning for warning in warnings)
        second_index.search.assert_not_called()
    finally:
        hybrid.shutdown_bm25()


def test_398_coverage_uses_strict_scope_pack_id() -> None:
    # total_rows=2 vs. one returned node keeps the scan "incomplete" (#398
    # narrows the missing-pack warning to fire only on a complete scan, see
    # _coverage_warnings) so this fixture stays decoupled from that
    # completeness signal and keeps testing what it is actually named for:
    # that covered_pack_ids never treats a forged source_path as coverage.
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[{
        **_node("forged", text="alpha"),
        "source_path": "/packs/forged-pack/source.txt",
    }])
    doc_store.bm25_fingerprint = MagicMock(return_value=(2, ""))
    hybrid = _hybrid(doc_store)
    try:
        _hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["forged-pack"]
        )
        assert any("missing" in warning and "forged-pack" in warning for warning in warnings)
    finally:
        hybrid.shutdown_bm25()


def test_398_ready_legacy_hot_path_detects_change_via_fallback_marker() -> None:
    """#398 regression: the ready hot path for a legacy store (no
    ``bm25_fingerprint``) must recompute the fallback marker via
    ``_bm25_probe_fingerprint()`` on every search and invalidate on change,
    exactly like the native path already does. Before this fix, a native
    probe of ``None`` skipped change detection entirely and the cache never
    noticed writes on a legacy store between searches.
    """

    class LegacyStore:
        def __init__(self, nodes):
            self.nodes = nodes

        def list_nodes(self, limit):
            return self.nodes

    store = LegacyStore([_node("a", pack_id="A")])
    hybrid = _hybrid(store)
    try:
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert hybrid._bm25.state.indexed_rows == 1

        store.nodes = [_node("a", pack_id="A"), _node("b", pack_id="A")]
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert _wait_until(lambda: hybrid._bm25.state.indexed_rows == 2)
    finally:
        hybrid.shutdown_bm25()


def test_398_hot_path_marker_failure_preserves_hits_and_warnings(monkeypatch) -> None:
    """#398: a probe/marker computation failure inside the ready hot path must
    not discard an already-successful search. The change-detection attempt is
    caught locally, the invalidate attempt is skipped, and the search below
    still runs against the already-captured state exactly as before this
    observability path existed.
    """
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))
    hybrid = _hybrid(doc_store)
    try:
        hits_before, warnings_before = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert hits_before  # cold build produced a real hit to preserve

        monkeypatch.setattr(
            hybrid,
            "_bm25_probe_fingerprint",
            MagicMock(side_effect=RuntimeError("marker boom")),
        )
        hits_after, warnings_after = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert hits_after == hits_before
        assert warnings_after == warnings_before
    finally:
        hybrid.shutdown_bm25()


def test_398_index_search_failure_still_returns_empty_pair() -> None:
    """Control group for the test above: a failure inside ``index.search()``
    itself is a different contract point than probe/marker containment. The
    existing outer catch in ``_bm25_search_state()`` still returns ``([], [])``
    -- this issue's local containment does not change that existing contract.
    """
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))
    hybrid = _hybrid(doc_store)
    try:
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        state = hybrid._bm25.state
        state.index.search = MagicMock(side_effect=RuntimeError("search boom"))
        hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert hits == []
        assert warnings == []
    finally:
        hybrid.shutdown_bm25()


def test_398_probe_failure_racing_worker_publish_keeps_hits_and_warnings_from_one_state(
    monkeypatch,
) -> None:
    """#398 axis B control: probe/marker computation fails inside the very
    call that also triggers a background worker publish (S0 -> S1 -> S2).
    The re-fetched reference (S1) must drive both the search and its
    coverage warnings together. Mixing S1's hits with a different state's
    coverage metadata (for example S2's) is the defect this test targets.
    """
    doc_store = MagicMock()
    hybrid = _hybrid(doc_store)

    s2_index = MagicMock()
    s2_index.search = MagicMock(return_value=[{"node_id": "s2-hit"}])

    def s1_search_then_second_publish(*args, **kwargs):
        # Another worker publish (S1 -> S2) lands mid-search. Correct code
        # already holds this call's local ``state`` variable (S1), so it is
        # unaffected -- the mixing mutation below targets this exact spot.
        hybrid._bm25.state = s2
        return [{"node_id": "s1-hit"}]

    s1_index = MagicMock()
    s1_index.search = MagicMock(side_effect=s1_search_then_second_publish)

    s0 = Bm25CacheState(
        index=MagicMock(), probe_fingerprint=(0, ""), indexed_rows=1,
        total_rows=1, covered_pack_ids=frozenset({"A"}), generation=0,
    )
    s1 = Bm25CacheState(
        index=s1_index, probe_fingerprint=(1, ""), indexed_rows=1,
        total_rows=2, covered_pack_ids=frozenset({"A"}), generation=1,
    )
    s2 = Bm25CacheState(
        index=s2_index, probe_fingerprint=(2, ""), indexed_rows=2,
        total_rows=2, covered_pack_ids=frozenset({"A", "B"}), generation=2,
    )
    hybrid._bm25.state = s0

    def probe_fails_after_worker_publishes(*args, **kwargs):
        hybrid._bm25.state = s1  # worker publishes S1 during the probe attempt
        raise RuntimeError("marker boom mid-race")

    monkeypatch.setattr(
        hybrid, "_bm25_probe_fingerprint", probe_fails_after_worker_publishes
    )
    try:
        hits, warnings = hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        # This test's probe always fails, so correct code deterministically
        # re-fetches S1 (no scheduler-luck race to arbitrate).
        assert hits == [{"node_id": "s1-hit", "source": "bm25"}]
        # S1: total=2, indexed=1 -> partial. S2: total=2, indexed=2 -> no
        # partial. That sign difference is what the mixing mutation flips.
        assert any("partial rows" in w for w in warnings)
    finally:
        hybrid.shutdown_bm25()


def test_398_ready_rebuild_discards_stale_candidate_when_invalidate_wins_the_race(
    monkeypatch, caplog,
) -> None:
    """#398 candidate-publish two-orderings (invalidate-wins branch).

    Once candidate S1 is fully built, a separate invalidate() may raise
    the epoch first. The worker must then discard S1 and keep S0.

    The barrier wait records its outcome instead of asserting inside the
    worker thread. A genuine timing violation now surfaces as a loud
    failure here. It no longer passes silently through a different
    _rebuild_loop path.

    Observation is pinned to the first lock cycle that finishes after
    release. This stays deterministic regardless of any retries that
    follow.
    """
    caplog.set_level(logging.WARNING, logger="opencrab.ontology.query")
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(side_effect=[
        (1, ""),   # cold build probe
        (2, ""),   # rebuild loop re-probe: detects a change -> builds candidate
    ])
    hybrid = _hybrid(doc_store)
    bm25 = hybrid._bm25
    try:
        bm25.ensure_built(doc_store)  # cold build -> S0
        s0 = bm25.state

        candidate_ready = threading.Event()
        release_candidate = threading.Event()
        made_state_returned = threading.Event()
        wait_ok = {"v": None}
        original_make_state = bm25._make_state

        def paused_make_state(observation):
            candidate = original_make_state(observation)
            candidate_ready.set()
            # No assert here: the worker thread must always return control to
            # _rebuild_loop, raise or not, so the real discard/publish
            # comparison always executes. The outcome is recorded and checked
            # on the main thread below instead.
            wait_ok["v"] = release_candidate.wait(timeout=2.0)
            made_state_returned.set()
            return candidate

        monkeypatch.setattr(bm25, "_make_state", paused_make_state)

        # Pure observation wrapper around self._lock: it never holds the
        # lock independently, it only counts release() calls. Holding this
        # lock (as a reused cold-build-style barrier) risks deadlock across
        # its several acquisition sites; counting releases does not.
        release_count = {"n": 0}
        real_lock = bm25._lock

        class _CountingLock:
            def acquire(self, *a, **kw):
                return real_lock.acquire(*a, **kw)

            def release(self):
                real_lock.release()
                release_count["n"] += 1

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc_info):
                self.release()

        bm25._lock = _CountingLock()

        bm25.invalidate()  # wakes the rebuild loop through re-probe and candidate build
        assert _wait_until(lambda: candidate_ready.is_set())

        bm25.invalidate()  # this second invalidate always acquires the lock first
        baseline = release_count["n"]
        release_candidate.set()

        # Wait only for the first lock cycle (publish-or-discard decision)
        # after release to finish. Any retries afterward do not call
        # _make_state again, since this test's mock only has two entries.
        assert _wait_until(lambda: release_count["n"] > baseline)

        # Positive observation: the targeted branch's precondition (the
        # worker thread returning control after the barrier wait) actually
        # happened, and _rebuild_loop's outer except never fired -- so the
        # lock cycle observed above is the real discard comparison, not a
        # coincidental cycle from an unrelated retry after a swallowed
        # exception (#398 axis B finding).
        assert made_state_returned.is_set(), (
            "_make_state monkeypatch never returned control to _rebuild_loop"
        )
        assert not any(
            "BM25 background rebuild failed" in rec.message
            for rec in caplog.records
        ), "background rebuild loop swallowed an exception during this test"
        assert wait_ok["v"] is True, (
            "release_candidate wait timed out -- the targeted race was not "
            "actually reproduced, so the discard/publish comparison below cannot be "
            "trusted as a test of the intended ordering"
        )
        assert bm25.state is s0
    finally:
        hybrid.shutdown_bm25()


def test_398_ready_rebuild_converges_when_publish_wins_the_race(caplog) -> None:
    """#398 candidate-publish two-orderings (publish-wins branch).

    Candidate S1 may publish before any competing invalidate arrives.
    That invalidate must still bump dirty, epoch, and wake. The next
    iteration then converges to S2.
    """
    caplog.set_level(logging.WARNING, logger="opencrab.ontology.query")
    doc_store = MagicMock()
    doc_store.list_nodes = MagicMock(side_effect=[
        [_node("a", pack_id="A")],
        [_node("a", pack_id="A"), _node("b", pack_id="A")],
        [_node("a", pack_id="A"), _node("b", pack_id="A"), _node("c", pack_id="A")],
    ])
    doc_store.bm25_fingerprint = MagicMock(side_effect=[(1, ""), (2, ""), (3, "")])
    hybrid = _hybrid(doc_store)
    bm25 = hybrid._bm25
    try:
        bm25.ensure_built(doc_store)  # S0 (1 row)
        bm25.invalidate()
        assert _wait_until(lambda: bm25.state.indexed_rows == 2)  # S1 published
        assert bm25.dirty is False

        bm25.invalidate()  # only called after publish is confirmed -> ordering is guaranteed
        assert _wait_until(lambda: bm25.state.indexed_rows == 3)  # converges to S2
        assert bm25.dirty is False

        # Defense in depth: this test does not monkeypatch _make_state or
        # introduce any artificial delay, so an exception here would only
        # come from a genuine production bug, and the _wait_until calls
        # above already fail loudly (via timeout) if that bug ever starves a
        # publish. This assertion just makes the diagnosis point straight at
        # _rebuild_loop's outer except instead of a generic timeout message.
        assert not any(
            "BM25 background rebuild failed" in rec.message
            for rec in caplog.records
        ), "background rebuild loop swallowed an exception during this test"
    finally:
        hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# #422 -- search-driven re-detection during a build must not discard it
# ---------------------------------------------------------------------------


def test_422_search_restale_during_inflight_build_does_not_discard_candidate(
    monkeypatch, caplog,
) -> None:
    """#422: search requests that land during an in-flight rebuild must not
    make the worker discard that rebuild's candidate.

    Root cause: the search hot path used to call ``invalidate()``, which
    unconditionally bumps ``_epoch``, even when the hot path was only
    re-detecting a staleness the worker already knows about and is already
    rebuilding for. Every such redundant re-detection during a build window
    bumped the epoch again with no real intervening write, so the publish-time
    ``self._epoch == build_epoch`` comparison in ``_rebuild_loop`` almost
    never matched and the worker discarded an otherwise up-to-date candidate,
    forever, as long as search traffic kept arriving faster than one build
    cycle (this is exactly what ``repro_livelock.py`` demonstrates by
    execution). The fix: the hot path now calls ``mark_stale()``, which wakes
    the worker without touching the epoch.

    This test mutates the store's real content once (one real write) and
    then floods the in-flight build with five redundant re-detections. It
    counts ``_make_state`` calls instead of polling only the final
    fingerprint, because a final-fingerprint-only check cannot distinguish
    "the first candidate was discarded and a second, wasteful cycle had to
    rebuild the same content" from "the first candidate published directly"
    -- both eventually converge to the same fingerprint once search traffic
    stops, but only the former is the bug this issue reports.
    """
    caplog.set_level(logging.WARNING, logger="opencrab.ontology.query")

    class LegacyStore:
        """No ``bm25_fingerprint`` attribute -> forces the legacy marker
        path, which is what the search hot path uses to detect staleness by
        recomputing ``compute_fingerprint(nodes)`` on every call."""

        def __init__(self, nodes: list[dict]) -> None:
            self.nodes = nodes

        def list_nodes(self, limit: int) -> list[dict]:
            return self.nodes

    store = LegacyStore([_node("a", pack_id="A")])
    hybrid = _hybrid(store)
    bm25 = hybrid._bm25
    try:
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )  # cold build -> S0({a})

        entered = threading.Event()
        release = threading.Event()
        call_count = {"n": 0}
        original_make_state = bm25._make_state

        def gated_make_state(observation):
            call_count["n"] += 1
            entered.set()
            assert release.wait(timeout=2.0), (
                "release was not set within the timeout -- the test's gate "
                "could not enforce the intended in-flight-build interleaving"
            )
            return original_make_state(observation)

        monkeypatch.setattr(bm25, "_make_state", gated_make_state)

        store.nodes = [_node("a", pack_id="A"), _node("b", pack_id="A")]  # real write
        hybrid.invalidate_bm25_cache()  # write-path call -> epoch += 1
        assert _wait_until(lambda: entered.is_set())
        assert call_count["n"] == 1, (
            "build did not reach _make_state -- the store mutation did not "
            "actually change the observed fingerprint"
        )
        epoch_after_invalidate = bm25._epoch

        for _ in range(5):
            hybrid._bm25_search_with_warnings(
                "alpha", spaces=None, limit=5, pack_ids=["A"]
            )

        # Recorded, not asserted yet: an early assert here would halt the
        # test before the call_count evidence below is captured.
        epoch_after_flood = bm25._epoch

        release.set()
        target_fp = compute_fingerprint(store.nodes)
        assert _wait_until(
            lambda: bm25.state is not None and bm25.state.probe_fingerprint == target_fp
        )

        assert epoch_after_flood == epoch_after_invalidate, (
            "five redundant re-detections bumped the epoch -- mark_stale() "
            "regressed to invalidate()'s behavior"
        )
        assert call_count["n"] == 1, (
            "the first candidate was discarded and rebuilt a second time "
            "for the same content -- this is the #422 livelock"
        )
        assert not any(
            "BM25 background rebuild failed" in rec.message
            for rec in caplog.records
        ), "background rebuild loop swallowed an exception during this test"
    finally:
        hybrid.shutdown_bm25()


def test_422_late_external_write_during_build_needs_one_more_cycle_then_converges(
    monkeypatch, caplog,
) -> None:
    """#422 boundary case: a live deployment splits the search gateway
    process from the pack-loading process, so a loader's write never calls
    the gateway's ``invalidate()`` -- the gateway only ever learns of a
    change through the search hot path's fingerprint re-detection
    (``mark_stale()``). This test drives two such external writes and checks
    the two-cycle convergence contract end to end, without relying on any
    epoch bump.

    Two distinct triggers are checked separately, because they are different
    code paths: (a) a first cycle whose candidate reflects only the write
    that happened before its observation, which still publishes because
    ``mark_stale()`` never touched the epoch; and (b) a *new* search issued
    strictly after that publish, which itself must re-detect the still-stale
    published state and independently schedule the second cycle -- this is
    checked separately from any wake left over from before the first
    publish, since a leftover wake and a fresh post-publish detection are
    different mechanisms and a symmetric design bug could pass the wrong one
    silently.

    Termination is checked by counting ``_make_state`` calls, not by
    checking for the absence of further ``_observe()`` entries: once content
    stops changing, the worker may still legitimately re-enter ``_observe()``
    on a leftover wake and take the same-fingerprint early exit without
    calling ``_make_state`` again -- that is correct behavior, not a bug.
    """
    caplog.set_level(logging.WARNING, logger="opencrab.ontology.query")

    class LegacyStore:
        def __init__(self, nodes: list[dict]) -> None:
            self.nodes = nodes

        def list_nodes(self, limit: int) -> list[dict]:
            return self.nodes

    store = LegacyStore([_node("a", pack_id="A")])
    hybrid = _hybrid(store)
    bm25 = hybrid._bm25
    try:
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )  # cold build -> S0({a})

        entered = threading.Event()
        release = threading.Event()
        last_observation = {"v": None}
        rebuild_count = {"n": 0}
        original_observe = bm25._observe
        original_make_state = bm25._make_state

        def gated_observe(doc_store, probe=None):
            observation = original_observe(doc_store, probe)
            last_observation["v"] = observation
            entered.set()
            assert release.wait(timeout=2.0), (
                "release was not set within the timeout -- the test's gate "
                "could not enforce the intended cycle-by-cycle interleaving"
            )
            release.clear()
            return observation

        def counting_make_state(observation):
            rebuild_count["n"] += 1
            return original_make_state(observation)

        monkeypatch.setattr(bm25, "_observe", gated_observe)
        monkeypatch.setattr(bm25, "_make_state", counting_make_state)

        # 1. External write #1 (no invalidate_bm25_cache() -- a separate
        #    loader process is simulated).
        store.nodes = [_node("a", pack_id="A"), _node("b", pack_id="A")]

        # 2. Exactly one search opens the first cycle. No further searches
        #    are sent until the first publish is confirmed below, so no
        #    leftover wake can accumulate.
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert _wait_until(lambda: entered.is_set())
        baseline_epoch = bm25._epoch
        assert last_observation["v"].probe.fingerprint == compute_fingerprint(store.nodes)
        entered.clear()

        # 3. External write #2 lands after the first cycle already observed
        #    (and thus will not reflect) write #1's content.
        store.nodes = [_node("a", pack_id="A"), _node("b", pack_id="A"), _node("c", pack_id="A")]
        write1_fp = compute_fingerprint(
            [_node("a", pack_id="A"), _node("b", pack_id="A")]
        )

        # 4. Release the first cycle. It publishes the write-#1-only
        #    candidate, because mark_stale() never touched the epoch.
        release.set()
        assert _wait_until(
            lambda: bm25.state is not None and bm25.state.probe_fingerprint == write1_fp
        )
        g1 = bm25.state.generation
        assert rebuild_count["n"] == 1

        # 5. No automatic second cycle: nothing set _wake after the single
        #    search in step 2 was consumed, so the worker must stay idle.
        assert not entered.wait(timeout=0.3), (
            "a second _observe() cycle started without any new search -- "
            "this would be a leftover-wake artifact, not the post-publish "
            "re-detection this test is isolating"
        )

        # 6. A *new* search, issued strictly after the first publish, must
        #    itself detect the still-stale published state and schedule the
        #    second cycle.
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )
        assert bm25._epoch == baseline_epoch
        assert _wait_until(lambda: entered.is_set())
        assert last_observation["v"].probe.fingerprint == compute_fingerprint(store.nodes)

        # 7. Release the second cycle. It publishes the fully up-to-date
        #    candidate.
        release.set()
        target_fp = compute_fingerprint(store.nodes)
        assert _wait_until(
            lambda: bm25.state is not None
            and bm25.state.probe_fingerprint == target_fp
            and bm25.state.generation == g1 + 1
        )
        assert rebuild_count["n"] == 2

        # 8. Once converged, hammering search must not create further
        #    rebuilds: content already matches the published fingerprint, so
        #    the hot path does not even call mark_stale().
        for _ in range(5):
            hybrid._bm25_search_with_warnings(
                "alpha", spaces=None, limit=5, pack_ids=["A"]
            )
        assert rebuild_count["n"] == 2

        assert bm25._epoch == baseline_epoch, (
            "epoch moved even though neither external write went through "
            "invalidate_bm25_cache()"
        )
        assert not any(
            "BM25 background rebuild failed" in rec.message
            for rec in caplog.records
        ), "background rebuild loop swallowed an exception during this test"
    finally:
        hybrid.shutdown_bm25()


def test_422_slow_build_with_hammering_search_converges_within_bounded_time() -> None:
    """#422 boundary case: reuses ``repro_livelock.py``'s exact load
    conditions (0.5s build, 0.05s search interval, 2.5s of concurrent
    search, one external write) as a non-deterministic supplementary check
    that the fix converges before the search load itself stops -- the
    barrier-based tests above are the deterministic, primary detection.
    """
    build_sleep = 0.5
    search_interval = 0.05
    search_duration = 2.5

    class LegacyStore:
        def __init__(self, nodes: list[dict]) -> None:
            self.nodes = nodes

        def list_nodes(self, limit: int) -> list[dict]:
            return self.nodes

    initial_nodes = [_node("a", pack_id="A"), _node("a2", pack_id="A")]
    store = LegacyStore(initial_nodes)
    hybrid = _hybrid(store)
    try:
        hybrid._bm25_search_with_warnings(
            "alpha", spaces=None, limit=5, pack_ids=["A"]
        )  # cold build
        cold_fp = hybrid._bm25.state.probe_fingerprint
        assert cold_fp == compute_fingerprint(initial_nodes)

        store.nodes = initial_nodes + [_node("b", pack_id="A")]  # one external write
        new_fp = compute_fingerprint(store.nodes)

        real_make_state = hybrid._bm25._make_state

        def slow_make_state(observation):
            time.sleep(build_sleep)
            return real_make_state(observation)

        hybrid._bm25._make_state = slow_make_state

        stop = threading.Event()
        converged_within_load = {"v": False}

        def search_loop() -> None:
            while not stop.is_set():
                hybrid._bm25_search_with_warnings(
                    "alpha", spaces=None, limit=5, pack_ids=["A"]
                )
                if hybrid._bm25.state.probe_fingerprint == new_fp:
                    converged_within_load["v"] = True
                time.sleep(search_interval)

        searcher = threading.Thread(target=search_loop, daemon=True)
        searcher.start()
        time.sleep(search_duration)
        stop.set()
        searcher.join(timeout=2.0)

        assert converged_within_load["v"], (
            "publication of the up-to-date candidate did not happen before "
            "the search load itself stopped"
        )
    finally:
        hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# #411 -- content fallback pins one BM25 state and probes once
# ---------------------------------------------------------------------------


def _warm_hybrid(packs: list[str]) -> tuple[HybridQuery, MagicMock]:
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(
        return_value=[_node(f"n-{p}", pack_id=p, text=f"alpha {p}") for p in packs]
    )
    doc_store.bm25_fingerprint = MagicMock(return_value=(len(packs), ""))
    hybrid = _hybrid(doc_store)
    hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=[packs[0]])  # cold build
    return hybrid, doc_store


def test_411_per_pack_search_probes_once_and_matches_loop() -> None:
    from opencrab.ontology.pack_registry import PackInfo, _choose_by_content

    packs = [f"p{i}" for i in range(12)]
    hybrid, doc_store = _warm_hybrid(packs)
    try:
        loop_hits = {
            p: hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=[p])
            for p in packs
        }
        doc_store.bm25_fingerprint.reset_mock()
        got = hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=packs)
        assert doc_store.bm25_fingerprint.call_count == 1
        assert got == loop_hits

        doc_store.bm25_fingerprint.reset_mock()
        registry = [PackInfo(pack_id=p, title=p, description="") for p in packs]
        _choose_by_content("alpha", registry, hybrid, spaces=None)
        assert doc_store.bm25_fingerprint.call_count == 1
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_marks_stale_once_without_epoch_bump() -> None:
    packs = ["a", "b", "c"]
    hybrid, doc_store = _warm_hybrid(packs)
    try:
        doc_store.bm25_fingerprint.return_value = (99, "")  # external write
        epoch = hybrid._bm25._epoch
        old_state = hybrid._bm25.state
        marks = []
        hybrid._bm25.mark_stale = lambda: marks.append(1)  # no worker wake
        got = hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=packs)
        assert len(marks) == 1
        assert hybrid._bm25._epoch == epoch
        assert set(got) == set(packs)
        assert hybrid._bm25.state is old_state  # older state served until publish
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_pins_state_across_publish() -> None:
    packs = ["a", "b"]
    hybrid, _doc_store = _warm_hybrid(packs)
    try:
        pinned = hybrid._bm25.state
        real_search = pinned.index.search
        seen = []

        def publish_after_first(*args, **kwargs):
            if not seen:
                hybrid._bm25.state = Bm25CacheState(
                    index=BM25Index.build([]),
                    probe_fingerprint=(0, ""),
                    indexed_rows=0,
                    total_rows=0,
                    covered_pack_ids=frozenset(),
                    generation=pinned.generation + 1,
                )
            seen.append(1)
            return real_search(*args, **kwargs)

        pinned.index.search = publish_after_first
        got = hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=packs)
        assert len(seen) == 2  # both packs searched the pinned index
        assert got["b"], "second pack must still see the pinned generation"
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_empty_pack_ids_returns_empty_dict() -> None:
    hybrid, _doc_store = _warm_hybrid(["a"])
    try:
        assert hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=[]) == {}
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_instance_override_returns_none_before_probe() -> None:
    hybrid, doc_store = _warm_hybrid(["a"])
    try:
        hybrid._bm25_search = lambda *a, **k: []  # instance override (test double)
        doc_store.bm25_fingerprint.reset_mock()
        assert hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=["a"]) is None
        assert doc_store.bm25_fingerprint.call_count == 0
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_exception_returns_none() -> None:
    hybrid, _doc_store = _warm_hybrid(["a"])
    try:
        hybrid._bm25.state.index.search = MagicMock(side_effect=RuntimeError("boom"))
        assert hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=["a"]) is None
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_cold_build_raise_returns_none() -> None:
    doc_store = MagicMock()
    doc_store.available = True
    hybrid = _hybrid(doc_store)
    try:
        hybrid._bm25.ensure_built = MagicMock(side_effect=RuntimeError("cold build"))
        assert hybrid._bm25.state is None
        assert hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=["a"]) is None
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_state_none_returns_none_without_error(caplog) -> None:
    hybrid, _doc_store = _warm_hybrid(["a"])
    try:
        search = MagicMock(return_value=[])
        hybrid._bm25.state.index.search = search
        hybrid._bm25_acquire_state = lambda: None
        with caplog.at_level(logging.WARNING):
            assert hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=["a"]) is None
        assert search.call_count == 0
        assert "per-pack search error" not in caplog.text
    finally:
        hybrid.shutdown_bm25()


def test_411_per_pack_search_serves_new_state_after_publish() -> None:
    hybrid, doc_store = _warm_hybrid(["a"])
    try:
        hybrid._bm25.mark_stale = lambda: None  # no worker wake
        fp = doc_store.bm25_fingerprint.return_value
        new_index = BM25Index.build([_node("g1", pack_id="a", text="gamma")], fingerprint=fp)
        old = hybrid._bm25.state
        hybrid._bm25.state = Bm25CacheState(
            index=new_index,
            probe_fingerprint=fp,
            indexed_rows=1,
            total_rows=1,
            covered_pack_ids=frozenset({"a"}),
            generation=old.generation + 1,
        )
        got = hybrid._bm25_search_per_pack("gamma", None, 5, pack_ids=["a"])
        assert [h["node_id"] for h in got["a"]] == ["g1"]
        assert old.index.search("gamma", limit=5, pack_ids=["a"]) == []
    finally:
        hybrid.shutdown_bm25()


def test_411_probe_failure_still_searches_without_none() -> None:
    hybrid, doc_store = _warm_hybrid(["a"])
    try:
        doc_store.bm25_fingerprint.side_effect = RuntimeError("probe down")
        got = hybrid._bm25_search_per_pack("alpha", None, 5, pack_ids=["a"])
        assert got is not None and got["a"]
    finally:
        hybrid.shutdown_bm25()
