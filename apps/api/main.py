from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / "apps" / ".env", override=False)
load_dotenv(REPO_ROOT / ".env", override=False)

from opencrab.auth import Principal
from opencrab.config import get_settings
from opencrab.grammar.validator import describe_grammar
from opencrab.locking import write_lock
from opencrab.ontology.builder import OntologyBuilder
from opencrab.ontology.impact import ImpactEngine
from opencrab.ontology.query import HybridQuery
from opencrab.pack.read_scope import assert_registry_covers_graph, read_scope
from opencrab.services.pack_selection import mcp_warning_text, resolve_packs
from opencrab.stores.factory import (
    make_doc_store,
    make_graph_store,
    make_sql_store,
    make_vector_store,
)

logger = logging.getLogger("opencrab.api")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

security = HTTPBearer(auto_error=False)
FREE_MAX_VECTORS = 1000
FREE_MAX_SOURCES = 1
QUERY_EVENTS = {"query"}


class IngestRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Text to ingest into the ontology vector layer.")
    source_id: str | None = Field(default=None, description="Optional stable source identifier.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional metadata for the ingested source.")


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural language query.")
    spaces: list[str] | None = Field(default=None, description="Optional space filter.")
    limit: int = Field(default=10, ge=1, le=25, description="Maximum result count.")
    graph_depth: int = Field(default=1, ge=1, le=4, description="Neighborhood expansion depth.")
    pack_ids: list[str] | None = Field(default=None, description="Restrict search to these content packs.")
    auto_pack: bool = Field(default=False, description="Auto-select the best-matching pack for the question.")
    include_unpackaged: bool = Field(default=False, description=("IGNORED (#147). Reads are always scoped to the packs you can read; data belonging to no pack is outside every scope. Passing true returns a warning, not unpackaged rows."))


class ImpactRequest(BaseModel):
    node_id: str = Field(..., min_length=1)
    change_type: str = Field(default="update", min_length=1)
    depth: int = Field(default=2, ge=1, le=5)


class NodeRequest(BaseModel):
    space: str = Field(..., min_length=1)
    node_type: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    pack_id: str | None = Field(default=None, description="Optional destination pack_id. Defaults to the caller's default pack.")


class EdgeRequest(BaseModel):
    from_space: str = Field(..., min_length=1)
    from_id: str = Field(..., min_length=1)
    relation: str = Field(..., min_length=1)
    to_space: str = Field(..., min_length=1)
    to_id: str = Field(..., min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    pack_id: str | None = Field(default=None, description="Optional destination pack_id. Defaults to the caller's default pack.")


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tier: str
    # #147: the verified Principal, so every scoped read on this request can
    # derive its pack filter from opencrab.pack.read_scope.read_scope(sql,
    # principal) instead of a second, ad-hoc identity. `user_id` stays a
    # separate field (== principal.user_id) rather than being removed,
    # because ~30 existing reads of `auth.user_id` in this module (owner
    # stamping, ingest quota checks, audit attribution, /api/usage) must not
    # all change at once for an unrelated reason.
    principal: Principal


@dataclass(frozen=True)
class CountResult:
    """Result of a counter query.

    `status` distinguishes a real 0 from a degraded count:
      - "ok": value is accurate
      - "unavailable": underlying store is not connected
      - "timeout": query exceeded a deadline (mongo timeout, etc.)
      - "error": unexpected exception; see `detail`
    """

    value: int = 0
    status: str = "ok"
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class ApiContext:
    settings: Any
    graph: Any
    vector: Any
    docs: Any
    sql: Any
    hybrid: HybridQuery
    impact: ImpactEngine


def _tier() -> str:
    tier = os.getenv("OPENCRAB_TIER", "free").strip().lower()
    return tier if tier in {"free", "pro", "api"} else "free"


def _limits_for_tier(tier: str) -> dict[str, int | None]:
    if tier == "free":
        return {"max_vectors": FREE_MAX_VECTORS, "max_sources": FREE_MAX_SOURCES}
    return {"max_vectors": None, "max_sources": None}


def _is_timeout_exc(exc: BaseException) -> bool:
    """Detect Mongo / generic timeout-shaped exceptions without hard pymongo dep."""
    name = type(exc).__name__
    return name in {
        "ExecutionTimeout",
        "NetworkTimeout",
        "ServerSelectionTimeoutError",
        "WTimeoutError",
        "TimeoutError",
    }


def _classify_count_exc(exc: BaseException) -> CountResult:
    if _is_timeout_exc(exc):
        return CountResult(value=0, status="timeout", detail=str(exc) or None)
    return CountResult(value=0, status="error", detail=str(exc) or type(exc).__name__)


def _safe_count(fn: Any) -> CountResult:
    """Wrap a zero-arg counter callable into a CountResult."""
    try:
        return CountResult(value=int(fn()), status="ok")
    except Exception as exc:
        return _classify_count_exc(exc)


def _docs_available(docs: Any) -> bool:
    return bool(getattr(docs, "available", False))


def _source_owner(docs: Any, source_id: str) -> str | None:
    if not _docs_available(docs):
        return None

    try:
        source = docs.get_source(source_id)
    except Exception:
        return None

    if not source:
        return None
    metadata = source.get("metadata") or {}
    return metadata.get("user_id")


def _log_event(docs: Any, event_type: str, user_id: str, details: dict[str, Any]) -> None:
    if not _docs_available(docs):
        return

    try:
        docs.log_event(event_type, subject_id=user_id, details=details)
        return
    except TypeError:
        pass
    except Exception as exc:
        logger.debug("Audit log write failed for %s: %s", event_type, exc)
        return

    try:
        docs.log_event(event_type, payload=details, actor=user_id)
    except Exception as exc:
        logger.debug("Audit log write failed for %s: %s", event_type, exc)


def _count_user_nodes(docs: Any, user_id: str) -> CountResult:
    if not _docs_available(docs):
        return CountResult(value=0, status="unavailable")

    if hasattr(docs, "_db"):
        # OR across top-level (preferred) and legacy nested `properties.owner_id`.
        query = {"$or": [{"owner_id": user_id}, {"properties.owner_id": user_id}]}
        try:
            value = int(docs._db["nodes"].count_documents(query))
            return CountResult(value=value, status="ok")
        except Exception as exc:
            return _classify_count_exc(exc)

    try:
        rows = docs.list_nodes()
    except Exception as exc:
        return _classify_count_exc(exc)

    matched = sum(
        1
        for row in rows
        if row.get("owner_id") == user_id
        or (row.get("properties") or {}).get("owner_id") == user_id
    )
    return CountResult(value=matched, status="ok")


def _count_user_sources(docs: Any, user_id: str) -> CountResult:
    if not _docs_available(docs):
        return CountResult(value=0, status="unavailable")

    if hasattr(docs, "_db"):
        query = {"$or": [{"user_id": user_id}, {"metadata.user_id": user_id}]}
        try:
            value = int(docs._db["sources"].count_documents(query))
            return CountResult(value=value, status="ok")
        except Exception as exc:
            return _classify_count_exc(exc)

    try:
        rows = docs.list_sources()
    except Exception as exc:
        return _classify_count_exc(exc)

    matched = sum(
        1
        for row in rows
        if row.get("user_id") == user_id
        or (row.get("metadata") or {}).get("user_id") == user_id
    )
    return CountResult(value=matched, status="ok")


def _count_user_queries(docs: Any, user_id: str) -> CountResult:
    if not _docs_available(docs):
        return CountResult(value=0, status="unavailable")

    if hasattr(docs, "_db"):
        try:
            value = int(
                docs._db["audit_log"].count_documents(
                    {"subject_id": user_id, "event_type": {"$in": list(QUERY_EVENTS)}}
                )
            )
            return CountResult(value=value, status="ok")
        except Exception as exc:
            return _classify_count_exc(exc)

    try:
        rows = docs.get_audit_log(limit=500)
    except TypeError:
        rows = docs.get_audit_log()
    except Exception as exc:
        return _classify_count_exc(exc)

    matched = sum(
        1
        for row in rows
        if row.get("actor") == user_id and row.get("event_type") in QUERY_EVENTS
    )
    return CountResult(value=matched, status="ok")


def _recent_activity(docs: Any, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
    if not _docs_available(docs):
        return []

    if hasattr(docs, "_db"):
        cursor = (
            docs._db["audit_log"]
            .find({"subject_id": user_id}, {"_id": 0})
            .sort("timestamp", -1)
            .limit(limit)
        )
        return [
            {
                "event_type": row.get("event_type"),
                "timestamp": row.get("timestamp"),
                "details": row.get("details") or {},
            }
            for row in cursor
        ]

    try:
        rows = docs.get_audit_log(limit=100)
    except TypeError:
        rows = docs.get_audit_log()
    except Exception:
        return []

    filtered = [row for row in rows if row.get("actor") == user_id][:limit]
    return [
        {
            "event_type": row.get("event_type"),
            "timestamp": row.get("timestamp"),
            "details": row.get("payload") or {},
        }
        for row in filtered
    ]


def _meter_call(ctx: ApiContext, auth: AuthContext, endpoint: str) -> None:
    if auth.tier != "api":
        return

    _log_event(
        ctx.docs,
        "api_meter",
        auth.user_id,
        {"endpoint": endpoint, "tier": auth.tier},
    )


def _build_context() -> ApiContext:
    settings = get_settings()
    graph = make_graph_store(settings)
    vector = make_vector_store(settings)
    docs = make_doc_store(settings)
    sql = make_sql_store(settings)

    try:
        graph.ensure_constraints()
    except Exception as exc:
        logger.debug("Skipping graph constraint bootstrap: %s", exc)

    return ApiContext(
        settings=settings,
        graph=graph,
        vector=vector,
        docs=docs,
        sql=sql,
        hybrid=HybridQuery(vector, graph),
        impact=ImpactEngine(graph, sql),
    )


def _close_context(ctx: ApiContext | None) -> None:
    if ctx is None:
        return

    for store_name in ("graph", "docs", "vector", "sql"):
        store = getattr(ctx, store_name, None)
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                logger.debug("Failed to close %s: %s", store_name, exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    from opencrab.mcp.http_app import refuse_stale_shared_secret_env

    refuse_stale_shared_secret_env()
    app.state.context = _build_context()
    # #147: the registry/graph reconciliation guard needs a live sql + graph
    # store, so it cannot run before _build_context() the way
    # refuse_stale_shared_secret_env() does -- there is nothing to check
    # against yet at that point.
    assert_registry_covers_graph(app.state.context.sql, app.state.context.graph)
    try:
        yield
    finally:
        _close_context(getattr(app.state, "context", None))


app = FastAPI(
    title="OpenCrab SaaS API",
    version="0.1.0",
    lifespan=lifespan,
)

cors_origins = [
    item.strip()
    for item in os.getenv("OPENCRAB_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if item.strip()
]
# Fail CLOSED on an empty allowlist (#145). The previous `cors_origins or ["*"]`
# turned "the operator set OPENCRAB_CORS_ORIGINS to an empty value" into "every
# origin may send credentialed requests" -- the opposite of what clearing a
# setting should mean. An empty list means no cross-origin access; a browser
# deployment must name its origins explicitly. (`*` with
# allow_credentials=True is rejected by browsers anyway, so the old fallback
# was both unsafe in intent and broken in practice.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_context() -> ApiContext:
    return app.state.context


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ctx: ApiContext = Depends(get_context),
) -> AuthContext:
    """Derive the principal from a per-user bearer token (#145).

    Replaces the pre-#145 shared ``OPENCRAB_API_KEY`` + client-asserted
    ``X-User-Id`` header: that let any key holder impersonate any user
    (#143's #72). The principal is now verified server-side
    (``opencrab.auth.verify_token``) and every downstream use of
    ``auth.user_id`` in this module -- owner stamping, ingest quota checks,
    audit attribution, ``/api/usage`` -- reads this server-derived value
    instead of a client-supplied one.
    """
    from opencrab.auth import verify_token

    presented = (
        credentials.credentials
        if (credentials is not None and credentials.scheme.lower() == "bearer")
        else None
    )
    principal = verify_token(ctx.sql, presented) if presented else None
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthContext(user_id=principal.user_id, tier=_tier(), principal=principal)


def _enforce_ingest_limits(ctx: ApiContext, auth: AuthContext, source_id: str) -> None:
    if auth.tier != "free":
        return

    source_owner = _source_owner(ctx.docs, source_id)
    if source_owner and source_owner != auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Source '{source_id}' belongs to another user.",
        )

    if source_owner:
        return

    current_sources = _count_user_sources(ctx.docs, auth.user_id).value
    if current_sources >= FREE_MAX_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Free tier is limited to {FREE_MAX_SOURCES} source.",
        )

    current_vectors = current_sources
    if current_vectors >= FREE_MAX_VECTORS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Free tier is limited to {FREE_MAX_VECTORS} vectors.",
        )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Auth-exempt liveness probe (#147).

    Locking /api/status behind require_auth (below) removes this app's only
    unauthenticated probe -- monitoring and cloudflared health checks would
    have nothing to poll. This endpoint intentionally carries none of
    /api/status's payload: no storage_mode, no per-store availability. Those
    fields are exactly what #147 is closing off from unauthenticated callers,
    so a "trimmed" /api/status here would still leak them. Mirrors
    opencrab/mcp/http_app.py's own auth-exempt /healthz.
    """
    return {"ok": True}


@app.get("/api/status")
def get_status(
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    return {
        "ok": True,
        "service": "opencrab-api",
        "tier": _tier(),
        "storage_mode": get_settings().storage_mode,
        "stores": {
            "graph": {"available": bool(getattr(ctx.graph, "available", False)), "healthy": bool(_safe_count(ctx.graph.ping).value)},
            "vector": {"available": bool(getattr(ctx.vector, "available", False)), "healthy": bool(_safe_count(ctx.vector.ping).value)},
            "docs": {"available": bool(getattr(ctx.docs, "available", False)), "healthy": bool(_safe_count(ctx.docs.ping).value)},
            "sql": {"available": bool(getattr(ctx.sql, "available", False)), "healthy": bool(_safe_count(ctx.sql.ping).value)},
        },
    }


@app.get("/api/manifest")
def get_manifest(auth: AuthContext = Depends(require_auth)) -> dict[str, Any]:
    ctx = get_context()
    _meter_call(ctx, auth, "/api/manifest")
    return describe_grammar()


@app.post("/api/ingest")
def ingest_text(
    payload: IngestRequest,
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    from opencrab.auth import principal_scope
    from opencrab.pack.ownership import resolve_write_pack
    from opencrab.pack.source_writer import write_source

    source_id = payload.source_id or f"{auth.user_id}-{uuid4().hex[:12]}"
    metadata = dict(payload.metadata)
    metadata.setdefault("source_id", source_id)

    with write_lock():
        _enforce_ingest_limits(ctx, auth, source_id)
        # IngestRequest carries no pack_id field, so every REST ingest lands
        # in the caller's default pack (unlike /api/nodes /api/edges, which
        # accept an explicit one).
        target_pack_id = resolve_write_pack(ctx.sql, auth.principal, None)
        try:
            with principal_scope(auth.principal):
                receipt = write_source(
                    ctx.hybrid, ctx.docs,
                    text=payload.text, source_id=source_id,
                    metadata=metadata, pack_id=target_pack_id,
                )
        except ValueError as exc:
            # Ownership-tag invariant violation (#171) — a client error, not a 500.
            # Same disposition the node/edge endpoints already give ValueError.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Adapter: keep the pre-#148 envelope (source_id/stores/vector_id at
        # the top level, no receipt "metadata") rather than handing the
        # write_source receipt back verbatim -- this shape is a pinned
        # response contract.
        result: dict[str, Any] = {"source_id": source_id, "stores": dict(receipt["stores"])}
        if "vector_id" in receipt:
            result["vector_id"] = receipt["vector_id"]

        _log_event(
            ctx.docs,
            "ingest",
            auth.user_id,
            {
                "source_id": source_id,
                "text_length": len(payload.text),
                "tier": auth.tier,
            },
        )
        _meter_call(ctx, auth, "/api/ingest")

    sources_count = _count_user_sources(ctx.docs, auth.user_id)
    result["tier"] = auth.tier
    result["usage"] = {
        "user_sources": sources_count.to_dict(),
        "user_vectors": sources_count.to_dict(),
    }
    return result


@app.post("/api/query")
def query_ontology(
    payload: QueryRequest,
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    # Pack selection shares the MCP/CLI service so the three query surfaces agree
    # on the resolved filter and warning vocabulary (auto_pack failures degrade
    # gracefully rather than failing the search).
    scope = read_scope(ctx.sql, auth.principal)
    selection = resolve_packs(
        payload.question,
        payload.pack_ids,
        payload.auto_pack,
        payload.include_unpackaged,
        ctx.settings.local_data_dir,
        scope=scope,
        raise_on_error=False,
    )

    outcome = ctx.hybrid.query(
        question=payload.question,
        spaces=payload.spaces,
        limit=payload.limit,
        graph_depth=payload.graph_depth,
        pack_ids=selection.effective_pack_ids,
    )
    results = outcome.results

    keyword_fallback: list[dict[str, Any]] = []
    if not results:
        # #147: the CALLER'S FULL readable scope, not selection.effective_pack_ids.
        # This fallback only fires when the primary (pack-filtered) search came
        # up empty, and its job is to widen the search -- narrowing it to the
        # same effective_pack_ids the primary leg already tried would make it
        # search the identical space and always come up empty too. Widening
        # stops at the caller's own read scope, never past it.
        keyword_fallback = ctx.hybrid.keyword_search(
            keyword=payload.question,
            spaces=payload.spaces,
            limit=payload.limit,
            pack_ids=sorted(scope),
        )

    pack_filter: dict[str, Any] = {
        "pack_ids": selection.effective_pack_ids,
        "auto_pack": selection.auto_pack_active,
        "include_unpackaged": selection.include_unpackaged_effective,
    }
    if selection.warnings:
        pack_filter["warnings"] = [mcp_warning_text(w) for w in selection.warnings]

    response = {
        "question": payload.question,
        "spaces_filter": payload.spaces,
        "total": len(results),
        "results": [result.to_dict() for result in results],
        "keyword_fallback": keyword_fallback,
        "selected_packs": selection.selected_packs,
        "pack_filter": pack_filter,
    }
    # #51: spaces 필터의 과도기 경고를 MCP(response["spaces_filter_warnings"])와
    # 동일하게 노출한다. outcome.warnings 는 query() 반환값(지역 변수)이라
    # 스레드풀에서 동시 요청이 몰려도 서로의 경고를 훔쳐보지 않는다.
    if outcome.warnings:
        response["spaces_filter_warnings"] = list(outcome.warnings)
    _log_event(
        ctx.docs,
        "query",
        auth.user_id,
        {
            "question": payload.question[:240],
            "result_count": len(results),
            "fallback_count": len(keyword_fallback),
        },
    )
    _meter_call(ctx, auth, "/api/query")
    return response


@app.post("/api/impact")
def analyse_impact(
    payload: ImpactRequest,
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    with write_lock():
        result = ctx.impact.analyse(
            node_id=payload.node_id,
            change_type=payload.change_type,
            depth=payload.depth,
            pack_ids=sorted(read_scope(ctx.sql, auth.principal)),
        ).to_dict()
        _log_event(
            ctx.docs,
            "impact",
            auth.user_id,
            {
                "node_id": payload.node_id,
                "change_type": payload.change_type,
            },
        )
        _meter_call(ctx, auth, "/api/impact")
    return result


@app.post("/api/nodes")
def add_node(
    payload: NodeRequest,
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    # Route through the shared OntologyBuilder so HTTP and MCP writes converge:
    # deep grammar + required-field validation, receipt stamping, role-based
    # store keys and audit are all produced once. owner_id/pack_id are now
    # stamped by the builder itself (#148) -- no caller-side setdefault here.
    from opencrab.auth import principal_scope
    from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, resolve_write_pack

    target_pack_id = resolve_write_pack(ctx.sql, auth.principal, payload.pack_id)
    builder = OntologyBuilder(ctx.graph, ctx.docs, ctx.sql, vec=ctx.vector)
    try:
        with principal_scope(auth.principal), write_lock():
            response = builder.add_node(
                payload.space,
                payload.node_type,
                payload.node_id,
                dict(payload.properties),
                pack_id=target_pack_id,
            )
            _meter_call(ctx, auth, "/api/nodes")
    except ValueError as exc:
        # Grammar / required-field validation failure — a client error, not a 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PackNotFoundError:
        # #143 invariant 7: identical response for "doesn't exist at all" and
        # "exists but it's someone else's private pack" -- the response body
        # must not hint at which case this is.
        raise HTTPException(status_code=404, detail="pack not found; use pack_create first") from None
    except PackForbiddenError:
        raise HTTPException(status_code=403, detail="PACK_NOT_WRITABLE: not the pack owner") from None

    return response


@app.post("/api/edges")
def add_edge(
    payload: EdgeRequest,
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    # Shared OntologyBuilder path (see add_node). The builder resolves real node
    # types via the graph store's lookup_node_type, validates the relation, and
    # produces a receipt + role-based store keys + audit in one place.
    from opencrab.auth import principal_scope
    from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, resolve_write_pack

    target_pack_id = resolve_write_pack(ctx.sql, auth.principal, payload.pack_id)
    builder = OntologyBuilder(ctx.graph, ctx.docs, ctx.sql, vec=ctx.vector)
    try:
        with principal_scope(auth.principal), write_lock():
            response = builder.add_edge(
                payload.from_space,
                payload.from_id,
                payload.relation,
                payload.to_space,
                payload.to_id,
                payload.properties,
                pack_id=target_pack_id,
            )
            _meter_call(ctx, auth, "/api/edges")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PackNotFoundError:
        raise HTTPException(status_code=404, detail="pack not found; use pack_create first") from None
    except PackForbiddenError:
        raise HTTPException(status_code=403, detail="PACK_NOT_WRITABLE: not the pack owner") from None

    return response


@app.get("/api/usage")
def get_usage(
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    _meter_call(ctx, auth, "/api/usage")

    sources_count = _count_user_sources(ctx.docs, auth.user_id)
    usage = {
        "nodes": _count_user_nodes(ctx.docs, auth.user_id).to_dict(),
        "vectors": sources_count.to_dict(),
        "sources": sources_count.to_dict(),
        "queries": _count_user_queries(ctx.docs, auth.user_id).to_dict(),
    }
    # #147: the `system` block (all-user audit-event count, whole-graph node
    # count, whole-vector-store count) is gone. All three counters were
    # cross-user aggregates with no per-caller filter, on an endpoint whose
    # entire contract is "your own usage" -- removing one and keeping the
    # other two would leave the remaining ones no more scoped than the one
    # that got cut, so keeping any of them is not a smaller version of this
    # fix, it is a different fix that does not address the reason this one
    # exists.
    return {
        "user_id": auth.user_id,
        "tier": auth.tier,
        "limits": _limits_for_tier(auth.tier),
        "usage": usage,
        "recent_activity": _recent_activity(ctx.docs, auth.user_id),
    }


NODE_VIZ_LIMIT = 500
EDGE_VIZ_LIMIT = 2000


def _viz_node_id(props: dict[str, Any]) -> str:
    """Node id as stored by every backend (see ontology_list_nodes)."""
    return props.get("node_id") or props.get("id") or ""


def _viz_space(props: dict[str, Any]) -> str:
    return props.get("space_id") or props.get("space") or "concept"


def _viz_type(labels: list[str] | None, props: dict[str, Any]) -> str:
    return (labels or [None])[0] or props.get("node_type") or "Node"


@app.get("/api/nodes")
def list_nodes(
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Return nodes for graph visualization.

    ``degree_in_view`` counts a node's links **within the returned edge set**
    (``/api/edges``, capped at ``EDGE_VIZ_LIMIT``), not its degree in the whole
    graph -- a hub node can score 458 here while holding 3302 edges overall.
    That is deliberate: this payload drives a rendered subgraph, so sizing and
    "N links" labels should describe what is on screen. Callers needing true
    degree must count edges themselves.
    """
    # export_nodes_scoped/export_edges_scoped are the #147 authorization-scoped
    # counterparts of export_nodes/export_edges (opencrab/stores/_graph_protocol.py)
    # -- the plain export_* methods use a 3/5-way OR predicate meant for pack
    # export/fork, which would let a node outside the caller's scope through via
    # source/source_id inference (design #147 §3.4b). Every backend implements
    # the _scoped pair.
    scope_list = sorted(read_scope(ctx.sql, auth.principal))
    try:
        raw = ctx.graph.export_nodes_scoped(scope_list, limit=NODE_VIZ_LIMIT)

        # Computed from the same edge set the graph view renders, so the two
        # endpoints stay consistent with each other.
        degree_in_view: dict[str, int] = {}
        for item in ctx.graph.export_edges_scoped(scope_list, limit=EDGE_VIZ_LIMIT):
            for side in ("source_props", "target_props"):
                nid = _viz_node_id(item.get(side) or {})
                if nid:
                    degree_in_view[nid] = degree_in_view.get(nid, 0) + 1

        nodes = []
        for item in (raw or []):
            props = item.get("props") or {}
            nid = _viz_node_id(props)
            if not nid:
                continue
            visible = {k: v for k, v in props.items()
                       if k not in ("id", "node_id", "space", "space_id", "node_type")}
            nodes.append({
                "id": nid,
                "space": _viz_space(props),
                "node_type": _viz_type(item.get("labels"), props),
                "properties": visible,
                "degree_in_view": degree_in_view.get(nid, 0),
            })
        return {"nodes": nodes, "total": len(nodes)}
    except Exception as exc:
        logger.exception("list_nodes failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/edges")
def list_edges(
    auth: AuthContext = Depends(require_auth),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Return all edges for graph visualization."""
    try:
        scope_list = sorted(read_scope(ctx.sql, auth.principal))
        raw = ctx.graph.export_edges_scoped(scope_list, limit=EDGE_VIZ_LIMIT)
        edges = []
        for item in (raw or []):
            src = item.get("source_props") or {}
            tgt = item.get("target_props") or {}
            from_id, to_id = _viz_node_id(src), _viz_node_id(tgt)
            if not from_id or not to_id:
                continue
            edges.append({
                "from_id": from_id,
                "to_id": to_id,
                "relation": item.get("relation") or "relates_to",
                "from_space": _viz_space(src),
                "to_space": _viz_space(tgt),
            })
        return {"edges": edges, "total": len(edges)}
    except Exception as exc:
        logger.exception("list_edges failed")
        raise HTTPException(status_code=500, detail=str(exc))


## ─── Remote MCP Server (Streamable HTTP) ────────────────────────────────────
# The /mcp routes are provided by the shared opencrab.mcp.http_app.mcp_router,
# which delegates to MCPServer.handle_request — the same dispatch used by the
# stdio server and `opencrab serve --transport http`. #145: mcp_router() now
# requires its own per-user bearer token on every request (no more
# auth_token=None open mount) — the same verification opencrab.mcp.http_app
# uses standalone, reused here rather than reimplemented.

from opencrab.mcp.http_app import install_mcp_no_store, mcp_router  # noqa: E402

app.include_router(mcp_router())
# Same guarantee as create_app(): /mcp responses this app produces --
# including framework-generated 405s and validation errors -- must be
# uncacheable. Two mount points means two installs.
install_mcp_no_store(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "apps.api.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        reload=False,
    )
