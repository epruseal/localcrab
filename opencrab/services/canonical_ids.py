"""Canonical identifier enrichment for query results.

``ontology_query`` returns items whose ``node_id`` came from vector metadata,
a Chroma document id, or a graph property — none of which prove a node with
that id exists in the graph. This module resolves each result against the
graph with an **exact** lookup and attaches what it found, so a caller can
address the hit by canonical (pack_id, node_id, node_type, space).

Two rules the implementation must never break:

1. Exact match only. If ``get_node_by_id`` returns nothing, the result is
   marked unresolved. A similar id is never searched for, scored, or
   substituted — a wrong-but-plausible id is worse than a declared miss.
2. Additive only. Existing keys (``node_id``, ``metadata``, ``graph_context``
   and its ``anchor_id``/``relation_type``) are left byte-identical; this
   module only adds ``canonical`` and ``graph_context["edge"]``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Node property keys that carry a document identity, in precedence order.
# Deliberately NOT including "source" (a file path on TextUnit nodes): turning
# a path into a document id is inference, not resolution.
_DOCUMENT_ID_KEYS = ("document_id", "source_id")


def _node_ref(props: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Canonical reference for one resolved node."""
    return {
        "node_id": props.get("id") or node_id,
        "node_type": props.get("node_type") or "",
        "space": props.get("space") or "",
        "pack_id": props.get("pack_id"),
    }


class _Resolver:
    """Per-call exact lookups with memoisation.

    Anchors repeat heavily across a result set (graph expansion emits many
    neighbours per anchor), so the cache turns a fan-out of edge-endpoint
    lookups back into one lookup per distinct node_id. ``None`` is cached too
    — a miss must not be retried.
    """

    def __init__(self, graph: Any, pack_ids: list[str]) -> None:
        self._graph = graph
        self._pack_ids = pack_ids
        self._cache: dict[str, dict[str, Any] | None] = {}

    def props(self, node_id: str) -> dict[str, Any] | None:
        if node_id in self._cache:
            return self._cache[node_id]
        try:
            # #147: the SCOPED lookup. Scoping the query legs is not enough
            # on its own -- this module re-reads the graph by node_id to
            # build the canonical block, so the scope predicate must be
            # applied by the store before the lookup result is returned.
            found = self._graph.get_node_by_id_scoped(node_id, self._pack_ids)
        except AttributeError:
            # A backend without the scoped method is a wiring defect, not a
            # miss. Swallowing it here would mark every result
            # "node_not_found" and look like a clean, empty enrichment.
            raise
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            logger.debug("canonical lookup failed for %s: %s", node_id, exc)
            found = None
        self._cache[node_id] = found
        return found

    def ref(self, node_id: str | None) -> dict[str, Any] | None:
        if not node_id:
            return None
        props = self.props(node_id)
        if props is None:
            return None
        return _node_ref(props, node_id)


def _canonical_for(resolver: _Resolver, item: dict[str, Any]) -> dict[str, Any]:
    node_id = item.get("node_id")
    if not node_id:
        return {"resolved": False, "reason": "missing_node_id"}

    props = resolver.props(node_id)
    if props is None:
        return {
            "resolved": False,
            "reason": "node_not_found",
            "requested_node_id": node_id,
        }

    canonical: dict[str, Any] = {"resolved": True, **_node_ref(props, node_id)}

    document_id = next(
        (props[key] for key in _DOCUMENT_ID_KEYS if props.get(key)), None
    )
    canonical["document_id"] = document_id
    unresolved = [] if document_id else ["document_id"]
    if canonical["pack_id"] is None:
        unresolved.append("pack_id")
    if unresolved:
        canonical["unresolved_fields"] = unresolved
    return canonical


def _edge_for(resolver: _Resolver, context: dict[str, Any]) -> dict[str, Any]:
    endpoints = context.get("edge_endpoints") or {}
    from_id = endpoints.get("from_id")
    to_id = endpoints.get("to_id")
    if not (from_id and to_id):
        # anchor_id is the BFS root, not the edge source — refusing to guess
        # here is the whole point (see _graph_protocol.find_neighbors).
        return {"resolved": False, "reason": "edge_endpoints_unavailable"}

    source = resolver.ref(from_id)
    target = resolver.ref(to_id)
    relation = context.get("relation_type") or ""
    if source is None or target is None:
        return {
            "resolved": False,
            "reason": "endpoint_not_found",
            "source_id": from_id,
            "target_id": to_id,
            "relation": relation,
        }
    return {
        "resolved": True,
        "source": source,
        "relation": relation,
        "target": target,
    }


def enrich(
    graph: Any,
    results: list[dict[str, Any]],
    resolve_edges: bool = True,
    *,
    pack_ids: list[str],
) -> list[dict[str, Any]]:
    """Attach canonical ids to ``results`` in place; returns the same list.

    ``results`` are ``QueryResult.to_dict()`` shapes. A store that is missing
    or unavailable is a no-op, not an error: enrichment never degrades the
    query it decorates.

    ``pack_ids`` is REQUIRED (#147) and bounds every lookup this makes,
    including both endpoints of an edge. A node outside it resolves to the
    module's existing ``node_not_found`` shape -- the same one a genuinely
    absent id gets -- so rule 1 of this module ("exact match only, a miss is
    declared as a miss") already covers the out-of-scope case without a
    second branch to keep in sync.

    Pass the caller's FULL readable scope, not the pack filter that
    produced these results. Canonical identity is "what is this id", not
    "what did this query search": narrowing it to the query filter would
    make the same node resolve in one query and report ``node_not_found``
    in another, which defeats the stable-address contract this module
    exists for. Authorization is unaffected either way -- the query filter
    is always a subset of the scope.
    """
    if graph is None or not getattr(graph, "available", False) or not results:
        return results

    resolver = _Resolver(graph, pack_ids)
    for item in results:
        item["canonical"] = _canonical_for(resolver, item)
        context = item.get("graph_context")
        if resolve_edges and isinstance(context, dict):
            context["edge"] = _edge_for(resolver, context)
    return results
