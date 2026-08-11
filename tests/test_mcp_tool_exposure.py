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
from opencrab.mcp.tools import (
    ForbiddenArgumentError,
    UnknownToolError,
    dispatch_tool,
    tools_for_principal,
)
from opencrab.mcp.tools._registry import _REGISTRY

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
    it) is refused by tools/call.

    #150 v3 (D2): the denial is reported as ``UnknownToolError``, not a
    dedicated ``ToolAccessDeniedError`` -- that class was removed because,
    correctly implemented, it is unreachable: dispatch_tool's lookup and
    authorization are now one merged gate (see ``_registry.dispatch_tool``'s
    docstring), so "registered but wrong tier" and "not registered at all"
    produce the identical exception a real caller can observe."""

    @pytest.mark.parametrize("name", sorted(_ADMIN_TOOL_NAMES))
    def test_remote_principal_denied(self, name):
        with principal_scope(_REMOTE):
            with pytest.raises(UnknownToolError):
                dispatch_tool(name, {})

    def test_denial_happens_before_any_handler_code_runs(self, monkeypatch):
        """A denied call must not reach the handler at all -- patch
        schema_pack_install's only store dependency (the on-disk pack
        registry) to blow up if it's ever touched, and confirm the deny
        still raises UnknownToolError, not that unrelated error."""
        import opencrab.schemas.pack_registry as pack_registry

        def _boom(*args, **kwargs):
            raise AssertionError("handler body must not run for a denied call")

        monkeypatch.setattr(pack_registry, "install_pack", _boom)
        with principal_scope(_REMOTE):
            with pytest.raises(UnknownToolError):
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
        NOT UnknownToolError -- the local principal must clear the tier
        gate before ever reaching the handler."""
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

            with pytest.raises(UnknownToolError):
                dispatch_tool("schema_pack_install", {"name": "saas"})


class TestHiddenIndistinguishableFromUnregistered:
    """#150 v3, D1: the response for a requested name N must depend only on
    N and the caller's visible tool set -- never on whether N happens to
    exist in the global ``_REGISTRY``. Compares, for the SAME remote
    principal and SAME name, (a) the tool present but ADMIN-tier (hidden)
    against (b) the tool removed from ``_REGISTRY`` outright: the two
    ``dispatch_tool()`` calls must raise byte-identical messages."""

    def test_hidden_and_removed_raise_identical_message(self):
        name = "schema_pack_install"

        with principal_scope(_REMOTE):
            with pytest.raises(UnknownToolError) as hidden_exc:
                dispatch_tool(name, {})

        snapshot = dict(_REGISTRY)
        try:
            del _REGISTRY[name]
            with principal_scope(_REMOTE):
                with pytest.raises(UnknownToolError) as removed_exc:
                    dispatch_tool(name, {})
        finally:
            _REGISTRY.clear()
            _REGISTRY.update(snapshot)

        # dict membership AND order restored -- a leaked mutation here would
        # silently corrupt the golden TOOLS ordering for every later test.
        assert list(_REGISTRY.keys()) == list(snapshot.keys())
        assert _REGISTRY == snapshot

        assert str(hidden_exc.value) == str(removed_exc.value)

    def test_available_list_in_message_is_scoped_to_the_principal_not_the_raw_registry(self):
        """The "Available" list embedded in the message is exactly
        ``tools_for_principal(principal)``'s names -- never the full
        registry -- which is *why* the hidden/removed cases above collapse
        to the same string: schema_pack_install was excluded from that list
        either way (#150 v3 criterion 13: also checked from a remote
        principal, not just the existing local-principal assertions in
        tests/test_mcp_dispatch_extended.py and
        tests/test_tool_registry_contract.py)."""
        with principal_scope(_REMOTE):
            with pytest.raises(UnknownToolError) as exc_info:
                dispatch_tool("does_not_exist_at_all", {})
            visible = {t["name"] for t in tools_for_principal(current_principal())}

        message = str(exc_info.value)
        assert "Available:" in message
        for name in visible:
            assert name in message
        for name in _ADMIN_TOOL_NAMES:
            assert name not in message


class TestSubjectIdDoesNotBypassTheTierGate:
    """v1 regression (codex's finding): a remote caller could attach a
    client-supplied ``subject_id`` to an ADMIN-tier call, hoping
    argument-validation (then the first gate) would answer with
    ForbiddenArgumentError -- confirming the tool exists -- before the tier
    check ever ran. #150 v3 fixes the ORDER: lookup+authorization (gate 2)
    now runs before argument validation (gate 3), so this must land on the
    identical UnknownToolError an unregistered name would produce."""

    def test_remote_admin_tool_with_subject_id_is_unknown_not_forbidden_arg(self):
        with principal_scope(_REMOTE):
            with pytest.raises(UnknownToolError):
                dispatch_tool("schema_pack_install", {"name": "saas", "subject_id": "attacker-controlled"})

    def test_remote_allowed_tool_with_subject_id_is_still_forbidden_arg(self):
        """Contrast: the identical forbidden argument, on a tool the remote
        principal IS authorized to call (WRITE-tier), still reaches gate 3
        and raises ForbiddenArgumentError as before -- the reordering only
        changes the outcome for a tool the caller was never allowed to
        reach in the first place."""
        with principal_scope(_REMOTE):
            with pytest.raises(ForbiddenArgumentError):
                dispatch_tool(
                    "ontology_add_node",
                    {
                        "space": "subject",
                        "node_type": "User",
                        "node_id": "u1",
                        "subject_id": "attacker-controlled",
                    },
                )


class TestHiddenCallNeverReachesTheHandler:
    """#150 v3 criterion 10: "the handler doesn't run" must be proven by
    replacing the handler with a spy and asserting zero calls -- NOT by
    observing "the store was never touched", which harness_promotion_apply
    can satisfy even when its handler DOES run (an empty/invalid package
    fails PromotionPackage validation and returns before touching any
    store, dry_run or otherwise)."""

    def test_handler_not_invoked_for_hidden_admin_tool(self, monkeypatch):
        import dataclasses

        name = "harness_promotion_apply"
        spec = _REGISTRY[name]
        calls: list[dict] = []
        spy = dataclasses.replace(spec, fn=lambda **kw: calls.append(kw) or {"ok": True})
        monkeypatch.setitem(_REGISTRY, name, spy)

        with principal_scope(_REMOTE):
            with pytest.raises(UnknownToolError):
                dispatch_tool(name, {"package": {}})

        assert calls == []

    def test_spy_sanity_handler_is_invoked_when_the_call_is_actually_allowed(self, monkeypatch):
        """Same spy, same tool, LOCAL principal (which IS ADMIN-tiered):
        proves the spy would have recorded a call had dispatch_tool reached
        the handler -- so the zero-calls assertion above is not vacuous."""
        import dataclasses

        name = "harness_promotion_apply"
        spec = _REGISTRY[name]
        calls: list[dict] = []
        spy = dataclasses.replace(spec, fn=lambda **kw: calls.append(kw) or {"ok": True})
        monkeypatch.setitem(_REGISTRY, name, spy)

        with principal_scope(_LOCAL):
            result = dispatch_tool(name, {"package": {}})

        assert calls == [{"package": {}}]
        assert result == {"ok": True}


class TestRemoteCanStillCallReadTools:
    """#150 v3 criterion 9: the fix hides ADMIN-tier tools from remote
    principals -- it must NOT make remote principals unable to call
    anything. Mirrors tests/test_http_app.py's
    ``test_tools_call_read_tool_still_works_for_remote_principal`` at the
    dispatch level (no HTTP transport needed for this assertion)."""

    def test_dispatch_read_tool_succeeds_for_remote_principal(self):
        with principal_scope(_REMOTE):
            result = dispatch_tool("ontology_manifest", {})
        assert "spaces" in result


class TestFourDispatchStatesExactCodesAndBodies:
    """#150 v3 criterion 11: pin the exact JSON-RPC code (and, where
    relevant, message contents) for each of the four distinguishable states
    a ``tools/call`` request can land in, through the real
    ``MCPServer.handle_request`` entry point (not a mocked dispatch_tool)."""

    @pytest.fixture
    def server(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "opencrab.mcp.server.get_settings",
            lambda: MagicMock(mcp_server_name="test", mcp_server_version="0.0.1"),
        )
        return MCPServer()

    @staticmethod
    def _call(server, name):
        return server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}
        )

    def test_principal_unbound_is_a_generic_tool_exception_not_method_not_found(self, server):
        """No principal_scope() open at all -- current_principal() raises a
        bare LookupError (#150 v3 D4: dispatch_tool now resolves the
        principal before anything else, so this fires before the name is
        even looked up). ``_handle_tools_call`` re-raises ONLY
        UnknownToolError specially (see its docstring) -- any other
        exception, LookupError included, falls into the SAME generic
        tool-exception envelope every other handler-level failure gets:
        HTTP-level JSON-RPC success (no top-level "error"), with
        ``{"error": str(exc)}`` inside ``result.content``. This is
        unchanged, pre-existing ``_handle_tools_call`` behavior -- #150 v3
        touched dispatch_tool's internal ordering, not this catch-all.
        Contrast tools/list, which has no such catch-all and DOES surface
        an unbound principal as INTERNAL_ERROR (see
        TestMCPServerToolsListScoping.test_tools_list_with_no_bound_principal_fails_closed
        above) -- the two methods are not symmetric here, and only
        tools/list's asymmetry is INTERNAL_ERROR."""
        import json

        response = self._call(server, "ontology_manifest")
        assert "error" not in response
        content = json.loads(response["result"]["content"][0]["text"])
        assert "error" in content

    def test_unbound_principal_beats_name_lookup_for_an_unregistered_name(self):
        """#150 v3 D4, the distinguishing case: dispatch_tool resolves the
        principal in step 1, BEFORE the registry lookup in step 2. With no
        principal bound, an unregistered name therefore raises a bare
        LookupError -- not the UnknownToolError it raised before #150.
        The server-level test above cannot pin this down: it goes through a
        registered name, where both orderings would fail identically."""
        with pytest.raises(LookupError) as excinfo:
            dispatch_tool("ghost-never-registered", {})
        assert not isinstance(excinfo.value, UnknownToolError)

    def test_hidden_admin_tool_for_remote_is_method_not_found(self, server):
        with principal_scope(_REMOTE):
            response = self._call(server, "schema_pack_install")
        assert response["error"]["code"] == -32601
        assert "schema_pack_install" in response["error"]["message"]
        assert "result" not in response

    def test_genuinely_unknown_tool_is_method_not_found(self, server):
        with principal_scope(_REMOTE):
            response = self._call(server, "this_name_was_never_registered")
        assert response["error"]["code"] == -32601
        assert "this_name_was_never_registered" in response["error"]["message"]
        assert "result" not in response

    def test_missing_name_param_is_invalid_params(self, server):
        with principal_scope(_REMOTE):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {}}}
            )
        assert response["error"]["code"] == -32602
        assert "result" not in response
