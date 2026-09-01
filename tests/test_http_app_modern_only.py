"""
Configuration-axis contract tests for issue #250: HTTP error status alignment
when the legacy era is DISABLED (``MCP_PROTOCOL_VERSIONS=2026-07-28``,
"modern-only"), plus explicit pins separating the expected surfaces of the
modern-only, dual (default), and legacy-only configurations.

Design of record: /home/asdf/orch-scratch/o250/design-v2.md. TDD RED file:
at commit time the ``TestModernOnlyTransportErrors`` group below is expected
to FAIL against main (errors come back 200/202/500); the normal-path and
configuration-pin groups pass before and after the fix and are regression
pins, not mutation targets.

Rationale (issue #250, follow-up to #136/#243): a modern-only server serves
no legacy era, so its rejection of legacy-shaped or non-object traffic is a
modern transport error and must carry the documented modern status mapping
(validation faults 400; batch arrays and non-object bodies 400 + -32600;
rejected notifications 400 with an empty body). The single-request JSON-RPC
error envelopes are byte-identical to what the same modern-only configuration
produced before this fix -- only the HTTP status changes.

Fixtures/helpers are deliberately duplicated from tests/test_http_app_modern.py
(same policy as that file states in its own docstring): this file owns the
configuration-axis contract independently.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opencrab.auth import bootstrap_local_user
from opencrab.mcp.http_app import create_app
from opencrab.stores.sql_store import SQLStore

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}

ALL_LEGACY = "2025-11-25,2025-06-18,2025-03-26,2024-11-05"

# Exact envelope strings pinned byte-for-byte: the "envelope unchanged" claim
# of #250 is only testable with full-dict equality (a partial-message check
# would let id/field/message drift pass -- verification round F1).
METADATA_REQUIRED_MSG = (
    "Protocol version metadata is required "
    "('io.modelcontextprotocol/protocolVersion' in params._meta); "
    "supported: 2026-07-28"
)
BATCH_REJECTED_MSG = (
    "JSON-RPC batch is a legacy-only extension; modern (2026-07-28) "
    "requests must be sent individually"
)
NON_OBJECT_BODY_MSG = "Invalid Request: the request body must be a JSON object"


def _error_envelope(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Shared fixtures (duplicated from test_http_app_modern.py -- see docstring)
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrapped(tmp_path):
    """(sql, user_id, secret) for a freshly bootstrapped local user+token."""
    sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    user_id, secret = bootstrap_local_user(sql)
    return sql, user_id, secret


def _stub_context(sql, **overrides):
    ctx = {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": sql,
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    ctx.update(overrides)
    return ctx


def _client_for_versions(bootstrapped, tmp_path, monkeypatch, versions):
    """TestClient under an explicit MCP_PROTOCOL_VERSIONS configuration.

    ``versions=None`` means the DUAL DEFAULT, and the variable is explicitly
    ``delenv``-ed (not merely left alone): a host environment that happens to
    export MCP_PROTOCOL_VERSIONS must not silently turn the dual pins below
    into a different configuration (design v2 §5).
    """
    from opencrab.config import get_settings
    from opencrab.mcp import tools as tools_pkg

    sql, _user_id, _secret = bootstrapped
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    if versions is None:
        monkeypatch.delenv("MCP_PROTOCOL_VERSIONS", raising=False)
    else:
        monkeypatch.setenv("MCP_PROTOCOL_VERSIONS", versions)
    get_settings.cache_clear()
    monkeypatch.setattr(tools_pkg, "_get_context", lambda: _stub_context(sql))
    yield TestClient(create_app())
    get_settings.cache_clear()


@pytest.fixture
def client_modern_only(bootstrapped, tmp_path, monkeypatch):
    yield from _client_for_versions(bootstrapped, tmp_path, monkeypatch, "2026-07-28")


@pytest.fixture
def client_dual(bootstrapped, tmp_path, monkeypatch):
    yield from _client_for_versions(bootstrapped, tmp_path, monkeypatch, None)


@pytest.fixture
def client_legacy_only(bootstrapped, tmp_path, monkeypatch):
    yield from _client_for_versions(bootstrapped, tmp_path, monkeypatch, ALL_LEGACY)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _modern_body(method: str, params: dict | None = None, id: int | None = 1) -> dict:
    merged = dict(params or {})
    merged["_meta"] = dict(MODERN_META)
    body: dict = {"jsonrpc": "2.0", "method": method, "params": merged}
    if id is not None:
        body["id"] = id
    return body


def _modern_headers(
    secret: str, method: str, name: str | None = None, version: str = "2026-07-28"
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {secret}",
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


# ---------------------------------------------------------------------------
# Normal: the modern path is untouched by the modern-only status fix
# ---------------------------------------------------------------------------


class TestModernOnlyNormal:
    def test_modern_discover_still_200(self, client_modern_only, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            json=_modern_body("server/discover"),
            headers=_modern_headers(secret, "server/discover"),
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["supportedVersions"] == ["2026-07-28"]

    def test_valid_modern_tools_call_notification_still_202(
        self, client_modern_only, bootstrapped
    ):
        _, _, secret = bootstrapped
        body = _modern_body(
            "tools/call", params={"name": "schema_pack_list", "arguments": {}}, id=None
        )
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": 1}) as mock_dispatch:
            resp = client_modern_only.post(
                "/mcp",
                json=body,
                headers=_modern_headers(secret, "tools/call", name="schema_pack_list"),
            )
        assert resp.status_code == 202
        assert resp.content == b""
        mock_dispatch.assert_called_once_with("schema_pack_list", {})


# ---------------------------------------------------------------------------
# Error: issue #250 body -- modern-only transport statuses (RED group; the
# reverse-mutation gate re-fails exactly this class)
# ---------------------------------------------------------------------------


class TestModernOnlyTransportErrors:
    def test_legacy_initialize_400_envelope_unchanged(self, client_modern_only, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json() == _error_envelope(
            1,
            -32602,
            "The initialize handshake is disabled on this server; send "
            "per-request _meta with a supported protocol version: 2026-07-28",
        )

    def test_legacy_tools_list_400_envelope_unchanged(self, client_modern_only, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json() == _error_envelope(2, -32602, METADATA_REQUIRED_MSG)

    def test_legacy_shaped_unknown_method_400_keeps_minus_32602(
        self, client_modern_only, bootstrapped
    ):
        """The legacy-disabled gate rejects BEFORE dispatch, so an unknown
        method surfaces as the same metadata-required -32602 (not -32601) --
        only the HTTP status changes with this fix."""
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "nope/nope", "params": {}},
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json() == _error_envelope(3, -32602, METADATA_REQUIRED_MSG)

    def test_non_string_method_400_envelope_unchanged(self, client_modern_only, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": 42, "params": {}},
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json() == _error_envelope(4, -32600, "Missing or invalid 'method'.")

    @pytest.mark.parametrize(
        "batch",
        [
            [
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
            ],
            [],
        ],
        ids=["legacy-batch", "empty-batch"],
    )
    def test_batch_rejected_400_single_error_object(
        self, client_modern_only, bootstrapped, batch
    ):
        """Batch arrays are a legacy-only extension; with the legacy era off,
        EVERY array (empty included) is rejected with the same single
        -32600 error object the modern-flagged batch rejection already uses."""
        _, _, secret = bootstrapped
        resp = client_modern_only.post("/mcp", json=batch, headers=_auth(secret))
        assert resp.status_code == 400
        assert resp.json() == _error_envelope(None, -32600, BATCH_REJECTED_MSG)

    def test_legacy_shaped_notification_400_empty_body(self, client_modern_only, bootstrapped):
        """handle_request rightly stays silent for notifications, so the
        transport must reject a legacy-shaped notification itself: 400 with
        an empty body (the error body is a spec MAY for rejected
        notifications), consistent with the modern body-fault notifications."""
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.content == b""

    @pytest.mark.parametrize(
        "raw", ['"hello"', "null", "42", "true"], ids=["string", "null", "number", "bool"]
    )
    def test_scalar_body_400_invalid_request(self, client_modern_only, bootstrapped, raw):
        """A JSON body that is neither an object nor an array is Invalid
        Request on a modern-only server (400 + -32600), never a 500 from the
        dict-presuming dispatch layer."""
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp",
            content=raw,
            headers={**_auth(secret), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json() == _error_envelope(None, -32600, NON_OBJECT_BODY_MSG)


# ---------------------------------------------------------------------------
# Edge: explicit configuration-separation pins (dual default / legacy-only)
# ---------------------------------------------------------------------------


class TestDualConfigurationPins:
    def test_legacy_initialize_still_200_result(self, client_dual, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_dual.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["protocolVersion"] == "2025-03-26"

    def test_legacy_batch_still_200_array(self, client_dual, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_dual.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
            ],
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert isinstance(payload, list)
        assert [item["id"] for item in payload] == [5, 6]

    def test_empty_batch_still_202(self, client_dual, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_dual.post("/mcp", json=[], headers=_auth(secret))
        assert resp.status_code == 202
        assert resp.content == b""

    def test_legacy_notification_still_202(self, client_dual, bootstrapped):
        _, _, secret = bootstrapped
        resp = client_dual.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_auth(secret),
        )
        assert resp.status_code == 202
        assert resp.content == b""


class TestLegacyOnlyConfigurationPins:
    def test_legacy_unknown_method_stays_200_with_minus_32601(
        self, client_legacy_only, bootstrapped
    ):
        """With the legacy era ON, legacy-shaped errors keep the pre-#136
        blanket 200 -- the modern status mapping must not leak into it."""
        _, _, secret = bootstrapped
        resp = client_legacy_only.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "nope/nope", "params": {}},
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32601

    def test_modern_request_reaches_minus_32022_with_empty_supported(
        self, client_legacy_only, bootstrapped
    ):
        """Matching modern headers pass header validation, so the request
        reaches the BODY fault: -32022 with data.supported == [] (asserting
        the code guards against a -32020 header fault false-positive)."""
        _, _, secret = bootstrapped
        resp = client_legacy_only.post(
            "/mcp",
            json=_modern_body("tools/list"),
            headers=_modern_headers(secret, "tools/list"),
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == -32022
        assert error["data"]["supported"] == []
