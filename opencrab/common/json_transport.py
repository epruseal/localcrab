"""JSON serializers for MCP response transports."""

from __future__ import annotations

import json
from typing import Any


def serialize_tool_result(value: Any) -> str:
    """Serialize a tool result with valid Unicode text.

    Lone UTF-16 surrogates become U+FFFD. Valid surrogate pairs become their
    Unicode scalar value. This keeps the result text valid for UTF-8 clients.
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def serialize_jsonrpc_response(response: Any, *, allow_nan: bool = True) -> str:
    """Serialize a JSON-RPC response for UTF-8 transport.

    Lone surrogates outside tool content remain JSON escapes. JSON-RPC uses
    these values for request identifiers, so this serializer does not replace
    them with U+FFFD.
    """
    text = json.dumps(response, ensure_ascii=False, allow_nan=allow_nan)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")
