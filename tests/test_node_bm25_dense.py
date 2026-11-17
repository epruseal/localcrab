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
from opencrab.stores import _node_bm25_sqlite as nbm
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


@pytest.fixture(params=[0.0, 100.0], ids=["all-dense", "mixed"])
def dense(monkeypatch, request):
    """Force the pruned path. 0 sends every term to the dense path. 100 sends only the
    common terms there (``SCAN_US * cf >= 100``) and keeps the rare ones on ``MATCH``."""
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", -1)
    monkeypatch.setattr(prune, "DENSE_SCAN_US", request.param)
    monkeypatch.delenv("OPENCRAB_BM25_PRUNE", raising=False)
    # The latency warning depends on the wall clock. Keep it out of these equality tests.
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", 1e9)
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", 1e9)
    calls = {"columns": 0, "match": 0, "postings": 0, "stats": []}
    real_columns, real_match, real_postings = prune._doc_columns, prune._match_docs, prune._postings
    real_select = prune.select

    def select(*a, **k):
        k["stats"] = {}
        calls["stats"].append(k["stats"])
        return real_select(*a, **k)

    monkeypatch.setattr(prune, "select", select)

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


def test_dense_admission_takes_the_same_candidates_as_the_match_join(tmp_path, monkeypatch, dense):
    """Equal hits cannot show a wrong candidate set: a wrongly admitted or missed document
    can lose by score anyway. ``admitted`` and ``chosen`` count the candidates."""
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "4000")
    store = _store(tmp_path)
    _zipf_corpus(store, random.Random(3))
    scopes = [["pa"], ["pb", "pc"], ["pa", "pb", "pc", "7"]]
    runs = []
    for q in _QUESTIONS:
        for scope in scopes:
            for per_pack in (False, True):
                runs.append((q, scope, per_pack))
    with_dense = []
    for q, scope, per_pack in runs:
        dense["stats"].clear()
        _search(store, q, scope, None, 10, per_pack=per_pack)
        with_dense.append([(s.get("admitted"), s.get("chosen")) for s in dense["stats"]])
    with monkeypatch.context() as m:
        m.setattr(prune, "DENSE_SCAN_US", 1e18)
        without = []
        for q, scope, per_pack in runs:
            dense["stats"].clear()
            _search(store, q, scope, None, 10, per_pack=per_pack)
            without.append([(s.get("admitted"), s.get("chosen")) for s in dense["stats"]])
    assert with_dense == without


def test_dense_terms_skip_the_match_join(tmp_path, dense):
    store = _store(tmp_path)
    _skewed(store)
    _search(store, "alpha beta gamma", ["pa"], limit=10)
    assert dense["columns"] == 1
    assert dense["match"] == 0
    assert dense["postings"] >= 1


def test_per_pack_search_keeps_the_match_join(tmp_path, dense):
    store = _store(tmp_path)
    _skewed(store)
    _search(store, "alpha beta gamma", ["pa"], limit=10, per_pack=True)
    assert dense["columns"] == 0
    assert dense["match"] >= 1


def test_scan_needed_rule():
    need = prune._scan_needed
    # All documents inside, many new documents: re-tokenising loses to the scan.
    assert need(m_docs=1000, df_term=1000, n_docs=1000, known=0, cf_term=5000)
    # The packs hold few documents: most matches lie outside, so few new documents remain.
    assert not need(m_docs=10, df_term=1000, n_docs=1000, known=0, cf_term=5000)
    # Known documents are not new.
    assert not need(m_docs=1000, df_term=1000, n_docs=1000, known=990, cf_term=5000)
    # The bound is exact at the edge: 50 us per new document against 0.25 us per occurrence.
    edge = int(prune.SCAN_US * 5000 / prune.RETOK_BASE_US)
    assert need(m_docs=1000, df_term=edge, n_docs=1000, known=0, cf_term=5000)
    assert not need(m_docs=1000, df_term=edge - 1, n_docs=1000, known=0, cf_term=5000)


def test_the_dense_path_follows_the_rule(tmp_path, monkeypatch, dense):
    store = _store(tmp_path)
    rng = random.Random(7)
    for i in range(400):
        pack = "pa" if i < 4 else "pb"
        store.upsert_node_doc("evidence", "Note", f"n{i:04d}", {
            "text": "alpha beta " * rng.randint(1, 6) + "gamma " * rng.randint(0, 3),
            "pack_id": pack})
    assert store.node_bm25_sync_pending() == 0
    q = "alpha beta gamma"
    # Most matches lie outside the one small pack: the join route serves it.
    small, _ = _search(store, q, ["pa"], limit=10)
    assert dense["match"] >= 1
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=10)
    assert _shape(small) == _shape(full)
    # Re-tokenising never looks cheap: the dense route serves the large pack.
    before = dict(dense)
    monkeypatch.setattr(prune, "RETOK_BASE_US", 1e12)
    big, _ = _search(store, q, ["pb"], limit=10)
    assert dense["match"] == before["match"]
    assert dense["columns"] > before["columns"]
    assert _shape(big) == _shape(_full(store, monkeypatch, q, ["pb"], limit=10)[0])
    # Re-tokenising looks free: the join route serves it again.
    before = dict(dense)
    monkeypatch.setattr(prune, "RETOK_BASE_US", 0.0)
    free, _ = _search(store, q, ["pb"], limit=10)
    assert dense["match"] > before["match"]
    assert _shape(free) == _shape(_full(store, monkeypatch, q, ["pb"], limit=10)[0])


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
    store = _store(tmp_path)
    _skewed(store)
    common, rare = "alpha beta gamma", "qxrare"
    # A search inside the budget has no latency warning.
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", 1e9)
    _hits, warnings = _search(store, common, ["pa"], limit=10)
    assert warnings == []
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", -1.0)
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", -1.0)
    hits, warnings = _search(store, common, ["pa"], limit=10)
    assert len(warnings) == 1
    assert warnings[0].startswith("BM25 latency over budget:")
    assert "3 of 3 indexed query terms occur in more than half" in warnings[0]
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


def test_probe_latency_warning_has_its_own_selection_code(monkeypatch):
    """A slow probe over a complete index must not read as incomplete coverage: the fault
    harness excuses a wrong pack only for the coverage code."""
    from unittest.mock import MagicMock

    from opencrab.services import pack_selection as ps

    assert nbm.SLOW_WARNING.startswith(ps.PROBE_LATENCY_PREFIX)
    assert not nbm.PENDING_WARNING.startswith(ps.PROBE_LATENCY_PREFIX)
    assert ps.mcp_warning_text(ps.PackWarning(ps.AUTO_PACK_PROBE_SLOW))
    assert ps.cli_warning_text(ps.PackWarning(ps.AUTO_PACK_PROBE_SLOW))
    slow = nbm.SLOW_WARNING.format(ms=3000, budget=2000, common=2, terms=3)
    coverage = nbm.PENDING_WARNING.format(n=1)
    monkeypatch.setattr(
        "opencrab.pack.ownership.list_packs_for",
        lambda sql, principal: [{"pack_id": "pack-a", "title": "x", "description": "y"}],
    )

    def codes(probe_warnings):
        class Hybrid:
            def _bm25_search_per_pack_with_warnings(self, question, spaces, limit, *, pack_ids):
                return ({"pack-a": [{"pack_id": "pack-a", "text": "zzword", "score": 3.5}]},
                        probe_warnings)

            def _fts_search(self, question, spaces, limit, *, pack_ids):
                return []

        sel = ps.resolve_packs(
            "zzword", None, True, False, "/tmp", scope=frozenset({"pack-a"}),
            raise_on_error=False, sql=MagicMock(), principal=MagicMock(), hybrid=Hybrid(),
            spaces=None)
        assert sel.effective_pack_ids == ["pack-a"]
        return [w.code for w in sel.warnings]

    assert codes([]) == []
    assert codes([slow]) == [ps.AUTO_PACK_PROBE_SLOW]
    assert codes([coverage]) == [ps.AUTO_PACK_PROBE_COVERAGE]
    assert codes([coverage, slow]) == [ps.AUTO_PACK_PROBE_COVERAGE, ps.AUTO_PACK_PROBE_SLOW]
