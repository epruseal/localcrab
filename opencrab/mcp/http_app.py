"""
OpenCrab MCP Server — Streamable HTTP (2025-03-26) transport.

Exposes the same MCP grammar as the stdio server over HTTP, reusing
``MCPServer.handle_request`` as the single JSON-RPC dispatch source. The
implementation is intentionally stateless: each POST is an independent
request/response exchange (no Mcp-Session-Id, no server→client SSE stream),
which is sufficient because every tool is request/response and the server
never pushes events.

Two surfaces share ``mcp_router``:
  - ``opencrab serve --transport http`` builds a lightweight app via ``create_app``.
  - ``apps/api`` includes the same router so there is one HTTP MCP implementation.

Authentication (#145): every ``/mcp`` request must present a valid per-user
bearer token, verified server-side via ``opencrab.auth.verify_token`` against
the ``users``/``api_tokens`` tables. There is no unauthenticated fallback --
the pre-#145 shared-secret model (a single ``auth_token`` that, when unset,
left the instance open) is deleted outright (#143 invariant 1). The verified
``Principal`` is bound via ``principal_scope()`` for the duration of the
request so ``dispatch_tool`` and the handlers it calls can read it via
``current_principal()``.

CREDENTIAL SOURCES. ``Authorization: Bearer`` is the default and the only one
enabled out of the box. ``?token=`` is available but **off unless explicitly
enabled** (``allow_query_token``), because a URL-borne credential leaks into
access logs, proxy logs, browser history and Referer headers. It exists
because some clients cannot set custom headers at all -- claude.ai's web UI
is the concrete case -- and deleting it would cut them off. See
``docs/mcp-client-auth.md`` for which client needs which, and read that table
before changing any of this.

Header wins when both are present, and a header that is present but invalid
gets a 401 rather than falling back to the query parameter: without that
rule, attaching a junk ``Authorization`` header would bypass the
``allow_query_token`` restriction entirely.

Run with a single uvicorn worker — the underlying chroma PersistentClient is
single-process only.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from opencrab.auth import (
    Principal,
    principal_scope,
    refuse_stale_shared_secret_env,
    verify_token,
)
from opencrab.mcp.server import MCPServer

logger = logging.getLogger(__name__)

# Re-exported: refuse_stale_shared_secret_env lives in opencrab.auth (a neutral
# module) because mcp/server.py's standalone entry point needs it too and
# importing this module from there would be circular. Kept in __all__ so the
# existing `from opencrab.mcp.http_app import refuse_stale_shared_secret_env`
# call sites and tests keep working.
__all__ = [
    "create_app",
    "install_mcp_no_store",
    "mcp_router",
    "refuse_stale_shared_secret_env",
]

# Every /mcp response carries this. Not just the successful ones: when the
# credential travels in the URL (allow_query_token), the 401/405/202/parse-error
# responses are just as cacheable by an intermediary, and their URLs carry the
# same secret.
_NO_STORE = {"Cache-Control": "no-store"}


def mcp_router(*, allow_query_token: bool = False) -> APIRouter:
    """
    Build the shared ``/mcp`` routes (POST / GET / DELETE).

    Every route requires a valid per-user token; there is no unauthenticated
    mode. Dispatch is delegated to ``MCPServer.handle_request`` so stdio and
    HTTP share one source of truth.

    ``allow_query_token`` defaults to **False** at every call site on purpose:
    a deployment that does not need URL credentials must not carry their
    exposure. Only ``opencrab serve --transport http --allow-query-token``
    turns it on.
    """
    router = APIRouter()
    server = MCPServer()  # constructed once; tool stores lazy-init on first call
    bearer = HTTPBearer(auto_error=False)

    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer", **_NO_STORE},
        )

    # One SQLStore for token lookups, opened lazily and reused. Deliberately
    # NOT the MCP tool context: `_get_context()` builds every store -- graph,
    # doc, vector, billing -- so routing an unauthenticated request through it
    # let a single junk token materialise nine database files. Authentication
    # must not be able to make the server do work on an anonymous caller's
    # behalf. This reads users/api_tokens and nothing else, and never runs DDL:
    # a database that does not exist yet cannot hold a valid token anyway.
    auth_sql: list[Any] = []

    def _auth_store() -> Any | None:
        """The lookup store, or None when there is demonstrably nothing to
        look up. Returns None rather than raising: a caller who cannot be
        authenticated gets a 401 either way.
        """
        if not auth_sql:
            from pathlib import Path

            from opencrab.config import get_settings
            from opencrab.stores.sql_store import SQLStore

            settings = get_settings()
            if settings.is_local:
                # Same reason as opencrab.auth.require_local_principal: on
                # SQLite, *connecting* to a missing path creates it. An
                # unauthenticated request must not leave a database behind.
                if not Path(settings.local_data_dir, "opencrab.db").is_file():
                    return None
                url = settings.sqlite_url
            else:
                url = settings.postgres_url
            store = SQLStore(url=url, create_tables=False)
            if not getattr(store, "available", False):
                # Do NOT cache a store that failed to connect. SQLStore
                # swallows a connect failure into `available = False` and
                # keeps the object, so caching it here would make one
                # transient outage at the moment of the first authentication
                # permanent: every subsequent request would reuse the dead
                # handle and 401 until the process restarted. Fail this
                # request, retry the connection on the next one.
                return store
            auth_sql.append(store)
        return auth_sql[0]

    def _verify(presented: str | None) -> Principal | None:
        if not presented:
            return None
        store = _auth_store()
        if store is None or not getattr(store, "available", False):
            return None
        # verify_token does a hash-equality lookup (opencrab/auth.py), not a
        # string compare, so there is no separate timing side-channel to defend
        # with hmac.compare_digest here (unlike the deleted shared-secret path).
        try:
            return verify_token(store, presented)
        except Exception:  # noqa: BLE001
            # No users table yet, or a transient query failure. Either way the
            # caller is not authenticated; a 401 is the honest answer and the
            # driver error is already in the store's own log line.
            return None

    def _check(request: Request, creds: HTTPAuthorizationCredentials | None) -> Principal:
        # A present Authorization header decides the request outright -- valid
        # or not. Checking the raw header rather than `creds` is deliberate:
        # HTTPBearer(auto_error=False) also yields None for a malformed or
        # non-Bearer header, so `creds is None` cannot distinguish "no header"
        # from "bad header". Falling back to the query parameter on a bad
        # header would let anyone bypass allow_query_token by attaching junk.
        # `is not None`, not truthiness: an empty `Authorization:` is still a
        # header the client chose to send. Treating it as absent would route
        # it to the query-parameter branch -- harmless today (that branch is
        # off by default) but it makes the rule "a header decides the request"
        # conditional on the header's *value*, which is not the rule.
        if request.headers.get("authorization") is not None:
            presented = creds.credentials if (creds and creds.scheme.lower() == "bearer") else None
            principal = _verify(presented)
            if principal is not None:
                return principal
            raise _unauthorized()

        if allow_query_token:
            principal = _verify(request.query_params.get("token"))
            if principal is not None:
                return principal

        raise _unauthorized()

    @router.post("/mcp")
    @router.post("/mcp/", include_in_schema=False)
    async def mcp_post(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal = _check(request, creds)
        with principal_scope(principal):
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                    status_code=400,
                    headers=_NO_STORE,
                )
            # JSON-RPC batch: collect non-notification responses
            if isinstance(body, list):
                out = [r for r in (server.handle_request(item) for item in body) if r is not None]
                if not out:
                    return Response(status_code=202, headers=_NO_STORE)
                return JSONResponse(out, headers=_NO_STORE)
            resp = server.handle_request(body)
            # Notifications (no id) get no body → 202 Accepted
            if resp is None:
                return Response(status_code=202, headers=_NO_STORE)
            return JSONResponse(resp, headers=_NO_STORE)

    @router.get("/mcp")
    @router.get("/mcp/", include_in_schema=False)
    async def mcp_get(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        _check(request, creds)  # auth is checked before the stateless-405 response
        # Stateless server offers no server→client SSE stream; per spec, 405.
        return Response(status_code=405, headers={"Allow": "POST, DELETE", **_NO_STORE})

    @router.delete("/mcp")
    @router.delete("/mcp/", include_in_schema=False)
    async def mcp_delete(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        _check(request, creds)
        # Stateless: no session to terminate. Acknowledge.
        return Response(status_code=200, headers=_NO_STORE)

    return router


def _is_mcp(request: Request) -> bool:
    """True for /mcp and its trailing-slash alias, and nothing else."""
    return request.url.path.rstrip("/") == "/mcp"


def install_mcp_no_store(app: FastAPI) -> None:
    """Guarantee ``Cache-Control: no-store`` on every ``/mcp`` response.

    The per-handler headers cover the responses this module writes, but not the
    ones the framework produces on its own -- 405s from route matching,
    OPTIONS, request-validation errors. When the credential rides in the URL
    (``allow_query_token``) those responses are just as sensitive as the
    successful ones: their *URLs* carry the secret.

    Applied at the app level rather than the router so it also covers whatever
    Starlette generates before a route is reached, and it must be installed on
    BOTH apps that mount the router -- ``create_app`` here and ``apps/api`` --
    since a header attached in one says nothing about the other.
    """

    @app.middleware("http")
    async def _no_store(request: Request, call_next):
        response = await call_next(request)
        if _is_mcp(request):
            response.headers["Cache-Control"] = "no-store"
        return response

    # The middleware above cannot reach a 500 produced by an unhandled
    # exception: Starlette's ServerErrorMiddleware sits *outside* the user
    # middleware stack, so that response is built after `_no_store` has
    # already returned. Registering a handler for bare Exception is what
    # Starlette hands to ServerErrorMiddleware itself, which is the only
    # layer that sees it -- verified by injecting a handler-level raise and
    # watching the 500 come back with no Cache-Control before this existed.
    async def _no_store_on_error(request: Request, exc: Exception):
        if not _is_mcp(request):
            # Registered app-wide because Starlette takes only one bare
            # Exception handler, but this function has no business changing
            # anything outside /mcp -- apps/api serves /api/* from the same
            # app, and swapping its 500 body for this one would be a
            # behaviour change nobody asked for. Re-raising hands the request
            # straight back to ServerErrorMiddleware, which produces exactly
            # the response and logging it did before.
            raise exc
        return JSONResponse(
            {"detail": "Internal Server Error"}, status_code=500, headers=dict(_NO_STORE)
        )

    app.add_exception_handler(Exception, _no_store_on_error)


def create_app(*, allow_query_token: bool = False) -> FastAPI:
    """Lightweight FastAPI app for ``serve --transport http`` — MCP router + healthz."""
    refuse_stale_shared_secret_env()
    if allow_query_token:
        logger.warning(
            "?token= query-parameter auth is ENABLED. The credential will appear "
            "in access logs, reverse-proxy logs, browser history and Referer "
            "headers. Rotate these tokens more often than header-borne ones, and "
            "issue a separate token per client so one leak revokes one client. "
            "See docs/mcp-client-auth.md."
        )
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(mcp_router(allow_query_token=allow_query_token))
    install_mcp_no_store(app)

    @app.get("/healthz")
    async def healthz():  # auth-exempt: lets cloudflared / monitoring probe freely
        return PlainTextResponse("ok")

    return app
