"""Query and analysis tools: hybrid search, impact analysis, lever simulation.

See graph.py's module docstring for why ``_get_context`` is imported at
function scope inside each handler rather than at module level (required for
``patch("opencrab.mcp.tools._get_context")`` to keep taking effect).
"""

from __future__ import annotations

import logging
from typing import Any

from ._registry import tool

logger = logging.getLogger(__name__)


@tool(
    "ontology_query",
    {
        "description": (
            "Hybrid vector + BM25 + graph search with RRF reranking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Natural language query."},
                "spaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of spaces to filter results.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default 10).",
                    "default": 10,
                },
                "use_bm25": {
                    "type": "boolean",
                    "description": "Include BM25 keyword results (default true).",
                    "default": True,
                },
                "use_fts": {
                    "type": "boolean",
                    "description": "Include FTS5 doc-body keyword results when the doc store supports it (default true).",
                    "default": True,
                },
                "use_rerank": {
                    "type": "boolean",
                    "description": "Apply RRF + BM25 cross-score reranking (default true).",
                    "default": True,
                },
                "pack_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict retrieval to one or more pack_ids. Wins over auto_pack.",
                },
                "auto_pack": {
                    "type": "boolean",
                    "description": "Pick the most relevant pack from the local registry (deterministic).",
                    "default": False,
                },
                "include_unpackaged": {
                    "type": "boolean",
                    "description": "Include items with no pack_id when pack filtering is active.",
                    "default": False,
                },
                "include_pack_provenance": {
                    "type": "boolean",
                    "description": "Embed selected_packs / pack_filter / metadata.pack_id in the response.",
                    "default": True,
                },
                "include_canonical_ids": {
                    "type": "boolean",
                    "description": "Resolve each result against the graph (exact lookup) and attach canonical pack_id/node_id/node_type/space/document_id, plus canonical source/relation/target for graph edges. Unresolvable ids are reported as unresolved, never substituted.",
                    "default": True,
                },
            },
            "required": ["question"],
        },
    },
    order=3,
)
def ontology_query(
    question: str,
    spaces: list[str] | None = None,
    limit: int = 10,
    use_bm25: bool = True,
    use_rerank: bool = True,
    use_fts: bool = True,
    pack_ids: list[str] | None = None,
    auto_pack: bool = False,
    include_unpackaged: bool = False,
    include_pack_provenance: bool = True,
    include_canonical_ids: bool = True,
) -> dict[str, Any]:
    """
    Run a hybrid vector + BM25 + graph query against the ontology.

    Pipeline: vector similarity → BM25 keyword → graph expansion → RRF
    reranking. No policy-aware (ReBAC) filter runs here: HybridQuery's
    ``subject_id`` param drives ``_policy_filter``, which is fail-closed
    (ReBACEngine.check returns granted=False for any unpolicied node), so
    wiring the caller's principal into it would silently drop every result
    with no explicit grant (#143's explicit prohibition). subject_id is
    therefore never forwarded to ``hybrid.query`` here, client-supplied or
    not -- #145 removed it as a tool argument entirely.

    Parameters
    ----------
    question:
        Natural language question or keyword query.
    spaces:
        Optional list of space IDs to restrict the search.
    limit:
        Maximum number of results.
    use_bm25:
        Include BM25 keyword results (default True).
    use_rerank:
        Apply RRF + BM25 cross-score reranking (default True).
    pack_ids:
        Optional list of pack_ids to scope retrieval. Takes precedence over
        auto_pack.
    auto_pack:
        When True (and pack_ids is empty), pick the most relevant pack from
        the local registry using deterministic keyword scoring.
    include_unpackaged:
        When pack filtering is active, also surface items with no pack_id
        (legacy data). Endpoint-failed edges are still suppressed.
    include_pack_provenance:
        Embed ``metadata.pack_id`` and ``selected_packs``/``pack_filter`` in
        the response (default True). Set to False for the bare legacy shape.
    include_canonical_ids:
        Resolve every result against the graph with an exact ``node_id``
        lookup and attach a ``canonical`` block (plus ``graph_context.edge``
        for graph hits). Costs at most one indexed single-row lookup per
        distinct node id in the returned page; set False to skip it entirely.
    """
    from opencrab.auth import current_principal
    from opencrab.config import get_settings
    from opencrab.mcp.tools import _get_context
    from opencrab.services.canonical_ids import enrich as enrich_canonical
    from opencrab.services.pack_selection import mcp_warning_text, resolve_packs

    ctx = _get_context()
    cfg = get_settings()
    principal = current_principal()
    tenant_id = "default"
    selection = resolve_packs(
        question,
        list(pack_ids) if pack_ids else None,
        auto_pack,
        include_unpackaged,
        cfg.local_data_dir,
        raise_on_error=False,
    )
    effective_pack_ids = selection.effective_pack_ids
    selected_packs = selection.selected_packs
    auto_pack = selection.auto_pack_active
    pack_filter_warnings = [mcp_warning_text(w) for w in selection.warnings]

    try:
        outcome = ctx["hybrid"].query(
            question=question,
            spaces=spaces,
            limit=limit,
            use_bm25=use_bm25,
            use_rerank=use_rerank,
            use_fts=use_fts,
            pack_ids=effective_pack_ids,
            include_unpackaged=include_unpackaged,
        )
        results = outcome.results
        # This is a store write (billing_events INSERT), but ontology_query stays
        # writes=False. NOT because the insert is idempotent (issue #105: it
        # isn't -- BillingHooks.emit mints a fresh UUID per call, so
        # UNIQUE(event_id) + INSERT OR IGNORE / ON CONFLICT DO NOTHING only
        # dedupes a literal double-send, never resurrects a failed one), and
        # NOT because losing this event would be an acceptable performance
        # trade (protecting it IS the correctness goal #105 exists for).
        # writes=False is safe here because billing_events lives in its own
        # SQLite file (issue #105: opencrab.billing.hooks module docstring +
        # opencrab.stores.factory.make_billing_sql_store) separate from the
        # write.lock'd tables' file -- a billing insert here never contends
        # with an unrelated writer no matter how long that writer holds
        # write.lock, so forcing writes=True would only cost every query --
        # a high-frequency, read-shaped path -- lock contention for no
        # correctness gain. Decided in #65's review against #68 (E-4 lock
        # ownership map); see `writes` field docstring in
        # _registry.py#tool for the general rule.
        billing_result = ctx["billing"].on_query(tenant_id, principal.user_id, question)
        if not billing_result.get("ok"):
            # #105: emit() is fire-and-forget by design and never raises, but a
            # failed persist must not vanish with only BillingHooks' own internal
            # log line -- surface it here too. Does not fail the query: the
            # query already succeeded above.
            logger.warning(
                "on_query billing event failed to persist (tenant=%s, subject=%s): %s",
                tenant_id, principal.user_id, billing_result.get("error"),
            )
        result_dicts = [r.to_dict() for r in results]
        if include_canonical_ids:
            # .get: a context without a graph store (test doubles, degraded
            # deployments) must not turn a good query into an error.
            enrich_canonical(ctx.get("neo4j"), result_dicts)
        response: dict[str, Any] = {
            "question": question,
            "spaces_filter": spaces,
            "subject_id": principal.user_id,
            "tenant_id": tenant_id,
            "pipeline": {"bm25": use_bm25, "rerank": use_rerank, "fts": use_fts},
            "total": len(results),
            "results": result_dicts,
        }
        # #51: spaces 필터가 벡터 leg 에서 조용히 0건이 되는 과도기 상태를 응답에
        # 드러낸다(호출자가 "결과 없음"과 "필터 적용 불가"를 구분할 수 있도록).
        # outcome.warnings 는 query() 호출마다 새로 만들어지는 지역 반환값이라
        # 동시 요청 간 공유 상태 경합이 없다(QueryOutcome 참조).
        if outcome.warnings:
            response["spaces_filter_warnings"] = list(outcome.warnings)
        if include_pack_provenance:
            response["selected_packs"] = selected_packs
            response["pack_filter"] = {
                "pack_ids": effective_pack_ids,
                "auto_pack": bool(auto_pack),
                "include_unpackaged": bool(include_unpackaged),
            }
            if pack_filter_warnings:
                response["pack_filter"]["warnings"] = pack_filter_warnings
        return response
    except Exception as exc:
        logger.error("ontology_query failed: %s", exc)
        return {"error": str(exc)}


@tool(
    "ontology_impact",
    {
        "description": "Analyse the I1–I7 impact of a change to a node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "ID of the node being changed."},
                "change_type": {
                    "type": "string",
                    "description": "Type of change: create, update, delete, etc.",
                    "default": "update",
                },
            },
            "required": ["node_id"],
        },
    },
    order=7,
    # #65: analyse() persists a row via ImpactEngine -> save_impact() (SQL
    # INSERT into impact_records), so this "analysis" tool must be lock-covered
    # like any other write despite its read-shaped name.
    writes=True,
)
def ontology_impact(
    node_id: str,
    change_type: str = "update",
) -> dict[str, Any]:
    """
    Analyse the impact of a change to a specific node.

    Returns which impact categories (I1–I7) are triggered,
    which neighbouring nodes are affected, and a human-readable summary.

    Parameters
    ----------
    node_id:
        ID of the node being changed.
    change_type:
        Nature of the change: create, update, delete, permission_change,
        relationship_add, relationship_remove, bulk_import.
    """
    from opencrab.mcp.tools import _get_context

    ctx = _get_context()
    try:
        result = ctx["impact"].analyse(node_id=node_id, change_type=change_type)
        return result.to_dict()
    except Exception as exc:
        logger.error("ontology_impact failed: %s", exc)
        return {"error": str(exc)}


@tool(
    "ontology_lever_simulate",
    {
        "description": "Simulate downstream outcome changes from a lever movement.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lever_id": {"type": "string", "description": "ID of the Lever node."},
                "direction": {
                    "type": "string",
                    "description": "Direction: raises, lowers, stabilizes, optimizes.",
                },
                "magnitude": {
                    "type": "number",
                    "description": "Strength of the lever movement (0.0–1.0).",
                },
            },
            "required": ["lever_id", "direction", "magnitude"],
        },
    },
    order=8,
    # #65: lever_simulate() persists a row via ImpactEngine -> save_simulation()
    # (SQL INSERT into lever_simulations); same rationale as ontology_impact above.
    writes=True,
)
def ontology_lever_simulate(
    lever_id: str,
    direction: str,
    magnitude: float,
) -> dict[str, Any]:
    """
    Simulate the downstream effects of moving a lever.

    Predicts changes to connected Outcome nodes and affected Concepts
    based on the current graph structure.

    Parameters
    ----------
    lever_id:
        ID of the Lever node.
    direction:
        One of: raises, lowers, stabilizes, optimizes.
    magnitude:
        Strength of the lever movement (recommended 0.0–1.0).
    """
    from opencrab.mcp.tools import _get_context

    ctx = _get_context()
    try:
        return ctx["impact"].lever_simulate(
            lever_id=lever_id,
            direction=direction,
            magnitude=float(magnitude),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("ontology_lever_simulate failed: %s", exc)
        return {"error": str(exc)}
