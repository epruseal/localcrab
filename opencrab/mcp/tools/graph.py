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
                        "IS capped at `limit` (total == len(nodes) always in that case). "
                        "0 or negative values are not an error -- they return an empty `nodes` "
                        "page (issue #120). This server does not validate `limit` against this "
                        "schema before calling the handler, so a negative value is never "
                        "rejected outright; it is simply defined to mean 'no rows'."
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
    ``export_nodes_scoped`` (graph) and ``list_nodes_scoped`` (doc store).
    They take the caller's readable pack set and match on ``pack_id``
    ALONE; the older ``export_nodes``/``count_exported_nodes`` also matched
    ``source``/``source_id``, which are caller-written and therefore
    unusable for an access decision (#143).

    WITH pack_id: this calls (in this order, matching the code below) the
    graph store's count_exported_nodes_scoped(pack_ids, space=..., no LIMIT)
    first for ``total``, THEN export_nodes_scoped(pack_ids, space=..., limit=...)
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

    ``limit <= 0`` (issue #120 follow-up): both branches return ``[]`` (and
    doc-store's ``total`` is then ``0`` too, since it's ``len(nodes)`` in
    that branch) -- the WITH-pack_id and WITHOUT-pack_id paths agree here
    even though they disagree on ``total`` semantics above. This wasn't
    true for every doc-store backend when the WITH-pack_id side of this
    contract first landed: MongoStore's ``list_nodes`` passed ``limit``
    straight to pymongo's ``Cursor.limit()``, where ``0`` means "no limit"
    (the opposite of this contract) -- same footgun class as SQLite mapping
    a bound ``LIMIT -1`` to "no limit" in ``_sql_doc_base.py``, just
    triggered by a different value. All three doc-store backends
    (LocalSQLDocStore/PgDocStore via ``_sql_doc_base.py``, MongoStore) now
    guard ``limit <= 0`` before querying, matching the graph store side.

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

    nodes: list[dict[str, Any]] = []
    total = 0

    if pack_id:
        graph_store = ctx["neo4j"]
        # True match count, independent of `limit` (issue #54's core
        # requirement -- see this function's docstring).
        total = graph_store.count_exported_nodes_scoped(effective, space=cleaned_space)
        # Graph store: indexed/native pack_id + space filter → correct rows
        # before limit (all three backends implement the same contract, see
        # _graph_protocol.py#export_nodes).
        raw = graph_store.export_nodes_scoped(effective, limit=limit, space=cleaned_space)
        # export_nodes returns [{"props": dict, "labels": [str]}, ...]
        # normalise to same shape as doc store list_nodes. The space check
        # below is now redundant with the backend's own filter (kept as
        # cheap defense-in-depth, same spirit as _expand()'s redundant
        # pack_set check in _sql_graph_base.py) rather than load-bearing.
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
    else:
        # Doc store fallback (no pack_id named). #147: still scoped -- the
        # doc store gets the readable set explicitly rather than the
        # unfiltered list_nodes it used to call. Data source is unchanged on
        # purpose: switching this branch to the graph store would also change
        # which rows and which `total` semantics callers see, and that is not
        # what this issue is for.
        nodes = ctx["mongo"].list_nodes_scoped(effective, space=cleaned_space, limit=limit)
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
