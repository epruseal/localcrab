"""#569: the latency warning of a BM25 search with common query terms.

Design: docs/issue-569-common-term-latency.md. A search over the budget names the
latency in a warning and returns the same hits as a fast search.
"""

from __future__ import annotations

import sqlite3

import pytest

from opencrab.stores import _node_bm25_sqlite as nbm
from tests.test_node_bm25_prune import _search, _shape, _skewed, _store

pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 43, 0),
    reason="FTS5 contentless_delete needs SQLite 3.43 or newer",
)


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
