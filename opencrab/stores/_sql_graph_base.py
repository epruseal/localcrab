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
from typing import Any

from opencrab.stores._graph_common import _as_dict, _edge_passes, _merge_space, _node_passes
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
        self, node_id: str, cap: int, out: bool
    ) -> list[tuple[str, str, str, Any]]:
        """Default per-node candidate-edge fetch (one query, LIMIT cap).
        Column order is always (other_type, other_id, relation, properties)
        regardless of direction, so callers never branch on direction to
        read a row."""
        table = self._table("graph_edges")
        if out:
            sql = f"SELECT to_type, to_id, relation, properties FROM {table} WHERE from_id=:nid LIMIT :cap"
        else:
            sql = f"SELECT from_type, from_id, relation, properties FROM {table} WHERE to_id=:nid LIMIT :cap"
        return self._fetch_all(sql, {"nid": node_id, "cap": cap})

    def _prefetch_frontier(
        self, frontier_ids: list[str], cap: int, out: bool
    ) -> dict[str, list[tuple[str, str, str, Any]]]:
        """HOOK — default per-row prefetch (one query per frontier node,
        reproducing LocalGraphStore's historical behavior). PgGraphStore's
        adopter overrides this with a single unnest+LATERAL batch query (see
        module docstring)."""
        return {fid: self._fetch_edges_for_node(fid, cap, out) for fid in frontier_ids}

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
            if pack_set is not None:
                other_pass = _node_passes(other_props, pack_set, include_unpackaged)
                if not other_pass:
                    continue
                edge_props = _as_dict(edge_props_raw)
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
    ) -> list[dict[str, Any]]:
        """BFS neighbour traversal, processed one depth-level at a time (see
        module docstring's "_prefetch_frontier / _batch_node_props HOOKS")."""
        self._require_available()
        pack_set: set[str] | None = set(pack_ids) if pack_ids else None

        if pack_set is not None:
            anchor_props = self._fetch_node_props_by_id(node_id)
            if not _node_passes(anchor_props or {}, pack_set, include_unpackaged):
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
                    out_batch = self._prefetch_frontier(expandable, limit, out=True)
                if direction in ("in", "both"):
                    in_batch = self._prefetch_frontier(expandable, limit, out=False)

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
                            include_unpackaged, props_cache,
                        )

                if direction in ("in", "both"):
                    remaining = limit - len(results)
                    if remaining > 0:
                        self._expand(
                            "in", in_batch, current_id, current_depth, remaining,
                            visited, results, next_level, limit, pack_set,
                            include_unpackaged, props_cache,
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

    def export_nodes(
        self,
        pack_id: str | None = None,
        limit: int = 500_000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        table = self._table("graph_nodes")
        # space_id is selected so _merge_space can restore it into props: this
        # backend keeps space in its own column, but the protocol's export shape
        # carries it inside props (see _merge_space for the measured fallout).
        if pack_id:
            pid = self._dialect.json_get("properties", "pack_id")
            src = self._dialect.json_get("properties", "source")
            src_id = self._dialect.json_get("properties", "source_id")
            sql = (
                f"SELECT node_type, space_id, properties FROM {table}"
                f" WHERE {pid} = :pid OR {src} = :pid OR {src_id} = :pid LIMIT :lim"
            )
            rows = self._fetch_all(sql, {"pid": pack_id, "lim": limit})
        else:
            rows = self._fetch_all(
                f"SELECT node_type, space_id, properties FROM {table} LIMIT :lim",
                {"lim": limit},
            )
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
        params = [
            {
                "node_type": n["node_type"],
                "node_id": n["node_id"],
                "space_id": n.get("space_id"),
                "properties": json.dumps({**n.get("properties", {}), "id": n["node_id"]}),
            }
            for n in nodes
        ]
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
