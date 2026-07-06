"""
MCP Tool Definitions for OpenCrab / LocalCrab.

Each tool is a plain function decorated with ``@tool(name, schema)``
(see ``_registry.py``), which registers it into a single source-of-truth
registry. ``TOOLS`` / ``TOOL_SCHEMAS`` / ``_TOOL_FUNCTIONS`` / ``dispatch_tool``
/ ``UnknownToolError`` are all derived from that registry at import time.

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

── R9 패키지 분해 진행 상태 (Stage 7 — @tool 컷오버 완료) ───────────────────
이 파일(__init__.py)이 이 패키지의 실제 구현 홈이다. 과거 tools.py가 있던
자리 그대로, TOOL_SCHEMAS/_TOOL_FUNCTIONS 이중 등록을 `@tool` 데코레이터
단일 등록(``_registry.py``의 ``_REGISTRY``)으로 컷오버했다. 핸들러 본체는
아직 이 파일에 남아 있다 — `patch("opencrab.mcp.tools.<name>")`이 "그 함수가
정의된 모듈"의 `__dict__`를 몽키패치하므로(파이썬 LEGB), 핸들러를 query.py/
pack.py 등 별도 모듈로 물리 이전하면 다수 테스트의 patch 대상이 조용히
무효화된다(직접 재현·확인함). 그래서 물리 이전은 보류되었고, 지금은 이
파일 안에서 데코레이터 등록 순서(=골든 스냅샷 순서, tests/
test_tool_registry_contract.py)만 맞춰 재배치했다.

query_bm25, ontology_rebac_check, ontology_extract, ontology_ingest,
workflow_create_run/advance, approval_request, identity_*(5),
canonicalize_*(2), promotion_*(4), billing_*(2) — 실사용 이력 0 / MCP
비노출이던 휴면 코드는 삭제됨(git history에 보존). 필요 시 git log로 복원.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from opencrab.common.text import slugify

from ._registry import _REGISTRY, build_tools, tool
from ._registry import UnknownToolError as UnknownToolError
from ._registry import dispatch_tool as _registry_dispatch_tool

logger = logging.getLogger(__name__)

# chroma PersistentClient는 chromadb 공식상 단일 프로세스 전용이다("not process-safe for
# concurrent writers sharing the same local persistence path"; thread-safe도 단일 프로세스 내에서만
# 보증). 즉 여러 serve가 같은 persist 경로를 공유하는 것은 공식 미지원이며, 아래 chroma.lock/
# write.lock 은 그 위에 opencrab이 직접 얹은 커스텀 안전층이다(공식 보증 아님). 정공법은 단일
# `chroma run` 서버 + HttpClient. 출처: cookbook.chromadb.dev/core/{system_constraints,clients}.
# 공유 락(LOCK_SH)을 서버 수명 동안 보유 → load_local_packs.py의 배타 락(LOCK_EX)과 상호 배제.
_chroma_lock_fh = None


def _lock_data_dir() -> str:
    """락 파일(chroma.lock/write.lock)을 둘 데이터 디렉터리 경로.

    os.environ.get() 직독을 get_settings() 보다 우선한다 — get_settings()는
    lru_cache라 테스트가 실행 도중 monkeypatch한 LOCAL_DATA_DIR을 못 보는 stale
    캐시 문제가 있다(env 직독은 매 호출 즉시 반영). 환경변수 미설정 시에만
    get_settings().local_data_dir(HOME 파생 기본값 포함)로 폴백한다.

    CI 러너처럼 .env가 없어 기본 디렉터리가 실제로 아직 존재하지 않는 경우를
    대비해, 반환 전 os.makedirs(exist_ok=True)로 생성을 보장한다(락 파일 open()이
    FileNotFoundError로 죽는 것을 방지).
    """
    data_dir = os.environ.get("LOCAL_DATA_DIR")
    if not data_dir:
        from opencrab.config import get_settings

        data_dir = get_settings().local_data_dir
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _acquire_chroma_shared_lock() -> None:
    global _chroma_lock_fh
    data_dir = _lock_data_dir()
    lock_path = os.path.join(data_dir, "chroma.lock")
    _chroma_lock_fh = open(lock_path, "w")
    fcntl.flock(_chroma_lock_fh, fcntl.LOCK_SH)


# Tools that mutate the stores. When several MCP server processes run against the
# same data dir (e.g. the unauthenticated + authenticated HTTP instances), their
# writes must be serialised. This is a *per-write* exclusive lock on a dedicated
# write.lock file — entirely separate from the lifetime-held chroma.lock (LOCK_SH)
# above, which only guards against the offline batch loader (LOCK_EX). Reads take
# no lock. NOTE: lockless concurrent reads across processes is THIS layer's design
# assumption, NOT a chromadb guarantee — chromadb officially treats multi-process
# PersistentClient sharing as unsupported. write.lock serialises the one hazard the
# docs name explicitly (concurrent writers); cross-process reads here are
# stale-risk (a reader's in-memory HNSW won't see another process's new vectors
# until reload), not corruption. Robust fix = single chroma server + HttpClient.
WRITE_TOOLS = {
    "ontology_add_node",
    "ontology_add_edge",
    "pack_create",
    "pack_ingest",
    "schema_pack_install",
    "schema_pack_uninstall",
    "harness_promotion_apply",
}


@contextmanager
def _write_lock():
    """Hold an exclusive cross-process lock for the duration of a write tool."""
    data_dir = _lock_data_dir()
    lock_path = os.path.join(data_dir, "write.lock")
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)  # blocks until no other instance is writing
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


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
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    cfg = get_settings()

    # chroma.lock (LOCK_SH) only coordinates with the offline chroma batch loader;
    # skip it when the vector backend isn't chroma (sqlite-vec uses SQLite WAL, not
    # chroma's flock layer, so the shared lock would be a pointless hold).
    # vector_backend_resolved (not the raw vector_backend field) — VECTOR_BACKEND
    # is now unset by default and resolves conditionally (config.py), so a raw
    # comparison would miss the chroma case whenever it's the resolved default.
    if cfg.vector_backend_resolved == "chroma":
        _acquire_chroma_shared_lock()

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


@tool(
    "ontology_query",
    {
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
            },
            "required": ["question"],
        },
    },
)
def ontology_query(
    question: str,
    spaces: list[str] | None = None,
    limit: int = 10,
    subject_id: str | None = None,
    tenant_id: str = "default",
    use_bm25: bool = True,
    use_rerank: bool = True,
    use_fts: bool = True,
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
    from opencrab.services.pack_selection import mcp_warning_text, resolve_packs

    ctx = _get_context()
    cfg = get_settings()
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
        results = ctx["hybrid"].query(
            question=question,
            spaces=spaces,
            limit=limit,
            subject_id=subject_id,
            use_bm25=use_bm25,
            use_rerank=use_rerank,
            use_fts=use_fts,
            pack_ids=effective_pack_ids,
            include_unpackaged=include_unpackaged,
        )
        ctx["billing"].on_query(tenant_id, subject_id, question)
        response: dict[str, Any] = {
            "question": question,
            "spaces_filter": spaces,
            "subject_id": subject_id,
            "tenant_id": tenant_id,
            "pipeline": {"bm25": use_bm25, "rerank": use_rerank, "fts": use_fts},
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
)
def ontology_get_node(node_id: str) -> dict[str, Any]:
    """Fetch a single node by node_id regardless of type.

    All four storage backends implement get_node_by_id() natively (type-
    agnostic, single SQL/Cypher LIMIT 1) — see opencrab/stores/_graph_protocol.py.
    """
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


@tool(
    "content_pack_list",
    {
        "description": "List all content packs currently loaded in the localcrab ontology (Neo4j). Returns pack_id, node count, and display title for each pack.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_nodes": {"type": "integer", "description": "Only return packs with at least this many nodes (default 1).", "default": 1},
            },
            "required": [],
        },
    },
)
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

    # All four backends implement list_packs() natively (Local/PG: SQL GROUP
    # BY; Kuzu/Neo4j: Cypher aggregation) — see opencrab/stores/_graph_protocol.py.
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


@tool(
    "schema_pack_list",
    {
        "description": "List all available schema packs (saas, biomedical, legal) with install status.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
)
def schema_pack_list() -> dict[str, Any]:
    """List all available schema packs with install status."""
    from opencrab.schemas.pack_registry import list_packs

    packs = list_packs()
    return {"total": len(packs), "packs": packs}


@tool(
    "schema_pack_install",
    {
        "description": "Install a domain schema pack by generating type YAML files in schemas/types/.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pack name: saas, biomedical, or legal."},
            },
            "required": ["name"],
        },
    },
)
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


@tool(
    "schema_pack_uninstall",
    {
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
)
def schema_pack_uninstall(name: str, force: bool = False) -> dict[str, Any]:
    """
    Remove auto-generated type schemas for a pack.

    User-customised schemas (no pack: header) are kept unless force=True.
    """
    from opencrab.schemas.pack_registry import uninstall_pack

    return uninstall_pack(name, force)


# ---------------------------------------------------------------------------
# Pack helpers (no server-side LLM — caller supplies structured nodes/edges)
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Generate a URL-safe pack_id slug from a title string.

    Strips MCP surrogate junk first (``_clean_str``) then delegates to the
    shared slugify with ``allow_hangul=True``. Dropping Hangul collapsed every
    all-Korean title onto the same fallback (``pack``), so distinct Korean packs
    would have collided on one id — keeping Hangul makes the slug faithful.
    """
    return slugify(_clean_str(text), allow_hangul=True, fallback="pack")


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


_NINE_SPACE_HINT: str = _nine_space_hint()


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


@tool(
    "pack_create",
    {
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
)
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


@tool(
    "pack_ingest",
    {
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
)
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


@tool(
    "harness_promotion_apply",
    {
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
)
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
# Tool registry (used by the MCP server for tools/list / tools/call)
# ---------------------------------------------------------------------------
# Derived from _registry._REGISTRY, which every @tool-decorated handler above
# populated in decoration (= file) order. TOOL_SCHEMAS / _TOOL_FUNCTIONS are
# kept as back-compat aliases — tests and _legacy.py import them by name.

TOOLS: list[dict[str, Any]] = build_tools()
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {name: spec.schema for name, spec in _REGISTRY.items()}
_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {name: spec.fn for name, spec in _REGISTRY.items()}
dispatch_tool = _registry_dispatch_tool
