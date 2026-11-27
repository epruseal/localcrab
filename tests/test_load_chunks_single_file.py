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


def test_body_rollback_and_cleanup_errors_keep_all_causes(tmp_path, monkeypatch):
    from opencrab.stores import sql_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    env = _Env("single", tmp_path)
    conn = env.vec.shared_file.connection()
    body = ValueError("body failed")
    rollback = RuntimeError("rollback failed")
    cleanup = RuntimeError("cleanup failed")
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
            raise rollback

    prepare = env.vec.prepare_upsert_vectors
    def arm(texts):
        result = prepare(texts)
        monkeypatch.setattr(env.vec.shared_file, "connection", lambda: ConnectionProxy())
        monkeypatch.setattr(sql_store, "write_lock_for_store", lock)
        return result
    def fail(*_):
        raise body
    monkeypatch.setattr(env.vec, "prepare_upsert_vectors", arm)
    monkeypatch.setattr(env.docs, "upsert_source", fail)
    try:
        with pytest.raises(pack_load.ChunkTransactionUncertainError) as caught:
            env.run()
        assert caught.value.body_error is body
        assert caught.value.rollback_error is rollback
        assert caught.value.cleanup_error is cleanup
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
