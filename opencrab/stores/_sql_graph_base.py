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
import os
import re
import threading
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import (
    ApplyMigrationRequest,
    DryRunMigrationRequest,
    EdgeIdentityConflict,
    EdgeWriteReceipt,
    ExplicitMerge,
    ExplicitRename,
    FrozenDict,
    GraphInventory,
    GraphMigrationConflict,
    GraphSchemaMigrationRequired,
    LegacyEdgeRow,
    LegacyNodeKey,
    LegacyNodeRow,
    MigrationPlanPayload,
    MigrationReceipt,
    MigrationReceiptPayload,
    NodeIdentityConflict,
    NodeWriteReceipt,
    PropertyNormalizationIssue,
    PropertyResolution,
    ProvenanceBatchReceipt,
    ProvenanceWriteReceipt,
    canonical_edge_digest,
    canonical_json_bytes,
    canonical_node_digest,
    canonical_plan_bytes,
    canonical_receipt_bytes,
    decode_raw_properties,
    normalize_edge_properties,
    parse_properties_object,
    plan_sha256,
    prepare_node,
    receipt_sha256,
    thaw_json,
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

# SQLSTATEs for "the object this statement names is not there": undefined_table
# and undefined_column. Used by ``count_dangling_edges`` to tell a damaged
# schema apart from a genuine query error.
_MISSING_OBJECT_SQLSTATES = frozenset({"42P01", "42703"})
_SQLITE_MISSING_OBJECT = re.compile(r"no such (?:table|column)", re.I)


def _is_missing_object_error(exc: BaseException) -> bool:
    """True only for "that table/column does not exist".

    Deliberately NOT a substring match on ``does not exist``: PostgreSQL
    phrases a type-mismatched comparison as ``operator does not exist:
    integer = text`` (SQLSTATE 42883), and swallowing that as "table missing"
    would make ``count_dangling_edges`` fall through to a bare edge count and
    confidently report every edge as dangling while the node table sits there
    intact. A wrong number is worse than a raised error, so the check is on
    the error code: psycopg2 exposes it as ``pgcode``, psycopg3 as
    ``sqlstate``. SQLite carries no code, but it has no "does not exist"
    phrasing either -- ``no such table``/``no such column`` is unambiguous.
    """
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code is not None:
        return str(code) in _MISSING_OBJECT_SQLSTATES
    return bool(_SQLITE_MISSING_OBJECT.search(str(exc)))


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

    def _run_graph_tx(self, callback: Callable[[GraphTx], Any], *, immediate: bool = False, exclusive: bool = False, snapshot_path: Path | None = None) -> Any:
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
                with self._tx(immediate=immediate, exclusive=exclusive) as raw:
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

    # ------------------------------------------------------------------
    # Issue 80 legacy inventory, request-independent plan, and cutover
    # ------------------------------------------------------------------

    @staticmethod
    def _raw_property_fingerprint(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"encoding": "bytes", "hex": bytes(value).hex()}
        return thaw_json(value)

    @staticmethod
    def _issue(
        kind: str,
        source_key: str,
        field: str,
        aliases: dict[str, Any],
        expected: Any,
        reason: str,
    ) -> PropertyNormalizationIssue:
        return PropertyNormalizationIssue(
            kind, source_key, field,
            FrozenDict({"aliases": aliases, "expected": expected}), reason,
        )

    @staticmethod
    def _same_value(left: Any, right: Any) -> bool:
        try:
            return canonical_json_bytes(left) == canonical_json_bytes(right)
        except (TypeError, ValueError):
            return left == right

    def _node_inventory_row(self, row: Any) -> LegacyNodeRow:
        node_type, node_id, space_id, raw_value = row
        raw, decoded, property_error = decode_raw_properties(
            raw_value, jsonb=self._dialect.name == "postgres"
        )
        issues: list[PropertyNormalizationIssue] = []
        pack_id: str | None = None
        normalized: FrozenDict | None = None
        if decoded is not None:
            values = decoded.to_dict()
            source_key = f"{node_type}:{node_id}"
            aliases = {
                "id": ("id", "node_id"),
                "node_type": ("node_type",),
                "space_id": ("space", "space_id"),
                "pack_id": ("pack_id",),
            }
            reserved = set().union(*aliases.values())
            for field, names in aliases.items():
                present = {name: values[name] for name in names if name in values}
                if not present:
                    continue
                expected = node_id if field == "id" else node_type if field == "node_type" else space_id
                if field == "pack_id":
                    expected = present.get("pack_id")
                    pack_id = expected
                    if expected is not None and (not isinstance(expected, str) or not expected):
                        issues.append(self._issue("node", source_key, field, present, expected, "malformed_reserved_value"))
                        continue
                if field == "space_id":
                    if any(value is not None and (not isinstance(value, str) or not value) for value in present.values()):
                        issues.append(self._issue("node", source_key, field, present, space_id, "malformed_reserved_value"))
                    elif any(not self._same_value(value, space_id) for value in present.values()):
                        issues.append(self._issue("node", source_key, field, present, space_id, "reserved_value_conflict"))
                elif field in {"id", "node_type"} and any(not self._same_value(value, expected) for value in present.values()):
                    issues.append(self._issue("node", source_key, field, present, expected, "reserved_value_conflict"))
            if "node_digest" in values:
                # ``prepare_node`` deliberately rejects this compatibility
                # field.  Report it as an inventory issue instead of carrying
                # it into a plan that would fail later during serialization.
                issues.append(self._issue(
                    "node", source_key, "node_digest",
                    {"node_digest": values["node_digest"]}, None,
                    "reserved_field_not_supported",
                ))
                reserved.add("node_digest")
            if not issues:
                user = {key: value for key, value in values.items() if key not in reserved}
                normalized = FrozenDict(user)
                target_props = dict(user)
                target_props["id"] = node_id
                if space_id is not None:
                    target_props["space"] = space_id
                if pack_id is not None:
                    target_props["pack_id"] = pack_id
                try:
                    digest = canonical_node_digest(node_type, space_id, target_props)
                except (TypeError, ValueError):
                    digest = ""
            else:
                digest = ""
        else:
            digest = ""
        return LegacyNodeRow(
            LegacyNodeKey(str(node_type), str(node_id)),
            space_id,
            pack_id,
            raw,
            normalized,
            property_error,
            tuple(issues),
            digest,
        )

    def _edge_inventory_row(self, row: Any) -> LegacyEdgeRow:
        from_type, from_id, relation, to_type, to_id, raw_value = row
        raw, decoded, property_error = decode_raw_properties(
            raw_value, jsonb=self._dialect.name == "postgres"
        )
        issues: list[PropertyNormalizationIssue] = []
        normalized: FrozenDict | None = None
        if decoded is not None:
            values = decoded.to_dict()
            source_key = f"{from_type}:{from_id}:{relation}:{to_type}:{to_id}"
            aliases = {
                "from_id": ("from_id", "source_id"),
                "from_type": ("from_type", "source_type"),
                "to_id": ("to_id", "target_id"),
                "to_type": ("to_type", "target_type"),
                "relation": ("relation", "edge_type"),
            }
            reserved = set().union(*aliases.values())
            expected_values = {
                "from_id": from_id, "from_type": from_type,
                "to_id": to_id, "to_type": to_type, "relation": relation,
            }
            for field, names in aliases.items():
                present = {name: values[name] for name in names if name in values}
                if not present:
                    continue
                expected = expected_values[field]
                if any(not self._same_value(value, expected) for value in present.values()):
                    issues.append(self._issue("edge", source_key, field, present, expected, "reserved_value_conflict"))
            if "edge_digest" in values:
                # The SQL target derives the edge digest from its endpoint
                # snapshots and properties; it has no writable digest field.
                issues.append(self._issue(
                    "edge", source_key, "edge_digest",
                    {"edge_digest": values["edge_digest"]}, None,
                    "reserved_field_not_supported",
                ))
                reserved.add("edge_digest")
            if not issues:
                user = {key: value for key, value in values.items() if key not in reserved}
                normalized = FrozenDict(user)
                props = dict(user)
                props.update({"from_id": from_id, "relation": relation, "to_id": to_id})
                try:
                    props = normalize_edge_properties(from_id, relation, to_id, props)
                    digest = canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)
                except (TypeError, ValueError):
                    property_error = property_error or "malformed_properties"
                    digest = ""
            else:
                digest = ""
        else:
            digest = ""
        return LegacyEdgeRow(
            LegacyNodeKey(str(from_type), str(from_id)),
            str(relation),
            LegacyNodeKey(str(to_type), str(to_id)),
            raw,
            normalized,
            property_error,
            tuple(issues),
            digest,
        )

    def _inventory_from_rows(self, state: str, node_rows: Iterable[Any], edge_rows: Iterable[Any]) -> GraphInventory:
        nodes = tuple(sorted((self._node_inventory_row(row) for row in node_rows), key=lambda row: (row.key.node_type, row.key.node_id)))
        edges = tuple(sorted((self._edge_inventory_row(row) for row in edge_rows), key=lambda row: (row.from_key.node_type, row.from_key.node_id, row.relation, row.to_key.node_type, row.to_key.node_id)))
        issues = tuple(issue for row in nodes + edges for issue in row.normalization_issues)
        def issue_payload(issue: PropertyNormalizationIssue) -> dict[str, Any]:
            return {
                "record_kind": issue.record_kind,
                "source_key": issue.source_key,
                "field": issue.field,
                "raw_values": thaw_json(issue.raw_values),
                "reason": issue.reason,
            }

        payload = {
            "schema_state": state,
            "nodes": [
                {
                    "key": {"node_type": row.key.node_type, "node_id": row.key.node_id},
                    "space_id": row.space_id,
                    "pack_id": row.pack_id,
                    "raw_properties": self._raw_property_fingerprint(row.raw_properties),
                    "normalized_properties": thaw_json(row.normalized_properties),
                    "property_error": row.property_error,
                    "issues": [issue_payload(issue) for issue in row.normalization_issues],
                }
                for row in nodes
            ],
            "edges": [
                {
                    "from": {"node_type": row.from_key.node_type, "node_id": row.from_key.node_id},
                    "relation": row.relation,
                    "to": {"node_type": row.to_key.node_type, "node_id": row.to_key.node_id},
                    "raw_properties": self._raw_property_fingerprint(row.raw_properties),
                    "normalized_properties": thaw_json(row.normalized_properties),
                    "property_error": row.property_error,
                    "issues": [issue_payload(issue) for issue in row.normalization_issues],
                }
                for row in edges
            ],
        }
        fingerprint = hashlib.sha256(
            b"opencrab.issue80.graph-source.v1\0" + canonical_json_bytes(payload)
        ).hexdigest()
        return GraphInventory(state, nodes, edges, issues, fingerprint)

    def _schema_kind(self) -> str:
        state = getattr(self, "_schema_state", "partial_or_unknown")
        if state == "target":
            return "target"
        if state == "legacy_migration_required":
            return "legacy"
        if state == "unconfigured":
            return "fresh"
        return "partial"

    def _inspect_graph_identity_tx(self, tx: GraphTx, state: str | None = None) -> GraphInventory:
        kind = state or self._schema_kind()
        if kind == "fresh":
            return self._inventory_from_rows(kind, (), ())
        nodes = self._table("graph_nodes")
        edges = self._table("graph_edges")
        node_rows = tx.fetchall(f"SELECT node_type, node_id, space_id, properties FROM {nodes}", {})
        edge_rows = tx.fetchall(f"SELECT from_type, from_id, relation, to_type, to_id, properties FROM {edges}", {})
        return self._inventory_from_rows(kind, node_rows, edge_rows)

    def inspect_graph_identity(self) -> GraphInventory:
        self._require_available()
        kind = self._schema_kind()
        if kind == "fresh":
            return self._inventory_from_rows(kind, (), ())
        # A partial schema is intentionally inspectable even when one of the
        # canonical tables is missing.  Expose the rows that still exist so
        # the operator can see recovery residue; planning remains rejected by
        # ``_build_migration_plan``.
        try:
            node_rows = self._fetch_all(
                f"SELECT node_type, node_id, space_id, properties FROM {self._table('graph_nodes')}", {}
            )
        except Exception as exc:
            if not re.search(r"(?:no such table|no such column|does not exist)", str(exc), re.I):
                raise
            node_rows = []
        try:
            edge_rows = self._fetch_all(
                f"SELECT from_type, from_id, relation, to_type, to_id, properties FROM {self._table('graph_edges')}", {}
            )
        except Exception as exc:
            if not re.search(r"(?:no such table|no such column|does not exist)", str(exc), re.I):
                raise
            edge_rows = []
        return self._inventory_from_rows(kind, node_rows, edge_rows)

    @staticmethod
    def _mapping_sources(action: ExplicitRename | ExplicitMerge) -> tuple[tuple[LegacyNodeKey, str], ...]:
        if isinstance(action, ExplicitRename):
            return ((action.source, action.source_digest),)
        return tuple(sorted(action.sources, key=lambda item: (item[0].node_type, item[0].node_id)))

    @staticmethod
    def _action_target(action: ExplicitRename | ExplicitMerge) -> dict[str, Any]:
        if (
            not isinstance(action.target_node_id, str)
            or not action.target_node_id
            or not isinstance(action.target_node_type, str)
            or not action.target_node_type
            or (action.target_space_id is not None and (not isinstance(action.target_space_id, str) or not action.target_space_id))
            or (action.target_pack_id is not None and (not isinstance(action.target_pack_id, str) or not action.target_pack_id))
        ):
            raise GraphMigrationConflict("invalid migration target identity")
        return {
            "node_id": action.target_node_id,
            "node_type": action.target_node_type,
            "space_id": action.target_space_id,
            "pack_id": action.target_pack_id,
        }

    @staticmethod
    def _resolution_key(resolution: PropertyResolution) -> tuple[LegacyNodeKey, str]:
        return resolution.source, resolution.source_property

    def _derive_user_properties(
        self,
        rows: list[LegacyNodeRow],
        resolutions: tuple[PropertyResolution, ...],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        fields: list[tuple[LegacyNodeKey, str, Any]] = []
        for row in rows:
            if row.property_error or row.normalization_issues or row.normalized_properties is None:
                raise GraphMigrationConflict("legacy graph properties are not normalizable")
            fields.extend((row.key, key, value) for key, value in row.normalized_properties.items())
        fields.sort(key=lambda item: (
            item[0].node_type.encode("utf-8"), item[0].node_id.encode("utf-8"),
            item[1].encode("utf-8"), canonical_json_bytes(item[2]),
        ))
        by_field: dict[tuple[LegacyNodeKey, str], PropertyResolution] = {}
        resolved_targets: set[str] = set()
        reserved = {"id", "node_id", "node_type", "space", "space_id", "pack_id", "node_digest"}
        field_set = {(source, key): value for source, key, value in fields}
        for resolution in resolutions:
            key = self._resolution_key(resolution)
            if key in by_field:
                raise GraphMigrationConflict("duplicate property resolution")
            if resolution.target_property in reserved or not isinstance(resolution.target_property, str) or not resolution.target_property:
                raise GraphMigrationConflict("property resolution targets a reserved key")
            if key not in field_set or not self._same_value(field_set[key], resolution.source_value):
                raise GraphMigrationConflict("property resolution source field mismatch")
            if resolution.target_property in resolved_targets:
                raise GraphMigrationConflict("property resolution target key collision")
            resolved_targets.add(resolution.target_property)
            by_field[key] = resolution
        by_name: dict[str, list[tuple[LegacyNodeKey, str, Any]]] = {}
        for field in fields:
            by_name.setdefault(field[1], []).append(field)
        output: dict[str, Any] = {}
        coverage: list[dict[str, Any]] = []
        for source, key, value in fields:
            resolution = by_field.get((source, key))
            target_key = resolution.target_property if resolution is not None else key
            if target_key in output and not self._same_value(output[target_key], value):
                raise GraphMigrationConflict("different source property values target one key")
            output[target_key] = value
            coverage.append({
                "source": {"node_type": source.node_type, "node_id": source.node_id},
                "property": key,
                "value": thaw_json(value),
                "target_property": target_key,
            })
        for key, group in by_name.items():
            values = [value for _source, _key, value in group]
            if len({canonical_json_bytes(value) for value in values}) > 1:
                if any((source, key) not in by_field for source, _key, _value in group):
                    raise GraphMigrationConflict("missing property resolution")
        return output, coverage

    def _canonical_action(self, action: ExplicitRename | ExplicitMerge) -> dict[str, Any]:
        return {
            "kind": "rename" if isinstance(action, ExplicitRename) else "merge",
            "sources": [
                {"node_type": source.node_type, "node_id": source.node_id, "digest": digest}
                for source, digest in self._mapping_sources(action)
            ],
            "target": self._action_target(action),
        }

    def _target_node(self, action: ExplicitRename | ExplicitMerge, rows: list[LegacyNodeRow], resolutions: tuple[PropertyResolution, ...]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        user, coverage = self._derive_user_properties(rows, resolutions)
        target = self._action_target(action)
        try:
            node_type, props, space_id, digest = prepare_node(
                target["node_type"], target["node_id"],
                {**user, **({"pack_id": target["pack_id"]} if target["pack_id"] is not None else {})},
                target["space_id"],
            )
        except (TypeError, ValueError) as exc:
            raise GraphMigrationConflict("invalid target node payload") from exc
        result = self._canonical_action(action)
        result.update({
            "target": {"node_id": target["node_id"], "node_type": node_type, "space_id": space_id, "pack_id": target["pack_id"]},
            "properties": thaw_json(props),
            "digest": digest,
            "property_fields": coverage,
        })
        return result, coverage, digest

    def _build_migration_plan(self, inventory: GraphInventory, request: DryRunMigrationRequest) -> MigrationPlanPayload:
        try:
            return self._build_migration_plan_inner(inventory, request)
        except GraphMigrationConflict:
            raise
        except Exception as exc:
            raise GraphMigrationConflict("malformed migration request") from exc

    def _build_migration_plan_inner(self, inventory: GraphInventory, request: DryRunMigrationRequest) -> MigrationPlanPayload:
        if inventory.schema_state not in {"legacy", "target"}:
            raise GraphMigrationConflict("graph schema is not migratable")
        if inventory.source_fingerprint != request.expected_source_fingerprint:
            raise GraphMigrationConflict("source fingerprint mismatch")
        if inventory.normalization_issues or any(row.property_error for row in inventory.nodes + inventory.edges):
            raise GraphMigrationConflict("graph inventory contains malformed or conflicting properties")
        rows_by_key = {row.key: row for row in inventory.nodes}
        groups: dict[str, list[LegacyNodeRow]] = {}
        for row in inventory.nodes:
            groups.setdefault(row.key.node_id, []).append(row)
        actions_by_source: dict[LegacyNodeKey, ExplicitRename | ExplicitMerge] = {}
        explicit_targets: dict[str, tuple[LegacyNodeKey, ...]] = {}
        for action in request.mappings:
            sources = self._mapping_sources(action)
            if isinstance(action, ExplicitMerge) and len(sources) < 2:
                raise GraphMigrationConflict("merge requires at least two sources")
            target = self._action_target(action)
            if target["node_id"] in explicit_targets:
                raise GraphMigrationConflict("independent actions collide on target node id")
            explicit_targets[target["node_id"]] = tuple(source for source, _digest in sources)
            for source, source_digest in sources:
                if source in actions_by_source or source not in rows_by_key:
                    raise GraphMigrationConflict("source mapping is repeated or unknown")
                if source_digest != rows_by_key[source].digest:
                    raise GraphMigrationConflict("source digest mismatch")
                actions_by_source[source] = action
        for node_id, group in groups.items():
            if len(group) == 1:
                row = group[0]
                if row.key not in actions_by_source:
                    actions_by_source[row.key] = ExplicitRename(
                        row.key, row.digest, row.key.node_id, row.key.node_type, row.space_id, row.pack_id
                    )
            elif any(row.key not in actions_by_source for row in group):
                raise GraphMigrationConflict(f"duplicate bare node id requires explicit mapping: {node_id}")
        resolutions_by_source: dict[LegacyNodeKey, list[PropertyResolution]] = {}
        for resolution in request.property_resolutions:
            resolutions_by_source.setdefault(resolution.source, []).append(resolution)
        node_specs: list[dict[str, Any]] = []
        source_to_target: dict[LegacyNodeKey, dict[str, Any]] = {}
        seen_action: set[int] = set()
        for source in sorted(actions_by_source, key=lambda key: (key.node_type, key.node_id)):
            action = actions_by_source[source]
            marker = id(action)
            if marker in seen_action:
                continue
            seen_action.add(marker)
            sources = [rows_by_key[key] for key, _digest in self._mapping_sources(action)]
            resolutions = tuple(sorted(
                (
                    resolution
                    for key, _digest in self._mapping_sources(action)
                    for resolution in resolutions_by_source.get(key, ())
                ),
                key=lambda item: (
                    item.source.node_type, item.source.node_id,
                    item.source_property, item.target_property,
                    canonical_json_bytes(item.source_value),
                ),
            ))
            spec, _coverage, _digest = self._target_node(action, sources, resolutions)
            node_specs.append(spec)
            for key, _source_digest in self._mapping_sources(action):
                source_to_target[key] = spec["target"]
        for resolution in request.property_resolutions:
            if resolution.source not in actions_by_source:
                raise GraphMigrationConflict("property resolution source is not mapped")
        target_ids: dict[str, dict[str, Any]] = {}
        for spec in node_specs:
            target_id = spec["target"]["node_id"]
            if target_id in target_ids:
                raise GraphMigrationConflict("target node collision")
            target_ids[target_id] = spec
        edge_specs: list[dict[str, Any]] = []
        collision_results: list[FrozenDict] = []
        dedup_results: list[FrozenDict] = []
        edge_targets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in inventory.edges:
            if row.property_error or row.normalization_issues or row.normalized_properties is None:
                raise GraphMigrationConflict("edge properties are not normalizable")
            if row.from_key not in source_to_target or row.to_key not in source_to_target:
                raise GraphMigrationConflict("edge endpoint mapping is missing")
            source_from = source_to_target[row.from_key]
            source_to = source_to_target[row.to_key]
            props = dict(row.normalized_properties)
            props = normalize_edge_properties(source_from["node_id"], row.relation, source_to["node_id"], props)
            digest = canonical_edge_digest(
                source_from["node_id"], row.relation, source_to["node_id"],
                source_from["node_type"], source_to["node_type"], props,
            )
            spec = {
                "kind": "edge",
                "source": {
                    "from": {"node_type": row.from_key.node_type, "node_id": row.from_key.node_id},
                    "relation": row.relation,
                    "to": {"node_type": row.to_key.node_type, "node_id": row.to_key.node_id},
                    "digest": row.digest,
                },
                "target": {
                    "from_id": source_from["node_id"], "relation": row.relation,
                    "to_id": source_to["node_id"], "from_type": source_from["node_type"], "to_type": source_to["node_type"],
                },
                "properties": thaw_json(props), "digest": digest,
                "result": "retained",
                "property_fields": [
                    {"source": {"from": {"node_type": row.from_key.node_type, "node_id": row.from_key.node_id}, "relation": row.relation, "to": {"node_type": row.to_key.node_type, "node_id": row.to_key.node_id}}, "property": key, "value": thaw_json(value), "target_property": key}
                    for key, value in sorted(
                        row.normalized_properties.items(),
                        key=lambda item: (item[0].encode("utf-8"), canonical_json_bytes(item[1])),
                    )
                ],
            }
            edge_key = (source_from["node_id"], row.relation, source_to["node_id"])
            existing = edge_targets.get(edge_key)
            if existing is not None:
                if existing["digest"] != digest:
                    raise GraphMigrationConflict("edge target collision")
                collision_results.append(FrozenDict({"edge": edge_key, "result": "deduplicated"}))
                dedup_results.append(FrozenDict({"source": spec["source"], "target": edge_key}))
                spec["result"] = "deduplicated"
                edge_specs.append(spec)
                continue
            edge_targets[edge_key] = spec
            edge_specs.append(spec)
        canonical_mappings = tuple(
            FrozenDict(spec) for spec in sorted(node_specs + edge_specs, key=lambda item: canonical_json_bytes(item)
        ))
        mapping_fingerprint = hashlib.sha256(
            b"opencrab.issue80.mapping.v1\0" + canonical_json_bytes(thaw_json(canonical_mappings))
        ).hexdigest()
        node_fingerprint = hashlib.sha256(
            b"opencrab.issue80.planned-nodes.v1\0" + canonical_json_bytes(
                sorted([[spec["target"]["node_id"], spec["digest"]] for spec in node_specs])
            )
        ).hexdigest()
        edge_fingerprint = hashlib.sha256(
            b"opencrab.issue80.planned-edges.v1\0" + canonical_json_bytes(
                sorted([[key[0], key[1], key[2], spec["digest"]] for key, spec in edge_targets.items()])
            )
        ).hexdigest()
        return MigrationPlanPayload(
            inventory.source_fingerprint,
            mapping_fingerprint,
            canonical_mappings,
            node_fingerprint,
            edge_fingerprint,
            tuple(collision_results),
            tuple(dedup_results),
            0,
            0,
        )

    @staticmethod
    def _plan_from_bytes(plan_bytes: bytes) -> MigrationPlanPayload:
        try:
            value = json.loads(bytes(plan_bytes).decode("utf-8"))
            return MigrationPlanPayload(
                value["source_fingerprint"], value["mapping_fingerprint"],
                tuple(FrozenDict(item) for item in value["canonical_mappings"]),
                value["planned_target_node_fingerprint"], value["planned_target_edge_fingerprint"],
                tuple(FrozenDict(item) for item in value["collision_results"]),
                tuple(FrozenDict(item) for item in value["dedup_results"]),
                int(value["edge_loss"]), int(value["property_loss"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphMigrationConflict("malformed migration plan bytes") from exc

    def _validate_decoded_plan(self, inventory: GraphInventory, plan: MigrationPlanPayload, raw_plan: bytes) -> None:
        try:
            self._validate_decoded_plan_inner(inventory, plan, raw_plan)
        except GraphMigrationConflict:
            raise
        except Exception as exc:
            raise GraphMigrationConflict("malformed migration plan") from exc

    def _validate_decoded_plan_inner(self, inventory: GraphInventory, plan: MigrationPlanPayload, raw_plan: bytes) -> None:
        if plan.source_fingerprint != inventory.source_fingerprint:
            raise GraphMigrationConflict("source fingerprint changed after dry-run")
        canonical_bytes = canonical_plan_bytes(plan)
        if canonical_bytes != bytes(raw_plan) or plan_sha256(raw_plan) != plan_sha256(canonical_bytes):
            raise GraphMigrationConflict("migration plan bytes are not canonical")
        if plan.edge_loss != 0 or plan.property_loss != 0:
            raise GraphMigrationConflict("migration plan reports data loss")
        expected_mapping_fingerprint = hashlib.sha256(
            b"opencrab.issue80.mapping.v1\0"
            + canonical_json_bytes(thaw_json(plan.canonical_mappings))
        ).hexdigest()
        if expected_mapping_fingerprint != plan.mapping_fingerprint:
            raise GraphMigrationConflict("migration plan mapping fingerprint changed")
        mappings = tuple(plan.canonical_mappings)
        if mappings != tuple(sorted(mappings, key=canonical_json_bytes)):
            raise GraphMigrationConflict("migration plan mappings are not canonically ordered")
        nodes = {row.key: row for row in inventory.nodes}
        edges = {
            (row.from_key, row.relation, row.to_key): row
            for row in inventory.edges
        }
        source_seen: set[LegacyNodeKey] = set()
        target_nodes: dict[str, str] = {}
        target_edges: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        retained_edge_specs: dict[tuple[str, str, str], dict[str, Any]] = {}
        seen_edges: set[tuple[LegacyNodeKey, str, LegacyNodeKey]] = set()

        def validate_property_fields(
            entries: Any,
            source_rows: list[LegacyNodeRow],
            target_properties: dict[str, Any],
            reserved: set[str],
            *,
            edge: bool = False,
        ) -> None:
            if not isinstance(entries, (list, tuple)):
                raise GraphMigrationConflict("migration plan property coverage is malformed")
            source_map = {} if edge else {row.key: row for row in source_rows}
            expected_keys = set() if edge else {
                (row.key, key)
                for row in source_rows
                for key in row.normalized_properties
            }
            seen_keys: set[tuple[LegacyNodeKey, str]] = set()
            target_values: dict[str, Any] = {}
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {
                    "source", "property", "value", "target_property"
                }:
                    raise GraphMigrationConflict("migration plan property coverage is malformed")
                source_value = entry["source"]
                if edge:
                    if (
                        not isinstance(source_value, dict)
                        or set(source_value) != {"from", "relation", "to"}
                        or not isinstance(source_value["from"], dict)
                        or set(source_value["from"]) != {"node_type", "node_id"}
                        or not isinstance(source_value["to"], dict)
                        or set(source_value["to"]) != {"node_type", "node_id"}
                    ):
                        raise GraphMigrationConflict("migration plan edge property source is malformed")
                    source_key = (
                        LegacyNodeKey(source_value["from"]["node_type"], source_value["from"]["node_id"]),
                        source_value["relation"],
                        LegacyNodeKey(source_value["to"]["node_type"], source_value["to"]["node_id"]),
                    )
                    row = next(
                        (
                            candidate for candidate in source_rows
                            if (candidate.from_key, candidate.relation, candidate.to_key) == source_key
                        ),
                        None,
                    )
                    field_key: tuple[LegacyNodeKey, str] | None = None
                else:
                    if not isinstance(source_value, dict) or set(source_value) != {"node_type", "node_id"}:
                        raise GraphMigrationConflict("migration plan node property source is malformed")
                    source_key = LegacyNodeKey(source_value["node_type"], source_value["node_id"])
                    row = source_map.get(source_key)
                    field_key = (source_key, entry["property"])
                if row is None:
                    raise GraphMigrationConflict("migration plan property source is unknown")
                property_name = entry["property"]
                target_name = entry["target_property"]
                if not isinstance(property_name, str) or not property_name:
                    raise GraphMigrationConflict("migration plan property name is malformed")
                if not isinstance(target_name, str) or not target_name or target_name in reserved:
                    raise GraphMigrationConflict("migration plan property target is reserved or malformed")
                if edge:
                    field_key = (row.from_key, property_name)
                    # Edge source identity is part of the key because one
                    # property name can legitimately occur on two edges.
                    edge_field_key = (source_key, property_name)
                    if edge_field_key in seen_keys:
                        raise GraphMigrationConflict("migration plan property coverage is duplicated")
                    seen_keys.add(edge_field_key)
                    if property_name not in row.normalized_properties:
                        raise GraphMigrationConflict("migration plan property source field is missing")
                    expected_value = row.normalized_properties[property_name]
                else:
                    if field_key in seen_keys or field_key not in expected_keys:
                        raise GraphMigrationConflict("migration plan property coverage is duplicated or unknown")
                    seen_keys.add(field_key)
                    expected_value = row.normalized_properties[property_name]
                if not self._same_value(entry["value"], expected_value):
                    raise GraphMigrationConflict("migration plan property source value changed")
                if target_name in target_values and not self._same_value(target_values[target_name], expected_value):
                    raise GraphMigrationConflict("migration plan property target values collide")
                target_values[target_name] = expected_value
                if target_name not in target_properties or not self._same_value(target_properties[target_name], expected_value):
                    raise GraphMigrationConflict("migration plan property target value is not serialized")
            if edge:
                expected_edge_keys = {
                    ((row.from_key, row.relation, row.to_key), key)
                    for row in source_rows
                    for key in row.normalized_properties
                }
                if seen_keys != expected_edge_keys:
                    raise GraphMigrationConflict("migration plan does not cover every edge property field")
            elif seen_keys != expected_keys:
                raise GraphMigrationConflict("migration plan does not cover every node property field")
            user_properties = {
                key: value for key, value in target_properties.items()
                if key not in reserved
            }
            if set(user_properties) != set(target_values) or any(
                not self._same_value(user_properties[key], value)
                for key, value in target_values.items()
            ):
                raise GraphMigrationConflict("migration plan target properties contain an unplanned field")

        for frozen in plan.canonical_mappings:
            spec = frozen.to_dict()
            if spec.get("kind") in {"rename", "merge"}:
                source_items = spec.get("sources", [])
                target = spec.get("target", {})
                kind = spec.get("kind")
                if not isinstance(source_items, (list, tuple)) or (
                    kind == "rename" and len(source_items) != 1
                ) or (kind == "merge" and len(source_items) < 2):
                    raise GraphMigrationConflict("migration plan node mapping cardinality is invalid")
                if not isinstance(target, dict) or set(target) != {"node_id", "node_type", "space_id", "pack_id"}:
                    raise GraphMigrationConflict("migration plan node target is malformed")
                if (
                    not isinstance(target.get("node_id"), str)
                    or not target["node_id"]
                    or not isinstance(target.get("node_type"), str)
                    or not target["node_type"]
                    or (
                        target.get("space_id") is not None
                        and (
                            not isinstance(target["space_id"], str)
                            or not target["space_id"]
                        )
                    )
                    or (
                        target.get("pack_id") is not None
                        and (
                            not isinstance(target["pack_id"], str)
                            or not target["pack_id"]
                        )
                    )
                ):
                    raise GraphMigrationConflict("migration plan node target identity is malformed")
                for item in source_items:
                    if not isinstance(item, dict):
                        raise GraphMigrationConflict("migration plan node source is malformed")
                    source_type, source_id, source_digest = (
                        item.get("node_type"), item.get("node_id"), item.get("digest")
                    )
                    if not isinstance(source_type, str) or not isinstance(source_id, str):
                        raise GraphMigrationConflict("migration plan node source identity is malformed")
                    source = LegacyNodeKey(source_type, source_id)
                    row = nodes.get(source)
                    if row is None or row.digest != source_digest or source in source_seen:
                        raise GraphMigrationConflict("migration plan source no longer matches")
                    if row.property_error or row.normalization_issues or row.normalized_properties is None:
                        raise GraphMigrationConflict("migration plan source properties are invalid")
                    source_seen.add(source)
                properties = spec.get("properties")
                if not isinstance(properties, dict) or not isinstance(spec.get("digest"), str):
                    raise GraphMigrationConflict("migration plan node properties are malformed")
                try:
                    _nt, prepared, _space, digest = prepare_node(
                        target["node_type"], target["node_id"], properties, target.get("space_id")
                    )
                except (TypeError, ValueError) as exc:
                    raise GraphMigrationConflict("migration plan node target is invalid") from exc
                target_pack = target.get("pack_id")
                if target_pack is None:
                    if "pack_id" in properties:
                        raise GraphMigrationConflict("migration plan node pack target changed")
                elif properties.get("pack_id") != target_pack:
                    raise GraphMigrationConflict("migration plan node pack target changed")
                if digest != spec.get("digest") or thaw_json(prepared) != properties:
                    raise GraphMigrationConflict("migration plan node payload changed")
                validate_property_fields(
                    spec.get("property_fields"),
                    [nodes[source] for source in (
                        LegacyNodeKey(item["node_type"], item["node_id"])
                        for item in source_items
                    )],
                    properties,
                    {"id", "node_id", "node_type", "space", "space_id", "pack_id", "node_digest"},
                )
                target_id = target.get("node_id")
                if target_id in target_nodes:
                    raise GraphMigrationConflict("migration plan target node collision")
                target_nodes[target_id] = digest
            elif spec.get("kind") == "edge":
                source = spec.get("source", {})
                from_value = source.get("from", {})
                to_value = source.get("to", {})
                if not isinstance(from_value, dict) or not isinstance(to_value, dict):
                    raise GraphMigrationConflict("migration plan edge source is malformed")
                source_key = (
                    LegacyNodeKey(from_value.get("node_type"), from_value.get("node_id")),
                    source.get("relation"),
                    LegacyNodeKey(to_value.get("node_type"), to_value.get("node_id")),
                )
                edge = edges.get(source_key)
                if edge is None or source_key in seen_edges or edge.digest != source.get("digest") or edge.normalized_properties is None or edge.property_error or edge.normalization_issues:
                    raise GraphMigrationConflict("migration plan edge source no longer matches")
                seen_edges.add(source_key)
                target = spec.get("target", {})
                if (
                    not isinstance(target, dict)
                    or set(target) != {"from_id", "relation", "to_id", "from_type", "to_type"}
                    or not isinstance(spec.get("properties"), dict)
                ):
                    raise GraphMigrationConflict("migration plan edge target is malformed")
                props = normalize_edge_properties(target["from_id"], target["relation"], target["to_id"], spec.get("properties"))
                digest = canonical_edge_digest(target["from_id"], target["relation"], target["to_id"], target["from_type"], target["to_type"], props)
                if digest != spec.get("digest") or thaw_json(props) != spec.get("properties"):
                    raise GraphMigrationConflict("migration plan edge payload changed")
                validate_property_fields(
                    spec.get("property_fields"),
                    [edge],
                    props,
                    {"from_id", "source_id", "from_type", "source_type", "to_id", "target_id", "to_type", "target_type", "relation", "edge_type"},
                    edge=True,
                )
                result = spec.get("result", "retained")
                target_key = (target["from_id"], target["relation"], target["to_id"])
                if result not in {"retained", "deduplicated"}:
                    raise GraphMigrationConflict("migration plan edge result is invalid")
                target_value = (target["from_type"], target["to_type"], digest)
                previous_target = target_edges.get(target_key)
                if previous_target is not None and previous_target != target_value:
                    raise GraphMigrationConflict("migration plan edge target collision")
                target_edges[target_key] = target_value
                if result == "retained":
                    if target_key in retained_edge_specs:
                        raise GraphMigrationConflict("migration plan edge target collision")
                    retained_edge_specs[target_key] = spec
            else:
                raise GraphMigrationConflict("unknown migration plan mapping")
        if source_seen != set(nodes):
            raise GraphMigrationConflict("migration plan does not cover every source row")
        if seen_edges != set(edges):
            raise GraphMigrationConflict("migration plan does not cover every source edge")
        expected_collisions: list[FrozenDict] = []
        expected_deduplications: list[FrozenDict] = []
        specs_by_source: dict[tuple[LegacyNodeKey, str, LegacyNodeKey], dict[str, Any]] = {}
        for frozen in mappings:
            spec = frozen.to_dict()
            if spec.get("kind") != "edge":
                continue
            source = spec["source"]
            specs_by_source[
                LegacyNodeKey(source["from"]["node_type"], source["from"]["node_id"]),
                source["relation"],
                LegacyNodeKey(source["to"]["node_type"], source["to"]["node_id"]),
            ] = spec
        for edge in inventory.edges:
            spec = specs_by_source[edge.from_key, edge.relation, edge.to_key]
            if spec.get("result", "retained") != "deduplicated":
                continue
            target = spec["target"]
            target_key = (target["from_id"], target["relation"], target["to_id"])
            expected_collisions.append(FrozenDict({"edge": target_key, "result": "deduplicated"}))
            expected_deduplications.append(FrozenDict({"source": spec["source"], "target": target_key}))
        if canonical_json_bytes(plan.collision_results) != canonical_json_bytes(expected_collisions):
            raise GraphMigrationConflict("migration plan collision results changed")
        if canonical_json_bytes(plan.dedup_results) != canonical_json_bytes(expected_deduplications):
            raise GraphMigrationConflict("migration plan deduplication results changed")
        for key, value in target_edges.items():
            spec = next(
                item.to_dict() for item in plan.canonical_mappings
                if item.get("kind") == "edge"
                and tuple(item.get("target", {}).get(field) for field in ("from_id", "relation", "to_id")) == key
                and item.get("digest") == value[2]
            )
            if spec.get("result", "retained") == "deduplicated" and key not in retained_edge_specs:
                raise GraphMigrationConflict("migration plan deduplication has no retained edge")
        for key, (from_type, to_type, _digest) in target_edges.items():
            node_from = target_nodes.get(key[0])
            node_to = target_nodes.get(key[2])
            if node_from is None or node_to is None:
                raise GraphMigrationConflict("migration plan edge endpoint is missing")
            edge_spec = retained_edge_specs.get(key)
            if edge_spec is not None and (
                edge_spec["target"]["from_type"] != from_type
                or edge_spec["target"]["to_type"] != to_type
            ):
                raise GraphMigrationConflict("migration plan edge endpoint type changed")
        node_fingerprint = hashlib.sha256(
            b"opencrab.issue80.planned-nodes.v1\0"
            + canonical_json_bytes(sorted([[key, value] for key, value in target_nodes.items()]))
        ).hexdigest()
        edge_fingerprint = hashlib.sha256(
            b"opencrab.issue80.planned-edges.v1\0"
            + canonical_json_bytes(sorted([[key[0], key[1], key[2], value[2]] for key, value in target_edges.items() if key in retained_edge_specs]))
        ).hexdigest()
        if node_fingerprint != plan.planned_target_node_fingerprint or edge_fingerprint != plan.planned_target_edge_fingerprint:
            raise GraphMigrationConflict("migration plan target fingerprint changed")

    @staticmethod
    def _request_digest(request: ApplyMigrationRequest) -> str:
        payload = {
            "request_id": request.request_id,
            "phase": "apply",
            "expected_source_fingerprint": request.expected_source_fingerprint,
            "plan_sha256": request.plan_sha256,
            "backup_path": str(Path(request.backup_path)),
            "backup_sha256": request.backup_sha256,
        }
        return hashlib.sha256(
            b"opencrab.issue80.apply-request.v1\0" + canonical_json_bytes(payload)
        ).hexdigest()

    @staticmethod
    def _dry_request_digest(request: DryRunMigrationRequest, mapping_fingerprint: str) -> str:
        return hashlib.sha256(
            b"opencrab.issue80.dry-request.v1\0" + canonical_json_bytes({
                "source_fingerprint": request.expected_source_fingerprint,
                "mapping_fingerprint": mapping_fingerprint,
            })
        ).hexdigest()

    @staticmethod
    def _make_receipt(
        *, request_id: str | None, phase: str, request_digest: str,
        inventory: GraphInventory, plan: MigrationPlanPayload, plan_bytes: bytes,
        target_before: str, target_after: str,
    ) -> MigrationReceipt:
        payload = MigrationReceiptPayload(
            request_id, phase, request_digest, inventory.source_fingerprint,
            plan.mapping_fingerprint, plan_sha256(plan_bytes), target_before,
            target_after, tuple(FrozenDict(item) for item in plan.canonical_mappings),
            plan.edge_loss, plan.property_loss,
        )
        canonical = canonical_receipt_bytes(payload)
        return MigrationReceipt(
            request_id, phase, request_digest, inventory.source_fingerprint,
            plan.mapping_fingerprint, plan_sha256(plan_bytes), bytes(plan_bytes),
            target_before, target_after, payload.mapping_result, plan.edge_loss,
            plan.property_loss, receipt_sha256(canonical), canonical,
        )

    def _ledger_table(self) -> str:
        return self._table("graph_migration_receipts")

    def _ledger_exists_tx(self, tx: GraphTx) -> bool:
        """Check ledger existence without provoking a failed-table SELECT."""
        if self._dialect.name == "sqlite":
            row = tx.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:name",
                {"name": "graph_migration_receipts"},
            )
        else:
            row = tx.fetchone(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=:schema AND table_name=:name",
                {"schema": self._schema, "name": "graph_migration_receipts"},
            )
        return row is not None

    def _ledger_lookup(self, request_id: str) -> Any | None:
        try:
            return self._fetch_one(
                f"SELECT request_id, phase, request_digest, source_fingerprint, mapping_fingerprint, plan_sha256, target_fingerprint_before, target_fingerprint_after, edge_loss, property_loss, receipt_bytes FROM {self._ledger_table()} WHERE request_id=:request_id",
                {"request_id": request_id},
            )
        except Exception:
            return None

    @staticmethod
    def _receipt_from_ledger(row: Any) -> MigrationReceipt:
        try:
            if len(row) != 11:
                raise GraphMigrationConflict("migration ledger row is malformed")
            raw = bytes(row[-1])
            value = json.loads(raw.decode("utf-8"))
            required = {
                "request_id", "phase", "request_digest", "source_fingerprint",
                "mapping_fingerprint", "plan_sha256", "target_fingerprint_before",
                "target_fingerprint_after", "mapping_result", "edge_loss", "property_loss",
            }
            if not isinstance(value, dict) or set(value) != required or value.get("phase") != "apply":
                raise GraphMigrationConflict("migration ledger receipt is malformed")
            columns = (
                value["request_id"], value["phase"], value["request_digest"],
                value["source_fingerprint"], value["mapping_fingerprint"],
                value["plan_sha256"], value["target_fingerprint_before"],
                value["target_fingerprint_after"], value["edge_loss"], value["property_loss"],
            )
            if tuple(row[:10]) != columns:
                raise GraphMigrationConflict("migration ledger columns do not match receipt")
            mapping_result = tuple(FrozenDict(item) for item in value["mapping_result"])
            payload = MigrationReceiptPayload(
                value["request_id"], value["phase"], value["request_digest"],
                value["source_fingerprint"], value["mapping_fingerprint"],
                value["plan_sha256"], value["target_fingerprint_before"],
                value["target_fingerprint_after"], mapping_result,
                value["edge_loss"], value["property_loss"],
            )
            if canonical_receipt_bytes(payload) != raw:
                raise GraphMigrationConflict("migration ledger receipt bytes are not canonical")
            receipt = MigrationReceipt(
                value["request_id"], value["phase"], value["request_digest"],
                value["source_fingerprint"], value["mapping_fingerprint"],
                value["plan_sha256"], b"", value["target_fingerprint_before"],
                value["target_fingerprint_after"], mapping_result,
                int(value["edge_loss"]), int(value.get("property_loss", 0)),
                receipt_sha256(raw), raw,
            )
            return receipt
        except GraphMigrationConflict:
            raise
        except Exception as exc:
            raise GraphMigrationConflict("migration ledger receipt is malformed") from exc

    def _verify_backup(self, request: ApplyMigrationRequest) -> None:
        try:
            path = Path(request.backup_path)
            if not path.is_file() or path.is_symlink():
                raise GraphMigrationConflict("verified migration backup is required")
            if self._dialect.name == "sqlite":
                live_path = Path(self._db_path).resolve()
                if path.resolve() == live_path or os.path.samefile(path, live_path):
                    raise GraphMigrationConflict("migration backup must differ from live graph")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != request.backup_sha256:
                raise GraphMigrationConflict("migration backup SHA-256 mismatch")
        except GraphMigrationConflict:
            raise
        except Exception as exc:
            raise GraphMigrationConflict("verified migration backup is required") from exc

    @staticmethod
    def _validate_apply_request(request: ApplyMigrationRequest) -> None:
        if (
            not isinstance(request.request_id, str)
            or not request.request_id
            or not isinstance(request.expected_source_fingerprint, str)
            or not request.expected_source_fingerprint
            or not isinstance(request.plan_bytes, bytes)
            or not isinstance(request.plan_sha256, str)
            or not request.plan_sha256
            or not isinstance(request.backup_sha256, str)
            or not request.backup_sha256
        ):
            raise GraphMigrationConflict("malformed apply request")
        try:
            Path(request.backup_path)
        except (TypeError, ValueError) as exc:
            raise GraphMigrationConflict("malformed apply request") from exc

    def _stage_and_cutover(self, tx: GraphTx, plan: MigrationPlanPayload) -> None:
        json_kind = "JSONB" if self._dialect.name == "postgres" else "TEXT"
        tx.execute(f"CREATE TEMP TABLE issue80_stage_nodes (node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, space_id TEXT, properties {json_kind} NOT NULL)")
        tx.execute(f"CREATE TEMP TABLE issue80_stage_edges (from_id TEXT NOT NULL, relation TEXT NOT NULL, to_id TEXT NOT NULL, from_type TEXT NOT NULL, to_type TEXT NOT NULL, properties {json_kind} NOT NULL, PRIMARY KEY (from_id, relation, to_id))")
        nodes = [spec.to_dict() for spec in plan.canonical_mappings if spec.get("kind") in {"rename", "merge"}]
        edges = [
            spec.to_dict()
            for spec in plan.canonical_mappings
            if spec.get("kind") == "edge" and spec.get("result", "retained") == "retained"
        ]
        for spec in nodes:
            target = spec["target"]
            tx.execute("INSERT INTO issue80_stage_nodes (node_id,node_type,space_id,properties) VALUES (:node_id,:node_type,:space_id,:properties)", {"node_id": target["node_id"], "node_type": target["node_type"], "space_id": target.get("space_id"), "properties": json.dumps(spec["properties"], ensure_ascii=False)})
        for spec in edges:
            target = spec["target"]
            tx.execute("INSERT INTO issue80_stage_edges (from_id,relation,to_id,from_type,to_type,properties) VALUES (:from_id,:relation,:to_id,:from_type,:to_type,:properties)", {"from_id": target["from_id"], "relation": target["relation"], "to_id": target["to_id"], "from_type": target["from_type"], "to_type": target["to_type"], "properties": json.dumps(spec["properties"], ensure_ascii=False)})
        nodes_table = self._table("graph_nodes")
        edges_table = self._table("graph_edges")
        tx.execute(f"DROP TABLE IF EXISTS {edges_table}")
        tx.execute(f"DROP TABLE IF EXISTS {nodes_table}")
        schema_name = getattr(self, "_schema", None)
        ddls = self._dialect.render_ddl(GRAPH_STORE_SCHEMA, schema_name=schema_name) if schema_name is not None else self._dialect.render_ddl(GRAPH_STORE_SCHEMA)
        for ddl in ddls:
            tx.execute(ddl)
        insert_node = self._dialect.insert(nodes_table, ["node_type", "node_id", "space_id", "properties"], json_columns=["properties"])
        insert_edge = self._dialect.insert(edges_table, ["from_type", "from_id", "relation", "to_type", "to_id", "properties"], json_columns=["properties"])
        for spec in nodes:
            target = spec["target"]
            tx.execute(insert_node, {"node_type": target["node_type"], "node_id": target["node_id"], "space_id": target.get("space_id"), "properties": json.dumps(spec["properties"], ensure_ascii=False)})
        for spec in edges:
            target = spec["target"]
            tx.execute(insert_edge, {"from_type": target["from_type"], "from_id": target["from_id"], "relation": target["relation"], "to_type": target["to_type"], "to_id": target["to_id"], "properties": json.dumps(spec["properties"], ensure_ascii=False)})
        tx.execute("DROP TABLE issue80_stage_edges")
        tx.execute("DROP TABLE issue80_stage_nodes")

    def _target_graph_matches_plan(self, tx: GraphTx, plan: MigrationPlanPayload) -> bool:
        """Return whether an already-qualified target is an exact plan no-op.

        A target database is not a legacy staging area.  Replacing its tables
        for an identity-preserving plan would create unnecessary DDL and
        could invalidate unrelated read snapshots.  Compare the complete
        canonical row set instead, including endpoint type snapshots, and let
        the caller reject any non-identical target before graph DML.
        """
        expected_nodes: dict[str, dict[str, Any]] = {}
        expected_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for frozen in plan.canonical_mappings:
            spec = frozen.to_dict()
            if spec.get("kind") in {"rename", "merge"}:
                target = spec["target"]
                expected_nodes[target["node_id"]] = spec
            elif spec.get("kind") == "edge" and spec.get("result", "retained") == "retained":
                target = spec["target"]
                expected_edges[(target["from_id"], target["relation"], target["to_id"])] = spec

        nodes = self._table("graph_nodes")
        actual_nodes = tx.fetchall(
            f"SELECT node_id, node_type, space_id, properties FROM {nodes}", {}
        )
        if len(actual_nodes) != len(expected_nodes):
            return False
        for node_id, node_type, space_id, raw_properties in actual_nodes:
            spec = expected_nodes.get(node_id)
            if spec is None:
                return False
            target = spec["target"]
            try:
                properties = _merge_space(_as_dict(raw_properties), space_id)
                digest = canonical_node_digest(
                    node_type, space_id or properties.get("space"), properties
                )
            except (TypeError, ValueError):
                return False
            if (
                node_type != target["node_type"]
                or space_id != target.get("space_id")
                or digest != spec["digest"]
            ):
                return False

        edges = self._table("graph_edges")
        actual_edges = tx.fetchall(
            f"SELECT from_id, relation, to_id, from_type, to_type, properties FROM {edges}", {}
        )
        if len(actual_edges) != len(expected_edges):
            return False
        for from_id, relation, to_id, from_type, to_type, raw_properties in actual_edges:
            key = (from_id, relation, to_id)
            spec = expected_edges.get(key)
            if spec is None:
                return False
            target = spec["target"]
            try:
                properties = normalize_edge_properties(
                    from_id, relation, to_id, _as_dict(raw_properties)
                )
                digest = canonical_edge_digest(
                    from_id, relation, to_id, from_type, to_type, properties
                )
            except (TypeError, ValueError):
                return False
            if (
                from_type != target["from_type"]
                or to_type != target["to_type"]
                or digest != spec["digest"]
            ):
                return False
        return True

    def _ensure_ledger(self, tx: GraphTx) -> None:
        blob = "BYTEA" if self._dialect.name == "postgres" else "BLOB"
        tx.execute(f"CREATE TABLE IF NOT EXISTS {self._ledger_table()} (request_id TEXT PRIMARY KEY, phase TEXT NOT NULL, request_digest TEXT NOT NULL, source_fingerprint TEXT NOT NULL, mapping_fingerprint TEXT NOT NULL, plan_sha256 TEXT NOT NULL, target_fingerprint_before TEXT NOT NULL, target_fingerprint_after TEXT NOT NULL, edge_loss INTEGER NOT NULL, property_loss INTEGER NOT NULL, receipt_bytes {blob} NOT NULL, created_at TEXT NOT NULL)")

    def _apply_migration(self, request: ApplyMigrationRequest) -> MigrationReceipt:
        self._require_available()
        self._validate_apply_request(request)
        try:
            request_digest = self._request_digest(request)
        except Exception as exc:
            raise GraphMigrationConflict("malformed apply request") from exc
        existing = self._ledger_lookup(request.request_id)
        if existing is not None:
            if existing[2] != request_digest:
                raise GraphMigrationConflict("apply request ID was reused with different bytes")
            return self._receipt_from_ledger(existing)
        self._verify_backup(request)
        supplied_plan = bytes(request.plan_bytes)
        if request.plan_sha256 != plan_sha256(supplied_plan):
            raise GraphMigrationConflict("migration plan SHA-256 mismatch")
        plan = self._plan_from_bytes(supplied_plan)
        if self._schema_kind() not in {"legacy", "target"}:
            raise GraphMigrationConflict("graph schema is not migratable")

        def body(tx: GraphTx) -> MigrationReceipt:
            # A duplicate caller can have waited for the first exclusive
            # transaction to commit.  Read the ledger before rechecking the
            # now-canonical graph so that unknown commit status resolves to
            # the original immutable receipt instead of a stale-source
            # conflict.  The table is absent on the first attempt; that is
            # the only expected SELECT failure here and is handled below by
            # the create-if-needed path after all validation.
            if self._ledger_exists_tx(tx):
                replay_row = tx.fetchone(
                    f"SELECT request_id, phase, request_digest, source_fingerprint, mapping_fingerprint, plan_sha256, target_fingerprint_before, target_fingerprint_after, edge_loss, property_loss, receipt_bytes FROM {self._ledger_table()} WHERE request_id=:request_id",
                    {"request_id": request.request_id},
                )
                if replay_row is not None:
                    if replay_row[2] != request_digest:
                        raise GraphMigrationConflict("apply request ID was reused with different bytes")
                    return self._receipt_from_ledger(replay_row)

            inventory = self._inspect_graph_identity_tx(tx)
            if inventory.source_fingerprint != request.expected_source_fingerprint:
                raise GraphMigrationConflict("source fingerprint changed before apply")
            self._validate_decoded_plan(inventory, plan, supplied_plan)
            self._ensure_ledger(tx)
            try:
                row = tx.fetchone(f"SELECT request_id, phase, request_digest, source_fingerprint, mapping_fingerprint, plan_sha256, target_fingerprint_before, target_fingerprint_after, edge_loss, property_loss, receipt_bytes FROM {self._ledger_table()} WHERE request_id=:request_id", {"request_id": request.request_id})
            except Exception as exc:
                raise GraphMigrationConflict("migration ledger is unavailable") from exc
            if row is not None:
                if row[2] != request_digest:
                    raise GraphMigrationConflict("apply request ID was reused with different bytes")
                return self._receipt_from_ledger(row)
            before = self._graph_fingerprint_tx(tx)
            if self._schema_kind() == "target":
                if not self._target_graph_matches_plan(tx, plan):
                    raise GraphMigrationConflict("preexisting target conflicts with migration plan")
                # The target is already canonical.  Keep its rows and indexes
                # untouched; this is a receipt-only no-op for a new request.
                after = before
            else:
                self._stage_and_cutover(tx, plan)
                after = self._graph_fingerprint_tx(tx)
            receipt = self._make_receipt(
                request_id=request.request_id, phase="apply", request_digest=request_digest,
                inventory=inventory, plan=plan, plan_bytes=supplied_plan,
                target_before=before, target_after=after,
            )
            tx.execute(f"INSERT INTO {self._ledger_table()} (request_id,phase,request_digest,source_fingerprint,mapping_fingerprint,plan_sha256,target_fingerprint_before,target_fingerprint_after,edge_loss,property_loss,receipt_bytes,created_at) VALUES (:request_id,:phase,:request_digest,:source_fingerprint,:mapping_fingerprint,:plan_sha256,:target_before,:target_after,:edge_loss,:property_loss,:receipt_bytes,:created_at)", {"request_id": request.request_id, "phase": "apply", "request_digest": request_digest, "source_fingerprint": inventory.source_fingerprint, "mapping_fingerprint": plan.mapping_fingerprint, "plan_sha256": plan_sha256(supplied_plan), "target_before": before, "target_after": after, "edge_loss": plan.edge_loss, "property_loss": plan.property_loss, "receipt_bytes": receipt.canonical_bytes, "created_at": self._dialect.bind_value_for_timestamp(datetime.now(UTC))})
            return receipt

        receipt = self._run_graph_tx(body, exclusive=self._dialect.name == "sqlite")
        setattr(self, "_schema_state", "target")
        return receipt

    def migrate_graph_identity(self, request: DryRunMigrationRequest | ApplyMigrationRequest) -> MigrationReceipt:
        if isinstance(request, ApplyMigrationRequest):
            return self._apply_migration(request)
        if not isinstance(request, DryRunMigrationRequest):
            raise GraphMigrationConflict("malformed graph migration request")
        self._require_available()
        inventory = self.inspect_graph_identity()
        plan = self._build_migration_plan(inventory, request)
        encoded = canonical_plan_bytes(plan)
        return self._make_receipt(
            request_id=None,
            phase="dry_run",
            request_digest=self._dry_request_digest(request, plan.mapping_fingerprint),
            inventory=inventory,
            plan=plan,
            plan_bytes=encoded,
            target_before=inventory.source_fingerprint,
            target_after=inventory.source_fingerprint,
        )

    def reclassify_node(
        self,
        node_id: str,
        *,
        expected_current_digest: str,
        new_type: str,
        new_space_id: str | None = None,
        new_properties: dict[str, Any],
        return_receipt: bool = False,
    ) -> dict[str, Any] | NodeWriteReceipt:
        return self.update_node(
            node_id, expected_current_digest, new_type, new_properties,
            new_space_id, return_receipt=return_receipt,
        )

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
                f"SELECT from_id, relation, to_id, from_type, to_type, properties FROM {edges} WHERE from_id=:nid OR to_id=:nid",
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
            edge_updates: list[tuple[str, str, str, str, str, str]] = []
            for from_id, relation, to_id, from_type, to_type, raw_properties in incident:
                try:
                    edge_properties = normalize_edge_properties(
                        from_id, relation, to_id, parse_properties_object(raw_properties)
                    )
                    next_from_type = new_type if from_id == node_id else from_type
                    next_to_type = new_type if to_id == node_id else to_type
                    edge_digest = canonical_edge_digest(
                        from_id, relation, to_id, next_from_type, next_to_type,
                        edge_properties,
                    )
                except (TypeError, ValueError) as exc:
                    raise GraphMigrationConflict("incident edge properties are malformed") from exc
                edge_updates.append((from_id, relation, to_id, next_from_type, next_to_type, edge_digest))
            result = tx.execute(f"UPDATE {nodes} SET node_type=:nt, space_id=:sid, properties=:props WHERE node_id=:nid", {"nt": new_type, "sid": space_id, "props": json.dumps(props, ensure_ascii=False), "nid": node_id})
            if tx.rowcount(result) != 1:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            # Type snapshots are compatibility fields and must follow the node.
            for from_id, relation, to_id, from_type, to_type, edge_digest in edge_updates:
                edge_result = tx.execute(
                    f"UPDATE {edges} SET from_type=:from_type, to_type=:to_type WHERE from_id=:from_id AND relation=:relation AND to_id=:to_id",
                    {
                        "from_type": from_type, "to_type": to_type,
                        "from_id": from_id, "relation": relation, "to_id": to_id,
                    },
                )
                if tx.rowcount(edge_result) != 1:
                    raise GraphMigrationConflict("incident edge snapshot disappeared")
                # The edge digest is a stored compatibility field only on
                # Neo4j; SQL derives it from the row, so no extra column is
                # written here.  ``edge_digest`` is still computed above to
                # validate the complete post-reclassification identity.
                del edge_digest
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

    def count_dangling_edges(self) -> int:
        """Edges whose endpoint snapshot does not resolve to a node row.

        Counts exactly what the write guards reject: an endpoint id with no
        ``graph_nodes`` row, OR a row whose ``node_type`` differs from the
        type the edge recorded. One invariant, one number.

        WHY THIS EXISTS (issue #84). Every edge writer here checks its
        endpoints inside the mutation transaction, and ``delete_node`` clears
        incident edges in the same transaction -- so no store API path
        produces a dangling row. But ``GRAPH_STORE_SCHEMA`` declares no
        foreign key, so the DATABASE does not enforce it: a raw SQL script
        against the file still can, and nothing could report it afterwards.
        This is that report. (A FK was evaluated and deferred: the schema
        classifiers treat any FK on these two tables as a non-canonical
        schema and refuse writes, ``SchemaSpec`` cannot express one, and
        SQLite cannot add one without rebuilding the table -- a schema
        generation/migration unit of its own.)

        SQL-backends only, and deliberately not on the GraphStore Protocol
        (same call as ``search_nodes`` -- see its note there). Neo4j cannot
        hold a relationship without both endpoints, and its own
        ``_initialise_schema_state`` already walks every OpenCrab-owned
        relationship and classifies label/type drift as partial_or_unknown,
        which gates writes. The SQL classifiers only read DDL and column
        metadata, never rows -- that asymmetry is the gap this closes.

        A damaged schema still answers rather than raising a driver error:
        ``inspect_graph_identity`` already takes that stance ("expose the
        rows that still exist so the operator can see recovery residue"),
        and a diagnostic is most needed exactly when the schema is damaged.
        No ``graph_edges`` -> 0; no ``graph_nodes`` -> every edge, since no
        endpoint can resolve. Only missing-object errors degrade (see
        ``_is_missing_object_error``); everything else reaches the caller.

        The fallback is a SEPARATE ``_fetch_one`` call on purpose. On
        PostgreSQL a failed statement poisons its transaction, so a retry
        sharing that transaction would fail too; ``PGGraphStore._fetch_one``
        opens its own connection context per call, which is what makes the
        sequence work.
        """
        self._require_available()
        nodes, edges = self._table("graph_nodes"), self._table("graph_edges")
        sql = (
            f"SELECT COUNT(*) FROM {edges} e"  # noqa: S608
            f" WHERE NOT EXISTS (SELECT 1 FROM {nodes} n WHERE n.node_type=e.from_type AND n.node_id=e.from_id)"
            f"    OR NOT EXISTS (SELECT 1 FROM {nodes} n WHERE n.node_type=e.to_type AND n.node_id=e.to_id)"
        )
        try:
            row = self._fetch_one(sql, {})
        except Exception as exc:
            if not _is_missing_object_error(exc):
                raise
            try:
                row = self._fetch_one(f"SELECT COUNT(*) FROM {edges}", {})  # noqa: S608
            except Exception as edges_exc:
                if not _is_missing_object_error(edges_exc):
                    raise
                return 0
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
