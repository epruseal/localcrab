"""Tests for make_vector_store() backend dispatch (VECTOR_BACKEND).

Verifies the backend option is wired correctly and defaults preserve existing
behaviour (chroma). See docs/pgvector-migration-plan.md §3.6 and config.py.
"""

from __future__ import annotations

import pytest


def test_default_is_chroma(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.chroma_store import ChromaStore
    from opencrab.stores.factory import make_vector_store

    settings = Settings(LOCAL_DATA_DIR=str(tmp_path))
    assert settings.vector_backend == "chroma"
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


def test_pgvector_reserved_not_implemented(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store

    settings = Settings(VECTOR_BACKEND="pgvector", LOCAL_DATA_DIR=str(tmp_path))
    with pytest.raises(NotImplementedError):
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
