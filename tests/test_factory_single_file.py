"""factory 단일 파일 연결(#671)의 시험 바탕 `_authorized_root`와 그 자체 점검(S0).

`_authorized_root`는 다중 파일 factory 네 개가 실제로 만든 데이터 디렉터리를 게시한 뒤
대장을 authorized로 다시 쓴 root를 돌려준다. 이 파일은 헬퍼만 가진다. 단일 모드 factory
시험은 S1 이후에 이 파일에 더한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_merge_sqlite_stores import _ready_setup, merge_mod

_DIM = 4


def _hash_embed(texts):
    """네트워크와 모델 내려받기를 쓰지 않는 4차원 해시 임베딩."""
    vectors = []
    for text in texts:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        vectors.append([digest[i] / 255.0 for i in range(_DIM)])
    return vectors


def _plain_table_counts(db_path: Path) -> dict[str, int]:
    """가상표를 뺀 일반 표의 행 수. 그림자 표는 일반 표로 센다."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name"
            )
        ]
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        conn.close()


def _authorized_root(base_dir, monkeypatch):
    """`base_dir` 아래에 live, src, published를 만들고 authorized 대장의 게시 root를 돌려준다.

    한 시험에서 두 번 이상 부르면 호출마다 새 하위 디렉터리를 만들어 넘긴다. 같은 디렉터리로
    다시 부르면 단일 로컬 사용자 색인과 게시의 "publication root already exists"가 막는다.
    """
    from opencrab.auth import create_user
    from opencrab.config import Settings, get_settings
    from opencrab.pack.ownership import ensure_default_pack
    from opencrab.stores import factory

    base_dir = Path(base_dir)
    live = base_dir / "live"
    live.mkdir()
    monkeypatch.setenv("LOCAL_DATA_DIR", str(live))
    get_settings.cache_clear()
    monkeypatch.setattr(factory, "_make_kure_embedding_function", lambda settings: _hash_embed)
    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(live), EMBEDDING_BACKEND="openai", EMBED_DIM=_DIM)

    graph = factory.make_graph_store(settings)
    docs = factory.make_doc_store(settings)
    vec = factory.make_vector_store(settings)
    sql = factory.make_sql_store(settings)
    try:
        owner = create_user(sql, "owner", is_local=True)
        pack_id = ensure_default_pack(sql, owner)
        graph.upsert_node("Entity", "n1", {"pack_id": pack_id, "name": "node one"})
        docs.upsert_node_doc("space1", "Entity", "n1", {"pack_id": pack_id, "name": "node one"})
        docs.upsert_source("src1", "source text one", {"pack_id": pack_id})
        vec.upsert_texts(["vector text one"], metadatas=[{"pack_id": pack_id}], ids=["v1"])
    finally:
        for store in (vec, docs, graph, sql):
            store.close()
    leftovers = sorted(p.name for p in live.iterdir() if p.name.endswith(("-wal", "-shm")))
    assert leftovers == [], leftovers
    counts = {}
    for name in merge_mod.FILES:
        for table, rows in _plain_table_counts(live / name).items():
            counts[f"{name}:{table}"] = rows

    src = base_dir / "src"
    src.mkdir()
    for name in merge_mod.FILES:
        shutil.copy2(live / name, src / name)
    ctx = _ready_setup(src, base_dir)
    rc = merge_mod.main(ctx.args)
    assert rc == 0, rc

    ledger_path = ctx.root / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    assert ledger["state"] == "ready", ledger["state"]
    ledger_path.write_text(json.dumps({**ledger, "state": "authorized"}, sort_keys=True, separators=(",", ":")))

    uri = f"{(ctx.root / 'single.db').as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        incarnations = [row[0] for row in conn.execute("SELECT incarnation FROM packs")]
    finally:
        conn.close()
    assert incarnations and all(value for value in incarnations), incarnations
    return SimpleNamespace(root=ctx.root, owner=owner, pack_id=pack_id, counts=counts,
                           incarnations=incarnations)


def test_authorized_root_helper_self_check(tmp_path, monkeypatch, capsys):
    """S0 자체 점검: 게시 rc 0과 incarnation은 헬퍼가 단언한다. 여기서는 BM25 상태와 비용을 잰다."""
    from opencrab.stores import _node_bm25_sqlite as nbm

    started = time.monotonic()
    built = _authorized_root(tmp_path, monkeypatch)
    elapsed = time.monotonic() - started

    ledger = json.loads((built.root / "ledger.json").read_text())
    assert ledger["state"] == "authorized"

    uri = f"{(built.root / 'single.db').as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        meta = nbm._meta(conn)
        pending = conn.execute(f"SELECT COUNT(*) FROM {nbm.PENDING}").fetchone()[0]
        indexed = conn.execute(f"SELECT doc_count FROM {nbm.STATS} WHERE id=1").fetchone()
        rows = conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0]
    finally:
        conn.close()
    assert meta.get("state") == nbm.STATE_READY, meta
    assert pending == 0, pending
    assert indexed is not None and int(indexed[0]) == rows and rows >= 1, (indexed, rows)

    maxrss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with capsys.disabled():
        print(f"\nS0-MEASURE elapsed_s={elapsed:.2f} maxrss_kib={maxrss_kib} "
              f"bm25_state={meta.get('state')} pending={pending} doc_count={indexed[0]} doc_nodes={rows} "
              f"incarnations={built.incarnations}")
        print("S0-COUNTS " + json.dumps(built.counts, sort_keys=True))


def _validate_binding_on(root: Path):
    """root의 single.db에 checked 공유 객체를 열어 validate_binding을 한 번 부른다."""
    from opencrab.stores._single_file import SharedSqliteFile

    shared = SharedSqliteFile(str(root / "single.db"), checked=True)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        shared.validate_binding(str(root), dir_fd, dir_fd, "ledger.json", "manifest.json", "marker.json")
    finally:
        os.close(dir_fd)
        shared.close()


def _write_ledger_state(root: Path, state: str) -> None:
    ledger_path = root / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger_path.write_text(json.dumps({**ledger, "state": state}, sort_keys=True, separators=(",", ":")))


@pytest.mark.parametrize("state", ["ready", "staged"])
def test_single_validate_binding_order_regression(tmp_path, monkeypatch, state):
    """T-G3: 대장이 authorized가 아니면 validate_binding은 DB를 열기 전에 거부한다.

    DB를 먼저 열면 mode=ro 연결이 WAL DB 옆에 -wal과 -shm을 만든다(S1이 막는 결함).
    """
    built = _authorized_root(tmp_path, monkeypatch)
    _write_ledger_state(built.root, state)
    names_before = sorted(os.listdir(built.root))
    real_connect = sqlite3.connect
    connect_calls: list[str] = []

    def spy(*args, **kwargs):
        connect_calls.append(str(args[0]) if args else "")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    with pytest.raises(RuntimeError, match="single SQLite generation is not authorized"):
        _validate_binding_on(built.root)
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert connect_calls == []
    assert sorted(os.listdir(built.root)) == names_before


def test_single_validate_binding_authorized_opens_db_once(tmp_path, monkeypatch):
    """T-G3 대조군: authorized 대장은 검증을 통과하고 DB 연결은 한 번 불린다."""
    built = _authorized_root(tmp_path, monkeypatch)
    real_connect = sqlite3.connect
    connect_calls: list[str] = []

    def spy(*args, **kwargs):
        connect_calls.append(str(args[0]) if args else "")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    _validate_binding_on(built.root)
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert len(connect_calls) == 1


class _InitInterrupt(BaseException):
    """`except Exception`을 지나 생성자 밖으로 나가는 중단(KeyboardInterrupt 같은 경로)."""


_EXCLUSIVE_LOCK_CODE = (
    "import sys; from opencrab.locking import acquire_file_lock; "
    "acquire_file_lock('single.db.lifetime.lock', sys.argv[1], timeout=0)"
)


def _exclusive_lock_attempt(root: Path) -> subprocess.CompletedProcess:
    """하위 프로세스가 수명 잠금을 독점으로 시도한다. 소유자가 남으면 TimeoutError로 실패한다."""
    return subprocess.run(
        [sys.executable, "-c", _EXCLUSIVE_LOCK_CODE, str(root)], capture_output=True, text=True
    )


def _build_store(kind: str, root: Path, shared):
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    path = str(root / "single.db")
    if kind == "graph":
        return LocalGraphStore(path, shared=shared)
    if kind == "doc":
        return LocalSQLDocStore(path, shared=shared)
    return SqliteVecStore(
        path, _hash_embed, _DIM, collection_name="vectors_kure", shared=shared
    )


_STORE_CLASS = {
    "graph": ("opencrab.stores.local_graph_store", "LocalGraphStore"),
    "doc": ("opencrab.stores.local_sql_doc_store", "LocalSQLDocStore"),
    "vector": ("opencrab.stores.sqlite_vec_store", "SqliteVecStore"),
}


@pytest.mark.parametrize("kind", ["graph", "doc", "vector"])
def test_single_ctor_baseexception_releases_owner(tmp_path, monkeypatch, kind):
    """T-E5a~c: 생성자의 `_init_db`가 BaseException으로 끝나면 등록한 공유 소유자를 푼다.

    소유자가 남으면 공유 수명 잠금이 풀리지 않아 다른 프로세스의 독점 시도가 막힌다.
    """
    import importlib

    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_root(tmp_path, monkeypatch)
    module_name, class_name = _STORE_CLASS[kind]
    store_class = getattr(importlib.import_module(module_name), class_name)

    def boom(self):
        raise _InitInterrupt

    monkeypatch.setattr(store_class, "_init_db", boom)
    shared = SharedSqliteFile(str(built.root / "single.db"), checked=True)
    try:
        with pytest.raises(_InitInterrupt):
            _build_store(kind, built.root, shared)
        assert shared._users == []
        result = _exclusive_lock_attempt(built.root)
        assert result.returncode == 0, result.stderr
    finally:
        for owner in list(shared._users):
            shared.release(owner)


def _open_doc_store(root: Path):
    """root의 single.db에 checked 공유 객체를 열고 doc 저장소를 만든다. 닫으면 공유 객체도 닫힌다."""
    from opencrab.stores._single_file import SharedSqliteFile
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    path = str(root / "single.db")
    shared = SharedSqliteFile(path, checked=True)
    try:
        return LocalSQLDocStore(path, shared=shared)
    except BaseException:
        for owner in list(shared._users):
            shared.release(owner)
        raise


def _edit_single_db(root: Path, *statements: str) -> None:
    """독립 연결로 게시 DB를 고치고 commit한 뒤 닫는다."""
    conn = sqlite3.connect(root / "single.db")
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def test_single_doc_store_fts_and_bm25_on_published_root(tmp_path, monkeypatch):
    """T-N2의 doc 저장소 몫: 게시 root에서 FTS와 node BM25가 켜진 채로 열린다.

    게시 DB는 pack_delete_operations를 가져 작업 단위 밖 _tx()가 거부되므로, 쓰기 없는 판정이
    없으면 두 기능이 조용히 꺼진다.
    """
    built = _authorized_root(tmp_path, monkeypatch)
    docs = _open_doc_store(built.root)
    try:
        assert docs.supports_keyword is True
        assert docs.supports_node_bm25 is True
        assert docs.node_bm25_ready() is True
    finally:
        docs.close()


def test_single_doc_fts_and_bm25_init_is_read_only(tmp_path, monkeypatch):
    """T-G5: 초기화가 쓰기를 하지 않는다. 독립 연결의 data_version이 생성 전후로 같다."""
    built = _authorized_root(tmp_path, monkeypatch)
    watcher = sqlite3.connect(built.root / "single.db")
    try:
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        docs = _open_doc_store(built.root)
        try:
            assert docs.supports_keyword is True
            assert docs.supports_node_bm25 is True
            assert docs._bm25_cache is not None
            after = watcher.execute("PRAGMA data_version").fetchone()[0]
        finally:
            docs.close()
        assert after == before
    finally:
        watcher.close()


def _assert_bm25_turns_off_after(root: Path, *statements: str) -> None:
    """대조군(수정 전 켜짐)을 확인한 뒤 DB를 고치면 node BM25가 꺼진다. data_version은 그대로다."""
    docs = _open_doc_store(root)
    try:
        assert docs.supports_node_bm25 is True
    finally:
        docs.close()
    _edit_single_db(root, *statements)
    watcher = sqlite3.connect(root / "single.db")
    try:
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        docs = _open_doc_store(root)
        try:
            assert docs.supports_node_bm25 is False
            assert docs.node_bm25_ready() is False
            after = watcher.execute("PRAGMA data_version").fetchone()[0]
        finally:
            docs.close()
        assert after == before
    finally:
        watcher.close()


@pytest.mark.parametrize("variant", ["count-mismatch", "stats-row-missing"])
def test_single_bm25_ready_count_mismatch_turns_off(tmp_path, monkeypatch, variant):
    """T-G6: state가 ready이고 pending이 비었는데 doc_count와 doc_nodes 행 수가 다르면 꺼진다.

    입력 행마다 새 _authorized_root를 쓴다(다른 경로로 복사하면 대장 결속이 먼저 거부한다).
    """
    from opencrab.stores import _node_bm25_sqlite as nbm

    built = _authorized_root(tmp_path, monkeypatch)
    if variant == "count-mismatch":
        statement = f"UPDATE {nbm.STATS} SET doc_count = doc_count + 1 WHERE id = 1"
    else:
        statement = f"DELETE FROM {nbm.STATS} WHERE id = 1"
    _assert_bm25_turns_off_after(built.root, statement)


@pytest.mark.parametrize("variant", ["trigger", "vocab-table"])
def test_single_bm25_missing_object_turns_off(tmp_path, monkeypatch, variant):
    """T-G7: BM25 객체가 하나라도 없으면 꺼진다. 입력 행마다 새 _authorized_root를 쓴다."""
    from opencrab.stores import _node_bm25_sqlite as nbm

    built = _authorized_root(tmp_path, monkeypatch)
    if variant == "trigger":
        statement = f"DROP TRIGGER {nbm.TRIGGERS[0]}"
    else:
        statement = f"DROP TABLE {nbm.VINST}"
    _assert_bm25_turns_off_after(built.root, statement)


def test_single_doc_ctor_cache_failure_releases_owner(tmp_path, monkeypatch):
    """T-E5d: doc 생성자의 BM25 캐시 생성(97~100행)이 실패하면 소유자를 푼다.

    게시 root에서는 S3가 있어야 이 구간에 닿는다(supports_node_bm25가 참이어야 한다).
    """
    import importlib

    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_root(tmp_path, monkeypatch)
    module = importlib.import_module("opencrab.stores.local_sql_doc_store")

    def boom(*args, **kwargs):
        raise RuntimeError("token source failed")

    monkeypatch.setattr(module._ncache, "TokenSource", boom)
    path = str(built.root / "single.db")
    shared = SharedSqliteFile(path, checked=True)
    try:
        with pytest.raises(RuntimeError, match="token source failed"):
            module.LocalSQLDocStore(path, shared=shared)
        assert shared._users == []
        result = _exclusive_lock_attempt(built.root)
        assert result.returncode == 0, result.stderr
    finally:
        for owner in list(shared._users):
            shared.release(owner)


@pytest.mark.parametrize("mode", ["multi-file", "shared-without-delete-table"])
def test_doc_store_write_path_init_unchanged(tmp_path, mode):
    """대조군: pack_delete_operations가 없는 경로(다중 파일, 표 없는 공유)는 쓰는 초기화를 그대로 탄다.

    FTS와 node BM25 객체를 이 호출이 만들고 두 기능이 켜진다.
    """
    from opencrab.stores._single_file import SharedSqliteFile
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    path = tmp_path / "doc.db"
    shared = None
    if mode == "shared-without-delete-table":
        sqlite3.connect(path).close()
        shared = SharedSqliteFile(str(path), checked=True)
    docs = LocalSQLDocStore(str(path), shared=shared)
    try:
        assert docs.supports_keyword is True
        assert docs.supports_node_bm25 is True
        assert docs.node_bm25_ready() is True
    finally:
        docs.close()
    conn = sqlite3.connect(path)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "doc_nodes_bm25_meta" in names and "doc_sources_fts" in names
    assert "pack_delete_operations" not in names
