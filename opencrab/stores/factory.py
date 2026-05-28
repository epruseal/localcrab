"""
Store factory — returns the right backend based on STORAGE_MODE setting.

Usage:
    from opencrab.stores.factory import make_graph_store, make_vector_store, ...
    graph  = make_graph_store(settings)
    vector = make_vector_store(settings)
    docs   = make_doc_store(settings)
    sql    = make_sql_store(settings)
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.config import Settings

_MADV_NOOP = "/usr/local/lib/madv_noop.so"


def _ensure_madv_noop() -> None:
    """kuzu 모드 진입 시 LD_PRELOAD에 madv_noop.so 가 없으면 자동 재실행.

    RPi5 aarch64 (CONFIG_PAGE_SIZE_16KB=y) 에서 KùzuDB가 4KB 단위 madvise를
    호출하면 EINVAL이 발생한다. madv_noop.so 는 해당 호출을 noop으로 대체하는
    LD_PRELOAD 심블 인터포저다.

    같은 경로가 이미 LD_PRELOAD에 있으면 동적 링커가 중복 로드를 방지하므로
    이중 실행 시에도 안전하다.
    """
    ld = os.environ.get("LD_PRELOAD", "")
    if _MADV_NOOP in ld:
        return
    if not os.path.exists(_MADV_NOOP):
        return  # 빌드 안 된 환경(비 RPi5)은 건너뜀
    new_ld = f"{_MADV_NOOP}:{ld}" if ld else _MADV_NOOP
    env = {**os.environ, "LD_PRELOAD": new_ld}
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def make_graph_store(settings: Settings) -> Any:
    """Return KuzuGraphStore (kuzu), LocalGraphStore (local), or Neo4jStore (docker)."""
    if settings.storage_mode == "kuzu":
        _ensure_madv_noop()
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore

        db_path = os.path.join(settings.local_data_dir, "graph.kuzu")
        return KuzuGraphStore(db_path=db_path)
    elif settings.is_local:
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = os.path.join(settings.local_data_dir, "graph.db")
        return LocalGraphStore(db_path=db_path)
    else:
        from opencrab.stores.neo4j_store import Neo4jStore

        return Neo4jStore(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )


def make_vector_store(settings: Settings) -> Any:
    """Return ChromaStore in local or docker mode."""
    from opencrab.stores.chroma_store import ChromaStore

    chroma_path = os.path.join(settings.local_data_dir, "chroma")
    return ChromaStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        collection_name=settings.chroma_collection,
        local_mode=settings.is_local,
        local_path=chroma_path,
    )


def make_doc_store(settings: Settings) -> Any:
    """Return LocalSQLDocStore (local) or MongoStore (docker).

    WHY LocalSQLDocStore INSTEAD OF LocalDocStore (JSON):
        list_nodes(limit=50000) is called on every BM25 cache rebuild (i.e.
        every query).  LocalDocStore._load() deserialises the entire JSON file
        on each call — O(N) — so a 10× data growth means a 10× slower hot
        path with no way to offset it.  LocalSQLDocStore issues a single
        SELECT … LIMIT query, which SQLite satisfies with an O(k) range scan
        and never reads rows beyond the limit.

    WHY LocalDocStore IS KEPT (not removed):
        Legacy callers that instantiate LocalDocStore directly (e.g. migration
        scripts, unit tests written before this switch) must continue to work.
        Removing the import here does not delete the class; leaving it avoids
        a confusing ImportError if someone still references it.

    WHY db_path = LOCAL_DATA_DIR / "doc_store.db":
        Keeps the SQLite file in the same directory as graph.db and
        opencrab.db, so a single LOCAL_DATA_DIR backup captures all local
        state.  A fixed filename ("doc_store.db") makes the path predictable
        for operators and migration tooling.
    """
    if settings.is_local:
        # LocalDocStore (JSON) → LocalSQLDocStore (SQLite).
        # See module docstring in local_sql_doc_store.py for full rationale.
        from pathlib import Path

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        db_path = Path(settings.local_data_dir) / "doc_store.db"
        return LocalSQLDocStore(str(db_path))
    else:
        from opencrab.stores.mongo_store import MongoStore

        return MongoStore(uri=settings.mongodb_uri, db_name=settings.mongodb_db)


def make_sql_store(settings: Settings) -> Any:
    """Return SQLStore with SQLite (local) or PostgreSQL (docker)."""
    from opencrab.stores.sql_store import SQLStore

    url = settings.sqlite_url if settings.is_local else settings.postgres_url
    return SQLStore(url=url)
