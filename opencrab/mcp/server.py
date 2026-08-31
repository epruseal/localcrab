"""
OpenCrab MCP Server — stdio JSON-RPC implementation.

Implements the Model Context Protocol (MCP) over stdin/stdout so that
any MCP-compatible host (Claude Code, n8n, etc.) can use OpenCrab tools.

Protocol:
  - Transport: newline-delimited JSON over stdio
  - Methods handled: initialize, tools/list, tools/call
  - Each request:  {"jsonrpc":"2.0","id":N,"method":"...","params":{...}}
  - Each response: {"jsonrpc":"2.0","id":N,"result":{...}}
                or {"jsonrpc":"2.0","id":N,"error":{"code":-32XXX,"message":"..."}}

Reference: https://modelcontextprotocol.io/specification
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from opencrab.auth import current_principal
from opencrab.config import get_settings
from opencrab.mcp import protocol
from opencrab.mcp.tools import UnknownToolError, dispatch_tool, tools_for_principal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Shared between the legacy initialize response and the modern server/discover
# result (#136) -- the text is a static constant, not per-deployment state, so
# carrying it in both eras contradicts nothing.
INSTRUCTIONS = (
    "OpenCrab exposes the MetaOntology OS grammar. "
    "Use ontology_manifest to explore the full grammar, "
    "then add nodes/edges and query the ontology."
)


class MCPServer:
    """
    Minimal stdio MCP server compatible with Claude Code's MCP protocol.

    Usage:
        server = MCPServer()
        server.run()   # blocks forever, reading from stdin
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._name = cfg.mcp_server_name
        self._version = cfg.mcp_server_version
        # #136: dual-era version gates. ValueError -> RuntimeError here so
        # every entry point (stdio main(), `opencrab serve`, mcp_router()/
        # create_app, the apps/api import) refuses startup the same way
        # refuse_stale_shared_secret_env does. getattr keeps MagicMock-based
        # test settings (which predate the field) on the all-enabled default.
        try:
            self._enabled_modern, self._enabled_legacy = protocol.parse_enabled_versions(
                getattr(cfg, "mcp_protocol_versions", None)
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Main event loop: read JSON-RPC requests from stdin, write responses
        to stdout. Runs until EOF or interrupt.
        """
        logger.info("OpenCrab MCP server starting (name=%s, version=%s)", self._name, self._version)

        for raw_line in sys.stdin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            response = self._handle_raw(raw_line)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

        logger.info("OpenCrab MCP server shutting down (EOF).")

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _handle_raw(self, raw: str) -> dict[str, Any] | None:
        """Parse a raw JSON line and return a JSON-RPC response dict."""
        if not raw or not raw.strip():
            return None

        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("Parse error: %s", exc)
            return self._error_response(None, PARSE_ERROR, f"Parse error: {exc}")

        return self.handle_request(request)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """
        Transport-agnostic JSON-RPC handler.

        Accepts an already-parsed request dict and returns a response dict,
        or None for notifications (which must NOT be answered). Shared by the
        stdio loop (``_handle_raw``) and the HTTP transport (``http_app``).
        """
        # JSON-RPC notifications have no "id" field — must NOT be responded to
        is_notification = "id" not in request
        req_id = request.get("id")
        method = request.get("method")

        if not isinstance(method, str):
            if is_notification:
                return None
            return self._error_response(req_id, INVALID_REQUEST, "Missing or invalid 'method'.")

        params = request.get("params") or {}

        # ── modern era (2026-07-28): per-request _meta (#136) ─────────────
        if protocol.is_modern_request(method, params):
            return self._handle_modern(req_id, method, params, is_notification)

        # ── legacy era (initialize handshake; no per-request _meta) ───────
        if not self._enabled_legacy:
            # Legacy support switched off via MCP_PROTOCOL_VERSIONS. The
            # errors name the modern versions: legacy clients have no
            # fall-forward mechanism, so this message is the only diagnostic
            # they can surface (spec basic/versioning SHOULD).
            if is_notification:
                return None
            supported = ", ".join(self._enabled_modern) or "none"
            if method == "initialize":
                return self._error_response(
                    req_id,
                    INVALID_PARAMS,
                    "The initialize handshake is disabled on this server; send "
                    f"per-request _meta with a supported protocol version: {supported}",
                )
            return self._error_response(
                req_id,
                INVALID_PARAMS,
                "Protocol version metadata is required "
                f"('{protocol.META_PROTOCOL_VERSION}' in params._meta); "
                f"supported: {supported}",
            )

        try:
            result = self._dispatch(method, params)
        except KeyError as exc:
            if is_notification:
                logger.debug("Ignoring notification for unknown method '%s'", method)
                return None
            return self._error_response(req_id, METHOD_NOT_FOUND, str(exc))
        except TypeError as exc:
            if is_notification:
                return None
            return self._error_response(req_id, INVALID_PARAMS, f"Invalid params: {exc}")
        except Exception as exc:
            logger.exception("Internal error handling method '%s': %s", method, exc)
            if is_notification:
                return None
            return self._error_response(req_id, INTERNAL_ERROR, str(exc))

        # Notifications never get a response, even on success
        if is_notification:
            return None

        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Route a method to its handler."""
        if method == "initialize":
            return self._handle_initialize(params)
        elif method == "tools/list":
            return self._handle_tools_list(params)
        elif method == "tools/call":
            return self._handle_tools_call(params)
        elif method == "ping":
            return {"status": "ok", "server": self._name}
        elif method.startswith("notifications/"):
            # MCP notifications: silently acknowledge, no response needed
            logger.debug("Received notification: %s", method)
            return None
        else:
            raise KeyError(f"Method not found: '{method}'")

    # ------------------------------------------------------------------
    # Modern era (2026-07-28) -- #136
    # ------------------------------------------------------------------

    def _handle_modern(
        self, req_id: Any, method: str, params: Any, is_notification: bool
    ) -> dict[str, Any] | None:
        """Serve one modern-era request (per-request ``_meta``; stateless).

        Body-side validation only: the HTTP transport runs its header
        validation BEFORE handle_request is reached (issue #136 design
        §4.3.1), and stdio has no header layer, so a metadata-less
        ``server/discover`` reports -32602 here (stdio) but -32020 at the
        HTTP layer. Notifications are never answered, whatever the outcome.
        """
        fault = protocol.validate_modern(params, self._enabled_modern)
        if fault is not None:
            if is_notification:
                return None
            return self._error_response(req_id, fault.code, fault.message, fault.data)

        if method.startswith("notifications/"):
            logger.debug("Received modern notification: %s", method)
            return None

        try:
            result = self._dispatch_modern(method, params)
        except UnknownToolError as exc:
            # Modern contract (spec server/tools "Error Handling"): an
            # unknown tool is Invalid Params (-32602). The legacy era keeps
            # its historical -32601 mapping; #150's hidden-vs-removed
            # indistinguishability holds in both (same code either way).
            if is_notification:
                return None
            return self._error_response(req_id, INVALID_PARAMS, str(exc))
        except KeyError as exc:
            if is_notification:
                return None
            return self._error_response(req_id, METHOD_NOT_FOUND, str(exc))
        except TypeError as exc:
            if is_notification:
                return None
            return self._error_response(req_id, INVALID_PARAMS, f"Invalid params: {exc}")
        except Exception as exc:
            logger.exception("Internal error handling modern method '%s': %s", method, exc)
            if is_notification:
                return None
            return self._error_response(req_id, INTERNAL_ERROR, str(exc))

        if is_notification:
            return None
        # Per-response identity (spec basic/index: servers SHOULD include
        # serverInfo in every result's _meta -- results only, never errors).
        result.setdefault("_meta", {})[protocol.META_SERVER_INFO] = {
            "name": self._name,
            "version": self._version,
        }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch_modern(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "server/discover":
            return self._handle_discover()
        if method == "tools/list":
            return self._modern_tools_list(params)
        if method == "tools/call":
            return self._modern_tools_call(params)
        # initialize/ping were removed in 2026-07-28 -- they fall through
        # here and surface as METHOD_NOT_FOUND on the modern era.
        raise KeyError(f"Method not found: '{method}'")

    def _handle_discover(self) -> dict[str, Any]:
        """``server/discover`` (2026-07-28: servers MUST implement).

        ``supportedVersions`` lists MODERN versions only: it is the set a
        client may put into per-request ``_meta``, and a legacy version
        there would be the exact contradiction validate_modern rejects.
        Legacy support is discoverable through the initialize path itself.
        The response is identical for every caller (no user data), so
        cacheScope is "public".
        """
        return {
            "resultType": "complete",
            "supportedVersions": list(self._enabled_modern),
            # No listChanged: the tool registry is import-time-static, so
            # advertising change notifications would be untrue (#136 scope).
            "capabilities": {"tools": {}},
            "instructions": INSTRUCTIONS,
            "ttlMs": protocol.DISCOVER_TTL_MS,
            "cacheScope": "public",
        }

    def _modern_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """tools/list with the 2026-07-28 CacheableResult contract.

        cacheScope MUST stay "private": #150 scopes the list per principal,
        and a "public" hint would let a shared cache serve the local
        principal's admin tools to remote callers. Single page (the registry
        is small) with no ``nextCursor``; this server never mints a cursor,
        so any presented cursor is invalid (-32602 via TypeError).
        """
        if params.get("cursor") is not None:
            raise TypeError(
                "unknown cursor: this server returns the complete tool list in a single page"
            )
        return {
            "resultType": "complete",
            "tools": tools_for_principal(current_principal()),
            "ttlMs": protocol.TOOLS_LIST_TTL_MS,
            "cacheScope": "private",
        }

    def _modern_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not name:
            raise TypeError("'name' is required in tools/call params.")
        # PR review R2: a PRESENT non-object `arguments` -- explicit JSON null
        # included, hence the key-presence check (same rationale as
        # _meta:null) -- is a malformed CallToolRequest: protocol error
        # -32602, raised BEFORE dispatch_tool so no tool ever runs on it.
        # An ABSENT key defaults to {}. The legacy path keeps its historical
        # `or {}` coercion untouched.
        if "arguments" in params and not isinstance(params["arguments"], dict):
            raise TypeError("'arguments' must be an object when present")
        arguments = params.get("arguments") or {}
        try:
            result = dispatch_tool(name, arguments)
        except UnknownToolError:
            raise
        except Exception as exc:
            logger.warning("Tool '%s' raised: %s", name, exc)
            result = {"error": str(exc)}
        # isError covers BOTH error channels the handlers use: raising, and
        # returning a top-level {"error": ...} dict without raising (e.g.
        # graph-handler validation failures) -- issue #136 design §4.2.7.
        is_error = isinstance(result, dict) and "error" in result
        content_text = json.dumps(result, ensure_ascii=True, default=str)
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": content_text}],
            "isError": is_error,
        }

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Respond to the MCP initialize handshake.

        Returns server capabilities and protocol version.
        """
        requested = params.get("protocolVersion")
        if requested in self._enabled_legacy:
            # A supported legacy version is honoured verbatim -- the
            # 2024-11-05 (stdio) and 2025-03-26 (Streamable HTTP) handshakes
            # keep working unchanged.
            negotiated = requested
        elif requested is None and "2024-11-05" in self._enabled_legacy:
            # Pre-#136 fallback kept for version-less clients.
            negotiated = "2024-11-05"
        else:
            # Unknown (or modern-only) version: never echo blindly (#136
            # principle 1). Offer the newest legacy version this server
            # actually serves; per the legacy negotiation rule the client
            # decides whether to proceed or disconnect.
            negotiated = self._enabled_legacy[0]
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": self._name,
                "version": self._version,
            },
            "instructions": INSTRUCTIONS,
        }

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return the MCP tools visible to the caller (#150).

        Scoped by the caller's ``current_principal()`` -- bound upstream by
        the transport (``opencrab/mcp/http_app.py``'s per-request token
        verification, or ``opencrab/mcp/server.py``'s/``opencrab/cli.py``'s
        stdio local-user binding) before ``handle_request`` is ever called.
        Fail-closed like ``dispatch_tool``: with no principal bound,
        ``current_principal()`` raises ``LookupError`` rather than falling
        back to "show everything" -- there is no anonymous fallback (#143).

        This is the list-side half of #150's tool exposure control; the
        call-side half is ``dispatch_tool``'s own independent access-tier
        check (see ``opencrab.mcp.tools._registry.dispatch_tool``) -- hiding
        a tool here does not, by itself, stop ``tools/call`` from reaching
        it, so that check exists and is NOT skipped just because this
        method already filtered the list.
        """
        return {"tools": tools_for_principal(current_principal())}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a tool and return the result.

        Expected params: {"name": "tool_name", "arguments": {...}}
        """
        name = params.get("name")
        if not name:
            raise TypeError("'name' is required in tools/call params.")

        arguments = params.get("arguments") or {}

        try:
            result = dispatch_tool(name, arguments)
        except UnknownToolError:
            # Genuinely unregistered tool name — surface as JSON-RPC
            # METHOD_NOT_FOUND. Any OTHER exception (including a KeyError
            # raised incidentally by a tool's own logic) falls through to
            # the generic envelope below instead of being misreported as
            # "method not found".
            raise
        except Exception as exc:
            logger.warning("Tool '%s' raised: %s", name, exc)
            result = {"error": str(exc)}

        # MCP content format: wrap result in a content list
        # Use ensure_ascii=True to avoid invalid Unicode surrogates (e.g. from
        # Korean/CJK data) crashing the Claude API JSON parser.
        content_text = json.dumps(result, ensure_ascii=True, default=str)
        return {
            "content": [
                {
                    "type": "text",
                    "text": content_text,
                }
            ]
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error_response(
        req_id: Any, code: int, message: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": error,
        }


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the stdio MCP server.

    Reachable two ways -- ``opencrab serve --transport stdio`` and
    ``python -m opencrab.mcp.server`` -- so the auth boundary has to live
    here, not only in cli.py. Both go through the same two guards in
    ``opencrab.auth`` (#145): refuse leftover shared-secret env vars, and
    bind the local user as the principal for the process's whole lifetime.
    stdio has no per-request identity; its trust boundary is the OS process.
    """
    import io
    import logging

    # Force UTF-8 on Windows stdio — prevents Korean/CJK from becoming surrogates
    if hasattr(sys.stdin, "buffer"):
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )

    logging.basicConfig(
        level=logging.WARNING,  # keep stderr quiet while serving MCP on stdio
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from opencrab.auth import (
        principal_scope,
        refuse_stale_shared_secret_env,
        require_local_principal,
    )

    try:
        refuse_stale_shared_secret_env()
        principal = require_local_principal()
    except RuntimeError as exc:
        # stdout is the JSON-RPC channel -- diagnostics go to stderr only.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    # #147 §3.11: refuse to start when the graph holds pack_ids the `packs`
    # registry has never heard of -- read scoping resolves against the
    # registry, so an unregistered pack_id would be silently invisible to
    # every caller, including its own owner. Built directly via the store
    # factory (not opencrab.mcp.tools._get_context) so this standalone
    # entry point does not pay for the vector/doc/billing stores tool
    # dispatch doesn't need yet, and closed again immediately after -- the
    # first tools/call still builds its own long-lived stores lazily.
    from opencrab.config import get_settings
    from opencrab.pack.read_scope import assert_registry_covers_graph
    from opencrab.stores.factory import make_graph_store, make_sql_store

    cfg = get_settings()
    startup_sql = make_sql_store(cfg)
    startup_graph = make_graph_store(cfg)
    try:
        assert_registry_covers_graph(startup_sql, startup_graph)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        for store in (startup_sql, startup_graph):
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    with principal_scope(principal):
        MCPServer().run()


if __name__ == "__main__":
    main()
