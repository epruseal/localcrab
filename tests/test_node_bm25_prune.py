"""#567: exact top-k pruning of the SQLite node BM25 search.

Design: docs/issue-567-bm25-prune.md (section 10 lists these tests). Small
corpora take the full scan by the cost rule, so each test forces the pruned
path with ``FULL_SCAN_CF_MAX = -1``.
"""

from __future__ import annotations

import logging
import random
import sqlite3

import pytest

from opencrab.ontology import bm25 as bm25_mod
from opencrab.ontology.bm25 import BM25Index
from opencrab.stores import _node_bm25_prune as prune
from opencrab.stores import _node_bm25_sqlite as nbm
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 43, 0),
    reason="FTS5 contentless_delete needs SQLite 3.43 or newer",
)

FALLBACK = "pruned search fell back"


@pytest.fixture
def forced(monkeypatch):
    monkeypatch.setattr(prune, "FULL_SCAN_CF_MAX", -1)
    monkeypatch.delenv("OPENCRAB_BM25_PRUNE", raising=False)
    calls = {"select": 0, "postings": 0, "retok": 0, "stats": {}}
    real_select, real_postings, real_terms = prune.select, prune._postings, nbm.node_terms

    def select(*a, **k):
        calls["select"] += 1
        calls["stats"] = k["stats"] = {}
        return real_select(*a, **k)

    def postings(*a, **k):
        calls["postings"] += 1
        return real_postings(*a, **k)

    def terms(*a, **k):
        calls["retok"] += 1
        return real_terms(*a, **k)

    monkeypatch.setattr(prune, "select", select)
    monkeypatch.setattr(prune, "_postings", postings)
    monkeypatch.setattr(nbm, "node_terms", terms)
    return calls


def _store(tmp_path, name="doc.db") -> LocalSQLDocStore:
    store = LocalSQLDocStore(str(tmp_path / name))
    assert store.supports_node_bm25
    return store


def _search(store, q, packs, spaces=None, limit=10, per_pack=False):
    result = store.node_bm25_search(q, pack_ids=packs, spaces=spaces, limit=limit,
                                    per_pack=per_pack)
    assert result is not None
    return result


def _full(store, monkeypatch, *a, **k):
    with monkeypatch.context() as m:
        m.setenv("OPENCRAB_BM25_PRUNE", "0")
        return _search(store, *a, **k)


def _oracle(store, q, packs, spaces=None, limit=10):
    nodes = store.list_nodes(limit=10**9)
    index = BM25Index.build(nodes, max_text_chars=bm25_mod.max_text_chars_setting())
    return index.search(q, spaces=spaces, limit=limit, pack_ids=packs)


def _shape(hits):
    return [(h["node_id"], h["space"], h["node_type"], h["score"], h["pack_id"], h["text"],
             h["properties"]) for h in hits]


_KO = ["건축", "설계", "변경이유", "적용불가", "구조물", "안전관리", "검토의견", "콘크리트"]
_EN = ["beam", "column", "load", "reason", "steel", "frame", "joint", "safety"]
_RARE = ["qxalpha", "qxbravo", "qxcharlie"]


def _zipf_corpus(store, rng: random.Random, n: int = 220) -> None:
    words = _KO + _EN
    weights = [1.0 / (r + 1) ** 1.1 for r in range(len(words))]
    packs = ["pa", "pb", "pc"]
    for i in range(n):
        body = rng.choices(words, weights, k=rng.randint(1, 30))
        if i % 9 == 0:
            body += [rng.choice(_RARE)] * rng.randint(1, 3)
        props = {"text": " ".join(body), "title": rng.choice(_EN), "pack_id": rng.choice(packs)}
        if i % 37 == 0:
            props["pack_id"] = ""
        if i % 41 == 0:
            props["pack_id"] = 7
        if i % 43 == 0:
            props["text"] += " " + "건축물 " * 3000
        store.upsert_node_doc("evidence" if i % 5 else "concept", "Note", f"n{i:04d}", props)
    for j in range(6):  # identical bodies so the k-th place has ties
        store.upsert_node_doc("evidence", "Note", f"dup{j}", {"text": "steel beam qxalpha",
                                                              "pack_id": "pa"})
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO doc_nodes(space, node_id, node_type, properties, updated_at) "
            "VALUES ('evidence', 'broken', 'Note', '{not json', '2026-01-01T00:00:00+00:00')"
        )
    assert store.node_bm25_sync_pending() == 0


_QUESTIONS = ["qxalpha steel beam", "건축 설계 load", "steel", "qxbravo 콘크리트 구조물 frame",
              "load load frame joint safety", "변경 이유 적용", "7", "zzz-none",
              "qxcharlie beam beam column 건축"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_pruned_path_equals_full_scan_and_memory_oracle(tmp_path, monkeypatch, forced, caplog,
                                                        seed):
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
                for pid in scope:
                    assert _shape(per.get(pid, [])) == _shape(_oracle(store, q, [pid], None, limit))
    assert forced["select"] > 0
    assert FALLBACK not in caplog.text


def test_db_oracle_of_541_with_the_pruned_path(tmp_path, monkeypatch, forced, caplog):
    """The #541 oracle comparison, forced through the pruned path."""
    from tests.test_node_bm25_index import _corpus

    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "4000")
    store = _store(tmp_path)
    _corpus(store, random.Random(1))
    caplog.set_level(logging.WARNING)
    questions = ["건축 설계", "steel beam", "변경 이유 적용", "load load frame", "안전", "7",
                 "joint 콘크리트 구조물", "zzz-none"]
    for q in questions:
        for scope in (["pa"], ["pb", "pc"], ["pa", "pb", "pc", "7"], ["7"], ["missing"]):
            for spaces in (None, ["concept"], ["evidence", "concept"]):
                for limit in (1, 3, 10):
                    got, _ = _search(store, q, scope, spaces, limit)
                    assert _shape(got) == _shape(_oracle(store, q, scope, spaces, limit))
    assert FALLBACK not in caplog.text


def _skewed(store, n_rare: int = 60, n_common: int = 400) -> None:
    for i in range(n_common):
        store.upsert_node_doc("evidence", "Note", f"c{i:04d}",
                              {"text": "alpha beta gamma " * (1 + i % 3), "pack_id": "pa"})
    for i in range(n_rare):
        store.upsert_node_doc("evidence", "Note", f"r{i:04d}",
                              {"text": "qxrare " * (1 + i % 4) + "alpha " * (1 + i % 7),
                               "pack_id": "pa"})
    assert store.node_bm25_sync_pending() == 0


def test_pruning_takes_only_the_rare_term_documents(tmp_path, monkeypatch, forced, caplog):
    store = _store(tmp_path)
    _skewed(store)
    caplog.set_level(logging.WARNING)
    q = "qxrare alpha beta gamma"
    got, _ = _search(store, q, ["pa"], limit=10)
    stats = forced["stats"]
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=10)
    assert _shape(got) == _shape(full) == _shape(_oracle(store, q, ["pa"], limit=10))
    assert forced["select"] == 1
    # The group closes after the rare term: the 400 common documents never enter.
    assert stats["admitted"] == 60, stats
    assert stats["chosen"] < 60, stats
    assert FALLBACK not in caplog.text


def test_each_finish_path_matches_the_full_scan(tmp_path, monkeypatch, forced, caplog):
    store = _store(tmp_path)
    _skewed(store)
    caplog.set_level(logging.WARNING)
    q = "qxrare alpha beta gamma"
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=10)
    # Posting scans only: re-tokenising never looks cheaper.
    with monkeypatch.context() as m:
        m.setattr(prune, "RETOK_BASE_US", 1e12)
        before = dict(forced)
        got, _ = _search(store, q, ["pa"], limit=10)
        assert _shape(got) == _shape(full)
        assert forced["retok"] == before["retok"]
        assert forced["postings"] - before["postings"] == 4
    # Re-tokenising everything: posting scans never look cheaper.
    with monkeypatch.context() as m:
        m.setattr(prune, "SCAN_US", 1e12)
        before = dict(forced)
        got, _ = _search(store, q, ["pa"], limit=10)
        assert _shape(got) == _shape(full)
        assert forced["postings"] == before["postings"]
        assert forced["retok"] > before["retok"]
    assert FALLBACK not in caplog.text


def test_average_length_below_one_keeps_the_full_scan_order(tmp_path, monkeypatch, forced):
    """Verifier counterexample: 225 documents, total length 45."""
    store = _store(tmp_path)
    for i in range(223):  # no token: punctuation id, no type, no properties
        store.upsert_node_doc("evidence", "", "-" * (i + 1), {})
    store.upsert_node_doc("evidence", "", "+", {"text": "x", "pack_id": "pa"})
    store.upsert_node_doc("evidence", "", "*", {"text": "x " * 10 + "f " * 28, "pack_id": "pa"})
    assert store.node_bm25_sync_pending() == 0
    st = store.node_bm25_status()
    assert (st["doc_count"], st["total_len"]) == (225, 45)
    for limit in (1, 2):
        got, _ = _search(store, "x", ["pa"], limit=limit)
        full, _ = _full(store, monkeypatch, "x", ["pa"], limit=limit)
        assert _shape(got) == _shape(full)
    assert forced["select"] == 2


def test_long_repeated_query_matches_the_full_scan(tmp_path, monkeypatch, forced, caplog):
    store = _store(tmp_path)
    _zipf_corpus(store, random.Random(7))
    caplog.set_level(logging.WARNING)
    for q in ("steel " * 1000, "qxalpha " * 500 + "건축 설계 beam " * 200):
        got, _ = _search(store, q, ["pa", "pb"], limit=10)
        full, _ = _full(store, monkeypatch, q, ["pa", "pb"], limit=10)
        assert _shape(got) == _shape(full)
    assert FALLBACK not in caplog.text


def test_entry_conditions_use_the_full_scan(tmp_path, monkeypatch, forced):
    store = _store(tmp_path)
    _skewed(store, n_rare=5, n_common=20)
    q = "qxrare alpha"
    for limit in (0, -2):
        _search(store, q, ["pa"], limit=limit)
    _full(store, monkeypatch, q, ["pa"], limit=5)
    assert forced["select"] == 0
    # A pending row in the snapshot takes the full scan (it excludes the key).
    with sqlite3.connect(store._db_path) as raw:
        raw.execute("UPDATE doc_nodes SET properties=json_set(properties, '$.text', 'qxrare') "
                    "WHERE node_id='c0000'")
    with monkeypatch.context() as m:
        m.setattr(store, "_node_bm25_try_drain", lambda: None)
        _hits, warnings = _search(store, q, ["pa"], limit=5)
    assert forced["select"] == 0
    assert warnings and warnings[0].startswith("BM25 coverage pending changes")


def test_inconsistencies_fall_back_to_the_full_scan(tmp_path, monkeypatch, forced, caplog):
    store = _store(tmp_path)
    _skewed(store)
    caplog.set_level(logging.WARNING)
    q = "qxrare alpha beta gamma"
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=10)
    real_postings = prune._postings

    def short_postings(conn, vinst, term):
        keys, tf = real_postings(conn, vinst, term)
        return keys[1:], tf[1:]

    with monkeypatch.context() as m:
        m.setattr(prune, "_postings", short_postings)
        m.setattr(prune, "RETOK_BASE_US", 1e12)
        got, _ = _search(store, q, ["pa"], limit=10)
    assert _shape(got) == _shape(full)
    assert FALLBACK in caplog.text and "match outside postings" in caplog.text
    caplog.clear()

    real_terms = nbm.node_terms

    def short_terms(node, max_chars):
        return real_terms(node, max_chars)[1:]

    with monkeypatch.context() as m:
        m.setattr(nbm, "node_terms", short_terms)
        m.setattr(prune, "SCAN_US", 1e12)
        got, _ = _search(store, q, ["pa"], limit=10)
    assert _shape(got) == _shape(full)
    assert FALLBACK in caplog.text and "dl" in caplog.text


def test_row_check_drop_falls_back_to_the_full_scan(tmp_path, monkeypatch, forced, caplog):
    store = _store(tmp_path)
    _skewed(store)
    caplog.set_level(logging.WARNING)
    q = "qxrare alpha"
    top = _full(store, monkeypatch, q, ["pa"], limit=5)[0][0]["node_id"]
    # Remove a top row behind the triggers: the snapshot has no pending key.
    with sqlite3.connect(store._db_path) as raw:
        for name in nbm.TRIGGERS:
            raw.execute(f'DROP TRIGGER "{name}"')
        raw.execute("DELETE FROM doc_nodes WHERE node_id=?", (top,))
    full, _ = _full(store, monkeypatch, q, ["pa"], limit=5)
    got, _ = _search(store, q, ["pa"], limit=5)
    assert top not in {h["node_id"] for h in got}
    assert _shape(got) == _shape(full)
    # The seeding re-tokeniser or the row check finds the missing row.
    assert FALLBACK in caplog.text
    assert "node row" in caplog.text or "missing row" in caplog.text


def test_switch_off_skips_the_pruned_path(tmp_path, monkeypatch, forced):
    store = _store(tmp_path)
    _skewed(store, n_rare=5, n_common=20)
    monkeypatch.setenv("OPENCRAB_BM25_PRUNE", "0")
    _search(store, "qxrare alpha", ["pa"], limit=5)
    assert forced["select"] == 0
