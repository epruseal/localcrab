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


def _assert_bm25_turns_off_after(root: Path, *statements: str, opener=_open_doc_store) -> None:
    """대조군(수정 전 켜짐)을 확인한 뒤 DB를 고치면 node BM25가 꺼진다. data_version은 그대로다.

    opener는 root를 받아 doc 저장소를 여는 함수다. 기본은 생성자 직접 호출이고 factory 형태는 S4가 넘긴다.
    """
    docs = opener(root)
    try:
        assert docs.supports_node_bm25 is True
    finally:
        docs.close()
    _edit_single_db(root, *statements)
    watcher = sqlite3.connect(root / "single.db")
    try:
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        docs = opener(root)
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


# ---------------------------------------------------------------------------
# S4: factory 단일 모드 분기(#671)
# ---------------------------------------------------------------------------

_LIFETIME_LOCK_NAME = "single.db.lifetime.lock"
_FACTORY_NAMES = ("make_graph_store", "make_doc_store", "make_vector_store", "make_sql_store")

_HOLD_LOCK_CODE = (
    "import sys; from opencrab.locking import acquire_file_lock; "
    "lock = acquire_file_lock('single.db.lifetime.lock', sys.argv[1], timeout=0); "
    "print('held', flush=True); sys.stdin.readline()"
)


@pytest.fixture(autouse=True)
def _fresh_single_registry(monkeypatch):
    """시험마다 factory의 경로별 등록부를 비우고, 끝나면 남은 공유 객체를 닫는다."""
    from opencrab.stores import factory

    registry: dict = {}
    monkeypatch.setattr(factory, "_SINGLE_FILES", registry, raising=False)
    yield registry
    for shared in list(registry.values()):
        for owner in list(shared._users):
            shared.release(owner)
        if not shared._closed:
            shared.close()


def _open_entries(registry) -> list:
    return [shared for shared in registry.values() if not shared._closed]


def _names(root: Path) -> list[str]:
    """수명 잠금 파일만 뺀 root 이름 집합."""
    return sorted(name for name in os.listdir(root) if name != _LIFETIME_LOCK_NAME)


def _point_at(monkeypatch, root: Path) -> None:
    """환경변수 LOCAL_DATA_DIR을 root로 맞추고 임베딩을 해시로 바꾼다(네트워크를 쓰지 않는다)."""
    from opencrab.config import get_settings
    from opencrab.stores import factory

    monkeypatch.setenv("LOCAL_DATA_DIR", str(root))
    get_settings.cache_clear()
    monkeypatch.setattr(factory, "_make_kure_embedding_function", lambda settings: _hash_embed)


def _single_settings(root: Path, **overrides):
    from opencrab.config import Settings

    values = {
        "STORAGE_MODE": "local",
        "LOCAL_DATA_DIR": str(root),
        "SINGLE_SQLITE_FILE": str(root / "single.db"),
        "EMBEDDING_BACKEND": "openai",
        "EMBED_DIM": _DIM,
    }
    values.update(overrides)
    return Settings(**values)


def _make_four(settings):
    """graph, docs, vec, sql 순서로 factory 네 개를 부른다. 중간에 실패하면 만든 것을 닫는다."""
    from opencrab.stores import factory

    made = []
    try:
        for name in _FACTORY_NAMES:
            made.append(getattr(factory, name)(settings))
    except BaseException:
        _close_all(made)
        raise
    return tuple(made)


def _close_all(stores) -> None:
    for store in reversed(list(stores)):
        store.close()


def _assert_lock_free(root: Path) -> None:
    result = _exclusive_lock_attempt(root)
    assert result.returncode == 0, result.stderr


def _authorized_and_pointed(tmp_path, monkeypatch):
    built = _authorized_root(tmp_path, monkeypatch)
    _point_at(monkeypatch, built.root)
    return built


def test_single_setting_empty_uses_multi_file_paths(tmp_path, monkeypatch, _fresh_single_registry):
    """T-N1(대조군): 설정이 비어 있으면 다섯 factory가 다중 파일 이름을 만들고 single.db는 없다."""
    from opencrab.config import Settings
    from opencrab.stores import factory

    data = tmp_path / "data"
    data.mkdir()
    _point_at(monkeypatch, data)
    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(data), EMBEDDING_BACKEND="openai", EMBED_DIM=_DIM)
    assert settings.single_sqlite_file == ""
    graph = factory.make_graph_store(settings)
    docs = factory.make_doc_store(settings)
    vec = factory.make_vector_store(settings)
    sql = factory.make_sql_store(settings)
    billing = factory.make_billing_sql_store(settings, sql)
    try:
        names = set(os.listdir(data))
        assert graph.shared_file is None and docs.shared_file is None
        assert vec.shared_file is None and sql.shared_file is None
    finally:
        _close_all([billing, vec, docs, graph, sql])
    assert {"graph.db", "doc_store.db", "vectors.db", "opencrab.db", "billing.db"} <= names, names
    assert "single.db" not in names
    assert _fresh_single_registry == {}


def test_single_four_stores_share_one_file(tmp_path, monkeypatch, _fresh_single_registry):
    """T-N2: authorized root에서 네 factory가 한 공유 객체를 쓰고 기능과 행 수가 게시와 같다(F-14, G6)."""
    from opencrab.stores._single_file import is_single_file

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    graph, docs, vec, sql = _make_four(_single_settings(built.root))
    try:
        assert is_single_file(graph, docs, vec, sql) is True
        shared = sql.shared_file
        assert all(store.shared_file is shared for store in (graph, docs, vec))
        assert list(_fresh_single_registry.values()) == [shared]
        assert docs.supports_keyword is True
        assert docs.supports_node_bm25 is True
        assert docs.node_bm25_ready() is True
        assert vec.count() == built.counts["vectors.db:vectors_kure_rowids"]
        conn = shared.connection()
        assert conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0] == built.counts["doc_store.db:doc_nodes"]
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == built.counts["graph.db:graph_nodes"]
        marker = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pack_delete_operations'"
        ).fetchone()
        assert marker is not None
    finally:
        _close_all((graph, docs, vec, sql))


def test_single_billing_store_uses_root_billing_db(tmp_path, monkeypatch):
    """T-N3: 결속을 마친 sql과 함께 부른 billing 저장소는 root의 billing.db를 쓰고 공유 객체가 없다(F-23 정상)."""
    from sqlalchemy.engine import make_url

    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    graph, docs, vec, sql = _make_four(settings)
    billing = factory.make_billing_sql_store(settings, sql)
    try:
        assert make_url(billing._url).database == str(built.root / "billing.db")
        assert billing.shared_file is None
    finally:
        billing.close()
        _close_all((graph, docs, vec, sql))


def test_single_second_call_reuses_open_shared_file(tmp_path, monkeypatch, _fresh_single_registry):
    """T-N4: 열린 객체가 있으면 같은 객체를 쓴다(F-15). write.lock을 쥔 채라도 성공한다(F-21 대조군)."""
    from opencrab.locking import write_lock
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    graph, docs, vec, sql = _make_four(settings)
    try:
        shared = sql.shared_file
        again = factory.make_sql_store(settings)
        try:
            assert again.shared_file is shared
        finally:
            again.close()
        with write_lock(str(built.root)):
            under_lock = factory.make_graph_store(settings)
            try:
                assert under_lock.shared_file is shared
            finally:
                under_lock.close()
        assert list(_fresh_single_registry.values()) == [shared]
        assert shared._closed is False
    finally:
        _close_all((graph, docs, vec, sql))


def test_single_reopen_after_close_makes_new_shared_file(tmp_path, monkeypatch, _fresh_single_registry):
    """T-N5: 모두 닫은 뒤 다시 부르면 새 객체가 결속을 다시 검증한다(F-16). 그 사이 ready가 되면 거부한다(F-08)."""
    from opencrab.stores import factory
    from opencrab.stores._single_file import is_single_file

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    first = _make_four(settings)
    old = first[3].shared_file
    _close_all(first)
    assert old._closed is True
    second = _make_four(settings)
    try:
        assert second[3].shared_file is not old
        assert is_single_file(*second) is True
        assert list(_fresh_single_registry.values()) == [second[3].shared_file]
    finally:
        _close_all(second)
    _write_ledger_state(built.root, "ready")
    with pytest.raises(RuntimeError, match="single SQLite generation is not authorized"):
        factory.make_sql_store(settings)
    assert _open_entries(_fresh_single_registry) == []
    _assert_lock_free(built.root)


def test_single_lifetime_lock_is_shared_while_open(tmp_path, monkeypatch):
    """T-N8: 저장소가 열려 있는 동안 다른 프로세스의 독점 시도가 막히고 닫은 뒤에는 성공한다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    stores = _make_four(_single_settings(built.root))
    try:
        held = _exclusive_lock_attempt(built.root)
        assert held.returncode != 0
        assert "TimeoutError" in held.stderr, held.stderr
    finally:
        _close_all(stores)
    _assert_lock_free(built.root)


def test_single_add_read_round_trip(tmp_path, monkeypatch):
    """T-N6: 팩 검증 뒤 graph와 doc 노드를 쓰고, 닫았다 다시 연 저장소가 읽는다. BM25 pending은 비어 있다."""
    from opencrab.stores import _node_bm25_sqlite as nbm
    from opencrab.stores.sql_store import shared_write_unit

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    incarnation = built.incarnations[0]
    graph, docs, vec, sql = _make_four(settings)
    try:
        shared = sql.shared_file
        with shared_write_unit(sql):
            shared.validate_pack_writes({built.pack_id: incarnation})
            graph.upsert_node("Entity", "n2", {"pack_id": built.pack_id, "name": "node two"})
            docs.upsert_node_doc("space1", "Entity", "n2", {"pack_id": built.pack_id, "name": "node two"})
    finally:
        _close_all((graph, docs, vec, sql))
    graph, docs, vec, sql = _make_four(settings)
    try:
        conn = sql.shared_file.connection()
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == built.counts["graph.db:graph_nodes"] + 1
        assert conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0] == built.counts["doc_store.db:doc_nodes"] + 1
        assert nbm.pending_count(conn) == 0
        assert docs.node_bm25_ready() is True
    finally:
        _close_all((graph, docs, vec, sql))


@pytest.mark.parametrize("state", ["ready", "staged"])
def test_single_rejects_unauthorized_state_without_sidecars(tmp_path, monkeypatch, state, _fresh_single_registry):
    """T-E1: authorized가 아닌 대장은 DB를 열기 전에 거부한다(F-08). root가 바뀌지 않고 잠금이 풀린다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _write_ledger_state(built.root, state)
    settings = _single_settings(built.root)
    before = _names(built.root)
    for name in _FACTORY_NAMES:
        with pytest.raises(RuntimeError, match="single SQLite generation is not authorized"):
            getattr(factory, name)(settings)
    assert _names(built.root) == before
    assert not any(n.endswith(("-wal", "-shm")) for n in os.listdir(built.root))
    assert _open_entries(_fresh_single_registry) == []
    _assert_lock_free(built.root)


def _tamper_ledger_missing(root: Path) -> None:
    (root / "ledger.json").unlink()


def _tamper_manifest(root: Path) -> None:
    path = root / "manifest.json"
    os.chmod(path, 0o600)
    path.write_bytes(path.read_bytes() + b"\n")


def _tamper_marker(root: Path) -> None:
    path = root / "marker.json"
    os.chmod(path, 0o600)
    data = json.loads(path.read_text())
    data["manifest_digest"] = "a" * 64
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))


def _tamper_generation(root: Path) -> None:
    _edit_single_db(root, f"UPDATE single_file_metadata SET generation = '{'b' * 32}'")


def _tamper_zero_rows(root: Path) -> None:
    _edit_single_db(root, "DELETE FROM single_file_metadata")


def _tamper_two_rows(root: Path) -> None:
    from tests.test_merge_sqlite_stores import _SECOND_METADATA_ROW

    _edit_single_db(root, "PRAGMA ignore_check_constraints=ON", _SECOND_METADATA_ROW)


_TAMPER_ROWS = {
    "ledger-missing": (_tamper_ledger_missing, FileNotFoundError, None),
    "manifest-bytes": (_tamper_manifest, RuntimeError, "single SQLite manifest binding differs"),
    "marker-digest": (_tamper_marker, RuntimeError, "single SQLite manifest binding differs"),
    "metadata-generation": (_tamper_generation, RuntimeError, "single SQLite generation binding differs"),
    "metadata-zero-rows": (_tamper_zero_rows, RuntimeError, "single SQLite metadata row is invalid"),
    "metadata-two-rows": (_tamper_two_rows, RuntimeError, "single SQLite metadata row is invalid"),
    "root-moved": (None, RuntimeError, "single SQLite path binding differs"),
}


@pytest.mark.parametrize("variant", list(_TAMPER_ROWS))
def test_single_rejects_missing_ledger_and_tampered_binding(tmp_path, monkeypatch, variant, _fresh_single_registry):
    """T-E2: 대장 없음과 결속 변조를 행마다 새 root에서 거부한다(F-07, F-09~F-12). 잠금이 풀린다."""
    from opencrab.stores import factory

    built = _authorized_root(tmp_path, monkeypatch)
    tamper, exc_type, message = _TAMPER_ROWS[variant]
    root = built.root
    if tamper is None:
        moved = root.parent / "moved"
        root.rename(moved)
        root = moved
    else:
        tamper(root)
    _point_at(monkeypatch, root)
    settings = _single_settings(root)
    kwargs = {"match": message} if message else {}
    before = _names(root)
    with pytest.raises(exc_type, **kwargs):
        factory.make_sql_store(settings)
    if variant == "ledger-missing":
        # F-07: 대장이 없으면 DB를 열기 전에 거부하므로 root에 새 이름도 -wal/-shm도 생기지 않는다.
        assert _names(root) == before
        assert not any(name.endswith(("-wal", "-shm")) for name in os.listdir(root))
    assert _open_entries(_fresh_single_registry) == []
    _assert_lock_free(root)


def test_single_failed_close_owner_is_refused_without_changes(tmp_path, monkeypatch, _fresh_single_registry):
    """F-13: 이전 닫기에 실패한 소유자가 남아 있으면 factory 네 함수가 파일을 만지기 전에 거부한다."""
    from opencrab.stores import _single_file as sf
    from opencrab.stores import factory

    built = _authorized_root(tmp_path, monkeypatch)
    root = built.root
    _point_at(monkeypatch, root)
    settings = _single_settings(root)
    marker = object()
    key = os.path.abspath(str(root / "single.db"))
    with sf._failed_shared_files_lock:
        sf._failed_shared_files.setdefault(key, set()).add(marker)
    try:
        before = _names(root)
        for name in _FACTORY_NAMES:
            with pytest.raises(RuntimeError, match="failed SQLite file owner requires close retry"):
                getattr(factory, name)(settings)
        assert _names(root) == before
        assert _open_entries(_fresh_single_registry) == []
        _assert_lock_free(root)
    finally:
        with sf._failed_shared_files_lock:
            sf._failed_shared_files.get(key, set()).discard(marker)
            if not sf._failed_shared_files.get(key):
                sf._failed_shared_files.pop(key, None)


def test_single_rejects_bad_settings(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E3: 설정 행마다 파일을 만지기 전에 거부한다(F-02~F-04). root 이름 집합과 등록부가 그대로다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    root = built.root
    other = tmp_path / "other"
    other.mkdir()
    link = tmp_path / "link"
    os.symlink(root, link)
    data_dir_message = "single SQLite requires LOCAL_DATA_DIR to be the database directory"
    rows = [
        ("docker", {"STORAGE_MODE": "docker"}, None, _FACTORY_NAMES, RuntimeError,
         "single SQLite requires STORAGE_MODE=local"),
        ("pg", {"STORAGE_MODE": "pg"}, None, _FACTORY_NAMES, RuntimeError,
         "single SQLite requires STORAGE_MODE=local"),
        ("kuzu", {"STORAGE_MODE": "kuzu"}, None, _FACTORY_NAMES, RuntimeError,
         "single SQLite requires STORAGE_MODE=local"),
        ("chroma", {"VECTOR_BACKEND": "chroma"}, None, _FACTORY_NAMES, RuntimeError,
         "single SQLite requires the sqlite-vec vector backend"),
        ("pgvector", {"VECTOR_BACKEND": "pgvector"}, None, _FACTORY_NAMES, RuntimeError,
         "single SQLite requires the sqlite-vec vector backend"),
        ("embedding-not-openai", {"VECTOR_BACKEND": "sqlite-vec", "EMBEDDING_BACKEND": "local"}, None,
         ("make_vector_store",), ValueError, "EMBEDDING_BACKEND=openai"),
        ("bad-vector-ann", {"VECTOR_ANN": "bogus"}, None, ("make_vector_store",), ValueError,
         "Unknown VECTOR_ANN"),
        ("relative-path", {"SINGLE_SQLITE_FILE": "single.db"}, None, _FACTORY_NAMES, RuntimeError,
         data_dir_message),
        ("setting-differs", {"LOCAL_DATA_DIR": str(other)}, None, _FACTORY_NAMES, RuntimeError,
         data_dir_message),
        ("env-differs", {}, str(other), _FACTORY_NAMES, RuntimeError, data_dir_message),
        ("symlink-parent", {"LOCAL_DATA_DIR": str(link), "SINGLE_SQLITE_FILE": str(link / "single.db")},
         str(link), _FACTORY_NAMES, RuntimeError, data_dir_message),
    ]
    before = _names(root)
    for label, overrides, env_value, names, exc_type, message in rows:
        settings = _single_settings(root, **overrides)
        if env_value is not None:
            monkeypatch.setenv("LOCAL_DATA_DIR", env_value)
        try:
            for name in names:
                with pytest.raises(exc_type, match=message):
                    getattr(factory, name)(settings)
        finally:
            monkeypatch.setenv("LOCAL_DATA_DIR", str(root))
        assert _names(root) == before, label
        assert _fresh_single_registry == {}, label
    _assert_lock_free(root)


@pytest.mark.parametrize("variant", ["missing", "symlink", "hardlink"])
def test_single_rejects_bad_single_db_file(tmp_path, monkeypatch, variant, _fresh_single_registry):
    """T-E3, F-06과 F-25: single.db가 없거나 심볼릭 링크이거나 링크 수가 2이면 거부한다. 파일을 만들지 않는다."""
    from opencrab.stores import factory

    root = tmp_path / "plain"
    root.mkdir()
    _point_at(monkeypatch, root)
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    if variant == "symlink":
        os.symlink(other, root / "single.db")
        exc_type, message = ValueError, "file must be regular and have one link"
    elif variant == "hardlink":
        (root / "single.db").write_bytes(b"")
        os.link(root / "single.db", tmp_path / "alias.db")
        exc_type, message = ValueError, "file must be regular and have one link"
    else:
        exc_type, message = FileNotFoundError, None
    before = _names(root)
    settings = _single_settings(root)
    for name in _FACTORY_NAMES:
        with pytest.raises(exc_type, match=message):
            getattr(factory, name)(settings)
    assert _names(root) == before
    assert _fresh_single_registry == {}
    if variant == "missing":
        assert not (root / "single.db").exists()


def test_single_symlinked_parent_swap_is_rejected(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E3, F-05: 3.2절 경로 검사 뒤 SharedSqliteFile 생성 중에 상위가 바뀌면 거부한다."""
    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile

    root = tmp_path / "plain"
    root.mkdir()
    (root / "single.db").write_bytes(b"")
    _point_at(monkeypatch, root)
    state = {"inside": False, "fired": False}
    real_open = SharedSqliteFile._open_checked_file
    real_realpath = os.path.realpath

    def wrapped_open(self):
        state["inside"] = True
        try:
            return real_open(self)
        finally:
            state["inside"] = False

    def fake_realpath(path, *args, **kwargs):
        if state["inside"] and not state["fired"]:
            state["fired"] = True
            return str(path) + "-elsewhere"
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(SharedSqliteFile, "_open_checked_file", wrapped_open)
    monkeypatch.setattr(os.path, "realpath", fake_realpath)
    before = _names(root)
    with pytest.raises(ValueError, match="SQLite path has a symlinked parent"):
        factory.make_sql_store(_single_settings(root))
    monkeypatch.setattr(os.path, "realpath", real_realpath)
    assert state["fired"] is True
    assert _names(root) == before
    assert _fresh_single_registry == {}
    _assert_lock_free(root)


def test_single_store_close_flow_releases_lock(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E4: sql과 graph를 닫으면 잠금이 풀리고 이어서 네 factory가 새 객체로 다시 열린다(F-16)."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    sql = factory.make_sql_store(settings)
    graph = factory.make_graph_store(settings)
    old = sql.shared_file
    assert graph.shared_file is old
    graph.close()
    sql.close()
    assert old._closed is True
    _assert_lock_free(built.root)
    stores = _make_four(settings)
    try:
        assert stores[3].shared_file is not old
    finally:
        _close_all(stores)


def test_single_vector_ctor_unsafe_collection_closes_unowned_shared(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E5e: 소유자 없이 만든 공유 객체는 생성자 실패 때 호출 쪽이 닫는다(F-17). 다음 호출은 새 객체를 만든다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Unsafe collection_name"):
        factory.make_vector_store(_single_settings(built.root, VECTOR_COLLECTION="bad name"))
    assert _open_entries(_fresh_single_registry) == []
    assert len(_fresh_single_registry) == 1
    old = next(iter(_fresh_single_registry.values()))
    assert old._closed is True
    _assert_lock_free(built.root)
    vec = factory.make_vector_store(_single_settings(built.root))
    try:
        assert vec.shared_file is not old
        assert vec.count() == built.counts["vectors.db:vectors_kure_rowids"]
    finally:
        vec.close()


def test_single_first_call_under_write_lock_is_rejected(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E9(F-21): write.lock을 쥔 스레드의 첫 factory 호출은 수명 잠금 순서 위반으로 거부된다.

    이 거부는 의도된 현재 동작이며 후속(followup_671_factory_write_lock_first_call)이 고친다.
    """
    from opencrab.locking import write_lock
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    with write_lock(str(built.root)):
        before = _names(built.root)
        with pytest.raises(RuntimeError, match="lifetime lock order violated"):
            factory.make_sql_store(settings)
        assert _names(built.root) == before
        assert _fresh_single_registry == {}
    sql = factory.make_sql_store(settings)
    sql.close()


def test_single_lifetime_lock_timeout(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E10(F-22): 다른 프로세스가 수명 잠금을 독점하면 대기 시간이 지나 TimeoutError로 거부된다."""
    from opencrab import locking
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    monkeypatch.setattr(locking, "default_lock_wait_timeout", lambda: 0.3)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_CODE, str(built.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        before = _names(built.root)
        with pytest.raises(TimeoutError, match="timed out waiting for file lock"):
            factory.make_sql_store(settings)
        assert _names(built.root) == before
        assert _fresh_single_registry == {}
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    sql = factory.make_sql_store(settings)
    sql.close()


def test_single_billing_requires_bound_sql_store(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E11(F-23): 결속을 마친 열린 SQL 저장소가 아니면 billing 저장소를 만들지 않는다."""
    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile
    from opencrab.stores.sql_store import SQLStore

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    message = "single SQLite billing requires the bound SQL store"
    multi_dir = tmp_path / "multi"
    multi_dir.mkdir()
    multi = SQLStore(url=f"sqlite:///{multi_dir}/opencrab.db")
    unregistered_path = str(built.root / "single.db")
    unregistered = SharedSqliteFile(unregistered_path, checked=True)
    unregistered_sql = SQLStore(url=f"sqlite:///{unregistered_path}", shared=unregistered)
    bound = _make_four(settings)
    try:
        before = _names(built.root)
        for candidate in (multi, unregistered_sql):
            with pytest.raises(RuntimeError, match=message):
                factory.make_billing_sql_store(settings, candidate)
        _close_all(bound)
        with pytest.raises(RuntimeError, match=message):
            factory.make_billing_sql_store(settings, bound[3])
        assert _names(built.root) == before
    finally:
        multi.close()
        unregistered_sql.close()


def test_single_concurrent_first_calls_share_one_object(tmp_path, monkeypatch, _fresh_single_registry):
    """T-G1: 두 스레드의 동시 첫 호출은 공유 객체 하나를 만들고 결속 검증은 한 번 한다."""
    import threading

    from opencrab.stores import factory
    from opencrab.stores._single_file import SharedSqliteFile

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    real_validate = SharedSqliteFile.validate_binding
    calls: list[int] = []

    def counting(self, *args, **kwargs):
        calls.append(threading.get_ident())
        time.sleep(0.3)
        return real_validate(self, *args, **kwargs)

    monkeypatch.setattr(SharedSqliteFile, "validate_binding", counting)

    # 한 공유 객체 위에서 저장소를 동시에 만드는 것은 지원하지 않는다(설계 11절 가정 3). 두 번째 생성자의
    # acquire가 첫 생성자가 쓰는 연결에 훅을 걸어 "Error creating function"이 날 수 있다. 이 시험은 등록부의
    # 동시성만 본다. 그래서 저장소 생성자만 직렬화하고 등록부 조회와 결속 검증은 동시에 부른다.
    from opencrab.stores.local_graph_store import LocalGraphStore

    real_init = LocalGraphStore.__init__
    construct_lock = threading.Lock()

    def serial_init(self, *args, **kwargs):
        with construct_lock:
            return real_init(self, *args, **kwargs)

    monkeypatch.setattr(LocalGraphStore, "__init__", serial_init)
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(factory.make_graph_store(settings))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    try:
        assert errors == []
        assert len(results) == 2
        assert len(calls) == 1, calls
        assert results[0].shared_file is results[1].shared_file
        assert list(_fresh_single_registry.values()) == [results[0].shared_file]
    finally:
        _close_all(results)


def test_single_open_close_cycle_leaks_no_descriptors(tmp_path, monkeypatch):
    """root 디렉터리 fd는 결속 검증 뒤(성공과 실패 모두) 닫힌다. 열고 닫는 순환이 fd를 남기지 않는다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)

    def cycle() -> None:
        _close_all(_make_four(settings))

    def failing_cycle() -> None:
        _write_ledger_state(built.root, "ready")
        try:
            with pytest.raises(RuntimeError, match="not authorized"):
                factory.make_sql_store(settings)
        finally:
            _write_ledger_state(built.root, "authorized")

    import gc

    cycle()
    failing_cycle()
    gc.collect()
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        cycle()
        failing_cycle()
    gc.collect()
    assert len(os.listdir("/proc/self/fd")) <= before


def test_single_authorized_root_with_stale_sidecars(tmp_path, monkeypatch):
    """T-G2: 이전 서비스가 남긴 -wal과 -shm이 있는 authorized root에도 연결된다."""
    from opencrab.stores._single_file import is_single_file

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    root = built.root
    (root / "single.db-wal").write_bytes(b"")
    (root / "single.db-shm").write_bytes(b"")
    stores = _make_four(_single_settings(root))
    try:
        assert is_single_file(*stores) is True
        assert stores[1].node_bm25_ready() is True
        assert stores[3].shared_file.connection().execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0] >= 1
    finally:
        _close_all(stores)


def test_single_factory_doc_fts_and_bm25_init_is_read_only(tmp_path, monkeypatch):
    """T-G5(factory 형태): doc factory의 초기화는 쓰기를 하지 않는다. data_version이 같다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    watcher = sqlite3.connect(built.root / "single.db")
    try:
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        docs = factory.make_doc_store(settings)
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


@pytest.mark.parametrize("variant", ["count-mismatch", "stats-row-missing"])
def test_single_factory_bm25_ready_count_mismatch_turns_off(tmp_path, monkeypatch, variant):
    """T-G6(factory 형태): 정합이 어긋난 게시 DB는 doc factory가 node BM25를 끈 채로 연다(F-24)."""
    from opencrab.stores import _node_bm25_sqlite as nbm
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    if variant == "count-mismatch":
        statement = f"UPDATE {nbm.STATS} SET doc_count = doc_count + 1 WHERE id = 1"
    else:
        statement = f"DELETE FROM {nbm.STATS} WHERE id = 1"
    _assert_bm25_turns_off_after(
        built.root, statement, opener=lambda root: factory.make_doc_store(settings)
    )


@pytest.mark.parametrize("variant", ["trigger", "vocab-table"])
def test_single_factory_bm25_missing_object_turns_off(tmp_path, monkeypatch, variant):
    """T-G7(factory 형태): BM25 객체가 없는 게시 DB는 doc factory가 node BM25를 끈 채로 연다(F-24)."""
    from opencrab.stores import _node_bm25_sqlite as nbm
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    settings = _single_settings(built.root)
    if variant == "trigger":
        statement = f"DROP TRIGGER {nbm.TRIGGERS[0]}"
    else:
        statement = f"DROP TABLE {nbm.VINST}"
    _assert_bm25_turns_off_after(
        built.root, statement, opener=lambda root: factory.make_doc_store(settings)
    )


# ---------------------------------------------------------------------------
# S5: 우회 경로 거부(#671)
# ---------------------------------------------------------------------------

_AUTH_REFUSAL = "single SQLite authentication is not wired"


def _single_env(monkeypatch, root: Path) -> None:
    """get_settings()가 단일 모드 설정을 읽게 환경변수를 맞춘다."""
    from opencrab.config import get_settings

    _point_at(monkeypatch, root)
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("SINGLE_SQLITE_FILE", str(root / "single.db"))
    get_settings.cache_clear()


def _assert_no_auth_files(root: Path, before: list[str]) -> None:
    names = _names(root)
    assert names == before, names
    assert "opencrab.db" not in names
    assert "bootstrap.lock" not in names


def _http_post(token: str = "tok"):
    from fastapi.testclient import TestClient

    from opencrab.mcp.http_app import create_app

    client = TestClient(create_app())
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("path", ["require_local_principal", "maybe_bootstrap_on_empty", "http_auth_store"])
def test_single_mode_refuses_auth_bypass_paths(tmp_path, monkeypatch, path):
    """T-E7(F-20): 단일 모드에서는 인증 세 경로가 옛 opencrab.db를 열기 전에 거부한다.

    게시 root에는 opencrab.db가 없으므로 거부가 부재 검사보다 앞서야 도달한다. root에 새 파일이 생기지 않는다.
    """
    from opencrab.auth import maybe_bootstrap_on_empty, require_local_principal

    built = _authorized_root(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    before = _names(built.root)
    assert "opencrab.db" not in before
    if path == "require_local_principal":
        with pytest.raises(RuntimeError, match=_AUTH_REFUSAL):
            require_local_principal()
    elif path == "maybe_bootstrap_on_empty":
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        with pytest.raises(RuntimeError, match=_AUTH_REFUSAL):
            maybe_bootstrap_on_empty()
    else:
        with pytest.raises(RuntimeError, match=_AUTH_REFUSAL):
            _http_post()
    _assert_no_auth_files(built.root, before)


def test_auth_paths_with_empty_setting_keep_current_behavior(tmp_path, monkeypatch):
    """T-E7 대조군: 설정이 비어 있으면 HTTP 인증은 이전 동작 그대로 401이다. 파일을 만들지 않는다.

    require_local_principal의 미부트스트랩 오류는 conftest의 쓰기 관문 목록을 건드리므로 기존 시험이 맡는다.
    """
    from opencrab.config import get_settings

    root = tmp_path / "plain"
    root.mkdir()
    _point_at(monkeypatch, root)
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.delenv("SINGLE_SQLITE_FILE", raising=False)
    get_settings.cache_clear()
    assert _http_post().status_code == 401
    assert os.listdir(root) == []


def test_single_build_context_failure_closes_all(tmp_path, monkeypatch, _fresh_single_registry):
    """T-E6(F-19): _build_context가 중간(vector)에서 실패하면 이미 만든 저장소가 닫히고 잠금이 풀린다."""
    from opencrab.mcp import tools as tools_pkg
    from opencrab.stores import factory

    built = _authorized_root(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)

    def boom(settings):
        raise ValueError("vector boom")

    monkeypatch.setattr(factory, "make_vector_store", boom)
    monkeypatch.setattr(tools_pkg, "_context", {})
    with pytest.raises(ValueError, match="vector boom"):
        tools_pkg._get_context()
    assert not tools_pkg._context
    assert len(_fresh_single_registry) == 1
    assert _open_entries(_fresh_single_registry) == []
    _assert_lock_free(built.root)


def test_single_write_gates_stay_fail_closed(tmp_path, monkeypatch):
    """T-N7(증분 정리 한 곳): 게시 DB는 pack_delete_operations 표를 가지고 증분 정리는 단일 모드에서 거부된다.

    팩 인가를 지나야 이 거부에 닿으므로 소유자 principal로 부른다. 삭제와 fork의 같은 거부는 인자가 무거워 S6로 넘긴다.
    """
    from pathlib import Path as _Path

    from opencrab.auth import Principal, principal_scope
    from opencrab.pack.load import load_nodes_incremental

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    graph, docs, vec, sql = _make_four(_single_settings(built.root))
    try:
        marker = sql.shared_file.connection().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pack_delete_operations'"
        ).fetchone()
        assert marker is not None
        principal = Principal(user_id=built.owner, is_local=True, disabled=False)
        with principal_scope(principal):
            with pytest.raises(RuntimeError, match="single SQLite incremental node cleanup is not yet supported"):
                load_nodes_incremental(
                    built.pack_id, _Path("unused.jsonl"), None, {}, {}, graph, docs, {}, vec=vec, sql=sql
                )
    finally:
        _close_all((graph, docs, vec, sql))


def test_single_backup_stays_refused(tmp_path, monkeypatch):
    """T-E8(a): 단일 모드 백업은 거부되고 root에 아무것도 만들지 않는다."""
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    before = _names(built.root)
    with pytest.raises(bk.BackupError, match="단일 SQLite"):
        bk.backup_data_dir(built.root, settings=_single_settings(built.root))
    assert _names(built.root) == before


def test_single_backfill_space_stays_refused(tmp_path, monkeypatch, capsys):
    """T-E8(b): backfill_space는 단일 모드에서 rc 2로 거부하고 root에 아무것도 만들지 않는다."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    try:
        import backfill_space
    finally:
        sys.path.pop(0)

    built = _authorized_root(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = _names(built.root)
    rc = backfill_space.main(["--dry-run-scratch", str(scratch)])
    assert rc == 2
    assert "single SQLite mode is not supported by this tool" in capsys.readouterr().err
    assert _names(built.root) == before


def test_single_admin_cli_backfill_pack_id_refusals(tmp_path, monkeypatch):
    """T-E8(c)(d): 관리 CLI backfill-pack-id는 단일 모드에서 우연히 거부된다. 이 거부를 고정한다.

    (c) graph.db가 없으면 종료 코드 1이고 graph.db를 만들지 않는다.
    (d) graph.db가 있으면 write.lock을 쥔 채 첫 factory 호출이 수명 잠금 순서 위반으로 거부되고 graph.db가 그대로다.
    """
    import hashlib

    from click.testing import CliRunner

    from opencrab.cli import main

    built = _authorized_root(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    runner = CliRunner()
    before = _names(built.root)
    result = runner.invoke(main, ["packs", "backfill-pack-id", "--apply"])
    assert result.exit_code == 1, (result.exit_code, result.output)
    assert not (built.root / "graph.db").exists()
    assert _names(built.root) == before

    shutil.copy2(tmp_path / "live" / "graph.db", built.root / "graph.db")
    digest = hashlib.sha256((built.root / "graph.db").read_bytes()).hexdigest()
    result = runner.invoke(main, ["packs", "backfill-pack-id", "--apply"])
    assert isinstance(result.exception, RuntimeError), (result.exit_code, result.output, result.exception)
    assert "lifetime lock order violated" in str(result.exception)
    assert hashlib.sha256((built.root / "graph.db").read_bytes()).hexdigest() == digest


def test_single_delete_and_fork_gates_stay_fail_closed(tmp_path, monkeypatch):
    """T-N7(삭제와 fork): 단일 모드에서 delete_pack과 fork_pack은 pack_delete_operations 표가 있으면 거부된다.

    팩 인가를 지나야 이 거부에 닿으므로 소유자 principal로 부른다. 거부된 뒤 DB 행이 그대로다.
    """
    from opencrab.auth import Principal, principal_scope
    from opencrab.pack.fork import fork_pack
    from opencrab.pack.load import delete_pack

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    graph, docs, vec, sql = _make_four(_single_settings(built.root))
    try:
        conn = sql.shared_file.connection()
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("graph_nodes", "doc_nodes", "packs", "pack_delete_operations")}
        assert before["packs"] >= 1
        principal = Principal(user_id=built.owner, is_local=True, disabled=False)
        with principal_scope(principal):
            with pytest.raises(RuntimeError, match="single SQLite deletion is not yet supported"):
                delete_pack(built.pack_id, graph, docs, vec, sql=sql)
            result = fork_pack(sql, graph, docs, vec, None, None, principal=principal, src_pack_id=built.pack_id)
        assert result == {"error": "single SQLite fork is not yet supported"}, result
        after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        assert after == before
    finally:
        _close_all((graph, docs, vec, sql))
