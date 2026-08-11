"""In-process (no HTTP transport) coverage for #150's per-principal MCP tool
exposure control.

tests/test_http_app.py's ``TestToolExposureByPrincipal`` covers the same
behaviour end-to-end through the real ``/mcp`` transport with real tokens;
this module isolates the two independent gates -- ``tools_for_principal``
(the ``tools/list`` filter) and ``dispatch_tool`` (the ``tools/call`` check)
-- directly, using ``opencrab.auth.principal_scope`` with plain in-memory
``Principal`` objects (no SQLStore/token round trip needed for either gate).
"""

from __future__ import annotations

import pytest

from opencrab.auth import Principal, current_principal, principal_scope
from opencrab.mcp.server import MCPServer
from opencrab.mcp.tools import ToolAccessDeniedError, dispatch_tool, tools_for_principal

_ADMIN_TOOL_NAMES = {"schema_pack_install", "schema_pack_uninstall", "harness_promotion_apply"}

_LOCAL = Principal(user_id="local-1", is_local=True, disabled=False)
_REMOTE = Principal(user_id="remote-1", is_local=False, disabled=False)


class TestToolsForPrincipal:
    def test_local_principal_sees_all_16(self):
        with principal_scope(_LOCAL):
            names = {t["name"] for t in tools_for_principal(current_principal())}
        assert len(names) == 16
        assert _ADMIN_TOOL_NAMES <= names

    def test_remote_principal_does_not_see_admin_tools(self):
        with principal_scope(_REMOTE):
            names = {t["name"] for t in tools_for_principal(current_principal())}
        assert len(names) == 13
        assert names.isdisjoint(_ADMIN_TOOL_NAMES)

    def test_remote_list_is_a_strict_subset_of_local_list(self):
        with principal_scope(_LOCAL):
            local_names = {t["name"] for t in tools_for_principal(current_principal())}
        with principal_scope(_REMOTE):
            remote_names = {t["name"] for t in tools_for_principal(current_principal())}
        assert remote_names < local_names
        assert local_names - remote_names == _ADMIN_TOOL_NAMES


class TestMCPServerToolsListScoping:
    """Same assertions as above, through MCPServer._handle_tools_list /
    handle_request -- the actual JSON-RPC entry point, not just the
    registry-level helper."""

    @pytest.fixture
    def server(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "opencrab.mcp.server.get_settings",
            lambda: MagicMock(mcp_server_name="test", mcp_server_version="0.0.1"),
        )
        return MCPServer()

    def test_tools_list_differs_by_principal(self, server):
        with principal_scope(_LOCAL):
            local = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        with principal_scope(_REMOTE):
            remote = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        local_names = {t["name"] for t in local["result"]["tools"]}
        remote_names = {t["name"] for t in remote["result"]["tools"]}
        assert local_names - remote_names == _ADMIN_TOOL_NAMES

    def test_tools_list_with_no_bound_principal_fails_closed(self, server):
        """No `principal_scope()` open at all -- current_principal() raises
        LookupError (#143: no anonymous fallback). handle_request's generic
        exception handler turns that into a JSON-RPC INTERNAL_ERROR rather
        than falling back to "list everything"."""
        response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "error" in response
        assert "result" not in response


class TestDispatchToolDeniesAdminForRemote:
    """The core #150 test, per the issue body: a caller that already knows
    an ADMIN-tier tool's name (independent of whether tools/list ever showed
    it) is refused by tools/call."""

    @pytest.mark.parametrize("name", sorted(_ADMIN_TOOL_NAMES))
    def test_remote_principal_denied(self, name):
        with principal_scope(_REMOTE):
            with pytest.raises(ToolAccessDeniedError):
                dispatch_tool(name, {})

    def test_denial_happens_before_any_handler_code_runs(self, monkeypatch):
        """A denied call must not reach the handler at all -- patch
        schema_pack_install's only store dependency (the on-disk pack
        registry) to blow up if it's ever touched, and confirm the deny
        still raises ToolAccessDeniedError, not that unrelated error."""
        import opencrab.schemas.pack_registry as pack_registry

        def _boom(*args, **kwargs):
            raise AssertionError("handler body must not run for a denied call")

        monkeypatch.setattr(pack_registry, "install_pack", _boom)
        with principal_scope(_REMOTE):
            with pytest.raises(ToolAccessDeniedError):
                dispatch_tool("schema_pack_install", {"name": "saas"})

    def test_local_principal_not_denied_schema_pack_install(self, monkeypatch):
        """Local principal must clear the tier check. schema_pack_install
        writes real YAML under opencrab/schemas/types/ (a hardcoded path --
        see tests/test_tools_handlers_direct.py's module docstring), so the
        underlying pack_registry call is patched the same way that file
        does; only the tier-check outcome is under test here."""
        import opencrab.schemas.pack_registry as pack_registry

        monkeypatch.setattr(pack_registry, "install_pack", lambda name: {"installed": name})
        with principal_scope(_LOCAL):
            result = dispatch_tool("schema_pack_install", {"name": "saas"})
        assert result == {"installed": "saas"}

    def test_local_principal_not_denied_schema_pack_uninstall(self, monkeypatch):
        import opencrab.schemas.pack_registry as pack_registry

        monkeypatch.setattr(pack_registry, "uninstall_pack", lambda name, force=False: {"removed": name})
        with principal_scope(_LOCAL):
            result = dispatch_tool("schema_pack_uninstall", {"name": "saas"})
        assert result == {"removed": "saas"}

    def test_local_principal_not_denied_harness_promotion_apply(self):
        """An empty package fails PromotionPackage validation (or, if
        `crabharness` isn't installed, the earlier ImportError branch)
        before anything is written -- either way the handler returns an
        {"error": ...} dict rather than raising, so no store patching is
        needed to keep this safe. What's under test is that the failure is
        NOT ToolAccessDeniedError."""
        with principal_scope(_LOCAL):
            result = dispatch_tool("harness_promotion_apply", {"package": {}})
        assert "error" in result


class TestListingFilterIsNotTheOnlyGate:
    def test_hidden_tool_absent_from_list_but_dispatch_is_the_thing_that_blocks_it(self):
        """Directly demonstrates the two independent checks: `tools/list`
        hides `schema_pack_install` from a remote principal, and separately
        `dispatch_tool` -- called with the bare name, exactly as a client
        that already knows it would -- refuses it too. Neither call reuses
        the other's result; each calls the shared predicate independently."""
        with principal_scope(_REMOTE):
            visible = {t["name"] for t in tools_for_principal(current_principal())}
            assert "schema_pack_install" not in visible

            with pytest.raises(ToolAccessDeniedError):
                dispatch_tool("schema_pack_install", {"name": "saas"})
