"""Tests for make_vector_store() backend dispatch (VECTOR_BACKEND).

Verifies the backend option is wired correctly, and that the conditional
smart default (vector_backend_resolved) picks sqlite-vec only for local
storage + openai embedding, falling back to chroma otherwise. See
docs/pgvector-migration-plan.md §3.6 and config.py (vector_backend /
vector_backend_resolved).
"""

from __future__ import annotations

import pytest


def test_unset_local_openai_resolves_sqlite_vec(tmp_path) -> None:
    """VECTOR_BACKEND 미설정 + local 모드 + EMBEDDING_BACKEND=openai → sqlite-vec."""
    pytest.importorskip("sqlite_vec")
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    settings = Settings(
        STORAGE_MODE="local",
        EMBEDDING_BACKEND="openai",
        LOCAL_DATA_DIR=str(tmp_path),
    )
    assert settings.vector_backend == ""
    assert settings.vector_backend_resolved == "sqlite-vec"
    store = make_vector_store(settings)
    try:
        assert isinstance(store, SqliteVecStore)
    finally:
        store.close()


def test_unset_local_embedding_local_resolves_chroma(tmp_path) -> None:
    """VECTOR_BACKEND 미설정 + EMBEDDING_BACKEND=local(minilm) → chroma."""
    from opencrab.config import Settings
    from opencrab.stores.chroma_store import ChromaStore
    from opencrab.stores.factory import make_vector_store

    settings = Settings(
        STORAGE_MODE="local",
        EMBEDDING_BACKEND="local",
        LOCAL_DATA_DIR=str(tmp_path),
    )
    assert settings.vector_backend_resolved == "chroma"
    store = make_vector_store(settings)
    assert isinstance(store, ChromaStore)


def test_unset_docker_mode_resolves_chroma(tmp_path) -> None:
    """VECTOR_BACKEND 미설정 + STORAGE_MODE=docker → chroma (openai 임베딩이어도)."""
    from opencrab.config import Settings

    settings = Settings(
        STORAGE_MODE="docker",
        EMBEDDING_BACKEND="openai",
        LOCAL_DATA_DIR=str(tmp_path),
    )
    assert settings.vector_backend_resolved == "chroma"


def test_explicit_chroma_overrides_smart_default(tmp_path) -> None:
    """local+openai 조합이어도 VECTOR_BACKEND 명시 설정이 항상 우선한다."""
    from opencrab.config import Settings
    from opencrab.stores.chroma_store import ChromaStore
    from opencrab.stores.factory import make_vector_store

    settings = Settings(
        STORAGE_MODE="local",
        EMBEDDING_BACKEND="openai",
        VECTOR_BACKEND="chroma",
        LOCAL_DATA_DIR=str(tmp_path),
    )
    assert settings.vector_backend_resolved == "chroma"
    store = make_vector_store(settings)
    assert isinstance(store, ChromaStore)


def test_sqlite_vec_backend(tmp_path) -> None:
    pytest.importorskip("sqlite_vec")
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    settings = Settings(
        VECTOR_BACKEND="sqlite-vec",
        EMBEDDING_BACKEND="openai",  # sqlite-vec requires the KURE (openai) EF
        LOCAL_DATA_DIR=str(tmp_path),
    )
    store = make_vector_store(settings)
    try:
        assert isinstance(store, SqliteVecStore)
        assert store.available is True
        assert store._dim == settings.embed_dim
        # vec0 db file lives under LOCAL_DATA_DIR (single-backup guarantee)
        assert store._db_path.endswith(settings.vector_db_file)
    finally:
        store.close()


def test_pgvector_backend_instantiates_without_live_connection(tmp_path) -> None:
    """VECTOR_BACKEND=pgvector 는 이제 구현되어 PgVectorStore 를 반환한다.

    PgVectorStore.__init__ 은 연결을 시도하지만 실패해도 raise 하지 않고
    available=False 로 떨어진다(pg_vector_store.py의 가용성/폴백 가드,
    ChromaStore/SqliteVecStore와 동일 패턴) — 그래서 실제 PG 서버 없이도
    인스턴스화가 가능하다(no-live-connection 게이트, OPENCRAB_PG_TEST_URL 불필요).
    """
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store
    from opencrab.stores.pg_vector_store import PgVectorStore

    settings = Settings(
        VECTOR_BACKEND="pgvector",
        EMBEDDING_BACKEND="openai",
        LOCAL_DATA_DIR=str(tmp_path),
        POSTGRES_URL="postgresql://opencrab:opencrab@127.0.0.1:1/opencrab",  # unreachable on purpose
    )
    store = make_vector_store(settings)
    try:
        assert isinstance(store, PgVectorStore)
        assert store.available is False  # unreachable DSN -> graceful degrade, no raise
    finally:
        store.close()


def test_pgvector_requires_openai_embedding(tmp_path) -> None:
    """pgvector needs the app-side KURE EF; EMBEDDING_BACKEND=local (minilm,
    no app-side EF, 384d) must raise a clear config error, not a cryptic crash."""
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store

    settings = Settings(
        VECTOR_BACKEND="pgvector",
        EMBEDDING_BACKEND="local",
        LOCAL_DATA_DIR=str(tmp_path),
    )
    with pytest.raises(ValueError):
        make_vector_store(settings)


def test_unknown_backend_raises(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store

    settings = Settings(VECTOR_BACKEND="bogus", LOCAL_DATA_DIR=str(tmp_path))
    with pytest.raises(ValueError):
        make_vector_store(settings)


def test_sqlite_vec_requires_openai_embedding(tmp_path) -> None:
    """sqlite-vec needs the app-side KURE EF; EMBEDDING_BACKEND=local (minilm,
    no app-side EF, 384d) must raise a clear config error, not a cryptic crash."""
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store

    settings = Settings(
        VECTOR_BACKEND="sqlite-vec",
        EMBEDDING_BACKEND="local",
        LOCAL_DATA_DIR=str(tmp_path),
    )
    with pytest.raises(ValueError):
        make_vector_store(settings)
