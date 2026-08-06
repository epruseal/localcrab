"""Graph tools: manifest, node/edge writes, and node/edge reads.

Handlers here call ``_get_context``/``_clean_str``/``_clean_meta`` via a
function-scope ``from opencrab.mcp.tools import ...`` inside each handler
body (not a module-level import). That is required for
``patch("opencrab.mcp.tools._get_context")`` (and friends) to keep working:
the patch replaces the attribute on the ``opencrab.mcp.tools`` package
object, and only a *late* import — resolved at call time — observes that
patched value. A module-level import here would bind a stale, unpatchable
reference to this module's own globals instead. See the package
``__init__.py`` docstring for the fuller mock.patch namespace-binding
rationale.
"""

from __future__ import annotations

import logging
from typing import Any

from ._registry import tool

logger = logging.getLogger(__name__)


@tool(
    "ontology_manifest",
    {
        "description": (
            "Return the full MetaOntology OS grammar: spaces, meta-edges, "
            "impact categories, active metadata layers, and ReBAC config."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    order=0,
)
def ontology_manifest() -> dict[str, Any]:
    """
    Return the full MetaOntology OS grammar.

    Includes spaces, meta-edges, impact categories, active metadata
    layers, and ReBAC configuration.
    """
    from opencrab.grammar.validator import describe_grammar

    return describe_grammar()


@tool(
    "ontology_add_node",
    {
        "description": "Add or update a node in the MetaOntology graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "space": {
                    "type": "string",
                    "description": "MetaOntology space (e.g. subject, resource, concept).",
                },
                "node_type": {
                    "type": "string",
                    "description": "Node type within the space (e.g. User, Document).",
                },
                "node_id": {
                    "type": "string",
                    "description": "Stable unique identifier for the node.",
                },
                "properties": {
                    "type": "object",
                    "description": "Optional key/value properties.",
                },
            },
            "required": ["space", "node_type", "node_id"],
        },
    },
    order=1,
    writes=True,
)
def ontology_add_node(
    space: str,
    node_type: str,
    node_id: str,
    properties: dict[str, Any] | None = None,
    tenant_id: str = "default",
    subject_id: str | None = None,
) -> dict[str, Any]:
    """
    Add or update a node in the MetaOntology graph.

    Parameters
    ----------
    space:
        MetaOntology space (e.g. "subject", "resource", "concept").
    node_type:
        Node type within that space (e.g. "User", "Document").
    node_id:
        Stable unique identifier.
    properties:
        Key/value properties for the node.
    tenant_id:
        Tenant identifier for multi-tenant isolation (default: 'default').
    subject_id:
        Optional subject performing the write (stamped into properties).
    """
    from opencrab.mcp.tools import _clean_meta, _clean_str, _get_context
    from opencrab.ontology.builder import graph_write_failed
    from opencrab.ontology.tenant import TenantContext, stamp_properties

    ctx = _get_context()
    space = _clean_str(space)
    node_type = _clean_str(node_type)
    node_id = _clean_str(node_id)
    tenant_ctx = TenantContext(tenant_id=tenant_id, subject_id=subject_id)
    props = stamp_properties(_clean_meta(properties or {}), tenant_ctx)
    try:
        result = ctx["builder"].add_node(
            space=space,
            node_type=node_type,
            node_id=node_id,
            properties=props,
            subject_id=subject_id,
        )
        # #66 codex re-review (finding [8]): this sibling of
        # ontology_add_edge had the exact same fail-open bug — add_node()
        # never raises for a per-store failure (builder.py's module
        # docstring), so a bare "no exception -> bill" call here charged for
        # nodes that never landed in the graph. Same graph-only,
        # fail-closed gate as ontology_add_edge/harness_promotion_apply.
        if not graph_write_failed(result.get("stores") or {}):
            billing_result = ctx["billing"].on_node_write(tenant_id, subject_id, space, node_type)
            if not billing_result.get("ok"):
                # #105: emit() is fire-and-forget by design and never raises,
                # but a failed persist must not vanish with only
                # BillingHooks' own internal log line — surface it here too
                # so this handler's own log context (tenant/space/node_type)
                # is attached. Does not fail the write: the node write
                # already succeeded above.
                logger.warning(
                    "on_node_write billing event failed to persist (tenant=%s, space=%s, node_type=%s): %s",
                    tenant_id, space, node_type, billing_result.get("error"),
                )
        ctx["hybrid"].invalidate_bm25_cache()
        return result
    except ValueError as exc:
        return {"error": str(exc), "valid": False}
    except Exception as exc:
        logger.error("ontology_add_node failed: %s", exc)
        return {"error": str(exc)}


@tool(
    "ontology_add_edge",
    {
        "description": (
            "Add a directed edge between two nodes. Validates the relation "
            "against the MetaOntology grammar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_space": {"type": "string", "description": "Source node space."},
                "from_id": {"type": "string", "description": "Source node ID."},
                "relation": {"type": "string", "description": "Relation label."},
                "to_space": {"type": "string", "description": "Target node space."},
                "to_id": {"type": "string", "description": "Target node ID."},
                "properties": {"type": "object", "description": "Optional edge properties."},
                "tenant_id": {
                    "type": "string",
                    "description": "Tenant identifier for multi-tenant isolation (default: 'default').",
                },
                "subject_id": {
                    "type": "string",
                    "description": "Optional subject performing the write (for billing/audit).",
                },
            },
            "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
        },
    },
    order=2,
    writes=True,
)
def ontology_add_edge(
    from_space: str,
    from_id: str,
    relation: str,
    to_space: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
    tenant_id: str = "default",
    subject_id: str | None = None,
) -> dict[str, Any]:
    """
    Add a directed edge between two ontology nodes.

    The (from_space, to_space, relation) triple is validated against
    the MetaOntology grammar before the write is attempted.

    Parameters
    ----------
    from_space:
        Space of the source node.
    from_id:
        ID of the source node.
    relation:
        Relation label (must be valid for the space pair).
    to_space:
        Space of the target node.
    to_id:
        ID of the target node.
    properties:
        Optional edge properties.
    tenant_id:
        Tenant identifier for multi-tenant isolation (default: 'default').
    subject_id:
        Optional subject performing the write (for billing/audit — not
        stamped into edge properties, unlike ontology_add_node).
    """
    from opencrab.mcp.tools import _clean_meta, _clean_str, _get_context
    from opencrab.ontology.builder import graph_write_failed

    ctx = _get_context()
    from_id = _clean_str(from_id)
    to_id = _clean_str(to_id)
    relation = _clean_str(relation)
    try:
        result = ctx["builder"].add_edge(
            from_space=_clean_str(from_space),
            from_id=from_id,
            relation=relation,
            to_space=_clean_str(to_space),
            to_id=to_id,
            properties=_clean_meta(properties or {}),
            subject_id=subject_id,
        )
        # #66 hardening: builder.add_edge() never raises for a per-store
        # failure (missing endpoint / store down all come back as a string
        # inside result["stores"], see builder.py's module docstring) — a
        # bare "no exception -> bill" would charge for edges that never
        # landed anywhere. graph_write_failed() reads the SAME store-status
        # map ontology_add_edge already returns to the caller, so a rejected
        # write ("no match (missing node: ...)" / "error: ..." / graph
        # "unavailable") is never billed. Optional-store-only failures
        # (docs) still bill — the edge exists in the graph either way.
        if not graph_write_failed(result.get("stores") or {}):
            billing_result = ctx["billing"].on_edge_write(tenant_id, subject_id, relation)
            if not billing_result.get("ok"):
                # #105: emit() is fire-and-forget by design and never raises,
                # but a failed persist must not vanish with only
                # BillingHooks' own internal log line — surface it here too
                # so this handler's own log context (tenant/relation) is
                # attached. Does not fail the write: the edge write already
                # succeeded above.
                logger.warning(
                    "on_edge_write billing event failed to persist (tenant=%s, relation=%s): %s",
                    tenant_id, relation, billing_result.get("error"),
                )
        ctx["hybrid"].invalidate_bm25_cache()
        return result
    except ValueError as exc:
        return {"error": str(exc), "valid": False}
    except Exception as exc:
        logger.error("ontology_add_edge failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# READ helpers (no grammar validation needed — pure reads)
# ---------------------------------------------------------------------------


@tool(
    "ontology_get_node",
    {
        "description": "Fetch a single node by node_id regardless of type or space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "The node_id to look up."},
            },
            "required": ["node_id"],
        },
    },
    order=4,
)
def ontology_get_node(node_id: str) -> dict[str, Any]:
    """Fetch a single node by node_id regardless of type.

    All four storage backends implement get_node_by_id() natively (type-
    agnostic, single SQL/Cypher LIMIT 1) — see opencrab/stores/_graph_protocol.py.
    """
    from opencrab.mcp.tools import _clean_str, _get_context

    ctx = _get_context()
    graph = ctx["neo4j"]
    node_id = _clean_str(node_id)
    result = graph.get_node_by_id(node_id)

    if result is None:
        return {"found": False, "node_id": node_id}
    return {"found": True, "node_id": node_id, "node": result}


@tool(
    "ontology_list_nodes",
    {
        "description": (
            "List nodes, optionally filtered by space and/or pack_id. Without pack_id, lists "
            "from the doc store. With pack_id, lists from the graph store, and `total` in the "
            "response is the TRUE count of all matching nodes -- it is NOT capped by `limit` "
            "and can be larger than the number of `nodes` actually returned; if `total` "
            "exceeds len(nodes), the page was truncated and a larger `limit` will return more. "
            "Useful for inspecting a pack's contents after ingest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "space": {"type": "string", "description": "Optional MetaOntology space filter (e.g. evidence, concept)."},
                "pack_id": {"type": "string", "description": "Optional pack_id filter."},
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of `nodes` rows returned (default 100). WITH pack_id: "
                        "does NOT cap `total`, which is the full match count regardless of "
                        "`limit`. WITHOUT pack_id: `total` is the doc-store page size, i.e. it "
                        "IS capped at `limit` (total == len(nodes) always in that case)."
                    ),
                    "default": 100,
                },
            },
            "required": [],
        },
    },
    order=5,
)
def ontology_list_nodes(
    space: str | None = None,
    pack_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List nodes filtered by space and/or pack_id.

    WITH pack_id: this calls (in this order, matching the code below) the
    graph store's count_exported_nodes(pack_id=..., space=..., no LIMIT)
    first for ``total``, THEN export_nodes(pack_id=..., space=..., limit=...)
    for the displayed page. All three concrete backends (SQL-backed
    local/pg, Kuzu, Neo4j) push ``space`` (and, for SQL/Neo4j, ``pack_id``
    too) into their native query ahead of ``limit`` — see each backend's
    export_nodes and the shared contract in opencrab/stores/_graph_protocol.py
    — so the returned rows are correct before truncation instead of being
    Python-filtered after (issue #54; same class of bug #62 fixed for
    find_neighbors' pack filter). ``total`` is deliberately NOT
    ``len(nodes)``: that would still be capped at ``limit`` even with the
    pushdown fix, which is the actual bug #54 reported (a caller cannot
    tell "5 of 5 matches" from "5 of 3000 matches, truncated" from the row
    count alone) — count_exported_nodes runs the identical filter with no
    LIMIT so ``total`` is the true match count.

    WITHOUT pack_id: falls back to the doc store's list_nodes, and ``total``
    IS ``len(nodes)`` — i.e. IS capped at ``limit``, same failure mode #54
    reported, just in a different subsystem (doc store, not graph store).
    Judged and left unfixed here (audit finding, MCP-visibility round):
    structurally this is NOT impossible to fix the same way — the SQL-backed
    doc stores (LocalSQLDocStore/PgDocStore, both opencrab/stores/
    _sql_doc_base.py) could grow a real ``COUNT(*) WHERE space=...``
    sibling to ``list_nodes``, mirroring count_exported_nodes exactly, and
    MongoStore (docker mode) has ``count_documents()`` for the same
    purpose. It is left out of this fix because it is a different
    subsystem than #54's named scope (graph.py's pack_id+space path +
    _sql_graph_base.py's export_nodes), touching three more backend files
    with their own test suites — a same-sized second effort, not a small
    extension of this one. Tracked as a follow-up, not silently accepted:
    the MCP ``description`` and ``limit`` parameter description (the part
    of this contract an MCP client actually sees -- this docstring is
    developer-only and never reaches a client) both say plainly that
    ``total`` is limit-capped in the no-pack_id case, so no caller is told
    a false "always accurate" guarantee in the meantime.

    SNAPSHOT CONSISTENCY (audit finding #54-[6]): count_exported_nodes and
    export_nodes are two separate queries, not wrapped in one transaction/
    snapshot. A write landing on the same pack_id/space between them can
    make ``total`` and ``len(nodes)`` momentarily disagree (e.g. a node
    inserted in that gap is counted in ``total`` but missed by the already-
    issued ``export_nodes`` page, or vice versa for a delete). This is a
    deliberate tradeoff, not an oversight: a cross-query transaction here
    would need to work uniformly across three backends with different
    transaction/snapshot primitives (SQL, Kuzu, Neo4j) for a single-user,
    mostly-read MCP tool call, which is not worth the complexity for a
    momentary, self-correcting inconsistency (the next call reflects
    current state). Callers must not assume ``total`` and ``len(nodes)``
    are always perfectly reconciled under concurrent writes.
    """
    from opencrab.mcp.tools import _clean_str, _get_context

    ctx = _get_context()
    pack_id = _clean_str(pack_id) if pack_id else None
    cleaned_space = _clean_str(space) if space else None

    nodes: list[dict[str, Any]] = []
    total = 0

    if pack_id:
        graph_store = ctx["neo4j"]
        # True match count, independent of `limit` (issue #54's core
        # requirement -- see this function's docstring).
        total = graph_store.count_exported_nodes(pack_id=pack_id, space=cleaned_space)
        # Graph store: indexed/native pack_id + space filter → correct rows
        # before limit (all three backends implement the same contract, see
        # _graph_protocol.py#export_nodes).
        raw = graph_store.export_nodes(pack_id=pack_id, limit=limit, space=cleaned_space)
        # export_nodes returns [{"props": dict, "labels": [str]}, ...]
        # normalise to same shape as doc store list_nodes. The space check
        # below is now redundant with the backend's own filter (kept as
        # cheap defense-in-depth, same spirit as _expand()'s redundant
        # pack_set check in _sql_graph_base.py) rather than load-bearing.
        for item in raw:
            props = item.get("props") or {}
            labels = item.get("labels") or []
            node_type = labels[0] if labels else props.get("node_type", "")
            n_id = props.get("node_id") or props.get("id", "")
            n_space = props.get("space_id") or props.get("space", "")
            if cleaned_space and n_space != cleaned_space:
                continue
            nodes.append({
                "node_id": n_id,
                "node_type": node_type,
                "space": n_space,
                "properties": props,
            })
    else:
        # Doc store fallback (no pack_id filter requested)
        nodes = ctx["mongo"].list_nodes(space=cleaned_space, limit=limit)
        total = len(nodes)

    return {
        "nodes": nodes,
        "total": total,
        "space_filter": space,
        "pack_id_filter": pack_id,
    }


@tool(
    "ontology_list_edges",
    {
        "description": (
            "List edges, optionally filtered by pack_id. "
            "Useful for inspecting graph relationships after ingest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_id": {"type": "string", "description": "Optional pack_id filter."},
                "limit": {"type": "integer", "description": "Maximum results (default 200).", "default": 200},
            },
            "required": [],
        },
    },
    order=6,
)
def ontology_list_edges(
    pack_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """List edges, optionally filtered by pack_id.

    All four backends implement export_edges() natively (wide shape:
    source_props/source_labels/target_props/target_labels/rel_props/relation
    — see opencrab/stores/_graph_protocol.py). pack_id matches either
    endpoint's pack_id/source/source_id, or the edge's own — the backend
    owns that filter, not this function.
    """
    from opencrab.mcp.tools import _clean_str, _get_context

    ctx = _get_context()
    graph = ctx["neo4j"]
    pack_id = _clean_str(pack_id) if pack_id else None

    if hasattr(graph, "export_edges"):
        try:
            edges = graph.export_edges(pack_id=pack_id, limit=limit)
            return {"edges": edges, "total": len(edges), "pack_id_filter": pack_id}
        except Exception as exc:
            # Report the real failure instead of falling through to the
            # generic "unavailable" message, which would otherwise mask an
            # operational error as if the store didn't exist at all.
            logger.warning("export_edges failed: %s", exc)
            return {"edges": [], "total": 0, "error": str(exc), "pack_id_filter": pack_id}

    return {"edges": [], "total": 0, "error": "graph store unavailable", "pack_id_filter": pack_id}
