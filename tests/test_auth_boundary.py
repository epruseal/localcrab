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
import threading
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


def _opencrab_bin() -> Path:
    """The installed console script, resolved the way issue #245 design v13's
    item 13 requires: next to the current interpreter, not via PATH lookup
    (``shutil.which``) -- this must find the venv's own script even when the
    test runner's PATH points elsewhere."""
    return Path(sys.executable).parent / "opencrab"


def _run_serve_stdio(env_dir, extra_env=None):
    """Run ``opencrab serve --transport stdio`` as a real console-script
    subprocess, mirroring ``_run_module`` for the sibling stdio entry point.
    """
    e = {**os.environ, "LOCAL_DATA_DIR": str(env_dir), "STORAGE_MODE": "local",
         "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
        e.pop(name, None)
    e.update(extra_env or {})
    return subprocess.run(
        [str(_opencrab_bin()), "serve", "--transport", "stdio"],
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


# ---------------------------------------------------------------------------
# Issue #245 -- automatic stdio bootstrap on an empty, explicit LOCAL_DATA_DIR
# (design v13, /home/asdf/orch-scratch/o245/design-v13.md).
#
# TDD RED: opencrab.auth.maybe_bootstrap_on_empty, bootstrap_on_empty_requested,
# bootstrap_local_user_idempotent, and bootstrap_local_user(issue_token=...) do
# not exist yet. Every test below either imports one of them directly (fails
# with ImportError/AttributeError) or drives the two stdio entry points
# end-to-end (fails because the unwired entry points still behave like
# current main -- unconditional refusal). Both failure shapes are correct RED
# for this stage; each docstring names the design's acceptance-criteria (AC)
# item it will pin once green.
# ---------------------------------------------------------------------------


class TestAutoBootstrapHappyPath:
    def test_python_dash_m_bootstraps_empty_dir(self, env):
        """AC1: opt-in + explicit LOCAL_DATA_DIR + empty real dir -> `python -m`
        exits 0 on EOF, creates opencrab.db, one local user, zero tokens (F6),
        a stderr bootstrap notice, and no stdout output at all."""
        assert sorted(os.listdir(env)) == []
        p = _run_module(env, {"OPENCRAB_BOOTSTRAP_ON_EMPTY": "1"})
        assert p.returncode == 0, f"stdout={p.stdout!r} stderr={p.stderr!r}"
        assert p.stdout == ""
        assert "bootstrap" in p.stderr.lower()

        db_path = Path(env) / "opencrab.db"
        assert db_path.is_file()

        from opencrab.auth import list_tokens, list_users

        users = list_users(_sql())
        assert len(users) == 1
        assert users[0]["is_local"] is True
        assert list_tokens(_sql(), users[0]["user_id"]) == []


class TestBootstrapOptInGate:
    """G1 (design §3.1): malformed values refuse startup; unset/""/"0" leave
    behaviour completely unchanged (immediate ``None``, no side effects)."""

    @pytest.mark.parametrize("value", ["yes", "true", "TRUE", "on", "2", " 1"])
    def test_malformed_value_refuses_startup(self, env, monkeypatch, value):
        """AC4: a value other than unset/""/"0"/"1" is a loud RuntimeError, not
        a silently-ignored typo."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", value)
        from opencrab.auth import bootstrap_on_empty_requested

        with pytest.raises(RuntimeError):
            bootstrap_on_empty_requested()
        assert sorted(os.listdir(env)) == []

    @pytest.mark.parametrize("value", [None, "", "0"])
    def test_off_values_return_false_and_touch_nothing(self, env, monkeypatch, value):
        """AC2: opt-in unset entirely unchanged -- off means an immediate
        ``None``/``False``, matching current (pre-#245) behaviour exactly."""
        if value is None:
            monkeypatch.delenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", raising=False)
        else:
            monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", value)
        from opencrab.auth import bootstrap_on_empty_requested, maybe_bootstrap_on_empty

        assert bootstrap_on_empty_requested() is False
        assert maybe_bootstrap_on_empty() is None
        assert sorted(os.listdir(env)) == []


class TestBootstrapStorageModeGate:
    """G2 (design §3.1, F2): non-local storage modes refuse even with opt-in,
    so a pg/docker/kuzu deployment can never grow a stray local opencrab.db."""

    @pytest.mark.parametrize("mode", ["docker", "kuzu", "pg"])
    def test_non_local_storage_mode_refuses(self, env, monkeypatch, mode):
        """AC3: G2 across its full 3-mode enumeration -- refuse and create no
        local file."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        monkeypatch.setenv("STORAGE_MODE", mode)
        from opencrab.config import get_settings

        get_settings.cache_clear()
        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(RuntimeError):
            maybe_bootstrap_on_empty()
        assert sorted(os.listdir(env)) == []


class TestBootstrapDataDirGate:
    """G3 (design §3.1, F3): LOCAL_DATA_DIR must be explicitly set (present in
    ``model_fields_set``), non-blank after ``.strip()``, and free of ``?`` --
    the character that splits sqlalchemy's ``make_url`` check/lock target from
    its actual connect target (measured 2026-08-31 against the running
    sqlalchemy version)."""

    def test_unset_local_data_dir_refuses(self, monkeypatch, tmp_path):
        """AC3 (G3, unspecified source): the built-in default derivation must
        never be treated as an explicit opt-in target."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        monkeypatch.setenv("STORAGE_MODE", "local")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)
        from opencrab.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        assert "local_data_dir" not in settings.model_fields_set, (
            "test setup invariant: this case must exercise the unspecified-source "
            "branch, not the explicit one"
        )
        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(RuntimeError, match="LOCAL_DATA_DIR"):
            maybe_bootstrap_on_empty()
        assert not Path(settings.local_data_dir).exists()

    def test_empty_string_local_data_dir_refuses(self, env, monkeypatch):
        """AC3 (G3, F3 counterexample): an explicit but blank value must not
        take the ``Path("")`` (cwd) check / ``sqlite:////opencrab.db`` (root)
        create split -- it is rejected before either is touched."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        monkeypatch.setenv("LOCAL_DATA_DIR", "")
        from opencrab.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        assert "local_data_dir" in settings.model_fields_set
        assert settings.local_data_dir.strip() == ""
        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(RuntimeError, match="LOCAL_DATA_DIR"):
            maybe_bootstrap_on_empty()
        assert not Path("/opencrab.db").exists(), "F3 split must never reach the root create"

    def test_question_mark_path_refuses_and_leaves_the_truncated_target_untouched(
        self, monkeypatch, tmp_path
    ):
        """AC3 (G3, round-2 finding 1 counterexample): ``sqlite:///{dir}/opencrab.db``
        truncates at ``?``, so a real ``a?b`` directory (checked/locked) and its
        truncated sibling ``a`` (what would actually get connected to) must
        BOTH remain untouched by the refusal."""
        real_dir = tmp_path / "a?b"
        real_dir.mkdir()
        truncated = tmp_path / "a"
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        monkeypatch.setenv("STORAGE_MODE", "local")
        monkeypatch.setenv("LOCAL_DATA_DIR", str(real_dir))
        from opencrab.config import get_settings

        get_settings.cache_clear()
        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(RuntimeError):
            maybe_bootstrap_on_empty()
        assert sorted(os.listdir(real_dir)) == []
        assert not truncated.exists()


class TestBootstrapDirectoryMustExist:
    """G4 (design §3.1): a missing directory refuses rather than being
    created -- PLUGIN_DATA's existence is the client's contract, and a typoed
    path must fail loudly, not spawn a new, wrong, empty store."""

    def test_missing_directory_refuses_and_is_not_created(self, monkeypatch, tmp_path):
        """AC3 (G4)."""
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        monkeypatch.setenv("STORAGE_MODE", "local")
        monkeypatch.setenv("LOCAL_DATA_DIR", str(missing))
        from opencrab.config import get_settings

        get_settings.cache_clear()
        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(RuntimeError):
            maybe_bootstrap_on_empty()
        assert not missing.exists()


class TestBootstrapIdempotentFastPath:
    """Design §3.2 step 0: an already-bootstrapped local user is a true no-op
    fast path -- no new store creation, no lock file, and the same
    open/query counts as today's ``require_local_principal`` alone."""

    def test_existing_user_counts_unchanged_and_no_lock_file(self, env, monkeypatch):
        """AC5/AC6: idempotent -- user/token counts unchanged, and fast path
        does not even create ``bootstrap.lock`` (directory listing identical
        before/after, design round-8 finding 1)."""
        user_id, _ = _bootstrap()
        before_files = sorted(os.listdir(env))
        sql = _sql()
        from opencrab.auth import list_tokens, list_users

        users_before = list_users(sql)
        tokens_before = list_tokens(sql, user_id)

        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        from opencrab.auth import maybe_bootstrap_on_empty, require_local_principal

        principal = maybe_bootstrap_on_empty() or require_local_principal()
        assert principal.user_id == user_id
        assert principal.disabled is False

        after_files = sorted(os.listdir(env))
        assert after_files == before_files
        assert "bootstrap.lock" not in after_files

        sql2 = _sql()
        assert list_users(sql2) == users_before
        assert list_tokens(sql2, user_id) == tokens_before

    def test_store_and_query_counts_match_require_local_principal_alone(self, env):
        """AC6 (round-9 finding 1): the ``maybe_bootstrap_on_empty() or
        require_local_principal()`` wiring opens exactly one store
        (``create_tables=False``) and queries ``get_local_user`` exactly
        once -- the same counters as calling ``require_local_principal``
        by itself, measured with the same counting monkeypatch so the
        comparison is apples-to-apples."""
        _bootstrap()

        def _measure(mp, *, opt_in):
            import opencrab.auth as auth_mod
            import opencrab.stores.sql_store as store_mod

            calls = {"store": [], "query": 0}
            real_init = store_mod.SQLStore.__init__
            real_get_local_user = auth_mod.get_local_user

            def _counting_init(self, url, create_tables=True):  # noqa: FBT002
                calls["store"].append({"create_tables": create_tables})
                real_init(self, url, create_tables=create_tables)

            def _counting_get_local_user(sql):
                calls["query"] += 1
                return real_get_local_user(sql)

            mp.setattr(store_mod.SQLStore, "__init__", _counting_init)
            mp.setattr(auth_mod, "get_local_user", _counting_get_local_user)
            if opt_in:
                mp.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
                principal = auth_mod.maybe_bootstrap_on_empty() or auth_mod.require_local_principal()
            else:
                mp.delenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", raising=False)
                principal = auth_mod.require_local_principal()
            return principal, calls

        with pytest.MonkeyPatch.context() as mp:
            principal_wired, calls_wired = _measure(mp, opt_in=True)
        with pytest.MonkeyPatch.context() as mp:
            principal_baseline, calls_baseline = _measure(mp, opt_in=False)

        assert principal_wired.user_id == principal_baseline.user_id
        assert len(calls_wired["store"]) == 1
        assert len(calls_baseline["store"]) == 1
        assert calls_wired["store"][0]["create_tables"] is False
        assert calls_baseline["store"][0]["create_tables"] is False
        assert calls_wired["query"] == 1
        assert calls_baseline["query"] == 1


class TestBootstrapFastPathOpenFailure:
    """Design §3.2 step 0(b): an existing ``opencrab.db`` that fails to open
    (``available=False``) must delegate -- ``None`` -- rather than raise its
    own error, so ``require_local_principal``'s existing "did not connect"
    diagnostic (not "opencrab init") still fires.

    Driven in-process rather than by subprocess: reliably forcing a SQLite
    open failure from outside (permission bits, a corrupt header) is
    platform- and CI-environment-dependent and would make this test flaky.
    Design §3.2 step 0(b) explicitly allows the in-process alternative.
    """

    def test_open_failure_delegates_none_with_zero_side_effects(self, env, monkeypatch):
        """AC5 (round-10 finding 1): available=False -> None, zero lock/helper
        calls, the pre-existing db file untouched (size/mtime), and the
        eventual diagnostic is "did not connect", never "opencrab init"."""
        _bootstrap()
        db_path = Path(env) / "opencrab.db"
        stat_before = db_path.stat()
        before_files = sorted(os.listdir(env))

        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod
        import opencrab.locking as locking_mod
        import opencrab.stores.sql_store as store_mod

        real_init = store_mod.SQLStore.__init__

        def _flaky_init(self, url, create_tables=True):  # noqa: FBT002
            real_init(self, url, create_tables=create_tables)
            self._available = False

        lock_calls = []
        real_file_lock = locking_mod.file_lock

        def _counting_file_lock(*a, **kw):
            lock_calls.append((a, kw))
            return real_file_lock(*a, **kw)

        helper_calls = []

        def _forbidden_helper(*a, **kw):
            helper_calls.append((a, kw))
            raise AssertionError("helper must not run when the store cannot open")

        monkeypatch.setattr(store_mod.SQLStore, "__init__", _flaky_init)
        monkeypatch.setattr(locking_mod, "file_lock", _counting_file_lock)
        monkeypatch.setattr(
            auth_mod, "bootstrap_local_user_idempotent", _forbidden_helper, raising=False
        )

        result = auth_mod.maybe_bootstrap_on_empty()

        assert result is None
        assert lock_calls == []
        assert helper_calls == []
        assert sorted(os.listdir(env)) == before_files
        stat_after = db_path.stat()
        assert (stat_after.st_size, stat_after.st_mtime) == (
            stat_before.st_size,
            stat_before.st_mtime,
        )

        with pytest.raises(RuntimeError) as excinfo:
            result or auth_mod.require_local_principal()
        assert "did not connect" in str(excinfo.value)
        assert "opencrab init" not in str(excinfo.value)


class TestBootstrapFastPathDisabledUser:
    """Design §3.2 step 0(c): a disabled local user delegates too -- no
    reactivation, and the entry point still gets the existing disabled
    error from ``require_local_principal``."""

    def test_disabled_user_delegates_none_without_reactivating(self, env, monkeypatch):
        """AC5: same zero-side-effect contract as the (b) branch."""
        from opencrab.auth import disable_user

        user_id, _ = _bootstrap()
        sql = _sql()
        assert disable_user(sql, user_id) is True
        before_files = sorted(os.listdir(env))

        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod
        import opencrab.locking as locking_mod

        lock_calls = []
        real_file_lock = locking_mod.file_lock

        def _counting_file_lock(*a, **kw):
            lock_calls.append((a, kw))
            return real_file_lock(*a, **kw)

        helper_calls = []

        def _forbidden_helper(*a, **kw):
            helper_calls.append((a, kw))
            raise AssertionError("helper must not run for a disabled user")

        monkeypatch.setattr(locking_mod, "file_lock", _counting_file_lock)
        monkeypatch.setattr(
            auth_mod, "bootstrap_local_user_idempotent", _forbidden_helper, raising=False
        )

        result = auth_mod.maybe_bootstrap_on_empty()

        assert result is None
        assert lock_calls == []
        assert helper_calls == []
        assert sorted(os.listdir(env)) == before_files

        from opencrab.auth import get_local_user

        assert get_local_user(_sql()).disabled is True, "must not reactivate"

        with pytest.raises(RuntimeError, match="disabled"):
            result or auth_mod.require_local_principal()


class TestBootstrapRecoversPartialState:
    """Design §3.2 [F4][F5]: the recovery condition is "no local user", not
    "no db file" -- a half-finished prior run (tables exist, no user row)
    must self-heal on the next opt-in launch."""

    def test_file_and_tables_without_user_self_heals(self, env, monkeypatch):
        """AC5 (F4/F5 condition-mutation detector): reverting the condition
        back to "file absent" makes this fail, because the file already
        exists here and only the user row is missing."""
        from opencrab.config import get_settings
        from opencrab.stores.sql_store import SQLStore

        settings = get_settings()
        sql = SQLStore(url=settings.sqlite_url, create_tables=True)
        assert sql.available
        assert (Path(env) / "opencrab.db").is_file()

        from opencrab.auth import get_local_user

        assert get_local_user(sql) is None

        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")
        from opencrab.auth import maybe_bootstrap_on_empty

        principal = maybe_bootstrap_on_empty()
        assert principal is not None
        assert principal.disabled is False

        sql2 = SQLStore(url=settings.sqlite_url, create_tables=False)
        from opencrab.auth import list_tokens

        assert get_local_user(sql2).user_id == principal.user_id
        assert list_tokens(sql2, principal.user_id) == [], "auto stdio path issues no token (F6)"


class TestBootstrapLockRecheckFindsUser:
    """Design §3.2 steps 2-3 (round-10/11 findings): both recheck points --
    immediately inside the lock, and again after the ``create_tables=True``
    open -- must return the found Principal directly rather than delegating,
    and must not reopen or re-query beyond that point."""

    def test_recheck_immediately_after_lock_returns_principal_without_reopen(
        self, env, monkeypatch
    ):
        """AC6: a concurrent finisher discovered right after acquiring the
        lock skips DDL entirely."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod
        from opencrab.auth import Principal

        injected = Principal(user_id="user_injected", is_local=True, disabled=False)
        responses = iter([None, injected])  # pre-lock fast path, then in-lock recheck

        def _scripted_get_local_user(sql):
            return next(responses, injected)

        def _forbidden_helper(*a, **kw):
            raise AssertionError("must not create_tables=True or create a user")

        monkeypatch.setattr(auth_mod, "get_local_user", _scripted_get_local_user)
        monkeypatch.setattr(
            auth_mod, "bootstrap_local_user_idempotent", _forbidden_helper, raising=False
        )

        import opencrab.stores.sql_store as store_mod

        real_init = store_mod.SQLStore.__init__
        store_create_tables_seen = []

        def _tracking_init(self, url, create_tables=True):  # noqa: FBT002
            store_create_tables_seen.append(create_tables)
            real_init(self, url, create_tables=create_tables)

        monkeypatch.setattr(store_mod.SQLStore, "__init__", _tracking_init)

        result = auth_mod.maybe_bootstrap_on_empty()

        assert result == injected
        assert True not in store_create_tables_seen, "no DDL open after the in-lock recheck hit"

    def test_recheck_after_create_tables_open_returns_principal_without_further_reopen(
        self, env, monkeypatch
    ):
        """AC6 (round-11 finding 2): the SECOND recheck point -- after the
        ``create_tables=True`` open -- must independently return the found
        Principal; a mutant that returns ``None`` only there is caught here."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod
        from opencrab.auth import Principal

        injected = Principal(user_id="user_injected2", is_local=True, disabled=False)
        # fast path miss, in-lock recheck miss, post-DDL-open recheck: found.
        responses = iter([None, None, injected])

        def _scripted_get_local_user(sql):
            return next(responses, injected)

        def _forbidden_helper(*a, **kw):
            raise AssertionError("must not create a user once the post-open recheck finds one")

        monkeypatch.setattr(auth_mod, "get_local_user", _scripted_get_local_user)
        monkeypatch.setattr(
            auth_mod, "bootstrap_local_user_idempotent", _forbidden_helper, raising=False
        )

        result = auth_mod.maybe_bootstrap_on_empty()

        assert result == injected


class TestBootstrapLockOccupancyProbe:
    """Design §3.2 (round-5/6 findings): a decisive, non-racy check that the
    critical section (store open + user creation) really runs INSIDE the
    lock. A synchronous probe from a second thread tries to acquire the same
    ``bootstrap.lock`` (timeout=0.2s) from inside each patched entry point;
    if the lock truly encloses that point the probe must time out.

    Role separation (deliberately not conflated): this test proves only that
    the critical section has not leaked outside the lock. It says nothing
    about single-user convergence CORRECTNESS under real concurrency -- that
    is TestBootstrapRecoversPartialState (condition mutation) and
    TestBootstrapLocalUserIdempotentHelper (IntegrityError convergence). The
    lock is a robustness layer against sqlite contention flakiness; those two
    tests are what guarantees exactly-one-user.
    """

    @staticmethod
    def _probe_from_thread(data_dir):
        from opencrab.locking import file_lock

        result = {}

        def _try():
            try:
                with file_lock("bootstrap.lock", data_dir=data_dir, timeout=0.2):
                    result["acquired"] = True
            except TimeoutError:
                result["timeout"] = True

        t = threading.Thread(target=_try)
        t.start()
        t.join(timeout=5)
        return result

    def test_store_open_point_is_inside_the_lock(self, env, monkeypatch):
        """AC6: probe point (i) -- the ``create_tables=True`` ``SQLStore``
        open used by ``maybe_bootstrap_on_empty``."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.stores.sql_store as store_mod

        real_init = store_mod.SQLStore.__init__
        probe_results = []

        def _probing_init(inner_self, url, create_tables=True):  # noqa: FBT002
            if create_tables:
                probe_results.append(self._probe_from_thread(env))
            real_init(inner_self, url, create_tables=create_tables)

        monkeypatch.setattr(store_mod.SQLStore, "__init__", _probing_init)

        from opencrab.auth import maybe_bootstrap_on_empty

        maybe_bootstrap_on_empty()

        assert probe_results, "the create_tables=True open never ran"
        assert probe_results[0].get("timeout") is True
        assert "acquired" not in probe_results[0]

    def test_helper_creation_point_is_inside_the_lock(self, env, monkeypatch):
        """AC6: probe point (ii) -- ``bootstrap_local_user_idempotent``'s own
        creation work. Opening the store outside the lock but creating the
        user inside it (or vice versa) is exactly the "weakened mutation"
        design v13 §3.2 rejects (round-6 finding 1): both points are probed
        independently, and this one on its own must also fail-closed."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod

        probe_results = []

        def _probing_helper(sql, *, issue_token=True):
            probe_results.append(self._probe_from_thread(env))
            return ("user_fake_helper", None, True)

        monkeypatch.setattr(
            auth_mod, "bootstrap_local_user_idempotent", _probing_helper, raising=False
        )

        auth_mod.maybe_bootstrap_on_empty()

        assert probe_results, "the create helper never ran"
        assert probe_results[0].get("timeout") is True
        assert "acquired" not in probe_results[0]


class TestBootstrapLockAcquisitionErrors:
    """Design §3.2 step 1 (round-2/3 findings): a lock-acquisition failure
    converts to a contextual RuntimeError (both stdio entry points only
    handle RuntimeError, so an uncaught TimeoutError/OSError would leak a
    traceback) -- but ONLY at acquisition. An OSError raised by the guarded
    body itself must propagate unconverted, or a real body bug gets
    misdiagnosed as "couldn't get the lock"."""

    @pytest.mark.parametrize("exc_cls", [TimeoutError, PermissionError])
    def test_lock_acquisition_failure_is_wrapped_and_creates_nothing(
        self, env, monkeypatch, exc_cls
    ):
        """AC4."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.locking as locking_mod

        def _boom(*a, **kw):
            raise exc_cls("simulated lock acquisition failure")

        monkeypatch.setattr(locking_mod, "file_lock", _boom)

        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(RuntimeError) as excinfo:
            maybe_bootstrap_on_empty()
        assert not isinstance(excinfo.value, (TimeoutError, PermissionError))
        assert sorted(os.listdir(env)) == []

    def test_oserror_inside_lock_body_is_not_converted(self, env, monkeypatch):
        """AC4 (misdiagnosis guard): the real ``file_lock`` runs unpatched
        here -- only the body (the helper call) raises -- so a conversion
        that wraps the whole ``with`` block rather than just acquisition
        would wrongly turn this into a RuntimeError."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod

        def _boom(*a, **kw):
            raise OSError("simulated failure inside the critical section, not at acquire")

        monkeypatch.setattr(auth_mod, "bootstrap_local_user_idempotent", _boom, raising=False)

        from opencrab.auth import maybe_bootstrap_on_empty

        with pytest.raises(OSError) as excinfo:
            maybe_bootstrap_on_empty()
        assert not isinstance(excinfo.value, RuntimeError)


class TestBootstrapLockErrorAtEntryPoints:
    """Design §3.2 step 1 (round-4 finding 2): with a directory (not a file)
    already sitting at ``bootstrap.lock``, the lock file open fails with
    ``IsADirectoryError`` unpatched -- a real injection, not a mock. Both
    stdio entry points must turn this into exit 1, stderr-only, no
    traceback, no db file -- exactly like any other lock-acquisition
    failure (see TestBootstrapLockAcquisitionErrors)."""

    def test_python_dash_m_reports_context_and_exits_1(self, env):
        """AC4, AC7 (entry-point pinning)."""
        (Path(env) / "bootstrap.lock").mkdir()
        p = _run_module(env, {"OPENCRAB_BOOTSTRAP_ON_EMPTY": "1"})
        assert p.returncode == 1
        assert p.stdout == ""
        assert p.stderr.strip() != ""
        assert "Traceback" not in p.stderr
        assert not (Path(env) / "opencrab.db").exists()

    def test_console_script_serve_stdio_reports_context_and_exits_1(self, env):
        """AC4, AC7 -- the sibling entry point, `opencrab serve --transport
        stdio`, via the actual installed console script."""
        (Path(env) / "bootstrap.lock").mkdir()
        p = _run_serve_stdio(env, {"OPENCRAB_BOOTSTRAP_ON_EMPTY": "1"})
        assert p.returncode == 1
        assert p.stdout == ""
        assert p.stderr.strip() != ""
        assert "Traceback" not in p.stderr
        assert not (Path(env) / "opencrab.db").exists()


class TestBootstrapLocalUserIdempotentHelper:
    """Design §3.3/§4 [F6]: the extracted helper's own unit contract --
    IntegrityError race convergence, the ``created`` flag driving the stderr
    notice, and ``issue_token=False`` issuing no token."""

    def test_integrity_error_converges_to_existing_user_created_false(self, env, monkeypatch):
        """AC5: a concurrent creator (e.g. `opencrab init` under pg) winning
        the race converges to the row that landed, with created=False."""
        from sqlalchemy.exc import IntegrityError

        from opencrab.auth import bootstrap_local_user_idempotent

        sql = _sql()
        existing_id, _ = _bootstrap()

        import opencrab.auth as auth_mod

        def _always_conflicts(sql_, *, issue_token=True):
            raise IntegrityError("INSERT", {}, Exception("idx_users_single_local"))

        monkeypatch.setattr(auth_mod, "bootstrap_local_user", _always_conflicts)

        user_id, secret, created = bootstrap_local_user_idempotent(sql, issue_token=False)

        assert created is False
        assert user_id == existing_id
        assert secret is None

    def test_created_false_means_no_bootstrap_notice(self, env, monkeypatch, capsys):
        """AC5 (design §3.2 step 7): the stderr creation notice is gated on
        ``created=True`` only -- a helper that converged onto an existing
        row (created=False) must print nothing new."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod
        from opencrab.auth import Principal

        injected = Principal(user_id="user_x", is_local=True, disabled=False)

        def _fake_helper(sql, *, issue_token=True):
            return ("user_x", None, False)

        # fast-path miss, in-lock recheck miss, post-open recheck miss (-> helper
        # runs, created=False), final post-helper recheck: found, enabled.
        responses = iter([None, None, None, injected])

        def _scripted_get_local_user(sql):
            return next(responses, injected)

        monkeypatch.setattr(auth_mod, "get_local_user", _scripted_get_local_user)
        monkeypatch.setattr(auth_mod, "bootstrap_local_user_idempotent", _fake_helper)

        result = auth_mod.maybe_bootstrap_on_empty()

        assert result == injected
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_issue_token_false_creates_zero_tokens(self, env):
        """AC5 [F6]: the stdio auto path's ``issue_token=False`` really
        issues nothing -- unlike ``opencrab init``'s default."""
        from opencrab.auth import bootstrap_local_user_idempotent, list_tokens

        sql = _sql()
        user_id, secret, created = bootstrap_local_user_idempotent(sql, issue_token=False)
        assert created is True
        assert secret is None
        assert list_tokens(sql, user_id) == []


class TestBootstrapDisableRaceAfterCreate:
    """Design §3.2 step 6 (round-12 finding 1, BLOCKER): the window between
    the helper's commit and the final post-create recheck is where an
    external process could disable the just-created user. The final check
    must apply the same enabled test as every other discovery branch --
    returning a truthy Principal here would let a disabled user slip past
    ``require_local_principal``'s authoritative disabled check."""

    def test_disabled_immediately_after_creation_delegates_none(self, env, monkeypatch):
        """AC5."""
        monkeypatch.setenv("OPENCRAB_BOOTSTRAP_ON_EMPTY", "1")

        import opencrab.auth as auth_mod
        from opencrab.auth import disable_user

        created_holder = {}

        def _disabling_helper(sql, *, issue_token=True):
            from opencrab.auth import bootstrap_local_user

            user_id, secret = bootstrap_local_user(sql, issue_token=issue_token)
            disable_user(sql, user_id)
            created_holder["user_id"] = user_id
            return (user_id, secret, True)

        monkeypatch.setattr(auth_mod, "bootstrap_local_user_idempotent", _disabling_helper)

        result = auth_mod.maybe_bootstrap_on_empty()

        assert result is None
        assert created_holder, "helper never ran"

        with pytest.raises(RuntimeError, match="disabled"):
            result or auth_mod.require_local_principal()


class TestServeStdioRejectionStreams:
    """Design §4 [F8]: all four `serve --transport stdio` rejection
    diagnostics go to stderr only (a dedicated ``err_console =
    Console(stderr=True)``), never to stdout -- the JSON-RPC channel must
    stay clean even on a rejected startup.

    Per design §4's test plan: the stale-secret and missing-principal cases
    run as real console-script subprocesses (they were already exercised
    that way pre-#245); --allow-query-token and the registry violation use
    a stream-separated CliRunner (Click 8.2+ keeps ``result.stdout`` and
    ``result.stderr`` genuinely separate, so no subprocess is needed there).
    """

    def test_stale_secret_rejection_is_stderr_only(self, env):
        """AC7."""
        p = _run_serve_stdio(env, {"OPENCRAB_API_KEY": "leftover"})
        assert p.returncode != 0
        assert p.stdout == ""
        assert p.stderr.strip() != ""

    def test_missing_principal_rejection_is_stderr_only(self, env):
        """AC7."""
        p = _run_serve_stdio(env)
        assert p.returncode != 0
        assert p.stdout == ""
        assert p.stderr.strip() != ""

    def test_allow_query_token_stdio_rejection_is_stderr_only(self, env):
        """AC7."""
        result = CliRunner().invoke(main, ["serve", "--transport", "stdio", "--allow-query-token"])
        assert result.exit_code != 0
        assert result.stdout == ""
        assert result.stderr.strip() != ""

    def test_registry_violation_rejection_is_stderr_only(self, env):
        """AC7. The violation shape mirrors
        TestStartupCheck.test_refuses_when_the_graph_holds_an_unregistered_pack_id
        in test_read_scope_isolation.py: a graph node carrying a pack_id that
        the ``packs`` registry (opencrab.pack.read_scope) never heard of."""
        from opencrab.config import get_settings
        from opencrab.stores.factory import make_graph_store

        _bootstrap()
        cfg = get_settings()
        graph = make_graph_store(cfg)
        graph.upsert_node(
            "Document",
            "ghost-node",
            {"node_id": "ghost-node", "pack_id": "ghost-pack"},
            space_id="resource",
        )
        close = getattr(graph, "close", None)
        if callable(close):
            close()

        result = CliRunner().invoke(main, ["serve", "--transport", "stdio"])
        assert result.exit_code != 0
        assert result.stdout == ""
        assert result.stderr.strip() != ""


class TestConcurrentSubprocessBootstrap:
    """Design §3.2 (round-2 finding 4(c)): integration evidence, not a
    mutation detector -- two real `python -m` children racing to bootstrap
    the same empty directory must both converge cleanly. Correctness
    (exactly-one-user) is guaranteed by TestBootstrapRecoversPartialState's
    condition test and TestBootstrapLocalUserIdempotentHelper's
    IntegrityError-convergence test; the lock (TestBootstrapLockOccupancyProbe)
    is the robustness layer against sqlite contention flakiness. This test
    confirms those guarantees actually cooperate across two real processes.
    """

    def test_two_children_converge_to_one_user(self, env):
        """AC6."""
        e = {**os.environ, "LOCAL_DATA_DIR": str(env), "STORAGE_MODE": "local",
             "PYTHONDONTWRITEBYTECODE": "1", "OPENCRAB_BOOTSTRAP_ON_EMPTY": "1"}
        for name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
            e.pop(name, None)

        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "opencrab.mcp.server"],
                cwd=REPO_ROOT, env=e, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            for _ in range(2)
        ]
        results = [p.communicate(input="", timeout=90) for p in procs]

        for p, (out, err) in zip(procs, results):
            assert p.returncode == 0, f"stdout={out!r} stderr={err!r}"

        from opencrab.auth import list_tokens, list_users

        sql = _sql()
        users = list_users(sql)
        assert len(users) == 1
        assert list_tokens(sql, users[0]["user_id"]) == []
