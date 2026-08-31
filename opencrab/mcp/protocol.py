"""
MCP 2026-07-28 dual-era protocol constants and pure helpers (#136).

Deliberately dependency-free (no stores, no tools, no FastAPI): both
``opencrab.mcp.server`` (era dispatch) and ``opencrab.mcp.http_app``
(transport validation) import this module, and it must never import back.

Era terminology (spec basic/versioning):
  - modern: per-request ``_meta`` metadata, revision 2026-07-28 and later.
  - legacy: ``initialize``-handshake revisions, 2025-11-25 and earlier.
A dual-era server serves both concurrently; each request selects its era
(see ``is_modern_request``).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Protocol versions
# ---------------------------------------------------------------------------

MODERN_VERSIONS: tuple[str, ...] = ("2026-07-28",)
# Newest first -- initialize negotiation offers index 0 for unknown versions.
LEGACY_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS: tuple[str, ...] = MODERN_VERSIONS + LEGACY_VERSIONS

# ---------------------------------------------------------------------------
# _meta keys (spec basic/index "General fields")
# ---------------------------------------------------------------------------

META_PREFIX = "io.modelcontextprotocol/"
META_PROTOCOL_VERSION = META_PREFIX + "protocolVersion"
META_CLIENT_CAPABILITIES = META_PREFIX + "clientCapabilities"
META_CLIENT_INFO = META_PREFIX + "clientInfo"
META_SERVER_INFO = META_PREFIX + "serverInfo"

# ---------------------------------------------------------------------------
# MCP-reserved JSON-RPC error codes (spec basic/index "Error Codes")
# ---------------------------------------------------------------------------

HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# ---------------------------------------------------------------------------
# CacheableResult hints (spec server/utilities/caching)
# ---------------------------------------------------------------------------

# The tool registry is import-time-static, but the list varies per principal
# (#150) and a modest TTL keeps a future dynamic registry honest.
TOOLS_LIST_TTL_MS = 300_000
DISCOVER_TTL_MS = 3_600_000


@dataclass(frozen=True)
class ProtocolFault:
    """A protocol-level rejection determined before dispatch."""

    code: int
    message: str
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Configured version gates
# ---------------------------------------------------------------------------


def parse_enabled_versions(raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(enabled_modern, enabled_legacy) from the MCP_PROTOCOL_VERSIONS setting.

    The setting can only RESTRICT the built-in ``SUPPORTED_VERSIONS`` --
    naming a version this code cannot parse is refused outright, so the
    server can never advertise a version it does not implement. A non-string
    value (None, or a MagicMock settings object in tests) and a blank string
    both mean "everything built-in". Raises ``ValueError``; startup entry
    points wrap it into ``RuntimeError`` (the established refuse-to-start
    contract of ``refuse_stale_shared_secret_env``).

    A legacy-only selection is legitimate (an operator freezing the pre-#136
    surface); modern-only is the migration lever for the eventual legacy
    removal. Selecting nothing at all is refused.
    """
    if not isinstance(raw, str) or not raw.strip():
        return MODERN_VERSIONS, LEGACY_VERSIONS
    chosen = [token.strip() for token in raw.split(",") if token.strip()]
    unknown = [version for version in chosen if version not in SUPPORTED_VERSIONS]
    if unknown:
        raise ValueError(
            f"MCP_PROTOCOL_VERSIONS names unsupported version(s) {unknown}; "
            f"this build supports: {list(SUPPORTED_VERSIONS)}"
        )
    enabled_modern = tuple(v for v in MODERN_VERSIONS if v in chosen)
    enabled_legacy = tuple(v for v in LEGACY_VERSIONS if v in chosen)
    if not enabled_modern and not enabled_legacy:
        raise ValueError("MCP_PROTOCOL_VERSIONS selects no supported protocol version")
    return enabled_modern, enabled_legacy


# ---------------------------------------------------------------------------
# Era determination and modern-request validation
# ---------------------------------------------------------------------------


def is_modern_request(method: Any, params: Any) -> bool:
    """Era rule (#136 design v4 §4.1, spec basic/versioning).

    ``server/discover`` is always modern (it only exists in 2026-07-28, and
    stdio probes rely on it reaching the modern path). Otherwise: a dict
    ``_meta`` carrying the protocolVersion key means modern; a PRESENT but
    non-dict ``_meta`` -- JSON ``null`` included, which is why presence is
    checked with ``in`` rather than ``.get() is not None`` -- is treated as
    modern too, so its malformed shape is rejected (-32602) instead of
    silently sliding into the legacy era unvalidated.
    """
    if method == "server/discover":
        return True
    if not isinstance(params, dict):
        return False
    if "_meta" not in params:
        return False
    meta = params["_meta"]
    if isinstance(meta, dict):
        return META_PROTOCOL_VERSION in meta
    return True


def validate_modern(params: Any, enabled_modern: tuple[str, ...]) -> ProtocolFault | None:
    """Body-side validation for a modern request; None means valid.

    Order: params shape -> _meta shape -> protocolVersion (type, then
    support) -> clientCapabilities. Version support faults carry the
    spec-shaped ``{"supported": [...], "requested": ...}`` data.
    """
    supported_note = ", ".join(enabled_modern) or "none"
    if not isinstance(params, dict):
        return ProtocolFault(
            -32602, "Invalid params: params must be an object for modern requests"
        )
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return ProtocolFault(
            -32602,
            f"Invalid params: params._meta with '{META_PROTOCOL_VERSION}' is required "
            f"on every modern request (supported versions: {supported_note})",
        )
    version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(version, str):
        return ProtocolFault(
            -32602, f"Invalid params: '{META_PROTOCOL_VERSION}' must be a string"
        )
    if version not in enabled_modern:
        return ProtocolFault(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": list(enabled_modern), "requested": version},
        )
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if META_CLIENT_CAPABILITIES not in meta or not isinstance(capabilities, dict):
        return ProtocolFault(
            -32602,
            f"Invalid params: '{META_CLIENT_CAPABILITIES}' (object) is required "
            "on every modern request",
        )
    return None


def validate_tools_call_params(params: dict[str, Any]) -> ProtocolFault | None:
    """Call-shape validation for a modern tools/call (#136 reviews R2/R4/R5).

    Shared single source for BOTH the server dispatch path (which raises
    TypeError(fault.message) to keep its historical -32602 mapping) and the
    HTTP notification pre-check (which turns a fault into the empty-body 400
    used for every other body-validation fault on a notification).

    Messages are EXACTLY the historical TypeError strings and carry no
    "Invalid params: " prefix -- the server's TypeError mapping adds that
    prefix exactly once.
    """
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return ProtocolFault(-32602, "'name' must be a non-empty string in tools/call params.")
    if "arguments" in params and not isinstance(params["arguments"], dict):
        return ProtocolFault(-32602, "'arguments' must be an object when present")
    return None


# ---------------------------------------------------------------------------
# HTTP header helpers (Streamable HTTP "Standard Request Headers")
# ---------------------------------------------------------------------------

_B64_SENTINEL_PREFIX = "=?base64?"
_B64_SENTINEL_SUFFIX = "?="


def decode_mcp_name(value: str) -> str:
    """Decode the ``=?base64?{...}?=`` sentinel form of an Mcp-Name header.

    A non-sentinel value passes through unchanged; an undecodable sentinel is
    returned as-is (it then simply fails the header/body comparison, which is
    the HeaderMismatch the sender earned).
    """
    if value.startswith(_B64_SENTINEL_PREFIX) and value.endswith(_B64_SENTINEL_SUFFIX):
        payload = value[len(_B64_SENTINEL_PREFIX) : -len(_B64_SENTINEL_SUFFIX)]
        try:
            return base64.b64decode(payload.encode("ascii"), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return value
    return value


# ---------------------------------------------------------------------------
# Origin validation (Streamable HTTP "Security & Endpoint")
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _normalize_bare_origin(value: str) -> tuple[str, str] | None:
    """(normalized ``scheme://netloc``, hostname) for a bare http(s) origin,
    or None for anything malformed.

    The single validator behind BOTH ``parse_allowed_origins`` (config side)
    and ``origin_allowed`` (request side), so the allowlist can never hold a
    shape the request check would not produce. The try wraps ``urlsplit``
    ITSELF plus the ``hostname``/``port`` accessors: ``urlsplit("http://[::1")``
    raises ValueError at the call, and a non-numeric or out-of-range port
    raises at ``.port`` -- every malformed shape must converge on None (the
    middleware turns that into a 403, never a 500). A trailing-colon netloc
    (``localhost:`` -- empty port) parses cleanly, so it is rejected by the
    shape check instead.
    """
    try:
        parts = urlsplit(value.strip())
        hostname = parts.hostname
        _ = parts.port  # accessor raises for non-numeric / out-of-range ports
    except ValueError:
        return None
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.netloc
        or not hostname
        or parts.path
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
        or parts.netloc.endswith(":")
    ):
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}", hostname


def parse_allowed_origins(raw: Any) -> frozenset[str]:
    """Validated allowlist from the MCP_ALLOWED_ORIGINS setting.

    Entries must be bare web origins -- ``http(s)://host[:port]`` with no
    path, query, fragment or userinfo; ``null``, non-http schemes, invalid
    ports and EMPTY entries (a stray comma) are refused -- consistent with
    MCP_PROTOCOL_VERSIONS, where "," also refuses startup while a fully
    blank value means "unset". Comparison is by normalized exact string
    (scheme and host lowercased, port kept verbatim): browsers omit default
    ports in the Origin header, so operators write entries exactly as
    browsers send them. Raises ``ValueError``; entry points wrap into
    ``RuntimeError``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return frozenset()
    allowed: set[str] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            raise ValueError(
                "MCP_ALLOWED_ORIGINS contains an empty entry (stray comma?) -- "
                "remove it or unset the variable entirely"
            )
        normalized = _normalize_bare_origin(token)
        if normalized is None:
            raise ValueError(
                f"MCP_ALLOWED_ORIGINS entry {token!r} is not a bare http(s) origin "
                "(expected scheme://host[:port] with no path/query/userinfo)"
            )
        allowed.add(normalized[0])
    return frozenset(allowed)


def origin_allowed(origin: str, allowed: frozenset[str]) -> bool:
    """Whether a PRESENT Origin header value may reach /mcp.

    A malformed value (path/userinfo attached, bad port, broken IPv6) is
    refused outright -- a loopback hostname does not sanctify a non-bare
    origin shape, and real browsers only ever send ``scheme://host[:port]``
    (RFC 6454), so nothing legitimate is lost. Well-formed loopback origins
    (any port) always pass: a DNS-rebinding attack cannot present one,
    because the attack works precisely by keeping the hostname non-local
    while rebinding its address. Everything else must be allowlisted
    exactly. Absent-Origin requests never reach this function (non-browser
    clients pass unconditionally).
    """
    parsed = _normalize_bare_origin(origin)
    if parsed is None:
        return False
    normalized, hostname = parsed
    if normalized in allowed:
        return True
    return hostname in _LOOPBACK_HOSTS
