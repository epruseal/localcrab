"""#437: one-MATCH per-pack FTS probe of the content fallback.

The control group is the old loop: ``keyword_search`` called once per pack.
``keyword_search_per_pack`` and the ``_choose_by_content`` wiring must return
the same hits and the same selection, ties included, while the doc-store
call count stays independent of the candidate pack count.
"""
from __future__ import annotations

import sqlite3

import pytest

from opencrab.ontology.pack_registry import PER_PACK_PROBE_LIMIT, PackInfo, _choose_by_content
from opencrab.ontology.query import HybridQuery
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore


@pytest.fixture()
def store(tmp_path):
    s = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert s.available
    if not s.supports_keyword:
        pytest.skip("FTS5 unavailable in this SQLite build")
    return s


def _seed(store: LocalSQLDocStore) -> list[str]:
    """Big pack over the probe limit, small pack, tie group, other space,
    numeric pack_id, unpackaged row."""
    for i in range(PER_PACK_PROBE_LIMIT + 10):
        store.upsert_source(
            f"big-{i:03d}",
            "common " * (1 + i % 3) + f"filler{i} word",
            {"pack_id": "big", "space": "s1", "node_id": f"nb{i}"},
        )
    store.upsert_source("small-0", "common rareword", {"pack_id": "small", "space": "s1"})
    for i in (3, 5, 0, 4, 1, 2):  # identical text => identical bm25 rank; scan order != id order
        store.upsert_source(f"tie-{i}", "common tied text", {"pack_id": "tie", "space": "s1"})
    store.upsert_source("os-0", "common rareword", {"pack_id": "other-space", "space": "s2"})
    store.upsert_source("num-0", "common rareword", {"pack_id": 7, "space": "s1"})
    store.upsert_source("nopack-0", "common rareword", {"space": "s1"})
    return ["big", "small", "tie", "other-space", "empty", "7"]


def _loop(store, query, pack_ids, limit, spaces=None):
    out = {}
    for pid in pack_ids:
        hits = store.keyword_search(query, pack_ids=[pid], limit=limit, spaces=spaces)
        if hits:
            out[pid] = hits
    return out


@pytest.mark.parametrize("spaces", [None, ["s1"], ["s2"]])
@pytest.mark.parametrize("query", ["common", "common rareword", "rareword tied", "zzz"])
def test_per_pack_equals_per_pack_loop(store, query, spaces):
    packs = _seed(store)
    for limit in (1, 5, PER_PACK_PROBE_LIMIT):
        assert store.keyword_search_per_pack(
            query, pack_ids=packs, per_pack_limit=limit, spaces=spaces
        ) == _loop(store, query, packs, limit, spaces)


def test_tie_group_matches_head_sql(store, tmp_path):
    """Control group independent of keyword_search: the HEAD query text with
    equal bm25 ranks cut at the limit."""
    packs = _seed(store)
    con = sqlite3.connect(str(tmp_path / "doc.db"))
    for limit in (1, 2, 5):
        rows = con.execute(
            "SELECT f.source_id FROM doc_sources_fts f JOIN doc_sources s "
            "ON s.source_id = f.source_id WHERE doc_sources_fts MATCH ? "
            "AND json_extract(s.metadata,'$.pack_id') = 'tie' "
            "ORDER BY bm25(doc_sources_fts) LIMIT ?",
            ('"common" OR "tied" OR "text"', limit),
        ).fetchall()
        got = store.keyword_search_per_pack(
            "common tied text", pack_ids=packs, per_pack_limit=limit
        )
        assert [h["source_id"] for h in got["tie"]] == [r[0] for r in rows]
    con.close()


def test_small_pack_hit_not_shadowed_by_big_pack(store):
    packs = _seed(store)
    got = store.keyword_search_per_pack("common rareword", pack_ids=packs, per_pack_limit=3)
    assert [h["source_id"] for h in got["small"]] == ["small-0"]
    assert len(got["big"]) == 3


def test_per_pack_guards(store):
    _seed(store)
    f = store.keyword_search_per_pack
    assert f("common", pack_ids=[], per_pack_limit=5) == {}
    assert f("common", pack_ids=["big"], per_pack_limit=0) == {}
    assert f("!!! ???", pack_ids=["big"], per_pack_limit=5) == {}


def test_malformed_metadata_row_does_not_crash(store, tmp_path):
    _seed(store)
    con = sqlite3.connect(str(tmp_path / "doc.db"))
    con.execute(
        "INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
        "VALUES ('bad', 'common rareword', '{not json', '2026-01-01')"
    )
    con.execute("INSERT INTO doc_sources_fts(source_id, text) VALUES ('bad', 'common rareword')")
    con.commit()
    con.close()
    packs = ["big", "small"]
    assert store.keyword_search_per_pack(
        "rareword", pack_ids=packs, per_pack_limit=5
    ) == _loop(store, "rareword", packs, 5)


class _CountingStore:
    """Delegates to a real store and counts the FTS calls."""

    supports_keyword = True

    def __init__(self, inner, *, per_pack: bool = True):
        self._inner = inner
        self.keyword_calls = 0
        self.per_pack_calls = 0
        if per_pack:
            self.keyword_search_per_pack = self._per_pack

    def keyword_search(self, *a, **k):
        self.keyword_calls += 1
        return self._inner.keyword_search(*a, **k)

    def _per_pack(self, *a, **k):
        self.per_pack_calls += 1
        return self._inner.keyword_search_per_pack(*a, **k)


class _NoBm25(HybridQuery):
    def __init__(self, ds):
        super().__init__(None, None)
        self._doc_store = ds

    def _bm25_search(self, question, spaces, limit, *, pack_ids):
        return []


def _registry(n_extra: int) -> list[PackInfo]:
    ids = ["big", "small", "tie", "other-space", "empty", "7"]
    ids += [f"filler-pack-{i}" for i in range(n_extra)]
    return [PackInfo(pack_id=i, title="", description="") for i in ids]


@pytest.mark.parametrize("query", ["rareword", "common", "tied", "common tied text"])
@pytest.mark.parametrize("spaces", [None, ["s1"]])
def test_choose_by_content_same_as_loop(store, query, spaces):
    _seed(store)
    reg = _registry(20)
    fast = _CountingStore(store)
    slow = _CountingStore(store, per_pack=False)
    a = _choose_by_content(query, reg, _NoBm25(fast), spaces)
    b = _choose_by_content(query, reg, _NoBm25(slow), spaces)
    assert a == b
    assert fast.keyword_calls == 0 and fast.per_pack_calls == 1
    assert slow.keyword_calls == len(reg)


def test_fts_call_count_independent_of_pack_count(store):
    _seed(store)
    calls = []
    for extra in (0, 50, 200):
        ds = _CountingStore(store)
        _choose_by_content("rareword", _registry(extra), _NoBm25(ds), None)
        calls.append(ds.per_pack_calls + ds.keyword_calls)
    assert len(set(calls)) == 1


def test_per_pack_failure_falls_back_to_loop(store):
    _seed(store)

    class _Boom(_CountingStore):
        def _per_pack(self, *a, **k):
            raise RuntimeError("boom")

    ds = _Boom(store)
    reg = _registry(3)
    got = _choose_by_content("rareword", reg, _NoBm25(ds), None)
    want = _choose_by_content(
        "rareword", reg, _NoBm25(_CountingStore(store, per_pack=False)), None
    )
    assert got == want
    assert ds.keyword_calls == len(reg)


def test_store_without_capability_uses_loop(store):
    _seed(store)
    ds = _CountingStore(store, per_pack=False)
    reg = _registry(2)
    _choose_by_content("rareword", reg, _NoBm25(ds), None)
    assert ds.keyword_calls == len(reg)
