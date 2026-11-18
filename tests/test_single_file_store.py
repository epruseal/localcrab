"""단일 파일 공유 연결과 트랜잭션 구간 (#671 시제품, 기본 꺼짐).

``SharedSqliteFile``을 쓰는 세 저장소(그래프, doc, 벡터)가 연결 하나와 쓰기 락 하나를 나눠 쓴다.
``unit_of_work()`` 안의 쓰기는 일반 테이블, fts5, vec0를 가리지 않고 한 트랜잭션으로 커밋되거나
롤백된다. ``shared`` 인자가 없으면 종전 동작이다(기존 저장소 시험이 그 경로를 이미 건다).
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading

import pytest
import sqlite_vec

from opencrab.stores._single_file import SharedSqliteFile, unit_of_work_for
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sqlite_vec_store import SqliteVecStore

DIM = 4


def _embed(texts):
    return [[float(len(t) % 5), 1.0, 0.0, 0.5] for t in texts]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    graph = LocalGraphStore(path, shared=shared)
    docs = LocalSQLDocStore(path, shared=shared)
    vec = SqliteVecStore(path, embedding_function=_embed, dim=DIM, shared=shared)
    yield path, shared, graph, docs, vec
    for store in (graph, docs, vec):
        store.close()


def _counts(path: str) -> tuple[int, int, int]:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        return (
            conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0],
            conn.execute("SELECT count(*) FROM doc_sources_fts").fetchone()[0],
            conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0],
        )
    finally:
        conn.close()


def _write_pair(docs, vec, sid="s1"):
    vec.upsert_texts(texts=["본문"], ids=[sid], metadatas=[{"pack_id": "p"}])
    docs.upsert_source(sid, "본문", {"pack_id": "p"})


def test_stores_share_one_file_and_one_connection(stores):
    path, shared, graph, docs, vec = stores
    assert graph.shared_file is docs.shared_file is vec.shared_file is shared
    assert graph._conn is docs._conn is vec._conn
    assert shared.db_path == path


def test_vec0_extension_is_usable_even_though_graph_store_opened_the_connection_first(stores):
    _path, _shared, _graph, _docs, vec = stores
    assert vec.available
    vec.upsert_texts(texts=["a"], ids=["x"], metadatas=[{"pack_id": "p"}])
    assert vec.count() == 1


def test_commit_makes_all_three_table_kinds_visible(stores):
    path, shared, _graph, docs, vec = stores
    with shared.unit_of_work():
        _write_pair(docs, vec)
        # 구간 안에서는 아직 다른 연결에 보이지 않는다.
        assert _counts(path) == (0, 0, 0)
    assert _counts(path) == (1, 1, 1)


def test_exception_rolls_back_regular_fts5_and_vec0_together(stores):
    path, shared, _graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            _write_pair(docs, vec)
            raise RuntimeError("boom")
    assert _counts(path) == (0, 0, 0)
    # 롤백 뒤에도 같은 연결로 다음 쓰기가 된다.
    _write_pair(docs, vec, "s2")
    assert _counts(path) == (1, 1, 1)


def test_inner_failure_rolls_back_only_its_savepoint(stores):
    path, shared, _graph, docs, vec = stores
    with shared.unit_of_work():
        _write_pair(docs, vec, "keep")
        with contextlib.suppress(sqlite3.IntegrityError):
            # 같은 구간 안의 _tx()가 자기 SAVEPOINT만 되돌린다.
            docs._exec_write_many([
                ("INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
                 "VALUES ('dup', 't', '{}', 'now')", {}),
                ("INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
                 "VALUES ('dup', 't', '{}', 'now')", {}),
            ])
    assert _counts(path)[0] == 1, "안쪽 실패의 부분 쓰기가 남았거나 바깥 쓰기가 사라졌다"


def test_nested_unit_of_work_joins_the_outer_one(stores):
    path, shared, _graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            with shared.unit_of_work():
                _write_pair(docs, vec)
            assert _counts(path) == (0, 0, 0), "안쪽 구간이 따로 커밋했다"
            raise RuntimeError("boom")
    assert _counts(path) == (0, 0, 0)


def test_unit_of_work_refuses_to_start_inside_an_open_transaction(stores):
    _path, shared, _graph, docs, _vec = stores
    conn = docs._conn
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="open transaction"):
            with shared.unit_of_work():
                pass
    finally:
        conn.rollback()


def test_close_of_one_store_keeps_the_connection_for_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    docs = LocalSQLDocStore(path, shared=shared)
    vec = SqliteVecStore(path, embedding_function=_embed, dim=DIM, shared=shared)
    docs.close()
    assert vec.ping()
    vec.upsert_texts(texts=["a"], ids=["x"], metadatas=[{"pack_id": "p"}])
    vec.close()
    assert shared.all_conns == []


def test_unit_of_work_for_is_a_noop_without_a_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    vec = SqliteVecStore(str(tmp_path / "vec.db"), embedding_function=_embed, dim=DIM)
    try:
        assert docs.shared_file is None
        assert isinstance(unit_of_work_for(vec, docs), contextlib.nullcontext)
    finally:
        docs.close()
        vec.close()


def test_unit_of_work_for_requires_the_same_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    a = SharedSqliteFile(str(tmp_path / "a.db"))
    b = SharedSqliteFile(str(tmp_path / "b.db"))
    docs = LocalSQLDocStore(str(tmp_path / "a.db"), shared=a)
    vec = SqliteVecStore(str(tmp_path / "b.db"), embedding_function=_embed, dim=DIM, shared=b)
    try:
        assert isinstance(unit_of_work_for(vec, docs), contextlib.nullcontext)
    finally:
        docs.close()
        vec.close()


def test_other_thread_sees_only_committed_state_during_the_unit(stores):
    path, shared, _graph, docs, vec = stores
    seen: list[tuple[int, int, int]] = []
    started = threading.Event()
    release = threading.Event()

    def reader():
        started.wait(5)
        seen.append(_counts(path))
        release.set()

    t = threading.Thread(target=reader)
    t.start()
    with shared.unit_of_work():
        _write_pair(docs, vec)
        started.set()
        assert release.wait(10)
    t.join()
    assert seen == [(0, 0, 0)]
    assert _counts(path) == (1, 1, 1)


def test_large_batch_rollback_crosses_vec0_chunk_and_fts_flush_boundaries(stores):
    """vec0 청크 경계와 fts5 pending 해시 flush를 넘는 크기도 구간 롤백이 전부 되돌린다."""
    path, shared, _graph, docs, vec = stores
    n = 1200
    ids = [f"big-{i}" for i in range(n)]
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            vec.upsert_texts(texts=ids, ids=ids, metadatas=[{"pack_id": "p"}] * n)
            for sid in ids:
                docs.upsert_source(sid, f"본문 {sid} alpha", {"pack_id": "p"})
            raise RuntimeError("boom")
    assert _counts(path) == (0, 0, 0)
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        conn.execute("INSERT INTO doc_sources_fts(doc_sources_fts) VALUES('integrity-check')")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM vectors_kure WHERE embedding MATCH ? AND k=3",
                            (sqlite_vec.serialize_float32([1.0, 1.0, 0.0, 0.5]),)).fetchone()[0] == 0
    finally:
        conn.close()


def test_exclusive_transaction_inside_a_unit_is_refused(stores):
    _path, shared, graph, _docs, _vec = stores
    with shared.unit_of_work():
        with pytest.raises(RuntimeError, match="exclusive"):
            with graph._tx(exclusive=True):
                pass


def test_immediate_transaction_inside_a_unit_joins_it(stores):
    path, shared, graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            with graph._tx(immediate=True) as conn:
                conn.execute("INSERT INTO graph_nodes(node_type, node_id, space_id, properties) "
                             "VALUES ('t', 'g1', 's', '{}')")
            raise RuntimeError("boom")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='g1'").fetchone()[0] == 0
    finally:
        conn.close()


def test_wal_is_checkpointed_after_a_unit_that_grows_it_past_the_threshold(stores, monkeypatch):
    path, shared, graph, docs, vec = stores
    monkeypatch.setattr(type(graph), "_WAL_CHECKPOINT_THRESHOLD_BYTES", 64 * 1024)
    with shared.unit_of_work():
        for i in range(400):
            docs.upsert_source(f"w{i}", "x" * 400, {"pack_id": "p"})
    import os

    assert os.path.getsize(path + "-wal") == 0


def test_lower_udf_is_consistent_on_the_shared_connection(stores):
    """graph 저장소가 등록한 ``lower`` UDF가 공유 연결에서 벡터와 doc 저장소의 질의에 영향을 주지 않는다."""
    _path, _shared, _graph, docs, vec = stores
    vec.upsert_texts(texts=["a"], ids=["x"], metadatas=[{"pack_id": "P"}])
    assert vec.count() == 1
    # SQLite 내장 lower는 ASCII만 바꾼다. 비ASCII 대문자가 바뀌어야 UDF가 이 연결에 걸렸다는 증거다.
    assert vec._conn.execute("SELECT lower('Ä')").fetchone()[0] == "ä"
    assert docs._conn.execute("SELECT lower('Ä')").fetchone()[0] == "ä"
