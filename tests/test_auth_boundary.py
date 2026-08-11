"""#145 auth-boundary contract.

What this pins, and why each one is here rather than assumed:

- No entry point serves traffic without a verified principal. Both stdio
  entry points are covered, including ``python -m opencrab.mcp.server``,
  which review found bypassed the boundary entirely because only
  ``cli.py serve`` had been fixed.
- Query-parameter auth is OFF by default and, when on, is not a second
  verification path. It exists because claude.ai's web UI cannot send an
  Authorization header (see ``docs/mcp-client-auth.md``); deleting it would
  cut that client off.
- A present-but-invalid header does NOT fall back to the query parameter.
  Without that rule, attaching a junk header bypasses the flag entirely.
- CLI writes carry an actor, and a write with no local user stops *before*
  touching a store.

The subprocess tests are subprocesses on purpose: an in-process call cannot
show that a standalone ``python -m`` invocation is gated, which is exactly
the hole that was missed the first time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from opencrab.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    for name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    from opencrab.config import get_settings

    get_settings.cache_clear()
    _reset_tool_context()
    yield tmp_path
    get_settings.cache_clear()
    _reset_tool_context()


def _reset_tool_context():
    """The MCP tool context is a process-global singleton, so without this a
    test would authenticate against whichever data dir happened to build it
    first. A product-level trap too: one process serves one data dir."""
    from opencrab.mcp import tools

    tools._context.clear()


def _sql():
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    return make_sql_store(get_settings())


def _bootstrap() -> tuple[str, str]:
    from opencrab.auth import bootstrap_local_user

    return bootstrap_local_user(_sql())


def _post(client, **kw):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, **kw)


# ---------------------------------------------------------------------------
# Credential sources
# ---------------------------------------------------------------------------


class TestCredentialSources:
    def test_no_credential_is_401(self, env):
        from opencrab.mcp.http_app import create_app

        _bootstrap()
        assert _post(TestClient(create_app())).status_code == 401

    def test_valid_header_is_200(self, env):
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        r = _post(TestClient(create_app()), headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200

    def test_query_token_rejected_when_flag_off(self, env):
        """The default. A correct secret in the URL still fails."""
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        assert _post(TestClient(create_app()), params={"token": secret}).status_code == 401

    def test_query_token_accepted_when_flag_on(self, env):
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        client = TestClient(create_app(allow_query_token=True))
        assert _post(client, params={"token": secret}).status_code == 200

    def test_query_token_still_verified_when_flag_on(self, env):
        """Enabling the flag adds a credential *source*, not a second
        verification path -- an unknown token is rejected either way."""
        from opencrab.mcp.http_app import create_app

        _bootstrap()
        client = TestClient(create_app(allow_query_token=True))
        assert _post(client, params={"token": "lc_not_a_real_token"}).status_code == 401

    def test_revoked_token_rejected_via_query(self, env):
        from opencrab.auth import list_tokens, revoke_token
        from opencrab.mcp.http_app import create_app

        user_id, secret = _bootstrap()
        sql = _sql()
        revoke_token(sql, list_tokens(sql, user_id)[0]["token_id"])
        client = TestClient(create_app(allow_query_token=True))
        assert _post(client, params={"token": secret}).status_code == 401

    def test_invalid_header_does_not_fall_back_to_query(self, env):
        """The bypass this rule exists to stop: if a bad header fell through
        to the query parameter, attaching junk would defeat the flag."""
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        client = TestClient(create_app(allow_query_token=True))
        r = _post(client, headers={"Authorization": "Bearer lc_junk"}, params={"token": secret})
        assert r.status_code == 401

    def test_non_bearer_header_does_not_fall_back_to_query(self, env):
        """HTTPBearer(auto_error=False) yields None for a non-Bearer scheme
        exactly as it does for a missing header, so the code must look at the
        raw header to tell them apart."""
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        client = TestClient(create_app(allow_query_token=True))
        r = _post(client, headers={"Authorization": "Basic Zm9vOmJhcg=="}, params={"token": secret})
        assert r.status_code == 401


class TestNoStore:
    """A URL-borne credential makes the *failure* responses sensitive too:
    their URLs carry the same secret."""

    @pytest.mark.parametrize("case", ["unauthorized", "method_not_allowed", "ok"])
    def test_every_mcp_response_is_no_store(self, env, case):
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        client = TestClient(create_app())
        auth = {"Authorization": f"Bearer {secret}"}
        if case == "unauthorized":
            r = _post(client)
        elif case == "method_not_allowed":
            r = client.get("/mcp", headers=auth)
        else:
            r = _post(client, headers=auth)
        assert r.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _run_module(env_dir, extra_env=None):
    """Run `python -m opencrab.mcp.server` as a real subprocess.

    In-process calls cannot demonstrate that the standalone entry point is
    gated -- that gap is why this is here.
    """
    e = {**os.environ, "LOCAL_DATA_DIR": str(env_dir), "STORAGE_MODE": "local",
         "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
        e.pop(name, None)
    e.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "opencrab.mcp.server"],
        cwd=REPO_ROOT, env=e, input="", capture_output=True, text=True, timeout=90,
    )


class TestStandaloneEntryPoint:
    def test_refuses_without_local_user(self, env):
        p = _run_module(env)
        assert p.returncode != 0
        assert "opencrab init" in p.stderr

    def test_refuses_stale_shared_secret_env(self, env):
        _bootstrap()
        p = _run_module(env, {"OPENCRAB_API_KEY": "leftover"})
        assert p.returncode != 0
        assert "OPENCRAB_API_KEY" in p.stderr

    def test_stale_env_message_names_dotenv_sources(self, env):
        """apps/api promotes repo .env into the environment at import time
        (#88), so an operator who only cleared their shell needs to be told
        where else to look."""
        _bootstrap()
        p = _run_module(env, {"LOCALCRAB_MCP_TOKEN": "leftover"})
        assert ".env" in p.stderr


class TestServeFlagScope:
    def test_stdio_rejects_query_token_flag(self, env):
        """Rejected rather than ignored: stdio carries no HTTP request, so
        silently accepting the flag would leave the operator believing
        query-token auth is on."""
        result = CliRunner().invoke(main, ["serve", "--transport", "stdio", "--allow-query-token"])
        assert result.exit_code != 0
        assert "http" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI writes
# ---------------------------------------------------------------------------


class TestCliWriteActor:
    def test_ingest_without_local_user_fails_before_writing(self, env, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        result = CliRunner().invoke(main, ["ingest", str(tmp_path / "a.txt")])
        assert result.exit_code != 0
        assert "opencrab init" in result.output
        assert "Traceback" not in result.output

    def test_extract_dry_run_needs_no_local_user(self, env, tmp_path):
        """--dry-run writes nothing; requiring a principal there would break a
        command that works today."""
        (tmp_path / "a.md").write_text("x")
        result = CliRunner().invoke(
            main, ["extract", str(tmp_path / "a.md"), "--dry-run", "--api-key", "sk-test"]
        )
        assert "opencrab init" not in result.output

    def test_admin_commands_need_no_local_user(self, env):
        """They are how a local user comes to exist -- gating them would be a
        bootstrap deadlock."""
        r = CliRunner().invoke(main, ["user", "add", "someone"])
        assert r.exit_code == 0
        assert "user_id" in json.loads(r.output)

    def test_ingest_records_principal_as_source_actor(self, env, tmp_path):
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_doc_store

        user_id, _ = _bootstrap()
        src = tmp_path / "note.txt"
        src.write_text("body text")
        result = CliRunner().invoke(main, ["ingest", str(src)])
        assert result.exit_code == 0

        docs = make_doc_store(get_settings())
        rows = docs.list_sources(limit=10)
        assert rows, "ingest wrote no source row"
        # The actor is read off the stored row, not off the call arguments:
        # passing an argument proves nothing about what was persisted.
        assert any((r.get("metadata") or {}).get("user_id") == user_id for r in rows)

        # And off the audit row, which is a separate write that can fail on
        # its own -- checking only the source row would miss that.
        events = docs.get_audit_log(limit=20)
        ingest_events = [e for e in events if e.get("event_type") == "ingest"]
        assert ingest_events, "ingest wrote no audit row"
        assert all(e.get("subject_id") == user_id for e in ingest_events)


class TestHeaderEdgeCases:
    """These pass today for a structural reason, not by accident, and the
    reason is worth pinning: when the flag is off the query parameter is never
    consulted at all, so no header shape can route around it."""

    def test_empty_header_does_not_enable_query_when_flag_off(self, env):
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        client = TestClient(create_app())
        r = _post(client, headers={"Authorization": ""}, params={"token": secret})
        assert r.status_code == 401

    @pytest.mark.parametrize(
        "order,expected",
        [("bad_first", 401), ("good_first", 200)],
        ids=["invalid_first_401", "valid_first_200"],
    )
    def test_duplicate_headers_resolve_to_the_first(self, env, order, expected):
        """Two Authorization headers on one request. A dict cannot express
        that, so they go in as a list of pairs; both orderings are checked
        because header joining is order-dependent.

        The contract is *not* "duplicates are always rejected" -- that was the
        author's first guess and it is wrong. The stack resolves to the first
        header, so:

        - invalid first: 401, and crucially it does NOT try the second header
          or fall back to the query parameter, which is what would turn a
          duplicate into a bypass of ``allow_query_token``
        - valid first: 200, which is correct -- the caller did present a valid
          credential, and a second junk header grants nothing extra

        A front proxy that validates a *different* copy than the app reads
        would be a desync, but that is a proxy-configuration concern; what is
        pinned here is that the app's own choice is deterministic.
        """
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        pair = ["Bearer lc_junk", f"Bearer {secret}"]
        if order == "good_first":
            pair.reverse()
        client = TestClient(create_app(allow_query_token=True))
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=[("authorization", pair[0]), ("authorization", pair[1])],
            params={"token": secret},
        )
        assert r.status_code == expected


class TestBothAppsNoStore:
    """The router is mounted by two apps. A header proven on one says nothing
    about the other -- that split has already been a trap twice here."""

    @pytest.mark.parametrize("which", ["serve", "apps_api"])
    def test_no_store_and_no_redirect(self, env, which):
        _, secret = _bootstrap()
        if which == "serve":
            from opencrab.mcp.http_app import create_app

            app = create_app()
        else:
            import apps.api.main as api_main

            app = api_main.app
        client = TestClient(app, follow_redirects=False)
        auth = {"Authorization": f"Bearer {secret}"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

        slash = client.post("/mcp/", json=body, headers=auth)
        assert slash.status_code != 307, "trailing-slash redirect is back"
        for r in (
            slash,
            client.post("/mcp", json=body),                       # 401
            client.get("/mcp", headers=auth),                     # 405
            client.options("/mcp", headers=auth),
        ):
            assert r.headers.get("cache-control") == "no-store", r.status_code

    def test_alias_absent_from_openapi(self, env):
        import apps.api.main as api_main

        assert "/mcp/" not in api_main.app.openapi().get("paths", {})


class TestPrincipalResolutionCreatesNothing:
    """Both of these were live defects: the PostgreSQL branch ran DDL because
    it went through make_sql_store's default, and a connection failure was
    reported as "run opencrab init" because the unavailable flag was checked
    before the missing-table match could ever be reached. Guards that exist
    but are unreachable are not guards."""

    def test_local_refusal_leaves_no_files(self, env):
        from opencrab.auth import require_local_principal

        assert sorted(os.listdir(env)) == []
        with pytest.raises(RuntimeError, match="opencrab init"):
            require_local_principal()
        assert sorted(os.listdir(env)) == [], "resolution created something"

    def test_connection_failure_is_not_reported_as_missing_bootstrap(
        self, env, monkeypatch
    ):
        """A refused connection must name itself. Telling the operator to run
        init sends them to fix a database that is fine."""
        from opencrab.auth import require_local_principal
        from opencrab.config import get_settings

        monkeypatch.setenv("STORAGE_MODE", "pg")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://nobody:nobody@127.0.0.1:1/nodb")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError) as excinfo:
            require_local_principal()
        assert "opencrab init" not in str(excinfo.value)
        assert "did not connect" in str(excinfo.value)

    def test_postgres_path_does_not_run_ddl(self, env, monkeypatch):
        """Pins create_tables=False on the remote branch. Without it the
        resolution would CREATE the auth tables just to discover they are
        empty."""
        import opencrab.auth as auth_mod
        from opencrab.config import get_settings

        seen = {}
        real = auth_mod.SQLStore if hasattr(auth_mod, "SQLStore") else None
        assert real is None, "SQLStore is imported lazily; adjust this test"

        import opencrab.stores.sql_store as store_mod

        class _Spy(store_mod.SQLStore):
            def __init__(self, url, create_tables=True):  # noqa: FBT002
                seen["create_tables"] = create_tables
                raise RuntimeError("stop before connecting")

        monkeypatch.setattr(store_mod, "SQLStore", _Spy)
        monkeypatch.setenv("STORAGE_MODE", "pg")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://nobody:nobody@127.0.0.1:1/nodb")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):
            require_local_principal_via(auth_mod)
        assert seen.get("create_tables") is False


def require_local_principal_via(auth_mod):
    return auth_mod.require_local_principal()


class TestAuthDoesNoWorkForAnonymousCallers:
    """A junk token used to materialise nine database files: the verify path
    went through the MCP tool context, which builds graph, doc, vector and
    billing stores. Authentication must not let an unauthenticated caller make
    the server do that."""

    def test_rejected_request_creates_no_stores(self, env):
        from opencrab.mcp.http_app import create_app

        client = TestClient(create_app())
        assert sorted(os.listdir(env)) == []
        r = _post(client, headers={"Authorization": "Bearer lc_junk"})
        assert r.status_code == 401
        assert sorted(os.listdir(env)) == [], "a rejected request built stores"

    def test_valid_token_still_authenticates(self, env):
        """The counterpart: narrowing the lookup must not break real auth."""
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        r = _post(TestClient(create_app()), headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200


class TestNoStoreOnServerError:
    def test_unhandled_exception_response_is_no_store(self, env, monkeypatch):
        """Starlette's ServerErrorMiddleware sits outside the user middleware
        stack, so the 500 it builds cannot be touched there -- only an
        Exception handler reaches it. Verified by injection, because reading
        the middleware order does not prove which layer wins."""
        import opencrab.mcp.server as srv
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        app = create_app()

        def _boom(self, request):
            raise RuntimeError("injected")

        monkeypatch.setattr(srv.MCPServer, "handle_request", _boom)
        client = TestClient(app, raise_server_exceptions=False)
        r = _post(client, headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 500
        assert r.headers.get("cache-control") == "no-store"


class TestEmptyHeaderIsStillAHeader:
    def test_empty_authorization_does_not_reach_query_branch(self, env):
        """An empty Authorization is a header the client chose to send. If it
        counted as absent, "the header decides the request" would depend on
        the header's value."""
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        client = TestClient(create_app(allow_query_token=True))
        r = _post(client, headers={"Authorization": ""}, params={"token": secret})
        assert r.status_code == 401


class TestAuthStoreDoesNotCacheFailure:
    """A transient database outage at the moment of the first authentication
    must not become permanent. SQLStore swallows a connect failure into
    ``available = False`` and keeps the object, so caching that object would
    401 every subsequent request until the process restarted -- fail-closed,
    but an availability defect: one blip locks every user out."""

    def test_recovers_after_a_transient_connect_failure(self, env, monkeypatch):
        import opencrab.stores.sql_store as store_mod
        from opencrab.mcp.http_app import create_app

        _, secret = _bootstrap()
        auth = {"Authorization": f"Bearer {secret}"}

        real_init = store_mod.SQLStore.__init__
        state = {"fail": True}

        def _flaky(self, url, create_tables=True):  # noqa: FBT002
            real_init(self, url, create_tables=create_tables)
            if state["fail"]:
                self._available = False
                state["fail"] = False

        monkeypatch.setattr(store_mod.SQLStore, "__init__", _flaky)
        client = TestClient(create_app())

        assert _post(client, headers=auth).status_code == 401, "outage should reject"
        assert _post(client, headers=auth).status_code == 200, "still bricked after recovery"


class TestErrorHandlerStaysInsideMcp:
    """Starlette accepts only one bare-Exception handler, so ours is
    registered app-wide -- but apps/api serves /api/* from that same app.
    Changing those responses would be a behaviour change nobody asked for, so
    the handler re-raises for anything that is not /mcp and lets
    ServerErrorMiddleware answer exactly as it did before."""

    def test_non_mcp_paths_keep_their_original_500(self, env):
        from fastapi import FastAPI

        from opencrab.mcp.http_app import install_mcp_no_store

        app = FastAPI()

        @app.get("/api/thing")
        async def _thing():
            raise RuntimeError("business logic bug")

        install_mcp_no_store(app)
        r = TestClient(app, raise_server_exceptions=False).get("/api/thing")
        assert r.status_code == 500
        # Starlette's own response: no JSON envelope of ours, and no no-store
        # (that header exists for URL-borne credentials, which /api/* has none of).
        assert r.headers.get("cache-control") is None
        assert "detail" not in r.text

    def test_non_mcp_exception_still_propagates_for_logging(self, env):
        from fastapi import FastAPI

        from opencrab.mcp.http_app import install_mcp_no_store

        app = FastAPI()

        @app.get("/api/thing")
        async def _thing():
            raise RuntimeError("business logic bug")

        install_mcp_no_store(app)
        with pytest.raises(RuntimeError, match="business logic bug"):
            TestClient(app, raise_server_exceptions=True).get("/api/thing")


class TestReservedIdentityKeysInPayloads:
    """The argument-level rejection had a door beside it: the same identities
    travelled inside `properties` / `metadata`. `stamp_properties` uses
    setdefault, so a caller-supplied tenant_id or created_by survived to the
    store, and `ontology_add_edge` does not stamp at all so anything passed
    through. The Mongo store additionally mirrors properties.owner_id to a
    top-level column that REST reads as ownership.

    The check walks the whole argument structure rather than guarding the call
    sites. There are six of them today and every hand-written list of "places
    to guard" in this change missed at least one; a walk cannot miss a site it
    has never heard of."""

    @pytest.mark.parametrize(
        "arguments,expected",
        [
            ({"properties": {"tenant_id": "other"}}, "properties.tenant_id"),
            ({"properties": {"created_by": "victim"}}, "properties.created_by"),
            ({"properties": {"owner_id": "victim"}}, "properties.owner_id"),
            ({"properties": {"subject_id": "victim"}}, "properties.subject_id"),
            ({"metadata": {"user_id": "victim"}}, "metadata.user_id"),
            ({"nodes": [{"properties": {"tenant_id": "x"}}]}, "nodes[0].properties.tenant_id"),
            ({"edges": [{"properties": {"created_by": "x"}}]}, "edges[0].properties.created_by"),
            (
                {"package": {"nodes": [{"properties": {"owner_id": "x"}}]}},
                "package.nodes[0].properties.owner_id",
            ),
            ({"a": {"b": [{"c": {"properties": {"owner_id": "x"}}}]}}, "a.b[0].c.properties.owner_id"),
        ],
    )
    def test_reserved_key_is_found_at_any_depth(self, arguments, expected):
        from opencrab.mcp.tools._registry import _reserved_identity_violations

        assert _reserved_identity_violations(arguments) == [expected]

    def test_ordinary_properties_pass(self):
        """The check must not become a reason to stop passing real data."""
        from opencrab.mcp.tools._registry import _reserved_identity_violations

        assert _reserved_identity_violations(
            {"properties": {"title": "hello", "text": "body", "pack_id": "p1"}}
        ) == []

    @pytest.mark.parametrize(
        "tool,arguments",
        [
            (
                "ontology_add_node",
                {
                    "space": "resource",
                    "node_type": "Dataset",
                    "node_id": "dataset:x",
                    "properties": {"tenant_id": "other", "created_by": "victim"},
                },
            ),
            (
                "ontology_add_edge",
                {
                    "from_space": "resource",
                    "from_id": "a",
                    "relation": "owns",
                    "to_space": "resource",
                    "to_id": "b",
                    "properties": {"owner_id": "victim"},
                },
            ),
        ],
    )
    def test_dispatch_rejects_and_writes_nothing(self, env, tool, arguments):
        """Rejected at dispatch, before the handler runs -- so no partial write."""
        from opencrab.auth import Principal, principal_scope
        from opencrab.mcp.tools import dispatch_tool
        from opencrab.mcp.tools._registry import ForbiddenArgumentError

        before = sorted(os.listdir(env))
        with principal_scope(Principal(user_id="u1", is_local=True, disabled=False)):
            with pytest.raises(ForbiddenArgumentError, match="reserved identity key"):
                dispatch_tool(tool, arguments)
        assert sorted(os.listdir(env)) == before
