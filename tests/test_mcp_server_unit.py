"""
Contract tests for MCPServer.handle_request (transport-agnostic JSON-RPC
handler), called directly with parsed dicts — no FastAPI, no stdio.

tests/test_mcp.py already covers _handle_raw (raw-string) round trips for
initialize / tools-list / tools-call-success plus parse/missing-method/
unknown-method errors — this file targets the request-dict contracts that
file does not exercise: the tool-exception envelope (server.py:202-204),
notifications, malformed dicts, and the batch-is-not-handled-here boundary.

Also pins the UnknownToolError-vs-KeyError contract: only a genuinely
unregistered tool name (opencrab.mcp.tools.UnknownToolError) is mapped to
JSON-RPC METHOD_NOT_FOUND; an incidental KeyError raised inside a tool's own
logic must fall through to the generic tool-exception error envelope
instead of being misreported as "method not found".
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def server():
    from opencrab.mcp.server import MCPServer

    with patch("opencrab.mcp.server.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            mcp_server_name="opencrab-test",
            mcp_server_version="0.0.1",
        )
        return MCPServer()


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestHandleRequestNormal:
    def test_initialize_echoes_requested_protocol_version(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        assert response["result"]["protocolVersion"] == "2025-03-26"

    def test_initialize_defaults_protocol_version_when_absent(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert response["result"]["protocolVersion"] == "2024-11-05"

    def test_tools_call_success_wraps_result_as_content(self, server):
        with patch("opencrab.mcp.server.dispatch_tool") as mock_dispatch:
            mock_dispatch.return_value = {"ok": True, "value": 42}
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "some_tool", "arguments": {"a": 1}},
                }
            )
        mock_dispatch.assert_called_once_with("some_tool", {"a": 1})
        assert response["id"] == 7
        assert "error" not in response
        content = response["result"]["content"]
        assert content[0]["type"] == "text"
        import json

        assert json.loads(content[0]["text"]) == {"ok": True, "value": 42}


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestHandleRequestError:
    def test_tools_call_tool_exception_is_caught_and_returned_as_error_content(self, server):
        """A tool raising a plain Exception must NOT surface as a JSON-RPC
        error response — server.py:202-204 catches it and returns a normal
        success envelope whose content encodes {"error": str(exc)}."""
        with patch("opencrab.mcp.server.dispatch_tool") as mock_dispatch:
            mock_dispatch.side_effect = ValueError("boom")
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "bad_tool", "arguments": {}}}
            )

        assert "error" not in response  # not a JSON-RPC-level error
        import json

        payload = json.loads(response["result"]["content"][0]["text"])
        assert payload == {"error": "boom"}

    def test_unknown_method_returns_method_not_found(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 9, "method": "totally/unknown"})
        assert response["error"]["code"] == -32601

    def test_tools_call_unknown_tool_error_maps_to_method_not_found(self, server):
        """UnknownToolError (raised by dispatch_tool for an unregistered
        name) is the ONLY tool-side exception mapped to -32601."""
        from opencrab.mcp.tools import UnknownToolError

        with patch("opencrab.mcp.server.dispatch_tool") as mock_dispatch:
            mock_dispatch.side_effect = UnknownToolError("Unknown tool: 'ghost'. Available: []")
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "ghost", "arguments": {}}}
            )

        assert response["error"]["code"] == -32601
        assert "ghost" in response["error"]["message"]

    def test_tools_call_incidental_keyerror_is_not_misreported_as_method_not_found(self, server):
        """A KeyError raised by a tool's OWN logic (e.g. a dict lookup bug,
        not an unknown-tool lookup) must not be conflated with
        UnknownToolError — it should land in the generic error-content
        envelope like any other tool exception, not as a JSON-RPC error."""
        with patch("opencrab.mcp.server.dispatch_tool") as mock_dispatch:
            mock_dispatch.side_effect = KeyError("some_unrelated_dict_key")
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "flaky_tool", "arguments": {}}}
            )

        assert "error" not in response
        import json

        payload = json.loads(response["result"]["content"][0]["text"])
        assert "some_unrelated_dict_key" in payload["error"]

    def test_tools_call_missing_name_returns_invalid_params(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"arguments": {}}}
        )
        assert response["error"]["code"] == -32602

    def test_dict_missing_method_key_returns_invalid_request(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 11, "params": {}})
        assert response["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestHandleRequestEdge:
    def test_notification_without_id_returns_none_even_on_success(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "method": "ping"})
        assert response is None

    def test_notification_without_id_returns_none_on_unknown_method(self, server):
        """Unknown-method notifications are dropped silently, not errored —
        a JSON-RPC notification must never get a response of any kind."""
        response = server.handle_request({"jsonrpc": "2.0", "method": "no/such/method"})
        assert response is None

    def test_missing_jsonrpc_key_is_not_validated_still_processed(self, server):
        """handle_request never actually inspects request["jsonrpc"] — only
        "id" (notification check) and "method" are consulted. Documents the
        real (permissive) contract rather than an assumed strict one."""
        response = server.handle_request({"id": 12, "method": "ping"})
        assert response["result"]["status"] == "ok"

    def test_batch_list_is_not_handled_at_this_layer(self, server):
        """Batch requests are unpacked by the HTTP transport before calling
        handle_request; a bare list here is not a supported input shape."""
        with pytest.raises(AttributeError):
            server.handle_request([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
