"""
GraphStore Protocol - the uniform graph-store interface implemented by
LocalGraphStore, PGGraphStore, and Neo4jStore. ``STORAGE_MODE=kuzu`` uses a
capability-negative facade: its lifecycle surface is present for startup
plumbing, while graph reads and writes raise capability exceptions until the
Ladybug transaction/CAS qualification is complete.

Consumers historically decided which methods a graph store supports by
``isinstance(store, (LocalGraphStore, KuzuGraphStore))`` (e.g.
opencrab/mcp/tools.py:1064, opencrab/ontology/impact.py:124,263,
opencrab/pack/neo4j_export.py:231) or ``hasattr(store, "...")``
(opencrab/mcp/tools.py:1577,1580,1611,1661,1667). Both patterns exist only
because Neo4jStore once lacked several methods the other backends shared -
this module names that shared surface as a single ``typing.Protocol`` so new
consumer code can branch on capability (``isinstance(store, GraphStore)`` or
a plain ``hasattr``) without enumerating concrete classes.

This module is DECLARATION ONLY: it defines the Protocol and documents each
method's contract (params, return shape) against the reference
implementations (LocalGraphStore / PGGraphStore, which share the SQL graph
base and therefore have line-for-line parity - see pg_graph_store.py's module
docstring). It does not
implement, patch, or monkeypatch anything on the store classes.

GAP TABLE - method presence per operational backend (checked against the
current source of each store module). The Kùzu facade is intentionally
capability-negative even where ``__getattr__`` supplies a compatibility
callable, so a table "yes" means only that the protocol shape is reachable,
not that the operation is supported:

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
    get_nodes_by_id       yes     yes     yes     yes
    list_packs            yes     yes     yes     yes
    find_by_relations     yes     yes     yes     yes
    export_nodes          yes     yes     yes     yes
    count_exported_nodes  yes     yes     yes     yes
    search_nodes          yes     yes     yes     no
    export_edges          yes     yes     yes     yes
    upsert_nodes_batch    yes     yes     yes     yes
    upsert_edges_batch    yes     yes     yes     yes
    count_dangling_edges  yes     yes     no      no

``count_dangling_edges`` (issue #84) is the second row that is genuinely
"no" outside the SQL backends, and like ``search_nodes`` it is deliberately
not declared as a Protocol member. It counts edges whose endpoint snapshot
resolves to no node row -- a state only the SQL schema permits, because it
declares no foreign key. Neo4j cannot hold a relationship without both
endpoints, and its ``_initialise_schema_state`` already walks every
OpenCrab-owned relationship and classifies label/type drift as
partial_or_unknown, which gates writes; the SQL classifiers only inspect DDL
and column metadata, never rows, so the SQL backends had no equivalent
signal at all. A Neo4j-side drift diagnostic is tracked separately rather
than stubbed here -- a constant would be a claim this code cannot make.

``search_nodes`` (issue #86) is the other row above that is genuinely "no" for
Neo4j, not stale -- ``HybridQuery.keyword_search`` never needed a
Neo4jStore.search_nodes because its Cypher ``CONTAINS`` branch already
pushes the same keyword/space predicate straight into Cypher without going
through a store method. The Kùzu facade rejects this call until qualification.

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
a drop-in replacement for capability checks; it does not turn the Kùzu
facade's rejected methods into operational support. ``GraphStoreExtended``
below is a separate Protocol consumers can check against independently of
the base ``GraphStore`` (see #64 for whether that split is still warranted
now that method presence is at parity).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from opencrab.common.graph_identity import (
    ApplyMigrationRequest,
    DryRunMigrationRequest,
    EdgeWriteReceipt,
    GraphInventory,
    MigrationReceipt,
    NodeWriteReceipt,
    ProvenanceBatchReceipt,
)


@runtime_checkable
class GraphStore(Protocol):
    """Core graph-store surface for operational backends and the Kùzu facade."""

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

        LocalGraphStore and PGGraphStore need no extra constraint call because
        their classified target schemas use primary keys. Neo4j creates its
        native constraints. The Kùzu facade raises a capability exception;
        an unavailable Neo4j store still degrades to a warning log.
        """
        ...

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
        space_id: str | None = None,
        *,
        return_receipt: bool = False,
    ) -> dict[str, Any] | NodeWriteReceipt:
        """Create or update one node; returns its stored properties dict.

        The returned dict always contains at least ``{"id": node_id, **properties}``.
        SQL backends merge the dedicated space column into the returned shape;
        the Kùzu facade raises until its writer capability is qualified.
        Raises an availability or capability exception when unsupported.
        """
        ...

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        """Fetch one node's properties by global node_id and type; None if absent.

        Requires the exact type — use ``get_node_by_id`` (extended surface)
        for a type-agnostic lookup by id alone.
        """
        ...

    def lookup_node_type(self, node_id: str) -> str | None:
        """Best-effort node_type resolution by id alone; None if not found.

        SQL and Neo4j implementations are best-effort and return None when
        unavailable. The Kùzu facade raises a read-capability exception until
        qualification, so callers must treat that mode as disabled.
        """
        ...

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """Delete one node and its incident edges.

        Returns True iff the node itself was deleted (i.e. it existed
        before the call) across operational backends. A node with
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
        *,
        return_receipt: bool = False,
    ) -> bool | EdgeWriteReceipt:
        """Create or update a directed (from)->(to) edge; True on success.

        Raises an availability or identity exception when unsupported.

        Endpoints must already exist, and must already carry the node_type
        this call names -- an id that resolves to a row of a DIFFERENT type
        is refused just like a missing one, since the edge's endpoint
        snapshot would not resolve either way. Both Neo4j and the qualified
        SQL writers report that refusal as **False**, and write nothing.
        The Kùzu facade is capability-negative.

        Note the deliberate asymmetry with ``upsert_edges_batch``, which
        raises instead: a single call has one outcome the caller is already
        branching on, while a batch has to say WHICH row was bad, and
        silently returning a lower count would let a partial write pass for
        a whole one.
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

        The contract is deliberately "match this backend's own conflict
        key". Qualified SQL and Neo4j backends use the global edge key
        ``(from_id, relation, to_id)`` and verify endpoint type snapshots.
        The Kùzu facade rejects the read until qualification.

        Always returns a parsed ``dict``, never a raw JSON blob.
        """
        ...

    def update_node(
        self, node_id: str, expected_current_digest: str, new_type: str,
        new_properties: dict[str, Any], new_space_id: str | None = None,
        *, return_receipt: bool = False,
    ) -> dict[str, Any] | NodeWriteReceipt:
        ...

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
        """Atomically reclassify one global node by expected current digest.

        This CAS has no operation ledger. A second call with the old digest is
        a stale conflict after the first call succeeds.
        """
        ...

    def inspect_graph_identity(self) -> GraphInventory:
        """Return every typed legacy row and its source fingerprint read-only."""
        ...

    def migrate_graph_identity(
        self,
        request: DryRunMigrationRequest | ApplyMigrationRequest,
    ) -> MigrationReceipt:
        """Plan or atomically apply the SQL graph identity migration."""
        ...

    def delete_edge(self, from_id: str, relation: str, to_id: str, *, owner_pack_id: str) -> bool:
        ...

    def update_edge(
        self, from_type: str, from_id: str, relation: str, to_type: str,
        to_id: str, properties: dict[str, Any] | None = None, *,
        expected_current_digest: str, owner_pack_id: str,
        return_receipt: bool = False,
    ) -> bool | EdgeWriteReceipt:
        ...

    def get_node_digest(self, node_id: str, *, node_type: str | None = None) -> str | None:
        ...

    def get_edge_digest(self, from_id: str, relation: str, to_id: str, *, from_type: str | None = None, to_type: str | None = None) -> str | None:
        ...

    def graph_fingerprint(self) -> str:
        ...

    def backfill_pack_provenance(self, records: list[dict[str, Any]]) -> ProvenanceBatchReceipt:
        ...

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute arbitrary Cypher; each result record as one dict.

        Only Neo4jStore runs arbitrary graph queries in the qualified
        production configuration. The Kùzu facade rejects both read and
        write queries until its capability is qualified. LocalGraphStore and
        PGGraphStore always return ``[]`` with a logger.warning — this method
        is NOT a reliable capability probe;
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
    """The 7 methods this Protocol was carved out for - originally the ones
    LocalGraphStore/PGGraphStore/KuzuGraphStore had and Neo4jStore did not
    (D3's Stage-4 R5 worklist).

    That gap has since been closed: ``test_graph_protocol_contract.py::
    test_neo4j_satisfies_graph_store_extended`` asserts
    ``isinstance(neo4j_store, GraphStoreExtended)`` and passes, so the
    sentence above describes why this Protocol is SEPARATE from
    ``GraphStore``, not a live difference between the backends. It stays
    separate because the split is what lets the assertion be a real parity
    check rather than a tautology.

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
        (e.g. ``{"id": ..., "node_type": "Lever", ...}``). Neo4j uses
        ``OpenCrabNode.node_id`` and the explicit ``node_type`` property.
        The Kùzu facade rejects this read until qualification.
        """
        ...

    def get_nodes_by_id(self, node_id: str) -> list[dict[str, Any]]:
        """Plural counterpart to ``get_node_by_id`` -- returns EVERY row for
        ``node_id``, not just whichever one ``get_node_by_id``'s unordered
        ``LIMIT 1`` happens to pick.

        All qualified graph backends use global node_id identity, so a valid
        target schema has at most one row for an id. The plural method remains
        available for inspecting legacy or externally corrupted data. Callers
        that need to reason about every matching row use this instead.
        Rows are ordered by node_type/label for a deterministic result;
        ``[]`` when nothing matches.
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

    def find_by_relations_scoped(
        self,
        node_id: str,
        relations: list[str],
        pack_ids: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Authorization-scoped ``find_by_relations`` (#147).

        Same shape, but the anchor, the other endpoint AND the edge itself
        must all be in ``pack_ids``, and all three are constrained before
        ``LIMIT``. ``find_by_relations`` never returns edge properties, so a
        caller cannot reproduce the edge half of that rule by post-filtering
        its results. Empty ``pack_ids`` or ``relations`` -> ``[]``.
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

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[NodeWriteReceipt, ...]:
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

    def upsert_edges_batch(self, edges: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[EdgeWriteReceipt, ...]:
        """Bulk upsert; returns the count processed.

        Each item: ``{"from_type": str, "from_id": str, "relation": str,
        "to_type": str, "to_id": str, "properties": dict | None}``. On
        Local/PG this is ``len(edges)`` (or 0 for empty input); Kuzu's port
        loops calling ``upsert_edge`` per item and only counts the ones that
        returned True.

        Local/PG validate EVERY item's endpoints (existence and node_type)
        in one pass BEFORE the insert loop starts, and raise ``ValueError``
        on the first offender. So a batch mixing good rows with one bad row
        writes none of them -- a pre-validation refusal, not a rollback after
        partial writes. That is why the count can be ``len(edges)``: the call
        either wrote them all or raised. Contrast ``upsert_edge``, which
        reports the same condition as False (see its note above).
        """
        ...

    def update_nodes_batch(self, nodes: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[NodeWriteReceipt, ...]:
        ...

    def update_edges_batch(self, edges: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[EdgeWriteReceipt, ...]:
        ...

    # ------------------------------------------------------------------
    # Scoped (authorization) surface — issue #147 read-path scoping.
    #
    # These 4 methods exist because the 4 above (``get_node_by_id``,
    # ``export_nodes``, ``count_exported_nodes``, ``export_edges``) are
    # deliberately broad bulk-export surfaces rather than authorization
    # predicates. The scoped methods keep their existing pack-export use case
    # separate from permission checks (see issue #147 §3.4(b)/§8). This is a
    # new surface, not a signature change to the 4 existing methods above.
    #
    # Declared on Local/PG/Neo4j. The Kùzu facade rejects reads until its
    # capability is qualified, so its compatibility callables are not a
    # supported scoped-read implementation.
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

        ``labels`` SHAPE (issue #201): a row's ``labels`` may contain the
        storage marker ``_graph_common.GRAPH_BASE_LABEL`` at an UNDECLARED
        position alongside the domain type -- the Neo4j backend returns
        ``labels(n)`` verbatim and its ``MERGE`` stamps both, while the SQL
        and Kuzu backends return a single-element ``[node_type]``. A consumer
        that needs the domain type must therefore filter with
        ``_graph_common.domain_labels()`` rather than indexing ``labels``.
        Stated as a fact about the return value, not as a claim about
        callers: consumers that still index ``labels`` directly exist in this
        repository.
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
        row). Graph identity is global ``node_id``; duplicate legacy rows
        remain a migration condition and must not be hidden by an unscoped
        ``LIMIT 1``. Concrete backend requirements differ because of this:
          - SQL (Local/PG): the pack predicate is pushed into the SQL WHERE
            clause AHEAD of ``LIMIT 1``.
          - Kuzu: the capability-negative facade rejects this read until the
            transaction/CAS qualification is complete.
          - Neo4j: native properties, so ``n.pack_id IN $pack_ids`` is
            pushed straight into the Cypher WHERE, same as SQL.

        A valid target cannot contain two node types for one id. A legacy
        duplicate is therefore a migration condition, not an in-scope
        authorization result.

        Empty ``pack_ids`` -> ``None`` without querying.
        """
        ...
