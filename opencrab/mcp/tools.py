"""
MCP Tool Definitions for OpenCrab / LocalCrab.

Each tool is a plain function that accepts keyword arguments and returns
a JSON-serialisable dict. The TOOLS registry maps tool names to their
schema (for tools/list) and their implementation function.

Exposed tools (16):
  ── Grammar ────────────────────────────────────────────────────────────
  1.  ontology_manifest         — full grammar as JSON
  ── Graph write ────────────────────────────────────────────────────────
  2.  ontology_add_node         — add/update a node (grammar-validated)
  3.  ontology_add_edge         — add/update an edge (grammar-validated)
  ── Retrieval / read ───────────────────────────────────────────────────
  4.  ontology_query            — hybrid vector + BM25 + graph search
  5.  ontology_get_node         — fetch a single node by node_id
  6.  ontology_list_nodes       — list nodes filtered by space / pack_id
  7.  ontology_list_edges       — list edges filtered by pack_id
  ── Analysis ───────────────────────────────────────────────────────────
  8.  ontology_impact           — I1–I7 impact analysis
  9.  ontology_lever_simulate   — predict outcome changes from lever movement
  ── Pack management ────────────────────────────────────────────────────
  10. content_pack_list         — list loaded packs (pack_id, node count, title)
  11. schema_pack_list          — list available schema packs
  12. schema_pack_install       — install a domain schema pack
  13. schema_pack_uninstall     — uninstall a schema pack
  14. pack_create               — create a new ontology pack
  15. pack_ingest               — add content to an existing pack
  ── Execution / harness ────────────────────────────────────────────────
  16. harness_promotion_apply   — apply a CrabHarness PromotionPackage

비노출(주석 처리, 코드 보존, 주석 해제로 즉시 복원):
  query_bm25, ontology_rebac_check, workflow_create_run, workflow_advance,
  approval_request, billing_get_usage, billing_list_events,
  identity_*(5), canonicalize_*(2), promotion_*(4),
  ontology_extract, ontology_ingest
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _clean_str(s: str) -> str:
    """Strip surrogate characters introduced by Windows MCP pipeline encoding."""
    if not isinstance(s, str):
        return str(s)
    return s.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize metadata dict — remove surrogates from string values."""
    result: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, str):
            result[_clean_str(k)] = _clean_str(v)
        elif isinstance(v, dict):
            result[_clean_str(k)] = _clean_meta(v)
        else:
            result[_clean_str(k)] = v
    return result


# ---------------------------------------------------------------------------
# Store / engine singletons (lazily initialised)
# ---------------------------------------------------------------------------
# These are populated by _get_context() which is called on first tool use.
# This design avoids importing heavy dependencies at module load time.

_context: dict[str, Any] = {}


def _get_context() -> dict[str, Any]:
    """Lazily initialise LocalCrab stores and engines using the local factory."""
    global _context
    if _context:
        return _context

    from opencrab.config import get_settings
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.query import HybridQuery
    from opencrab.ontology.rebac import ReBACEngine
    from opencrab.stores.factory import make_doc_store, make_graph_store, make_sql_store, make_vector_store

    cfg = get_settings()

    graph = make_graph_store(cfg)
    vector = make_vector_store(cfg)
    docs = make_doc_store(cfg)
    sql = make_sql_store(cfg)

    builder = OntologyBuilder(graph, docs, sql, vec=vector)
    rebac = ReBACEngine(graph, sql)
    impact = ImpactEngine(graph, sql)
    hybrid = HybridQuery(vector, graph)

    # Attach Phase 4 dependencies to HybridQuery for BM25 + policy filter
    hybrid._doc_store = docs
    hybrid._rebac = rebac

    # Phase 5: billing hooks
    from opencrab.billing.hooks import BillingHooks
    billing = BillingHooks(sql)

    _context = {
        "neo4j": graph,
        "chroma": vector,
        "mongo": docs,
        "sql": sql,
        "builder": builder,
        "rebac": rebac,
        "impact": impact,
        "hybrid": hybrid,
        "billing": billing,
    }
    return _context


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def ontology_manifest() -> dict[str, Any]:
    """
    Return the full MetaOntology OS grammar.

    Includes spaces, meta-edges, impact categories, active metadata
    layers, and ReBAC configuration.
    """
    from opencrab.grammar.validator import describe_grammar

    return describe_grammar()


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
        )
        ctx["billing"].on_node_write(tenant_id, subject_id, space, node_type)
        ctx["hybrid"].invalidate_bm25_cache()
        return result
    except ValueError as exc:
        return {"error": str(exc), "valid": False}
    except Exception as exc:
        logger.error("ontology_add_node failed: %s", exc)
        return {"error": str(exc)}


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


def ontology_query(
    question: str,
    spaces: list[str] | None = None,
    limit: int = 10,
    subject_id: str | None = None,
    tenant_id: str = "default",
    use_bm25: bool = True,
    use_rerank: bool = True,
    pack_ids: list[str] | None = None,
    auto_pack: bool = False,
    include_unpackaged: bool = False,
    include_pack_provenance: bool = True,
) -> dict[str, Any]:
    """
    Run a hybrid vector + BM25 + graph query against the ontology.

    Pipeline: vector similarity → BM25 keyword → graph expansion →
    RRF reranking → policy-aware filter (if subject_id provided).

    Parameters
    ----------
    question:
        Natural language question or keyword query.
    spaces:
        Optional list of space IDs to restrict the search.
    limit:
        Maximum number of results.
    subject_id:
        If set, filters results to only nodes the subject can view (ReBAC).
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
    """
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import choose_packs, load_pack_registry

    ctx = _get_context()
    selected_packs: list[dict[str, Any]] = []
    effective_pack_ids: list[str] | None = list(pack_ids) if pack_ids else None
    pack_filter_warnings: list[str] = []

    if effective_pack_ids and auto_pack:
        pack_filter_warnings.append("pack_ids provided; ignoring auto_pack")
        auto_pack = False

    if auto_pack:
        try:
            cfg = get_settings()
            registry = load_pack_registry(cfg.local_data_dir)
            candidates = choose_packs(question, registry, limit=1)
            if candidates:
                pack, score, matched = candidates[0]
                effective_pack_ids = [pack.pack_id]
                selected_packs.append(
                    {"pack_id": pack.pack_id, "score": score, "matched": matched}
                )
            else:
                pack_filter_warnings.append(
                    "auto_pack could not select a pack above the score threshold; "
                    "falling back to full-store search"
                )
        except Exception as exc:
            logger.warning("auto_pack selection failed: %s", exc)
            pack_filter_warnings.append(f"auto_pack failed: {exc}")

    if include_unpackaged and not effective_pack_ids:
        pack_filter_warnings.append(
            "include_unpackaged has no effect without pack_ids/auto_pack"
        )

    try:
        results = ctx["hybrid"].query(
            question=question,
            spaces=spaces,
            limit=limit,
            subject_id=subject_id,
            use_bm25=use_bm25,
            use_rerank=use_rerank,
            pack_ids=effective_pack_ids,
            include_unpackaged=include_unpackaged,
        )
        ctx["billing"].on_query(tenant_id, subject_id, question)
        response: dict[str, Any] = {
            "question": question,
            "spaces_filter": spaces,
            "subject_id": subject_id,
            "tenant_id": tenant_id,
            "pipeline": {"bm25": use_bm25, "rerank": use_rerank},
            "total": len(results),
            "results": [r.to_dict() for r in results],
        }
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


def query_bm25(
    question: str,
    spaces: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Run a BM25-only keyword search against ontology node properties.

    Faster than hybrid query for precise keyword lookups.
    Indexes node_id, name, description, text, title, summary fields.

    Parameters
    ----------
    question:
        Search keywords.
    spaces:
        Optional space filter.
    limit:
        Maximum results.
    """
    from opencrab.ontology.bm25 import BM25Index
    from opencrab.ontology.query import _BM25_NODE_LIMIT

    ctx = _get_context()
    doc_store = ctx["mongo"]
    try:
        nodes = doc_store.list_nodes(limit=_BM25_NODE_LIMIT)
        index = BM25Index.build(nodes)
        hits = index.search(question, spaces=spaces, limit=limit)
        return {
            "question": question,
            "index_size": len(index),
            "total": len(hits),
            "results": hits,
        }
    except Exception as exc:
        logger.error("query_bm25 failed: %s", exc)
        return {"error": str(exc)}


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
    ctx = _get_context()
    try:
        result = ctx["impact"].analyse(node_id=node_id, change_type=change_type)
        return result.to_dict()
    except Exception as exc:
        logger.error("ontology_impact failed: %s", exc)
        return {"error": str(exc)}


def ontology_rebac_check(
    subject_id: str,
    permission: str,
    resource_id: str,
) -> dict[str, Any]:
    """
    Check whether a subject has a given permission over a resource.

    Uses ReBAC (Relationship-Based Access Control): checks explicit SQL
    policies first, then traverses the graph for relationship-based access.

    Parameters
    ----------
    subject_id:
        ID of the subject (User, Team, Org, Agent).
    permission:
        One of: view, edit, execute, simulate, approve, admin.
    resource_id:
        ID of the resource being accessed.
    """
    ctx = _get_context()
    try:
        decision = ctx["rebac"].check(
            subject_id=subject_id,
            permission=permission,
            resource_id=resource_id,
        )
        return decision.to_dict()
    except Exception as exc:
        logger.error("ontology_rebac_check failed: %s", exc)
        return {"error": str(exc), "granted": False}


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


def ontology_extract(
    text: str,
    source_id: str,
    model: str = "claude-haiku-4-5-20251001",
    backend: str = "auto",
) -> dict[str, Any]:
    """
    LLM-extract ontology nodes and edges from text and write to the graph.

    Uses Claude to identify entities and relationships according to the
    9-Space MetaOntology grammar, then persists them.

    Parameters
    ----------
    text:
        Raw text to extract knowledge from.
    source_id:
        Stable identifier for this source (e.g. file path or URL).
    model:
        Claude model to use for extraction (API backend only).
    backend:
        'auto' (default) — use API if ANTHROPIC_API_KEY is set, else fall back
        to the locally-installed `claude -p` CLI (subscription auth, no key needed).
        'api'  — Anthropic SDK (requires ANTHROPIC_API_KEY).
        'cli'  — `claude -p` subprocess (uses existing Claude Code subscription).
    """
    from opencrab.ontology.extractor import LLMExtractor

    text = _clean_str(text)
    source_id = _clean_str(source_id)

    ctx = _get_context()

    try:
        extractor = LLMExtractor(model=model, backend=backend)
        result = extractor.extract_from_text(text, source_id=source_id)

        added_nodes = 0
        added_edges = 0
        node_errors: list[str] = []
        edge_errors: list[str] = []

        for node in result.nodes:
            try:
                ctx["builder"].add_node(
                    space=node.space,
                    node_type=node.node_type,
                    node_id=node.node_id,
                    properties=node.properties,
                )
                added_nodes += 1
            except Exception as exc:
                node_errors.append(f"{node.node_id}: {exc}")

        for edge in result.edges:
            try:
                ctx["builder"].add_edge(
                    from_space=edge.from_space,
                    from_id=edge.from_id,
                    relation=edge.relation,
                    to_space=edge.to_space,
                    to_id=edge.to_id,
                    properties=edge.properties,
                )
                added_edges += 1
            except Exception as exc:
                edge_errors.append(f"{edge.from_id}→{edge.to_id}: {exc}")

        return {
            "source_id": source_id,
            "extracted_nodes": len(result.nodes),
            "extracted_edges": len(result.edges),
            "added_nodes": added_nodes,
            "added_edges": added_edges,
            "extraction_errors": result.errors,
            "node_errors": node_errors,
            "edge_errors": edge_errors,
        }
    except Exception as exc:
        logger.error("ontology_extract failed: %s", exc)
        return {"error": str(exc)}


def ontology_ingest(
    text: str,
    source_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Ingest a text document into the vector store.

    The text is embedded and stored in ChromaDB. A source record is
    also created in MongoDB if available.

    Parameters
    ----------
    text:
        The text content to ingest.
    source_id:
        Stable unique identifier for this source document.
    metadata:
        Optional metadata (e.g. space, node_id, author, created_at).
    """
    ctx = _get_context()
    text = _clean_str(text)
    source_id = _clean_str(source_id)
    meta = _clean_meta(metadata or {})
    result: dict[str, Any] = {"source_id": source_id, "stores": {}}

    # Ingest into vector store
    try:
        vector_result = ctx["hybrid"].ingest(text=text, source_id=source_id, metadata=meta)
        result["stores"].update(vector_result.get("stores", {}))
        if "vector_id" in vector_result:
            result["vector_id"] = vector_result["vector_id"]
    except Exception as exc:
        result["stores"]["chromadb"] = f"error: {exc}"

    # Ingest into MongoDB
    mongo: Any = ctx["mongo"]
    if mongo.available:
        try:
            mongo_id = mongo.upsert_source(source_id, text, meta)
            result["stores"]["mongodb"] = f"ok (id={mongo_id})"
        except Exception as exc:
            result["stores"]["mongodb"] = f"error: {exc}"
    else:
        result["stores"]["mongodb"] = "unavailable"

    result["text_length"] = len(text)
    result["metadata"] = meta
    return result


def workflow_create_run(
    action_type: str,
    payload: dict[str, Any],
    subject_id: str | None = None,
) -> dict[str, Any]:
    """
    Create a new workflow run in 'pending' state.

    Use this to start an auditable action workflow before executing it.
    Returns run_id, receipt_id, and status='pending'.

    Parameters
    ----------
    action_type:
        The action being requested (e.g. 'add_node', 'harness_apply').
    payload:
        The full action payload (will be stored for audit).
    subject_id:
        Optional identifier of the actor initiating the run.
    """
    from opencrab.execution.workflow import WorkflowEngine

    ctx = _get_context()
    engine = WorkflowEngine(ctx["sql"])
    return engine.create_run(action_type, payload, subject_id)


def workflow_advance(
    run_id: str,
    new_status: str,
    output: dict[str, Any] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """
    Advance a workflow run to a new status.

    Valid statuses: pending, running, approved, rejected, completed, failed.
    Each advance is appended to the action_log for full provenance.

    Parameters
    ----------
    run_id:
        The workflow run to advance.
    new_status:
        Target status.
    output:
        Optional output/result dict to log.
    actor:
        Optional identifier of who triggered this transition.
    """
    from opencrab.execution.workflow import WorkflowEngine

    ctx = _get_context()
    engine = WorkflowEngine(ctx["sql"])
    return engine.advance(run_id, new_status, output, actor)


def approval_request(
    action_type: str,
    subject_id: str,
    payload: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Submit an approval request for a sensitive action.

    Creates a pending approval entry that must be resolved (approved/rejected)
    before the action is executed. Returns approval_id and status='pending'.

    Parameters
    ----------
    action_type:
        The type of action requiring approval.
    subject_id:
        The subject requesting the action.
    payload:
        The full action payload to be reviewed.
    run_id:
        Optional workflow run_id this approval is linked to.
    """
    from opencrab.execution.approvals import ApprovalEngine

    ctx = _get_context()
    engine = ApprovalEngine(ctx["sql"])
    return engine.request(action_type, subject_id, payload, run_id)


# ---------------------------------------------------------------------------
# Phase 3: Identity / Canonicalization / Promotion tools
# ---------------------------------------------------------------------------


def identity_add_alias(
    canonical_id: str,
    alias_id: str,
    alias_type: str = "name",
    space: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """
    Register alias_id as an alias for canonical_id.

    alias_type hints: 'name' (same name, diff ID), 'merge' (confirmed merge),
    'external' (same entity from external source).
    """
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    engine = IdentityEngine(ctx["sql"])
    return engine.add_alias(canonical_id, alias_id, alias_type, space, created_by)


def identity_resolve_canonical(node_id: str) -> dict[str, Any]:
    """
    Resolve node_id to its canonical ID.

    If node_id is an alias, returns the canonical. Otherwise returns node_id unchanged.
    """
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    engine = IdentityEngine(ctx["sql"])
    canonical = engine.resolve_canonical(node_id)
    return {"node_id": node_id, "canonical_id": canonical, "is_alias": canonical != node_id}


def identity_propose_duplicate(
    node_a_id: str,
    node_b_id: str,
    space: str | None = None,
    similarity: float | None = None,
    method: str = "name_fuzzy",
) -> dict[str, Any]:
    """
    Propose that two nodes may be the same entity.

    Creates a pending duplicate candidate for human review. Returns early if
    the pair is already proposed.
    """
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    engine = IdentityEngine(ctx["sql"])
    return engine.propose_duplicate(node_a_id, node_b_id, space, similarity, method)


def identity_resolve_duplicate(
    candidate_id: str,
    decision: str,
    decided_by: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Accept or reject a pending duplicate candidate.

    decision: 'accepted' | 'rejected'
    If accepted, automatically registers node_b as alias of node_a.
    """
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    engine = IdentityEngine(ctx["sql"])
    try:
        return engine.resolve_duplicate(candidate_id, decision, decided_by, note)
    except ValueError as exc:
        return {"error": str(exc)}


def identity_list_pending_duplicates(limit: int = 50) -> dict[str, Any]:
    """Return all pending duplicate candidates sorted by similarity descending."""
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    engine = IdentityEngine(ctx["sql"])
    candidates = engine.list_pending_candidates(limit)
    return {"total": len(candidates), "candidates": candidates}


def canonicalize_merge_nodes(
    canonical_id: str,
    alias_id: str,
    canonical_space: str,
    canonical_type: str,
    merge_properties: bool = True,
    merged_by: str | None = None,
) -> dict[str, Any]:
    """
    Merge alias_id into canonical_id (tombstone pattern).

    The alias node is preserved — use resolve_canonical() to normalise IDs.
    Returns a merge receipt.
    """
    from opencrab.ontology.canonicalize import CanonicalizeEngine
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    identity = IdentityEngine(ctx["sql"])
    engine = CanonicalizeEngine(identity, ctx["builder"])
    return engine.merge_nodes(
        canonical_id, alias_id, canonical_space, canonical_type, merge_properties, merged_by
    )


def canonicalize_find_and_propose(
    node_id: str,
    name: str,
    space: str | None = None,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """
    Find similar nodes by name and auto-propose them as duplicate candidates for review.

    Returns proposed candidates — none are applied automatically.
    """
    from opencrab.ontology.canonicalize import CanonicalizeEngine
    from opencrab.ontology.identity import IdentityEngine

    ctx = _get_context()
    identity = IdentityEngine(ctx["sql"])
    engine = CanonicalizeEngine(identity, ctx["builder"])
    return engine.find_and_propose(node_id, name, space, threshold)


def promotion_register_candidate(
    space: str,
    node_type: str,
    node_id: str,
    properties: dict[str, Any],
    confidence: float | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """
    Register an extracted entity as a promotion candidate (status='candidate').

    The node will not appear in promoted queries until promoted.
    """
    from opencrab.ontology.promotion import PromotionEngine

    ctx = _get_context()
    engine = PromotionEngine(ctx["builder"], ctx["sql"])
    return engine.register_candidate(space, node_type, node_id, properties, confidence, source_id)


def promotion_validate_candidate(
    space: str,
    node_type: str,
    node_id: str,
    existing_properties: dict[str, Any],
    validator_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Mark a candidate as 'validated' — ready for final promotion review.

    Does not promote yet. Call promotion_promote() after validation.
    """
    from opencrab.ontology.promotion import PromotionEngine

    ctx = _get_context()
    engine = PromotionEngine(ctx["builder"], ctx["sql"])
    return engine.validate_candidate(space, node_type, node_id, existing_properties, validator_id, note)


def promotion_promote(
    space: str,
    node_type: str,
    node_id: str,
    existing_properties: dict[str, Any],
    promoted_by: str | None = None,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Promote a validated candidate to 'promoted' status.

    Optionally links evidence nodes via 'supports' edges.
    Returns a promotion receipt with receipt_id and receipt_ts.
    """
    from opencrab.ontology.promotion import PromotionEngine

    ctx = _get_context()
    engine = PromotionEngine(ctx["builder"], ctx["sql"])
    return engine.promote(space, node_type, node_id, existing_properties, promoted_by, evidence_ids)


def promotion_reject(
    space: str,
    node_type: str,
    node_id: str,
    existing_properties: dict[str, Any],
    rejected_by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark a candidate as 'rejected' with an optional reason."""
    from opencrab.ontology.promotion import PromotionEngine

    ctx = _get_context()
    engine = PromotionEngine(ctx["builder"], ctx["sql"])
    return engine.reject(space, node_type, node_id, existing_properties, rejected_by, reason)


# ---------------------------------------------------------------------------
# Phase 5: Billing / Tenant / Schema Packs
# ---------------------------------------------------------------------------


def billing_get_usage(
    tenant_id: str = "default",
    event_type: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """
    Return aggregated usage counts for a tenant.

    Parameters
    ----------
    tenant_id:
        Tenant to report on (default: 'default').
    event_type:
        Optional filter: node_write, edge_write, query, ingest, promotion, harness_apply.
    since:
        Optional ISO timestamp — only count events after this time.
    """
    ctx = _get_context()
    return ctx["billing"].get_usage(tenant_id, event_type, since)


def billing_list_events(
    tenant_id: str = "default",
    limit: int = 50,
) -> dict[str, Any]:
    """Return recent billing events for a tenant."""
    ctx = _get_context()
    events = ctx["billing"].list_events(tenant_id, limit)
    return {"tenant_id": tenant_id, "total": len(events), "events": events}


def content_pack_list(min_nodes: int = 1) -> dict[str, Any]:
    """
    List all content packs loaded into the localcrab ontology stores.

    Returns each pack_id with node count and a representative title
    derived from node properties (source_package_title / title / name).

    Parameters
    ----------
    min_nodes:
        Only return packs with at least this many nodes (default 1).
    """
    ctx = _get_context()
    graph = ctx["neo4j"]
    if not graph.available:
        return {"error": "graph store unavailable"}

    # LocalGraphStore는 run_cypher()가 no-op(항상 [])이므로 Cypher를 사용할 수 없다.
    # 대신 LocalGraphStore.list_packs()가 동등한 SQL GROUP BY 집계를 제공한다.
    # Neo4j 모드에서는 기존 Cypher 경로를 그대로 유지해 동작 변화를 최소화한다.
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore
    if isinstance(graph, (LocalGraphStore, KuzuGraphStore)):
        rows = graph.list_packs(min_nodes)
        # list_packs() 반환 형식: [{"pack_id": str, "node_count": int, "sample_title": str}]
        packs = []
        for r in rows:
            pid = r.get("pack_id") or ""
            title = r.get("sample_title") or ""
            display = title.replace(" ontology pack", "").replace(" ontology Pack", "").strip()
            packs.append({
                "pack_id":    pid,
                "node_count": r["node_count"],
                "title":      display or pid or "(no pack_id)",
            })
        return {"total": len(packs), "packs": packs}

    # Neo4j 모드: anchor 우선 + source_package_title 폴백
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
    rows = graph.run_cypher(cypher, {"min_nodes": min_nodes})

    packs = []
    for r in rows:
        pid = r["pack_id"] or ""
        title = r["sample_title"] or ""
        # trim trailing " ontology pack" boilerplate for readability
        display = title.replace(" ontology pack", "").replace(" ontology Pack", "").strip()
        packs.append({
            "pack_id":    pid,
            "node_count": r["node_count"],
            "title":      display or pid or "(no pack_id)",
        })

    return {"total": len(packs), "packs": packs}


def schema_pack_list() -> dict[str, Any]:
    """List all available schema packs with install status."""
    from opencrab.schemas.pack_registry import list_packs

    packs = list_packs()
    return {"total": len(packs), "packs": packs}


def schema_pack_install(name: str) -> dict[str, Any]:
    """
    Install a schema pack by generating type YAML files.

    Existing user-customised schemas are NOT overwritten.

    Parameters
    ----------
    name:
        Pack name (e.g. 'saas', 'biomedical', 'legal').
    """
    from opencrab.schemas.pack_registry import install_pack

    return install_pack(name)


def schema_pack_uninstall(name: str, force: bool = False) -> dict[str, Any]:
    """
    Remove auto-generated type schemas for a pack.

    User-customised schemas (no pack: header) are kept unless force=True.
    """
    from opencrab.schemas.pack_registry import uninstall_pack

    return uninstall_pack(name, force)


def harness_promotion_apply(
    package: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Apply a CrabHarness PromotionPackage directly to the OpenCrab ontology stores.

    Accepts the promotion package as a JSON object (not a file path) so it can
    be called inline from Claude or any MCP client without file I/O.

    Each node and edge write returns a receipt_id + receipt_ts for provenance.

    Parameters
    ----------
    package:
        A serialised PromotionPackage object (from CrabHarness promotion-stub output).
    dry_run:
        If True, validate grammar + schema without writing to any store.
    """
    try:
        from crabharness.crabharness.models import PromotionPackage
    except ImportError:
        return {"error": "crabharness package not installed. Run: pip install -e crabharness/"}

    from opencrab.grammar.validator import validate_node, validate_node_properties

    try:
        promo = PromotionPackage.model_validate(package)
    except Exception as exc:
        return {"error": f"Invalid PromotionPackage: {exc}"}

    node_receipts: list[dict[str, Any]] = []
    edge_receipts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if dry_run:
        for node in promo.nodes:
            r = validate_node(node.space, node.node_type)
            if not r.valid:
                errors.append({"node_id": node.node_id, "error": r.error})
            else:
                pr = validate_node_properties(node.node_type, node.properties or {})
                if not pr.valid:
                    errors.append({"node_id": node.node_id, "error": pr.error})
                else:
                    node_receipts.append({
                        "node_id": node.node_id,
                        "space": node.space,
                        "node_type": node.node_type,
                        "status": "dry_run_valid",
                    })
        return {
            "package_id": promo.package_id,
            "dry_run": True,
            "node_receipts": node_receipts,
            "edge_receipts": edge_receipts,
            "errors": errors,
        }

    ctx = _get_context()
    builder = ctx["builder"]

    for node in promo.nodes:
        try:
            result = builder.add_node(
                space=node.space,
                node_type=node.node_type,
                node_id=node.node_id,
                properties=node.properties or {},
            )
            node_receipts.append({
                "node_id": node.node_id,
                "receipt_id": result.get("receipt_id"),
                "receipt_ts": result.get("receipt_ts"),
                "stores": result.get("stores"),
            })
        except Exception as exc:
            errors.append({"node_id": node.node_id, "error": str(exc)})

    for edge in promo.edges:
        try:
            result = builder.add_edge(
                from_space=edge.from_space,
                from_id=edge.from_id,
                relation=edge.relation,
                to_space=edge.to_space,
                to_id=edge.to_id,
            )
            edge_receipts.append({
                "from_id": edge.from_id,
                "relation": edge.relation,
                "to_id": edge.to_id,
                "receipt_id": result.get("receipt_id"),
                "receipt_ts": result.get("receipt_ts"),
                "stores": result.get("stores"),
            })
        except Exception as exc:
            errors.append({
                "edge": f"{edge.from_id}-[{edge.relation}]->{edge.to_id}",
                "error": str(exc),
            })

    return {
        "package_id": promo.package_id,
        "mission_id": promo.mission_id,
        "run_id": promo.run_id,
        "dry_run": False,
        "node_receipts": node_receipts,
        "edge_receipts": edge_receipts,
        "errors": errors,
        "summary": {
            "nodes_written": len(node_receipts),
            "edges_written": len(edge_receipts),
            "errors": len(errors),
        },
    }


# ---------------------------------------------------------------------------
# Pack helpers (no server-side LLM — caller supplies structured nodes/edges)
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Generate a URL-safe pack_id slug from a title string."""
    text = _clean_str(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "pack"


def _nine_space_hint() -> str:
    """Build a concise 9-space grammar summary from manifest.SPACES."""
    try:
        from opencrab.grammar.manifest import SPACES
        lines = [
            "9-Space MetaOntology grammar (`space` + `node_type` values):",
        ]
        for space_id, spec in SPACES.items():
            types = ", ".join(spec.get("node_types", []))
            desc = spec.get("description", "")
            lines.append(f"  {space_id:<10} — {desc}: {types}")
        lines.append(
            "For valid edge relations between spaces, call ontology_manifest."
        )
        return "\n".join(lines)
    except Exception:
        return ""


def _ingest_into_pack(
    pack_id: str,
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    text: str | None = None,
    source_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    text_as_node: bool = True,
) -> dict[str, Any]:
    """Store caller-supplied nodes/edges and/or embed text, all tagged with pack_id. No server LLM.

    Parameters
    ----------
    text_as_node:
        When True (default), raw ``text`` is materialised as a 9-space
        ``evidence/TextUnit`` graph node via ``builder.add_node`` so it
        becomes a first-class grammar-compliant node (graph + doc + vector,
        all pack_id-tagged).  ``hybrid.ingest`` and ``mongo.upsert_source``
        are skipped to avoid duplicate vector writes under the same id.
        When False, the legacy path is used: vector-only embedding via
        ``hybrid.ingest`` + doc_sources record via ``mongo.upsert_source``.
    """
    ctx = _get_context()
    added_nodes = 0
    added_edges = 0
    node_errors: list[str] = []
    edge_errors: list[str] = []
    stores: dict[str, Any] = {}
    evidence_node: str | None = None

    for item in nodes or []:
        try:
            props = dict(_clean_meta(item.get("properties") or {}))
            props["pack_id"] = pack_id
            ctx["builder"].add_node(
                space=_clean_str(item.get("space", "")),
                node_type=_clean_str(item.get("node_type", "")),
                node_id=_clean_str(item.get("node_id", "")),
                properties=props,
            )
            added_nodes += 1
        except Exception as exc:
            node_errors.append(f"{item.get('node_id', '?')}: {exc}")

    for item in edges or []:
        try:
            props = dict(_clean_meta(item.get("properties") or {}))
            props["pack_id"] = pack_id
            ctx["builder"].add_edge(
                from_space=_clean_str(item.get("from_space", "")),
                from_id=_clean_str(item.get("from_id", "")),
                relation=_clean_str(item.get("relation", "")),
                to_space=_clean_str(item.get("to_space", "")),
                to_id=_clean_str(item.get("to_id", "")),
                properties=props,
            )
            added_edges += 1
        except Exception as exc:
            edge_errors.append(
                f"{item.get('from_id', '?')}→{item.get('to_id', '?')}: {exc}"
            )

    text_ingested = False
    if text and source_id:
        text = _clean_str(text)
        meta = _clean_meta(metadata or {})
        meta["pack_id"] = pack_id

        if text_as_node:
            # Materialise text as a 9-space evidence/TextUnit graph node so it
            # becomes a grammar-compliant first-class node (graph + doc_nodes +
            # vector), all tagged with pack_id.  builder.add_node handles vector
            # embedding internally, so we skip hybrid.ingest / mongo.upsert_source
            # to avoid duplicate writes under the same source_id.
            try:
                node_props: dict[str, Any] = {
                    "pack_id": pack_id,
                    "text": text,
                }
                if meta.get("title"):
                    node_props["title"] = meta["title"]
                if meta.get("source"):
                    node_props["source"] = meta["source"]
                ctx["builder"].add_node(
                    space="evidence",
                    node_type="TextUnit",
                    node_id=source_id,
                    properties=node_props,
                )
                evidence_node = source_id
                added_nodes += 1
                stores["evidence_node"] = "ok"
            except Exception as exc:
                node_errors.append(f"{source_id} (evidence/TextUnit): {exc}")
                stores["evidence_node"] = f"error: {exc}"
        else:
            # Legacy path: vector-only embedding + doc_sources record.
            try:
                vector_result = ctx["hybrid"].ingest(
                    text=text, source_id=source_id, metadata=meta
                )
                stores.update(vector_result.get("stores", {}))
            except Exception as exc:
                stores["chromadb"] = f"error: {exc}"
            if ctx["mongo"].available:
                try:
                    ctx["mongo"].upsert_source(source_id, text, meta)
                    stores["mongodb"] = "ok"
                except Exception as exc:
                    stores["mongodb"] = f"error: {exc}"
            else:
                stores["mongodb"] = "unavailable"

        text_ingested = True

    ctx["hybrid"].invalidate_bm25_cache()

    return {
        "pack_id": pack_id,
        "added_nodes": added_nodes,
        "added_edges": added_edges,
        "node_errors": node_errors,
        "edge_errors": edge_errors,
        "stores": stores,
        "text_ingested": text_ingested,
        "evidence_node": evidence_node,
    }


def pack_create(
    title: str,
    pack_id: str | None = None,
    description: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    text: str | None = None,
    text_as_node: bool = True,
) -> dict[str, Any]:
    """
    Create a new localcrab ontology pack and ingest content into it.

    Caller supplies pre-extracted nodes/edges; the server does NOT call any LLM.
    pack_id is auto-slugged from title unless explicitly provided.
    Optional text is materialised as a 9-space evidence/TextUnit graph node
    (text_as_node=True, default) or embedded as a vector blob only (False).
    """
    slug = _clean_str(pack_id) if pack_id else _slugify(title)
    if not slug:
        return {"error": "Could not derive a valid pack_id from title."}

    existing = content_pack_list()
    existing_ids = {p["pack_id"] for p in existing.get("packs", [])}
    if slug in existing_ids:
        return {
            "error": "pack already exists",
            "pack_id": slug,
            "hint": "use pack_ingest to add more content",
        }

    ctx = _get_context()
    anchor_node_id = f"dataset:{slug}"
    try:
        ctx["builder"].add_node(
            space="resource",
            node_type="Dataset",
            node_id=anchor_node_id,
            properties={
                "pack_id": slug,
                "title": _clean_str(title),
                "description": _clean_str(description or ""),
                "created_by": "localcrab-mcp",
            },
        )
    except Exception as exc:
        return {"error": f"anchor node failed: {exc}"}

    source_id: str | None = None
    if text:
        digest = hashlib.sha1(
            (_clean_str(title) + _clean_str(text)).encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        source_id = f"{slug}:doc:{digest}"

    ingest_result = _ingest_into_pack(
        slug,
        nodes=nodes,
        edges=edges,
        text=text,
        source_id=source_id,
        metadata={"title": _clean_str(title), "source": "pack_create"},
        text_as_node=text_as_node,
    )

    return {
        "status": "ok",
        "pack_id": slug,
        "title": _clean_str(title),
        "anchor_node": anchor_node_id,
        **ingest_result,
    }


def pack_ingest(
    pack_id: str,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    text: str | None = None,
    title: str | None = None,
    source_id: str | None = None,
    text_as_node: bool = True,
) -> dict[str, Any]:
    """
    Add content into an EXISTING localcrab ontology pack.

    Caller supplies pre-extracted nodes/edges; the server does NOT call any LLM.
    Optional text is materialised as a 9-space evidence/TextUnit graph node
    (text_as_node=True, default) so it becomes a grammar-compliant first-class
    node. Set text_as_node=False for legacy vector-only embedding.
    Fails if the pack does not exist — use pack_create first.
    """
    pack_id = _clean_str(pack_id)

    existing = content_pack_list()
    existing_ids = {p["pack_id"] for p in existing.get("packs", [])}
    if pack_id not in existing_ids:
        return {
            "error": "pack not found; use pack_create first",
            "pack_id": pack_id,
        }

    if not (nodes or edges or text):
        return {
            "error": "no content provided: supply at least one of nodes, edges, or text"
        }

    sid = source_id
    if text and not sid:
        digest = hashlib.sha1(
            (_clean_str(title or "") + _clean_str(text)).encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:12]
        sid = f"{pack_id}:doc:{digest}"

    ingest_result = _ingest_into_pack(
        pack_id,
        nodes=nodes,
        edges=edges,
        text=text,
        source_id=sid,
        metadata={"title": _clean_str(title or ""), "source": "pack_ingest"},
        text_as_node=text_as_node,
    )

    return {"status": "ok", "pack_id": pack_id, **ingest_result}


# ---------------------------------------------------------------------------
# READ helpers (no grammar validation needed — pure reads)
# ---------------------------------------------------------------------------


def ontology_get_node(node_id: str) -> dict[str, Any]:
    """Fetch a single node by node_id regardless of type.

    Works across all storage backends:
    - Local / Kuzu: uses get_node_by_id() (type-agnostic, single SQL/Cypher LIMIT 1)
    - Neo4j: falls back to type-agnostic Cypher MATCH (n {id: $id})
    """
    ctx = _get_context()
    graph = ctx["neo4j"]
    node_id = _clean_str(node_id)
    result: dict[str, Any] | None = None

    # Local / Kuzu backend
    if hasattr(graph, "get_node_by_id"):
        result = graph.get_node_by_id(node_id)
    # Neo4j backend: type-agnostic Cypher
    elif hasattr(graph, "run_cypher"):
        rows = graph.run_cypher(
            "MATCH (n {id: $id}) RETURN properties(n) AS props, labels(n)[0] AS lbl LIMIT 1",
            {"id": node_id},
        )
        if rows:
            result = {**(rows[0].get("props") or {}), "node_type": rows[0].get("lbl")}

    if result is None:
        return {"found": False, "node_id": node_id}
    return {"found": True, "node_id": node_id, "node": result}


def ontology_list_nodes(
    space: str | None = None,
    pack_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List nodes filtered by space and/or pack_id.

    When pack_id is given, queries the graph store's export_nodes(pack_id=...)
    which uses an indexed SQL filter (idx_nodes_pack) — avoids the limit-before-
    filter bug that would occur if we fetched N rows then Python-filtered.
    When pack_id is absent, falls back to the doc store's list_nodes.
    """
    ctx = _get_context()
    pack_id = _clean_str(pack_id) if pack_id else None
    cleaned_space = _clean_str(space) if space else None

    nodes: list[dict[str, Any]] = []

    if pack_id and hasattr(ctx["neo4j"], "export_nodes"):
        # Graph store: indexed pack_id filter → correct count before limit
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
        # Doc store fallback (no pack_id or no export_nodes on backend)
        nodes = ctx["mongo"].list_nodes(space=cleaned_space, limit=limit)
        if pack_id:
            nodes = [
                n for n in nodes
                if (n.get("properties") or {}).get("pack_id") == pack_id
            ]

    return {
        "nodes": nodes,
        "total": len(nodes),
        "space_filter": space,
        "pack_id_filter": pack_id,
    }


def ontology_list_edges(
    pack_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """List edges, optionally filtered by pack_id.

    Local / Kuzu: uses export_edges(pack_id, limit).
    Neo4j: falls back to pack_id-aware Cypher.
    """
    ctx = _get_context()
    graph = ctx["neo4j"]
    pack_id = _clean_str(pack_id) if pack_id else None

    # Local / Kuzu backend
    if hasattr(graph, "export_edges"):
        try:
            edges = graph.export_edges(pack_id=pack_id, limit=limit)
            return {"edges": edges, "total": len(edges), "pack_id_filter": pack_id}
        except Exception as exc:
            logger.warning("export_edges failed: %s", exc)

    # Neo4j backend
    if hasattr(graph, "run_cypher"):
        try:
            if pack_id:
                cypher = (
                    "MATCH (a)-[r]->(b) WHERE r.pack_id = $pack_id "
                    "RETURN a.id AS from_id, type(r) AS relation, b.id AS to_id, "
                    "properties(r) AS props LIMIT $limit"
                )
                rows = graph.run_cypher(cypher, {"pack_id": pack_id, "limit": limit})
            else:
                cypher = (
                    "MATCH (a)-[r]->(b) "
                    "RETURN a.id AS from_id, type(r) AS relation, b.id AS to_id, "
                    "properties(r) AS props LIMIT $limit"
                )
                rows = graph.run_cypher(cypher, {"limit": limit})
            return {"edges": rows or [], "total": len(rows or []), "pack_id_filter": pack_id}
        except Exception as exc:
            return {"edges": [], "total": 0, "error": str(exc), "pack_id_filter": pack_id}

    return {"edges": [], "total": 0, "error": "graph store unavailable", "pack_id_filter": pack_id}


# ---------------------------------------------------------------------------
# Tool registry (used by the MCP server for tools/list)
# ---------------------------------------------------------------------------

_NINE_SPACE_HINT: str = _nine_space_hint()

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # ── Grammar ──────────────────────────────────────────────────────────────
    "ontology_manifest": {
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
    # ── Graph write ──────────────────────────────────────────────────────────
    "ontology_add_node": {
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
    "ontology_add_edge": {
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
    "ontology_query": {
        "description": (
            "Hybrid vector + BM25 + graph search with RRF reranking. "
            "Pass subject_id for policy-aware filtering via ReBAC."
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
                "subject_id": {
                    "type": "string",
                    "description": "Optional subject ID for policy-aware filtering (ReBAC view check).",
                },
                "use_bm25": {
                    "type": "boolean",
                    "description": "Include BM25 keyword results (default true).",
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
            },
            "required": ["question"],
        },
    },
    # MCP 비노출: ontology_query(use_bm25=True)의 strict subset — 중복.
    # 주석 해제하면 즉시 복원됨.
    # "query_bm25": {
    #     "description": "BM25-only keyword search against ontology node properties. Fast and deterministic.",
    #     "inputSchema": {
    #         "type": "object",
    #         "properties": {
    #             "question": {"type": "string", "description": "Search keywords."},
    #             "spaces": {"type": "array", "items": {"type": "string"}, "description": "Optional space filter."},
    #             "limit": {"type": "integer", "description": "Maximum results (default 10).", "default": 10},
    #         },
    #         "required": ["question"],
    #     },
    # },
    "ontology_get_node": {
        "description": "Fetch a single node by node_id regardless of type or space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "The node_id to look up."},
            },
            "required": ["node_id"],
        },
    },
    "ontology_list_nodes": {
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
    "ontology_list_edges": {
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
    # ── Analysis ─────────────────────────────────────────────────────────────
    "ontology_impact": {
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
    # MCP 비노출: 접근제어·감사·빌링 — 현재 워크플로 미사용. 주석 해제로 복원.
    # "ontology_rebac_check": {
    #     "description": "Check whether a subject has a permission over a resource.",
    #     "inputSchema": {
    #         "type": "object",
    #         "properties": {
    #             "subject_id": {"type": "string", "description": "Subject node ID."},
    #             "permission": {"type": "string", "description": "Permission: view, edit, execute, simulate, approve, admin."},
    #             "resource_id": {"type": "string", "description": "Resource node ID."},
    #         },
    #         "required": ["subject_id", "permission", "resource_id"],
    #     },
    # },
    "ontology_lever_simulate": {
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
    # MCP 비노출: 대화 적재는 pack_ingest(text_as_node)로 일원화. 주석 해제로 복원.
    # "ontology_extract": { ... }  ← server-LLM path
    # "ontology_ingest": { ... }   ← pack-unaware vector-only path
    # (전체 스키마는 위 주석 블록 참조 — 이전 세션에서 비노출 처리됨)

    # MCP 비노출: 감사/워크플로/승인 — 현재 워크플로 미사용. 주석 해제로 복원.
    # "workflow_create_run": {
    #     "description": "Create a new workflow run in 'pending' state.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "action_type": {"type": "string"}, "payload": {"type": "object"},
    #         "subject_id": {"type": "string"}}, "required": ["action_type", "payload"]},
    # },
    # "workflow_advance": {
    #     "description": "Advance a workflow run to a new status (pending/running/approved/rejected/completed/failed).",
    #     "inputSchema": {"type": "object", "properties": {
    #         "run_id": {"type": "string"}, "new_status": {"type": "string"},
    #         "output": {"type": "object"}, "actor": {"type": "string"}},
    #         "required": ["run_id", "new_status"]},
    # },
    # "approval_request": {
    #     "description": "Submit an approval request for a sensitive action.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "action_type": {"type": "string"}, "subject_id": {"type": "string"},
    #         "payload": {"type": "object"}, "run_id": {"type": "string"}},
    #         "required": ["action_type", "subject_id", "payload"]},
    # },
    # MCP 비노출: identity/canonicalize/promotion — 실사용 이력 0. 주석 해제로 복원.
    # "identity_add_alias": {
    #     "description": "Register an alias_id for a canonical_id in the alias table.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "canonical_id": {"type": "string"}, "alias_id": {"type": "string"},
    #         "alias_type": {"type": "string", "default": "name"},
    #         "space": {"type": "string"}, "created_by": {"type": "string"}},
    #         "required": ["canonical_id", "alias_id"]},
    # },
    # "identity_resolve_canonical": {
    #     "description": "Resolve a node_id to its canonical ID.",
    #     "inputSchema": {"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]},
    # },
    # "identity_propose_duplicate": {
    #     "description": "Propose that two nodes may be the same entity.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "node_a_id": {"type": "string"}, "node_b_id": {"type": "string"},
    #         "space": {"type": "string"}, "similarity": {"type": "number"},
    #         "method": {"type": "string", "default": "name_fuzzy"}},
    #         "required": ["node_a_id", "node_b_id"]},
    # },
    # "identity_resolve_duplicate": {
    #     "description": "Accept or reject a pending duplicate candidate.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "candidate_id": {"type": "string"}, "decision": {"type": "string"},
    #         "decided_by": {"type": "string"}, "note": {"type": "string"}},
    #         "required": ["candidate_id", "decision"]},
    # },
    # "identity_list_pending_duplicates": {
    #     "description": "List all pending duplicate candidates sorted by similarity descending.",
    #     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}, "required": []},
    # },
    # "canonicalize_merge_nodes": {
    #     "description": "Merge alias_id into canonical_id using the tombstone pattern.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "canonical_id": {"type": "string"}, "alias_id": {"type": "string"},
    #         "canonical_space": {"type": "string"}, "canonical_type": {"type": "string"},
    #         "merge_properties": {"type": "boolean", "default": True},
    #         "merged_by": {"type": "string"}},
    #         "required": ["canonical_id", "alias_id", "canonical_space", "canonical_type"]},
    # },
    # "canonicalize_find_and_propose": {
    #     "description": "Find nodes with similar names and auto-propose as duplicate candidates.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "node_id": {"type": "string"}, "name": {"type": "string"},
    #         "space": {"type": "string"}, "threshold": {"type": "number", "default": 0.5}},
    #         "required": ["node_id", "name"]},
    # },
    # "promotion_register_candidate": {
    #     "description": "Register an extracted entity as a promotion candidate.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "space": {"type": "string"}, "node_type": {"type": "string"},
    #         "node_id": {"type": "string"}, "properties": {"type": "object"},
    #         "confidence": {"type": "number"}, "source_id": {"type": "string"}},
    #         "required": ["space", "node_type", "node_id", "properties"]},
    # },
    # "promotion_validate_candidate": {
    #     "description": "Mark a candidate as validated (ready for promotion review).",
    #     "inputSchema": {"type": "object", "properties": {
    #         "space": {"type": "string"}, "node_type": {"type": "string"},
    #         "node_id": {"type": "string"}, "existing_properties": {"type": "object"},
    #         "validator_id": {"type": "string"}, "note": {"type": "string"}},
    #         "required": ["space", "node_type", "node_id", "existing_properties"]},
    # },
    # "promotion_promote": {
    #     "description": "Promote a validated candidate. Optionally links evidence via supports edges.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "space": {"type": "string"}, "node_type": {"type": "string"},
    #         "node_id": {"type": "string"}, "existing_properties": {"type": "object"},
    #         "promoted_by": {"type": "string"},
    #         "evidence_ids": {"type": "array", "items": {"type": "string"}}},
    #         "required": ["space", "node_type", "node_id", "existing_properties"]},
    # },
    # "promotion_reject": {
    #     "description": "Mark a candidate as rejected with an optional reason.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "space": {"type": "string"}, "node_type": {"type": "string"},
    #         "node_id": {"type": "string"}, "existing_properties": {"type": "object"},
    #         "rejected_by": {"type": "string"}, "reason": {"type": "string"}},
    #         "required": ["space", "node_type", "node_id", "existing_properties"]},
    # },
    # MCP 비노출: billing — 현재 워크플로 미사용. 주석 해제로 복원.
    # "billing_get_usage": {
    #     "description": "Return aggregated usage counts for a tenant.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "tenant_id": {"type": "string", "default": "default"},
    #         "event_type": {"type": "string"}, "since": {"type": "string"}}, "required": []},
    # },
    # "billing_list_events": {
    #     "description": "Return recent billing events for a tenant.",
    #     "inputSchema": {"type": "object", "properties": {
    #         "tenant_id": {"type": "string", "default": "default"},
    #         "limit": {"type": "integer", "default": 50}}, "required": []},
    # },
    # ── Pack management ──────────────────────────────────────────────────────
    "content_pack_list": {
        "description": "List all content packs currently loaded in the localcrab ontology (Neo4j). Returns pack_id, node count, and display title for each pack.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_nodes": {"type": "integer", "description": "Only return packs with at least this many nodes (default 1).", "default": 1},
            },
            "required": [],
        },
    },
    # ── Schema packs ─────────────────────────────────────────────────────────
    "schema_pack_list": {
        "description": "List all available schema packs (saas, biomedical, legal) with install status.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "schema_pack_install": {
        "description": "Install a domain schema pack by generating type YAML files in schemas/types/.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pack name: saas, biomedical, or legal."},
            },
            "required": ["name"],
        },
    },
    "schema_pack_uninstall": {
        "description": "Remove auto-generated type schemas for a pack. User-customised schemas are kept unless force=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pack name to uninstall."},
                "force": {"type": "boolean", "description": "Remove even user-customised schemas (default false).", "default": False},
            },
            "required": ["name"],
        },
    },
    # ------------------------------------------------------------------
    # Pack create / pack ingest (no server-side LLM)
    # ------------------------------------------------------------------
    "pack_create": {
        "description": (
            "Create a new localcrab ontology pack and ingest content into it. "
            "Caller supplies pre-extracted nodes/edges (same shape as ontology_add_node/ontology_add_edge); "
            "the server does NOT call any LLM. pack_id is auto-slugged from title unless provided. "
            "Optional `text` is embedded locally into the vector/doc store (no external API).\n\n"
            + _NINE_SPACE_HINT
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Human-readable pack title (also used to auto-generate pack_id if not provided).",
                },
                "pack_id": {
                    "type": "string",
                    "description": "Optional explicit pack_id slug. Auto-slugged from title if omitted.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional pack description stored on the anchor node.",
                },
                "nodes": {
                    "type": "array",
                    "description": "Pre-extracted ontology nodes to add to the pack.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "space": {"type": "string", "description": "MetaOntology space (e.g. 'concept', 'resource')."},
                            "node_type": {"type": "string", "description": "Node type within the space (e.g. 'Entity', 'Document')."},
                            "node_id": {"type": "string", "description": "Stable unique identifier."},
                            "properties": {"type": "object", "description": "Arbitrary key/value node properties."},
                        },
                        "required": ["space", "node_type", "node_id"],
                    },
                },
                "edges": {
                    "type": "array",
                    "description": "Pre-extracted ontology edges to add to the pack.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_space": {"type": "string"},
                            "from_id": {"type": "string"},
                            "relation": {"type": "string", "description": "Relation label (call ontology_manifest for valid relations per space pair)."},
                            "to_space": {"type": "string"},
                            "to_id": {"type": "string"},
                            "properties": {"type": "object"},
                        },
                        "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
                    },
                },
                "text": {
                    "type": "string",
                    "description": "Optional raw text. Materialised as a 9-space evidence/TextUnit graph node by default (text_as_node=true).",
                },
                "text_as_node": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true (default), text is stored as an evidence/TextUnit graph node (grammar-compliant, pack_id-tagged). Set false for legacy vector-only embedding.",
                },
            },
            "required": ["title"],
        },
    },
    "pack_ingest": {
        "description": (
            "Add content into an EXISTING localcrab ontology pack. "
            "Caller supplies pre-extracted nodes/edges and/or raw text; the server does NOT call any LLM. "
            "Fails if the pack does not exist — use pack_create first.\n\n"
            + _NINE_SPACE_HINT
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_id": {
                    "type": "string",
                    "description": "Existing pack_id to add content into.",
                },
                "nodes": {
                    "type": "array",
                    "description": "Pre-extracted ontology nodes to add.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "space": {"type": "string"},
                            "node_type": {"type": "string"},
                            "node_id": {"type": "string"},
                            "properties": {"type": "object"},
                        },
                        "required": ["space", "node_type", "node_id"],
                    },
                },
                "edges": {
                    "type": "array",
                    "description": "Pre-extracted ontology edges to add.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_space": {"type": "string"},
                            "from_id": {"type": "string"},
                            "relation": {"type": "string"},
                            "to_space": {"type": "string"},
                            "to_id": {"type": "string"},
                            "properties": {"type": "object"},
                        },
                        "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
                    },
                },
                "text": {
                    "type": "string",
                    "description": "Optional raw text. Materialised as a 9-space evidence/TextUnit graph node by default (text_as_node=true). Use to append conversation content to a loaded pack.",
                },
                "text_as_node": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true (default), text is stored as an evidence/TextUnit graph node (grammar-compliant, pack_id-tagged, graph+doc+vector). Set false for legacy vector-only embedding.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional document title (stored as metadata).",
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional stable source identifier for the text document. Auto-generated from title+text hash if omitted.",
                },
            },
            "required": ["pack_id"],
        },
    },
    # ── Execution / harness ──────────────────────────────────────────────────
    "harness_promotion_apply": {
        "description": (
            "Apply a CrabHarness PromotionPackage to the OpenCrab ontology stores. "
            "Writes each node and edge, returning receipt_id + receipt_ts per operation. "
            "Use dry_run=true to validate grammar and schema without writing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "object",
                    "description": "Serialised PromotionPackage (from crabharness promotion-stub or run output).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Validate without writing to stores.",
                    "default": False,
                },
            },
            "required": ["package"],
        },
    },
}

# Callable map
_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    # ── Grammar ──────────────────────────────────────────────────────────────
    "ontology_manifest": ontology_manifest,
    # ── Graph write ──────────────────────────────────────────────────────────
    "ontology_add_node": ontology_add_node,
    "ontology_add_edge": ontology_add_edge,
    # ── Retrieval / read ─────────────────────────────────────────────────────
    "ontology_query": ontology_query,
    "ontology_get_node": ontology_get_node,
    "ontology_list_nodes": ontology_list_nodes,
    "ontology_list_edges": ontology_list_edges,
    # MCP 비노출: ontology_query의 strict subset — 주석 해제로 복원.
    # "query_bm25": query_bm25,
    # ── Analysis ─────────────────────────────────────────────────────────────
    "ontology_impact": ontology_impact,
    "ontology_lever_simulate": ontology_lever_simulate,
    # MCP 비노출: 접근제어·감사·빌링·거버넌스 — 현재 워크플로 미사용. 주석 해제로 복원.
    # "ontology_rebac_check": ontology_rebac_check,
    # "workflow_create_run": workflow_create_run,
    # "workflow_advance": workflow_advance,
    # "approval_request": approval_request,
    # "identity_add_alias": identity_add_alias,
    # "identity_resolve_canonical": identity_resolve_canonical,
    # "identity_propose_duplicate": identity_propose_duplicate,
    # "identity_resolve_duplicate": identity_resolve_duplicate,
    # "identity_list_pending_duplicates": identity_list_pending_duplicates,
    # "canonicalize_merge_nodes": canonicalize_merge_nodes,
    # "canonicalize_find_and_propose": canonicalize_find_and_propose,
    # "promotion_register_candidate": promotion_register_candidate,
    # "promotion_validate_candidate": promotion_validate_candidate,
    # "promotion_promote": promotion_promote,
    # "promotion_reject": promotion_reject,
    # "billing_get_usage": billing_get_usage,
    # "billing_list_events": billing_list_events,
    # MCP 비노출: 이전 세션에서 비노출 처리됨 — 주석 해제로 복원.
    # "ontology_extract": ontology_extract,
    # "ontology_ingest": ontology_ingest,
    # ── Pack management ──────────────────────────────────────────────────────
    "content_pack_list": content_pack_list,
    "pack_create": pack_create,
    "pack_ingest": pack_ingest,
    # ── Schema packs ─────────────────────────────────────────────────────────
    "schema_pack_list": schema_pack_list,
    "schema_pack_install": schema_pack_install,
    "schema_pack_uninstall": schema_pack_uninstall,
    # ── Execution / harness ──────────────────────────────────────────────────
    "harness_promotion_apply": harness_promotion_apply,
}

# Combined tool descriptor list (name + schema)
TOOLS: list[dict[str, Any]] = [
    {"name": name, **schema}
    for name, schema in TOOL_SCHEMAS.items()
]


def dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """
    Look up and call a tool by name.

    Parameters
    ----------
    name:
        Tool name from TOOL_SCHEMAS.
    arguments:
        Arguments dict from the MCP tools/call request.

    Returns
    -------
    JSON-serialisable result.

    Raises
    ------
    KeyError
        If the tool name is not registered.
    """
    fn = _TOOL_FUNCTIONS.get(name)
    if fn is None:
        raise KeyError(f"Unknown tool: '{name}'. Available: {list(_TOOL_FUNCTIONS)}")
    return fn(**arguments)
