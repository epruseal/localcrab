"""#569: the dense-term admission of the exact top-k pruning.

Design: docs/issue-569-common-term-latency.md. A term whose posting scan is
expensive is admitted from its postings and one column read of ``docs`` instead
of a ``MATCH`` join. These tests force that path on small corpora
(``DENSE_SCAN_US = 0``) and require the same hits as the full scan, the #541
oracle and the pruned path without the dense rule.
"""

from __future__ import annotations

import logging
import random
import sqlite3

import pytest

from opencrab.stores import _node_bm25_prune as prune
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


@pytest.fixture
def dense(monkeypatch):
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", -1)
    monkeypatch.setattr(prune, "DENSE_SCAN_US", 0.0)
    monkeypatch.delenv("OPENCRAB_BM25_PRUNE", raising=False)
    calls = {"columns": 0, "match": 0, "postings": 0}
    real_columns, real_match, real_postings = prune._doc_columns, prune._match_docs, prune._postings

    def columns(*a, **k):
        calls["columns"] += 1
        return real_columns(*a, **k)

    def match(*a, **k):
        calls["match"] += 1
        return real_match(*a, **k)

    def postings(*a, **k):
        calls["postings"] += 1
        return real_postings(*a, **k)

    monkeypatch.setattr(prune, "_doc_columns", columns)
    monkeypatch.setattr(prune, "_match_docs", match)
    monkeypatch.setattr(prune, "_postings", postings)
    return calls


@pytest.mark.parametrize("seed", [1, 2])
def test_dense_admission_equals_full_scan_and_oracle(tmp_path, monkeypatch, dense, caplog, seed):
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "4000")
    store = _store(tmp_path)
    _zipf_corpus(store, random.Random(seed))
    caplog.set_level(logging.WARNING)
    scopes = [["pa"], ["pb", "pc"], ["pa", "pb", "pc", "7"], ["missing"]]
    for q in _QUESTIONS:
        for scope in scopes:
            for spaces in (None, ["concept"]):
                for limit in (1, 3, 10, 40):
                    got, warnings = _search(store, q, scope, spaces, limit)
                    full, _ = _full(store, monkeypatch, q, scope, spaces, limit)
                    want = _oracle(store, q, scope, spaces, limit)
                    assert _shape(got) == _shape(full) == _shape(want), (q, scope, spaces, limit)
                    assert warnings == []
            for limit in (1, 3, 50):
                per, _ = _search(store, q, scope, None, limit, per_pack=True)
                per_full, _ = _full(store, monkeypatch, q, scope, None, limit, per_pack=True)
                assert {p: _shape(h) for p, h in per.items()} == \
                    {p: _shape(h) for p, h in per_full.items()}, (q, scope, limit)
    assert dense["columns"] > 0
    assert FALLBACK not in caplog.text


def test_dense_terms_skip_the_match_join(tmp_path, dense):
    store = _store(tmp_path)
    _skewed(store)
    _search(store, "alpha beta gamma", ["pa"], limit=10)
    assert dense["columns"] == 1
    assert dense["match"] == 0
    assert dense["postings"] >= 1


def test_the_default_threshold_keeps_small_terms_on_the_match_join(tmp_path, monkeypatch):
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", -1)
    calls = {"columns": 0}
    real = prune._doc_columns

    def columns(*a, **k):
        calls["columns"] += 1
        return real(*a, **k)

    monkeypatch.setattr(prune, "_doc_columns", columns)
    store = _store(tmp_path)
    _skewed(store)
    got, _ = _search(store, "qxrare alpha beta gamma", ["pa"], limit=10)
    full, _ = _full(store, monkeypatch, "qxrare alpha beta gamma", ["pa"], limit=10)
    assert _shape(got) == _shape(full)
    assert calls["columns"] == 0


def test_posting_count_mismatch_falls_back(tmp_path, monkeypatch, dense, caplog):
    store = _store(tmp_path)
    _skewed(store)
    caplog.set_level(logging.WARNING)
    q = "alpha beta gamma"
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=10)
    real_postings = prune._postings

    def short_postings(conn, vinst, term):
        keys, tf = real_postings(conn, vinst, term)
        return keys[1:], tf[1:]

    monkeypatch.setattr(prune, "_postings", short_postings)
    got, _ = _search(store, q, ["pa"], limit=10)
    assert _shape(got) == _shape(full)
    assert FALLBACK in caplog.text and "posting count" in caplog.text


def test_column_text_mismatch_falls_back(tmp_path, monkeypatch, dense, caplog):
    store = _store(tmp_path)
    _skewed(store)
    caplog.set_level(logging.WARNING)
    q = "alpha beta gamma"
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=10)

    def broken(*a, **k):
        raise prune.PruneFallbackError("column text")

    monkeypatch.setattr(prune, "_doc_columns", broken)
    got, _ = _search(store, q, ["pa"], limit=10)
    assert _shape(got) == _shape(full)
    assert FALLBACK in caplog.text and "column text" in caplog.text


def test_dense_columns_honour_pack_and_space_filters(tmp_path, monkeypatch, dense):
    store = _store(tmp_path)
    rng = random.Random(5)
    for i in range(300):
        pack = ["pa", "pb", "pc"][i % 3]
        space = "evidence" if i % 4 else "concept"
        store.upsert_node_doc(space, "Note", f"n{i:04d}", {
            "text": "alpha beta " * rng.randint(1, 6) + "gamma " * rng.randint(0, 3),
            "pack_id": pack})
    assert store.node_bm25_sync_pending() == 0
    for scope in (["pa"], ["pb", "pc"], ["pa", "pb", "pc"]):
        for spaces in (None, ["concept"]):
            for per_pack in (False, True):
                got, _ = _search(store, "alpha beta gamma", scope, spaces, 20, per_pack=per_pack)
                want, _ = _full(store, monkeypatch, "alpha beta gamma", scope, spaces, 20,
                                per_pack=per_pack)
                if per_pack:
                    got = {p: _shape(h) for p, h in got.items()}
                    want = {p: _shape(h) for p, h in want.items()}
                else:
                    got, want = _shape(got), _shape(want)
                assert got == want, (scope, spaces, per_pack)


def test_slow_search_with_common_terms_names_the_latency(tmp_path, monkeypatch):
    from opencrab.stores import _node_bm25_sqlite as nbm

    store = _store(tmp_path)
    _skewed(store)
    common, rare = "alpha beta gamma", "qxrare"
    # The default budget leaves a fast search without a latency warning.
    _hits, warnings = _search(store, common, ["pa"], limit=10)
    assert warnings == []
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", -1.0)
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", -1.0)
    hits, warnings = _search(store, common, ["pa"], limit=10)
    assert len(warnings) == 1
    assert warnings[0].startswith("BM25 latency over budget:")
    assert "3 of 3 query terms occur in more than half" in warnings[0]
    assert not warnings[0].startswith("BM25 coverage")
    # The hits equal the hits of a search that is fast enough.
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", 10**9)
    fast, fast_warnings = _search(store, common, ["pa"], limit=10)
    assert fast_warnings == [] and _shape(hits) == _shape(fast)
    # A query without a common term gets no latency warning at any speed.
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", -1.0)
    assert _search(store, rare, ["pa"], limit=10)[1] == []
    # The per-pack probe reads its own budget.
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", 10**9)
    assert _search(store, common, ["pa"], limit=10, per_pack=True)[1] == []
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", -1.0)
    assert len(_search(store, common, ["pa"], limit=10, per_pack=True)[1]) == 1
