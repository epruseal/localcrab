"""
_SqlGraphStoreBase — shared implementation of the 20-method graph-store
surface (ensure_constraints, upsert_node, get_node, lookup_node_type,
delete_node, upsert_edge, run_cypher, find_neighbors, find_path, count_nodes,
list_packs, find_by_relations, get_node_by_id, export_nodes, export_edges,
upsert_nodes_batch, upsert_edges_batch, plus the store-owned lifecycle trio
available/ping/close), parameterised by a ``SqlDialect`` (SQLITE or POSTGRES
from ``_sql_dialect.py``) — the Stage 6b (graph) counterpart of
``_sql_doc_base.py`` (Stage 6a, doc-store).

STAGE 6b STATUS: authored and unit-tested standalone. NOT yet wired into
LocalGraphStore / PGGraphStore — that migration is Stage 6b's F3 (SQLite) /
F4 (PG) adopter follow-up, done separately given how risky this refactor
class is (BFS traversal + hub fan-out perf are load-bearing). factory.py, the
two stores' public class names, and their module paths are all unchanged by
this file's existence.

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
       - ``_exec_write(sql, params) -> int``          one write statement;
         rowcount; must commit
       - ``_exec_write_many(statements) -> list[int]``  MULTIPLE write
         statements in ONE transaction (rowcount per statement) — needed
         because ``delete_node`` must delete the node row and its incident
         edges atomically (two DELETEs, one commit), matching both existing
         stores' current behavior
       - ``_exec_write_batch(sql, params_list) -> None``  one INSERT/UPSERT
         statement executed once per params dict, all in one transaction
         (SQLite: ``executemany`` + one commit; PG: one ``conn.execute(stmt,
         list_of_dicts)`` — SQLAlchemy expands this to an executemany
         automatically) — used by ``upsert_nodes_batch``/``upsert_edges_batch``
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
    F3 should re-run bench_graph_backends.py after adoption before assuming
    this is a non-issue.

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
import json
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from opencrab.stores._graph_common import (
    KEYWORD_SEARCH_FIELDS,
    _as_dict,
    _edge_passes,
    _merge_space,
    _node_passes,
    _normalize_space,
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
            primary_key=("node_type", "node_id"),
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
            primary_key=("from_type", "from_id", "relation", "to_type", "to_id"),
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
    def _exec_write(self, sql: str, params: dict[str, Any]) -> int: ...

    @abc.abstractmethod
    def _exec_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]: ...

    @abc.abstractmethod
    def _exec_write_batch(self, sql: str, params_list: list[dict[str, Any]]) -> None: ...

    @abc.abstractmethod
    def _require_available(self) -> None: ...

    # ------------------------------------------------------------------
    # Schema (no-op for both current backends — PRIMARY KEY covers uniqueness)
    # ------------------------------------------------------------------

    def ensure_constraints(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
        space_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_available()
        props = {**properties, "id": node_id}
        # issue #118: reconcile space_id/props["space"] before either lands,
        # so the SQL predicates below (space_id column) can never disagree
        # with what _merge_space would report back out on read.
        props, space_id = _normalize_space(props, space_id)
        sql = self._dialect.upsert(
            self._table("graph_nodes"),
            ["node_type", "node_id", "space_id", "properties"],
            conflict_cols=["node_type", "node_id"],
            update_cols=["space_id", "properties"],
            json_columns=["properties"],
        )
        self._exec_write(
            sql,
            {
                "node_type": node_type,
                "node_id": node_id,
                "space_id": space_id,
                "properties": json.dumps(props),
            },
        )
        return props

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
        if not getattr(self, "_available", False):
            return None
        sql = f"SELECT node_type FROM {self._table('graph_nodes')} WHERE node_id=:node_id LIMIT 1"
        row = self._fetch_one(sql, {"node_id": node_id})
        return row[0] if row else None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """True iff the node itself was deleted (unified B2 contract); the
        incident-edge cleanup is a side effect, not the signal. Both DELETEs
        run in ONE transaction via ``_exec_write_many`` so a crash between
        them can never leave orphaned edges."""
        self._require_available()
        params = {"nt": node_type, "nid": node_id}
        node_sql = f"DELETE FROM {self._table('graph_nodes')} WHERE node_type=:nt AND node_id=:nid"
        edge_sql = (
            f"DELETE FROM {self._table('graph_edges')}"
            " WHERE (from_type=:nt AND from_id=:nid) OR (to_type=:nt AND to_id=:nid)"
        )
        rowcounts = self._exec_write_many([(node_sql, params), (edge_sql, params)])
        return rowcounts[0] > 0

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
    ) -> bool:
        self._require_available()
        sql = self._dialect.upsert(
            self._table("graph_edges"),
            ["from_type", "from_id", "relation", "to_type", "to_id", "properties"],
            conflict_cols=["from_type", "from_id", "relation", "to_type", "to_id"],
            update_cols=["properties"],
            json_columns=["properties"],
        )
        self._exec_write(
            sql,
            {
                "from_type": from_type,
                "from_id": from_id,
                "relation": relation,
                "to_type": to_type,
                "to_id": to_id,
                "properties": json.dumps(properties or {}),
            },
        )
        return True

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
            f"SELECT properties FROM {self._table('graph_edges')}"
            " WHERE from_type=:from_type AND from_id=:from_id"
            " AND relation=:relation AND to_type=:to_type AND to_id=:to_id"
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
        return _as_dict(row[0]) if row else None

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Not supported by either SQL backend — returns [] with a warning."""
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
                # from_id/to_id alone (the PK is (node_type, node_id)), so an
                # unreadable twin's edges can reach this point. Confidentiality
                # is preserved by the fetch's own JOIN, which pack-filters the
                # OTHER endpoint in SQL -- not by _edge_passes. See
                # _pack_where's docstring; #147 section 8 records the residue.
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
            rows = self._fetch_all(
                f"SELECT DISTINCT {pid} FROM {self._table(table)} "  # noqa: S608
                f"WHERE {pid} IS NOT NULL",
                {},
            )
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

        THAT ASSUMPTION IS NOT GUARANTEED, and an earlier version of this
        docstring asserted it as one. The anchor is resolved by
        ``_fetch_node_props_by_id``, whose SQL matches ``node_id`` alone
        even though the PK is ``(node_type, node_id)``, while
        ``_fetch_edges_for_node`` matches ``from_id``/``to_id`` alone too --
        so when one ``node_id`` exists in two packs under different types,
        edges belonging to the unreadable twin do enter the traversal.
        What actually keeps that from disclosing anything is NOT
        ``_edge_passes``: it is the JOIN in ``_fetch_edges_for_node`` and
        ``PGGraphStore._batch_frontier_edges``, which pack-filters the OTHER
        endpoint in SQL, so every row that comes back is one the caller may
        read. Do not remove that JOIN clause on the grounds that
        ``_edge_passes`` will catch it -- it will not. The residue is a
        correctness defect (neighbours attributed to the wrong node), not a
        confidentiality one; issue #147 section 8 records it and opens a
        follow-up for making traversal match on the full key.

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
            {"props": _merge_space(_as_dict(properties), space_id), "labels": [node_type]}
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
        ``get_node_by_id``'s ``WHERE node_id=:nid LIMIT 1`` has no
        ``node_type`` predicate, and ``graph_nodes``' real PK is
        ``(node_type, node_id)`` -- the same ``node_id`` CAN exist under a
        different ``node_type`` in a pack the caller cannot read. A
        post-filter on whichever single row SQL's ``LIMIT 1`` happened to
        pick would then answer "not found" even when the caller's OWN pack
        genuinely has that id under a different type -- a real (if narrow)
        recall bug on top of the leak. Pushing the pack predicate ahead of
        ``LIMIT 1`` picks a row already known to be in-scope, so it can
        never reject a row the caller is actually entitled to see.

        Scope-INTERNAL homonym collisions (two DIFFERENT node_types, both
        inside the caller's own readable scope, sharing one node_id) are
        NOT resolved by this predicate -- which of the two rows ``LIMIT 1``
        returns is still arbitrary. Deliberately out of scope for issue
        #147 (a data-integrity question, not a confidentiality one -- see
        issue #147 §8's homonym-limits section); the real fix is giving
        neighbour-traversal a ``(node_type, node_id)`` matching key, tracked
        as a follow-up issue there.

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
                        {"properties": props, "labels": [other_ntype], "relation_type": relation}
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
                    results.append({"properties": props, "labels": [to_type], "relation_type": relation})

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
                        results.append({"properties": props, "labels": [from_type], "relation_type": relation})

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
            {"props": _merge_space(_as_dict(properties), space_id), "labels": [node_type]}
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
            {"props": _merge_space(_as_dict(properties), space_id), "labels": [node_type]}
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

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]]) -> int:
        self._require_available()
        if not nodes:
            return 0
        sql = self._dialect.upsert(
            self._table("graph_nodes"),
            ["node_type", "node_id", "space_id", "properties"],
            conflict_cols=["node_type", "node_id"],
            update_cols=["space_id", "properties"],
            json_columns=["properties"],
        )
        # issue #118: same write shape as upsert_node (a props dict + a
        # separate space_id column) built inline here instead of delegating
        # to upsert_node, so it needs the identical _normalize_space
        # reconciliation applied per node -- otherwise a batch caller could
        # reintroduce the exact divergence upsert_node no longer allows.
        params = []
        for n in nodes:
            props = {**n.get("properties", {}), "id": n["node_id"]}
            props, space_id = _normalize_space(props, n.get("space_id"))
            params.append({
                "node_type": n["node_type"],
                "node_id": n["node_id"],
                "space_id": space_id,
                "properties": json.dumps(props),
            })
        self._exec_write_batch(sql, params)
        return len(params)

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        self._require_available()
        if not edges:
            return 0
        sql = self._dialect.upsert(
            self._table("graph_edges"),
            ["from_type", "from_id", "relation", "to_type", "to_id", "properties"],
            conflict_cols=["from_type", "from_id", "relation", "to_type", "to_id"],
            update_cols=["properties"],
            json_columns=["properties"],
        )
        params = [
            {
                "from_type": e["from_type"],
                "from_id": e["from_id"],
                "relation": e["relation"],
                "to_type": e["to_type"],
                "to_id": e["to_id"],
                "properties": json.dumps(e.get("properties") or {}),
            }
            for e in edges
        ]
        self._exec_write_batch(sql, params)
        return len(params)
