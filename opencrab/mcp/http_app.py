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
access logs, proxy logs, browser history and Referer headers. This server masks
``token`` in its own uvicorn logs (#427). The other copies remain. It exists
because some clients cannot set custom headers at all -- claude.ai's web UI
is the concrete case -- and deleting it would cut them off. See
``docs/mcp-client-auth.md`` for which client needs which, and read that table
before changing any of this.

Header wins when both are present, and a header that is present but invalid
gets a 401 rather than falling back to the query parameter: without that
rule, attaching a junk ``Authorization`` header would bypass the
``allow_query_token`` restriction entirely.

THREADING (#577). The event loop must not run tool code. A write tool that
embeds on the CPU fallback takes tens of seconds. While it ran on the loop
thread, ``/healthz`` and every other request of the process stopped answering.
``mcp_post`` hands ``tools/call`` work to two fixed pools: ``mcp-write`` (one
thread) and ``mcp-read`` (four threads). A legacy JSON-RPC batch that contains
a ``tools/call`` runs as one unit on one of these pools. A ``ping`` in that
batch runs there too. The token check of every ``/mcp`` route runs on a third
pool, ``mcp-auth`` (one thread). Single ``initialize``, ``tools/list`` and
``ping`` requests, and batches without a ``tools/call``, stay on the loop. An
error for a ``tools/call`` (for example an unknown tool name) is produced by
``handle_request`` on the pool thread that call was sent to. See ``mcp_router``
for the routing rules and the lock reasoning.

Run with a single uvicorn worker — the underlying chroma PersistentClient is
single-process only.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from opencrab.auth import (
    Principal,
    principal_scope,
    refuse_stale_shared_secret_env,
    verify_token,
)
from opencrab.common.json_transport import serialize_jsonrpc_response
from opencrab.mcp import protocol
from opencrab.mcp import tools as tools_pkg
from opencrab.mcp.server import MCPServer
from opencrab.mcp.tools import tool_pool_kind

logger = logging.getLogger(__name__)

# Re-exported: refuse_stale_shared_secret_env lives in opencrab.auth (a neutral
# module) because mcp/server.py's standalone entry point needs it too and
# importing this module from there would be circular. Kept in __all__ so the
# existing `from opencrab.mcp.http_app import refuse_stale_shared_secret_env`
# call sites and tests keep working.
__all__ = [
    "create_app",
    "AccessLogTokenRedactor",
    "install_access_log_redaction",
    "install_mcp_no_store",
    "install_mcp_origin_guard",
    "mcp_router",
    "refuse_stale_shared_secret_env",
]

# #427: the ?token= credential travels in the URL when allow_query_token is on,
# and uvicorn logs the URL. Two uvicorn loggers carry the query string: the
# access log (request line) and the TRACE-level ASGI message logger (scope
# dump). The WebSocket handshake lines on uvicorn.error also carry it; that
# channel is closed by ws="none" in ``opencrab serve``, not filtered here.
# The mask is total (no prefix): a token prefix still shrinks the search space.
_TOKEN_PARAM = "token"
_TOKEN_MASK = "***"
_REDACTED_LOGGERS = ("uvicorn.access", "uvicorn.asgi")


def _redact_query(query: str) -> str:
    """Mask the value of every ``token`` parameter in a raw query string.

    This function decodes the name before it compares, because Starlette
    decodes the name before lookup. ``?%74oken=`` authenticates like
    ``?token=``. Starlette decodes ``+`` to a space, so ``to+ken`` is not an alias.
    """
    parts = query.split("&")
    for i, part in enumerate(parts):
        name, sep, _value = part.partition("=")
        if sep and unquote(name.replace("+", " ")) == _TOKEN_PARAM:
            parts[i] = f"{name}={_TOKEN_MASK}"
    return "&".join(parts)


def _redact_target(target: str) -> str:
    path, sep, query = target.partition("?")
    return f"{path}?{_redact_query(query)}" if sep else target


def _scrub_log_arg(arg: Any) -> Any:
    if isinstance(arg, str):
        return _redact_target(arg)
    if isinstance(arg, dict) and isinstance(arg.get("query_string"), bytes):
        # uvicorn.asgi logs a copy of the ASGI scope; copy again so the live
        # scope the app reads is never touched.
        scrubbed = dict(arg)
        scrubbed["query_string"] = _redact_query(arg["query_string"].decode("latin-1")).encode("latin-1")
        return scrubbed
    return arg


class AccessLogTokenRedactor(logging.Filter):
    """Mask ``token`` query values in uvicorn access and TRACE log records.

    The filter never drops a record and never raises. A record with an
    unexpected args shape passes through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple):
                record.args = tuple(_scrub_log_arg(a) for a in record.args)
        except Exception:  # noqa: BLE001 - a logging filter must not break requests
            pass
        return True


def install_access_log_redaction() -> None:
    """Attach the redactor to the uvicorn loggers once (idempotent).

    Logger filters survive the ``dictConfig`` call in ``uvicorn.run()``, but
    handlers do not. ``create_app()`` runs before that call, so one install
    from ``create_app()`` is enough.
    """
    for name in _REDACTED_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, AccessLogTokenRedactor) for f in target.filters):
            target.addFilter(AccessLogTokenRedactor())


# Every /mcp response carries this. Not just the successful ones: when the
# credential travels in the URL (allow_query_token), the 401/405/202/parse-error
# responses are just as cacheable by an intermediary, and their URLs carry the
# same secret.
_NO_STORE = {"Cache-Control": "no-store"}


def _jsonrpc_response(content: Any, **kwargs: Any) -> Response:
    """Return a JSON-RPC response with valid UTF-8 JSON bytes."""
    return Response(
        content=serialize_jsonrpc_response(content, allow_nan=False),
        media_type="application/json",
        **kwargs,
    )


# #136: HTTP status for JSON-RPC error codes on MODERN single requests
# (legacy responses keep the pre-#136 blanket 200). #250: when the legacy era
# is disabled outright (MCP_PROTOCOL_VERSIONS selects no legacy version), the
# server is modern-only and this mapping applies to EVERY single request --
# a legacy-shaped request's rejection is a modern transport error there, and
# answering it 200 contradicted the documented modern mapping. Spec MUSTs:
# unknown RPC method -> 404; version/header/meta faults -> 400. -32603 stays
# 200 -- the spec leaves it unspecified, so the deviation is kept minimal.
_MODERN_ERROR_STATUS = {
    -32700: 400,
    -32600: 400,
    -32602: 400,
    protocol.HEADER_MISMATCH: 400,
    protocol.MISSING_REQUIRED_CLIENT_CAPABILITY: 400,
    protocol.UNSUPPORTED_PROTOCOL_VERSION: 400,
    -32601: 404,
}


def _modern_header_fault(request: Request, body: dict[str, Any]) -> str | None:
    """Standard-header validation for one modern single request (#136).

    Runs BEFORE the body's own _meta validation (design §4.3.1): a
    metadata-less ``server/discover`` over HTTP therefore reports the
    missing MCP-Protocol-Version header (-32020), while the same body over
    stdio (no header layer) reports -32602 from handle_request.
    """
    headers = request.headers
    params = body.get("params")
    if not isinstance(params, dict):
        params = {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    header_version = headers.get("mcp-protocol-version")
    if header_version is None:
        return "missing required MCP-Protocol-Version header"
    if header_version != meta.get(protocol.META_PROTOCOL_VERSION):
        return (
            "MCP-Protocol-Version header does not match the request body's "
            f"'{protocol.META_PROTOCOL_VERSION}'"
        )
    mcp_method = headers.get("mcp-method")
    if mcp_method is None:
        return "missing required Mcp-Method header"
    if mcp_method != body.get("method"):
        return "Mcp-Method header does not match the request body's method"
    if body.get("method") == "tools/call":
        mcp_name = headers.get("mcp-name")
        if mcp_name is None:
            return "missing required Mcp-Name header for tools/call"
        if protocol.decode_mcp_name(mcp_name) != params.get("name"):
            return "Mcp-Name header does not match the request body's params.name"
    return None


def _batch_is_modern(request: Request, body: list[Any]) -> bool:
    """#136: JSON-RPC batch arrays are a legacy-only LocalCrab extension
    (2026-07-28 Streamable HTTP allows exactly one request or notification
    per POST body). Per-item processing would bypass header validation and
    the modern status mapping, so a modern version header on the request, a
    modern-flagged element (modern _meta or server/discover), or an element
    whose ``_meta`` is present but not a dict all reject the whole array.
    Measured legacy consumers (the reingest hook's lc_call, the loader plan)
    send no ``_meta`` at all, so none of this touches them."""
    if request.headers.get("mcp-protocol-version") in protocol.MODERN_VERSIONS:
        return True
    for item in body:
        if not isinstance(item, dict):
            continue
        if item.get("method") == "server/discover":
            return True
        params = item.get("params")
        if isinstance(params, dict) and "_meta" in params:
            meta = params["_meta"]
            if not isinstance(meta, dict) or protocol.META_PROTOCOL_VERSION in meta:
                return True
    return False


# #577: worker pools for tool calls and the token check. Their sizes are
# fixed so the number of threads is fixed. The SQLite stores keep one
# connection per thread in `_all_conns` and do not close the connections of
# threads that have ended, so a fixed set of threads caps the connection
# count. One write thread runs the MCP write calls one after another; the
# auth pool has one thread.
#
# The pools belong to the PROCESS, not to a router. The gateway builds one app
# and apps/api mounts one router, so the thread count is the same either way,
# and a test session that builds many apps does not keep a set of idle
# threads per app (each thread reserves stack address space, and the gate runs
# under an address-space limit). They are created lazily, under a lock,
# because several event loops (test clients, each on its own thread) can ask
# at the same time.
_READ_WORKERS = 4
_POOLS: dict[str, ThreadPoolExecutor] = {}
_POOLS_LOCK = threading.Lock()


def _pool(name: str) -> ThreadPoolExecutor:
    with _POOLS_LOCK:
        pool = _POOLS.get(name)
        if pool is None:
            width = _READ_WORKERS if name == "read" else 1
            pool = _POOLS[name] = ThreadPoolExecutor(
                max_workers=width, thread_name_prefix=f"mcp-{name}"
            )
        return pool


def _call_kind(item: Any, principal: Principal) -> str | None:
    """Pool kind for one JSON-RPC item, or None when it is not a tools/call.

    "write" / "read" come from ``tool_pool_kind``; a tools/call that names an
    unknown or hidden tool is "unknown". Never raises on client-shaped JSON.
    """
    if not isinstance(item, dict) or item.get("method") != "tools/call":
        return None
    params = item.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return tool_pool_kind(name, principal) or "unknown"


def _choose_pool(items: list[Any], principal: Principal) -> str | None:
    """Which pool runs these request items; None keeps them on the loop.

    Cold-context rule: while the MCP tool context is still empty, every call
    that will reach a tool runs on the write pool, whatever its kind. Building
    the context takes ``_context_init_lock`` and, in the store constructors,
    ``write.lock``; a write tool takes ``write.lock`` first and then waits for
    the init lock. With a reader building on another thread the two wait for
    each other (the reader's ``write.lock`` wait ends at its timeout, the
    writer's init-lock wait only when the reader lets go). One thread that
    builds, the write thread, cannot do that. ``_context`` is rebound when the
    build publishes, so it is read here as a package attribute, at call time.
    """
    kinds = [k for k in (_call_kind(item, principal) for item in items) if k is not None]
    if not kinds:
        return None
    if "write" in kinds:
        return "write"
    if "read" in kinds:
        return "read" if tools_pkg._context else "write"
    return "read"  # only unknown or hidden tool names: rejected before any context use


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
    #
    # #302 reachability note: `_auth_store()` below has the same
    # check-then-act shape as the embedding/BM25 sites that issue fixed (read
    # `auth_sql`, maybe build+append), but it deliberately carries NO lock.
    # What makes that safe is that exactly ONE thread ever runs `_check()`:
    # since #577 that is the `mcp-auth` worker (see `_check_off_loop`), where
    # before it was the event-loop thread. One thread cannot interleave with
    # itself, so the closure's `auth_sql` list needs no lock.
    # This is a structural judgment, not an absence of observed incidents.
    # It stops holding the moment `_check()` (or `_auth_store()` itself) can
    # run on a second thread -- a second thread in the auth pool, a direct
    # call from a route, a call from a tool worker. Any of those needs the same
    # instance-level lock the embedding and BM25 sites got.
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

    async def _offload(pool_name: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn(*args)`` on a pool thread without blocking the loop.

        The context is copied first so ``current_principal()`` (bound by
        ``principal_scope`` in the caller) is visible in the worker. If the
        awaiting task is cancelled, the cancellation is passed on to the
        executor future. A job the worker has not started by then does not run.
        A job that has started finishes on its thread and its result is
        dropped.
        """
        ctx = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(
            _pool(pool_name), ctx.run, fn, *args
        )

    async def _check_off_loop(
        request: Request, creds: HTTPAuthorizationCredentials | None
    ) -> Principal:
        # The token lookup is a database read; a write committing on the same
        # file in this process could make it wait. Run it on the one auth
        # thread so the loop never waits on the database (and see the #302
        # note above for why one thread is also what keeps it lock free).
        return await _offload("auth", _check, request, creds)

    def _handle_batch(items: list[Any]) -> list[Any]:
        return [r for r in (server.handle_request(item) for item in items) if r is not None]

    @router.post("/mcp")
    @router.post("/mcp/", include_in_schema=False)
    async def mcp_post(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal = await _check_off_loop(request, creds)
        with principal_scope(principal):
            try:
                body = await request.json()
            except Exception:
                return _jsonrpc_response(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                    status_code=400,
                    headers=_NO_STORE,
                )
            # #250: whether this server serves the legacy era at all -- fixed
            # at construction time by MCP_PROTOCOL_VERSIONS. With it off the
            # server is modern-only, and every transport rejection below is a
            # modern one (status mapping; batch/scalar/notification rules).
            # With it on, all legacy behaviour is byte-for-byte unchanged.
            legacy_enabled = bool(server._enabled_legacy)
            # JSON-RPC batch: legacy-only LocalCrab extension (#136) --
            # anything modern-flagged in or on the array is rejected outright,
            # and with the legacy era disabled EVERY array is, empty included
            # (#250).
            if isinstance(body, list):
                if not legacy_enabled or _batch_is_modern(request, body):
                    return _jsonrpc_response(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32600,
                                "message": (
                                    "JSON-RPC batch is a legacy-only extension; modern "
                                    "(2026-07-28) requests must be sent individually"
                                ),
                            },
                        },
                        status_code=400,
                        headers=_NO_STORE,
                    )
                pool = _choose_pool(body, principal)
                if pool is None:
                    out = _handle_batch(body)
                else:
                    out = await _offload(pool, _handle_batch, body)
                if not out:
                    return Response(status_code=202, headers=_NO_STORE)
                return _jsonrpc_response(out, headers=_NO_STORE)

            # #250: a body that is neither an object nor an array is Invalid
            # Request on a modern-only server (400 + -32600), rejected before
            # the dict-presuming handle_request can trip over it. Legacy-
            # enabled configurations keep their historical handling of this
            # shape untouched.
            if not legacy_enabled and not isinstance(body, dict):
                return _jsonrpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32600,
                            "message": "Invalid Request: the request body must be a JSON object",
                        },
                    },
                    status_code=400,
                    headers=_NO_STORE,
                )

            # #136: a single request runs under modern transport rules when
            # its body is modern-era OR it carries a modern version header --
            # a modern header on a legacy body must not slip through as
            # unvalidated legacy traffic (design §4.3.1).
            modern_http = isinstance(body, dict) and (
                protocol.is_modern_request(body.get("method"), body.get("params") or {})
                or request.headers.get("mcp-protocol-version") in protocol.MODERN_VERSIONS
            )
            if modern_http:
                fault = _modern_header_fault(request, body)
                if fault is not None:
                    if "id" not in body:
                        # PR review R1: a NOTIFICATION failing header
                        # validation is rejected at the transport (400) with
                        # an empty body. The spec leaves the JSON-RPC error
                        # body optional (MAY) for rejected notifications;
                        # omitting it keeps this layer consistent with
                        # handle_request, which never answers a notification.
                        return Response(status_code=400, headers=_NO_STORE)
                    return _jsonrpc_response(
                        {
                            "jsonrpc": "2.0",
                            "id": body.get("id"),
                            "error": {"code": protocol.HEADER_MISMATCH, "message": fault},
                        },
                        status_code=400,
                        headers=_NO_STORE,
                    )
                if "id" not in body:
                    # PR review R3: a NOTIFICATION failing BODY validation
                    # (unsupported version with matching header, malformed
                    # _meta shape) must not be 202-acknowledged either.
                    # handle_request rightly stays silent for notifications,
                    # so the transport pre-checks the body here and rejects
                    # with the same empty-body 400 as a header fault.
                    # validate_modern is pure (no side effects on the second
                    # call inside handle_request), and a notification that
                    # PASSES it but names an unknown method stays a silent
                    # 202 -- JSON-RPC drops unknown-method notifications,
                    # exactly as the legacy path does.
                    body_fault = protocol.validate_modern(
                        body.get("params") or {}, server._enabled_modern
                    )
                    if body_fault is not None:
                        return Response(status_code=400, headers=_NO_STORE)
                    # PR review R5: the same courtesy for call-shape faults --
                    # a tools/call notification with a malformed name or
                    # arguments would otherwise be swallowed into a 202 by
                    # handle_request's notification silence. Shared validator,
                    # tools/call only; a VALID call notification still runs
                    # fire-and-forget and 202s.
                    if body.get("method") == "tools/call":
                        call_fault = protocol.validate_tools_call_params(
                            body.get("params") or {}
                        )
                        if call_fault is not None:
                            return Response(status_code=400, headers=_NO_STORE)
            elif not legacy_enabled and "id" not in body:
                # #250: a legacy-shaped NOTIFICATION on a modern-only server
                # must not be 202-acknowledged. handle_request rightly stays
                # silent for notifications, so the transport rejects it here
                # with the same empty-body 400 the modern notification faults
                # use (the error body is a spec MAY for rejected
                # notifications). Legacy-shaped REQUESTS (id present) fall
                # through: handle_request's legacy-disabled gate produces
                # their error envelope, and the status mapping below applies.
                return Response(status_code=400, headers=_NO_STORE)
            pool = _choose_pool([body], principal)
            if pool is None:
                resp = server.handle_request(body)
            else:
                resp = await _offload(pool, server.handle_request, body)
            # Notifications (no id) get no body → 202 Accepted
            if resp is None:
                return Response(status_code=202, headers=_NO_STORE)
            # #136: modern requests carry the modern status mapping; #250: on
            # a modern-only server every single request does -- the JSON-RPC
            # error envelope is untouched either way, only the HTTP status
            # changes.
            if (modern_http or not legacy_enabled) and "error" in resp:
                status = _MODERN_ERROR_STATUS.get(resp["error"]["code"], 200)
                return _jsonrpc_response(resp, status_code=status, headers=_NO_STORE)
            return _jsonrpc_response(resp, headers=_NO_STORE)

    @router.get("/mcp")
    @router.get("/mcp/", include_in_schema=False)
    async def mcp_get(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        await _check_off_loop(request, creds)  # auth before the stateless-405 response
        # Stateless server offers no server→client SSE stream; per spec, 405.
        # #136: 2026-07-28 removed the GET stream endpoint outright, and
        # DELETE is no longer allowed either -- Allow shrinks to POST.
        return Response(status_code=405, headers={"Allow": "POST", **_NO_STORE})

    @router.delete("/mcp")
    @router.delete("/mcp/", include_in_schema=False)
    async def mcp_delete(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        await _check_off_loop(request, creds)
        # #136: 2026-07-28 prescribes 405 for DELETE on the MCP endpoint, and
        # the legacy Streamable HTTP revisions equally allow 405 from a server
        # that never minted a session (this one never did). Auth-first is kept
        # for parity with GET (401 before 405).
        return Response(status_code=405, headers={"Allow": "POST", **_NO_STORE})

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


def install_mcp_origin_guard(app: FastAPI) -> None:
    """#136: Origin validation on every ``/mcp`` request (2026-07-28
    Streamable HTTP "Security & Endpoint" MUST -- DNS-rebinding defence).

    App-level middleware, NOT a route check: framework-generated responses
    (e.g. the 405 for an unrouted PUT) must also sit behind it, so an
    invalid Origin gets 403 before routing happens at all. An absent Origin
    passes -- every measured non-browser consumer (lc_call/curl, Claude
    Code, server-to-server connector traffic) sends none. A present Origin
    must be loopback or exactly allowlisted via MCP_ALLOWED_ORIGINS.

    Install this BEFORE install_mcp_no_store on BOTH mounting apps:
    Starlette places the later-registered middleware outermost, and the
    no-store invariant must wrap this guard's 403 as well. The 403 also
    carries _NO_STORE directly, so a future ordering mistake degrades to
    redundancy rather than cache exposure.
    """
    from opencrab.config import get_settings

    try:
        allowed = protocol.parse_allowed_origins(
            getattr(get_settings(), "mcp_allowed_origins", None)
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    @app.middleware("http")
    async def _origin_guard(request: Request, call_next):
        if _is_mcp(request):
            origin = request.headers.get("origin")
            if origin is not None and not protocol.origin_allowed(origin, allowed):
                return JSONResponse(
                    {"detail": "Origin not allowed"}, status_code=403, headers=dict(_NO_STORE)
                )
        return await call_next(request)


@asynccontextmanager
async def _startup_registry_check(app: FastAPI) -> Any:
    """#147 §3.11: refuse ASGI startup when the graph holds pack_ids the
    ``packs`` registry has never heard of.

    Deliberately an ASGI ``lifespan``, not code that runs inline in
    ``create_app()``: this app's stores are otherwise built lazily
    (``mcp_router()``'s tool dispatch builds its own via
    ``opencrab.mcp.tools._get_context`` on the first ``tools/call``, and its
    own auth lookup store is separately lazy too -- see
    ``TestAuthDoesNoWorkForAnonymousCallers`` in tests/test_auth_boundary.py,
    which pins that constructing this app and rejecting an unauthenticated
    caller must not touch disk). Building sql/graph stores directly inside
    ``create_app()`` would run this check -- and create graph.db/opencrab.db
    -- every time a test merely calls ``create_app()`` to get an app object,
    which is exactly the "authenticating does work on the caller's behalf"
    failure mode that boundary exists to prevent. An ASGI lifespan instead
    only fires when the app is actually served: real ``uvicorn.run()``
    traffic, or a test that opts in via ``with TestClient(app) as client:``.
    Built directly via the store factory (not ``_get_context``, which also
    loads the vector/doc/billing stores this check does not need) and closed
    again immediately after.
    """
    from opencrab.config import get_settings
    from opencrab.pack.read_scope import assert_registry_covers_graph
    from opencrab.stores.factory import make_graph_store, make_sql_store

    cfg = get_settings()
    startup_sql = make_sql_store(cfg)
    startup_graph = make_graph_store(cfg)
    try:
        assert_registry_covers_graph(startup_sql, startup_graph)
    finally:
        for store in (startup_sql, startup_graph):
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    yield


def create_app(*, allow_query_token: bool = False) -> FastAPI:
    """Lightweight FastAPI app for ``serve --transport http`` — MCP router + healthz."""
    refuse_stale_shared_secret_env()
    install_access_log_redaction()
    if allow_query_token:
        logger.warning(
            "?token= query-parameter auth is ENABLED. The credential will appear "
            "in reverse-proxy logs, browser history and Referer headers "
            "(this server masks it in its own logs). Rotate these tokens more often than header-borne ones, and "
            "issue a separate token per client so one leak revokes one client. "
            "See docs/mcp-client-auth.md."
        )

    app = FastAPI(docs_url=None, redoc_url=None, lifespan=_startup_registry_check)
    app.include_router(mcp_router(allow_query_token=allow_query_token))
    # #136: guard first, no_store second -- the later registration is the
    # outer middleware, and no-store must wrap the guard's 403 responses.
    install_mcp_origin_guard(app)
    install_mcp_no_store(app)

    @app.get("/healthz")
    async def healthz():  # auth-exempt: lets cloudflared / monitoring probe freely
        return PlainTextResponse("ok")

    return app
