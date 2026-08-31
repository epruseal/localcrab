"""
RED-phase contract tests for issue #136 (MCP 2026-07-28 dual-era server).

Design of record: /home/asdf/orch-scratch/o136/design-v4.md.

These tests exercise ``MCPServer.handle_request`` directly (no HTTP, no
stdio) against the modern (2026-07-28) era added by #136 alongside the
existing legacy (initialize-handshake) era it must keep serving byte-for-
byte unchanged. The implementation (``opencrab.mcp.protocol`` plus the
``server.py`` era-dispatch it drives) does not exist yet, so at the point
this file is committed every test below is expected to FAIL -- either with
an assertion mismatch against today's legacy-only behaviour, or with a
collection-time ``ModuleNotFoundError`` for ``opencrab.mcp.protocol`` (both
outcomes count as a valid RED). Any import of the new module is kept
local to this file so a missing module cannot break collection of the
rest of the test suite.

Era determination (see design v4 §4.1): ``method == "server/discover"`` is always
modern; otherwise a dict ``params["_meta"]`` carrying the
``io.modelcontextprotocol/protocolVersion`` key means modern, and its
absence means legacy.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}

UNSUPPORTED_PROTOCOL_VERSION = -32022
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


def _make_server(mcp_protocol_versions):
    """Build an ``MCPServer`` with a patched settings object carrying a
    specific ``mcp_protocol_versions`` value. Construction happens inside
    the patch context (mirroring the ``server`` fixture below) so a
    startup-time ``RuntimeError`` for an unknown configured version is
    raised while the patch is still active."""
    from opencrab.mcp.server import MCPServer

    with patch("opencrab.mcp.server.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            mcp_server_name="opencrab-test",
            mcp_server_version="0.0.1",
            mcp_protocol_versions=mcp_protocol_versions,
        )
        return MCPServer()


@pytest.fixture
def server():
    from opencrab.mcp.server import MCPServer

    with patch("opencrab.mcp.server.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            mcp_server_name="opencrab-test",
            mcp_server_version="0.0.1",
            mcp_protocol_versions=None,
        )
        return MCPServer()


_SERVER_INFO = {"name": "opencrab-test", "version": "0.0.1"}


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestHandleRequestNormal:
    def test_discover_returns_complete_envelope(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MODERN_META}}
        )
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"] == ["2026-07-28"]
        assert result["capabilities"] == {"tools": {}}
        assert isinstance(result["instructions"], str) and result["instructions"]
        assert result["ttlMs"] == 3600000
        assert result["cacheScope"] == "public"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == _SERVER_INFO

    def test_modern_tools_list_returns_complete_envelope(self, server):
        with (
            patch("opencrab.mcp.server.tools_for_principal", return_value=[{"name": "t1"}]),
            patch("opencrab.mcp.server.current_principal"),
        ):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": MODERN_META}}
            )
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 300000
        assert result["cacheScope"] == "private"
        assert "nextCursor" not in result
        assert result["tools"] == [{"name": "t1"}]
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == _SERVER_INFO

    def test_modern_tools_call_success_returns_complete_envelope(self, server):
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": 1}):
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "t1", "arguments": {}, "_meta": MODERN_META},
                }
            )
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert json.loads(result["content"][0]["text"]) == {"ok": 1}
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == _SERVER_INFO

    def test_modern_tools_call_returned_error_dict_sets_is_error(self, server):
        """dispatch_tool returning {"error": ...} WITHOUT raising must still
        flip isError -- and must remain a JSON-RPC result envelope, not an
        error envelope."""
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"error": "denied"}):
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "t1", "arguments": {}, "_meta": MODERN_META},
                }
            )
        assert "error" not in response
        assert response["result"]["isError"] is True

    def test_modern_tools_call_exception_sets_is_error(self, server):
        with patch("opencrab.mcp.server.dispatch_tool", side_effect=ValueError("boom")):
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "t1", "arguments": {}, "_meta": MODERN_META},
                }
            )
        result = response["result"]
        assert result["isError"] is True
        assert "boom" in result["content"][0]["text"]

    def test_legacy_initialize_echoes_requested_protocol_version(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        assert response["result"]["protocolVersion"] == "2025-03-26"

    def test_legacy_initialize_defaults_protocol_version_when_absent(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {}})
        assert response["result"]["protocolVersion"] == "2024-11-05"

    def test_legacy_ping_returns_ok_status(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 6, "method": "ping"})
        assert response["result"]["status"] == "ok"

    def test_legacy_tools_call_envelope_has_no_modern_fields(self, server):
        """Byte-compat pin: the legacy tools/call result must not gain
        resultType/isError/_meta just because the modern era exists now."""
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "t1", "arguments": {}}}
            )
        result = response["result"]
        assert "resultType" not in result
        assert "isError" not in result
        assert "_meta" not in result


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestHandleRequestError:
    def test_modern_meta_with_legacy_version_is_unsupported(self, server):
        """A legacy version number inside a per-request _meta is a
        contradiction (legacy requests never carry _meta at all)."""
        meta = {**MODERN_META, "io.modelcontextprotocol/protocolVersion": "2025-11-25"}
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"_meta": meta}}
        )
        error = response["error"]
        assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
        assert error["data"]["supported"] == ["2026-07-28"]
        assert error["data"]["requested"] == "2025-11-25"

    def test_modern_meta_with_unknown_version_is_unsupported(self, server):
        meta = {**MODERN_META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {"_meta": meta}}
        )
        assert response["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION

    def test_modern_meta_missing_client_capabilities_is_invalid_params(self, server):
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {"_meta": meta}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_discover_without_meta_is_invalid_params_naming_supported_versions(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 10, "method": "server/discover", "params": {}})
        error = response["error"]
        assert error["code"] == INVALID_PARAMS
        assert "2026-07-28" in error["message"]

    def test_modern_ping_is_method_not_found(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 11, "method": "ping", "params": {"_meta": MODERN_META}}
        )
        assert response["error"]["code"] == METHOD_NOT_FOUND

    def test_modern_initialize_is_method_not_found(self, server):
        """initialize is removed from the modern era -- a request that
        carries modern _meta but calls initialize gets -32601, not the
        legacy handshake response."""
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 11, "method": "initialize", "params": {"_meta": MODERN_META}}
        )
        assert response["error"]["code"] == METHOD_NOT_FOUND

    def test_modern_tools_call_unknown_tool_is_invalid_params_not_method_not_found(self, server):
        """Differs from the legacy contract: modern UnknownToolError maps to
        -32602, not the legacy -32601."""
        from opencrab.mcp.tools import UnknownToolError

        with patch(
            "opencrab.mcp.server.dispatch_tool",
            side_effect=UnknownToolError("Unknown tool: 'ghost'. Available: []"),
        ):
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {"name": "ghost", "arguments": {}, "_meta": MODERN_META},
                }
            )
        error = response["error"]
        assert error["code"] == INVALID_PARAMS
        assert "ghost" in error["message"]

    def test_legacy_initialize_unknown_version_negotiates_latest_legacy(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": {"protocolVersion": "1900-01-01"}}
        )
        assert response["result"]["protocolVersion"] == "2025-11-25"

    def test_legacy_initialize_modern_version_negotiates_latest_legacy(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}}
        )
        assert response["result"]["protocolVersion"] == "2025-11-25"

    def test_legacy_tools_call_unknown_tool_still_method_not_found(self, server):
        """Short re-confirmation of the pre-existing legacy contract already
        pinned by test_mcp_server_unit.py -- kept here only to make the
        legacy/modern divergence of the previous test explicit side by
        side."""
        from opencrab.mcp.tools import UnknownToolError

        with patch(
            "opencrab.mcp.server.dispatch_tool",
            side_effect=UnknownToolError("Unknown tool: 'ghost'. Available: []"),
        ):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "ghost", "arguments": {}}}
            )
        assert response["error"]["code"] == METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestHandleRequestEdge:
    def test_non_dict_meta_is_invalid_params(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 15, "method": "tools/list", "params": {"_meta": "2026-07-28"}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_non_string_protocol_version_is_invalid_params(self, server):
        meta = {
            "io.modelcontextprotocol/protocolVersion": 123,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 16, "method": "tools/list", "params": {"_meta": meta}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_non_dict_client_capabilities_is_invalid_params(self, server):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": "caps",
        }
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 17, "method": "tools/list", "params": {"_meta": meta}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_discover_with_non_dict_params_is_invalid_params(self, server):
        response = server.handle_request({"jsonrpc": "2.0", "id": 18, "method": "server/discover", "params": "abc"})
        assert response["error"]["code"] == INVALID_PARAMS

    @pytest.mark.parametrize("bad_args", [None, [], "", 0, False, "x"])
    def test_modern_tools_call_non_object_arguments_invalid_params(self, server, bad_args):
        """PR review R2: a PRESENT non-object `arguments` (explicit null
        included -- key-presence check, same rationale as _meta:null) is
        -32602, and the tool is never dispatched."""
        with patch("opencrab.mcp.server.dispatch_tool") as mock_dispatch:
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "tools/call",
                    "params": {"name": "t1", "arguments": bad_args, "_meta": MODERN_META},
                }
            )
        assert response["error"]["code"] == INVALID_PARAMS
        mock_dispatch.assert_not_called()

    @pytest.mark.parametrize("bad_name", [["tool"], 123, {"a": 1}, "", None])
    def test_modern_tools_call_non_string_name_invalid_params(self, server, bad_name):
        """PR review R4: a non-string (or empty/absent) `name` is malformed
        request metadata -- protocol error -32602 BEFORE dispatch, never a
        tool-execution isError envelope. Same rationale as the R2 arguments
        check."""
        params = {"arguments": {}, "_meta": MODERN_META}
        if bad_name is not None:
            params["name"] = bad_name
        with patch("opencrab.mcp.server.dispatch_tool") as mock_dispatch:
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 33, "method": "tools/call", "params": params}
            )
        assert response["error"]["code"] == INVALID_PARAMS
        mock_dispatch.assert_not_called()

    def test_legacy_tools_call_non_string_name_unchanged(self, server):
        """Regression pin: the LEGACY path keeps its historical behaviour for
        a truthy non-string name (dispatch is entered; the failure surfaces
        as the legacy error envelope, not -32602) -- R4 strictness is
        modern-only."""
        with patch("opencrab.mcp.server.dispatch_tool", side_effect=TypeError("unhashable")) as mock_dispatch:
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 34,
                    "method": "tools/call",
                    "params": {"name": ["tool"], "arguments": {}},
                }
            )
        mock_dispatch.assert_called_once()
        assert "error" not in response  # legacy tool-exception envelope, not JSON-RPC error
        assert "unhashable" in response["result"]["content"][0]["text"]

    def test_modern_tools_call_bad_name_error_message_exact(self, server):
        """Equivalence pin (committed GREEN before the R5 refactor): the
        exact -32602 message must survive the extraction of the call-shape
        checks into protocol.validate_tools_call_params -- including that
        the 'Invalid params: ' prefix appears exactly once."""
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 35, "method": "tools/call",
             "params": {"name": "", "arguments": {}, "_meta": MODERN_META}}
        )
        assert response["error"]["message"] == (
            "Invalid params: 'name' must be a non-empty string in tools/call params."
        )

    def test_modern_tools_call_bad_arguments_error_message_exact(self, server):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 36, "method": "tools/call",
             "params": {"name": "t1", "arguments": [], "_meta": MODERN_META}}
        )
        assert response["error"]["message"] == (
            "Invalid params: 'arguments' must be an object when present"
        )

    def test_modern_tools_call_absent_arguments_dispatches_empty_dict(self, server):
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": 1}) as mock_dispatch:
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "tools/call",
                    "params": {"name": "t1", "_meta": MODERN_META},
                }
            )
        mock_dispatch.assert_called_once_with("t1", {})
        assert response["result"]["isError"] is False

    @pytest.mark.parametrize("legacy_args", [None, []])
    def test_legacy_tools_call_falsey_arguments_unchanged(self, server, legacy_args):
        """Regression pin: the LEGACY path keeps its historical `or {}`
        coercion for falsey arguments -- the R2 strictness is modern-only."""
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": 1}) as mock_dispatch:
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "tools/call",
                    "params": {"name": "t1", "arguments": legacy_args},
                }
            )
        mock_dispatch.assert_called_once_with("t1", {})
        assert "error" not in response

    def test_null_meta_is_invalid_params(self, server):
        """JSON `"_meta": null` is PRESENT-but-non-dict -- a malformed modern
        marker (-32602), never an absent _meta (which would mean legacy).
        Sliding it into the legacy era would bypass modern validation
        entirely (dual-verification round 1, channel B MAJOR)."""
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 24, "method": "tools/list", "params": {"_meta": None}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_notification_with_unsupported_modern_version_returns_none(self, server):
        """A notification (no "id") must get no response of any kind, even
        when the request would otherwise be a protocol-version error."""
        meta = {**MODERN_META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}
        response = server.handle_request({"jsonrpc": "2.0", "method": "tools/list", "params": {"_meta": meta}})
        assert response is None

    def test_legacy_disabled_initialize_is_invalid_params_naming_modern_versions(self):
        server = _make_server("2026-07-28")
        response = server.handle_request({"jsonrpc": "2.0", "id": 20, "method": "initialize", "params": {}})
        error = response["error"]
        assert error["code"] == INVALID_PARAMS
        assert "2026-07-28" in error["message"]

    def test_legacy_disabled_tools_call_without_meta_is_invalid_params(self):
        server = _make_server("2026-07-28")
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "t1", "arguments": {}}}
            )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_legacy_disabled_modern_tools_list_still_works(self):
        server = _make_server("2026-07-28")
        with (
            patch("opencrab.mcp.server.tools_for_principal", return_value=[{"name": "t1"}]),
            patch("opencrab.mcp.server.current_principal"),
        ):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 20, "method": "tools/list", "params": {"_meta": MODERN_META}}
            )
        assert response["result"]["resultType"] == "complete"

    def test_legacy_only_modern_request_is_unsupported_with_empty_supported(self):
        server = _make_server("2025-11-25,2024-11-05")
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {"_meta": MODERN_META}}
        )
        error = response["error"]
        assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
        assert error["data"]["supported"] == []

    def test_legacy_only_ping_still_works(self):
        server = _make_server("2025-11-25,2024-11-05")
        response = server.handle_request({"jsonrpc": "2.0", "id": 21, "method": "ping"})
        assert response["result"]["status"] == "ok"

    def test_unknown_configured_version_refuses_startup(self):
        with pytest.raises(RuntimeError):
            _make_server("9999-01-01")

    def test_empty_string_configured_versions_enables_everything(self):
        server = _make_server("")
        response = server.handle_request({"jsonrpc": "2.0", "id": 23, "method": "ping"})
        assert response["result"]["status"] == "ok"
