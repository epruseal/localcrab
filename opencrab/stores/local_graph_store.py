"""
Local graph store — SQLite-backed graph for local-only mode.

Adopter of ``_SqlGraphStoreBase`` (Stage 6b F3): the 17 shared graph-store
methods (upsert_node, get_node, lookup_node_type, delete_node, upsert_edge,
run_cypher, find_neighbors, find_path, count_nodes, list_packs,
find_by_relations, get_node_by_id, export_nodes, export_edges,
upsert_nodes_batch, upsert_edges_batch, ensure_constraints) now live in
``_sql_graph_base.py``, parameterised by ``SQLITE``. This module supplies the
SQLite-specific plumbing the base's adoption contract requires: connection
hooks (via ``_SqliteConnMixin``) and lifecycle (``__init__``/``_init_db``/
``available``/``ping``).

THREAD SAFETY: each thread gets its own sqlite3 connection (threading.local) —
    sharing one connection across threads corrupts even reads. WAL mode lets
    those per-thread connections read concurrently while a threading.Lock
    serialises writers so only one connection writes the file at a time
    (avoids SQLITE_BUSY). Reads take no lock.

PERF NOTE (find_neighbors hub fan-out): this adopter does NOT override
    ``_prefetch_frontier`` — the base's default (one per-node query per BFS
    level, capped at the level-wide ``limit``) was benchmarked against a
    ~10k-out-edge hub node vs. the pre-adoption "live remaining" SQL LIMIT and
    found to not regress materially (measured ~1.2x, well under threshold) —
    the historical 32x hub-fanout fix
    (see git history for this file) is preserved by the *inner* `_expand`
    loop's `[:remaining]` slice + early `break`, both still present in
    ``_sql_graph_base.py``'s ``_expand``, so results are unaffected either way.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opencrab.stores._json import parse_props  # noqa: F401 — re-exported for tests/callers
from opencrab.stores._sql_dialect import SQLITE
from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA, _SqlGraphStoreBase
from opencrab.stores._sqlite_base import _SqliteConnMixin

logger = logging.getLogger(__name__)


class LocalGraphStore(_SqliteConnMixin, _SqlGraphStoreBase):
    """SQLite-backed graph store with the same interface as Neo4jStore."""

    _dialect = SQLITE

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._available = False
        self._init_conn_state(db_path)
        self._init_db()

    def _init_db(self) -> None:
        try:
            conn = self._conn  # 이 스레드 커넥션 생성 + WAL pragma 적용
            cur = conn.cursor()
            for ddl in SQLITE.render_ddl(GRAPH_STORE_SCHEMA):
                cur.execute(ddl)
            conn.commit()
            self._available = True
            logger.info("LocalGraphStore initialised at %s", self._db_path)
        except Exception as exc:
            logger.warning("LocalGraphStore init failed: %s", exc)

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        try:
            assert self._conn
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # _SqlGraphStoreBase hooks
    # ------------------------------------------------------------------

    def _table(self, name: str) -> str:
        return name

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()

    def _exec_write(self, sql: str, params: dict[str, Any]) -> int:
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def _exec_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]:
        with self._tx() as conn:
            rowcounts = []
            for sql, params in statements:
                cur = conn.execute(sql, params)
                rowcounts.append(cur.rowcount)
            return rowcounts

    def _exec_write_batch(self, sql: str, params_list: list[dict[str, Any]]) -> None:
        with self._tx() as conn:
            conn.executemany(sql, params_list)

    # ``_require_available`` is inherited from ``_SqliteConnMixin``.
