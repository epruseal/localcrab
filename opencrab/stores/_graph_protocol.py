"""
GraphStore Protocol — the uniform graph-store interface implemented by
LocalGraphStore, PGGraphStore, KuzuGraphStore, and (partially) Neo4jStore.

Consumers historically decided which methods a graph store supports by
``isinstance(store, (LocalGraphStore, KuzuGraphStore))`` (e.g.
opencrab/mcp/tools.py:1064, opencrab/ontology/impact.py:124,263,
opencrab/pack/neo4j_export.py:231) or ``hasattr(store, "...")``
(opencrab/mcp/tools.py:1577,1580,1611,1661,1667). Both patterns exist only
because Neo4jStore lacks several methods the other three backends share —
this module names that shared surface as a single ``typing.Protocol`` so new
consumer code can branch on capability (``isinstance(store, GraphStore)`` or
a plain ``hasattr``) without enumerating concrete classes.

This module is DECLARATION ONLY: it defines the Protocol and documents each
method's contract (params, return shape) against the reference
implementations (LocalGraphStore / PGGraphStore, which are line-for-line
ports of each other — see pg_graph_store.py's module docstring). It does not
implement, patch, or monkeypatch anything on the four store classes.

GAP TABLE — method presence per backend (checked against the current source
of each store module):

    method               Local   PG      Kuzu    Neo4j
    -------------------- ------- ------- ------- -------
    available            yes     yes     yes     yes
    ping                  yes     yes     yes     yes
    close                 yes     yes     yes     yes
    ensure_constraints    yes     yes     yes     yes
    upsert_node           yes     yes     yes     yes
    get_node              yes     yes     yes     yes
    lookup_node_type      yes     yes     yes     yes
    delete_node           yes     yes     yes     yes
    upsert_edge           yes     yes     yes     yes
    run_cypher            yes     yes     yes     yes
    find_neighbors        yes     yes     yes     yes
    find_path             yes     yes     yes     yes
    count_nodes           yes     yes     yes     yes
    -------------------- ------- ------- ------- -------
    get_node_by_id        yes     yes     yes     NO
    list_packs            yes     yes     yes     NO
    find_by_relations     yes     yes     yes     NO
    export_nodes          yes     yes     yes     NO
    export_edges          yes     yes     yes     NO
    upsert_nodes_batch    yes     yes     yes     NO
    upsert_edges_batch    yes     yes     yes     NO

Neo4j is missing exactly 7 methods (the "extended" block above) — this is
D3's worklist for Stage 4's R5 leg. Note ``get_node_by_id`` is grouped with
the extended/missing block, NOT the always-present block: Neo4jStore has no
such method (its callers fall back to a type-agnostic ``run_cypher`` MATCH
instead — see opencrab/mcp/tools.py:ontology_get_node and
opencrab/ontology/impact.py's ``_is_local`` branch).

``@runtime_checkable`` is set so ``isinstance(store, GraphStore)`` works as
a drop-in replacement for the ``isinstance(store, (LocalGraphStore,
KuzuGraphStore, PGGraphStore))`` tuple checks above — note that until D3
closes the gap, Neo4jStore will NOT satisfy this Protocol (isinstance check
fails, since 7 required methods are absent), so ``GraphStoreExtended``
below is deliberately split out as a separate Protocol consumers can check
against independently of the base ``GraphStore``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GraphStore(Protocol):
    """Core graph-store surface implemented by all four backends today."""

    @property
    def available(self) -> bool:
        """True once the backend finished its connection/schema bootstrap.

        Methods below raise RuntimeError (via each backend's
        ``_require_available``) when called while ``available`` is False,
        except where individually noted otherwise (``ping``,
        ``lookup_node_type``, ``ensure_constraints`` degrade instead of
        raising).
        """
        ...

    def ping(self) -> bool:
        """Return True iff a trivial round-trip query succeeds right now.

        Never raises — swallows any backend exception and returns False.
        """
        ...

    def close(self) -> None:
        """Release backend-owned resources (connections/driver/engine).

        Idempotent; safe to call multiple times. PGGraphStore is a no-op
        when it was handed an externally-owned SQLAlchemy Engine rather than
        a DSN string (see its LIFECYCLE NOTE).
        """
        ...

    def ensure_constraints(self) -> None:
        """Create backend-native uniqueness constraints for all node types.

        No-op for the three non-Neo4j backends (their primary keys already
        cover uniqueness). Degrades to a warning log (does not raise) when
        the store is unavailable.
        """
        ...

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
        space_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one node; returns its stored properties dict.

        The returned dict always contains at least ``{"id": node_id, **properties}``
        (Neo4j additionally injects ``"space": space_id`` into the same dict
        when ``space_id`` is given — the other backends store ``space_id`` in
        a separate column and do not merge it into the properties dict).
        Raises RuntimeError if the store is unavailable.
        """
        ...

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        """Fetch one node's properties by (node_type, node_id); None if absent.

        Requires the exact type — use ``get_node_by_id`` (extended surface)
        for a type-agnostic lookup by id alone.
        """
        ...

    def lookup_node_type(self, node_id: str) -> str | None:
        """Best-effort node_type resolution by id alone; None if not found.

        Deliberately lenient: returns None (never raises) even when the
        store is unavailable — used by OntologyBuilder.add_edge to resolve
        real node types for edge endpoints.
        """
        ...

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """Delete one node and its incident edges.

        Returns True iff the node itself was deleted (i.e. it existed
        before the call) — unified across all four backends. A node with
        zero incident edges still returns True. A nonexistent node returns
        False.
        """
        ...

    def upsert_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
    ) -> bool:
        """Create or update a directed (from)->(to) edge; True on success.

        Raises RuntimeError if the store is unavailable. Endpoints must
        already exist (Neo4j: MATCH fails silently to no record -> False;
        SQL backends: FK-less schema, so a dangling edge is written and
        later reads of a missing endpoint return None as its properties).
        """
        ...

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute arbitrary Cypher; each result record as one dict.

        Only Neo4jStore and KuzuGraphStore actually run the query.
        LocalGraphStore and PGGraphStore always return ``[]`` with a
        logger.warning — this method is NOT a reliable capability probe;
        use the extended-surface methods (``list_packs``, ``export_nodes``,
        ``find_by_relations``, ...) as the SQL-backend-native replacements
        for the Cypher queries those two backends can't run.
        """
        ...

    def find_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
    ) -> list[dict[str, Any]]:
        """BFS neighbour traversal up to ``depth`` hops from ``node_id``.

        Parameters
        ----------
        direction: "out", "in", or "both".
        depth: max hop count (0 returns no neighbours).
        limit: max results returned; also caps per-node/per-direction
            fan-out during traversal (a "remaining slot" budget — see
            local_graph_store.find_neighbors's docstring for the hub
            fan-out performance rationale).
        pack_ids / include_unpackaged: the shared 3-rule pack filter policy
            (see opencrab/stores/_graph_common.py's ``_node_passes`` /
            ``_edge_passes``): nodes outside ``pack_ids`` (or unpackaged,
            unless ``include_unpackaged``) are dropped and do not expand
            traversal; an edge with a foreign ``pack_id`` is always
            dropped; an edge with no ``pack_id`` survives only when both
            endpoints pass the node filter. The anchor itself must also
            pass, or the whole call returns ``[]``.

        Returns a list of dicts, each shaped:
            {"properties": dict, "labels": [str], "relation_type": str,
             "relationship_types": [str], "depth": int}
        Ordering is backend-native (SQLite/PG: edge-table insertion/scan
        order; Neo4j: engine-internal index order) — not guaranteed
        identical across backends when a LIMIT truncates a high-degree hub.
        Unknown ``node_id``: returns ``[]`` (there is nothing to expand from;
        this is NOT an error condition on any backend).
        """
        ...

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        """Shortest path from ``from_id`` to ``to_id``; ``[]`` if none found.

        ``max_depth`` is the maximum number of HOPS (edges traversed), and
        traversal follows only OUTGOING edges (``from_id`` -> ... ->
        ``to_id``) — unified across all four backends. A path requiring
        more than ``max_depth`` hops is not found, and a reverse-only edge
        (b->a) does not make ``a`` reachable from ``a`` to ``b``.

        Returns a list of hop dicts: ``[{"node": dict, "relation": str}, ...]``,
        one entry per edge traversed (the ``from_id`` node itself is never
        included as an entry; the final entry's ``"node"`` is ``to_id``).
        Neo4j's last hop uses ``""`` as a sentinel relation on ``find_path``'s
        implicit trailing entry — the three non-Neo4j ports do not add a
        trailing sentinel (their last entry's relation is a real edge type).
        """
        ...

    def count_nodes(self, node_type: str | None = None) -> int:
        """Count nodes, optionally filtered by exact node_type; 0 if empty."""
        ...


@runtime_checkable
class GraphStoreExtended(Protocol):
    """The 7 methods LocalGraphStore/PGGraphStore/KuzuGraphStore share that
    Neo4jStore currently lacks (D3's Stage-4 R5 worklist).

    Split into its own Protocol (rather than folded into ``GraphStore``)
    because until D3 implements these on Neo4jStore, no Neo4j instance can
    satisfy them — keeping them separate lets consumer code do:

        if isinstance(store, GraphStoreExtended):
            ...  # SQL-native fast path (Local/PG/Kuzu)
        else:
            ...  # Cypher fallback (Neo4j today)

    which is exactly the branch every hasattr/isinstance call site in
    mcp/tools.py, ontology/impact.py, and pack/neo4j_export.py already
    performs by hand.
    """

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        """Type-agnostic node lookup by id alone; None if not found.

        Returns the node's properties dict with ``"node_type"`` merged in
        (e.g. ``{"id": ..., "node_type": "Lever", ...}``). Neo4j callers use
        a Cypher ``MATCH (n {id: $id}) RETURN properties(n), labels(n)[0]``
        fallback for this today (see opencrab/mcp/tools.py:ontology_get_node).
        """
        ...

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        """Aggregate node counts per ``pack_id``; packs below ``min_nodes`` omitted.

        Returns ``[{"pack_id": str, "node_count": int, "sample_title": str}, ...]``
        ordered by node_count descending. ``sample_title`` prefers the
        pack_create anchor node's title, then any node's
        ``source_package_title``, else ``""``.
        """
        ...

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Single-hop neighbours filtered to a relation-type allow-list.

        Unlike ``find_neighbors``, this is depth-1 only with no pack
        filter, but supports narrowing to specific relation types (e.g.
        lever_simulate's ``["raises", "lowers", "stabilizes", "optimizes"]``).
        Returns ``[]`` immediately when ``relations`` is empty. Result shape:
            {"properties": dict, "labels": [str], "relation_type": str}
        """
        ...

    def export_nodes(
        self, pack_id: str | None = None, limit: int = 500_000
    ) -> list[dict[str, Any]]:
        """Bulk node export for pack ingest/re-export tooling.

        ``pack_id=None`` exports everything (up to ``limit``); otherwise
        matches a node whose ``pack_id``, ``source``, or ``source_id``
        property equals ``pack_id``. Result shape:
            {"props": dict, "labels": [str]}
        (the shape ``_normalise_node()`` in opencrab/pack/neo4j_export.py
        consumes).
        """
        ...

    def export_edges(
        self, pack_id: str | None = None, limit: int = 1_000_000
    ) -> list[dict[str, Any]]:
        """Bulk edge export, joined with both endpoints' properties.

        ``pack_id`` matches if either endpoint's ``pack_id``/``source`` OR
        the edge's own ``pack_id`` equals it. Result shape:
            {"source_props": dict, "source_labels": [str],
             "target_props": dict, "target_labels": [str],
             "rel_props": dict, "relation": str}
        (the shape ``_normalise_edge()`` in opencrab/pack/neo4j_export.py
        consumes).
        """
        ...

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]]) -> int:
        """Bulk upsert; returns the count processed (``len(nodes)``, or 0
        for the given empty-list edge case — PG/Local short-circuit before
        touching the DB when ``nodes`` is empty).

        Each item: ``{"node_type": str, "node_id": str,
        "properties": dict, "space_id": str | None}``. Faster than N calls
        to ``upsert_node`` (single transaction/commit for the whole batch on
        Local/PG; Kuzu's port is currently a per-item loop calling
        ``upsert_node`` — same result, no batching speedup yet).
        """
        ...

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        """Bulk upsert; returns the count processed.

        Each item: ``{"from_type": str, "from_id": str, "relation": str,
        "to_type": str, "to_id": str, "properties": dict | None}``. On
        Local/PG this is ``len(edges)`` (or 0 for empty input) since every
        row in one executemany/INSERT batch is assumed to succeed; Kuzu's
        port loops calling ``upsert_edge`` per item and only counts the
        ones that returned True.
        """
        ...
