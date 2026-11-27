"""``load_chunks``의 단일 파일 경로: 쓰기 예외 주입 (#671).

벡터와 doc 저장소가 한 ``SharedSqliteFile``을 나눠 쓰면 ``load_chunks``는 배치(와 건별 재시도)를 한
트랜잭션에 넣고 예외를 삼키지 않는다. 한 건의 쓰기가 실패하면 그 건은 벡터 행도 doc 행도 남지
않는다. 다섯 파일 구조(대조군)는 doc 쓰기 실패 건에 벡터 행이 남는다(현재 동작).

시험 DB의 초기 벡터와 doc 행은 0이다. 배치 크기 B=8, 총 청크 N=19로 가득 찬 배치 둘과 부분 배치를 덮는다.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import create_pack
from opencrab.stores._single_file import SharedSqliteFile
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from opencrab.stores.sqlite_vec_store import SqliteVecStore

B = 8
N = 2 * B + 3
DIM = 4
PACK = "inject-pack"


def _embed(texts):
    return [[float(len(t) % 5), 1.0, 0.0, 0.5] for t in texts]


class _Env:
    def __init__(self, layout: str, d: Path) -> None:
        self.layout = layout
        self.d = d
        if layout == "single":
            from scripts.probe_single_db_crash import _supported_single_fixture

            self.doc_path = self.vec_path = _supported_single_fixture(d, dim=DIM)
            shared = SharedSqliteFile(str(self.doc_path))
            kw = {"shared": shared}
        else:
            self.doc_path, self.vec_path = d / "doc_store.db", d / "vectors.db"
            kw = {}
        self.docs = LocalSQLDocStore(str(self.doc_path), **kw)
        self.vec = SqliteVecStore(str(self.vec_path), embedding_function=_embed, dim=DIM, **kw)
        sql_path = self.doc_path if layout == "single" else d / "opencrab.db"
        self.sql = SQLStore(f"sqlite:///{sql_path}", **kw)
        self.user_id = create_user(self.sql, "owner", is_local=True)
        create_pack(self.sql, self.user_id, PACK)
        self.chunks = d / "chunks.jsonl"
        with self.chunks.open("w", encoding="utf-8") as fh:
            for i in range(N):
                fh.write(json.dumps({"id": f"c{i}", "text": f"본문 {i} alpha", "document_id": f"d{i}",
                                     "metadata": {}}, ensure_ascii=False) + "\n")

    def run(self) -> tuple[int, int]:
        principal = Principal(user_id=self.user_id, is_local=True, disabled=False)
        with principal_scope(principal):
            return pack_load.load_chunks(PACK, self.chunks, self.vec, self.docs, batch_size=B, sql=self.sql)

    def ids(self) -> tuple[set[str], set[str]]:
        def connect(path):
            conn = sqlite3.connect(str(path))
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            return conn

        v = connect(self.vec_path)
        d = connect(self.doc_path)
        try:
            vec_ids = {r[0] for r in v.execute("SELECT node_id FROM vectors_kure")}
            doc_ids = {r[0] for r in d.execute("SELECT source_id FROM doc_sources")}
        finally:
            v.close()
            d.close()
        return vec_ids, doc_ids

    def inject_doc_error(self, bad: str) -> None:
        original = self.docs.upsert_source

        def wrapped(source_id, text, metadata):
            if source_id == bad:
                raise RuntimeError("injected doc write failure")
            return original(source_id, text, metadata)

        self.docs.upsert_source = wrapped  # type: ignore[method-assign]

    def inject_vector_error(self, bad: str) -> None:
        original = self.vec.upsert_texts
        original_precomputed = self.vec.upsert_precomputed

        def wrapped(texts, metadatas=None, ids=None):
            if ids and bad in ids:
                raise RuntimeError("injected vector write failure")
            return original(texts=texts, metadatas=metadatas, ids=ids)

        def wrapped_precomputed(texts, metadatas, ids, vectors):
            if bad in ids:
                raise RuntimeError("injected vector write failure")
            return original_precomputed(texts, metadatas, ids, vectors)

        self.vec.upsert_texts = wrapped  # type: ignore[method-assign]
        self.vec.upsert_precomputed = wrapped_precomputed  # type: ignore[method-assign]

    def close(self) -> None:
        self.docs.close()
        self.vec.close()
        self.sql.close()


@pytest.fixture(params=["single", "five"])
def env(request, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    e = _Env(request.param, tmp_path)
    yield e
    e.close()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("failure", ["begin", "rollback", "commit_before", "commit_after"])
def test_chunk_transaction_failure_never_retries_unknown(tmp_path, monkeypatch, incremental, failure):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    shared = env.vec.shared_file
    conn = shared.connection()
    calls = []

    class ConnectionProxy:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE" and failure == "begin":
                raise RuntimeError("begin failure")
            return conn.execute(sql, *args)

        def rollback(self):
            if failure == "rollback":
                raise RuntimeError("rollback failure")
            return conn.rollback()

        def commit(self):
            if failure == "commit_before":
                raise RuntimeError("commit failure")
            conn.commit()
            if failure == "commit_after":
                raise RuntimeError("commit response lost")

    proxy = ConnectionProxy()
    original_prepare = env.vec.prepare_upsert_vectors

    def prepare(texts):
        calls.append(len(texts))
        result = original_prepare(texts)
        monkeypatch.setattr(shared, "connection", lambda: proxy)
        return result

    if failure == "rollback":
        env.inject_doc_error("c3")
    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", prepare)
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        expected_error = RuntimeError if failure == "begin" else pack_load.ChunkTransactionUncertainError
        with principal_scope(principal), pytest.raises(expected_error) as caught:
            if incremental:
                pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {}, batch_size=B, sql=env.sql)
            else:
                pack_load.load_chunks(PACK, env.chunks, env.vec, env.docs, batch_size=B, sql=env.sql)
        if failure == "begin":
            assert type(caught.value) is RuntimeError
            assert str(caught.value) == "begin failure"
        assert calls == [B]
        conn.rollback()
        vec_ids, doc_ids = env.ids()
        expected = {f"c{i}" for i in range(B)} if failure == "commit_after" else set()
        assert vec_ids == doc_ids == expected
    finally:
        conn.rollback()
        env.close()


@pytest.mark.parametrize("incremental", [False, True])
def test_single_retry_commit_unknown_propagates(tmp_path, monkeypatch, incremental):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    conn = env.vec.shared_file.connection()
    calls = []

    class ConnectionProxy:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            conn.commit()
            raise RuntimeError("single commit response lost")

    original = env.docs.upsert_source

    def fail_batch(source_id, text, metadata):
        calls.append(source_id)
        if source_id == "c3":
            raise RuntimeError("batch failure")
        return original(source_id, text, metadata)

    prepare = env.vec.prepare_upsert_vectors

    def armed_prepare(texts):
        result = prepare(texts)
        monkeypatch.setattr(env.vec.shared_file, "connection", lambda: ConnectionProxy())
        return result

    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", armed_prepare)
    monkeypatch.setattr(env.docs, "upsert_source", fail_batch)
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with principal_scope(principal), pytest.raises(pack_load.ChunkTransactionUncertainError):
            if incremental:
                pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {}, batch_size=B, sql=env.sql)
            else:
                pack_load.load_chunks(PACK, env.chunks, env.vec, env.docs, batch_size=B, sql=env.sql)
        assert calls == ["c0", "c1", "c2", "c3", "c0"]
        assert env.ids() == ({"c0"}, {"c0"})
    finally:
        env.close()


def test_incremental_metadata_rollback_failure_propagates(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        assert env.run() == (N, 0)
        live = {f"c{i}": (f"본문 {i} alpha", {"pack_id": PACK, "old": "value"}) for i in range(N)}
        conn = env.vec.shared_file.connection()

        class ConnectionProxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def rollback(self):
                raise RuntimeError("rollback failure")

        def fail(*_args):
            raise RuntimeError("metadata rewrite failed")

        monkeypatch.setattr(env.vec.shared_file, "connection", lambda: ConnectionProxy())
        monkeypatch.setattr(env.docs, "rewrite_source_metadata", fail)
        with principal_scope(principal), pytest.raises(pack_load.ChunkTransactionUncertainError):
            pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, live, sql=env.sql)
        conn.rollback()
    finally:
        env.close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_chunk_post_commit_callback_failure(tmp_path, monkeypatch, interrupt):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    original = env.docs.upsert_source
    calls = []

    def upsert(source_id, text, metadata):
        original(source_id, text, metadata)
        if not calls:
            calls.append(source_id)
            def callback():
                if interrupt:
                    raise KeyboardInterrupt()
                raise RuntimeError("callback failure")
            env.vec.shared_file.after_commit(callback)

    monkeypatch.setattr(env.docs, "upsert_source", upsert)
    try:
        if interrupt:
            with pytest.raises(KeyboardInterrupt):
                env.run()
            expected = {f"c{i}" for i in range(B)}
        else:
            assert env.run() == (N, 0)
            expected = {f"c{i}" for i in range(N)}
        assert env.ids() == (expected, expected)
    finally:
        env.close()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("input_kind", ["empty", "missing", "malformed", "missing_key"])
def test_outer_uow_rejection_precedes_input(tmp_path, monkeypatch, incremental, input_kind):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    path = tmp_path / "input.jsonl"
    if input_kind != "missing":
        path.write_text({"empty": "", "malformed": "{\n", "missing_key": "{}\n"}[input_kind])
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with principal_scope(principal), env.vec.shared_file.unit_of_work():
            with pytest.raises(RuntimeError, match="chunk loader cannot run inside"):
                if incremental:
                    pack_load.load_chunks_incremental(PACK, path, env.vec, env.docs, {}, sql=env.sql)
                else:
                    pack_load.load_chunks(PACK, path, env.vec, env.docs, sql=env.sql)
        assert env.ids() == (set(), set())
    finally:
        env.close()


def test_chunk_rejects_registry_outside_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    shared_sql = env.sql
    (tmp_path / "separate").mkdir()  # root 밖 디렉터리여야 B11의 root 검사에 걸리지 않는다
    env.sql = SQLStore(f"sqlite:///{tmp_path / 'separate' / 'separate.db'}")
    user = create_user(env.sql, "owner", is_local=True)
    create_pack(env.sql, user, PACK)
    env.user_id = user
    try:
        with pytest.raises(RuntimeError, match="shared"):
            env.run()
        assert env.ids() == (set(), set())
    finally:
        env.close()
        shared_sql.close()


@pytest.mark.parametrize("incremental", [False, True])
def test_chunks_reject_pending_deletion_during_embedding(tmp_path, monkeypatch, incremental):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    incarnation = env.vec.shared_file.pack_incarnation(PACK)
    prepare = env.vec.prepare_upsert_vectors
    reserved = False

    def reserve(texts):
        nonlocal reserved
        vectors = prepare(texts)
        if not reserved:
            with sqlite3.connect(env.doc_path) as other:
                other.execute("INSERT INTO pack_delete_operations(operation_id, pack_id, incarnation, state) "
                              "VALUES ('op', ?, ?, 'pending')", (PACK, incarnation))
            reserved = True
        return vectors

    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", reserve)
    try:
        if incremental:
            principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
            with principal_scope(principal):
                result = pack_load.load_chunks_incremental(
                    PACK, env.chunks, env.vec, env.docs, {}, batch_size=B, sql=env.sql)
            assert result[:5] == (0, 0, 0, 0, N)
        else:
            assert env.run() == (0, N)
        assert env.ids() == (set(), set())
    finally:
        env.close()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("rollback", [False, True])
def test_chunks_reject_outer_shared_uow(tmp_path, monkeypatch, incremental, rollback):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with principal_scope(principal):
            try:
                with env.vec.shared_file.unit_of_work():
                    with pytest.raises(RuntimeError, match="chunk loader cannot run inside"):
                        if incremental:
                            pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {}, sql=env.sql)
                        else:
                            pack_load.load_chunks(PACK, env.chunks, env.vec, env.docs, sql=env.sql)
                    if rollback:
                        raise ValueError("outer rollback")
            except ValueError:
                assert rollback
        assert env.ids() == (set(), set())
    finally:
        env.close()


def test_short_embedding_batch_counts_preparation_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    def embed(texts):
        vectors = _embed(texts)
        return vectors[:-1] if len(texts) > 1 else vectors
    env.vec._ef = embed
    try:
        assert env.run() == (0, N)
        assert env.ids() == (set(), set())
    finally:
        env.close()


def test_single_file_embedding_precedes_transaction_and_reuses_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    calls = []
    def embed(texts):
        assert not env.vec.shared_file.in_unit_of_work
        calls.append(tuple(texts))
        return _embed(texts)
    env.vec._ef = embed
    env.inject_doc_error("c3")
    try:
        assert env.run() == (N - 1, 1)
        assert len(calls) == (N + B - 1) // B
    finally:
        env.close()


def test_incremental_metadata_doc_failure_rolls_back_vector(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        assert env.run() == (N, 0)
        live = {f"c{i}": (f"본문 {i} alpha", {"pack_id": PACK, "old": "value"}) for i in range(N)}
        def fail(*_args):
            raise RuntimeError("metadata rewrite failed")
        monkeypatch.setattr(env.docs, "rewrite_source_metadata", fail)
        with sqlite3.connect(env.vec_path) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            before = conn.execute("SELECT metadata FROM vectors_kure WHERE node_id = 'c3'").fetchone()[0]
        with principal_scope(principal):
            result = pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, live, sql=env.sql)
        assert result[4] == N
        with sqlite3.connect(env.vec_path) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            after = conn.execute("SELECT metadata FROM vectors_kure WHERE node_id = 'c3'").fetchone()[0]
        assert after == before
    finally:
        env.close()


def test_incremental_rejects_unknown_shared_metadata_updater(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    calls = []
    try:
        assert env.run() == (N, 0)
        live = {f"c{i}": (f"본문 {i} alpha", {"pack_id": PACK, "old": "value"}) for i in range(N)}
        monkeypatch.setattr(env.vec, "update_metadata", lambda *_args: calls.append(True), raising=False)
        with principal_scope(principal), pytest.raises(RuntimeError, match="metadata updater"):
            pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, live, sql=env.sql)
        assert calls == []
    finally:
        env.close()


def test_incremental_embedding_precedes_transaction_and_reuses_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    calls = []
    def embed(texts):
        assert not env.vec.shared_file.in_unit_of_work
        calls.append(tuple(texts))
        return _embed(texts)
    env.vec._ef = embed
    env.inject_doc_error("c3")
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with principal_scope(principal):
            result = pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {},
                                                       batch_size=B, sql=env.sql)
        assert result[0] == N - 1 and result[4] == 1
        assert len(calls) == (N + B - 1) // B
    finally:
        env.close()


def test_incremental_doc_failure_rolls_back_vector(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    env.inject_doc_error("c3")
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with principal_scope(principal):
            result = pack_load.load_chunks_incremental(
                PACK, env.chunks, env.vec, env.docs, {}, batch_size=B, sql=env.sql,
            )
        assert result[0] == N - 1 and result[4] == 1
        vec_ids, doc_ids = env.ids()
        assert vec_ids == doc_ids
        assert "c3" not in vec_ids
    finally:
        env.close()


def test_shared_batch_embedding_failure_does_not_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    def embed(texts):
        assert not env.vec.shared_file.in_unit_of_work
        if len(texts) > 1:
            raise RuntimeError("batch embedding failure")
        return _embed(texts)
    env.vec._ef = embed
    try:
        assert env.run() == (0, N)
        assert env.ids() == (set(), set())
    finally:
        env.close()


def test_without_injection_every_chunk_is_written(env):
    assert env.run() == (N, 0)
    vec_ids, doc_ids = env.ids()
    assert len(vec_ids) == len(doc_ids) == N
    assert vec_ids == doc_ids


# 첫 배치 안(c3), 둘째 배치 안(c11), 마지막 부분 배치 안(c17)
@pytest.mark.parametrize("bad", ["c3", "c11", "c17"])
def test_doc_write_failure_leaves_no_vector_row_only_on_single_file(env, bad):
    env.inject_doc_error(bad)
    ok, err = env.run()
    assert (ok, err) == (N - 1, 1)
    vec_ids, doc_ids = env.ids()
    assert bad not in doc_ids
    if env.layout == "single":
        assert bad not in vec_ids
        assert vec_ids == doc_ids and len(doc_ids) == N - 1
    else:
        # 대조군: 벡터 배치가 먼저 커밋되므로 실패한 건의 벡터 행이 고아로 남는다.
        assert vec_ids - doc_ids == {bad}


@pytest.mark.parametrize("bad", ["c3", "c11"])
def test_vector_write_failure_leaves_no_row_on_either_layout(env, bad):
    env.inject_vector_error(bad)
    ok, err = env.run()
    assert (ok, err) == (N - 1, 1)
    vec_ids, doc_ids = env.ids()
    assert bad not in vec_ids and bad not in doc_ids
    assert vec_ids == doc_ids and len(doc_ids) == N - 1


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("failure", ["rollback_only", "cleanup", "cancel", "uncertain_class", "cleanup_class", "start_class"])
def test_shared_outcome_uses_failure_boundary(tmp_path, monkeypatch, incremental, failure):
    from opencrab.stores import sql_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    original = env.docs.upsert_source
    calls = []
    real_lock = sql_store.write_lock_for_store
    cleanup = RuntimeError("injected cleanup")

    @contextlib.contextmanager
    def bad_lock(sql):
        with real_lock(sql):
            try:
                yield
            finally:
                raise cleanup

    def write(source_id, text, metadata):
        calls.append(source_id)
        if source_id == "c3":
            if failure == "rollback_only":
                env.vec.shared_file.mark_rollback_only()
            elif failure == "cancel":
                raise KeyboardInterrupt("body cancel")
            elif failure == "uncertain_class":
                raise pack_load.ChunkTransactionUncertainError("ordinary body failure")
            elif failure == "cleanup_class":
                raise pack_load.LoaderWriteCleanupError("ordinary body failure")
            elif failure == "start_class":
                raise pack_load._ChunkTransactionStartError(RuntimeError("ordinary body failure"))
            elif failure == "cleanup":
                monkeypatch.setattr(sql_store, "write_lock_for_store", bad_lock)
        return original(source_id, text, metadata)

    def run():
        with principal_scope(Principal(user_id=env.user_id, is_local=True, disabled=False)):
            if incremental:
                return pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs,
                                                         {}, batch_size=B, sql=env.sql)
            return env.run()

    monkeypatch.setattr(env.docs, "upsert_source", write)
    if failure == "cleanup":
        prepare = env.vec.prepare_upsert_vectors
        def arm(texts):
            vectors = prepare(texts)
            monkeypatch.setattr(sql_store, "write_lock_for_store", bad_lock)
            return vectors
        monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    try:
        if failure in {"uncertain_class", "cleanup_class", "start_class"}:
            result = run()
            assert (result[0], result[4]) == (N - 1, 1) if incremental else result == (N - 1, 1)
            assert calls.count("c0") == 2
            assert env.ids()[0] == env.ids()[1] == {f"c{i}" for i in range(N)} - {"c3"}
        else:
            expected = {"rollback_only": pack_load.LoaderRollbackOnlyError,
                        "cleanup": pack_load.LoaderWriteCleanupError,
                        "cancel": KeyboardInterrupt}[failure]
            with pytest.raises(expected) as caught:
                run()
            assert calls.count("c0") == 1
            if failure == "cleanup":
                assert caught.value.outcome["state"] == "committed"
                assert caught.value.cleanup_error is cleanup
                assert env.ids() == ({f"c{i}" for i in range(B)}, {f"c{i}" for i in range(B)})
            else:
                assert env.ids() == (set(), set())
    finally:
        env.close()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("defect", ["vector_unavailable", "fts_flag", "source_reader", "metadata_hook", "raw_transaction"])
def test_shared_entry_rejects_before_first_data_write(tmp_path, monkeypatch, incremental, defect):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    try:
        if defect == "vector_unavailable":
            env.vec._available = False
        elif defect == "fts_flag":
            env.docs._fts_ok = False
        elif defect == "source_reader":
            env.docs.read_source_state_exact = None
        elif defect == "metadata_hook":
            env.vec.update_metadata = lambda *_: None
        else:
            env.vec.shared_file.connection().execute("BEGIN")
        if not incremental and defect in {"source_reader", "metadata_hook"}:
            assert env.run() == (N, 0)
        else:
            with principal_scope(Principal(user_id=env.user_id, is_local=True, disabled=False)):
                with pytest.raises(RuntimeError):
                    if incremental:
                        pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {}, sql=env.sql)
                    else:
                        env.run()
            assert env.ids() == (set(), set())
    finally:
        env.vec.shared_file.connection().rollback()
        env.close()


@pytest.mark.parametrize("incremental", [False, True])
def test_changed_generation_after_successful_embedding_is_rejected(tmp_path, monkeypatch, incremental):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    old_generation = env.sql.shared_file.pack_incarnation(PACK)
    prepare = env.vec.prepare_upsert_vectors
    prepared = []

    def replace_pack(texts):
        vectors = prepare(texts)
        with sqlite3.connect(env.doc_path) as other:
            other.execute("DELETE FROM packs WHERE pack_id=?", (PACK,))
        create_pack(env.sql, env.user_id, PACK)
        assert env.sql.shared_file.pack_incarnation(PACK) != old_generation
        prepared.append(len(vectors))
        return vectors

    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", replace_pack)
    try:
        with principal_scope(Principal(user_id=env.user_id, is_local=True, disabled=False)):
            if incremental:
                result = pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {}, batch_size=B, sql=env.sql)
                assert result[0] == 0 and result[4] == N
            else:
                assert env.run() == (0, N)
        assert prepared == [B, B, N - 2 * B]
        assert env.ids() == (set(), set())
    finally:
        env.close()


@pytest.mark.parametrize("auxiliary", ["callback", "checkpoint"])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_committed_cancel_preserves_original_and_never_retries(tmp_path, monkeypatch, caplog,
                                                              auxiliary, cleanup_failure):
    from opencrab.stores import sql_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    cancel = KeyboardInterrupt("auxiliary cancel")
    cleanup = RuntimeError("lock cleanup failed")
    original_write = env.docs.upsert_source
    real_lock = sql_store.write_lock_for_store
    calls = []

    @contextlib.contextmanager
    def lock(sql):
        with real_lock(sql):
            try:
                yield
            finally:
                if cleanup_failure:
                    raise cleanup

    def cancel_auxiliary(*_):
        raise cancel

    def write(source_id, text, metadata):
        calls.append(source_id)
        original_write(source_id, text, metadata)
        if source_id == "c0":
            if auxiliary == "callback":
                env.vec.shared_file.after_commit(cancel_auxiliary)
            else:
                monkeypatch.setattr(env.vec.shared_file._checkpointer,
                                    "_checkpoint_if_wal_large", cancel_auxiliary)

    prepare = env.vec.prepare_upsert_vectors
    def arm(texts):
        result = prepare(texts)
        monkeypatch.setattr(sql_store, "write_lock_for_store", lock)
        return result

    monkeypatch.setattr(env.docs, "upsert_source", write)
    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            env.run()
        assert caught.value is cancel
        assert calls == [f"c{i}" for i in range(B)]
        assert env.ids() == ({f"c{i}" for i in range(B)}, {f"c{i}" for i in range(B)})
        if cleanup_failure:
            assert "잠금 정리 실패" in caplog.text
    finally:
        env.close()


def test_body_rollback_and_cleanup_errors_keep_all_causes(tmp_path, monkeypatch, caplog):
    from opencrab.stores import sql_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    conn = env.vec.shared_file.connection()
    body = ValueError("private-body-failure-sentinel")
    rollback = RuntimeError("private-rollback-failure-sentinel")
    cleanup = RuntimeError("private-cleanup-failure-sentinel")
    trace = []
    real_lock = sql_store.write_lock_for_store

    @contextlib.contextmanager
    def lock(sql):
        with real_lock(sql):
            try:
                yield
            finally:
                raise cleanup

    class ConnectionProxy:
        def __getattr__(self, name):
            return getattr(conn, name)
        def rollback(self):
            trace.append("rollback")
            raise rollback

    prepare = env.vec.prepare_upsert_vectors
    def arm(texts):
        result = prepare(texts)
        monkeypatch.setattr(env.vec.shared_file, "connection", lambda: ConnectionProxy())
        monkeypatch.setattr(sql_store, "write_lock_for_store", lock)
        return result
    def fail(*_):
        assert conn.execute("SELECT count(*) FROM main.vectors_kure").fetchone()[0] == B
        trace.append("body")
        raise body
    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    monkeypatch.setattr(env.docs, "upsert_source", fail)
    try:
        with pytest.raises(pack_load.ChunkTransactionUncertainError) as caught:
            env.run()
        assert caught.value.body_error is body
        assert caught.value.rollback_error is rollback
        assert caught.value.cleanup_error is cleanup
        assert trace == ["body", "rollback"]
        assert caught.value.outcome.get("state") not in {"committed", "rolled_back"}
        for error in (body, rollback, cleanup):
            assert str(error) not in caplog.text
    finally:
        conn.rollback()
        env.close()


@pytest.mark.parametrize("layout", ["single", "five"])
def test_batch_preparation_failure_has_mode_specific_retries(tmp_path, monkeypatch, layout):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env(layout, tmp_path)
    calls = []
    def embed(texts):
        calls.append(len(texts))
        if len(texts) > 1:
            raise RuntimeError("batch preparation failed")
        return _embed(texts)
    env.vec._ef = embed
    try:
        assert env.run() == ((0, N) if layout == "single" else (N, 0))
        assert calls.count(1) == (0 if layout == "single" else N)
    finally:
        env.close()


@pytest.mark.parametrize("rollback_only", [False, True])
@pytest.mark.parametrize("cleanup_cancel", [False, True])
def test_cleanup_boundary_preserves_rollback_only_priority(tmp_path, monkeypatch,
                                                          rollback_only, cleanup_cancel):
    from opencrab.stores import sql_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    cleanup = KeyboardInterrupt("cleanup cancel") if cleanup_cancel else RuntimeError("cleanup failed")
    real_lock = sql_store.write_lock_for_store
    prepare = env.vec.prepare_upsert_vectors
    original = env.docs.upsert_source
    calls = []

    @contextlib.contextmanager
    def lock(sql):
        with real_lock(sql):
            try:
                yield
            finally:
                raise cleanup

    def arm(texts):
        vectors = prepare(texts)
        monkeypatch.setattr(sql_store, "write_lock_for_store", lock)
        return vectors

    def write(source_id, text, metadata):
        calls.append(source_id)
        result = original(source_id, text, metadata)
        if rollback_only:
            env.sql.shared_file.mark_rollback_only()
        return result

    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    monkeypatch.setattr(env.docs, "upsert_source", write)
    expected = pack_load.LoaderRollbackOnlyError if rollback_only else pack_load.LoaderWriteCleanupError
    try:
        with pytest.raises(expected) as caught:
            env.run()
        assert caught.value.cleanup_error is cleanup
        assert caught.value.outcome["state"] == ("rolled_back" if rollback_only else "committed")
        assert calls == [f"c{i}" for i in range(B)]
        ids = set() if rollback_only else {f"c{i}" for i in range(B)}
        assert env.ids() == (ids, ids)
    finally:
        env.close()


@pytest.mark.parametrize("stage", ["decoder", "validator"])
def test_nonshared_property_depth_preserves_original_exception(tmp_path, monkeypatch, stage):
    from opencrab.common import graph_identity
    from opencrab.stores._graph_common import decode_properties

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("five", tmp_path)
    original = RecursionError(f"{stage} depth")
    env.docs.upsert_source("source", "body", {"pack_id": PACK})

    def fail(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(graph_identity.json if stage == "decoder" else graph_identity,
                        "loads" if stage == "decoder" else "_validate_json", fail)
    try:
        for read in (lambda: graph_identity.parse_properties_object('{}'),
                     lambda: decode_properties('{}'),
                     lambda: env.docs.get_source("source")):
            with pytest.raises(RecursionError) as caught:
                read()
            assert caught.value is original
    finally:
        env.close()


def test_nonshared_unavailable_vector_precedes_pg_unit_validation(tmp_path, monkeypatch):
    from opencrab.stores._pg_uow import PgUnitOfWork

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("five", tmp_path)
    env.vec._available = False
    env.vec._pg_uow = PgUnitOfWork(None)
    env.docs._pg_uow = PgUnitOfWork(None)
    try:
        assert env.run() == (0, 0)
        env.vec._available = True
        with pytest.raises(RuntimeError, match="same PostgreSQL"):
            env.run()
    finally:
        env.vec._pg_uow = None
        env.docs._pg_uow = None
        env.close()


def _node_loader(env, graph, builder):
    path = env.d / "nodes.jsonl"
    path.write_text(json.dumps({"id": "n1", "space": "resource", "node_type": "Document",
                                "properties": {"name": "node body"}}) + "\n")
    with principal_scope(Principal(user_id=env.user_id, is_local=True, disabled=False)):
        return pack_load.load_nodes_incremental(
            PACK, path, builder, {}, {}, graph, env.docs, {}, vec=env.vec, sql=env.sql)


@pytest.mark.parametrize("defect", ["trigger", "bm25"])
def test_loader_builder_rechecks_layout_after_embedding(tmp_path, monkeypatch, defect):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    prepare = env.vec.prepare_upsert_vectors
    calls = []

    def corrupt(texts):
        vectors = prepare(texts)
        assert len(vectors) == len(texts)
        calls.append(tuple(texts))
        if defect == "trigger":
            with sqlite3.connect(env.doc_path) as conn:
                name = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                    "AND tbl_name='pack_delete_operations' LIMIT 1").fetchone()[0]
                conn.execute(f'DROP TRIGGER "{name}"')
        else:
            env.docs._node_bm25_ok = False
        return vectors

    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", corrupt)
    try:
        result = _node_loader(env, graph, builder)
        assert result.ingest_err == 1 and result[:2] == (0, 0)
        assert calls
        conn = env.sql.shared_file.connection()
        for table in ("graph_nodes", "doc_nodes", "vectors_kure", "ontology_nodes", "audit_log"):
            assert conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0] == 0
    finally:
        graph.close()
        env.close()


@pytest.mark.parametrize("incremental", [False, True])
def test_shared_preparation_logs_fixed_reason_without_exception(tmp_path, monkeypatch, caplog, incremental):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    sentinel = "private-embedding-body-sentinel"

    def fail(_texts):
        raise RuntimeError(sentinel)

    env.vec._ef = fail
    try:
        with principal_scope(Principal(user_id=env.user_id, is_local=True, disabled=False)):
            if incremental:
                result = pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs,
                                                           {}, batch_size=B, sql=env.sql)
                assert result[4] == N
            else:
                assert env.run() == (0, N)
        assert sentinel not in caplog.text
        assert "reason=embedding_preparation_failed" in caplog.text
        for index in range(N):
            assert f"c{index}" in caplog.text
        assert env.ids() == (set(), set())
    finally:
        env.close()


@pytest.mark.parametrize("sink", ["graph", "docs", "vec"])
def test_node_loader_reports_real_identity_conflict(tmp_path, monkeypatch, caplog, sink):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.sql_store import shared_write_unit

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    create_pack(env.sql, env.user_id, "foreign-pack")
    shared = env.sql.shared_file
    try:
        with shared_write_unit(env.sql):
            shared.validate_pack_writes({"foreign-pack": shared.pack_incarnation("foreign-pack")})
            shared._uow.requested_pack = "foreign-pack"
            if sink == "graph":
                graph.upsert_node("Document", "n1", {"pack_id": "foreign-pack"}, "resource")
            elif sink == "docs":
                env.docs.upsert_node_doc("resource", "Document", "n1", {"pack_id": "foreign-pack"})
            else:
                env.vec.upsert_texts(["foreign"], [{"pack_id": "foreign-pack"}], ["n1"])
        builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
        result = _node_loader(env, graph, builder)
        assert result[3:5] == (1, 0)
        assert "reason=node_identity_conflict" in caplog.text
        assert "resource/Document/n1" in caplog.text
        assert "foreign-pack" not in caplog.text
    finally:
        graph.close()
        env.close()


@pytest.mark.parametrize("failure", ["embedding", "graph", "digest", "docs", "audit", "registry", "vector", "getter"])
def test_real_node_loader_logs_no_exception_text(tmp_path, monkeypatch, caplog, failure):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    sentinel = "private-node-error-sentinel"
    original = RuntimeError(sentinel)
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(failure)
        raise original

    target, method = {
        "embedding": (env.vec, "prepare_upsert_vectors"),
        "graph": (graph, "upsert_node"), "digest": (graph, "get_node_digest"),
        "docs": (env.docs, "upsert_node_doc"), "audit": (env.docs, "log_event"),
        "registry": (env.sql, "register_node"), "vector": (env.vec, "upsert_precomputed"),
        "getter": (env.docs, "get_source"),
    }[failure]
    monkeypatch.setattr(target, method, fail)
    if failure == "digest":
        monkeypatch.setattr(graph, "upsert_node", fail)
    try:
        if failure == "getter":
            with pytest.raises(RuntimeError) as caught:
                _node_loader(env, graph, builder)
            assert caught.value is original
        else:
            result = _node_loader(env, graph, builder)
            assert result.ingest_err == 1
        assert calls
        assert sentinel not in caplog.text
        assert "reason=" in caplog.text
        assert "resource/Document/n1" in caplog.text
    finally:
        graph.close()
        env.close()


@pytest.mark.parametrize("failure", ["endpoint", "graph", "edge_probe", "held_probe", "registry", "audit"])
def test_real_edge_loader_logs_no_exception_text(tmp_path, monkeypatch, caplog, failure):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    sentinel = "private-edge-error-sentinel"
    calls = []
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    with principal_scope(principal):
        for ident in ("e1", "e2"):
            receipt = builder.add_node("resource", "Document", ident, {"title": ident}, pack_id=PACK)
            assert receipt["stores"]["graph"] == "ok"
    path = env.d / "edges.jsonl"
    path.write_text(json.dumps({"source_id": "e1", "target_id": "e2", "relation": "cites"}) + "\n")

    def fail(*_args, **_kwargs):
        calls.append(failure)
        raise RuntimeError(sentinel)

    target, method = {
        "endpoint": (graph, "lookup_node_type"), "graph": (graph, "upsert_edge"),
        "edge_probe": (graph, "get_edge"), "held_probe": (graph, "upsert_edge"),
        "registry": (env.sql, "register_edge"), "audit": (env.docs, "log_event"),
    }[failure]
    monkeypatch.setattr(target, method, fail)
    if failure == "held_probe":
        original_get = graph.get_edge
        probes = []
        def get_edge(*args, **kwargs):
            probes.append(True)
            if len(probes) > 1:
                return fail(*args, **kwargs)
            return original_get(*args, **kwargs)
        monkeypatch.setattr(graph, "get_edge", get_edge)
    try:
        with principal_scope(principal):
            result = pack_load.load_edges(PACK, path, builder,
                                          {"e1": ("resource", "Document"), "e2": ("resource", "Document")})
        assert result[0] == 0
        assert calls
        assert sentinel not in caplog.text
        assert "reason=" in caplog.text
        assert "e1" in caplog.text and "e2" in caplog.text
    finally:
        graph.close()
        env.close()


@pytest.mark.parametrize("conflict", ["edge", "endpoint"])
def test_edge_loader_classifies_real_identity_conflicts(tmp_path, monkeypatch, caplog, conflict):
    from collections import Counter

    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    create_pack(env.sql, env.user_id, "private-owner-pack")
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with principal_scope(principal):
            for ident in ("e1", "e2"):
                owner = "private-owner-pack" if conflict == "endpoint" and ident == "e2" else PACK
                assert builder.add_node("resource", "Document", ident, {"title": ident},
                                        pack_id=owner)["stores"]["graph"] == "ok"
        conn = env.sql.shared_file.connection()
        if conflict == "edge":
            conn.execute("INSERT INTO main.graph_edges "
                         "(from_type, from_id, relation, to_type, to_id, properties) "
                         "VALUES ('Document','e1','cites','Document','e2',?)",
                         (json.dumps({"pack_id": "private-owner-pack"}),))
            conn.commit()
            assert graph.get_edge("Document", "e1", "cites", "Document", "e2")["pack_id"] == "private-owner-pack"
        path = env.d / "edges.jsonl"
        path.write_text(json.dumps({"source_id": "e1", "target_id": "e2", "relation": "cites"}) + "\n")
        reasons = Counter()
        with principal_scope(principal):
            result = pack_load.load_edges(PACK, path, builder,
                                          {"e1": ("resource", "Document"), "e2": ("resource", "Document")},
                                          reasons=reasons)
        assert result == (0, 1, 0)
        assert sum(count for key, count in reasons.items() if key[0] == "신원 충돌") == 1
        assert "reason=edge_identity_conflict" in caplog.text
        assert "resource/e1-cites->resource/e2" in caplog.text
        assert "private-owner-pack" not in caplog.text
    finally:
        graph.close()
        env.close()


@pytest.mark.parametrize("requested", [PACK, None])
def test_vector_metadata_update_rejects_other_validated_pack(tmp_path, monkeypatch, requested):
    from opencrab.stores._single_file import RollbackOnlyError
    from opencrab.stores.sql_store import shared_write_unit

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    create_pack(env.sql, env.user_id, "q")
    shared = env.sql.shared_file
    generations = {pack: shared.pack_incarnation(pack) for pack in (PACK, "q")}
    try:
        with shared_write_unit(env.sql):
            shared.validate_pack_writes({"q": generations["q"]})
            shared._uow.requested_pack = "q"
            env.vec.upsert_texts(["q body"], [{"pack_id": "q", "before": True}], ["q-slot"])
        conn = shared.connection()
        before = tuple(conn.execute("SELECT * FROM vectors_kure WHERE node_id='q-slot'").fetchone())
        with pytest.raises(RollbackOnlyError), shared_write_unit(env.sql):
            shared.validate_pack_writes(generations)
            shared._uow.requested_pack = PACK
            env.vec.upsert_texts(["p body"], [{"pack_id": PACK}], ["p-first"])
            shared._uow.requested_pack = requested
            with pytest.raises(RuntimeError):
                pack_load._vec_meta_update(env.vec, "q-slot", {"pack_id": "q", "after": True}, "q")
        assert tuple(conn.execute("SELECT * FROM vectors_kure WHERE node_id='q-slot'").fetchone()) == before
        assert env.vec.get_by_id("p-first") is None
    finally:
        env.close()


@pytest.mark.parametrize("savepoint_failure", [False, True])
def test_builder_actual_insert_and_owner_reprobe_keep_original(tmp_path, monkeypatch, caplog,
                                                             savepoint_failure):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    shared = env.sql.shared_file
    conn = shared.connection()
    insertion = sqlite3.OperationalError("private-insert-sentinel")
    reprobe = RuntimeError("private-owner-reprobe-sentinel")
    savepoint = sqlite3.OperationalError("private-savepoint-sentinel")
    trace = []
    seen = []
    insert_attempted = False
    original_upsert = env.vec.upsert_precomputed
    original_owners = env.vec._slot_owners
    prepare = env.vec.prepare_upsert_vectors

    class ConnectionProxy:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            nonlocal insert_attempted
            if sql.startswith("SAVEPOINT"):
                trace.append("savepoint")
            if sql == env.vec._insert_sql():
                trace.append("insert_failed")
                insert_attempted = True
                raise insertion
            if sql.startswith("ROLLBACK TO"):
                trace.append("rollback_to")
                if savepoint_failure and insert_attempted:
                    trace.append("rollback_to_failed")
                    raise savepoint
            return conn.execute(sql, *args)

    def owners(connection, ids):
        if insert_attempted:
            trace.append("reprobe_failed")
            raise reprobe
        trace.append("owners_actual")
        return original_owners(connection, ids)

    def observe_upsert(*args, **kwargs):
        try:
            return original_upsert(*args, **kwargs)
        except BaseException as exc:
            seen.append(exc)
            raise

    def arm(texts):
        vectors = prepare(texts)
        monkeypatch.setattr(shared, "connection", lambda: ConnectionProxy())
        return vectors

    monkeypatch.setattr(env.vec, "_slot_owners", owners)
    monkeypatch.setattr(env.vec, "upsert_precomputed", observe_upsert)
    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    try:
        result = _node_loader(env, graph, builder)
        assert result.ingest_err == 1
        assert seen == [insertion]
        assert trace.index("owners_actual") < trace.index("insert_failed") < trace.index("reprobe_failed")
        assert "savepoint" in trace and "rollback_to" in trace
        assert ("rollback_to_failed" in trace) is savepoint_failure
        for error in (insertion, reprobe, savepoint):
            assert str(error) not in caplog.text
        assert "reason=" in caplog.text
        for table in ("graph_nodes", "doc_nodes", "vectors_kure", "ontology_nodes", "audit_log"):
            assert conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0] == 0
    finally:
        conn.rollback()
        graph.close()
        env.close()


def test_loader_actual_body_and_rollback_failure_preserves_no_cleanup(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    shared = env.sql.shared_file
    conn = shared.connection()
    body = ValueError("private-loader-body-sentinel")
    rollback = RuntimeError("private-loader-rollback-sentinel")
    trace = []
    prepare = env.vec.prepare_upsert_vectors

    class ConnectionProxy:
        def __getattr__(self, name):
            return getattr(conn, name)

        def rollback(self):
            trace.append("rollback_failed")
            raise rollback

    def arm(texts):
        vectors = prepare(texts)
        monkeypatch.setattr(shared, "connection", lambda: ConnectionProxy())
        return vectors

    def fail(*_args):
        assert conn.execute("SELECT count(*) FROM main.vectors_kure").fetchone()[0] == B
        trace.append("body_failed")
        raise body

    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    monkeypatch.setattr(env.docs, "upsert_source", fail)
    try:
        with pytest.raises(pack_load.ChunkTransactionUncertainError) as caught:
            env.run()
        assert trace == ["body_failed", "rollback_failed"]
        assert caught.value.body_error is body
        assert caught.value.rollback_error is rollback
        assert caught.value.cleanup_error is None
        assert caught.value.outcome.get("state") not in {"committed", "rolled_back"}
        assert str(body) not in caplog.text and str(rollback) not in caplog.text
    finally:
        conn.rollback()
        env.close()


def test_independent_builder_generation_does_not_enable_loader_layout(tmp_path, monkeypatch, caplog):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    checks = []
    sentinel = "independent-builder-original-error"

    def unexpected_layout(**_kwargs):
        checks.append(True)
        raise AssertionError("independent builder entered loader layout")

    def fail(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(env.sql.shared_file, "require_write_layout", unexpected_layout)
    monkeypatch.setattr(env.docs, "upsert_node_doc", fail)
    try:
        with principal_scope(Principal(user_id=env.user_id, is_local=True, disabled=False)):
            receipt = builder.add_node("resource", "Document", "n1", {"title": "node"},
                                       pack_id=PACK, write_vector=False,
                                       _expected_pack_incarnation=env.sql.shared_file.pack_incarnation(PACK))
        assert checks == []
        assert receipt["stores"]["docs"].startswith("error:")
        assert sentinel in caplog.text
    finally:
        graph.close()
        env.close()


@pytest.mark.parametrize("entry", ["nodes", "finalize", "chunks", "incremental_chunks"])
@pytest.mark.parametrize("defect", ["idx_packs_incarnation", "idx_pack_delete_pending",
                                    "idx_pack_delete_successor", "temp_vocab"])
def test_public_loader_layout_rejects_before_data_change(tmp_path, monkeypatch, entry, defect):
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.sql_store import shared_write_unit

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    graph = LocalGraphStore(str(env.doc_path), shared=env.sql.shared_file)
    builder = OntologyBuilder(graph, env.docs, env.sql, env.vec)
    shared = env.sql.shared_file
    conn = shared.connection()
    principal = Principal(user_id=env.user_id, is_local=True, disabled=False)
    try:
        with shared_write_unit(env.sql):
            shared.validate_pack_writes({PACK: shared.pack_incarnation(PACK)})
            shared._uow.requested_pack = PACK
            env.vec.upsert_texts(["prior vector"], [{"pack_id": PACK}], ["preserve-vector"])
        before = tuple(conn.execute("SELECT * FROM main.vectors_kure WHERE node_id='preserve-vector'").fetchone())
        if defect == "temp_vocab":
            conn.execute("CREATE VIRTUAL TABLE temp.SOURCE_FTS_PROTECTION_VINST "
                         "USING fts5vocab(main, doc_sources_fts, row)")
            row = conn.execute("SELECT sql FROM temp.sqlite_master "
                               "WHERE name='SOURCE_FTS_PROTECTION_VINST'").fetchone()
            assert row is not None and "doc_sources_fts, row" in row[0]
            assert [row[1] for row in conn.execute("PRAGMA temp.table_info(SOURCE_FTS_PROTECTION_VINST)")] == ["term", "doc", "cnt"]
        else:
            assert conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='index' AND name=?", (defect,)).fetchone()
            conn.execute(f'DROP INDEX "{defect}"')
            assert conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='index' AND name=?", (defect,)).fetchone() is None
        conn.commit()
        with principal_scope(principal), pytest.raises(RuntimeError):
            if entry == "nodes":
                _node_loader(env, graph, builder)
            elif entry == "chunks":
                env.run()
            elif entry == "incremental_chunks":
                pack_load.load_chunks_incremental(PACK, env.chunks, env.vec, env.docs, {}, sql=env.sql)
            else:
                live = {"nodes": {}, "chunks": {}, "edges": set(), "vec_ids": {"preserve-vector"},
                        "doc_node_spaces": {}}
                pack_load.incremental_finalize(PACK, graph, env.docs, env.vec, live,
                                               {"keep-node"}, {"keep-chunk"}, set(), True, 1, 1, sql=env.sql)
        assert tuple(conn.execute("SELECT * FROM main.vectors_kure WHERE node_id='preserve-vector'").fetchone()) == before
        assert env.ids() == ({"preserve-vector"}, set())
        for table in ("graph_nodes", "doc_nodes", "ontology_nodes", "audit_log"):
            assert conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0] == 0
    finally:
        graph.close()
        env.close()
