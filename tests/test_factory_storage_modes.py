"""Tests for factory.py storage mode branching.

Verifies that make_graph_store() returns the correct store type for each
STORAGE_MODE value, and that Settings.is_local behaves correctly.
"""

from __future__ import annotations


def test_settings_local_is_local_true() -> None:
    from opencrab.config import Settings

    s = Settings(STORAGE_MODE="local")
    assert s.is_local is True


def test_settings_kuzu_is_local_true() -> None:
    """kuzu is a local-mode variant — is_local must return True so doc/sql/vector
    stores still use SQLite/local paths instead of docker services."""
    from opencrab.config import Settings

    s = Settings(STORAGE_MODE="kuzu")
    assert s.is_local is True


def test_settings_docker_is_local_false() -> None:
    from opencrab.config import Settings

    s = Settings(STORAGE_MODE="docker")
    assert s.is_local is False


def test_factory_local_returns_local_graph_store(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_graph_store
    from opencrab.stores.local_graph_store import LocalGraphStore

    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
    store = make_graph_store(settings)
    assert isinstance(store, LocalGraphStore)


def test_factory_kuzu_returns_kuzu_graph_store(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_graph_store
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    settings = Settings(STORAGE_MODE="kuzu", LOCAL_DATA_DIR=str(tmp_path))
    store = make_graph_store(settings)
    try:
        assert isinstance(store, KuzuGraphStore)
        assert store.available is True
    finally:
        store.close()


def test_factory_local_uses_graph_db_file(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_graph_store

    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
    store = make_graph_store(settings)
    assert store._db_path == str(tmp_path / "graph.db")


def test_factory_kuzu_uses_graph_kuzu_file(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_graph_store

    settings = Settings(STORAGE_MODE="kuzu", LOCAL_DATA_DIR=str(tmp_path))
    store = make_graph_store(settings)
    try:
        assert store._db_path == str(tmp_path / "graph.kuzu")
    finally:
        store.close()


def test_factory_kuzu_doc_store_is_sqlite(tmp_path) -> None:
    """In kuzu mode, make_doc_store must still return LocalSQLDocStore (SQLite),
    not MongoStore — because is_local is True for kuzu."""
    from opencrab.config import Settings
    from opencrab.stores.factory import make_doc_store
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    settings = Settings(STORAGE_MODE="kuzu", LOCAL_DATA_DIR=str(tmp_path))
    store = make_doc_store(settings)
    assert isinstance(store, LocalSQLDocStore)


def test_factory_kuzu_sql_store_uses_sqlite_url(tmp_path) -> None:
    """In kuzu mode, make_sql_store must use sqlite:// (not postgres://)."""
    from opencrab.config import Settings
    from opencrab.stores.factory import make_sql_store

    settings = Settings(STORAGE_MODE="kuzu", LOCAL_DATA_DIR=str(tmp_path))
    store = make_sql_store(settings)
    assert "sqlite" in store._url


# ---------------------------------------------------------------------------
# storage_mode == "pg" — 4-store type mapping (no live PG connection required).
#
# All three PG store constructors (PGGraphStore/PgDocStore/PgVectorStore)
# attempt a connection in __init__ but degrade gracefully (available=False)
# instead of raising when the DSN is unreachable — same pattern as
# ChromaStore/SqliteVecStore. An unreachable port (127.0.0.1:1) keeps these
# tests fast and connection-free; real-connection CRUD parity is covered by
# tests/test_pg_graph_doc_parity.py and tests/test_store_concurrency.py,
# gated on OPENCRAB_PG_TEST_URL.
# ---------------------------------------------------------------------------

_UNREACHABLE_PG_URL = "postgresql://opencrab:opencrab@127.0.0.1:1/opencrab"


def test_settings_pg_is_local_false() -> None:
    """pg is a separate branch, not a local-SQLite variant like kuzu."""
    from opencrab.config import Settings

    s = Settings(STORAGE_MODE="pg")
    assert s.is_local is False


def test_settings_pg_resolves_pgvector() -> None:
    from opencrab.config import Settings

    s = Settings(STORAGE_MODE="pg")
    assert s.vector_backend_resolved == "pgvector"


def test_factory_pg_returns_pg_graph_store(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_graph_store
    from opencrab.stores.pg_graph_store import PGGraphStore

    settings = Settings(
        STORAGE_MODE="pg", LOCAL_DATA_DIR=str(tmp_path), POSTGRES_URL=_UNREACHABLE_PG_URL
    )
    store = make_graph_store(settings)
    try:
        assert isinstance(store, PGGraphStore)
    finally:
        store.close()


def test_factory_pg_returns_pg_doc_store(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_doc_store
    from opencrab.stores.pg_doc_store import PgDocStore

    settings = Settings(
        STORAGE_MODE="pg", LOCAL_DATA_DIR=str(tmp_path), POSTGRES_URL=_UNREACHABLE_PG_URL
    )
    store = make_doc_store(settings)
    try:
        assert isinstance(store, PgDocStore)
    finally:
        store.close()


def test_factory_pg_returns_pg_vector_store(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store
    from opencrab.stores.pg_vector_store import PgVectorStore

    settings = Settings(
        STORAGE_MODE="pg",
        EMBEDDING_BACKEND="openai",
        LOCAL_DATA_DIR=str(tmp_path),
        POSTGRES_URL=_UNREACHABLE_PG_URL,
    )
    store = make_vector_store(settings)
    try:
        assert isinstance(store, PgVectorStore)
    finally:
        store.close()


def test_factory_pg_sql_store_uses_postgres_url(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_sql_store

    settings = Settings(
        STORAGE_MODE="pg", LOCAL_DATA_DIR=str(tmp_path), POSTGRES_URL=_UNREACHABLE_PG_URL
    )
    store = make_sql_store(settings)
    assert "postgresql" in store._url


def test_factory_pg_graph_and_doc_share_engine(tmp_path) -> None:
    """§3.5: graph/doc/vector share one SQLAlchemy engine (one connection pool)
    per POSTGRES_URL via factory._get_pg_engine's lru_cache."""
    from opencrab.config import Settings
    from opencrab.stores.factory import make_doc_store, make_graph_store

    settings = Settings(
        STORAGE_MODE="pg", LOCAL_DATA_DIR=str(tmp_path), POSTGRES_URL=_UNREACHABLE_PG_URL
    )
    graph_store = make_graph_store(settings)
    doc_store = make_doc_store(settings)
    try:
        assert graph_store._engine is doc_store._engine
    finally:
        graph_store.close()
        doc_store.close()
