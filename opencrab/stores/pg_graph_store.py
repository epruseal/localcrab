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
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import GraphSchemaMigrationRequired
from opencrab.stores._graph_common import IDENT_RE as _SCHEMA_IDENT_RE
from opencrab.stores._graph_common import _as_dict, _merge_space
from opencrab.stores._sql_dialect import POSTGRES
from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA, GraphTx, _SqlGraphStoreBase

logger = logging.getLogger(__name__)


class _CatalogTxAdapter:
    """Expose catalog ``execute`` through the guarded ``GraphTx`` surface."""

    def __init__(self, tx: GraphTx) -> None:
        self._tx = tx

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> Any:
        return self._tx.execute(statement, params)


class PGGraphStore(_SqlGraphStoreBase):
    """PostgreSQL-backed graph store with the same interface as LocalGraphStore."""

    _dialect = POSTGRES

    def __init__(self, dsn_or_engine: Any, schema: str = "public") -> None:
        if not _SCHEMA_IDENT_RE.match(schema):
            raise ValueError(f"Invalid schema identifier: {schema!r}")
        self._schema = schema
        self._available = False
        self._schema_state = "unconfigured"
        self._owns_engine = False
        self._graph_tx_state = threading.local()

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

    GRAPH_WRITE_NAMESPACE = 80480
    GRAPH_WRITE_KEY = 1

    def _run_graph_tx(self, callback: Callable[[GraphTx], Any], *, immediate: bool = False, exclusive: bool = False, snapshot_path: Path | None = None) -> Any:
        if immediate or exclusive or snapshot_path is not None:
            raise ValueError("graph transaction options are SQLite-only")
        if self._graph_tx_is_active():
            raise RuntimeError("nested graph transaction is not allowed")
        self._set_graph_tx_active(True)
        try:
            with self._engine.begin() as conn:
                tx = GraphTx(conn, self._dialect, self._text)
                tx.execute(
                    "SELECT pg_advisory_xact_lock(:graph_write_namespace, :graph_write_key)",
                    {"graph_write_namespace": self.GRAPH_WRITE_NAMESPACE, "graph_write_key": self.GRAPH_WRITE_KEY},
                )
                return callback(tx)
        finally:
            self._set_graph_tx_active(False)

    def _lock_graph_rows(
        self,
        tx: GraphTx,
        node_ids: Any = (),
        edge_keys: Any = (),
    ) -> None:
        """Acquire node locks first, then edge locks in canonical order.

        The transaction-scoped admission lock in ``_run_graph_tx`` already
        serializes graph writers.  These row locks make the required lock
        phases explicit for PostgreSQL and protect future callers that add
        reads between enumeration and mutation.
        """
        nodes = sorted(set(node_ids), key=lambda value: str(value).encode("utf-8"))
        if nodes:
            placeholders, params = self._in_placeholders(nodes, "lock_node_")
            tx.fetchall(
                f"SELECT node_id FROM {self._table('graph_nodes')} "
                # PostgreSQL's database collation is not necessarily byte
                # order.  Use convert_to so every writer acquires the same
                # UTF-8 canonical order on every cluster locale.
                f"WHERE node_id IN ({placeholders}) ORDER BY convert_to(node_id, 'UTF8') FOR UPDATE",
                params,
            )
        edges = sorted(
            set(edge_keys),
            key=lambda key: tuple(part.encode("utf-8") for part in key),
        )
        if edges:
            conditions: list[str] = []
            params: dict[str, str] = {}
            for index, (from_id, relation, to_id) in enumerate(edges):
                names = (f"lock_edge_f_{index}", f"lock_edge_r_{index}", f"lock_edge_t_{index}")
                conditions.append(
                    f"(from_id=:{names[0]} AND relation=:{names[1]} AND to_id=:{names[2]})"
                )
                params.update(dict(zip(names, (from_id, relation, to_id), strict=True)))
            tx.fetchall(
                f"SELECT from_id, relation, to_id FROM {self._table('graph_edges')} "
                f"WHERE {' OR '.join(conditions)} ORDER BY convert_to(from_id, 'UTF8'), convert_to(relation, 'UTF8'), convert_to(to_id, 'UTF8') FOR UPDATE",
                params,
            )

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    def _require_schema_available(self) -> None:
        if self._schema_state == "legacy_migration_required":
            raise GraphSchemaMigrationRequired("graph schema requires issue80 migration before writes")
        if self._schema_state == "partial_or_unknown":
            raise GraphSchemaMigrationRequired("graph schema is unavailable: migration required")
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            state = self._classify_schema()
            if state == "fresh":
                def bootstrap(tx: GraphTx):
                    # The initial classification was read-only and happened
                    # before admission. Reclassify on the admitted connection
                    # so a constructor that lost the race sees target and
                    # emits no DDL of its own.
                    state_after_lock = self._classify_schema(tx)
                    if state_after_lock == "target":
                        return
                    if state_after_lock != "fresh":
                        raise RuntimeError("graph schema is unavailable: migration required")
                    if self._schema != "public":
                        tx.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                    for ddl in POSTGRES.render_ddl(GRAPH_STORE_SCHEMA, schema_name=self._schema):
                        tx.execute(ddl)
                    if self._classify_schema(tx) != "target":
                        raise RuntimeError("graph schema is unavailable: migration required")
                self._run_graph_tx(bootstrap)
                state = self._classify_schema()
            self._schema_state = "target" if state == "target" else ("legacy_migration_required" if state == "legacy" else "partial_or_unknown")
            self._available = state in {"target", "legacy", "partial"}
            logger.info("PGGraphStore initialised (schema=%s)", self._schema)
        except Exception as exc:
            self._schema_state = "partial_or_unknown"
            logger.warning("PGGraphStore init failed: %s", exc)
            self._available = False

    def _classify_schema(self, connection: Any | None = None) -> str:
        """Read-only startup classification of graph-owned objects."""
        try:
            tx_adapter = _CatalogTxAdapter(connection) if isinstance(connection, GraphTx) else None
            cm = None if connection is not None else self._engine.connect()
            conn = tx_adapter or connection or cm
            if cm is not None:
                conn = cm.__enter__()
            try:
                rows = conn.execute(self._text("SELECT table_name FROM information_schema.tables WHERE table_schema=:schema AND (table_name IN ('graph_nodes','graph_edges') OR table_name LIKE 'graph_nodes\\_%' OR table_name LIKE 'graph_edges\\_%')"), {"schema": self._schema}).fetchall()
                tables = {r[0] for r in rows}
                if any(name not in {"graph_nodes", "graph_edges"} for name in tables):
                    return "partial"
                if not tables:
                    return "fresh"
                if tables != {"graph_nodes", "graph_edges"}:
                    return "partial"
                cols = {}
                for table in tables:
                    cols[table] = conn.execute(self._text("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema=:schema AND table_name=:table ORDER BY ordinal_position"), {"schema": self._schema, "table": table}).fetchall()
                n_pk = tuple(r[0] for r in conn.execute(self._text("SELECT kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema AND tc.table_name=kcu.table_name WHERE tc.table_schema=:schema AND tc.table_name='graph_nodes' AND tc.constraint_type='PRIMARY KEY' ORDER BY kcu.ordinal_position"), {"schema": self._schema}).fetchall())
                e_pk = tuple(r[0] for r in conn.execute(self._text("SELECT kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema AND tc.table_name=kcu.table_name WHERE tc.table_schema=:schema AND tc.table_name='graph_edges' AND tc.constraint_type='PRIMARY KEY' ORDER BY kcu.ordinal_position"), {"schema": self._schema}).fetchall())
                target_meta_n = [(c.name, "text", "NO" if c.not_null else "YES") for c in GRAPH_STORE_SCHEMA.tables[0].columns]
                target_meta_e = [(c.name, "text", "NO") for c in GRAPH_STORE_SCHEMA.tables[1].columns]
                actual_n = [(r[0], r[1], r[2]) for r in cols["graph_nodes"]]
                actual_e = [(r[0], "jsonb" if r[1] in ("json", "jsonb") else r[1], r[2]) for r in cols["graph_edges"]]
                # PostgreSQL reports JSONB as ``jsonb`` and all text columns
                # as ``text`` through information_schema.
                target_meta_n[-1] = ("properties", "jsonb", "NO")
                target_meta_e[-1] = ("properties", "jsonb", "NO")
                if actual_n != target_meta_n or actual_e != target_meta_e:
                    legacy_n = [("node_type", "text", "NO"), ("node_id", "text", "NO"), ("space_id", "text", "YES"), ("properties", "jsonb", "NO")]
                    legacy_e = [("from_type", "text", "NO"), ("from_id", "text", "NO"), ("relation", "text", "NO"), ("to_type", "text", "NO"), ("to_id", "text", "NO"), ("properties", "jsonb", "NO")]
                    if actual_n != legacy_n or actual_e != legacy_e:
                        return "partial"

                constraints = conn.execute(self._text("SELECT table_name, constraint_type FROM information_schema.table_constraints WHERE table_schema=:schema AND table_name IN ('graph_nodes','graph_edges')"), {"schema": self._schema}).fetchall()
                if any(r[1] != "PRIMARY KEY" for r in constraints):
                    return "partial"
                indexes = conn.execute(self._text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=:schema AND tablename IN ('graph_nodes','graph_edges')"), {"schema": self._schema}).fetchall()
                secondary = []
                primary = 0
                for name, definition in indexes:
                    normalized = "".join((definition or "").lower().split())
                    if "_pkey" in str(name).lower() or "primarykey" in normalized:
                        primary += 1
                    else:
                        secondary.append(normalized)
                if primary != 2 or len(secondary) != 4:
                    return "partial"
                required_fragments = ("graph_nodes", "graph_nodes", "graph_edges", "graph_edges")
                if not all(any(table in definition for definition in secondary) for table in required_fragments):
                    return "partial"
                if not any("pack_id" in definition and "properties" in definition for definition in secondary):
                    return "partial"
                if not any("space_id" in definition for definition in secondary):
                    return "partial"
                if not any("from_id" in definition for definition in secondary):
                    return "partial"
                if not any("to_id" in definition for definition in secondary):
                    return "partial"
                if conn.execute(self._text("SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname IN ('graph_nodes','graph_edges') AND NOT t.tgisinternal LIMIT 1"), {"schema": self._schema}).fetchone():
                    return "partial"
                if conn.execute(self._text("SELECT 1 FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=:schema AND r.relname IN ('graph_nodes','graph_edges') AND c.contype NOT IN ('p') LIMIT 1"), {"schema": self._schema}).fetchone():
                    return "partial"
                if conn.execute(self._text("SELECT 1 FROM pg_policies WHERE schemaname=:schema AND tablename IN ('graph_nodes','graph_edges') LIMIT 1"), {"schema": self._schema}).fetchone():
                    return "partial"
                if conn.execute(self._text("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname IN ('graph_nodes','graph_edges') AND (c.relrowsecurity OR c.relforcerowsecurity) LIMIT 1"), {"schema": self._schema}).fetchone():
                    return "partial"
                # A view depends on a table through its rewrite rule.  Join
                # pg_depend's referenced object (refobjid), not the view's
                # own objid, so a harmless view mentioning graph_nodes is
                # classified as partial instead of slipping through.
                if conn.execute(self._text("SELECT 1 FROM pg_class v JOIN pg_namespace vn ON vn.oid=v.relnamespace JOIN pg_rewrite rw ON rw.ev_class=v.oid JOIN pg_depend d ON d.objid=rw.oid JOIN pg_class dep ON dep.oid=d.refobjid JOIN pg_namespace dn ON dn.oid=dep.relnamespace WHERE vn.nspname=:schema AND dn.nspname=:schema AND dep.relname IN ('graph_nodes','graph_edges') AND v.relkind IN ('v','m') AND v.relname NOT IN ('graph_nodes','graph_edges') LIMIT 1"), {"schema": self._schema}).fetchone():
                    return "partial"
                if n_pk == ("node_id",) and e_pk == ("from_id", "relation", "to_id"):
                    return "target"
                if n_pk == ("node_type", "node_id") and e_pk == ("from_type", "from_id", "relation", "to_type", "to_id"):
                    return "legacy"
                return "partial"
            finally:
                if cm is not None:
                    cm.__exit__(None, None, None)
        except Exception:
            return "partial"

    @property
    def schema_state(self) -> str:
        return self._schema_state

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
        self,
        conn: Any,
        frontier_ids: list[str],
        cap: int,
        out: bool,
        pack_set: set[str] | None = None,
        include_unpackaged: bool = False,
        space_set: set[str] | None = None,
    ) -> dict[str, list[tuple[str, str, str, Any]]]:
        """One-round-trip candidate fetch for every node in `frontier_ids`.

        Equivalent to running "SELECT ... WHERE from_id=:fid LIMIT :cap" (or
        to_id for in-edges) once per frontier node, but as a single
        unnest+LATERAL query. `cap` is `limit` (the traversal's max possible
        "remaining" value) — a safe, hub-safe upper bound; callers still
        apply the live "remaining" cap by slicing the per-node row list.

        When `pack_set`/`space_set` are given, their filters (`_pack_where`,
        shared with `_SqlGraphStoreBase._fetch_edges_for_node`, and a plain
        `gn.space_id IN (...)` — `space_id` is a real column, unlike
        `pack_id`) are joined into the LATERAL subquery itself, so
        `LIMIT :cap` applies AFTER filtering, not before (issue #62, and
        issue #52 for the space leg).
        """
        if not frontier_ids:
            return {}
        anchor_col = "from_id" if out else "to_id"
        type_col = "to_type" if out else "from_type"
        id_col = "to_id" if out else "from_id"

        if pack_set is None and space_set is None:
            sql = f"""
                SELECT f.frontier_id, e.c1, e.c2, e.relation, e.properties
                FROM unnest(CAST(:ids AS text[])) AS f(frontier_id)
                CROSS JOIN LATERAL (
                    SELECT {type_col} AS c1, {id_col} AS c2, relation, properties
                    FROM {self._table('graph_edges')}
                    WHERE {anchor_col} = f.frontier_id
                    LIMIT :cap
                ) e
                """
            params: dict[str, Any] = {"ids": frontier_ids, "cap": cap}
        else:
            where_clauses: list[str] = []
            params = {"ids": frontier_ids, "cap": cap}
            if pack_set is not None:
                pack_where, pack_params = self._pack_where(
                    "gn.properties", "ge.properties", pack_set, include_unpackaged, "bf"
                )
                where_clauses.append(pack_where)
                params.update(pack_params)
            if space_set is not None:
                placeholders, space_params = self._in_placeholders(sorted(space_set), "bfsp")
                where_clauses.append(f"gn.space_id IN ({placeholders})")
                params.update(space_params)
            extra_where = " AND ".join(where_clauses)
            sql = f"""
                SELECT f.frontier_id, e.c1, e.c2, e.relation, e.properties
                FROM unnest(CAST(:ids AS text[])) AS f(frontier_id)
                CROSS JOIN LATERAL (
                    SELECT ge.{type_col} AS c1, ge.{id_col} AS c2, ge.relation, ge.properties
                    FROM {self._table('graph_edges')} ge
                    JOIN {self._table('graph_nodes')} gn
                      ON gn.node_type = ge.{type_col} AND gn.node_id = ge.{id_col}
                    WHERE ge.{anchor_col} = f.frontier_id AND {extra_where}
                    LIMIT :cap
                ) e
                """

        rows = conn.execute(self._text(sql), params).fetchall()
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
        # g.space_id is folded into props (see _merge_space) so this override
        # returns the same shape as the base's get_node-backed default.
        rows = conn.execute(
            self._text(
                f"""
                SELECT g.node_type, g.node_id, g.properties, g.space_id
                FROM unnest(CAST(:types AS text[]), CAST(:ids AS text[])) AS p(node_type, node_id)
                JOIN {self._table('graph_nodes')} g
                  ON g.node_type = p.node_type AND g.node_id = p.node_id
                """
            ),
            {"types": types, "ids": ids},
        ).fetchall()
        return {(r[0], r[1]): _merge_space(_as_dict(r[2]), r[3]) for r in rows}

    def _prefetch_frontier(
        self,
        frontier_ids: list[str],
        cap: int,
        out: bool,
        pack_set: set[str] | None = None,
        include_unpackaged: bool = False,
        space_set: set[str] | None = None,
    ) -> dict[str, list[tuple[str, str, str, Any]]]:
        """HOOK override — one short-lived connection, verbatim batched query
        (see module docstring). Do NOT let this fall back to the base's
        per-node default; that would silently regress hub-fanout perf."""
        with self._conn() as conn:
            return self._batch_frontier_edges(
                conn, frontier_ids, cap, out, pack_set, include_unpackaged, space_set
            )

    def _batch_node_props(
        self, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """HOOK override — one short-lived connection, verbatim batched query."""
        with self._conn() as conn:
            return self._batch_node_props_multi(conn, pairs)
