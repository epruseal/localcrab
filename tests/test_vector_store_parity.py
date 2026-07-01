"""Vector-store parity tests — ChromaStore vs SqliteVecStore.

green→green characterization: the same suite runs against both backends
(parametrized) and asserts identical behaviour, plus a direct cross-backend
equivalence test. Uses tmp_path only (no real data) and a deterministic MockEF
(no network). See docs/pgvector-migration-plan.md §11.
"""

from __future__ import annotations

import pytest

from _vec_helpers import MockEF, build_vector_store

BACKENDS = ["chroma", "sqlite-vec"]

# node_id, text, metadata (pack_id + space present so where-filters are testable)
CORPUS = [
    ("n1", "apple fruit red sweet", {"pack_id": "A", "space": "s1"}),
    ("n2", "banana fruit yellow soft", {"pack_id": "A", "space": "s1"}),
    ("n3", "car vehicle fast road", {"pack_id": "B", "space": "s2"}),
    ("n4", "train vehicle rail steel", {"pack_id": "B", "space": "s1"}),
    ("n5", "python snake reptile", {"pack_id": "C", "space": "s2"}),
    ("n6", "java coffee bean code", {"pack_id": "C", "space": "s1"}),
]


def _load(store):
    texts = [t for _, t, _ in CORPUS]
    ids = [i for i, _, _ in CORPUS]
    metas = [m for _, _, m in CORPUS]
    store.upsert_texts(texts=texts, metadatas=metas, ids=ids)


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    s = build_vector_store(request.param, tmp_path)
    assert s.available
    yield s
    if hasattr(s, "close"):
        s.close()


# ---------------------------------------------------------------------------
# Per-backend contract (both backends must satisfy identically)
# ---------------------------------------------------------------------------


class TestVectorStoreContract:
    def test_upsert_and_count(self, store):
        _load(store)
        assert store.count() == len(CORPUS)

    def test_upsert_idempotent(self, store):
        store.upsert_texts(texts=["hello"], metadatas=[{"pack_id": "A"}], ids=["x"])
        store.upsert_texts(texts=["hello"], metadatas=[{"pack_id": "A"}], ids=["x"])
        assert store.count() == 1
        # same id, new content → document updated, still one row
        store.upsert_texts(texts=["world"], metadatas=[{"pack_id": "A"}], ids=["x"])
        assert store.count() == 1
        assert store.get_by_id("x")["document"] == "world"

    def test_get_by_id(self, store):
        _load(store)
        hit = store.get_by_id("n3")
        assert hit is not None
        assert hit["id"] == "n3"
        assert hit["document"] == "car vehicle fast road"
        assert hit["metadata"]["pack_id"] == "B"
        assert store.get_by_id("nonexistent") is None

    def test_delete(self, store):
        _load(store)
        store.delete(["n1", "n2"])
        assert store.count() == len(CORPUS) - 2
        assert store.get_by_id("n1") is None

    def test_reset(self, store):
        _load(store)
        store.reset_collection()
        assert store.count() == 0
        # store still usable after reset
        store.upsert_texts(texts=["again"], metadatas=[{"pack_id": "Z"}], ids=["z"])
        assert store.count() == 1

    def test_empty_inputs(self, store):
        # sqlite-vec returns [] gracefully; Chroma rejects empty batches with
        # ValueError. Either way, no rows are created and no corruption occurs.
        for call in (lambda: store.upsert_texts(texts=[]), lambda: store.add_texts(texts=[])):
            try:
                assert call() == []
            except ValueError:
                pass
        try:
            store.delete([])  # sqlite-vec: no-op; Chroma: rejects empty
        except ValueError:
            pass
        assert store.count() == 0

    def test_query_topk_ordering(self, store):
        _load(store)
        hits = store.query("fruit sweet banana", n_results=3)
        assert len(hits) == 3
        # distances ascending (nearest first)
        dists = [h["distance"] for h in hits]
        assert dists == sorted(dists)
        # keys present
        for h in hits:
            assert set(h.keys()) == {"id", "document", "metadata", "distance"}

    def test_query_n_results_cap(self, store):
        _load(store)
        hits = store.query("anything", n_results=2)
        assert len(hits) == 2

    def test_where_single_pack(self, store):
        _load(store)
        hits = store.query("fruit", n_results=10, where={"pack_id": "A"})
        assert {h["id"] for h in hits} == {"n1", "n2"}
        assert all(h["metadata"]["pack_id"] == "A" for h in hits)

    def test_where_in(self, store):
        _load(store)
        hits = store.query(
            "anything", n_results=10, where={"pack_id": {"$in": ["A", "C"]}}
        )
        assert {h["id"] for h in hits} == {"n1", "n2", "n5", "n6"}

    def test_where_and(self, store):
        _load(store)
        where = {"$and": [{"space": "s1"}, {"pack_id": {"$in": ["A", "B", "C"]}}]}
        hits = store.query("anything", n_results=10, where=where)
        assert {h["id"] for h in hits} == {"n1", "n2", "n4", "n6"}

    def test_where_missing_key_no_match(self, store):
        _load(store)
        hits = store.query("anything", n_results=10, where={"pack_id": "DOES_NOT_EXIST"})
        assert hits == []

    def test_distance_to_score_range(self, store):
        _load(store)
        hits = store.query("fruit", n_results=5)
        for h in hits:
            score = max(0.0, 1.0 - float(h["distance"]))
            assert 0.0 <= score <= 1.0

    def test_unavailable_raises(self, store):
        store._available = False
        with pytest.raises(RuntimeError):
            store.upsert_texts(texts=["x"], metadatas=[{"pack_id": "A"}], ids=["x"])
        # count() must NOT raise when unavailable (returns 0)
        assert store.count() == 0


# ---------------------------------------------------------------------------
# Cross-backend equivalence (Chroma vs sqlite-vec, same EF/data → same results)
# ---------------------------------------------------------------------------


def test_cross_backend_parity(tmp_path):
    chroma = build_vector_store("chroma", tmp_path / "c")
    sv = build_vector_store("sqlite-vec", tmp_path / "s")
    _load(chroma)
    _load(sv)

    queries = ["fruit sweet", "vehicle fast", "code snake", "coffee bean"]

    # No-where: identical ordering + close distances.
    for q in queries:
        c_hits = chroma.query(q, n_results=len(CORPUS))
        s_hits = sv.query(q, n_results=len(CORPUS))
        assert [h["id"] for h in c_hits] == [h["id"] for h in s_hits], (
            f"ordering mismatch for {q!r}"
        )
        c_dist = {h["id"]: h["distance"] for h in c_hits}
        s_dist = {h["id"]: h["distance"] for h in s_hits}
        for nid in c_dist:
            assert abs(c_dist[nid] - s_dist[nid]) < 1e-3, (
                f"distance mismatch {nid} for {q!r}: {c_dist[nid]} vs {s_dist[nid]}"
            )

    # With where: identical result sets.
    wheres = [
        {"pack_id": "A"},
        {"pack_id": {"$in": ["A", "C"]}},
        {"$and": [{"space": "s1"}, {"pack_id": {"$in": ["A", "B", "C"]}}]},
    ]
    for w in wheres:
        c_ids = {h["id"] for h in chroma.query("anything", n_results=10, where=w)}
        s_ids = {h["id"] for h in sv.query("anything", n_results=10, where=w)}
        assert c_ids == s_ids, f"where set mismatch for {w}: {c_ids} vs {s_ids}"

    if hasattr(sv, "close"):
        sv.close()


# ---------------------------------------------------------------------------
# Scale > vec0 k-limit (4096): the store must NOT crash and pushdown stays exact
# (regression guard for the k=4096 cap; small corpora never exercise this).
# ---------------------------------------------------------------------------


def _bruteforce_topk(ef, corpus, query, k, packs=None):
    """Exact top-k node_ids by cosine (MockEF vectors are unit-norm → cos=dot)."""
    qv = ef([query])[0]
    scored = []
    for _id, text, meta in corpus:
        if packs is not None and meta.get("pack_id") not in packs:
            continue
        v = ef([text])[0]
        scored.append((sum(a * b for a, b in zip(qv, v)), _id))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored[:k]]


def _bruteforce_topk_filter(ef, corpus, query, k, predicate):
    """Exact top-k node_ids by cosine among corpus items matching `predicate`."""
    qv = ef([query])[0]
    scored = []
    for _id, text, meta in corpus:
        if not predicate(meta):
            continue
        v = ef([text])[0]
        scored.append((sum(a * b for a, b in zip(qv, v)), _id))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored[:k]]


def test_scale_over_4096_no_crash_and_pushdown_exact(tmp_path):
    from _vec_helpers import MockEF
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    ef = MockEF(32)
    store = SqliteVecStore(
        db_path=str(tmp_path / "vbig.db"),
        embedding_function=ef,
        dim=32,
        collection_name="vbig",
    )
    N = 4200  # > vec0's k cap of 4096
    corpus = [
        (f"n{i}", f"text number {i} content", {"pack_id": f"p{i % 3}",
                                               "space": "s1" if i % 4 == 0 else "s2"})
        for i in range(N)
    ]
    store.add_texts(
        texts=[t for _, t, _ in corpus],
        metadatas=[m for _, _, m in corpus],
        ids=[i for i, _, _ in corpus],
    )
    assert store.count() == N
    q = "text number 7 content"

    # 1. Every filter path must NOT raise (the C1 k>4096 crash) and stay bounded.
    for where in [
        None,
        {"pack_id": "p0"},
        {"pack_id": {"$in": ["p0", "p1"]}},
        {"$and": [{"space": "s1"}, {"pack_id": "p0"}]},
        {"$and": [{"space": "s1"}, {"pack_id": {"$in": ["p0", "p1"]}}]},
        {"space": "s1"},
    ]:
        hits = store.query(q, n_results=10, where=where)
        assert len(hits) <= 10

    # 1b. Force fetch_k to vec0's 4096 cap so the clamp is exercised (removing the
    #     clamp makes these raise OperationalError):
    #       n_results=5000, no where  → fetch_k=5000 → clamp 4096
    #       residual (space) filter    → fetch_k = _VEC0_K_MAX (4096)
    big = store.query(q, n_results=5000, where=None)
    assert len(big) <= 5000
    res = store.query(q, n_results=500, where={"space": "s1"})
    assert all(h["metadata"].get("space") == "s1" for h in res)  # residual filter honored
    store.query(q, n_results=600, where={"$and": [{"space": "s1"}, {"pack_id": "p0"}]})

    # 1c. duplicate pack in $in must not yield duplicate result rows
    dup = store.query(q, n_results=10, where={"pack_id": {"$in": ["p0", "p0"]}})
    assert len({h["id"] for h in dup}) == len(dup)

    # 2. pack isolation at scale
    hits = store.query(q, n_results=10, where={"pack_id": "p1"})
    assert hits and all(h["metadata"]["pack_id"] == "p1" for h in hits)

    # 3. pushdown exactness vs brute-force ground truth (single pack + $in)
    assert [h["id"] for h in store.query(q, n_results=10, where={"pack_id": "p0"})] \
        == _bruteforce_topk(ef, corpus, q, 10, packs={"p0"})
    assert [h["id"] for h in store.query(
        q, n_results=10, where={"pack_id": {"$in": ["p0", "p1"]}})] \
        == _bruteforce_topk(ef, corpus, q, 10, packs={"p0", "p1"})

    store.close()


def test_residual_filter_recall_exact_within_cap(tmp_path):
    """Residual (non-pack) filter recall is EXACT when the corpus fits within
    vec0's k cap. Regression guard for C1: the residual post-filter must scan up
    to _VEC0_K_MAX and must not silently drop matches that rank beyond a small
    inflate (the pre-fix code fetched only n_results*12 and returned []/short)."""
    from _vec_helpers import MockEF
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    ef = MockEF(32)
    store = SqliteVecStore(
        db_path=str(tmp_path / "vres.db"), embedding_function=ef, dim=32,
        collection_name="vres",
    )
    N = 2000  # < 4096 → top-4096 scan covers the whole corpus → exact recall
    corpus = [
        (f"n{i}", f"doc {i} body text", {"pack_id": f"p{i % 3}",
                                         "space": "s1" if i % 7 == 0 else "s2"})
        for i in range(N)
    ]
    store.add_texts(
        texts=[t for _, t, _ in corpus], metadatas=[m for _, _, m in corpus],
        ids=[i for i, _, _ in corpus],
    )
    q = "doc 13 body text"
    # residual-only (space, not pushable) — matches must be recalled exactly
    got = [h["id"] for h in store.query(q, n_results=10, where={"space": "s1"})]
    exact = _bruteforce_topk_filter(ef, corpus, q, 10, lambda m: m.get("space") == "s1")
    assert got == exact
    assert all(h["metadata"]["space"] == "s1"
               for h in store.query(q, n_results=10, where={"space": "s1"}))
    store.close()


def test_add_texts_duplicate_id_raises(tmp_path):
    """Documented divergence from Chroma: vec0 has no INSERT OR IGNORE, so
    add_texts raises on a duplicate primary key (Chroma warns and skips)."""
    import sqlite3

    from _vec_helpers import MockEF
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    store = SqliteVecStore(
        db_path=str(tmp_path / "vdup.db"), embedding_function=MockEF(16), dim=16,
        collection_name="vdup",
    )
    store.add_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
    with pytest.raises(sqlite3.Error):
        store.add_texts(texts=["b"], metadatas=[{"pack_id": "p"}], ids=["x"])
    assert store.count() == 1
    store.close()
