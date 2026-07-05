"""
PostgreSQL-backed graph store — PG mode 4-store integration (graph axis).

STAGE 6b F4: the 17-method shared surface (upsert_node, get_node,
lookup_node_type, delete_node, upsert_edge, run_cypher, find_neighbors,
find_path, count_nodes, list_packs, find_by_relations, get_node_by_id,
export_nodes, export_edges, upsert_nodes_batch, upsert_edges_batch, plus
ensure_constraints) is inherited from ``_SqlGraphStoreBase``
(opencrab/stores/_sql_graph_base.py), parameterised by the POSTGRES
``SqlDialect`` (opencrab/stores/_sql_dialect.py). This module owns
connection/engine management, DDL bootstrap, and lifecycle (available/ping/
close) — see _sql_graph_base.py's module docstring for the adoption
contract.

INTENTIONAL DEVIATION (find_neighbors/find_path traversal strategy):
    The preflight benchmark validated a single recursive-CTE-with-LATERAL
    query for hub fan-out capping (534ms unbounded CTE -> 61ms p95 LATERAL
    CTE). This module instead keeps LocalGraphStore's Python-orchestrated BFS
    loop (now living in ``_SqlGraphStoreBase.find_neighbors``) and swaps
    sqlite3 calls for equivalent parameterised PG queries, because the BFS
    algorithm's "remaining slot" budget is shared *across* both directions
    and *across* nodes in FIFO order — reproducing that exact interleaving
    declaratively in one SQL statement risks subtle ordering/truncation
    differences from the reference implementation, and the task's own
    acceptance bar is "identical input -> identical output" parity against
    LocalGraphStore.

    find_neighbors() BATCHING (post-Phase-2-gate fix, preserved verbatim
    across the Stage 6b migration onto ``_SqlGraphStoreBase``): the 179k-scale
    gate measured 102.27/164.82ms p50/p95 on a 3-hop hub query (gate
    <=100ms), root-caused to a per-ROW round trip: the original port fetched
    each candidate edge's destination/source node properties with one
    SELECT-by-PK *per row*, so a degree-6,583 hub incurred up to ~50
    individual round trips for a single hop (capped by `limit`, but still
    one socket round trip per row). Fixed by batching candidate collection
    per BFS *level* (all frontier nodes at the same depth, processed
    together in FIFO order — a queue-based BFS is always in level order, so
    "the current level" is just the run of nodes at the front of the queue
    sharing the same depth): one `unnest(:frontier_ids) CROSS JOIN LATERAL
    (... LIMIT :cap)` query collects every frontier node's candidate edges
    in one round trip (`:cap` = the traversal's `limit` parameter — the same
    safe upper bound "remaining" could ever reach, so no hub's full edge
    list is ever fetched), and one more `unnest` + join collects every
    candidate node's properties in one round trip. The Python "remaining
    slot" selection logic (``_SqlGraphStoreBase._expand``) is untouched — it
    walks the prefetched rows in the exact same per-node, per-direction,
    per-row order the original live queries returned them in, so output is
    unchanged — only round-trip *count* drops, from O(rows fetched) to O(3
    per hop) regardless of frontier size or hub degree.

    ``_batch_frontier_edges``/``_batch_node_props_multi`` below are the
    verbatim batch-query implementations from the pre-migration module;
    ``_prefetch_frontier``/``_batch_node_props`` (the base's hook points)
    each open one short-lived connection and delegate straight through —
    the base's ``find_neighbors`` no longer holds one connection open across
    the whole traversal (see _sql_doc_base.py-adjacent PgDocStore precedent),
    but the query count per BFS level is unchanged.

LIFECYCLE NOTE: close() disposes the engine only when this store created it
    from a DSN string. When an external SQLAlchemy Engine is injected, the
    caller owns its lifecycle and close() is a no-op (unlike
    LocalGraphStore.close(), which always closes its own connections).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from opencrab.stores._graph_common import IDENT_RE as _SCHEMA_IDENT_RE
from opencrab.stores._graph_common import _as_dict
from opencrab.stores._sql_dialect import POSTGRES
from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA, _SqlGraphStoreBase

logger = logging.getLogger(__name__)


class PGGraphStore(_SqlGraphStoreBase):
    """PostgreSQL-backed graph store with the same interface as LocalGraphStore."""

    _dialect = POSTGRES

    def __init__(self, dsn_or_engine: Any, schema: str = "public") -> None:
        if not _SCHEMA_IDENT_RE.match(schema):
            raise ValueError(f"Invalid schema identifier: {schema!r}")
        self._schema = schema
        self._available = False
        self._owns_engine = False

        from sqlalchemy import text
        from sqlalchemy.engine import Engine

        self._text = text

        if isinstance(dsn_or_engine, Engine):
            self._engine = dsn_or_engine
        else:
            from sqlalchemy import create_engine

            self._engine = create_engine(str(dsn_or_engine))
            self._owns_engine = True

        self._init_db()

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self, write: bool = False):
        cm = self._engine.begin() if write else self._engine.connect()
        with cm as conn:
            yield conn

    # ------------------------------------------------------------------
    # _SqlGraphStoreBase hooks
    # ------------------------------------------------------------------

    def _table(self, name: str) -> str:
        return f'"{self._schema}".{name}'

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[Any]:
        with self._conn() as conn:
            return conn.execute(self._text(sql), params).fetchall()

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> Any | None:
        with self._conn() as conn:
            return conn.execute(self._text(sql), params).fetchone()

    def _exec_write(self, sql: str, params: dict[str, Any]) -> int:
        with self._conn(write=True) as conn:
            return conn.execute(self._text(sql), params).rowcount

    def _exec_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]:
        with self._conn(write=True) as conn:
            return [conn.execute(self._text(sql), params).rowcount for sql, params in statements]

    def _exec_write_batch(self, sql: str, params_list: list[dict[str, Any]]) -> None:
        with self._conn(write=True) as conn:
            conn.execute(self._text(sql), params_list)

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            with self._engine.begin() as conn:
                if self._schema != "public":
                    conn.execute(self._text(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'))
                for ddl in POSTGRES.render_ddl(GRAPH_STORE_SCHEMA, schema_name=self._schema):
                    conn.execute(self._text(ddl))
            self._available = True
            logger.info("PGGraphStore initialised (schema=%s)", self._schema)
        except Exception as exc:
            logger.warning("PGGraphStore init failed: %s", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(self._text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._owns_engine:
            try:
                self._engine.dispose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # find_neighbors BFS batching — verbatim pre-migration queries (see
    # module docstring), wired into _SqlGraphStoreBase.find_neighbors via
    # the _prefetch_frontier / _batch_node_props hook overrides below.
    # ------------------------------------------------------------------

    def _batch_frontier_edges(
        self, conn: Any, frontier_ids: list[str], cap: int, out: bool
    ) -> dict[str, list[tuple[str, str, str, Any]]]:
        """One-round-trip candidate fetch for every node in `frontier_ids`.

        Equivalent to running "SELECT ... WHERE from_id=:fid LIMIT :cap" (or
        to_id for in-edges) once per frontier node, but as a single
        unnest+LATERAL query. `cap` is `limit` (the traversal's max possible
        "remaining" value) — a safe, hub-safe upper bound; callers still
        apply the live "remaining" cap by slicing the per-node row list.
        """
        if not frontier_ids:
            return {}
        anchor_col = "from_id" if out else "to_id"
        type_col = "to_type" if out else "from_type"
        id_col = "to_id" if out else "from_id"
        rows = conn.execute(
            self._text(
                f"""
                SELECT f.frontier_id, e.c1, e.c2, e.relation, e.properties
                FROM unnest(CAST(:ids AS text[])) AS f(frontier_id)
                CROSS JOIN LATERAL (
                    SELECT {type_col} AS c1, {id_col} AS c2, relation, properties
                    FROM {self._table('graph_edges')}
                    WHERE {anchor_col} = f.frontier_id
                    LIMIT :cap
                ) e
                """
            ),
            {"ids": frontier_ids, "cap": cap},
        ).fetchall()
        out_map: dict[str, list[tuple[str, str, str, Any]]] = {}
        for frontier_id, c1, c2, relation, props in rows:
            out_map.setdefault(frontier_id, []).append((c1, c2, relation, props))
        return out_map

    def _batch_node_props_multi(
        self, conn: Any, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """One-round-trip node-properties fetch for a set of (type, id) pairs."""
        if not pairs:
            return {}
        types = [p[0] for p in pairs]
        ids = [p[1] for p in pairs]
        rows = conn.execute(
            self._text(
                f"""
                SELECT g.node_type, g.node_id, g.properties
                FROM unnest(CAST(:types AS text[]), CAST(:ids AS text[])) AS p(node_type, node_id)
                JOIN {self._table('graph_nodes')} g
                  ON g.node_type = p.node_type AND g.node_id = p.node_id
                """
            ),
            {"types": types, "ids": ids},
        ).fetchall()
        return {(r[0], r[1]): _as_dict(r[2]) for r in rows}

    def _prefetch_frontier(
        self, frontier_ids: list[str], cap: int, out: bool
    ) -> dict[str, list[tuple[str, str, str, Any]]]:
        """HOOK override — one short-lived connection, verbatim batched query
        (see module docstring). Do NOT let this fall back to the base's
        per-node default; that would silently regress hub-fanout perf."""
        with self._conn() as conn:
            return self._batch_frontier_edges(conn, frontier_ids, cap, out)

    def _batch_node_props(
        self, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """HOOK override — one short-lived connection, verbatim batched query."""
        with self._conn() as conn:
            return self._batch_node_props_multi(conn, pairs)
