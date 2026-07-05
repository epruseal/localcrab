"""
Neo4j graph store adapter.

Wraps the official neo4j-python-driver and exposes typed methods for
creating nodes and edges, running Cypher queries, and traversing paths.
All methods gracefully handle connection failures.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


class Neo4jStore:
    """Thread-safe Neo4j adapter using the official driver."""

    def __init__(self, uri: str, user: str, password: str, database: str | None = None) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver: Any = None
        self._available = False
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from neo4j import GraphDatabase  # type: ignore[import]

            from opencrab.common.neo4j_driver import make_driver

            self._driver = make_driver(
                GraphDatabase, self._uri, self._user, self._password
            )
            # Verify connectivity with a lightweight query
            session_kwargs = {"database": self._database} if self._database else {}
            with self._driver.session(**session_kwargs) as session:
                session.run("RETURN 1")
            self._available = True
            if self._database:
                logger.info("Neo4j connected at %s (database=%s)", self._uri, self._database)
            else:
                logger.info("Neo4j connected at %s", self._uri)
        except Exception as exc:
            logger.warning("Neo4j unavailable (%s): %s", self._uri, exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("Neo4j is not available.")

    @contextmanager
    def _session(self) -> Generator[Any, None, None]:
        self._require_available()
        if self._driver is None:
            raise RuntimeError("Neo4j is not available.")
        session_kwargs = {"database": self._database} if self._database else {}
        with self._driver.session(**session_kwargs) as session:
            yield session

    # ------------------------------------------------------------------
    # Schema / constraints
    # ------------------------------------------------------------------

    def ensure_constraints(self) -> None:
        """Create uniqueness constraints for all node types if they don't exist."""
        from opencrab.grammar.manifest import all_node_types

        if not self._available:
            logger.warning("Neo4j unavailable; skipping constraint creation.")
            return

        with self._session() as session:
            for node_type in all_node_types():
                try:
                    session.run(
                        f"CREATE CONSTRAINT IF NOT EXISTS "
                        f"FOR (n:{node_type}) REQUIRE n.id IS UNIQUE"
                    )
                except Exception as exc:
                    logger.debug("Constraint for %s: %s", node_type, exc)

        logger.info("Neo4j constraints ensured.")

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
        """
        Create or update a node. Returns the node's properties.

        Parameters
        ----------
        node_type:
            Label to apply (e.g. "User", "Document").
        node_id:
            Unique identifier for the node.
        properties:
            Additional properties to merge onto the node.
        space_id:
            Optional space tag stored as a property.
        """
        self._require_available()

        props = {**properties, "id": node_id}
        if space_id:
            props["space"] = space_id

        set_clause = ", ".join(f"n.{k} = ${k}" for k in props)
        cypher = f"""
            MERGE (n:OpenCrabNode:{node_type} {{id: $id}})
            SET {set_clause}
            RETURN properties(n) AS props
        """
        with self._session() as session:
            result = session.run(cypher, **props)
            record = result.single()
            return dict(record["props"]) if record else {}

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        """Retrieve a node by type and id."""
        self._require_available()

        cypher = f"MATCH (n:{node_type} {{id: $id}}) RETURN properties(n) AS props"
        with self._session() as session:
            result = session.run(cypher, id=node_id)
            record = result.single()
            return dict(record["props"]) if record else None

    def lookup_node_type(self, node_id: str) -> str | None:
        """Return the first label for a node_id, or None if not found.

        Used by OntologyBuilder to resolve real node types when writing edges,
        so that edges preserve typed labels instead of falling back to a single
        per-space default.
        """
        if not self._available:
            return None
        try:
            with self._session() as session:
                result = session.run(
                    "MATCH (n {id: $id}) RETURN labels(n)[0] AS lbl LIMIT 1",
                    id=node_id,
                )
                record = result.single()
                return record["lbl"] if record and record["lbl"] else None
        except Exception:
            return None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """Delete a node and all its relationships."""
        self._require_available()

        cypher = f"MATCH (n:{node_type} {{id: $id}}) DETACH DELETE n RETURN count(n) AS cnt"
        with self._session() as session:
            result = session.run(cypher, id=node_id)
            record = result.single()
            return bool(record and record["cnt"] > 0)

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
        """
        Create or update a directed relationship between two nodes.

        Returns True on success.
        """
        self._require_available()

        props = properties or {}
        prop_str = ", ".join(f"r.{k} = ${k}" for k in props) if props else "r.created = timestamp()"
        cypher = f"""
            MATCH (a:{from_type} {{id: $from_id}})
            MATCH (b:{to_type} {{id: $to_id}})
            MERGE (a)-[r:{relation}]->(b)
            SET {prop_str}
            RETURN r
        """
        params = {"from_id": from_id, "to_id": to_id, **props}
        with self._session() as session:
            result = session.run(cypher, **params)
            return result.single() is not None

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute arbitrary Cypher and return a list of record dicts."""
        self._require_available()

        with self._session() as session:
            result = session.run(cypher, **(params or {}))
            return [dict(record) for record in result]

    def find_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Find neighboring nodes up to *depth* hops.

        Parameters
        ----------
        direction:
            "out", "in", or "both".
        pack_ids:
            Optional pack_id allow-list. When set, neighbours whose
            ``n.pack_id`` is not in the list (and not NULL, unless
            ``include_unpackaged`` is True) are filtered out. Relationships
            whose own ``r.pack_id`` is set to a foreign value are also
            excluded; relationships with no ``r.pack_id`` are accepted when
            both endpoints survive the node filter.
        include_unpackaged:
            When ``pack_ids`` is set, also allow nodes/edges with no
            ``pack_id`` property (legacy data).
        """
        self._require_available()

        cypher, params = self._build_neighbors_cypher(
            node_id=node_id,
            direction=direction,
            depth=depth,
            limit=limit,
            pack_ids=pack_ids,
            include_unpackaged=include_unpackaged,
        )

        with self._session() as session:
            result = session.run(cypher, **params)
            return [
                {
                    "properties": dict(record["props"]),
                    "labels": list(record["labels"]),
                    "relationship_types": list(record["relationship_types"]),
                    "relation_type": list(record["relationship_types"])[-1]
                    if record["relationship_types"] else "",
                    "depth": record["depth"],
                }
                for record in result
            ]

    @staticmethod
    def _build_neighbors_cypher(
        node_id: str,
        direction: str,
        depth: int,
        limit: int,
        pack_ids: list[str] | None,
        include_unpackaged: bool,
    ) -> tuple[str, dict[str, Any]]:
        """Pure helper that builds the Cypher query string and parameters.

        Separated for unit testing — exercising real Neo4j is out of scope
        for this change; we only verify the generated query structure.
        """
        depth = int(depth)
        arrow = {
            "out": "-[rels*1..{depth}]->",
            "in": "<-[rels*1..{depth}]-",
            "both": "-[rels*1..{depth}]-",
        }.get(direction, "-[rels*1..{depth}]-").format(depth=depth)

        where_clauses: list[str] = []
        params: dict[str, Any] = {"id": node_id, "limit": limit}

        if pack_ids:
            params["pack_ids"] = list(pack_ids)
            if include_unpackaged:
                where_clauses.append(
                    "ALL(n IN nodes(path) WHERE n.pack_id IS NULL OR n.pack_id IN $pack_ids)"
                )
                where_clauses.append(
                    "ALL(r IN relationships(path) WHERE r.pack_id IS NULL OR r.pack_id IN $pack_ids)"
                )
            else:
                where_clauses.append(
                    "ALL(n IN nodes(path) WHERE n.pack_id IN $pack_ids)"
                )
                where_clauses.append(
                    "ALL(r IN relationships(path) WHERE r.pack_id IS NULL OR r.pack_id IN $pack_ids)"
                )

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        cypher = f"""
            MATCH path = (start {{id: $id}}){arrow}(neighbor)
            {where_sql}
            WITH neighbor, labels(neighbor) AS labels, properties(neighbor) AS props,
                 [rel IN relationships(path) | type(rel)] AS relationship_types,
                 length(path) AS depth
            RETURN DISTINCT props, labels, relationship_types, depth
            LIMIT $limit
        """
        return cypher, params

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        """Find shortest path between two nodes by id."""
        self._require_available()

        cypher = f"""
            MATCH path = shortestPath(
                (a {{id: $from_id}})-[*1..{max_depth}]-(b {{id: $to_id}})
            )
            RETURN [node IN nodes(path) | properties(node)] AS node_props,
                   [rel IN relationships(path) | type(rel)] AS rel_types
        """
        with self._session() as session:
            result = session.run(cypher, from_id=from_id, to_id=to_id)
            record = result.single()
            if not record:
                return []
            return [
                {"node": node, "relation": rel}
                for node, rel in zip(record["node_props"], record["rel_types"] + [""])
            ]

    def count_nodes(self, node_type: str | None = None) -> int:
        """Count nodes, optionally filtered by type."""
        self._require_available()

        if node_type:
            cypher = f"MATCH (n:{node_type}) RETURN count(n) AS cnt"
        else:
            cypher = "MATCH (n) RETURN count(n) AS cnt"

        with self._session() as session:
            result = session.run(cypher)
            record = result.single()
            return int(record["cnt"]) if record else 0

    def ping(self) -> bool:
        """Return True if the database is reachable."""
        try:
            with self._session() as session:
                session.run("RETURN 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Extended operations — GraphStoreExtended (opencrab/stores/_graph_protocol.py)
    #
    # These 7 methods mirror what LocalGraphStore/PGGraphStore/KuzuGraphStore
    # already provide. Several were previously inlined as ad-hoc Cypher in
    # consumers (content_pack_list's Neo4j branch, lever_simulate's Neo4j
    # branch, pack/neo4j_export.py's _node_query/_edge_query, ontology_get_node's
    # Neo4j fallback) — the Cypher below is copied from those call sites
    # verbatim (or generalised, for find_by_relations) so switching a consumer
    # from its old inline branch to calling these methods directly produces
    # identical output.
    # ------------------------------------------------------------------

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        """Type-agnostic node lookup by id alone; None if not found.

        Cypher matches ontology_get_node()'s former Neo4j fallback exactly
        (``labels(n)[0]`` for node_type, unfiltered — a node created via
        ``upsert_node``'s ``MERGE (n:OpenCrabNode:{node_type} ...)`` carries
        both labels; this is the same label-ordering behaviour every other
        ``labels(n)[0]`` call site in this codebase already relies on).
        """
        self._require_available()
        cypher = "MATCH (n {id: $id}) RETURN properties(n) AS props, labels(n)[0] AS lbl LIMIT 1"
        with self._session() as session:
            result = session.run(cypher, id=node_id)
            record = result.single()
            if not record:
                return None
            props = dict(record["props"])
            props["node_type"] = record["lbl"]
            return props

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        """Aggregate node counts per pack_id; packs below min_nodes omitted.

        Cypher matches content_pack_list()'s former inline Neo4j branch:
        anchor node (``dataset:{pack_id}``) title takes priority, falling
        back to any node's ``source_package_title``.
        """
        self._require_available()
        cypher = """
            MATCH (n:OpenCrabNode)
            WHERE n.pack_id IS NOT NULL
            WITH n.pack_id AS pack_id, count(n) AS node_count,
                 collect(CASE WHEN n.id = 'dataset:' + n.pack_id THEN n.title ELSE null END) AS anchor_titles,
                 collect(n.source_package_title) AS pkg_titles
            WHERE node_count >= $min_nodes
            WITH pack_id, node_count,
                 coalesce(
                     [t IN anchor_titles WHERE t IS NOT NULL AND t <> ''][0],
                     [t IN pkg_titles  WHERE t IS NOT NULL AND t <> ''][0],
                     ''
                 ) AS sample_title
            RETURN pack_id, node_count, sample_title
            ORDER BY node_count DESC
        """
        with self._session() as session:
            result = session.run(cypher, min_nodes=min_nodes)
            return [dict(record) for record in result]

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Single-hop neighbours filtered to a relation-type allow-list.

        Cypher generalises lever_simulate()'s former inline Neo4j branch
        (``-[r:raises|lowers|stabilizes|optimizes]->``) to an arbitrary
        relation list and direction. Relation types are interpolated
        directly into the pattern (Cypher cannot bind relationship types as
        parameters) — same approach ``upsert_edge`` already uses for a
        single relation type.
        """
        self._require_available()
        if not relations:
            return []

        rel_pattern = "|".join(relations)
        arrow = {
            "out": f"-[r:{rel_pattern}]->",
            "in": f"<-[r:{rel_pattern}]-",
            "both": f"-[r:{rel_pattern}]-",
        }.get(direction, f"-[r:{rel_pattern}]->")

        cypher = f"""
            MATCH (n {{id: $id}}){arrow}(m)
            RETURN properties(m) AS props, labels(m) AS labels, type(r) AS relation_type
            LIMIT $limit
        """
        with self._session() as session:
            result = session.run(cypher, id=node_id, limit=limit)
            return [
                {
                    "properties": dict(record["props"]),
                    "labels": list(record["labels"]),
                    "relation_type": record["relation_type"],
                }
                for record in result
            ]

    def export_nodes(
        self, pack_id: str | None = None, limit: int = 500_000
    ) -> list[dict[str, Any]]:
        """Bulk node export for pack ingest/re-export tooling.

        Cypher is identical to pack/neo4j_export.py's former inline
        ``_node_query()`` helper (pack_id matches ``pack_id``, ``source``,
        or ``source_id``).
        """
        self._require_available()
        cypher = f"""
            MATCH (n)
            WHERE $pack_id IS NULL
               OR n.pack_id = $pack_id OR n.source = $pack_id OR n.source_id = $pack_id
            RETURN properties(n) AS props, labels(n) AS labels
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(cypher, pack_id=pack_id)
            return [
                {"props": dict(record["props"]), "labels": list(record["labels"])}
                for record in result
            ]

    def export_edges(
        self, pack_id: str | None = None, limit: int = 1_000_000
    ) -> list[dict[str, Any]]:
        """Bulk edge export, joined with both endpoints' properties.

        Cypher is identical to pack/neo4j_export.py's former inline
        ``_edge_query()`` helper (pack_id matches either endpoint's
        ``pack_id``/``source``/``source_id``, or the edge's own).
        """
        self._require_available()
        node_filter = (
            "($pack_id IS NULL OR a.pack_id = $pack_id OR a.source = $pack_id OR a.source_id = $pack_id) "
            "OR ($pack_id IS NULL OR b.pack_id = $pack_id OR b.source = $pack_id OR b.source_id = $pack_id) "
            "OR ($pack_id IS NULL OR r.pack_id = $pack_id OR r.source = $pack_id OR r.source_id = $pack_id)"
        )
        cypher = f"""
            MATCH (a)-[r]->(b)
            WHERE {node_filter}
            RETURN properties(a) AS source_props, labels(a) AS source_labels,
                   properties(b) AS target_props, labels(b) AS target_labels,
                   properties(r) AS rel_props, type(r) AS relation
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(cypher, pack_id=pack_id)
            return [
                {
                    "source_props": dict(record["source_props"]),
                    "source_labels": list(record["source_labels"]),
                    "target_props": dict(record["target_props"]),
                    "target_labels": list(record["target_labels"]),
                    "rel_props": dict(record["rel_props"]),
                    "relation": record["relation"],
                }
                for record in result
            ]

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]]) -> int:
        """Bulk upsert; returns the count processed.

        Per-item loop calling ``upsert_node`` — same approach
        ``KuzuGraphStore.upsert_nodes_batch`` uses, since a node's label is
        fixed at Cypher-compile time and a single ``UNWIND`` can't vary the
        label across a mixed-node_type batch without APOC.
        """
        self._require_available()
        for n in nodes:
            self.upsert_node(
                n["node_type"], n["node_id"], n.get("properties", {}), n.get("space_id")
            )
        return len(nodes)

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        """Bulk upsert; returns the count of edges that upserted successfully.

        Per-item loop calling ``upsert_edge`` — mirrors
        ``KuzuGraphStore.upsert_edges_batch``.
        """
        self._require_available()
        count = 0
        for e in edges:
            ok = self.upsert_edge(
                e["from_type"], e["from_id"], e["relation"],
                e["to_type"], e["to_id"], e.get("properties"),
            )
            if ok:
                count += 1
        return count
