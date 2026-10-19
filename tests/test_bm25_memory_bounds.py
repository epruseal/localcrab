"""#506: the BM25 cache must keep resident memory bounded under a write stream.

The tests pin the five causes named in ``docs/issue-506-bm25-memory.md``: the
rebuild worker's references to superseded indexes, per-document token string
objects, an input window bounded by row count only, back-to-back rebuilds
during a write burst, and the shared window contract of the two budget paths.
"""

from __future__ import annotations

import copy
import datetime
import gc
import math
import threading
import time
import weakref
from collections import Counter
from unittest.mock import MagicMock

import pytest

from opencrab.ontology.bm25 import _B, _K1, BM25Index, _node_text, node_size, trim_to_budget
from opencrab.ontology.query import HybridQuery
from opencrab.ontology.text_cues import tokenize
from opencrab.stores._json import dump_props
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _node(node_id: str, *, pack_id: str | None = "A", text: str = "alpha beta",
          updated_at: str | None = None) -> dict:
    props: dict = {"name": text}
    if pack_id is not None:
        props["pack_id"] = pack_id
    doc: dict = {
        "node_id": node_id,
        "space": "claim",
        "node_type": "Claim",
        "properties": props,
    }
    if updated_at:
        doc["updated_at"] = updated_at
    return doc


def _hybrid(doc_store, debounce: float = 0.0) -> HybridQuery:
    chroma = MagicMock()
    chroma.available = False
    neo4j = MagicMock()
    neo4j.available = False
    hybrid = HybridQuery(chroma, neo4j)
    hybrid._doc_store = doc_store
    hybrid._bm25_debounce = debounce
    return hybrid


class _TimedStore:
    """Native-fingerprint doc store whose reads take a controllable time."""

    available = True

    def __init__(self, delay: float = 0.0) -> None:
        self.nodes = [_node("n0", text="alpha base")]
        self.version = 0
        self.delay = delay
        self.builds: list[list[float | None]] = []
        self.hook = None
        self.fail_at: set[int] = set()

    def write(self) -> None:
        self.version += 1
        self.nodes.append(_node(f"n{self.version}", text=f"alpha w{self.version}"))

    def bm25_fingerprint(self, limit: int = 0) -> tuple[int, str]:
        return (len(self.nodes), str(self.version))

    def list_nodes(self, space=None, limit: int = 100) -> list[dict]:
        idx = len(self.builds)
        self.builds.append([time.monotonic(), None])
        snapshot = list(self.nodes)
        if self.hook is not None:
            self.hook(idx)
        time.sleep(self.delay)
        self.builds[idx][1] = time.monotonic()
        if idx in self.fail_at:
            raise RuntimeError("injected read failure")
        return snapshot


# ---------------------------------------------------------------------------
# 1. Worker references (cause 1)
# ---------------------------------------------------------------------------


def test_published_rebuild_releases_the_superseded_index() -> None:
    store = _TimedStore()
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    try:
        worker.ensure_built(store)
        first = weakref.ref(worker.state.index)
        generation = worker.state.generation
        store.write()
        hybrid.invalidate_bm25_cache()
        assert _wait_until(lambda: worker.state.generation > generation)
        # The rebuild thread is idle once the publish has happened; no other
        # holder of the first index may remain.
        assert _wait_until(lambda: (gc.collect(), first() is None)[1]), (
            "the rebuild worker still references the superseded index"
        )
    finally:
        hybrid.shutdown_bm25()


def test_discarded_candidate_is_released_during_the_rest() -> None:
    store = _TimedStore(delay=0.1)
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    refs: list[weakref.ReferenceType] = []
    real_make = worker._make_state

    def recording(observation):
        state = real_make(observation)
        refs.append(weakref.ref(state.index))
        return state

    worker._make_state = recording  # type: ignore[method-assign]

    def write_during_second_build(idx: int) -> None:
        if idx == 1:  # the first background build: invalidate while it runs
            store.write()
            hybrid.invalidate_bm25_cache()

    try:
        worker.ensure_built(store)  # build 0
        store.write()
        store.hook = write_during_second_build
        hybrid.invalidate_bm25_cache()  # build 1 starts, gets discarded
        assert _wait_until(lambda: len(refs) >= 2)
        discarded = refs[1]
        assert _wait_until(lambda: (gc.collect(), discarded() is None)[1], timeout=0.3), (
            "a discarded candidate stays referenced while the worker rests"
        )
    finally:
        hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# 2. Token string sharing (cause 2)
# ---------------------------------------------------------------------------


def test_equal_tokens_across_documents_are_one_object() -> None:
    nodes = [
        _node("a", text="shared unique-a"),
        _node("b", text="shared unique-b"),
    ]
    index = BM25Index.build(nodes)
    first = next(t for t in index._tokens[0] if t == "shared")
    second = next(t for t in index._tokens[1] if t == "shared")
    assert first is second


def _reference_scores(
    nodes: list[dict], query: str, max_text_chars: int | None = None
) -> dict[str, float]:
    """BM25 from ``tokenize`` and ``_node_text`` only, with no sharing and no index state."""
    docs = [tokenize(_node_text(n)[:max_text_chars]) for n in nodes]
    n_docs = len(docs)
    avgdl = sum(len(d) for d in docs) / max(n_docs, 1)
    df: Counter[str] = Counter()
    for toks in docs:
        for term in set(toks):
            df[term] += 1
    out: dict[str, float] = {}
    for node, toks in zip(nodes, docs, strict=True):
        tf_map = Counter(toks)
        score = 0.0
        for term in tokenize(query):
            if term not in df:
                continue
            idf = math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1)
            tf = tf_map.get(term, 0)
            score += idf * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * len(toks) / max(avgdl, 1)))
        if score > 0:
            out[node["node_id"]] = round(score, 4)
    return out


def test_token_sharing_matches_an_independent_reference() -> None:
    nodes = [_node(str(i), text=f"common term{i % 3} " + "w" * (i % 4)) for i in range(12)]
    index = BM25Index.build(nodes)
    hits = index.search("common term1", pack_ids=["A"], limit=12)
    got = {h["node_id"]: h["score"] for h in hits}
    assert got == _reference_scores(nodes, "common term1")
    assert got  # the reference found hits, so the comparison is not vacuous


# ---------------------------------------------------------------------------
# 3. Input window contract (cause 3)
# ---------------------------------------------------------------------------


def _size(text: str) -> int:
    return len(dump_props({"text": text}))


def _sized(node_id: str, chars: int) -> dict:
    return {
        "node_id": node_id,
        "space": "claim",
        "node_type": "Claim",
        "properties": {"text": "x" * chars, "pack_id": "A"},
    }


def test_node_size_uses_the_stored_json_length() -> None:
    node = {"properties": {"text": "한글"}}
    assert node_size(node) == len(dump_props({"text": "한글"}))


def test_node_size_survives_values_json_cannot_carry() -> None:
    stamp = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    # JSON cannot carry a datetime: the size is that of its str form, not the repr fallback
    assert node_size({"properties": {"at": stamp}}) == len(dump_props({"at": str(stamp)}))
    assert node_size({"properties": {"x": float("nan")}}) > 0
    loop: dict = {}
    loop["self"] = loop
    assert node_size({"properties": loop}) > 0


def test_legacy_build_survives_a_datetime_property(monkeypatch) -> None:
    stamp = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    node = _node("dt")
    node["properties"]["created"] = stamp
    store = _LegacyStore([node])
    hybrid = _hybrid(store)
    try:
        hybrid._bm25.ensure_built(store)
        assert hybrid._bm25.state.indexed_rows == 1
    finally:
        hybrid.shutdown_bm25()


def test_trim_budget_keeps_a_node_while_the_sum_before_it_is_below_the_budget() -> None:
    nodes = [_sized(str(i), 50) for i in range(5)]
    one = node_size(nodes[0])
    # the sum before the third node equals the budget: the loop stops before it
    assert len(trim_to_budget(nodes, max_chars=2 * one)) == 2
    # the sum before the third node is one below the budget: the loop keeps it
    assert len(trim_to_budget(nodes, max_chars=2 * one + 1)) == 3


def test_trim_keeps_a_first_node_larger_than_the_budget() -> None:
    nodes = [_sized("big", 5000), _sized("b", 50)]
    assert [n["node_id"] for n in trim_to_budget(nodes, max_chars=100)] == ["big"]


def test_a_zero_budget_keeps_nothing_in_both_paths(sql_store) -> None:
    nodes = [_sized("a", 50), _sized("b", 50)]
    assert trim_to_budget(nodes, max_chars=0) == []
    _put(sql_store, "a", 50)
    assert sql_store.list_nodes_within_budget(limit=5, max_chars=0) == []


def test_trim_without_a_budget_keeps_everything() -> None:
    nodes = [_sized("a", 5000), _sized("b", 5000)]
    assert trim_to_budget(nodes, max_chars=None) == nodes


@pytest.fixture()
def sql_store(tmp_path):
    store = LocalSQLDocStore(str(tmp_path / "doc_store.db"))
    yield store
    store.close()


def _put(store: LocalSQLDocStore, node_id: str, chars: int) -> None:
    store.upsert_node_doc("claim", "Claim", node_id, {"text": "x" * chars, "pack_id": "A"})
    time.sleep(0.002)  # distinct updated_at, so the newest-first order is total


def test_sql_window_keeps_a_large_node_whole(sql_store) -> None:
    _put(sql_store, "b", 50)
    _put(sql_store, "big", 800)  # newest
    got = sql_store.list_nodes_within_budget(limit=2, max_chars=None)
    assert [n["node_id"] for n in got] == ["big", "b"]
    assert len(got[0]["properties"]["text"]) == 800


@pytest.mark.parametrize("limit", [1, 3, 5, 100])
@pytest.mark.parametrize("budget", [None, 1, 120, 300, 100000])
def test_sql_and_fallback_paths_choose_the_same_window(sql_store, limit, budget) -> None:
    for i, chars in enumerate([40, 900, 60, 40, 500, 60, 30]):
        _put(sql_store, f"n{i}", chars)
    expected = trim_to_budget(sql_store.list_nodes(limit=limit), max_chars=budget)
    got = sql_store.list_nodes_within_budget(limit=limit, max_chars=budget)
    assert [(n["space"], n["node_id"]) for n in got] == [
        (n["space"], n["node_id"]) for n in expected
    ]


def test_sql_budget_boundary_matches_the_loop(sql_store) -> None:
    for i in range(4):
        _put(sql_store, f"n{i}", 50)
    one = node_size(sql_store.list_nodes(limit=1)[0])
    for budget, kept in ((2 * one, 2), (2 * one + 1, 3), (1, 1)):
        via_sql = sql_store.list_nodes_within_budget(limit=10, max_chars=budget)
        via_loop = trim_to_budget(sql_store.list_nodes(limit=10), max_chars=budget)
        assert len(via_sql) == len(via_loop) == kept


def test_worker_falls_back_to_python_cut_when_the_budgeted_query_fails(
    sql_store, monkeypatch
) -> None:
    for i, chars in enumerate([40, 4000, 40]):
        _put(sql_store, f"n{i}", chars)

    def broken(*args, **kwargs):
        raise RuntimeError("no such function: SUM OVER")

    monkeypatch.setattr(sql_store, "list_nodes_within_budget", broken)
    monkeypatch.setenv("OPENCRAB_BM25_CHAR_BUDGET", "500")
    hybrid = _hybrid(sql_store)
    try:
        hybrid._bm25.ensure_built(sql_store)
        assert hybrid._bm25.state.indexed_rows == 2
    finally:
        hybrid.shutdown_bm25()


def test_worker_marks_a_budget_cut_window_as_partial_coverage(sql_store, monkeypatch) -> None:
    for i, chars in enumerate([40, 4000, 40]):
        _put(sql_store, f"n{i}", chars)
    monkeypatch.setenv("OPENCRAB_BM25_CHAR_BUDGET", "500")
    hybrid = _hybrid(sql_store)
    try:
        hybrid._bm25.ensure_built(sql_store)
        state = hybrid._bm25.state
        assert state.indexed_rows == 2
        assert state.total_rows == 3
        warnings = hybrid._coverage_warnings(state, ["A"])
        assert any("partial rows" in w for w in warnings)
    finally:
        hybrid.shutdown_bm25()


class _LegacyStore:
    """No native fingerprint and no budget method (the MongoStore shape)."""

    available = True

    def __init__(self, nodes: list[dict]) -> None:
        self._nodes = nodes

    def list_nodes(self, space=None, limit: int = 100) -> list[dict]:
        return list(self._nodes[:limit])


def test_legacy_fingerprint_comes_from_the_untrimmed_list(monkeypatch) -> None:
    nodes = [
        {**_sized("big", 5000), "updated_at": "2026-01-02T00:00:00"},
        _node("a", updated_at="2026-01-01T00:00:00"),
    ]
    monkeypatch.setenv("OPENCRAB_BM25_CHAR_BUDGET", "500")
    store = _LegacyStore(nodes)
    hybrid = _hybrid(store)
    try:
        hybrid._bm25.ensure_built(store)
        state = hybrid._bm25.state
        assert state.indexed_rows == 1
        assert state.probe_fingerprint == hybrid._bm25_probe_fingerprint()
    finally:
        hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# 3b. Head-only tokenising of long node text
# ---------------------------------------------------------------------------


def _long_node(node_id: str = "long", pack_id: str = "A") -> dict:
    return {
        "node_id": node_id,
        "space": "claim",
        "node_type": "Claim",
        "properties": {
            "pack_id": pack_id,
            "text": "headword " + "filler " * 200 + "tailword",
        },
    }


def test_only_the_head_of_a_long_text_is_searchable() -> None:
    nodes = [_long_node()]
    cut = len(_node_text(nodes[0])) // 2
    index = BM25Index.build(nodes, max_text_chars=cut)
    assert [h["node_id"] for h in index.search("headword", pack_ids=["A"])] == ["long"]
    assert index.search("tailword", pack_ids=["A"]) == []


def test_without_a_cut_the_whole_text_is_searchable() -> None:
    index = BM25Index.build([_long_node()])
    assert [h["node_id"] for h in index.search("tailword", pack_ids=["A"])] == ["long"]


def test_head_only_tokenising_keeps_the_node_objects_and_results() -> None:
    nodes = [_long_node(), _node("short", text="headword")]
    original = copy.deepcopy(nodes)
    objects = list(nodes)
    properties = [n["properties"] for n in nodes]
    cut = len(_node_text(nodes[0])) // 2
    index = BM25Index.build(nodes, max_text_chars=cut)
    assert index._docs is nodes
    assert all(a is b for a, b in zip(nodes, objects, strict=True))
    assert all(n["properties"] is p for n, p in zip(nodes, properties, strict=True))
    assert nodes == original
    long_hit = next(h for h in index.search("headword", pack_ids=["A"], limit=5) if h["node_id"] == "long")
    assert long_hit["text"] == _node_text(nodes[0])
    assert long_hit["properties"] is nodes[0]["properties"]
    assert index._pack_buckets == {"A": [0, 1]}


def test_head_only_scores_match_an_independent_reference() -> None:
    nodes = [_long_node("a"), _long_node("b"), _node("c", text="headword filler")]
    cut = len(_node_text(nodes[0])) // 3
    truncated = [
        {**n, "properties": {**n["properties"]}} for n in nodes
    ]
    index = BM25Index.build(nodes, max_text_chars=cut)
    got = {h["node_id"]: h["score"] for h in index.search("headword filler", pack_ids=["A"], limit=9)}
    assert got == _reference_scores(truncated, "headword filler", max_text_chars=cut)
    assert got


def test_the_worker_passes_the_node_text_cut_to_the_index(sql_store, monkeypatch) -> None:
    sql_store.upsert_node_doc(
        "claim", "Claim", "long",
        {"pack_id": "A", "text": "headword " + "filler " * 200 + "tailword"},
    )
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "300")
    hybrid = _hybrid(sql_store)
    try:
        hybrid._bm25.ensure_built(sql_store)
        state = hybrid._bm25.state
        assert state.indexed_rows == 1 and state.covered_pack_ids == frozenset({"A"})
        assert [h["node_id"] for h in state.index.search("headword", pack_ids=["A"])] == ["long"]
        assert state.index.search("tailword", pack_ids=["A"]) == []
    finally:
        hybrid.shutdown_bm25()


def test_a_zero_node_text_cut_turns_the_cut_off(sql_store, monkeypatch) -> None:
    sql_store.upsert_node_doc(
        "claim", "Claim", "long",
        {"pack_id": "A", "text": "headword " + "filler " * 200 + "tailword"},
    )
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "0")
    hybrid = _hybrid(sql_store)
    try:
        hybrid._bm25.ensure_built(sql_store)
        assert hybrid._bm25.state.index.search("tailword", pack_ids=["A"])
    finally:
        hybrid.shutdown_bm25()


# ---------------------------------------------------------------------------
# 4. Rebuild rest (cause 4)
# ---------------------------------------------------------------------------

_DELAY = 0.15
_REST = 2 * _DELAY
_TOL = 0.04


def _starts_ends(store: _TimedStore) -> tuple[list[float], list[float]]:
    done = [b for b in store.builds if b[1] is not None]
    return [b[0] for b in done], [b[1] for b in done]


def test_rest_follows_a_first_build_the_worker_runs() -> None:
    store = _TimedStore(delay=_DELAY)
    hybrid = _hybrid(store)

    def write_during_first_build(idx: int) -> None:
        if idx == 0:
            store.write()
            hybrid.invalidate_bm25_cache()

    store.hook = write_during_first_build
    try:
        hybrid.invalidate_bm25_cache()  # the worker's first wake builds
        assert _wait_until(lambda: len(_starts_ends(store)[0]) >= 2, timeout=5)
        starts, ends = _starts_ends(store)
        assert starts[1] - ends[0] >= _REST - _TOL
    finally:
        hybrid.shutdown_bm25()


def test_rest_follows_a_first_build_a_search_thread_finishes() -> None:
    store = _TimedStore(delay=_DELAY)
    hybrid = _hybrid(store, debounce=0.3)  # the worker is still debouncing

    def write_during_first_build(idx: int) -> None:
        if idx == 0:
            store.write()
            hybrid.invalidate_bm25_cache()

    store.hook = write_during_first_build
    try:
        hybrid.invalidate_bm25_cache()  # wakes the worker; it debounces 0.3 s
        time.sleep(0.05)
        hybrid._bm25.ensure_built(store)  # the search thread builds first
        assert _wait_until(lambda: len(_starts_ends(store)[0]) >= 2, timeout=5)
        starts, ends = _starts_ends(store)
        assert starts[1] - ends[0] >= _REST - _TOL
    finally:
        hybrid.shutdown_bm25()


def test_rest_follows_a_discarded_build_and_the_last_write_lands() -> None:
    store = _TimedStore(delay=_DELAY)
    hybrid = _hybrid(store)
    worker = hybrid._bm25

    def write_during_build_one(idx: int) -> None:
        if idx == 1:
            store.write()
            hybrid.invalidate_bm25_cache()

    try:
        worker.ensure_built(store)  # build 0
        store.write()
        store.hook = write_during_build_one
        hybrid.invalidate_bm25_cache()  # build 1 (discarded), then build 2
        assert _wait_until(lambda: len(_starts_ends(store)[0]) >= 3, timeout=8)
        starts, ends = _starts_ends(store)
        assert starts[1] - ends[0] >= _REST - _TOL
        assert starts[2] - ends[1] >= _REST - _TOL
        last = f"w{store.version}"
        assert _wait_until(
            lambda: any(h["node_id"] == f"n{store.version}"
                        for h in worker.cache.search(last, pack_ids=["A"], limit=5))
        )
    finally:
        hybrid.shutdown_bm25()


def test_mark_stale_does_not_move_the_rest_deadline() -> None:
    store = _TimedStore()
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    try:
        worker.ensure_built(store)
        deadline = worker._next_build_at
        assert deadline > 0
        for _ in range(5):
            worker.mark_stale()
        assert worker._next_build_at == deadline
    finally:
        hybrid.shutdown_bm25()


class _RecordingLock:
    """Wraps a lock and records the thread that tries to take it (see test_bm25_cold_build_race)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.attempts: list[int] = []

    def acquire(self, *args, **kwargs):
        self.attempts.append(threading.get_ident())
        return self._inner.acquire(*args, **kwargs)

    def release(self) -> None:
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def test_rest_holds_when_the_search_thread_publishes_while_the_worker_waits_on_the_cold_lock() -> None:
    store = _TimedStore()
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    lock = _RecordingLock(worker._cold_build_lock)
    worker._cold_build_lock = lock  # type: ignore[assignment]
    me = threading.get_ident()
    try:
        lock.acquire()  # this thread plays the search thread that is building
        hybrid.invalidate_bm25_cache()  # the worker wakes and blocks in ensure_built
        assert _wait_until(lambda: any(t != me for t in lock.attempts)), (
            "the worker never reached the cold-build lock"
        )
        observation = worker._observe(store)  # the search thread's build (builds[0])
        candidate = worker._make_state(observation)
        store.write()
        hybrid.invalidate_bm25_cache()  # a write lands during that build
        deadline = time.monotonic() + 0.4
        with worker._lock:
            worker.state = candidate
            worker.dirty = False
            worker._next_build_at = deadline
        lock.release()
        assert _wait_until(lambda: len(store.builds) >= 2, timeout=5)
        assert store.builds[1][0] >= deadline - 0.02
    finally:
        hybrid.shutdown_bm25()


def test_rest_follows_a_failed_background_rebuild() -> None:
    store = _TimedStore(delay=_DELAY)
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    store.fail_at = {1}
    try:
        worker.ensure_built(store)  # build 0 publishes a state
        store.write()
        hybrid.invalidate_bm25_cache()  # build 1 raises after the delay
        assert _wait_until(lambda: len(store.builds) >= 2 and store.builds[1][1] is not None)
        failed_at = store.builds[1][1]
        assert _wait_until(lambda: worker._next_build_at > failed_at)
        store.write()
        hybrid.invalidate_bm25_cache()  # a later write wakes the worker again
        assert _wait_until(lambda: len(store.builds) >= 3, timeout=5)
        assert store.builds[2][0] - failed_at >= _REST - _TOL
    finally:
        hybrid.shutdown_bm25()


def test_shutdown_interrupts_a_long_rest() -> None:
    store = _TimedStore()
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    worker.ensure_built(store)
    with worker._lock:
        worker._next_build_at = time.monotonic() + 60
    store.write()
    hybrid.invalidate_bm25_cache()
    assert _wait_until(lambda: not worker._wake.is_set())  # the worker took the wake and rests
    started = time.monotonic()
    hybrid.shutdown_bm25(timeout=5.0)
    assert time.monotonic() - started < 2.0
    assert worker.thread is None or not worker.thread.is_alive()


def test_round_finished_line_follows_the_release_and_the_memory_return(monkeypatch, caplog) -> None:
    import logging

    from opencrab.ontology import query as query_module

    log = logging.getLogger("opencrab.ontology.query")
    released_when_returned: list[bool] = []
    holder: list[weakref.ReferenceType] = []

    def returning_memory() -> None:
        gc.collect()
        released_when_returned.append(holder[0]() is None)
        log.debug("TRIM")

    monkeypatch.setattr(query_module, "_return_freed_memory", returning_memory)
    store = _TimedStore()
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    try:
        worker.ensure_built(store)
        first = weakref.ref(worker.state.index)
        holder.append(first)
        generation = worker.state.generation
        store.write()
        with caplog.at_level(logging.DEBUG, logger="opencrab.ontology.query"):
            hybrid.invalidate_bm25_cache()
            assert _wait_until(
                lambda: any(m == "BM25 worker round finished" for m in caplog.messages)
            )
        assert worker.state.generation > generation
        messages = [m for m in caplog.messages if m in ("TRIM", "BM25 worker round finished")]
        assert messages == ["TRIM", "BM25 worker round finished"]
        # the memory return runs only after the superseded index is released
        assert released_when_returned == [True]
        # the superseded index is already gone when the line appears
        gc.collect()
        assert first() is None
    finally:
        hybrid.shutdown_bm25()


def test_a_failed_round_leaves_no_round_finished_line(caplog) -> None:
    import logging

    store = _TimedStore()
    hybrid = _hybrid(store)
    worker = hybrid._bm25
    store.fail_at = {1}
    try:
        worker.ensure_built(store)
        store.write()
        with caplog.at_level(logging.DEBUG, logger="opencrab.ontology.query"):
            hybrid.invalidate_bm25_cache()
            assert _wait_until(
                lambda: any("background rebuild failed" in m for m in caplog.messages)
            )
            time.sleep(0.3)  # the round ends right after the warning
        assert not any(m == "BM25 worker round finished" for m in caplog.messages)
    finally:
        hybrid.shutdown_bm25()
