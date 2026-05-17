"""
Store factory — returns LocalCrab's local-only backends.

Usage:
    from opencrab.stores.factory import make_graph_store, make_vector_store, ...
    graph  = make_graph_store(settings)
    vector = make_vector_store(settings)
    docs   = make_doc_store(settings)
    sql    = make_sql_store(settings)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.config import Settings


def make_graph_store(settings: Settings) -> Any:
    """Return the local SQLite graph store."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    db_path = os.path.join(settings.local_data_dir, "graph.db")
    return LocalGraphStore(db_path=db_path)


def make_vector_store(settings: Settings) -> Any:
    """Return the local persistent Chroma store."""
    from opencrab.stores.chroma_store import ChromaStore

    chroma_path = os.path.join(settings.local_data_dir, "chroma")
    return ChromaStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        collection_name=settings.chroma_collection,
        local_mode=True,
        local_path=chroma_path,
    )


def make_doc_store(settings: Settings) -> Any:
    """Return the local JSON document store."""
    from opencrab.stores.local_doc_store import LocalDocStore

    docs_path = os.path.join(settings.local_data_dir, "docs")
    return LocalDocStore(data_dir=docs_path)


def make_sql_store(settings: Settings) -> Any:
    """Return the local SQLite SQL store."""
    from opencrab.stores.sql_store import SQLStore

    return SQLStore(url=settings.sqlite_url)
