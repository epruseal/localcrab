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
        ctx["billing"].on_node_write(tenant_id, subject_id, space, node_type)
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
            },
            "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
        },
    },
    order=2,
)
def ontology_add_edge(
    from_space: str,
    from_id: str,
    relation: str,
    to_space: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
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
    """
    from opencrab.mcp.tools import _clean_meta, _clean_str, _get_context

    ctx = _get_context()
    from_id = _clean_str(from_id)
    to_id = _clean_str(to_id)
    try:
        result = ctx["builder"].add_edge(
            from_space=_clean_str(from_space),
            from_id=from_id,
            relation=_clean_str(relation),
            to_space=_clean_str(to_space),
            to_id=to_id,
            properties=_clean_meta(properties or {}),
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
            "List nodes from the doc store, optionally filtered by space and/or pack_id. "
            "Useful for inspecting a pack's contents after ingest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "space": {"type": "string", "description": "Optional MetaOntology space filter (e.g. evidence, concept)."},
                "pack_id": {"type": "string", "description": "Optional pack_id filter."},
                "limit": {"type": "integer", "description": "Maximum results (default 100).", "default": 100},
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

    When pack_id is given, queries the graph store's export_nodes(pack_id=...)
    (all four backends implement it — see opencrab/stores/_graph_protocol.py)
    which uses an indexed/native pack_id filter — avoids the limit-before-
    filter bug that would occur if we fetched N rows then Python-filtered.
    When pack_id is absent, falls back to the doc store's list_nodes.
    """
    from opencrab.mcp.tools import _clean_str, _get_context

    ctx = _get_context()
    pack_id = _clean_str(pack_id) if pack_id else None
    cleaned_space = _clean_str(space) if space else None

    nodes: list[dict[str, Any]] = []

    if pack_id:
        # Graph store: indexed/native pack_id filter → correct count before limit
        raw = ctx["neo4j"].export_nodes(pack_id=pack_id, limit=limit)
        # export_nodes returns [{"props": dict, "labels": [str]}, ...]
        # normalise to same shape as doc store list_nodes
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

    return {
        "nodes": nodes,
        "total": len(nodes),
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
