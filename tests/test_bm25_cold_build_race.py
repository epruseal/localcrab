"""#302 — the BM25 cold build must be serialised across its two call sites.

``HybridQuery._bm25_search()``'s synchronous cold-start branch and
``_Bm25CacheWorker._rebuild_loop()``'s own first wake both observe
``cache is None`` and, before this fix, both built a ``BM25Index``
independently -- a query thread and the background worker racing the very
first build. Both call sites now route through
``_Bm25CacheWorker.ensure_built()``, which serialises them with a dedicated
``_cold_build_lock`` (double-checked locking, mirroring ``_get_context()``'s
pattern for #192).

The concurrency test below does NOT assert "the loser did not finish in
time" (that passes on scheduling delay alone). It installs an instrumented
stand-in for ``_cold_build_lock`` that records the ident of every acquiring
thread, and waits for the loser's own ident to appear -- proof that it
actually contended for the lock, not merely that it was slow. Every wait
carries an upper bound, a termination condition for the harness rather than
a pass condition.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from opencrab.ontology.query import HybridQuery

_WAIT = 30.0


def _node(node_id: str, *, pack_id: str | None = None, text: str = "alpha beta") -> dict:
    props: dict = {"name": text}
    if pack_id is not None:
        props["pack_id"] = pack_id
    return {
        "node_id": node_id,
        "space": "claim",
        "node_type": "Claim",
        "properties": props,
    }


def _hybrid(doc_store) -> HybridQuery:
    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    hybrid = HybridQuery(chroma, neo4j)
    hybrid._doc_store = doc_store
    hybrid._bm25_debounce = 0.0  # no debounce delay in tests
    return hybrid


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _BuildRecorder:
    """Stand-in for ``_get_bm25()``'s returned ``BM25Index`` class. Counts
    ``.build(...)`` calls and can hold the first one open indefinitely --
    mirrors ``_FactoryRecorder`` in ``tests/test_mcp_context_init_race.py``.
    """

    def __init__(self, *, hold_first: bool = False) -> None:
        self._guard = threading.Lock()
        self.calls = 0
        self.hold_first = hold_first
        self.first_entered = threading.Event()
        self.release_first = threading.Event()

    def build(self, nodes, fingerprint=None):  # noqa: ARG002 - shape match
        with self._guard:
            self.calls += 1
            n = self.calls
        if n == 1 and self.hold_first:
            self.first_entered.set()
            assert self.release_first.wait(timeout=_WAIT), "first build never released"
        index = MagicMock(name=f"bm25-{n}")
        index.fingerprint = fingerprint
        index.search = MagicMock(return_value=[{"node_id": nid} for nid in ()])
        return index

    @property
    def call_count(self) -> int:
        with self._guard:
            return self.calls


class _InstrumentedLock:
    """Duck-typed stand-in recording the ident of every acquiring thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self.attempts: list[int] = []

    def acquire(self, *args, **kwargs):
        with self._guard:
            self.attempts.append(threading.get_ident())
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def saw(self, ident: int | None) -> bool:
        with self._guard:
            return ident in self.attempts


# ---------------------------------------------------------------------------
# Normal path (negative control: no forced contention, must pass regardless
# of the lock's presence).
# ---------------------------------------------------------------------------

def test_ensure_built_builds_exactly_once(monkeypatch) -> None:
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))
    hybrid = _hybrid(doc_store)

    first = hybrid._bm25.ensure_built(doc_store)
    second = hybrid._bm25.ensure_built(doc_store)

    assert first is second
    assert doc_store.bm25_fingerprint.call_count == 1
    assert doc_store.list_nodes.call_count == 1


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------

def test_query_thread_and_worker_first_wake_share_one_cold_build(monkeypatch) -> None:
    """A query thread's _bm25_search() cold path and the background worker's
    own first-wake cold path (both of which now call ensure_built()) must
    not both build a BM25Index -- forced via a barrier/hold pair, not by
    hoping the scheduler interleaves them."""
    import opencrab.ontology.query as query_mod

    recorder = _BuildRecorder(hold_first=True)
    monkeypatch.setattr(query_mod, "_get_bm25", lambda: recorder)

    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))

    hybrid = _hybrid(doc_store)
    probe = _InstrumentedLock()
    monkeypatch.setattr(hybrid._bm25, "_cold_build_lock", probe)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def query_worker() -> None:
        try:
            hybrid._bm25_search("alpha", spaces=None, limit=5, pack_ids=["A"])
            results["query"] = hybrid._bm25_cache
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors["query"] = exc

    def rebuild_worker() -> None:
        # What _rebuild_loop() does on its own first wake, when it sees
        # self.cache is None -- the second real call site into ensure_built().
        try:
            results["rebuild"] = hybrid._bm25.ensure_built(doc_store)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors["rebuild"] = exc

    thread_a = threading.Thread(target=query_worker, name="bm25-query", daemon=True)
    thread_b = threading.Thread(target=rebuild_worker, name="bm25-rebuild-wake", daemon=True)

    thread_a.start()
    try:
        assert recorder.first_entered.wait(timeout=_WAIT), "A never reached the builder"
        thread_b.start()

        # Positive observation: wait until B's own ident shows up among the
        # cold-build lock acquisitions. Reverting the fix (no shared lock
        # between the two call sites) means B never contends for anything
        # and this fails deterministically.
        deadline = time.monotonic() + _WAIT
        while not probe.saw(thread_b.ident):
            assert time.monotonic() < deadline, (
                "the rebuild-worker's first-wake path never tried to acquire "
                "the cold-build lock -- ensure_built() is not serialising the "
                "query thread and the background worker"
            )
            time.sleep(0.01)

        # B is parked on the lock, so it cannot have reached the builder.
        assert recorder.call_count == 1
    finally:
        recorder.release_first.set()

    thread_a.join(timeout=_WAIT)
    thread_b.join(timeout=_WAIT)
    assert not thread_a.is_alive(), "query thread never finished"
    assert not thread_b.is_alive(), "rebuild-wake thread never finished"
    assert errors == {}

    assert recorder.call_count == 1
    assert results["query"] is results["rebuild"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_rebuild_loop_first_wake_still_converges_to_the_latest_fingerprint() -> None:
    """_rebuild_loop()'s integration of ensure_built() (design v4) is a
    first-move guard, not a first-run branch: the same wake unconditionally
    re-runs its own probe/compare/build afterward, so if the store already
    changed again by the time that probe runs, the FINAL cache must reflect
    that latest state -- not the value ensure_built() just stamped. This is
    the deliberate redundant-probe cost the design accepts at cold start."""
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.list_nodes = MagicMock(side_effect=[
        [_node("a", pack_id="A")],                        # ensure_built()'s build
        [_node("a", pack_id="A"), _node("b", pack_id="A")],  # loop's own re-probe
    ])
    doc_store.bm25_fingerprint = MagicMock(side_effect=[
        (1, ""),  # ensure_built()'s probe
        (2, ""),  # loop's own re-probe: diverges -> real rebuild
    ])
    hybrid = _hybrid(doc_store)
    try:
        hybrid._bm25.invalidate()
        assert _wait_until(
            lambda: hybrid._bm25_cache is not None and hybrid._bm25_cache.fingerprint == (2, "")
        )
        assert hybrid._bm25_cache_size == 2
    finally:
        hybrid.shutdown_bm25()


def test_ensure_built_swallows_fingerprint_probe_exception(monkeypatch) -> None:
    """#302 v4 fix: ensure_built()'s fingerprint probe must swallow any
    exception exactly like HybridQuery._bm25_probe_fingerprint() already
    does -- a probe failure must not block the cold build itself. Exact
    counter-example from codex's third design-verification round: a store
    whose bm25_fingerprint() raises but list_nodes() still succeeds must
    still get a built cache (via BM25Index.build()'s own
    fingerprint=None -> compute_fingerprint(nodes) fallback), not a
    propagated exception."""
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.bm25_fingerprint = MagicMock(side_effect=RuntimeError("probe boom"))
    doc_store.list_nodes = MagicMock(return_value=[_node("a", pack_id="A")])

    hybrid = _hybrid(doc_store)
    try:
        cache = hybrid._bm25.ensure_built(doc_store)
        assert cache is not None
        assert cache.fingerprint is not None
        hits = cache.search("alpha", pack_ids=["A"], limit=5)
        assert [h["node_id"] for h in hits] == ["a"]
    finally:
        hybrid.shutdown_bm25()


def test_ensure_built_propagates_a_list_nodes_failure(monkeypatch) -> None:
    """Contrast to the probe-exception case above: list_nodes() supplies the
    rows to index, so its own failure must propagate (there is nothing to
    build), not be silently swallowed into a phantom empty index."""
    doc_store = MagicMock()
    doc_store.available = True
    doc_store.bm25_fingerprint = MagicMock(return_value=(1, ""))
    doc_store.list_nodes = MagicMock(side_effect=RuntimeError("list_nodes boom"))

    hybrid = _hybrid(doc_store)
    with pytest.raises(RuntimeError, match="list_nodes boom"):
        hybrid._bm25.ensure_built(doc_store)


# ---------------------------------------------------------------------------
# Reachability boundary (#302 requirement 3): REST never reaches BM25 today
# because apps/api/main.py's _build_context() never attaches a doc store to
# its HybridQuery -- _bm25_search() short-circuits entirely on
# self._doc_store is None. This guards the PR's documented reachability
# judgment against silent staleness if a future change attaches one.
# ---------------------------------------------------------------------------

def test_rest_context_never_attaches_a_doc_store_to_hybrid_query() -> None:
    import inspect

    from apps.api.main import _build_context

    source = inspect.getsource(_build_context)
    assert "_doc_store =" not in source and "_doc_store=" not in source, (
        "_build_context() now assigns _doc_store -- re-examine the #302 "
        "REST-cannot-reach-BM25 judgment (and its PR-body documentation) "
        "before trusting it; this test existing to fail is the point."
    )
