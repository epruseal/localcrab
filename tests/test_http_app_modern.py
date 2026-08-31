"""
Contract tests for the 2026-07-28 "modern" Streamable HTTP transport layer in
opencrab/mcp/http_app.py -- issue #136.

Design of record: /home/asdf/orch-scratch/o136/design-v4.md §4.3. This is the
TDD RED file: none of protocol.py, the header-validation branch, the batch
boundary guard, the Origin-guard middleware, or the MCP_ALLOWED_ORIGINS /
MCP_PROTOCOL_VERSIONS settings exist yet, so every test below is expected to
fail until the corresponding implementation lands.

Era判定 (design §4.1): a request body is "modern" iff its ``params._meta``
carries the ``io.modelcontextprotocol/protocolVersion`` key, or its method is
``server/discover`` (always modern regardless of ``_meta``). At the HTTP
transport, header validation runs BEFORE body ``_meta`` validation (design
§4.3.1) -- that ordering is pinned explicitly below (see test 17).

Fixtures/helpers are deliberately duplicated from tests/test_http_app.py
rather than imported, per this file's own scope: this file owns the modern
transport contract independently of that file's legacy-auth contract.
"""

from __future__ import annotations

import base64
import sys
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


# ---------------------------------------------------------------------------
# Shared fixtures (duplicated from test_http_app.py -- see module docstring)
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


@pytest.fixture
def client(bootstrapped, tmp_path, monkeypatch):
    """TestClient whose auth reads the same file the fixture bootstrapped into."""
    from opencrab.config import get_settings
    from opencrab.mcp import tools as tools_pkg

    sql, _user_id, _secret = bootstrapped
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    get_settings.cache_clear()
    monkeypatch.setattr(tools_pkg, "_get_context", lambda: _stub_context(sql))
    yield TestClient(create_app())
    get_settings.cache_clear()


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _modern_body(method: str, params: dict | None = None, id: int | None = 1) -> dict:
    """A JSON-RPC request body carrying modern ``_meta``.

    ``params`` (if given) is merged with ``_meta`` -- e.g. pass
    ``{"name": "schema_pack_list", "arguments": {}}`` for a ``tools/call``.
    ``id=None`` produces a notification (no ``id`` field at all).
    """
    merged = dict(params or {})
    merged["_meta"] = MODERN_META
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
# Normal
# ---------------------------------------------------------------------------


class TestModernNormal:
    def test_modern_tools_list_envelope(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/list"),
            headers=_modern_headers(secret, "tools/list"),
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["resultType"] == "complete"
        assert "ttlMs" in result
        assert result["cacheScope"] == "private"

    def test_modern_tools_call_read_tool(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/call", params={"name": "schema_pack_list", "arguments": {}}),
            headers=_modern_headers(secret, "tools/call", name="schema_pack_list"),
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["isError"] is False

    def test_modern_tools_call_mcp_name_base64_sentinel(self, client, bootstrapped):
        _, _, secret = bootstrapped
        name_sentinel = "=?base64?" + base64.b64encode(b"schema_pack_list").decode() + "?="
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/call", params={"name": "schema_pack_list", "arguments": {}}),
            headers=_modern_headers(secret, "tools/call", name=name_sentinel),
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("origin", ["http://localhost:3000", "https://127.0.0.1:8443"])
    def test_localhost_and_loopback_origins_pass(self, client, bootstrapped, origin):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/list"),
            headers={**_modern_headers(secret, "tools/list"), "Origin": origin},
        )
        assert resp.status_code == 200

    def test_mcp_allowed_origins_env_allowlists_extra_origin(self, bootstrapped, tmp_path, monkeypatch):
        from opencrab.config import get_settings
        from opencrab.mcp import tools as tools_pkg

        sql, _user_id, secret = bootstrapped
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("STORAGE_MODE", "local")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://claude.ai")
        get_settings.cache_clear()
        monkeypatch.setattr(tools_pkg, "_get_context", lambda: _stub_context(sql))
        try:
            allowlisted_client = TestClient(create_app())
            resp = allowlisted_client.post(
                "/mcp",
                json=_modern_body("tools/list"),
                headers={**_modern_headers(secret, "tools/list"), "Origin": "https://claude.ai"},
            )
            assert resp.status_code == 200
        finally:
            get_settings.cache_clear()

    def test_legacy_body_with_legacy_protocol_version_header_is_unvalidated(self, client, bootstrapped):
        """Core regression guard: a 2025-06-18 legacy client sends
        MCP-Protocol-Version without Mcp-Method/Mcp-Name, and it must not be
        subjected to modern header validation."""
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**_auth(secret), "MCP-Protocol-Version": "2025-06-18"},
        )
        assert resp.status_code == 200

    def test_legacy_batch_contract_unchanged(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert [r["id"] for r in body] == [1, 2]

    def test_modern_tool_execution_error_is_200_with_is_error_true(self, client, bootstrapped):
        _, _, secret = bootstrapped
        with patch("opencrab.mcp.server.dispatch_tool", side_effect=ValueError("boom")):
            resp = client.post(
                "/mcp",
                json=_modern_body("tools/call", params={"name": "schema_pack_list", "arguments": {}}),
                headers=_modern_headers(secret, "tools/call", name="schema_pack_list"),
            )
        assert resp.status_code == 200
        assert resp.json()["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestModernHeaderErrors:
    def test_missing_protocol_version_header_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post("/mcp", json=_modern_body("tools/list"), headers=_auth(secret))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_protocol_version_header_body_mismatch_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        body = _modern_body("tools/list")
        body["params"]["_meta"] = {
            **MODERN_META,
            "io.modelcontextprotocol/protocolVersion": "1900-01-01",
        }
        resp = client.post("/mcp", json=body, headers=_modern_headers(secret, "tools/list"))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_missing_mcp_method_header_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        headers = {**_auth(secret), "MCP-Protocol-Version": "2026-07-28"}
        resp = client.post("/mcp", json=_modern_body("tools/list"), headers=headers)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_mcp_method_header_body_mismatch_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/list"),
            headers=_modern_headers(secret, "tools/call"),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_missing_mcp_name_for_tools_call_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/call", params={"name": "schema_pack_list", "arguments": {}}),
            headers=_modern_headers(secret, "tools/call"),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_mcp_name_header_body_mismatch_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/call", params={"name": "schema_pack_list", "arguments": {}}),
            headers=_modern_headers(secret, "tools/call", name="wrong_name"),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_null_meta_body_is_modern_and_header_checked(self, client, bootstrapped):
        """A body with `"_meta": null` must not slide into the legacy era:
        it is a malformed modern marker, so header-first validation reports
        the missing MCP-Protocol-Version header (-32020/400), never a 200
        legacy envelope (dual-verification round 1, channel B MAJOR)."""
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": None}},
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    def test_modern_headers_with_legacy_body_400(self, client, bootstrapped):
        """[v2/M2] modern MCP-Protocol-Version header + a legacy (no-_meta)
        body must not slip through as legacy -- otherwise header validation
        is a no-op bypass for legacy-shaped traffic."""
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_modern_headers(secret, "tools/list"),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020


class TestModernBodyErrors:
    def test_unsupported_version_matching_header_and_body_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        body = _modern_body("tools/list")
        body["params"]["_meta"] = {
            **MODERN_META,
            "io.modelcontextprotocol/protocolVersion": "1900-01-01",
        }
        resp = client.post(
            "/mcp", json=body, headers=_modern_headers(secret, "tools/list", version="1900-01-01")
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == -32022
        assert error["data"]["supported"] == ["2026-07-28"]

    def test_unknown_modern_method_404(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("no/such"),
            headers=_modern_headers(secret, "no/such"),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == -32601

    def test_missing_client_capabilities_400(self, client, bootstrapped):
        _, _, secret = bootstrapped
        body = _modern_body("tools/list")
        del body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
        resp = client.post("/mcp", json=body, headers=_modern_headers(secret, "tools/list"))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32602

    def test_discover_without_meta_or_headers_is_header_error_not_body_error(self, client, bootstrapped):
        """[v3/R2-1] header validation runs before body ``_meta`` validation.
        A ``server/discover`` POST with no ``_meta`` and no MCP-* headers must
        surface -32020 (missing MCP-Protocol-Version), not the -32602 that a
        stdio transport (no header layer) would raise for the same body."""
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020


class TestOriginGuard:
    def test_disallowed_origin_403_with_auth_and_no_store(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/list"),
            headers={**_modern_headers(secret, "tools/list"), "Origin": "https://evil.example"},
        )
        assert resp.status_code == 403
        assert resp.headers["cache-control"] == "no-store"

    def test_disallowed_origin_403_without_auth(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403

    def test_disallowed_origin_403_on_get(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.get("/mcp", headers={**_auth(secret), "Origin": "https://evil.example"})
        assert resp.status_code == 403

    def test_disallowed_origin_403_on_unrouted_method(self, client):
        resp = client.put("/mcp", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "origin",
        [
            "http://localhost/evil",  # path attached
            "http://evil@localhost",  # userinfo
            "http://localhost:",  # empty port
            "http://localhost:bad",  # non-numeric port
            "http://localhost:99999",  # out-of-range port
            "http://[::1",  # malformed IPv6 -- urlsplit itself raises
        ],
    )
    def test_malformed_or_decorated_loopback_origin_403_not_500(self, client, origin):
        """A present-but-invalid Origin must be 403, never a pass (the
        loopback hostname does not sanctify a non-bare origin shape) and
        never a 500 (urlsplit's own ValueError must be contained)."""
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Origin": origin},
        )
        assert resp.status_code == 403

    def test_valid_ipv6_loopback_origin_passes(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("tools/list"),
            headers={**_modern_headers(secret, "tools/list"), "Origin": "http://[::1]:8080"},
        )
        assert resp.status_code == 200

    def test_ipv6_allowlist_entry_parses_and_passes(self, bootstrapped, tmp_path, monkeypatch):
        """A bracketed IPv6 allowlist entry must survive the shared
        bare-origin parser and then admit exactly that Origin."""
        from opencrab.config import get_settings
        from opencrab.mcp import tools as tools_pkg

        sql, _user_id, secret = bootstrapped
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("STORAGE_MODE", "local")
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://[2001:db8::1]")
        get_settings.cache_clear()
        monkeypatch.setattr(tools_pkg, "_get_context", lambda: _stub_context(sql))
        try:
            allowlisted_client = TestClient(create_app())
            resp = allowlisted_client.post(
                "/mcp",
                json=_modern_body("tools/list"),
                headers={**_modern_headers(secret, "tools/list"), "Origin": "https://[2001:db8::1]"},
            )
            assert resp.status_code == 200
        finally:
            get_settings.cache_clear()


class TestModernBatchBoundary:
    def test_batch_with_modern_meta_element_rejected(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post("/mcp", json=[_modern_body("tools/list")], headers=_auth(secret))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600

    def test_batch_with_modern_header_and_legacy_elements_rejected(self, client, bootstrapped):
        """[v3/R2-2] a modern MCP-Protocol-Version header attached to an
        otherwise fully legacy array body is itself a bypass attempt."""
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
            headers={**_auth(secret), "MCP-Protocol-Version": "2026-07-28"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600

    def test_batch_with_non_dict_meta_element_rejected(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": "x"}}],
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600


class TestMalformedAllowedOriginsConfig:
    @pytest.mark.parametrize(
        "bad_origin",
        [
            "https://claude.ai/path",
            "null",
            "ftp://x",
            "https://a.example,,https://b.example",  # empty entry (stray comma)
            "https://claude.ai:bad",  # non-numeric port
        ],
    )
    def test_create_app_refuses_malformed_origin(self, bad_origin, monkeypatch):
        from opencrab.config import get_settings

        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", bad_origin)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError):
                create_app()
        finally:
            monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
            get_settings.cache_clear()

    @pytest.mark.parametrize("bad_origin", ["https://claude.ai/path", "null", "ftp://x"])
    def test_apps_api_import_refuses_malformed_origin(self, bad_origin, monkeypatch):
        """apps/api validates MCP_ALLOWED_ORIGINS at module-import time (the
        2-mount contract: both surfaces that mount mcp_router must fail
        before serving, not just the standalone create_app() one)."""
        from opencrab.config import get_settings

        module_name = "apps.api.main"
        original_module = sys.modules.get(module_name)
        sys.modules.pop(module_name, None)
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", bad_origin)
        monkeypatch.setenv("LOCAL_DATA_DIR", "/tmp/does-not-matter-for-import")
        monkeypatch.setenv("STORAGE_MODE", "local")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError):
                import apps.api.main  # noqa: F401
        finally:
            sys.modules.pop(module_name, None)
            if original_module is not None:
                sys.modules[module_name] = original_module
            monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestModernEdgeCases:
    def test_modern_notification_returns_202_empty_body(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=_modern_body("notifications/cancelled", id=None),
            headers=_modern_headers(secret, "notifications/cancelled"),
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_allowed_origin_then_existing_get_405_rule_applies(self, client, bootstrapped):
        """An allowed Origin must not change the (already-existing) GET
        semantics -- Origin passing is a gate, not a bypass of other rules."""
        _, _, secret = bootstrapped
        resp = client.get(
            "/mcp", headers={**_auth(secret), "Origin": "http://localhost:5173"}
        )
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST"

    def test_empty_legacy_batch_returns_202(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post("/mcp", json=[], headers=_auth(secret))
        assert resp.status_code == 202
        assert resp.content == b""
