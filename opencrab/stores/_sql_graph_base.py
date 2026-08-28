"""
_SqlGraphStoreBase — shared implementation of the 20-method graph-store
surface (ensure_constraints, upsert_node, get_node, lookup_node_type,
delete_node, upsert_edge, run_cypher, find_neighbors, find_path, count_nodes,
list_packs, find_by_relations, get_node_by_id, export_nodes, export_edges,
upsert_nodes_batch, upsert_edges_batch, plus the store-owned lifecycle trio
available/ping/close), parameterised by a ``SqlDialect`` (SQLITE or POSTGRES
from ``_sql_dialect.py``) — the Stage 6b (graph) counterpart of
``_sql_doc_base.py`` (Stage 6a, doc-store).

STAGE 6b STATUS: wired into LocalGraphStore and PGGraphStore. Both adapters
use the global graph identity declared by ``GRAPH_STORE_SCHEMA`` and retain
their backend-specific connection, transaction, and lifecycle hooks.

ADOPTION CONTRACT — a subclass must:
  1. Set ``self._dialect = SQLITE`` or ``POSTGRES`` before any base method
     runs (typically the first line of ``__init__``).
  2. Implement the low-level hooks below — connection/transaction management
     genuinely differs (SQLite: thread-local sqlite3 connection + a
     process-wide write lock via ``_SqliteConnMixin``; PG: short-lived
     SQLAlchemy engine connections via ``with self._engine.connect()/
     .begin()``):
       - ``_table(name) -> str``                     table-name qualification
       - ``_fetch_all(sql, params) -> list[tuple]``   SELECT, all rows
       - ``_fetch_one(sql, params) -> tuple | None``  SELECT, first row
       - ``_require_available() -> None``             raise if store unavailable
  3. Override the BFS prefetch hooks IF batching matters for that backend
     (see "THE _prefetch_frontier / _batch_node_props HOOKS" below) — the
     base's default implementations reproduce the historical LocalGraphStore
     per-node-query behavior and are correct (if less round-trip-efficient)
     for any subclass that does not override them.
  4. Provide ``available`` / ``ping()`` / ``close()`` and own DDL bootstrap,
     calling ``self._dialect.render_ddl(GRAPH_STORE_SCHEMA, schema_name=...)``
     for the two tables' CREATE TABLE / CREATE INDEX statements — lifecycle
     stays store-specific because it's entangled with each backend's
     connection model (identical division of responsibility to
     ``_sql_doc_base.py``'s adoption contract).

ALL 20 METHODS AND WHERE THEY LIVE:
    Shared here (parameterised by ``self._dialect``, no override needed):
        ensure_constraints, upsert_node, get_node, lookup_node_type,
        delete_node, upsert_edge, run_cypher, find_neighbors, find_path,
        count_nodes, list_packs, find_by_relations, get_node_by_id,
        export_nodes, export_edges, upsert_nodes_batch, upsert_edges_batch
        (17 methods — the BFS-internal helpers ``_expand``/
        ``_fetch_node_props_by_id``/``_fetch_edges_for_node`` are private,
        not counted in the 20).
    Store-owned (per adoption contract point 4): available, ping, close
        (3 methods). 17 + 3 = 20.

THE _prefetch_frontier / _batch_node_props HOOKS (find_neighbors' BFS):
    find_neighbors' BFS was already extracted to a level-batched skeleton in
    Stage 4/the PG port (process one whole BFS depth-level at a time, not one
    node at a time) — this base adopts THAT structure verbatim as the shared
    algorithm, since queue-based BFS with FIFO insertion already visits nodes
    in level order, so "all same-depth nodes, batched" produces the exact
    same ``results`` append sequence as "one node at a time" (this equivalence
    is what let pg_graph_store.py's batched port pass byte-for-byte parity
    against LocalGraphStore's original per-node port — see its module
    docstring). Two hook points:
        ``_prefetch_frontier(frontier_ids, cap, out) -> dict[str, list[tuple]]``
            candidate (other_type, other_id, relation, properties_raw) rows
            for every node in ``frontier_ids``, keyed by frontier node id.
            DEFAULT (this base): one query per id (``_fetch_edges_for_node``)
            — reproduces LocalGraphStore's historical per-node SQL-LIMIT
            query, just called from inside the level loop instead of a
            per-node while-loop.
        ``_batch_node_props(pairs) -> dict[(type, id), dict]``
            properties for a set of (node_type, node_id) pairs.
            DEFAULT (this base): one ``get_node()`` call per pair.
    PgGraphStore's adopter (F4) OVERRIDES BOTH with its existing
    ``_batch_frontier_edges``/``_batch_node_props_multi`` VERBATIM (one
    ``unnest(...) CROSS JOIN LATERAL`` round trip per level instead of N) —
    do not lose that optimization when wiring PGGraphStore onto this base.

    KNOWN TRADEOFF (flagged, not fixed, here): both this base's default
    ``_prefetch_frontier`` and PgGraphStore's existing override cap each
    per-node fetch at ``limit`` (a level-wide, static upper bound), not at
    the live "remaining slots" LocalGraphStore's ORIGINAL per-node loop used
    (which shrinks as ``results`` fills up within the same level — see
    local_graph_store.py's "수정 1" comment on ``_expand``, a documented 32x
    hub-fanout speedup). Final ``results`` are IDENTICAL either way (``_expand``
    still slices ``batch[node][:remaining]`` before appending), so this is a
    perf-only, not correctness, gap — but it means adopting LocalGraphStore
    onto this base as-is would slightly loosen (to PG's already-shipped,
    already-benchmarked level) its own historical hub-fanout optimization.
    The adapters should re-run bench_graph_backends.py after substantial
    traversal changes because hub fan-out remains load-bearing.

PACK_ID TYPE UNIFICATION (Stage 6b Deliverable 2, user-approved: unify to
    str): ``list_packs()`` projects pack_id via a raw JSON-field extraction.
    SQLite's ``json_extract()`` preserves the stored JSON scalar's native
    type (an int-valued pack_id round-trips as Python ``int``); PG's ``->>``
    always coerces to ``text``. This base's ``list_packs()`` wraps the
    projected value in ``str(...)`` unconditionally (the WHERE clause already
    filters out ``NULL`` pack_id, so this is total, not partial), which
    lives HERE rather than in either store so F3 (SQLite adopter) inherits
    the str-coercion automatically instead of needing its own fix. See
    tests/test_pg_graph_doc_parity.py::test_list_packs_pack_id_is_str_on_both_backends.
"""

from __future__ import annotations

import abc
import hashlib
import json
import logging
import re
import threading
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import (
    EdgeIdentityConflict,
    EdgeWriteReceipt,
    GraphSchemaMigrationRequired,
    NodeIdentityConflict,
    NodeWriteReceipt,
    ProvenanceBatchReceipt,
    ProvenanceWriteReceipt,
    canonical_edge_digest,
    canonical_json_bytes,
    canonical_node_digest,
    normalize_edge_properties,
    parse_properties_object,
    prepare_node,
    validate_digest,
)
from opencrab.stores._graph_common import (
    KEYWORD_SEARCH_FIELDS,
    _as_dict,
    _edge_passes,
    _merge_space,
    _node_passes,
    _space_passes,
    _validate_search_fields,
)
from opencrab.stores._sql_dialect import Column, IndexSpec, SchemaSpec, SqlDialect, TableSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# One dialect-neutral schema spec for the two graph-store tables. Column-by-
# column checked against local_graph_store.py's ``_DDL`` and
# pg_graph_store.py's ``_DDL_TEMPLATE`` at authoring time (Stage 6b).
#
# STATEMENT ORDER NOTE: ``SqlDialect.render_ddl`` emits each table immediately
# followed by ITS OWN indexes (established doc-store convention from Stage
# 6a). The hand-written originals instead emit both tables first, then all
# three indexes. Both orders are behaviorally inert (every statement is
# idempotent ``IF NOT EXISTS`` and an index only ever references its own
# already-just-created table) — see _sql_doc_base.py's identical reordering
# for doc_nodes/doc_sources/audit_log, already shipped in Stage 6a.
# ---------------------------------------------------------------------------

GRAPH_STORE_SCHEMA = SchemaSpec(
    tables=(
        TableSpec(
            name="graph_nodes",
            columns=(
                Column("node_type", "text"),
                Column("node_id", "text"),
                Column("space_id", "text", not_null=False),
                Column("properties", "json", default="{}"),
            ),
            primary_key=("node_id",),
        ),
        TableSpec(
            name="graph_edges",
            columns=(
                Column("from_type", "text"),
                Column("from_id", "text"),
                Column("relation", "text"),
                Column("to_type", "text"),
                Column("to_id", "text"),
                Column("properties", "json", default="{}"),
            ),
            primary_key=("from_id", "relation", "to_id"),
        ),
    ),
    indexes=(
        IndexSpec("idx_nodes_pack", "graph_nodes", json_key=("properties", "pack_id")),
        # issue #54 audit finding [4]: export_nodes/count_exported_nodes's
        # combined "pack_id OR source OR source_id) AND space_id" WHERE was
        # measured (250k rows, 200 packs x 3 spaces) doing a full
        # `SCAN graph_nodes` -- idx_nodes_pack alone doesn't help because
        # SQLite won't turn a 3-way OR across one indexed + two unindexed
        # expressions into an index-union. Adding this single plain-column
        # index on the always-present, highly-selective space_id flips the
        # plan to `SEARCH ... USING INDEX idx_nodes_space`, cutting the
        # measured COUNT from ~209ms to ~93-116ms (see PR discussion) --
        # same "index instead of eating the scan" resolution #63 used for
        # its own 3x regression.
        #
        # COST, MEASURED (audit finding #54-[1], 250k-row SQLite table):
        # - `CREATE INDEX IF NOT EXISTS` for this whole schema runs exactly
        #   ONCE per store instance, inside `_init_db()`/`__init__` (see
        #   LocalGraphStore/PGGraphStore) -- NOT once per thread-local
        #   connection. Per-thread connections (`_new_conn()` in
        #   _sqlite_base.py) only run two PRAGMAs (WAL, synchronous); they
        #   never touch DDL. So there is exactly one first-ever build per
        #   physical DB file, not a per-thread/per-connection cost.
        # - First-ever `CREATE INDEX IF NOT EXISTS` on an existing 250k-row
        #   DB (the one-time migration every LocalGraphStore/PGGraphStore
        #   pays on its first open after upgrading, in that single
        #   `_init_db()` call): ~150ms, synchronous, blocks that store's
        #   constructor (same as any DDL already run there -- not a new
        #   blocking pattern, just one more statement in the existing
        #   list). Every later store open against the same (already
        #   indexed) file: ~0.1ms (existence check only).
        # - Write path: upsert cost with vs without this index was
        #   benchmarked at 250k existing rows; the measured delta was at
        #   noise level (within run-to-run variance, no consistent
        #   direction) rather than a clear regression -- not reporting a
        #   specific number here since it did not reproduce reliably
        #   across runs.
        # Verdict: a bounded ~150ms one-time migration and no measurable
        # write regression against the measured 2x+ read improvement
        # above, so the index is kept.
        IndexSpec("idx_nodes_space", "graph_nodes", expr="space_id"),
        IndexSpec("idx_edges_from", "graph_edges", expr="from_id"),
        IndexSpec("idx_edges_to", "graph_edges", expr="to_id"),
    ),
)


_GRAPH_SQL_CONTROLS = frozenset({
    "BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "ABORT",
    "SET", "RESET", "DISCARD", "LOCK", "PREPARE", "EXECUTE", "DEALLOCATE", "DECLARE",
    "FETCH", "MOVE", "CLOSE", "LISTEN", "UNLISTEN", "NOTIFY", "CHECKPOINT",
    "ATTACH", "DETACH", "VACUUM", "PRAGMA",
})
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def validate_graph_sql(sql: str) -> None:
    """Allow exactly one ordinary SQL statement and reject state controls."""
    if not isinstance(sql, str):
        raise ValueError("graph SQL lexical form is invalid")
    tokens: list[str] = []
    statement_count = 0
    saw_effective = False
    boundary = False
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError("graph SQL lexical form is invalid")
            continue
        if c == ";":
            if not saw_effective:
                raise ValueError("graph SQL must contain exactly one statement")
            if boundary:
                raise ValueError("graph SQL must contain exactly one statement")
            boundary = True
            i += 1
            continue
        if c in ("'", '"', "`", "["):
            opening = c
            closing = "]" if c == "[" else c
            i += 1
            closed = False
            while i < n:
                if sql[i] == closing:
                    if i + 1 < n and sql[i + 1] == closing:
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                if sql[i] == "\\" and opening == "'" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            if not closed:
                raise ValueError("graph SQL lexical form is invalid")
            if boundary:
                raise ValueError("graph SQL must contain exactly one statement")
            saw_effective = True
            continue
        if c == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                if end < 0:
                    raise ValueError("graph SQL lexical form is invalid")
                if boundary:
                    raise ValueError("graph SQL must contain exactly one statement")
                saw_effective = True
                i = end + len(tag)
                continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j].upper()
            if boundary and word in _GRAPH_SQL_CONTROLS:
                raise ValueError("graph SQL control statements are forbidden")
            if boundary:
                raise ValueError("graph SQL must contain exactly one statement")
            if not saw_effective:
                if word in _GRAPH_SQL_CONTROLS:
                    raise ValueError("graph SQL control statements are forbidden")
                statement_count += 1
            saw_effective = True
            tokens.append(word)
            i = j
            continue
        if boundary:
            # punctuation after a terminator starts a second statement
            raise ValueError("graph SQL must contain exactly one statement")
        saw_effective = True
        i += 1
    if not saw_effective or statement_count != 1:
        raise ValueError("graph SQL must contain exactly one statement")


class GraphResult:
    """Small result facade that prevents cursor/driver objects escaping."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def fetchone(self) -> Any:
        return self._result.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._result.fetchall())

    @property
    def rowcount(self) -> int:
        return int(getattr(self._result, "rowcount", -1))


class GraphTx:
    """Dialect-aware, single-statement transaction callback surface."""

    def __init__(self, connection: Any, dialect: SqlDialect, text_factory: Callable[[str], Any] | None = None) -> None:
        self._dialect = dialect
        def execute_impl(sql: str, params: dict[str, Any] | None = None) -> GraphResult:
            validate_graph_sql(sql)
            statement = text_factory(sql) if text_factory else sql
            return GraphResult(connection.execute(statement, params or {}))

        def executemany_impl(sql: str, rows: Iterable[dict[str, Any]]) -> GraphResult:
            validate_graph_sql(sql)
            statement = text_factory(sql) if text_factory else sql
            bound_rows = list(rows)
            executemany = getattr(connection, "executemany", None)
            if executemany is not None and text_factory is None:
                return GraphResult(executemany(statement, bound_rows))
            return GraphResult(connection.execute(statement, bound_rows))

        self._execute_impl = execute_impl
        self._executemany_impl = executemany_impl

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> GraphResult:
        return self._execute_impl(sql, params)

    def executemany(self, sql: str, rows: Iterable[dict[str, Any]]) -> GraphResult:
        return self._executemany_impl(sql, rows)

    def fetchone(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return self.execute(sql, params).fetchall()

    @staticmethod
    def rowcount(result: GraphResult) -> int:
        return result.rowcount


class _SqlGraphStoreBase(abc.ABC):
    _dialect: SqlDialect

    # ------------------------------------------------------------------
    # Hooks subclasses must implement
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _table(self, name: str) -> str: ...

    @abc.abstractmethod
    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[tuple]: ...

    @abc.abstractmethod
    def _fetch_one(self, sql: str, params: dict[str, Any]) -> tuple | None: ...

    @abc.abstractmethod
    def _require_available(self) -> None: ...

    def _require_write_available(self) -> None:
        """Require a classified target schema for graph mutations.

        Legacy and partially classified databases retain their read surface so
        operators can inspect them, but no mutation may infer that they are
        safe merely because a connection exists.
        """
        self._require_available()
        state = getattr(self, "_schema_state", "available")
        if state not in {"target", "available"}:
            raise GraphSchemaMigrationRequired("graph schema migration required")

    def _graph_tx_is_active(self) -> bool:
        """Return whether this thread owns a graph transaction callback.

        The store object is shared by the local concurrency callers, while
        each SQLite worker has its own connection.  A boolean on ``self``
        therefore made an unrelated thread look like a nested transaction.
        Keep the marker thread-local; the database connection/transaction
        remains the authority for the actual boundary.
        """
        state = getattr(self, "_graph_tx_state", None)
        return bool(state is not None and getattr(state, "active", False))

    def _set_graph_tx_active(self, active: bool) -> None:
        state = getattr(self, "_graph_tx_state", None)
        if state is None:
            state = threading.local()
            self._graph_tx_state = state
        state.active = active

    def _run_graph_tx(self, callback: Callable[[GraphTx], Any], *, immediate: bool = False, snapshot_path: Path | None = None) -> Any:
        """Run a callback on one connection.

        Concrete adapters override this to supply their native transaction
        boundary.  The fallback keeps small in-memory test adopters useful.
        """
        if self._graph_tx_is_active():
            raise RuntimeError("nested graph transaction is not allowed")
        conn = getattr(self, "_conn", None)
        if conn is None:
            raise RuntimeError("graph transaction is unavailable")
        if callable(conn):
            conn = conn
        if hasattr(self, "_tx"):
            self._set_graph_tx_active(True)
            try:
                with self._tx(immediate=immediate) as raw:
                    return callback(GraphTx(raw, self._dialect, getattr(self, "_text", None)))
            finally:
                self._set_graph_tx_active(False)
        self._set_graph_tx_active(True)
        try:
            result = callback(GraphTx(conn, self._dialect, getattr(self, "_text", None)))
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._set_graph_tx_active(False)

    def _run_mutation_tx(self, callback: Callable[[GraphTx], Any]) -> Any:
        """Run a mutation with the backend's transaction options.

        SQLite needs ``BEGIN IMMEDIATE`` for its process-local writer lock;
        PostgreSQL's adapter opens every graph transaction as a write-capable
        transaction and deliberately rejects SQLite-only options.
        """
        return self._run_graph_tx(callback, immediate=self._dialect.name == "sqlite")

    def _lock_graph_rows(
        self,
        tx: GraphTx,
        node_ids: Iterable[str] = (),
        edge_keys: Iterable[tuple[str, str, str]] = (),
    ) -> None:
        """Lock graph rows in the backend's canonical order.

        SQLite already serializes graph writers with ``BEGIN IMMEDIATE`` and
        therefore needs no row-level operation.  PostgreSQL overrides this
        hook with ``FOR UPDATE`` reads.  Keeping the calls in the shared
        mutation bodies makes the lock plan auditable and prevents one SQL
        adapter from silently skipping a phase when a new writer is added.
        """

    # ------------------------------------------------------------------
    # Schema (no-op for both current backends — PRIMARY KEY covers uniqueness)
    # ------------------------------------------------------------------

    def ensure_constraints(self) -> None:
        self._require_write_available()

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
        space_id: str | None = None,
        *,
        return_receipt: bool = False,
    ) -> dict[str, Any] | NodeWriteReceipt:
        self._require_write_available()
        node_type, props, space_id, digest = prepare_node(node_type, node_id, properties, space_id)
        table = self._table("graph_nodes")
        insert_sql = self._dialect.insert(
            table, ["node_type", "node_id", "space_id", "properties"], json_columns=["properties"]
        ) + "\nON CONFLICT (node_id) DO NOTHING"
        params = {"node_type": node_type, "node_id": node_id, "space_id": space_id, "properties": json.dumps(props, ensure_ascii=False)}

        def body(tx: GraphTx) -> dict[str, Any] | NodeWriteReceipt:
            self._lock_graph_rows(tx, (node_id,))
            row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {table} WHERE node_id=:nid", {"nid": node_id})
            operation = "idempotent" if row is not None else "created"
            if row is None:
                inserted = tx.execute(insert_sql, params)
                if tx.rowcount(inserted) == 0:
                    operation = "idempotent"
                row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {table} WHERE node_id=:nid", {"nid": node_id})
            if row is None:
                raise RuntimeError("graph node insert did not produce a row")
            stored_type, stored_space, stored_raw = row
            stored_props = _merge_space(_as_dict(stored_raw), stored_space)
            try:
                stored_digest = canonical_node_digest(stored_type, stored_space or stored_props.get("space"), stored_props)
            except (TypeError, ValueError):
                stored_digest = ""
            if stored_digest != digest:
                raise NodeIdentityConflict(f"node identity conflict: {node_id}")
            if operation != "created":
                operation = "idempotent"
            if return_receipt:
                return NodeWriteReceipt(operation, node_id, stored_type, stored_space, stored_props, stored_digest)
            return stored_props
        return self._run_mutation_tx(body)

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        # space_id is folded in for the same reason as export_nodes (see
        # _merge_space). This is the funnel for _batch_node_props (hence
        # find_neighbors' BFS) and find_path, so fixing it here covers them.
        sql = (
            f"SELECT properties, space_id FROM {self._table('graph_nodes')}"
            " WHERE node_type=:node_type AND node_id=:node_id"
        )
        row = self._fetch_one(sql, {"node_type": node_type, "node_id": node_id})
        return _merge_space(_as_dict(row[0]), row[1]) if row else None

    def lookup_node_type(self, node_id: str) -> str | None:
        """No ``_require_available()`` — mirrors both existing stores, which
        return ``None`` on an unavailable store instead of raising (used as a
        best-effort probe by OntologyBuilder)."""
        # Schema classification gates mutations, not this read probe. A
        # legacy database is intentionally left inspectable so an operator
        # can discover the endpoint type before planning its migration.
        if not getattr(self, "_available", False):
            return None
        sql = f"SELECT node_type FROM {self._table('graph_nodes')} WHERE node_id=:node_id LIMIT 1"
        row = self._fetch_one(sql, {"node_id": node_id})
        return row[0] if row else None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """True iff the node itself was deleted (unified B2 contract); the
        incident-edge cleanup is a side effect, not the signal. Both DELETEs
        run in the same managed transaction so a crash between them can never
        leave orphaned edges."""
        self._require_write_available()
        if not isinstance(node_type, str) or not isinstance(node_id, str) or not node_type or not node_id:
            raise ValueError("graph identity fields must be non-empty strings")
        nodes, edges = self._table("graph_nodes"), self._table("graph_edges")
        def body(tx: GraphTx) -> bool:
            self._lock_graph_rows(tx, (node_id,))
            incident = tx.fetchall(
                f"SELECT from_id, relation, to_id FROM {edges} WHERE from_id=:nid OR to_id=:nid",
                {"nid": node_id},
            )
            self._lock_graph_rows(tx, (), ((row[0], row[1], row[2]) for row in incident))
            row = tx.fetchone(f"SELECT node_type FROM {nodes} WHERE node_id=:nid", {"nid": node_id})
            if row is None or row[0] != node_type:
                return False
            result = tx.execute(f"DELETE FROM {nodes} WHERE node_id=:nid AND node_type=:nt", {"nid": node_id, "nt": node_type})
            if tx.rowcount(result) != 1:
                raise RuntimeError("graph node delete rowcount mismatch")
            tx.execute(f"DELETE FROM {edges} WHERE from_id=:nid OR to_id=:nid", {"nid": node_id})
            return True
        return self._run_mutation_tx(body)

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
        *,
        return_receipt: bool = False,
    ) -> bool | EdgeWriteReceipt:
        self._require_write_available()
        for value in (from_type, from_id, relation, to_type, to_id):
            if not isinstance(value, str) or not value:
                raise ValueError("graph identity fields must be non-empty strings")
        props = normalize_edge_properties(from_id, relation, to_id, properties)
        digest = canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)
        nodes, edges = self._table("graph_nodes"), self._table("graph_edges")
        insert_sql = self._dialect.insert(edges, ["from_type", "from_id", "relation", "to_type", "to_id", "properties"], json_columns=["properties"]) + "\nON CONFLICT (from_id, relation, to_id) DO NOTHING"
        params = {"from_type": from_type, "from_id": from_id, "relation": relation, "to_type": to_type, "to_id": to_id, "properties": json.dumps(props, ensure_ascii=False)}
        def body(tx: GraphTx) -> bool | EdgeWriteReceipt:
            self._lock_graph_rows(tx, (from_id, to_id), ((from_id, relation, to_id),))
            endpoint_rows = tx.fetchall(f"SELECT node_id, node_type FROM {nodes} WHERE node_id IN (:fid, :tid)", {"fid": from_id, "tid": to_id})
            endpoint_map = {r[0]: r[1] for r in endpoint_rows}
            if from_id not in endpoint_map:
                return False
            if to_id not in endpoint_map:
                return False
            if endpoint_map[from_id] != from_type:
                return False
            if endpoint_map[to_id] != to_type:
                return False
            row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": from_id, "rel": relation, "tid": to_id})
            operation = "idempotent" if row is not None else "created"
            if row is None:
                result = tx.execute(insert_sql, params)
                if tx.rowcount(result) == 0:
                    operation = "idempotent"
                row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": from_id, "rel": relation, "tid": to_id})
            if row is None:
                raise RuntimeError("graph edge insert did not produce a row")
            stored_ft, stored_tt, stored_raw = row
            stored_props = normalize_edge_properties(from_id, relation, to_id, _as_dict(stored_raw))
            try:
                stored_digest = canonical_edge_digest(from_id, relation, to_id, stored_ft, stored_tt, stored_props)
            except (TypeError, ValueError):
                stored_digest = ""
            if stored_digest != digest:
                raise EdgeIdentityConflict(f"edge identity conflict: ({from_id}, {relation}, {to_id})")
            if return_receipt:
                return EdgeWriteReceipt(operation, from_id, relation, to_id, stored_ft, stored_tt, stored_props, stored_digest)
            return True
        return self._run_mutation_tx(body)

    def get_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
    ) -> dict[str, Any] | None:
        """Same 5-column WHERE as ``upsert_edge``'s ``conflict_cols`` — see
        ``GraphStore.get_edge``'s docstring for the cross-backend contract."""
        self._require_available()
        sql = (
            f"SELECT from_type, to_type, properties FROM {self._table('graph_edges')}"
            " WHERE from_id=:from_id AND relation=:relation AND to_id=:to_id"
        )
        row = self._fetch_one(
            sql,
            {
                "from_type": from_type,
                "from_id": from_id,
                "relation": relation,
                "to_type": to_type,
                "to_id": to_id,
            },
        )
        if not row or row[0] != from_type or row[1] != to_type:
            return None
        return normalize_edge_properties(from_id, relation, to_id, _as_dict(row[2]))

    def get_node_digest(self, node_id: str, *, node_type: str | None = None) -> str | None:
        self._require_available()
        sql = f"SELECT node_type, space_id, properties FROM {self._table('graph_nodes')} WHERE node_id=:nid"
        params: dict[str, Any] = {"nid": node_id}
        if node_type is not None:
            sql += " AND node_type=:nt"
            params["nt"] = node_type
        row = self._fetch_one(sql, params)
        if not row:
            return None
        props = _merge_space(_as_dict(row[2]), row[1])
        try:
            return canonical_node_digest(row[0], row[1] or props.get("space"), props)
        except (TypeError, ValueError):
            return None

    def get_edge_digest(
        self, from_id: str, relation: str, to_id: str, *, from_type: str | None = None, to_type: str | None = None
    ) -> str | None:
        self._require_available()
        row = self._fetch_one(
            f"SELECT from_type, to_type, properties FROM {self._table('graph_edges')} WHERE from_id=:fid AND relation=:rel AND to_id=:tid",
            {"fid": from_id, "rel": relation, "tid": to_id},
        )
        if not row or (from_type is not None and row[0] != from_type) or (to_type is not None and row[1] != to_type):
            return None
        try:
            props = normalize_edge_properties(from_id, relation, to_id, _as_dict(row[2]))
            return canonical_edge_digest(from_id, relation, to_id, row[0], row[1], props)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _provenance_schema_fingerprint() -> str:
        schema = {
            "tables": [
                {"name": table.name, "columns": [[c.name, c.kind, c.not_null, c.default] for c in table.columns], "primary_key": list(table.primary_key)}
                for table in GRAPH_STORE_SCHEMA.tables
            ],
            "indexes": [[i.name, i.table, i.expr, list(i.json_key) if i.json_key else None] for i in GRAPH_STORE_SCHEMA.indexes],
        }
        return hashlib.sha256(
            b"opencrab.issue80.graph-schema.v1\0" + canonical_json_bytes(schema)
        ).hexdigest()

    def _graph_fingerprint_tx(self, tx: GraphTx) -> str:
        """Compute the backend-neutral graph snapshot fingerprint in ``tx``."""
        nodes = self._table("graph_nodes")
        edges = self._table("graph_edges")
        node_items: list[list[str]] = []
        for node_id, node_type, space_id, raw in tx.fetchall(
            f"SELECT node_id, node_type, space_id, properties FROM {nodes}", {}
        ):
            try:
                props = _merge_space(parse_properties_object(raw), space_id)
                digest = canonical_node_digest(node_type, space_id or props.get("space"), props)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"graph pack provenance malformed properties: node {node_id}") from exc
            node_items.append([node_id, digest])
        edge_items: list[list[str]] = []
        for from_id, relation, to_id, from_type, to_type, raw in tx.fetchall(
            f"SELECT from_id, relation, to_id, from_type, to_type, properties FROM {edges}", {}
        ):
            try:
                props = parse_properties_object(raw)
                props = normalize_edge_properties(from_id, relation, to_id, props)
                digest = canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"graph pack provenance malformed properties: edge ({from_id}, {relation}, {to_id})"
                ) from exc
            edge_items.append([from_id, relation, to_id, digest])
        payload = {
            "schema_fingerprint": self._provenance_schema_fingerprint(),
            "nodes": sorted(node_items),
            "edges": sorted(edge_items),
        }
        return hashlib.sha256(
            b"opencrab.issue80.provenance-target.v1\0" + canonical_json_bytes(payload)
        ).hexdigest()

    def graph_fingerprint(self) -> str:
        """Return the current graph-owned snapshot fingerprint."""
        self._require_available()

        def body(tx: GraphTx) -> str:
            return self._graph_fingerprint_tx(tx)

        return self._run_graph_tx(body)

    @staticmethod
    def _validate_provenance_record(record: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(record, dict):
            raise ValueError("malformed graph provenance record")
        common = {
            "kind", "target_fingerprint", "expected_current_digest", "proposed_pack_id",
            "reason", "dry_run_evidence_digest", "allowed_properties_delta",
        }
        kind = record.get("kind")
        if kind == "node":
            required = common | {"node_id", "node_type"}
        elif kind == "edge":
            required = common | {"from_id", "relation", "to_id", "from_type", "to_type"}
        else:
            raise ValueError("malformed graph provenance record")
        if set(record) != required:
            raise ValueError("malformed graph provenance record")
        for key in ("target_fingerprint", "expected_current_digest", "dry_run_evidence_digest"):
            validate_digest(record[key], edge=(key == "expected_current_digest" and kind == "edge"))
        owner = record["proposed_pack_id"]
        if not isinstance(owner, str) or not owner:
            raise ValueError("malformed graph provenance owner")
        if record["reason"] not in {"inferred", "assumed"}:
            raise ValueError("malformed graph provenance reason")
        delta = record["allowed_properties_delta"]
        if not isinstance(delta, dict) or set(delta) != {"set", "remove"} or not isinstance(delta["set"], dict) or not isinstance(delta["remove"], list):
            raise ValueError("malformed graph provenance delta")
        if any(not isinstance(k, str) for k in delta["set"]) or any(not isinstance(k, str) for k in delta["remove"]):
            raise ValueError("malformed graph provenance delta")
        for key in ("node_id", "node_type") if kind == "node" else ("from_id", "relation", "to_id", "from_type", "to_type"):
            if not isinstance(record[key], str) or not record[key]:
                raise ValueError("malformed graph provenance identity")
        return kind, record

    def backfill_pack_provenance(self, records: Iterable[dict[str, Any]]) -> ProvenanceBatchReceipt:
        """Apply one frozen, tagged ownership plan as an all-or-none CAS."""
        self._require_write_available()
        if isinstance(records, (str, bytes, dict)):
            raise ValueError("graph provenance records must be a non-empty sequence")
        try:
            materialized = list(records)
        except TypeError as exc:
            raise ValueError("graph provenance records must be a non-empty sequence") from exc
        if not materialized:
            raise ValueError("graph provenance records must be a non-empty sequence")
        validated: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[str, Any]] = set()
        for record in materialized:
            kind, value = self._validate_provenance_record(record)
            key = value["node_id"] if kind == "node" else (value["from_id"], value["relation"], value["to_id"])
            marker = (kind, key)
            if marker in seen:
                raise RuntimeError(f"graph pack provenance duplicate key: {kind}:{key}")
            seen.add(marker)
            validated.append((kind, value))
        target = validated[0][1]["target_fingerprint"]
        if any(value["target_fingerprint"] != target for _kind, value in validated):
            raise ValueError("graph provenance target fingerprint mismatch")

        def body(tx: GraphTx) -> ProvenanceBatchReceipt:
            node_ids: set[str] = set()
            edge_keys: list[tuple[str, str, str]] = []
            for kind, record in validated:
                if kind == "node":
                    node_ids.add(record["node_id"])
                else:
                    node_ids.update((record["from_id"], record["to_id"]))
                    edge_keys.append((record["from_id"], record["relation"], record["to_id"]))
            self._lock_graph_rows(tx, node_ids, edge_keys)
            before = self._graph_fingerprint_tx(tx)
            if before != target:
                raise RuntimeError("graph pack provenance target fingerprint mismatch")
            nodes = self._table("graph_nodes")
            edges = self._table("graph_edges")
            pending: list[tuple[str, Any, str, str, dict[str, Any], dict[str, Any], str]] = []
            for kind, record in validated:
                owner = record["proposed_pack_id"]
                if kind == "node":
                    nid = record["node_id"]
                    row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {nodes} WHERE node_id=:nid", {"nid": nid})
                    if row is None:
                        raise RuntimeError(f"graph pack provenance node missing: {nid}")
                    if row[0] != record["node_type"]:
                        raise RuntimeError(f"graph pack provenance node snapshot mismatch: {nid}")
                    try:
                        raw_current = parse_properties_object(row[2])
                        current = _merge_space(raw_current, row[1])
                        digest = canonical_node_digest(row[0], row[1] or current.get("space"), current)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f"graph pack provenance malformed properties: node {nid}") from exc
                    if digest != record["expected_current_digest"]:
                        raise NodeIdentityConflict(f"stale node update: {nid}")
                    existing = current.get("pack_id")
                    if existing not in (None, "") and not isinstance(existing, str):
                        raise RuntimeError(f"graph pack provenance malformed owner: node {nid}")
                    if isinstance(existing, str) and existing and existing != owner:
                        raise RuntimeError("graph pack provenance conflict")
                    if isinstance(existing, str) and existing == owner:
                        expected_delta = {"set": {}, "remove": ["pack"] if "pack" in current else []}
                        after_props = dict(raw_current)
                        after_props.pop("pack", None)
                        operation = "assigned" if "pack" in current else "idempotent"
                    else:
                        expected_delta = {"set": {"pack_id": owner}, "remove": ["pack"] if "pack" in current else []}
                        # ``space_id`` is a dedicated column.  Keep it out of
                        # the JSON delta when it was only folded in for the
                        # digest/read shape.
                        after_props = dict(raw_current)
                        after_props.pop("pack", None)
                        after_props["pack_id"] = owner
                        operation = "assigned"
                    if record["allowed_properties_delta"] != expected_delta:
                        raise RuntimeError("graph pack provenance delta mismatch")
                    pending.append((kind, nid, digest, operation, current, after_props, record["node_type"]))
                else:
                    fid, rel, tid = record["from_id"], record["relation"], record["to_id"]
                    endpoints = tx.fetchall(f"SELECT node_id, node_type FROM {nodes} WHERE node_id IN (:fid,:tid)", {"fid": fid, "tid": tid})
                    endpoint_types = {row[0]: row[1] for row in endpoints}
                    if fid not in endpoint_types or tid not in endpoint_types:
                        missing = fid if fid not in endpoint_types else tid
                        raise ValueError(f"edge endpoint does not exist: {missing}")
                    if endpoint_types[fid] != record["from_type"]:
                        raise ValueError(f"edge endpoint type mismatch: {fid}")
                    if endpoint_types[tid] != record["to_type"]:
                        raise ValueError(f"edge endpoint type mismatch: {tid}")
                    row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": fid, "rel": rel, "tid": tid})
                    if row is None:
                        raise RuntimeError(f"graph pack provenance edge missing: ({fid}, {rel}, {tid})")
                    try:
                        raw_current = parse_properties_object(row[2])
                        current = normalize_edge_properties(fid, rel, tid, raw_current)
                        digest = canonical_edge_digest(fid, rel, tid, row[0], row[1], current)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f"graph pack provenance malformed properties: edge ({fid}, {rel}, {tid})") from exc
                    if digest != record["expected_current_digest"]:
                        raise EdgeIdentityConflict(f"stale edge update: ({fid}, {rel}, {tid})")
                    existing = current.get("pack_id")
                    if existing not in (None, "") and not isinstance(existing, str):
                        raise RuntimeError(f"graph pack provenance malformed owner: edge ({fid}, {rel}, {tid})")
                    if isinstance(existing, str) and existing and existing != owner:
                        raise RuntimeError("graph pack provenance conflict")
                    if isinstance(existing, str) and existing == owner:
                        expected_delta = {"set": {}, "remove": ["pack"] if "pack" in current else []}
                        after_props = dict(raw_current)
                        after_props.pop("pack", None)
                        operation = "assigned" if "pack" in current else "idempotent"
                    else:
                        expected_delta = {"set": {"pack_id": owner}, "remove": ["pack"] if "pack" in current else []}
                        after_props = dict(raw_current)
                        after_props.pop("pack", None)
                        after_props["pack_id"] = owner
                        operation = "assigned"
                    if record["allowed_properties_delta"] != expected_delta:
                        raise RuntimeError("graph pack provenance delta mismatch")
                    pending.append((kind, (fid, rel, tid), digest, operation, current, after_props, row[0]))

            receipts: list[ProvenanceWriteReceipt] = []
            for kind, key, before_digest, operation, current, after_props, _snapshot in pending:
                if operation == "assigned":
                    if kind == "node":
                        result = tx.execute(f"UPDATE {nodes} SET properties=:props WHERE node_id=:nid", {"props": json.dumps(after_props, ensure_ascii=False), "nid": key})
                    else:
                        result = tx.execute(f"UPDATE {edges} SET properties=:props WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"props": json.dumps(after_props, ensure_ascii=False), "fid": key[0], "rel": key[1], "tid": key[2]})
                    if tx.rowcount(result) != 1:
                        raise RuntimeError("graph pack provenance rowcount mismatch")
                # Re-read the changed row through the same transaction and
                # compute the receipt from the authoritative post-write row.
                if kind == "node":
                    row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {nodes} WHERE node_id=:nid", {"nid": key})
                    props = _merge_space(parse_properties_object(row[2]), row[1])
                    after_digest = canonical_node_digest(row[0], row[1] or props.get("space"), props)
                else:
                    row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": key[0], "rel": key[1], "tid": key[2]})
                    props = normalize_edge_properties(key[0], key[1], key[2], parse_properties_object(row[2]))
                    after_digest = canonical_edge_digest(key[0], key[1], key[2], row[0], row[1], props)
                receipts.append(ProvenanceWriteReceipt(kind, key, operation, before_digest, after_digest, 1 if operation == "assigned" else 0))
            after = self._graph_fingerprint_tx(tx)
            return ProvenanceBatchReceipt(before, after, tuple(receipts))

        return self._run_mutation_tx(body)

    def update_node(
        self, node_id: str, expected_current_digest: str, new_type: str,
        new_properties: dict[str, Any], new_space_id: str | None = None, *, return_receipt: bool = False,
    ) -> dict[str, Any] | NodeWriteReceipt:
        self._require_write_available()
        validate_digest(expected_current_digest)
        new_type, props, space_id, digest = prepare_node(new_type, node_id, new_properties, new_space_id)
        nodes, edges = self._table("graph_nodes"), self._table("graph_edges")
        def body(tx: GraphTx) -> dict[str, Any] | NodeWriteReceipt:
            self._lock_graph_rows(tx, (node_id,))
            incident = tx.fetchall(
                f"SELECT from_id, relation, to_id FROM {edges} WHERE from_id=:nid OR to_id=:nid",
                {"nid": node_id},
            )
            self._lock_graph_rows(
                tx,
                (value for row in incident for value in (row[0], row[2])),
                ((row[0], row[1], row[2]) for row in incident),
            )
            row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {nodes} WHERE node_id=:nid", {"nid": node_id})
            if row is None:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            current_props = _merge_space(_as_dict(row[2]), row[1])
            try:
                current_digest = canonical_node_digest(row[0], row[1] or current_props.get("space"), current_props)
            except (TypeError, ValueError):
                current_digest = ""
            if current_digest != expected_current_digest:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            result = tx.execute(f"UPDATE {nodes} SET node_type=:nt, space_id=:sid, properties=:props WHERE node_id=:nid", {"nt": new_type, "sid": space_id, "props": json.dumps(props, ensure_ascii=False), "nid": node_id})
            if tx.rowcount(result) != 1:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            # Type snapshots are compatibility fields and must follow the node.
            tx.execute(f"UPDATE {edges} SET from_type=:nt WHERE from_id=:nid", {"nt": new_type, "nid": node_id})
            tx.execute(f"UPDATE {edges} SET to_type=:nt WHERE to_id=:nid", {"nt": new_type, "nid": node_id})
            if return_receipt:
                return NodeWriteReceipt("updated", node_id, new_type, space_id, props, digest)
            return props
        return self._run_mutation_tx(body)

    def delete_edge(self, from_id: str, relation: str, to_id: str, *, owner_pack_id: str) -> bool:
        self._require_write_available()
        for value in (from_id, relation, to_id, owner_pack_id):
            if not isinstance(value, str) or not value:
                raise ValueError("graph identity fields must be non-empty strings")
        edges = self._table("graph_edges")
        def body(tx: GraphTx) -> bool:
            self._lock_graph_rows(tx, (), ((from_id, relation, to_id),))
            row = tx.fetchone(f"SELECT properties FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": from_id, "rel": relation, "tid": to_id})
            if not row:
                return False
            props = _as_dict(row[0])
            if props.get("pack_id") != owner_pack_id:
                return False
            result = tx.execute(f"DELETE FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": from_id, "rel": relation, "tid": to_id})
            if tx.rowcount(result) != 1:
                raise RuntimeError("graph edge delete rowcount mismatch")
            return True
        return self._run_mutation_tx(body)

    def update_edge(
        self, from_type: str, from_id: str, relation: str, to_type: str, to_id: str,
        properties: dict[str, Any] | None = None, *, expected_current_digest: str, owner_pack_id: str,
        return_receipt: bool = False,
    ) -> bool | EdgeWriteReceipt:
        self._require_write_available()
        validate_digest(expected_current_digest, edge=True)
        if not isinstance(owner_pack_id, str) or not owner_pack_id:
            raise ValueError("graph identity fields must be non-empty strings")
        props = normalize_edge_properties(from_id, relation, to_id, properties)
        if props.get("pack_id") != owner_pack_id:
            raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
        digest = canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)
        nodes, edges = self._table("graph_nodes"), self._table("graph_edges")
        def body(tx: GraphTx) -> bool | EdgeWriteReceipt:
            self._lock_graph_rows(tx, (from_id, to_id), ((from_id, relation, to_id),))
            ends = tx.fetchall(f"SELECT node_id, node_type FROM {nodes} WHERE node_id IN (:fid,:tid)", {"fid": from_id, "tid": to_id})
            types = {r[0]: r[1] for r in ends}
            if types.get(from_id) != from_type or types.get(to_id) != to_type:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {edges} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": from_id, "rel": relation, "tid": to_id})
            if not row:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            current = normalize_edge_properties(from_id, relation, to_id, _as_dict(row[2]))
            if current.get("pack_id") != owner_pack_id:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            current_digest = canonical_edge_digest(from_id, relation, to_id, row[0], row[1], current)
            if current_digest != expected_current_digest:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            result = tx.execute(f"UPDATE {edges} SET from_type=:ft, to_type=:tt, properties=:props WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"ft": from_type, "tt": to_type, "props": json.dumps(props, ensure_ascii=False), "fid": from_id, "rel": relation, "tid": to_id})
            if tx.rowcount(result) != 1:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            if return_receipt:
                return EdgeWriteReceipt("updated", from_id, relation, to_id, from_type, to_type, props, digest)
            return True
        return self._run_mutation_tx(body)

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Not supported by either SQL backend — returns [] with a warning."""
        self._require_available()
        logger.warning("run_cypher() is not supported in %s mode; returning [].", self._dialect.name)
        return []

    def _fetch_node_props_by_id(self, node_id: str) -> dict[str, Any] | None:
        sql = (
            f"SELECT properties, space_id FROM {self._table('graph_nodes')}"
            " WHERE node_id=:nid LIMIT 1"
        )
        row = self._fetch_one(sql, {"nid": node_id})
        return _merge_space(_as_dict(row[0]), row[1]) if row else None

    def _fetch_edges_for_node(
        self,
        node_id: str,
        cap: int,
        out: bool,
        pack_set: set[str] | None = None,
        include_unpackaged: bool = False,
        space_set: set[str] | None = None,
    ) -> list[tuple[str, str, str, Any]]:
        """Default per-node candidate-edge fetch (one query, LIMIT cap).
        Column order is always (other_type, other_id, relation, properties)
        regardless of direction, so callers never branch on direction to
        read a row.

        When ``pack_set``/``space_set`` are given, their filters
        (``_pack_where`` / ``space_id IN (...)``) are pushed into this
        query's WHERE clause — joined against ``graph_nodes`` for the
        "other" endpoint — so ``LIMIT :cap`` applies AFTER filtering, not
        before (issue #62, and issue #52 for the space leg). Unlike
        ``pack_id`` (a JSON property), ``space_id`` is a real column, so no
        JSON extraction helper is needed — a plain ``IN`` clause suffices."""
        edges = self._table("graph_edges")
        anchor_col = "from_id" if out else "to_id"
        other_type_col = "to_type" if out else "from_type"
        other_id_col = "to_id" if out else "from_id"

        if pack_set is None and space_set is None:
            sql = (
                f"SELECT {other_type_col}, {other_id_col}, relation, properties"
                f" FROM {edges} WHERE {anchor_col}=:nid LIMIT :cap"
            )
            return self._fetch_all(sql, {"nid": node_id, "cap": cap})

        nodes = self._table("graph_nodes")
        where_clauses: list[str] = []
        params: dict[str, Any] = {"nid": node_id, "cap": cap}
        if pack_set is not None:
            pack_where, pack_params = self._pack_where(
                "n.properties", "e.properties", pack_set, include_unpackaged, "fe"
            )
            where_clauses.append(pack_where)
            params.update(pack_params)
        if space_set is not None:
            placeholders, space_params = self._in_placeholders(sorted(space_set), "fesp")
            where_clauses.append(f"n.space_id IN ({placeholders})")
            params.update(space_params)
        extra_where = " AND ".join(where_clauses)
        sql = (
            f"SELECT e.{other_type_col}, e.{other_id_col}, e.relation, e.properties"
            f" FROM {edges} e"
            f" JOIN {nodes} n ON n.node_type=e.{other_type_col} AND n.node_id=e.{other_id_col}"
            f" WHERE e.{anchor_col}=:nid AND {extra_where}"
            f" LIMIT :cap"
        )
        return self._fetch_all(sql, params)

    def _prefetch_frontier(
        self,
        frontier_ids: list[str],
        cap: int,
        out: bool,
        pack_set: set[str] | None = None,
        include_unpackaged: bool = False,
        space_set: set[str] | None = None,
    ) -> dict[str, list[tuple[str, str, str, Any]]]:
        """HOOK — default per-row prefetch (one query per frontier node,
        reproducing LocalGraphStore's historical behavior). PgGraphStore's
        adopter overrides this with a single unnest+LATERAL batch query (see
        module docstring)."""
        return {
            fid: self._fetch_edges_for_node(fid, cap, out, pack_set, include_unpackaged, space_set)
            for fid in frontier_ids
        }

    def _batch_node_props(
        self, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """HOOK — default per-pair prop fetch. PgGraphStore's adopter
        overrides this with a single unnest+JOIN batch query."""
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for node_type, node_id in pairs:
            props = self.get_node(node_type, node_id)
            if props:
                result[(node_type, node_id)] = props
        return result

    def _expand(
        self,
        direction: str,
        batch: dict[str, list],
        current_id: str,
        current_depth: int,
        remaining: int,
        visited: set[str],
        results: list[dict[str, Any]],
        next_level: list[tuple[str, int]],
        limit: int,
        pack_set: set[str] | None,
        include_unpackaged: bool,
        props_cache: dict[tuple[str, str], dict[str, Any]],
        space_set: set[str] | None = None,
    ) -> None:
        """Consume one node's prefetched candidate edges for one direction."""
        is_out = direction == "out"
        for other_type, other_id, relation, edge_props_raw in batch.get(current_id, [])[:remaining]:
            if len(results) >= limit:
                break
            if other_id in visited:
                continue
            other_props = props_cache.get((other_type, other_id))
            if not other_props:
                continue
            if space_set is not None:
                # Redundant for the common case: SQL already filtered on the
                # real `space_id` column ahead of LIMIT (see
                # _fetch_edges_for_node). Kept as defense-in-depth for the one
                # divergent case _merge_space documents — an explicit
                # caller-supplied props["space"] wins over the column — so a
                # node whose properties JSON disagrees with its column is
                # still caught here rather than leaking cross-space.
                if not _space_passes(other_props, space_set):
                    continue
            if pack_set is not None:
                # Provably redundant for SCALAR pack_id values only: SQL
                # already applied this same policy (via _pack_where /
                # json_truthy_text) before LIMIT ran, so for a string/
                # number/boolean/null pack_id this can never reject a row
                # that reached here. It stays real, load-bearing defense
                # for a non-scalar pack_id (a JSON object/array) — SQL's
                # json_truthy_text falls through to a raw JSON-serialized
                # ELSE for those (undefined relative to Python, see its
                # docstring), so a composite pack_id can slip past the SQL
                # filter and only get caught here. Do not delete this
                # thinking it is dead code.
                other_pass = _node_passes(other_props, pack_set, include_unpackaged)
                if not other_pass:
                    continue
                edge_props = _as_dict(edge_props_raw)
                # The `True` here says "the anchor side already passed". That
                # holds for the anchor this call was given, but NOT for a
                # same-node_id row in another pack: the edge fetch matches on
                # from_id/to_id identify one global edge key, so the fetched
                # endpoint row is the same graph node selected by the edge.
                # Confidentiality is preserved by the fetch's own JOIN, which
                # pack-filters the OTHER endpoint in SQL -- not by
                # _edge_passes. See _pack_where's docstring.
                from_pass, to_pass = (True, other_pass) if is_out else (other_pass, True)
                if not _edge_passes(edge_props, from_pass, to_pass, pack_set):
                    continue
            visited.add(other_id)
            results.append({
                "properties": other_props,
                "labels": [other_type],
                "relation_type": relation,
                "relationship_types": [relation],
                "depth": current_depth + 1,
                # Canonical endpoints of the edge just traversed. Both ids are
                # already in hand here (no extra query), and they are the only
                # place the *direction* survives: find_neighbors is usually
                # called with direction="both", and the caller's anchor is not
                # the edge source once depth > 1.
                "from_id": current_id if is_out else other_id,
                "to_id": other_id if is_out else current_id,
            })
            next_level.append((other_id, current_depth + 1))

    def find_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        spaces: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """BFS neighbour traversal, processed one depth-level at a time (see
        module docstring's "_prefetch_frontier / _batch_node_props HOOKS").

        ``spaces`` (issue #52): strict space-membership filter, pushed into
        the SQL WHERE clause ahead of LIMIT (``_fetch_edges_for_node``) —
        same "filter before limit, not after" discipline as ``pack_ids``
        (issue #62), and cheaper since ``space_id`` is a real column, not a
        JSON property. No "include unspaced" escape hatch, matching the
        BM25/vector legs' strict semantics."""
        self._require_available()
        # issue #147 §3.4(a): `pack_ids=[]` must NOT collapse into `pack_set
        # = None` ("no filter") the way `set(pack_ids) if pack_ids else
        # None` did -- that collapse is exactly what let a principal with
        # zero readable packs see the whole graph. `[]` now means "nothing
        # passes" and short-circuits before even the anchor lookup (the
        # store is never touched for a caller who can read nothing).
        pack_set: set[str] | None = None if pack_ids is None else set(pack_ids)
        if pack_set is not None and not pack_set:
            return []
        space_set: set[str] | None = set(spaces) if spaces else None

        if pack_set is not None or space_set is not None:
            anchor_props = self._fetch_node_props_by_id(node_id)
            if not _node_passes(anchor_props or {}, pack_set, include_unpackaged):
                return []
            if not _space_passes(anchor_props or {}, space_set):
                return []

        visited: set[str] = {node_id}
        results: list[dict[str, Any]] = []
        level: list[tuple[str, int]] = [(node_id, 0)]

        while level and len(results) < limit:
            expandable = [nid for nid, d in level if d < depth]

            out_batch: dict[str, list] = {}
            in_batch: dict[str, list] = {}
            if expandable:
                if direction in ("out", "both"):
                    out_batch = self._prefetch_frontier(
                        expandable, limit, out=True,
                        pack_set=pack_set, include_unpackaged=include_unpackaged,
                        space_set=space_set,
                    )
                if direction in ("in", "both"):
                    in_batch = self._prefetch_frontier(
                        expandable, limit, out=False,
                        pack_set=pack_set, include_unpackaged=include_unpackaged,
                        space_set=space_set,
                    )

            candidate_pairs: set[tuple[str, str]] = set()
            for rows in out_batch.values():
                candidate_pairs.update((c1, c2) for c1, c2, _rel, _props in rows)
            for rows in in_batch.values():
                candidate_pairs.update((c1, c2) for c1, c2, _rel, _props in rows)
            props_cache = self._batch_node_props(candidate_pairs)

            next_level: list[tuple[str, int]] = []

            for current_id, current_depth in level:
                if current_depth >= depth:
                    continue

                if direction in ("out", "both"):
                    remaining = limit - len(results)
                    if remaining > 0:
                        self._expand(
                            "out", out_batch, current_id, current_depth, remaining,
                            visited, results, next_level, limit, pack_set,
                            include_unpackaged, props_cache, space_set,
                        )

                if direction in ("in", "both"):
                    remaining = limit - len(results)
                    if remaining > 0:
                        self._expand(
                            "in", in_batch, current_id, current_depth, remaining,
                            visited, results, next_level, limit, pack_set,
                            include_unpackaged, props_cache, space_set,
                        )

            level = next_level

        return results[:limit]

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        """BFS shortest path between two nodes (out-edges only, B1 contract:
        ``max_depth`` is a hop bound)."""
        self._require_available()
        table = self._table("graph_edges")
        visited: set[str] = {from_id}
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_id, [])])

        while queue:
            current_id, path = queue.popleft()
            if len(path) >= max_depth:
                continue

            sql = f"SELECT to_type, to_id, relation FROM {table} WHERE from_id=:fid"
            rows = self._fetch_all(sql, {"fid": current_id})
            for to_type, nid, rel in rows:
                node = self.get_node(to_type, nid) or {"id": nid}
                new_path = path + [{"node": node, "relation": rel}]

                if nid == to_id:
                    return new_path

                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, new_path))

        return []

    def count_nodes(self, node_type: str | None = None) -> int:
        self._require_available()
        table = self._table("graph_nodes")
        if node_type:
            row = self._fetch_one(f"SELECT COUNT(*) FROM {table} WHERE node_type=:nt", {"nt": node_type})  # noqa: S608
        else:
            row = self._fetch_one(f"SELECT COUNT(*) FROM {table}", {})  # noqa: S608
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Extended operations
    # ------------------------------------------------------------------

    def list_pack_ids(self) -> set[str]:
        """See GraphStore.list_pack_ids. Uses ``json_truthy_text`` rather
        than the bare extraction ``list_packs`` groups by, so a row whose
        pack_id is ``""``/``0``/``false`` is reported as unattributed here
        exactly as the Python and scoped-SQL predicates treat it -- reusing
        ``list_packs`` would have surfaced those as packs named ``"0"`` and
        made the startup guard refuse over rows no read can reach."""
        self._require_available()
        pid = self._dialect.json_truthy_text("properties", "pack_id")
        out: set[str] = set()
        # Nodes AND edges. An edge can carry a pack_id that appears on no
        # node, and the migration's own registry enumeration unions the two
        # for exactly that reason -- so leaving edges out here would let a
        # deployment start with that pack unregistered, after which scoped
        # traversal and export drop the edge because the caller's scope,
        # derived from the registry, cannot contain its pack.
        for table in ("graph_nodes", "graph_edges"):
            try:
                rows = self._fetch_all(
                    f"SELECT DISTINCT {pid} FROM {self._table(table)} "  # noqa: S608
                    f"WHERE {pid} IS NOT NULL",
                    {},
                )
            except Exception as exc:
                # SQLite's JSON expression raises on a single malformed
                # properties value. Enumeration is an inspection guard, so
                # retain valid owners while ignoring only that malformed row;
                # unrelated connection or schema failures must still surface.
                if "malformed" not in str(exc).lower():
                    raise
                rows = []
                for (raw,) in self._fetch_all(
                    f"SELECT properties FROM {self._table(table)}",  # noqa: S608
                    {},
                ):
                    try:
                        props = parse_properties_object(raw)
                    except ValueError:
                        continue
                    value = props.get("pack_id")
                    if value:
                        rows.append((value,))
            out |= {str(r[0]) for r in rows if r[0]}
        return out

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        """pack_id is unified to ``str`` on BOTH backends (Stage 6b Deliverable
        2 — see module docstring's "PACK_ID TYPE UNIFICATION")."""
        self._require_available()
        table = self._table("graph_nodes")
        pid = self._dialect.json_get("properties", "pack_id")
        title = self._dialect.json_get("properties", "title")
        src_title = self._dialect.json_get("properties", "source_package_title")
        desc = self._dialect.json_get("properties", "description")
        sql = f"""
            SELECT
                {pid} AS pack_id,
                COUNT(*) AS node_count,
                COALESCE(
                    -- ({pid}) parenthesized: PG's `||` binds tighter than `->>`,
                    -- so an unparenthesized `'dataset:' || properties->>'pack_id'`
                    -- parses as `('dataset:' || properties) ->> 'pack_id'` and
                    -- throws on the raw jsonb concat. SQLite's json_extract(...)
                    -- is a function call, so the extra parens are a no-op there.
                    MAX(CASE WHEN node_id = 'dataset:' || ({pid}) THEN {title} END),
                    MAX({src_title}),
                    ''
                ) AS sample_title,
                COALESCE(
                    MAX(CASE WHEN node_id = 'dataset:' || ({pid}) THEN {desc} END),
                    ''
                ) AS sample_description
            FROM {table}
            WHERE {pid} IS NOT NULL
            GROUP BY {pid}
            HAVING COUNT(*) >= :min_nodes
            ORDER BY COUNT(*) DESC
        """
        rows = self._fetch_all(sql, {"min_nodes": min_nodes})
        return [
            {
                "pack_id": str(pack_id),
                "node_count": node_count,
                "sample_title": sample_title or "",
                "sample_description": sample_description or "",
            }
            for pack_id, node_count, sample_title, sample_description in rows
        ]

    @staticmethod
    def _in_placeholders(values: list[str], prefix: str) -> tuple[str, dict[str, str]]:
        """Manually expand an ``IN (...)`` list into numbered named
        placeholders — works identically for both dialects (no need for
        SQLAlchemy's ``bindparam(expanding=True)`` on the PG side)."""
        names = [f"{prefix}{i}" for i in range(len(values))]
        return ", ".join(f":{n}" for n in names), dict(zip(names, values, strict=True))

    def _pack_where(
        self,
        node_col: str,
        edge_col: str,
        pack_set: set[str],
        include_unpackaged: bool,
        prefix: str,
    ) -> tuple[str, dict[str, str]]:
        """SINGLE SOURCE for translating the shared pack-filter policy
        (``opencrab/stores/_graph_common.py``'s ``_node_passes``/
        ``_edge_passes``) into SQL, so it can be pushed into a WHERE clause
        ahead of ``LIMIT`` instead of applied in Python after truncation
        (issue #62: a hub whose first ``limit`` edges are all out-of-pack
        could starve every in-pack neighbour before the Python filter ever
        saw them). Both ``_fetch_edges_for_node`` (below) and
        ``PGGraphStore._batch_frontier_edges`` call this one method, so a
        future change to the policy cannot silently diverge between them.

        Written for a candidate edge whose "current" endpoint has already
        passed ``_node_passes``, which is what lets this reduce to two
        independent clauses instead of a full min/max reproduction of
        ``_edge_passes`` (``src_passes`` is then always True).

        The anchor is resolved by ``_fetch_node_props_by_id`` and the edge
        fetch uses the global endpoint ids. Both queries join the endpoint
        node when a pack or space filter is active, so the SQL predicate is
        applied before the traversal limit. Keep that JOIN as the
        confidentiality boundary; the Python predicate remains defense in
        depth for malformed composite property values.

        Uses ``json_truthy_text`` (not the bare ``json_get`` extraction) for
        both sides: a raw JSON extraction is non-NULL for ``""``/``0``/
        ``false``, which Python's ``_node_pack_id`` treats as "no pack_id"
        (falsy). Without this, those values would be wrongly excluded here
        — before ``LIMIT`` even runs — instead of being governed by
        ``include_unpackaged`` like ``_node_passes`` does.

        ARRAY BIND, NOT PER-VALUE (issue #147 §3.4(c), mandatory not
        optional): ``_graph_expand``/``ImpactEngine`` now call
        ``find_neighbors(pack_ids=sorted(readable_scope))`` with the
        caller's FULL readable-pack scope -- every non-private pack in the
        deployment, not a small hand-typed filter -- on every BFS frontier
        query this method's callers (``_fetch_edges_for_node``,
        ``pg_graph_store.py::_batch_frontier_edges``) issue. The old
        ``_in_placeholders``-per-value expansion would blow past SQLite's
        bind-variable cap at that scale ("too many SQL variables"), and the
        callers that could hit it (``impact.py::analyse``'s bare
        ``except Exception: logger.debug``, ``query.py::_graph_expand``'s
        anchor-loop ``try``) SWALLOW that exception -- so a scope over the
        limit would not error, it would silently return zero neighbours.
        ``self._dialect.in_string_array`` (``_sql_dialect.py``) binds the
        whole pack set as ONE array parameter instead, reused for both the
        node and edge membership tests below (same value, same bind name,
        referenced twice -- valid with named params on both dialects).
        """
        bind_name = f"{prefix}_packs"
        node_pid = self._dialect.json_truthy_text(node_col, "pack_id")
        edge_pid = self._dialect.json_truthy_text(edge_col, "pack_id")
        membership_frag, transform = self._dialect.in_string_array(node_pid, f":{bind_name}")
        node_cond = membership_frag
        if include_unpackaged:
            node_cond = f"({node_cond} OR {node_pid} IS NULL)"
        edge_membership_frag, _ = self._dialect.in_string_array(edge_pid, f":{bind_name}")
        edge_cond = f"({edge_pid} IS NULL OR {edge_membership_frag})"
        params = {bind_name: transform(sorted(pack_set))}
        return f"{node_cond} AND {edge_cond}", params

    def _scoped_node_where(
        self, col: str, bind_name: str
    ) -> tuple[str, Callable[[list[str]], Any]]:
        """SINGLE SOURCE for the new pack_id-ONLY, index-friendly scope
        predicate the ``*_scoped`` methods below share (issue #147 §3.4(b)) --
        deliberately NOT ``_pack_where`` (the ``find_neighbors``/BFS
        predicate above). Both look at ``pack_id`` alone, but that one
        implements the 3-rule edge policy and supports
        ``include_unpackaged``; this one has no unpackaged escape hatch at
        all, because it backs AUTHORIZATION reads and data outside every
        pack is outside every read scope (#143 invariant 5). Neither of them
        touches ``source``/``source_id`` -- that is ``_export_nodes_where``,
        the pack-EXPORT predicate, whose 3-way OR is unusable for an access
        decision because those two properties are caller-written.

        TYPE PARITY, stated precisely (it is not uniform, and an earlier
        draft of this docstring overclaimed it): for a pack_id that is a
        JSON string -- the only form ``pack_create`` and the ``packs``
        registry produce -- SQL and Python agree exactly. For non-string
        JSON values they do not, and they disagree in OPPOSITE directions
        by layer: SQLite's ``json_extract`` preserves the native type, so a
        JSON number ``1`` never equals the bound TEXT ``'1'`` and the row is
        EXCLUDED here, while Python's ``_node_pack_id`` does ``str(1)`` and
        would INCLUDE it (so ``find_neighbors``/BM25 match it). Neither
        direction crosses a user boundary -- a match still requires the id
        to be in the caller's own scope -- so what remains is a recall
        difference between backends, not a leak.

        Two-clause AND, both clauses load-bearing for different reasons:
          1. ``json_get(col,'pack_id') IN <array bind>`` -- uses the BARE
             extraction, not ``json_truthy_text``, because
             ``GRAPH_STORE_SCHEMA``'s only pack index (``idx_nodes_pack``)
             is built on ``json_get`` (a plain function-call expression);
             a CASE-expression predicate (what ``json_truthy_text`` is)
             cannot use that index on either dialect. Authorization is now
             the primary read path, so silently losing the one pack index
             here would be a real regression, not a style choice.
          2. ``json_truthy_text(col,'pack_id') IS NOT NULL`` -- closes a
             real fail-open gap the bare ``json_get`` clause alone leaves:
             on PostgreSQL, a JSON number ``0``/boolean ``false`` extracts
             via ``->>`` as the TEXT ``'0'``/``'false'``, which is non-NULL
             and could equal a real pack_id string in the caller's scope
             (a pack literally named ``"0"``) -- so a row that Python's
             ``_node_pack_id``/``in_pack_scope`` treat as "has no pack_id"
             (falsy) could otherwise satisfy clause 1 and leak into an
             authorized caller's results. This second clause reproduces
             the same falsy-exclusion ``json_truthy_text`` already applies
             elsewhere (see its own docstring), closing that gap. Cost is
             bounded to rows already narrowed by the indexed clause above.

        Returns ``(where_fragment, value_transform)`` -- same contract as
        ``SqlDialect.in_string_array``: the caller must still apply
        ``value_transform`` to its ``list[str]`` and bind the result under
        ``bind_name`` (this is a callable, not a pre-applied value, exactly
        like ``_pack_where``'s ``transform`` -- kept as a callable here too
        so ``export_edges_scoped`` can call this once per endpoint alias --
        twice, not three times: the edge's own clause is assembled
        separately because it must also admit a NULL pack_id, which this
        two-clause node form does not. All the clauses still share ONE
        array bind, and the transform is applied to it exactly once).
        """
        membership_expr = self._dialect.json_get(col, "pack_id")
        frag, transform = self._dialect.in_string_array(membership_expr, f":{bind_name}")
        truthy = self._dialect.json_truthy_text(col, "pack_id")
        return f"{frag} AND {truthy} IS NOT NULL", transform

    def export_nodes_scoped(
        self, pack_ids: list[str], limit: int, space: str | None = None
    ) -> list[dict[str, Any]]:
        """Authorization-scoped node export (issue #147 §3.4(b)) -- the
        ``export_nodes``/``count_exported_nodes`` counterpart for a READ
        path caller instead of a pack-export/fork tool. Differs from
        ``export_nodes`` in exactly the ways that matter for authorization:
        pack_id ONLY (never ``source``/``source_id`` -- ``export_nodes``'s
        3-way OR is deliberately loose for its bulk-export use case, but
        loose enough to let a node claim membership in a pack it was never
        actually written into, which is a real gap for a permission check),
        and ``pack_ids`` is REQUIRED with no default -- there is no "export
        everything" mode here, because "everything" is exactly what an
        authorization-scoped read must never mean. ``export_nodes`` itself
        is left untouched (see its own docstring / issue #147 §8's
        "intentionally not fixed" list) -- this is a new, separate method,
        not a signature change to that one.

        Empty ``pack_ids`` -> ``[]`` WITHOUT querying (a principal who can
        read nothing is not a reason to touch the store), matching every
        other ``*_scoped`` method's contract in this file. ``limit <= 0``
        -> ``[]``, same rule ``export_nodes`` already applies (issue #120).
        """
        self._require_available()
        if not pack_ids or limit <= 0:
            return []
        table = self._table("graph_nodes")
        where_sql, transform = self._scoped_node_where("properties", "sc_packs")
        where_parts = [where_sql]
        params: dict[str, Any] = {"sc_packs": transform(sorted(set(pack_ids)))}
        if space:
            where_parts.append("space_id = :space")
            params["space"] = space
        params["lim"] = limit
        sql = (
            f"SELECT node_type, space_id, properties FROM {table} "
            f"WHERE {' AND '.join(where_parts)} LIMIT :lim"
        )
        rows = self._fetch_all(sql, params)
        return [
            {
                "props": _merge_space(_as_dict(properties), space_id),
                "labels": [node_type],
                "node_type": node_type,
            }
            for node_type, space_id, properties in rows
        ]

    def count_exported_nodes_scoped(
        self, pack_ids: list[str], space: str | None = None
    ) -> int:
        """Exact ``COUNT(*)`` counterpart to ``export_nodes_scoped``, same
        predicate, no LIMIT (issue #54's "total must not be capped by a
        display limit" reasoning, applied to the scoped predicate). Empty
        ``pack_ids`` -> ``0`` without querying."""
        self._require_available()
        if not pack_ids:
            return 0
        table = self._table("graph_nodes")
        where_sql, transform = self._scoped_node_where("properties", "sc_packs")
        where_parts = [where_sql]
        params: dict[str, Any] = {"sc_packs": transform(sorted(set(pack_ids)))}
        if space:
            where_parts.append("space_id = :space")
            params["space"] = space
        row = self._fetch_one(
            f"SELECT COUNT(*) FROM {table} WHERE {' AND '.join(where_parts)}", params  # noqa: S608
        )
        return int(row[0]) if row else 0

    def export_edges_scoped(self, pack_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """Authorization-scoped edge export (issue #147 §3.4(b)) -- AND
        rule, the exact OPPOSITE of ``export_edges``'s 5-way OR:
        ``a.pack_id in scope AND b.pack_id in scope AND (e.pack_id IS NULL
        OR e.pack_id in scope)``. The response embeds BOTH endpoints' full
        ``properties`` (see this method's return shape below), so an OR
        across endpoints (like ``export_edges`` uses) would expose a node
        outside the caller's scope whenever the OTHER endpoint happened to
        be in-scope -- the exact class of leak this predicate exists to
        close. This is the same rule ``_graph_common._edge_passes`` already
        enforces for ``find_neighbors``' 3-rule policy; this SQL form is
        just that same rule pushed ahead of LIMIT for a bulk query instead
        of applied node-by-node during BFS.

        Built from ``_scoped_node_where`` (the same helper
        ``export_nodes_scoped``/``get_node_by_id_scoped`` use, not a new
        predicate builder) called once per endpoint alias plus once more
        for the edge's own pack_id, all three reusing ONE array bind
        (``sc_packs``, same value bound once, referenced three times --
        valid with named params, mirrors ``_pack_where``'s same trick).

        Empty ``pack_ids`` -> ``[]`` without querying. ``limit <= 0`` ->
        ``[]``, matching ``export_edges``' contract."""
        self._require_available()
        if not pack_ids or limit <= 0:
            return []
        nodes = self._table("graph_nodes")
        edges = self._table("graph_edges")
        a_where, transform = self._scoped_node_where("a.properties", "sc_packs")
        b_where, _ = self._scoped_node_where("b.properties", "sc_packs")
        edge_truthy = self._dialect.json_truthy_text("e.properties", "pack_id")
        e_membership, _ = self._dialect.in_string_array(
            self._dialect.json_get("e.properties", "pack_id"), ":sc_packs"
        )
        edge_cond = f"({edge_truthy} IS NULL OR ({e_membership} AND {edge_truthy} IS NOT NULL))"
        sql = f"""
            SELECT
                a.node_type AS from_type, a.properties AS source_props,
                b.node_type AS to_type,   b.properties AS target_props,
                e.properties AS rel_props, e.relation,
                a.space_id AS from_space_id, b.space_id AS to_space_id
            FROM {edges} e
            JOIN {nodes} a ON e.from_type=a.node_type AND e.from_id=a.node_id
            JOIN {nodes} b ON e.to_type=b.node_type   AND e.to_id=b.node_id
            WHERE {a_where} AND {b_where} AND {edge_cond}
            LIMIT :lim
        """
        params = {"sc_packs": transform(sorted(set(pack_ids))), "lim": limit}
        rows = self._fetch_all(sql, params)
        return [
            {
                "source_props": _merge_space(_as_dict(r[1]), r[6]), "source_labels": [r[0]],
                "target_props": _merge_space(_as_dict(r[3]), r[7]), "target_labels": [r[2]],
                "rel_props": _as_dict(r[4]), "relation": r[5],
            }
            for r in rows
        ]

    def get_node_by_id_scoped(self, node_id: str, pack_ids: list[str]) -> dict[str, Any] | None:
        """Type-agnostic, SCOPE-FILTERED node lookup (issue #147 §1.2-6b,
        §3.4(b)) -- replaces a Python post-filter over ``get_node_by_id``,
        which cannot be made safe by filtering after the fact:
        ``get_node_by_id``'s ``WHERE node_id=:nid LIMIT 1`` is unscoped. The
        pack predicate must therefore be pushed ahead of ``LIMIT 1`` so the
        selected row is already known to be readable. Global node identity
        means one id has one row, so no in-scope homonym remains to resolve.

        Empty ``pack_ids`` -> ``None`` without querying (nothing is in
        scope, so there is nothing to find)."""
        self._require_available()
        if not pack_ids:
            return None
        where_sql, transform = self._scoped_node_where("properties", "sc_packs")
        sql = (
            f"SELECT node_type, properties, space_id FROM {self._table('graph_nodes')}"
            f" WHERE node_id=:nid AND {where_sql} LIMIT 1"
        )
        params = {"nid": node_id, "sc_packs": transform(sorted(set(pack_ids)))}
        row = self._fetch_one(sql, params)
        if not row:
            return None
        props = dict(_merge_space(_as_dict(row[1]), row[2]))
        props["node_type"] = row[0]
        return props

    def find_by_relations_scoped(
        self,
        node_id: str,
        relations: list[str],
        pack_ids: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Authorization-scoped ``find_by_relations`` (issue #147).

        Constrains the ANCHOR, the OTHER endpoint and the EDGE ITSELF,
        all before ``LIMIT``. Post-filtering the destination properties --
        which is what the caller used to do -- is not enough on three
        counts, each of which lets something out of scope through:

        - The edge's own ``pack_id`` is never returned by
          ``find_by_relations``, so a relationship belonging to a pack the
          caller cannot read still contributes its ``relation_type`` (and,
          for lever simulation, a prediction derived from it).
        - The anchor is matched on ``node_id`` alone, so a same-id node in
          another pack supplies its edges.
        - Filtering after ``LIMIT`` starves in-scope rows behind
          out-of-scope ones.

        Edge rule matches ``export_edges_scoped``: both endpoints in scope,
        and the edge's own pack_id either absent or in scope. Empty
        ``pack_ids`` or ``relations`` returns ``[]`` without querying.
        """
        self._require_available()
        if not relations or not pack_ids or limit <= 0:
            return []
        edges = self._table("graph_edges")
        nodes = self._table("graph_nodes")
        placeholders, rel_params = self._in_placeholders(relations, "rel")
        anchor_where, transform = self._scoped_node_where("anchor.properties", "sc_packs")
        other_where, _ = self._scoped_node_where("other.properties", "sc_packs")
        edge_truthy = self._dialect.json_truthy_text("e.properties", "pack_id")
        edge_membership, _ = self._dialect.in_string_array(
            self._dialect.json_get("e.properties", "pack_id"), ":sc_packs"
        )
        edge_cond = f"({edge_truthy} IS NULL OR ({edge_membership} AND {edge_truthy} IS NOT NULL))"
        results: list[dict[str, Any]] = []

        def leg(anchor_col: str, anchor_type: str, other_col: str, other_type: str, lim: int):
            sql = (
                f"SELECT other.node_type, other.node_id, e.relation FROM {edges} e"
                f" JOIN {nodes} anchor ON anchor.node_type=e.{anchor_type} AND anchor.node_id=e.{anchor_col}"
                f" JOIN {nodes} other ON other.node_type=e.{other_type} AND other.node_id=e.{other_col}"
                f" WHERE e.{anchor_col}=:nid AND e.relation IN ({placeholders})"
                f" AND {anchor_where} AND {other_where} AND {edge_cond} LIMIT :lim"
            )
            params = {
                "nid": node_id,
                "lim": lim,
                "sc_packs": transform(sorted(set(pack_ids))),
                **rel_params,
            }
            for other_ntype, other_nid, relation in self._fetch_all(sql, params):
                props = self.get_node(other_ntype, other_nid)
                if props:
                    results.append(
                        {
                            "properties": props,
                            "labels": [other_ntype],
                            "node_type": other_ntype,
                            "relation_type": relation,
                        }
                    )

        if direction in ("out", "both"):
            leg("from_id", "from_type", "to_id", "to_type", limit)
        if direction in ("in", "both"):
            remaining = limit - len(results)
            if remaining > 0:
                leg("to_id", "to_type", "from_id", "from_type", remaining)
        return results

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._require_available()
        if not relations:
            return []
        table = self._table("graph_edges")
        placeholders, rel_params = self._in_placeholders(relations, "rel")
        results: list[dict[str, Any]] = []

        if direction in ("out", "both"):
            sql = (
                f"SELECT to_type, to_id, relation FROM {table}"
                f" WHERE from_id=:nid AND relation IN ({placeholders}) LIMIT :lim"
            )
            rows = self._fetch_all(sql, {"nid": node_id, "lim": limit, **rel_params})
            for to_type, to_id, relation in rows:
                props = self.get_node(to_type, to_id)
                if props:
                    results.append({
                        "properties": props,
                        "labels": [to_type],
                        "node_type": to_type,
                        "relation_type": relation,
                    })

        if direction in ("in", "both"):
            remaining = limit - len(results)
            if remaining > 0:
                sql = (
                    f"SELECT from_type, from_id, relation FROM {table}"
                    f" WHERE to_id=:nid AND relation IN ({placeholders}) LIMIT :lim"
                )
                rows = self._fetch_all(sql, {"nid": node_id, "lim": remaining, **rel_params})
                for from_type, from_id, relation in rows:
                    props = self.get_node(from_type, from_id)
                    if props:
                        results.append({
                            "properties": props,
                            "labels": [from_type],
                            "node_type": from_type,
                            "relation_type": relation,
                        })

        return results

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        sql = (
            f"SELECT node_type, properties, space_id FROM {self._table('graph_nodes')}"
            " WHERE node_id=:nid LIMIT 1"
        )
        row = self._fetch_one(sql, {"nid": node_id})
        if not row:
            return None
        props = dict(_merge_space(_as_dict(row[1]), row[2]))
        props["node_type"] = row[0]
        return props

    def get_nodes_by_id(self, node_id: str) -> list[dict[str, Any]]:
        """Plural counterpart to ``get_node_by_id`` -- returns EVERY row for
        ``node_id``, not just whichever one ``LIMIT 1`` happens to pick.

        Global node identity makes this normally a one-row result, but the
        plural API remains useful for inspecting legacy or externally
        corrupted data. ``ORDER BY node_type`` makes any such diagnostic
        result deterministic; empty list, not ``None``, when nothing
        matches."""
        self._require_available()
        sql = (
            f"SELECT node_type, properties, space_id FROM {self._table('graph_nodes')}"
            " WHERE node_id=:nid ORDER BY node_type"
        )
        rows = self._fetch_all(sql, {"nid": node_id})
        results = []
        for node_type, properties, space_id in rows:
            props = dict(_merge_space(_as_dict(properties), space_id))
            props["node_type"] = node_type
            results.append(props)
        return results

    def _export_nodes_where(
        self, pack_id: str | None, space: str | None
    ) -> tuple[str, dict[str, Any]]:
        """Shared pack_id/space WHERE-clause builder for ``export_nodes`` and
        ``count_exported_nodes`` -- keeping the predicate in one place
        guarantees the COUNT variant can never silently drift from what
        ``export_nodes`` actually filters on (issue #54: that agreement is
        the whole point of ``count_exported_nodes`` existing)."""
        where_parts: list[str] = []
        params: dict[str, Any] = {}
        if pack_id:
            pid = self._dialect.json_get("properties", "pack_id")
            src = self._dialect.json_get("properties", "source")
            src_id = self._dialect.json_get("properties", "source_id")
            where_parts.append(f"({pid} = :pid OR {src} = :pid OR {src_id} = :pid)")
            params["pid"] = pack_id
        if space:
            where_parts.append("space_id = :space")
            params["space"] = space
        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        return where_sql, params

    def export_nodes(
        self,
        pack_id: str | None = None,
        limit: int = 500_000,
        space: str | None = None,
    ) -> list[dict[str, Any]]:
        """``space``, when given, is pushed into the WHERE clause ahead of
        LIMIT (issue #54: same "store truncates, caller Python-filters"
        pattern as #62's pack-filter pushdown for ``find_neighbors``). Unlike
        pack_id, space lives in its own ``space_id`` column, so this is a
        plain equality clause -- no JSON extraction needed. For an accurate
        match count that isn't capped by ``limit``, use
        ``count_exported_nodes`` instead of ``len(export_nodes(...))``.

        ``limit <= 0`` (issue #120): returns ``[]`` without querying. This
        matters more than it looks: SQLite treats a bound ``LIMIT -1`` as
        "no limit at all" (verified -- ``LIMIT ?`` with param ``-1`` returns
        every row), so without this guard a negative ``limit`` here would
        silently return the *entire* unbounded table instead of nothing.
        Kuzu's ``export_nodes`` applies the same ``limit <= 0`` guard so all
        backends agree on both ``limit=0`` and negative ``limit``."""
        self._require_available()
        if limit <= 0:
            return []
        table = self._table("graph_nodes")
        # space_id is selected so _merge_space can restore it into props: this
        # backend keeps space in its own column, but the protocol's export shape
        # carries it inside props (see _merge_space for the measured fallout).
        where_sql, params = self._export_nodes_where(pack_id, space)
        params = {**params, "lim": limit}
        sql = f"SELECT node_type, space_id, properties FROM {table}{where_sql} LIMIT :lim"
        rows = self._fetch_all(sql, params)
        return [
            {
                "props": _merge_space(_as_dict(properties), space_id),
                "labels": [node_type],
                "node_type": node_type,
            }
            for node_type, space_id, properties in rows
        ]

    def count_exported_nodes(
        self, pack_id: str | None = None, space: str | None = None
    ) -> int:
        """Real ``COUNT(*)`` with the exact same predicate ``export_nodes``
        filters on (via the shared ``_export_nodes_where``), unbounded by any
        LIMIT -- issue #54: ``total`` must reflect the true match count, not
        get truncated by a caller's display ``limit``."""
        self._require_available()
        table = self._table("graph_nodes")
        where_sql, params = self._export_nodes_where(pack_id, space)
        row = self._fetch_one(f"SELECT COUNT(*) FROM {table}{where_sql}", params)  # noqa: S608
        return int(row[0]) if row else 0

    def search_nodes(
        self,
        keyword: str,
        *,
        pack_ids: list[str],
        spaces: list[str] | None = None,
        limit: int = 10,
        fields: tuple[str, ...] = KEYWORD_SEARCH_FIELDS,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search of ``keyword`` across ``fields``
        of every node's JSON ``properties``, optionally restricted to
        ``spaces`` -- both pushed into the SQL WHERE clause ahead of
        ``LIMIT`` (issue #86, the same "store truncates first, caller
        filters after" class ``_export_nodes_where`` fixed for #54:
        ``HybridQuery.keyword_search`` used to fetch only the first
        50,000 rows via ``export_nodes`` and search only those in Python,
        silently missing ~80% of a 252k-row corpus with no error). Unlike
        ``export_nodes``, this ``LIMIT`` is the caller's actual desired
        result count, not an internal cap -- applying it here is correct
        once the keyword/space predicate is already in the WHERE clause,
        because every row that reaches LIMIT already matched.

        Case-insensitivity is done in SQL (``LOWER(...)``) rather than
        Python so it composes with pushdown; ``%``/``_``/``\\`` in
        ``keyword`` are escaped so a literal percent or underscore in the
        search term can't be misread as a SQL LIKE wildcard.

        ``limit <= 0`` short-circuits to ``[]`` without a query (issue
        #86 boundary check, same class as #120's ``.limit(0)`` Mongo
        surprise): SQLite treats a NEGATIVE ``LIMIT`` as "no limit at
        all" (unbounded scan) rather than "zero rows", and PostgreSQL
        raises ``LIMIT must not be negative`` for the same input --
        binding ``limit`` straight into ``LIMIT :lim`` would make this one
        shared method behave three different ways (SQLite: unbounded scan,
        Postgres: SQL error, ``0``: empty on both) depending on dialect and
        sign. Clamping here keeps ``limit<=0`` meaning exactly one thing
        ("caller wants nothing back") on every SQL backend. Checked AFTER
        ``_require_available()`` so an unavailable store still raises
        (matching every other guarded method's contract) rather than
        returning ``[]`` and masking the real problem.

        ``fields`` is validated against ``KEYWORD_SEARCH_FIELDS`` (issue
        #86 bot finding) -- each field name below is interpolated directly
        into a JSON path via ``self._dialect.json_get`` with NO escaping
        (unlike ``keyword``, which is a bound parameter), so an
        unvalidated ``fields`` value is a SQL injection vector. See
        ``_validate_search_fields`` (_graph_common.py) for the rejected
        payload and why unknown fields raise instead of being silently
        dropped. An empty ``fields`` tuple returns ``[]`` immediately --
        an empty WHERE-clause OR-group is invalid SQL (``WHERE ()``), and
        "search zero fields" has only one sane meaning: no field can ever
        match, so there is nothing to search for.

        ``pack_ids`` (issue #147 §3.4(b)/item 5, required -- no default):
        the same strict pack_id-ONLY predicate ``export_nodes_scoped``/
        ``get_node_by_id_scoped`` use (via ``_scoped_node_where`` --
        ``json_get(...) IN <array bind> AND json_truthy_text(...) IS NOT
        NULL``), pushed into the SAME WHERE clause as the keyword/space
        predicates, ahead of ``LIMIT`` -- this method's only caller
        (``HybridQuery.keyword_search``, the graph leg of the hybrid
        keyword search) is a read-path caller, so there is no "unfiltered"
        mode here the way ``export_nodes``' ``pack_id=None`` has one.
        Empty ``pack_ids`` -> ``[]`` without querying, same as every other
        ``*_scoped`` contract in this file."""
        self._require_available()
        if limit <= 0:
            return []
        if not fields:
            return []
        if not pack_ids:
            return []
        _validate_search_fields(fields)
        table = self._table("graph_nodes")
        kw = keyword.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_parts = [
            "(" + " OR ".join(
                f"LOWER({self._dialect.json_get('properties', f)}) LIKE :kw ESCAPE '\\'"
                for f in fields
            ) + ")"
        ]
        pack_where, transform = self._scoped_node_where("properties", "sc_packs")
        where_parts.append(pack_where)
        params: dict[str, Any] = {
            "kw": f"%{kw}%",
            "sc_packs": transform(sorted(set(pack_ids))),
        }
        if spaces:
            placeholders = ", ".join(f":space{i}" for i in range(len(spaces)))
            where_parts.append(f"space_id IN ({placeholders})")
            params.update({f"space{i}": s for i, s in enumerate(spaces)})
        params["lim"] = limit
        sql = (
            f"SELECT node_type, space_id, properties FROM {table} "
            f"WHERE {' AND '.join(where_parts)} LIMIT :lim"
        )
        rows = self._fetch_all(sql, params)  # noqa: S608
        return [
            {
                "props": _merge_space(_as_dict(properties), space_id),
                "labels": [node_type],
                "node_type": node_type,
            }
            for node_type, space_id, properties in rows
        ]

    def export_edges(
        self,
        pack_id: str | None = None,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        nodes = self._table("graph_nodes")
        edges = self._table("graph_edges")
        # a.space_id / b.space_id are selected for the same reason as in
        # export_nodes -- both endpoints' props must carry their space.
        base_select = f"""
            SELECT
                a.node_type AS from_type, a.properties AS source_props,
                b.node_type AS to_type,   b.properties AS target_props,
                e.properties AS rel_props, e.relation,
                a.space_id AS from_space_id, b.space_id AS to_space_id
            FROM {edges} e
            JOIN {nodes} a ON e.from_type=a.node_type AND e.from_id=a.node_id
            JOIN {nodes} b ON e.to_type=b.node_type   AND e.to_id=b.node_id
        """
        if pack_id:
            pid_a = self._dialect.json_get("a.properties", "pack_id")
            src_a = self._dialect.json_get("a.properties", "source")
            pid_b = self._dialect.json_get("b.properties", "pack_id")
            src_b = self._dialect.json_get("b.properties", "source")
            pid_e = self._dialect.json_get("e.properties", "pack_id")
            sql = base_select + f"""
                WHERE {pid_a} = :pid OR {src_a} = :pid
                   OR {pid_b} = :pid OR {src_b} = :pid
                   OR {pid_e} = :pid
                LIMIT :lim
            """
            rows = self._fetch_all(sql, {"pid": pack_id, "lim": limit})
        else:
            rows = self._fetch_all(base_select + " LIMIT :lim", {"lim": limit})
        return [
            {
                "source_props": _merge_space(_as_dict(r[1]), r[6]), "source_labels": [r[0]],
                "target_props": _merge_space(_as_dict(r[3]), r[7]), "target_labels": [r[2]],
                "rel_props": _as_dict(r[4]), "relation": r[5],
            }
            for r in rows
        ]

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]], *, return_receipt: bool = False) -> Any:
        self._require_write_available()
        return self._upsert_nodes_batch_impl(nodes, return_receipt=return_receipt)

    def upsert_edges_batch(self, edges: list[dict[str, Any]], *, return_receipt: bool = False) -> Any:
        self._require_write_available()
        return self._upsert_edges_batch_impl(edges, return_receipt=return_receipt)

    # Issue 80 batch implementations live below the compatibility bodies
    # above so downstream subclasses that introspect the historical method
    # order continue to work.  The public methods return before those bodies.
    def _upsert_nodes_batch_impl(self, nodes: list[dict[str, Any]], *, return_receipt: bool) -> Any:
        if not nodes:
            return () if return_receipt else 0
        prepared = []
        seen: set[str] = set()
        for item in nodes:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            try:
                nt, props, sid, digest = prepare_node(item["node_type"], item["node_id"], item.get("properties", {}), item.get("space_id"))
            except KeyError as exc:
                raise ValueError("malformed graph batch item") from exc
            nid = item["node_id"]
            if nid in seen:
                raise ValueError(f"duplicate global graph key in batch: node_id={nid}")
            seen.add(nid)
            prepared.append((nt, nid, props, sid, digest))
        table = self._table("graph_nodes")
        insert_sql = self._dialect.insert(table, ["node_type", "node_id", "space_id", "properties"], json_columns=["properties"]) + "\nON CONFLICT (node_id) DO NOTHING"
        def body(tx: GraphTx) -> Any:
            self._lock_graph_rows(tx, (item[1] for item in prepared))
            rows: dict[str, tuple] = {}
            for _nt, nid, _props, _sid, _digest in prepared:
                row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {table} WHERE node_id=:nid", {"nid": nid})
                if row:
                    rows[nid] = row
            for _nt, nid, _props, _sid, digest in prepared:
                if nid in rows:
                    row = rows[nid]
                    stored = _merge_space(_as_dict(row[2]), row[1])
                    if canonical_node_digest(row[0], row[1] or stored.get("space"), stored) != digest:
                        raise NodeIdentityConflict(f"node identity conflict: {nid}")
            receipts = []
            for nt, nid, props, sid, _digest in prepared:
                operation = "idempotent" if nid in rows else "created"
                if nid not in rows:
                    result = tx.execute(insert_sql, {"node_type": nt, "node_id": nid, "space_id": sid, "properties": json.dumps(props, ensure_ascii=False)})
                    if tx.rowcount(result) == 0:
                        operation = "idempotent"
                    row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {table} WHERE node_id=:nid", {"nid": nid})
                else:
                    row = rows[nid]
                if row is None:
                    raise RuntimeError("graph node insert did not produce a row")
                stored = _merge_space(_as_dict(row[2]), row[1])
                digest = canonical_node_digest(row[0], row[1] or stored.get("space"), stored)
                if return_receipt:
                    receipts.append(NodeWriteReceipt(operation, nid, row[0], row[1], stored, digest))
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_mutation_tx(body)

    def update_nodes_batch(self, nodes: list[dict[str, Any]], *, return_receipt: bool = False) -> Any:
        self._require_write_available()
        if not nodes:
            return () if return_receipt else 0
        prepared = []
        seen: set[str] = set()
        for item in nodes:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            nid = item.get("node_id")
            if not isinstance(nid, str) or nid in seen:
                raise ValueError(f"duplicate global graph key in batch: node_id={nid}")
            seen.add(nid)
            expected = item.get("expected_current_digest")
            validate_digest(expected)
            nt = item.get("new_type", item.get("node_type"))
            props = item.get("new_properties", item.get("properties"))
            if nt is None or props is None:
                raise ValueError("malformed graph batch item")
            nt, props, sid, digest = prepare_node(nt, nid, props, item.get("new_space_id", item.get("space_id")))
            prepared.append((nid, expected, nt, props, sid, digest))
        nodes_table, edges_table = self._table("graph_nodes"), self._table("graph_edges")
        def body(tx: GraphTx) -> Any:
            target_ids = [item[0] for item in prepared]
            self._lock_graph_rows(tx, target_ids)
            if target_ids:
                placeholders, id_params = self._in_placeholders(target_ids, "incident_")
                incident = tx.fetchall(
                    f"SELECT from_id, relation, to_id FROM {edges_table} "
                    f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
                    id_params,
                )
            else:
                incident = []
            self._lock_graph_rows(
                tx,
                (value for row in incident for value in (row[0], row[2])),
                ((row[0], row[1], row[2]) for row in incident),
            )
            for nid, expected, _nt, _props, _sid, _digest in prepared:
                row = tx.fetchone(f"SELECT node_type, space_id, properties FROM {nodes_table} WHERE node_id=:nid", {"nid": nid})
                if row is None:
                    raise NodeIdentityConflict(f"stale node update: {nid}")
                current = _merge_space(_as_dict(row[2]), row[1])
                if canonical_node_digest(row[0], row[1] or current.get("space"), current) != expected:
                    raise NodeIdentityConflict(f"stale node update: {nid}")
            receipts = []
            for nid, _expected, nt, props, sid, digest in prepared:
                result = tx.execute(f"UPDATE {nodes_table} SET node_type=:nt, space_id=:sid, properties=:props WHERE node_id=:nid", {"nt": nt, "sid": sid, "props": json.dumps(props, ensure_ascii=False), "nid": nid})
                if tx.rowcount(result) != 1:
                    raise NodeIdentityConflict(f"stale node update: {nid}")
                tx.execute(f"UPDATE {edges_table} SET from_type=:nt WHERE from_id=:nid", {"nt": nt, "nid": nid})
                tx.execute(f"UPDATE {edges_table} SET to_type=:nt WHERE to_id=:nid", {"nt": nt, "nid": nid})
                if return_receipt:
                    receipts.append(NodeWriteReceipt("updated", nid, nt, sid, props, digest))
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_mutation_tx(body)

    def _upsert_edges_batch_impl(self, edges: list[dict[str, Any]], *, return_receipt: bool) -> Any:
        if not edges:
            return () if return_receipt else 0
        prepared = []
        seen: set[tuple[str, str, str]] = set()
        for item in edges:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            try:
                ft, fid, rel, tt, tid = item["from_type"], item["from_id"], item["relation"], item["to_type"], item["to_id"]
            except KeyError as exc:
                raise ValueError("malformed graph batch item") from exc
            key = (fid, rel, tid)
            if key in seen:
                raise ValueError(f"duplicate global graph key in batch: edge_key={key}")
            seen.add(key)
            props = normalize_edge_properties(fid, rel, tid, item.get("properties"))
            prepared.append((ft, fid, rel, tt, tid, props, canonical_edge_digest(fid, rel, tid, ft, tt, props)))
        nodes_table, table = self._table("graph_nodes"), self._table("graph_edges")
        insert_sql = self._dialect.insert(table, ["from_type", "from_id", "relation", "to_type", "to_id", "properties"], json_columns=["properties"]) + "\nON CONFLICT (from_id, relation, to_id) DO NOTHING"
        def body(tx: GraphTx) -> Any:
            self._lock_graph_rows(
                tx,
                (value for item in prepared for value in (item[1], item[4])),
                ((item[1], item[2], item[4]) for item in prepared),
            )
            for ft, fid, rel, tt, tid, props, digest in prepared:
                types = {r[0]: r[1] for r in tx.fetchall(f"SELECT node_id, node_type FROM {nodes_table} WHERE node_id IN (:fid,:tid)", {"fid": fid, "tid": tid})}
                if fid not in types or tid not in types:
                    raise ValueError(f"edge endpoint does not exist: {fid if fid not in types else tid}")
                if types.get(fid) != ft or types.get(tid) != tt:
                    mismatch = fid if types.get(fid) != ft else tid
                    raise ValueError(f"edge endpoint type mismatch: {mismatch}")
                row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {table} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": fid, "rel": rel, "tid": tid})
                if row:
                    stored = normalize_edge_properties(fid, rel, tid, _as_dict(row[2]))
                    if canonical_edge_digest(fid, rel, tid, row[0], row[1], stored) != digest:
                        raise EdgeIdentityConflict(f"edge identity conflict: ({fid}, {rel}, {tid})")
            receipts = []
            for ft, fid, rel, tt, tid, props, _digest in prepared:
                row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {table} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": fid, "rel": rel, "tid": tid})
                operation = "idempotent" if row else "created"
                if row is None:
                    result = tx.execute(insert_sql, {"from_type": ft, "from_id": fid, "relation": rel, "to_type": tt, "to_id": tid, "properties": json.dumps(props, ensure_ascii=False)})
                    operation = "created" if tx.rowcount(result) else "idempotent"
                    row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {table} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": fid, "rel": rel, "tid": tid})
                if row is None:
                    raise RuntimeError("graph edge insert did not produce a row")
                if return_receipt:
                    stored = normalize_edge_properties(fid, rel, tid, _as_dict(row[2]))
                    receipts.append(EdgeWriteReceipt(operation, fid, rel, tid, row[0], row[1], stored, canonical_edge_digest(fid, rel, tid, row[0], row[1], stored)))
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_mutation_tx(body)

    def update_edges_batch(self, edges: list[dict[str, Any]], *, return_receipt: bool = False) -> Any:
        self._require_write_available()
        if not edges:
            return () if return_receipt else 0
        prepared = []
        seen: set[tuple[str, str, str]] = set()
        for item in edges:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            key = (item.get("from_id"), item.get("relation"), item.get("to_id"))
            if key in seen:
                raise ValueError(f"duplicate global graph key in batch: edge_key={key}")
            seen.add(key)
            validate_digest(item.get("expected_current_digest"), edge=True)
            owner = item.get("owner_pack_id")
            if not isinstance(owner, str) or not owner:
                raise ValueError("graph identity fields must be non-empty strings")
            props = normalize_edge_properties(*key, item.get("properties"))
            if props.get("pack_id") != owner:
                raise EdgeIdentityConflict(f"stale edge update: ({key[0]}, {key[1]}, {key[2]})")
            prepared.append((item["from_type"], key[0], key[1], item["to_type"], key[2], props, item["expected_current_digest"], owner))
        nodes_table, table = self._table("graph_nodes"), self._table("graph_edges")
        def body(tx: GraphTx) -> Any:
            self._lock_graph_rows(
                tx,
                (value for item in prepared for value in (item[1], item[4])),
                ((item[1], item[2], item[4]) for item in prepared),
            )
            receipts = []
            for ft, fid, rel, tt, tid, props, expected, owner in prepared:
                types = {r[0]: r[1] for r in tx.fetchall(f"SELECT node_id, node_type FROM {nodes_table} WHERE node_id IN (:fid,:tid)", {"fid": fid, "tid": tid})}
                row = tx.fetchone(f"SELECT from_type, to_type, properties FROM {table} WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"fid": fid, "rel": rel, "tid": tid})
                if types.get(fid) != ft or types.get(tid) != tt or row is None:
                    raise EdgeIdentityConflict(f"stale edge update: ({fid}, {rel}, {tid})")
                current = normalize_edge_properties(fid, rel, tid, _as_dict(row[2]))
                if current.get("pack_id") != owner or canonical_edge_digest(fid, rel, tid, row[0], row[1], current) != expected:
                    raise EdgeIdentityConflict(f"stale edge update: ({fid}, {rel}, {tid})")
                digest = canonical_edge_digest(fid, rel, tid, ft, tt, props)
                result = tx.execute(f"UPDATE {table} SET from_type=:ft, to_type=:tt, properties=:props WHERE from_id=:fid AND relation=:rel AND to_id=:tid", {"ft": ft, "tt": tt, "props": json.dumps(props, ensure_ascii=False), "fid": fid, "rel": rel, "tid": tid})
                if tx.rowcount(result) != 1:
                    raise EdgeIdentityConflict(f"stale edge update: ({fid}, {rel}, {tid})")
                if return_receipt:
                    receipts.append(EdgeWriteReceipt("updated", fid, rel, tid, ft, tt, props, digest))
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_mutation_tx(body)
