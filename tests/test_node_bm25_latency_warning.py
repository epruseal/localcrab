"""#569: the latency warning of a BM25 search with common query terms.

Design: docs/issue-569-common-term-latency.md. A search over the budget names the
latency in a warning and returns the same hits as a fast search.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from opencrab.stores import _node_bm25_sqlite as nbm
from tests.test_node_bm25_prune import _search, _shape, _skewed, _store

pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 43, 0),
    reason="FTS5 contentless_delete needs SQLite 3.43 or newer",
)


def test_budgets_follow_the_latency_requirement():
    """docs/issue-542-search-baseline.md 4.3: BM25 leg 500 ms. The auto request gets 2500 ms
    and the probe leaves the leg budget of the query that follows it."""
    assert nbm.LEG_BUDGET_MS == 500.0
    assert nbm.PROBE_BUDGET_MS + nbm.LEG_BUDGET_MS == 2500.0


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
    assert "3 of 3 indexed query terms are common" in warnings[0]
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


def test_a_term_with_many_occurrences_counts_as_common_below_half_the_documents(
        tmp_path, monkeypatch):
    """#667: the query cost follows the occurrences, so a term that is in few documents but
    occurs often counts as common (``qxrare``: 60 of 460 documents, 150 occurrences)."""
    from opencrab.stores import _node_bm25_prune as prune

    store = _store(tmp_path)
    _skewed(store)
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", -1.0)
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", 200)
    assert _search(store, "qxrare", ["pa"], limit=10)[1] == []
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", 150)  # "more than": the bound itself is not common
    assert _search(store, "qxrare", ["pa"], limit=10)[1] == []
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", 149)
    warnings = _search(store, "qxrare", ["pa"], limit=10)[1]
    assert len(warnings) == 1
    assert "1 of 1 indexed query terms are common" in warnings[0]
    assert "more than 149 times" in warnings[0]
    # The same rule applies to the per-pack probe.
    monkeypatch.setattr(nbm, "PROBE_BUDGET_MS", -1.0)
    assert len(_search(store, "qxrare", ["pa"], limit=10, per_pack=True)[1]) == 1


class _Clock:
    """A clock that reads 0 at the start of the search and ``ms`` milliseconds at its end."""

    def __init__(self, ms):
        self._reads = iter([0.0, ms / 1000.0])

    def perf_counter(self):
        return next(self._reads)


def _timed_search(monkeypatch, ms, *, per_pack=False, common=1, earlier=()):
    """``nbm.search`` with a body that takes ``ms`` and reports ``common`` of 4 terms."""

    def body(conn, row_to_node, question, *, terms, **kwargs):
        terms.update(common=common, all=4)
        return [], list(earlier)

    monkeypatch.setattr(nbm, "_search", body)
    monkeypatch.setattr(nbm, "time", SimpleNamespace(perf_counter=_Clock(ms).perf_counter))
    return nbm.search(None, None, "q", pack_ids=["pa"], spaces=None, limit=1,
                      per_pack=per_pack)[1]


def test_the_warning_needs_a_time_over_the_budget_and_a_common_term(monkeypatch):
    assert _timed_search(monkeypatch, 500) == []  # the budget itself is within it
    assert len(_timed_search(monkeypatch, 501)) == 1
    assert _timed_search(monkeypatch, 2000, per_pack=True) == []
    assert len(_timed_search(monkeypatch, 2001, per_pack=True)) == 1
    assert _timed_search(monkeypatch, 10**6, common=0) == []


def test_the_warning_text_names_time_budget_and_term_counts(monkeypatch):
    leg = _timed_search(monkeypatch, 750)[0]
    assert leg.startswith("BM25 latency over budget: 750 ms against 500 ms; "
                          "1 of 4 indexed query terms are common")
    assert "more than 50000 times" in leg
    probe = _timed_search(monkeypatch, 2500, per_pack=True)[0]
    assert probe.startswith("BM25 latency over budget: 2500 ms against 2000 ms; ")


def test_the_warning_follows_the_earlier_warnings(monkeypatch):
    warnings = _timed_search(monkeypatch, 750, earlier=("earlier one",))
    assert warnings[0] == "earlier one" and len(warnings) == 2
    assert warnings[1].startswith("BM25 latency over budget:")


def test_the_term_counts_cover_distinct_indexed_terms_only(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _skewed(store)
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", -1.0)
    # "alpha" twice and a term that is not in the index: two indexed terms, both common.
    warnings = _search(store, "alpha alpha beta qzxnotindexed", ["pa"], limit=10)[1]
    assert len(warnings) == 1 and "2 of 2 indexed query terms are common" in warnings[0]


def test_a_term_in_exactly_half_of_the_documents_is_not_common(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for i, text in enumerate(["most half", "most half", "most other", "most other", "most"]):
        store.upsert_node_doc("evidence", "Note", f"n{i}", {"text": text, "pack_id": "pa"})
    monkeypatch.setattr(nbm, "LEG_BUDGET_MS", -1.0)
    # 5 documents: "half" is in 2 of them and "most" in all 5.
    assert _search(store, "half", ["pa"], limit=10)[1] == []
    assert len(_search(store, "most", ["pa"], limit=10)[1]) == 1
    store.upsert_node_doc("evidence", "Note", "n5", {"text": "half", "pack_id": "pa"})
    assert _search(store, "half", ["pa"], limit=10)[1] == []  # 3 of 6: exactly half
    store.upsert_node_doc("evidence", "Note", "n6", {"text": "half", "pack_id": "pa"})
    assert len(_search(store, "half", ["pa"], limit=10)[1]) == 1  # 4 of 7: more than half


def test_probe_latency_warning_has_its_own_selection_code(monkeypatch):
    """A slow probe over a complete index must not read as incomplete coverage: the fault
    harness excuses a wrong pack only for the coverage code."""
    from unittest.mock import MagicMock

    from opencrab.services import pack_selection as ps

    assert nbm.SLOW_WARNING.startswith(ps.PROBE_LATENCY_PREFIX)
    assert not nbm.PENDING_WARNING.startswith(ps.PROBE_LATENCY_PREFIX)
    assert ps.mcp_warning_text(ps.PackWarning(ps.AUTO_PACK_PROBE_SLOW))
    assert ps.cli_warning_text(ps.PackWarning(ps.AUTO_PACK_PROBE_SLOW))
    slow = nbm.SLOW_WARNING.format(ms=3000, budget=2000, common=2, terms=3, cf_max=50000)
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
