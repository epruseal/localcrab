"""#512 MCP UTF-8 response transport contracts."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from opencrab.mcp.server import MCPServer


@pytest.fixture
def server():
    with patch("opencrab.mcp.server.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            mcp_server_name="opencrab-test",
            mcp_server_version="0.0.1",
            mcp_protocol_versions=None,
        )
        return MCPServer()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_tool_result_replaces_lone_surrogate(surrogate):
    from opencrab.common.json_transport import serialize_tool_result

    text = serialize_tool_result({"key": surrogate, surrogate: "value"})

    assert json.loads(text) == {"key": "�", "�": "value"}
    assert "�" in text


def test_tool_result_preserves_valid_unicode_and_json_escapes():
    from opencrab.common.json_transport import serialize_tool_result

    value = {"text": "한é😀😀\\ud800\x00"}
    text = serialize_tool_result(value)

    assert json.loads(text) == {"text": "한é😀😀\\ud800\x00"}
    assert "한é😀" in text
    assert "\\\\ud800" in text
    assert "\\u0000" in text


def test_outer_response_keeps_lone_surrogate_id_as_escape():
    from opencrab.common.json_transport import serialize_jsonrpc_response

    text = serialize_jsonrpc_response({"jsonrpc": "2.0", "id": "\ud800", "result": {}})

    assert "\\ud800" in text
    assert json.loads(text)["id"] == "\ud800"


@pytest.mark.parametrize("modern", [False, True])
def test_tool_calls_emit_raw_unicode_and_clean_lone_surrogate(server, modern):
    params = {"name": "tool", "arguments": {}}
    if modern:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    with patch("opencrab.mcp.server.dispatch_tool", return_value={"text": "한\ud800"}):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )

    content = response["result"]["content"][0]["text"]
    assert "한" in content
    assert "\\ud800" not in content
    assert json.loads(content) == {"text": "한�"}


class _PendingBytes:
    def __init__(self) -> None:
        self._published = io.BytesIO()
        self._pending = bytearray()
        self.flush_count = 0

    def write(self, data: bytes) -> int:
        self._pending.extend(data)
        return len(data)

    def flush(self) -> None:
        self._published.write(self._pending)
        self._pending.clear()
        self.flush_count += 1

    def getvalue(self) -> bytes:
        return self._published.getvalue()


class _BinarySink:
    def __init__(self) -> None:
        self.buffer = _PendingBytes()

    def write(self, text: str) -> None:
        raise AssertionError(f"text output used: {text!r}")


def test_stdio_writes_utf8_bytes_and_flushes(server, monkeypatch):
    stdout = _BinarySink()
    monkeypatch.setattr("opencrab.mcp.server.sys.stdin", io.StringIO("request\n"))
    monkeypatch.setattr(
        server,
        "_handle_raw",
        lambda _request: {"jsonrpc": "2.0", "id": 1, "result": {"text": "한"}},
    )
    monkeypatch.setattr("opencrab.mcp.server.sys.stdout", stdout)

    server.run()

    payload = stdout.buffer.getvalue()
    assert payload.endswith(b"\n")
    assert "한".encode() in payload
    assert json.loads(payload.decode("utf-8"))["result"]["text"] == "한"
    assert stdout.buffer.flush_count == 1
    assert not stdout.buffer._pending


def test_wire_length_matches_tool_content_outer_json_bytes():
    from opencrab.common.json_transport import serialize_tool_result
    from opencrab.services.result_caps import _wire_length

    value = {"text": "한\ud800\\ud800"}
    text = serialize_tool_result(value)

    assert _wire_length(value) == len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def test_wire_length_uses_utf8_bytes_for_non_ascii():
    from opencrab.services.result_caps import _wire_length

    base = _wire_length({"a": ""})
    assert _wire_length({"a": "한"}) - base == len(json.dumps("한", ensure_ascii=False).encode("utf-8")) - 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_http_json_response_rejects_nonfinite_value(value):
    from opencrab.mcp.http_app import _jsonrpc_response

    with pytest.raises(ValueError):
        _jsonrpc_response({"id": value})
