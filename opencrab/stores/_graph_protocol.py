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
    get_edge              yes     yes     yes     yes
    run_cypher            yes     yes     yes     yes
    find_neighbors        yes     yes     yes     yes
    find_path             yes     yes     yes     yes
    count_nodes           yes     yes     yes     yes
    -------------------- ------- ------- ------- -------
    get_node_by_id        yes     yes     yes     yes
    list_packs            yes     yes     yes     yes
    find_by_relations     yes     yes     yes     yes
    export_nodes          yes     yes     yes     yes
    count_exported_nodes  yes     yes     yes     yes
    search_nodes          yes     yes     yes     no
    export_edges          yes     yes     yes     yes
    upsert_nodes_batch    yes     yes     yes     yes
    upsert_edges_batch    yes     yes     yes     yes

``search_nodes`` (issue #86) is the one row above that is genuinely "no" for
Neo4j, not stale -- ``HybridQuery.keyword_search`` never needed a
Neo4jStore.search_nodes because its Cypher ``CONTAINS`` branch already
pushes the same keyword/space predicate straight into Cypher without going
through a store method (see query.py's isinstance branch, which routes
Local/PG/Kuzu to ``search_nodes`` and leaves Neo4j on that pre-existing
Cypher path).

CORRECTION (issue #54 audit finding [5], verified by grepping each `def` in
neo4j_store.py): this table previously marked all 7 "extended" methods NO
for Neo4j and claimed "Neo4j is missing exactly 7 methods" — stale relative
to the source (already flagged separately as issue #64's D-3, since
Neo4jStore has implemented every one of them for a while). Corrected here
because #54's own changes added a new method (``count_exported_nodes``) and
extended an existing one (``export_nodes``) on Neo4jStore, which would have
made the table's staleness worse if left uncorrected. Note this table only
tracks *method presence*; whether ``isinstance(store, GraphStore)`` /
``isinstance(store, GraphStoreExtended)`` actually evaluates True for
Neo4jStore today (the runtime-checkable behavior implied by the surrounding
module docstring, e.g. mcp/tools.py's and ontology/impact.py's isinstance
branches) is unverified here and is #64's scope, not #54's — do not treat
this table's "yes" as a claim about those call sites' current behavior.

``@runtime_checkable`` is set so ``isinstance(store, GraphStore)`` works as
a drop-in replacement for the ``isinstance(store, (LocalGraphStore,
KuzuGraphStore, PGGraphStore))`` tuple checks above; ``GraphStoreExtended``
below is a separate Protocol consumers can check against independently of
the base ``GraphStore`` (see #64 for whether that split is still warranted
now that method presence is at parity).
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

    def get_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
    ) -> dict[str, Any] | None:
        """Fetch one edge's properties by THIS backend's own upsert conflict
        key; None if absent.

        There is no single-edge read among ``upsert_edge``/``export_edges``/
        ``upsert_edges_batch`` — ``export_edges`` is a full scan and
        ``find_neighbors`` truncates via ``limit``, neither an exact probe.
        This method exists for #146's identity-ownership check (a probe read
        before a write), not general traversal.

        The contract is deliberately "match THIS backend's own conflict
        key", not a uniform 5-argument predicate: SQL backends (Local/PG)
        and Neo4j key an edge on all 5 arguments (matching ``upsert_edge``'s
        ``conflict_cols``/MATCH-by-type), but ``KuzuGraphStore``'s
        ``upsert_edge`` MERGEs on ``(from_id, relation, to_id)`` alone (no
        type predicate — Kuzu's ``OntologyEdge`` has no type column). Kuzu's
        ``get_edge`` therefore accepts ``from_type``/``to_type`` for
        signature parity with the other three backends but does not use
        them in its MATCH; passing the wrong type there still finds the
        same edge ``upsert_edge`` would have upserted.

        Always returns a parsed ``dict``, never a raw JSON blob — the
        properties column/property is JSON-serialized text on Local/PG/Kuzu
        (SQLite/Kuzu store JSON strings; see each backend's ``upsert_edge``)
        and this method decodes it before returning, the same way
        ``get_node`` does for node properties.
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
        spaces: list[str] | None = None,
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
        spaces: optional space allow-list (issue #52). Strict membership —
            unlike ``pack_ids`` there is no "include unspaced" mode. Every
            node visited (anchor included) must have its space in this
            list, or the whole call returns ``[]``. Pushed into the
            store's native query ahead of any LIMIT on every backend
            (``space``/``space_id`` is a real column/property everywhere,
            unlike ``pack_id`` which is JSON-blob-only on Kuzu).

        Returns a list of dicts, each shaped:
            {"properties": dict, "labels": [str], "relation_type": str,
             "relationship_types": [str], "depth": int,
             "from_id": str, "to_id": str}
        ``from_id``/``to_id`` are the canonical endpoints of the edge that
        reached this neighbour, in true edge direction (so with
        ``direction="both"`` the anchor may be either side, and at depth > 1
        neither endpoint is the anchor). Optional: Neo4jStore does not emit
        them yet, so consumers must treat their absence as "unknown", never as
        "the anchor is the source".
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

    def list_pack_ids(self) -> set[str]:
        """Every distinct pack_id present on graph data.

        Deliberately NOT ``{r["pack_id"] for r in list_packs(0)}``. That
        aggregation exists to report per-pack node counts and titles for a
        UI, and each backend narrows it accordingly -- Neo4j to the
        ``OpenCrabNode`` label, which the documented
        ``scripts/import_pack_graph_to_neo4j.py`` path does not apply (it
        MERGEs each node under its own domain label). Reusing it to answer
        "which packs exist here" therefore under-reports, and the one caller
        that asks that question is #147's startup reconciliation: an
        under-report there means the guard stays silent while scoped reads
        hide the pack.

        Uses the same truthiness rule as ``_graph_common._node_pack_id`` and
        ``pack_provenance.scope_pack_id`` -- ``""``/``0``/``false`` are "no
        pack_id" -- so the set returned here is exactly the set of packs
        that scoped reads can resolve.
        """
        ...

    def count_nodes(self, node_type: str | None = None) -> int:
        """Count nodes, optionally filtered by exact node_type; 0 if empty."""
        ...


@runtime_checkable
class GraphStoreExtended(Protocol):
    """The 7 methods LocalGraphStore/PGGraphStore/KuzuGraphStore share that
    Neo4jStore currently lacks (D3's Stage-4 R5 worklist).

    ``search_nodes`` (issue #86, see the GAP TABLE above) is deliberately
    NOT declared as an 8th member here, even though it fits this Protocol's
    "Local/PG/Kuzu have it, Neo4j doesn't" shape: ``test_graph_protocol_
    contract.py::test_neo4j_satisfies_graph_store_extended`` asserts
    ``isinstance(neo4j_store, GraphStoreExtended)`` is True, a real
    per-instance parity check (``@runtime_checkable``) proving Neo4jStore
    actually implements every declared member -- adding ``search_nodes``
    here would make that assertion False and break the invariant this
    Protocol exists to guarantee. ``search_nodes`` has no consumer that
    checks ``isinstance(store, GraphStoreExtended)`` either: ``query.py``'s
    only routing check is a concrete-class isinstance tuple (Local/PG/Kuzu),
    so there is nothing for a Protocol declaration to buy here beyond the
    GAP TABLE's documentation, which already covers it.

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

        Returns ``[{"pack_id": str, "node_count": int, "sample_title": str,
        "sample_description": str}, ...]`` ordered by node_count descending.
        ``sample_title`` prefers the pack_create anchor node's title, then any
        node's ``source_package_title``, else ``""``. ``sample_description`` is
        the anchor node's ``description`` only (no node-level fallback exists),
        else ``""`` — it is projected inside this same aggregation so
        pack-relevance scoring (``content_pack_list(query=...)``) needs no
        per-pack follow-up lookup.
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
        self, pack_id: str | None = None, limit: int = 500_000, space: str | None = None
    ) -> list[dict[str, Any]]:
        """Bulk node export for pack ingest/re-export tooling.

        ``pack_id=None`` exports everything (up to ``limit``); otherwise
        matches a node whose ``pack_id``, ``source``, or ``source_id``
        property equals ``pack_id``. ``space=None`` (default) applies no
        space filter; otherwise every implementation pushes the space
        equality check into its native query (SQL WHERE / Cypher WHERE)
        ahead of ``limit``, not after -- filtering client-side post-limit
        silently undercounts and misreports ``total`` whenever the matching
        rows happen to sort past the limit boundary (issue #54). Result
        shape:
            {"props": dict, "labels": [str]}
        (the shape ``_normalise_node()`` in opencrab/pack/neo4j_export.py
        consumes).

        A caller that needs an accurate MATCH COUNT (e.g. to report
        ``total`` alongside a limited page of rows) must NOT use
        ``len(export_nodes(..., limit=N))`` -- that is still capped by
        ``limit`` even with the space/pack_id pushdown above. Use
        ``count_exported_nodes`` instead, which applies the identical
        predicate with no LIMIT.

        ``limit <= 0`` (issue #120): every implementation returns ``[]``
        immediately, without issuing a query. This is the pinned contract
        for both ``limit=0`` (0 rows requested -> 0 rows returned, checked
        BEFORE any row is collected -- Kuzu's ``pack_id`` branch used to
        collect one row before checking) and negative ``limit`` (which has
        no natural "N rows" meaning; treating it as unbounded would be a
        footgun -- e.g. SQLite maps a bound ``LIMIT -1`` to "no limit").

        SCOPE OF "BACKEND-WIDE" (issue #120, round 3 -- this line exists
        because "every implementation" was declared twice before without
        saying what "every" enumerates, and a 5th and 6th implementation
        were found missing the guard each time it wasn't spelled out).
        DOMAIN of this enumeration: PUBLIC methods on the CONCRETE backend
        classes in ``opencrab/stores/*.py`` that take a parameter literally
        named ``limit``. Two exclusions from that domain, for DIFFERENT
        reasons -- do not collapse them:
          - the Protocol declarations in THIS file (``GraphStore``,
            ``GraphStoreExtended``) are excluded because there is nothing
            to guard: their bodies are ``...``. They state the interface;
            the guard lives in each implementation.
          - private helpers are excluded on entirely different grounds:
            they DO contain real executable code, and each one uses its
            ``limit`` (``_find_neighbors_1hop`` executes one bounded query
            per directed pass; ``_build_neighbors_cypher`` executes nothing
            and returns Cypher containing ``LIMIT $limit`` plus params
            binding ``limit``, for ``find_neighbors`` to run; ``_expand``
            runs no query at all and slices an already-fetched batch with
            ``[:remaining]``).
            They are excluded because production reaches all three ONLY
            through ``find_neighbors``, which is itself classified below,
            so their status is decided there. A guard on a helper would be the
            wrong placement, not a missing one. If any of them ever gains
            a second public caller, it stops inheriting and needs its own
            row here.
        The ``limit <= 0 -> []`` contract is pinned on exactly the methods
        enumerated below, no more, no less --
          - ``GraphStoreExtended.export_nodes`` on all 4 graph stores:
            ``_sql_graph_base.py`` (LocalGraphStore + PGGraphStore, shared),
            ``KuzuGraphStore``, ``Neo4jStore``.
          - ``list_nodes`` / ``list_sources`` / ``get_audit_log`` on all 4
            doc stores: ``_sql_doc_base.py`` (LocalSQLDocStore + PgDocStore,
            shared), ``MongoStore``, and ``LocalDocStore`` (the legacy
            JSON store -- NOT reachable through ``factory.py``, but kept
            for callers that instantiate it directly per that module's own
            "WHY LocalDocStore IS KEPT" docstring, so it is in scope too).
          - ``search_nodes`` on 3 of the 4 graph stores, across 2
            implementations: ``_sql_graph_base.py`` (LocalGraphStore +
            PGGraphStore, shared) and ``KuzuGraphStore``. ``Neo4jStore`` has
            no ``search_nodes`` -- see the GAP TABLE above, which is why
            this is the one covered method that is not "all 4". Guarded by
            issue #86, not by #120, but it is the same contract and belongs
            in this enumeration so the list stays exhaustive.
        Explicitly NOT covered, left as pre-existing/tracked-separately
        gaps rather than silently absorbed into this contract:
          - other ``limit``-accepting GRAPH-store methods --
            ``find_neighbors``, ``find_by_relations`` and ``export_edges``
            (issue #131).
          - other ``limit``-accepting DOC-store methods --
            ``keyword_search``, implemented separately (NOT shared via
            ``_sql_doc_base.py``) in ``local_sql_doc_store.py`` and
            ``pg_doc_store.py``; ``MongoStore`` and ``LocalDocStore`` have
            none. Both implementations overfetch
            ``max(1, limit) * 5`` rows and then append BEFORE testing
            ``len(out) >= limit``, so ``limit <= 0`` yields 1 row rather
            than 0 -- the same append-before-check shape as the Kuzu bug
            #120 fixed, tracked separately. And ``bm25_fingerprint`` on
            ``_sql_doc_base.py`` (no graph store implements it), which
            accepts a ``limit`` but never applies it: it is a whole-table
            ``COUNT(*)`` staleness probe and a capped count would pin
            forever once the corpus exceeds the cap (#63) -- so a
            ``limit <= 0 -> []`` guard there would be a regression, not a
            fix. See its own docstring.
          - ``sql_store.py``'s ``get_impacts``, the only limit-accepting
            method outside the graph/doc surface. It binds a negative
            ``limit`` straight into ``LIMIT :limit`` (unbounded on SQLite),
            but belongs to a different subsystem per that module's own
            docstring: "impact records, ReBAC policy assignments, lever
            simulations".
        NOT in the domain at all, so not an exclusion: the vector stores.
        ``ChromaStore``, ``PgVectorStore`` and ``SqliteVecStore`` have no
        ``limit`` parameter anywhere -- they bound top-k with ``n_results``.
        That is a different parameter, so this contract says nothing about
        it either way; it is not an exclusion, it is out of scope.
        A future round extending this contract to one of those must add it
        to this enumeration, not just fix the code.
        """
        ...

    def count_exported_nodes(
        self, pack_id: str | None = None, space: str | None = None
    ) -> int:
        """Exact count of nodes matching the same ``pack_id``/``space``
        predicate ``export_nodes`` filters on, with no LIMIT applied
        (issue #54: a caller reporting ``total`` must not have it capped by
        whatever display ``limit`` it also passes to ``export_nodes`` --
        ``len(export_nodes(..., limit=N))`` silently truncates at ``N`` even
        after the space/pack_id pushdown fix).

        Every implementation runs a real ``COUNT(*)``/``count(n)`` query
        except ``KuzuGraphStore``'s ``pack_id`` case: ``pack_id`` lives
        inside a JSON-serialized ``props`` blob Cypher cannot index into (the
        same limitation ``export_nodes`` has there), so when ``pack_id`` is
        given, KuzuGraphStore scans every space-matching row (no LIMIT
        clause -- a cap here would just move issue #54's bug to a bigger
        number) and counts the Python-filtered result. This guarantee --
        exact, uncapped, regardless of match count -- holds for every
        implementation and every argument combination; see
        ``KuzuGraphStore.count_exported_nodes`` for the tracked scalability
        follow-up (the pack_id case costs an O(n) scan, not O(1)).
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

    # ------------------------------------------------------------------
    # Scoped (authorization) surface — issue #147 read-path scoping.
    #
    # These 4 methods exist BECAUSE the 4 above (``get_node_by_id``,
    # ``export_nodes``, ``count_exported_nodes``, ``export_edges``) are
    # unsafe for an authorization read path and are deliberately left
    # unchanged for their existing bulk-export/fork use case (see issue
    # #147 §3.4(b)/§8): ``export_nodes``'s pack_id/source/source_id 3-way OR
    # lets a node claim membership in a pack it was never actually written
    # into, and ``get_node_by_id``'s bare ``WHERE node_id=:nid LIMIT 1`` has
    # no ``node_type`` predicate even though ``graph_nodes``' real PK is
    # ``(node_type, node_id)`` -- either gap is fine for a pack-export tool
    # (the caller already owns the whole pack) but not for a permission
    # check. This is a NEW, separate surface, not a signature change to the
    # 4 existing methods above.
    #
    # Declared on ALL FOUR backends (Local/PG via ``_sql_graph_base.py``,
    # Kuzu, Neo4j) -- unlike ``search_nodes`` above, Neo4j is NOT exempt
    # here: Neo4j's node/edge properties are native (not a JSON blob like
    # Kuzu's), so pushing a pack predicate into Cypher is not the
    # capability gap ``search_nodes`` has there.
    # ------------------------------------------------------------------

    def export_nodes_scoped(
        self, pack_ids: list[str], limit: int, space: str | None = None
    ) -> list[dict[str, Any]]:
        """Authorization-scoped node export -- ``export_nodes``, but with
        ``pack_id`` REQUIRED (no "everything" mode) and matched STRICTLY
        (only a node's own ``pack_id`` property counts; never ``source``/
        ``source_id``). Return shape identical to ``export_nodes``.

        Empty ``pack_ids`` -> ``[]`` WITHOUT querying (nothing is in scope,
        so there is nothing to fetch). ``limit <= 0`` -> ``[]``, same
        contract as ``export_nodes`` (issue #120).
        """
        ...

    def count_exported_nodes_scoped(
        self, pack_ids: list[str], space: str | None = None
    ) -> int:
        """Exact ``COUNT`` counterpart to ``export_nodes_scoped``, same
        predicate, unbounded by any LIMIT (issue #54's reasoning, applied
        to the scoped predicate). Empty ``pack_ids`` -> ``0`` without
        querying.
        """
        ...

    def export_edges_scoped(self, pack_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """Authorization-scoped edge export -- AND rule, the opposite of
        ``export_edges``' 5-way OR: BOTH endpoints' ``pack_id`` must be in
        ``pack_ids``, AND the edge's own ``pack_id`` (if it has one) must
        also be in ``pack_ids``. Required because ``export_edges``' OR would
        expose a node outside the caller's scope via the OTHER endpoint's
        membership -- the response embeds both endpoints' full properties,
        so an OR-across-endpoints predicate is a leak here, not just a
        looser filter (see ``_graph_common._edge_passes``, the same 3-rule
        policy this reproduces as SQL/Cypher). Return shape identical to
        ``export_edges``.

        Empty ``pack_ids`` -> ``[]`` without querying. ``limit <= 0`` ->
        ``[]``, matching ``export_edges``' contract.
        """
        ...

    def get_node_by_id_scoped(self, node_id: str, pack_ids: list[str]) -> dict[str, Any] | None:
        """Type-agnostic, SCOPE-FILTERED node lookup -- ``get_node_by_id``,
        but with the pack predicate applied BEFORE any row-limiting
        operation (never a Python post-filter over a single already-picked
        row). This matters because ``graph_nodes``' real PK is
        ``(node_type, node_id)``: the same ``node_id`` can exist under a
        DIFFERENT ``node_type`` in a pack the caller cannot read, and
        filtering after an unscoped ``LIMIT 1`` would sometimes answer "not
        found" even when the caller's OWN pack genuinely has that id under
        a different type -- a false-negative on top of the leak a naive
        post-filter would still have to prevent. Concrete backend
        requirements differ because of this:
          - SQL (Local/PG): the pack predicate is pushed into the SQL WHERE
            clause AHEAD of ``LIMIT 1``.
          - Kuzu: no ``LIMIT 1`` in the underlying query AT ALL (its
            properties are a JSON-blob column, so the pack predicate can't
            be pushed into Cypher) -- every ``node_id``-matching row is
            fetched and the scope filter picks among them in Python.
          - Neo4j: native properties, so ``n.pack_id IN $pack_ids`` is
            pushed straight into the Cypher WHERE, same as SQL.

        Scope-INTERNAL homonym collisions (two different node_types, BOTH
        inside the caller's own readable scope, sharing one node_id) are
        NOT resolved by any backend here -- which row is returned in that
        case is unspecified. Out of scope for issue #147 (a data-integrity
        question, not a confidentiality one; see its §8).

        Empty ``pack_ids`` -> ``None`` without querying.
        """
        ...
