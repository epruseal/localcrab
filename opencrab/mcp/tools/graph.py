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

from opencrab.common.graph_identity import NodeIdentityConflict

from ._registry import AccessTier, tool

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
    access=AccessTier.READ,
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
                "pack_id": {
                    "type": "string",
                    "description": "Optional destination pack_id. Defaults to the caller's default pack.",
                },
            },
            "required": ["space", "node_type", "node_id"],
        },
    },
    order=1,
    access=AccessTier.WRITE,
    writes=True,
)
def ontology_add_node(
    space: str,
    node_type: str,
    node_id: str,
    properties: dict[str, Any] | None = None,
    pack_id: str | None = None,
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
    pack_id:
        Optional destination pack_id. Defaults to the caller's default pack
        (``resolve_write_pack``).

    The writing subject is never a client-supplied argument (#145, #143
    invariant 2) -- it is the caller's server-derived ``current_principal()``,
    bound by dispatch_tool before this handler runs. tenant_id stays fixed at
    'default' -- multi-tenant scoping is out of scope for this fix (tracked
    separately, see opencrab/ontology/tenant.py's module docstring).
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_meta, _clean_str, _get_context
    from opencrab.ontology.builder import graph_write_failed
    from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, resolve_write_pack

    ctx = _get_context()
    principal = current_principal()
    # Billing identifiers only -- NOT stamps. The builder writes the ownership
    # keys; these two just label the metering event.
    tenant_id = "default"
    subject_id = principal.user_id
    space = _clean_str(space)
    node_type = _clean_str(node_type)
    node_id = _clean_str(node_id)
    # #148: no stamping here. The builder is the single stamper -- a second
    # one in this handler is why the same node written over MCP carried
    # tenant_id/created_by and the same node written over REST did not.
    props = _clean_meta(properties or {})
    target_pack_id = resolve_write_pack(ctx["sql"], principal, _clean_str(pack_id) if pack_id else None)
    try:
        result = ctx["builder"].add_node(
            space=space,
            node_type=node_type,
            node_id=node_id,
            properties=props,
            pack_id=target_pack_id,
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
    except (ValueError, NodeIdentityConflict) as exc:
        return {"error": str(exc), "valid": False}
    except PackNotFoundError:
        # #148: same wording/contract as pack_ingest (opencrab/mcp/tools/pack.py)
        # -- #143 invariant 7 folds "doesn't exist" and "someone else's
        # private pack" into one indistinguishable response.
        return {"error": "pack not found; use pack_create first", "pack_id": target_pack_id}
    except PackForbiddenError:
        # A visible (public) pack owned by someone else -- existence is
        # already observable, so this can safely say more than "not found".
        # No fork tool exists yet (see pack.py's pack_ingest/pack_publish) --
        # naming one here would send callers into an unknown-tool error.
        return {
            "error": "PACK_NOT_WRITABLE: not the pack owner",
            "pack_id": target_pack_id,
            "hint": (
                "this pack is readable but not writable by you; "
                "create your own with pack_create and ingest into that"
            ),
        }
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
                "pack_id": {
                    "type": "string",
                    "description": "Optional destination pack_id. Defaults to the caller's default pack.",
                },
            },
            "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
        },
    },
    order=2,
    access=AccessTier.WRITE,
    writes=True,
)
def ontology_add_edge(
    from_space: str,
    from_id: str,
    relation: str,
    to_space: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
    pack_id: str | None = None,
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
    pack_id:
        Optional destination pack_id. Defaults to the caller's default pack
        (``resolve_write_pack``).

    The writing subject is never a client-supplied argument (#145) -- it is
    the caller's server-derived ``current_principal()``. tenant_id stays
    fixed at 'default', matching ontology_add_node.
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_meta, _clean_str, _get_context
    from opencrab.ontology.builder import graph_write_failed
    from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, resolve_write_pack

    ctx = _get_context()
    principal = current_principal()
    tenant_id = "default"
    subject_id = principal.user_id
    from_id = _clean_str(from_id)
    to_id = _clean_str(to_id)
    relation = _clean_str(relation)
    target_pack_id = resolve_write_pack(ctx["sql"], principal, _clean_str(pack_id) if pack_id else None)
    try:
        result = ctx["builder"].add_edge(
            from_space=_clean_str(from_space),
            from_id=from_id,
            relation=relation,
            to_space=_clean_str(to_space),
            to_id=to_id,
            properties=_clean_meta(properties or {}),
            pack_id=target_pack_id,
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
    except PackNotFoundError:
        # #148: same wording/contract as pack_ingest (opencrab/mcp/tools/pack.py)
        # -- #143 invariant 7 folds "doesn't exist" and "someone else's
        # private pack" into one indistinguishable response.
        return {"error": "pack not found; use pack_create first", "pack_id": target_pack_id}
    except PackForbiddenError:
        # A visible (public) pack owned by someone else -- existence is
        # already observable, so this can safely say more than "not found".
        # No fork tool exists yet (see pack.py's pack_ingest/pack_publish) --
        # naming one here would send callers into an unknown-tool error.
        return {
            "error": "PACK_NOT_WRITABLE: not the pack owner",
            "pack_id": target_pack_id,
            "hint": (
                "this pack is readable but not writable by you; "
                "create your own with pack_create and ingest into that"
            ),
        }
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
    access=AccessTier.READ,
)
def ontology_get_node(node_id: str) -> dict[str, Any]:
    """Fetch a single node by node_id regardless of type, within the
    caller's readable pack scope.

    #147: this calls ``get_node_by_id_scoped``, not ``get_node_by_id``. The
    scoped predicate must run in the store query before any result limit.
    All four backends implement the scoped form (see
    opencrab/stores/_graph_protocol.py).
    """
    from opencrab.mcp.tools import _clean_str, _current_read_scope, _get_context

    ctx = _get_context()
    graph = ctx["neo4j"]
    node_id = _clean_str(node_id)
    # #147: scope at the store, not after the lookup result has been chosen.
    scope = _current_read_scope(ctx)
    result = graph.get_node_by_id_scoped(node_id, sorted(scope))

    # A node outside the scope returns the response an absent node returns.
    # Identical apart from the node_id echoed back, which is the caller's own
    # input and carries no information they did not already have (#143
    # invariant 7). There is no second branch here to tell the two cases
    # apart, which is the point -- a distinct "not permitted" path would be
    # free to drift into a distinguishable one.
    if result is None:
        return {"found": False, "node_id": node_id}
    # #55: same normalised fields ontology_list_nodes exposes (node_type/space/
    # properties), added alongside the pre-existing "node" key rather than
    # replacing it -- "node" is part of this tool's public MCP contract and at
    # least one existing test pins its exact shape (tests/test_mcp.py,
    # tests/test_mcp_dispatch_extended.py), so removing it would be a breaking
    # change for any external caller reading it. "properties" and "node" are
    # the same object (no copy), so a caller reading either sees identical
    # data.
    node_type = result.get("node_type", "")
    n_space = result.get("space", "")
    return {
        "found": True,
        "node_id": node_id,
        "node_type": node_type,
        "space": n_space,
        "node": result,
        "properties": result,
    }


@tool(
    "ontology_list_nodes",
    {
        "description": (
            "List nodes, optionally filtered by space and/or pack_id. Always lists from the "
            "graph store, whether or not pack_id is given (issue #55: this used to fall back "
            "to a separate doc store without pack_id, which could disagree with "
            "ontology_get_node -- both now read the same store). `total` in the response is "
            "the TRUE count of all matching nodes -- it is NOT capped by `limit` and can be "
            "larger than the number of `nodes` actually returned; if `total` exceeds "
            "len(nodes), the page was truncated and a larger `limit` will return more. Row "
            "order is not guaranteed. Useful for inspecting a pack's contents after ingest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "space": {"type": "string", "description": "Optional MetaOntology space filter (e.g. evidence, concept)."},
                "pack_id": {"type": "string", "description": "Optional pack_id filter."},
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of `nodes` rows returned (default 100). Does NOT cap "
                        "`total`, which is the full match count regardless of `limit`, whether "
                        "or not pack_id is given. 0 or negative values are not an error -- they "
                        "return an empty `nodes` page (issue #120). This server does not "
                        "validate `limit` against this schema before calling the handler, so a "
                        "negative value is never rejected outright; it is simply defined to "
                        "mean 'no rows'."
                    ),
                    "default": 100,
                },
            },
            "required": [],
        },
    },
    order=5,
    access=AccessTier.READ,
)
def ontology_list_nodes(
    space: str | None = None,
    pack_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List nodes filtered by space and/or pack_id.

    #147: every branch is pack-scoped now, and the calls below are the
    ``*_scoped`` variants -- ``count_exported_nodes_scoped`` /
    ``export_nodes_scoped``. They take the caller's readable pack set and
    match on ``pack_id`` ALONE; the older ``export_nodes``/
    ``count_exported_nodes`` also matched ``source``/``source_id``, which
    are caller-written and therefore unusable for an access decision (#143).

    #55: pack_id given or not, this always queries the graph store now.
    Before this fix, omitting ``pack_id`` fell back to the doc store
    (``ctx["mongo"].list_nodes_scoped``) instead -- a different store than
    ``ontology_get_node`` ever reads, so the two tools could disagree on
    whether a given node_id exists at all, and the doc-store branch
    returned raw, un-normalised rows while the pack_id branch returned the
    ``{node_id, node_type, space, properties}`` shape below. Both problems
    share one cause (two different stores answering two nominally
    equivalent reads) and one fix (one store, one code path). The graph
    store is the canonical choice: ``ontology_get_node`` already only reads
    it, and it already has the scoped single-node AND scoped-list read
    paths this tool needs (``get_node_by_id_scoped``,
    ``export_nodes_scoped``, ``count_exported_nodes_scoped``) -- making the
    doc store canonical instead would mean building an equivalent scoped
    single-node lookup for it first. The data itself can still disagree
    between stores (a node written to the doc store but never reaching the
    graph store, or vice versa) -- that is a separate, out-of-scope defect
    in how writes reach each store, not a defect in these two read tools;
    after this fix a node either shows up in both tools or neither, instead
    of splitting.

    This calls (in this order, matching the code below)
    ``count_exported_nodes_scoped(pack_ids, space=..., no LIMIT)`` first for
    ``total``, THEN ``export_nodes_scoped(pack_ids, space=..., limit=...)``
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
    LIMIT so ``total`` is the true match count. This is now also true
    without ``pack_id`` -- before #55, that branch's ``total`` was
    ``len(nodes)`` (doc-store page size, capped at ``limit``), a narrower
    version of the same #54 bug in a different subsystem. Row order is not
    guaranteed by either code path: ``export_nodes_scoped`` makes no
    ordering promise on any backend. (The old doc-store branch happened to
    be ordered on the SQL-backed doc stores -- ``_sql_doc_base.py``'s
    ``list_nodes_scoped`` runs ``ORDER BY updated_at DESC, space, node_id``
    -- but not on MongoStore, whose ``list_nodes_scoped`` never sorts. That
    was never a documented contract of this tool, so losing it here is not
    a breaking change, just a fact worth recording for anyone who observed
    it.)

    ``limit <= 0`` (issue #120): returns ``[]`` (and ``total`` is then
    ``0`` too, via the same guard in ``count_exported_nodes_scoped``).

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
    from opencrab.mcp.tools import _clean_str, _current_read_scope, _get_context
    from opencrab.pack.read_scope import narrow
    from opencrab.stores._graph_common import domain_labels

    ctx = _get_context()
    pack_id = _clean_str(pack_id) if pack_id else None
    cleaned_space = _clean_str(space) if space else None

    # #147: pack_id=None no longer means "no filter" -- it means "every pack
    # I can read". A named pack_id is intersected with that set, so naming
    # someone else's private pack lands on the same empty list as naming a
    # pack_id that was never created (#143 invariant 7).
    scope = _current_read_scope(ctx)
    effective, _ = narrow(scope, [pack_id] if pack_id else None)

    graph_store = ctx["neo4j"]
    # True match count, independent of `limit` (issue #54's core
    # requirement -- see this function's docstring).
    total = graph_store.count_exported_nodes_scoped(effective, space=cleaned_space)
    # Graph store: indexed/native pack_id + space filter → correct rows
    # before limit (all three backends implement the same contract, see
    # _graph_protocol.py#export_nodes).
    raw = graph_store.export_nodes_scoped(effective, limit=limit, space=cleaned_space)
    # export_nodes returns [{"props": dict, "labels": [str]}, ...]
    # normalise to a stable shape. The space check below is now redundant
    # with the backend's own filter (kept as cheap defense-in-depth, same
    # spirit as _expand()'s redundant pack_set check in
    # _sql_graph_base.py) rather than load-bearing.
    nodes: list[dict[str, Any]] = []
    for item in raw:
        props = item.get("props") or {}
        labels = item.get("labels") or []
        domains = domain_labels(labels)
        node_type = props.get("node_type") or (domains[0] if len(domains) == 1 else "")
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
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum results (default 200). Unlike ontology_list_nodes, 0 or "
                        "negative values here are NOT yet defined to return an empty page -- "
                        "export_edges' store-side guard for issue #120 is a separate, not-yet-"
                        "landed fix (tracked in a follow-up issue); a negative value's behavior "
                        "is backend-dependent today and may return the entire edge set. Do not "
                        "rely on 0/negative here until that follow-up lands."
                    ),
                    "default": 200,
                },
            },
            "required": [],
        },
    },
    order=6,
    access=AccessTier.READ,
)
def ontology_list_edges(
    pack_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """List edges, optionally filtered by pack_id.

    #147: routed through ``export_edges_scoped``, not ``export_edges``.
    The scoped predicate requires BOTH endpoints to be in the caller's
    readable set (plus the edge's own pack_id, when it has one) -- an edge
    row carries both endpoints' full properties, so one unreadable endpoint
    would disclose that node. ``export_edges``'s OR-any-endpoint matching,
    which also accepted caller-written ``source``/``source_id``, remains for
    pack export and is not an access decision.
    """
    from opencrab.mcp.tools import _clean_str, _current_read_scope, _get_context
    from opencrab.pack.read_scope import narrow

    ctx = _get_context()
    graph = ctx["neo4j"]
    pack_id = _clean_str(pack_id) if pack_id else None

    scope = _current_read_scope(ctx)
    effective, _ = narrow(scope, [pack_id] if pack_id else None)

    # #147: no hasattr() guard around export_edges_scoped, deliberately. The
    # old guard around export_edges was harmless because its fallback was a
    # plain "unavailable" message; here a fallback would mean quietly
    # reverting to the unscoped 5-way-OR path. A backend missing the method
    # is a wiring defect and must surface as one, so AttributeError is
    # re-raised rather than folded into the operational-error branch below.
    try:
        edges = graph.export_edges_scoped(effective, limit=limit)
        return {"edges": edges, "total": len(edges), "pack_id_filter": pack_id}
    except AttributeError:
        raise
    except Exception as exc:
        # Report the real failure instead of falling through to the
        # generic "unavailable" message, which would otherwise mask an
        # operational error as if the store didn't exist at all.
        logger.warning("export_edges_scoped failed: %s", exc)
        return {"edges": [], "total": 0, "error": str(exc), "pack_id_filter": pack_id}
