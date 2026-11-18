"""``load_chunks``의 단일 파일 경로: 쓰기 예외 주입 (#671).

벡터와 doc 저장소가 한 ``SharedSqliteFile``을 나눠 쓰면 ``load_chunks``는 배치(와 건별 재시도)를 한
트랜잭션에 넣고 예외를 삼키지 않는다. 한 건의 쓰기가 실패하면 그 건은 벡터 행도 doc 행도 남지
않는다. 다섯 파일 구조(대조군)는 doc 쓰기 실패 건에 벡터 행이 남는다(현재 동작).

시험 DB의 초기 벡터와 doc 행은 0이다. 배치 크기 B=8, 총 청크 N=19로 가득 찬 배치 둘과 부분 배치를 덮는다.
"""

from __future__ import annotations

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
            self.doc_path = self.vec_path = d / "single.db"
            shared = SharedSqliteFile(str(self.doc_path))
            kw = {"shared": shared}
        else:
            self.doc_path, self.vec_path = d / "doc_store.db", d / "vectors.db"
            kw = {}
        self.docs = LocalSQLDocStore(str(self.doc_path), **kw)
        self.vec = SqliteVecStore(str(self.vec_path), embedding_function=_embed, dim=DIM, **kw)
        self.sql = SQLStore(f"sqlite:///{d / 'opencrab.db'}")
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

        def wrapped(texts, metadatas=None, ids=None):
            if ids and bad in ids:
                raise RuntimeError("injected vector write failure")
            return original(texts=texts, metadatas=metadatas, ids=ids)

        self.vec.upsert_texts = wrapped  # type: ignore[method-assign]

    def close(self) -> None:
        self.docs.close()
        self.vec.close()


@pytest.fixture(params=["single", "five"])
def env(request, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    e = _Env(request.param, tmp_path)
    yield e
    e.close()


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
