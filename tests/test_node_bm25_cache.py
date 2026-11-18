"""#667: the in-process cache of term statistics and posting arrays of the node BM25 search.

Design: docs/issue-667-bm25-posting-cache.md (section 8 lists these tests). The pruned path
runs on small corpora only when ``FULL_SCAN_CF_MAX`` is lowered, which also lowers the cache
threshold, so each integration test lowers it.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3

import numpy as np
import pytest

from opencrab.stores import _node_bm25_cache as cmod
from opencrab.stores import _node_bm25_prune as prune
from opencrab.stores import _node_bm25_sqlite as nbm
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from tests.test_node_bm25_prune import (
    _QUESTIONS,
    FALLBACK,
    _full,
    _oracle,
    _search,
    _shape,
    _skewed,
    _store,
    _zipf_corpus,
)

pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 43, 0),
    reason="FTS5 contentless_delete needs SQLite 3.43 or newer",
)


@pytest.fixture(autouse=True)
def _no_latency_warning(monkeypatch):
    """Keep wall-clock time and the caller's cache switch out of every test of this file."""
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", 1e9)
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", 1e9)
    monkeypatch.delenv(cmod.SWITCH, raising=False)


@pytest.fixture
def lowered(monkeypatch):
    """Force the pruned path and let every term count as heavy. Records the stats of each select."""
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", -1)
    monkeypatch.delenv("OPENCRAB_BM25_PRUNE", raising=False)
    monkeypatch.delenv(cmod.SWITCH, raising=False)
    calls: list[dict] = []
    real = prune.select

    def select(*a, **k):
        k["stats"] = {}
        calls.append(k["stats"])
        return real(*a, **k)

    monkeypatch.setattr(prune, "select", select)
    return calls


# -- PostingCache and Session (fake tokens) ---------------------------------------------


def _arrays(n=3):
    return np.arange(n, dtype=np.int64), np.ones(n, dtype=np.int64)


def test_session_reads_back_what_it_wrote():
    cache = cmod.PostingCache()
    s = cache.open_session("t1", 1)
    assert s is not None
    assert s.stats_get("a") is None and not s.has_postings("a") and s.postings_get("a") is None
    assert cache.counts == {"stats_hits": 0, "postings_hits": 0, "stores": 0}  # misses count nothing
    assert s.stats_put("a", 5, 9) is True
    assert s.postings_put("a", *_arrays()) is True
    assert s.stats_get("a") == (5, 9)
    keys, tf = s.postings_get("a")
    assert keys.tolist() == [0, 1, 2] and tf.tolist() == [1, 1, 1]
    assert s.has_postings("a")
    assert cache.counts == {"stats_hits": 1, "postings_hits": 1, "stores": 2}
    with pytest.raises(ValueError):
        keys[0] = 7


def test_none_token_gives_no_session():
    assert cmod.PostingCache().open_session(None, 1) is None


def test_stored_arrays_are_read_only_and_a_view_is_copied():
    cache = cmod.PostingCache()
    s = cache.open_session("t1", 1)
    keys, tf = _arrays()
    s.postings_put("a", keys, tf)
    assert not keys.flags.writeable  # the cache took the arrays
    big = np.arange(10, dtype=np.int64)
    view = big[2:5]
    s.postings_put("b", view, view)
    big[2] = 99  # a view would follow this change
    assert s.postings_get("b")[0].tolist() == [2, 3, 4]


def test_a_newer_generation_hides_the_entries_of_an_older_session():
    cache = cmod.PostingCache()
    a = cache.open_session("t1", 1)
    a.stats_put("x", 1, 1)
    b = cache.open_session("t2", 2)
    assert b is not None
    assert b.stats_get("x") is None
    assert a.stats_put("y", 2, 2) is False  # the old session cannot write into the new generation
    assert a.postings_put("y", *_arrays()) is False
    assert b.stats_get("y") is None and not b.has_postings("y")
    assert a.stats_get("x") is None and a.stats_get("y") is None and a.postings_get("y") is None
    # The newer generation holds the same terms with other values. The old session must not
    # read them: its snapshot has another version.
    b.stats_put("x", 9, 9)
    b.postings_put("x", *_arrays(5))
    assert b.stats_get("x") == (9, 9) and b.has_postings("x")
    assert a.stats_get("x") is None and a.postings_get("x") is None and not a.has_postings("x")


def test_an_older_snapshot_cannot_replace_a_newer_generation():
    cache = cmod.PostingCache()
    new = cache.open_session("t2", 20)
    new.stats_put("x", 1, 1)
    assert cache.open_session("t1", 10) is None
    assert new.stats_get("x") == (1, 1)
    again = cache.open_session("t2", 5)  # same version: shares the generation
    assert again is not None and again.stats_get("x") == (1, 1)


def test_a_newer_snapshot_replaces_an_older_generation():
    cache = cmod.PostingCache()
    old = cache.open_session("t1", 10)
    old.stats_put("x", 1, 1)
    new = cache.open_session("t2", 20)
    assert new is not None and new.stats_get("x") is None


def test_oldest_posting_leaves_first_and_oversize_is_refused():
    keys, tf = _arrays(100)
    size = keys.nbytes + tf.nbytes
    cache = cmod.PostingCache(max_bytes=2 * size)
    s = cache.open_session("t", 1)
    s.postings_put("a", keys, tf)
    s.postings_put("b", keys, tf)
    assert s.postings_get("a") is not None  # "a" is now the newest
    s.postings_put("c", keys, tf)
    assert s.has_postings("a") and s.has_postings("c") and not s.has_postings("b")
    big = _arrays(300)
    assert s.postings_put("big", *big) is False
    assert not s.has_postings("big") and s.has_postings("a")
    # An entry of exactly ``max_bytes`` fits.
    exact = cmod.PostingCache(max_bytes=size).open_session("t", 1)
    assert exact.postings_put("x", *_arrays(100)) is True and exact.has_postings("x")


def test_a_stored_term_replaces_its_old_arrays_without_counting_them_twice():
    keys, tf = _arrays(100)
    size = keys.nbytes + tf.nbytes
    cache = cmod.PostingCache(max_bytes=2 * size)
    s = cache.open_session("t", 1)
    s.postings_put("a", keys, tf)
    s.postings_put("b", keys.copy(), tf.copy())
    s.postings_put("a", keys.copy(), tf.copy())  # same term, same size: nothing else leaves
    assert s.has_postings("a") and s.has_postings("b")


def test_stats_entries_are_capped():
    cache = cmod.PostingCache(max_stats=2)
    s = cache.open_session("t", 1)
    puts = [s.stats_put(f"t{i}", i, i) for i in range(3)]
    assert puts == [True, True, False]
    assert s.stats_get("t2") is None and s.stats_get("t0") == (0, 0)
    assert s.stats_put("t0", 7, 7) is True  # a term that is already in stays writable
    assert s.stats_get("t0") == (7, 7)


def test_scope_needs_equal_tokens_before_and_after_the_snapshot():
    cache = cmod.PostingCache()
    reads = iter(["v1", "v1"])
    scope = cmod.Scope(cache, lambda: next(reads), "v1", 5)
    assert scope.open() is not None
    assert cmod.Scope(cache, lambda: "v2", "v1", 5).open() is None   # commit between the reads
    assert cmod.Scope(cache, lambda: "v1", None, 5).open() is None   # caller held a transaction
    assert cmod.Scope(cache, lambda: None, "v1", 5).open() is None   # token source failed


# -- TokenSource ---------------------------------------------------------------------


def _file_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn.commit()
    return conn


def test_token_changes_with_commits_of_other_connections_only(tmp_path):
    path = str(tmp_path / "t.db")
    writer = _file_db(path)
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    first = src()
    assert first is not None and src() == first
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    second = src()
    assert second is not None and second != first
    assert src() == second
    conn = src._conn
    src.close()
    assert src() is None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # close() closes the connection itself
    writer.close()


def test_token_is_none_after_the_file_is_replaced(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    assert src() is not None
    other = str(tmp_path / "other.db")
    _file_db(other).close()
    os.replace(other, path)
    assert src() is None
    src.close()


def test_the_source_connects_when_it_is_made(tmp_path):
    """A connection opened at the first read would adopt a file that replaced the original."""
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    calls = []

    def connect():
        calls.append(1)
        return sqlite3.connect(path)

    src = cmod.TokenSource(path, connect)
    assert calls == [1]
    src.close()


def test_a_file_replaced_before_the_first_read_gives_no_token(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    other = str(tmp_path / "other.db")
    _file_db(other).close()
    os.replace(other, path)
    assert src() is None
    src.close()


def test_a_file_replaced_while_the_source_opens_gives_no_token(tmp_path):
    """The identity is read before and after the connection opens. A change between the two
    reads means the connection may hold either file."""
    path = str(tmp_path / "t.db")
    other = str(tmp_path / "other.db")
    _file_db(path).close()
    _file_db(other).close()

    opened = []

    def connect():
        os.replace(other, path)
        opened.append(sqlite3.connect(path))
        return opened[0]

    src = cmod.TokenSource(path, connect)
    assert src() is None
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # the source closed the connection it refused
    src.close()


def test_a_failed_read_of_the_file_gives_no_token(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    assert src() is not None
    os.replace(path, str(tmp_path / "away.db"))  # the stat of the path fails
    assert src() is None
    src.close()


def test_a_failed_read_of_the_version_gives_no_token(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    assert src() is not None
    src._conn.close()  # the next PRAGMA raises sqlite3.ProgrammingError
    assert src() is None
    src.close()


def test_closing_a_source_survives_a_failing_connection_close(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))

    class Failing:
        def close(self):
            raise sqlite3.OperationalError("close failed")

    real, src._conn = src._conn, Failing()
    src.close()
    assert src() is None
    real.close()


def test_a_store_opens_its_token_connection_when_it_is_made(tmp_path):
    store = _store(tmp_path)
    assert store._bm25_token._conn is not None
    store.close()


def test_token_is_none_when_the_file_or_connection_fails(tmp_path):
    path = str(tmp_path / "missing.db")
    assert cmod.TokenSource(path, lambda: sqlite3.connect(path))() is None

    def boom():
        raise sqlite3.OperationalError("no")

    live = str(tmp_path / "t.db")
    _file_db(live).close()
    assert cmod.TokenSource(live, boom)() is None


def test_a_replaced_file_that_comes_back_does_not_switch_the_source_on(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    assert src() is not None
    os.replace(path, str(tmp_path / "a.db"))  # the file is gone: a different identity follows
    other = str(tmp_path / "b.db")
    _file_db(other).close()
    os.replace(other, path)
    assert src() is None
    os.replace(path, other)
    os.replace(str(tmp_path / "a.db"), path)  # the first file is back
    assert src() is None
    src.close()


def test_a_relative_path_is_fixed_at_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _file_db("rel.db").close()
    src = cmod.TokenSource("rel.db", lambda: sqlite3.connect(str(tmp_path / "rel.db")))
    assert src() is not None
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert src() is not None
    src.close()


def test_token_reads_are_numbered_in_read_order(tmp_path):
    path = str(tmp_path / "t.db")
    _file_db(path).close()
    src = cmod.TokenSource(path, lambda: sqlite3.connect(path))
    seqs = [src.read()[1] for _ in range(3)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    src.close()


# -- the cache inside the search ---------------------------------------------------------


def _twin(tmp_path, seed):
    """The same corpus in a store with the cache and in a store without it."""
    on = _store(tmp_path, "on.db")
    os.environ[cmod.SWITCH] = "0"
    try:
        off = _store(tmp_path, "off.db")
    finally:
        del os.environ[cmod.SWITCH]
    for store in (on, off):
        _zipf_corpus(store, random.Random(seed))
    assert on._bm25_cache is not None and off._bm25_cache is None
    return on, off


@pytest.mark.parametrize("seed", [1, 2])
def test_cache_on_equals_cache_off_the_full_scan_and_the_oracle(
        tmp_path, monkeypatch, lowered, caplog, seed):
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "4000")
    on, off = _twin(tmp_path, seed)
    caplog.set_level(logging.WARNING)
    scopes = [["pa"], ["pb", "pc"], ["pa", "pb", "pc", "7"], ["missing"]]
    for q in _QUESTIONS:
        for scope in scopes:
            for spaces in (None, ["concept"]):
                for limit in (1, 3, 10):
                    cold, _ = _search(on, q, scope, spaces, limit)
                    warm, _ = _search(on, q, scope, spaces, limit)
                    plain, _ = _search(off, q, scope, spaces, limit)
                    assert lowered[-1].get("cache_hits", 0) == 0
                    full, _ = _full(on, monkeypatch, q, scope, spaces, limit)
                    want = _oracle(on, q, scope, spaces, limit)
                    assert _shape(cold) == _shape(warm) == _shape(plain) == _shape(full) \
                        == _shape(want), (q, scope, spaces, limit)
            for limit in (1, 3, 50):
                per, _ = _search(on, q, scope, None, limit, per_pack=True)
                per_again, _ = _search(on, q, scope, None, limit, per_pack=True)
                per_off, _ = _search(off, q, scope, None, limit, per_pack=True)
                assert {p: _shape(h) for p, h in per.items()} == \
                    {p: _shape(h) for p, h in per_again.items()} == \
                    {p: _shape(h) for p, h in per_off.items()}, (q, scope, limit)
    assert sum(call.get("cache_hits", 0) for call in lowered) > 0
    assert FALLBACK not in caplog.text


def test_a_write_by_any_connection_starts_a_new_generation(tmp_path, lowered):
    store = _store(tmp_path)
    _skewed(store)
    other = LocalSQLDocStore(str(tmp_path / "doc.db"))
    q = "qxrare alpha beta gamma"
    first, _ = _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_stores"] > 0 and lowered[-1]["cache_hits"] == 0
    second, _ = _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0 and _shape(first) == _shape(second)
    other.upsert_node_doc("evidence", "Note", "rnew",
                          {"text": "qxrare qxrare qxrare qxrare alpha", "pack_id": "pa"})
    after, _ = _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] == 0
    assert "rnew" in [h["node_id"] for h in after]
    assert _shape(after) == _shape(_oracle(store, q, ["pa"], limit=10))
    store.upsert_node_doc("evidence", "Note", "rnew2",
                          {"text": "qxrare qxrare qxrare qxrare qxrare alpha", "pack_id": "pa"})
    again, _ = _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] == 0
    assert "rnew2" in [h["node_id"] for h in again]
    assert _shape(again) == _shape(_oracle(store, q, ["pa"], limit=10))
    other.close()


def test_a_warm_query_reads_no_vocabulary_table(tmp_path, lowered):
    """Negative control: only statements on the two fts5vocab tables are counted."""
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)

    def vocab_reads():
        seen.clear()
        _search(store, q, ["pa"], limit=10)
        return sum(1 for sql in seen if nbm.VINST in sql or nbm.VROW in sql)

    counts = [vocab_reads() for _ in range(4)]
    assert counts[0] > 0 and counts[-1] == 0, counts
    store.upsert_node_doc("evidence", "Note", "rnew", {"text": "qxrare alpha", "pack_id": "pa"})
    assert vocab_reads() > 0


def test_a_replaced_database_file_switches_the_cache_off(tmp_path, lowered):
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    _search(store, q, ["pa"], limit=10)
    _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0
    spare = _store(tmp_path, "spare.db")
    _skewed(spare)
    spare.close()
    os.replace(tmp_path / "spare.db", tmp_path / "doc.db")
    for _ in range(2):
        _search(store, q, ["pa"], limit=10)
        assert lowered[-1]["cache_hits"] == 0 and lowered[-1]["cache_stores"] == 0


def test_closing_the_store_closes_the_token_connection(tmp_path):
    store = _store(tmp_path)
    token = store._bm25_token
    assert token() is not None
    conn = token._conn
    store.close()
    assert token() is None and token._conn is None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # the connection is closed, not only dropped


def test_a_caller_transaction_bypasses_the_cache(tmp_path, lowered):
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    _search(store, q, ["pa"], limit=10)
    conn = store._conn
    conn.execute("BEGIN")
    try:
        _search(store, q, ["pa"], limit=10)
        assert lowered[-1]["cache_hits"] == 0 and lowered[-1]["cache_stores"] == 0
    finally:
        conn.commit()
    _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0


class _Source:
    """A token source with scripted reads: ``read`` is the read before BEGIN."""

    def __init__(self, before, after):
        self._before, self._after, self.n = before, after, 0

    def read(self):
        self.n += 1
        return self._before, self.n

    def __call__(self):
        return self._after


def test_a_commit_between_the_two_token_reads_bypasses_the_cache(tmp_path, lowered):
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    store._bm25_token = _Source("v1", "v2")
    for _ in range(2):
        _search(store, q, ["pa"], limit=10)
        assert lowered[-1]["cache_hits"] == 0 and lowered[-1]["cache_stores"] == 0
    store._bm25_token = _Source("v1", "v1")
    _search(store, q, ["pa"], limit=10)
    _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0


class _CommitAfterPost:
    """The real token source. The second read of a search (the one after the snapshot is
    pinned) runs ``write`` once before it returns."""

    def __init__(self, real, write):
        self._real, self._write, self.armed = real, write, True

    def read(self):
        return self._real.read()

    def __call__(self):
        token = self._real()
        if self.armed:
            self.armed = False
            self._write()
        return token


def test_a_commit_after_the_second_token_read_does_not_mix_versions(tmp_path, lowered):
    """The second read must come after the snapshot is pinned. A commit that follows it is not
    in the snapshot, so the result equals the oracle before that commit."""
    store = _store(tmp_path)
    _skewed(store)
    other = LocalSQLDocStore(str(tmp_path / "doc.db"))
    q = "qxrare alpha beta gamma"
    _search(store, q, ["pa"], limit=10)
    _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0
    want = _oracle(store, q, ["pa"], limit=10)
    real = store._bm25_token

    def write():
        other.upsert_node_doc("evidence", "Note", "rnew",
                              {"text": "qxrare qxrare qxrare qxrare alpha", "pack_id": "pa"})

    proxy = _CommitAfterPost(real, write)
    store._bm25_token = proxy
    got, _ = _search(store, q, ["pa"], limit=10)
    assert not proxy.armed
    assert _shape(got) == _shape(want)
    store._bm25_token = real
    after, _ = _search(store, q, ["pa"], limit=10)
    assert "rnew" in [h["node_id"] for h in after]
    assert _shape(after) == _shape(_oracle(store, q, ["pa"], limit=10))
    other.close()


def test_a_search_runs_without_the_cache_when_the_file_cannot_be_read(tmp_path, lowered):
    """A failed read of the file identity must not stop the search. The store keeps its own
    connection, so the search still finds the same hits."""
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    want, _ = _search(store, q, ["pa"], limit=10)
    _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0
    os.replace(tmp_path / "doc.db", tmp_path / "away.db")
    got, _ = _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] == 0
    assert _shape(got) == _shape(want)
    store.close()


def test_a_failed_token_read_bypasses_the_cache(tmp_path, lowered):
    store = _store(tmp_path)
    _skewed(store)
    store._bm25_token = _Source(None, None)
    for _ in range(2):
        _search(store, "qxrare alpha", ["pa"], limit=10)
        assert lowered[-1]["cache_hits"] == 0 and lowered[-1]["cache_stores"] == 0


def test_a_term_is_cached_only_above_the_full_scan_bound(tmp_path, monkeypatch, lowered):
    """``qxrare`` occurs 150 times. A term at the bound is not cached, a term above it is.
    The per-pack search always takes the pruned path, so it reads the arrays too."""
    store = _store(tmp_path)
    _skewed(store)
    cache = store._bm25_cache
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", 150)
    for per_pack in (False, True, False, True):
        _search(store, "qxrare", ["pa"], limit=10, per_pack=per_pack)
    assert cache.counts == {"stats_hits": 0, "postings_hits": 0, "stores": 0}
    assert lowered[-1]["cache_stores"] == 0 and lowered[-1]["cache_hits"] == 0
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", 149)
    _search(store, "qxrare", ["pa"], limit=10, per_pack=True)
    assert lowered[-1]["cache_stores"] > 0 and cache.counts["stores"] >= 2
    _search(store, "qxrare", ["pa"], limit=10, per_pack=True)
    assert lowered[-1]["cache_hits"] > 0 and cache.counts["stats_hits"] > 0


def test_the_switch_is_read_when_the_store_is_made(tmp_path, monkeypatch, lowered):
    monkeypatch.setenv(cmod.SWITCH, "0")
    off = _store(tmp_path, "off.db")
    monkeypatch.delenv(cmod.SWITCH)
    on = _store(tmp_path, "on.db")
    assert off._bm25_cache is None and on._bm25_cache is not None
    _skewed(off)
    for _ in range(2):
        _search(off, "qxrare alpha", ["pa"], limit=10)
        assert lowered[-1]["cache_hits"] == 0 and lowered[-1]["cache_stores"] == 0


def test_a_pruned_search_falls_back_with_a_warm_cache(tmp_path, monkeypatch, lowered, caplog):
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    want, _ = _search(store, q, ["pa"], limit=10)
    _search(store, q, ["pa"], limit=10)
    assert lowered[-1]["cache_hits"] > 0

    def broken(*a, **k):
        raise prune.PruneFallbackError("forced")

    monkeypatch.setattr(prune, "_match_docs", broken)
    caplog.set_level(logging.WARNING)
    got, _ = _search(store, q, ["pa"], limit=10)
    assert FALLBACK in caplog.text
    assert _shape(got) == _shape(want) == _shape(_oracle(store, q, ["pa"], limit=10))


def test_cached_arrays_cannot_be_changed_by_the_search(tmp_path, lowered):
    store = _store(tmp_path)
    _skewed(store)
    q = "qxrare alpha beta gamma"
    for _ in range(2):
        _search(store, q, ["pa"], limit=10)
    gen = store._bm25_cache._gen
    assert gen.postings
    for keys, tf in gen.postings.values():
        assert not keys.flags.writeable and not tf.flags.writeable
