"""
Contract tests for opencrab/mcp/http_app.py — Streamable HTTP MCP transport.

#145: the shared-secret auth model (an optional single ``auth_token``, a
``?token=`` query param, and a "no token configured -> open" fallback) is
deleted outright. Every ``/mcp`` request now requires a valid per-user
bearer token verified via ``opencrab.auth.verify_token`` against a real
``users``/``api_tokens`` SQLStore -- there is no unauthenticated mode.
Covers: bearer-token auth (valid / missing / wrong / revoked / disabled-user),
the deleted query-param form, JSON-RPC batch/notification semantics on
POST /mcp, the auth-exempt /healthz probe, `refuse_stale_shared_secret_env`,
and #143 invariant 2 (dispatch_tool rejects a client-supplied subject_id
rather than silently accepting it, and audit attribution matches the token
owner regardless of client input).
"""

from __future__ import annotations

import json as json_module
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opencrab.auth import bootstrap_local_user, disable_user, list_tokens, revoke_token
from opencrab.mcp.http_app import create_app, refuse_stale_shared_secret_env
from opencrab.stores.sql_store import SQLStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrapped(tmp_path):
    """(sql, user_id, secret) for a freshly bootstrapped local user+token.
    A real file-backed SQLStore, not a mock or ``:memory:`` -- verify_token
    needs real sha256 hash-equality lookups against actual rows, and
    Starlette's TestClient runs the ASGI app on a separate thread from the
    one that bootstraps the user here; ``sqlite:///:memory:`` would hand
    that thread a distinct, empty in-memory database (SQLAlchemy's default
    SingletonThreadPool keys connections per-thread) instead of the one just
    populated."""
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
    """TestClient whose auth reads the same file the fixture bootstrapped into.

    LOCAL_DATA_DIR is pointed at tmp_path because #145 moved token lookup off
    the MCP tool context and onto its own minimal store: routing auth through
    _get_context meant an unauthenticated request built every store -- graph,
    doc, vector, billing -- so one junk token left nine database files behind.
    Auth now resolves its store from settings, which is also closer to what
    production does; the _get_context stub below is still needed, but only for
    the tool-dispatch side, where these tests want MagicMocks rather than real
    stores."""
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


# ---------------------------------------------------------------------------
# Auth: valid bearer token
# ---------------------------------------------------------------------------


class TestAuthValidToken:
    def test_tools_list_200_with_valid_bearer(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        assert "tools" in resp.json()["result"]

    def test_healthz_always_open(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_bearer_token_with_surrounding_whitespace_still_matches(self, client, bootstrapped):
        # FastAPI's own HTTPBearer/get_authorization_scheme_param strips
        # whitespace around the credential before _check ever sees it
        # (confirmed directly) -- unlike the deleted shared-secret _matches(),
        # opencrab code does no stripping of its own; this pins that the
        # verify_token hash lookup still receives the clean token.
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer   {secret}  "},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth: missing / wrong / revoked / disabled-user / query-param token
# ---------------------------------------------------------------------------


class TestAuthErrors:
    def test_missing_token_401_with_www_authenticate(self, client):
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_wrong_bearer_token_401(self, client):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_auth("not-a-real-token"),
        )
        assert resp.status_code == 401

    def test_non_bearer_scheme_401(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Basic {secret}"},
        )
        assert resp.status_code == 401

    def test_revoked_token_401(self, client, bootstrapped):
        sql, user_id, secret = bootstrapped
        token_id = list_tokens(sql, user_id)[0]["token_id"]
        revoke_token(sql, token_id)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_auth(secret),
        )
        assert resp.status_code == 401

    def test_disabled_user_token_401(self, client, bootstrapped):
        sql, user_id, secret = bootstrapped
        disable_user(sql, user_id)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_auth(secret),
        )
        assert resp.status_code == 401

    def test_query_param_token_does_not_authenticate(self, client, bootstrapped):
        # #145, #143 invariant 8: the ?token= form is deleted outright (it
        # leaked credentials into access logs / proxies / browser history).
        # A correct secret in the query string must NOT authenticate, even
        # with no Authorization header at all.
        _, _, secret = bootstrapped
        resp = client.post(
            f"/mcp?token={secret}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401

    def test_malformed_json_body_400_not_500(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            content=b"{not valid json",
            headers=_auth(secret),
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == -32700

    def test_get_mcp_returns_405_with_allow_header(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.get("/mcp", headers=_auth(secret))
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST, DELETE"

    def test_get_mcp_without_auth_401_before_405(self, client):
        # auth is checked before the stateless-405 response
        resp = client.get("/mcp")
        assert resp.status_code == 401

    def test_delete_mcp_without_auth_401(self, client):
        resp = client.delete("/mcp")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# #143 invariant 2: principal is server-derived, never a client argument
# ---------------------------------------------------------------------------


class TestPrincipalIsServerDerived:
    def test_tools_call_with_client_subject_id_is_rejected(self, client, bootstrapped):
        """dispatch_tool rejects a client-supplied subject_id outright
        (ForbiddenArgumentError) instead of silently ignoring it -- a
        silently-dropped value would make the caller believe it took
        effect. The rejection surfaces as a normal tool-envelope error
        (HTTP 200, {"error": ...} in the result content), matching how
        every other handler-level error is reported at this transport."""
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ontology_add_node",
                    "arguments": {
                        "space": "subject",
                        "node_type": "User",
                        "node_id": "u1",
                        "subject_id": "someone-else",
                    },
                },
            },
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        content = json_module.loads(resp.json()["result"]["content"][0]["text"])
        assert "error" in content
        assert "subject_id" in content["error"]

    def test_audit_actor_matches_token_owner_regardless_of_client_input(self, client, bootstrapped):
        """The write handler's audit subject is the verified token's owner,
        never anything the client could smuggle in -- since subject_id is
        rejected outright (see above), the only way it could reach the
        builder is via current_principal(), which resolves to the real
        bootstrapped user_id from the presented token."""
        from opencrab.mcp import tools as tools_pkg

        sql, user_id, secret = bootstrapped
        builder = MagicMock()
        builder.add_node.return_value = {"node_id": "u1", "stores": {"graph": "ok"}}

        with patch.object(tools_pkg, "_get_context", return_value=_stub_context(sql, builder=builder)):
            resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "ontology_add_node",
                        "arguments": {"space": "subject", "node_type": "User", "node_id": "u1"},
                    },
                },
                headers=_auth(secret),
            )
        assert resp.status_code == 200
        assert builder.add_node.call_args.kwargs["subject_id"] == user_id


# ---------------------------------------------------------------------------
# Edge: batch requests, notifications, DELETE
# ---------------------------------------------------------------------------


class TestBatchAndNotifications:
    def test_batch_two_requests_returns_array_of_responses(self, client, bootstrapped):
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

    def test_batch_all_notifications_returns_202_empty(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ],
            headers=_auth(secret),
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_empty_batch_list_returns_202(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post("/mcp", json=[], headers=_auth(secret))
        assert resp.status_code == 202
        assert resp.content == b""

    def test_single_notification_no_id_returns_202_empty_body(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_auth(secret),
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_mixed_batch_notification_and_request(self, client, bootstrapped):
        # A notification alongside a real request: only the request gets a
        # response entry, but the batch itself is still a 200 array (not 202)
        # because `out` is non-empty.
        _, _, secret = bootstrapped
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ],
            headers=_auth(secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == 1

    def test_delete_mcp_returns_200_with_auth(self, client, bootstrapped):
        _, _, secret = bootstrapped
        resp = client.delete("/mcp", headers=_auth(secret))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# #150: per-principal MCP tool exposure. ADMIN-tier tools
# (schema_pack_install/uninstall, harness_promotion_apply) are hidden from
# tools/list AND rejected by tools/call for a remote (non-local, token-only)
# principal -- the local bootstrapped user from `bootstrapped`/`client` above
# is_local=True, so it keeps seeing/calling all 16.
# ---------------------------------------------------------------------------

_ADMIN_TOOL_NAMES = {"schema_pack_install", "schema_pack_uninstall", "harness_promotion_apply"}


@pytest.fixture
def remote_secret(bootstrapped):
    """A second user on the SAME sql store as `bootstrapped`, with
    is_local=False -- a token-authenticated remote principal, as opposed to
    the local bootstrapped one."""
    from opencrab.auth import create_user, issue_token

    sql, _local_user_id, _local_secret = bootstrapped
    user_id = create_user(sql, "remote-user", is_local=False)
    _token_id, secret = issue_token(sql, user_id)
    return secret


class TestToolExposureByPrincipal:
    def test_tools_list_admin_tools_hidden_from_remote_principal(self, client, bootstrapped, remote_secret):
        _, _, local_secret = bootstrapped
        local_names = {
            t["name"]
            for t in client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=_auth(local_secret)
            ).json()["result"]["tools"]
        }
        remote_names = {
            t["name"]
            for t in client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=_auth(remote_secret)
            ).json()["result"]["tools"]
        }
        assert _ADMIN_TOOL_NAMES <= local_names
        assert remote_names == local_names - _ADMIN_TOOL_NAMES

    def test_tools_list_no_cross_user_cache_leak_across_interleaved_requests(
        self, client, bootstrapped, remote_secret
    ):
        """Same MCPServer instance (constructed once by `mcp_router`), hit by
        alternating local/remote requests: if tools/list were memoised
        anywhere keyed on something other than the caller's principal, one
        principal's filtered (or unfiltered) list would leak into the
        other's response. Interleaving -- not just "call each once" -- is
        what would actually surface a naive process-global cache."""
        _, _, local_secret = bootstrapped
        for _ in range(3):
            local_names = {
                t["name"]
                for t in client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers=_auth(local_secret),
                ).json()["result"]["tools"]
            }
            remote_names = {
                t["name"]
                for t in client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers=_auth(remote_secret),
                ).json()["result"]["tools"]
            }
            assert _ADMIN_TOOL_NAMES & local_names == _ADMIN_TOOL_NAMES
            assert _ADMIN_TOOL_NAMES & remote_names == set()

    def test_tools_call_admin_tool_denied_for_remote_principal(self, client, remote_secret):
        """The core #150 test: a caller that already knows an ADMIN-tier
        tool's name (it doesn't need tools/list to have shown it) still gets
        refused by tools/call -- the list filter alone would be decoration.
        Surfaces as the normal tool-envelope error (HTTP 200, {"error": ...}
        in the content), same as every other handler-level rejection at this
        transport (see TestPrincipalIsServerDerived above) -- no store is
        ever touched for a denied call."""
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "schema_pack_install", "arguments": {"name": "saas"}},
            },
            headers=_auth(remote_secret),
        )
        assert resp.status_code == 200
        content = json_module.loads(resp.json()["result"]["content"][0]["text"])
        assert "error" in content

    def test_tools_call_read_tool_still_works_for_remote_principal(self, client, remote_secret):
        """Sanity check the denial above is tier-specific, not "remote can't
        call anything": a READ-tier tool works fine for the same principal.
        schema_pack_list needs no store context (see schema.py's module
        docstring), so this needs no _get_context stub."""
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "schema_pack_list", "arguments": {}},
            },
            headers=_auth(remote_secret),
        )
        assert resp.status_code == 200
        content = json_module.loads(resp.json()["result"]["content"][0]["text"])
        assert "error" not in content


# ---------------------------------------------------------------------------
# refuse_stale_shared_secret_env
# ---------------------------------------------------------------------------


class TestRefuseStaleSharedSecretEnv:
    @pytest.mark.parametrize(
        "var_name", ["OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"]
    )
    def test_raises_when_stale_var_set(self, var_name, monkeypatch):
        monkeypatch.setenv(var_name, "leftover-value")
        with pytest.raises(RuntimeError, match=var_name):
            refuse_stale_shared_secret_env()

    def test_no_op_when_unset(self, monkeypatch):
        for var_name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
            monkeypatch.delenv(var_name, raising=False)
        refuse_stale_shared_secret_env()  # must not raise

    def test_blank_value_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("OPENCRAB_API_KEY", "   ")
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN", raising=False)
        monkeypatch.delenv("LOCALCRAB_MCP_TOKEN_FILE", raising=False)
        refuse_stale_shared_secret_env()  # whitespace-only .strip()s to falsy

    def test_create_app_refuses_to_build_with_stale_env(self, monkeypatch):
        monkeypatch.setenv("OPENCRAB_API_KEY", "leftover-value")
        with pytest.raises(RuntimeError, match="OPENCRAB_API_KEY"):
            create_app()
