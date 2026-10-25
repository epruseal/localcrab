"""#541: persisted full node BM25 index in the SQLite doc store.

Design: docs/issue-541-db-bm25.md (section 9 lists these tests).
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time

import pytest

from opencrab.ontology import bm25 as bm25_mod
from opencrab.ontology.bm25 import BM25Index
from opencrab.ontology.query import HybridQuery
from opencrab.stores import _node_bm25_sqlite as nbm
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

FTS5_DELETE_OK = sqlite3.sqlite_version_info >= (3, 43, 0)
pytestmark = pytest.mark.skipif(
    not FTS5_DELETE_OK, reason="FTS5 contentless_delete needs SQLite 3.43 or newer"
)


class _NoGraph:
    available = False


class _NoVector:
    available = False

    def query(self, *args, **kwargs):
        return []


def _store(tmp_path, name: str = "doc.db") -> LocalSQLDocStore:
    store = LocalSQLDocStore(str(tmp_path / name))
    assert store.available
    assert store.supports_node_bm25, "SQLite >= 3.43 must support the node index"
    return store


def _hybrid(store) -> HybridQuery:
    hq = HybridQuery(_NoVector(), _NoGraph())
    hq._doc_store = store
    hq._bm25_debounce = 0.05
    return hq


def _oracle(store, question, pack_ids, spaces=None, limit=10):
    nodes = store.list_nodes(limit=10**9)
    index = BM25Index.build(nodes, max_text_chars=bm25_mod.max_text_chars_setting())
    return index.search(question, spaces=spaces, limit=limit, pack_ids=pack_ids)


def _db(store, question, pack_ids, spaces=None, limit=10, per_pack=False):
    result = store.node_bm25_search(
        question, pack_ids=pack_ids, spaces=spaces, limit=limit, per_pack=per_pack
    )
    assert result is not None
    return result


def _shape(hits):
    return [(h["node_id"], h["space"], h["node_type"], h["score"], h["pack_id"], h["text"],
             h["properties"]) for h in hits]


_KO = ["건축", "설계", "변경이유", "적용불가", "구조물", "안전관리", "검토의견", "콘크리트"]
_EN = ["beam", "column", "load", "reason", "steel", "frame", "joint", "safety"]


def _corpus(store, rng: random.Random, n: int = 160) -> None:
    packs = ["pa", "pb", "pc"]
    for i in range(n):
        words = [rng.choice(_KO + _EN) for _ in range(rng.randint(1, 30))]
        pid = rng.choice(packs + [packs[0]])
        props = {"text": " ".join(words), "title": rng.choice(_EN), "pack_id": pid}
        if i % 37 == 0:
            props["pack_id"] = ""  # unpackaged
        if i % 41 == 0:
            props["pack_id"] = 7  # truthy non-string
        if i % 43 == 0:
            props["text"] = props["text"] + " " + "건축물 " * 3000  # past the cut
        space = "evidence" if i % 5 else "concept"
        store.upsert_node_doc(space, "Note", f"n{i:04d}", props)
    # same body, different ids
    store.upsert_node_doc("evidence", "Note", "dup-a", {"text": "steel beam", "pack_id": "pa"})
    store.upsert_node_doc("evidence", "Note", "dup-b", {"text": "steel beam", "pack_id": "pa"})
    # malformed properties written by a raw writer
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO doc_nodes(space, node_id, node_type, properties, updated_at) "
            "VALUES ('evidence', 'broken', 'Note', '{not json', '2026-01-01T00:00:00+00:00')"
        )
    remaining = store.node_bm25_sync_pending()
    assert remaining == 0


@pytest.mark.parametrize("seed", [1, 2])
def test_db_index_equals_the_full_memory_index(tmp_path, monkeypatch, seed):
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "4000")
    store = _store(tmp_path)
    rng = random.Random(seed)
    _corpus(store, rng)
    questions = ["건축 설계", "steel beam", "변경 이유 적용", "load load frame", "안전", "7",
                 "joint 콘크리트 구조물", "zzz-none"]
    scopes = [["pa"], ["pb", "pc"], ["pa", "pb", "pc", "7"], ["7"], ["missing"]]
    for q in questions:
        for scope in scopes:
            for spaces in (None, ["concept"], ["evidence", "concept"]):
                for limit in (1, 3, 10, 0, -2):
                    got, warnings = _db(store, q, scope, spaces, limit)
                    want = _oracle(store, q, scope, spaces, limit)
                    assert _shape(got) == _shape(want), (q, scope, spaces, limit)
                    assert warnings == []
            per, _w = _db(store, q, scope, None, 3, per_pack=True)
            for pid in scope:
                assert _shape(per.get(pid, [])) == _shape(_oracle(store, q, [pid], None, 3))


def test_token_round_trip_and_long_token_surrogate(tmp_path):
    store = _store(tmp_path)
    long_a = "가" * 600 + "나"
    long_b = "가" * 600 + "다"
    texts = {
        "x1": "Hello_World foo-bar 123 x_1 가나다라 한국어문장입니다 Ünïcödé İstanbul ß",
        "x2": f"{long_a} tail",
        "x3": f"{long_b} tail",
        "x4": "零一二 مرحبا שלום ①② ３４ _ __ ___x",
    }
    for nid, text in texts.items():
        store.upsert_node_doc("evidence", "Note", nid, {"text": text, "pack_id": "p"})
    conn = store._conn
    for nid in texts:
        key = conn.execute(
            f"SELECT doc_key FROM {nbm.DOCS} WHERE node_id=?", (nid,)
        ).fetchone()[0]
        got = [r[0] for r in conn.execute(
            f"SELECT term FROM {nbm.VINST} WHERE doc=? ORDER BY offset", (key,))]
        node = store.get_node_doc("evidence", nid)
        assert got == nbm.node_terms(node, bm25_mod.max_text_chars_setting())
    # The two long tokens share a 1800-byte prefix but stay two terms.
    for long_tok in (long_a, long_b):
        row = conn.execute(f"SELECT doc FROM {nbm.VROW} WHERE term=?",
                           (nbm.fts_term(long_tok),)).fetchone()
        assert row is not None and row[0] == 1
    hits, _w = _db(store, long_a, ["p"])
    assert hits[0]["node_id"] == "x2"
    assert _shape(hits) == _shape(_oracle(store, long_a, ["p"]))


def test_window_limits_do_not_hide_old_rows(tmp_path, monkeypatch):
    import opencrab.ontology.query as qmod

    monkeypatch.setattr(qmod, "_BM25_NODE_LIMIT", 2)
    monkeypatch.setenv("OPENCRAB_BM25_CHAR_BUDGET", "10")
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "old", {"text": "ancient marker", "pack_id": "a"})
    with store._tx() as conn:
        conn.execute("UPDATE doc_nodes SET updated_at='2000-01-01T00:00:00+00:00'")
    for i in range(20):
        store.upsert_node_doc("evidence", "Note", f"new{i}", {"text": "fresh", "pack_id": "b"})
    hq = _hybrid(store)
    hits, warnings = hq._bm25_search_with_warnings("ancient", None, 10, pack_ids=["a"])
    assert [h["node_id"] for h in hits] == ["old"]
    assert warnings == []
    hq.shutdown_bm25()


def test_store_writes_are_visible_immediately(tmp_path):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "t", {"text": "alpha", "pack_id": "a"})
    assert [h["node_id"] for h in _db(store, "alpha", ["a"])[0]] == ["t"]
    store.upsert_node_doc("evidence", "Note", "t", {"text": "beta", "pack_id": "a"})
    assert _db(store, "alpha", ["a"])[0] == []
    assert [h["node_id"] for h in _db(store, "beta", ["a"])[0]] == ["t"]
    store.upsert_node_doc("evidence", "Note", "t", {"text": "beta", "pack_id": "b"})
    assert _db(store, "beta", ["a"])[0] == []
    assert store.create_node_doc_if_absent("evidence", "Note", "u", {"text": "beta", "pack_id": "b"}) == "created"
    assert sorted(h["node_id"] for h in _db(store, "beta", ["b"])[0]) == ["t", "u"]
    assert store.delete_node_doc("evidence", "t")
    assert [h["node_id"] for h in _db(store, "beta", ["b"])[0]] == ["u"]
    assert store.node_bm25_status()["pending"] == 0


def _raw(store_path, sql, params=()):
    conn = sqlite3.connect(store_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_external_writes_are_drained_by_the_next_search(tmp_path):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "t", {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    _raw(path, "UPDATE doc_nodes SET properties=? WHERE node_id='t'",
         ('{"text": "beta", "pack_id": "a"}',))
    _raw(path, "INSERT INTO doc_nodes VALUES ('evidence','x','Note','{\"text\":\"beta\",\"pack_id\":\"a\"}','2026-01-01')")
    assert store.node_bm25_status()["pending"] == 2
    hits, warnings = _db(store, "beta", ["a"])
    assert sorted(h["node_id"] for h in hits) == ["t", "x"]
    assert warnings == []
    assert store.node_bm25_status()["pending"] == 0


def test_pending_rows_are_excluded_and_flagged(tmp_path):
    """Adversarial controls: moved, deleted and rewritten rows never surface stale."""
    store = _store(tmp_path)
    for nid in ("moved", "gone", "edited", "kept"):
        store.upsert_node_doc("evidence", "Note", nid, {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    _raw(path, "UPDATE doc_nodes SET properties=? WHERE node_id='moved'",
         ('{"text": "alpha", "pack_id": "private"}',))
    _raw(path, "DELETE FROM doc_nodes WHERE node_id='gone'")
    _raw(path, "UPDATE doc_nodes SET properties=? WHERE node_id='edited'",
         ('{"text": "beta", "pack_id": "a"}',))
    held = threading.Event()
    release = threading.Event()

    def hold():
        with store._lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        started = time.monotonic()
        hits, warnings = _db(store, "alpha", ["a"])
        elapsed = time.monotonic() - started
    finally:
        release.set()
        t.join()
    assert elapsed < 2.0
    assert [h["node_id"] for h in hits] == ["kept"]
    assert warnings and warnings[0].startswith("BM25 coverage pending changes: 3 ")
    hits, warnings = _db(store, "alpha", ["a"])
    assert [h["node_id"] for h in hits] == ["kept"]
    assert warnings == []


def test_drain_does_not_wait_for_another_connection_write_lock(tmp_path):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    _raw(path, "UPDATE doc_nodes SET properties=? WHERE node_id='k'",
         ('{"text": "alpha gamma", "pack_id": "a"}',))
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        hits, warnings = _db(store, "alpha", ["a"])
        elapsed = time.monotonic() - started
    finally:
        other.rollback()
        other.close()
    assert elapsed < 2.0
    assert hits == []
    assert warnings and warnings[0].startswith("BM25 coverage pending changes")


def test_snapshot_sees_a_write_that_lands_after_the_drain(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    real = store._node_bm25_try_drain

    def drain_then_write():
        real()
        _raw(path, "INSERT INTO doc_nodes VALUES ('evidence','late','Note','{\"text\":\"alpha\",\"pack_id\":\"a\"}','2026-01-01')")

    monkeypatch.setattr(store, "_node_bm25_try_drain", drain_then_write)
    hits, warnings = _db(store, "alpha", ["a"])
    assert [h["node_id"] for h in hits] == ["k"]
    assert warnings and warnings[0].startswith("BM25 coverage pending changes: 1 ")


def test_snapshot_falls_back_when_another_process_resets(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    real = store._node_bm25_try_drain

    def drain_then_reset():
        real()
        _raw(path, f"UPDATE {nbm.META} SET value='backfill' WHERE key='state'")

    monkeypatch.setattr(store, "_node_bm25_try_drain", drain_then_reset)
    assert store.node_bm25_search("alpha", pack_ids=["a"]) is None
    hq = _hybrid(store)
    hits, _w = hq._bm25_search_with_warnings("alpha", None, 10, pack_ids=["a"])
    assert [h["node_id"] for h in hits] == ["k"]  # memory path
    hq.shutdown_bm25()


def test_auto_pack_probe_forwards_pending_warning(tmp_path):
    from opencrab.ontology.pack_registry import PackInfo, _choose_by_content_with_warnings
    from opencrab.services import pack_selection as ps

    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "qzxmarker", "pack_id": "a"})
    _raw(str(tmp_path / "doc.db"), "UPDATE doc_nodes SET properties=? WHERE node_id='k'",
         ('{"text": "qzxmarker two", "pack_id": "a"}',))
    other = sqlite3.connect(str(tmp_path / "doc.db"), timeout=0)
    other.execute("BEGIN IMMEDIATE")
    try:
        hq = _hybrid(store)
        _c, _t, warnings = _choose_by_content_with_warnings(
            "qzxmarker", [PackInfo(pack_id="a")], hq, None)
    finally:
        other.rollback()
        other.close()
    assert warnings and warnings[0].startswith("BM25 coverage pending changes")
    code = ps.AUTO_PACK_PROBE_COVERAGE
    assert ps.mcp_warning_text(ps.PackWarning(code))
    assert ps.cli_warning_text(ps.PackWarning(code))
    hq.shutdown_bm25()


def test_scope_and_top_k_against_many_out_of_scope_hits(tmp_path):
    store = _store(tmp_path)
    for i in range(60):
        store.upsert_node_doc("evidence", "Note", f"x{i}", {"text": "target " * 5, "pack_id": "other"})
    store.upsert_node_doc("evidence", "Note", "mine", {"text": "target noise noise", "pack_id": "mine"})
    store.upsert_node_doc("concept", "Note", "mine-c", {"text": "target", "pack_id": "mine"})
    hits, _w = _db(store, "target", ["mine"], limit=1)
    assert [h["node_id"] for h in hits] == ["mine-c"]
    hits, _w = _db(store, "target", ["mine"], spaces=["evidence"], limit=5)
    assert [h["node_id"] for h in hits] == ["mine"]
    assert _db(store, "target", [])[0] == []


def test_reindex_failure_keeps_the_document_write(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(nbm, "reindex_key", boom)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    assert store.get_node_doc("evidence", "k") is not None
    assert store.node_bm25_status()["pending"] == 1
    monkeypatch.undo()
    hits, _w = _db(store, "alpha", ["a"])
    assert [h["node_id"] for h in hits] == ["k"]


def test_ddl_failure_leaves_no_index_object(tmp_path, monkeypatch):
    real = nbm.ddl_statements

    def failing():
        stmts = real()
        return stmts[:4] + ["CREATE VIRTUAL TABLE bad USING fts5(x, bogus_option=1)"] + stmts[4:]

    monkeypatch.setattr(nbm, "ddl_statements", failing)
    store = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert store.available and not store.supports_node_bm25
    names = [r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'doc_nodes_bm25%' OR name='bad'")]
    assert names == []
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    hq = _hybrid(store)
    hits, _w = hq._bm25_search_with_warnings("alpha", None, 10, pack_ids=["a"])
    assert [h["node_id"] for h in hits] == ["k"]  # memory path
    hq.shutdown_bm25()


def _drop_and_reopen(tmp_path, store) -> LocalSQLDocStore:
    store.node_bm25_drop()
    names = [r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'doc_nodes_bm25%'")]
    assert names == []
    return LocalSQLDocStore(str(tmp_path / "doc.db"))


def test_existing_rows_start_at_backfill_and_resume_after_failure(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for i in range(12):
        store.upsert_node_doc("evidence", "Note", f"n{i:02d}", {"text": f"word{i} common", "pack_id": "a"})
    store = _drop_and_reopen(tmp_path, store)
    assert store.node_bm25_status()["state"] == "backfill"
    assert store.node_bm25_search("common", pack_ids=["a"]) is None
    hq = _hybrid(store)
    hits, _w = hq._bm25_search_with_warnings("word3", None, 10, pack_ids=["a"])
    assert [h["node_id"] for h in hits] == ["n03"]  # memory path during backfill
    hq.shutdown_bm25()

    assert store.node_bm25_backfill_step(5)["processed"] == 5
    before = store.node_bm25_status()
    real = nbm.reindex_key
    calls = {"n": 0}

    def flaky(conn, row_to_node, space, node_id, meta=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected")
        return real(conn, row_to_node, space, node_id, meta)

    monkeypatch.setattr(nbm, "reindex_key", flaky)
    with pytest.raises(nbm.ReindexError):
        store.node_bm25_backfill_step(5)
    assert store.node_bm25_status() == before
    monkeypatch.undo()
    while store.node_bm25_backfill_step(5)["state"] != "ready":
        pass
    assert store.node_bm25_verify()["ok"] is True
    for q in ("common", "word7"):
        assert _shape(_db(store, q, ["a"])[0]) == _shape(_oracle(store, q, ["a"]))


def test_format_mismatch_blocks_cursor_ready_and_search(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        store.upsert_node_doc("evidence", "Note", f"n{i}", {"text": "alpha", "pack_id": "a"})
    store = _drop_and_reopen(tmp_path, store)
    with store._tx() as conn:
        conn.execute(f"UPDATE {nbm.META} SET value='999' WHERE key='format'")
    before = store.node_bm25_status()
    with pytest.raises(nbm.IndexFormatMismatchError):
        store.node_bm25_backfill_step(10)
    assert store.node_bm25_status() == before
    # the final, keyless step is guarded as well
    with store._tx() as conn:
        conn.execute(f"UPDATE {nbm.META} SET value='n9' WHERE key='cursor_node_id'")
        conn.execute(f"UPDATE {nbm.META} SET value='evidence' WHERE key='cursor_space'")
    with pytest.raises(nbm.IndexFormatMismatchError):
        store.node_bm25_backfill_step(10)
    assert store.node_bm25_status()["state"] == "backfill"
    _raw(str(tmp_path / "doc.db"), "UPDATE doc_nodes SET properties=? WHERE node_id='n0'",
         ('{"text": "beta", "pack_id": "a"}',))
    with pytest.raises(nbm.IndexFormatMismatchError):
        store.node_bm25_sync_pending()
    assert store.node_bm25_status()["pending"] >= 1
    assert not store.node_bm25_ready()


def test_reopen_resets_on_config_change_and_row_count_drift(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "a1", {"text": "alpha", "pack_id": "a"})
    monkeypatch.setenv("OPENCRAB_BM25_MAX_NODE_CHARS", "100")
    store = LocalSQLDocStore(str(tmp_path / "doc.db"))
    assert store.node_bm25_status()["state"] == "backfill"
    while store.node_bm25_backfill_step(10)["state"] != "ready":
        pass
    # orphan entry and row count drift: the triggers were missing for a delete
    with store._tx() as conn:
        conn.execute(f"DROP TRIGGER {nbm.TRIGGERS[2]}")
        conn.execute("DELETE FROM doc_nodes WHERE node_id='a1'")
    store = LocalSQLDocStore(str(tmp_path / "doc.db"))
    status = store.node_bm25_status()
    assert status["state"] == "backfill" and status["doc_count"] == 0
    while store.node_bm25_backfill_step(10)["state"] != "ready":
        pass
    assert store.node_bm25_verify()["ok"] is True


def test_verify_detects_swapped_term_frequencies(tmp_path):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "d1", {"text": "alpha alpha beta", "pack_id": "a"})
    store.upsert_node_doc("evidence", "Note", "d2", {"text": "alpha beta beta", "pack_id": "a"})
    assert store.node_bm25_verify()["ok"] is True
    conn = store._conn
    keys = {n: k for k, n in conn.execute(f"SELECT doc_key, node_id FROM {nbm.DOCS}")}
    t1 = nbm.node_terms(store.get_node_doc("evidence", "d1"), bm25_mod.max_text_chars_setting())
    t2 = nbm.node_terms(store.get_node_doc("evidence", "d2"), bm25_mod.max_text_chars_setting())
    with store._tx() as c:
        c.execute(f"DELETE FROM {nbm.FTS} WHERE rowid IN (?, ?)", (keys["d1"], keys["d2"]))
        c.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (?, ?)", (keys["d1"], " ".join(t2)))
        c.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (?, ?)", (keys["d2"], " ".join(t1)))
    report = store.node_bm25_verify()
    assert report["ok"] is False and report["mismatched_terms"] == 2


def test_trigger_forms_record_keys_without_failing(tmp_path):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "a", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    with store._tx() as conn:  # leave a pending row for the same key first
        conn.execute("UPDATE doc_nodes SET node_type='X' WHERE node_id='k'")
    _raw(path, "INSERT INTO doc_nodes VALUES ('evidence','k','Note','{}','1') "
               "ON CONFLICT(space, node_id) DO UPDATE SET updated_at=excluded.updated_at")
    _raw(path, "INSERT OR REPLACE INTO doc_nodes VALUES ('evidence','k','Note','{}','2')")
    _raw(path, "UPDATE doc_nodes SET space='concept' WHERE node_id='k'")
    keys = sorted(tuple(r) for r in store._conn.execute(f"SELECT space, node_id FROM {nbm.PENDING}"))
    assert keys == [("concept", "k"), ("evidence", "k")]


def test_two_instances_on_one_file_share_the_index(tmp_path):
    a = _store(tmp_path)
    b = LocalSQLDocStore(str(tmp_path / "doc.db"))
    b.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    assert [h["node_id"] for h in _db(a, "alpha", ["a"])[0]] == ["k"]


def test_kill_switch_and_skipped_invalidation_keep_the_memory_path_fresh(tmp_path, monkeypatch):
    """Design 4.8: an equal-fingerprint edit with invalidate() must reach the memory index."""
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "target", {"text": "alpha", "pack_id": "a"})
    store.upsert_node_doc("evidence", "Note", "other", {"text": "gamma", "pack_id": "a"})
    with store._tx() as conn:
        conn.execute("UPDATE doc_nodes SET updated_at='2099-01-01T00:00:00+00:00' WHERE node_id='other'")
    store.node_bm25_sync_pending()

    def wait_for(hq, word, expected):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            got = [h["node_id"] for h in hq._bm25_search_with_warnings(word, None, 10, pack_ids=["a"])[0]]
            if got == expected:
                return got
            time.sleep(0.1)
        return got

    # 1) ready + kill switch
    monkeypatch.setenv("OPENCRAB_BM25_DB_INDEX", "0")
    hq = _hybrid(store)
    assert wait_for(hq, "alpha", ["target"]) == ["target"]
    fp = store.bm25_fingerprint()
    store.upsert_node_doc("evidence", "Note", "target", {"text": "beta", "pack_id": "a"})
    assert store.bm25_fingerprint() == fp
    hq.invalidate_bm25_cache()
    assert wait_for(hq, "beta", ["target"]) == ["target"]
    assert wait_for(hq, "alpha", []) == []
    hq.shutdown_bm25()

    # 2) DB path skipped the invalidation, then the memory path is used again
    monkeypatch.delenv("OPENCRAB_BM25_DB_INDEX")
    hq = _hybrid(store)
    monkeypatch.setenv("OPENCRAB_BM25_DB_INDEX", "0")
    assert wait_for(hq, "beta", ["target"]) == ["target"]
    monkeypatch.delenv("OPENCRAB_BM25_DB_INDEX")
    store.upsert_node_doc("evidence", "Note", "target", {"text": "delta", "pack_id": "a"})
    hq.invalidate_bm25_cache()
    assert hq._bm25_invalidation_skipped is True
    monkeypatch.setenv("OPENCRAB_BM25_DB_INDEX", "0")
    assert wait_for(hq, "delta", ["target"]) == ["target"]
    hq.shutdown_bm25()


def test_cli_status_backfill_verify_drop(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from opencrab import cli as cli_mod

    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})

    class _Stores:
        doc = store

    monkeypatch.setattr(cli_mod, "_make_stores", lambda *a, **k: _Stores())
    runner = CliRunner()
    for args in (["status"], ["backfill", "--batch", "2"], ["verify"]):
        result = runner.invoke(cli_mod.main, ["packs", "bm25-index", *args])
        assert result.exit_code == 0, result.output
    assert runner.invoke(cli_mod.main, ["packs", "bm25-index", "drop"]).exit_code == 1
    result = runner.invoke(cli_mod.main, ["packs", "bm25-index", "drop", "--yes"])
    assert result.exit_code == 0, result.output
    assert not store.supports_node_bm25


def test_final_backfill_step_drains_pending_before_ready(tmp_path, monkeypatch):
    """Design 5.3: a failing pending key keeps the index in backfill."""
    store = _store(tmp_path)
    for i in range(4):
        store.upsert_node_doc("evidence", "Note", f"n{i}", {"text": "alpha", "pack_id": "a"})
    store = _drop_and_reopen(tmp_path, store)
    assert store.node_bm25_backfill_step(10)["processed"] == 4
    _raw(str(tmp_path / "doc.db"), "UPDATE doc_nodes SET properties=? WHERE node_id='n0'",
         ('{"text": "beta", "pack_id": "a"}',))
    real = nbm.reindex_key

    def fail_n0(conn, row_to_node, space, node_id, meta=None):
        if node_id == "n0":
            raise RuntimeError("injected pending failure")
        return real(conn, row_to_node, space, node_id, meta)

    monkeypatch.setattr(nbm, "reindex_key", fail_n0)
    with pytest.raises(nbm.ReindexError):
        store.node_bm25_backfill_step(10)
    status = store.node_bm25_status()
    assert status["state"] == "backfill" and status["pending"] == 1
    assert store.node_bm25_search("beta", pack_ids=["a"]) is None
    monkeypatch.undo()
    step = store.node_bm25_backfill_step(10)
    assert step["state"] == "ready" and step["pending_drained"] == 1
    assert store.node_bm25_status()["pending"] == 0
    assert store.node_bm25_verify()["ok"] is True


def _drop_through_another_connection(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        nbm.drop(conn)
        conn.commit()
    finally:
        conn.close()


def test_drop_after_entry_check_before_drain_uses_the_memory_path(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    real_ready = store.node_bm25_ready

    def ready_then_drop():
        ready = real_ready()
        _drop_through_another_connection(path)
        return ready

    monkeypatch.setattr(store, "node_bm25_ready", ready_then_drop)
    hq = _hybrid(store)
    hits, warnings = hq._bm25_search_with_warnings("alpha", None, 10, pack_ids=["a"])
    assert [h["node_id"] for h in hits] == ["k"]
    assert not any("unavailable" in w for w in warnings)
    hq.shutdown_bm25()


def test_drop_after_drain_before_snapshot_uses_the_memory_path(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_node_doc("evidence", "Note", "k", {"text": "alpha", "pack_id": "a"})
    path = str(tmp_path / "doc.db")
    real = store._node_bm25_try_drain

    def drain_then_drop():
        real()
        _drop_through_another_connection(path)

    monkeypatch.setattr(store, "_node_bm25_try_drain", drain_then_drop)
    assert store.node_bm25_search("alpha", pack_ids=["a"]) is None
    hq = _hybrid(store)
    hits, warnings = hq._bm25_search_with_warnings("alpha", None, 10, pack_ids=["a"])
    assert [h["node_id"] for h in hits] == ["k"]
    assert not any("unavailable" in w for w in warnings)
    hq.shutdown_bm25()
