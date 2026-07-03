"""
PostgreSQL-backed graph store — PG mode 4-store integration (graph axis).

Contract source: opencrab/stores/local_graph_store.py (SQLite backend). This
module implements the SAME 20 public methods with the SAME signatures and
return shapes so store consumers stay backend-agnostic. Node/edge tables are
1:1 ports of the SQLite schema (TEXT JSON -> JSONB), and query methods
(find_neighbors/find_path/list_packs/find_by_relations/export_*) are ported
line-for-line from the Python/SQLite version, with SQLite-specific syntax
(json_extract, INSERT OR REPLACE, ? placeholders) swapped for PostgreSQL
equivalents (jsonb ->>, INSERT ... ON CONFLICT, :named placeholders).

INTENTIONAL DEVIATION (find_neighbors/find_path traversal strategy):
    The preflight benchmark validated a single recursive-CTE-with-LATERAL
    query for hub fan-out capping (534ms unbounded CTE -> 61ms p95 LATERAL
    CTE). This module instead keeps LocalGraphStore's Python-orchestrated BFS
    loop and swaps sqlite3 calls for equivalent parameterised PG queries,
    because the BFS algorithm's "remaining slot" budget is shared *across*
    both directions and *across* nodes in FIFO order — reproducing that
    exact interleaving declaratively in one SQL statement risks subtle
    ordering/truncation differences from the reference implementation, and
    the task's own acceptance bar is "identical input -> identical output"
    parity against LocalGraphStore. The performance property that actually
    caused the hub blow-up (534ms) is preserved: every per-node fetch is
    still bounded by "LIMIT :remaining" (never fetches a hub's full edge
    list), which is the exact mechanism validated for LocalGraphStore at
    154k/431k scale. Each hop is one localhost round trip; PGGraphStore does
    not claim the identical 61ms p95 figure, but does not re-introduce the
    unbounded-fetch failure mode either.

LIFECYCLE NOTE: close() disposes the engine only when this store created it
    from a DSN string. When an external SQLAlchemy Engine is injected, the
    caller owns its lifecycle and close() is a no-op (unlike
    LocalGraphStore.close(), which always closes its own connections).

QUIRK PRESERVED ON PURPOSE: delete_node()'s return value reflects the
    *edges* DELETE rowcount, not the node DELETE rowcount, because the
    reference implementation reuses one sqlite3 cursor across two execute()
    calls and returns cur.rowcount after the *last* statement. See
    delete_node() docstring.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _node_pack_id(props: dict[str, Any]) -> str | None:
    pid = props.get("pack_id") if isinstance(props, dict) else None
    if pid:
        return str(pid)
    return None


def _node_passes(
    props: dict[str, Any],
    pack_set: set[str] | None,
    include_unpackaged: bool,
) -> bool:
    if not pack_set:
        return True
    pid = _node_pack_id(props)
    if pid is None:
        return include_unpackaged
    return pid in pack_set


def _edge_passes(
    edge_props: dict[str, Any],
    src_passes: bool,
    dst_passes: bool,
    pack_set: set[str] | None,
) -> bool:
    if not pack_set:
        return True
    edge_pid = _node_pack_id(edge_props) if isinstance(edge_props, dict) else None
    if edge_pid is not None:
        if edge_pid not in pack_set:
            return False
        return src_passes and dst_passes
    return src_passes and dst_passes


def _as_dict(value: Any) -> dict[str, Any]:
    """psycopg2 auto-decodes JSONB into dict/list; tolerate str/None too."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


_DDL_TEMPLATE = [
    """
    CREATE TABLE IF NOT EXISTS {schema}.graph_nodes (
        node_type   TEXT NOT NULL,
        node_id     TEXT NOT NULL,
        space_id    TEXT,
        properties  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        PRIMARY KEY (node_type, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.graph_edges (
        from_type   TEXT NOT NULL,
        from_id     TEXT NOT NULL,
        relation    TEXT NOT NULL,
        to_type     TEXT NOT NULL,
        to_id       TEXT NOT NULL,
        properties  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        PRIMARY KEY (from_type, from_id, relation, to_type, to_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edges_from ON {schema}.graph_edges(from_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_to   ON {schema}.graph_edges(to_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_pack ON {schema}.graph_nodes((properties->>'pack_id'))",
]


class PGGraphStore:
    """PostgreSQL-backed graph store with the same interface as LocalGraphStore."""

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
    # Lifecycle
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            with self._engine.begin() as conn:
                if self._schema != "public":
                    conn.execute(self._text(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'))
                for ddl in _DDL_TEMPLATE:
                    conn.execute(self._text(ddl.format(schema=f'"{self._schema}"')))
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

    def ensure_constraints(self) -> None:
        pass  # PRIMARY KEY constraints cover uniqueness

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _t(self) -> str:
        return f'"{self._schema}"'

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("PGGraphStore is not available.")

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
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.graph_nodes(node_type, node_id, space_id, properties)
                    VALUES (:node_type, :node_id, :space_id, CAST(:properties AS jsonb))
                    ON CONFLICT (node_type, node_id) DO UPDATE SET
                        space_id   = EXCLUDED.space_id,
                        properties = EXCLUDED.properties
                    """
                ),
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
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    f"SELECT properties FROM {self._t}.graph_nodes"
                    " WHERE node_type=:node_type AND node_id=:node_id"
                ),
                {"node_type": node_type, "node_id": node_id},
            ).fetchone()
        return _as_dict(row[0]) if row else None

    def lookup_node_type(self, node_id: str) -> str | None:
        if not self._available:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    f"SELECT node_type FROM {self._t}.graph_nodes"
                    " WHERE node_id=:node_id LIMIT 1"
                ),
                {"node_id": node_id},
            ).fetchone()
        return row[0] if row else None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """Delete a node and its incident edges.

        QUIRK PRESERVED: mirrors LocalGraphStore, which reuses one sqlite3
        cursor for two DELETE statements and returns cur.rowcount from the
        LAST execute() (the edges delete), NOT the node delete. So this
        returns True iff at least one incident EDGE was removed — a node
        with no edges deletes silently but returns False. Replicated here by
        running both statements on one connection/transaction and returning
        the edges-DELETE rowcount.
        """
        self._require_available()
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    f"DELETE FROM {self._t}.graph_nodes WHERE node_type=:nt AND node_id=:nid"
                ),
                {"nt": node_type, "nid": node_id},
            )
            result = conn.execute(
                self._text(
                    f"""
                    DELETE FROM {self._t}.graph_edges
                    WHERE (from_type=:nt AND from_id=:nid) OR (to_type=:nt AND to_id=:nid)
                    """
                ),
                {"nt": node_type, "nid": node_id},
            )
            rowcount = result.rowcount
        return rowcount > 0

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
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.graph_edges(from_type, from_id, relation, to_type, to_id, properties)
                    VALUES (:from_type, :from_id, :relation, :to_type, :to_id, CAST(:properties AS jsonb))
                    ON CONFLICT (from_type, from_id, relation, to_type, to_id) DO UPDATE SET
                        properties = EXCLUDED.properties
                    """
                ),
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
        """Not supported in PG mode — returns empty list with a warning (matches local mode)."""
        logger.warning("run_cypher() is not supported in local mode; returning [].")
        return []

    def _fetch_node_props(self, conn: Any, node_type: str, node_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            self._text(
                f"SELECT properties FROM {self._t}.graph_nodes"
                " WHERE node_type=:nt AND node_id=:nid"
            ),
            {"nt": node_type, "nid": node_id},
        ).fetchone()
        return _as_dict(row[0]) if row else None

    def _fetch_node_props_by_id(self, conn: Any, node_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            self._text(
                f"SELECT properties FROM {self._t}.graph_nodes WHERE node_id=:nid LIMIT 1"
            ),
            {"nid": node_id},
        ).fetchone()
        return _as_dict(row[0]) if row else None

    def find_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
    ) -> list[dict[str, Any]]:
        """BFS neighbour traversal — Python-orchestrated port of LocalGraphStore.

        Same algorithm, same "remaining slot" fan-out cap per direction per
        node (SQL LIMIT :remaining), same pack-filter 3-rule policy. See
        module docstring for why this stays Python-orchestrated rather than
        a single recursive CTE.
        """
        self._require_available()
        pack_set: set[str] | None = set(pack_ids) if pack_ids else None

        with self._engine.connect() as conn:
            if pack_set is not None:
                anchor_props = self._fetch_node_props_by_id(conn, node_id)
                if not _node_passes(anchor_props or {}, pack_set, include_unpackaged):
                    return []

            visited: set[str] = {node_id}
            queue: deque[tuple[str, int]] = deque([(node_id, 0)])
            results: list[dict[str, Any]] = []

            while queue and len(results) < limit:
                current_id, current_depth = queue.popleft()
                if current_depth >= depth:
                    continue

                if direction in ("out", "both"):
                    remaining = limit - len(results)
                    if remaining > 0:
                        rows = conn.execute(
                            self._text(
                                f"""
                                SELECT to_type, to_id, relation, properties
                                FROM {self._t}.graph_edges WHERE from_id=:fid LIMIT :lim
                                """
                            ),
                            {"fid": current_id, "lim": remaining},
                        ).fetchall()
                        for row in rows:
                            if len(results) >= limit:
                                break
                            to_type, to_id, relation, edge_props_raw = row
                            if to_id in visited:
                                continue
                            dst_props = self._fetch_node_props(conn, to_type, to_id)
                            if not dst_props:
                                continue
                            if pack_set is not None:
                                dst_pass = _node_passes(dst_props, pack_set, include_unpackaged)
                                if not dst_pass:
                                    continue
                                edge_props = _as_dict(edge_props_raw)
                                if not _edge_passes(edge_props, True, dst_pass, pack_set):
                                    continue
                            visited.add(to_id)
                            results.append({
                                "properties": dst_props,
                                "labels": [to_type],
                                "relation_type": relation,
                                "relationship_types": [relation],
                                "depth": current_depth + 1,
                            })
                            queue.append((to_id, current_depth + 1))

                if direction in ("in", "both"):
                    remaining = limit - len(results)
                    if remaining > 0:
                        rows = conn.execute(
                            self._text(
                                f"""
                                SELECT from_type, from_id, relation, properties
                                FROM {self._t}.graph_edges WHERE to_id=:tid LIMIT :lim
                                """
                            ),
                            {"tid": current_id, "lim": remaining},
                        ).fetchall()
                        for row in rows:
                            if len(results) >= limit:
                                break
                            from_type, from_id, relation, edge_props_raw = row
                            if from_id in visited:
                                continue
                            src_props = self._fetch_node_props(conn, from_type, from_id)
                            if not src_props:
                                continue
                            if pack_set is not None:
                                src_pass = _node_passes(src_props, pack_set, include_unpackaged)
                                if not src_pass:
                                    continue
                                edge_props = _as_dict(edge_props_raw)
                                if not _edge_passes(edge_props, src_pass, True, pack_set):
                                    continue
                            visited.add(from_id)
                            results.append({
                                "properties": src_props,
                                "labels": [from_type],
                                "relation_type": relation,
                                "relationship_types": [relation],
                                "depth": current_depth + 1,
                            })
                            queue.append((from_id, current_depth + 1))

        return results[:limit]

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        """BFS shortest path between two nodes — port of LocalGraphStore.find_path."""
        self._require_available()

        with self._engine.connect() as conn:
            visited: set[str] = {from_id}
            queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_id, [])])

            while queue:
                current_id, path = queue.popleft()
                if len(path) >= max_depth * 2:
                    continue

                rows = conn.execute(
                    self._text(
                        f"SELECT to_type, to_id, relation FROM {self._t}.graph_edges WHERE from_id=:fid"
                    ),
                    {"fid": current_id},
                ).fetchall()
                for to_type, nid, rel in rows:
                    node = self._fetch_node_props(conn, to_type, nid) or {"id": nid}
                    new_path = path + [{"node": node, "relation": rel}]

                    if nid == to_id:
                        return new_path

                    if nid not in visited:
                        visited.add(nid)
                        queue.append((nid, new_path))

        return []

    def count_nodes(self, node_type: str | None = None) -> int:
        self._require_available()
        with self._engine.connect() as conn:
            if node_type:
                row = conn.execute(
                    self._text(
                        f"SELECT COUNT(*) FROM {self._t}.graph_nodes WHERE node_type=:nt"
                    ),
                    {"nt": node_type},
                ).fetchone()
            else:
                row = conn.execute(
                    self._text(f"SELECT COUNT(*) FROM {self._t}.graph_nodes")
                ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Extended operations
    # ------------------------------------------------------------------

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        self._require_available()
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._text(
                    f"""
                    SELECT
                        properties->>'pack_id' AS pack_id,
                        COUNT(*) AS node_count,
                        COALESCE(
                            MAX(CASE
                                WHEN node_id = 'dataset:' || (properties->>'pack_id')
                                THEN properties->>'title'
                            END),
                            MAX(properties->>'source_package_title'),
                            ''
                        ) AS sample_title
                    FROM {self._t}.graph_nodes
                    WHERE properties->>'pack_id' IS NOT NULL
                    GROUP BY properties->>'pack_id'
                    HAVING COUNT(*) >= :min_nodes
                    ORDER BY COUNT(*) DESC
                    """
                ),
                {"min_nodes": min_nodes},
            ).fetchall()
        return [
            {"pack_id": r[0], "node_count": r[1], "sample_title": r[2]} for r in rows
        ]

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

        from sqlalchemy import bindparam

        results: list[dict[str, Any]] = []

        with self._engine.connect() as conn:
            if direction in ("out", "both"):
                rows = conn.execute(
                    self._text(
                        f"""
                        SELECT to_type, to_id, relation FROM {self._t}.graph_edges
                        WHERE from_id=:nid AND relation IN :rels LIMIT :lim
                        """
                    ).bindparams(bindparam("rels", expanding=True)),
                    {"nid": node_id, "rels": relations, "lim": limit},
                ).fetchall()
                for to_type, to_id, relation in rows:
                    props = self._fetch_node_props(conn, to_type, to_id)
                    if props:
                        results.append({
                            "properties": props,
                            "labels": [to_type],
                            "relation_type": relation,
                        })

            if direction in ("in", "both"):
                remaining = limit - len(results)
                if remaining > 0:
                    rows = conn.execute(
                        self._text(
                            f"""
                            SELECT from_type, from_id, relation FROM {self._t}.graph_edges
                            WHERE to_id=:nid AND relation IN :rels LIMIT :lim
                            """
                        ).bindparams(bindparam("rels", expanding=True)),
                        {"nid": node_id, "rels": relations, "lim": remaining},
                    ).fetchall()
                    for from_type, from_id, relation in rows:
                        props = self._fetch_node_props(conn, from_type, from_id)
                        if props:
                            results.append({
                                "properties": props,
                                "labels": [from_type],
                                "relation_type": relation,
                            })

        return results

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        with self._engine.connect() as conn:
            row = conn.execute(
                self._text(
                    f"SELECT node_type, properties FROM {self._t}.graph_nodes"
                    " WHERE node_id=:nid LIMIT 1"
                ),
                {"nid": node_id},
            ).fetchone()
        if not row:
            return None
        props = _as_dict(row[1])
        props["node_type"] = row[0]
        return props

    def export_nodes(
        self,
        pack_id: str | None = None,
        limit: int = 500_000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        with self._engine.connect() as conn:
            if pack_id:
                rows = conn.execute(
                    self._text(
                        f"""
                        SELECT node_type, properties FROM {self._t}.graph_nodes
                        WHERE properties->>'pack_id' = :pid
                           OR properties->>'source'   = :pid
                           OR properties->>'source_id' = :pid
                        LIMIT :lim
                        """
                    ),
                    {"pid": pack_id, "lim": limit},
                ).fetchall()
            else:
                rows = conn.execute(
                    self._text(f"SELECT node_type, properties FROM {self._t}.graph_nodes LIMIT :lim"),
                    {"lim": limit},
                ).fetchall()
        return [{"props": _as_dict(r[1]), "labels": [r[0]]} for r in rows]

    def export_edges(
        self,
        pack_id: str | None = None,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        with self._engine.connect() as conn:
            if pack_id:
                rows = conn.execute(
                    self._text(
                        f"""
                        SELECT
                            a.node_type AS from_type, a.properties AS source_props,
                            b.node_type AS to_type,   b.properties AS target_props,
                            e.properties AS rel_props, e.relation
                        FROM {self._t}.graph_edges e
                        JOIN {self._t}.graph_nodes a ON e.from_type=a.node_type AND e.from_id=a.node_id
                        JOIN {self._t}.graph_nodes b ON e.to_type=b.node_type   AND e.to_id=b.node_id
                        WHERE a.properties->>'pack_id' = :pid
                           OR a.properties->>'source'   = :pid
                           OR b.properties->>'pack_id' = :pid
                           OR b.properties->>'source'   = :pid
                           OR e.properties->>'pack_id' = :pid
                        LIMIT :lim
                        """
                    ),
                    {"pid": pack_id, "lim": limit},
                ).fetchall()
            else:
                rows = conn.execute(
                    self._text(
                        f"""
                        SELECT
                            a.node_type AS from_type, a.properties AS source_props,
                            b.node_type AS to_type,   b.properties AS target_props,
                            e.properties AS rel_props, e.relation
                        FROM {self._t}.graph_edges e
                        JOIN {self._t}.graph_nodes a ON e.from_type=a.node_type AND e.from_id=a.node_id
                        JOIN {self._t}.graph_nodes b ON e.to_type=b.node_type   AND e.to_id=b.node_id
                        LIMIT :lim
                        """
                    ),
                    {"lim": limit},
                ).fetchall()
        return [
            {
                "source_props": _as_dict(r[1]),
                "source_labels": [r[0]],
                "target_props": _as_dict(r[3]),
                "target_labels": [r[2]],
                "rel_props": _as_dict(r[4]),
                "relation": r[5],
            }
            for r in rows
        ]

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]]) -> int:
        self._require_available()
        if not nodes:
            return 0
        params = [
            {
                "node_type": n["node_type"],
                "node_id": n["node_id"],
                "space_id": n.get("space_id"),
                "properties": json.dumps({**n.get("properties", {}), "id": n["node_id"]}),
            }
            for n in nodes
        ]
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.graph_nodes(node_type, node_id, space_id, properties)
                    VALUES (:node_type, :node_id, :space_id, CAST(:properties AS jsonb))
                    ON CONFLICT (node_type, node_id) DO UPDATE SET
                        space_id   = EXCLUDED.space_id,
                        properties = EXCLUDED.properties
                    """
                ),
                params,
            )
        return len(params)

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        self._require_available()
        if not edges:
            return 0
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
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.graph_edges(from_type, from_id, relation, to_type, to_id, properties)
                    VALUES (:from_type, :from_id, :relation, :to_type, :to_id, CAST(:properties AS jsonb))
                    ON CONFLICT (from_type, from_id, relation, to_type, to_id) DO UPDATE SET
                        properties = EXCLUDED.properties
                    """
                ),
                params,
            )
        return len(params)
