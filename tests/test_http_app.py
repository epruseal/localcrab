"""
Contract tests for opencrab/mcp/http_app.py — Streamable HTTP MCP transport.

Covers: bearer/query-param auth (_check/_matches), token resolution
precedence (_resolve_token), JSON-RPC batch/notification semantics on
POST /mcp, and the auth-exempt /healthz probe endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opencrab.mcp.http_app import _resolve_token, create_app


def test_composite_api_lifespan_closes_embedded_mcp_context(monkeypatch):
    from fastapi.testclient import TestClient

    from apps.api import main as api_main

    context = object()
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(api_main, "_build_context", lambda: context)
        closed_api: list[object] = []
        patcher.setattr(api_main, "_close_context", closed_api.append)
        closed_mcp: list[bool] = []
        patcher.setattr(
            "opencrab.mcp.tools.close_context", lambda: closed_mcp.append(True)
        )
        with TestClient(api_main.app):
            pass
    assert closed_api == [context]
    assert closed_mcp == [True]

# ---------------------------------------------------------------------------
# _resolve_token — precedence: CLI > env > file
# ---------------------------------------------------------------------------


class TestResolveToken:
    def test_no_source_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN", raising=False)
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN_FILE", raising=False)
        assert _resolve_token() is None

    def test_cli_token_wins_over_env_and_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALCRAB_MCP_TOKEN", "env-token")
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token")
        assert _resolve_token("cli-token", str(token_file)) == "cli-token"

    def test_env_wins_over_file_when_no_cli(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALCRAB_MCP_TOKEN", "env-token")
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token")
        assert _resolve_token(None, str(token_file)) == "env-token"

    def test_file_used_when_no_cli_and_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN", raising=False)
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token")
        assert _resolve_token(None, str(token_file)) == "file-token"

    def test_file_token_trailing_newline_is_stripped(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN", raising=False)
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token\n")
        assert _resolve_token(None, str(token_file)) == "file-token"

    def test_blank_env_falls_through_to_file(self, monkeypatch, tmp_path):
        # env set but whitespace-only → "".strip() is falsy → falls through
        monkeypatch.setenv("LOCALCRAB_MCP_TOKEN", "   ")
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token")
        assert _resolve_token(None, str(token_file)) == "file-token"

    def test_token_file_env_var_used_when_no_cli_file_arg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN", raising=False)
        token_file = tmp_path / "token.txt"
        token_file.write_text("env-file-token")
        monkeypatch.setenv("LOCALCRAB_MCP_TOKEN_FILE", str(token_file))
        assert _resolve_token() == "env-file-token"


# ---------------------------------------------------------------------------
# Auth: open access when no auth_token configured
# ---------------------------------------------------------------------------


class TestNoAuthConfigured:
    @pytest.fixture
    def client(self):
        return TestClient(create_app(auth_token=None))

    def test_tools_list_open_access(self, client):
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 200
        assert "tools" in resp.json()["result"]

    def test_healthz_always_open(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"


# ---------------------------------------------------------------------------
# Auth: auth_token configured — normal (correct credential) paths
# ---------------------------------------------------------------------------


class TestAuthConfiguredNormal:
    @pytest.fixture
    def client(self):
        return TestClient(create_app(auth_token="secret"))

    def test_correct_bearer_header_200(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 200

    def test_correct_query_token_200(self, client):
        resp = client.post(
            "/mcp?token=secret",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 200

    def test_healthz_exempt_even_with_auth_configured(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_bearer_token_with_surrounding_whitespace_matches(self, client):
        # _matches() strips the *candidate* side only — a client that sends
        # extra whitespace around an otherwise-correct token still matches.
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer   secret  "},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth: auth_token configured — error (wrong/missing credential) paths
# ---------------------------------------------------------------------------


class TestAuthConfiguredErrors:
    @pytest.fixture
    def client(self):
        return TestClient(create_app(auth_token="secret"))

    def test_missing_token_401_with_www_authenticate(self, client):
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_wrong_bearer_token_401(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_wrong_query_token_401(self, client):
        resp = client.post(
            "/mcp?token=wrong",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401

    def test_non_bearer_scheme_ignored_401(self, client):
        # Basic-scheme credentials never reach _matches; falls through to
        # the query-param check and, absent that, 401.
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Basic secret"},
        )
        assert resp.status_code == 401

    def test_malformed_json_body_400_not_500(self, client):
        resp = client.post(
            "/mcp",
            content=b"{not valid json",
            headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == -32700

    def test_get_mcp_returns_405_with_allow_header(self, client):
        resp = client.get("/mcp", headers={"Authorization": "Bearer secret"})
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST, DELETE"

    def test_get_mcp_without_auth_401_before_405(self, client):
        # auth is checked before the stateless-405 response
        resp = client.get("/mcp")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Edge: batch requests, notifications, DELETE
# ---------------------------------------------------------------------------


class TestBatchAndNotifications:
    @pytest.fixture
    def client(self):
        return TestClient(create_app(auth_token=None))

    def test_batch_two_requests_returns_array_of_responses(self, client):
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert [r["id"] for r in body] == [1, 2]

    def test_batch_all_notifications_returns_202_empty(self, client):
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ],
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_empty_batch_list_returns_202(self, client):
        resp = client.post("/mcp", json=[])
        assert resp.status_code == 202
        assert resp.content == b""

    def test_single_notification_no_id_returns_202_empty_body(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_mixed_batch_notification_and_request(self, client):
        # A notification alongside a real request: only the request gets a
        # response entry, but the batch itself is still a 200 array (not 202)
        # because `out` is non-empty.
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == 1

    def test_delete_mcp_returns_200(self, client):
        resp = client.delete("/mcp")
        assert resp.status_code == 200
