"""Tests for factory.py storage mode branching.

Verifies that make_graph_store() returns the correct store type for each
STORAGE_MODE value, and that Settings.is_local behaves correctly.
"""

from __future__ import annotations

import pytest


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
