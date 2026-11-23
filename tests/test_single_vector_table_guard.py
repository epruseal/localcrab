"""단일 모드 벡터 표 가드(#671 B7)의 시험.

설정의 컬렉션 이름이나 차원이 게시 DB의 vec0 표와 다르면 SqliteVecStore가 표를 새로 만들지 않고
VectorTableMismatchError로 거부한다. 합성 root와 해시 임베딩만 쓴다(네트워크와 운영 데이터를 쓰지 않는다).
단일 모드 시험은 게시 표 vectors_kure(float[4], 1행)를 가진 authorized root에서 시작한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opencrab.stores.sqlite_vec_store import SqliteVecStore, VectorTableMismatchError
from tests.test_factory_single_file import (  # noqa: F401  (autouse 픽스처 재노출)
    _DIM,
    _assert_lock_free,
    _authorized_and_pointed,
    _authorized_root,
    _fresh_single_registry,
    _hash_embed,
    _open_entries,
    _point_at,
    _single_env,
    _single_settings,
)

_TABLE = "vectors_kure"


def _master_snapshot(db_path) -> list[tuple]:
    """sqlite_master의 모든 행(type, name, sql)을 정렬해 돌려준다. 읽기 전용 연결이라 파일을 바꾸지 않는다."""
    uri = f"{Path(db_path).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def _rows(db_path, table: str) -> int:
    uri = f"{Path(db_path).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()


def _make_vec(built, **overrides):
    from opencrab.stores import factory

    return factory.make_vector_store(_single_settings(built.root, **overrides))


def _vec0_table_with_int8(db_path: str, name: str) -> None:
    """embedding int8[4]로 선언한 vec0 표를 합성 파일에 만든다(차원을 float[N]에서 읽을 수 없는 정의)."""
    import sqlite_vec

    conn = sqlite3.connect(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            f"CREATE VIRTUAL TABLE {name} USING vec0("
            "node_id TEXT PRIMARY KEY, pack_id TEXT partition key, embedding int8[4], "
            "+document TEXT, +metadata TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 거부(T1~T5)
# ---------------------------------------------------------------------------


def test_t1_name_mismatch_refused(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    with pytest.raises(VectorTableMismatchError) as caught:
        _make_vec(built, VECTOR_COLLECTION="vectors_other")
    assert caught.value.reason == "name"
    message = str(caught.value)
    assert "vectors_other" in message and _TABLE in message


def test_t2_dim_mismatch_refused(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    with pytest.raises(VectorTableMismatchError) as caught:
        _make_vec(built, EMBED_DIM=8)
    assert caught.value.reason == "dim"
    message = str(caught.value)
    assert "EMBED_DIM=8" in message and f"{_TABLE}(차원 4)" in message


def test_t3_name_and_dim_both_differ_reports_name(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    with pytest.raises(VectorTableMismatchError) as caught:
        _make_vec(built, VECTOR_COLLECTION="vectors_other", EMBED_DIM=8)
    assert caught.value.reason == "name"


def test_t4_same_name_non_vec0_object_refused(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    with pytest.raises(VectorTableMismatchError) as caught:
        _make_vec(built, VECTOR_COLLECTION=f"{_TABLE}_rowids")
    assert caught.value.reason == "kind"


@pytest.mark.parametrize(
    ("overrides", "new_prefix"),
    [
        pytest.param({"VECTOR_COLLECTION": "vectors_other"}, "vectors_other", id="name"),
        pytest.param({"EMBED_DIM": 8}, None, id="dim"),
        pytest.param({"VECTOR_COLLECTION": "vectors_other", "EMBED_DIM": 8}, "vectors_other", id="both"),
        pytest.param({"VECTOR_COLLECTION": f"{_TABLE}_rowids"}, None, id="kind"),
    ],
)
def test_t5_refusal_creates_no_table(tmp_path, monkeypatch, overrides, new_prefix):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    db = built.root / "single.db"
    before = _master_snapshot(db)
    rows_before = _rows(db, f"{_TABLE}_rowids")
    with pytest.raises(VectorTableMismatchError):
        _make_vec(built, **overrides)
    after = _master_snapshot(db)
    assert after == before
    assert _rows(db, f"{_TABLE}_rowids") == rows_before
    if new_prefix is not None:
        assert not [row for row in after if row[1].lower().startswith(new_prefix)]


# ---------------------------------------------------------------------------
# 통과(T6, T7)
# ---------------------------------------------------------------------------


def test_t6_match_passes_and_creates_nothing(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    db = built.root / "single.db"
    before = _master_snapshot(db)
    vec = _make_vec(built)
    try:
        assert vec.available is True
        assert vec.count() == built.counts["vectors.db:vectors_kure_rowids"]
        assert [row["id"] for row in vec.query("vector text one", n_results=3)] == ["v1"]
    finally:
        vec.close()
    assert _master_snapshot(db) == before


def test_t7_name_comparison_ignores_ascii_case(tmp_path, monkeypatch):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    db = built.root / "single.db"
    before = _master_snapshot(db)
    vec = _make_vec(built, VECTOR_COLLECTION="VECTORS_KURE")
    try:
        assert vec.available is True
        assert vec.count() == built.counts["vectors.db:vectors_kure_rowids"]
    finally:
        vec.close()
    assert _master_snapshot(db) == before


# ---------------------------------------------------------------------------
# 직접 생성(T8, T9, T14)
# ---------------------------------------------------------------------------


def test_t8_unreadable_definition_refused(tmp_path):
    db = str(tmp_path / "int8.db")
    _vec0_table_with_int8(db, "vectors_int8")
    before = _master_snapshot(db)
    assert not [row for row in before if row[2] and "embedding float[" in row[2]]
    with pytest.raises(VectorTableMismatchError) as caught:
        SqliteVecStore(db, _hash_embed, _DIM, collection_name="vectors_int8", guard_existing_table=True)
    assert caught.value.reason == "unreadable"
    assert _master_snapshot(db) == before


def test_t9_first_creation_on_empty_db_is_unchanged(tmp_path):
    guarded_db = str(tmp_path / "guarded" / "v.db")
    plain_db = str(tmp_path / "plain" / "v.db")
    guarded = SqliteVecStore(guarded_db, _hash_embed, _DIM, guard_existing_table=True)
    plain = SqliteVecStore(plain_db, _hash_embed, _DIM)
    try:
        assert guarded.available is True and plain.available is True
    finally:
        guarded.close()
        plain.close()
    guarded_sql = [row for row in _master_snapshot(guarded_db) if row[1] == _TABLE]
    plain_sql = [row for row in _master_snapshot(plain_db) if row[1] == _TABLE]
    assert guarded_sql and guarded_sql == plain_sql


def test_t14_default_keeps_old_behavior_on_single_root(tmp_path, monkeypatch, _fresh_single_registry):  # noqa: F811
    """guard_existing_table를 넘기지 않으면 shared 객체와 함께여도 이름이 다를 때 종전처럼 새 표를 만든다."""
    from opencrab.stores import factory

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    db = built.root / "single.db"
    shared = factory._open_single_file(str(db))
    vec = SqliteVecStore(str(db), _hash_embed, _DIM, collection_name="vectors_other", shared=shared)
    try:
        assert vec.available is True
    finally:
        vec.close()
    assert [row for row in _master_snapshot(db) if row[1] == "vectors_other"]


# ---------------------------------------------------------------------------
# 거부 뒤 정리와 운영 경로(T10, T11)
# ---------------------------------------------------------------------------


def test_t10_refusal_releases_locks(tmp_path, monkeypatch, _fresh_single_registry):  # noqa: F811
    from opencrab.locking import write_lock

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    with pytest.raises(VectorTableMismatchError):
        _make_vec(built, VECTOR_COLLECTION="vectors_other")
    assert _open_entries(_fresh_single_registry) == []
    _assert_lock_free(built.root)
    with write_lock(str(built.root)):
        pass


def test_t11_get_context_refuses_then_succeeds(tmp_path, monkeypatch, _fresh_single_registry):  # noqa: F811
    """운영 경로 _get_context: 불일치면 닫힌 실패(컨텍스트 미게시, 재시도도 같은 오류), 일치면 성공한다."""
    from opencrab.config import get_settings
    from opencrab.mcp import tools as tools_pkg

    built = _authorized_root(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    monkeypatch.setenv("EMBED_DIM", "4")
    monkeypatch.setenv("VECTOR_COLLECTION", "vectors_other")
    get_settings.cache_clear()
    monkeypatch.setattr(tools_pkg, "_context", {})

    for _ in range(2):
        with pytest.raises(VectorTableMismatchError):
            tools_pkg._get_context()
        assert not tools_pkg._context
        assert _open_entries(_fresh_single_registry) == []
        _assert_lock_free(built.root)

    monkeypatch.setenv("VECTOR_COLLECTION", _TABLE)
    get_settings.cache_clear()
    ctx = tools_pkg._get_context()
    try:
        assert tools_pkg._context
        assert ctx["chroma"].available is True
    finally:
        for key in ("billing", "chroma", "mongo", "sql", "neo4j"):
            store = ctx.get(key)
            close = getattr(store, "close", None)
            if callable(close):
                close()


# ---------------------------------------------------------------------------
# 다섯 파일 모드 불변(T12, T13)
# ---------------------------------------------------------------------------


def _five_file_vec(data: Path, monkeypatch, **overrides):
    from opencrab.config import Settings
    from opencrab.stores import factory

    _point_at(monkeypatch, data)
    values = {
        "STORAGE_MODE": "local",
        "LOCAL_DATA_DIR": str(data),
        "EMBEDDING_BACKEND": "openai",
        "EMBED_DIM": _DIM,
    }
    values.update(overrides)
    return factory.make_vector_store(Settings(**values))


def test_t12_five_file_mode_does_not_refuse_name_change(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _five_file_vec(data, monkeypatch).close()
    vec = _five_file_vec(data, monkeypatch, VECTOR_COLLECTION="vectors_other")
    try:
        assert vec.available is True
    finally:
        vec.close()


def test_t13_five_file_mode_does_not_refuse_dim_change(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _five_file_vec(data, monkeypatch).close()
    vec = _five_file_vec(data, monkeypatch, EMBED_DIM=8)
    try:
        assert vec.available is True
    finally:
        vec.close()
